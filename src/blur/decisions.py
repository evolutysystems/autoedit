# ぼかし指定の読み書きと、囲みパスの当て込み (ver5 resolve2 §5.2.2 / §5.6.3)
#
# 指定は timeline.source["blur"] に置く。ここに置くと
#   ・commands._snapshot が deepcopy するため Undo/Redo が自動で効く (§2.6)
#   ・project_io がそのまま書き出すため、開き直しても残る
# ただし **1KB 程度に収まる大きさしか置かない**。重い解析結果は store.py 側。
#
# 指定は「明示されたものだけ」を持つ。触っていない人物は default_policy に従い、
# 主役 (R3) は policy に関わらず常にぼかさない。
#
# ver5 resolve3 で次を足した (version 2)。
#   excluded   : 削除した検出枠 (アンカー = 素材・時刻・位置で覚える / §3.5)
#   splits     : 別の人物にした検出枠 (同上。as に新しい人物 ID)
#   region_seq : 領域 ID の通し番号 (削除しても戻さない / §5.7)
#   regions[].kind : "place" (場所 / カメラの動きを追う) | "object" (人物・物 / その物を追う)
import hashlib
import json
import re

from ..utils.logger import get_logger
from .config import POLICY_BLUR_OTHERS, POLICY_MANUAL_ONLY
from .geometry import iou, point_in_polygon, polygon_bounds, rect_coverage

_logger = get_logger(__name__)

# source["blur"] の書式版
VERSION = 2

# 人物・領域に対する指定値
BLUR = "blur"      # ぼかす
KEEP = "keep"      # ぼかさない

# 追加した枠の種類 (§3.6)
KIND_PLACE = "place"     # 場所 (建物・看板)。カメラの動きに合わせて追う
KIND_OBJECT = "object"   # 人物・物。囲んだ物そのものを追う

# 分割でできる人物 ID の頭文字 (既存の p1, p2, … を振り直さない / §4-3)
SPLIT_PREFIX = "s"

# アンカーと検出枠を同じものとみなす重なり (§3.5)
ANCHOR_MIN_IOU = 0.5

_REGION_NUMBER_RE = re.compile(r"^r(\d+)$")
_SPLIT_NUMBER_RE = re.compile(r"^s(\d+)$")


# timeline.source["blur"] を読む (無ければ空の指定を返す)
def load(timeline):
    section = (timeline.source or {}).get("blur")
    if not isinstance(section, dict):
        return _empty()
    return {
        "version": int(section.get("version", VERSION) or VERSION),
        "cache": str(section.get("cache", "") or ""),
        "cache_abs": str(section.get("cache_abs", "") or ""),
        "fingerprint": str(section.get("fingerprint", "") or ""),
        "default_policy": str(section.get("default_policy", "") or ""),
        "identities": {str(k): str(v) for k, v in (section.get("identities") or {}).items()
                       if str(v) in (BLUR, KEEP)},
        "merges": [list(pair) for pair in (section.get("merges") or []) if len(pair) >= 2],
        "regions": [dict(region) for region in (section.get("regions") or [])
                    if isinstance(region, dict)],
        "excluded": [anchor for anchor in (_anchor(item) for item in (section.get("excluded") or []))
                     if anchor is not None],
        "splits": [dict(anchor, **{"as": str(item.get("as"))})
                   for item, anchor in ((item, _anchor(item)) for item in (section.get("splits") or []))
                   if anchor is not None and item.get("as")],
        "region_seq": _to_int(section.get("region_seq")),
    }


# timeline.source["blur"] へ書き戻す (コマンドの apply から呼ぶ)
def store(timeline, decisions):
    source = timeline.source if isinstance(timeline.source, dict) else {}
    section = {
        "version": VERSION,
        "identities": dict(decisions.get("identities") or {}),
        "regions": [dict(region) for region in (decisions.get("regions") or [])],
    }
    # 空の値は書かない (プロジェクト JSON を無駄に太らせない)
    for key in ("cache", "cache_abs", "fingerprint", "default_policy"):
        value = decisions.get(key)
        if value:
            section[key] = value
    if decisions.get("merges"):
        section["merges"] = [list(pair) for pair in decisions["merges"]]
    if decisions.get("excluded"):
        section["excluded"] = [dict(item) for item in decisions["excluded"]]
    if decisions.get("splits"):
        section["splits"] = [dict(item) for item in decisions["splits"]]
    if decisions.get("region_seq"):
        section["region_seq"] = int(decisions["region_seq"])

    source["blur"] = section
    timeline.source = source
    return section


