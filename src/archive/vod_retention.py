# 取得した VOD の寿命管理 (docs/error/fromUser/resolve.md §6-6)
#
# 背景:
#   取得した VOD (vod_<id>.mp4 / 1 本 10〜25 GB) は、従来 **誰のものでもなかった**。
#   作業フォルダに置かれたまま誰も消さず、開発端末では 45 GB が 2 か月滞留していた。
#   残っていた理由は「アーカイブ用プロジェクトを開き直すとき、映像を元 VOD から
#   切り直していた」ためで、保存の既定が音声だけ (keep_audio) だったことによる。
#
# 方針 (利用者の提案):
#   「続きから編集するためのものは 1 つのフォルダに、動画も JSON もまとめて置く」
#   プロジェクトが自己完結していれば元 VOD は要らない。要らなくなったら捨てる。
#
# ここでの判定:
#   vod_<id>.mp4 は、**それを元に作られた自己完結プロジェクトが在る**ときに不要とみなす。
#   自己完結 = <プロジェクト>.media/ に映像素材が在る (音声サイドカーだけでは不足)。
#   不要と判定したものはゴミ箱へ送る。完全削除はしない (取り消せる形で消す)。
import os
import re

from ..timeline import project_io
from ..timeline.builder import timeline_config
from ..utils import trash
from ..utils.logger import get_logger
from .config import download_config, resolve_work_dir, source_usage

_logger = get_logger(__name__)

# 取得した VOD のファイル名 (vod_<id>.mp4)
_VOD_NAME_RE = re.compile(r"^vod_(\d+)\.mp4$")

# 映像素材とみなす拡張子 (音声サイドカー .m4a / .aac は含めない)
_VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".avi")


# VOD に対応するプロジェクトファイルのパスを返す (保存されていなくてもパスは返す)。
# 保存名は archive_timeline_dialog._default_project_path と同じ決まりにする:
#   <プロジェクト置き場>/<VOD名><archive_suffix><project_suffix>
def project_path_for(vod_path, settings):
    cfg = timeline_config(settings)
    return project_io.default_project_path(
        settings, vod_path, name_suffix=cfg["project"]["archive_suffix"])


# プロジェクトが自己完結しているか (元 VOD 無しで開けるか) を判定する。
# <プロジェクト>.media/ に映像ファイルが 1 つでも在れば自己完結とみなす。
# 音声サイドカーしか無い場合は、開くときに元 VOD から切り直すため**不足**である。
def is_self_contained(project_path, settings):
    if not project_path or not os.path.isfile(project_path):
        return False
    cfg = timeline_config(settings)
    media_dir = project_io.media_dir_path(
        project_path, cfg["project_suffix"], cfg["project"]["media_dir_suffix"])
    if not media_dir or not os.path.isdir(media_dir):
        return False
    try:
        names = os.listdir(media_dir)
    except OSError:
        return False
    return any(name.lower().endswith(_VIDEO_SUFFIXES) for name in names)


# 不要になった VOD を探す。
# 戻り値: [{"path", "project", "bytes"}…] (ゴミ箱へ送ってよいもの)
def find_unneeded(settings):
    work_dir = resolve_work_dir(settings)
    found = []
    for path in source_usage(settings)["paths"]:
        name = os.path.basename(path)
        if not _VOD_NAME_RE.match(name):
            continue                    # チャット JSON は対象外 (小さく、採点に要る)
        project = project_path_for(os.path.join(work_dir, name), settings)
        if not is_self_contained(project, settings):
            continue                    # まだこの VOD が要る
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        found.append({"path": path, "project": project, "bytes": size})
    return found


# 不要になった VOD をゴミ箱へ送る。
# keep_vod=True (設定 archive.download.keep_vod) のときは何もしない。
# ゴミ箱を使えない環境でも何もしない (黙って完全削除はしない)。
# 戻り値: 回収したバイト数。
def sweep_unneeded(settings):
    if download_config(settings)["keep_vod"]:
        return 0
    targets = find_unneeded(settings)
    if not targets:
        return 0
    if not trash.is_available():
        _logger.info("ゴミ箱を使えないため元 VOD の整理は行いません (%d 件)", len(targets))
        return 0

    sizes = {item["path"]: item["bytes"] for item in targets}
    result = trash.send_to_trash([item["path"] for item in targets])
    freed = sum(sizes.get(path, 0) for path in result["trashed"])
    for path in result["trashed"]:
        _logger.info("プロジェクトが自己完結したため元 VOD をゴミ箱へ送りました: %s",
                     os.path.basename(path))
    for item in result["failed"]:
        _logger.warning("元 VOD をゴミ箱へ送れませんでした: %s (%s)",
                        item["path"], item["reason"])
    if freed:
        _logger.info("元 VOD の整理で %.1f GB を回収しました", freed / (1024 ** 3))
    return freed
