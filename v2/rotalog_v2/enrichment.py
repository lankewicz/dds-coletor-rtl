"""Enriquecimento determinístico dos eventos coletados da tela Tempo Real."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo

from .scraper import team_key_from_group


def normalize_category(value: str | None) -> str | None:
    if not value:
        return None
    ascii_value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^A-Z]", "", ascii_value.upper())
    if "EMERGENCIA" in normalized:
        return "EMERGENCIA"
    if "COMERCIAL" in normalized:
        return "COMERCIAL"
    return normalized or None


def normalize_protocol(value: str) -> str | None:
    """Extrai protocolos comerciais ou emergenciais sem inventar identificadores."""
    clean = re.sub(r"\.\d+(?:\.\d+)?$", "", str(value or "").strip())
    commercial = re.search(r"\b(202\d{11})\b", clean)
    if commercial:
        return commercial.group(1)
    emergency = re.search(r"\b(\d{7,8})\b", clean)
    if emergency:
        return emergency.group(1)
    return None


def _iso(timestamp_ms: int | None, timezone: ZoneInfo) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone).isoformat()


def _classify(event: dict[str, Any]) -> tuple[str, str | None]:
    class_name = str(event.get("className") or "")
    class_normalized = normalize_category(class_name) or class_name.upper()
    class_lower = class_name.lower()
    content = str(event.get("content") or "").strip()
    if content == "T" and "macro" in class_lower:
        return "SHIFT_MARKER", None
    if content.upper() == "INTERVALO" or "intervalo" in class_lower:
        return "BREAK", None
    category = "EMERGENCIA" if "EMERGENCIA" in class_normalized else "COMERCIAL"
    if "executado" in class_lower:
        return "SERVICE_COMPLETED", category
    if "emdeslocamento" in class_lower:
        return "SERVICE_IN_TRANSIT", category
    if "emexecucao" in class_lower:
        return "SERVICE_IN_PROGRESS", category
    if "pendente" in class_lower:
        return "SERVICE_PENDING", category
    return "UNKNOWN", None


def enrich_realtime(parsed: dict[str, Any], timezone_name: str) -> dict[str, Any]:
    timezone = ZoneInfo(timezone_name)
    enriched_events: list[dict[str, Any]] = []
    teams: dict[str, dict[str, Any]] = {}

    for raw in parsed.get("events", []):
        event_type, category = _classify(raw)
        event = {
            **raw,
            "type": event_type,
            "category": category,
            "startAt": _iso(raw.get("startMs"), timezone),
            "endAt": _iso(raw.get("endMs"), timezone),
        }
        enriched_events.append(event)
        metadata = parsed.get("teams", {}).get(raw.get("group"), {})
        # O parser indexa equipes pela chave normalizada; busca direta é o caminho comum.
        team_key = team_key_from_group(str(raw.get("group") or ""))
        metadata = parsed.get("teams", {}).get(team_key, metadata)
        team = teams.setdefault(team_key, {
            "team": team_key,
            "vehicle": metadata.get("vehicle", ""),
            "vehicleId": metadata.get("vehicleId"),
            "regionalCode": metadata.get("regionalCode"),
            "identitySource": metadata.get("identitySource"),
            "collaborator": metadata.get("collaborator", ""),
            "connection": metadata.get("connection", ""),
            "online": bool(metadata.get("online")),
            "state": "UNKNOWN",
            "currentActivity": None,
            "shiftMarkers": [],
            "breaks": [],
            "completedServices": [],
            "pendingServices": [],
            "unclassifiedEvents": [],
        })

        if event_type == "SHIFT_MARKER":
            team["shiftMarkers"].append({"at": event["startAt"], "timestampMs": event["startMs"]})
        elif event_type == "BREAK":
            team["breaks"].append({
                "startAt": event["startAt"],
                "endAt": event["endAt"],
                "active": event["endMs"] is None,
            })
        elif event_type == "SERVICE_COMPLETED":
            team["completedServices"].append(_service(event))
        elif event_type in {"SERVICE_IN_TRANSIT", "SERVICE_IN_PROGRESS"}:
            activity = _service(event)
            current = team["currentActivity"]
            if current is None or activity["startMs"] >= current["startMs"]:
                team["currentActivity"] = activity
        elif event_type == "SERVICE_PENDING":
            team["pendingServices"].append({
                "sequence": event["content"],
                "category": category,
                "observedAt": event["startAt"],
            })
        else:
            team["unclassifiedEvents"].append(event["index"])

    for team in teams.values():
        active_break = next((item for item in reversed(team["breaks"]) if item["active"]), None)
        if active_break:
            team["state"] = "BREAK"
        elif team["currentActivity"]:
            team["state"] = team["currentActivity"]["status"]
        elif team["online"]:
            team["state"] = "AVAILABLE"
        elif team["shiftMarkers"] or team["completedServices"]:
            team["state"] = "OFFLINE_WITH_ACTIVITY"
        team["counts"] = {
            "completed": len(team["completedServices"]),
            "pending": len(team["pendingServices"]),
            "pendingEmergency": sum(item["category"] == "EMERGENCIA" for item in team["pendingServices"]),
            "pendingCommercial": sum(item["category"] == "COMERCIAL" for item in team["pendingServices"]),
            "unclassified": len(team["unclassifiedEvents"]),
        }

    return {"events": enriched_events, "teams": teams}


def _service(event: dict[str, Any]) -> dict[str, Any]:
    status_by_type = {
        "SERVICE_COMPLETED": "COMPLETED",
        "SERVICE_IN_TRANSIT": "IN_TRANSIT",
        "SERVICE_IN_PROGRESS": "IN_PROGRESS",
    }
    return {
        "eventIndex": event["index"],
        "protocol": normalize_protocol(event["content"]),
        "rawContent": event["content"],
        "category": event["category"],
        "status": status_by_type[event["type"]],
        "startMs": event["startMs"],
        "endMs": event["endMs"],
        "startAt": event["startAt"],
        "endAt": event["endAt"],
        "source": "TIMELINE",
    }
