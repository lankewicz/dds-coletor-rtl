import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coletor.historico import (
    RotalogEquipesScraper, RotalogEventosScraper, executar_coleta_historico_dia,
    estruturar_quilometragem_diaria, estruturar_resumo_equipes, varrer_mes,
)
from coletor.client import CrawlerRotalog


def evento(contrato, informado, aceito, protocolo="123456"):
    return {
        "Contrato": contrato,
        "Veículo": "E3733",
        "Informado com limitador (km)": informado,
        "Autorizado final(km)": aceito,
        "Protocolo": protocolo,
        "Inicio Deslo": "08:00",
        "Inicio Exec": "08:30",
        "Fim Exec": "09:15",
        "Retorno": "09:45",
    }


def row_equipe(veiculo="E3C02", contrato="4600026988"):
    return {
        "Veiculo": veiculo, "Tablet": f"{veiculo} *", "Agencia": "BSCCEL",
        "Contrato": contrato, "Data Referencia - Turno": "14/09/2026 | 08:05 - 18:44",
        "Eletricista 1": "RENNA", "Eletricista 2": "GLEYDSO", "Servicos executados": "10",
        "Informado com limitador (km)": "69", "Glosado critico (km)": "0",
        "Glosado por parecer (km)": "0", "Autorizado (km)": "53,63",
        "Recuperados por Parecer (km)": "17,78", "Autorizado Final (km)": "71,41",
        "Diferenca": "-3,37%", "Aguardando justificativa": "0 (0,0Km)",
        "Em analise": "2 (8,4Km)", "Alertas": "0",
    }


