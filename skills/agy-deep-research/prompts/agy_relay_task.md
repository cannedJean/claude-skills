Execute the single Gemini Deep Research relay step `{step}` (seq `{seq}`) prepared in `{job_dir}/relay/request.json`.

1. Run the relay operator command:
   `python "{dr_skill_dir}/scripts/relay_step.py" --job "{job_dir}"`
{cdp_recovery}
3. Read `relay/result.json` (or `relay/started.json` when `mode` is `"detach"`) and return its `status`, `step`, `code`, `url`, `plan_count`, `report_chars`, `sources_count`, and a 1-sentence operational `summary`. Never retype or paraphrase the research plan or report body.
