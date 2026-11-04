"""1-min bars + approximate order flow (buy / sell volume, delta) from live feed snapshots.

Neither Fyers nor Upstox streams individual trades: the feed sends snapshots (Fyers: about every
100-300 ms) with the last price, cumulative day volume and best bid/ask. Each snapshot's new volume
(change in cumulative volume) is classified as:
  * buy  if the trade price >= the previous snapshot's ask (buyer lifted the offer)
  * sell if the trade price <= the previous snapshot's bid (seller hit the bid)
  * otherwise the tick rule: up-tick = buy, down-tick = sell, unchanged = same side as last time
  * no history at all yet: split half / half
Several trades between two snapshots are merged into one, so delta is an approximation of what
a true tick-by-tick footprint shows; treat small deltas as noise.
"""
from __future__ import annotations

import csv
import fcntl
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pathlib import Path
from typing import Callable

import pandas as pd

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
BAR_COLUMNS = ["open", "high", "low", "close", "volume", "buy_vol", "sell_vol", "delta"]


@dataclass
class Quote:
    ltp: float
    bid: float | None = None
    ask: float | None = None
    ts: datetime | None = None     # naive IST time of the snapshot
    volume: float | None = None    # cumulative day volume


@dataclass
class _SymState:
    prev_vol: float | None = None
    prev_ltp: float | None = None
    prev_bid: float | None = None
    prev_ask: float | None = None
    last_side: int = 0                      # +1 buy, -1 sell, 0 unknown
    bars: dict = field(default_factory=dict)   # minute -> [o, h, l, c, vol, buy, sell]


def to_ist(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, IST).replace(tzinfo=None)


TICK_COLUMNS = ["ts", "symbol", "ltp", "bid", "ask", "cum_vol", "vol", "buy", "sell"]


