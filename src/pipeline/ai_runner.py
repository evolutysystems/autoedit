# 経路 D — EditPlan から編集済みプロジェクトを作る (docs/AiEditor/resolve1.md §11.2)
#
# この段では **AI を呼ばない**。手書き (またはすでに手元にある) EditPlan を食わせて
# 「plan → 素材 → Timeline → 保存」まで通す (§21 Phase 1 の 7 番)。
# AI 側の品質とエンジン側の不具合を切り分けられるようにするためである。
#
# 工程 (§11.2 の [1]〜[5] に対応する。[1] の採点と [3][4] の AI 呼び出しは後の段):
#   ① 事前確認    ffmpeg の有無 / 出力キャンバスの解決
#   ② 検証        plan_validator (VOD の尺と設定の上限に照らす)
#   ③ 素材        plan のシーンごとに cut_region → 正規化 → 編集点検出 → 文字起こし
#   ④ Timeline    archive/timeline_builder.build_archive_timeline (plan の順で並ぶ)
#   ⑤ 適用        plan_applier (既存コマンド経由。Undo が効く)
#   ⑥ 保存        project_io.save + <project>.plan.json
#   ⑦ 縦          縦ターゲットなら縦プロジェクトも作る (元 VOD が要る)
#
# 工程 [6] (レンダリング・出力) より後ろは経路 A/B/C と**完全に同一**であり、
# ここでは行わない。保存したプロジェクトを既存の経路が開いて書き出す。
#
# 素材は <プロジェクト名>.media へ置く (既存の規約 / project_io.media_dir_path)。
# 一時フォルダへ置くと、保存したプロジェクトを開いたときに素材が無い。
import os
import tempfile
import time

from ..ai import (context_builder, plan_applier, plan_schema, plan_validator,
                  planner, profile as ai_profile, review_report, reviewer,
                  scene_finder, scene_scanner)
from ..ai import config as ai_config
from ..archive import clip_writer, config as archive_config, vod_rebase
from ..archive import pipeline as archive_pipeline
from ..archive import timeline_builder as archive_timeline
from ..exceptions import AiPlanError, AutoEditError, InputError, PipelineCancelled
from ..i18n import tr
from ..modules import ffmpeg_runner, output_profile
from ..timeline import crop, project_io, vertical_builder
from ..timeline.builder import timeline_config
from ..timeline.commands import CommandStack
from ..utils import stage_timer
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 進捗の割り当て (合計 1.0)。素材の用意が最も長い。
_RATIO_VALIDATE = 0.05
_RATIO_PREPARE = 0.70
_RATIO_BUILD = 0.80
_RATIO_APPLY = 0.90
_RATIO_SAVE = 1.0


# 1 回の実行の結果。
class AiRunResult:

    def __init__(self, project_path="", plan_path="", timeline=None, report=None,
                 prepared=None, media_dir="", vertical_path="", stack=None,
                 base_path="", report_path="", report_html="", review=None):
        self.project_path = str(project_path or "")
        self.plan_path = str(plan_path or "")
        self.timeline = timeline
        self.report = report
        self.prepared = list(prepared or [])
        self.media_dir = str(media_dir or "")
        # 縦ターゲットのときに作った縦プロジェクト ("" = 作れなかった / 作らない)
        self.vertical_path = str(vertical_path or "")
        # 適用に使った履歴 (画面へ渡すと AI の 1 手も Undo できる)
        self.stack = stack
        # AI 版のスナップショット (§14.1 の <project>.ai-base.json)
        self.base_path = str(base_path or "")
        # レビューの読み物 (HTML)。PDF 化は gui/ai_report.py が行う (§12.6)
        self.report_path = str(report_path or "")
        self.report_html = str(report_html or "")
        # reviewer.ReviewResult (レビューを回していなければ None)
        self.review = review


