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
    # 再生ヘッドのドラッグ中か (スクラブ音声の開始・終了 / resolve7 §3-8)
    scrub_changed = Signal(bool)
    # 保存状態の変化 (タイトルの * を出し入れするため / resolve7 §5.6)
    saved_state_changed = Signal()

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
        # 再生ヘッドをドラッグ中か (resolve7 §5.12)
        self._scrubbing = False

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

    # 未保存の編集があるか (閉じるときの確認に使う / resolve7 §5.6)
    # is_dirty() は「一度でも編集したか」で戻せない別概念のため、両方を残す。
    def is_modified(self):
        return not self._stack.is_clean()

    # 保存できた時点で呼ぶ (以後 Undo で戻ってきたときも「保存済み」と判定できる)
    def mark_saved(self):
        self._stack.mark_clean()
        self.saved_state_changed.emit()

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

    # リップルで一緒に詰める対象が全トラックか (設定 timeline.ripple_sync_tracks)
    def _sync_all(self):
        return str(self._cfg.get("ripple_sync_tracks", "all")).strip().lower() != "same"

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
    # ripple=True のときは全トラックを一緒に詰める (R8 / resolve2 §5.4)。
    def delete_selected(self, ripple=None):
        if not self._selected_ids:
            return False
        if ripple is None:
            ripple = self._cfg["ripple_delete"]
        return self.delete_clips(self._selected_ids, ripple=ripple)

    def delete_clips(self, clip_ids, ripple=False):
        return self.execute(commands.DeleteClip(
            clip_ids, ripple=ripple,
            min_clip_sec=self._cfg["min_clip_sec"], sync_all=self._sync_all()))

    # 再生ヘッドを境に、選択クリップの前 / 後ろをリップル削除する (R4・R6)
    # side="before" (A キー) / "after" (D キー)
    # 戻り値: 実行できたら True。対象が無ければ False (画面側で案内を出す)
    def ripple_trim_to_playhead(self, side):
        clip = self.clip_at_playhead()
        if clip is None:
            return False
        return self.execute(commands.RippleTrimToPlayhead(
            clip.id, self._playhead, side,
            min_clip_sec=self._cfg["min_clip_sec"], sync_all=self._sync_all()))

    # 再生ヘッド上のクリップを返す (resolve2 §5.3-3 / 回答 Q3)
    # 1. 選択中のクリップが再生ヘッドを含んでいればそれ
    # 2. 無ければベース映像トラック (V1) で再生ヘッドを含むクリップ
    # 3. それも無ければ None
    def clip_at_playhead(self):
        for clip_id in self._selected_ids:
            clip = self._timeline.clip_by_id(clip_id)
            if clip is not None and self._contains(clip, self._playhead):
                return clip
        base = self._timeline.base_video_track()
        if base is None:
            return None
        for clip in base.clips:
            if self._contains(clip, self._playhead):
                return clip
        return None

    def change_layer(self, clip_id, direction):
        return self.execute(commands.ChangeZOrder(clip_id, direction))

    def move_overlay(self, clip_id, x, y):
        return self.execute(commands.MoveOverlay(clip_id, x, y))

    # オーバーレイの大きさを変える (resolve7 §5.2)。scale はキャンバス幅に対する比率。
    def resize_overlay(self, clip_id, scale):
        overlay = self._cfg["overlay"]
        return self.execute(commands.ResizeOverlay(
            clip_id, scale, overlay["min_scale"], overlay["max_scale"]))

    def edit_subtitle(self, clip_id, **fields):
        return self.execute(commands.EditSubtitle(clip_id, **fields))

    # 複数の字幕へまとめて適用する (色の一括変更 / ver3 resolve6 §3-10)
    def edit_subtitles(self, clip_ids, **fields):
        return self.execute(commands.EditSubtitles(clip_ids, **fields))

    def set_clip_enabled(self, clip_id, enabled):
        return self.execute(commands.SetClipEnabled(clip_id, enabled))

    def set_audio_muted(self, audio_clip_id, muted):
        return self.execute(commands.SetAudioMuted(audio_clip_id, muted))

    def set_audio_gain(self, audio_clip_id, gain_db):
        return self.execute(commands.SetAudioGain(audio_clip_id, gain_db))

    # 置いた直後の大きさを決める (resolve7 §7 overlay.default_scale_mode)
    #   "fit_width" (既定) … 従来どおりキャンバス幅いっぱい (None を返して既定に任せる)
    #   "native"          … 素材のピクセル数どおり (キャンバスより大きければ幅いっぱいに収める)
    def _initial_overlay_scale(self, media):
        if str(self._cfg["overlay"]["default_scale_mode"]) != "native":
            return None
        width = int(getattr(media, "width", 0) or 0)
        canvas = int(self._timeline.width or 0)
        if width <= 0 or canvas <= 0:
            return None
        return min(float(width) / float(canvas), 1.0)

    # メディアを追加する。追加できたら新しいクリップ ID を返す。
    def add_media(self, media, timeline_start, duration, track_id=None):
        command = commands.AddMediaClip(
            media, timeline_start, duration, track_id=track_id,
            max_video_tracks=self._cfg["media"]["max_video_tracks"],
            scale=self._initial_overlay_scale(media),
        )
        if not self.execute(command):
            return None
        self.select([command.created_clip_id])
        return command.created_clip_id

    # 字幕クリップを追加する。追加できたら新しいクリップ ID を返す (置けなければ None)。
    # duration 省略時は設定 (timeline.default_subtitle_sec) の尺を使う。
    def add_subtitle(self, timeline_start, duration=None, track_id=None, text=""):
        command = commands.AddSubtitleClip(
            timeline_start,
            self._cfg["default_subtitle_sec"] if duration is None else duration,
            text=text, track_id=track_id, min_clip_sec=self._cfg["min_clip_sec"],
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

    # ------------------------------------------------------------------
    # スクラブ (再生ヘッドのドラッグ / resolve7 §5.12)
    # ------------------------------------------------------------------
    # ドラッグ経路はルーラとトラック area の 2 つある。両方に音の制御を書かず、
    # ここへ「掴んだ・離した」を集約し、音は PreviewPanel が受け持つ。

    # 再生ヘッドを掴んだ
    def begin_scrub(self):
        if self._scrubbing:
            return
        self._scrubbing = True
        self.scrub_changed.emit(True)

    # 離した (取りこぼしに備え PreviewPanel 側からも呼べるようにしておく)
    def end_scrub(self):
        if not self._scrubbing:
            return
        self._scrubbing = False
        self.scrub_changed.emit(False)

    def is_scrubbing(self):
        return self._scrubbing

    # ------------------------------------------------------------------
    # 吸着 (resolve2 §5.2 / R1・R2)
    # ------------------------------------------------------------------

    # 吸着先の候補 (編集点の集合) を返す
    #   ・タイムラインの先頭と全長
    #   ・非音声トラックの全クリップの開始・終了 (= 編集点)
    #   ・再生ヘッド (include_playhead=True のとき)
    # 音声トラックは V1 からの導出のため候補に含めない (同じ値が重複するだけ)。
    def snap_targets(self, exclude_id=None, include_playhead=True):
        targets = [0.0, self._timeline.duration_sec()]
        if include_playhead:
            targets.append(self._playhead)
        for track in self._timeline.tracks:
            if track.is_audio():
                continue
            for clip in track.clips:
                if clip.id == exclude_id:
                    continue
                targets.append(clip.timeline_start)
                targets.append(clip.timeline_end)
        return targets

    # 指定秒を近くの編集点へ吸着させる
    # exclude_id       : ドラッグ中のクリップ (自分自身の端へは吸着しない)
    # include_playhead : 再生ヘッドを吸着先に含めるか
    #                    (再生ヘッド自身を動かすときは自分へ吸着しないよう False)
    # 戻り値: 吸着後の秒。閾値の外なら元の値をそのまま返す。
    def snap_sec(self, sec, exclude_id=None, include_playhead=True):
        if not self._cfg["snap_enabled"] or self._zoom <= 0:
            return sec
        threshold = self._cfg["snap_threshold_px"] / self._zoom
        targets = self.snap_targets(exclude_id, include_playhead)
        if not targets:
            return sec
        best = min(targets, key=lambda c: abs(c - sec))
        return best if abs(best - sec) <= threshold else sec

    # 再生ヘッド用の吸着 (設定 timeline.snap_playhead で個別に無効化できる)
    def snap_playhead_sec(self, sec):
        if not self._cfg["snap_playhead"]:
            return sec
        return self.snap_sec(sec, include_playhead=False)

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
