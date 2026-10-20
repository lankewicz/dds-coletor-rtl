"""Painéis de eventos e situação atual das equipes."""
from __future__ import annotations

from datetime import datetime, timedelta
from coletor.fleet_control import parse_iso
from . import terminal
from .state import TZ
from .formatting import _format_columns
from .terminal import C_DEFAULT, C_TITLE, C_OK, C_WARN, C_ERROR, C_STATUS_OK, C_STATUS_WARN, C_STATUS_ERR, C_STATUS_RUN, C_DIM, C_ACCENT, C_HEADER_BAR
from .terminal import _box, _cp, _fill, _health_color, _init_colors, _line


def _team_current_rows(snapshots: dict, now: datetime) -> list[tuple]:
    rows = []
    for key, doc in sorted(snapshots.items()):
        journey = doc.get("jornada") or {}
        shift = journey.get("turno") or {}
        service = (doc.get("ordensServico") or {}).get("atual") or {}
        value = None
        if journey.get("emIntervalo"):
            label, color = "Intervalo", C_ACCENT
            intervals = [i for i in journey.get("intervalos") or [] if not i.get("fim")]
            value = (journey.get("intervaloAtual") or {}).get("inicio") or (intervals[-1].get("inicio") if intervals else None)
        elif service.get("statusAtual") in ("EXECUCAO", "DESLOCAMENTO"):
            execution = service["statusAtual"] == "EXECUCAO"
            label, color = ("Execucao", C_WARN) if execution else ("Desloc.", C_TITLE)
            field = "inicioExecucao" if execution else "inicioDeslocamento"
            if field not in (service.get("camposEstimados") or []):
                value = service.get(field)
        else:
            label, color = {"ABERTO": ("Aberto", C_OK), "FECHADO": ("Fechado", C_DIM)}.get(shift.get("status"), ("Indef.", C_DIM))
            value = shift.get("fim") if shift.get("status") == "FECHADO" else shift.get("inicio") if shift.get("status") == "ABERTO" else None
        stamp = parse_iso(value) if value else None
        valid_stamp = stamp if stamp and stamp <= now else None
        hour = valid_stamp.astimezone(TZ).strftime("%H:%M:%S") if valid_stamp else "—"
        queue = doc.get("fila") or {}
        pending = f"{queue.get('comercial', '—')}/{queue.get('emergencia', '—')}"
        rows.append((valid_stamp.timestamp() if valid_stamp else float("-inf"),
                     (key, label, hour, pending, color)))
    # Ordenar pela data completa evita inverter eventos na virada da meia-noite.
    # A ordem inicial por equipe mantém o desempate estável.
    rows.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in rows]


def _render_operational_panels(screen, last, data, top, height, width, now):
    split = width >= 144 and not data["teams_expanded"]
    show_teams = split or data["active_panel"] == "teams"
    right_width = (width - 1) // 4 if split else width - 1
    left_width = width - right_width - 2 if split else width - 1
    offsets = {"events_scroll": data["events_scroll"], "teams_scroll": data["teams_scroll"]}
    if split or not show_teams:
        _box(screen, top, 0, height, left_width, "EVENTOS DE EQUIPES", C_TITLE if data["active_panel"] == "events" else C_DIM)
        lines = []
        events = last.get("events") or {}
        for key, title, color in (("local", "LOCAL", C_WARN), ("cloud", "NUVEM", C_OK)):
            entries = events.get(key) or []
            if entries:
                lines.append((f"{title} ({len(entries)} eventos)", color))
                lines.extend((text, C_DEFAULT) for text in _format_columns(
                    [f"{e['team']}: {e['action']}" for e in entries], left_width - 5, col_width=35))
        if not lines:
            lines = [("Nenhuma transicao de equipe no ultimo ciclo." if last else "Aguardando execucoes para exibir eventos.", C_DIM)]
        visible = max(1, height - 3)
        offset = min(max(0, data["events_scroll"]), max(0, len(lines) - visible))
        offsets["events_scroll"] = offset
        for row, (text, color) in enumerate(lines[offset:offset + visible], top + 1):
            _line(screen, row, 2, text, left_width - 4, _cp(color))
        _line(screen, top + height - 2, 2, f"{offset + 1}-{min(offset + visible, len(lines))}/{len(lines)}  Tab: quadro | Setas/Pg: rolar", left_width - 4, _cp(C_DIM))
    if show_teams:
        x = left_width + 1 if split else 0
        _box(screen, top, x, height, right_width, "SITUACAO ATUAL", C_TITLE if data["active_panel"] == "teams" else C_DIM)
        rows = _team_current_rows(data["team_snapshots"], now)
        stamp = parse_iso(data["teams_collected_at"]) if data["teams_collected_at"] else None
        stamp_text = stamp.astimezone(TZ).strftime("%H:%M:%S") if stamp else "—"
        stale = stamp and now - stamp > timedelta(seconds=data["teams_stale_seconds"])
        note = "Indice indisponivel" if data["teams_error"] else f"Desatualizado | {stamp_text}" if stale else f"{len(rows)} equipes | Coleta {stamp_text}"
        _line(screen, top + 1, x + 2, note, right_width - 4, _cp(C_WARN if data["teams_error"] or stale else C_DIM))
        _line(screen, top + 2, x + 2, f"{'Equipe':<8} {'Situacao':<9} {'Hora':<8} C/E", right_width - 4, _cp(C_TITLE, terminal.curses.A_BOLD))
        visible = max(1, height - 5)
        offset = min(max(0, data["teams_scroll"]), max(0, len(rows) - visible))
        offsets["teams_scroll"] = offset
        for row, (team, label, hour, queue, color) in enumerate(rows[offset:offset + visible], top + 3):
            _line(screen, row, x + 2, f"{team:<8} {label:<9} {hour:<8} {queue}", right_width - 4, _cp(color))
        if not rows:
            _line(screen, top + 3, x + 2, "Sem equipes no indice" if not data["teams_error"] else "Aguardando indice local", right_width - 4, _cp(C_DIM))
        _line(screen, top + height - 2, x + 2, f"{offset + 1 if rows else 0}-{min(offset + visible, len(rows))}/{len(rows)} | C/E: fila", right_width - 4, _cp(C_DIM))
    return offsets
