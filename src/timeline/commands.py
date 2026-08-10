# Timeline の編集操作 (docs/request/ver3/resolve.md §6.3.3)
# すべての編集を Command として実装し CommandStack に積む。
# UI 側は「コマンドを積む」以外の方法でモデルを触らないため、
# UI と編集処理の分離 (§4-2) が崩れず、Undo/Redo が全操作で一様に効く。
#
# R18 (§6.3.4): 音声クリップは時刻を持たず V1 から導出するため、
# 移動・トリム・リップルは追随処理なしで自動的に一致する。
# 明示的な追随が要るのはクリップ個数が変わる Split と Delete の 2 つだけ。
from ..utils.logger import get_logger
from .model import (
    BASE_SUBTITLE_TRACK_ID,
    BASE_Z_ORDER,
    DEFAULT_ROLE,
    DEFAULT_SUBTITLE_Z_ORDER,
    ORIGIN_USER_SUBTITLE,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    Z_ORDER_STEP,
    AudioClip,
    Clip,
    SubtitleClip,
    Track,
    Transform,
)

_logger = get_logger(__name__)

# 位置比較の許容誤差
_EPS = 1e-6

# レイヤー変更の方向 (§6.5-2)
LAYER_TOP = "top"
LAYER_UP = "up"
LAYER_DOWN = "down"
LAYER_BOTTOM = "bottom"

# トリムの端
EDGE_LEFT = "left"
EDGE_RIGHT = "right"


# ------------------------------------------------------------------
# スナップショットによる Undo/Redo
# ------------------------------------------------------------------

# Timeline の編集可能な状態を丸ごと控える。
# コマンドごとに逆操作を書くと、コマンドを増やすたびに書き忘れが不整合として
# 表面化するため、状態を控える方式にして正しさをデータ側で担保する。
def _snapshot(timeline):
    return {
        "tracks": [
            {
                "id": t.id, "kind": t.kind, "index": t.index, "name": t.name,
                "enabled": t.enabled, "locked": t.locked, "is_base": t.is_base,
                "link_track": t.link_track,
                "clips": [c.copy() for c in t.clips],
            }
            for t in timeline.tracks
        ],
        "media_pool": list(timeline.media_pool),
    }


# 控えた状態へ戻す
def _restore(timeline, snapshot):
    timeline.tracks = [
        Track(t["id"], t["kind"], t["index"], t["name"], t["enabled"],
              t["locked"], t["is_base"], t["link_track"], t["clips"])
        for t in snapshot["tracks"]
    ]
    timeline.media_pool = list(snapshot["media_pool"])


# 編集履歴 (Undo/Redo)
class CommandStack:

    def __init__(self, limit=100):
        self._limit = max(int(limit), 1)
        self._undo = []
        self._redo = []

    # コマンドを実行して履歴へ積む。変化が無ければ False を返し履歴も汚さない。
    def push(self, timeline, command):
        snapshot = _snapshot(timeline)
        try:
            changed = command.apply(timeline)
        except Exception:  # noqa: BLE001 (編集失敗で画面を落とさない)
            _logger.exception("編集操作に失敗したため元の状態へ戻します: %s", command.label)
            _restore(timeline, snapshot)
            return False
        if not changed:
            return False
        timeline.normalize()
        self._undo.append((command.label, snapshot))
        if len(self._undo) > self._limit:
            self._undo.pop(0)
        self._redo.clear()
        _logger.debug("編集: %s", command.label)
        return True

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    # 直前の操作を取り消す。取り消した操作のラベルを返す (無ければ None)。
    def undo(self, timeline):
        if not self._undo:
            return None
        label, snapshot = self._undo.pop()
        self._redo.append((label, _snapshot(timeline)))
        _restore(timeline, snapshot)
        timeline.normalize()
        return label

    # 取り消した操作をやり直す
    def redo(self, timeline):
        if not self._redo:
            return None
        label, snapshot = self._redo.pop()
        self._undo.append((label, _snapshot(timeline)))
        _restore(timeline, snapshot)
        timeline.normalize()
        return label

    def clear(self):
        self._undo.clear()
        self._redo.clear()

    # 次に取り消される操作のラベル (メニュー表示用)
    def undo_label(self):
        return self._undo[-1][0] if self._undo else ""

    def redo_label(self):
        return self._redo[-1][0] if self._redo else ""


