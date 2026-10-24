# SPDX-License-Identifier: MIT
"""Serve the web dashboard design option on loopback from a fixture bundle.

This is a design preview, not a worker listener. It reads one bundle file
and serves it unchanged with four static files. It answers GET and HEAD
only and refuses any address that is not loopback; a LAN bind waits on an
owner decision about the listener, clients and authentication.
"""

import argparse
import http.server
import ipaddress
import json
from pathlib import Path
import socket
import sys

HERE = Path(__file__).resolve().parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/model.js": ("model.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def handler(bundle, mode=None):
    observation = json.dumps({"mode": mode}).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "rookrunner-dashboard-option"
        sys_version = ""

        def do_GET(self):
            self._answer(body=True)

        def do_HEAD(self):
            self._answer(body=False)

        def _refuse(self, code):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            if code == 405:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()

        def _answer(self, body):
            port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
            if self.headers.get("Host") not in allowed:
                return self._refuse(421)
            path = self.path.split("?", 1)[0]
            if path == "/bundle.json":
                data, kind = bundle.read_bytes(), "application/json"
            elif path == "/observation.json":
                data, kind = observation, "application/json"
            elif path in STATIC:
                name, kind = STATIC[path]
                data = (HERE / name).read_bytes()
            else:
                return self._refuse(404)
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if body:
                self.wfile.write(data)

        def log_message(self, *_args):
            pass

    for verb in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        setattr(Handler, f"do_{verb}", lambda self: self._refuse(405))
    return Handler


class V6Server(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


def build(bundle, host="127.0.0.1", port=8766, mode=None):
    if not loopback(host):
        raise ValueError(f"{host} is not a loopback address; a LAN bind needs an owner decision")
    family = V6Server if ":" in host else http.server.ThreadingHTTPServer
    return family((host, port), handler(Path(bundle), mode))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", required=True, help="fixture bundle JSON path")
    parser.add_argument("--host", default="127.0.0.1", help="loopback address only")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--mode",
        choices=("current", "stale", "unavailable"),
        help="observation mode to show instead of the bundle's own; the bundle is served unchanged",
    )
    args = parser.parse_args(argv)
    if not Path(args.bundle).is_file():
        parser.error(f"bundle not found: {args.bundle}")
    try:
        server = build(args.bundle, args.host, args.port, args.mode)
    except ValueError as exc:
        parser.error(str(exc))
    sys.stderr.write(f"http://{args.host}:{args.port}/\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