# EditPlan を 1 本のプロジェクトへ落とす。
#
# input_path   : 元 VOD (またはローカル長尺動画) のパス
# plan         : EditPlan (plan_schema の形。検証はここで行う)
# target       : 出力先。省略時は plan の値
# project_path : 保存先。省略時は project_io.default_project_path
# duration_sec : 元 VOD の尺。0 なら ffprobe で測る
# progress_cb  : progress_cb(ratio 0..1, label)
# cancel_cb    : True を返すと PipelineCancelled
# prepare      : 素材を用意する呼び出し (テストで差し替える)
# vertical     : 縦ターゲットのときに縦プロジェクトも作るか
# curve        : 採点グラフの窓スコア列 (archive_pipeline.analyze の "curve")。
#                source.archive.curve へ残さないと、保存したプロジェクトを開いた
#                ときに採点グラフの折れ線が出ない
#                (docs/error/20261125/resolve.md §4 R3)。
def run_plan(input_path, settings, plan, target=None, project_path="",
             duration_sec=0.0, progress_cb=None, cancel_cb=None,
             vod_source=None, vod_scan=None, stack=None, media_dir="",
             prepare=None, vertical=True, rebase_plan=None, vertical_build=None,
             report=None, curve=None):
    stage_timer.reset()
    started_at = time.monotonic()
    if not input_path or not os.path.exists(input_path):
        raise InputError(tr("入力の動画が見つかりません。{path}").format(path=input_path))

    ffmpeg_runner.ensure_available((settings or {}).get("ffmpeg", {}))
    # 出力キャンバスを素材等倍へ解決し、実行用の設定コピーへ書き戻す (既存と同じ作法)。
    # setting.json の保存値は変えない。
    settings = output_profile.apply_canvas_to_settings(settings, input_path=input_path)
    ffmpeg_cfg = settings.get("ffmpeg", {})
    chosen = target if target in plan_schema.TARGETS else _plan_target(plan)

    # ---- ② 検証 (素材を作る前に行う。使えない指示のために ffmpeg を回さない)
    _notify(progress_cb, _RATIO_VALIDATE, tr("AI の編集計画を確認しています"))
    duration = float(duration_sec or 0.0)
    if duration <= 0:
        duration = float(ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg) or 0.0)
    with stage_timer.timed("ai_validate"):
        validated, checked = plan_validator.validate(
            plan, duration_sec=duration, settings=settings, target=chosen)
    # 呼び出し側が記録を持っているなら (run_ai) そこへ足す。件数を分けて数えない。
    if report is None:
        report = checked
    else:
        report.extend(checked.notes)
    scenes = validated["scenes"]

    project_path = project_path or _default_project_path(settings, input_path, chosen)
    media_dir = media_dir or project_io.media_dir_path(
        project_path, project_suffix=timeline_config(settings)["project_suffix"])
    os.makedirs(media_dir, exist_ok=True)

    # ---- ③ 素材 (plan の順に用意する。並べ替えのコマンドを使わないため / plan_applier ②)
    clip_pipe = archive_config.clip_pipeline_config(settings) \
        if hasattr(archive_config, "clip_pipeline_config") else {}
    clip_settings = clip_writer.build_clip_settings(settings, media_dir, clip_pipe)
    make = prepare if prepare is not None else clip_writer.prepare_one_clip
    prepared = []
    for position, scene in enumerate(scenes):
        _check_cancel(cancel_cb)
        _notify(progress_cb,
                _RATIO_VALIDATE + (_RATIO_PREPARE - _RATIO_VALIDATE)
                * (position / max(len(scenes), 1)),
                tr("素材を用意しています ({done}/{total})").format(
                    done=position + 1, total=len(scenes)))
        with stage_timer.timed("ai_prepare"):
            prepared.append(make(
                input_path, settings, clip_settings,
                {"index": position, "start": scene["source_start"],
                 "end": scene["source_end"], "score": 0.0},
                ffmpeg_cfg, media_dir, use_timeline=True))
    if not prepared:
        raise AiPlanError(tr("EditPlan に使えるシーンがありません。"))

    # ---- ④ Timeline
    _check_cancel(cancel_cb)
    _notify(progress_cb, _RATIO_BUILD, tr("Timeline を組んでいます"))
    with stage_timer.timed("ai_build"):
        timeline = archive_timeline.build_archive_timeline(
            prepared, clip_settings, source_path=input_path, curve=curve,
            vod_source=vod_source, vod_analysis=vod_scan)

    # ---- ⑤ 適用
    _check_cancel(cancel_cb)
    _notify(progress_cb, _RATIO_APPLY, tr("AI の指示を適用しています"))
    stack = stack if stack is not None else CommandStack()
    with stage_timer.timed("ai_apply"):
        timeline, report = plan_applier.apply_plan(
            timeline, validated, stack=stack, settings=settings, report=report)

    # ---- ⑥ 保存
    _notify(progress_cb, _RATIO_SAVE, tr("プロジェクトを保存しています"))
    project_io.save(timeline, project_path, generator="ai_runner",
                    project_path=project_path)
    plan_path = plan_schema.plan_path_for(project_path)
    plan_schema.save(validated, plan_path)
    # AI 版のスナップショット (§14.1)。**適用直後に 1 回だけ**取る。
    # これが無いと「人間が直したのか AI がそう作ったのか」が後から分からない。
    base_path = plan_schema.base_path_for(project_path)
    project_io.save(timeline, base_path, generator="ai_runner",
                    project_path=project_path)
    stack.mark_clean()

    result = AiRunResult(project_path=project_path, plan_path=plan_path,
                         timeline=timeline, report=report, prepared=prepared,
                         media_dir=media_dir, stack=stack)
    result.base_path = base_path

    # ---- ⑦ 縦 (縦ターゲットのときだけ)
    if vertical and chosen in plan_schema.VERTICAL_TARGETS:
        result.vertical_path = _make_vertical(
            timeline, validated, settings, project_path, report,
            rebase_plan=rebase_plan, vertical_build=vertical_build)

    stage_timer.report("AI 編集", total_sec=time.monotonic() - started_at)
    message = report.message()
    _logger.info("経路 D 完了: %s (シーン %d%s)", project_path, len(scenes),
                 " / " + message if message else "")

    return result


