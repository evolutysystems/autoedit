# Twitch VOD / コメント取得 (twitch-dl ラッパ) (flow17 R3 / resolve17 §4.3.1・§4.3.2)
# VOD もコメントも twitch-dl(ihabunek) に一本化する。VOD=`download` / コメント=`chat json`。
# GUI(windowed)から起動するためコンソール窓を出さない (no_window_creationflags)。
# twitch-dl は同梱CLI(ffmpeg と同方式で解決) / PATH / 開発時は `python -m twitchdl` を順に探す。
import json
import os
import re
import shutil
import subprocess
import sys

from ..exceptions import DiskSpaceError, FFmpegError, InputError, TwitchError
from ..i18n import tr
from ..utils import disk
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags

_logger = get_logger(__name__)

# 子プロセス (main_window._run_twitchdl_child) と取り決めた合図。
# 片方だけ直すと伝わらなくなるため、値を変えるときは必ず両方を直すこと。
_EXIT_DISK_FULL = 28
_TWITCHDL_ERROR_MARK = "__TWITCHDL_ERROR__"

# Twitch VOD URL から video id を抽出する正規表現 (twitch.tv/videos/<digits>) と裸ID。
_VIDEO_URL_RE = re.compile(r"(?:twitch\.tv/videos/)(\d+)", re.IGNORECASE)
_BARE_ID_RE = re.compile(r"^\d+$")

# 画質名から解像度とフレームレートを読む ("1080p60" → 1080/60 / "360p" → 360/なし)
_QUALITY_NAME_RE = re.compile(r"(\d+)p(\d+)?")

# 音声のみのレンディション (twitch-dl の group_id)。映像が無く編集できないため候補から外す。
_AUDIO_ONLY_GROUP = "audio_only"

# 画質一覧の取得を待つ上限 (秒)。API 2 回ぶんのため通常は数秒で返る。
# 応答が無いときに VOD 取得ごと固まらないよう頭打ちにする (error 20260810)。
_INFO_TIMEOUT_SEC = 60

# 画質名にフレームレートが書かれていないときの既定 (Twitch の通常配信)
_DEFAULT_FPS = 30


# 入力(URL または 数字ID)から video id を取り出す。不正なら InputError。
def extract_video_id(url_or_id):
    text = (url_or_id or "").strip()
    if not text:
        raise InputError(tr("Twitch VOD の URL または動画IDを入力してください。"))
    if _BARE_ID_RE.match(text):
        return text
    m = _VIDEO_URL_RE.search(text)
    if not m:
        raise InputError(
            tr("Twitch VOD の URL 形式が不正です: {url}", url=text))
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
            tr("twitch-dl が見つかりません。導入(pip install twitch-dl)するか、"
            "setting.json の archive.download.twitch_dl_path を設定してください。"))
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
        # 凍結配布の子プロセスは容量不足を終了コードで知らせてくる
        # (main_window._run_twitchdl_child / resolve.md §6-1)。
        # 「取得に失敗しました」ではなく原因そのものを出すため、ここで作り分ける。
        if code == _EXIT_DISK_FULL or disk.looks_like_no_space("\n".join(tail)):
            path = _last_target_dir(argv)
            raise DiskSpaceError(
                disk.shortage_message(path, disk.free_bytes(path), None),
                path=path, free=disk.free_bytes(path))
        detail = "\n".join(
            line for line in tail[-5:] if not line.startswith(_TWITCHDL_ERROR_MARK))
        marked = [line for line in tail if line.startswith(_TWITCHDL_ERROR_MARK)]
        if marked:
            # 子が握った例外の型と文言 (ダイアログの代わりに届く 1 行)
            detail = (marked[-1][len(_TWITCHDL_ERROR_MARK):].strip()
                      + ("\n" + detail if detail else ""))
        raise TwitchError(f"twitch-dl の実行に失敗しました (code {code})。\n" + detail)
    return tail


