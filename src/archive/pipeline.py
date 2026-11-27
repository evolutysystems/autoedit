# アーカイブ採点オーケストレーション (request17 §4.1 / flow17 R1)
# 入力(ローカル mp4) → 特徴抽出 → 方式A採点 → 窓積分 → TOP5 選択。
# 字幕焼き込みは行わない (TOP5 確定後に clip_writer が担当する)。
from ..i18n import tr
from ..modules import ffmpeg_runner
from ..utils.logger import get_logger
from . import comment_source, config, features, marker_source, scoring

_logger = get_logger(__name__)


# AI の山を採点へ混ぜる比率 (ai.scan.ai_weight)。0 なら従来どおり混ぜない。
# blend モードのときだけ使う。既定 0.5 は利用者の指示 (2026-11-26 / resolve3.md §4)。
# 0.3 を実測で決めた経緯は resolve2.md §14.2 にある。
# 設定の読み込みは浅いマージのため、古い設定ファイルには鍵が無い。そのときも
# 意図した既定で動くよう、欠落・不正値のどちらもこの定数へ落とす。
_DEFAULT_AI_WEIGHT = 0.5

# 採点モード (ai.scan.scoring_mode / resolve3.md §3.5)
#   ai_only … 窓の total を AI の点だけで置き換える (既定 / 利用者の指示 2026-11-26)
#   blend   … 方式 A と AI の点を ai_weight で混ぜる (resolve2.md §6.1)
#   machine … 方式 A のまま。山は後段へ渡るだけで採点へ効かせない (従来の挙動)
# **どのモードでも方式 A の採点そのものは走る。** 消さずに設定で呼び分ける。
MODE_AI_ONLY = "ai_only"
MODE_BLEND = "blend"
MODE_MACHINE = "machine"
_SCORING_MODES = (MODE_AI_ONLY, MODE_BLEND, MODE_MACHINE)
_DEFAULT_SCORING_MODE = MODE_AI_ONLY

# 候補を集めるときの余裕 (archive.scoring.target_headroom / resolve3.md §9)。
# 構成 (③') が「選ぶ」余地を残すため、出力目標の素材換算に掛ける。
_DEFAULT_TARGET_HEADROOM = 2.0


# archive/ は ai/ を import しないため、設定を直接読む (resolve1.md §4)。
def ai_scan_weight(settings):
    scan = ((settings or {}).get("ai", {}) or {}).get("scan", {}) or {}
    try:
        return max(min(float(scan.get("ai_weight", _DEFAULT_AI_WEIGHT)), 1.0), 0.0)
    except (TypeError, ValueError):
        return _DEFAULT_AI_WEIGHT


# 採点モードを読む (resolve3.md §3.5)。知らない値は既定へ落とす。
def scoring_mode(settings):
    scan = ((settings or {}).get("ai", {}) or {}).get("scan", {}) or {}
    value = str(scan.get("scoring_mode", _DEFAULT_SCORING_MODE) or "").strip().lower()

    return value if value in _SCORING_MODES else _DEFAULT_SCORING_MODE


# 無音を除いたあとに残る割合を見積もる (resolve3.md §9)。
#
# セルの silence_ratio から出す。実際のカット閾値はクリップごとの音量解析で
# 決まるため**目安**だが、「出力 14 分にするには素材を何秒集めればよいか」の
# 換算にはこれで足りる。実測では素材 840s → 残す 430.6s (51%) だった。
#
# ai/config.MIN_KEPT_RATIO と同じ下限を持つ (archive/ は ai/ を import しないため写し)。
_MIN_KEPT_RATIO = 0.2


def kept_ratio(cells):
    total = 0.0
    silent = 0.0
    for cell in cells or []:
        length = max(float(cell.get("end", 0.0)) - float(cell.get("start", 0.0)), 0.0)
        if length <= 0:
            continue
        ratio = float(cell.get("silence_ratio", 0.0) or 0.0)
        total += length
        silent += length * max(min(ratio, 1.0), 0.0)
    if total <= 0:
        return 1.0

    return max(min((total - silent) / total, 1.0), _MIN_KEPT_RATIO)


