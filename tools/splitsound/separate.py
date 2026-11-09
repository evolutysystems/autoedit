# Split Sound 実験: 解析用音声 (話者の声だけ) を作る (docs/request/ver10/resolve_ss1.md §4.2)
#
# 条件 (§3.1)
#   c1_htdemucs : Demucs htdemucs の vocals stem
#   c2_df3      : DeepFilterNet DF3 の強調音 (prebuilt の deep-filter CLI を使う)
#   c3_gate     : c1 の vocals に、元音声とのレベル差でゲートを掛ける
#   c4_df3_demucs : DF3 → htdemucs の順で通す
#
# 守ること (§4.2-2 時間軸の保全)
#   分離音の時刻はそのまま keep_segments = 実際の映像のカット点になる。
#   1 サンプルのずれが映像のずれになるため、
#     ・atrim / apad / asetpts を使わない (変換はリサンプルとチャンネル変換だけ)
#     ・採用範囲は秒ではなく**出力サンプルの添字**で決める (common.window_plan)
#     ・書き終わりに総サンプル数を入力と突き合わせ、合わなければ補正してログへ出す
#   を全条件で共通に行う (_separate_windowed)。
import argparse
import os
import shutil
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402

# htdemucs の学習条件 (44.1kHz ステレオ)
_DEMUCS_RATE = 44100
_DEMUCS_CHANNELS = 2

# DF3 の前提 (48kHz モノラル)
_DF_RATE = 48000

# 分離結果の受け入れ検査 (§4.2-4)
_MIN_RMS_DBFS = -60.0


# ------------------------------------------------------------------
# エンジン
# ------------------------------------------------------------------

# htdemucs を 1 回だけ読み込んで使い回す
class DemucsEngine:

    name = "htdemucs"
    sample_rate = _DEMUCS_RATE
    channels = _DEMUCS_CHANNELS

    def __init__(self, threads=0, model_name="htdemucs", stem="vocals"):
        import torch
        from demucs.apply import apply_model
        from demucs.pretrained import get_model

        if threads and threads > 0:
            torch.set_num_threads(int(threads))
        else:
            # 既定任せにすると計測が安定しないため CPU 数で明示する (§4.2-5)
            torch.set_num_threads(os.cpu_count() or 1)
        self._torch = torch
        self._apply_model = apply_model
        self._model = get_model(model_name)
        self._model.cpu()
        self._model.eval()
        self._stem = stem
        if stem not in self._model.sources:
            raise RuntimeError(
                f"{model_name} に stem '{stem}' がありません: {self._model.sources}")
        self._stem_index = list(self._model.sources).index(stem)

    # (2, N) の 44.1kHz 波形 → vocals の (2, N) を返す
    def process(self, samples):
        torch = self._torch
        tensor = torch.from_numpy(np.ascontiguousarray(samples, dtype=np.float32))
        with torch.no_grad():
            # apply_model は (バッチ, ch, サンプル) を期待する
            separated = self._apply_model(
                self._model, tensor.unsqueeze(0), device="cpu",
                split=True, overlap=0.25, progress=False)
        return separated[0, self._stem_index].numpy()


