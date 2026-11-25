// lab.ssafy.com lister. Paste as the `text` of mcp__claude-in-chrome__javascript_tool
// on a tab showing any https://lab.ssafy.com page (signed in).
// Uses GitLab's REST API with the page's own session cookie (same-origin fetch);
// no token is read or created. Saves ssafy_lab_manifest.json through Chrome's download
// and returns a short summary.
// LAB_GROUP: the course group whose projects are the lm material. Take it from an
// existing clone if there is one: git -C <data_track>\lm\<any> remote get-url origin
const LAB_GROUP = 's16/data';

const getAll = async (path) => {
  let out = [], page = 1;
  while (page) {
    const r = await fetch(`/api/v4/${path}${path.includes('?') ? '&' : '?'}per_page=100&page=${page}`);
    if (r.status !== 200) throw new Error(`${path} -> HTTP ${r.status}`);
    out = out.concat(await r.json());
    page = +r.headers.get('x-next-page') || 0;
  }
  return out;
};
const slim = p => ({ path: p.path, path_with_namespace: p.path_with_namespace, http_url_to_repo: p.http_url_to_repo,
  default_branch: p.default_branch, empty_repo: p.empty_repo, archived: p.archived, created_at: p.created_at, last_activity_at: p.last_activity_at });
const me = await (await fetch('/api/v4/user')).json();
if (!me || !me.id) { ({ error: 'not signed in to lab.ssafy.com: ask the user to sign in in Chrome' }); } else {
  const group = (await getAll(`groups/${encodeURIComponent(LAB_GROUP)}/projects?include_subgroups=true&simple=true`)).map(slim);
  const own = (await getAll(`users/${me.id}/projects?simple=true`)).map(slim);
  const out = { site: 'lab.ssafy.com', collectedAt: new Date().toISOString(), group: LAB_GROUP, namespace: me.username, groupProjects: group, ownProjects: own };
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], { type: 'application/json' }));
  a.download = 'ssafy_lab_manifest.json';
  document.body.appendChild(a); a.click(); a.remove();
  ({ downloaded: 'ssafy_lab_manifest.json', groupProjects: group.length, ownProjects: own.length,
     practiceRepos: own.filter(p => /_(ws|hw)_\d+_\w+$|^chapter_\d+_\d+_(ws|hw)$/.test(p.path)).length });
}
