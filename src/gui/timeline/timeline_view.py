# Timeline 表示・操作 (docs/request/ver3/resolve.md §6.6 / §6.7 / §6.8)
# DaVinci Resolve / Premiere Pro のような一般的なノンリニア編集ソフトの Timeline を参考に、
# トラック (Video / Audio / Subtitle)・クリップ編集・ズーム・横スクロール・
# 再生ヘッド移動・メディアの D&D を提供する。
#
# 描画と入力のみを担い、モデルの変更は必ず TimelineController (= コマンド) 経由で行う。
import os

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSlider,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollBar,
    QVBoxLayout,
    QWidget,
)

from ...timeline import commands, media_probe
from ...timeline.model import (
    ORIGIN_ENDING,
    ORIGIN_OPENING,
    AudioClip,
    SubtitleClip,
)
from ...utils.logger import get_logger

_logger = get_logger(__name__)

# ルーラの目盛り候補 (秒)。ズームに応じてラベルが詰まらない最小の刻みを選ぶ (§6.7)。
_TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600]

# ルーラの高さ
RULER_HEIGHT = 26

# クリップの色 (種別で見分けられるようにする)
_COLOR_BODY = QColor(58, 108, 168)          # 本編 (編集点由来)
_COLOR_OPENING = QColor(126, 87, 168)       # オープニング
_COLOR_ENDING = QColor(96, 87, 168)         # エンディング
_COLOR_OVERLAY = QColor(40, 132, 130)       # 追加メディア
_COLOR_SUBTITLE = QColor(178, 130, 46)      # 字幕
_COLOR_AUDIO = QColor(56, 130, 78)          # 音声
_COLOR_DISABLED = QColor(96, 96, 96)        # 無効化されたクリップ
_COLOR_SELECTED_BORDER = QColor(255, 214, 92)
_COLOR_TRACK_BG = QColor(38, 38, 40)
_COLOR_TRACK_BG_ALT = QColor(44, 44, 47)
_COLOR_GRID = QColor(64, 64, 68)
_COLOR_PLAYHEAD = QColor(232, 84, 84)
_COLOR_TEXT = QColor(236, 236, 236)
_COLOR_RULER_BG = QColor(30, 30, 32)

# ドラッグの種類
_DRAG_NONE = 0
_DRAG_PLAYHEAD = 1
_DRAG_MOVE = 2
_DRAG_TRIM_LEFT = 3
_DRAG_TRIM_RIGHT = 4


# 秒を "H:MM:SS" / "MM:SS" 形式にする (ルーラのラベル用)
def format_time(seconds, with_hours=None):
    seconds = max(float(seconds), 0.0)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if with_hours or hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# 秒を "H:MM:SS.cc" 形式にする (時刻表示用)
def format_time_precise(seconds):
    seconds = max(float(seconds), 0.0)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


# トラックの表示行を上から順に組み立てる
# 上から: 字幕 → 追加映像 (index 降順) → ベース映像 → 音声
def build_rows(timeline, cfg):
    ui = cfg["ui"]
    rows = []
    y = 0
    for track in sorted(timeline.subtitle_tracks(), key=lambda t: t.index):
        height = ui["subtitle_track_height_px"]
        rows.append({"track": track, "y": y, "height": height})
        y += height
    base = timeline.base_video_track()
    overlays = [t for t in timeline.video_tracks() if base is None or t.id != base.id]
    for track in sorted(overlays, key=lambda t: t.index, reverse=True):
        height = ui["track_height_px"]
        rows.append({"track": track, "y": y, "height": height})
        y += height
    if base is not None:
        rows.append({"track": base, "y": y, "height": ui["track_height_px"]})
        y += ui["track_height_px"]
    for track in sorted(timeline.audio_tracks(), key=lambda t: t.index):
        height = ui["track_height_px"]
        rows.append({"track": track, "y": y, "height": height})
        y += height
    return rows


# ==================================================================
# タイムルーラ
# ==================================================================

