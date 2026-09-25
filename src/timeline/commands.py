# Timeline の編集操作 (docs/request/ver3/resolve.md §6.3.3)
# すべての編集を Command として実装し CommandStack に積む。
# UI 側は「コマンドを積む」以外の方法でモデルを触らないため、
# UI と編集処理の分離 (§4-2) が崩れず、Undo/Redo が全操作で一様に効く。
#
# R18 (§6.3.4): 音声クリップは時刻を持たず V1 から導出するため、
# 移動・トリム・リップルは追随処理なしで自動的に一致する。
# 明示的な追随が要るのはクリップ個数が変わる Split と Delete の 2 つだけ。
import copy
import os

from ..utils.logger import get_logger
from . import clipboard
from .model import (
    BASE_SUBTITLE_TRACK_ID,
    BASE_Z_ORDER,
    COMMENT_SUBTITLE_TRACK_ID,
    DEFAULT_ROLE,
    DEFAULT_SUBTITLE_Z_ORDER,
    ORIGIN_USER_SUBTITLE,
    ROLE_COMMENT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    Z_ORDER_STEP,
    AudioClip,
    Clip,
    MediaRef,
    SubtitleClip,
    Track,
    Transform,
)

_logger = get_logger(__name__)

# 位置比較の許容誤差
_EPS = 1e-6

# 役割別の字幕トラックが未設定のときに使う既定 (ver3 resolve11 §5.2)
_DEFAULT_SUBTITLE_TRACKS_CFG = {
    "role_track_enabled": True,
    "comment_track_id": COMMENT_SUBTITLE_TRACK_ID,
    "comment_track_name": "Comment",
    "comment_track_index": 2,
    "auto_move_on_role_change": True,
}


# 役割に対応する字幕トラックを返す。無ければ作る (ver3 resolve11 §5.2-1)
# cfg: timeline_config()["subtitle_tracks"]。None なら既定 (コメントは S2) で動く。
def ensure_subtitle_track(timeline, role, cfg=None):
    cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(cfg or {})}
    track = timeline.subtitle_track_for_role(
        role, cfg["comment_track_id"], cfg["role_track_enabled"])
    if track is not None:
        return track
    if not cfg["role_track_enabled"] or role != ROLE_COMMENT:
        track = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    else:
        track = Track(cfg["comment_track_id"], TRACK_SUBTITLE,
                      cfg["comment_track_index"], name=cfg["comment_track_name"])
        _logger.info("コメント用の字幕トラックを作成しました: %s", track.id)
    timeline.tracks.append(track)
    return track


# トラック上に [start, end) と重なるクリップがあるか (自分自身は除く)
def _overlaps_any(track, start, end, exclude_id=None):
    for clip in track.clips:
        if exclude_id is not None and clip.id == exclude_id:
            continue
        if clip.timeline_start < end - _EPS and start < clip.timeline_end - _EPS:
            return True
    return False


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
        # source も控える (ver3 resolve9 §3-3)。アーカイブ用のテーマは
        # source.archive.clips[].theme にあり、コマンドで書き換えるため
        # Undo で戻せる必要がある。クリップ用は小さな辞書のため負担にならない。
        "source": copy.deepcopy(timeline.source),
    }


# 控えた状態へ戻す
def _restore(timeline, snapshot):
    timeline.tracks = [
        Track(t["id"], t["kind"], t["index"], t["name"], t["enabled"],
              t["locked"], t["is_base"], t["link_track"], t["clips"])
        for t in snapshot["tracks"]
    ]
    timeline.media_pool = list(snapshot["media_pool"])
    if "source" in snapshot:
        timeline.source = snapshot["source"]


# 編集履歴 (Undo/Redo)
class CommandStack:

    def __init__(self, limit=100):
        self._limit = max(int(limit), 1)
        self._undo = []
        self._redo = []
        # 保存済みの位置 (= その時点の undo スタックの深さ / resolve7 §5.6)。
        # None は「もう保存時の状態には戻れない」ことを表す。
        self._clean_depth = 0

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
        # 保存点より浅い位置から新しい操作を積む = 保存点を含む枝を捨てた。
        # 以後どう操作しても保存時の状態へは戻れないため印を無効化する (resolve7 §5.6)。
        if self._clean_depth is not None and len(self._undo) < self._clean_depth:
            self._clean_depth = None
        self._undo.append((command.label, snapshot))
        if len(self._undo) > self._limit:
            self._undo.pop(0)
            # 先頭を捨てると深さの基準がずれるため保存点も 1 つ手前へ寄せる。
            # 0 を下回ったら保存点そのものが履歴から消えたということ。
            if self._clean_depth is not None:
                self._clean_depth -= 1
                if self._clean_depth < 0:
                    self._clean_depth = None
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
        # 履歴を捨てても「今が保存済みの状態か」は変わらない。
        # 保存済みなら深さ 0 を新しい保存点にし、未保存なら二度と一致させない。
        was_clean = self.is_clean()
        self._undo.clear()
        self._redo.clear()
        self._clean_depth = 0 if was_clean else None

    # 現在が保存済みの状態か (Undo で保存点まで戻った場合も True になる / resolve7 §5.6)
    def is_clean(self):
        return self._clean_depth is not None and self._clean_depth == len(self._undo)

    # 保存できた時点を「保存点」として覚える
    def mark_clean(self):
        self._clean_depth = len(self._undo)

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


