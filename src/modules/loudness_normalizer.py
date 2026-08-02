# ラウドネス正規化モジュール (resolve22)
# YouTube 向けに音声を Integrated -14.0 LUFS / True Peak -1.0 dBTP へ正規化する。
# FFmpeg loudnorm (EBU R128) の 2 パスを既定とする:
#   1パス目: -af loudnorm=...:print_format=json -f null - で実測値を取得
#   2パス目: 実測値 + linear=true で線形適用 (ダイナミクス維持)。映像は -c:v copy (無劣化)
# 正規化は品質改善の工程のため、測定・適用の失敗では処理を止めず
# 元パスのまま次工程へ継続する (resolve22 §7 / §10-7 スキップ継続)。
import json
import os
import subprocess

from ..exceptions import FFmpegError
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from ..utils.progress import run_ffmpeg_progress
from . import ffmpeg_runner

_logger = get_logger(__name__)

# 正規化済み中間ファイル名 (クリップ用パイプライン / resolve22 §5.3)
_INTERMEDIATE_NAME = "loudness_normalized.mp4"

# 2パス目へ引き渡す実測値の対応 (1パス目 JSON キー → loudnorm パラメータ名)
_MEASURED_KEYS = (
    ("input_i", "measured_I"),
    ("input_tp", "measured_TP"),
    ("input_lra", "measured_LRA"),
    ("input_thresh", "measured_thresh"),
)


# loudness 設定を既定値で補完して返す (resolve22 §6)
# _merge_with_defaults はセクション単位の浅いマージのため、キー欠落はここで既定へ戻す。
def loudness_config(settings):
    cfg = settings.get("loudness", {}) if isinstance(settings, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        # 正規化 ON/OFF (false で従来挙動)
        "enabled": bool(cfg.get("enabled", True)),
        # Integrated 目標 (LUFS) = YouTube 推奨 (要望 A2)
        "target_i": float(cfg.get("target_i", -14.0)),
        # True Peak 目標 (dBTP) (要望 A2)
        "target_tp": float(cfg.get("target_tp", -1.0)),
        # Loudness Range 目標 (LU)
        "target_lra": float(cfg.get("target_lra", 11.0)),
        # true=2パス測定+線形適用 (精度優先) / false=1パス dynamic
        "two_pass": bool(cfg.get("two_pass", True)),
        # 適用パスの音声ビットレート
        "audio_bitrate": str(cfg.get("audio_bitrate", "192k") or "192k"),
    }


# 数値を loudnorm パラメータ表記へ整形する (-14.0 → "-14" / -0.3 → "-0.3")
def _num(value):
    return f"{float(value):g}"


# loudnorm フィルタ文字列を組み立てる (純関数 / resolve22 §5.2)
# measured を与えると 2パス目 (実測値 + 線形適用)、print_json で 1パス目 (測定) 用になる。
def build_loudnorm_filter(cfg, measured=None, print_json=False):
    parts = [
        f"I={_num(cfg['target_i'])}",
        f"TP={_num(cfg['target_tp'])}",
        f"LRA={_num(cfg['target_lra'])}",
    ]
    if measured is not None:
        for src_key, param in _MEASURED_KEYS:
            parts.append(f"{param}={_num(measured[src_key])}")
        parts.append(f"offset={_num(measured.get('target_offset', 0.0))}")
        # 線形適用 = 一定ゲインのみでダイナミクスを変えない (§3)
        parts.append("linear=true")
    if print_json:
        parts.append("print_format=json")
    return "loudnorm=" + ":".join(parts)


