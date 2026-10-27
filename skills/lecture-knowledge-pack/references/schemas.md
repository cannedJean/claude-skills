# Schemas

## course.json (`kpack.course/1`), written by the user or `init`
```json
{"schema": "kpack.course/1", "course_id": "db", "title": "데이터베이스", "subject": "",
 "lectures": [{"id": "db01", "title": "1강 관계형 모델", "slides": "db01/slides", "analysis": "db01/slide_analysis"}],
 "work": ".kpack", "package": "package"}
```
Ids are `^[a-z][a-z0-9]{1,15}$`. Paths are relative to course.json. Two lectures may not share an analysis folder.
`concept_registry.json` lives next to course.json. For agy-pdf-analyzer output, `slides` is the analysis folder's
`ref/` (`{"slides": "lec1_analysis/ref", "analysis": "lec1_analysis"}`); `init` writes that for folders holding a
`source.json`.

## Ingest record (`.kpack/ingest/<lec>.json`, `kpack.ingest/1`), one per slide image in natural order
`id, lecture, n, stem, file, image, image_sha, analyzer_page, status, text_check, provenance, has_ref, ref_path,
analysis_dir, results_sha, ref_sha, engines, digits_checked, triggers, graphics_ratio, size, analysis, analysis_sha,
qa, boilerplate, build_of, flags, uncertain_count, confidence` (+ `injection_patterns`).
`results_sha` / `ref_sha` are the sha256 of the bytes of `results/<stem>.json` / `ref/<stem>.json` as ingest read
them (null when absent); `kp_ingest.analyzer_changed(rec)` re-hashes both, and status and gate G9 ask for a new
ingest when either differs.

## Struct answers (`.kpack/struct/reads/bNNN.json`), written by a reader subagent
```json
{"i00001": {"language": "sql", "code": "SELECT ...\nWHERE age >= 30;"},
 "i00002": {"rows": [["구분", "파일 시스템", "DBMS"], ["중복", "높음", "최소화"]], "header_rows": 1},
 "i00003": {"diagram1": {"nodes": ["학생", "수강"], "edges": [{"from": "학생", "to": "수강", "label": "1:N", "directed": true}]}},
 "i00004": {"1": "3NF를 만족한다", "2": "[?]"},
 "i00005": {"plain": "y = wx + b", "latex": "y = wx + b"},
 "i00006": {"not_found": true}}
```
Crop item: the answer itself. View item: object keyed by the question keys. Sheet item: object keyed by line numbers.
The file must be one JSON object (a ```json fence around it is tolerated). Anything else is not taken in: `struct`
and `status` list the path under `unreadable_reads`, and the batch stays pending until a fresh subagent re-answers it.

## deck.json (`kpack.deck/1`), written by the integrate subagent
See `prompts/integrate_lecture.md`. `merge` writes `deck.resolved.json` with final concept ids (and the `deck_sha`
of the deck.json it was resolved from).

## check.json (`.kpack/integrate/<lec>/`), written by `integrate`
`{"lecture", "digest_sha", "deck_sha", "valid", "errors"}`. `digest_sha` hashes only the slide part of the digest
(`kp_integrate.current_digest_sha`): the registry and candidate tables are context, so a publish that grows the
registry does not invalidate a deck. `deck_sha` is the sha of the deck.json that was validated. A deck is used only
while both still match.

## concepts.resolved.json (`kpack.concepts/1`), written by `merge`
```json
{"schema": "kpack.concepts/1", "inputs_sha": "...", "decks_sha": "...",
 "concepts": {"2nf": {"term": "2NF", "definition": "...", "definition_slide": "db01-s006", "defined_in_lecture": "db01",
                      "aliases_seen": [], "aliases_derived": ["제2정규형"], "status": "active", "superseded_by": null,
                      "requires": [{"requires": "1nf", "kind": "stated", "evidence": ["db01-s006"]}],
                      "mentions": [{"slide": "db01-s006", "role": "definition"}], "grounded": true,
                      "first_defined": "db01", "first_mentioned": "db01"}}}
```

`decks_sha` (`kp_integrate.decks_sha`) covers every lecture's current deck.json and merge_decisions.json: when it
differs, status says `merge` and the build renders no concepts (G11). A merge that ends in errors deletes
concepts.resolved.json and every deck.resolved.json.

## concept_registry.json (`kpack.registry/1`), append-only, updated only after a publish
```json
{"schema": "kpack.registry/1", "version": 2, "course_id": "db",
 "concepts": {"erd": {"term": "ERD", "aliases": ["entity relationship diagram"], "status": "deprecated",
                      "superseded_by": "dbms", "lecture": "db01", "deprecation_reason": "moved",
                      "history": [{"build": "eb5b952b4443", "change": "created"},
                                  {"build": "1f0c...", "change": "deprecated"}]}}}
```
`lecture` is the defining lecture (shown in each digest's registry table); history changes are `created`,
`alias_added`, `deprecated`, `superseded_by_changed`, `reactivated`, `moved`, `lecture_recorded`.
Learner state key: `<course_id>:<concept_id>` plus the package `build_id`. A published deprecation carries
forward through later merges (a deck need not repeat it); when its successor is retired in turn it is re-pointed at
that successor's successor, so `superseded_by` is always an active concept (gate G6); a successor retired with no
replacement is a merge error until a deck restates the deprecation. A deck that lists a deprecated id as a concept
fails merge.

## Card front-matter (`lectures/<lec>/cards/<id>.md`), one `key: <JSON>` per line
`id, lecture, n, title, slide_type, trust, text_check, digits_checked, struct {region: state}, concepts, section,
image (relative), prev, next, build_of, flags, agy_confidence`

## slides.jsonl row
`{"id", "lecture", "n", "title", "slide_type", "trust", "text_check", "section", "concepts", "card", "image", "build_of", "flags"}`

## concepts.jsonl row
`{"id", "term", "aliases_seen", "aliases_derived", "definition_slide", "defined_in_lecture", "first_defined", "status", "superseded_by", "requires", "slides"}`

## manifest.json (`kpack.manifest/1`)
`{"schema", "build_id", "created", "course_id", "tool": {"version", "shared_dir", "view_max"}, "registry_version",
  "lectures": [{"id", "n_slides", "image_seq_sha", "deck_sha"}], "slides": {id: {"image_sha256", "analysis_sha", "trust", "card"}},
  "counts": {"high", "medium", "low", "image_only"}, "gate": {"G1": true, ...}}`
