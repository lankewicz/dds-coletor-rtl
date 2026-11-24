import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from coletor.historico_consolidacao import enriquecer_cadastro_permanente_equipes
from coletor.historico_parsing import extract_source_diary_id
from coletor.historico_scrapers import EQUIPES_HEADERS, RotalogEquipesScraper
from coletor.team_registry import TeamRegistry


class ShiftIdentityTests(unittest.TestCase):
    def test_diary_id_requires_portal_timeline_link(self):
        self.assertEqual(extract_source_diary_id("timeline?id=1863726"), "1863726")
        for invalid in ("", "#", "javascript:void(0)", "timeline?id=abc",
                        "timeline?id=1&id=2", "https://example.com/timeline?id=1",
                        "/rtlweb/paginas/equipes?id=1"):
            with self.subTest(link=invalid):
                self.assertEqual(extract_source_diary_id(invalid), "")

    def test_scraper_keeps_diary_id_outside_temporary_detail_fields(self):
        cells = '<td><a href="timeline?id=1863726">E3550</a></td>'
        cells += "<td></td>" * (len(EQUIPES_HEADERS) - 1)
        row = RotalogEquipesScraper._parse_rows(BeautifulSoup(f"<table><tr>{cells}</tr></table>", "html.parser"))[0]
        self.assertEqual(row["sourceDiaryId"], "1863726")

    def test_same_diary_updates_non_overlapping_hours_in_both_histories(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "registry.json")
            crew = [{"registration": "123456", "fullName": "FELIPE"}]
            for hour, reference in (("08:00", "10/11/2026 | 08:00 - 12:00"),
                                    ("18:00", "10/11/2026 | 18:00 - 23:00")):
                team_id = registry.enrich_professionals(
                    "E3550", crew, f"2026-11-10T{hour}:00-03:00", reference,
                    operational_date="2026-11-10", source_diary_id="1863726",
                )
            team = registry.data["teams"][team_id]
            for field in ("assignmentHistory", "crewHistory"):
                self.assertEqual(len(team[field]), 1)
                self.assertEqual(team[field][0]["observedAt"], "2026-11-10T18:00:00-03:00")
                self.assertEqual(team[field][0]["sourceDiaryId"], "1863726")
            self.assertEqual(team["assignmentChanges"], [])

    def test_different_diaries_stay_separate_even_with_identical_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "registry.json")
            crew = [{"registration": "123456", "fullName": "FELIPE"}]
            for diary_id in ("1863726", "1863727", "1863726"):
                team_id = registry.enrich_professionals(
                    "E3550", crew, "2026-11-10T08:00:00-03:00", "10/11/2026 | 08:00 - 17:00",
                    operational_date="2026-11-10", source_diary_id=diary_id,
                )
            for field in ("assignmentHistory", "crewHistory"):
                self.assertEqual(len(registry.data["teams"][team_id][field]), 2)

    def test_unique_legacy_observation_adopts_diary_id(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "registry.json")
            crew = [{"registration": "123456", "fullName": "FELIPE"}]
            for diary_id in ("", "1863726"):
                team_id = registry.enrich_professionals(
                    "E3550", crew, "2026-11-10T08:00:00-03:00", "10/11/2026 | 08:00 - 17:00",
                    operational_date="2026-11-10", source_diary_id=diary_id,
                )
            for field in ("assignmentHistory", "crewHistory"):
                observations = registry.data["teams"][team_id][field]
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]["sourceDiaryId"], "1863726")

    def test_current_diary_url_is_used_instead_of_reused_details_url(self):
        with tempfile.TemporaryDirectory() as directory:
            row = {
                "Veículo": "E3550", "Eletricista 1 Registro": "123456", "Eletricista 1 Nome": "FELIPE",
                "Data Referência - Turno": "10/11/2026 | 08:00 - 17:00",
                "Diário URL": "https://www.copel.com/rtlweb/paginas/timeline?id=1863726",
                "Detalhes diário consultado": "https://www.copel.com/rtlweb/paginas/timeline?id=999",
            }
            enriquecer_cadastro_permanente_equipes(Path(directory), [row], "2026-11-10")
            registry = TeamRegistry(Path(directory) / "rotalog/equipes/team-registry.json")
            team = next(iter(registry.data["teams"].values()))
            self.assertEqual(team["assignmentHistory"][0]["sourceDiaryId"], "1863726")


if __name__ == "__main__":
    unittest.main()
