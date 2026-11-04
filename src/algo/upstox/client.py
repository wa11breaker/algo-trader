"""Thin Upstox REST client with rate limiting and retries (data endpoints only for now)."""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import date
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)
BASE = "https://api.upstox.com"


class UpstoxError(RuntimeError):
    def __init__(self, status: int, body: dict | str):
        self.status, self.body = status, body
        codes = []
        if isinstance(body, dict):
            codes = [e.get("errorCode") or e.get("error_code") for e in body.get("errors", [])]
        self.codes = [c for c in codes if c]
        hint = ""
        if "UDAPI1149" in self.codes:
            hint = " -> This API needs Upstox Plus. Activate it in the Upstox app, then run `algo login` again."
        super().__init__(f"Upstox API error {status}: {body}{hint}")


class RateLimiter:
    """Sliding-window limiter over several windows, e.g. {1: 8, 60: 400, 1800: 1800}."""

    def __init__(self, limits: dict[float, int]) -> None:
        self.limits = limits
        self.calls: deque[float] = deque()
        self.horizon = max(limits)

    def wait(self) -> None:
        while True:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > self.horizon:
                self.calls.popleft()
            sleep_for = 0.0
            for window, limit in self.limits.items():
                recent = [t for t in self.calls if now - t < window]
                if len(recent) >= limit:
                    sleep_for = max(sleep_for, window - (now - recent[0]) + 0.01)
            if sleep_for <= 0:
                self.calls.append(now)
                return
            if sleep_for > 5:
                log.info("rate limit: sleeping %.0fs", sleep_for)
            time.sleep(sleep_for)


def _k(instrument_key: str) -> str:
    return quote(instrument_key, safe="")


class UpstoxClient:
    def __init__(self, token: str, rps: int = 8, rpm: int = 400, r30m: int = 1800, max_retries: int = 5) -> None:
        self.http = httpx.Client(
            base_url=BASE,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        )
        self.limiter = RateLimiter({1: rps, 60: rpm, 1800: r30m})
        self.max_retries = max_retries

    def get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                r = self.http.get(path, params=params)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise
                log.warning("network error %s, retrying", e)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_retries:
                    break
                wait = 2 ** attempt
                log.warning("HTTP %s on %s, retry in %ss", r.status_code, path, wait)
                time.sleep(wait)
                continue
            try:
                body = r.json()
            except ValueError:
                body = r.text
            if r.status_code != 200 or (isinstance(body, dict) and body.get("status") == "error"):
                raise UpstoxError(r.status_code, body)
            return body
        raise UpstoxError(r.status_code, r.text)

    # ---- profile ----------------------------------------------------------
    def profile(self) -> dict:
        return self.get("/v2/user/profile")["data"]

    # ---- historical candles (V3; spot/indices & active instruments) -------
    def historical_candles(self, instrument_key: str, unit: str, interval: int,
                           to_date: date, from_date: date) -> list[list]:
        path = f"/v3/historical-candle/{_k(instrument_key)}/{unit}/{interval}/{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}"
        return self.get(path)["data"]["candles"]

    # ---- instrument master (public BOD file, no auth) ---------------------
    @staticmethod
    def instrument_master(exchange: str = "NSE") -> list[dict]:
        """Today's tradable instruments for an exchange (NSE, BSE, ...). Expiry is epoch ms (IST end of day)."""
        import gzip
        import json
        url = f"https://assets.upstox.com/market-quote/instruments/exchange/{exchange}.json.gz"
        r = httpx.get(url, timeout=60)
        r.raise_for_status()
        return json.loads(gzip.decompress(r.content))

    # ---- expired instruments (Upstox Plus) --------------------------------
    def expired_expiries(self, underlying_key: str) -> list[str]:
        return self.get("/v2/expired-instruments/expiries", {"instrument_key": underlying_key})["data"]

    def expired_option_contracts(self, underlying_key: str, expiry: date | str) -> list[dict]:
        exp = expiry if isinstance(expiry, str) else expiry.isoformat()
        return self.get("/v2/expired-instruments/option/contract",
                        {"instrument_key": underlying_key, "expiry_date": exp})["data"] or []

    def expired_future_contracts(self, underlying_key: str, expiry: date | str) -> list[dict]:
        exp = expiry if isinstance(expiry, str) else expiry.isoformat()
        return self.get("/v2/expired-instruments/future/contract",
                        {"instrument_key": underlying_key, "expiry_date": exp})["data"] or []

    def expired_candles(self, expired_key: str, interval: str, to_date: date, from_date: date) -> list[list]:
        path = (f"/v2/expired-instruments/historical-candle/{_k(expired_key)}/{interval}/"
                f"{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}")
        return (self.get(path).get("data") or {}).get("candles") or []
