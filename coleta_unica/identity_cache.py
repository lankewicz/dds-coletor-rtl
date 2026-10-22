"""Identificações observadas nos diários, separadas por veículo e composição."""
from __future__ import annotations

import json
from pathlib import Path

if __package__:
    from .storage import atomic_text
else:
    from storage import atomic_text

KEY_FIELDS = ('Veículo', 'Contrato', 'Agência', 'Eletricista 1', 'Eletricista 2')
VALUE_FIELDS = ('Agência Nome completo', 'Eletricista 1 Nome completo',
                'Eletricista 1 Matrícula', 'Eletricista 2 Nome completo', 'Eletricista 2 Matrícula')


def composition_key(record):
    values = tuple(' '.join(str(record.get(k, '')).split()).upper() for k in KEY_FIELDS)
    unknown = {'*', 'NULL', 'NÃO IDENTIFICADO', 'NAO IDENTIFICADO'}
    if not all(values[:4]) or any(value in unknown for value in values):
        return None
    return values


class IdentityCache:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.entries = {}
        if self.path and self.path.exists():
            try:
                document = json.loads(self.path.read_text(encoding='utf-8'))
                if document['version'] != 1:
                    raise ValueError('Versão de cache inesperada')
                for item in document['observacoes']:
                    key = tuple(item['chave'])
                    if len(key) != len(KEY_FIELDS) or not all(isinstance(v, str) for v in key):
                        raise ValueError('Chave inválida')
                    if not self.valid(item['detalhes']) or not isinstance(item['diario'], str):
                        raise ValueError('Identificação inválida')
                    self.entries.setdefault(key, []).append({'detalhes': item['detalhes'], 'diario': item['diario']})
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f'Cache de profissionais inválido, arquivo preservado: {self.path}') from exc

    @staticmethod
    def valid(details):
        if not isinstance(details, dict) or not all(isinstance(details.get(k), str) for k in VALUE_FIELDS):
            return False
        if not details['Agência Nome completo']:
            return False
        for number in (1, 2):
            name, registration = details[f'Eletricista {number} Nome completo'], details[f'Eletricista {number} Matrícula']
            if bool(name) != bool(registration) or (registration and not registration.isdigit()):
                return False
        return True

    def lookup(self, record):
        variants = self.entries.get(composition_key(record), [])
        if len(variants) != 1:
            return None
        return {'detalhes': dict(variants[0]['detalhes']), 'diario': variants[0]['diario']}

    def remember(self, record, details, persist=True):
        key = composition_key(record)
        values = {k: details.get(k) for k in VALUE_FIELDS}
        if key is None or not self.valid(values):
            return False
        variants = self.entries.setdefault(key, [])
        if any(v['detalhes'] == values for v in variants):
            return False
        variants.append({'detalhes': values, 'diario': record['Diário URL']})
        if persist:
            self.save()
        return True

    def seed(self, folder):
        changed = False
        for path in sorted(Path(folder).glob('????-??-??/dados.json')):
            try:
                document = json.loads(path.read_text(encoding='utf-8'))
                if document.get('completo') is not True or document.get('schemaVersion') != 2:
                    continue
                for record in document.get('turnos', []):
                    # Dados reaproveitados não contam como uma nova observação do portal.
                    if record.get('Detalhes fonte', 'diario') == 'diario' and record.get('Diário URL'):
                        changed = self.remember(record, record, persist=False) or changed
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if changed:
            self.save()

    def save(self):
        if self.path is None:
            return
        document = {'version': 1, 'observacoes': [dict(chave=list(key), **variant)
                    for key, variants in sorted(self.entries.items()) for variant in variants]}
        atomic_text(self.path, json.dumps(document, ensure_ascii=False, indent=2))
