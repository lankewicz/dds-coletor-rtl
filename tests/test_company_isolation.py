import unittest
from pathlib import Path
import tempfile
import datetime
from unittest.mock import Mock, patch

from coletor.storage import (
    RotalogExecutionLog,
    RotalogGcsSnapshotStore,
    RotalogTeamFileRepository,
    company_key,
    rotalog_gcs_paths,
    write_json,
)
from main import LocalRotalogRunner
from coletor.historico import executar_coleta_historico_dia, varrer_mes


class CompanyIsolationTests(unittest.TestCase):
    def test_company_key_normalizes_accents_and_spaces(self):
        self.assertEqual(company_key("  Elétrica São José  "), "eletrica-sao-jose")

    def test_default_paths_share_one_company_root(self):
        paths = rotalog_gcs_paths("Elétrica São José")
        root = "dados/eletrica-sao-jose/rotalog"
        self.assertEqual(paths["root"], root)
        self.assertEqual(paths["index"], f"{root}/equipes/current/index.json.gz")
        self.assertEqual(paths["teams"], f"{root}/equipes")
        self.assertEqual(paths["logs"], f"{root}/logs")
        self.assertEqual(paths["kilometers"], f"{root}/quilometragem")

    def test_template_root_is_expanded_for_selected_company(self):
        paths = rotalog_gcs_paths(
            "Outra Empresa",
            root_prefix="clientes/{empresa}/dados/rotalog",
        )
        self.assertEqual(paths["root"], "clientes/outra-empresa/dados/rotalog")

    def test_legacy_index_configuration_derives_all_other_paths(self):
        paths = rotalog_gcs_paths(
            "ChicoEletro",
            index_blob="dados/chicoeletro/rotalog/equipes/current/index.json.gz",
        )
        self.assertEqual(paths["teams"], "dados/chicoeletro/rotalog/equipes")
        self.assertEqual(paths["logs"], "dados/chicoeletro/rotalog/logs")

    def test_configuration_for_another_company_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "não contém a chave"):
            rotalog_gcs_paths(
                "Outra Empresa",
                index_blob="dados/chicoeletro/rotalog/equipes/current/index.json.gz",
            )

    def test_conflicting_root_and_index_are_blocked(self):
        with self.assertRaisesRegex(ValueError, "raízes diferentes"):
            rotalog_gcs_paths(
                "ChicoEletro",
                root_prefix="dados/chicoeletro/rotalog",
                index_blob="arquivo/chicoeletro/rotalog/equipes/current/index.json.gz",
            )

    def test_repositories_use_the_derived_root(self):
        paths = rotalog_gcs_paths("Empresa Teste")
        store = RotalogGcsSnapshotStore(
            "bucket",
            paths["index"],
            root_prefix=paths["root"],
        )
        teams = RotalogTeamFileRepository(store, paths["teams"])
        logs = RotalogExecutionLog(store, paths["logs"])
        self.assertEqual(teams.daily_path("2026-09-14", "E3733"),
                         "dados/empresa-teste/rotalog/equipes/daily/2026-09-14/E3733.json.gz")
        self.assertEqual(logs.path("2026-09-14"),
                         "dados/empresa-teste/rotalog/logs/2026-09-14.json.gz")

    def test_persistent_queues_are_separate_per_company(self):
        first = LocalRotalogRunner(Path("data"), "Empresa A")
        second = LocalRotalogRunner(Path("data"), "Empresa B")
        self.assertNotEqual(first.daily_sync_queue_path, second.daily_sync_queue_path)

    def test_runner_blocks_local_index_from_another_company(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LocalRotalogRunner(Path(directory), "Empresa B")
            write_json(runner.index_path, {"company": "Empresa A", "equipes": {}})
            result = runner.run_once()
            self.assertEqual(result["status"], "error")
            self.assertIn("diretório local separado", result["error"])

    def test_runner_rejects_daily_file_from_another_company(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LocalRotalogRunner(Path(directory), "Empresa B")
            path = Path(directory) / "rotalog/equipes/daily/2026-09-14/E3733.json.gz"
            write_json(path, {
                "company": "Empresa A",
                "companyKey": "empresa-a",
                "teamKey": "E3733",
                "date": "2026-09-14",
                "jornada": {},
                "ordensServico": {},
            })
            with self.assertRaisesRegex(RuntimeError, "use outro --output-dir"):
                runner._load_daily_with_recovery(path, "2026-09-14", "E3733")

    def test_kilometer_uploads_follow_the_same_custom_root(self):
        store = Mock(enabled=True, root_prefix="clientes/empresa-teste/rotalog")
        scraper = Mock()
        scraper.raspar_periodo.return_value = []
        with tempfile.TemporaryDirectory() as directory, patch(
            "coletor.historico.RotalogEventosScraper", return_value=scraper
        ):
            executar_coleta_historico_dia(
                datetime.date(2026, 9, 14),
                Path(directory),
                "Empresa Teste",
                enable_firebase=True,
                firebase_store=store,
            )
            varrer_mes(
                2026,
                8,
                Path(directory),
                "Empresa Teste",
                enable_firebase=True,
                firebase_store=store,
            )
        uploaded_paths = [call.args[0] for call in store.save_blob.call_args_list]
        self.assertIn(
            "clientes/empresa-teste/rotalog/quilometragem/diario/2026-09-14.json.gz",
            uploaded_paths,
        )
        self.assertIn(
            "clientes/empresa-teste/rotalog/quilometragem/mensal/2026-08.json.gz",
            uploaded_paths,
        )


if __name__ == "__main__":
    unittest.main()
