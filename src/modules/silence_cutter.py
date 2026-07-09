# 無音カットモジュール
# 設計書 ① 無音カット に対応
# silencedetect → 残す有音区間を算出 → trim+concat フィルタで結合
import os
import re
import subprocess
import tempfile

from ..exceptions import FFmpegError, InputError
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from . import ffmpeg_runner

_logger = get_logger(__name__)

# silencedetect の出力パターン
_RE_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_RE_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")


# FFmpeg silencedetect を実行し無音区間を抽出する
# 戻り値: [(start_sec, end_sec), ...]
def detect_silence(input_path, noise_threshold_db, min_duration_sec, ffmpeg_settings):
    if not os.path.exists(input_path):
        raise InputError(f"入力動画が見つかりません: {input_path}")

    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i", input_path,
        "-af", f"silencedetect=noise={noise_threshold_db}dB:d={min_duration_sec}",
        "-f", "null",
        "-",
    ]
    _logger.debug("silencedetect 実行: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            # GUI(windowed)実行時にコンソール窓を出さない (Windows のみ有効)
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError as e:
        raise FFmpegError(f"FFmpeg 実行失敗: {e}") from e

    if result.returncode != 0:
        raise FFmpegError(
            "silencedetect 失敗",
            command=cmd,
            stderr_tail="\n".join(result.stderr.splitlines()[-30:]),
            returncode=result.returncode,
        )

    # silencedetect は stderr に出力する
    return _parse_silence_log(result.stderr)


# silencedetect ログから (開始秒, 終了秒) のリストを構築する
def _parse_silence_log(stderr_text):
    silence_ranges = []
    current_start = None
    for line in stderr_text.splitlines():
        m_start = _RE_SILENCE_START.search(line)
        if m_start:
            current_start = float(m_start.group(1))
            continue
        m_end = _RE_SILENCE_END.search(line)
        if m_end and current_start is not None:
            silence_ranges.append((current_start, float(m_end.group(1))))
            current_start = None
    return silence_ranges


# 残すべき有音区間を算出する
# 入力: 無音区間リスト, 動画総尺
# 出力: [(keep_start, keep_end), ...]
def build_keep_segments(silence_ranges, total_duration):
    keep = []
    cursor = 0.0
    for s_start, s_end in silence_ranges:
        if s_start > cursor:
            keep.append((cursor, s_start))
        cursor = max(cursor, s_end)
    if cursor < total_duration:
        keep.append((cursor, total_duration))
    return [seg for seg in keep if (seg[1] - seg[0]) > 0.01]


# 残す区間を trim+concat フィルタで結合する
# fade_enabled が True なら各セグメント境界に微フェードを付与
def cut_and_concat(input_path, keep_segments, output_path, ffmpeg_settings,
                   fade_enabled=False, fade_duration_sec=0.05,
                   total_duration=0.0, on_progress=None):
    if not keep_segments:
        raise InputError("残すべき有音区間が存在しません (全区間が無音判定)")

    filter_parts = []
    concat_inputs_v = []
    concat_inputs_a = []

    for i, (start, end) in enumerate(keep_segments):
        seg_duration = end - start
        v_label = f"v{i}"
        a_label = f"a{i}"
        # 映像
        v_chain = f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS"
        if fade_enabled and seg_duration > fade_duration_sec * 2:
            fade_out_start = seg_duration - fade_duration_sec
            v_chain += (
                f",fade=t=in:st=0:d={fade_duration_sec}"
                f",fade=t=out:st={fade_out_start:.3f}:d={fade_duration_sec}"
            )
        v_chain += f"[{v_label}]"
        filter_parts.append(v_chain)

        # 音声
        a_chain = f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS"
        if fade_enabled and seg_duration > fade_duration_sec * 2:
            fade_out_start = seg_duration - fade_duration_sec
            a_chain += (
                f",afade=t=in:st=0:d={fade_duration_sec}"
                f",afade=t=out:st={fade_out_start:.3f}:d={fade_duration_sec}"
            )
        a_chain += f"[{a_label}]"
        filter_parts.append(a_chain)

        concat_inputs_v.append(f"[{v_label}]")
        concat_inputs_a.append(f"[{a_label}]")

    n = len(keep_segments)
    # concat フィルターはセグメント毎に [映像][音声] を交互に並べる必要がある
    # (偶数パッド=映像 / 奇数パッド=音声)。まとめて並べると型不一致になる。
    interleaved = "".join(
        v + a for v, a in zip(concat_inputs_v, concat_inputs_a)
    )
    concat_filter = interleaved + f"concat=n={n}:v=1:a=1[outv][outa]"
    filter_parts.append(concat_filter)
    filter_complex = ";".join(filter_parts)

    # フィルタグラフを一時ファイルへ書き出し、パスのみをコマンドに渡す
    # 2時間超など無音区間が膨大な動画では filter_complex 文字列が数十万文字に達し、
    # コマンドライン長の上限 (Windows: 32,767 文字) を超えて WinError 206 となる。
    # -filter_complex_script でファイル経由にすることでこの上限を回避する。
    filter_script_path = _write_filter_script(filter_complex, output_path)
    try:
        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-i", input_path,
            "-filter_complex_script", filter_script_path,
            "-map", "[outv]",
            "-map", "[outa]",
            *ffmpeg_runner.build_encode_options(ffmpeg_settings),
            output_path,
        ]
        # 残り合計尺を進捗用 total_duration として利用
        kept_total = sum((e - s) for s, e in keep_segments)
        effective_duration = kept_total if total_duration <= 0 else total_duration
        ffmpeg_runner.execute(
            cmd, total_duration=effective_duration, on_progress=on_progress,
            progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_settings),
        )
    finally:
        # 成否に関わらず一時スクリプトを削除する
        if os.path.exists(filter_script_path):
            os.remove(filter_script_path)
    return output_path


