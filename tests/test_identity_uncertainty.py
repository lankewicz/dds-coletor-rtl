import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import LocalRotalogRunner
from coletor.merger import merge_daily_document
from coletor.storage import load_json, compactar_equipe_para_index, changed_fields


class IdentityUncertaintyTests(unittest.TestCase):
    def test_explicit_uncertainty_does_not_assign_history_to_new_identity(self):
        old = {"teamKey": "E2269", "teamId": "TEAM-OLD", "membersKey": "JOAO|PEDRO",
               "date": "2026-10-19"}
        new = {"teamKey": "E2269", "teamId": "TEAM-NEW", "membersKey": "MARCOS|PEDRO"}
        result = merge_daily_document(old, new, "2026-10-19", allow_identity_uncertainty=True)
        self.assertIsNone(result["teamId"])
        self.assertIsNone(result["membersKey"])
        self.assertEqual(result["conexao"]["colaborador"], "PEDRO NÃO IDENTIFICADO")
        self.assertEqual(result["identityResolution"]["candidateTeamIds"], ["TEAM-NEW", "TEAM-OLD"])
        projection = compactar_equipe_para_index(result)
        self.assertEqual(projection["identitySource"], "UNRESOLVED")
        self.assertEqual(compactar_equipe_para_index(projection), projection)
        again = merge_daily_document(result, new, "2026-10-19", allow_identity_uncertainty=True)
        self.assertIsNone(again["teamId"])
        self.assertEqual(again["identityResolution"], result["identityResolution"])

    def test_wrong_vehicle_or_date_still_rejected(self):
        for old in ({"teamKey": "E9999"}, {"teamKey": "E2269", "date": "2026-10-18"}):
            with self.assertRaises(ValueError):
                merge_daily_document(old, {"teamKey": "E2269"}, "2026-10-19", allow_identity_uncertainty=True)

    def test_operational_services_and_shift_history_survive_conflict(self):
        old = {"teamKey": "E2269", "teamId": "TEAM-OLD", "date": "2026-10-19",
               "membersKey": "JOAO|PEDRO",
               "jornada": {"turnos": [{"inicio": "2026-10-19T08:00:00-03:00", "fim": "2026-10-19T09:00:00-03:00"}]},
               "ordensServico": {"historico": [{"protocolo": "12345678", "serviceId": "E2269_old",
                   "statusAtual": "CONCLUSAO", "inicioDeslocamento": "2026-10-19T08:10:00-03:00",
                   "inicioExecucao": "2026-10-19T08:20:00-03:00", "baseDay": "2026-10-19"}]}}
        service = {"protocolo": "87654321", "status": "EXECUCAO",
                   "inicioDeslocamento": "10:00", "inicioExecucao": "10:30"}
        new = {"teamKey": "E2269", "teamId": "TEAM-NEW", "membersKey": "MARCOS|PEDRO",
               "updatedAtIso": "2026-10-19T11:00:00-03:00",
               "turno": {"aberto": True, "inicio": "10:00", "turnos": [
                   {"inicio": "2026-10-19T10:00:00-03:00", "fim": None}]},
               "atividadeAtual": service, "ssEmAndamento": [service]}
        result = merge_daily_document(old, new, "2026-10-19", allow_identity_uncertainty=True)
        self.assertIsNone(result["teamId"])
        self.assertEqual(result["ordensServico"]["historico"][0]["protocolo"], "12345678")
        self.assertEqual(result["ordensServico"]["atual"]["protocolo"], "87654321")
        self.assertEqual(len(result["jornada"]["turnos"]), 2)
        self.assertEqual(result["jornada"]["turno"]["status"], "ABERTO")

    def test_uncertainty_is_an_operational_change(self):
        old = {"jornada": {}, "identitySource": "UNRESOLVED", "identityResolution": {"candidateTeamIds": ["A", "B"]}}
        new = {**old, "identityResolution": {"candidateTeamIds": ["A", "B", "C"]}}
        self.assertIn("identityResolution", changed_fields(old, new))

    def test_sequential_collections_and_restart_preserve_original_and_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = LocalRotalogRunner(root, "EmpresaTeste")
            team = {"equipe_codigo": "E2269", "colaborador": "JOAO PEDRO", "ss_pendentes": []}
            other = {"equipe_codigo": "E9999", "colaborador": "MARCOS PEDRO", "ss_pendentes": []}
            with patch("main.extrair_dados_tempo_real", return_value=[team, other]):
                first = runner.run_once()
            self.assertEqual(first["status"], "success", first)
            consolidated = load_json(Path(first["localConsolidatedDaily"]), {})
            old = consolidated["equipes"]["E2269"]
            old_id = old["teamId"]
            new_id = consolidated["equipes"]["E9999"]["teamId"]
            team["colaborador"] = "MARCOS PEDRO"
            team["ss_pendentes"] = [{"tipo": "EMERGENCIA"}]
            healthy = {"equipe_codigo": "E3733", "colaborador": "ANA BRUNO", "ss_pendentes": []}
            with patch("main.extrair_dados_tempo_real", return_value=[team, healthy]):
                second = runner.run_once()
            self.assertEqual(second["status"], "success", second)
            daily = runner._daily_path(old["date"], "E2269")
            uncertain = load_json(daily, {})
            self.assertIsNone(uncertain["teamId"])
            self.assertEqual(uncertain["fila"]["emergencia"], 1)
            self.assertEqual(uncertain["conexao"]["colaborador"], "PEDRO NÃO IDENTIFICADO")
            archived = list((root / "rotalog/equipes/identity-history").rglob("*.json"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(load_json(archived[0], {}), old)
            original_bytes = archived[0].read_bytes()
            old_reference = runner.team_registry.data["teams"][old_id]["historyFiles"][0]["path"]
            self.assertIn("identity-history/", old_reference)
            self.assertFalse(any(ref["vehicleCode"] == "E2269"
                                 for ref in runner.team_registry.data["teams"][new_id].get("historyFiles", [])))
            restarted = LocalRotalogRunner(root, "EmpresaTeste")
            with patch("main.extrair_dados_tempo_real", return_value=[team, healthy]):
                third = restarted.run_once()
            self.assertEqual(third["status"], "success", third)
            self.assertIsNone(load_json(daily, {})["teamId"])
            self.assertEqual(archived[0].read_bytes(), original_bytes)
            index = load_json(restarted.index_path, {})
            self.assertIn("E3733", index["equipes"])
            self.assertEqual(index["equipes"]["E2269"]["identityResolution"]["status"], "PENDENTE")
