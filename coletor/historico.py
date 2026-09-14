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
import typing
import warnings
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from coletor.client import CrawlerRotalog
from coletor.parser import formatar_protocolo_copel
from coletor.storage import company_key, normalize_team_key

LOG = logging.getLogger("coletor-historico")
TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))

URL_BASE = "https://www.copel.com/rtlweb"
URL_LISTAGEM_EVENTOS = f"{URL_BASE}/paginas/listagemEventos"

# Contratos monitorados para controle de produção / faturamento
CONTRATOS_ALVO: tuple[str, ...] = ("4600026988", "4600025149")


def parse_float_br(val: typing.Any) -> float:
    """Converte strings com valores numéricos no padrão brasileiro para float.
    
    Exemplos: '189,53' -> 189.53, '286' -> 286.0, '0' -> 0.0
    """
    if val is None:
        return 0.0
    s = str(val).strip()
    if not s or s == "-":
        return 0.0
    m = re.search(r"([\d.,]+)", s)
    if not m:
        return 0.0
    s_num = m.group(1).replace(".", "").replace(",", ".")
    try:
        return round(float(s_num), 2)
    except ValueError:
        return 0.0


def parse_iso_datetime(dt_str: typing.Any, base_day_iso: str | None = None) -> str | None:
    """Converte data/hora brasileira para ISO com timezone local (America/Sao_Paulo)."""
    if not dt_str:
        return None
    s = str(dt_str).strip()
    if not s or s == "-":
        return None

    for fmt in (
        "%d/%m/%y %H:%M",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ):
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=TZ).isoformat()
        except ValueError:
            pass

    if base_day_iso and re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", s):
        try:
            h_parts = [int(p) for p in s.split(":")]
            d_parts = [int(p) for p in base_day_iso.split("-")]
            sec = h_parts[2] if len(h_parts) > 2 else 0
            dt = datetime.datetime(d_parts[0], d_parts[1], d_parts[2], h_parts[0], h_parts[1], sec, tzinfo=TZ)
            return dt.isoformat()
        except Exception:
            pass

    return None


def extrair_data_iso_de_evento(row: dict[str, typing.Any], default_day: str | None = None) -> str:
    """Extrai a data AAAA-MM-DD a partir dos carimbos de data/hora do evento."""
    for col in ("Inicio Deslo", "Inicio Exec", "Fim Exec", "Retorno"):
        val = str(row.get(col) or "").strip()
        m = re.search(r"(\d{2})/(\d{2})/(\d{2,4})", val)
        if m:
            d, mth, y = m.group(1), m.group(2), m.group(3)
            ano = int(y) if len(y) == 4 else (2000 + int(y))
            return f"{ano:04d}-{mth}-{d}"
    if default_day:
        return default_day
    return datetime.datetime.now(TZ).date().isoformat()


def parse_target_date(data_str: str | None = None) -> datetime.date:
    """Converte string de data (YYYY-MM-DD ou DD/MM/YYYY) para datetime.date."""
    if not data_str or data_str.strip().lower() in ("ontem", "d-1", "yesterday", "true", ""):
        hoje = datetime.datetime.now(TZ).date()
        return hoje - datetime.timedelta(days=1)

    limpa = data_str.strip()
    try:
        return datetime.date.fromisoformat(limpa)
    except ValueError:
        pass

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(limpa, fmt).date()
        except ValueError:
            pass

    raise ValueError(f"Formato de data inválido: '{data_str}'. Use AAAA-MM-DD ou DD/MM/AAAA.")


