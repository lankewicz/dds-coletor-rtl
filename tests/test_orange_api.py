import gzip
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from torre_access import issue_token, validate_token

spec = importlib.util.spec_from_file_location("torre_api", ROOT / "torre_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
KEY = "k" * 40
ORIGIN = "https://chicoeletro.web.app"


class OrangeApiTests(unittest.TestCase):
    def test_token_rejects_tampering_expiry_and_wrong_company(self):
        token = issue_token(KEY, "ChicoEletro", "user", now=1000)
        self.assertEqual(validate_token(token, KEY, "ChicoEletro", now=1001)["sub"], "user")
        for candidate, company, now in [(token + "x", "ChicoEletro", 1001),
                                         (token, "Outra", 1001), (token, "ChicoEletro", 1600),
                                         ("malformed", "ChicoEletro", 1001)]:
            with self.assertRaises(ValueError):
                validate_token(candidate, KEY, company, now=now)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        path = self.root / "rotalog/equipes/current/index.json.gz"
        path.parent.mkdir(parents=True)
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump({"company": "ChicoEletro", "equipes": {}}, stream)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.create_handler(self.root, KEY, "ChicoEletro", {ORIGIN}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, headers=None, method="GET"):
        try:
            return urlopen(Request(self.url + path, headers=headers or {}, method=method), timeout=3)
        except HTTPError as exc:
            return exc

    def auth(self):
        return {"Authorization": "Bearer " + issue_token(KEY, "ChicoEletro", "user"), "Origin": ORIGIN}

    def test_authorization_required_even_for_conditional_requests(self):
        with self.request("/api/rotalog/index") as response:
            self.assertEqual(response.status, 401)
        with self.request("/api/rotalog/index", self.auth()) as response:
            self.assertEqual(response.status, 200)
            etag = response.headers["ETag"]
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], ORIGIN)
        with self.request("/api/rotalog/index", {"If-None-Match": etag}) as response:
            self.assertEqual(response.status, 401)
        with self.request("/api/rotalog/index", {**self.auth(), "If-None-Match": etag}) as response:
            self.assertEqual(response.status, 304)

    def test_cors_and_paths_are_restricted(self):
        with self.request("/api/rotalog/index", {**self.auth(), "Origin": "https://other.test"}) as response:
            self.assertEqual(response.status, 403)
        with self.request("/.env", self.auth()) as response:
            self.assertEqual(response.status, 404)
        with self.request("/api/torre", self.auth()) as response:
            self.assertEqual(response.status, 503)
        headers = {"Origin": ORIGIN, "Access-Control-Request-Method": "GET",
                   "Access-Control-Request-Headers": "authorization,if-none-match"}
        with self.request("/api/torre", headers, "OPTIONS") as response:
            self.assertEqual(response.status, 204)

    def test_wrong_company_and_corrupt_data_are_not_delivered(self):
        path = self.root / "torre/current.json"
        path.parent.mkdir()
        for payload in ['{"schemaVersion":1,"company":"Outra","items":[]}', '{invalid']:
            path.write_text(payload)
            with self.request("/api/torre", self.auth()) as response:
                self.assertEqual(response.status, 503)

    def test_truncated_gzip_returns_503(self):
        path = self.root / "rotalog/equipes/current/index.json.gz"
        for raw in [gzip.compress(b'{"company":"ChicoEletro","equipes":{}}')[:-4],
                    b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\x07']:
            path.write_bytes(raw)
            with self.request("/api/rotalog/index", self.auth()) as response:
                self.assertEqual(response.status, 503)

    def test_index_requires_team_map_and_objects(self):
        path = self.root / "rotalog/equipes/current/index.json.gz"
        for teams in [None, [], {"E3733": 1}]:
            path.write_bytes(gzip.compress(json.dumps({"company": "ChicoEletro", "equipes": teams}).encode()))
            with self.request("/api/rotalog/index", self.auth()) as response:
                self.assertEqual(response.status, 503)

    def test_complete_tower_and_conditional_read(self):
        from tests.test_torre_contract import projection
        path = self.root / "torre/current.json"
        path.parent.mkdir()
        path.write_text(json.dumps(projection()), encoding="utf-8")
        with self.request("/api/torre", self.auth()) as response:
            self.assertEqual(response.status, 200)
            etag = response.headers["ETag"]
        with self.request("/api/torre", {**self.auth(), "If-None-Match": etag}) as response:
            self.assertEqual(response.status, 304)
            self.assertEqual(response.headers["Access-Control-Expose-Headers"], "ETag")
        path.write_text('{"company":"ChicoEletro","schemaVersion":1,"items":[]}', encoding="utf-8")
        with self.request("/api/torre", self.auth()) as response:
            self.assertEqual(response.status, 503)


if __name__ == "__main__":
    unittest.main()
