"""Panorama de equipes por situação, separado do histórico de conclusões."""
from datetime import timedelta

from coletor.fleet_control import parse_iso
from . import terminal
from .state import TZ
from .panels import _team_current_rows
from .terminal import C_TITLE, C_WARN, C_OK, C_ACCENT, C_DIM, _box, _line, _cp


GROUPS = (
    ("DESLOCAMENTO", C_TITLE), ("EXECUCAO", C_WARN), ("INTERVALO", C_ACCENT),
    ("SEM EXECUCAO", C_WARN), ("FECHADO", C_DIM), ("CONCLUSOES RECENTES", C_OK),
)


def _overview_groups(snapshots, events, now):
    groups = [[] for _ in GROUPS]
    labels = {"Desloc.": 0, "Execucao": 1, "Intervalo": 2, "Aberto": 3, "Fechado": 4}
    for row in _team_current_rows(snapshots, now):
        index = labels.get(row[1])
        if index is not None:
            groups[index].append(row)
    conclusions = {}
    for event in events:
        if not isinstance(event, dict) or event.get("status") != "CONCLUSAO":
            continue
        stamp = parse_iso(event.get("occurredAt"))
        key = event.get("id")
        team = event.get("teamKey")
        if key and team and stamp and now - timedelta(hours=24) <= stamp <= now:
            conclusions[key] = (stamp, (team, "Conclusao", stamp.astimezone(TZ).strftime("%H:%M:%S"), "", C_OK))
    groups[5] = [row for _, row in sorted(conclusions.values(), key=lambda item: (item[0], item[1][0]), reverse=True)]
    return groups


def _render_overview(screen, data, top, height, width, now):
    groups = _overview_groups(data["team_snapshots"], data["team_events"], now)
    opened = sum((doc.get("jornada") or {}).get("turno", {}).get("status") == "ABERTO"
                 for doc in data["team_snapshots"].values())
    stamp = parse_iso(data["teams_collected_at"]) if data["teams_collected_at"] else None
    hour = stamp.astimezone(TZ).strftime("%H:%M:%S") if stamp else "—"
    stale = stamp and now - stamp > timedelta(seconds=data["teams_stale_seconds"])
    suffix = " | INDICE INDISPONIVEL" if data["teams_error"] else " | DESATUALIZADO" if stale else ""
    unclassified = len(data["team_snapshots"]) - sum(len(group) for group in groups[:5])
    if unclassified:
        suffix += f" | {unclassified} sem classificacao"
    _line(screen, top, 1, f"SITUACAO DAS EQUIPES | {len(data['team_snapshots'])} equipes | {opened} turnos abertos", width - 3, _cp(C_TITLE, terminal.curses.A_BOLD))
    _line(screen, top + 1, 1, f"Coleta {hour}{suffix} | Conclusoes: ultimas 24h (eventos, nao equipes exclusivas)", width - 3, _cp(C_WARN if suffix else C_DIM))
    columns = 3 if width >= 144 else 2 if width >= 100 else 1
    grid_rows = (len(GROUPS) + columns - 1) // columns
    single = columns == 1 or (height - 3) // grid_rows < 6
    selected = data["overview_group"] % len(GROUPS)
    indexes = [selected] if single else list(range(len(GROUPS)))
    if single:
        columns, grid_rows = 1, 1
    cell_height = (height - 3) // grid_rows
    cell_width = (width - 1) // columns
    offsets = list(data["overview_scroll"])
    for index in indexes:
        position = 0 if single else index
        row, col = divmod(position, columns)
        x, y = col * cell_width, top + 3 + row * cell_height
        w = cell_width - 1
        h = cell_height
        name, color = GROUPS[index]
        entries = groups[index]
        focused = index == selected
        marker = "> " if focused else ""
        _box(screen, y, x, h, w, f"{marker}{name} ({len(entries)})", C_TITLE if focused else C_DIM)
        heading = f"{'Equipe':<8} {'Abertura':<8} Fila C/E" if index == 3 else f"{'Equipe':<8} Hora"
        _line(screen, y + 1, x + 2, heading, w - 4, _cp(color, terminal.curses.A_BOLD))
        visible = max(1, h - 4)
        offset = min(max(0, offsets[index]), max(0, len(entries) - visible))
        offsets[index] = offset
        for line, entry in enumerate(entries[offset:offset + visible], y + 2):
            team, _, event_hour, pending, _ = entry
            text = f"{team:<8} {event_hour}" + (f" {pending}" if index == 3 else "")
            _line(screen, line, x + 2, text, w - 4, _cp(color))
        if not entries:
            message = "Eventos indisponiveis" if index == 5 and data["team_events_error"] else "Nenhum registro"
            _line(screen, y + 2, x + 2, message, w - 4, _cp(C_DIM))
        count = f"{offset + 1 if entries else 0}-{min(offset + visible, len(entries))}/{len(entries)}"
        note = " | Arquivo indisponivel" if index == 5 and data["team_events_error"] else " | Tab: proximo" if focused else ""
        _line(screen, y + h - 2, x + 2, count + note, w - 4, _cp(C_WARN if index == 5 and data["team_events_error"] else C_DIM))
    return offsets
