# 元 VOD の一部区間だけを取り直して「切り出しに使える素材」を用意する (ver9 resolve5)
#
# 元 VOD は既定 (archive.download.keep_vod=false) でプロジェクトが自己完結した時点で
# ゴミ箱へ送る。1 本 10〜25GB を利用者に抱えさせないためである。その代わり
# 「後から区間を足す」(セクション追加) が使えなくなっていたため、
# **要る区間だけを取り直す**経路をここに用意する。180 秒で約 50MB で済む。
#
# 戻すのは素材のパスだけでは足りない。セクションの管理は「元 VOD の絶対秒」
# (source.archive.clips[].vod_start) で行うため、**その素材の 0 秒が VOD の何秒目か**を
# 誤差なく添えなければ、切り出し位置がそのままずれる。
#
# 位置の決め方 (resolve5 §2.3〜§2.5):
#   twitch-dl のキャッシュには取得範囲に関わらず **VOD 全体のプレイリスト**が残り、
#   セグメントの保存名は**全体プレイリストでの並び順** ({index:05d}.ts) である。
#   よって「取得できた先頭セグメント番号 N」から EXTINF を N 件ぶん足せば原点が決まる。
#   この方式の位置誤差は実測 ±0.0001 秒 (VOD 前半・後半とも / 音声相関で確認)。
#
# twitch-dl 自身の --start/--end による切り詰めは使わない。ffmpeg へ整数秒で渡され、
# しかも int() で切り捨てられるため末尾が最大 1 秒欠ける (resolve5 §2.2)。
import math
import os
import re

from ..exceptions import InputError, TwitchError
from ..i18n import tr
from ..modules import ffmpeg_runner
from ..utils import disk
from ..utils.logger import get_logger
from . import config
from . import twitch_source

_logger = get_logger(__name__)

# これ以下の区間は取りに行かない (切り出しても素材にならない)
_MIN_RANGE_SEC = 0.05

# 取得素材が指定区間を覆っているかの許容誤差 (秒)。
# セグメント単位で余裕を持って取るため本来ゼロで足りるが、
# 連結後の実測尺はコンテナ化で数十ミリ秒前後するため、その幅だけ見逃す。
_COVERAGE_TOLERANCE_SEC = 0.5

# EXTINF 累積と、プレイリストが宣言する総尺の許容差 (秒)。
# ここが食い違うと「EXTINF を足して位置を決める」前提そのものが崩れるため、
# 黙って進めず失敗させる。実測では完全一致した (8991.436 対 8991.436)。
_TOTAL_TOLERANCE_SEC = 2.0

# 1MB
_MB = 1024 * 1024

_EXTINF_RE = re.compile(r"^#EXTINF:\s*([0-9.]+)")
_TOTAL_SECS_RE = re.compile(r"^#EXT-X-TWITCH-TOTAL-SECS:\s*([0-9.]+)")


# ------------------------------------------------------------------
# プレイリストの読み取り (位置計算の土台)
# ------------------------------------------------------------------

# 全体プレイリストを読み、セグメントの開始秒を並び順で返す (resolve5 §2.3)。
#
# 要素番号がそのままセグメントの保存名の番号になる
# (twitch-dl は全体プレイリストでの並び順を {index:05d} で付けるため)。
# 戻り値: ([開始秒, …], 合計秒)
def parse_segment_starts(text):
    starts = []
    acc = 0.0
    duration = None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        found = _EXTINF_RE.match(line)
        if found:
            duration = float(found.group(1))
            continue
        if line.startswith("#"):
            continue
        if duration is None:
            continue            # EXTINF を伴わない URI 行は数に入れない
        starts.append(acc)
        acc += duration
        duration = None
    return starts, acc


# プレイリストが宣言する総尺 (#EXT-X-TWITCH-TOTAL-SECS)。無ければ None。
def declared_total_sec(text):
    for line in (text or "").splitlines():
        found = _TOTAL_SECS_RE.match(line.strip())
        if found:
            return float(found.group(1))
    return None


# セグメント番号 index の開始秒を返す。表に無い番号は失敗させる
# (位置が分からない素材を「0 秒から」として扱うと、ずれたまま挿入されてしまう)。
def segment_origin(starts, index):
    if index is None or index < 0 or index >= len(starts):
        raise TwitchError(
            tr("取得した区間の位置を特定できませんでした"
               "（セグメント {index} / プレイリスト {count} 件）。",
               index=index, count=len(starts)))
    return float(starts[index])


