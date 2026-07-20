"""S1 DoD: holdout 배정 함수 — 재현성 + 1만 개 분포 68~72% (S1 구현계획 §1.5)."""
import uuid

from app.services.split import split_for


def test_deterministic_and_valid_values():
    sid = uuid.uuid4()
    first = split_for(sid)
    assert first in ("train", "holdout")
    assert all(split_for(sid) == first for _ in range(100))
    # str 로 넣어도 같은 답 (API 경계에서 타입이 섞여도 배정 불변)
    assert split_for(str(sid)) == first


def test_distribution_10k():
    n = 10_000
    train = sum(1 for i in range(n) if split_for(uuid.UUID(int=i)) == "train")
    assert 0.68 <= train / n <= 0.72, f"train 비율 {train / n:.3f}"
