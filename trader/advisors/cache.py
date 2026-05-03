"""Disk-backed JSON cache for advisor recommendations.

Why: each /advisors page load otherwise burns N_advisors × N_symbols LLM calls.
With caching keyed by (advisor_name, symbol, hour), a refresh within an hour is free.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import Recommendation


class RecommendationCache:
    def __init__(self, cache_path: Path | str, ttl_seconds: int = 3600):
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.cache_path)

    def _key(self, advisor: str, symbol: str) -> str:
        return f"{advisor}::{symbol}"

    def get(self, advisor: str, symbol: str) -> Recommendation | None:
        entry = self._data.get(self._key(advisor, symbol))
        if not entry:
            return None
        if time.time() - entry["_cached_at"] > self.ttl_seconds:
            return None
        rec_dict = {k: v for k, v in entry.items() if not k.startswith("_")}
        try:
            return Recommendation(**rec_dict)
        except TypeError:
            return None

    def put(self, rec: Recommendation) -> None:
        entry = rec.to_dict()
        entry["_cached_at"] = time.time()
        self._data[self._key(rec.advisor, rec.symbol)] = entry
        self._save()

    def invalidate(self, advisor: str | None = None, symbol: str | None = None) -> int:
        """Remove cached entries matching the filters. None means 'any'."""
        keys_to_remove = []
        for key, entry in self._data.items():
            if advisor and entry.get("advisor") != advisor:
                continue
            if symbol and entry.get("symbol") != symbol:
                continue
            keys_to_remove.append(key)
        for k in keys_to_remove:
            del self._data[k]
        if keys_to_remove:
            self._save()
        return len(keys_to_remove)

    def age_seconds(self, advisor: str, symbol: str) -> int | None:
        entry = self._data.get(self._key(advisor, symbol))
        if not entry:
            return None
        return int(time.time() - entry["_cached_at"])
