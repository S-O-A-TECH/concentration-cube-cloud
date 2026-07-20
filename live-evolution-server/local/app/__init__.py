"""live-evolution 서버 — 임상 검증 게이트형 자가진화 엔진의 조종석.

경계 (docs/plan/README.md):
- 원천 데이터 없음 — 세션·라벨·param_sets 는 전부 운영 서버 소유, 이 서버는 조회/요청만.
- 게이트 판정·승격 집행은 운영 서버의 일. 채택(promote)으로 가는 문은 사람의 버튼 하나.
- 자체 SQLite(state/state.sqlite)에는 에이전트 실행 이력·제안 원문·설정만 둔다.
"""
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT_DIR = PKG_DIR.parent            # live-evolution-server/local/
WEBUI_DIR = PKG_DIR / "webui"

APP_VERSION = "0.1.0"
