"""A tiny stand-in for GitHub's release URLs, so get.sh can be tested without publishing anything.

    python3 fake_releases.py DIR TAG PORT [--tamper FILE]

  GET /latest              -> 302 to /tag/TAG           (what github.com/<repo>/releases/latest does)
  GET /download/TAG/<file> -> the file from DIR         (--tamper: with one byte flipped, to test checksum refusal)
"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

directory, tag, port = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
tamper = sys.argv[sys.argv.index("--tamper") + 1] if "--tamper" in sys.argv else None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/latest":
            self.send_response(302)
            self.send_header("Location", f"/tag/{tag}")
            self.end_headers()
        elif self.path == f"/tag/{tag}":
            self._send(b"release page")
        elif self.path.startswith(f"/download/{tag}/"):
            name = self.path.rsplit("/", 1)[1]
            f = directory / name
            if not f.is_file():
                return self.send_error(404)
            data = f.read_bytes()
            if name == tamper:
                data = data[:-1] + bytes([data[-1] ^ 0xFF])
            self._send(data)
        else:
            self.send_error(404)

    def _send(self, data):
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
