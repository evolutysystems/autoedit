# 位相相関で 2 枚の絵のずれを求める道具 (ver5 resolve8 §5.1 / 旧 region_tracker.py の前半)
#
# 位相相関は「振幅を捨てて位相だけを見る」ため、明るさの変化に強く、
# カメラのパン・手ぶれのような平行移動をはっきり 1 点のピークとして出せる。
#
# 一致の判定には**ピークの高さではなく PSR** (ピークが裾野の標準偏差の何倍突き出ているか) を使う。
# ピークの高さは絵の変化量で大きく下がり、人が動くだけの固定カメラでも 0.07 程度になる
# (ずれは正しく 0,0 と出ている)。PSR は同じ場面で 50 以上、無関係な動画で 15 以下だった。
#
# 限界: 拡大縮小・回転には追従しない。ver5 resolve8 では**位置だけを追う**と決めたので、
# これで足りる (大きさはキーフレームが持つ / §3.3)。
import numpy as np

# PSR を求めるとき、ピークの周りのこの半径 (px) は裾野として数えない
_PSR_GUARD_PX = 5


# RGB 画像をグレースケールの float 配列にする (ITU-R BT.601 の重み)
def to_gray(image):
    array = np.asarray(image, dtype=np.float32)
    if array.ndim == 2:
        return array
    return array[:, :, 0] * 0.299 + array[:, :, 1] * 0.587 + array[:, :, 2] * 0.114


# 最近傍で縮小する (速度のため。相関は縮小した絵で取る)
def shrink(image, scale):
    if scale >= 1.0:
        return image
    height, width = image.shape[:2]
    new_h = max(int(height * scale), 8)
    new_w = max(int(width * scale), 8)
    rows = np.clip((np.arange(new_h) * (height / float(new_h))).astype(np.int32), 0, height - 1)
    cols = np.clip((np.arange(new_w) * (width / float(new_w))).astype(np.int32), 0, width - 1)
    return image[rows][:, cols]


# 2 枚の同じ大きさの絵から平行移動量を求める。
# 戻り値 (dx, dy, ピークの強さ 0.0〜1.0, PSR)。
def phase_correlate(reference, target):
    if reference.shape != target.shape or reference.size == 0:
        return 0.0, 0.0, 0.0, 0.0

    height, width = reference.shape
    # 窓を掛けて端の不連続 (FFT が周期だと思い込む段差) による偽ピークを抑える
    window = np.outer(np.hanning(height), np.hanning(width)).astype(np.float32)
    a = np.fft.rfft2((reference - reference.mean()) * window)
    b = np.fft.rfft2((target - target.mean()) * window)

    cross = a * np.conj(b)
    magnitude = np.abs(cross)
    # 振幅で割る = 位相だけを残す
    spectrum = cross / np.maximum(magnitude, 1e-9)
    correlation = np.fft.irfft2(spectrum, s=reference.shape)

    index = int(np.argmax(correlation))
    peak_y, peak_x = divmod(index, width)
    peak = float(correlation.flat[index])

    # 折り返し: 半分を超えるずれは負の方向として読む
    dy = peak_y - height if peak_y > height // 2 else peak_y
    dx = peak_x - width if peak_x > width // 2 else peak_x
    return float(dx), float(dy), max(min(peak, 1.0), 0.0), _psr(correlation, peak_y, peak_x, peak)


# ピークの周り (_PSR_GUARD_PX) を除いた裾野に対し、ピークが標準偏差の何倍高いか
def _psr(correlation, peak_y, peak_x, peak):
    height, width = correlation.shape
    rows = [(peak_y + d) % height for d in range(-_PSR_GUARD_PX, _PSR_GUARD_PX + 1)]
    cols = [(peak_x + d) % width for d in range(-_PSR_GUARD_PX, _PSR_GUARD_PX + 1)]
    sidelobe = np.ones(correlation.shape, dtype=bool)
    sidelobe[np.ix_(rows, cols)] = False
    values = correlation[sidelobe]
    if values.size == 0:
        return 0.0
    return max((peak - float(values.mean())) / max(float(values.std()), 1e-9), 0.0)
