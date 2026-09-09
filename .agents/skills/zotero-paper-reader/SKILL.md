---
name: zotero-paper-reader
description: Read one or more papers from a named Zotero collection and create evidence-backed Markdown research cards. Use for structured paper reading, summaries, or extracting theory, methods, findings, and limitations.
---

# Zotero Paper Reader

Use the connected, read-only Zotero MCP server for evidence. Work only on a
collection or papers the user names. Markdown remains the source of truth.

1. Confirm paper metadata with lookup, then read only the full-text passages
   needed to support the card. If full text is absent or not indexed, state that
   instead of inferring from metadata.
2. Resolve the Better BibTeX citekey when available and retain the Zotero parent
   item key. Create or refresh research/cards/<citekey>.md:

       .venv/bin/python -m comm_paper_radar.research_workspace card-template \
         --citekey "<citekey>" --title "<title>" --collection "<collection>"
         --zotero-item-key "<zotero-parent-item-key>"

3. Preserve the final ## My Thoughts section. Fill the reader-card sections with
   research problem, RQ/H, theory and constructs, operationalization,
   context/data/sample, methods, findings, contributions, limitations, and key
   evidence. Use 未报告 when the source does not provide an answer.
4. Add machine-readable theories and methods entries to front matter for every
   reported item. Each entry needs a name and evidence containing the citekey
   and a Zotero paragraph position, for example [Smith2024 ¶18]. Do not merge
   distinct theories or methods merely because they are similar.
5. Validate before declaring completion:

       .venv/bin/python -m comm_paper_radar.research_workspace validate-card \
         research/cards/<citekey>.md

Only report claims that have a source location. After the card validates, offer
`sync-zotero-notes` as an explicit separate step. It is allowed to update only
the generated `Codex Research Card` child Note; never update tags, collections,
PDFs, or hand-written Notes.
