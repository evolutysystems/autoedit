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
import math
import os

from ..exceptions import InputError, TimelineError
from ..modules import (
    blur_overlay,
    comment_decor,
    ffmpeg_runner,
    silence_cutter,
    subtitle_generator,
)
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

    # ── Step 1.5: ぼかしマスクの用意 (ver5 resolve2 §5.5.2)
    # 機能 OFF・指定なしなら None が返り、以降のコマンドは 1 文字も変わらない (R9)。
    context.blur_mask_path = _prepare_blur_mask(timeline, context, settings)

    # ── Step 2 / 3: オーバーレイ合成と字幕焼き込み
    result_path = _render_overlays_and_subtitles(
        timeline, context, base_path, cfg, ffmpeg_cfg)

    # ── Step 3.4: ぼかしが未適用のまま先へ進めない (§5.5.3)
    # オーバーレイも字幕も無い経路はここで単独パスとして焼き込む。
    result_path = blur_overlay.ensure_applied(
        context, result_path, (timeline.width, timeline.height), ffmpeg_cfg,
        total_duration=timeline.duration_sec())

    # ── Step 3.5: 焼き込みもオーバーレイも走らなかった場合の最終化 (resolve3 §5.5)
    # 焼き込みを通っていれば既に mp4/AAC なので、その場合は何もしない。
    result_path = _finalize_base(timeline, context, result_path, ffmpeg_cfg)
    context.set_current_video_path(result_path)
    return result_path


# ぼかしマスクを用意する (ver5 resolve2 §5.4 prepare)
# 機能 OFF なら blur パッケージを **import すらしない** (§4-1)。
def _prepare_blur_mask(timeline, context, settings):
    from ..blur.config import is_enabled        # noqa: PLC0415 (機能 OFF なら読まない)

    if not is_enabled(settings):
        return None
    from ..blur import mask_builder             # noqa: PLC0415

    context.blur_mask_failed = False
    mask_path = mask_builder.prepare(timeline, context)
    # マスクを作れた場合でも、指定の一部が効かないなら確認する (ver5 resolve8 §5.8)。
    # 「ボカす」と言われた場所の位置がまったく決まらないときがこれに当たる。
    if getattr(context, "blur_mask_failed", False):
        _confirm_blur_failure(context)
    return mask_path


# ぼかしを掛けられなかったときの扱い (§5.9)
#
# **「ぼかすと指定したのに素で出た」が最も損害の大きい失敗**であるため、
# 黙って続行することだけは絶対にしない。GUI ではフックで利用者に選ばせ、
# フックが無い呼び出し (CLI / テスト) では出力を中止する。
def _confirm_blur_failure(context):
    reason = ("ぼかしを掛けられませんでした (マスクを作れませんでした)。\n"
              "ぼかしを入れずに出力しますか？")
    callback = getattr(context, "blur_failure_callback", None)
    if callback is None:
        raise TimelineError(
            "ぼかしを掛けられないため出力を中止しました。"
            "「ぼかし...」を開き、指定を確かめてください。")

    if not callback(reason):
        raise TimelineError("ぼかしを掛けられないため、利用者の指示で出力を中止しました。")
    _logger.warning("利用者の指示により、ぼかしを入れずに出力します")


# ------------------------------------------------------------------
# Step 1: ベース映像の構築 (§8.2)
# ------------------------------------------------------------------

