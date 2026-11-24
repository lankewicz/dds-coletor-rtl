import unittest

from coletor.merger import merge_daily_document


class ServiceWithdrawalTests(unittest.TestCase):
    day = "2026-11-10"

    def service(self, protocol, status, hour):
        return {
            "protocolo": protocol,
            "statusAtual": status,
            "inicioDeslocamento": f"{self.day}T{hour}:00-03:00",
            "inicioExecucao": f"{self.day}T08:10:00-03:00" if status == "EXECUCAO" else None,
        }

    def snapshot(self, service=None, completed=None):
        return {
            "teamKey": "E3733",
            "updatedAt": f"{self.day}T10:00:00-03:00",
            "turno": {"aberto": True, "inicio": "07:00"},
            "atividadeAtual": service,
            "ssEmAndamento": [service] if service else [],
            "ssExecutadas": completed or [],
        }

    def test_switch_classifies_previous_stage_without_fabricating_completion(self):
        for old_status, expected in [
            ("DESLOCAMENTO", "DESLOCAMENTO_ISOLADO"),
            ("EXECUCAO", "RETIRADO_PELO_COD"),
        ]:
            for next_status in ("DESLOCAMENTO", "EXECUCAO"):
                for next_protocol in ("50954710", "10000123456", "20265422950240.2.1"):
                    with self.subTest(old=old_status, new=next_status, protocol=next_protocol):
                        old = self.service("20265422950239.2.1", old_status, "08:00")
                        previous = merge_daily_document({}, self.snapshot(old), self.day)
                        new = self.service(next_protocol, next_status, "08:30")
                        if next_status == "EXECUCAO":
                            new["inicioExecucao"] = f"{self.day}T08:40:00-03:00"
                        result = merge_daily_document(previous, self.snapshot(new), self.day)
                        self.assertEqual(len(result["services"]), 1)
                        interrupted = result["services"][0]
                        self.assertEqual(interrupted["statusAtual"], expected)
                        self.assertEqual(interrupted["inicioDeslocamento"], old["inicioDeslocamento"])
                        self.assertEqual(interrupted.get("inicioExecucao"), old["inicioExecucao"])
                        self.assertIsNone(interrupted.get("fimExecucao"))
                        self.assertIsNone(interrupted.get("retorno"))
                        self.assertTrue(interrupted["interrupcao"]["inferida"])
                        self.assertEqual(interrupted["interrupcao"]["protocoloSeguinte"], next_protocol)
                        self.assertEqual(result["ordensServico"]["atual"]["protocolo"], next_protocol)

    def test_disappearance_is_preserved_and_only_later_switch_is_classified(self):
        old = self.service("20265422950239.2.1", "DESLOCAMENTO", "08:00")
        previous = merge_daily_document({}, self.snapshot(old), self.day)
        absent = merge_daily_document(previous, self.snapshot(), self.day)
        self.assertEqual(absent["services"][0]["statusAtual"], "DESLOCAMENTO")
        self.assertNotIn("interrupcao", absent["services"][0])
        new = self.service("50954710", "DESLOCAMENTO", "09:00")
        result = merge_daily_document(absent, self.snapshot(new), self.day)
        self.assertEqual(result["services"][0]["statusAtual"], "DESLOCAMENTO_ISOLADO")

    def test_same_service_is_not_classified_as_withdrawn(self):
        old = self.service("50954710", "DESLOCAMENTO", "08:00")
        previous = merge_daily_document({}, self.snapshot(old), self.day)
        new = self.service("50954710", "EXECUCAO", "08:00")
        result = merge_daily_document(previous, self.snapshot(new), self.day)
        self.assertEqual(result["services"], [])
        self.assertEqual(result["ordensServico"]["atual"]["statusAtual"], "EXECUCAO")

    def test_switch_is_detected_when_next_service_only_appears_in_current_activity(self):
        old = self.service("50954710", "DESLOCAMENTO", "08:00")
        previous = merge_daily_document({}, self.snapshot(old), self.day)
        snapshot = self.snapshot(self.service("10000123456", "DESLOCAMENTO", "09:00"))
        snapshot["ssEmAndamento"] = []
        result = merge_daily_document(previous, snapshot, self.day)
        self.assertEqual(result["services"][0]["statusAtual"], "DESLOCAMENTO_ISOLADO")

    def test_completion_received_with_next_service_is_not_classified_as_withdrawal(self):
        old = self.service("50954710", "EXECUCAO", "08:00")
        previous = merge_daily_document({}, self.snapshot(old), self.day)
        completed = {**old, "statusAtual": "CONCLUSAO", "fimExecucao": f"{self.day}T08:55:00-03:00"}
        new = self.service("10000123456", "DESLOCAMENTO", "09:00")
        result = merge_daily_document(previous, self.snapshot(new, [completed]), self.day)
        self.assertEqual(result["services"][0]["statusAtual"], "CONCLUSAO")
        self.assertNotIn("interrupcao", result["services"][0])

    def test_later_completion_replaces_inferred_withdrawal(self):
        old = self.service("50954710", "EXECUCAO", "08:00")
        previous = merge_daily_document({}, self.snapshot(old), self.day)
        new = self.service("10000123456", "DESLOCAMENTO", "09:00")
        interrupted = merge_daily_document(previous, self.snapshot(new), self.day)
        completed = {**old, "statusAtual": "CONCLUSAO", "fimExecucao": f"{self.day}T08:55:00-03:00"}
        result = merge_daily_document(interrupted, self.snapshot(new, [completed]), self.day)
        self.assertEqual(result["services"][0]["statusAtual"], "CONCLUSAO")
        self.assertNotIn("interrupcao", result["services"][0])
        self.assertNotIn("semExecucaoType", result["services"][0])


if __name__ == "__main__":
    unittest.main()
