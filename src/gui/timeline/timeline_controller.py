# モデル ↔ UI の橋渡し (docs/request/ver3/resolve.md §6.1.3)
# UI は「コマンドを積む」以外の方法でモデルを触らない。ここが唯一の窓口になることで、
# UI と編集処理の分離 (§4-2) が崩れず、Undo/Redo が全操作で一様に効く。
from PySide6.QtCore import QObject, Signal

from ...timeline import clipboard, commands, model, subtitle_style
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
        # 実効字幕設定の控え (縦動画の上書き反映済み / ver9 resolve3 §R3)
        self._eff_subtitle_cfg = None

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

    # 実効字幕設定 (縦動画の上書きを反映済み) を返す (ver9 resolve3 §R3)
    # プレビューと同じ値を使うことで「インスペクタの表示 = 見え方」を一致させる。
    # 1 プロジェクトの間は変わらないため 1 回だけ組んで覚えておく。
    def effective_subtitle_cfg(self):
        from ...modules import subtitle_generator  # noqa: PLC0415 (重い import を遅らせる)

        if self._eff_subtitle_cfg is None:
            self._eff_subtitle_cfg = subtitle_generator.build_effective_subtitle_cfg(
                self._settings.get("subtitle", {}), self._settings.get("vertical", {}),
                {"is_portrait": self._timeline.orientation == "portrait",
                 "width": self._timeline.width, "height": self._timeline.height},
            )
        return self._eff_subtitle_cfg

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
            # 字幕を割ったときの本文の扱いはテロップ用の分割と同じ規約にする (ver9 要望)
            if self.execute(commands.SplitClip(
                    target, at_sec, self._cfg["min_clip_sec"],
                    split_text=self._cfg["subtitle_split_text"])):
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

    # V1 (ベース映像) の拡大率を変える (ver9 resolve6 §3.5)。
    # zoom はキャンバスへ収めた状態に対する倍率 (1.0 = 今の見た目)。
    def zoom_base_clip(self, clip_id, zoom):
        cfg = self._cfg["base_zoom"]
        return self.execute(commands.ZoomBaseClip(
            clip_id, zoom, cfg["min"], cfg["max"]))

    # V1 の表示位置を変える (拡大・縮小しているときだけ効く / §3.4)
    def move_base_clip(self, clip_id, x, y):
        return self.execute(commands.MoveBaseClip(clip_id, x, y))

    def edit_subtitle(self, clip_id, **fields):
        return self.execute(commands.EditSubtitle(clip_id, **fields))

    # 複数の字幕へまとめて適用する (色の一括変更 / ver3 resolve6 §3-10)
    def edit_subtitles(self, clip_ids, **fields):
        return self.execute(commands.EditSubtitles(clip_ids, **fields))

    # 字幕の開始を V1 クリップ (カット) の頭へ揃える (ver8 resolve6 R5)
    # clip_ids 省略時は全字幕が対象。戻り値 {"changed": bool, "aligned": 件数}
    def align_subtitles_to_cut_heads(self, clip_ids=None):
        from ...modules import subtitle_generator  # noqa: PLC0415 (重い import を遅らせる)

        subtitle_cfg = self._settings.get("subtitle", {})
        # 文字数で単位を作る既定 ("length") では字幕がカットを跨ぐのが正常形のため
        # 終端を切らない。切るのは "cut" (旧挙動) のときだけ (ver8 resolve6 §12-6)。
        clamp = (subtitle_generator.subtitle_unit(subtitle_cfg) == "cut"
                 and not bool(subtitle_cfg.get("hold_across_cut", False)))
        command = commands.AlignSubtitlesToCutHeads(
            clip_ids,
            tolerance_sec=float(subtitle_cfg.get("align_tolerance_sec", 0.6)),
            clamp_to_cut_end=clamp,
            min_clip_sec=self._cfg["min_clip_sec"],
        )
        changed = self.execute(command)
        return {"changed": changed, "aligned": command.aligned}

    # 選択した字幕を 1 本へ結合する (G4 / docs/quality/resolve1.md §3.4 G4)
    # 本文は時間順に連結し、改行は現在の設定で引き直す。
    def merge_subtitles(self, clip_ids):
        ids = [i for i in (clip_ids or []) if i]
        if len(ids) < 2:
            return False
        return self.execute(commands.MergeSubtitles(ids, wrap=self._subtitle_wrap()))

    # 再生ヘッド位置で字幕を 2 本へ分ける (G4)。
    # 本文は既定では分けずに複製する (ver9 要望)。
    # timeline.subtitle_split_text=true で本文も分ける旧挙動へ戻せる。
    def split_subtitle(self, clip_id, at_sec=None):
        at_sec = self._playhead if at_sec is None else at_sec
        return self.execute(commands.SplitSubtitle(
            clip_id, at_sec, self._cfg["min_clip_sec"],
            split_text=self._cfg["subtitle_split_text"]))

    # 本文の整形関数 (改行の引き直し) を返す
    # 結合すると行が長くなるため、現在の折り返し設定で引き直す必要がある。
    def _subtitle_wrap(self):
        from ...modules import subtitle_generator  # noqa: PLC0415 (重い import を遅らせる)

        cfg = self._settings.get("subtitle", {})
        profile = getattr(self._timeline, "output_profile", None) or {}
        width = int(profile.get("width") or
                    self._settings.get("ffmpeg", {}).get("output_width", 0) or 0)
        measure = subtitle_generator.build_text_measure(cfg, width)
        min_len = int(cfg.get("min_line_length", 15))
        max_len = int(cfg.get("max_line_length", 20))
        engine = str(cfg.get("wrap_engine", "budoux")).strip().lower()

        def _wrap(text):
            return subtitle_generator.wrap_lines(
                text, min_len, max_len, engine, measure=measure)
        return _wrap

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
    # role を渡すとその役割で作り、対応するトラックへ置く (ver3 resolve11 §5.8)。
    def add_subtitle(self, timeline_start, duration=None, track_id=None, text="",
                     role=model.DEFAULT_ROLE):
        command = commands.AddSubtitleClip(
            timeline_start,
            self._cfg["default_subtitle_sec"] if duration is None else duration,
            text=text, track_id=track_id, min_clip_sec=self._cfg["min_clip_sec"],
            role=role, subtitle_tracks_cfg=self._cfg["subtitle_tracks"],
        )
        if not self.execute(command):
            return None
        self.select([command.created_clip_id])
        return command.created_clip_id

    # 字幕の役割を変える。コメントにするとコメント用トラック (S2) へ移す。
    # 戻り値 {"changed": bool, "moved": bool, "blocked": bool, "skipped": bool}
    # (ver3 resolve11 §5.8 / skipped は ver9 resolve10 §3.5)
    #   skipped = 利用者が増やしたトラックに置かれていたため移動しなかった
    def change_subtitle_role(self, clip_id, role):
        command = commands.ChangeSubtitleRole(
            clip_id, role, subtitle_tracks_cfg=self._cfg["subtitle_tracks"])
        changed = self.execute(command)
        return {"changed": changed, "moved": command.moved,
                "blocked": command.move_blocked,
                "skipped": command.move_skipped}

    # 選択した字幕を新しいテロップトラックへ移す (ver9 resolve2 §R2)
    # 役割は変えないため、アイコンも背景も付かない (見た目は従来のテロップのまま)。
    # 戻り値 {"moved": int, "skipped": int, "track": str|None, "limit": bool}
    def move_subtitles_to_new_track(self, clip_ids):
        command = commands.MoveSubtitlesToNewTrack(
            clip_ids, subtitle_tracks_cfg=self._cfg["subtitle_tracks"])
        changed = self.execute(command)
        return {
            "moved": command.moved if changed else 0,
            "skipped": command.skipped,
            "track": command.created_track_id if changed else None,
            "limit": command.limit_reached,
        }

    # テロップを既にあるテロップトラックへ移す (ドラッグ＆ドロップ / ver9 要望)
    # new_start を渡すと時刻も同時に変わる (斜めのドラッグ)。
    def move_subtitle_to_track(self, clip_id, track_id, new_start=None):
        return self.execute(commands.MoveSubtitleToTrack(
            clip_id, track_id, new_start, self._cfg["min_clip_sec"],
            # 役割は落ち先のトラックが決める (ver9 resolve13 §3.7)
            subtitle_tracks_cfg=self._cfg["subtitle_tracks"]))

    # テロップトラックの既定の文字色を変える (ヘッダの色見本 / ver9 resolve13 §3.5)
    # そのトラックの、個別指定が無いテロップすべてへ効く。空文字で既定へ戻す。
    def set_subtitle_track_color(self, track_id, color):
        return self.execute(commands.SetSubtitleTrackColor(track_id, color))

    # テロップトラックの既定の文字色 (未設定は空文字)
    def subtitle_track_color(self, track_id):
        return subtitle_style.track_color(self._timeline.track_by_id(track_id))

    # テロップトラックを 1 本削除する (ヘッダの右クリック / ver9 要望)
    # 載っているテロップも一緒に消える (確認は UI 側で取る)。
    # 戻り値 {"changed": bool, "clips": 一緒に消したテロップの件数}
    def remove_subtitle_track(self, track_id):
        command = commands.RemoveSubtitleTrack(
            track_id, subtitle_tracks_cfg=self._cfg["subtitle_tracks"])
        changed = self.execute(command)
        return {"changed": changed,
                "clips": command.removed_clips if changed else 0}

    # 削除できるテロップトラックか (ヘッダの右クリックの出し分け / ver9 要望)
    def can_remove_subtitle_track(self, track_id):
        cfg = self._cfg["subtitle_tracks"]
        if not cfg["remove_track_enabled"]:
            return False
        track = self._timeline.track_by_id(track_id)
        return commands.is_removable_subtitle_track(self._timeline, track, cfg)

    # ------------------------------------------------------------------
    # コピー＆ペースト (ver3 resolve10 §5.4)
    # ------------------------------------------------------------------

    # 選択中のノードをクリップボードへ控える。控えた件数を返す (0 = 対象なし)。
    # Timeline は変更しないため履歴には積まない (Undo の対象外・未保存の印も付かない)。
    def copy_selected(self):
        return clipboard.copy_clips(self._timeline, self._selected_ids)

    def can_paste(self):
        return not clipboard.is_empty()

    # クリップボードの内容を再生ヘッド位置へ貼り付ける (Ctrl+V)
    # 貼り付けたノードが優先され、干渉した既存ノードだけが必要な分だけ右へずれる。
    # V1 へ貼るときは字幕・オーバーレイも同量ずれる (設定 timeline.paste.ripple_scope)。
    # 戻り値: {"pasted": 件数, "skipped": 件数, "shifted": ずらした秒数, "empty": bool}
    def paste(self):
        payload = clipboard.payload()
        if not payload:
            return {"pasted": 0, "skipped": 0, "shifted": 0.0, "empty": True}
        paste_cfg = self._cfg["paste"]
        command = commands.PasteClips(
            payload, self._playhead,
            min_clip_sec=self._cfg["min_clip_sec"],
            insert_policy=paste_cfg["insert_policy"],
            ripple_scope=paste_cfg["ripple_scope"],
            archive_index_policy=paste_cfg["archive_index_policy"],
            max_video_tracks=self._cfg["media"]["max_video_tracks"],
            subtitle_tracks_cfg=self._cfg["subtitle_tracks"],
        )
        if not self.execute(command):
            return {"pasted": 0,
                    "skipped": command.skipped or len(payload.get("items") or []),
                    "shifted": 0.0, "empty": False}
        if paste_cfg["select_pasted"] and command.created_clip_ids:
            self.select(command.created_clip_ids)
        if paste_cfg["move_playhead_to_end"] and command.pasted_end_sec is not None:
            self.set_playhead(command.pasted_end_sec)
        return {"pasted": len(command.created_clip_ids), "skipped": command.skipped,
                "shifted": command.shifted_sec, "empty": False}

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
