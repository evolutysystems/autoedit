# ぼかしの指定の読み書き (ver5 resolve8 §5.2 / §5.3)
#
# 指定は timeline.source["blur"] に置く。ここに置くと
#   ・commands._snapshot が deepcopy するため Undo/Redo が自動で効く
#   ・project_io がそのまま書き出すため、開き直しても残る
#
# ver5 resolve8 で考え方を単純にした (version 5)。
#
#   ・指定 (spec) は **「全面ぼかし (frame)」と「囲み (area)」の 2 種類だけ**。
#     人物の自動検出による枠・アンカー・名前表・隠した枠は廃止した。
#   ・囲みは **キーフレーム (keys)** を持つ。利用者が置いた位置と大きさが正で、
#     追従はその間を埋めるだけ (resolve8 §3.3)。
#   ・指定は **並び順のまま重ねる**。後から足したものが上に来る (最後の操作が勝つ)。
#   ・効く範囲は **そのクリップの素材区間 (span)** だけ。適用範囲の種類は無い (§3.8)。
#
# 座標はすべて **正規化キャンバス座標 (0.0〜1.0)** で持つ (§4-3)。
# 出力の解像度が変わっても指定が生きる。
import re

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# source["blur"] の書式版
VERSION = 5

# 指定の値
BLUR = "blur"      # ボカす
KEEP = "keep"      # ボカさない

# 指定の種類
KIND_FRAME = "frame"   # 画面全体 (ボカさないを選んだときに自動で足される)
KIND_AREA = "area"     # マウスで囲んだ矩形
_KINDS = (KIND_FRAME, KIND_AREA)

# 囲みの追従方法
FOLLOW_TRACK = "track"
FOLLOW_FIXED = "fixed"
_FOLLOWS = (FOLLOW_TRACK, FOLLOW_FIXED)

# 指定がこの件数を超えたら警告する (プロジェクト JSON が太る)
SPEC_WARN_COUNT = 200

# 同じキーフレームとみなす時刻の差 (呼び出し側は半コマを渡す / §5.3)
DEFAULT_KEY_EPSILON = 1.0e-3

_SPEC_NUMBER_RE = re.compile(r"^b(\d+)$")


# ------------------------------------------------------------------
# 読み書き
# ------------------------------------------------------------------

# timeline.source["blur"] を読む (無ければ空の指定を返す)。
# v4 以前の書式は新しい書式へ読み替える (resolve8 §3.9)。
def load(timeline):
    section = (timeline.source or {}).get("blur") if timeline is not None else None
    if not isinstance(section, dict):
        return _empty()

    version = _to_int(section.get("version"), VERSION)
    decisions = _empty()
    decisions["cache"] = str(section.get("cache", "") or "")
    decisions["cache_abs"] = str(section.get("cache_abs", "") or "")
    decisions["spec_seq"] = _to_int(section.get("spec_seq"))
    if version >= VERSION:
        decisions["specs"] = [spec for spec in (_spec(item) for item in (section.get("specs") or []))
                              if spec is not None]
        return decisions

    _migrate(section, decisions)
    return decisions


# timeline.source["blur"] へ書き戻す (コマンドの apply から呼ぶ)
def store(timeline, decisions):
    source = timeline.source if isinstance(timeline.source, dict) else {}
    section = {"version": VERSION}

    # 空の値は書かない (プロジェクト JSON を無駄に太らせない)
    for key in ("cache", "cache_abs"):
        value = (decisions or {}).get(key)
        if value:
            section[key] = value
    specs = [dict(spec) for spec in (decisions or {}).get("specs") or []]
    if specs:
        section["specs"] = specs
        if len(specs) > SPEC_WARN_COUNT:
            _logger.warning("ぼかしの指定が %d 件あります。プロジェクトの保存が重くなる場合があります",
                            len(specs))
    sequence = _to_int((decisions or {}).get("spec_seq"))
    if sequence:
        section["spec_seq"] = sequence

    source["blur"] = section
    timeline.source = source
    return section


def _empty():
    return {
        "version": VERSION,
        "cache": "",
        "cache_abs": "",
        "specs": [],
        "spec_seq": 0,
        "migrated": False,
        "migrated_dropped": 0,
        "migrated_kept": 0,
    }


