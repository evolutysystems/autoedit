# Split Sound 実験: 条件ごとに閾値決定 → 無音検出 → 文字起こしを回す
# (docs/request/ver10/resolve_ss1.md §3.5-4〜6 / §4.3)
#
# 使う関数はすべて src/ の既存実装である (自分で書き直さない / §4.3)。
#   閾値   : volume_analyzer.analyze_min_speech_db
#   無音   : silence_cutter.detect_silence → build_keep_segments
#   認識   : subtitle_generator.resolve_text_source().extract()
# これにより C0 (ベースライン) は本体の実行と同じ結果になる = ハーネスが正しい証拠になる。
#
# 閾値は 2 通り測る (§3.1)
#   T1 : その条件の音声で測り直した値 (分離でレベルが変わるため)
#   T2 : C0 で決まった値をそのまま流用
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402

# 条件名 → 解析用音声のファイル名 (C0 は素材直下の共通入力そのまま)
_CONDITION_FILES = {
    "c0_baseline": None,
    "c1_htdemucs": "voice.wav",
    "c2_df3": "voice.wav",
    "c3_gate": "voice.wav",
    "c4_df3_demucs": "voice.wav",
}


# 条件の解析用音声のパスを返す (無ければ None)
def analysis_path(material_id, condition):
    if condition == "c0_baseline":
        path = os.path.join(common.material_dir(material_id), "analysis_source.wav")
        return path if os.path.exists(path) else None
    name = _CONDITION_FILES.get(condition)
    if name is None:
        return None
    path = os.path.join(common.condition_dir(material_id, condition), name)
    return path if os.path.exists(path) else None


# 発話区間の代表 dB を測る (本体と同じ関数)。測れなければ None
def measure_threshold(path, settings):
    common.ensure_repo_path()
    from src.modules import volume_analyzer

    watch = common.Stopwatch()
    analysis = volume_analyzer.analyze_min_speech_db(path, settings)
    analysis["elapsed_sec"] = round(watch.elapsed(), 1)
    return analysis


# 指定した閾値で「残す区間」を求める (本体と同じ関数)
def detect_keep_segments(path, threshold_db, settings, duration):
    common.ensure_repo_path()
    from src.modules import silence_cutter

    va_cfg = settings.get("volume_analysis", {})
    min_silence = float(va_cfg.get("cut_min_silence_sec", 0.6))
    watch = common.Stopwatch()
    silence = silence_cutter.detect_silence(
        path, threshold_db, min_silence, settings.get("ffmpeg", {}))
    keep = silence_cutter.build_keep_segments(silence, duration)
    kept = sum(float(e) - float(s) for s, e in keep)
    return {
        "threshold_db": threshold_db,
        "min_silence_sec": min_silence,
        "silence_count": len(silence),
        "keep_count": len(keep),
        "kept_sec": round(kept, 2),
        "kept_ratio": round(kept / duration, 4) if duration else None,
        "keep_segments": [[round(float(s), 3), round(float(e), 3)] for s, e in keep],
        "elapsed_sec": round(watch.elapsed(), 1),
    }


# 文字起こしを行う (本体と同じエンジン・同じ設定)
#
# テロップの整形 (フィラー除去・置換・改行・表示タイミング) は**掛けない**。
# 設定差で CER が動くのを避けるため、認識の素の結果だけを残す (§3.4 M3)。
def transcribe(path, settings):
    common.ensure_repo_path()
    from src.modules import subtitle_generator

    subtitle_cfg = settings.get("subtitle", {})
    source = subtitle_generator.resolve_text_source(settings, subtitle_cfg)
    if source is None:
        return {"items": [], "error": "音声認識エンジンが無効です"}
    watch = common.Stopwatch()
    items = source.extract(path, subtitle_cfg.get("language", "ja"))
    # 語単位の時刻も残す (subtitle.word_timestamps=True のとき faster-whisper が返す)。
    #
    # 【なぜ要るか / 2026-11-09】
    # 認識の「区間」は VAD の都合で実際の発話より大幅に長くなることがある
    # (実測: 10 文字の一言に 14〜25 秒の区間が付く)。区間をそのまま「発話時間」と
    # みなすと、無音カットの評価が実態と合わない。語の時刻があれば
    # 「本当に声が出ている時間」を union で作れる。
    rows = []
    for item in items:
        row = {
            "start": round(float(item.get("start", 0.0)), 3),
            "end": round(float(item.get("end", 0.0)), 3),
            "text": str(item.get("text", "")),
        }
        words = item.get("words") or []
        spans = [[round(float(w["start"]), 3), round(float(w["end"]), 3)]
                 for w in words
                 if w.get("start") is not None and w.get("end") is not None]
        if spans:
            row["word_spans"] = spans
        rows.append(row)
    return {
        "items": rows,
        "count": len(rows),
        "elapsed_sec": round(watch.elapsed(), 1),
        "model": subtitle_cfg.get("whisper_model"),
        "compute_type": subtitle_cfg.get("whisper_compute_type"),
        "beam_size": subtitle_cfg.get("whisper_beam_size"),
    }