def _empty():
    return {
        "version": VERSION,
        "cache": "",
        "cache_abs": "",
        "fingerprint": "",
        "default_policy": "",
        "identities": {},
        "merges": [],
        "regions": [],
        "excluded": [],
        "splits": [],
        "region_seq": 0,
    }


# 明示した指定が 1 つでもあるか
def has_any(decisions):
    return bool((decisions or {}).get("identities") or (decisions or {}).get("regions"))


# マスクを作る必要があるか (ぼかす対象になり得るものが 1 つでもあるか)
#
# 明示した指定が無くても、既定方針が blur_others なら**解析済みの人物 (主役以外) はぼかす対象**になる
# (§9-1)。指定画面もその前提で「ぼかす」と表示するため、ここで見落とすと素で出力してしまう。
# 解析していない Timeline (指紋が無い = 旧プロジェクトなど) は、ぼかす人物がまだ存在しないため対象外とする。
def needs_mask(decisions, cfg):
    decisions = decisions or {}
    if has_any(decisions):
        return True
    if not decisions.get("fingerprint"):
        return False
    policy = decisions.get("default_policy") or cfg["default_policy"]
    return policy == POLICY_BLUR_OTHERS


# ------------------------------------------------------------------
# ぼかす / ぼかさない の判定 (§5.2.2)
# ------------------------------------------------------------------

# 人物 1 人をぼかすか決める。
#   identity_id : 人物 ID
#   decisions   : load() の戻り値
#   cfg         : config.config() の戻り値 (既定方針の出どころ)
#   main_id     : 主役の人物 ID (R3)
# 明示指定 > 主役は常にぼかさない > 既定方針、の順で決まる。
def should_blur_identity(identity_id, decisions, cfg, main_id=None):
    explicit = (decisions or {}).get("identities", {}).get(str(identity_id))
    if explicit == BLUR:
        return True
    if explicit == KEEP:
        return False

    # 一番映っている人物はぼかさない (R3)。明示的に「ぼかす」と言われた場合だけ上で覆る。
    if main_id is not None and str(identity_id) == str(main_id):
        return False

    policy = (decisions or {}).get("default_policy") or cfg["default_policy"]
    return policy == POLICY_BLUR_OTHERS


# 領域 (建物など) をぼかすか。領域は囲んだ時点で mode が決まっている。
def should_blur_region(region):
    return str((region or {}).get("mode", BLUR)) == BLUR


# 現在の方針を人が読める文言にする (画面の説明用)
def policy_label(decisions, cfg):
    policy = (decisions or {}).get("default_policy") or cfg["default_policy"]
    if policy == POLICY_MANUAL_ONLY:
        return "囲った対象だけをぼかします"
    return "主役以外は自動でぼかします"


# ------------------------------------------------------------------
# 囲みパスの当て込み (§5.6.3)
# ------------------------------------------------------------------

# 囲みの中に入っている人物を拾う。
#   polygon  : キャンバス座標の点列 (閉じている前提)
#   boxes    : [{"identity": 人物 ID, "rect": (x, y, w, h)}, …] キャンバス座標
#   hit_ratio: 枠と囲みの重なりがこの比率以上なら採用する
# 中心が入っていれば即採用。中心が外でも重なりが hit_ratio 以上なら採用する。
# 戻り値: 人物 ID の一覧 (登場順・重複なし)
def identities_in_path(polygon, boxes, hit_ratio=0.5):
    if len(polygon or []) < 3:
        return []

    bounds = polygon_bounds(polygon)
    found = []
    for entry in boxes or []:
        identity = str(entry.get("identity") or "")
        if not identity or identity in found:
            continue
        rect = entry.get("rect")
        if not rect or not _overlaps_bounds(rect, bounds):
            continue

        center = (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)
        if point_in_polygon(center, polygon):
            found.append(identity)
            continue
        if rect_coverage(rect, polygon) >= float(hit_ratio):
            found.append(identity)
    return found


# 外接矩形どうしが重なるか (多角形の判定を走らせる前のふるい)
def _overlaps_bounds(rect, bounds):
    if bounds is None:
        return False
    x, y, w, h = rect
    bx, by, bw, bh = bounds
    return x < bx + bw and bx < x + w and y < by + bh and by < y + h