# 静止画クリップか (素材内の時間を持たず、尺を前後どちらへも伸ばせる素材か)
# 素材が見つからないときは False (= 動画として扱い、素材の制限を残す安全側 / ver4 resolve §3-1)。
# 字幕クリップは素材を持たないため False。
def _is_still_image(timeline, clip):
    if not isinstance(clip, Clip):
        return False
    media = timeline.media_by_id(clip.media_id)
    return media is not None and media.is_image()


# オーバーレイ要素 (V2 以降 + 字幕) を z_order 昇順で返す
def _overlay_elements(timeline):
    return timeline.overlay_elements(include_disabled=True)


# クリップを at_sec で 2 つに割り、後半のクリップを返す (割れなければ None)
# 両側が最小尺を満たさない位置では割らない。リンク音声も 2 本へ分ける (R18 §6.3.4)。
# ver3 resolve10 §5.2: SplitClip と貼り付けの割り込み (make_room_for_range) で
# 分割の規約を共有するため関数へ出した。2 か所へ書くと必ずズレるため 1 つに寄せる。
def split_clip_at(timeline, track, clip, at_sec, min_clip_sec=0.05):
    offset = at_sec - clip.timeline_start
    if offset < min_clip_sec or (clip.duration - offset) < min_clip_sec:
        return None

    if isinstance(clip, SubtitleClip):
        new_clip = clip.copy()
        new_clip.id = timeline.next_id("s")
        new_clip.timeline_start = at_sec
        new_clip.duration = clip.duration - offset
        clip.duration = offset
        track.clips.append(new_clip)
        return new_clip

    new_clip = clip.copy()
    new_clip.id = timeline.next_id("c")
    new_clip.timeline_start = at_sec
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
    return new_clip


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
# 割り込み (貼り付け用 / ver3 resolve10 §3-4 / §5.2-2)
# ------------------------------------------------------------------
# ripple_remove_range() の逆にあたるが、対称形ではない:
#   ・対象は「干渉したトラックだけ」(呼び出し側が渡す)
#   ・ずらす量は「必要な分だけ」トラックごとに計算する
#   ・干渉が無ければ 1 か所も変更しない
# 貼り付けたノードが優先され、既存ノードが右へ逃げる、という要望に対応する。

# 割り込み時に跨ぎクリップをどう扱うか (ver3 resolve10 §3-4)
INSERT_SPLIT = "split"              # start で分割し後半だけずらす (既定)
INSERT_SHIFT_WHOLE = "shift_whole"  # 丸ごとずらす


# クリップ 1 件を [start, ...) の外へ出す方法と「動き出す位置」を返す
#   (None,    None)     : 区間より前にいる。何もしない
#   ("move",  開始位置)  : 丸ごと右へずらす
#   ("split", start)    : start で分割し、後半だけ右へずらす
#   ("trim",  None)     : start へ終端を切り詰める (動かさない)
# ver3 resolve10 §3-4。ずらし量の計算 (required_shift) と実際の適用
# (make_room_for_range) で必ず同じ判定を使うため、分岐をこの 1 関数へ寄せる。
# 2 か所へ書くと必ずズレて、貼り付け区間に重なりが残る。
def _insert_action(clip, start, min_clip_sec, policy):
    if clip.timeline_end <= start + _EPS:
        return (None, None)                           # 区間より前 → 無関係
    if clip.timeline_start >= start - _EPS:
        return ("move", clip.timeline_start)          # start 以降 → 丸ごとずらす
    # ここから: start を跨ぐクリップ
    head = start - clip.timeline_start                # start より前に残る長さ
    tail = clip.timeline_end - start                  # start より後ろの長さ
    if policy == INSERT_SHIFT_WHOLE or head < min_clip_sec:
        return ("move", clip.timeline_start)          # 頭がごく短い → 丸ごと右へ
    if tail < min_clip_sec:
        return ("trim", None)                         # 尻がごく短い → start で切る
    return ("split", start)                           # start で分割し後半が動く


# トラック上で [start, end) を空けるのに必要なずらし量を求める (ver3 resolve10 §3-4)
# 干渉が無ければ 0.0 (＝ずらさずそのまま置ける)。
# 「動き出す位置」が最も早いクリップが、必要量を決める。
def required_shift(track, start, end, min_clip_sec=0.05, policy=INSERT_SPLIT):
    shift = 0.0
    for clip in track.clips:
        _action, move_from = _insert_action(clip, start, min_clip_sec, policy)
        if move_from is None:
            continue
        if move_from < end - _EPS:                    # 貼り付け区間へ食い込んでいる
            shift = max(shift, end - move_from)
    return shift


# クリップの終端を at へ切り詰める (最小尺未満の食い込みを潰すための後始末)
def _trim_tail_to(clip, at):
    duration = max(at - clip.timeline_start, 0.0)
    if isinstance(clip, Clip):
        clip.source_out = clip.source_in + duration
    clip.duration = duration


