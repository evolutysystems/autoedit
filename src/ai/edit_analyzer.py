# 人間の編集の統計 (docs/AiEditor/resolve1.md §13.1 / §13.2 の ① / R11)
#
# 入力は**保存済みプロジェクト JSON** である (§13.1)。新しいログ機構は要らない。
# ここは **AI を呼ばない**。分布・率・中央値だけを出すため、同じ入力なら必ず同じ結果になる。
# 言語化 (仮説) は ai/profile.py が AI に頼む (§13.2 の ②)。
#
# プロジェクト JSON は `project_io` を通さず**生の JSON として読む**。
#   ・`project_io.load` は Timeline を組み、素材の実在まで確認する。
#     統計を取るだけなら素材は要らず、素材が消えた古いプロジェクトも数えたい
#   ・形 (tracks[].clips[]) は `project_io.to_json` の約束であり、
#     読み方を 1 か所 (_clips_of) に閉じてある
import json
import os

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# トラックの種別 (timeline.model と同じ値。model を import せずに済ませる)
_KIND_VIDEO = "video"
_KIND_AUDIO = "audio"
_KIND_SUBTITLE = "subtitle"
_KIND_SOUND = "sound"

# 読める schema_version (project_io.SCHEMA_VERSION と同じ)
_SCHEMA_VERSION = 1


# プロジェクト 1 件の素の測定値。
def analyze_project(path_or_data):
    data = path_or_data
    if isinstance(path_or_data, str):
        data = _load(path_or_data)
    if not isinstance(data, dict) or int(data.get("schema_version", 0) or 0) != _SCHEMA_VERSION:
        return None

    video = _clips_of(data, _KIND_VIDEO)
    subtitles = _clips_of(data, _KIND_SUBTITLE)
    sounds = _clips_of(data, _KIND_SOUND)
    audio = _clips_of(data, _KIND_AUDIO)
    total = float((data.get("timeline") or {}).get("duration_sec", 0.0) or 0.0)

    return {
        "updated_at": str(data.get("updated_at", "") or ""),
        "duration_sec": total,
        "clip_sec": [float(clip.get("duration", 0.0) or 0.0) for clip in video],
        # カットで詰めた間 (素材内の隙間)。連なりから測る (§13.1)
        "cut_gap_sec": _cut_gaps(video),
        "subtitle_chars": [len(str(clip.get("text", "") or "").replace("\\N", ""))
                           for clip in subtitles],
        "subtitle_sec": [float(clip.get("duration", 0.0) or 0.0)
                         for clip in subtitles],
        "animations": [str(clip.get("animation", "") or "") for clip in subtitles],
        "fades": sum(1 for clip in subtitles
                     if clip.get("fade_in") or clip.get("fade_out")),
        "colored": sum(1 for clip in subtitles if str(clip.get("color", "") or "")),
        "font_sizes": [int(clip.get("font_size") or 0) for clip in subtitles
                       if clip.get("font_size")],
        "aligned": sum(1 for clip in subtitles if str(clip.get("align", "") or "")),
        "zoomed": sum(1 for clip in video
                      if abs(float((clip.get("transform") or {}).get("scale", 1.0)
                                   or 1.0) - 1.0) > 1e-6),
        "moved": sum(1 for clip in video
                     if abs(float((clip.get("transform") or {}).get("x", 0.0) or 0.0)) > 1e-6
                     or abs(float((clip.get("transform") or {}).get("y", 0.0) or 0.0)) > 1e-6),
        "sound_count": len(sounds),
        "sound_gains": [float(clip.get("gain_db", 0.0) or 0.0) for clip in sounds],
        "audio_gains": [float(clip.get("gain_db", 0.0) or 0.0) for clip in audio],
        "muted": sum(1 for clip in audio if clip.get("muted")),
        "blur": bool((data.get("source") or {}).get("blur")),
        # 先頭・末尾の尺の偏り (導入・締めの作り方 / §13.1 末尾)
        "head_sec": float(video[0].get("duration", 0.0) or 0.0) if video else 0.0,
        "tail_sec": float(video[-1].get("duration", 0.0) or 0.0) if video else 0.0,
        "longest_at": _longest_position(video),
    }


# 複数のプロジェクトをまとめて測る。返り値は {"sample", "stats"} (§13.3)。
def analyze_paths(paths, loader=None):
    load = loader if loader is not None else analyze_project
    measures = []
    for path in paths or []:
        try:
            measure = load(path)
        except Exception as e:  # noqa: BLE001 (1 件の破損で統計を止めない)
            _logger.warning("プロジェクトを測れませんでした (%s): %s", path, e)
            continue
        if measure:
            measures.append(measure)

    return summarize(measures)


