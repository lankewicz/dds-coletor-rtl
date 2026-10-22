"""Cliente independente: apenas pesquisa e paginação, sem integração com o daemon."""
from __future__ import annotations

import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

if __package__:
    from .identity_cache import IdentityCache
else:
    from identity_cache import IdentityCache

BASE = 'https://www.copel.com/rtlweb/paginas/'
URL = BASE + 'listagemEventos'
TABLES = {'turnos': 'form:tbEquipes', 'eventos': 'form:tbListagemEventos'}
AJAX = {'Faces-Request': 'partial/ajax', 'X-Requested-With': 'XMLHttpRequest'}
SCHEMA_VERSION = 2
DETAIL_FIELDS = ('Diário URL', 'Agência Nome completo',
                 'Eletricista 1 Nome completo', 'Eletricista 1 Matrícula',
                 'Eletricista 2 Nome completo', 'Eletricista 2 Matrícula')


def diary_url(href):
    url = urljoin(URL, href)
    parsed = urlparse(url)
    ids = parse_qs(parsed.query).get('id', [])
    if (parsed.scheme != 'https' or parsed.netloc != 'www.copel.com'
            or parsed.path != '/rtlweb/paginas/timeline'
            or len(ids) != 1 or not ids[0].isdigit()):
        raise RuntimeError('Link do diário ausente ou inesperado.')
    return url


def parse_details(doc, record):
    vehicle = record.get('Veículo', '')
    title = doc.title.get_text(' ', strip=True) if doc.title else ''
    if not vehicle or vehicle not in [part.strip() for part in title.split('|')]:
        raise RuntimeError(f'Diário incompatível com o veículo {vehicle}.')
    fields = {}
    for li in doc.find_all('li'):
        label, value = li.select_one('.project-title'), li.select_one('.project-detail')
        if label is not None and value is not None:
            key = re.sub(r'\s+', ' ', label.get_text(' ', strip=True)).rstrip(':').strip()
            fields[key] = re.sub(r'\s+', ' ', value.get_text(' ', strip=True)).strip()
    agency = fields.get('Agência', '')
    code, separator, name = agency.partition(' - ')
    if not separator or not name.strip() or code.strip() != record.get('Agência', '').strip():
        raise RuntimeError(f'Agência ausente ou divergente no diário de {vehicle}.')
    result = {'Agência Nome completo': name.strip()}
    for number in (1, 2):
        label = f'Eletricista {number}'
        if label not in fields:
            raise RuntimeError(f'{label} ausente no diário de {vehicle}.')
        value = fields[label]
        match = re.fullmatch(r'(\d+)\s*-\s*(\S.*)', value)
        if match:
            result[label + ' Matrícula'] = match[1]
            result[label + ' Nome completo'] = match[2].strip()
        elif value in ('', '-', '*') and record.get(label, '').strip() in ('', '-', '*'):
            result[label + ' Matrícula'] = ''
            result[label + ' Nome completo'] = ''
        else:
            raise RuntimeError(f'Nome ou matrícula não reconhecidos em {label} de {vehicle}.')
    return result


def soup(html):
    return BeautifulSoup(html, 'html.parser')


def view_state(doc):
    field = doc.find('form', id='form')
    field = (field or doc).find('input', attrs={'name': 'javax.faces.ViewState'})
    if not field:
        raise RuntimeError('Sessão expirada ou ViewState ausente. O dia não será concluído.')
    return field['value']


def partial(text, component, previous_state):
    root = ET.fromstring(text)
    if root.find('.//error') is not None or root.find('.//redirect') is not None:
        raise RuntimeError('Portal retornou erro ou redirecionamento durante a paginação.')
    content = None
    state = previous_state
    for update in root.iter('update'):
        key = update.get('id', '')
        if key == component:
            content = update.text or ''
        if 'javax.faces.ViewState' in key:
            state = (update.text or '').strip() or state
    if content is None:
        raise RuntimeError(f'Paginação sem atualização de {component}.')
    doc = soup(content)
    # PrimeFaces devolve apenas <tr> nas atualizações de paginação.
    if doc.find(id=component + '_data') is None and doc.find('tr', recursive=False):
        doc = soup(f'<table><tbody id="{component}_data">{content}</tbody></table>')
    return doc, state


