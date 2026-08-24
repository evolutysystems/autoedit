# Timeline 表示・操作 (docs/request/ver3/resolve.md §6.6 / §6.7 / §6.8)
# DaVinci Resolve / Premiere Pro のような一般的なノンリニア編集ソフトの Timeline を参考に、
# トラック (Video / Audio / Subtitle)・クリップ編集・ズーム・横スクロール・
# 再生ヘッド移動・メディアの D&D を提供する。
#
# 描画と入力のみを担い、モデルの変更は必ず TimelineController (= コマンド) 経由で行う。
import math
import os

from PySide6.QtCore import QLine, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSlider,
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollBar,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from .waveform import WaveformCache
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

# 右クリック「字幕追加」で置く字幕の初期テキスト。
# 空文字にすると Timeline でもプレビューでも何も見えず、追加できたか分からないため入れる。
NEW_SUBTITLE_TEXT = "新しい字幕"

# クリップの色 (種別で見分けられるようにする)
# ver3 resolve3 R10: クリップ・再生ヘッド・選択枠はガラス化の対象外。
# 編集点の境界が今と同じ精度で判別できるよう、現状の不透明色をそのまま使う (§5.7-2)。
_COLOR_BODY = QColor(58, 108, 168)          # 本編 (編集点由来)
_COLOR_OPENING = QColor(126, 87, 168)       # オープニング
_COLOR_ENDING = QColor(96, 87, 168)         # エンディング
_COLOR_OVERLAY = QColor(40, 132, 130)       # 追加メディア
_COLOR_SUBTITLE = QColor(178, 130, 46)      # 字幕
_COLOR_AUDIO = QColor(56, 130, 78)          # 音声
_COLOR_DISABLED = QColor(96, 96, 96)        # 無効化されたクリップ
_COLOR_SELECTED_BORDER = QColor(255, 214, 92)   # 選択枠 (琥珀。赤にすると再生ヘッドと紛れる)

# 波形を描く最小のクリップ幅 (px)。これより細いと形が読めない
_WAVE_MIN_PX = 4
# 波形に RMS を重ねる最小の幅 (px)。これより細いと重なって見分けが付かない
_WAVE_RMS_MIN_PX = 24
_COLOR_PLAYHEAD = QColor(232, 84, 84)           # 再生ヘッド (位置を示す機能色)

# 背景系の色 (ver3 resolve3 §5.7-1)。
# 「Timeline の背景の黒色部分のみガラスにする」(R10) ため、下記だけをテーマから引く。
# Timeline は OS の明暗に追従せず常にダーク基調 (§3-6-3)。
# ui.theme = "system" のときは従来の不透明色へ完全に戻す (§7 の切り戻し)ため、
# 旧定数を _LEGACY_ として残し、フォールバック値に使う。
_LEGACY_TRACK_BG = QColor(38, 38, 40)
_LEGACY_TRACK_BG_ALT = QColor(44, 44, 47)
_LEGACY_GRID = QColor(64, 64, 68)
_LEGACY_TEXT = QColor(236, 236, 236)
_LEGACY_RULER_BG = QColor(30, 30, 32)


# トラック領域の下地 (= 「Timeline の背景の黒色部分」)。半透明なのでウィンドウ背景が透ける。
def _backdrop_color():
    return theme.color("timeline.bg") if theme.is_enabled() else _LEGACY_TRACK_BG


# 交互に敷くトラック行の色 (縞模様は維持する)。
# 下地 (timeline.bg) と重ねた結果を 1 色へ畳んで返すため、塗りは 1 回で済む (§5.8)。
def _row_color(index):
    if not theme.is_enabled():
        return _LEGACY_TRACK_BG if index % 2 == 0 else _LEGACY_TRACK_BG_ALT
    token = "timeline.track" if index % 2 == 0 else "timeline.track.alt"
    return theme.composite_color(token, "timeline.bg")


# ルーラ・トラックヘッダの色 (同じく下地と畳んだ 1 色)
def _ruler_color():
    if not theme.is_enabled():
        return _LEGACY_RULER_BG
    return theme.composite_color("timeline.ruler", "timeline.bg")


# 罫線
def _grid_color():
    return theme.color("timeline.grid") if theme.is_enabled() else _LEGACY_GRID


