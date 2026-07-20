"""수면세션 음성 배포 — 저장/검증/배포시각 계산 (2026-07-07 결정).

앱의 수면-릴랙스 세션 단계별 대사를 '깨끗한 mp3'로 교체하는 배포 채널의 순수 로직.
스토리지는 세션 record 와 같은 로컬 볼륨(STORAGE_ROOT) 아래 relax_voice/ 서브트리 —
클라우드에서 Object Storage 로 바꿀 때 SessionStorage 와 같은 지점에서 함께 교체한다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 앱과 공유하는 고정 슬롯 계약 — 절대 임의 변경 금지 (앱이 이 키로 재생 단계를 분기).
# 수면 세션 2종: short(기존 4슬롯) / long(긴 버전 5분, 7슬롯 — 페이드아웃은 음성 없음).
RELAX_VOICE_PROTOCOLS: tuple[str, ...] = ("short", "long")

RELAX_VOICE_SLOTS_BY_PROTOCOL: dict[str, tuple[str, ...]] = {
    "short": ("close_eyes", "pmr", "breathing", "closing"),
    "long": ("close_eyes", "stretch_1", "breathing_1", "meditation_1",
             "stretch_2", "breathing_2", "meditation_2"),
}
RELAX_VOICE_SLOT_LABELS_BY_PROTOCOL: dict[str, dict[str, str]] = {
    "short": {
        "close_eyes": "눈 감기 안내",
        "pmr": "근이완",
        "breathing": "호흡",
        "closing": "마무리",
    },
    "long": {
        "close_eyes": "눈 감기 안내",
        "stretch_1": "가벼운 스트레칭 ①",
        "breathing_1": "호흡 ①",
        "meditation_1": "명상 리드 ①",
        "stretch_2": "가벼운 스트레칭 ②",
        "breathing_2": "호흡 ②",
        "meditation_2": "명상 리드 ②",
    },
}

# 하위호환 별칭 — 기존 참조 지점(short 기본).
RELAX_VOICE_SLOTS: tuple[str, ...] = RELAX_VOICE_SLOTS_BY_PROTOCOL["short"]
RELAX_VOICE_SLOT_LABELS: dict[str, str] = RELAX_VOICE_SLOT_LABELS_BY_PROTOCOL["short"]


def slots_for(protocol: str) -> tuple[str, ...]:
    return RELAX_VOICE_SLOTS_BY_PROTOCOL.get(protocol, ())


def labels_for(protocol: str) -> dict[str, str]:
    return RELAX_VOICE_SLOT_LABELS_BY_PROTOCOL.get(protocol, {})


MAX_VOICE_BYTES = 15 * 1024 * 1024        # mp3 슬롯 상한 15MB
KST = timezone(timedelta(hours=9))


def as_utc(dt: datetime | None) -> datetime | None:
    """naive(예: sqlite 반환) 는 UTC 로 간주해 tz-aware 로 통일한다."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def kst_date_to_release_at(release_date: str) -> datetime:
    """배포일(KST 'YYYY-MM-DD')의 그 날 0시(KST) = UTC 전날 15:00.

    반환은 tz-aware UTC — DB 에 그대로 저장한다. 형식 오류는 ValueError.
    """
    parts = release_date.split("-")
    if len(parts) != 3:
        raise ValueError("release_date must be YYYY-MM-DD")
    y, m, d = (int(x) for x in parts)
    kst_midnight = datetime(y, m, d, 0, 0, 0, tzinfo=KST)
    return kst_midnight.astimezone(timezone.utc)


def looks_like_mp3(head: bytes) -> bool:
    """파일 시그니처 검사 — ID3 태그 또는 MPEG 오디오 프레임 sync(0xFF 0xEx)."""
    if head[:3] == b"ID3":
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    return False


