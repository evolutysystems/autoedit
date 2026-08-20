# アーカイブ切り抜き用プロジェクトの再編集 (docs/request/ver3/resolve9.md §5.5 / §5.7)
#
# 保存されたアーカイブ用 Timeline が参照する素材 (クリップごとの中間ファイル) は
# 実行の終わりに消えている。映像は VOD から同じ引数で切り直し、音声は保存して
# おいたサイドカーを貼り直して復元する (§3-1 案D)。どちらもコピーのため速い。
# サイドカーが無い旧プロジェクトは、正規化をやり直して復元する (案B)。
#
# 復元した Timeline は編集画面へ渡し、確定後は通常の切り抜きと同じ書き出し
# (clip_writer.finish_clips) を通す。出力の作られ方は通常実行と変わらない。
import os
import tempfile

from ..exceptions import InputError, PipelineCancelled
from ..modules import ffmpeg_runner, loudness_normalizer
from ..timeline import media_recovery, media_sidecar, project_io
from ..timeline.builder import timeline_config
from ..utils.logger import get_logger
from ..version import __version__
from . import clip_writer
from . import timeline_builder as archive_timeline

_logger = get_logger(__name__)


# アーカイブ切り抜き用のプロジェクトか
def is_archive_project(timeline):
    return project_io.project_kind(timeline) == project_io.KIND_ARCHIVE


# source.archive セクションを返す (無ければ空辞書)
def archive_section(timeline):
    return archive_timeline.archive_section(timeline)


# ------------------------------------------------------------------
# 素材の復旧 (§5.5)
# ------------------------------------------------------------------

# media_recovery.recover へ渡す復旧関数を作る
# workdir  : 復元先の作業ディレクトリ (クリップごとのサブフォルダを掘る)
# media_dir: 音声サイドカーの置き場 (<プロジェクト名>.media/。無ければ空文字)
# 戻り値: callable(media, source) -> 復旧したパス / None
def make_media_recover(timeline, settings, workdir, media_dir="", progress_cb=None,
                       stats=None):
    ffmpeg_cfg = settings.get("ffmpeg", {})
    project_cfg = timeline_config(settings)["project"]
    tolerance = float(project_cfg["sidecar_tolerance_sec"])
    policy = str(project_cfg["missing_media_policy"])
    clips = list(archive_section(timeline).get("clips") or [])
    total = max(len(clips), 1)
    counters = stats if isinstance(stats, dict) else {}
    counters.setdefault("from_sidecar", 0)
    counters.setdefault("renormalized", 0)
    counters.setdefault("raw", 0)
    done = [0]

    def recover_one(media, source):
        entry = next((c for c in clips if c.get("media_id") == media.id), None)
        if entry is None:
            return None                 # アーカイブのクリップ素材ではない (画像など)

        vod = _vod_path(source)
        if not vod:
            _logger.warning("元 VOD が見つからないため clip%s を復元できません",
                            entry.get("index"))
            return None

        index = entry.get("index")
        clip_dir = os.path.join(workdir, f"clip{index}")
        os.makedirs(clip_dir, exist_ok=True)
        done[0] += 1
        position = (done[0] - 1) / total

        # ① 映像を切り直す (ストリームコピーのため数秒)
        if progress_cb:
            progress_cb(position, f"クリップ {done[0]}/{len(clips)} を復元中…（切り出し）")
        raw = os.path.join(clip_dir, "raw.mp4")
        clip_writer.cut_region(vod, float(entry.get("vod_start", 0.0)),
                               float(entry.get("vod_end", 0.0)), raw, ffmpeg_cfg)

        # ② 正規化されていなかった素材はそのまま使う
        if str(entry.get("media_role", "normalized")) != "normalized":
            counters["raw"] += 1
            return raw

        # ③ 音声サイドカーがあれば貼り直す (再エンコードも測定も無い / §3-1 案D)
        sidecar = _sidecar_for(media_dir, media.id, ffmpeg_cfg)
        if sidecar:
            if progress_cb:
                progress_cb(position,
                            f"クリップ {done[0]}/{len(clips)} を復元中…（音声の復元）")
            if media_sidecar.matches_duration(
                    sidecar, media.duration_sec, ffmpeg_cfg, tolerance):
                _container, video_suffix = media_sidecar.suffixes_for(ffmpeg_cfg)
                merged = media_sidecar.mux(
                    raw, sidecar, os.path.join(clip_dir, f"normalized{video_suffix}"),
                    ffmpeg_cfg)
                if merged:
                    counters["from_sidecar"] += 1
                    return merged

        # ④ サイドカーが無い / 使えない → 正規化をやり直す (時間がかかる)
        if policy == "use_source":
            _logger.info("設定に従い切り出したままの音量で使います: clip%s", index)
            counters["raw"] += 1
            return raw
        if progress_cb:
            progress_cb(position,
                        f"クリップ {done[0]}/{len(clips)} を復元中…（音量を正規化）")
        _logger.info("音声サイドカーが無いため clip%s の正規化をやり直します", index)
        normalized = loudness_normalizer.normalize_file(
            raw, os.path.join(clip_dir, "normalized.mp4"), settings)
        counters["renormalized"] += 1
        return normalized

    return recover_one


