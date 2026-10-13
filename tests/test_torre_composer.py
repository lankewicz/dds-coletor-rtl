import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from coletor.storage_io import load_json
from coletor.torre_composer import TorreComposer, compose_cards
from tests.test_torre_inputs import catalog, daily

NOW = datetime.fromisoformat("2026-10-10T10:00:00-03:00")


def sources():
    cat = catalog()
    cat["teams"]["E3733"]["aliases"].append("E3733(2)")
    return {"company": "ChicoEletro", "catalog": {"data": cat},
            "daily": {"2026-10-10": {"data": daily()}},
            "sources": {name: {"updatedAt": NOW.isoformat(), "stale": False} for name in ("cadastro", "dds")}}


def index(status="ABERTO"):
    return {"company": "ChicoEletro", "lastCollectedAt": NOW.isoformat(), "summary": {},
            "equipes": {"E3733": {"teamKey": "E3733", "teamId": "TEAM-1", "updatedAt": NOW.isoformat(),
                "conexao": {"isOnline": True, "colaborador": "Pessoa / Outra pessoa", "veiculo": "V1"},
                "jornada": {"turno": {"status": status, "inicio": "2026-10-10T08:00:00-03:00"}},
                "ordensServico": {"atual": None}, "executadosHoje": {"comercial": 2, "emergencia": 1},
                "fila": {"comercial": 3, "emergencia": 2}}}}


class ComposerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.composer = TorreComposer(self.root, "ChicoEletro")

    def test_telemetry_metadata_counters_and_dds_history(self):
        data = self.composer.publish(index(), sources(), now=NOW)
        item = data["items"][0]
        self.assertEqual(item["participantes"], ["Pessoa", "Outra pessoa"])
        self.assertEqual(item["setor"], "SUL")
        self.assertEqual(item["ssExecutadasCount"], 3)
        self.assertEqual(item["ddsHistory"], ["ok"])
        self.assertEqual(item["fila"]["emergencia"], 2)
        self.assertFalse(item["cadastroActive"])
        self.assertTrue(item["active"])
        self.assertEqual(item["teamId"], "TEAM-1")
        self.assertEqual(data["events"], [])

    def test_first_closed_does_not_invent_event(self):
        first = self.composer.publish(index("FECHADO"), sources(), now=NOW)
        second = TorreComposer(self.root, "ChicoEletro").publish(index("FECHADO"), sources(), now=NOW+timedelta(minutes=3))
        self.assertEqual(first["events"], [])
        self.assertEqual(second["events"], [])

    def test_real_close_keeps_event_id_and_time_after_restart(self):
        self.composer.publish(index(), sources(), now=NOW)
        closed = index("FECHADO")
        closed["equipes"]["E3733"]["jornada"]["turno"]["fim"] = (NOW+timedelta(minutes=2)).isoformat()
        after = self.composer.publish(closed, sources(), now=NOW+timedelta(minutes=3))
        repeat = TorreComposer(self.root, "ChicoEletro").publish(closed, sources(), now=NOW+timedelta(minutes=6))
        self.assertEqual(len(after["events"]), 1)
        self.assertEqual(after["events"], repeat["events"])
        self.assertEqual(after["events"][0]["occurredAt"], "2026-10-10T13:02:00+00:00")

    def test_dds_only_closes_at_18_without_rotalog_change(self):
        empty = index()
        empty["equipes"] = {}
        before = self.composer.publish(empty, sources(), now=NOW.replace(hour=17))
        self.assertEqual(before["items"][0]["turnStatus"], "ABERTO")
        after = self.composer.publish(empty, sources(), now=NOW.replace(hour=18))
        self.assertEqual(after["items"][0]["turnoFim"], "2026-10-10T18:00:00-03:00")
        self.assertEqual(after["events"][0]["type"], "turno_fechado")

    def test_inactive_no_dds_and_midnight_active_service(self):
        src = sources()
        src["daily"] = {}
        empty = index()
        empty["equipes"] = {}
        items, _ = compose_cards(empty, src, "ChicoEletro", NOW)
        self.assertFalse(items[0]["active"])
        self.assertEqual(items[0]["ddsHistory"], [])
        live = index()
        team = live["equipes"]["E3733"]
        team["jornada"]["turno"]["inicio"] = "2026-10-09T23:00:00-03:00"
        team["ordensServico"]["atual"] = {"statusAtual": "EXECUCAO", "protocolo": "123"}
        items, _ = compose_cards(live, src, "ChicoEletro", NOW.replace(hour=1))
        self.assertEqual(items[0]["turnStatus"], "ABERTO")

    def test_alias_and_ambiguous_alias(self):
        src = sources()
        src["daily"]["2026-10-10"]["data"]["teams"] = {"ALIAS": {"completedAt": NOW.isoformat()}}
        result = self.composer.publish(index(), src, now=NOW)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["ddsToday"], "ok")
        src["catalog"]["data"]["teams"]["E9999"] = {**src["catalog"]["data"]["teams"]["E3733"], "teamKey": "E9999"}
        with self.assertRaises(ValueError):
            self.composer.publish(index(), src, now=NOW)

    def test_service_transition_and_completion_persist(self):
        service = {"serviceId": "ss-1", "statusAtual": "DESLOCAMENTO", "inicioDeslocamento": NOW.isoformat()}
        docs = {"E3733": {"ordensServico": {"atual": service}}}
        self.composer.publish(index(), sources(), docs, NOW)
        service.update(statusAtual="EXECUCAO", inicioExecucao=(NOW+timedelta(minutes=1)).isoformat())
        first = self.composer.publish(index(), sources(), docs, NOW+timedelta(minutes=2))
        self.assertEqual(first["events"][0]["type"], "servico_execucao")
        service.update(statusAtual="CONCLUSAO", fimExecucao=(NOW+timedelta(minutes=3)).isoformat())
        after = self.composer.publish(index(), sources(), docs, NOW+timedelta(minutes=4))
        repeat = TorreComposer(self.root, "ChicoEletro").publish(index(), sources(), docs, NOW+timedelta(minutes=5))
        self.assertEqual(len(after["events"]), 2)
        self.assertEqual(after["events"], repeat["events"])

    def test_unknown_historical_completion_does_not_become_recent(self):
        self.composer.publish(index(), sources(), now=NOW)
        docs = {"E3733": {"ordensServico": {"historico": [{"serviceId": "old", "statusAtual": "CONCLUSAO"}]}}}
        after = self.composer.publish(index(), sources(), docs, NOW+timedelta(minutes=2))
        self.assertEqual(after["events"], [])

    def test_bad_inputs_or_corrupt_journal_preserves_publication(self):
        self.composer.publish(index(), sources(), now=NOW)
        saved = self.composer.path.read_bytes()
        src = sources()
        src["catalog"]["data"]["company"] = "Outra"
        with self.assertRaises(ValueError):
            self.composer.publish(index(), src, now=NOW)
        self.assertEqual(self.composer.path.read_bytes(), saved)
        self.composer.journal.write_bytes(b"invalid")
        with self.assertRaises(ValueError):
            self.composer.publish(index(), sources(), now=NOW)
        self.assertEqual(self.composer.path.read_bytes(), saved)

    def test_failed_publication_recovers_journal_events(self):
        self.composer.publish(index(), sources(), now=NOW)
        closed = index("FECHADO")
        saved = self.composer.path.read_bytes()
        with patch("coletor.torre_composer.publish_projection", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.composer.publish(closed, sources(), now=NOW+timedelta(minutes=2))
        self.assertEqual(self.composer.path.read_bytes(), saved)
        recovered = self.composer.publish(closed, sources(), now=NOW+timedelta(minutes=3))
        self.assertEqual(len(recovered["events"]), 1)

    def test_collection_failure_and_old_index_mark_stale(self):
        old = index()
        self.assertTrue(self.composer.publish(old, sources(), now=NOW+timedelta(hours=1))["stale"])
        old["collectionFailed"] = True
        self.assertTrue(self.composer.publish(old, sources(), now=NOW)["stale"])

    def test_runner_publishes_after_collection_using_local_index(self):
        from main import LocalRotalogRunner
        from coletor.storage_io import write_json
        with patch.dict("os.environ", {"TORRE_INPUTS_ENABLED": "false", "TORRE_COMPOSE_ENABLED": "false"}):
            runner = LocalRotalogRunner(self.root, "ChicoEletro")
        runner.torre_inputs = unittest.mock.Mock()
        runner.torre_inputs.sync.return_value = {"available": True, "stale": False}
        runner.torre_inputs.snapshot.return_value = sources()
        runner.torre_composer = self.composer
        write_json(runner.index_path, index())
        with patch.object(runner, "_run_once", return_value={"status": "success"}):
            result = runner.run_once()
        self.assertTrue(result["torreProjection"]["available"])
        self.assertTrue(self.composer.path.exists())

    def test_service_intermediate_transitions_between_scrapes(self):
        service = {"serviceId": "ss-1", "statusAtual": "DESLOCAMENTO", "inicioDeslocamento": NOW.isoformat()}
        docs = {"E3733": {"ordensServico": {"atual": service}}}
        self.composer.publish(index(), sources(), docs, NOW)
        service.update(statusAtual="CONCLUSAO", inicioExecucao=(NOW+timedelta(minutes=1)).isoformat(),
                       fimExecucao=(NOW+timedelta(minutes=2)).isoformat())
        data = self.composer.publish(index(), sources(), docs, NOW+timedelta(minutes=3))
        self.assertEqual([e["type"] for e in data["events"]], ["servico_execucao", "servico_conclusao"])
        again = TorreComposer(self.root, "ChicoEletro").publish(index(), sources(), docs, NOW+timedelta(minutes=4))
        self.assertEqual(data["events"], again["events"])