# ------------------------------------------------------------------
# 共通ヘルパ
# ------------------------------------------------------------------

# 同一トラック内で、指定クリップを除いた他クリップと重ならない位置へ丸める
def _clamp_position(track, clip, desired, min_clip_sec):
    desired = max(float(desired), 0.0)
    others = sorted(
        (c for c in track.clips if c.id != clip.id),
        key=lambda c: c.timeline_start,
    )
    end = desired + clip.duration
    for other in others:
        if end <= other.timeline_start + _EPS or desired >= other.timeline_end - _EPS:
            continue
        # 重なっている: 近い側の境界へ寄せる
        to_left = other.timeline_start - clip.duration
        to_right = other.timeline_end
        if abs(desired - to_left) <= abs(desired - to_right) and to_left >= 0:
            desired = to_left
        else:
            desired = to_right
        end = desired + clip.duration
    return max(desired, 0.0)


# 素材のトリム上限 (画像は上限なし)
def _source_limit(timeline, clip):
    media = timeline.media_by_id(clip.media_id)
    if media is None:
        return None
    return media.max_source_sec()


# オーバーレイ要素 (V2 以降 + 字幕) を z_order 昇順で返す
def _overlay_elements(timeline):
    return timeline.overlay_elements(include_disabled=True)


# ------------------------------------------------------------------
# 範囲リップル (resolve2 §5.4 / R8)
# ------------------------------------------------------------------

# 詰める対象のトラックを返す (設定 timeline.ripple_sync_tracks)
#   sync_all=True  : 全トラック (既定。字幕・オーバーレイも一緒に詰める)
#   sync_all=False : 操作したトラックのみ (旧挙動。切り戻し用)
# 音声トラックは常に除外する: AudioClip は時刻を持たずリンク元の映像クリップから
# 導出するため (resolve.md R18)、映像を詰めれば自動的に追従する。
# ここで一緒に動かすと二重にずれる。
def ripple_target_tracks(timeline, source_track, sync_all=True):
    if not sync_all:
        return [source_track] if source_track is not None else []
    return [t for t in timeline.tracks if not t.is_audio()]


# 区間 [start, end] を 1 クリップへ適用する (resolve2 §5.4-3 の規則 1〜6)
# クリップは破壊的に更新する。尺が下限を割った場合は呼び出し側が捨てる。
# 種別 (映像/画像/字幕) で処理を分けない。区間を跨ぐ場合も分割せず尺を縮める (回答 Q8)。
# 戻り値: 変化があれば True
def _apply_range_removal(clip, start, end, delta):
    clip_start = clip.timeline_start
    clip_end = clip.timeline_end
    has_source = isinstance(clip, Clip)   # 字幕クリップは source_in/out を持たない

    # 規則 1: 区間より前 → 何もしない
    if clip_end <= start + _EPS:
        return False

    # 規則 2: 区間より後ろ → 詰める
    if clip_start >= end - _EPS:
        clip.timeline_start = clip_start - delta
        return True

    head = start - clip_start      # 区間より前に残る長さ
    tail = clip_end - end          # 区間より後ろに残る長さ

    # 規則 3: 区間に完全に含まれる → 削除 (尺を 0 にして呼び出し側で捨てる)
    if head <= _EPS and tail <= _EPS:
        clip.duration = 0.0
        return True

    # 規則 6: 区間を跨ぐ → 分割せず尺を delta 縮める (回答 Q8)
    if head > _EPS and tail > _EPS:
        clip.duration -= delta
        if has_source:
            clip.source_out -= delta
        return True

    # 規則 4: 頭だけかかる → 右端を start へトリム
    if head > _EPS:
        clip.duration = head
        if has_source:
            clip.source_out = clip.source_in + head
        return True

    # 規則 5: 尻だけかかる → 左端を end へトリムし、start の位置へ詰める
    if has_source:
        clip.source_in += (end - clip_start)
        clip.source_out = clip.source_in + tail
    clip.timeline_start = start
    clip.duration = tail
    return True


# リンク先の映像クリップを失った音声クリップを落とす (R18 の明示的な追随)
def _drop_orphan_audio_clips(timeline):
    for track in timeline.audio_tracks():
        track.clips = [
            c for c in track.clips if timeline.clip_by_id(c.link_clip) is not None
        ]