def table_spec(doc, component):
    table = doc.find(id=component)
    if table is None:
        raise RuntimeError(f'Tabela ausente: {component}')
    head = table.find('thead')
    headers = []
    for th in head.find_all('th') if head else []:
        title = th.select_one('.ui-column-title')
        label = (title or th).get_text(' ', strip=True)
        label = re.sub(r'\s+', ' ', label).strip()
        if not label or label in headers:
            raise RuntimeError(f'Cabeçalho vazio ou repetido em {component}')
        headers.append(label)
    if not headers:
        raise RuntimeError(f'Cabeçalho ausente: {component}')
    script = next((s.get_text() for s in doc.find_all('script')
                   if f'id:"{component}"' in s.get_text() and 'rowCount' in s.get_text()), '')
    count = re.search(r'rowCount\s*:\s*(\d+)', script)
    size = re.search(r'rows\s*:\s*(\d+)', script)
    if not count or not size or int(size[1]) <= 0:
        raise RuntimeError(f'Total ou tamanho de página ausente: {component}')
    return headers, int(count[1]), int(size[1])


def rows(doc, component, headers, offset=None, include_diary=False):
    body = doc.find(id=component + '_data')
    if body is None:
        raise RuntimeError(f'Corpo da tabela ausente: {component}')
    result = []
    for tr in body.find_all('tr', recursive=False):
        if 'ui-datatable-empty-message' in tr.get('class', []):
            continue
        if offset is not None and tr.get('data-ri') != str(offset + len(result)):
            raise RuntimeError(f'Índice de linha inesperado em {component}; página repetida ou incompleta.')
        cells = tr.find_all('td', recursive=False)
        if len(cells) != len(headers):
            raise RuntimeError(f'Colunas divergentes em {component}: {len(cells)}/{len(headers)}')
        values = []
        for cell in cells:
            cell = soup(str(cell)).td
            for title in cell.select('.ui-column-title'):
                title.decompose()
            values.append(cell.get_text(' ', strip=True))
        record = dict(zip(headers, values))
        if include_diary:
            link = cells[0].find('a', href=True)
            record['Diário URL'] = diary_url(link['href'] if link else '')
        result.append(record)
    return result


def summary(doc):
    result = []
    for icon in doc.select('i.fa'):
        box = icon.parent
        value = box.select_one('.FontBold')
        if value is None:
            continue
        result.append({'valor': value.get_text(' ', strip=True),
                       'texto': box.get_text(' ', strip=True)})
    if len(result) != 15:
        raise RuntimeError(f'Resumo inesperado: {len(result)} indicadores, esperados 15.')
    return result


