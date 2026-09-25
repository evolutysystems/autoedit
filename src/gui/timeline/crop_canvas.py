# 縦動画の切り抜き枠を操作するキャンバス (ver5 resolve9 §5.7)
#
# ソースのフレームを敷き、その上に切り抜きの枠 (全体=1 つ / 分割=上下 2 つ) を出す。
#   枠の内側をドラッグ … 平行移動
#   四隅のハンドル     … 大きさを変える (分割は縦横比を固定)
# 座標は**すべてソースのピクセル**で扱う。切り抜きの指定がソース px のため、
# 画面と出力で座標系を揃えられる (ver5 resolve9 §5.2)。
#
# ぼかしのキャンバス (blur_canvas.py) と作りは似ているが、あちらは指定の種別・
# 複数選択・キーフレームを持つ。ここは枠 1〜2 個の移動と拡大縮小だけでよい (§3.4)。
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ...timeline import crop
from ...utils.logger import get_logger

_logger = get_logger(__name__)

# 枠の色 (全体 / 分割の上 / 分割の下)
_COLORS = (QColor(90, 170, 255), QColor(90, 170, 255), QColor(120, 210, 150))
_COLOR_HANDLE = QColor(255, 255, 255)
_COLOR_LABEL = QColor(255, 255, 255)

# ハンドルの大きさ (ソース px 換算はビューの拡大率で決める)
_HANDLE_PX = 12

_HANDLES = ("nw", "ne", "se", "sw")

_OP_NONE = ""
_OP_MOVE = "move"
_OP_RESIZE = "resize"


