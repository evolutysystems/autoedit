# 囲んだ場所の追従 (ver5 resolve8 §3.4 / §5.5)
#
# キーフレームで区切った「区切り (segment)」を 1 つずつ追う。
#
#   1. 区切りの端のキーフレームの矩形を初期位置にする
#   2. 端から離れる向きへ、track.sample_fps の間隔で進む
#   3. 前の位置の周りを切り出して検出器を走らせ、前の位置と一番重なる枠の**中心**へ移る
#   4. 検出できなければ、切り出した範囲どうしの位相相関で平行移動だけ補う
#   5. 分からない状態が hold_sec を超えたら、その区切りの追従を打ち切る (= 見失った)
#
# **大きさは追わない** (resolve8 §3.3)。位相相関は拡大縮小に追従せず、検出枠の大きさへ
# 合わせると利用者が決めた大きさが勝手に変わるため、位置だけを追う。
# 大きさはキーフレームの線形補間で決まる (plan.rect_at)。
#
# 検出モデルが無い環境では 4. だけで追う (枠は途切れやすくなるが機能は動く)。
import hashlib
import json

from ..utils.logger import get_logger
from . import correlate, decisions as decisions_module, detector, frames, geometry, store

_logger = get_logger(__name__)

# 相関を取る前に縮める上限 (切り出しが大きいときだけ縮める)
_CORRELATE_MAX_PX = 256
# 相関を取る範囲の余白 (枠の何倍ぶん広げるか)。検出の探索範囲 (search_ratio) より狭くする。
# 広いと動かない背景が相関を支配し、物が動いても「ずれ 0」と出て位置が止まる (実素材で確認)
_CORRELATE_MARGIN = 0.25

# 追従の具合
STATUS_OK = "ok"
STATUS_LOST = "lost"
STATUS_FAILED = "failed"

# 向き
DIR_FORWARD = "fwd"
DIR_BACKWARD = "back"


# ------------------------------------------------------------------
# 区切り (resolve8 §5.5.1)
# ------------------------------------------------------------------

# 指定から追う区切りを作る。span とキーフレームで端が決まる。
#   戻り値: [{"spec_id","media_id","dir","start","end","start_rect","end_rect","hash"}, …]
def segments_for(spec, sample_fps):
    if spec.get("kind") != decisions_module.KIND_AREA:
        return []
    if str(spec.get("follow") or decisions_module.FOLLOW_TRACK) != decisions_module.FOLLOW_TRACK:
        return []                       # 動かさない指定は追わない
    keys = decisions_module.keys_of(spec)
    if not keys:
        return []

    span = spec.get("span") or {}
    start = float(span.get("start", 0.0))
    end = float(span.get("end", 0.0))
    minimum = 1.0 / max(float(sample_fps), 0.01)

    out = []
    if keys[0]["t"] - start > minimum:
        # 先頭のキーから後ろ向きに、クリップの頭まで追う
        out.append(_segment(spec, DIR_BACKWARD, start, keys[0]["t"], keys[0], None))
    for before, after in zip(keys, keys[1:]):
        if after["t"] - before["t"] > minimum:
            out.append(_segment(spec, DIR_FORWARD, before["t"], after["t"], before, after))
    if end - keys[-1]["t"] > minimum:
        out.append(_segment(spec, DIR_FORWARD, keys[-1]["t"], end, keys[-1], None))
    return out


# 区切り 1 つを作る。
#   anchor : 追い始めるキーフレーム / other: 反対の端のキーフレーム (無ければ None)
def _segment(spec, direction, start, end, anchor, other):
    segment = {
        "spec_id": str(spec.get("id") or ""),
        "media_id": str(spec.get("media_id") or ""),
        "dir": direction,
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "start_rect": list(anchor["rect"]),
        "end_rect": list(other["rect"]) if other is not None else None,
    }
    segment["hash"] = _segment_hash(segment)
    return segment


