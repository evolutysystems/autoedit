# 音量解析モジュール
# resolve7 に対応
# 無音カット前に発話候補区間を切り出し、各区間の音量(RMS)を実測して
# 「発話区間の最低dB」を算出する。確定値は単一のカット閾値として用いる
# (db以下=カット / db以上=動画として使用)。
import os
import re
import subprocess

from ..exceptions import FFmpegError, InputError
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from . import ffmpeg_runner, silence_cutter

_logger = get_logger(__name__)

# volumedetect の出力パターン (stderr に出力される)
_RE_MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?[0-9.]+)\s*dB")
_RE_MAX_VOLUME = re.compile(r"max_volume:\s*(-?[0-9.]+)\s*dB")


# 1区間を volumedetect で測定し (mean_db, max_db) を返す
# 区間単体の失敗は致命としないため、失敗時は None を返す
def measure_region_volume(input_path, start, end, ffmpeg_settings):
    duration = end - start
    if duration <= 0:
        return None

    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
    # -ss/-t を -i の前に置き対象区間付近のみをデコードする (高速シーク)
    cmd = [
        ffmpeg, "-hide_banner",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", input_path,
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            # GUI(windowed)実行時にコンソール窓を出さない (Windows のみ有効)
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError as e:
        raise FFmpegError(f"FFmpeg 実行失敗: {e}") from e

    if result.returncode != 0:
        _logger.warning("volumedetect 失敗 (区間 %.3f-%.3f)", start, end)
        return None
    return _parse_volumedetect(result.stderr)


# volumedetect ログから (mean_db, max_db) を抽出する
def _parse_volumedetect(stderr_text):
    mean_db = None
    max_db = None
    m_mean = _RE_MEAN_VOLUME.search(stderr_text)
    if m_mean:
        mean_db = float(m_mean.group(1))
    m_max = _RE_MAX_VOLUME.search(stderr_text)
    if m_max:
        max_db = float(m_max.group(1))
    return (mean_db, max_db)


# 発話区間の最低dBを解析する
# 戻り値: {"min_db": int|None, "region_count": int, "measured_count": int}
#   min_db   : 発話候補区間の代表音量の最小値 (整数化)。算出不能時は None
def analyze_min_speech_db(input_path, settings):
    if not os.path.exists(input_path):
        raise InputError(f"入力動画が見つかりません: {input_path}")

    va_cfg = settings.get("volume_analysis", {})
    ffmpeg_cfg = settings.get("ffmpeg", {})

    # 発話候補区間を切り出す緩い基準 (解析専用パラメータ)
    base_noise_db = va_cfg.get("base_noise_db", -30)
    base_min_silence_sec = float(va_cfg.get("base_min_silence_sec", 0.3))
    # 0.8秒以下の発話は無視する (R8 / resolve7)
    min_region_sec = float(va_cfg.get("min_region_sec", 0.8))
    max_regions = int(va_cfg.get("max_regions", 100))
    metric = str(va_cfg.get("metric", "mean")).strip().lower()

    total_duration = ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg)

    # 発話候補区間 = 無音(緩い基準)の補集合 (既存関数を再利用)
    silence_ranges = silence_cutter.detect_silence(
        input_path, base_noise_db, base_min_silence_sec, ffmpeg_cfg
    )
    regions = silence_cutter.build_keep_segments(silence_ranges, total_duration)

    # 0.8秒以下の発話は無視する (R8)。「以下を無視」のため厳密超過で採用する。
    regions = [(s, e) for (s, e) in regions if (e - s) > min_region_sec]
    region_count = len(regions)
    if not regions:
        _logger.warning("発話候補区間が0件のため最低dBを算出できません")
        return {"min_db": None, "region_count": 0, "measured_count": 0}

    # 区間数が多い場合は長い順に標本抽出し、打ち切りをログへ明示する
    if max_regions > 0 and len(regions) > max_regions:
        sampled = sorted(regions, key=lambda r: (r[1] - r[0]), reverse=True)[:max_regions]
        _logger.info("測定区間を標本化: %d → %d 区間 (長い順)", len(regions), len(sampled))
        regions = sampled

    # 各区間の代表音量を測定し最小値を採る (= 最も静かに発話している区間の音量)
    min_db = None
    measured_count = 0
    for (s, e) in regions:
        result = measure_region_volume(input_path, s, e, ffmpeg_cfg)
        if result is None:
            continue
        mean_db, max_db = result
        value = max_db if metric == "max" else mean_db
        if value is None:
            continue
        measured_count += 1
        if min_db is None or value < min_db:
            min_db = value

    if min_db is None:
        _logger.warning("全区間の音量測定に失敗したため最低dBを算出できません")
        return {"min_db": None, "region_count": region_count, "measured_count": 0}

    rounded = int(round(min_db))
    _logger.info(
        "発話区間の最低dB=%d (%s, 測定 %d/%d 区間)",
        rounded, metric, measured_count, region_count,
    )
    return {
        "min_db": rounded,
        "region_count": region_count,
        "measured_count": measured_count,
    }
