"""
quality_model.py — Thompson Sampling quality model for per-entity win-rate tracking.

Entity keys (must be consistent between trade and settle):
  W-bot:  f"{whale_name.lower()}_{direction.lower()}"   e.g. "jaicobioas_up"
  D-bot:  f"{asset.lower()}_{direction.lower()}"        e.g. "eth_down"

Usage:
    qm = QualityModel(positions_path="logs_w/positions.jsonl")
    qm.warmup()                          # seed from history (call once at startup)
    multiplier = qm.get_multiplier(key)  # 0.0 if below floor, else (0.0, 1.0]
    qm.record(key, won=True)             # call after each settlement
"""
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DECAY       = float(os.getenv("QM_DECAY",       "0.95"))
FLOOR       = float(os.getenv("QM_FLOOR",       "0.45"))
PRIOR_ALPHA = float(os.getenv("QM_PRIOR_ALPHA", "1.5"))
PRIOR_BETA  = float(os.getenv("QM_PRIOR_BETA",  "1.0"))
SKIP_WARMUP  = os.getenv("QM_SKIP_WARMUP", "0") == "1"


class _Entity:
    __slots__ = ("alpha", "beta", "n_trades")

    def __init__(self):
        self.alpha    = PRIOR_ALPHA
        self.beta     = PRIOR_BETA
        self.n_trades = 0

    @property
    def quality(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def decay(self):
        self.alpha = max(1.0, self.alpha * DECAY)
        self.beta  = max(1.0, self.beta  * DECAY)

    def update(self, won: bool):
        self.decay()
        if won:
            self.alpha += 1
        else:
            self.beta  += 1
        self.n_trades += 1

    def to_dict(self) -> dict:
        return {
            "alpha":    round(self.alpha,   6),
            "beta":     round(self.beta,    6),
            "n_trades": self.n_trades,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_Entity":
        e = cls()
        e.alpha    = float(d.get("alpha",    PRIOR_ALPHA))
        e.beta     = float(d.get("beta",     PRIOR_BETA))
        e.n_trades = int(d.get("n_trades",   0))
        return e


class QualityModel:
    def __init__(
        self,
        positions_path: str = "",
        persist_path:   str = "quality_model.json",
    ):
        self._entities:      dict[str, _Entity] = {}
        self._positions_path = positions_path
        self._persist_path   = persist_path
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self):
        if not self._persist_path or not os.path.exists(self._persist_path):
            return
        try:
            data = json.loads(Path(self._persist_path).read_text(encoding="utf-8"))
            for key, d in data.get("entities", {}).items():
                self._entities[key] = _Entity.from_dict(d)
            logger.info(
                f"QualityModel: loaded {len(self._entities)} entities "
                f"from {self._persist_path}"
            )
        except Exception as e:
            logger.warning(f"QualityModel: failed to load {self._persist_path}: {e}")

    def _save(self):
        if not self._persist_path:
            return
        try:
            data = {
                "entities":     {k: e.to_dict() for k, e in self._entities.items()},
                "last_updated": int(time.time()),
            }
            tmp = self._persist_path + ".tmp"
            Path(tmp).write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self._persist_path)
        except Exception as e:
            logger.warning(f"QualityModel: failed to save: {e}")

    # ── Warmup ─────────────────────────────────────────────────────────────────

    def warmup(self, entity_key_fn=None):
        """
        Replay all settled trades from positions.jsonl to seed Beta distributions.

        entity_key_fn: optional callable(record: dict) -> str | None
            Default: f"{symbol_prefix.lower()}_{direction.lower()}"
            For W-bot, pass: lambda p: f"{p['source'].split(':')[1].lower()}_{p['direction'].lower()}"
        """
        # Skip warmup if persisted state already loaded from disk
        if SKIP_WARMUP:
            logger.info("QualityModel: QM_SKIP_WARMUP=1, starting cold")
            return
        if self._entities:
            logger.info(
                f"QualityModel: {len(self._entities)} entities loaded from disk, skipping warmup"
            )
            return

        if not self._positions_path or not os.path.exists(self._positions_path):
            logger.info("QualityModel: no positions file, starting cold")
            return

        if entity_key_fn is None:
            def entity_key_fn(p):
                asset     = p.get("symbol", "").split("-")[0].lower()
                direction = p.get("direction", "").lower()
                if not asset or not direction:
                    return None
                return f"{asset}_{direction}"

        trades = []
        with open(self._positions_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                    if p.get("event") != "close":
                        continue
                    if p.get("status") not in ("won", "lost"):
                        continue
                    key = entity_key_fn(p)
                    if key is None:
                        continue
                    trades.append((p.get("ts_close", 0), key, p["status"] == "won"))
                except Exception:
                    continue

        trades.sort(key=lambda x: x[0])   # chronological order
        for _, key, won in trades:
            self._get_or_create(key).update(won)

        below = sum(1 for e in self._entities.values() if e.quality < FLOOR)
        logger.info(
            f"QualityModel warmup: {len(trades)} trades replayed, "
            f"{len(self._entities)} entities, {below} below floor ({FLOOR:.2f})"
        )
        for key, e in sorted(self._entities.items(), key=lambda x: x[1].quality):
            flag = " [BELOW FLOOR]" if e.quality < FLOOR else ""
            logger.info(
                f"  {key}: quality={e.quality:.3f} "
                f"alpha={e.alpha:.2f} beta={e.beta:.2f} "
                f"n={e.n_trades}{flag}"
            )
        self._save()

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_multiplier(self, key: str) -> float:
        """
        Returns 0.0 if entity quality is below FLOOR (caller should skip trade).
        Returns a value in (0.0, 1.0] to multiply Kelly fraction otherwise.
        Unknown entities use the prior quality (~PRIOR_ALPHA/(PRIOR_ALPHA+PRIOR_BETA)).
        """
        e = self._entities.get(key)
        if e is None:
            prior_quality = PRIOR_ALPHA / (PRIOR_ALPHA + PRIOR_BETA)
            if prior_quality < FLOOR:
                return 0.0
            return (prior_quality - FLOOR) / (1.0 - FLOOR)
        q = e.quality
        if q < FLOOR:
            return 0.0
        return (q - FLOOR) / (1.0 - FLOOR)

    def record(self, key: str, won: bool):
        """Update entity after a settled trade. Persists to disk."""
        self._get_or_create(key).update(won)
        self._save()

    def status_summary(self) -> list:
        """Return entity list sorted ascending by quality."""
        return sorted(
            [
                {
                    "key":         k,
                    "quality":     round(e.quality, 4),
                    "n_trades":    e.n_trades,
                    "alpha":       round(e.alpha, 3),
                    "beta":        round(e.beta,  3),
                    "below_floor": e.quality < FLOOR,
                }
                for k, e in self._entities.items()
            ],
            key=lambda x: x["quality"],
        )

    # ── Internal ───────────────────────────────────────────────────────────────

    def _get_or_create(self, key: str) -> _Entity:
        if key not in self._entities:
            self._entities[key] = _Entity()
        return self._entities[key]
