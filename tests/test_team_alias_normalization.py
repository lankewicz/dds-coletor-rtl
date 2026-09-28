import unittest

from coletor.equipes import (
    build_identifier_map,
    canonicalize_team_snapshots,
    member_signature,
    normalize_team_key,
    valid_team_key,
)
from coletor.parser import consolidar_equipes_duplicadas, parse_group_string, resolver_equipe_group
from coletor.storage import build_rotalog_document


class TeamAliasNormalizationTests(unittest.TestCase):
    def test_autotrack_suffixes_use_base_team(self):
        for raw in ("E3T01", "E3T01(2)", "e3t01 (3)", " E3T01(27) "):
            self.assertEqual(normalize_team_key(raw), "E3T01")
            self.assertTrue(valid_team_key(raw))

    def test_group_parser_does_not_treat_numeric_suffix_as_connection_status(self):
        parsed = parse_group_string("E3T01(2)-CA123 JOAO (online)")
        resolved = resolver_equipe_group(parsed)
        self.assertEqual(resolved["equipe_codigo"], "E3T01")
        self.assertEqual(resolved["status_conexao"], "online")
        self.assertTrue(resolved["is_online"])

    def test_duplicate_devices_are_consolidated(self):
        teams = consolidar_equipes_duplicadas([
            {"equipe_codigo": "E3T01", "group_raw": "tablet", "ss_executadas": [{"protocolo": "123"}]},
            {"equipe_codigo": "E3T01(2)", "group_raw": "autotrack", "ss_executadas": [{"protocolo": "456"}]},
        ])
        self.assertEqual(len(teams), 1)
        self.assertEqual(teams[0]["equipe_codigo"], "E3T01")
        self.assertEqual(len(teams[0]["ss_executadas"]), 2)

    def test_documents_and_old_snapshots_use_canonical_identity(self):
        document = build_rotalog_document({"equipe_codigo": "E3T01(2)"}, "Empresa", "E3T01", "agora", {})
        self.assertEqual(document["equipe"], "E3T01")
        migrated = canonicalize_team_snapshots({"E3T01(2)": {"teamKey": "E3T01(2)", "updatedAt": "1"}})
        self.assertEqual(set(migrated), {"E3T01"})
        self.assertEqual(migrated["E3T01"]["teamKey"], "E3T01")

    def test_member_signature_is_order_independent(self):
        self.assertEqual(member_signature("FELIPE ELIAS"), "ELIAS|FELIPE")
        self.assertEqual(member_signature("Elias Felipe"), "ELIAS|FELIPE")

    def test_transient_vehicle_is_resolved_by_exact_member_pair(self):
        aliases = build_identifier_map({
            "E3P29": {
                "teamKey": "E3P29",
                "conexao": {"veiculo": "MA968", "colaborador": "FELIPE ELIAS"},
            }
        })
        resolved = resolver_equipe_group(parse_group_string("veiculo?-MA968 ELIAS FELIPE (online)"), aliases)
        self.assertEqual(resolved["equipe_codigo"], "E3P29")
        self.assertEqual(resolved["origem_resolucao"], "IDENTIFICACAO_TABLET")

    def test_exact_pair_resolves_even_when_vehicle_alias_is_missing(self):
        aliases = build_identifier_map({
            "E3P29": {"teamKey": "E3P29", "conexao": {"colaborador": "FELIPE ELIAS"}}
        })
        resolved = resolver_equipe_group(parse_group_string("veiculo?-XX999 ELIAS FELIPE (online)"), aliases)
        self.assertEqual(resolved["equipe_codigo"], "E3P29")
        self.assertEqual(resolved["origem_resolucao"], "INTEGRANTES_EXATOS")


if __name__ == "__main__":
    unittest.main()