# トラックが映っているセクションの数 (§5.6.3-5「該当: N セクション」)
# セクションの実体は経路で違う (§2.1)。素材の数で数えると、クリップ用は
# 1 本の素材を無音カットで分けているため、区間がいくつあっても 1 になってしまう。
#   クリップ用   : V1 のベースクリップ 1 件
#   アーカイブ用 : clip{N} 1 件 (複数のベースクリップに分かれる)
#   tracks : 解析結果のトラック [{"media_id","start_sec","end_sec"}, …] (素材の秒)
def count_sections(timeline, tracks):
    from ..archive.timeline_builder import ORIGIN_ARCHIVE_INDEX   # noqa: PLC0415 (循環 import を避ける)

    sections = set()
    for clip in timeline.base_clips():
        if clip.is_opening_or_ending():
            continue
        for track in tracks:
            if (str(track.get("media_id") or "") == str(clip.media_id)
                    and float(track.get("start_sec", 0.0)) < float(clip.source_out)
                    and float(clip.source_in) < float(track.get("end_sec", 0.0))):
                archive_index = clip.origin.get(ORIGIN_ARCHIVE_INDEX)
                sections.add(("clip", clip.id) if archive_index is None
                             else ("archive", archive_index))
                break
    return len(sections)


# 人物への指定を書き換えた新しい decisions を返す (元は変更しない)
def with_identity(decisions, identity_id, mode):
    updated = _copy(decisions)
    if mode in (BLUR, KEEP):
        updated["identities"][str(identity_id)] = mode
    else:
        updated["identities"].pop(str(identity_id), None)
    return updated


# 領域を足した新しい decisions を返す。通し番号も進める (§5.7 (1))。
def with_region(decisions, region):
    updated = _copy(decisions)
    updated["regions"].append(dict(region))
    updated["region_seq"] = max(_to_int(updated.get("region_seq")),
                                region_number(region.get("id")))
    return updated


# 領域を消した新しい decisions を返す
def without_region(decisions, region_id):
    updated = _copy(decisions)
    updated["regions"] = [r for r in updated["regions"] if str(r.get("id")) != str(region_id)]
    return updated


# 人物の統合を足した新しい decisions を返す (§5.6.6)
def with_merge(decisions, first_id, second_id):
    updated = _copy(decisions)
    updated["merges"].append([str(first_id), str(second_id)])
    return updated


# 領域 ID を採番する (r1, r2, …)。
#
# **一度使った番号は二度と使わない** (ver5 resolve3 §2.4 (a) / §5.7)。削除した領域の追従トラックは
# 解析結果に残るため、同じ ID を振ると新しい囲みが古い位置のトラックに化ける。
# 通し番号 (region_seq) に加え、Undo で通し番号が戻った場合に備えて
# 指定と解析結果に残っている ID も避ける。
def next_region_id(decisions, analysis=None):
    highest = _to_int((decisions or {}).get("region_seq"))
    names = [str(r.get("id") or "") for r in (decisions or {}).get("regions", [])]
    names += [str(t.get("identity") or "") for t in (analysis or {}).get("tracks", [])
              if str(t.get("kind", "person")) != "person"]
    for name in names:
        highest = max(highest, region_number(name))
    return f"r{highest + 1}"


# 領域 ID の番号部分 (r12 → 12)。形が違えば 0。
def region_number(region_id):
    match = _REGION_NUMBER_RE.match(str(region_id or ""))
    return int(match.group(1)) if match else 0


# 追加した枠の種類 (kind が無い v1 の領域は場所として読む)
def region_kind(region):
    kind = str((region or {}).get("kind") or KIND_PLACE)
    return kind if kind in (KIND_PLACE, KIND_OBJECT) else KIND_PLACE


