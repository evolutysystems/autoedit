# 「どの時刻に、何を、ぼかす / 外すか」を 1 か所で決める (ver5 resolve8 §5.4 / §5.7)
#
# 指定画面・Timeline のプレビューの目印・マスク生成は、すべてここから同じ結果を引く。
# 判定を 1 か所に集めておかないと「画面にだけ残る枠」のような食い違いが起きる。
#
# ver5 resolve8 の決めごと。
#   ・指定の相手は「画面全体」と「囲み」だけ。人物の枠・アンカー・名前表は無い。
#   ・囲みの位置は **キーフレームが正**。追従はその間を埋めるだけ (§3.3)。
#       大きさ … キーフレームの線形補間 (追従は大きさを変えない)
#       位置   … 追従 + 誤差の線形配分。追従が無ければキーフレームの線形補間
#   ・指定は **並び順のまま重ねる**。後から足したものが上に来る。
#   ・指定は span (クリップの素材区間) の外では 1 フレームも効かない。
from ..utils.logger import get_logger
from . import contour
from . import decisions as decisions_module
from .geometry import normalized_rect_to_canvas

_logger = get_logger(__name__)

ROLE_BLUR = decisions_module.BLUR
ROLE_KEEP = decisions_module.KEEP

# 追従の具合 (tracker と同じ値。循環 import を避けて直接持つ)
STATUS_OK = "ok"
STATUS_LOST = "lost"
STATUS_FAILED = "failed"


