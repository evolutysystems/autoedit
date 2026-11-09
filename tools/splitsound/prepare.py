# Split Sound 実験: 素材の調査と切り出し (docs/request/ver10/resolve_ss1.md §3.2 / §3.5-1)
#
# やること
#   --survey : 手元の素材候補を 1 本ずつ測り、S1 (BGM 大) / S2 (ゲーム内ボイス多) /
#              S3 (静かな雑談) を選ぶ手掛かりを表で出す。音は聞かずに選べる指標だけを使う。
#   --cut    : materials.json の定義どおりに 10 分窓を切り出し、正規化して共通入力を作る。
#
# 共通入力の作り方 (§3.2)
#   元素材 → 10 分窓を切り出す → loudness_normalizer.normalize_file → これを全条件の入力にする。
#   C0 と C1〜C4 の違いが「分離だけ」に限定され、条件の比較が公平になる。
#
# 【素材について / 2026-11-09 の実測】
#   手元 (output/*.archive.media/m*_audio.m4a) にあるのは、アーカイブ切り抜きが
#   素材復旧用に残した **media_role="normalized" の音声** (= 正規化済み・無音カット前) である。
#   映像は残っていないが、本実験が測る指標 (無音検出・音量解析・文字起こし) は
#   すべて音声だけで決まるため、音声のみで実験が成立する (§3.4)。
#   レンダリングは行わない = 映像は不要。
import argparse
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402 (sys.path を通した後に読む)


# 候補素材を探す (output/*.archive.media/m*_audio.m4a と input/ の動画)
def find_candidates():
    found = []
    media_root = os.path.join(common.REPO_ROOT, "output")
    if os.path.isdir(media_root):
        for name in sorted(os.listdir(media_root)):
            folder = os.path.join(media_root, name)
            if not (name.endswith(".archive.media") and os.path.isdir(folder)):
                continue
            vod = name.replace(".archive.media", "")
            for file_name in sorted(os.listdir(folder)):
                if file_name.endswith("_audio.m4a"):
                    found.append({
                        "id": f"{vod}_{file_name.replace('_audio.m4a', '')}",
                        "path": os.path.join(folder, file_name),
                        "kind": "retained_normalized_audio",
                    })
    input_root = os.path.join(common.REPO_ROOT, "input")
    if os.path.isdir(input_root):
        for file_name in sorted(os.listdir(input_root)):
            if file_name.lower().endswith((".mp4", ".mkv", ".ts", ".m4a", ".wav")):
                found.append({
                    "id": os.path.splitext(file_name)[0],
                    "path": os.path.join(input_root, file_name),
                    "kind": "input",
                })
    return found