# ラベル文字
def _text_color():
    return theme.color("timeline.text") if theme.is_enabled() else _LEGACY_TEXT

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
        # 下地 (黒) とルーラ層を畳んだ 1 色で塗る。ガラスのため背景が透ける (§5.7-1)
        painter.fillRect(self.rect(), _ruler_color())
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

        grid_pen = QPen(_grid_color())
        text_pen = QPen(_text_color())
        painter.setPen(grid_pen)
        value = first
        while value <= self._offset + visible_sec + step:
            x = int((value - self._offset) * zoom)
            painter.setPen(grid_pen)
            painter.drawLine(x, RULER_HEIGHT - 8, x, RULER_HEIGHT)
            painter.setPen(text_pen)
            painter.drawText(x + 3, RULER_HEIGHT - 10, format_time(value, with_hours))
            value += step

        # 再生ヘッド
        head_x = int((self._controller.playhead() - self._offset) * zoom)
        if 0 <= head_x <= self.width():
            painter.setPen(QPen(_COLOR_PLAYHEAD, 2))
            painter.drawLine(head_x, 0, head_x, RULER_HEIGHT)

    def mousePressEvent(self, event):
        # 再生ヘッドを掴んだことを Controller へ伝える (スクラブ音声 / resolve7 §5.12)
        if event.button() == Qt.LeftButton:
            self._controller.begin_scrub()
        self._emit_seek(event.position().x(), event.modifiers())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._emit_seek(event.position().x(), event.modifiers())

    # 離したらスクラブを終える (ルーラは従来 mouseReleaseEvent を持っていなかった)
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._controller.end_scrub()

    # ルーラの操作でも編集点へ吸着させる (resolve2 §5.2-2 / R1)
    # Alt 押下中は吸着しない。
    def _emit_seek(self, x, modifiers=Qt.NoModifier):
        zoom = self._controller.zoom()
        if zoom <= 0:
            return
        sec = self._offset + x / zoom
        if not (modifiers & Qt.AltModifier):
            sec = self._controller.snap_playhead_sec(sec)
        self.seek_requested.emit(sec)


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
        # ルーラと同じ見え方に揃える (下地と重ねた 1 色で塗る / §5.7-1)
        painter.fillRect(self.rect(), _ruler_color())
        rows = build_rows(self._controller.timeline, self._controller.cfg)
        for index, row in enumerate(rows):
            rect = QRect(0, row["y"], self.width(), row["height"])
            painter.fillRect(rect, _row_color(index))
            painter.setPen(QPen(_grid_color()))
            painter.drawLine(0, row["y"] + row["height"], self.width(),
                             row["y"] + row["height"])
            painter.setPen(QPen(_text_color()))
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
    # 操作できなかったときの案内 (プレビュー下のステータスへ出す)
    status_message = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._offset = 0.0                 # 表示左端の時刻 (秒)
        self._drag = _DRAG_NONE
        self._drag_clip_id = None
        self._drag_anchor_sec = 0.0
        self._drag_origin = 0.0
        self._drag_preview = None          # ドラッグ中の仮位置 (確定まで模型には触らない)
        self._press_pos = QPoint()         # 押した座標 (クリック/ドラッグの判定に使う)
        self._drag_moved = False           # しきい値を超えて実際に動かしたか
        # クリップのアウトラインの太さ (ver3 resolve4 E5)。
        # 長尺では 1,000 個超のクリップを毎フレーム描くため、設定の解決は 1 度だけ行う。
        ui_cfg = controller.cfg["ui"]
        self._outline_width = int(ui_cfg["clip_outline_width_px"])
        self._outline_selected_width = int(ui_cfg["clip_outline_selected_width_px"])
        # 音声クリップの波形。素材のデコードは裏で進み、伸びるたびに描き直す。
        wave_cfg = controller.cfg["waveform"]
        self._waveform_scale = wave_cfg["scale"]
        self._waveform_db_floor = abs(float(wave_cfg["db_floor"])) or 48.0
        self._waveform = WaveformCache(controller.settings, controller.cfg, parent=self)
        self._waveform.changed.connect(self.update)
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
        timeline = self._controller.timeline
        rows = build_rows(timeline, self._controller.cfg)

        # トラック領域の最背面に下地を敷く (= 「Timeline の背景の黒色部分」/ §5.7-1)。
        # 完全な透明にはしない。下地が明るいとクリップの色が沈んで見分けにくくなるため。
        # 各行は下地を畳み込んだ色で塗るため、ここではトラックが無い余白だけを塗る
        # (同じ画素を 2 度合成しないようにして描画コストを増やさない / §5.8)。
        filled_height = rows[-1]["y"] + rows[-1]["height"] if rows else 0
        if filled_height < self.height():
            painter.fillRect(0, filled_height, self.width(),
                             self.height() - filled_height, _backdrop_color())

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
        painter.fillRect(rect, _row_color(index))
        painter.setPen(QPen(_grid_color()))
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

        # 音声クリップは塗りの上に波形 (X=時間 / Y=音量) を重ねる。
        # アウトライン・ラベルより先に描き、枠と文字が波形に埋もれないようにする。
        if isinstance(clip, AudioClip):
            self._paint_waveform(painter, clip, rect, color)

        # アウトライン (ver3 resolve4 E5)。太さは設定から引く (値は __init__ で解決済み)。
        # QPen は境界線を中心に描かれ、太さのぶん外側へはみ出す。隣のクリップへ
        # 食い込むと編集点の境界が読めなくなるため、太さに応じて内側へ縮めて描く。
        # 細いクリップでは塗りが潰れないよう太さを抑える。
        width = self._outline_selected_width if is_selected else self._outline_width
        limit = max(1, min(rect.width(), rect.height()) // 2)
        if width > limit:
            width = limit
        painter.setPen(QPen(_COLOR_SELECTED_BORDER if is_selected else color.darker(160),
                            width))
        inset = width // 2
        # inset=0 のときは矩形を作り直さない (長尺では 1,000 個超を毎フレーム描くため)
        painter.drawRect(rect.adjusted(inset, inset, -inset, -inset) if inset else rect)

        # ラベル (幅に余裕があるときだけ)
        if rect.width() >= 40:
            painter.setPen(QPen(_text_color()))
            painter.drawText(rect.adjusted(5, 0, -5, 0),
                             Qt.AlignVCenter | Qt.AlignLeft, self._clip_label(clip, track))

    # 音声クリップの背景へ波形を敷く (DaVinci Resolve と同じ、緑地に明るい波形)。
    # 波形は素材内の時刻で引くため、クリップを動かしても切っても中身がずれない。
    def _paint_waveform(self, painter, clip, rect, color):
        timeline = self._controller.timeline
        video = timeline.clip_by_id(clip.link_clip)
        if video is None:
            return
        peaks = self._waveform.peaks_for(timeline.media_by_id(video.media_id))
        if peaks is None:
            return
        zoom = self._controller.zoom()
        if zoom <= 0 or rect.height() < 8:
            return

        # 画面に出ている範囲だけを列に落とす (長尺・高倍率でも描画量が画面幅で頭打ちになる)
        left = max(rect.left(), 0)
        right = min(rect.right(), self.width() - 1)
        columns = right - left + 1
        # 数 px しか無いクリップは波形を出しても読めない。縮小しきった長尺では
        # この幅のクリップが数千個並ぶため、ここで落とすと描画コストが跳ねない。
        if columns < _WAVE_MIN_PX:
            return
        source_start = video.source_in + (left - rect.left()) / zoom
        source_end = video.source_in + (right + 1 - rect.left()) / zoom
        # RMS は細いクリップでは重なって見えないので計算しない。
        # 無音カット後は細いクリップが大量に並ぶため、ここが描画コストに効く。
        result = peaks.columns(source_start, source_end, columns,
                               with_rms=columns >= _WAVE_RMS_MIN_PX)
        if result is None:
            return
        peak_values, rms_values = result

        center = rect.top() + rect.height() / 2.0
        half = rect.height() / 2.0 - 2.0
        if half <= 1.0:
            return
        # 波形は塗りより明るくする。ミュート時は塗りが灰色になるため、
        # ここで色を作れば波形も自動的に灰色系へ揃う。
        peak_color = color.lighter(155)
        peak_color.setAlpha(200)
        # 音量 (gain) を波形へ反映する。メニューで dB を変えると形が上下に伸縮し、
        # 実際に書き出される音の大きさが目で分かる。
        gain = 10.0 ** (clip.gain_db / 20.0) if abs(clip.gain_db) > 1e-6 else 1.0
        self._draw_wave_lines(painter, peak_values, left, center, half, peak_color, gain)
        # RMS (実効値) を重ねると音の詰まり具合が読み取りやすくなる (numpy がある環境のみ)
        if rms_values is not None:
            painter.setOpacity(0.9)
            self._draw_wave_lines(painter, rms_values, left, center, half,
                                  color.lighter(215), gain)
            painter.setOpacity(1.0)

    # 列ごとの音量 (0.0〜1.0) を中心線から上下対称に立てる
    def _draw_wave_lines(self, painter, values, left, center, half, color, gain=1.0):
        log_scale = self._waveform_scale == "log"
        floor_db = self._waveform_db_floor
        lines = []
        for index, value in enumerate(values):
            level = float(value) * gain
            if log_scale:
                # dB 目盛り。底 (db_floor) 以下は高さ 0 にする
                level = 0.0 if level <= 1e-6 else 1.0 + (20.0 * math.log10(level)) / floor_db
            # gain を上げた分がトラックからはみ出さないよう、必ず 0〜1 へ収める
            if level > 1.0:
                level = 1.0
            elif level < 0.0:
                level = 0.0
            offset = level * half
            x = left + index
            top = int(round(center - offset))
            bottom = int(round(center + offset))
            if bottom <= top:
                bottom = top + 1     # 無音でも中心線は残す (クリップの存在が分かる)
            lines.append(QLine(x, top, x, bottom))
        if lines:
            painter.setPen(QPen(color))
            painter.drawLines(lines)

    # 走っている波形デコードを止める (画面を閉じるとき)
    def shutdown(self):
        self._waveform.shutdown()

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

    # 座標にあるトラック行を返す (トラックの外なら None)
    def _row_at_pos(self, pos):
        for row in build_rows(self._controller.timeline, self._controller.cfg):
            if row["y"] <= pos.y() < row["y"] + row["height"]:
                return row
        return None

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
            # 空き領域 → 再生ヘッド移動 (編集点へ吸着) + 選択解除
            self._controller.clear_selection()
            self._drag = _DRAG_PLAYHEAD
            # クリップのドラッグ (移動・トリム) ではスクラブしない。
            # 鳴らすのは再生ヘッドを掴んだときだけ (resolve7 §5.12)。
            self._controller.begin_scrub()
            self._controller.set_playhead(
                self._playhead_sec_at(pos.x(), event.modifiers()))
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
        # 押した位置を覚え、しきい値を超えるまでは「クリック」として扱う (§6-1)
        self._press_pos = pos
        self._drag_moved = False
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
            self._controller.set_playhead(
                self._playhead_sec_at(pos.x(), event.modifiers()))
            return

        clip = self._controller.timeline.clip_by_id(self._drag_clip_id)
        if clip is None:
            return

        # クリックとドラッグの区別。しきい値は Qt のプラットフォーム値に任せる
        # (設定項目を増やさない / docs/error/20260812/resolve2.md §6-1)
        if not self._drag_moved:
            if (pos - self._press_pos).manhattanLength() < QApplication.startDragDistance():
                return
            self._drag_moved = True

        no_snap = bool(event.modifiers() & Qt.AltModifier)
        raw_sec = self.x_to_sec(pos.x())
        sec = raw_sec if no_snap else self._snap(raw_sec, exclude_id=clip.id)

        if self._drag == _DRAG_MOVE:
            delta = raw_sec - self._drag_anchor_sec
            desired = max(self._drag_origin + delta, 0.0)
            # 先頭・末尾の両方で吸着を試す (R2)
            start = desired if no_snap else self._snap_move(desired, clip)
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
        moved = self._drag_moved
        self._drag = _DRAG_NONE
        self._drag_clip_id = None
        self._drag_preview = None
        self._drag_moved = False

        if drag == _DRAG_PLAYHEAD or clip_id is None or preview is None:
            self._controller.end_scrub()
            self.update()
            return

        # 動かしていない = 単なるクリック → 選択だけで終える。
        # ここでコマンドを積むと、重なっている字幕が重なり解消のため別の位置へ飛ぶ
        # (docs/error/20260812/Analyze.md §5)
        if not moved:
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

    # ドラッグ中の吸着 (編集点・再生ヘッド・先頭/末尾 / resolve2 §5.2)
    # 計算そのものは Controller に集約してある (クリップドラッグと再生ヘッドで共有)。
    def _snap(self, sec, exclude_id=None):
        return self._controller.snap_sec(sec, exclude_id=exclude_id)

    # 移動時は先頭・末尾の両方で吸着を試し、ズレの小さい方を採る (resolve2 §5.2-3 / R2)
    # 末尾を隣のクリップの先頭へ合わせたい場面で吸着が効くようになる。
    def _snap_move(self, desired_start, clip):
        head = self._controller.snap_sec(desired_start, exclude_id=clip.id)
        tail = self._controller.snap_sec(
            desired_start + clip.duration, exclude_id=clip.id) - clip.duration
        if abs(head - desired_start) <= abs(tail - desired_start):
            return head
        return tail

    # マウス座標から再生ヘッド位置を決める (編集点へ吸着させる / resolve2 §5.2-2 / R1)
    # 再生ヘッド自身は吸着先から外す (自分へ吸着して動かなくなるため)。
    # Alt 押下中は吸着しない (クリップ移動と同じ規約)。
    def _playhead_sec_at(self, x, modifiers=Qt.NoModifier):
        sec = self.x_to_sec(x)
        if modifiers & Qt.AltModifier:
            return sec
        return self._controller.snap_playhead_sec(sec)

    # ------------------------------------------------------------------
    # 右クリックメニュー (§6.6)
    # ------------------------------------------------------------------

    def contextMenuEvent(self, event):
        pos = event.pos()
        hit = self.clip_at_pos(pos)
        menu = QMenu(self)

        if hit is None:
            menu.addAction("ここへ再生ヘッドを移動",
                           lambda: self._controller.set_playhead(
                               self._playhead_sec_at(pos.x())))
            # 字幕トラックの空き領域なら、その位置へ新しい字幕を足せる
            row = self._row_at_pos(pos)
            track = row["track"] if row is not None else None
            # 貼り付け先はあくまで再生ヘッド (右クリックした位置ではない / resolve10 §5.6)
            paste = menu.addAction("再生ヘッドへ貼り付け\tCtrl+V", self._paste)
            paste.setEnabled(self._controller.can_paste())
            # 字幕トラックの空き領域なら、その位置へ新しい字幕を足せる
            row = self._row_at_pos(pos)
            track = row["track"] if row is not None else None
            if track is not None and track.is_subtitle():
                menu.addSeparator()
                action = menu.addAction(
                    "字幕追加", lambda: self._add_subtitle(pos, track))
                action.setEnabled(not track.locked)
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
        # コピー＆ペースト (ver3 resolve10 §5.6)。貼り付け先は再生ヘッド。
        menu.addAction("コピー\tCtrl+C", self._copy)
        paste = menu.addAction("再生ヘッドへ貼り付け\tCtrl+V", self._paste)
        paste.setEnabled(self._controller.can_paste())
        menu.addSeparator()
        # 再生ヘッドを境にしたリップル削除 (A / D / resolve2 R4・R6)
        before = menu.addAction(
            "再生ヘッドより前をリップル削除\tA",
            lambda: self._controller.ripple_trim_to_playhead("before"))
        after = menu.addAction(
            "再生ヘッドより後ろをリップル削除\tD",
            lambda: self._controller.ripple_trim_to_playhead("after"))
        # 再生ヘッドがクリップの中に無ければ実行できない
        on_playhead = clip.timeline_start < self._controller.playhead() < clip.timeline_end
        before.setEnabled(on_playhead)
        after.setEnabled(on_playhead)
        menu.addSeparator()
        # 削除は 2 種類を常に並べて出す (回答 Q5 / R17)
        menu.addAction("リップル削除\tDelete",
                       lambda: self._controller.delete_selected(ripple=True))
        menu.addAction("削除のみ (空白を残す)\tBackSpace",
                       lambda: self._controller.delete_selected(ripple=False))
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

    # 右クリックした位置へ新しい字幕クリップを足す
    # 開始位置は編集点へ吸着させる (クリップ移動と同じ規約)。
    def _add_subtitle(self, pos, track):
        start = max(self._snap(self.x_to_sec(pos.x())), 0.0)
        clip_id = self._controller.add_subtitle(
            start, track_id=track.id, text=NEW_SUBTITLE_TEXT)
        if clip_id is None:
            self.status_message.emit("この位置には字幕を追加できません")

    # 右クリックメニューからのコピー (ver3 resolve10 §5.6)
    def _copy(self):
        count = self._controller.copy_selected()
        self.status_message.emit(
            f"{count} 件のノードをコピーしました" if count
            else "コピーするノードが選択されていません")

    # 右クリックメニューからの貼り付け。位置は再生ヘッド (ver3 resolve10 §5.6)
    def _paste(self):
        result = self._controller.paste()
        if result["empty"]:
            self.status_message.emit("コピーされたノードがありません")
            return
        if result["pasted"] == 0:
            self.status_message.emit(
                "貼り付けできませんでした (トラックのロックや素材の欠落を確認してください)")
            return
        message = f"{result['pasted']} 件を貼り付けました"
        if result["shifted"] > 0:
            message += f" (既存ノードを {result['shifted']:.2f} 秒ぶん右へ移動)"
        if result["skipped"]:
            message += f" ({result['skipped']} 件は貼れませんでした)"
        self.status_message.emit(message)

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
        # ボタン幅は設定から引く (ver3 resolve4 E4)。テーマの QSS が左右に余白を持つため、
        # 記号が見切れない幅を明示する。文字ボタン (全体) は自然な幅のままでよい。
        zoom_button_width = int(self._controller.cfg["ui"]["zoom_button_width_px"])
        minus = QPushButton("－")
        minus.setFixedWidth(zoom_button_width)
        theme.mark_icon_button(minus)
        minus.setToolTip("Timeline を縮小 (Ctrl + ホイールでも操作できます)")
        minus.clicked.connect(lambda: self._controller.zoom_by(1 / 1.25))
        plus = QPushButton("＋")
        plus.setFixedWidth(zoom_button_width)
        theme.mark_icon_button(plus)
        plus.setToolTip("Timeline を拡大 (Ctrl + ホイールでも操作できます)")
        plus.clicked.connect(lambda: self._controller.zoom_by(1.25))
        fit = QPushButton("全体")
        fit.setToolTip("Timeline 全体が収まるまで縮小します")
        fit.clicked.connect(self.zoom_to_fit)
        zoom_row.addWidget(minus)
        zoom_row.addWidget(plus)
        zoom_row.addWidget(fit)
        self.hint_label = QLabel(
            "W 分割 / A 前をリップル削除 / D 後ろをリップル削除 / "
            "Delete リップル削除・BackSpace 削除のみ / "
            "Space 再生・Q 倍速逆再生・E 倍速再生 / Alt で吸着オフ")
        theme.mark_note(self.hint_label)
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
        # 表示位置も新しい上限へ丸める。丸めないと、右端まで送ってから縮小・
        # 画面幅の変化で上限が縮んだとき、スクロールバーだけが上限へ張り付いて
        # 表示は Timeline の外に取り残される。そうなるとバーを動かしても値が
        # 変わらず (= valueChanged が出ず) 左へ戻れなくなる。
        offset = min(max(self.view.offset(), 0.0), maximum)
        self.scrollbar.blockSignals(True)
        self.scrollbar.setRange(0, int(maximum * 1000))
        self.scrollbar.setPageStep(int(max(visible, 0.1) * 1000))
        self.scrollbar.setSingleStep(int(max(visible / 10.0, 0.05) * 1000))
        self.scrollbar.setValue(int(offset * 1000))
        self.scrollbar.blockSignals(False)
        # 信号を止めている間の valueChanged は届かないため、表示側へは自分で配る。
        # これでバーの値と表示位置が常に一致する。
        self._on_scroll(self.scrollbar.value())

    # 再生ヘッドが可視域外に出たら追従スクロールする (§6.7)
    def _ensure_visible(self, sec):
        visible = self.view.visible_sec()
        offset = self.view.offset()
        if sec < offset or sec > offset + visible:
            self.scrollbar.setValue(int(max(sec - visible / 3.0, 0.0) * 1000))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_scrollbar()

    # 裏で走っている処理を止める (画面を閉じるとき)
    def shutdown(self):
        self.view.shutdown()

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
