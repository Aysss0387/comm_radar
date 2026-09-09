---
name: zotero-idea-audit
description: Audit a named Zotero collection for evidence-backed research ideas, candidate gaps, disagreements, or citation support. Use when every conclusion must distinguish collection evidence from field-wide claims.
---

# Zotero Idea Audit

Work from one named Zotero collection and its validated cards. Use Zotero
fulltext only to resolve missing or disputed evidence. Do not represent the
library as exhaustive and do not write to Zotero.

1. Create an audit document before analysing:

       .venv/bin/python -m comm_paper_radar.research_workspace idea-template \
         --collection "<collection>" --question "<question>" \
         --output "research/collections/<collection-slug>/idea-audits/<slug>.md"

2. Fill the audit in this order: evidence coverage, what is known, research
   design distribution, tensions/boundary conditions, candidate gaps, then
   candidate ideas. Cite each empirical statement with card or Zotero paragraph
   evidence.
3. A candidate idea must state existing evidence, the low-coverage combination
   observed in this collection, closest competing paper, RQ/H, feasible data
   and method, expected contribution, and uncertainty.
4. For citation-finder requests, create a short audit that splits the supplied
   prose into factual claims. Mark each claim as supported, weakly supported, or
   evidence insufficient; never invent a citation.

Use 候选 gap rather than 领域空白 unless the user separately supplies a
systematic-review scope that establishes exhaustiveness.
