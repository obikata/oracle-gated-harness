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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import threads as th  # noqa: E402

RUN_RE = re.compile(r"^[A-Za-z0-9._-]+$")

QUEUE_DIR = ".claude/harness/queues"
TASKS_DIR = ".claude/harness/tasks"


def _load_queue_tasks(repo: Path) -> list[dict]:
    """Task specs found in .claude/harness/queues/*.jsonl and .claude/harness/tasks/**/*.json,
    deduped by id (first occurrence wins)."""
    seen: dict[str, dict] = {}
    for p in sorted((repo / QUEUE_DIR).glob("*.jsonl")) if (repo / QUEUE_DIR).is_dir() else []:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = t.get("id")
            if tid and tid not in seen:
                seen[tid] = {"id": tid, "title": t.get("title", tid), "type": t.get("type", ""),
                             "source": str(p.relative_to(repo)), "task_id": tid, "budget": t.get("budget", {}),
                             "_full": t}
    if (repo / TASKS_DIR).is_dir():
        for p in sorted((repo / TASKS_DIR).rglob("*.json")):
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tid = t.get("id")
            if tid and tid not in seen:
                seen[tid] = {"id": tid, "title": t.get("title", tid), "type": t.get("type", ""),
                             "source": str(p.relative_to(repo)), "task_id": None, "budget": t.get("budget", {}),
                             "_full": t}
    return list(seen.values())


def _public_queue_item(t: dict) -> dict:
    return {k: v for k, v in t.items() if k != "_full"}


def _task_status(repo: Path, troot: Path, task_id: str) -> dict:
    """pending (never run) / running / done, cross-referencing threads (any run, any branch)
    and legacy .harness/<id>/ evidence dirs (runs that predate the threads feature, or that were
    already merged to the current branch)."""
    if troot.is_dir():
        for d in troot.iterdir():
            if d.is_dir() and th.read_meta(d).get("task") == task_id:
                m = th.read_meta(d)
                return {"status": m.get("status", "unknown"), "verdict": m.get("verdict"), "run": d.name}
    legacy = repo / ".harness" / task_id
    if legacy.is_dir():
        verdict = None
        try:
            rj = json.loads((legacy / "run.json").read_text(encoding="utf-8"))
            verdict = rj.get("verdict")
        except (OSError, json.JSONDecodeError):
            pass
        return {"status": "done", "verdict": verdict, "run": None}
    return {"status": "pending", "verdict": None, "run": None}


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
            if u.path == "/api/queue":
                items = [_public_queue_item({**t, **_task_status(repo, troot, t["id"])})
                         for t in _load_queue_tasks(repo)]
                items.sort(key=lambda t: (t["status"] != "pending", t["id"]))
                return self._json({"repo": str(repo), "queue": items})
            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            u = urlparse(self.path)
            if u.path == "/api/queue/run":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    body = {}
                source = str(body.get("source", ""))
                task_id = body.get("task_id")
                full = (repo / source).resolve()
                allowed = [(repo / QUEUE_DIR).resolve(), (repo / TASKS_DIR).resolve()]
                if not full.is_file() or not any(str(full).startswith(str(a) + "/") for a in allowed):
                    return self._json({"error": "invalid source"}, 400)
                cmd = [sys.executable, str(HERE / "run_task.py"), "--task", str(full)]
                if task_id:
                    cmd += ["--id", str(task_id)]
                log_dir = repo / ".harness"
                log_dir.mkdir(parents=True, exist_ok=True)
                with open(log_dir / "queue_launch.log", "ab") as lf:
                    subprocess.Popen(cmd, cwd=str(repo), stdout=lf, stderr=lf,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
                return self._json({"ok": True})
            if u.path == "/api/queue/run_batch":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    body = {}
                ids = [str(i) for i in (body.get("ids") or [])]
                by_id = {t["id"]: t for t in _load_queue_tasks(repo)}
                standalone_files: list[Path] = []
                batched_specs = []
                for tid in ids:
                    t = by_id.get(tid)
                    if t is None:
                        continue
                    if t["task_id"] is None:
                        standalone_files.append((repo / t["source"]).resolve())
                    else:
                        batched_specs.append(t["_full"])
                if not standalone_files and not batched_specs:
                    return self._json({"error": "no valid task ids"}, 400)
                task_file_args = [str(p) for p in standalone_files]
                if batched_specs:
                    qdir = repo / ".harness" / "queue"
                    qdir.mkdir(parents=True, exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    batch_file = qdir / f"_webrun_{stamp}.jsonl"
                    with open(batch_file, "w", encoding="utf-8") as f:
                        for spec in batched_specs:
                            f.write(json.dumps(spec, ensure_ascii=False) + "\n")
                    task_file_args.append(str(batch_file))
                cmd = [sys.executable, str(HERE / "run_queue.py"), "--tasks", *task_file_args]
                log_dir = repo / ".harness"
                log_dir.mkdir(parents=True, exist_ok=True)
                with open(log_dir / "queue_launch.log", "ab") as lf:
                    subprocess.Popen(cmd, cwd=str(repo), stdout=lf, stderr=lf,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
                return self._json({"ok": True, "n": len(ids)})
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
