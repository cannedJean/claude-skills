# agy-slide-analyzer

Turn a folder of lecture slide images into verified, structured JSON by delegating each image to the
Antigravity CLI (`agy`), and verify every result **without a second LLM look at the image** and **without
the analyst's vendor reviewing itself**. Built on [agy-delegate](../agy-delegate/). Design notes:
[docs/ocr-evaluation-design.md](../../docs/ocr-evaluation-design.md).

```
slide.png ──> [0] ocr_ref.py  local OCR + layout stats, sealed with the image sha256 (before agy runs)
          ──> [1] agy analyst ──> slide_analysis.json
          ──> [2] slide_validate.py (schema, grounding, leaks)
          ──> [3] ocr_score.py  analysis vs. reference ──> accept | retry (feedback = missing OCR lines) | escalate
          ──> [4] crops.py     escalate: crops of disputed lines / 640px thumbnail ──> review_queue.md ──> Claude (blind)
                               audit: a deterministic sample of accepted slides gets a thumbnail read too
          ──> [5] resolve      Claude's crop_reads.json vs. the analysis ──> accepted | needs_attention
```

- **Reference** (`scripts/ocr_ref.py`): Tesseract (`tesseract` binary, `kor+eng`, word confidences) and/or
  Windows OCR (`winsdk`, no confidences). With two engines, words both read get weight `high`; one engine's
  unconfirmed words stay `mid`/`low`. Layout statistics: ink density, non-text ink (graphics), ruling lines,
  table-like word grids, title band, dHash.
- **Scorer** (`scripts/ocr_score.py`): weighted OCR recall (character-bigram matching, so per-syllable OCR
  splits do not matter), unsupported share of the analysis text, digit and Latin-identifier recall, structure
  triggers; a decision policy, Korean retry feedback, gold-set metrics and threshold fitting.
- **Reviewer material** (`scripts/crops.py`): what Claude is allowed to see and how its blind answers are
  judged (numbers and identifiers must match exactly; a thumbnail read also flags analysis text neither the
  OCR nor Claude saw).
- **Analyst prompt** (`prompts/analyst.md`): instructional-designer persona, deck context, 5-step chain of
  thought, two fictional few-shot examples, strict JSON schema. `prompts/reviewer.md` is only used by the
  legacy `--reviewer agy` mode.

## Requirements

- The [agy-delegate](../agy-delegate/) skill installed next to this one (or `AGY_TASK=/path/to/agy_task.py`)
  and `agy` signed in (see its README).
- Python 3.9+, Pillow (`pip install pillow`); numpy recommended (layout statistics).
- One OCR engine (both are better):
  - **Tesseract** with Korean: Windows installer from the UB-Mannheim build (tick *Korean* under additional
    language data), `brew install tesseract tesseract-lang`, or `apt install tesseract-ocr tesseract-ocr-kor`.
    Found on PATH, via `TESSERACT_BIN`, or in the default install folder. No Python package needed.
  - **Windows OCR** (Windows 10/11): `pip install winsdk` and the Korean OCR language pack
    (Settings → Language → 한국어 → optional features → OCR; present when `C:\Windows\OCR\ko-kr` exists).
- Nothing is installed automatically; `preflight` reports what it found.

## Use

```bash
python scripts/slide_analyze.py preflight
python scripts/slide_analyze.py run --slides ./lm/slides --pages 1-3 \
  --subject 데이터베이스 --context "SSAFY 데이터베이스 강의. SQL DDL/DML, 모델링, 정규화."
python scripts/slide_analyze.py run --slides ./lm/slides -j 4 --subject 데이터베이스 --context "..."
# Claude answers review_queue.md into crop_reads.json, then:
python scripts/slide_analyze.py resolve --slides ./lm/slides
python scripts/slide_analyze.py report --slides ./lm/slides
```

Images are sorted by file name; that order is the page number. Output (default `<slides>/../slide_analysis`):

```
results/<stem>.json   per-slide record: status, decision, attempts, analysis, validation, score, queue, judgments
ref/<stem>.json       sealed OCR reference (lines, word boxes, weights, layout stats, image sha256)
crops/*.png           line crops and thumbnails for the reviewer
review_queue.md/json  the reviewer's blind work list; crop_reads.json / grounding_reads.json are its answers
all_slides.json       all analyses, each with a `_qa` block (status, decision, metrics, judgments, flags)
report.md             QA table, one row per slide, plus queue size / image-token estimate / audit line
calibration.json      written by `calibrate`; thresholds picked up by later runs
.work/                agy work directories and rendered prompts (safe to delete)
```

## Commands and options

| Command | Purpose |
|---|---|
| `run --slides DIR` | reference → analyst → validate → score → (retry) → queue; resumable |
| `reference --slides DIR` | build the sealed references only (no agy); useful before a long run |
| `resolve --slides DIR` | apply `crop_reads.json` / `grounding_reads.json`, set final statuses, rebuild the report |
| `report --slides DIR` | rebuild `all_slides.json`, `report.md` and the review queue from `results/` |
| `rescore --slides DIR` | re-run validator + scorer on the stored analyses without agy (after `calibrate`, a scorer change or a reference rebuild); re-queues escalations from scratch |
| `calibrate --slides DIR --gold DIR` | fit thresholds on human-corrected analyses (`<stem>.json`), compare OCR engines |
| `bench --slides DIR --gold DIR --label X` | true recall / precision / similarity / digit / cell accuracy per run |

| Flag (`run`) | Meaning |
|---|---|
| `--out DIR`, `--pages 1-10,15` | output folder, subset (1-based) |
| `--subject`, `--context` | fills the analyst persona and deck context |
| `--engine auto\|winocr,tesseract` | OCR engines for the reference (default: every available one; `winocr` first) |
| `--audit-rate 0.1` | share of accepted slides queued for a blind thumbnail read (at least 2) |
| `--grounding-check flagged\|all\|none` | which slides go to the text-only grounding queue |
| `--thresholds FILE` | thresholds JSON (default `<out>/calibration.json`, then the skill's `calibration.json`, then built-in) |
| `-j/--jobs N`, `--timeout MIN`, `--model` | parallel sessions (default 4), minutes per agy call (10), agy model slug |
| `--force` | redo slides that already have results |
| `--reviewer agy` | legacy mode: an agy reviewer session per slide instead of the reference + Claude flow |
| `--work DIR` | where agy work directories go |

Exit codes: 0 every slide accepted, 2 some slides escalated / need attention / failed, 3 agy quota exhausted
(resumable), 4 agy-delegate missing or no OCR engine for `reference`, 5 bad arguments.

**Quota:** one agy session per slide, two when a retry fires (the legacy mode costs two to four). On
`RESOURCE_EXHAUSTED` the run stops immediately, the log shows agy's "Resets in …", exit code 3; re-run later.

## Tests

```bash
python -m unittest discover -s scripts/tests -v
```

Pure scorer/judge tests always run; engine and pipeline tests run when Pillow and an OCR engine are present.
The pipeline tests use `scripts/tests/fake_agy_task.py` (set `AGY_TASK` to it to exercise the flow without agy).
