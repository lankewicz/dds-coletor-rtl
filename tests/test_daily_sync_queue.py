import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from coletor.storage import load_json, write_json
from main import LocalRotalogRunner


class DailySyncQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Mock(enabled=True, bucket_name="bucket", blob_name="index.json.gz")
        self.repo = Mock()
        self.runner = self.new_runner()
        self.day = "2026-09-14"
        self.team_key = "E3733"
        self.document = {
            "teamKey": self.team_key,
            "date": self.day,
            "jornada": {"turno": {"status": "ABERTO"}, "intervalos": []},
            "ordensServico": {"historico": []},
        }
        self.daily_path = self.root / "rotalog/equipes/daily" / self.day / f"{self.team_key}.json.gz"
        write_json(self.daily_path, self.document)

    def new_runner(self):
        runner = LocalRotalogRunner(self.root, "EmpresaTeste")
        runner.enable_firebase = True
        runner.firebase_store = self.store
        runner.team_repo = self.repo
        return runner

    def queue_items(self):
        return load_json(self.runner.daily_sync_queue_path, {}).get("items", {})

    def test_pending_is_written_before_upload_and_removed_only_after_success(self):
        self.runner._enqueue_daily_sync(self.day, self.team_key, ["turno_aberto"])
        self.assertIn(f"{self.day}/{self.team_key}", self.queue_items())
        self.repo.save_daily.side_effect = OSError("offline")
        failed = self.runner._flush_daily_sync_queue()
        self.assertEqual(failed, {"uploaded": 0, "failed": 1, "pending": 1})
        self.assertEqual(self.queue_items()[f"{self.day}/{self.team_key}"]["attempts"], 1)

        self.repo.save_daily.side_effect = None
        restarted = self.new_runner()
        recovered = restarted._flush_daily_sync_queue()
        self.assertEqual(recovered, {"uploaded": 1, "failed": 0, "pending": 0})
        self.assertEqual(self.queue_items(), {})

    def test_repeated_events_coalesce_and_upload_latest_local_document(self):
        self.runner._enqueue_daily_sync(self.day, self.team_key, ["turno_aberto"])
        self.runner._enqueue_daily_sync(self.day, self.team_key, ["servico_concluido"])
        latest = copy.deepcopy(self.document)
        latest["ordensServico"]["historico"] = [{"protocolo": "12345678"}]
        write_json(self.daily_path, latest)
        self.runner._flush_daily_sync_queue()
        self.repo.save_daily.assert_called_once_with(latest, self.day)

    def test_queue_remains_when_firebase_is_unavailable(self):
        self.runner._enqueue_daily_sync(self.day, self.team_key, ["turno_aberto"])
        self.runner.firebase_store = None
        self.runner.team_repo = None
        self.assertEqual(
            self.runner._flush_daily_sync_queue(),
            {"uploaded": 0, "failed": 0, "pending": 1},
        )

    def test_event_policy_covers_open_new_completion_and_close_only(self):
        opened = self.document
        self.assertEqual(self.runner._daily_sync_reasons({}, opened), ["estado_inicial"])

        completed = copy.deepcopy(opened)
        completed["ordensServico"]["historico"] = [{
            "serviceId": "E3733_service-1",
            "statusAtual": "CONCLUSAO",
            "protocolo": "12345678",
            "fimExecucao": "10:00",
        }]
        self.assertEqual(
            self.runner._daily_sync_reasons(opened, completed),
            ["servico_concluido"],
        )

        corrected = copy.deepcopy(completed)
        corrected["ordensServico"]["historico"][0]["fimExecucao"] = "10:15"
        self.assertEqual(self.runner._daily_sync_reasons(completed, corrected), [])

        redirected = copy.deepcopy(opened)
        redirected["ordensServico"]["historico"] = [{
            "serviceId": "E3733_service-2",
            "statusAtual": "REDIRECIONADO",
            "protocolo": "87654321",
        }]
        self.assertEqual(self.runner._daily_sync_reasons(opened, redirected), [])

        closed = copy.deepcopy(corrected)
        closed["jornada"]["turno"]["status"] = "FECHADO"
        self.assertEqual(self.runner._daily_sync_reasons(corrected, closed), ["turno_fechado"])
        # Equipe que começa o dia já fechada (sem estado prévio) NÃO deve disparar turno_fechado
        self.assertNotIn("turno_fechado", self.runner._daily_sync_reasons({}, closed))
        self.assertEqual(self.runner._daily_sync_reasons({}, {"jornada": {"turno": {"status": "FECHADO"}}}), [])

    def test_corrected_service_id_does_not_repeat_same_completion(self):
        previous = copy.deepcopy(self.document)
        previous["ordensServico"]["historico"] = [{
            "serviceId": "E3733_old", "protocolo": "51049390", "statusAtual": "CONCLUSAO",
            "inicioDeslocamento": "2026-09-14T09:56:00-03:00", "fimExecucao": "2026-09-14T10:21:00-03:00",
        }]
        current = copy.deepcopy(previous)
        current["ordensServico"]["historico"][0].update(
            serviceId="E3733_new", inicioDeslocamento="2026-09-14T12:56:00Z")
        self.assertEqual(self.runner._daily_sync_reasons(previous, current), [])
        current["ordensServico"]["historico"][0].update(
            inicioDeslocamento="2026-09-14T11:00:00-03:00", fimExecucao="2026-09-14T11:20:00-03:00")
        self.assertIn("servico_concluido", self.runner._daily_sync_reasons(previous, current))

    def test_closed_daily_history_is_finalized_and_open_history_is_not(self):
        open_history = {"jornada": {"turno": {"status": "ABERTO"}}}
        closed_history = {"jornada": {"turno": {"status": "FECHADO"}}}

        self.assertFalse(self.runner._daily_history_is_finalized(open_history))
        self.assertTrue(self.runner._daily_history_is_finalized(closed_history))

    def test_corrupt_sync_queue_is_quarantined_and_reinitialized(self):
        queue_file = self.runner.daily_sync_queue_path
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        queue_file.write_bytes(b"corrupted-non-gzip-content-or-truncated")

        loaded = self.runner._load_daily_sync_queue()
        self.assertEqual(loaded["schemaVersion"], 1)
        self.assertEqual(loaded["items"], {})
        self.assertTrue(queue_file.exists())
        corrupt_files = list(queue_file.parent.glob("pending-daily.json.gz.corrupt-*"))
        self.assertEqual(len(corrupt_files), 1)

    def test_corrupt_queue_is_rebuilt_from_unconfirmed_local_history(self):
        completed = copy.deepcopy(self.document)
        completed["ordensServico"]["historico"] = [{"protocolo": "12345678"}]
        write_json(self.daily_path, completed)
        self.runner.daily_sync_queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.runner.daily_sync_queue_path.write_bytes(b"corrupted")

        rebuilt = self.runner._load_daily_sync_queue()

        self.assertIn(f"{self.day}/{self.team_key}", rebuilt["items"])
        self.assertEqual(
            rebuilt["items"][f"{self.day}/{self.team_key}"]["reasons"],
            ["fila_reconstruida"],
        )

    def test_repeated_shift_states_do_not_repeat_upload_events(self):
        for status in ("ABERTO", "FECHADO"):
            doc = copy.deepcopy(self.document)
            doc["jornada"]["turno"].update(status=status, inicio="2026-09-14T08:00:00-03:00")
            updated = copy.deepcopy(doc)
            updated["updatedAt"] = "2026-09-14T10:00:00-03:00"
            self.assertEqual(self.runner._daily_sync_reasons(doc, updated), [])
            write_json(self.daily_path, doc)
            restarted = self.new_runner()
            saved = restarted._load_daily_with_recovery(self.daily_path, self.day, self.team_key)
            self.assertEqual(restarted._daily_sync_reasons(saved, updated), [])

    def test_missing_history_and_shift_oscillation_remain_detection_risks(self):
        opened = copy.deepcopy(self.document)
        closed = copy.deepcopy(opened)
        closed["jornada"]["turno"]["status"] = "FECHADO"
        self.assertEqual(self.runner._daily_sync_reasons({}, opened), ["estado_inicial"])
        self.assertEqual(self.runner._daily_sync_reasons({}, closed), [])
        for old, new, reason in ((opened, closed, "turno_fechado"),
                                 (closed, opened, "turno_aberto"), (opened, closed, "turno_fechado")):
            self.assertEqual(self.runner._daily_sync_reasons(old, new), [reason])

    def test_interval_changes_remain_local_and_stable_state_does_not_repeat(self):
        from coletor.storage import summarize_team_transition
        normal = copy.deepcopy(self.document)
        normal["jornada"]["emIntervalo"] = False
        paused = copy.deepcopy(normal)
        paused["jornada"]["emIntervalo"] = True
        self.assertEqual(summarize_team_transition(normal, paused), "Início de Intervalo")
        self.assertEqual(summarize_team_transition(paused, normal), "Fim de Intervalo")
        for old, new in ((normal, paused), (paused, normal), (paused, paused)):
            self.assertEqual(self.runner._daily_sync_reasons(old, new), [])
        self.assertNotIn(summarize_team_transition(paused, paused), ("Início de Intervalo", "Fim de Intervalo"))

    def test_persisted_shift_markers_block_oscillation_but_allow_new_shift(self):
        closed = copy.deepcopy(self.document)
        closed["jornada"]["turno"].update(status="FECHADO", inicio="2026-09-14T08:00:00-03:00")
        key = self.runner._shift_event_key(closed, "turno_fechado")
        reopened = copy.deepcopy(closed)
        reopened["jornada"]["turno"]["status"] = "ABERTO"
        reopened["observedShiftEvents"] = {key: True}
        write_json(self.daily_path, reopened)
        restarted = self.new_runner()
        saved = load_json(self.daily_path, {})
        self.assertEqual(restarted._daily_sync_reasons(saved, closed), [])
        closed["jornada"]["turno"]["inicio"] = "2026-09-14T14:00:00-03:00"
        self.assertEqual(restarted._daily_sync_reasons(saved, closed), ["turno_fechado"])


if __name__ == "__main__":
    unittest.main()
