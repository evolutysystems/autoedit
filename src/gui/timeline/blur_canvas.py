# ぼかし指定画面のキャンバス (ver5 resolve8 §5.11)
#
# フレームを表示し、その上に
#   ・指定の枠 (ボカす = 赤 / ボカさない = 緑 / 全面ぼかし = 画面の縁)
#   ・選択中の枠の 8 つのハンドル (大きさを変える)
#   ・ドラッグ中のゴムバンド (新しい囲み)
#   ・「ボカさない範囲 (緑)」の重ね塗り / 画像・字幕のオーバーレイ
# を重ねる。座標は**すべてキャンバス座標** (timeline.width x timeline.height) で扱い、
# 外へ出すときだけ正規化座標 (0.0〜1.0) へ直す。
#
# 操作 (resolve8 §5.10.5):
#   何も無い所をドラッグ   新しい囲み (area_drawn)
#   枠の内側をドラッグ     平行移動 → 離すと rect_committed
#   ハンドルをドラッグ     大きさを変える → 離すと rect_committed
#     Shift … 縦横比を保つ / Alt … 中心を動かさない
#   クリック               枠を選ぶ。同じ位置を続けて押すと重なった奥の枠へ移る
#   ダブルクリック         ボカす / ボカさない を入れ替える (spec_activated)
#   右クリック             枠のメニュー (context_requested)
#   Delete                 選択中の指定を削除 (delete_requested)
#   ← → / Shift+← → / Home / End / Ctrl+← →   コマ送り (navigate_requested)
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ...utils.logger import get_logger

_logger = get_logger(__name__)

# 枠の色 (ボカす / ボカさない / 全面ぼかし)
_COLOR_BLUR = QColor(232, 80, 80)
_COLOR_KEEP = QColor(80, 200, 120)
_COLOR_FRAME = QColor(232, 80, 80, 180)
# ハンドルとゴムバンド
_COLOR_HANDLE = QColor(255, 255, 255)
_COLOR_BAND = QColor(90, 170, 255)

# 囲みとみなす最小の動き (キャンバス px)。これより小さい動きはクリックとして扱う
_MIN_DRAG_EXTENT = 6.0
# 「同じ位置を続けてクリックした」とみなす距離 (キャンバス px)
_SAME_CLICK_DISTANCE = 8.0

# ハンドルの並び (角 4 + 辺 4)
_HANDLES = ("nw", "ne", "se", "sw", "n", "e", "s", "w")

# 操作の種類
_OP_NONE = ""
_OP_BAND = "band"
_OP_MOVE = "move"
_OP_RESIZE = "resize"


