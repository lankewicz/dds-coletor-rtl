import datetime
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coletor.historico import executar_coleta_historico_dia
from coletor.historico_cache import load_unique_day
from coletor.historico_scrapers import RotalogEquipesScraper


DAY = datetime.date(2026, 9, 14)


def unique_record():
    return {
        "Veículo": "E3C02",
        "Contrato": "4600026988",
        "Agência": "BSCCEL",
        "Data Referência - Turno": "14/09/2026 | 08:05 - 18:44",
        "Eletricista 1": "RENNA",
        "Eletricista 2": "GLEYDSO",
        "Diário URL": "https://www.copel.com/rtlweb/paginas/timeline?id=123",
        "Agência Nome completo": "AGÊNCIA TESTE",
        "Eletricista 1 Nome completo": "RENNA NOME COMPLETO",
        "Eletricista 1 Matrícula": "001234",
        "Eletricista 2 Nome completo": "GLEYDSO NOME COMPLETO",
        "Eletricista 2 Matrícula": "005678",
    }


def unique_document():
    event = {
        "Contrato": "4600026988",
        "Veículo": "E3C02",
        "Informado com limitador (km)": "10,00",
        "Autorizado final(km)": "8,00",
        "Protocolo": "123456789",
    }
    return {
        "completo": True,
        "schemaVersion": 2,
        "data": DAY.isoformat(),
        "resumo": [{"valor": "0", "texto": "Indicador"}] * 15,
        "turnos": [unique_record()],
        "eventos": [event],
        "paginacao": {
            "turnos": {"total": 1, "colunas": list(unique_record())},
            "eventos": {"total": 1, "colunas": list(event)},
        },
        "coletadoEm": "2026-09-15T01:00:00-03:00",
    }


def write_cache(root, document=None):
    folder = root / DAY.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("resumo", "turnos", "eventos"):
        (folder / f"{name}.csv").write_text("ok", encoding="utf-8")
    (folder / "dados.json").write_text(
        json.dumps(document or unique_document(), ensure_ascii=False),
        encoding="utf-8",
    )
    return folder


class HistoricalCacheTests(unittest.TestCase):
    def test_only_complete_unfiltered_current_schema_receipts_are_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_cache(root)
            self.assertEqual(
                unique_document(),
                load_unique_day(DAY, root, now=datetime.datetime(2026, 9, 15, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=-3)))),
            )

            filtered = unique_document()
            filtered["filtroVeiculo"] = "E3C02"
            write_cache(root, filtered)
            self.assertIsNone(load_unique_day(DAY, root, now=datetime.datetime(2026, 9, 15, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=-3)))))

    def test_incomplete_or_incompatible_cache_falls_back_to_portal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = write_cache(root)
            (folder / "eventos.csv").unlink()
            self.assertIsNone(load_unique_day(DAY, root, now=datetime.datetime(2026, 9, 15, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=-3)))))

            write_cache(root)
            document = unique_document()
            document["paginacao"]["eventos"]["colunas"].remove("Contrato")
            write_cache(root, document)
            self.assertIsNone(load_unique_day(DAY, root, now=datetime.datetime(2026, 9, 15, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=-3)))))

    def test_expired_cache_falls_back_to_preserve_historical_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_cache(root)
            now = datetime.datetime(
                2026, 9, 16, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=-3)),
            )
            self.assertIsNone(load_unique_day(DAY, root, now=now))

    def test_cached_professional_details_skip_duplicate_team_detail_requests(self):
        row = {
            "Veiculo": "E3C02",
            "Contrato": "4600026988",
            "Agencia": "BSCCEL",
            "Data Referencia - Turno": "14/09/2026 | 08:05 - 18:44",
        }
        scraper = RotalogEquipesScraper(session=Mock())
        with patch.object(scraper, "_fetch_team_details") as fetch_details:
            scraper._enrich_team_details([row], "view-state", cached_turnos=[unique_record()])

        fetch_details.assert_not_called()
        self.assertEqual("001234", row["Eletricista 1 Registro"])
        self.assertEqual("RENNA NOME COMPLETO", row["Eletricista 1 Nome"])
        self.assertEqual("AGÊNCIA TESTE", row["Agência Nome completo"])

    def test_ambiguous_team_details_fall_back_to_portal(self):
        row = {
            "Veiculo": "E3C02",
            "Contrato": "4600026988",
            "Agencia": "BSCCEL",
            "Data Referencia - Turno": "14/09/2026 | 08:05 - 18:44",
        }
        duplicate = unique_record()
        duplicate["Eletricista 1 Matrícula"] = "009999"
        scraper = RotalogEquipesScraper(session=Mock())
        with patch.object(
            scraper, "_fetch_team_details",
            return_value=({"Eletricista 1 Registro": "000001"}, "next-state"),
        ) as fetch_details:
            scraper._enrich_team_details(
                [row], "view-state", cached_turnos=[unique_record(), duplicate],
            )

        fetch_details.assert_called_once()
        self.assertEqual("000001", row["Eletricista 1 Registro"])

    def test_daily_collection_reuses_cached_events_and_marks_provenance(self):
        cache = unique_document()
        teams_scraper = Mock()
        teams_scraper.raspar_dia.return_value = []
        with tempfile.TemporaryDirectory() as directory, \
                patch("coletor.historico.load_unique_day", return_value=cache), \
                patch("coletor.historico.RotalogEventosScraper") as events_factory, \
                patch("coletor.historico.RotalogEquipesScraper", return_value=teams_scraper):
            result = executar_coleta_historico_dia(
                DAY, Path(directory), "Empresa", atualizar_terminal=False,
            )

            events_factory.assert_not_called()
            self.assertEqual("coleta_unica_cache", result["eventSource"])
            self.assertEqual(cache["turnos"], teams_scraper.raspar_dia.call_args.kwargs["cached_turnos"])
            with gzip.open(result["localArquivoEventosBrutos"], "rt", encoding="utf-8") as stream:
                raw = json.load(stream)
            self.assertEqual("coleta_unica_cache", raw["origemDados"])
            self.assertEqual(2, raw["coletaUnica"]["schemaVersion"])


if __name__ == "__main__":
    unittest.main()
