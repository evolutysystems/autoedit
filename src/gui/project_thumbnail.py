# Timeline の V1 からサムネイル (帯) を作り、キャッシュする
# (docs/request/ver3/resolve9.md §3-9 / §5.15)
#
# 保存済みプロジェクトの V1 素材は一時ファイルで、一覧を開く時点では消えている。
# そのため素材が無ければ元動画へ読み替えて同じ絵を取る (§2.7)。
# ラウドネス正規化は映像を -c:v copy で通すため、元動画と時間軸が一致する。
#
# 1 コマ = ffmpeg の 1 プロセスで JPEG を書く。raw バッファ方式 (frame_source) を
# 使わないのは、元動画へフォールバックした場合その寸法が Timeline に記録されて
# いないため。JPEG なら寸法を知らずに済み、そのままキャッシュにもなる。
import hashlib
import os
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap

from ..modules import ffmpeg_runner
from ..timeline import project_io
from ..timeline.builder import timeline_config
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags

_logger = get_logger(__name__)

# キャッシュの相対指定を解決する基準 (archive.download.work_dir と同方針)
_SETTINGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings")


def _library_cfg(settings):
    return timeline_config(settings)["project"]["library"]


# キャッシュ置き場を絶対パスで返す (無ければ作る)
def cache_dir(settings):
    folder = _library_cfg(settings)["thumbnail_dir"]
    if not os.path.isabs(folder):
        folder = os.path.join(_SETTINGS_DIR, folder)
    os.makedirs(folder, exist_ok=True)
    return folder


# プロジェクトごとのキャッシュ名の先頭部分 (絶対パスの sha1)
def _cache_key(project_path):
    full = os.path.normcase(os.path.abspath(str(project_path)))
    return hashlib.sha1(full.encode("utf-8")).hexdigest()


# キャッシュファイルのパス一覧 (コマ数ぶん)
def cache_paths(project_path, settings):
    folder = cache_dir(settings)
    key = _cache_key(project_path)
    count = max(int(_library_cfg(settings)["thumbnail_count"]), 1)
    return [os.path.join(folder, f"{key}_{i}.jpg") for i in range(count)]


# キャッシュが最新か (プロジェクトの更新時刻より新しいか)
def is_fresh(project_path, settings):
    paths = cache_paths(project_path, settings)
    if not paths or not all(os.path.exists(p) for p in paths):
        return False
    try:
        project_mtime = os.path.getmtime(project_path)
        return all(os.path.getmtime(p) >= project_mtime for p in paths)
    except OSError:
        return False


# サムネイルを用意する (無ければ作る)
# timeline を渡すと読み込みを省ける (保存直後の呼び出し用)。
# summary を渡すと read_summary の結果 (frames) を再利用する。
# 戻り値: 実在する JPEG のパス一覧 (作れなければ空)
def ensure(project_path, settings, timeline=None, summary=None):
    try:
        if is_fresh(project_path, settings):
            return [p for p in cache_paths(project_path, settings) if os.path.exists(p)]
        frames = _resolve_frames(project_path, settings, timeline, summary)
        if not frames:
            return []
        return _render(project_path, settings, frames)
    except Exception:  # noqa: BLE001 (サムネイルの失敗で保存や一覧を止めない)
        _logger.warning("サムネイルを作れませんでした: %s", project_path)
        return []


# フレーム位置 ({"path","sec"} の一覧) を決める
def _resolve_frames(project_path, settings, timeline, summary):
    count = max(int(_library_cfg(settings)["thumbnail_count"]), 1)
    if summary is not None and summary.get("frames"):
        return summary["frames"][:count]
    if timeline is not None:
        return project_io.plan_frames(timeline, count)
    loaded = project_io.load(project_path, validate_timeline=False)
    return project_io.plan_frames(loaded, count)


# ffmpeg で 1 コマずつ JPEG を書き出す
def _render(project_path, settings, frames):
    cfg = _library_cfg(settings)
    width = max(int(cfg["thumbnail_width_px"]), 16)
    ffmpeg_cfg = settings.get("ffmpeg", {})
    try:
        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    except Exception:  # noqa: BLE001 (ffmpeg が無ければサムネイルは諦める)
        return []

    created = []
    for dest, frame in zip(cache_paths(project_path, settings), frames):
        source = str(frame.get("path", "") or "")
        if not source or not os.path.exists(source):
            continue
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(float(frame.get('sec', 0.0)), 0.0):.3f}",
            "-i", source,
            "-frames:v", "1",
            "-vf", f"scale={width}:-2",
            "-q:v", "3",
            dest,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True,
                creationflags=no_window_creationflags())
        except OSError:
            _logger.warning("サムネイルの抽出に失敗しました: %s", source)
            continue
        if result.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            created.append(dest)
    if created:
        _cleanup(settings)
    return created


# JPEG 群を横に並べた 1 枚の QPixmap にする (一覧のアイコン用)
def build_pixmap(paths, settings):
    pixmaps = [QPixmap(p) for p in (paths or []) if p and os.path.exists(p)]
    pixmaps = [p for p in pixmaps if not p.isNull()]
    if not pixmaps:
        return None
    if len(pixmaps) == 1:
        return pixmaps[0]
    gap = 2
    height = max(p.height() for p in pixmaps)
    width = sum(p.width() for p in pixmaps) + gap * (len(pixmaps) - 1)
    strip = QPixmap(width, height)
    strip.fill(Qt.transparent)
    painter = QPainter(strip)
    try:
        x = 0
        for pixmap in pixmaps:
            painter.drawPixmap(x, (height - pixmap.height()) // 2, pixmap)
            x += pixmap.width() + gap
    finally:
        painter.end()
    return strip


# プロジェクトの削除に合わせてキャッシュも消す (再生成できる中間物のためゴミ箱へは送らない)
def discard(project_path, settings):
    for path in cache_paths(project_path, settings):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            _logger.debug("サムネイルの削除に失敗しました: %s", path)


# 改名に追随させる (失敗しても次に開くとき作り直されるだけ)
def rename(old_path, new_path, settings):
    for old, new in zip(cache_paths(old_path, settings),
                        cache_paths(new_path, settings)):
        try:
            if os.path.exists(old):
                os.replace(old, new)
        except OSError:
            _logger.debug("サムネイルの付け替えに失敗しました: %s", old)


# キャッシュが増えすぎたら古いものから消す (§5.15)
def _cleanup(settings):
    cfg = _library_cfg(settings)
    limit = max(int(cfg["thumbnail_cache_limit"]), 1) * max(
        int(cfg["thumbnail_count"]), 1)
    folder = cache_dir(settings)
    try:
        with os.scandir(folder) as entries:
            files = [(e.path, e.stat().st_mtime) for e in entries
                     if e.is_file() and e.name.endswith(".jpg")]
    except OSError:
        return
    if len(files) <= limit:
        return
    files.sort(key=lambda item: item[1])
    for path, _mtime in files[:len(files) - limit]:
        try:
            os.remove(path)
        except OSError:
            pass
