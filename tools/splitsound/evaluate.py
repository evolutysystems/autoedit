# Split Sound 実験: 指標の計算 (docs/request/ver10/resolve_ss1.md §3.4)
#
# 入力
#   正解   : docs/request/ver10/ss1_truth/<素材ID>.jsonl (§3.3)
#   実行結果: output/_splitsound/<素材ID>/<条件>/result.json (run_conditions.py)
#
# 出力
#   output/_splitsound/metrics.csv  (条件 × 素材 × 指標の生表)
#   標準出力に Markdown の表
#
# 指標 (§3.4)
#   M1 残す区間の F1 / M2 ゲーム音のみ区間の残存率 / M2' 尺削減率
#   M3 CER / M4 ゲーム由来テロップ件数 / M5 取りこぼし率 / M6 ハルシネーション件数
#   M7 処理時間 (工程別の秒数と RTF)
#
# 評価は 10ms グリッドの集合演算で行う。区間の端の扱いで数字が揺れないようにするため。
import argparse
import csv
import json
import os
import re
import sys
import unicodedata

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402

# 評価グリッド (秒)
FRAME_SEC = 0.01

# 正解データの置き場
TRUTH_DIR = os.path.join(common.REPO_ROOT, "docs", "request", "ver10", "ss1_truth")

# M4 の判定: テロップの表示区間が「ゲーム音のみ」に何割重なったらゲーム由来とみなすか
_GAME_OVERLAP_RATIO = 0.5

# 人が聞き取れなかったことを表す印 (正解の本文に入れる)
_UNCLEAR_MARK = "[?]"

# 聞き取れない区間と何割重なったテロップを CER の採点から外すか
_UNCLEAR_OVERLAP_RATIO = 0.5


# ------------------------------------------------------------------
# 正解データ
# ------------------------------------------------------------------

# 正解 JSONL を読む (§3.3)
#
# 先頭に {"_meta": {...}} の行を置けるようにしてある。
#   game_everywhere: true → **全区間にゲーム音が鳴っている**ものとして扱う。
#     S1 / S2 のように「現行の閾値で無音が 1 件も出ない」素材では実測で成立しており、
#     これを宣言すると**配信者の発話だけラベル付けすれば M2 / M4 が計算できる**
#     (ゲーム音のみの区間 = 配信者の発話以外の全部、と決まるため)。
# 戻り値: (行の一覧, メタ) / ファイルが無ければ (None, {})
def load_truth(material_id):
    path = os.path.join(TRUTH_DIR, f"{material_id}.jsonl")
    if not os.path.exists(path):
        return None, {}
    rows = []
    meta = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            row = json.loads(line)
            if "_meta" in row:
                meta = row["_meta"] or {}
                continue
            speaker = str(row.get("speaker", "") or "").strip().lower()
            if not speaker:
                # ラベル未設定の行は評価に入れない (下書きのまま混ざるのを防ぐ)
                continue
            text = str(row.get("text", "") or "")
            rows.append({
                "start": float(row["start"]),
                "end": float(row["end"]),
                "speaker": speaker,
                "text": text,
                # 人が聞き取れなかった行 (§ 聞き取れない場合)。
                # 正解の文字列を作れないため **CER の採点から外す**。
                # 残す / 切るの判定 (M1 / M2) には従来どおり使う。
                "unclear": bool(row.get("unclear")) or _UNCLEAR_MARK in text,
            })
    return rows, meta


# 区間の一覧を 10ms グリッドの真偽配列へ展開する
def to_frames(spans, duration):
    size = int(round(duration / FRAME_SEC))
    mask = np.zeros(size, dtype=bool)
    for start, end in spans:
        begin = max(int(round(float(start) / FRAME_SEC)), 0)
        finish = min(int(round(float(end) / FRAME_SEC)), size)
        if finish > begin:
            mask[begin:finish] = True
    return mask


