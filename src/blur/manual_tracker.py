# 手で足した人物・物の追従 (ver5 resolve3 §3.6)
#
# 既存の領域の追従 (region_tracker) はフレーム全体の位相相関で「カメラの動き」を追うため、
# 検出されなかった人物や動く物を足しても、その物を追いかけない (resolve3 §2.4 (b))。
# ここでは**囲んだ物そのもの**を追う。
#
#   1. 囲んだ時刻 (アンカー) の囲みを初期位置にする。YOLOX の枠 (しきい値を下げたもの) に
#      IoU 0.5 以上で重なるものがあれば、その枠へ吸着する (手描きの囲みは雑でよい)
#   2. アンカーから前と後ろの両方向へ、解析と同じ間隔で進む
#   3. 前の位置の周りを切り出して YOLOX を走らせ、前の位置と一番重なる枠へ乗り換える
#   4. 検出できなければ、切り出した範囲どうしの位相相関で平行移動だけ補う (物・部分的な隠れ)
#   5. 見失った状態が hold_sec を超えたら、その方向の追従を打ち切る
#
# 追う範囲は**アンカーを含むセクションの中だけ**。他の場面で同じ見た目を探すと誤爆が多い。
import copy

import numpy as np

from ..utils.logger import get_logger
from . import detector, geometry
from .region_tracker import _shrink, make_track, missing_regions, outline_of, phase_correlate
from .region_tracker import replace_tracks, to_gray

_logger = get_logger(__name__)

# 後ろ向きに追うとき、一度にデコードする長さ (秒)。フレームを溜めるためメモリを抑える
_BACKWARD_CHUNK_SEC = 2.0
# 吸着する検出枠の最小 IoU
_SNAP_MIN_IOU = 0.5
# 相関を取る前に縮める上限 (切り出しが大きいときだけ縮める)
_CORRELATE_MAX_PX = 256
# 相関を取る範囲の余白 (枠の何倍ぶん広げるか)。検出の探索範囲 (search_ratio) より狭くする。
# 広いと動かない背景が相関を支配し、物が動いても「ずれ 0」と出て位置が止まる (実素材で確認)
_CORRELATE_MARGIN = 0.25


# 指定のうち「人物・物」の枠で、追従トラックがまだ無いものを作る。
# 戻り値: 追加したトラックの件数
def build_manual_tracks(timeline, settings, analysis, decisions, cfg,
                        on_progress=None, cancel=None):
    from . import decisions as decisions_module  # noqa: PLC0415 (循環 import を避ける)

    regions = [r for r in (decisions or {}).get("regions", [])
               if r.get("path") and decisions_module.region_kind(r) == decisions_module.KIND_OBJECT]
    targets = missing_regions(analysis, regions, "manual")
    if not targets:
        return 0

    added = 0
    for index, region in enumerate(targets):
        if cancel is not None and cancel():
            break
        tracks = track_object(
            timeline, settings, region, cfg,
            on_progress=lambda ratio, base=index: _report(
                on_progress, (base + ratio) / float(len(targets))),
            cancel=cancel)
        replace_tracks(analysis, region, tracks)
        added += len(tracks)

    _report(on_progress, 1.0)
    _logger.info("手で足した枠の追従: %d 件のトラックを追加しました", added)
    return added


