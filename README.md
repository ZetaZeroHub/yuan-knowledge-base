# Yuan Knowledge Base

[中文版本](README.zh-CN.md)

Yuan Knowledge Base is a local-first AI knowledge workspace for building, browsing, evolving, and practicing with a personal technical knowledge base. It combines a lightweight web console, a file-based agent IPC layer, Markdown/HTML knowledge notes, and extensible workspace skills.

The project is designed for agent-assisted learning workflows: research a topic, preserve source material, generate reviewable notes, practice with interview-style modes, and create or modify custom skills inside the same workspace.

## Highlights

- **Local web console**: browse the knowledge base, chat with an agent, upload PDFs, inspect progress, and switch runtime settings from `paper-ui/`.
- **Agent-powered knowledge evolution**: use Codex or Claude Code to research topics, create draft chapters, update indexes, and render notes.
- **Built-in learning modes**:
  - `Search`: generate knowledge notes from research tasks.
  - `Baguwen`: quiz yourself on the currently opened knowledge chapter.
  - `Interview`: run resume/JD-aware mock interviews.
  - `Yuan Skill`: create or modify workspace skills through a structured clarification workflow.
  - default `Agent`: free-form workspace help without entering the full knowledge-generation pipeline.
- **Reviewable knowledge workflow**: new knowledge is created as draft content first, then promoted after review.
- **Raw source retention**: web-derived notes can keep local source excerpts in sibling `.sources/` folders for later verification.
- **Resumable chat history**: conversation history is stored as JSON under `.agent/history/`.
- **Extensible skills**: each subdirectory under `skills/` defines a reusable workflow or utility.

## Quick Start

### macOS

```bash
./start.command
```

### Windows

```bat
start.bat
```

### Manual

```bash
python3 -u server.py --port 8741 --bind 127.0.0.1 --agent codex
```

Then open:

```text
http://127.0.0.1:8741/paper-ui/index.html
```

The default startup scripts use the legacy compatibility environment variables `EVOLVEKB_PORT` and `EVOLVEKB_AGENT`:

```bash
EVOLVEKB_PORT=8741 EVOLVEKB_AGENT=codex ./start.command
```

## Requirements

- Python 3.10+
- A modern browser
- Optional, for agent workflows:
  - Codex CLI, or
  - Claude Code CLI

You can browse existing knowledge with the local server alone. Agent-backed generation, interview feedback, and skill editing require the selected CLI to be installed and authenticated.

## Project Layout

```text
yuan-knowledge-base-workspace/
├── INDEX.md              # Workspace rules for humans and agents
├── server.py             # Local server, API routes, agent IPC, prompt routing
├── paper-ui/             # Browser console
├── knowledge/            # Markdown/HTML knowledge base
│   ├── INDEX_KB.md       # Knowledge route map and pending review index
│   └── 01_* ... 07_*     # Curated cold-start chapters
├── skills/               # Agent-readable skills and utilities
│   ├── baguwen/
│   ├── interview/
│   ├── personalize/
│   ├── render_html/
│   ├── search_evolve/
│   └── yuan-skill/
└── .agent/               # Runtime IPC, uploads, histories, notes
```

## Core Workflow Rules

The canonical workspace rules live in `INDEX.md`. The most important conventions are:

1. `knowledge/01_*` through `knowledge/07_*` are curated cold-start chapters and should be treated as read-only by default.
2. Ordinary new knowledge should be appended in a new incrementally numbered chapter directory, then registered in `knowledge/INDEX_KB.md`.
3. HTML is the display format. After editing Markdown, regenerate HTML with `skills/render_html/render.py`.
4. New knowledge should go through the draft review pipeline before promotion.
5. Source material from web research should be retained in `<note_stem>.sources/` folders and should not be registered as notes.

## Skills

- `search_evolve`: research and generate new knowledge chapters.
- `personalize`: use a PDF resume as context for personalized knowledge generation.
- `render_html`: convert Markdown/JSON artifacts into single-file HTML.
- `baguwen`: run chapter-based oral-exam practice.
- `interview`: run resume/JD-aware mock interviews.
- `yuan-skill`: create or modify workspace skills with structured clarification.

## Data and Privacy Notes

This workspace is local-first, but agent workflows may call external tools or model providers depending on the CLI you select.

Runtime files are stored under `.agent/`, including uploaded PDFs, inbox/outbox messages, temporary interview notes, and saved chat histories. The `.gitignore` excludes these transient directories by default, but review the repository carefully before publishing it if your `knowledge/` folder contains private notes.

## Useful Commands

Render a Markdown note to HTML:

```bash
python3 skills/render_html/render.py knowledge/01_general/attention_tutorial.md --out knowledge/01_general/attention_tutorial.html
```

Run the server with Claude Code:

```bash
python3 -u server.py --port 8741 --bind 127.0.0.1 --agent claude
```

Run the server with Codex:

```bash
python3 -u server.py --port 8741 --bind 127.0.0.1 --agent codex
```

## Status

This is a personal workspace project. It is built to be practical, hackable, and easy to extend rather than packaged as a polished hosted service.
