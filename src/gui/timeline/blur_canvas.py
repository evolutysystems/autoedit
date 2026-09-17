# ぼかし指定画面のキャンバス (ver5 resolve2 §5.6.2 / §5.6.3)
#
# フレームを表示し、その上に
#   ・検出枠 (ぼかす = 赤 / ぼかさない = 緑)
#   ・ドラッグで描いた自由曲線の囲み
# を重ねる。座標は**すべてキャンバス座標** (timeline.width x timeline.height) で扱い、
# 素材ピクセルとの変換は blur.geometry だけが知る (§4-4)。
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ...utils.logger import get_logger

_logger = get_logger(__name__)

# 枠の色 (ぼかす / ぼかさない / 主役)
_COLOR_BLUR = QColor(232, 80, 80)
_COLOR_KEEP = QColor(80, 200, 120)
_COLOR_MAIN = QColor(240, 200, 80)
# 描画中の囲み
_COLOR_PATH = QColor(90, 170, 255)

# 囲みとみなす最小の点数 (これ未満の点はクリックとみなして捨てる)
_MIN_PATH_POINTS = 3


class BlurCanvas(QGraphicsView):

    # 囲みを描き終えた (キャンバス座標の点列)
    path_drawn = Signal(list)
    # 検出枠がクリックされた (人物 ID)
    box_clicked = Signal(str)

    def __init__(self, canvas_width, canvas_height, parent=None):
        super().__init__(parent)
        self._canvas_width = int(canvas_width)
        self._canvas_height = int(canvas_height)

        self._scene = QGraphicsScene(self)
        self._scene.setSceneRect(0, 0, self._canvas_width, self._canvas_height)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._frame_item = QGraphicsPixmapItem()
        self._frame_item.setZValue(0)
        self._scene.addItem(self._frame_item)

        self._box_items = []        # 検出枠 (矩形 + ラベル)
        self._boxes = []            # [{"identity","rect","mode","main"}]
        self._path_item = None      # 描画中の囲み
        self._points = []
        self._drawing = False

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------

    # フレームを差し替える (rgb24 のバイト列)
    def set_frame(self, width, height, rgb_bytes):
        if not rgb_bytes:
            self._frame_item.setPixmap(QPixmap())
            return
        image = QImage(rgb_bytes, width, height, width * 3, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(image.copy())
        # フレームはキャンバス寸法へ合わせて表示する (以降の座標がキャンバス座標になる)
        if width != self._canvas_width or height != self._canvas_height:
            pixmap = pixmap.scaled(self._canvas_width, self._canvas_height,
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._frame_item.setPixmap(pixmap)
        # レターボックスの余白ぶんを中央へ寄せる
        self._frame_item.setOffset(
            (self._canvas_width - pixmap.width()) / 2.0,
            (self._canvas_height - pixmap.height()) / 2.0)

    # 「表示できません」の状態にする
    def clear_frame(self):
        self._frame_item.setPixmap(QPixmap())

    # 検出枠を描き直す。
    # boxes: [{"identity": 人物 ID, "rect": (x, y, w, h), "mode": "blur"/"keep",
    #          "main": 主役か, "label": 表示名}]
    def set_boxes(self, boxes):
        for item in self._box_items:
            self._scene.removeItem(item)
        self._box_items = []
        self._boxes = list(boxes or [])

        for entry in self._boxes:
            rect = entry.get("rect")
            if not rect:
                continue
            color = _COLOR_BLUR if entry.get("mode") == "blur" else _COLOR_KEEP
            if entry.get("main"):
                color = _COLOR_MAIN

            item = QGraphicsRectItem(QRectF(rect[0], rect[1], rect[2], rect[3]))
            item.setPen(QPen(color, max(self._canvas_width / 400.0, 2.0)))
            item.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), 40)))
            item.setZValue(10)
            self._scene.addItem(item)
            self._box_items.append(item)

            label = QGraphicsSimpleTextItem(str(entry.get("label") or ""))
            label.setBrush(QBrush(color))
            font = label.font()
            font.setPointSizeF(max(self._canvas_width / 90.0, 10.0))
            font.setBold(True)
            label.setFont(font)
            label.setPos(rect[0], max(rect[1] - font.pointSizeF() * 1.6, 0))
            label.setZValue(11)
            self._scene.addItem(label)
            self._box_items.append(label)

    # 今描いている検出枠の一覧 (囲みの当て込みで使う)
    def boxes(self):
        return list(self._boxes)

    # 描いた囲みを消す
    def clear_path(self):
        if self._path_item is not None:
            self._scene.removeItem(self._path_item)
            self._path_item = None
        self._points = []

    # 表示倍率を枠へ合わせる (ウィンドウの大きさが変わるたびに呼ぶ)
    def fit(self):
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event):       # noqa: N802 (Qt の命名に合わせる)
        super().resizeEvent(event)
        self.fit()

    # ------------------------------------------------------------------
    # 囲む操作 (R5)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):   # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._drawing = True
        self.clear_path()
        point = self.mapToScene(event.position().toPoint())
        self._points = [(point.x(), point.y())]

        path = QPainterPath(QPointF(point))
        self._path_item = QGraphicsPathItem(path)
        self._path_item.setPen(QPen(_COLOR_PATH, max(self._canvas_width / 300.0, 2.0),
                                    Qt.DashLine))
        self._path_item.setBrush(QBrush(QColor(90, 170, 255, 50)))
        self._path_item.setZValue(20)
        self._scene.addItem(self._path_item)

    def mouseMoveEvent(self, event):    # noqa: N802
        if not self._drawing or self._path_item is None:
            super().mouseMoveEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        self._points.append((point.x(), point.y()))
        path = self._path_item.path()
        path.lineTo(QPointF(point))
        self._path_item.setPath(path)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if not self._drawing:
            super().mouseReleaseEvent(event)
            return
        self._drawing = False

        if len(self._points) < _MIN_PATH_POINTS:
            # 点を打っただけ = 枠のクリックとみなす (囲まずに 1 人だけ選べる)
            self.clear_path()
            identity = self._identity_at(self._points[-1] if self._points else None)
            if identity:
                self.box_clicked.emit(identity)
            return

        # 離したら自動で閉じる (§5.6.3-2)
        path = self._path_item.path()
        path.closeSubpath()
        self._path_item.setPath(path)
        self.path_drawn.emit(list(self._points))

    # その点を含む検出枠の人物 ID (無ければ空文字)
    def _identity_at(self, point):
        if not point:
            return ""
        x, y = point
        for entry in self._boxes:
            rect = entry.get("rect")
            if not rect:
                continue
            if rect[0] <= x <= rect[0] + rect[2] and rect[1] <= y <= rect[1] + rect[3]:
                return str(entry.get("identity") or "")
        return ""
