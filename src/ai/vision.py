# レビュー用のフレーム抜き出し (docs/AiEditor/resolve1.md §12.5 / 回答 Q5)
#
# Claude は画像を受け取れるが**動画は送れない**。シーンごとに静止画を抜いて送る。
#   ・送るのはフレーム画像だけ。**音声は送らない** (音の問題は機械の測定で拾う / §12.2)
#   ・枚数と大きさは設定で絞る。**トークンは面積に比例する**
#   ・抜いた画像は作業フォルダへ一時出力し、**送信用に読み込んだら消す**
#
# 抜く元は**元 VOD** である (設計 §12.5 は「Timeline から」としていたが、レビューは
# 適用前の EditPlan に対して行うため Timeline がまだ無い。縦の crop を当てた絵は
# 作れないので、その点だけは見られない / §12.5 の「クロップで人物が切れている」は
# 当面この経路では拾えない)。
#
# ffmpeg は 1 枚書き出すだけに使う。抜き方は差し替えられる (テストは ffmpeg を呼ばない)。
import base64
import os
import re
import subprocess

from ..modules import ffmpeg_runner
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from . import config as ai_config
from .provider import AiImage

_logger = get_logger(__name__)

# 抜く位置 (ai.vision.points)
POINT_HEAD = "head"
POINT_MIDDLE = "middle"
POINT_PEAK = "peak"
POINTS = (POINT_PEAK, POINT_HEAD, POINT_MIDDLE)

# 先頭ちょうどは暗転・切り替わりに当たりやすいので少し中へ入る (秒)
_HEAD_OFFSET_SEC = 0.5

_FRAME_NAME = "ai_frame{index}.jpg"
_MEDIA_TYPE = "image/jpeg"


# EditPlan のシーンからフレームを抜いて AiImage のリストを返す。
#
# plan      : EditPlan (検証済み)
# scenes    : RankedScene のリスト (peak を使うため。無くてもよい)
# workdir   : 一時出力先
# per_scene : 1 シーンあたりの枚数 (省略時は ai.vision.frames_per_scene)
# extract   : extract(input_path, at_sec, dest, max_edge_px, ffmpeg_cfg) -> bool
def frames_for_plan(input_path, plan, settings, scenes=None, workdir="",
                    per_scene=0, extract=None, ffmpeg_cfg=None):
    cfg = ai_config.ai_config(settings)["vision"]
    if not cfg.get("enabled", True):
        return []
    if not input_path or not os.path.exists(input_path):
        _logger.info("元動画が無いためフレームを送りません: %s", input_path)

        return []

    count = int(per_scene or cfg.get("frames_per_scene") or 1)
    limit = max(int(cfg.get("max_frames") or 0), 0)
    edge = max(int(cfg.get("max_edge_px") or 0), 16)
    points = [point for point in (cfg.get("points") or POINTS) if point in POINTS]
    peaks = {scene.index: scene.peak for scene in scenes or []}

    times = _frame_times(plan, points, count, peaks)
    if limit and len(times) > limit:
        times = times[:limit]
    if not times:
        return []

    take = extract if extract is not None else _extract_with_ffmpeg
    ffmpeg = ffmpeg_cfg if ffmpeg_cfg is not None else (settings or {}).get("ffmpeg", {})
    folder = workdir or os.path.dirname(os.path.abspath(input_path))
    os.makedirs(folder, exist_ok=True)

    images = []
    for index, at in enumerate(times):
        dest = os.path.join(folder, _FRAME_NAME.format(index=index))
        try:
            if not take(input_path, at, dest, edge, ffmpeg):
                continue
            with open(dest, "rb") as handle:
                data = handle.read()
        except Exception as e:  # noqa: BLE001 (フレームが撮れなくてもレビューは続ける)
            _logger.warning("フレーム %.1fs を抜けませんでした: %s", at, e)
            continue
        finally:
            # 送信用に読んだら消す (作業フォルダへ溜めない / §12.5)。
            _remove(dest)
        if not data:
            continue
        images.append(AiImage(media_type=_MEDIA_TYPE,
                              data_base64=base64.b64encode(data).decode("ascii")))

    _logger.info("レビュー用のフレーム: %d 枚 (長辺 %dpx)", len(images), edge)

    return images


# どの時刻を抜くか。シーンの並び順に、points の順で詰める。
def _frame_times(plan, points, per_scene, peaks):
    times = []
    for scene in plan.get("scenes") or []:
        start = float(scene.get("source_start", 0.0) or 0.0)
        end = float(scene.get("source_end", 0.0) or 0.0)
        if end <= start:
            continue
        chosen = []
        for point in points:
            if len(chosen) >= max(per_scene, 1):
                break
            at = _time_of(point, scene, start, end, peaks)
            if at is None:
                continue
            if all(abs(at - already) > 0.2 for already in chosen):
                chosen.append(at)
        times.extend(chosen)

    return times