# トラック上の [start, end) を空ける (ver3 resolve10 §3-4 / §5.2-2)
# shift を渡さなければ required_shift() で最小限を求める。
# 全トラックを同量ずらす場合 (ripple_scope=base_syncs_all / all) は呼び出し側が指定する。
# 音声トラックは対象外: AudioClip は時刻を持たず V1 から導出するため自動で追従する (R18)。
# ロック中のトラックも触らない (ロックの意味を優先する)。
# 戻り値: 実際にずらした量 (0.0 = 何も動かしていない)
#
# 不変条件:
#   ① 戻った時点で [start, end) に重なる既存クリップは 1 つも無い
#   ② 干渉が無ければ Timeline を 1 か所も変えない
#   ③ start 以降のクリップは全て同じ shift だけ動く (間隔が保たれ、新しい重なりを作らない)
def make_room_for_range(timeline, track, start, end, min_clip_sec=0.05,
                        policy=INSERT_SPLIT, shift=None):
    if track.is_audio() or track.locked:
        return 0.0
    if shift is None:
        shift = required_shift(track, start, end, min_clip_sec, policy)
    if shift <= _EPS:
        return 0.0

    moved = split = trimmed = 0
    # split_clip_at() が track.clips へ追加するため、走査は複製に対して行う
    for clip in list(track.clips):
        action, _move_from = _insert_action(clip, start, min_clip_sec, policy)
        if action is None:
            continue
        if action == "move":
            clip.timeline_start += shift
            moved += 1
        elif action == "trim":
            _trim_tail_to(clip, start)
            trimmed += 1
        else:                                          # split
            tail = split_clip_at(timeline, track, clip, start, min_clip_sec)
            if tail is None:                           # 念のため (通常は起きない)
                clip.timeline_start += shift
                moved += 1
            else:
                tail.timeline_start += shift
                split += 1

    _logger.info(
        "貼り付けの割り込み: %s の %.3fs へ %.2fs を確保 / 移動 %d 件・分割 %d 件・切詰 %d 件",
        track.id, start, shift, moved, split, trimmed)
    return shift


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
        # 要求位置が現在位置と同じなら何もしない。
        # clamp を先に通すと、すでに他クリップと重なっている場合に
        # 「重なりの解消」として別の位置へ動いてしまう
        # (docs/error/20260812/Analyze.md §5-2)
        if abs(float(self._new_start) - clip.timeline_start) <= _EPS:
            return False
        target = _clamp_position(track, clip, self._new_start, self._min_clip_sec)
        if abs(target - clip.timeline_start) <= _EPS:
            return False
        clip.timeline_start = target
        return True


# クリップの左右端をドラッグして尺を変える (R10)
# 左端: source_in と timeline_start を同量動かす (静止画は source_in を動かさない) / 右端: source_out を動かす
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

    # 左端: 新しい開始時刻へ寄せる。直前クリップの終端を超えない (0 秒未満にもならない)。
    # 動画は素材の先頭 (source_in>=0) も超えない。静止画は素材内の時間を持たないため
    # 素材先頭の制限を掛けない (右端で _source_limit が画像に None を返すのと対称 / ver4 resolve §2.3)。
    def _trim_left(self, timeline, track, clip):
        target = float(self._new_value)
        # 直前クリップの終端より前へは伸ばせない (直前が無ければ 0 秒)
        previous_end = max(
            (c.timeline_end for c in track.clips
             if c.id != clip.id and c.timeline_end <= clip.timeline_start + _EPS),
            default=0.0,
        )
        target = max(target, previous_end)
        still = _is_still_image(timeline, clip)
        # 素材の先頭より前へは伸ばせない (字幕・静止画は素材内の時間を持たないため無制限)
        if isinstance(clip, Clip) and not still:
            target = max(target, clip.timeline_start - clip.source_in)
        # 最小尺を割らない
        target = min(target, clip.timeline_end - self._min_clip_sec)
        delta = target - clip.timeline_start
        if abs(delta) <= _EPS:
            return False
        clip.timeline_start = target
        clip.duration -= delta
        if still:
            # 静止画は source_in を動かさず、source_out だけ尺に合わせる (ver4 resolve §3-2)。
            # source_in は描画・出力のどちらからも参照されない (§2.5)。
            clip.source_out = clip.source_in + clip.duration
        elif isinstance(clip, Clip):
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
        # 分割の規約は split_clip_at() が持つ (ver3 resolve10 §5.2)。
        # 両側が最小尺を満たさない位置では割れず None が返る。
        return split_clip_at(timeline, track, clip, self._at_sec,
                             self._min_clip_sec) is not None


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
                 max_video_tracks=8, origin_type="user_media", scale=None):
        self._media = media
        self._start = float(timeline_start)
        self._duration = float(duration)
        self._track_id = track_id
        self._max_video_tracks = int(max_video_tracks)
        self._origin_type = origin_type
        # 置いた直後の大きさ (キャンバス幅に対する比率 / resolve7 §7 default_scale_mode)。
        # None は従来どおり 1.0 = キャンバス幅いっぱい。
        self._scale = None if scale is None else float(scale)
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
            transform=Transform(scale=1.0 if self._scale is None else self._scale),
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
                 role=DEFAULT_ROLE, min_clip_sec=0.05, subtitle_tracks_cfg=None):
        self._start = max(float(timeline_start), 0.0)
        self._duration = float(duration)
        self._text = str(text or "")
        self._track_id = track_id
        self._role = role
        self._min_clip_sec = float(min_clip_sec)
        # 役割別トラックの設定 (ver3 resolve11 §5.2-3)
        self._tracks_cfg = subtitle_tracks_cfg
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

    # 追加先の字幕トラックを決める
    # トラックの明示指定があればそれを使い、無ければ役割に対応するトラック
    # (コメントは S2 / それ以外は S1) へ置く。無ければ作る (ver3 resolve11 §5.2-3)。
    def _resolve_track(self, timeline):
        if self._track_id:
            track = timeline.track_by_id(self._track_id)
            if track is not None and track.is_subtitle():
                return track
        before = {t.id for t in timeline.subtitle_tracks()}
        track = ensure_subtitle_track(timeline, self._role, self._tracks_cfg)
        if track is not None and track.id not in before:
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


