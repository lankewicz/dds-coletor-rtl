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
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from coletor.client import CrawlerRotalog
from coletor.parser import formatar_protocolo_copel
from coletor.relatorio import gerar_relatorio_diario, gerar_relatorio_mensal
from coletor.equipes import normalize_team_key
from coletor.storage import company_key, write_json

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


def formatar_numero_br(valor: float | int | None, casas: int = 2) -> str:
    """Formata valores numéricos no padrão brasileiro (ex: 12.345,67 ou 1.234)."""
    if valor is None:
        return "0,00" if casas > 0 else "0"
    try:
        val_float = float(valor)
    except (ValueError, TypeError):
        return str(valor)
    if casas == 0:
        return f"{int(round(val_float)):,}".replace(",", ".")
    partes = f"{val_float:,.{casas}f}".split(".")
    milhares = partes[0].replace(",", ".")
    decimal = partes[1]
    return f"{milhares},{decimal}"


def atualizar_status_terminal(texto: str, final: bool = False) -> None:
    """Atualiza o status na mesma linha do terminal via carriage return (\r)."""
    largura = 110
    linha_limpa = texto.replace("\n", " ").replace("\r", "")
    if len(linha_limpa) > largura:
        linha_formatada = linha_limpa[:largura - 3] + "..."
    else:
        linha_formatada = linha_limpa.ljust(largura)

    if final:
        sys.stdout.write(f"\r{linha_formatada}\n")
    else:
        sys.stdout.write(f"\r{linha_formatada}")
    sys.stdout.flush()


def formatar_resumo_mensal_terminal(res: dict[str, typing.Any]) -> str:
    """Gera caixa visual resumida para exibição do fechamento mensal no terminal."""
    mes = res.get("month", "")
    dias_proc = res.get("diasProcessados", 0)
    dias_tot = res.get("diasTotal", 0)
    dur = res.get("durationSeconds", 0.0)
    servicos = formatar_numero_br(res.get("totalServicos", 0), 0)
    km_inf = formatar_numero_br(res.get("totalKmInformado", 0.0), 2)
    km_aut = formatar_numero_br(res.get("totalKmAutorizadoFinal", 0.0), 2)
    km_rec = formatar_numero_br(res.get("totalKmRecuperadoParecer", 0.0), 2)
    arq_mensal = res.get("localArquivoMensal", "-")
    rel_mensal = res.get("localRelatorioHtml", "-")
    tot_diarios = res.get("totalRelatoriosDiarios", 0)
    firebase = "Sim" if res.get("firebaseSynced") else "Não"

    linhas = [
        "",
        "=" * 80,
        "             FECHAMENTO MENSAL E DIÁRIO CONCLUÍDO COM SUCESSO",
        "=" * 80,
        f" Mês de Referência       : {mes}",
        f" Dias Processados        : {dias_proc}/{dias_tot} dias",
        f" Quantidade de Serviços  : {servicos}",
        f" Total KM Informado      : {km_inf} km",
        f" Total KM Autorizado     : {km_aut} km",
        f" Total KM Recuperado     : {km_rec} km",
        f" Relatórios Diários      : {tot_diarios} gerados em rotalog/quilometragem/diario/",
        f" Arquivo Mensal JSON     : {arq_mensal}",
        f" Relatório Mensal HTML   : {rel_mensal}",
        f" Sincronizado Firebase   : {firebase}",
        f" Tempo de Execução       : {dur}s",
        "=" * 80,
        "",
    ]
    return "\n".join(linhas)


