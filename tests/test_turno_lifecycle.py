import datetime
import unittest
from zoneinfo import ZoneInfo

from coletor.parser import consolidar_turno_por_contexto

TZ = ZoneInfo("America/Sao_Paulo")


class TestTurnoLifecycle(unittest.TestCase):
    def test_e3k59_fechamento_na_madrugada_pos_servico(self):
        """Caso E3K59: equipe trabalhou a noite toda e fechou às 03:55:47.
        Deve ser FECHADO com fim às 03:55:47, e NÃO abertura às 03:55."""
        # 2026-09-22 11:16:00
        now = datetime.datetime(2026, 9, 22, 11, 16, 0, tzinfo=TZ)
        t_fechamento = datetime.datetime(2026, 9, 22, 3, 55, 47, tzinfo=TZ)
        t_fechamento_ms = int(t_fechamento.timestamp() * 1000)

        # Ultimo servico concluido minutos antes
        fim_servico = datetime.datetime(2026, 9, 22, 3, 54, 0, tzinfo=TZ)
        fim_servico_ms = int(fim_servico.timestamp() * 1000)

        inicio_servico = datetime.datetime(2026, 9, 21, 20, 46, 0, tzinfo=TZ)
        inicio_servico_ms = int(inicio_servico.timestamp() * 1000)

        marcadores_t = [{"start": t_fechamento_ms, "end": None}]
        eventos_servico_ms = [inicio_servico_ms]

        snapshot_ant = {
            "jornada": {
                "turno": {
                    "inicio": "2026-09-21T05:39:42-03:00",
                    "status": "ABERTO",
                }
            }
        }

        res = consolidar_turno_por_contexto(
            marcadores_t=marcadores_t,
            eventos_servico_ms=eventos_servico_ms,
            tem_atividade_andamento=False,
            retorno_ultimo_servico_ms=fim_servico_ms,
            now=now,
            snapshot_anterior=snapshot_ant,
        )

        self.assertFalse(res["aberto"])
        self.assertEqual(res["classificacao"], "FECHADO")
        self.assertEqual(res["fim_ms"], t_fechamento_ms)
        self.assertIn("03:55:47", str(res["fim_iso"]))

    def test_e2148_fechamento_apos_chave_na_madrugada(self):
        """Caso E2148: servico CHAVE terminou às 01:15, T às 01:16:30.
        Deve ser FECHADO com fim às 01:16:30."""
        now = datetime.datetime(2026, 9, 22, 11, 16, 0, tzinfo=TZ)
        t_fechamento = datetime.datetime(2026, 9, 22, 1, 16, 30, tzinfo=TZ)
        t_fechamento_ms = int(t_fechamento.timestamp() * 1000)

        fim_servico = datetime.datetime(2026, 9, 22, 1, 15, 0, tzinfo=TZ)
        fim_servico_ms = int(fim_servico.timestamp() * 1000)

        marcadores_t = [{"start": t_fechamento_ms, "end": None}]
        eventos_servico_ms = [int(datetime.datetime(2026, 9, 21, 23, 30, 0, tzinfo=TZ).timestamp() * 1000)]

        res = consolidar_turno_por_contexto(
            marcadores_t=marcadores_t,
            eventos_servico_ms=eventos_servico_ms,
            tem_atividade_andamento=False,
            retorno_ultimo_servico_ms=fim_servico_ms,
            now=now,
        )

        self.assertFalse(res["aberto"])
        self.assertEqual(res["classificacao"], "FECHADO")
        self.assertEqual(res["fim_ms"], t_fechamento_ms)

    def test_t_acidental_no_meio_do_expediente_ignorado(self):
        """Equipe com turno aberto manda T acidental às 10:00, mas atende OS às 10:30 e 14:00.
        O T das 10:00 deve ser ignorado e o turno deve continuar ABERTO."""
        now = datetime.datetime(2026, 9, 22, 15, 0, 0, tzinfo=TZ)
        t_inicio = datetime.datetime(2026, 9, 22, 7, 30, 0, tzinfo=TZ)
        t_acidental = datetime.datetime(2026, 9, 22, 10, 0, 0, tzinfo=TZ)

        s1_inicio = datetime.datetime(2026, 9, 22, 8, 0, 0, tzinfo=TZ)
        s2_inicio = datetime.datetime(2026, 9, 22, 10, 30, 0, tzinfo=TZ)
        s3_inicio = datetime.datetime(2026, 9, 22, 14, 0, 0, tzinfo=TZ)
        s3_fim = datetime.datetime(2026, 9, 22, 14, 45, 0, tzinfo=TZ)

        marcadores_t = [
            {"start": int(t_inicio.timestamp() * 1000), "end": None},
            {"start": int(t_acidental.timestamp() * 1000), "end": None},
        ]
        eventos_servico = [
            int(s1_inicio.timestamp() * 1000),
            int(s2_inicio.timestamp() * 1000),
            int(s3_inicio.timestamp() * 1000),
        ]

        res = consolidar_turno_por_contexto(
            marcadores_t=marcadores_t,
            eventos_servico_ms=eventos_servico,
            tem_atividade_andamento=False,
            retorno_ultimo_servico_ms=int(s3_fim.timestamp() * 1000),
            now=now,
        )

        # O T acidental das 10:00 foi ignorado. E como sao 15:00 (horario comercial),
        # nao fecha por inatividade. O turno segue ABERTO!
        self.assertTrue(res["aberto"])
        self.assertEqual(res["classificacao"], "ABERTO")
        self.assertEqual(res["inicio_ms"], int(t_inicio.timestamp() * 1000))
        self.assertIsNone(res["fim_ms"])

    def test_fechamento_por_inatividade_pos_18h(self):
        """Equipe concluiu ultimo servico às 18:15, nao mandou novo deslocamento.
        Às 20:30 (> 2h depois e apos 18h), turno deve fechar automaticamente."""
        now = datetime.datetime(2026, 9, 22, 20, 30, 0, tzinfo=TZ)
        t_inicio = datetime.datetime(2026, 9, 22, 7, 30, 0, tzinfo=TZ)
        fim_servico = datetime.datetime(2026, 9, 22, 18, 15, 0, tzinfo=TZ)
        fim_servico_ms = int(fim_servico.timestamp() * 1000)

        marcadores_t = [{"start": int(t_inicio.timestamp() * 1000), "end": None}]
        eventos_servico = [int(datetime.datetime(2026, 9, 22, 8, 0, 0, tzinfo=TZ).timestamp() * 1000)]

        res = consolidar_turno_por_contexto(
            marcadores_t=marcadores_t,
            eventos_servico_ms=eventos_servico,
            tem_atividade_andamento=False,
            retorno_ultimo_servico_ms=fim_servico_ms,
            now=now,
        )

        self.assertFalse(res["aberto"])
        self.assertEqual(res["classificacao"], "FECHADO")
        self.assertEqual(res["fim_ms"], fim_servico_ms)

    def test_nao_fechar_por_inatividade_em_horario_comercial(self):
        """Equipe concluiu servico às 13:30, sao 16:00 (2.5h sem servico).
        Em horario comercial (<= 18:00), NAO deve fechar o turno."""
        now = datetime.datetime(2026, 9, 22, 16, 0, 0, tzinfo=TZ)
        t_inicio = datetime.datetime(2026, 9, 22, 7, 30, 0, tzinfo=TZ)
        fim_servico = datetime.datetime(2026, 9, 22, 13, 30, 0, tzinfo=TZ)

        marcadores_t = [{"start": int(t_inicio.timestamp() * 1000), "end": None}]
        eventos_servico = [int(datetime.datetime(2026, 9, 22, 8, 0, 0, tzinfo=TZ).timestamp() * 1000)]

        res = consolidar_turno_por_contexto(
            marcadores_t=marcadores_t,
            eventos_servico_ms=eventos_servico,
            tem_atividade_andamento=False,
            retorno_ultimo_servico_ms=int(fim_servico.timestamp() * 1000),
            now=now,
        )

        self.assertTrue(res["aberto"])
        self.assertEqual(res["classificacao"], "ABERTO")

    def test_os_presa_mais_de_10_horas(self):
        """Equipe iniciou deslocamento/execucao ha mais de 10 horas sem novo sinal.
        A OS deve ser auto-concluida e o turno fechado."""
        now = datetime.datetime(2026, 9, 22, 18, 30, 0, tzinfo=TZ)
        inicio_os = datetime.datetime(2026, 9, 22, 7, 0, 0, tzinfo=TZ)
        inicio_os_ms = int(inicio_os.timestamp() * 1000)

        os_ativa = {
            "protocolo": "50999999",
            "status": "EXECUCAO",
            "inicio_ms": inicio_os_ms,
            "observadoEm": inicio_os.isoformat(),
        }

        res = consolidar_turno_por_contexto(
            marcadores_t=[],
            eventos_servico_ms=[inicio_os_ms],
            tem_atividade_andamento=True,
            now=now,
            atividade_atual=os_ativa,
            ss_em_andamento=[os_ativa],
        )

        self.assertFalse(res["aberto"])
        self.assertEqual(res["classificacao"], "FECHADO")
        self.assertEqual(os_ativa.get("status"), "CONCLUSAO")

    def test_abertura_legitima_pela_manha(self):
        """Equipe descansou a noite toda e abre turno às 07:30."""
        now = datetime.datetime(2026, 9, 22, 8, 0, 0, tzinfo=TZ)
        t_abertura = datetime.datetime(2026, 9, 22, 7, 30, 0, tzinfo=TZ)
        t_abertura_ms = int(t_abertura.timestamp() * 1000)

        marcadores_t = [{"start": t_abertura_ms, "end": None}]

        res = consolidar_turno_por_contexto(
            marcadores_t=marcadores_t,
            eventos_servico_ms=[],
            tem_atividade_andamento=False,
            now=now,
        )

        self.assertTrue(res["aberto"])
        self.assertEqual(res["classificacao"], "ABERTO")
        self.assertEqual(res["inicio_ms"], t_abertura_ms)
        self.assertIsNone(res["fim_ms"])

    def test_dois_turnos_no_mesmo_dia_civil(self):
        """Caso E3K59: Plantão da madrugada fechou às 03:55:47.
        Equipe descansou 11h e abriu novo turno às 14:56:00, fechando às 18:00:00.
        Deve registrar ambos os turnos e indicar Artigo 66 cumprido."""
        now = datetime.datetime(2026, 9, 22, 19, 0, 0, tzinfo=TZ)
        t_fim_1 = datetime.datetime(2026, 9, 22, 3, 55, 47, tzinfo=TZ)
        t_ini_2 = datetime.datetime(2026, 9, 22, 14, 56, 0, tzinfo=TZ)
        t_fim_2 = datetime.datetime(2026, 9, 22, 18, 0, 0, tzinfo=TZ)

        marcadores_t = [
            {"start": int(t_fim_1.timestamp() * 1000), "end": None},
            {"start": int(t_ini_2.timestamp() * 1000), "end": None},
            {"start": int(t_fim_2.timestamp() * 1000), "end": None},
        ]
        # Serviços da tarde
        s1 = datetime.datetime(2026, 9, 22, 15, 10, 0, tzinfo=TZ)
        s2 = datetime.datetime(2026, 9, 22, 17, 30, 0, tzinfo=TZ)
        eventos_servico = [
            int(s1.timestamp() * 1000),
            int(s2.timestamp() * 1000),
        ]
        ss_exec = [
            {"inicio_ms": int(s1.timestamp() * 1000), "fim_ms": int(s1.timestamp() * 1000) + 1800000},
            {"inicio_ms": int(s2.timestamp() * 1000), "fim_ms": int(s2.timestamp() * 1000) + 1500000},
        ]

        snapshot_ant = {
            "jornada": {
                "turno": {
                    "inicio": "2026-09-21T05:39:42-03:00",
                    "status": "ABERTO",
                }
            }
        }

        res = consolidar_turno_por_contexto(
            marcadores_t=marcadores_t,
            eventos_servico_ms=eventos_servico,
            tem_atividade_andamento=False,
            retorno_ultimo_servico_ms=int(s2.timestamp() * 1000) + 1500000,
            now=now,
            snapshot_anterior=snapshot_ant,
            ss_executadas=ss_exec,
        )

        self.assertFalse(res["aberto"])
        self.assertEqual(res["classificacao"], "FECHADO")
        self.assertEqual(res["fim_ms"], int(t_fim_2.timestamp() * 1000))
        self.assertEqual(len(res["turnos"]), 2)
        self.assertEqual(res["turnos"][0]["status"], "FECHADO")
        self.assertIn("03:55:47", res["turnos"][0]["fim"])
        self.assertEqual(res["turnos"][1]["status"], "FECHADO")
        self.assertIn("18:00:00", res["turnos"][1]["fim"])
        self.assertTrue(res["artigo66"]["cumprido"])
        self.assertGreaterEqual(res["artigo66"]["descansoMinutos"], 660)

    def test_turno_madrugada_fechado_e_em_descanso(self):
        """Plantão da madrugada fechou às 03:55:47. São 10:00 da manhã.
        O turno deve constar como FECHADO e o Artigo 66 deve calcular 364 min de descanso (não cumprido ainda)."""
        now = datetime.datetime(2026, 9, 22, 10, 0, 0, tzinfo=TZ)
        t_fim_1 = datetime.datetime(2026, 9, 22, 3, 55, 47, tzinfo=TZ)

        marcadores_t = [
            {"start": int(t_fim_1.timestamp() * 1000), "end": None},
        ]
        snapshot_ant = {
            "jornada": {
                "turno": {
                    "inicio": "2026-09-21T05:39:42-03:00",
                    "status": "ABERTO",
                }
            }
        }

        res = consolidar_turno_por_contexto(
            marcadores_t=marcadores_t,
            eventos_servico_ms=[],
            tem_atividade_andamento=False,
            retorno_ultimo_servico_ms=int(t_fim_1.timestamp() * 1000) - 60000,
            now=now,
            snapshot_anterior=snapshot_ant,
        )

        self.assertFalse(res["aberto"])
        self.assertEqual(res["classificacao"], "FECHADO")
        self.assertFalse(res["artigo66"]["cumprido"])
        self.assertAlmostEqual(res["artigo66"]["descansoMinutos"], 364, delta=2)
        self.assertEqual(len(res["turnos"]), 1)

    def test_abertura_emergencial_quebra_artigo66(self):
        """Plantão fechou às 03:55:47. Às 09:30 (5h34m depois) há emergência, equipe abre turno e inicia serviço.
        O turno deve abrir, com artigo66.cumprido == False."""
        now = datetime.datetime(2026, 9, 22, 10, 0, 0, tzinfo=TZ)
        t_fim_1 = datetime.datetime(2026, 9, 22, 3, 55, 47, tzinfo=TZ)
        t_ini_2 = datetime.datetime(2026, 9, 22, 9, 30, 0, tzinfo=TZ)
        s_emerg = datetime.datetime(2026, 9, 22, 9, 35, 0, tzinfo=TZ)

        marcadores_t = [
            {"start": int(t_fim_1.timestamp() * 1000), "end": None},
            {"start": int(t_ini_2.timestamp() * 1000), "end": None},
        ]
        eventos_servico = [int(s_emerg.timestamp() * 1000)]
        ss_andamento = [{
            "protocolo": "50123456",
            "status": "DESLOCAMENTO",
            "inicio_ms": int(s_emerg.timestamp() * 1000),
            "inicioIso": s_emerg.isoformat(),
        }]

        snapshot_ant = {
            "jornada": {
                "turno": {
                    "inicio": "2026-09-21T05:39:42-03:00",
                    "status": "ABERTO",
                }
            }
        }

        res = consolidar_turno_por_contexto(
            marcadores_t=marcadores_t,
            eventos_servico_ms=eventos_servico,
            tem_atividade_andamento=True,
            now=now,
            snapshot_anterior=snapshot_ant,
            ss_em_andamento=ss_andamento,
            atividade_atual=ss_andamento[0],
        )

        self.assertTrue(res["aberto"])
        self.assertEqual(res["classificacao"], "ABERTO")
        self.assertEqual(res["inicio_ms"], int(t_ini_2.timestamp() * 1000))
        self.assertFalse(res["artigo66"]["cumprido"])
        self.assertAlmostEqual(res["artigo66"]["descansoMinutos"], 334, delta=2)
        self.assertEqual(len(res["turnos"]), 2)

    def test_os_iniciada_ontem_concluida_hoje_pertence_a_ontem(self):
        """OS iniciada às 23:50 do dia 21 e concluída às 00:40 do dia 22 pertence à data base do dia 21."""
        from coletor.storage import compact_service, merge_daily_document

        s_ontem = datetime.datetime(2026, 9, 21, 23, 50, 0, tzinfo=TZ)
        s_concl = datetime.datetime(2026, 9, 22, 0, 40, 0, tzinfo=TZ)

        raw_servico = {
            "protocolo": "20261234567890",
            "status": "CONCLUSAO",
            "inicio_ms": int(s_ontem.timestamp() * 1000),
            "inicioIso": s_ontem.isoformat(),
            "inicioDeslocamento": s_ontem.isoformat(),
            "fim_ms": int(s_concl.timestamp() * 1000),
            "fimIso": s_concl.isoformat(),
            "retorno": s_concl.isoformat(),
        }

        compacted = compact_service("E3K59", "2026-09-22", raw_servico)
        self.assertEqual(compacted["baseDay"], "2026-09-21")

        # Quando mergeado para o dia 22, este servico não entra no historico do dia 22
        current = {
            "teamKey": "E3K59",
            "date": "2026-09-22",
            "updatedAt": "2026-09-22T01:00:00-03:00",
            "ssExecutadas": [raw_servico],
            "turno": {"inicio_iso": "2026-09-21T20:00:00-03:00", "status": "ABERTO"},
        }
        merged_22 = merge_daily_document(None, current, "2026-09-22")
        self.assertEqual(len(merged_22["ordensServico"]["historico"]), 0)

        # Quando mergeado para o dia 21, ele entra normalmente no dia 21
        merged_21 = merge_daily_document(None, current, "2026-09-21")
        self.assertEqual(len(merged_21["ordensServico"]["historico"]), 1)
        self.assertEqual(merged_21["ordensServico"]["historico"][0]["protocolo"], "20261234567890")

    def test_turnos_recentes_no_index_janela_48h(self):
        """compactar_equipe_para_index preserva os turnos recentes na jornada para a torre de controle."""
        from coletor.storage import compactar_equipe_para_index

        doc = {
            "schemaVersion": 2,
            "teamKey": "E3K59",
            "date": "2026-09-22",
            "updatedAt": "2026-09-22T18:05:00-03:00",
            "jornada": {
                "turno": {"status": "FECHADO", "inicio": "2026-09-22T14:56:00-03:00", "fim": "2026-09-22T18:00:00-03:00"},
                "turnos": [
                    {"status": "FECHADO", "inicio": "2026-09-21T05:39:42-03:00", "fim": "2026-09-22T03:55:47-03:00"},
                    {"status": "FECHADO", "inicio": "2026-09-22T14:56:00-03:00", "fim": "2026-09-22T18:00:00-03:00"},
                ],
                "artigo66": {"cumprido": True, "descansoMinutos": 660},
            },
            "ordensServico": {
                "atual": None,
                "historico": [{"protocolo": "123"}, {"protocolo": "456"}],
            }
        }

        compacted = compactar_equipe_para_index(doc)
        self.assertIn("turnosRecentes", compacted["jornada"])
        self.assertEqual(len(compacted["jornada"]["turnosRecentes"]), 2)
        self.assertTrue(compacted["jornada"]["artigo66"]["cumprido"])
        self.assertIsNone(compacted["ordensServico"]["atual"])
        self.assertEqual(compacted["ordensServico"]["totalConcluidos"], 2)


if __name__ == "__main__":
    unittest.main()
