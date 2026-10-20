import copy
import gzip
from pathlib import Path
import tempfile
import unittest

from coletor.storage import changed_fields, compactar_equipe_para_index, load_json, load_json_with_status, write_json


class StorageRegressionTests(unittest.TestCase):
    def test_compact_index_preserves_only_active_interval_start(self):
        doc = self.document()
        doc["jornada"]["emIntervalo"] = True
        doc["jornada"]["intervalos"] = [
            {"inicio": "2026-10-20T09:00:00-03:00", "fim": "2026-10-20T09:10:00-03:00"},
            {"inicio": "2026-10-20T12:00:00-03:00", "fim": None},
        ]
        compact = compactar_equipe_para_index(doc)
        self.assertEqual(compact["jornada"]["intervaloAtual"], {"inicio": "2026-10-20T12:00:00-03:00"})
        self.assertNotIn("intervalos", compact["jornada"])
        self.assertEqual(compactar_equipe_para_index(compact), compact)
        compact["jornada"]["emIntervalo"] = False
        self.assertIsNone(compactar_equipe_para_index(compact)["jornada"]["intervaloAtual"])

    def document(self):
        return {
            "teamKey": "E3733",
            "jornada": {
                "turno": {"status": "ABERTO", "inicio": "08:00"},
                "intervalos": [{"inicio": "12:00", "fim": None}],
            },
            "ordensServico": {
                "historico": [{"protocolo": "12345678", "fimExecucao": "10:00"}],
            },
        }

    def test_compacting_twice_preserves_current_projection(self):
        compact = compactar_equipe_para_index(self.document())
        self.assertNotIn("historico", compact["ordensServico"])
        self.assertNotIn("turnosRecentes", compact["jornada"])
        self.assertEqual(compact["executadosHoje"], {"comercial": 0, "emergencia": 0})
        self.assertEqual(compactar_equipe_para_index(compact), compact)

    def test_queue_uses_raw_pending_list_and_handles_empty_list(self):
        doc = self.document()
        doc["fila"] = {"comercial": 9, "emergencia": 8}
        current = {"ss_pendentes": [{"tipo": "COMERCIAL"}, {"tipo": "EMERGENCIA"}],
                   "ssPendentesComercialCount": 99}
        self.assertEqual(compactar_equipe_para_index(doc, current)["fila"],
                         {"comercial": 1, "emergencia": 1})
        self.assertEqual(compactar_equipe_para_index(doc, {"ss_pendentes": []})["fila"],
                         {"comercial": 0, "emergencia": 0})

    def test_queue_preserves_previous_values_when_snapshot_has_no_queue(self):
        doc = self.document()
        doc["fila"] = {"comercial": 9, "emergencia": 8}
        self.assertEqual(compactar_equipe_para_index(doc, {"isOnline": True})["fila"], doc["fila"])
        self.assertEqual(compactar_equipe_para_index(doc, {"ssPendentesComercialCount": 2})["fila"],
                         {"comercial": 2, "emergencia": 0})

    def test_interval_end_change_without_new_interval(self):
        old = self.document()
        new = copy.deepcopy(old)
        new["jornada"]["intervalos"][0]["fim"] = "13:00"
        self.assertIn("jornada.intervalos", changed_fields(old, new))

    def test_history_correction_without_new_service(self):
        old = self.document()
        new = copy.deepcopy(old)
        new["ordensServico"]["historico"][0]["fimExecucao"] = "10:30"
        self.assertIn("ordensServico.historico", changed_fields(old, new))

    def test_shift_start_correction(self):
        old = self.document()
        new = copy.deepcopy(old)
        new["jornada"]["turno"]["inicio"] = "08:30"
        self.assertIn("jornada.turno.inicio", changed_fields(old, new))

    def test_legacy_diff_returns_changes_and_empty_dict(self):
        self.assertIn("isOnline", changed_fields({"isOnline": False}, {"isOnline": True}))
        self.assertEqual(changed_fields({"isOnline": True}, {"isOnline": True}), {})

    def test_unchanged_daily_ignores_observation_timestamp(self):
        old = self.document()
        new = copy.deepcopy(old)
        new["updatedAt"] = "2026-09-14T10:00:00-03:00"
        new["ordensServico"]["historico"][0]["observadoEm"] = "10:00"
        self.assertEqual(changed_fields(old, new), {})

    def test_tower_does_not_publish_redundant_total_counter(self):
        doc = self.document()
        doc["ordensServico"].update(historico=[], totalConcluidos=10)
        compact = compactar_equipe_para_index(doc)
        self.assertNotIn("totalConcluidos", compact["ordensServico"])

    def test_corrupt_local_files_and_gzip_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json.gz"
            write_json(path, self.document())
            self.assertEqual(load_json(path, {}), self.document())
            path.write_bytes(gzip.compress(b'{}')[:-4])
            self.assertEqual(load_json(path, {}), {})
            path.write_bytes(b'\xff\xfe')
            self.assertEqual(load_json(path, {}), {})

    def test_load_status_distinguishes_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json.gz"
            self.assertEqual(load_json_with_status(path, {})[1], "missing")
            path.write_bytes(b"invalid")
            self.assertEqual(load_json_with_status(path, {})[1], "corrupt")
            write_json(path, {})
            self.assertEqual(load_json_with_status(path, {})[1], "ok")

    def test_fila_na_conclusao_is_immutable_snapshot(self):
        from coletor.storage import merge_daily_document
        day = "2026-09-14"
        service_raw = {
            "protocolo": "50986126",
            "tipo": "CHAVE",
            "categoria": "EMERGENCIA",
            "status": "CONCLUSAO",
            "inicioExecucao": "2026-09-14T08:00:00-03:00",
            "fimExecucao": "2026-09-14T09:00:00-03:00",
        }

        # Primeiro scrape: serviço acaba de ser concluído quando a fila estava em 2 e 5
        scrape_1 = {
            "equipe": "E3733",
            "empresa": "ChicoEletro",
            "ssExecutadas": [service_raw],
            "ssPendentesEmergenciaCount": 2,
            "ssPendentesComercialCount": 5,
        }
        merged_1 = merge_daily_document(None, scrape_1, day)
        concluded_1 = merged_1["ordensServico"]["historico"][0]
        self.assertEqual(concluded_1["filaNaConclusao"], {"emergencia": 2, "comercial": 5})

        # Segundo scrape mais tarde: fila da empresa agora mudou para 8 e 12
        scrape_2 = {
            "equipe": "E3733",
            "empresa": "ChicoEletro",
            "ssExecutadas": [service_raw],
            "ssPendentesEmergenciaCount": 8,
            "ssPendentesComercialCount": 12,
        }
        merged_2 = merge_daily_document(merged_1, scrape_2, day)
        concluded_2 = merged_2["ordensServico"]["historico"][0]

        # A foto da fila no momento da conclusão DEVE PERMANECER INTACTA (2 e 5)
        self.assertEqual(concluded_2["filaNaConclusao"], {"emergencia": 2, "comercial": 5})
        # Nenhum campo operacional deve ter sido alterado
        self.assertEqual(changed_fields(merged_1, merged_2), {})


if __name__ == "__main__":
    unittest.main()