# ------------------------------------------------------------------
# 貼り付け (ver3 resolve10)
# ------------------------------------------------------------------

# 右へずらす範囲 (ver3 resolve10 §3-5)
SCOPE_BASE_SYNCS_ALL = "base_syncs_all"  # V1 へ貼るときだけ全トラック (既定)
SCOPE_TRACK = "track"                    # 常に干渉したトラックだけ
SCOPE_ALL = "all"                        # 常に全トラック

# アーカイブ用 V1 の archive_clip_index の扱い (ver3 resolve10 §3-8)
ARCHIVE_INDEX_INHERIT = "inherit"
ARCHIVE_INDEX_KEEP = "keep"

# アーカイブ書き出しのグループ分けに使う origin のキー (archive/timeline_builder.py)
_ORIGIN_ARCHIVE_INDEX = "archive_clip_index"


# クリップボードの内容を at_sec へ貼り付ける (ver3 resolve10 §5.3)
# 貼り付けたノードが優先され、干渉した既存ノードは右へずれる (要望 P3)。
# ずれるのは干渉したトラックだけ・必要な分だけで、干渉が無ければ何も動かない (要望 P4)。
# ただし V1 (ベース映像) へ貼るときは字幕・オーバーレイも同量ずらす (要望 P14 / §3-5)。
#
# 【手順】順序に意味がある:
#   ① 解決 : 貼り付け先トラックと素材を先に確定する (貼れない項目のために場所を空けない)
#   ② 確保 : トラックごとに必要な分だけ場所を空ける
#   ③ 配置 : 空いた区間へクリップを作って置く
class PasteClips(Command):

    def __init__(self, payload, at_sec, min_clip_sec=0.05,
                 insert_policy=INSERT_SPLIT, ripple_scope=SCOPE_BASE_SYNCS_ALL,
                 archive_index_policy=ARCHIVE_INDEX_INHERIT, max_video_tracks=8,
                 subtitle_tracks_cfg=None):
        self._payload = payload or {}
        self._at = max(float(at_sec), 0.0)
        self._min_clip_sec = float(min_clip_sec)
        self._insert_policy = insert_policy
        self._ripple_scope = ripple_scope
        self._archive_index_policy = archive_index_policy
        self._max_video_tracks = int(max_video_tracks)
        # 役割別の字幕トラック (貼り付け先が見つからないときの解決に使う / resolve11 §5.2-4)
        self._subtitle_tracks_cfg = subtitle_tracks_cfg
        # 呼び出し側が選択・再生ヘッド・案内に使う結果
        self.created_clip_ids = []
        self.pasted_end_sec = None
        self.skipped = 0
        self.shifted_sec = 0.0        # 実際にずらした最大量 (0 = 何も動いていない)
        self.label = "貼り付け"

    def apply(self, timeline):
        items = self._payload.get("items") or []
        if not items:
            return False

        # ① 解決: 貼れる項目だけを (トラック, 項目, 素材ID, 区間) の形へ落とす
        plans = []
        for item in items:
            plan = self._resolve_item(timeline, item)
            if plan is None:
                self.skipped += 1
                continue
            plans.append(plan)
        if not plans:
            return False

        # ② 確保: トラックごとの外接区間で場所を空ける
        self._make_room(timeline, plans)

        # ③ 配置
        for plan in plans:
            self._place(timeline, plan)

        # 【重要】少しでも Timeline を変えたら True を返す。CommandStack.push() は
        # False のときスナップショットへ戻さないため、場所だけ空いて 1 件も貼れなかった
        # 場合に False を返すと、Undo できない変更が残る。
        return bool(self.created_clip_ids) or self.shifted_sec > _EPS

    # 項目 1 件について貼り付け先と素材を解決する (貼れなければ None)
    def _resolve_item(self, timeline, item):
        track = self._resolve_track(timeline, item)
        if track is None or track.locked:
            return None
        duration = float((item.get("clip") or {}).get("duration") or 0.0)
        if duration < self._min_clip_sec:
            return None
        media_id = None
        if item.get("kind") == clipboard.KIND_VIDEO:
            media_id = self._resolve_media(timeline, item)
            if media_id is None:            # 素材が見つからない → 貼らない (§3-7)
                return None
        start = self._at + float(item.get("offset_sec") or 0.0)
        return {"track": track, "item": item, "media_id": media_id,
                "start": start, "end": start + duration}

    # トラックごとに必要な分だけ場所を空ける (§3-4 / §3-5)
    # 貼り付け先に V1 (ベース映像) が含まれる場合は、字幕・オーバーレイも同量ずらす
    # (既定 base_syncs_all / 要望 P14)。V1 だけ動かすと字幕が置いていかれるため。
    def _make_room(self, timeline, plans):
        # 同じトラックへ乗る項目は外接区間 (最小開始〜最大終端) でまとめて 1 回だけ空ける。
        # コピー元にあった項目間の隙間は隙間のまま貼る (並びを崩さないため)。
        regions, tracks = {}, {}
        for plan in plans:
            track = plan["track"]
            tracks[track.id] = track
            low, high = regions.get(track.id, (plan["start"], plan["end"]))
            regions[track.id] = (min(low, plan["start"]), max(high, plan["end"]))

        base = timeline.base_video_track()
        sync_all = (self._ripple_scope == SCOPE_ALL) or (
            self._ripple_scope == SCOPE_BASE_SYNCS_ALL
            and base is not None and base.id in tracks)

        if not sync_all:
            # 干渉したトラックだけを、それぞれ必要な分だけずらす (要望 P4)
            for track_id, (start, end) in regions.items():
                shift = make_room_for_range(
                    timeline, tracks[track_id], start, end,
                    self._min_clip_sec, self._insert_policy)
                self.shifted_sec = max(self.shifted_sec, shift)
            return

        # 全トラックを同量ずらす (縦の同期を保つため最大値へ揃える / §3-5)。
        # make_room_for_range() が音声トラックとロック中のトラックを弾く。
        # shift が 0 (= 貼り付け先に十分な隙間がある) なら 1 つも動かさない。
        shift = 0.0
        for track_id, (start, end) in regions.items():
            shift = max(shift, required_shift(tracks[track_id], start, end,
                                              self._min_clip_sec, self._insert_policy))
        if shift <= _EPS:
            return
        start = min(s for s, _e in regions.values())
        end = max(e for _s, e in regions.values())
        for track in timeline.tracks:
            make_room_for_range(timeline, track, start, end, self._min_clip_sec,
                                self._insert_policy, shift=shift)
        self.shifted_sec = shift

    # 項目 1 件を置く (場所は ② で空いているため衝突しない)
    def _place(self, timeline, plan):
        track, item = plan["track"], plan["item"]
        clip = self._instantiate(timeline, track, item, plan["media_id"],
                                 plan["start"], plan["end"] - plan["start"])
        track.clips.append(clip)
        self.created_clip_ids.append(clip.id)
        self._attach_audio(timeline, track, clip, item)
        end = clip.timeline_end
        self.pasted_end_sec = (end if self.pasted_end_sec is None
                               else max(self.pasted_end_sec, end))

    # 新しいクリップを作る (ID は採番し直す。尺・素材位置はコピー元のまま)
    def _instantiate(self, timeline, track, item, media_id, start, duration):
        data = dict(item.get("clip") or {})
        if item.get("kind") == clipboard.KIND_SUBTITLE:
            clip = SubtitleClip.from_dict(data)
            clip.id = timeline.next_id("s")
            clip.timeline_start = start
            clip.duration = duration
            return clip

        clip = Clip.from_dict(data)
        clip.id = timeline.next_id("c")
        clip.media_id = media_id
        clip.timeline_start = start
        clip.duration = duration
        clip.origin = self._resolve_origin(timeline, track,
                                           dict(data.get("origin") or {}), start)
        return clip

    # アーカイブ用 V1 の archive_clip_index を決める (§3-8)
    # 書き出しは V1 を index で区切り、index からフォルダ名・出力ファイル名を作るため、
    # 同じ index のグループが 2 つできると出力が衝突する。直前のクリップの index を
    # 引き継げば新しいグループ境界を作らないので、構造的に衝突しない。
    # 判定は「場所を空けた後」の並びに対して行う (ずらす前だと直前クリップを取り違える)。
    def _resolve_origin(self, timeline, track, origin, start):
        if self._archive_index_policy != ARCHIVE_INDEX_INHERIT:
            return origin
        if not isinstance(timeline.source, dict) or not timeline.source.get("archive"):
            return origin                        # クリップ用 Timeline は対象外
        base = timeline.base_video_track()
        if base is None or track.id != base.id:
            return origin                        # V1 以外は書き出し分割に関与しない
        previous = following = None
        for clip in sorted(base.clips, key=lambda c: c.timeline_start):
            if clip.timeline_start <= start + _EPS:
                previous = clip
            elif following is None:
                following = clip
        neighbor = previous or following
        if neighbor is not None and _ORIGIN_ARCHIVE_INDEX in neighbor.origin:
            origin[_ORIGIN_ARCHIVE_INDEX] = neighbor.origin[_ORIGIN_ARCHIVE_INDEX]
        return origin

    # 貼り付け先トラックを決める (§3-3)
    # コピー元のトラックを引き継ぐ。AddMediaClip の「重なるなら別トラックへ逃がす」
    # 規約は使わない (要望は「押しのける」であり、逃がすと違う結果になるため)。
    def _resolve_track(self, timeline, item):
        info = item.get("track") or {}
        if item.get("kind") == clipboard.KIND_SUBTITLE:
            track = timeline.track_by_id(str(info.get("id") or ""))
            if track is not None and track.is_subtitle():
                return track
            # 別プロジェクトへ貼るとコピー元のトラック ID が無い。役割で置き場所を決め直す
            # (コメントが S1 へ落ちないようにする / ver3 resolve11 §5.2-4)。
            role = str((item.get("clip") or {}).get("role") or DEFAULT_ROLE)
            return ensure_subtitle_track(timeline, role, self._subtitle_tracks_cfg)

        if info.get("is_base"):
            return timeline.base_video_track()
        track = timeline.track_by_id(str(info.get("id") or ""))
        if track is not None and track.is_video():
            return track
        if len(timeline.video_tracks()) >= self._max_video_tracks:
            _logger.warning("映像トラックの上限 (%d) に達しているため貼り付けません",
                            self._max_video_tracks)
            return None
        track_id, index = timeline.next_video_track_id()
        track = Track(track_id, TRACK_VIDEO, index, name=f"Video {index}")
        timeline.tracks.append(track)
        return track

    # 素材をプールへ再登録して media_id を返す (見つからなければ None / §3-7)
    # 別画面の Timeline へ貼ると media_id が存在しないため、パスで引き直す。
    def _resolve_media(self, timeline, item):
        data = item.get("media")
        clip_media_id = str((item.get("clip") or {}).get("media_id") or "")
        if not data:
            return clip_media_id if timeline.media_by_id(clip_media_id) else None
        path = str(data.get("path") or "")
        existing = timeline.media_by_path(path) if path else None
        if existing is not None:
            return existing.id
        if not path or not os.path.exists(path):
            # 実ファイルが無いまま貼ると黒画面のクリップが増えるだけなので貼らない
            _logger.warning("素材が見つからないため貼り付けません: %s", path)
            return None
        media = MediaRef.from_dict(data)
        media.id = timeline.next_id("m")
        timeline.media_pool.append(media)
        return media.id

    # リンク音声を作る (§3-6)。素材に音声が無ければ作らない。
    def _attach_audio(self, timeline, track, clip, item):
        if item.get("kind") != clipboard.KIND_VIDEO:
            return
        media = timeline.media_by_id(clip.media_id)
        if media is None or media.is_image() or not media.has_audio:
            return
        audio_track = timeline.audio_track_for(track.id)
        if audio_track is None:
            audio_id, audio_index = timeline.next_audio_track_id()
            audio_track = Track(audio_id, TRACK_AUDIO, audio_index,
                                name=f"Audio {audio_index}", link_track=track.id)
            timeline.tracks.append(audio_track)
        attrs = item.get("audio") or {}
        audio_track.clips.append(AudioClip(
            timeline.next_id("a"), clip.id,
            gain_db=attrs.get("gain_db", 0.0), muted=attrs.get("muted", False)))


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