# 容量不足の報告に使う「書き込み先」を argv から拾う。
# --cache-dir / --output のどちらかが必ず付いているため、そこから親フォルダを取る。
# 見つからなければ現在の作業フォルダを返す (空き容量の桁が分かれば十分なため)。
def _last_target_dir(argv):
    for key in ("--cache-dir", "--output"):
        if key in argv:
            value = str(argv[argv.index(key) + 1])
            # テンプレート ({id} 等) を含むため、実在する親まで遡って使う
            while value and ("{" in os.path.basename(value) or not os.path.isdir(value)):
                parent = os.path.dirname(value)
                if not parent or parent == value:
                    break
                value = parent
            if value:
                return value
    return os.getcwd()


# ------------------------------------------------------------------
# 画質の解決 (docs/error/20260810/resolve.md)
# ------------------------------------------------------------------

# VOD で利用できる画質の一覧を返す。
# 戻り値: [{"name","group_id","resolution","is_source"}] / 取得できないときは []。
# 取れなくても VOD 取得自体は続けられるよう、失敗は例外にせず空リストで返す。
def list_qualities(video_id, twitch_dl_path="twitch-dl", ffmpeg_dir=None):
    argv = list(_resolve_twitchdl(twitch_dl_path)) + ["info", str(video_id), "--json"]
    try:
        # info は JSON を stdout・進捗ログを stderr へ出すため、_run() (stderr を stdout へ
        # 合流させる) は使わず stdout だけを取る。合流させると JSON が壊れる。
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env=_twitchdl_env(ffmpeg_dir),
            creationflags=no_window_creationflags(),
            timeout=_INFO_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _logger.warning("画質一覧を取得できませんでした (希望画質のまま続行します): %s", e)
        return []
    if result.returncode != 0:
        _logger.info("画質一覧を取得できませんでした (code %d)。希望画質のまま続行します",
                     result.returncode)
        return []

    text = result.stdout or ""
    start = text.find("{")
    if start < 0:
        _logger.info("画質一覧の出力を解釈できませんでした。希望画質のまま続行します")
        return []
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError:
        _logger.info("画質一覧の出力を解釈できませんでした。希望画質のまま続行します")
        return []

    playlists = []
    for entry in data.get("playlists", []) or []:
        playlists.append({
            "name": str(entry.get("name", "") or ""),
            "group_id": str(entry.get("group_id", "") or ""),
            "resolution": entry.get("resolution"),
            "is_source": bool(entry.get("is_source", False)),
        })
    if playlists:
        _logger.info("利用できる画質: %s",
                     ", ".join(p["name"] for p in playlists))
    return playlists


# 画質の優劣を表す並べ替えキー (解像度の高さ → フレームレート の順)
def _quality_key(playlist):
    height = 0
    resolution = str(playlist.get("resolution") or "")
    if "x" in resolution:
        try:
            height = int(resolution.split("x")[1])
        except (IndexError, ValueError):
            height = 0
    match = _QUALITY_NAME_RE.search(str(playlist.get("name") or ""))
    fps = _DEFAULT_FPS
    if match:
        if not height:
            height = int(match.group(1))
        if match.group(2):
            fps = int(match.group(2))
    return (height, fps)


# 希望画質 preferred が使えるならそれを、無ければ利用できる最高画質の名前を返す。
# playlists が空 (一覧を取れなかった) 場合は preferred をそのまま返す。
# 希望画質が使えるときは preferred を返す = 組み立てるコマンドが従来と同一になる。
def resolve_quality(playlists, preferred="source"):
    preferred = (preferred or "source").strip() or "source"
    # 音声のみは映像が無く編集できないため、常に候補から外す
    usable = [p for p in (playlists or [])
              if str(p.get("group_id") or "") != _AUDIO_ONLY_GROUP]
    if not usable:
        return preferred

    if preferred == "source":
        if any(p.get("is_source") for p in usable):
            return preferred
    else:
        for playlist in usable:
            if preferred in (playlist.get("name"), playlist.get("group_id")):
                return preferred

    # 希望画質が無い → 利用できる最高画質へ落とす (error 20260810 / 回答 Q1・Q2)。
    # twitch-dl は name / group_id のどちらでも選べるが、一覧表示と一致する name を渡す。
    best = max(usable, key=_quality_key)
    return best.get("name") or preferred


