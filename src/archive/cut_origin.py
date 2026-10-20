# 切り出し素材の先頭が元 VOD 上の何秒かを測る (素材 → VOD の座標変換の起点)
#
# clip_writer.cut_region は
#     ffmpeg -ss <vod_start> -i <vod> -t <len> -c copy -avoid_negative_ts make_zero
# で区間を切り出す。ストリームコピーではシーク点と要求位置の間を捨てられないため、
# 素材の先頭は要求位置ではなく **その手前** になる (Twitch VOD なら最大 1 GOP ≒ 2 秒)。
# make_zero は全ストリームを同じ量だけずらして最小 PTS を 0 にするので、
#
#     VOD の秒 = 素材内の秒 + (シーク後に最初に出てくるパケットの PTS の最小値)
#
# が成り立つ。この右辺 (= 切り出しの原点) を実測して返すのがこのモジュール。
#
# 無音カットも文字起こしもこの素材の上で行うため、素材の時刻を元 VOD へ戻す経路
# (Resolve 出力 / 縦プロジェクトの素材張り替え) は必ずここを通すこと。
# vod_start をそのまま足すと最大 1 GOP ずれる。
#
# 測り方は 2 段構え。どちらも失敗したら vod_start をそのまま返し、処理は止めない。
#   ① パケット実測 (正確): ffprobe -read_intervals は ffmpeg -ss と同じシークを行うため、
#      シーク直後のパケットの PTS をそのまま読める。映像だけでなく音声も見る点が重要で、
#      mp4 のインターリーブ次第で音声パケットは映像キーフレームより少し前から始まる。
#   ② キーフレーム位置 (近似): ①が使えない環境向け。映像キーフレームしか見ないため、
#      ①より最大で数フレームぶん後ろの値になる。
import subprocess

from ..modules import ffmpeg_runner
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags

_logger = get_logger(__name__)

# キーフレームを探しに戻る幅 (秒) の既定。これより前は測らない (= 想定外として捨てる)。
KEYFRAME_WINDOW_SEC = 30.0

# pts_time の比較に使う許容 (秒)
_EPS = 0.001

# パケット実測で読み取る長さ (秒)。先頭のパケットが取れれば足りる。
_PROBE_SEC = 0.3


# cut_region が切り出した素材の先頭に当たる VOD 上の時刻を返す
#   vod_path   : 元 VOD
#   vod_start  : cut_region に渡した開始位置 (秒)
#   ffmpeg_cfg : setting.json の ffmpeg セクション
#   window_sec : 戻って探す上限 (秒)。None なら KEYFRAME_WINDOW_SEC
# 測れない場合は vod_start をそのまま返す (ズレは残るが出力は止めない)。
def measure(vod_path, vod_start, ffmpeg_cfg, window_sec=None):
    position = max(float(vod_start or 0.0), 0.0)
    if position <= _EPS:
        return 0.0
    window = float(window_sec if window_sec is not None else KEYFRAME_WINDOW_SEC)
    ffprobe = ffmpeg_runner.get_ffprobe_exe(ffmpeg_cfg or {})

    origin = _first_packet_at_or_before(ffprobe, vod_path, position, window)
    if origin is not None:
        if position - origin > _EPS:
            _logger.info("切り出しの原点は %.3fs でした (要求 %.3fs / ずれ %.3fs)",
                         origin, position, position - origin)
        return origin

    origin = _keyframe_at_or_before(ffprobe, vod_path, position, window)
    if origin is not None:
        _logger.warning(
            "切り出しの原点をパケットから測れないため映像キーフレームで代用します: "
            "%.3fs (要求 %.3fs)", origin, position)
        return origin

    _logger.warning("切り出しの原点を測れませんでした (%.3fs をそのまま使います)", position)
    return position


# シーク直後に現れるパケットの PTS の最小値を返す (測れなければ None)
#
# ffprobe -read_intervals は ffmpeg -ss と同じ avformat_seek_file を使うため、
# cut_region が実際に書き出す先頭と同じ位置が得られる。全ストリームを対象にする。
def _first_packet_at_or_before(ffprobe, vod_path, position, window):
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "packet=stream_index,pts_time",
        "-read_intervals", f"{position:.3f}%+{_PROBE_SEC}",
        "-of", "csv=p=0", vod_path,
    ]
    lines = _run(cmd, position)
    if lines is None:
        return None

    best = None
    for line in lines:
        _index, _, pts = line.strip().partition(",")
        try:
            value = float(pts)
        except ValueError:
            continue
        if best is None or value < best:
            best = value
    return _accept(best, position, window)


# position 以前で最も近い映像キーフレームの時刻を返す (測れなければ None)
#
# ffprobe はパケットのフラグだけを見るためデコードは走らない
# (5 時間の VOD でも 0.1 秒程度)。
def _keyframe_at_or_before(ffprobe, vod_path, position, window):
    begin = max(position - window, 0.0)
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_packets", "-show_entries", "packet=pts_time,flags",
        "-read_intervals", f"{begin:.3f}%{position + _EPS:.3f}",
        "-of", "csv=p=0", vod_path,
    ]
    lines = _run(cmd, position)
    if lines is None:
        return None

    best = None
    for line in lines:
        pts, _, flags = line.strip().partition(",")
        if "K" not in flags:
            continue
        try:
            value = float(pts)
        except ValueError:
            continue
        if value <= position + _EPS and (best is None or value > best):
            best = value
    return _accept(best, position, window)


# ffprobe を実行して出力行を返す (失敗は None。例外は出さない)
def _run(cmd, position):
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=no_window_creationflags())
    except (OSError, ValueError) as e:
        _logger.debug("切り出し原点の測定に失敗 (%.3fs): %s", position, e)
        return None
    if result.returncode != 0:
        _logger.debug("切り出し原点の測定に失敗 (%.3fs): rc=%s", position, result.returncode)
        return None
    return (result.stdout or "").splitlines()


# 測定値が妥当か見る (要求位置より後 / 戻りすぎ / 負値は採用しない)
def _accept(value, position, window):
    if value is None:
        return None
    if value < 0.0 or value > position + _EPS:
        return None
    if position - value > window + _EPS:
        return None
    return value


__all__ = ["KEYFRAME_WINDOW_SEC", "measure"]
