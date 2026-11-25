// Poll the job started by project_collector.js. Waits up to 30 s for it to finish
// (the javascript tool times out at 45 s), then returns a short status.
// To stop a running job early: window.__ssafy.cancel = true
document.title = 'SSAFY collector'; // the site's DRM script appends to the title on every iframe load
const S = window.__ssafy;
if (!S) {
  let cp = null; try { cp = JSON.parse(localStorage.getItem('ssafy_collector_v1') || 'null'); } catch (e) {}
  ({ running: false, error: 'no job on this tab (it navigated or was reloaded)',
     checkpoint: cp ? { savedAt: cp.savedAt, rows: cp.todo.length, results: Object.keys(cp.results).length,
       ok: Object.values(cp.results).filter(r => !r.error).length } : null,
     next: cp ? 'paste project_collector.js again with the same courses option to resume' : 'paste project_collector.js to start' });
} else {
  const t0 = Date.now();
  while (S.running && Date.now() - t0 < 30000) await new Promise(r => setTimeout(r, 1000));
  const res = Object.values(S.results);
  ({ running: S.running, resumed: S.resumed, phase: S.progress.phase, done: S.progress.done, total: S.progress.total,
     current: S.progress.current, courses: S.courses.length,
     started: res.filter(p => p.started).length, withRepo: res.filter(p => p.repo).length,
     problemErrors: res.filter(p => p.error).length,
     errors: S.errors.slice(-3), errorCount: S.errors.length,
     downloaded: S.finished && S.opts.download ? 'ssafy_project_manifest.json' : null });
}
