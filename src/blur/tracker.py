# 区間内トラッキングと全体クラスタリング (ver5 resolve2 §3.2)
#
# 処理は 2 段階に分ける。
#   ① 区間内トラッキング (tracklet)  IoU + 埋め込み距離で、連続フレームの検出をつなぐ
#   ② 全体クラスタリング (identity)  全 tracklet の平均埋め込みをまとめ、人物 ID を振る
#
# ②があるため、**離れたセクション・別ファイルにまたがっても同じ人物 ID になる** (R6)。
# 単純な階層的クラスタリング (しきい値以下の距離を貪欲に統合) で足りるため、
# sklearn は使わず numpy で書く (数百件程度の統合に外部ライブラリは要らない)。
import numpy as np

from ..utils.logger import get_logger
from .geometry import containment, iou

_logger = get_logger(__name__)

# 同じ時刻の 2 つの枠を「同じ人への重複した枠」とみなす条件の既定値
# (setting.json の blur.analysis.same_box_containment / same_box_center_ratio / ver5 resolve3 §5.8)。
#   ・小さい方の枠がこの割合以上、大きい枠に入っている (実素材の重複枠は 0.98 前後)
#   ・横の中心のずれが、小さい方の枠の幅のこの倍率以内
# 割合だけで判定すると、隣り合って座った 2 人 (実測 0.78 / 中心のずれ 0.60 倍) を 1 人にしてしまう。
_SAME_BOX_CONTAINMENT = 0.85
_SAME_BOX_CENTER_RATIO = 0.5


# tracklet 1 本 (同じ素材の中で連続して追えた 1 人ぶん)
class Tracklet:

    def __init__(self, track_id, media_id, kind="person"):
        self.id = track_id
        self.media_id = media_id
        self.kind = kind
        self.samples = []            # [{"t","x","y","w","h","score"[,"sil"]}, …] 素材ピクセル座標
        self.embeddings = []         # サンプルごとの特徴ベクトル
        self.identity = None         # 全体クラスタリングで決まる人物 ID
        self.last_sec = 0.0

    @property
    def start_sec(self):
        return self.samples[0]["t"] if self.samples else 0.0

    @property
    def end_sec(self):
        return self.samples[-1]["t"] if self.samples else 0.0

    @property
    def duration_sec(self):
        return max(self.end_sec - self.start_sec, 0.0)

    # 直前の矩形 (IoU の比較対象)
    def last_box(self):
        if not self.samples:
            return None
        sample = self.samples[-1]
        return (sample["x"], sample["y"], sample["w"], sample["h"])

    # 平均特徴ベクトル (L2 正規化済み)。埋め込みが無ければ None。
    def mean_embedding(self):
        if not self.embeddings:
            return None
        mean = np.mean(np.stack(self.embeddings, axis=0), axis=0)
        norm = float(np.linalg.norm(mean))
        return mean / norm if norm > 1e-9 else None

    #   silhouette: 身体の輪郭 (枠に対する相対座標 / contour.encode 済みの文字列) or None
    def add(self, sec, box, embedding, silhouette=None):
        x, y, w, h = (float(v) for v in box[:4])
        score = float(box[4]) if len(box) > 4 else 1.0
        sample = {"t": float(sec), "x": x, "y": y, "w": w, "h": h, "score": score}
        if silhouette:
            sample["sil"] = silhouette
        self.samples.append(sample)
        if embedding is not None:
            self.embeddings.append(np.asarray(embedding, dtype=np.float32))
        self.last_sec = float(sec)

    def to_dict(self):
        return {
            "id": self.id,
            "identity": self.identity,
            "media_id": self.media_id,
            "kind": self.kind,
            "start_sec": round(self.start_sec, 3),
            "end_sec": round(self.end_sec, 3),
            "samples": [_sample_dict(s) for s in self.samples],
        }


# サンプルを保存用の辞書にする (輪郭があるときだけ "sil" を持たせる)
def _sample_dict(sample):
    result = {
        "t": round(sample["t"], 3),
        "x": round(sample["x"], 1), "y": round(sample["y"], 1),
        "w": round(sample["w"], 1), "h": round(sample["h"], 1),
        "score": round(sample["score"], 3),
    }
    if sample.get("sil"):
        result["sil"] = sample["sil"]
    return result


