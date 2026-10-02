"""Módulo para raspagem e consolidação diária/mensal de quilometragem e serviços do ROTALOG.

Gera um ÚNICO arquivo consolidado por dia (dados/rotalog/quilometragem/diario/AAAA-MM-DD.json.gz),
indexado por protocolo e com resumo por equipe, reduzindo drasticamente as gravações no Firebase.
Zero Pandas, otimizado para Orange Pi / Raspberry Pi.
"""

from __future__ import annotations

import calendar
import datetime
import gzip
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time
import typing
import warnings
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from coletor.client import CrawlerRotalog
from coletor.parser import formatar_protocolo_copel
from coletor.equipes import normalize_team_key
from coletor.storage import company_key, load_json, write_json, rotalog_gcs_paths
from coletor.team_registry import TeamRegistry
from coletor.historico_terminal import (
    atualizar_status_terminal,
    formatar_numero_br,
    formatar_resumo_diario_terminal,
    formatar_resumo_mensal_terminal,
)
from coletor.historico_parsing import (
    extrair_data_iso_de_evento as _extrair_data_iso_de_evento,
    parse_float_br,
    parse_iso_datetime as _parse_iso_datetime,
    parse_target_date as _parse_target_date,
)
from coletor.historico_estado import MonthlyCollectionState
from coletor.historico_sync import (
    enqueue_historical_upload as _enqueue_historical_upload,
    flush_historical_uploads as _flush_historical_uploads,
    stable_payload_digest as _stable_payload_digest,
    upload_payload_if_changed as _upload_payload_if_changed,
)

LOG = logging.getLogger("coletor-historico")
TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))

URL_BASE = "https://www.copel.com/rtlweb"
URL_LISTAGEM_EVENTOS = f"{URL_BASE}/paginas/listagemEventos"
URL_EQUIPES = f"{URL_BASE}/paginas/equipes"

# Contratos monitorados para controle de produção / faturamento
CONTRATOS_ALVO: tuple[str, ...] = ("4600026988", "4600025149")


EQUIPES_HEADERS = (
    "Veiculo", "Tablet", "Agencia", "Contrato", "Data Referencia - Turno",
    "Eletricista 1", "Eletricista 2", "Servicos executados",
    "Informado com limitador (km)", "Glosado critico (km)",
    "Glosado por parecer (km)", "Autorizado (km)",
    "Recuperados por Parecer (km)", "Autorizado Final (km)", "Diferenca",
    "Aguardando justificativa", "Em analise", "Alertas",
)


def parse_iso_datetime(dt_str: typing.Any, base_day_iso: str | None = None) -> str | None:
    return _parse_iso_datetime(dt_str, base_day_iso, timezone=TZ)


def extrair_data_iso_de_evento(row: dict[str, typing.Any], default_day: str | None = None) -> str:
    return _extrair_data_iso_de_evento(row, default_day, timezone=TZ)


def parse_target_date(data_str: str | None = None) -> datetime.date:
    return _parse_target_date(data_str, timezone=TZ)


from coletor.historico_scrapers import RotalogEquipesScraper, RotalogEventosScraper



def salvar_arquivo_gzip(path: Path, payload: dict[str, typing.Any]) -> None:
    """Substitui o retrato local atomicamente após concluir a gravação."""
    write_json(path, payload)


from coletor.historico_consolidacao import (
    deduplicar_eventos_consolidados,
    enriquecer_cadastro_permanente_equipes,
    estruturar_quilometragem_diaria,
    estruturar_resumo_equipes,
)