class TickRecorder:
    """Appends every classified feed snapshot of the wanted symbols to <root>/<day>.csv, so the
    footprint can be rebuilt later (any timeframe / price step) with `footprint()`.
    Several `algo paper` processes may run at once: a lock file lets only the first one record."""

    def __init__(self, root: Path, want: Callable[[str], bool]) -> None:
        self.root, self.want = root, want
        self._day = None
        self._f = None
        self._w = None
        self._lock = None

    def _acquire(self) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        f = (self.root / ".lock").open("w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)   # released when this process exits
        except BlockingIOError:
            f.close()
            log.info("another process is recording footprint ticks in %s; not recording here", self.root)
            self.want = lambda s: False
            return False
        self._lock = f
        return True

    def write(self, symbol: str, ts: datetime, ltp: float, bid, ask, cum_vol, vol: float, buy: float, sell: float) -> None:
        if not self.want(symbol) or (self._lock is None and not self._acquire()):
            return
        if ts.date() != self._day:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{ts.date().isoformat()}.csv"
            new = not path.exists()
            if self._f:
                self._f.close()
            self._f = path.open("a", newline="", buffering=1)   # line-buffered: a crash loses at most one row
            self._w = csv.writer(self._f)
            if new:
                self._w.writerow(TICK_COLUMNS)
            self._day = ts.date()
        self._w.writerow([ts.isoformat(), symbol, ltp, bid, ask, cum_vol, vol, buy, sell])


def footprint(ticks: pd.DataFrame, tf: str = "1min", step: float = 1.0) -> pd.DataFrame:
    """Footprint from recorded ticks: buy / sell volume per (bar, price level), index (bar, price).
    `step` = price bucket in points (NIFTY futures tick is 0.10; 1-5 points reads better)."""
    t = ticks[ticks["vol"] > 0]
    bar = pd.to_datetime(t["ts"]).dt.floor(tf).rename("bar")
    price = ((t["ltp"] / step).round() * step).rename("price")
    fp = t.groupby([bar, price])[["buy", "sell"]].sum()
    fp["delta"] = fp["buy"] - fp["sell"]
    return fp


class BarBuilder:
    """Thread-safe: the websocket thread calls `on_tick`, the trading loop reads `bars` / `quote`."""

    def __init__(self, recorder: TickRecorder | None = None) -> None:
        self.recorder = recorder
        self._lock = threading.Lock()
        self._s: dict[str, _SymState] = {}
        self._quotes: dict[str, Quote] = {}

    def on_tick(self, symbol: str, ts: datetime, ltp: float | None, volume: float | None = None,
                bid: float | None = None, ask: float | None = None) -> None:
        if ltp is None or ltp <= 0:
            return
        bid = bid if bid and bid > 0 else None
        ask = ask if ask and ask > 0 else None
        minute = ts.replace(second=0, microsecond=0)
        with self._lock:
            st = self._s.setdefault(symbol, _SymState())
            dv = 0.0
            if volume is not None:
                if st.prev_vol is not None and volume > st.prev_vol:
                    dv = float(volume - st.prev_vol)
                st.prev_vol = volume if st.prev_vol is None else max(st.prev_vol, volume)
            buy = sell = 0.0
            if dv > 0:
                side = self._classify(st, ltp)
                if side > 0:
                    buy = dv
                elif side < 0:
                    sell = dv
                else:
                    buy = sell = dv / 2
                if side:
                    st.last_side = side
            b = st.bars.get(minute)
            if b is None:
                st.bars[minute] = [ltp, ltp, ltp, ltp, dv, buy, sell]
            else:
                b[1], b[2], b[3] = max(b[1], ltp), min(b[2], ltp), ltp
                b[4] += dv
                b[5] += buy
                b[6] += sell
            if self.recorder is not None:
                try:
                    self.recorder.write(symbol, ts, ltp, bid, ask, volume, dv, buy, sell)
                except Exception:   # noqa: BLE001 - recording must never stop the live feed
                    log.exception("tick recording failed; disabled for this session")
                    self.recorder = None
            st.prev_ltp = ltp
            if bid is not None:
                st.prev_bid = bid
            if ask is not None:
                st.prev_ask = ask
            self._quotes[symbol] = Quote(ltp=ltp, bid=bid, ask=ask, ts=ts, volume=volume)

    @staticmethod
    def _classify(st: _SymState, ltp: float) -> int:
        if st.prev_ask is not None and ltp >= st.prev_ask:
            return 1
        if st.prev_bid is not None and ltp <= st.prev_bid:
            return -1
        if st.prev_ltp is not None:
            if ltp > st.prev_ltp:
                return 1
            if ltp < st.prev_ltp:
                return -1
        return st.last_side

    def quote(self, symbol: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(symbol)

    def bar(self, symbol: str, minute: datetime) -> tuple[float, float, float, float] | None:
        with self._lock:
            st = self._s.get(symbol)
            b = st.bars.get(pd.Timestamp(minute).to_pydatetime()) if st else None
            return None if b is None else (b[0], b[1], b[2], b[3])

    def bars(self, symbol: str, start: datetime | None = None, until: datetime | None = None) -> pd.DataFrame:
        """Bars with start <= bar start <= until, as a DataFrame (index = bar start)."""
        with self._lock:
            st = self._s.get(symbol)
            items = sorted(st.bars.items()) if st else []
        rows = [(t, *v) for t, v in items
                if (start is None or t >= start) and (until is None or t <= until)]
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "buy_vol", "sell_vol"])
        df = df.set_index(pd.DatetimeIndex(df.pop("ts"), name="ts").as_unit("ns")).astype(float)
        df["delta"] = df["buy_vol"] - df["sell_vol"]
        return df

    def prune(self, before: datetime) -> None:
        """Drop bars older than `before` (keeps memory flat over a long session)."""
        with self._lock:
            for st in self._s.values():
                for t in [t for t in st.bars if t < before]:
                    del st.bars[t]
