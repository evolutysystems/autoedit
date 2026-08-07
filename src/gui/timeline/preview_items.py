# プレビュー上のオーバーレイ要素 (docs/request/ver3/resolve.md §6.5)
# 画像・字幕を QGraphicsItem として重ね、クリックで選択・ドラッグで位置変更できるようにする。
# シーン座標はキャンバス実寸 (例 1920x1080) で持つため、正規化座標 (-1〜1) と
# レンダリング側の座標計算が一致する。
#
# 字幕の描画は Qt による近似であり libass と完全一致しない (§6.4-4 に明記した仕様)。
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsSimpleTextItem,
)

# ASS テンキー配置 → 水平/垂直の寄せ (resolve_export と同じ対応)
_ALIGN_LEFT = (1, 4, 7)
_ALIGN_RIGHT = (3, 6, 9)
_ALIGN_BOTTOM = (1, 2, 3)
_ALIGN_TOP = (7, 8, 9)

# 選択枠の色
_SELECTION_COLOR = QColor(255, 214, 92)


# ASS 色文字列 (&HAABBGGRR) / HTML (#RRGGBB) を QColor へ変換する
def ass_color_to_qcolor(value, default="#FFFFFF"):
    text = str(value or "").strip()
    if text.startswith("#") and len(text) == 7:
        return QColor(text)
    if text.upper().startswith("&H"):
        digits = text[2:].rstrip("&")
        if len(digits) == 8:
            alpha = 255 - int(digits[0:2], 16)
            bb, gg, rr = digits[2:4], digits[4:6], digits[6:8]
            color = QColor(int(rr, 16), int(gg, 16), int(bb, 16))
            color.setAlpha(alpha)
            return color
        if len(digits) == 6:
            bb, gg, rr = digits[0:2], digits[2:4], digits[4:6]
            return QColor(int(rr, 16), int(gg, 16), int(bb, 16))
    return QColor(default)


# 字幕設定の配置(alignment)+余白から、位置未指定時の描画位置を求める
# 戻り値は (x_px, y_px, 水平アンカー, 垂直アンカー)。
def default_subtitle_anchor(eff_cfg, canvas_width, canvas_height):
    alignment = int(eff_cfg.get("alignment", 2) or 2)
    margin_l = int(eff_cfg.get("margin_l", 40) or 0)
    margin_r = int(eff_cfg.get("margin_r", 40) or 0)
    margin_v = int(eff_cfg.get("margin_v", 60) or 0)

    if alignment in _ALIGN_LEFT:
        x, h_anchor = float(margin_l), "left"
    elif alignment in _ALIGN_RIGHT:
        x, h_anchor = float(canvas_width - margin_r), "right"
    else:
        x, h_anchor = canvas_width / 2.0, "center"

    if alignment in _ALIGN_BOTTOM:
        y, v_anchor = float(canvas_height - margin_v), "bottom"
    elif alignment in _ALIGN_TOP:
        y, v_anchor = float(margin_v), "top"
    else:
        y, v_anchor = canvas_height / 2.0, "middle"
    return x, y, h_anchor, v_anchor


# 正規化座標 (-1〜1・Y は上が正) をキャンバスのピクセル座標へ
def normalized_to_pixel(x, y, canvas_width, canvas_height):
    return ((float(x) + 1.0) * canvas_width / 2.0,
            (1.0 - float(y)) * canvas_height / 2.0)


# キャンバスのピクセル座標を正規化座標へ
def pixel_to_normalized(px, py, canvas_width, canvas_height):
    return (px / (canvas_width / 2.0) - 1.0,
            1.0 - py / (canvas_height / 2.0))


# 選択・ドラッグに対応するオーバーレイ要素の共通振る舞い
class _OverlayMixin:

    # 位置確定を伝える先 (PreviewPanel が設定する)
    move_finished = None

    def _init_overlay(self, clip):
        self.clip_id = clip.id
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setCursor(Qt.OpenHandCursor)

    # ドラッグ確定時に 1 回だけ通知する (ドラッグ中は積まない = 履歴を汚さない / §6.5-1)
    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if callable(self.move_finished):
            center = self.sceneBoundingRect().center()
            self.move_finished(self.clip_id, center.x(), center.y())

    # 選択枠を描く
    def _paint_selection(self, painter):
        if not self.isSelected():
            return
        painter.setPen(QPen(_SELECTION_COLOR, 2, Qt.DashLine))
        painter.setBrush(QBrush(Qt.NoBrush))
        painter.drawRect(self.boundingRect())


