# EditPlan → Timeline の適用 (docs/AiEditor/resolve1.md §11 / R8)
#
# **すべて既存のコマンド経由**で行う (§11.1)。これで AI の編集にも Undo/Redo が効き、
# 利用者は「AI の 1 手だけ戻す」ことができる。新しい編集ロジックはここに作らない。
#
# 時間座標 (§9.2 / Z2):
#   EditPlan の秒      元 VOD の秒
#     − 素材の vod_start → 素材内の秒   (cut_region は -c copy のため素材先頭 = 切り出し開始)
#     − TimeMap          → Timeline の秒 (無音カットで消えた分を詰めた後の時刻)
# AI は Timeline 時間を知らない。その変換はここだけが持つ。
#
# 実装時の分担 (設計 §11.1 からの意図的なずれ):
#   ① 骨組み (素材の用意と Timeline の構築) は pipeline/ai_runner が行う。
#      ffmpeg を回す工程であり、進捗とキャンセルを持つ側に置くのが筋である。
#   ② 並べ替えは**骨組みを plan の順で組む**ことで満たす。MoveClip は同一トラックの
#      重なりを避けて位置を丸める (commands._clamp_position) ため、並べ替えの手段には向かない。
#      テロップ・SE も一緒に動かす必要があり、既存コマンドの範囲を超える。
#      そのため ai_runner は「plan が使うシーンだけを plan の順で」用意する。
#   ③〜⑦ と V15 はここで行う (渡された Timeline が plan と違う並びでも、
#      使わないシーンは消し、端は詰める)。
from ..archive.timeline_builder import (ORIGIN_ARCHIVE_INDEX, archive_media_entry,
                                        clip_entry)
from ..exceptions import AiPlanError
from ..i18n import tr
from ..timeline import commands, media_probe, project_io
from ..timeline.builder import timeline_config
from ..timeline.model import SubtitleClip
from ..timeline.timemap import TimeMap
from ..utils.logger import get_logger
from . import plan_schema as schema
from .plan_validator import PlanReport

_logger = get_logger(__name__)

# 時刻の比較の許容誤差 (ミリ秒精度で保持するため)
_EPS = 1e-6

# 記録 (PlanReport) の check は §11.1 の工程番号である (A3 = ③ 不要の削除 /
# A4 = ④ 端の調整・音量・拡大 / A5 = ⑤⑥ テロップ / A7 = ⑦ SE / V15 = ⑨ 検証)。
# 検証側 (plan_validator) の V 番号と混ざらないよう別の記号を使う。


# EditPlan を Timeline へ適用する。返り値は (timeline, PlanReport).
#
# timeline      : ai_runner が組んだ骨組み (plan の各シーンの素材が載っている)
# plan          : plan_validator.validate を通した EditPlan
# stack         : CommandStack。渡さなければここで作る (Undo の履歴は呼び出し側が持つべき)
# scene_indexes : {scene_id: アーカイブ index}。省略時は plan の並び順と同じ index を使う
# probe         : SE のファイルを調べる呼び出し (テストで差し替える)
# validate      : V15 (適用後の project_io.validate) を行うか
def apply_plan(timeline, plan, stack=None, settings=None, report=None,
               scene_indexes=None, probe=None, validate=True):
    cfg = timeline_config(settings)
    report = report if report is not None else PlanReport()
    stack = stack if stack is not None else commands.CommandStack()
    scenes = plan.get("scenes") or []
    indexes = _scene_indexes(scenes, scene_indexes)
    state = {"applied": 0}

    _drop_unused_scenes(timeline, stack, cfg, set(indexes.values()), report, state)

    for scene in scenes:
        index = indexes[scene["id"]]
        _trim_scene(timeline, stack, cfg, scene, index, report, state)
        _apply_scene_audio(timeline, stack, scene, index, report, state)
        _apply_scene_zoom(timeline, stack, scene, index, report, state)

    _apply_subtitles(timeline, stack, cfg, plan, indexes, report, state)
    _apply_sounds(timeline, stack, cfg, plan, indexes, settings, probe, report, state)

    if validate:
        _validate_timeline(timeline, stack, cfg, report, state)

    _logger.info("EditPlan を適用しました: シーン %d / テロップ %d / SE %d / 操作 %d 手",
                 len(scenes), len(plan.get("subtitles") or []),
                 len(plan.get("sounds") or []), state["applied"])

    return timeline, report