# 1 本の素材を測り、素材選びの手掛かりを返す (§3.2)
#
# 測るもの (いずれも音を聞かずに出せる値):
#   duration     : 総尺
#   lufs         : Integrated ラウドネス (正規化済みかどうかの確認)
#   silence_ratio: 現行のカット閾値で無音と判定される割合
#                  → 小さい = 常に音が鳴っている (ゲーム音が大きい回の疑い)
#   speech_ratio : 緩い基準 (volume_analysis.base_noise_db) での有音割合
#   low_mid_db   : 低域 (〜250Hz) と中域 (250〜4kHz) のエネルギー差
#                  → 大きい = BGM・爆発音などの低域が強い回
#   high_mid_db  : 高域 (4k〜) と中域の差
def survey_one(entry, settings, probe_sec=300.0):
    path = entry["path"]
    duration = common.probe_duration(path, settings)
    va_cfg = settings.get("volume_analysis", {})
    cut_db = va_cfg.get("last_cut_db", -28)
    base_db = va_cfg.get("base_noise_db", -30)
    cut_min_silence = float(va_cfg.get("cut_min_silence_sec", 0.6))
    base_min_silence = float(va_cfg.get("base_min_silence_sec", 0.3))

    common.ensure_repo_path()
    from src.modules import silence_cutter

    silence = silence_cutter.detect_silence(path, cut_db, cut_min_silence,
                                            settings.get("ffmpeg", {}))
    silent_total = sum(max(float(e) - float(s), 0.0) for s, e in silence)
    base_silence = silence_cutter.detect_silence(path, base_db, base_min_silence,
                                                 settings.get("ffmpeg", {}))
    base_regions = silence_cutter.build_keep_segments(base_silence, duration)
    speech_total = sum(max(float(e) - float(s), 0.0) for s, e in base_regions)

    # 帯域のバランスは先頭 probe_sec 秒だけで測る (全長を読む必要が無い)
    window = min(probe_sec, duration)
    samples = common.read_audio(path, settings, 0.0, window,
                               sample_rate=common.OUT_SAMPLE_RATE, channels=1)[0]
    bands = _band_levels(samples, common.OUT_SAMPLE_RATE)

    return {
        "id": entry["id"],
        "path": os.path.relpath(path, common.REPO_ROOT),
        "duration_sec": round(duration, 1),
        "lufs": _measure_lufs(path, settings),
        "silence_ratio": round(silent_total / duration, 3) if duration else None,
        "speech_ratio": round(speech_total / duration, 3) if duration else None,
        "low_mid_db": bands["low_mid_db"],
        "high_mid_db": bands["high_mid_db"],
        "rms_dbfs": round(common.rms_dbfs(samples), 1),
    }


# 低域/中域/高域のエネルギー差を dB で返す (FFT の大まかな集計)
def _band_levels(samples, sample_rate):
    if len(samples) < sample_rate:
        return {"low_mid_db": None, "high_mid_db": None}
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples)))) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1.0 / sample_rate)

    def energy(low, high):
        mask = (freqs >= low) & (freqs < high)
        return float(spectrum[mask].sum()) + 1e-20

    mid = energy(250.0, 4000.0)
    return {
        "low_mid_db": round(10.0 * float(np.log10(energy(20.0, 250.0) / mid)), 1),
        "high_mid_db": round(10.0 * float(np.log10(energy(4000.0, 16000.0) / mid)), 1),
    }


# Integrated ラウドネス (LUFS) を測る。測れなければ None
#
# 既存の measure_loudness (loudnorm 1 パス測定) をそのまま使う。
# measure_scope="selected" の区間測定へ入らないよう、ここでは全長測定の設定で呼ぶ。
def _measure_lufs(path, settings):
    common.ensure_repo_path()
    from src.modules import loudness_normalizer

    cfg = dict(loudness_normalizer.loudness_config(settings))
    cfg["measure_scope"] = "vod"            # 全長を測る (素材の素の値が知りたい)
    measured = loudness_normalizer.measure_loudness(
        path, cfg, settings.get("ffmpeg", {}))
    if not measured:
        return None
    value = measured.get("input_i")
    return round(float(value), 1) if value is not None else None