def _copy(decisions):
    base = decisions or _empty()
    return {
        "version": VERSION,
        "cache": base.get("cache", ""),
        "cache_abs": base.get("cache_abs", ""),
        "specs": [_deep_spec(spec) for spec in (base.get("specs") or [])],
        "spec_seq": _to_int(base.get("spec_seq")),
        "migrated": bool(base.get("migrated")),
        "migrated_dropped": _to_int(base.get("migrated_dropped")),
        "migrated_kept": _to_int(base.get("migrated_kept")),
    }


# 指定 1 件を複製する (keys は入れ子なので浅いコピーでは足りない)
def _deep_spec(spec):
    copied = dict(spec)
    copied["span"] = dict(spec.get("span") or {})
    if spec.get("keys") is not None:
        copied["keys"] = [{"t": float(k["t"]), "rect": list(k["rect"])}
                          for k in spec.get("keys") or []]
    return copied


# ------------------------------------------------------------------
# 区間 (resolve8 §3.8)
# ------------------------------------------------------------------

# クリップから「効く素材区間」を作る。
#   clip : 基準にするベースクリップ
# 戻り値: {"start","end","clip_id","label"}
#
# クリップ ID だけを覚えず **素材の区間**を写し取るのは、クリップを分割・移動しても
# 指定が生き残るようにするため。clip_id と label は表示用で、判定には使わない。
def span_for(timeline, clip):
    return {
        "start": round(float(clip.source_in), 3),
        "end": round(float(clip.source_out), 3),
        "clip_id": str(getattr(clip, "id", "") or ""),
        "label": f"クリップ {clip_number(timeline, clip)}",
    }


# ベーストラックの何本目のクリップか (1 始まり)
def clip_number(timeline, clip):
    if timeline is None:
        return 1
    for index, other in enumerate(timeline.base_clips(), 1):
        if other.id == getattr(clip, "id", None):
            return index
    return 1


# その指定が、この素材のこの時刻に効くか
def span_contains(spec, media_id, source_sec):
    if not isinstance(spec, dict):
        return False
    if str(spec.get("media_id") or "") != str(media_id):
        return False
    span = spec.get("span") or {}
    try:
        return float(span["start"]) <= float(source_sec) <= float(span["end"])
    except (KeyError, TypeError, ValueError):
        return False


# そのクリップの区間に重なる指定 (並び順のまま)
def specs_in_clip(decisions, clip):
    if clip is None:
        return []
    media_id = str(getattr(clip, "media_id", "") or "")
    start = float(clip.source_in)
    end = float(clip.source_out)
    found = []
    for spec in (decisions or {}).get("specs", []):
        if str(spec.get("media_id") or "") != media_id:
            continue
        span = spec.get("span") or {}
        if float(span.get("start", 0.0)) < end and start < float(span.get("end", 0.0)):
            found.append(spec)
    return found


# その時刻に掛かっている「全面ぼかし」の指定 (無ければ None)
def frame_spec_at(decisions, media_id, source_sec):
    for spec in (decisions or {}).get("specs", []):
        if spec.get("kind") != KIND_FRAME or spec.get("mode") != BLUR:
            continue
        if span_contains(spec, media_id, source_sec):
            return spec
    return None


# 指定が触っている素材 ID の一覧
def media_ids(decisions):
    return sorted({str(spec.get("media_id") or "")
                   for spec in (decisions or {}).get("specs", [])
                   if str(spec.get("media_id") or "")})


# ------------------------------------------------------------------
# 指定 (spec)
# ------------------------------------------------------------------

# 「画面全体をぼかす」指定を作る
def make_frame_spec(spec_id, media_id, span):
    return {"id": str(spec_id), "kind": KIND_FRAME, "mode": BLUR,
            "media_id": str(media_id), "span": dict(span or {})}