class TimelineRuler(QWidget):

    # ルーラのクリック/ドラッグで再生ヘッドを動かす
    seek_requested = Signal(float)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._offset = 0.0
        self.setFixedHeight(RULER_HEIGHT)
        self.setMouseTracking(True)

    # 横スクロール量 (秒) を受け取る
    def set_offset(self, offset_sec):
        self._offset = float(offset_sec)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), _COLOR_RULER_BG)
        zoom = self._controller.zoom()
        if zoom <= 0:
            return
        step = _pick_tick_step(zoom, self._controller.cfg["ui"]["ruler_min_label_px"])
        font = QFont(painter.font())
        font.setPointSizeF(max(font.pointSizeF() - 1.0, 7.0))
        painter.setFont(font)

        visible_sec = self.width() / zoom
        first = int(self._offset // step) * step
        total = self._controller.timeline.duration_sec()
        with_hours = total >= 3600

        painter.setPen(QPen(_COLOR_GRID))
        value = first
        while value <= self._offset + visible_sec + step:
            x = int((value - self._offset) * zoom)
            painter.setPen(QPen(_COLOR_GRID))
            painter.drawLine(x, RULER_HEIGHT - 8, x, RULER_HEIGHT)
            painter.setPen(QPen(_COLOR_TEXT))
            painter.drawText(x + 3, RULER_HEIGHT - 10, format_time(value, with_hours))
            value += step

        # 再生ヘッド
        head_x = int((self._controller.playhead() - self._offset) * zoom)
        if 0 <= head_x <= self.width():
            painter.setPen(QPen(_COLOR_PLAYHEAD, 2))
            painter.drawLine(head_x, 0, head_x, RULER_HEIGHT)

    def mousePressEvent(self, event):
        self._emit_seek(event.position().x())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._emit_seek(event.position().x())

    def _emit_seek(self, x):
        zoom = self._controller.zoom()
        if zoom > 0:
            self.seek_requested.emit(self._offset + x / zoom)


# ラベルが詰まらない最小の目盛り刻みを選ぶ (固定刻みをハードコードしない / §6.7)
def _pick_tick_step(zoom_px_per_sec, min_label_px):
    for step in _TICK_STEPS:
        if step * zoom_px_per_sec >= min_label_px:
            return step
    return _TICK_STEPS[-1]


# ==================================================================
# トラックヘッダ
# ==================================================================

class TrackHeaderWidget(QWidget):

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self.setFixedWidth(controller.cfg["ui"]["header_width_px"])

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), _COLOR_RULER_BG)
        rows = build_rows(self._controller.timeline, self._controller.cfg)
        for index, row in enumerate(rows):
            rect = QRect(0, row["y"], self.width(), row["height"])
            painter.fillRect(
                rect, _COLOR_TRACK_BG if index % 2 == 0 else _COLOR_TRACK_BG_ALT)
            painter.setPen(QPen(_COLOR_GRID))
            painter.drawLine(0, row["y"] + row["height"], self.width(),
                             row["y"] + row["height"])
            painter.setPen(QPen(_COLOR_TEXT))
            painter.drawText(rect.adjusted(8, 0, -4, 0),
                             Qt.AlignVCenter | Qt.AlignLeft, row["track"].id)

    # トラック行の合計高さ
    def rows_height(self):
        rows = build_rows(self._controller.timeline, self._controller.cfg)
        return rows[-1]["y"] + rows[-1]["height"] if rows else 0


# ==================================================================
# Timeline 本体
# ==================================================================

