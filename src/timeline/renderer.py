# Timeline レンダリング (docs/request/ver3/resolve.md §8)
# Timeline だけを入力に取り、編集内容を反映した動画を書き出す。
#
#   Step 1  ベース映像の構築   V1 の各クリップを抽出 → 連結 (OP/ED/ギャップを含む)
#   Step 2  オーバーレイ合成   V2 以降のクリップを z_order 順に overlay
#   Step 3  字幕焼き込み       ASS 生成 → 焼き込み
#   Step 4  出力配置           output_writer (呼び出し側)
#
# 区間抽出・連結・ASS 生成・焼き込みは既存資産をそのまま使う (§4-3)。
# FFmpeg 実行はすべて ffmpeg_runner.execute (= run_ffmpeg_progress) を経由する。
import os

from ..exceptions import InputError, TimelineError
from ..modules import ffmpeg_runner, silence_cutter, subtitle_generator
from ..settings.settings_window import resolve_fonts_dir
from ..utils.logger import get_logger
from .builder import timeline_config
from .media_probe import needs_normalize
from .model import SubtitleClip

_logger = get_logger(__name__)

# 抽出しない極短クリップの下限
_MIN_RENDER_SEC = 0.01


# ------------------------------------------------------------------
# 公開エントリ
# ------------------------------------------------------------------

# Timeline をレンダリングして 1 本の動画にする
# context: PipelineContext (working_dir / settings / 進捗コールバックを使う)
# 戻り値: 生成した動画のパス (context.current_video_path も更新する)
def render(timeline, context):
    settings = context.settings
    cfg = timeline_config(settings)
    ffmpeg_cfg = settings.get("ffmpeg", {})

    base_clips = timeline.base_clips()
    if not base_clips:
        raise TimelineError("出力対象のクリップがありません (Timeline が空です)")

    # 無音カットのフェード設定を編集点モードでも尊重する (§8.2)
    silence_cfg = settings.get("silence_cut", {})
    fade_sec = (float(silence_cfg.get("fade_duration_sec", 0.05))
                if silence_cfg.get("fade_enabled", False) else 0.0)

    # ── Step 1: ベース映像 (OP / 本編 / ED / ギャップ)
    base_path = _render_base(timeline, context, cfg, ffmpeg_cfg, fade_sec)
    context.set_current_video_path(base_path)

    # ── Step 2 / 3: オーバーレイ合成と字幕焼き込み
    result_path = _render_overlays_and_subtitles(
        timeline, context, base_path, cfg, ffmpeg_cfg)
    context.set_current_video_path(result_path)
    return result_path


# ------------------------------------------------------------------
# Step 1: ベース映像の構築 (§8.2)
# ------------------------------------------------------------------

# V1 のクリップ列とギャップから、レンダリング単位のセグメント列を作る
# gap_policy="black" のときだけギャップを黒+無音のセグメントとして挟む。
def _build_segments(timeline, cfg):
    segments = []
    gap_policy = cfg["render"]["gap_policy"]
    cursor = 0.0
    for clip in timeline.base_clips():
        gap = clip.timeline_start - cursor
        if gap > _MIN_RENDER_SEC and gap_policy == "black":
            segments.append({"kind": "gap", "duration": gap})
        segments.append({"kind": "clip", "clip": clip})
        cursor = clip.timeline_end
    return segments