class BlurPlan:

    #   timeline  : 対象の Timeline (座標変換に使う)
    #   tracks    : 追従結果 (store.load の戻り値 / 無くてもよい)
    #   decisions : decisions.load(timeline) の戻り値
    #   cfg       : config.config() の戻り値
    def __init__(self, timeline, tracks, decisions, cfg):
        self._timeline = timeline
        self._tracks = (tracks or {}).get("tracks") or {}
        self._decisions = decisions or decisions_module._empty()   # noqa: SLF001 (同じ層の道具)
        self._cfg = cfg
        self._feather = float(cfg["render"]["feather_ratio"]) * float(timeline.width)
        # 同じキーフレームとみなす時刻の差 = 半コマ (画面が渡す時刻の丸め誤差を吸収する)
        self._epsilon = 0.5 / max(int(getattr(timeline, "fps", 60) or 60), 1)
        self.specs = [spec for spec in self._decisions.get("specs", []) if isinstance(spec, dict)]
        self._labels = None

    # ------------------------------------------------------------------
    # 公開 — 指定 (マスク生成・プレビューが使う)
    # ------------------------------------------------------------------

    # その時刻に効いている指定を、**重ね順のまま**返す。
    # 戻り値: [{"spec", "mode", "shape"}, …] 先頭が一番下。
    #   shape … {"kind","rect","outline","feather","label"}
    #           kind が "frame" の形は画面全体を指す (rect はキャンバス全体)
    def layers_at(self, media_id, source_sec):
        layers = []
        for spec in self.specs:
            if not decisions_module.span_contains(spec, media_id, source_sec):
                continue
            shape = self._shape_of(spec, float(source_sec))
            if shape is None:
                continue                # 追従が切れていて位置が決まらない
            layers.append({"spec": spec, "mode": spec.get("mode"), "shape": shape})
        return layers

    # マスクが要るか (「ボカす」指定が 1 つでもあるか)。
    # 無ければマスク動画を作らず、FFmpeg に 1 行も足さない (R14)。
    def has_blur(self):
        return any(spec.get("mode") == decisions_module.BLUR for spec in self.specs)

    # ------------------------------------------------------------------
    # 公開 — 画面が使うもの
    # ------------------------------------------------------------------

    # そのクリップに効く指定 (並び順のまま)
    def specs_in_clip(self, clip):
        return decisions_module.specs_in_clip(self._decisions, clip)

    # 指定 ID から指定を引く
    def spec_by_id(self, spec_id):
        for spec in self.specs:
            if str(spec.get("id")) == str(spec_id):
                return spec
        return None

    # その時刻の矩形 (正規化キャンバス座標)。位置が決まらなければ None。
    def rect_at(self, spec, source_sec):
        if spec.get("kind") == decisions_module.KIND_FRAME:
            return (0.0, 0.0, 1.0, 1.0)
        if not decisions_module.span_contains(spec, spec.get("media_id"), source_sec):
            return None
        keys = decisions_module.keys_of(spec)
        if not keys:
            return None
        # キーフレームの時刻は、位置も大きさもキーそのもの (人の指示が追従より強い / §4-7)
        key = _key_near(keys, float(source_sec), self._epsilon)
        if key is not None:
            return tuple(float(v) for v in key["rect"])

        width, height = _size_at(keys, float(source_sec))
        center = self._center_at(spec, keys, float(source_sec))
        if center is None:
            return None
        return (center[0] - width / 2.0, center[1] - height / 2.0, width, height)

    # その時刻の矩形 (キャンバス px)。位置が決まらなければ None。
    def canvas_rect_at(self, spec, source_sec):
        rect = self.rect_at(spec, source_sec)
        if rect is None:
            return None
        return normalized_rect_to_canvas(rect, self._timeline.width, self._timeline.height)

    # 指定の表示名 (付けた名前 / 「ボカす 1」/「画面全体をぼかす」)
    def label_of(self, spec):
        if spec.get("label"):
            return str(spec["label"])
        return self.default_label(spec)

    # 名前を付けていない指定の呼び名。入力がこれと同じなら名前なし扱いにする。
    def default_label(self, spec):
        if spec.get("kind") == decisions_module.KIND_FRAME:
            return "画面全体をぼかす"
        if self._labels is None:
            self._labels = {}
            number = 0
            for other in self.specs:
                if other.get("kind") == decisions_module.KIND_FRAME:
                    continue
                number += 1
                self._labels[str(other.get("id"))] = (
                    f"{'ボカす' if other.get('mode') == decisions_module.BLUR else 'ボカさない'}"
                    f" {number}")
        return self._labels.get(str(spec.get("id")), "囲み")

    # 指定 1 件の説明 ("ボカさない / 店員 / キー 4 点")
    def describe(self, spec):
        if spec.get("kind") == decisions_module.KIND_FRAME:
            return "画面全体をぼかす"
        mode = "ボカす" if spec.get("mode") == decisions_module.BLUR else "ボカさない"
        keys = len(decisions_module.keys_of(spec))
        follow = "" if spec.get("follow", decisions_module.FOLLOW_TRACK) == \
            decisions_module.FOLLOW_TRACK else " / 動かさない"
        return f"{mode} / {self.label_of(spec)} / キー {keys} 点{follow}"

    # その指定の追従の具合 {"status","lost_sec"}。追わない指定は status=None。
    def status_of(self, spec):
        if spec.get("kind") != decisions_module.KIND_AREA:
            return {"status": None, "lost_sec": None}
        if spec.get("follow", decisions_module.FOLLOW_TRACK) != decisions_module.FOLLOW_TRACK:
            return {"status": None, "lost_sec": None}
        segments = self._segments_of(spec)
        if not segments:
            return {"status": None, "lost_sec": None}
        for segment in segments:
            if str(segment.get("status")) == STATUS_FAILED:
                return {"status": STATUS_FAILED, "lost_sec": None}
        for segment in segments:
            if str(segment.get("status")) == STATUS_LOST:
                return {"status": STATUS_LOST, "lost_sec": segment.get("lost_sec")}
        return {"status": STATUS_OK, "lost_sec": None}

    # ------------------------------------------------------------------
    # 内部 — 指定 1 件ぶんの形
    # ------------------------------------------------------------------

    def _shape_of(self, spec, source_sec):
        if spec.get("kind") == decisions_module.KIND_FRAME:
            # 画面全体。追従が無くても必ず作れる = ぼかしは必ず掛かる (安全側)
            return {
                "kind": decisions_module.KIND_FRAME,
                "rect": (0.0, 0.0, float(self._timeline.width), float(self._timeline.height)),
                "outline": None, "feather": 0.0, "label": self.label_of(spec),
            }

        rect = self.canvas_rect_at(spec, source_sec)
        if rect is None:
            return None
        return {
            "kind": decisions_module.KIND_AREA,
            "rect": rect,
            # v4 以前から引き継いだ自由な囲みだけが形を持つ (resolve8 §3.6)
            "outline": contour.decode(spec.get("outline")),
            "feather": self._feather,
            "label": self.label_of(spec),
        }

    # ------------------------------------------------------------------
    # 内部 — 位置 (resolve8 §3.3 / §5.4)
    # ------------------------------------------------------------------

    # その時刻の中心 (正規化キャンバス座標)。位置が決まらなければ None。
    def _center_at(self, spec, keys, source_sec):
        before, after = _around(keys, source_sec)
        segment = self._segment_for(spec, source_sec)
        samples = (segment or {}).get("samples") or []
        if not samples:
            # 追従が無い (固定 / 追えなかった / まだ追っていない) = キーフレームの補間だけ
            return _interpolate_centers(before, after, source_sec)

        first, last = samples[0], samples[-1]
        if source_sec < float(first["t"]) - 1e-6:
            # 後ろ向きの区切りで、追えた範囲より前 (途中で見失った)
            return self._beyond(spec, (float(first["cx"]), float(first["cy"])),
                                before, source_sec, float(first["t"]))
        if source_sec > float(last["t"]) + 1e-6:
            # 前向きの区切りで、追えた範囲より後ろ (途中で見失った)
            return self._beyond(spec, (float(last["cx"]), float(last["cy"])),
                                after, source_sec, float(last["t"]))

        tracked = _sample_center_at(samples, source_sec)
        if tracked is None:
            return _interpolate_centers(before, after, source_sec)

        # 前向きの区切りで反対の端にキーフレームがあるなら、追従がためた誤差を
        # 区切り全体へ配ってならす (次のキーフレームで飛ばないように / §3.3)
        if (segment.get("dir") == "fwd" and after is not None
                and abs(float(segment.get("end", 0.0)) - float(after["t"])) < 1e-3):
            target = _center_of(after["rect"])
            span = float(last["t"]) - float(segment.get("start", 0.0))
            ratio = ((source_sec - float(segment.get("start", 0.0))) / span
                     if span > 1e-9 else 1.0)
            ratio = min(max(ratio, 0.0), 1.0)
            return (tracked[0] + (target[0] - float(last["cx"])) * ratio,
                    tracked[1] + (target[1] - float(last["cy"])) * ratio)
        return tracked

    # 追えた範囲の外側の時刻をどう扱うか (resolve8 §3.3 / §4-8)。
    #   反対の端にキーフレームがある … そこへ向かって線形で結ぶ
    #   無い + ボカす               … 最後に追えた位置を保持する (素で出すより安全)
    #   無い + ボカさない           … 位置を返さない = 穴が閉じる (ぼかしたまま = 安全)
    def _beyond(self, spec, edge, target_key, source_sec, edge_sec):
        if target_key is not None:
            target = _center_of(target_key["rect"])
            span = abs(edge_sec - float(target_key["t"]))
            if span <= 1e-9:
                return target
            ratio = min(max(abs(source_sec - edge_sec) / span, 0.0), 1.0)
            return (edge[0] + (target[0] - edge[0]) * ratio,
                    edge[1] + (target[1] - edge[1]) * ratio)
        if spec.get("mode") == decisions_module.BLUR:
            return edge
        return None

    # その時刻を含む区切りの追従結果 (無ければ None)。
    # 区切りは span をちょうど分けているため、含むものを 1 つ選べばよい。
    def _segment_for(self, spec, source_sec):
        for segment in self._segments_of(spec):
            if (float(segment.get("start", 0.0)) - 1e-3 <= source_sec
                    <= float(segment.get("end", 0.0)) + 1e-3):
                return segment
        return None

    def _segments_of(self, spec):
        if spec.get("follow", decisions_module.FOLLOW_TRACK) != decisions_module.FOLLOW_TRACK:
            return []                   # 「動かさない」指定は追従結果を使わない
        entry = self._tracks.get(str(spec.get("id")))
        if not isinstance(entry, dict):
            return []
        return [s for s in entry.get("segments") or [] if isinstance(s, dict)]


