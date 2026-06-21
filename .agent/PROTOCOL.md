# .agent/ — File-based IPC Protocol

This directory is the communication bridge between the paper-ui frontend
and the local Agent process (Claude Code, Codex, Antigravity, etc.).

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
  "context": {
    "current_file": "04_generative_theory/flow_matching_tutorial.html",
    "uploaded_pdf": null
  }
}
```

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