# ベース映像を構築する
def _render_base(timeline, context, cfg, ffmpeg_cfg, fade_sec=0.0):
    segments = _build_segments(timeline, cfg)
    work_dir = context.working_dir
    output_path = context.allocate_intermediate("timeline_base.mp4")

    total = sum(
        s["duration"] if s["kind"] == "gap" else s["clip"].duration for s in segments
    ) or 1.0
    on_progress = context.progress_subcallback("ベース映像構築")

    gap_count = sum(1 for s in segments if s["kind"] == "gap")
    _logger.info(
        "ベース映像構築: %d クリップ / ギャップ %d 件 / 合計 %.1fs",
        len(segments) - gap_count, gap_count, total,
    )

    part_paths = []
    done = 0.0
    try:
        for index, segment in enumerate(segments):
            part_path = os.path.join(work_dir, f"tl_part_{index:05d}.mp4")
            duration = (segment["duration"] if segment["kind"] == "gap"
                        else segment["clip"].duration)

            def _part_progress(ratio, _kv, _base=done, _dur=duration):
                if on_progress is not None:
                    on_progress((_base + ratio * _dur) / total, _kv)

            if segment["kind"] == "gap":
                _render_gap(timeline, duration, part_path, ffmpeg_cfg, _part_progress)
            else:
                _extract_clip(timeline, segment["clip"], part_path,
                              cfg, ffmpeg_cfg, _part_progress, fade_sec)
            part_paths.append(part_path)
            done += duration

        # 既存の連結処理を再利用する (+genpts などの既知対策を共有する)
        silence_cutter.concat_files(
            part_paths, output_path, ffmpeg_cfg,
            reencode=cfg["render"]["concat_reencode"],
        )
        if on_progress is not None:
            on_progress(1.0, {})
    finally:
        for path in part_paths:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    _logger.warning("中間セグメントの削除に失敗: %s", path)

    return output_path


# クリップ 1 件を抽出して中間ファイル化する
# 素材の規格がキャンバスと一致する場合、生成されるコマンドは
# silence_cutter.extract_segment と実質同一 (CFR 固定・入力シーク) になる。
# OP/ED や D&D 素材など規格が異なる場合だけ正規化チェーンを挟む (§8.2)。
def _extract_clip(timeline, clip, out_path, cfg, ffmpeg_cfg, on_progress, fade_sec=0.0):
    media = timeline.media_by_id(clip.media_id)
    if media is None or not media.path or not os.path.exists(media.path):
        raise TimelineError(f"素材が見つかりません: {clip.media_id}")

    duration = clip.duration
    if duration <= _MIN_RENDER_SEC:
        raise InputError(f"クリップ尺が不正です: {clip.id} ({duration:.3f}s)")

    fps = ffmpeg_runner.get_output_fps(ffmpeg_cfg)
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)

    cmd = [ffmpeg, "-y", "-hide_banner"]
    if media.is_image():
        # 静止画はループ入力で指定尺ぶんの映像にする
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{duration:.3f}",
                "-i", media.path]
    else:
        # -ss を -i の前に置き、対象区間付近のみをデコードする (高速シーク)
        cmd += ["-ss", f"{clip.source_in:.3f}", "-i", media.path,
                "-t", f"{duration:.3f}"]

    # 音声を持たない素材 / ミュート指定には無音を合成し、映像と同尺の音声を必ず持たせる
    audio_clip = timeline.audio_clip_for(clip.id)
    muted = audio_clip is not None and audio_clip.muted
    needs_silence = media.is_image() or not media.has_audio or muted or audio_clip is None
    if needs_silence:
        sample_rate = ffmpeg_cfg.get("audio_sample_rate", 48000)
        cmd += ["-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}"]

    video_filters = _video_filters(timeline, media, clip, fps, fade_sec)
    audio_filters = [] if needs_silence else _audio_filters(audio_clip)

    if video_filters:
        cmd += ["-vf", ",".join(video_filters)]
    if audio_filters:
        cmd += ["-af", ",".join(audio_filters)]

    if needs_silence:
        # 映像は 0 番入力、音声は無音生成の 1 番入力から取る
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]

    cmd += [
        *ffmpeg_runner.build_encode_options(ffmpeg_cfg),
        "-fps_mode", "cfr",                 # 可変フレームレートを固定化
        "-r", str(fps),
        "-ar", str(ffmpeg_cfg.get("audio_sample_rate", 48000)),
        "-ac", "2",
        "-avoid_negative_ts", "make_zero",  # 連結時の負PTS/音ズレ対策
        out_path,
    ]

    ffmpeg_runner.execute(
        cmd, total_duration=duration, on_progress=on_progress,
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
    )
    return out_path


