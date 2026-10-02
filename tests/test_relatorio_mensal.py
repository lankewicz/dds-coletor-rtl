import datetime
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coletor.historico import (
    RotalogEquipesScraper, RotalogEventosScraper, deduplicar_eventos_consolidados, executar_coleta_historico_dia,
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
    def test_varredura_mensal_retries_failed_days_after_first_pass(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.executar_coleta_historico_dia"
        ) as daily, patch("coletor.historico.RotalogEquipesScraper") as equipes_factory, patch(
            "coletor.historico.time.sleep"
        ) as sleep:
            attempts = {}

            def collect(target_date, **kwargs):
                attempts[target_date] = attempts.get(target_date, 0) + 1
                if target_date.day == 2 and attempts[target_date] < 3:
                    raise RuntimeError("portal indisponível")
                return {
                    "status": "success", "totalServicos": 1,
                    "totalKmInformado": 1.0, "totalKmAutorizadoFinal": 1.0,
                    "localRelatorioHtml": str(Path(directory) / f"{target_date}.html"),
                }

            daily.side_effect = collect
            equipes_factory.return_value.raspar_periodo.return_value = []
            result = varrer_mes(
                2026, 8, Path(directory), "Empresa", session=Mock(), max_tentativas_por_dia=3,
            )

            self.assertEqual("success", result["status"])
            self.assertEqual(3, attempts[datetime.date(2026, 8, 2)])
            self.assertEqual([60, 120], [call.args[0] for call in sleep.call_args_list])
            call_days = [call.kwargs["target_date"].day for call in daily.call_args_list]
            self.assertGreater(call_days.index(2, 2), call_days.index(31))

    def test_varredura_mensal_persists_failure_for_next_day(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.executar_coleta_historico_dia"
        ) as daily, patch("coletor.historico.RotalogEquipesScraper") as equipes_factory, patch(
            "coletor.historico.time.sleep"
        ) as sleep:
            def collect(target_date, **kwargs):
                if target_date.day == 2:
                    raise RuntimeError("offline")
                return {
                    "status": "success", "totalServicos": 0,
                    "totalKmInformado": 0.0, "totalKmAutorizadoFinal": 0.0,
                    "localRelatorioHtml": str(Path(directory) / f"{target_date}.html"),
                }

            daily.side_effect = collect
            result = varrer_mes(
                2026, 8, Path(directory), "Empresa", session=Mock(), max_tentativas_por_dia=3,
            )
            self.assertEqual("pending", result["status"])
            self.assertEqual(["2026-08-02"], result["diasPendentes"])
            self.assertEqual([60, 120], [call.args[0] for call in sleep.call_args_list])
            self.assertTrue(Path(result["stateFile"]).exists())
            equipes_factory.return_value.raspar_periodo.assert_not_called()

            daily.reset_mock()
            daily.side_effect = lambda target_date, **kwargs: {
                "status": "success", "totalServicos": 0,
                "totalKmInformado": 0.0, "totalKmAutorizadoFinal": 0.0,
                "localRelatorioHtml": str(Path(directory) / f"{target_date}.html"),
            }
            equipes_factory.return_value.raspar_periodo.return_value = []
            resumed = varrer_mes(
                2026, 8, Path(directory), "Empresa", session=Mock(), max_tentativas_por_dia=3,
            )
            self.assertEqual("success", resumed["status"])
            self.assertEqual([2], [call.kwargs["target_date"].day for call in daily.call_args_list])

    def test_varredura_mensal_retoma_apos_interrupcao_do_processo(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.executar_coleta_historico_dia"
        ) as daily, patch("coletor.historico.RotalogEquipesScraper") as equipes_factory:
            def interrupted(target_date, **kwargs):
                if target_date.day == 3:
                    raise KeyboardInterrupt()
                return {
                    "status": "success", "totalServicos": 0,
                    "totalKmInformado": 0.0, "totalKmAutorizadoFinal": 0.0,
                    "localRelatorioHtml": str(Path(directory) / f"{target_date}.html"),
                }

            daily.side_effect = interrupted
            with self.assertRaises(KeyboardInterrupt):
                varrer_mes(2026, 8, Path(directory), "Empresa", session=Mock())

            daily.reset_mock()
            daily.side_effect = lambda target_date, **kwargs: {
                "status": "success", "totalServicos": 0,
                "totalKmInformado": 0.0, "totalKmAutorizadoFinal": 0.0,
                "localRelatorioHtml": str(Path(directory) / f"{target_date}.html"),
            }
            equipes_factory.return_value.raspar_periodo.return_value = []
            resumed = varrer_mes(2026, 8, Path(directory), "Empresa", session=Mock())

            self.assertEqual("success", resumed["status"])
            self.assertEqual(list(range(3, 32)), [call.kwargs["target_date"].day for call in daily.call_args_list])
    def test_deduplica_fim_de_turno_repetido_sem_alterar_entrada(self):
        rows = [
            {"Veiculo": "E3K91", "Contrato": "4600025149", "Evento": "Fim de turno", "Data Evento": "21/09/26 02:10"},
            {"Veiculo": "E3K91", "Contrato": "4600025149", "Evento": "Fim de turno", "Data Evento": "21/09/26 02:10"},
            {"Veiculo": "E3K91", "Contrato": "4600025149", "Evento": "Fim de turno", "Data Evento": "21/09/26 02:11"},
            {"Veiculo": "E3K91", "Contrato": "4600025149", "Evento": "Fim de turno", "Data Evento": "21/09/26 02:12"},
            {"Veiculo": "E3389", "Contrato": "4600026988", "Evento": "Fim de turno", "Data Evento": "21/09/26 02:19"},
        ]
        original = [dict(row) for row in rows]
        deduplicated, stats = deduplicar_eventos_consolidados(rows)
        self.assertEqual(original, rows)
        self.assertEqual(2, len(deduplicated))
        self.assertEqual("21/09/26 02:10", deduplicated[0]["Data Evento"])
        self.assertEqual(1, stats["duplicatasExatasRemovidas"])
        self.assertEqual(2, stats["repeticoesTecnicasRemovidas"])
        self.assertEqual(3, stats["totalRemovidos"])

    def test_novo_inicio_libera_outro_fim_de_turno(self):
        rows = [
            {"Veiculo": "E3K91", "Contrato": "4600025149", "Evento": "Fim de turno", "Data Evento": "21/09/26 02:10"},
            {"Veiculo": "E3K91", "Contrato": "4600025149", "Evento": "Fim de turno", "Data Evento": "21/09/26 02:40"},
            {"Veiculo": "E3K91", "Contrato": "4600025149", "Evento": "Inicio de turno", "Data Evento": "21/09/26 09:00"},
            {"Veiculo": "E3K91", "Contrato": "4600025149", "Evento": "Fim de turno", "Data Evento": "21/09/26 18:00"},
        ]
        deduplicated, stats = deduplicar_eventos_consolidados(rows)
        self.assertEqual(["Fim de turno", "Inicio de turno", "Fim de turno"], [r["Evento"] for r in deduplicated])
        self.assertEqual(1, stats["repeticoesTecnicasRemovidas"])

    def test_autenticacao_usa_interface_real_do_cliente(self):
        with patch("coletor.historico_scrapers.CrawlerRotalog", autospec=CrawlerRotalog) as factory:
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

            with gzip.open(result["localArquivoMensal"], "rt", encoding="utf-8") as stream:
                payload = json.load(stream)
            self.assertEqual("Empresa <Teste>", payload["empresa"])
            self.assertEqual(2, payload["totalEquipes"])
            self.assertEqual(35.56, payload["totalKmRecuperadoParecer"])
            self.assertEqual([], list(Path(directory).rglob("*-relatorio.html")))
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
            self.assertTrue(Path(result["localArquivoMensal"]).exists())
            # Verifica existência do primeiro e último diário
            self.assertTrue((Path(directory) / "rotalog" / "quilometragem" / "diario" / "2026-08-01.json.gz").exists())
            self.assertTrue((Path(directory) / "rotalog" / "quilometragem" / "diario" / "2026-08-31.json.gz").exists())

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
            detail_call = equipes_factory.return_value.raspar_dia.call_args
            self.assertEqual(("14/09/2026",), detail_call.args)
            self.assertTrue(detail_call.kwargs["enriquecer_detalhes"])
            self.assertTrue(callable(detail_call.kwargs["progresso"]))

            with gzip.open(result["localArquivoKm"], "rt", encoding="utf-8") as stream:
                payload = json.load(stream)
            self.assertEqual("E3733", payload["servicos"][0]["equipe"])
            self.assertEqual(15.5, payload["totalKmInformado"])
            self.assertEqual(12, payload["totalKmAutorizadoFinal"])
            self.assertNotIn("localRelatorioHtml", result)
            self.assertEqual([], list(Path(directory).rglob("*-relatorio.html")))
            self.assertTrue(Path(result["localArquivoKm"]).exists())
            self.assertEqual(
                {
                    "eventosScrapeSeconds", "equipesScrapeSeconds", "consolidacaoSeconds",
                    "cadastroEquipesSeconds", "arquivosRelatorioSeconds",
                },
                set(result["phaseDurations"]),
            )
            raw_path = Path(result["localArquivoEventosBrutos"])
            self.assertTrue(raw_path.exists())
            with gzip.open(raw_path, "rt", encoding="utf-8") as stream:
                raw = json.load(stream)
            self.assertEqual(1, raw["totalRegistros"])
            self.assertEqual("9876543210", raw["registros"][0]["Protocolo"])

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
            self.assertIn("pendente", result_err["erroUpload"])

    def test_envio_mensal_pendente_reutiliza_arquivo_local_sem_nova_coleta(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEquipesScraper"
        ) as equipes_factory, patch("coletor.historico.executar_coleta_historico_dia") as daily:
            root = Path(directory)
            monthly_path = root / "rotalog/quilometragem/mensal/2026-08.json.gz"
            monthly_path.parent.mkdir(parents=True)
            with gzip.open(monthly_path, "wt", encoding="utf-8") as stream:
                json.dump({"month": "2026-08", "totalServicos": 7}, stream)
            state_path = root / "rotalog/sync/empresa/historico/mensal/2026-08-coleta.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "status": "monthly_upload_pending",
                "completedDays": [f"2026-08-{day:02d}" for day in range(1, 32)],
            }), encoding="utf-8")
            store = Mock(enabled=True, root_prefix="dados/empresa/rotalog", bucket_name="bucket")

            result = varrer_mes(2026, 8, root, "Empresa", True, store)

            self.assertEqual("success", result["status"])
            self.assertTrue(result["alreadyCollected"])
            self.assertEqual(7, result["totalServicos"])
            daily.assert_not_called()
            equipes_factory.assert_not_called()

    def test_coleta_diaria_envia_bruto_e_consolidado_ao_firebase(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEventosScraper"
        ) as eventos_factory, patch("coletor.historico.RotalogEquipesScraper") as equipes_factory:
            eventos_factory.return_value.raspar_periodo.return_value = [evento("4600026988", "10", "8")]
            equipes_factory.return_value.raspar_dia.return_value = []
            store = Mock(enabled=True, root_prefix="dados/empresa/rotalog")

            result = executar_coleta_historico_dia(
                datetime.date(2026, 9, 14), Path(directory), "Empresa", True, store,
                atualizar_terminal=False,
            )

            self.assertEqual(2, store.save_blob.call_count)
            paths = [call.args[0] for call in store.save_blob.call_args_list]
            self.assertIn("dados/empresa/rotalog/eventos/diario/2026-09-14.json.gz", paths)
            self.assertIn("dados/empresa/rotalog/quilometragem/diario/2026-09-14.json.gz", paths)
            self.assertTrue(result["firebaseRawSynced"])
            self.assertTrue(result["firebaseSynced"])

    def test_coleta_diaria_identica_nao_reenvia_arquivos(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEventosScraper"
        ) as eventos_factory, patch("coletor.historico.RotalogEquipesScraper") as equipes_factory:
            eventos_factory.return_value.raspar_periodo.return_value = [evento("4600026988", "10", "8")]
            equipes_factory.return_value.raspar_dia.return_value = []
            store = Mock(enabled=True, root_prefix="dados/empresa/rotalog", bucket_name="bucket")

            first = executar_coleta_historico_dia(
                datetime.date(2026, 9, 14), Path(directory), "Empresa", True, store,
                atualizar_terminal=False,
            )
            second = executar_coleta_historico_dia(
                datetime.date(2026, 9, 14), Path(directory), "Empresa", True, store,
                atualizar_terminal=False,
            )

            self.assertEqual(2, store.save_blob.call_count)
            self.assertTrue(first["firebaseUploaded"])
            self.assertFalse(second["firebaseUploaded"])
            self.assertFalse(second["firebaseRawUploaded"])

    def test_falha_de_upload_diario_nao_repete_coleta_e_fica_na_fila(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEventosScraper"
        ) as eventos_factory, patch("coletor.historico.RotalogEquipesScraper") as equipes_factory:
            eventos_factory.return_value.raspar_periodo.return_value = [evento("4600026988", "10", "8")]
            equipes_factory.return_value.raspar_dia.return_value = []
            store = Mock(enabled=True, root_prefix="dados/empresa/rotalog", bucket_name="bucket")
            store.save_blob.side_effect = OSError("offline")

            result = executar_coleta_historico_dia(
                datetime.date(2026, 9, 14), Path(directory), "Empresa", True, store,
                atualizar_terminal=False,
            )

            self.assertEqual("success", result["status"])
            self.assertEqual("pending", result["syncStatus"])
            self.assertEqual(2, result["uploadQueue"]["pending"])
            self.assertEqual(2, result["uploadQueue"]["totalAttempts"])
            self.assertEqual("offline", result["uploadQueue"]["lastError"])
            self.assertIsNotNone(result["uploadQueue"]["oldestQueuedAt"])
            self.assertIsNotNone(result["uploadQueue"]["lastAttemptAt"])
            queue_path = Path(directory) / "rotalog/sync/empresa/historico/pending-uploads.json"
            self.assertEqual(2, len(json.loads(queue_path.read_text(encoding="utf-8"))["items"]))

            store.save_blob.side_effect = None
            resumed = executar_coleta_historico_dia(
                datetime.date(2026, 9, 15), Path(directory), "Empresa", True, store,
                atualizar_terminal=False,
            )
            self.assertEqual("synced", resumed["syncStatus"])
            self.assertEqual({}, json.loads(queue_path.read_text(encoding="utf-8"))["items"])

    def test_mes_vazio_informa_ausencia(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEquipesScraper"
        ) as equipes_factory:
            equipes_factory.return_value.raspar_periodo.return_value = []
            result = varrer_mes(2026, 8, Path(directory), "Empresa", coletar_diarios=False)
            self.assertEqual(0, result["totalRegistros"])
            self.assertTrue(Path(result["localArquivoMensal"]).exists())

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

    def test_parser_detalhe_separa_registro_e_nome_dos_eletricistas(self):
        from bs4 import BeautifulSoup
        html = """
        <div>
          <div>Eletricista 1: 354291 - ROBERT RENAN DA SILVA CARLESSO</div>
          <div>Eletricista 2: 392267 - MARCIO LUIZ GOMES</div>
        </div>
        """
        details = RotalogEquipesScraper._parse_electricians(BeautifulSoup(html, "html.parser"))
        self.assertEqual("354291", details["Eletricista 1 Registro"])
        self.assertEqual("ROBERT RENAN DA SILVA CARLESSO", details["Eletricista 1 Nome"])
        self.assertEqual("392267", details["Eletricista 2 Registro"])
        self.assertEqual("MARCIO LUIZ GOMES", details["Eletricista 2 Nome"])

        resumo = estruturar_resumo_equipes([{**row_equipe(), **details}])
        registro = resumo["registros"][0]
        self.assertEqual("354291", registro["eletricista1Registro"])
        self.assertEqual("ROBERT RENAN DA SILVA CARLESSO", registro["eletricista1Nome"])


if __name__ == "__main__":
    unittest.main()

