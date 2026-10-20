"""Interface interativa de terminal (Curses TUI) para monitoramento do coletor ROTALOG.

Pode rodar de duas formas:
1. Modo Visualizador Passivo (recomendado quando o systemd já está rodando):
   python tui.py --view --output-dir dados-local

2. Modo Ativo (executa a raspagem diretamente no terminal):
   python tui.py --firebase --interval-seconds 120

Entrada compatível; implementação em coletor.tui.
"""
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from coletor.tui.state import TZ, DAILY_HISTORY_MAX
from coletor.tui.state import (
    TuiState,
)
from coletor.tui.formatting import (
    _format_duration,
    _format_bytes,
    _format_health,
    _format_storage,
    _entry_day,
    _today_entries,
    _format_columns,
)
from coletor.tui.workers import (
    _worker,
    _update_viewer_state_timing,
    _load_runtime_status,
    _viewer_worker,
    _team_state_worker,
    _health_worker,
)
from coletor.tui.terminal import (
    _init_colors,
    _cp,
    _health_color,
    _line,
    _fill,
    _box,
)
from coletor.tui.details import (
    _detail_timestamp,
    _detail_sections,
    _detail_rows,
    _render_details,
)
from coletor.tui.panels import (
    _team_current_rows,
    _render_operational_panels,
)
from coletor.tui.view import (
    _compact_summary,
    _render,
)
from coletor.tui.controller import (
    _curses_main,
)
from coletor.tui.cli import (
    _SafeStream,
    _setup_tui_logging,
    main,
)
from coletor.tui.terminal import C_DEFAULT, C_TITLE, C_OK, C_WARN, C_ERROR, C_STATUS_OK, C_STATUS_WARN, C_STATUS_ERR, C_STATUS_RUN, C_DIM, C_ACCENT, C_HEADER_BAR


if __name__ == "__main__":
    raise SystemExit(main())
