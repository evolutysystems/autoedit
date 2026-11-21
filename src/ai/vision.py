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

from ..utils.logger import get_logger
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
