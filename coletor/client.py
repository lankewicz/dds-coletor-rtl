"""Cliente HTTP para autenticação e sessão no ROTALOG Copel."""

from __future__ import annotations

import logging
import os
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

URL_BASE = "https://www.copel.com/rtlweb"
URL_DASHBOARD = f"{URL_BASE}/paginas/dashboard"
URL_LOGIN_ACTION = f"{URL_BASE}/paginas/j_security_check"
URL_TEMPO_REAL = f"{URL_BASE}/paginas/tempoReal"


class CrawlerRotalog:
    def __init__(self, usuario: str | None = None, senha: str | None = None):
        self.usuario = usuario or os.getenv("ROTALOG_USUARIO", "").strip()
        self.senha = senha or os.getenv("ROTALOG_SENHA", "").strip()

        if not self.usuario or not self.senha:
            raise RuntimeError(
                "Credenciais do ROTALOG não configuradas. "
                "Defina ROTALOG_USUARIO e ROTALOG_SENHA no arquivo .env."
            )

    def criar_sessao_autenticada(self) -> requests.Session:
        """Cria e autentica uma sessão HTTP no portal Copel RTLWeb."""
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
        })

        # 1. Carrega o dashboard para obter os cookies iniciais (JSESSIONID)
        session.get(URL_DASHBOARD, verify=False, timeout=30)

        # 2. Realiza o POST de autenticação
        payload = {"j_username": self.usuario, "j_password": self.senha}
        resp = session.post(URL_LOGIN_ACTION, data=payload, verify=False, timeout=30)
        if resp.status_code != 200 or "j_security_check" in resp.text:
            raise PermissionError("Falha na autenticação do portal Copel RTLWeb. Verifique credenciais.")

        logger.debug("Sessão autenticada no portal ROTALOG Copel com sucesso.")
        return session