# DeepFilterNet (DF3) を prebuilt の deep-filter CLI 経由で使う
#
# 【なぜ CLI か / 2026-11-09 の確認】
#   pip install deepfilternet は依存の deepfilterlib が Rust ツールチェーン (cargo) を
#   要求し、Python 3.12 / Windows には wheel が無いため**ビルドできない**。
#   DeepFilterNet は公式に単体実行ファイル (deep-filter) を配布しているため、
#   ffmpeg と同じ「外部 exe を呼ぶ」形にする。パスは --df-bin か環境変数
#   SPLITSOUND_DF_BIN で渡す (見つからなければこの条件は測らない / §6)。
class DeepFilterEngine:

    name = "df3"
    sample_rate = _DF_RATE
    channels = 1

    def __init__(self, binary=None, work_dir=None):
        self._binary = (binary or os.environ.get("SPLITSOUND_DF_BIN")
                        or shutil.which("deep-filter"))
        if not self._binary or not os.path.exists(self._binary):
            raise RuntimeError(
                "deep-filter の実行ファイルが見つかりません。"
                "--df-bin <path> か環境変数 SPLITSOUND_DF_BIN で指定してください "
                "(DeepFilterNet の Releases にある prebuilt を使う)")
        self._work_dir = work_dir or common.WORK_ROOT
        os.makedirs(self._work_dir, exist_ok=True)

    # (1, N) の 48kHz 波形 → 強調後の (1, N) を返す
    def process(self, samples):
        folder = os.path.join(self._work_dir, "_df_tmp")
        os.makedirs(folder, exist_ok=True)
        in_path = os.path.join(folder, "in.wav")
        out_dir = os.path.join(folder, "out")
        os.makedirs(out_dir, exist_ok=True)
        common.write_wav(in_path, samples, _DF_RATE)
        cmd = [self._binary, "-o", out_dir, in_path]
        result = subprocess.run(cmd, capture_output=True, check=False)
        produced = os.path.join(out_dir, "in.wav")
        if result.returncode != 0 or not os.path.exists(produced):
            raise RuntimeError(
                "deep-filter の実行に失敗しました:\n"
                + result.stderr.decode("utf-8", "replace")[-2000:])
        import wave

        with wave.open(produced, "rb") as handle:
            raw = handle.readframes(handle.getnframes())
            rate = handle.getframerate()
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if rate != _DF_RATE:
            values = common.resample(values, rate, _DF_RATE)
        return values.reshape(1, -1)


# ------------------------------------------------------------------
# 窓ごとの分離と連結 (全条件で共通 / §4.2-2・§4.2-3)
# ------------------------------------------------------------------

# エンジンを窓ごとに適用し、48kHz モノラル WAV へ連結する
#
# 戻り値: 検査結果の辞書 (長さの差・RMS・所要・窓数)
def _separate_windowed(source_path, output_path, engine, settings,
                       chunk_sec, overlap_sec, on_window=None):
    duration = common.probe_duration(source_path, settings)
    plan = common.window_plan(duration, chunk_sec, overlap_sec, common.OUT_SAMPLE_RATE)
    expected = int(round(duration * common.OUT_SAMPLE_RATE))
    watch = common.Stopwatch()
    corrections = []

    with common.WavWriter(output_path, common.OUT_SAMPLE_RATE) as writer:
        # シークせずに順番に読む (AAC のフレーム境界丸めを踏まないため / common.iter_windows)
        windows = common.iter_windows(source_path, settings, plan,
                                      engine.sample_rate, engine.channels)
        for index, (window, block, abs_start) in enumerate(windows):
            processed = np.asarray(engine.process(block), dtype=np.float32)
            mono_model = processed.mean(axis=0) if processed.ndim > 1 else processed
            # 48kHz へ寄せてから採用範囲を切る (丸めを 1 回で終わらせる)
            mono = common.resample(mono_model, engine.sample_rate, common.OUT_SAMPLE_RATE)
            # 窓の先頭が 48kHz 基準で何サンプル目かは、実際に読んだ絶対添字から求める。
            # 秒を経由しないので、デコード位置と採用範囲が必ず一致する (§4.2-2)。
            window_start_out = int(round(abs_start * common.OUT_SAMPLE_RATE
                                         / engine.sample_rate))
            head = window["out_start"] - window_start_out
            want = window["out_end"] - window["out_start"]
            if head < 0:
                corrections.append(abs(head))
                head = 0
            piece = mono[head:head + want]
            if len(piece) < want:
                # 端数は 0 埋めで合わせる (§4.2-2)。補正量は必ず記録する
                corrections.append(want - len(piece))
                piece = np.concatenate([piece, np.zeros(want - len(piece), dtype=np.float32)])
            writer.append(piece)
            if on_window is not None:
                on_window(index + 1, len(plan))
        written = writer.written
    diff = written - expected
    stats = {
        "engine": engine.name,
        "duration_sec": round(duration, 3),
        "windows": len(plan),
        "chunk_sec": chunk_sec,
        "overlap_sec": overlap_sec,
        "expected_samples": expected,
        "written_samples": written,
        "sample_diff": diff,
        "pad_corrections": corrections,
        "elapsed_sec": round(watch.elapsed(), 1),
        "rtf": round(watch.elapsed() / duration, 3) if duration else None,
    }
    return stats