# V1 のクリップ列とギャップから、レンダリング単位のセグメント列を作る
# gap_policy="black" のときだけギャップを黒+無音のセグメントとして挟む。
# frame_quantize=True のときは各セグメントの尺を「終端フレーム − 開始フレーム」で決める
# (20260812 resolve3 §3-2)。絶対フレーム番号の差を取るため丸め誤差が累積しない。
def _build_segments(timeline, cfg):
    segments = []
    gap_policy = cfg["render"]["gap_policy"]
    quantize = cfg["render"]["frame_quantize"]
    cursor = 0.0
    cursor_frame = 0            # 直前セグメントの終端 (フレーム番号)
    for clip in timeline.base_clips():
        gap = clip.timeline_start - cursor
        start_frame = timeline.to_frames(clip.timeline_start)
        if gap > _MIN_RENDER_SEC and gap_policy == "black":
            segments.append({
                "kind": "gap",
                "duration": _tile_duration(
                    timeline, cursor_frame, start_frame, gap, quantize),
            })
            cursor_frame = start_frame
        end_frame = timeline.to_frames(clip.timeline_end)
        segments.append({
            "kind": "clip",
            "clip": clip,
            "duration": _tile_duration(
                timeline, start_frame, end_frame, clip.duration, quantize),
        })
        cursor = clip.timeline_end
        cursor_frame = end_frame
    return segments


# セグメントの尺を求める
# quantize=False なら従来どおりモデルの秒をそのまま使う (切り戻し経路)。
# quantize=True なら絶対フレーム番号の差で決めるため、部品を何本並べても誤差が積み上がらない
# (全体の誤差は最大でも半フレーム = 60fps で 8.3ms)。
# ※ gap_policy="close" で空白を詰める場合はクリップ単体の尺だけが量子化される。
def _tile_duration(timeline, start_frame, end_frame, raw_duration, quantize):
    if not quantize:
        return raw_duration
    frames = max(end_frame - start_frame, 1)
    return timeline.from_frames(frames)


# -t (尺) へ渡す文字列を作る (20260812 resolve3 §5.3)
# フレーム境界は有限小数で表せない (60fps の 1 フレーム = 0.016666... 秒)。
# 四捨五入して境界より 1 マイクロ秒でも上へ寄せると、FFmpeg は次のフレームまで書いてしまい
# **映像だけが 1 フレーム長くなる**。concat デマルチプレクサは長い方 (=映像) の尺で次の
# ファイルを配置するため、その 1 フレームぶん音声に穴が空き、部品数ぶん累積する
# (実測: -t 5.016667 → 映像 302 フレーム / -t 5.016666 → 映像 301 フレーム・音声と完全一致)。
# したがって必ず切り捨てて境界の内側へ寄せる。切り捨て幅は 1 マイクロ秒未満のため、
# 音声のサンプル数は目標値へ丸められて映像と一致する。
def _duration_arg(duration):
    micro = max(math.floor(float(duration) * 1_000_000), 0)
    return f"{micro // 1_000_000}.{micro % 1_000_000:06d}"


# ベース映像を構築する
def _render_base(timeline, context, cfg, ffmpeg_cfg, fade_sec=0.0):
    segments = _build_segments(timeline, cfg)
    work_dir = context.working_dir

    # 中間パートの音声コーデックと容器 (20260812 resolve3 §3-1)。
    # 部品を AAC で書くとエンコーダ遅延 (1024 サンプル = 21.3ms) が 1 部品ごとに尺へ乗り、
    # concat デマルチプレクサの連結で部品数ぶん累積してテロップが先行する。
    # 既定は非圧縮 (pcm_s16le) とし、AAC 化は焼き込み / 最終化の 1 回だけにする。
    audio_codec = cfg["render"]["intermediate_audio_codec"]
    suffix = ffmpeg_runner.intermediate_suffix(
        audio_codec, cfg["render"]["intermediate_container"])
    output_path = context.allocate_intermediate(f"timeline_base{suffix}")

    total = sum(s["duration"] for s in segments) or 1.0
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
            part_path = os.path.join(work_dir, f"tl_part_{index:05d}{suffix}")
            duration = segment["duration"]

            def _part_progress(ratio, _kv, _base=done, _dur=duration):
                if on_progress is not None:
                    on_progress((_base + ratio * _dur) / total, _kv)

            if segment["kind"] == "gap":
                _render_gap(timeline, duration, part_path, ffmpeg_cfg, _part_progress,
                            audio_codec)
            else:
                _extract_clip(timeline, segment["clip"], part_path,
                              cfg, ffmpeg_cfg, _part_progress, fade_sec,
                              duration, audio_codec)
            part_paths.append(part_path)
            done += duration

        # 既存の連結処理を再利用する (+genpts などの既知対策を共有する)
        silence_cutter.concat_files(
            part_paths, output_path, ffmpeg_cfg,
            reencode=cfg["render"]["concat_reencode"],
        )
        if on_progress is not None:
            on_progress(1.0, {})
        _verify_base_duration(output_path, total, len(segments), ffmpeg_cfg)
    finally:
        for path in part_paths:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    _logger.warning("中間セグメントの削除に失敗: %s", path)

    return output_path


