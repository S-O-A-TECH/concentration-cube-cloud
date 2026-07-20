/* 공용 헬퍼 — 웹캠 프로토 webui/js/api.js 이식 + live-evolution 확장
   (상태 표기·타임라인 렌더러는 여기서만 정의 — 전 화면 공용) */
const STATE_LABEL = {
  focus: '집중', off_task: '이탈', blank_stare: '멍때림', invalid: '측정 낮음',
};
const STATE_CLASS = {
  focus: 'st-focus', off_task: 'st-off_task',
  blank_stare: 'st-blank_stare', invalid: 'st-invalid',
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401 && !location.pathname.startsWith('/login')) {
    location.href = '/login';
    throw new Error('로그인이 필요합니다');
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
const fmtT = (s) => (s >= 60 ? `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초` : `${Math.round(s)}초`);
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
  session: ['세션 (측정 1회)',
    '[시작]부터 종료까지 기기가 눈 움직임을 기록한 "한 판"입니다. 검증 세션은 5분(LAB-5)짜리로 짧게 여러 번 합니다.'],
  truth: ['정답지 (= 라벨)',
    '측정 중 관리자가 "지금 실제 상태"를 버튼으로 기록한 것. 예: "집중해!"라고 지시하며 [집중]을 누른 기록. 기계 판정을 채점하는 기준이 되며, 저장 후에는 수정할 수 없습니다(세션당 1회).'],
  train: ['연습용 (train)',
    'AI에게 보여주는 정답지 묶음. AI는 이것만 보고 개선안을 연습합니다. 어느 세션이 연습용이 될지는 자동 배정되며 사람이 고를 수 없습니다.'],
  holdout: ['시험용 (holdout)',
    'AI에게 절대 보여주지 않는 정답지 묶음(전체의 약 1/4, 자동 배정). 개선안의 "진짜 실력"은 이 숨겨둔 시험지 점수로 판단합니다 — 연습 문제만 잘 푸는 개선안(과적합)을 걸러내는 장치.'],
  paramset: ['판정 기준 (param_set)',
    '집중/이탈/멍때림을 가르는 숫자들의 묶음(임계값·가중치). 진화가 개선하는 대상이며 v1.0 → v1.1-gen1… 버전으로 관리됩니다.'],
  generation: ['세대 (개선 시도 1회)',
    '증거 수집 → AI 제안 → 게이트 시험 → 채택/기각까지 한 바퀴. 기각된 세대도 기록으로 남아 다음 세대의 교훈이 됩니다.'],
  gate: ['게이트 (자동 합격선)',
    '채택 후보가 되기 위한 시험. 시험용(holdout) 세션에서 어떤 상태도 2%p 넘게 나빠지지 않으면서, 고치려던 상태는 좋아져야 통과합니다. 판정은 운영 서버가 합니다.'],
  sens: ['민감도 (잡아내는 비율)',
    '실제로 그 상태였던 구간 중, 기계도 그 상태로 판정한 비율. 예: 멍때림 민감도 60% = 실제 멍때림 10번 중 6번만 잡아냄(4번은 놓침).'],
  spec: ['특이도 (오인하지 않는 비율)',
    '그 상태가 아니었던 구간 중, 기계도 아니라고 본 비율. 낮으면 "아닌데 그렇다고 우기는" 오탐이 많다는 뜻.'],
  bin: ['10초 구간 (bin)',
    '채점의 최소 단위. 5분 세션은 30개 구간으로 잘라 구간마다 정답지와 기계 판정을 비교합니다.'],
  mistake: ['오답',
    '정답지와 기계 판정이 어긋난 10초 구간. 오답이 모인 것이 오답노트이고, AI가 받는 개선 증거의 핵심입니다.'],
  adopt: ['채택 (promote)',
    '게이트를 통과한 개선안을 실제 판정 기준으로 확정하는 "사람의 결정". 채택하면 과거 세션 전체가 새 기준으로 다시 채점되고 즉시 적용됩니다. 세대 이력에서 [롤백]으로 되돌릴 수 있습니다.'],
  exclude: ['연구 제외',
    '실수한 세션을 평가 대상에서만 빼는 것. 삭제가 아니라서 언제든 복원할 수 있고, 원본 신호와 정답지는 영구 보존됩니다.'],
  agent: ['AI 에이전트',
    '이 PC에 로그인된 Claude Code/Codex CLI. 오답 증거를 읽고 판정 기준 개선안을 만들어 주는 "도구"이며, 제안까지만 합니다 — 채택은 언제나 사람.'],
  labeler: ['기록자 (labeler)',
    '정답지 버튼을 누른 사람. 임상에서 복수 연구원이 참여할 때 누가 기록했는지 구분하기 위한 항목입니다.'],
  coverage: ['측정 낮음 (invalid)',
    '얼굴/시선을 못 읽은 구간. 이탈로 세지 않고 채점에서 제외합니다 — "측정 실패 ≠ 딴짓" 원칙.'],
};