# 作った直後の自己検査 (§4.2-4)。失敗なら理由を返す (None なら合格)
def verify_output(source_path, output_path, settings, stats,
                  tolerance_ms=10, check_offset=True):
    tolerance = int(round(common.OUT_SAMPLE_RATE * tolerance_ms / 1000.0))
    if abs(stats["sample_diff"]) > 0:
        return f"総サンプル数が一致しません (差 {stats['sample_diff']})"
    if stats["pad_corrections"] and max(stats["pad_corrections"]) > tolerance:
        return (f"窓の端で {max(stats['pad_corrections'])} サンプルの補正が必要でした "
                f"(許容 {tolerance})")

    samples = common.read_audio(output_path, settings, 0.0, None,
                               sample_rate=common.OUT_SAMPLE_RATE, channels=1)[0]
    level = common.rms_dbfs(samples)
    stats["output_rms_dbfs"] = round(level, 1)
    if level < _MIN_RMS_DBFS:
        return f"出力がほぼ無音です (RMS {level:.1f} dBFS)"

    if check_offset:
        offsets = []
        duration = stats["duration_sec"]
        for position in (0.0, max(duration / 2 - 15, 0.0), max(duration - 30, 0.0)):
            window = min(30.0, duration - position)
            if window < 5.0:
                continue
            offsets.append(_lag_samples(source_path, output_path, settings,
                                        position, window))
        stats["offset_samples"] = offsets
        if any(abs(value) > 1 for value in offsets):
            return f"位置ずれが出ています (lag={offsets} サンプル)"
    return None


# 元音声と分離音の相互相関のピーク位置 (サンプル) を返す
def _lag_samples(source_path, output_path, settings, start, length, max_lag=400):
    left = common.read_audio(source_path, settings, start, length,
                             sample_rate=common.OUT_SAMPLE_RATE, channels=1)[0]
    right = common.read_audio(output_path, settings, start, length,
                              sample_rate=common.OUT_SAMPLE_RATE, channels=1)[0]
    size = min(len(left), len(right))
    if size < common.OUT_SAMPLE_RATE:
        return 0
    left = left[:size] - float(np.mean(left[:size]))
    right = right[:size] - float(np.mean(right[:size]))
    best_lag, best_score = 0, -1e30
    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            score = float(np.dot(left[-lag:], right[:size + lag]))
        else:
            score = float(np.dot(left[:size - lag], right[lag:]))
        if score > best_score:
            best_score, best_lag = score, lag
    return best_lag


# ------------------------------------------------------------------
# 条件ごとの入口
# ------------------------------------------------------------------