# タイムライン上の区間 [start, end] を対象トラックから抜いて詰める (resolve2 §5.4)
# リップル削除 (Delete) と A / D はいずれもこの処理へ帰着する。
# 音声トラックは対象外 (V1 からの導出で自動的に追従するため)。
# 戻り値: 何か変化があれば True
def ripple_remove_range(timeline, start, end, tracks, min_clip_sec=0.05):
    delta = float(end) - float(start)
    if delta <= _EPS:
        return False

    changed = False
    removed = shortened = moved = 0
    for track in tracks:
        if track.is_audio() or track.locked:
            continue
        survivors = []
        for clip in track.clips:
            was_start = clip.timeline_start
            if not _apply_range_removal(clip, start, end, delta):
                survivors.append(clip)
                continue
            changed = True
            if clip.duration < min_clip_sec - _EPS:
                removed += 1
                continue
            if abs(clip.timeline_start - was_start) > _EPS:
                moved += 1
            else:
                shortened += 1
            survivors.append(clip)
        track.clips = survivors

    _drop_orphan_audio_clips(timeline)
    if changed:
        _logger.info(
            "リップル: %.3f〜%.3fs (%.2fs) を除去 / 削除 %d 件・短縮 %d 件・移動 %d 件",
            start, end, delta, removed, shortened, moved,
        )
    return changed


# ------------------------------------------------------------------
# コマンド
# ------------------------------------------------------------------

class Command:

    label = "編集"

    # 変化があれば True を返す
    def apply(self, timeline):
        raise NotImplementedError


# クリップの開始位置を変更する (R10)
# A1 は時刻を持たないため追随処理は不要 (R18)。
class MoveClip(Command):

    label = "クリップの移動"

    def __init__(self, clip_id, new_start, min_clip_sec=0.05):
        self._clip_id = clip_id
        self._new_start = new_start
        self._min_clip_sec = min_clip_sec

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        track = timeline.track_of_clip(self._clip_id)
        if clip is None or track is None or track.locked:
            return False
        target = _clamp_position(track, clip, self._new_start, self._min_clip_sec)
        if abs(target - clip.timeline_start) <= _EPS:
            return False
        clip.timeline_start = target
        return True


# クリップの左右端をドラッグして尺を変える (R10)
# 左端: source_in と timeline_start を同量動かす / 右端: source_out を動かす
class TrimClip(Command):

    label = "クリップの長さ変更"

    def __init__(self, clip_id, edge, new_value, min_clip_sec=0.05):
        self._clip_id = clip_id
        self._edge = edge
        self._new_value = float(new_value)
        self._min_clip_sec = float(min_clip_sec)

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        track = timeline.track_of_clip(self._clip_id)
        if clip is None or track is None or track.locked:
            return False
        if self._edge == EDGE_LEFT:
            return self._trim_left(timeline, track, clip)
        return self._trim_right(timeline, track, clip)

    # 左端: 新しい開始時刻へ寄せる。素材の先頭 (source_in>=0) と直前クリップを超えない。
    def _trim_left(self, timeline, track, clip):
        target = float(self._new_value)
        # 直前クリップの終端より前へは伸ばせない
        previous_end = max(
            (c.timeline_end for c in track.clips
             if c.id != clip.id and c.timeline_end <= clip.timeline_start + _EPS),
            default=0.0,
        )
        target = max(target, previous_end)
        # 素材の先頭より前へは伸ばせない (字幕クリップは素材を持たないため無制限)
        if isinstance(clip, Clip):
            target = max(target, clip.timeline_start - clip.source_in)
        # 最小尺を割らない
        target = min(target, clip.timeline_end - self._min_clip_sec)
        delta = target - clip.timeline_start
        if abs(delta) <= _EPS:
            return False
        clip.timeline_start = target
        clip.duration -= delta
        if isinstance(clip, Clip):
            clip.source_in += delta
        return True

    # 右端: 新しい終了時刻へ寄せる。素材の終端と直後クリップを超えない。
    def _trim_right(self, timeline, track, clip):
        target = float(self._new_value)
        next_start = min(
            (c.timeline_start for c in track.clips
             if c.id != clip.id and c.timeline_start >= clip.timeline_end - _EPS),
            default=None,
        )
        if next_start is not None:
            target = min(target, next_start)
        if isinstance(clip, Clip):
            limit = _source_limit(timeline, clip)
            if limit is not None:
                target = min(target, clip.timeline_start + (limit - clip.source_in))
        target = max(target, clip.timeline_start + self._min_clip_sec)
        new_duration = target - clip.timeline_start
        if abs(new_duration - clip.duration) <= _EPS:
            return False
        clip.duration = new_duration
        if isinstance(clip, Clip):
            clip.source_out = clip.source_in + new_duration
        return True