# materials.json の 1 件を切り出し、正規化して共通入力を作る (§3.5-1)
#
# 戻り値: 正規化後のパス。正規化がスキップされた場合は切り出しそのままのパス。
def cut_and_normalize(material, settings):
    source = material["source"]
    if not os.path.isabs(source):
        source = os.path.join(common.REPO_ROOT, source)
    folder = common.material_dir(material["id"])
    raw_path = os.path.join(folder, "source_cut.m4a")
    normalized_path = os.path.join(folder, "normalized.m4a")

    start = float(material.get("start_sec", 0.0))
    length = float(material.get("duration_sec", 600.0))

    # 切り出しは再エンコードする (コピーだと先頭に無音や欠けが入り、時刻が 1 対 1 にならない)
    cmd = [
        common.ffmpeg_exe(settings), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-i", source,
        "-t", f"{length:.3f}",
        "-vn",
        "-c:a", settings.get("ffmpeg", {}).get("audio_codec", "aac"),
        "-b:a", "192k",
        "-ar", str(settings.get("ffmpeg", {}).get("audio_sample_rate", 48000)),
        "-ac", "2",
        raw_path,
    ]
    subprocess.run(cmd, check=True)

    common.ensure_repo_path()
    from src.modules import loudness_normalizer

    details = {}
    result = loudness_normalizer.normalize_file(
        raw_path, normalized_path, settings, details=details)

    # 全条件の共通入力を **PCM WAV** で確定させる (2026-11-09 の実測 / §4.2-1)
    #
    # なぜ WAV を挟むか:
    #   実測で、手元の m4a (AAC) は 325 秒付近にタイムスタンプの不連続を持っており、
    #   「-ss で頭から捨てて読む (タイムスタンプ基準)」と「先頭から流し読みする
    #   (サンプル数基準)」で**同じ時刻の内容が 13 サンプルずれた**。
    #   分離は流し読みで作るため、比較や後段が時刻基準で読むとずれを拾ってしまう。
    #   WAV には編集リストや欠落が無く、どちらの読み方でも同じ位置になるため、
    #   ここで 1 回だけ WAV へ確定させ、以降 (C0 も含む全条件) はこの WAV だけを使う。
    wav_path = os.path.join(folder, "analysis_source.wav")
    subprocess.run([
        common.ffmpeg_exe(settings), "-y", "-hide_banner", "-loglevel", "error",
        "-i", result,
        "-vn",
        "-c:a", "pcm_s16le",
        "-ar", str(common.OUT_SAMPLE_RATE),
        "-ac", "2",
        wav_path,
    ], check=True)
    details["analysis_source_wav"] = wav_path
    return result, details


# 素材の定義を読む (無ければ空)
def load_materials():
    path = common.materials_path()
    if not os.path.exists(path):
        return {"materials": []}
    return common.load_json(path)


def main():
    parser = argparse.ArgumentParser(
        description="Split Sound 実験の素材を調査・切り出しする")
    parser.add_argument("--survey", action="store_true",
                        help="素材候補を測って一覧を出す (選定用)")
    parser.add_argument("--cut", action="store_true",
                        help="materials.json のとおりに切り出して正規化する")
    parser.add_argument("--only", default="",
                        help="--cut で対象にする素材 ID (カンマ区切り。既定は全部)")
    parser.add_argument("--min-duration", type=float, default=600.0,
                        help="--survey で一覧に出す最短の尺 (秒)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    common.setup_logging(args.verbose)
    settings = common.load_app_settings()

    if args.survey:
        rows = []
        for entry in find_candidates():
            try:
                duration = common.probe_duration(entry["path"], settings)
            except Exception as error:  # noqa: BLE001 (読めない候補は飛ばす)
                print(f"  (skip) {entry['id']}: {error}", file=sys.stderr)
                continue
            if duration < args.min_duration:
                continue
            rows.append(survey_one(entry, settings))
        rows.sort(key=lambda r: -(r["duration_sec"] or 0))
        print("\n素材候補 (無音割合が小さい = 常に音が鳴っている回)\n")
        header = ("ID", "尺(s)", "LUFS", "無音率", "有音率", "低-中dB", "高-中dB", "RMS")
        print("| {} | {} | {} | {} | {} | {} | {} | {} |".format(*header))
        print("|---|---|---|---|---|---|---|---|")
        for row in rows:
            print("| {id} | {duration_sec} | {lufs} | {silence_ratio} | {speech_ratio} "
                  "| {low_mid_db} | {high_mid_db} | {rms_dbfs} |".format(**row))
        out = os.path.join(common.WORK_ROOT, "survey.json")
        common.save_json(out, rows)
        print(f"\n詳細を書きました: {out}")
        return 0

    if args.cut:
        definition = load_materials()
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        done = []
        for material in definition.get("materials", []):
            if wanted and material["id"] not in wanted:
                continue
            path, details = cut_and_normalize(material, settings)
            done.append({"id": material["id"], "normalized": path, "loudness": details})
            print(f"  {material['id']} → {path}")
        common.save_json(os.path.join(common.WORK_ROOT, "prepared.json"), done)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
