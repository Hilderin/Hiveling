"""Single-page dashboard served by the server (no build step).

Polling-based, vanilla JS/CSS. It talks to the JSON API exposed in ``web.py``.
Runs are started from the home page (or by pushing a plan over HTTP); the UI
focuses on monitoring (active runs, history), live plan edits and
retry/cancel. Navigation updates the URL with pushState so the browser back
button works, and the periodic refresh never rebuilds the text you are editing
or scroll/``<details>`` state you opened.
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
  button.brand { display:flex; align-items:center; gap:8px; background:transparent; border:0;
                 padding:0; color:var(--fg); cursor:pointer; font:inherit; }
  button.brand:hover { color:var(--accent); }
  button.brand h1 { font-size:15px; margin:0; letter-spacing:.5px; line-height:26px; }
  button.brand img.logo { width:26px; height:26px; object-fit:contain; display:block; }
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
  .tabs { display:flex; gap:4px; border-bottom:1px solid var(--border); margin:0 0 10px; flex-wrap:wrap; }
  button.tab { background:transparent; border:1px solid transparent; border-bottom:0;
               border-radius:6px 6px 0 0; padding:4px 10px; color:var(--muted); }
  button.tab:hover { color:var(--fg); }
  button.tab.active { background:var(--panel2); border-color:var(--border); color:var(--fg); }
  #task-body .tabpane[data-pane="events"] pre { max-height:60vh; }
  #task-body .tabpane p:first-child { margin-top:0; }
  a { color:var(--accent); }
  .hidden { display:none !important; }
  #confirm-overlay { position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:50;
                     display:flex; align-items:center; justify-content:center; }
  .confirm-box { max-width:440px; width:calc(100% - 32px); margin:0; }
  .confirm-box h3 { margin:0 0 6px; font-size:15px; }
  @media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; }
    .sidebar { border-right:0; border-bottom:1px solid var(--border); }
    pre { max-height:220px; }
  }
</style>
</head>
<body>
<header>
  <button class="brand" type="button" onclick="openHome()" title="Home">
    <img class="logo" src="/static/hiveling.png" alt="Hiveling logo">
    <h1>Hiveling</h1>
  </button>
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
    <div id="home-view" class="hidden">
      <div class="panel">
        <div class="row">
          <span class="grow task-title">Home</span>
          <button class="link" onclick="openHistory()">Browse all runs &rarr;</button>
        </div>
        <div class="muted" style="margin-top:6px">
          Hiveling runs OpenCode plans across workers. Start a run, follow its tasks
          and gates, and edit a plan live while it runs.
        </div>
      </div>

      <div class="panel">
        <h2>Start a run</h2>
        <div class="row" style="margin-bottom:8px">
          <select id="home-plan" class="grow" style="min-width:200px" aria-label="Plan"></select>
        </div>
        <div class="row">
          <input id="home-only" class="grow" style="min-width:180px"
                 placeholder="only tasks (comma-separated, optional)" aria-label="only tasks">
          <button class="primary" onclick="startRun()">Start run</button>
        </div>
        <div id="home-msg" class="muted" style="margin-top:6px"></div>
      </div>

      <div class="panel">
        <h2>Recent runs</h2>
        <div id="home-runs"></div>
      </div>
    </div>

    <div id="history-view" class="hidden">
      <div class="panel">
        <div class="row">
          <span class="grow task-title">History</span>
          <button onclick="openHome()">Close</button>
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
        </div>
        <div class="muted" id="run-meta" style="margin-top:6px"></div>
        <div class="muted" id="run-usage" style="margin-top:2px"></div>
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
        <div id="editor-hint" class="muted" style="margin-bottom:6px"></div>
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

<div id="confirm-overlay" class="hidden">
  <div class="panel confirm-box">
    <h3 id="confirm-title"></h3>
    <div id="confirm-body" class="muted"></div>
    <div class="row" style="justify-content:flex-end; margin-top:12px">
      <button id="confirm-no" type="button"></button>
      <button id="confirm-yes" class="danger" type="button"></button>
    </div>
  </div>
</div>

<script>
const state = {
  workers: [], activeRuns: [], run: null, runDetail: null, task: null, taskDetail: null,
  editing: null, view: 'home',
  history: { runs: [], total: 0, limit: 20, offset: 0 },
  home: { plans: [], runs: [] }
};
// True while route() is applying a URL (init/deep link/back/forward): setUrl then
// replaces the current entry instead of pushing a new one.
let fromRoute = false;

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
function fmtRam(free, total) {
  if (total == null) return '';
  const gb = 1024 * 1024 * 1024;
  const freeText = free == null ? '?' : Math.round(free/gb*10)/10;
  return 'ram ' + freeText + ' / ' + Math.round(total/gb) + ' G';
}
function workerResources(w) {
  const r = w.resources || {};
  const parts = [];
  if (r.cpu_count != null) {
    parts.push('cpu ' + r.cpu_count + (r.cpu_speed_mhz ? 'x' + Math.round(r.cpu_speed_mhz) + ' MHz' : ''));
  }
  const ram = fmtRam(r.ram_available_bytes, r.ram_total_bytes);
  if (ram) parts.push(ram);
  return parts.join(' · ');
}
function runDuration(r) { return r.duration_s != null ? ' &middot; ' + fmtDur(r.duration_s) : ''; }
function fmtResource(r) {
  const lines = [`${r.type || '?'} '${r.id || '?'}'`];
  const opts = r.with || {};
  Object.keys(opts).forEach(k => {
    const v = opts[k];
    lines.push('  ' + k + ': ' + (Array.isArray(v) ? v.join(', ')
      : (v && typeof v === 'object' ? JSON.stringify(v) : v)));
  });
  if (r.when && Object.keys(r.when).length) lines.push('  when: ' + JSON.stringify(r.when));
  return lines.join('\n');
}
// Turn one OpenCode/hiveling JSON event into a readable console-log line.
function fmtEvent(line) {
  const raw = String(line == null ? '' : line);
  if (!raw.trim()) return '';
  let ev;
  try { ev = JSON.parse(raw); } catch (e) { return raw; }
  if (!ev || typeof ev !== 'object') return raw;
  const part = ev.part || {};
  const type = ev.type || part.type || 'event';
  let ts = '';
  if (ev.timestamp) {
    const d = new Date(ev.timestamp);
    if (!isNaN(d.getTime())) ts = d.toTimeString().slice(0, 8) + ' ';
  }
  const tag = name => ts + '[' + name + '] ';
  switch (type) {
    case 'step_start':
    case 'step-start':
      return tag('step') + 'start';
    case 'step_finish':
    case 'step-finish': {
      const t = part.tokens || {};
      const bits = [];
      if (t.input != null) bits.push('in ' + t.input);
      if (t.output != null) bits.push('out ' + t.output);
      if (t.reasoning) bits.push('reasoning ' + t.reasoning);
      if (part.cost != null) bits.push('$' + Number(part.cost).toFixed(4));
      return tag('step') + 'finish' + (bits.length ? '  ' + bits.join(' / ') : '');
    }
    case 'text':
      return tag('text') + String(part.text || '');
    case 'tool_use':
    case 'tool': {
      const tool = part.tool || part.name || 'tool';
      const state = part.state || {};
      const title = (state.input && (state.input.command || state.input.filePath || state.input.path))
        || state.title || '';
      let out = tag('tool') + tool + (title ? ': ' + title : '');
      let output = state.output != null ? state.output : (state.metadata && state.metadata.output);
      if (typeof output === 'string') {
        // The shell tool sometimes returns a JSON envelope {exit, output, ...};
        // unwrap it so the line shows the real output (or the exit code).
        try {
          const parsed = JSON.parse(output);
          if (parsed && typeof parsed === 'object' && 'output' in parsed) {
            const inner = String(parsed.output == null ? '' : parsed.output).trim();
            output = inner || ('exit ' + (parsed.exit != null ? parsed.exit : '?'));
          }
        } catch (e) { /* not JSON: keep the raw output */ }
      }
      if (output) {
        const first = String(output).split('\n').map(s => s.trim()).filter(Boolean)[0] || '';
        if (first && first !== title) out += ' \u2192 ' + first.slice(0, 160);
      }
      return out;
    }
    case 'error': {
      const err = ev.error || {};
      const data = err.data || {};
      const message = data.message || err.message || err.name || err.type || raw;
      return tag('error') + message;
    }
    case 'hiveling.merge':
      return tag('merge') + (ev.phase || '') + (ev.ref ? ' ' + ev.ref : '')
        + (ev.files && ev.files.length ? ' (' + ev.files.length + ' file(s))' : '');
    default:
      return tag(type) + raw.slice(0, 240);
  }
}
function fmtEvents(text) {
  return String(text || '').split('\n').map(fmtEvent).filter(l => l !== '').join('\n');
}
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
function runSummary(r) {
  const c = r.counts || {};
  return Object.entries(c).map(([k,v]) => `${k}:${v}`).join(' ');
}

// -------------------------------------------------------------------- refresh
let refreshing = false;
async function refresh(silent) {
  if (refreshing) return;
  if (document.hidden) return;  // do not disturb a background tab
  refreshing = true;
  try {
    const [workers, active] = await Promise.all([
      api('/api/workers'),
      api('/api/runs?status=active&limit=50')
    ]);
    state.workers = workers.workers;
    state.activeRuns = active.runs;
    reportError('');
    renderWorkers(); renderActiveRuns();
    if (state.view === 'history') await loadHistory(state.history.offset);
    else if (state.view === 'home') await loadHomeRuns();
    if (state.run && (state.view === 'run' || state.view === 'task')) {
      await loadRun(state.run, true);
    }
  } catch (e) { if (!silent) reportError(e.message); }
  finally { refreshing = false; }
}

function renderWorkers() {
  const ul = document.getElementById('workers');
  ul.innerHTML = state.workers.map(w => {
    const cls = !w.reachable ? 'failed' : (w.busy ? 'canceled' : 'succeeded');
    const txt = !w.reachable ? 'unreachable' : (w.busy ? 'busy' : 'free');
    const res = w.reachable ? workerResources(w) : '';
    return `<li><span class="dot ${cls}"></span><b>${esc(w.name)}</b> <span class="muted">${txt}</span>` +
      (res ? `<div class="muted" style="font-size:11px; margin-left:15px">${esc(res)}</div>` : '') +
      `</li>`;
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

// ------------------------------------------------------------------- home
async function loadHome() {
  await Promise.all([loadHomePlans(), loadHomeRuns()]);
}

async function loadHomePlans() {
  try {
    const data = await api('/api/plans');
    state.home.plans = data.plans || [];
    renderHomePlans();
  } catch (e) { reportError(e.message); }
}

async function loadHomeRuns() {
  try {
    const data = await api('/api/runs?status=all&limit=8&offset=0');
    state.home.runs = data.runs || [];
    renderHomeRuns();
  } catch (e) { reportError(e.message); }
}

function renderHomePlans() {
  const sel = document.getElementById('home-plan');
  const previous = sel.value;
  const plans = state.home.plans || [];
  sel.innerHTML = plans.length
    ? plans.map(p => `<option value="${esc(p.name)}"${p.error ? ' disabled' : ''}>` +
        `${esc(p.name)} (${p.task_count} task${p.task_count === 1 ? '' : 's'})` +
        `${p.error ? ' — invalid' : ''}</option>`).join('')
    : '<option value="">no plans found</option>';
  if (plans.some(p => p.name === previous)) sel.value = previous;
}

function renderHomeRuns() {
  const runs = state.home.runs || [];
  document.getElementById('home-runs').innerHTML = runs.map(r => {
    return `<button class="runitem history" onclick="openRun('${r.run_id}')">
      <span class="dot ${r.status}"></span><b>${esc(r.plan_name)}</b>
      <span class="muted">${esc(r.status)} &middot; ${fmtTime(r.created_at)} &middot; ${r.task_count} tasks${runDuration(r)}</span>
      <div class="muted" style="font-size:12px">${esc(r.run_id)} &middot; ${runSummary(r)}</div>
    </button>`;
  }).join('') || '<div class="muted">no runs yet</div>';
}

async function startRun() {
  const plan = document.getElementById('home-plan').value;
  const only = document.getElementById('home-only').value
    .split(',').map(s => s.trim()).filter(Boolean);
  const msg = document.getElementById('home-msg');
  if (!plan) { msg.textContent = ' select a plan'; return; }
  msg.textContent = ' starting...';
  try {
    const data = await api('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan, only })
    });
    msg.textContent = ' started ' + data.run_id;
    await loadHomeRuns();
    openRun(data.run_id);
  } catch (e) { msg.textContent = ' ' + e.message; }
}

// ------------------------------------------------------------------ history
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
    return `<button class="runitem history" onclick="openRun('${r.run_id}')">
      <span class="dot ${r.status}"></span><b>${esc(r.plan_name)}</b>
      <span class="muted">${esc(r.status)} &middot; ${fmtTime(r.created_at)} &middot; ${r.task_count} tasks${runDuration(r)}</span>
      <div class="muted" style="font-size:12px">${esc(r.run_id)} &middot; ${runSummary(r)}</div>
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
  stopTaskStream();
  state.view = 'history';
  state.run = null; state.runDetail = null;
  state.task = null; state.taskDetail = null;
  showView(); setUrl();
  await loadHistory(0);
}
// ------------------------------------------------------------------ views
function showView() {
  let view = state.view;
  if (view === 'run' && !state.runDetail) view = 'home';
  if (view === 'task' && !state.taskDetail) view = 'home';
  const home = view === 'home';
  const history = view === 'history';
  const task = view === 'task';
  const run = view === 'run';
  document.getElementById('home-view').classList.toggle('hidden', !home);
  document.getElementById('history-view').classList.toggle('hidden', !history);
  document.getElementById('run-view').classList.toggle('hidden', !run);
  document.getElementById('task-view').classList.toggle('hidden', !task);
}

function openHome() {
  stopTaskStream();
  state.view = 'home';
  state.run = null; state.runDetail = null;
  state.task = null; state.taskDetail = null;
  state.editing = null;
  document.getElementById('editor-panel').classList.add('hidden');
  renderActiveRuns();
  showView(); setUrl();
  loadHome();
}

function openRun(runId) { loadRun(runId); }

function backToRun() {
  stopTaskStream();
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
    stopTaskStream();
    state.task = null; state.taskDetail = null;
    state.view = 'run';
    document.getElementById('editor-panel').classList.add('hidden');
  }
  renderRun(); renderActiveRuns(); showView(); setUrl();
  if (state.view === 'task' && state.task) renderTaskHeader();
}

function renderRun() {
  const d = state.runDetail;
  if (!d) return;
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
  const usage = d.usage || {};
  const attempts = usage.attempts || 0;
  document.getElementById('run-usage').innerHTML =
    `duration ${fmtDur(usage.duration_s) || '-'} &middot; ` +
    `tokens ${tokenBreakdown(usage.tokens)} &middot; cost ${fmtCost(usage.cost)}` +
    ` &middot; attempts ${attempts}`;
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

// ------------------------------------------------------------------- tasks
async function loadTask(taskId) {
  state.task = taskId;
  try { state.taskDetail = await api('/api/runs/' + encodeURIComponent(state.run) + '/tasks/' + encodeURIComponent(taskId)); }
  catch (e) { reportError(e.message); return; }
  state.view = 'task';
  renderTaskHeader();
  renderTaskBody();
  renderRun();
  showView(); setUrl();
}

// Cheap, refresh-safe part of the task view (status/worker/model change live).
function renderTaskHeader() {
  const t = state.runDetail && state.runDetail.tasks.find(x => x.id === state.task);
  if (!t) return;
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
}

// Rebuilt only when a task is opened: the periodic refresh deliberately leaves
// it alone so scroll position, the selected tab and the live stream survive.
let taskTab = 'overview';
let taskTabFor = null;

function renderTaskBody() {
  const t = state.runDetail.tasks.find(x => x.id === state.task);
  if (!t) return;
  const d = state.taskDetail || {};
  const isGate = t.kind === 'gate';
  const s = d.status || {};
  const live = t.status === 'pending' || t.status === 'running';
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

  // Overview: identity, resources and the prompt.
  let overview = '<table>' + rows.map(([k,v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join('') + '</table>';
  if (d.resources && d.resources.length)
    overview += `<p><b>resources</b><pre>${esc(d.resources.map(fmtResource).join('\n\n'))}</pre></p>`;
  if (isGate && t.gate_feedback)
    overview += `<p><b>last gate feedback</b><pre>${esc(t.gate_feedback)}</pre></p>`;
  if (d.request && d.request.prompt)
    overview += `<p><b>prompt</b><pre>${esc(d.request.prompt)}</pre></p>`;

  // Result: what the task produced.
  let result = '';
  if (s.tool_calls && s.tool_calls.length)
    result += `<p><b>tool calls</b><pre>${esc(s.tool_calls.join('\n'))}</pre></p>`;
  if (d.result) result += `<p><b>result</b><pre>${esc(d.result)}</pre></p>`;
  if (t.error) result += `<p><b>error</b><pre>${esc(t.error)}</pre></p>`;
  if (t.changed_files && t.changed_files.length)
    result += `<p><b>changed files</b><pre>${esc(t.changed_files.join('\n'))}</pre></p>`;
  if (!result) result = '<div class="muted">no result yet</div>';

  // Events: live while running, saved once finished.
  let events = '';
  if (live) events += '<pre id="live-events" style="min-height:180px"></pre>';
  if (d.events) events += `<pre>${esc(fmtEvents(d.events))}</pre>`;
  if (!events) events = '<div class="muted">no events yet</div>';

  const tabs = [['overview', 'Overview'], ['events', 'Events'], ['result', 'Result']];
  if (d.stderr) tabs.push(['stderr', 'stderr']);
  if (taskTabFor !== t.id || !tabs.some(([id]) => id === taskTab)) {
    taskTabFor = t.id;
    taskTab = live ? 'events' : 'overview';
  }
  const tabBar = '<div class="tabs" id="task-tabs">' + tabs.map(([id, label]) =>
    `<button type="button" class="tab${id===taskTab?' active':''}" data-tab="${id}" ` +
    `onclick="showTaskTab('${id}')">${esc(label)}</button>`).join('') + '</div>';
  const pane = (id, content) =>
    `<div class="tabpane${id===taskTab?'':' hidden'}" data-pane="${id}">${content}</div>`;
  let html = tabBar + pane('overview', overview) + pane('events', events) + pane('result', result);
  if (d.stderr) html += pane('stderr', `<pre>${esc(d.stderr)}</pre>`);
  document.getElementById('task-body').innerHTML = html;
  // The body is rebuilt on every open, so (re)bind the live event stream to the
  // fresh <pre>; the periodic refresh never calls this, so the stream persists.
  if (live) startTaskStream(state.run, t.id);
  else stopTaskStream();
}

// Switch panes without rebuilding the body, so the live stream keeps running.
function showTaskTab(name) {
  taskTab = name;
  document.querySelectorAll('#task-tabs .tab').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('#task-body .tabpane').forEach(p =>
    p.classList.toggle('hidden', p.dataset.pane !== name));
}

// ------------------------------------------------------- live event stream
let taskStream = null;

function stopTaskStream() {
  if (taskStream) { taskStream.close(); taskStream = null; }
}

// Follow a task's events step by step: the server tails the running worker job
// (or the saved log once the task is done) and pushes each complete line.
function startTaskStream(runId, taskId) {
  const pre = document.getElementById('live-events');
  stopTaskStream();
  if (!pre) return;
  const es = new EventSource(
    `/api/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/events`);
  taskStream = es;
  pre.textContent = '';
  // A reconnect replays the log from the start; clearing avoids duplicates.
  es.onopen = () => { pre.textContent = ''; };
  es.onmessage = ev => {
    const pinned = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
    const line = fmtEvent(ev.data);
    if (line) pre.textContent += line + '\n';
    if (pinned) pre.scrollTop = pre.scrollHeight;
  };
  es.addEventListener('end', () => stopTaskStream());
}

// ------------------------------------------------------------------ actions
// In-page confirmation (no native dialog): explicit labels and observable in
// the browser preview, so "keep" and "confirm" are never confused.
function askConfirm(opts) {
  return new Promise(resolve => {
    const overlay = document.getElementById('confirm-overlay');
    const yes = document.getElementById('confirm-yes');
    const no = document.getElementById('confirm-no');
    document.getElementById('confirm-title').textContent = opts.title || 'Please confirm';
    document.getElementById('confirm-body').textContent = opts.body || '';
    yes.textContent = opts.confirmLabel || 'Confirm';
    no.textContent = opts.dismissLabel || 'Keep';
    const done = value => {
      overlay.classList.add('hidden');
      yes.removeEventListener('click', onYes);
      no.removeEventListener('click', onNo);
      overlay.removeEventListener('click', onOverlay);
      document.removeEventListener('keydown', onKey);
      resolve(value);
    };
    const onYes = () => done(true);
    const onNo = () => done(false);
    const onOverlay = ev => { if (ev.target === overlay) done(false); };
    const onKey = ev => { if (ev.key === 'Escape') done(false); };
    yes.addEventListener('click', onYes);
    no.addEventListener('click', onNo);
    overlay.addEventListener('click', onOverlay);
    document.addEventListener('keydown', onKey);
    overlay.classList.remove('hidden');
    // Focus the safe option: Enter/Space never triggers the destructive action.
    no.focus();
  });
}

async function cancelRun() {
  if (!state.run) return;
  const label = (state.runDetail && state.runDetail.plan_name) || state.run;
  const ok = await askConfirm({
    title: 'Cancel this run?',
    body: label + ' — running tasks are stopped and pending tasks are canceled.',
    confirmLabel: 'Cancel run',
    dismissLabel: 'Keep running'
  });
  if (!ok) return;
  try { await api('/api/runs/' + encodeURIComponent(state.run) + '/cancel', { method:'POST' }); }
  catch (e) { reportError(e.message); }
  await refresh(true);
}

async function resumeRun() {
  if (!state.run) return;
  const ok = await askConfirm({
    title: 'Resume this run?',
    body: state.run + ' — failed, canceled and skipped tasks are re-armed.',
    confirmLabel: 'Resume run',
    dismissLabel: 'Keep'
  });
  if (!ok) return;
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
    const running = state.runDetail && state.runDetail.status === 'running';
    document.getElementById('editor-hint').textContent = running
      ? 'This run is active: saving applies the change to its pending tasks between tasks; running and finished tasks keep the plan they started with.'
      : 'Saving applies the change to this run and, when it is a stored plan, to the plan file.';
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
    if (state.run) loadRun(state.run, true);
  } catch (e) { msg.textContent = ' ' + e.message; }
}

// ---------------------------------------------------------------- routing
function currentPath() {
  if (state.view === 'history') return '/history';
  if (state.view === 'task' && state.run && state.task) {
    const t = state.runDetail && state.runDetail.tasks.find(x => x.id === state.task);
    const seg = (t && t.kind === 'gate') ? 'gate' : 'task';
    return `/run/${encodeURIComponent(state.run)}/${seg}/${encodeURIComponent(state.task)}`;
  }
  if (state.run) return `/run/${encodeURIComponent(state.run)}`;
  return '/';
}

function setUrl() {
  const path = currentPath();
  if (location.pathname + location.search === path) return;
  if (fromRoute) window.history.replaceState(null, '', path);
  else window.history.pushState(null, '', path);
}

async function route() {
  fromRoute = true;
  try {
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
    openHome();
  } finally { fromRoute = false; }
}

window.addEventListener('popstate', () => { route(); });

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