# plan のシーンとアーカイブ index の対応。
# ai_runner は plan の順で素材を用意するため、既定は「並び順 = index」である。
def _scene_indexes(scenes, scene_indexes):
    given = scene_indexes if isinstance(scene_indexes, dict) else {}

    return {scene["id"]: int(given.get(scene["id"], position))
            for position, scene in enumerate(scenes)}


# 1 手ぶんのコマンドを積む。変化が無ければ数えない (履歴も汚れない)。
def _push(timeline, stack, command, state):
    if stack.push(timeline, command):
        state["applied"] += 1

        return True

    return False


# ---- ③ 使わないシーンを消す ----------------------------------------------

def _drop_unused_scenes(timeline, stack, cfg, used, report, state):
    track = timeline.base_video_track()
    if track is None:
        return
    extra = sorted({_archive_index(clip) for clip in track.clips} - used - {None})
    for index in extra:
        clip_ids = [clip.id for clip in _scene_clips(timeline, index)]
        if not clip_ids:
            continue
        # リップル削除なら空白を残さず、テロップと SE も一緒に詰まる。
        if _push(timeline, stack,
                 commands.DeleteClip(clip_ids, ripple=True,
                                     min_clip_sec=cfg["min_clip_sec"],
                                     sync_all=_sync_all(cfg)), state):
            report.add("A3", "scene#{0}".format(index), schema.ACTION_DROP, "unused")


# ---- ④ 端の調整 ----------------------------------------------------------

def _trim_scene(timeline, stack, cfg, scene, index, report, state):
    where = "scenes[{0}]".format(scene.get("order", index))
    offset = _vod_offset(timeline, index)
    if offset is None:
        report.add("A4", where, schema.ACTION_DROP, "no media record")

        return
    plan_in = scene["source_start"] - offset
    plan_out = scene["source_end"] - offset

    clips = _scene_clips(timeline, index)
    if not clips:
        report.add("A4", where, schema.ACTION_DROP, "no clip")

        return

    # 後ろから詰める (前を先に詰めると後ろの Timeline 時刻がずれる)。
    if plan_out < max(clip.source_out for clip in clips) - _EPS:
        _trim_edge(timeline, stack, cfg, index, plan_out, "after", report, where, state)
    clips = _scene_clips(timeline, index)
    if clips and plan_in > min(clip.source_in for clip in clips) + _EPS:
        _trim_edge(timeline, stack, cfg, index, plan_in, "before", report, where, state)


def _trim_edge(timeline, stack, cfg, index, source_sec, side, report, where, state):
    clips = _scene_clips(timeline, index)
    clip = _clip_at_source(clips, source_sec)
    if clip is None:
        # 指定の位置が無音カットで消えている。詰める必要が無い (すでに外れている)。
        return
    at = clip.timeline_start + (source_sec - clip.source_in)
    if not (clip.timeline_start + _EPS < at < clip.timeline_end - _EPS):
        return
    if _push(timeline, stack,
             commands.RippleTrimToPlayhead(clip.id, at, side,
                                           min_clip_sec=cfg["min_clip_sec"],
                                           sync_all=_sync_all(cfg)), state):
        report.add("A4", where, schema.ACTION_CUT, "{0}:{1:.3f}".format(side, at))


# 素材内の時刻を含むクリップ。無音カットで消えている時刻なら None。
def _clip_at_source(clips, source_sec):
    for clip in clips:
        if clip.source_in - _EPS <= source_sec < clip.source_out - _EPS:
            return clip

    return None


# ---- ④ 音量と拡大 (シーン単位) ------------------------------------------

def _apply_scene_audio(timeline, stack, scene, index, report, state):
    gain = float(scene.get("gain_db") or 0.0)
    if abs(gain) <= _EPS:
        return
    where = "scenes[{0}].gain_db".format(scene.get("order", index))
    for clip in _scene_clips(timeline, index):
        audio = timeline.audio_clip_for(clip.id)
        if audio is None:
            continue
        if _push(timeline, stack, commands.SetAudioGain(audio.id, gain), state):
            report.add("A4", where, schema.ACTION_CLAMP, "{0:g}".format(gain))


