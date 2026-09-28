# agy-slide-analyzer

Turn a folder of lecture slide images into verified, structured JSON by delegating each image to the
Antigravity CLI (`agy`). Built on [agy-delegate](../agy-delegate/).

```
slide image ──> agy analyst ──> slide_validate.py ──> agy reviewer ──> ok
                    ^                  │ errors             │ major_issues
                    └──── retry once with feedback ─────────┘
```

- **Analyst prompt** (`prompts/analyst.md`): instructional-designer persona, deck context, a 5-step
  chain of thought (observe → transcribe → structure → interpret → self-check, kept in `reasoning`),
  two fictional few-shot examples, strict JSON schema.
- **Validator** (`scripts/slide_validate.py`, no image access): schema/types, page/file identity,
  few-shot leakage, viewer-UI transcribed as content, grounding of title and key concepts in the
  transcribed text, table shape, confidence, cross-slide duplicate detection.
- **Reviewer prompt** (`prompts/reviewer.md`): QA persona that observes the image *before* reading
  the analysis, then returns `verdict`, `text_recall`, `errors`, `missing`, `hallucinated`.

## Requirements

- The [agy-delegate](../agy-delegate/) skill installed next to this one (or `AGY_TASK=/path/to/agy_task.py`)
  and `agy` signed in (see its README).
- Python 3.9+, stdlib only.

## Use

```bash
python scripts/slide_analyze.py preflight
python scripts/slide_analyze.py run --slides ./lm/slides --pages 1-3 \
  --subject 데이터베이스 --context "SSAFY 데이터베이스 강의. SQL DDL/DML, 모델링, 정규화."
python scripts/slide_analyze.py run --slides ./lm/slides -j 4 --subject 데이터베이스 --context "..."
python scripts/slide_analyze.py report --slides ./lm/slides
```

Images are sorted by file name; that order is the page number. Output (default `<slides>/../slide_analysis`):

```
results/<stem>.json   per-slide record: status, attempts, analysis, validation, review
all_slides.json       all analyses, each with a `_qa` block (status, verdict, recall, flags)
report.md             QA table, one row per slide
.work/                agy work directories and rendered prompts (safe to delete)
```

## Options (`run`)

| Flag | Meaning |
|---|---|
| `--slides DIR` | image folder (png/jpg/jpeg/webp) |
| `--out DIR` | output folder |
| `--pages 1-10,15` | subset (1-based) |
| `--subject`, `--context` | fills the analyst/reviewer personas and deck context |
| `-j/--jobs N` | parallel agy sessions (default 4) |
| `--timeout MIN` | per agy call (default 10) |
| `--model` | agy model slug (`agy models`) |
| `--force` | redo slides that already have results |
| `--skip-review` | analyst + validator only (half the cost, weaker verification) |
| `--work DIR` | where agy work directories go |

Exit codes: 0 every slide ok, 2 some slides need attention or failed, 4 agy-delegate missing, 5 bad arguments.