# 1パス目の stderr から loudnorm の測定 JSON を抽出する (純関数)
# 抽出・解析できない場合は None (呼び出し側でスキップ判断する)。
def parse_loudnorm_json(stderr_text):
    text = str(stderr_text or "")
    start = text.rfind("{")
    if start < 0:
        return None
    end = text.find("}", start)
    if end < 0:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    try:
        measured = {src_key: float(data[src_key]) for src_key, _param in _MEASURED_KEYS}
        measured["target_offset"] = float(data.get("target_offset", 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    return measured


# 音声ストリームの有無を ffprobe で判定する (無ければ正規化をスキップ / §5.5)
def has_audio_stream(input_path, ffmpeg_cfg):
    ffprobe = ffmpeg_runner.get_ffprobe_exe(ffmpeg_cfg)
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "json",
        input_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            # GUI(windowed)実行時にコンソール窓を出さない (Windows のみ有効)
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError:
        _logger.warning("ffprobe が見つからないため音声ストリーム判定に失敗 (正規化をスキップ)")
        return False
    if result.returncode != 0:
        _logger.warning("音声ストリーム判定に失敗 (正規化をスキップ): %s", input_path)
        return False
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        return False
    return len(streams) > 0


# 1パス目: ラウドネスを測定して実測値 dict を返す (失敗時は None / §5.2)
# 戻り値: {"input_i","input_tp","input_lra","input_thresh","target_offset"}
def measure_loudness(input_path, cfg, ffmpeg_cfg, total_duration=0.0, on_progress=None):
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-hide_banner",
        "-i", input_path,
        "-af", build_loudnorm_filter(cfg, print_json=True),
        # 測定は音声のみで足りるため映像デコードを省いて高速化する
        "-vn",
        "-f", "null", "-",
    ]
    # stderr 末尾 (run_ffmpeg_progress が保持) に loudnorm の測定 JSON が出力される
    returncode, stderr_tail = run_ffmpeg_progress(
        cmd,
        total_duration=total_duration,
        on_progress=on_progress,
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
    )
    if returncode != 0:
        _logger.warning("ラウドネス測定の FFmpeg 実行に失敗 (code=%s)", returncode)
        return None
    measured = parse_loudnorm_json(stderr_tail)
    if measured is None:
        _logger.warning("loudnorm 測定結果 (JSON) の解析に失敗")
    return measured


# 入力動画を正規化して output_path へ書き出す (resolve22 §5.2)
# 成功時は output_path、スキップ/失敗時は input_path を返す
# (呼び出し側は戻り値をそのまま次工程へ渡すだけでよい / §5.5・§7)。
# on_progress(ratio, kv): 測定 0〜0.5 / 適用 0.5〜1.0 に配分 (1パス時は適用のみ 0〜1.0)。
def normalize_file(input_path, output_path, settings, on_progress=None):
    cfg = loudness_config(settings)
    ffmpeg_cfg = settings.get("ffmpeg", {}) if isinstance(settings, dict) else {}

    if not cfg["enabled"]:
        _logger.info("ラウドネス正規化は無効のためスキップ")
        return input_path
    if not has_audio_stream(input_path, ffmpeg_cfg):
        _logger.info("音声ストリームが無いためラウドネス正規化をスキップ: %s", input_path)
        return input_path

    # 進捗の総尺 (取得失敗は進捗率なしで続行)
    try:
        total_duration = ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg)
    except FFmpegError:
        _logger.warning("総尺の取得に失敗 (進捗率なしで正規化を続行): %s", input_path)
        total_duration = 0.0

    # 1パス目: 測定 (two_pass 時のみ。失敗したらスキップ継続 / §10-7)
    measured = None
    if cfg["two_pass"]:
        measure_progress = None
        if on_progress is not None:
            measure_progress = lambda ratio, kv: on_progress(ratio * 0.5, kv)  # noqa: E731
        measured = measure_loudness(
            input_path, cfg, ffmpeg_cfg, total_duration, measure_progress)
        if measured is None:
            _logger.warning(
                "ラウドネス測定に失敗したため正規化をスキップ (元音声のまま継続): %s",
                input_path)
            return input_path
        # 解析結果の可視化 (§5.6)
        _logger.info(
            "入力ラウドネス: I=%.1f LUFS / TP=%.1f dBTP / LRA=%.1f LU "
            "→ 目標 I=%.1f LUFS / TP=%.1f dBTP (offset %.1f dB)",
            measured["input_i"], measured["input_tp"], measured["input_lra"],
            cfg["target_i"], cfg["target_tp"], measured["target_offset"],
        )

    # 2パス目 (または 1パス dynamic): 適用。映像は無劣化コピーでタイムラインを変えない (§4-7)
    apply_progress = None
    if on_progress is not None:
        base = 0.5 if cfg["two_pass"] else 0.0
        scale = 0.5 if cfg["two_pass"] else 1.0
        apply_progress = lambda ratio, kv: on_progress(base + ratio * scale, kv)  # noqa: E731
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-i", input_path,
        "-af", build_loudnorm_filter(cfg, measured=measured),
        "-c:v", "copy",
        "-c:a", ffmpeg_cfg.get("audio_codec", "aac"),
        "-b:a", cfg["audio_bitrate"],
        # loudnorm は内部 192kHz へアップサンプルするため出力レートを明示的に戻す (§3)
        "-ar", str(ffmpeg_cfg.get("audio_sample_rate", 48000)),
        output_path,
    ]
    try:
        ffmpeg_runner.execute(
            cmd, total_duration=total_duration, on_progress=apply_progress,
            progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg))
    except FFmpegError:
        # 失敗しても本編を失わない: 部分ファイルを消して元パスで継続 (§7)
        _logger.exception(
            "ラウドネス正規化の適用に失敗 (元音声のまま継続): %s", input_path)
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass
        return input_path

    _logger.info("ラウドネス正規化 完了: %s", output_path)
    return output_path


# クリップ用パイプラインの工程エントリ (resolve22 §5.3)
# 現在の処理対象を正規化し、成功時のみ中間ファイルへ差し替える。
def run(context):
    settings = context.settings
    if not loudness_config(settings)["enabled"]:
        _logger.info("ラウドネス正規化は無効のためスキップ")
        return

    input_path = context.current_video_path()
    output_path = context.allocate_intermediate(_INTERMEDIATE_NAME)
    result = normalize_file(
        input_path, output_path, settings,
        on_progress=context.progress_subcallback("音声解析・正規化 実行中…"))
    if result != input_path:
        context.set_current_video_path(result)