# 1区間を入力シーク (-ss) で抽出し中間ファイル化する
# trim フィルタと異なり入力をシークするため、その区間付近のみをデコードする。
# 再エンコード前提のため -ss を -i の前に置いてもフレーム精度が得られる。
def extract_segment(input_path, start, end, seg_path, ffmpeg_settings,
                    fade_enabled=False, fade_duration_sec=0.05,
                    on_progress=None):
    seg_duration = end - start
    if seg_duration <= 0:
        raise InputError(f"区間長が不正です: start={start}, end={end}")

    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
    # -ss を -i の前に置き、対象区間付近のみをデコードする (高速シーク)
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-ss", f"{start:.3f}",
        "-i", input_path,
        "-t", f"{seg_duration:.3f}",   # 区間長で停止 (基準が曖昧な -to は使わない)
    ]
    # フェード指定時は区間内で完結する fade/afade を付与する (trim 不要)
    if fade_enabled and seg_duration > fade_duration_sec * 2:
        fade_out_start = seg_duration - fade_duration_sec
        cmd += [
            "-vf",
            f"fade=t=in:st=0:d={fade_duration_sec},"
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_duration_sec}",
            "-af",
            f"afade=t=in:st=0:d={fade_duration_sec},"
            f"afade=t=out:st={fade_out_start:.3f}:d={fade_duration_sec}",
        ]
    # 各区間を CFR (固定フレームレート) で固定する (resolve 20260630 対策B)。
    # VFR ソースを CFR 非強制で抽出→コピー連結すると区間境界で TS が破損し、
    # 後段の再エンコード (テロップ焼き込み/OP・ED結合) で mux エラー(EINVAL)を招くため。
    fps = ffmpeg_runner.get_output_fps(ffmpeg_settings)
    cmd += [
        *ffmpeg_runner.build_encode_options(ffmpeg_settings),
        "-fps_mode", "cfr",            # 可変フレームレートを固定化
        "-r", f"{fps}",                # 出力フレームレート (setting.json: ffmpeg.output_fps)
        "-avoid_negative_ts", "make_zero",  # 連結時の負PTS/音ズレ対策
        seg_path,
    ]

    # 区間尺を total_duration にすることで進捗率を 0-1 に正規化する
    ffmpeg_runner.execute(
        cmd, total_duration=seg_duration, on_progress=on_progress,
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_settings),
    )
    return seg_path