# 再生ヘッド位置でクリップを分割する = 編集点の追加 (R10)
# 【R18 の明示的な追随①】リンクしている音声クリップも 2 つへ分ける。
class SplitClip(Command):

    label = "編集点の追加"

    def __init__(self, clip_id, at_sec, min_clip_sec=0.05):
        self._clip_id = clip_id
        self._at_sec = float(at_sec)
        self._min_clip_sec = float(min_clip_sec)

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        track = timeline.track_of_clip(self._clip_id)
        if clip is None or track is None or track.locked:
            return False
        offset = self._at_sec - clip.timeline_start
        # 両側が最小尺を満たさない位置では分割しない
        if offset < self._min_clip_sec or (clip.duration - offset) < self._min_clip_sec:
            return False

        if isinstance(clip, SubtitleClip):
            new_clip = clip.copy()
            new_clip.id = timeline.next_id("s")
            new_clip.timeline_start = self._at_sec
            new_clip.duration = clip.duration - offset
            clip.duration = offset
            track.clips.append(new_clip)
            return True

        new_clip = clip.copy()
        new_clip.id = timeline.next_id("c")
        new_clip.timeline_start = self._at_sec
        new_clip.duration = clip.duration - offset
        new_clip.source_in = clip.source_in + offset
        new_clip.source_out = clip.source_out
        clip.duration = offset
        clip.source_out = clip.source_in + offset
        track.clips.append(new_clip)

        # リンク音声も 2 つへ分ける (R18 §6.3.4)
        audio_clip = timeline.audio_clip_for(clip.id)
        if audio_clip is not None:
            audio_track = timeline.track_of_clip(audio_clip.id)
            if audio_track is not None:
                new_audio = audio_clip.copy()
                new_audio.id = timeline.next_id("a")
                new_audio.link_clip = new_clip.id
                audio_track.clips.append(new_audio)
        return True


# クリップを削除する (R10 / R17)
# ripple=False: 跡は空白として残る / ripple=True: 区間を抜いて全体を詰める (R8)
# 【R18 の明示的な追随②】リンクしている音声クリップも同時に削除する。
class DeleteClip(Command):

    def __init__(self, clip_ids, ripple=False, min_clip_sec=0.05, sync_all=True):
        self._clip_ids = list(clip_ids or [])
        self._ripple = bool(ripple)
        self._min_clip_sec = float(min_clip_sec)
        self._sync_all = bool(sync_all)
        self.label = "リップル削除" if ripple else "クリップの削除"

    def apply(self, timeline):
        # 後ろから消すことで、リップルのシフト量が前のクリップに影響しないようにする
        ordered = sorted(
            (c for c in (timeline.clip_by_id(i) for i in self._clip_ids) if c is not None),
            key=lambda c: c.timeline_start, reverse=True,
        )
        clip_ids = [c.id for c in ordered]
        if self._ripple:
            return self._apply_ripple(timeline, clip_ids)
        return self._apply_plain(timeline, clip_ids)

    # 空白を残す削除: 該当クリップ (とリンク音声) を消すだけで他は動かさない
    def _apply_plain(self, timeline, clip_ids):
        changed = False
        for clip_id in clip_ids:
            clip = timeline.clip_by_id(clip_id)
            track = timeline.track_of_clip(clip_id) if clip is not None else None
            if clip is None or track is None or track.locked:
                continue
            track.remove_clip(clip_id)
            # リンク音声も一緒に消す (R18)
            audio_clip = timeline.audio_clip_for(clip_id)
            if audio_clip is not None:
                audio_track = timeline.track_of_clip(audio_clip.id)
                if audio_track is not None:
                    audio_track.remove_clip(audio_clip.id)
            changed = True
        return changed

    # リップル削除: クリップの占める区間を抜いて全トラックを詰める (R8 / §5.4)
    def _apply_ripple(self, timeline, clip_ids):
        changed = False
        for clip_id in clip_ids:
            # 直前の区間除去で消えている可能性があるため毎回引き直す
            clip = timeline.clip_by_id(clip_id)
            track = timeline.track_of_clip(clip_id) if clip is not None else None
            if clip is None or track is None or track.locked:
                continue
            if ripple_remove_range(
                timeline, clip.timeline_start, clip.timeline_end,
                ripple_target_tracks(timeline, track, self._sync_all),
                self._min_clip_sec,
            ):
                changed = True
        return changed


