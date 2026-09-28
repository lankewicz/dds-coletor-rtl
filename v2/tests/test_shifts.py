import unittest

from rotalog_v2.shifts import enrich_shift_state


def marker(at, timestamp):
    return {"at": at, "timestampMs": timestamp}


def service(start_at, start_ms, **extra):
    return {"startAt": start_at, "startMs": start_ms, "status": "COMPLETED", **extra}


class ShiftTests(unittest.TestCase):
    def test_last_opening_signal_is_used(self):
        current = {"teams": {"E3733": {
            "state": "IN_TRANSIT",
            "shiftMarkers": [
                marker("2026-09-24T07:28:00-03:00", 1000),
                marker("2026-09-24T07:30:00-03:00", 2000),
            ],
            "completedServices": [],
            "currentActivity": service("2026-09-24T07:40:00-03:00", 3000),
        }}}
        enrich_shift_state({}, current)
        shift = current["teams"]["E3733"]["shift"]
        self.assertEqual(shift["status"], "ABERTO")
        self.assertEqual(shift["openedAt"], "2026-09-24T07:30:00-03:00")
        self.assertEqual(shift["fonteAbertura"], "MARCADOR_T")
        self.assertEqual(shift["newTransitions"], [])

    def test_service_opens_shift_when_marker_is_missing(self):
        current = {"teams": {"E3733": {
            "state": "IN_TRANSIT", "shiftMarkers": [], "completedServices": [],
            "currentActivity": service("2026-09-24T08:49:00-03:00", 1000),
        }}}
        enrich_shift_state({}, current)
        shift = current["teams"]["E3733"]["shift"]
        self.assertEqual(shift["openedAt"], "2026-09-24T08:49:00-03:00")
        self.assertEqual(shift["fonteAbertura"], "INICIO_DESLOCAMENTO")

    def test_repeated_signal_refreshes_but_does_not_close_open_shift(self):
        previous = {"teams": {"E3733": {
            "shiftMarkers": [marker("2026-09-24T08:00:00-03:00", 1000)],
            "shift": {"status": "ABERTO", "openedAt": "2026-09-24T08:00:00-03:00", "fonteAbertura": "MARCADOR_T"},
        }}}
        current = {"teams": {"E3733": {
            "state": "IN_PROGRESS",
            "shiftMarkers": [
                marker("2026-09-24T08:00:00-03:00", 1000),
                marker("2026-09-24T14:49:00-03:00", 2000),
                marker("2026-09-24T14:54:00-03:00", 3000),
            ],
            "completedServices": [],
            "currentActivity": service("2026-09-24T15:00:00-03:00", 4000),
        }}}
        enrich_shift_state(previous, current)
        shift = current["teams"]["E3733"]["shift"]
        self.assertEqual(shift["status"], "ABERTO")
        self.assertIsNone(shift["fechamentoPendente"])
        self.assertEqual(len(shift["atualizacoesTurno"]), 2)

    def test_return_closes_only_after_candidate_is_confirmed(self):
        previous = {"teams": {"E3733": {
            "shiftMarkers": [],
            "shift": {"status": "ABERTO", "openedAt": "2026-09-24T08:00:00-03:00"},
        }}}
        team = {
            "state": "OFFLINE_WITH_ACTIVITY", "shiftMarkers": [], "currentActivity": None,
            "completedServices": [service("2026-09-24T10:00:00-03:00", 1000, returnTime="11:15")],
        }
        first = {"teams": {"E3733": dict(team)}}
        enrich_shift_state(previous, first)
        self.assertEqual(first["teams"]["E3733"]["shift"]["status"], "ABERTO")
        self.assertEqual(first["teams"]["E3733"]["shift"]["fechamentoPendente"]["fimProposto"], "2026-09-24T11:15:00-03:00")

        second = {"teams": {"E3733": dict(team)}}
        enrich_shift_state(first, second)
        shift = second["teams"]["E3733"]["shift"]
        self.assertEqual(shift["status"], "FECHADO")
        self.assertEqual(shift["closedAt"], "2026-09-24T11:15:00-03:00")
        self.assertEqual(shift["newTransitions"][0]["type"], "FECHAMENTO")

    def test_turn_can_close_on_following_day(self):
        previous = {"teams": {"E3733": {
            "shiftMarkers": [],
            "shift": {
                "status": "ABERTO", "openedAt": "2026-08-05T08:49:00-03:00",
                "fechamentoPendente": {
                    "fimProposto": "2026-08-06T03:23:00-03:00", "origem": "RETORNO_SERVICO"
                },
            },
        }}}
        current = {"teams": {"E3733": {
            "state": "OFFLINE_WITH_ACTIVITY", "shiftMarkers": [], "currentActivity": None,
            "completedServices": [service("2026-08-05T20:11:00-03:00", 1000, returnTime="03:23")],
        }}}
        enrich_shift_state(previous, current)
        shift = current["teams"]["E3733"]["shift"]
        self.assertEqual(shift["status"], "FECHADO")
        self.assertEqual(shift["openedAt"], "2026-08-05T08:49:00-03:00")
        self.assertEqual(shift["closedAt"], "2026-08-06T03:23:00-03:00")


if __name__ == "__main__":
    unittest.main()