# ---- ⑦ 縦プロジェクト ---------------------------------------------------
#
# 縦は「切り抜きの指定を持った別プロジェクト」である (切り抜き済み動画は作らない)。
# 元 VOD を参照するため、VOD が手元に無ければ作らずに記録だけ残す
# (横のプロジェクトは完成しているので、後から作り直せる)。
#
# 切り抜きの枠は素材ごとに 1 つしか持てない (crop.make_layout は media 1 件ぶん)。
# plan のシーンごとの crop.mode は**最も多いもの**へ寄せる。
def _make_vertical(timeline, plan, settings, project_path, report,
                   rebase_plan=None, vertical_build=None):
    build = vertical_build if vertical_build is not None else vertical_builder.build
    cfg = crop.config(settings)

    # 素材の持ち方は既定で「複製して自己完結」(crop.MEDIA_COPY) である。
    # 素材は <プロジェクト名>.media に残るため、張り替えは要らない。
    # media_mode="rebase" のときだけ元 VOD へ張り替える (画面と同じ判断)。
    rebase = None
    if cfg["media_mode"] == crop.MEDIA_REBASE:
        planner = rebase_plan if rebase_plan is not None else vod_rebase.plan
        try:
            rebase = planner(timeline, settings)
        except AutoEditError as e:
            report.add("A8", "vertical", plan_schema.ACTION_DROP, "no vod")
            _logger.warning("縦プロジェクトを作れませんでした: %s", e)

            return ""

    media = rebase.get("media") if rebase else _dominant_media(timeline)
    clip_ids = [clip.id for clip in timeline.base_clips(include_disabled=True)]
    mode = _crop_mode(plan)
    layout = _layout_for(mode, media, settings, cfg)
    try:
        vertical, warnings = build(timeline, clip_ids, layout, settings,
                                   close_gaps=cfg["close_gaps"], rebase=rebase)
    except (ValueError, AutoEditError) as e:
        report.add("A8", "vertical", plan_schema.ACTION_DROP, "build")
        _logger.warning("縦プロジェクトを作れませんでした: %s", e)

        return ""
    for warning in warnings or []:
        _logger.info("縦プロジェクト: %s", warning)

    suffix = str(cfg.get("project_suffix", "_vertical") or "_vertical")
    dest = project_io.default_project_path(
        settings, project_path, timeline_cfg=timeline_config(settings),
        name_suffix=suffix)
    project_io.save(vertical, dest, generator="ai_runner", project_path=dest)
    report.add("A8", "vertical", plan_schema.ACTION_WRAP, mode)

    return dest


