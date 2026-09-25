"""Busca detalhes ausentes simulando a seleção AJAX de eventos PrimeFaces."""

from __future__ import annotations

from datetime import datetime
import hashlib
import html as html_module
import json
import logging
from pathlib import Path
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

from .client import RotalogClient
from .enrichment import normalize_category, normalize_protocol
from .storage import read_json_gzip, write_json_gzip


LOG = logging.getLogger("rotalog-v2.ajax")


def extract_view_state(page_html: str) -> str | None:
    match = re.search(
        r'<input[^>]+name=["\']javax\.faces\.ViewState["\'][^>]+value=["\']([^"\']+)',
        page_html,
        re.IGNORECASE,
    )
    if not match:
        # Alguns templates invertem a ordem dos atributos.
        match = re.search(
            r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']javax\.faces\.ViewState["\']',
            page_html,
            re.IGNORECASE,
        )
    return html_module.unescape(match.group(1)) if match else None


def _popup_details(response_text: str, expected_team: str) -> dict[str, Any] | None:
    popups = re.findall(
        r"voarParaCoordenadaZoom\(\s*\[(-?\d+\.\d+),\s*(-?\d+\.\d+)\],\s*\d+,\s*['\"](.*?)['\"]\s*\)",
        response_text,
        re.DOTALL,
    )
    for latitude, longitude, popup in popups:
        clean = html_module.unescape(
            popup.replace(r"\'", "'").replace(r"\n", "\n").replace("<BR />", "\n").replace("<br />", "\n")
        )
        popup_team = _match(clean, r"Equipe[\s:-]*(E[A-Z0-9]{3,7})\b")
        if popup_team and popup_team.upper() != expected_team.upper():
            continue
        raw_protocol = _match(clean, r"Protocolo[\s:-]*([0-9.]+)")
        return {
            "protocol": normalize_protocol(raw_protocol or ""),
            "rawProtocol": raw_protocol,
            "sequence": _match(clean, r"Sequ[eê]ncia[\s:-]*([^\n]+)"),
            "statusDetail": _match(clean, r"Status[\s:-]*([^\n]+)"),
            "serviceType": _match(clean, r"Tipo[\s:-]*([^\n]+)"),
            "categoryDetail": normalize_category(_match(clean, r"Categoria[\s:-]*([^\n]+)")),
            "latitude": float(latitude),
            "longitude": float(longitude),
            "dispatchStart": _match(clean, r"In[ií]cio\s+Deslocamento[\s:-]*(\d{2}:\d{2})"),
            "executionStart": _match(clean, r"In[ií]cio\s+Execu[çc][ãa]o[\s:-]*(\d{2}:\d{2})"),
            "executionEnd": _match(clean, r"T[eé]rmino[\s:-]*(\d{2}:\d{2})"),
            "returnTime": _match(clean, r"Retorno[\s:-]*(\d{2}:\d{2})"),
        }
    return None


