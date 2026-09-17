# 身体の輪郭 (ONNX) — 画像 + 枠 → 枠に対する相対座標の輪郭 (ver5 resolve3 §3.1 / §5.3)
#
# SAM 系の「枠を与えると、その枠の中の物の形を返す」モデルを使う。
# 検出器 (YOLOX) が出した枠をそのまま渡すため、**重なって並んだ人物も 1 人ずつ分かれる**
# (人物の画素かどうかだけを返す意味セグメンテーションでは分けられない / resolve3 §3.1)。
#
# 重い画像エンコーダは 1 枚 1 回、軽いデコーダを枠ごとに回す。人数が増えても重い部分は増えない。
#
# 前後処理 (silhouette.format):
#   "mobilesam"    tools/export_silhouette.py で書き出したもの (入出力の約束はそのファイルの冒頭)
#   "efficientsam" EfficientSAM 公式配布の ONNX (0〜1 の RGB を入れる / 3 候補から IoU の高いものを使う)
#
# 輪郭が取れない・当てにならない場合は None を返す。呼び出し側は矩形で塗る (取りこぼし防止 / §3.3)。
import numpy as np

from ..utils.logger import get_logger
from . import contour, models

_logger = get_logger(__name__)

# PIL は任意依存として扱う (無ければ最近傍で縮小する)
try:
    from PIL import Image
    _PIL_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PIL_AVAILABLE = False

# 連結成分の切り出しに scipy を使えれば使う (無ければ切り出さずに外周をたどる)
try:
    from scipy import ndimage as _ndimage
except Exception:  # noqa: BLE001
    _ndimage = None

# SAM の画像の正規化 (0〜255 の尺度)
_PIXEL_MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32)
_PIXEL_STD = np.array([58.395, 57.12, 57.375], dtype=np.float32)
# デコーダに作らせるマスクの長辺 (px)。輪郭の細かさと、外周をたどる時間の釣り合い
_MASK_LONG_SIDE = 320
# 枠の左上 / 右下を表す点のラベル (SAM の約束)
_LABEL_TOP_LEFT = 2.0
_LABEL_BOTTOM_RIGHT = 3.0
# 外周をたどる 8 近傍 (時計回り: 左, 左上, 上, 右上, 右, 右下, 下, 左下) / (行, 列) の差
_NEIGHBORS = ((0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1))

_FORMATS = ("mobilesam", "efficientsam")


# 輪郭モデルが使えるか。戻り値 (使えるか, 理由の文字列)。
def availability(cfg):
    sil = cfg["silhouette"]
    if sil["format"] not in _FORMATS:
        return False, f"輪郭モデルの形式 ({sil['format']}) には対応していないため、四角でぼかします"
    missing = [sil[key] for key in ("model", "decoder") if models.resolve_path(sil[key]) is None]
    if missing:
        return False, "輪郭モデルが見つからないため、四角でぼかします: " + " / ".join(missing)
    if not models.is_runtime_available():
        return False, "onnxruntime が利用できないため、四角でぼかします"
    return True, "輪郭モデル: 準備完了"


# 画像 1 枚と枠の一覧から、枠ごとの輪郭を返す。
#   image : RGB の ndarray (高さ, 幅, 3) / uint8 (素材ピクセル)
#   boxes : [(x, y, w, h, score), …] detector.detect の戻り値
# 戻り値: [輪郭 or None, …] (boxes と同じ並び)。輪郭は枠に対する相対座標 [(u, v), …]
def segment(image, boxes, cfg):
    if image is None or not boxes:
        return []
    sil = cfg["silhouette"]
    encoder = models.load_session(models.resolve_path(sil["model"]), cfg["model"]["providers"])
    decoder = models.load_session(models.resolve_path(sil["decoder"]), cfg["model"]["providers"])
    if encoder is None or decoder is None:
        return [None] * len(boxes)

    height, width = image.shape[:2]
    mask_scale = _MASK_LONG_SIDE / float(max(height, width))
    mask_h = max(int(round(height * mask_scale)), 8)
    mask_w = max(int(round(width * mask_scale)), 8)

    if sil["format"] == "efficientsam":
        masks = _efficientsam(encoder, decoder, image, boxes, sil, mask_h, mask_w)
    else:
        masks = _mobilesam(encoder, decoder, image, boxes, sil, mask_h, mask_w)

    results = []
    for box, mask in zip(boxes, masks):
        results.append(None if mask is None else
                       _to_contour(mask, box, mask_scale, mask_h, mask_w, sil))
    return results


