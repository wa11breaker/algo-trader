"""Settings loader: YAML for parameters, .env for secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "settings.yaml"


@dataclass(frozen=True)
class Underlying:
    name: str
    instrument_key: str
    exchange: str
    strike_step: int
    fyers_symbol: str = ""     # index symbol on Fyers, e.g. NSE:NIFTY50-INDEX

    @property
    def fyers_fo_master(self) -> str:
        """Fyers symbol-master segment file holding this index's futures and options."""
        return f"{self.exchange}_FO"

    @property
    def fo_segment(self) -> str:
        return f"{self.exchange}_FO"


@dataclass
class Settings:
    raw: dict
    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / self.raw.get("data_dir", "data")

    @property
    def reports_dir(self) -> Path:
        return self.root / self.raw.get("reports_dir", "reports")

    def underlying(self, name: str) -> Underlying:
        name = name.upper()
        cfg = self.raw["underlyings"].get(name)
        if cfg is None:
            raise KeyError(f"Unknown underlying {name!r}; known: {list(self.raw['underlyings'])}")
        return Underlying(name=name, **cfg)

    @property
    def costs(self) -> dict:
        return self.raw["costs"]

    @property
    def execution(self) -> dict:
        return self.raw["execution"]

    @property
    def download(self) -> dict:
        return self.raw["download"]

    @property
    def backtest(self) -> dict:
        return self.raw["backtest"]

    @property
    def paper(self) -> dict:
        return self.raw.get("paper") or {}


@lru_cache
def get_settings(path: str | None = None) -> Settings:
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p) as f:
        raw = yaml.safe_load(f)
    return Settings(raw=raw, root=ROOT)


def env(name: str) -> str:
    load_dotenv(ROOT / ".env")
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing {name} in environment / .env (see .env.example)")
    return val
