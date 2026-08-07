# パイプラインオーケストレーション
# 設計書 5.1 pipeline_runner.py に対応
import os

from ..exceptions import AutoEditError, InputError, PipelineCancelled
from ..modules import (
    concat_processor,
    ffmpeg_runner,
    loudness_normalizer,
    output_profile,
    output_writer,
    silence_cutter,
    subtitle_generator,
    volume_analyzer,
)
from ..settings.settings_window import save_settings
from ..timeline import builder as timeline_builder
from ..timeline import project_io, renderer
from ..utils.logger import get_logger
from ..version import __version__
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
                 volume_analysis_callback=None, timeline_review_callback=None):
    _logger.info("=" * 50)
    _logger.info("パイプライン開始: %s", input_path)

    if not input_path or not os.path.exists(input_path):
        raise InputError(f"入力動画が見つかりません: {input_path}")

    ffmpeg_cfg = settings.get("ffmpeg", {})
    ffmpeg_runner.ensure_available(ffmpeg_cfg)

    use_timeline = is_timeline_mode(settings)
    context = _prepare_context(input_path, settings, progress_cb, subtitle_review_callback,
                               volume_analysis_callback, timeline_review_callback,
                               use_timeline)
    # 出力プロファイル(縦/横)を入力動画から1回だけ解決し、以降の全工程で共有する (request14)
    context.output_profile = output_profile.resolve_output_profile(input_path, settings)
    try:
        if use_timeline:
            # ver3: 編集点方式 + Timeline 編集画面 + Timeline レンダリング
            output_path = _run_timeline(context)
        else:
            # 従来フロー (実カット + 字幕編集画面 + OP/ED 後段結合)
            output_path = _run_legacy(context)
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


# Timeline モード (ver3) で動作するかを判定する
# timeline.enabled=false のときは silence_cut.mode の値に関わらず従来フローとする
# (Timeline が無ければ編集点を反映する先が無いため / resolve.md §7.1)。
def is_timeline_mode(settings):
    if not (settings or {}).get("timeline", {}).get("enabled", True):
        return False
    mode = (settings or {}).get("silence_cut", {}).get("mode", "edit_points")
    return str(mode).strip().lower() == "edit_points"


# コンテキストを準備する
def _prepare_context(input_path, settings, progress_cb, subtitle_review_callback=None,
                     volume_analysis_callback=None, timeline_review_callback=None,
                     use_timeline=False):
    # 進捗の総工程数
    #   従来  : 音声解析・正規化, 無音カット, テロップ, OP結合, ED結合 の 5 (resolve22)
    #   ver3  : 音声解析・正規化, 無音検出, 音声認識, Timeline レンダリング の 4
    #           (OP/ED 結合はレンダリングに吸収されるため独立工程ではない)
    return PipelineContext(
        input_path=input_path,
        settings=settings,
        progress_callback=progress_cb,
        total_steps=4 if use_timeline else 5,
        subtitle_review_callback=subtitle_review_callback,
        volume_analysis_callback=volume_analysis_callback,
        timeline_review_callback=timeline_review_callback,
    )


# ------------------------------------------------------------------
# 従来フロー (silence_cut.mode="physical" / timeline.enabled=false)
# ------------------------------------------------------------------

def _run_legacy(context):
    # ⓪ 音声解析・正規化 (YouTube 向けラウドネス正規化。最初の工程) ── resolve22
    context.begin_step("音声解析・正規化")
    loudness_normalizer.run(context)
    context.end_step("音声解析・正規化")

    # 音量解析・カット閾値の確認 (無音カットの前。正規化後の音声に対して実施) ── resolve7
    context.progress_callback(
        context.current_step / max(context.total_steps, 1), "音量解析中…")
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
    return output_writer.run(context)


# ------------------------------------------------------------------
# Timeline フロー (ver3 / resolve.md §5.1)
# ------------------------------------------------------------------

