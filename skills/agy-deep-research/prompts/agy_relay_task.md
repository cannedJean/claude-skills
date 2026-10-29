Execute the single Gemini Deep Research relay step `{step}` (seq `{seq}`) prepared in `{job_dir}/relay/request.json`.

1. Run the relay operator command:
   `python "{dr_skill_dir}/scripts/relay_step.py" --job "{job_dir}"`
2. If and only if `relay/result.json` reports `status: "error"` with `code: "CDP_NOT_READY"` or `"CDP_CONNECT_FAILED"` and `prompt_sent: false`, run `python "{dr_skill_dir}/scripts/patch_gemini_app.py"` once and re-run `relay_step.py --job "{job_dir}"` once.
3. Read `relay/result.json` (or `relay/started.json` when `mode` is `"detach"`) and return its `status`, `step`, `code`, `url`, `plan_count`, `report_chars`, `sources_count`, and a 1-sentence operational `summary`. Never retype or paraphrase the research plan or report body.
