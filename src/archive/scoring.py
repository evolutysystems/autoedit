# 方式A スコアリング (窓生成・集約・TOP5選択) — request17 §4.5 / flow17 R1
# 純粋関数で構成し、ffmpeg 等の副作用を持たない (単体検証しやすくするため)。
# 入力は「セル特徴の一覧」。1セル = slide_sec 秒の区間で、以下を持つ想定:
#   {"start","end","mean_db","max_db","silence_ratio","w_count","comment_count"}
# R1 の中核採点は torch 等を使わず、音量(mean/max)・無音率・コメントのみで採点する。


# セル数と窓幅(セル数)からスライド窓(セル添字範囲)を生成する
# 1セルずつスライドし、はみ出す部分窓は作らない (例: 0-5,1-6,2-7,...)。
# セル数が窓幅以下なら全体を1窓にする (短い動画でも1窓は返す)。
def build_cell_windows(n_cells, cells_per_window):
    if n_cells <= 0 or cells_per_window <= 0:
        return []
    if n_cells <= cells_per_window:
        return [(0, n_cells)]
    return [(i, i + cells_per_window) for i in range(0, n_cells - cells_per_window + 1)]


# 各セルの mean_db を 0..1 に正規化した配列を返す (音量ベースの盛り上がり指標)
# mean_db 欠損セルや全セル同値の場合は 0 とする。
def loudness_norm(cells):
    values = [c.get("mean_db") for c in cells if c.get("mean_db") is not None]
    if not values:
        return [0.0] * len(cells)
    lo, hi = min(values), max(values)
    span = hi - lo
    out = []
    for c in cells:
        v = c.get("mean_db")
        out.append(0.0 if (v is None or span <= 0) else (v - lo) / span)
    return out


# max_db 群の指定パーセンタイル値を返す (大声セル判定のしきい値)。データ相対で頑健にする。
# max_db が1つも無ければ None (大声加点を行わない)。
def loud_threshold(cells, percentile):
    values = sorted(c.get("max_db") for c in cells if c.get("max_db") is not None)
    if not values:
        return None
    p = min(max(percentile, 0.0), 1.0)
    idx = int(round(p * (len(values) - 1)))
    return values[idx]


# 値を lo..hi に丸める
def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


# 1つの窓(セル添字 i0..i1)を採点し {start,end,emotion,comment,total} を返す
# emotion: 音量ベース(0..100) + 大声加点 + 無音減点 をクランプ (R1は大声/無音のみ検出可能)
# comment: 区間内の「ｗ」総数を点数化 (+急増ボーナス)。コメント未取得(local/取得失敗)なら 0。
#          R3 で twitch-dl 取得したコメントをセルに集計すると有効になる (comment_source)。
def score_window(cells, norm, window, loud_thr, cfg):
    i0, i1 = window
    slice_cells = cells[i0:i1]
    slice_norm = norm[i0:i1]
    if not slice_cells:
        return None

    points = cfg["emotion_points"]
    loud_points = float(points.get("loud", 10))
    long_silence_points = float(points.get("long_silence", -8))
    silence_thr = float(cfg["silence_ratio_threshold"])

    # 音量ベース(0..100): 窓内セルの正規化音量の平均
    base = (sum(slice_norm) / len(slice_norm)) * 100.0

    # 大声セル: max_db がしきい値以上のセル数 → 加点
    loud_cells = 0
    if loud_thr is not None:
        loud_cells = sum(1 for c in slice_cells
                         if c.get("max_db") is not None and c["max_db"] >= loud_thr)
    # 無音セル: 無音率がしきい値以上のセル数 → 減点
    silent_cells = sum(1 for c in slice_cells
                       if c.get("silence_ratio", 0.0) >= silence_thr)

    emotion = base + loud_points * loud_cells + long_silence_points * silent_cells
    emotion = _clamp(emotion, 0.0, 100.0)

    # コメント: ｗ総数を点数化 (+ 平均コメント数/分 を超える急増区間にボーナス / resolve17 §4.4.1)
    comment_cfg = cfg["comment"]
    w_total = sum(int(c.get("w_count", 0)) for c in slice_cells)
    comment = float(w_total) * float(comment_cfg.get("w_point_per_char", 1))
    # 急増ボーナス: この窓のコメント数/分が動画全体の平均を上回るとき加点。
    # avg_comments_per_min は pipeline がコメント取得時に算出して cfg へ供給する。
    if cfg.get("comment_spike", False):
        avg_cpm = float(cfg.get("avg_comments_per_min", 0.0))
        win_min = max((slice_cells[-1]["end"] - slice_cells[0]["start"]) / 60.0, 1e-6)
        win_cpm = sum(int(c.get("comment_count", 0)) for c in slice_cells) / win_min
        if avg_cpm > 0 and win_cpm > avg_cpm:
            comment += float(comment_cfg.get("rate_spike_bonus", 10))
    comment = _clamp(comment, 0.0, 100.0)

    weights = cfg["weights"]
    total = emotion * float(weights.get("emotion", 0.55)) + comment * float(weights.get("comment", 0.45))
    return {
        "start": slice_cells[0]["start"],
        "end": slice_cells[-1]["end"],
        "emotion": round(emotion, 1),
        "comment": round(comment, 1),
        "total": round(total, 1),
    }


