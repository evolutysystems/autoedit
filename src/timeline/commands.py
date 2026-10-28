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

from ..i18n import tr
from ..utils.logger import get_logger
from . import clipboard
from . import compositor
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
    # ver9 resolve10: 役割変更の自動移動をどこまで効かせるか。
    #   "role_tracks" (既定) … 役割が面倒を見るトラック (S1 / コメント用 S2) に
    #                          居るテロップだけ移す。利用者が増やしたトラック
    #                          (S3 以降) に置いたテロップは動かさない
    #   "all"                … どのトラックに居ても移す (v1.6.1 までの挙動)
    "role_move_scope": "role_tracks",
    # ver9 resolve2: テロップノードの右クリックから足す通常テロップ用トラック
    "new_track_enabled": True,
    "new_track_id_prefix": "S",
    "new_track_name_format": "Subtitle {index}",
    # 2 トラック目はコメント専用のため 3 から払い出す (§R4)
    "new_track_min_index": 3,
    "max_tracks": 8,
    "new_track_moves_selection": True,
    # ver9 要望: ドラッグ＆ドロップでテロップトラックを行き来できるか
    "drag_between_tracks": True,
    # ver9 要望: 増やしたトラックをヘッダの右クリックから削除できるか
    "remove_track_enabled": True,
}


# 役割に対応する字幕トラックを返す。無ければ作る (ver3 resolve11 §5.2-1)
# cfg: timeline_config()["subtitle_tracks"]。None なら既定 (コメントは S2) で動く。
def ensure_subtitle_track(timeline, role, cfg=None):
    cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(cfg or {})}
    # 番号も渡して拾い直しをコメント用の行に限る (ver9 resolve10 §3.4)。
    # 渡さないと、通常テロップ用に増やしたトラックの中身が全部コメントに
    # なった時点でそのトラックがコメント用として拾われてしまう。
    track = timeline.subtitle_track_for_role(
        role, cfg["comment_track_id"], cfg["role_track_enabled"],
        comment_track_index=_positive_int(cfg["comment_track_index"], 2))
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


# 設定値を正の int へ寄せる (不正値・未設定は既定へ)
def _positive_int(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


# 通常テロップ用の字幕トラックを 1 本作って返す (ver9 resolve2 §R4)
# コメント用トラックとは独立で、役割の印は何も持たせない
# (見た目を決めるのは clip.role だけのため、アイコンも背景も付かない)。
# コメント用の番号 (既定 index 2 / S2) は予約として飛ばす。
def create_subtitle_track(timeline, cfg=None):
    cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(cfg or {})}
    track_id, index = timeline.next_subtitle_track_id(
        prefix=cfg["new_track_id_prefix"],
        min_index=cfg["new_track_min_index"],
        reserved_indexes=(cfg["comment_track_index"],),
        reserved_ids=(cfg["comment_track_id"],),
    )
    try:
        name = str(cfg["new_track_name_format"]).format(index=index)
    except (KeyError, IndexError, ValueError):
        # 書式が壊れていても画面は止めない (ID をそのまま名前にする)
        _logger.warning("字幕トラック名の書式が不正です: %r",
                        cfg["new_track_name_format"])
        name = track_id
    track = Track(track_id, TRACK_SUBTITLE, index, name=name)
    timeline.tracks.append(track)
    _logger.info("テロップトラックを追加しました: %s (%s)", track_id, name)
    return track


# 削除してよいテロップトラックか (ver9 要望)
# 残すもの:
#   ・既定トラック (index 最小 / 通常 S1) … テロップの置き場が無くなるのを防ぐ
#   ・コメント用トラック (既定 S2)        … 役割変更の移動先が無くなるのを防ぐ
# つまり右クリックから消せるのは、増やしたトラック (S3 以降) だけになる。
def is_removable_subtitle_track(timeline, track, cfg=None):
    cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(cfg or {})}
    if track is None or not track.is_subtitle():
        return False
    base = timeline.base_subtitle_track()
    if base is not None and track.id == base.id:
        return False
    if cfg["role_track_enabled"] and (
            track.id == cfg["comment_track_id"]
            or track.index == _positive_int(cfg["comment_track_index"], 2)):
        return False
    return True