# 囲みの指定を作る (キーフレーム 1 点つき)
#   t    : 囲んだ時刻 (素材の秒)
#   rect : 正規化キャンバス座標の (x, y, w, h)
# 矩形が潰れている (幅か高さが 0) 場合は None を返す。
def make_area_spec(spec_id, mode, media_id, span, t, rect, label="",
                   follow=FOLLOW_TRACK, outline=""):
    clean = _clean_rect(rect)
    if clean is None:
        return None
    spec = {
        "id": str(spec_id),
        "kind": KIND_AREA,
        "mode": mode if mode in (BLUR, KEEP) else BLUR,
        "media_id": str(media_id),
        "span": dict(span or {}),
        "follow": follow if follow in _FOLLOWS else FOLLOW_TRACK,
        "keys": [{"t": round(float(t), 3), "rect": clean}],
    }
    if label:
        spec["label"] = str(label)
    if outline:
        spec["outline"] = str(outline)
    return spec


# 指定 ID を採番する (b1, b2, …)。
# 一度使った番号は二度と使わない (消した指定の追従結果と取り違えないため)。
def next_spec_id(decisions):
    highest = _to_int((decisions or {}).get("spec_seq"))
    for spec in (decisions or {}).get("specs", []):
        highest = max(highest, spec_number(spec.get("id")))
    return f"b{highest + 1}"


# 指定 ID の番号部分 (b12 → 12)。形が違えば 0。
def spec_number(spec_id):
    match = _SPEC_NUMBER_RE.match(str(spec_id or ""))
    return int(match.group(1)) if match else 0


# 指定を足した新しい decisions を返す。
#   to_bottom: True なら一覧の先頭へ (全面ぼかしは一番下が自然なため)
# 既定は末尾 = 一番上に重なる (後からやった操作が勝つ / resolve8 §3.2)。
def with_spec(decisions, spec, to_bottom=False):
    updated = _copy(decisions)
    entry = _deep_spec(spec)
    if to_bottom:
        updated["specs"].insert(0, entry)
    else:
        updated["specs"].append(entry)
    updated["spec_seq"] = max(_to_int(updated.get("spec_seq")), spec_number(entry.get("id")))
    return updated


# 指定を消した新しい decisions を返す
def without_spec(decisions, spec_id):
    updated = _copy(decisions)
    updated["specs"] = [s for s in updated["specs"] if str(s.get("id")) != str(spec_id)]
    return updated


# 指定の「ボカす / ボカさない」を変えた新しい decisions を返す
def with_spec_mode(decisions, spec_id, mode):
    return _with_field(decisions, spec_id, "mode", mode if mode in (BLUR, KEEP) else BLUR)


# 指定の表示名を変えた新しい decisions を返す (空文字なら既定の呼び名へ戻す)
def with_spec_label(decisions, spec_id, label):
    updated = _copy(decisions)
    for spec in updated["specs"]:
        if str(spec.get("id")) != str(spec_id):
            continue
        text = str(label or "")
        if text:
            spec["label"] = text
        else:
            spec.pop("label", None)
    return updated


# 指定の追従方法を変えた新しい decisions を返す
def with_spec_follow(decisions, spec_id, follow):
    return _with_field(decisions, spec_id, "follow",
                       follow if follow in _FOLLOWS else FOLLOW_TRACK)


def _with_field(decisions, spec_id, key, value):
    updated = _copy(decisions)
    for spec in updated["specs"]:
        if str(spec.get("id")) == str(spec_id):
            spec[key] = value
    return updated


# 指定の重ね順を 1 つ動かした新しい decisions を返す (delta: +1 = 上へ / -1 = 下へ)
def with_spec_moved(decisions, spec_id, delta):
    updated = _copy(decisions)
    specs = updated["specs"]
    for index, spec in enumerate(specs):
        if str(spec.get("id")) != str(spec_id):
            continue
        target = index + (1 if int(delta) > 0 else -1)
        if 0 <= target < len(specs):
            specs[index], specs[target] = specs[target], specs[index]
        break
    return updated


# ------------------------------------------------------------------
# キーフレーム (resolve8 §5.3 / §5.4)
# ------------------------------------------------------------------

# 指定のキーフレーム (時刻の昇順)
def keys_of(spec):
    keys = [{"t": float(k["t"]), "rect": [float(v) for v in k["rect"]]}
            for k in (spec or {}).get("keys") or []
            if isinstance(k, dict) and k.get("rect") is not None]
    return sorted(keys, key=lambda k: k["t"])


# その時刻のキーフレーム (無ければ None)
def key_at(spec, t, epsilon=DEFAULT_KEY_EPSILON):
    for key in keys_of(spec):
        if abs(key["t"] - float(t)) <= float(epsilon):
            return key
    return None


