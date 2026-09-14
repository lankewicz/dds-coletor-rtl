"""Testes para o resumo de transições operacionais e formatação de colunas da TUI."""

from __future__ import annotations

import unittest
from coletor.storage import summarize_team_transition
from tui import _format_columns, _format_bytes


class TeamTransitionsTests(unittest.TestCase):
    def test_shift_start_and_end(self):
        doc_closed = {"jornada": {"turno": {"status": "FECHADO"}}}
        doc_open = {"jornada": {"turno": {"status": "ABERTO"}}}

        self.assertEqual(summarize_team_transition(doc_closed, doc_open), "Início de Turno")
        self.assertEqual(summarize_team_transition(doc_open, doc_closed), "Fim de Turno")

    def test_interval_start_and_end(self):
        doc_normal = {"jornada": {"turno": {"status": "ABERTO"}, "emIntervalo": False}}
        doc_break = {"jornada": {"turno": {"status": "ABERTO"}, "emIntervalo": True}}

        self.assertEqual(summarize_team_transition(doc_normal, doc_break), "Início de Intervalo")
        self.assertEqual(summarize_team_transition(doc_break, doc_normal), "Fim de Intervalo")

    def test_service_execution_to_conclusion(self):
        doc_exec = {
            "jornada": {"turno": {"status": "ABERTO"}},
            "ordensServico": {"totalConcluidos": 2, "historico": [{"id": 1}, {"id": 2}], "atual": {"status": "EXECUCAO"}},
        }
        doc_concl = {
            "jornada": {"turno": {"status": "ABERTO"}},
            "ordensServico": {"totalConcluidos": 3, "historico": [{"id": 1}, {"id": 2}, {"id": 3}], "atual": None},
        }

        self.assertEqual(summarize_team_transition(doc_exec, doc_concl), "Execução --> Conclusão")

    def test_service_status_transition(self):
        doc_desloc = {
            "jornada": {"turno": {"status": "ABERTO"}},
            "ordensServico": {"totalConcluidos": 1, "atual": {"status": "DESLOCAMENTO"}},
        }
        doc_exec = {
            "jornada": {"turno": {"status": "ABERTO"}},
            "ordensServico": {"totalConcluidos": 1, "atual": {"status": "EXECUCAO"}},
        }
        doc_livre = {
            "jornada": {"turno": {"status": "ABERTO"}},
            "ordensServico": {"totalConcluidos": 1, "atual": None},
        }

        self.assertEqual(summarize_team_transition(doc_desloc, doc_exec), "Deslocamento --> Execução")
        self.assertEqual(summarize_team_transition(doc_livre, doc_desloc), "Livre --> Deslocamento")

    def test_schema_v2_status_atual_and_sync_reasons(self):
        doc_v2_desloc = {
            "jornada": {"turno": {"status": "ABERTO"}},
            "ordensServico": {"atual": {"statusAtual": "DESLOCAMENTO", "protocolo": "123"}},
        }
        doc_v2_exec = {
            "jornada": {"turno": {"status": "ABERTO"}},
            "ordensServico": {"atual": {"statusAtual": "EXECUCAO", "protocolo": "123"}},
        }
        self.assertEqual(summarize_team_transition(doc_v2_desloc, doc_v2_exec), "Deslocamento --> Execução")

        # Com sync_reasons
        self.assertEqual(
            summarize_team_transition(doc_v2_exec, doc_v2_exec, sync_reasons=["servico_concluido"]),
            "Execução --> Conclusão",
        )
        self.assertEqual(
            summarize_team_transition(doc_v2_exec, doc_v2_exec, sync_reasons=["correcao_servico_concluido"]),
            "Correção de OS",
        )

        doc_prev_hist = {
            "ordensServico": {
                "historico": [{
                    "serviceId": "1",
                    "protocolo": None,
                    "fimExecucao": "10:00",
                    "latitude": None,
                }],
            },
        }
        doc_curr_proto = {
            "ordensServico": {
                "historico": [{
                    "serviceId": "1",
                    "protocolo": "50986126",
                    "fimExecucao": "10:00",
                    "latitude": None,
                }],
            },
        }
        doc_curr_time = {
            "ordensServico": {
                "historico": [{
                    "serviceId": "1",
                    "protocolo": None,
                    "fimExecucao": "10:15",
                    "latitude": None,
                }],
            },
        }
        doc_curr_gps = {
            "ordensServico": {
                "historico": [{
                    "serviceId": "1",
                    "protocolo": None,
                    "fimExecucao": "10:00",
                    "latitude": -25.4,
                    "longitude": -53.1,
                }],
            },
        }

        self.assertEqual(
            summarize_team_transition(doc_prev_hist, doc_curr_proto, sync_reasons=["correcao_servico_concluido"]),
            "OS (+Protocolo)",
        )
        self.assertEqual(
            summarize_team_transition(doc_prev_hist, doc_curr_time, sync_reasons=["correcao_servico_concluido"]),
            "OS (Horário)",
        )
        self.assertEqual(
            summarize_team_transition(doc_prev_hist, doc_curr_gps, sync_reasons=["correcao_servico_concluido"]),
            "OS (+GPS)",
        )
        self.assertEqual(
            summarize_team_transition(None, doc_v2_exec, sync_reasons=["turno_aberto"]),
            "Início de Turno",
        )

    def test_format_columns_fixed_width(self):
        items = [
            "E3T99: Deslocamento --> Execução",
            "E3X04: Execução --> Conclusão",
            "E3S04: Deslocamento --> Execução",
        ]

        # Em largura 80, cabem 2 colunas de 35 chars com separador ' | '
        rows = _format_columns(items, width=80, col_width=35)
        self.assertEqual(len(rows), 2)
        # Primeira linha tem item 0 e item 1
        self.assertIn("E3T99:", rows[0])
        self.assertIn("E3X04:", rows[0])
        self.assertIn(" | ", rows[0])
        # Segunda linha tem item 2
        self.assertIn("E3S04:", rows[1])

        # Em largura 120, cabem 3 colunas de 35 chars
        rows_wide = _format_columns(items, width=120, col_width=35)
        self.assertEqual(len(rows_wide), 1)
        self.assertIn("E3T99:", rows_wide[0])
        self.assertIn("E3X04:", rows_wide[0])
        self.assertIn("E3S04:", rows_wide[0])

    def test_format_bytes(self):
        self.assertEqual(_format_bytes(0), "0 B")
        self.assertEqual(_format_bytes(None), "0 B")
        self.assertEqual(_format_bytes(512), "512 B")
        self.assertEqual(_format_bytes(1024), "1.0 KB")
        self.assertEqual(_format_bytes(1536), "1.5 KB")
        self.assertEqual(_format_bytes(1048576), "1.0 MB")
        self.assertEqual(_format_bytes(1073741824), "1.0 GB")


if __name__ == "__main__":
    unittest.main()