# 2区間が時間的に重なるか
def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


# start 昇順のクリップ列から、時間が連続(接触)または重なる区間を1つのセクションへ統合する。
# 統合区間は union([start,end]) とし、3分窓の固定枠を外して実際の連続範囲にする。
# 代表スコア(score/emotion/comment)は統合対象のうち最大 total の窓の値を採用する。
def _merge_time_sections(clips):
    if not clips:
        return []
    merged = [dict(clips[0])]
    for c in clips[1:]:
        cur = merged[-1]
        # next.start <= cur.end なら「連続 or 重なり」 → 統合して区間を伸ばす
        if c["start"] <= cur["end"]:
            cur["end"] = max(cur["end"], c["end"])
            cur["start"] = min(cur["start"], c["start"])
            if c["total"] > cur["total"]:
                cur["total"] = c["total"]
                cur["emotion"] = c["emotion"]
                cur["comment"] = c["comment"]
        else:
            merged.append(dict(c))
    return merged


# 採点済み窓から重なりを避けて上位 top_n をイベントとして選び、
# その中で時間が連続 or 重なるものは 1 つのクリップ(セクション)へ統合して返す。
# 1) 高得点の窓を貪欲採用し、重なる窓を除外 → 連続する高得点窓群はピーク窓に代表される (§4.5)。
# 2) clip_pad_sec で前後に余白を付け duration でクランプ。
# 3) 連続(接触)/重なりの採用区間を union で1セクションに統合 (3分枠を外す)。要望: TOP10内の
#    継続 or 重なりは1クリップ。
def select_top_events(scored, top_n, clip_pad_sec, duration):
    ordered = sorted((w for w in scored if w), key=lambda w: w["total"], reverse=True)
    chosen = []
    for w in ordered:
        if len(chosen) >= top_n:
            break
        if any(_overlaps(w["start"], w["end"], c["start"], c["end"]) for c in chosen):
            continue
        chosen.append(w)

    # 時系列順に並べ、パディングを付与
    chosen.sort(key=lambda w: w["start"])
    padded = []
    for w in chosen:
        cstart = max(0.0, w["start"] - clip_pad_sec)
        cend = min(duration, w["end"] + clip_pad_sec) if duration > 0 else w["end"] + clip_pad_sec
        padded.append({
            "start": cstart, "end": cend, "total": w["total"],
            "emotion": w["emotion"], "comment": w["comment"],
        })

    # 連続/重なりを1セクションへ統合し、クリップ番号を振る
    merged = _merge_time_sections(padded)
    clips = []
    for idx, m in enumerate(merged, 1):
        clips.append({
            "index": idx,
            "start": m["start"],
            "end": m["end"],
            "score": m["total"],
            "emotion": m["emotion"],
            "comment": m["comment"],
            "use": True,
        })
    return clips
