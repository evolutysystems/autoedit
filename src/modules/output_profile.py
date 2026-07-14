# 出力プロファイル解決モジュール (縦動画対応 / request14 / resolve14)
# 入力動画の表示寸法から向き(縦/横)を判定し、以降の全工程が参照する
# 出力プロファイル(キャンバス寸法・縦フラグ)を1回だけ解決する。
# 横(landscape)は既存挙動=1920x1080、縦(portrait)は YouTube Shorts / TikTok 向けの
# 9:16 キャンバスを採用する。判定・解析に失敗した場合は安全側=横として続行する。
from ..exceptions import AutoEditError
from ..utils.logger import get_logger
from . import ffmpeg_runner

_logger = get_logger(__name__)

# 縦動作が無効/失敗時のフォールバック既定 (横=landscape の標準寸法)
_DEFAULT_LANDSCAPE_WIDTH = 1920
_DEFAULT_LANDSCAPE_HEIGHT = 1080
# 縦(portrait)の既定キャンバス (9:16 / §8-1 確定: 1080x1920 固定)
_DEFAULT_PORTRAIT_WIDTH = 1080
_DEFAULT_PORTRAIT_HEIGHT = 1920


# 横(landscape)プロファイルを生成する
# 横は既存の ffmpeg.output_width/height をそのまま用い、挙動を変えない。
def _landscape_profile(ffmpeg_cfg):
    return {
        "is_portrait": False,
        "orientation": "landscape",
        "width": int(ffmpeg_cfg.get("output_width", _DEFAULT_LANDSCAPE_WIDTH)),
        "height": int(ffmpeg_cfg.get("output_height", _DEFAULT_LANDSCAPE_HEIGHT)),
    }


# 縦(portrait)プロファイルを生成する
# キャンバス寸法は vertical.output_width/height (既定 1080x1920) から取得する。
def _portrait_profile(vertical_cfg):
    return {
        "is_portrait": True,
        "orientation": "portrait",
        "width": int(vertical_cfg.get("output_width", _DEFAULT_PORTRAIT_WIDTH)),
        "height": int(vertical_cfg.get("output_height", _DEFAULT_PORTRAIT_HEIGHT)),
    }


# 入力動画から出力プロファイルを解決する (パイプライン開始時に1回だけ呼ぶ)
# 判定基準:
#   ・vertical.enabled=false        → 常に横 (縦対応の完全無効化)
#   ・height > width               → 縦 (portrait)
#   ・それ以外(正方形含む / §8-3)  → 横 (landscape)
# ffprobe 失敗時は横として続行し、パイプラインを止めない (§6 フォールバック)。
def resolve_output_profile(input_path, settings):
    ffmpeg_cfg = settings.get("ffmpeg", {})
    vertical_cfg = settings.get("vertical", {})

    # 縦対応が無効なら判定せず横で確定する
    if not vertical_cfg.get("enabled", True):
        _logger.info("縦動画対応が無効 (vertical.enabled=false) のため横で処理します")
        return _landscape_profile(ffmpeg_cfg)

    try:
        width, height = ffmpeg_runner.probe_dimensions(input_path, ffmpeg_cfg)
    except AutoEditError as e:
        # 寸法取得失敗時は安全側=横で続行する
        _logger.warning("動画寸法の取得に失敗したため横として処理します: %s", e)
        return _landscape_profile(ffmpeg_cfg)

    # 高さが幅を上回る場合のみ縦。正方形(等値)は横扱い (§8-3)。
    if height > width:
        profile = _portrait_profile(vertical_cfg)
        _logger.info(
            "縦動画と判定 (入力=%dx%d → 出力=%dx%d)",
            width, height, profile["width"], profile["height"],
        )
        return profile

    _logger.info("横動画と判定 (入力=%dx%d)", width, height)
    return _landscape_profile(ffmpeg_cfg)