# プレビュー上の四隅ハンドル / インスペクタで大きさを変える (resolve7 §3-1)
# 縦横比は transform.scale がスカラー 1 つであることで構造的に保たれるため、
# 「固定を実装する」のではなく「固定でない指定を UI から与えない」だけでよい。
# ドラッグ確定時に 1 回だけ積む (ドラッグ中は積まない = 履歴を汚さない / MoveOverlay と同じ)。
class ResizeOverlay(Command):

    label = "大きさの変更"

    # scale: キャンバス幅に対する比率 (1.0 = キャンバス幅いっぱい)
    def __init__(self, clip_id, scale, min_scale=0.02, max_scale=4.0):
        self._clip_id = clip_id
        self._scale = float(scale)
        self._min = float(min_scale)
        self._max = float(max_scale)

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        # 字幕の大きさはフォントサイズで表すため、ここでは扱わない (transform は位置のみ)
        if clip is None or isinstance(clip, SubtitleClip) or not hasattr(clip, "transform"):
            return False
        # ベース (V1) のクリップはキャンバスへ合わせて描かれ transform を見ない (renderer)
        track = timeline.track_of_clip(clip.id)
        base = timeline.base_video_track()
        if track is None or (base is not None and track.id == base.id):
            return False
        value = min(max(self._scale, self._min), self._max)
        if abs(float(clip.transform.scale or 1.0) - value) <= _EPS:
            return False
        clip.transform.scale = value
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


