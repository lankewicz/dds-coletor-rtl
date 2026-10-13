import copy
import tempfile
import unittest
from pathlib import Path
from torre_contract import publish_projection, validate_projection

NOW = "2026-10-09T10:00:00-03:00"


def projection():
    return {
        "schemaVersion": 1, "company": "ChicoEletro", "collectedAt": NOW,
        "composedAt": NOW, "stale": False,
        "sources": {name: {"updatedAt": NOW, "stale": False}
                    for name in ("rotalog", "cadastro", "dds")},
        "items": [{"teamKey": "E3733", "equipe": "E3733", "setor": "TODOS",
                   "estado": "FECHADO", "turnStatus": "FECHADO", "active": True,
                   "updatedAt": NOW, "participantes": ["Pessoa de teste"],
                   "operacional": {}, "ddsHistory": ["ok"], "ddsDays": ["2026-10-09"],
                   "ddsTimes": {"2026-10-09": "08:00"}, "ddsToday": "ok"}],
        "events": [{"id": "event-1", "teamKey": "E3733", "type": "turno_fechado", "occurredAt": NOW}],
    }


class ContractTests(unittest.TestCase):
    def test_complete_and_explicit_empty_are_valid(self):
        data = projection()
        self.assertIs(validate_projection(data, "ChicoEletro"), data)
        data.update(items=[], events=[])
        validate_projection(data, "ChicoEletro")

    def test_missing_sources_wrong_company_and_naive_time(self):
        for edit in [lambda d: d["sources"].pop("cadastro"),
                     lambda d: d.update(company="Outra"),
                     lambda d: d.update(collectedAt="2026-10-09T10:00:00"),
                     lambda d: d["sources"]["dds"].update(stale=True),
                     lambda d: d.update(schemaVersion=True)]:
            data = projection()
            edit(data)
            with self.assertRaises(ValueError):
                validate_projection(data, "ChicoEletro")

    def test_duplicate_identity_and_invalid_history_or_event(self):
        for edit in [lambda d: d["items"].append(copy.deepcopy(d["items"][0])),
                     lambda d: d["items"][0].update(ddsDays=[]),
                     lambda d: d["items"][0].pop("participantes"),
                     lambda d: d["events"].append(copy.deepcopy(d["events"][0])),
                     lambda d: d["events"][0].update(teamKey="UNKNOWN"),
                     lambda d: d["events"][0].update(occurredAt="unknown")]:
            data = projection()
            edit(data)
            with self.assertRaises(ValueError):
                validate_projection(data, "ChicoEletro")

    def test_failed_validation_preserves_last_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "torre/current.json"
            data = projection()
            publish_projection(path, data, "ChicoEletro")
            saved = path.read_bytes()
            data["sources"].pop("dds")
            with self.assertRaises(ValueError):
                publish_projection(path, data, "ChicoEletro")
            self.assertEqual(path.read_bytes(), saved)
            self.assertFalse(path.with_name("current.json.tmp").exists())