# 役割変更の自動移動が面倒を見るテロップトラックか (ver9 resolve10 §3.2)
#
# 見るのは 2 本だけ:
#   ・既定トラック (index 最小 / 通常 S1) … コメントを解除したときの戻り先
#   ・コメント用トラック (既定 S2)        … コメント化したときの移動先
# 利用者が増やしたトラック (S3 以降) は「同時刻に並べたいから自分で置いた」
# 場所であり、役割を変えても動かさない (置き場所の指定を役割で上書きしない)。
def is_role_managed_subtitle_track(timeline, track, cfg=None):
    cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(cfg or {})}
    if track is None or not track.is_subtitle():
        return False
    base = timeline.base_subtitle_track()
    if base is not None and track.id == base.id:
        return True
    return (track.id == cfg["comment_track_id"]
            or track.index == _positive_int(cfg["comment_track_index"], 2))


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
def split_clip_at(timeline, track, clip, at_sec, min_clip_sec=0.05, split_text=False):
    offset = at_sec - clip.timeline_start
    if offset < min_clip_sec or (clip.duration - offset) < min_clip_sec:
        return None

    if isinstance(clip, SubtitleClip):
        # 本文の扱いは split_text で決める (ver9 要望 / docs/quality/resolve1.md §3.4 G4)。
        #   split_text=False (既定): 本文はそのまま複製する。「こんにちは」を割ると
        #       「こんにちは」「こんにちは」の 2 本になる。同じ文を 2 回出したい、
        #       表示時間だけ割りたいという使い方がこちらのため、こちらを既定にする。
        #   split_text=True: 本文も再生ヘッド位置で分ける (G4 の挙動)。
        #       「こんにちは」が「こんに」「ちは」に分かれる。設定で戻せるよう温存する。
        # 本文の分割は「尺を変える前」に決める。単語列が無いクリップは尺の比率で
        # 按分するため、先に duration を縮めると比率が常に 1.0 になり本文が分かれない。
        head_text, tail_text = (split_subtitle_text(clip, at_sec) if split_text
                                else (None, None))
        new_clip = clip.copy()
        new_clip.id = timeline.next_id("s")
        new_clip.timeline_start = at_sec
        new_clip.duration = clip.duration - offset
        clip.duration = offset
        if head_text is not None:
            clip.text = head_text
            new_clip.text = tail_text
            # 分けた側の単語列も分ける (次の分割でも語の境界で切れるように)
            _split_origin_words(clip, new_clip, at_sec)
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


# 字幕の本文を再生ヘッド位置で 2 つへ分ける (G4 / docs/quality/resolve1.md §3.4 G4)
#
# origin["words"] に単語の時刻があれば、その境界で分ける (= 発話どおりに分かれる)。
# 単語列が無い (手入力したテロップ・古いプロジェクト) ときは文字数で按分する。
# 分けられない (本文が空・片側が空になる) ときは (None, None) を返し、
# 呼び出し側は従来どおり本文を複製する。
# 戻り値: (前半の本文, 後半の本文) / 分けられなければ (None, None)
def split_subtitle_text(clip, at_sec):
    body = str(getattr(clip, "text", "") or "")
    plain = body.replace("\\N", "")
    if not plain:
        return None, None

    words = (getattr(clip, "origin", None) or {}).get("words")
    cut = None
    if words:
        cut = _char_index_from_words(words, clip, at_sec, plain)
    if cut is None:
        # 単語列が無い: 尺の比率で文字数を按分する
        ratio = (float(at_sec) - float(clip.timeline_start)) / float(clip.duration or 1.0)
        cut = int(round(min(max(ratio, 0.0), 1.0) * len(plain)))
    if cut <= 0 or cut >= len(plain):
        return None, None
    return plain[:cut].strip(), plain[cut:].strip()


