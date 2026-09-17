# 輪郭・囲みの形の扱い (ver5 resolve3 §3.3 / §5.2.1)
#
# 形は**枠 (x, y, w, h) に対する相対座標** (u, v は 0.0〜1.0) で持つ。
# 枠の補間 (移動・拡大) に形を当てはめるだけで、形が枠と一緒に動くようにするため。
#
#   ・打ち直し   : 輪郭を周長で等間隔の N 点にする
#   ・始点揃え   : 一番上 (同じ高さなら一番左) の点を先頭にし、時計回りへそろえる
#                 → 前後 2 つの輪郭を点どうしで対応させて混ぜられる
#   ・量子化     : u, v を 0〜255 の 1 バイトにして base64 で保存する (1 点 2 バイト)
#
# 依存は numpy だけ (resolve3 Q8: ライブラリを増やさない)。
import base64

import numpy as np

# 量子化の段数 (1 バイト)
_LEVELS = 255


# 点列を周長に沿って等間隔の count 点へ打ち直す (閉じた多角形として扱う)
def resample(points, count):
    array = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(array) < 3 or count < 3:
        return array.tolist()

    closed = np.vstack([array, array[:1]])
    segments = np.diff(closed, axis=0)
    lengths = np.hypot(segments[:, 0], segments[:, 1])
    total = float(lengths.sum())
    if total <= 1e-9:
        return np.repeat(array[:1], count, axis=0).tolist()

    cumulative = np.concatenate([[0.0], np.cumsum(lengths)])
    targets = np.linspace(0.0, total, count, endpoint=False)
    result = np.empty((count, 2), dtype=np.float64)
    index = 0
    for out, distance in enumerate(targets):
        while index < len(lengths) - 1 and cumulative[index + 1] <= distance:
            index += 1
        span = lengths[index]
        ratio = (distance - cumulative[index]) / span if span > 1e-12 else 0.0
        result[out] = closed[index] + segments[index] * ratio
    return result.tolist()


# 始点を一番上の点へ回し、向きを時計回り (画面座標で符号付き面積が正) へそろえる
def align(points):
    array = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(array) < 3:
        return array.tolist()
    if _signed_area(array) < 0:
        array = array[::-1]
    # 一番上 (y 最小)。同じ高さなら左 (x 最小) を先頭にする
    start = int(np.lexsort((array[:, 0], array[:, 1]))[0])
    return np.roll(array, -start, axis=0).tolist()


# 輪郭を保存用の形にする: 打ち直し → 始点揃え
def normalize(points, count):
    return align(resample(points, count))


# 画面座標での符号付き面積 (y が下向きのため、時計回りが正になる)
def _signed_area(array):
    x = array[:, 0]
    y = array[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


# 多角形の面積 (絶対値)
def area(points):
    array = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(array) < 3:
        return 0.0
    return abs(_signed_area(array))


# 多角形を外側へ distance だけ膨らませた多角形を返す (おおまかな余白 / ver5 resolve3 §3.3.3)。
# 各頂点を、隣り合う 2 辺の外向き法線の平均方向へずらす。尖った頂点で飛び出しすぎないよう、
# ずらす量は distance の 2 倍までに抑える。画像に MaxFilter を掛けたり太い線を描いたりするより
# 桁違いに速い (マスク生成で毎フレーム呼ぶため)。
def offset(points, distance):
    array = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(array) < 3 or distance <= 0:
        return array.tolist()
    # 画面座標 (y 下向き) で符号付き面積が正 = 時計回り。外向き法線は辺ベクトルを左回り 90 度
    sign = 1.0 if _signed_area(array) >= 0 else -1.0
    edges = np.roll(array, -1, axis=0) - array
    lengths = np.maximum(np.hypot(edges[:, 0], edges[:, 1]), 1e-9)
    normals = np.stack([edges[:, 1], -edges[:, 0]], axis=1) / lengths[:, None] * sign
    vertex = normals + np.roll(normals, 1, axis=0)
    norm = np.hypot(vertex[:, 0], vertex[:, 1])
    direction = vertex / np.maximum(norm, 1e-9)[:, None]
    # 2 辺の法線がなす角が大きいほど、角の外側を覆うには長くずらす必要がある (1 / cos(半角))
    cos_half = np.clip(norm / 2.0, 0.5, 1.0)
    return (array + direction * (distance / cos_half)[:, None]).tolist()


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


# 前後 2 つの相対輪郭を ratio (0=前, 1=後) で混ぜる。点数が違えば近い方をそのまま返す。
#
# 始点は「一番上の点」にそろえてあるが、姿勢が変わると一番上の点が髪から肩へ移るなど、
# 始点が輪郭の別の場所へ飛ぶ。そのまま点どうしを混ぜると形がねじれて崩れる
# (「ぼかさない」人物の顔が削れずにぼけた / 実素材で確認)。
# 混ぜる前に、後ろの輪郭を回して**前の輪郭と点の距離の合計が最小になる対応**に合わせる。
def interpolate(before, after, ratio):
    if before is None:
        return after
    if after is None or len(before) != len(after):
        return before if ratio < 0.5 else after
    a = np.asarray(before, dtype=np.float64)
    b = np.roll(np.asarray(after, dtype=np.float64), -_best_shift(a, np.asarray(after)), axis=0)
    return (a + (b - a) * float(ratio)).tolist()


# b を何点ぶん回すと a との距離の二乗和が最小になるか (点数ぶん全部を試す。64 点なら一瞬)
def _best_shift(a, b):
    count = len(a)
    b = np.asarray(b, dtype=np.float64)
    # shift ごとの距離の二乗和 = |a|² + |b|² − 2 Σ a[i]·b[i+shift]。相互相関を FFT で一度に求める
    cross = np.zeros(count)
    for axis in range(2):
        cross += np.real(np.fft.ifft(np.conj(np.fft.fft(a[:, axis])) * np.fft.fft(b[:, axis])))
    return int(np.argmax(cross))


# 相対輪郭を base64 の文字列にする (1 点 = u, v の 2 バイト)
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
