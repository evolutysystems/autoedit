# 全身 ReID (ONNX) — 切り抜き画像 → 特徴ベクトル (ver5 resolve2 §3.7.2)
#
# 顔ではなく**服・髪・体型・持ち物を含めた全身の見た目**をベクトルにする (R2)。
# 同じ動画の中では服装が変わらないため、離れたセクション間でも同一人物を結べる (R6)。
#
# OSNet-x0.25 の入出力 (model.reid_format = "osnet"):
#   入力   float32[1, 3, 256, 128] (縦 256 x 横 128) / RGB / 0〜1 へ割る
#   正規化 ImageNet の mean (0.485, 0.456, 0.406) / std (0.229, 0.224, 0.225)
#   前処理 矩形を**縦横比を無視して**引き伸ばす (ReID の作法。letterbox にしない)
#   出力   float32[1, 512] → L2 正規化して返す (以降の比較は内積 = コサイン類似度)
#
# 検出器 (BGR・正規化なし・letterbox) とは前処理がまったく違うため、
# 共通関数にまとめず、このモジュールが自前で持つ (§3.7.2)。
import numpy as np

from ..utils.logger import get_logger
from . import models

_logger = get_logger(__name__)

# ImageNet の正規化パラメータ (torchreid の既定)
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


# 矩形ごとに特徴ベクトルを出す。
#   image : RGB の ndarray (高さ, 幅, 3) / uint8
#   boxes : [(x, y, w, h, score), ...] 素材ピクセル座標 (detector.detect の戻り値)
# 戻り値: ndarray (件数, 次元)。L2 正規化済み。モデルが無ければ 0 件の配列。
def embed(image, boxes, cfg):
    if image is None or not boxes:
        return np.zeros((0, int(cfg["model"]["reid_dim"])), dtype=np.float32)

    session = _session(cfg)
    if session is None:
        return np.zeros((0, int(cfg["model"]["reid_dim"])), dtype=np.float32)

    model_cfg = cfg["model"]
    width, height = (int(v) for v in model_cfg["reid_input"])
    crops = [_crop(image, box, width, height, model_cfg["reid_format"]) for box in boxes]
    blob = np.stack(crops, axis=0)

    try:
        outputs = session.run(None, {session.get_inputs()[0].name: blob})
    except Exception as error:                  # noqa: BLE001 (1 枚の失敗で解析を止めない)
        _logger.debug("特徴抽出に失敗したためこのフレームを飛ばします: %s", error, exc_info=True)
        return np.zeros((0, int(model_cfg["reid_dim"])), dtype=np.float32)

    vectors = np.asarray(outputs[0], dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors[None, :]
    return normalize(vectors)


# ベクトルを L2 正規化する (0 ベクトルは 0 のまま返す)
def normalize(vectors):
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim == 1:
        array = array[None, :]
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    return array / np.maximum(norms, 1e-9)


# 2 つのベクトルのコサイン距離 (0 = 同じ向き / 2 = 真逆)。
# どちらも L2 正規化済みであることを前提にする。
def distance(a, b):
    return float(1.0 - np.dot(np.asarray(a, dtype=np.float32),
                              np.asarray(b, dtype=np.float32)))


# ReID のセッション (見つからなければ None)
def _session(cfg):
    path = models.resolve_path(cfg["model"]["reid"])
    return models.load_session(path, cfg["model"]["providers"])


# 矩形を切り出して入力テンソル 1 件ぶんへ直す
def _crop(image, box, width, height, fmt):
    src_h, src_w = image.shape[:2]
    x, y, w, h = (float(v) for v in box[:4])
    left = int(max(x, 0))
    top = int(max(y, 0))
    right = int(min(x + w, src_w))
    bottom = int(min(y + h, src_h))
    if right - left < 2 or bottom - top < 2:
        # 極端に小さい矩形は黒画像で代用する (例外にせず解析を続ける)
        patch = np.zeros((2, 2, 3), dtype=np.uint8)
    else:
        patch = image[top:bottom, left:right]

    resized = _resize(patch, width, height).astype(np.float32) / 255.0
    if fmt == "osnet":
        resized = (resized - _MEAN) / _STD
    return resized.transpose(2, 0, 1)


# 縦横比を無視して指定の大きさへ引き伸ばす (最近傍 / OpenCV は使わない)
def _resize(image, new_w, new_h):
    height, width = image.shape[:2]
    rows = (np.arange(new_h) * (height / float(new_h))).astype(np.int32)
    cols = (np.arange(new_w) * (width / float(new_w))).astype(np.int32)
    rows = np.clip(rows, 0, height - 1)
    cols = np.clip(cols, 0, width - 1)
    return image[rows][:, cols]
