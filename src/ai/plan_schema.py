# EditPlan / SceneEval / VideoPlan の形と既定 (docs/AiEditor/resolve1.md §7.2 / §8.1 / §9 / R6)
#
# ここは **AI と既存の編集エンジンの契約**である。AI の応答はここを通ってからでないと
# plan_validator / plan_applier へ入れない。
#
# 時間座標の規約 (§9.2。最重要):
#   ・scenes[].source_start / source_end   元 VOD の秒
#   ・subtitles[].source_start / source_end 元 VOD の秒
#   ・sounds[].offset_sec                  そのシーンの先頭からの相対秒
#   AI は Timeline 時間を一切知らない。素材時間・Timeline 時間への変換は plan_applier が行う。
#
# 列挙値は既存の実装と同じ値を持つ (timeline.model / modules.subtitle_animation)。
# ai/ は Qt 非依存であり、settings_window (PySide6 を引く) も timeline も import しない。
# そのため値は**写し**として持ち、食い違っていないことを tests/test_ai_plan_schema.py が見る。
import json
import os

from ..exceptions import AiPlanError
from ..i18n import tr
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# EditPlan の書式の版 (§9.5)。未知の版は読まない。
PLAN_VERSION = 1

# 出力先 (§9.4)
TARGET_YOUTUBE_LONG = "youtube_long"
TARGET_YOUTUBE_SHORTS = "youtube_shorts"
TARGET_TIKTOK = "tiktok"
TARGETS = (TARGET_YOUTUBE_LONG, TARGET_YOUTUBE_SHORTS, TARGET_TIKTOK)

# 縦になる出力 (キャンバスの向きの既定を決める)
VERTICAL_TARGETS = (TARGET_YOUTUBE_SHORTS, TARGET_TIKTOK)

# シーンの役割 (§9.4)。長尺と短尺で使う値が違うが、検証では両方を許す
# (オープニングだけ長尺の語を使う構成なども通したいため)。
SCENE_ROLES = ("opening", "intro", "body", "climax", "ending",
               "hook", "context", "punchline")
DEFAULT_SCENE_ROLE = "body"

# ターゲットごとの既定の並び (§8.1)。planner が指示として渡し、検証では強制しない。
SECTION_ROLE_ORDER = {
    TARGET_YOUTUBE_LONG: ("opening", "intro", "body", "climax", "ending"),
    TARGET_YOUTUBE_SHORTS: ("hook", "context", "body", "punchline"),
    TARGET_TIKTOK: ("hook", "context", "body", "punchline"),
}

# テロップの役割 (§9.4)。線の上は own / sub / comment で、
# Timeline のモデルでは "streamer" / "sub" / "comment" である (model.DEFAULT_ROLE = "streamer")。
# 既定の名前を AI に書かせると揺れるため、plan 側は own に固定して写す。
TRACK_ROLE_OWN = "own"
TRACK_ROLE_SUB = "sub"
TRACK_ROLE_COMMENT = "comment"
TRACK_ROLES = (TRACK_ROLE_OWN, TRACK_ROLE_SUB, TRACK_ROLE_COMMENT)
DEFAULT_TRACK_ROLE = TRACK_ROLE_OWN
_TRACK_ROLE_TO_MODEL = {
    TRACK_ROLE_OWN: "streamer",
    TRACK_ROLE_SUB: "sub",
    TRACK_ROLE_COMMENT: "comment",
}

# テロップの動き (timeline.model.SUBTITLE_ANIMATIONS と同じ 12 種)。
# 廃止値・未知の値は "" (動かさない) へ落とす (§9.4 / design.md §5.4)。
ANIM_NONE = ""
ANIMATIONS = (
    "shake", "glow_in", "tsukkomi", "glow_blur", "shock1", "shock2",
    "shine", "poyon", "dive_tsukkomi", "double_jump",
    "black_blur_zoom_in", "black_blur_zoom_out",
)

# テロップ 1 本の行の揃え (timeline.model.SUBTITLE_ALIGNS)。"" = 設定に従う。
ALIGNS = ("left", "center", "right")

# 切り抜きの寄せ方 (§9.4)。face は blur の検出が有効なときだけ使える。
CROP_MODE_CENTER = "center"
CROP_MODES = (CROP_MODE_CENTER, "left", "right", "face")
DEFAULT_CROP_MODE = CROP_MODE_CENTER
# 切り抜きの拡大率の上限 (vertical.crop.max_scale の既定と同じ)。
CROP_SCALE_MIN = 1.0
CROP_SCALE_MAX = 2.0

# 素材の由来
SOURCE_KINDS = ("vod", "local")

# キャンバスの向き (modules.output_profile と同じ語)
ORIENTATION_LANDSCAPE = "landscape"
ORIENTATION_PORTRAIT = "portrait"
ORIENTATIONS = (ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT)

# ベースクリップの拡大 (commands.ZoomBaseClip の min_zoom / max_zoom)
ZOOM_MIN = 0.1
ZOOM_MAX = 3.0

# シーン評価の軸 (§7.2)。単一の「面白さ」を聞かない。
SCORE_KEYS = ("funny", "surprise", "context_dependency", "first_time_clarity",
              "excitement", "shorts_fit", "long_fit", "standalone")
SCORE_MIN = 0
SCORE_MAX = 100
RECOMMENDED_USES = ("shorts", "long_hook", "long_body", "long_climax", "skip")

# EditPlan を保存するときの拡張子 (§9.5)。プロジェクトの隣へ置く。
PLAN_SUFFIX = ".plan.json"
# シーン評価の保存先 (§7.2 末尾。再実行で呼び出しを減らす)
EVAL_SUFFIX = ".ai.json"
# AI 版のスナップショット (<project>.ai-base.json / §14.1)。
# 適用直後に 1 回だけ取る。人間の修正との差分の基準になる。
AI_BASE_SUFFIX = ".ai-base.json"