# 入力シーク方式で全区間を抽出し concat デマルチプレクサで結合する (案A)
# trim フィルタによる全長再デコードを排し、watchdog 停滞 kill を回避する。
def cut_and_concat_seek(input_path, keep_segments, output_path, ffmpeg_settings,
                        fade_enabled=False, fade_duration_sec=0.05,
                        on_progress=None, concat_reencode=False):
    if not keep_segments:
        raise InputError("残すべき有音区間が存在しません (全区間が無音判定)")

    out_dir = os.path.dirname(output_path) or "."
    kept_total = sum((e - s) for s, e in keep_segments) or 1.0
    seg_paths = []
    done_duration = 0.0
    try:
        for idx, (start, end) in enumerate(keep_segments):
            seg_path = os.path.join(out_dir, f"silence_seg_{idx:05d}.mp4")
            seg_dur = end - start

            # 区間内進捗 (0-1) を全体進捗へ写像するサブコールバック
            def _seg_progress(ratio_in_seg, _kv, _base=done_duration, _dur=seg_dur):
                if on_progress is not None:
                    on_progress((_base + ratio_in_seg * _dur) / kept_total, _kv)

            extract_segment(
                input_path, start, end, seg_path, ffmpeg_settings,
                fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
                on_progress=_seg_progress if on_progress else None,
            )
            seg_paths.append(seg_path)
            done_duration += seg_dur

        # 抽出済み区間群を concat デマルチプレクサで最終結合する (既存関数を再利用)
        _concat_demux(seg_paths, output_path, ffmpeg_settings, reencode=concat_reencode)
        if on_progress is not None:
            on_progress(1.0, {})
    finally:
        # 中間区間ファイルを後始末する (成否に関わらず)
        for p in seg_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    _logger.warning("区間中間ファイル削除に失敗: %s", p)
    return output_path


# 残す区間を結合する。区間数が batch_size を超える場合はバッチ分割し、
# 各バッチを中間ファイル化したうえで concat デマルチプレクサで最終結合する。
# batch_size <= 0 または区間数が batch_size 以下のときは従来どおり一括処理する。
# 巨大フィルタグラフ (split のファンアウト/フレームキュー膨張) によるハングを回避する。
def cut_and_concat_batched(input_path, keep_segments, output_path, ffmpeg_settings,
                           fade_enabled=False, fade_duration_sec=0.05,
                           total_duration=0.0, on_progress=None,
                           batch_size=0, concat_reencode=False):
    if not keep_segments:
        raise InputError("残すべき有音区間が存在しません (全区間が無音判定)")

    # 分割不要 (小規模 or 無効) なら従来経路へフォールバックする
    if batch_size <= 0 or len(keep_segments) <= batch_size:
        return cut_and_concat(
            input_path, keep_segments, output_path, ffmpeg_settings,
            fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
            total_duration=total_duration, on_progress=on_progress,
        )

    # 区間を batch_size ずつのバッチへ分割する
    batches = [
        keep_segments[i:i + batch_size]
        for i in range(0, len(keep_segments), batch_size)
    ]
    _logger.info(
        "無音カットをバッチ分割: %d 区間 → %d バッチ (size=%d)",
        len(keep_segments), len(batches), batch_size,
    )

    out_dir = os.path.dirname(output_path) or "."
    batch_paths = []
    # 進捗はバッチ尺の累積で按分する
    kept_total = sum((e - s) for s, e in keep_segments) or 1.0
    done_duration = 0.0
    try:
        for idx, batch in enumerate(batches):
            batch_path = os.path.join(out_dir, f"silence_batch_{idx:04d}.mp4")
            batch_dur = sum((e - s) for s, e in batch)

            # 各バッチ内の進捗 (0-1) を全体進捗へ写像するサブコールバック
            def _batch_progress(ratio_in_batch, _kv, _base=done_duration, _dur=batch_dur):
                if on_progress is not None:
                    on_progress((_base + ratio_in_batch * _dur) / kept_total, _kv)

            # バッチ内結合は既存の trim+concat フィルタ (cut_and_concat) を再利用する
            cut_and_concat(
                input_path, batch, batch_path, ffmpeg_settings,
                fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
                total_duration=batch_dur,
                on_progress=_batch_progress if on_progress else None,
            )
            batch_paths.append(batch_path)
            done_duration += batch_dur

        # バッチ群を concat デマルチプレクサで最終結合する
        _concat_demux(batch_paths, output_path, ffmpeg_settings,
                      reencode=concat_reencode)
        if on_progress is not None:
            on_progress(1.0, {})
    finally:
        # 中間バッチファイルを後始末する (成否に関わらず)
        for p in batch_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    _logger.warning("バッチ中間ファイル削除に失敗: %s", p)
    return output_path


