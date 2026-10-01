---
name: agy-pdf-analyzer
description: Analyse a whole lecture PDF (slides or handouts, any number of pages) by handing the PDF itself to the Antigravity CLI (`agy`) — no conversion to page images. The page count is read from the PDF, the pages are sent a few at a time to an agy analyst session (persona + deck context + chain-of-thought + few-shot) that writes structured JSON per page (verbatim text, tables, code, diagrams, key concepts, summary), a deterministic validator and an independent agy reviewer session check every page against the PDF, and failed pages are retried with feedback. Use when the user has a `.pdf` of lecture material and wants it transcribed, summarised or turned into study notes — "이 PDF agy로 분석해줘", "강의자료 pdf 전체 분석", "analyse this PDF with agy". For a folder of PNG/JPG page images use agy-slide-analyzer instead. Claude must not open the PDF itself; it consumes the JSON.
---

# agy-pdf-analyzer

Delegates PDF understanding to `agy` and gives Claude text it can trust: every page gets a structured
analysis, validator flags and a second-opinion review. The PDF variant of **agy-slide-analyzer** (same
per-page schema and QA signals), built on the **agy-delegate** skill
(`../agy-delegate/scripts/agy_task.py`, or set `AGY_TASK`).

Commands are relative to this skill's directory. Use `python3` on macOS/Linux, `python` on Windows.

## Workflow

1. **Preflight** (checks `agy` sign-in via agy-delegate, and reports whether `pypdf` is installed):
   ```
   python scripts/pdf_analyze.py preflight
   ```
   If pypdf is missing, `python -m pip install pypdf` (otherwise pass `--total N`; see below).
2. **Pilot on the first chunk**, read the results, then run the whole PDF. Give the personas the subject
   and a one-paragraph context (course, audience, topics) — it measurably improves transcription.
   ```
   python scripts/pdf_analyze.py run --pdf <file.pdf> --pages 1-5 --subject 데이터베이스 --context "SSAFY 데이터베이스 강의 자료. SQL DDL/DML, 모델링, 정규화 포함."
   python scripts/pdf_analyze.py run --pdf <file.pdf> --subject ... --context ...
   ```
   Never pass a page count: it is read from the PDF on every run. `--chunk N` (default 5) is how many
   pages one agy session handles; each chunk is cut out of the PDF so a session only loads its own pages.
   `--chunk 0` sends the whole PDF to a single session (fine for short PDFs only).
   A chunk takes a few minutes; run full PDFs in the background. Runs are resumable: `ok` /
   `needs_attention` pages are skipped unless `--force`; `failed` pages are re-analysed and
   `unreviewed` pages only get the reviewer pass again. `--work` moves agy scratch dirs (default `<out>/.work`).
   A PDF costs ~2 agy sessions per chunk (more with retries) and can exhaust the agy quota.
   When agy reports `RESOURCE_EXHAUSTED`, the run stops at once (queued chunks are left pending, pages
   whose review hit the limit keep their analysis as `unreviewed`) and exits with code 3; tell the user
   the reset time from the log and re-run the same command afterwards.
3. **Read `report.md` first**, then `all_pages.json` (one object per page with a `_qa` block).
   Do not open the PDF yourself — if a page looks wrong, re-run it:
   `run --pdf <file.pdf> --pages N --force`.

Output goes to `<pdf dir>/<pdf stem>_analysis` unless `--out` is given. One output folder belongs to
one PDF (`source.json` holds its sha256); a changed or different PDF is refused unless `--force`.

## Trusting the output

| Signal | Meaning |
|---|---|
| `status: ok` + `review_verdict: pass` | validator clean and the reviewer found no meaningful errors |
| `review_verdict: minor_issues` | usable; see `_qa.review_errors` / `review_missing` for what to patch |
| `status: needs_attention` | still failing after the retry — report it to the user, do not paper over it |
| `status: failed` / `missing` | no analysis yet (agy did not write the page, or the run was cut off) — re-run |
| `concept_ungrounded`, `title_ungrounded` | a term is not in the transcribed text → possible hallucination |
| `fewshot_leak` | the few-shot example was copied (error, triggers retry) |
| `low_confidence`, `[?]` in text, `uncertain` | agy could not read part of the page |
| `printed_page_diff` | printed page number ≠ PDF page index: normal for excerpts/cover pages, but a run of off-by-one values inside a chunk means pages were mixed up |
| `duplicate_content` | same text as another page: a real duplicate slide, or a page mix-up |

When summarising for the user, quote counts from `report.md` and list every non-ok page.

## Rules

- All PDF reading goes through agy. Claude reads only the JSON / report files.
- Read files agy writes (UTF-8); agy's stdout is mangled by the console code page on Windows.
- Prompts live in `prompts/analyst.md` and `prompts/reviewer.md` (`str.format` templates: literal
  braces are doubled). Keep the few-shot examples fictional and keep their marker strings in
  `pdf_validate.FEWSHOT_LEAKS` in sync if you edit them.
- agy runs without `--allow-all`; it only needs to view the PDF and write JSON files.
- Without pypdf (or with `--no-split`) every session is handed the whole PDF and told which pages to
  do — it works, but each session then loads every page, so prefer installing pypdf for long PDFs.