# 1 素材ぶんの区間内トラッキング
#
# 人物 ID の採番を決定的にするため (§8-7)、tracklet は**登場順**に番号を振る。
# 指紋が同じなら解析をやり直しても同じ ID になり、指定 (source["blur"]) が生き残る。
class Tracker:

    def __init__(self, cfg, media_id, id_prefix="t", start_index=0):
        analysis = cfg["analysis"]
        self._iou_threshold = float(analysis["iou_threshold"])
        self._embed_threshold = float(analysis["embed_threshold"])
        self._min_track_sec = float(analysis["min_track_sec"])
        self._sample_interval = 1.0 / max(float(analysis["sample_fps"]), 0.01)
        self._media_id = media_id
        self._prefix = id_prefix
        self._counter = int(start_index)
        self._active = []
        self._finished = []

    # 1 サンプル時刻ぶんの検出を取り込む。
    #   sec        : 素材内の時刻
    #   boxes      : [(x, y, w, h, score), …]
    #   embeddings : boxes と同じ並びの特徴ベクトル (無ければ None)
    #   silhouettes: boxes と同じ並びの輪郭 (encode 済みの文字列 or None / ver5 resolve3 §5.3.3)
    def update(self, sec, boxes, embeddings=None, silhouettes=None):
        sec = float(sec)
        # サンプル 2 回ぶん途切れたら別人として切る (途中で見失った扱い)
        self._retire(sec, self._sample_interval * 2.5)

        boxes = list(boxes or [])
        vectors = list(embeddings) if embeddings is not None and len(embeddings) else []
        shapes = list(silhouettes or [])
        pairs = self._match(boxes, vectors)

        used = set()
        for box_index, track in pairs.items():
            used.add(box_index)
            vector = vectors[box_index] if box_index < len(vectors) else None
            shape = shapes[box_index] if box_index < len(shapes) else None
            track.add(sec, boxes[box_index], vector, shape)

        # 対応が付かなかった検出は新しい tracklet にする
        for index, box in enumerate(boxes):
            if index in used:
                continue
            self._counter += 1
            track = Tracklet(f"{self._prefix}{self._counter}", self._media_id)
            vector = vectors[index] if index < len(vectors) else None
            shape = shapes[index] if index < len(shapes) else None
            track.add(sec, box, vector, shape)
            self._active.append(track)

    # 検出と追跡中 tracklet の対応を決める (貪欲マッチ)。
    # IoU が高いものから順に確定させ、IoU が足りないものは埋め込み距離で救う。
    # 埋め込みで救うのは「一瞬の遮蔽で枠が飛んだ」場合を同一人物として続けるため。
    def _match(self, boxes, vectors):
        candidates = []
        for box_index, box in enumerate(boxes):
            vector = vectors[box_index] if box_index < len(vectors) else None
            for track in self._active:
                last = track.last_box()
                if last is None:
                    continue
                overlap = iou(last, box[:4])
                if overlap >= self._iou_threshold:
                    # 重なりが十分ある = 位置で確実に同じもの。距離が小さいほど先に確定させる
                    candidates.append((1.0 - overlap, box_index, track))
                    continue
                mean = track.mean_embedding()
                if vector is None or mean is None:
                    continue
                gap = float(1.0 - np.dot(mean, np.asarray(vector, dtype=np.float32)))
                if gap <= self._embed_threshold:
                    # 位置は離れたが見た目が一致。IoU の一致より後ろへ回す
                    candidates.append((1.0 + gap, box_index, track))

        candidates.sort(key=lambda item: item[0])
        pairs = {}
        taken = set()
        for _cost, box_index, track in candidates:
            if box_index in pairs or id(track) in taken:
                continue
            pairs[box_index] = track
            taken.add(id(track))
        return pairs

    # 一定時間更新されなかった tracklet を確定させる
    def _retire(self, sec, max_gap):
        still_active = []
        for track in self._active:
            if sec - track.last_sec > max_gap:
                self._finish(track)
            else:
                still_active.append(track)
        self._active = still_active

    def _finish(self, track):
        # 短すぎる tracklet は誤検出とみなして捨てる (§7 min_track_sec)
        if track.duration_sec + 1e-6 < self._min_track_sec:
            return
        self._finished.append(track)

    # 追跡中 (まだ確定していない) の tracklet 一覧。見本画像の紐付けに使う。
    def active_tracks(self):
        return list(self._active)

    # 追跡を終え、確定した tracklet の一覧を返す
    def finish(self):
        for track in self._active:
            self._finish(track)
        self._active = []
        return list(self._finished)

    # 次の Tracker へ引き継ぐ採番位置 (素材をまたいで ID を重複させない)
    def next_index(self):
        return self._counter