# クリップの映像フィルタを組み立てる (正規化 + フェード)
# 正規化チェーンの内容は concat_processor.concat と同一にする (§8.2)。
def _video_filters(timeline, media, clip, fps, fade_sec):
    filters = []
    if needs_normalize(media, timeline.width, timeline.height, fps):
        filters.append(
            f"scale={timeline.width}:{timeline.height}:force_original_aspect_ratio=decrease")
        filters.append(
            f"pad={timeline.width}:{timeline.height}:(ow-iw)/2:(oh-ih)/2")
        filters.append("setsar=1")
        filters.append(f"fps={fps}")
        filters.append("format=yuv420p")
        _warn_normalize_once(media, timeline, fps)

    # 無音カットのフェード設定 (render() が settings から解決して渡す / §8.2)
    if fade_sec > 0 and clip.duration > fade_sec * 2:
        fade_out_start = clip.duration - fade_sec
        filters.append(f"fade=t=in:st=0:d={fade_sec}")
        filters.append(f"fade=t=out:st={fade_out_start:.3f}:d={fade_sec}")
    return filters


# クリップの音声フィルタを組み立てる (ゲイン)
def _audio_filters(audio_clip):
    filters = []
    if audio_clip is not None and abs(audio_clip.gain_db) > 1e-6:
        filters.append(f"volume={audio_clip.gain_db:g}dB")
    return filters


# 正規化を適用した旨を 1 回だけ通知する (§11 ログ方針)
_warned_normalize = set()


def _warn_normalize_once(media, timeline, fps):
    if media.id in _warned_normalize:
        return
    _warned_normalize.add(media.id)
    _logger.info(
        "素材を正規化: %s (%dx%d %.3gfps → %dx%d %dfps)",
        os.path.basename(media.path), media.width, media.height, media.fps or 0,
        timeline.width, timeline.height, fps,
    )


# ギャップ (削除で残した空白) を黒 + 無音のセグメントとして生成する (§8.2)
def _render_gap(timeline, duration, out_path, ffmpeg_cfg, on_progress):
    fps = ffmpeg_runner.get_output_fps(ffmpeg_cfg)
    sample_rate = ffmpeg_cfg.get("audio_sample_rate", 48000)
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-f", "lavfi", "-t", f"{duration:.3f}",
        "-i", f"color=c=black:s={timeline.width}x{timeline.height}:r={fps}",
        "-f", "lavfi", "-t", f"{duration:.3f}",
        "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
        *ffmpeg_runner.build_encode_options(ffmpeg_cfg),
        "-fps_mode", "cfr", "-r", str(fps),
        "-ar", str(sample_rate), "-ac", "2",
        "-pix_fmt", "yuv420p",
        "-avoid_negative_ts", "make_zero",
        out_path,
    ]
    ffmpeg_runner.execute(
        cmd, total_duration=duration, on_progress=on_progress,
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
    )
    return out_path


# ------------------------------------------------------------------
# Step 2 / 3: オーバーレイ合成と字幕焼き込み (§8.3 / §8.4)
# ------------------------------------------------------------------

# z_order 昇順のオーバーレイ要素を「字幕グループ」と「メディアクリップ」の層へ分ける
# 連続する字幕はまとめて 1 つの ASS にする (§8.4)。
def _build_layers(timeline, cfg):
    layers = []
    if not cfg["render"]["overlay_enabled"]:
        # オーバーレイ無効時は字幕だけを 1 グループとして扱う (切り分け用)
        subtitles = timeline.subtitle_clips()
        return [{"kind": "subtitles", "clips": subtitles}] if subtitles else []

    for element in timeline.overlay_elements():
        if isinstance(element, SubtitleClip):
            if layers and layers[-1]["kind"] == "subtitles":
                layers[-1]["clips"].append(element)
            else:
                layers.append({"kind": "subtitles", "clips": [element]})
        else:
            layers.append({"kind": "media", "clip": element})
    return layers


