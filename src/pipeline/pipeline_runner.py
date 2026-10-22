# パイプラインオーケストレーション
# 設計書 5.1 pipeline_runner.py に対応
import os
import tempfile
import time

from ..exceptions import AutoEditError, FFmpegError, InputError, PipelineCancelled
from ..i18n import tr
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
from ..services import points as points_service
from ..services import telemetry
from ..settings.settings_window import save_settings
from ..timeline import builder as timeline_builder
from ..timeline import media_recovery, project_io, renderer
from ..utils import disk
from ..utils.logger import get_logger
from ..version import __version__
from .pipeline_context import PipelineContext

_logger = get_logger(__name__)

# 解析・保存値ともに得られない場合の最終フォールバック閾値 (dB)
# 通常は settings が DEFAULT_SETTINGS とマージ済みのため到達しない安全弁。
_DEFAULT_CUT_DB = -28


# 出力の開始時に予約し、透かしの要否をコンテキストへ載せる (ver5 resolve §5.4)。
# points を注入しない呼び出し (CLI / テスト) では何もせず、透かしも入らない。
# 要否はサーバーの応答だけで決まり、残高からは推測しない (R9)。
def _reserve(points, context):
    reservation = points_service.reserve(
        points, points_service.JOB_CLIP, points_service.OUTPUT_VIDEO)
    context.watermark_required = points_service.watermark_required(reservation)
    return reservation


# 出力 1 本の結果をログへ積む (request1 / resolve1 §7.3)。
# 成功・失敗・キャンセルのすべてで呼ぶ。例外は telemetry 側で握るため戻り値は見ない。
def _log_export(context, settings, started_at, success, cancelled=False, error=None):
    telemetry.export_finished(
        context.project_id, success, (time.monotonic() - started_at) * 1000.0,
        settings=settings, output_profile=context.output_profile, cancelled=cancelled)
    if error is not None:
        telemetry.report_error("pipeline_runner", error)


# 1クリック実行のエントリ
# input_path                : 入力動画パス
# settings                  : setting.json から読み込んだ辞書
# progress_cb               : (ratio: float, label: str) -> None
# subtitle_review_callback  : 字幕編集画面フック (GUI 実行時のみ。None でレビュー無し)
# volume_analysis_callback  : 音量解析の閾値確認フック (GUI 実行時のみ。None でダイアログ無し)
# points                    : PointsService (GUI 実行時のみ注入。None でポイント処理なし)
# 戻り値                    : 出力動画パス
def run_pipeline(input_path, settings, progress_cb=None, subtitle_review_callback=None,
                 volume_analysis_callback=None, timeline_review_callback=None,
                 blur_failure_callback=None, points=None, watermark_confirm_callback=None):
    _logger.info("=" * 50)
    _logger.info("パイプライン開始: %s", input_path)

    if not input_path or not os.path.exists(input_path):
        raise InputError(tr("入力動画が見つかりません: {path}", path=input_path))

    ffmpeg_cfg = settings.get("ffmpeg", {})
    ffmpeg_runner.ensure_available(ffmpeg_cfg)
    # 工程に入る前に「読める動画か」「置き場があるか」を確かめる
    # (docs/error/fromUser/resolve.md §6-8・§6-12-4)。
    # 従来は存在確認しかしておらず、読めない動画は音量解析まで進んでから
    # 「ffprobe が失敗」という分かりにくい形で落ちていた。
    _ensure_readable_input(input_path, settings)
    _ensure_space_for_run(input_path, settings)

    use_timeline = is_timeline_mode(settings)
    context = _prepare_context(input_path, settings, progress_cb, subtitle_review_callback,
                               volume_analysis_callback, timeline_review_callback,
                               use_timeline,
                               blur_failure_callback=blur_failure_callback)
    # 出力プロファイル(縦/横)を入力動画から1回だけ解決し、以降の全工程で共有する (request14)
    context.output_profile = output_profile.resolve_output_profile(input_path, settings)
    # ログ送信 (request1 / resolve1 §7.2)。projectId を決めて Projects を積む。
    # 送信は行わず、ファイルを 1 本書くだけなのでオフラインでも失敗しない。
    context.project_id = telemetry.register_project(
        input_path, settings, output_profile=context.output_profile)
    started_at = telemetry.export_started(context.project_id)
    # ポイントの予約 (出力の開始時 / ver5 resolve §3.2)
    reservation = _reserve(points, context)
    # 透かしが入るなら開始前に確認する (R12)。やめるなら消費せず中断する。
    if not points_service.confirmed(reservation, watermark_confirm_callback):
        points_service.cancel(points, reservation)
        _log_export(context, settings, started_at, False, cancelled=True)
        raise PipelineCancelled(tr("透かし入りでの出力を取りやめました"))
    try:
        if use_timeline:
            # ver3: 編集点方式 + Timeline 編集画面 + Timeline レンダリング
            output_path = _run_timeline(context)
        else:
            # 従来フロー (実カット + 字幕編集画面 + OP/ED 後段結合)
            output_path = _run_legacy(context)
        _logger.info("パイプライン正常終了: %s", output_path)
        # 成果物ができてから確定する (R3)
        points_service.commit(points, reservation)
        _log_export(context, settings, started_at, True)
        return output_path

    except PipelineCancelled:
        # ユーザーが字幕編集画面でキャンセルした場合 (異常終了ではない)
        _logger.info("パイプライン中断: ユーザーによるキャンセル")
        points_service.cancel(points, reservation)
        _log_export(context, settings, started_at, False, cancelled=True)
        raise
    except AutoEditError as e:
        _logger.exception("パイプライン中断: 既知エラー")
        points_service.cancel(points, reservation)
        _log_export(context, settings, started_at, False, error=e)
        raise
    except Exception as e:  # noqa: BLE001 (記録して再送出する)
        _logger.exception("パイプライン中断: 想定外エラー")
        points_service.cancel(points, reservation)
        _log_export(context, settings, started_at, False, error=e)
        raise
    finally:
        _cleanup(context)


