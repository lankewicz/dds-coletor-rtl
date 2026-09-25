"""Persistência local atômica, sem dependências de nuvem."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import re

from .scraper import team_key_from_group


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_gzip_bytes(path: Path, payload: bytes) -> None:
    _atomic_write(path, gzip.compress(payload, compresslevel=9))


def write_json_gzip(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    write_gzip_bytes(path, payload)


def read_json_gzip(path: Path) -> Any:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


class LocalSnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def current_snapshot_path(self) -> Path:
        return self.root / "rotalog" / "current" / "snapshot.json.gz"

    @property
    def ajax_cache_path(self) -> Path:
        return self.root / "rotalog" / "cache" / "ajax-details.json.gz"

    def load_current(self) -> dict[str, Any]:
        if not self.current_snapshot_path.exists():
            return {}
        try:
            value = read_json_gzip(self.current_snapshot_path)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, gzip.BadGzipFile, json.JSONDecodeError):
            return {}

    def save(
        self,
        html: str,
        snapshot: dict[str, Any],
        *,
        day: str,
        stamp: str,
        changes: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        raw_path = self.root / "rotalog" / "raw" / day / f"{stamp}.html.gz"
        archive_path = self.root / "rotalog" / "snapshots" / day / f"{stamp}.json.gz"
        current_path = self.current_snapshot_path
        tower_path = self.root / "rotalog" / "control-tower" / "index.json.gz"
        queue_history_path = self.root / "rotalog" / "queue-history" / f"{day}.json.gz"
        write_gzip_bytes(raw_path, html.encode("utf-8"))
        write_json_gzip(archive_path, snapshot)
        write_json_gzip(current_path, snapshot)
        write_json_gzip(tower_path, self._control_tower(snapshot))
        queue_history = self._update_queue_history(snapshot, queue_history_path, day)
        team_paths = self.save_teams(
            snapshot,
            day=day,
            stamp=stamp,
            changes=changes or {},
            queue_history=queue_history,
        )
        return {
            "raw": raw_path,
            "archive": archive_path,
            "current": current_path,
            "controlTower": tower_path,
            "queueHistory": queue_history_path,
            "teamsCurrentDir": self.root / "rotalog" / "teams" / "current",
            "teamsArchiveDir": self.root / "rotalog" / "teams" / "snapshots" / day,
            "teamFilesWritten": len(team_paths),
        }

    @staticmethod
    def _team_write_reasons(changes: list[dict[str, Any]]) -> list[str]:
        reasons = []
        for change in changes:
            kind = change.get("kind")
            if kind == "SHIFT_OPENED":
                reasons.append("SHIFT_OPENED")
            elif kind == "SHIFT_CLOSED":
                reasons.append("SHIFT_CLOSED")
            elif (
                kind == "SERVICE_STATUS" and change.get("to") == "COMPLETED"
            ) or (
                kind == "SERVICE_NEW" and "CONCLUSÃO" in str(change.get("message") or "")
            ):
                reasons.append("SERVICE_COMPLETED")
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _control_tower(snapshot: dict[str, Any]) -> dict[str, Any]:
        teams = {}
        global_completed = 0
        global_active = 0
        global_pending_emergency = 0
        global_pending_commercial = 0
        for team_key, team in (snapshot.get("teams") or {}).items():
            shift = team.get("shift") or {}
            opened_at = shift.get("openedAt")
            completed_keys = set()
            for service in team.get("completedServices") or []:
                service_at = service.get("startAt")
                if opened_at and service_at and str(service_at) < str(opened_at):
                    continue
                protocol = str(service.get("protocol") or "").strip()
                key = protocol or f"{service.get('startMs')}:{service.get('rawContent') or ''}"
                completed_keys.add(key)
            completed_count = len(completed_keys)
            active_count = 1 if team.get("currentActivity") else 0
            global_completed += completed_count
            global_active += active_count
            pending_emergency = int((team.get("counts") or {}).get("pendingEmergency") or 0)
            pending_commercial = int((team.get("counts") or {}).get("pendingCommercial") or 0)
            global_pending_emergency += pending_emergency
            global_pending_commercial += pending_commercial
            teams[team_key] = {
                "teamKey": team_key,
                "vehicleId": team.get("vehicleId"),
                "regionalCode": team.get("regionalCode"),
                "collaborator": team.get("collaborator"),
                "connection": team.get("connection"),
                "online": team.get("online"),
                "state": team.get("state"),
                "shift": shift,
                "currentActivity": team.get("currentActivity"),
                "shiftServices": {
                    "completed": completed_count,
                    "active": active_count,
                },
                "queue": {
                    "emergency": pending_emergency,
                    "commercial": pending_commercial,
                },
            }
        return {
            "schemaVersion": 1,
            "collectedAt": snapshot.get("collectedAt"),
            "timezone": snapshot.get("timezone"),
            "teamCount": len(teams),
            "shiftServices": {
                "completed": global_completed,
                "active": global_active,
            },
            "queue": {
                "emergency": global_pending_emergency,
                "commercial": global_pending_commercial,
            },
            "teams": teams,
        }

    def save_teams(
        self,
        snapshot: dict[str, Any],
        *,
        day: str,
        stamp: str,
        changes: dict[str, list[dict[str, Any]]] | None = None,
        queue_history: dict[str, Any] | None = None,
    ) -> list[Path]:
        written: list[Path] = []
        events = snapshot.get("events") or []
        for team_key, team in (snapshot.get("teams") or {}).items():
            if not re.fullmatch(
                r"(?:E[A-Z0-9]{3,7}|(?:CA|LO|MA|PG|CB)[A-Z0-9]{2,5})",
                str(team_key or "").upper(),
            ):
                continue
            reasons = self._team_write_reasons((changes or {}).get(team_key, []))
            if not reasons:
                continue
            event_indexes = {
                item.get("eventIndex")
                for item in (team.get("completedServices") or [])
                if item.get("eventIndex") is not None
            }
            current = team.get("currentActivity") or {}
            if current.get("eventIndex") is not None:
                event_indexes.add(current["eventIndex"])
            event_indexes.update(team.get("unclassifiedEvents") or [])
            # Turnos, intervalos e pendências não carregam eventIndex no resumo; o grupo
            # original permite preservar todos os eventos brutos daquela equipe.
            team_events = [
                event for event in events
                if event.get("index") in event_indexes
                or team_key_from_group(str(event.get("group") or "")) == str(team_key).upper()
            ]
            document = {
                "schemaVersion": snapshot.get("schemaVersion", 1),
                "collectedAt": snapshot.get("collectedAt"),
                "timezone": snapshot.get("timezone"),
                "source": snapshot.get("source"),
                "teamKey": team_key,
                "team": team,
                "writeReasons": reasons,
                "eventCount": len(team_events),
                "events": team_events,
                "queueHistory": list(((queue_history or {}).get("teams") or {}).get(team_key) or []),
            }
            current_path = self.root / "rotalog" / "teams" / "current" / f"{team_key}.json.gz"
            archive_path = (
                self.root / "rotalog" / "teams" / "snapshots" / day / team_key / f"{stamp}.json.gz"
            )
            previous_document = {}
            if current_path.exists():
                try:
                    loaded = read_json_gzip(current_path)
                    previous_document = loaded if isinstance(loaded, dict) else {}
                except (OSError, ValueError, gzip.BadGzipFile, json.JSONDecodeError):
                    previous_document = {}
            document = self._merge_team_history(previous_document, document)
            write_json_gzip(current_path, document)
            write_json_gzip(archive_path, document)
            written.extend((current_path, archive_path))
        return written

    @staticmethod
    def _queue_availability(team: dict[str, Any]) -> str:
        if team.get("state") == "BREAK":
            return "BREAK"
        if team.get("currentActivity"):
            return "BUSY"
        pending = team.get("pendingServices") or []
        if pending:
            return "WAITING_WITH_QUEUE"
        if not team.get("online"):
            return "OFFLINE"
        return "IDLE_NO_QUEUE"

    @classmethod
    def _update_queue_history(
        cls,
        snapshot: dict[str, Any],
        path: Path,
        day: str,
    ) -> dict[str, Any]:
        history: dict[str, Any] = {"schemaVersion": 1, "date": day, "teams": {}}
        if path.exists():
            try:
                loaded = read_json_gzip(path)
                if isinstance(loaded, dict) and loaded.get("date") == day:
                    history = loaded
            except (OSError, ValueError, gzip.BadGzipFile, json.JSONDecodeError):
                pass
        teams_history = history.setdefault("teams", {})
        observed_at = snapshot.get("collectedAt")
        for team_key, team in (snapshot.get("teams") or {}).items():
            pending = team.get("pendingServices") or []
            observation = {
                "firstObservedAt": observed_at,
                "lastObservedAt": observed_at,
                "availability": cls._queue_availability(team),
                "currentProtocol": (team.get("currentActivity") or {}).get("protocol"),
                "pendingTotal": len(pending),
                "pendingEmergency": sum(item.get("category") == "EMERGENCIA" for item in pending),
                "pendingCommercial": sum(item.get("category") == "COMERCIAL" for item in pending),
                "pending": [
                    {"sequence": item.get("sequence"), "category": item.get("category")}
                    for item in pending
                ],
            }
            observations = teams_history.setdefault(team_key, [])
            signature_fields = (
                "availability", "currentProtocol", "pendingTotal",
                "pendingEmergency", "pendingCommercial", "pending",
            )
            if observations and all(
                observations[-1].get(field) == observation.get(field) for field in signature_fields
            ):
                observations[-1]["lastObservedAt"] = observed_at
            else:
                observations.append(observation)
        history["updatedAt"] = observed_at
        write_json_gzip(path, history)
        return history

    @staticmethod
    def _service_history_key(service: dict[str, Any]) -> str:
        protocol = str(service.get("protocol") or "").strip()
        if protocol:
            return f"protocol:{protocol}"
        return f"raw:{service.get('startMs')}:{service.get('rawContent') or ''}"

    @classmethod
    def _merge_team_history(cls, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """Preserva serviços já conhecidos e acrescenta somente novos registros."""
        if not previous:
            result = current
            history = (result.get("team") or {}).get("completedServices") or []
            result["historyServiceCount"] = len(history)
            result["writeHistory"] = [{
                "collectedAt": current.get("collectedAt"),
                "reasons": current.get("writeReasons") or [],
            }]
            return result

        old_team = previous.get("team") or {}
        new_team = current.get("team") or {}
        merged_services: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for service in (old_team.get("completedServices") or []) + (new_team.get("completedServices") or []):
            key = cls._service_history_key(service)
            if key not in merged_services:
                merged_services[key] = dict(service)
                order.append(key)
            else:
                # Mantém o registro histórico e aceita apenas informações novas ou
                # correções explícitas que não sejam vazias.
                merged_services[key].update({k: v for k, v in service.items() if v not in (None, "", [])})
        new_team["completedServices"] = [merged_services[key] for key in order]
        counts = dict(new_team.get("counts") or {})
        counts["completed"] = len(new_team["completedServices"])
        new_team["counts"] = counts
        current["team"] = new_team

        merged_queue: dict[tuple[Any, ...], dict[str, Any]] = {}
        queue_order: list[tuple[Any, ...]] = []
        for observation in (previous.get("queueHistory") or []) + (current.get("queueHistory") or []):
            key = (
                observation.get("firstObservedAt"),
                observation.get("availability"),
                observation.get("currentProtocol"),
                observation.get("pendingTotal"),
            )
            if key not in merged_queue:
                queue_order.append(key)
            merged_queue[key] = dict(observation)
        current["queueHistory"] = [merged_queue[key] for key in queue_order]

        # Acumula somente eventos operacionais estáveis. Posições futuras da fila são
        # uma projeção móvel da interface e permanecem apenas na visão atual da equipe.
        stable_types = {
            "SHIFT_MARKER", "BREAK", "SERVICE_COMPLETED",
            "SERVICE_IN_TRANSIT", "SERVICE_IN_PROGRESS",
        }
        merged_events: dict[tuple[Any, ...], dict[str, Any]] = {}
        event_order: list[tuple[Any, ...]] = []
        for event in (previous.get("events") or []) + (current.get("events") or []):
            if event.get("type") not in stable_types:
                continue
            key = (event.get("startMs"), event.get("className"), event.get("content"))
            if key not in merged_events:
                event_order.append(key)
            merged_events[key] = dict(event)
        current["events"] = [merged_events[key] for key in event_order]
        current["eventCount"] = len(current["events"])
        current["historyServiceCount"] = len(new_team["completedServices"])
        write_history = list(previous.get("writeHistory") or [])
        write_history.append({
            "collectedAt": current.get("collectedAt"),
            "reasons": current.get("writeReasons") or [],
        })
        current["writeHistory"] = write_history
        return current