class BlurCanvas(QGraphicsView):

    # 枠が選ばれた (指定 ID / 空文字 = 選択解除)
    spec_selected = Signal(str)
    # 枠がダブルクリックされた (指定 ID)
    spec_activated = Signal(str)
    # 移動・大きさ変更を確定した (指定 ID, 正規化矩形)
    rect_committed = Signal(str, tuple)
    # 新しい囲みを描いた (正規化矩形)
    area_drawn = Signal(tuple)
    # 枠の上で右クリックされた (指定 ID, 画面座標)
    context_requested = Signal(str, QPoint)
    # Delete キーが押された
    delete_requested = Signal()
    # コマ送りの要求 ("prev"/"next"/"prev_fast"/"next_fast"/"home"/"end"/"prev_key"/"next_key")
    navigate_requested = Signal(str)

    def __init__(self, canvas_width, canvas_height, parent=None):
        super().__init__(parent)
        self._canvas_width = int(canvas_width)
        self._canvas_height = int(canvas_height)
        self._handle_px = 10
        self._min_size = max(self._canvas_width * 0.01, 4.0)

        self._scene = QGraphicsScene(self)
        self._scene.setSceneRect(0, 0, self._canvas_width, self._canvas_height)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.StrongFocus)

        self._frame_item = QGraphicsPixmapItem()
        self._frame_item.setZValue(0)
        self._scene.addItem(self._frame_item)

        self._overlay_item = QGraphicsPixmapItem()
        self._overlay_item.setZValue(5)
        self._scene.addItem(self._overlay_item)

        # 画像・動画・字幕のオーバーレイ (出力ではぼかしの上に来る層)
        self._overlay_items = []
        self._subtitle_cfg = {}
        self._font_profile = None
        self._overlay_cfg = {}

        self._spec_items = []       # 枠 + ラベル + ハンドル
        self._specs = []            # [{"id","rect","polygon","mode","label","is_frame","at_key"}]
        self._selected_id = ""
        self._last_click = None     # (点, そこで選んだ候補の番号)

        # 進行中の操作
        self._op = _OP_NONE
        self._op_spec = ""
        self._op_handle = ""
        self._op_origin = None
        self._op_rect = None        # 掴んだ時点の矩形 (キャンバス px)
        self._ghost = None          # 仮表示

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

    # 「ボカさない範囲 (緑)」の絵を重ねる (QImage / None で消す)
    def set_overlay(self, image):
        if image is None or image.isNull():
            self._overlay_item.setPixmap(QPixmap())
            return
        pixmap = QPixmap.fromImage(image).scaled(
            self._canvas_width, self._canvas_height, Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation)
        self._overlay_item.setPixmap(pixmap)

    # 画像・動画・字幕のオーバーレイを描き直す。
    # specs: [{"kind": "image"/"text", "element": クリップ, "pixmap": QPixmap (image のみ)}]
    # ここは位置を決める画面ではないため、**掴めない・選べない**状態で置く。
    def set_overlays(self, specs):
        for item in self._overlay_items:
            self._scene.removeItem(item)
        self._overlay_items = []
        for spec in specs or []:
            item = self._make_overlay_item(spec)
            if item is None:
                continue
            item.setAcceptedMouseButtons(Qt.NoButton)
            item.setFlag(QGraphicsPixmapItem.ItemIsMovable, False)
            item.setFlag(QGraphicsPixmapItem.ItemIsSelectable, False)
            # ぼかし (5) の上、枠 (10 以上) の下
            item.setZValue(6)
            self._scene.addItem(item)
            self._overlay_items.append(item)

    def _make_overlay_item(self, spec):
        from .preview_items import ImageOverlayItem, SubtitleOverlayItem   # noqa: PLC0415

        element = spec.get("element")
        canvas = (self._canvas_width, self._canvas_height)
        try:
            if spec.get("kind") == "text":
                return SubtitleOverlayItem(element, self._subtitle_cfg,
                                           self._font_profile, canvas)
            return ImageOverlayItem(element, spec.get("pixmap"), canvas,
                                    overlay_cfg=self._overlay_cfg)
        except Exception:                   # noqa: BLE001 (1 件の失敗で画面を落とさない)
            _logger.debug("オーバーレイの部品を作れませんでした", exc_info=True)
            return None

    # オーバーレイの描画に要る設定を渡す (指定画面が開くときに 1 回)
    def set_overlay_config(self, subtitle_cfg, font_profile, overlay_cfg):
        self._subtitle_cfg = subtitle_cfg
        self._font_profile = font_profile
        self._overlay_cfg = overlay_cfg

    # ハンドルの大きさ (画面 px) と囲みの最小の大きさ (キャンバス幅比) を設定する
    def set_metrics(self, handle_px, min_size_ratio):
        self._handle_px = max(int(handle_px), 4)
        self._min_size = max(float(min_size_ratio) * self._canvas_width, 4.0)

    # 指定の枠を描き直す。
    # specs: [{"id","rect"(キャンバス px),"polygon" or None,"mode","label","is_frame","at_key"}]
    def set_specs(self, specs, selected_id=""):
        for item in self._spec_items:
            self._scene.removeItem(item)
        self._spec_items = []
        self._specs = list(specs or [])
        self._selected_id = str(selected_id or "")
        pen_width = max(self._canvas_width / 400.0, 2.0)

        for entry in self._specs:
            rect = entry.get("rect")
            if not rect:
                continue
            color = _COLOR_FRAME if entry.get("is_frame") else (
                _COLOR_BLUR if entry.get("mode") == "blur" else _COLOR_KEEP)
            selected = str(entry.get("id")) == self._selected_id

            polygon = entry.get("polygon")
            if polygon and len(polygon) >= 3:
                item = QGraphicsPolygonItem(QPolygonF([QPointF(x, y) for x, y in polygon]))
            else:
                item = QGraphicsRectItem(QRectF(rect[0], rect[1], rect[2], rect[3]))
            pen = QPen(color, pen_width * (2.0 if selected else 1.0))
            if not entry.get("at_key", True):
                # キーフレームでない時刻 = 追従・補間で置かれている位置
                pen.setStyle(Qt.DashLine)
            item.setPen(pen)
            if not entry.get("is_frame"):
                item.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(),
                                            70 if selected else 40)))
            item.setZValue(12 if selected else 10)
            self._scene.addItem(item)
            self._spec_items.append(item)

            label = QGraphicsSimpleTextItem(str(entry.get("label") or ""))
            label.setBrush(QBrush(color))
            font = label.font()
            font.setPointSizeF(max(self._canvas_width / 90.0, 10.0))
            font.setBold(True)
            label.setFont(font)
            label.setPos(rect[0], max(rect[1] - font.pointSizeF() * 1.6, 0))
            label.setZValue(14)
            self._scene.addItem(label)
            self._spec_items.append(label)

            if selected and not entry.get("is_frame"):
                self._add_handles(rect)

    # 選択中の枠へハンドルを 8 個置く
    def _add_handles(self, rect):
        size = self._handle_size()
        for name in _HANDLES:
            center = _handle_center(rect, name)
            item = QGraphicsEllipseItem(QRectF(center[0] - size / 2.0, center[1] - size / 2.0,
                                               size, size))
            item.setPen(QPen(QColor(40, 40, 40), max(size * 0.12, 1.0)))
            item.setBrush(QBrush(_COLOR_HANDLE))
            item.setZValue(13)
            self._scene.addItem(item)
            self._spec_items.append(item)

    # ハンドルの 1 辺 (キャンバス座標)。表示倍率に合わせて画面上の大きさを一定に保つ
    def _handle_size(self):
        scale = float(self.transform().m11()) or 1.0
        return max(self._handle_px / max(scale, 1e-6), 4.0)

    def selected_id(self):
        return self._selected_id

    # 今描いている枠の一覧
    def specs(self):
        return list(self._specs)

    # 表示倍率を枠へ合わせる (ウィンドウの大きさが変わるたびに呼ぶ)
    def fit(self):
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event):       # noqa: N802 (Qt の命名に合わせる)
        super().resizeEvent(event)
        self.fit()

    # ------------------------------------------------------------------
    # マウス操作
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):   # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.MouseFocusReason)
        point = self.mapToScene(event.position().toPoint())
        origin = (point.x(), point.y())
        self._op_origin = origin

        handle = self._handle_at(origin)
        if handle is not None:
            self._op = _OP_RESIZE
            self._op_spec = self._selected_id
            self._op_handle = handle
            self._op_rect = self._rect_of(self._selected_id)
            return

        keys = self._ids_at(origin)
        if keys:
            spec_id = self._selected_id if self._selected_id in keys else keys[0]
            self._op = _OP_MOVE
            self._op_spec = spec_id
            self._op_handle = ""
            self._op_rect = self._rect_of(spec_id)
            return

        self._op = _OP_BAND
        self._op_spec = ""
        self._op_rect = None
        self._show_ghost((origin[0], origin[1], 0.0, 0.0), band=True)

    def mouseMoveEvent(self, event):    # noqa: N802
        if self._op == _OP_NONE:
            super().mouseMoveEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        current = (point.x(), point.y())
        if self._op == _OP_BAND:
            self._show_ghost(_rect_between(self._op_origin, current), band=True)
            return
        rect = self._dragged_rect(current, event.modifiers())
        if rect is not None:
            self._show_ghost(rect, band=False)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._op == _OP_NONE:
            super().mouseReleaseEvent(event)
            return
        operation = self._op
        spec_id = self._op_spec
        origin = self._op_origin
        point = self.mapToScene(event.position().toPoint())
        current = (point.x(), point.y())
        modifiers = event.modifiers()
        moved = _extent(origin, current) >= _MIN_DRAG_EXTENT
        rect = self._dragged_rect(current, modifiers) if operation != _OP_BAND else None
        self._clear_ghost()
        self._op = _OP_NONE
        self._op_spec = ""
        self._op_handle = ""
        self._op_rect = None

        if operation == _OP_BAND:
            if not moved:
                self._click(origin)
                return
            band = _clamp(_rect_between(origin, current), self._canvas_width,
                          self._canvas_height, self._min_size)
            if band is None:
                return
            self.area_drawn.emit(self._normalized(band))
            return

        if not moved or rect is None:
            self._click(origin)         # 掴んだだけ = 選択
            return
        self.rect_committed.emit(spec_id, self._normalized(rect))

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        keys = self._ids_at((point.x(), point.y()))
        if keys:
            spec_id = self._selected_id if self._selected_id in keys else keys[0]
            self.spec_activated.emit(spec_id)

    def contextMenuEvent(self, event):  # noqa: N802
        point = self.mapToScene(event.pos())
        keys = self._ids_at((point.x(), point.y()))
        if not keys:
            return
        spec_id = self._selected_id if self._selected_id in keys else keys[0]
        if spec_id != self._selected_id:
            self.spec_selected.emit(spec_id)
        self.context_requested.emit(spec_id, event.globalPos())

    def keyPressEvent(self, event):     # noqa: N802
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_requested.emit()
            return
        if key == Qt.Key_Left or key == Qt.Key_Right:
            forward = key == Qt.Key_Right
            if modifiers & Qt.ControlModifier:
                self.navigate_requested.emit("next_key" if forward else "prev_key")
            elif modifiers & Qt.ShiftModifier:
                self.navigate_requested.emit("next_fast" if forward else "prev_fast")
            else:
                self.navigate_requested.emit("next" if forward else "prev")
            return
        if key == Qt.Key_Home:
            self.navigate_requested.emit("home")
            return
        if key == Qt.Key_End:
            self.navigate_requested.emit("end")
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # 内部 — 当たり判定と仮表示
    # ------------------------------------------------------------------

    # その点に重なっている枠の ID (小さい枠 = 手前にあるものから順)。
    # 全面ぼかしは画面全体を覆うため**当たり判定に入れない** (触れなくする)。
    def _ids_at(self, point):
        x, y = point
        hits = []
        for entry in self._specs:
            rect = entry.get("rect")
            spec_id = str(entry.get("id") or "")
            if not rect or not spec_id or entry.get("is_frame"):
                continue
            if rect[0] <= x <= rect[0] + rect[2] and rect[1] <= y <= rect[1] + rect[3]:
                hits.append((float(rect[2]) * float(rect[3]), spec_id))
        hits.sort(key=lambda item: item[0])
        return [spec_id for _area, spec_id in hits]

    # 選択中の枠のハンドルに当たっていればその名前 (無ければ None)
    def _handle_at(self, point):
        rect = self._rect_of(self._selected_id)
        if rect is None:
            return None
        reach = self._handle_size()
        for name in _HANDLES:
            center = _handle_center(rect, name)
            if abs(point[0] - center[0]) <= reach and abs(point[1] - center[1]) <= reach:
                return name
        return None

    def _rect_of(self, spec_id):
        for entry in self._specs:
            if str(entry.get("id")) == str(spec_id) and not entry.get("is_frame"):
                rect = entry.get("rect")
                if rect:
                    return tuple(float(v) for v in rect)
        return None

    # ドラッグ中の矩形 (キャンバス px)。掴めていなければ None。
    def _dragged_rect(self, current, modifiers):
        if self._op_rect is None or self._op_origin is None:
            return None
        dx = current[0] - self._op_origin[0]
        dy = current[1] - self._op_origin[1]
        x, y, width, height = self._op_rect
        if self._op == _OP_MOVE:
            moved = (x + dx, y + dy, width, height)
        else:
            moved = _resized(self._op_rect, self._op_handle, dx, dy,
                             keep_ratio=bool(modifiers & Qt.ShiftModifier),
                             keep_center=bool(modifiers & Qt.AltModifier),
                             minimum=self._min_size)
        return _clamp(moved, self._canvas_width, self._canvas_height, self._min_size)

    # 仮表示の枠を出す
    def _show_ghost(self, rect, band):
        self._clear_ghost()
        if rect is None:
            return
        item = QGraphicsRectItem(QRectF(rect[0], rect[1], rect[2], rect[3]))
        pen = QPen(_COLOR_BAND, max(self._canvas_width / 300.0, 2.0))
        pen.setStyle(Qt.DashLine if band else Qt.SolidLine)
        item.setPen(pen)
        item.setBrush(QBrush(QColor(90, 170, 255, 50)))
        item.setZValue(20)
        self._scene.addItem(item)
        self._ghost = item

    def _clear_ghost(self):
        if self._ghost is not None:
            self._scene.removeItem(self._ghost)
            self._ghost = None

    # クリックで選ぶ。同じ位置を続けて押すと、重なった奥の枠へ順に移る
    def _click(self, point):
        keys = self._ids_at(point)
        if not keys:
            self._last_click = None
            self.spec_selected.emit("")
            return
        index = 0
        if self._last_click is not None and point is not None:
            (last_x, last_y), last_index = self._last_click
            if abs(point[0] - last_x) + abs(point[1] - last_y) <= _SAME_CLICK_DISTANCE:
                index = (last_index + 1) % len(keys)
        self._last_click = (point, index)
        self.spec_selected.emit(keys[index])

    # キャンバス px の矩形を正規化座標へ
    def _normalized(self, rect):
        width = float(self._canvas_width) or 1.0
        height = float(self._canvas_height) or 1.0
        return (rect[0] / width, rect[1] / height, rect[2] / width, rect[3] / height)


