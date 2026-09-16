"""Parser do ROTALOG Tempo Real (/paginas/tempoReal).

Extrai timeline, status de equipes, marcadores de turno (T), intervalos,
ordens de serviço (SSs executadas, em andamento e pendentes) e consolida duplicidades.
"""

from __future__ import annotations

import datetime
import html as html_lib
import logging
import os
import re
import threading
import time
import typing
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests

from .client import CrawlerRotalog, URL_BASE, URL_TEMPO_REAL
from .equipes import normalize_team_key, resolve_team_group, valid_team_key

# Nome mantido para compatibilidade com chamadas existentes.
resolver_equipe_group = resolve_team_group

LOCAL_TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))
logger = logging.getLogger(__name__)

_CLIQUE_EVENTOS_CACHE: dict[tuple[int, int | None, str], dict[str, typing.Any]] = {}
_CLIQUE_CACHE_LOCK = threading.RLock()


def equipe_codigo_valido(value: str | None) -> bool:
    """Rejeita cabeçalhos/placeholders como ``veiculo?`` da timeline."""
    return valid_team_key(value)


def limpar_protocolo(protocolo_raw: str | None) -> str:
    """Remove o sufixo .x.y do protocolo (ex: '20265422950239.2.1' -> '20265422950239')."""
    if not protocolo_raw:
        return ""
    prot = str(protocolo_raw).strip()
    return re.sub(r"\.\d+(\.\d+)?$", "", prot)


def formatar_protocolo_copel(proto_raw: str | None) -> str:
    """Formatador de protocolos da Copel:
    - Comercial: 14 dígitos (Ex: 20265259525570.4.2 -> 20265259525570)
    - Emergencial: 8 dígitos (Ex: 50954710 -> 50954710)
    """
    if not proto_raw:
        return ""
    clean = re.sub(r"\.\d+(\.\d+)?$", "", str(proto_raw).strip())
    m_com = re.search(r"\b(202\d{11})\b", clean)
    if m_com:
        return m_com.group(1)
    m_em = re.search(r"\b(\d{7,8})\b", clean)
    if m_em:
        return m_em.group(1)
    m_any = re.search(r"\b(\d{5,14})\b", clean)
    if m_any:
        return m_any.group(1)
    return ""


def _eh_protocolo_valido(prot: typing.Any) -> bool:
    """Verifica se uma string representa um protocolo real e não um tipo/placeholder de serviço."""
    if not prot:
        return False
    prot_str = str(prot).strip().upper()
    if prot_str in ["UC", "CHAVE", "TRAFO", "ALIM", "ALIMENTADOR", "RISCO", "9901", "196", "NONE", "NULL", ""]:
        return False
    return bool(re.fullmatch(r"\d{7,15}(?:\.\d+)*", prot_str))


def _convert_ms_to_iso(ms: int | str | None) -> str | None:
    if not ms or not str(ms).isdigit():
        return None
    try:
        dt = datetime.datetime.fromtimestamp(int(ms) / 1000.0, tz=LOCAL_TZ)
        return dt.isoformat()
    except Exception:
        return None


def _convert_ms_to_hora(ms: int | str | None) -> str:
    if not ms or not str(ms).isdigit():
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(int(ms) / 1000.0, tz=LOCAL_TZ)
        return dt.strftime("%H:%M")
    except Exception:
        return ""


def parse_group_string(group_raw: str) -> dict[str, str]:
    """Parseia strings de grupo como:
    - 'E3733-CA127 BRUNNO PEDRO (online)'
    - 'E3188- EDIVAN ANDERSON (50 min)'
    """
    group_clean = group_raw.strip()

    status_conexao = ""
    m_status = re.search(r"\(([^()]*)\)$", group_clean)
    if m_status and not m_status.group(1).strip().isdigit():
        status_conexao = m_status.group(1).strip()
        group_clean = re.sub(r"\s*\([^()]*\)$", "", group_clean).strip()

    partes = group_clean.split(" ", 1)
    cod_veic = partes[0] if partes else group_clean
    nome_colab = partes[1].strip() if len(partes) > 1 else ""

    if "-" in cod_veic:
        sub = cod_veic.split("-", 1)
        eq_codigo = sub[0].strip()
        veiculo = sub[1].strip()
    else:
        eq_codigo = cod_veic.strip()
        veiculo = ""

    return {
        "equipe_codigo": eq_codigo,
        "veiculo": veiculo,
        "colaborador": nome_colab,
        "status_conexao": status_conexao,
        "is_online": "online" in status_conexao.lower(),
    }