# 単語の時刻から「本文の何文字目で切るか」を求める (G4)
# 単語列と本文は同じ語から作られているため、文字数の累積で突き合わせられる。
# 本文が手で書き換えられている場合は長さが合わないので None を返す (按分へ落ちる)。
def _char_index_from_words(words, clip, at_sec, plain):
    joined = "".join(str(w.get("text", "")) for w in words).strip()
    if len(joined) != len(plain):
        return None
    # origin["words"] は素材時間。clip 側は Timeline 時間なので、
    # 「先頭語からの相対位置」で突き合わせる (どちらも同じ発話の並びのため)。
    first = float(words[0]["start"])
    offset = float(at_sec) - float(clip.timeline_start)
    target = first + offset
    cursor = 0
    for word in words:
        if float(word["start"]) >= target:
            return cursor
        cursor += len(str(word.get("text", "")))
    return None


# 分割した 2 つのクリップへ単語列を振り分ける (G4)
def _split_origin_words(head, tail, at_sec):
    words = (getattr(head, "origin", None) or {}).get("words")
    if not words:
        return
    first = float(words[0]["start"])
    target = first + (float(at_sec) - float(head.timeline_start))
    head_words = [w for w in words if float(w["start"]) < target]
    tail_words = [w for w in words if float(w["start"]) >= target]
    head.origin = {**head.origin, "words": head_words}
    tail.origin = {**tail.origin, "words": tail_words}


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

    def __init__(self, clip_id, at_sec, min_clip_sec=0.05, split_text=False):
        self._clip_id = clip_id
        self._at_sec = float(at_sec)
        self._min_clip_sec = float(min_clip_sec)
        # 字幕を割ったときに本文も分けるか (既定 False = 複製 / ver9 要望)。
        # 「ここで分割」からも字幕を割れるため、テロップ用の分割と規約を合わせる。
        self._split_text = bool(split_text)

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        track = timeline.track_of_clip(self._clip_id)
        if clip is None or track is None or track.locked:
            return False
        # 分割の規約は split_clip_at() が持つ (ver3 resolve10 §5.2)。
        # 両側が最小尺を満たさない位置では割れず None が返る。
        return split_clip_at(timeline, track, clip, self._at_sec,
                             self._min_clip_sec,
                             split_text=self._split_text) is not None


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


# ------------------------------------------------------------------
# V1 (ベース映像) の拡大率と表示位置 (ver9 resolve6)
# ------------------------------------------------------------------
#
# オーバーレイの ResizeOverlay / MoveOverlay とは**基準と可動範囲が違う**ため、
# 相乗りさせず別のコマンドにする (resolve6 §3.5)。
#   オーバーレイ : scale = キャンバス幅に対する比率 / 位置は画面のどこにでも置ける
#   V1 (ここ)    : scale = キャンバスへ収めた状態に対する倍率 (100% = 今の見た目)
#                  位置は**黒帯が出ない範囲**に限る


# V1 クリップか (ベース映像トラックに載る映像クリップか)
def is_base_video_clip(timeline, clip):
    if clip is None or isinstance(clip, (SubtitleClip, AudioClip)):
        return False
    if not hasattr(clip, "transform"):
        return False
    base = timeline.base_video_track()
    track = timeline.track_of_clip(clip.id)
    return base is not None and track is not None and track.id == base.id


# 表示位置を可動範囲へ丸める (resolve6 §3.4)
#
# 拡大しているときは「はみ出しているぶん」までしか動かせない。それを超えると
# 画面の端に黒帯が出る。縮小しているときは「余白のぶん」までしか動かせない。
# 拡大率を変えたときも位置を変えたときも、必ずここを通して丸め直す。
#
# 素材の寸法が分からないときは丸めない (倍率の基準が決まらないため)。
# 戻り値: (x, y) 正規化座標
def clamp_base_offset(timeline, media, zoom, x, y):
    fit_w, fit_h = compositor.fit_size_px(
        getattr(media, "width", 0), getattr(media, "height", 0),
        timeline.width, timeline.height)
    if fit_w <= 0 or fit_h <= 0:
        return float(x or 0.0), float(y or 0.0)
    zoom = float(zoom or 1.0)
    # 正規化座標の限界 = はみ出し (または余白) の半分 ÷ キャンバスの半分
    limit_x = abs(fit_w * zoom - timeline.width) / float(timeline.width)
    limit_y = abs(fit_h * zoom - timeline.height) / float(timeline.height)
    return (min(max(float(x or 0.0), -limit_x), limit_x),
            min(max(float(y or 0.0), -limit_y), limit_y))


