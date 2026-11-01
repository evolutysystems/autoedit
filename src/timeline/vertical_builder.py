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
import os
import shutil

from ..i18n import tr
from ..modules import output_profile
from ..utils.logger import get_logger
from . import crop
from . import project_io
from .builder import timeline_config
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

# クリップの境目は縫い目とみなす許容差 (秒 / ver9 resolve15 §3.5)。
# フレーム量子化で 1 フレーム未満のずれ・重なりが残るため、
# project_io._OVERLAP_TOLERANCE_SEC と同じ値で「繋がっている」とみなす。
_SEAM_TOLERANCE_SEC = 0.0015


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
    # トラックの構成 (本数・色) と Timeline 上の配置をそのまま引き継ぐ
    # (ver9 resolve15)。設定で従来どおり S1 の 1 本へ潰すこともできる。
    _copy_subtitles(timeline, vertical, offsets, warnings,
                    carry_tracks=crop.config(settings)["carry_subtitle_tracks"])

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
# crop.is_valid が見るのは**寸法の一致だけ**で、素材 ID は合否にしない
# (ver9 resolve16 §3.1)。そのため ID の向け直しは必須ではないが、
# 「どの素材の絵で枠を決めたか」の控えを張り替え後の素材へ合わせておく
# (控えが実在しない素材を指したままになるのを避ける)。
# 寸法が変わっている場合は換算で当てることになるため、ここでは指定を入れない。
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


# 元のテロップトラック構成 (本数・名前・ヘッダーで決めた色) をそのまま複製する
# (ver9 resolve15 §3.1)
#
# 1 本へ潰していたため、(a) トラックの既定色 (ver9 resolve13) が消える
# (b) S1 と S2 のテロップが同じトラックで重なり、開き直すときの
# project_io._validate_overlaps が後ろへ押し出す — の 2 つが起きていた。
# アーカイブの書き出し (archive/timeline_builder._build_group_timeline) と同じ流儀。
#
# 中身が空のトラックも作る (「トラック数を引き継ぐ」= 空も数える)。
# locked は写さない: 作りたてのプロジェクトで編集を止める理由は引き継がれない。
# carry=False のときは従来どおり S1 を 1 本だけ作る。
# 戻り値: 元のトラック ID → 縦 Timeline の対応トラック
def _copy_subtitle_tracks(timeline, vertical, carry=True):
    mapping = {}
    if carry:
        for track in sorted(timeline.subtitle_tracks(), key=lambda t: t.index):
            copied = Track(track.id, TRACK_SUBTITLE, track.index, name=track.name,
                           enabled=track.enabled, color=track.color)
            vertical.tracks.append(copied)
            mapping[track.id] = copied
    if not mapping:
        # 字幕トラックが 1 本も無い横 Timeline (従来どおり S1 を用意する)
        base = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
        vertical.tracks.append(base)
        mapping[base.id] = base
    return mapping


# テロップ 1 本を縦 Timeline の時刻へ写す (ver9 resolve15 §3.5)
#
# 従来は「最初に重なったクリップ」だけを採ってその端で打ち切っていたため、
# 無音カットの境目をまたぐテロップが尻切れになっていた。重なったクリップぶんの
# 欠片をすべて作り、縦 Timeline 上で隣り合うものを 1 本へ結合する。詰めても
# 繋がっている区間では元の尺がそのまま残り、選んでいない区間を挟んだところで
# だけ 2 本に分かれる。
#
# 欠片の時刻はクリップごとの差分 (new - old) で写す。区間の先頭の差分で
# まとめて写すと、クリップ間に残る 1 フレーム未満のずれが積もって終端がずれる。
# 戻り値: [(開始, 終了), ...] (縦 Timeline の時刻 / 時系列順)
def _map_spans(start, end, offsets):
    pieces = []
    for old_start, new_start, duration in offsets:          # 時系列順
        overlap_start = max(start, old_start)
        overlap_end = min(end, old_start + duration)
        if overlap_end <= overlap_start:
            continue
        shift = new_start - old_start
        piece = [overlap_start + shift, overlap_end + shift]
        if pieces and piece[0] - pieces[-1][1] <= _SEAM_TOLERANCE_SEC:
            pieces[-1][1] = max(pieces[-1][1], piece[1])    # 縫い目なので繋ぐ
        else:
            pieces.append(piece)
    # 短すぎる欠片は捨てる。判定は結合したあとの 1 本に対して行う
    # (繋ぐ前に捨てると、またいだテロップに穴が開く)。
    return [(a, b) for a, b in pieces if b - a >= _MIN_SUBTITLE_SEC]


