# ぼかし指定の読み書きと、囲みパスの当て込み (ver5 resolve2 §5.2.2 / §5.6.3)
#
# 指定は timeline.source["blur"] に置く。ここに置くと
#   ・commands._snapshot が deepcopy するため Undo/Redo が自動で効く (§2.6)
#   ・project_io がそのまま書き出すため、開き直しても残る
# ただし **1KB 程度に収まる大きさしか置かない**。重い解析結果は store.py 側。
#
# 指定は「明示されたものだけ」を持つ。触っていない人物は default_policy に従い、
# 主役 (R3) は policy に関わらず常にぼかさない。
from ..utils.logger import get_logger
from .config import POLICY_BLUR_OTHERS, POLICY_MANUAL_ONLY
from .geometry import point_in_polygon, polygon_bounds, rect_coverage

_logger = get_logger(__name__)

# source["blur"] の書式版
VERSION = 1

# 人物・領域に対する指定値
BLUR = "blur"      # ぼかす
KEEP = "keep"      # ぼかさない


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


# 領域を足した新しい decisions を返す
def with_region(decisions, region):
    updated = _copy(decisions)
    updated["regions"].append(dict(region))
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


# 領域 ID を採番する (r1, r2, …)。既存と重複させない。
def next_region_id(decisions):
    used = {str(r.get("id")) for r in (decisions or {}).get("regions", [])}
    index = 1
    while f"r{index}" in used:
        index += 1
    return f"r{index}"


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
    }
