"""Validação de falhas que poderiam gerar exportações incompletas."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from coleta_unica.main import complete, output_lock, save_day
from coleta_unica.scraper import partial, rows, soup, table_spec, parse_details, diary_url, SCHEMA_VERSION


class ExportTests(unittest.TestCase):
    def test_partial_rotates_state_and_keeps_cdata(self):
        doc, state = partial('''<partial-response><changes>
            <update id="form:tbEquipes"><![CDATA[<tbody id="form:tbEquipes_data"><tr><td>E001</td></tr></tbody>]]></update>
            <update id="j_id1:javax.faces.ViewState:1"><![CDATA[new]]></update>
            </changes></partial-response>''', 'form:tbEquipes', 'old')
        self.assertEqual(state, 'new')
        self.assertEqual(rows(doc, 'form:tbEquipes', ['Veículo']), [{'Veículo': 'E001'}])

    def test_missing_update_and_redirect_are_errors(self):
        for xml in ('<partial-response><changes/></partial-response>',
                    '<partial-response><redirect url="login"/></partial-response>'):
            with self.assertRaises(RuntimeError):
                partial(xml, 'form:tbEquipes', 'old')

    def test_primefaces_partial_rows_and_repeated_page(self):
        doc, state = partial('''<partial-response><changes>
            <update id="t"><![CDATA[<tr data-ri="25"><td>E001</td></tr>]]></update>
            </changes></partial-response>''', 't', 'old')
        self.assertEqual(len(rows(doc, 't', ['Veículo'], offset=25)), 1)
        with self.assertRaises(RuntimeError):
            rows(doc, 't', ['Veículo'], offset=50)

    def test_missing_total_never_defaults_to_one_page(self):
        with self.assertRaises(RuntimeError):
            table_spec(soup('<div id="t"><thead><th>Veículo</th></thead></div>'), 't')

    def test_empty_table_and_hidden_columns(self):
        empty = soup('<tbody id="t_data"><tr class="ui-datatable-empty-message"><td colspan="2">Nenhum registro encontrado</td></tr></tbody>')
        self.assertEqual(rows(empty, 't', ['Veículo', 'Tablet']), [])
        doc = soup('<tbody id="t_data"><tr><td><span class="ui-column-title">Veículo</span>E001</td><td style="display:none">000123</td></tr></tbody>')
        self.assertEqual(rows(doc, 't', ['Veículo', 'Tablet'])[0]['Tablet'], '000123')
        with self.assertRaises(RuntimeError):
            rows(doc, 't', ['Veículo'])

    def test_resume_requires_receipt_counts_and_csvs(self):
        day = date(2026, 1, 1)
        result = {'completo': True, 'schemaVersion': SCHEMA_VERSION, 'data': day.isoformat(),
                  'resumo': [{'valor': '0', 'texto': 'indicador'}] * 15,
                  'turnos': [], 'eventos': [],
                  'paginacao': {k: {'total': 0, 'colunas': ['Veículo']} for k in ('turnos', 'eventos')}}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            self.assertFalse(complete(folder, day))
            save_day(folder, result)
            self.assertTrue(complete(folder, day))
            legacy = dict(result)
            legacy.pop('schemaVersion')
            (folder / 'dados.json').write_text(json.dumps(legacy), encoding='utf-8')
            self.assertFalse(complete(folder, day))
            save_day(folder, result)
            self.assertTrue((folder / 'eventos.csv').read_bytes().startswith(b'\xef\xbb\xbf'))
            result['paginacao']['eventos']['total'] = 1
            (folder / 'dados.json').write_text(json.dumps(result), encoding='utf-8')
            self.assertFalse(complete(folder, day))

    def test_output_lock_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            with output_lock(Path(temp)):
                with self.assertRaises(RuntimeError):
                    with output_lock(Path(temp)):
                        pass
            with output_lock(Path(temp)):
                pass

    def test_details_preserve_registration_and_agency(self):
        html = '''<title>RotaLog | E001 | *</title><ul>
          <li><span class="project-title">Agência:</span><span class="project-detail">ABC - AGÊNCIA TESTE</span></li>
          <li><span class="project-title">Eletricista \n 1:</span><span class="project-detail">00123 - ANA MARIA TESTE</span></li>
          <li><span class="project-title">Eletricista 2:</span><span class="project-detail">-</span></li>
          </ul><div>Eletricista(s): IGNORAR CONTEÚDO DO MODAL</div>'''
        record = {'Veículo': 'E001', 'Agência': 'ABC', 'Eletricista 1': 'ANA', 'Eletricista 2': ''}
        details = parse_details(soup(html), record)
        self.assertEqual(details['Eletricista 1 Matrícula'], '00123')
        self.assertEqual(details['Eletricista 1 Nome completo'], 'ANA MARIA TESTE')
        self.assertEqual(details['Agência Nome completo'], 'AGÊNCIA TESTE')
        self.assertEqual(details['Eletricista 2 Matrícula'], '')
        for replacement in (dict(record, **{'Veículo': 'E002'}),
                            dict(record, **{'Agência': 'OUTRA'}),
                            dict(record, **{'Eletricista 2': 'JOÃO'})):
            with self.assertRaises(RuntimeError):
                parse_details(soup(html), replacement)

    def test_diary_links_must_reference_portal_timeline(self):
        self.assertEqual(diary_url('/rtlweb/paginas/timeline?id=123'),
                         'https://www.copel.com/rtlweb/paginas/timeline?id=123')
        for href in ('', 'https://example.com/timeline?id=123', '/rtlweb/paginas/timeline',
                     '/rtlweb/paginas/timeline?id=invalid'):
            with self.assertRaises(RuntimeError):
                diary_url(href)


if __name__ == '__main__':
    unittest.main()
