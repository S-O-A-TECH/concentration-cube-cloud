/* 공용 헬퍼 — 웹캠 프로토 webui/js/api.js 이식 + live-evolution 확장
   (상태 표기·타임라인 렌더러는 여기서만 정의 — 전 화면 공용) */
const STATE_LABEL = {
  focus: 'Focus', off_task: 'Off-task', blank_stare: 'Blank stare', invalid: 'Low signal',
};
const STATE_CLASS = {
  focus: 'st-focus', off_task: 'st-off_task',
  blank_stare: 'st-blank_stare', invalid: 'st-invalid',
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401 && !location.pathname.startsWith('/login')) {
    location.href = '/login';
    throw new Error('Login required');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}
const post = (path, body) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});

const qs = (k) => new URLSearchParams(location.search).get(k);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtMMSS = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const fmtT = (s) => (s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`);
/* 일시 표기 표준: YYYY-MM-DD HH:MM (SPEC-03 공통 규칙) */
const fmtDT = (iso) => (iso ? `${iso.slice(0, 10)} ${iso.slice(11, 16)}` : '—');
const pct = (v) => (v == null ? '—' : (v * 100).toFixed(1) + '%');

function renderTimeline(el, timeline, total) {
  el.innerHTML = '';
  if (!timeline || !timeline.length) return;
  const t1 = total || timeline[timeline.length - 1].t1 || 1;
  for (const seg of timeline) {
    const d = document.createElement('div');
    d.className = STATE_CLASS[seg.state || seg.label] || 'st-invalid';
    d.style.width = `${((seg.t1 - seg.t0) / t1) * 100}%`;
    d.title = `${STATE_LABEL[seg.state || seg.label]} ${fmtT(seg.t0)}~${fmtT(seg.t1)}`;
    el.appendChild(d);
  }
}
function renderLegend(el) {
  el.innerHTML = Object.keys(STATE_LABEL).map(
    (k) => `<span><i class="${STATE_CLASS[k]}"></i>${STATE_LABEL[k]}</span>`
  ).join('');
}

/* ── 용어 사전 (전 화면 공용) — 점선 밑줄 용어를 클릭하면 뜻이 뜬다 ──
   HTML 쪽 사용법: <i class="term" data-term="holdout">시험용</i> */
const TERMS = {
  session: ['Session (one measurement)',
    'One "take" where the device recorded eye movement from [Start] to the end. Validation sessions are short 5-minute (LAB-5) takes, done many times.'],
  truth: ['Ground truth (= labels)',
    'What the administrator recorded with a button as "the actual state right now" during measurement. E.g. pressing [Focus] while instructing "focus now". It is the basis for scoring the machine\'s judgment, and cannot be edited after saving (once per session).'],
  train: ['Training set (train)',
    'The bundle of ground-truth labels shown to the AI. The AI practices its proposals on this alone. Which sessions become training is assigned automatically — a human cannot choose.'],
  holdout: ['Holdout set (holdout)',
    'The bundle of ground-truth labels never shown to the AI (about 1/4 of the total, auto-assigned). A proposal\'s "real ability" is judged by this hidden exam score — a device that filters out proposals that only ace the practice problems (overfitting).'],
  paramset: ['Decision criteria (param_set)',
    'The bundle of numbers (thresholds/weights) that separate focus/off-task/blank-stare. This is what evolution improves, versioned as v1.0 → v1.1-gen1…'],
  generation: ['Generation (one improvement attempt)',
    'One full loop: evidence collection → AI proposal → gate exam → adopt/reject. Rejected generations are also kept on record as lessons for the next generation.'],
  gate: ['Gate (automatic pass line)',
    'The exam a candidate must pass to become adoptable. It passes if, on the holdout sessions, no state degrades by more than 2%p while the state being fixed improves. The operations server makes the call.'],
  sens: ['Sensitivity (catch rate)',
    'Of the bins that were actually in that state, the fraction the machine also judged to be in it. E.g. blank-stare sensitivity 60% = of 10 real blank-stares it catches only 6 (misses 4).'],
  spec: ['Specificity (non-misjudgment rate)',
    'Of the bins that were NOT in that state, the fraction the machine also judged as not. Low means many false alarms — "insisting it is when it isn\'t".'],
  bin: ['10-second bin (bin)',
    'The smallest unit of scoring. A 5-minute session is cut into 30 bins, and each bin compares ground truth against the machine\'s judgment.'],
  mistake: ['Mistake',
    'A 10-second bin where ground truth and the machine\'s judgment disagree. Collected mistakes form the Mistake Log, the core improvement evidence the AI receives.'],
  adopt: ['Adopt (promote)',
    'The "human decision" to confirm a gate-passed proposal as the actual decision criteria. On adoption, all past sessions are re-scored with the new criteria and it applies immediately. It can be undone with [Rollback] in the generation history.'],
  exclude: ['Exclude from research',
    'Removing a mistaken session from evaluation only. Not a deletion, so it can be restored anytime, and the raw signal and ground-truth labels are preserved permanently.'],
  agent: ['AI agent',
    'A Claude Code/Codex CLI logged in on this PC. A "tool" that reads mistake evidence and produces proposals to improve the decision criteria — it only proposes; adoption is always a human.'],
  labeler: ['Labeler',
    'The person who pressed the ground-truth buttons. Used to tell apart who recorded, for clinical settings where multiple researchers take part.'],
  coverage: ['Low signal (invalid)',
    'A bin where the face/gaze could not be read. Not counted as off-task and excluded from scoring — the "measurement failure ≠ distraction" principle.'],
};

function showTerm(key) {
  const t = TERMS[key];
  if (!t) return;
  const dlg = document.createElement('dialog');
  dlg.innerHTML = `<h3>${esc(t[0])}</h3><div class="body">${esc(t[1])}</div>
    <div class="row" style="justify-content:space-between">
      <button class="ghost small" value="all">📖 View all terms</button>
      <button class="small" value="ok">Close</button></div>`;
  document.body.appendChild(dlg);
  dlg.querySelectorAll('button').forEach((b) => b.onclick = () => {
    dlg.close(); dlg.remove();
    if (b.value === 'all') showGlossary();
  });
  dlg.showModal();
}

function showGlossary() {
  const dlg = document.createElement('dialog');
  dlg.style.maxWidth = '560px';
  dlg.innerHTML = `<h3>📖 Glossary</h3>
    <div class="body" style="max-height:60vh;overflow-y:auto">` +
    Object.values(TERMS).map(([name, def]) =>
      `<p style="margin-bottom:10px"><b>${esc(name)}</b><br><span class="sub">${esc(def)}</span></p>`
    ).join('') +
    `</div><div class="row" style="justify-content:flex-end"><button class="small">Close</button></div>`;
  document.body.appendChild(dlg);
  dlg.querySelector('button').onclick = () => { dlg.close(); dlg.remove(); };
  dlg.showModal();
}

/* 용어 클릭 위임 — 동적으로 렌더된 .term 도 동작 */
document.addEventListener('click', (e) => {
  const el = e.target.closest('.term');
  if (el && el.dataset.term) { e.preventDefault(); showTerm(el.dataset.term); }
});
const term = (key, text) => `<i class="term" data-term="${key}">${esc(text)}</i>`;

/* 진화 루프 상태의 한국어 표기 (대시보드·진화 실행 공용) */
const LOOP_STATE_KO = {
  IDLE: 'Idle', COLLECT: '① Collecting evidence', PROPOSE: '② AI drafting proposal',
  REVIEW_DIFF: '③ Awaiting human review', REGISTERED: '④ Candidate registered', EVALUATING: '⑤ Gate exam running',
  PASSED: '✅ Gate passed — awaiting adoption', REJECTED: 'Rejected', FAILED: 'Failed',
};
const loopStateKo = (s) => LOOP_STATE_KO[s] || s;

/* 주황 배너 내비게이션 — 운영 콘솔(남색)과 즉시 구별 (SPEC-03) */
const NAV_ITEMS = [
  ['/', 'Dashboard'], ['/console', 'Validation session'], ['/archive', 'Archive'],
  ['/mistakes', 'Mistake Log'], ['/evolve', 'Run evolution'], ['/report', 'Gate report'],
  ['/generations', 'Generations'], ['/agent', 'Agent'], ['/settings', 'Settings'],
];
function injectNav(active) {
  const el = document.createElement('div');
  el.className = 'banner no-print';
  el.innerHTML = `<span class="brand">🧬 Live Evolution<small>Clinical validation console</small></span>`
    + NAV_ITEMS.map(([p, t]) =>
      `<a href="${p}" class="${p === active ? 'on' : ''}">${t}</a>`).join('')
    + `<a href="#" id="nav-glossary" title="Glossary">📖 Glossary</a>`
    + `<a href="#" id="nav-logout" title="Logout">↩</a>`;
  document.querySelector('main').prepend(el);
  el.querySelector('#nav-glossary').onclick = (e) => { e.preventDefault(); showGlossary(); };
  el.querySelector('#nav-logout').onclick = async (e) => {
    e.preventDefault();
    await post('/api/logout');
    location.href = '/login';
  };
}

/* 확인 모달 — 모든 파괴적 버튼은 이걸 거친다 (SPEC-03 공통 규칙) */
function confirmModal({ title, body, confirmText = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.innerHTML = `<h3>${esc(title)}</h3><div class="body">${body}</div>
      <div class="row" style="justify-content:flex-end">
        <button class="ghost" value="no">Cancel</button>
        <button class="${danger ? 'danger' : ''}" value="yes">${esc(confirmText)}</button>
      </div>`;
    document.body.appendChild(dlg);
    dlg.querySelectorAll('button').forEach((b) => b.onclick = () => {
      dlg.close(); dlg.remove(); resolve(b.value === 'yes');
    });
    dlg.showModal();
  });
}

/* 사유 입력 모달 — 제외/복원/폐기 등 audit 사유가 필수인 동작용 */
function promptModal({ title, body, placeholder = 'Reason (required)', confirmText = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.innerHTML = `<h3>${esc(title)}</h3><div class="body">${body || ''}</div>
      <input type="text" id="pm-input" placeholder="${esc(placeholder)}" style="margin-bottom:14px">
      <div class="row" style="justify-content:flex-end">
        <button class="ghost" value="no">Cancel</button>
        <button class="${danger ? 'danger' : ''}" value="yes">${esc(confirmText)}</button>
      </div>`;
    document.body.appendChild(dlg);
    const input = dlg.querySelector('#pm-input');
    dlg.querySelectorAll('button').forEach((b) => b.onclick = () => {
      if (b.value === 'yes' && !input.value.trim()) { input.focus(); return; }
      const v = b.value === 'yes' ? input.value.trim() : null;
      dlg.close(); dlg.remove(); resolve(v);
    });
    dlg.showModal();
    input.focus();
  });
}

function toast(msg, isErr) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);'
      + 'padding:12px 22px;border-radius:999px;color:#fff;font-weight:600;z-index:99;'
      + 'box-shadow:0 8px 24px rgba(0,0,0,.25);transition:opacity .3s';
    document.body.appendChild(t);
  }
  t.style.background = isErr ? 'var(--bad)' : 'var(--ink)';
  t.textContent = msg;
  t.style.opacity = '1';
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.style.opacity = '0'; }, 3200);
}
const showErr = (e) => toast(e.message || String(e), true);