# 字幕の役割を変え、必要ならコメント用トラック (S2) へ移す (ver3 resolve11 §5.2-2)
# 【重要】役割の書き換えは必ず通し、トラック移動は best-effort とする。
#   出力 (色・Style・配置・アイコン・背景) を決めるのは role であって置き場所ではないため、
#   移動できなくても見た目は正しく出る。ここで役割変更ごと失敗させると
#   「色は変えられるのに役割は変えられない」という不可解な挙動になる。
# 移動しても clip.id は変えない (選択・Undo・アーカイブ index の紐付けを切らないため)。
class ChangeSubtitleRole(Command):

    label = "字幕の役割変更"

    def __init__(self, clip_id, role, subtitle_tracks_cfg=None):
        self._clip_id = clip_id
        self._role = str(role or DEFAULT_ROLE)
        self._cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(subtitle_tracks_cfg or {})}
        # 呼び出し側の案内文言用 (移動できたか / 移動先が埋まっていたか)
        self.moved = False
        self.move_blocked = False

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if not isinstance(clip, SubtitleClip):
            return False
        changed = clip.role != self._role
        clip.role = self._role
        if not (self._cfg["role_track_enabled"]
                and self._cfg["auto_move_on_role_change"]):
            return changed

        source = timeline.track_of_clip(clip.id)
        target = ensure_subtitle_track(timeline, self._role, self._cfg)
        if source is None or target is None or target.id == source.id:
            return changed
        if target.locked or _overlaps_any(
                target, clip.timeline_start, clip.timeline_end, exclude_id=clip.id):
            self.move_blocked = True
            _logger.info("移動先 %s が空いていないため役割だけ変更しました: %s",
                         target.id, clip.id)
            return changed

        source.remove_clip(clip.id)
        target.clips.append(clip)
        target.sort_clips(timeline)
        self.moved = True
        return True


