"""Limite operacional de turno, independente da presença na coleta."""

from copy import deepcopy
from datetime import datetime, timedelta


MAX_SHIFT = timedelta(hours=18)
LIMIT_REASON = "LIMITE_18_HORAS"


def shift_expired(document, now):
    shift = (document.get("jornada") or {}).get("turno") or {}
    if shift.get("status") != "ABERTO":
        return False
    try:
        start = datetime.fromisoformat(str(shift.get("inicio")))
        return start.tzinfo is not None and now - start > MAX_SHIFT
    except (TypeError, ValueError):
        return False


def apply_shift_limit(document, now, *, absent=False):
    """Fecha por limite; preserva a OS sem inventar conclusão ou retorno."""
    shift = (document.get("jornada") or {}).get("turno") or {}
    expired = shift_expired(document, now)
    mark_absent = (absent and shift.get("motivoFechamento") == LIMIT_REASON
                   and not shift.get("ausenteNaColeta"))
    if not expired and not mark_absent:
        return document
    result = deepcopy(document)
    journey = result["jornada"]
    shift = journey["turno"]
    if expired:
        end = datetime.fromisoformat(shift["inicio"]) + MAX_SHIFT
        shift.update(status="FECHADO", fim=end.isoformat(), duracaoMinutos=1080,
                     motivoFechamento=LIMIT_REASON, fimEstimado=True,
                     fechamentoObservadoEm=now.isoformat(), ausenteNaColeta=absent)
        for item in journey.get("turnos") or []:
            if item.get("inicio") == shift["inicio"]:
                item.update(shift)
        journey["emIntervalo"] = False
        journey["intervaloAtual"] = None
        orders = result.setdefault("ordensServico", {})
        active = orders.get("atual")
        if active and active not in orders.setdefault("historico", []):
            orders["historico"].append(active)
        orders["atual"] = None
    else:
        shift["ausenteNaColeta"] = True
    result["updatedAt"] = now.isoformat()
    result["version"] = int(result.get("version") or 0) + 1
    return result


def reopen_returning_team(team, previous, now):
    """Só permite substituir um turno expirado por uma abertura com novo T.

    Retorna False quando o fechamento anterior deve ser preservado no merge.
    """
    old = (previous.get("jornada") or {}).get("turno") or {}
    shift = team.get("turno") or {}
    if not (old.get("status") == "FECHADO" and old.get("motivoFechamento") == LIMIT_REASON):
        return True
    try:
        previous_end = datetime.fromisoformat(old["fim"])
        opening = bool(shift.get("aberto") or shift.get("classificacao") == "ABERTO")
        markers = sorted(m["start"] for m in team.get("turno_marcadores_t") or []
                         if isinstance(m.get("start"), (int, float))
                         and previous_end.timestamp() * 1000 < m["start"] <= now.timestamp() * 1000)
        if opening and markers:
            start = datetime.fromtimestamp(markers[0] / 1000, tz=now.tzinfo).isoformat()
            team["turno"] = {
                "aberto": True, "classificacao": "ABERTO", "inicio_iso": start,
                "turnos": [{"status": "ABERTO", "inicio": start, "fim": None}],
            }
            team["estado_consolidado"] = "ABERTO"
            return True
    except (TypeError, ValueError, KeyError):
        pass
    team["turno"] = {
        "aberto": False, "classificacao": "FECHADO", "inicio_iso": old.get("inicio"),
        "fim_iso": old.get("fim"), "turnos": [deepcopy(old)],
    }
    team["estado_consolidado"] = "FECHADO"
    return False


def preserve_closed_shift(document, previous):
    """Mantém o fechamento mesmo que uma OS ativa tente reabrir no merge."""
    result = deepcopy(document)
    old = (previous.get("jornada") or {})["turno"]
    journey = result["jornada"]
    journey["turno"] = deepcopy(old)
    for shift in journey.get("turnos") or []:
        if shift.get("inicio") == old.get("inicio"):
            shift.update(old)
    journey["emIntervalo"] = False
    journey["intervaloAtual"] = None
    orders = result.setdefault("ordensServico", {})
    active = orders.get("atual")
    if active and active not in orders.setdefault("historico", []):
        orders["historico"].append(active)
    orders["atual"] = None
    return result
