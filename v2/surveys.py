"""
surveys.py — Shared survey pool for surveyor → miner communication.
Protected by asyncio.Lock(); no threading locks required.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class SurveyPool:
    """Thread-safe (async-safe) pool of active surveys shared between
    SurveyorRole (writes) and MinerRole (reads)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pool: list[dict] = []

    async def add(self, surveys: list[dict]) -> None:
        """Add new surveys and prune expired ones."""
        async with self._lock:
            self._pool.extend(surveys)
            self._prune()

    async def get_best(self, target_good: str | None = None) -> dict | None:
        """Return the best unexpired survey for target_good (or any survey if None).
        Surveys are NOT consumed — they remain usable until expiration.
        """
        async with self._lock:
            self._prune()
            if not self._pool:
                return None
            if target_good:
                focused = [
                    s for s in self._pool
                    if any(d["symbol"] == target_good for d in s.get("deposits", []))
                ]
                if focused:
                    return max(
                        focused,
                        key=lambda s: sum(1 for d in s["deposits"] if d["symbol"] == target_good),
                    )
            # Fall back to largest survey by total deposits
            return max(self._pool, key=lambda s: len(s.get("deposits", [])))

    async def size(self) -> int:
        async with self._lock:
            self._prune()
            return len(self._pool)

    async def prune(self) -> None:
        async with self._lock:
            self._prune()

    def _prune(self) -> None:
        """Remove expired surveys. Must be called while holding _lock."""
        now = datetime.now(timezone.utc)
        before = len(self._pool)
        self._pool = [s for s in self._pool if not _is_expired(s, now)]
        removed = before - len(self._pool)
        if removed:
            log.debug("Pruned %d expired surveys", removed)


def _is_expired(survey: dict, now: datetime) -> bool:
    exp = survey.get("expiration", "")
    if not exp:
        return True
    try:
        dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        return dt <= now
    except Exception:
        return True