def _time_of(point, scene, start, end, peaks):
    if point == POINT_PEAK:
        peak = peaks.get(_scene_ref(scene))
        if peak is None or not (start <= float(peak) <= end):
            return None

        return round(float(peak), 3)
    if point == POINT_HEAD:
        return round(min(start + _HEAD_OFFSET_SEC, end), 3)

    return round((start + end) / 2.0, 3)


# EditPlan のシーンから候補 index を推測する (peak を引くため)。
# id が "sc3" のような形なら数字を使い、無ければ order を使う。
def _scene_ref(scene):
    text = str(scene.get("id", "") or "")
    digits = "".join(char for char in text if char.isdigit())
    if digits:
        return int(digits)

    return int(scene.get("order", 0) or 0)


# ffmpeg で 1 枚だけ書き出す (長辺を max_edge_px へ縮める)。
def _extract_with_ffmpeg(input_path, at_sec, dest, max_edge_px, ffmpeg_cfg):
    from ..modules import ffmpeg_runner

    exe = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg or {})
    scale = ("scale='if(gt(iw,ih),{edge},-2)':'if(gt(iw,ih),-2,{edge})'"
             .format(edge=int(max_edge_px)))
    command = [exe, "-y", "-ss", "{0:.3f}".format(float(at_sec)), "-i", input_path,
               "-frames:v", "1", "-vf", scale, "-q:v", "4", dest]
    ffmpeg_runner.execute(command)

    return os.path.exists(dest) and os.path.getsize(dest) > 0


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        _logger.debug("フレームを消せませんでした: %s", path)


# ------------------------------------------------------------------
# 採点スイープ用のフレーム抽出 (docs/AiEditor/resolve2.md §7 / P1)
# ------------------------------------------------------------------
#
# 全長から等間隔でフレームを抜く。レビュー用の frames_for_plan と違い、
# **1 回のデコードで全部抜く**のが要点である。
#   ・frames_for_plan は 1 枚ごとに ffmpeg を起動して seek する。12 枚なら
#     問題ないが、1 時間の配信を 30 秒間隔で抜くと 200 枚を超え、起動と seek
#     だけで時間が溶ける。
#   ・-skip_frame nokey でキーフレームだけデコードする。Twitch VOD の
#     キーフレームは約 2 秒間隔なので 30 秒間隔には十分な粒度があり、
#     デコード量が大きく減る。
#
# 時刻は**連番から逆算してはいけない**。キーフレームの位置は等間隔ではないため、
# ffmpeg の showinfo が出す pts_time を読んで 1 枚ごとに添える
# (archive/cut_origin.py が解いているのと同じ「要求位置と実際の位置は違う」問題)。

_SCAN_NAME = "ai_scan%05d.jpg"

# showinfo が stderr へ出す 1 枚ぶんの行から pts_time を拾う
_RE_SHOWINFO_PTS = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


# 抜いたフレーム 1 枚。path は呼び出し側が読んだら消すこと。
class ScanFrame:

    def __init__(self, at_sec, path):
        self.at_sec = float(at_sec)
        self.path = str(path)

    def __repr__(self):
        return "ScanFrame(at={0:.3f}, path={1})".format(self.at_sec, self.path)


# 全長から interval_sec ごとにフレームを抜き、ScanFrame の一覧を返す。
#
# input_path   : 元 VOD
# workdir      : 書き出し先 (呼び出し側が作って片づける)
# settings     : setting.json (ai.scan / ffmpeg を見る)
# interval_sec : 抜く間隔 (秒)。省略時は ai.scan.frame_interval_sec
# max_frames   : 枚数の上限。省略時は ai.scan.max_frames (0 で無制限)
# edge_px      : 長辺の画素数。省略時は ai.scan.frame_edge_px
# run          : run(command) -> (returncode, stderr) の差し替え口 (テスト用)
#
# 1 枚も抜けなくても例外にしない (空を返す)。スイープを飛ばして機械採点で続けるため。
def frames_every(input_path, workdir, settings, interval_sec=0.0, max_frames=None,
                 edge_px=0, run=None):
    cfg = ai_config.ai_config(settings)["scan"]
    interval = float(interval_sec or cfg.get("frame_interval_sec") or 30.0)
    if interval <= 0:
        return []
    edge = max(int(edge_px or cfg.get("frame_edge_px") or 512), 16)
    limit = int(cfg.get("max_frames") or 0) if max_frames is None else int(max_frames)
    if not input_path or not os.path.exists(input_path):
        _logger.info("元動画が無いためフレームを抜けません: %s", input_path)

        return []
    os.makedirs(workdir, exist_ok=True)

    command = _scan_command(input_path, workdir, settings, interval, edge,
                            bool(cfg.get("fast_decode", True)))
    runner = run if run is not None else _run_capture
    try:
        code, stderr = runner(command)
    except Exception as e:  # noqa: BLE001 (抜けなくてもスイープを飛ばすだけ)
        _logger.warning("フレームの抽出に失敗しました: %s", e)

        return []
    if code != 0:
        _logger.warning("フレームの抽出に失敗しました (code=%s)", code)

        return []

    frames = _collect_frames(workdir, stderr)
    if limit > 0 and len(frames) > limit:
        _logger.info("フレームが上限 %d 枚を超えたため間引きます (%d 枚)", limit, len(frames))
        frames = _thin_out(frames, limit)
    _logger.info("スイープ用のフレーム: %d 枚 (間隔 %.0fs / 長辺 %dpx)",
                 len(frames), interval, edge)

    return frames