# 測定値をプロファイルの stats へ畳む。
def summarize(measures):
    clip_sec = _flat(measures, "clip_sec")
    gaps = _flat(measures, "cut_gap_sec")
    chars = _flat(measures, "subtitle_chars")
    animations = _flat(measures, "animations")
    subtitle_count = len(animations)
    video_count = len(clip_sec)
    minutes = sum(float(m["duration_sec"]) for m in measures) / 60.0

    stamps = sorted(str(m["updated_at"]) for m in measures if m.get("updated_at"))
    sample = {"projects": len(measures), "clips": video_count,
              "subtitles": subtitle_count,
              "from": stamps[0][:10] if stamps else "",
              "to": stamps[-1][:10] if stamps else ""}

    with_animation = [value for value in animations if value]
    stats = {
        "clip_sec": _spread(clip_sec),
        "cut_gap_sec": {"median": _percentile(gaps, 0.5)},
        "subtitle_chars": {"median": _percentile(chars, 0.5),
                           "p90": _percentile(chars, 0.9)},
        "subtitle_sec": _spread(_flat(measures, "subtitle_sec")),
        "animation_rate": _rate(len(with_animation), subtitle_count),
        "animation_top": _top(with_animation, subtitle_count),
        "fade_rate": _rate(sum(m["fades"] for m in measures), subtitle_count),
        "color_rate": _rate(sum(m["colored"] for m in measures), subtitle_count),
        "align_rate": _rate(sum(m["aligned"] for m in measures), subtitle_count),
        "font_size_median": _percentile(_flat(measures, "font_sizes"), 0.5),
        "zoom_rate": _rate(sum(m["zoomed"] for m in measures), video_count),
        "move_rate": _rate(sum(m["moved"] for m in measures), video_count),
        "se_per_min": (round(sum(m["sound_count"] for m in measures) / minutes, 2)
                       if minutes > 0 else 0.0),
        "se_gain_median": _percentile(_flat(measures, "sound_gains"), 0.5),
        "audio_gain_median": _percentile(_flat(measures, "audio_gains"), 0.5),
        "mute_rate": _rate(sum(m["muted"] for m in measures), video_count),
        "blur_rate": _rate(sum(1 for m in measures if m["blur"]), len(measures)),
        "head_sec_median": _percentile([m["head_sec"] for m in measures], 0.5),
        "tail_sec_median": _percentile([m["tail_sec"] for m in measures], 0.5),
        "longest_at_median": _percentile(
            [m["longest_at"] for m in measures if m["longest_at"] is not None], 0.5),
    }

    return {"sample": sample, "stats": stats}


# ---- 読み出し ------------------------------------------------------------

def _load(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _clips_of(data, kind):
    clips = []
    for track in data.get("tracks") or []:
        if not isinstance(track, dict) or str(track.get("kind")) != kind:
            continue
        for clip in track.get("clips") or []:
            if isinstance(clip, dict):
                clips.append(clip)
    clips.sort(key=lambda clip: float(clip.get("timeline_start", 0.0) or 0.0))

    return clips


# 素材内の隙間 (= カットで取り除いた長さ)。同じ素材の連なりだけを見る。
def _cut_gaps(video):
    gaps = []
    previous = None
    for clip in video:
        if previous is not None and clip.get("media_id") == previous.get("media_id"):
            gap = (float(clip.get("source_in", 0.0) or 0.0)
                   - float(previous.get("source_out", 0.0) or 0.0))
            if gap > 1e-6:
                gaps.append(round(gap, 3))
        previous = clip

    return gaps


# 最長クリップが全体のどこにあるか (0.0 = 先頭 / 1.0 = 末尾)。
def _longest_position(video):
    if not video:
        return None
    longest = max(range(len(video)),
                  key=lambda index: float(video[index].get("duration", 0.0) or 0.0))

    return round(longest / max(len(video) - 1, 1), 3)


# ---- 統計の小物 ----------------------------------------------------------

def _flat(measures, key):
    values = []
    for measure in measures:
        values.extend(measure.get(key) or [])

    return values


def _spread(values):
    return {"median": _percentile(values, 0.5), "p25": _percentile(values, 0.25),
            "p75": _percentile(values, 0.75)}


# 線形補間なしの単純な分位点 (再現性を優先する)。
def _percentile(values, ratio):
    numbers = sorted(float(value) for value in values
                     if isinstance(value, (int, float)))
    if not numbers:
        return 0.0
    index = min(int(round(ratio * (len(numbers) - 1))), len(numbers) - 1)

    return round(numbers[index], 3)


def _rate(count, total):
    return round(count / total, 3) if total else 0.0


# 使われた動きの上位 ([[種類, 率], …])。
def _top(values, total, limit=3):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))

    return [[name, _rate(count, total)] for name, count in ordered[:limit]]