# 保存済みプロジェクトから編集を再開して書き出す (ver3 resolve7 §3-4 / §5.10)
# 新しい編集画面もレンダラも作らず、既存パイプラインの後半だけを走らせる。
# 前半 (正規化・無音検出・音声認識・Timeline 構築) は保存済みの内容で置き換わる。
#
# project_path         : <元動画名>.timeline.json
# timeline_review_callback : Timeline 編集画面フック (GUI 実行時のみ)
# media_relink_callback    : 見つからない素材の差し替えフック (GUI 実行時のみ)
# restore_path         : 読み込むファイルだけを差し替える (自動保存からの復元 / §5.9)。
#                        保存先は project_path のままにするため、復元して「決定」しても
#                        上書きは本体のプロジェクトファイルへ行く。
# points               : PointsService (GUI 実行時のみ注入。None でポイント処理なし)
# 戻り値               : 出力動画パス
def run_from_project(project_path, settings, progress_cb=None,
                     timeline_review_callback=None, media_relink_callback=None,
                     restore_path=None, blur_failure_callback=None, points=None,
                     watermark_confirm_callback=None):
    _logger.info("=" * 50)
    _logger.info("保存済みプロジェクトから再開: %s", project_path)

    if not project_path or not os.path.exists(project_path):
        raise InputError(
            tr("プロジェクトファイルが見つかりません: {path}", path=project_path))

    ffmpeg_cfg = settings.get("ffmpeg", {})
    ffmpeg_runner.ensure_available(ffmpeg_cfg)

    cfg = timeline_builder.timeline_config(settings)
    load_path = restore_path or project_path
    if restore_path:
        _logger.info("自動保存から復元します: %s", restore_path)
    # 検証は素材の復旧より後に行う。先に走らせると、消えた中間ファイルを参照する
    # クリップが片っ端から無効化されてしまう (resolve7 §5.8)。
    timeline, meta = project_io.load_project(
        load_path, min_clip_sec=cfg["min_clip_sec"], validate_timeline=False)

    input_path = str((timeline.source or {}).get("input_path", "") or "")
    if not input_path:
        raise InputError(
            tr("プロジェクトに元動画のパスが記録されていないため開けません"))
    # 種別違いをここで弾く (ver3 resolve9 §3-4)。アーカイブ切り抜き用は素材の復旧も
    # 書き出し方も違うため、この経路へ通すと空の Timeline を書き出してしまう。
    if project_io.project_kind(timeline) == project_io.KIND_ARCHIVE:
        raise InputError(
            tr("アーカイブ切り抜き用のプロジェクトです。"
               "「アーカイブ切り抜き用」タブの「編集の続き」から開いてください。"))

    # 工程は「素材の復旧」と「Timeline レンダリング」の 2 つ
    context = PipelineContext(
        input_path=input_path,
        settings=settings,
        progress_callback=progress_cb,
        # 再編集も中間ファイルを作るため、置き場の設定は同じく効かせる (§6-7-1)
        working_dir=_work_directory(settings),
        total_steps=2,
        timeline_review_callback=timeline_review_callback,
        blur_failure_callback=blur_failure_callback,
    )
    # 出力プロファイルは Timeline のキャンバスから復元する (保存時と同じ寸法で出す)
    context.output_profile = {
        "is_portrait": timeline.orientation == "portrait",
        "orientation": timeline.orientation,
        "width": timeline.width,
        "height": timeline.height,
    }
    context.project_path = project_path
    # 上書き保存で初回作成時刻を引き継ぐため覚えておく
    context.project_created_at = meta.get("created_at")
    context.timeline = timeline

    # ログ送信 (request1 / resolve1 §7.2)。projectId はプロジェクトファイルに
    # 紐づけて覚えるため、再開を何度しても Projects は 1 行のまま (upsert) になる。
    context.project_id = telemetry.register_project(
        input_path, settings, project_path=project_path,
        output_profile=context.output_profile)
    started_at = telemetry.export_started(context.project_id, resumed=True)
    # 再開でも出力は 1 本できるため、通常実行と同じく予約する (ver5 resolve §3.2)
    reservation = _reserve(points, context)
    if not points_service.confirmed(reservation, watermark_confirm_callback):
        points_service.cancel(points, reservation)
        _log_export(context, settings, started_at, False, cancelled=True)
        raise PipelineCancelled(tr("透かし入りでの出力を取りやめました"))
    try:
        # ① 素材の復旧 (足りなければ元動画から作り直す / 再リンクを求める)
        context.begin_step("素材の復旧")
        media_recovery.recover(timeline, project_path, settings, context,
                               relink_callback=media_relink_callback)
        project_io.validate(timeline, min_clip_sec=cfg["min_clip_sec"])
        context.end_step("素材の復旧")

        # ② Timeline 編集画面 (既存関数をそのまま使う。キャンセルは PipelineCancelled)
        timeline = _review_timeline(context, timeline, mode="resume")
        context.timeline = timeline

        # ③ レンダリング (OP/ED・オーバーレイ・字幕を含む / §8)
        context.begin_step("Timeline レンダリング")
        renderer.render(timeline, context)
        context.end_step("Timeline レンダリング")

        # ④ 出力 (最終ファイル配置。既存 output_writer をそのまま使う)
        output_path = output_writer.run(context)
        _logger.info("再編集の書き出し正常終了: %s", output_path)
        points_service.commit(points, reservation)
        _log_export(context, settings, started_at, True)
        return output_path

    except PipelineCancelled:
        _logger.info("再編集を中断: ユーザーによるキャンセル")
        points_service.cancel(points, reservation)
        _log_export(context, settings, started_at, False, cancelled=True)
        raise
    except AutoEditError as e:
        _logger.exception("再編集を中断: 既知エラー")
        points_service.cancel(points, reservation)
        _log_export(context, settings, started_at, False, error=e)
        raise
    except Exception as e:  # noqa: BLE001 (記録して再送出する)
        _logger.exception("再編集を中断: 想定外エラー")
        points_service.cancel(points, reservation)
        _log_export(context, settings, started_at, False, error=e)
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


