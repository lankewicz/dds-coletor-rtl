"""Cadastro local permanente de equipes e suas atribuições ao longo do tempo."""

from __future__ import annotations

import uuid
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .equipes import member_signature, normalize_team_key, valid_team_key
from .file_lock import exclusive_file_lock
from .storage_io import load_json, write_json


class TeamRegistry:
    """Mantém a identidade da equipe separada do código mutável do veículo."""

    def __init__(self, path: Path):
        self.path = path
        self._load()

    def _load(self) -> None:
        loaded = load_json(self.path, {})
        self.data: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
        self.data.setdefault("schemaVersion", 1)
        self.data.setdefault("teams", {})
        self.data.setdefault("professionals", {})
        self.dirty = False

    @contextmanager
    def exclusive_update(self, timeout_seconds: float = 120.0) -> Iterator["TeamRegistry"]:
        """Serializa uma atualização e recarrega o estado após obter o lock."""
        lock_path = self.path.with_name(self.path.name + ".lock")
        with exclusive_file_lock(lock_path, timeout_seconds=timeout_seconds):
            self._load()
            yield self
            self.save()

    @staticmethod
    def _new_team_id() -> str:
        return f"TEAM-{uuid.uuid4().hex[:12].upper()}"

    def _indexes(self) -> tuple[dict[str, str], dict[str, str]]:
        vehicles: dict[str, str] = {}
        members: dict[str, str] = {}
        ambiguous_members: set[str] = set()
        for team_id, team in self.data["teams"].items():
            for assignment in team.get("vehicleAssignments") or []:
                if not assignment.get("to") and valid_team_key(assignment.get("vehicleCode")):
                    vehicles[normalize_team_key(assignment["vehicleCode"])] = team_id
            for alias in team.get("memberAliases") or []:
                key = str(alias.get("membersKey") or "")
                if not key:
                    continue
                if key in members and members[key] != team_id:
                    ambiguous_members.add(key)
                else:
                    members[key] = team_id
        for key in ambiguous_members:
            members.pop(key, None)
        return vehicles, members

    def observe(self, vehicle_code: object, members_display: object, observed_at: str) -> dict[str, Any]:
        """Relaciona uma observação à identidade permanente e atualiza atribuições."""
        vehicle_code = normalize_team_key(vehicle_code)
        members_display = str(members_display or "").strip()
        members_key = member_signature(members_display)
        vehicle_index, member_index = self._indexes()

        by_vehicle = vehicle_index.get(vehicle_code) if valid_team_key(vehicle_code) else None
        by_members = member_index.get(members_key) if members_key else None
        source = "NEW_TEAM"

        if by_members:
            team_id = by_members
            source = "MEMBER_HISTORY"
        elif by_vehicle:
            team_id = by_vehicle
            source = "ACTIVE_VEHICLE"
        else:
            team_id = self._new_team_id()

        team = self.data["teams"].setdefault(team_id, {
            "teamId": team_id,
            "createdAt": observed_at,
            "vehicleAssignments": [],
            "memberAliases": [],
        })
        if "updatedAt" not in team:
            team["updatedAt"] = observed_at
            self.dirty = True

        if valid_team_key(vehicle_code):
            # Uma equipe conhecida pelo conjunto de integrantes assumiu um novo veículo.
            # Encerra a atribuição ativa anterior desse veículo, sem apagar seu histórico.
            if by_members and by_vehicle and by_members != by_vehicle:
                previous_team = self.data["teams"].get(by_vehicle) or {}
                for assignment in previous_team.get("vehicleAssignments") or []:
                    if normalize_team_key(assignment.get("vehicleCode")) == vehicle_code and not assignment.get("to"):
                        assignment["to"] = observed_at
                        assignment["reason"] = "REASSIGNED"
                previous_team["updatedAt"] = observed_at
                self.dirty = True

            active = next((item for item in team["vehicleAssignments"]
                           if normalize_team_key(item.get("vehicleCode")) == vehicle_code and not item.get("to")), None)
            if not active:
                for assignment in team["vehicleAssignments"]:
                    if not assignment.get("to"):
                        assignment["to"] = observed_at
                        assignment.setdefault("reason", "VEHICLE_CHANGED")
                active = {"vehicleCode": vehicle_code, "from": observed_at, "to": None}
                team["vehicleAssignments"].append(active)
                team["updatedAt"] = observed_at
                self.dirty = True

        if members_key:
            alias = next((item for item in team["memberAliases"] if item.get("membersKey") == members_key), None)
            if not alias:
                alias = {
                    "membersKey": members_key,
                    "displayName": members_display,
                    "firstSeenAt": observed_at,
                }
                team["memberAliases"].append(alias)
                team["updatedAt"] = observed_at
                self.dirty = True

        return {
            "teamId": team_id,
            "vehicleCode": vehicle_code,
            "membersKey": members_key,
            "identitySource": source,
        }

    def record_history(self, team_id: str, operational_date: str, vehicle_code: str) -> None:
        """Indexa o arquivo diário sem duplicar serviços dentro do cadastro."""
        team = self.data["teams"].get(team_id)
        if not team:
            return
        reference = {
            "operationalDate": operational_date,
            "vehicleCode": normalize_team_key(vehicle_code),
            "path": f"daily/{operational_date}/{normalize_team_key(vehicle_code)}.json",
        }
        history = team.setdefault("historyFiles", [])
        current = next((item for item in history
                        if item.get("operationalDate") == operational_date
                        and normalize_team_key(item.get("vehicleCode")) == reference["vehicleCode"]), None)
        if current:
            current.update(reference)
        else:
            history.append(reference)
            history.sort(key=lambda item: (str(item.get("operationalDate") or ""), str(item.get("vehicleCode") or "")))
            self.dirty = True

    @staticmethod
    def _first_name(full_name: str) -> str:
        tokens = re.sub(r"[^A-Z0-9\s]", " ", str(full_name or "").upper()).split()
        return tokens[0] if tokens else ""

    def _team_for_vehicle(self, vehicle_code: str, observed_at: str) -> str | None:
        vehicle_code = normalize_team_key(vehicle_code)
        candidates: list[tuple[str, dict[str, Any]]] = []
        for team_id, team in self.data["teams"].items():
            for assignment in team.get("vehicleAssignments") or []:
                if normalize_team_key(assignment.get("vehicleCode")) != vehicle_code:
                    continue
                candidates.append((team_id, assignment))
                start = str(assignment.get("from") or "")
                end = str(assignment.get("to") or "")
                if (not start or observed_at >= start) and (not end or observed_at < end):
                    return team_id
        # Durante a migração não conhecemos a data inicial real da primeira atribuição.
        # Se existe uma única equipe ligada ao veículo, ela é a melhor evidência histórica.
        unique = {team_id for team_id, _ in candidates}
        return next(iter(unique)) if len(unique) == 1 else None

    def enrich_professionals(
        self,
        vehicle_code: str,
        professionals: list[dict[str, str]],
        observed_at: str,
        shift_reference: str = "",
    ) -> str | None:
        """Inclui registros e nomes oficiais obtidos no histórico do ROTALOG."""
        clean = [
            {
                "registration": str(item.get("registration") or "").strip(),
                "fullName": str(item.get("fullName") or "").strip().upper(),
            }
            for item in professionals
            if str(item.get("registration") or "").strip() or str(item.get("fullName") or "").strip()
        ]
        if not clean:
            return None

        short_names = [self._first_name(item["fullName"]) for item in clean]
        short_display = " ".join(name for name in short_names if name)
        team_id = self._team_for_vehicle(vehicle_code, observed_at)
        if not team_id:
            team_id = self.observe(vehicle_code, short_display, observed_at)["teamId"]
        team = self.data["teams"][team_id]

        short_key = member_signature(short_display)
        if short_key and not any(item.get("membersKey") == short_key for item in team.get("memberAliases") or []):
            team.setdefault("memberAliases", []).append({
                "membersKey": short_key,
                "displayName": short_display,
                "firstSeenAt": observed_at,
                "source": "ROTALOG_HISTORY",
            })
            self.dirty = True

        registrations: list[str] = []
        for item in clean:
            registration = item["registration"]
            full_name = item["fullName"]
            professional_key = registration or f"NAME:{member_signature(full_name)}"
            professional = self.data["professionals"].setdefault(professional_key, {
                "registration": registration,
                "fullName": full_name,
                "nameAliases": [],
                "firstSeenAt": observed_at,
            })
            if full_name and professional.get("fullName") != full_name:
                old_name = professional.get("fullName")
                if old_name and old_name not in professional["nameAliases"]:
                    professional["nameAliases"].append(old_name)
                professional["fullName"] = full_name
                self.dirty = True
            if professional.get("lastSeenAt") != observed_at:
                professional["lastSeenAt"] = observed_at
                self.dirty = True
            registrations.append(professional_key)

            relation = next((ref for ref in team.setdefault("professionals", [])
                             if ref.get("professionalId") == professional_key), None)
            if not relation:
                team["professionals"].append({
                    "professionalId": professional_key,
                    "registration": registration,
                    "fullName": full_name,
                    "firstSeenAt": observed_at,
                    "lastSeenAt": observed_at,
                })
                self.dirty = True
            elif relation.get("lastSeenAt") != observed_at:
                relation["lastSeenAt"] = observed_at
                relation["fullName"] = full_name or relation.get("fullName")
                self.dirty = True

        crew_key = "|".join(sorted(registrations))
        crew = next((item for item in team.setdefault("crewHistory", [])
                     if item.get("observedAt") == observed_at
                     and item.get("crewKey") == crew_key
                     and item.get("shiftReference") == shift_reference), None)
        if not crew:
            team["crewHistory"].append({
                "observedAt": observed_at,
                "shiftReference": shift_reference,
                "vehicleCode": normalize_team_key(vehicle_code),
                "crewKey": crew_key,
                "professionalIds": sorted(registrations),
            })
            team["crewHistory"].sort(key=lambda item: str(item.get("observedAt") or ""))
            self.dirty = True
        team["updatedAt"] = max(str(team.get("updatedAt") or ""), observed_at)
        return team_id

    def save(self) -> None:
        if self.dirty or not self.path.exists():
            write_json(self.path, self.data)
            self.dirty = False
