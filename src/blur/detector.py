# 人物検出 (ONNX) — 画像 → 矩形の列 (ver5 resolve2 §3.7.1)
#
# 公開するのは detect() だけ。**画像 → [矩形, スコア] しか外へ出さない**ため、
# モデルを差し替えても呼び出し側は変わらない (§3.7.3)。
#
# YOLOX-Tiny の入出力 (model.detector_format = "yolox"):
#   入力   float32[1, 3, 416, 416] / BGR のまま / 0〜255 のまま (正規化しない)
#   前処理 縦横比を保って縮小し、余白を 114 で詰める (letterbox)
#   出力   float32[1, 3549, 85]  3549 = 52² + 26² + 13² / 85 = cx,cy,w,h,obj,80クラス
#   後処理 グリッド復元 → obj x cls をスコアに → NMS → class 0 (person) だけ残す
#
# **デコードは ONNX の中に入っていない**。公式配布の .onnx は --decode_in_inference
# 無しでエクスポートされており、生のグリッド出力が出てくる。
import numpy as np

from ..utils.logger import get_logger
from . import models

_logger = get_logger(__name__)

# COCO の person クラス (80 クラスの先頭)
_PERSON_CLASS = 0
# YOLOX のストライド (入力の一辺をこれで割った格子が並ぶ)
_STRIDES = (8, 16, 32)


# 1 枚から人物を検出する。
#   image : RGB の ndarray (高さ, 幅, 3) / uint8
#   cfg   : config.config() の戻り値
# 戻り値: [(x, y, w, h, score), ...] 素材ピクセル座標。検出できなければ空リスト。
def detect(image, cfg):
    session = _session(cfg)
    if session is None or image is None:
        return []

    model_cfg = cfg["model"]
    size = int(model_cfg["detector_input"])
    try:
        blob, scale = _preprocess(image, size, int(model_cfg["detector_pad_value"]),
                                  model_cfg["detector_format"])
        outputs = session.run(None, {session.get_inputs()[0].name: blob})
    except Exception as error:                  # noqa: BLE001 (1 枚の失敗で解析を止めない)
        _logger.debug("検出に失敗したためこのフレームを飛ばします: %s", error, exc_info=True)
        return []

    boxes = _decode(outputs[0], size, model_cfg["detector_format"],
                    float(model_cfg["detector_score"]))
    if not len(boxes):
        return []
    boxes = _nms(boxes, float(model_cfg["detector_nms"]))

    # letterbox の倍率で割って素材ピクセルへ戻す
    height, width = image.shape[:2]
    results = []
    for x1, y1, x2, y2, score in boxes:
        left = max(x1 / scale, 0.0)
        top = max(y1 / scale, 0.0)
        right = min(x2 / scale, float(width))
        bottom = min(y2 / scale, float(height))
        if right - left < 1.0 or bottom - top < 1.0:
            continue
        results.append((left, top, right - left, bottom - top, float(score)))
    return results


# 検出器のセッション (見つからなければ None)
def _session(cfg):
    path = models.resolve_path(cfg["model"]["detector"])
    return models.load_session(path, cfg["model"]["providers"])


# ------------------------------------------------------------------
# 前処理
# ------------------------------------------------------------------

# 入力テンソルと、元画像へ戻すための倍率を返す。
# YOLOX は BGR・0〜255 のまま・余白 114 で詰める (公式の preproc と同じ)。
def _preprocess(image, size, pad_value, fmt):
    height, width = image.shape[:2]
    scale = min(size / float(height), size / float(width))
    new_w = max(int(width * scale), 1)
    new_h = max(int(height * scale), 1)

    resized = _resize(image, new_w, new_h)
    padded = np.full((size, size, 3), pad_value, dtype=np.uint8)
    padded[:new_h, :new_w] = resized

    if fmt == "yolox":
        # RGB で受け取っているため BGR へ入れ替える。正規化はしない。
        padded = padded[:, :, ::-1]
        blob = padded.transpose(2, 0, 1).astype(np.float32)
    else:
        # 未知の形式は「RGB / 0〜1」という最も一般的な作法で扱う
        blob = padded.transpose(2, 0, 1).astype(np.float32) / 255.0
    return blob[None, ...], scale


