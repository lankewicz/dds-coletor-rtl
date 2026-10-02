import tempfile
import unittest
from pathlib import Path

from coletor.team_registry import TeamRegistry


class TeamRegistryLockTests(unittest.TestCase):
    def test_recarrega_estado_antes_de_atualizacao_exclusiva(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team-registry.json"
            first = TeamRegistry(path)
            stale = TeamRegistry(path)

            with first.exclusive_update():
                first.observe("E3A01", "ANA", "2026-09-30T03:00:00-03:00")

            with stale.exclusive_update():
                stale.observe("E3A02", "BRUNO", "2026-09-30T03:01:00-03:00")

            saved = TeamRegistry(path)
            vehicles = {
                assignment["vehicleCode"]
                for team in saved.data["teams"].values()
                for assignment in team.get("vehicleAssignments", [])
            }
            self.assertEqual({"E3A01", "E3A02"}, vehicles)


if __name__ == "__main__":
    unittest.main()
