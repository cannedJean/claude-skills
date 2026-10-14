# agy-pdf-analyzer

Turn a whole lecture PDF into verified, structured JSON — one record per page — by handing the PDF itself to
the Antigravity CLI (`agy`), and verify each page against a sealed local reference instead of a second agy
look. The PDF sibling of [agy-slide-analyzer](../agy-slide-analyzer/) (which also holds the shared evaluation
code), built on [agy-delegate](../agy-delegate/). Design notes:
[docs/ocr-evaluation-design.md](../../docs/ocr-evaluation-design.md).

```
file.pdf ──> page count ──> chunks of N pages (cut out with pypdf)
  per page   ref/page_XXXX.json  text layer (pypdf extract_text) when the page has one — a trusted reference —
                                 otherwise the embedded page image (pypdf page.images, or a pymupdf render)
                                 through ocr_ref.py (Tesseract / Windows OCR + layout stats)
  per chunk  chunk.pdf ──> agy analyst ──> page_XXXX.json ──> pdf_validate.py ──> ocr_score.py
                              ^                                   │ accept / retry (feedback = missing lines)
                              └── failing pages re-run once ──────┘ escalate → crops.py → review_queue.md → Claude
```

- **Any page count.** The number of pages is read from the PDF on every run; nothing is hard-coded.
- **Analyst prompt** (`prompts/analyst.md`): instructional-designer persona, deck context, a table mapping
  "n-th page of this file → page number → file to write", a 5-step chain of thought per page, two fictional
  few-shot examples, strict JSON schema. One `page_XXXX.json` per page, so a session that dies half-way still
  yields its finished pages.
- **Validator** (`scripts/pdf_validate.py`, no PDF access): schema/types, page identity, few-shot leakage,
  viewer-UI transcribed as content, grounding of title and key concepts, table shape, confidence, duplicates.
- **Reference, scorer, reviewer material**: shared with agy-slide-analyzer (`ocr_ref.py`, `ocr_score.py`,
  `crops.py`); the report / resolve / calibrate / bench commands reuse its code through `slide_analyze.py`.
- `prompts/reviewer.md` is only used by the legacy `--reviewer agy` mode (same-vendor review).

## Requirements

- The [agy-delegate](../agy-delegate/) and [agy-slide-analyzer](../agy-slide-analyzer/) skills installed next
  to this one (or `AGY_TASK=…/agy_task.py`, `OCR_EVAL_DIR=…/agy-slide-analyzer/scripts`), `agy` signed in.
  Antigravity CLI 1.2+ (its `view_file` tool must render PDFs).
- Python 3.9+, Pillow; numpy recommended.
- [pypdf](https://pypi.org/project/pypdf/) (`python -m pip install pypdf`) to count pages, cut chunks and
  extract page images. Optional [pymupdf](https://pypi.org/project/PyMuPDF/) renders pages whose embedded
  image cannot be extracted. Without pypdf pass `--total N`; pages then have no reference (`unverified`).
- An OCR engine for scanned pages: see agy-slide-analyzer's README (Tesseract `kor` / Windows OCR `winsdk`).

## Use

```bash
python scripts/pdf_analyze.py preflight
python scripts/pdf_analyze.py run --pdf ./lm/database.pdf --pages 1-5 \
  --subject 데이터베이스 --context "SSAFY 데이터베이스 강의. SQL DDL/DML, 모델링, 정규화."
python scripts/pdf_analyze.py run --pdf ./lm/database.pdf --subject 데이터베이스 --context "..."
# Claude answers review_queue.md into crop_reads.json, then:
python scripts/pdf_analyze.py resolve --pdf ./lm/database.pdf
python scripts/pdf_analyze.py report --pdf ./lm/database.pdf
```

Output (default `<pdf dir>/<pdf stem>_analysis`):

```
results/page_XXXX.json  per-page record: status, decision, attempts, analysis, validation, score, queue, judgments
ref/page_XXXX.json|png  sealed reference and the extracted page image (when available)
crops/*.png             line crops and thumbnails for the reviewer
review_queue.md/json    the reviewer's blind work list; crop_reads.json / grounding_reads.json are its answers
all_pages.json          all analyses, each with a `_qa` block
report.md               QA table, one row per page, plus queue size / image-token estimate / audit line
source.json             name, sha256 and page count of the analysed PDF
.work/                  chunk PDFs, agy work directories and rendered prompts (safe to delete)
```

The per-page `analysis` has the same schema as agy-slide-analyzer's, so downstream consumers can read both.

## Commands and options

| Command | Purpose |
|---|---|
| `run --pdf FILE` | reference → analyst → validate → score → (retry) → queue; resumable |
| `reference --pdf FILE` | build the sealed page references only (no agy) |
| `resolve --pdf FILE` | apply `crop_reads.json` / `grounding_reads.json`, set final statuses, rebuild the report |
| `report --pdf FILE` | rebuild `all_pages.json`, `report.md` and the review queue |
| `rescore --pdf FILE` | re-run validator + scorer on the stored analyses without agy |
| `calibrate --pdf FILE --gold DIR` | fit thresholds on human-corrected `page_XXXX.json` files |
| `bench --pdf FILE --gold DIR --label X` | true recall / precision / similarity / digit / cell accuracy per run |

| Flag (`run`) | Meaning |
|---|---|
| `--out DIR`, `--pages 1-10,15` | output folder, subset (1-based PDF page numbers) |
| `--chunk N` | pages per agy session (default 5; `0` = the whole PDF in one session) |
| `--no-split` | stage the whole PDF in every session instead of a cut-out of its pages |
| `--total N` | page count, only when pypdf is not installed |
| `--subject`, `--context` | fills the analyst persona and deck context |
| `--engine auto\|winocr,tesseract` | OCR engines for scanned pages |
| `--audit-rate 0.1`, `--grounding-check flagged\|all\|none`, `--thresholds FILE` | as in agy-slide-analyzer |
| `-j/--jobs N`, `--timeout MIN`, `--model` | parallel sessions (default 3), minutes per agy call (20), agy model slug |
| `--force` | redo pages that already have results |
| `--reviewer agy` | legacy mode: an agy reviewer session per chunk instead of the reference + Claude flow |
| `--work DIR` | where chunk PDFs and agy work directories go |

Chunks sit on a fixed grid (`1-5`, `6-10`, … for `--chunk 5`), so `--pages 7` re-runs page 7 inside its `6-10`
chunk PDF with the neighbouring pages visible as context. An output folder is tied to one PDF by sha256;
pointing it at a different PDF is refused unless `--force`.

**Quota:** one agy session per chunk, two when a page in it retries. On `RESOURCE_EXHAUSTED` the run stops
immediately, pages already written are scored and kept, the log shows agy's "Resets in …", exit code 3.

Exit codes: 0 every page accepted, 2 some pages escalated / need attention / failed, 3 agy quota exhausted
(resumable), 4 agy-delegate or shared code missing / page count unknown, 5 bad arguments.
