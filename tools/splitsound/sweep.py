# Split Sound 実験: 閾値を振って「選択的に削れているか」を測る
# (docs/request/ver10/resolve_ss1.md §3.4 の代理指標 / result_ss1.md §4.5)
#
# なぜ要るか
#   残存率だけでは「正しく削れたか」が分からない。正解データ (人手ラベル) は
#   作るのに時間がかかるため、**正解データ無しで使える代理指標**でまず当たりを付ける。
#
# 代理指標
#   発話時間 = C0 (元音声) の認識結果の**語の時刻**を重ね合わせたもの。
#     認識の「区間」は VAD の都合で実発話より大幅に長くなる (実測: 10 文字の一言に
#     14〜25 秒の区間)。区間をそのまま使うと発話時間が 3 倍近く過大になるため、
#     必ず語の時刻 (word_spans) を使う。
#   発話の残存   = 残す区間 ∩ 発話 / 発話          (高いほど取りこぼしが少ない)
#   非発話の削除 = 非発話 − 残す区間 / 非発話       (高いほど不要な音を削れている)
#   調和平均     = 上 2 つの F1。閾値の良さを 1 つの数にする
#
# 【限界】ここでの「発話」は**認識できた声すべて**であり、配信者と
#   実況・周囲の人の声を区別しない。「配信者だけ残せたか」は正解データが要る。
import argparse
import json
import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402

# 評価グリッド (秒)
FRAME_SEC = 0.01

# 既定で振る閾値 (dB)
_DEFAULT_THRESHOLDS = (-14, -17, -20, -23, -26, -30, -34, -37, -40)


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


# C0 の認識結果から「語の時刻」で発話マスクを作る
# 戻り値: (マスク, 語の数, 区間ベースのマスク)
def speech_mask(material_id, duration):
    path = os.path.join(common.condition_dir(material_id, "c0_baseline"), "result.json")
    result = common.load_json(path)
    items = (result.get("asr") or {}).get("items") or []
    words = [tuple(span) for item in items for span in (item.get("word_spans") or [])]
    segments = [(item["start"], item["end"]) for item in items]
    if not words:
        raise RuntimeError(
            f"{material_id}: 語の時刻がありません。"
            "run_conditions.py を (--no-asr なしで) 実行し直してください")
    return to_frames(words, duration), len(words), to_frames(segments, duration)


# 1 本の音声について閾値を振る
def sweep_audio(path, settings, duration, speech, thresholds, min_silence):
    from src.modules import silence_cutter

    rows = []
    non_speech = ~speech
    for threshold in thresholds:
        silence = silence_cutter.detect_silence(
            path, threshold, min_silence, settings.get("ffmpeg", {}))
        keep = silence_cutter.build_keep_segments(silence, duration)
        mask = to_frames(keep, duration)
        retained = float((mask & speech).sum() / speech.sum()) if speech.any() else 0.0
        removed = (float((non_speech & ~mask).sum() / non_speech.sum())
                   if non_speech.any() else 0.0)
        harmonic = (2 * retained * removed / (retained + removed)
                    if (retained + removed) else 0.0)
        rows.append({
            "threshold_db": threshold,
            "keep_count": len(keep),
            "kept_ratio": round(float(mask.mean()), 4),
            "speech_retained": round(retained, 4),
            "nonspeech_removed": round(removed, 4),
            "harmonic": round(harmonic, 4),
        })
    return rows


# 条件ごとの解析用音声のパスを返す
def audio_paths(material_id):
    paths = {}
    base = os.path.join(common.material_dir(material_id), "analysis_source.wav")
    if os.path.exists(base):
        paths["c0_baseline"] = base
    for condition in ("c1_htdemucs", "c2_df3", "c3_gate", "c4_df3_demucs"):
        path = os.path.join(common.condition_dir(material_id, condition), "voice.wav")
        if os.path.exists(path):
            paths[condition] = path
    return paths


def main():
    parser = argparse.ArgumentParser(
        description="閾値を振って選択性 (発話を残し非発話を削れているか) を測る")
    parser.add_argument("--materials", default="")
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
    report = {}
    for material in definition.get("materials", []):
        material_id = material["id"]
        if wanted and material_id not in wanted:
            continue
        if material.get("metrics") == "time_only":
            continue
        paths = audio_paths(material_id)
        if "c0_baseline" not in paths:
            continue
        duration = common.probe_duration(paths["c0_baseline"], settings)
        try:
            speech, word_count, seg_mask = speech_mask(material_id, duration)
        except Exception as error:  # noqa: BLE001 (語時刻が無い素材は飛ばす)
            print(f"  {material_id}: {error}", file=sys.stderr)
            continue
        entry = {
            "kind": material.get("kind", ""),
            "label": material.get("label", ""),
            "duration_sec": round(duration, 1),
            "words": word_count,
            "speech_sec_words": round(float(speech.sum()) * FRAME_SEC, 1),
            "speech_ratio_words": round(float(speech.mean()), 4),
            "speech_ratio_segments": round(float(seg_mask.mean()), 4),
            "conditions": {},
        }
        for condition, path in paths.items():
            entry["conditions"][condition] = sweep_audio(
                path, settings, duration, speech, thresholds, min_silence)
            best = max(entry["conditions"][condition], key=lambda r: r["harmonic"])
            print(f"  {material_id} / {condition}: 最良 {best['threshold_db']}dB "
                  f"→ 発話の残存 {best['speech_retained']*100:.1f}% / "
                  f"非発話の削除 {best['nonspeech_removed']*100:.1f}% / "
                  f"調和平均 {best['harmonic']*100:.1f}%", file=sys.stderr)
        report[material_id] = entry

    if not report:
        print("対象がありません (先に run_conditions.py で C0 の文字起こしを)",
              file=sys.stderr)
        return 1
    out = os.path.join(common.WORK_ROOT, "sweep.json")
    common.save_json(out, report)
    _write_markdown(report, os.path.join(common.WORK_ROOT, "sweep.md"))
    print(f"\n書きました: {out} と sweep.md", file=sys.stderr)
    return 0


# 読みやすい表を書き出す (コンソールの文字コードに左右されないため)
def _write_markdown(report, path):
    lines = ["# 閾値スイープ (代理指標)", ""]
    for material_id, entry in report.items():
        lines.append(f"## {material_id} — {entry['label']} ({entry['kind']})")
        lines.append("")
        lines.append(f"- 尺 {entry['duration_sec']}s / 語 {entry['words']} 個")
        lines.append(f"- 語ベースの発話時間 **{entry['speech_sec_words']}s "
                     f"({entry['speech_ratio_words']*100:.1f}%)**  "
                     f"(区間ベースなら {entry['speech_ratio_segments']*100:.1f}% = 過大)")
        lines.append("")
        lines.append("| 条件 | 閾値 | 残存率 | 発話の残存 | 非発話の削除 | 調和平均 |")
        lines.append("|---|---|---|---|---|---|")
        for condition, rows in entry["conditions"].items():
            best = max(rows, key=lambda r: r["harmonic"])
            for row in rows:
                mark = " **←最良**" if row is best else ""
                lines.append(
                    f"| {condition} | {row['threshold_db']} | {row['kept_ratio']:.3f} "
                    f"| {row['speech_retained']*100:.1f}% "
                    f"| {row['nonspeech_removed']*100:.1f}% "
                    f"| {row['harmonic']*100:.1f}%{mark} |")
        lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


if __name__ == "__main__":
    raise SystemExit(main())
