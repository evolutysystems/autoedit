# Timeline 構築 (docs/request/ver3/resolve.md §6.9 / §7)
# 無音カットの編集点 (元動画時間) と音声認識結果から Timeline を組み立てる。
#   V1: オープニング → 本編 (残す区間) → エンディング
#   A1: V1 にリンクした音声 (時刻は持たない / R18)
#   S1: 字幕クリップ (Timeline 時間。OP の尺ぶんだけ後ろへずらす)
# OP/ED の配置条件は現行 concat_processor.run() と完全に同一にして、
# 「従来と同じ条件のときに、従来と同じ素材が同じ位置に入る」ことを保証する (§6.9-1)。
import os

from ..modules import concat_processor
from ..utils.logger import get_logger
from . import media_probe
from .model import (
    BASE_AUDIO_TRACK_ID,
    BASE_SUBTITLE_TRACK_ID,
    BASE_VIDEO_TRACK_ID,
    BASE_Z_ORDER,
    DEFAULT_ROLE,
    DEFAULT_SUBTITLE_Z_ORDER,
    ORIGIN_ASR,
    ORIGIN_ENDING,
    ORIGIN_OPENING,
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    SubtitleClip,
    Timeline,
    Track,
)
from .timemap import TimeMap

_logger = get_logger(__name__)

# 極短の区間は Timeline に載せない (既存 build_keep_segments と同じ 0.01 秒)
_MIN_SEGMENT_SEC = 0.01


# キー割り当ての既定 (ver3 resolve2 §6)
# S には何も割り当てない (回答 Q1) ため項目自体を置かない。
_DEFAULT_SHORTCUTS = {
    "split": "W",
    "ripple_trim_before": "A",
    "ripple_trim_after": "D",
    "play_pause": "Space",
    "play_fast_forward": "E",
    "play_fast_backward": "Q",
    "split_alt": "Ctrl+B",
    "delete": "Delete",
    "delete_alternate": "Shift+Delete",
    # 選択中のノードを削除するだけ (後続を詰めない / resolve2 R12)
    "delete_plain": "Backspace",
    "undo": "Ctrl+Z",
    "redo": "Ctrl+Y",
    "redo_alt": "Ctrl+Shift+Z",
    "step_backward": "Left",
    "step_forward": "Right",
    "go_start": "Home",
    "go_end": "End",
    "zoom_in": "Ctrl++",
    "zoom_in_alt": "Ctrl+=",
    "zoom_out": "Ctrl+-",
}

# キー割り当てを既定で補完する (欠落キーは既定へ戻す)
def _shortcut_config(values):
    merged = dict(_DEFAULT_SHORTCUTS)
    if isinstance(values, dict):
        for key, value in values.items():
            if key in merged:
                merged[key] = str(value or "")
    return merged


# 正の float を返す (0 以下・不正値は既定へ寄せて警告する)
def _positive_float(value, default):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result <= 0:
        _logger.warning("再生速度の設定が不正なため既定 %.1f を使用します: %r", default, value)
        return default
    return result


# 正の int を返す (0 以下・不正値は既定へ寄せて警告する / ver3 resolve4 §7)
# ボタン幅・線の太さのように「0 では成立しない」寸法に使う。
def _positive_int(value, default):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if result <= 0:
        _logger.warning("寸法の設定が不正なため既定 %d を使用します: %r", default, value)
        return default
    return result