# 再生ヘッドを境にクリップの一部を削除して全体を詰める (resolve2 §5.3 / R4・R6)
# side="before" : クリップ先頭〜再生ヘッド を削除 (A キー)
# side="after"  : 再生ヘッド〜クリップ末尾 を削除 (D キー)
# 抜く区間を決めたあとは範囲リップル (§5.4) に委譲する。
class RippleTrimToPlayhead(Command):

    def __init__(self, clip_id, at_sec, side, min_clip_sec=0.05, sync_all=True):
        self._clip_id = clip_id
        self._at_sec = float(at_sec)
        self._side = side
        self._min_clip_sec = float(min_clip_sec)
        self._sync_all = bool(sync_all)
        self.label = ("再生ヘッドより前をリップル削除" if side == "before"
                      else "再生ヘッドより後ろをリップル削除")

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        track = timeline.track_of_clip(self._clip_id) if clip is not None else None
        if clip is None or track is None or track.locked:
            return False
        # 再生ヘッドがクリップの中に無ければ何もしない
        if not (clip.timeline_start + _EPS < self._at_sec < clip.timeline_end - _EPS):
            return False

        if self._side == "before":
            start, end = clip.timeline_start, self._at_sec
        else:
            start, end = self._at_sec, clip.timeline_end

        # 残りが最小尺を割る場合はクリップごとリップル削除する (回答 Q4)
        remaining = clip.duration - (end - start)
        if remaining < self._min_clip_sec:
            _logger.info(
                "残り尺が下限を割るためクリップごとリップル削除しました: %s", clip.id)
            start, end = clip.timeline_start, clip.timeline_end

        _logger.debug("リップルトリム: clip=%s side=%s delta=%.3fs",
                      clip.id, self._side, end - start)
        return ripple_remove_range(
            timeline, start, end,
            ripple_target_tracks(timeline, track, self._sync_all),
            self._min_clip_sec,
        )