# 連結後のベース映像の実尺とモデル尺を突き合わせる (20260812 resolve3 §5.6)
# 1 フレームを超える差はタイムスタンプの累積ずれを疑うべき兆候として WARNING に残す。
# 計測に失敗しても書き出しは止めない (照合はあくまで再発検知のため)。
def _verify_base_duration(output_path, total, segment_count, ffmpeg_cfg):
    try:
        actual = ffmpeg_runner.probe_duration(output_path, ffmpeg_cfg)
    except Exception:  # noqa: BLE001 (計測失敗で書き出しを止めない)
        _logger.debug("ベース映像の尺照合に失敗しました (処理は続行します)", exc_info=True)
        return
    tolerance = 1.0 / ffmpeg_runner.get_output_fps(ffmpeg_cfg)
    diff = actual - total
    log = _logger.warning if abs(diff) > tolerance else _logger.info
    log("ベース映像の尺: モデル %.3fs / 実測 %.3fs (差 %+.3fs / %d 部品)",
        total, actual, diff, segment_count)


# クリップ 1 件を抽出して中間ファイル化する
# 素材の規格がキャンバスと一致する場合、生成されるコマンドは
# silence_cutter.extract_segment と実質同一 (CFR 固定・入力シーク) になる。
# OP/ED や D&D 素材など規格が異なる場合だけ正規化チェーンを挟む (§8.2)。
# duration: レンダリング上の尺 (フレームタイル化した値 / None ならモデルの尺をそのまま使う)
# audio_codec: 中間ファイル用の音声コーデック (None なら setting.json の ffmpeg.audio_codec)
def _extract_clip(timeline, clip, out_path, cfg, ffmpeg_cfg, on_progress, fade_sec=0.0,
                  duration=None, audio_codec=None):
    media = timeline.media_by_id(clip.media_id)
    if media is None or not media.path or not os.path.exists(media.path):
        raise TimelineError(f"素材が見つかりません: {clip.media_id}")

    duration = clip.duration if duration is None else duration
    if duration <= _MIN_RENDER_SEC:
        raise InputError(f"クリップ尺が不正です: {clip.id} ({duration:.3f}s)")

    fps = ffmpeg_runner.get_output_fps(ffmpeg_cfg)
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)

    # 尺は _duration_arg で切り捨てて渡す (フレーム境界の丸め上げを避ける / resolve3 §5.3)
    cmd = [ffmpeg, "-y", "-hide_banner"]
    if media.is_image():
        # 静止画はループ入力で指定尺ぶんの映像にする
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", _duration_arg(duration),
                "-i", media.path]
    else:
        # -ss を -i の前に置き、対象区間付近のみをデコードする (高速シーク)
        cmd += ["-ss", f"{clip.source_in:.3f}", "-i", media.path,
                "-t", _duration_arg(duration)]

    # 音声を持たない素材 / ミュート指定には無音を合成し、映像と同尺の音声を必ず持たせる
    audio_clip = timeline.audio_clip_for(clip.id)
    muted = audio_clip is not None and audio_clip.muted
    needs_silence = media.is_image() or not media.has_audio or muted or audio_clip is None
    if needs_silence:
        sample_rate = ffmpeg_cfg.get("audio_sample_rate", 48000)
        cmd += ["-f", "lavfi", "-t", _duration_arg(duration),
                "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}"]

    video_filters = _video_filters(timeline, media, clip, fps, fade_sec, duration)
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
        *ffmpeg_runner.build_intermediate_encode_options(ffmpeg_cfg, audio_codec),
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
def _video_filters(timeline, media, clip, fps, fade_sec, duration=None):
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
    # フェードアウトの開始位置は実際に書き出す尺を基準にする (resolve3 §5.3)
    duration = clip.duration if duration is None else duration
    if fade_sec > 0 and duration > fade_sec * 2:
        fade_out_start = duration - fade_sec
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
def _render_gap(timeline, duration, out_path, ffmpeg_cfg, on_progress, audio_codec=None):
    fps = ffmpeg_runner.get_output_fps(ffmpeg_cfg)
    sample_rate = ffmpeg_cfg.get("audio_sample_rate", 48000)
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-f", "lavfi", "-t", _duration_arg(duration),
        "-i", f"color=c=black:s={timeline.width}x{timeline.height}:r={fps}",
        "-f", "lavfi", "-t", _duration_arg(duration),
        "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
        *ffmpeg_runner.build_intermediate_encode_options(ffmpeg_cfg, audio_codec),
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
# Step 3.5: 中間形式のままの成果物を最終形式へ直す (§5.5)
# ------------------------------------------------------------------

