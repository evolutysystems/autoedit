# 音量解析モジュール
# resolve7 に対応
# 無音カット前に発話候補区間を切り出し、各区間の音量(RMS)を実測して
# 「発話区間の最低dB」を算出する。確定値は単一のカット閾値として用いる
# (db以下=カット / db以上=動画として使用)。
import os
import re
import subprocess

from ..exceptions import FFmpegError, InputError
from ..i18n import tr
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from . import ffmpeg_runner, silence_cutter

_logger = get_logger(__name__)

# volumedetect の出力パターン (stderr に出力される)
_RE_MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?[0-9.]+)\s*dB")
_RE_MAX_VOLUME = re.compile(r"max_volume:\s*(-?[0-9.]+)\s*dB")

# 発話候補区間に残すピーク音量の下限 (dBFS / ver11 resolve1)
# volume_analysis.min_region_peak_db が不正値だったときの既定。
_DEFAULT_MIN_REGION_PEAK_DB = -20.0


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
        # 音声解析に映像の復号は不要 (resolve3 R4)
        "-vn",
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
        raise FFmpegError(tr("FFmpeg 実行失敗: {error}", error=e)) from e

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
# scan: 全長 1 パス解析の結果 (resolve3 R5)。渡すと ffmpeg を 1 回も起動せず、
#   1 秒ごとの RMS/Peak から区間の代表音量を求める (実測 122 秒 -> 0 秒)。
#   測定条件の照合は呼び出し側 (features.scan_matches) が済ませている前提。
def analyze_min_speech_db(input_path, settings, scan=None):
    if not os.path.exists(input_path):
        raise InputError(tr("入力動画が見つかりません: {path}", path=input_path))

    va_cfg = settings.get("volume_analysis", {})
    ffmpeg_cfg = settings.get("ffmpeg", {})

    # 発話候補区間を切り出す緩い基準 (解析専用パラメータ)
    base_noise_db = va_cfg.get("base_noise_db", -30)
    base_min_silence_sec = float(va_cfg.get("base_min_silence_sec", 0.3))
    # 0.8秒以下の発話は無視する (R8 / resolve7)
    min_region_sec = float(va_cfg.get("min_region_sec", 0.8))
    max_regions = int(va_cfg.get("max_regions", 100))
    metric = str(va_cfg.get("metric", "mean")).strip().lower()

    # 引き継いだ 1 パス解析があれば、総尺と無音区間もそこから採る (resolve3 R5)。
    # 無ければ従来どおり ffprobe と silencedetect を回す。
    per_sec = (scan or {}).get("per_sec") or None
    if per_sec:
        total_duration = float(scan.get("duration") or 0.0)
        silence_ranges = scan.get("silence_ranges") or []
    else:
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

    # 発話候補に混ざった「ピークが立たない定常ノイズ」を外す下限 (ver11 resolve1)。
    # None / 空 で従来動作 (切り戻し経路)。
    configured_peak = va_cfg.get("min_region_peak_db")
    peak_floor = (_to_float(configured_peak, _DEFAULT_MIN_REGION_PEAK_DB)
                  if configured_peak not in (None, "") else None)

    # 各区間の代表音量を測定して集める
    # metric="mean"/"max" は従来どおり最小値を採り、"percentile" は下側分位点を採る。
    values = []
    # ピーク下限で全区間が外れたときに使う退避先 (安全弁 / ver11 resolve1 §3)
    fallback_values = []
    dropped_quiet = 0
    for (s, e) in regions:
        if per_sec:
            # 1 秒値から求める (resolve3 R5)。mean は**エネルギー平均**にすること。
            # volumedetect の mean_volume は RMS なので、dB の算術平均では値がずれる。
            mean_db, max_db = _region_levels_from_scan(per_sec, s, e)
        else:
            result = measure_region_volume(input_path, s, e, ffmpeg_cfg)
            if result is None:
                continue
            mean_db, max_db = result
        value = max_db if metric == "max" else mean_db
        if value is None:
            continue
        fallback_values.append(value)
        # ピークが立たない区間は発話ではないとみなし、母集団から外す (ver11 resolve1)。
        #
        # base_noise_db による無音判定は**瞬間値**で行われるため、
        # 「ピークは立たないが常に鳴っている音」(低域ノイズなど) を無音と判定できない。
        # その区間が発話候補に入ると代表音量の分位点を引き下げ、
        # カット閾値がノイズフロアまで落ちて**カットがほぼ効かなくなる**。
        # 実測 (ver10 result_ss1.md §12): 候補 70 区間のうち 19 区間 (27%) が
        # mean -38〜-32dB / max -28.8〜-23.7dB のノイズで、閾値が -37dB になっていた。
        # 同素材の発話区間は max -4.9〜-1.1dB で、max には 19dB の空白がある。
        if peak_floor is not None and max_db is not None and max_db < peak_floor:
            dropped_quiet += 1
            continue
        values.append(value)

    # 全区間が下限未満 = 素材全体が静か。従来どおり全区間で算出する (安全弁)。
    # ここで諦めると min_db=None になり、呼び出し側が閾値を決められなくなる。
    if not values and fallback_values:
        _logger.warning(
            "ピーク下限 %.1fdB を満たす発話候補が無いため、全 %d 区間で算出します",
            peak_floor if peak_floor is not None else 0.0, len(fallback_values))
        values = fallback_values
        dropped_quiet = 0

    measured_count = len(values)
    if not values:
        _logger.warning("全区間の音量測定に失敗したため最低dBを算出できません")
        return {"min_db": None, "region_count": region_count, "measured_count": 0,
                "dropped_quiet_count": dropped_quiet}

    if metric == "percentile":
        percentile = _to_float(va_cfg.get("percentile"), 10.0)
        representative = _percentile(values, percentile)
    else:
        # 従来: 最も静かに発話している区間の音量 (最小値)
        representative = min(values)

    rounded = int(round(representative))
    _logger.info(
        "発話区間の代表dB=%d (%s, 測定 %d/%d 区間%s%s)",
        rounded, metric, measured_count, region_count,
        " / 1 パス解析から" if per_sec else "",
        f" / ピーク下限で {dropped_quiet} 区間除外" if dropped_quiet else "",
    )
    return {
        "min_db": rounded,
        "region_count": region_count,
        "measured_count": measured_count,
        # ピーク下限で外した区間数 (ver11 resolve1 / 記録用)
        "dropped_quiet_count": dropped_quiet,
    }

# 1 パス解析の 1 秒値から区間の (mean_db, max_db) を求める (resolve3 R5)
#
# 実装は archive/features.region_levels にある。循環 import を避けるため遅延 import する
# (features は modules を参照するが、modules は本来 archive を参照しない)。
def _region_levels_from_scan(per_sec, start, end):
    from ..archive import features        # noqa: PLC0415 (循環回避のため遅延 import)

    return features.region_levels(per_sec, start, end)


# 下側分位点を返す (docs/quality/resolve1.md §2.3 F7)
#
# 従来の「最小値」は 1 区間でも静かなものがあると閾値がノイズフロアまで落ち、
# カットが一切効かなくなっていた (実測: 最低dB=-41 → 無音 0 件)。
# 分位点なら外れ値 1 件では動かない。
# percentile は 0..100 (%)。0 で最小値、100 で最大値と同じになる。
def _percentile(values, percentile):
    ordered = sorted(values)
    if not ordered:
        return None
    ratio = min(max(float(percentile) / 100.0, 0.0), 1.0)
    index = int(round(ratio * (len(ordered) - 1)))
    return ordered[index]


# 設定値を float へ寄せる (不正値は既定へ)
def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
