# agy-pdf-analyzer

Turn a whole lecture PDF into verified, structured JSON — one record per page — by handing the PDF itself to
the Antigravity CLI (`agy`), and verify each page against a sealed local reference instead of a second agy
look. The PDF sibling of [agy-slide-analyzer](../agy-slide-analyzer/) (which also holds the shared evaluation
code), built on [agy-delegate](../agy-delegate/). Design notes:
[docs/ocr-evaluation-design.md](../../docs/ocr-evaluation-design.md).

```
file.pdf ──> page count ──> chunks of N pages (cut out with pypdf)
  every page ref/page_XXXX.json  text layer (pypdf extract_text) when the page has one — a trusted reference —
                                 otherwise the embedded page image (pypdf page.images, or a pymupdf render)
                                 through ocr_ref.py (Tesseract / Windows OCR + layout stats); all sealed before
                                 the first agy session, template lines of the OCR'd pages set aside
  per chunk  chunk.pdf ──> agy analyst ──> page_XXXX.json ──> pdf_validate.py ──> ocr_score.py
                              ^                                   │ accept / retry (feedback = missing lines)
                              └── failing pages re-run once ──────┘ escalate → crops.py → review_queue.md → Claude
```

- **Any page count.** The number of pages is read from the PDF on every run; nothing is hard-coded.
- **Analyst prompt** (`prompts/analyst.md`): instructional-designer persona, deck context, a table mapping
  "n-th page of this file → page number → file to write", a 5-step chain of thought per page, three fictional
  few-shot examples (table, SQL, code + capture + highlight), strict JSON schema. One `page_XXXX.json` per
  page, so a session that dies half-way still yields its finished pages. Same rules as agy-slide-analyzer's
  prompt: captures are `screenshot` diagrams with their UI labels, highlight marks `annotation` diagrams,
  labelled code breakdowns `syntax_breakdown`; text moved into code / tables / diagrams is not repeated in
  `verbatim_text`; title-only pages get no exam points.
- **Validator** (`scripts/pdf_validate.py`, no PDF access; the mirror of `slide_validate.py` without the
  `file` key): schema/types (a malformed field is a `bad_type` / `bad_table` error, never a crash), page
  identity, few-shot leakage, viewer-UI transcribed as content, grounding of title and key concepts, table
  shape, confidence, and the prompt-rule warnings `interpretation_on_title_only`, `visual_undeclared`,
  `code_duplicated`, `illustration_with_relations`, `divider_with_body`.
- **Reference, scorer, reviewer material**: shared with agy-slide-analyzer (`ocr_ref.py`, `ocr_score.py`,
  `crops.py`), including the variant OCR pass on single-engine machines, deck-template lines and the operator
  / number / line-order checks; the report / resolve / queue / calibrate / bench commands reuse its code
  through `slide_analyze.py`. A text-layer reference has no word boxes, so its layout says
  `text_masked: false`: the graphics trigger stays off for that page and the table trigger uses the
  version 2 keys (a ruled grid in the page image still asks for a `tables` entry). The text layer is exact, so
  an operator or number on an anchored line that differs from it sends the page back to the analyst (retry,
  then `needs_attention`); no thumbnail read can overrule it.
- `prompts/reviewer.md` is only used by the legacy `--reviewer agy` mode (same-vendor review).

## Requirements

- The [agy-delegate](../agy-delegate/) and [agy-slide-analyzer](../agy-slide-analyzer/) skills installed next
  to this one (or `AGY_TASK=…/agy_task.py`, `OCR_EVAL_DIR=…/agy-slide-analyzer/scripts`), `agy` signed in.
  Antigravity CLI 1.2+ (its `view_file` tool must render PDFs).