# timeline セクションの設定を既定値で補完して返す (§9)
def timeline_config(settings):
    cfg = (settings or {}).get("timeline", {}) or {}
    ui = cfg.get("ui", {}) or {}
    preview = cfg.get("preview", {}) or {}
    render = cfg.get("render", {}) or {}
    opening_ending = cfg.get("opening_ending", {}) or {}
    waveform = cfg.get("waveform", {}) or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "project_dir": str(cfg.get("project_dir", "") or ""),
        "project_suffix": str(cfg.get("project_suffix", ".timeline.json") or ".timeline.json"),
        "autosave_sec": int(cfg.get("autosave_sec", 0) or 0),
        "keep_project_file": bool(cfg.get("keep_project_file", True)),
        "auto_place_opening_ending": bool(opening_ending.get("auto_place", True)),
        "min_clip_sec": float(cfg.get("min_clip_sec", 0.05)),
        # 右クリック「字幕追加」で置く字幕の既定の尺 (秒)
        "default_subtitle_sec": _positive_float(cfg.get("default_subtitle_sec"), 2.0),
        # Delete キー単独の割り当て (ver3 resolve2 R7)。true=リップル削除 (既定)
        "ripple_delete": bool(cfg.get("ripple_delete", True)),
        # リップルで一緒に詰める対象 (resolve2 R8)。"all"=全トラック (既定) / "same"=同一のみ
        "ripple_sync_tracks": str(cfg.get("ripple_sync_tracks", "all") or "all"),
        "snap_enabled": bool(cfg.get("snap_enabled", True)),
        # 再生ヘッドを編集点へ吸着させる (resolve2 R1)
        "snap_playhead": bool(cfg.get("snap_playhead", True)),
        "snap_threshold_px": int(cfg.get("snap_threshold_px", 8)),
        # キー割り当て (resolve2 §6)。空文字で無効。S は割り当てない (回答 Q1)
        "shortcuts": _shortcut_config(cfg.get("shortcuts", {})),
        "default_zoom_px_per_sec": float(cfg.get("default_zoom_px_per_sec", 40)),
        "zoom_min_px_per_sec": float(cfg.get("zoom_min_px_per_sec", 2)),
        "zoom_max_px_per_sec": float(cfg.get("zoom_max_px_per_sec", 400)),
        "ui": {
            "split_ratio": float(ui.get("split_ratio", 0.55)),
            "track_height_px": int(ui.get("track_height_px", 56)),
            "subtitle_track_height_px": int(ui.get("subtitle_track_height_px", 40)),
            "header_width_px": int(ui.get("header_width_px", 88)),
            "trim_handle_px": int(ui.get("trim_handle_px", 6)),
            "ruler_min_label_px": int(ui.get("ruler_min_label_px", 60)),
            "window_width": int(ui.get("window_width", 1280)),
            "window_height": int(ui.get("window_height", 820)),
            # Timeline 左下のズームボタンの幅 (ver3 resolve4 E4)。
            # テーマの QSS が左右に余白を持つため、記号が見切れない幅が要る。
            "zoom_button_width_px": _positive_int(ui.get("zoom_button_width_px"), 40),
            # クリップのアウトラインの太さ (ver3 resolve4 E5)
            "clip_outline_width_px": _positive_int(ui.get("clip_outline_width_px"), 2),
            "clip_outline_selected_width_px": _positive_int(
                ui.get("clip_outline_selected_width_px"), 3),
        },
        # 音声クリップの波形表示 (X = 時間 / Y = 音量)
        "waveform": {
            "enabled": bool(waveform.get("enabled", True)),
            # 解析用にデコードする音声のサンプリングレート (Hz)。
            # 表示用の包絡線を作るだけなので低くてよく、低いほど取得が速い。
            "sample_rate": _positive_int(waveform.get("sample_rate"), 8000),
            # 1 秒あたりのバケツ数 = 波形の時間解像度。
            # 100 なら 10ms ごとの音量になり、最大ズームでも階段が見えない。
            "resolution_hz": _positive_int(waveform.get("resolution_hz"), 100),
            # 縦軸の取り方。"linear"=振幅そのまま (DaVinci の既定と同じ見え方) /
            # "log"=dB 目盛り (小さい音も見えるが、うねりは平坦になる)
            "scale": ("log" if str(waveform.get("scale", "linear")).strip().lower() == "log"
                      else "linear"),
            # "log" のときに底とする音量 (dB)。これ以下は高さ 0 になる。
            "db_floor": float(waveform.get("db_floor", -48.0) or -48.0),
        },
        "preview": {
            "backend": str(preview.get("backend", "pyav") or "pyav"),
            "width": int(preview.get("width", 960)),
            "update_debounce_ms": int(preview.get("update_debounce_ms", 60)),
            "cache_frames": int(preview.get("cache_frames", 8)),
            "high_quality_button": bool(preview.get("high_quality_button", True)),
            "audio_enabled": bool(preview.get("audio_enabled", True)),
            "audio_volume": float(preview.get("audio_volume", 0.8)),
            "audio_chunk_sec": float(preview.get("audio_chunk_sec", 30)),
            "audio_prefetch_sec": float(preview.get("audio_prefetch_sec", 8)),
            "play_fps": int(preview.get("play_fps", 15)),
            # 倍速再生・倍速逆再生 (resolve2 R10・R11)
            "playback_rate": _positive_float(preview.get("playback_rate"), 2.0),
            "reverse_play_fps": int(preview.get("reverse_play_fps", 8) or 8),
            # トランスポートのボタン幅 (ver3 resolve4 E2)。
            # 2 記号のボタン (◀◀ ▶▶) だけ広い値を使う。
            "transport_button_width_px": _positive_int(
                preview.get("transport_button_width_px"), 40),
            "transport_wide_button_width_px": _positive_int(
                preview.get("transport_wide_button_width_px"), 52),
            # 「高精度プレビュー」ボタンの右側の余白 (ver3 resolve4 E3)。
            # 0 を許すため下限は 0 とする。
            "transport_row_right_margin_px": max(
                0, int(preview.get("transport_row_right_margin_px", 8) or 0)),
        },
        "render": {
            "gap_policy": str(render.get("gap_policy", "black") or "black"),
            "extract_mode": str(render.get("extract_mode", "seek") or "seek"),
            "concat_reencode": bool(render.get("concat_reencode", False)),
            "overlay_enabled": bool(render.get("overlay_enabled", True)),
        },
        "media": media_probe.media_config(settings),
    }