# 候補として集める素材尺の目標 (resolve3.md §9 / 利用者の指示 2026-11-26)。
#
# 「14 分以上の切り抜きにする」ための数字は `ai.plan.<target>.min_sec`
# (= **出力尺**) に 1 つだけ置く。ここはそれを素材尺へ割り戻し、
# 構成 (③') が選べる幅を持たせるため余裕を掛けた分だけ集める。
#
#   目標素材尺 = max(min_sec) ÷ 残存率 × headroom
#
# 0 を返したときは従来どおり top_n 件で打ち切る (経路 B と、尺指定が無い構成)。
def candidate_target_sec(settings, kept=1.0):
    ai = ((settings or {}).get("ai", {}) or {}) if isinstance(settings, dict) else {}
    plan = ai.get("plan", {}) or {}
    wanted = 0.0
    for name in (plan.get("targets") or []):
        values = plan.get(name) if isinstance(name, str) else None
        if not isinstance(values, dict):
            continue
        try:
            wanted = max(wanted, float(values.get("min_sec") or 0.0))
        except (TypeError, ValueError):
            continue
    if wanted <= 0:
        return 0.0
    scoring_cfg = ((settings or {}).get("archive", {}) or {}).get("scoring", {}) or {}
    try:
        headroom = float(scoring_cfg.get("target_headroom", _DEFAULT_TARGET_HEADROOM))
    except (TypeError, ValueError):
        headroom = _DEFAULT_TARGET_HEADROOM

    return wanted / max(min(float(kept or 1.0), 1.0), _MIN_KEPT_RATIO) \
        * max(headroom, 1.0)


# スイープを回す。失敗しても採点は止めない (機械採点のまま続ける / resolve2.md §9)。
def _run_scan(scan, input_path, settings, cells, comments, duration):
    if scan is None:
        return []
    try:
        return list(scan(input_path, settings, cells, comments, duration) or [])
    except Exception as e:  # noqa: BLE001 (AI が落ちても既存の採点で続ける)
        _logger.warning("AI の採点スイープに失敗しました (機械採点で続けます): %s", e)

        return []


