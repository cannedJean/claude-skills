// Download what the collector's checkpoint holds right now, as ssafy_project_manifest.json.
// Paste as the `text` of javascript_tool on any https://project.ssafy.com page. Use it when a
// run cannot be resumed, or to sync early. sync.py merges every manifest in Downloads.
let cp = null;
try { cp = JSON.parse(localStorage.getItem('ssafy_collector_v1') || 'null'); } catch (e) {}
if (!cp) { ({ error: 'no checkpoint on this site' }); } else {
  const out = { site: 'project.ssafy.com', collectedAt: cp.savedAt, partial: true, opts: cp.opts, courses: cp.courses, problems: Object.values(cp.results), errors: [] };
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], { type: 'application/json' }));
  a.download = 'ssafy_project_manifest.json';
  document.body.appendChild(a); a.click(); a.remove();
  ({ downloaded: 'ssafy_project_manifest.json', problems: out.problems.length, withRepo: out.problems.filter(p => p.repo).length });
}
