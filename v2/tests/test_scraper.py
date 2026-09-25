import unittest

from rotalog_v2.scraper import extract_timeline, parse_team_group


class ScraperTests(unittest.TestCase):
    def test_extracts_events_and_groups_them_by_team(self):
        source = '''
        <script>
        var timelineAlert = [
          {"start": 1790236800000, "end": null, "editable": false, "group": "E3733-CA127 BRUNNO PEDRO (online)", "className": "turno", "content": "T"},
          {"start": 1790240400000, "end": 1790244000000, "editable": false, "group": "E3733-CA127 BRUNNO PEDRO (online)", "className": "execucao", "content": "50954710"}
        ];
        </script>
        '''
        result = extract_timeline(source)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(list(result["teams"]), ["E3733"])
        self.assertEqual(result["teams"]["E3733"]["eventIndexes"], [0, 1])
        self.assertTrue(result["teams"]["E3733"]["online"])

    def test_understands_new_date_syntax(self):
        source = '''{"start":new Date(1790236800000),"end":null,"editable":true,"group":"E3K95-ABC Nome","className":"x","content":"T"}'''
        result = extract_timeline(source)
        self.assertEqual(result["events"][0]["startMs"], 1790236800000)
        self.assertEqual(result["teams"]["E3K95"]["vehicleId"], "E3K95")
        self.assertIsNone(result["teams"]["E3K95"]["regionalCode"])

    def test_rejects_html_without_recognized_events(self):
        with self.assertRaises(ValueError):
            extract_timeline("<html>sem timeline</html>")

    def test_parse_team_without_vehicle(self):
        self.assertEqual(parse_team_group("E3188 EDIVAN (50 min)")["team"], "E3188")

    def test_vehicle_placeholder_uses_ca_code_as_team_key(self):
        parsed = parse_team_group("veiculo?-CA078 CLAYTON JOSIAS (online)")
        self.assertEqual(parsed["team"], "CA078")
        self.assertEqual(parsed["vehicle"], "")
        self.assertIsNone(parsed["vehicleId"])
        self.assertEqual(parsed["regionalCode"], "CA078")
        self.assertEqual(parsed["identitySource"], "REGIONAL_CODE_FALLBACK")
        self.assertEqual(parsed["collaborator"], "CLAYTON JOSIAS")

    def test_stable_vehicle_id_is_separate_from_regional_code(self):
        parsed = parse_team_group("E4A20-CA263 DANIEL WELINGTON (online)")
        self.assertEqual(parsed["team"], "E4A20")
        self.assertEqual(parsed["vehicleId"], "E4A20")
        self.assertEqual(parsed["regionalCode"], "CA263")
        self.assertEqual(parsed["identitySource"], "VEHICLE_ID")

    def test_placeholder_can_still_contain_stable_vehicle_id(self):
        parsed = parse_team_group("veiculo?-E3G67 ARDEL TAIS (online)")
        self.assertEqual(parsed["team"], "E3G67")
        self.assertEqual(parsed["vehicleId"], "E3G67")
        self.assertIsNone(parsed["regionalCode"])
        self.assertEqual(parsed["identitySource"], "VEHICLE_ID_AFTER_PLACEHOLDER")


if __name__ == "__main__":
    unittest.main()