# オーバーレイと字幕を適用する。どちらも無ければベースをそのまま返す。
def _render_overlays_and_subtitles(timeline, context, base_path, cfg, ffmpeg_cfg):
    layers = _build_layers(timeline, cfg)
    if not layers:
        _logger.info("オーバーレイ・字幕が無いため合成をスキップします")
        return base_path

    media_layers = [layer for layer in layers if layer["kind"] == "media"]
    if not media_layers:
        # 字幕のみ = 既存の burn_subtitle をそのまま使う (既知の A/V 同期対策を共有する)
        return _burn_subtitles_only(timeline, context, base_path, layers[0]["clips"],
                                    ffmpeg_cfg)
    return _composite(timeline, context, base_path, layers, cfg, ffmpeg_cfg)


# 実効字幕設定と FontProfile を解決する (縦動画の上書きを反映する)
def _subtitle_profile(context):
    settings = context.settings
    profile = getattr(context, "output_profile", None)
    eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
        settings.get("subtitle", {}), settings.get("vertical", {}), profile)
    return eff_cfg, subtitle_generator.build_font_profile(eff_cfg)


# 字幕クリップ群から ASS を書き出す
def _write_ass(timeline, context, clips, name):
    eff_cfg, font_profile = _subtitle_profile(context)
    ass_path = context.allocate_intermediate(name)
    items = [clip.to_item() for clip in clips if clip.use]
    subtitle_generator.build_subtitle_file(
        items, font_profile, ass_path,
        video_width=timeline.width, video_height=timeline.height,
    )
    return ass_path


# 字幕のみのケース: 既存 burn_subtitle をそのまま使う
def _burn_subtitles_only(timeline, context, base_path, clips, ffmpeg_cfg):
    used = [c for c in clips if c.use]
    if not used:
        _logger.info("使用する字幕が無いため焼き込みをスキップします")
        return base_path

    ass_path = _write_ass(timeline, context, used, "timeline_subtitle.ass")
    output_path = context.allocate_intermediate("timeline_subtitle.mp4")
    total_duration = timeline.duration_sec()
    _logger.info("字幕焼き込み: %d 件", len(used))
    subtitle_generator.burn_subtitle(
        base_path, ass_path, output_path, ffmpeg_cfg,
        total_duration=total_duration,
        on_progress=context.progress_subcallback("字幕焼き込み"),
        # ベースは既にキャンバス寸法へ正規化済みのため二重 scale はしない
        target_size=None,
        fonts_dir=resolve_fonts_dir(context.settings),
    )
    return output_path


# ass フィルタ用にパスをエスケープする (burn_subtitle と同一の 2 段階エスケープ)
def _escape_filter_path(path):
    return str(path).replace("\\", "/").replace(":", "\\:")