# ハンドルの中心 (キャンバス座標)
def _handle_center(rect, name):
    x, y, width, height = (float(v) for v in rect)
    mid_x = x + width / 2.0
    mid_y = y + height / 2.0
    return {
        "nw": (x, y), "ne": (x + width, y), "se": (x + width, y + height),
        "sw": (x, y + height), "n": (mid_x, y), "e": (x + width, mid_y),
        "s": (mid_x, y + height), "w": (x, mid_y),
    }[name]


# ハンドルを dx, dy 動かした矩形。
# 掴んだ辺の動きを幅・高さの変化へ直し、掴んでいない側の辺を固定する。
#   keep_ratio  : 角のハンドルで縦横比を保つ (Shift)
#   keep_center : 中心を動かさない (Alt)
def _resized(rect, handle, dx, dy, keep_ratio, keep_center, minimum):
    x, y, width, height = (float(v) for v in rect)
    sx = -1 if "w" in handle else (1 if "e" in handle else 0)
    sy = -1 if "n" in handle else (1 if "s" in handle else 0)

    scale = 2.0 if keep_center else 1.0
    new_width = max(width + dx * sx * scale, minimum) if sx else width
    new_height = max(height + dy * sy * scale, minimum) if sy else height

    if keep_ratio and sx and sy and width > 0 and height > 0:
        ratio = height / width
        if abs(new_width - width) * ratio >= abs(new_height - height):
            new_height = max(new_width * ratio, minimum)
            new_width = new_height / ratio
        else:
            new_width = max(new_height / ratio, minimum)
            new_height = new_width * ratio

    if keep_center:
        new_x = x + width / 2.0 - new_width / 2.0
        new_y = y + height / 2.0 - new_height / 2.0
    else:
        # 西 (左) を掴んだら右辺を固定、北 (上) を掴んだら下辺を固定する
        new_x = x if sx >= 0 else x + width - new_width
        new_y = y if sy >= 0 else y + height - new_height
    return (new_x, new_y, new_width, new_height)


# 2 点からできる矩形
def _rect_between(origin, current):
    x = min(origin[0], current[0])
    y = min(origin[1], current[1])
    return (x, y, abs(current[0] - origin[0]), abs(current[1] - origin[1]))


# 矩形をキャンバスの内側へ収める。小さすぎれば None。
def _clamp(rect, canvas_width, canvas_height, minimum):
    if rect is None:
        return None
    x, y, width, height = (float(v) for v in rect)
    if width < minimum or height < minimum:
        return None
    width = min(width, float(canvas_width))
    height = min(height, float(canvas_height))
    x = min(max(x, 0.0), float(canvas_width) - width)
    y = min(max(y, 0.0), float(canvas_height) - height)
    return (x, y, width, height)


# 2 点の隔たり (クリックとドラッグの区別に使う)
def _extent(origin, current):
    if origin is None or current is None:
        return 0.0
    return max(abs(current[0] - origin[0]), abs(current[1] - origin[1]))
