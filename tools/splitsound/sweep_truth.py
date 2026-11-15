# Split Sound 実験: 閾値を振って「最終動画に出るテロップ」の品質を正解データで測る
# (docs/request/ver10/resolve_ss1.md §3.4 / result_ss1.md §10)
#
# なぜ要るか
#   evaluate.py の M3 (CER) は認識結果を**全部**つないで正解と比べる。
#   これは「認識エンジンの素の精度」であり、**閾値に依存しない**。
#   しかし実機では残す区間の外のテロップは最終動画に出ない。
#     src/modules/subtitle_generator.py map_items_to_timeline():
#       ・一部がカットへかかる → かかった分だけ短くなる
#       ・全体がカットへ落ちる → 載せない (カットした無音への幻聴字幕もここで落ちる)
#   つまり**閾値を変えると利用者が見るテロップが変わる**。そこを測るのが本スクリプト。
#
# なぜ安いか
#   認識は**素材そのまま**に対して行う (subtitle_generator.py の recognize_for_timeline /
#   20260812 resolve2.md §4) ため、認識結果は閾値に依存しない。
#   よって **ASR の再実行は要らない**。既にある result.json の items を
#   閾値ごとに絞り直すだけで済む (S1 の認識は 14 分かかるが、それを回避できる)。
#
# 測るもの (閾値ごと)
#   M1 / M2 / M2'   残す区間の当たり外れ            (evaluate.silence_metrics)
#   M3_kept         **残ったテロップだけ**で計算した CER (evaluate.cer)
#   M4 / M5 / M6    テロップの出どころ              (evaluate.subtitle_metrics)
#   dropped / clipped  閾値で落ちた件数 / 短くなった件数
#
# 【実機との差 (読むときの注意)】
#   ハーネスの items は**認識の素の結果**で、実機が通す
#   speaker_filter (配信者以外の声を落とす / F15 / docs/quality/resolve1.md §12.5) と
#   テロップの単位再構成 (regroup_items_by_length) を通っていない。
#   実機の M4 はここで出る数より小さくなる。**閾値による変化の向きと大きさ**を見る道具である。
import argparse
import csv
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows のコンソール (cp932) で出せない文字があっても落ちないようにする
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # noqa: BLE001 (古い環境では reconfigure が無い)
    pass

import common    # noqa: E402
import evaluate  # noqa: E402  (指標の計算はすべて evaluate.py を再利用する)
import sweep     # noqa: E402  (条件ごとの音声パスの引き当てを再利用する)

# 既定で振る閾値 (dB) — sweep.py と同じグリッドにして代理指標と並べて読めるようにする
_DEFAULT_THRESHOLDS = sweep._DEFAULT_THRESHOLDS

# 区間が重なっているとみなす最小の秒数 (浮動小数の誤差対策)
_EPS = 1e-6


# 残す区間に残るテロップだけを返す
#
# 落ちる / 残るの判定は**実機と同じ機構**を使う (src/timeline/timemap.py の
# TimeMap.split_interval)。map_items_to_timeline() が空の結果を「載せない」と
# 扱うため、ここでも空なら落とす。
#
# 表示区間は素材時間で「残す区間と重なる部分」へ詰める。
# 実機は詰めた軸 (Timeline 時間) へ写すが、正解データは素材時間で付けてあるため、
# ここでは素材時間のまま短くする (M4 の重なり判定を実機に合わせるため)。
# 本文は変えない (実機でも本文は変わらず表示時間だけ短くなる)。
#
# 戻り値: (残ったテロップ, 落ちた件数, 短くなった件数)
def filter_items(items, keep_segments):
    common.ensure_repo_path()
    from src.timeline.timemap import TimeMap

    timemap = TimeMap.from_segments(keep_segments)
    kept = []
    dropped = 0
    clipped = 0
    for item in items:
        start = float(item["start"])
        end = float(item["end"])
        if not timemap.split_interval(start, end):
            # 全体がカットへ落ちる = 最終動画に出ない
            dropped += 1
            continue
        # 素材時間での重なり (実機の「かかった分だけ短くなる」に相当)
        spans = [(max(start, float(s)), min(end, float(e)))
                 for s, e in keep_segments
                 if min(end, float(e)) - max(start, float(s)) > _EPS]
        if not spans:
            # split_interval が非空なら通常ここへは来ない (同じ keep を見ているため)
            dropped += 1
            continue
        new_start = spans[0][0]
        new_end = spans[-1][1]
        if new_start > start + _EPS or new_end < end - _EPS:
            clipped += 1
        kept.append({**item, "start": new_start, "end": new_end})
    return kept, dropped, clipped