# 複数の字幕クリップをまとめて変更する (ver3 resolve6 §3-10 / §5.10)
# Undo はスナップショット方式のため、N 件の変更がまとめて 1 手で戻る。
# 選択には映像・音声クリップも混ざり得るので、字幕以外の ID は黙って読み飛ばす。
class EditSubtitles(Command):

    def __init__(self, clip_ids, **fields):
        self._clip_ids = [i for i in (clip_ids or []) if i]
        self._fields = fields
        # 何件に効く操作なのかを履歴ボタンへ出す (「元に戻す: 字幕の編集（3 件）」)
        self.label = ("字幕の編集" if len(self._clip_ids) <= 1
                      else f"字幕の編集（{len(self._clip_ids)} 件）")

    def apply(self, timeline):
        changed = False
        for clip_id in self._clip_ids:
            clip = timeline.clip_by_id(clip_id)
            if not isinstance(clip, SubtitleClip):
                continue
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


# ------------------------------------------------------------------
# トラッキングぼかしの指定 (ver5 resolve8 §5.9)
# ------------------------------------------------------------------
#
# 指定は timeline.source["blur"] にあり、_snapshot が source を deepcopy するため
# **apply の中で書き換えるだけで Undo/Redo が効く**。Timeline 編集画面の
# 「元に戻す」からぼかし指定も戻せるため、利用者から見て操作の一貫性がある。
#
# 指定は「全面ぼかし (frame)」と「囲み (area)」の 2 種類だけで、並び順が重ね順になる
# (後から足したものが上)。囲みの位置はキーフレームで持つ (resolve8 §5.2)。


# 指定を 1 件以上足す (ver5 resolve8 §5.9)
#
# 「ボカさない」の 1 件目は「全面ぼかし + 囲み」を **1 回で**足す。
# こうしないと Ctrl+Z を 2 回押さないと元へ戻らない。
class AddBlurSpecs(Command):

    label = "ぼかし指定の追加"

    #   specs     : decisions.make_frame_spec / make_area_spec の戻り値の一覧
    #   to_bottom : True なら一覧の先頭 (= 一番下の層) へ入れる
    def __init__(self, specs, to_bottom=False):
        if isinstance(specs, dict):
            specs = [specs]
        self._specs = [dict(spec) for spec in (specs or []) if spec]
        self._to_bottom = bool(to_bottom)

    def apply(self, timeline):
        from ..blur import decisions as blur_decisions   # noqa: PLC0415 (機能 OFF なら読まない)

        if not self._specs:
            return False
        current = blur_decisions.load(timeline)
        for spec in self._specs:
            if not spec.get("id"):
                spec["id"] = blur_decisions.next_spec_id(current)
            # 全面ぼかしは一番下、囲みは一番上へ積む
            to_bottom = self._to_bottom or spec.get("kind") == blur_decisions.KIND_FRAME
            current = blur_decisions.with_spec(current, spec, to_bottom)
        blur_decisions.store(timeline, current)
        return True


# 指定を消す (1 件でも複数でも受ける)
class RemoveBlurSpecs(Command):

    label = "ぼかし指定の削除"

    def __init__(self, spec_ids):
        if isinstance(spec_ids, (list, tuple, set)):
            self._spec_ids = [str(i) for i in spec_ids]
        else:
            self._spec_ids = [str(spec_ids)]

    def apply(self, timeline):
        from ..blur import decisions as blur_decisions   # noqa: PLC0415

        current = blur_decisions.load(timeline)
        updated = current
        for spec_id in self._spec_ids:
            updated = blur_decisions.without_spec(updated, spec_id)
        if len(updated["specs"]) == len(current["specs"]):
            return False
        blur_decisions.store(timeline, updated)
        return True


# 指定の「ボカす / ボカさない」を変える
class SetBlurSpecMode(Command):

    label = "ぼかし指定の変更"

    def __init__(self, spec_id, mode):
        self._spec_id = str(spec_id)
        self._mode = mode

    def apply(self, timeline):
        from ..blur import decisions as blur_decisions   # noqa: PLC0415

        current = blur_decisions.load(timeline)
        updated = blur_decisions.with_spec_mode(current, self._spec_id, self._mode)
        if updated["specs"] == current["specs"]:
            return False
        blur_decisions.store(timeline, updated)
        return True


