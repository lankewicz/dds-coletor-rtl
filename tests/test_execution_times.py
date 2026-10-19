import unittest

from coletor.merger import compact_service, _merge_service_records, merge_daily_document


class ExecutionTimeTests(unittest.TestCase):
    def service(self, execution=None):
        return {
            "protocolo": "20265920422499", "status": "EXECUCAO",
            "inicioIso": "2026-10-19T10:00:00-03:00",
            "inicioDeslocamento": "10:00", "inicioExecucao": execution,
            "transitions": [{"status": "EXECUCAO", "timestampMs": 1, "hora": "08:00"}],
        }

    def compact(self, raw):
        return compact_service("E3X89", "2026-10-19", raw, "2026-10-19T14:50:00-03:00")

    def test_execution_before_departure_is_not_clamped(self):
        s = self.compact(self.service("08:00"))
        self.assertIsNone(s["inicioExecucao"])
        self.assertEqual(s["transitions"], [])
        self.assertEqual(s["horariosDescartados"][0]["valor"], "08:00")

    def test_future_execution_is_discarded(self):
        s = self.compact(self.service("15:00"))
        self.assertIsNone(s["inicioExecucao"])
        self.assertEqual(s["horariosDescartados"][0]["motivo"], "POSTERIOR_A_COLETA")

    def test_missing_execution_does_not_use_block_start(self):
        s = self.compact(self.service())
        self.assertIsNone(s["inicioExecucao"])
        self.assertEqual(s["transitions"], [])

    def test_valid_correction_updates_same_service_and_transition(self):
        old = self.compact(self.service("15:00"))
        old["observadoEm"] = "2026-10-19T14:50:00-03:00"
        new = self.compact(self.service("10:30"))
        new["observadoEm"] = "2026-10-19T14:51:00-03:00"
        records = _merge_service_records("E3X89", [old, new])
        self.assertEqual(len(records), 1)
        s = next(iter(records.values()))
        self.assertEqual(s["inicioExecucao"], "2026-10-19T10:30:00-03:00")
        self.assertEqual(s["transitions"][0]["hora"], "10:30")
        self.assertTrue(s["horariosDescartados"])

    def test_overnight_execution_is_preserved(self):
        raw = self.service("00:20")
        raw["inicioIso"] = "2026-10-18T23:50:00-03:00"
        raw["inicioDeslocamento"] = "23:50"
        s = compact_service("E3X89", "2026-10-18", raw, "2026-10-19T01:00:00-03:00")
        self.assertEqual(s["inicioExecucao"], "2026-10-19T00:20:00-03:00")

    def test_execution_at_departure_is_allowed(self):
        self.assertEqual(self.compact(self.service("10:00"))["inicioExecucao"],
                         "2026-10-19T10:00:00-03:00")

    def test_invalid_execution_does_not_move_completion_into_future(self):
        raw = self.service("15:00")
        raw.update(status="CONCLUSAO", termino="14:00", retorno="14:10")
        s = self.compact(raw)
        self.assertIsNone(s["inicioExecucao"])
        self.assertEqual(s["fimExecucao"], "2026-10-19T14:00:00-03:00")

    def test_sequential_daily_merge_corrects_current_and_history(self):
        raw = self.service("15:00")
        current = {"teamKey": "E3X89", "empresa": "ChicoEletro",
                   "updatedAtIso": "2026-10-19T14:50:00-03:00",
                   "atividadeAtual": raw, "ssEmAndamento": [raw]}
        previous = merge_daily_document({}, current, "2026-10-19")
        self.assertIsNone(previous["ordensServico"]["atual"]["inicioExecucao"])
        corrected = self.service("10:30")
        corrected.update(status="CONCLUSAO", termino="14:00")
        current.update(updatedAtIso="2026-10-19T14:51:00-03:00",
                       atividadeAtual=None, ssEmAndamento=[], ssExecutadas=[corrected])
        result = merge_daily_document(previous, current, "2026-10-19")
        history = result["ordensServico"]["historico"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["inicioExecucao"], "2026-10-19T10:30:00-03:00")
        self.assertTrue(history[0]["horariosDescartados"])

    def test_old_invalid_record_cannot_restore_discarded_execution(self):
        old = self.compact(self.service("10:30"))
        old.update(inicioExecucao="2026-10-19T08:00:00-03:00",
                   observadoEm="2026-10-19T14:40:00-03:00")
        new = self.compact(self.service("08:00"))
        new["observadoEm"] = "2026-10-19T14:50:00-03:00"
        s = next(iter(_merge_service_records("E3X89", [old, new]).values()))
        self.assertIsNone(s["inicioExecucao"])
        self.assertEqual(s["transitions"], [])
