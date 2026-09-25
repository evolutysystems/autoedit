# 横 Timeline から縦 Timeline を組み立てる (ver5 resolve9 §5.4)
#
# 選んだクリップだけを 0 秒起点へ詰め直し、縦キャンバス (既定 1080x1920) の
# **クリップ用プロジェクト**にする。素材は元動画をそのまま参照し、切り抜きは
# 指定 (source["crop"]) として持たせる。切り抜き済みの動画は作らない (§3.1)。
#
# 元の Timeline は読むだけで、変更しない (§4-5)。
import copy

from ..modules import output_profile
from ..utils.logger import get_logger
from . import crop
from .model import (
    BASE_AUDIO_TRACK_ID,
    BASE_SUBTITLE_TRACK_ID,
    BASE_VIDEO_TRACK_ID,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Timeline,
    Track,
)

_logger = get_logger(__name__)

# 切り詰めた結果これより短くなった字幕は捨てる (見えないため)
_MIN_SUBTITLE_SEC = 0.05


# 縦プロジェクト用の Timeline を作る
#   timeline   : 元 (横) の Timeline
#   clip_ids   : 選んだベース (V1) クリップの ID
#   layout     : crop.make_layout() が作った切り抜きの指定 (None 可)
#   settings   : setting.json
#   close_gaps : True なら選んだクリップ間の空白を詰める
# 戻り値: (縦 Timeline, 警告メッセージの一覧)
def build(timeline, clip_ids, layout, settings, close_gaps=True):
    clips = _target_clips(timeline, clip_ids)
    if not clips:
        raise ValueError("縦動画にするクリップが選ばれていません")

    profile = output_profile._portrait_profile((settings or {}).get("vertical", {}))
    vertical = Timeline(
        fps=timeline.fps,
        width=profile["width"],
        height=profile["height"],
        orientation=profile["orientation"],
        zoom_px_per_sec=timeline.zoom_px_per_sec,
    )

    video_track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    audio_track = Track(BASE_AUDIO_TRACK_ID, TRACK_AUDIO, 1, name="Audio 1",
                        link_track=BASE_VIDEO_TRACK_ID)
    vertical.tracks = [video_track, audio_track]

    # ── ① 映像と音声 (0 秒起点へ詰め直す / R7 / R9)
    offsets = []                 # (元の開始, 新しい開始) 選択区間の対応表 (字幕の移送に使う)
    cursor = 0.0
    base_start = clips[0].timeline_start
    for clip in clips:
        new_clip = copy.deepcopy(clip)
        new_clip.timeline_start = (cursor if close_gaps
                                   else clip.timeline_start - base_start)
        video_track.clips.append(new_clip)
        offsets.append((clip.timeline_start, new_clip.timeline_start, clip.duration))
        cursor = new_clip.timeline_start + new_clip.duration

        audio = timeline.audio_clip_for(clip.id)
        if audio is not None:
            audio_track.clips.append(
                AudioClip(audio.id, new_clip.id, audio.gain_db, audio.muted))

    # ── ② 素材 (参照されるものだけ / R8)
    used = {clip.media_id for clip in video_track.clips if clip.media_id}
    vertical.media_pool = [copy.deepcopy(media) for media in timeline.media_pool
                           if media.id in used]

    # ── ③ 字幕 (重なるものを時刻シフト / R10)
    warnings = []
    _copy_subtitles(timeline, vertical, offsets, warnings)

    # ── ④ 引き継がないもの (§3.6 / §5.4-10)
    _warn_dropped(timeline, clips, warnings)

    # ── ⑤ source (「続きから」で開けるようにする / R14)
    source = dict(timeline.source or {})
    source.pop("archive", None)          # クリップ用として開かせる (§3.7)
    source.pop(crop.KEY, None)
    # ぼかしの指定は座標の基準が変わるため引き継がない (警告は _warn_dropped が積む)
    source.pop("blur", None)
    vertical.source = source
    if layout is not None:
        crop.store(vertical, layout)

    _logger.info("縦プロジェクトを作成: %d クリップ / 合計 %.2fs (%dx%d)",
                 len(video_track.clips), vertical.duration_sec(),
                 vertical.width, vertical.height)
    return vertical, warnings


# 縦にできるクリップか (ベース V1 の素材付きクリップだけ)
def target_clips(timeline, clip_ids):
    return _target_clips(timeline, clip_ids)


def _target_clips(timeline, clip_ids):
    base = timeline.base_video_track()
    if base is None:
        return []

    wanted = set(clip_ids or [])
    clips = [clip for clip in base.clips
             if clip.id in wanted and clip.enabled
             and not clip.is_opening_or_ending() and getattr(clip, "media_id", "")]
    return sorted(clips, key=lambda c: c.timeline_start)


# 選択区間に重なる字幕を、同じ時間差で移す。はみ出しは端で切り詰める。
def _copy_subtitles(timeline, vertical, offsets, warnings):
    subtitle_track = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    vertical.tracks.append(subtitle_track)

    dropped = 0
    for track in timeline.subtitle_tracks():
        for clip in track.clips:
            start = clip.timeline_start
            end = start + clip.duration
            for old_start, new_start, duration in offsets:
                overlap_start = max(start, old_start)
                overlap_end = min(end, old_start + duration)
                if overlap_end - overlap_start < _MIN_SUBTITLE_SEC:
                    continue
                moved = copy.deepcopy(clip)
                moved.timeline_start = new_start + (overlap_start - old_start)
                moved.duration = overlap_end - overlap_start
                subtitle_track.clips.append(moved)
                break
            else:
                if clip.use:
                    dropped += 1

    subtitle_track.clips.sort(key=lambda c: c.timeline_start)
    _renumber(vertical, subtitle_track)
    if dropped:
        warnings.append(f"選んだ範囲の外にある字幕 {dropped} 件は引き継ぎませんでした。")


# 同じ ID が重なった字幕に新しい ID を振る (1 つの字幕が複数区間へまたがった場合)
def _renumber(vertical, track):
    seen = set()
    for clip in track.clips:
        if clip.id in seen:
            clip.id = vertical.next_id("s")
        seen.add(clip.id)


# 引き継がないものを知らせる (黙って落とさない / §3.6)
def _warn_dropped(timeline, clips, warnings):
    span = [(c.timeline_start, c.timeline_start + c.duration) for c in clips]

    overlays = 0
    base = timeline.base_video_track()
    for track in timeline.video_tracks():
        if base is not None and track.id == base.id:
            continue
        for clip in track.clips:
            start = clip.timeline_start
            end = start + clip.duration
            if any(start < e and end > s for s, e in span):
                overlays += 1
    if overlays:
        warnings.append(
            f"オーバーレイ素材 {overlays} 件は引き継ぎませんでした "
            "(横向きの位置・大きさが縦では合わないため)。")

    if (timeline.source or {}).get("blur"):
        warnings.append("ぼかしの指定は引き継ぎませんでした (切り抜き後の座標が変わるため)。")
