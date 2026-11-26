# prompt.md specification (5-block XML) and the files of a job

Read this before writing `prompt.md` by hand, or when `init` reports `missing_xml_tags` / `architecture_warnings`. `init --compose-xml` and `compose-prompt` generate this structure from a plain objective.

## Who reads which file

| File | Written by | Read by | Content |
|---|---|---|---|
| `prompt.md` (`--prompt`) | Claude | Gemini Deep Research | Execution specification: what to investigate, where, how to report |
| `goal.md` (`--goal`) | Claude | Claude, at plan review and report review | Fixed acceptance checklist (see SKILL.md step 2) |
| `feedback_N.md` (`--feedback`) | Claude | Gemini plan editor | Imperative changes to the plan |
| `approval.md` (`--reason`) | Claude | the approval gate, the audit trail | Which plan items satisfy which goal items |

The same text in `prompt.md` and `goal.md` is rejected (`IDENTICAL_PROMPT_AND_GOAL`): a checklist copied into the prompt cannot catch what the prompt itself left out.

## The five blocks

```xml
<research_objective>
Compare, analyze, and synthesize [topic] across [systems] to produce [decision or deliverable].
</research_objective>

<context_and_scope>
- Self-contained context: [inline the relevant code, schema, or constraints].
- Google Workspace sources (optional): [exact Drive folder or document titles, Gmail thread queries].
- Temporal and domain scope: [e.g. 2022–2026 production systems and peer-reviewed literature].
- Exclusions: [textbook basics, marketing overviews].
</context_and_scope>

<investigation_axes>
1. [Axis with specific technical terms: HNSW, DiskANN Vamana graph, IVF-PQ].
2. [Axis with quantitative metrics: Recall@10, p99 latency, DRAM vs. NVMe footprint].
3. [Axis with operational edge cases: compaction, streaming ingestion, recovery].
Conditional depth-first trigger: when sources report conflicting numbers, trace hardware, dataset and method in the primary text to explain the gap.
</investigation_axes>

<source_governance>
- Prefer primary sources: peer-reviewed papers (DOI/arXiv), official engineering documentation, reproducible benchmarks.
- Do not use vendor marketing or unverified blogs as sole evidence.
- Numbered inline citations with URL and publication or verification date.
- Separate confirmed facts from hypotheses. Where no verified public evidence exists, write '검증 가능한 공개 근거 불충분 (Insufficient verified evidence)' instead of estimating.
</source_governance>

<output_format>
- GitHub-Flavored Markdown with a `#` title and `##` sections.
- Sections: executive summary and verdict; technical analysis; comparison table with fixed columns; risks and recommendations; `## 참고 출처 (Sources)` as a numbered list of links.
- If structured data is needed, put a fenced json block inside a `##` section.
</output_format>
```

## Why each rule exists

- **Objective is a multi-dimensional task with an action verb** (compare, analyze, evaluate). A lookup or an open question makes Gemini reply with prose or a clarifying question, and no plan card appears (`PLAN_TIMEOUT`).
- **Context is inlined, never a local path.** Gemini has no access to the user's files and agy is confined to the job directory. `init` reports `LOCAL_PATH_REFERENCE` in `architecture_warnings` when the prompt names `./…` or `C:\…`.
- **Workspace sources are named exactly.** The app uses the signed-in account's Drive, Gmail and Chat access, so internal documents are reachable by title or query without uploading them.
- **The "insufficient evidence" phrase is mandatory.** It gives Gemini an allowed alternative to inventing numbers, and gives Claude a string to search for in the report (SKILL.md step 7).
- **The Sources section title and numbered-link format are fixed.** `sources_count` is counted from that section.
- **Never ask for JSON only.** A report without a Markdown heading fails the Report Quality Gate (`REPORT_STRUCTURE_INVALID`); `init` reports `JSON_ONLY_OUTPUT_TRAP`.
- **Ask for a full-length report.** The gate minimum (`REPORT_MIN_CHARS`) only rejects empty shells; request the depth the user needs.

`--strict-xml` on `init` / `plan` turns a missing block into `PROMPT_XML_STRUCTURE_INVALID`. Without it a plain-text prompt is accepted as is.
