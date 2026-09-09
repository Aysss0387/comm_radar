---
name: zotero-card-sync
description: Preview or synchronize validated Markdown research cards as generated child Notes beneath their matching Zotero items. Use for showing Codex cards inside Zotero; do not use for manual Zotero Notes or unreviewed citekey migration.
---

# Zotero Card Sync

Markdown cards are authoritative. A Zotero `Codex Research Card` is a generated
HTML display copy and may be replaced on later syncs.

1. Require a named collection and run a dry run first:

       .venv/bin/python -m comm_paper_radar.research_workspace sync-zotero-notes \
         --collection "<collection>"

2. Only run the following after the user has reviewed the dry run and Zotero 10
   has granted local API access:

       .venv/bin/python -m comm_paper_radar.research_workspace sync-zotero-notes \
         --collection "<collection>" --apply

3. Sync only valid cards with a `zotero_item_key`. The generated Note is marked
   by the tool, so hand-written Notes must never be edited or deleted.
4. For existing citekeys, use `citekey-preview` and obtain explicit user review
   before `migrate-citekeys --confirm`. Do not regenerate an unreviewed
   collection or an entire library.