class TimelineView(QWidget):

    # クリップのダブルクリック (字幕編集などを開く)
    clip_activated = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._offset = 0.0                 # 表示左端の時刻 (秒)
        self._drag = _DRAG_NONE
        self._drag_clip_id = None
        self._drag_anchor_sec = 0.0
        self._drag_origin = 0.0
        self._drag_preview = None          # ドラッグ中の仮位置 (確定まで模型には触らない)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(160)

    # ------------------------------------------------------------------
    # 座標変換
    # ------------------------------------------------------------------

    def offset(self):
        return self._offset

    def set_offset(self, offset_sec):
        self._offset = max(float(offset_sec), 0.0)
        self.update()

    def sec_to_x(self, sec):
        return (float(sec) - self._offset) * self._controller.zoom()

    def x_to_sec(self, x):
        zoom = self._controller.zoom()
        return self._offset + (float(x) / zoom if zoom > 0 else 0.0)

    # 表示できる秒数
    def visible_sec(self):
        zoom = self._controller.zoom()
        return self.width() / zoom if zoom > 0 else 0.0

    # ------------------------------------------------------------------
    # 描画
    # ------------------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), _COLOR_TRACK_BG)
        timeline = self._controller.timeline
        rows = build_rows(timeline, self._controller.cfg)

        for index, row in enumerate(rows):
            self._paint_row(painter, row, index, timeline)

        # 再生ヘッド (最前面)
        head_x = int(self.sec_to_x(self._controller.playhead()))
        if -1 <= head_x <= self.width() + 1:
            painter.setPen(QPen(_COLOR_PLAYHEAD, 2))
            painter.drawLine(head_x, 0, head_x, self.height())

    def _paint_row(self, painter, row, index, timeline):
        track = row["track"]
        rect = QRect(0, row["y"], self.width(), row["height"])
        painter.fillRect(rect, _COLOR_TRACK_BG if index % 2 == 0 else _COLOR_TRACK_BG_ALT)
        painter.setPen(QPen(_COLOR_GRID))
        painter.drawLine(0, rect.bottom(), self.width(), rect.bottom())

        selected = set(self._controller.selected_ids())
        for clip in track.clips:
            span = self._clip_span(timeline, clip)
            if span is None:
                continue
            start, duration = span
            x1 = self.sec_to_x(start)
            x2 = self.sec_to_x(start + duration)
            if x2 < -2 or x1 > self.width() + 2:
                continue  # 画面外はスキップ (長尺でも描画コストが増えない)
            self._paint_clip(painter, clip, track, row, x1, x2, clip.id in selected)

    # クリップの (開始, 尺) を返す。音声は V1 からの導出値 (R18)。
    def _clip_span(self, timeline, clip):
        if isinstance(clip, AudioClip):
            start = clip.timeline_start(timeline)
            duration = clip.duration(timeline)
            if start is None or duration is None:
                return None
            return start, duration
        # ドラッグ中の仮位置を優先して描く (確定まで模型は書き換えない)
        if self._drag_preview and self._drag_preview["id"] == clip.id:
            return self._drag_preview["start"], self._drag_preview["duration"]
        return clip.timeline_start, clip.duration

    def _paint_clip(self, painter, clip, track, row, x1, x2, is_selected):
        top = row["y"] + 3
        height = row["height"] - 7
        rect = QRect(int(x1), top, max(int(x2 - x1), 2), height)
        color = self._clip_color(clip, track)
        painter.fillRect(rect, QBrush(color))
        painter.setPen(QPen(_COLOR_SELECTED_BORDER if is_selected else color.darker(160),
                            2 if is_selected else 1))
        painter.drawRect(rect)

        # ラベル (幅に余裕があるときだけ)
        if rect.width() >= 40:
            painter.setPen(QPen(_COLOR_TEXT))
            painter.drawText(rect.adjusted(5, 0, -5, 0),
                             Qt.AlignVCenter | Qt.AlignLeft, self._clip_label(clip, track))

    def _clip_color(self, clip, track):
        if isinstance(clip, AudioClip):
            return _COLOR_AUDIO if not clip.muted else _COLOR_DISABLED
        if isinstance(clip, SubtitleClip):
            return _COLOR_SUBTITLE if clip.use else _COLOR_DISABLED
        if not clip.enabled:
            return _COLOR_DISABLED
        origin = clip.origin_type()
        if origin == ORIGIN_OPENING:
            return _COLOR_OPENING
        if origin == ORIGIN_ENDING:
            return _COLOR_ENDING
        base = self._controller.timeline.base_video_track()
        if base is not None and track.id == base.id:
            return _COLOR_BODY
        return _COLOR_OVERLAY

    def _clip_label(self, clip, track):
        if isinstance(clip, SubtitleClip):
            return clip.text.replace("\\N", " ")[:40]
        if isinstance(clip, AudioClip):
            video = self._controller.timeline.clip_by_id(clip.link_clip)
            suffix = " (ミュート)" if clip.muted else ""
            return (video.id if video else "") + suffix
        origin = clip.origin_type()
        if origin == ORIGIN_OPENING:
            return "オープニング"
        if origin == ORIGIN_ENDING:
            return "エンディング"
        media = self._controller.timeline.media_by_id(clip.media_id)
        if media is not None and origin not in ("silence_cut", ""):
            return os.path.basename(media.path)
        return clip.id

    # ------------------------------------------------------------------
    # ヒットテスト
    # ------------------------------------------------------------------

    # 座標にあるクリップとトラックを返す ((clip, track, row) / 無ければ None)
    def clip_at_pos(self, pos):
        timeline = self._controller.timeline
        for row in build_rows(timeline, self._controller.cfg):
            if not (row["y"] <= pos.y() < row["y"] + row["height"]):
                continue
            for clip in reversed(row["track"].clips):
                span = self._clip_span(timeline, clip)
                if span is None:
                    continue
                start, duration = span
                x1 = self.sec_to_x(start)
                x2 = self.sec_to_x(start + duration)
                if x1 - 1 <= pos.x() <= x2 + 1:
                    return clip, row["track"], row
            return None
        return None

    # クリップの端 (トリムハンドル) に触れているか
    def _edge_at(self, clip, pos):
        handle = self._controller.cfg["ui"]["trim_handle_px"]
        span = self._clip_span(self._controller.timeline, clip)
        if span is None:
            return None
        start, duration = span
        x1 = self.sec_to_x(start)
        x2 = self.sec_to_x(start + duration)
        # 極端に細いクリップでは移動を優先する (トリムで掴めなくなるのを避ける)
        if x2 - x1 < handle * 3:
            return None
        if abs(pos.x() - x1) <= handle:
            return commands.EDGE_LEFT
        if abs(pos.x() - x2) <= handle:
            return commands.EDGE_RIGHT
        return None

    # ------------------------------------------------------------------
    # マウス操作 (§6.6)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        hit = self.clip_at_pos(pos)

        if hit is None:
            # 空き領域 → 再生ヘッド移動 + 選択解除
            self._controller.clear_selection()
            self._drag = _DRAG_PLAYHEAD
            self._controller.set_playhead(self.x_to_sec(pos.x()))
            return

        clip, track, _row = hit
        # 音声クリップの操作はリンク元 V1 クリップへ転送する (R18 / §6.3.4)
        target = clip
        if isinstance(clip, AudioClip):
            linked = self._controller.timeline.clip_by_id(clip.link_clip)
            if linked is None:
                return
            target = linked

        if event.modifiers() & Qt.ControlModifier:
            self._controller.toggle_select(target.id)
        elif target.id not in self._controller.selected_ids():
            self._controller.select([target.id])

        if track.locked:
            return

        edge = self._edge_at(clip, pos)
        span = self._clip_span(self._controller.timeline, target)
        if span is None:
            return
        self._drag_clip_id = target.id
        self._drag_anchor_sec = self.x_to_sec(pos.x())
        self._drag_origin = span[0]
        self._drag_preview = {"id": target.id, "start": span[0], "duration": span[1]}
        if edge == commands.EDGE_LEFT:
            self._drag = _DRAG_TRIM_LEFT
        elif edge == commands.EDGE_RIGHT:
            self._drag = _DRAG_TRIM_RIGHT
        else:
            self._drag = _DRAG_MOVE

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()

        if self._drag == _DRAG_NONE:
            self._update_cursor(pos)
            return

        if self._drag == _DRAG_PLAYHEAD:
            self._controller.set_playhead(self.x_to_sec(pos.x()))
            return

        clip = self._controller.timeline.clip_by_id(self._drag_clip_id)
        if clip is None:
            return
        sec = self._snap(self.x_to_sec(pos.x()), exclude_id=clip.id)

        if self._drag == _DRAG_MOVE:
            delta = self.x_to_sec(pos.x()) - self._drag_anchor_sec
            start = self._snap(max(self._drag_origin + delta, 0.0), exclude_id=clip.id)
            self._drag_preview = {"id": clip.id, "start": max(start, 0.0),
                                  "duration": clip.duration}
        elif self._drag == _DRAG_TRIM_LEFT:
            start = min(sec, clip.timeline_end - self._controller.cfg["min_clip_sec"])
            self._drag_preview = {"id": clip.id, "start": start,
                                  "duration": clip.timeline_end - start}
        else:
            end = max(sec, clip.timeline_start + self._controller.cfg["min_clip_sec"])
            self._drag_preview = {"id": clip.id, "start": clip.timeline_start,
                                  "duration": end - clip.timeline_start}
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drag == _DRAG_NONE:
            return
        drag = self._drag
        clip_id = self._drag_clip_id
        preview = self._drag_preview
        self._drag = _DRAG_NONE
        self._drag_clip_id = None
        self._drag_preview = None

        if drag == _DRAG_PLAYHEAD or clip_id is None or preview is None:
            self.update()
            return

        # ドラッグ確定時に 1 回だけコマンドを積む (ドラッグ中は履歴を汚さない)
        if drag == _DRAG_MOVE:
            self._controller.move_clip(clip_id, preview["start"])
        elif drag == _DRAG_TRIM_LEFT:
            self._controller.trim_clip(clip_id, commands.EDGE_LEFT, preview["start"])
        else:
            self._controller.trim_clip(
                clip_id, commands.EDGE_RIGHT, preview["start"] + preview["duration"])
        self.update()

    def mouseDoubleClickEvent(self, event):
        hit = self.clip_at_pos(event.position().toPoint())
        if hit is not None:
            self.clip_activated.emit(hit[0].id)

    # 端にカーソルを乗せたらリサイズ形状にする
    def _update_cursor(self, pos):
        hit = self.clip_at_pos(pos)
        if hit is not None and self._edge_at(hit[0], pos) is not None:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.unsetCursor()

    # ドラッグ中の吸着 (再生ヘッド・他クリップの端・先頭 / §6.6)
    def _snap(self, sec, exclude_id=None):
        if not self._controller.cfg["snap_enabled"]:
            return sec
        zoom = self._controller.zoom()
        if zoom <= 0:
            return sec
        threshold = self._controller.cfg["snap_threshold_px"] / zoom
        candidates = [0.0, self._controller.playhead()]
        timeline = self._controller.timeline
        for track in timeline.tracks:
            if track.is_audio():
                continue
            for clip in track.clips:
                if clip.id == exclude_id:
                    continue
                candidates.append(clip.timeline_start)
                candidates.append(clip.timeline_end)
        best = min(candidates, key=lambda c: abs(c - sec), default=sec)
        return best if abs(best - sec) <= threshold else sec

    # ------------------------------------------------------------------
    # 右クリックメニュー (§6.6)
    # ------------------------------------------------------------------

    def contextMenuEvent(self, event):
        pos = event.pos()
        hit = self.clip_at_pos(pos)
        menu = QMenu(self)

        if hit is None:
            menu.addAction("ここへ再生ヘッドを移動",
                           lambda: self._controller.set_playhead(self.x_to_sec(pos.x())))
            menu.exec(event.globalPos())
            return

        clip, track, _row = hit
        if isinstance(clip, AudioClip):
            self._build_audio_menu(menu, clip)
            menu.exec(event.globalPos())
            return

        if clip.id not in self._controller.selected_ids():
            self._controller.select([clip.id])

        at_sec = self.x_to_sec(pos.x())
        menu.addAction("ここで分割", lambda: self._controller.split_at_playhead(
            clip.id, at_sec))
        menu.addSeparator()
        # 削除は 2 種類を常に並べて出す (回答 Q5 / R17)
        menu.addAction("削除", lambda: self._controller.delete_selected(ripple=False))
        menu.addAction("リップル削除", lambda: self._controller.delete_selected(ripple=True))
        menu.addSeparator()
        layer = menu.addMenu("レイヤー")
        base = self._controller.timeline.base_video_track()
        is_base_clip = base is not None and track.id == base.id
        for label, direction in (("最前面", commands.LAYER_TOP), ("前面", commands.LAYER_UP),
                                 ("背面", commands.LAYER_DOWN),
                                 ("最背面", commands.LAYER_BOTTOM)):
            action = layer.addAction(
                label, lambda d=direction: self._controller.change_layer(clip.id, d))
            # ベース (V1) のクリップは常に最背面固定のため対象外
            action.setEnabled(not is_base_clip)
        menu.addSeparator()
        used = clip.use if isinstance(clip, SubtitleClip) else clip.enabled
        menu.addAction("使用しない" if used else "使用する",
                       lambda: self._controller.set_clip_enabled(clip.id, not used))
        menu.exec(event.globalPos())

    # 音声クリップのメニュー: 削除は出さない (V1 側から消す / §6.3.4)
    def _build_audio_menu(self, menu, clip):
        menu.addAction("ミュート解除" if clip.muted else "ミュート",
                       lambda: self._controller.set_audio_muted(clip.id, not clip.muted))
        gain = menu.addMenu("音量")
        for label, value in (("+6 dB", 6.0), ("+3 dB", 3.0), ("0 dB", 0.0),
                             ("-3 dB", -3.0), ("-6 dB", -6.0)):
            gain.addAction(label, lambda v=value: self._controller.set_audio_gain(clip.id, v))

    # ------------------------------------------------------------------
    # メディアの D&D (§6.8)
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event):
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._dropped_paths(event)
        if not paths:
            event.ignore()
            return
        pos = event.position().toPoint()
        start = max(self.x_to_sec(pos.x()), 0.0)
        hit_row = None
        for row in build_rows(self._controller.timeline, self._controller.cfg):
            if row["y"] <= pos.y() < row["y"] + row["height"]:
                hit_row = row
                break
        track_id = hit_row["track"].id if hit_row and hit_row["track"].is_video() else None

        cfg = self._controller.cfg["media"]
        settings = self._controller.settings
        cursor = start
        for path in paths:
            media = media_probe.probe(path, "", settings, cfg)
            duration = media_probe.default_clip_duration(media, cfg, settings)
            if duration <= 0:
                _logger.warning("尺を取得できないため追加しません: %s", path)
                continue
            if self._controller.add_media(media, cursor, duration, track_id=track_id) is None:
                _logger.warning("メディアを追加できませんでした: %s", path)
                continue
            cursor += duration
        event.acceptProposedAction()

    # 受理できるローカルファイルのパス列を返す
    def _dropped_paths(self, event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        cfg = self._controller.cfg["media"]
        paths = []
        for url in mime.urls():
            local = url.toLocalFile()
            if local and media_probe.is_supported(local, cfg):
                paths.append(local)
        return paths


# ==================================================================
# Timeline パネル (ルーラ + ヘッダ + 本体 + ズーム/スクロール)
# ==================================================================

class TimelinePanel(QWidget):

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._build_ui()
        controller.timeline_changed.connect(self._on_timeline_changed)
        controller.playhead_moved.connect(self._on_playhead_moved)
        controller.selection_changed.connect(lambda _c: self.view.update())
        controller.view_changed.connect(self._on_view_changed)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header_width = self._controller.cfg["ui"]["header_width_px"]

        # ルーラ行 (ヘッダ幅ぶんの余白 + ルーラ)
        ruler_row = QHBoxLayout()
        ruler_row.setContentsMargins(0, 0, 0, 0)
        ruler_row.setSpacing(0)
        spacer = QWidget()
        spacer.setFixedWidth(header_width)
        spacer.setFixedHeight(RULER_HEIGHT)
        ruler_row.addWidget(spacer)
        self.ruler = TimelineRuler(self._controller)
        self.ruler.seek_requested.connect(self._controller.set_playhead)
        ruler_row.addWidget(self.ruler, 1)
        root.addLayout(ruler_row)

        # トラック行 (ヘッダ + 本体)
        track_row = QHBoxLayout()
        track_row.setContentsMargins(0, 0, 0, 0)
        track_row.setSpacing(0)
        self.header = TrackHeaderWidget(self._controller)
        track_row.addWidget(self.header)
        self.view = TimelineView(self._controller)
        track_row.addWidget(self.view, 1)
        root.addLayout(track_row, 1)

        # 横スクロールバー
        self.scrollbar = QScrollBar(Qt.Horizontal)
        self.scrollbar.valueChanged.connect(self._on_scroll)
        scroll_row = QHBoxLayout()
        scroll_row.setContentsMargins(0, 0, 0, 0)
        scroll_row.setSpacing(0)
        scroll_spacer = QWidget()
        scroll_spacer.setFixedWidth(header_width)
        scroll_row.addWidget(scroll_spacer)
        scroll_row.addWidget(self.scrollbar, 1)
        root.addLayout(scroll_row)

        # ズーム操作
        zoom_row = QHBoxLayout()
        zoom_row.setContentsMargins(6, 2, 6, 2)
        minus = QPushButton("－")
        minus.setFixedWidth(32)
        minus.setToolTip("Timeline を縮小 (Ctrl + ホイールでも操作できます)")
        minus.clicked.connect(lambda: self._controller.zoom_by(1 / 1.25))
        plus = QPushButton("＋")
        plus.setFixedWidth(32)
        plus.setToolTip("Timeline を拡大 (Ctrl + ホイールでも操作できます)")
        plus.clicked.connect(lambda: self._controller.zoom_by(1.25))
        fit = QPushButton("全体")
        fit.setToolTip("Timeline 全体が収まるまで縮小します")
        fit.clicked.connect(self.zoom_to_fit)
        zoom_row.addWidget(minus)
        zoom_row.addWidget(plus)
        zoom_row.addWidget(fit)
        self.hint_label = QLabel(
            "クリップ: ドラッグで移動 / 端をドラッグで長さ変更 / Ctrl+B で分割 / "
            "Delete で削除・Shift+Delete でリップル削除")
        self.hint_label.setStyleSheet("color:#888;")
        zoom_row.addWidget(self.hint_label, 1)
        root.addLayout(zoom_row)

        self._sync_scrollbar()

    # ------------------------------------------------------------------
    # 表示同期
    # ------------------------------------------------------------------

    def _on_timeline_changed(self):
        self._sync_scrollbar()
        self.header.update()
        self.view.update()

    def _on_view_changed(self):
        self._sync_scrollbar()
        self.ruler.update()
        self.view.update()

    def _on_playhead_moved(self, sec):
        self._ensure_visible(sec)
        self.ruler.update()
        self.view.update()

    def _on_scroll(self, value):
        offset = value / 1000.0
        self.view.set_offset(offset)
        self.ruler.set_offset(offset)

    # スクロール範囲を Timeline 全長へ合わせる (単位はミリ秒)
    def _sync_scrollbar(self):
        total = self._controller.timeline.duration_sec()
        visible = self.view.visible_sec()
        maximum = max(total - visible, 0.0)
        self.scrollbar.blockSignals(True)
        self.scrollbar.setRange(0, int(maximum * 1000))
        self.scrollbar.setPageStep(int(max(visible, 0.1) * 1000))
        self.scrollbar.setSingleStep(int(max(visible / 10.0, 0.05) * 1000))
        self.scrollbar.setValue(int(self.view.offset() * 1000))
        self.scrollbar.blockSignals(False)

    # 再生ヘッドが可視域外に出たら追従スクロールする (§6.7)
    def _ensure_visible(self, sec):
        visible = self.view.visible_sec()
        offset = self.view.offset()
        if sec < offset or sec > offset + visible:
            self.scrollbar.setValue(int(max(sec - visible / 3.0, 0.0) * 1000))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_scrollbar()

    # Timeline 全体が収まるズームにする
    def zoom_to_fit(self):
        total = self._controller.timeline.duration_sec()
        width = max(self.view.width() - 8, 100)
        if total > 0:
            self._controller.set_zoom(width / total)
        self.scrollbar.setValue(0)

    # マウス位置の時刻を固定点にして拡大縮小する (DaVinci と同じ挙動 / §6.7)
    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ControlModifier:
            local = self.view.mapFrom(self, event.position().toPoint())
            anchor_sec = self.view.x_to_sec(local.x())
            self._controller.zoom_by(1.2 if delta > 0 else 1 / 1.2)
            # 拡大後もアンカーが同じ画面位置に来るようオフセットを解き直す
            zoom = self._controller.zoom()
            new_offset = anchor_sec - (local.x() / zoom if zoom > 0 else 0.0)
            self.scrollbar.setValue(int(max(new_offset, 0.0) * 1000))
            event.accept()
            return
        if event.modifiers() & Qt.ShiftModifier or True:
            self.scrollbar.triggerAction(
                QAbstractSlider.SliderSingleStepSub if delta > 0
                else QAbstractSlider.SliderSingleStepAdd)
            event.accept()