# 区切りの指紋。**大きさ (w, h) は入れない** = 大きさだけ変えても追い直さない (§5.10.5)
def _segment_hash(segment):
    payload = {
        "media": segment["media_id"],
        "dir": segment["dir"],
        "start": round(float(segment["start"]), 3),
        "end": round(float(segment["end"]), 3),
        "from": _center(segment["start_rect"]),
        "to": _center(segment["end_rect"]) if segment["end_rect"] else None,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _center(rect):
    x, y, width, height = (float(v) for v in rect)
    return [round(x + width / 2.0, 4), round(y + height / 2.0, 4)]


# ------------------------------------------------------------------
# まとめて走らせる入口 (resolve8 §5.5.3)
# ------------------------------------------------------------------

# 指紋の合わない区切りだけを追い直し、キャッシュへ書く。
# 戻り値: 追い直した区切りの数
def ensure_tracks(timeline, settings, tracks, decisions, cfg, on_progress=None, cancel=None):
    wanted = []
    for spec in (decisions or {}).get("specs", []):
        wanted.extend(segments_for(spec, cfg["track"]["sample_fps"]))
    store.drop_unused(tracks, decisions, wanted)

    missing = store.missing_segments(tracks, wanted)
    if not missing:
        _report(on_progress, 1.0)
        return 0

    total = len(missing)
    for index, segment in enumerate(missing):
        if cancel is not None and cancel():
            break
        result = track_segment(
            timeline, settings, segment, cfg,
            on_progress=lambda ratio, base=index: _report(on_progress, (base + ratio) / total),
            cancel=cancel)
        store.put_segment(tracks, segment, result)
    _report(on_progress, 1.0)
    _logger.info("ぼかしの追従: %d 区切りを追いました", total)
    return total


# 区切り 1 つを追う。戻り値: キャッシュへ入れる辞書
def track_segment(timeline, settings, segment, cfg, on_progress=None, cancel=None):
    media = timeline.media_by_id(segment["media_id"])
    empty = {"hash": segment["hash"], "dir": segment["dir"],
             "start": segment["start"], "end": segment["end"],
             "status": STATUS_FAILED, "lost_sec": None, "samples": []}
    if media is None:
        _logger.warning("ぼかしの追従: 素材が見つかりません: %s", segment["media_id"])
        return empty

    transform = geometry.source_to_canvas_transform(media, timeline.width, timeline.height)
    canvas = (timeline.width, timeline.height)
    rect = geometry.normalized_rect_to_source(segment["start_rect"], transform, *canvas)
    if rect[2] < 2 or rect[3] < 2:
        return empty

    sample_fps = float(cfg["track"]["sample_fps"])
    anchor_sec = float(segment["start"] if segment["dir"] == DIR_FORWARD else segment["end"])
    anchor_image = frames.first_frame(media, anchor_sec, settings)
    if anchor_image is None:
        _logger.warning("ぼかしの追従: 基準のフレームを取得できません (%.2f 秒)", anchor_sec)
        return empty

    if segment["dir"] == DIR_FORWARD:
        stream = frames.forward(media, anchor_sec, float(segment["end"]), sample_fps, settings)
        length = max(float(segment["end"]) - anchor_sec, 1e-6)
    else:
        stream = frames.backward(media, float(segment["start"]), anchor_sec, sample_fps,
                                 settings, cancel)
        length = max(anchor_sec - float(segment["start"]), 1e-6)

    # 進捗は「基準の時刻からどれだけ離れたか」で出す
    def on_step(sec):
        _report(on_progress, abs(float(sec) - anchor_sec) / length)

    follower = _Follower(cfg)
    samples, lost_sec = follower.follow(stream, rect, cancel, on_step, anchor_image)
    _report(on_progress, 1.0)

    if not samples:
        # 1 点も追えなかった = 追従できていない。キーフレームだけで位置が決まる。
        return dict(empty, status=STATUS_FAILED)

    ordered = sorted(samples, key=lambda s: s["t"])
    return {
        "hash": segment["hash"], "dir": segment["dir"],
        "start": segment["start"], "end": segment["end"],
        "status": STATUS_LOST if lost_sec is not None else STATUS_OK,
        "lost_sec": round(float(lost_sec), 3) if lost_sec is not None else None,
        "samples": [_to_sample(sample, transform, canvas) for sample in ordered],
    }


# 素材ピクセルのサンプルを、保存する形 (正規化キャンバス座標の中心) へ直す
def _to_sample(sample, transform, canvas):
    rect = geometry.source_rect_to_normalized(
        (sample["x"], sample["y"], sample["w"], sample["h"]), transform, *canvas)
    return {"t": round(float(sample["t"]), 3),
            "cx": round(rect[0] + rect[2] / 2.0, 4),
            "cy": round(rect[1] + rect[3] / 2.0, 4),
            "score": round(float(sample.get("score", 1.0)), 3)}


# ------------------------------------------------------------------
# 追従の本体
# ------------------------------------------------------------------

class _Follower:

    def __init__(self, cfg):
        # 手で囲む対象は検出器が拾いにくいものが多いため、model.detector_score は
        # 低め (既定 0.2) にしてある (config._DEFAULTS)
        self._cfg = cfg
        self._search_ratio = float(cfg["track"]["search_ratio"])
        self._min_iou = float(cfg["track"]["min_iou"])
        # 小さな範囲の相関は人物の動きで PSR が下がりやすいため、低い合格ラインを使う
        self._match_psr = float(cfg["track"]["match_psr"])
        self._hold_sec = float(cfg["track"]["hold_sec"])

    # frames の順に追う。frames は (時刻, 画像) の反復子 (基準から離れる向き)。
    # 戻り値 (サンプル列, 見失った時刻 or None)
    def follow(self, stream, rect, cancel, on_step, anchor_image=None):
        samples = []
        previous_image = anchor_image
        current = tuple(rect)
        lost_since = None
        for sec, image in stream:
            if cancel is not None and cancel():
                break
            on_step(sec)
            found = self._step(image, previous_image, current)
            if found is None:
                lost_since = sec if lost_since is None else lost_since
                if abs(sec - lost_since) > self._hold_sec:
                    return samples, lost_since
                continue
            lost_since = None
            current = found
            previous_image = image
            samples.append({"t": float(sec), "x": current[0], "y": current[1],
                            "w": current[2], "h": current[3], "score": 1.0})
        return samples, lost_since

    # 1 枚ぶん進める。戻り値: 新しい矩形 (大きさは変えない) / 分からなければ None
    def _step(self, image, previous_image, rect):
        found = self._best_detection(image, rect, self._min_iou)
        if found is not None:
            # 検出枠の中心へ移し、大きさは囲みのまま保つ (resolve8 §3.3)
            cx = found[0] + found[2] / 2.0
            cy = found[1] + found[3] / 2.0
            return (cx - rect[2] / 2.0, cy - rect[3] / 2.0, rect[2], rect[3])
        if previous_image is None:
            return None
        return self._correlate(previous_image, image, rect)

    # 前の位置の周りを切り出して検出し、前の位置と一番重なる枠を返す (無ければ None)
    def _best_detection(self, image, rect, min_iou):
        window = _search_window(image, rect, self._search_ratio)
        if window is None:
            return None
        left, top, right, bottom = window
        boxes = detector.detect(image[top:bottom, left:right], self._cfg)
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
        if window is None or previous_image.shape != image.shape:
            return None
        left, top, right, bottom = window
        scale = min(1.0, _CORRELATE_MAX_PX / float(max(right - left, bottom - top, 1)))
        reference = correlate.shrink(
            correlate.to_gray(previous_image[top:bottom, left:right]), scale)
        target = correlate.shrink(correlate.to_gray(image[top:bottom, left:right]), scale)
        dx, dy, _peak, psr = correlate.phase_correlate(reference, target)
        if psr < self._match_psr:
            return None
        x, y, width, height = rect
        return (x - dx / scale, y - dy / scale, width, height)


# 前の位置を ratio 倍ぶん広げた切り出し範囲 (left, top, right, bottom)。小さすぎれば None。
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


def _report(on_progress, ratio):
    if on_progress is not None:
        on_progress(min(max(float(ratio), 0.0), 1.0))