# 話者ごとのフレーム集合を作る (H = 配信者 / G = ゲーム / O = その他)
#
# meta["game_everywhere"] が真なら、ゲーム音は全区間で鳴っているものとする
# (= 「ゲーム音のみの区間」は配信者の発話以外の全部になる / load_truth の注記)。
def truth_masks(truth, duration, meta=None):
    def spans(*kinds):
        return [(row["start"], row["end"]) for row in truth
                if row["speaker"] in kinds]

    # 共演者 (sub) も「残したい声」なので host と同じ側に入れる
    # (複数人の配信では共演者の声を切ってはいけない / truth_schema.md)
    host = to_frames(spans("host", "sub"), duration)
    if (meta or {}).get("game_everywhere"):
        game = np.ones(len(host), dtype=bool)
    else:
        game = to_frames(spans("game"), duration)
    return {"host": host, "game": game, "other": to_frames(spans("other"), duration)}


# ------------------------------------------------------------------
# 指標
# ------------------------------------------------------------------

# M1 / M2 / M2' — 残す区間の当たり外れ
def silence_metrics(keep_segments, masks, duration):
    keep = to_frames(keep_segments, duration)
    host = masks["host"]
    game_only = masks["game"] & ~host

    kept = int(keep.sum())
    hit = int((keep & host).sum())
    precision = hit / kept if kept else 0.0
    recall = hit / int(host.sum()) if host.any() else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    game_kept = (int((keep & game_only).sum()) / int(game_only.sum())
                 if game_only.any() else None)
    return {
        "M1_f1": round(f1, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "M2_game_only_kept": round(game_kept, 4) if game_kept is not None else None,
        "M2d_reduction": round(1.0 - kept / len(keep), 4),
    }


# 比較用にテキストを正規化する (§3.4 M3)
# 空白除去 / 記号・句読点除去 / 全角英数を半角へ。カタカナはそのまま。
def normalize_text(text):
    # ASS の改行記号 (\N / \n) は字幕の体裁であって認識内容ではないため落とす
    value = re.sub(r"\\[Nn]", "", str(text or ""))
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[、。，．,\.!？?！「」『』（）\(\)\[\]【】…ー~〜・:：;；\"'`]", "", value)


# レーベンシュタイン距離 (2 行の DP で持つ)
def levenshtein(left, right):
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, lchar in enumerate(left, start=1):
        current = [i]
        for j, rchar in enumerate(right, start=1):
            current.append(min(
                previous[j] + 1,            # 削除
                current[j - 1] + 1,         # 挿入
                previous[j - 1] + (lchar != rchar),  # 置換
            ))
        previous = current
    return previous[-1]


# M3 — CER (文字誤り率)
#
# 聞き取れなかった区間 (unclear) は**両側から外す**。
#   ・正解からその行の本文を外す (正解が作れないため)
#   ・仮説からもその区間に重なるテロップを外す
# 片側だけ外すと、認識が正しく出していても挿入誤りとして数えられてしまう。
#
# 戻り値: (CER, 正解文字数, 仮説文字数, 採点から外した情報)
def cer(truth, items, duration=0.0):
    keep_roles = ("host", "sub")
    scored = [row for row in sorted(truth, key=lambda r: r["start"])
              if row["speaker"] in keep_roles and not row["unclear"]]
    unclear = [row for row in truth
               if row["speaker"] in keep_roles and row["unclear"]]

    # 本文は開始時刻の順に繋ぐ。
    # **時刻が重なる行があってよい** (同じ区間を 2 人が喋った場合は 2 行に分ける)。
    # 生成テロップ側も時刻順に繋ぐので、重なりがあっても突き合わせは成立する。
    reference = normalize_text("".join(row["text"] for row in scored))

    # 採点から外す時間 = 聞き取れない行の区間から、**採点できる行の区間を引いたもの**。
    # 引かないと、重なっている採点対象の発話のテロップまで外してしまい、
    # 正解にあるのに仮説から消える = 不当な削除誤りになる (重なり発話で起きる)。
    span_end = max([float(i["end"]) for i in items] +
                   [r["end"] for r in truth] + [float(duration or 0.0)] + [0.0])
    unclear_mask = to_frames([(r["start"], r["end"]) for r in unclear], span_end)
    scored_mask = to_frames([(r["start"], r["end"]) for r in scored], span_end)
    net_unclear = unclear_mask & ~scored_mask

    dropped = 0
    kept_items = []
    for item in sorted(items, key=lambda i: float(i["start"])):
        if _mask_overlap_ratio(item, net_unclear) >= _UNCLEAR_OVERLAP_RATIO:
            dropped += 1
            continue
        kept_items.append(item)
    hypothesis = normalize_text("".join(item["text"] for item in kept_items))

    info = {
        "unclear_rows": len(unclear),
        "unclear_sec": round(sum(r["end"] - r["start"] for r in unclear), 1),
        "dropped_items": dropped,
    }
    total_keep = sum(r["end"] - r["start"] for r in truth
                     if r["speaker"] in keep_roles)
    if total_keep > 0:
        info["scored_ratio"] = round(
            sum(r["end"] - r["start"] for r in scored) / total_keep, 3)
    if not reference:
        return None, 0, 0, info
    distance = levenshtein(reference, hypothesis)
    return round(distance / len(reference), 4), len(reference), len(hypothesis), info


# item の表示区間のうち、mask が立っているフレームの割合を返す
def _mask_overlap_ratio(item, mask):
    begin = max(int(round(float(item["start"]) / FRAME_SEC)), 0)
    finish = min(int(round(float(item["end"]) / FRAME_SEC)), len(mask))
    if finish <= begin:
        return 0.0
    return float(mask[begin:finish].sum()) / float(finish - begin)


# M4 / M5 / M6 — テロップの出どころ
def subtitle_metrics(truth, items, masks, duration):
    host = masks["host"]
    game_only = masks["game"] & ~host
    # game_everywhere のときは全区間が「ゲーム音あり」になるため、
    # M6 (完全な無音に出たテロップ) は 0 件になる。その素材では M6 を見ない。
    labelled = masks["host"] | masks["game"] | masks["other"]

    game_sourced = 0
    hallucination = 0
    for item in items:
        span = to_frames([(item["start"], item["end"])], duration)
        size = int(span.sum())
        if not size:
            continue
        if int((span & game_only).sum()) / size >= _GAME_OVERLAP_RATIO:
            game_sourced += 1
        if not (span & labelled).any():
            hallucination += 1

    # 取りこぼしの対象は「残したい声」= host と sub。
    # 聞き取れなかった行 (unclear) も喋ってはいるので対象に残す
    # (本文が無いだけで、テロップが出るべき区間であることは変わらない)。
    host_rows = [row for row in truth if row["speaker"] in ("host", "sub")]
    missed = 0
    for row in host_rows:
        covered = any(float(item["end"]) > row["start"] and float(item["start"]) < row["end"]
                      for item in items)
        if not covered:
            missed += 1
    return {
        "M4_game_items": game_sourced,
        "M4_game_ratio": round(game_sourced / len(items), 4) if items else None,
        "M5_missed_ratio": round(missed / len(host_rows), 4) if host_rows else None,
        "M6_hallucination": hallucination,
        "items": len(items),
        "_unused_host_frames": int(host.sum()),
    }


# M7 — 工程別の処理時間
def timing_metrics(result):
    separation = (result.get("separation") or {}).get("elapsed_sec") or 0.0
    threshold = (result.get("thresholds", {}).get("T1", {}) or {}).get("elapsed_sec") or 0.0
    silence = (result.get("silence", {}).get("T1", {}) or {}).get("elapsed_sec") or 0.0
    asr = (result.get("asr") or {}).get("elapsed_sec") or 0.0
    duration = float(result.get("duration_sec") or 0.0)
    total = float(separation) + float(threshold) + float(silence) + float(asr)
    return {
        "t_separation": round(float(separation), 1),
        "t_threshold": round(float(threshold), 1),
        "t_silence": round(float(silence), 1),
        "t_asr": round(float(asr), 1),
        "t_total": round(total, 1),
        "rtf_total": round(total / duration, 3) if duration else None,
    }


# ------------------------------------------------------------------
# 集計
# ------------------------------------------------------------------

# 1 件 (素材 × 条件 × 閾値) の指標をまとめる
def evaluate_one(material_id, condition, result, truth, meta=None):
    duration = float(result.get("duration_sec") or 0.0)
    rows = []
    masks = truth_masks(truth, duration, meta) if truth else None

    for name in ("T1", "T2"):
        silence = result.get("silence", {}).get(name) or {}
        row = {
            "material": material_id,
            "condition": condition,
            "threshold": name,
            "threshold_db": (result.get("thresholds", {}).get(name, {}) or {}).get("db"),
            "keep_count": silence.get("keep_count"),
            "kept_ratio": silence.get("kept_ratio"),
        }
        if masks and silence.get("keep_segments") is not None:
            row.update(silence_metrics(silence["keep_segments"], masks, duration))
        row.update(timing_metrics(result))
        if name == "T1":
            items = (result.get("asr") or {}).get("items") or []
            if truth and items:
                value, ref_len, hyp_len, info = cer(truth, items, duration)
                row["M3_cer"] = value
                row["ref_chars"] = ref_len
                row["hyp_chars"] = hyp_len
                row["cer_scored_ratio"] = info.get("scored_ratio")
                row["unclear_rows"] = info["unclear_rows"]
                row["unclear_sec"] = info["unclear_sec"]
                row.update(subtitle_metrics(truth, items, masks, duration))
            else:
                row["items"] = len(items)
        rows.append(row)
    return rows


# 出力表の列 (無い値は空で埋める)
_COLUMNS = [
    "material", "condition", "threshold", "threshold_db",
    "keep_count", "kept_ratio",
    "M1_f1", "precision", "recall", "M2_game_only_kept", "M2d_reduction",
    "M3_cer", "ref_chars", "hyp_chars", "cer_scored_ratio", "unclear_rows", "unclear_sec",
    "M4_game_items", "M4_game_ratio", "M5_missed_ratio", "M6_hallucination", "items",
    "t_separation", "t_threshold", "t_silence", "t_asr", "t_total", "rtf_total",
]


def main():
    parser = argparse.ArgumentParser(description="実験結果から指標を計算する")
    parser.add_argument("--materials", default="")
    parser.add_argument("--conditions",
                        default="c0_baseline,c1_htdemucs,c2_df3,c3_gate,c4_df3_demucs")
    args = parser.parse_args()

    definition = common.load_json(common.materials_path())
    wanted = {x.strip() for x in args.materials.split(",") if x.strip()}
    conditions = [x.strip() for x in args.conditions.split(",") if x.strip()]

    rows = []
    for material in definition.get("materials", []):
        material_id = material["id"]
        if wanted and material_id not in wanted:
            continue
        truth, truth_meta = load_truth(material_id)
        if truth is None and material.get("metrics") != "time_only":
            print(f"  (正解データなし) {material_id}: "
                  f"{os.path.join('docs/request/ver10/ss1_truth', material_id + '.jsonl')} "
                  f"が無いため精度の指標は空になります", file=sys.stderr)
        for condition in conditions:
            path = os.path.join(common.condition_dir(material_id, condition), "result.json")
            if not os.path.exists(path):
                continue
            rows.extend(evaluate_one(material_id, condition,
                                     common.load_json(path), truth, truth_meta))

    if not rows:
        print("結果がありません (先に run_conditions.py を実行する)", file=sys.stderr)
        return 1

    out_csv = os.path.join(common.WORK_ROOT, "metrics.csv")
    os.makedirs(common.WORK_ROOT, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    # 画面には主要列だけを出す
    show = ["material", "condition", "threshold", "threshold_db", "keep_count",
            "kept_ratio", "M1_f1", "M2_game_only_kept", "M3_cer", "M4_game_items",
            "t_separation", "rtf_total"]
    print("\n| " + " | ".join(show) + " |")
    print("|" + "---|" * len(show))
    for row in rows:
        print("| " + " | ".join(str(row.get(key, "")) for key in show) + " |")
    print(f"\n生表を書きました: {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
