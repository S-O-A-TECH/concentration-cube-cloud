"""holdout 배정 — 세션 id 해시 기반 70:30, 재현 가능 (SPEC-01 §2).

세션 finish 시 자동 배정, 배정 후 변경 금지, 라벨 유무와 무관.
같은 sid 는 언제 어디서 계산해도 같은 답 — 그래서 난수가 아니라 해시다.
"""
import hashlib
import uuid

TRAIN_PERCENT = 70


def split_for(session_id: str | uuid.UUID) -> str:
    digest = hashlib.sha256(str(session_id).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") % 100
    return "train" if bucket < TRAIN_PERCENT else "holdout"
