# EditPlan の検証 (docs/AiEditor/resolve1.md §10 / R7)
#
# **AI の出力をそのまま実行しない。** 検証は 2 段で行う。
#   ・形の検証 (V1〜V5)   plan_schema.normalize が行う (読める形か)
#   ・中身の検証 (V6〜V14) ここが行う (素材と設定に対して実行できるか)
#   ・V15 (適用後の Timeline が project_io.validate を通るか) は plan_applier が行う。
#     CommandStack のスナップショットで巻き戻せる場所でしか判定できないため。
#
# 原則は「黙って崩さない」(§10 末尾 / design.md §5.4)。
#   落とす   = その要素を捨てる (理由を記録)
#   丸める   = 既定値へ寄せる
#   切る     = 範囲へ収める
# 落ちた件数は画面へ 1 行で出し、理由はログへ残す (§18.2)。
#
# ここも Qt 非依存であり、timeline / settings_window を import しない。
# 設定は渡された dict から読む (既存の archive/config.py と同じ作法)。
import os

from ..exceptions import AiPlanError
from ..i18n import tr
from ..utils.logger import get_logger
from . import config as ai_config
from . import plan_schema as schema

_logger = get_logger(__name__)

# 設定が渡されなかったときの既定 (出どころは settings_window.DEFAULT_SETTINGS)。
# ai/ は settings_window を import しないため写しを持つ。
_DEFAULT_MIN_CLIP_SEC = 0.05            # timeline.min_clip_sec
_DEFAULT_MAX_LINE_LENGTH = 20           # subtitle.max_line_length
_DEFAULT_MAX_LINES = 2                  # subtitle.subtitle_max_lines
_DEFAULT_MAX_MIX_INPUTS = 32            # timeline.sound_tracks.max_mix_inputs
# テロップの重なり判定の許容差 (project_io._OVERLAP_TOLERANCE_SEC と同じ)。
# ミリ秒精度の丸めで生じる 1ms 程度のずれを重なりとみなさない。
_OVERLAP_TOLERANCE_SEC = 0.0015
# 尺を削るときに残す役割 (§10.2 V10)。山と入りは落とさない。
_KEEP_ROLES = ("opening", "climax", "hook", "punchline")


# 検証の結果。落とした件数と理由を持つ。
class PlanReport:

    def __init__(self, notes=None):
        self.notes = list(notes or [])

    def add(self, check, where, action, detail=""):
        self.notes.append(schema.note(check, where, action, detail))

    def extend(self, notes):
        self.notes.extend(notes or [])

    # 落とした件数 (画面に出すのはこれだけ)。
    @property
    def dropped(self):
        return schema.dropped_count(self.notes)

    # 手を入れた件数 (落とした分を含む)。
    @property
    def changed(self):
        return len(self.notes)

    # 画面へ出す 1 行。手を入れていなければ空文字 (何も出さない)。
    def message(self):
        if not self.dropped:
            return ""

        return tr("AI の指示のうち {count} 件を適用できませんでした。").format(
            count=self.dropped)

    # ログへ 1 行。本文 (テロップの文言) は含まない (§17.2)。
    def log(self):
        if not self.notes:
            return
        _logger.info("EditPlan の検証: 変更 %d 件 (落とした %d 件) %s",
                     self.changed, self.dropped, schema.format_notes(self.notes))


# 手元にある素材の区間 (§10.2 V8)。
#
# ranges は候補として切り出した VOD 区間 [(start, end), ...]。
# ensure は無い区間を取り直す呼び出し (pipeline/ai_runner が vod_range.fetch_range を渡す)。
# どちらも渡さなければ「素材の有無は問わない」= 手書きの EditPlan をそのまま通す。
class Materials:

    def __init__(self, ranges=None, ensure=None):
        self.ranges = [(float(a), float(b)) for a, b in (ranges or [])]
        self._ensure = ensure
        # 取り直した区間 (呼び出し側が素材の対応を更新するために見る)
        self.fetched = []

    # 区間が手元の素材に収まっているか。
    def covers(self, start, end):
        if not self.ranges and self._ensure is None:
            return True

        return any(a - _OVERLAP_TOLERANCE_SEC <= start
                   and end <= b + _OVERLAP_TOLERANCE_SEC
                   for a, b in self.ranges)

    # 無ければ取り直す。取れたら True。
    def ensure(self, start, end):
        if self.covers(start, end):
            return True
        if self._ensure is None:
            return False
        try:
            if not self._ensure(start, end):
                return False
        except Exception as e:  # noqa: BLE001 (取り直しの失敗で経路を止めない)
            _logger.warning("区間 %.1f〜%.1f の取り直しに失敗しました: %s", start, end, e)

            return False
        self.ranges.append((float(start), float(end)))
        self.fetched.append((float(start), float(end)))

        return True


