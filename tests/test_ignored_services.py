"""Testes para o tratamento de serviços anômalos/ignorados e priorização de retorno."""

from __future__ import annotations

import unittest
from pathlib import Path
from coletor.parser import carregar_servicos_ignorados, consolidar_turno_por_contexto
import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


class IgnoredServicesTests(unittest.TestCase):
    def test_carregar_servicos_ignorados_encontra_config(self):
        ignored = carregar_servicos_ignorados(Path("dados-local"))
        self.assertIn("50968602", ignored)

    def test_fechamento_por_inatividade_prioriza_retorno(self):
        now = datetime.datetime(2026, 9, 23, 20, 30, tzinfo=LOCAL_TZ)
        ini_ms = int(datetime.datetime(2026, 9, 23, 7, 30, tzinfo=LOCAL_TZ).timestamp() * 1000)
        fim_exec_ms = int(datetime.datetime(2026, 9, 23, 17, 30, tzinfo=LOCAL_TZ).timestamp() * 1000)
        retorno_ms = int(datetime.datetime(2026, 9, 23, 18, 10, tzinfo=LOCAL_TZ).timestamp() * 1000)

        res = consolidar_turno_por_contexto(
            marcadores_t=[],
            eventos_servico_ms=[ini_ms],
            tem_atividade_andamento=False,
            retorno_ultimo_servico_ms=retorno_ms,
            now=now,
            ss_executadas=[
                {
                    "inicio_ms": ini_ms,
                    "fim_ms": fim_exec_ms,
                    "retorno": "18:10",
                }
            ],
        )

        self.assertEqual(res["classificacao"], "FECHADO")
        self.assertEqual(res["fim_ms"], retorno_ms)
        self.assertTrue(res["fim_iso"].endswith("18:10:00-03:00"))


if __name__ == "__main__":
    unittest.main()