# 焼き込みもオーバーレイも無いとき、中間形式 (PCM/.mov) を最終形式 (mp4/AAC) へ直す
# (20260812 resolve3 §5.5)。後段の output_writer.finalize / clip_writer._copy_individual は
# 成果物を .mp4 名へ移動・複製するため、中間形式のまま返すと破綻する。
# 映像は再エンコードせずコピーし、音声だけ setting.json どおり (既定 aac) へ変換する。
def _finalize_base(timeline, context, video_path, ffmpeg_cfg):
    if os.path.splitext(video_path)[1].lower() == ".mp4":
        return video_path           # 焼き込み済み / 中間も mp4 の設定なら何もしない

    output_path = context.allocate_intermediate("timeline_final.mp4")
    sample_rate = ffmpeg_cfg.get("audio_sample_rate", 48000)
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    total_duration = timeline.duration_sec()
    _logger.info("中間形式を最終形式へ変換: %s → mp4 (音声のみ再エンコード)",
                 os.path.splitext(video_path)[1] or "?")
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-i", video_path,
        "-c:v", "copy",
        "-c:a", ffmpeg_cfg.get("audio_codec", "aac"),
        "-ar", str(sample_rate), "-ac", "2",
        output_path,
    ]
    ffmpeg_runner.execute(
        cmd, total_duration=total_duration,
        on_progress=context.progress_subcallback("最終化"),
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
    )
    return output_path


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
# 戻り値 (ass のパス, 書き出した item 配列)。item はアイコン合成でも使う (resolve11 §5.6)。
def _write_ass(timeline, context, clips, name):
    eff_cfg, font_profile = _subtitle_profile(context)
    ass_path = context.allocate_intermediate(name)
    items = [clip.to_item() for clip in clips if clip.use]
    subtitle_generator.build_subtitle_file(
        items, font_profile, ass_path,
        video_width=timeline.width, video_height=timeline.height,
        # コメントの背景 (角丸の箱) を出すため実効字幕設定を渡す (resolve11 §5.11-5)
        subtitle_cfg=eff_cfg,
    )
    return ass_path, items