# V1 クリップの拡大率を変える (resolve6 §3.5)
# zoom: キャンバスへ収めた状態に対する倍率 (1.0 = 等倍 = 今の見た目)
class ZoomBaseClip(Command):

    label = "拡大率の変更"

    def __init__(self, clip_id, zoom, min_zoom=0.1, max_zoom=3.0):
        self._clip_id = clip_id
        self._zoom = float(zoom)
        self._min = float(min_zoom)
        self._max = float(max_zoom)

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if not is_base_video_clip(timeline, clip):
            return False
        value = min(max(self._zoom, self._min), self._max)
        media = timeline.media_by_id(clip.media_id)
        # 拡大率が変われば可動範囲も変わる。位置を新しい範囲へ丸め直す
        # (縮めたときに前の位置が残って黒帯が出るのを防ぐ)。
        new_x, new_y = clamp_base_offset(
            timeline, media, value, clip.transform.x, clip.transform.y)
        same_zoom = abs(float(clip.transform.scale or 1.0) - value) <= _EPS
        same_pos = (abs(float(clip.transform.x or 0.0) - new_x) <= _EPS
                    and abs(float(clip.transform.y or 0.0) - new_y) <= _EPS)
        if same_zoom and same_pos:
            return False        # 変化なし → 履歴を汚さない
        clip.transform.scale = value
        clip.transform.x = new_x
        clip.transform.y = new_y
        return True


# V1 クリップの表示位置を変える (resolve6 §3.5)
# 等倍 (100%) のときは動かせない。動かす余地が無く、誤操作で履歴を汚さないため。
class MoveBaseClip(Command):

    label = "表示位置の変更"

    def __init__(self, clip_id, x, y):
        self._clip_id = clip_id
        self._x = x
        self._y = y

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if not is_base_video_clip(timeline, clip):
            return False
        if compositor.is_identity_zoom(clip):
            return False        # 等倍は動かせない (はみ出しも余白も無い)
        zoom = float(clip.transform.scale or 1.0)
        media = timeline.media_by_id(clip.media_id)
        new_x, new_y = clamp_base_offset(timeline, media, zoom, self._x, self._y)
        if (abs(float(clip.transform.x or 0.0) - new_x) <= _EPS
                and abs(float(clip.transform.y or 0.0) - new_y) <= _EPS):
            return False
        clip.transform.x = new_x
        clip.transform.y = new_y
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
#
# ver9 resolve10: 自動移動は「S1 ↔ コメント用 S2」の往復に限る。
#   以前は行き先を role だけで決めていたため、利用者が増やしたトラック (S3 以降) へ
#   移したテロップも、役割を触った瞬間に S1 へ引き戻されていた (resolve10 §2.1)。
#   置き場所は「最後にそこへ置いた人の意思」として尊重する。
class ChangeSubtitleRole(Command):

    label = "字幕の役割変更"

    def __init__(self, clip_id, role, subtitle_tracks_cfg=None):
        self._clip_id = clip_id
        self._role = str(role or DEFAULT_ROLE)
        self._cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(subtitle_tracks_cfg or {})}
        # 呼び出し側の案内文言用 (移動できたか / 移動先が埋まっていたか)
        self.moved = False
        self.move_blocked = False
        # 置き場所を尊重して移動しなかったか (ver9 resolve10 §3.3)
        self.move_skipped = False

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if not isinstance(clip, SubtitleClip):
            return False
        # 役割が変わらないなら何もしない (ver9 resolve10 §3.3)。
        # 以前は「役割は同じだが移動はする」経路があり、コンボで同じ役割を
        # 選び直しただけでテロップが S1 へ動いていた。
        if clip.role == self._role:
            return False

        source = timeline.track_of_clip(clip.id)
        # 行き先は clip.role を書き換える前に決める (ver9 resolve10 §3.3)。
        # 書き換えてから探すと、増やしたトラックに載っている 1 本をコメント化
        # したときに「中身が全部コメント」の条件へ自分が引っかかり、
        # そのトラックがコメント用と誤認される。
        target = self._move_target(timeline, clip, source)
        clip.role = self._role
        if source is None or target is None or target.id == source.id:
            return True
        if target.locked or _overlaps_any(
                target, clip.timeline_start, clip.timeline_end, exclude_id=clip.id):
            self.move_blocked = True
            _logger.info("移動先 %s が空いていないため役割だけ変更しました: %s",
                         target.id, clip.id)
            return True

        source.remove_clip(clip.id)
        target.clips.append(clip)
        target.sort_clips(timeline)
        self.moved = True
        return True

    # 役割変更にともなう移動先を返す (移動しないときは None)
    # 【重要】clip.role を書き換える前に呼ぶ (ver9 resolve10 §3.3)。
    def _move_target(self, timeline, clip, source):
        cfg = self._cfg
        if source is None:
            return None
        if not (cfg["role_track_enabled"] and cfg["auto_move_on_role_change"]):
            return None
        # 利用者が増やしたトラックに置かれたテロップは動かさない (§3.2)
        if (str(cfg["role_move_scope"]) == "role_tracks"
                and not is_role_managed_subtitle_track(timeline, source, cfg)):
            self.move_skipped = True
            _logger.info("%s は利用者が置いたトラックのため役割だけ変更します: %s",
                         source.id, clip.id)
            return None
        return ensure_subtitle_track(timeline, self._role, cfg)


