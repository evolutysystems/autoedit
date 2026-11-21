# AI 版と人間の修正版の差分 (docs/AiEditor/resolve1.md §14 / R12)
#
# 比べるのは 2 つのプロジェクト JSON である (§14.1)。
#   AI 版   : plan_applier の適用直後に保存した <project>.ai-base.json
#   人間版  : 利用者が編集画面で保存した本体
#
# **時刻は VOD 時間へ戻してから突き合わせる** (§14.2)。Timeline 時間のまま比べると、
# 1 か所のカットで以降が全部「変更された」ことになってしまう。
#   VOD 秒 = 素材の vod_start + 素材内の秒   (source.archive.media が持つ)
#
# 差分には必ず `context` を付ける (§14.3)。編集操作だけ溜めても
# 「どういう状況でその編集をしたか」が学べない。
# `hypothesis` は後で AI が付ける。ここでは確定事実だけを書く。
import json
import os

from ..utils.logger import get_logger
from . import config as ai_config

_logger = get_logger(__name__)

# 分類 (§14.2)
KIND_CUT_SHORTEN = "cut_shorten"
KIND_CUT_EXTEND = "cut_extend"
KIND_SCENE_DROP = "scene_drop"
KIND_SCENE_ADD = "scene_add"
KIND_REORDER = "reorder"
KIND_SUBTITLE_TEXT = "subtitle_text"
KIND_SUBTITLE_MOVE = "subtitle_move"
KIND_SUBTITLE_STYLE = "subtitle_style"
KIND_ANIMATION_ADD = "animation_add"
KIND_ANIMATION_REMOVE = "animation_remove"
KIND_ANIMATION_CHANGE = "animation_change"
KIND_SE_ADD = "se_add"
KIND_SE_REMOVE = "se_remove"
KIND_SE_GAIN = "se_gain"
KIND_AUDIO_GAIN = "audio_gain"
KIND_EMPHASIS_ADD = "emphasis_add"

_KIND_VIDEO = "video"
_KIND_AUDIO = "audio"
_KIND_SUBTITLE = "subtitle"
_KIND_SOUND = "sound"

# 音量の差を「変えた」とみなす下限 (dB)
_GAIN_EPS = 0.5


# 差分を取る。返り値は §14.3 の形のリスト。
#
# ai_project / human_project : プロジェクト JSON のパス または dict
# plan        : EditPlan (シーンの役割を context へ入れるため。無くてもよい)
# bundle      : AnalysisBundle (文字起こし・コメント・音量を context へ入れる)
# ignore_ranges: 検証で落とした区間 (人間が入れ直した分は学習しない / §14.4)
def diff(ai_project, human_project, settings, plan=None, bundle=None,
         ignore_ranges=None, drop_singletons=True):
    before = _read(ai_project)
    after = _read(human_project)
    if not before or not after:
        return []

    tolerance = float(ai_config.ai_config(settings)["learning"]
                      .get("match_tolerance_sec") or 0.3)
    old_map = _VodMap(before)
    new_map = _VodMap(after)
    rows = []

    rows.extend(_scene_diffs(old_map, new_map, tolerance, plan, bundle))
    rows.extend(_subtitle_diffs(old_map, new_map, tolerance, plan, bundle))
    rows.extend(_sound_diffs(old_map, new_map, tolerance, plan, bundle))
    rows.extend(_audio_diffs(old_map, new_map, tolerance, plan, bundle))

    rows = [row for row in rows
            if not _inside_any(row["vod_range"], ignore_ranges, tolerance)]
    if drop_singletons:
        rows = _drop_singletons(rows)
    _logger.info("差分: %d 件 (許容 %.2fs)", len(rows), tolerance)

    return rows


# プロジェクトが学習の対象外か (§14.4 の「利用者が指定したプロジェクト」)。
def is_excluded(project_path, settings):
    excluded = ai_config.ai_config(settings)["learning"].get("exclude") or []
    target = os.path.normcase(os.path.abspath(str(project_path or "")))
    for entry in excluded:
        value = os.path.normcase(os.path.abspath(str(entry)))
        if target == value or target.startswith(value + os.sep):
            return True

    return False