# ------------------------------------------------------------------
# OP / ED の配置判定 (§6.9-1)
# ------------------------------------------------------------------

# 配置対象の OP/ED 素材パスを返す (配置しない場合は None)
# 判定条件は concat_processor.run() と同一にする:
#   縦動画 → 付けない / *_enabled=false → 付けない / 素材が無い → 付けない
def resolve_material(settings, profile, position, cfg=None):
    cfg = cfg or timeline_config(settings)
    if not cfg["auto_place_opening_ending"]:
        return None

    label = "オープニング" if position == "opening" else "エンディング"

    # 縦動画では OP/ED を付けない (request14 §8-2 / concat_processor.py:97)
    if profile and profile.get("is_portrait"):
        _logger.info("%s配置スキップ (縦動画のため)", label)
        return None

    general = (settings or {}).get("general", {})
    if position == "opening":
        material = general.get("opening_video", "")
        enabled = general.get("opening_enabled", True)
    else:
        material = general.get("ending_video", "")
        enabled = general.get("ending_enabled", True)

    if not enabled:
        _logger.info("%s配置スキップ (フラグ無効)", label)
        return None
    # 素材の実在確認は既存 concat_processor.is_available をそのまま使う
    if not concat_processor.is_available(material):
        _logger.info("%s配置スキップ (素材未設定)", label)
        return None
    return material


# ------------------------------------------------------------------
# Timeline 構築
# ------------------------------------------------------------------

# 編集点と字幕から Timeline を組み立てる
# input_path      : ユーザーが選んだ元動画 (プロジェクト名・出力名の基準)
# media_path      : 実際に参照する動画 (正規化後の中間ファイル。時間軸は input と同一)
# keep_segments   : 残す区間 [(start, end), ...] (元動画時間)
# subtitle_items  : [{"start","end","text","role","font","font_size","use"}]
#                   時刻は「本編を詰めた後の時間軸」(OP を含まない / §3-2 方式 C)
# settings/profile: setting.json と出力プロファイル (縦/横)
def build(input_path, media_path, keep_segments, subtitle_items,
          settings, profile=None, source_duration=None, edit_point_meta=None):
    cfg = timeline_config(settings)
    ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
    canvas_w = int(profile["width"]) if profile else int(ffmpeg_cfg.get("output_width", 1920))
    canvas_h = int(profile["height"]) if profile else int(ffmpeg_cfg.get("output_height", 1080))
    orientation = (profile or {}).get("orientation", "landscape")
    fps = int(ffmpeg_cfg.get("output_fps", 60) or 60)

    timeline = Timeline(
        fps=fps, width=canvas_w, height=canvas_h, orientation=orientation,
        zoom_px_per_sec=cfg["default_zoom_px_per_sec"],
    )

    # ── メディアプール: 元動画を m1 として登録する
    body_media = media_probe.probe(media_path, "m1", settings, cfg["media"])
    if source_duration:
        body_media.duration_sec = float(source_duration)
    timeline.media_pool.append(body_media)
    timeline.source = {
        "input_path": os.path.abspath(input_path),
        "media_path": os.path.abspath(media_path),
        "media_id": body_media.id,
        "duration_sec": float(source_duration or body_media.duration_sec or 0.0),
        "width": body_media.width,
        "height": body_media.height,
        "fps": body_media.fps,
    }

    # ── 編集点 (検出結果そのものを記録する / §6.2.2)
    meta = dict(edit_point_meta or {})
    timeline.edit_points = {
        "detector": meta.get("detector", "silencedetect"),
        "threshold_db": meta.get("threshold_db"),
        "min_silence_sec": meta.get("min_silence_sec"),
        "detected_at": meta.get("detected_at", ""),
        "source_duration_sec": float(source_duration or body_media.duration_sec or 0.0),
        "keep_segments": [[float(s), float(e)] for s, e in (keep_segments or [])],
    }

    # ── トラックの器を作る
    video_track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    audio_track = Track(BASE_AUDIO_TRACK_ID, TRACK_AUDIO, 1, name="Audio 1",
                        link_track=BASE_VIDEO_TRACK_ID)
    subtitle_track = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    timeline.tracks = [video_track, audio_track, subtitle_track]

    cursor = 0.0

    # ── オープニング (§6.9-2)
    opening_path = resolve_material(settings, profile, "opening", cfg)
    if opening_path:
        cursor = _append_material(
            timeline, video_track, audio_track, opening_path,
            cursor, ORIGIN_OPENING, settings, cfg)

    # 本編の開始位置 = OP の尺。字幕はこのぶんだけ後ろへずれる (§6.9-2)
    body_offset = cursor

    # ── 本編 (編集点由来のクリップ列)
    for index, (start, end) in enumerate(keep_segments or []):
        duration = float(end) - float(start)
        if duration <= _MIN_SEGMENT_SEC:
            continue
        clip_id = timeline.next_id("c")
        video_track.clips.append(Clip(
            clip_id, body_media.id, cursor, duration,
            source_in=float(start), source_out=float(end),
            z_order=BASE_Z_ORDER,
            origin={"type": ORIGIN_SILENCE_CUT, "segment_index": index},
        ))
        if body_media.has_audio:
            audio_track.clips.append(AudioClip(timeline.next_id("a"), clip_id))
        cursor += duration

    # ── エンディング
    ending_path = resolve_material(settings, profile, "ending", cfg)
    if ending_path:
        cursor = _append_material(
            timeline, video_track, audio_track, ending_path,
            cursor, ORIGIN_ENDING, settings, cfg)

    # ── 字幕 (本編基準の時刻へ OP の尺を一律で加える)
    _append_subtitles(timeline, subtitle_track, subtitle_items, body_offset, keep_segments)

    timeline.normalize()
    _logger.info(
        "Timeline 構築: V1 %d クリップ (OP=%s / ED=%s) / S1 %d 字幕 / 全長 %.1fs / %dfps %dx%d",
        len(video_track.clips), "有" if opening_path else "無",
        "有" if ending_path else "無", len(subtitle_track.clips),
        timeline.duration_sec(), fps, canvas_w, canvas_h,
    )
    return timeline