# ------------------------------------------------------------------
# 前後処理 (形式ごと)
# ------------------------------------------------------------------

def _mobilesam(encoder, decoder, image, boxes, sil, mask_h, mask_w):
    size = int(sil["input"])
    height, width = image.shape[:2]
    scale = size / float(max(height, width))
    new_h = int(height * scale + 0.5)
    new_w = int(width * scale + 0.5)
    resized = (_resize(image, new_w, new_h).astype(np.float32) - _PIXEL_MEAN) / _PIXEL_STD
    blob = np.zeros((size, size, 3), dtype=np.float32)
    blob[:new_h, :new_w] = resized
    embedding = encoder.run(None, {encoder.get_inputs()[0].name:
                                   blob.transpose(2, 0, 1)[None]})[0]

    masks = []
    for x, y, w, h, *_rest in boxes:
        feed = {
            "image_embeddings": embedding,
            "point_coords": np.array([[[x * scale, y * scale],
                                       [(x + w) * scale, (y + h) * scale]]], dtype=np.float32),
            "point_labels": np.array([[_LABEL_TOP_LEFT, _LABEL_BOTTOM_RIGHT]], dtype=np.float32),
            "mask_input": np.zeros((1, 1, 256, 256), dtype=np.float32),
            "has_mask_input": np.zeros(1, dtype=np.float32),
            "orig_im_size": np.array([mask_h, mask_w], dtype=np.float32),
        }
        try:
            logits = decoder.run(None, feed)[0]
        except Exception:                           # noqa: BLE001 (1 人の失敗で他を止めない)
            _logger.debug("輪郭のデコードに失敗しました", exc_info=True)
            masks.append(None)
            continue
        masks.append(logits[0, 0] > float(sil["threshold"]))
    return masks