def save(rows, project_path, settings, root=""):
    from . import store

    if not rows:
        return ""

    return store.save_json({"project": os.path.basename(str(project_path or "")),
                            "count": len(rows), "diffs": rows},
                           store.diff_path(project_path, root))


# ---- シーン (カット・削除・追加・並べ替え) ------------------------------

def _scene_diffs(old_map, new_map, tolerance, plan, bundle):
    rows = []
    matched = []
    used = set()
    for old in old_map.scenes:
        new = _nearest_scene(new_map.scenes, old, tolerance, used)
        if new is None:
            rows.append(_row(KIND_SCENE_DROP, old["vod_start"], old["vod_end"],
                             ai={"head": old["vod_start"], "sec": old["sec"]},
                             human={}, delta={"sec": -old["sec"]},
                             plan=plan, bundle=bundle))
            continue
        used.add(id(new))
        matched.append((old, new))
        head = round(new["vod_start"] - old["vod_start"], 3)
        tail = round(new["vod_end"] - old["vod_end"], 3)
        if abs(head) < tolerance and abs(tail) < tolerance:
            continue
        kind = (KIND_CUT_SHORTEN if new["sec"] < old["sec"] - tolerance
                else KIND_CUT_EXTEND)
        rows.append(_row(kind, old["vod_start"], old["vod_end"],
                         ai={"head": old["vod_start"], "sec": old["sec"]},
                         human={"head": new["vod_start"], "sec": new["sec"]},
                         delta={"head_sec": head, "tail_sec": tail},
                         plan=plan, bundle=bundle))

    for new in new_map.scenes:
        if id(new) in used:
            continue
        rows.append(_row(KIND_SCENE_ADD, new["vod_start"], new["vod_end"],
                         ai={}, human={"head": new["vod_start"], "sec": new["sec"]},
                         delta={"sec": new["sec"]}, plan=plan, bundle=bundle))

    rows.extend(_reorder_diff(matched, plan, bundle))

    return rows


def _reorder_diff(matched, plan, bundle):
    if len(matched) < 2:
        return []
    old_order = [old["vod_start"] for old, _ in
                 sorted(matched, key=lambda pair: pair[0]["timeline_start"])]
    new_order = [old["vod_start"] for old, _ in
                 sorted(matched, key=lambda pair: pair[1]["timeline_start"])]
    if old_order == new_order:
        return []

    return [_row(KIND_REORDER, min(old_order), max(old_order), ai={"order": old_order},
                 human={"order": new_order}, delta={}, plan=plan, bundle=bundle)]


def _nearest_scene(scenes, old, tolerance, used):
    best = None
    best_gap = None
    for scene in scenes:
        if id(scene) in used:
            continue
        overlap = (min(old["vod_end"], scene["vod_end"])
                   - max(old["vod_start"], scene["vod_start"]))
        if overlap <= 0:
            continue
        gap = abs(scene["vod_start"] - old["vod_start"])
        if best_gap is None or gap < best_gap:
            best = scene
            best_gap = gap

    return best


# ---- テロップ ------------------------------------------------------------

def _subtitle_diffs(old_map, new_map, tolerance, plan, bundle):
    rows = []
    used = set()
    for old in old_map.subtitles:
        new = _nearest(new_map.subtitles, old, tolerance, used)
        if new is None:
            continue
        used.add(id(new))
        rows.extend(_one_subtitle(old, new, plan, bundle))

    for new in new_map.subtitles:
        if id(new) in used or not _is_emphasized(new):
            continue
        # AI 版に無いテロップで、強調が付いているもの (人間が足した山)
        rows.append(_row(KIND_EMPHASIS_ADD, new["vod_start"], new["vod_end"],
                         ai={}, human=_look(new), delta={}, plan=plan,
                         bundle=bundle))

    return rows


