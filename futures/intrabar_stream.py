"""Public Futures WebSocket + REST Fallback Intrabar Stream Manager (CoinAgentX v6.0.5 Stage 1).

Read-only public market data ONLY (Binance USD-M Futures Primary -> Bybit USDT Linear Secondary).
NEVER places orders, NEVER mixes Spot Reference into Futures execution streams, and strictly
separates running intrabar candles (`is_closed=False`) from closed historical candles (`is_closed=True`).
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import OrderedDict
from typing import Callable, Iterable, Optional

import pandas as pd

from .models import INTERVAL_MS, MONITOR_TIMEFRAMES, IntrabarCandle, MonitorSnapshot
from .security import GLOBAL_MONITOR

BINANCE_FUTURES_WS_URL = "wss://fstream.binance.com/stream"
BYBIT_LINEAR_WS_URL = "wss://stream.bybit.com/v5/public/linear"
MAX_STREAM_SUBSCRIPTIONS = 50


def validate_ohlcv_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate OHLCV integrity, drop invalid/corrupted rows, deduplicate by open_time, and sort ascending."""
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
    df = frame.copy()
    req = ("open_time", "open", "high", "low", "close", "volume", "close_time")
    for col in req:
        if col not in df.columns:
            raise ValueError(f"Kolom OHLCV wajib tidak ditemukan: {col}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=list(req))
    valid_mask = (
        (df["open"] > 0)
        & (df["high"] > 0)
        & (df["low"] > 0)
        & (df["close"] > 0)
        & (df["volume"] >= 0)
        & (df["high"] >= df[["open", "close"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & (df["close_time"] > df["open_time"])
    )
    df = df.loc[valid_mask].copy()
    if df.empty:
        return df.reset_index(drop=True)
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["close_time"].astype("int64")
    df = df.sort_values("open_time").drop_duplicates(subset=["open_time"], keep="last").reset_index(drop=True)
    return df


def parse_binance_kline_message(payload: dict, receive_ms: Optional[int] = None) -> Optional[IntrabarCandle]:
    """Parse a Binance USD-M Futures combined-stream kline event into an IntrabarCandle."""
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("e") != "kline":
        return None
    k = data.get("k")
    if not isinstance(k, dict):
        return None
    tf = str(k.get("i") or "")
    if tf not in INTERVAL_MS:
        return None
    rx_ms = int(receive_ms if receive_ms is not None else time.time() * 1000)
    ex_ms = int(data.get("E") or k.get("T") or rx_ms)
    candle = IntrabarCandle(
        symbol=str(k.get("s") or data.get("s") or "").upper(),
        timeframe=tf,
        open_time=int(k["t"]),
        close_time=int(k["T"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        is_closed=bool(k.get("x", False)),
        provider="Binance Futures",
        instrument_kind="USD_M_PERPETUAL",
        exchange_timestamp_ms=ex_ms,
        local_receive_ms=rx_ms,
        data_freshness_ms=max(0, rx_ms - ex_ms),
    )
    return candle if candle.is_valid_ohlc() else None


def parse_bybit_kline_message(payload: dict, receive_ms: Optional[int] = None) -> list[IntrabarCandle]:
    """Parse a Bybit v5 public linear kline message into IntrabarCandle list."""
    if not isinstance(payload, dict):
        return []
    topic = str(payload.get("topic") or "")
    if not topic.startswith("kline."):
        return []
    parts = topic.split(".")
    if len(parts) < 3:
        return []
    tf_map = {"1": "1m", "5": "5m", "15": "15m", "60": "1h", "240": "4h", "D": "1d"}
    tf = tf_map.get(parts[1])
    symbol = parts[2].upper()
    if not tf:
        return []
    rx_ms = int(receive_ms if receive_ms is not None else time.time() * 1000)
    ex_ms = int(payload.get("ts") or rx_ms)
    out = []
    for row in payload.get("data") or []:
        try:
            candle = IntrabarCandle(
                symbol=symbol,
                timeframe=tf,
                open_time=int(row["start"]),
                close_time=int(row["end"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                is_closed=bool(row.get("confirm", False)),
                provider="Bybit USDT Perpetual",
                instrument_kind="BYBIT_LINEAR_PERPETUAL",
                exchange_timestamp_ms=int(row.get("timestamp") or ex_ms),
                local_receive_ms=rx_ms,
                data_freshness_ms=max(0, rx_ms - int(row.get("timestamp") or ex_ms)),
            )
            if candle.is_valid_ohlc():
                out.append(candle)
        except (KeyError, ValueError, TypeError):
            continue
    return out


class IntrabarStreamManager:
    """Manages public M1/M5/M15 WebSocket streams with exponential backoff reconnect,
    stale feed detection, bounded subscriptions (<=50), provider failover, and REST fallback.
    """

    def __init__(
        self,
        market_client,
        *,
        websocket_enabled: bool = True,
        rest_fallback_enabled: bool = True,
        max_subscriptions: int = MAX_STREAM_SUBSCRIPTIONS,
        stale_threshold_seconds: float = 30.0,
        monitor_interval_seconds: float = 10.0,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        max_rest_requests_per_cycle: int = 120,
        ws_connector: Optional[Callable] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.market = market_client
        self.websocket_enabled = bool(websocket_enabled)
        self.rest_fallback_enabled = bool(rest_fallback_enabled)
        self.max_subscriptions = max(1, min(MAX_STREAM_SUBSCRIPTIONS, int(max_subscriptions)))
        self.stale_threshold_ms = int(max(5.0, float(stale_threshold_seconds)) * 1000)
        self.monitor_interval_seconds = max(5.0, float(monitor_interval_seconds))
        self.backoff_base_seconds = max(0.25, float(backoff_base_seconds))
        self.backoff_max_seconds = max(2.0, float(backoff_max_seconds))
        self.max_rest_requests_per_cycle = max(10, int(max_rest_requests_per_cycle))
        self._ws_connector = ws_connector
        self.clock = clock

        self._lock = threading.RLock()
        self._subscriptions: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        # Separate buffers: live intrabar candles NEVER overwrite closed history cache
        self._live_candles: dict[tuple[str, str], IntrabarCandle] = {}
        self._closed_buffer: dict[tuple[str, str], OrderedDict[int, IntrabarCandle]] = {}
        self._latest_snapshots: dict[str, MonitorSnapshot] = {}

        # Connection / health state
        self.ws_state = "DISCONNECTED"
        self.active_provider = getattr(market_client, "market_label", "Binance Futures")
        self.active_instrument_kind = getattr(market_client, "instrument_kind", "USD_M_PERPETUAL")
        self.failover_reason: Optional[str] = None
        self.last_message_ms: int = 0
        self.last_heartbeat_ms: int = 0
        self.reconnect_attempts: int = 0
        self.reconnect_count: int = 0
        self.next_reconnect_at: float = 0.0
        self.rest_fallback_active: bool = False
        self.rest_requests_last_cycle: int = 0
        self.last_error: str = ""
        self._stop_event = threading.Event()

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def subscribe(self, symbols: Iterable[str], timeframes: tuple[str, ...] = MONITOR_TIMEFRAMES) -> list[str]:
        """Dynamically subscribe symbols (bounded to max_subscriptions to prevent memory leaks)."""
        tfs = tuple(tf for tf in timeframes if tf in INTERVAL_MS) or MONITOR_TIMEFRAMES
        with self._lock:
            for raw in symbols:
                sym = str(raw or "").strip().upper()
                if not sym:
                    continue
                self._subscriptions[sym] = tfs
                self._subscriptions.move_to_end(sym)
                while len(self._subscriptions) > self.max_subscriptions:
                    evicted, _ = self._subscriptions.popitem(last=False)
                    self._evict_symbol_state(evicted)
            return list(self._subscriptions.keys())

    def unsubscribe(self, symbols: Iterable[str]) -> list[str]:
        """Unsubscribe symbols and purge their live buffers to prevent memory leaks."""
        with self._lock:
            for raw in symbols:
                sym = str(raw or "").strip().upper()
                self._subscriptions.pop(sym, None)
                self._evict_symbol_state(sym)
            return list(self._subscriptions.keys())

    def _evict_symbol_state(self, symbol: str) -> None:
        self._latest_snapshots.pop(symbol, None)
        for key in [k for k in self._live_candles if k[0] == symbol]:
            self._live_candles.pop(key, None)
        for key in [k for k in self._closed_buffer if k[0] == symbol]:
            self._closed_buffer.pop(key, None)

    def subscribed_symbols(self) -> list[str]:
        with self._lock:
            return list(self._subscriptions.keys())

    def compute_backoff_delay(self, attempt: int) -> float:
        exp = min(10, max(0, int(attempt)))
        return min(self.backoff_max_seconds, self.backoff_base_seconds * (2 ** exp))

    def ingest_candle(self, candle: IntrabarCandle) -> bool:
        """Ingest a validated IntrabarCandle from WebSocket or REST without mixing providers or polluting closed cache."""
        if candle is None or not candle.is_valid_ohlc():
            return False
        with self._lock:
            # Provider consistency check: if provider switched, clear stale buffers for symbol
            existing = self._live_candles.get((candle.symbol, candle.timeframe))
            if existing is not None and existing.provider != candle.provider:
                self._evict_symbol_state(candle.symbol)
            rx_ms = self.now_ms()
            candle.local_receive_ms = rx_ms
            candle.data_freshness_ms = max(0, rx_ms - int(candle.exchange_timestamp_ms or rx_ms))
            candle.is_stale = candle.data_freshness_ms > self.stale_threshold_ms
            self.last_message_ms = rx_ms
            self.active_provider = candle.provider
            self.active_instrument_kind = candle.instrument_kind

            key = (candle.symbol, candle.timeframe)
            if candle.is_closed:
                buf = self._closed_buffer.setdefault(key, OrderedDict())
                # Immutable once closed: never overwrite an already-finalized closed bar
                if candle.open_time not in buf:
                    buf[candle.open_time] = candle
                    while len(buf) > 300:
                        buf.popitem(last=False)
                # Clear live bar if it matches the newly closed open_time
                live = self._live_candles.get(key)
                if live is not None and live.open_time == candle.open_time:
                    self._live_candles.pop(key, None)
            else:
                self._live_candles[key] = candle
            return True

    def ingest_raw_ws_message(self, raw_message: str | dict, provider_hint: Optional[str] = None) -> int:
        """Parse and ingest a raw JSON WebSocket message from Binance or Bybit."""
        try:
            payload = json.loads(raw_message) if isinstance(raw_message, str) else raw_message
        except (ValueError, TypeError):
            return 0
        if not isinstance(payload, dict):
            return 0
        rx_ms = self.now_ms()
        count = 0
        if provider_hint == "Bybit" or str(payload.get("topic", "")).startswith("kline."):
            for c in parse_bybit_kline_message(payload, receive_ms=rx_ms):
                if self.ingest_candle(c):
                    count += 1
        else:
            c = parse_binance_kline_message(payload, receive_ms=rx_ms)
            if c is not None and self.ingest_candle(c):
                count += 1
        return count

    def connect_websocket(self) -> bool:
        """Attempt WebSocket connection with exponential backoff; sets REST fallback cleanly if unavailable."""
        with self._lock:
            if not self.websocket_enabled:
                self.ws_state = "DISABLED_REST_MODE"
                self.rest_fallback_active = self.rest_fallback_enabled
                return False
            now_s = self.clock()
            if now_s < self.next_reconnect_at:
                return False
            if self._ws_connector is None:
                # Check if optional websocket library exists; never crash if offline/missing
                try:
                    import websockets  # noqa: F401
                except Exception:
                    self.ws_state = "REST_FALLBACK"
                    self.rest_fallback_active = self.rest_fallback_enabled
                    self.last_error = "WebSocket client package not installed; using budgeted REST fallback."
                    return False
                # Real network socket is only opened when explicitly started in live worker daemon
                self.ws_state = "REST_FALLBACK"
                self.rest_fallback_active = self.rest_fallback_enabled
                return False
            try:
                self._ws_connector(self.active_provider, list(self._subscriptions.keys()))
                self.ws_state = "CONNECTED"
                self.reconnect_attempts = 0
                self.last_heartbeat_ms = self.now_ms()
                self.rest_fallback_active = False
                self.last_error = ""
                return True
            except Exception as exc:
                self.ws_state = "RECONNECTING"
                self.reconnect_attempts += 1
                self.reconnect_count += 1
                delay = self.compute_backoff_delay(self.reconnect_attempts - 1)
                self.next_reconnect_at = now_s + delay
                self.last_error = str(exc)[:180]
                if self.rest_fallback_enabled:
                    self.rest_fallback_active = True
                return False

    def handle_disconnect(self, reason: str = "connection_lost") -> float:
        """Handle WebSocket disconnect: trigger exponential backoff, rebuild state flag, and enable REST fallback."""
        with self._lock:
            self.ws_state = "RECONNECTING"
            self.reconnect_attempts += 1
            self.reconnect_count += 1
            delay = self.compute_backoff_delay(self.reconnect_attempts - 1)
            self.next_reconnect_at = self.clock() + delay
            self.last_error = str(reason)[:180]
            if self.rest_fallback_enabled:
                self.rest_fallback_active = True
            return delay

    def is_feed_stale(self, now_ms: Optional[int] = None) -> bool:
        now = int(now_ms if now_ms is not None else self.now_ms())
        if self.last_message_ms <= 0:
            return True
        return (now - self.last_message_ms) > self.stale_threshold_ms

    def poll_rest_cycle(
        self,
        symbols: Optional[Iterable[str]] = None,
        *,
        intervals: tuple[str, ...] = MONITOR_TIMEFRAMES,
    ) -> dict[str, MonitorSnapshot]:
        """Budgeted REST fallback cycle for <=50 shortlisted symbols (never polls entire 3000+ universe)."""
        if not self.rest_fallback_enabled:
            return {}
        target_symbols = list(symbols) if symbols is not None else self.subscribed_symbols()
        target_symbols = target_symbols[: self.max_subscriptions]
        results: dict[str, MonitorSnapshot] = {}
        req_count = 0
        for sym in target_symbols:
            if req_count >= self.max_rest_requests_per_cycle:
                break
            if not hasattr(self.market, "monitor_snapshot"):
                continue
            try:
                snap = self.market.monitor_snapshot(
                    sym,
                    intervals=intervals,
                    stale_threshold_ms=self.stale_threshold_ms,
                )
                req_count += len(intervals) + 1
                if snap is not None:
                    with self._lock:
                        self._latest_snapshots[sym] = snap
                        self.last_message_ms = int(snap.local_receive_ms or self.now_ms())
                        self.active_provider = snap.provider
                        self.active_instrument_kind = snap.instrument_kind
                        self.failover_reason = snap.failover_reason
                        for tf, c in (snap.current_candles or {}).items():
                            if isinstance(c, IntrabarCandle):
                                self.ingest_candle(c)
                    results[sym] = snap
            except Exception as exc:
                self.last_error = str(exc)[:180]
                if getattr(exc, 'access_blocked', False):
                    break
        with self._lock:
            self.rest_requests_last_cycle = req_count
            self.rest_fallback_active = True
            if self.ws_state != "CONNECTED":
                self.ws_state = "REST_FALLBACK"
        return results

    def get_live_candle(self, symbol: str, timeframe: str) -> Optional[IntrabarCandle]:
        with self._lock:
            c = self._live_candles.get((str(symbol).upper(), str(timeframe)))
            if c is None:
                return None
            c.data_freshness_ms = max(0, self.now_ms() - int(c.exchange_timestamp_ms or c.local_receive_ms))
            c.is_stale = c.data_freshness_ms > self.stale_threshold_ms
            return c

    def get_snapshot(self, symbol: str) -> Optional[MonitorSnapshot]:
        with self._lock:
            return self._latest_snapshots.get(str(symbol).upper())

    def health_status(self) -> dict:
        with self._lock:
            now = self.now_ms()
            stale_syms = []
            avail = {tf: 0 for tf in MONITOR_TIMEFRAMES}
            for (sym, tf), c in self._live_candles.items():
                age = max(0, now - int(c.exchange_timestamp_ms or c.local_receive_ms))
                if age > self.stale_threshold_ms:
                    if sym not in stale_syms:
                        stale_syms.append(sym)
                elif tf in avail:
                    avail[tf] += 1
            return {
                "websocket_state": self.ws_state,
                "rest_fallback_active": self.rest_fallback_active,
                "active_provider": self.active_provider,
                "instrument_kind": self.active_instrument_kind,
                "failover_reason": self.failover_reason,
                "last_message_ms": self.last_message_ms,
                "feed_stale": self.is_feed_stale(now),
                "stale_symbols": stale_syms,
                "active_subscriptions": len(self._subscriptions),
                "max_subscriptions": self.max_subscriptions,
                "reconnect_count": self.reconnect_count,
                "rest_requests_last_cycle": self.rest_requests_last_cycle,
                "m1_m5_m15_availability": avail,
                "last_error": self.last_error,
            }