def _match(value: str, pattern: str) -> str | None:
    match = re.search(pattern, value, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _minutes(value: str | None) -> int | None:
    if not value or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _near(left: str | None, right: str | None, tolerance: int = 3) -> bool:
    left_minutes, right_minutes = _minutes(left), _minutes(right)
    return left_minutes is not None and right_minutes is not None and abs(left_minutes - right_minutes) <= tolerance


def _matches_event_time(details: dict[str, Any], service: dict[str, Any], timezone: ZoneInfo) -> bool:
    start = datetime.fromtimestamp(service["startMs"] / 1000, tz=timezone).strftime("%H:%M")
    end = (
        datetime.fromtimestamp(service["endMs"] / 1000, tz=timezone).strftime("%H:%M")
        if service.get("endMs") else None
    )
    observed = [details.get("dispatchStart"), details.get("executionStart"), details.get("executionEnd"), details.get("returnTime")]
    if not any(observed):
        return True
    return (
        _near(start, details.get("dispatchStart"))
        or _near(start, details.get("executionStart"))
        or _near(end, details.get("executionEnd"))
        or _near(end, details.get("returnTime"))
    )


class AjaxProtocolEnricher:
    def __init__(
        self,
        client: RotalogClient,
        timezone: str,
        *,
        cache_path: Path | None = None,
        time_limit_seconds: float = 120,
    ) -> None:
        self.client = client
        self.timezone = ZoneInfo(timezone)
        self.time_limit_seconds = time_limit_seconds
        self.cache_path = cache_path
        self._cache: dict[str, dict[str, Any]] = self._load_cache()

    @staticmethod
    def _cache_key(team_key: str, service: dict[str, Any]) -> str:
        identity = [
            team_key.upper(),
            service.get("startMs"),
            service.get("endMs"),
            service.get("rawContent"),
        ]
        raw = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path or not self.cache_path.exists():
            return {}
        try:
            payload = read_json_gzip(self.cache_path)
            items = payload.get("items", {}) if isinstance(payload, dict) else {}
            return items if isinstance(items, dict) else {}
        except (OSError, ValueError):
            LOG.warning("AJAX: cache local inválido; será reconstruído")
            return {}

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        write_json_gzip(self.cache_path, {"schemaVersion": 1, "items": self._cache})

    def seed_from_snapshot(self, snapshot: dict[str, Any]) -> int:
        """Migra resultados AJAX existentes para o cache persistente."""
        added = 0
        for team_key, team in (snapshot.get("teams") or {}).items():
            services = list(team.get("completedServices") or [])
            if team.get("currentActivity"):
                services.append(team["currentActivity"])
            for service in services:
                if service.get("source") != "AJAX_POPUP" or not service.get("protocol"):
                    continue
                key = self._cache_key(team_key, service)
                if key not in self._cache:
                    self._cache[key] = {
                        field: service.get(field)
                        for field in (
                            "protocol", "rawProtocol", "sequence", "statusDetail", "serviceType",
                            "categoryDetail", "latitude", "longitude", "dispatchStart",
                            "executionStart", "executionEnd", "returnTime",
                        )
                        if service.get(field) is not None
                    }
                    added += 1
        if added:
            self._save_cache()
            LOG.info("AJAX: cache persistente inicializado com %d resultado(s) anterior(es)", added)
        return added

    def enrich(self, snapshot: dict[str, Any], page_html: str) -> dict[str, int]:
        candidates = self._candidates(snapshot)
        stats = {"candidates": len(candidates), "resolved": 0, "notFound": 0, "failed": 0, "cached": 0}
        if not candidates:
            LOG.info("AJAX: nenhum serviço sem protocolo nesta coleta")
            return stats
        pending = []
        for team_key, service in candidates:
            details = self._cache.get(self._cache_key(team_key, service))
            if details and details.get("protocol"):
                self._apply(service, details)
                stats["cached"] += 1
            else:
                pending.append((team_key, service))
        LOG.info(
            "AJAX: cache local reutilizado para %d serviço(s); consultas necessárias=%d",
            stats["cached"], len(pending),
        )
        if not pending:
            return stats

        view_state = extract_view_state(page_html)
        if not view_state:
            LOG.error("AJAX: ViewState não encontrado; %d serviço(s) não consultado(s)", len(pending))
            stats["failed"] = len(pending)
            return stats

        LOG.info("AJAX: iniciando busca de protocolo para %d serviço(s)", len(pending))
        started = time.monotonic()
        cache_changed = False
        for team_key, service in pending:
            if time.monotonic() - started >= self.time_limit_seconds:
                LOG.warning("AJAX: limite de %.0fs atingido; consultas restantes adiadas", self.time_limit_seconds)
                break
            event_index = service["eventIndex"]
            cache_key = self._cache_key(team_key, service)
            LOG.info(
                "AJAX: equipe=%s evento=%s conteudo=%r status=%s",
                team_key, event_index, service.get("rawContent"), service.get("status"),
            )
            try:
                response = self.client.post_realtime_ajax(self._payload(event_index, view_state))
                details = _popup_details(response, team_key)
                if not details:
                    stats["notFound"] += 1
                    LOG.warning("AJAX: equipe=%s evento=%s sem popup compatível", team_key, event_index)
                    continue
                if not _matches_event_time(details, service, self.timezone):
                    stats["notFound"] += 1
                    LOG.warning("AJAX: equipe=%s evento=%s popup rejeitado por horário divergente", team_key, event_index)
                    continue
                self._apply(service, details)
                if service.get("protocol"):
                    self._cache[cache_key] = dict(details)
                    cache_changed = True
                    stats["resolved"] += 1
                    LOG.info("AJAX: equipe=%s evento=%s protocolo=%s encontrado", team_key, event_index, service["protocol"])
                else:
                    stats["notFound"] += 1
                    LOG.warning("AJAX: equipe=%s evento=%s popup sem protocolo", team_key, event_index)
            except Exception as exc:
                stats["failed"] += 1
                LOG.warning("AJAX: equipe=%s evento=%s falhou: %s", team_key, event_index, exc)
        if cache_changed:
            self._save_cache()
        LOG.info(
            "AJAX: finalizado candidatos=%d resolvidos=%d cache=%d sem_resultado=%d falhas=%d",
            stats["candidates"], stats["resolved"], stats["cached"], stats["notFound"], stats["failed"],
        )
        return stats

    @staticmethod
    def _candidates(snapshot: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        candidates = []
        for team_key, team in (snapshot.get("teams") or {}).items():
            if not re.fullmatch(r"E[A-Z0-9]{3,7}", str(team_key or "").upper()):
                continue
            services = list(team.get("completedServices") or [])
            if team.get("currentActivity"):
                services.insert(0, team["currentActivity"])
            for service in services:
                if not service.get("protocol"):
                    candidates.append((team_key, service))
        candidates.sort(key=lambda item: 0 if item[1].get("status") != "COMPLETED" else 1)
        return candidates

    @staticmethod
    def _payload(event_index: int, view_state: str) -> dict[str, str]:
        source = "form:cm-patientregistry-facesheet-timeline"
        return {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": source,
            "javax.faces.partial.execute": source,
            "javax.faces.partial.render": "form:panelAtualizacaoMapa",
            "javax.faces.behavior.event": "select",
            "javax.faces.partial.event": "select",
            f"{source}_eventIdx": str(event_index),
            "form": "form",
            "javax.faces.ViewState": view_state,
        }

    @staticmethod
    def _apply(service: dict[str, Any], details: dict[str, Any]) -> None:
        for key, value in details.items():
            if value is not None:
                if key == "categoryDetail":
                    value = normalize_category(value)
                service[key] = value
        service["source"] = "AJAX_POPUP"
