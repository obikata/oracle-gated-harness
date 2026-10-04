#!/usr/bin/env python3
"""Task threads in the browser: watch agents report, and talk back to them.

    cd <your project> && python3 <plugin>/harness/app.py          # http://127.0.0.1:8765
    python3 <plugin>/harness/app.py --repo ~/proj --port 9000

Each run_task.py run is a thread. The working agent, the harness, the oracles and the
evaluator post into it; anything you type is delivered to the running agent after its
next tool call (via the plugin's PostToolUse hook). "Abort" stops the running agent.
Binds to 127.0.0.1 only. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import threads as th  # noqa: E402

RUN_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def make_handler(repo: Path):
    troot = th.root(repo)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _run_dir(self, run: str) -> Path | None:
            if not RUN_RE.match(run):
                return None
            d = troot / run
            return d if d.is_dir() else None

        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "app.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/threads":
                items = []
                if troot.is_dir():
                    for d in troot.iterdir():
                        if d.is_dir():
                            m = th.read_meta(d)
                            if m:
                                evs = th.read_thread(d)
                                m["events"] = len(evs)
                                m["last"] = evs[-1] if evs else None
                                m["aborting"] = (d / "ABORT").exists()
                                items.append(m)
                items.sort(key=lambda m: m.get("started", ""), reverse=True)
                return self._json({"repo": str(repo), "threads": items})
            m = re.match(r"^/api/thread/([^/]+)$", u.path)
            if m:
                d = self._run_dir(m.group(1))
                if d is None:
                    return self._json({"error": "not found"}, 404)
                after = int(parse_qs(u.query).get("after", ["0"])[0] or 0)
                evs = th.read_thread(d, after)
                meta = th.read_meta(d)
                meta["aborting"] = (d / "ABORT").exists()
                return self._json({"meta": meta, "events": evs, "next": after + len(evs)})
            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            u = urlparse(self.path)
            m = re.match(r"^/api/thread/([^/]+)/(message|abort)$", u.path)
            if not m:
                return self._json({"error": "not found"}, 404)
            d = self._run_dir(m.group(1))
            if d is None:
                return self._json({"error": "not found"}, 404)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                body = {}
            meta = th.read_meta(d)
            if meta.get("status") != "running":
                return self._json({"error": "this task is no longer running"}, 409)
            if m.group(2) == "message":
                text = str(body.get("text", "")).strip()
                if not text:
                    return self._json({"error": "empty"}, 400)
                th.send_to_agent(d, text[:4000])
                return self._json({"ok": True})
            (d / "ABORT").write_text(th.now())
            th.post(d, "user", "text", "⏹ 中断を指示しました" if meta.get("language") == "ja" else "⏹ Abort requested")
            return self._json({"ok": True})

    return H


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="project root (default: git root of the current directory)")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.repo:
        repo = Path(a.repo).expanduser().resolve()
    else:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        repo = Path(r.stdout.strip() or ".").resolve()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(repo))
    print(f"task threads for {repo}\n  → http://127.0.0.1:{a.port}   (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
