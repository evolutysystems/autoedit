# Split Sound 実験ハーネス 共通処理 (docs/request/ver10/resolve_ss1.md §4.1)
#
# この配下のスクリプトは「配布物に入らない補助スクリプト」である (tools/ の方針)。
# src/ のモジュールを import して再利用するだけで、src/ 側は 1 行も変更しない。
# 設定は src/settings/setting.json を**読むだけ** (save_settings は呼ばない / §4.1)。
import json
import logging
import os
import subprocess
import sys
import time

import numpy as np

# tools/splitsound/common.py → リポジトリ直下
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 実験の中間物・成果物の置き場 (.gitignore の output/ 配下なのでコミットされない / §10 Q4)
WORK_ROOT = os.path.join(REPO_ROOT, "output", "_splitsound")

# 解析用音声の形式 (§4.2-1)。silencedetect も faster-whisper も WAV を直接読める。
OUT_SAMPLE_RATE = 48000

_logger = logging.getLogger("splitsound")


# src/ を import できるようにする (実験スクリプトの先頭で 1 回呼ぶ)
def ensure_repo_path():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)


# 画面を出さずにアプリの設定を読む (新しいキーの永続化もしない / §4.1)
def load_app_settings():
    ensure_repo_path()
    from src.settings.settings_window import load_settings

    return load_settings(persist_new_keys=False)


# ログを標準エラーへ 1 行ずつ出す (src 側の logger もこの設定に乗る)
def setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # ffmpeg のコマンド全文 (DEBUG) は出力が長すぎるので既定では抑える
    if not verbose:
        logging.getLogger("src.modules.silence_cutter").setLevel(logging.INFO)


# ffmpeg / ffprobe の実体パスを設定から解決する (src の解決規則をそのまま使う)
def ffmpeg_exe(settings):
    ensure_repo_path()
    from src.modules import ffmpeg_runner

    return ffmpeg_runner.get_ffmpeg_exe(settings.get("ffmpeg", {}))


def ffprobe_exe(settings):
    ensure_repo_path()
    from src.modules import ffmpeg_runner

    return ffmpeg_runner.get_ffprobe_exe(settings.get("ffmpeg", {}))


# 総尺 (秒) を返す
def probe_duration(path, settings):
    ensure_repo_path()
    from src.modules import ffmpeg_runner

    return ffmpeg_runner.probe_duration(path, settings.get("ffmpeg", {}))


# ------------------------------------------------------------------
# 音声の読み書き
# ------------------------------------------------------------------

# 音声の一部を float32 の numpy 配列として読む
#
# 全長を一度に読むとメモリが足りない (3 時間 / 44.1kHz / 2ch で約 1.9GB) ため、
# 区間を指定して ffmpeg から PCM をパイプで受け取る。
#
# 【accurate の意味 / 2026-11-09 の実測】
#   -ss を -i の**前**に置く入力シークは速いが、AAC では**フレーム境界へ丸められる**。
#   実測で、同じ時刻を要求しても m4a (AAC) と wav で読み出し位置が 13 サンプルずれた。
#   時刻の比較 (位置ずれ検査) でこれを踏むと、分離の誤りと区別できない。
#   accurate=True は -ss を -i の**後**へ置く (出力シーク = 先頭からデコードして捨てる)。
#   遅いがサンプル厳密なので、**検査と比較には必ずこちらを使う**。
#   分離本体はシークを使わない逐次デコード (iter_windows) で読むため影響を受けない。
# 戻り値: shape (チャンネル数, サンプル数) の配列
def read_audio(path, settings, start_sec=0.0, duration_sec=None,
               sample_rate=OUT_SAMPLE_RATE, channels=1, accurate=True):
    cmd = [ffmpeg_exe(settings), "-hide_banner", "-loglevel", "error"]
    seek = []
    if float(start_sec) > 0:
        seek = ["-ss", f"{float(start_sec):.6f}"]
    if duration_sec is not None:
        seek += ["-t", f"{float(duration_sec):.6f}"]
    if not accurate:
        cmd += seek
    cmd += ["-i", path]
    if accurate:
        cmd += seek
    cmd += [
        "-vn",
        "-f", "f32le",                 # 量子化を挟まない (分離の前後で丸めたくない)
        "-acodec", "pcm_f32le",
        "-ac", str(int(channels)),
        "-ar", str(int(sample_rate)),
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"音声の読み込みに失敗しました: {path}\n"
            + result.stderr.decode("utf-8", "replace")[-2000:])
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    if channels > 1:
        usable = (len(samples) // channels) * channels
        return samples[:usable].reshape(-1, channels).T.copy()
    return samples.reshape(1, -1).copy()


# 16bit PCM WAV として書き出す (soundfile を使わず標準の wave で足りる)
def write_wav(path, samples, sample_rate=OUT_SAMPLE_RATE):
    import wave

    mono = _to_mono(samples)
    clipped = np.clip(mono, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    return path


# 書き足し用の WAV ライタ (チャンクごとに追記する / §4.2-3)
class WavWriter:

    def __init__(self, path, sample_rate=OUT_SAMPLE_RATE):
        import wave

        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.path = path
        self.written = 0
        self._handle = wave.open(path, "wb")
        self._handle.setnchannels(1)
        self._handle.setsampwidth(2)
        self._handle.setframerate(int(sample_rate))

    # モノラル float 配列を追記する
    def append(self, samples):
        mono = _to_mono(samples)
        pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")
        self._handle.writeframes(pcm.tobytes())
        self.written += len(mono)
        return self.written

    def close(self):
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


# (チャンネル, サンプル) または (サンプル,) をモノラル 1 次元へ寄せる
def _to_mono(samples):
    array = np.asarray(samples, dtype=np.float32)
    if array.ndim == 1:
        return array
    return array.mean(axis=0)


# 全体の RMS を dBFS で返す (分離が黙って無音を返す事故の検出に使う / §4.2-4)
def rms_dbfs(samples):
    mono = _to_mono(samples)
    if len(mono) == 0:
        return -999.0
    rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float64)))))
    if rms <= 1e-12:
        return -999.0
    return 20.0 * float(np.log10(rms))