# ------------------------------------------------------------------
# キーフレームの補間
# ------------------------------------------------------------------

# その時刻とほぼ同じキーフレーム (無ければ None)
def _key_near(keys, source_sec, epsilon):
    for key in keys:
        if abs(float(key["t"]) - source_sec) <= epsilon:
            return key
    return None


# その時刻を挟むキーフレーム (前, 後)。外側なら片方が None。
def _around(keys, source_sec):
    before = None
    after = None
    for key in keys:
        if float(key["t"]) <= source_sec + 1e-9:
            before = key
        else:
            after = key
            break
    return before, after


# 大きさ (幅, 高さ) をキーフレームの線形補間で求める
def _size_at(keys, source_sec):
    before, after = _around(keys, source_sec)
    if before is None:
        return float(after["rect"][2]), float(after["rect"][3])
    if after is None:
        return float(before["rect"][2]), float(before["rect"][3])
    span = float(after["t"]) - float(before["t"])
    ratio = (source_sec - float(before["t"])) / span if span > 1e-9 else 0.0
    ratio = min(max(ratio, 0.0), 1.0)
    return (
        float(before["rect"][2]) + (float(after["rect"][2]) - float(before["rect"][2])) * ratio,
        float(before["rect"][3]) + (float(after["rect"][3]) - float(before["rect"][3])) * ratio,
    )