def _run_timeline(context):
    settings = context.settings

    # ⓪ 音声解析・正規化 (映像は -c:v copy のため時間軸は元動画と同一)
    context.begin_step("音声解析・正規化")
    loudness_normalizer.run(context)
    context.end_step("音声解析・正規化")

    # 音量解析・カット閾値の確認 (従来と同一)
    context.progress_callback(
        context.current_step / max(context.total_steps, 1), "音量解析中…")
    _apply_volume_analysis(context)

    # ① 無音検出 (実カットせず編集点だけを求める / §7.2)
    context.begin_step("無音検出")
    keep_segments, edit_meta = silence_cutter.detect_edit_points(context)
    context.end_step("無音検出")

    # ② 音声認識 (残す区間の音声のみに対して実行 / §7.3)
    context.begin_step("音声認識")
    items, _eff_cfg = subtitle_generator.recognize_for_timeline(context, keep_segments)
    context.end_step("音声認識")

    # ③ Timeline 構築 (OP → 本編 → ED) とプロジェクト JSON 保存
    timeline = timeline_builder.build(
        context.input_path, context.current_video_path(), keep_segments, items,
        settings, profile=context.output_profile,
        source_duration=edit_meta.get("source_duration_sec"),
        edit_point_meta=edit_meta,
    )
    context.timeline = timeline
    context.project_path = _save_project(timeline, context)

    # ④ Timeline 編集画面 (GUI 経路のみ)。キャンセルは PipelineCancelled で中断する。
    timeline = _review_timeline(context, timeline)
    context.timeline = timeline

    # ⑤ レンダリング (OP/ED・オーバーレイ・字幕を含む / §8)
    context.begin_step("Timeline レンダリング")
    renderer.render(timeline, context)
    context.end_step("Timeline レンダリング")

    # ⑥ 出力 (最終ファイル配置。既存 output_writer をそのまま使う)
    return output_writer.run(context)


# プロジェクト JSON を保存する。失敗しても実行は止めない (§10)。
def _save_project(timeline, context):
    try:
        path = project_io.default_project_path(context.settings, context.input_path)
        return project_io.save(timeline, path, generator=f"Stretheus {__version__}")
    except Exception:  # noqa: BLE001 (記録用のため失敗しても続行する)
        _logger.exception("プロジェクトファイルの保存に失敗しました (処理は続行します)")
        return None


# Timeline 編集画面を開き、編集結果で置き換える (§5.1)
# コールバック未注入 (CLI/ヘッドレス) の場合は編集点をそのまま採用して自動レンダリングする。
def _review_timeline(context, timeline):
    callback = getattr(context, "timeline_review_callback", None)
    if callback is None:
        _logger.info("Timeline 編集画面なしで続行します (編集点をそのまま採用)")
        return timeline

    edited = callback({
        "timeline": timeline,
        "settings": context.settings,
        "project_path": context.project_path,
        "asr_audio_path": context.asr_audio_path(),
        # プレビューの一時ファイルは中間ファイルと同じ寿命にする (cleanup で消える)
        "working_dir": context.working_dir,
    })
    if edited is None:
        raise PipelineCancelled("Timeline 編集がキャンセルされたためパイプラインを中断します")

    # 確定後の状態を上書き保存する (記録用 / §6.2.1)
    if context.project_path:
        try:
            project_io.save(edited, context.project_path,
                            generator=f"Stretheus {__version__}")
        except Exception:  # noqa: BLE001
            _logger.warning("確定後のプロジェクト保存に失敗しました (処理は続行します)")
    return edited


# 音量解析・カット閾値の確認を行い、確定dBを volume_analysis.last_cut_db へ反映する
# (resolve7 §3.2/§9-1)。無音カットはこの last_cut_db を単一閾値として参照する。
def _apply_volume_analysis(context):
    settings = context.settings
    va_cfg = settings.setdefault("volume_analysis", {})

    # 無音カット無効時は閾値確認自体が不要のためスキップ (resolve12)
    if not settings.get("silence_cut", {}).get("enabled", True):
        return

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
