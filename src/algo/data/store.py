"""Local Parquet store.

Layout:
  data/spot/{UL}/{YYYY-MM}.parquet                    1-min index candles
  data/options/{UL}/{YYYY-MM-DD}/contracts.parquet    contract metadata for that expiry
  data/options/{UL}/{YYYY-MM-DD}/{strike}{CE|PE}.parquet
  data/options/{UL}/{YYYY-MM-DD}/_empty.json          contracts the API returned no candles for
  data/futures/{UL}/{YYYY-MM-DD}.parquet              1-min candles of the future expiring that day
  data/futures/{UL}/contracts.parquet                 futures contract metadata
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume", "oi"]


def candles_to_df(candles: list[list]) -> pd.DataFrame:
    """Upstox candle arrays -> DataFrame indexed by naive IST timestamp (bar start), ascending."""
    if not candles:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="ts"), dtype=float)
    rows = [c[:7] + [0] * (7 - len(c)) for c in candles]
    df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
    ts = pd.to_datetime(df.pop("ts"), utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df.index = pd.DatetimeIndex(ts, name="ts").as_unit("ns")
    df = df.astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _fmt_strike(strike: float) -> str:
    return f"{strike:g}"


class DataStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ---- spot -------------------------------------------------------------
    def spot_path(self, ul: str, year: int, month: int) -> Path:
        return self.root / "spot" / ul / f"{year:04d}-{month:02d}.parquet"

    def save_spot_month(self, ul: str, year: int, month: int, df: pd.DataFrame) -> Path:
        p = self.spot_path(ul, year, month)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p)
        return p

    def load_spot(self, ul: str, start: date, end: date) -> pd.DataFrame:
        frames = []
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            p = self.spot_path(ul, y, m)
            if p.exists():
                frames.append(pd.read_parquet(p))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if not frames:
            return candles_to_df([])
        df = pd.concat(frames).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
        return df[(df.index >= lo) & (df.index < hi)]

    # ---- options ----------------------------------------------------------
    def expiry_dir(self, ul: str, expiry: date) -> Path:
        return self.root / "options" / ul / expiry.isoformat()

    def save_contracts(self, ul: str, expiry: date, contracts: list[dict]) -> pd.DataFrame:
        d = self.expiry_dir(ul, expiry)
        d.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(contracts)
        keep = ["instrument_key", "trading_symbol", "strike_price", "instrument_type", "lot_size",
                "expiry", "exchange", "segment", "tick_size", "weekly", "underlying_key"]
        df = df[[c for c in keep if c in df.columns]]
        df.to_parquet(d / "contracts.parquet")
        return df

    def load_contracts(self, ul: str, expiry: date) -> pd.DataFrame | None:
        p = self.expiry_dir(ul, expiry) / "contracts.parquet"
        return pd.read_parquet(p) if p.exists() else None

    def option_path(self, ul: str, expiry: date, strike: float, option_type: str) -> Path:
        return self.expiry_dir(ul, expiry) / f"{_fmt_strike(strike)}{option_type}.parquet"

    def save_option(self, ul: str, expiry: date, strike: float, option_type: str, df: pd.DataFrame) -> Path:
        p = self.option_path(ul, expiry, strike, option_type)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p)
        return p

    def load_option(self, ul: str, expiry: date, strike: float, option_type: str) -> pd.DataFrame | None:
        p = self.option_path(ul, expiry, strike, option_type)
        return pd.read_parquet(p) if p.exists() else None

    def empty_set(self, ul: str, expiry: date) -> set[str]:
        p = self.expiry_dir(ul, expiry) / "_empty.json"
        return set(json.loads(p.read_text())) if p.exists() else set()

    def mark_empty(self, ul: str, expiry: date, keys: set[str]) -> None:
        p = self.expiry_dir(ul, expiry) / "_empty.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(keys), indent=1))

    def expiries(self, ul: str) -> list[date]:
        base = self.root / "options" / ul
        if not base.exists():
            return []
        out = []
        for d in base.iterdir():
            if d.is_dir() and (d / "contracts.parquet").exists():
                try:
                    out.append(date.fromisoformat(d.name))
                except ValueError:
                    pass
        return sorted(out)

    # ---- futures ----------------------------------------------------------
    def futures_dir(self, ul: str) -> Path:
        return self.root / "futures" / ul

    def future_path(self, ul: str, expiry: date) -> Path:
        return self.futures_dir(ul) / f"{expiry.isoformat()}.parquet"

    def save_future(self, ul: str, expiry: date, df: pd.DataFrame, meta: dict) -> Path:
        p = self.future_path(ul, expiry)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p)
        mp = self.futures_dir(ul) / "contracts.parquet"
        row = pd.DataFrame([{k: meta.get(k) for k in ("instrument_key", "trading_symbol", "expiry", "lot_size")}])
        if mp.exists():
            old = pd.read_parquet(mp)
            row = pd.concat([old[old["expiry"] != meta.get("expiry")], row])
        row.sort_values("expiry").to_parquet(mp, index=False)
        return p

    def future_expiries(self, ul: str) -> list[date]:
        d = self.futures_dir(ul)
        if not d.exists():
            return []
        out = []
        for p in d.glob("*.parquet"):
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                pass
        return sorted(out)

    def load_future(self, ul: str, expiry: date) -> pd.DataFrame | None:
        p = self.future_path(ul, expiry)
        return pd.read_parquet(p) if p.exists() else None

    def continuous_futures(self, ul: str, start: date, end: date, roll_days_before: int = 1) -> pd.DataFrame:
        """Front-month futures, 1-min, unadjusted.

        On each day the contract used is the nearest expiry with
        day <= expiry - roll_days_before (calendar days). Default 1 = switch to the
        next month on expiry day, when most volume has already rolled over.
        Adds an `expiry` column so you can see which contract each bar came from.
        If the front contract has no bars on a day (a data gap at the source, e.g. Upstox has
        no NIFTY Dec-2024 futures candles), the next contract is used for that day instead.
        """
        exps = self.future_expiries(ul)
        if not exps:
            return pd.DataFrame(columns=[*COLUMNS, "expiry"], index=pd.DatetimeIndex([], name="ts"))
        frames, cache = [], {}
        day = start
        while day <= end:
            candidates = [e for e in exps if day <= e - timedelta(days=roll_days_before)][:2]
            if not candidates:
                break
            lo, hi = pd.Timestamp(day), pd.Timestamp(day) + pd.Timedelta(days=1)
            for exp in candidates:          # front month, else next month
                if exp not in cache:
                    cache[exp] = self.load_future(ul, exp)
                df = cache[exp]
                part = df[(df.index >= lo) & (df.index < hi)] if df is not None else df
                if part is not None and len(part):
                    part = part.copy()
                    part["expiry"] = exp.isoformat()
                    frames.append(part)
                    break
            day += timedelta(days=1)
        if not frames:
            return pd.DataFrame(columns=[*COLUMNS, "expiry"], index=pd.DatetimeIndex([], name="ts"))
        return pd.concat(frames)

