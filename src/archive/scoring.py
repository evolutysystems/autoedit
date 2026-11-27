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
    # 日本語の笑い表現 (「草」「笑」) と笑いエモート (LUL/KEKW 等) を足す
    # (docs/quality/resolve1.md §2.3 F4)。集計が無い (= 従来の cells) なら 0 のまま。
    laugh_words = sum(int(c.get("laugh_word_count", 0)) for c in slice_cells)
    laugh_emotes = sum(int(c.get("laugh_emote_count", 0)) for c in slice_cells)
    comment += float(laugh_words) * float(comment_cfg.get("laugh_word_point", 1))
    comment += float(laugh_emotes) * float(comment_cfg.get("emote_point", 1))
    # 急増ボーナス: この窓のコメント数/分が動画全体の平均を上回るとき加点。
    # avg_comments_per_min は pipeline がコメント取得時に算出して cfg へ供給する。
    if cfg.get("comment_spike", False):
        avg_cpm = float(cfg.get("avg_comments_per_min", 0.0))
        win_min = max((slice_cells[-1]["end"] - slice_cells[0]["start"]) / 60.0, 1e-6)
        win_cpm = sum(int(c.get("comment_count", 0)) for c in slice_cells) / win_min
        if avg_cpm > 0 and win_cpm > avg_cpm:
            comment += float(comment_cfg.get("rate_spike_bonus", 10))
    comment = _clamp(comment, 0.0, 100.0)

    total = _weighted_total(emotion, comment, cfg)
    return {
        "start": slice_cells[0]["start"],
        "end": slice_cells[-1]["end"],
        "emotion": round(emotion, 1),
        "comment": round(comment, 1),
        "total": round(total, 1),
    }


# 感情点とコメント点を重み付けして合計する (docs/quality/resolve1.md §2.3 F4)
#
# コメントを取得できない入力 (ローカル mp4 / 取得失敗) では comment が常に 0 になり、
# total = emotion * 0.55 で満点が 55 点にとどまっていた。順位付けは変わらないが、
# 採点グラフの目盛りが 0..100 で読めず「満点でも 55」という状態だった。
# normalize_weights=True のときはコメント側の重みを感情側へ寄せ、0..100 に収める。
def _weighted_total(emotion, comment, cfg):
    weights = cfg["weights"]
    w_emotion = float(weights.get("emotion", 0.55))
    w_comment = float(weights.get("comment", 0.45))
    if cfg.get("normalize_weights", True) and not cfg.get("comment_spike", False):
        # コメント未取得 = コメント点は常に 0。感情点だけで 0..100 へ正規化する。
        total_weight = w_emotion + w_comment
        if total_weight > 0:
            w_emotion, w_comment = total_weight, 0.0
    return emotion * w_emotion + comment * w_comment


# ------------------------------------------------------------------
# AI の山を窓スコアへ混ぜる (docs/AiEditor/resolve2.md §6.1 / P3)
# ------------------------------------------------------------------
#
# 採点そのものは変えない。既存の窓 (180 秒 / 45 秒スライド) をそのまま使い、
# **total だけ**を混ぜる。
#
#   ai_window = 窓に重なる山の scan_score の最大値 (重なりが無ければ 0)
#   total     = ai_weight * ai_window + (1 - ai_weight) * machine_total
#
# emotion / comment は**機械の値をそのまま残す**。採点グラフの内訳表示と
# セクション追加の best_window_for_range がこの 2 つを見ているためである。
# 窓には ai_score / moment_index を**足す** (既存の鍵は消さない)。
#
# ここは純粋関数であり ai/ を import しない。山は呼び出し側が用意して渡す
# (pipeline → ai/ の向きを作らない / resolve1.md §4)。


# 窓スコア列へ AI の山を混ぜた新しい列を返す。
#
# curve    : score_window の戻り値の一覧
# moments  : [{"start","end","scan_score"}] (ai/scene_scanner.Moment.as_dict())
# ai_weight: 0..1。0 なら**完全に従来どおり** (混ぜない)
#
# 山が空、または ai_weight が 0 のときは curve をそのまま返す。
def blend_ai_moments(curve, moments, ai_weight):
    weight = _clamp(float(ai_weight or 0.0), 0.0, 1.0)
    rows = list(curve or [])
    items = [m for m in (moments or []) if isinstance(m, dict)]
    if weight <= 0.0 or not items or not rows:
        return rows

    blended = []
    for window in rows:
        row = dict(window)
        start = float(row.get("start", 0.0))
        end = float(row.get("end", 0.0))
        best, index = _best_moment(items, start, end)
        machine = float(row.get("total", 0.0))
        row["ai_score"] = round(best, 1)
        row["moment_index"] = index
        # 方式 A の点を控える (同点の解きと縮退に使う / resolve3 §3.2)
        row["machine_total"] = round(machine, 1)
        row["total"] = round(weight * best + (1.0 - weight) * machine, 1)
        blended.append(row)

    return blended