def _obter_inicio_dia_operacional_ms(now: datetime.datetime | None = None) -> int:
    """Retorna o timestamp em ms da mudança do dia (meia-noite 00:00:00)."""
    now = (now or datetime.datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    meia_noite = datetime.datetime.combine(now.date(), datetime.time(0, 0, 0), tzinfo=LOCAL_TZ)
    return int(meia_noite.timestamp() * 1000)


def consolidar_turno_por_contexto(
    marcadores_t: list[dict[str, typing.Any]],
    eventos_servico_ms: list[int],
    tem_atividade_andamento: bool = False,
    retorno_ultimo_servico_ms: int | None = None,
    now: datetime.datetime | None = None,
) -> dict[str, typing.Any]:
    now = (now or datetime.datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    inicio_dia_ms = _obter_inicio_dia_operacional_ms(now)
    now_ms = int(now.timestamp() * 1000)

    markers = sorted(
        (item for item in marcadores_t if item.get("start")),
        key=lambda item: int(item["start"]),
    )
    all_services = sorted(int(value) for value in eventos_servico_ms if value)
    services_today = [v for v in all_services if v >= inicio_dia_ms]

    result = {
        "aberto": False,
        "inicio_ms": None,
        "inicio_iso": None,
        "fim_ms": None,
        "fim_iso": None,
        "classificacao": "DESCONHECIDO",
    }

    if not markers and not all_services:
        return result

    markers_today = [m for m in markers if int(m["start"]) >= inicio_dia_ms]
    first_marker_today = markers_today[0] if markers_today else None
    last_marker_today = markers_today[-1] if markers_today else None

    # 1. Determina o horário de início do turno
    inicio_ms = None
    if first_marker_today:
        inicio_ms = int(first_marker_today["start"])
    elif services_today:
        inicio_ms = services_today[0]
    elif markers:
        inicio_ms = int(markers[0]["start"])
    elif all_services:
        inicio_ms = all_services[0]

    # 2. Se a equipe tem atividade em andamento, o turno está ABERTO
    if tem_atividade_andamento:
        result.update({
            "aberto": True,
            "inicio_ms": inicio_ms,
            "inicio_iso": _convert_ms_to_iso(inicio_ms) if inicio_ms else None,
            "fim_ms": None,
            "fim_iso": None,
            "classificacao": "ABERTO",
        })
        return result

    fim_servico_ms = retorno_ultimo_servico_ms if retorno_ultimo_servico_ms else (services_today[-1] if services_today else (all_services[-1] if all_services else None))

    # 3. Verifica se há marcador T de Fechamento de Turno
    # Um marcador T é considerado fechamento se ocorreu após os serviços executados
    # e pelo menos 1 hora após a abertura do turno
    tem_fechamento_t = False
    fim_fechamento_ms = None

    if last_marker_today and first_marker_today:
        t_last = int(last_marker_today["start"])
        t_first = int(first_marker_today["start"])
        if t_last - t_first >= 3600 * 1000:
            if not fim_servico_ms or t_last >= (fim_servico_ms - 5 * 60 * 1000):
                tem_fechamento_t = True
                fim_fechamento_ms = t_last

    if tem_fechamento_t:
        result.update({
            "aberto": False,
            "inicio_ms": inicio_ms,
            "inicio_iso": _convert_ms_to_iso(inicio_ms) if inicio_ms else None,
            "fim_ms": fim_fechamento_ms,
            "fim_iso": _convert_ms_to_iso(fim_fechamento_ms) if fim_fechamento_ms else None,
            "classificacao": "FECHADO",
        })
        return result

    # 4. Se não há marcador de fechamento: verificar inatividade prolongada
    if fim_servico_ms:
        tempo_sem_servico_ms = now_ms - fim_servico_ms
        hora_atual_local = now.hour

        fechar_diurno_20h = (hora_atual_local >= 20 and tempo_sem_servico_ms >= 2 * 3600 * 1000)
        fechar_inatividade_longa = (tempo_sem_servico_ms >= int(3.5 * 3600 * 1000) and hora_atual_local >= 19)

        if fechar_diurno_20h or fechar_inatividade_longa:
            result.update({
                "aberto": False,
                "inicio_ms": inicio_ms,
                "inicio_iso": _convert_ms_to_iso(inicio_ms) if inicio_ms else None,
                "fim_ms": fim_servico_ms,
                "fim_iso": _convert_ms_to_iso(fim_servico_ms) if fim_servico_ms else None,
                "classificacao": "FECHADO",
            })
            return result

    # 5. Caso contrário, se o turno foi aberto hoje e não foi fechado, permanece ABERTO!
    if inicio_ms:
        result.update({
            "aberto": True,
            "inicio_ms": inicio_ms,
            "inicio_iso": _convert_ms_to_iso(inicio_ms),
            "fim_ms": None,
            "fim_iso": None,
            "classificacao": "ABERTO",
        })
        return result

    result["classificacao"] = "DESCONHECIDO"
    result["aberto"] = False
    return result


def _service_merge_key(service: dict[str, typing.Any]) -> tuple[typing.Any, ...]:
    return (
        service.get("status"),
        service.get("tipo"),
        service.get("protocolo") or service.get("protocoloBruto"),
        service.get("sequencia"),
        service.get("inicioIso"),
        service.get("fimIso"),
    )


def _merge_service_lists(target: list[dict[str, typing.Any]], incoming: list[dict[str, typing.Any]]) -> None:
    known = {_service_merge_key(item) for item in target}
    for item in incoming:
        key = _service_merge_key(item)
        if key not in known:
            target.append(item)
            known.add(key)


def consolidar_equipes_duplicadas(equipes: list[dict[str, typing.Any]]) -> list[dict[str, typing.Any]]:
    consolidadas: dict[str, dict[str, typing.Any]] = {}
    duplicadas: dict[str, int] = {}
    for equipe in equipes:
        codigo = normalize_team_key(equipe.get("equipe_codigo"))
        if not valid_team_key(codigo):
            continue
        equipe["equipe_codigo"] = codigo
        atual = consolidadas.get(codigo)
        if atual is None:
            consolidadas[codigo] = equipe
            continue

        duplicadas[codigo] = duplicadas.get(codigo, 1) + 1
        grupos = [part.strip() for part in str(atual.get("group_raw") or "").split(" | ") if part.strip()]
        novo_grupo = str(equipe.get("group_raw") or "").strip()
        if novo_grupo and novo_grupo not in grupos:
            grupos.append(novo_grupo)
        atual["group_raw"] = " | ".join(grupos)
        atual["is_online"] = bool(atual.get("is_online") or equipe.get("is_online"))
        for field in ("veiculo", "identificador_equipamento", "origem_resolucao"):
            if not atual.get(field) and equipe.get(field):
                atual[field] = equipe[field]
        colaboradores = [part.strip() for part in str(atual.get("colaborador") or "").split(" / ") if part.strip()]
        novo_colaborador = str(equipe.get("colaborador") or "").strip()
        if novo_colaborador and novo_colaborador not in colaboradores:
            colaboradores.append(novo_colaborador)
        atual["colaborador"] = " / ".join(colaboradores)

        for field in ("turno_marcadores_t", "eventos_servico_ms"):
            atual.setdefault(field, []).extend(equipe.get(field) or [])
        for field in ("bdo_list", "ss_executadas", "ss_em_andamento", "ss_pendentes"):
            _merge_service_lists(atual.setdefault(field, []), equipe.get(field) or [])
        intervalos = {item.get("inicio_ms"): dict(item) for item in atual.get("intervalos", []) if item.get("inicio_ms")}
        for item in equipe.get("intervalos") or []:
            key = item.get("inicio_ms")
            if key:
                intervalos[key] = {**intervalos.get(key, {}), **{k: v for k, v in item.items() if v is not None}}
        atual["intervalos"] = sorted(intervalos.values(), key=lambda item: item.get("inicio_ms") or 0)

        inicio_atual = atual.get("intervalo", {}).get("inicio_ms") or 0
        inicio_novo = equipe.get("intervalo", {}).get("inicio_ms") or 0
        if inicio_novo > inicio_atual:
            atual["intervalo"] = equipe["intervalo"]
        atividades = atual.get("ss_em_andamento") or []
        if atividades:
            atual["atividade_atual"] = max(
                atividades,
                key=lambda item: str(item.get("inicioIso") or ""),
            )

    return list(consolidadas.values())


def _servico_precisa_detalhes(srv: dict[str, typing.Any]) -> bool:
    if not _eh_protocolo_valido(srv.get("protocolo")):
        return True
    if srv.get("latitude") is None:
        return True
    if not srv.get("inicioDeslocamento") and not srv.get("inicioExecucao") and not srv.get("inicioIso"):
        return True
    status = srv.get("status") or srv.get("statusAtual")
    if status == "CONCLUSAO" and not (srv.get("termino") or srv.get("retorno") or srv.get("fimIso")):
        return True
    return False


def _enriquecer_com_snapshot_anterior(
    equipes: list[dict[str, typing.Any]],
    snapshots: dict[str, dict[str, typing.Any]],
) -> int:
    if not equipes or not snapshots:
        return 0

    resolvidos = 0
    campos = (
        "protocolo",
        "protocoloBruto",
        "ssId",
        "categoria",
        "sequencia",
        "tipo",
        "latitude",
        "longitude",
        "geolocalizacao",
        "inicioDeslocamento",
        "inicioExecucao",
    )
    for eq in equipes:
        equipe_codigo = str(eq.get("equipe_codigo") or "").strip().upper()
        team_key = normalize_team_key(equipe_codigo)
        anterior = snapshots.get(team_key) or snapshots.get(equipe_codigo) or next(
            (doc for key, doc in snapshots.items() if normalize_team_key(key) == team_key), {}
        )

        # Compatível com Schema v2 (ordensServico) e Schema v1 (ssExecutadas/ssEmAndamento)
        os_section = anterior.get("ordensServico") or {}
        servicos_anteriores = list(anterior.get("ssExecutadas") or []) + list(anterior.get("ssEmAndamento") or [])
        if os_section.get("historico"):
            servicos_anteriores.extend(os_section["historico"])
        if os_section.get("atual"):
            servicos_anteriores.append(os_section["atual"])

        if not servicos_anteriores:
            continue

        for srv in (eq.get("ss_executadas") or []) + (eq.get("ss_em_andamento") or []):
            inicio_srv = str(srv.get("inicioIso") or srv.get("inicioDeslocamento") or srv.get("inicioExecucao") or "")[:16]
            tipo_srv = str(srv.get("tipo") or "").strip().upper()
            if not inicio_srv:
                continue

            candidatos = []
            for item in servicos_anteriores:
                inicio_item = str(item.get("inicioDeslocamento") or item.get("inicioIso") or item.get("inicioExecucao") or "")[:16]
                if not inicio_item or inicio_item != inicio_srv:
                    continue
                if not _eh_protocolo_valido(item.get("protocolo")):
                    continue
                tipo_item = str(item.get("tipo") or "").strip().upper()
                if not tipo_srv or not tipo_item or tipo_srv in tipo_item or tipo_item in tipo_srv:
                    candidatos.append(item)

            if not candidatos:
                continue

            anterior_srv = candidatos[0]
            if (_eh_protocolo_valido(srv.get("protocolo"))
                    and srv["protocolo"] != anterior_srv.get("protocolo")):
                continue

            for campo in campos:
                valor = anterior_srv.get(campo)
                missing = srv.get(campo) in (None, "")
                estimated = campo in srv.get("camposEstimados", [])
                if campo == "tipo" and valor and srv.get("tipo"):
                    if str(srv["tipo"]).strip().upper() in str(valor).strip().upper():
                        missing = True
                if (missing or estimated) and valor not in (None, ""):
                    srv[campo] = valor
                    if campo in srv.get("camposEstimados", []):
                        srv["camposEstimados"].remove(campo)

            for campo in ("termino", "retorno"):
                if srv.get(campo) in (None, "") and anterior_srv.get(campo) not in (None, ""):
                    srv[campo] = anterior_srv.get(campo)

            srv["detalhesStatus"] = srv.get("status")
            srv["fonteProtocolo"] = anterior_srv.get("fonteProtocolo") or "SNAPSHOT_ANTERIOR"
            srv["validacaoProtocolo"] = anterior_srv.get("validacaoProtocolo") or "EQUIPE_INICIO_TIPO_UNICOS"
            resolvidos += 1
    return resolvidos


def _forcar_cliques_timeline_tempo_real(
    session: requests.Session,
    view_state: str,
    raw_items: list[dict[str, typing.Any]],
    event_indices: set[int] | None = None,
) -> dict[int, dict[str, typing.Any]]:
    global _CLIQUE_EVENTOS_CACHE
    service_items = [
        item for item in raw_items
        if (event_indices is None or item.get("idx") in event_indices)
        and (
            "tempoRealExecutado" in item.get("className", "")
            or "tempoRealEmExecucao" in item.get("className", "")
            or "tempoRealEmDeslocamento" in item.get("className", "")
            or "tempoRealPendente" in item.get("className", "")
        )
    ]
    # Prioriza o serviço ativo atual (deslocamento/execução) antes de executados
    service_items.sort(
        key=lambda it: 0 if ("EmExecucao" in it.get("className", "") or "EmDeslocamento" in it.get("className", "")) else 1
    )

    if not service_items or not view_state:
        return {}

    url_tempo_real = URL_TEMPO_REAL
    headers = {
        "Faces-Request": "partial/ajax",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    cliques_by_idx: dict[int, dict[str, typing.Any]] = {}
    items_to_fetch: list[dict[str, typing.Any]] = []

    with _CLIQUE_CACHE_LOCK:
        for item in service_items:
            idx = item["idx"]
            cls = item.get("className", "")
            start_ms = item.get("start")
            end_ms = item.get("end")
            group = item.get("group", "")
            is_executado = "tempoRealExecutado" in cls

            cache_key = (start_ms, end_ms, group, item.get("content"))
            if is_executado and cache_key in _CLIQUE_EVENTOS_CACHE:
                cached = dict(_CLIQUE_EVENTOS_CACHE[cache_key])
                cached["eventIdx"] = idx
                cliques_by_idx[idx] = cached
            else:
                items_to_fetch.append(item)

    if not items_to_fetch:
        return cliques_by_idx

    def _fetch_event_popup(item: dict[str, typing.Any]):
        idx = item["idx"]
        cls = item.get("className", "")
        start_ms = item.get("start")
        end_ms = item.get("end")
        group = item.get("group", "")
        is_executado = "tempoRealExecutado" in cls
        cache_key = (start_ms, end_ms, group, item.get("content")) if is_executado else None

        payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": "form:cm-patientregistry-facesheet-timeline",
            "javax.faces.partial.execute": "form:cm-patientregistry-facesheet-timeline",
            "javax.faces.partial.render": "form:panelAtualizacaoMapa",
            "javax.faces.behavior.event": "select",
            "javax.faces.partial.event": "select",
            "form:cm-patientregistry-facesheet-timeline_eventIdx": str(idx),
            "form": "form",
            "javax.faces.ViewState": view_state,
        }
        try:
            r = session.post(url_tempo_real, data=payload, headers=headers, verify=False, timeout=8)
            if r.status_code == 200:
                popups = re.findall(
                    r"voarParaCoordenadaZoom\(\s*\[(-?\d+\.\d+),\s*(-?\d+\.\d+)\],\s*\d+,\s*['\"](.*?)['\"]\s*\)",
                    r.text,
                    re.DOTALL,
                )
                for lat, lng, popup in popups:
                    p_clean = popup.replace(r"\'", "'").replace(r"\n", " ").replace("<BR />", "\n").replace("<br />", "\n")
                    m_prot = re.search(r"Protocolo[\s:-]*([0-9\.]+)", p_clean, re.IGNORECASE)
                    m_seq = re.search(r"Sequ[eê]ncia[\s:-]*([^\n]+)", p_clean, re.IGNORECASE)
                    m_status = re.search(r"Status[\s:-]*([^\n]+)", p_clean, re.IGNORECASE)
                    m_tipo = re.search(r"Tipo[\s:-]*([^\n]+)", p_clean, re.IGNORECASE)
                    m_cat = re.search(r"Categoria[\s:-]*([^\n]+)", p_clean, re.IGNORECASE)
                    m_desl = re.search(r"In[ií]cio\s+Deslocamento[\s:-]*(\d{2}:\d{2})", p_clean, re.IGNORECASE)
                    m_exec = re.search(r"In[ií]cio\s+Execu[çc][ãa]o[\s:-]*(\d{2}:\d{2})", p_clean, re.IGNORECASE)
                    m_term = re.search(r"T[eé]rmino[\s:-]*(\d{2}:\d{2})", p_clean, re.IGNORECASE)
                    m_ret = re.search(r"Retorno[\s:-]*(\d{2}:\d{2})", p_clean, re.IGNORECASE)

                    prot_raw = m_prot.group(1).strip() if m_prot else None
                    clean_prot = formatar_protocolo_copel(prot_raw) if prot_raw else None

                    ini_desl_val = m_desl.group(1) if m_desl else None
                    ini_exec_val = m_exec.group(1) if m_exec else None
                    if ini_desl_val and ini_exec_val and ini_exec_val < ini_desl_val:
                        ini_exec_val = ini_desl_val

                    data = {
                        "eventIdx": idx,
                        "protocolo": clean_prot or prot_raw,
                        "protocoloBruto": prot_raw,
                        "sequencia": m_seq.group(1).strip() if m_seq else None,
                        "status": m_status.group(1).strip() if m_status else None,
                        "tipo": m_tipo.group(1).strip() if m_tipo else item.get("content", ""),
                        "categoria": m_cat.group(1).strip() if m_cat else None,
                        "latitude": float(lat),
                        "longitude": float(lng),
                        "inicioDeslocamento": ini_desl_val,
                        "inicioExecucao": ini_exec_val,
                        "termino": m_term.group(1) if m_term else None,
                        "retorno": m_ret.group(1) if m_ret else None,
                        "start_ms": item.get("start"),
                        "end_ms": item.get("end"),
                    }
                    equipe_popup = re.search(r"Equipe[\s:-]*(E[A-Z0-9]{3,7})\b", p_clean, re.IGNORECASE)
                    equipe_item = parse_group_string(group).get("equipe_codigo", "")
                    if equipe_popup and equipe_item:
                        if normalize_team_key(equipe_popup.group(1)) != normalize_team_key(equipe_item):
                            return idx, None, None, False

                    # Validação de compatibilidade de horários
                    start_hora = _convert_ms_to_hora(start_ms)
                    end_hora = _convert_ms_to_hora(end_ms) if isinstance(end_ms, int) else None
                    desl_hora = data.get("inicioDeslocamento")
                    exec_hora = data.get("inicioExecucao")
                    term_hora = data.get("termino")
                    ret_hora = data.get("retorno")

                    def _min_hora(h: str | None) -> int | None:
                        if not h or ":" not in h:
                            return None
                        try:
                            pts = h.split(":")
                            return int(pts[0]) * 60 + int(pts[1])
                        except Exception:
                            return None

                    def _horas_proximas(h1: str | None, h2: str | None, tol: int = 3) -> bool:
                        m1 = _min_hora(h1)
                        m2 = _min_hora(h2)
                        return m1 is not None and m2 is not None and abs(m1 - m2) <= tol

                    hora_valida = (
                        _horas_proximas(start_hora, desl_hora)
                        or _horas_proximas(start_hora, exec_hora)
                        or (end_hora and (_horas_proximas(end_hora, term_hora) or _horas_proximas(end_hora, ret_hora)))
                    )
                    if (desl_hora or exec_hora or term_hora) and not hora_valida:
                        return idx, None, None, False

                    return idx, data, cache_key, is_executado
        except Exception:
            pass
        return idx, None, None, False

    popup_started = time.monotonic()
    for item in items_to_fetch:
        if time.monotonic() - popup_started >= 120:
            logger.warning("Limite de 120s dos popups atingido; detalhes restantes pendentes.")
            break
        try:
            idx, data, cache_key, is_executado = _fetch_event_popup(item)
            if data:
                cliques_by_idx[idx] = data
                if is_executado and cache_key and data.get("protocolo"):
                    with _CLIQUE_CACHE_LOCK:
                        _CLIQUE_EVENTOS_CACHE[cache_key] = data
        except Exception:
            pass

    return cliques_by_idx


def extrair_dados_tempo_real(
    crawler: CrawlerRotalog | None = None,
    max_tentativas: int = 3,
    identificador_para_equipe: dict[str, str] | None = None,
    snapshots_anteriores: dict[str, dict[str, typing.Any]] | None = None,
) -> list[dict[str, typing.Any]]:
    """Extrai e estrutura os dados do /paginas/tempoReal do ROTALOG Copel."""
    if crawler is None:
        crawler = CrawlerRotalog()

    resp = None
    session = None
    for tentativa in range(1, max_tentativas + 1):
        try:
            session = crawler.criar_sessao_autenticada()
            resp = session.get(URL_TEMPO_REAL, verify=False, timeout=60)
            if resp.status_code == 200:
                break
        except Exception as e:
            if tentativa == max_tentativas:
                raise RuntimeError(f"Erro de conexão com Rotalog após {max_tentativas} tentativas: {e}")
            time.sleep(2)

    if not resp or resp.status_code != 200:
        raise RuntimeError(f"Falha ao acessar Rotalog Tempo Real: HTTP {resp.status_code if resp else 'No Response'}")

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    scripts = soup.find_all("script")
    timeline_script = ""
    for s in scripts:
        text = s.string or s.text or ""
        if "timelineAlert" in text or 'PrimeFaces.cw("Timeline"' in text:
            timeline_script = text
            break

    if not timeline_script:
        raise RuntimeError("Resposta Copel sem timeline; coleta não confirmada.")

    js_clean = re.sub(r"new Date\((\d+)\)", r"\1", timeline_script)
    pattern = r'\{"start":\s*(\d+)\s*,\s*"end":\s*(\d+|\w+)\s*,\s*"editable":\s*(true|false)\s*,\s*"group":\s*"(.*?)"\s*,\s*"className":\s*"(.*?)"\s*,\s*"content":\s*"(.*?)"\}'

    raw_items = []
    for idx, match in enumerate(re.finditer(pattern, js_clean)):
        start_ms, end_ms, editable, group, class_name, content = match.groups()
        raw_items.append({
            "idx": idx,
            "start": int(start_ms) if start_ms.isdigit() else 0,
            "end": int(end_ms) if end_ms.isdigit() else None,
            "group": group.strip(),
            "className": class_name.strip(),
            "content": content.strip(),
        })

    equipas_map: dict[str, dict[str, typing.Any]] = {}
    if not raw_items:
        raise RuntimeError("Timeline sem eventos reconhecidos; preservar último snapshot.")

    for item in raw_items:
        group_raw = item["group"]
        if group_raw not in equipas_map:
            meta = resolver_equipe_group(
                parse_group_string(group_raw), identificador_para_equipe
            )
            if not equipe_codigo_valido(meta["equipe_codigo"]):
                continue
            equipas_map[group_raw] = {
                "group_raw": group_raw,
                "equipe_codigo": meta["equipe_codigo"],
                "veiculo": meta["veiculo"],
                "colaborador": meta["colaborador"],
                "status_conexao": meta["status_conexao"],
                "is_online": meta["is_online"],
                "origem_resolucao": meta.get("origem_resolucao"),
                "identificador_equipamento": meta.get("identificador_equipamento"),
                "turno_marcadores_t": [],
                "turno": {"aberto": False, "inicio_ms": None, "inicio_iso": None, "fim_ms": None, "fim_iso": None},
                "intervalo": {"em_intervalo": False, "inicio_ms": None, "fim_ms": None},
                "intervalos": [],
                "atividade_atual": None,
                "bdo_list": [],
                "ss_executadas": [],
                "ss_em_andamento": [],
                "ss_pendentes": [],
                "eventos_servico_ms": [],
            }

        eq_dict = equipas_map.get(group_raw)
        if eq_dict is None:
            continue
        cls = item["className"]
        cnt = item["content"]

        # 1. Marcador de Turno "T"
        if cnt == "T" and cls == "tempoRealMacro":
            eq_dict["turno_marcadores_t"].append({
                "start": item["start"],
                "end": item["end"],
            })

        # 2. Marcador de Intervalo
        elif cnt == "INTERVALO" or "intervalo" in cls.lower():
            interval_record = {
                "inicio_ms": item["start"],
                "inicioIso": _convert_ms_to_iso(item["start"]),
                "fim_ms": item["end"] if isinstance(item["end"], int) else None,
                "fimIso": _convert_ms_to_iso(item["end"]) if isinstance(item["end"], int) else None,
            }
            existing_interval = next((value for value in eq_dict["intervalos"] if value.get("inicio_ms") == item["start"]), None)
            if existing_interval:
                existing_interval.update({key: value for key, value in interval_record.items() if value is not None})
            else:
                eq_dict["intervalos"].append(interval_record)
            is_active_interval = interval_record["fim_ms"] is None
            inicio_atual = eq_dict["intervalo"].get("inicio_ms")
            if not isinstance(inicio_atual, int) or item["start"] >= inicio_atual:
                eq_dict["intervalo"].update({"em_intervalo": is_active_interval, **interval_record})

        # 3. SSs Executadas (BDO)
        elif "Executado" in cls:
            eq_dict["eventos_servico_ms"].append(item["start"])
            hora_inicio = _convert_ms_to_hora(item["start"])
            hora_fim = _convert_ms_to_hora(item["end"]) if isinstance(item["end"], int) else ""
            prot_real = formatar_protocolo_copel(cnt)

            ss_item = {
                "eventIdx": item.get("idx"),
                "ssId": prot_real,
                "protocolo": prot_real,
                "protocoloBruto": cnt,
                "categoria": "EMERGENCIA" if "Emergencia" in cls else "COMERCIAL",
                "tipo": cnt,
                "status": "CONCLUSAO",
                "camposEstimados": ["inicioDeslocamento", "retorno"],
                "inicioDeslocamento": hora_inicio,
                "inicioExecucao": hora_inicio,
                "termino": hora_fim,
                "retorno": hora_fim,
                "sequencia": "",
                "latitude": None,
                "longitude": None,
                "geolocalizacao": None,
                "inicio_ms": item["start"],
                "fim_ms": item["end"] if isinstance(item["end"], int) else None,
                "start": item["start"],
                "end": item["end"],
                "end_ms": item["end"] if isinstance(item["end"], int) else None,
                "inicioIso": _convert_ms_to_iso(item["start"]),
                "fimIso": _convert_ms_to_iso(item["end"]) if isinstance(item["end"], int) else None,
                "transitions": [
                    {"status": "EXECUCAO", "timestampMs": item["start"], "hora": hora_inicio},
                    {"status": "CONCLUSAO", "timestampMs": item["end"] if isinstance(item["end"], int) else item["start"], "hora": hora_fim},
                ],
            }
            eq_dict["ss_executadas"].append(ss_item)
            eq_dict["bdo_list"].append(ss_item)

        # 4. SSs em Deslocamento ou Execução
        elif "EmDeslocamento" in cls or "EmExecucao" in cls:
            eq_dict["eventos_servico_ms"].append(item["start"])
            status_str = "DESLOCAMENTO" if "EmDeslocamento" in cls else "EXECUCAO"
            hora_inicio = _convert_ms_to_hora(item["start"])
            prot_real = formatar_protocolo_copel(cnt)

            ss_andamento = {
                "eventIdx": item.get("idx"),
                "ssId": prot_real,
                "protocolo": prot_real,
                "protocoloBruto": cnt,
                "status": status_str,
                "camposEstimados": ["inicioDeslocamento"] if status_str == "EXECUCAO" else [],
                "categoria": "EMERGENCIA" if "Emergencia" in cls else "COMERCIAL",
                "tipo": cnt,
                "inicioDeslocamento": hora_inicio,
                "inicioExecucao": hora_inicio if status_str == "EXECUCAO" else "",
                "termino": "",
                "retorno": "",
                "sequencia": "",
                "latitude": None,
                "longitude": None,
                "geolocalizacao": None,
                "inicio_ms": item["start"],
                "start": item["start"],
                "inicioIso": _convert_ms_to_iso(item["start"]),
                "inicioHora": hora_inicio,
                "transitions": [
                    {"status": status_str, "timestampMs": item["start"], "hora": hora_inicio},
                ],
            }
            eq_dict["ss_em_andamento"].append(ss_andamento)
            eq_dict["atividade_atual"] = ss_andamento
            eq_dict["bdo_list"].append(ss_andamento)

        # 5. SSs Pendentes (Fila)
        elif "Pendente" in cls:
            eq_dict["ss_pendentes"].append({
                "sequencia": cnt,
                "tipo": "EMERGENCIA" if "Emergencia" in cls else "COMERCIAL",
            })

    resultado = consolidar_equipes_duplicadas(list(equipas_map.values()))

    # Enriquecimento com snapshot anterior
    _enriquecer_com_snapshot_anterior(resultado, snapshots_anteriores or {})

    hoje_local = datetime.datetime.now(LOCAL_TZ).date()
    data_minima_tempo_real = hoje_local - datetime.timedelta(days=1)

    def _servico_recente_sem_protocolo(srv: dict[str, typing.Any]) -> bool:
        if not _servico_precisa_detalhes(srv) or not srv.get("inicioIso"):
            return False
        try:
            data_servico = (
                datetime.datetime.fromisoformat(srv["inicioIso"])
                .astimezone(LOCAL_TZ)
                .date()
            )
        except (TypeError, ValueError):
            return False
        return data_minima_tempo_real <= data_servico <= hoje_local

    indices_sem_protocolo = {
        srv.get("eventIdx")
        for eq in resultado
        for srv in (eq.get("ss_executadas", []) + eq.get("ss_em_andamento", []))
        if srv.get("eventIdx") is not None and _servico_recente_sem_protocolo(srv)
    }

    # Consulta individual aos popups
    try:
        vs_input = soup.find("input", {"name": "javax.faces.ViewState"})
        view_state = vs_input["value"] if vs_input and vs_input.get("value") else None
        if session and view_state and raw_items:
            cliques_by_idx = _forcar_cliques_timeline_tempo_real(
                session, view_state, raw_items, event_indices=indices_sem_protocolo
            )
            if cliques_by_idx:
                for eq in resultado:
                    for srv in (eq.get("ss_executadas", []) + eq.get("ss_em_andamento", [])):
                        ev_idx = srv.get("eventIdx")
                        if ev_idx is not None and ev_idx in cliques_by_idx:
                            popup_data = cliques_by_idx[ev_idx]
                            if popup_data.get("protocolo"):
                                srv["protocolo"] = popup_data["protocolo"]
                                srv["protocoloBruto"] = popup_data["protocoloBruto"]
                                srv["ssId"] = popup_data["protocolo"]
                            if popup_data.get("categoria"):
                                srv["categoria"] = popup_data["categoria"]
                            if popup_data.get("tipo"):
                                srv["tipo"] = popup_data["tipo"]
                            if popup_data.get("sequencia"):
                                srv["sequencia"] = popup_data["sequencia"]
                            if popup_data.get("latitude") is not None:
                                srv["latitude"] = popup_data["latitude"]
                                srv["longitude"] = popup_data["longitude"]
                                srv["geolocalizacao"] = {
                                    "latitude": popup_data["latitude"],
                                    "longitude": popup_data["longitude"],
                                }
                            if popup_data.get("inicioDeslocamento"):
                                srv["inicioDeslocamento"] = popup_data["inicioDeslocamento"]
                            if popup_data.get("inicioExecucao"):
                                srv["inicioExecucao"] = popup_data["inicioExecucao"]
                            if popup_data.get("termino"):
                                srv["termino"] = popup_data["termino"]
                            if popup_data.get("retorno"):
                                srv["retorno"] = popup_data["retorno"]
                            srv["detalhesStatus"] = srv.get("status")
                            srv["camposEstimados"] = [
                                f for f in srv.get("camposEstimados", [])
                                if not popup_data.get(f)
                            ]
                            srv["fonteProtocolo"] = "POPUP"
    except Exception as exc:
        logger.warning("Falha ao executar cliques forçados na timeline: %s", exc)

    # Consolidação final do turno
    for eq in resultado:
        eventos_servico_ms = eq.pop("eventos_servico_ms", [])
        tem_andamento = bool(eq.get("ss_em_andamento") or eq.get("atividade_atual"))
        retorno_ultimo_ms = None
        if eq.get("ss_executadas"):
            def _srv_sort_key(srv):
                for k in ("fim_ms", "end", "end_ms"):
                    v = srv.get(k)
                    if isinstance(v, (int, float)) and v > 1000000000000:
                        return int(v)
                for k in ("fimIso", "inicioIso"):
                    v = str(srv.get(k) or "")
                    if len(v) >= 10:
                        try:
                            return int(datetime.datetime.fromisoformat(v).timestamp() * 1000)
                        except Exception:
                            pass
                return 0
            ultimo_executado = max(eq["ss_executadas"], key=_srv_sort_key)
            for k in ("fim_ms", "end", "end_ms"):
                v = ultimo_executado.get(k)
                if isinstance(v, (int, float)) and v > 1000000000000:
                    retorno_ultimo_ms = int(v)
                    break

        eq["turno"] = consolidar_turno_por_contexto(
            eq.get("turno_marcadores_t", []),
            eventos_servico_ms,
            tem_atividade_andamento=tem_andamento,
            retorno_ultimo_servico_ms=retorno_ultimo_ms,
        )

        em_intervalo = bool(eq.get("intervalo", {}).get("em_intervalo"))
        if em_intervalo:
            eq["estado_consolidado"] = "INTERVALO"
        elif tem_andamento:
            eq["estado_consolidado"] = "ABERTO"
        elif eq["turno"]["aberto"]:
            eq["estado_consolidado"] = "ABERTO"
        elif eq["turno"]["classificacao"] == "FECHADO":
            eq["estado_consolidado"] = "FECHADO"
        else:
            eq["estado_consolidado"] = "DESCONHECIDO"

    return resultado