# キーフレームを置いた新しい decisions を返す (同じ時刻のキーは置き換える)。
# 矩形が潰れているときは何もしない。
def with_key(decisions, spec_id, t, rect, epsilon=DEFAULT_KEY_EPSILON):
    updated = _copy(decisions)
    clean = _clean_rect(rect)
    if clean is None:
        return updated
    for spec in updated["specs"]:
        if str(spec.get("id")) != str(spec_id) or spec.get("kind") != KIND_AREA:
            continue
        keys = [k for k in spec.get("keys") or []
                if abs(float(k["t"]) - float(t)) > float(epsilon)]
        keys.append({"t": round(float(t), 3), "rect": list(clean)})
        spec["keys"] = sorted(keys, key=lambda k: float(k["t"]))
    return updated


# キーフレームを消した新しい decisions を返す。
# **最後の 1 件は消さない** (キーが 0 件になると囲みの位置が決まらないため)。
def without_key(decisions, spec_id, t, epsilon=DEFAULT_KEY_EPSILON):
    updated = _copy(decisions)
    for spec in updated["specs"]:
        if str(spec.get("id")) != str(spec_id) or spec.get("kind") != KIND_AREA:
            continue
        keys = spec.get("keys") or []
        if len(keys) <= 1:
            continue
        spec["keys"] = [k for k in keys if abs(float(k["t"]) - float(t)) > float(epsilon)] or keys
    return updated


# ------------------------------------------------------------------
# 何が必要かの判定
# ------------------------------------------------------------------

# 明示した指定が 1 つでもあるか
def has_any(decisions):
    return bool((decisions or {}).get("specs"))


# マスクを作る必要があるか。
# 「ボカす」指定が 1 つも無ければ作らない (= FFmpeg に 1 行も足さない)。
def needs_mask(decisions):
    return any(spec.get("mode") == BLUR for spec in (decisions or {}).get("specs", []))


# 追従が必要か (追いかける囲みがあるか)
def needs_tracking(decisions):
    return any(spec.get("kind") == KIND_AREA and spec.get("follow", FOLLOW_TRACK) == FOLLOW_TRACK
               for spec in (decisions or {}).get("specs", []))


# ------------------------------------------------------------------
# 名前
# ------------------------------------------------------------------

# 名前を整える (前後の空白と改行を除き、上限で切る)。空になれば "" を返す。
def clean_label(name, max_len=32):
    text = str(name if name is not None else "")
    text = "".join(" " if ch in "\r\n\t" else ch for ch in text if ch >= " " or ch in "\r\n\t")
    text = " ".join(text.split()).strip()
    limit = max(int(max_len or 0), 1)
    return text[:limit]


# ------------------------------------------------------------------
# v4 以前からの読み替え (resolve8 §3.9)
# ------------------------------------------------------------------

# 旧書式を新しい書式へ移す。移せないものは捨て、件数を控える。
#   引き継ぐ : 全面ぼかし (marks kind=frame) / 手で囲んだ枠 (marks kind=region + regions)
#   捨てる   : 人物の枠への指定 (marks kind=track) / 隠した枠 (excluded) / 枠の名前 (names)
#              v3 以前の identities / merges / splits / default_policy
def _migrate(section, decisions):
    decisions["migrated"] = True
    regions = {str(r.get("id") or ""): r for r in (section.get("regions") or [])
               if isinstance(r, dict)}
    dropped = 0

    for mark in (section.get("marks") or []):
        if not isinstance(mark, dict):
            continue
        target = mark.get("target") if isinstance(mark.get("target"), dict) else {}
        scope = mark.get("scope") if isinstance(mark.get("scope"), dict) else {}
        kind = str(target.get("kind") or "")
        mode = str(mark.get("mode") or BLUR)
        media_id = str(scope.get("media_id") or "")
        if not media_id:
            continue
        span = {"start": float(scope.get("start", 0.0)), "end": float(scope.get("end", 0.0)),
                "clip_id": str(scope.get("clip_id") or ""),
                "label": str(scope.get("label") or "")}

        if kind == "frame":
            decisions["spec_seq"] += 1
            decisions["specs"].append(make_frame_spec(
                f"b{decisions['spec_seq']}", media_id, span))
            decisions["migrated_kept"] += 1
            continue
        if kind == "region":
            spec = _spec_from_region(regions.get(str(target.get("id") or "")), mode, span,
                                     decisions)
            if spec is None:
                dropped += 1
                continue
            decisions["specs"].append(spec)
            decisions["migrated_kept"] += 1
            continue
        dropped += 1                    # 人物の枠への指定 (移し先が無い)

    # v3 以前の人物への指定なども移せない
    dropped += len(section.get("identities") or {}) + len(section.get("merges") or []) \
        + len(section.get("splits") or [])
    decisions["migrated_dropped"] = dropped
    if dropped:
        _logger.info("ぼかし: 以前の書式の指定 %d 件は新しい方式へ移せないため引き継ぎません "
                     "(人物の自動検出を廃止したため)", dropped)


