# モデル ↔ UI の橋渡し (docs/request/ver3/resolve.md §6.1.3)
# UI は「コマンドを積む」以外の方法でモデルを触らない。ここが唯一の窓口になることで、
# UI と編集処理の分離 (§4-2) が崩れず、Undo/Redo が全操作で一様に効く。
from PySide6.QtCore import QObject, Signal

from ...timeline import commands
from ...timeline.builder import timeline_config
from ...timeline.timemap import TimeMap
from ...utils.logger import get_logger

_logger = get_logger(__name__)


class TimelineController(QObject):

    # 構造変化 (クリップ増減・トラック増減)
    timeline_changed = Signal()
    # clip_id 単位の更新 (移動・トリム・レイヤー)
    clip_changed = Signal(str)
    # 選択要素 (Clip / SubtitleClip / AudioClip / None)
    selection_changed = Signal(object)
    # 再生ヘッド秒
    playhead_moved = Signal(float)
    # ズーム・スクロール
    view_changed = Signal()

    def __init__(self, timeline, settings, parent=None):
        super().__init__(parent)
        self._timeline = timeline
        self._settings = settings or {}
        self._cfg = timeline_config(settings)
        self._stack = commands.CommandStack()
        self._selected_ids = []
        self._playhead = float(timeline.playhead_sec or 0.0)
        self._zoom = float(timeline.zoom_px_per_sec or self._cfg["default_zoom_px_per_sec"])
        # 編集が一度でも入ったか (プレビュー音声の初回再利用の可否判定に使う)
        self._dirty = False

    # ------------------------------------------------------------------
    # 参照
    # ------------------------------------------------------------------

    @property
    def timeline(self):
        return self._timeline

    @property
    def settings(self):
        return self._settings

    @property
    def cfg(self):
        return self._cfg

    def is_dirty(self):
        return self._dirty

    def playhead(self):
        return self._playhead

    def zoom(self):
        return self._zoom

    def selected_ids(self):
        return list(self._selected_ids)

    # 単一選択の要素を返す (複数選択時は先頭)
    def selected_clip(self):
        for clip_id in self._selected_ids:
            clip = self._timeline.clip_by_id(clip_id)
            if clip is not None:
                return clip
        return None

    # 現在の再生ヘッドに対応する素材と時刻を返す ((media, source_sec) / 空白は None)
    def source_at_playhead(self):
        return self.source_at(self._playhead)

    def source_at(self, timeline_sec):
        timemap = TimeMap.from_clips(
            self._timeline.base_clips(),
            body_media_id=(self._timeline.source or {}).get("media_id"),
        )
        resolved = timemap.to_source(timeline_sec)
        if resolved is None:
            return None
        media_id, source_sec = resolved
        media = self._timeline.media_by_id(media_id)
        if media is None:
            return None
        return media, source_sec

    # ------------------------------------------------------------------
    # コマンド実行
    # ------------------------------------------------------------------

    # コマンドを実行して履歴へ積む。変化があれば timeline_changed を放送する。
    def execute(self, command):
        if not self._stack.push(self._timeline, command):
            return False
        self._dirty = True
        self.timeline_changed.emit()
        return True

    def can_undo(self):
        return self._stack.can_undo()

    def can_redo(self):
        return self._stack.can_redo()

    def undo_label(self):
        return self._stack.undo_label()

    def redo_label(self):
        return self._stack.redo_label()

    def undo(self):
        label = self._stack.undo(self._timeline)
        if label is None:
            return False
        self._prune_selection()
        self.timeline_changed.emit()
        return True

    def redo(self):
        label = self._stack.redo(self._timeline)
        if label is None:
            return False
        self._prune_selection()
        self.timeline_changed.emit()
        return True

    # ------------------------------------------------------------------
    # 編集操作 (UI からはこれらを呼ぶ)
    # ------------------------------------------------------------------

    def move_clip(self, clip_id, new_start):
        return self.execute(
            commands.MoveClip(clip_id, new_start, self._cfg["min_clip_sec"]))

    def trim_clip(self, clip_id, edge, new_value):
        return self.execute(
            commands.TrimClip(clip_id, edge, new_value, self._cfg["min_clip_sec"]))

    # 再生ヘッド位置 (省略時) で選択クリップを分割する = 編集点の追加
    def split_at_playhead(self, clip_id=None, at_sec=None):
        at_sec = self._playhead if at_sec is None else at_sec
        targets = [clip_id] if clip_id else self._clips_at(at_sec)
        changed = False
        for target in targets:
            if self.execute(commands.SplitClip(target, at_sec, self._cfg["min_clip_sec"])):
                changed = True
        return changed

    # 削除。ripple 未指定時は設定 (timeline.ripple_delete) に従う。
    def delete_selected(self, ripple=None):
        if not self._selected_ids:
            return False
        if ripple is None:
            ripple = self._cfg["ripple_delete"]
        return self.execute(commands.DeleteClip(self._selected_ids, ripple=ripple))

    def delete_clips(self, clip_ids, ripple=False):
        return self.execute(commands.DeleteClip(clip_ids, ripple=ripple))

    def change_layer(self, clip_id, direction):
        return self.execute(commands.ChangeZOrder(clip_id, direction))

    def move_overlay(self, clip_id, x, y):
        return self.execute(commands.MoveOverlay(clip_id, x, y))

    def edit_subtitle(self, clip_id, **fields):
        return self.execute(commands.EditSubtitle(clip_id, **fields))

    def set_clip_enabled(self, clip_id, enabled):
        return self.execute(commands.SetClipEnabled(clip_id, enabled))

    def set_audio_muted(self, audio_clip_id, muted):
        return self.execute(commands.SetAudioMuted(audio_clip_id, muted))

    def set_audio_gain(self, audio_clip_id, gain_db):
        return self.execute(commands.SetAudioGain(audio_clip_id, gain_db))

    # メディアを追加する。追加できたら新しいクリップ ID を返す。
    def add_media(self, media, timeline_start, duration, track_id=None):
        command = commands.AddMediaClip(
            media, timeline_start, duration, track_id=track_id,
            max_video_tracks=self._cfg["media"]["max_video_tracks"],
        )
        if not self.execute(command):
            return None
        self.select([command.created_clip_id])
        return command.created_clip_id

    # ------------------------------------------------------------------
    # 選択・再生ヘッド・ズーム
    # ------------------------------------------------------------------

    def select(self, clip_ids):
        ids = [i for i in (clip_ids or []) if i]
        if ids == self._selected_ids:
            return
        self._selected_ids = ids
        self.selection_changed.emit(self.selected_clip())

    def toggle_select(self, clip_id):
        ids = list(self._selected_ids)
        if clip_id in ids:
            ids.remove(clip_id)
        else:
            ids.append(clip_id)
        self.select(ids)

    def clear_selection(self):
        self.select([])

    # 再生ヘッドを移動する (Timeline の範囲内へ丸める)
    def set_playhead(self, sec):
        total = self._timeline.duration_sec()
        value = min(max(float(sec), 0.0), total)
        if abs(value - self._playhead) < 1e-6:
            return
        self._playhead = value
        self._timeline.playhead_sec = value
        self.playhead_moved.emit(value)

    # 1 フレームぶん進める / 戻す
    def step_playhead(self, frames):
        delta = frames / float(self._timeline.fps or 60)
        self.set_playhead(self._playhead + delta)

    def set_zoom(self, px_per_sec):
        value = min(max(float(px_per_sec), self._cfg["zoom_min_px_per_sec"]),
                    self._cfg["zoom_max_px_per_sec"])
        if abs(value - self._zoom) < 1e-9:
            return
        self._zoom = value
        self._timeline.zoom_px_per_sec = value
        self.view_changed.emit()

    def zoom_by(self, factor):
        self.set_zoom(self._zoom * factor)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    # 指定時刻を含む編集可能クリップの ID を返す (分割対象の解決に使う)
    def _clips_at(self, sec):
        if self._selected_ids:
            return [i for i in self._selected_ids
                    if self._contains(self._timeline.clip_by_id(i), sec)]
        found = []
        for track in self._timeline.tracks:
            if track.is_audio() or track.locked:
                continue
            for clip in track.clips:
                if self._contains(clip, sec):
                    found.append(clip.id)
        return found

    @staticmethod
    def _contains(clip, sec):
        return clip is not None and clip.timeline_start < sec < clip.timeline_end

    # Undo/Redo で消えた要素を選択から外す
    def _prune_selection(self):
        ids = [i for i in self._selected_ids if self._timeline.clip_by_id(i) is not None]
        if ids != self._selected_ids:
            self._selected_ids = ids
            self.selection_changed.emit(self.selected_clip())