def formatar_resumo_diario_terminal(res: dict[str, typing.Any]) -> str:
    """Gera caixa visual resumida para exibição de coleta diária no terminal."""
    data_iso = res.get("date", "")
    dur = res.get("durationSeconds", 0.0)
    equipes = formatar_numero_br(res.get("totalEquipes", 0), 0)
    servicos = formatar_numero_br(res.get("totalServicos", 0), 0)
    km_inf = formatar_numero_br(res.get("totalKmInformado", 0.0), 2)
    km_aut = formatar_numero_br(res.get("totalKmAutorizadoFinal", 0.0), 2)
    km_rec = formatar_numero_br(res.get("totalKmRecuperadoParecer", 0.0), 2)
    arq_diario = res.get("localArquivoKm", "-")
    rel_diario = res.get("localRelatorioHtml", "-")
    firebase = "Sim" if res.get("firebaseSynced") else "Não"

    linhas = [
        "",
        "=" * 80,
        "                    COLETA DIÁRIA CONCLUÍDA COM SUCESSO",
        "=" * 80,
        f" Data de Referência      : {data_iso}",
        f" Total de Equipes        : {equipes}",
        f" Quantidade de Serviços  : {servicos}",
        f" Total KM Informado      : {km_inf} km",
        f" Total KM Autorizado     : {km_aut} km",
        f" Total KM Recuperado     : {km_rec} km",
        f" Arquivo Diário JSON     : {arq_diario}",
        f" Relatório Diário HTML   : {rel_diario}",
        f" Sincronizado Firebase   : {firebase}",
        f" Tempo de Execução       : {dur}s",
        "=" * 80,
        "",
    ]
    return "\n".join(linhas)


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
    """Sessão autenticada e extrator de listagem de eventos (/paginas/listagemEventos) com paginação PrimeFaces."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or self._autenticar()

    def _autenticar(self) -> requests.Session:
        """Efetua login no ROTALOG reutilizando o CrawlerRotalog."""
        LOG.debug("Autenticando sessão para raspagem de eventos...")
        crawler = CrawlerRotalog()
        return crawler.criar_sessao_autenticada()

    def raspar_periodo(
        self,
        data_inicio_br: str,
        data_fim_br: str,
        progresso: typing.Callable[[str], None] | None = None,
    ) -> list[dict[str, typing.Any]]:
        """Pesquisa e extrai todos os eventos e serviços do período na tela listagemEventos."""
        LOG.debug("Consultando listagem de eventos: período %s até %s", data_inicio_br, data_fim_br)

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
        m_rows = re.search(r"widget_form_tbListagemEventos.*?rows:(\d+)", resp_search.text)
        page_size_ev = int(m_rows.group(1)) if m_rows else 50
        total_pages_ev = math.ceil(row_count_ev / page_size_ev) if row_count_ev > 0 else 1
        LOG.debug("Eventos encontrados: %d linhas em %d páginas", row_count_ev, total_pages_ev)

        soup_search = BeautifulSoup(resp_search.text, "html.parser")
        updated_vs = soup_search.find("input", {"name": "javax.faces.ViewState"})
        vs_eventos = updated_vs["value"] if updated_vs else view_state_elem["value"]

        eventos_records: list[dict[str, typing.Any]] = []

        eventos_component = soup_search.find(id="form:tbListagemEventos")
        eventos_table = eventos_component.find("table") if eventos_component else None
        headers_clean: list[str] = []
        if eventos_table:
            raw_ths = [th.text.strip().replace("\n", " ") for th in eventos_table.find_all("th") if th.text.strip()]
            headers_clean = [re.sub(r"Filter by.*", "", h).strip() for h in raw_ths]

        def parse_eventos_rows(soup_ctx):
            for tr in soup_ctx.find_all("tr"):
                tds = tr.find_all("td")
                if not tds or (len(tds) == 1 and "nenhum" in tds[0].text.lower()):
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
                vs_update = soup_ajax.find("update", {"id": "javax.faces.ViewState"})
                if vs_update and vs_update.text.strip():
                    vs_eventos = vs_update.text.strip()

            if progresso:
                cur_km_inf = sum(parse_float_br(r.get("Informado com limitador (km)") or r.get("Km Informado") or r.get("Km Inform")) for r in eventos_records)
                cur_km_aut = sum(parse_float_br(r.get("Autorizado final(km)") or r.get("Km Autorizado") or r.get("Km Auto")) for r in eventos_records)
                progresso(
                    f"Pág {page + 1}/{total_pages_ev} | "
                    f"Serviços: {formatar_numero_br(len(eventos_records), 0)} | "
                    f"KM Inf: {formatar_numero_br(cur_km_inf, 2)} | "
                    f"KM Aut: {formatar_numero_br(cur_km_aut, 2)}"
                )

        LOG.debug("Raspagem de eventos concluída: %d registros obtidos em %d páginas.", len(eventos_records), total_pages_ev)
        return eventos_records


class RotalogEquipesScraper:
    """Extrai o fechamento oficial por equipe da tela ``/paginas/equipes`` com paginação PrimeFaces."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or self._autenticar()

    def _autenticar(self) -> requests.Session:
        crawler = CrawlerRotalog()
        return crawler.criar_sessao_autenticada()

    @staticmethod
    def _parse_rows(context: typing.Any) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        if not context:
            return records
        for tr in context.find_all("tr"):
            tds = tr.find_all("td")
            if not tds or (len(tds) == 1 and "nenhum" in tds[0].text.lower()):
                continue
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) >= len(EQUIPES_HEADERS):
                record = dict(zip(EQUIPES_HEADERS, cells[:len(EQUIPES_HEADERS)]))
                team_link = tds[0].find("a")
                if team_link:
                    href = str(team_link.get("href") or "").strip()
                    onclick = str(team_link.get("onclick") or "").strip()
                    source_match = re.search(r"(?:s|source)\s*:\s*['\"]([^'\"]+)", onclick)
                    render_match = re.search(r"(?:u|update)\s*:\s*['\"]([^'\"]+)", onclick)
                    source = source_match.group(1) if source_match else str(team_link.get("id") or "").strip()
                    record["_detalheHref"] = href
                    record["_detalheSource"] = source
                    record["_detalheRender"] = render_match.group(1) if render_match else "@all"
                records.append(record)
        return records

    @staticmethod
    def _parse_electricians(context: typing.Any) -> dict[str, str]:
        """Extrai matrícula e nome completo do painel de detalhes da equipe."""
        result: dict[str, str] = {}
        if not context:
            return result
        candidates = []
        for tag in context.find_all(["li", "tr", "p", "div"]):
            text = tag.get_text(" ", strip=True)
            if "Eletricista" in text and len(text) <= 300:
                candidates.append(text)
        candidates.append(context.get_text("\n", strip=True))
        for number in (1, 2):
            pattern = re.compile(
                rf"Eletricista\s*{number}\s*:\s*(\d+)\s*-\s*([^\n|]+)",
                re.IGNORECASE,
            )
            match = next(
                (pattern.search(text) for text in sorted(candidates, key=len) if pattern.search(text)),
                None,
            )
            if match:
                result[f"Eletricista {number} Registro"] = match.group(1).strip()
                result[f"Eletricista {number} Nome"] = match.group(2).strip()
                result[f"Eletricista {number}"] = f"{match.group(1).strip()} - {match.group(2).strip()}"
        return result

    def _fetch_team_details(self, record: dict[str, str], view_state: str) -> tuple[dict[str, str], str]:
        href = str(record.get("_detalheHref") or "").strip()
        source = str(record.get("_detalheSource") or "").strip()
        response = None
        if href and href != "#" and not href.lower().startswith("javascript:"):
            response = self.session.get(urljoin(URL_EQUIPES, href), verify=False, timeout=30)
        elif source:
            render = str(record.get("_detalheRender") or "@all")
            ajax_data = {
                "javax.faces.partial.ajax": "true",
                "javax.faces.source": source,
                "javax.faces.partial.execute": source,
                "javax.faces.partial.render": render,
                source: source,
                "form": "form",
                "javax.faces.ViewState": view_state,
            }
            response = self.session.post(
                URL_EQUIPES,
                data=ajax_data,
                headers={"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"},
                verify=False,
                timeout=30,
            )
        if response is None:
            return {}, view_state
        response.raise_for_status()
        detail_soup = BeautifulSoup(response.content, "html.parser")
        state_update = detail_soup.find("update", {"id": "javax.faces.ViewState"})
        new_state = state_update.get_text().strip() if state_update else view_state
        return self._parse_electricians(detail_soup), new_state

    def _enrich_team_details(self, records: list[dict[str, str]], view_state: str) -> str:
        for record in records:
            try:
                details, view_state = self._fetch_team_details(record, view_state)
                record.update(details)
            except Exception as exc:
                LOG.debug("Falha ao consultar detalhes da equipe %s: %s", record.get("Veiculo"), exc)
            finally:
                record.pop("_detalheHref", None)
                record.pop("_detalheSource", None)
                record.pop("_detalheRender", None)
        return view_state

    def raspar_periodo(
        self,
        data_inicio_br: str,
        data_fim_br: str,
        progresso: typing.Callable[[str], None] | None = None,
        enriquecer_detalhes: bool = False,
    ) -> list[dict[str, str]]:
        """Pesquisa e extrai todos os fechamentos de equipes do período na tela /paginas/equipes."""
        LOG.debug("Consultando fechamento de equipes: período %s até %s", data_inicio_br, data_fim_br)
        response = self.session.get(URL_EQUIPES, verify=False, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        view_state = soup.find("input", {"name": "javax.faces.ViewState"})
        if not view_state:
            raise RuntimeError(f"ViewState ausente em {URL_EQUIPES}")

        search_data = {
            "form": "form",
            "form:j_idt27:dataInicial_input": data_inicio_br,
            "form:j_idt27:dataFinal_input": data_fim_br,
            "form:veiculo": "",
            "form:contrato_input": "",
            "form:j_idt40": "form:j_idt40",
            "javax.faces.ViewState": view_state["value"],
        }
        searched = self.session.post(URL_EQUIPES, data=search_data, verify=False, timeout=60)
        searched.raise_for_status()
        searched_soup = BeautifulSoup(searched.content, "html.parser")
        updated_state = searched_soup.find("input", {"name": "javax.faces.ViewState"})
        current_state = updated_state["value"] if updated_state else view_state["value"]
        widget_match = re.search(r'widget_form_tbEquipes.*?rowCount:(\d+)', searched.text)
        row_count = int(widget_match.group(1)) if widget_match else 0
        rows_match = re.search(r'widget_form_tbEquipes.*?rows:(\d+)', searched.text)
        page_size = int(rows_match.group(1)) if rows_match else 25

        component = searched_soup.find(id="form:tbEquipes")
        records = self._parse_rows(component) if component else []
        if enriquecer_detalhes:
            current_state = self._enrich_team_details(records, current_state)
        total_pages = math.ceil(row_count / page_size) if row_count else 1
        LOG.debug("Equipes encontradas: %d linhas em %d páginas", row_count, total_pages)

        if progresso:
            progresso(f"Pág 1/{total_pages} ({len(records)}/{row_count} equipes)")

        ajax_headers = {
            "Faces-Request": "partial/ajax",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        for page in range(1, total_pages):
            offset = page * page_size
            ajax_data = {
                "javax.faces.partial.ajax": "true",
                "javax.faces.source": "form:tbEquipes",
                "javax.faces.partial.execute": "form:tbEquipes",
                "javax.faces.partial.render": "form:tbEquipes",
                "form:tbEquipes": "form:tbEquipes",
                "form:tbEquipes_pagination": "true",
                "form:tbEquipes_first": str(offset),
                "form:tbEquipes_rows": str(page_size),
                "form:tbEquipes_encodeFeature": "true",
                "form": "form",
                "form:j_idt27:dataInicial_input": data_inicio_br,
                "form:j_idt27:dataFinal_input": data_fim_br,
                "javax.faces.ViewState": current_state,
            }
            paged = self.session.post(URL_EQUIPES, data=ajax_data, headers=ajax_headers, verify=False, timeout=30)
            paged.raise_for_status()
            ajax_soup = BeautifulSoup(paged.content, "html.parser")
            update = ajax_soup.find("update", {"id": "form:tbEquipes"})
            vs_update = ajax_soup.find("update", {"id": "javax.faces.ViewState"})
            if vs_update and vs_update.get_text().strip():
                current_state = vs_update.get_text().strip()
            if update:
                page_rows = self._parse_rows(BeautifulSoup(update.get_text(), "html.parser"))
                if enriquecer_detalhes:
                    current_state = self._enrich_team_details(page_rows, current_state)
                records.extend(page_rows)

            if progresso:
                cur_serv = sum(int(parse_float_br(r.get("Servicos executados"))) for r in records)
                cur_km_inf = sum(parse_float_br(r.get("Informado com limitador (km)")) for r in records)
                cur_km_aut = sum(parse_float_br(r.get("Autorizado Final (km)") or r.get("Autorizado (km)")) for r in records)
                progresso(
                    f"Pág {page + 1}/{total_pages} | "
                    f"Equipes: {len(records)} | Serv: {formatar_numero_br(cur_serv, 0)} | "
                    f"KM Inf: {formatar_numero_br(cur_km_inf, 2)} | "
                    f"KM Aut: {formatar_numero_br(cur_km_aut, 2)}"
                )

        LOG.debug("Fechamentos de equipes obtidos: %d registros em %d páginas", len(records), total_pages)
        return records

    def raspar_dia(
        self,
        data_br: str,
        progresso: typing.Callable[[str], None] | None = None,
    ) -> list[dict[str, str]]:
        return self.raspar_periodo(data_br, data_br, progresso=progresso, enriquecer_detalhes=True)


def salvar_arquivo_gzip(path: Path, payload: dict[str, typing.Any]) -> None:
    """Salva arquivo JSON compactado em GZIP localmente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=6) as stream:
        stream.write(raw)


_CAMPOS_KM_EQUIPE = (
    "kmInformado", "kmGlosadoCritico", "kmGlosadoParecer", "kmAutorizadoInicial",
    "kmRecuperadoParecer", "kmAutorizadoFinal", "kmAguardandoJustificativa", "kmEmAnalise",
)


def _quantidade_e_km(value: typing.Any) -> tuple[int, float]:
    raw = str(value or "").strip()
    quantidade_match = re.search(r"\d+", raw)
    km_match = re.search(r"\(([\d.,]+)\s*Km\)", raw, re.IGNORECASE)
    return (
        int(quantidade_match.group(0)) if quantidade_match else 0,
        parse_float_br(km_match.group(1)) if km_match else 0.0,
    )


def _electrician_identity(row: dict[str, typing.Any], number: int) -> tuple[str, str, str]:
    display = str(row.get(f"Eletricista {number}") or "").strip()
    registration = str(row.get(f"Eletricista {number} Registro") or "").strip()
    name = str(row.get(f"Eletricista {number} Nome") or "").strip()
    if (not registration or not name) and display:
        match = re.match(r"\s*(\d+)\s*-\s*(.+?)\s*$", display)
        if match:
            registration = registration or match.group(1)
            name = name or match.group(2)
    return registration, name, display or " - ".join(value for value in (registration, name) if value)


def _normalized_event_type(row: dict[str, typing.Any]) -> str:
    for key in ("Evento", "Tipo Evento", "Tipo de Evento", "Descricao", "Descrição", "Status"):
        value = str(row.get(key) or "").strip()
        if value:
            return re.sub(r"\s+", " ", value).casefold()
    return ""


def deduplicar_eventos_consolidados(
    records: list[dict[str, typing.Any]],
) -> tuple[list[dict[str, typing.Any]], dict[str, int]]:
    """Remove duplicatas do consolidado sem alterar o arquivo bruto de auditoria."""
    exact_seen: set[str] = set()
    last_technical_state: dict[tuple[str, str], str] = {}
    kept: list[dict[str, typing.Any]] = []
    exact_removed = 0
    burst_removed = 0
    technical_types = {"fim de turno", "inicio de turno", "início de turno"}

    for row in records:
        exact_key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if exact_key in exact_seen:
            exact_removed += 1
            continue
        exact_seen.add(exact_key)

        event_type = _normalized_event_type(row)
        if event_type in technical_types:
            team = normalize_team_key(row.get("Veiculo") or row.get("Veículo"))
            contract = str(row.get("Contrato") or "").strip()
            logical_key = (team, contract)
            normalized_state = event_type.replace("í", "i")
            if last_technical_state.get(logical_key) == normalized_state:
                burst_removed += 1
                continue
            last_technical_state[logical_key] = normalized_state
        kept.append(row)

    return kept, {
        "recebidos": len(records),
        "mantidos": len(kept),
        "duplicatasExatasRemovidas": exact_removed,
        "repeticoesTecnicasRemovidas": burst_removed,
        "totalRemovidos": exact_removed + burst_removed,
    }


def estruturar_resumo_equipes(
    records: list[dict[str, typing.Any]],
    contratos_alvo: tuple[str, ...] = CONTRATOS_ALVO,
) -> dict[str, typing.Any]:
    """Consolida a medição oficial diária/mensal exibida pela tela de equipes."""
    contratos = set(contratos_alvo) if contratos_alvo else None
    por_contrato: dict[str, dict[str, typing.Any]] = {}
    registros: list[dict[str, typing.Any]] = []
    for row in records:
        contrato = str(row.get("Contrato") or "").strip()
        if contratos and contrato not in contratos:
            continue
        equipe = normalize_team_key(row.get("Veiculo") or row.get("Veículo"))
        if not equipe:
            continue
        aguardando_qtd, aguardando_km = _quantidade_e_km(row.get("Aguardando justificativa"))
        analise_qtd, analise_km = _quantidade_e_km(row.get("Em analise") or row.get("Em análise"))
        eletricista1_registro, eletricista1_nome, eletricista1 = _electrician_identity(row, 1)
        eletricista2_registro, eletricista2_nome, eletricista2 = _electrician_identity(row, 2)
        registro = {
            "equipe": equipe,
            "tablet": str(row.get("Tablet") or "").replace("*", "").strip(),
            "agencia": str(row.get("Agencia") or row.get("Agência") or "").strip(),
            "contrato": contrato,
            "turnoReferencia": str(row.get("Data Referencia - Turno") or row.get("Data Referência - Turno") or "").strip(),
            "eletricista1": eletricista1,
            "eletricista1Registro": eletricista1_registro,
            "eletricista1Nome": eletricista1_nome,
            "eletricista2": eletricista2,
            "eletricista2Registro": eletricista2_registro,
            "eletricista2Nome": eletricista2_nome,
            "servicosExecutados": int(parse_float_br(row.get("Servicos executados") or row.get("Serviços executados"))),
            "kmInformado": parse_float_br(row.get("Informado com limitador (km)")),
            "kmGlosadoCritico": parse_float_br(row.get("Glosado critico (km)") or row.get("Glosado crítico (km)")),
            "kmGlosadoParecer": parse_float_br(row.get("Glosado por parecer (km)")),
            "kmAutorizadoInicial": parse_float_br(row.get("Autorizado (km)")),
            "kmRecuperadoParecer": parse_float_br(row.get("Recuperados por Parecer (km)")),
            "kmAutorizadoFinal": parse_float_br(row.get("Autorizado Final (km)")),
            "aguardandoJustificativa": aguardando_qtd,
            "kmAguardandoJustificativa": aguardando_km,
            "emAnalise": analise_qtd,
            "kmEmAnalise": analise_km,
            "alertas": int(parse_float_br(row.get("Alertas"))),
        }
        registros.append(registro)
        grupo = por_contrato.setdefault(contrato, {
            **{campo: 0.0 for campo in _CAMPOS_KM_EQUIPE},
            "servicosExecutados": 0, "aguardandoJustificativa": 0, "emAnalise": 0,
            "alertas": 0, "equipes": {},
        })
        total_equipe = grupo["equipes"].setdefault(equipe, {
            **{campo: 0.0 for campo in _CAMPOS_KM_EQUIPE},
            "servicosExecutados": 0, "aguardandoJustificativa": 0, "emAnalise": 0,
            "alertas": 0, "dias": [], "eletricistas": [], "agencias": [], "tablets": [],
        })
        for target in (grupo, total_equipe):
            for campo in _CAMPOS_KM_EQUIPE:
                target[campo] = round(target[campo] + registro[campo], 2)
            for campo in ("servicosExecutados", "aguardandoJustificativa", "emAnalise", "alertas"):
                target[campo] += registro[campo]
        dia_match = re.search(r"\d{2}/\d{2}/\d{4}", registro["turnoReferencia"])
        if dia_match and dia_match.group(0) not in total_equipe["dias"]:
            total_equipe["dias"].append(dia_match.group(0))
        for field, value in (
            ("eletricistas", registro["eletricista1"]), ("eletricistas", registro["eletricista2"]),
            ("agencias", registro["agencia"]), ("tablets", registro["tablet"]),
        ):
            if value and value not in total_equipe[field]:
                total_equipe[field].append(value)

    totais = {campo: round(sum(g[campo] for g in por_contrato.values()), 2) for campo in _CAMPOS_KM_EQUIPE}
    for campo in ("servicosExecutados", "aguardandoJustificativa", "emAnalise", "alertas"):
        totais[campo] = sum(g[campo] for g in por_contrato.values())
    return {
        "fonte": "ROTALOG_EQUIPES",
        "granularidade": "EQUIPE_DIA",
        "totalRegistros": len(registros),
        "totais": totais,
        "totaisPorContrato": por_contrato,
        "registros": registros,
    }


def estruturar_quilometragem_diaria(
    eventos_records: list[dict[str, typing.Any]],
    target_day: str,
    empresa: str,
    contratos_alvo: tuple[str, ...] = CONTRATOS_ALVO,
    equipes_records: list[dict[str, typing.Any]] | None = None,
) -> dict[str, typing.Any]:
    """Cria a estrutura enxuta única do dia com totais por equipe e protocolos filtrados pelos contratos alvo."""
    emp_key = company_key(empresa)
    eventos_records, dedup_stats = deduplicar_eventos_consolidados(eventos_records)
    protocolos: dict[str, dict[str, typing.Any]] = {}
    servicos: list[dict[str, typing.Any]] = []
    totais_equipe: dict[str, dict[str, typing.Any]] = {}
    totais_contrato: dict[str, dict[str, typing.Any]] = {}
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

        grupo = totais_contrato.setdefault(contrato, {
            "kmInformado": 0.0, "kmAutorizadoFinal": 0.0, "kmRecuperado": None, "equipes": {},
        })
        equipe_contrato = grupo["equipes"].setdefault(team_key, {
            "kmInformado": 0.0, "kmAutorizadoFinal": 0.0, "kmRecuperado": None,
        })
        for total in (grupo, equipe_contrato):
            total["kmInformado"] = round(total["kmInformado"] + km_inf, 2)
            total["kmAutorizadoFinal"] = round(total["kmAutorizadoFinal"] + km_aut, 2)

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
        prot_clean = formatar_protocolo_copel(prot_raw) if prot_raw else ""
        servico_info = {
            "protocolo": prot_clean or prot_raw,
            "equipe": team_key,
            "contrato": contrato,
            "inicioDeslocamento": str(r.get("Inicio Deslo") or r.get("Início Deslo") or "").strip(),
            "inicioExecucao": str(r.get("Inicio Exec") or r.get("Início Exec") or "").strip(),
            "fimExecucao": str(r.get("Fim Exec") or "").strip(),
            "retorno": str(r.get("Retorno") or "").strip(),
            "kmInformado": km_inf,
            "kmAutorizadoFinal": km_aut,
            "diferencaKm": round(km_inf - km_aut, 2),
        }
        servicos.append(servico_info)
        if prot_clean:
            protocolos[prot_clean] = servico_info

    total_km_inf = sum(eq["kmInformado"] for eq in totais_equipe.values())
    total_km_aut = sum(eq["kmAutorizadoFinal"] for eq in totais_equipe.values())

    resumo_equipes = estruturar_resumo_equipes(equipes_records or [], contratos_alvo)
    return {
        "schemaVersion": 3,
        "data": target_day,
        "empresa": empresa,
        "empresaKey": emp_key,
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "contratos": list(contratos_alvo) if contratos_alvo else [],
        "totalEquipes": len(totais_equipe),
        "totalServicos": len(servicos) or len(protocolos),
        "totalKmInformado": round(total_km_inf, 2),
        "totalKmAutorizadoFinal": round(total_km_aut, 2),
        "totaisPorEquipe": totais_equipe,
        "totaisPorContrato": totais_contrato,
        "protocolos": protocolos,
        "servicos": servicos,
        "detalhamentoServicos": {
            "fonte": "ROTALOG_LISTAGEM_EVENTOS",
            "granularidade": "SERVICO",
            "totalKmInformado": round(total_km_inf, 2),
            "totalKmAutorizadoFinal": round(total_km_aut, 2),
        },
        "deduplicacaoEventos": dedup_stats,
        "resumoEquipes": resumo_equipes,
    }


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

    equipes = []
    try:
        equipes_scraper = RotalogEquipesScraper(session=session or scraper.session)
        equipes = equipes_scraper.raspar_dia(day_br)
    except Exception as exc:
        LOG.debug("Não foi possível pré-carregar fechamento de equipes do dia (%s): %s", day_br, exc)

    # 1. Gera o documento consolidado do dia
    payload_km = estruturar_quilometragem_diaria(
        eventos, target_day=day_iso, empresa=empresa, equipes_records=equipes,
    )

    # 2. Salva localmente em dados-local/rotalog/quilometragem/diario/AAAA-MM-DD.json.gz
    dir_km_local = output_dir / "rotalog" / "quilometragem" / "diario"
    local_km_path = dir_km_local / f"{day_iso}.json.gz"
    salvar_arquivo_gzip(local_km_path, payload_km)
    LOG.debug("Arquivo diário de quilometragem salvo: %s", local_km_path)

    # 3. Gera relatório HTML diário da listagem de eventos
    relatorio_diario_path = gerar_relatorio_diario(
        payload_km, dir_km_local / f"{day_iso}-relatorio.html",
    )
    LOG.debug("Relatório diário HTML gerado em: %s", relatorio_diario_path)

    # 4. Upload de APENAS UM ARQUIVO para o Firebase Storage
    firebase_synced = False
    firebase_raw_synced = False
    if enable_firebase and firebase_store and firebase_store.enabled:
        try:
            remote_raw_blob = f"{firebase_store.root_prefix}/eventos/diario/{day_iso}.json.gz"
            firebase_store.save_blob(remote_raw_blob, raw_payload)
            firebase_raw_synced = True
            LOG.debug("Arquivo bruto diário sincronizado no Firebase: %s", remote_raw_blob)
        except Exception as exc:
            LOG.error("Erro ao sincronizar arquivo bruto diário no Firebase Storage: %s", exc)
        try:
            remote_blob = f"{firebase_store.root_prefix}/quilometragem/diario/{day_iso}.json.gz"
            firebase_store.save_blob(remote_blob, payload_km)
            firebase_synced = True
            LOG.debug("Arquivo diário consolidado sincronizado no Firebase: %s", remote_blob)
        except Exception as exc:
            LOG.error("Erro ao sincronizar arquivo diário no Firebase Storage: %s", exc)

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
        "date": day_iso,
        "durationSeconds": duration_s,
        "totalEquipes": payload_km["totalEquipes"],
        "totalServicos": payload_km["totalServicos"],
        "totalKmInformado": payload_km["totalKmInformado"],
        "totalKmAutorizadoFinal": payload_km["totalKmAutorizadoFinal"],
        "totalKmOficialInformado": payload_km["resumoEquipes"]["totais"]["kmInformado"],
        "totalKmOficialAutorizadoFinal": payload_km["resumoEquipes"]["totais"]["kmAutorizadoFinal"],
        "totalKmRecuperadoParecer": payload_km["resumoEquipes"]["totais"]["kmRecuperadoParecer"],
        "localArquivoKm": str(local_km_path),
        "localArquivoEventosBrutos": str(raw_path),
        "localRelatorioHtml": str(relatorio_diario_path),
        "firebaseSynced": firebase_synced,
        "firebaseRawSynced": firebase_raw_synced,
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
    arquivos_diarios_gerados: list[str] = []

    crawler_session = session
    if coletar_diarios and crawler_session is None:
        try:
            crawler = CrawlerRotalog()
            crawler_session = crawler.criar_sessao_autenticada()
        except Exception as exc:
            LOG.debug("Sessão autenticada não inicializada no crawler: %s", exc)

    if coletar_diarios:
        for dia in range(1, ultimo_dia + 1):
            target_date = datetime.date(ano, mes, dia)
            dia_br = target_date.strftime("%d/%m/%Y")
            pct = int((dia / ultimo_dia) * 100)
            prefix = f"[{mes:02d}/{ano:04d}] Dia {dia:02d}/{ultimo_dia:02d} ({pct:3d}%) {dia_br}"

            def cb_progresso_dia(sub_msg: str) -> None:
                atualizar_status_terminal(
                    f"{prefix} {sub_msg} | "
                    f"Serviços: {formatar_numero_br(total_servicos_acumulado, 0)} | "
                    f"KM Inf: {formatar_numero_br(total_km_inf_acumulado, 2)} | "
                    f"KM Aut: {formatar_numero_br(total_km_aut_acumulado, 2)}"
                )

            atualizar_status_terminal(
                f"{prefix} Coletando... | "
                f"Serviços: {formatar_numero_br(total_servicos_acumulado, 0)} | "
                f"KM Inf: {formatar_numero_br(total_km_inf_acumulado, 2)} | "
                f"KM Aut: {formatar_numero_br(total_km_aut_acumulado, 2)}"
            )

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
                total_servicos_acumulado += res_dia.get("totalServicos", 0)
                total_km_inf_acumulado += res_dia.get("totalKmInformado", 0.0)
                total_km_aut_acumulado += res_dia.get("totalKmAutorizadoFinal", 0.0)
                dias_processados += 1
                arquivos_diarios_gerados.append(res_dia["localRelatorioHtml"])
            except Exception as exc:
                LOG.debug("Falha na coleta do dia %s: %s", dia_br, exc)

            atualizar_status_terminal(
                f"{prefix} OK | "
                f"Serviços: {formatar_numero_br(total_servicos_acumulado, 0)} | "
                f"KM Inf: {formatar_numero_br(total_km_inf_acumulado, 2)} | "
                f"KM Aut: {formatar_numero_br(total_km_aut_acumulado, 2)}"
            )

        atualizar_status_terminal(
            f"[{mes:02d}/{ano:04d}] Consolidando fechamento mensal oficial de equipes ({ini_br} a {fim_br})..."
        )
    elif progresso:
        progresso(f"Iniciando fechamento mensal {mes_str} via tela Equipes")

    equipes_scraper = RotalogEquipesScraper(session=crawler_session)
    records = equipes_scraper.raspar_periodo(ini_br, fim_br, progresso=progresso)

    if progresso:
        progresso("Estruturando resumo oficial das equipes por contrato")

    resumo_equipes = estruturar_resumo_equipes(records, CONTRATOS_ALVO)
    emp_key = company_key(empresa)

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
        "diasProcessados": dias_processados,
        "diasTotal": ultimo_dia,
        "totalEquipes": len(resumo_equipes.get("totaisPorContrato", {})),
        "totalServicos": resumo_equipes["totais"]["servicosExecutados"] or total_servicos_acumulado,
        "totalKmInformado": resumo_equipes["totais"]["kmInformado"] or round(total_km_inf_acumulado, 2),
        "totalKmAutorizadoFinal": resumo_equipes["totais"]["kmAutorizadoFinal"] or round(total_km_aut_acumulado, 2),
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

    relatorio_path = gerar_relatorio_mensal(
        payload_mensal, dir_mensal_local / f"{mes_str}-relatorio.html",
    )
    LOG.debug("Relatório mensal HTML gerado em: %s", relatorio_path)

    # Upload de APENAS UM ARQUIVO mensal para o Firebase Storage
    firebase_synced = False
    erro_upload = None
    if enable_firebase:
        if progresso:
            progresso("Enviando consolidado mensal para a nuvem")
        try:
            if not firebase_store or not firebase_store.enabled:
                raise RuntimeError("Firebase solicitado, mas indisponível; arquivos locais preservados")
            remote_monthly_blob = f"{firebase_store.root_prefix}/quilometragem/mensal/{mes_str}.json.gz"
            firebase_store.save_blob(remote_monthly_blob, payload_mensal)
            firebase_synced = True
            LOG.debug("Arquivo único mensal sincronizado no Firebase: %s", remote_monthly_blob)
        except Exception as exc:
            erro_upload = str(exc)
            LOG.error("Erro ao sincronizar arquivo mensal no Firebase: %s", exc)

    atualizar_status_terminal(
        f"[{mes:02d}/{ano:04d}] Fechamento mensal e diário concluído com sucesso.",
        final=True,
    )

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
        "localRelatorioHtml": str(relatorio_path),
        "totalRelatoriosDiarios": len(arquivos_diarios_gerados),
        "erroUpload": erro_upload,
        "firebaseSynced": firebase_synced,
    }


def varrer_mes_anterior(
    output_dir: Path,
    empresa: str,
    enable_firebase: bool = False,
    firebase_store: typing.Any = None,
    progresso: typing.Callable[[str], None] | None = None,
    coletar_diarios: bool = True,
    session: requests.Session | None = None,
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
    )