# 窓スコアの total を **AI の点だけ**で置き換えた新しい列を返す (resolve3 §3.1 / §3.2)。
#
# curve   : score_window の戻り値の一覧
# moments : [{"start","end","scan_score"}] (ai/scene_scanner.Moment.as_dict())
#
# blend_ai_moments を ai_weight=1.0 で呼ぶと「山に重なる窓が全て同点」になり
# 順位が付かない (resolve2 §14.2 の実測)。そこで**重なりの深さ**で差を付ける。
#
#   重なり率 = 重なり秒 / min(窓長, 山長)   (0..1 へ丸める)
#   total    = 山の scan_score × 重なり率
#
# 山を丸ごと含む窓は満点、端をかすめる窓は比例して低い。山に重ならない窓は 0.0。
# emotion / comment は**機械の値のまま**残し、方式 A の点は machine_total へ控える
# (ai_only では 0 点の窓が大量に出るため、同点は machine_total で解く / select_top_events)。
# 山が空なら curve をそのまま返す (= 方式 A のまま / 縮退 / §3.5)。
def score_by_ai_moments(curve, moments):
    rows = list(curve or [])
    items = [m for m in (moments or []) if isinstance(m, dict)]
    if not items or not rows:
        return rows

    scored = []
    for window in rows:
        row = dict(window)
        start = float(row.get("start", 0.0))
        end = float(row.get("end", 0.0))
        best, index, ratio = _best_moment_ratio(items, start, end)
        row["machine_total"] = round(float(row.get("total", 0.0)), 1)
        row["ai_score"] = round(best, 1)
        row["moment_index"] = index
        row["total"] = round(best * ratio, 1)
        scored.append(row)

    return scored


# 窓に最も強く効く山 (重なるもののうち scan_score が最大) を返す。
# 戻り値: (点, 山の番号)。重なりが無ければ (0.0, None)。
def _best_moment(moments, start, end):
    best = 0.0
    index = None
    for position, moment in enumerate(moments):
        if not _overlaps(start, end, float(moment.get("start", 0.0)),
                         float(moment.get("end", 0.0))):
            continue
        score = float(moment.get("scan_score", 0.0))
        if score > best or index is None:
            best = score
            index = position

    return max(best, 0.0), index


# 窓に最も強く効く山と、その**重なりの深さ**を返す (resolve3 §3.2)。
# 戻り値: (点, 山の番号, 重なり率 0..1)。重なりが無ければ (0.0, None, 0.0)。
#
# 山の選び方は _best_moment と同じ規則 (重なるもののうち scan_score が最大) であり、
# 重なり率は**選ばれた山**について測る。
def _best_moment_ratio(moments, start, end):
    best = 0.0
    index = None
    ratio = 0.0
    window_len = max(float(end) - float(start), 0.0)
    for position, moment in enumerate(moments):
        m_start = float(moment.get("start", 0.0))
        m_end = float(moment.get("end", 0.0))
        if not _overlaps(start, end, m_start, m_end):
            continue
        score = float(moment.get("scan_score", 0.0))
        if score > best or index is None:
            overlap = min(float(end), m_end) - max(float(start), m_start)
            # 窓より短い山は「丸ごと入っていれば満点」とする (山長で割る)
            span = min(window_len, max(m_end - m_start, 0.0))
            best = score
            index = position
            ratio = _clamp(overlap / span, 0.0, 1.0) if span > 0 else 0.0

    return max(best, 0.0), index, ratio


# 2区間が時間的に重なるか
def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


# 区間 [start,end] に重なる窓のうち total が最大のものを返す (無ければ None)
# 採点し直さずに区間のスコアを決める唯一の規則。追加セクション (ver3 resolve13 §5.4) と
# マーカー由来セクション (ver3 resolve16 §5.4) の両方がこれを使う。
def best_window_for_range(scored, start_sec, end_sec):
    best = None
    for w in (scored or []):
        if not w:
            continue
        if (float(w.get("start", 0.0)) < float(end_sec)
                and float(start_sec) < float(w.get("end", 0.0))):
            if best is None or float(w.get("total", 0.0)) > float(best.get("total", 0.0)):
                best = w
    return best


# 区間のスコア (重なる窓の total 最大 / 重なりが無ければ 0.0)
def score_for_range(scored, start_sec, end_sec):
    best = best_window_for_range(scored, start_sec, end_sec)
    return round(float(best.get("total", 0.0)), 1) if best else 0.0


# 統合先として控える 1 件を複製する。labels は統合で追記するため実体も分ける
# (呼び出し側が渡したリストを書き換えないため)。
def _copy_section(clip):
    copied = dict(clip)
    copied["labels"] = list(clip.get("labels") or [])
    copied["marker"] = bool(clip.get("marker"))
    return copied