- Python 3.9+, Pillow; numpy recommended.
- [pypdf](https://pypi.org/project/pypdf/) (`python -m pip install pypdf`) to count pages, cut chunks and
  extract page images. Optional [pymupdf](https://pypi.org/project/PyMuPDF/) renders pages whose embedded
  image cannot be extracted. Without pypdf pass `--total N`; pages then have no reference (`unverified`).
- An OCR engine for scanned pages: see agy-slide-analyzer's README (Tesseract `kor` / Windows OCR `winsdk`;
  with Windows OCR alone a 2x upscaled second pass is added, `OCR_SCALES`).

## Use

```bash
python scripts/pdf_analyze.py preflight
python scripts/pdf_analyze.py run --pdf ./lm/database.pdf --pages 1-5 \
  --subject 데이터베이스 --context "SSAFY 데이터베이스 강의. SQL DDL/DML, 모델링, 정규화."
python scripts/pdf_analyze.py run --pdf ./lm/database.pdf --subject 데이터베이스 --context "..."
# Claude answers review_queue.md into crop_reads.json (or, split: queue --shards 3, crop_reads_<k>.json), then:
python scripts/pdf_analyze.py resolve --pdf ./lm/database.pdf
python scripts/pdf_analyze.py report --pdf ./lm/database.pdf
```

Output (default `<pdf dir>/<pdf stem>_analysis`):

```
results/page_XXXX.json  per-page record: status, decision, attempts, analysis, validation, score, score_legacy, queue, judgments
ref/page_XXXX.json|png  sealed reference and the extracted page image (when available)
crops/*.png             line crops and thumbnails for the reviewer
review_queue.md/json    the reviewer's blind work list; crop_reads.json / grounding_reads.json are its answers
review_queue_<k>.md     `queue --shards N`: disjoint parts, answered in crop_reads_<k>.json
all_pages.json          all analyses of the PDF, each with a `_qa` block
report.md               QA table, one row per page, plus queue size / token estimates / audit lines (shadow stratum too)
source.json             name, sha256 and page count of the analysed PDF
.work/                  chunk PDFs, agy work directories and rendered prompts (safe to delete)
```

The per-page `analysis` has the same schema as agy-slide-analyzer's, so downstream consumers can read both.
Results, text-layer references, page images and `source.json` are written atomically; an unreadable results
file is redone and a damaged page image (a crash mid-save) is extracted again, never fatal.

## Commands and options

| Command | Purpose |
|---|---|
| `run --pdf FILE` | references (every page) → analyst → validate → score → (retry) → queue; resumable |
| `reference --pdf FILE` | build the sealed page references only (no agy) |
| `resolve --pdf FILE` | apply `crop_reads*.json` / `grounding_reads.json`, set final statuses, rebuild the report |
| `queue --pdf FILE --shards N` | rebuild the report and split the review queue into N disjoint files |
| `report --pdf FILE` | rebuild `all_pages.json`, `report.md` and the review queue |
| `rescore --pdf FILE` | re-run validator + scorer on the stored analyses without agy (may rebuild references) |
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
| `--shadow-audit-rate 0.3` | shadow mode, as in agy-slide-analyzer: share of the pages only the new scorer rules accept that get a blind audit read; `0` = off once two PDFs show no issue there (also on `report`, `resolve`, `rescore`, `queue`) |
| `-j/--jobs N`, `--timeout MIN`, `--model` | parallel sessions (default 3), minutes per agy call (20), agy model slug |
| `--force` | redo pages that already have results (and take over a folder of another PDF) |
| `--reviewer agy` | legacy mode: an agy reviewer session per chunk instead of the reference + Claude flow |
| `--work DIR` | where chunk PDFs and agy work directories go |

Chunks sit on a fixed grid (`1-5`, `6-10`, … for `--chunk 5`), so `--pages 7` re-runs page 7 inside its `6-10`
chunk PDF with the neighbouring pages visible as context. An output folder is tied to one PDF by sha256:
`run`, `reference` and `rescore` refuse a folder holding another PDF's results (exit 5); `run --force` takes it
over and drops the old page images so they are extracted again.

**Reference rebuilds:** a reference older than the shared `REF_VERSION` 3 is rebuilt when its page is
re-analysed, by `reference` and by `rescore` (text-layer pages: no OCR); `run` does not touch the references of
finished pages. A scanned page whose record holds Claude's judgments or queued line crops keeps its old
reference until `rescore`, because those ids name its line ids.

**Deck boilerplate and settling:** header/footer words that recur in the header/footer items of 3+ pages (the
course logo, a section band) leave the unsupported share, as in agy-slide-analyzer. A chunk's pages are scored
against the pages on disk plus the chunk itself; after the last chunk `run` re-scores the pages it analysed
against every page (no agy; a changed decision is re-queued, and a would-be retry keeps its stored decision), so
`run` and `rescore` decide alike.

**Quota:** one agy session per chunk, two when a page in it retries. On `RESOURCE_EXHAUSTED` the run stops
immediately; pages already written are scored and kept, and a page whose retry hit the quota keeps its first
analysis (`needs_attention`) instead of costing another call. The log shows agy's "Resets in …", exit code 3.

Exit codes: 0 every page accepted, 2 some pages escalated / need attention / failed, 3 agy quota exhausted
(resumable), 4 agy-delegate or shared code missing, the shared code older than this skill (REF_VERSION < 3) or
page count unknown, 5 bad arguments, a folder of another PDF, or two different answers for one queue id. stdout
is UTF-8 JSON whatever the console code page.

## Tests

```bash
python -m unittest discover -s scripts/tests -v
```

`scripts/tests/test_pdf.py`: validator guards and warnings, the prompt (placeholders, few-shot markers, its
examples pass the validator, enums identical to agy-slide-analyzer's), and the pipeline on a synthetic 3-page
image PDF with a fake agy (`scripts/tests/fake_agy_pdf.py`, which reuses agy-slide-analyzer's
`tests/fake_agy_task.py`): cp949 console, truncated result, quota during a retry, another PDF in the same
folder, every reference sealed first, queue shards, text-layer references, the boilerplate of this PDF only and
settling every page (the contract test also checks that `run` settles on the decisions of `rescore` and writes
`score_legacy`). The pipeline tests need agy-slide-analyzer next to this skill, Pillow and pypdf, and are
skipped otherwise. agy-slide-analyzer's
`tests/test_contract.py` runs this skill's commands against the shared code too.