# 分離音に絶対閾値のノイズゲートを掛ける (C3 / §4.2-7)
#
# 【なぜ要るか / 2026-11-09 の実測】
#   分離した声は**平均レベルは下がるが、残渣が常に鳴っている**。
#   ffmpeg の silencedetect は「指定区間のあいだ 1 度も閾値を超えない」ことを求めるため、
#   残渣のピークで連続性が切れ、**-40dB まで下げても無音を 1 件も見つけられなかった**
#   (100ms RMS では 52% のフレームが -20dB 以下なのに、である)。
#   そこで「静かなフレームを本当の無音 (ゼロ) にする」ゲートを挟む。
#   これで silencedetect が従来どおりの判定で無音を拾えるようになる。
#
# 判定 (ヒステリシス + ホールド):
#   ・frame_ms ごとに RMS を測る
#   ・open_db を超えたら開く / close_db (= open_db - hysteresis_db) を下回ったら閉じる
#   ・閉じるのは hold_ms ぶん待ってから (語尾や息継ぎで途切れさせない)
#   ・閉じた区間は 0 にする (減衰ではなく無音にする。silencedetect が確実に拾う)
# open_db を省略すると、**その分離音のレベル分布の分位点**から決める (素材差を吸収する)。
# 絶対値で固定すると、素材が変わるたびに「全部無音」か「全く無音なし」へ振れる。
def apply_gate(voice_path, source_path, output_path, settings,
               open_db=None, percentile=60.0, hysteresis_db=6.0, frame_ms=20.0,
               hold_ms=250.0, attack_ms=40.0):
    voice = common.read_audio(voice_path, settings, 0.0, None,
                              sample_rate=common.OUT_SAMPLE_RATE, channels=1)[0]
    size = len(voice)
    step = max(int(common.OUT_SAMPLE_RATE * frame_ms / 1000.0), 1)
    frames = size // step
    hold_frames = max(int(round(hold_ms / frame_ms)), 0)
    attack_frames = max(int(round(attack_ms / frame_ms)), 0)

    block = voice[:frames * step].reshape(frames, step).astype(np.float64)
    levels = 20.0 * np.log10(np.sqrt((block * block).mean(axis=1)) + 1e-12)
    if open_db is None:
        open_db = float(np.percentile(levels, percentile))
    close_db = open_db - abs(hysteresis_db)

    # 開閉を決める (ホールド付き)
    open_mask = np.zeros(frames, dtype=bool)
    is_open = False
    remaining = 0
    for index, level in enumerate(levels):
        if level >= open_db:
            is_open, remaining = True, hold_frames
        elif is_open and level < close_db:
            if remaining > 0:
                remaining -= 1
            else:
                is_open = False
        open_mask[index] = is_open

    # 立ち上がりの手前も開ける (語頭を削らない)
    if attack_frames:
        padded = open_mask.copy()
        for index in np.flatnonzero(open_mask):
            padded[max(index - attack_frames, 0):index] = True
        open_mask = padded

    output = np.zeros(size, dtype=np.float32)
    gate = np.repeat(open_mask, step)
    output[:len(gate)] = voice[:len(gate)] * gate
    if len(gate) < size:
        output[len(gate):] = voice[len(gate):]     # 端数フレームはそのまま残す
    common.write_wav(output_path, output, common.OUT_SAMPLE_RATE)
    return {"path": output_path, "open_db": round(float(open_db), 1),
            "close_db": round(float(close_db), 1), "hold_ms": hold_ms,
            "open_ratio": round(float(open_mask.mean()), 4)}