# 中間ファイルの置き場を設定から解決する (空なら None = %TEMP%)
# (docs/error/fromUser/resolve.md §6-7-1)
def _work_directory(settings):
    folder = str((settings or {}).get("general", {}).get("work_directory", "") or "").strip()
    if not folder:
        return None
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        # 作れない指定 (外したドライブ等) は従来どおり %TEMP% へ倒す。
        # 設定の誤りで実行そのものができなくなる方が損が大きい。
        _logger.warning("作業フォルダを使えないため既定の一時フォルダを使います: %s", folder)
        return None
    return folder


# 入力動画が ffprobe で読めることを確かめる (resolve.md §6-12-4)
# 読めない理由 (無い / 0 バイト / 壊れている) はここで名指しして止める。
def _ensure_readable_input(input_path, settings):
    try:
        width, height = ffmpeg_runner.probe_dimensions(
            input_path, settings.get("ffmpeg", {}))
    except FFmpegError as e:
        raise InputError(
            tr("この動画を読み込めません: {path}\n{error}",
               path=input_path, error=e)) from e
    _logger.info("入力動画を確認しました: %dx%d / %s",
                 width, height, disk.human(_size_or_zero(input_path)))


# 実行に必要な空き容量を確かめる (resolve.md §6-8)
# 作業先と出力先が別ドライブのこともあるため、両方を見る。
def _ensure_space_for_run(input_path, settings):
    general = (settings or {}).get("general", {})
    required = disk.required_from_gb(general.get("min_free_space_gb", 20))
    if required <= 0:
        return
    work_dir = _work_directory(settings) or tempfile.gettempdir()
    disk.ensure_free(work_dir, required, label="作業フォルダ")
    output_dir = str(general.get("output_directory", "") or "").strip()
    if not output_dir:
        output_dir = os.path.dirname(os.path.abspath(input_path))
    if _same_drive(work_dir, output_dir):
        return      # 同じドライブなら 2 度測らない
    disk.ensure_free(output_dir, required, label="出力先")


