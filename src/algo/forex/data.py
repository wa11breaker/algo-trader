"""Loading Dukascopy 1-minute bars and building broker-server-time bars.

Source files: data/forex/<sym>/dukascopy/csv/<SYM>_M1_<year>_UTC.csv with columns
datetime_utc,open,high,low,close,volume (bid prices, bar start in UTC, zero-volume
filler minutes already removed). Each year is cached as parquet next to the CSVs.

Broker "server time" is New York time + 7h (UTC+2 in winter, UTC+3 in US summer),
so the trading day starts at the 17:00 New York rollover and a 4H chart has six
bars a day starting 00:00, 04:00, ... server time, as on MT4/MT5 brokers.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

SERVER_SHIFT = pd.Timedelta(hours=7)


def source_dir(data_dir: Path, symbol: str) -> Path:
    return Path(data_dir) / "forex" / symbol.lower() / "dukascopy"


def _year(src: Path, symbol: str, year: int) -> pd.DataFrame:
    pq = src / "parquet" / f"{symbol}_M1_{year}.parquet"
    csv = src / "csv" / f"{symbol}_M1_{year}_UTC.csv"
    if pq.exists() and (not csv.exists() or pq.stat().st_mtime >= csv.stat().st_mtime):
        return pd.read_parquet(pq)
    if not csv.exists():
        return pd.DataFrame()
    df = pd.read_csv(csv)
    df.index = pd.to_datetime(df.pop("datetime_utc"), format="%Y-%m-%d %H:%M")
    df.index.name = "utc"
    df = df[~df.index.duplicated()].sort_index().astype("float64")
    pq.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(pq)
    return df


def load_m1(data_dir: Path, symbol: str, start: date, end: date) -> pd.DataFrame:
    """1-minute bid bars indexed by naive UTC bar start, for [start, end] inclusive."""
    src = source_dir(data_dir, symbol)
    frames = [_year(src, symbol, y) for y in range(start.year, end.year + 1)]
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise FileNotFoundError(f"No {symbol} 1-minute data under {src}")
    df = pd.concat(frames)
    return df.loc[pd.Timestamp(start): pd.Timestamp(end) + pd.Timedelta(hours=23, minutes=59)]


def to_server(idx_utc: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Naive UTC -> naive broker server time (New York + 7h, DST aware)."""
    ny = idx_utc.tz_localize("UTC").tz_convert("America/New_York").tz_localize(None)
    return ny + SERVER_SHIFT


def resample_server(m1: pd.DataFrame, hours: int = 4) -> pd.DataFrame:
    """OHLCV bars aligned to server time.

    Index = bar start in server time. Extra columns: `i0`/`i1` = positional range
    [i0, i1) of the bar's minutes inside `m1`, `first_utc`/`last_utc` = UTC start of
    its first/last minute. A bar is complete once its last minute has closed.
    """
    srv = to_server(m1.index)
    bucket = srv.floor(f"{hours}h")
    pos = pd.RangeIndex(len(m1))
    g = pd.DataFrame({
        "open": m1["open"].to_numpy(), "high": m1["high"].to_numpy(),
        "low": m1["low"].to_numpy(), "close": m1["close"].to_numpy(),
        "volume": m1["volume"].to_numpy() if "volume" in m1 else 0.0,
        "pos": pos, "utc": m1.index,
    }, index=bucket).groupby(level=0, sort=True)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
        "i0": g["pos"].min(), "i1": g["pos"].max() + 1,
        "first_utc": g["utc"].min(), "last_utc": g["utc"].max(),
    })
    out.index.name = "server"
    return out
