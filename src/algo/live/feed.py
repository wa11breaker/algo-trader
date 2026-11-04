"""Live Fyers data socket -> BarBuilder (1-min bars, quotes, order flow).

Uses the official SDK (`pip install -e '.[live]'`), which handles Fyers' binary protocol,
pings and reconnects. Messages arrive on the SDK's thread; `BarBuilder` is thread-safe.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from algo.live.orderflow import BarBuilder, to_ist

log = logging.getLogger(__name__)


class FyersFeed:
    def __init__(self, socket_token: str, builder: BarBuilder, log_path: str | None = None,
                 max_symbols: int = 100) -> None:
        """max_symbols: Fyers allows 100 subscribed symbols per user on the Standard plan."""
        self.token = socket_token
        self.max_symbols = max_symbols
        self.builder = builder
        self.log_path = log_path
        self.subscribed: set[str] = set()
        self.connected = threading.Event()
        self.last_message: float = 0.0
        self.messages = 0
        self._ws = None
        self._lock = threading.Lock()

    # ---- SDK callbacks ------------------------------------------------------
    def _on_message(self, msg: dict) -> None:
        if not isinstance(msg, dict) or "symbol" not in msg or "ltp" not in msg:
            return
        ts_s = msg.get("exch_feed_time") or msg.get("last_traded_time")
        ts = to_ist(ts_s) if ts_s else datetime.now()
        self.builder.on_tick(msg["symbol"], ts, msg.get("ltp"), msg.get("vol_traded_today"),
                             msg.get("bid_price"), msg.get("ask_price"))
        self.last_message = time.monotonic()
        self.messages += 1

    def _on_connect(self) -> None:
        log.info("Fyers data socket connected")
        self.connected.set()
        if self.subscribed:   # after a reconnect
            self._ws.subscribe(symbols=sorted(self.subscribed), data_type="SymbolUpdate")

    def _on_error(self, msg) -> None:
        log.warning("Fyers socket error: %s", msg)

    def _on_close(self, msg) -> None:
        log.warning("Fyers socket closed: %s", msg)
        self.connected.clear()

    # ---- control ------------------------------------------------------------
    def start(self, symbols: list[str], timeout_s: float = 20) -> None:
        try:
            from fyers_apiv3.FyersWebsocket import data_ws
        except ImportError as e:
            raise RuntimeError("Fyers SDK missing. Install it with:  pip install -e '.[live]'") from e
        self._ws = data_ws.FyersDataSocket(
            access_token=self.token, log_path=self.log_path or "", litemode=False, write_to_file=False,
            reconnect=True, reconnect_retry=50, on_connect=self._on_connect, on_close=self._on_close,
            on_error=self._on_error, on_message=self._on_message)
        self._ws.connect()
        if not self.connected.wait(timeout_s):
            raise RuntimeError("Fyers data socket did not connect (token expired? run `algo fyers-login`)")
        self.subscribe(symbols)
        # The SDK silently drops a subscribe sent before its token check finished: retry once.
        deadline = time.monotonic() + 10
        while not self.messages and time.monotonic() < deadline:
            time.sleep(0.5)
        if not self.messages:
            log.warning("no feed data 10 s after subscribing; subscribing again")
            self.resubscribe()

    def resubscribe(self) -> None:
        """Send the whole subscription list again (watchdog / after a silent feed)."""
        if self._ws is not None and self.subscribed:
            self._ws.subscribe(symbols=sorted(self.subscribed), data_type="SymbolUpdate")

    def subscribe(self, symbols: list[str]) -> None:
        with self._lock:
            new = [s for s in dict.fromkeys(symbols) if s not in self.subscribed]
            room = self.max_symbols - len(self.subscribed)
            if len(new) > room:
                log.warning("symbol cap %d reached: not subscribing %d symbols (they use REST quotes)",
                            self.max_symbols, len(new) - max(room, 0))
                new = new[:max(room, 0)]
            if not new:
                return
            self.subscribed.update(new)
        if self._ws is not None and self.connected.is_set():
            self._ws.subscribe(symbols=new, data_type="SymbolUpdate")
            log.debug("subscribed %d symbols (%d total)", len(new), len(self.subscribed))

    def stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception as e:   # noqa: BLE001 - shutting down anyway
                log.debug("socket close: %s", e)

    def seconds_since_message(self) -> float:
        return float("inf") if not self.last_message else time.monotonic() - self.last_message