# v4 の領域 (囲みパス) を v5 の囲みの指定へ直す
def _spec_from_region(region, mode, span, decisions):
    if not isinstance(region, dict):
        return None
    path = [[float(p[0]), float(p[1])] for p in (region.get("path") or []) if len(p) >= 2]
    if len(path) < 3:
        return None

    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    rect = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    if rect[2] <= 0 or rect[3] <= 0:
        return None

    # 自由な囲みの形は「枠に対する相対の点列」として持ち越す (新規には作らない / §3.6)
    from . import contour                          # noqa: PLC0415 (移行のときだけ読む)

    outline = contour.encode(contour.to_relative(path, rect))
    decisions["spec_seq"] += 1
    return make_area_spec(
        f"b{decisions['spec_seq']}", mode, str(region.get("media_id") or ""), span,
        float(region.get("anchor_sec") or span.get("start") or 0.0), rect,
        label=str(region.get("label") or ""),
        follow=str(region.get("follow") or FOLLOW_TRACK), outline=outline)


# ------------------------------------------------------------------
# 形を整える道具
# ------------------------------------------------------------------

# 指定 1 件の形を整える。壊れていれば None。
def _spec(item):
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "")
    mode = str(item.get("mode") or "")
    if kind not in _KINDS or mode not in (BLUR, KEEP):
        return None
    media_id = str(item.get("media_id") or "")
    span = _span(item.get("span"))
    if not media_id or span is None:
        return None

    spec = {"id": str(item.get("id") or ""), "kind": kind, "mode": mode,
            "media_id": media_id, "span": span}
    if item.get("label"):
        spec["label"] = str(item["label"])
    if kind == KIND_FRAME:
        spec["mode"] = BLUR                        # 全面は必ず「ボカす」
        return spec

    keys = []
    for key in item.get("keys") or []:
        if not isinstance(key, dict):
            continue
        rect = _clean_rect(key.get("rect"))
        if rect is None:
            continue
        try:
            keys.append({"t": round(float(key["t"]), 3), "rect": rect})
        except (KeyError, TypeError, ValueError):
            continue
    if not keys:
        return None
    spec["keys"] = sorted(keys, key=lambda k: k["t"])
    follow = str(item.get("follow") or FOLLOW_TRACK)
    spec["follow"] = follow if follow in _FOLLOWS else FOLLOW_TRACK
    if item.get("outline"):
        spec["outline"] = str(item["outline"])
    return spec


def _span(value):
    if not isinstance(value, dict):
        return None
    try:
        start = float(value.get("start", 0.0))
        end = float(value.get("end", 0.0))
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    return {"start": round(start, 3), "end": round(end, 3),
            "clip_id": str(value.get("clip_id") or ""),
            "label": str(value.get("label") or "")}


# 正規化キャンバス座標の矩形を整える (0.0〜1.0 の中へ収め、4 桁で丸める)。壊れていれば None。
def _clean_rect(rect):
    try:
        x, y, width, height = (float(v) for v in rect)
    except (TypeError, ValueError):
        return None
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    width = min(max(width, 0.0), 1.0 - x)
    height = min(max(height, 0.0), 1.0 - y)
    if width <= 0.0 or height <= 0.0:
        return None
    return [round(x, 4), round(y, 4), round(width, 4), round(height, 4)]


def _to_int(value, default=0):
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default
