"""
bias_tracker.py — Rolling per-(asset, direction) win-rate circuit breaker.

Pauses a combo when WR < 40% over last 15 trades (min 3 trades).
Resumes when WR >= 50% (hysteresis prevents flapping).
"""
import json
import logging
import os
from collections import deque

logger = logging.getLogger(__name__)

MIN_WINDOW       = 3     # minimum trades before any pause can fire
PAUSE_THRESHOLD  = 0.40  # pause if WR drops below this
RESUME_THRESHOLD = 0.50  # resume if WR recovers to this
WINDOW_SIZE      = 15    # rolling window length


class BiasTracker:

    def __init__(self):
        self._window: dict[tuple[str, str], deque] = {}
        self._paused: set[tuple[str, str]] = set()

    def _get_window(self, asset: str, direction: str) -> deque:
        key = (asset, direction)
        if key not in self._window:
            self._window[key] = deque(maxlen=WINDOW_SIZE)
        return self._window[key]

    def record(self, asset: str, direction: str, won: bool):
        """Record a settled trade result and update pause state."""
        key = (asset, direction)
        w = self._get_window(asset, direction)
        w.append(1 if won else 0)

        if len(w) < MIN_WINDOW:
            return

        wr = sum(w) / len(w)
        if wr < PAUSE_THRESHOLD and key not in self._paused:
            self._paused.add(key)
            logger.info(
                f"BiasTracker: PAUSED {asset} {direction} — "
                f"WR={wr:.1%} over last {len(w)} trades"
            )
        elif wr >= RESUME_THRESHOLD and key in self._paused:
            self._paused.discard(key)
            logger.info(
                f"BiasTracker: RESUMED {asset} {direction} — "
                f"WR={wr:.1%} over last {len(w)} trades"
            )

    def is_paused(self, asset: str, direction: str) -> bool:
        """Return True if this (asset, direction) combo is currently paused."""
        return (asset, direction) in self._paused

    def warmup(self, positions_jsonl_path: str):
        """
        Load last 15 settled trades per (asset, direction) from positions.jsonl.
        Calls record() in chronological order so pause state reflects recent history.
        """
        if not os.path.exists(positions_jsonl_path):
            logger.info(f"BiasTracker warmup: {positions_jsonl_path} not found, starting fresh")
            return

        by_combo: dict[tuple[str, str], list] = {}
        try:
            with open(positions_jsonl_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if rec.get('event') != 'close':
                        continue
                    status = rec.get('status')
                    if status not in ('won', 'lost'):
                        continue

                    asset = rec.get('symbol', '').split('-')[0]
                    direction = rec.get('direction', '')
                    if not asset or not direction:
                        continue

                    ts = float(rec.get('ts_close') or rec.get('ts_open') or 0)
                    key = (asset, direction)
                    if key not in by_combo:
                        by_combo[key] = []
                    by_combo[key].append((ts, status == 'won'))
        except Exception as e:
            logger.warning(f"BiasTracker warmup read failed: {e}")
            return

        total_loaded = 0
        for (asset, direction), trades in by_combo.items():
            trades.sort(key=lambda x: x[0])
            last_n = trades[-WINDOW_SIZE:]
            for _, won in last_n:
                self.record(asset, direction, won)
            total_loaded += len(last_n)

        paused_list = [f"{a} {d}" for a, d in sorted(self._paused)]
        logger.info(
            f"BiasTracker warmup: {total_loaded} trades loaded "
            f"across {len(by_combo)} combos. "
            f"Paused: {paused_list if paused_list else ['none']}"
        )

    def status_summary(self) -> str:
        """Return a human-readable summary of all tracked combos."""
        if not self._window:
            return "  (no data)"
        lines = []
        for (asset, direction), w in sorted(self._window.items()):
            if not w:
                continue
            wr = sum(w) / len(w)
            paused = (asset, direction) in self._paused
            tag = " [PAUSED]" if paused else ""
            lines.append(
                f"  {asset} {direction}: {sum(w)}/{len(w)} ({wr:.1%}){tag}"
            )
        return "\n".join(lines)