class RotalogEventosScraper:
    """Sessão autenticada e extrator de listagem de eventos com paginação PrimeFaces."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or self._autenticar()

    def _autenticar(self) -> requests.Session:
        """Efetua login no ROTALOG reutilizando o CrawlerRotalog."""
        LOG.info("Autenticando sessão para raspagem de eventos...")
        crawler = CrawlerRotalog()
        if not crawler.autenticar():
            raise RuntimeError("Falha de autenticação ao conectar no ROTALOG Web.")
        return crawler.session

    def raspar_periodo(self, data_inicio_br: str, data_fim_br: str) -> list[dict[str, typing.Any]]:
        """Pesquisa e extrai todos os eventos e serviços do período na tela listagemEventos."""
        LOG.info("Consultando listagem de eventos: período %s até %s", data_inicio_br, data_fim_br)

        resp_page = self.session.get(URL_LISTAGEM_EVENTOS, verify=False, timeout=30)
        resp_page.raise_for_status()
        soup_page = BeautifulSoup(resp_page.text, "html.parser")
        view_state_elem = soup_page.find("input", {"name": "javax.faces.ViewState"})
        if not view_state_elem:
            raise RuntimeError(f"ViewState ausente em {URL_LISTAGEM_EVENTOS}")

        post_data = {
            "form": "form",
            "form:j_idt27:dataInicial_input": data_inicio_br,
            "form:j_idt27:dataFinal_input": data_fim_br,
            "form:veiculo": "",
            "form:contrato_input": "",
            "form:j_idt40": "",
            "javax.faces.ViewState": view_state_elem["value"],
        }

        resp_search = self.session.post(URL_LISTAGEM_EVENTOS, data=post_data, verify=False, timeout=60)
        resp_search.raise_for_status()

        m_ev = re.search(r"widget_form_tbListagemEventos.*?rowCount:(\d+)", resp_search.text)
        row_count_ev = int(m_ev.group(1)) if m_ev else 0
        LOG.info("Eventos encontrados no portal: %d linhas", row_count_ev)

        soup_search = BeautifulSoup(resp_search.text, "html.parser")
        updated_vs = soup_search.find("input", {"name": "javax.faces.ViewState"})
        vs_eventos = updated_vs["value"] if updated_vs else view_state_elem["value"]

        eventos_records: list[dict[str, typing.Any]] = []
        page_size_ev = 50
        total_pages_ev = math.ceil(row_count_ev / page_size_ev) if row_count_ev > 0 else 1

        eventos_component = soup_search.find(id="form:tbListagemEventos")
        eventos_table = eventos_component.find("table") if eventos_component else None
        headers_clean: list[str] = []
        if eventos_table:
            raw_ths = [th.text.strip().replace("\n", " ") for th in eventos_table.find_all("th") if th.text.strip()]
            headers_clean = [re.sub(r"Filter by.*", "", h).strip() for h in raw_ths]

        def parse_eventos_rows(soup_ctx):
            for tr in soup_ctx.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                cells = [td.text.strip().replace("\n", " ") for td in tds]
                if len(cells) >= len(headers_clean):
                    row_dict = {
                        headers_clean[i]: cells[i]
                        for i in range(min(len(headers_clean), len(cells)))
                    }
                    eventos_records.append(row_dict)

        headers_ajax = {
            "Faces-Request": "partial/ajax",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        for page in range(total_pages_ev):
            offset = page * page_size_ev
            if offset == 0 and eventos_table:
                parse_eventos_rows(eventos_table)
            else:
                ajax_data = {
                    "javax.faces.partial.ajax": "true",
                    "javax.faces.source": "form:tbListagemEventos",
                    "javax.faces.partial.execute": "form:tbListagemEventos",
                    "javax.faces.partial.render": "form:tbListagemEventos",
                    "form:tbListagemEventos": "form:tbListagemEventos",
                    "form:tbListagemEventos_pagination": "true",
                    "form:tbListagemEventos_first": str(offset),
                    "form:tbListagemEventos_rows": str(page_size_ev),
                    "form:tbListagemEventos_encodeFeature": "true",
                    "form": "form",
                    "form:j_idt27:dataInicial_input": data_inicio_br,
                    "form:j_idt27:dataFinal_input": data_fim_br,
                    "javax.faces.ViewState": vs_eventos,
                }
                resp_ajax = self.session.post(URL_LISTAGEM_EVENTOS, data=ajax_data, headers=headers_ajax, verify=False, timeout=30)
                resp_ajax.raise_for_status()
                soup_ajax = BeautifulSoup(resp_ajax.text, "html.parser")
                update = soup_ajax.find("update", {"id": "form:tbListagemEventos"})
                if update:
                    parse_eventos_rows(BeautifulSoup(update.text, "html.parser"))

        LOG.info("Raspagem de eventos concluída: %d registros obtidos.", len(eventos_records))
        return eventos_records


def salvar_arquivo_gzip(path: Path, payload: dict[str, typing.Any]) -> None:
    """Salva arquivo JSON compactado em GZIP localmente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=6) as stream:
        stream.write(raw)