# セグメントが連番で揃っているかを確かめる。
# 欠番は取得が不完全な証拠であり、そのまま連結すると
# 「見た目は繋がっているのに中身が飛ぶ」素材になる。
def verify_continuous(segments):
    indexes = []
    for name in segments:
        index = twitch_source.segment_index(name)
        if index is None:
            raise TwitchError(
                tr("取得した区間に読めないファイルが混ざっています: {name}", name=name))
        indexes.append(index)
    for previous, current in zip(indexes, indexes[1:]):
        if current != previous + 1:
            raise TwitchError(
                tr("取得した区間が途切れています（{previous} の次が {current}）。\n"
                   "もう一度お試しください。", previous=previous, current=current))
    return indexes


# 取得した素材が指定区間を本当に覆っているかを確かめる (resolve5 §4.1 ⑥)
def verify_coverage(origin_sec, duration_sec, start_sec, end_sec):
    available_end = origin_sec + duration_sec
    if (start_sec < origin_sec - _COVERAGE_TOLERANCE_SEC
            or end_sec > available_end + _COVERAGE_TOLERANCE_SEC):
        raise TwitchError(
            tr("取得できた区間が指定範囲に足りません"
               "（取得 {got_start:.1f}→{got_end:.1f} 秒 / 指定 {start:.1f}→{end:.1f} 秒）。",
               got_start=origin_sec, got_end=available_end,
               start=start_sec, end=end_sec))


# EXTINF 累積と宣言された総尺を突き合わせる。食い違えば位置計算の前提が崩れている。
def verify_total(text, total_sec):
    declared = declared_total_sec(text)
    if declared is None:
        return                  # 宣言が無いプレイリストは突き合わせない
    if abs(declared - total_sec) > _TOTAL_TOLERANCE_SEC:
        raise TwitchError(
            tr("元動画のプレイリストが読めません"
               "（区間の合計 {total:.1f} 秒に対し全長 {declared:.1f} 秒）。",
               total=total_sec, declared=declared))


# ------------------------------------------------------------------
# 取得
# ------------------------------------------------------------------

# twitch-dl へ渡す取得範囲を決める (resolve5 §4.1 ①)。
# --start/--end は整数秒しか受けないため、開始は切り捨て、終了は切り上げる。
# 戻り値: (開始秒, 終了秒) いずれも整数
def plan_window(start_sec, end_sec, pad_sec, vod_duration_sec=0.0):
    window_start = max(int(math.floor(float(start_sec) - float(pad_sec))), 0)
    window_end = int(math.ceil(float(end_sec) + float(pad_sec)))
    duration = float(vod_duration_sec or 0.0)
    if duration > 0:
        window_end = min(window_end, int(math.ceil(duration)))
    if window_end <= window_start:
        # twitch-dl は end <= start を受け付けない
        window_end = window_start + 1
    return window_start, window_end


# 秒を twitch-dl の --start/--end が受ける "H:MM:SS" へ直す。
# 分・秒は 0〜59 でなければ弾かれるため、必ず 3 つ組で渡す。
def to_hms(seconds):
    total = max(int(seconds), 0)
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


# 取得にあたって確かめる空き容量 (バイト)。
# セグメントと連結後の 2 本ぶんを見込む。
def required_bytes(window_sec, mb_per_min):
    minutes = max(float(window_sec), 0.0) / 60.0
    return int(minutes * float(mb_per_min) * _MB * 2)