def _one_subtitle(old, new, plan, bundle):
    rows = []
    if old["text"] != new["text"]:
        rows.append(_row(KIND_SUBTITLE_TEXT, old["vod_start"], old["vod_end"],
                         ai={"text": old["text"]}, human={"text": new["text"]},
                         delta={"chars": len(new["text"]) - len(old["text"])},
                         plan=plan, bundle=bundle))
    if (old["x"], old["y"], old["align"], old["font_size"]) != \
            (new["x"], new["y"], new["align"], new["font_size"]):
        rows.append(_row(KIND_SUBTITLE_MOVE, old["vod_start"], old["vod_end"],
                         ai=_look(old), human=_look(new),
                         delta={"font_size": (new["font_size"] or 0)
                                - (old["font_size"] or 0)},
                         plan=plan, bundle=bundle))
    if (old["color"], old["outline_color"]) != (new["color"], new["outline_color"]):
        rows.append(_row(KIND_SUBTITLE_STYLE, old["vod_start"], old["vod_end"],
                         ai={"color": old["color"],
                             "outline_color": old["outline_color"]},
                         human={"color": new["color"],
                                "outline_color": new["outline_color"]},
                         delta={}, plan=plan, bundle=bundle))
    if old["animation"] != new["animation"]:
        if not old["animation"]:
            kind = KIND_ANIMATION_ADD
        elif not new["animation"]:
            kind = KIND_ANIMATION_REMOVE
        else:
            kind = KIND_ANIMATION_CHANGE
        rows.append(_row(kind, old["vod_start"], old["vod_end"],
                         ai={"animation": old["animation"]},
                         human={"animation": new["animation"]}, delta={},
                         plan=plan, bundle=bundle))

    return rows


def _is_emphasized(item):
    return bool(item["animation"] or item["color"] or item["font_size"])


def _look(item):
    return {"x": item["x"], "y": item["y"], "align": item["align"],
            "font_size": item["font_size"], "animation": item["animation"],
            "color": item["color"]}


# ---- SE と音量 -----------------------------------------------------------

def _sound_diffs(old_map, new_map, tolerance, plan, bundle):
    rows = []
    used = set()
    for old in old_map.sounds:
        new = _nearest(new_map.sounds, old, tolerance, used)
        if new is None:
            rows.append(_row(KIND_SE_REMOVE, old["vod_start"], old["vod_end"],
                             ai={"gain_db": old["gain_db"]}, human={}, delta={},
                             plan=plan, bundle=bundle))
            continue
        used.add(id(new))
        if abs(new["gain_db"] - old["gain_db"]) >= _GAIN_EPS:
            rows.append(_row(KIND_SE_GAIN, old["vod_start"], old["vod_end"],
                             ai={"gain_db": old["gain_db"]},
                             human={"gain_db": new["gain_db"]},
                             delta={"gain_db": round(new["gain_db"]
                                                     - old["gain_db"], 2)},
                             plan=plan, bundle=bundle))
    for new in new_map.sounds:
        if id(new) in used:
            continue
        rows.append(_row(KIND_SE_ADD, new["vod_start"], new["vod_end"], ai={},
                         human={"gain_db": new["gain_db"]}, delta={}, plan=plan,
                         bundle=bundle))

    return rows


def _audio_diffs(old_map, new_map, tolerance, plan, bundle):
    rows = []
    used = set()
    for old in old_map.audio:
        new = _nearest(new_map.audio, old, tolerance, used)
        if new is None:
            continue
        used.add(id(new))
        if abs(new["gain_db"] - old["gain_db"]) >= _GAIN_EPS \
                or bool(new["muted"]) != bool(old["muted"]):
            rows.append(_row(KIND_AUDIO_GAIN, old["vod_start"], old["vod_end"],
                             ai={"gain_db": old["gain_db"], "muted": old["muted"]},
                             human={"gain_db": new["gain_db"],
                                    "muted": new["muted"]},
                             delta={"gain_db": round(new["gain_db"]
                                                     - old["gain_db"], 2)},
                             plan=plan, bundle=bundle))

    return rows


def _nearest(items, target, tolerance, used):
    best = None
    best_gap = None
    for item in items:
        if id(item) in used:
            continue
        gap = abs(item["vod_start"] - target["vod_start"])
        if gap > max(tolerance, 0.3) + 0.0001:
            continue
        if best_gap is None or gap < best_gap:
            best = item
            best_gap = gap

    return best


# ---- 差分 1 件 (§14.3) ---------------------------------------------------

def _row(kind, start, end, ai=None, human=None, delta=None, plan=None, bundle=None):
    scene = _scene_at(plan, start, end)

    return {
        "kind": kind,
        "scene_id": scene["id"] if scene else "",
        "vod_range": {"start": round(float(start), 3), "end": round(float(end), 3)},
        "ai": dict(ai or {}),
        "human": dict(human or {}),
        "delta": dict(delta or {}),
        "context": _context(start, end, scene, bundle),
        # AI が後で付ける (確定事実にしない / §14.3)
        "hypothesis": None,
        "user_note": "",
    }