# ------------------------------------------------------------------
# 全体クラスタリング (§3.2 ②)
# ------------------------------------------------------------------

# tracklet 群を人物 ID へまとめる。
#   tracklets : Tracklet の列 (全素材ぶん)
#   cfg       : config.config() の戻り値
#   merges    : 手で統合した組 [[人物 ID, 人物 ID], …] (§5.6.6)。解析後に当て込む。
# 戻り値: identities の一覧 (辞書)。各 tracklet の identity も書き換える。
#
# 見た目が近くても、同じフレームに並んで映っている tracklet は同じ群へ入れない (_appears_together)。
# 服装の似た 2 人 (同じ場所・同じ照明) は全身 ReID の距離が縮みやすく、
# 距離だけで統合すると 2 人とも主役扱いになり、誰もぼかされなくなるため。
def cluster(tracklets, cfg, merges=None):
    tracks = [t for t in tracklets if t.kind == "person"]
    if not tracks:
        return []

    threshold = float(cfg["analysis"]["merge_threshold"])
    max_identities = int(cfg["analysis"]["max_identities"])
    same_box = (float(cfg["analysis"].get("same_box_containment", _SAME_BOX_CONTAINMENT)),
                float(cfg["analysis"].get("same_box_center_ratio", _SAME_BOX_CENTER_RATIO)))

    # 登場順に並べる = 人物 ID の採番を決定的にする (§8-7)
    tracks.sort(key=lambda t: (t.start_sec, t.id))

    groups = []          # [{"vector": 平均ベクトル, "tracks": [...], "weight": 秒数}]
    for track in tracks:
        vector = track.mean_embedding()
        best_index = -1
        best_gap = threshold
        if vector is not None:
            for index, group in enumerate(groups):
                if group["vector"] is None:
                    continue
                if _appears_together(track, group["tracks"], same_box):
                    continue
                gap = float(1.0 - np.dot(group["vector"], vector))
                if gap < best_gap:
                    best_gap = gap
                    best_index = index

        if best_index < 0:
            groups.append({"vector": vector, "tracks": [track],
                           "weight": max(track.duration_sec, 1e-6)})
            continue

        group = groups[best_index]
        group["tracks"].append(track)
        # 重み付き平均で群の代表ベクトルを更新する (長く映った tracklet を重く見る)
        weight = max(track.duration_sec, 1e-6)
        if vector is not None and group["vector"] is not None:
            merged = group["vector"] * group["weight"] + vector * weight
            norm = float(np.linalg.norm(merged))
            group["vector"] = merged / norm if norm > 1e-9 else group["vector"]
        group["weight"] += weight

    identities = _to_identities(groups, max_identities)
    if merges:
        identities = apply_merges(identities, merges)
    _assign(identities)

    main = identities[0]["id"] if identities else None
    _logger.info("人物クラスタリング: tracklet %d 本 → 人物 %d 人 (主役 %s)",
                 len(tracks), len(identities), main or "なし")
    return identities


