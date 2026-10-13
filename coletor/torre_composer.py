"""Compõe a torre local e mantém eventos em journal atômico entre reinícios."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coletor.equipes import normalize_team_key
from coletor.file_lock import exclusive_file_lock
from coletor.storage_io import load_json_with_status, write_json
from coletor.torre_cards import (
    _build_pure_torre_items, _build_dds_controlled_items, _normalized_alias_matches,
    to_utc_dt,
)
from coletor.torre_inputs import validate_catalog, validate_daily
from torre_contract import timestamp, validate_projection, publish_projection


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def canonical_resolver(teams):
    def resolve(raw):
        raw = normalize_team_key(raw)
        if raw in teams:
            return raw
        matches = {key for key, team in teams.items()
                   if any(_normalized_alias_matches(raw, alias) for alias in team["aliases"])}
        if len(matches) > 1:
            raise ValueError("Alias de equipe ambíguo")
        return next(iter(matches), raw)
    return resolve


def compose_cards(index, inputs, company, now):
    if not isinstance(index, dict) or index.get("company") != company or not isinstance(index.get("equipes"), dict):
        raise ValueError("Índice Rotalog inválido ou de outra empresa")
    timestamp(index.get("lastCollectedAt"))
    if inputs.get("company") != company or not isinstance(inputs.get("daily"), dict):
        raise ValueError("Entradas DDS inválidas ou de outra empresa")
    catalog = validate_catalog(inputs["catalog"]["data"], company)
    teams = catalog["teams"]
    resolve = canonical_resolver(teams)
    snapshots = {}
    for key, snapshot in index["equipes"].items():
        if not isinstance(snapshot, dict):
            raise ValueError("Snapshot inválido")
        canonical = resolve(snapshot.get("teamKey") or key)
        if canonical in snapshots:
            raise ValueError("Snapshots duplicados para a mesma equipe")
        snapshots[canonical] = {**snapshot, "teamKey": canonical}
    projections = {}
    for day, entry in inputs["daily"].items():
        data = validate_daily(entry["data"], company, day)
        mapped = {}
        for key, presence in data["teams"].items():
            canonical = resolve(key)
            # O DDS escolhe a primeira execução do dia para a abertura.
            if canonical not in mapped or timestamp(presence["completedAt"]) < timestamp(mapped[canonical]["completedAt"]):
                mapped[canonical] = presence
        projections[day] = {**data, "teams": mapped}
    available = sorted(projections)
    today = now.date().isoformat()
    recent = {}
    for day in available:
        if 0 <= (now.date() - datetime.fromisoformat(day).date()).days <= 7:
            for key, presence in projections[day]["teams"].items():
                if key not in recent or timestamp(presence["completedAt"]) > timestamp(recent[key]["completedAt"]):
                    recent[key] = presence
    daily = (available, projections)
    items = _build_pure_torre_items(snapshots, now, teams, recent, daily_projections=daily)
    items += _build_dds_controlled_items(set(snapshots), now, teams, recent,
                                        today_dds_teams=projections.get(today, {}).get("teams", {}),
                                        daily_projections=daily)
    for item in items:
        snapshot = snapshots.get(item["teamKey"])
        if snapshot:
            executed = snapshot.get("executadosHoje", {})
            if not isinstance(executed, dict):
                raise ValueError("Contadores inválidos")
            for count in executed.values():
                if type(count) is not int or count < 0:
                    raise ValueError("Contador inválido")
            total = sum(executed.values())
            item.update(executadosHoje=executed, fila=snapshot.get("fila", {}),
                        totalConcluidos=total, ssExecutadasCount=total)
            item["operacional"]["totalConcluidos"] = total
            item["teamId"] = snapshot.get("teamId")
        item["cadastroActive"] = teams.get(item["teamKey"], {}).get("active")
    return sorted(items, key=lambda item: item["teamKey"]), resolve


def service_observations(documents, resolve):
    observed = {}
    for raw_team, document in documents.items():
        team = resolve(raw_team)
        orders = document.get("ordensServico") or {}
        records = list(orders.get("historico") or document.get("services") or [])
        if orders.get("atual"):
            records.append(orders["atual"])
        for service in records:
            if not isinstance(service, dict):
                raise ValueError("Serviço inválido")
            key = service.get("serviceId") or [service.get("protocolo"),
                    service.get("inicioDeslocamento") or service.get("inicioExecucao")]
            if not key or key == [None, None]:
                continue
            status = str(service.get("statusAtual") or service.get("status") or "").upper()
            if status not in ("DESLOCAMENTO", "EXECUCAO", "CONCLUSAO", "CONCLUIDO"):
                continue
            if status == "CONCLUIDO":
                status = "CONCLUSAO"
            field = {"DESLOCAMENTO": "inicioDeslocamento", "EXECUCAO": "inicioExecucao", "CONCLUSAO": "fimExecucao"}[status]
            event_at = service.get(field) or (service.get("retorno") if status == "CONCLUSAO" else None)
            for transition in service.get("transitions") or []:
                if transition.get("status") in (status, "CONCLUIDO" if status == "CONCLUSAO" else status):
                    millis = transition.get("timestampMs")
                    if isinstance(millis, (int, float)) and not isinstance(millis, bool) and millis > 0:
                        event_at = datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat()
                    elif transition.get("timestamp"):
                        event_at = transition["timestamp"]
            timeline = {}
            for candidate in (service.get("transitions") or []):
                transition_status = str(candidate.get("status") or "").upper()
                if transition_status == "CONCLUIDO":
                    transition_status = "CONCLUSAO"
                if transition_status not in ("DESLOCAMENTO", "EXECUCAO", "CONCLUSAO"):
                    continue
                millis = candidate.get("timestampMs")
                value = candidate.get("timestamp")
                if isinstance(millis, (int, float)) and not isinstance(millis, bool) and millis > 0:
                    value = datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat()
                dt = to_utc_dt(value)
                if dt:
                    transition = {"status": transition_status, "occurredAt": dt.isoformat()}
                    timeline[identity(transition)] = transition
            for transition_status, field_name in (("DESLOCAMENTO", "inicioDeslocamento"),
                                                   ("EXECUCAO", "inicioExecucao"), ("CONCLUSAO", "fimExecucao")):
                dt = to_utc_dt(service.get(field_name))
                if dt:
                    transition = {"status": transition_status, "occurredAt": dt.isoformat()}
                    timeline[identity(transition)] = transition
            stamp = to_utc_dt(event_at)
            service_key = identity([team, key])
            accumulated = observed.get(service_key, {}).get("timeline", {})
            observed[service_key] = {"teamKey": team, "serviceId": key,
                                    "status": status, "occurredAt": stamp.isoformat() if stamp else None,
                                    "timeline": {**accumulated, **timeline}}
    return observed


def update_events(previous, items, services, now):
    old_states = previous.get("states", {})
    old_services = previous.get("services", {})
    previous_at = timestamp(previous["projection"]["composedAt"]) if previous else None
    states = {item["teamKey"]: {"turnStatus": item["turnStatus"], "estado": item["estado"]} for item in items}
    events = {event["id"]: event for event in previous.get("projection", {}).get("events", [])
              if event["teamKey"] in states and timestamp(event["occurredAt"]) >= now - timedelta(hours=24)}

    def add(team, kind, real_at, details):
        stamp = to_utc_dt(real_at)
        if stamp and (stamp > now + timedelta(seconds=30) or (previous_at and stamp < previous_at)):
            return
        occurred = (stamp or now).isoformat()
        event_id = identity([team, kind, occurred, details])
        events[event_id] = {"id": event_id, "teamKey": team, "type": kind,
                            "occurredAt": occurred, "timeSource": "source" if stamp else "observation", **details}

    for item in items:
        key = item["teamKey"]
        old = old_states.get(key)
        if old is None:
            continue  # Primeira observação nunca inventa abertura/fechamento.
        if old["turnStatus"] == "ABERTO" and item["turnStatus"] == "FECHADO":
            add(key, "turno_fechado", item.get("turnoFim"), {})
        elif old["turnStatus"] != "ABERTO" and item["turnStatus"] == "ABERTO":
            add(key, "turno_aberto", item.get("turnoInicio"), {})
        if old["estado"] != item["estado"] and "INTERVALO" in (old["estado"], item["estado"]):
            add(key, "intervalo_inicio" if item["estado"] == "INTERVALO" else "intervalo_fim", None, {})
    if previous:
        for key, service in services.items():
            old = old_services.get(key)
            timeline = service.get("timeline", {})
            known = (old or {}).get("timeline", {})
            for transition_id, transition in timeline.items():
                if transition_id not in known and service["teamKey"] in states:
                    add(service["teamKey"], "servico_" + transition["status"].lower(), transition["occurredAt"],
                        {"serviceId": service["serviceId"], "status": transition["status"]})
            if old is None and service["status"] == "CONCLUSAO" and not service["occurredAt"]:
                continue  # Histórico sem horário não prova uma conclusão recente.
            if old is None or old["status"] != service["status"]:
                if service["teamKey"] in states and not any(t["status"] == service["status"] for t in timeline.values()):
                    add(service["teamKey"], "servico_" + service["status"].lower(), service["occurredAt"],
                        {"serviceId": service["serviceId"], "status": service["status"]})
    # Guardar cursores de serviços já observados evita duplicação em janelas/arquivos ausentes.
    cursor = dict(old_services)
    for key, service in services.items():
        cursor[key] = {**service, "timeline": {**old_services.get(key, {}).get("timeline", {}),
                                               **service.get("timeline", {})}}
    return states, cursor, sorted(events.values(), key=lambda e: (e["occurredAt"], e["id"]))


class TorreComposer:
    def __init__(self, root, company, rotalog_max_age=1200):
        self.root, self.company, self.rotalog_max_age = Path(root), company, rotalog_max_age
        self.path = self.root / "torre/current.json"
        self.journal = self.root / "torre/state/current.json.gz"

    def publish(self, index, inputs, documents=None, now=None):
        from zoneinfo import ZoneInfo
        now = (now or datetime.now(ZoneInfo("America/Sao_Paulo"))).astimezone(ZoneInfo("America/Sao_Paulo"))
        with exclusive_file_lock(self.journal.with_name("compose.lock")):
            previous, state = load_json_with_status(self.journal, {})
            if state == "corrupt":
                raise ValueError("Journal corrompido; preservar eventos e saída anterior")
            if previous:
                validate_projection(previous["projection"], self.company)
            items, resolve = compose_cards(index, inputs, self.company, now)
            services = service_observations(documents or index["equipes"], resolve)
            states, cursor, events = update_events(previous, items, services, now)
            collected = timestamp(index["lastCollectedAt"])
            age = (now-collected).total_seconds()
            sources = {**inputs["sources"], "rotalog": {"updatedAt": collected.isoformat(),
                        "stale": bool(index.get("collectionFailed")) or age < -30 or age > self.rotalog_max_age}}
            projection = {"schemaVersion": 1, "company": self.company, "collectedAt": collected.isoformat(),
                          "composedAt": now.isoformat(), "stale": any(source["stale"] for source in sources.values()),
                          "sources": sources, "items": items, "events": events, "summary": index.get("summary", {})}
            validate_projection(projection, self.company)
            # Journal primeiro: se a publicação falhar, a próxima tentativa recupera os eventos.
            write_json(self.journal, {"projection": projection, "states": states, "services": cursor})
            publish_projection(self.path, projection, self.company)
            return projection
