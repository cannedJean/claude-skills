// project.ssafy.com collector. Paste the whole file as the `text` of
// mcp__claude-in-chrome__javascript_tool on a tab showing https://project.ssafy.com/home.
// It drives the site's own UI inside hidden same-origin iframes: it never reads
// the access token and never calls /ssafy/api itself.
// Starts a background job and returns at once; poll with project_poll.js.
// Every finished step is checkpointed in this site's localStorage (key ssafy_collector_v1),
// so if the tab navigates away or closes, pasting this file again resumes where it stopped.
// project_export.js downloads whatever the checkpoint holds at any time.
// Options: edit SSAFY_OPTS below before pasting.
//   courses: [] = all courses on the home page, or title substrings e.g. ["Web", "DB"]
//   start:   true = press 실습하기 on every problem (creates the repo of an unstarted one);
//            false = only resolve repos of problems that are already 참여/제출
//   limit:   max problems to process in this run (0 = no limit), for test runs
//   download: true = when finished, save ssafy_project_manifest.json through Chrome's download
//   fresh:   true = ignore and replace the checkpoint (a different `courses` value also starts fresh)
const SSAFY_OPTS = { courses: [], start: true, limit: 0, download: true, fresh: false };

(() => {
  if (window.__ssafy && window.__ssafy.running) return { already: 'running', progress: window.__ssafy.progress };
  const KEY = 'ssafy_collector_v1';
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) {}
  const resume = !!saved && !SSAFY_OPTS.fresh && JSON.stringify(saved.opts.courses) === JSON.stringify(SSAFY_OPTS.courses);
  const S = window.__ssafy = {
    running: true, opts: SSAFY_OPTS, resumed: resume, finished: false,
    progress: { phase: 'init', done: 0, total: 0, current: '' },
    courses: resume ? saved.courses : [], todo: resume ? saved.todo : [],
    results: resume ? saved.results : {}, errors: [],
  };
  const save = () => {
    try { localStorage.setItem(KEY, JSON.stringify({ opts: S.opts, savedAt: new Date().toISOString(), courses: S.courses, todo: S.todo, results: S.results })); }
    catch (e) { S.errors.push({ where: 'checkpoint', error: String(e) }); }
  };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const waitFor = async (fn, t = 20000) => { const s = Date.now(); while (Date.now() - s < t) { try { const v = fn(); if (v) return v; } catch (e) {} await sleep(250); } return null; };
  const frame = () => { const f = document.createElement('iframe'); f.style.cssText = 'position:fixed;left:-5000px;top:0;width:1280px;height:900px'; document.body.appendChild(f); return f; };
  const anchors = (f, re) => [...f.contentDocument.querySelectorAll('a')].filter(a => re.test(a.innerText.trim()));
  const load = async (f, url, ready) => { f.src = 'about:blank'; await sleep(100); f.src = url; return await waitFor(() => f.contentWindow.location.pathname !== 'blank' && f.contentDocument.readyState === 'complete' && ready(f), 25000); };
  const clickAndCapture = async (f, a, re) => { a.click(); return await waitFor(() => re.test(f.contentWindow.location.pathname) && f.contentWindow.location.pathname, 15000); };
  const keyOf = t => `${t.courseId}|${t.chapterId}|${t.listIndex}`;

  const main = async () => {
    const f = frame();
    // 1. courses from the home page cards (나의 실습과정)
    if (!S.courses.length) {
      S.progress.phase = 'courses';
      const homeReady = x => x.contentDocument.querySelector('[class*=swiper] a');
      const cardTitle = a => a.innerText.replace(/\s+/g, ' ').trim();
      await load(f, '/home', homeReady);
      const cardTitles = [...new Set([...f.contentDocument.querySelectorAll('[class*=swiper] a')].map(cardTitle).filter(t => t))];
      for (const title of cardTitles) {
        if (S.opts.courses.length && !S.opts.courses.some(c => title.includes(c))) continue;
        await load(f, '/home', homeReady);
        const a = [...f.contentDocument.querySelectorAll('[class*=swiper] a')].find(x => cardTitle(x) === title);
        const p = a && await clickAndCapture(f, a, /\/course\/CS\w+\/home$/);
        if (!p) { S.errors.push({ where: 'course', title }); continue; }
        S.courses.push({ title, id: p.match(/CS\w+/)[0], chapters: [], chaptersDone: false });
      }
      save();
    }
    // 2. chapters per course (목차 table on the course home); a course is redone until all its chapters resolve
    S.progress.phase = 'chapters';
    const tocLinks = x => { const t = [...x.contentDocument.querySelectorAll('article table')].find(tb => /주제/.test((tb.querySelector('thead') || tb).innerText.split('\n')[0] + (tb.querySelector('th') ? tb.querySelector('th').innerText : ''))); return t ? [...t.querySelectorAll('tbody a')] : []; };
    for (const c of S.courses.filter(c => !c.chaptersDone)) {
      if (S.cancel) break;
      const home = `/practice/user/course/${c.id}/home`;
      const ready = x => tocLinks(x).length;
      await load(f, home, ready); await sleep(800);
      const names = tocLinks(f).map(a => a.innerText.trim()).filter(t => t);
      let ok = names.length > 0;
      for (const [i, name] of names.entries()) {
        if (c.chapters.some(ch => ch.title === name)) continue;
        let p = null;
        for (let attempt = 0; attempt < 3 && !p; attempt++) {  // the course home is slow now and then
          await load(f, home, ready); await sleep(500 + attempt * 1500);
          const a = tocLinks(f).find(x => x.innerText.trim() === name);
          p = a && await clickAndCapture(f, a, /\/chapter\/CT\w+$/);
        }
        if (!p) { ok = false; S.errors.push({ where: 'chapter', course: c.title, name }); continue; }
        c.chapters.push({ index: i + 1, title: name, id: p.match(/CT\w+/)[0], listed: false });
      }
      c.chapters.sort((x, y) => x.index - y.index);
      c.chaptersDone = ok;
      save();
    }
    // 3. problem rows per chapter
    S.progress.phase = 'lists';
    const listReady = x => anchors(x, /상세보기/).length;
    for (const c of S.courses) for (const ch of c.chapters.filter(ch => !ch.listed)) {
      if (S.cancel) break;
      const url = `/practice/user/course/${c.id}/home/chapter/${ch.id}`;
      if (!await load(f, url, listReady)) { S.errors.push({ where: 'chapter-list', course: c.title, chapter: ch.title, note: 'no problems shown (often a chapter not open yet)' }); continue; }
      const art = f.contentDocument.querySelector('article');
      const lis = [...art.querySelectorAll('li')].filter(li => /^\s*\d{3,5}\./.test(li.innerText));
      lis.forEach((li, k) => {
        let p = li.parentElement, sec = null;
        for (let d = 0; d < 6 && p && !sec; d++) { const h = [...p.querySelectorAll(':scope > *')].find(x => /^(실습|과제)$/.test(x.innerText.trim())); if (h) sec = h.innerText.trim(); p = p.parentElement; }
        // level and status sit beside the <li>, in the row that also holds 상세보기
        let box = li; while (box.parentElement && !/상세보기/.test(box.innerText)) box = box.parentElement;
        const lines = (/상세보기/.test(box.innerText) && box.querySelectorAll('a').length <= 2 ? box : li).innerText.split('\n').map(s => s.trim()).filter(Boolean);
        S.todo.push({ course: c.title, courseId: c.id, chapterIndex: ch.index, chapter: ch.title, chapterId: ch.id, listIndex: k,
          kind: sec === '과제' ? 'hw' : sec === '실습' ? 'ws' : null, title: lines[0],
          level: (lines.find(l => /Lv\d/.test(l)) || '').replace(/.*(Lv\d).*/, '$1'),
          status: lines.find(l => l === '제출' || l === '참여') || '미참여' });
      });
      ch.listed = true;
      save();
    }
    // 4. each problem: statement, then 실습하기 -> practice room -> GitLab project
    S.progress.phase = 'problems';
    const isDone = t => { const r = S.results[keyOf(t)]; return r && !r.error && (r.repo || !S.opts.start); };
    const pending = S.todo.filter(t => !isDone(t));
    S.progress.done = S.todo.length - pending.length;
    S.progress.total = S.todo.length;
    const batch = S.opts.limit ? pending.slice(0, S.opts.limit) : pending;
    const g = frame();
    for (const t0 of batch) {
      if (S.cancel) { S.errors.push({ where: 'cancelled' }); break; }
      const t = Object.assign({}, t0);
      delete t.error;
      S.progress.current = `${t.course} / ${t.chapter} / ${t.title}`;
      try {
        await load(f, `/practice/user/course/${t.courseId}/home/chapter/${t.chapterId}`, listReady);
        const a = anchors(f, /상세보기/)[t.listIndex];
        const p = a && await clickAndCapture(f, a, /\/practice\/PR\w+$/);
        if (!p) throw new Error('problem page not reached');
        t.problemId = p.match(/PR\w+/)[0];
        t.url = 'https://project.ssafy.com' + p;
        await waitFor(() => anchors(f, /실습하기/).length && f.contentDocument.querySelector('article').innerText.includes('목록'));
        const txt = f.contentDocument.querySelector('article').innerText;
        const from = txt.indexOf(t.title);
        const to = txt.lastIndexOf('목록');
        t.statement = txt.slice(from >= 0 ? from : 0, to > from ? to : undefined)
          .split('\n').filter(l => !/^(auto_stories|monitor|auto_storiesmonitor)$/.test(l.trim()) && !/아이콘박스|저작권의 보호를 받는/.test(l)).join('\n')
          .replace(/\n{3,}/g, '\n\n').trim();
        if (!t.level) t.level = (t.statement.match(/^Lv\d$/m) || [''])[0];
        if (S.opts.start || t.status !== '미참여') {
          let opened = null;
          f.contentWindow.open = u => { opened = String(u); return null; };
          anchors(f, /실습하기/)[0].click();
          await waitFor(() => opened, 20000);
          if (!opened) throw new Error('실습하기 did not open a practice room');
          g.src = opened;
          // a freshly started problem's repo is imported first, which can take well over 30 s
          const repo = await waitFor(() => { const i = [...g.contentDocument.querySelectorAll('iframe')].find(x => /\/-\/ide\/project\//.test(x.src)); return i && i.src.split('/-/ide/project/')[1].split(/[?#]/)[0]; }, 60000);
          g.src = 'about:blank';
          if (t.status === '미참여') t.started = true;
          if (!repo) throw new Error('practice room showed no GitLab project');
          t.repo = repo; // "<namespace>/<project>"
        }
      } catch (e) { t.error = String(e.message || e); S.errors.push({ where: 'problem', title: t.title, course: t.course, error: t.error }); }
      const prev = S.results[keyOf(t)];
      if (prev && prev.started) t.started = true;  // keep the fact that an earlier run started it
      S.results[keyOf(t)] = t;
      if (!t.error) S.progress.done++;
      save();
    }
    f.remove(); g.remove();
  };

  main().catch(e => S.errors.push({ where: 'fatal', error: String(e && e.stack || e) })).finally(() => {
    S.running = false; S.finished = true; S.progress.phase = 'finished';
    const out = S.manifest = { site: 'project.ssafy.com', collectedAt: new Date().toISOString(), opts: S.opts, courses: S.courses, problems: Object.values(S.results), errors: S.errors };
    if (!S.opts.download) return;
    const blob = new Blob([JSON.stringify(out, null, 1)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'ssafy_project_manifest.json';
    document.body.appendChild(a); a.click(); a.remove();
  });
  return { started: true, resumed: resume, opts: SSAFY_OPTS, checkpoint: resume ? { courses: S.courses.length, rows: S.todo.length, results: Object.keys(S.results).length } : null };
})();
