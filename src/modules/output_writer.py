# 出力モジュール
# 設計書 5.5 output_writer.py に対応
import os
import shutil

from ..exceptions import InputError
from ..utils.logger import get_logger
from . import ffmpeg_runner, watermark_overlay

_logger = get_logger(__name__)

# 既定の出力ファイル接尾辞
_DEFAULT_SUFFIX = "_edited"


# 衝突しない出力ファイル名を生成する
def resolve_output_path(input_path, output_dir, suffix=_DEFAULT_SUFFIX):
    if not output_dir:
        # 出力ディレクトリ未指定時は入力と同じディレクトリへ
        output_dir = os.path.dirname(os.path.abspath(input_path))

    os.makedirs(output_dir, exist_ok=True)

    base = os.path.splitext(os.path.basename(input_path))[0]
    ext = ".mp4"

    candidate = os.path.join(output_dir, f"{base}{suffix}{ext}")
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(output_dir, f"{base}{suffix}_{counter}{ext}")
        counter += 1
    return candidate


# 中間ファイルを最終位置へ配置する
def finalize(intermediate_path, output_path):
    if not os.path.exists(intermediate_path):
        raise InputError(f"中間ファイルが存在しません: {intermediate_path}")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # 同一ボリュームなら move、跨ぐなら copy+remove
    shutil.move(intermediate_path, output_path)
    return output_path


# 出力直前の保険 (ver5 resolve §5.4)。
# 合成が走らない経路 (レガシー) はここが唯一の焼き込み地点になる。
# レンダリング経路では既に適用済みのため何もしない。
def _ensure_watermark(context):
    video_path = context.current_video_path()
    if not watermark_overlay.is_required(context):
        return video_path

    ffmpeg_cfg = context.settings.get("ffmpeg", {})
    result = watermark_overlay.ensure_applied(
        context, video_path, _canvas_size(context, video_path, ffmpeg_cfg), ffmpeg_cfg,
        total_duration=_duration(video_path, ffmpeg_cfg))
    context.set_current_video_path(result)
    return result


# キャンバス寸法。出力プロファイルがあればそれを使い、無ければ動画から測る。
def _canvas_size(context, video_path, ffmpeg_cfg):
    profile = getattr(context, "output_profile", None) or {}
    width = int(profile.get("width") or 0)
    height = int(profile.get("height") or 0)
    if width > 0 and height > 0:
        return width, height

    try:
        return ffmpeg_runner.probe_dimensions(video_path, ffmpeg_cfg)[:2]
    except Exception:  # noqa: BLE001 (測れなくても既定値で焼き込む)
        _logger.warning("出力寸法を取得できないため 1920x1080 として透かしを配置します")
        return 1920, 1080


# 進捗表示用の総尺。取れなくても焼き込みは続ける。
def _duration(video_path, ffmpeg_cfg):
    try:
        return ffmpeg_runner.probe_duration(video_path, ffmpeg_cfg)
    except Exception:  # noqa: BLE001 (進捗の総尺は取れなくても致命でない)
        return 0.0


# 公開エントリポイント
def run(context):
    settings = context.settings
    general_cfg = settings.get("general", {})
    output_dir = general_cfg.get("output_directory", "")

    output_path = resolve_output_path(context.input_path, output_dir)
    final = finalize(_ensure_watermark(context), output_path)
    context.output_path = final
    _logger.info("出力完了: %s", final)
    return final