class RelatorioMensalTests(unittest.TestCase):
    def test_autenticacao_usa_interface_real_do_cliente(self):
        with patch("coletor.historico.CrawlerRotalog", autospec=CrawlerRotalog) as factory:
            scraper = RotalogEventosScraper()
            factory.return_value.criar_sessao_autenticada.assert_called_once_with()
            self.assertIs(scraper.session, factory.return_value.criar_sessao_autenticada.return_value)

    def test_mesma_equipe_em_dois_contratos_nao_mistura_valores(self):
        payload = estruturar_quilometragem_diaria([
            evento("4600026988", "1.200,50", "1.100,25", "1001"),
            {**evento("4600025149", "20", "15", "1002"), "Veículo": "E3733(2)"},
            evento("ignorado", "999", "999", "1003"),
        ], "2026-08", "Empresa")
        grupos = payload["totaisPorContrato"]
        self.assertEqual(set(grupos), {"4600026988", "4600025149"})
        self.assertEqual(grupos["4600026988"]["equipes"]["E3733"]["kmInformado"], 1200.5)
        self.assertEqual(grupos["4600025149"]["kmAutorizadoFinal"], 15)
        self.assertEqual(set(payload["totaisPorEquipe"]), {"E3733"})
        self.assertEqual(payload["totalKmInformado"], 1220.5)

    def test_varredura_mensal_busca_de_equipes_com_multiplas_paginas(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEquipesScraper"
        ) as equipes_factory:
            equipes_scraper = Mock()
            equipes_scraper.raspar_periodo.return_value = [
                row_equipe("E3C02", "4600026988"),
                row_equipe("E3C03", "4600025149"),
            ]
            equipes_factory.return_value = equipes_scraper

            result = varrer_mes(2026, 8, Path(directory), "Empresa <Teste>", coletar_diarios=False)
            equipes_scraper.raspar_periodo.assert_called_once_with("01/08/2026", "31/08/2026", progresso=None)

            html = Path(result["localRelatorioHtml"]).read_text(encoding="utf-8")
            self.assertIn("Empresa &lt;Teste&gt;", html)
            self.assertIn("Relatório Mensal de Fechamento de Equipes", html)
            self.assertIn("E3C02", html)
            self.assertIn("E3C03", html)
            self.assertIn("4600026988", html)
            self.assertIn("4600025149", html)
            self.assertIn("17,78", html)  # KM recuperada parecer
            self.assertTrue(Path(result["localArquivoMensal"]).exists())

    def test_varredura_mensal_coleta_diarios_e_mensal_conjuntos(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEventosScraper"
        ) as eventos_factory, patch(
            "coletor.historico.RotalogEquipesScraper"
        ) as equipes_factory, patch(
            "coletor.historico.CrawlerRotalog"
        ):
            eventos_scraper = Mock()
            eventos_scraper.raspar_periodo.return_value = [
                evento("4600026988", "10,00", "8,00", "111222"),
            ]
            eventos_factory.return_value = eventos_scraper

            equipes_scraper = Mock()
            equipes_scraper.raspar_dia.return_value = []
            equipes_scraper.raspar_periodo.return_value = [row_equipe("E3C02", "4600026988")]
            equipes_factory.return_value = equipes_scraper

            result = varrer_mes(2026, 8, Path(directory), "Empresa Teste", coletar_diarios=True)
            self.assertEqual(result["diasTotal"], 31)
            self.assertEqual(result["diasProcessados"], 31)
            self.assertEqual(result["totalRelatoriosDiarios"], 31)
            self.assertTrue(Path(result["localArquivoMensal"]).exists())
            self.assertTrue(Path(result["localRelatorioHtml"]).exists())
            # Verifica existência do primeiro e último diário
            self.assertTrue((Path(directory) / "rotalog" / "quilometragem" / "diario" / "2026-08-01.json.gz").exists())
            self.assertTrue((Path(directory) / "rotalog" / "quilometragem" / "diario" / "2026-08-31-relatorio.html").exists())

    def test_coleta_diaria_busca_de_listagem_eventos_e_gera_relatorio_diario(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEventosScraper"
        ) as eventos_factory, patch("coletor.historico.RotalogEquipesScraper") as equipes_factory:
            eventos_scraper = Mock()
            eventos_scraper.raspar_periodo.return_value = [
                evento("4600026988", "15,50", "12,00", "9876543210"),
            ]
            eventos_factory.return_value = eventos_scraper
            equipes_factory.return_value.raspar_dia.return_value = []

            result = executar_coleta_historico_dia(
                datetime.date(2026, 9, 14),
                Path(directory),
                "Empresa Teste",
            )
            eventos_scraper.raspar_periodo.assert_called_once()

            html = Path(result["localRelatorioHtml"]).read_text(encoding="utf-8")
            self.assertIn("Relatório Diário de Serviços e Quilometragem", html)
            self.assertIn("9876543210", html)
            self.assertIn("E3733", html)
            self.assertIn("15,50", html)
            self.assertIn("12,00", html)
            self.assertTrue(Path(result["localArquivoKm"]).exists())

    def test_envio_firebase_mensal_apos_geracao(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEquipesScraper"
        ) as equipes_factory:
            equipes_scraper = Mock()
            equipes_scraper.raspar_periodo.return_value = [row_equipe("E3C02", "4600026988")]
            equipes_factory.return_value = equipes_scraper

            store = Mock(enabled=True, root_prefix="dados/empresa/rotalog")
            result = varrer_mes(2026, 8, Path(directory), "Empresa", True, store, coletar_diarios=False)
            store.save_blob.assert_called_once()
            self.assertTrue(result["firebaseSynced"])

            store.save_blob.side_effect = RuntimeError("offline")
            result_err = varrer_mes(2026, 8, Path(directory), "Empresa", True, store, coletar_diarios=False)
            self.assertEqual(result_err["status"], "error")
            self.assertEqual(result_err["erroUpload"], "offline")

    def test_mes_vazio_informa_ausencia(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEquipesScraper"
        ) as equipes_factory:
            equipes_factory.return_value.raspar_periodo.return_value = []
            result = varrer_mes(2026, 8, Path(directory), "Empresa", coletar_diarios=False)
            html = Path(result["localRelatorioHtml"]).read_text(encoding="utf-8")
            self.assertIn("Nenhum registro encontrado", html)

    def test_resumo_oficial_preserva_totais_e_detalhamento(self):
        row = row_equipe()
        resumo = estruturar_resumo_equipes([row])
        equipe = resumo["totaisPorContrato"]["4600026988"]["equipes"]["E3C02"]
        self.assertEqual(equipe["kmAutorizadoInicial"], 53.63)
        self.assertEqual(equipe["kmRecuperadoParecer"], 17.78)
        self.assertEqual(equipe["kmAutorizadoFinal"], 71.41)
        self.assertEqual(equipe["emAnalise"], 2)
        self.assertEqual(equipe["kmEmAnalise"], 8.4)

        payload = estruturar_quilometragem_diaria(
            [evento("4600026988", "10", "8")], "2026-09-14", "Empresa", equipes_records=[row],
        )
        self.assertEqual(payload["detalhamentoServicos"]["totalKmInformado"], 10)
        self.assertEqual(payload["resumoEquipes"]["totais"]["kmInformado"], 69)

    def test_parser_da_tabela_equipes_usa_colunas_estaveis(self):
        html = "<div><table><tbody><tr>" + "".join(
            f"<td>{value}</td>" for value in [
                "E3C02", "E3C02 *", "BSCCEL", "4600026988", "14/09/2026 | 08:05 - 18:44",
                "RENNA", "GLEYDSO", "10", "69", "0", "0", "53,63", "17,78", "71,41",
                "-3,37%", "0 (0,0Km)", "0 (0,0Km)", "0",
            ]
        ) + "</tr></tbody></table></div>"
        from bs4 import BeautifulSoup
        records = RotalogEquipesScraper._parse_rows(BeautifulSoup(html, "html.parser").div)
        self.assertEqual(records[0]["Veiculo"], "E3C02")
        self.assertEqual(records[0]["Recuperados por Parecer (km)"], "17,78")


if __name__ == "__main__":
    unittest.main()

