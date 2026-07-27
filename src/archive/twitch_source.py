# Twitch VOD / コメント取得 (twitch-dl ラッパ) (flow17 R3 / resolve17 §4.3.1・§4.3.2)
# VOD もコメントも twitch-dl(ihabunek) に一本化する。VOD=`download` / コメント=`chat json`。
# GUI(windowed)から起動するためコンソール窓を出さない (no_window_creationflags)。
# twitch-dl は同梱CLI(ffmpeg と同方式で解決) / PATH / 開発時は `python -m twitchdl` を順に探す。
import os
import re
import subprocess
import sys

from ..exceptions import InputError, TwitchError
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags

_logger = get_logger(__name__)

# Twitch VOD URL から video id を抽出する正規表現 (twitch.tv/videos/<digits>) と裸ID。
_VIDEO_URL_RE = re.compile(r"(?:twitch\.tv/videos/)(\d+)", re.IGNORECASE)
_BARE_ID_RE = re.compile(r"^\d+$")


# 入力(URL または 数字ID)から video id を取り出す。不正なら InputError。
def extract_video_id(url_or_id):
    text = (url_or_id or "").strip()
    if not text:
        raise InputError("Twitch VOD の URL または動画IDを入力してください。")
    if _BARE_ID_RE.match(text):
        return text
    m = _VIDEO_URL_RE.search(text)
    if not m:
        raise InputError(f"Twitch VOD の URL 形式が不正です: {text}")
    return m.group(1)


# 凍結配布物で同梱物を探索する基準ディレクトリ (ffmpeg_runner と同方針)
def _bundle_base_dirs():
    if not getattr(sys, "frozen", False):
        return []
    bases = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(meipass)
    bases.append(os.path.dirname(sys.executable))
    return bases


# twitch-dl 実行コマンド(argv 前半)を解決する。
# 解決順: 絶対パス → 同梱 twitch-dl.exe → 凍結時は本体exeの multi-call(同梱twitchdl) →
#         PATH(システム導入) → 開発時 `python -m twitchdl` → 設定値。
# 凍結配布物では twitchdl モジュールを同梱し、本体exeを `Stretheus.exe __twitchdl__ ...` として
# 呼ぶことで twitch-dl を別バイナリ無しに同梱する (main_window.main のディスパッチ / flow17 R3)。
def _resolve_twitchdl(configured):
    import shutil

    configured = configured or "twitch-dl"
    # 1) 明示的な絶対パス
    if os.path.isabs(configured) and os.path.isfile(configured):
        return [configured]
    # 2) 同梱 twitch-dl.exe (別途同梱している場合)
    name = configured if configured.lower().endswith(".exe") else configured + ".exe"
    for base in _bundle_base_dirs():
        for rel in (configured, name, os.path.join("twitch-dl", name)):
            candidate = os.path.normpath(os.path.join(base, rel))
            if os.path.isfile(candidate):
                return [candidate]
    # 3) 凍結時: 本体exeを multi-call で twitchdl として使う (同梱 twitchdl モジュール)
    if getattr(sys, "frozen", False):
        return [sys.executable, "__twitchdl__"]
    # 4) PATH (システム導入の twitch-dl)
    found = shutil.which(configured) or shutil.which(os.path.basename(configured))
    if found:
        return [found]
    # 5) 開発実行時のフォールバック: モジュールが import 可能なら python -m twitchdl
    try:
        import twitchdl  # noqa: F401
        return [sys.executable, "-m", "twitchdl"]
    except ImportError:
        pass
    # 6) 見つからず: 設定値をそのまま (実行時にエラー通知)
    return [configured]


# twitch-dl 実行用の環境変数を用意する。twitch-dl は VOD 結合に ffmpeg を PATH から探すため、
# 同梱 ffmpeg のディレクトリを PATH 先頭へ加える (PATH 非依存で確実に見つけさせる)。
def _twitchdl_env(ffmpeg_dir):
    env = os.environ.copy()
    if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
        env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
    return env


# サブプロセスを実行し、標準出力/エラーを progress_cb に流す。非0終了で TwitchError。
def _run(argv, progress_cb=None, label="取得中…", env=None):
    _logger.info("twitch-dl 実行: %s", " ".join(str(a) for a in argv[:2]) + " …")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError:
        raise TwitchError(
            "twitch-dl が見つかりません。導入(pip install twitch-dl)するか、"
            "setting.json の archive.download.twitch_dl_path を設定してください。")
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        if len(tail) > 30:
            tail.pop(0)
        if progress_cb:
            progress_cb(f"{label} {line[:80]}")
    code = proc.wait()
    if code != 0:
        raise TwitchError(f"twitch-dl の実行に失敗しました (code {code})。\n" + "\n".join(tail[-5:]))
    return tail


# VOD をダウンロードして mp4 の絶対パスを返す (resolve17 §4.3.1)。
# auth_token を渡すと sub-only/限定 VOD にも対応 (公開 VOD は不要)。
# start/end は "hh:mm[:ss]" 形式の範囲指定 (任意)。
def download_vod(video_id, work_dir, twitch_dl_path="twitch-dl", quality="source",
                 auth_token="", start=None, end=None, progress_cb=None, ffmpeg_dir=None):
    os.makedirs(work_dir, exist_ok=True)
    out_template = os.path.join(work_dir, "vod_{id}.{format}")
    expected = os.path.join(work_dir, f"vod_{video_id}.mp4")

    argv = list(_resolve_twitchdl(twitch_dl_path))
    argv += ["download", str(video_id),
             "--quality", quality or "source",
             "--format", "mp4",
             "--output", out_template,
             "--skip-existing"]
    if auth_token:
        argv += ["--auth-token", auth_token]
    if start:
        argv += ["--start", start]
    if end:
        argv += ["--end", end]

    _run(argv, progress_cb, label="VOD取得中…", env=_twitchdl_env(ffmpeg_dir))
    if not os.path.isfile(expected):
        raise TwitchError(f"VOD の取得後にファイルが見つかりません: {expected}")
    _logger.info("VOD 取得完了: %s", expected)
    return expected


# コメント(チャット)を json で取得し、その json ファイルの絶対パスを返す (resolve17 §4.3.2)。
# 取得失敗は TwitchError。呼び出し側で握りつぶしてコメント無し採点に切り替えてよい (§5)。
def download_chat(video_id, work_dir, twitch_dl_path="twitch-dl", progress_cb=None,
                  ffmpeg_dir=None):
    os.makedirs(work_dir, exist_ok=True)
    out_template = os.path.join(work_dir, "chat_{id}.{format}")
    expected = os.path.join(work_dir, f"chat_{video_id}.json")

    argv = list(_resolve_twitchdl(twitch_dl_path))
    argv += ["chat", "json", str(video_id), "--output", out_template, "--overwrite"]

    _run(argv, progress_cb, label="コメント取得中…", env=_twitchdl_env(ffmpeg_dir))
    if not os.path.isfile(expected):
        raise TwitchError(f"コメントの取得後にファイルが見つかりません: {expected}")
    _logger.info("コメント取得完了: %s", expected)
    return expected