# VOD をダウンロードして mp4 の絶対パスを返す (resolve17 §4.3.1)。
# auth_token を渡すと sub-only/限定 VOD にも対応 (公開 VOD は不要)。
# start/end は "hh:mm[:ss]" 形式の範囲指定 (任意)。
# quality_fallback=True なら、希望画質が無い VOD で利用できる最高画質へ自動で落とす
# (error 20260810。source を持たない VOD が存在するため)。False で従来どおりの挙動。
def download_vod(video_id, work_dir, twitch_dl_path="twitch-dl", quality="source",
                 auth_token="", start=None, end=None, progress_cb=None, ffmpeg_dir=None,
                 quality_fallback=True, ffmpeg_cfg=None, min_free_bytes=0,
                 keep_cache_on_error=False):
    os.makedirs(work_dir, exist_ok=True)
    out_template = os.path.join(work_dir, "vod_{id}.{format}")
    expected = os.path.join(work_dir, f"vod_{video_id}.mp4")
    # 断片 (.ts) の置き場を work_dir 配下へ引き取る (resolve.md §6-3-1)。
    # 既定のままだと %LOCALAPPDATA%\twitch-dl へ落ち、
    #   ・利用者からは見えない
    #   ・work_dir を別ドライブへ移しても断片は C: に残る
    #   ・失敗時に消えない (twitch-dl は成功時しか消さない)
    # という三重の問題になる (2026-10-22 の報告で 48 GB が滞留していた)。
    cache_root = os.path.join(work_dir, "cache")
    cache_template = os.path.join(cache_root, "{id}", "{quality}")
    cache_for_video = os.path.join(cache_root, str(video_id))

    # 既に取得済みなら作り直さない。ただし**使える mp4 かどうかで判断する**
    # (resolve.md §6-4)。twitch-dl の --skip-existing は存在するかしか見ないため、
    # 前回の失敗で残った壊れたファイルを「取得済み」として通してしまい、
    # 採点の最初の ffprobe で「ffprobeが失敗」になる。
    if os.path.exists(expected):
        if _is_usable_video(expected, ffmpeg_cfg):
            _logger.info("取得済みの VOD を再利用します: %s", expected)
            if progress_cb:
                progress_cb(tr("取得済みの配信データを使います"))
            return expected
        _logger.error(
            "取得済みの VOD が壊れているため取得し直します: %s (%s)",
            expected, disk.human(_size_or_zero(expected)))
        if progress_cb:
            progress_cb(tr("前回の取得が壊れていたため取得し直します"))
        _remove_quietly(expected)

    # 取得前に空きを確かめる (resolve.md §6-3-3)。
    # 断片と結合後で VOD 1 本の約 2 倍を使うが、尺は事前に分からないため
    # 設定値 (min_free_space_gb) をそのまま要求値とする (§8-2 7-5)。
    disk.ensure_free(work_dir, min_free_bytes, label="VOD取得")

    wanted = quality or "source"
    playlists = []
    chosen = wanted
    if quality_fallback:
        playlists = list_qualities(video_id, twitch_dl_path, ffmpeg_dir)
        chosen = resolve_quality(playlists, wanted)
        if chosen != wanted:
            message = (f"{wanted} 画質が無いため {chosen} で取得します "
                       f"(利用可能: {', '.join(p['name'] for p in playlists)})")
            _logger.info(message)
            if progress_cb:
                progress_cb(message)

    argv = list(_resolve_twitchdl(twitch_dl_path))
    # --skip-existing は使わない (上で自前に検証済み / resolve.md §6-4)。
    # 判断をアプリ側へ一本化し、壊れたファイルを素通りさせない。
    argv += ["download", str(video_id),
             "--quality", chosen,
             "--format", "mp4",
             "--output", out_template,
             "--cache-dir", cache_template,
             "--overwrite"]
    if auth_token:
        argv += ["--auth-token", auth_token]
    if start:
        argv += ["--start", start]
    if end:
        argv += ["--end", end]

    try:
        _run(argv, progress_cb, label="VOD取得中…", env=_twitchdl_env(ffmpeg_dir))
    except DiskSpaceError:
        # 容量不足は原因がそのまま答えなので包み直さない。
        # ただし断片は丸ごと残るため、ここで必ず引き取る (48 GB 滞留の再発防止)。
        _cleanup_cache(cache_for_video, keep_cache_on_error)
        raise
    except TwitchError as e:
        _cleanup_cache(cache_for_video, keep_cache_on_error)
        # 画質が原因のときに何を直せばよいか分かるよう、候補を添えて包み直す
        names = ", ".join(p["name"] for p in playlists) if playlists else "取得できませんでした"
        raise TwitchError(
            tr("VOD の取得に失敗しました (指定画質: {quality} / 利用可能: {available})。\n"
               "{error}", quality=chosen, available=names, error=e)) from e
    if not os.path.isfile(expected):
        raise TwitchError(
            tr("VOD の取得後にファイルが見つかりません: {path}", path=expected))
    # 取得できた「ことになっている」ファイルも必ず確かめる (resolve.md §6-4-3)。
    # ここで弾いておかないと、壊れたまま採点へ進んで ffprobe の失敗として現れる。
    if not _is_usable_video(expected, ffmpeg_cfg):
        size = _size_or_zero(expected)
        _remove_quietly(expected)
        _cleanup_cache(cache_for_video, keep_cache_on_error)
        raise TwitchError(
            tr("取得した配信データが壊れています ({size})。"
               "空き容量を確かめてから、もう一度お試しください。",
               size=disk.human(size)))
    _logger.info("VOD 取得完了: %s (%s / 画質 %s)",
                 expected, disk.human(_size_or_zero(expected)), chosen)
    return expected