# メディアを Timeline へ追加する (R12)
# track_id 未指定・配置不可のときは新しい映像トラックを作る。
class AddMediaClip(Command):

    label = "メディアの追加"

    def __init__(self, media, timeline_start, duration, track_id=None,
                 max_video_tracks=8, origin_type="user_media"):
        self._media = media
        self._start = float(timeline_start)
        self._duration = float(duration)
        self._track_id = track_id
        self._max_video_tracks = int(max_video_tracks)
        self._origin_type = origin_type
        # 追加したクリップの ID (呼び出し側が選択状態にするため公開する)
        self.created_clip_id = None
        self.created_track_id = None

    def apply(self, timeline):
        if self._duration <= _EPS:
            return False
        # メディアプールへ登録 (同一パスは再利用する)
        media = timeline.media_by_path(self._media.path)
        if media is None:
            media = self._media
            if not media.id:
                media.id = timeline.next_id("m")
            timeline.media_pool.append(media)

        track = self._resolve_track(timeline)
        if track is None:
            return False

        clip = Clip(
            timeline.next_id("c"), media.id, self._start, self._duration,
            source_in=0.0, source_out=self._duration,
            z_order=self._next_z_order(timeline),
            transform=Transform(),
            origin={"type": self._origin_type},
        )
        track.clips.append(clip)
        self.created_clip_id = clip.id
        self.created_track_id = track.id

        # 音声付き動画は対応する音声トラック/クリップも用意する (§6.8)
        if media.has_audio and not media.is_image():
            audio_track = timeline.audio_track_for(track.id)
            if audio_track is None:
                audio_id, audio_index = timeline.next_audio_track_id()
                audio_track = Track(audio_id, TRACK_AUDIO, audio_index,
                                    name=f"Audio {audio_index}", link_track=track.id)
                timeline.tracks.append(audio_track)
            audio_track.clips.append(AudioClip(timeline.next_id("a"), clip.id))
        return True

    # 配置先トラックを決める。空き領域が無ければ新しい映像トラックを作る (§6.8)
    def _resolve_track(self, timeline):
        base = timeline.base_video_track()
        end = self._start + self._duration
        if self._track_id:
            track = timeline.track_by_id(self._track_id)
            # ベーストラック上・既存クリップと重なる場合は新トラックへ回す
            if (track is not None and track.is_video() and not track.locked
                    and not (base is not None and track.id == base.id)
                    and self._is_free(track, self._start, end)):
                return track
        for track in sorted(timeline.video_tracks(), key=lambda t: t.index):
            if base is not None and track.id == base.id:
                continue
            if track.locked:
                continue
            if self._is_free(track, self._start, end):
                return track
        return self._create_video_track(timeline)

    def _is_free(self, track, start, end):
        for clip in track.clips:
            if start < clip.timeline_end - _EPS and end > clip.timeline_start + _EPS:
                return False
        return True

    def _create_video_track(self, timeline):
        if len(timeline.video_tracks()) >= self._max_video_tracks:
            _logger.warning(
                "映像トラックの上限 (%d) に達しているため追加できません", self._max_video_tracks)
            return None
        track_id, index = timeline.next_video_track_id()
        track = Track(track_id, TRACK_VIDEO, index, name=f"Video {index}")
        timeline.tracks.append(track)
        return track

    # 既存オーバーレイの最前面より前へ置く
    def _next_z_order(self, timeline):
        elements = _overlay_elements(timeline)
        highest = max((e.z_order for e in elements), default=BASE_Z_ORDER)
        return highest + Z_ORDER_STEP


# 字幕クリップを追加する (Timeline の右クリック「字幕追加」)
# 右クリックした位置を開始として新しい字幕を 1 つ置く。
# 同じトラック上の既存字幕とは重ねない (重なると ASS で同時に 2 行出てしまうため):
#   ・クリックした位置が既存字幕の中 → 追加しない
#   ・次の字幕まで既定の尺が入らない  → その手前まで縮めて置く
class AddSubtitleClip(Command):

    label = "字幕の追加"

    def __init__(self, timeline_start, duration, text="", track_id=None,
                 role=DEFAULT_ROLE, min_clip_sec=0.05):
        self._start = max(float(timeline_start), 0.0)
        self._duration = float(duration)
        self._text = str(text or "")
        self._track_id = track_id
        self._role = role
        self._min_clip_sec = float(min_clip_sec)
        # 追加したクリップの ID (呼び出し側が選択状態にするため公開する)
        self.created_clip_id = None
        self.created_track_id = None

    def apply(self, timeline):
        track = self._resolve_track(timeline)
        if track is None or track.locked:
            return False
        duration = self._available_duration(track)
        if duration is None:
            _logger.info("字幕を追加できる空きがありません (%.3f 秒)", self._start)
            return False
        clip = SubtitleClip(
            timeline.next_id("s"), self._start, duration,
            text=self._text, role=self._role,
            z_order=DEFAULT_SUBTITLE_Z_ORDER,
            origin={"type": ORIGIN_USER_SUBTITLE},
        )
        track.clips.append(clip)
        self.created_clip_id = clip.id
        return True

    # 追加先の字幕トラックを決める (無ければ S1 を作る)
    def _resolve_track(self, timeline):
        if self._track_id:
            track = timeline.track_by_id(self._track_id)
            if track is not None and track.is_subtitle():
                return track
        track = timeline.base_subtitle_track()
        if track is not None:
            return track
        track = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
        timeline.tracks.append(track)
        self.created_track_id = track.id
        return track

    # 開始位置に置ける尺を返す (置けないときは None)
    def _available_duration(self, track):
        end = self._start + self._duration
        for clip in sorted(track.clips, key=lambda c: c.timeline_start):
            if clip.timeline_end <= self._start + _EPS:
                continue
            if clip.timeline_start <= self._start + _EPS:
                return None          # クリックした位置が既存字幕の中
            end = min(end, clip.timeline_start)
            break
        duration = end - self._start
        return duration if duration >= self._min_clip_sec else None


