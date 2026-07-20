"""코칭 문구 템플릿 (rule-based — LLM 없음).

가드레일을 템플릿 설계로 원천 차단: 숫자는 result 의 사실 수치만 삽입,
진단 어휘·비난 어조 금지 (phase2 Layer 3 가드레일의 축소판).
어투는 미확정(반말/존대 논의 중) — 이 파일만 교체하면 되도록 분리해 둠.

다국어(2026-07-16): 앱이 ko/en/zh 셋 중 하나를 고를 수 있어야 하므로, 언어별
전용 함수로 나누고 `student_text`/`parent_text`/`unscorable_text` 는 `lang` 을 보고
디스패치만 한다. report.py 는 세 언어를 모두 생성해 result.coach_text 에 저장하고,
실제 노출 언어 선택은 API 레이어(build_session_report)가 담당한다 — 이 파일 자체는
여전히 순수 함수(같은 입력 → 같은 출력)다.
"""
from __future__ import annotations


def _student_ko(sfi: int, focused_min: float, streak_min: float,
                n_off: int, mean_return: float | None, n_blank: int) -> str:
    parts = []
    if streak_min >= 15:
        parts.append(f"한 번에 {streak_min:.0f}분을 이어서 집중했어요. 아주 단단한 집중이에요.")
    elif streak_min >= 7:
        parts.append(f"가장 길게 이어진 집중은 {streak_min:.0f}분이었어요. 좋은 흐름이에요.")
    elif streak_min >= 1:
        parts.append(f"이번엔 짧은 집중이 여러 번 있었어요. 가장 길게는 {streak_min:.0f}분이었어요.")
    else:
        parts.append("이번엔 집중이 자리 잡기 전에 끝났어요. 다음 도전에서 다시 해봐요.")
    if n_off == 0:
        parts.append("자리를 벗어난 시선이 거의 없었어요.")
    elif mean_return is not None and mean_return <= 10:
        parts.append(f"다른 곳을 봐도 평균 {mean_return:.0f}초 만에 돌아왔어요. 돌아오는 힘이 좋아요.")
    else:
        parts.append("시선이 떠났을 때 돌아오기까지 시간이 조금 걸렸어요. 다음엔 더 빨라질 거예요.")
    if n_blank > 0:
        parts.append("책을 보면서도 잠시 생각이 멈춘 구간이 있었어요. 그럴 땐 한 줄 소리 내어 읽으면 돌아오기 쉬워요.")
    return " ".join(parts)


def _student_en(sfi: int, focused_min: float, streak_min: float,
                n_off: int, mean_return: float | None, n_blank: int) -> str:
    parts = []
    if streak_min >= 15:
        parts.append(f"You stayed focused for {streak_min:.0f} minutes straight — that's a really solid streak.")
    elif streak_min >= 7:
        parts.append(f"Your longest focus streak was {streak_min:.0f} minutes. Nice flow.")
    elif streak_min >= 1:
        parts.append(f"You had a few short bursts of focus this time. The longest was {streak_min:.0f} minutes.")
    else:
        parts.append("Focus didn't quite settle in this time. Let's try again next challenge.")
    if n_off == 0:
        parts.append("Your eyes barely left the page.")
    elif mean_return is not None and mean_return <= 10:
        parts.append(f"Even when you looked away, you came back in about {mean_return:.0f} seconds on average — great recovery.")
    else:
        parts.append("It took a little while to come back after looking away. You'll get faster next time.")
    if n_blank > 0:
        parts.append("There were moments your eyes stayed on the book but your reading rhythm paused. Reading a line out loud can help you snap back in.")
    return " ".join(parts)


def _student_zh(sfi: int, focused_min: float, streak_min: float,
                n_off: int, mean_return: float | None, n_blank: int) -> str:
    parts = []
    if streak_min >= 15:
        parts.append(f"一次性连续专注了{streak_min:.0f}分钟，非常稳定的专注！")
    elif streak_min >= 7:
        parts.append(f"这次最长的连续专注是{streak_min:.0f}分钟，节奏很不错。")
    elif streak_min >= 1:
        parts.append(f"这次有几次较短的专注，其中最长的是{streak_min:.0f}分钟。")
    else:
        parts.append("这次专注还没稳定下来就结束了，下次挑战再试试吧。")
    if n_off == 0:
        parts.append("视线几乎没有离开过书本。")
    elif mean_return is not None and mean_return <= 10:
        parts.append(f"即使看向别处，平均也只用了{mean_return:.0f}秒就回来了，回归的能力很棒。")
    else:
        parts.append("视线移开后花了一些时间才回来，下次会更快的。")
    if n_blank > 0:
        parts.append("虽然视线在书本上，但有几段阅读节奏停了下来。这时候小声读一行字会更容易回到状态。")
    return " ".join(parts)