def executar_coleta_historico_dia(
    target_date: datetime.date,
    output_dir: Path,
    empresa: str,
    enable_firebase: bool = False,
    firebase_store: typing.Any = None,
    progresso: typing.Callable[[str], None] | None = None,
    session: requests.Session | None = None,
    atualizar_terminal: bool = True,
) -> dict[str, typing.Any]:
    """Raspa a listagem de eventos (/paginas/listagemEventos) e gera arquivo e relatório diário."""
    day_iso = target_date.isoformat()
    day_br = target_date.strftime("%d/%m/%Y")
    started_at = datetime.datetime.now(TZ)
    phase_started = time.perf_counter()
    phase_durations: dict[str, float] = {}

    LOG.debug("=== Coleta Diária de Eventos e Quilometragem: data %s (Empresa: %s) ===", day_iso, empresa)

    def progresso_handler(sub_msg: str) -> None:
        if atualizar_terminal:
            atualizar_status_terminal(f"[{day_br}] {sub_msg}")
        if progresso:
            progresso(sub_msg)

    if atualizar_terminal:
        atualizar_status_terminal(f"[{day_br}] Consultando listagem de eventos...")
    elif progresso:
        progresso(f"Consultando listagem de eventos para {day_br}")

    scraper = RotalogEventosScraper(session=session)
    eventos = scraper.raspar_periodo(day_br, day_br, progresso=progresso_handler)
    phase_durations["eventosScrapeSeconds"] = round(time.perf_counter() - phase_started, 3)

    # Preserva todas as linhas e colunas retornadas pela listagem para auditoria/reprocessamento.
    raw_payload = {
        "schemaVersion": 1,
        "fonte": "ROTALOG_LISTAGEM_EVENTOS",
        "data": day_iso,
        "empresa": empresa,
        "empresaKey": company_key(empresa),
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "totalRegistros": len(eventos),
        "registros": eventos,
    }
    raw_dir = output_dir / "rotalog" / "eventos" / "diario"
    raw_path = raw_dir / f"{day_iso}.json.gz"
    salvar_arquivo_gzip(raw_path, raw_payload)
    LOG.debug("Arquivo bruto diário de eventos salvo: %s", raw_path)

    phase_started = time.perf_counter()
    equipes = []
    try:
        equipes_scraper = RotalogEquipesScraper(session=session or scraper.session)
        equipes = equipes_scraper.raspar_dia(
            day_br, progresso=progresso_handler, enriquecer_detalhes=True,
        )
    except Exception as exc:
        raise RuntimeError(f"Coleta de equipes incompleta em {day_br}: {exc}") from exc
    phase_durations["equipesScrapeSeconds"] = round(time.perf_counter() - phase_started, 3)

    # 1. Gera o documento consolidado do dia
    phase_started = time.perf_counter()
    payload_km = estruturar_quilometragem_diaria(
        eventos, target_day=day_iso, empresa=empresa, equipes_records=equipes,
    )
    phase_durations["consolidacaoSeconds"] = round(time.perf_counter() - phase_started, 3)

    phase_started = time.perf_counter()
    identity_enrichment = enriquecer_cadastro_permanente_equipes(
        output_dir, equipes, day_iso,
    )
    phase_durations["cadastroEquipesSeconds"] = round(time.perf_counter() - phase_started, 3)

    # 2. Salva localmente em dados-local/rotalog/quilometragem/diario/AAAA-MM-DD.json.gz
    dir_km_local = output_dir / "rotalog" / "quilometragem" / "diario"
    local_km_path = dir_km_local / f"{day_iso}.json.gz"
    phase_started = time.perf_counter()
    salvar_arquivo_gzip(local_km_path, payload_km)
    LOG.debug("Arquivo diário de quilometragem salvo: %s", local_km_path)

    phase_durations["arquivosRelatorioSeconds"] = round(time.perf_counter() - phase_started, 3)

    # 4. Upload de APENAS UM ARQUIVO para o Firebase Storage
    firebase_synced = False
    firebase_raw_synced = False
    firebase_uploaded = False
    firebase_raw_uploaded = False
    upload_errors = []
    historical_queue = (
        output_dir / "rotalog" / "sync" / company_key(empresa) / "historico" / "pending-uploads.json"
    )
    if enable_firebase and (not firebase_store or not firebase_store.enabled):
        upload_errors.append("Firebase solicitado, mas indisponível; arquivos locais preservados")
    if enable_firebase:
        remote_root = (
            firebase_store.root_prefix if firebase_store and firebase_store.enabled
            else rotalog_gcs_paths(empresa, root_prefix=os.getenv("ROTALOG_GCS_ROOT_PREFIX"),
                                  index_blob=os.getenv("ROTALOG_GCS_CACHE_BLOB"))["root"]
        )
        raw_key = f"eventos/{day_iso}"
        km_key = f"quilometragem/{day_iso}"
        _enqueue_historical_upload(
            historical_queue, raw_key, raw_path,
            f"{remote_root}/eventos/diario/{day_iso}.json.gz",
            output_dir / "rotalog" / "sync" / company_key(empresa) / "historico" / "eventos" / f"{day_iso}.json",
        )
        _enqueue_historical_upload(
            historical_queue, km_key, local_km_path,
            f"{remote_root}/quilometragem/diario/{day_iso}.json.gz",
            output_dir / "rotalog" / "sync" / company_key(empresa) / "historico" / "quilometragem" / f"{day_iso}.json",
        )
        sync_result = (
            _flush_historical_uploads(historical_queue, firebase_store)
            if firebase_store and firebase_store.enabled
            else {"results": {}, "pending": len(load_json(historical_queue, {}).get("items", {}))}
        )
        raw_sync_status = sync_result["results"].get(raw_key)
        km_sync_status = sync_result["results"].get(km_key)
        firebase_raw_synced = raw_sync_status in {"uploaded", "unchanged"}
        firebase_synced = km_sync_status in {"uploaded", "unchanged"}
        firebase_raw_uploaded = raw_sync_status == "uploaded"
        firebase_uploaded = km_sync_status == "uploaded"
        if not firebase_raw_synced or not firebase_synced:
            upload_errors.append("envio diário pendente; arquivos locais preservados na fila")

    if atualizar_terminal:
        atualizar_status_terminal(
            f"[{day_br}] Concluído | "
            f"Serviços: {formatar_numero_br(payload_km['totalServicos'], 0)} | "
            f"KM Inf: {formatar_numero_br(payload_km['totalKmInformado'], 2)} | "
            f"KM Aut: {formatar_numero_br(payload_km['totalKmAutorizadoFinal'], 2)}"
        )

    finished_at = datetime.datetime.now(TZ)
    duration_s = round((finished_at - started_at).total_seconds(), 2)

    return {
        "status": "success",
        "syncStatus": "pending" if upload_errors else ("synced" if enable_firebase else "disabled"),
        "date": day_iso,
        "durationSeconds": duration_s,
        "phaseDurations": phase_durations,
        "totalEquipes": payload_km["totalEquipes"],
        "totalServicos": payload_km["totalServicos"],
        "totalKmInformado": payload_km["totalKmInformado"],
        "totalKmAutorizadoFinal": payload_km["totalKmAutorizadoFinal"],
        "totalKmOficialInformado": payload_km["resumoEquipes"]["totais"]["kmInformado"],
        "totalKmOficialAutorizadoFinal": payload_km["resumoEquipes"]["totais"]["kmAutorizadoFinal"],
        "totalKmRecuperadoParecer": payload_km["resumoEquipes"]["totais"]["kmRecuperadoParecer"],
        "localArquivoKm": str(local_km_path),
        "localArquivoEventosBrutos": str(raw_path),
        "firebaseSynced": firebase_synced,
        "identityEnrichment": identity_enrichment,
        "firebaseRawSynced": firebase_raw_synced,
        "firebaseUploaded": firebase_uploaded,
        "firebaseRawUploaded": firebase_raw_uploaded,
        "erroUpload": "; ".join(upload_errors) if upload_errors else None,
        "uploadQueue": sync_result if enable_firebase and firebase_store and firebase_store.enabled else {
            "pending": 0, "totalAttempts": 0, "oldestQueuedAt": None,
            "lastAttemptAt": None, "lastError": None,
        },
    }