# 最近傍で縮小する (OpenCV は無いため numpy だけで行う / §2.7)
# 検出の前処理は 1/4 以下への縮小が普通で、最近傍でも精度への影響は小さい。
def _resize(image, new_w, new_h):
    height, width = image.shape[:2]
    rows = (np.arange(new_h) * (height / float(new_h))).astype(np.int32)
    cols = (np.arange(new_w) * (width / float(new_w))).astype(np.int32)
    rows = np.clip(rows, 0, height - 1)
    cols = np.clip(cols, 0, width - 1)
    return image[rows][:, cols]


# ------------------------------------------------------------------
# 後処理
# ------------------------------------------------------------------

# 出力テンソルを (x1, y1, x2, y2, score) の配列へ直す (入力画像の座標系)。
def _decode(output, size, fmt, score_threshold):
    predictions = np.asarray(output)
    if predictions.ndim == 3:
        predictions = predictions[0]
    if predictions.ndim != 2 or predictions.shape[1] < 6:
        _logger.warning("検出モデルの出力の形が想定と違います: %s", predictions.shape)
        return np.zeros((0, 5), dtype=np.float32)

    if fmt == "yolox":
        predictions = _decode_yolox_grid(predictions, size)

    # cx, cy, w, h, obj, クラススコア…
    objectness = predictions[:, 4]
    class_scores = predictions[:, 5:]
    class_ids = np.argmax(class_scores, axis=1)
    scores = objectness * class_scores[np.arange(len(class_ids)), class_ids]

    keep = (class_ids == _PERSON_CLASS) & (scores >= score_threshold)
    if not np.any(keep):
        return np.zeros((0, 5), dtype=np.float32)

    boxes = predictions[keep, :4]
    scores = scores[keep]
    # 中心 + 大きさ → 左上 + 右下
    half_w = boxes[:, 2] / 2.0
    half_h = boxes[:, 3] / 2.0
    result = np.empty((len(boxes), 5), dtype=np.float32)
    result[:, 0] = boxes[:, 0] - half_w
    result[:, 1] = boxes[:, 1] - half_h
    result[:, 2] = boxes[:, 0] + half_w
    result[:, 3] = boxes[:, 1] + half_h
    result[:, 4] = scores
    return result


# YOLOX のグリッド出力を入力画像の座標へ復元する。
#   x = (out_x + grid_x) * stride    w = exp(out_w) * stride
# 格子はストライドの小さい順 (52x52 → 26x26 → 13x13) に並んでいる。
def _decode_yolox_grid(predictions, size):
    grids = []
    strides = []
    for stride in _STRIDES:
        cells = size // stride
        grid_y, grid_x = np.meshgrid(np.arange(cells), np.arange(cells), indexing="ij")
        grid = np.stack((grid_x, grid_y), axis=2).reshape(-1, 2)
        grids.append(grid)
        strides.append(np.full((len(grid), 1), stride, dtype=np.float32))

    grid = np.concatenate(grids, axis=0).astype(np.float32)
    stride = np.concatenate(strides, axis=0)
    if len(grid) != len(predictions):
        # 入力サイズと出力の格子数が合わない = 想定と違うモデル。
        # 復元すると座標が壊れるため、デコード済みとみなしてそのまま返す。
        _logger.warning(
            "検出モデルの格子数が入力サイズと合いません (格子 %d / 出力 %d)。"
            "デコード済みの出力として扱います", len(grid), len(predictions))
        return predictions

    decoded = predictions.copy()
    decoded[:, 0:2] = (predictions[:, 0:2] + grid) * stride
    decoded[:, 2:4] = np.exp(predictions[:, 2:4]) * stride
    return decoded


# Non-Maximum Suppression (重なった検出を 1 つに絞る)
def _nms(boxes, threshold):
    order = np.argsort(-boxes[:, 4])
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)

    keep = []
    while len(order):
        index = order[0]
        keep.append(index)
        if len(order) == 1:
            break
        rest = order[1:]
        left = np.maximum(x1[index], x1[rest])
        top = np.maximum(y1[index], y1[rest])
        right = np.minimum(x2[index], x2[rest])
        bottom = np.minimum(y2[index], y2[rest])
        overlap = np.maximum(right - left, 0) * np.maximum(bottom - top, 0)
        union = areas[index] + areas[rest] - overlap
        iou = np.where(union > 0, overlap / np.maximum(union, 1e-9), 0.0)
        order = rest[iou <= threshold]

    return boxes[keep]
