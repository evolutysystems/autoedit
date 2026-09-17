# 「どの時刻に、何を、ぼかす / 守る / 使わないか」を 1 か所で決める (ver5 resolve3 §4-5 / §5.4.1)
#
# 指定画面・Timeline のプレビューの目印・マスク生成が、それぞれ別に「ぼかすか」を判定していたため、
# 削除した領域の枠が画面にだけ残る不具合 (resolve3 §2.3 (a)) が起きた。
# 判定はここへまとめ、3 か所から同じ結果を引く。
#
# トラックの役割 (role):
#   "blur" … ぼかす
#   "keep" … 守る (重なったぼかしを削る / resolve3 §3.4)
#   None   … 使わない (対象外・削除した枠・指定に無い領域)
from bisect import bisect_left

from . import contour
from . import decisions as decisions_module
from .config import SHAPE_SILHOUETTE
from .geometry import source_rect_to_canvas, source_to_canvas_transform

ROLE_BLUR = "blur"
ROLE_KEEP = "keep"

KIND_PERSON = "person"
KIND_REGION = "region"      # 場所 (既存の領域の追従)
KIND_MANUAL = "manual"      # 手で足した人物・物 (resolve3 §3.6)


class BlurPlan:

    #   timeline  : 対象の Timeline (座標変換に使う)
    #   analysis  : 解析結果 (store.load / analyzer.analyze の戻り値)
    #   decisions : decisions.load(timeline) の戻り値
    #   cfg       : config.config() の戻り値
    def __init__(self, timeline, analysis, decisions, cfg):
        self._timeline = timeline
        self._analysis = analysis or {}
        self._decisions = decisions or decisions_module._empty()
        self._cfg = cfg
        self._pad = float(cfg["render"]["pad_sec"])
        self._use_silhouette = str(cfg["render"]["shape"]) == SHAPE_SILHOUETTE
        self._max_gap = float(cfg["silhouette"]["max_gap_sec"])
        self._min_fill = float(cfg["silhouette"]["min_fill_ratio"])
        # 輪郭の余白 (激しい動きでぼかしが外れないように / resolve3 §3.3.3)
        self._dilate_ratio = float(cfg["silhouette"]["dilate_ratio"])
        self._margin_box_ratio = float(cfg["silhouette"]["margin_box_ratio"])
        self._max_margin_box_ratio = float(cfg["silhouette"]["max_margin_box_ratio"])
        self._motion_lookahead = float(cfg["silhouette"]["motion_lookahead_sec"])
        self._keep_motion = bool(cfg["render"]["keep_motion_margin"])
        self._fast_motion_ratio = float(cfg["silhouette"]["fast_motion_box_ratio"])
        sample_fps = float(self._analysis.get("sample_fps") or cfg["analysis"]["sample_fps"])
        # アンカーの時刻のずれの許容 (解析の間隔の 1.5 倍)
        self._tolerance = 1.5 / max(sample_fps, 0.01)
        self._regions = {str(r.get("id")): r for r in self._decisions.get("regions", [])}
        self._transforms = {}
        self._labels = None

        self.entries = [self._entry(track) for track in self._analysis.get("tracks", [])]
        self.identities = self._effective_identities()
        self.main_id = next((i["id"] for i in self.identities if i["main"]), None)
        for entry in self.entries:
            entry["main"] = bool(entry["identity"]) and entry["identity"] == self.main_id
            entry["role"] = self._role(entry)

    # ------------------------------------------------------------------
    # 公開
    # ------------------------------------------------------------------

    # その時刻に映っている形の一覧 (キャンバス座標)。
    #   include_unused : 役割が None の枠 (対象外・削除した枠) も返すか (指定画面の表示用)
    # 戻り値: [{"key","kind","identity","role","main","excluded","rect","outline","silhouette",
    #          "label","track"}, …]
    #   rect       : 枠 (広げていない) (x, y, w, h)
    #   outline    : 手で描いた囲みの形 (枠に対する相対座標) or None
    #   silhouette : 身体の輪郭 (枠に対する相対座標) or None
    #   motion     : 前後 motion_lookahead_sec の枠の動きの大きさ (キャンバス px)。ぼかす人物だけ
    #   grow       : 輪郭の外側へ取る余白 (キャンバス px)。ぼかす人物だけ (守る形は 0)
    def shapes_at(self, media_id, source_sec, include_unused=False):
        labels = self.labels()
        shapes = []
        for entry in self.entries:
            track = entry["track"]
            if str(track.get("media_id") or "") != str(media_id):
                continue
            role = entry["role"]
            if role is None and not (include_unused and entry["displayable"]):
                continue
            # ぼかすものだけ前後へ pad_sec 伸ばす (取りこぼし対策)。守るものは伸ばさない
            pad = self._pad if role == ROLE_BLUR else 0.0
            if not (entry["start"] - pad <= source_sec <= entry["end"] + pad):
                continue
            rect = self._rect_at(entry, source_sec)
            if rect is None:
                continue
            transform = self._transform(media_id)
            canvas_rect = source_rect_to_canvas(rect, transform)
            motion = grow = 0.0
            silhouette = (self._silhouette_at(track, source_sec)
                          if entry["kind"] == KIND_PERSON and self._use_silhouette else None)
            if role == ROLE_BLUR and entry["kind"] == KIND_PERSON:
                motion = self._motion_at(entry, source_sec, transform)
                grow = self._grow(canvas_rect, motion)
                # 激しく動いている時刻は、輪郭をやめて四角 (+ 余白) でぼかす。
                # 輪郭は間引いた時刻の形しか持たず、姿勢の変化に追いつかないため (resolve3 §3.3.3)
                if (silhouette is not None and self._fast_motion_ratio > 0
                        and motion > self._fast_motion_ratio * min(canvas_rect[2], canvas_rect[3])):
                    silhouette = None
            elif role == ROLE_KEEP and entry["kind"] == KIND_PERSON and self._keep_motion:
                # 守る人物も激しく動くと輪郭がずれる。動いた量ぶんだけ守る形を広げる (基本の余白は足さない)
                motion = self._motion_at(entry, source_sec, transform)
                grow = min(motion, self._max_margin_box_ratio * max(canvas_rect[2], canvas_rect[3]))
            shapes.append({
                "key": str(track.get("id") or ""),
                "kind": entry["kind"],
                "identity": entry["identity"],
                "role": role,
                "main": entry["main"],
                "excluded": entry["excluded"],
                "rect": canvas_rect,
                "outline": entry["outline"],
                "silhouette": silhouette,
                "label": labels.get(entry["identity"], entry["identity"]),
                "track": track,
                "motion": motion,
                "grow": grow,
            })
        return shapes

    # ぼかす (role=blur) トラックが 1 本でもあるか
    def has_blur(self):
        return any(entry["role"] == ROLE_BLUR for entry in self.entries)

    # 役割別のトラック一覧 (マスク生成の件数表示・テストで使う)
    def tracks_with_role(self, role):
        return [entry for entry in self.entries if entry["role"] == role]

    # 表示名 (人物 1 / 人物 2 (主役) / 領域の名前)。マスク生成で毎フレーム引くため 1 回だけ作る
    def labels(self):
        if self._labels is not None:
            return self._labels
        labels = {}
        for index, identity in enumerate(self.identities, 1):
            name = f"人物 {index}"
            if identity["main"]:
                name += " (主役)"
            labels[identity["id"]] = name
        for region_id, region in self._regions.items():
            labels[region_id] = str(region.get("label") or region_id)
        self._labels = labels
        return labels

    # 検出枠の人物 ID (分割を当て込んだ後)。tracklet ID で引く。
    def identity_of(self, track_id):
        for entry in self.entries:
            if str(entry["track"].get("id")) == str(track_id):
                return entry["identity"]
        return ""

    # tracklet の削除 / 分割のアンカー (指定と突き合わせ済みのもの)
    def entry_of(self, track_id):
        for entry in self.entries:
            if str(entry["track"].get("id")) == str(track_id):
                return entry
        return None

    # 削除した検出枠の一覧 (指定画面の「削除した枠」用)
    def excluded_entries(self):
        return [entry for entry in self.entries if entry["excluded"]]

    # 指定にあるのに、使えるトラックが 1 本も無い領域 (追従できていない / resolve3 §5.7 (4))
    def untracked_regions(self):
        tracked = {entry["identity"] for entry in self.entries
                   if entry["kind"] != KIND_PERSON and entry["region"] is not None
                   and entry["track"].get("samples")}
        return [region for region_id, region in self._regions.items() if region_id not in tracked]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _entry(self, track):
        kind = str(track.get("kind", KIND_PERSON))
        samples = track.get("samples") or []
        entry = {
            "track": track,
            # サンプル時刻 (二分探索用)。マスク生成は毎フレーム何度も補間するため、先頭から探さない
            "times": [float(sample["t"]) for sample in samples],
            "kind": kind,
            "identity": str(track.get("identity") or ""),
            "start": float(samples[0]["t"]) if samples else 0.0,
            "end": float(samples[-1]["t"]) if samples else 0.0,
            "excluded": False,
            "region": None,
            "outline": contour.decode(track.get("outline")),
            "displayable": True,
            "main": False,
            "role": None,
        }
        if kind == KIND_PERSON:
            for split in self._decisions.get("splits", []):
                if decisions_module.anchor_matches(split, track, self._tolerance):
                    entry["identity"] = str(split["as"])
                    break
            entry["excluded"] = any(
                decisions_module.anchor_matches(anchor, track, self._tolerance)
                for anchor in self._decisions.get("excluded", []))
            return entry

        # 領域・手動の枠: 指定に無い (削除済み) か、形が変わった古いトラックは使わない (§5.7 (2))
        region = self._regions.get(entry["identity"])
        stale = (region is not None and track.get("shape_hash")
                 and track.get("shape_hash") != decisions_module.region_shape_hash(region))
        if region is None or stale:
            entry["displayable"] = False
        else:
            entry["region"] = region
        return entry

    def _role(self, entry):
        if entry["kind"] != KIND_PERSON:
            region = entry["region"]
            if region is None:
                return None
            return ROLE_BLUR if decisions_module.should_blur_region(region) else ROLE_KEEP

        if entry["excluded"] or not entry["identity"]:
            return None
        explicit = self._decisions.get("identities", {}).get(entry["identity"])
        if explicit == decisions_module.BLUR:
            return ROLE_BLUR
        if explicit == decisions_module.KEEP:
            return ROLE_KEEP
        # 主役は暗黙に守る (resolve2 R3 / resolve3 §3.4)
        if entry["main"]:
            return ROLE_KEEP
        policy = self._decisions.get("default_policy") or self._cfg["default_policy"]
        if policy == decisions_module.POLICY_BLUR_OTHERS:
            return ROLE_BLUR
        # manual_only で未指定 = 対象外。守る対象ではない (§3.4)
        return None

    # 分割・削除を当て込んだ人物の一覧。合計秒数の多い順で、先頭が主役 (resolve2 R3)。
    # 人物 ID は振り直さない (resolve3 §4-3)。分割でできた人物は s1, s2, … のまま後ろへ足す。
    def _effective_identities(self):
        by_id = {}
        order = []
        for identity in self._analysis.get("identities", []):
            identity_id = str(identity.get("id"))
            by_id[identity_id] = {"id": identity_id, "total_sec": 0.0,
                                  "thumb": identity.get("thumb") or "", "tracks": [],
                                  "split": False, "first": None, "main": False}
            order.append(identity_id)

        for entry in self.entries:
            if entry["kind"] != KIND_PERSON or not entry["identity"]:
                continue
            identity_id = entry["identity"]
            if identity_id not in by_id:
                by_id[identity_id] = {"id": identity_id, "total_sec": 0.0, "thumb": "",
                                      "tracks": [], "split": True, "first": None, "main": False}
                order.append(identity_id)
            item = by_id[identity_id]
            item["tracks"].append(str(entry["track"].get("id")))
            if entry["excluded"]:
                continue
            item["total_sec"] += max(entry["end"] - entry["start"], 0.0)
            item["first"] = entry["start"] if item["first"] is None else min(item["first"],
                                                                               entry["start"])

        identities = [by_id[identity_id] for identity_id in order if by_id[identity_id]["tracks"]
                      or not by_id[identity_id]["split"]]
        ranked = sorted((i for i in identities if i["total_sec"] > 0),
                        key=lambda i: (-i["total_sec"], i["first"] or 0.0))
        if ranked:
            ranked[0]["main"] = True
        return identities

    def _rect_at(self, entry, source_sec):
        return _rect_between(entry["track"].get("samples") or [], entry["times"], source_sec)

    def _transform(self, media_id):
        transform = self._transforms.get(media_id)
        if transform is None:
            media = self._timeline.media_by_id(media_id)
            transform = source_to_canvas_transform(media, self._timeline.width,
                                                   self._timeline.height)
            self._transforms[media_id] = transform
        return transform

    # 前後 motion_lookahead_sec の枠の動きの大きさ (キャンバス px)。
    # 中心の移動と、大きさの変化の半分 (縁の移動) の大きい方を、前と後ろで大きい方を取る。
    # 輪郭は間引いた時刻にしか無く、その間の激しい動きには形が追いつかないため、
    # 動いた量だけ余白を広げて、ぼかしが人物から外れないようにする (resolve3 §3.3.3)。
    def _motion_at(self, entry, source_sec, transform):
        if self._motion_lookahead <= 0:
            return 0.0
        now = self._rect_at(entry, source_sec)
        if now is None:
            return 0.0
        scale = transform[0]
        largest = 0.0
        for offset in (-self._motion_lookahead, self._motion_lookahead):
            other = self._rect_at(entry, source_sec + offset)
            if other is None:
                continue
            shift = max(abs((now[0] + now[2] / 2.0) - (other[0] + other[2] / 2.0)),
                        abs((now[1] + now[3] / 2.0) - (other[1] + other[3] / 2.0)))
            resize = max(abs(now[2] - other[2]), abs(now[3] - other[3])) / 2.0
            largest = max(largest, shift + resize)
        return largest * scale

    # 輪郭の外側へ取る余白 (キャンバス px)
    #   = max(画面幅の比率ぶん, 人物の幅の比率ぶん) + 動いた量
    # 人物の大きさに比例させるのは、手前に大きく映る人物ほど腕などの動きも大きく見えるため。
    # 上限は人物の枠の長い辺の max_margin_box_ratio 倍 (広げすぎて背景まで大きくぼかさない)。
    def _grow(self, canvas_rect, motion):
        width, height = float(canvas_rect[2]), float(canvas_rect[3])
        base = max(self._dilate_ratio * float(self._timeline.width),
                   self._margin_box_ratio * max(min(width, height), 0.0))
        limit = self._max_margin_box_ratio * max(width, height)
        return max(min(base + motion, limit), 0.0)

    # その時刻の輪郭 (枠に対する相対座標)。前後の輪郭を混ぜる (resolve3 §3.3)。
    # 当てにならない (離れすぎ・小さすぎ) なら None = 矩形へ落とす。
    def _silhouette_at(self, track, source_sec):
        before = None
        after = None
        for sample in track.get("samples") or []:
            if not sample.get("sil"):
                continue
            if float(sample["t"]) <= source_sec:
                before = sample
            else:
                after = sample
                break

        candidates = []
        if before is not None and source_sec - float(before["t"]) <= self._max_gap:
            candidates.append(before)
        if after is not None and float(after["t"]) - source_sec <= self._max_gap:
            candidates.append(after)
        if not candidates:
            return None

        shapes = [contour.decode(sample["sil"]) for sample in candidates]
        if any(shape is None for shape in shapes):
            return None
        if len(shapes) == 2:
            span = float(after["t"]) - float(before["t"])
            ratio = (source_sec - float(before["t"])) / span if span > 1e-9 else 0.0
            shape = contour.interpolate(shapes[0], shapes[1], ratio)
        else:
            shape = shapes[0]
        if contour.area(shape) < self._min_fill:
            return None
        return shape


