# 出力モジュール
# 設計書 5.5 output_writer.py に対応
import os
import shutil

from ..exceptions import DiskSpaceError, InputError
from ..i18n import tr
from ..utils import disk
from ..utils.logger import get_logger
from . import ffmpeg_runner, loudness_normalizer, watermark_overlay

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
        raise InputError(
            tr("中間ファイルが存在しません: {path}", path=intermediate_path))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # 同一ボリュームなら move、跨ぐなら copy+remove
    #
    # 跨ぐ場合は copy になるため、出力先の空きが尽きると**中途半端な出力**が残る
    # (docs/error/fromUser/resolve.md §6-9)。再生できない動画を成果物として
    # 残さないよう、失敗したら必ず消してから原因を伝える。
    try:
        shutil.move(intermediate_path, output_path)
    except OSError as e:
        _remove_quietly(output_path)
        if disk.is_no_space(e):
            folder = os.path.dirname(os.path.abspath(output_path))
            raise DiskSpaceError(
                disk.shortage_message(folder, disk.free_bytes(folder),
                                      _size_or_zero(intermediate_path)),
                path=folder, free=disk.free_bytes(folder)) from e
        raise
    return output_path


def _size_or_zero(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# 書きかけの出力を片づける (消せなくても処理は続ける)
def _remove_quietly(path):
    try:
        if os.path.exists(path):
            os.remove(path)
            _logger.warning("書きかけの出力を削除しました: %s", path)
    except OSError:
        _logger.warning("書きかけの出力を削除できませんでした: %s", path)


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
    # 配置する直前に出力先の空きを確かめる (resolve.md §6-8)。
    # ここまで来て失敗すると、長い処理をやり直すことになるため必要量で測る。
    video_path = context.current_video_path()
    disk.ensure_free(os.path.dirname(os.path.abspath(output_path)),
                     _size_or_zero(video_path), label="出力先")
    final = finalize(_verify_loudness(context, _ensure_watermark(context)), output_path)
    context.output_path = final
    _logger.info("出力完了: %s", final)
    return final


# 最終成果物のラウドネスを測り、目標から外れていれば補正する
# (F10 / docs/quality/resolve1.md §4.3 F10)。
# 正規化は「本編の素材」に掛けるため、無音カット・OP/ED 結合・オーバーレイを
# 通した最終出力は従来一度も測られていなかった。
# 測定・補正の失敗で成果物を失わないよう、必ず有効なパスを返す。
def _verify_loudness(context, video_path):
    try:
        corrected = context.allocate_intermediate("loudness_verified.mp4")
        return loudness_normalizer.verify_and_correct(
            video_path, corrected, context.settings,
            on_progress=context.progress_subcallback("音量の最終確認"))
    except DiskSpaceError:
        raise      # 容量不足は継続しない (resolve.md §6-9)
    except Exception:  # noqa: BLE001 (確認・補正の失敗で成果物を失わない)
        _logger.exception("最終出力の音量確認に失敗 (そのまま出力します)")
        return video_path
