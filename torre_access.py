"""Credenciais curtas e exclusivas para leitura da API pública do Orange."""
import base64
import hashlib
import hmac
import json
import time


def _encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def issue_token(key, company, subject, now=None):
    if len(key.encode()) < 32:
        raise ValueError("TORRE_SIGNING_KEY deve ter pelo menos 32 bytes")
    now = int(time.time() if now is None else now)
    payload = _encode(json.dumps({"aud": "orange-torre-v1", "scope": "torre:read",
                                 "company": company, "sub": subject,
                                 "iat": now, "exp": now + 600}, separators=(",", ":")).encode())
    signature = _encode(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
    return payload + "." + signature


def validate_token(token, key, company, now=None):
    if len(key.encode()) < 32 or not token or len(token) > 4096:
        raise ValueError("Credencial inválida")
    now = int(time.time() if now is None else now)
    try:
        payload, signature = token.split(".")
        expected = _encode(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Assinatura inválida")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if (claims["aud"] != "orange-torre-v1" or claims["scope"] != "torre:read"
                or claims["company"] != company or not claims["sub"]
                or not isinstance(claims["iat"], int) or not isinstance(claims["exp"], int)
                or claims["iat"] > now + 30 or claims["exp"] <= now
                or claims["exp"] - claims["iat"] != 600):
            raise ValueError("Credencial inválida")
        return claims
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Credencial inválida") from exc