# 字幕を新しいテロップトラックへ移す (ver9 resolve2 §R2)
# 役割 (role) は変えない = 色・配置はそのままで、アイコンも背景も付かない。
# 「同じ時刻に並べられる本数」を増やすだけの操作であり、出力の見た目は変わらない
# (焼き込みもプレビューも subtitle_clips() で全字幕を集めるため)。
class MoveSubtitlesToNewTrack(Command):

    def __init__(self, clip_ids, subtitle_tracks_cfg=None):
        self._clip_ids = [i for i in (clip_ids or []) if i]
        self._cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(subtitle_tracks_cfg or {})}
        # 呼び出し側の案内文言用 (移せた件数 / 残した件数 / 上限で止まったか)
        self.created_track_id = None
        self.moved = 0
        self.skipped = 0
        self.limit_reached = False
        # 件数を履歴ボタンへ出す (静的な label と違いここで組み立てる)
        self.label = (tr("新規テロップトラックへ移動") if len(self._clip_ids) <= 1
                      else tr("新規テロップトラックへ移動（{count} 件）",
                              count=len(self._clip_ids)))

    def apply(self, timeline):
        # 【重要】CommandStack.push は apply が False を返しても状態を戻さない
        # (例外時だけ _restore する)。そのため「移せるか」を全部決めてから
        # トラックを作る。空のトラックだけが残る状態を作らない。
        targets = self._movable(timeline)
        if not targets:
            return False
        if len(timeline.subtitle_tracks()) >= _positive_int(self._cfg["max_tracks"], 8):
            self.limit_reached = True
            _logger.info("字幕トラックの上限 (%s) に達しています",
                         self._cfg["max_tracks"])
            return False

        track = create_subtitle_track(timeline, self._cfg)
        self.created_track_id = track.id
        for clip in targets:
            source = timeline.track_of_clip(clip.id)
            if source is None:
                continue
            source.remove_clip(clip.id)
            track.clips.append(clip)
            self.moved += 1
        track.sort_clips(timeline)
        return self.moved > 0

    # 1 本のトラックへ入れられる字幕を開始時刻順に選ぶ。
    # 互いに重なるものは同じトラックへ入れられないため、早いもの勝ちで落とす (§R3)。
    def _movable(self, timeline):
        clips = []
        for clip_id in self._clip_ids:
            clip = timeline.clip_by_id(clip_id)
            if not isinstance(clip, SubtitleClip):
                continue                  # 映像・音声の混在選択は黙って読み飛ばす
            source = timeline.track_of_clip(clip.id)
            if source is None or source.locked:
                self.skipped += 1
                continue
            clips.append(clip)
        chosen, cursor = [], None
        for clip in sorted(clips, key=lambda c: c.timeline_start):
            if cursor is not None and clip.timeline_start < cursor - _EPS:
                self.skipped += 1
                continue
            chosen.append(clip)
            cursor = clip.timeline_end
        return chosen