# ローカル動画を採点し TOP5 クリップ候補を返す
# progress_cb(ratio 0..1, label) で進捗通知。
# comments: comment_source.normalize_comments 済みの正規化コメント配列 (省略/None ならコメント採点は 0)。
# markers : ストリームマーカー配列 (省略/None ならマーカー無し / ver3 resolve16)。
#           採点には混ぜず、前後 N 秒を「必ず残すセクション」として採点結果へ合流させる。
# scan    : AI の採点スイープ (docs/AiEditor/resolve2.md §6.2)。
#           scan(input_path, settings, cells, comments, duration) -> 山の一覧。
#           **省略時は今までと完全に同じ挙動**であり、従来の切り抜き (経路 B) は変わらない。
#           渡すのは AI 編集 (経路 D) だけ。関数で受けることで pipeline が ai/ を
#           import せずに済む (依存の向き / resolve1.md §4)。
# 戻り値: {"duration", "curve"(窓スコア列), "clips"(TOP5候補), "moments"(AI の山)}
def analyze(input_path, settings, progress_cb=None, comments=None, markers=None,
            scan=None):
    ffmpeg_cfg = settings.get("ffmpeg", {})
    ffmpeg_runner.ensure_available(ffmpeg_cfg)

    cfg = config.method_a_config(settings)
    cell_sec = max(1, cfg["slide_sec"])
    cells_per_window = max(1, round(cfg["window_sec"] / cell_sec))

    _logger.info("アーカイブ採点開始: %s (窓%ds/スライド%ds, コメント%d件)",
                 input_path, cfg["window_sec"], cfg["slide_sec"],
                 len(comments) if comments else 0)

    # 特徴抽出 (進捗の 0..0.9 を割り当てる)
    def _feat_progress(ratio, label):
        if progress_cb:
            progress_cb(ratio * 0.9, label)

    # audio_scan は全長 1 パス解析の結果 (resolve3 R5)。書き出しへ引き継ぎ、
    # 同じ測定を 2 度流さないために戻り値へ同梱する (per_cell モードでは None)。
    duration, cells, audio_scan = features.extract_cells(
        input_path, settings, cell_sec, _feat_progress)
    if not cells:
        _logger.warning("特徴が空のため採点できません")
        return {"duration": duration, "curve": [], "clips": [],
                "audio_scan": audio_scan, "moments": [], "kept_ratio": 1.0}

    # コメントをセルへ集計し、急増ボーナス(平均コメント数/分)を有効化する (R3 / resolve17 §4.4.1)。
    # コメント未取得なら comment_spike=False のまま = 従来どおり音声のみ採点で完走する。
    if comments:
        comment_cfg = cfg["comment"]
        comment_source.aggregate_into_cells(
            comments, cells, cell_sec,
            # 日本語の笑い表現と笑いエモートも数える (docs/quality/resolve1.md §2.3 F4)
            laugh_words=comment_cfg.get("laugh_words"),
            laugh_emotes=comment_cfg.get("laugh_emotes"))
        cfg["avg_comments_per_min"] = comment_source.average_comments_per_min(comments, duration)
        cfg["comment_spike"] = True
        _logger.info("コメント集計: %d件 / 平均%.2f件/分",
                     len(comments), cfg["avg_comments_per_min"])

    if progress_cb:
        progress_cb(0.92, tr("採点中…"))

    # 方式A 採点 → 窓積分
    # **AI の採点モードでもここは必ず走る** (resolve3.md §0-1)。
    # emotion / comment の内訳・採点グラフ・同点の解き・縮退先がこの値に乗っている。
    norm = scoring.loudness_norm(cells)
    loud_thr = scoring.loud_threshold(cells, cfg["loud_percentile"])
    windows = scoring.build_cell_windows(len(cells), cells_per_window)
    curve = [scoring.score_window(cells, norm, w, loud_thr, cfg) for w in windows]
    curve = [w for w in curve if w]

    # AI の採点スイープ (渡されたときだけ / resolve2.md §6)。
    # 失敗しても機械採点のまま続ける = AI が使えない日も従来どおり動く。
    moments = _run_scan(scan, input_path, settings, cells, comments, duration)
    if moments:
        # 山が取れたときだけ total を差し替える (resolve3.md §3.5)。
        mode = scoring_mode(settings)
        if mode == MODE_AI_ONLY:
            curve = scoring.score_by_ai_moments(curve, moments)
            _logger.info("採点を AI の点で置き換えました: 山 %d 件 (モード %s)",
                         len(moments), mode)
        elif mode == MODE_BLEND:
            weight = ai_scan_weight(settings)
            curve = scoring.blend_ai_moments(curve, moments, weight)
            _logger.info("AI の山を採点へ混ぜました: %d 件 / 比率 %.2f", len(moments), weight)
        else:
            _logger.info("AI の山を採点へ効かせません (モード %s): 山 %d 件は構成へ渡します",
                         mode, len(moments))

    # ストリームマーカーを「必ず残す区間」へ変換する (ver3 resolve16 / 要望 I2)。
    # 採点には混ぜない。採点結果と合流させ、重なりは select_top_events が統合する。
    forced = []
    marker_cfg = config.marker_config(settings)
    if markers and marker_cfg["enabled"] and duration > 0:
        forced = marker_source.to_sections(
            markers, marker_cfg["before_sec"], marker_cfg["after_sec"], duration)
        _logger.info("ストリームマーカー: %d件 → 強制セクション %d件 (前%.0fs/後%.0fs)",
                     len(markers), len(forced),
                     marker_cfg["before_sec"], marker_cfg["after_sec"])

    # イベント統合 + 候補の選択 (マーカー由来の強制区間は枠を消費しない)。
    #
    # 尺の目標があれば**点の高い順に積み上げ、目標に達したら終了**する
    # (resolve3.md §9 / 利用者の指示 2026-11-26)。無ければ従来どおり top_n 件。
    kept = kept_ratio(cells)
    target_sec = candidate_target_sec(settings, kept)
    clips = scoring.select_top_events(
        curve, cfg["top_n"], cfg["clip_pad_sec"], duration, forced=forced,
        target_total_sec=target_sec)
    picked_sec = sum(max(float(c["end"]) - float(c["start"]), 0.0) for c in clips)
    if target_sec > 0:
        _logger.info(
            "候補の積み上げ: 素材 %.0fs / 目標 %.0fs (残存率 %.2f → 出力 約%.0fs 見込み)%s",
            picked_sec, target_sec, kept, picked_sec * kept,
            "" if picked_sec >= target_sec else " ← 素材が尽きました")
    _logger.info("採点完了: 窓%d件 → %dセクション (うちマーカー由来 %d)",
                 len(curve), len(clips), sum(1 for c in clips if c.get("marker")))
    for c in clips:
        _logger.info("  clip%d: %.1f-%.1f 点=%s (感情%s/コメント%s)%s",
                     c["index"], c["start"], c["end"], c["score"], c["emotion"], c["comment"],
                     f" [マーカー: {'/'.join(c['marker_labels'])}]" if c.get("marker") else "")

    if progress_cb:
        progress_cb(1.0, tr("採点完了"))
    return {"duration": duration, "curve": curve, "clips": clips,
            # 書き出しへ引き継ぐ全長 1 パス解析の結果 (resolve3 R5)
            "audio_scan": audio_scan,
            # AI の山 (スイープを行わなければ空)。構成と後段の編集が使う
            "moments": moments,
            # 無音を除いて残る割合の見積り (resolve3.md §9)。
            # 構成が「出力尺」で尺を決めるために使う
            "kept_ratio": kept}
