"""FastAPI 조립 (SPEC-00 §3) — 127.0.0.1:8200 전용, 화면 9종 + JSON API.

라우트 계층은 얇다: 판단은 machine/console/validate 가, 원천 데이터는 운영 서버가.
운영 서버 미연결이어도 화면은 뜨고 데이터 영역에 연결 안내를 보인다 (SPEC-03 공통 규칙).
"""
from __future__ import annotations

import datetime as dt
import subprocess
import threading
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import APP_VERSION, WEBUI_DIR, db
from .agents import ADAPTERS, get_adapter
from .auth import COOKIE_NAME, current_user, issue_cookie, require_api_auth, try_login
from .config import get_config
from .console import get_console, get_presets
from .device_client import DeviceClient, DeviceError
from .loop.machine import get_loop
from .server_client import OpsError, get_ops
from .triggers import get_triggers, record_generation_baseline

app = FastAPI(title="concentration-cube live-evolution", version=APP_VERSION)


@app.on_event("startup")
def _startup():
    db.connect()
    get_loop()            # 재시작 시 loop_state 복원 (EVALUATING 폴링 재개 포함)
    get_triggers().start()


# ------------------------------------------------------------------ 공통 헬퍼

def _ops_guard(fn, *a, **kw):
    """OpsError → HTTP 오류 (사용자 문장 그대로)."""
    try:
        return fn(*a, **kw)
    except OpsError as e:
        raise HTTPException(e.status if (e.status and e.status < 500) else 502, e.user_msg)


class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._val = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self, producer):
        with self._lock:
            if self._val is None or time.monotonic() - self._at > self.ttl:
                self._val = producer()
                self._at = time.monotonic()
            return self._val

    def invalidate(self):
        with self._lock:
            self._val = None


_ops_status_cache = _TTLCache(30.0)     # E1 §1.2 — 30s
_device_status_cache = _TTLCache(30.0)
_agent_cache: dict[str, dict] = {}      # {name: {detect, auth, at}} — 인증 30분 캐시 (SPEC-01 §2)


def _ops_status() -> dict:
    def probe():
        cfg = get_config()
        try:
            h = get_ops().health()
            ov = get_ops().overview()
            return {"ok": True, "url": cfg.server_url, "health": h, "overview": ov}
        except OpsError as e:
            return {"ok": False, "url": cfg.server_url, "error": e.user_msg}
    return _ops_status_cache.get(probe)


def _device_status() -> dict:
    def probe():
        cfg = get_config()
        try:
            h = DeviceClient().health()
            return {"ok": True, "url": cfg.device_url, "health": h}
        except DeviceError as e:
            return {"ok": False, "url": cfg.device_url, "error": e.user_msg}
    return _device_status_cache.get(probe)


def _agent_status(name: str, force_auth: bool = False) -> dict:
    ttl_min = float(db.get_setting("agent_auth_cache_min", 30))
    ent = _agent_cache.get(name)
    if ent and not force_auth and time.monotonic() - ent["at"] < ttl_min * 60:
        return ent["view"]
    adapter = get_adapter(name)
    det = adapter.detect()
    auth = adapter.check_auth() if det.installed else None
    view = {
        "name": name, "display": adapter.display,
        "installed": det.installed, "version": det.version,
        "detect_command": det.command, "detect_error": det.error,
        "install_hint": adapter.install_hint,
        "auth_ok": bool(auth and auth.ok),
        "auth_detail": auth.detail if auth else "",
        "auth_command": auth.command if auth else "",
        "checked_at": auth.checked_at if auth else "",
    }
    _agent_cache[name] = {"at": time.monotonic(), "view": view}
    return view


# ------------------------------------------------------------------ 로그인

class LoginReq(BaseModel):
    id: str
    pw: str


@app.post("/api/login")
def api_login(req: LoginReq):
    ok, msg = try_login(req.id, req.pw)
    if not ok:
        raise HTTPException(401, msg)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE_NAME, issue_cookie(req.id), max_age=24 * 3600,
                    httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/api/health")
