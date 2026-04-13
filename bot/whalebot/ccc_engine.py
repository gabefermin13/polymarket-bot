"""
ccc_engine.py — Collective Confidence Consensus (CCC) score computation

Maintains a rolling activity window of whale buy events per market (condition_id).
On each new event, computes a confidence-weighted majority score:

    active_whales   = all whales that bought Up OR Down in this market within the window
    agreeing_weight = sum(CCC_conf) for whales that bought the trigger direction
    total_weight    = sum(CCC_conf) for all active whales
    CCC_score       = agreeing_weight / total_weight

Special case: if only one whale is active, CCC_score = whale's own CCC confidence
(lone high-CCC whale is a valid signal; the CCC score protects against low WR wallets).
"""
import time
from dataclasses import dataclass, field


@dataclass
class WhaleEvent:
    addr:      str
    name:      str
    direction: str    # "Up" or "Down"
    ccc_conf:  float
    ts:        float  # epoch seconds


@dataclass
class CCCResult:
    ccc_score:       float
    n_active:        int
    agreeing_weight: float
    total_weight:    float
    whale_details:   list = field(default_factory=list)
    # [{"address": ..., "name": ..., "direction": ..., "ccc_conf": ...}]


class CCCEngine:
    """
    Tracks whale activity per condition_id and computes CCC scores.

    Thread-safety: asyncio single-threaded — no locking needed.
    """

    def __init__(self, window_secs: float = 120.0, min_conf: float = 0.60):
        self._window_secs = window_secs
        self._min_conf    = min_conf
        # condition_id → list[WhaleEvent]
        self._windows: dict[str, list[WhaleEvent]] = {}

    def record_event(
        self,
        condition_id: str,
        addr:         str,
        name:         str,
        direction:    str,
        ccc_conf:     float,
    ) -> None:
        """
        Record a whale buy event. Low-confidence wallets are dropped silently.
        If the same whale re-signals the same condition_id, the old event is
        replaced (prevents double-weighting from duplicate tx fills).
        """
        if ccc_conf < self._min_conf:
            return

        now = time.time()
        if condition_id not in self._windows:
            self._windows[condition_id] = []

        # Prune expired events
        self._windows[condition_id] = [
            e for e in self._windows[condition_id]
            if now - e.ts < self._window_secs
        ]

        # Remove any prior event from this whale for this condition_id
        self._windows[condition_id] = [
            e for e in self._windows[condition_id] if e.addr != addr
        ]

        self._windows[condition_id].append(
            WhaleEvent(addr=addr, name=name, direction=direction,
                       ccc_conf=ccc_conf, ts=now)
        )

    def compute(self, condition_id: str, direction: str) -> CCCResult:
        """
        Compute CCC score for condition_id + direction.

        direction: "Up" or "Down" (Polymarket convention).
        """
        now    = time.time()
        events = self._windows.get(condition_id, [])

        # Prune stale events
        events = [e for e in events if now - e.ts < self._window_secs]
        self._windows[condition_id] = events

        if not events:
            return CCCResult(0.0, 0, 0.0, 0.0, [])

        total_weight    = sum(e.ccc_conf for e in events)
        agreeing_weight = sum(e.ccc_conf for e in events if e.direction == direction)

        if total_weight == 0:
            return CCCResult(0.0, 0, 0.0, 0.0, [])

        # Lone whale: score = whale's own confidence (not diluted to 1.0)
        if len(events) == 1:
            score = events[0].ccc_conf
        else:
            score = agreeing_weight / total_weight

        return CCCResult(
            ccc_score       = round(score, 4),
            n_active        = len(events),
            agreeing_weight = round(agreeing_weight, 4),
            total_weight    = round(total_weight, 4),
            whale_details   = [
                {
                    "address":   e.addr,
                    "name":      e.name,
                    "direction": e.direction,
                    "ccc_conf":  e.ccc_conf,
                }
                for e in events
            ],
        )

    def cleanup_old_markets(self, max_age_secs: float = 7200.0) -> None:
        """Evict condition_ids with no events newer than max_age_secs."""
        now   = time.time()
        stale = [
            cid for cid, evts in self._windows.items()
            if not any(now - e.ts < max_age_secs for e in evts)
        ]
        for cid in stale:
            del self._windows[cid]
