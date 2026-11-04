"""MarketData implementation over the local Parquet store (for backtests)."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from algo.config import Underlying
from algo.core.broker import MarketData
from algo.core.models import OptionContract, OptionType
from algo.data.store import DataStore
from algo.indicators import resample

log = logging.getLogger(__name__)

Bar = tuple[float, float, float, float]  # open, high, low, close
SESSION_START, SESSION_LAST_BAR = time(9, 15), time(15, 29)


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars that start within the regular 09:15-15:29 session.

    Upstox index candles for some periods include a 09:07 pre-open bar and a 15:30/15:31 closing
    price bar (and Muhurat sessions in the evening); those are not tradable minutes and would
    distort day ranges (previous-day high/low), the first/last resampled bars and indicators.
    """
    if df.empty:
        return df
    t = df.index.time
    keep = (t >= SESSION_START) & (t <= SESSION_LAST_BAR)
    return df if keep.all() else df[keep]


class BarFrames:
    """Spot / futures bar access shared by HistoricalData and LiveMarketData.

    Needs `spot_1m`, `fut_1m` (1-min DataFrames, index = bar start), `ul` and a `_tf` cache dict.
    """
    spot_1m: pd.DataFrame
    fut_1m: pd.DataFrame
    _tf: dict

    @property
    def has_futures(self) -> bool:
        return len(self.fut_1m) > 0

    def _frame(self, tf: str, source: str = "spot") -> tuple[pd.DataFrame, np.ndarray]:
        key = (source, tf)
        if key not in self._tf:
            base = self.spot_1m if source == "spot" else self.fut_1m
            df = resample(base, tf)
            self._tf[key] = (df.drop(columns="bar_end"), df["bar_end"].to_numpy())
        return self._tf[key]

    def futures_bars(self, until: datetime, tf: str = "1min", lookback: int | None = None) -> pd.DataFrame:
        if not self.has_futures:
            raise RuntimeError(f"No {self.ul.name} futures data stored. Run `algo download` (without --spot-only).")
        return self._slice(self._frame(tf, "futures"), until, lookback)

    def spot_bars(self, until: datetime, tf: str = "1min", lookback: int | None = None) -> pd.DataFrame:
        return self._slice(self._frame(tf), until, lookback)

    @staticmethod
    def _slice(frame, until: datetime, lookback: int | None) -> pd.DataFrame:
        df, ends = frame
        close_t = np.datetime64(pd.Timestamp(until) + pd.Timedelta(minutes=1), "ns")
        i = int(np.searchsorted(ends, close_t, side="right"))
        j = 0 if lookback is None else max(0, i - lookback)
        return df.iloc[j:i]


class HistoricalData(BarFrames, MarketData):
    def __init__(self, store: DataStore, underlying: Underlying, start: date, end: date,
                 warmup_days: int = 10, spot: pd.DataFrame | None = None,
                 futures_roll_days_before: int = 1, options: bool = True) -> None:
        """options=False hides all option contracts (index-only research runs)."""
        self.store = store
        self.ul = underlying
        first = start - timedelta(days=warmup_days)
        self.spot_1m = regular_session(spot if spot is not None else store.load_spot(underlying.name, first, end))
        self.fut_1m = regular_session(store.continuous_futures(underlying.name, first, end, futures_roll_days_before))
        if len(self.fut_1m) and len(self.spot_1m):
            # Index has no volume: borrow front-month futures volume / OI per minute.
            self.spot_1m = self.spot_1m.copy()
            self.spot_1m["volume"] = self.fut_1m["volume"].reindex(self.spot_1m.index).fillna(0.0)
            self.spot_1m["oi"] = (self.fut_1m["oi"].reindex(self.spot_1m.index)
                                  .groupby(self.spot_1m.index.normalize()).ffill().fillna(0.0))
        self._tf: dict[tuple[str, str], tuple[pd.DataFrame, np.ndarray]] = {}
        self.options_enabled = options
        self._expiries = store.expiries(underlying.name) if options else []
        self._contracts: dict[date, pd.DataFrame | None] = {}
        self._options: dict[tuple, tuple[pd.DataFrame, dict, np.ndarray]] = {}
        self._missing_warned: set = set()

    # ---- options ----------------------------------------------------------
    def expiries(self, on: date) -> list[date]:
        return [e for e in self._expiries if e >= on]

    def _contracts_for(self, expiry: date) -> pd.DataFrame | None:
        if expiry not in self._contracts:
            self._contracts[expiry] = self.store.load_contracts(self.ul.name, expiry)
        return self._contracts[expiry]

    def strikes(self, expiry: date) -> list[float]:
        c = self._contracts_for(expiry)
        return [] if c is None else sorted(c["strike_price"].astype(float).unique())

    def contract(self, expiry: date, strike: float, option_type: OptionType) -> OptionContract | None:
        c = self._contracts_for(expiry)
        if c is None:
            return None
        row = c[(c["strike_price"].astype(float) == float(strike)) & (c["instrument_type"] == option_type.value)]
        if row.empty:
            return None
        if not self.store.option_path(self.ul.name, expiry, strike, option_type.value).exists():
            k = (expiry, strike, option_type)
            if k not in self._missing_warned:
                self._missing_warned.add(k)
                log.debug("no candle data for %s %s %s", expiry, strike, option_type.value)
            return None
        r = row.iloc[0]
        return OptionContract(underlying=self.ul.name, expiry=expiry, strike=float(strike),
                              option_type=option_type, lot_size=int(r["lot_size"]),
                              instrument_key=str(r["instrument_key"]), exchange=self.ul.exchange)

    def _option(self, c: OptionContract):
        key = (c.expiry, c.strike, c.option_type)
        if key not in self._options:
            df = self.store.load_option(self.ul.name, c.expiry, c.strike, c.option_type.value)
            if df is None:
                df = pd.DataFrame(columns=["open", "high", "low", "close"], index=pd.DatetimeIndex([]))
            bars = dict(zip(df.index, zip(df["open"], df["high"], df["low"], df["close"])))
            self._options[key] = (df, bars, df.index.to_numpy())
        return self._options[key]

    def option_bar(self, c: OptionContract, ts: datetime) -> Bar | None:
        return self._option(c)[1].get(pd.Timestamp(ts))

    def option_ltp(self, c: OptionContract, at: datetime) -> float | None:
        df, _, idx = self._option(c)
        i = int(np.searchsorted(idx, np.datetime64(pd.Timestamp(at), "ns"), side="right"))
        return None if i == 0 else float(df["close"].iloc[i - 1])
