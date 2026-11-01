# アーカイブ採点用の特徴抽出 (request17 §4.4.1 中核 / flow17 R1)
# 既存の ffmpeg ベース解析のみを使う (torch/ML 不要):
#   ・音量 mean/max : volume_analyzer.measure_region_volume (volumedetect)
#   ・無音率        : silence_cutter.detect_silence (1パス) をセルへ按分
# セル = slide_sec 秒の連続区間。窓積分の最小単位。
import math
import re
import subprocess

from ..exceptions import DiskSpaceError, FFmpegError, PipelineCancelled
from ..i18n import tr
from ..modules import ffmpeg_runner, silence_cutter, volume_analyzer
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags

_logger = get_logger(__name__)

# 1 パス解析の 1 秒値を取り出すパターン (resolve3 R5)
# ametadata=print は "lavfi.astats.Overall.RMS_level=-23.456" の形で標準出力へ出す。
_RE_SCAN_RMS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[0-9.eE+]+|-?inf|nan)")
_RE_SCAN_PEAK = re.compile(r"lavfi\.astats\.Overall\.Peak_level=(-?[0-9.eE+]+|-?inf|nan)")
# 1 秒値を取る粒度 (サンプル数は入力のレートではなく asetnsamples の指定値で決まる)
_SCAN_STEP_SEC = 1.0


# 無音区間リストと [start,end] の重なり合計秒を返す (無音率算出用)
def _silence_overlap(silence_ranges, start, end):
    total = 0.0
    for s, e in silence_ranges:
        lo = max(start, s)
        hi = min(end, e)
        if hi > lo:
            total += hi - lo
    return total


# ------------------------------------------------------------------
# 全長 1 パスの音声解析 (resolve3 R5)
# ------------------------------------------------------------------

# 1 パス解析の測定条件を返す (引き継ぎの照合に使う / resolve3 R5)
def scan_params(settings):
    va_cfg = (settings or {}).get("volume_analysis", {})
    return {
        "noise_db": va_cfg.get("base_noise_db", -30),
        "min_silence_sec": float(va_cfg.get("base_min_silence_sec", 0.3)),
        "step_sec": _SCAN_STEP_SEC,
    }


# 引き継いだ scan が今の設定と同じ条件で測られたものか照合する (resolve3 R5)
#
# 採点のあと設定画面で閾値を変えられた場合に、古い値で切ってしまうのを防ぐ。
# 一致しなければ呼び出し側が測り直す = 常に正しい値で動く。
def scan_matches(scan, settings):
    if not scan or not scan.get("per_sec"):
        return False
    want = scan_params(settings)
    have = scan.get("params") or {}
    try:
        return (have.get("noise_db") == want["noise_db"]
                and abs(float(have.get("min_silence_sec", -1))
                        - want["min_silence_sec"]) < 1e-9
                and abs(float(have.get("step_sec", -1)) - want["step_sec"]) < 1e-9)
    except (TypeError, ValueError):
        return False