# サンプリング周波数を変換する (torchaudio の実装を使う)
def resample(samples, src_rate, dst_rate):
    if int(src_rate) == int(dst_rate):
        return np.asarray(samples, dtype=np.float32)
    import torch
    import torchaudio

    tensor = torch.from_numpy(np.ascontiguousarray(np.asarray(samples, dtype=np.float32)))
    if tensor.ndim == 1:
        tensor = tensor.unsqueeze(0)
    converted = torchaudio.functional.resample(tensor, int(src_rate), int(dst_rate))
    return converted.squeeze(0).numpy() if converted.shape[0] == 1 else converted.numpy()


# ------------------------------------------------------------------
# チャンク分割の計画 (§4.2-3)
# ------------------------------------------------------------------

# のりしろ付きの窓を並べ、採用する範囲をサンプル単位で確定させる
#
# 1 サンプルのずれが映像のずれになるため (§4.2-2)、採用範囲は**秒ではなく出力サンプルの
# 添字**で決める。out_start / out_end は隙間なく連続し、最後の out_end は
# round(duration * sample_rate) に一致する = 連結後の総サンプル数が入力と一致する。
#
# 戻り値: [{"read_start","read_end","out_start","out_end"}, ...]
def window_plan(duration_sec, chunk_sec, overlap_sec, sample_rate=OUT_SAMPLE_RATE):
    duration = float(duration_sec)
    width = max(float(chunk_sec), 1.0)
    margin = max(float(overlap_sec), 0.0)
    total_out = int(round(duration * sample_rate))

    plan = []
    index = 0
    while True:
        begin = index * width
        if begin >= duration:
            break
        end = min((index + 1) * width, duration)
        out_start = int(round(begin * sample_rate))
        out_end = int(round(end * sample_rate))
        if out_end > total_out:
            out_end = total_out
        if out_end > out_start:
            plan.append({
                "read_start": max(begin - margin, 0.0),
                "read_end": min(end + margin, duration),
                "out_start": out_start,
                "out_end": out_end,
            })
        index += 1
    if plan:
        # 丸めの端数で最後の窓が足りない場合は末尾を総数へ伸ばす
        plan[-1]["out_end"] = total_out
    return plan


# のりしろ付きの窓を「シークせずに」順番に読み出す (§4.2-2 / §4.2-3)
#
# ffmpeg を 1 本だけ起動して先頭から流し読みし、窓に必要な範囲だけを切り出して返す。
# シークを一切しないため、AAC のフレーム境界丸め (read_audio の accurate の注記) を
# 踏まない = 窓の先頭サンプルが要求どおりになる。
#
# yield するもの: (窓の定義, 波形 (ch, N), 波形の先頭の絶対サンプル添字)
#   絶対サンプル添字は sample_rate 基準。これがあるので、呼び出し側は
#   「秒」を経由せずにサンプル添字だけで採用範囲を決められる。
def iter_windows(path, settings, plan, sample_rate, channels):
    if not plan:
        return
    cmd = [
        ffmpeg_exe(settings), "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-vn",
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "-ac", str(int(channels)),
        "-ar", str(int(sample_rate)),
        "-",
    ]
    frame_bytes = 4 * int(channels)
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        buffer = np.zeros((int(channels), 0), dtype=np.float32)
        buffer_start = 0            # buffer の先頭が全体の何サンプル目か
        for window in plan:
            begin = int(round(window["read_start"] * sample_rate))
            end = int(round(window["read_end"] * sample_rate))
            # 窓の手前はもう要らないので捨てる (メモリを窓 1 つ分に抑える)
            if begin > buffer_start:
                buffer = buffer[:, begin - buffer_start:]
                buffer_start = begin
            # 窓の終わりまで読み足す
            while buffer_start + buffer.shape[1] < end:
                need = end - (buffer_start + buffer.shape[1])
                chunk = process.stdout.read(min(need, 1 << 22) * frame_bytes)
                if not chunk:
                    break
                values = np.frombuffer(chunk, dtype=np.float32)
                usable = (len(values) // int(channels)) * int(channels)
                block = values[:usable].reshape(-1, int(channels)).T
                buffer = np.concatenate([buffer, block], axis=1)
            take = buffer[:, :end - buffer_start].copy()
            yield window, take, buffer_start
    finally:
        if process.stdout is not None:
            process.stdout.close()
        process.wait()


# ------------------------------------------------------------------
# 置き場とファイル
# ------------------------------------------------------------------

# 素材 × 条件の作業フォルダを返す (無ければ作る)
def condition_dir(material_id, condition):
    path = os.path.join(WORK_ROOT, material_id, condition)
    os.makedirs(path, exist_ok=True)
    return path


def material_dir(material_id):
    path = os.path.join(WORK_ROOT, material_id)
    os.makedirs(path, exist_ok=True)
    return path


def save_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# materials.json のパス (素材の定義 / §3.2)
def materials_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "materials.json")


# 計測用の簡易ストップウォッチ
class Stopwatch:

    def __init__(self):
        self.started = time.monotonic()

    def elapsed(self):
        return time.monotonic() - self.started