# 字幕のみのケース: 既存 burn_subtitle をそのまま使う
def _burn_subtitles_only(timeline, context, base_path, clips, ffmpeg_cfg):
    used = [c for c in clips if c.use]
    if not used:
        _logger.info("使用する字幕が無いため焼き込みをスキップします")
        return base_path

    ass_path, items = _write_ass(timeline, context, used, "timeline_subtitle.ass")
    output_path = context.allocate_intermediate("timeline_subtitle.mp4")
    total_duration = timeline.duration_sec()
    _logger.info("字幕焼き込み: %d 件", len(used))
    # コメントアイコンを字幕の上へ重ねる (resolve11 §5.6-1)。
    # 対象が無ければ空リストが返り、コマンドは従来と完全に同一になる。
    eff_cfg, _profile = _subtitle_profile(context)
    chains, _count, _groups = comment_decor.build_icon_chains(
        items, eff_cfg, timeline.width, timeline.height,
        in_label="[vsub]", out_label="")
    # ぼかしは字幕より**前**へ入れる (字幕やアイコンはぼかさない / R8)
    pre_chains, _label = _blur_chains(context, timeline, "", "[vpre]")
    subtitle_generator.burn_subtitle(
        base_path, ass_path, output_path, ffmpeg_cfg,
        total_duration=total_duration,
        on_progress=context.progress_subcallback("字幕焼き込み"),
        # ベースは既にキャンバス寸法へ正規化済みのため二重 scale はしない
        target_size=None,
        fonts_dir=resolve_fonts_dir(context.settings),
        extra_chains=chains,
        filter_script_path=context.allocate_intermediate("timeline_subtitle_vf.txt"),
        filter_script_chars=eff_cfg.get("comment_icon_filter_script_chars", 8000),
        pre_chains=pre_chains,
    )
    if pre_chains:
        context.blur_applied = True
    return output_path


# ぼかしのフィルタチェーンを組む (マスクが無ければ空リスト / §5.5.2)
# 呼び出し側はチェーンの**先頭**へ足すこと。後ろへ足すと字幕までぼける (R8)。
def _blur_chains(context, timeline, in_label, out_label, prefix="bl"):
    mask_path = getattr(context, "blur_mask_path", None)
    if not mask_path or getattr(context, "blur_applied", False):
        return [], in_label

    from ..blur.config import config            # noqa: PLC0415 (機能 OFF なら読まない)

    return blur_overlay.build_chains(
        mask_path, in_label, out_label, timeline.width, timeline.height,
        config(context.settings), prefix=prefix)


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
    # コメントアイコンの配置に使う実効字幕設定 (縦動画の上書きを含む)
    eff_cfg, _font_profile = _subtitle_profile(context)

    inputs = [base_path]
    input_args = ["-i", base_path]
    chains = []
    audio_labels = ["[0:a]"]
    current = "[0:v]"
    step = 0

    # ぼかしはチェーンの**先頭**へ足す (§5.5.2)。後ろへ足すと字幕・アイコン・
    # オーバーレイ素材までぼけてしまう (R8)。movie= で読むため入力は増えない。
    blur_chains, blur_label = _blur_chains(context, timeline, current, "[blout]")
    if blur_chains:
        chains.extend(blur_chains)
        current = blur_label
        context.blur_applied = True

    for layer in layers:
        if layer["kind"] == "subtitles":
            used = [c for c in layer["clips"] if c.use]
            if not used:
                continue
            ass_path, items = _write_ass(
                timeline, context, used, f"timeline_subtitle_{step}.ass")
            option = f"ass='{_escape_filter_path(ass_path)}'"
            if fonts_dir and os.path.isdir(fonts_dir):
                option += f":fontsdir='{_escape_filter_path(fonts_dir)}'"
            label = f"[v{step}]"
            chains.append(f"{current}{option}{label}")
            current = label
            step += 1
            # コメントアイコンは、その字幕グループの直後へ重ねる (resolve11 §5.6-2)。
            # movie ソースで読むため入力本数 (inputs / index) は増えない。
            icon_out = f"[v{step}]"
            icon_chains, _count, _groups = comment_decor.build_icon_chains(
                items, eff_cfg, timeline.width, timeline.height,
                in_label=current, out_label=icon_out,
                # 1 つの filtergraph で複数回呼ぶためラベルを重複させない
                prefix=f"cic{step}")
            if icon_chains:
                chains.extend(icon_chains)
                current = icon_out
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