# 全長を 1 パスで解析し、無音区間と 1 秒ごとの RMS / Peak をまとめて返す (resolve3 R5)
#
# 従来はセル 1 個につき volumedetect を 1 プロセス起動し (138 プロセス / 132 秒)、
# さらに全長の silencedetect を採点と書き出しで 2 回流していた。
# 1 本のコマンドで両方を得ることで実測 259.5 秒 -> 13.2 秒になる。
#
# 設計上の注意 (実測で確かめた仕様):
#   ・astats の reset=N は**フレーム数**であって秒ではない。音声フレームは 1024
#     サンプル (21.3ms) なので、asetnsamples で 1 フレーム = 1 秒に揃えて reset=1 とする。
#   ・asetnsamples は silencedetect の**後ろ**へ置く。前に置くと silencedetect が
#     1 秒粒度でしか判定できなくなる。
#   ・ametadata=print:file=- は**標準出力**へ出る (silencedetect は標準エラー)。
#   ・無音部では RMS_level=-inf / Peak_level=-inf が出るため必ず弾く。
#
# 戻り値: {"duration", "silence_ranges", "per_sec", "params"}
#   per_sec[i] = {"rms": float|None, "peak": float|None}  (index = 秒)
def scan_audio(input_path, settings, progress_cb=None):
    ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
    params = scan_params(settings)
    duration = ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg)
    if duration <= 0:
        return {"duration": 0.0, "silence_ranges": [], "per_sec": [], "params": params}

    if progress_cb is not None:
        progress_cb(0.0, tr("音声解析 (VOD 全体)"))

    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    sample_rate = int(ffmpeg_cfg.get("audio_sample_rate", 48000) or 48000)
    samples = max(int(sample_rate * _SCAN_STEP_SEC), 1)
    audio_filter = (
        "silencedetect=noise={noise}dB:d={dur},".format(
            noise=params["noise_db"], dur=params["min_silence_sec"])
        + "asetnsamples=n={n}:p=0,".format(n=samples)
        + "astats=metadata=1:reset=1,"
        + "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-,"
        + "ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-"
    )
    cmd = [
        ffmpeg, "-hide_banner", "-nostats",
        "-i", input_path,
        # 映像の復号は結果に一切寄与しない (resolve3 R4 と同じ理由)
        "-vn",
        "-af", audio_filter,
        "-f", "null", "-",
    ]
    _logger.debug("全長 1 パス解析 実行: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError as e:
        raise FFmpegError(tr("FFmpeg 実行失敗: {error}", error=e)) from e

    if result.returncode != 0:
        raise FFmpegError(
            tr("全長の音声解析に失敗"),
            command=cmd,
            stderr_tail="\n".join(result.stderr.splitlines()[-30:]),
            returncode=result.returncode,
        )

    silence_ranges = silence_cutter.parse_silence_log(result.stderr)
    per_sec = _parse_per_sec(result.stdout)
    _logger.info(
        "全長 1 パス解析: %.1fs / 1 秒値 %d 件 / 無音区間 %d 件",
        duration, len(per_sec), len(silence_ranges))
    if progress_cb is not None:
        progress_cb(1.0, tr("音声解析 (VOD 全体)"))
    return {"duration": duration, "silence_ranges": silence_ranges,
            "per_sec": per_sec, "params": params}


# ametadata の標準出力から 1 秒ごとの RMS / Peak を取り出す (resolve3 R5)
#
# RMS と Peak は同じフレームについて続けて出るため、出現順に 1 件ずつ対応させる。
# -inf / nan (無音部) は None にする。
def _parse_per_sec(stdout_text):
    rms_values = [_scan_float(m) for m in _RE_SCAN_RMS.findall(stdout_text or "")]
    peak_values = [_scan_float(m) for m in _RE_SCAN_PEAK.findall(stdout_text or "")]
    count = max(len(rms_values), len(peak_values))
    return [{"rms": rms_values[i] if i < len(rms_values) else None,
             "peak": peak_values[i] if i < len(peak_values) else None}
            for i in range(count)]


# astats の値を float へ寄せる (-inf / nan は None / resolve3 R5)
def _scan_float(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if math.isinf(value) or math.isnan(value):
        return None
    return value


# dB 値の**エネルギー平均**を返す (resolve3 R5)
#
# volumedetect の mean_volume は RMS なので、dB の算術平均ではなく
# 10*log10(mean(10**(db/10))) で合わせないと値がずれる。
# 有効値が 1 つも無ければ None。
def energy_mean_db(values):
    powers = [10.0 ** (float(v) / 10.0) for v in values if v is not None]
    if not powers:
        return None
    return 10.0 * math.log10(sum(powers) / len(powers))


# 1 秒値から [start, end) の (mean_db, max_db) を求める (resolve3 R5)
def region_levels(per_sec, start, end):
    if not per_sec:
        return None, None
    lo = max(int(math.floor(float(start) / _SCAN_STEP_SEC)), 0)
    hi = min(int(math.ceil(float(end) / _SCAN_STEP_SEC)), len(per_sec))
    if hi <= lo:
        # 1 秒未満の区間は、重なる 1 件だけを見る
        hi = min(lo + 1, len(per_sec))
    window = per_sec[lo:hi]
    mean_db = energy_mean_db([item.get("rms") for item in window])
    peaks = [item.get("peak") for item in window if item.get("peak") is not None]
    return mean_db, (max(peaks) if peaks else None)


# 入力動画をセル分割し、各セルの音量(mean/max)・無音率を抽出する
# 戻り値: (duration, cells, scan)。
#   cells[i] = {start,end,mean_db,max_db,silence_ratio,w_count,comment_count}
#   scan     = 全長 1 パス解析の結果 (resolve3 R5)。per_cell モードでは None。
#              書き出しへ引き継ぎ、同じ測定を 2 度流さないために返す。
# progress_cb(ratio 0..1, label) を渡すと進捗を通知する。
#
# archive.scoring.feature_mode (resolve3 R5):
#   "single_pass" : 全長 1 パス (silencedetect + astats) で抽出する (既定)
#   "per_cell"    : セルごとに volumedetect を呼ぶ (従来 / 切り戻し)
def extract_cells(input_path, settings, cell_sec, progress_cb=None):
    ffmpeg_cfg = settings.get("ffmpeg", {})
    va_cfg = settings.get("volume_analysis", {})
    mode = str((settings.get("archive", {}).get("scoring", {})
                .get("feature_mode", "single_pass")) or "single_pass").strip().lower()

    scan = None
    if mode == "single_pass":
        try:
            scan = scan_audio(input_path, settings, progress_cb=progress_cb)
        except (PipelineCancelled, DiskSpaceError):
            raise
        except Exception as e:  # noqa: BLE001 (1 パスが使えなければ従来方式へ落ちる)
            _logger.warning("全長 1 パス解析に失敗 → セル別の測定へ落ちます: %s", e)
            scan = None

    if scan and scan.get("per_sec"):
        duration, cells = _cells_from_scan(scan, cell_sec, progress_cb)
        return duration, cells, scan

    duration = ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg)
    if duration <= 0:
        return 0.0, [], None

    # 無音検出は動画全体で1回だけ実行し、各セルへ按分する (パス数を抑える)
    noise_db = va_cfg.get("base_noise_db", -30)
    min_silence = float(va_cfg.get("base_min_silence_sec", 0.3))
    try:
        silence_ranges = silence_cutter.detect_silence(input_path, noise_db, min_silence, ffmpeg_cfg)
    except (PipelineCancelled, DiskSpaceError):
        # 中止と容量不足は「継続してはいけない」失敗なので必ず貫通させる (resolve3 R9-6)
        raise
    except Exception as e:  # noqa: BLE001 (無音検出失敗は致命としない=無音率0で継続)
        _logger.warning("無音検出に失敗 (無音率0で継続): %s", e)
        silence_ranges = []

    n_cells = int(math.ceil(duration / cell_sec))
    cells = []
    for i in range(n_cells):
        start = i * cell_sec
        end = min((i + 1) * cell_sec, duration)
        cell_len = max(end - start, 1e-6)

        result = volume_analyzer.measure_region_volume(input_path, start, end, ffmpeg_cfg)
        mean_db, max_db = (result if result else (None, None))
        silence_ratio = min(1.0, _silence_overlap(silence_ranges, start, end) / cell_len)

        cells.append({
            "start": start,
            "end": end,
            "mean_db": mean_db,
            "max_db": max_db,
            "silence_ratio": silence_ratio,
            # コメント特徴 (R1 local は未取得=0。R3 で twitch-dl から供給する)
            "w_count": 0,
            "comment_count": 0,
        })
        if progress_cb is not None:
            progress_cb((i + 1) / n_cells,
                        tr("特徴抽出中… ({done}/{total})",
                           done=i + 1, total=n_cells))

    return duration, cells, None


# 1 パス解析の結果からセルを組み立てる (resolve3 R5)
# 戻り値は従来の extract_cells と同じ (duration, cells)。
def _cells_from_scan(scan, cell_sec, progress_cb=None):
    duration = float(scan["duration"])
    per_sec = scan["per_sec"]
    silence_ranges = scan["silence_ranges"]
    n_cells = int(math.ceil(duration / cell_sec))
    cells = []
    for i in range(n_cells):
        start = i * cell_sec
        end = min((i + 1) * cell_sec, duration)
        cell_len = max(end - start, 1e-6)
        mean_db, max_db = region_levels(per_sec, start, end)
        cells.append({
            "start": start,
            "end": end,
            "mean_db": mean_db,
            "max_db": max_db,
            "silence_ratio": min(
                1.0, _silence_overlap(silence_ranges, start, end) / cell_len),
            # コメント特徴 (R1 local は未取得=0。R3 で twitch-dl から供給する)
            "w_count": 0,
            "comment_count": 0,
        })
    if progress_cb is not None:
        progress_cb(1.0, tr("特徴抽出中… ({done}/{total})",
                            done=n_cells, total=n_cells))
    _logger.info("特徴抽出 (全長 1 パス): %d セル", len(cells))
    return duration, cells
