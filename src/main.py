# autoedit エントリーポイント
# 1クリック実行を行うCLI/関数インタフェースを提供する
# 設計書 11. 追加提案 - src/main.py に対応
import argparse
import glob
import os
import sys

# パッケージとして実行された場合と単独スクリプトとして実行された場合の両対応
if __package__ is None or __package__ == "":
    # 単独実行時: src ディレクトリをパスへ追加して相対 import を成立させる
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.pipeline.pipeline_runner import run_pipeline
    from src.settings.settings_window import load_settings
    from src.utils.logger import get_logger
else:
    from .pipeline.pipeline_runner import run_pipeline
    from .settings.settings_window import load_settings
    from .utils.logger import get_logger


# 対応動画拡張子 (settings_window.py の VIDEO_FILE_FILTER と整合)
_VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv")


# コンソールに進捗バーを描画する
def _console_progress(ratio, label):
    bar_length = 30
    filled = int(bar_length * ratio)
    bar = "#" * filled + "-" * (bar_length - filled)
    sys.stdout.write(f"\r[{bar}] {ratio * 100:5.1f}%  {label}")
    sys.stdout.flush()
    if ratio >= 1.0:
        sys.stdout.write("\n")


# 入力動画パスを解決する (指定ファイル or ディレクトリ内全動画)
def _resolve_inputs(input_arg, settings):
    if input_arg:
        targets = [input_arg]
    else:
        video_dir = settings.get("general", {}).get("video_directory", "")
        if not video_dir or not os.path.isdir(video_dir):
            raise SystemExit(
                "入力動画またはディレクトリが指定されていません "
                "(setting.json の general.video_directory も未設定)"
            )
        targets = []
        for ext in _VIDEO_EXTENSIONS:
            targets.extend(glob.glob(os.path.join(video_dir, f"*{ext}")))
        targets.sort()

    if not targets:
        raise SystemExit("処理対象動画が見つかりません")
    return targets


# エントリ
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="autoedit",
        description="フルテロップ動画編集を1クリックで実行するパイプライン",
    )
    parser.add_argument("input", nargs="?", default=None,
                        help="入力動画ファイル (未指定時は setting.json の video_directory 内を一括処理)")
    args = parser.parse_args(argv)

    settings = load_settings()
    log_cfg = settings.get("logging", {})
    logger = get_logger("autoedit",
                        log_dir=log_cfg.get("log_dir", "logs"),
                        level=log_cfg.get("level", "INFO"))

    targets = _resolve_inputs(args.input, settings)
    logger.info("処理対象: %d 件", len(targets))

    outputs = []
    for path in targets:
        logger.info("処理開始: %s", path)
        try:
            output = run_pipeline(path, settings, progress_cb=_console_progress)
            outputs.append(output)
            logger.info("処理完了: %s", output)
        except Exception:
            logger.exception("処理失敗: %s", path)

    print()
    print("=== 結果 ===")
    for out in outputs:
        print(f"  OK: {out}")
    if len(outputs) < len(targets):
        print(f"  失敗 {len(targets) - len(outputs)} 件 (ログ参照)")


if __name__ == "__main__":
    main()