# 元 VOD のパスを取り出す (archive.vod_path → source.input_path の順)
def _vod_path(source):
    archive = (source or {}).get("archive") if isinstance(source, dict) else None
    candidates = []
    if isinstance(archive, dict):
        candidates.append(str(archive.get("vod_path", "") or ""))
    candidates.append(str((source or {}).get("input_path", "") or ""))
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


# メディアに対応する音声サイドカーのパス (無ければ空文字)
def _sidecar_for(media_dir, media_id, ffmpeg_cfg):
    if not media_dir or not os.path.isdir(media_dir):
        return ""
    path = media_sidecar.sidecar_path(media_dir, media_id, ffmpeg_cfg)
    return path if os.path.exists(path) else ""


# ダイアログと書き出しが要る prepared 相当を Timeline から組み直す (§5.13)
# items / eff_cfg / keep_segments / profile は入れない (字幕は Timeline 側にあり、
# Resolve 出力の eff_cfg は resolve_export が既定設定から組み直すため)。
def rebuild_prepared(timeline):
    prepared = []
    for entry in (archive_section(timeline).get("clips") or []):
        media = timeline.media_by_id(str(entry.get("media_id", "") or ""))
        if media is None or not media.path:
            continue
        prepared.append({
            "index": entry.get("index"),
            "start": float(entry.get("vod_start", 0.0)),
            "end": float(entry.get("vod_end", 0.0)),
            "score": float(entry.get("score", 0.0)),
            "normalized_path": media.path,
            "normalized_duration": float(media.duration_sec or 0.0),
            "theme": str(entry.get("theme", "") or ""),
            "media_role": str(entry.get("media_role", "normalized") or "normalized"),
        })
    prepared.sort(key=lambda p: p["index"] if p["index"] is not None else 0)
    return prepared


# ------------------------------------------------------------------
# 再編集の実行 (§5.7)
# ------------------------------------------------------------------