def _apply_scene_zoom(timeline, stack, scene, index, report, state):
    zoom = scene.get("zoom")
    if not isinstance(zoom, dict):
        return
    where = "scenes[{0}].zoom".format(scene.get("order", index))
    scale = float(zoom.get("scale") or 1.0)
    x = float(zoom.get("x") or 0.0)
    y = float(zoom.get("y") or 0.0)
    changed = False
    for clip in _scene_clips(timeline, index):
        if abs(scale - 1.0) > _EPS:
            changed = _push(timeline, stack,
                            commands.ZoomBaseClip(clip.id, scale), state) or changed
        if abs(x) > _EPS or abs(y) > _EPS:
            changed = _push(timeline, stack,
                            commands.MoveBaseClip(clip.id, x, y), state) or changed
    if changed:
        report.add("A4", where, schema.ACTION_CLAMP, "{0:g}".format(scale))


# ---- ⑤⑥ テロップ --------------------------------------------------------
#
# 本文は既存の文字起こしが正であり、AI には直すべきものだけを指示させる (§11.1 ⑤ / §19 の 6)。
# そのため「同じ場所に既存のテロップがあれば直す」「無ければ足す」の 2 通りになる。

def _apply_subtitles(timeline, stack, cfg, plan, indexes, report, state):
    scenes = {scene["id"]: scene for scene in plan.get("scenes") or []}
    for position, item in enumerate(plan.get("subtitles") or []):
        where = "subtitles[{0}]".format(position)
        if item["scene_id"] not in scenes:
            report.add("A5", where, schema.ACTION_DROP, "no scene")
            continue
        index = indexes.get(item["scene_id"])
        pieces = _source_pieces(timeline, index, item["source_start"],
                                item["source_end"])
        if not pieces:
            # 無音カットで消えた区間。置く場所が無い。
            report.add("A5", where, schema.ACTION_DROP, "cut")
            continue
        start, end = pieces[0]
        existing = _subtitle_at(timeline, start, end)
        if existing is None:
            _add_subtitle(timeline, stack, cfg, item, start, end - start,
                          report, where, state)
        else:
            _edit_subtitle(timeline, stack, cfg, item, existing, report, where, state)


def _add_subtitle(timeline, stack, cfg, item, start, duration, report, where, state):
    role = schema.model_track_role(item["track_role"])
    command = commands.AddSubtitleClip(
        start, duration, text=item["text"], role=role,
        min_clip_sec=cfg["min_clip_sec"], subtitle_tracks_cfg=cfg["subtitle_tracks"])
    if not _push(timeline, stack, command, state):
        report.add("A5", where, schema.ACTION_DROP, "no room")

        return
    report.add("A5", where, schema.ACTION_WRAP, "added")
    if command.created_clip_id:
        _push(timeline, stack,
              commands.EditSubtitle(command.created_clip_id, **_look_of(item)), state)


def _edit_subtitle(timeline, stack, cfg, item, clip, report, where, state):
    fields = _look_of(item)
    if item["text"] and item["text"] != clip.text:
        fields["text"] = item["text"]
    if _push(timeline, stack, commands.EditSubtitle(clip.id, **fields), state):
        report.add("A5", where, schema.ACTION_WRAP, "edited")
    role = schema.model_track_role(item["track_role"])
    if role != clip.role:
        _push(timeline, stack,
              commands.ChangeSubtitleRole(clip.id, role, cfg["subtitle_tracks"]), state)


# EditSubtitle へ渡す見た目の指定 (本文は別に扱う)。
def _look_of(item):
    return {"animation": item["animation"], "fade_in": bool(item["fade_in"]),
            "fade_out": bool(item["fade_out"]), "color": item["color"],
            "align": item["align"]}


# 指定の Timeline 区間と最も重なる既存テロップ。無ければ None。
def _subtitle_at(timeline, start, end):
    best = None
    best_overlap = 0.0
    for clip in timeline.subtitle_clips(only_used=False):
        overlap = min(end, clip.timeline_end) - max(start, clip.timeline_start)
        if overlap > best_overlap + _EPS:
            best = clip
            best_overlap = overlap

    return best if isinstance(best, SubtitleClip) else None


# ---- ⑦ SE ---------------------------------------------------------------

