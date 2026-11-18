"""One bounded scan at a time; market-candle-aligned auto-scanner schedule and no live order API."""
import threading
import time
from dataclasses import dataclass
from .groq_rotation import combine
from .strategy import evaluate

SCAN_INTERVALS = (60, 300, 900)
ALLOWED_SCAN_INTERVALS = (30, 45, 60, 90, 300, 900)
SCAN_INTERVAL_LABELS = {60: "1m (60 detik)", 300: "5m (300 detik)", 900: "15m (900 detik)",
                        30: "30 detik", 45: "45 detik", 90: "90 detik"}

# Epoch threshold (2001-09-09 UTC) distinguishing real UTC market timestamps from small synthetic monotonic test values.
_EPOCH_SECONDS_THRESHOLD = 1_000_000_000
_EPOCH_MS_THRESHOLD = 1_000_000_000_000


def current_market_candle_boundary_ms(now_ms: int, interval_seconds: int) -> int:
    """Return the exact UTC market candle boundary timestamp (in ms) for `interval_seconds` (e.g. M1=60s, M5=300s, M15=900s)."""
    interval_ms = max(1_000, int(interval_seconds) * 1000)
    stamp_ms = int(now_ms)
    return (stamp_ms // interval_ms) * interval_ms


def next_market_aligned_scan_ms(now_ms: int, interval_seconds: int, grace_ms: int = 0) -> int:
    """Return the exact next UTC market candle close boundary (in ms) aligned to exchange market time.

    Example:
    - For M5 (`interval_seconds=300`), if `now_ms` is `10:02:37.400 UTC`, returns `10:05:00.000 UTC` (+ `grace_ms`).
    - For M1 (`interval_seconds=60`), if `now_ms` is `10:02:37.400 UTC`, returns `10:03:00.000 UTC` (+ `grace_ms`).
    - For M15 (`interval_seconds=900`), if `now_ms` is `10:02:37.400 UTC`, returns `10:15:00.000 UTC` (+ `grace_ms`).
    """
    interval_ms = max(1_000, int(interval_seconds) * 1000)
    stamp_ms = int(now_ms)
    next_boundary = ((stamp_ms // interval_ms) + 1) * interval_ms
    return next_boundary + max(0, int(grace_ms))


def seconds_until_next_market_boundary(now_ms: int, interval_seconds: int, grace_ms: int = 0) -> float:
    """Return the remaining seconds until the next UTC market candle close boundary."""
    target_ms = next_market_aligned_scan_ms(now_ms, interval_seconds, grace_ms=grace_ms)
    return max(0.001, (target_ms - int(now_ms)) / 1000.0)


@dataclass
class ScanSchedule:
    enabled: bool = False
    context: str = ""
    next_due: float = 0.0
    running: bool = False
    market_aligned: bool = True
    current_market_boundary_ms: int = 0
    next_market_boundary_ms: int = 0

    def _compute_next_due(self, now: float, interval: int, market_now_ms: int | None = None) -> float:
        eff_market_ms = market_now_ms
        if eff_market_ms is None and self.market_aligned and float(now) >= _EPOCH_SECONDS_THRESHOLD:
            eff_market_ms = int(float(now) * 1000)
        if self.market_aligned and eff_market_ms is not None and int(eff_market_ms) >= _EPOCH_MS_THRESHOLD:
            self.current_market_boundary_ms = current_market_candle_boundary_ms(int(eff_market_ms), interval)
            self.next_market_boundary_ms = next_market_aligned_scan_ms(int(eff_market_ms), interval)
            wait_s = seconds_until_next_market_boundary(int(eff_market_ms), interval)
            return float(now) + wait_s
        return float(now) + float(interval)

    def claim(self, now, enabled, interval, context, manual=False, market_now_ms=None):
        if interval not in ALLOWED_SCAN_INTERVALS:
            raise ValueError("Interval scan harus 1m (60s), 5m (300s), atau 15m (900s)")
        if enabled and (not self.enabled or context != self.context):
            self.next_due = now
        self.enabled, self.context = enabled, context
        if self.running or not (manual or (enabled and now >= self.next_due - .25)):
            return False
        self.running = True
        # Reserve before any I/O. A UI rerun cannot start the same scan twice.
        # When market_now_ms is available, align next_due to the exact market candle close boundary.
        self.next_due = self._compute_next_due(now, interval, market_now_ms=market_now_ms)
        return True

    def finish(self, now, interval, market_now_ms=None):
        self.running = False
        if market_now_ms is not None and self.market_aligned and int(market_now_ms) >= _EPOCH_MS_THRESHOLD:
            self.next_due = self._compute_next_due(now, interval, market_now_ms=market_now_ms)
        elif now >= self.next_due:
            # If a slow API exceeded the interval, skip missed ticks, never catch up.
            self.next_due = self._compute_next_due(now, interval, market_now_ms=market_now_ms)


class ScanBusy(RuntimeError):
    pass


class ScanEngine:
    def __init__(self, clock=time.monotonic):
        self.lock = threading.Lock()
        self.clock = clock

    def run(self, market, symbol, mode, account, pool, model):
        if not self.lock.acquire(blocking=False):
            raise ScanBusy("Scan lain sedang berjalan. Menunggu jadwal berikutnya.")
        try:
            snap = market.load(symbol, mode)
            acquired = self.clock()
            result = evaluate(snap, account)
            # Every acquired scan receives AI review, including WAIT/NO_TRADE.
            # combine() prevents AI from overriding a failed mandatory gate.
            ai = pool.review(result, model)
            if combine(result, ai) == "VALID":
                reviewed_id = result.plan["id"]
                snap = market.load(symbol, mode)
                acquired = self.clock()
                result = evaluate(snap, account)
                if not result.plan or result.plan["id"] != reviewed_id:
                    ai = {"decision": "WAIT", "reasons": [
                        "Setup atau data berubah setelah respons AI. Analisis ulang diperlukan."],
                        "cautions": [], "ok": False}
            return {"snapshot": snap, "result": result, "ai": ai,
                    "status": combine(result, ai), "monotonic": acquired}
        finally:
            self.lock.release()
