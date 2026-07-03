# パイプラインオーケストレーション
# 設計書 5.1 pipeline_runner.py に対応
import os

from ..exceptions import AutoEditError, InputError, PipelineCancelled
from ..modules import (
    concat_processor,
    ffmpeg_runner,
    output_writer,
    silence_cutter,
    subtitle_generator,
    volume_analyzer,
)
from ..settings.settings_window import save_settings
from ..utils.logger import get_logger
from .pipeline_context import PipelineContext

_logger = get_logger(__name__)

# 解析・保存値ともに得られない場合の最終フォールバック閾値 (dB)
# 通常は settings が DEFAULT_SETTINGS とマージ済みのため到達しない安全弁。
_DEFAULT_CUT_DB = -28


# 1クリック実行のエントリ
# input_path                : 入力動画パス
# settings                  : setting.json から読み込んだ辞書
# progress_cb               : (ratio: float, label: str) -> None
# subtitle_review_callback  : 字幕編集画面フック (GUI 実行時のみ。None でレビュー無し)
# volume_analysis_callback  : 音量解析の閾値確認フック (GUI 実行時のみ。None でダイアログ無し)
# 戻り値                    : 出力動画パス
def run_pipeline(input_path, settings, progress_cb=None, subtitle_review_callback=None,
                 volume_analysis_callback=None):
    _logger.info("=" * 50)
    _logger.info("パイプライン開始: %s", input_path)

    if not input_path or not os.path.exists(input_path):
        raise InputError(f"入力動画が見つかりません: {input_path}")

    ffmpeg_cfg = settings.get("ffmpeg", {})
    ffmpeg_runner.ensure_available(ffmpeg_cfg)

    context = _prepare_context(input_path, settings, progress_cb, subtitle_review_callback,
                               volume_analysis_callback)
    try:
        # （新規）音量解析・カット閾値の確認 (無音カットの前) ── resolve7
        context.progress_callback(0.0, "音量解析中…")
        _apply_volume_analysis(context)

        # ① 無音カット
        context.begin_step("無音カット")
        silence_cutter.run(context)
        context.end_step("無音カット")

        # ② フルテロップ生成 (ON/OFF対応)
        context.begin_step("フルテロップ生成")
        subtitle_generator.run(context)
        context.end_step("フルテロップ生成")

        # ③ オープニング結合 (未設定スキップ)
        context.begin_step("オープニング結合")
        concat_processor.run(context, "opening")
        context.end_step("オープニング結合")

        # ④ エンディング結合 (未設定スキップ)
        context.begin_step("エンディング結合")
        concat_processor.run(context, "ending")
        context.end_step("エンディング結合")

        # 出力 (最終ファイル配置)
        output_path = output_writer.run(context)
        _logger.info("パイプライン正常終了: %s", output_path)
        return output_path

    except PipelineCancelled:
        # ユーザーが字幕編集画面でキャンセルした場合 (異常終了ではない)
        _logger.info("パイプライン中断: ユーザーによるキャンセル")
        raise
    except AutoEditError:
        _logger.exception("パイプライン中断: 既知エラー")
        raise
    except Exception:
        _logger.exception("パイプライン中断: 想定外エラー")
        raise
    finally:
        _cleanup(context)


# コンテキストを準備する
def _prepare_context(input_path, settings, progress_cb, subtitle_review_callback=None,
                     volume_analysis_callback=None):
    # 進捗の総工程数は: 無音カット, テロップ, OP結合, ED結合 の 4
    return PipelineContext(
        input_path=input_path,
        settings=settings,
        progress_callback=progress_cb,
        total_steps=4,
        subtitle_review_callback=subtitle_review_callback,
        volume_analysis_callback=volume_analysis_callback,
    )


# 音量解析・カット閾値の確認を行い、確定dBを volume_analysis.last_cut_db へ反映する
# (resolve7 §3.2/§9-1)。無音カットはこの last_cut_db を単一閾値として参照する。
def _apply_volume_analysis(context):
    settings = context.settings
    va_cfg = settings.setdefault("volume_analysis", {})

    # 機能無効時は何もしない (従来挙動には戻らないが解析・ダイアログをスキップ)
    if not va_cfg.get("enabled", True):
        return

    # GUI ダイアログ未注入 (CLI/ヘッドレス) の場合は解析せず保存済み値を使用する (§9-6)
    callback = getattr(context, "volume_analysis_callback", None)
    if callback is None:
        return

    input_path = context.current_video_path()
    # 解析失敗時はダイアログを出さず既定値で無音カットへ進む (処理全体は止めない)
    try:
        analysis = volume_analyzer.analyze_min_speech_db(input_path, settings)
    except AutoEditError:
        _logger.exception("音量解析に失敗したため閾値確認ダイアログをスキップします")
        return

    measured = analysis.get("min_db")
    saved = va_cfg.get("last_cut_db")
    # ダイアログ初期値: 測定値 > 保存値 > フォールバック既定
    if measured is not None:
        initial = measured
    elif saved is not None:
        initial = saved
    else:
        initial = _DEFAULT_CUT_DB

    confirmed = callback({
        "initial_db": int(initial),
        "measured_db": measured,
        "region_count": analysis.get("region_count", 0),
    })

    if confirmed is not None:
        # OK: 確定値を採用し setting.json へ永続化する (§9-1)
        final_db = int(confirmed)
        va_cfg["last_cut_db"] = final_db
        try:
            save_settings(settings)
            _logger.info("カット閾値を更新し保存: %d dB", final_db)
        except OSError:
            _logger.exception("カット閾値の保存に失敗しました (当該実行はメモリ値で継続)")
    else:
        # 変更しない: 保存値 (無ければ初期値) を当該実行で使用する (保存はしない)
        va_cfg["last_cut_db"] = int(saved if saved is not None else initial)


# 中間ファイル後処理
# 失敗時はデバッグのため一時ディレクトリを保持する選択肢もあるが、
# デフォルトは tempfile が自動 cleanup する
def _cleanup(context):
    try:
        context.cleanup()
    except Exception:
        _logger.exception("中間ファイル後処理に失敗")
