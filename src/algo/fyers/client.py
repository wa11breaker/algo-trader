"""Thin Fyers REST client (market data only: profile, 1-min history, quotes, symbol master).

No order endpoints: paper trading never sends real orders.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from pathlib import Path

import httpx
import pandas as pd

from algo.data.store import COLUMNS
from algo.upstox.client import RateLimiter

log = logging.getLogger(__name__)
API = "https://api-t1.fyers.in/api/v3"
DATA = "https://api-t1.fyers.in/data"
MASTER_URL = "https://public.fyers.in/sym_details/{segment}_sym_master.json"
IST = "Asia/Kolkata"


class FyersError(RuntimeError):
    pass


def candles_to_df(candles: list[list]) -> pd.DataFrame:
    """Fyers candles [epoch_s, o, h, l, c, v(, oi)] -> DataFrame indexed by naive IST bar start."""
    if not candles:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="ts"), dtype=float)
    rows = [list(c[:7]) + [0] * (7 - len(c)) for c in candles]
    df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
    ts = pd.to_datetime(df.pop("ts"), unit="s", utc=True).dt.tz_convert(IST).dt.tz_localize(None)
    df.index = pd.DatetimeIndex(ts, name="ts").as_unit("ns")
    df = df.astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _epoch(ts: datetime) -> int:
    """Naive IST datetime -> epoch seconds."""
    return int(pd.Timestamp(ts).tz_localize(IST).timestamp())


class FyersClient:
    def __init__(self, app_id: str, access_token: str, rps: int = 8, rpm: int = 180,
                 max_retries: int = 4) -> None:
        self.app_id, self.access_token = app_id, access_token
        self.http = httpx.Client(headers={"Authorization": f"{app_id}:{access_token}"}, timeout=30)
        self.limiter = RateLimiter({1: rps, 60: rpm})
        self.max_retries = max_retries

    @property
    def socket_token(self) -> str:
        return f"{self.app_id}:{self.access_token}"

    def get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                r = self.http.get(url, params=params)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise
                log.warning("network error %s, retrying", e)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_retries:
                    break
                log.warning("HTTP %s on %s, retrying", r.status_code, url)
                time.sleep(2 ** attempt)
                continue
            try:
                body = r.json()
            except ValueError:
                raise FyersError(f"Fyers {r.status_code}: {r.text[:300]}")
            if r.status_code != 200 or body.get("s") != "ok":
                hint = ""
                if body.get("code") in (-8, -15, -16, -17):
                    hint = " -> token expired/invalid, run `algo fyers-login`"
                raise FyersError(f"Fyers error on {url}: {body}{hint}")
            return body
        raise FyersError(f"Fyers {r.status_code} on {url}: {r.text[:300]}")

    # ---- account ----------------------------------------------------------
    def profile(self) -> dict:
        return self.get(f"{API}/profile").get("data") or {}

    # ---- candles ----------------------------------------------------------
    def history(self, symbol: str, start: datetime, end: datetime, resolution: str = "1",
                oi: bool = False) -> pd.DataFrame:
        """1-min (default) candles with bar start in [start, end]. Fyers serves at most 100 days
        of 1-min data per call; this splits longer ranges. Pass end <= the last *closed* minute,
        otherwise the forming candle comes back too."""
        frames, lo = [], pd.Timestamp(start)
        hi_all = pd.Timestamp(end)
        while lo <= hi_all:
            hi = min(hi_all, lo + pd.Timedelta(days=99))
            body = self.get(f"{DATA}/history", {
                "symbol": symbol, "resolution": resolution, "date_format": 0,
                "range_from": _epoch(lo), "range_to": _epoch(hi), "cont_flag": 1,
                **({"oi_flag": 1} if oi else {})})
            frames.append(candles_to_df(body.get("candles") or []))
            lo = hi + pd.Timedelta(minutes=1)
        df = pd.concat(frames) if frames else candles_to_df([])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= hi_all)]

    # ---- quotes -----------------------------------------------------------
    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """symbol -> {'lp', 'bid', 'ask', 'volume', ...}; 50 symbols per request."""
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            body = self.get(f"{DATA}/quotes", {"symbols": ",".join(chunk)})
            for item in body.get("d") or []:
                if item.get("s") == "ok":
                    out[item["n"]] = item.get("v") or {}
        return out

    # ---- symbol master (public, no auth) ----------------------------------
    @staticmethod
    def symbol_master(segment: str, cache_dir: Path | None = None, today: date | None = None) -> dict:
        """Fyers symbol master for a segment (NSE_FO, BSE_FO, ...), cached once per day."""
        import json
        today = today or date.today()
        path = Path(cache_dir) / f"{segment}_{today:%Y%m%d}.json" if cache_dir else None
        if path and path.exists():
            return json.loads(path.read_text())
        r = httpx.get(MASTER_URL.format(segment=segment), timeout=120)
        r.raise_for_status()
        data = r.json()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            for old in path.parent.glob(f"{segment}_*.json"):
                old.unlink()
            path.write_text(json.dumps(data))
        return data