# 1 パスで抜くコマンド。select で間引き、showinfo で 1 枚ごとの pts_time を出させる。
# fast_decode=True ならキーフレームだけデコードする (-skip_frame nokey)。
def _scan_command(input_path, workdir, settings, interval, edge, fast_decode):
    ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
    exe = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    # fps フィルタは時刻を丸めた新しいフレームを作る。ここでは「元の絵をそのまま」
    # 欲しいので select で間引く (pts_time が元のまま残る)。
    # select の式に入れるカンマは \, で逃がす (フィルタの区切りと紛れるため)
    chain = (r"select='isnan(prev_selected_t)+gte(t-prev_selected_t\,{interval})'"
             ",scale='if(gt(iw,ih),{edge},-2)':'if(gt(iw,ih),-2,{edge})',showinfo"
             .format(interval=_ff_number(interval), edge=int(edge)))
    command = [exe, "-y"]
    if fast_decode:
        command += ["-skip_frame", "nokey"]
    command += ["-i", input_path, "-vf", chain, "-vsync", "0", "-q:v", "5",
                "-an", "-sn", os.path.join(workdir, _SCAN_NAME)]

    return command


# ffmpeg のフィルタ引数に入れる数値 (ロケールに依らない書式)
def _ff_number(value):
    return "{0:.3f}".format(float(value)).rstrip("0").rstrip(".")


# 書き出されたファイルと showinfo の pts_time を突き合わせる。
# showinfo は選ばれたフレームの順に 1 行ずつ出るため、連番と順序が一致する。
def _collect_frames(workdir, stderr):
    times = [float(m) for m in _RE_SHOWINFO_PTS.findall(str(stderr or ""))]
    names = sorted(name for name in os.listdir(workdir)
                   if name.startswith("ai_scan") and name.endswith(".jpg"))
    if len(times) < len(names):
        # 行が足りない = 取りこぼしている。先頭から順に対応させられないため、
        # **間違った時刻を付けるくらいなら捨てる** (時刻のずれは後段の
        # テロップずれと同じ事故になる / docs/AiEditor/resolve2.md §7.2)。
        _logger.warning("showinfo の行 %d 枚に対しファイルが %d 枚ありました。"
                        "時刻を対応させられないため抽出を破棄します",
                        len(times), len(names))
        for name in names:
            _remove(os.path.join(workdir, name))

        return []
    frames = []
    for index, name in enumerate(names):
        if index < len(times):
            at = times[index]
        else:
            # showinfo が読めなかったぶんは時刻不明として捨てる。
            # 連番から秒を逆算すると、キーフレーム位置のばらつきがそのまま
            # 時刻のずれになる (docs/AiEditor/resolve2.md §7.2)。
            _logger.warning("フレーム %s の時刻を読めませんでした (捨てます)", name)
            _remove(os.path.join(workdir, name))
            continue
        frames.append(ScanFrame(at, os.path.join(workdir, name)))
    if len(times) > len(names):
        _logger.info("showinfo の行 %d に対しファイルは %d 枚でした",
                     len(times), len(names))

    return frames


# 上限まで均等に間引く (端は必ず残す)。落とした絵はその場で消す。
def _thin_out(frames, limit):
    if limit <= 0 or len(frames) <= limit:
        return frames
    if limit == 1:
        keep_index = {0}
    else:
        # 端 (先頭と末尾) は必ず残す。配信の冒頭と終わりは
        # 掴みと締めの判断に要るため、間引きで落とさない。
        last = len(frames) - 1
        keep_index = {int(round(i * last / float(limit - 1)))
                      for i in range(limit)}
    kept = []
    for index, frame in enumerate(frames):
        if index in keep_index:
            kept.append(frame)
        else:
            _remove(frame.path)

    return kept


# ffmpeg を回して (returncode, stderr 全文) を返す。
#
# ffmpeg_runner.execute_capture は **stderr の末尾しか返さない** (エラー表示用)。
# showinfo の行は 1 枚につき 1 行出るため、末尾だけを読むと行とファイルの対応が
# 先頭からずれ、**間違った時刻が付く**。ここは全文が要るので自分で回す
# (archive/cut_origin.py と同じ作法)。
def _run_capture(command):
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=no_window_creationflags())

    return result.returncode, (result.stderr or "")