# 指定の重ね順を 1 つ動かす (delta: +1 = 上へ / -1 = 下へ)
class MoveBlurSpec(Command):

    label = "ぼかし指定の並べ替え"

    def __init__(self, spec_id, delta):
        self._spec_id = str(spec_id)
        self._delta = int(delta)

    def apply(self, timeline):
        from ..blur import decisions as blur_decisions   # noqa: PLC0415

        current = blur_decisions.load(timeline)
        updated = blur_decisions.with_spec_moved(current, self._spec_id, self._delta)
        if updated["specs"] == current["specs"]:
            return False
        blur_decisions.store(timeline, updated)
        return True


# 囲みの追従方法を変える (追えなかった囲みを「動かさない」にする)
class SetBlurSpecFollow(Command):

    label = "ぼかし枠を固定にする"

    def __init__(self, spec_id, follow):
        self._spec_id = str(spec_id)
        self._follow = str(follow)

    def apply(self, timeline):
        from ..blur import decisions as blur_decisions   # noqa: PLC0415

        current = blur_decisions.load(timeline)
        updated = blur_decisions.with_spec_follow(current, self._spec_id, self._follow)
        if updated["specs"] == current["specs"]:
            return False
        blur_decisions.store(timeline, updated)
        return True


# 指定に名前を付ける / 名前を変える。
# 名前は表示だけに効き、ぼかす / ぼかさないの判定は変えない。
class RenameBlurSpec(Command):

    label = "ぼかし指定の名前の変更"

    # label が空文字なら名前を消す (「ボカす N」へ戻る)
    def __init__(self, spec_id, label):
        self._spec_id = str(spec_id)
        self._label = label

    def apply(self, timeline):
        from ..blur import decisions as blur_decisions   # noqa: PLC0415

        current = blur_decisions.load(timeline)
        updated = blur_decisions.with_spec_label(current, self._spec_id, self._label)
        if updated["specs"] == current["specs"]:
            return False
        blur_decisions.store(timeline, updated)
        return True


# キーフレームを置く (ver5 resolve8 §5.10.5)
#
# **移動・拡大縮小・キーフレームの追加を兼ねる。**1 回のドラッグで 1 件だけ積むため、
# Ctrl+Z 1 回で掴む前の位置へ戻る。
class SetBlurKey(Command):

    label = "ぼかし枠の移動"

    #   t       : キーフレームの時刻 (素材の秒。コマ境界に乗っていること)
    #   rect    : 正規化キャンバス座標の (x, y, w, h)
    #   epsilon : 同じキーフレームとみなす時刻の差 (半コマ)
    def __init__(self, spec_id, t, rect, epsilon=None):
        self._spec_id = str(spec_id)
        self._t = float(t)
        self._rect = tuple(float(v) for v in rect)
        self._epsilon = epsilon

    def apply(self, timeline):
        from ..blur import decisions as blur_decisions   # noqa: PLC0415

        current = blur_decisions.load(timeline)
        epsilon = (blur_decisions.DEFAULT_KEY_EPSILON if self._epsilon is None
                   else float(self._epsilon))
        updated = blur_decisions.with_key(current, self._spec_id, self._t, self._rect, epsilon)
        if updated["specs"] == current["specs"]:
            return False
        blur_decisions.store(timeline, updated)
        return True


# キーフレームを消す (最後の 1 件は消さない)
class RemoveBlurKey(Command):

    label = "キーフレームの削除"

    def __init__(self, spec_id, t, epsilon=None):
        self._spec_id = str(spec_id)
        self._t = float(t)
        self._epsilon = epsilon

    def apply(self, timeline):
        from ..blur import decisions as blur_decisions   # noqa: PLC0415

        current = blur_decisions.load(timeline)
        epsilon = (blur_decisions.DEFAULT_KEY_EPSILON if self._epsilon is None
                   else float(self._epsilon))
        updated = blur_decisions.without_key(current, self._spec_id, self._t, epsilon)
        if updated["specs"] == current["specs"]:
            return False
        blur_decisions.store(timeline, updated)
        return True


# 追従結果の在りかを指定へ書き留める (追い終わった直後に 1 回だけ)
# これが無いと、書き出しのときに追従結果を見つけられない (mask_builder._cache_path)。
class SetBlurCache(Command):

    label = "ぼかし追従結果の反映"

    def __init__(self, cache_path, project_path=None):
        self._cache_path = str(cache_path or "")
        self._project_path = project_path

    def apply(self, timeline):
        from ..blur import decisions as blur_decisions   # noqa: PLC0415

        current = blur_decisions.load(timeline)
        relative = self._relative_cache()
        if (current.get("cache_abs") == self._cache_path
                and current.get("cache") == relative):
            return False
        current["cache"] = relative
        # 絶対パスも持つ。アーカイブ用のサブ Timeline は project_path を持たず、
        # 相対パスの起点が無いため、これが無いと追従結果を見つけられない。
        current["cache_abs"] = self._cache_path
        blur_decisions.store(timeline, current)
        return True

    # プロジェクトからの相対パス (プロジェクトごと移しても効く)
    def _relative_cache(self):
        if not self._cache_path or not self._project_path:
            return ""
        try:
            return os.path.relpath(self._cache_path, os.path.dirname(self._project_path))
        except ValueError:
            return ""        # 別ドライブなど相対にできない場合は絶対パスだけで運用する