def dividir_periodo_em_intervalos(
    dt_inicio: datetime.date,
    dt_fim: datetime.date,
    dias_por_chunk: int = 5,
) -> list[tuple[datetime.date, datetime.date]]:
    """Divide um período de datas em blocos menores (ex: 5 dias cada)."""
    intervalos = []
    atual = dt_inicio
    while atual <= dt_fim:
        proximo = min(atual + datetime.timedelta(days=dias_por_chunk - 1), dt_fim)
        intervalos.append((atual, proximo))
        atual = proximo + datetime.timedelta(days=1)
    return intervalos


def varrer_mes(
    ano: int,
    mes: int,
    output_dir: Path,
    empresa: str,
    enable_firebase: bool = False,
    firebase_store: typing.Any = None,
    progresso: typing.Callable[[str], None] | None = None,
    coletar_diarios: bool = True,
    session: requests.Session | None = None,
    max_tentativas_por_dia: int = 10,
    skip_if_complete: bool = False,
) -> dict[str, typing.Any]:
    """Varre todos os dias do mês na listagemEventos gerando os diários,
    e depois consolida a tela /paginas/equipes no mês gerando o relatório mensal oficial."""
    started_at = datetime.datetime.now(TZ)
    mes_str = f"{ano:04d}-{mes:02d}"
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    dt_inicio = datetime.date(ano, mes, 1)
    dt_fim = datetime.date(ano, mes, ultimo_dia)
    ini_br = dt_inicio.strftime("%d/%m/%Y")
    fim_br = dt_fim.strftime("%d/%m/%Y")

    LOG.debug("=== Iniciando Fechamento Mensal de Equipes e Eventos: %s (%s a %s) ===", mes_str, ini_br, fim_br)

    total_servicos_acumulado = 0
    total_km_inf_acumulado = 0.0
    total_km_aut_acumulado = 0.0
    dias_processados = 0
    state_path = (
        output_dir / "rotalog" / "sync" / company_key(empresa)
        / "historico" / "mensal" / f"{mes_str}-coleta.json"
    )
    monthly_state = MonthlyCollectionState.load(
        state_path, month=mes_str, company_key=company_key(empresa), timezone=TZ,
    )
    historical_queue = (
        output_dir / "rotalog" / "sync" / company_key(empresa) / "historico" / "pending-uploads.json"
    )
    saved_monthly_path = output_dir / "rotalog" / "quilometragem" / "mensal" / f"{mes_str}.json.gz"
    if monthly_state.status == "monthly_upload_pending" and enable_firebase:
        payload_local = load_json(saved_monthly_path, {})
        if payload_local and firebase_store and firebase_store.enabled:
            monthly_key = f"mensal/{mes_str}"
            _enqueue_historical_upload(
                historical_queue, monthly_key, saved_monthly_path,
                f"{firebase_store.root_prefix}/quilometragem/mensal/{mes_str}.json.gz",
                output_dir / "rotalog" / "sync" / company_key(empresa)
                / "historico" / "mensal" / f"{mes_str}.json",
            )
            sync_result = _flush_historical_uploads(historical_queue, firebase_store)
            if sync_result["results"].get(monthly_key) in {"uploaded", "unchanged"}:
                monthly_state.save("complete", year=ano, month=mes, total_days=ultimo_dia)
                return {
                    "status": "success", "month": mes_str, "alreadyCollected": True,
                    "firebaseSynced": True, "diasProcessados": ultimo_dia,
                    "diasTotal": ultimo_dia, "totalRegistros": payload_local.get("totalRegistros", 0),
                    "totalServicos": payload_local.get("totalServicos", 0),
                    "totalKmInformado": payload_local.get("totalKmInformado", 0),
                    "totalKmAutorizadoFinal": payload_local.get("totalKmAutorizadoFinal", 0),
                    "localArquivoMensal": str(saved_monthly_path), "stateFile": str(state_path),
                    "uploadQueue": sync_result,
                }
        return {
            "status": "error", "month": mes_str, "alreadyCollected": True,
            "firebaseSynced": False,
            "erroUpload": "envio mensal ainda pendente; arquivo local preservado",
            "diasProcessados": ultimo_dia, "diasTotal": ultimo_dia,
            "localArquivoMensal": str(saved_monthly_path), "stateFile": str(state_path),
            "uploadQueue": sync_result if 'sync_result' in locals() else None,
        }
    if skip_if_complete and monthly_state.status == "complete":
        return {
            "status": "success",
            "month": mes_str,
            "alreadyComplete": True,
            "diasProcessados": ultimo_dia,
            "diasTotal": ultimo_dia,
            "stateFile": str(state_path),
        }
    completed_days = monthly_state.completed_days if coletar_diarios else set()
    attempts_by_day = monthly_state.attempts_by_day if coletar_diarios else {}
    errors_by_day = monthly_state.errors_by_day if coletar_diarios else {}

    def persist_state(status: str) -> None:
        if not coletar_diarios:
            return
        monthly_state.save(status, year=ano, month=mes, total_days=ultimo_dia)

    crawler_session = session
    if coletar_diarios and crawler_session is None:
        try:
            crawler = CrawlerRotalog()
            crawler_session = crawler.criar_sessao_autenticada()
        except Exception as exc:
            LOG.debug("Sessão autenticada não inicializada no crawler: %s", exc)

    if coletar_diarios:
        pending_dates = [
            datetime.date(ano, mes, dia)
            for dia in range(1, ultimo_dia + 1)
            if datetime.date(ano, mes, dia).isoformat() not in completed_days
        ]
        max_attempts = max(1, int(max_tentativas_por_dia))
        for round_number in range(1, max_attempts + 1):
            if not pending_dates:
                break
            if round_number > 1:
                delay_seconds = min(60 * (2 ** (round_number - 2)), 15 * 60)
                pending_label = ", ".join(day.strftime("%d/%m") for day in pending_dates)
                atualizar_status_terminal(
                    f"[{mes:02d}/{ano:04d}] Aguardando {delay_seconds // 60} min para "
                    f"repetir somente os dias pendentes: {pending_label}"
                )
                time.sleep(delay_seconds)
            if round_number > 1 and session is None:
                try:
                    if crawler_session is not None:
                        crawler_session.close()
                    crawler_session = CrawlerRotalog().criar_sessao_autenticada()
                except Exception as exc:
                    LOG.warning("Não foi possível renovar a sessão antes da tentativa %d: %s", round_number, exc)
            retry_dates = []
            for target_date in pending_dates:
                dia = target_date.day
                day_iso = target_date.isoformat()
                monthly_state.record_attempt(day_iso)
                persist_state("running")
                dia_br = target_date.strftime("%d/%m/%Y")
                pct = int((dia / ultimo_dia) * 100)
                prefix = (
                    f"[{mes:02d}/{ano:04d}] Dia {dia:02d}/{ultimo_dia:02d} ({pct:3d}%) {dia_br} "
                    f"tentativa {round_number}/{max_attempts}"
                )

                def cb_progresso_dia(sub_msg: str) -> None:
                    atualizar_status_terminal(
                        f"{prefix} {sub_msg} | "
                        f"Serviços: {formatar_numero_br(total_servicos_acumulado, 0)} | "
                        f"KM Inf: {formatar_numero_br(total_km_inf_acumulado, 2)} | "
                        f"KM Aut: {formatar_numero_br(total_km_aut_acumulado, 2)}"
                    )

                atualizar_status_terminal(f"{prefix} Coletando...")

                try:
                    res_dia = executar_coleta_historico_dia(
                        target_date=target_date,
                        output_dir=output_dir,
                        empresa=empresa,
                        enable_firebase=enable_firebase,
                        firebase_store=firebase_store,
                        progresso=cb_progresso_dia,
                        session=crawler_session,
                        atualizar_terminal=False,
                    )
                    if res_dia.get("status") != "success":
                        raise RuntimeError(res_dia.get("erroUpload") or "coleta diária incompleta")
                    total_servicos_acumulado += res_dia.get("totalServicos", 0)
                    total_km_inf_acumulado += res_dia.get("totalKmInformado", 0.0)
                    total_km_aut_acumulado += res_dia.get("totalKmAutorizadoFinal", 0.0)
                    dias_processados += 1
                    monthly_state.mark_success(day_iso)
                    persist_state("running")
                    atualizar_status_terminal(f"{prefix} OK")
                except Exception as exc:
                    monthly_state.mark_failure(day_iso, exc)
                    retry_dates.append(target_date)
                    persist_state("retrying")
                    LOG.warning("Falha na coleta do dia %s (tentativa %d/%d): %s", dia_br, round_number, max_attempts, exc)
                    atualizar_status_terminal(f"{prefix} FALHOU; será tentado novamente")
            pending_dates = retry_dates

        if pending_dates:
            persist_state("pending_next_day")
            return {
                "status": "pending",
                "month": mes_str,
                "diasProcessados": len(completed_days),
                "diasTotal": ultimo_dia,
                "diasPendentes": [day.isoformat() for day in pending_dates],
                "tentativasPorDia": attempts_by_day,
                "errosPorDia": errors_by_day,
                "nextRetryDate": (datetime.datetime.now(TZ).date() + datetime.timedelta(days=1)).isoformat(),
                "stateFile": str(state_path),
            }

        atualizar_status_terminal(
            f"[{mes:02d}/{ano:04d}] Consolidando fechamento mensal oficial de equipes ({ini_br} a {fim_br})..."
        )
    elif progresso:
        progresso(f"Iniciando fechamento mensal {mes_str} via tela Equipes")

    # Reconstrói os totais a partir do disco também após retomadas; os contadores
    # desta execução isolada não incluem os dias concluídos anteriormente.
    local_days = []
    for number in range(1, ultimo_dia + 1):
        day_iso = datetime.date(ano, mes, number).isoformat()
        saved = load_json(output_dir / "rotalog" / "quilometragem" / "diario" / f"{day_iso}.json.gz", {})
        if saved.get("data") == day_iso and saved.get("empresaKey") == company_key(empresa):
            local_days.append(saved)
    if local_days:
        total_servicos_acumulado = sum(day.get("totalServicos", 0) for day in local_days)
        total_km_inf_acumulado = sum(day.get("totalKmInformado", 0) for day in local_days)
        total_km_aut_acumulado = sum(day.get("totalKmAutorizadoFinal", 0) for day in local_days)
        dias_processados = len(local_days)

    equipes_scraper = RotalogEquipesScraper(session=crawler_session)
    records = equipes_scraper.raspar_periodo(ini_br, fim_br, progresso=progresso)

    if progresso:
        progresso("Estruturando resumo oficial das equipes por contrato")

    resumo_equipes = estruturar_resumo_equipes(records, CONTRATOS_ALVO)
    emp_key = company_key(empresa)
    official_available = resumo_equipes["totalRegistros"] > 0
    official_team_count = len({
        team_key
        for contract in resumo_equipes["totaisPorContrato"].values()
        for team_key in (contract.get("equipes") or {})
    })

    # Gera a estrutura única do mês
    payload_mensal = {
        "schemaVersion": 3,
        "mes": mes_str,
        "empresa": empresa,
        "empresaKey": emp_key,
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "contratos": list(CONTRATOS_ALVO),
        "periodo": {"inicio": dt_inicio.isoformat(), "fim": dt_fim.isoformat()},
        "fonte": "ROTALOG_EQUIPES_E_EVENTOS" if coletar_diarios else "ROTALOG_EQUIPES",
        "diasProcessados": len(completed_days) if coletar_diarios else dias_processados,
        "diasTotal": ultimo_dia,
        "totalEquipes": official_team_count,
        "totalServicos": resumo_equipes["totais"]["servicosExecutados"] if official_available else total_servicos_acumulado,
        "totalKmInformado": resumo_equipes["totais"]["kmInformado"] if official_available else round(total_km_inf_acumulado, 2),
        "totalKmAutorizadoFinal": resumo_equipes["totais"]["kmAutorizadoFinal"] if official_available else round(total_km_aut_acumulado, 2),
        "totalKmRecuperadoParecer": resumo_equipes["totais"]["kmRecuperadoParecer"],
        "totaisAcumuladosDiarios": {
            "totalServicos": total_servicos_acumulado,
            "totalKmInformado": round(total_km_inf_acumulado, 2),
            "totalKmAutorizadoFinal": round(total_km_aut_acumulado, 2),
        },
        "totaisPorContrato": resumo_equipes["totaisPorContrato"],
        "resumoEquipes": resumo_equipes,
    }

    # Salva arquivo único mensal localmente
    dir_mensal_local = output_dir / "rotalog" / "quilometragem" / "mensal"
    local_mensal_path = dir_mensal_local / f"{mes_str}.json.gz"
    salvar_arquivo_gzip(local_mensal_path, payload_mensal)
    LOG.debug("Arquivo único mensal salvo em: %s", local_mensal_path)


    # Upload de APENAS UM ARQUIVO mensal para o Firebase Storage
    firebase_synced = False
    erro_upload = None
    if enable_firebase:
        if progresso:
            progresso("Enviando consolidado mensal para a nuvem")
        try:
            if not firebase_store or not firebase_store.enabled:
                raise RuntimeError("Firebase solicitado, mas indisponível; arquivos locais preservados")
            monthly_key = f"mensal/{mes_str}"
            remote_monthly_blob = f"{firebase_store.root_prefix}/quilometragem/mensal/{mes_str}.json.gz"
            monthly_receipt = output_dir / "rotalog" / "sync" / company_key(empresa) / "historico" / "mensal" / f"{mes_str}.json"
            _enqueue_historical_upload(
                historical_queue, monthly_key, local_mensal_path, remote_monthly_blob, monthly_receipt,
            )
            sync_result = _flush_historical_uploads(historical_queue, firebase_store)
            if sync_result["results"].get(monthly_key) not in {"uploaded", "unchanged"}:
                raise RuntimeError("envio mensal pendente; arquivo local preservado na fila")
            firebase_synced = True
            LOG.debug("Arquivo único mensal sincronizado no Firebase: %s", remote_monthly_blob)
        except Exception as exc:
            erro_upload = str(exc)
            LOG.error("Erro ao sincronizar arquivo mensal no Firebase: %s", exc)

    if not coletar_diarios and erro_upload:
        monthly_state.save("monthly_upload_pending", year=ano, month=mes, total_days=ultimo_dia)
    else:
        persist_state("complete" if not erro_upload else "monthly_upload_pending")

    final_message = (
        "Fechamento mensal concluído, mas a sincronização ficou pendente."
        if erro_upload
        else "Fechamento mensal e diário concluído com sucesso."
    )
    atualizar_status_terminal(f"[{mes:02d}/{ano:04d}] {final_message}", final=True)

    finished_at = datetime.datetime.now(TZ)
    duration_s = round((finished_at - started_at).total_seconds(), 2)

    return {
        "status": "error" if erro_upload else "success",
        "month": mes_str,
        "durationSeconds": duration_s,
        "diasProcessados": dias_processados,
        "diasTotal": ultimo_dia,
        "totalRegistros": resumo_equipes["totalRegistros"],
        "totalServicos": payload_mensal["totalServicos"],
        "totalKmInformado": payload_mensal["totalKmInformado"],
        "totalKmAutorizadoFinal": payload_mensal["totalKmAutorizadoFinal"],
        "totalKmOficialInformado": resumo_equipes["totais"]["kmInformado"],
        "totalKmOficialAutorizadoFinal": resumo_equipes["totais"]["kmAutorizadoFinal"],
        "totalKmRecuperadoParecer": resumo_equipes["totais"]["kmRecuperadoParecer"],
        "localArquivoMensal": str(local_mensal_path),
        "erroUpload": erro_upload,
        "firebaseSynced": firebase_synced,
        "uploadQueue": sync_result if enable_firebase and firebase_store and firebase_store.enabled else {
            "pending": 0, "totalAttempts": 0, "oldestQueuedAt": None,
            "lastAttemptAt": None, "lastError": None,
        },
    }


def varrer_mes_anterior(
    output_dir: Path,
    empresa: str,
    enable_firebase: bool = False,
    firebase_store: typing.Any = None,
    progresso: typing.Callable[[str], None] | None = None,
    coletar_diarios: bool = True,
    session: requests.Session | None = None,
    skip_if_complete: bool = False,
) -> dict[str, typing.Any]:
    """Determina o mês anterior ao atual e dispara a varredura completa."""
    hoje = datetime.datetime.now(TZ).date()
    primeiro_do_mes = hoje.replace(day=1)
    ultimo_do_mes_anterior = primeiro_do_mes - datetime.timedelta(days=1)
    return varrer_mes(
        ano=ultimo_do_mes_anterior.year,
        mes=ultimo_do_mes_anterior.month,
        output_dir=output_dir,
        empresa=empresa,
        enable_firebase=enable_firebase,
        firebase_store=firebase_store,
        progresso=progresso,
        coletar_diarios=coletar_diarios,
        session=session,
        skip_if_complete=skip_if_complete,
    )