# 1 本の音声について閾値を振る
def sweep_one(path, settings, duration, items, truth, masks, thresholds, min_silence):
    common.ensure_repo_path()
    from src.modules import silence_cutter

    rows = []
    for threshold in thresholds:
        silence = silence_cutter.detect_silence(
            path, threshold, min_silence, settings.get("ffmpeg", {}))
        keep_segments = silence_cutter.build_keep_segments(silence, duration)
        kept_items, dropped, clipped = filter_items(items, keep_segments)

        row = {
            "threshold_db": threshold,
            "keep_count": len(keep_segments),
            "items_all": len(items),
            "items_kept": len(kept_items),
            "dropped_items": dropped,
            "clipped_items": clipped,
        }
        row.update(evaluate.silence_metrics(keep_segments, masks, duration))
        # 残存率は M2' (尺削減率) の裏返し。evaluate.py と同じ 10ms グリッド基準に
        # 揃えるため、区間長の素朴な合計ではなくこちらから導く。
        row["kept_ratio"] = round(1.0 - row["M2d_reduction"], 4)

        # 認識結果を全部使った CER (閾値非依存 / evaluate.py が出しているもの) と
        # 残ったテロップだけの CER を並べる。差が「閾値がテロップ品質に与える影響」。
        if kept_items:
            value, ref_len, hyp_len, info = evaluate.cer(truth, kept_items, duration)
            row["M3_cer_kept"] = value
            row["ref_chars"] = ref_len
            row["hyp_chars_kept"] = hyp_len
            row["cer_scored_ratio"] = info.get("scored_ratio")
            row.update(evaluate.subtitle_metrics(truth, kept_items, masks, duration))
        else:
            row["M3_cer_kept"] = None
            row["items"] = 0
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="閾値を振って「最終動画に出るテロップ」の品質を正解データで測る")
    parser.add_argument("--materials", default="",
                        help="カンマ区切りの素材 ID (既定: 正解データがある全部)")
    parser.add_argument("--conditions", default="c0_baseline",
                        help="カンマ区切りの条件 (既定: c0_baseline)")
    parser.add_argument("--thresholds", default="",
                        help="カンマ区切りの dB (既定 -14..-40)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    common.setup_logging(args.verbose)
    if not args.verbose:
        logging.getLogger("src.modules.silence_cutter").setLevel(logging.WARNING)
    settings = common.load_app_settings()
    thresholds = ([int(x) for x in args.thresholds.split(",") if x.strip()]
                  or list(_DEFAULT_THRESHOLDS))
    min_silence = float(settings.get("volume_analysis", {})
                        .get("cut_min_silence_sec", 0.6))

    definition = common.load_json(common.materials_path())
    wanted = {x.strip() for x in args.materials.split(",") if x.strip()}
    conditions = [x.strip() for x in args.conditions.split(",") if x.strip()]

    report = {}
    for material in definition.get("materials", []):
        material_id = material["id"]
        if wanted and material_id not in wanted:
            continue
        if material.get("metrics") == "time_only":
            continue
        truth, truth_meta = evaluate.load_truth(material_id)
        if not truth:
            if wanted:
                print(f"  {material_id}: 正解データがありません "
                      f"(docs/request/ver10/ss1_truth/{material_id}.jsonl)",
                      file=sys.stderr)
            continue

        paths = sweep.audio_paths(material_id)
        entry = {
            "kind": material.get("kind", ""),
            "label": material.get("label", ""),
            "conditions": {},
        }
        for condition in conditions:
            path = paths.get(condition)
            result_path = os.path.join(
                common.condition_dir(material_id, condition), "result.json")
            if not path or not os.path.exists(result_path):
                continue
            result = common.load_json(result_path)
            items = (result.get("asr") or {}).get("items") or []
            if not items:
                print(f"  {material_id} / {condition}: 認識結果がありません "
                      f"(run_conditions.py を --no-asr なしで)", file=sys.stderr)
                continue
            duration = float(result.get("duration_sec") or 0.0)
            masks = evaluate.truth_masks(truth, duration, truth_meta)

            # 現行の自動閾値 (印として表に出す)
            auto_db = (result.get("thresholds", {}).get("T1", {}) or {}).get("db")
            entry["duration_sec"] = round(duration, 1)
            entry["conditions"][condition] = {
                "auto_threshold_db": auto_db,
                "rows": sweep_one(path, settings, duration, items, truth,
                                  masks, thresholds, min_silence),
            }
            best = max(entry["conditions"][condition]["rows"],
                       key=lambda r: (r["M1_f1"] or 0.0))
            print(f"  {material_id} / {condition}: 現行 {auto_db}dB / "
                  f"M1 最良 {best['threshold_db']}dB (M1 {best['M1_f1']}) ",
                  file=sys.stderr)
        if entry["conditions"]:
            report[material_id] = entry

    if not report:
        print("対象がありません (正解データと C0 の認識結果が必要)", file=sys.stderr)
        return 1

    out = os.path.join(common.WORK_ROOT, "sweep_truth.json")
    common.save_json(out, report)
    _write_csv(report, os.path.join(common.WORK_ROOT, "sweep_truth.csv"))
    _write_markdown(report, os.path.join(common.WORK_ROOT, "sweep_truth.md"))
    print(f"\n書きました: {out} と sweep_truth.md / sweep_truth.csv", file=sys.stderr)
    return 0


_CSV_COLUMNS = [
    "material", "condition", "threshold_db", "auto",
    "keep_count", "kept_ratio", "M1_f1", "precision", "recall",
    "M2_game_only_kept", "M2d_reduction",
    "items_all", "items_kept", "dropped_items", "clipped_items",
    "M3_cer_kept", "ref_chars", "hyp_chars_kept", "cer_scored_ratio",
    "M4_game_items", "M4_game_ratio", "M5_missed_ratio", "M6_hallucination",
]


def _write_csv(report, path):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for material_id, entry in report.items():
            for condition, block in entry["conditions"].items():
                for row in block["rows"]:
                    out = dict(row)
                    out["material"] = material_id
                    out["condition"] = condition
                    out["auto"] = ("←現行" if row["threshold_db"] ==
                                   block["auto_threshold_db"] else "")
                    writer.writerow(out)
    return path


# 読みやすい表を書き出す (コンソールの文字コードに左右されないため)
def _write_markdown(report, path):
    lines = [
        "# 閾値スイープ (正解データ / 最終動画に出るテロップ)",
        "",
        "認識は素材そのままに対して行うため**閾値に依存しない**。",
        "閾値が変えるのは「残す区間の外のテロップが落ちること」だけである",
        "(`src/modules/subtitle_generator.py` の `map_items_to_timeline`)。",
        "この表はその影響を正解データで測ったもの。",
        "",
        "> ハーネスの認識結果は**素の結果**で、実機の `speaker_filter` (配信者以外の声を",
        "> 落とす / F15) とテロップの単位再構成を通っていない。実機の M4 はこれより小さい。",
        "",
    ]
    for material_id, entry in report.items():
        lines.append(f"## {material_id} — {entry.get('label','')} ({entry.get('kind','')})")
        lines.append("")
        lines.append(f"- 尺 {entry.get('duration_sec')}s")
        lines.append("")
        for condition, block in entry["conditions"].items():
            lines.append(f"### {condition} (現行の自動閾値 "
                         f"**{block['auto_threshold_db']}dB**)")
            lines.append("")
            lines.append("| 閾値 | 残す区間 | 残存率 | M1 | M2 | テロップ 残/全 | "
                         "落ちた | 短縮 | CER(残存) | M4 | M5 | 印 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
            rows = block["rows"]
            best = max(rows, key=lambda r: (r["M1_f1"] or 0.0))
            for row in rows:
                marks = []
                if row["threshold_db"] == block["auto_threshold_db"]:
                    marks.append("**←現行**")
                if row is best:
                    marks.append("**←M1 最良**")
                m2 = row.get("M2_game_only_kept")
                cer_value = row.get("M3_cer_kept")
                m5 = row.get("M5_missed_ratio")
                lines.append(
                    f"| {row['threshold_db']} | {row['keep_count']} "
                    f"| {row['kept_ratio']:.3f} "
                    f"| {row['M1_f1']:.4f} "
                    f"| {('%.4f' % m2) if m2 is not None else '-'} "
                    f"| {row['items_kept']}/{row['items_all']} "
                    f"| {row['dropped_items']} "
                    f"| {row['clipped_items']} "
                    f"| {('%.4f' % cer_value) if cer_value is not None else '-'} "
                    f"| {row.get('M4_game_items', '-')} "
                    f"| {('%.1f%%' % (m5 * 100)) if m5 is not None else '-'} "
                    f"| {' '.join(marks)}".rstrip() + " |")
            lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


if __name__ == "__main__":
    raise SystemExit(main())
