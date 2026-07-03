# オープニング・エンディング結合モジュール
# 設計書 ③ オープニング・エンディング結合 に対応
# concat filter はコーデック差異は吸収するが解像度/SAR差異は吸収しないため、
# concat 前に各入力を scale/pad で統一解像度へ正規化してから結合する
import os

from ..exceptions import InputError
from ..utils.logger import get_logger
from . import ffmpeg_runner

_logger = get_logger(__name__)


# 結合対象パスが利用可能か判定する (空/未存在ならスキップ)
def is_available(path):
    if not path:
        return False
    if not os.path.exists(path):
        _logger.warning("結合素材が見つかりません: %s (スキップ)", path)
        return False
    return True


# 複数動画を concat filter で結合する
def concat(parts, output_path, ffmpeg_settings, total_duration=0.0, on_progress=None):
    if len(parts) < 2:
        raise InputError("結合対象が2つ未満です")

    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)

    inputs = []
    for path in parts:
        inputs.extend(["-i", path])

    n = len(parts)
    # 結合時の統一パラメータを setting.json から取得 (ハードコード回避)
    width = ffmpeg_settings.get("output_width", 1920)
    height = ffmpeg_settings.get("output_height", 1080)
    fps = ffmpeg_settings.get("output_fps", 60)
    sample_rate = ffmpeg_settings.get("audio_sample_rate", 48000)

    # 各入力を正規化: 映像はアスペクト比維持で scale→pad→SAR/fps/pix_fmt 統一、
    # 音声は sample_rate/channel_layout を統一する。
    # concat フィルタは全入力が同一形式 (解像度/SAR 等) であることを前提とするため。
    chains = []
    concat_labels = ""
    for i in range(n):
        chains.append(
            f"[{i}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
        chains.append(
            f"[{i}:a:0]aformat=sample_rates={sample_rate}:channel_layouts=stereo,"
            f"asetpts=N/SR/TB[a{i}]"
        )
        concat_labels += f"[v{i}][a{i}]"

    filter_spec = (
        ";".join(chains)
        + f";{concat_labels}concat=n={n}:v=1:a=1[outv][outa]"
    )

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        *inputs,
        "-filter_complex", filter_spec,
        "-map", "[outv]",
        "-map", "[outa]",
        *ffmpeg_runner.build_encode_options(ffmpeg_settings),
        output_path,
    ]
    ffmpeg_runner.execute(cmd, total_duration=total_duration, on_progress=on_progress)
    return output_path


# 指定位置 (opening/ending) の素材を結合する
def run(context, position):
    settings = context.settings
    general_cfg = settings.get("general", {})
    ffmpeg_cfg = settings.get("ffmpeg", {})

    if position == "opening":
        material = general_cfg.get("opening_video", "")
        enabled = general_cfg.get("opening_enabled", True)
        label = "オープニング結合"
    elif position == "ending":
        material = general_cfg.get("ending_video", "")
        enabled = general_cfg.get("ending_enabled", True)
        label = "エンディング結合"
    else:
        raise InputError(f"未知の結合位置: {position}")

    # 結合フラグが無効なら素材有無に関わらずスキップする
    if not enabled:
        _logger.info("%s スキップ (フラグ無効)", label)
        return context.current_video_path()

    if not is_available(material):
        _logger.info("%s スキップ (素材未設定)", label)
        return context.current_video_path()

    current = context.current_video_path()
    output_path = context.allocate_intermediate(f"{position}_merged.mp4")

    parts = [material, current] if position == "opening" else [current, material]

    # 進捗用 total_duration は本編と素材の総和を用いる
    main_dur = ffmpeg_runner.probe_duration(current, ffmpeg_cfg)
    material_dur = ffmpeg_runner.probe_duration(material, ffmpeg_cfg)

    _logger.info("%s 開始: %s + %s", label, *parts)
    concat(
        parts, output_path, ffmpeg_cfg,
        total_duration=main_dur + material_dur,
        on_progress=context.progress_subcallback(label),
    )
    context.set_current_video_path(output_path)
    _logger.info("%s 完了: %s", label, output_path)
    return output_path