def _apply_sounds(timeline, stack, cfg, plan, indexes, settings, probe, report, state):
    if not cfg["sound_tracks"].get("enabled", True):
        return
    scenes = {scene["id"]: scene for scene in plan.get("scenes") or []}
    media_cfg = media_probe.media_config(settings)
    look = probe if probe is not None else (
        lambda path: media_probe.probe(path, timeline.next_id("m"), settings, media_cfg))

    for position, item in enumerate(plan.get("sounds") or []):
        where = "sounds[{0}]".format(position)
        scene = scenes.get(item["scene_id"])
        path = str(item.get("path") or "")
        if scene is None or not path:
            report.add("A7", where, schema.ACTION_DROP, "no scene/path")
            continue
        at = _source_to_timeline(timeline, indexes.get(item["scene_id"]),
                                 scene["source_start"] + item["offset_sec"])
        if at is None:
            report.add("A7", where, schema.ACTION_DROP, "cut")
            continue
        media = look(path)
        duration = float(getattr(media, "duration_sec", 0.0) or 0.0)
        if duration <= _EPS:
            report.add("A7", where, schema.ACTION_DROP, "no duration")
            continue

        command = commands.AddSoundClip(media, at, duration, cfg=cfg["sound_tracks"])
        if not _push(timeline, stack, command, state):
            report.add("A7", where, schema.ACTION_DROP, command.failure or "rejected")
            continue
        report.add("A7", where, schema.ACTION_WRAP, item["preset"])
        gain = float(item.get("gain_db") or 0.0)
        if command.created_clip_id and abs(gain) > _EPS:
            _push(timeline, stack,
                  commands.SetAudioGain(command.created_clip_id, gain), state)


# ---- ⑨ V15 適用後の検証 -------------------------------------------------
#
# project_io.validate は「直して続ける」実装であり例外を投げない。
# そのため**クリップが消えたか**を見て判定する。消えたなら plan の適用で
# 壊れた形になったということなので、積んだ手を全て戻して失敗にする。
# (素材のファイルが無いだけなら validate はクリップを無効化するだけであり、
#  巻き戻しの対象にはならない。書き出しまでに素材が揃えば有効へ戻る。)
def _validate_timeline(timeline, stack, cfg, report, state):
    before = _clip_ids(timeline)
    project_io.validate(timeline, cfg["min_clip_sec"])
    lost = before - _clip_ids(timeline)
    # include_disabled: 素材のファイルがまだ無い間は validate がクリップを
    # 無効化する (消さない)。それを「全部無くなった」と誤判定しない。
    if not lost and timeline.base_clips(include_disabled=True):
        return

    for _ in range(state["applied"]):
        stack.undo(timeline)
    state["applied"] = 0
    report.add("V15", "timeline", schema.ACTION_DROP, str(len(lost)))
    report.log()
    raise AiPlanError(tr("AI の編集を適用できなかったため元の状態へ戻しました。"))


def _clip_ids(timeline):
    return {clip.id for track in timeline.tracks for clip in track.clips}


# ---- 時間座標の橋渡し ----------------------------------------------------

# そのシーンの素材が始まる VOD 秒 (素材内の秒 + これ = VOD の秒)。
def _vod_offset(timeline, index):
    entry = clip_entry(timeline, index)
    if entry is None:
        return None
    media = archive_media_entry(timeline, str(entry.get("media_id") or ""))
    source = media if isinstance(media, dict) else entry

    return float(source.get("vod_start", 0.0))


# そのシーンの V1 クリップ (Timeline の順)。
def _scene_clips(timeline, index):
    track = timeline.base_video_track()
    if track is None or index is None:
        return []

    return sorted((clip for clip in track.clips
                   if _archive_index(clip) == int(index)),
                  key=lambda clip: clip.timeline_start)


def _archive_index(clip):
    value = (clip.origin or {}).get(ORIGIN_ARCHIVE_INDEX)

    return int(value) if isinstance(value, int) else None


# VOD の区間 → Timeline の区間列 (無音カットを跨ぐときは複数)。
def _source_pieces(timeline, index, vod_start, vod_end):
    offset = _vod_offset(timeline, index)
    clips = _scene_clips(timeline, index)
    if offset is None or not clips:
        return []
    time_map = TimeMap.from_clips(clips, body_media_id=clips[0].media_id)

    return time_map.split_interval(vod_start - offset, vod_end - offset,
                                   media_id=clips[0].media_id)


# VOD の時刻 → Timeline の時刻 (カットで消えていれば None)。
def _source_to_timeline(timeline, index, vod_sec):
    offset = _vod_offset(timeline, index)
    clips = _scene_clips(timeline, index)
    if offset is None or not clips:
        return None
    time_map = TimeMap.from_clips(clips, body_media_id=clips[0].media_id)

    return time_map.to_timeline(vod_sec - offset, media_id=clips[0].media_id)


def _sync_all(cfg):
    return str(cfg.get("ripple_sync_tracks", "all") or "all") != "same"