class CropCanvas(QGraphicsView):

    # 枠が動いた (移動・大きさ変更のたび)
    frames_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = (1920, 1080)
        self._canvas = (1080, 1920)
        self._mode = crop.MODE_SINGLE
        self._max_scale = 2.0
        self._frames = []            # [(x, y, w, h), ...] ソース px

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._frame_item = QGraphicsPixmapItem()
        self._frame_item.setZValue(0)
        self._scene.addItem(self._frame_item)
        self._items = []             # 枠・ハンドル・ラベル

        self._op = _OP_NONE
        self._op_index = -1
        self._op_handle = ""
        self._op_origin = None
        self._op_rect = None

        self._apply_source()

    # ------------------------------------------------------------------
    # 設定
    # ------------------------------------------------------------------

    # ソースの寸法・縦キャンバス・拡大率の上限を入れる
    def setup(self, source_size, canvas_size, max_scale):
        self._source = (max(int(source_size[0]), 2), max(int(source_size[1]), 2))
        self._canvas = (max(int(canvas_size[0]), 2), max(int(canvas_size[1]), 2))
        self._max_scale = max(float(max_scale or 1.0), 1.0)
        self._apply_source()

    # フレームを差し替える (rgb24 のバイト列)。None で「表示できません」の状態。
    def set_frame(self, width, height, rgb_bytes):
        if not rgb_bytes:
            self._frame_item.setPixmap(QPixmap())
            return
        image = QImage(rgb_bytes, int(width), int(height), int(width) * 3,
                       QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(image.copy())
        # フレームはソース寸法へ合わせる (以降の座標がソース px になる)
        if pixmap.width() != self._source[0] or pixmap.height() != self._source[1]:
            pixmap = pixmap.scaled(self._source[0], self._source[1],
                                   Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._frame_item.setPixmap(pixmap)

    def clear_frame(self):
        self._frame_item.setPixmap(QPixmap())

    # 切り抜き方を変える。枠は既定値へ作り直す。
    def set_mode(self, mode):
        self._mode = mode if mode in (crop.MODE_SINGLE, crop.MODE_SPLIT) else crop.MODE_SINGLE
        self._frames = self.default_frames()
        self._rebuild()
        self.frames_changed.emit()

    def mode(self):
        return self._mode

    # 枠を入れる (ソース px)
    def set_frames(self, frames):
        self._frames = [self._fit(tuple(rect), index)
                        for index, rect in enumerate(frames or [])]
        self._rebuild()

    # 枠を返す (ソース px)
    def frames(self):
        return [tuple(int(v) for v in rect) for rect in self._frames]

    # 既定の枠 (§5.7 初期値)
    def default_frames(self):
        width, height = self._source
        if self._mode != crop.MODE_SPLIT:
            size = min(self._canvas[0], width, height)
            return [self._fit(((width - size) // 2, (height - size) // 2, size, size), 0)]

        rects = []
        for index in range(2):
            max_w, max_h = crop.max_src_size(
                self._mode, index, self._canvas[0], self._canvas[1], width, height)
            x = (width - max_w) // 2
            y = 0 if index == 0 else max(height - max_h, 0)
            rects.append(self._fit((x, y, max_w, max_h), index))
        return rects

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------

    def _apply_source(self):
        self._scene.setSceneRect(0, 0, self._source[0], self._source[1])
        self.fit()

    def fit(self):
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event):       # noqa: N802 (Qt の命名に合わせる)
        super().resizeEvent(event)
        self.fit()

    def _rebuild(self):
        for item in self._items:
            self._scene.removeItem(item)
        self._items = []

        labels = ("", "上", "下") if self._mode == crop.MODE_SPLIT else ("",)
        for index, rect in enumerate(self._frames):
            color = _COLORS[min(index + (1 if self._mode == crop.MODE_SPLIT else 0),
                                len(_COLORS) - 1)]
            box = QGraphicsRectItem(QRectF(*rect))
            box.setPen(QPen(color, self._line_width()))
            box.setBrush(QBrush(Qt.transparent))
            box.setZValue(10)
            self._scene.addItem(box)
            self._items.append(box)

            if self._mode == crop.MODE_SPLIT and index + 1 < len(labels):
                label = QGraphicsSimpleTextItem(labels[index + 1])
                label.setBrush(QBrush(_COLOR_LABEL))
                font = label.font()
                font.setPixelSize(max(int(self._source[1] * 0.05), 12))
                label.setFont(font)
                label.setPos(rect[0] + 8, rect[1] + 8)
                label.setZValue(11)
                self._scene.addItem(label)
                self._items.append(label)

            for name in _HANDLES:
                center = _handle_center(rect, name)
                size = self._handle_size()
                handle = QGraphicsRectItem(
                    QRectF(center[0] - size / 2, center[1] - size / 2, size, size))
                handle.setPen(QPen(color, self._line_width()))
                handle.setBrush(QBrush(_COLOR_HANDLE))
                handle.setZValue(12)
                self._scene.addItem(handle)
                self._items.append(handle)

    # 画面上で同じ太さに見えるよう、ビューの拡大率から線幅を決める
    def _line_width(self):
        scale = self.transform().m11() or 1.0
        return max(2.0 / scale, 1.0)

    def _handle_size(self):
        scale = self.transform().m11() or 1.0
        return max(_HANDLE_PX / scale, 6.0)

    # ------------------------------------------------------------------
    # マウス操作
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):   # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        origin = (point.x(), point.y())

        handle = self._handle_at(origin)
        if handle is not None:
            self._op = _OP_RESIZE
            self._op_index, self._op_handle = handle
            self._op_origin = origin
            self._op_rect = self._frames[self._op_index]
            return

        index = self._frame_at(origin)
        if index is None:
            super().mousePressEvent(event)
            return
        self._op = _OP_MOVE
        self._op_index = index
        self._op_handle = ""
        self._op_origin = origin
        self._op_rect = self._frames[index]

    def mouseMoveEvent(self, event):    # noqa: N802
        if self._op == _OP_NONE:
            super().mouseMoveEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        rect = self._dragged_rect((point.x(), point.y()))
        if rect is None:
            return
        self._frames[self._op_index] = rect
        self._rebuild()
        self.frames_changed.emit()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._op == _OP_NONE:
            super().mouseReleaseEvent(event)
            return
        self._op = _OP_NONE
        self._op_index = -1
        self._op_handle = ""
        self._op_origin = None
        self._op_rect = None

    # ------------------------------------------------------------------
    # 計算
    # ------------------------------------------------------------------

    # ドラッグ中の枠 (ソース px)
    def _dragged_rect(self, current):
        if self._op_rect is None or self._op_origin is None:
            return None
        dx = current[0] - self._op_origin[0]
        dy = current[1] - self._op_origin[1]
        x, y, width, height = self._op_rect

        if self._op == _OP_MOVE:
            return self._fit((x + dx, y + dy, width, height), self._op_index)

        # 掴んだ角の対角は動かさない
        left, top, right, bottom = x, y, x + width, y + height
        if "n" in self._op_handle:
            top += dy
        if "s" in self._op_handle:
            bottom += dy
        if "w" in self._op_handle:
            left += dx
        if "e" in self._op_handle:
            right += dx
        return self._fit((min(left, right), min(top, bottom),
                          abs(right - left), abs(bottom - top)),
                         self._op_index, anchor=self._op_handle)

    # 枠を「比率・上下限・ソースの内側」へ収める
    def _fit(self, rect, index, anchor=""):
        x, y, width, height = (float(v) for v in rect)
        source_w, source_h = self._source

        min_w, min_h = crop.min_src_size(self._mode, index, self._canvas[0],
                                         self._canvas[1], self._max_scale)
        max_w, max_h = crop.max_src_size(self._mode, index, self._canvas[0],
                                         self._canvas[1], source_w, source_h)
        ratio = crop.aspect(self._mode, index, self._canvas[0], self._canvas[1])

        width = min(max(width, min_w), max_w)
        height = min(max(height, min_h), max_h)
        if ratio:
            # 比率固定 (分割)。幅を基準に高さを合わせ、入らなければ高さを基準にする。
            height = width / ratio
            if height > max_h:
                height = max_h
                width = height * ratio

        # 掴んだ角と反対側を固定する
        if "w" in anchor:
            x = x + (rect[2] - width)
        if "n" in anchor:
            y = y + (rect[3] - height)

        x = min(max(x, 0.0), max(source_w - width, 0.0))
        y = min(max(y, 0.0), max(source_h - height, 0.0))
        return (int(round(x)), int(round(y)), int(round(width)), int(round(height)))

    def _frame_at(self, point):
        # 後ろの枠 (分割の下) を先に拾う
        for index in reversed(range(len(self._frames))):
            x, y, width, height = self._frames[index]
            if x <= point[0] <= x + width and y <= point[1] <= y + height:
                return index
        return None

    def _handle_at(self, point):
        reach = self._handle_size()
        for index, rect in enumerate(self._frames):
            for name in _HANDLES:
                center = _handle_center(rect, name)
                if (abs(point[0] - center[0]) <= reach
                        and abs(point[1] - center[1]) <= reach):
                    return (index, name)
        return None


# ハンドルの中心 (ソース px)
def _handle_center(rect, name):
    x, y, width, height = rect
    cx = x if "w" in name else x + width
    cy = y if "n" in name else y + height
    return (cx, cy)