# 群を identity の辞書へ直す。映っていた合計秒数の多い順に並べ、先頭を主役にする (R3)。
def _to_identities(groups, max_identities):
    identities = []
    for group in groups:
        total = sum(t.duration_sec for t in group["tracks"])
        identities.append({
            "total_sec": total,
            "vector": group["vector"],
            "tracks": list(group["tracks"]),
        })

    # 合計秒数の降順。同点なら先に登場した方を上にする (決定的にするため / §3.3)
    identities.sort(key=lambda e: (-e["total_sec"], _first_start(e["tracks"])))
    if len(identities) > max_identities:
        _logger.info("人物が %d 人を超えたため、短いものから捨てます (上限 %d)",
                     len(identities), max_identities)
        identities = identities[:max_identities]

    for index, identity in enumerate(identities):
        identity["id"] = f"p{index + 1}"
        identity["main"] = index == 0      # 一番映っている人物 = 主役 (R3)
    return identities


def _first_start(tracks):
    return min((t.start_sec for t in tracks), default=0.0)


# track と同じ素材の同じフレームに、別の位置で映っている tracklet が others にあるか。
# 1 フレームの別々の位置に写る 2 つの検出は同一人物になり得ない。
#
# ただし検出器は 1 人に「全身」と「上半身」の枠を重ねて出すことがあり、
# その 2 本は同じ時刻に映っていても同一人物である。そこで、共通する時刻の
# **過半数で枠が離れている**ときだけ「並んで映っている」とみなす。
# 枠の乗り換えの瞬間は重なりが一時的に下がるため、1 回だけでは決めない。
#   same_box: (入っている割合の下限, 横の中心のずれの上限倍率) / _same_box を参照
def _appears_together(track, others, same_box=(_SAME_BOX_CONTAINMENT, _SAME_BOX_CENTER_RATIO)):
    samples = _samples_by_time(track)
    for other in others:
        if other.media_id != track.media_id:
            continue
        other_samples = _samples_by_time(other)
        shared = samples.keys() & other_samples.keys()
        if not shared:
            continue
        apart = sum(1 for t in shared
                    if not _same_box(_rect(samples[t]), _rect(other_samples[t]), same_box))
        if apart * 2 > len(shared):
            return True
    return False


# 同じ時刻の 2 つの枠が、1 人への重複した枠 (全身と上半身など) か。
# 小さい枠がほぼ丸ごと大きい枠に入り、しかも横方向の中心がそろっているときだけ真。
def _same_box(a, b, same_box):
    min_containment, max_center_ratio = same_box
    if containment(a, b) < min_containment:
        return False
    smaller_width = min(float(a[2]), float(b[2]))
    center_gap = abs((a[0] + a[2] / 2.0) - (b[0] + b[2] / 2.0))
    return center_gap <= max_center_ratio * max(smaller_width, 1e-6)


# サンプル時刻は素材ごとに同じ刻みで取るため、ミリ秒へ丸めて突き合わせる
def _samples_by_time(track):
    return {round(float(sample["t"]), 3): sample for sample in track.samples}


def _rect(sample):
    return (sample["x"], sample["y"], sample["w"], sample["h"])


# 手で統合した組 (§5.6.6) を当て込む。統合後は合計秒数で並べ直す。
def apply_merges(identities, merges):
    by_id = {identity["id"]: identity for identity in identities}
    alias = {}

    def _root(name):
        while alias.get(name) and alias[name] != name:
            name = alias[name]
        return name

    for pair in merges or []:
        if len(pair) < 2:
            continue
        first = _root(str(pair[0]))
        for other in pair[1:]:
            second = _root(str(other))
            if first == second or first not in by_id or second not in by_id:
                continue
            target = by_id[first]
            source = by_id[second]
            target["tracks"].extend(source["tracks"])
            target["total_sec"] += source["total_sec"]
            alias[second] = first
            by_id.pop(second, None)

    merged = list(by_id.values())
    merged.sort(key=lambda e: (-e["total_sec"], _first_start(e["tracks"])))
    for index, identity in enumerate(merged):
        identity["main"] = index == 0
    return merged


# 各 tracklet へ人物 ID を書き戻す
def _assign(identities):
    for identity in identities:
        for track in identity["tracks"]:
            track.identity = identity["id"]
