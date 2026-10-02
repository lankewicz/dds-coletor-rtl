import os
import unittest
from unittest.mock import Mock, patch

from coletor.client import CrawlerRotalog, rotalog_tls_verify


class ClientTlsTests(unittest.TestCase):
    def test_tls_e_validado_por_padrao(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(True, rotalog_tls_verify())

    def test_aceita_bundle_de_ca_corporativa(self):
        with patch.dict(os.environ, {"ROTALOG_CA_BUNDLE": "/etc/ssl/rotalog.pem"}, clear=True):
            self.assertEqual("/etc/ssl/rotalog.pem", rotalog_tls_verify())

    def test_sessao_aplica_tls_e_valida_respostas_http(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.post.return_value.status_code = 200
        session.post.return_value.text = "dashboard"
        with patch("coletor.client.requests.Session", return_value=session), patch.dict(
            os.environ, {"ROTALOG_VERIFY_TLS": "true"}, clear=True,
        ):
            result = CrawlerRotalog("usuario", "senha").criar_sessao_autenticada()

        self.assertIs(result, session)
        self.assertIs(True, session.verify)
        session.get.return_value.raise_for_status.assert_called_once_with()
        session.post.return_value.raise_for_status.assert_called_once_with()

    def test_tls_inseguro_exige_configuracao_explicita(self):
        with patch.dict(os.environ, {"ROTALOG_VERIFY_TLS": "false"}, clear=True):
            self.assertIs(False, rotalog_tls_verify())


if __name__ == "__main__":
    unittest.main()
