"""Extração conservadora da timeline embutida no HTML do ROTALOG."""

from __future__ import annotations

import html as html_module
import re
from typing import Any


EVENT_PATTERN = re.compile(
    r'\{"start":\s*(?:new Date\()?\s*(\d+)\s*\)?\s*,'
    r'\s*"end":\s*(?:new Date\()?\s*(\d+|null)\s*\)?\s*,'
    r'\s*"editable":\s*(true|false)\s*,'
    r'\s*"group":\s*"((?:\\.|[^"\\])*)"\s*,'
    r'\s*"className":\s*"((?:\\.|[^"\\])*)"\s*,'
    r'\s*"content":\s*"((?:\\.|[^"\\])*)"\}',
    re.IGNORECASE,
)


def _decode_js_text(value: str) -> str:
    value = value.replace(r"\"", '"').replace(r"\/", "/")
    value = value.replace(r"\n", "\n").replace(r"\r", "\r").replace(r"\t", "\t")
    return html_module.unescape(value).strip()


def parse_team_group(group: str) -> dict[str, Any]:
    clean = group.strip()
    match = re.search(r"\(([^()]*)\)\s*$", clean)
    connection = match.group(1).strip() if match else ""
    if match:
        clean = clean[: match.start()].strip()
    first, _, collaborator = clean.partition(" ")
    team, separator, vehicle = first.partition("-")
    team = team.strip().upper()
    vehicle = vehicle.strip().upper() if separator else ""
    vehicle_id = team if re.fullmatch(r"E[A-Z0-9]{3,7}", team) else None
    regional_code = vehicle if re.fullmatch(r"(?:CA|LO|MA|PG|CB)[A-Z0-9]{2,5}", vehicle) else None
    identity_source = "VEHICLE_ID" if vehicle_id else "UNRESOLVED"
    if team in {"VEICULO?", "VEÍCULO?"} and vehicle:
        if re.fullmatch(r"E[A-Z0-9]{3,7}", vehicle):
            team = vehicle
            vehicle_id = vehicle
            regional_code = None
            identity_source = "VEHICLE_ID_AFTER_PLACEHOLDER"
        else:
            team = vehicle
            vehicle_id = None
            regional_code = vehicle
            identity_source = "REGIONAL_CODE_FALLBACK"
    return {
        "team": team,
        "vehicle": vehicle_id or "",
        "vehicleId": vehicle_id,
        "regionalCode": regional_code,
        "identitySource": identity_source,
        "collaborator": collaborator.strip(),
        "connection": connection,
        "online": "online" in connection.lower(),
    }


def team_key_from_group(group: str) -> str:
    return parse_team_group(group)["team"]


def extract_timeline(html: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    teams: dict[str, dict[str, Any]] = {}
    for index, match in enumerate(EVENT_PATTERN.finditer(html)):
        start, end, editable, group, class_name, content = match.groups()
        decoded_group = _decode_js_text(group)
        event = {
            "index": index,
            "startMs": int(start),
            "endMs": int(end) if end.isdigit() else None,
            "editable": editable.lower() == "true",
            "group": decoded_group,
            "className": _decode_js_text(class_name),
            "content": _decode_js_text(content),
        }
        events.append(event)
        team = parse_team_group(decoded_group)
        team_key = team["team"]
        if team_key:
            current = teams.setdefault(team_key, {**team, "eventIndexes": []})
            current["eventIndexes"].append(index)

    if not events:
        raise ValueError("Nenhum evento da timeline foi reconhecido.")
    return {"events": events, "teams": teams}