def api_health():
    return {"ok": True, "app": "live-evolution", "version": APP_VERSION}


# ------------------------------------------------------------------ 대시보드 (②)

@app.get("/api/dashboard")
def api_dashboard(user: str = Depends(require_api_auth)):
    cfg = get_config()
    loop = get_loop()
    ops = _ops_status()
    recent = []
    for p in db.list_proposals(limit=10):
        if p["status"] in ("collecting",):
            continue
        recent.append({"gen_id": p["gen_id"], "version": p["version"],
                       "status": p["status"], "created_at": p["created_at"],
                       "param_set_id": p["param_set_id"]})
        if len(recent) >= 3:
            break
    runs = db.agent_runs(purpose="propose", limit=200)
    today = dt.date.today().isoformat()
    today_runs = [r for r in runs if (r["started_at"] or "").startswith(today)]
    return {
        "ops": ops,
        "device": _device_status(),
        "agent": {
            "default": db.get_setting("agent_default", cfg.agent_default),
            "agents": [_agent_cache[n]["view"] if n in _agent_cache else
                       {"name": n, "display": ADAPTERS[n].display, "installed": None}
                       for n in ADAPTERS],
        },
        "loop": {"state": loop.state, "gen_id": loop.gen_id},
        "notice": get_triggers().notice,
        "recent_generations": recent,
        "today": {"runs": len(today_runs),
                  "cost_usd": round(sum(r["cost_usd"] or 0 for r in today_runs), 4),
                  "limit": db.get_setting("daily_run_limit", cfg.daily_run_limit)},
        "consecutive_rejects": db.consecutive_rejects(),
    }


# ------------------------------------------------------------------ 에이전트 (③)

@app.get("/api/agents")
def api_agents(probe: bool = False, user: str = Depends(require_api_auth)):
    cfg = get_config()
    agents = [_agent_status(n) if (probe or n in _agent_cache) else
              {"name": n, "display": ADAPTERS[n].display, "installed": None}
              for n in ADAPTERS]
    return {"default": db.get_setting("agent_default", cfg.agent_default), "agents": agents}


@app.post("/api/agents/{name}/check")
def api_agent_check(name: str, user: str = Depends(require_api_auth)):
    if name not in ADAPTERS:
        raise HTTPException(404, "알 수 없는 에이전트")
    return _agent_status(name, force_auth=True)


@app.post("/api/agents/{name}/terminal")
def api_agent_terminal(name: str, user: str = Depends(require_api_auth)):
    """[터미널 열기] — CLI 가 OAuth 브라우저를 띄운다 (SPEC-01 §2 ③)."""
    if name not in ADAPTERS:
        raise HTTPException(404, "알 수 없는 에이전트")
    try:
        subprocess.Popen(get_adapter(name).terminal_command())
    except OSError as e:
        raise HTTPException(500, f"터미널 열기 실패: {e}")
    return {"ok": True, "hint": "터미널에서 로그인을 마친 뒤 [다시 확인]을 눌러 주세요."}


@app.post("/api/agents/{name}/select")
def api_agent_select(name: str, user: str = Depends(require_api_auth)):
    if name not in ADAPTERS:
        raise HTTPException(404, "알 수 없는 에이전트")
    db.set_setting("agent_default", name)
    return {"ok": True, "default": name}


# ------------------------------------------------------------------ 검증 세션 콘솔 (④)

class ConsoleStartReq(BaseModel):
    preset: str | None = None
    labeler: str = "admin"
    skip_calibration: bool = False


class CalibReq(BaseModel):
    command: str


class MarkReq(BaseModel):
    button: str


class ReasonReq(BaseModel):
    reason: str = ""


def _console_guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (RuntimeError, DeviceError) as e:
        msg = e.user_msg if isinstance(e, DeviceError) else str(e)
        raise HTTPException(400, msg)


