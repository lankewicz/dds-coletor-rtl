"""Contrato da projeção completa; independente da coleta e do servidor HTTP."""
from datetime import datetime


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp ausente")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp deve conter fuso")
    return parsed


def validate_projection(data, company):
    """Rejeita projeções incompletas antes de publicar ou servir."""
    if (not isinstance(data, dict) or data.get("company") != company
            or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1):
        raise ValueError("Versão ou empresa inválida")
    timestamp(data.get("collectedAt"))
    timestamp(data.get("composedAt"))
    if type(data.get("stale")) is not bool:
        raise ValueError("Disponibilidade ausente")
    sources = data.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("Fontes ausentes")
    for name in ("rotalog", "cadastro", "dds"):
        source = sources.get(name)
        if not isinstance(source, dict) or type(source.get("stale")) is not bool:
            raise ValueError("Fonte incompleta")
        timestamp(source.get("updatedAt"))
    if any(sources[name]["stale"] for name in ("rotalog", "cadastro", "dds")) and not data["stale"]:
        raise ValueError("Fonte desatualizada exige stale")
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("Cards ausentes")
    keys = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Card inválido")
        key = item.get("teamKey")
        if not isinstance(key, str) or not key.strip() or key != key.strip().upper() or key in keys:
            raise ValueError("Identidade ausente ou duplicada")
        keys.add(key)
        for name in ("equipe", "setor", "estado", "turnStatus"):
            if not isinstance(item.get(name), str) or not item[name]:
                raise ValueError("Campo obrigatório do card ausente")
        if type(item.get("active")) is not bool or not isinstance(item.get("operacional"), dict):
            raise ValueError("Estado do card ausente")
        timestamp(item.get("updatedAt"))
        for name in ("participantes", "ddsHistory", "ddsDays"):
            if not isinstance(item.get(name), list) or any(not isinstance(v, str) for v in item[name]):
                raise ValueError("Lista do card inválida")
        if len(item["ddsHistory"]) != len(item["ddsDays"]):
            raise ValueError("Histórico DDS desalinhado")
        if not isinstance(item.get("ddsTimes"), dict) or item.get("ddsToday") not in ("ok", "neutral"):
            raise ValueError("Presença DDS inválida")
    events = data.get("events")
    if not isinstance(events, list):
        raise ValueError("Eventos ausentes")
    ids = set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Evento inválido")
        identity = event.get("id")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError("Identidade do evento inválida")
        ids.add(identity)
        if not isinstance(event.get("teamKey"), str) or event["teamKey"] not in keys:
            raise ValueError("Equipe do evento desconhecida")
        if not isinstance(event.get("type"), str) or not event["type"]:
            raise ValueError("Tipo do evento ausente")
        timestamp(event.get("occurredAt"))
    return data


def publish_projection(path, data, company):
    """Valida antes da troca atômica, preservando a última saída em falhas."""
    from coletor.storage_io import write_json
    validate_projection(data, company)
    write_json(path, data)
