---
name: agy-pdf-analyzer
description: Analyse a whole lecture PDF (slides or handouts, any number of pages) by handing the PDF itself to the Antigravity CLI (`agy`) — no conversion to page images for agy. The page count is read from the PDF, pages are sent a few at a time to an agy analyst session (persona + deck context + chain-of-thought + few-shot) that writes structured JSON per page, and every page is verified WITHOUT a second LLM look: a sealed per-page reference (the page's text layer when it has one, otherwise the embedded page image through local OCR) is compared with the JSON by a deterministic scorer, omissions are retried with OCR-derived feedback, and only disputed lines are cropped for Claude to read blind. Use when the user has a `.pdf` of lecture material and wants it transcribed, summarised or turned into study notes — "이 PDF agy로 분석해줘", "강의자료 pdf 전체 분석", "analyse this PDF with agy". For a folder of PNG/JPG page images use agy-slide-analyzer instead. Claude never opens the PDF; it opens only the crops and thumbnails listed in review_queue.md.
---

# agy-pdf-analyzer

The PDF variant of **agy-slide-analyzer** (same per-page schema, same QA signals, same review protocol); the
evaluation code (`ocr_ref.py`, `ocr_score.py`, `crops.py`) is shared from that skill's `scripts/`, so install
both (or set `OCR_EVAL_DIR`). Built on **agy-delegate** (`../agy-delegate/scripts/agy_task.py`, or `AGY_TASK`).

```
file.pdf ──> page count ──> chunks of N pages (cut with pypdf)
  per page  [0] reference: text layer (trusted) or embedded page image → local OCR + layout, sealed
  per chunk [1] agy analyst ──> page_XXXX.json ──> [2] pdf_validate ──> [3] scorer
                 ^                                      │ accept → done     │ retry (feedback = missing lines)
                 └──── failing pages re-run once ───────┘                   │ escalate → crops/thumbnail → Claude (blind)
```

Commands are relative to this skill's directory. Use `python3` on macOS/Linux, `python` on Windows.

## Workflow

1. **Preflight** (agy sign-in, pypdf, OCR engines, shared code):
   ```
   python scripts/pdf_analyze.py preflight
   ```
   If pypdf is missing, `python -m pip install pypdf` (otherwise pass `--total N`, every session then loads the
   whole PDF and no page images can be extracted → pages end `unverified`).
2. **Pilot on the first chunk**, read the results, then run the whole PDF. Give the personas the subject and a
   one-paragraph context.
   ```
   python scripts/pdf_analyze.py run --pdf <file.pdf> --pages 1-5 --subject 데이터베이스 --context "SSAFY 데이터베이스 강의 자료. SQL DDL/DML, 모델링, 정규화 포함."
   python scripts/pdf_analyze.py run --pdf <file.pdf> --subject ... --context ...
   ```
   Never pass a page count: it is read from the PDF. `--chunk N` (default 5) pages per agy session; `--chunk 0`
   sends the whole PDF to one session. One agy session per chunk (two when some page retries). Resumable: only
   `failed` pages are redone unless `--force`. On agy `RESOURCE_EXHAUSTED` the run stops (exit 3); re-run later.
3. **Read `report.md`**, then `all_pages.json` (one object per page with a `_qa` block). **Do not open the PDF.**
4. **Work the review queue (blind)** exactly as in agy-slide-analyzer's SKILL.md step 4: open only the files
   listed in `review_queue.md`, write `crop_reads.json` (and `grounding_reads.json` if `grounding_queue.md`
   exists), then
   ```
   python scripts/pdf_analyze.py resolve --pdf <file.pdf>
   ```
5. **Report to the user**: counts from `report.md`, every non-`accepted` page with its reason, the audit line.

Output goes to `<pdf dir>/<pdf stem>_analysis` unless `--out` is given; one output folder belongs to one PDF
(`source.json` holds its sha256).

## Trusting the output

Same table as agy-slide-analyzer, plus:

| Signal | Meaning |
|---|---|
| `ref/page_XXXX.json` with `primary: pdf_text` | the page had a text layer; the reference is exact, disagreements are real |
| `primary: tesseract` / `winocr` | scanned page: the reference is OCR, disputed lines went to crops |
| `status: unverified` | no text layer and no page image could be extracted (no pypdf/pymupdf, or an unusual encoding) — the page is unverified, say so |
| `printed_page_diff` | printed page number ≠ PDF page index: normal for excerpts/cover pages, but a run of off-by-one values inside a chunk means pages were mixed up; the per-page reference makes a mix-up show as a recall collapse |
| `duplicate_content` | same text as another page: a real duplicate, or a page mix-up |

## Calibration and benchmarking

As in agy-slide-analyzer, with gold files named `page_XXXX.json`:
```
python scripts/pdf_analyze.py calibrate --pdf <file.pdf> --gold gold/
python scripts/pdf_analyze.py bench --pdf <file.pdf> --gold gold/ --label <variant>
python scripts/pdf_analyze.py rescore --pdf <file.pdf>      # re-score stored analyses without agy
```

## Rules

- All PDF reading goes through agy and local tools. Claude reads JSON/report files and the queue's crops only.
- Answer the queue before looking at the page's analysis JSON (blind); type what the crop shows.
- Read files agy writes (UTF-8); agy's stdout is mangled by the console code page on Windows.
- Prompts live in `prompts/analyst.md` (`str.format` template: literal braces doubled). Keep the few-shot
  examples fictional and their markers in `pdf_validate.FEWSHOT_LEAKS` in sync. `prompts/reviewer.md` is only
  used by the legacy `--reviewer agy` mode.
- agy runs without `--allow-all`; it only needs to view the PDF and write JSON files.
- Without pypdf (or with `--no-split`) every session is handed the whole PDF and told which pages to do;
  prefer installing pypdf for long PDFs and for page images.
