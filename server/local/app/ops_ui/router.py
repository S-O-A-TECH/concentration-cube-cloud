"""운영 콘솔 내장 UI — /ops (Jinja 서버렌더링, S6 §2.2).

인증은 /v1/ops 와 같은 서명 쿠키. 미로그인 페이지 접근은 /ops/login 으로 리다이렉트.
데이터는 /v1/ops API 함수를 직접 호출해 재사용한다 (계약 単一화).
"""
from __future__ import annotations

import uuid
from pathlib import Path

import jwt
from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api import ops as ops_api
from app.api.deps import JWT_ALGO, get_db
from app.config import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/ops", include_in_schema=False)


def _logged_in(ops_session: str) -> bool:
    try:
        claims = jwt.decode(ops_session, get_settings().jwt_secret,
                            algorithms=[JWT_ALGO])
        return claims.get("role") == "ops"
    except jwt.PyJWTError:
        return False


def _page(request: Request, name: str, ops_session: str, **ctx):
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    return templates.TemplateResponse(request, name, ctx)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def index(request: Request, ops_session: str = Cookie(default="")):
    return RedirectResponse("/ops/dashboard" if _logged_in(ops_session)
                            else "/ops/login", status_code=302)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, ops_session: str = Cookie(default=""),
                   db: Session = Depends(get_db)):
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    return _page(request, "dashboard.html", ops_session,
                 data=ops_api.dashboard(db=db))


@router.get("/dashboard/{country}", response_class=HTMLResponse)
def dashboard_country_page(request: Request, country: str,
                           ops_session: str = Cookie(default=""),
                           db: Session = Depends(get_db)):
    """국가 상세 — 기간(2/4/6/12개월)별 구독 인원 → 클릭하면 계정 목록으로."""
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    data = ops_api.dashboard(db=db)
    card = next((c for c in data["subscriptions"]["countries"]
                 if c["country"] == country), None)
    if card is None:
        return RedirectResponse("/ops/dashboard", status_code=302)
    return _page(request, "country.html", ops_session, card=card)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, q: str | None = None, offset: int = 0,
               ops_session: str = Cookie(default=""),
               db: Session = Depends(get_db)):
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    return _page(request, "users.html", ops_session,
                 data=ops_api.list_users(q=q, offset=offset, db=db),
                 q=q or "", offset=offset)


@router.get("/users/{user_id}", response_class=HTMLResponse)
def user_detail_page(request: Request, user_id: int,
                     ops_session: str = Cookie(default=""),
                     db: Session = Depends(get_db)):
    """계정 상세 — 프로필별 최근 데이터 시간순, 클릭하면 리포트로 (2026-07-07)."""
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    try:
        data = ops_api.user_detail(user_id=user_id, db=db)
    except Exception:
        return RedirectResponse("/ops/users", status_code=302)
    all_devices = ops_api.list_devices(db=db)["devices"]
    return _page(request, "user_detail.html", ops_session,
                 data=data, all_devices=all_devices)


@router.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: Request, user_id: int | None = None,
                  ops_session: str = Cookie(default=""),
                  db: Session = Depends(get_db)):
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    return _page(request, "profiles.html", ops_session,
                 data=ops_api.list_profiles(user_id=user_id, db=db),
                 user_id=user_id)


@router.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, ops_session: str = Cookie(default=""),
                 db: Session = Depends(get_db)):
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    return _page(request, "devices.html", ops_session,
                 data=ops_api.list_devices(db=db))


@router.get("/sessions", response_class=HTMLResponse)
def sessions_page(request: Request, state: str | None = None,
                  count_match: bool | None = None, device: str | None = None,
                  ops_session: str = Cookie(default=""),
                  db: Session = Depends(get_db)):
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    data = ops_api.list_sessions(state=state, count_match=count_match,
                                 device=device, db=db)
    devices = ops_api.list_devices(db=db)["devices"]
    return _page(request, "sessions.html", ops_session, data=data,
                 devices=devices, f_state=state or "",
                 f_count_match="" if count_match is None else str(count_match).lower(),
                 f_device=device or "")


@router.get("/sessions/{sid}", response_class=HTMLResponse)
def report_page(request: Request, sid: uuid.UUID,
                ops_session: str = Cookie(default=""),
                db: Session = Depends(get_db)):
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    try:
        data = ops_api.session_report(sid=sid, db=db)
    except Exception:
        return _page(request, "report.html", ops_session, data=None, sid=str(sid))
    result = data["result"]
    total = result["timeline"][-1]["t1"] if result.get("timeline") else 0
    segs = [{"state": s["state"],
             "left": (s["t0"] / total * 100) if total else 0,
             "width": ((s["t1"] - s["t0"]) / total * 100) if total else 0}
            for s in (result.get("timeline") or [])]
    return _page(request, "report.html", ops_session, data=data,
                 sid=str(sid), segs=segs)


@router.get("/subscriptions", response_class=HTMLResponse)
def subscriptions_page(request: Request, country: str | None = None,
                       months: int | None = None,
                       ops_session: str = Cookie(default=""),
                       db: Session = Depends(get_db)):
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    return _page(request, "subscriptions.html", ops_session,
                 data=ops_api.list_subscriptions(country=country,
                                                 months=months, db=db))


@router.get("/versions", response_class=HTMLResponse)
def versions_page(request: Request, ops_session: str = Cookie(default=""),
                  db: Session = Depends(get_db)):
    """버전 이력 — Live Evolution 채택마다 0.0.0.1 증가 (2026-07-07)."""
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    return _page(request, "versions.html", ops_session,
                 data=ops_api.list_versions(db=db))


@router.get("/relax_voice", response_class=HTMLResponse)
def relax_voice_page(request: Request, ops_session: str = Cookie(default=""),
                     db: Session = Depends(get_db)):
    """수면세션 음성 배포 — active/draft/이력 (2026-07-07)."""
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    from app.api import relax_voice as rv_api
    return _page(request, "relax_voice.html", ops_session,
                 data=rv_api.list_releases(db=db),
                 long_protocol=rv_api.ops_long_protocol(db=db))


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, ops_session: str = Cookie(default=""),
               db: Session = Depends(get_db)):
    if not _logged_in(ops_session):
        return RedirectResponse("/ops/login", status_code=302)
    return _page(request, "audit.html", ops_session,
                 data=ops_api.audit_log(db=db))