# テロップを別のテロップトラックへ移す (ver9 要望 / ドラッグ＆ドロップの受け皿)
#
# 「新規テロップトラックへ移動」はトラックを増やす操作しか持たなかったため、
# 2 行で足りる場面でもトラックが増え続けていた。これは**既にあるトラックへ**移す操作。
# 時刻も同時に変わる (斜めにドラッグできる) ため new_start も受ける。
# 移動先で時間が重なるときは近い側の空きへ寄せる (トラック内の移動と同じ規約)。
# 役割 (role) は変えない = 見た目は一切変わらない (ver9 resolve2 §1.1)。
class MoveSubtitleToTrack(Command):

    def __init__(self, clip_id, track_id, new_start=None, min_clip_sec=0.05):
        self._clip_id = clip_id
        self._track_id = track_id
        self._new_start = None if new_start is None else float(new_start)
        self._min_clip_sec = float(min_clip_sec)
        # 呼び出し側の案内文言用 (実際に落ち着いた位置)
        self.moved_start = None
        self.label = tr("テロップのトラック移動")

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        source = timeline.track_of_clip(self._clip_id)
        target = timeline.track_by_id(self._track_id)
        if not isinstance(clip, SubtitleClip) or source is None or target is None:
            return False
        if not target.is_subtitle() or source.locked or target.locked:
            return False
        if target.id == source.id:
            return False
        desired = clip.timeline_start if self._new_start is None else self._new_start
        start = _clamp_position(target, clip, desired, self._min_clip_sec)
        source.remove_clip(clip.id)
        clip.timeline_start = start
        target.clips.append(clip)
        target.sort_clips(timeline)
        self.moved_start = start
        _logger.info("テロップを %s から %s へ移しました: %s",
                     source.id, target.id, clip.id)
        return True


# テロップトラックを 1 本削除する (ver9 要望 / ヘッダの右クリック)
#
# 載っているテロップも一緒に消える。消えることの確認は呼び出し側 (UI) で取る。
# Undo はスナップショット方式なので、間違えても Ctrl+Z で丸ごと戻る。
# 既定トラックとコメント用トラックは消さない (is_removable_subtitle_track)。
class RemoveSubtitleTrack(Command):

    def __init__(self, track_id, subtitle_tracks_cfg=None):
        self._track_id = track_id
        self._cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(subtitle_tracks_cfg or {})}
        # 呼び出し側の案内文言用 (一緒に消したテロップの件数)
        self.removed_clips = 0
        self.label = tr("テロップトラックの削除")

    def apply(self, timeline):
        track = timeline.track_by_id(self._track_id)
        if not is_removable_subtitle_track(timeline, track, self._cfg):
            return False
        self.removed_clips = len(track.clips)
        timeline.tracks = [t for t in timeline.tracks if t.id != track.id]
        _logger.info("テロップトラックを削除しました: %s (テロップ %d 件)",
                     track.id, self.removed_clips)
        return True


