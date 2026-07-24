# アーカイブ採点用の特徴抽出 (request17 §4.4.1 中核 / flow17 R1)
# 既存の ffmpeg ベース解析のみを使う (torch/ML 不要):
#   ・音量 mean/max : volume_analyzer.measure_region_volume (volumedetect)
#   ・無音率        : silence_cutter.detect_silence (1パス) をセルへ按分
# セル = slide_sec 秒の連続区間。窓積分の最小単位。
import math

from ..modules import ffmpeg_runner, silence_cutter, volume_analyzer
from ..utils.logger import get_logger

_logger = get_logger(__name__)


# 無音区間リストと [start,end] の重なり合計秒を返す (無音率算出用)
def _silence_overlap(silence_ranges, start, end):
    total = 0.0
    for s, e in silence_ranges:
        lo = max(start, s)
        hi = min(end, e)
        if hi > lo:
            total += hi - lo
    return total


# 入力動画をセル分割し、各セルの音量(mean/max)・無音率を抽出する
# 戻り値: (duration, cells)。cells[i] = {start,end,mean_db,max_db,silence_ratio,w_count,comment_count}
# progress_cb(ratio 0..1, label) を渡すと進捗を通知する。
def extract_cells(input_path, settings, cell_sec, progress_cb=None):
    ffmpeg_cfg = settings.get("ffmpeg", {})
    va_cfg = settings.get("volume_analysis", {})

    duration = ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg)
    if duration <= 0:
        return 0.0, []

    # 無音検出は動画全体で1回だけ実行し、各セルへ按分する (パス数を抑える)
    noise_db = va_cfg.get("base_noise_db", -30)
    min_silence = float(va_cfg.get("base_min_silence_sec", 0.3))
    try:
        silence_ranges = silence_cutter.detect_silence(input_path, noise_db, min_silence, ffmpeg_cfg)
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
            progress_cb((i + 1) / n_cells, f"特徴抽出中… ({i + 1}/{n_cells})")

    return duration, cells