class Scraper:
    def __init__(self, usuario, senha, verify=True, delay=0.5, notify=lambda **kw: None, identity_cache=None):
        if not usuario or not senha:
            raise RuntimeError('Defina ROTALOG_USUARIO e ROTALOG_SENHA no .env.')
        self.usuario, self.senha = usuario, senha
        self.session = requests.Session()
        self.session.verify = verify
        self.session.headers['User-Agent'] = 'Mozilla/5.0'
        self.delay, self.notify = delay, notify
        self.identity_cache = identity_cache if identity_cache is not None else IdentityCache()

    def enrich(self, records, archive):
        consulted = reused = 0
        pages = {}
        self.notify(equipes_total=len(records), equipes_feitas=0, equipe_index=0,
                    equipe='—', equipe_etapa=0, equipe_acao='Aguardando equipe')
        for index, record in enumerate(records, 1):
            self.notify(etapa='Profissionais e agência', pagina=index, paginas=len(records),
                        linhas=index - 1, detalhes_consultados=consulted, detalhes_reutilizados=reused,
                        equipe_index=index, equipes_feitas=index - 1, equipe=record.get('Veículo', '—'),
                        equipe_etapa=0, equipe_acao='Verificando composição')
            cached = self.identity_cache.lookup(record)
            self.notify(equipe_etapa=1, equipe_acao='Reaproveitando identificação' if cached else 'Consultando diário')
            if cached:
                record.update(cached['detalhes'])
                record['Detalhes fonte'] = 'cache'
                record['Detalhes diário consultado'] = cached['diario']
                reused += 1
            else:
                url = diary_url(record['Diário URL'])
                if url not in pages:
                    response = self.request('GET', url)
                    archive(f'diario-{parse_qs(urlparse(url).query)["id"][0]}.html', response.text)
                    pages[url] = soup(response.text)
                    consulted += 1
                self.notify(equipe_etapa=2, equipe_acao='Validando nomes, matrículas e agência')
                details = parse_details(pages[url], record)
                self.identity_cache.remember(record, details)
                record.update(details)
                record['Detalhes fonte'] = 'diario'
                record['Detalhes diário consultado'] = url
            self.notify(equipe_etapa=3, equipe_acao='Concluída pelo cache' if cached else 'Concluída pelo diário',
                        equipes_feitas=index, linhas=index,
                        detalhes_consultados=consulted, detalhes_reutilizados=reused)
        self.notify(detalhes_consultados=consulted, detalhes_reutilizados=reused, linhas=len(records))
        return {'consultados': consulted, 'reutilizados': reused}

    def close(self):
        self.session.close()

    def request(self, method, url, **kwargs):
        time.sleep(self.delay)
        response = self.session.request(method, url, timeout=(15, 90), **kwargs)
        response.raise_for_status()
        # O portal envia HTML em UTF-8, mesmo quando o header HTTP omite charset.
        response.encoding = 'utf-8'
        return response

    def login(self):
        self.request('GET', BASE + 'dashboard')
        response = self.request('POST', BASE + 'j_security_check',
                                data={'j_username': self.usuario, 'j_password': self.senha})
        if 'j_security_check' in response.text or soup(response.text).find('input', attrs={'type': 'password'}):
            raise PermissionError('Falha na autenticação do RTLWeb.')

    def collect(self, day, archive):
        self.notify(etapa='Pesquisa', pagina=0, paginas=0, linhas=0)
        response = self.request('GET', URL)
        doc = soup(response.text)
        state = view_state(doc)
        form = doc.find('form', id='form')
        dates = [i['name'] for i in form.select('input[name]') if 'dataInicial' in i['name'] or 'dataFinal' in i['name']]
        if len(dates) != 2:
            raise RuntimeError('Campos de data não reconhecidos.')
        button = next((b for b in form.find_all('button') if b.get_text(strip=True) == 'Pesquisar'), None)
        if button is None or not button.get('name'):
            raise RuntimeError('Botão Pesquisar ausente.')
        payload = {i['name']: i.get('value', '') for i in form.select('input[type=hidden][name]')}
        payload.update({name: day.strftime('%d/%m/%Y') for name in dates})
        payload.update({'form': 'form', 'form:veiculo': '', 'form:contrato_input': '',
                        button['name']: '', 'javax.faces.ViewState': state})
        action = urljoin(URL, form.get('action') or URL)
        if not action.startswith(BASE):
            raise RuntimeError('Destino inesperado para a pesquisa.')
        response = self.request('POST', action, data=payload)
        archive('pesquisa.html', response.text)
        doc = soup(response.text)
        state = view_state(doc)
        # Não aceitar uma pesquisa que tenha perdido as datas solicitadas.
        for name in dates:
            field = doc.find('input', attrs={'name': name})
            if field is None or field.get('value') != day.strftime('%d/%m/%Y'):
                raise RuntimeError('O portal não confirmou a data pesquisada.')
        result = {'data': day.isoformat(), 'fonte': URL,
                  'coletadoEm': datetime.now().astimezone().isoformat(), 'resumo': summary(doc)}
        specs = {name: table_spec(doc, component) for name, component in TABLES.items()}
        self.notify(equipes_total=specs['turnos'][1], equipes_feitas=0)
        result['paginacao'] = {}
        for name, component in TABLES.items():
            headers, count, size = specs[name]
            records = []
            pages = max(1, (count + size - 1) // size)
            for page in range(pages):
                current = doc
                if page:
                    ajax = {key: value for key, value in payload.items() if key != button['name']}
                    ajax.update({'javax.faces.partial.ajax': 'true', 'javax.faces.source': component,
                                 'javax.faces.partial.execute': component, 'javax.faces.partial.render': component,
                                 component: component, component + '_pagination': 'true',
                                 component + '_first': str(page * size), component + '_rows': str(size),
                                 component + '_encodeFeature': 'true', 'javax.faces.ViewState': state})
                    response = self.request('POST', action, data=ajax, headers=AJAX)
                    archive(f'{name}-{page + 1:05d}.xml', response.text)
                    current, state = partial(response.text, component, state)
                batch = rows(current, component, headers, offset=page * size, include_diary=name == 'turnos')
                expected = min(size, max(0, count - page * size))
                if len(batch) != expected:
                    raise RuntimeError(f'{name}, página {page + 1}: {len(batch)} linhas; esperadas {expected}.')
                records.extend(batch)
                self.notify(etapa=name.title(), pagina=page + 1, paginas=pages, linhas=len(records), total=count)
            if len(records) != count:
                raise RuntimeError(f'Contagem divergente em {name}.')
            result[name] = records
            result['paginacao'][name] = {'total': count, 'paginas': pages, 'colunas': headers}
        # Abrir os diários depois da paginação evita invalidar a ViewState da listagem.
        result['detalhes'] = self.enrich(result['turnos'], archive)
        result['paginacao']['turnos']['colunas'] += list(DETAIL_FIELDS) + ['Detalhes fonte', 'Detalhes diário consultado']
        result['schemaVersion'] = SCHEMA_VERSION
        result['completo'] = True
        return result