def _parent_ko(sfi: int, focused_min: float, streak_min: float,
              n_off: int, n_blank: int, coverage: float) -> str:
    parts = [f"이번 세션에서 실제 집중 유지 시간은 약 {focused_min:.0f}분, "
             f"가장 길게 이어진 집중은 {streak_min:.0f}분이었습니다."]
    if n_off:
        parts.append(f"자리 이탈성 시선 전환은 {n_off}회 관찰되었습니다.")
    if n_blank:
        parts.append("시선은 책에 있으나 읽기 리듬이 멈춘 구간이 관찰되었습니다. "
                     "피로하거나 내용이 어려울 때 나타나는 자연스러운 패턴입니다.")
    if coverage < 0.85:
        parts.append("일부 구간은 측정 품질이 낮아 해석에서 제외되었습니다.")
    parts.append("점수보다 유지 시간의 추세를 함께 봐 주세요.")
    return " ".join(parts)


def _parent_en(sfi: int, focused_min: float, streak_min: float,
              n_off: int, n_blank: int, coverage: float) -> str:
    parts = [f"In this session, actual sustained focus time was about {focused_min:.0f} minutes, "
             f"with the longest streak lasting {streak_min:.0f} minutes."]
    if n_off:
        parts.append(f"{n_off} off-task gaze shifts were observed.")
    if n_blank:
        parts.append("There were moments the gaze stayed on the book but the reading rhythm paused. "
                     "This is a natural pattern that can occur with fatigue or difficult material.")
    if coverage < 0.85:
        parts.append("Some segments had lower measurement quality and were excluded from interpretation.")
    parts.append("Please look at the trend in sustained focus time together with the score, rather than the score alone.")
    return " ".join(parts)


def _parent_zh(sfi: int, focused_min: float, streak_min: float,
              n_off: int, n_blank: int, coverage: float) -> str:
    parts = [f"本次学习中，实际保持专注的时间约为{focused_min:.0f}分钟，"
             f"最长连续专注为{streak_min:.0f}分钟。"]
    if n_off:
        parts.append(f"观察到{n_off}次离开位置的视线转移。")
    if n_blank:
        parts.append("观察到视线停留在书本上但阅读节奏中断的情况，这是疲劳或内容较难时常见的自然现象。")
    if coverage < 0.85:
        parts.append("部分时段测量质量较低，已从解读中排除。")
    parts.append("请更多关注保持专注时间的趋势，而不只是分数本身。")
    return " ".join(parts)


_STUDENT_BY_LANG = {"ko": _student_ko, "en": _student_en, "zh": _student_zh}
_PARENT_BY_LANG = {"ko": _parent_ko, "en": _parent_en, "zh": _parent_zh}
_UNSCORABLE_BY_LANG = {
    "ko": "이번 세션은 측정이 충분하지 않았어요. 카메라가 얼굴을 잘 볼 수 있는지, "
          "책과 카메라 사이 거리(30~60cm)를 확인하고 다시 도전해 보세요.",
    "en": "This session didn't have enough measurement data. Check that the camera can "
          "clearly see your face and that the book-to-camera distance is 30-60cm, then try again.",
    "zh": "这次学习的测量数据不够充分。请检查摄像头是否能清楚看到脸部，"
          "以及书本与摄像头之间的距离（30~60厘米），然后再试一次。",
}


def student_text(sfi: int, focused_min: float, streak_min: float,
                 n_off: int, mean_return: float | None, n_blank: int,
                 lang: str = "ko") -> str:
    fn = _STUDENT_BY_LANG.get(lang, _student_ko)
    return fn(sfi, focused_min, streak_min, n_off, mean_return, n_blank)


def parent_text(sfi: int, focused_min: float, streak_min: float,
                n_off: int, n_blank: int, coverage: float,
                lang: str = "ko") -> str:
    fn = _PARENT_BY_LANG.get(lang, _parent_ko)
    return fn(sfi, focused_min, streak_min, n_off, n_blank, coverage)


def unscorable_text(lang: str = "ko") -> str:
    return _UNSCORABLE_BY_LANG.get(lang, _UNSCORABLE_BY_LANG["ko"])