# 複数の中間動画を concat デマルチプレクサ (ファイルリスト方式) で1本に結合する。
# reencode=False のときはストリームコピー (高速・劣化なし) で結合する。
def _concat_demux(batch_paths, output_path, ffmpeg_settings, reencode=False):
    if not batch_paths:
        raise InputError("結合対象のバッチが存在しません")

    out_dir = os.path.dirname(output_path) or "."
    # concat デマルチプレクサ用のファイルリストを書き出す
    fd, list_path = tempfile.mkstemp(suffix=".txt", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for p in batch_paths:
                # シングルクォートを含むパス対策でエスケープする
                safe = p.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
        cmd = [
            ffmpeg, "-y", "-hide_banner",
            "-fflags", "+genpts",        # 連結境界で欠落PTSを再生成する (resolve 20260630 対策B)
            "-f", "concat", "-safe", "0",
            "-i", list_path,
        ]
        if reencode:
            cmd += ffmpeg_runner.build_encode_options(ffmpeg_settings)
        else:
            cmd += ["-c", "copy"]
        # 連結時の負PTS/不連続を是正し、後段再エンコードでの mux エラー(EINVAL)を防ぐ
        cmd += [
            "-avoid_negative_ts", "make_zero",
            "-max_interleave_delta", "0",
            output_path,
        ]

        ffmpeg_runner.execute(
            cmd,
            progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_settings),
        )
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)
    return output_path


# フィルタグラフを一時ファイルへ書き出しパスを返す
# 出力先と同じディレクトリに置き、文字コードは UTF-8 で統一する
def _write_filter_script(filter_complex, output_path):
    out_dir = os.path.dirname(output_path) or "."
    fd, path = tempfile.mkstemp(suffix=".ffscript", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(filter_complex)
    except Exception:
        # 書き込み失敗時は生成済みの一時ファイルを後始末する
        os.remove(path)
        raise
    return path


# 公開エントリポイント
def run(context):
    settings = context.settings
    silence_cfg = settings.get("silence_cut", {})
    ffmpeg_cfg = settings.get("ffmpeg", {})

    # 無音カット ON/OFF (resolve12)。無効時は入力をそのまま次工程へ通す。
    if not silence_cfg.get("enabled", True):
        _logger.info("無音カットスキップ (silence_cut.enabled=false)")
        return context.current_video_path()

    # 要望(resolve7)により無効化。将来戻す可能性があるため処理を温存する。
    # 旧方式: silence_cut の無音音量閾値(dB)・無音最小継続時間(秒)を使用していた。
    # noise_threshold_db = silence_cfg.get("noise_threshold_db", -30)
    # min_silence_duration_sec = _coerce_float(
    #     silence_cfg.get("min_silence_duration_sec"),
    #     default=0.6,
    # )

    # 新方式(resolve7): 音量解析で確定した単一閾値を使用する。
    # この dB 以下を無音(カット)、以上を有音(動画として使用)とみなす。
    va_cfg = settings.get("volume_analysis", {})
    noise_threshold_db = va_cfg.get("last_cut_db", -28)
    min_silence_duration_sec = _coerce_float(
        va_cfg.get("cut_min_silence_sec"),
        default=0.6,
    )
    fade_enabled = bool(silence_cfg.get("fade_enabled", False))
    fade_duration_sec = float(silence_cfg.get("fade_duration_sec", 0.05))
    batch_size = int(silence_cfg.get("batch_size", 0))
    concat_reencode = bool(silence_cfg.get("concat_reencode", False))
    extract_mode = silence_cfg.get("extract_mode", "seek")  # "seek"=案A / "filter"=従来

    input_path = context.current_video_path()
    output_path = context.allocate_intermediate("silence_cut.mp4")

    _logger.info(
        "無音カット開始: threshold=%sdB, min=%.3fs, fade=%s",
        noise_threshold_db, min_silence_duration_sec, fade_enabled,
    )

    total_duration = ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg)
    silence_ranges = detect_silence(
        input_path, noise_threshold_db, min_silence_duration_sec, ffmpeg_cfg
    )
    _logger.info("検出された無音区間: %d 件", len(silence_ranges))

    keep_segments = build_keep_segments(silence_ranges, total_duration)
    if not keep_segments:
        raise InputError("有音区間が抽出できませんでした")

    if extract_mode == "seek":
        # 案A: 入力シーク方式 (推奨)。trim による全長再デコードを回避する。
        cut_and_concat_seek(
            input_path, keep_segments, output_path, ffmpeg_cfg,
            fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
            on_progress=context.progress_subcallback("無音カット"),
            concat_reencode=concat_reencode,
        )
    else:
        # 従来: trim+concat フィルタ方式 (互換維持)
        cut_and_concat_batched(
            input_path, keep_segments, output_path, ffmpeg_cfg,
            fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
            total_duration=total_duration,
            on_progress=context.progress_subcallback("無音カット"),
            batch_size=batch_size, concat_reencode=concat_reencode,
        )

    context.set_current_video_path(output_path)
    _logger.info("無音カット完了: %s", output_path)
    return output_path


# 候補値から最初に有効な float を返す
def _coerce_float(*candidates, default=0.0):
    for value in candidates:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default