# 1 素材 × 1 条件を回して result.json を書く
def run_one(material, condition, settings, args, baseline_db=None):
    material_id = material["id"]
    path = analysis_path(material_id, condition)
    if path is None:
        raise RuntimeError(f"解析用音声がありません ({condition})")

    duration = common.probe_duration(path, settings)
    result = {
        "material": material_id,
        "condition": condition,
        "analysis_audio": os.path.relpath(path, common.REPO_ROOT),
        "duration_sec": round(duration, 3),
        "thresholds": {},
        "silence": {},
    }

    # 分離の所要は separate.json から引き継ぐ (工程別の比較に使う / §3.4 M7)
    separate_json = os.path.join(common.condition_dir(material_id, condition),
                                 "separate.json")
    if os.path.exists(separate_json):
        stats = common.load_json(separate_json)
        result["separation"] = {
            "engine": stats.get("engine"),
            "elapsed_sec": stats.get("elapsed_sec"),
            "rtf": stats.get("rtf"),
            "verified": stats.get("verified"),
            "output_rms_dbfs": stats.get("output_rms_dbfs"),
        }

    # T1: その条件の音声で測り直す
    analysis = measure_threshold(path, settings)
    t1 = analysis.get("min_db")
    result["thresholds"]["T1"] = {
        "db": t1,
        "region_count": analysis.get("region_count"),
        "measured_count": analysis.get("measured_count"),
        "elapsed_sec": analysis.get("elapsed_sec"),
    }
    # T2: C0 で決まった値を流用する
    # C0 自身は「流用元」なので T1 と同じ値になる (表で空欄にしないため明示的に入れる)
    if condition == "c0_baseline" and baseline_db is None:
        result["thresholds"]["T2"] = {"db": t1, "source": "self (baseline)"}
    else:
        result["thresholds"]["T2"] = {"db": baseline_db, "source": "c0_baseline T1"}

    for name in ("T1", "T2"):
        threshold = result["thresholds"][name]["db"]
        if threshold is None:
            result["silence"][name] = {"error": "閾値が決まりませんでした"}
            continue
        result["silence"][name] = detect_keep_segments(
            path, threshold, settings, duration)

    if not args.no_asr and material.get("metrics") != "time_only":
        result["asr"] = transcribe(path, settings)

    out = os.path.join(common.condition_dir(material_id, condition), "result.json")
    common.save_json(out, result)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="条件ごとに閾値・無音検出・文字起こしを回す")
    parser.add_argument("--materials", default="")
    parser.add_argument("--conditions", default="c0_baseline,c1_htdemucs")
    parser.add_argument("--no-asr", action="store_true",
                        help="文字起こしを飛ばす (無音カットだけ見るとき)")
    parser.add_argument("--include-time-only", action="store_true",
                        help="metrics=time_only の素材 (S4) も対象にする")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    common.setup_logging(args.verbose)
    settings = common.load_app_settings()
    definition = common.load_json(common.materials_path())
    wanted = {x.strip() for x in args.materials.split(",") if x.strip()}
    conditions = [x.strip() for x in args.conditions.split(",") if x.strip()]
    # C0 を先に回して T2 の基準値を作る
    if "c0_baseline" in conditions:
        conditions = ["c0_baseline"] + [c for c in conditions if c != "c0_baseline"]

    failures = []
    for material in definition.get("materials", []):
        if wanted and material["id"] not in wanted:
            continue
        if material.get("metrics") == "time_only" and not args.include_time_only:
            continue
        baseline_db = None
        for condition in conditions:
            print(f"  {material['id']} / {condition} …", file=sys.stderr)
            try:
                result = run_one(material, condition, settings, args, baseline_db)
            except Exception as error:  # noqa: BLE001 (1 条件の失敗で全体を止めない / §6)
                print(f"  {material['id']} / {condition}: 失敗 {error}", file=sys.stderr)
                failures.append((f"{material['id']}/{condition}", str(error)))
                continue
            if condition == "c0_baseline":
                baseline_db = result["thresholds"]["T1"]["db"]
            t1 = result["thresholds"]["T1"]["db"]
            silence = result["silence"].get("T1", {})
            asr = result.get("asr", {})
            print(f"  {material['id']} / {condition}: 閾値 {t1} dB / "
                  f"残す区間 {silence.get('keep_count')} 件 "
                  f"({silence.get('kept_ratio')}) / テロップ {asr.get('count', '-')} 件",
                  file=sys.stderr)

    if failures:
        print("\n失敗した組み合わせ:", file=sys.stderr)
        for name, reason in failures:
            print(f"  - {name}: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