# 複数の字幕クリップをまとめて変更する (ver3 resolve6 §3-10 / §5.10)
# Undo はスナップショット方式のため、N 件の変更がまとめて 1 手で戻る。
# 選択には映像・音声クリップも混ざり得るので、字幕以外の ID は黙って読み飛ばす。
class EditSubtitles(Command):

    def __init__(self, clip_ids, **fields):
        self._clip_ids = [i for i in (clip_ids or []) if i]
        self._fields = fields
        # 何件に効く操作なのかを履歴ボタンへ出す (「元に戻す: 字幕の編集（3 件）」)
        # 件数が入るため、ここで現在の言語に組み立てる
        # (静的な label は表示するときに tr() を通す)
        self.label = (tr("字幕の編集") if len(self._clip_ids) <= 1
                      else tr("字幕の編集（{count} 件）",
                              count=len(self._clip_ids)))

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


# 選択した字幕を 1 本へ結合する (G4 / docs/quality/resolve1.md §3.4 G4)
#
# 従来は結合コマンドが無く、2 本を 1 本にするには
#   片方を削除 → 残りを伸ばす → 本文を打ち直す
# という手順を踏む必要があった。要望の「テロップを結合する作業」はここが主因。
#
# 規約:
#   ・時刻 = 最初の timeline_start 〜 最後の timeline_end
#   ・本文 = 時間順に連結し、改行は呼び出し側の整形関数 (wrap) で引き直す
#   ・役割・色・フォントは先頭クリップのものを採用する
#   ・origin は先頭のものを引き継ぎ、merged_from に元 ID を残す
# wrap: 本文の整形関数 (省略時は連結のみ)。
class MergeSubtitles(Command):

    def __init__(self, clip_ids, wrap=None):
        self._clip_ids = [i for i in (clip_ids or []) if i]
        self._wrap = wrap
        self.label = tr("テロップの結合")

    def apply(self, timeline):
        clips = []
        for clip_id in self._clip_ids:
            clip = timeline.clip_by_id(clip_id)
            if isinstance(clip, SubtitleClip):
                clips.append(clip)
        if len(clips) < 2:
            return False
        # 同じトラックのものだけを結合する (役割をまたぐ結合は意味が定まらない)
        track = timeline.track_of_clip(clips[0].id)
        if track is None or track.locked:
            return False
        clips = [c for c in clips if timeline.track_of_clip(c.id) is track]
        if len(clips) < 2:
            return False

        clips.sort(key=lambda c: c.timeline_start)
        head, rest = clips[0], clips[1:]
        start = clips[0].timeline_start
        end = max(c.timeline_end for c in clips)
        body = "".join(str(c.text or "").replace("\\N", "") for c in clips)

        head.timeline_start = start
        head.duration = end - start
        head.text = self._wrap(body) if self._wrap else body
        # 単語列も時間順に連ねる (以降の分割でも語の境界で切れるように)
        words = []
        for clip in clips:
            words.extend((getattr(clip, "origin", None) or {}).get("words") or [])
        origin = {**(head.origin or {}), "merged_from": [c.id for c in clips]}
        if words:
            origin["words"] = sorted(words, key=lambda w: float(w["start"]))
        head.origin = origin

        for clip in rest:
            track.remove_clip(clip.id)
        return True


# 字幕を再生ヘッド位置で 2 本へ分ける (G4)
# 本文も分けるため、分割後に両方の本文を手で削る必要がない。
class SplitSubtitle(Command):

    def __init__(self, clip_id, at_sec, min_clip_sec=0.05, split_text=False):
        self._clip_id = clip_id
        self._at_sec = float(at_sec)
        self._min_clip_sec = float(min_clip_sec)
        # 本文も分けるか (既定 False = 両方へ同じ本文を複製する / ver9 要望)
        self._split_text = bool(split_text)
        self.label = tr("テロップの分割")

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        track = timeline.track_of_clip(self._clip_id)
        if not isinstance(clip, SubtitleClip) or track is None or track.locked:
            return False
        return split_clip_at(timeline, track, clip, self._at_sec,
                             self._min_clip_sec,
                             split_text=self._split_text) is not None


