"""MISSION.md 렌더 (SPEC-06 §4.1 고정 임무문).

임무문은 세대가 바뀌어도 뼈대가 고정이다 — 변하는 것은 증거(input/)와
재시도 피드백뿐. 렌더 결과 전문이 agent_runs 에 보존된다 (재현성).
"""
from __future__ import annotations

from jinja2 import Template

# 개인정보 차단 테스트(SPEC-04 §3)의 검사 대상 — 임무문·증거 어디에도 나오면 안 되는 필드
FORBIDDEN_IDENTIFIER_PATTERNS = (
    "student_name", "birth", "birthday", "phone", "email", "address",
    "이름:", "생년월일", "전화번호", "주소:",
)

_TEMPLATE = Template("""\
당신은 시선추적 집중 판정 엔진의 파라미터 튜닝 담당이다.
목표: input/confusion.json 의 실패 모드를 개선하는 파라미터 세트 1개를 제출하라.

## 작업장 구성
- input/            증거 7파일 (confusion, mistakes, feature_stats, current_params,
                    targets_bounds, gate_rules, history)
- data/train_lite.parquet   분류기 입력 컬럼 + truth 라벨 (train 세션만)
- tools/simulate.py         연습장 — 후보 params 로 train 성적(sens/spec)을 재계산
- output/proposal.json      제출물 (유일한 수거 대상)

## simulate 사용법 (반드시 이 인터프리터로)
    "{{ python_exe }}" tools/simulate.py <candidate_params.json>
→ stdout 에 {"per_state": {상태: {"sens":…, "spec":…}}, …} JSON 이 출력된다.
candidate_params.json 은 input/current_params.json 과 같은 구조의 전체 params 파일이다
(작업장 안에 자유롭게 만들어 시험하라).

## 절차 (반드시 이 순서)
1  진단 — confusion·mistakes·feature_stats 에서 주된 실패 모드와 원인 피처를 특정하라
2  가설 — input/targets_bounds.json 의 allowed_keys 중 무엇을 왜 움직일지 서술하라
3  시험 — tools/simulate.py 로 후보를 **최소 2회 이상** 자가 시험하라 (스윕 권장)
4  선택 — input/gate_rules.json 기준(다른 상태 -2%p 이내 저하 & 표적 개선)을
   train 에서 만족하는 최선 1개를 골라라
5  제출 — **output/proposal.json 파일로 저장하라** (아래 계약).
   self_test 에는 최종 simulate 수치를 그대로 기입하라 — 우리가 같은 도구로
   재계산해 대조한다. 불일치는 즉시 기각이다.

## 제약
- 변경 키 ≤ 8, targets_bounds 의 allowed_keys 밖 금지, bounds 밖 금지
- sfi_weights 를 바꾼다면 합이 정확히 100 이어야 한다
- input/history.json 의 과거 기각(특히 train 좋고 holdout 나빴던 과적합)을 반복하지 마라
- train 에 과적합하지 마라 — 진짜 시험은 네가 못 보는 holdout 이다. 단순한 변경이 강하다.
- 네트워크 접근 금지·불필요. 이 작업장 폴더 밖을 읽거나 쓰지 마라.

## output/proposal.json 계약 (proposal.v1)
```json
{
  "schema": "proposal.v1", "level": 1,
  "diagnosis": "실패 모드 서술 (데이터 근거 인용)",
  "new_params": { "...current_params.json 과 동일 키셋의 전체 params..." },
  "changes": [{"key": "blank_stare.stare_dispersion_th", "from": 0.035, "to": 0.05,
               "reason": "feature_stats 근거"}],
  "self_test": {
    "n_simulate_runs": 2,
    "train_before": {"focus": {"sens": 0.0, "spec": 0.0},
                     "off_task": {"sens": 0.0, "spec": 0.0},
                     "blank_stare": {"sens": 0.0, "spec": 0.0}},
    "train_after":  {"focus": {"sens": 0.0, "spec": 0.0},
                     "off_task": {"sens": 0.0, "spec": 0.0},
                     "blank_stare": {"sens": 0.0, "spec": 0.0}}
  },
  "risk": "예상 부작용과 holdout 격차 가능성",
  "rationale": "3~5문장 요약"
}
```
다시 강조: 제출물은 **output/proposal.json 파일**이다. stdout 요약은 참고용일 뿐,
파일이 없으면 이 세대는 실패 처리된다.
{% if feedback %}
## 직전 시도 실패 피드백 (이번 시도에서 반드시 해소할 것)
{{ feedback }}
{% endif %}
""")


def render_mission(python_exe: str, feedback: str = "") -> str:
    return _TEMPLATE.render(python_exe=python_exe, feedback=feedback.strip())