# start 昇順のクリップ列から、時間が連続(接触)または重なる区間を1つのセクションへ統合する。
# 統合区間は union([start,end]) とし、3分窓の固定枠を外して実際の連続範囲にする。
# 代表スコア(score/emotion/comment)は統合対象のうち最大 total の窓の値を採用する。
# マーカー由来の印 (marker/labels) は統合先へ引き継ぐ (ver3 resolve16 §5.4)。
def _merge_time_sections(clips):
    if not clips:
        return []
    merged = [_copy_section(clips[0])]
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
            # 片方でもマーカー由来なら統合後もマーカー由来として扱う
            cur["marker"] = bool(cur.get("marker")) or bool(c.get("marker"))
            cur.setdefault("labels", [])
            for label in (c.get("labels") or []):
                if label not in cur["labels"]:
                    cur["labels"].append(label)
        else:
            merged.append(_copy_section(c))
    return merged


# 採点済み窓から重なりを避けて上位 top_n をイベントとして選び、
# その中で時間が連続 or 重なるものは 1 つのクリップ(セクション)へ統合して返す。
# 1) 高得点の窓を貪欲採用し、重なる窓を除外 → 連続する高得点窓群はピーク窓に代表される (§4.5)。
# 2) clip_pad_sec で前後に余白を付け duration でクランプ。
# 3) 連続(接触)/重なりの採用区間を union で1セクションに統合 (3分枠を外す)。要望: TOP10内の
#    継続 or 重なりは1クリップ。
# forced : 必ず残す区間 [{"start","end","label"}] (ver3 resolve16 / 要望 I2)。
#   ストリームマーカーの前後 N 分がこれにあたる。top_n の枠は消費せず、採点で選ばれた
#   区間と合流させてから同じマージ規約に通す (resolve16 §3-3・§3-4)。スコアは採点し
#   直さず、重なる窓の最大値を代表として採る (best_window_for_range)。
#   None/空なら結果は従来と同一 (marker/marker_labels が付くだけ)。
# target_total_sec : 集める素材尺の目標 (resolve3.md §9 / 利用者の指示 2026-11-26)。
#   0 以下 … 従来どおり top_n 件で打ち切る (経路 B はこちら)
#   0 超   … **点の高い順に積み、素材尺が目標に達したら終了する**。
#            top_n は使わない (件数ではなく尺で決める)。
#            素材が尽きれば目標未満で終わる (無い物は選べない)。
def select_top_events(scored, top_n, clip_pad_sec, duration, forced=None,
                      target_total_sec=0.0):
    # 同点は方式 A の点 (machine_total) で解く (resolve3 §3.2)。
    # ai_only では山に重ならない窓が**全て 0 点**になるため、ここが無いと
    # 残りの枠が時間順 (配信の冒頭から) で埋まってしまう。
    # machine_total を持たない従来の呼び出しでは 0.0 となり、並びは変わらない。
    ordered = sorted((w for w in scored if w),
                     key=lambda w: (float(w["total"]),
                                    float(w.get("machine_total", 0.0) or 0.0)),
                     reverse=True)
    target = max(float(target_total_sec or 0.0), 0.0)
    chosen = []
    picked_sec = 0.0
    for w in ordered:
        if target > 0.0:
            if picked_sec >= target:
                break
        elif len(chosen) >= top_n:
            break
        if any(_overlaps(w["start"], w["end"], c["start"], c["end"]) for c in chosen):
            continue
        chosen.append(w)
        picked_sec += max(float(w["end"]) - float(w["start"]), 0.0)

    # 時系列順に並べ、パディングを付与
    chosen.sort(key=lambda w: w["start"])
    padded = []
    for w in chosen:
        cstart = max(0.0, w["start"] - clip_pad_sec)
        cend = min(duration, w["end"] + clip_pad_sec) if duration > 0 else w["end"] + clip_pad_sec
        padded.append({
            "start": cstart, "end": cend, "total": w["total"],
            "emotion": w["emotion"], "comment": w["comment"],
            "marker": False, "labels": [],
        })

    # 強制区間 (マーカー由来) を合流させる。パディングは付けない
    # (前後 N 分の指定そのものが余白のため / resolve16 §5.4)。
    for f in (forced or []):
        fstart = max(0.0, float(f["start"]))
        fend = min(duration, float(f["end"])) if duration > 0 else float(f["end"])
        if fend <= fstart:
            continue
        best = best_window_for_range(scored, fstart, fend)
        padded.append({
            "start": fstart, "end": fend,
            "total": round(float(best["total"]), 1) if best else 0.0,
            "emotion": float(best["emotion"]) if best else 0.0,
            "comment": float(best["comment"]) if best else 0.0,
            "marker": True,
            "labels": [f["label"]] if f.get("label") else [],
        })
    padded.sort(key=lambda w: w["start"])

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
            # ストリームマーカー由来を含むか / そのマーカーの説明 (ver3 resolve16 §5.8)
            "marker": bool(m.get("marker")),
            "marker_labels": list(m.get("labels") or []),
        })
    return clips
