# TOP5 クリップの処理と結合 (request18 / resolve18)
# 各クリップ: 区間を切り出し → 既存 run_pipeline (クリップ用と同一の
#   無音カット + テロップ編集画面 + 焼き込み) で処理 (OP/ED は付けない・出力は一時領域)。
# 最後に: 全クリップを結合し、先頭にオープニング・末尾にエンディングを付けて 1 本にする。
# 全 VOD を文字起こしせず採用区間のみ処理する点は従来どおり。
import copy
import os
import shutil
import tempfile

from ..exceptions import PipelineCancelled
from ..modules import concat_processor, ffmpeg_runner
from ..pipeline.pipeline_runner import run_pipeline
from ..utils.logger import get_logger
from . import config

_logger = get_logger(__name__)


# 出力ディレクトリを解決する (general.output_directory。未設定/不在なら入力と同じ場所)
def _resolve_output_dir(settings, input_path):
    out_dir = settings.get("general", {}).get("output_directory", "")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        return out_dir
    return os.path.dirname(os.path.abspath(input_path))


# 秒をファイル名に使える表記へ整形する (H-MM-SS。コロンは Windows で不可のため使わない)
def _fmt_ts(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}-{m:02d}-{s:02d}"


# 区間 [start,end] を切り出す (ストリームコピーで高速に。焼き込み側で再エンコードされる)
def _cut_region(input_path, start, end, dest, ffmpeg_cfg):
    duration = max(end - start, 0.0)
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y",
        "-ss", f"{start:.3f}",
        "-i", input_path,
        "-t", f"{duration:.3f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        dest,
    ]
    ffmpeg_runner.execute(cmd, total_duration=duration)
    return dest


# クリップ処理用の設定コピーを作る (OP/ED は付けない・出力は一時領域・無音カット/レビューは設定に従う)
def _build_clip_settings(settings, workdir, clip_pipe):
    clip_settings = copy.deepcopy(settings)
    general = clip_settings.setdefault("general", {})
    # OP/ED は各クリップには付けない (結合段階で1回だけ付ける / resolve18 §3-2)
    general["opening_enabled"] = False
    general["ending_enabled"] = False
    # 各クリップの出力は一時領域へ (最終結合まで出力先を汚さない)
    general["output_directory"] = workdir
    # 無音カット / テロップ編集画面の ON/OFF (既定 True = 要望どおり)
    clip_settings.setdefault("silence_cut", {})["enabled"] = bool(clip_pipe.get("silence_cut", True))
    clip_settings.setdefault("subtitle", {})["review_enabled"] = bool(clip_pipe.get("subtitle_review", True))
    return clip_settings


# 各クリップを run_pipeline で処理し焼き込み済みパスの一覧を返す (キャンセルはスキップ)
def _process_clips(input_path, clip_settings, used, ffmpeg_cfg, workdir,
                   review_callback, progress_cb):
    burned, skipped = [], 0
    total = len(used)
    for pos, clip in enumerate(used, 1):
        base = (pos - 1) / total
        if progress_cb:
            progress_cb(base * 0.85, f"クリップ {pos}/{total} を処理中…")
        raw = os.path.join(workdir, f"clip{clip['index']}_raw.mp4")
        _cut_region(input_path, clip["start"], clip["end"], raw, ffmpeg_cfg)

        def _sub(ratio, _label, _b=base):
            if progress_cb:
                progress_cb((_b + ratio / total) * 0.85, f"クリップ {pos}/{total}: {_label}")

        try:
            # 既存クリップ用と同一: 無音カット + テロップ編集画面 + 焼き込み
            # 回答①: 音量ダイアログは出さない (volume_analysis_callback=None)
            out = run_pipeline(
                raw, clip_settings,
                progress_cb=_sub,
                subtitle_review_callback=review_callback,
                volume_analysis_callback=None,
            )
            burned.append((clip, out))
        except PipelineCancelled:
            skipped += 1
            _logger.info("clip%d 編集キャンセル → スキップ", clip["index"])
            continue
    return burned, skipped