@app.post("/api/console/start")
def api_console_start(req: ConsoleStartReq, user: str = Depends(require_api_auth)):
    return _console_guard(get_console().start, req.preset, req.labeler, req.skip_calibration)


@app.get("/api/console/state")
def api_console_state(user: str = Depends(require_api_auth)):
    return get_console().state()


@app.post("/api/console/calibrate")
def api_console_calibrate(req: CalibReq, user: str = Depends(require_api_auth)):
    return _console_guard(get_console().calibrate, req.command)


@app.post("/api/console/mark")
def api_console_mark(req: MarkReq, user: str = Depends(require_api_auth)):
    return _console_guard(get_console().mark, req.button)


@app.post("/api/console/finish")
def api_console_finish(user: str = Depends(require_api_auth)):
    return _console_guard(get_console().finish)


@app.post("/api/console/save")
def api_console_save(user: str = Depends(require_api_auth)):
    return _console_guard(get_console().save)


@app.post("/api/console/discard")
def api_console_discard(req: ReasonReq, user: str = Depends(require_api_auth)):
    return _console_guard(get_console().discard, req.reason or "프로토콜 실패")


@app.get("/api/console/presets")
def api_console_presets(user: str = Depends(require_api_auth)):
    return {"presets": get_presets()}


# ------------------------------------------------------------------ 오답노트 (⑤)

@app.get("/api/mistakes")
def api_mistakes(param_set_id: str | None = None, user: str = Depends(require_api_auth)):
    return _ops_guard(get_ops().mistakes, param_set_id)


# ------------------------------------------------------------------ 진화 실행 (⑥)

class RunReq(BaseModel):
    agent: str | None = None


class NoteReq(BaseModel):
    note: str = ""


@app.get("/api/loop")
def api_loop(user: str = Depends(require_api_auth)):
    loop = get_loop()
    st = loop.status()
    ok, reason = loop.can_run() if loop.state == "IDLE" else (False, "")
    st["can_run"] = ok
    st["can_run_reason"] = reason
    st["estimate"] = loop.estimate()
    return st


@app.post("/api/loop/run")
def api_loop_run(req: RunReq, user: str = Depends(require_api_auth)):
    try:
        res = get_loop().run(agent_name=req.agent)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    try:
        ov = get_ops().overview()
        record_generation_baseline(int(ov.get("labeled_realtime", 0)))
        get_triggers().clear()
    except OpsError:
        pass
    return res


@app.post("/api/loop/register")
def api_loop_register(user: str = Depends(require_api_auth)):
    try:
        return get_loop().register()
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/loop/dismiss")
def api_loop_dismiss(req: NoteReq, user: str = Depends(require_api_auth)):
    try:
        get_loop().dismiss(req.note)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# ------------------------------------------------------------------ 성적표·세대 (⑦⑧)

class PromoteReq(BaseModel):
    confirm: bool = False


@app.get("/api/param_sets")
def api_param_sets(user: str = Depends(require_api_auth)):
    return {"param_sets": _ops_guard(get_ops().param_sets)}


@app.get("/api/param_sets/{pid}/report")
def api_param_set_report(pid: str, user: str = Depends(require_api_auth)):
    return _ops_guard(get_ops().report, pid)


@app.post("/api/param_sets/{pid}/promote")
def api_promote(pid: str, req: PromoteReq, user: str = Depends(require_api_auth)):
    if not req.confirm:
        raise HTTPException(400, "confirm 이 필요합니다 — 확인 모달을 거쳐 주세요.")
    res = _ops_guard(get_ops().promote, pid, True)   # 게이트 미통과면 운영 서버가 409 (이중 방어)
    get_loop().mark_adopted(pid)
    try:
        ov = get_ops().overview()
        record_generation_baseline(int(ov.get("labeled_realtime", 0)))
    except OpsError:
        pass
    _ops_status_cache.invalidate()
    return res


