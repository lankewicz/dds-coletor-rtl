"""Orquestra um ciclo de raspagem e persistência exclusivamente local."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .client import RotalogClient
from .ajax_enrichment import AjaxProtocolEnricher
from .changes import detect_changes, log_changes
from .shifts import enrich_shift_state
from .enrichment import enrich_realtime
from .scraper import extract_timeline
from .storage import LocalSnapshotStore


class LocalCollector:
    def __init__(self, client: RotalogClient, store: LocalSnapshotStore, timezone: str) -> None:
        self.client = client
        self.store = store
        self.timezone = ZoneInfo(timezone)
        self.ajax_enricher = AjaxProtocolEnricher(client, timezone, cache_path=store.ajax_cache_path)
        self.ajax_enricher.seed_from_snapshot(store.load_current())

    def run_once(self) -> dict[str, Any]:
        started = datetime.now(self.timezone)
        previous = self.store.load_current()
        html = self.client.fetch_realtime_html()
        parsed = extract_timeline(html)
        enriched = enrich_realtime(parsed, str(self.timezone))
        collected = datetime.now(self.timezone)
        snapshot = {
            "schemaVersion": 1,
            "collectedAt": collected.isoformat(),
            "timezone": str(self.timezone),
            "source": "rotalog-tempo-real",
            "teamCount": len(enriched["teams"]),
            "eventCount": len(enriched["events"]),
            **enriched,
        }
        snapshot["ajaxEnrichment"] = self.ajax_enricher.enrich(snapshot, html)
        enrich_shift_state(previous, snapshot)
        changes = detect_changes(previous, snapshot)
        snapshot["changes"] = changes
        snapshot["changeCount"] = sum(len(items) for items in changes.values())
        log_changes(changes, snapshot["teams"])
        finished = datetime.now(self.timezone)
        paths = self.store.save(
            html,
            snapshot,
            day=collected.date().isoformat(),
            stamp=collected.strftime("%H%M%S-%f"),
            changes=changes,
        )
        return {
            "status": "success",
            "startedAt": started.isoformat(),
            "finishedAt": finished.isoformat(),
            "teamCount": snapshot["teamCount"],
            "eventCount": snapshot["eventCount"],
            "changeCount": snapshot["changeCount"],
            "files": {
                key: (str(value) if isinstance(value, Path) else value)
                for key, value in paths.items()
            },
        }