# 指定区間を含む素材を用意する (resolve5 §4.1)
#
# video_id         : Twitch の動画 ID
# start_sec/end_sec: 要求区間 (元 VOD の絶対秒)
# workdir          : 置き場。画面の clip_workdir (TemporaryDirectory) を渡すため、
#                    画面を閉じれば消える = 容量を抱え続けない
# tag              : 区間ごとに分けるための名前 (前回の残りを拾わないため)
# cancel_cb        : 中止を問い合わせる callable (取得中の中断に使う / §4.7)
# vod_duration_sec : 元 VOD の全長 (分かっていれば終端の丸めに使う / 0 なら使わない)
#
# 戻り値:
#   {"path"       : 連結済み mp4 の絶対パス
#    "origin_sec" : path の 0 秒が VOD の何秒目か (EXTINF 累積で確定)
#    "end_sec"    : origin_sec + 実測 duration
#    "quality"    : 実際に使った画質名
#    "bytes"      : 取得量
#    "segments"   : セグメント数}
def fetch_range(video_id, start_sec, end_sec, workdir, settings,
                quality="source", auth_token="", ffmpeg_dir=None,
                progress_cb=None, cancel_cb=None, tag="", vod_duration_sec=0.0):
    start = max(float(start_sec), 0.0)
    end = float(end_sec)
    if end - start <= _MIN_RANGE_SEC:
        raise InputError(tr("取得する区間が短すぎます。"))

    section_cfg = config.section_add_config(settings)
    cfg = section_cfg["refetch"]
    if not cfg["enabled"]:
        raise InputError(
            tr("元動画の区間の取り直しは設定で無効になっています。"))

    download_cfg = config.download_config(settings)
    ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
    # イントロカードの素はクリップ開始の buffer_sec 秒**手前**から作るため、
    # その手前ぶんも取得範囲へ含める (resolve5 §5.3)。
    pad = max(cfg["pad_sec"],
              float(config.intro_card_config(settings)["buffer_sec"]))
    window_start, window_end = plan_window(start, end, pad, vod_duration_sec)

    # 取得前に空きを確かめる (download_vod と同じ作法)
    disk.ensure_free(workdir,
                     required_bytes(window_end - window_start,
                                    cfg["min_free_mb_per_min"]),
                     label="区間取得")

    cache_root = os.path.join(workdir, f"vod_range_{tag or 'x'}")
    os.makedirs(cache_root, exist_ok=True)
    _logger.info("区間取得: %s %d→%d 秒 (要求 %.1f→%.1f / 余裕 %.1fs / 画質 %s)",
                 video_id, window_start, window_end, start, end, pad, quality)

    fetched = twitch_source.download_vod_range(
        video_id, cache_root, to_hms(window_start), to_hms(window_end),
        twitch_dl_path=download_cfg["twitch_dl_path"], quality=quality,
        auth_token=auth_token, progress_cb=progress_cb, ffmpeg_dir=ffmpeg_dir,
        quality_fallback=cfg["allow_quality_fallback"],
        keep_cache_on_error=download_cfg["keep_cache_on_error"],
        cancel_cb=cancel_cb)

    # 位置の確定 (ここが本モジュールの要点)
    with open(fetched["playlist"], "r", encoding="utf-8", errors="replace") as handle:
        playlist_text = handle.read()
    starts, total = parse_segment_starts(playlist_text)
    verify_total(playlist_text, total)
    indexes = verify_continuous(fetched["segments"])
    origin = segment_origin(starts, indexes[0])

    if progress_cb:
        progress_cb(tr("取得した区間をつなぎ合わせています…"))
    dest = os.path.join(cache_root, "range.mp4")
    concat_segments(fetched["segment_dir"], fetched["segments"], dest, ffmpeg_cfg)
    duration = float(ffmpeg_runner.probe_duration(dest, ffmpeg_cfg) or 0.0)
    if duration <= 0:
        raise TwitchError(tr("取得した区間をつなぎ合わせられませんでした。"))
    verify_coverage(origin, duration, start, end)

    size = _size_or_zero(dest)
    _logger.info(
        "区間取得の素材を用意しました: %s (%s / 原点 %.3f 秒 / 尺 %.3f 秒 / %d セグメント)",
        dest, disk.human(size), origin, duration, len(fetched["segments"]))
    return {"path": dest, "origin_sec": origin, "end_sec": origin + duration,
            "quality": fetched["quality"], "bytes": size,
            "segments": len(fetched["segments"])}


# セグメントを 1 本の mp4 へ連結する (再エンコードなし)。
#
# concat デマルチプレクサのリストは行頭 file + 単一引用符で書く。
# Windows の \ はリスト内でエスケープとして解釈されるため / へ直す。
# 先頭を 0 秒に揃えるため -avoid_negative_ts make_zero を付ける
# (cut_region と同じ作法。原点は呼び側が origin_sec で持つ)。
def concat_segments(segment_dir, segments, dest, ffmpeg_cfg):
    list_path = os.path.join(os.path.dirname(dest), "concat.txt")
    with open(list_path, "w", encoding="utf-8") as handle:
        for name in segments:
            path = os.path.join(segment_dir, name).replace("\\", "/")
            handle.write(f"file '{path}'\n")
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        dest,
    ]
    ffmpeg_runner.execute(cmd)
    return dest


def _size_or_zero(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


__all__ = [
    "concat_segments",
    "declared_total_sec",
    "fetch_range",
    "parse_segment_starts",
    "plan_window",
    "required_bytes",
    "segment_origin",
    "to_hms",
    "verify_continuous",
    "verify_coverage",
    "verify_total",
]
