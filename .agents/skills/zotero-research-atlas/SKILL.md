---
name: zotero-research-atlas
description: Build an evidence-traceable theory map and method bank from validated Zotero research cards in one named collection. Use for cross-paper comparison, not for unscoped whole-library claims.
---

# Zotero Research Atlas

Use a collection explicitly named by the user. The atlas is derived from
validated evidence cards, so it remains reproducible and does not reread every
PDF unless a card needs repair.

1. Find cards in research/cards/ whose collection front-matter field exactly
   matches the requested collection. Validate any card that will be used.
2. Build both views:

       .venv/bin/python -m comm_paper_radar.research_workspace build-atlas \
         --collection "<collection>"

3. Inspect the generated files under research/collections/<collection-slug>/.
   theory-map.md shows theory, definition, dimensions, operationalization,
   context, outcomes, and source evidence. method-bank.md shows method, design,
   data/sample, analysis, validation, research question, and evidence.
4. If an entry lacks a source position, repair the original card through
   zotero-paper-reader rather than fabricating a map row.

The maps describe the selected collection only. 未报告 is not evidence of
absence in the original study or the field.