# 時刻を含む区間の番号を返す (区間の外なら None / ver8 resolve6)
def _cut_index_at(bounds, sec):
    value = float(sec)
    for index, (head, tail) in enumerate(bounds or []):
        if head - _EPS <= value < tail - _EPS:
            return index
    return None


# 字幕の開始を V1 クリップ (カット) の頭へ揃える (ver8 resolve6 R5)
#
# 生成時の整列 (subtitle_generator.align_items_to_cut_heads) は作り直さないと
# 効かないため、既存プロジェクトと手編集後のための救済としてこのコマンドを置く。
# 揃え先は keep_segments ではなく V1 のクリップにする。手編集で分割・削除した
# 後は V1 のクリップこそが「いまのカット」であり、keep_segments は古いため。
#
# 1 カット内のテロップの本数は変えない (resolve6 §3-3)。寄せるのはそのカットで
# 最初に出る 1 本だけで、2 本目以降は位置をそのまま残す。
#
# clamp_to_cut_end の既定は False (resolve6 §12-5)。テロップの単位を文字数へ
# 移した第3部では字幕がカットを跨ぐのが正常形であり、手編集後のクリップには
# 「発話がどこで終わったか」の情報が無いため、終端をカット境界で切ると
# 意図した跨ぎを壊してしまう。True は subtitle_unit = "cut" 用の退路。
class AlignSubtitlesToCutHeads(Command):

    label = "字幕をカット頭へ揃える"

    def __init__(self, clip_ids=None, tolerance_sec=0.6,
                 clamp_to_cut_end=False, min_clip_sec=0.05):
        # None = 全字幕が対象。空リストを渡されたときも全件にはしない
        self._clip_ids = None if clip_ids is None else {i for i in clip_ids if i}
        self._tolerance_sec = float(tolerance_sec)
        self._clamp_to_cut_end = bool(clamp_to_cut_end)
        self._min_clip_sec = float(min_clip_sec)
        # 揃えた件数 (UI の状態表示用。apply 後に読む)
        self.aligned = 0

    def apply(self, timeline):
        self.aligned = 0
        if self._tolerance_sec <= 0:
            return False
        # OP/ED は揃え先にしない (素材の頭へ字幕を吸わせない)
        bounds = [
            (c.timeline_start, c.timeline_end)
            for c in timeline.base_clips() if not c.is_opening_or_ending()
        ]
        if not bounds:
            return False

        changed = False
        for track in timeline.subtitle_tracks():
            if track.locked:
                continue
            prev_end = None
            taken = set()
            for clip in sorted(track.clips, key=lambda c: c.timeline_start):
                if self._align_one(clip, bounds, taken, prev_end):
                    changed = True
                    self.aligned += 1
                prev_end = clip.timeline_end
            track.sort_clips()

        if changed:
            _logger.info("字幕をカット頭へ揃えました: %d 件 (許容差 %.2fs)",
                         self.aligned, self._tolerance_sec)
        return changed

    # 字幕 1 件を揃える。変化があれば True。
    def _align_one(self, clip, bounds, taken, prev_end):
        if self._clip_ids is not None and clip.id not in self._clip_ids:
            return False
        index = _cut_index_at(bounds, clip.timeline_start)
        if index is None:
            return False
        head, tail = bounds[index]
        # そのカットの先頭字幕だけが対象 (2 本目以降は動かさない)
        first_in_cut = index not in taken
        taken.add(index)

        start = clip.timeline_start
        end = clip.timeline_end
        gap = start - head
        if first_in_cut and _EPS < gap <= self._tolerance_sec:
            # 直前の字幕へ食い込むなら動かさない (重なりを作らない)
            if prev_end is None or head >= prev_end - _EPS:
                start = head
        if self._clamp_to_cut_end:
            end = min(end, tail)
        if end - start < self._min_clip_sec - _EPS:
            return False
        if abs(start - clip.timeline_start) <= _EPS and abs(end - clip.timeline_end) <= _EPS:
            return False
        clip.timeline_start = start
        clip.duration = end - start
        return True


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