# 追従トラックのその時刻の矩形 (sample_rect_at と同じ結果を二分探索で求める)
def _rect_between(samples, times, source_sec):
    if not samples:
        return None
    if source_sec <= times[0]:
        first = samples[0]
        return (first["x"], first["y"], first["w"], first["h"])
    if source_sec >= times[-1]:
        last = samples[-1]
        return (last["x"], last["y"], last["w"], last["h"])
    index = bisect_left(times, source_sec)
    after = samples[index]
    before = samples[max(index - 1, 0)]
    span = times[index] - times[max(index - 1, 0)]
    ratio = (source_sec - times[max(index - 1, 0)]) / span if span > 1e-9 else 0.0
    return (
        before["x"] + (after["x"] - before["x"]) * ratio,
        before["y"] + (after["y"] - before["y"]) * ratio,
        before["w"] + (after["w"] - before["w"]) * ratio,
        before["h"] + (after["h"] - before["h"]) * ratio,
    )


# サンプル列から、その時刻の矩形を線形補間で求める (素材ピクセル)。
# 範囲外なら端の矩形 (pad_sec ぶんのはみ出しに備える)。サンプルが無ければ None。
def sample_rect_at(samples, source_sec):
    if not samples:
        return None
    first = samples[0]
    last = samples[-1]
    if source_sec <= float(first["t"]):
        return (first["x"], first["y"], first["w"], first["h"])
    if source_sec >= float(last["t"]):
        return (last["x"], last["y"], last["w"], last["h"])

    previous = first
    for sample in samples:
        if float(sample["t"]) >= source_sec:
            span = float(sample["t"]) - float(previous["t"])
            ratio = (source_sec - float(previous["t"])) / span if span > 1e-9 else 0.0
            return (
                previous["x"] + (sample["x"] - previous["x"]) * ratio,
                previous["y"] + (sample["y"] - previous["y"]) * ratio,
                previous["w"] + (sample["w"] - previous["w"]) * ratio,
                previous["h"] + (sample["h"] - previous["h"]) * ratio,
            )
        previous = sample
    return (last["x"], last["y"], last["w"], last["h"])
