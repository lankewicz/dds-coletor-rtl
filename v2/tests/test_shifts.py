import unittest

from rotalog_v2.shifts import enrich_shift_state


class ShiftTests(unittest.TestCase):
    def test_initial_snapshot_establishes_open_baseline_without_transition(self):
        current = {"teams": {"E3733": {
            "state": "AVAILABLE",
            "shiftMarkers": [{"at": "2026-09-24T07:30:00-03:00", "timestampMs": 1}],
        }}}
        enrich_shift_state({}, current)
        shift = current["teams"]["E3733"]["shift"]
        self.assertEqual(shift["status"], "OPEN")
        self.assertEqual(shift["newTransitions"], [])

    def test_new_marker_closes_then_next_marker_opens_shift(self):
        previous = {"teams": {"E3733": {
            "shiftMarkers": [{"at": "2026-09-24T07:30:00-03:00", "timestampMs": 1}],
            "shift": {"status": "OPEN", "openedAt": "2026-09-24T07:30:00-03:00", "transitions": []},
        }}}
        current = {"teams": {"E3733": {
            "state": "OFFLINE_WITH_ACTIVITY",
            "shiftMarkers": [
                {"at": "2026-09-24T07:30:00-03:00", "timestampMs": 1},
                {"at": "2026-09-24T12:00:00-03:00", "timestampMs": 2},
            ],
        }}}
        enrich_shift_state(previous, current)
        shift = current["teams"]["E3733"]["shift"]
        self.assertEqual(shift["status"], "CLOSED")
        self.assertEqual(shift["newTransitions"][0]["type"], "CLOSE")

        later = {"teams": {"E3733": {
            "state": "AVAILABLE",
            "shiftMarkers": current["teams"]["E3733"]["shiftMarkers"] + [
                {"at": "2026-09-24T14:00:00-03:00", "timestampMs": 3},
            ],
        }}}
        enrich_shift_state(current, later)
        self.assertEqual(later["teams"]["E3733"]["shift"]["status"], "OPEN")
        self.assertEqual(later["teams"]["E3733"]["shift"]["newTransitions"][0]["type"], "OPEN")


if __name__ == "__main__":
    unittest.main()