# 1 つの枠を追い、トラックの一覧を返す (追えなければ空リスト)
def track_object(timeline, settings, region, cfg, on_progress=None, cancel=None):
    from . import analyzer                      # noqa: PLC0415 (循環 import を避ける)

    media_id = str(region.get("media_id") or "")
    media = timeline.media_by_id(media_id)
    if media is None:
        _logger.warning("枠の素材が見つからないため追従できません: %s", media_id)
        return []

    anchor_sec = float(region.get("anchor_sec") or 0.0)
    canvas_path = geometry.normalized_path_to_canvas(
        region.get("path"), timeline.width, timeline.height)
    canvas_rect = geometry.polygon_bounds(canvas_path)
    if canvas_rect is None or canvas_rect[2] <= 0 or canvas_rect[3] <= 0:
        return []
    transform = geometry.source_to_canvas_transform(media, timeline.width, timeline.height)
    anchor_rect = geometry.canvas_rect_to_source(canvas_rect, transform)
    outline = outline_of(canvas_path, transform, anchor_rect)

    start, end = _section_of(analyzer.collect_spans(timeline), media_id, anchor_sec,
                             float(cfg["manual"]["fixed_span_sec"]))
    sample_fps = float(cfg["analysis"]["sample_fps"])

    if str(region.get("follow") or "") == "fixed":
        # 追えなかった枠を「この位置に固定」にした場合 (resolve3 §3.6)。アンカーの前後だけに置く
        span = float(cfg["manual"]["fixed_span_sec"])
        samples = _fixed_samples(anchor_rect, max(start, anchor_sec - span),
                                 min(end, anchor_sec + span), sample_fps)
        return [make_track(region, media_id, start, "manual", samples, outline)]

    follower = _Follower(cfg, settings)
    anchor_image = _first_frame(analyzer, media, anchor_sec, settings)
    if anchor_image is None:
        _logger.warning("枠の基準フレームを取得できないため追従できません")
        return []

    rect, snapped = follower.snap(anchor_image, anchor_rect)
    if snapped:
        # 検出枠へ吸着した = 人物。手描きの形ではなく人物の塗り方 (角丸・輪郭) を使う
        outline = ""
    samples = [_sample(anchor_sec, rect)]

    total = max(end - start, 1e-6)
    forward = follower.follow(
        _forward_frames(analyzer, media, anchor_sec, end, sample_fps, settings),
        anchor_image, rect, cancel,
        lambda sec: _report(on_progress, 0.5 * (sec - anchor_sec) / max(end - anchor_sec, 1e-6)))
    backward = follower.follow(
        _backward_frames(analyzer, media, start, anchor_sec, sample_fps, settings, cancel),
        anchor_image, rect, cancel,
        lambda sec: _report(on_progress,
                            0.5 + 0.5 * (anchor_sec - sec) / max(anchor_sec - start, 1e-6)))
    _report(on_progress, 1.0)

    samples = sorted(backward + samples + forward, key=lambda s: s["t"])
    if len(samples) < 2:
        _logger.info("枠を追いかけられませんでした (%.1f 秒地点 / 区間 %.1f 秒)", anchor_sec, total)
        return []
    return [make_track(region, media_id, start, "manual", samples, outline)]


# 追従の本体 (前向き・後ろ向きで共通)
class _Follower:

    def __init__(self, cfg, settings):
        self._cfg = cfg
        # 手で足す対象は検出器が拾えなかったものが多いため、しきい値を下げた設定で探す
        self._detect_cfg = copy.deepcopy(cfg)
        self._detect_cfg["model"]["detector_score"] = float(cfg["manual"]["detector_score"])
        self._search_ratio = float(cfg["manual"]["search_ratio"])
        self._min_iou = float(cfg["manual"]["min_iou"])
        # 小さな範囲の相関は人物の動きで PSR が下がりやすいため、場所の追従より低い合格ラインを使う
        self._match_psr = float(cfg["manual"]["match_psr"])
        self._hold_sec = float(cfg["region"]["hold_sec"])

    # 囲みを検出枠へ吸着させる。戻り値 (矩形, 吸着したか)
    def snap(self, image, rect):
        best = self._best_detection(image, rect, _SNAP_MIN_IOU)
        if best is None:
            return tuple(rect), False
        return best, True

    # frames の順に追う。frames は (時刻, 画像) の反復子 (アンカーから離れる向き)。
    # 戻り値: 見つかった時刻のサンプル列
    def follow(self, frames, anchor_image, rect, cancel, on_step):
        samples = []
        previous_image = anchor_image
        current = tuple(rect)
        lost_since = None
        last_sec = None
        for sec, image in frames:
            if cancel is not None and cancel():
                break
            on_step(sec)
            found = self._best_detection(image, current, self._min_iou)
            if found is None:
                found = self._correlate(previous_image, image, current)
            if found is None:
                lost_since = sec if lost_since is None else lost_since
                if abs(sec - lost_since) > self._hold_sec:
                    break
                continue
            lost_since = None
            current = found
            previous_image = image
            last_sec = sec
            samples.append(_sample(sec, current))
        if last_sec is not None:
            _logger.debug("枠の追従: %d 点 (最後 %.2f 秒)", len(samples), last_sec)
        return samples

    # 前の位置の周りを切り出して検出し、前の位置と一番重なる枠を返す (無ければ None)
    def _best_detection(self, image, rect, min_iou):
        window = _search_window(image, rect, self._search_ratio)
        if window is None:
            return None
        left, top, right, bottom = window
        boxes = detector.detect(image[top:bottom, left:right], self._detect_cfg)
        best = None
        best_iou = min_iou
        for x, y, w, h, _score in boxes:
            candidate = (x + left, y + top, w, h)
            overlap = geometry.iou(candidate, rect)
            if overlap >= best_iou:
                best = candidate
                best_iou = overlap
        return best

    # 同じ切り出し範囲の前後 2 枚で位相相関を取り、平行移動を当てる (無ければ None)
    def _correlate(self, previous_image, image, rect):
        window = _search_window(image, rect, _CORRELATE_MARGIN)
        if window is None or previous_image is None or previous_image.shape != image.shape:
            return None
        left, top, right, bottom = window
        scale = min(1.0, _CORRELATE_MAX_PX / float(max(right - left, bottom - top, 1)))
        reference = _shrink(to_gray(previous_image[top:bottom, left:right]), scale)
        target = _shrink(to_gray(image[top:bottom, left:right]), scale)
        dx, dy, _peak, psr = phase_correlate(reference, target)
        if psr < self._match_psr:
            return None
        x, y, width, height = rect
        return (x - dx / scale, y - dy / scale, width, height)


