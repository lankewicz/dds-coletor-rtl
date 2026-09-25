"""Estado incremental de abertura e fechamento de turno por equipe."""

from __future__ import annotations

from typing import Any


OPEN_STATES = {"IN_TRANSIT", "IN_PROGRESS", "BREAK", "AVAILABLE"}


def enrich_shift_state(previous: dict[str, Any], current: dict[str, Any]) -> None:
    previous_teams = previous.get("teams") or {}
    for team_key, team in (current.get("teams") or {}).items():
        before = previous_teams.get(team_key) or {}
        previous_shift = before.get("shift") or {}
        markers = sorted(team.get("shiftMarkers") or [], key=lambda item: item.get("timestampMs") or 0)
        previous_markers = {
            item.get("timestampMs") for item in (before.get("shiftMarkers") or []) if item.get("timestampMs")
        }
        new_markers = [item for item in markers if item.get("timestampMs") not in previous_markers]

        if previous_shift.get("status") in {"OPEN", "CLOSED"}:
            status = previous_shift["status"]
            opened_at = previous_shift.get("openedAt")
            closed_at = previous_shift.get("closedAt")
            transitions = list(previous_shift.get("transitions") or [])
        else:
            status = "OPEN" if team.get("state") in OPEN_STATES else "UNKNOWN"
            opened_at = markers[-1].get("at") if status == "OPEN" and markers else None
            closed_at = None
            transitions = []
            # O primeiro snapshot estabelece uma base; marcadores já existentes não
            # são anunciados como eventos novos.
            new_markers = []

        for marker in new_markers:
            at = marker.get("at")
            if status == "OPEN":
                status = "CLOSED"
                closed_at = at
                transition = "CLOSE"
            else:
                status = "OPEN"
                opened_at = at
                closed_at = None
                transition = "OPEN"
            transitions.append({"type": transition, "at": at, "timestampMs": marker.get("timestampMs")})

        team["shift"] = {
            "status": status,
            "openedAt": opened_at,
            "closedAt": closed_at,
            "transitions": transitions[-10:],
            "newTransitions": transitions[-len(new_markers):] if new_markers else [],
        }