# OP/ED 素材を V1 の現在位置へ 1 クリップとして追加する。戻り値は次の開始位置。
def _append_material(timeline, video_track, audio_track, path,
                     cursor, origin_type, settings, cfg):
    media = timeline.media_by_path(path)
    if media is None:
        media = media_probe.probe(path, timeline.next_id("m"), settings, cfg["media"])
        timeline.media_pool.append(media)

    duration = media_probe.default_clip_duration(media, cfg["media"], settings)
    if duration <= _MIN_SEGMENT_SEC:
        _logger.warning("素材の尺を取得できないため配置しません: %s", path)
        return cursor

    clip_id = timeline.next_id("c")
    video_track.clips.append(Clip(
        clip_id, media.id, cursor, duration,
        source_in=0.0, source_out=duration,
        z_order=BASE_Z_ORDER, origin={"type": origin_type},
    ))
    if media.has_audio:
        audio_track.clips.append(AudioClip(timeline.next_id("a"), clip_id))

    label = "オープニング" if origin_type == ORIGIN_OPENING else "エンディング"
    _logger.info("%sを配置: %s (%.2fs)", label, os.path.basename(path), duration)
    return cursor + duration


# 字幕 items を字幕トラックへ載せる
# items の時刻は本編基準のため body_offset を一律で加える (§6.9-2)。
# 由来の元動画時刻は TimeMap で逆算し origin へ残す (将来の再同期用 / §13)。
def _append_subtitles(timeline, subtitle_track, items, body_offset, keep_segments):
    timemap = TimeMap.from_segments(keep_segments, media_id="m1")
    for item in (items or []):
        start = float(item.get("start", 0.0))
        end = float(item.get("end", 0.0))
        duration = end - start
        if duration <= 0:
            continue
        origin = {"type": ORIGIN_ASR}
        # 本編基準の時刻を元動画時間へ逆算する (取れない場合は記録を省く)
        source_start = timemap.to_source(start)
        source_end = timemap.to_source(end)
        if source_start is not None:
            origin["source_start"] = round(source_start[1], 3)
        if source_end is not None:
            origin["source_end"] = round(source_end[1], 3)

        subtitle_track.clips.append(SubtitleClip(
            timeline.next_id("s"),
            timeline_start=start + body_offset,
            duration=duration,
            text=item.get("text", ""),
            role=item.get("role", DEFAULT_ROLE),
            font=item.get("font", ""),
            font_size=item.get("font_size"),
            use=item.get("use", True),
            z_order=DEFAULT_SUBTITLE_Z_ORDER,
            origin=origin,
        ))


# 編集点のみで Timeline を組む近道 (字幕が無い/認識をスキップした経路用)
def build_without_subtitles(input_path, media_path, keep_segments, settings,
                            profile=None, source_duration=None, edit_point_meta=None):
    return build(input_path, media_path, keep_segments, [], settings,
                 profile=profile, source_duration=source_duration,
                 edit_point_meta=edit_point_meta)
