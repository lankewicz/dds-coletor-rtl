import tempfile
import unittest
from pathlib import Path

from coletor.team_registry import TeamRegistry
from coletor.storage import merge_daily_document
from coletor.historico import enriquecer_cadastro_permanente_equipes


class TeamRegistryTests(unittest.TestCase):
    def test_member_order_keeps_same_permanent_team(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "team-registry.json")
            first = registry.observe("E3P29", "FELIPE ELIAS", "2026-09-25T09:00:00-03:00")
            second = registry.observe("E3P29", "ELIAS FELIPE", "2026-09-25T10:00:00-03:00")
            self.assertEqual(first["teamId"], second["teamId"])
            self.assertEqual(second["membersKey"], "ELIAS|FELIPE")

    def test_known_crew_keeps_history_after_vehicle_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team-registry.json"
            registry = TeamRegistry(path)
            old = registry.observe("E3P29", "FELIPE ELIAS", "2026-09-25T09:00:00-03:00")
            changed = registry.observe("E3X90", "ELIAS FELIPE", "2026-10-01T09:00:00-03:00")
            registry.save()

            self.assertEqual(old["teamId"], changed["teamId"])
            loaded = TeamRegistry(path)
            team = loaded.data["teams"][old["teamId"]]
            assignments = team["vehicleAssignments"]
            self.assertEqual(assignments[0]["vehicleCode"], "E3P29")
            self.assertEqual(assignments[0]["to"], "2026-10-01T09:00:00-03:00")
            self.assertEqual(assignments[1]["vehicleCode"], "E3X90")
            self.assertIsNone(assignments[1]["to"])

    def test_reassigned_old_vehicle_does_not_steal_known_crew_history(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "team-registry.json")
            elias = registry.observe("E3P29", "FELIPE ELIAS", "2026-09-01T09:00:00-03:00")
            other = registry.observe("E9Z99", "JOAO PEDRO", "2026-09-01T09:00:00-03:00")
            moved = registry.observe("E3P29", "PEDRO JOAO", "2026-10-01T09:00:00-03:00")

            self.assertEqual(moved["teamId"], other["teamId"])
            self.assertNotEqual(moved["teamId"], elias["teamId"])
            old_assignment = registry.data["teams"][elias["teamId"]]["vehicleAssignments"][0]
            self.assertEqual(old_assignment["reason"], "REASSIGNED")

    def test_history_index_follows_team_across_vehicle_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "team-registry.json")
            first = registry.observe("E3P29", "FELIPE ELIAS", "2026-09-25T09:00:00-03:00")
            registry.record_history(first["teamId"], "2026-09-25", "E3P29")
            second = registry.observe("E3X90", "ELIAS FELIPE", "2026-10-01T09:00:00-03:00")
            registry.record_history(second["teamId"], "2026-10-01", "E3X90")

            history = registry.data["teams"][first["teamId"]]["historyFiles"]
            self.assertEqual([item["path"] for item in history], [
                "daily/2026-09-25/E3P29.json",
                "daily/2026-10-01/E3X90.json",
            ])

    def test_daily_merge_rejects_different_permanent_team(self):
        previous = {"teamKey": "E3P29", "teamId": "TEAM-OLD", "date": "2026-09-25"}
        current = {"teamKey": "E3P29", "teamId": "TEAM-NEW", "turno": {}}
        with self.assertRaisesRegex(ValueError, "identidade permanente"):
            merge_daily_document(previous, current, "2026-09-25")

    def test_enriches_professional_registration_and_full_name(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "team-registry.json")
            identity = registry.observe("E3P29", "FELIPE ELIAS", "2026-09-25T09:00:00-03:00")
            enriched = registry.enrich_professionals("E3P29", [
                {"registration": "390994", "fullName": "FELIPE DA SILVA FORTUNATO"},
                {"registration": "390995", "fullName": "ELIAS MORAS DA SILVA SANTOS"},
            ], "2026-09-25T12:00:00-03:00", "25/09/2026 | 09:00 - 19:00")

            self.assertEqual(enriched, identity["teamId"])
            self.assertEqual(registry.data["professionals"]["390994"]["fullName"], "FELIPE DA SILVA FORTUNATO")
            team = registry.data["teams"][identity["teamId"]]
            self.assertEqual({item["registration"] for item in team["professionals"]}, {"390994", "390995"})
            self.assertTrue(any(item["membersKey"] == "ELIAS|FELIPE" for item in team["memberAliases"]))
            self.assertEqual(team["crewHistory"][0]["professionalIds"], ["390994", "390995"])

    def test_enriches_all_contracts_from_team_page(self):
        with tempfile.TemporaryDirectory() as directory:
            records = [
                {
                    "Veiculo": "E3P29",
                    "Contrato": "FORA_DOS_CONTRATOS_ALVO",
                    "Data Referencia - Turno": "24/09/2026 | 07:45 - 16:42",
                    "Eletricista 1 Registro": "390994",
                    "Eletricista 1 Nome": "FELIPE DA SILVA FORTUNATO",
                    "Eletricista 2 Registro": "390995",
                    "Eletricista 2 Nome": "ELIAS MORAS DA SILVA SANTOS",
                }
            ]
            result = enriquecer_cadastro_permanente_equipes(
                Path(directory), records, "2026-09-24"
            )
            registry = TeamRegistry(Path(directory) / "rotalog/equipes/team-registry.json")
            self.assertEqual(result["sourceRows"], 1)
            self.assertEqual(result["teamsEnriched"], 1)
            self.assertEqual(result["professionalsIdentified"], 2)
            self.assertEqual(len(registry.data["professionals"]), 2)


if __name__ == "__main__":
    unittest.main()
