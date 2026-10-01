# agy-pdf-analyzer

Turn a whole lecture PDF into verified, structured JSON — one record per page — by handing the PDF
itself to the Antigravity CLI (`agy`). No page images: agy's file viewer renders PDF pages directly.
The PDF sibling of [agy-slide-analyzer](../agy-slide-analyzer/), built on [agy-delegate](../agy-delegate/).

```
file.pdf ──> page count ──> chunks of N pages (cut out with pypdf)
chunk.pdf ──> agy analyst ──> pdf_validate.py ──> agy reviewer ──> ok
                  ^                │ errors            │ major_issues      (all per page)
                  └── retry the failing pages once with feedback ──┘
```

- **Any page count.** The number of pages is read from the PDF on every run; nothing is hard-coded.
- **Analyst prompt** (`prompts/analyst.md`): instructional-designer persona, deck context, a table
  mapping "n-th page of this file → page number → file to write", a 5-step chain of thought per page
  (observe → transcribe → structure → interpret → self-check, kept in `reasoning`), two fictional
  few-shot examples, strict JSON schema. One `page_XXXX.json` per page, so a session that dies
  half-way still yields its finished pages.
- **Validator** (`scripts/pdf_validate.py`, no PDF access): schema/types, page identity, few-shot
  leakage, viewer-UI transcribed as content, grounding of title and key concepts in the transcribed
  text, table shape, confidence, cross-page duplicate detection.
- **Reviewer prompt** (`prompts/reviewer.md`): QA persona that looks at the PDF pages *before* reading
  the analyses, then returns `verdict`, `text_recall`, `errors`, `missing`, `hallucinated` per page.

## Requirements

- The [agy-delegate](../agy-delegate/) skill installed next to this one (or `AGY_TASK=/path/to/agy_task.py`)
  and `agy` signed in (see its README). Antigravity CLI 1.2+ (its `view_file` tool must render PDFs).
- Python 3.9+ and [pypdf](https://pypi.org/project/pypdf/) (`python -m pip install pypdf`) to count
  pages and cut the chunks. Without pypdf pass `--total N`; every session then gets the whole PDF.

## Use

```bash
python scripts/pdf_analyze.py preflight
python scripts/pdf_analyze.py run --pdf ./lm/database.pdf --pages 1-5 \
  --subject 데이터베이스 --context "SSAFY 데이터베이스 강의. SQL DDL/DML, 모델링, 정규화."
python scripts/pdf_analyze.py run --pdf ./lm/database.pdf --subject 데이터베이스 --context "..."
python scripts/pdf_analyze.py report --pdf ./lm/database.pdf
```

Output (default `<pdf dir>/<pdf stem>_analysis`):

```
results/page_XXXX.json  per-page record: page, file, status, attempts, analysis, validation, review
all_pages.json          all analyses, each with a `_qa` block (status, verdict, recall, flags)
report.md               QA table, one row per page
source.json             name, sha256 and page count of the analysed PDF
.work/                  chunk PDFs, agy work directories and rendered prompts (safe to delete)
```

The per-page `analysis` has the same schema as agy-slide-analyzer's, so downstream consumers can read both.

## Options (`run`)

| Flag | Meaning |
|---|---|
| `--pdf FILE` | the PDF to analyse (every page unless `--pages`) |
| `--out DIR` | output folder |
| `--pages 1-10,15` | subset (1-based PDF page numbers) |
| `--chunk N` | pages per agy session (default 5; `0` = the whole PDF in one session) |
| `--no-split` | stage the whole PDF in every session instead of a cut-out of its pages |
| `--total N` | page count, only when pypdf is not installed |
| `--subject`, `--context` | fills the analyst/reviewer personas and deck context |
| `-j/--jobs N` | parallel agy sessions (default 3) |
| `--timeout MIN` | per agy call (default 20) |
| `--model` | agy model slug (`agy models`) |
| `--force` | redo pages that already have results |
| `--skip-review` | analyst + validator only (half the cost, weaker verification) |
| `--work DIR` | where chunk PDFs and agy work directories go |

Chunks sit on a fixed grid (`1-5`, `6-10`, … for `--chunk 5`), so `--pages 7` re-runs page 7 inside
its `6-10` chunk PDF with the neighbouring pages visible as context. Larger chunks mean fewer agy
sessions but more for one session to keep apart; 3–8 is a sensible range.

Re-running the same command resumes: `failed` pages are re-analysed, `unreviewed` pages only get the
reviewer pass, everything else is skipped (`--force` redoes them). An output folder is tied to one PDF
by sha256; pointing it at a different PDF is refused unless `--force`.

**Quota:** each chunk costs ~2 agy sessions. When agy returns `RESOURCE_EXHAUSTED` (HTTP 429), the run
stops immediately instead of burning through the queue: in-flight chunks finish, queued chunks are left
untouched, pages whose review was cut off keep their analysis as `unreviewed`, the log shows agy's
"Resets in …" time, and the exit code is 3. Re-run after the reset.

Exit codes: 0 every page ok, 2 some pages need attention or failed, 3 agy quota exhausted (resumable),
4 agy-delegate missing / page count unknown, 5 bad arguments.
