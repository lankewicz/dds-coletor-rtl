import copy
import datetime
import unittest
from zoneinfo import ZoneInfo

from coletor.shift_policy import apply_shift_limit, reopen_returning_team

TZ = ZoneInfo("America/Sao_Paulo")


class ShiftLimitTests(unittest.TestCase):
    def setUp(self):
        self.document = {
            "jornada": {"turno": {"status": "ABERTO", "inicio": "2026-11-12T07:00:00-03:00"},
                        "turnos": [{"status": "ABERTO", "inicio": "2026-11-12T07:00:00-03:00"}],
                        "emIntervalo": True},
            "ordensServico": {"atual": {"serviceId": "s1", "statusAtual": "EXECUCAO"},
                              "historico": [{"serviceId": "s0", "statusAtual": "CONCLUSAO"}]},
        }

    def test_strict_limit_and_active_service_preserved_without_completion(self):
        boundary = datetime.datetime(2026, 11, 13, 1, tzinfo=TZ)
        self.assertIs(apply_shift_limit(self.document, boundary), self.document)
        original = copy.deepcopy(self.document)
        now = boundary + datetime.timedelta(seconds=1)
        closed = apply_shift_limit(self.document, now)
        shift = closed["jornada"]["turno"]
        self.assertEqual(shift["status"], "FECHADO")
        self.assertEqual(shift["fim"], boundary.isoformat())
        self.assertTrue(shift["fimEstimado"])
        self.assertIsNone(closed["ordensServico"]["atual"])
        self.assertEqual(closed["ordensServico"]["historico"][-1]["statusAtual"], "EXECUCAO")
        self.assertEqual(closed["jornada"]["turnos"][0]["status"], "FECHADO")
        self.assertEqual(self.document, original)
        self.assertIs(apply_shift_limit(closed, now), closed)

    def expired(self, absent=True):
        now = datetime.datetime(2026, 11, 23, 10, tzinfo=TZ)
        return apply_shift_limit(self.document, now, absent=absent), now

    def test_reappearance_without_new_t_preserves_old_closed_shift(self):
        closed, now = self.expired()
        team = {"turno": {"inicio_iso": "2026-11-12T07:00:00-03:00", "aberto": True}}
        self.assertFalse(reopen_returning_team(team, closed, now))
        self.assertFalse(team["turno"]["aberto"])
        self.assertEqual(team["turno"]["inicio_iso"], closed["jornada"]["turno"]["inicio"])

    def test_new_service_without_t_does_not_replace_old_opening(self):
        closed, now = self.expired()
        team = {"turno": {"inicio_iso": "2026-11-23T09:00:00-03:00", "aberto": True}}
        self.assertFalse(reopen_returning_team(team, closed, now))
        self.assertFalse(team["turno"]["aberto"])

    def test_new_opening_t_reopens_with_actual_time_even_without_absence(self):
        for absent in (True, False):
            closed, now = self.expired(absent)
            start = datetime.datetime(2026, 11, 23, 9, tzinfo=TZ)
            team = {"turno": {"inicio_iso": start.isoformat(), "aberto": True},
                    "turno_marcadores_t": [{"start": int(start.timestamp() * 1000)}]}
            self.assertTrue(reopen_returning_team(team, closed, now))
            self.assertTrue(team["turno"]["aberto"])
            self.assertEqual(team["turno"]["inicio_iso"], start.isoformat())
            self.assertNotIn("inicioEstimado", team["turno"])

    def test_old_future_and_closing_t_do_not_reopen(self):
        closed, now = self.expired()
        cases = [("2026-11-12T07:00:00-03:00", True),
                 ("2026-11-24T07:00:00-03:00", True),
                 ("2026-11-23T09:00:00-03:00", False)]
        for value, opened in cases:
            start = datetime.datetime.fromisoformat(value)
            team = {"turno": {"inicio_iso": value, "aberto": opened},
                    "turno_marcadores_t": [{"start": int(start.timestamp() * 1000)}]}
            self.assertFalse(reopen_returning_team(team, closed, now))
            self.assertFalse(team["turno"]["aberto"])

    def test_new_t_replaces_old_start_still_reported_in_timeline(self):
        closed, now = self.expired()
        start = datetime.datetime(2026, 11, 23, 9, tzinfo=TZ)
        old_start = datetime.datetime.fromisoformat(closed["jornada"]["turno"]["inicio"])
        team = {"turno": {"inicio_iso": old_start.isoformat(), "aberto": True},
                "turno_marcadores_t": [{"start": int(old_start.timestamp() * 1000)},
                                        {"start": int(start.timestamp() * 1000)}]}
        self.assertTrue(reopen_returning_team(team, closed, now))
        self.assertEqual(team["turno"]["inicio_iso"], start.isoformat())

    def test_active_service_cannot_reopen_during_merge(self):
        from coletor.shift_policy import preserve_closed_shift
        from coletor.storage import build_rotalog_document, merge_daily_document
        closed, now = self.expired()
        team = {"turno": {"inicio_iso": "2026-11-23T09:00:00-03:00", "aberto": True},
                "atividade_atual": {"protocolo": "12345678", "status": "EXECUCAO",
                                    "inicioIso": "2026-11-23T09:00:00-03:00"}}
        self.assertFalse(reopen_returning_team(team, closed, now))
        raw = build_rotalog_document(team, "teste", "E3733", now.isoformat(), {})
        merged = merge_daily_document(None, raw, "2026-11-12")
        result = preserve_closed_shift(merged, closed)
        self.assertEqual(result["jornada"]["turno"], closed["jornada"]["turno"])
        self.assertIsNone(result["ordensServico"]["atual"])
        self.assertTrue(any(s.get("protocolo") == "12345678"
                            for s in result["ordensServico"]["historico"]))

    def test_missing_or_invalid_start_is_not_invented(self):
        now = datetime.datetime(2026, 11, 23, 10, tzinfo=TZ)
        for value in (None, "bad", "2026-11-12T07:00:00"):
            self.document["jornada"]["turno"]["inicio"] = value
            self.assertIs(apply_shift_limit(self.document, now), self.document)
