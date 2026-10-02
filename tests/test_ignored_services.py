"""Testes para o tratamento de serviços anômalos/ignorados e priorização de retorno."""

from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from coletor.parser import (
    _corrigir_inicio_execucao_ativa,
    carregar_servicos_ignorados,
    consolidar_turno_por_contexto,
)
import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


class IgnoredServicesTests(unittest.TestCase):
    def test_execucao_ativa_iniciada_ontem_usa_hora_da_coleta(self):
        scraped_at = datetime.datetime(2026, 9, 28, 10, 30, tzinfo=LOCAL_TZ)
        service = {
            "status": "EXECUCAO",
            "inicioIso": "2026-09-27T22:00:00-03:00",
            "inicioDeslocamento": "2026-09-27T21:30:00-03:00",
            "inicioExecucao": "22:00",
        }

        _corrigir_inicio_execucao_ativa(service, scraped_at)

        self.assertEqual(service["inicioExecucao"], scraped_at.isoformat())
        self.assertEqual(service["inicioDeslocamento"], "2026-09-27T21:30:00-03:00")

    def test_servico_concluido_nao_tem_inicio_execucao_reescrito(self):
        scraped_at = datetime.datetime(2026, 9, 28, 10, 30, tzinfo=LOCAL_TZ)
        service = {
            "status": "CONCLUSAO",
            "inicioIso": "2026-09-27T22:00:00-03:00",
            "inicioExecucao": "22:00",
        }

        _corrigir_inicio_execucao_ativa(service, scraped_at)

        self.assertEqual(service["inicioExecucao"], "22:00")

    def test_carregar_servicos_ignorados_encontra_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "rotalog" / "config" / "ignored_services.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps(["50968602"]), encoding="utf-8")

            ignored = carregar_servicos_ignorados(Path(directory))

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