# 保存済みのアーカイブ用プロジェクトを開いて編集し、書き出しまで行う
# project_path    : プロジェクトファイル
# result_callback : 編集画面のブリッジ (clip_writer.write_clips と同じ形)
#                   callback(prepared, curve, timeline) -> {"timeline","clips"} / None
# relink_callback : 見つからない素材を利用者へ尋ねるフック
# restore_path    : 自動保存から復元する場合の読み込み元 (保存先は project_path のまま)
# 戻り値: 出力ファイルパスの一覧
def run_from_archive_project(project_path, settings, progress_cb=None,
                             result_callback=None, relink_callback=None,
                             restore_path=None):
    _logger.info("=" * 50)
    _logger.info("保存済みアーカイブプロジェクトから再開: %s", project_path)

    if not project_path or not os.path.exists(project_path):
        raise InputError(f"プロジェクトファイルが見つかりません: {project_path}")

    ffmpeg_cfg = settings.get("ffmpeg", {})
    ffmpeg_runner.ensure_available(ffmpeg_cfg)

    cfg = timeline_config(settings)
    load_path = restore_path or project_path
    if restore_path:
        _logger.info("自動保存から復元します: %s", restore_path)
    # 検証は素材の復旧より後に行う (先に走らせると消えた素材のクリップが全部無効になる)
    timeline, meta = project_io.load_project(
        load_path, min_clip_sec=cfg["min_clip_sec"], validate_timeline=False)
    if not is_archive_project(timeline):
        raise InputError(
            "アーカイブ切り抜き用のプロジェクトではありません。"
            "クリップ用タブの「編集の続き」から開いてください。")

    # 元 VOD が見つからなければ差し替えを尋ねる (無いと映像を復元できない)
    _ensure_vod(timeline, relink_callback)

    media_dir = project_io.media_dir_path(
        project_path, cfg["project_suffix"], cfg["project"]["media_dir_suffix"])

    # Windows では書き出し直後のファイルが一時的にロックされ得るため削除失敗を無視する
    with tempfile.TemporaryDirectory(prefix="archive_clips_",
                                     ignore_cleanup_errors=True) as workdir:
        clip_settings = clip_writer.build_clip_settings(
            settings, workdir, settings.get("archive", {}).get("clip_pipeline", {}))

        # ① 素材の復旧
        stats = {}
        if progress_cb:
            progress_cb(0.0, "素材を復元中…")
        recover_progress = None
        if progress_cb:
            def recover_progress(ratio, label):
                progress_cb(min(max(float(ratio), 0.0), 1.0) * 0.55, label)
        media_recovery.recover(
            timeline, project_path, settings, context=None,
            relink_callback=relink_callback,
            source_recover=make_media_recover(
                timeline, settings, workdir, media_dir=media_dir,
                progress_cb=recover_progress, stats=stats))
        project_io.validate(timeline, min_clip_sec=cfg["min_clip_sec"])
        _log_recovery(stats)
        if progress_cb:
            progress_cb(0.55, "素材の復元が完了しました")

        # ② 編集画面 (未注入の CLI/テスト経路は全件そのまま採用)
        prepared = rebuild_prepared(timeline)
        if not prepared:
            raise InputError("復元できたクリップがありません（元 VOD を確認してください）")
        curve = list(archive_section(timeline).get("curve") or [])
        if result_callback is not None:
            review = result_callback(prepared, curve, timeline,
                                     project_path=project_path,
                                     created_at=meta.get("created_at"))
            if review is None:
                raise PipelineCancelled("再編集がキャンセルされたため中断します")
            if isinstance(review, dict):
                timeline = review.get("timeline") or timeline
                edited = review.get("clips") or []
            else:
                edited = review
        else:
            edited = [{"index": p["index"], "theme": p.get("theme", ""), "use": True}
                      for p in prepared]

        # ③ 確定後の上書き保存 (クリップ用 _review_timeline と同じ扱い / 回答 Q6)
        _save_after_confirm(timeline, project_path, meta.get("created_at"))

        # ④ 書き出し (通常の切り抜きと同じ経路)
        return clip_writer.finish_clips(
            timeline.source.get("input_path", ""), settings, clip_settings, timeline,
            edited, prepared, workdir, ffmpeg_cfg=ffmpeg_cfg, progress_cb=progress_cb)


# 元 VOD の実在を確かめ、無ければ差し替えを尋ねる (§5.7 ②)
# 選び直したパスは source.input_path と archive.vod_path の両方へ書き戻す (回答 Q7)。
def _ensure_vod(timeline, relink_callback):
    source = dict(timeline.source or {})
    if _vod_path(source):
        return
    recorded = str(source.get("input_path", "") or "")
    replaced = None
    if relink_callback is not None:
        replaced = relink_callback({
            "media_id": "vod", "path": recorded, "kind": "video",
        })
    if not replaced or not os.path.exists(replaced):
        raise InputError(
            f"元の配信アーカイブ（VOD）が見つかりません。\n{recorded}\n"
            "素材を復元できないため開けません。")
    source["input_path"] = replaced
    archive = dict(source.get("archive") or {})
    archive["vod_path"] = replaced
    source["archive"] = archive
    timeline.source = source
    _logger.info("元 VOD を差し替えました: %s", replaced)


# 復旧の内訳をログへ残す (待ち時間の理由を追えるようにする / §5.5)
def _log_recovery(stats):
    if not stats:
        return
    _logger.info(
        "素材の復元: 音声サイドカー %d 件 / 再正規化 %d 件 / 切り出しのまま %d 件",
        stats.get("from_sidecar", 0), stats.get("renormalized", 0), stats.get("raw", 0))
    if stats.get("renormalized"):
        _logger.info(
            "音声サイドカーが無いクリップがありました。"
            "次に保存すると作られ、以後は復元が速くなります。")


# 確定後の状態を上書き保存する (失敗しても書き出しは続ける)
def _save_after_confirm(timeline, project_path, created_at):
    try:
        project_io.save(timeline, project_path,
                        generator=f"Stretheus {__version__}",
                        created_at=created_at, project_path=project_path)
    except Exception:  # noqa: BLE001 (記録用のため失敗しても続行する)
        _logger.warning("確定後のプロジェクト保存に失敗しました (処理は続行します)")


# 一覧・画面から使う: プロジェクトが指す VOD のパス (存在しなくても記録値を返す)
def recorded_vod_path(timeline):
    source = timeline.source or {}
    archive = source.get("archive") or {}
    return str(archive.get("vod_path", "") or source.get("input_path", "") or "")


__all__ = [
    "archive_section",
    "is_archive_project",
    "make_media_recover",
    "rebuild_prepared",
    "recorded_vod_path",
    "run_from_archive_project",
]
