# 囲みの形 (枠に対する相対座標) の扱い (ver5 resolve8 §3.6)
#
# 形は**枠 (x, y, w, h) に対する相対座標** (u, v は 0.0〜1.0) で持つ。
# 枠が動く・大きさが変わると、形も一緒に付いてくる。
#
# ver5 resolve8 では**新しい囲みは矩形だけ**になったため、ここを使うのは
# 「v4 以前の自由な囲みを引き継いだ指定」を表示・塗るときだけである。
# 輪郭の打ち直し・始点揃え・前後の混ぜ合わせ (身体の輪郭のための処理) は廃止した。
#
# 依存は numpy だけ。
import base64

import numpy as np

# 量子化の段数 (1 バイト)
_LEVELS = 255


# 絶対座標の点列を、枠に対する相対座標 (0〜1) へ直す。枠の外へはみ出た点は枠へ寄せる。
def to_relative(points, rect):
    x, y, width, height = (float(v) for v in rect)
    if width <= 0 or height <= 0:
        return []
    array = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    u = np.clip((array[:, 0] - x) / width, 0.0, 1.0)
    v = np.clip((array[:, 1] - y) / height, 0.0, 1.0)
    return np.stack([u, v], axis=1).tolist()


# 相対座標の点列を、枠に当てはめて絶対座標へ直す
def to_absolute(relative, rect):
    x, y, width, height = (float(v) for v in rect)
    array = np.asarray(relative, dtype=np.float64).reshape(-1, 2)
    return np.stack([x + array[:, 0] * width, y + array[:, 1] * height], axis=1).tolist()


# 相対座標の点列を base64 の文字列にする (1 点 = u, v の 2 バイト)
def encode(relative):
    if not relative:
        return ""
    array = np.clip(np.rint(np.asarray(relative, dtype=np.float64) * _LEVELS), 0, _LEVELS)
    return base64.b64encode(array.astype(np.uint8).tobytes()).decode("ascii")


# encode の逆。壊れていれば None。
def decode(text):
    if not text:
        return None
    try:
        raw = base64.b64decode(str(text), validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) < 6 or len(raw) % 2:
        return None
    array = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 2).astype(np.float64) / _LEVELS
    return array.tolist()
