# 素材ピクセル ↔ キャンバスピクセルの変換 (ver5 resolve2 §2.5 / §4-4)
#
# 座標系は 3 つある。
#   素材ピクセル     検出結果。素材の解像度そのまま
#   キャンバスピクセル レンダリング / プレビュー (timeline.width x timeline.height)
#   正規化座標       囲みパスの保存形式 (キャンバスに対する 0.0〜1.0)
#
# 素材 → キャンバスの変換は renderer._video_filters の正規化チェーンで決まる。
#   scale=W:H:force_original_aspect_ratio=decrease, pad=W:H:(ow-iw)/2:(oh-ih)/2
# = 等倍率の縮小 + 中央寄せのレターボックス。
# 規格が一致する素材 (needs_normalize が False) では恒等変換になる。
#
# **この変換を知ってよいのはこのモジュールだけ**。他は常にキャンバス座標で話す。


# 素材 → キャンバスの写像を (倍率, 横オフセット, 縦オフセット) で返す。
# 素材とキャンバスの規格が同じとき (needs_normalize が False になる唯一の条件) は
# 倍率 1.0・オフセット 0 が自然に出るため、正規化の有無で分岐する必要はない。
def source_to_canvas_transform(media, canvas_width, canvas_height):
    src_w = float(getattr(media, "width", 0) or 0)
    src_h = float(getattr(media, "height", 0) or 0)
    canvas_w = float(canvas_width or 0)
    canvas_h = float(canvas_height or 0)
    if src_w <= 0 or src_h <= 0 or canvas_w <= 0 or canvas_h <= 0:
        # 寸法が分からない素材はそのままキャンバス座標とみなす (ずらすより安全)
        return 1.0, 0.0, 0.0

    # force_original_aspect_ratio=decrease = 収まる方の倍率
    scale = min(canvas_w / src_w, canvas_h / src_h)
    offset_x = (canvas_w - src_w * scale) / 2.0
    offset_y = (canvas_h - src_h * scale) / 2.0
    return scale, offset_x, offset_y


# 素材ピクセルの矩形 (x, y, w, h) をキャンバスピクセルへ直す
def source_rect_to_canvas(rect, transform):
    scale, offset_x, offset_y = transform
    x, y, width, height = rect
    return (
        x * scale + offset_x,
        y * scale + offset_y,
        width * scale,
        height * scale,
    )


# キャンバスピクセルの矩形を素材ピクセルへ戻す (囲み判定を素材側で行う場合に使う)
def canvas_rect_to_source(rect, transform):
    scale, offset_x, offset_y = transform
    if scale <= 0:
        return tuple(rect)
    x, y, width, height = rect
    return (
        (x - offset_x) / scale,
        (y - offset_y) / scale,
        width / scale,
        height / scale,
    )


# 正規化座標 (0.0〜1.0) の点列をキャンバスピクセルへ直す
def normalized_path_to_canvas(path, canvas_width, canvas_height):
    width = float(canvas_width or 0)
    height = float(canvas_height or 0)
    return [(float(px) * width, float(py) * height) for px, py in (path or [])]


# キャンバスピクセルの点列を正規化座標へ直す (保存用 / §5.2.2)
def canvas_path_to_normalized(path, canvas_width, canvas_height):
    width = float(canvas_width or 0) or 1.0
    height = float(canvas_height or 0) or 1.0
    return [(float(px) / width, float(py) / height) for px, py in (path or [])]


# 矩形を比率ぶん広げる (取りこぼし対策の margin / §5.4-4)
# 広げる量はキャンバス幅を基準にする。縦横で同じ太さの縁を付けるため。
def expand_rect(rect, margin_ratio, canvas_width):
    x, y, width, height = rect
    margin = float(canvas_width or 0) * float(margin_ratio or 0.0)
    return (x - margin, y - margin, width + margin * 2, height + margin * 2)


# 矩形をキャンバスの内側へ収める (負の座標や外へのはみ出しを切る)
def clamp_rect(rect, canvas_width, canvas_height):
    x, y, width, height = rect
    left = max(x, 0.0)
    top = max(y, 0.0)
    right = min(x + width, float(canvas_width))
    bottom = min(y + height, float(canvas_height))
    if right <= left or bottom <= top:
        return None
    return (left, top, right - left, bottom - top)


# 2 つの矩形の IoU (トラッキングと囲み判定で使う)
def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


# 小さい方の矩形のうち、もう一方と重なっている面積の割合 (0.0〜1.0)。
# 片方がもう片方にほぼ収まっていれば 1.0 に近づく (IoU は大きさが違うと低く出る)。
def containment(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    smaller = min(aw * ah, bw * bh)
    if right <= left or bottom <= top or smaller <= 0:
        return 0.0
    return (right - left) * (bottom - top) / smaller


# 点が多角形の内側にあるか (交差数判定 / 囲みパスの当て込みで使う)
def point_in_polygon(point, polygon):
    x, y = point
    inside = False
    count = len(polygon or [])
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # 辺が走査線をまたぐか判定し、交点の x が右側にあれば反転させる
        if (yi > y) != (yj > y):
            denom = (yj - yi) or 1e-12
            if x < (xj - xi) * (y - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


# 多角形の外接矩形 (x, y, w, h)
def polygon_bounds(polygon):
    if not polygon:
        return None
    xs = [float(p[0]) for p in polygon]
    ys = [float(p[1]) for p in polygon]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


# 矩形のうち多角形に覆われている面積の割合 (0.0〜1.0)
# 厳密な多角形クリッピングはせず、矩形を格子状に標本化して数える。
# 囲みの当て込み (hit_ratio) にはこの粒度で足りる。
def rect_coverage(rect, polygon, samples=8):
    x, y, width, height = rect
    if width <= 0 or height <= 0 or len(polygon or []) < 3:
        return 0.0
    hit = 0
    total = samples * samples
    for row in range(samples):
        py = y + height * (row + 0.5) / samples
        for col in range(samples):
            px = x + width * (col + 0.5) / samples
            if point_in_polygon((px, py), polygon):
                hit += 1
    return hit / float(total)
