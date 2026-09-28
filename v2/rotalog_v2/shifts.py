"""Consolidação incremental de turnos, compatível com os termos da V1."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


ACTIVE_STATES = {"IN_TRANSIT", "IN_PROGRESS", "BREAK", "AVAILABLE"}


def _ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _service_start(service: dict[str, Any] | None) -> tuple[int | None, str | None]:
    service = service or {}
    return _ms(service.get("startMs")), service.get("dispatchStart") or service.get("startAt")


def _time_on_service_day(service: dict[str, Any], value: Any) -> tuple[int | None, str | None]:
    """Combina horários HH:MM do AJAX com a data do evento da timeline."""
    if not value:
        return None, None
    text = str(value)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        base = service.get("startAt")
        if not base or len(text) != 5 or text[2] != ":":
            return None, text
        try:
            start = datetime.fromisoformat(str(base))
            hour, minute = (int(part) for part in text.split(":"))
            moment = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if moment < start:
                moment += timedelta(days=1)
        except (TypeError, ValueError):
            return None, text
    return int(moment.timestamp() * 1000), moment.isoformat()


def _service_end(service: dict[str, Any]) -> tuple[int | None, str | None, str]:
    for field in ("returnTime", "executionEnd", "endAt"):
        timestamp, at = _time_on_service_day(service, service.get(field))
        if timestamp is not None:
            return timestamp, at, "RETORNO_SERVICO"
    timestamp, at = _service_start(service)
    return timestamp, at, "RETORNO_SERVICO"


def _candidate(team: dict[str, Any]) -> dict[str, Any] | None:
    """Encontra um possível fim, sem fechar o turno nesta mesma observação."""
    if team.get("currentActivity") or team.get("state") in {"IN_TRANSIT", "IN_PROGRESS", "BREAK"}:
        return None
    completed = team.get("completedServices") or []
    if not completed:
        return None
    last = max(completed, key=lambda item: _service_end(item)[0] or -1)
    end_ms, end_at, source = _service_end(last)
    if end_ms is None:
        return None
    later_markers = [
        marker for marker in (team.get("shiftMarkers") or [])
        if (_ms(marker.get("timestampMs")) or -1) > end_ms
    ]
    if later_markers:
        # Para vários sinais de fechamento em sequência vale o primeiro.
        marker = min(later_markers, key=lambda item: _ms(item.get("timestampMs")) or 0)
        return {"fimProposto": marker.get("at"), "timestampMs": marker.get("timestampMs"), "origem": "MARCADOR_T"}
    return {"fimProposto": end_at, "timestampMs": end_ms, "origem": source}


def _opening(team: dict[str, Any]) -> tuple[str | None, str | None, int | None]:
    services = list(team.get("completedServices") or [])
    if team.get("currentActivity"):
        services.append(team["currentActivity"])
    starts = [(_service_start(item), item) for item in services]
    starts = [item for item in starts if item[0][0] is not None]
    first_ms, first_at = min((item[0] for item in starts), default=(None, None), key=lambda item: item[0] or 0)
    markers = sorted(team.get("shiftMarkers") or [], key=lambda item: _ms(item.get("timestampMs")) or 0)
    opening_markers = [item for item in markers if first_ms is None or (_ms(item.get("timestampMs")) or 0) <= first_ms]
    if opening_markers:
        # Para vários sinais de abertura em sequência vale o último.
        marker = opening_markers[-1]
        return marker.get("at"), "MARCADOR_T", _ms(marker.get("timestampMs"))
    if first_ms is not None:
        return first_at, "INICIO_DESLOCAMENTO", first_ms
    return None, None, None


def enrich_shift_state(previous: dict[str, Any], current: dict[str, Any]) -> None:
    previous_teams = previous.get("teams") or {}
    for team_key, team in (current.get("teams") or {}).items():
        before = previous_teams.get(team_key) or {}
        old = before.get("shift") or {}
        transitions = list(old.get("transitions") or [])
        new_transitions: list[dict[str, Any]] = []
        opened_at = old.get("openedAt")
        opening_source = old.get("fonteAbertura")
        closed_at = old.get("closedAt")
        closing_source = old.get("fonteFechamento")
        status = old.get("status") if old.get("status") in {"ABERTO", "FECHADO"} else "DESCONHECIDO"

        inferred_open, inferred_source, inferred_ms = _opening(team)
        has_activity = bool(team.get("currentActivity")) or team.get("state") in ACTIVE_STATES
        if status != "ABERTO" and (inferred_open or has_activity):
            status = "ABERTO"
            opened_at = inferred_open
            opening_source = inferred_source or "ATIVIDADE_TEMPO_REAL"
            closed_at = None
            closing_source = None
            if old:
                transition = {"type": "ABERTURA", "at": opened_at, "timestampMs": inferred_ms, "source": opening_source}
                transitions.append(transition)
                new_transitions.append(transition)

        candidate = _candidate(team) if status == "ABERTO" else None
        old_candidate = old.get("fechamentoPendente")
        same_candidate = (
            candidate and old_candidate
            and candidate.get("fimProposto") == old_candidate.get("fimProposto")
            and candidate.get("origem") == old_candidate.get("origem")
        )
        if same_candidate:
            status = "FECHADO"
            closed_at = candidate.get("fimProposto")
            closing_source = candidate.get("origem")
            transition = {
                "type": "FECHAMENTO", "at": closed_at,
                "timestampMs": candidate.get("timestampMs"), "source": closing_source,
            }
            transitions.append(transition)
            new_transitions.append(transition)
            candidate = None

        previous_marker_ids = {
            item.get("timestampMs") for item in (before.get("shiftMarkers") or []) if item.get("timestampMs")
        }
        updates = list(old.get("atualizacoesTurno") or [])
        for marker in team.get("shiftMarkers") or []:
            marker_id = marker.get("timestampMs")
            if marker_id and marker_id not in previous_marker_ids and marker.get("at") != opened_at:
                updates.append({"at": marker.get("at"), "timestampMs": marker_id, "origem": "MARCADOR_T"})

        team["shift"] = {
            "status": status,
            "openedAt": opened_at,
            "closedAt": closed_at,
            "fonteAbertura": opening_source,
            "fonteFechamento": closing_source,
            "fechamentoPendente": candidate,
            "atualizacoesTurno": updates[-20:],
            "transitions": transitions[-20:],
            "newTransitions": new_transitions,
        }