# EditPlan を検証して「実行できる形」へ直す。返り値は (plan, PlanReport).
#
# duration_sec は元 VOD の尺。0 のときは plan の値を使い、どちらも無ければ V7 を飛ばす。
# materials を渡すと素材の有無を見る (V8)。exists はファイルの実在確認 (テストで差し替える)。
def validate(plan, duration_sec=0.0, settings=None, target=None, materials=None,
             exists=None, kept_ratio=1.0):
    cfg = ai_config.ai_config(settings)
    limits = _limits(settings, cfg, plan, target, kept_ratio=kept_ratio)
    normalized, notes = schema.normalize(plan, target=target,
                                         gain_limits=limits["gain"])
    report = PlanReport(notes)
    holder = materials if materials is not None else Materials()
    is_file = exists if exists is not None else os.path.isfile

    duration = float(duration_sec or 0.0)
    if duration <= 0:
        duration = float(normalized["project"]["duration_sec"] or 0.0)
    if duration > 0:
        normalized["project"]["duration_sec"] = round(duration, 3)

    scenes = _check_scenes(normalized["scenes"], duration, limits, holder, report)
    if not scenes:
        raise AiPlanError(tr("EditPlan に使えるシーンがありません。"))
    scenes = _fit_duration(scenes, limits, report)
    for position, scene in enumerate(scenes):
        scene["order"] = position + 1
    normalized["scenes"] = scenes

    by_id = {scene["id"]: scene for scene in scenes}
    normalized["subtitles"] = _check_subtitles(
        normalized["subtitles"], by_id, limits, report)
    normalized["sounds"] = _check_sounds(
        normalized["sounds"], by_id, limits, report, is_file)

    report.log()

    return normalized, report


# ---- シーン (V6〜V10) ----------------------------------------------------

def _check_scenes(scenes, duration, limits, materials, report):
    kept = []
    for index, scene in enumerate(scenes):
        where = "scenes[{0}]".format(index)
        start = scene["source_start"]
        end = scene["source_end"]

        # V6 start < end
        if end <= start:
            report.add("V6", where, schema.ACTION_DROP, "start>=end")
            continue

        # V7 VOD の尺に収める
        if duration > 0:
            start, end, cut = _clip_range(start, end, duration)
            if cut:
                report.add("V7", where, schema.ACTION_CUT,
                           "{0:.1f}-{1:.1f}".format(start, end))
            if end <= start:
                report.add("V7", where, schema.ACTION_DROP, "outside")
                continue

        # V9 最短尺
        if end - start < limits["min_clip_sec"]:
            report.add("V9", where, schema.ACTION_DROP,
                       "{0:.3f}".format(end - start))
            continue

        # V8 素材が手元にあるか (無ければ取り直す)
        if not materials.ensure(start, end):
            report.add("V8", where, schema.ACTION_DROP, "no media")
            continue
        if (start, end) in materials.fetched:
            report.add("V8", where, schema.ACTION_REFETCH,
                       "{0:.1f}-{1:.1f}".format(start, end))

        scene["source_start"] = round(start, 3)
        scene["source_end"] = round(end, 3)
        kept.append(scene)

    return kept


# V10 合計尺と本数を上限へ収める。後ろの body から削り、山と入りは残す。
def _fit_duration(scenes, limits, report):
    kept = list(scenes)
    max_scenes = limits["max_scenes"]
    while max_scenes > 0 and len(kept) > max_scenes:
        index = _droppable_index(kept)
        if index is None:
            break
        report.add("V10", "scenes[{0}]".format(index), schema.ACTION_DROP, "max_scenes")
        kept.pop(index)

    max_sec = limits["max_sec"]
    if max_sec <= 0:
        return kept

    while _total_sec(kept) > max_sec and len(kept) > 1:
        index = _droppable_index(kept)
        if index is None:
            break
        report.add("V10", "scenes[{0}]".format(index), schema.ACTION_DROP, "max_sec")
        kept.pop(index)

    over = _total_sec(kept) - max_sec
    if over > 0 and kept:
        # 落とせる役割が無くなっても超えるときは最後のシーンの終わりを詰める。
        last = kept[-1]
        allowed = max(last["source_end"] - over, last["source_start"]
                      + limits["min_clip_sec"])
        if allowed < last["source_end"]:
            report.add("V10", "scenes[{0}]".format(len(kept) - 1),
                       schema.ACTION_CUT, "{0:.1f}".format(allowed))
            last["source_end"] = round(allowed, 3)

    return kept