# plan のシーンで最も多い切り抜きの寄せ方。
def _crop_mode(plan):
    counts = {}
    for scene in plan.get("scenes") or []:
        mode = (scene.get("crop") or {}).get("mode") or plan_schema.DEFAULT_CROP_MODE
        counts[mode] = counts.get(mode, 0) + 1
    if not counts:
        return plan_schema.DEFAULT_CROP_MODE

    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]


# V1 で最も使われている素材 (切り抜きの枠は素材 1 件ぶんしか持てない)。
def _dominant_media(timeline):
    counts = {}
    for clip in timeline.base_clips(include_disabled=True):
        counts[clip.media_id] = counts.get(clip.media_id, 0) + clip.duration
    if not counts:
        return None
    media_id = sorted(counts.items(), key=lambda pair: -pair[1])[0][0]

    return timeline.media_by_id(media_id)


# 寄せ方から縦 1 枠の切り抜きを作る。
# 素材の高さをそのまま使い、幅は縦キャンバスの比 (既定 9:16) で取る。
def _layout_for(mode, media, settings, cfg):
    width = int(getattr(media, "width", 0) or 0)
    height = int(getattr(media, "height", 0) or 0)
    # 縦キャンバスは vertical_builder.build と同じ出どころから取る。
    profile = output_profile._portrait_profile((settings or {}).get("vertical", {}))
    canvas_w = int(profile["width"])
    canvas_h = int(profile["height"])
    if width <= 0 or height <= 0:
        return None

    frame_w = min(width, max(int(round(height * canvas_w / float(canvas_h))), 2))
    if mode == "left":
        x = 0
    elif mode == "right":
        x = width - frame_w
    else:
        # center / face は中央。face (顔追従) は blur の検出が要るため後の段 (§9.4)。
        x = max((width - frame_w) // 2, 0)

    return crop.make_layout(crop.MODE_SINGLE, [(x, 0, frame_w, height)], media,
                            canvas_w, canvas_h, background=cfg["background"])


# ---- 小物 ----------------------------------------------------------------

def _plan_target(plan):
    project = plan.get("project") if isinstance(plan, dict) else None
    value = project.get("target") if isinstance(project, dict) else None

    return value if value in plan_schema.TARGETS else plan_schema.TARGET_YOUTUBE_LONG


# 保存先 (既存の規約どおり <入力名><接尾辞>。ターゲットを名前へ入れて取り違えを防ぐ)
def _default_project_path(settings, input_path, target):
    return project_io.default_project_path(
        settings, input_path, timeline_cfg=timeline_config(settings),
        name_suffix=".ai_" + str(target))


def _notify(progress_cb, ratio, label):
    if progress_cb is None:
        return
    try:
        progress_cb(float(ratio), label)
    except Exception:  # noqa: BLE001 (進捗の失敗で本処理を止めない)
        _logger.exception("進捗コールバックで例外発生")


def _check_cancel(cancel_cb):
    if cancel_cb is None:
        return
    try:
        requested = bool(cancel_cb())
    except Exception:  # noqa: BLE001 (中止判定の失敗で処理を壊さない)
        _logger.exception("cancel_cb コールバックで例外発生")

        return
    if requested:
        raise PipelineCancelled(tr("AI の編集を中止しました"))


# ---- AI に決めさせる経路 (§11.2 の [1]〜[5] 全部) ------------------------

# 1 回の実行の結果 (ターゲットごとのプロジェクトをまとめて持つ)。
class AiAutoResult:

    def __init__(self, bundle=None, scenes=None, report=None, runs=None,
                 eval_path=""):
        self.bundle = bundle
        self.scenes = list(scenes or [])
        self.report = report
        # ターゲットごとの AiRunResult
        self.runs = list(runs or [])
        self.eval_path = str(eval_path or "")

    @property
    def project_paths(self):
        return [run.project_path for run in self.runs]


# 解析 → 候補の文字起こし → AI のシーン評価 → 構成 → 編集指示 → プロジェクト。
#
# provider が None なら AI を呼ばず、既存の採点だけで構成する (経路 B 相当まで縮退)。
# analysis を渡すと採点をやり直さない (画面が既に採点済みのときに使う)。
#
# targets   : ["youtube_long", "youtube_shorts"] など。省略時は ai.plan.targets
# comments  : comment_source.normalize_comments 済みのコメント
# source    : {"video_id","title","game","width","height","fps"} (分かるものだけ)
# analyze / transcribe / prepare : 工程の差し替え口 (テストは ffmpeg を呼ばない)
def run_ai(input_path, settings, provider=None, targets=None, analysis=None,
           comments=None, source=None, duration_sec=0.0, progress_cb=None,
           cancel_cb=None, vod_source=None, prepare=None, transcribe=None,
           analyze=None, vertical=True, reuse_eval=True):
    cfg = ai_config.ai_config(settings)
    chosen = [target for target in (targets or cfg["plan"]["targets"])
              if target in plan_schema.TARGETS]
    if not chosen:
        raise AiPlanError(tr("EditPlan の target が不正です。"))

    # ① 採点 (docs/AiEditor/resolve2.md)。
    # 機械測定はそのまま。AI のスイープを**関数として**渡し、山を候補へ混ぜる。
    # ai.scan.ai_weight = 0 の間は混ざらず、従来と同じ結果になる (§6.1)。
    _check_cancel(cancel_cb)
    scoring_settings = context_builder.analysis_settings(settings)
    if analysis is None:
        _notify(progress_cb, 0.05, tr("配信を解析しています"))
        runner = analyze if analyze is not None else archive_pipeline.analyze
        analysis = runner(input_path, scoring_settings,
                          lambda ratio, label: _notify(
                              progress_cb, 0.05 + ratio * 0.35, label),
                          comments, None,
                          scan=_scan_runner(settings, provider, cancel_cb))
    duration = float(duration_sec or analysis.get("duration", 0.0) or 0.0)

    bundle = context_builder.build(analysis, settings, source=source,
                                   comments=comments)
    if bundle.moments:
        _logger.info("AI の山: %d 件 (採点へ混ぜた比率 %.2f)",
                     len(bundle.moments), archive_pipeline.ai_scan_weight(settings))
    if not bundle.candidates:
        raise AiPlanError(tr("EditPlan に使えるシーンがありません。"))

    # ② 候補だけ文字起こしする (§5.3)
    _check_cancel(cancel_cb)
    _notify(progress_cb, 0.42, tr("候補を文字起こししています"))
    with tempfile.TemporaryDirectory(prefix="ai_transcribe_",
                                     ignore_cleanup_errors=True) as workdir:
        # transcribe を渡すと文字起こしの工程ごと差し替える (テストと別経路のため)。
        transcriber = (transcribe if transcribe is not None
                       else context_builder.transcribe_candidates)
        with stage_timer.timed("ai_transcribe"):
            bundle.transcript = transcriber(
                input_path, settings, bundle.candidates, workdir,
                duration_sec=duration,
                progress_cb=lambda ratio, done, total: _notify(
                    progress_cb, 0.42 + ratio * 0.18,
                    tr("候補を文字起こししています ({done}/{total})").format(
                        done=done, total=total)),
                cancel_cb=cancel_cb)

    # ③ AI のシーン評価 (失敗したバッチは既存の採点へ落ちる / §7.2)
    _check_cancel(cancel_cb)
    _notify(progress_cb, 0.62, tr("シーンを評価しています"))
    # 評価の保存先は最初のターゲットのプロジェクトの隣 (<project>.ai.json / §7.2)。
    primary = _default_project_path(settings, input_path, chosen[0])
    with stage_timer.timed("ai_scene_eval"):
        scenes, report = scene_finder.evaluate(
            bundle, provider, settings, use=planner.use_of(chosen[0]),
            project_path=primary, reuse=reuse_eval,
            progress_cb=lambda ratio, done, total: _notify(
                progress_cb, 0.62 + ratio * 0.10, tr("シーンを評価しています")),
            cancel_cb=cancel_cb)

    # ④⑤ ターゲットごとに構成 → 編集指示 → プロジェクト (§8.1 / §11.2)
    runs = []
    for position, target in enumerate(chosen):
        _check_cancel(cancel_cb)
        base = 0.75 + position * (0.25 / max(len(chosen), 1))
        _notify(progress_cb, base, tr("構成を組んでいます"))
        ranked = scene_finder.rank(scenes, settings, use=planner.use_of(target))
        # 編集プロファイルは**参考**として添える (§8.2 / §13.4)。未設定なら使わない。
        bundle.profile = ai_profile.for_prompt(
            ai_profile.active(settings, target), settings)
        with stage_timer.timed("ai_plan"):
            plan, report = planner.build(bundle, scene_finder.top_scenes(ranked),
                                         provider, settings, target, report=report)
        if plan is None:
            report.add("P4", target, plan_schema.ACTION_DROP, "no plan")
            continue

        # レビューと修正 (§12)。既定は無効 (ai.review.enabled = false)。
        with stage_timer.timed("ai_review"):
            checked = reviewer.review(
                plan, bundle, ranked, provider, settings, target,
                input_path=input_path, workdir=tempfile.gettempdir(),
                report=report, cancel_cb=cancel_cb)
        plan = checked.plan

        run = run_plan(
            input_path, settings, plan, target=target, duration_sec=duration,
            progress_cb=lambda ratio, label, base=base: _notify(
                progress_cb, base + ratio * (0.25 / max(len(chosen), 1)), label),
            cancel_cb=cancel_cb, vod_source=vod_source, prepare=prepare,
            vertical=vertical, report=report,
            curve=analysis.get("curve") or [])
        run.review = checked
        _write_report(run, plan, bundle, ranked, checked, settings)
        runs.append(run)

    if not runs:
        raise AiPlanError(tr("EditPlan に使えるシーンがありません。"))
    _logger.info("AI の経路 D 完了: %s", ", ".join(run.project_path for run in runs))

    return AiAutoResult(bundle=bundle, scenes=scenes, report=report, runs=runs,
                        eval_path=plan_schema.eval_path_for(primary))


# 採点スイープを archive/pipeline へ渡す形にする (docs/AiEditor/resolve2.md §6.2)。
#
# pipeline は ai/ を import しない。**関数を渡す**ことで依存の向きを保つ。
# プロバイダーが無い (AI 無効・未ログイン) ときや設定で切ってあるときは None を返し、
# pipeline 側は従来どおり機械採点だけで動く。
def _scan_runner(settings, provider, cancel_cb=None):
    if provider is None or not ai_config.ai_config(settings)["scan"].get("enabled", True):
        return None

    def run(input_path, scoring_settings, cells, comments, duration):
        # 作業フォルダはここで作って片づける (フレームは送信後に消える)。
        with tempfile.TemporaryDirectory(prefix="ai_scan_",
                                         ignore_cleanup_errors=True) as workdir:
            found = scene_scanner.scan(
                input_path, settings, provider, cells=cells, comments=comments,
                duration_sec=duration, workdir=workdir, cancel_cb=cancel_cb)

        return [moment.as_dict() for moment in found]

    return run


# ---- レビューの読み物 (§12.6) -------------------------------------------
#
# **レビューを回していなくても出す** (AI が何を考えて組んだかの記録になる / §12.6)。
# ここでは HTML だけを書く。PDF 化は PySide6 を持つ gui/ai_report.py が行う
# (pipeline から gui を import しないため / §4 の依存の向き)。
def _write_report(run, plan, bundle, scenes, checked, settings):
    cfg = ai_config.ai_config(settings)["report"]
    if not cfg.get("enabled", True):
        return ""
    try:
        html = review_report.build_html(
            plan,
            # 構成の表は EditPlan から作る (VideoPlan は planner の中で消えるため。
            # 役割・区間・理由は EditPlan にも載っている / §12.6)
            video_plan=None, scenes=scenes,
            issues=checked.issues if checked else None,
            checks=checked.checks if checked else None,
            notes=run.report.notes if run.report else None,
            settings=settings, source=bundle.source if bundle else None)
        run.report_html = html
        run.report_path = review_report.write_html(
            html, review_report.report_path_for(settings, run.project_path, "html"))
    except Exception as e:  # noqa: BLE001 (読み物が作れなくても編集結果は残す)
        _logger.warning("レビューの読み物を作れませんでした: %s", e)

        return ""

    return run.report_path
