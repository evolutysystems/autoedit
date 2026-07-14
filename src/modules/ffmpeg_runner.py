# FFmpeg 共通実行ラッパ
# 設計書 5.6 ffmpeg_runner.py に対応
import json
import os
import shutil
import subprocess
import sys

from ..exceptions import FFmpegError
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from ..utils.progress import run_ffmpeg_progress

_logger = get_logger(__name__)


# 凍結配布物で同梱物を探索する基準ディレクトリ一覧を返す (error 20260708)
# onedir では datas は _internal(=sys._MEIPASS) 配下に、手動配置は exe 直下に置かれ得るため双方を見る。
def _bundle_base_dirs():
    if not getattr(sys, "frozen", False):
        return []
    bases = []
    meipass = getattr(sys, "_MEIPASS", None)   # PyInstaller 展開先 (onedir では _internal)
    if meipass:
        bases.append(meipass)
    bases.append(os.path.dirname(sys.executable))   # exe 直下 (手動配置の同梱物)
    return bases


# 設定値を同梱物として探索する相対パス候補へ展開する (error 20260709)
# 裸名("ffmpeg"/"ffprobe") のレガシー設定でも同梱レイアウト ffmpeg/<name>.exe を探せるようにし、
# setting.json の正規化が効かない環境でも同梱 FFmpeg を解決できる保険とする。
def _bundle_relpath_candidates(configured):
    candidates = [configured]
    if "/" not in configured and "\\" not in configured:
        name = configured if configured.lower().endswith(".exe") else configured + ".exe"
        candidates.append(os.path.join("ffmpeg", name))
    return candidates


# 設定値から実行ファイルの実体パスを PATH 非依存で解決する (error 20260708)
# 解決順: 絶対パス → 凍結配布物の同梱物(exe 隣接) → PATH(shutil.which) → 設定値そのまま。
# 同梱 ffmpeg を確実に使わせ、PATH に FFmpeg が無いPCでの起動失敗を防ぐ。
def _resolve_exe(configured):
    # 1) 絶対パス指定はそのまま採用
    if os.path.isabs(configured):
        return configured
    # 2) 凍結時は同梱物(相対パス)を exe 隣接基準で絶対化して優先採用。
    #    裸名のレガシー値でも ffmpeg/<name>.exe を試す (error 20260709)。
    for rel in _bundle_relpath_candidates(configured):
        for base in _bundle_base_dirs():
            candidate = os.path.normpath(os.path.join(base, rel))
            if os.path.isfile(candidate):
                return candidate
    # 3) PATH 解決 (開発実行や PATH 導入環境)。相対区切りを含む値は basename でも探す
    found = shutil.which(configured) or shutil.which(os.path.basename(configured))
    if found:
        return found
    # 4) 見つからず: 設定値をそのまま返す (ensure_available が明確なエラーを出す)
    return configured


# FFmpeg 設定から実行ファイルパスを取得する
def get_ffmpeg_exe(ffmpeg_settings):
    return _resolve_exe(ffmpeg_settings.get("executable", "ffmpeg/ffmpeg.exe"))


# ffprobe 実行ファイルパスを取得する
def get_ffprobe_exe(ffmpeg_settings):
    return _resolve_exe(ffmpeg_settings.get("ffprobe_executable", "ffmpeg/ffprobe.exe"))


# 無進捗タイムアウト秒数を設定から取得する (0 以下で監視無効)
def get_progress_timeout_sec(ffmpeg_settings):
    try:
        return int(ffmpeg_settings.get("progress_timeout_sec", 0))
    except (TypeError, ValueError):
        return 0


# 出力フレームレート(CFR 正規化用)を設定から取得する (不正値は既定 60)
# VFR/タイムスタンプ破損対策で、再エンコード時の CFR 化に使用する (resolve 20260630 対策A/B)
def get_output_fps(ffmpeg_settings):
    try:
        fps = int(ffmpeg_settings.get("output_fps", 60))
    except (TypeError, ValueError):
        return 60
    return fps if fps > 0 else 60


