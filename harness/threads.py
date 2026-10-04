"""Per-task conversation threads: the event feed behind the browser app.

Each run gets <repo>/.harness/threads/<run_id>/ with
    meta.json     task id/title/type/branch/status/verdict
    thread.jsonl  one event per line: {ts, role, kind, text, meta}
    inbox.jsonl   messages the user typed into the app (delivered by the PostToolUse hook)
    ABORT         present => the harness kills the running agent

Roles: generator (the working agent), harness, oracle, evaluator, user, system.
Stdlib only; safe to import from hooks.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

THREAD_ENV = "OGH_THREAD_DIR"

# ---------------------------------------------------------------- i18n (harness-side voices)

MSG = {
    "en": {
        "start": "Task started on branch `{branch}`. Iterations: {iters}, turns: {turns}, budget ${usd}.",
        "iter": "Iteration {it} begins (fresh context).",
        "agent_done": "The agent finished its turn ({turns} turns{cost}). Running the oracles now.",
        "fast_ok": "Fast oracle: green.", "fast_red": "Fast oracle: RED.\n{tail}",
        "full_ok": "Full suite: green.", "full_red": "Full suite: RED.\n{tail}",
        "gutter": "Same failure {n} times in a row. Stopping this task.",
        "zones_ok": "Zone check: {n} file(s) touched, no violations.",
        "zones_bad": "Zone check: VIOLATION in {files}.",
        "nochange": "False-green guard: {why}",
        "eval_start": "Handing the evidence to the evaluator.",
        "verdict": "{verdict} — {summary}",
        "finish": "Done in {min} min. Branch `{branch}` (never merged).",
        "blocked": "Blocked: {text}",
        "queued": "📨 Got it. The agent is mid-tool-call, so this will reach it after that call finishes.",
        "delivered": "Delivered to the agent: “{text}”",
        "aborted": "Aborted by the user.",
        "stop_check": "Before stopping, I'm running the fast oracle…",
        "stop_red": "The fast oracle is red, so I sent the agent back to fix it.",
        "stop_ok": "Fast oracle green; the agent may finish.",
    },
    "ja": {
        "start": "タスクを開始しました。ブランチは `{branch}`。反復 {iters} 回、ターン {turns}、予算 ${usd}。",
        "iter": "反復 {it} を開始します（新しいコンテキスト）。",
        "agent_done": "作業担当がひと区切りつけました（{turns} ターン{cost}）。オラクルを回します。",
        "fast_ok": "fast オラクル：緑です。", "fast_red": "fast オラクル：赤です。\n{tail}",
        "full_ok": "フルスイート：緑です。", "full_red": "フルスイート：赤です。\n{tail}",
        "gutter": "同じ失敗が {n} 回続いたので、このタスクは打ち切ります。",
        "zones_ok": "ゾーン検査：{n} ファイル変更、違反なし。",
        "zones_bad": "ゾーン検査：違反があります（{files}）。",
        "nochange": "false-green ガード：{why}",
        "eval_start": "証拠一式を審査担当に渡します。",
        "verdict": "判定は {verdict} です。{summary}",
        "finish": "{min} 分で完了。ブランチ `{branch}`（マージはしていません）。",
        "blocked": "止めました：{text}",
        "queued": "📨 受け取りました。作業担当は今ツール操作の最中なので、それが終わり次第届きます。",
        "delivered": "作業担当に届けました：「{text}」",
        "aborted": "ユーザーの指示で中断しました。",
        "stop_check": "終わる前に fast オラクルを回します…",
        "stop_red": "fast オラクルが赤なので、作業担当に直すよう差し戻しました。",
        "stop_ok": "fast オラクルは緑です。作業担当は終了して大丈夫です。",
    },
}


def lang(cfg: dict | None) -> str:
    l = (cfg or {}).get("language", "en")
    return l if l in MSG else "en"


def t(cfg: dict | None, key: str, **kw) -> str:
    return MSG[lang(cfg)][key].format(**kw)


# ---------------------------------------------------------------- thread files

def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def root(repo: Path) -> Path:
    return Path(repo) / ".harness" / "threads"


def create(repo: Path, run_id: str, meta: dict) -> Path:
    d = root(repo) / run_id
    d.mkdir(parents=True, exist_ok=True)
    write_meta(d, {"run": run_id, "status": "running", "started": now(), **meta})
    (d / "thread.jsonl").touch()
    (d / "inbox.jsonl").touch()
    return d


def write_meta(d: Path, meta: dict) -> None:
    cur = read_meta(d)
    cur.update(meta)
    cur["updated"] = now()
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / "meta.json")


def read_meta(d: Path) -> dict:
    try:
        return json.loads((Path(d) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def post(d: Path | str | None, role: str, kind: str, text: str, **meta) -> None:
    if not d:
        return
    ev = {"ts": now(), "role": role, "kind": kind, "text": text}
    if meta:
        ev["meta"] = meta
    with open(Path(d) / "thread.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def read_thread(d: Path, after: int = 0) -> list[dict]:
    out = []
    try:
        with open(Path(d) / "thread.jsonl", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < after:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return out


def from_env() -> Path | None:
    v = os.environ.get(THREAD_ENV)
    return Path(v) if v and Path(v).is_dir() else None


# ---------------------------------------------------------------- inbox (user -> agent)

def send_to_agent(d: Path, text: str) -> None:
    with open(Path(d) / "inbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "text": text}, ensure_ascii=False) + "\n")
    post(d, "user", "text", text)


def take_unread(d: Path) -> list[str]:
    """Return inbox messages not yet delivered and advance the cursor (atomic enough for one consumer)."""
    d = Path(d)
    cur_f = d / "inbox.cursor"
    try:
        cursor = int(cur_f.read_text())
    except (OSError, ValueError):
        cursor = 0
    try:
        lines = (d / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    new = []
    for line in lines[cursor:]:
        try:
            new.append(json.loads(line)["text"])
        except (json.JSONDecodeError, KeyError):
            pass
    if len(lines) > cursor:
        cur_f.write_text(str(len(lines)))
    return new


# ---------------------------------------------------------------- stream-json -> thread events

def _short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


TOOL_KEY = {"Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
            "MultiEdit": "file_path", "NotebookEdit": "notebook_path", "Glob": "pattern", "Grep": "pattern",
            "WebSearch": "query", "WebFetch": "url", "Task": "description", "Agent": "description"}


def stream_event_to_posts(ev: dict, cfg: dict | None = None) -> list[tuple[str, str, str, dict]]:
    """Map one claude stream-json event to (role, kind, text, meta) thread posts."""
    t_ = ev.get("type")
    out: list[tuple[str, str, str, dict]] = []
    if t_ == "assistant":
        for blk in (ev.get("message") or {}).get("content", []):
            if blk.get("type") == "text" and blk.get("text", "").strip():
                out.append(("generator", "text", blk["text"].strip(), {}))
            elif blk.get("type") == "tool_use":
                name = blk.get("name", "?")
                inp = blk.get("input") or {}
                key = TOOL_KEY.get(name)
                arg = inp.get(key, "") if key else json.dumps(inp, ensure_ascii=False)
                out.append(("generator", "tool", _short(arg, 300), {"tool": name}))
    elif t_ == "user":
        for blk in (ev.get("message") or {}).get("content", []):
            if isinstance(blk, dict) and blk.get("type") == "tool_result" and blk.get("is_error"):
                c = blk.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                c = str(c)
                if "zone_guard" in c:
                    out.append(("harness", "blocked", t(cfg, "blocked", text=_short(c, 400)), {}))
                else:
                    out.append(("generator", "error", _short(c, 400), {}))
    elif t_ == "result":
        cost = ev.get("total_cost_usd")
        out.append(("harness", "status", t(cfg, "agent_done", turns=ev.get("num_turns"),
                    cost=f", ${cost:.2f}" if isinstance(cost, (int, float)) else ""),
                    {"cost_usd": cost, "turns": ev.get("num_turns")}))
    return out
