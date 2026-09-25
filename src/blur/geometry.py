# 素材ピクセル ↔ キャンバスピクセル ↔ 正規化座標の変換 (ver5 resolve8 §4-3)
#
# 座標系は 3 つある。
#   素材ピクセル       検出・追従の作業座標。素材の解像度そのまま
#   キャンバスピクセル  レンダリング / プレビュー (timeline.width x timeline.height)
#   正規化座標         **保存形式**。キャンバスに対する 0.0〜1.0
#
# 素材 → キャンバスの変換は renderer._video_filters の正規化チェーンで決まる。
#   scale=W:H:force_original_aspect_ratio=decrease, pad=W:H:(ow-iw)/2:(oh-ih)/2
# = 等倍率の縮小 + 中央寄せのレターボックス。
# 規格が一致する素材 (needs_normalize が False) では恒等変換になる。
#
# **この変換を知ってよいのはこのモジュールだけ**。他は常にキャンバス座標か正規化座標で話す。


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


# キャンバスピクセルの矩形を素材ピクセルへ戻す
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


# 正規化座標 (0.0〜1.0) の矩形をキャンバスピクセルへ直す
def normalized_rect_to_canvas(rect, canvas_width, canvas_height):
    width = float(canvas_width or 0)
    height = float(canvas_height or 0)
    x, y, w, h = (float(v) for v in rect)
    return (x * width, y * height, w * width, h * height)


# キャンバスピクセルの矩形を正規化座標へ直す (保存用)
def canvas_rect_to_normalized(rect, canvas_width, canvas_height):
    width = float(canvas_width or 0) or 1.0
    height = float(canvas_height or 0) or 1.0
    x, y, w, h = (float(v) for v in rect)
    return (x / width, y / height, w / width, h / height)


# 正規化座標の矩形を素材ピクセルへ直す (追従の作業座標へ持ち込むとき)
def normalized_rect_to_source(rect, transform, canvas_width, canvas_height):
    return canvas_rect_to_source(
        normalized_rect_to_canvas(rect, canvas_width, canvas_height), transform)


# 素材ピクセルの矩形を正規化座標へ直す (追従の結果を保存するとき)
def source_rect_to_normalized(rect, transform, canvas_width, canvas_height):
    return canvas_rect_to_normalized(
        source_rect_to_canvas(rect, transform), canvas_width, canvas_height)


# 正規化座標の矩形を 0.0〜1.0 の中へ収める (画面の外へドラッグされたとき)
def clamp_normalized_rect(rect, min_size=0.001):
    x, y, width, height = (float(v) for v in rect)
    width = min(max(width, float(min_size)), 1.0)
    height = min(max(height, float(min_size)), 1.0)
    x = min(max(x, 0.0), 1.0 - width)
    y = min(max(y, 0.0), 1.0 - height)
    return (x, y, width, height)


# 矩形を比率ぶん広げる (取りこぼし対策の margin)
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


# 2 つの矩形の IoU (追従で検出枠を当てるときに使う)
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