def _efficientsam(encoder, decoder, image, boxes, sil, mask_h, mask_w):
    size = int(sil["input"])
    height, width = image.shape[:2]
    scale = size / float(max(height, width))
    resized = _resize(image, int(width * scale + 0.5), int(height * scale + 0.5))
    blob = (resized.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
    embedding = encoder.run(None, {encoder.get_inputs()[0].name: blob})[0]

    to_mask_x = mask_w / float(width)
    to_mask_y = mask_h / float(height)
    masks = []
    for x, y, w, h, *_rest in boxes:
        feed = {
            "image_embeddings": embedding,
            "batched_point_coords": np.array(
                [[[[x * to_mask_x, y * to_mask_y],
                   [(x + w) * to_mask_x, (y + h) * to_mask_y]]]], dtype=np.float32),
            "batched_point_labels": np.array([[[_LABEL_TOP_LEFT, _LABEL_BOTTOM_RIGHT]]],
                                             dtype=np.float32),
            "orig_im_size": np.array([mask_h, mask_w], dtype=np.int64),
        }
        try:
            outputs = decoder.run(None, feed)
        except Exception:                           # noqa: BLE001
            _logger.debug("輪郭のデコードに失敗しました", exc_info=True)
            masks.append(None)
            continue
        best = int(np.argmax(outputs[1][0, 0]))
        masks.append(outputs[0][0, 0, best] > float(sil["threshold"]))
    return masks


def _resize(image, new_w, new_h):
    if _PIL_AVAILABLE:
        return np.asarray(Image.fromarray(image).resize((new_w, new_h), Image.BILINEAR))
    height, width = image.shape[:2]
    rows = np.clip((np.arange(new_h) * (height / float(new_h))).astype(np.int32), 0, height - 1)
    cols = np.clip((np.arange(new_w) * (width / float(new_w))).astype(np.int32), 0, width - 1)
    return image[rows][:, cols]


# ------------------------------------------------------------------
# マスク → 輪郭
# ------------------------------------------------------------------

# 2 値マスクを、枠に対する相対座標の輪郭にする。当てにならなければ None。
#   ・枠の外へ伸びた部分は枠で切る (隣の人物へはみ出した形を持ち込まない)
#   ・一番大きい塊だけを残す (背景に散った点を拾わない)
#   ・外周だけをたどる (穴は無視する = ぼかす側に倒れる)
def _to_contour(mask, box, mask_scale, mask_h, mask_w, sil):
    x, y, w, h = (float(v) for v in box[:4])
    left = int(max(np.floor(x * mask_scale), 0))
    top = int(max(np.floor(y * mask_scale), 0))
    right = int(min(np.ceil((x + w) * mask_scale), mask_w))
    bottom = int(min(np.ceil((y + h) * mask_scale), mask_h))
    if right - left < 3 or bottom - top < 3:
        return None

    region = np.asarray(mask[top:bottom, left:right], dtype=bool)
    region = _largest_component(region)
    box_area = float((right - left) * (bottom - top))
    if region is None or region.sum() < float(sil["min_fill_ratio"]) * box_area:
        return None

    boundary = _trace_boundary(region)
    if len(boundary) < 3:
        return None
    # マスクの画素 → 素材ピクセル → 枠に対する相対座標
    points = [((col + left + 0.5) / mask_scale, (row + top + 0.5) / mask_scale)
              for row, col in boundary]
    relative = contour.to_relative(points, (x, y, w, h))
    return contour.normalize(relative, int(sil["points"]))


# 一番大きい連結成分だけを残す (scipy が無ければそのまま返す)
def _largest_component(region):
    if not region.any():
        return None
    if _ndimage is None:
        return region
    labels, count = _ndimage.label(region)
    if count <= 1:
        return region
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    return labels == int(np.argmax(sizes))


# 塊の外周を時計回りにたどる (Moore 近傍の境界追跡)。戻り値 [(行, 列), …]
# 停止は「始点へ戻り、しかも入ってきた向きも最初と同じ」とき (Jacob の停止条件)。
# 始点へ戻っただけで止めると、くびれのある形で外周を一周する前に止まってしまう。
def _trace_boundary(region):
    padded = np.pad(region, 1, constant_values=False)
    rows, cols = np.nonzero(padded)
    if len(rows) == 0:
        return []
    # 一番上の行の一番左の画素から始める (その左は必ず外)
    first = int(np.argmin(rows * padded.shape[1] + cols))
    start = (int(rows[first]), int(cols[first]))
    start_back = (start[0], start[1] - 1)

    boundary = [start]
    current = start
    back = start_back
    # 外周の長さは画素数の 4 倍を超えない。斜め 1 画素の線のような退化した形で回り続けないための上限
    for _step in range(len(rows) * 4 + 8):
        back_index = _NEIGHBORS.index((back[0] - current[0], back[1] - current[1]))
        moved = False
        previous = back
        for turn in range(1, 9):
            d_row, d_col = _NEIGHBORS[(back_index + turn) % 8]
            candidate = (current[0] + d_row, current[1] + d_col)
            if padded[candidate]:
                back = previous
                current = candidate
                moved = True
                break
            previous = candidate
        if not moved:
            break                                   # 1 画素だけの塊
        if current == start and back == start_back:
            break
        boundary.append(current)
    # 詰め物の 1 画素ぶんを戻す
    return [(row - 1, col - 1) for row, col in boundary]