# 領域の形の指紋 (§5.7 (2))。追従トラックがこの指定から作られたものかを確かめる。
# 位置・種類・追従方法のどれかが変われば別物になり、トラックを作り直す。
def region_shape_hash(region):
    region = region or {}
    payload = {
        "kind": region_kind(region),
        "media_id": str(region.get("media_id") or ""),
        "anchor_sec": round(float(region.get("anchor_sec") or 0.0), 3),
        "path": [[round(float(p[0]), 5), round(float(p[1]), 5)]
                 for p in (region.get("path") or []) if len(p) >= 2],
        "follow": str(region.get("follow") or ""),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# 領域の「ぼかす / ぼかさない」を変えた新しい decisions を返す
def with_region_mode(decisions, region_id, mode):
    updated = _copy(decisions)
    for region in updated["regions"]:
        if str(region.get("id")) == str(region_id):
            region["mode"] = mode
    return updated


# 領域の追従方法を変えた新しい decisions を返す (追従できなかった枠を固定にする / §3.6)
def with_region_follow(decisions, region_id, follow):
    updated = _copy(decisions)
    for region in updated["regions"]:
        if str(region.get("id")) == str(region_id):
            region["follow"] = follow
    return updated


# ------------------------------------------------------------------
# 検出枠のアンカー (削除・分割 / ver5 resolve3 §3.5)
# ------------------------------------------------------------------

# tracklet を覚えるためのアンカーを作る。解析のたびに振り直される tracklet ID は使わず、
# 「どの素材の、何秒に、どこにあった枠か」で覚える。サンプルの真ん中を代表にする。
def track_anchor(track):
    samples = (track or {}).get("samples") or []
    if not samples:
        return None
    sample = samples[len(samples) // 2]
    return {
        "media_id": str(track.get("media_id") or ""),
        "t": round(float(sample["t"]), 3),
        "rect": [round(float(sample[key]), 1) for key in ("x", "y", "w", "h")],
    }


# アンカーがこの tracklet を指しているか。
#   tolerance_sec: サンプル時刻のずれの許容 (解析の間隔ぶん)
def anchor_matches(anchor, track, tolerance_sec):
    if not anchor or str(anchor.get("media_id") or "") != str((track or {}).get("media_id") or ""):
        return False
    t = float(anchor["t"])
    rect = tuple(float(v) for v in anchor["rect"])
    for sample in (track or {}).get("samples") or []:
        if abs(float(sample["t"]) - t) > tolerance_sec:
            continue
        if iou(rect, (sample["x"], sample["y"], sample["w"], sample["h"])) >= ANCHOR_MIN_IOU:
            return True
    return False


# 2 つのアンカーが同じ枠を指すか (二重登録を避ける)
def same_anchor(a, b):
    if not a or not b or str(a.get("media_id")) != str(b.get("media_id")):
        return False
    if abs(float(a["t"]) - float(b["t"])) > 1e-3:
        return False
    return iou(tuple(a["rect"]), tuple(b["rect"])) >= 0.95


# 検出枠を削除した新しい decisions を返す (既に削除済みなら変えない)
def with_excluded(decisions, anchor):
    updated = _copy(decisions)
    if anchor and not any(same_anchor(anchor, item) for item in updated["excluded"]):
        updated["excluded"].append(dict(anchor))
    return updated


# 削除した検出枠を戻した新しい decisions を返す
def without_excluded(decisions, anchor):
    updated = _copy(decisions)
    updated["excluded"] = [item for item in updated["excluded"] if not same_anchor(anchor, item)]
    return updated


# 検出枠を別の人物にした新しい decisions を返す。新しい人物 ID (s1, s2, …) を振る。
def with_split(decisions, anchor):
    updated = _copy(decisions)
    if not anchor or any(same_anchor(anchor, item) for item in updated["splits"]):
        return updated
    highest = 0
    for item in updated["splits"]:
        match = _SPLIT_NUMBER_RE.match(str(item.get("as") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    updated["splits"].append(dict(anchor, **{"as": f"{SPLIT_PREFIX}{highest + 1}"}))
    return updated


# 分割を取り消した新しい decisions を返す
def without_split(decisions, anchor):
    updated = _copy(decisions)
    updated["splits"] = [item for item in updated["splits"] if not same_anchor(anchor, item)]
    return updated


# アンカーの形を整える。壊れていれば None。
def _anchor(item):
    if not isinstance(item, dict):
        return None
    try:
        rect = [float(v) for v in item.get("rect")]
        if len(rect) != 4:
            return None
        return {"media_id": str(item.get("media_id") or ""), "t": float(item.get("t")),
                "rect": rect}
    except (TypeError, ValueError):
        return None


def _to_int(value):
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _copy(decisions):
    base = decisions or _empty()
    return {
        "version": VERSION,
        "cache": base.get("cache", ""),
        "cache_abs": base.get("cache_abs", ""),
        "fingerprint": base.get("fingerprint", ""),
        "default_policy": base.get("default_policy", ""),
        "identities": dict(base.get("identities") or {}),
        "merges": [list(pair) for pair in (base.get("merges") or [])],
        "regions": [dict(region) for region in (base.get("regions") or [])],
        "excluded": [dict(item) for item in (base.get("excluded") or [])],
        "splits": [dict(item) for item in (base.get("splits") or [])],
        "region_seq": _to_int(base.get("region_seq")),
    }
