# Yuan Knowledge Base Workspace

This is a personal AI knowledge workspace designed for AI Agents.

## Progressive Routing

- **Knowledge Base**: Read `knowledge/INDEX_KB.md` for the full route map.
- **Available Skills**: Read the `skill.json` in each subdirectory under `skills/`.

## Workflow Rules

1. **Cold-start corpus**: `knowledge/01_*` through `knowledge/07_*` are the curated cold-start library. This includes any explicit maintainer-approved seed additions placed inside those directories. Treat all files there as read-only by default.
2. **Evolve by appending**: Ordinary new/evolved content must be created in a new incrementally-numbered chapter directory (e.g., `knowledge/08_<topic>/`), then registered in `knowledge/INDEX_KB.md`. Exception: when the user explicitly asks to maintain or expand the cold-start library, the new seed content may be added to `knowledge/01_*` through `knowledge/07_*` and registered under "Cold-Start Chapters"; after that it is part of the read-only cold-start corpus.
3. **HTML is the display format**: All knowledge viewing is done via compiled single-file `.html`. After modifying any Markdown source, invoke `skills/render_html/render.py` to regenerate the corresponding HTML.
4. **Draft review pipeline**: New content must first be generated as `.draft.html`, registered in the "Pending Review" section of `INDEX_KB.md`, and only promoted to official status after user confirmation.
5. **Raw sources retention (`<stem>.sources/`)**: Whenever a note is produced from web retrieval, persist the source originals next to that note in a sibling folder `knowledge/<NN_topic>/<note_stem>.sources/` — one `NN_<slug>.md` per source (key excerpt + source URL + retrieval date + priority P0–P4) plus a `_manifest.md` index. This folder is **reference material, not a note**: never register it in `INDEX_KB.md`, never render it to HTML, never let it pass through the draft pipeline. Its purpose is to let later Q&A read the local originals and avoid hallucination. Notes always live at depth 2 (`knowledge/NN_topic/<file>`); everything under a `.sources/` folder is reference only.