# 確定した TOP5 クリップを処理し、結合して 1 本の動画を出力する
# clips: [{index,start,end,use,...}] (use=False は除外)。
# review_callback: 字幕編集画面ブリッジ (None なら編集画面なしで自動焼き込み)。
# 戻り値: 出力ファイルパスの一覧 (結合時は [結合1本]、非結合時は個別クリップ群)。
def write_clips(input_path, settings, clips, progress_cb=None, review_callback=None):
    ffmpeg_cfg = settings.get("ffmpeg", {})
    ffmpeg_runner.ensure_available(ffmpeg_cfg)

    used = [c for c in clips if c.get("use", True)]
    if not used:
        _logger.info("使用クリップが0件のため切り抜きをスキップ")
        return []

    archive_cfg = settings.get("archive", {})
    clip_pipe = archive_cfg.get("clip_pipeline", {})
    combine_cfg = archive_cfg.get("combine", {})

    out_dir = _resolve_output_dir(settings, input_path)
    prefix = config.clip_prefix(settings)
    stem = os.path.splitext(os.path.basename(input_path))[0]

    # Windows では焼き込み直後のファイルが一時的にロックされ得るため、
    # 一時ディレクトリ削除の失敗を無視する (削除失敗で処理成功が誤ってエラー扱いになるのを防ぐ)。
    with tempfile.TemporaryDirectory(prefix="archive_clips_", ignore_cleanup_errors=True) as workdir:
        clip_settings = _build_clip_settings(settings, workdir, clip_pipe)
        burned, skipped = _process_clips(
            input_path, clip_settings, used, ffmpeg_cfg, workdir, review_callback, progress_cb)

        if not burned:
            _logger.info("焼き込み済みクリップが0件 (全スキップ)")
            return []

        # keep_individual: 個別クリップも出力先へ残す
        outputs = []
        if combine_cfg.get("keep_individual", False):
            for clip, path in burned:
                name = f"{prefix}_{stem}_clip{clip['index']}_{_fmt_ts(clip['start'])}-{_fmt_ts(clip['end'])}.mp4"
                dest = os.path.join(out_dir, name)
                shutil.copy(path, dest)
                outputs.append(dest)

        burned_paths = [path for _clip, path in burned]

        # 結合しない設定なら個別出力のみ (keep_individual 未指定でも個別を出す)
        if not combine_cfg.get("enabled", True):
            if not outputs:
                for clip, path in burned:
                    name = f"{prefix}_{stem}_clip{clip['index']}_{_fmt_ts(clip['start'])}-{_fmt_ts(clip['end'])}.mp4"
                    dest = os.path.join(out_dir, name)
                    shutil.copy(path, dest)
                    outputs.append(dest)
            _logger.info("個別クリップ %d 件を出力 (結合なし / スキップ%d)", len(outputs), skipped)
            if progress_cb:
                progress_cb(1.0, "完了")
            return outputs

        # 結合: [Opening?] + clips + [Ending?] を 1 本へ (回答④・OP/ED を1回だけ)
        if progress_cb:
            progress_cb(0.9, "結合中…")
        parts = _build_combine_parts(settings, combine_cfg, burned_paths)

        suffix = combine_cfg.get("combined_suffix", "combined") or "combined"
        final = os.path.join(out_dir, f"{prefix}_{stem}_{suffix}.mp4")
        _combine(parts, final, ffmpeg_cfg)

        _logger.info("結合出力: %s (使用%d / スキップ%d)", final, len(burned_paths), skipped)
        if progress_cb:
            progress_cb(1.0, "完了")
        return outputs + [final]


# 結合パーツ [Opening?] + クリップ群 + [Ending?] を組み立てる
# OP/ED は combine.opening_ending かつ general フラグ+素材有無で判定 (クリップ用と同一ルール)
def _build_combine_parts(settings, combine_cfg, burned_paths):
    parts = []
    general = settings.get("general", {})
    add_oped = bool(combine_cfg.get("opening_ending", True))
    opening = general.get("opening_video", "")
    ending = general.get("ending_video", "")
    if add_oped and general.get("opening_enabled", False) and concat_processor.is_available(opening):
        parts.append(opening)
    parts.extend(burned_paths)
    if add_oped and general.get("ending_enabled", False) and concat_processor.is_available(ending):
        parts.append(ending)
    return parts


# パーツを結合して final を作る (2本以上は concat、1本のみはコピー)
def _combine(parts, final, ffmpeg_cfg):
    if len(parts) >= 2:
        total_dur = 0.0
        for p in parts:
            try:
                total_dur += ffmpeg_runner.probe_duration(p, ffmpeg_cfg)
            except Exception:  # noqa: BLE001 (進捗総尺の取得失敗は致命でない)
                pass
        concat_processor.concat(parts, final, ffmpeg_cfg, total_duration=total_dur)
    else:
        # クリップ1本のみ・OP/ED 無し → そのまま出力先へ複製 (原本は一時領域で破棄)
        shutil.copy(parts[0], final)
    return final