# 実行可能性チェック (FFmpeg/ffprobe が解決可能か)
# get_ffmpeg_exe/get_ffprobe_exe が絶対パス(同梱物)を返した場合は実在で判定し、
# 裸の名前を返した場合は PATH 解決で判定する (error 20260708)。
def ensure_available(ffmpeg_settings):
    for exe in (get_ffmpeg_exe(ffmpeg_settings), get_ffprobe_exe(ffmpeg_settings)):
        if not (os.path.isfile(exe) or shutil.which(exe)):
            raise FFmpegError(f"FFmpeg 実行ファイルが見つかりません: {exe}")


# 動画の総再生時間を ffprobe で取得する
def probe_duration(input_path, ffmpeg_settings):
    ffprobe = get_ffprobe_exe(ffmpeg_settings)
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        input_path,
    ]
    _logger.debug("ffprobe 実行: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            # GUI(windowed)実行時にコンソール窓を出さない (Windows のみ有効)
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError as e:
        raise FFmpegError(f"ffprobe 実行に失敗: {e}") from e

    if result.returncode != 0:
        raise FFmpegError(
            "ffprobe が失敗",
            command=cmd,
            stderr_tail=result.stderr,
            returncode=result.returncode,
        )

    try:
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        raise FFmpegError(f"ffprobe 出力解析に失敗: {e}") from e


# 映像ストリームの回転量(度)を取得する (縦横判定の表示寸法補正用 / request14)
# スマホ撮影動画は生の width/height が横のまま rotation メタ(±90 等)で縦表示されるため、
# 回転を加味しないと縦動画を横と誤判定する。新旧 ffmpeg の両表現に対応する:
#   ・新: stream.side_data_list[].rotation (Display Matrix, 例 -90)
#   ・旧: stream.tags.rotate (例 "90")
def _extract_rotation(stream):
    # 新形式: side_data_list の rotation を優先
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            try:
                return int(round(float(side["rotation"])))
            except (TypeError, ValueError):
                pass
    # 旧形式: tags.rotate
    rotate = (stream.get("tags", {}) or {}).get("rotate")
    if rotate is not None:
        try:
            return int(round(float(rotate)))
        except (TypeError, ValueError):
            pass
    return 0


# 動画の表示寸法(回転適用後の幅・高さ)を ffprobe で取得する (request14)
# 戻り値: (width, height)。取得・解析失敗時は FFmpegError を送出する。
def probe_dimensions(input_path, ffmpeg_settings):
    ffprobe = get_ffprobe_exe(ffmpeg_settings)
    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_streams",
        "-of", "json",
        input_path,
    ]
    _logger.debug("ffprobe(寸法) 実行: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError as e:
        raise FFmpegError(f"ffprobe 実行に失敗: {e}") from e

    if result.returncode != 0:
        raise FFmpegError(
            "ffprobe が失敗", command=cmd,
            stderr_tail=result.stderr, returncode=result.returncode,
        )

    try:
        streams = json.loads(result.stdout).get("streams", [])
        if not streams:
            raise FFmpegError("映像ストリームが見つかりません")
        stream = streams[0]
        width = int(stream["width"])
        height = int(stream["height"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        raise FFmpegError(f"ffprobe 出力解析に失敗: {e}") from e

    # 回転(±90/±270)がある場合は表示寸法として幅・高さを入れ替える
    rotation = _extract_rotation(stream)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


# 進捗通知付きで FFmpeg コマンドを実行する
# 失敗時は FFmpegError を送出
# progress_timeout_sec : 無進捗タイムアウト秒数 (0 以下で監視無効)
def execute(command, total_duration=0.0, on_progress=None, progress_timeout_sec=0):
    returncode, stderr_tail = run_ffmpeg_progress(
        command,
        total_duration=total_duration,
        on_progress=on_progress,
        progress_timeout_sec=progress_timeout_sec,
    )
    if returncode != 0:
        _logger.error("FFmpeg 失敗 (code=%s): %s", returncode, stderr_tail)
        raise FFmpegError(
            f"FFmpeg 実行失敗 (returncode={returncode})",
            command=command,
            stderr_tail=stderr_tail,
            returncode=returncode,
        )
    return returncode


# 出力品質オプションを setting.json の ffmpeg セクションから構築する
def build_encode_options(ffmpeg_settings):
    return [
        "-c:v", ffmpeg_settings.get("video_codec", "libx264"),
        "-preset", ffmpeg_settings.get("preset", "medium"),
        "-crf", str(ffmpeg_settings.get("crf", 20)),
        "-c:a", ffmpeg_settings.get("audio_codec", "aac"),
    ]