@app.post("/api/param_sets/{pid}/reject")
def api_reject(pid: str, req: ReasonReq, user: str = Depends(require_api_auth)):
    res = _ops_guard(get_ops().reject, pid, req.reason)
    loop = get_loop()
    if loop.state == "REJECTED":
        loop.dismiss(req.reason or "기각 확정")
    return res


@app.post("/api/param_sets/{pid}/rollback")
def api_rollback(pid: str, req: ReasonReq, user: str = Depends(require_api_auth)):
    res = _ops_guard(get_ops().rollback, pid, req.reason)
    get_loop().mark_rolled_back(pid)
    _ops_status_cache.invalidate()
    return res


@app.get("/api/jobs")
def api_jobs(user: str = Depends(require_api_auth)):
    return {"jobs": _ops_guard(get_ops().jobs)}


@app.get("/api/generations")
def api_generations(user: str = Depends(require_api_auth)):
    """세대 이력 = 운영 서버 param_sets(정본) + 이 서버 proposals/agent_runs 조인 (SPEC-02 §3)."""
    try:
        param_sets = get_ops().param_sets()
        ops_err = None
    except OpsError as e:
        param_sets = []
        ops_err = e.user_msg
    local = {p["param_set_id"]: p for p in db.list_proposals(limit=200) if p["param_set_id"]}
    for ps in param_sets:
        lp = local.get(ps["id"])
        ps["local"] = {"gen_id": lp["gen_id"], "agent": lp["agent"],
                       "status": lp["status"]} if lp else None
    return {"param_sets": param_sets, "ops_error": ops_err,
            "local_only": [p for p in db.list_proposals(limit=50) if not p["param_set_id"]]}


@app.get("/api/generations/{gen_id}")
def api_generation_detail(gen_id: str, user: str = Depends(require_api_auth)):
    p = db.get_proposal(gen_id)
    if not p:
        raise HTTPException(404, "해당 세대 기록이 없습니다")
    runs = db.agent_runs(gen_id=gen_id, purpose="propose", limit=10)
    return {"proposal_row": p, "agent_runs": runs}


# ------------------------------------------------------------------ 아카이브 (⑨)

@app.get("/api/archive")
def api_archive(date_from: str | None = None, date_to: str | None = None,
                split: str | None = None, method: str | None = None,
                include_excluded: bool = True, user: str = Depends(require_api_auth)):
    sessions = _ops_guard(get_ops().sessions, None, None, None, True)
    out = []
    for s in sessions:
        if not include_excluded and s.get("excluded"):
            continue
        if split and s.get("split") != split:
            continue
        if method and (s.get("label") or {}).get("method") != method:
            continue
        day = (s.get("started_at") or "")[:10]
        if date_from and day and day < date_from:
            continue
        if date_to and day and day > date_to:
            continue
        out.append(s)
    labeled = [s for s in out if s.get("labeled")]
    stats = {
        "total": len(out),
        "labeled": len(labeled),
        "label_minutes": round(sum((s.get("label") or {}).get("label_sec", 0) for s in labeled) / 60, 1),
        "train": len([s for s in labeled if s.get("split") == "train"]),
        "holdout": len([s for s in labeled if s.get("split") == "holdout"]),
        "excluded": len([s for s in out if s.get("excluded")]),
        "date_range": [min((s["started_at"] for s in out), default=None),
                       max((s["started_at"] for s in out), default=None)],
    }
    return {"sessions": out, "stats": stats}


@app.get("/api/archive/{sid}")
def api_archive_detail(sid: str, user: str = Depends(require_api_auth)):
    detail = _ops_guard(get_ops().session_detail, sid)
    try:
        runs = get_ops().session_runs(sid)
    except OpsError:
        runs = []
    detail["runs"] = runs
    detail.pop("classifier_inputs", None)   # 화면에는 불필요 (Evidence 전용)
    return detail


@app.post("/api/archive/{sid}/exclude")
def api_archive_exclude(sid: str, req: ReasonReq, user: str = Depends(require_api_auth)):
    if not req.reason.strip():
        raise HTTPException(400, "사유는 필수입니다 (audit 기록)")
    return _ops_guard(get_ops().exclude, sid, req.reason)