# 選択区間に重なる字幕を、元のトラックへ元の配置のまま移す。
# 選んでいない区間にかかる部分だけを切り詰める (ver9 resolve15 §3.2 / §3.5)。
def _copy_subtitles(timeline, vertical, offsets, warnings, carry_tracks=True):
    tracks = _copy_subtitle_tracks(timeline, vertical, carry_tracks)
    fallback = min(tracks.values(), key=lambda t: t.index)

    dropped = trimmed = 0
    for track in timeline.subtitle_tracks():
        # 元のトラックに対応する側へ入れる (役割ごとの重なりを保つ)
        target = tracks.get(track.id, fallback)
        for clip in track.clips:
            spans = _map_spans(clip.timeline_start,
                               clip.timeline_start + clip.duration, offsets)
            if not spans:
                if clip.use:
                    dropped += 1
                continue
            carried = sum(end - start for start, end in spans)
            if clip.use and carried < clip.duration - _MIN_SUBTITLE_SEC:
                trimmed += 1
            for span_start, span_end in spans:
                moved = copy.deepcopy(clip)
                moved.timeline_start = span_start
                moved.duration = span_end - span_start
                target.clips.append(moved)

    for target in tracks.values():
        target.clips.sort(key=lambda c: c.timeline_start)
        _renumber(vertical, target)
    if dropped:
        warnings.append(f"選んだ範囲の外にある字幕 {dropped} 件は引き継ぎませんでした。")
    if trimmed:
        warnings.append(
            tr("選んだ範囲の境目にかかる字幕 {count} 件は、はみ出した部分を切り詰めました。",
               count=trimmed))


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


# ==================================================================
# 素材の複製 (ver9 resolve14 §3.1)
# ==================================================================
#
# 縦プロジェクトが参照する素材は、アーカイブ用だと
#   ・未保存   … 実行の終わりに消える一時ファイル
#   ・保存済み … 元プロジェクトの <プロジェクト>.media/
# のどちらかにある。どちらも「縦プロジェクトだけを残す」と開けなくなるため、
# 作成時に縦プロジェクトの隣へ写して自己完結させる。
#
# 元 VOD 1 本へ張り替える従来の方式 (vod_rebase) は、元 VOD が既定で
# 自動整理されるため同じ問題を抱えていた (resolve14 §1.4)。


# 複製先のファイル名。素材 ID を前に付けて衝突を防ぐ
# (アーカイブ用は全クリップが normalized.mp4 という同名のため / resolve9 §5.4)。
def _media_dest_name(media):
    return f"{media.id}_{os.path.basename(media.path)}"


# 縦プロジェクトが参照する素材を <プロジェクト>.media/ へ複製して自己完結させる
#
# timeline     : build() が作った縦 Timeline (media_pool は deepcopy 済みのため
#                パスを書き換えても元の Timeline は変わらない)
# project_path : 保存先のプロジェクトファイル
# skip_paths   : 複製しない素材のパス (クリップ用の元動画など、消えないもの)
# warnings     : 複製できなかったものを積む一覧 (作成そのものは続ける)
# 戻り値: (複製した件数, 複製したバイト数)
def copy_media_beside(timeline, project_path, settings, skip_paths=(),
                      warnings=None, progress_cb=None):
    cfg = timeline_config(settings)
    folder = project_io.media_dir_path(
        project_path, cfg["project_suffix"], cfg["project"]["media_dir_suffix"])
    if not folder:
        return 0, 0
    skip = {os.path.normcase(os.path.abspath(path)) for path in (skip_paths or []) if path}
    source = dict(timeline.source or {})
    body_id = str(source.get("media_id", "") or "")
    copied, total = 0, 0

    for media in timeline.media_pool:
        path = str(getattr(media, "path", "") or "")
        if not path or os.path.normcase(os.path.abspath(path)) in skip:
            continue
        if not os.path.exists(path):
            # 既に消えている素材。ここで止めず、開けない可能性として伝える
            if warnings is not None:
                warnings.append(
                    tr("素材が見つからないため複製できませんでした: {name}",
                       name=os.path.basename(path)))
            continue
        dest = os.path.join(folder, _media_dest_name(media))
        try:
            os.makedirs(folder, exist_ok=True)
            # 同じ内容が既にあるなら複製し直さない (作るたびに数百 MB を書かない)
            if not (os.path.exists(dest)
                    and os.path.getsize(dest) == os.path.getsize(path)):
                if progress_cb:
                    progress_cb(tr("素材を複製しています…"))
                shutil.copy2(path, dest)
                copied += 1
                total += os.path.getsize(dest)
        except OSError:
            # 複製に失敗しても作成は続ける (元のパスを指したまま保存する)
            _logger.exception("縦プロジェクトへの素材の複製に失敗しました: %s", path)
            if warnings is not None:
                warnings.append(
                    tr("素材を複製できませんでした: {name}",
                       name=os.path.basename(path)))
            continue
        media.path = dest
        if media.id == body_id:
            source["media_path"] = dest
            timeline.source = source

    if copied:
        _logger.info("縦プロジェクトの素材を %d 件複製しました (%.1f MB): %s",
                     copied, total / (1024 * 1024), folder)
    return copied, total
