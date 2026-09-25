import unittest
from pathlib import Path
import tempfile

from rotalog_v2.ajax_enrichment import AjaxProtocolEnricher, extract_view_state


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.payloads = []

    def post_realtime_ajax(self, payload):
        self.payloads.append(payload)
        return self.response


class AjaxEnrichmentTests(unittest.TestCase):
    def test_extracts_view_state_in_both_attribute_orders(self):
        self.assertEqual(extract_view_state('<input name="javax.faces.ViewState" value="abc:1">'), "abc:1")
        self.assertEqual(extract_view_state('<input value="xyz" name="javax.faces.ViewState">'), "xyz")

    def test_fetches_and_applies_missing_protocol(self):
        response = r'''voarParaCoordenadaZoom([-25.123, -49.456], 15, 'Equipe: E3733<BR />Protocolo: 50954710<BR />Tipo: CHAVE<BR />Categoria: EMERGENCIA<BR />Inicio Deslocamento: 08:00<BR />Inicio Execucao: 08:02')'''
        client = FakeClient(response)
        service = {
            "eventIndex": 7,
            "protocol": None,
            "rawContent": "CHAVE",
            "status": "IN_PROGRESS",
            "startMs": 1790247600000,
            "endMs": None,
            "source": "TIMELINE",
        }
        snapshot = {"teams": {"E3733": {"currentActivity": service, "completedServices": []}}}
        stats = AjaxProtocolEnricher(client, "America/Sao_Paulo").enrich(
            snapshot,
            '<input name="javax.faces.ViewState" value="state-1">',
        )
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(service["protocol"], "50954710")
        self.assertEqual(service["source"], "AJAX_POPUP")
        self.assertEqual(service["categoryDetail"], "EMERGENCIA")
        self.assertEqual(client.payloads[0]["form:cm-patientregistry-facesheet-timeline_eventIdx"], "7")

    def test_does_not_call_ajax_when_protocol_already_exists(self):
        client = FakeClient("")
        snapshot = {"teams": {"E3733": {
            "currentActivity": {"protocol": "50954710"},
            "completedServices": [],
        }}}
        stats = AjaxProtocolEnricher(client, "America/Sao_Paulo").enrich(snapshot, "")
        self.assertEqual(stats["candidates"], 0)
        self.assertEqual(client.payloads, [])

    def test_ignores_invalid_team_placeholder(self):
        client = FakeClient("")
        snapshot = {"teams": {"VEICULO?": {
            "currentActivity": {
                "eventIndex": 1,
                "protocol": None,
                "status": "IN_PROGRESS",
            },
            "completedServices": [],
        }}}
        stats = AjaxProtocolEnricher(client, "America/Sao_Paulo").enrich(snapshot, "")
        self.assertEqual(stats["candidates"], 0)
        self.assertEqual(client.payloads, [])

    def test_persistent_cache_survives_new_process_instance(self):
        response = r'''voarParaCoordenadaZoom([-25.123, -49.456], 15, 'Equipe: E3733<BR />Protocolo: 50954710')'''
        service = {
            "eventIndex": 7, "protocol": None, "rawContent": "CHAVE",
            "status": "IN_PROGRESS", "startMs": 1790247600000, "endMs": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "ajax.json.gz"
            first_client = FakeClient(response)
            first = AjaxProtocolEnricher(first_client, "America/Sao_Paulo", cache_path=cache_path)
            first.enrich({"teams": {"E3733": {"currentActivity": service, "completedServices": []}}}, '<input name="javax.faces.ViewState" value="x">')
            self.assertEqual(len(first_client.payloads), 1)

            repeated_service = {**service, "protocol": None, "source": "TIMELINE"}
            second_client = FakeClient("")
            second = AjaxProtocolEnricher(second_client, "America/Sao_Paulo", cache_path=cache_path)
            stats = second.enrich({"teams": {"E3733": {"currentActivity": repeated_service, "completedServices": []}}}, "")
            self.assertEqual(stats["cached"], 1)
            self.assertEqual(stats["resolved"], 0)
            self.assertEqual(repeated_service["protocol"], "50954710")
            self.assertEqual(second_client.payloads, [])


if __name__ == "__main__":
    unittest.main()