# オーバーレイ + 字幕を 1 パスで合成する (§8.4)
# z_order 昇順に ass / overlay を鎖状につなぐ。
def _composite(timeline, context, base_path, layers, cfg, ffmpeg_cfg):
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    fps = ffmpeg_runner.get_output_fps(ffmpeg_cfg)
    sample_rate = ffmpeg_cfg.get("audio_sample_rate", 48000)
    fonts_dir = resolve_fonts_dir(context.settings)
    output_path = context.allocate_intermediate("timeline_composite.mp4")

    inputs = [base_path]
    input_args = ["-i", base_path]
    chains = []
    audio_labels = ["[0:a]"]
    current = "[0:v]"
    step = 0

    for layer in layers:
        if layer["kind"] == "subtitles":
            used = [c for c in layer["clips"] if c.use]
            if not used:
                continue
            ass_path = _write_ass(
                timeline, context, used, f"timeline_subtitle_{step}.ass")
            option = f"ass='{_escape_filter_path(ass_path)}'"
            if fonts_dir and os.path.isdir(fonts_dir):
                option += f":fontsdir='{_escape_filter_path(fonts_dir)}'"
            label = f"[v{step}]"
            chains.append(f"{current}{option}{label}")
            current = label
            step += 1
            continue

        clip = layer["clip"]
        media = timeline.media_by_id(clip.media_id)
        if media is None or not os.path.exists(media.path or ""):
            _logger.warning("オーバーレイ素材が見つからないためスキップ: %s", clip.id)
            continue

        index = len(inputs)
        if media.is_image():
            input_args += ["-loop", "1", "-framerate", str(fps),
                           "-t", f"{clip.timeline_end:.3f}", "-i", media.path]
        else:
            input_args += ["-ss", f"{clip.source_in:.3f}",
                           "-t", f"{clip.duration:.3f}", "-i", media.path]
        inputs.append(media.path)

        overlay_label = f"[ov{step}]"
        # 拡大率をキャンバス比で解決する (transform.scale = キャンバス幅に対する比率)
        target_w = max(int(timeline.width * float(clip.transform.scale or 1.0)), 2)
        scale_chain = f"scale={target_w}:-2"
        if float(clip.transform.opacity or 1.0) < 1.0:
            scale_chain += (f",format=rgba,colorchannelmixer="
                            f"aa={float(clip.transform.opacity):.3f}")
        if not media.is_image():
            # 動画オーバーレイは Timeline 上の開始位置へ PTS をずらす
            scale_chain += f",setpts=PTS+{clip.timeline_start:.3f}/TB"
        chains.append(f"[{index}:v]{scale_chain}{overlay_label}")

        # 正規化座標 (-1〜1・Y は上が正) を overlay の左上座標へ変換する
        pos_x = (float(clip.transform.x or 0.0) + 1.0) * timeline.width / 2.0
        pos_y = (1.0 - float(clip.transform.y or 0.0)) * timeline.height / 2.0
        expr_x = f"{pos_x:.1f}-w/2"
        expr_y = f"{pos_y:.1f}-h/2"
        enable = (f"between(t,{clip.timeline_start:.3f},"
                  f"{clip.timeline_end:.3f})")
        label = f"[v{step}]"
        chains.append(
            f"{current}{overlay_label}overlay={expr_x}:{expr_y}:enable='{enable}'{label}")
        current = label
        step += 1

        # 音声付き動画オーバーレイはベース音声へ合成する
        if media.has_audio and not media.is_image():
            delay_ms = int(clip.timeline_start * 1000)
            alabel = f"[a{step}]"
            chains.append(
                f"[{index}:a]adelay={delay_ms}|{delay_ms},"
                f"aformat=sample_rates={sample_rate}:channel_layouts=stereo{alabel}")
            audio_labels.append(alabel)

    if not chains:
        _logger.info("有効なオーバーレイが無いため合成をスキップします")
        return base_path

    # 音声の合流 (複数あれば amix。1 本ならそのまま使う)
    if len(audio_labels) > 1:
        chains.append(
            f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:"
            f"duration=first:dropout_transition=0[aout]")
        audio_map = "[aout]"
    else:
        audio_map = "0:a"

    filter_complex = ";".join(chains)
    total_duration = timeline.duration_sec()
    _logger.info(
        "オーバーレイ合成: %d 層 (追加入力 %d 件)", len(chains), len(inputs) - 1)

    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-fflags", "+genpts",
        *input_args,
        "-filter_complex", filter_complex,
        "-map", current,
        "-map", audio_map,
        *ffmpeg_runner.build_encode_options(ffmpeg_cfg),
        "-fps_mode", "cfr", "-r", str(fps),
        "-ar", str(sample_rate), "-ac", "2",
        "-t", f"{total_duration:.3f}",
        output_path,
    ]
    ffmpeg_runner.execute(
        cmd, total_duration=total_duration,
        on_progress=context.progress_subcallback("オーバーレイ・字幕合成"),
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
    )
    return output_path
