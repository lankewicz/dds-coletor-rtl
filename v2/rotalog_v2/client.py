"""Cliente HTTP mínimo para o portal ROTALOG."""

from __future__ import annotations

from dataclasses import dataclass
import os

import requests
import urllib3


URL_BASE = "https://www.copel.com/rtlweb"
URL_DASHBOARD = f"{URL_BASE}/paginas/dashboard"
URL_LOGIN = f"{URL_BASE}/paginas/j_security_check"
URL_TEMPO_REAL = f"{URL_BASE}/paginas/tempoReal"


@dataclass(frozen=True)
class RotalogCredentials:
    username: str
    password: str

    @classmethod
    def from_environment(cls) -> "RotalogCredentials":
        username = os.getenv("ROTALOG_USUARIO", "").strip()
        password = os.getenv("ROTALOG_SENHA", "").strip()
        if not username or not password:
            raise RuntimeError("Defina ROTALOG_USUARIO e ROTALOG_SENHA.")
        return cls(username=username, password=password)


class RotalogClient:
    def __init__(
        self,
        credentials: RotalogCredentials,
        *,
        verify_tls: bool = False,
        timeout_seconds: float = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.credentials = credentials
        self.verify_tls = verify_tls
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
        })
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def authenticate(self) -> None:
        self.session.get(
            URL_DASHBOARD,
            verify=self.verify_tls,
            timeout=self.timeout_seconds,
        ).raise_for_status()
        response = self.session.post(
            URL_LOGIN,
            data={
                "j_username": self.credentials.username,
                "j_password": self.credentials.password,
            },
            verify=self.verify_tls,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        if "j_security_check" in response.text:
            raise PermissionError("O ROTALOG recusou as credenciais.")

    def fetch_realtime_html(self) -> str:
        self.authenticate()
        response = self.session.get(
            URL_TEMPO_REAL,
            verify=self.verify_tls,
            timeout=max(60, self.timeout_seconds),
        )
        response.raise_for_status()
        if "j_security_check" in response.text:
            raise PermissionError("A sessão retornou à tela de login.")
        if "timelineAlert" not in response.text and 'PrimeFaces.cw("Timeline"' not in response.text:
            raise RuntimeError("Resposta sem timeline reconhecível; coleta descartada.")
        return response.text

    def post_realtime_ajax(self, payload: dict[str, str]) -> str:
        response = self.session.post(
            URL_TEMPO_REAL,
            data=payload,
            headers={
                "Faces-Request": "partial/ajax",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            verify=self.verify_tls,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.text