def _context(start, end, scene, bundle):
    context = {"role": scene["role"] if scene else ""}
    if bundle is None:
        return context
    lines = [item["text"] for item in getattr(bundle, "transcript", []) or []
             if float(item["end"]) > float(start) and float(item["start"]) < float(end)]
    if lines:
        context["transcript"] = " ".join(lines)[:200]
    comments = [item for item in getattr(bundle, "comments", []) or []
                if float(start) <= float(item.get("offset_sec", -1.0)) <= float(end)]
    length = max(float(end) - float(start), 1e-6)
    if comments:
        context["comment_rate"] = round(len(comments) / (length / 60.0), 2)
    cells = [cell for cell in getattr(bundle, "cells", []) or []
             if float(cell.get("end", 0.0)) > float(start)
             and float(cell.get("start", 0.0)) < float(end)]
    peaks = [float(cell["max_db"]) for cell in cells if cell.get("max_db") is not None]
    if peaks:
        context["max_db"] = round(max(peaks), 1)

    return context


def _scene_at(plan, start, end):
    for scene in (plan or {}).get("scenes") or []:
        if (float(scene["source_end"]) > float(start)
                and float(scene["source_start"]) < float(end)):
            return scene

    return None


# ---- §14.4 学習に取り込まない差分 --------------------------------------

# 1 プロジェクトで 1 回しか現れない種類は落とす (一時的な修正の可能性が高い)。
def _drop_singletons(rows):
    counts = {}
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1

    return [row for row in rows if counts[row["kind"]] > 1]


def _inside_any(vod_range, ranges, tolerance):
    for start, end in ranges or []:
        if (float(vod_range["start"]) >= float(start) - tolerance
                and float(vod_range["end"]) <= float(end) + tolerance):
            return True

    return False


# ---- プロジェクト JSON → VOD 時間 --------------------------------------