# 画像・動画オーバーレイ (V2 以降のクリップ)
class ImageOverlayItem(_OverlayMixin, QGraphicsPixmapItem):

    def __init__(self, clip, pixmap, canvas_size, parent=None):
        super().__init__(parent)
        self._init_overlay(clip)
        self._canvas_size = canvas_size
        target_width = max(int(canvas_size[0] * float(clip.transform.scale or 1.0)), 2)
        if not pixmap.isNull():
            scaled = pixmap.scaledToWidth(target_width, Qt.SmoothTransformation)
            self.setPixmap(scaled)
        self.setOpacity(float(clip.transform.opacity or 1.0))
        # transform.x/y はクリップ中心を指す
        cx, cy = normalized_to_pixel(
            clip.transform.x or 0.0, clip.transform.y or 0.0, *canvas_size)
        rect = self.boundingRect()
        self.setPos(cx - rect.width() / 2.0, cy - rect.height() / 2.0)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        self._paint_selection(painter)


# 字幕オーバーレイ (Qt による近似描画 / §6.4-4)
class SubtitleOverlayItem(_OverlayMixin, QGraphicsSimpleTextItem):

    def __init__(self, clip, eff_cfg, font_profile, canvas_size, parent=None):
        # ASS の改行 \N を実改行へ戻して表示する
        text = str(clip.text or "").replace("\\N", "\n")
        role = str(clip.role or "streamer")
        if role == "comment" and font_profile.comment_label:
            text = f"{font_profile.comment_label}\n{text}"
        super().__init__(text, parent)
        self._init_overlay(clip)
        self._canvas_size = canvas_size

        font = QFont(clip.font or font_profile.family)
        # ASS の Fontsize は PlayRes 単位 = キャンバスのピクセル
        font.setPixelSize(int(clip.font_size or font_profile.size or 48))
        font.setBold(bool(font_profile.bold))
        font.setItalic(bool(font_profile.italic))
        font.setUnderline(bool(font_profile.underline))
        self.setFont(font)

        fill = font_profile.role_colors.get(role, font_profile.color_hex)
        self.setBrush(QBrush(ass_color_to_qcolor(fill)))
        outline = font_profile.role_outline_colors.get(role, font_profile.outline_color)
        width = max(int(font_profile.outline_width or 0), 0)
        if width > 0:
            pen = QPen(ass_color_to_qcolor(outline, "#000000"))
            pen.setWidth(width)
            pen.setJoinStyle(Qt.RoundJoin)
            self.setPen(pen)
        else:
            self.setPen(QPen(Qt.NoPen))

        self._place(clip, eff_cfg, canvas_size)

    # 位置指定があればその中心へ、無ければ設定の配置(alignment)+余白へ置く
    def _place(self, clip, eff_cfg, canvas_size):
        rect = self.boundingRect()
        if clip.transform.is_positioned():
            cx, cy = normalized_to_pixel(clip.transform.x, clip.transform.y, *canvas_size)
            self.setPos(cx - rect.width() / 2.0, cy - rect.height() / 2.0)
            return
        x, y, h_anchor, v_anchor = default_subtitle_anchor(eff_cfg, *canvas_size)
        if h_anchor == "left":
            left = x
        elif h_anchor == "right":
            left = x - rect.width()
        else:
            left = x - rect.width() / 2.0
        if v_anchor == "top":
            top = y
        elif v_anchor == "bottom":
            top = y - rect.height()
        else:
            top = y - rect.height() / 2.0
        self.setPos(left, top)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        self._paint_selection(painter)


# ベース映像フレームのアイテム (選択・移動は不可)
class BaseFrameItem(QGraphicsPixmapItem):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setZValue(-1000)
        self.setFlag(QGraphicsItem.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.ItemIsMovable, False)

    # 空フレーム (取得できないとき) は黒で塗る
    def show_blank(self, canvas_size):
        pixmap = QPixmap(int(canvas_size[0]), int(canvas_size[1]))
        pixmap.fill(QColor(0, 0, 0))
        self.setPixmap(pixmap)

    def bounding_canvas(self, canvas_size):
        return QRectF(0, 0, float(canvas_size[0]), float(canvas_size[1]))
