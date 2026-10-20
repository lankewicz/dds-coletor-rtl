"""Projeção persistente das últimas 24 horas, derivada dos diários consolidados."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from .storage_io import LOCAL_TZ, company_key, load_json_with_status, write_json


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(LOCAL_TZ) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def update_activity_feed(path: Path, company: str, documents: dict, now: datetime) -> dict:
    """Mescla eventos por ocorrência; um horário corrigido substitui a mesma etapa."""
    previous, status = load_json_with_status(path, {})
    if status == "corrupt":
        raise ValueError("Arquivo de eventos corrompido; original preservado")
    if status == "ok" and (company_key(previous.get("company")) != company_key(company)
                     or previous.get("schemaVersion") != 1 or not isinstance(previous.get("items"), list)):
        raise ValueError("Arquivo de eventos incompatível ou de outra empresa")
    start = now - timedelta(hours=24)
    events = {item["id"]: item for item in previous.get("items", [])
              if isinstance(item, dict) and item.get("id") and _timestamp(item.get("occurredAt"))
              and start <= _timestamp(item["occurredAt"]) <= now}

    def add(team, entity, stage, value, protocol=None, category=None, time_source="consolidated"):
        identity = hashlib.sha256(json.dumps([team, entity, stage], ensure_ascii=False).encode()).hexdigest()[:32]
        # Um valor retirado/invalidado não pode sobreviver como evento confirmado.
        events.pop(identity, None)
        stamp = _timestamp(value)
        if not stamp or not start <= stamp <= now:
            return
        category = str(category or "").upper()
        events[identity] = {
            "id": identity, "teamKey": team, "occurredAt": stamp.isoformat(),
            "type": "C" if category == "COMERCIAL" else "E" if category == "EMERGENCIA" else None,
            "protocol": str(protocol) if protocol else None, "status": stage,
            "entityId": entity, "timeSource": time_source,
        }

    for team, doc in documents.items():
        if doc.get("teamKey") != team or company_key(doc.get("company")) != company_key(company):
            raise ValueError("Documento de eventos pertence a outra equipe ou empresa")
        journey = doc.get("jornada") or {}
        for shift in journey.get("turnos") or [journey.get("turno") or {}]:
            begin, end = _timestamp(shift.get("inicio")), _timestamp(shift.get("fim"))
            if begin and end and end < begin:
                continue
            entity = shift.get("turnoId") or "turno:" + str(shift.get("inicio") or shift.get("fim"))
            source = shift.get("timeSource") or "consolidated"
            add(team, entity, "TURNO_ABERTO", shift.get("inicio"), time_source=source)
            add(team, entity, "TURNO_FECHADO", shift.get("fim"), time_source=source)
        for interval in journey.get("intervalos") or []:
            entity = "intervalo:" + str(interval.get("inicio"))
            begin, end = _timestamp(interval.get("inicio")), _timestamp(interval.get("fim"))
            if begin and end and end < begin:
                continue
            add(team, entity, "INTERVALO_INICIADO", interval.get("inicio"))
            add(team, entity, "INTERVALO_ENCERRADO", interval.get("fim"))
        orders = doc.get("ordensServico") or {}
        services = list(orders.get("historico") or [])
        if orders.get("atual"):
            services.append(orders["atual"])
        for service in services:
            entity = service.get("serviceId")
            if not entity:
                continue  # Não deduplicar atendimentos distintos apenas pelo protocolo.
            for stage, field in (("DESLOCAMENTO", "inicioDeslocamento"), ("EXECUCAO", "inicioExecucao"),
                                 ("CONCLUSAO", "fimExecucao")):
                value = service.get(field)
                stamp = _timestamp(value)
                departure = _timestamp(service.get("inicioDeslocamento"))
                if stage == "EXECUCAO" and stamp and departure and stamp < departure:
                    value = None
                if stage == "CONCLUSAO" and service.get("statusAtual") != "CONCLUSAO":
                    value = None
                source = "inferred" if field in (service.get("camposEstimados") or []) else "consolidated"
                add(team, entity, stage, value, service.get("protocolo"), service.get("categoria"), source)
    items = sorted(events.values(), key=lambda item: (item["occurredAt"], item["id"]), reverse=True)
    changed = status == "missing" or items != previous.get("items")
    payload = {"schemaVersion": 1, "company": company, "timezone": str(LOCAL_TZ),
               "windowHours": 24, "updatedAt": now.isoformat() if changed else previous["updatedAt"], "items": items}
    if changed:
        write_json(path, payload)
    return {"payload": payload, "changed": changed}


def sync_activity_feed(path: Path, payload: dict, store) -> str:
    """Recibo por destino permite retomar envios após falha ou reinicialização."""
    suffix = "/equipes/current/index.json.gz"
    if not store.blob_name.endswith(suffix):
        raise ValueError("Destino do índice inválido para publicação do feed")
    blob = store.blob_name[:-len(suffix)] + "/events/current/index.json.gz"
    receipt = path.with_name("firebase-sync.json")
    confirmation = {"bucket": store.bucket_name, "blob": blob,
                    "sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    old, _ = load_json_with_status(receipt, {})
    if old == confirmation:
        return "unchanged"
    store.save_blob(blob, payload)
    write_json(receipt, confirmation)
    return "uploaded"
