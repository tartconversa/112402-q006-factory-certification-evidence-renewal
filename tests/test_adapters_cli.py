"""CLI、解析器和 HTTP 接收端的轻量测试。"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from green_review.cli import main as cli_main
from green_review.server import create_handler
from green_review.sources import parse_factor_content
from green_review import GreenReviewApp

ROOT = Path(__file__).resolve().parent.parent


class CliTests(unittest.TestCase):
    def test_import_samples_and_dashboard_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "review.json")
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main(["--db", db, "import-samples", "--samples", str(ROOT / "samples")])
            self.assertEqual(code, 0)
            self.assertIn('"decisions": 3', output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main(["--db", db, "dashboard", "--as-of", "2026-11-25"])
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertIn("current_certifications", payload)
            self.assertEqual(3, len(payload["decisions"]))


class FactorParserTests(unittest.TestCase):
    def test_parse_json_factor(self):
        factor = parse_factor_content('{"factor_id":"F","value":"1.25","unit":"kg/t"}')
        self.assertEqual(factor["factor_id"], "F")
        self.assertEqual(factor["value"], "1.25")

    def test_parse_html_data_attributes(self):
        html = '<div data-factor-id="FHTML" data-factor-value="0.42" data-factor-unit="kg/kWh"></div>'
        factor = parse_factor_content(html, url="https://example/f")
        self.assertEqual(factor["factor_id"], "FHTML")
        self.assertEqual(factor["value"], "0.42")
        self.assertEqual(factor["source_url"], "https://example/f")


class HttpServerTests(unittest.TestCase):
    def test_health_and_event_endpoint(self):
        app = GreenReviewApp.in_memory()
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                self.assertEqual(response.status, 200)
                health = json.loads(response.read().decode())
                self.assertTrue(health["ok"])

            body = json.dumps(
                {
                    "source": "attachment",
                    "payload": {"attachment_id": "A-HTTP", "site_id": "S1", "sha256": "h"},
                }
            ).encode()
            request = Request(
                f"http://127.0.0.1:{port}/api/events",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                result = json.loads(response.read().decode())
                self.assertEqual(result["status"], "processed")
            self.assertIsNotNone(app.get_evidence("A-HTTP"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
