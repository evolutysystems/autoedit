# アーカイブ切り抜き用 Timeline の素材を元 VOD へ張り替える (ver5 resolve10 §3.1 / §5.2)
#
# アーカイブ用 Timeline が参照しているのは「実行の終わりに消えるクリップ単位の
# 中間ファイル」で、それを指したまま縦プロジェクトを保存しても後から開けない。
# そこで縦プロジェクトを作るときだけ、素材を**元 VOD 1 本**へ張り替える。
# 中間ファイルは VOD からのストリームコピーなので、時刻を足し直すだけで
# 同じ絵になる。切り出しもエンコードも行わない (resolve9 §4-2 と同じ方針)。
#
#     VOD の秒 = 切り出しの原点 + 素材内の秒
#
# 足す値は vod_start **ではない** (最大 1 GOP ≒ 2 秒ずれる)。
# 原点の求め方は archive/cut_origin.py に集約してある。
import os

from ..exceptions import InputError
from ..i18n import tr
from ..timeline import crop, media_probe, project_io
from ..timeline.builder import timeline_config
from ..utils.logger import get_logger
from . import cut_origin
from . import project_resume
from . import timeline_builder as archive_timeline

_logger = get_logger(__name__)

# 張り替え先の素材 ID (縦プロジェクト内で 1 本だけ作る)
MEDIA_ID = "vod"


# 縦プロジェクト用の素材張り替え指定を作る
#   timeline : アーカイブ切り抜き用の Timeline
#   settings : setting.json
#   clips    : 対象のベースクリップ (省略すると V1 の全クリップ)
# 戻り値: vertical_builder.build(rebase=...) へ渡す辞書 / アーカイブ用でなければ None
# 例外  : 元 VOD が見つからない場合 InputError
def plan(timeline, settings, clips=None):
    if project_io.project_kind(timeline) != project_io.KIND_ARCHIVE:
        return None

    vod = project_resume.recorded_vod_path(timeline)
    if not vod or not os.path.exists(vod):
        raise InputError(
            tr("元の配信アーカイブ（VOD）が見つかりません。\n{path}\n"
               "縦動画プロジェクトは元 VOD を参照するため、"
               "先に VOD を戻してください。", path=vod))

    ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
    window_sec = crop.config(settings or {})["keyframe_window_sec"]
    offsets = {}
    unknown = 0
    for media_id in _media_ids(timeline, clips):
        entry = archive_timeline.archive_media_entry(timeline, media_id)
        if entry is None:
            # VOD 由来でない素材 (D&D で足した動画・画像など)。元のまま残す。
            unknown += 1
            continue
        offsets[media_id] = cut_origin.measure(
            vod, float(entry.get("vod_start", 0.0)), ffmpeg_cfg, window_sec)

    if not offsets:
        return None

    cfg = timeline_config(settings)
    media = media_probe.probe(vod, MEDIA_ID, settings, cfg["media"])

    warnings = []
    if unknown:
        warnings.append(
            f"元 VOD から作られていない素材 {unknown} 件は、そのまま参照しています。")
    if _has_normalized(timeline, offsets):
        warnings.append(
            "音量の正規化は引き継ぎません (元 VOD をそのまま参照するため)。")

    _logger.info("縦プロジェクトの素材を元 VOD へ張り替えます: %s (素材 %d 本)",
                 vod, len(offsets))
    return {
        "media": media,
        "offsets": offsets,
        "source": {
            "input_path": vod,
            "media_path": vod,
            # 中間ファイルではなく元動画そのものを指す。media_recovery が
            # 「正規化をやり直す」経路へ入らないようにする (resolve7 §3-5 判定 3)。
            "media_role": "source",
        },
        "warnings": warnings,
    }


# 対象クリップが参照している素材 ID (指定が無ければ V1 の全クリップ)
def _media_ids(timeline, clips):
    if clips is None:
        track = timeline.base_video_track()
        clips = list(track.clips) if track is not None else []
    seen = []
    for clip in clips:
        media_id = str(getattr(clip, "media_id", "") or "")
        if media_id and media_id not in seen:
            seen.append(media_id)
    return seen


# 張り替える素材のどれかが正規化済みだったか (音量の注意を出すかの判断)
def _has_normalized(timeline, offsets):
    for media_id in offsets:
        entry = archive_timeline.archive_media_entry(timeline, media_id)
        if entry is not None and str(entry.get("media_role", "normalized")) == "normalized":
            return True
    return False


__all__ = ["MEDIA_ID", "plan"]
