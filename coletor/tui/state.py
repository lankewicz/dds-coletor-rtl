"""Estado compartilhado da interface de terminal."""
from __future__ import annotations

import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from main import get_adaptive_interval_seconds

TZ = ZoneInfo("America/Sao_Paulo")
DAILY_HISTORY_MAX = 5_000


class TuiState:
    def __init__(
        self,
        interval_seconds: int | None = None,
        peak_interval: int = 180,
        offpeak_interval: int = 600,
        viewer_mode: bool = False,
    ):
        self.interval_seconds = interval_seconds
        self.peak_interval = peak_interval
        self.offpeak_interval = offpeak_interval
        self.viewer_mode = viewer_mode
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.run_now = threading.Event()
        self.is_running = False
        self.is_stale = False
        self.historico_running = False
        self.last_result: dict | None = None
        self.history: list[dict] = []
        self.next_run: datetime | None = None
        self.status_message = "Inicializando..."
        self.runtime_status: dict = {}
        self.device_health: dict = {}
        self.device_health_updated_at: datetime | None = None
        self.storage: dict = {}
        self.show_details = False
        self.details_scroll = 0
        self.team_snapshots: dict = {}
        self.teams_collected_at = None
        self.teams_error = "Aguardando indice local"
        self.active_panel = "events"
        self.teams_expanded = False
        self.events_scroll = 0
        self.teams_scroll = 0
        self.team_events: list[dict] = []
        self.team_events_error = "Aguardando arquivo de eventos"
        self.overview_group = 0
        self.overview_scroll = [0] * 6

    def get_interval(self, now: datetime | None = None) -> int:
        if self.interval_seconds is not None:
            return self.interval_seconds
        return get_adaptive_interval_seconds(
            now=now,
            peak_interval=self.peak_interval,
            offpeak_interval=self.offpeak_interval,
        )
