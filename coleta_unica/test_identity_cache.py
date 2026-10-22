import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from coleta_unica.identity_cache import IdentityCache
from coleta_unica.scraper import Scraper


def record(**changes):
    result = {'Veículo': 'E001', 'Contrato': '123', 'Agência': 'ABC',
              'Eletricista 1': 'ANA', 'Eletricista 2': 'BEA',
              'Diário URL': 'https://www.copel.com/rtlweb/paginas/timeline?id=1'}
    result.update(changes)
    return result


def details():
    return {'Agência Nome completo': 'AGÊNCIA TESTE',
            'Eletricista 1 Nome completo': 'ANA TESTE', 'Eletricista 1 Matrícula': '00123',
            'Eletricista 2 Nome completo': 'BEA TESTE', 'Eletricista 2 Matrícula': '00456'}


HTML = '''<title>RotaLog | E001 | *</title><ul>
<li><span class="project-title">Agência:</span><span class="project-detail">ABC - AGÊNCIA TESTE</span></li>
<li><span class="project-title">Eletricista 1:</span><span class="project-detail">00123 - ANA TESTE</span></li>
<li><span class="project-title">Eletricista 2:</span><span class="project-detail">00456 - BEA TESTE</span></li></ul>'''


class IdentityTests(unittest.TestCase):
    def test_unchanged_composition_opens_only_first_diary(self):
        with ScraperContext() as scraper:
            scraper.request = Mock(return_value=SimpleNamespace(text=HTML))
            first, second = record(), record(**{'Diário URL': record()['Diário URL'][:-1] + '2'})
            stats = scraper.enrich([first, second], lambda *args: None)
            self.assertEqual(stats, {'consultados': 1, 'reutilizados': 1})
            self.assertEqual(scraper.request.call_count, 1)
            self.assertEqual(second['Eletricista 1 Matrícula'], '00123')
            self.assertEqual(second['Detalhes fonte'], 'cache')
            self.assertEqual(second['Detalhes diário consultado'], first['Diário URL'])
            self.assertNotEqual(second['Diário URL'], first['Diário URL'])
            changed = record(**{'Eletricista 2': 'CARLA', 'Diário URL': record()['Diário URL'][:-1] + '3'})
            scraper.request.return_value = SimpleNamespace(text=HTML.replace('00456 - BEA TESTE', '00789 - CARLA TESTE'))
            scraper.enrich([changed], lambda *args: None)
            self.assertEqual(scraper.request.call_count, 2)
            self.assertEqual(changed['Eletricista 2 Matrícula'], '00789')

    def test_scope_changes_and_homonyms_do_not_reuse(self):
        cache = IdentityCache()
        cache.remember(record(), details())
        for field in ('Veículo', 'Contrato', 'Agência', 'Eletricista 1', 'Eletricista 2'):
            self.assertIsNone(cache.lookup(record(**{field: 'OUTRO'})))
        changed = dict(details(), **{'Eletricista 1 Matrícula': '99999'})
        cache.remember(record(), changed)
        self.assertIsNone(cache.lookup(record()))
        with ScraperContext(cache) as scraper:
            scraper.request = Mock(return_value=SimpleNamespace(text=HTML))
            self.assertEqual(scraper.enrich([record()], lambda *args: None)['consultados'], 1)

    def test_persistence_seed_and_unknown_names(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            daily = folder / '2026-01-01' / 'dados.json'
            daily.parent.mkdir()
            known = dict(record(), **details())
            daily.write_text(json.dumps({'schemaVersion': 2, 'completo': True, 'turnos': [known]}), encoding='utf-8')
            cache = IdentityCache(folder / 'cache.json')
            cache.seed(folder)
            loaded = IdentityCache(folder / 'cache.json')
            self.assertEqual(loaded.lookup(record())['detalhes']['Eletricista 1 Matrícula'], '00123')
            unknown = record(**{'Eletricista 1': '*'})
            self.assertFalse(loaded.remember(unknown, details()))
            self.assertIsNone(loaded.lookup(unknown))

    def test_inferred_records_cannot_create_new_observations(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            daily = folder / '2026-01-01' / 'dados.json'
            daily.parent.mkdir()
            inferred = dict(record(), **details(), **{'Detalhes fonte': 'cache'})
            daily.write_text(json.dumps({'schemaVersion': 2, 'completo': True, 'turnos': [inferred]}), encoding='utf-8')
            cache = IdentityCache()
            cache.seed(folder)
            self.assertIsNone(cache.lookup(record()))


class ScraperContext:
    def __init__(self, cache=None):
        self.scraper = Scraper('test', 'test', delay=0, identity_cache=cache)

    def __enter__(self):
        return self.scraper

    def __exit__(self, *args):
        self.scraper.close()


if __name__ == '__main__':
    unittest.main()