# 後ろから探して、落としてよい役割のシーンの位置を返す。
def _droppable_index(scenes):
    for index in range(len(scenes) - 1, -1, -1):
        if scenes[index]["role"] not in _KEEP_ROLES:
            return index

    return None


def _total_sec(scenes):
    return sum(scene["source_end"] - scene["source_start"] for scene in scenes)


# ---- テロップ (V4 / V11 / V12) -------------------------------------------

def _check_subtitles(subtitles, by_id, limits, report):
    kept = []
    for index, item in enumerate(subtitles):
        where = "subtitles[{0}]".format(index)
        scene = by_id.get(item["scene_id"])
        if scene is None:
            # 属するシーンが落ちた (V10 など)。テロップも一緒に落ちる。
            report.add("V11", where, schema.ACTION_DROP, "scene dropped")
            continue

        start = item["source_start"]
        end = item["source_end"]
        if end <= start:
            report.add("V6", where, schema.ACTION_DROP, "start>=end")
            continue

        # V11 シーンの区間内へ収める
        if end <= scene["source_start"] or start >= scene["source_end"]:
            report.add("V11", where, schema.ACTION_DROP, "outside scene")
            continue
        start, end, cut = _clip_range(start, end, scene["source_end"],
                                      low=scene["source_start"])
        if cut:
            report.add("V11", where, schema.ACTION_CUT,
                       "{0:.1f}-{1:.1f}".format(start, end))
        if end - start < limits["min_clip_sec"]:
            report.add("V11", where, schema.ACTION_DROP, "too short")
            continue

        # V4 文字数の上限 (上限 = max_line_length × subtitle_max_lines)
        item["text"] = _shorten_text(item["text"], limits["max_text_chars"],
                                     report, where)
        item["source_start"] = round(start, 3)
        item["source_end"] = round(end, 3)
        kept.append(item)

    return _resolve_overlaps(kept, by_id, limits, report)


# V12 同じシーン・同じトラックのテロップが重ならないようにする。
#
# コメント役割は専用トラック (S2) へ置き分かれるため、本人・サブとは重なっても構わない
# (timeline.model.subtitle_track_for_role)。
def _resolve_overlaps(subtitles, by_id, limits, report):
    groups = {}
    for position, item in enumerate(subtitles):
        key = (item["scene_id"], schema.is_comment_role(item["track_role"]))
        groups.setdefault(key, []).append(position)

    dropped = set()
    for key, positions in groups.items():
        scene = by_id.get(key[0])
        order = sorted(positions, key=lambda p: subtitles[p]["source_start"])
        cursor = None
        for position in order:
            item = subtitles[position]
            start = item["source_start"]
            end = item["source_end"]
            if cursor is not None and start < cursor - _OVERLAP_TOLERANCE_SEC:
                start = cursor
                if end - start < limits["min_clip_sec"]:
                    end = start + limits["min_clip_sec"]
                if scene is not None and end > scene["source_end"]:
                    report.add("V12", "subtitles[{0}]".format(position),
                               schema.ACTION_DROP, "no room")
                    dropped.add(position)
                    continue
                report.add("V12", "subtitles[{0}]".format(position),
                           schema.ACTION_SHIFT, "{0:.3f}".format(start))
                item["source_start"] = round(start, 3)
                item["source_end"] = round(end, 3)
            cursor = item["source_end"]

    return [item for position, item in enumerate(subtitles) if position not in dropped]


# 上限を超えた本文を詰める。手動改行 (\N) は文字数に数えない。
def _shorten_text(text, max_chars, report, where):
    if max_chars <= 0:
        return text
    plain = text.replace("\\N", "")
    if len(plain) <= max_chars:
        return text
    report.add("V4", where, schema.ACTION_WRAP, str(max_chars))

    return plain[:max_chars]


