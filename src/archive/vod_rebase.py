# アーカイブ切り抜き用 Timeline の素材を元 VOD へ張り替える (ver5 resolve10 §3.1 / §5.2)
#
# アーカイブ用 Timeline が参照しているのは「実行の終わりに消えるクリップ単位の
# 中間ファイル」で、それを指したまま縦プロジェクトを保存しても後から開けない。
# そこで縦プロジェクトを作るときだけ、素材を**元 VOD 1 本**へ張り替える。
# 中間ファイルは VOD からのストリームコピーなので、時刻を足し直すだけで
# 同じ絵になる。切り出しもエンコードも行わない (resolve9 §4-2 と同じ方針)。
#
#     VOD の秒 = 切り出しの先頭 (キーフレーム) + 素材内の秒
#
# 足す値は vod_start **ではない**。clip_writer.cut_region は
# "-ss <start> -i <vod> -c copy" で切り出しており、FFmpeg はストリームコピーでは
# シーク点と要求位置の間を捨てない (accurate_seek が効かない) ため、
# 中間ファイルの先頭は **要求位置の手前のキーフレーム**になる。
# vod_start をそのまま足すと最大 1 GOP (Twitch なら約 2 秒) ずれるので、
# ここで VOD のキーフレーム位置を実測して足す。
import os
import subprocess

from ..exceptions import InputError
from ..i18n import tr
from ..modules import ffmpeg_runner
from ..timeline import crop, media_probe, project_io
from ..timeline.builder import timeline_config
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from . import project_resume
from . import timeline_builder as archive_timeline

_logger = get_logger(__name__)

# 張り替え先の素材 ID (縦プロジェクト内で 1 本だけ作る)
MEDIA_ID = "vod"

# キーフレームを探しに戻る幅 (秒) の既定。設定 vertical.crop.keyframe_window_sec で変えられる。
_KEYFRAME_WINDOW_SEC = 30.0

# pts_time の比較に使う許容 (秒)
_EPS = 0.001


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
        offsets[media_id] = _keyframe_at_or_before(
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


# position 以前で最も近いキーフレームの時刻を返す (測れなければ position をそのまま返す)
#
# cut_region が実際に切り出した先頭がこの位置である。ffprobe はパケットの
# フラグだけを見るため、デコードは走らない (5 時間の VOD でも 0.1 秒程度)。
def _keyframe_at_or_before(vod_path, position, ffmpeg_cfg, window_sec=_KEYFRAME_WINDOW_SEC):
    position = max(float(position), 0.0)
    if position <= _EPS:
        return 0.0

    begin = max(position - float(window_sec), 0.0)
    ffprobe = ffmpeg_runner.get_ffprobe_exe(ffmpeg_cfg or {})
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_packets", "-show_entries", "packet=pts_time,flags",
        "-read_intervals", f"{begin:.3f}%{position + _EPS:.3f}",
        "-of", "csv=p=0", vod_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=no_window_creationflags())
    except (OSError, ValueError) as e:
        _logger.warning("キーフレーム位置を測れませんでした (%s を使います): %s", position, e)
        return position
    if result.returncode != 0:
        _logger.warning("キーフレーム位置を測れませんでした (%s を使います)", position)
        return position

    best = None
    for line in (result.stdout or "").splitlines():
        pts, _, flags = line.strip().partition(",")
        if "K" not in flags:
            continue
        try:
            value = float(pts)
        except ValueError:
            continue
        if value <= position + _EPS and (best is None or value > best):
            best = value
    if best is None:
        _logger.warning("%s 秒の手前にキーフレームが見つかりませんでした (そのまま使います)",
                        position)
        return position
    if position - best > _EPS:
        _logger.info("切り出しの先頭は %.3fs でした (要求 %.3fs / ずれ %.3fs)",
                     best, position, position - best)
    return best


__all__ = ["MEDIA_ID", "plan"]