# プロジェクト JSON のクリップを VOD 時間へ写した見え方。
#
# 素材内の秒 + 素材の vod_start = VOD の秒 (archive/timeline_builder の約束)。
# vod_start の控えが無いプロジェクト (クリップ用) は Timeline 時間のまま扱う。
class _VodMap:

    def __init__(self, data):
        self._data = data if isinstance(data, dict) else {}
        self._offsets = self._media_offsets()
        self.scenes = self._video()
        self.subtitles = self._subtitles()
        self.sounds = self._sounds()
        self.audio = self._audio()

    def _media_offsets(self):
        archive = ((self._data.get("source") or {}).get("archive") or {})
        offsets = {}
        for row in archive.get("media") or []:
            if isinstance(row, dict) and row.get("media_id"):
                offsets[str(row["media_id"])] = float(row.get("vod_start", 0.0) or 0.0)
        for row in archive.get("clips") or []:
            if isinstance(row, dict) and row.get("media_id"):
                offsets.setdefault(str(row["media_id"]),
                                   float(row.get("vod_start", 0.0) or 0.0))

        return offsets

    def _clips(self, kind):
        clips = []
        for track in self._data.get("tracks") or []:
            if not isinstance(track, dict) or str(track.get("kind")) != kind:
                continue
            for clip in track.get("clips") or []:
                if isinstance(clip, dict):
                    clips.append(clip)
        clips.sort(key=lambda clip: float(clip.get("timeline_start", 0.0) or 0.0))

        return clips

    def _video(self):
        scenes = []
        for clip in self._clips(_KIND_VIDEO):
            offset = self._offsets.get(str(clip.get("media_id", "")), 0.0)
            start = offset + float(clip.get("source_in", 0.0) or 0.0)
            end = offset + float(clip.get("source_out", 0.0) or 0.0)
            scenes.append({
                "id": str(clip.get("id", "")),
                "media_id": str(clip.get("media_id", "")),
                "timeline_start": float(clip.get("timeline_start", 0.0) or 0.0),
                "timeline_end": (float(clip.get("timeline_start", 0.0) or 0.0)
                                 + float(clip.get("duration", 0.0) or 0.0)),
                "vod_start": round(start, 3),
                "vod_end": round(end, 3),
                "sec": round(float(clip.get("duration", 0.0) or 0.0), 3),
            })

        return scenes

    # Timeline 時刻 → VOD 秒 (その時刻を含む V1 クリップから求める)。
    def to_vod(self, timeline_sec):
        value = float(timeline_sec)
        for scene in self.scenes:
            if scene["timeline_start"] - 1e-6 <= value < scene["timeline_end"] - 1e-6:
                return round(scene["vod_start"]
                             + (value - scene["timeline_start"]), 3)
        if self.scenes:
            last = self.scenes[-1]
            if abs(value - last["timeline_end"]) <= 1e-6:
                return last["vod_end"]

        return round(value, 3)

    def _subtitles(self):
        rows = []
        for clip in self._clips(_KIND_SUBTITLE):
            start = float(clip.get("timeline_start", 0.0) or 0.0)
            end = start + float(clip.get("duration", 0.0) or 0.0)
            transform = clip.get("transform") or {}
            rows.append({
                "id": str(clip.get("id", "")),
                "vod_start": self.to_vod(start),
                "vod_end": self.to_vod(max(end - 1e-3, start)),
                "text": str(clip.get("text", "") or ""),
                "role": str(clip.get("role", "") or ""),
                "animation": str(clip.get("animation", "") or ""),
                "color": str(clip.get("color", "") or ""),
                "outline_color": str(clip.get("outline_color", "") or ""),
                "align": str(clip.get("align", "") or ""),
                "font_size": clip.get("font_size"),
                "x": transform.get("x"),
                "y": transform.get("y"),
            })

        return rows

    def _sounds(self):
        rows = []
        for clip in self._clips(_KIND_SOUND):
            start = float(clip.get("timeline_start", 0.0) or 0.0)
            end = start + float(clip.get("duration", 0.0) or 0.0)
            rows.append({"id": str(clip.get("id", "")),
                         "vod_start": self.to_vod(start),
                         "vod_end": self.to_vod(max(end - 1e-3, start)),
                         "gain_db": float(clip.get("gain_db", 0.0) or 0.0),
                         "muted": bool(clip.get("muted"))})

        return rows

    def _audio(self):
        by_clip = {scene["id"]: scene for scene in self.scenes}
        rows = []
        for clip in self._clips(_KIND_AUDIO):
            linked = by_clip.get(str(clip.get("link_clip", "")))
            if linked is None:
                continue
            rows.append({"id": str(clip.get("id", "")),
                         "vod_start": linked["vod_start"],
                         "vod_end": linked["vod_end"],
                         "gain_db": float(clip.get("gain_db", 0.0) or 0.0),
                         "muted": bool(clip.get("muted"))})

        return rows


def _read(project):
    if isinstance(project, dict):
        return project
    path = str(project or "")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as e:
        _logger.warning("プロジェクトを読めませんでした (%s): %s", path, e)

        return None


# 保存されたプロジェクト 1 件ぶんの差分を取って置き場へ残す (§14.1 の「保存時」)。
#
# 画面 (保存の直後) から 1 回呼ぶだけで済むようにまとめてある。
# 学習が無効・対象外・AI 版のスナップショットが無いときは何もしない。
# 返り値は (差分のリスト, 保存先のパス)。
def record_for_project(project_path, settings, bundle=None, root=""):
    from . import plan_schema

    learning = ai_config.ai_config(settings)["learning"]
    if not learning.get("enabled"):
        return ([], "")
    if is_excluded(project_path, settings):
        _logger.info("学習の対象外に指定されたプロジェクトです: %s", project_path)

        return ([], "")

    base = plan_schema.base_path_for(project_path)
    if not base or not os.path.exists(base):
        # AI が作ったプロジェクトではない (手で作ったもの) = 比べる相手が無い。
        return ([], "")

    plan = None
    plan_path = plan_schema.plan_path_for(project_path)
    if plan_path and os.path.exists(plan_path):
        try:
            plan = plan_schema.load(plan_path)
        except Exception:  # noqa: BLE001 (plan が読めなくても差分は取れる)
            plan = None

    rows = diff(base, project_path, settings, plan=plan, bundle=bundle)

    return (rows, save(rows, project_path, settings, root=root))