# ---- SE (V13 / V14) ------------------------------------------------------

def _check_sounds(sounds, by_id, limits, report, is_file):
    presets = limits["presets"]
    kept = []
    for index, item in enumerate(sounds):
        where = "sounds[{0}]".format(index)
        scene = by_id.get(item["scene_id"])
        if scene is None:
            report.add("V14", where, schema.ACTION_DROP, "scene dropped")
            continue

        # SE はシーン先頭からの相対秒 (§9.2)。シーンの外に鳴らす指示は落とす。
        length = scene["source_end"] - scene["source_start"]
        if item["offset_sec"] >= length:
            report.add("V6", where + ".offset_sec", schema.ACTION_DROP,
                       "{0:.1f}".format(item["offset_sec"]))
            continue

        # V14 プリセットが登録されていて、ファイルが実在するか
        path = presets.get(item["preset"]) if isinstance(presets, dict) else None
        if not isinstance(path, str) or not path.strip():
            report.add("V14", where + ".preset", schema.ACTION_DROP, "unknown")
            continue
        if not is_file(path):
            report.add("V14", where + ".preset", schema.ACTION_DROP, "missing file")
            continue
        item["path"] = path

        # V13 1 回の ffmpeg へ渡せる本数の上限
        if limits["max_mix_inputs"] > 0 and len(kept) >= limits["max_mix_inputs"]:
            report.add("V13", where, schema.ACTION_DROP,
                       str(limits["max_mix_inputs"]))
            continue
        kept.append(item)

    return kept


# ---- 設定の読み出し ------------------------------------------------------

# kept_ratio: 無音を除いて残る割合 (resolve3.md §9)。
# plan の max_sec は**出力尺**なので、素材尺で測る V10 のために割り戻す。
def _limits(settings, cfg, plan, target, kept_ratio=1.0):
    values = settings if isinstance(settings, dict) else {}
    timeline = values.get("timeline", {}) if isinstance(values.get("timeline"), dict) else {}
    sound = timeline.get("sound_tracks", {}) if isinstance(timeline.get("sound_tracks"), dict) else {}
    subtitle = values.get("subtitle", {}) if isinstance(values.get("subtitle"), dict) else {}

    chosen = target if target in schema.TARGETS else _plan_target(plan)
    plan_cfg = cfg.get("plan", {}).get(chosen, {})
    sounds_cfg = cfg.get("sounds", {})

    return {
        "min_clip_sec": _positive(timeline.get("min_clip_sec"), _DEFAULT_MIN_CLIP_SEC),
        "max_text_chars": int(_positive(subtitle.get("max_line_length"),
                                        _DEFAULT_MAX_LINE_LENGTH)
                              * _positive(subtitle.get("subtitle_max_lines"),
                                          _DEFAULT_MAX_LINES)),
        "max_mix_inputs": int(_positive(sound.get("max_mix_inputs"),
                                        _DEFAULT_MAX_MIX_INPUTS)),
        "gain": (_number(sound.get("gain_min_db"), schema.GAIN_MIN_DB),
                 _number(sound.get("gain_max_db"), schema.GAIN_MAX_DB)),
        # 出力尺 → 素材尺へ換算 (resolve3.md §9)
        "max_sec": ai_config.source_sec(_number(plan_cfg.get("max_sec"), 0.0),
                                        kept_ratio),
        "max_scenes": int(_number(plan_cfg.get("max_scenes"), 0.0)),
        # SE が無効なら登録済みでも適用しない (§16)。
        "presets": sounds_cfg.get("presets", {}) if sounds_cfg.get("enabled") else {},
    }


def _plan_target(plan):
    project = plan.get("project") if isinstance(plan, dict) else None
    value = project.get("target") if isinstance(project, dict) else None

    return value if value in schema.TARGETS else schema.TARGET_YOUTUBE_LONG


# 区間を [low, high] へ収める。返り値は (start, end, 切ったか).
def _clip_range(start, end, high, low=0.0):
    new_start = max(start, low)
    new_end = min(end, high)

    return new_start, new_end, (new_start != start or new_end != end)


def _number(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default)

    return float(value)


def _positive(value, default):
    number = _number(value, default)

    return number if number > 0 else float(default)
