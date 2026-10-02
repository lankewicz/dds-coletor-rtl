"""Clientes de raspagem das telas históricas do ROTALOG."""

from __future__ import annotations

import logging
import math
import re
import typing
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .client import CrawlerRotalog
from .historico_parsing import parse_float_br
from .historico_terminal import formatar_numero_br

LOG = logging.getLogger("coletor-historico")
URL_BASE = "https://www.copel.com/rtlweb"
URL_LISTAGEM_EVENTOS = f"{URL_BASE}/paginas/listagemEventos"
URL_EQUIPES = f"{URL_BASE}/paginas/equipes"
EQUIPES_HEADERS = (
    "Veiculo", "Tablet", "Agencia", "Contrato", "Data Referencia - Turno",
    "Eletricista 1", "Eletricista 2", "Servicos executados",
    "Informado com limitador (km)", "Glosado critico (km)",
    "Glosado por parecer (km)", "Autorizado (km)",
    "Recuperados por Parecer (km)", "Autorizado Final (km)", "Diferenca",
    "Aguardando justificativa", "Em analise", "Alertas",
)

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

        resp_page = self.session.get(URL_LISTAGEM_EVENTOS, timeout=30)
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

        resp_search = self.session.post(URL_LISTAGEM_EVENTOS, data=post_data, timeout=60)
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
                resp_ajax = self.session.post(URL_LISTAGEM_EVENTOS, data=ajax_data, headers=headers_ajax, timeout=30)
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
            response = self.session.get(urljoin(URL_EQUIPES, href), timeout=30)
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
                timeout=30,
            )
        if response is None:
            return {}, view_state
        response.raise_for_status()
        detail_soup = BeautifulSoup(response.content, "html.parser")
        state_update = detail_soup.find("update", {"id": "javax.faces.ViewState"})
        new_state = state_update.get_text().strip() if state_update else view_state
        return self._parse_electricians(detail_soup), new_state

    def _enrich_team_details(
        self, records: list[dict[str, str]], view_state: str,
        progresso: typing.Callable[[str], None] | None = None,
    ) -> str:
        for index, record in enumerate(records, 1):
            if progresso:
                progresso(f"Profissionais {index}/{len(records)} | Equipe {record.get('Veiculo', '')}")
            try:
                details, view_state = self._fetch_team_details(record, view_state)
                record.update(details)
            except Exception as exc:
                LOG.warning("Falha ao consultar detalhes da equipe %s: %s", record.get("Veiculo"), exc)
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
        response = self.session.get(URL_EQUIPES, timeout=30)
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
        searched = self.session.post(URL_EQUIPES, data=search_data, timeout=60)
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
            paged = self.session.post(URL_EQUIPES, data=ajax_data, headers=ajax_headers, timeout=30)
            paged.raise_for_status()
            ajax_soup = BeautifulSoup(paged.content, "html.parser")
            update = ajax_soup.find("update", {"id": "form:tbEquipes"})
            vs_update = ajax_soup.find("update", {"id": "javax.faces.ViewState"})
            if vs_update and vs_update.get_text().strip():
                current_state = vs_update.get_text().strip()
            if update:
                page_rows = self._parse_rows(BeautifulSoup(update.get_text(), "html.parser"))
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

        # Os detalhes usam o mesmo ViewState da tabela. Abrir cada equipe durante a
        # paginação invalida as requisições das páginas seguintes; por isso a lista
        # completa é carregada primeiro e somente então os detalhes são consultados.
        if enriquecer_detalhes:
            self._enrich_team_details(records, current_state, progresso=progresso)

        LOG.debug("Fechamentos de equipes obtidos: %d registros em %d páginas", len(records), total_pages)
        return records

    def raspar_dia(
        self,
        data_br: str,
        progresso: typing.Callable[[str], None] | None = None,
        enriquecer_detalhes: bool = False,
    ) -> list[dict[str, str]]:
        return self.raspar_periodo(
            data_br,
            data_br,
            progresso=progresso,
            enriquecer_detalhes=enriquecer_detalhes,
        )
