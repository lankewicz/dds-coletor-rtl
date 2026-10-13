"""API somente leitura. Publicar via Funnel; nunca expor a pasta do coletor."""
import argparse
import gzip
import hashlib
import json
import os
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from torre_access import validate_token


def create_handler(root, key, company, origins):
    root = Path(root).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Não registrar URLs, headers ou credenciais.
            return

        def headers_for(self, status, etag=None):
            self.send_response(status)
            self.send_header("Cache-Control", "private, no-cache" if status in (200, 304) else "no-store")
            self.send_header("Vary", "Origin, Authorization")
            self.send_header("X-Content-Type-Options", "nosniff")
            origin = self.headers.get("Origin")
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Expose-Headers", "ETag")
            if etag:
                self.send_header("ETag", etag)

        def error(self, status):
            self.headers_for(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "status": status}).encode())

        def do_OPTIONS(self):
            if (self.headers.get("Origin") not in origins
                    or urlsplit(self.path).path not in ("/api/rotalog/index", "/api/torre")
                    or self.headers.get("Access-Control-Request-Method") != "GET"):
                return self.error(403)
            requested = {v.strip().lower() for v in self.headers.get("Access-Control-Request-Headers", "").split(",") if v.strip()}
            if not requested.issubset({"authorization", "if-none-match"}):
                return self.error(403)
            self.headers_for(204)
            self.send_header("Access-Control-Allow-Methods", "GET")
            self.send_header("Access-Control-Allow-Headers", "Authorization, If-None-Match")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self):
            path = urlsplit(self.path).path
            files = {"/api/rotalog/index": root / "rotalog/equipes/current/index.json.gz",
                     "/api/torre": root / "torre/current.json"}
            if path not in files:
                return self.error(404)
            if self.headers.get("Origin") and self.headers["Origin"] not in origins:
                return self.error(403)
            auth = self.headers.get("Authorization", "")
            try:
                if not auth.startswith("Bearer "):
                    raise ValueError("Ausente")
                validate_token(auth[7:], key, company)
            except ValueError:
                return self.error(401)
            try:
                target = files[path]
                if target.suffix == ".gz":
                    with gzip.open(target, "rb") as stream:
                        payload = stream.read()
                else:
                    payload = target.read_bytes()
                data = json.loads(payload)
                if not isinstance(data, dict) or data.get("company") != company:
                    return self.error(503)
                if path == "/api/rotalog/index":
                    teams = data.get("equipes", data.get("snapshots"))
                    if not isinstance(teams, dict) or any(
                            not isinstance(k, str) or not k.strip() or not isinstance(v, dict)
                            for k, v in teams.items()):
                        return self.error(503)
                if path == "/api/torre":
                    from torre_contract import validate_projection
                    validate_projection(data, company)
            except (OSError, EOFError, zlib.error, ValueError, AttributeError, TypeError):
                return self.error(503)
            etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
            # A autorização é verificada mesmo para respostas 304.
            if self.headers.get("If-None-Match") == etag:
                self.headers_for(304, etag)
                self.end_headers()
                return
            self.headers_for(200, etag)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    key = os.environ.get("TORRE_SIGNING_KEY", "")
    company = os.environ.get("DDS_EMPRESA_PADRAO", "")
    origins = set(filter(None, (v.strip() for v in os.environ.get("TORRE_ALLOWED_ORIGINS", "").split(","))))
    if len(key.encode()) < 32 or not company or not origins or any(not v.startswith("https://") for v in origins):
        raise SystemExit("Configurar chave forte, empresa e origens HTTPS antes de iniciar")
    ThreadingHTTPServer(("127.0.0.1", args.port), create_handler(args.output_dir, key, company, origins)).serve_forever()