# 中心をキーフレームの線形補間で求める (追従が無いとき)
def _interpolate_centers(before, after, source_sec):
    if before is None and after is None:
        return None
    if before is None:
        return _center_of(after["rect"])
    if after is None:
        return _center_of(before["rect"])
    start = _center_of(before["rect"])
    end = _center_of(after["rect"])
    span = float(after["t"]) - float(before["t"])
    ratio = (source_sec - float(before["t"])) / span if span > 1e-9 else 0.0
    ratio = min(max(ratio, 0.0), 1.0)
    return (start[0] + (end[0] - start[0]) * ratio, start[1] + (end[1] - start[1]) * ratio)


# 矩形の中心
def _center_of(rect):
    x, y, width, height = (float(v) for v in rect)
    return (x + width / 2.0, y + height / 2.0)


# 追従サンプルからその時刻の中心を線形補間で求める
def _sample_center_at(samples, source_sec):
    if not samples:
        return None
    if source_sec <= float(samples[0]["t"]):
        return (float(samples[0]["cx"]), float(samples[0]["cy"]))
    if source_sec >= float(samples[-1]["t"]):
        return (float(samples[-1]["cx"]), float(samples[-1]["cy"]))
    previous = samples[0]
    for sample in samples:
        if float(sample["t"]) >= source_sec:
            span = float(sample["t"]) - float(previous["t"])
            ratio = (source_sec - float(previous["t"])) / span if span > 1e-9 else 0.0
            return (
                float(previous["cx"]) + (float(sample["cx"]) - float(previous["cx"])) * ratio,
                float(previous["cy"]) + (float(sample["cy"]) - float(previous["cy"])) * ratio,
            )
        previous = sample
    return (float(samples[-1]["cx"]), float(samples[-1]["cy"]))