def _same_drive(left, right):
    return (os.path.splitdrive(os.path.abspath(left))[0].lower()
            == os.path.splitdrive(os.path.abspath(right))[0].lower())


def _size_or_zero(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# コンテキストを準備する
def _prepare_context(input_path, settings, progress_cb, subtitle_review_callback=None,
                     volume_analysis_callback=None, timeline_review_callback=None,
                     use_timeline=False, blur_failure_callback=None):
    # 進捗の総工程数
    #   従来  : 音声解析・正規化, 無音カット, テロップ, OP結合, ED結合 の 5 (resolve22)
    #   ver3  : 音声解析・正規化, 無音検出, 音声認識, Timeline レンダリング の 4
    #           (OP/ED 結合はレンダリングに吸収されるため独立工程ではない)
    return PipelineContext(
        input_path=input_path,
        settings=settings,
        progress_callback=progress_cb,
        # 中間ファイルの置き場 (docs/error/fromUser/resolve.md §6-7-1)。
        # 空なら従来どおり %TEMP% を使うため、設定しなければ挙動は変わらない。
        working_dir=_work_directory(settings),
        total_steps=4 if use_timeline else 5,
        subtitle_review_callback=subtitle_review_callback,
        volume_analysis_callback=volume_analysis_callback,
        timeline_review_callback=timeline_review_callback,
        blur_failure_callback=blur_failure_callback,
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
        context.current_step / max(context.total_steps, 1), tr("音量解析中…"))
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
        context.current_step / max(context.total_steps, 1), tr("音量解析中…"))
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
# mode: "pipeline" (パイプラインの途中) / "resume" (保存済みを開き直した / resolve7 §5.10)
def _review_timeline(context, timeline, mode="pipeline"):
    callback = getattr(context, "timeline_review_callback", None)
    if callback is None:
        _logger.info("Timeline 編集画面なしで続行します (編集点をそのまま採用)")
        return timeline

    edited = callback({
        "timeline": timeline,
        "settings": context.settings,
        "project_path": context.project_path,
        # 上書き保存で初回作成時刻を引き継ぐため画面へ渡す (resolve7 §5.7)
        "created_at": getattr(context, "project_created_at", None),
        "mode": mode,
        "asr_audio_path": context.asr_audio_path(),
        # プレビューの一時ファイルは中間ファイルと同じ寿命にする (cleanup で消える)
        "working_dir": context.working_dir,
    })
    # 戻り値は Timeline (従来) / {"timeline","project_path"} (追加) のどちらでもよい。
    # 画面が「名前を付けて保存」で保存先を変えていたら、確定後の保存もそちらへ行う。
    if isinstance(edited, dict):
        context.project_path = edited.get("project_path") or context.project_path
        edited = edited.get("timeline")
    if edited is None:
        raise PipelineCancelled(
            tr("Timeline 編集がキャンセルされたためパイプラインを中断します"))

    # 確定後の状態を上書き保存する (記録用 / §6.2.1)
    if context.project_path:
        try:
            project_io.save(edited, context.project_path,
                            generator=f"Stretheus {__version__}",
                            created_at=getattr(context, "project_created_at", None))
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
