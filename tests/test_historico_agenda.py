import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from coletor.historico_agenda import janela_do_dia, reconciliar_historico


class AgendaTests(unittest.TestCase):
    def test_janelas_e_prioridade_no_domingo_dia_dez(self):
        self.assertEqual(7, janela_do_dia(datetime.date(2026, 10, 2)))
        self.assertEqual(30, janela_do_dia(datetime.date(2026, 10, 4)))
        self.assertEqual(60, janela_do_dia(datetime.date(2027, 1, 10)))

    def test_interrupcao_retoma_so_dias_restantes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collect = Mock(side_effect=[{"status": "success"}, KeyboardInterrupt()])
            with self.assertRaises(KeyboardInterrupt):
                reconciliar_historico(root, "Empresa", today=datetime.date(2026, 10, 2), collect=collect)
            collect = Mock(return_value={"status": "success"})
            result = reconciliar_historico(root, "Empresa", today=datetime.date(2026, 10, 2), collect=collect)
            self.assertEqual("success", result["status"])
            self.assertEqual(6, collect.call_count)
            collect.reset_mock()
            reconciliar_historico(root, "Empresa", today=datetime.date(2026, 10, 2), collect=collect)
            collect.assert_not_called()

    def test_dez_tentativas_apenas_falhas_e_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            def collect(target_date, **kwargs):
                if target_date == datetime.date(2026, 9, 25):
                    raise RuntimeError("offline")
                return {"status": "success"}
            mocked = Mock(side_effect=collect)
            sleep = Mock()
            result = reconciliar_historico(Path(directory), "Empresa", today=datetime.date(2026, 10, 2), collect=mocked, sleep=sleep)
            self.assertEqual(16, mocked.call_count)
            self.assertEqual([60, 120, 240, 480, 900, 900, 900, 900, 900], [call.args[0] for call in sleep.call_args_list])
            self.assertEqual(["2026-09-25"], result["pendingDays"])
            resumed = Mock(return_value={"status": "success"})
            reconciliar_historico(Path(directory), "Empresa", today=datetime.date(2026, 10, 3), collect=resumed)
            self.assertEqual(8, resumed.call_count)

    def test_dia_dez_coleta_sessenta_e_fecha_mes_anterior(self):
        with tempfile.TemporaryDirectory() as directory:
            collect = Mock(return_value={"status": "success"})
            monthly = Mock(return_value={"status": "success"})
            root = Path(directory)
            result = reconciliar_historico(root, "Empresa", today=datetime.date(2026, 10, 10), collect=collect, close_month=monthly)
            self.assertEqual(60, collect.call_count)
            self.assertEqual(datetime.date(2026, 8, 11), collect.call_args_list[0].kwargs["target_date"])
            self.assertEqual(datetime.date(2026, 10, 9), collect.call_args_list[-1].kwargs["target_date"])
            monthly.assert_called_once_with(2026, 9, root, "Empresa", enable_firebase=False, firebase_store=None, coletar_diarios=False)
            self.assertEqual("success", result["status"])

    def test_falha_mensal_retoma_no_dia_seguinte_sem_repetir_sessenta_dias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collect = Mock(return_value={"status": "success"})
            monthly = Mock(side_effect=RuntimeError("portal offline"))
            first = reconciliar_historico(root, "Empresa", today=datetime.date(2026, 10, 10), collect=collect, close_month=monthly)
            self.assertTrue(first["monthlyPending"])
            collect.reset_mock()
            monthly.side_effect = None
            monthly.return_value = {"status": "success"}
            second = reconciliar_historico(root, "Empresa", today=datetime.date(2026, 10, 12), collect=collect, close_month=monthly)
            self.assertEqual(7, collect.call_count)
            self.assertEqual("success", second["status"])
