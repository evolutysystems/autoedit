# アーカイブ採点オーケストレーション (request17 §4.1 / flow17 R1)
# 入力(ローカル mp4) → 特徴抽出 → 方式A採点 → 窓積分 → TOP5 選択。
# 字幕焼き込みは行わない (TOP5 確定後に clip_writer が担当する)。
from ..modules import ffmpeg_runner
from ..utils.logger import get_logger
from . import config, features, scoring

_logger = get_logger(__name__)


# ローカル動画を採点し TOP5 クリップ候補を返す
# progress_cb(ratio 0..1, label) で進捗通知。
# 戻り値: {"duration", "curve"(窓スコア列), "clips"(TOP5候補)}
def analyze(input_path, settings, progress_cb=None):
    ffmpeg_cfg = settings.get("ffmpeg", {})
    ffmpeg_runner.ensure_available(ffmpeg_cfg)

    cfg = config.method_a_config(settings)
    cell_sec = max(1, cfg["slide_sec"])
    cells_per_window = max(1, round(cfg["window_sec"] / cell_sec))

    _logger.info("アーカイブ採点開始: %s (窓%ds/スライド%ds)",
                 input_path, cfg["window_sec"], cfg["slide_sec"])

    # 特徴抽出 (進捗の 0..0.9 を割り当てる)
    def _feat_progress(ratio, label):
        if progress_cb:
            progress_cb(ratio * 0.9, label)

    duration, cells = features.extract_cells(input_path, settings, cell_sec, _feat_progress)
    if not cells:
        _logger.warning("特徴が空のため採点できません")
        return {"duration": duration, "curve": [], "clips": []}

    if progress_cb:
        progress_cb(0.92, "採点中…")

    # 方式A 採点 → 窓積分
    norm = scoring.loudness_norm(cells)
    loud_thr = scoring.loud_threshold(cells, cfg["loud_percentile"])
    windows = scoring.build_cell_windows(len(cells), cells_per_window)
    curve = [scoring.score_window(cells, norm, w, loud_thr, cfg) for w in windows]
    curve = [w for w in curve if w]

    # イベント統合 + TOP5 選択
    clips = scoring.select_top_events(curve, cfg["top_n"], cfg["clip_pad_sec"], duration)
    _logger.info("採点完了: 窓%d件 → TOP%d抽出", len(curve), len(clips))
    for c in clips:
        _logger.info("  clip%d: %.1f-%.1f 点=%s (感情%s/コメント%s)",
                     c["index"], c["start"], c["end"], c["score"], c["emotion"], c["comment"])

    if progress_cb:
        progress_cb(1.0, "採点完了")
    return {"duration": duration, "curve": curve, "clips": clips}
