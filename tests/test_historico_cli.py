import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import coletor.historico_scrapers as scrapers
from main import executar_historico


class HistoricoCliTests(unittest.TestCase):
    def test_scraper_tem_dependencia_de_paginacao_importada(self):
        self.assertEqual(3, scrapers.math.ceil(2.1))
        self.assertEqual("1.234,50", scrapers.formatar_numero_br(1234.5, 2))

    def test_falha_da_coleta_nao_imprime_resumo_de_sucesso(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "main.executar_coleta_historico_dia", side_effect=NameError("falha simulada")
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            exit_code = executar_historico(
                "2026-09-29", output_dir=Path(directory), enable_firebase=False,
            )

        self.assertEqual(1, exit_code)
        self.assertIn("COLETA DIÁRIA FALHOU", output.getvalue())
        self.assertNotIn("CONCLUÍDA COM SUCESSO", output.getvalue())
        self.assertIn("falha simulada", output.getvalue())


if __name__ == "__main__":
    unittest.main()