# 前の位置を search_ratio 倍ぶん広げた切り出し範囲 (left, top, right, bottom)。小さすぎれば None。
def _search_window(image, rect, ratio):
    height, width = image.shape[:2]
    x, y, w, h = (float(v) for v in rect)
    left = int(max(x - w * ratio, 0))
    top = int(max(y - h * ratio, 0))
    right = int(min(x + w * (1.0 + ratio), width))
    bottom = int(min(y + h * (1.0 + ratio), height))
    if right - left < 16 or bottom - top < 16:
        return None
    return left, top, right, bottom


# アンカーを含む解析区間 (開始, 終了)。無ければアンカーの前後 fallback_sec。
def _section_of(spans, media_id, anchor_sec, fallback_sec):
    for span_media_id, ranges in spans:
        if span_media_id != media_id:
            continue
        for start, end in ranges:
            if start - 1e-3 <= anchor_sec <= end + 1e-3:
                return float(start), float(end)
    return max(anchor_sec - fallback_sec, 0.0), anchor_sec + fallback_sec


def _first_frame(analyzer, media, sec, settings):
    for _t, image in analyzer.iter_frames(media, max(sec - 0.01, 0.0), sec + 0.5, 4.0, settings):
        return image
    return None


# アンカーより後ろを前向きに返す (アンカーそのものは含めない)
def _forward_frames(analyzer, media, anchor_sec, end, sample_fps, settings):
    interval = 1.0 / max(sample_fps, 0.01)
    if end - anchor_sec < interval * 0.5:
        return
    for sec, image in analyzer.iter_frames(media, anchor_sec + interval * 0.5, end,
                                           sample_fps, settings):
        if sec > anchor_sec + 1e-3:
            yield sec, image


# アンカーより前を、アンカーに近い順に返す。短い区間ずつデコードして逆順に並べる。
def _backward_frames(analyzer, media, start, anchor_sec, sample_fps, settings, cancel):
    chunk_end = anchor_sec
    while chunk_end - start > 1e-3:
        if cancel is not None and cancel():
            return
        chunk_start = max(start, chunk_end - _BACKWARD_CHUNK_SEC)
        frames = [(sec, image) for sec, image in analyzer.iter_frames(
            media, chunk_start, chunk_end, sample_fps, settings)
            if chunk_start - 1e-3 <= sec < chunk_end - 1e-3]
        yield from reversed(frames)
        chunk_end = chunk_start


def _fixed_samples(rect, start, end, sample_fps):
    interval = 1.0 / max(sample_fps, 0.01)
    count = max(int((end - start) / interval), 1)
    samples = [_sample(start + index * interval, rect) for index in range(count)]
    samples.append(_sample(end, rect))
    return samples


def _sample(sec, rect):
    x, y, w, h = (float(v) for v in rect)
    return {"t": float(sec), "x": x, "y": y, "w": w, "h": h, "score": 1.0}


def _report(on_progress, ratio):
    if on_progress is not None:
        on_progress(float(np.clip(ratio, 0.0, 1.0)))
