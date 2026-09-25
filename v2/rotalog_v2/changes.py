"""Detecta e apresenta mudanças operacionais entre snapshots do Tempo Real."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any


LOG = logging.getLogger("rotalog-v2.changes")

STATUS_LABELS = {
    "IN_TRANSIT": "DESLOCAMENTO",
    "IN_PROGRESS": "EXECUÇÃO",
    "COMPLETED": "CONCLUSÃO",
    "BREAK": "INTERVALO",
    "AVAILABLE": "DISPONÍVEL",
    "OFFLINE_WITH_ACTIVITY": "OFFLINE COM ATIVIDADE",
    "UNKNOWN": "DESCONHECIDO",
}


def _service_key(service: dict[str, Any]) -> str:
    protocol = str(service.get("protocol") or "").strip()
    if protocol:
        return f"protocol:{protocol}"
    return f"raw:{service.get('startMs')}:{service.get('rawContent') or ''}"


def _services(team: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for service in team.get("completedServices") or []:
        result[_service_key(service)] = service
    current = team.get("currentActivity")
    if current:
        # A atividade atual prevalece quando a timeline apresenta mais de um bloco
        # relacionado ao mesmo protocolo.
        result[_service_key(current)] = current
    return result


def _hhmm(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) == 5 and text[2] == ":":
        return text
    try:
        return datetime.fromisoformat(text).strftime("%H:%M")
    except (TypeError, ValueError):
        return None


def _status_time(service: dict[str, Any]) -> str | None:
    status = service.get("status")
    if status == "IN_TRANSIT":
        return _hhmm(service.get("dispatchStart")) or _hhmm(service.get("startAt"))
    if status == "IN_PROGRESS":
        return _hhmm(service.get("executionStart")) or _hhmm(service.get("startAt"))
    if status == "COMPLETED":
        return (
            _hhmm(service.get("returnTime"))
            or _hhmm(service.get("executionEnd"))
            or _hhmm(service.get("endAt"))
        )
    return _hhmm(service.get("startAt"))


def _label(status: Any) -> str:
    value = str(status or "UNKNOWN")
    return STATUS_LABELS.get(value, value)


def detect_changes(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Retorna somente mudanças relevantes, agrupadas pela identidade da equipe."""
    if not previous or not previous.get("teams"):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    previous_teams = previous.get("teams") or {}
    for team_key, team in (current.get("teams") or {}).items():
        before = previous_teams.get(team_key)
        if not before:
            # Uma equipe recém-visível não gera dezenas de falsos "novos serviços".
            result[team_key] = [{"kind": "TEAM_APPEARED", "message": "equipe apareceu no Tempo Real"}]
            continue
        changes: list[dict[str, Any]] = []
        for transition in (team.get("shift") or {}).get("newTransitions") or []:
            kind = "SHIFT_OPENED" if transition.get("type") == "OPEN" else "SHIFT_CLOSED"
            label = "turno aberto" if kind == "SHIFT_OPENED" else "turno fechado"
            changes.append({
                "kind": kind,
                "at": transition.get("at"),
                "message": f"{label}" + (f" às {_hhmm(transition.get('at'))}" if transition.get("at") else ""),
            })
        previous_services = _services(before)
        current_services = _services(team)
        for key, service in current_services.items():
            old = previous_services.get(key)
            identifier = service.get("protocol") or f"SEM PROTOCOLO ({service.get('rawContent') or 'evento'})"
            if old is None:
                status = _label(service.get("status"))
                at = _status_time(service)
                changes.append({
                    "kind": "SERVICE_NEW",
                    "protocol": service.get("protocol"),
                    "message": f"{identifier}: NOVO -> {status}" + (f" {at}" if at else ""),
                })
                continue
            if old.get("status") != service.get("status"):
                old_status, new_status = _label(old.get("status")), _label(service.get("status"))
                old_at, new_at = _status_time(old), _status_time(service)
                left = f"{old_status}" + (f" {old_at}" if old_at else "")
                right = f"{new_status}" + (f" {new_at}" if new_at else "")
                changes.append({
                    "kind": "SERVICE_STATUS",
                    "protocol": service.get("protocol"),
                    "from": old.get("status"),
                    "to": service.get("status"),
                    "message": f"{identifier}: {left} -> {right}",
                })

        old_regional = before.get("regionalCode")
        new_regional = team.get("regionalCode")
        if old_regional and new_regional and old_regional != new_regional:
            changes.append({
                "kind": "REGIONAL_CHANGED",
                "message": f"regional: {old_regional} -> {new_regional}",
            })
        if before.get("online") != team.get("online"):
            changes.append({
                "kind": "CONNECTION_CHANGED",
                "message": "conexão: " + ("ONLINE" if team.get("online") else "OFFLINE"),
            })
        old_state, new_state = before.get("state"), team.get("state")
        if old_state != new_state and not any(item["kind"] == "SERVICE_STATUS" for item in changes):
            changes.append({
                "kind": "TEAM_STATE",
                "message": f"estado: {_label(old_state)} -> {_label(new_state)}",
            })
        if changes:
            result[team_key] = changes
    return result


def log_changes(changes: dict[str, list[dict[str, Any]]], teams: dict[str, Any]) -> None:
    if not changes:
        LOG.info("ALTERAÇÕES: nenhuma mudança operacional")
        return
    total = sum(len(items) for items in changes.values())
    LOG.info("ALTERAÇÕES: %d mudança(s) em %d equipe(s)", total, len(changes))
    for team_key, items in changes.items():
        team = teams.get(team_key) or {}
        context = " | ".join(
            value for value in (team.get("regionalCode"), team.get("collaborator")) if value
        )
        LOG.info("%s%s", team_key, f" | {context}" if context else "")
        for item in items:
            LOG.info("  %s", item["message"])