# 描画順を変更する (R8 / §6.5-2)
# オーバーレイ要素を z_order 昇順に並べ、対象を移動させて 10 刻みで振り直す。
class ChangeZOrder(Command):

    def __init__(self, clip_id, direction):
        self._clip_id = clip_id
        self._direction = direction
        self.label = {
            LAYER_TOP: "最前面へ", LAYER_UP: "前面へ",
            LAYER_DOWN: "背面へ", LAYER_BOTTOM: "最背面へ",
        }.get(direction, "レイヤー変更")

    def apply(self, timeline):
        elements = _overlay_elements(timeline)
        ids = [e.id for e in elements]
        if self._clip_id not in ids:
            return False
        index = ids.index(self._clip_id)
        target = self._target_index(index, len(elements))
        if target == index:
            return False
        element = elements.pop(index)
        elements.insert(target, element)
        # 10 刻みで振り直す (間に挿入する将来操作で全体再計算を避けるため)
        for order, item in enumerate(elements, 1):
            item.z_order = order * Z_ORDER_STEP
        return True

    def _target_index(self, index, count):
        if self._direction == LAYER_TOP:
            return count - 1
        if self._direction == LAYER_BOTTOM:
            return 0
        if self._direction == LAYER_UP:
            return min(index + 1, count - 1)
        if self._direction == LAYER_DOWN:
            return max(index - 1, 0)
        return index


# プレビュー上のドラッグで位置を変える (R7)
# ドラッグ確定時に 1 回だけ積む (ドラッグ中は積まない = 履歴を汚さない)。
class MoveOverlay(Command):

    def __init__(self, clip_id, x, y):
        self._clip_id = clip_id
        self._x = x
        self._y = y
        # x/y に None を渡すと位置指定を捨てる (字幕は設定の配置・余白へ戻る)
        self.label = "位置を既定へ戻す" if x is None or y is None else "位置の変更"

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if clip is None:
            return False
        if self._x is None or self._y is None:
            if clip.transform.x is None and clip.transform.y is None:
                return False
            clip.transform.x = None
            clip.transform.y = None
            return True
        if (clip.transform.x is not None and clip.transform.y is not None
                and abs(clip.transform.x - self._x) <= _EPS
                and abs(clip.transform.y - self._y) <= _EPS):
            return False
        clip.transform.x = float(self._x)
        clip.transform.y = float(self._y)
        return True


# 字幕の内容を変更する
class EditSubtitle(Command):

    label = "字幕の編集"

    def __init__(self, clip_id, **fields):
        self._clip_id = clip_id
        self._fields = fields

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if not isinstance(clip, SubtitleClip):
            return False
        changed = False
        for key, value in self._fields.items():
            if not hasattr(clip, key):
                continue
            if getattr(clip, key) != value:
                setattr(clip, key, value)
                changed = True
        return changed


# 使用 ON/OFF (旧画面の「使用」チェックに相当)
class SetClipEnabled(Command):

    label = "使用の切り替え"

    def __init__(self, clip_id, enabled):
        self._clip_id = clip_id
        self._enabled = bool(enabled)

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if clip is None:
            return False
        if isinstance(clip, SubtitleClip):
            if clip.use == self._enabled:
                return False
            clip.use = self._enabled
            return True
        if clip.enabled == self._enabled:
            return False
        clip.enabled = self._enabled
        return True


# 音声クリップのゲインを変える (R18: A1 固有の値。V1 には影響しない)
class SetAudioGain(Command):

    label = "音量の変更"

    def __init__(self, audio_clip_id, gain_db):
        self._clip_id = audio_clip_id
        self._gain_db = float(gain_db)

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if not isinstance(clip, AudioClip):
            return False
        if abs(clip.gain_db - self._gain_db) <= _EPS:
            return False
        clip.gain_db = self._gain_db
        return True


# 音声クリップのミュートを切り替える
class SetAudioMuted(Command):

    label = "ミュートの切り替え"

    def __init__(self, audio_clip_id, muted):
        self._clip_id = audio_clip_id
        self._muted = bool(muted)

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if not isinstance(clip, AudioClip):
            return False
        if clip.muted == self._muted:
            return False
        clip.muted = self._muted
        return True