function showTerm(key) {
  const t = TERMS[key];
  if (!t) return;
  const dlg = document.createElement('dialog');
  dlg.innerHTML = `<h3>${esc(t[0])}</h3><div class="body">${esc(t[1])}</div>
    <div class="row" style="justify-content:space-between">
      <button class="ghost small" value="all">📖 용어 전체 보기</button>
      <button class="small" value="ok">닫기</button></div>`;
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
  dlg.innerHTML = `<h3>📖 용어 사전</h3>
    <div class="body" style="max-height:60vh;overflow-y:auto">` +
    Object.values(TERMS).map(([name, def]) =>
      `<p style="margin-bottom:10px"><b>${esc(name)}</b><br><span class="sub">${esc(def)}</span></p>`
    ).join('') +
    `</div><div class="row" style="justify-content:flex-end"><button class="small">닫기</button></div>`;
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
  IDLE: '대기 중', COLLECT: '① 증거 수집', PROPOSE: '② AI 제안 작성 중',
  REVIEW_DIFF: '③ 사람 검토 대기', REGISTERED: '④ 후보 등록', EVALUATING: '⑤ 게이트 시험 중',
  PASSED: '✅ 게이트 통과 — 채택 대기', REJECTED: '기각됨', FAILED: '실패',
};
const loopStateKo = (s) => LOOP_STATE_KO[s] || s;

/* 주황 배너 내비게이션 — 운영 콘솔(남색)과 즉시 구별 (SPEC-03) */
const NAV_ITEMS = [
  ['/', '대시보드'], ['/console', '검증 세션'], ['/archive', '아카이브'],
  ['/mistakes', '오답노트'], ['/evolve', '진화 실행'], ['/report', '성적표'],
  ['/generations', '세대 이력'], ['/agent', '에이전트'], ['/settings', '설정'],
];
function injectNav(active) {
  const el = document.createElement('div');
  el.className = 'banner no-print';
  el.innerHTML = `<span class="brand">🧬 Live Evolution<small>임상 검증 콘솔</small></span>`
    + NAV_ITEMS.map(([p, t]) =>
      `<a href="${p}" class="${p === active ? 'on' : ''}">${t}</a>`).join('')
    + `<a href="#" id="nav-glossary" title="용어 사전">📖 용어</a>`
    + `<a href="#" id="nav-logout" title="로그아웃">↩</a>`;
  document.querySelector('main').prepend(el);
  el.querySelector('#nav-glossary').onclick = (e) => { e.preventDefault(); showGlossary(); };
  el.querySelector('#nav-logout').onclick = async (e) => {
    e.preventDefault();
    await post('/api/logout');
    location.href = '/login';
  };
}

/* 확인 모달 — 모든 파괴적 버튼은 이걸 거친다 (SPEC-03 공통 규칙) */
function confirmModal({ title, body, confirmText = '확인', danger = false }) {
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.innerHTML = `<h3>${esc(title)}</h3><div class="body">${body}</div>
      <div class="row" style="justify-content:flex-end">
        <button class="ghost" value="no">취소</button>
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
function promptModal({ title, body, placeholder = '사유 (필수)', confirmText = '확인', danger = false }) {
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.innerHTML = `<h3>${esc(title)}</h3><div class="body">${body || ''}</div>
      <input type="text" id="pm-input" placeholder="${esc(placeholder)}" style="margin-bottom:14px">
      <div class="row" style="justify-content:flex-end">
        <button class="ghost" value="no">취소</button>
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
