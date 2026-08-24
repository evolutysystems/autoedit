# Timeline ノードのクリップボード (docs/request/ver3/resolve10.md §3-1 / §3-2)
# コピーした内容はプロセス内で共有し、編集画面をまたいでも貼り付けられるようにする。
# モデルの参照ではなく辞書 (JSON 化できる形) で控える:
#   ・コピー後に元クリップを編集・削除・Undo しても貼り付け内容が変わらない
#   ・Qt に依存しないためテストから直接叩ける
#   ・将来 OS クリップボードへ載せ替える場合もこの payload をそのまま使える
from ..utils.logger import get_logger
from .model import AudioClip, SubtitleClip, round_sec

_logger = get_logger(__name__)

PAYLOAD_VERSION = 1
KIND_VIDEO = "video"
KIND_SUBTITLE = "subtitle"


# コピー対象を (トラック, クリップ) の一覧へ解決する
# 音声クリップはリンク元の映像クリップへ読み替える (§3-6)。重複は 1 件にまとめる。
def _resolve_targets(timeline, clip_ids):
    targets, seen = [], set()
    for clip_id in (clip_ids or []):
        clip = timeline.clip_by_id(clip_id)
        if isinstance(clip, AudioClip):
            clip = timeline.clip_by_id(clip.link_clip)
        if clip is None or clip.id in seen:
            continue
        track = timeline.track_of_clip(clip.id)
        if track is None:
            continue
        seen.add(clip.id)
        targets.append((track, clip))
    return targets


# クリップ 1 件を payload の項目へ変換する
def _build_item(timeline, track, clip, anchor):
    is_subtitle = isinstance(clip, SubtitleClip)
    item = {
        "kind": KIND_SUBTITLE if is_subtitle else KIND_VIDEO,
        "offset_sec": round_sec(clip.timeline_start - anchor),
        "track": {"id": track.id, "kind": track.kind,
                  "index": track.index, "is_base": bool(track.is_base)},
        "clip": clip.to_dict(),
        "audio": None,
        "media": None,
    }
    if not is_subtitle:
        media = timeline.media_by_id(clip.media_id)
        if media is not None:
            item["media"] = media.to_dict()          # 別 Timeline へ貼るときの再登録用
        linked = timeline.audio_clip_for(clip.id)
        if linked is not None:
            item["audio"] = {"gain_db": linked.gain_db, "muted": linked.muted}
    return item


# 選択中のクリップ群から payload を作る (§3-2)。作れなければ None。
# 位置は「最も早いノードの開始 = 0」とした相対値で持つため、貼り付け先の
# 再生ヘッドがどこでも、選択したときの並びがそのまま再現される。
def build_payload(timeline, clip_ids):
    targets = _resolve_targets(timeline, clip_ids)
    if not targets:
        return None
    anchor = min(clip.timeline_start for _track, clip in targets)
    span = max(clip.timeline_end for _track, clip in targets) - anchor
    items = [_build_item(timeline, track, clip, anchor)
             for track, clip in sorted(targets, key=lambda tc: tc[1].timeline_start)]
    return {"version": PAYLOAD_VERSION, "span_sec": round_sec(span), "items": items}


# プロセス内で共有する 1 つだけのクリップボード
class _Clipboard:

    def __init__(self):
        self._payload = None

    def set(self, payload):
        self._payload = payload

    def get(self):
        return self._payload

    def is_empty(self):
        return not (self._payload or {}).get("items")

    def clear(self):
        self._payload = None


_clipboard = _Clipboard()


# 選択中のノードをコピーする。控えた件数を返す (0 = コピーできるものが無い)
def copy_clips(timeline, clip_ids):
    payload = build_payload(timeline, clip_ids)
    if payload is None:
        return 0
    _clipboard.set(payload)
    _logger.info("Timeline のノードをコピーしました: %d 件", len(payload["items"]))
    return len(payload["items"])


# 控えてある payload (未コピーなら None)
def payload():
    return _clipboard.get()


def is_empty():
    return _clipboard.is_empty()


def clear():
    _clipboard.clear()
