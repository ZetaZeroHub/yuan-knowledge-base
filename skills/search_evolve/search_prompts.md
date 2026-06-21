# Search Prompt Templates

> **Raw-sources rule (applies to every template below):** before you condense
> retrieved material into the note, save each source to the note's sibling
> folder `knowledge/[NN]_[topic]/[note_stem].sources/` — one `NN_<slug>.md` per
> source (key excerpt + source URL + retrieval date + priority P0–P4) plus a
> `_manifest.md` index. This folder is reference material only: never register it
> in `INDEX_KB.md` and never render it to HTML. Fire independent searches in
> parallel (one batch), not one at a time.

## Template 1: Direction Search & Knowledge Update

**Scenario**: Given a frontier research direction (e.g., "Flow Matching Post-Training advances").

```
Please search for recent top-venue ArXiv papers on [<direction>].
1. Filter and select the 3 most cited or noteworthy representative works.
2. Extract their core improvements and align formula notation across all 3 papers using standard LaTeX.
3. Create a new incrementally-numbered chapter under knowledge/, and update knowledge/INDEX_KB.md.
```

## Template 2: Citation Tracking (Specific Paper Deep-Dive)

**Scenario**: Given a specific paper (e.g., "Medusa: Simple LLM Generation").

```
1. Search for the paper [<paper name/authors>].
2. Identify its publication year, venue, and official GitHub implementation.
3. Extract the core mathematical derivation (e.g., multi-head prediction probability loss in Medusa).
4. Implement the minimal core operator in PyTorch (under 60 lines, must be runnable).
5. Write 10 technical Q&A items about the paper's core concepts, output as HTML and register in the index.
```

## Template 3: Algorithm & Module Synthesis

**Scenario**: Given a new algorithm name (e.g., "FlashAttention-3").

```
1. Search for the technical whitepaper and hardware acceleration principles of [<algorithm/module>].
2. Compare time/space complexity and memory overhead against the previous version (e.g., FlashAttention-2).
3. Draft a complete technical reference with comparison tables, output as HTML and register in the index.
```
