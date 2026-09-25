import unittest

from rotalog_v2.changes import detect_changes


def service(protocol, status, **values):
    return {
        "protocol": protocol,
        "status": status,
        "startMs": values.pop("startMs", 1),
        "rawContent": values.pop("rawContent", "UC"),
        **values,
    }


def snapshot(team):
    return {"teams": {"E3X99": team}}


class ChangeTests(unittest.TestCase):
    def test_reports_service_transition_with_times(self):
        previous = snapshot({
            "online": True, "state": "IN_TRANSIT", "regionalCode": "CA123",
            "completedServices": [],
            "currentActivity": service("999997", "IN_TRANSIT", dispatchStart="10:48"),
        })
        current = snapshot({
            "online": True, "state": "IN_PROGRESS", "regionalCode": "CA123",
            "completedServices": [],
            "currentActivity": service("999997", "IN_PROGRESS", executionStart="11:17"),
        })
        changes = detect_changes(previous, current)["E3X99"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["message"], "999997: DESLOCAMENTO 10:48 -> EXECUÇÃO 11:17")

    def test_reports_completion_and_new_service(self):
        previous = snapshot({
            "online": True, "state": "IN_PROGRESS", "completedServices": [],
            "currentActivity": service("999999", "IN_PROGRESS", executionStart="10:42"),
        })
        current = snapshot({
            "online": True, "state": "IN_TRANSIT",
            "completedServices": [service("999999", "COMPLETED", returnTime="11:18")],
            "currentActivity": service("999998", "IN_TRANSIT", dispatchStart="11:19", startMs=2),
        })
        messages = [item["message"] for item in detect_changes(previous, current)["E3X99"]]
        self.assertIn("999999: EXECUÇÃO 10:42 -> CONCLUSÃO 11:18", messages)
        self.assertIn("999998: NOVO -> DESLOCAMENTO 11:19", messages)

    def test_ignores_same_data_and_first_snapshot(self):
        team = {
            "online": True, "state": "IN_PROGRESS", "completedServices": [],
            "currentActivity": service("999999", "IN_PROGRESS", executionStart="10:42"),
        }
        self.assertEqual(detect_changes(snapshot(team), snapshot(team)), {})
        self.assertEqual(detect_changes({}, snapshot(team)), {})


if __name__ == "__main__":
    unittest.main()