def estruturar_quilometragem_diaria(
    eventos_records: list[dict[str, typing.Any]],
    target_day: str,
    empresa: str,
    contratos_alvo: tuple[str, ...] = CONTRATOS_ALVO,
) -> dict[str, typing.Any]:
    """Cria a estrutura enxuta única do dia com totais por equipe e protocolos filtrados pelos contratos alvo."""
    emp_key = company_key(empresa)
    protocolos: dict[str, dict[str, typing.Any]] = {}
    totais_equipe: dict[str, dict[str, typing.Any]] = {}
    alvo_set = set(contratos_alvo) if contratos_alvo else None

    for r in eventos_records:
        contrato = str(r.get("Contrato") or "").strip()
        if alvo_set and contrato not in alvo_set:
            continue

        team_raw = str(r.get("Veículo") or r.get("Veiculo") or "").strip()
        team_key = normalize_team_key(team_raw)
        if not team_key:
            continue

        km_inf = parse_float_br(r.get("Informado com limitador (km)"))
        km_aut = parse_float_br(r.get("Autorizado final(km)"))

        if team_key not in totais_equipe:
            totais_equipe[team_key] = {
                "kmInformado": 0.0,
                "kmAutorizadoFinal": 0.0,
                "contrato": contrato,
            }

        eq = totais_equipe[team_key]
        eq["kmInformado"] = round(eq["kmInformado"] + km_inf, 2)
        eq["kmAutorizadoFinal"] = round(eq["kmAutorizadoFinal"] + km_aut, 2)

        prot_raw = str(r.get("Protocolo") or "").strip()
        if prot_raw:
            prot_clean = formatar_protocolo_copel(prot_raw)
            protocolos[prot_clean] = {
                "equipe": team_key,
                "kmInformado": km_inf,
                "kmAutorizadoFinal": km_aut,
            }

    total_km_inf = sum(eq["kmInformado"] for eq in totais_equipe.values())
    total_km_aut = sum(eq["kmAutorizadoFinal"] for eq in totais_equipe.values())

    return {
        "schemaVersion": 2,
        "data": target_day,
        "empresa": empresa,
        "empresaKey": emp_key,
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "contratos": list(contratos_alvo) if contratos_alvo else [],
        "totalEquipes": len(totais_equipe),
        "totalServicos": len(protocolos),
        "totalKmInformado": round(total_km_inf, 2),
        "totalKmAutorizadoFinal": round(total_km_aut, 2),
        "totaisPorEquipe": totais_equipe,
        "protocolos": protocolos,
    }


