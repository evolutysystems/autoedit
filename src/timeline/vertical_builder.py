# 横 Timeline から縦 Timeline を組み立てる (ver5 resolve9 §5.4)
#
# 選んだクリップだけを 0 秒起点へ詰め直し、縦キャンバス (既定 1080x1920) の
# **クリップ用プロジェクト**にする。素材は元動画をそのまま参照し、切り抜きは
# 指定 (source["crop"]) として持たせる。切り抜き済みの動画は作らない (§3.1)。
#
# アーカイブ切り抜き用の Timeline からも作れる (ver5 resolve10)。
# その場合は呼び出し側が rebase を渡し、消える中間ファイルの代わりに
# 元 VOD を参照させる。張り替えの中身は archive/vod_rebase.py が決める
# (timeline 層が archive 層を import しないための構造 / media_recovery と同じ流儀)。
#
# 元の Timeline は読むだけで、変更しない (§4-5)。
import copy

from ..i18n import tr
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
#   rebase     : 素材を別の 1 本へ張り替える指定 (None 可 / archive.vod_rebase.plan)
#                {"media": MediaRef,           張り替え先 (元 VOD)
#                 "offsets": {素材ID: 秒},      素材内の秒へ足すと張り替え先の秒になる
#                 "source": {キー: 値},         source へ上書きするもの
#                 "warnings": [文言, ...]}
# 戻り値: (縦 Timeline, 警告メッセージの一覧)
def build(timeline, clip_ids, layout, settings, close_gaps=True, rebase=None):
    clips = _target_clips(timeline, clip_ids)
    if not clips:
        raise ValueError(tr("縦動画にするクリップが選ばれていません"))

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

    # ── ② 素材 (参照されるものだけ / R8)。
    # アーカイブ用は消える中間ファイルを指しているため、ここで元 VOD へ張り替える。
    warnings = []
    replacement = _apply_rebase(timeline, video_track, rebase, warnings)
    used = {clip.media_id for clip in video_track.clips if clip.media_id}
    vertical.media_pool = [copy.deepcopy(media) for media in timeline.media_pool
                           if media.id in used]
    if replacement is not None:
        vertical.media_pool.insert(0, replacement)

    # ── ③ 字幕 (重なるものを時刻シフト / R10)
    _copy_subtitles(timeline, vertical, offsets, warnings)

    # ── ④ 引き継がないもの (§3.6 / §5.4-10)
    _warn_dropped(timeline, clips, warnings)

    # ── ⑤ source (「続きから」で開けるようにする / R14)
    source = dict(timeline.source or {})
    source.pop("archive", None)          # クリップ用として開かせる (§3.7)
    source.pop(crop.KEY, None)
    # ぼかしの指定は座標の基準が変わるため引き継がない (警告は _warn_dropped が積む)
    source.pop("blur", None)
    if replacement is not None:
        source.update(rebase.get("source") or {})
        source["media_id"] = replacement.id
        layout = _rebase_layout(layout, rebase, replacement, warnings)
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


# 素材を別の 1 本 (元 VOD) へ張り替える (ver5 resolve10 §5.3)
#
# クリップの素材内時刻へ offsets の秒を足し、参照先を張り替え先の素材へ向ける。
# 張り替え先に控えの無い素材 (D&D で足した動画・画像など) は触らない。
# 戻り値: 縦プロジェクトへ入れる張り替え先の MediaRef / 張り替えないなら None
def _apply_rebase(timeline, video_track, rebase, warnings):
    if not rebase:
        return None
    media = rebase.get("media")
    offsets = {str(key): float(value)
               for key, value in (rebase.get("offsets") or {}).items()}
    if media is None or not offsets:
        return None

    replacement = copy.deepcopy(media)
    # 張り替えない素材と ID がぶつからないようにする
    taken = {item.id for item in timeline.media_pool if item.id not in offsets}
    if replacement.id in taken or not replacement.id:
        base = replacement.id or "vod"
        index = 2
        while f"{base}{index}" in taken:
            index += 1
        replacement.id = f"{base}{index}"

    moved = 0
    for clip in video_track.clips:
        offset = offsets.get(str(clip.media_id))
        if offset is None:
            continue
        clip.source_in += offset
        clip.source_out += offset
        clip.media_id = replacement.id
        moved += 1
    if not moved:
        return None

    warnings.extend(str(text) for text in (rebase.get("warnings") or []))
    _logger.info("素材を張り替えました: %d クリップ → %s", moved, replacement.path)
    return replacement


# 切り抜きの指定が指す素材を張り替え先へ向け直す
#
# crop.is_valid は素材 ID と寸法の一致を見るため、張り替えたまま放っておくと
# 「指定が噛み合わない」として切り抜きごと捨てられてしまう (§5.5)。
def _rebase_layout(layout, rebase, replacement, warnings):
    if layout is None:
        return None
    offsets = rebase.get("offsets") or {}
    if str(layout.get("media_id") or "") not in offsets:
        return layout

    layout = copy.deepcopy(layout)
    layout["media_id"] = replacement.id
    source = layout.get("source") or []
    size = (int(getattr(replacement, "width", 0) or 0),
            int(getattr(replacement, "height", 0) or 0))
    if len(source) == 2 and (int(source[0] or 0), int(source[1] or 0)) != size:
        # 中間ファイルは VOD のストリームコピーのため、通常はここへ来ない。
        warnings.append(
            "元 VOD の解像度が編集中の素材と違うため、切り抜きの指定は入れませんでした。")
        return None
    return layout


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
