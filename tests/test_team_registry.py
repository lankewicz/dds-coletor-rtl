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

    def test_records_daily_assignments_and_only_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "team-registry.json")
            dates = [
                ("2026-09-25", "E3P29", "BSCCEL", "390994", "FELIPE"),
                ("2026-09-26", "E3P29", "BSCPIN", "390994", "FELIPE"),
                ("2026-09-27", "E3P29", "BSCPIN", "390995", "ELIAS"),
                ("2026-09-28", "E3X90", "BSCPIN", "390995", "ELIAS"),
            ]
            team_id = None
            for day, vehicle, base, registration, name in dates:
                result = registry.enrich_professionals(
                    vehicle,
                    [{"registration": registration, "fullName": name}],
                    f"{day}T08:00:00-03:00",
                    f"{day} | 08:00 - 17:00",
                    operational_date=day,
                    base=base,
                    tablet="CA231",
                )
                team_id = team_id or result
                self.assertEqual(result, team_id)

            registry.enrich_professionals(
                "E3X90",
                [{"registration": "390995", "fullName": "ELIAS"}],
                "2026-09-28T08:00:00-03:00",
                "2026-09-28 | 08:00 - 17:00",
                operational_date="2026-09-28",
                base="BSCPIN",
                tablet="CA231",
            )

            team = registry.data["teams"][team_id]
            self.assertEqual(len(team["assignmentHistory"]), 4)
            self.assertEqual(
                [(change["operationalDate"], change["field"]) for change in team["assignmentChanges"]],
                [
                    ("2026-09-26", "base"),
                    ("2026-09-27", "electricians"),
                    ("2026-09-28", "vehicle"),
                ],
            )
            self.assertEqual(team["assignmentChanges"][0]["from"], "BSCCEL")
            self.assertEqual(team["assignmentChanges"][0]["to"], "BSCPIN")

    def test_corrected_shift_replaces_assignment_without_false_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "team-registry.json")
            team_id = registry.observe("E3P29", "FELIPE", "2026-09-24T08:00:00-03:00")["teamId"]
            crew = [{"registration": "390994", "fullName": "FELIPE"}]
            for day, time, reference, base in [
                ("2026-09-24", "08:00", "24/09/2026 | 08:00 - 17:00", "BSCCEL"),
                ("2026-09-25", "08:00", "25/09/2026 | 08:00 - 17:00", "BSCPIN"),
                ("2026-09-25", "08:15", "25/09/2026 | 08:15 - 17:15", "BSCCEL"),
                ("2026-09-25", "08:15", "25/09/2026 | 08:15 - 17:15", "BSCCEL"),
            ]:
                registry.record_assignment(
                    team_id, day, f"{day}T{time}:00-03:00", reference,
                    "E3P29", base, "CA231", crew,
                )

            team = registry.data["teams"][team_id]
            self.assertEqual(len(team["assignmentHistory"]), 2)
            corrected = team["assignmentHistory"][1]
            self.assertEqual(corrected["observedAt"], "2026-09-25T08:15:00-03:00")
            self.assertEqual(corrected["shiftReference"], "25/09/2026 | 08:15 - 17:15")
            self.assertEqual(corrected["base"], "BSCCEL")
            self.assertEqual(team["assignmentChanges"], [])

    def test_corrected_shift_preserves_other_shift_on_same_day(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "team-registry.json")
            team_id = registry.observe("E3P29", "FELIPE", "2026-09-25T08:00:00-03:00")["teamId"]
            crew = [{"registration": "390994", "fullName": "FELIPE"}]
            for time, reference, base in [
                ("08:00", "25/09/2026 | 08:00 - 16:00", "BSCCEL"),
                ("16:00", "25/09/2026 | 16:00 - 23:00", "BSCPIN"),
                ("08:15", "25/09/2026 | 08:15 - 16:00", "BSCCEL"),
            ]:
                registry.record_assignment(
                    team_id, "2026-09-25", f"2026-09-25T{time}:00-03:00", reference,
                    "E3P29", base, "CA231", crew,
                )

            team = registry.data["teams"][team_id]
            self.assertEqual(
                [(item["shiftReference"], item["base"]) for item in team["assignmentHistory"]],
                [
                    ("25/09/2026 | 08:15 - 16:00", "BSCCEL"),
                    ("25/09/2026 | 16:00 - 23:00", "BSCPIN"),
                ],
            )
            self.assertEqual(len(team["assignmentChanges"]), 1)
            change = team["assignmentChanges"][0]
            self.assertEqual(change["field"], "base")
            self.assertEqual((change["from"], change["to"]), ("BSCCEL", "BSCPIN"))
            self.assertEqual(change["observedAt"], "2026-09-25T16:00:00-03:00")

    def test_enriches_all_contracts_from_team_page(self):
        with tempfile.TemporaryDirectory() as directory:
            records = [
                {
                    "Veiculo": "E3P29",
                    "Tablet": "CA231",
                    "Agencia": "BSCCEL",
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
            team = next(iter(registry.data["teams"].values()))
            self.assertEqual(team["assignmentHistory"][0]["base"], "BSCCEL")
            self.assertEqual(team["assignmentHistory"][0]["tablet"], "CA231")


if __name__ == "__main__":
    unittest.main()