def executar_coleta_historico_dia(
    target_date: datetime.date,
    output_dir: Path,
    empresa: str,
    enable_firebase: bool = False,
    firebase_store: typing.Any = None,
) -> dict[str, typing.Any]:
    """Raspa a listagem de eventos e gera um ÚNICO arquivo diário de quilometragem."""
    day_iso = target_date.isoformat()
    day_br = target_date.strftime("%d/%m/%Y")
    started_at = datetime.datetime.now(TZ)

    LOG.info("=== Coleta Diária de Quilometragem: data %s (Empresa: %s) ===", day_iso, empresa)

    scraper = RotalogEventosScraper()
    eventos = scraper.raspar_periodo(day_br, day_br)

    # 1. Gera o documento consolidado do dia
    payload_km = estruturar_quilometragem_diaria(eventos, target_day=day_iso, empresa=empresa)

    # 2. Salva localmente em dados-local/rotalog/quilometragem/diario/AAAA-MM-DD.json.gz
    dir_km_local = output_dir / "rotalog" / "quilometragem" / "diario"
    local_km_path = dir_km_local / f"{day_iso}.json.gz"
    salvar_arquivo_gzip(local_km_path, payload_km)
    LOG.info("Arquivo único de quilometragem salvo: %s", local_km_path)

    # 3. Upload de APENAS UM ARQUIVO para o Firebase Storage
    firebase_synced = False
    if enable_firebase and firebase_store and firebase_store.enabled:
        try:
            remote_blob = f"{firebase_store.root_prefix}/quilometragem/diario/{day_iso}.json.gz"
            firebase_store.save_blob(remote_blob, payload_km)
            firebase_synced = True
            LOG.info("Arquivo único de quilometragem sincronizado no Firebase: %s", remote_blob)
        except Exception as exc:
            LOG.error("Erro ao sincronizar arquivo diário no Firebase Storage: %s", exc)

    finished_at = datetime.datetime.now(TZ)
    duration_s = round((finished_at - started_at).total_seconds(), 2)

    return {
        "status": "success",
        "date": day_iso,
        "durationSeconds": duration_s,
        "totalEquipes": payload_km["totalEquipes"],
        "totalServicos": payload_km["totalServicos"],
        "totalKmInformado": payload_km["totalKmInformado"],
        "totalKmAutorizadoFinal": payload_km["totalKmAutorizadoFinal"],
        "localArquivoKm": str(local_km_path),
        "firebaseSynced": firebase_synced,
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
) -> dict[str, typing.Any]:
    """Varre todos os eventos do mês e gera um ÚNICO arquivo consolidado mensal de faturamento."""
    started_at = datetime.datetime.now(TZ)
    mes_str = f"{ano:04d}-{mes:02d}"
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    dt_inicio = datetime.date(ano, mes, 1)
    dt_fim = datetime.date(ano, mes, ultimo_dia)

    LOG.info("=== Iniciando Varredura Mensal de Quilometragens: %s (%s a %s) ===", mes_str, dt_inicio, dt_fim)

    chunks = dividir_periodo_em_intervalos(dt_inicio, dt_fim, dias_por_chunk=5)
    scraper = RotalogEventosScraper()

    todos_eventos: list[dict[str, typing.Any]] = []
    for idx, (chunk_ini, chunk_fim) in enumerate(chunks, start=1):
        ini_br = chunk_ini.strftime("%d/%m/%Y")
        fim_br = chunk_fim.strftime("%d/%m/%Y")
        LOG.info("Processando bloco %d/%d: %s até %s", idx, len(chunks), ini_br, fim_br)
        eventos_chunk = scraper.raspar_periodo(ini_br, fim_br)
        todos_eventos.extend(eventos_chunk)

    # Gera a estrutura única do mês com todos os protocolos
    payload_mensal = estruturar_quilometragem_diaria(todos_eventos, target_day=mes_str, empresa=empresa)
    payload_mensal["mes"] = mes_str
    payload_mensal["periodo"] = {"inicio": dt_inicio.isoformat(), "fim": dt_fim.isoformat()}

    # Salva arquivo único mensal localmente
    dir_mensal_local = output_dir / "rotalog" / "quilometragem" / "mensal"
    local_mensal_path = dir_mensal_local / f"{mes_str}.json.gz"
    salvar_arquivo_gzip(local_mensal_path, payload_mensal)
    LOG.info("Arquivo único mensal salvo em: %s", local_mensal_path)

    # Upload de APENAS UM ARQUIVO mensal para o Firebase Storage
    firebase_synced = False
    if enable_firebase and firebase_store and firebase_store.enabled:
        try:
            remote_monthly_blob = f"{firebase_store.root_prefix}/quilometragem/mensal/{mes_str}.json.gz"
            firebase_store.save_blob(remote_monthly_blob, payload_mensal)
            firebase_synced = True
            LOG.info("Arquivo único mensal sincronizado no Firebase: %s", remote_monthly_blob)
        except Exception as exc:
            LOG.error("Erro ao sincronizar arquivo mensal no Firebase: %s", exc)

    finished_at = datetime.datetime.now(TZ)
    duration_s = round((finished_at - started_at).total_seconds(), 2)

    return {
        "status": "success",
        "month": mes_str,
        "durationSeconds": duration_s,
        "totalEquipes": payload_mensal["totalEquipes"],
        "totalServicos": payload_mensal["totalServicos"],
        "totalKmInformado": payload_mensal["totalKmInformado"],
        "totalKmAutorizadoFinal": payload_mensal["totalKmAutorizadoFinal"],
        "localArquivoMensal": str(local_mensal_path),
        "firebaseSynced": firebase_synced,
    }


def varrer_mes_anterior(
    output_dir: Path,
    empresa: str,
    enable_firebase: bool = False,
    firebase_store: typing.Any = None,
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
    )
