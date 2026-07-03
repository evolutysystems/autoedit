# FFmpeg 進捗管理ラッパ
# claude.md 指定の run_ffmpeg_progress を提供する
# 設計書 10. 不明点 #1: 外部の run_ffmpeg_progress 実装が見つからないため
# 本ファイルで FFmpeg -progress pipe:1 を用いた汎用ラッパを定義する
import subprocess
import threading
import time
from collections import deque

from .logger import get_logger

_logger = get_logger(__name__)

# 進捗パイプの読み取りバッファサイズ
_READ_BUFSIZE = 1
# stderr で末尾保持する行数
_STDERR_TAIL_LINES = 30


# stderr を別スレッドで読み続け、末尾 N 行のみ保持する。
# 実行中も常時消費することで、パイプバッファ満杯による
# ffmpeg 側の書き込みブロック (stdout/stderr 相互デッドロック) を防ぐ。
def _drain_stderr(stderr, sink):
    if stderr is None:
        return
    for line in iter(stderr.readline, ""):
        sink.append(line.rstrip("\n"))  # deque(maxlen) が古い行を自動破棄する


# FFmpeg を進捗通知付きで実行する
# command              : ffmpeg コマンド配列 (-progress pipe:1 は本関数で追加)
# total_duration       : 入力動画の総尺 (秒)。0 以下なら進捗率は不明扱い
# on_progress          : Callable[[float, dict], None] 進捗率(0.0-1.0)とkey/value辞書を渡す
# progress_timeout_sec : この秒数 progress 更新が無ければ停滞と判断しプロセスを終了する
#                        (0 以下で監視無効=従来どおりタイムアウトなし)
# 戻り値               : (returncode, stderr_tail)
def run_ffmpeg_progress(command, total_duration=0.0, on_progress=None,
                        progress_timeout_sec=0):
    # progress pipe を追加してプロセス起動
    cmd = list(command) + ["-progress", "pipe:1", "-nostats"]
    _logger.debug("FFmpeg 実行: %s", " ".join(cmd))

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=_READ_BUFSIZE,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
    )

    # stderr を専用スレッドで並行ドレインする (デッドロック対策)
    stderr_tail_buf = deque(maxlen=_STDERR_TAIL_LINES)
    stderr_thread = threading.Thread(
        target=_drain_stderr, args=(process.stderr, stderr_tail_buf), daemon=True
    )
    stderr_thread.start()

    # 無進捗タイムアウト監視スレッドを起動する (有効時のみ)
    timed_out = {"flag": False}
    last_progress_at = {"t": _now()}
    watchdog_stop = threading.Event()
    watchdog_thread = None
    if progress_timeout_sec and progress_timeout_sec > 0:
        watchdog_thread = threading.Thread(
            target=_watch_progress_timeout,
            args=(process, progress_timeout_sec, last_progress_at,
                  timed_out, watchdog_stop),
            daemon=True,
        )
        watchdog_thread.start()

    progress_kv = {}
    try:
        # stdout から progress pipe を逐次パース
        for line in iter(process.stdout.readline, ""):
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            progress_kv[key] = value

            # progress=continue/end のたびにコールバックを呼ぶ
            if key == "progress":
                last_progress_at["t"] = _now()  # 最終進捗時刻を更新 (監視用)
                ratio = _calc_ratio(progress_kv, total_duration)
                if on_progress is not None:
                    try:
                        on_progress(ratio, dict(progress_kv))
                    except Exception:
                        _logger.exception("on_progress コールバックで例外発生")
                if value == "end":
                    break
    finally:
        # 監視スレッドを止め、プロセスと stderr ドレインの終了を待つ
        watchdog_stop.set()
        process.wait()
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    stderr_tail = "\n".join(stderr_tail_buf)
    if timed_out["flag"]:
        # 停滞による強制終了をログに残し、上位で異常終了として扱えるようにする
        _logger.error(
            "FFmpeg が %d 秒間 進捗を更新しなかったため強制終了しました", progress_timeout_sec
        )
    return process.returncode, stderr_tail


# 現在時刻 (単調増加クロック) を返す。Date 系が使えない環境差を避けるため monotonic を使う
def _now():
    return time.monotonic()


# 無進捗タイムアウトを監視し、超過時に ffmpeg プロセスを強制終了する
def _watch_progress_timeout(process, timeout_sec, last_progress_at,
                            timed_out, stop_event):
    while not stop_event.wait(1.0):
        if process.poll() is not None:
            return  # プロセスが既に終了していれば監視終了
        if _now() - last_progress_at["t"] >= timeout_sec:
            timed_out["flag"] = True
            try:
                process.kill()
            except Exception:
                _logger.exception("停滞 FFmpeg プロセスの強制終了に失敗")
            return


# out_time_ms から進捗率を算出する
def _calc_ratio(progress_kv, total_duration):
    if total_duration <= 0:
        return 0.0
    out_time_us = progress_kv.get("out_time_us") or progress_kv.get("out_time_ms")
    if not out_time_us:
        return 0.0
    try:
        elapsed_sec = int(out_time_us) / 1_000_000.0
    except ValueError:
        return 0.0
    ratio = elapsed_sec / total_duration
    if ratio < 0.0:
        return 0.0
    if ratio > 1.0:
        return 1.0
    return ratio
