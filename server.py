#!/usr/bin/env python3
"""server.py — Yuan Knowledge Base workspace server with Agent IPC bridge.

Replaces Python's plain http.server with a lightweight server that:
  1. Serves static files (knowledge HTML, paper-ui, etc.)
  2. Provides a POST /api/message endpoint → writes to .agent/inbox/
  3. Provides a GET  /api/status  endpoint → reads from .agent/outbox/
  4. Provides a POST /api/upload  endpoint → saves PDF to .agent/uploads/
  5. Provides GET/POST /api/config  → read/update runtime configuration

The local Agent (Claude Code, Codex, etc.) watches .agent/inbox/ for new
messages and writes responses/progress to .agent/outbox/. This file-based
IPC keeps the server pure-stdlib and avoids any pip dependency.

Usage:
    python3 server.py [--port 8741]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import re
from urllib.parse import urlparse, parse_qs

WORKSPACE = Path(__file__).resolve().parent
AGENT_DIR = WORKSPACE / ".agent"
INBOX = AGENT_DIR / "inbox"
OUTBOX = AGENT_DIR / "outbox"
UPLOADS = AGENT_DIR / "uploads"
HISTORY_DIR = AGENT_DIR / "history"

# Random id regenerated on every server boot. Used by the frontend to tell a
# browser refresh (same boot_id ⇒ restore conversation) apart from a process
# restart (boot_id changed ⇒ blank page).
_BOOT_ID = uuid.uuid4().hex[:8]

AGENT_COMMANDS = {
    # bypassPermissions aligns claude's trust level with codex's `-s workspace-write`:
    # both treat the local workspace as trusted, so WebSearch/WebFetch/Bash run without
    # per-call confirmation. In headless `-p` mode there is no human to approve prompts,
    # so we also disable AskUserQuestion to avoid the dead-end where a permission prompt
    # is auto-denied and the agent falls back to self-configuring permissions.
    # NOTE: --disallowedTools is variadic (<tools...>); it must NOT be the last
    # flag, or it greedily swallows the prompt that watcher_loop appends as the
    # final positional arg. Keep the single-value --permission-mode last so the
    # variadic list is terminated by the following "--permission-mode" token.
    "claude": ["claude", "-p", "--output-format", "stream-json", "--verbose",
               "--disallowedTools", "AskUserQuestion",
               "--permission-mode", "bypassPermissions"],
    "codex": ["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write"],
}

# ── Runtime config (hot-switchable via /api/config) ──
current_agent: str = "claude"
agent_timeout: int = 1800
auto_open_pdf: bool = True
_config_lock = threading.Lock()
# Serializes read/modify/write of knowledge/INDEX_KB.md so a frontend delete/
# rename can't interleave with an Agent appending a new chapter entry.
# The same lock guards knowledge/.kb_meta.json (display-layer metadata) so a
# chapter rename / reorder / move stays atomic across both files.
_index_lock = threading.Lock()


# ── Display-layer metadata (knowledge/.kb_meta.json) ──
# Decouples the sidebar's *display* (custom chapter labels + per-chapter note
# order) from the filesystem, so renaming a chapter or reordering notes never
# has to touch directory/file names (and thus never breaks a note's rel_path).
# Absent or corrupt → safe empty defaults, i.e. exactly today's behavior.
def _kb_meta_path() -> Path:
    return WORKSPACE / "knowledge" / ".kb_meta.json"


def _load_kb_meta() -> dict:
    meta = {"version": 1, "chapters": {}, "order": {}}
    p = _kb_meta_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("chapters"), dict):
                    meta["chapters"] = data["chapters"]
                if isinstance(data.get("order"), dict):
                    meta["order"] = data["order"]
        except Exception:
            pass  # corrupt file → fall back to defaults, never break the tree
    return meta


def _save_kb_meta(meta: dict) -> None:
    """Atomic write: temp file + os.replace, so a reader never sees half a file."""
    p = _kb_meta_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ── Note-companion reconciliation (self-heal out-of-band deletions) ──
# A note's canonical artifact is its HTML view — `<stem>.html` (final) or
# `<stem>.draft.html` (pending review). Its companions (`<stem>.md`,
# `<stem>.draft.md`, `<stem>.sources/`) only have meaning alongside it, so
# `_handle_delete_note` removes the whole set together. But a deletion that
# bypasses that API — a manual file-explorer delete, or another agent editing
# the workspace — strands the companions (and can leave an empty chapter dir),
# breaking the invariant. Reconciliation re-derives it from disk so the same
# end state results no matter who removed the HTML.
_ORPHAN_GRACE_SECONDS = 10 * 60  # spare companions of a note still being written


def _note_base_stem(name: str) -> str:
    """Strip a note artifact's suffix to the base stem its companions share.

    'foo.draft.html' / 'foo.html' / 'foo.draft.md' / 'foo.md' / 'foo.sources'
    all map to 'foo'. Order matters: the longer compound suffixes must win.
    """
    for suffix in (".draft.html", ".html", ".draft.md", ".md", ".sources"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _newest_mtime(p: Path) -> float:
    """Most recent mtime of a path; for a directory, the max over its contents
    so a `.sources/` folder being actively populated reads as fresh."""
    try:
        if p.is_dir():
            newest = p.stat().st_mtime
            for c in p.rglob("*"):
                try:
                    newest = max(newest, c.stat().st_mtime)
                except OSError:
                    pass
            return newest
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _reconcile_kb_orphans(kb_dir: Path) -> tuple[list[str], list[str]]:
    """Garbage-collect note companions stranded by out-of-band note deletions.

    Mirrors what `_handle_delete_note` would have swept, derived purely from
    disk. Safe to call on every tree load:
      * A stem is "live" if either its .html or .draft.html exists, so finished
        notes and pending-review drafts are never touched.
      * Companions modified within the grace window are skipped, so a note still
        mid-generation (its .md / .sources land before the .html) survives.
      * A chapter directory is removed only if *this pass* emptied it — never a
        pre-existing or freshly-created empty chapter another agent may be about
        to fill.

    Returns (removed_paths, removed_dirs); best-effort, never raises.
    """
    removed: list[str] = []
    removed_dirs: list[str] = []
    if not kb_dir.exists():
        return removed, removed_dirs
    cutoff = time.time() - _ORPHAN_GRACE_SECONDS
    for d in sorted(kb_dir.iterdir()):
        if not d.is_dir() or not d.name[:2].isdigit():
            continue
        try:
            entries = [p for p in d.iterdir() if p.name != ".DS_Store"]
        except OSError:
            continue
        # Stems backed by a real note view (covers both .html and .draft.html).
        live_stems = {
            _note_base_stem(p.name)
            for p in entries
            if p.is_file() and p.name.endswith(".html")
        }
        swept_here = False
        for p in entries:
            is_companion = (p.is_dir() and p.name.endswith(".sources")) or (
                p.is_file() and p.name.endswith(".md")
            )
            if not is_companion or _note_base_stem(p.name) in live_stems:
                continue
            if _newest_mtime(p) >= cutoff:
                continue  # still being written — let the generation finish
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                removed.append(str(p.relative_to(WORKSPACE)))
                swept_here = True
            except OSError:
                pass
        # Drop the chapter dir only when this sweep is what emptied it.
        if swept_here:
            try:
                if not [p for p in d.iterdir() if p.name != ".DS_Store"]:
                    for junk in d.iterdir():
                        junk.unlink()
                    d.rmdir()
                    removed_dirs.append(d.name)
            except OSError:
                pass
    return removed, removed_dirs


# ── Agent process lifecycle (parallel task registry) ──
MAX_PARALLEL = 3

class TaskHandle:
    __slots__ = ("msg_id", "proc", "abort", "thread", "start_time", "mode",
                 "session_id", "snapshot")
    def __init__(self, msg_id: str, mode: str, session_id: str):
        self.msg_id = msg_id
        self.proc: subprocess.Popen | None = None
        self.abort = threading.Event()
        self.thread: threading.Thread | None = None
        self.start_time = time.time()
        self.mode = mode
        self.session_id = session_id
        self.snapshot: set[str] = set()

_tasks: dict[str, TaskHandle] = {}
_tasks_lock = threading.Lock()
# Kept for backwards compat: no-arg abort sets this to signal all tasks.
_abort_flag = threading.Event()

# ── Milestone classification (replaces simple keyword matching) ──
MILESTONE_PATTERNS = [
    (re.compile(r"^(Reading|Analyzing|Extracting|Parsing|Summarizing|Processing) ", re.IGNORECASE), "content_analysis"),
    (re.compile(r"^(I'll |I found |I'm going to |Next, |Done\.|Completed )", re.IGNORECASE), "reasoning"),
    (re.compile(r"^(wrote|Created|Generated) .+\.(html|md)", re.IGNORECASE), "file_created"),
    (re.compile(r"python3 skills/"), "skill_invoked"),
    (re.compile(r"^web search:\s*\S", re.IGNORECASE), "web_search"),
    (re.compile(r"^(ERROR|Error|error)[:\s]|^FAILED\b"), "error"),
    (re.compile(r"PDF uploaded:", re.IGNORECASE), "user_action"),
    (re.compile(r"Agent \(\w+\) started", re.IGNORECASE), "lifecycle"),
]

SUPPRESS_PATTERNS = [
    re.compile(r"^knowledge/\S+$"),
    re.compile(r"^/bin/"),
    re.compile(r"^(diff --git|\+\+\+ |\-\-\- |@@)"),
    re.compile(r"^\+"),             # git diff added-content lines (any line starting with +)
    re.compile(r"^-[^-]"),         # git diff removed-content lines (- but not ---)
    re.compile(r"^\s*(exec|succeeded|exited)"),
    re.compile(r"(source sha256|TOC entries|bytes,)"),
    re.compile(r"^(files = |except |import )"),
    re.compile(r"^\s*$"),
    re.compile(r"OMP: Error #15"),
    re.compile(r"^mcp:.*\(failed\)"),
    re.compile(r"Available Skills:|Read the skill"),
    re.compile(r"Reading additional input from stdin"),
    re.compile(r"Execute the task using skills/"),
    re.compile(r"\$.*\\"),
    re.compile(r"^The \w.{20,}"),
    re.compile(r"^(Each|This|These|Those|A |An |In |For |With |Since |Although )\w.{30,}"),
    re.compile(r"^(#{1,3} )"),
]


PHASE_MAP = {
    "lifecycle": "initializing",
    "web_search": "searching",
    "reasoning": "analyzing",
    "content_analysis": "analyzing",
    "skill_invoked": "generating",
    "file_created": "generating",
}


def _classify_line(line: str) -> str | None:
    """Return the milestone category for a line, or None to suppress."""
    stripped = line.strip()
    if not stripped:
        return None
    for pat in SUPPRESS_PATTERNS:
        if pat.search(stripped):
            return None
    for pat, category in MILESTONE_PATTERNS:
        if pat.search(stripped):
            return category
    return None


# ── Claude stream-json event parsing ──
# When claude runs with `--output-format stream-json --verbose`, every stdout
# line is a JSON event. We translate tool_use / text / result events into
# milestones that are *isomorphic* to the Codex text-line milestones, so the
# rest of the progress pipeline (_write_outbox, frontend) needs zero changes.
_STREAM_EVENT_TYPES = {"system", "assistant", "user", "result"}


def _rel_to_workspace(path: str) -> str:
    """Best-effort make an absolute path relative to the workspace for display."""
    try:
        return str(Path(path).resolve().relative_to(WORKSPACE))
    except (ValueError, OSError):
        return os.path.basename(path) or path


def _is_kb_content_file(path: str) -> bool:
    """True only for knowledge/** .html/.md files — the real knowledge output.

    Used to keep the "generating / files" milestone count honest: writes/edits to
    .claude/**, tmp files, scripts, etc. are process noise and must not be counted
    as knowledge produced.
    """
    if not path:
        return False
    if _is_sources_path(path):
        return False  # raw retrieved material, not a knowledge note
    rel = _rel_to_workspace(path)
    p = rel.replace(os.sep, "/")
    return p.startswith("knowledge/") and p.lower().endswith((".html", ".md"))


def _is_sources_path(path: str) -> bool:
    """True if path lives inside a per-note raw-sources folder (`<stem>.sources/`).

    Web-retrieved originals are persisted next to each note in a sibling
    `<stem>.sources/` directory (manifest + per-source excerpts). That material is
    *reference*, NOT a knowledge note: it must never be counted as generated
    output, surfaced as a "note created" milestone, or flagged as an unregistered
    draft. We match the `.sources` directory-name suffix (explicit) and, as a
    belt-and-suspenders fallback, any knowledge file nested deeper than the
    canonical `knowledge/NN_dir/<file>` layout (every real note sits at depth 2).
    """
    if not path:
        return False
    rel = _rel_to_workspace(path).replace(os.sep, "/")
    if not rel.startswith("knowledge/"):
        return False
    parts = rel.split("/")
    if any(seg.endswith(".sources") for seg in parts[:-1]):
        return True
    # parts == ["knowledge", "NN_dir", "file"] for a real note (len 3).
    return len(parts) > 3


def _tool_use_to_milestone(name: str, tool_input: dict) -> tuple[str, str] | None:
    """Map a claude tool_use block to (milestone_text, phase).

    Text is deliberately prefixed (web search: / Reading / Created / wrote /
    INDEX_KB) so the frontend's getMilestoneCategory()/inferPhase() fallbacks
    classify it correctly even without reading meta.phase — double insurance.
    """
    inp = tool_input if isinstance(tool_input, dict) else {}
    if name == "WebSearch":
        return f"web search: {inp.get('query', '')}".strip(), "searching"
    if name == "WebFetch":
        return f"web search: {inp.get('url', '')}".strip(), "searching"
    if name == "Read":
        return f"Reading {os.path.basename(inp.get('file_path', ''))}".strip(), "analyzing"
    if name in ("Grep", "Glob"):
        pat = inp.get("pattern") or inp.get("query") or ""
        return f"Analyzing {pat}".strip(), "analyzing"
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        if not cmd:
            return None
        snippet = cmd[:110]
        # Index updates are a real, user-meaningful step.
        if "INDEX_KB" in cmd:
            return snippet, "indexing"
        # Genuine "produce/render" work goes through the workspace skills (e.g.
        # `python3 skills/render_html/render.py …`). Surface those as generating.
        if "skills/" in cmd or "render" in cmd:
            return snippet, "generating"
        # Everything else (ls/cat/mkdir/touch/echo/tee/cd/grep/head/tail/curl/find/
        # mv/cp/rm and other auxiliary shell) is process noise — emit no milestone
        # so it can't inflate the "files generated" count.
        return None
    if name == "Write":
        fp = inp.get("file_path", "")
        if not _is_kb_content_file(fp):
            return None  # .claude/**, tmp, scripts → not knowledge output
        return f"Created {_rel_to_workspace(fp)}", "generating"
    if name in ("Edit", "MultiEdit"):
        fp = inp.get("file_path", "")
        if "INDEX_KB" in fp:
            return f"Updating INDEX_KB.md", "indexing"
        if not _is_kb_content_file(fp):
            return None
        return f"wrote {_rel_to_workspace(fp)}", "generating"
    if name == "Skill":
        return f"python3 skills/{inp.get('command') or inp.get('skill') or ''}".strip(), "generating"
    if name in ("TodoWrite", "Task", "TaskOutput", "TaskStop"):
        return None  # internal bookkeeping, suppress
    # Unknown tool: still surface it in the Activity stream without a phase.
    return name, ""


def _parse_stream_event(line: str) -> dict | None:
    """Parse one claude stream-json line.

    Returns None for non-JSON lines (Codex plain text) so they fall through to
    the legacy _classify_line() branch. For recognized claude events returns:
        {"milestones": [(text, phase), ...],
         "assistant_text": str | None,
         "final_text": str | None}
    """
    stripped = line.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("type") not in _STREAM_EVENT_TYPES:
        return None

    result: dict = {"milestones": [], "assistant_text": None, "final_text": None}
    etype = obj.get("type")

    if etype == "assistant":
        message = obj.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                m = _tool_use_to_milestone(block.get("name", ""), block.get("input") or {})
                if m and m[0]:
                    result["milestones"].append(m)
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    # First sentence/line as a milestone; full text accumulated.
                    first = re.split(r"(?<=[.!?。！？])\s|\n", text, maxsplit=1)[0].strip()
                    if first:
                        result["milestones"].append((first[:120], "analyzing"))
                    result["assistant_text"] = text
            # 'thinking' blocks are internal reasoning — ignore.
    elif etype == "result":
        res = obj.get("result")
        if isinstance(res, str) and res.strip():
            result["final_text"] = res.strip()
    # 'system'/'user' events carry no user-facing milestone.

    return result


def _is_similar_to_recent(new_text: str, recent: list[str], threshold: int = 3) -> bool:
    """Check if new_text is too similar to any of the last N milestones."""
    new_words = set(new_text.lower().split())
    for prev in recent[-threshold:]:
        prev_words = set(prev.lower().split())
        overlap = len(new_words & prev_words) / max(len(new_words | prev_words), 1)
        if overlap > 0.7:
            return True
    return False


def ensure_dirs():
    for d in [AGENT_DIR, INBOX, OUTBOX, UPLOADS, HISTORY_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    # Write a README so agents know the protocol
    protocol_file = AGENT_DIR / "PROTOCOL.md"
    if not protocol_file.exists():
        protocol_file.write_text(PROTOCOL_DOC, encoding="utf-8")


PROTOCOL_DOC = """\
# .agent/ — File-based IPC Protocol

This directory is the communication bridge between the paper-ui frontend
and the local Agent process (Claude Code, Codex, etc.).

## Directory Layout

```
.agent/
├── PROTOCOL.md          ← This file
├── inbox/               ← Frontend → Agent (agent reads & deletes after processing)
│   └── msg_<timestamp>_<uuid>.json
├── outbox/              ← Agent → Frontend (frontend polls & displays)
│   └── resp_<timestamp>_<uuid>.json
└── uploads/             ← PDF files uploaded via the frontend
    └── <filename>.pdf
```

## Message Format (inbox)

```json
{
  "id": "msg_1716700000_abc123",
  "timestamp": "2025-05-26T10:00:00Z",
  "type": "user_message",
  "content": "检索 FlashAttention-3 最新进展",
  "mode": "generate",
  "context": {
    "current_file": "04_generative_theory/flow_matching_tutorial.html",
    "uploaded_pdf": null
  }
}
```

### Optional chat-mode fields (all default-safe; absence ⇒ legacy generate)

- `mode`: `generate` (default) | `baguwen` | `interview` | `baguwen_complete` | `agent_complete`
- `history`: `[{"role":"user|agent","text":"..."}]` — replayed for multi-turn continuity (baguwen/interview)
- `selection`: string — the user's highlighted passage
- `session_id`: string — ties a baguwen/interview page together (notes filename, etc.)

`baguwen`/`interview` run a lightweight prompt (no INDEX.md, no draft pipeline);
their outbox events carry `meta.mode` so the frontend renders them as inline chat
bubbles instead of Progress cards. `*_complete` writes a finalized supplement note
(the user already confirmed in the UI, so it skips the draft / Pending Review flow).

## Response Format (outbox)

```json
{
  "id": "resp_1716700005_def456",
  "reply_to": "msg_1716700000_abc123",
  "timestamp": "2025-05-26T10:00:05Z",
  "type": "agent_response | progress | error",
  "content": "正在检索 ArXiv 上的 FlashAttention-3 相关论文...",
  "status": "running | completed | error"
}
```

## Agent Workflow

1. Agent watches `inbox/` for new `.json` files (poll or fs-watch).
2. Agent reads the message, deletes it from `inbox/`.
3. Agent writes progress updates to `outbox/` (type=progress).
4. Agent writes the final response to `outbox/` (type=agent_response, status=completed).
5. Frontend polls `GET /api/status` to pick up new outbox messages.
"""


class WorkspaceHandler(SimpleHTTPRequestHandler):
    """Static file server + JSON API for Agent IPC."""

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/message":
            self._handle_message()
        elif path == "/api/upload":
            self._handle_upload()
        elif path == "/api/config":
            self._handle_config_post()
        elif path == "/api/clear":
            self._handle_clear()
        elif path == "/api/abort":
            self._handle_abort()
        elif path == "/api/knowledge-note/rename":
            self._handle_rename_label()
        elif path == "/api/knowledge-chapter/rename":
            self._handle_chapter_rename()
        elif path == "/api/knowledge-note/reorder":
            self._handle_note_reorder()
        elif path == "/api/knowledge-note/move":
            self._handle_note_move()
        elif path == "/api/completion-withdraw":
            self._handle_completion_withdraw()
        elif path == "/api/history/save":
            self._handle_history_save()
        elif path == "/api/history/delete":
            self._handle_history_delete()
        else:
            self.send_error(404, "Not Found")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path == "/api/knowledge-note":
            self._handle_delete_note()
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/status":
            self._handle_status()
        elif path == "/api/tasks":
            self._handle_tasks()
        elif path == "/api/health":
            self._send_json({"status": "ok", "timestamp": _now_iso()})
        elif path == "/api/config":
            self._handle_config_get()
        elif path == "/api/knowledge-tree":
            self._handle_knowledge_tree()
        elif path == "/api/history/list":
            self._handle_history_list()
        elif path == "/api/history/get":
            self._handle_history_get()
        else:
            # Serve static files
            super().do_GET()

    # --- API Handlers ---

    def _handle_message(self):
        """POST /api/message — write user message to inbox."""
        try:
            body = self._read_body()
            data = json.loads(body)
        except (json.JSONDecodeError, Exception) as e:
            self._send_json({"error": str(e)}, status=400)
            return

        msg_id = f"msg_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        # ── Chat-mode fields (all optional; absence ⇒ legacy generate flow) ──
        # mode: agent | generate | baguwen | interview | baguwen_complete | agent_complete
        mode = data.get("mode") or "generate"
        message = {
            "id": msg_id,
            "timestamp": _now_iso(),
            "type": "user_message",
            "content": data.get("content", ""),
            "mode": mode,
            "history": data.get("history") or [],
            "selection": data.get("selection") or "",
            "session_id": data.get("session_id") or "",
            "interview_difficulty": data.get("interview_difficulty") or "",
            "interview_round": data.get("interview_round") or "",
            "context": {
                "current_file": data.get("current_file"),
                "uploaded_pdf": data.get("uploaded_pdf"),
            },
        }

        msg_file = INBOX / f"{msg_id}.json"
        msg_file.write_text(json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8")

        self._send_json({
            "ok": True,
            "id": msg_id,
            "inbox_path": str(msg_file.relative_to(WORKSPACE)),
        })

    def _handle_status(self):
        """GET /api/status — read recent outbox messages (max 200)."""
        messages = []
        if OUTBOX.exists():
            # Order by mtime (write time), not by name: resp_id is only
            # second-granularity, so a queued task's "started" and the prior
            # task's "completed" can share a second and sort randomly by name.
            # The frontend processes events in this order, so chronological
            # ordering is what keeps queued task cards from being mis-sequenced.
            def _outbox_mtime(f: Path) -> tuple:
                try:
                    return (f.stat().st_mtime, f.name)
                except OSError:
                    return (float("inf"), f.name)

            files = sorted(OUTBOX.glob("*.json"), key=_outbox_mtime)
            for f in files[-200:]:
                try:
                    msg = json.loads(f.read_text(encoding="utf-8"))
                    msg["_file"] = f.name
                    messages.append(msg)
                except Exception:
                    pass

        inbox_count = len(list(INBOX.glob("*.json"))) if INBOX.exists() else 0

        self._send_json({
            "messages": messages,
            "pending_inbox": inbox_count,
            "timestamp": _now_iso(),
        })

    def _handle_tasks(self):
        """GET /api/tasks — return currently running task info."""
        with _tasks_lock:
            running = []
            for mid, h in _tasks.items():
                if h.proc and h.proc.poll() is None:
                    running.append({
                        "task_id": mid,
                        "mode": h.mode,
                        "elapsed": round(time.time() - h.start_time, 1),
                    })
        self._send_json({"running": running, "count": len(running),
                         "max_parallel": MAX_PARALLEL})

    def _handle_upload(self):
        """POST /api/upload — save uploaded PDF to .agent/uploads/."""
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if "multipart/form-data" in content_type:
            try:
                boundary = content_type.split("boundary=")[1].strip().encode()
            except (IndexError, AttributeError):
                self._send_json({"error": "Missing boundary"}, status=400)
                return
            filename, file_data = _parse_multipart(body, boundary)
        else:
            filename = self.headers.get("X-Filename", "cv_temp.pdf")
            file_data = body

        if not filename or not file_data:
            self._send_json({"error": "No file found in request"}, status=400)
            return

        safe_name = Path(filename).name
        if not safe_name.lower().endswith('.pdf'):
            self._send_json({"error": "Only PDF files are accepted"}, status=400)
            return
        if len(file_data) > 20 * 1024 * 1024:
            self._send_json({"error": "File too large (max 20MB)"}, status=400)
            return

        dest = UPLOADS / safe_name
        dest.write_bytes(file_data)
        self._send_json({
            "ok": True,
            "filename": safe_name,
            "path": str(dest.relative_to(WORKSPACE)),
            "size": len(file_data),
        })

    def _handle_config_get(self):
        """GET /api/config — return current runtime configuration."""
        kb_dir = WORKSPACE / "knowledge"
        kb_count = len(list(kb_dir.rglob("*.html"))) if kb_dir.exists() else 0
        with _config_lock:
            self._send_json({
                "agent": current_agent,
                "timeout": agent_timeout,
                "auto_open_pdf": auto_open_pdf,
                "available_agents": list(AGENT_COMMANDS.keys()),
                "knowledge_count": kb_count,
                "port": self.server.server_port,
                "boot_id": _BOOT_ID,
            })

    def _handle_config_post(self):
        """POST /api/config — update runtime configuration."""
        global current_agent, agent_timeout, auto_open_pdf
        try:
            body = self._read_body()
            data = json.loads(body)
        except (json.JSONDecodeError, Exception) as e:
            self._send_json({"error": str(e)}, status=400)
            return

        with _config_lock:
            changed = {}
            if "agent" in data and data["agent"] in AGENT_COMMANDS:
                current_agent = data["agent"]
                changed["agent"] = current_agent
            if "timeout" in data:
                agent_timeout = max(60, min(3600, int(data["timeout"])))
                changed["timeout"] = agent_timeout
            if "auto_open_pdf" in data:
                auto_open_pdf = bool(data["auto_open_pdf"])
                changed["auto_open_pdf"] = auto_open_pdf

        if changed:
            print(f"[config] Updated: {changed}")

        self._send_json({"ok": True, "current": changed})

    # --- Conversation history (persisted chat) ---

    @staticmethod
    def _safe_conv_id(conv_id: str) -> str | None:
        """Validate a conversation id so it can't escape HISTORY_DIR."""
        if conv_id and re.fullmatch(r"[A-Za-z0-9_-]+", conv_id):
            return conv_id
        return None

    def _handle_history_list(self):
        """GET /api/history/list — metadata for every saved conversation."""
        items = []
        if HISTORY_DIR.exists():
            for f in HISTORY_DIR.glob("*.json"):
                try:
                    conv = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                conv.pop("messages", None)
                items.append(conv)
        items.sort(key=lambda c: c.get("updated", ""), reverse=True)
        self._send_json(items)

    def _handle_history_get(self):
        """GET /api/history/get?id=xxx — full JSON for one conversation."""
        qs = parse_qs(urlparse(self.path).query)
        conv_id = self._safe_conv_id((qs.get("id") or [""])[0])
        if not conv_id:
            self._send_json({"error": "invalid id"}, status=400)
            return
        target = HISTORY_DIR / f"{conv_id}.json"
        if not target.exists():
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            self._send_json(json.loads(target.read_text(encoding="utf-8")))
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_history_save(self):
        """POST /api/history/save — persist a conversation JSON body."""
        try:
            data = json.loads(self._read_body())
        except Exception as e:
            self._send_json({"error": str(e)}, status=400)
            return
        conv_id = self._safe_conv_id(str(data.get("id") or ""))
        if not conv_id:
            self._send_json({"error": "invalid id"}, status=400)
            return
        try:
            _save_history_file(conv_id, data)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)
            return
        self._send_json({"ok": True, "id": conv_id})

    def _handle_history_delete(self):
        """POST /api/history/delete — remove one or more conversations."""
        try:
            data = json.loads(self._read_body())
        except Exception as e:
            self._send_json({"error": str(e)}, status=400)
            return
        ids = data.get("ids")
        if not ids:
            ids = [data.get("id")] if data.get("id") else []
        deleted = []
        for raw in ids:
            conv_id = self._safe_conv_id(str(raw or ""))
            if not conv_id:
                continue
            target = HISTORY_DIR / f"{conv_id}.json"
            try:
                target.unlink()
                deleted.append(conv_id)
            except FileNotFoundError:
                pass
            except Exception:
                pass
        self._send_json({"ok": True, "deleted": deleted})

    def _handle_knowledge_tree(self):
        """GET /api/knowledge-tree — scan knowledge/ and INDEX_KB.md for the nav tree."""
        kb_dir = WORKSPACE / "knowledge"
        # Self-heal before scanning: sweep note companions / empty chapters left
        # behind by a deletion that bypassed the delete API (manual file ops or
        # another agent), so the nav reflects on-disk reality. Held under the
        # same lock as delete/move/rename so it can't interleave with them.
        with _index_lock:
            swept, swept_dirs = _reconcile_kb_orphans(kb_dir)
        if swept or swept_dirs:
            print(f"[reconcile] swept orphans={swept} dirs={swept_dirs}")
        # Parse INDEX_KB.md for human-readable labels
        label_map: dict[str, str] = {}
        index_path = kb_dir / "INDEX_KB.md"
        if index_path.exists():
            for m in re.finditer(r"\[(.+?)\]\((.+?)\)", index_path.read_text(encoding="utf-8")):
                label_map[m.group(2)] = m.group(1)

        # Display-layer overrides: custom chapter labels + persisted note order.
        meta = _load_kb_meta()
        meta_chapters = meta.get("chapters", {})
        meta_order = meta.get("order", {})

        tree: list[dict] = []
        if kb_dir.exists():
            for d in sorted(kb_dir.iterdir()):
                if not d.is_dir() or not d.name[:2].isdigit():
                    continue
                parts = d.name.split("_", 1)
                derived = parts[0] + " " + parts[1].replace("_", " ").title() if len(parts) == 2 else d.name
                dir_label = (meta_chapters.get(d.name) or {}).get("label") or derived
                items: list[dict] = []
                for f in sorted(d.glob("*.html")):
                    is_draft = f.name.endswith(".draft.html")
                    rel_path = f"{d.name}/{f.name}"
                    label = label_map.get(rel_path)
                    if not label:
                        stem = f.stem.replace(".draft", "").replace("_tutorial", "").replace("_", " ").title()
                        label = stem
                    items.append({"label": label, "file": rel_path, "draft": is_draft})
                # Apply persisted order; files absent from it keep their alphabetical
                # position and trail the ordered ones (sort is stable).
                order = meta_order.get(d.name)
                if order:
                    pos = {rel: i for i, rel in enumerate(order)}
                    items.sort(key=lambda it: pos.get(it["file"], len(pos)))
                if items:
                    tree.append({"label": dir_label, "dir": d.name, "items": items})
        self._send_json(tree)

    def _handle_clear(self):
        """POST /api/clear — clear inbox/outbox to start a fresh conversation,
        but preserve files tied to still-running background tasks (generate /
        补充) so a mode switch doesn't destroy their in-flight events or queued
        work. A full reset uses /api/abort first, which empties _tasks."""
        with _tasks_lock:
            running_ids = {h.msg_id for h in _tasks.values()
                           if h.proc is not None and h.proc.poll() is None}
            # Also protect queued (not-yet-started) completions still in inbox.

        def _msg_doc(f: Path) -> dict:
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                return {}

        completion_modes = COMPLETION_MODES | APPEND_COMPLETION_MODES
        cleared_inbox = 0
        cleared_outbox = 0
        if INBOX.exists():
            for f in INBOX.glob("*.json"):
                doc = _msg_doc(f)
                # Keep a running task's source message AND any queued 补充 task
                # (completion modes serialize, so extras wait here for their turn).
                if doc.get("id") in running_ids or doc.get("mode") in completion_modes:
                    continue
                try:
                    f.unlink()
                    cleared_inbox += 1
                except Exception:
                    pass
        if OUTBOX.exists():
            for f in OUTBOX.glob("*.json"):
                if _msg_doc(f).get("reply_to") in running_ids:
                    continue  # in-flight events for a running task — keep
                try:
                    f.unlink()
                    cleared_outbox += 1
                except Exception:
                    pass
        print(f"[clear] Cleared {cleared_inbox} inbox + {cleared_outbox} outbox files "
              f"(preserved {len(running_ids)} running task(s))")
        self._send_json({
            "ok": True,
            "cleared_inbox": cleared_inbox,
            "cleared_outbox": cleared_outbox,
        })

    def _handle_abort(self):
        """POST /api/abort — terminate agent process(es).

        With {task_id}: abort only that task.
        Without body / empty: abort ALL running tasks.
        """
        task_id = None
        try:
            body = self._read_body()
            if body.strip():
                data = json.loads(body)
                task_id = data.get("task_id")
        except Exception:
            pass

        killed_ids: list[str] = []

        def _kill_handle(h: TaskHandle):
            h.abort.set()
            if h.proc and h.proc.poll() is None:
                h.proc.terminate()
                try:
                    h.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    h.proc.kill()
                    h.proc.wait()
                killed_ids.append(h.msg_id)

        with _tasks_lock:
            if task_id:
                h = _tasks.get(task_id)
                if h:
                    _kill_handle(h)
                else:
                    # Task not yet picked up by watcher — remove from inbox
                    if INBOX.exists():
                        for f in INBOX.glob("*.json"):
                            try:
                                msg = json.loads(f.read_text(encoding="utf-8"))
                                if msg.get("id") == task_id:
                                    f.unlink()
                                    killed_ids.append(task_id)
                                    print(f"[abort] Removed pending inbox message for {task_id}")
                                    break
                            except Exception:
                                pass
            else:
                _abort_flag.set()
                for h in list(_tasks.values()):
                    _kill_handle(h)
                # Also clear any pending inbox messages
                if INBOX.exists():
                    for f in INBOX.glob("*.json"):
                        try:
                            f.unlink()
                        except Exception:
                            pass

        # Clean up in-progress outbox events for aborted tasks only
        aborted_set = set(killed_ids) if task_id else None
        if OUTBOX.exists():
            for f in OUTBOX.glob("*.json"):
                try:
                    msg = json.loads(f.read_text(encoding="utf-8"))
                    if msg.get("status") in ("running", "started", "milestone"):
                        if aborted_set is None or msg.get("reply_to") in aborted_set:
                            f.unlink()
                except Exception:
                    pass
        print(f"[abort] Terminated tasks: {killed_ids or 'none'} (scope={'single:'+task_id if task_id else 'all'})")
        self._send_json({"ok": True, "killed": killed_ids})

    def _resolve_kb_file(self, rel: str):
        """Validate a knowledge-relative path and return its resolved Path.

        Returns (path, None) on success or (None, error_dict) on failure.
        Guards against absolute paths, traversal (..), and non-html/md files.
        """
        kb_dir = (WORKSPACE / "knowledge").resolve()
        rel = (rel or "").strip().lstrip("/")
        if not rel:
            return None, {"error": "Missing file"}
        target = (kb_dir / rel).resolve()
        # Containment check: target must live strictly inside knowledge/.
        if target == kb_dir or kb_dir not in target.parents:
            return None, {"error": "Invalid path"}
        if target.suffix.lower() not in (".html", ".md"):
            return None, {"error": "Only .html or .md notes can be modified"}
        # Never let INDEX_KB itself be deleted/renamed through this API.
        if target.name in ("INDEX_KB.md", "INDEX_KB.html"):
            return None, {"error": "INDEX_KB is protected"}
        return target, None

    def _resolve_kb_dir(self, name: str):
        """Validate a chapter directory name (direct child of knowledge/, NN_ prefix).

        Returns (path, None) on success or (None, error_dict) on failure.
        """
        kb_dir = (WORKSPACE / "knowledge").resolve()
        name = (name or "").strip().strip("/")
        if not name:
            return None, {"error": "Missing dir"}
        target = (kb_dir / name).resolve()
        if target == kb_dir or target.parent != kb_dir:
            return None, {"error": "Invalid path"}
        if not target.is_dir():
            return None, {"error": "Chapter not found"}
        if not target.name[:2].isdigit():
            return None, {"error": "Not a chapter directory"}
        return target, None

    @staticmethod
    def _rename_index_header(text: str, dir_name: str, new_label: str) -> tuple[str, bool]:
        """Rename the `### ` header that owns the items under dir_name/ in INDEX_KB.md."""
        lines = text.split("\n")
        cur_header = -1
        header_idx = -1
        for i, line in enumerate(lines):
            if line.lstrip().startswith("### "):
                cur_header = i
            m = re.match(r"\s*-\s*\[.*?\]\((.+?)\)", line)
            if m and m.group(1).strip().startswith(dir_name + "/"):
                header_idx = cur_header
                break
        if header_idx < 0:
            return text, False
        raw = lines[header_idx]
        indent = raw[:len(raw) - len(raw.lstrip())]
        lines[header_idx] = indent + "### " + new_label
        return "\n".join(lines), True

    @staticmethod
    def _reorder_index_section(text: str, dir_name: str, order: list[str]) -> tuple[str, bool]:
        """Reorder the `- [..](..)` item lines belonging to dir_name to match `order`.

        Only the item-line slots are rewritten; headers, blanks and other sections
        keep their positions.
        """
        lines = text.split("\n")
        slots: list[int] = []
        line_by_path: dict[str, str] = {}
        for i, line in enumerate(lines):
            m = re.match(r"\s*-\s*\[.*?\]\((.+?)\)", line)
            if m and m.group(1).strip().startswith(dir_name + "/"):
                slots.append(i)
                line_by_path[m.group(1).strip()] = line
        if not slots:
            return text, False
        ordered = [p for p in order if p in line_by_path]
        for p in line_by_path:                 # unlisted-but-belonging → keep, trail
            if p not in ordered:
                ordered.append(p)
        for slot_i, line_i in enumerate(slots):
            lines[line_i] = line_by_path[ordered[slot_i]]
        return "\n".join(lines), True

    @staticmethod
    def _move_index_entry(text: str, old_rel: str, new_rel: str, to_dir: str) -> tuple[str, bool]:
        """Rewrite an item's path old_rel→new_rel and relocate it under to_dir's section."""
        lines = text.split("\n")
        moved_line = None
        kept: list[str] = []
        for line in lines:
            m = re.match(r"(\s*-\s*\[.*?\]\()(.+?)(\).*)$", line)
            if m and m.group(2).strip() == old_rel:
                moved_line = m.group(1) + new_rel + m.group(3)
                continue
            kept.append(line)
        if moved_line is None:
            return text, False
        insert_at = -1
        for i, line in enumerate(kept):
            m = re.match(r"\s*-\s*\[.*?\]\((.+?)\)", line)
            if m and m.group(1).strip().startswith(to_dir + "/"):
                insert_at = i  # after the last existing item of the target section
        if insert_at >= 0:
            kept.insert(insert_at + 1, moved_line)
        else:
            kept.append(moved_line)  # target section empty/unknown → append, link stays alive
        return "\n".join(kept), True

    @staticmethod
    def _rewrite_note_refs(kb_dir: Path, old_rel: str, new_rel: str, skip: Path) -> int:
        """Best-effort: replace full-path references to old_rel in sibling notes."""
        if old_rel == new_rel:
            return 0
        count = 0
        for f in kb_dir.rglob("*"):
            if f.suffix.lower() not in (".html", ".md"):
                continue
            if f == skip or f.name in ("INDEX_KB.md", "INDEX_KB.html"):
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            if old_rel in content:
                f.write_text(content.replace(old_rel, new_rel), encoding="utf-8")
                count += 1
        return count

    @staticmethod
    def _strip_index_entry(text: str, rel_path: str) -> tuple[str, bool]:
        """Remove the markdown link line(s) referencing rel_path from INDEX_KB.md.

        Also drops a now-empty `### Section` header whose only following list
        items were the removed entry. Returns (new_text, removed_any).
        """
        lines = text.split("\n")
        kept: list[str] = []
        removed = False
        for line in lines:
            # Match a list item linking to this exact path, e.g.
            #   - [Label](03_architecture/moe_tutorial.html) · 2026-06-05
            m = re.match(r"\s*-\s*\[.*?\]\((.+?)\)", line)
            if m and m.group(1).strip() == rel_path:
                removed = True
                continue
            kept.append(line)

        if not removed:
            return text, False

        # Second pass: drop `###` headers left with no list items beneath them.
        result: list[str] = []
        for i, line in enumerate(kept):
            if line.lstrip().startswith("### "):
                has_item = False
                for nxt in kept[i + 1:]:
                    s = nxt.strip()
                    if not s:
                        continue
                    if s.startswith("#") or s.startswith("---"):
                        break  # reached next section without finding an item
                    if s.startswith("- "):
                        has_item = True
                    break
                if not has_item:
                    continue  # skip the orphaned header
            result.append(line)
        return "\n".join(result), True

    def _handle_delete_note(self):
        """DELETE /api/knowledge-note — remove a note file and its INDEX_KB.md entry."""
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, Exception) as e:
            self._send_json({"error": str(e)}, status=400)
            return

        rel = (data.get("file") or "").strip().lstrip("/")
        target, err = self._resolve_kb_file(rel)
        if err:
            status = 403 if err.get("error") in ("Invalid path", "INDEX_KB is protected") else 400
            self._send_json(err, status=status)
            return
        if not target.exists():
            self._send_json({"error": "File not found"}, status=404)
            return

        # A note is an .html *view* paired with same-stem companions in the same
        # chapter: the .md source, a .draft.md, and a <stem>.sources/ folder of
        # raw retrieved material. Deleting just the .html orphans the rest, so
        # sweep them all out together. Derive the base stem by stripping the
        # note's own suffix (.html / .md) so "foo.html" → "foo".
        stem = target.stem
        parent = target.parent
        companions = [
            parent / f"{stem}.html",
            parent / f"{stem}.md",
            parent / f"{stem}.draft.md",
            parent / f"{stem}.sources",
        ]

        with _index_lock:
            removed = []
            try:
                for c in companions:
                    if not c.exists():
                        continue
                    if c.is_dir():
                        shutil.rmtree(c)
                    else:
                        c.unlink()
                    removed.append(c.name)
            except OSError as e:
                self._send_json({"error": f"Delete failed: {e}"}, status=500)
                return

            index_updated = False
            index_path = (WORKSPACE / "knowledge" / "INDEX_KB.md")
            if index_path.exists():
                new_text, index_updated = self._strip_index_entry(
                    index_path.read_text(encoding="utf-8"), rel)
                if index_updated:
                    index_path.write_text(new_text, encoding="utf-8")

            # Remove the chapter directory if it is now empty (ignore .DS_Store).
            dir_removed = False
            try:
                if parent != (WORKSPACE / "knowledge").resolve():
                    leftovers = [p for p in parent.iterdir() if p.name != ".DS_Store"]
                    if not leftovers:
                        for junk in parent.iterdir():
                            junk.unlink()
                        parent.rmdir()
                        dir_removed = True
            except OSError:
                pass

        print(f"[delete] Removed note {rel} (files={removed}, index_updated={index_updated}, dir_removed={dir_removed})")
        self._send_json({
            "ok": True,
            "deleted": rel,
            "removed": removed,
            "index_updated": index_updated,
            "dir_removed": dir_removed,
        })

    def _handle_completion_withdraw(self):
        """POST /api/completion-withdraw — restore backups for append-mode rollback.

        Append-mode completion edits the original .md/.html in place after leaving
        a sibling .bak of each. Withdrawing copies every .bak back over its original
        and removes the .bak. Paths must live inside knowledge/ (KB append rollback)
        or skills/ (元 skill modify rollback).
        """
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, Exception) as e:
            self._send_json({"error": str(e)}, status=400)
            return

        if data.get("mode") != "restore_backup":
            self._send_json({"error": "Unknown mode"}, status=400)
            return

        backups = data.get("backup_files") or []
        # Legal roots: knowledge/ (KB append rollback) and skills/ (元 skill modify
        # rollback). skills/ is *appended* as an allowed root — the knowledge/ rule
        # and every other check (.bak suffix, existence) stay exactly as before, so
        # KB-append withdraw behaviour is unchanged.
        kb_dir = (WORKSPACE / "knowledge").resolve()
        skills_dir = (WORKSPACE / "skills").resolve()
        allowed_roots = (kb_dir, skills_dir)
        restored: list[str] = []
        errors: list[str] = []

        for bak_rel in backups:
            bak_path = (WORKSPACE / bak_rel).resolve()
            if not any(root in bak_path.parents for root in allowed_roots):
                errors.append(f"Invalid backup path: {bak_rel}")
                continue
            if not bak_path.name.endswith(".bak"):
                errors.append(f"Not a backup file: {bak_rel}")
                continue
            if not bak_path.exists():
                errors.append(f"Backup not found: {bak_rel}")
                continue
            orig_path = bak_path.parent / bak_path.name[:-4]  # strip ".bak"
            try:
                shutil.copy2(str(bak_path), str(orig_path))
                bak_path.unlink()
                restored.append(str(orig_path.relative_to(WORKSPACE)))
            except OSError as e:
                errors.append(f"Restore failed for {bak_rel}: {e}")

        print(f"[withdraw] restored={restored} errors={errors}")
        self._send_json({
            "ok": len(errors) == 0 and len(restored) > 0,
            "restored": restored,
            "errors": errors,
        })

    def _handle_rename_label(self):
        """POST /api/knowledge-note/rename — change a note's label in INDEX_KB.md."""
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, Exception) as e:
            self._send_json({"error": str(e)}, status=400)
            return

        rel = (data.get("file") or "").strip().lstrip("/")
        new_label = (data.get("new_label") or "").strip()
        if not new_label:
            self._send_json({"error": "Missing new_label"}, status=400)
            return
        target, err = self._resolve_kb_file(rel)
        if err:
            status = 403 if err.get("error") in ("Invalid path", "INDEX_KB is protected") else 400
            self._send_json(err, status=status)
            return

        with _index_lock:
            index_path = (WORKSPACE / "knowledge" / "INDEX_KB.md")
            if not index_path.exists():
                self._send_json({"error": "INDEX_KB.md not found"}, status=404)
                return
            lines = index_path.read_text(encoding="utf-8").split("\n")
            updated = False
            for i, line in enumerate(lines):
                m = re.match(r"(\s*-\s*\[)(.*?)(\]\()(.+?)(\).*)$", line)
                if m and m.group(4).strip() == rel:
                    lines[i] = m.group(1) + new_label + m.group(3) + m.group(4) + m.group(5)
                    updated = True
                    break
            if not updated:
                self._send_json({"error": "Note not found in INDEX_KB.md"}, status=404)
                return
            index_path.write_text("\n".join(lines), encoding="utf-8")

        print(f"[rename] {rel} → label '{new_label}'")
        self._send_json({"ok": True, "file": rel, "new_label": new_label})

    def _handle_chapter_rename(self):
        """POST /api/knowledge-chapter/rename — change a chapter's display label.

        Stores the override in .kb_meta.json (directory name is never touched, so no
        rel_path changes) and syncs the matching `### ` header in INDEX_KB.md.
        """
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, Exception) as e:
            self._send_json({"error": str(e)}, status=400)
            return

        dir_name = (data.get("dir") or "").strip().strip("/")
        new_label = (data.get("new_label") or "").strip()
        if not new_label:
            self._send_json({"error": "Missing new_label"}, status=400)
            return
        _, err = self._resolve_kb_dir(dir_name)
        if err:
            status = 403 if err.get("error") == "Invalid path" else 400
            self._send_json(err, status=status)
            return

        with _index_lock:
            meta = _load_kb_meta()
            meta.setdefault("chapters", {}).setdefault(dir_name, {})["label"] = new_label
            _save_kb_meta(meta)

            index_header_updated = False
            index_path = WORKSPACE / "knowledge" / "INDEX_KB.md"
            if index_path.exists():
                new_text, index_header_updated = self._rename_index_header(
                    index_path.read_text(encoding="utf-8"), dir_name, new_label)
                if index_header_updated:
                    index_path.write_text(new_text, encoding="utf-8")

        print(f"[chapter-rename] {dir_name} → '{new_label}' (header={index_header_updated})")
        self._send_json({"ok": True, "dir": dir_name, "new_label": new_label,
                         "index_header_updated": index_header_updated})

    def _handle_note_reorder(self):
        """POST /api/knowledge-note/reorder — persist note order within one chapter."""
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, Exception) as e:
            self._send_json({"error": str(e)}, status=400)
            return

        dir_name = (data.get("dir") or "").strip().strip("/")
        order = data.get("order")
        if not isinstance(order, list) or not order:
            self._send_json({"error": "Missing order"}, status=400)
            return
        target, err = self._resolve_kb_dir(dir_name)
        if err:
            status = 403 if err.get("error") == "Invalid path" else 400
            self._send_json(err, status=status)
            return

        clean: list[str] = []
        for rel in order:
            rel = (rel or "").strip().lstrip("/")
            fp, ferr = self._resolve_kb_file(rel)
            if ferr or fp.parent != target or not fp.exists():
                self._send_json({"error": f"Invalid entry: {rel}"}, status=400)
                return
            clean.append(rel)

        with _index_lock:
            meta = _load_kb_meta()
            meta.setdefault("order", {})[dir_name] = clean
            _save_kb_meta(meta)

            index_path = WORKSPACE / "knowledge" / "INDEX_KB.md"
            if index_path.exists():
                new_text, _ = self._reorder_index_section(
                    index_path.read_text(encoding="utf-8"), dir_name, clean)
                index_path.write_text(new_text, encoding="utf-8")

        print(f"[reorder] {dir_name}: {len(clean)} items")
        self._send_json({"ok": True, "dir": dir_name, "order": clean})

    def _handle_note_move(self):
        """POST /api/knowledge-note/move — move a note to another chapter.

        The one operation that genuinely changes a note's rel_path, so it must
        keep every reference in sync: physical file, INDEX_KB.md (path + section),
        .kb_meta.json order, and (best-effort) sibling-note links.
        """
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, Exception) as e:
            self._send_json({"error": str(e)}, status=400)
            return

        rel = (data.get("file") or "").strip().lstrip("/")
        to_dir = (data.get("to_dir") or "").strip().strip("/")
        index = data.get("index")
        rewrite_refs = data.get("rewrite_refs", True)

        src, err = self._resolve_kb_file(rel)
        if err:
            status = 403 if err.get("error") in ("Invalid path", "INDEX_KB is protected") else 400
            self._send_json(err, status=status)
            return
        if not src.exists():
            self._send_json({"error": "File not found"}, status=404)
            return
        dst_dir, derr = self._resolve_kb_dir(to_dir)
        if derr:
            status = 403 if derr.get("error") == "Invalid path" else 400
            self._send_json(derr, status=status)
            return

        old_dir = rel.split("/")[0]
        if to_dir == old_dir:
            self._send_json({"error": "Source and target chapter are the same"}, status=400)
            return

        filename = src.name
        new_rel = f"{to_dir}/{filename}"
        dst = dst_dir / filename
        if dst.exists():
            self._send_json({"error": "目标章节已存在同名文件"}, status=409)
            return

        # A note is an .html *view* paired with an .md *source* of the same stem.
        # Move both together so the source isn't orphaned in the old chapter.
        src_md = src.with_suffix(".md") if src.suffix.lower() == ".html" else None
        has_md = bool(src_md and src_md.exists())
        dst_md = dst_dir / src_md.name if has_md else None
        rel_md = f"{old_dir}/{src_md.name}" if has_md else None
        new_rel_md = f"{to_dir}/{src_md.name}" if has_md else None
        if has_md and dst_md.exists():
            self._send_json({"error": "目标章节已存在同名 .md 源文件"}, status=409)
            return

        kb_dir = (WORKSPACE / "knowledge").resolve()
        with _index_lock:
            try:
                shutil.move(str(src), str(dst))
            except OSError as e:
                self._send_json({"error": f"Move failed: {e}"}, status=500)
                return

            md_moved = False
            if has_md:
                try:
                    shutil.move(str(src_md), str(dst_md))
                    md_moved = True
                except OSError as e:
                    shutil.move(str(dst), str(src))  # roll back html so the pair stays together
                    self._send_json({"error": f"Move failed (md): {e}"}, status=500)
                    return

            index_updated = False
            index_path = WORKSPACE / "knowledge" / "INDEX_KB.md"
            if index_path.exists():
                new_text, index_updated = self._move_index_entry(
                    index_path.read_text(encoding="utf-8"), rel, new_rel, to_dir)
                if index_updated:
                    index_path.write_text(new_text, encoding="utf-8")

            # .kb_meta.json order: drop from old chapter, insert into new at `index`.
            meta = _load_kb_meta()
            order_map = meta.setdefault("order", {})
            if old_dir in order_map:
                order_map[old_dir] = [p for p in order_map[old_dir] if p != rel]
            target_order = order_map.get(to_dir)
            if target_order is None:
                # Materialize current target order (dst already moved in) so the
                # insertion index is meaningful, then re-place the moved file.
                target_order = [f"{to_dir}/{p.name}" for p in sorted(dst_dir.glob("*.html"))]
            target_order = [p for p in target_order if p != new_rel]
            try:
                idx = int(index)
            except (TypeError, ValueError):
                idx = len(target_order)
            idx = max(0, min(idx, len(target_order)))
            target_order.insert(idx, new_rel)
            order_map[to_dir] = target_order
            _save_kb_meta(meta)

            refs_rewritten = 0
            if rewrite_refs:
                refs_rewritten = self._rewrite_note_refs(kb_dir, rel, new_rel, dst)
                if has_md:
                    refs_rewritten += self._rewrite_note_refs(kb_dir, rel_md, new_rel_md, dst_md)

            # Remove the old chapter dir if it is now empty (ignore .DS_Store).
            dir_removed = False
            old_parent = WORKSPACE / "knowledge" / old_dir
            try:
                if old_parent.is_dir():
                    leftovers = [p for p in old_parent.iterdir() if p.name != ".DS_Store"]
                    if not leftovers:
                        for junk in old_parent.iterdir():
                            junk.unlink()
                        old_parent.rmdir()
                        dir_removed = True
            except OSError:
                pass

        print(f"[move] {rel} → {new_rel} (md_moved={md_moved}, index={index_updated}, refs={refs_rewritten}, dir_removed={dir_removed})")
        self._send_json({"ok": True, "from": rel, "to": new_rel,
                         "md_moved": md_moved,
                         "index_updated": index_updated, "refs_rewritten": refs_rewritten,
                         "dir_removed": dir_removed})

    # --- Helpers ---

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("utf-8")

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        # Quieter logging: only log API calls, not static file requests.
        # NOTE: log_error() routes here too with args=(code, message) where
        # args[0] is an HTTPStatus (no .split). Guard for that so a 404 on a
        # static asset (e.g. /favicon.ico) can't raise inside the handler.
        first = args[0] if args else ""
        parts = first.split() if isinstance(first, str) else []
        path = parts[1] if len(parts) > 1 else ""
        if path.startswith("/api/"):
            super().log_message(format, *args)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_history_file(conv_id: str, data: dict):
    """Atomically write a conversation-history JSON file (tmp + rename)."""
    target = HISTORY_DIR / f"{conv_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(target)


def _parse_multipart(body: bytes, boundary: bytes) -> tuple[str | None, bytes | None]:
    parts = body.split(b"--" + boundary)
    for part in parts:
        if b"filename=" not in part:
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        header = part[:header_end].decode("utf-8", errors="replace")
        # Extract only the Content-Disposition line for parsing
        for header_line in header.split("\r\n"):
            if "filename=" not in header_line:
                continue
            for segment in header_line.split(";"):
                segment = segment.strip()
                if segment.startswith("filename="):
                    filename = segment.split("=", 1)[1].strip('"')
                    file_data = part[header_end + 4:]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]
                    return filename, file_data
    return None, None


def _write_outbox(msg_id: str, reply_to: str | None, msg_type: str,
                   content: str, status: str, elapsed: float | None = None,
                   meta: dict | None = None, raw_output: str | None = None) -> str:
    """Write a single JSON message to the outbox and return its ID."""
    resp_id = f"resp_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    resp: dict = {
        "id": resp_id,
        "reply_to": reply_to,
        "timestamp": _now_iso(),
        "type": msg_type,
        "content": content,
        "status": status,
    }
    if elapsed is not None:
        resp["elapsed"] = round(elapsed, 1)
    if meta is not None:
        resp["meta"] = meta
    if raw_output is not None:
        resp["raw_output"] = raw_output
    (OUTBOX / f"{resp_id}.json").write_text(
        json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return resp_id


def _build_clean_env() -> dict[str, str]:
    clean_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE_CODE_") and k not in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL", "CLAUDE_AGENT_SDK_VERSION",
        )
    }
    clean_env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    npm_bin = str(Path.home() / ".npm-global" / "bin")
    if npm_bin not in clean_env.get("PATH", ""):
        clean_env["PATH"] = clean_env.get("PATH", "") + os.pathsep + npm_bin
    return clean_env


def _annotate_agent_error(agent_name: str, output: str) -> str:
    """Prepend an actionable hint when claude failed on authentication.

    The raw 401/402 message is opaque to end users. A 402 in particular comes
    from the API provider's billing layer (e.g. an expired Kimi membership),
    not from our code — so we point the user at their account/credentials.
    """
    low = output.lower()
    if agent_name == "claude" and ("402" in low or "membership" in low):
        hint = (
            "⚠️ 认证失败：API 服务商在计费层拒绝了你的 token（402）——"
            "对应的会员/订阅未激活或已过期。这是账号层面的问题，代码无法绕过。"
            "请到服务商处续费/更换 token，并更新 ~/.claude/settings.json 里的凭证后重试。\n\n"
            "--- raw error ---\n"
        )
        return hint + output
    return output


def _snapshot_knowledge_files() -> set[str]:
    """Return the set of knowledge/ file paths (relative to WORKSPACE) at this moment."""
    kb_dir = WORKSPACE / "knowledge"
    if not kb_dir.exists():
        return set()
    result = set()
    for f in kb_dir.rglob("*"):
        if f.is_file():
            result.add(str(f.relative_to(WORKSPACE)))
    return result


def _post_completion_scan(msg_id: str, start_time: float,
                          snapshot: set[str] | None = None) -> dict:
    """Scan workspace for changes made by the Agent.

    When snapshot is provided (parallel mode), uses set-diff to attribute only
    files that appeared after the task started. Falls back to mtime when no
    snapshot is given (single-task compat).
    """
    result: dict = {
        "new_files": [],
        "updated_files": [],
        "new_chapters": [],
        "warnings": [],
    }
    kb_dir = WORKSPACE / "knowledge"
    if not kb_dir.exists():
        return result

    current_files = _snapshot_knowledge_files()

    if snapshot is not None:
        new_on_disk = current_files - snapshot
        for rel in sorted(new_on_disk):
            if _is_sources_path(rel):
                continue
            p = Path(WORKSPACE / rel)
            if p.name == "INDEX_KB.md":
                result["updated_files"].append(rel)
            elif p.suffix in (".html", ".md"):
                result["new_files"].append(rel)
    else:
        for f in kb_dir.rglob("*"):
            if f.is_file() and f.stat().st_mtime > start_time:
                rel = str(f.relative_to(WORKSPACE))
                if _is_sources_path(rel):
                    continue
                if f.name == "INDEX_KB.md":
                    result["updated_files"].append(rel)
                elif f.suffix in (".html", ".md"):
                    result["new_files"].append(rel)

    for d in sorted(kb_dir.iterdir()):
        if d.is_dir() and d.name[:2].isdigit():
            if snapshot is not None:
                if any(str(f.relative_to(WORKSPACE)) in (current_files - snapshot)
                       for f in d.rglob("*") if f.is_file()):
                    result["new_chapters"].append(d.name)
            else:
                if any(f.stat().st_mtime > start_time for f in d.rglob("*")
                       if f.is_file() and not _is_sources_path(str(f.relative_to(WORKSPACE)))):
                    result["new_chapters"].append(d.name)

    index_path = kb_dir / "INDEX_KB.md"
    index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    for f in kb_dir.rglob("*.draft.html"):
        if _is_sources_path(str(f.relative_to(WORKSPACE))):
            continue
        rel = str(f.relative_to(kb_dir))
        if rel not in index_text:
            result["warnings"].append(f"Draft file {rel} not registered in INDEX_KB.md")

    return result


def _extract_final_answer(raw_output: str) -> str:
    """Extract the Agent's own final summary from raw output."""
    lines = raw_output.split("\n")
    summary_lines: list[str] = []
    collecting = False
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped and not collecting:
            continue
        if stripped.startswith(("/bin/", "diff --git", "+++", "---", "@@", "exec", "succeeded")):
            if collecting:
                break
            continue
        collecting = True
        summary_lines.append(stripped)
        if len(summary_lines) >= 10:
            break
    summary_lines.reverse()
    return "\n".join(summary_lines).strip()


def _extract_codex_reply(raw_output: str) -> str:
    """Pull just the assistant reply out of `codex exec` stdout.

    codex prints a banner (`OpenAI Codex vX`, `workdir:`, `model:`, `session id:`
    …), then echoes our full prompt after a lone `user` line, then the reply
    after a lone `codex` line, then a `tokens used` footer (and sometimes repeats
    the final message). For the lightweight qa/interview chat bubble we want ONLY
    the reply — none of that plumbing. We take the text between the last `codex`
    marker and the `tokens used` footer. Falls back to the raw text if the
    markers aren't present (format drift), so we never lose the answer entirely.
    """
    if not raw_output:
        return ""
    lines = raw_output.split("\n")
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "codex":
            start = i + 1  # last 'codex' marker wins
    if start is None:
        return raw_output.strip()
    reply: list[str] = []
    for ln in lines[start:]:
        s = ln.lstrip()
        if ln.strip() == "tokens used":
            break
        # codex shows file-write tool output (a git diff / apply_patch block)
        # AFTER its prose reply. The assistant's message is everything up to that
        # first patch marker — drop the diff so it can't leak into the chat bubble
        # (e.g. the interview notes file write showing up as `diff --git …`).
        if s.startswith("diff --git ") or s.startswith("*** Begin Patch"):
            break
        reply.append(ln)
    cleaned = "\n".join(reply).strip()
    if cleaned:
        return cleaned
    # Truncation removed everything (diff appeared before any prose) → fall back
    # to the full between-markers text so we never drop the answer entirely.
    fallback: list[str] = []
    for ln in lines[start:]:
        if ln.strip() == "tokens used":
            break
        fallback.append(ln)
    return "\n".join(fallback).strip() or raw_output.strip()


def _extract_agent_summary(raw_output: str, scan_result: dict) -> str:
    """Build a user-friendly summary from raw agent output and scan results."""
    sections: list[str] = []

    if scan_result["new_chapters"]:
        sections.append("## New Chapters Created")
        for ch in scan_result["new_chapters"]:
            sections.append(f"- `knowledge/{ch}/`")

    if scan_result["new_files"]:
        html_files = [f for f in scan_result["new_files"] if f.endswith(".html")]
        md_files = [f for f in scan_result["new_files"] if f.endswith(".md")]
        sections.append("## Files Generated")
        if html_files:
            sections.append(f"- {len(html_files)} HTML draft(s)")
            for f in html_files:
                name = Path(f).stem.replace(".draft", "").replace("_", " ").title()
                sections.append(f"  - {name}")
        if md_files:
            sections.append(f"- {len(md_files)} Markdown source(s)")

    if scan_result["updated_files"]:
        sections.append("## Updated")
        for f in scan_result["updated_files"]:
            sections.append(f"- `{f}`")

    agent_final = _extract_final_answer(raw_output)
    if agent_final:
        if sections:
            sections.append("## Agent Summary")
        sections.append(agent_final)

    if scan_result["warnings"]:
        sections.append("## Warnings")
        for w in scan_result["warnings"]:
            sections.append(f"- {w}")

    return "\n".join(sections) if sections else "(Agent completed with no detectable output)"


# ── Chat-mode (baguwen / interview / completion) support ──
# These power the lightweight, multi-turn conversational modes layered on top of
# the heavy "generate" research pipeline. Everything here is additive: the
# generate path never calls into it, so its behaviour is unchanged.

BAGUWEN_MODE = "baguwen"
BAGUWEN_START_TOKEN = "__BAGUWEN_START__"
BAGUWEN_DIR = AGENT_DIR / "baguwen"

INTERVIEW_MODE = "interview"
INTERVIEW_START_TOKEN = "__INTERVIEW_START__"
INTERVIEW_DIR = AGENT_DIR / "interview"

# 元 skill (Yuan Skill) mode — an interactive workflow for creating/modifying
# skills under skills/. It reuses the inline interactive reply path (see
# INTERACTIVE_MODES) but has its own prompt builder so it never inherits the
# knowledge-base ("完善知识库 / 原始资料留存") framing of the generate fallback.
YUAN_MODE = "yuan"
YUAN_START_TOKEN = "__YUAN_START__"

# personalize (generate mode) resume-text cache — server pre-extracts PDF text
# here so the Agent never invokes pdftotext itself (no tmp/pdfs/ byproducts).
PERSONALIZE_DIR = AGENT_DIR / "personalize"

INTERACTIVE_MODES = {BAGUWEN_MODE, INTERVIEW_MODE, YUAN_MODE}
AGENT_MODE = "agent"
COMPLETION_MODES = {"baguwen_complete", "interview_kb_complete", "agent_complete"}
APPEND_COMPLETION_MODES = {"baguwen_append_complete", "interview_kb_append_complete", "agent_append_complete"}


# ── Transient process-file cleanup (age-based; never touches knowledge/) ──
# All thresholds in hours; None disables that category. See CLEANUP_AND_ABORT_PLAN.md.
CLEANUP_POLICY = {
    "baguwen_notes":   24 * 7,   # baguwen blind-spot notes: 7 days
    "interview_notes": 24 * 7,   # interview state/notes: 7 days
    "personalize":     24 * 7,   # personalize resume-text cache: 7 days
    "outbox":          24,       # outbox receipts: 1 day backstop
    "inbox":           6,        # stale inbox: 6h (guards against restart replay)
    "uploads":         24 * 7,   # uploaded PDFs: 7 days (user data, conservative)
    "tmp":             24 * 3,   # tmp scratch files: 3 days
}


def _purge_old(dir_path: Path, pattern: str, max_age_h: float | None) -> int:
    """Delete files under dir_path matching pattern whose mtime is older than
    max_age_h. Returns the number deleted. Age-based only, so actively
    written/read files (fresh mtime) are naturally skipped."""
    if max_age_h is None or not dir_path.exists():
        return 0
    cutoff = time.time() - max_age_h * 3600
    n = 0
    for f in dir_path.glob(pattern):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def cleanup_transient_files() -> None:
    """Purge transient process files by age. Never touches knowledge/ content."""
    p = CLEANUP_POLICY
    removed = {
        "baguwen":   _purge_old(BAGUWEN_DIR,   "notes_*.md", p["baguwen_notes"]),
        "interview": _purge_old(INTERVIEW_DIR,  "*",          p["interview_notes"]),
        "personalize": _purge_old(PERSONALIZE_DIR, "*",       p["personalize"]),
        "outbox":    _purge_old(OUTBOX,         "*.json",     p["outbox"]),
        "inbox":     _purge_old(INBOX,          "*.json",     p["inbox"]),
        "uploads":   _purge_old(UPLOADS,        "*",          p["uploads"]),
        "tmp":       _purge_old(WORKSPACE / "tmp" / "pdfs", "*", p["tmp"]),
    }
    total = sum(removed.values())
    if total:
        print(f"[cleanup] removed {total} stale files: {removed}")


def _chapter_parts(current_file: str | None) -> tuple[str, str] | None:
    """Split 'NN_dir/stem[.draft].html' → ('NN_dir', 'stem'). None if unusable."""
    if not current_file or "/" not in current_file:
        return None
    chapter_dir, fname = current_file.split("/", 1)
    stem = fname
    for suffix in (".draft.html", ".html", ".md"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if stem.endswith(".supplement"):
        stem = stem[: -len(".supplement")]
    return chapter_dir, stem


def _append_targets(current_file: str | None) -> tuple[str, str, str] | None:
    """Resolve the real (chapter_dir, md_rel, html_rel) for an append-completion.

    html_rel is *exactly* the note the index/UI references (= current_file), so
    re-rendering writes back into the registered file instead of a guessed bare
    stem (which produced an orphan, unregistered .html — see draft notes). md_rel
    is the markdown source backing it, mirroring the html's draft-ness with a
    fallback to the other naming. All paths are workspace-relative.
    """
    if not current_file or "/" not in current_file:
        return None
    chapter_dir, fname = current_file.split("/", 1)
    kb = WORKSPACE / "knowledge" / chapter_dir
    html_rel = f"knowledge/{current_file}"
    if fname.endswith(".draft.html"):
        base = fname[: -len(".draft.html")]
        md_cands = [f"{base}.draft.md", f"{base}.md"]
    elif fname.endswith(".html"):
        base = fname[: -len(".html")]
        md_cands = [f"{base}.md", f"{base}.draft.md"]
    else:
        base = fname.rsplit(".", 1)[0] if "." in fname else fname
        md_cands = [f"{base}.md"]
    md_name = next((c for c in md_cands if (kb / c).exists()), md_cands[0])
    return chapter_dir, f"knowledge/{chapter_dir}/{md_name}", html_rel


def _resolve_chapter_file(current_file: str | None) -> Path | None:
    """Resolve a nav path (…/foo.html) to the actual source we should read.

    Always prefers the markdown source over the rendered HTML — the .md is the
    authoring source (smaller, no HTML/CSS boilerplate), so injecting it is both
    more accurate and more token-efficient for qa/interview prompts.
    """
    parts = _chapter_parts(current_file)
    if not parts:
        return None
    chapter_dir, stem = parts
    kb = WORKSPACE / "knowledge" / chapter_dir
    for cand in (kb / f"{stem}.md", kb / f"{stem}.html", kb / f"{stem}.draft.html"):
        if cand.exists():
            return cand
    return None


def _sources_dir(current_file: str | None) -> Path | None:
    """Return the note's raw-sources folder (`<stem>.sources/`) if it exists.

    Web-retrieved originals are saved here at generate time; qa/completion prompts
    point the agent at it so it grounds answers in local source text (anti-
    hallucination) instead of re-searching the web. Returns None for notes that
    predate the feature → callers degrade to today's behavior.
    """
    parts = _chapter_parts(current_file)
    if not parts:
        return None
    chapter_dir, stem = parts
    d = WORKSPACE / "knowledge" / chapter_dir / f"{stem}.sources"
    return d if d.is_dir() else None


def _chapter_label(current_file: str | None) -> str:
    """Human label for the chapter that names the file we actually read (.md)."""
    src = _resolve_chapter_file(current_file)
    if src:
        try:
            return str(src.relative_to(WORKSPACE / "knowledge"))
        except ValueError:
            return src.name
    return current_file or "（未指定章节）"


def _read_chapter_source(current_file: str | None, max_chars: int = 12000) -> str:
    """Return the chapter's text for prompt injection (prefers .md, truncated).

    qa/interview answer 'based on the current chapter', so we feed the markdown
    source (richer than rendered HTML) up to a token-safe budget. Returns '' when
    nothing readable is found — callers decide how to degrade.
    """
    cand = _resolve_chapter_file(current_file)
    if cand:
        try:
            text = cand.read_text(encoding="utf-8")
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n…（正文已截断，仅展示前部分）…"
            return text
        except Exception:
            return ""
    return ""


def _format_history(history: list) -> str:
    lines: list[str] = []
    for h in history or []:
        if not isinstance(h, dict):
            continue
        text = (h.get("text") or "").strip()
        if not text:
            continue
        who = "用户" if h.get("role") == "user" else "助手"
        lines.append(f"{who}：{text}")
    return "\n".join(lines)


def _baguwen_notes_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "session")
    return BAGUWEN_DIR / f"notes_{safe}.md"


def _claude_cmd_for_mode(mode: str) -> list[str]:
    """Base claude argv with per-mode tool restrictions.

    --disallowedTools is variadic, so it must be followed by the single-valued
    --permission-mode (which terminates the list) and never sit last — same rule
    the AGENT_COMMANDS comment documents.
    """
    base = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    tail = ["--permission-mode", "bypassPermissions"]
    if mode == BAGUWEN_MODE:
        # Write stays enabled so the agent can append the blind-spot notes file.
        # Edit/Bash stay disabled so existing files are safe.
        disallow = ["AskUserQuestion", "Edit", "MultiEdit", "NotebookEdit", "Bash"]
    elif mode == INTERVIEW_MODE:
        # Same restriction set: Write for state/notes, no shell or edits.
        disallow = ["AskUserQuestion", "Edit", "MultiEdit", "NotebookEdit", "Bash"]
    elif mode == YUAN_MODE:
        # 元 skill 需要建/改 skills/ 下的文件，并用 Bash 做 cp .bak 备份与
        # json 校验，因此放开 Read/Write/Edit/MultiEdit/Bash；目录边界由 prompt
        # 约束（与 baguwen 同信任模型）。只禁用交互式提问与 notebook 编辑。
        disallow = ["AskUserQuestion", "NotebookEdit"]
    else:
        # generate / agent / *_complete: full toolset.
        disallow = ["AskUserQuestion"]
    return base + ["--disallowedTools", *disallow] + tail


_BAGUWEN_FLOW_FALLBACK = (
    "你是八股考官。通读章节正文，按重要性排序考点，逐个提问。"
    "用户掌握则进入下题；未掌握则追问并向笔记文件追加盲点记录"
    "（格式：问题/用户回答/缺口/正确要点）。"
    "用户反问则先简答再拉回。除笔记文件外不改任何文件。全程中文。"
)


def _read_baguwen_flow() -> str:
    """Read skills/baguwen/flow.md for prompt injection; fall back to built-in minimal string."""
    flow_path = WORKSPACE / "skills" / "baguwen" / "flow.md"
    try:
        text = flow_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except Exception:
        pass
    return _BAGUWEN_FLOW_FALLBACK


def _read_interview_flow() -> str:
    """Read skills/interview/flow.md for prompt injection."""
    flow_path = WORKSPACE / "skills" / "interview" / "flow.md"
    try:
        text = flow_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except Exception:
        pass
    return "你是严格但专业的面试官。每次只问一个问题，不提前透题。全程中文。"


def _read_yuan_flow() -> str:
    """Read skills/yuan-skill/flow.md for 元 skill prompt injection."""
    flow_path = WORKSPACE / "skills" / "yuan-skill" / "flow.md"
    try:
        text = flow_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except Exception:
        pass
    return (
        "你是「元 skill」助手，专职帮用户创建新 skill 或修改现有 skill。"
        "先澄清意图与模糊点，意图明确后再在 skills/<skill-name>/ 下落盘文件。全程中文。"
    )


def _read_interview_rubric() -> str:
    """Read skills/interview/rubric.md for prompt injection."""
    rubric_path = WORKSPACE / "skills" / "interview" / "rubric.md"
    try:
        text = rubric_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except Exception:
        pass
    return ""


def _extract_resume_text(uploaded_pdf: str | None, session_id: str,
                         cache_dir: Path = INTERVIEW_DIR) -> str:
    """Extract text from uploaded PDF resume, with caching.

    cache_dir selects where the extracted text is cached: INTERVIEW_DIR for
    interview mode, PERSONALIZE_DIR for personalize/generate mode.
    """
    if not uploaded_pdf:
        return ""
    cache_path = cache_dir / f"resume_{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id or 'session')}.txt"
    if cache_path.exists():
        try:
            return cache_path.read_text(encoding="utf-8")
        except Exception:
            pass

    pdf_path = UPLOADS / uploaded_pdf
    if not pdf_path.exists():
        return ""

    text = ""
    # Try pdftotext first
    try:
        result = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            text = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: try skills/personalize/pdf_parser.py
    if not text:
        parser_path = WORKSPACE / "skills" / "personalize" / "pdf_parser.py"
        if parser_path.exists():
            try:
                result = subprocess.run(
                    ["python3", str(parser_path), str(pdf_path)],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(WORKSPACE),
                )
                if result.returncode == 0 and result.stdout.strip():
                    text = result.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    if not text:
        return ""

    # Truncate to safe length
    max_chars = 16000
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n…（简历文本已截断）…"

    # Cache for subsequent turns
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        cache_path.write_text(text, encoding="utf-8")
    except Exception:
        pass
    return text


def _interview_notes_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "session")
    return INTERVIEW_DIR / f"notes_{safe}.md"


def _read_kb_index_summary(max_chars: int = 4000) -> str:
    """Read a truncated summary of knowledge/INDEX_KB.md for interview context."""
    index_path = WORKSPACE / "knowledge" / "INDEX_KB.md"
    if not index_path.exists():
        return ""
    try:
        text = index_path.read_text(encoding="utf-8")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（索引已截断）…"
        return text
    except Exception:
        return ""


# The knowledge index is a SINGLE root-level file: knowledge/INDEX_KB.md.
# Agents repeatedly assume each chapter directory has its own INDEX_KB.md and then
# fail with "Could not access knowledge/<NN_chapter>/INDEX_KB.md". Every prompt that
# may touch the index appends this so the rule is stated once, consistently.
INDEX_KB_SINGLE_SOURCE_NOTE = (
    "\n\n重要：全工作区只有一个知识索引文件 `knowledge/INDEX_KB.md`（根级别）。"
    "各章节目录内（如 `knowledge/NN_xxx/`）**不存在也不需要** INDEX_KB.md 文件，"
    "绝不要尝试读写章节目录内的 INDEX_KB.md。"
)


def build_interview_prompt(msg: dict) -> str:
    """Construct the prompt for interview (面试) mode."""
    content = (msg.get("content") or "").strip()
    history = msg.get("history") or []
    session_id = msg.get("session_id") or "session"
    uploaded_pdf = msg.get("context", {}).get("uploaded_pdf")
    difficulty = msg.get("interview_difficulty") or "standard"
    round_type = msg.get("interview_round") or "auto"

    flow = _read_interview_flow()
    rubric = _read_interview_rubric()
    resume_text = _extract_resume_text(uploaded_pdf, session_id)
    kb_index = _read_kb_index_summary()
    notes_path = _interview_notes_path(session_id)

    # ── Conversation phase detection ────────────────────────────────────────
    # The frontend pushes the just-sent user message into the transcript BEFORE
    # building `history`, so history's last entry is the *current* message. Strip
    # it for phase detection so "prior conversation" and "current message" don't
    # collide — otherwise the opening turn is mis-read as an answer and scored.
    is_start_token = (content == INTERVIEW_START_TOKEN)
    if (history and isinstance(history[-1], dict)
            and history[-1].get("role") == "user"
            and (history[-1].get("text") or "").strip() == content):
        prior_history = history[:-1]
    else:
        prior_history = list(history)
    hist_text = _format_history(prior_history)

    has_resume = bool(resume_text)
    has_jd = (not is_start_token) and bool(content)

    # Count interviewer turns that actually happened (current user msg excluded
    # via prior_history). Zero → this is the opening turn.
    agent_turns = sum(
        1 for h in prior_history
        if isinstance(h, dict) and h.get("role") == "agent" and (h.get("text") or "").strip()
    )
    first_user_text = next(
        ((h.get("text") or "").strip() for h in prior_history
         if isinstance(h, dict) and h.get("role") == "user"),
        "",
    )
    # Opening with no materials: the first user turn was a bare start token and
    # no resume was attached → the interviewer must first ask for the target role.
    opening_had_no_materials = (not has_resume) and (first_user_text == INTERVIEW_START_TOKEN)
    # The single turn right after a "no materials" opening is the user supplying
    # the target role; it is still part of the opening, not an answer to score.
    is_position_reply = (agent_turns == 1) and opening_had_no_materials
    is_opening = (agent_turns == 0) or is_position_reply

    if has_resume and has_jd:
        input_profile = "resume_and_jd"
    elif has_resume:
        input_profile = "resume_only"
    elif has_jd:
        input_profile = "jd_only"
    else:
        input_profile = "no_materials"

    # Difficulty instruction
    if difficulty == "strict":
        diff_inst = "面试风格：全程严格。回答不清楚立即追问。回答过于完美立即核验证据。不给缓冲，不给鼓励性评价。评分不虚高。"
    else:
        diff_inst = "面试风格：专业标准。第一轮语气专业但不过度压迫。回答泛泛、求提示、逃避问题时升级为严格追问。"

    # Round persona
    round_personas = {
        "auto": "综合面试官，根据简历/JD自动切换角色，全维度考察。",
        "hr": "HR 面试官。侧重：动机、文化匹配、稳定性、薪酬预期、空窗/跳槽解释。",
        "business": "业务面试官。侧重：业务理解、项目贡献、跨部门协作、目标达成。",
        "tech": "技术面试官。侧重：技术深度、系统设计、代码能力、技术决策。",
        "director": "部门主管/VP。侧重：战略视野、团队管理、资源分配、向上汇报。",
    }
    round_inst = round_personas.get(round_type, round_personas["auto"])

    # Build prompt sections
    sections: list[str] = []
    sections.append("你是严格但专业的真实面试官。")
    sections.append(f"面试官人设：{round_inst}")
    sections.append(diff_inst)

    if is_opening:
        # Opening turn — the uploaded resume/JD (or start token) are input
        # materials, NOT answers. Acknowledge them, then ask the first question.
        opening = (
            "\n这是面试开场，本轮没有用户的面试回答。"
            "\n【输出格式·硬性要求】本轮用自然口语化的对话展开，像面试官刚拿到材料、与候选人寒暄开场。"
            "严禁出现「本轮评分」「评价」「主要风险」「追问/下一题」这类字段标题或任何打分格式，"
            "也不要写「不评分」「第 0 轮」之类的系统措辞——这些只在正式答题后的轮次使用。"
        )
        if has_resume and has_jd:
            opening += (
                "\n用户已提供简历和岗位需求/JD 作为面试输入材料（不是面试回答）。"
                "\n你必须按以下顺序输出：\n"
                "1. 开场概述：用 2-3 句话概括候选人的核心背景（从简历提取）和目标岗位要点（从 JD 提取），让用户确认信息无误。\n"
                "2. 面试规则说明（见开场规则）。\n"
                "3. 第 1 题。\n"
                "禁止：对用户提供的简历或 JD 文本进行评价、打分或追问——这些是输入材料，不是面试回答。"
            )
        elif has_resume:
            opening += (
                "\n用户已上传简历，但未提供岗位需求/JD。"
                "\n你必须按以下顺序输出：\n"
                "1. 开场概述：用 1-2 句话概括候选人的核心背景，表明你已读取简历，并提示用户可补充目标岗位以做匹配判断。\n"
                "2. 面试规则说明（见开场规则）。\n"
                "3. 第 1 题。\n"
                "禁止：对简历文本进行评价或打分——这是输入材料，不是面试回答。"
            )
        elif has_jd:
            opening += (
                "\n用户提供了岗位需求/JD，但未上传简历。"
                "\n你必须按以下顺序输出：\n"
                "1. 开场概述：用 1-2 句话概括目标岗位要点，表明你已理解岗位需求，并提示用户可上传简历以做匹配判断。\n"
                "2. 面试规则说明（见开场规则）。\n"
                "3. 第 1 题。\n"
                "禁止：对 JD 文本进行评价或打分——这是输入材料，不是面试回答。"
            )
        else:
            # No resume and no JD — must ask for the target role before starting.
            opening += (
                "\n用户尚未提供简历或岗位需求，只是想开始面试。"
                "\n你必须按以下顺序输出：\n"
                "1. 用 1-2 句话礼貌说明：要开始面试，请先告诉我你想应聘的岗位（公司/方向/职级均可），如有简历也可以上传。\n"
                "2. 反问用户目标岗位。\n"
                "禁止：自行编造一个岗位、输出面试规则或直接给出第 1 题——必须等用户回复目标岗位后才正式开始。"
            )
        opening += (
            "\n\n私下建立岗位画像、证据地图和问题计划，不要把完整题库透露给用户。"
        )
        sections.append(opening)
    else:
        # Subsequent turns
        sections.append(
            "\n用户刚刚回答了上一个问题（见历史对话的最后一轮）。"
            "请评价用户上一轮回答，更新评分和证据缺口，决定追问或下一题。"
            "按照面试流程规范中的每轮评价格式输出。"
            f"\n用户本次回答：{content}"
        )

    sections.append(f"\n【面试流程规范】\n{flow}")
    if rubric:
        sections.append(f"\n【评分标准】\n{rubric}")

    if resume_text:
        sections.append(f"\n【用户简历文本】\n{resume_text}")

    # On any opening turn that carries a JD (resume+JD / jd_only / the position
    # the user typed right after a no-materials opening), surface it explicitly.
    # On later turns the JD already lives in the prior history.
    if has_jd and is_opening:
        sections.append(f"\n【用户提供的岗位需求/JD】\n{content}")

    # KB fallback only when there is something to interview on; suppress it during
    # a no-materials opening so the model asks for the target role instead of
    # quizzing the candidate from the knowledge index.
    if kb_index and not has_resume and not has_jd and not is_opening:
        sections.append(
            f"\n【候选人知识储备索引（仅供兜底出题参考，不要进行脱离场景的八股考察）】\n{kb_index}"
        )
    elif kb_index and (has_resume or has_jd):
        sections.append(
            "\n【知识库】候选人有本地知识库可供参考，"
            "但面试出题必须围绕简历项目和岗位需求，"
            "只有追问项目技术细节时才可用 Read 工具查阅 knowledge/ 目录下的相关章节。"
        )

    if hist_text:
        sections.append(f"\n【历史对话】\n{hist_text}")

    sections.append(f"\n面试笔记文件：{notes_path}（可向其追加每轮评分和笔记）")
    sections.append(f"\n输入完整度：{input_profile}")

    return "\n".join(sections)


def build_yuan_prompt(msg: dict) -> str:
    """Construct the prompt for 元 skill (Yuan Skill) mode.

    Intentionally standalone: it must NOT reuse build_agent_prompt or the generate
    fallback, both of which carry INDEX_KB / 原始资料留存 / 完善知识库 framing. 元
    skill only creates/modifies skills under skills/ and never touches the KB.
    """
    content = (msg.get("content") or "").strip()
    history = msg.get("history") or []

    flow = _read_yuan_flow()

    # The frontend pushes the just-sent user message into the transcript BEFORE
    # building history, so history's last entry is the current message. Strip it
    # so "prior conversation" and "current message" don't collide.
    if (history and isinstance(history[-1], dict)
            and history[-1].get("role") == "user"
            and (history[-1].get("text") or "").strip() == content):
        prior_history = history[:-1]
    else:
        prior_history = list(history)
    hist_text = _format_history(prior_history)

    is_start = (content == YUAN_START_TOKEN)
    if is_start:
        turn = (
            "本回合是开场。【D2】先用一句话问清意图，不要假设：\n"
            "「你是想 ① 新建一个 skill，还是 ② 修改现有 skill"
            "（baguwen / interview / search_evolve / personalize / render_html / yuan-skill）？」\n"
            "—— 等用户选定后，再进入对应分支，本回合不要创建或修改任何文件。"
        )
    else:
        turn = (
            "用户表明【新建】→ 执行 flow.md 阶段 1（创建版）：Entry Gate + 6 类模糊点 + "
            "用问题卡抛出 P0 问题。\n"
            "用户表明【修改】→ 先用 Read 把目标 skill 的全部文件读一遍建立基线，"
            "再执行 flow.md 阶段 1（修改版）。\n"
            "后续轮：按 flow.md 继续收敛；意图清晰后进入阶段 2 生成/修改文件。\n"
            f"用户本次输入：{content}"
        )

    return (
        f"你正在 Yuan Knowledge Base 的「元 skill」模式中，专职帮用户【创建新 skill】或【修改现有 skill】。\n"
        f"工作区：{WORKSPACE}。\n\n"
        f"【yuan-skill 工作流规范】\n{flow}\n\n"
        f"补充材料（按需用 Read 工具自行读取，不要全量内联）：\n"
        f"- skills/yuan-skill/project-architecture.md\n"
        f"- skills/yuan-skill/existing-skills-inventory.md\n"
        f"- skills/yuan-skill/skill-patterns.md\n\n"
        f"【历史对话】\n{hist_text or '（无）'}\n\n"
        f"【本回合】\n{turn}\n\n"
        f"硬约束：\n"
        f"1. 只在 skills/<skill-name>/ 下创建或修改文件。禁止改 knowledge/、server.py、"
        f"INDEX.md、paper-ui/、.agent/、.claude/。\n"
        f"2. 阶段 1（澄清）期间不要创建/修改任何文件；只有意图明确后（阶段 2）才动文件。\n"
        f"3. 修改任何**现有** skill 文件前，先用 Bash 执行 `cp <file> <file>.bak` 留备份；"
        f"新建文件无需备份。完成后在回复里逐条列出：新建了哪些文件、修改了哪些文件（含其 .bak）。\n"
        f"4. 全程中文。问题卡用纯文本 / Markdown 输出（不要调用 AskUserQuestion 工具）。\n"
        f"5. 本模式禁止输出任何「完善知识库 / 知识章节 / 原始资料留存 / INDEX_KB / draft」"
        f"相关提示——元 skill 只负责 skill 的创建与修改，不涉及知识库演化。"
    )


def build_agent_prompt(msg: dict) -> str:
    """Construct the prompt for the default free-form agent mode."""
    content = (msg.get("content") or "").strip()
    current_file = msg.get("context", {}).get("current_file")
    selection = (msg.get("selection") or "").strip()
    uploaded_pdf = msg.get("context", {}).get("uploaded_pdf")

    pdf_hint = ""
    if uploaded_pdf:
        pdf_path = UPLOADS / uploaded_pdf
        if pdf_path.exists():
            pdf_hint = (
                f"\n\n用户上传了 PDF 附件：{pdf_path}，可在需要时用 Read 工具参考其内容。"
            )

    note_context = ""
    if current_file:
        chapter_text = _read_chapter_source(current_file)
        chapter_label = _chapter_label(current_file)
        if chapter_text:
            note_context = (
                f"\n\n【当前打开的笔记：{chapter_label}】\n{chapter_text}\n"
            )

    selection_context = ""
    if selection:
        selection_context = f"\n\n【用户划选的文本片段】\n{selection}\n"

    prompt = (
        f"你是 Yuan Knowledge Base 工作台的通用 Agent，工作区路径：{WORKSPACE}。\n"
        f"你拥有全部工具权限（Read/Write/Edit/Bash/WebSearch/WebFetch 等），"
        f"可自由处理用户的任意复杂任务。\n\n"
        f"可用资源：\n"
        f"- skills/ 目录下的技能（可自行阅读 skill.json 了解用法）\n"
        f"- knowledge/ 目录下的知识库内容（可读写）\n"
        f"- 联网搜索（WebSearch/WebFetch）\n\n"
        f"{note_context}{selection_context}"
        f"【用户指令】\n{content}{pdf_hint}\n\n"
        f"回答优先级：如果用户的问题与当前打开的笔记内容相关，优先基于笔记正文回答，"
        f"再结合你自身的知识进行补充。如果问题与笔记无关则正常回答。\n\n"
        f"护栏约束：\n"
        f"1. 禁止修改 .claude/ 下任何配置或宿主全局权限设置。\n"
        f"2. 禁止调用 update-config、fewer-permission-prompts 等与本任务无关的全局技能；"
        f"只使用本工作区 skills/ 下的技能。\n"
        f"3. 完成后把最终答案打印到 stdout。"
        f"{INDEX_KB_SINGLE_SOURCE_NOTE}"
    )
    if _running_task_count() > 0:
        prompt += PARALLEL_COLLAB_PROMPT
    return prompt


def build_prompt(mode: str, msg: dict) -> str:
    """Construct the prompt for non-generate modes. (generate stays inline.)"""
    content = (msg.get("content") or "").strip()
    current_file = msg.get("context", {}).get("current_file")
    selection = (msg.get("selection") or "").strip()
    history = msg.get("history") or []
    session_id = msg.get("session_id") or "session"
    chapter_text = _read_chapter_source(current_file)
    hist_text = _format_history(history)
    chapter_label = _chapter_label(current_file)  # names the .md we actually read

    # Optional PDF attachment the user uploaded — qa/interview may reference it.
    uploaded_pdf = msg.get("context", {}).get("uploaded_pdf")
    pdf_note = ""
    if uploaded_pdf:
        pdf_path = UPLOADS / uploaded_pdf
        if pdf_path.exists():
            pdf_note = (
                f"\n\n【用户上传了 PDF 附件】路径：{pdf_path}，"
                f"可在需要时用 Read 工具参考其内容。"
            )

    if mode == BAGUWEN_MODE:
        flow = _read_baguwen_flow()
        notes_path = _baguwen_notes_path(session_id)
        if content == BAGUWEN_START_TOKEN:
            turn = (
                "这是八股开场。请按流程规范中的「开场」段执行。"
            )
        else:
            turn = (
                "用户刚刚回答了上一个问题（见历史对话的最后一轮）。"
                "请按流程规范中的「追问/下一题」段执行。\n"
                f"用户本次回答：{content}"
            )
        return (
            f"你正在就章节《{chapter_label}》对用户进行八股考察。\n\n"
            f"【八股流程规范】\n{flow}\n\n"
            f"【当前章节正文（可能已截断）】\n{chapter_text or '（无法读取正文）'}\n\n"
            f"【历史对话（含此前的提问与用户回答）】\n{hist_text or '（无）'}\n\n"
            f"【本回合】{turn}\n\n"
            f"八股笔记文件：{notes_path}（按流程规范向其追加盲点记录）"
            f"{pdf_note}"
        )

    # completion modes — both "new supplement note" (*_complete) and
    # "append to original" (*_append_complete) variants.
    parts = _chapter_parts(current_file)
    chapter_dir, stem = parts if parts else ("", "")
    source_kind = "八股" if "baguwen" in mode else ("面试" if "interview_kb" in mode else ("对话" if "agent" in mode else "补充"))
    sdir = _sources_dir(current_file)
    sources_rule = (
        f"\n0. 本章节已有原始检索资料目录 {sdir}；如需核对事实，"
        f"先用 Read 查阅其中本地原文，不要为此重新联网。"
        if sdir else ""
    )
    extra = ""
    if "baguwen" in mode:
        notes_path = _baguwen_notes_path(session_id)
        if notes_path.exists():
            try:
                extra = "\n\n【八股盲点笔记】\n" + notes_path.read_text(encoding="utf-8")
            except Exception:
                extra = ""
    elif "interview_kb" in mode:
        notes_path = _interview_notes_path(session_id)
        if notes_path.exists():
            try:
                extra = "\n\n【面试笔记】\n" + notes_path.read_text(encoding="utf-8")
            except Exception:
                extra = ""
        extra += (
            "\n\n【面试补充知识库过滤规则】\n"
            "1. 只提取通用技术知识、方法论、最佳实践、常见考点。\n"
            "2. 不要写入任何个人信息：姓名、简历内容、公司名称、项目细节、薪酬、联系方式。\n"
            "3. 不要写入面试评分、个人表现评价。\n"
            "4. 如果面试中暴露了知识盲点，以知识点本身的形式记录（而非「用户不知道 X」的形式）。"
        )

    if mode in APPEND_COMPLETION_MODES:
        # ─── append the supplement straight into the *registered* note ───
        tgt = _append_targets(current_file)
        if tgt:
            _cdir, md_rel, html_rel = tgt
            return (
                f"你正在把本轮{source_kind}产生的有价值补充，追加到原始知识笔记中（就地修改，不新建文件）。\n\n"
                f"用户已在前端确认本次操作。规则：{sources_rule}\n"
                f"1. 先备份原始笔记（务必先备份再修改）：\n"
                f"   - 用 Bash 执行：cp {md_rel} {md_rel}.bak\n"
                f"   - 用 Bash 执行：cp {html_rel} {html_rel}.bak\n"
                f"2. 用 Read 读取原始笔记 {md_rel}，在文件末尾追加补充内容："
                f"以 '---' 分隔线 + '## 补充（来自{source_kind}）' 标题开头，结构化列出新增知识点。\n"
                f"3. 用 Edit 工具直接在 {md_rel} 末尾追加（不要整体重写、不要改动已有正文）。\n"
                f"4. 重新渲染 HTML，且**必须用 --out 写回原文件名**（保持注册名不变、不要改名）：\n"
                f"   python3 skills/render_html/render.py {md_rel} --out {html_rel}\n"
                f"5. 普通补充流程绝不修改 knowledge/INDEX_KB.md，也不改动 01_* ~ 07_* 冷启动章节；"
                f"若用户明确要求维护/扩展冷启动库，不走本补充流程，改按 INDEX.md 的 cold-start seed 例外处理。"
                f"**绝不创建任何新的 .md 或 .html 文件**（备份 .bak 除外）。\n"
                f"6. 完成后把你做了什么简要打印到 stdout。\n\n"
                f"【可供提炼的对话内容】\n{hist_text or '（见下方笔记）'}{extra}"
                f"{INDEX_KB_SINGLE_SOURCE_NOTE}"
            )
        # No resolvable target → degrade to creating a new supplement note below.

    return (
        f"你正在把本轮{source_kind}产生的有价值补充，作为一篇新笔记加入知识库章节《{chapter_label}》。\n\n"
        f"用户已在前端确认过本次操作，因此**直接产出正式笔记，无需走草稿 / Pending Review 审阅流程**。规则："
        f"{sources_rule}\n"
        f"1. 普通补充流程绝不修改原章节正文，也不改动 01_* ~ 07_* 冷启动章节；只新增补充笔记。"
        f"若用户明确要求维护/扩展冷启动库，不走本补充流程，改按 INDEX.md 的 cold-start seed 例外处理。\n"
        f"2. 把补充内容写入补充笔记文件：knowledge/{chapter_dir}/{stem}.supplement.md"
        f"（若该文件已存在则**追加**一个新小节，不要新建多个文件）。内容需结构化列出新增知识点，"
        f"并标注来源（本次{source_kind}、对应考点）。\n"
        f"3. 调用 python3 skills/render_html/render.py 把补充文件渲染为正式 HTML："
        f"knowledge/{chapter_dir}/{stem}.supplement.html（注意是正式 .html，**不要**带 .draft 后缀）。\n"
        f"4. 在 knowledge/INDEX_KB.md 中为该补充笔记登记一个普通条目（与该章节其它正式笔记并列，"
        f"**不要**放进 \"Pending Review\" 段）。\n"
        f"5. 完成后把你做了什么简要打印到 stdout。\n\n"
        f"【可供提炼的对话内容】\n{hist_text or '（见下方笔记）'}{extra}"
        f"{INDEX_KB_SINGLE_SOURCE_NOTE}"
    )


PARALLEL_COLLAB_PROMPT = (
    "\n\n**并发协作约束**：你不是本工作区当前唯一运行的 Agent，可能有其它任务正同时读写 `knowledge/`。"
    "因此：(1) 只新建属于本任务的文件，命名带唯一标识，避免与他人重名；"
    "(2) 对 `knowledge/INDEX_KB.md` 只**追加**你自己的条目，编辑前先重新读取、不要整体重写、不要删除你未创建的条目；"
    "(3) 不要修改或删除非本任务产生的文件；普通演化任务不要改动 `01_*~07_*` 冷启动章节，"
    "但若当前任务明确要求维护/扩展冷启动库，则可按 INDEX.md 的 cold-start seed 例外处理；不要改动 `.claude/` 配置；"
    "(4) 章节元数据由服务端统一维护，你无需直接修改 `.kb_meta.json`。"
)


def _running_task_count() -> int:
    with _tasks_lock:
        return sum(1 for h in _tasks.values()
                   if h.proc is not None and h.proc.poll() is None)


def _completion_running() -> bool:
    """True if a 补充(completion) task is currently running.

    Completion tasks (qa/interview → supplement) are serialized: only one runs at
    a time so two append/supplement jobs can't race on the same chapter. generate
    stays parallel; qa/interview chat turns are never gated by this.
    """
    completion_modes = COMPLETION_MODES | APPEND_COMPLETION_MODES
    with _tasks_lock:
        for h in _tasks.values():
            if h.mode not in completion_modes:
                continue
            # Present in _tasks ⇒ active. proc is None means it was just dispatched
            # and its subprocess hasn't spawned yet (closes the race where two
            # back-to-back completions both saw "nothing running"). The worker pops
            # the handle from _tasks when it finishes, so finished ones don't count.
            if h.proc is None or h.proc.poll() is None:
                return True
    return False


def _scan_skills_changes(start_time: float) -> dict:
    """Scan skills/ for files touched this turn (mtime > start_time).

    Mirrors the knowledge/ append-mode scan: .bak files become backup_files (what
    the withdraw endpoint restores), and the rest are split into modified_files
    (have a sibling .bak → an existing skill was edited) vs new_files. Used by 元
    skill mode so the frontend can offer a backup-restore withdraw (C2).
    """
    skills_root = WORKSPACE / "skills"
    backups: list[str] = []
    changed: list[str] = []
    if skills_root.exists():
        for f in skills_root.rglob("*"):
            if not f.is_file():
                continue
            try:
                if f.stat().st_mtime <= start_time:
                    continue
            except OSError:
                continue
            rel = str(f.relative_to(WORKSPACE))
            if f.name.endswith(".bak"):
                backups.append(rel)
            else:
                changed.append(rel)
    bak_origs = {b[:-4] for b in backups}  # strip ".bak"
    modified = [c for c in changed if c in bak_origs]
    new = [c for c in changed if c not in bak_origs]
    return {
        "skills_change": True,
        "backup_files": backups,
        "modified_files": modified,
        "new_files": new,
    }


def _run_task_worker(handle: TaskHandle, agent_name: str, agent_cmd: list[str],
                     exe: str, prompt: str, msg: dict, timeout: int):
    """Worker function that runs a single agent task in its own thread."""
    msg_id = handle.msg_id
    mode = handle.mode
    session_id = handle.session_id
    start_time = handle.start_time
    clean_env = _build_clean_env()

    mode_meta = ({"mode": mode, "session_id": session_id}
                 if mode != "generate" else None)

    def _merge_meta(phase: str | None = None) -> dict | None:
        m = dict(mode_meta) if mode_meta else {}
        if phase:
            m["phase"] = phase
        return m or None

    try:
        proc = subprocess.Popen(
            [exe, *agent_cmd[1:], prompt],
            cwd=str(WORKSPACE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=clean_env,
            stdin=subprocess.DEVNULL,
        )
        handle.proc = proc
    except Exception as e:
        elapsed = time.time() - start_time
        _write_outbox(msg_id, msg_id, "agent_response",
                      f"Error starting agent: {e}", "error", elapsed)
        print(f"[worker:{msg_id}] Error starting agent: {e}")
        with _tasks_lock:
            _tasks.pop(msg_id, None)
        return

    output_lines: list[str] = []
    last_heartbeat = start_time
    recent_milestones: list[str] = []
    dir_file_counts: dict[str, int] = {}
    assistant_texts: list[str] = []
    final_result_text: str | None = None

    def _reader():
        nonlocal last_heartbeat, final_result_text
        for raw_line in proc.stdout:
            if handle.abort.is_set() or _abort_flag.is_set():
                break
            line = raw_line.rstrip("\n")
            output_lines.append(line)

            event = _parse_stream_event(line)
            if event is not None:
                for text, phase in event["milestones"]:
                    short = text[:120]
                    if not _is_similar_to_recent(short, recent_milestones):
                        recent_milestones.append(short)
                        _write_outbox(msg_id, msg_id, "progress",
                                      short, "milestone",
                                      time.time() - start_time,
                                      meta=_merge_meta(phase))
                if event["assistant_text"]:
                    assistant_texts.append(event["assistant_text"])
                if event["final_text"]:
                    final_result_text = event["final_text"]
                now = time.time()
                if now - last_heartbeat >= 10:
                    last_heartbeat = now
                    elapsed_now = now - start_time
                    _write_outbox(msg_id, msg_id, "progress",
                                  f"Agent working... ({int(elapsed_now)}s elapsed)",
                                  "running", elapsed_now, meta=mode_meta)
                continue

            category = _classify_line(line)
            if category:
                short = line.strip()[:120]
                phase = PHASE_MAP.get(category)

                if category == "file_created":
                    for part in short.split():
                        if "/" in part:
                            d = part.rsplit("/", 1)[0]
                            dir_file_counts[d] = dir_file_counts.get(d, 0) + 1
                            if dir_file_counts[d] == 1:
                                elapsed_t = time.time() - start_time
                                _write_outbox(msg_id, msg_id, "progress",
                                              short, "milestone", elapsed_t,
                                              meta=_merge_meta(phase))
                                recent_milestones.append(short)
                            break
                elif not _is_similar_to_recent(short, recent_milestones):
                    recent_milestones.append(short)
                    elapsed_t = time.time() - start_time
                    _write_outbox(msg_id, msg_id, "progress",
                                  short, "milestone", elapsed_t,
                                  meta=_merge_meta(phase))

            now = time.time()
            if now - last_heartbeat >= 10:
                last_heartbeat = now
                elapsed_now = now - start_time
                _write_outbox(msg_id, msg_id, "progress",
                              f"Agent working... ({int(elapsed_now)}s elapsed)",
                              "running", elapsed_now, meta=mode_meta)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        reader_thread.join(timeout=2)
        elapsed = time.time() - start_time
        _write_outbox(msg_id, msg_id, "agent_response",
                      f"Agent timed out after {int(elapsed)} seconds.",
                      "error", elapsed)
        print(f"[worker:{msg_id}] Agent timed out after {int(elapsed)}s.")
        with _tasks_lock:
            _tasks.pop(msg_id, None)
        return

    reader_thread.join(timeout=5)
    elapsed = time.time() - start_time

    with _tasks_lock:
        _tasks.pop(msg_id, None)

    if handle.abort.is_set() or _abort_flag.is_set():
        print(f"[worker:{msg_id}] Agent was aborted, skipping response output.")
        return

    agent_output = "\n".join(output_lines).strip() or "(no output)"
    status = "completed" if proc.returncode == 0 else "error"
    print(f"[worker:{msg_id}] Agent finished (rc={proc.returncode}), output length: {len(agent_output)}, elapsed: {elapsed:.1f}s")

    if final_result_text or assistant_texts:
        clean_output = final_result_text or "\n\n".join(assistant_texts)
    elif agent_name == "codex":
        clean_output = _extract_codex_reply(agent_output)
    else:
        clean_output = agent_output

    if status == "completed" and (mode in INTERACTIVE_MODES or mode == AGENT_MODE):
        if final_result_text or assistant_texts:
            answer = (final_result_text or "\n\n".join(assistant_texts)).strip()
        elif agent_name == "codex":
            answer = _extract_codex_reply(agent_output)
        else:
            answer = _extract_final_answer(agent_output)
        answer = answer or "(无输出)"
        meta = {"mode": mode, "session_id": session_id}
        if mode == YUAN_MODE:
            # Surface this turn's skills/ changes so the frontend can offer a
            # backup-restore withdraw for any modified existing skill (C2).
            meta.update(_scan_skills_changes(start_time))
        _write_outbox(msg_id, msg_id, "agent_response",
                      answer, status, elapsed,
                      meta=meta)
    elif status == "completed":
        scan_result = _post_completion_scan(msg_id, start_time,
                                            snapshot=handle.snapshot)
        if mode_meta:
            scan_result = {**scan_result, **mode_meta}
        if mode and "_append_" in mode:
            # Append mode modifies existing .md/.html in place (so they don't show
            # up in new_files) and drops .bak backups (excluded by suffix). Detect
            # both via mtime so the frontend can offer a backup-restore withdraw.
            scan_result["append_mode"] = True
            backups: list[str] = []
            modified: list[str] = []
            kb = WORKSPACE / "knowledge"
            if kb.exists():
                for f in kb.rglob("*"):
                    if not f.is_file():
                        continue
                    try:
                        if f.stat().st_mtime <= start_time:
                            continue
                    except OSError:
                        continue
                    rel = str(f.relative_to(WORKSPACE))
                    if _is_sources_path(rel):
                        continue
                    if f.name.endswith(".bak"):
                        backups.append(rel)
                    elif f.suffix in (".md", ".html"):
                        modified.append(rel)
            scan_result["backup_files"] = backups
            scan_result["modified_files"] = modified
        summary = _extract_agent_summary(clean_output, scan_result)
        show_raw = (len(clean_output) > len(summary) + 40
                    or clean_output.count("\n") > summary.count("\n") + 3)
        _write_outbox(msg_id, msg_id, "agent_response",
                      summary, status, elapsed,
                      meta=scan_result,
                      raw_output=clean_output if show_raw else None)
    else:
        _write_outbox(msg_id, msg_id, "agent_response",
                      _annotate_agent_error(agent_name, clean_output),
                      status, elapsed, meta=mode_meta)
    print(f"[worker:{msg_id}] Response written to outbox.")


def watcher_loop(initial_agent: str, interval: float = 1.0):
    global current_agent
    with _config_lock:
        current_agent = initial_agent

    print(f"[watcher] Monitoring {INBOX} for new messages (agent: {current_agent}, max_parallel={MAX_PARALLEL})")
    seen: set[str] = set()
    last_cleanup = time.time()

    while True:
        time.sleep(interval)

        if time.time() - last_cleanup > 6 * 3600:
            cleanup_transient_files()
            last_cleanup = time.time()

        if not INBOX.exists():
            continue

        def _inbox_mtime(f: Path) -> tuple:
            try:
                return (f.stat().st_mtime, f.name)
            except OSError:
                return (float("inf"), f.name)

        for msg_file in sorted(INBOX.glob("*.json"), key=_inbox_mtime):
            if msg_file.name in seen:
                continue

            # Concurrency gate: if at capacity, leave this message in inbox
            # for the next poll iteration (overflow → FIFO fallback).
            if _running_task_count() >= MAX_PARALLEL:
                break

            # Peek the mode WITHOUT consuming, so a 补充(completion) task can be
            # left queued in the inbox when another completion is still running.
            try:
                msg = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                seen.add(msg_file.name)  # corrupt → skip permanently
                continue

            mode = msg.get("mode") or "generate"
            is_completion = mode in COMPLETION_MODES or mode in APPEND_COMPLETION_MODES

            # Serialize completions: only one supplement job at a time. Extra ones
            # wait their turn in the inbox (FIFO by mtime) — not rejected. generate
            # stays parallel; qa/interview turns are never gated.
            if is_completion and _completion_running():
                continue  # leave queued, retry next poll iteration

            # Consume the message now that we've decided to dispatch it.
            seen.add(msg_file.name)
            try:
                msg_file.unlink()
            except Exception:
                pass

            content = msg.get("content", "").strip()
            if not content:
                continue

            session_id = msg.get("session_id") or ""

            # generate / baguwen / interview / completion may all run concurrently:
            # generate creates fresh chapters (no conflict), completions are
            # serialized above, and baguwen/interview turns are quick read-mostly
            # calls. The only cap is MAX_PARALLEL (checked above).

            with _config_lock:
                agent_name = current_agent
                timeout = agent_timeout

            agent_cmd = AGENT_COMMANDS.get(agent_name)
            if not agent_cmd:
                _write_outbox(msg.get("id", ""), msg.get("id"), "error",
                              f"Unknown agent: {agent_name}", "error")
                continue

            if agent_name == "claude" and mode != "generate":
                agent_cmd = _claude_cmd_for_mode(mode)
                if mode == BAGUWEN_MODE:
                    BAGUWEN_DIR.mkdir(parents=True, exist_ok=True)
                elif mode == INTERVIEW_MODE:
                    INTERVIEW_DIR.mkdir(parents=True, exist_ok=True)

            extra_path = str(Path.home() / ".npm-global" / "bin")
            search_path = os.environ.get("PATH", "") + os.pathsep + extra_path
            exe = shutil.which(agent_cmd[0], path=search_path)
            if not exe:
                print(f"[watcher] Agent CLI '{agent_cmd[0]}' not found in PATH. Skipping.")
                _write_outbox(msg.get("id", ""), msg.get("id"), "error",
                              f"Agent CLI '{agent_cmd[0]}' not found in PATH. Please install it or start the agent manually.",
                              "error")
                continue

            if mode == INTERVIEW_MODE:
                prompt = build_interview_prompt(msg)
            elif mode == BAGUWEN_MODE or mode in COMPLETION_MODES or mode in APPEND_COMPLETION_MODES:
                prompt = build_prompt(mode, msg)
            elif mode == YUAN_MODE:
                # Must precede AGENT_MODE / the generate fallback so 元 skill never
                # inherits the knowledge-base framing of those paths.
                prompt = build_yuan_prompt(msg)
            elif mode == AGENT_MODE:
                prompt = build_agent_prompt(msg)
            else:
                uploaded_pdf = msg.get("context", {}).get("uploaded_pdf")
                pdf_hint = ""
                if uploaded_pdf:
                    # Server-side pre-extraction: the Agent must NOT run pdftotext
                    # itself (avoids tmp/pdfs/ byproducts). Inject the resume text
                    # directly; fall back to the path hint if extraction fails.
                    resume_text = _extract_resume_text(
                        uploaded_pdf, session_id, cache_dir=PERSONALIZE_DIR)
                    if resume_text:
                        pdf_hint = (
                            f"\n\n【简历原文（已由服务端预提取，无需再自行解析 PDF）】\n"
                            f"{resume_text}"
                        )
                    else:
                        pdf_hint = (
                            f"\nThe user also uploaded a PDF resume at: "
                            f"{UPLOADS / uploaded_pdf}"
                        )

                prompt = (
                    f"You are working in the Yuan Knowledge Base workspace at {WORKSPACE}. "
                    f"Read INDEX.md first for workspace rules. "
                    f"The user submitted this command via the console:\n\n"
                    f"{content}{pdf_hint}\n\n"
                    f"Execute the task using skills/ and update knowledge/ as needed. "
                    f"Print your final answer to stdout when done."
                    f"{INDEX_KB_SINGLE_SOURCE_NOTE}\n\n"
                    f"原始资料留存（凡涉及联网检索的任务都必须做）：\n"
                    f"把检索到的有价值原文，在浓缩成笔记的同时，按来源逐条保存到该笔记的原始资料夹"
                    f" knowledge/<章节目录>/<笔记stem>.sources/ 下——每条来源写一个文件"
                    f" NN_<来源slug>.md（含：原文关键摘录、来源URL、抓取日期、优先级P0-P4），"
                    f"并在该资料夹内维护 _manifest.md 作为来源总清单。"
                    f"这些是原始参考资料、不是笔记：绝不登记进 knowledge/INDEX_KB.md，绝不渲染成 HTML，"
                    f"也不要修改它们之外的冷启动章节。其目的是让后续问答能查阅本地原文、避免幻觉。"
                )
                if agent_name == "claude":
                    prompt += (
                        f"\n\n工作约束（必须严格遵守）：\n"
                        f"1. 严格遵循 INDEX.md：新内容一律先产出为 *.draft.html，并登记到 "
                        f"knowledge/INDEX_KB.md 的 \"Pending Review\" 段；不得直接产出正式 .html。"
                        f"普通演化任务不得修改 01_* ~ 07_* 冷启动章节；"
                        f"只有用户明确要求维护/扩展冷启动库时，才可按 INDEX.md 的 cold-start seed 例外处理。\n"
                        f"2. 禁止修改 .claude/ 下任何配置或宿主权限设置；禁止调用 update-config、"
                        f"fewer-permission-prompts 等与本任务无关的全局技能；只使用本工作区 skills/ 下的技能。\n"
                        f"3. 联网检索后如确认目标对象不存在或无法获取，必须如实说明\"未找到\"，"
                        f"禁止用其它版本或近似对象冒充交付。"
                    )

                # B6: inject collaboration prompt when parallel tasks are running
                if _running_task_count() > 0:
                    prompt += PARALLEL_COLLAB_PROMPT

            msg_id = msg.get("id", "")
            print(f"[watcher] Dispatching to {agent_name} (mode={mode}): {content[:80]}...")

            handle = TaskHandle(msg_id, mode, session_id)
            handle.snapshot = _snapshot_knowledge_files()

            with _tasks_lock:
                _tasks[msg_id] = handle

            _abort_flag.clear()

            _write_outbox(msg_id, msg_id, "progress",
                          f"Agent ({agent_name}) started processing...", "started",
                          meta=({"mode": mode, "session_id": session_id}
                                if mode != "generate" else None))

            worker = threading.Thread(
                target=_run_task_worker,
                args=(handle, agent_name, agent_cmd, exe, prompt, msg, timeout),
                daemon=True,
                name=f"worker-{msg_id}",
            )
            handle.thread = worker
            worker.start()


def main():
    parser = argparse.ArgumentParser(description="Yuan Knowledge Base workspace server")
    # Default port honours the PORT env var (lets a launcher/preview harness pick
    # the port) and falls back to 8741. An explicit --port still overrides both.
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8741)))
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--agent", default="claude", choices=list(AGENT_COMMANDS.keys()),
                        help="Which Agent CLI to invoke (default: claude)")
    parser.add_argument("--no-watch", action="store_true",
                        help="Disable the file watcher (server-only mode)")
    args = parser.parse_args()

    ensure_dirs()
    cleanup_transient_files()      # purge stale transient files once at startup
    os.chdir(WORKSPACE)

    if not args.no_watch:
        watcher = threading.Thread(target=watcher_loop, args=(args.agent,), daemon=True, name="watcher")
        watcher.start()

    server = HTTPServer((args.bind, args.port), WorkspaceHandler)
    print(f"Yuan Knowledge Base server running on http://{args.bind}:{args.port}")
    print(f"  Console:  http://{args.bind}:{args.port}/paper-ui/index.html")
    print(f"  API:      http://{args.bind}:{args.port}/api/health")
    print(f"  Agent:    {args.agent} ({'watching' if not args.no_watch else 'disabled'})")
    print(f"  Inbox:    {INBOX}")
    print(f"  Outbox:   {OUTBOX}")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
