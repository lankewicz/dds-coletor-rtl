"""Lógica de negócio para consolidação, deduplicação e mesclagem diária de equipes e serviços."""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import typing
from zoneinfo import ZoneInfo

from .equipes import normalize_team_key
from .parser import _eh_protocolo_valido
from .storage_io import LOCAL_TZ, company_key, _safe_team_key


def _iso_local(value: typing.Any, day: str | None = None) -> str | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)) and value > 1000000000000:
        return datetime.datetime.fromtimestamp(value / 1000, LOCAL_TZ).isoformat()
    raw = str(value).strip()
    if len(raw) == 5 and raw[2] == ":" and day:
        try:
            return datetime.datetime.fromisoformat(f"{day}T{raw}:00").replace(tzinfo=LOCAL_TZ).isoformat()
        except ValueError:
            return None
    if len(raw) == 8 and raw[2] == ":" and raw[5] == ":" and day:
        try:
            return datetime.datetime.fromisoformat(f"{day}T{raw}").replace(tzinfo=LOCAL_TZ).isoformat()
        except ValueError:
            return None
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.astimezone(LOCAL_TZ).isoformat()
    except ValueError:
        return None


def _extrair_data_base(service: dict[str, typing.Any], day: str) -> str:
    for field in ("inicioIso", "inicio_iso", "fimIso", "fim_iso"):
        val = str(service.get(field) or "").strip()
        if len(val) >= 10 and val[4] == "-" and val[7] == "-":
            try:
                datetime.date.fromisoformat(val[:10])
                return val[:10]
            except ValueError:
                pass

    for field in ("inicio_ms", "timestampMs", "start"):
        val = service.get(field)
        if isinstance(val, (int, float)) and val > 1000000000000:
            dt = datetime.datetime.fromtimestamp(val / 1000, LOCAL_TZ)
            return dt.date().isoformat()

    hora_str = str(service.get("inicioDeslocamento") or service.get("inicioExecucao") or "").strip()
    if len(hora_str) == 5 and hora_str[2] == ":":
        try:
            ref_dt = datetime.datetime.fromisoformat(f"{day}T{hora_str}:00").replace(tzinfo=LOCAL_TZ)
            agora = datetime.datetime.now(LOCAL_TZ)
            if day == agora.date().isoformat() and ref_dt > agora + datetime.timedelta(minutes=15):
                ontem = (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()
                return ontem
        except Exception:
            pass

    return day


def _formatar_horarios_servico(service: dict[str, typing.Any], base_day: str) -> dict[str, str | None]:
    raw_desloc = service.get("inicioDeslocamento") or service.get("inicioIso")
    raw_exec = service.get("inicioExecucao") or service.get("inicioIso")
    raw_fim = service.get("termino") or service.get("fimIso") or service.get("fimExecucao")
    raw_retorno = service.get("retorno")

    cur_day = datetime.date.fromisoformat(base_day)
    prev_dt: datetime.datetime | None = None

    result: dict[str, str | None] = {
        "inicioDeslocamento": None,
        "inicioExecucao": None,
        "fimExecucao": None,
        "retorno": None,
    }

    for key, raw_val in [
        ("inicioDeslocamento", raw_desloc),
        ("inicioExecucao", raw_exec),
        ("fimExecucao", raw_fim),
        ("retorno", raw_retorno),
    ]:
        if not raw_val or str(raw_val).strip() in ("", "-"):
            continue

        if isinstance(raw_val, (int, float)) and raw_val > 1000000000000:
            dt_val = datetime.datetime.fromtimestamp(raw_val / 1000, LOCAL_TZ)
            prev_dt = dt_val
            result[key] = dt_val.isoformat()
            continue

        raw_str = str(raw_val).strip()
        dt_val: datetime.datetime | None = None

        if len(raw_str) == 5 and raw_str[2] == ":":
            try:
                candidate = datetime.datetime.fromisoformat(f"{cur_day.isoformat()}T{raw_str}:00").replace(tzinfo=LOCAL_TZ)
                if prev_dt and prev_dt.hour >= 21 and int(raw_str[:2]) < 6:
                    cur_day = cur_day + datetime.timedelta(days=1)
                    candidate = datetime.datetime.fromisoformat(f"{cur_day.isoformat()}T{raw_str}:00").replace(tzinfo=LOCAL_TZ)
                dt_val = candidate
            except ValueError:
                dt_val = None
        elif len(raw_str) == 8 and raw_str[2] == ":" and raw_str[5] == ":":
            try:
                candidate = datetime.datetime.fromisoformat(f"{cur_day.isoformat()}T{raw_str}").replace(tzinfo=LOCAL_TZ)
                if prev_dt and prev_dt.hour >= 21 and int(raw_str[:2]) < 6:
                    cur_day = cur_day + datetime.timedelta(days=1)
                    candidate = datetime.datetime.fromisoformat(f"{cur_day.isoformat()}T{raw_str}").replace(tzinfo=LOCAL_TZ)
                dt_val = candidate
            except ValueError:
                dt_val = None
        else:
            try:
                parsed = datetime.datetime.fromisoformat(raw_str.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=LOCAL_TZ)
                dt_val = parsed.astimezone(LOCAL_TZ)
            except ValueError:
                dt_val = None

        if dt_val:
            prev_dt = dt_val
            result[key] = dt_val.isoformat()

    if result["inicioDeslocamento"] and result["inicioExecucao"]:
        if result["inicioExecucao"] < result["inicioDeslocamento"]:
            result["inicioExecucao"] = result["inicioDeslocamento"]

    if result["inicioExecucao"] and result["fimExecucao"]:
        if result["fimExecucao"] < result["inicioExecucao"]:
            result["fimExecucao"] = result["inicioExecucao"]

    if result["fimExecucao"] and result["retorno"]:
        if result["retorno"] < result["fimExecucao"]:
            result["retorno"] = result["fimExecucao"]

    return result


def _service_id(team_key: str, service: dict[str, typing.Any]) -> str:
    existing = str(service.get("serviceId") or "").strip()
    if existing:
        if not existing.startswith(team_key + "_"):
            raise ValueError("serviceId pertence a outra equipe")
        return existing
    identity = "|".join(
        str(value or "")
        for value in (
            team_key,
            service.get("inicioIso") or service.get("inicioDeslocamento") or service.get("inicioExecucao"),
        )
    )
    return f"{team_key}_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def compact_service(team_key: str, day: str, service: dict[str, typing.Any]) -> dict[str, typing.Any]:
    lat = service.get("latitude")
    lon = service.get("longitude")
    if lat is None and isinstance(service.get("geolocalizacao"), dict):
        lat = service["geolocalizacao"].get("latitude")
        lon = service["geolocalizacao"].get("longitude")

    fila_conclusao = service.get("filaNaConclusao")
    if isinstance(fila_conclusao, dict):
        fila_conclusao = {
            "emergencia": int(fila_conclusao.get("emergencia") or 0),
            "comercial": int(fila_conclusao.get("comercial") or 0),
        }

    status_atual = service.get("status") or service.get("statusAtual")
    base_day = _extrair_data_base(service, day)
    horarios = _formatar_horarios_servico(service, base_day)

    result = {
        "categoria": service.get("categoria"),
        "tipo": service.get("tipo"),
        "protocolo": service.get("protocolo") or service.get("ssId") or None,
        "inicioDeslocamento": horarios["inicioDeslocamento"],
        "inicioExecucao": horarios["inicioExecucao"],
        "fimExecucao": horarios["fimExecucao"],
        "retorno": horarios["retorno"],
        "latitude": lat,
        "longitude": lon,
        "serviceId": _service_id(team_key, service),
        "statusAtual": status_atual,
        "sequencia": service.get("sequencia") or None,
        "baseDay": base_day,
        "camposEstimados": list(service.get("camposEstimados") or []),
    }
    if fila_conclusao is not None and status_atual == "CONCLUSAO":
        result["filaNaConclusao"] = fila_conclusao
    return result


def _compact_interval(day: str, interval: dict[str, typing.Any]) -> dict[str, typing.Any] | None:
    start = _iso_local(interval.get("inicioIso") or interval.get("inicio"), day)
    if not start and interval.get("inicio_ms"):
        start = _iso_local(int(interval["inicio_ms"]), day)
    if not start:
        return None
    end = _iso_local(interval.get("fimIso") or interval.get("fim"), day)
    if not end and interval.get("fim_ms"):
        end = _iso_local(int(interval["fim_ms"]), day)
    duracao = None
    if start and end:
        try:
            d_start = datetime.datetime.fromisoformat(start)
            d_end = datetime.datetime.fromisoformat(end)
            duracao = int((d_end - d_start).total_seconds() // 60)
        except Exception:
            duracao = None
    return {"inicio": start, "fim": end, "duracaoMinutos": duracao}


def _merge_service_records(team_key: str, records: list[dict[str, typing.Any]]) -> dict[str, dict[str, typing.Any]]:
    result = {}
    keys = {}
    starts = {}
    for incoming in records:
        item = dict(incoming)
        sid = _service_id(team_key, item)
        start = item.get("inicioDeslocamento") or item.get("inicioExecucao")
        prot = str(item.get("protocolo") or "").strip()
        prot_valid = _eh_protocolo_valido(prot)
        key = (team_key, start, prot) if start and prot_valid else None

        target = None
        if key and key in keys:
            target = keys[key]
        elif sid in result:
            target = sid
        elif start and (team_key, start) in starts:
            target = starts[(team_key, start)]
        else:
            target = sid

        old = result.get(target, {})
        old_prot = str(old.get("protocolo") or "").strip()
        old_start = old.get("inicioDeslocamento") or old.get("inicioExecucao")
        if old and old_prot and prot_valid and old_prot != prot:
            suffix = hashlib.sha256(repr((sid, start, prot)).encode()).hexdigest()[:24]
            target = f"{team_key}_{suffix}"
            old = result.get(target, {})

        merged = dict(old)
        old_time = old.get("observadoEm")
        new_time = item.get("observadoEm")
        stale = bool(old_time and new_time and
                     datetime.datetime.fromisoformat(new_time) < datetime.datetime.fromisoformat(old_time))
        for field, value in item.items():
            if field == "filaNaConclusao" and merged.get("filaNaConclusao") is not None:
                continue
            if value is not None and value != "" and (not stale or merged.get(field) in (None, "")):
                merged[field] = value
        if old.get("statusAtual") == "CONCLUSAO" and item.get("statusAtual") in ("EXECUCAO", "DESLOCAMENTO"):
            merged["statusAtual"] = "CONCLUSAO"
        tipos = list(old.get("historicoTipos") or [])
        for tipo in (old.get("tipo"), item.get("tipo")):
            if tipo and tipo not in tipos:
                tipos.append(tipo)
        if tipos:
            merged["historicoTipos"] = tipos
        merged["serviceId"] = target
        result[target] = merged
        if key:
            keys[key] = target
        if start:
            starts[(team_key, start)] = target
    return result


def merge_daily_document(
    previous: dict[str, typing.Any] | None,
    current: dict[str, typing.Any],
    day: str,
) -> dict[str, typing.Any]:
    team_key = _safe_team_key(current.get("teamKey") or current.get("equipe"))
    previous = previous or {}
    if previous.get("teamKey") and previous["teamKey"] != team_key:
        raise ValueError("Historico pertence a outra equipe")
    if previous.get("date") and previous["date"] != day:
        raise ValueError("Historico pertence a outra data")

    # Recupera serviços anteriores (compatível com v2 ordensServico.historico ou v1 services)
    records = []
    if isinstance(previous.get("ordensServico"), dict):
        records = [dict(s) for s in previous["ordensServico"].get("historico", [])]
        if previous["ordensServico"].get("atual"):
            records.append(dict(previous["ordensServico"]["atual"]))
    elif "services" in previous:
        records = [dict(s) for s in previous.get("services", [])]

    existing_concluded_ids = {
        _service_id(team_key, s)
        for s in records
        if isinstance(s, dict) and s.get("statusAtual") == "CONCLUSAO" and s.get("filaNaConclusao") is not None
    }

    for field in ("ssExecutadas", "ssEmAndamento", "services", "bdoList"):
        for raw in current.get(field) or []:
            item = compact_service(team_key, day, raw)
            item["observadoEm"] = current.get("updatedAtIso") or current.get("updatedAt")
            for meta in ("fonteProtocolo", "validacaoProtocolo", "protocoloBruto"):
                if raw.get(meta) is not None:
                    item[meta] = raw[meta]
            if item.get("statusAtual") == "CONCLUSAO" and "filaNaConclusao" not in item:
                sid_check = _service_id(team_key, item)
                if sid_check not in existing_concluded_ids:
                    item["filaNaConclusao"] = {
                        "emergencia": int(current.get("ssPendentesEmergenciaCount") or 0),
                        "comercial": int(current.get("ssPendentesComercialCount") or 0),
                    }
            records.append(item)
    service_map = _merge_service_records(team_key, records)

    # Intervalos anteriores e novos
    prev_intervals = []
    if isinstance(previous.get("jornada"), dict):
        prev_intervals = previous["jornada"].get("intervalos") or []
    elif isinstance(previous.get("turno"), dict):
        prev_intervals = previous["turno"].get("intervalos") or []

    interval_map = {
        str(item.get("inicio")): dict(item)
        for item in prev_intervals
        if isinstance(item, dict) and item.get("inicio")
    }
    raw_intervals = current.get("intervalos") or []
    if not raw_intervals and current.get("intervalo"):
        raw_intervals = [current["intervalo"]]
    for raw_interval in raw_intervals:
        compact_interval = _compact_interval(day, raw_interval)
        if compact_interval:
            previous_interval = interval_map.get(compact_interval["inicio"], {})
            interval_map[compact_interval["inicio"]] = {
                **previous_interval,
                **{key: value for key, value in compact_interval.items() if value is not None},
            }

    turno = current.get("turno") or {}
    services = [
        srv for srv in sorted(service_map.values(), key=lambda item: str(item.get("inicioDeslocamento") or item.get("inicioExecucao") or ""))
        if (srv.get("baseDay") == day or str(srv.get("inicioDeslocamento") or srv.get("inicioExecucao") or "")[:10] == day)
    ]
    activity_raw = current.get("atividadeAtual") or {}
    current_service = compact_service(team_key, day, activity_raw) if activity_raw else None

    # Garantia de unicidade de atividade ativa
    active_in_list = [srv for srv in services if srv.get("statusAtual") in ("EXECUCAO", "DESLOCAMENTO")]
    if active_in_list:
        if current_service is None:
            for srv in active_in_list:
                srv["statusAtual"] = "REDIRECIONADO"
                srv["semExecucaoType"] = "REDIRECIONADO"
                srv["fimExecucao"] = srv.get("fimExecucao") or srv.get("inicioExecucao") or srv.get("inicioDeslocamento")
                srv["retorno"] = srv["fimExecucao"]
        elif len(active_in_list) > 1:
            active_most_recent = max(active_in_list, key=lambda item: str(item.get("inicioDeslocamento") or item.get("inicioExecucao") or ""))
            for srv in active_in_list:
                if srv != active_most_recent:
                    proximo_ini = active_most_recent.get("inicioDeslocamento") or active_most_recent.get("inicioExecucao")
                    srv["statusAtual"] = "REDIRECIONADO"
                    srv["semExecucaoType"] = "REDIRECIONADO"
                    srv["fimExecucao"] = proximo_ini or srv.get("inicioExecucao") or srv.get("inicioDeslocamento")
                    srv["retorno"] = srv["fimExecucao"]

    # Fila de pendências (usada para registrar filaNaAbertura se o turno estiver abrindo)
    fila_total = int(current.get("ssPendentesCount") or 0)
    fila_emergencia = int(current.get("ssPendentesEmergenciaCount") or 0)
    fila_comercial = int(current.get("ssPendentesComercialCount") or 0)

    # Status e horários do turno
    status_turno = "ABERTO" if (turno.get("aberto") or current.get("estadoConsolidado") == "ABERTO" or current_service) else ("FECHADO" if turno.get("classificacao") == "FECHADO" else "FECHADO")
    ini_turno = _iso_local(turno.get("inicio_iso") or turno.get("inicioIso") or turno.get("inicio"), day)
    if not ini_turno and isinstance(previous.get("jornada"), dict):
        ini_turno = _iso_local((previous["jornada"].get("turno") or {}).get("inicio"))
    elif not ini_turno and isinstance(previous.get("turno"), dict):
        ini_turno = _iso_local(previous["turno"].get("inicio"))

    fim_turno = _iso_local(turno.get("fim_iso") or turno.get("fimIso") or turno.get("fim"), day)
    duracao_minutos = None
    if ini_turno and fim_turno:
        try:
            d_ini = datetime.datetime.fromisoformat(ini_turno)
            d_fim = datetime.datetime.fromisoformat(fim_turno)
            duracao_minutos = int((d_fim - d_ini).total_seconds() // 60)
        except Exception:
            duracao_minutos = None

    # Fila na abertura de turno
    prev_fila_abertura = None
    if isinstance(previous.get("jornada"), dict):
        prev_fila_abertura = (previous["jornada"].get("turno") or {}).get("filaNaAbertura")
    elif isinstance(previous.get("turno"), dict):
        prev_fila_abertura = previous["turno"].get("filaNaAbertura")

    if prev_fila_abertura is not None:
        fila_na_abertura = prev_fila_abertura
    elif status_turno == "ABERTO":
        fila_na_abertura = {
            "total": fila_total,
            "emergencia": fila_emergencia,
            "comercial": fila_comercial,
        }
    else:
        fila_na_abertura = None

    # Histórico de serviços concluídos / redirecionados
    historico = [srv for srv in services if srv.get("statusAtual") in ("CONCLUSAO", "REDIRECIONADO", "CANCELADO")]

    prev_version = int(previous.get("version") or (previous.get("current") or {}).get("version") or 0)
    prev_date = str(previous.get("date") or "")
    daily_version = 1 if prev_date != day else (prev_version + 1 if previous else 1)

    # Consolidação dos turnos ocorridos no dia
    turnos_map: dict[str, dict[str, typing.Any]] = {}
    prev_turnos = (previous.get("jornada") or {}).get("turnos") or []
    for t in prev_turnos:
        if isinstance(t, dict):
            k = str(t.get("inicio") or t.get("fim") or "")
            if k:
                turnos_map[k] = dict(t)
    curr_turnos = turno.get("turnos") or []
    for t in curr_turnos:
        if isinstance(t, dict):
            k = str(t.get("inicio") or t.get("fim") or "")
            if k:
                turnos_map[k] = {**turnos_map.get(k, {}), **t}

    if not turnos_map and (ini_turno or fim_turno):
        k = str(ini_turno or fim_turno)
        turnos_map[k] = {
            "tipo": "REGULAR",
            "status": status_turno,
            "inicio": ini_turno,
            "fim": fim_turno,
            "duracaoMinutos": duracao_minutos,
        }
    turnos_consolidados = sorted(turnos_map.values(), key=lambda t: str(t.get("inicio") or t.get("fim") or ""))
    artigo66_info = turno.get("artigo66") or (previous.get("jornada") or {}).get("artigo66")

    return {
        "schemaVersion": 2,
        "company": current.get("empresa") or previous.get("company") or previous.get("empresa"),
        "companyKey": company_key(
            current.get("empresa") or previous.get("company") or previous.get("empresa")
        ),
        "teamKey": team_key,
        "date": day,
        "updatedAt": current.get("updatedAtIso") or current.get("updatedAt"),
        "timezone": str(LOCAL_TZ),
        "version": daily_version,
        "conexao": {
            "isOnline": bool(current.get("isOnline")),
            "status": current.get("statusConexao") or "offline",
            "veiculo": current.get("veiculo") or "",
            "identificadorEquipamento": current.get("identificadorEquipamento") or "",
            "colaborador": current.get("colaborador") or "",
            "origemResolucao": current.get("origemResolucaoEquipe") or "",
        },
        "jornada": {
            "turno": {
                "status": status_turno,
                "inicio": ini_turno,
                "fim": fim_turno,
                "duracaoMinutos": duracao_minutos,
                "filaNaAbertura": fila_na_abertura,
            },
            "turnos": turnos_consolidados,
            "artigo66": artigo66_info,
            "emIntervalo": bool(
                ((current.get("intervalo") or {}).get("em_intervalo") and not (current.get("intervalo") or {}).get("fim_ms") and not (current.get("intervalo") or {}).get("fimIso"))
                or current.get("estadoConsolidado") == "INTERVALO"
            ) and current_service is None,
            "intervalos": sorted(interval_map.values(), key=lambda item: str(item.get("inicio") or "")),
        },
        "ordensServico": {
            "atual": current_service,
            "historico": historico,
        },
    }