# 動画として読める状態かを ffprobe で確かめる (resolve.md §6-4)。
# ffmpeg_cfg が無い呼び出し (テスト等) ではサイズだけで判定し、従来どおり通す。
def _is_usable_video(path, ffmpeg_cfg):
    if _size_or_zero(path) <= 0:
        return False
    if ffmpeg_cfg is None:
        return True
    from ..modules import ffmpeg_runner       # noqa: PLC0415 (循環 import を避ける)

    try:
        return float(ffmpeg_runner.probe_duration(path, ffmpeg_cfg)) > 0.0
    except (FFmpegError, OSError, ValueError):
        return False


def _size_or_zero(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remove_quietly(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        _logger.warning("壊れたファイルを削除できませんでした: %s (%s)", path, e)


# 取得に失敗したときの断片キャッシュを片づける (resolve.md §6-3-2)。
# twitch-dl は成功時しか消さないため、失敗が積み重なると断片だけが残り続ける。
# keep=True (設定 keep_cache_on_error) のときは再試行を速くするため残す。
def _cleanup_cache(cache_dir, keep):
    if keep:
        _logger.info("設定により断片キャッシュを残します: %s", cache_dir)
        return
    if not os.path.isdir(cache_dir):
        return
    freed = 0
    for root, _dirs, files in os.walk(cache_dir):
        for name in files:
            freed += _size_or_zero(os.path.join(root, name))
    try:
        shutil.rmtree(cache_dir)
        _logger.info("断片キャッシュを削除しました: %s (%s 回収)",
                     cache_dir, disk.human(freed))
    except OSError as e:
        _logger.warning("断片キャッシュを削除できませんでした: %s (%s)", cache_dir, e)


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
        raise TwitchError(
            tr("コメントの取得後にファイルが見つかりません: {path}", path=expected))
    _logger.info("コメント取得完了: %s", expected)
    return expected
