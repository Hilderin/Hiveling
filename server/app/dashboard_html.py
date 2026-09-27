"""Single-page dashboard served by the server (no build step).

Polling-based, vanilla JS/CSS. It talks to the JSON API exposed in ``web.py``.
Runs are started by pushing a plan over HTTP (``POST /api/runs``); the UI
focuses on monitoring (active runs, history) and on retry/cancel/edit.
"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/static/favicon.png">
<title>Hiveling dashboard</title>
<style>
  :root { --bg:#0f1419; --panel:#171d26; --panel2:#1e2632; --border:#2a3543;
          --fg:#dfe7ef; --muted:#8b98a8; --accent:#4da3ff; --gate:#c792ea;
          --pending:#8b98a8; --running:#4da3ff; --succeeded:#3ecf8e;
          --failed:#ff5f56; --skipped:#e5c07b; --canceled:#e08e4d; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         background:var(--bg); color:var(--fg); }
  header { display:flex; align-items:center; gap:8px; padding:10px 16px;
           background:var(--panel); border-bottom:1px solid var(--border); position:sticky; top:0; z-index:5; }
  header h1 { font-size:15px; margin:0; letter-spacing:.5px; line-height:26px; }
  header img.logo { width:26px; height:26px; object-fit:contain; display:block; }
  #error-bar { color:var(--failed); }
  .layout { display:grid; grid-template-columns:290px 1fr; min-height:calc(100vh - 44px); }
  .sidebar { border-right:1px solid var(--border); padding:12px; overflow:auto; }
  .main { padding:12px 16px; overflow:auto; }
  .panel { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:12px; }
  .panel h2 { font-size:12px; text-transform:uppercase; letter-spacing:.8px; color:var(--muted); margin:0 0 8px; }
  select, input, textarea, button { font:inherit; color:var(--fg); background:var(--panel2);
        border:1px solid var(--border); border-radius:6px; padding:5px 8px; }
  button { cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#06131f; font-weight:600; }
  button.danger { border-color:var(--failed); color:var(--failed); }
  button.link { background:transparent; border:0; color:var(--accent); padding:0; }
  button.link:hover { text-decoration:underline; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .grow { flex:1; min-width:0; }
  .muted { color:var(--muted); }
  .dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:6px; vertical-align:middle; }
  .pending { background:var(--pending); } .running { background:var(--running); }
  .succeeded { background:var(--succeeded); } .failed { background:var(--failed); }
  .skipped { background:var(--skipped); } .canceled { background:var(--canceled); }
  .badge { font-size:11px; padding:1px 6px; border-radius:10px; border:1px solid var(--border); color:var(--muted); }
  .badge.worker { border-color:var(--accent); color:var(--accent); }
  .badge.gate { border-color:var(--gate); color:var(--gate); }
  .taskrow.gate-node { border-left:2px solid var(--gate); }
  ul.tree { list-style:none; margin:0; padding-left:16px; }
  ul.tree > li { margin:3px 0; }
  .taskrow { display:inline-flex; align-items:center; gap:8px; padding:3px 8px; border-radius:6px; cursor:pointer;
             background:transparent; border:1px solid transparent; }
  .taskrow:hover { background:var(--panel2); }
  .taskrow.active { background:var(--panel2); border-color:var(--accent); }
  .workers, .runs { margin:0; padding:0; }
  .workers li { list-style:none; padding:3px 4px; }
  ul.runs li { list-style:none; padding:0; margin:0 0 3px; }
  button.runitem { display:block; width:100%; text-align:left; background:transparent;
                   border:1px solid transparent; padding:4px 6px; border-radius:6px; white-space:nowrap;
                   overflow:hidden; text-overflow:ellipsis; }
  button.runitem:hover { background:var(--panel2); }
  button.runitem.active { background:var(--panel2); border-color:var(--accent); }
  button.runitem.history { white-space:normal; }
  pre { background:var(--panel2); border:1px solid var(--border); border-radius:6px; padding:8px;
        overflow:auto; max-height:340px; white-space:pre-wrap; word-break:break-word; }
  textarea { width:100%; min-height:340px; }
  table { border-collapse:collapse; width:100%; }
  td, th { text-align:left; padding:3px 8px 3px 0; vertical-align:top; }
  th { color:var(--muted); font-weight:400; width:110px; }
  .task-title { font-size:16px; font-weight:600; }
  a { color:var(--accent); }
  .hidden { display:none !important; }
  @media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; }
    .sidebar { border-right:0; border-bottom:1px solid var(--border); }
    pre { max-height:220px; }
  }
</style>
</head>
<body>
<header>
  <img class="logo" src="/static/hiveling.png" alt="Hiveling logo">
  <h1>Hiveling</h1>
  <span class="grow"></span>
  <span id="error-bar"></span>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="panel">
      <h2>Workers</h2>
      <ul id="workers" class="workers"></ul>
    </div>

    <div class="panel">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">Active runs</h2>
        <button class="link" onclick="openHistory()">History</button>
      </div>
      <ul id="active-runs" class="runs"></ul>
    </div>
  </aside>

  <main class="main">
    <div id="empty" class="muted">Select a run or open the history.</div>

    <div id="history-view" class="hidden">
      <div class="panel">
        <div class="row">
          <span class="grow task-title">History</span>
          <button onclick="closeHistory()">Close</button>
        </div>
        <div class="row" style="margin-top:8px">
          <input id="history-search" class="grow" placeholder="search by plan name or run id"
                 oninput="scheduleHistorySearch()">
        </div>
        <div id="history-list" style="margin-top:6px"></div>
        <div class="row" style="margin-top:8px">
          <button onclick="historyPage(-1)">Prev</button>
          <span id="history-page" class="muted"></span>
          <button onclick="historyPage(1)">Next</button>
        </div>
      </div>
    </div>

    <div id="run-view" class="hidden">
      <div class="panel">
        <div class="row">
          <span class="grow task-title" id="run-title"></span>
          <button id="edit-plan-btn" onclick="openEditor()">Edit plan</button>
          <button id="cancel-btn" class="danger" onclick="cancelRun()">Cancel run</button>
          <button id="resume-btn" onclick="resumeRun()">Resume run</button>
          <button onclick="refresh(true)">Refresh</button>
        </div>
        <div class="muted" id="run-meta" style="margin-top:6px"></div>
      </div>

      <div class="panel">
        <h2>Tasks &amp; gates</h2>
        <div id="tree"></div>
      </div>

      <div id="editor-panel" class="panel hidden">
        <div class="row" style="margin-bottom:8px">
          <span class="grow task-title" id="editor-title"></span>
          <button class="primary" onclick="savePlan()">Save</button>
          <button onclick="closeEditor()">Close</button>
          <span id="editor-msg" class="muted"></span>
        </div>
        <textarea id="editor-text"></textarea>
      </div>
    </div>

    <div id="task-view" class="hidden">
      <div class="panel">
        <div class="row">
          <button onclick="backToRun()">&larr; Back to run</button>
          <span class="grow task-title"><span id="task-kind" class="badge gate hidden">gate</span> <span id="task-title"></span></span>
          <a id="task-download" class="badge" href="#">Download files</a>
        </div>
        <div class="muted" id="task-sub" style="margin-top:6px"></div>
      </div>
      <div class="panel"><div id="task-body"></div></div>
    </div>
  </main>
</div>

<script>
const state = {
  workers: [], activeRuns: [], run: null, runDetail: null, task: null, taskDetail: null,
  editing: null, view: 'run', history: { runs: [], total: 0, limit: 20, offset: 0 }
};

function reportError(msg) {
  const el = document.getElementById('error-bar');
  if (el) el.textContent = msg ? String(msg) : '';
}
window.addEventListener('error', ev => reportError('JS error: ' + ev.message));
window.addEventListener('unhandledrejection', ev => reportError('error: ' + ((ev.reason && ev.reason.message) || ev.reason)));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (JSON.parse(await r.text()) || {}).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  const txt = await r.text();
  return txt ? JSON.parse(txt) : null;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtDur(s) { return s == null ? '' : (Math.round(s*10)/10) + 's'; }
function fmtTime(ts) { return ts ? new Date(ts*1000).toLocaleString() : ''; }
function tokenTotal(t) {
  if (!t) return 0;
  if (t.total != null) return t.total;
  const c = t.cache || {};
  return (t.input||0) + (t.output||0) + (t.reasoning||0) + (c.read||0) + (c.write||0);
}
function fmtNum(n) { return (n||0).toLocaleString(); }
function fmtCost(c) { return c == null ? '-' : Number(c).toFixed(4); }
function tokenBreakdown(t) {
  if (!t) return '-';
  const c = t.cache || {};
  const parts = ['in ' + fmtNum(t.input), 'out ' + fmtNum(t.output)];
  if (t.reasoning) parts.push('reasoning ' + fmtNum(t.reasoning));
  const cache = (c.read||0) + (c.write||0);
  if (cache) parts.push('cache ' + fmtNum(cache));
  return fmtNum(tokenTotal(t)) + ' (' + parts.join(' / ') + ')';
}

async function refresh(silent) {
  try {
    const [workers, active] = await Promise.all([
      api('/api/workers'),
      api('/api/runs?status=active&limit=50')
    ]);
    state.workers = workers.workers;
    state.activeRuns = active.runs;
    reportError('');
    renderWorkers(); renderActiveRuns();
    if (state.run) await loadRun(state.run, true);
    if (state.view === 'history') await loadHistory(state.history.offset);
  } catch (e) { if (!silent) reportError(e.message); }
}

function renderWorkers() {
  const ul = document.getElementById('workers');
  ul.innerHTML = state.workers.map(w => {
    const cls = !w.reachable ? 'failed' : (w.busy ? 'canceled' : 'succeeded');
    const txt = !w.reachable ? 'unreachable' : (w.busy ? 'busy' : 'free');
    return `<li><span class="dot ${cls}"></span><b>${esc(w.name)}</b> <span class="muted">${txt}</span></li>`;
  }).join('') || '<li class="muted">none</li>';
}

function renderActiveRuns() {
  const ul = document.getElementById('active-runs');
  if (!state.activeRuns.length) { ul.innerHTML = '<li class="muted">none</li>'; return; }
  ul.innerHTML = state.activeRuns.map(r => {
    const c = r.counts || {};
    const done = (c.succeeded||0) + (c.failed||0) + (c.skipped||0) + (c.canceled||0);
    const active = (r.run_id === state.run && state.view === 'run') ? 'active' : '';
    return `<li><button class="runitem ${active}" onclick="openRun('${r.run_id}')" title="${esc(r.run_id)}">
      <span class="dot ${r.status}"></span><b>${esc(r.plan_name)}</b>
      <span class="muted">${done}/${r.task_count}</span>
    </button></li>`;
  }).join('');
}

async function loadHistory(offset) {
  try {
    const search = document.getElementById('history-search');
    const q = (search && search.value) || '';
    const limit = state.history.limit || 20;
    const data = await api(`/api/runs?status=all&limit=${limit}&offset=${Math.max(0, offset)}&q=${encodeURIComponent(q)}`);
    state.history = { runs: data.runs, total: data.total, limit: data.limit, offset: data.offset };
    renderHistory();
  } catch (e) { reportError(e.message); }
}

function renderHistory() {
  const h = state.history;
  document.getElementById('history-list').innerHTML = h.runs.map(r => {
    const c = r.counts || {};
    const summary = Object.entries(c).map(([k,v]) => `${k}:${v}`).join(' ');
    return `<button class="runitem history" onclick="openRun('${r.run_id}')">
      <span class="dot ${r.status}"></span><b>${esc(r.plan_name)}</b>
      <span class="muted">${esc(r.status)} &middot; ${fmtTime(r.created_at)} &middot; ${r.task_count} tasks</span>
      <div class="muted" style="font-size:12px">${esc(r.run_id)} &middot; ${summary}</div>
    </button>`;
  }).join('') || '<div class="muted">no runs</div>';
  const from = h.total ? h.offset + 1 : 0;
  const to = Math.min(h.offset + h.runs.length, h.total);
  document.getElementById('history-page').textContent = `${from}-${to} / ${h.total}`;
}

let searchTimer = null;
function scheduleHistorySearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadHistory(0), 300);
}
function historyPage(delta) {
  const target = state.history.offset + delta * state.history.limit;
  loadHistory(Math.max(0, target));
}

async function openHistory() {
  state.view = 'history';
  showView();
  await loadHistory(0);
  setUrl();
}
function closeHistory() { state.view = 'run'; showView(); setUrl(); }

function showView() {
  const view = state.view;
  const history = view === 'history';
  const task = view === 'task' && !!state.taskDetail;
  const run = !history && !task && !!state.runDetail;
  document.getElementById('history-view').classList.toggle('hidden', !history);
  document.getElementById('run-view').classList.toggle('hidden', !run);
  document.getElementById('task-view').classList.toggle('hidden', !task);
  document.getElementById('empty').classList.toggle('hidden', history || run || task);
}

function openRun(runId) { loadRun(runId); }

function backToRun() {
  state.view = 'run';
  state.task = null;
  state.taskDetail = null;
  if (state.run) loadRun(state.run);
  else { showView(); setUrl(); }
}

async function loadRun(runId, keepTask) {
  state.run = runId;
  try { state.runDetail = await api('/api/runs/' + encodeURIComponent(runId)); }
  catch (e) { reportError(e.message); return; }
  if (!keepTask) {
    state.task = null; state.taskDetail = null;
    state.view = 'run';
    document.getElementById('editor-panel').classList.add('hidden');
  }
  renderActiveRuns(); renderRun(); showView(); setUrl();
  if (state.view === 'task' && state.task) await loadTask(state.task, true);
}

function renderRun() {
  const d = state.runDetail;
  document.getElementById('run-title').textContent = d.plan_name + '  (' + d.run_id + ')';
  const c = d.tasks.reduce((a,t) => (a[t.status]=(a[t.status]||0)+1, a), {});
  const gates = d.tasks.filter(t => t.kind === 'gate').length;
  document.getElementById('run-meta').innerHTML =
    `<span class="dot ${d.status}"></span>${esc(d.status)} &middot; ` +
    Object.entries(c).map(([k,v]) => `${k}:${v}`).join(' &middot; ') +
    ` &middot; ${d.tasks.length - gates} tasks` + (gates ? ` + ${gates} gates` : '') +
    ` &middot; started ${fmtTime(d.started_at)}` +
    (d.finished_at ? ` &middot; finished ${fmtTime(d.finished_at)}` : '') +
    (d.error ? ` &middot; <span class="failed">${esc(d.error)}</span>` : '');
  document.getElementById('cancel-btn').classList.toggle('hidden', d.status !== 'running');
  document.getElementById('tree').innerHTML = renderTree(d.tasks);
}

function renderTree(tasks) {
  const byId = {}; tasks.forEach(t => byId[t.id] = t);
  const children = {}, roots = [];
  tasks.forEach(t => {
    const parents = [...new Set([...(t.depends_on||[]), ...(t.inputs_from||[])])].filter(p => byId[p]);
    if (parents.length) (children[parents[0]] = children[parents[0]] || []).push(t.id);
    else roots.push(t.id);
  });
  const seen = new Set();
  function node(id) {
    if (seen.has(id)) return '';
    seen.add(id);
    const t = byId[id];
    const kids = (children[id] || []).map(node).join('');
    const active = state.task === id ? 'active' : '';
    const deps = [...new Set([...(t.depends_on||[]), ...(t.inputs_from||[])])];
    const ids = deps.length ? ` <span class="badge">&larr; ${esc(deps.join(', '))}</span>` : '';
    const isGate = t.kind === 'gate';
    const kind = isGate ? ' <span class="badge gate">gate</span>' : '';
    const model = t.model ? ` <span class="badge" title="model">${esc(t.model)}</span>` : '';
    const worker = t.worker ? ` <span class="badge worker" title="worker">${esc(t.worker)}</span>` : '';
    const detail = isGate
      ? `attempt ${t.gate_attempt||0}/${t.gate_max_attempts||0}`
      : `${t.status}${t.duration_s!=null?' '+fmtDur(t.duration_s):''}`;
    return `<li><button type="button" class="taskrow ${active} ${isGate?'gate-node':''}" onclick="loadTask('${id}')">
        <span class="dot ${t.status}"></span><b>${esc(id)}</b>${kind}
        <span class="muted">${detail}</span>${worker}${model}${ids}
      </button>${kids ? '<ul class="tree">' + kids + '</ul>' : ''}</li>`;
  }
  const items = (roots.length ? roots : tasks.map(t => t.id)).map(node).join('');
  return '<ul class="tree">' + items + '</ul>';
}

async function loadTask(taskId, keepView) {
  state.task = taskId;
  try { state.taskDetail = await api('/api/runs/' + encodeURIComponent(state.run) + '/tasks/' + encodeURIComponent(taskId)); }
  catch (e) { reportError(e.message); return; }
  state.view = 'task';
  renderRun();
  renderTask();
  showView();
  setUrl();
}

function renderTask() {
  const t = state.runDetail.tasks.find(x => x.id === state.task);
  const d = state.taskDetail || {};
  const isGate = t.kind === 'gate';
  document.getElementById('task-kind').classList.toggle('hidden', !isGate);
  document.getElementById('task-title').innerHTML = `<span class="dot ${t.status}"></span>${esc(t.id)}`;
  document.getElementById('task-sub').innerHTML =
    `${esc(t.status)}` +
    (t.worker ? ` &middot; ${esc(t.worker)}` : '') +
    (t.model ? ` &middot; ${esc(t.model)}` : '') +
    (isGate ? ` &middot; attempt ${t.gate_attempt||0}/${t.gate_max_attempts||0}` : '');
  const dl = document.getElementById('task-download');
  dl.textContent = isGate ? 'Download decision material' : 'Download files';
  dl.href = `/api/runs/${encodeURIComponent(state.run)}/tasks/${encodeURIComponent(t.id)}/` +
    (isGate ? 'gate-input' : 'files');
  const s = d.status || {};
  const rows = [
    ['kind', t.kind || 'task'],
    ['status', `${t.status}${s.exit_code!=null?' (exit '+s.exit_code+')':''}`],
    ['worker', t.worker || '-'],
    ['job id', t.job_id || '-'],
    ['model', t.model || '(default)'],
    ['agent', t.agent || '-'],
    ['duration', fmtDur(t.duration_s)],
    ['tokens', tokenBreakdown(s.tokens)],
    ['cost', fmtCost(s.cost)],
    ['session', s.session_id || '-'],
    ['depends on', (t.depends_on||[]).join(', ') || '-'],
    ['inputs from', (t.inputs_from||[]).join(', ') || '-'],
  ];
  if (isGate) {
    rows.push(['analyses', (t.gate_targets||[]).join(', ') || '-']);
    rows.push(['attempt', `${t.gate_attempt||0} of ${t.gate_max_attempts||0}`]);
    rows.push(['verdict', t.gate_verdict || (t.status === 'succeeded' ? 'VALID' : '-')]);
  }
  let html = '<table>' + rows.map(([k,v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join('') + '</table>';
  if (isGate && t.gate_feedback)
    html += `<p><b>last gate feedback</b><pre>${esc(t.gate_feedback)}</pre></p>`;
  if (d.request && d.request.prompt)
    html += `<p><b>prompt</b><pre>${esc(d.request.prompt)}</pre></p>`;
  if (s.tool_calls && s.tool_calls.length)
    html += `<p><b>tool calls</b><pre>${esc(s.tool_calls.join('\n'))}</pre></p>`;
  if (d.result) html += `<p><b>result</b><pre>${esc(d.result)}</pre></p>`;
  if (t.error) html += `<p><b>error</b><pre>${esc(t.error)}</pre></p>`;
  if (t.changed_files && t.changed_files.length)
    html += `<p><b>changed files</b><pre>${esc(t.changed_files.join('\n'))}</pre></p>`;
  if (d.events) html += `<details><summary>events (${d.event_lines} lines)</summary><pre>${esc(d.events)}</pre></details>`;
  if (d.stderr) html += `<details><summary>stderr</summary><pre>${esc(d.stderr)}</pre></details>`;
  document.getElementById('task-body').innerHTML = html;
}

async function cancelRun() {
  if (!state.run) return;
  try { await api('/api/runs/' + encodeURIComponent(state.run) + '/cancel', { method:'POST' }); }
  catch (e) { reportError(e.message); }
  await refresh(true);
}

async function resumeRun() {
  if (!state.run) return;
  try { await api('/api/runs/' + encodeURIComponent(state.run) + '/resume', { method:'POST' }); }
  catch (e) { reportError(e.message); }
  await refresh(true);
}

async function openEditor() {
  if (!state.run) return;
  try {
    const data = await api('/api/runs/' + encodeURIComponent(state.run) + '/plan');
    state.editing = state.run;
    document.getElementById('editor-panel').classList.remove('hidden');
    document.getElementById('editor-title').textContent = 'Edit ' + data.name;
    document.getElementById('editor-msg').textContent = '';
    document.getElementById('editor-text').value = data.content;
  } catch (e) { reportError(e.message); }
}
function closeEditor() { document.getElementById('editor-panel').classList.add('hidden'); state.editing = null; }

async function savePlan() {
  if (!state.editing) return;
  const msg = document.getElementById('editor-msg');
  msg.textContent = ' saving...';
  try {
    await api('/api/runs/' + encodeURIComponent(state.editing) + '/plan',
      { method:'PUT', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ content: document.getElementById('editor-text').value }) });
    msg.textContent = ' saved';
  } catch (e) { msg.textContent = ' ' + e.message; }
}

function setUrl() {
  let path = '/';
  if (state.view === 'history') {
    path = '/history';
  } else if (state.view === 'task' && state.run && state.task) {
    const t = state.runDetail && state.runDetail.tasks.find(x => x.id === state.task);
    const seg = (t && t.kind === 'gate') ? 'gate' : 'task';
    path = `/run/${encodeURIComponent(state.run)}/${seg}/${encodeURIComponent(state.task)}`;
  } else if (state.run) {
    path = `/run/${encodeURIComponent(state.run)}`;
  }
  if (location.pathname + location.search !== path) history.replaceState(null, '', path);
}

async function route() {
  const parts = location.pathname.split('/').filter(Boolean);
  if (parts[0] === 'history') { await openHistory(); return; }
  if (parts[0] === 'run' && parts[1]) {
    const runId = decodeURIComponent(parts[1]);
    if (parts[2] && parts[3]) {
      await loadRun(runId, true);
      await loadTask(decodeURIComponent(parts[3]));
    } else {
      await loadRun(runId);
    }
    return;
  }
  state.view = 'run';
  showView();
  setUrl();
}

(async function init() {
  try {
    await refresh();
    await route();
  } catch (e) { reportError('init: ' + e.message); }
})();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