# ---- 検証の記録 ----------------------------------------------------------
#
# 「落とす・丸める」を黙って行わない (§10 の原則)。1 件 1 行で残し、
# 画面には件数だけを出す (§18.2)。本文 (テロップの文言など) は入れない。

# 記録の種類
ACTION_DEFAULT = "default"    # 既定へ丸めた
ACTION_CLAMP = "clamp"        # 範囲へ寄せた
ACTION_CUT = "cut"            # はみ出しを切った
ACTION_DROP = "drop"          # その要素を落とした
ACTION_SHIFT = "shift"        # 時刻をずらした
ACTION_WRAP = "wrap"          # 折り返した / 詰めた
ACTION_REFETCH = "refetch"    # 素材を取り直した


# 記録 1 件。where は "scenes[2].role" のような位置、detail は数値や列挙値だけ。
def note(check, where, action, detail=""):
    return {"check": str(check), "where": str(where),
            "action": str(action), "detail": str(detail)}


# 記録を 1 行のログ用文字列へ (本文は含まない)。
def format_notes(notes):
    return " / ".join(
        "{0} {1} {2}{3}".format(n["check"], n["where"], n["action"],
                                "=" + n["detail"] if n["detail"] else "")
        for n in notes or [])


# 落とした件数 (画面に出すのはこれだけ / §10 末尾)。
def dropped_count(notes):
    return sum(1 for n in notes or [] if n["action"] == ACTION_DROP)


# ---- EditPlan ------------------------------------------------------------

# 空の EditPlan を既定値つきで作る。手書きの plan を組むときの土台でもある (§21 の 4 番)。
def new_plan(target, source=None, duration_sec=0.0, orientation="",
             model="", generated_at="", provider="claude"):
    chosen = target if target in TARGETS else TARGET_YOUTUBE_LONG

    return {
        "plan_version": PLAN_VERSION,
        "generated": {"provider": str(provider or ""), "model": str(model or ""),
                      "at": str(generated_at or "")},
        "project": {
            "target": chosen,
            "source": _source_of(source),
            "canvas": {"orientation": orientation if orientation in ORIENTATIONS
                       else default_orientation(chosen)},
            "duration_sec": _round_sec(duration_sec),
        },
        "scenes": [],
        "subtitles": [],
        "sounds": [],
        "editing": {"subtitle": True, "vertical_crop": chosen in VERTICAL_TARGETS,
                    "sound_effects": True, "watermark": None},
        "notes": "",
    }


# ターゲットから既定のキャンバスの向きを決める (解決値は保存しない / §16)。
def default_orientation(target):
    return ORIENTATION_PORTRAIT if target in VERTICAL_TARGETS else ORIENTATION_LANDSCAPE


# Timeline のモデルが使う役割名へ写す (own → streamer)。
def model_track_role(value):
    return _TRACK_ROLE_TO_MODEL.get(_track_role(value), _TRACK_ROLE_TO_MODEL[DEFAULT_TRACK_ROLE])


# コメント役割か (動きを付けない対象 / §9.4)。
def is_comment_role(value):
    return _track_role(value) == TRACK_ROLE_COMMENT


# EditPlan の置き場所 (<project>.plan.json / §9.5)。
def plan_path_for(project_path):
    base = os.path.splitext(str(project_path or ""))[0]

    return base + PLAN_SUFFIX if base else ""


# シーン評価の置き場所 (<project>.ai.json / §7.2)。
def eval_path_for(project_path):
    base = os.path.splitext(str(project_path or ""))[0]

    return base + EVAL_SUFFIX if base else ""


# AI 版のスナップショットの置き場 (<project>.ai-base.json / §14.1)。
def base_path_for(project_path):
    base = os.path.splitext(str(project_path or ""))[0]

    return base + AI_BASE_SUFFIX if base else ""


# EditPlan を保存する。AI が何を意図したかの原本であり、必ず残す (§9.5 / §14)。
def save(plan, path):
    target = str(path or "")
    if not target:
        raise AiPlanError(tr("EditPlan の保存先が指定されていません。"))
    folder = os.path.dirname(target)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, target)

    return target


