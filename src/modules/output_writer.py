# 出力モジュール
# 設計書 5.5 output_writer.py に対応
import os
import shutil

from ..exceptions import InputError
from ..utils.logger import get_logger

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


# 公開エントリポイント
def run(context):
    settings = context.settings
    general_cfg = settings.get("general", {})
    output_dir = general_cfg.get("output_directory", "")

    output_path = resolve_output_path(context.input_path, output_dir)
    final = finalize(context.current_video_path(), output_path)
    context.output_path = final
    _logger.info("出力完了: %s", final)
    return final