@app.post("/api/archive/{sid}/restore")
def api_archive_restore(sid: str, req: ReasonReq, user: str = Depends(require_api_auth)):
    if not req.reason.strip():
        raise HTTPException(400, "사유는 필수입니다 (audit 기록)")
    return _ops_guard(get_ops().restore, sid, req.reason)


# ------------------------------------------------------------------ 설정

_SETTINGS_KEYS = {
    "agent_default", "agent_timeout_sec", "daily_run_limit", "trigger_n",
    "auto_propose", "protocol_presets", "debug_show_live_state",
    "session_filter", "agent_claude_args", "agent_codex_args", "agent_auth_cache_min",
    "agent_auth_timeout_sec",
}


@app.get("/api/settings")
def api_settings(user: str = Depends(require_api_auth)):
    cfg = get_config()
    values = {k: db.get_setting(k) for k in sorted(_SETTINGS_KEYS)}
    values["protocol_presets"] = get_presets()
    defaults = {"agent_default": cfg.agent_default, "agent_timeout_sec": cfg.agent_timeout_sec,
                "daily_run_limit": cfg.daily_run_limit, "trigger_n": cfg.trigger_n,
                "auto_propose": False, "debug_show_live_state": False}
    env_info = {"SERVER_URL": cfg.server_url, "DEVICE_URL": cfg.device_url,
                "EVOLUTION_TOKEN_set": bool(cfg.evolution_token),
                "state_dir": str(cfg.state_dir)}
    gate_rule = None
    ops = _ops_status()
    if ops.get("ok"):
        gate_rule = (ops.get("overview") or {}).get("gate_rule")
    return {"values": values, "defaults": defaults, "env": env_info, "gate_rule": gate_rule}


_NUMERIC_SETTINGS = {"agent_timeout_sec": (60, 3600), "daily_run_limit": (1, 200),
                     "trigger_n": (1, 50), "agent_auth_cache_min": (1, 240),
                     "agent_auth_timeout_sec": (15, 300)}
_BOOL_SETTINGS = {"auto_propose", "debug_show_live_state"}


@app.post("/api/settings")
async def api_settings_post(request: Request, user: str = Depends(require_api_auth)):
    body = await request.json()
    unknown = set(body) - _SETTINGS_KEYS
    if unknown:
        raise HTTPException(400, f"알 수 없는 설정 키: {sorted(unknown)}")
    for k, v in body.items():
        if k in _NUMERIC_SETTINGS:
            lo, hi = _NUMERIC_SETTINGS[k]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                raise HTTPException(400, f"{k}: {lo}~{hi} 범위의 숫자여야 합니다")
        if k in _BOOL_SETTINGS and not isinstance(v, bool):
            raise HTTPException(400, f"{k}: true/false 여야 합니다")
    for k, v in body.items():
        db.set_setting(k, v)
    return {"ok": True, "saved": sorted(body)}


@app.post("/api/notice/clear")
def api_notice_clear(user: str = Depends(require_api_auth)):
    get_triggers().clear()
    return {"ok": True}


# ------------------------------------------------------------------ 페이지 (정적 HTML + 인증 리다이렉트)

_PAGES = {
    "/": "index.html", "/agent": "agent.html", "/console": "console.html",
    "/mistakes": "mistakes.html", "/evolve": "evolve.html", "/report": "report.html",
    "/generations": "generations.html", "/archive": "archive.html",
    "/settings": "settings.html",
}


@app.get("/login")
def page_login():
    return FileResponse(WEBUI_DIR / "login.html")


def _page_route(fname: str):
    def handler(request: Request):
        if not current_user(request):
            return RedirectResponse("/login", status_code=302)
        return FileResponse(WEBUI_DIR / fname)
    return handler


for path, fname in _PAGES.items():
    app.get(path, include_in_schema=False)(_page_route(fname))

app.mount("/static", StaticFiles(directory=WEBUI_DIR), name="static")