# 1 素材 × 1 条件の分離を行い、検査結果を返す
def run_condition(material_id, source_path, condition, settings, args):
    folder = common.condition_dir(material_id, condition)
    output_path = os.path.join(folder, "voice.wav")

    def progress(done, total):
        print(f"    {condition}: 窓 {done}/{total}", file=sys.stderr)

    if condition == "c1_htdemucs":
        engine = DemucsEngine(threads=args.threads)
        stats = _separate_windowed(source_path, output_path, engine, settings,
                                   args.chunk_sec, args.overlap_sec, progress)
    elif condition == "c2_df3":
        engine = DeepFilterEngine(binary=args.df_bin, work_dir=folder)
        stats = _separate_windowed(source_path, output_path, engine, settings,
                                   args.chunk_sec, args.overlap_sec, progress)
    elif condition == "c3_gate":
        base = os.path.join(common.condition_dir(material_id, "c1_htdemucs"), "voice.wav")
        if not os.path.exists(base):
            raise RuntimeError("c3_gate は c1_htdemucs の結果が必要です (先に c1 を回す)")
        watch = common.Stopwatch()
        gate_info = apply_gate(base, source_path, output_path, settings,
                               open_db=args.gate_open_db,
                               percentile=args.gate_percentile,
                               hold_ms=args.gate_hold_ms)
        duration = common.probe_duration(source_path, settings)
        stats = {
            "engine": "htdemucs+gate", "duration_sec": round(duration, 3),
            "windows": 0, "chunk_sec": args.chunk_sec, "overlap_sec": args.overlap_sec,
            "expected_samples": int(round(duration * common.OUT_SAMPLE_RATE)),
            "written_samples": int(round(duration * common.OUT_SAMPLE_RATE)),
            "sample_diff": 0, "pad_corrections": [],
            "elapsed_sec": round(watch.elapsed(), 1),
            "rtf": round(watch.elapsed() / duration, 3) if duration else None,
            "gate": gate_info,
        }
    elif condition == "c4_df3_demucs":
        stage_dir = common.condition_dir(material_id, "c4_df3_demucs")
        stage_path = os.path.join(stage_dir, "stage1_df3.wav")
        df_engine = DeepFilterEngine(binary=args.df_bin, work_dir=stage_dir)
        stage_stats = _separate_windowed(source_path, stage_path, df_engine, settings,
                                         args.chunk_sec, args.overlap_sec, progress)
        reason = verify_output(source_path, stage_path, settings, stage_stats,
                               tolerance_ms=args.length_tolerance_ms, check_offset=False)
        if reason:
            raise RuntimeError(f"C4 の 1 段目 (DF3) が検査に通りませんでした: {reason}")
        engine = DemucsEngine(threads=args.threads)
        stats = _separate_windowed(stage_path, output_path, engine, settings,
                                   args.chunk_sec, args.overlap_sec, progress)
        stats["stage1"] = stage_stats
    else:
        raise RuntimeError(f"未知の条件です: {condition}")

    reason = verify_output(source_path, output_path, settings, stats,
                           tolerance_ms=args.length_tolerance_ms)
    stats["verified"] = reason is None
    stats["verify_error"] = reason
    common.save_json(os.path.join(folder, "separate.json"), stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description="解析用音声 (声だけ) を作る")
    parser.add_argument("--materials", default="",
                        help="対象素材 ID (カンマ区切り。既定は materials.json の全部)")
    parser.add_argument("--conditions", default="c1_htdemucs",
                        help="条件 (カンマ区切り): c1_htdemucs,c2_df3,c3_gate,c4_df3_demucs")
    parser.add_argument("--chunk-sec", type=float, default=300.0, dest="chunk_sec")
    parser.add_argument("--overlap-sec", type=float, default=5.0, dest="overlap_sec")
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--df-bin", default="", dest="df_bin",
                        help="deep-filter の実行ファイル (C2/C4 で使う)")
    parser.add_argument("--gate-open-db", type=float, default=None, dest="gate_open_db",
                        help="ゲートの開放閾値 (dBFS)。省略すると分位点から決める")
    parser.add_argument("--gate-percentile", type=float, default=60.0,
                        dest="gate_percentile",
                        help="--gate-open-db 省略時に使うレベル分布の分位点 (%%)")
    parser.add_argument("--gate-hold-ms", type=float, default=250.0, dest="gate_hold_ms")
    parser.add_argument("--length-tolerance-ms", type=float, default=10.0,
                        dest="length_tolerance_ms")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    common.setup_logging(args.verbose)
    settings = common.load_app_settings()
    definition = common.load_json(common.materials_path())
    wanted = {x.strip() for x in args.materials.split(",") if x.strip()}
    conditions = [x.strip() for x in args.conditions.split(",") if x.strip()]

    failures = []
    for material in definition.get("materials", []):
        if wanted and material["id"] not in wanted:
            continue
        # 共通入力は PCM WAV (prepare.py が作る / 時間軸を 1 つに固定するため)
        source_path = os.path.join(
            common.material_dir(material["id"]), "analysis_source.wav")
        if not os.path.exists(source_path):
            print(f"  {material['id']}: 共通入力 (analysis_source.wav) がありません "
                  f"(先に prepare.py --cut を実行する)", file=sys.stderr)
            failures.append((material["id"], "no input"))
            continue
        for condition in conditions:
            print(f"  {material['id']} / {condition} …", file=sys.stderr)
            try:
                stats = run_condition(material["id"], source_path, condition,
                                      settings, args)
            except Exception as error:  # noqa: BLE001 (1 条件の失敗で全体を止めない / §6)
                print(f"  {material['id']} / {condition}: 失敗 {error}", file=sys.stderr)
                failures.append((f"{material['id']}/{condition}", str(error)))
                continue
            flag = "OK" if stats["verified"] else f"NG ({stats['verify_error']})"
            print(f"  {material['id']} / {condition}: {flag} / "
                  f"所要 {stats['elapsed_sec']}s (RTF {stats['rtf']}) / "
                  f"RMS {stats.get('output_rms_dbfs')} dBFS", file=sys.stderr)

    if failures:
        print("\n失敗した組み合わせ:", file=sys.stderr)
        for name, reason in failures:
            print(f"  - {name}: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