class RelaxVoiceStorage:
    """relax_voice/{release_id}/{slot}.mp3 — SessionStorage 와 같은 로컬 볼륨 패턴.

    root 는 SessionStorage.root 와 동일한 STORAGE_ROOT 를 공유한다(같은 Docker 볼륨,
    컨테이너 재시작에도 유지). 테스트는 get_storage 오버라이드로 tmp_path 를 흘려보낸다.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _dir(self, release_id: int) -> Path:
        return self.root / "relax_voice" / str(release_id)

    def rel_path(self, release_id: int, slot: str) -> str:
        return f"relax_voice/{release_id}/{slot}.mp3"

    def abs_path(self, rel: str) -> Path:
        return self.root / rel

    def save(self, release_id: int, slot: str, data: bytes) -> str:
        """mp3 바이트를 저장하고 스토리지 상대경로를 반환. 원자적 교체로 부분 파일 노출 없음."""
        d = self._dir(release_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{slot}.mp3"
        tmp = path.with_suffix(".mp3.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return self.rel_path(release_id, slot)


# ============================================================ 긴 버전 시퀀스(음악·길이)
#
# 긴 버전(5분)은 음악 선택 없이 서버가 정한 시퀀스로 진행된다. 이 시퀀스(단계 길이·음악)와
# 음성 대사 모두 운영콘솔에서 수정/배포할 수 있어야 한다(2026-07-07 요구). 서버가 정본이고
# 앱 내장 assets/enhance/relax_long_v1.json 은 오프라인 폴백이다. 편집기는 steps 의 seconds 와
# params.sound 만 바꾸고(호흡은 reps 로 환산) 단계 종류/순서/텍스트 등 다른 필드는 보존한다.

RELAX_LONG_PROTOCOL_KEY = "relax_long_v1"
CONTINUE_MUSIC = "이어서(변경 없음)"          # params.sound 제거 = 직전 음악 유지

# 음악명 → 앱 에셋 경로 (앱과 공유하는 고정 매핑).
RELAX_MUSIC_ASSETS: dict[str, str] = {
    "느린 파도": "assets/sounds/relax_waves.mp3",
    "여린 시냇물": "assets/sounds/relax_stream.mp3",
    "호흡 페이싱": "assets/sounds/relax_breath.mp3",
    "수면 배경 음악": "assets/sounds/relax_night.mp3",
}
RELAX_MUSIC_NAMES: dict[str, str] = {v: k for k, v in RELAX_MUSIC_ASSETS.items()}
RELAX_MUSIC_OPTIONS: list[str] = list(RELAX_MUSIC_ASSETS.keys()) + [CONTINUE_MUSIC]

# 기본(시드) 프로토콜 — 앱 내장 relax_long_v1.json 과 의미가 동일해야 한다. 정확히 이 문자열을
# json.loads 해 시드한다(부동소수/불리언/키 표기가 요구된 JSON 과 어긋나지 않도록 문자열로 보관).
DEFAULT_RELAX_LONG_PROTOCOL_JSON = r"""
{"id":"relax_long_v1","type":"relax","title":"수면-릴랙스 세션 (긴 버전)","_source":"2026-07-07 긴 버전 v1 — 시퀀스·음악은 운영콘솔 '수면세션'에서 수정, 이 JSON 이 정본이고 앱 에셋은 오프라인 폴백.","steps":[
{"kind":"message","seconds":10,"title":{"ko":"이제 눈을 감아요","en":"Now close your eyes","zh":"现在闭上眼睛"},"body":{"ko":"여기서부터는 목소리만 따라오면 돼요.","en":"From here, just follow the voice.","zh":"从这里开始，只要跟随声音就好。"},"voice":{"ko":"이제 눈을 감아요. 여기서부터는 제 목소리만 따라오면 돼요.","en":"Now close your eyes. From here, just follow my voice.","zh":"现在闭上眼睛。从这里开始，只要跟随我的声音就好。"},"params":{"sound":"assets/sounds/relax_waves.mp3","voiceSlot":"close_eyes"}},
{"kind":"stretch","seconds":30,"title":"","body":"","voice":{"ko":"천천히 어깨를 귀 쪽으로 올렸다가, 툭 내려놓아요. 목을 좌우로 부드럽게 기울여요.","en":"Slowly lift your shoulders toward your ears, then drop them. Gently tilt your neck side to side.","zh":"慢慢把肩膀朝耳朵抬起，然后放下。轻轻左右倾斜脖子。"},"params":{"eyesClosed":true,"voiceSlot":"stretch_1"}},
{"kind":"breathing","seconds":30,"title":"","body":"","voice":{"ko":"넷을 세며 들이쉬고, 여섯을 세며 길게 내쉬어요.","en":"Breathe in for four, and breathe out long for six.","zh":"数四拍吸气，数六拍慢慢呼气。"},"params":{"eyesClosed":true,"voiceSlot":"breathing_1","inhale":4,"exhale":6,"reps":3,"repsUnder9":2,"tone":"down","sound":"assets/sounds/relax_breath.mp3"}},
{"kind":"message","seconds":60,"title":"","body":"","voice":{"ko":"숨이 들어오고 나가는 것을 그냥 바라봐요. 몸이 조금씩 무거워져요.","en":"Just watch the breath coming in and going out. Your body grows heavier, little by little.","zh":"只是看着呼吸进出。身体一点点变得沉重。"},"params":{"eyesClosed":true,"voiceSlot":"meditation_1","sound":"assets/sounds/relax_night.mp3"}},
{"kind":"stretch","seconds":30,"title":"","body":"","voice":{"ko":"이번엔 두 손을 가볍게 쥐었다 펴고, 팔을 부드럽게 흔들어 풀어요.","en":"Now gently squeeze and open your hands, and softly shake out your arms.","zh":"现在轻轻握拳再张开，柔和地抖动手臂放松。"},"params":{"eyesClosed":true,"voiceSlot":"stretch_2","sound":"assets/sounds/relax_stream.mp3"}},
{"kind":"breathing","seconds":30,"title":"","body":"","voice":{"ko":"다시 숨이에요. 넷을 세며 들이쉬고, 여섯을 세며 내쉬어요.","en":"The breath again. In for four, out for six.","zh":"再次呼吸。数四拍吸气，数六拍呼气。"},"params":{"eyesClosed":true,"voiceSlot":"breathing_2","inhale":4,"exhale":6,"reps":3,"repsUnder9":2,"tone":"down","sound":"assets/sounds/relax_breath.mp3"}},
{"kind":"message","seconds":60,"title":"","body":"","voice":{"ko":"지금 이대로 충분해요. 편안함이 온몸으로 퍼져요. 음악이 잦아들면, 그대로 잠들면 돼요.","en":"You are enough, just as you are. Comfort spreads through your whole body. When the music fades, just fall asleep.","zh":"现在这样就很好。舒适感传遍全身。音乐消散后，就这样睡着吧。"},"params":{"eyesClosed":true,"voiceSlot":"meditation_2","sound":"assets/sounds/relax_night.mp3"}},
{"kind":"fadeout","seconds":50,"title":"","body":"","params":{"silentEnd":true}}]}
"""
DEFAULT_RELAX_LONG_PROTOCOL: dict = json.loads(DEFAULT_RELAX_LONG_PROTOCOL_JSON)


def _step_label(step: dict, index: int) -> str:
    params = step.get("params") or {}
    slot = params.get("voiceSlot")
    labels = labels_for("long")
    if slot and slot in labels:
        return labels[slot]
    if step.get("kind") == "fadeout":
        return "마무리 페이드아웃"
    return step.get("kind") or f"단계 {index + 1}"


def long_protocol_rows(config: dict) -> list[dict]:
    """콘솔 시퀀스 편집기용 행 — 단계명(고정)·현재 길이(초)·현재 음악명."""
    rows = []
    for i, step in enumerate(config.get("steps", [])):
        params = step.get("params") or {}
        sound = params.get("sound")
        rows.append({
            "index": i,
            "kind": step.get("kind"),
            "label": _step_label(step, i),
            "voice_slot": params.get("voiceSlot"),
            "seconds": step.get("seconds"),
            "music": RELAX_MUSIC_NAMES.get(sound, CONTINUE_MUSIC),
            "is_breathing": step.get("kind") == "breathing",
        })
    return rows


def apply_long_protocol_edits(config: dict, edits: list[dict]) -> dict:
    """단계 순서대로 [{seconds, music}] 를 적용한 새 config 반환.

    - 단계 종류/순서/텍스트 등 다른 필드는 전부 보존(deep copy 후 seconds·params.sound 만 수정).
    - music == CONTINUE_MUSIC(또는 빈 값) → params.sound 제거(직전 음악 유지).
    - breathing 단계는 실제 길이를 앱이 reps×(inhale+exhale)로 계산하므로, 입력 초를
      reps = round(초/(inhale+exhale))(최소 1)로 환산해 저장하고 seconds 도 그에 맞춰 스냅한다.
    """
    steps = config.get("steps", [])
    if len(edits) != len(steps):
        raise ValueError(f"edit count {len(edits)} != step count {len(steps)}")
    new_config = json.loads(json.dumps(config))
    for step, edit in zip(new_config["steps"], edits):
        params = step.setdefault("params", {})
        try:
            seconds = int(edit["seconds"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("seconds must be an integer")
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        if step.get("kind") == "breathing":
            per = int(params.get("inhale", 4)) + int(params.get("exhale", 6))
            reps = max(1, round(seconds / per)) if per > 0 else int(params.get("reps", 1))
            params["reps"] = reps
            step["seconds"] = reps * per
        else:
            step["seconds"] = seconds
        music = edit.get("music", CONTINUE_MUSIC)
        if music in (CONTINUE_MUSIC, None, ""):
            params.pop("sound", None)
        elif music in RELAX_MUSIC_ASSETS:
            params["sound"] = RELAX_MUSIC_ASSETS[music]
        else:
            raise ValueError(f"unknown music: {music}")
    return new_config
