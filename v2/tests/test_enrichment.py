import unittest

from rotalog_v2.enrichment import enrich_realtime, normalize_category, normalize_protocol
from rotalog_v2.scraper import extract_timeline


class EnrichmentTests(unittest.TestCase):
    def _enrich(self, events):
        source = "<script>var timelineAlert=[" + ",".join(events) + "];</script>"
        return enrich_realtime(extract_timeline(source), "America/Sao_Paulo")

    def test_enriches_realtime_team_state_and_counts(self):
        events = [
            '{"start":1790236800000,"end":null,"editable":false,"group":"E3733-CA127 NOME (online)","className":"tempoRealMacro","content":"T"}',
            '{"start":1790240400000,"end":1790244000000,"editable":false,"group":"E3733-CA127 NOME (online)","className":"tempoRealExecutadoEmergencia","content":"50954710"}',
            '{"start":1790247600000,"end":null,"editable":false,"group":"E3733-CA127 NOME (online)","className":"tempoRealEmDeslocamentoComercial","content":"20265259525570.4.2"}',
            '{"start":1790248000000,"end":null,"editable":false,"group":"E3733-CA127 NOME (online)","className":"tempoRealPendenteEmergencia","content":"3"}',
        ]
        result = self._enrich(events)
        team = result["teams"]["E3733"]
        self.assertEqual(team["state"], "IN_TRANSIT")
        self.assertEqual(team["currentActivity"]["protocol"], "20265259525570")
        self.assertEqual(team["completedServices"][0]["protocol"], "50954710")
        self.assertEqual(team["counts"]["pendingEmergency"], 1)

    def test_active_break_has_priority_in_team_state(self):
        events = [
            '{"start":1790247600000,"end":null,"editable":false,"group":"E3K95-ABC NOME (online)","className":"tempoRealEmExecucaoEmergencia","content":"50954710"}',
            '{"start":1790248000000,"end":null,"editable":false,"group":"E3K95-ABC NOME (online)","className":"tempoRealIntervalo","content":"INTERVALO"}',
        ]
        team = self._enrich(events)["teams"]["E3K95"]
        self.assertEqual(team["state"], "BREAK")
        self.assertTrue(team["breaks"][0]["active"])

    def test_unknown_event_is_preserved_for_analysis(self):
        events = [
            '{"start":1790247600000,"end":null,"editable":false,"group":"E3K95 NOME","className":"classeNova","content":"NOVO"}',
        ]
        team = self._enrich(events)["teams"]["E3K95"]
        self.assertEqual(team["counts"]["unclassified"], 1)
        self.assertEqual(team["unclassifiedEvents"], [0])

    def test_protocol_is_null_when_not_supported_by_source(self):
        self.assertIsNone(normalize_protocol("UC"))
        self.assertEqual(normalize_protocol("20265259525570.4.2"), "20265259525570")

    def test_category_variants_are_deduplicated(self):
        self.assertEqual(normalize_category("emergencia"), "EMERGENCIA")
        self.assertEqual(normalize_category("Emergência"), "EMERGENCIA")
        self.assertEqual(normalize_category("EMERGÊNCIA"), "EMERGENCIA")
        self.assertEqual(normalize_category("Comercial"), "COMERCIAL")


if __name__ == "__main__":
    unittest.main()