# EditPlan を読む。未知の版は読まない (§9.5)。
def load(path):
    try:
        with open(str(path or ""), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as e:
        raise AiPlanError(tr("EditPlan を読めませんでした。")) from e
    if not isinstance(data, dict):
        raise AiPlanError(tr("EditPlan の形が正しくありません。"))
    # 未知の版は読まない (§9.5)。新しい版の plan を古いクライアントが解釈しないため。
    _require_version(data.get("plan_version"))

    return data


# ---- 形の検証 (§10.1 V1〜V5) ---------------------------------------------
#
# V1 必須キーの有無・型              → AiPlanError (致命)
# V2 plan_version が既知か           → AiPlanError
# V3 列挙値 (§9.4)                   → 既定へ丸める
# V4 文字列長                        → plan_validator (設定の上限を見るため)
# V5 数値の範囲                      → clamp
#
# 素材との整合 (V6〜V15) は plan_validator が見る。ここは「読める形か」だけを見る。

# 音量の範囲の既定 (settings の timeline.sound_tracks.gain_min_db / gain_max_db と同じ値)。
# ai/ は settings_window を import しないため写しを持つ。実値は呼び出し側が渡す。
GAIN_MIN_DB = -40.0
GAIN_MAX_DB = 12.0


# EditPlan を正規化する。返り値は (正規化した plan, 記録).
#
# target を渡した場合はそれを正とする (呼び出し側が「どの出力向けに頼んだか」を知っている。
# モデルが別の target を書いても採用しない)。
def normalize(plan, target=None, gain_limits=None):
    if not isinstance(plan, dict):
        raise AiPlanError(tr("EditPlan の形が正しくありません。"))
    _require_version(plan.get("plan_version"))

    notes = []
    low, high = _gain_limits(gain_limits)
    project = plan.get("project")
    if not isinstance(project, dict):
        raise AiPlanError(tr("EditPlan に project がありません。"))

    chosen = _resolve_target(project.get("target"), target, notes)
    raw_scenes = plan.get("scenes")
    if not isinstance(raw_scenes, list):
        raise AiPlanError(tr("EditPlan に scenes がありません。"))

    scenes = []
    for index, raw in enumerate(raw_scenes):
        scene = _normalize_scene(raw, index, notes, low, high)
        if scene is not None:
            scenes.append(scene)
    if not scenes:
        raise AiPlanError(tr("EditPlan に使えるシーンがありません。"))
    scenes = _unique_ids(_ordered(scenes), notes)
    known_ids = {scene["id"] for scene in scenes}

    subtitles = []
    for index, raw in enumerate(plan.get("subtitles") or []):
        item = _normalize_subtitle(raw, index, known_ids, notes)
        if item is not None:
            subtitles.append(item)

    sounds = []
    for index, raw in enumerate(plan.get("sounds") or []):
        item = _normalize_sound(raw, index, known_ids, notes, low, high)
        if item is not None:
            sounds.append(item)

    normalized = {
        "plan_version": PLAN_VERSION,
        "generated": _generated_of(plan.get("generated")),
        "project": {
            "target": chosen,
            "source": _source_of(project.get("source")),
            "canvas": _canvas_of(project.get("canvas"), chosen, notes),
            "duration_sec": max(_round_sec(project.get("duration_sec")), 0.0),
        },
        "scenes": scenes,
        "subtitles": subtitles,
        "sounds": sounds,
        "editing": _editing_of(plan.get("editing"), chosen),
        "notes": _text(plan.get("notes")),
    }

    return normalized, notes


def _require_version(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise AiPlanError(tr("EditPlan の plan_version がありません。"))
    if value != PLAN_VERSION:
        # 未知の版は読まずに落とす (project_io と同じ作法 / §9.5)。
        raise AiPlanError(
            tr("EditPlan の版 {version} は読めません。").format(version=value))


def _resolve_target(value, requested, notes):
    if requested in TARGETS:
        if value != requested:
            notes.append(note("V3", "project.target", ACTION_DEFAULT, requested))

        return requested
    if value in TARGETS:
        return value

    raise AiPlanError(tr("EditPlan の target が不正です。"))


def _normalize_scene(raw, index, notes, gain_low, gain_high):
    where = "scenes[{0}]".format(index)
    if not isinstance(raw, dict):
        notes.append(note("V1", where, ACTION_DROP, "type"))

        return None
    start = _number(raw.get("source_start"))
    end = _number(raw.get("source_end"))
    if start is None or end is None:
        notes.append(note("V1", where, ACTION_DROP, "source_start/source_end"))

        return None

    role = raw.get("role")
    if role not in SCENE_ROLES:
        # 書かれていなかっただけなら記録しない (既定の採用は「直し」ではない)。
        if _text(role):
            notes.append(note("V3", where + ".role", ACTION_DEFAULT, DEFAULT_SCENE_ROLE))
        role = DEFAULT_SCENE_ROLE

    gain = _clamped(raw.get("gain_db"), gain_low, gain_high, 0.0,
                    notes, "V5", where + ".gain_db")

    return {
        "id": _scene_id(raw.get("id"), index),
        "source_start": _round_sec(start),
        "source_end": _round_sec(end),
        "role": role,
        "order": _order_of(raw.get("order"), index),
        "reason": _text(raw.get("reason")),
        "crop": _crop_of(raw.get("crop"), notes, where + ".crop"),
        "zoom": _zoom_of(raw.get("zoom"), notes, where + ".zoom"),
        "gain_db": gain,
    }


def _normalize_subtitle(raw, index, known_ids, notes):
    where = "subtitles[{0}]".format(index)
    if not isinstance(raw, dict):
        notes.append(note("V1", where, ACTION_DROP, "type"))

        return None
    start = _number(raw.get("source_start"))
    end = _number(raw.get("source_end"))
    text = _text(raw.get("text"))
    if start is None or end is None or not text.strip():
        notes.append(note("V1", where, ACTION_DROP, "source_start/source_end/text"))

        return None

    scene_id = _text(raw.get("scene_id"))
    if scene_id not in known_ids:
        # 属するシーンが無いテロップは置き場所が決まらない (V11 の前段)。
        notes.append(note("V11", where + ".scene_id", ACTION_DROP, "unknown"))

        return None

    role = raw.get("track_role")
    if role not in TRACK_ROLES:
        if _text(role):
            notes.append(note("V3", where + ".track_role", ACTION_DEFAULT,
                              DEFAULT_TRACK_ROLE))
        role = DEFAULT_TRACK_ROLE

    animation = _animation_of(raw.get("animation"), role, notes, where + ".animation")
    align = raw.get("align")
    if align not in ALIGNS:
        if _text(align):
            notes.append(note("V3", where + ".align", ACTION_DEFAULT, ""))
        align = ""

    return {
        "scene_id": scene_id,
        "source_start": _round_sec(start),
        "source_end": _round_sec(end),
        "text": text,
        "track_role": role,
        "animation": animation,
        "fade_in": bool(raw.get("fade_in")),
        "fade_out": bool(raw.get("fade_out")),
        "color": _color_of(raw.get("color"), notes, where + ".color"),
        "align": align,
        "reason": _text(raw.get("reason")),
    }


def _normalize_sound(raw, index, known_ids, notes, gain_low, gain_high):
    where = "sounds[{0}]".format(index)
    if not isinstance(raw, dict):
        notes.append(note("V1", where, ACTION_DROP, "type"))

        return None
    preset = _text(raw.get("preset")).strip()
    offset = _number(raw.get("offset_sec"))
    if not preset or offset is None:
        notes.append(note("V1", where, ACTION_DROP, "preset/offset_sec"))

        return None
    scene_id = _text(raw.get("scene_id"))
    if scene_id not in known_ids:
        notes.append(note("V1", where + ".scene_id", ACTION_DROP, "unknown"))

        return None
    if os.path.sep in preset or "/" in preset or os.path.isabs(preset):
        # プリセット名だけを許す。パスを書かせない (§9.4 末尾)。
        notes.append(note("V3", where + ".preset", ACTION_DROP, "path"))

        return None

    return {
        "scene_id": scene_id,
        "offset_sec": max(_round_sec(offset), 0.0),
        "preset": preset,
        "gain_db": _clamped(raw.get("gain_db"), gain_low, gain_high, 0.0,
                            notes, "V5", where + ".gain_db"),
        "reason": _text(raw.get("reason")),
    }


# ---- 小さな変換 ----------------------------------------------------------

def _source_of(value):
    source = value if isinstance(value, dict) else {}
    kind = source.get("kind")

    return {
        "kind": kind if kind in SOURCE_KINDS else SOURCE_KINDS[0],
        "video_id": _text(source.get("video_id")),
        "path": _text(source.get("path")),
    }


def _canvas_of(value, target, notes):
    canvas = value if isinstance(value, dict) else {}
    orientation = canvas.get("orientation")
    if orientation not in ORIENTATIONS:
        orientation = default_orientation(target)
        if _text(canvas.get("orientation")):
            notes.append(note("V3", "project.canvas.orientation",
                              ACTION_DEFAULT, orientation))

    return {"orientation": orientation}


def _editing_of(value, target):
    editing = value if isinstance(value, dict) else {}
    watermark = editing.get("watermark")

    return {
        "subtitle": _flag(editing.get("subtitle"), True),
        "vertical_crop": _flag(editing.get("vertical_crop"), target in VERTICAL_TARGETS),
        "sound_effects": _flag(editing.get("sound_effects"), True),
        # 透かしは AI に決めさせない。None = 既存の設定のまま (§19)。
        "watermark": None if watermark is None else bool(watermark),
    }


def _generated_of(value):
    generated = value if isinstance(value, dict) else {}

    return {
        "provider": _text(generated.get("provider")),
        "model": _text(generated.get("model")),
        "at": _text(generated.get("at")),
    }


def _scene_id(value, index):
    text = _text(value).strip()

    return text if text else "sc{0}".format(index + 1)


def _order_of(value, index):
    number = _number(value)

    return int(number) if number is not None else index + 1


# order のとおりに並べる。同じ order は書かれた順を保つ (安定ソート)。
def _ordered(scenes):
    ordered = sorted(enumerate(scenes), key=lambda pair: (pair[1]["order"], pair[0]))
    result = []
    for position, (_, scene) in enumerate(ordered):
        scene["order"] = position + 1
        result.append(scene)

    return result


# シーンの id は後続 (subtitles / sounds の scene_id) の宛先になる。
# 重なると宛先が決まらないため、2 つ目以降を付け替える。
def _unique_ids(scenes, notes):
    seen = set()
    for index, scene in enumerate(scenes):
        if scene["id"] in seen:
            renamed = "sc{0}_{1}".format(index + 1, len(seen))
            notes.append(note("V1", "scenes[{0}].id".format(index),
                              ACTION_DEFAULT, "duplicate"))
            scene["id"] = renamed
        seen.add(scene["id"])

    return scenes


def _crop_of(value, notes, where):
    crop = value if isinstance(value, dict) else {}
    mode = crop.get("mode")
    if mode not in CROP_MODES:
        if _text(mode):
            notes.append(note("V3", where + ".mode", ACTION_DEFAULT, DEFAULT_CROP_MODE))
        mode = DEFAULT_CROP_MODE

    return {
        "mode": mode,
        "scale": _clamped(crop.get("scale"), CROP_SCALE_MIN, CROP_SCALE_MAX, 1.0,
                          notes, "V5", where + ".scale"),
    }


def _zoom_of(value, notes, where):
    if not isinstance(value, dict):
        return None
    scale = _number(value.get("scale"))
    if scale is None:
        return None

    return {
        "scale": _clamped(scale, ZOOM_MIN, ZOOM_MAX, 1.0, notes, "V5", where + ".scale"),
        # x / y はキャンバス中心を原点とする正規化座標 (model.Transform と同じ)。
        "x": _clamped(value.get("x"), -1.0, 1.0, 0.0, notes, "V5", where + ".x"),
        "y": _clamped(value.get("y"), -1.0, 1.0, 0.0, notes, "V5", where + ".y"),
    }


def _animation_of(value, track_role, notes, where):
    text = _text(value).strip()
    if text and text not in ANIMATIONS:
        # 廃止値・未知の値は「動かさない」へ (design.md §5.4)。
        notes.append(note("V3", where, ACTION_DEFAULT, ANIM_NONE or "none"))

        return ANIM_NONE
    if text and track_role == TRACK_ROLE_COMMENT:
        # コメント役割はフェードしか効かない (背景の箱とアイコンが追従できない / §9.4)。
        notes.append(note("V3", where, ACTION_DEFAULT, "comment"))

        return ANIM_NONE

    return text


def _color_of(value, notes, where):
    text = _text(value).strip()
    if not text:
        return ""
    if len(text) == 7 and text.startswith("#") and _is_hex(text[1:]):
        return text.upper()
    notes.append(note("V3", where, ACTION_DEFAULT, "format"))

    return ""


def _is_hex(text):
    return all(ch in "0123456789abcdefABCDEF" for ch in text)


def _track_role(value):
    return value if value in TRACK_ROLES else DEFAULT_TRACK_ROLE


def _flag(value, default):
    return bool(value) if isinstance(value, bool) else bool(default)


def _text(value):
    return value if isinstance(value, str) else ""


# 数値だけを受ける (真偽値・文字列は受けない。AI に "12:34" を書かせない / §5.4)。
def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return float(value)


def _round_sec(value):
    number = _number(value)

    return round(number, 3) if number is not None else 0.0


def _clamped(value, low, high, default, notes, check, where):
    number = _number(value)
    if number is None:
        return float(default)
    if number < low:
        notes.append(note(check, where, ACTION_CLAMP, "{0:g}".format(low)))

        return float(low)
    if number > high:
        notes.append(note(check, where, ACTION_CLAMP, "{0:g}".format(high)))

        return float(high)

    return round(number, 3)


def _gain_limits(limits):
    if isinstance(limits, (tuple, list)) and len(limits) == 2:
        low = _number(limits[0])
        high = _number(limits[1])
        if low is not None and high is not None and low < high:
            return low, high

    return GAIN_MIN_DB, GAIN_MAX_DB


# ---- SceneEval (§7.2) ----------------------------------------------------

# 軸が欠けたときの既定。0 にすると 1 軸の欠落で面白い場面が沈むため中立を置く。
DEFAULT_SCORE = 50
# 要約・理由の既定の上限 (ai.scene_eval.max_reason_chars と同じ値)。
DEFAULT_MAX_REASON_CHARS = 120


# シーン評価の応答を正規化する。返り値は (評価のリスト, 記録).
#
# candidate_count を渡すと、範囲外の candidate_index を落とす
# (モデルが存在しない候補を指しても素材が無い)。
def normalize_scene_eval(payload, candidate_count=0,
                         max_reason_chars=DEFAULT_MAX_REASON_CHARS):
    notes = []
    raw_scenes = (payload or {}).get("scenes") if isinstance(payload, dict) else None
    if not isinstance(raw_scenes, list):
        raise AiPlanError(tr("シーン評価の応答に scenes がありません。"))

    scenes = []
    seen = set()
    for index, raw in enumerate(raw_scenes):
        where = "scenes[{0}]".format(index)
        if not isinstance(raw, dict):
            notes.append(note("V1", where, ACTION_DROP, "type"))
            continue
        candidate = _number(raw.get("candidate_index"))
        if candidate is None:
            notes.append(note("V1", where, ACTION_DROP, "candidate_index"))
            continue
        candidate = int(candidate)
        if candidate < 0 or (candidate_count and candidate >= candidate_count):
            notes.append(note("V6", where + ".candidate_index", ACTION_DROP,
                              str(candidate)))
            continue
        if candidate in seen:
            # 同じ候補を 2 回評価してきたら後ろを捨てる (順位が決まらなくなる)。
            notes.append(note("V6", where + ".candidate_index", ACTION_DROP, "duplicate"))
            continue
        seen.add(candidate)

        scenes.append({
            "candidate_index": candidate,
            "start": _optional_sec(raw.get("start")),
            "end": _optional_sec(raw.get("end")),
            "scores": _scores_of(raw.get("scores"), notes, where + ".scores"),
            "summary": _shortened(raw.get("summary"), max_reason_chars,
                                  notes, where + ".summary"),
            "reason": _shortened(raw.get("reason"), max_reason_chars,
                                 notes, where + ".reason"),
            "needed_context": _needed_context_of(raw.get("needed_context"),
                                                 max_reason_chars, notes, where),
            "recommended_use": _recommended_use_of(raw.get("recommended_use"),
                                                   notes, where),
            "peak": _optional_sec(raw.get("peak")),
        })

    return scenes, notes


# 重み (ai.scene_eval.weights[用途]) と既存の採点から総合点を出す (§7.3)。
# 順位そのものを AI に作らせない = 再現性のため。
def rank_score(scores, weights, base_score=0.0, base_weight=0.0):
    total = 0.0
    for key in SCORE_KEYS:
        weight = _number((weights or {}).get(key))
        if weight is None:
            continue
        total += float(scores.get(key, DEFAULT_SCORE)) * weight
    base = _number(base_score) or 0.0

    return round(total + base * (_number(base_weight) or 0.0), 4)


def _scores_of(value, notes, where):
    source = value if isinstance(value, dict) else {}
    scores = {}
    for key in SCORE_KEYS:
        number = _number(source.get(key))
        if number is None:
            notes.append(note("V3", where + "." + key, ACTION_DEFAULT,
                              str(DEFAULT_SCORE)))
            scores[key] = float(DEFAULT_SCORE)
            continue
        scores[key] = _clamped(number, SCORE_MIN, SCORE_MAX, DEFAULT_SCORE,
                               notes, "V5", where + "." + key)

    return scores


def _needed_context_of(value, max_chars, notes, where):
    if not isinstance(value, dict):
        return None
    start = _number(value.get("start"))
    end = _number(value.get("end"))
    if start is None or end is None or end <= start:
        notes.append(note("V6", where + ".needed_context", ACTION_DROP, "range"))

        return None

    return {"start": _round_sec(start), "end": _round_sec(end),
            "why": _shortened(value.get("why"), max_chars, notes,
                              where + ".needed_context.why")}


def _recommended_use_of(value, notes, where):
    if not isinstance(value, list):
        return []
    uses = []
    for item in value:
        text = _text(item).strip()
        if text in RECOMMENDED_USES:
            if text not in uses:
                uses.append(text)
        elif text:
            notes.append(note("V3", where + ".recommended_use", ACTION_DROP, text))

    return uses


def _optional_sec(value):
    number = _number(value)

    return round(number, 3) if number is not None else None


def _shortened(value, max_chars, notes, where):
    text = _text(value).strip()
    limit = int(max_chars or 0)
    if limit > 0 and len(text) > limit:
        notes.append(note("V4", where, ACTION_WRAP, str(limit)))

        return text[:limit]

    return text


# ---- VideoPlan (§8.1) ----------------------------------------------------

# テロップの付け方 (ai.plan.subtitle_mode と同じ語)
SUBTITLE_MODES = ("full", "key", "none")
# SE の量。量だけを決めさせ、どの音かは sounds[].preset で指定させる (§9.4)。
SE_LEVELS = ("none", "light", "normal")
# BGM は受け口だけで適用しない (§19 の 4)。PDF の提案にだけ使う。
BGM_MODES = ("none", "calm", "upbeat")


# 構成案を正規化する。返り値は (VideoPlan, 記録).
def normalize_video_plan(payload, target, max_reason_chars=DEFAULT_MAX_REASON_CHARS):
    if not isinstance(payload, dict):
        raise AiPlanError(tr("構成案の形が正しくありません。"))
    chosen = target if target in TARGETS else payload.get("target")
    if chosen not in TARGETS:
        raise AiPlanError(tr("構成案の target が不正です。"))

    notes = []
    sections = []
    for index, raw in enumerate(payload.get("sections") or []):
        where = "sections[{0}]".format(index)
        if not isinstance(raw, dict):
            notes.append(note("V1", where, ACTION_DROP, "type"))
            continue
        start = _number(raw.get("start"))
        end = _number(raw.get("end"))
        if start is None or end is None:
            notes.append(note("V1", where, ACTION_DROP, "start/end"))
            continue
        role = raw.get("role")
        if role not in SCENE_ROLES:
            notes.append(note("V3", where + ".role", ACTION_DEFAULT, DEFAULT_SCENE_ROLE))
            role = DEFAULT_SCENE_ROLE
        scene_ref = _number(raw.get("scene_ref"))

        sections.append({
            "role": role,
            "scene_ref": int(scene_ref) if scene_ref is not None else None,
            "start": _round_sec(start),
            "end": _round_sec(end),
            "reason": _shortened(raw.get("reason"), max_reason_chars,
                                 notes, where + ".reason"),
        })
    if not sections:
        raise AiPlanError(tr("構成案に使える区間がありません。"))

    return {
        "target": chosen,
        "title": _shortened(payload.get("title"), max_reason_chars, notes, "title"),
        "concept": _shortened(payload.get("concept"), max_reason_chars, notes, "concept"),
        "duration_sec": max(_round_sec(payload.get("duration_sec")), 0.0),
        "sections": sections,
        "direction": _direction_of(payload.get("direction"), chosen, notes),
    }, notes


def _direction_of(value, target, notes):
    source = value if isinstance(value, dict) else {}
    subtitle = source.get("subtitle")
    if subtitle not in SUBTITLE_MODES:
        if _text(subtitle):
            notes.append(note("V3", "direction.subtitle", ACTION_DEFAULT, "full"))
        subtitle = "full"
    effects = source.get("sound_effects")
    if effects not in SE_LEVELS:
        if _text(effects):
            notes.append(note("V3", "direction.sound_effects", ACTION_DEFAULT, "light"))
        effects = "light"
    bgm = source.get("bgm")
    if bgm not in BGM_MODES:
        bgm = "none"

    emphasis = []
    for item in source.get("emphasis_scenes") or []:
        number = _number(item)
        if number is not None and int(number) not in emphasis:
            emphasis.append(int(number))

    return {
        "subtitle": subtitle,
        "emphasis_scenes": emphasis,
        "sound_effects": effects,
        # 適用しない (§19 の 4)。PDF の提案にだけ使う。
        "bgm": bgm,
        "vertical_crop": _flag(source.get("vertical_crop"), target in VERTICAL_TARGETS),
    }


# ---- 構造化出力のスキーマ (§6.2 の schema) -------------------------------
#
# ここで作った形をそのままサーバーへ送り、サーバーが Claude の構造化出力へ渡す。
# 定数から組み立てるため、§9.4 の列挙値とスキーマが食い違うことがない。
# それでもモデルが外した場合は ai_invalid_output として 1 回だけ作り直す (§6.2 追補 A3)。

def _obj(properties, required):
    return {"type": "object", "additionalProperties": False,
            "required": list(required), "properties": properties}


def _enum(values, nullable=False):
    schema = {"type": "string", "enum": list(values)}

    return schema if not nullable else {"anyOf": [schema, {"type": "null"}]}


def _num(minimum=None, maximum=None):
    schema = {"type": "number"}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum

    return schema


def _array(items):
    return {"type": "array", "items": items}


# シーン評価 (§7.2) の期待する応答。
def scene_eval_schema(candidate_count=0, max_reason_chars=DEFAULT_MAX_REASON_CHARS):
    index_schema = {"type": "integer", "minimum": 0}
    if candidate_count:
        index_schema["maximum"] = int(candidate_count) - 1
    text_schema = {"type": "string", "maxLength": int(max_reason_chars or 0) or 120}
    scores = _obj({key: _num(SCORE_MIN, SCORE_MAX) for key in SCORE_KEYS}, SCORE_KEYS)

    scene = _obj({
        "candidate_index": index_schema,
        "start": _num(0),
        "end": _num(0),
        "scores": scores,
        "summary": text_schema,
        "reason": text_schema,
        "needed_context": _obj({"start": _num(0), "end": _num(0), "why": text_schema},
                               ("start", "end", "why")),
        "recommended_use": _array(_enum(RECOMMENDED_USES)),
        "peak": _num(0),
    }, ("candidate_index", "start", "end", "scores", "summary", "reason"))

    return _obj({"scenes": _array(scene)}, ("scenes",))


# 構成案 (§8.1) の期待する応答。
def video_plan_schema(target, max_reason_chars=DEFAULT_MAX_REASON_CHARS):
    chosen = target if target in TARGETS else TARGET_YOUTUBE_LONG
    text_schema = {"type": "string", "maxLength": int(max_reason_chars or 0) or 120}
    roles = SECTION_ROLE_ORDER.get(chosen, SCENE_ROLES)

    section = _obj({
        "role": _enum(roles),
        "scene_ref": {"type": "integer", "minimum": 0},
        "start": _num(0),
        "end": _num(0),
        "reason": text_schema,
    }, ("role", "scene_ref", "start", "end", "reason"))

    direction = _obj({
        "subtitle": _enum(SUBTITLE_MODES),
        "emphasis_scenes": _array({"type": "integer", "minimum": 0}),
        "sound_effects": _enum(SE_LEVELS),
        "bgm": _enum(BGM_MODES),
        "vertical_crop": {"type": "boolean"},
    }, ("subtitle", "emphasis_scenes", "sound_effects", "bgm", "vertical_crop"))

    return _obj({
        "target": _enum((chosen,)),
        "title": text_schema,
        "concept": text_schema,
        "duration_sec": _num(0),
        "sections": _array(section),
        "direction": direction,
    }, ("target", "title", "concept", "duration_sec", "sections", "direction"))


# EditPlan (§9.1) の期待する応答。
def edit_plan_schema(target, max_reason_chars=DEFAULT_MAX_REASON_CHARS):
    chosen = target if target in TARGETS else TARGET_YOUTUBE_LONG
    text_schema = {"type": "string", "maxLength": int(max_reason_chars or 0) or 120}

    scene = _obj({
        "id": {"type": "string", "maxLength": 32},
        "source_start": _num(0),
        "source_end": _num(0),
        "role": _enum(SCENE_ROLES),
        "order": {"type": "integer", "minimum": 1},
        "reason": text_schema,
        "crop": _obj({"mode": _enum(CROP_MODES),
                      "scale": _num(CROP_SCALE_MIN, CROP_SCALE_MAX)}, ("mode", "scale")),
        "zoom": {"anyOf": [
            _obj({"scale": _num(ZOOM_MIN, ZOOM_MAX), "x": _num(-1.0, 1.0),
                  "y": _num(-1.0, 1.0)}, ("scale", "x", "y")),
            {"type": "null"}]},
        "gain_db": _num(GAIN_MIN_DB, GAIN_MAX_DB),
    }, ("id", "source_start", "source_end", "role", "order", "reason"))

    subtitle = _obj({
        "scene_id": {"type": "string", "maxLength": 32},
        "source_start": _num(0),
        "source_end": _num(0),
        "text": {"type": "string"},
        "track_role": _enum(TRACK_ROLES),
        "animation": _enum(("",) + ANIMATIONS),
        "fade_in": {"type": "boolean"},
        "fade_out": {"type": "boolean"},
        "color": {"type": "string"},
        "align": _enum(("",) + ALIGNS),
        "reason": text_schema,
    }, ("scene_id", "source_start", "source_end", "text", "track_role", "animation"))

    sound = _obj({
        "scene_id": {"type": "string", "maxLength": 32},
        "offset_sec": _num(0),
        "preset": {"type": "string", "maxLength": 64},
        "gain_db": _num(GAIN_MIN_DB, GAIN_MAX_DB),
        "reason": text_schema,
    }, ("scene_id", "offset_sec", "preset", "gain_db"))

    return _obj({
        "plan_version": _enum_int(PLAN_VERSION),
        "project": _obj({
            "target": _enum((chosen,)),
            "source": _obj({"kind": _enum(SOURCE_KINDS),
                            "video_id": {"type": "string"},
                            "path": {"type": "string"}}, ("kind",)),
            "canvas": _obj({"orientation": _enum(ORIENTATIONS)}, ("orientation",)),
            "duration_sec": _num(0),
        }, ("target", "canvas", "duration_sec")),
        "scenes": _array(scene),
        "subtitles": _array(subtitle),
        "sounds": _array(sound),
        "editing": _obj({"subtitle": {"type": "boolean"},
                         "vertical_crop": {"type": "boolean"},
                         "sound_effects": {"type": "boolean"}},
                        ("subtitle", "vertical_crop", "sound_effects")),
        "notes": {"type": "string"},
    }, ("plan_version", "project", "scenes", "subtitles", "sounds"))


def _enum_int(value):
    return {"type": "integer", "enum": [int(value)]}


# ---- Review (§12.3) ------------------------------------------------------
#
# レビューの指摘。**時刻がどちらの座標か (coord) を必ず持つ** (曖昧さを残さない / §12.3)。
# 指摘の採否と適用は reviewer が決める。ここは形だけを見る。

ISSUE_KINDS = ("pacing", "clarity", "funny", "subtitle", "audio", "visual", "platform")
SEVERITIES = ("minor", "major", "critical")
# 重大度の重さ (下限の比較に使う)
SEVERITY_RANK = {"minor": 1, "major": 2, "critical": 3}
FIX_OPS = ("trim", "drop", "reorder", "edit_subtitle", "add_subtitle", "add_sound",
           "set_animation", "none")
COORDS = ("timeline", "source")
DEFAULT_COORD = "source"


# レビューの応答を正規化する。返り値は (指摘のリスト, 記録).
#
# scene_ids を渡すと、存在しないシーンを指した指摘を落とす。
def normalize_review(payload, scene_ids=None, max_detail_chars=DEFAULT_MAX_REASON_CHARS,
                     role=""):
    raw = (payload or {}).get("issues") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise AiPlanError(tr("レビューの応答に issues がありません。"))

    known = set(scene_ids or [])
    notes = []
    issues = []
    for index, item in enumerate(raw):
        where = "issues[{0}]".format(index)
        if not isinstance(item, dict):
            notes.append(note("R1", where, ACTION_DROP, "type"))
            continue
        scene_id = _text(item.get("scene_id")).strip()
        if known and scene_id and scene_id not in known:
            notes.append(note("R1", where + ".scene_id", ACTION_DROP, "unknown"))
            continue

        kind = item.get("kind")
        if kind not in ISSUE_KINDS:
            notes.append(note("R3", where + ".kind", ACTION_DEFAULT, "pacing"))
            kind = "pacing"
        severity = item.get("severity")
        if severity not in SEVERITIES:
            notes.append(note("R3", where + ".severity", ACTION_DEFAULT, "minor"))
            severity = "minor"

        issues.append({
            "id": _text(item.get("id")).strip() or "iss{0}".format(index + 1),
            "role": str(role or ""),
            "at": _at_of(item.get("at"), notes, where),
            "scene_id": scene_id,
            "kind": kind,
            "severity": severity,
            "detail": _shortened(item.get("detail"), max_detail_chars, notes,
                                 where + ".detail"),
            "reason": _shortened(item.get("reason"), max_detail_chars, notes,
                                 where + ".reason"),
            "fix": _fix_of(item.get("fix"), notes, where),
            "expected": _shortened(item.get("expected"), max_detail_chars, notes,
                                   where + ".expected"),
            "priority": _priority_of(item.get("priority")),
        })

    return issues, notes


def _at_of(value, notes, where):
    at = value if isinstance(value, dict) else {}
    start = _number(at.get("start"))
    end = _number(at.get("end"))
    coord = at.get("coord")
    if coord not in COORDS:
        if _text(coord):
            notes.append(note("R3", where + ".at.coord", ACTION_DEFAULT, DEFAULT_COORD))
        coord = DEFAULT_COORD

    return {"start": _round_sec(start) if start is not None else None,
            "end": _round_sec(end) if end is not None else None,
            "coord": coord}


def _fix_of(value, notes, where):
    fix = value if isinstance(value, dict) else {}
    op = fix.get("op")
    if op not in FIX_OPS:
        if _text(op):
            notes.append(note("R3", where + ".fix.op", ACTION_DEFAULT, "none"))
        op = "none"

    return {
        "op": op,
        "target": _text(fix.get("target")).strip(),
        "source_start": _optional_sec(fix.get("source_start")),
        "source_end": _optional_sec(fix.get("source_end")),
        "text": _text(fix.get("text")),
        "animation": _text(fix.get("animation")).strip(),
        "preset": _text(fix.get("preset")).strip(),
        "order": [int(_number(value) or 0) for value in (fix.get("order") or [])
                  if _number(value) is not None],
    }


def _priority_of(value):
    number = _number(value)

    return int(number) if number is not None and number > 0 else 5


# 重大度が下限を満たすか (§12.4)。
def meets_severity(severity, minimum):
    return (SEVERITY_RANK.get(str(severity), 0)
            >= SEVERITY_RANK.get(str(minimum), SEVERITY_RANK["major"]))


# レビューの期待する応答 (構造化出力)。
def review_schema(scene_ids=None, max_detail_chars=DEFAULT_MAX_REASON_CHARS):
    text_schema = {"type": "string", "maxLength": int(max_detail_chars or 0) or 120}
    scene_schema = ({"type": "string", "enum": list(scene_ids)} if scene_ids
                    else {"type": "string", "maxLength": 32})

    issue = _obj({
        "id": {"type": "string", "maxLength": 16},
        "at": _obj({"start": _num(0), "end": _num(0), "coord": _enum(COORDS)},
                   ("start", "end", "coord")),
        "scene_id": scene_schema,
        "kind": _enum(ISSUE_KINDS),
        "severity": _enum(SEVERITIES),
        "detail": text_schema,
        "reason": text_schema,
        "fix": _obj({
            "op": _enum(FIX_OPS),
            "target": {"type": "string", "maxLength": 32},
            "source_start": _num(0),
            "source_end": _num(0),
            "text": {"type": "string"},
            "animation": _enum(("",) + ANIMATIONS),
            "preset": {"type": "string", "maxLength": 64},
            "order": _array({"type": "integer", "minimum": 1}),
        }, ("op",)),
        "expected": text_schema,
        "priority": {"type": "integer", "minimum": 1, "maximum": 9},
    }, ("id", "at", "scene_id", "kind", "severity", "detail", "reason", "fix",
        "expected", "priority"))

    return _obj({"issues": _array(issue)}, ("issues",))
