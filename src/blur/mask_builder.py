# マスク動画の生成 (ver5 resolve2 §5.4)
#
# Timeline 時間のグレースケール動画を 1 本作る。白い所だけがぼける (§3.5 案 2)。
# 対象が 1 つも無ければ **マスクを作らない** (= フィルタを 1 文字も足さない / R9)。
#
# 手順:
#   1. TimeMap を作り、出力フレームごとに「どの素材の何秒か」を引く
#   2. その時刻に生きているトラックのうち、ぼかすと決まったものを集める
#   3. samples を線形補間して矩形を求め、margin_ratio ぶん広げる
#   4. geometry で素材ピクセル → キャンバスピクセルへ直す
#   5. PIL で白く塗り、feather ぶんぼかす → 合成時に自然に溶ける
#   6. PyAV で可逆コーデック (ffv1/.mkv) へ書き出す。無ければ FFmpeg へパイプする
#
# マスクは mask_scale (既定 0.25) に縮小して作る。ぼかしのマスクに原寸の精度は
# 要らないため、生成時間とサイズを 1/16 にできる。フィルタ側の scale で戻す。
import os
import subprocess

import numpy as np

from ..modules import ffmpeg_runner
from ..timeline.timemap import TimeMap
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from . import contour, geometry, store
from . import decisions as decisions_module
from .plan import ROLE_BLUR, ROLE_KEEP, BlurPlan, sample_rect_at

_logger = get_logger(__name__)

# PIL / PyAV は任意依存として扱う (無ければマスクを作らない = 従来どおりの出力)
try:
    from PIL import Image, ImageDraw, ImageFilter
    _PIL_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PIL_AVAILABLE = False

try:
    import av
    _PYAV_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PYAV_AVAILABLE = False


# renderer から呼ぶ入口。設定・キャッシュ・指定を読み、必要なときだけ build へ回す。
# 機能 OFF / ぼかす対象なし (decisions.needs_mask) / キャッシュ不一致のいずれでも None を返す (= 従来どおりの出力)。
def prepare(timeline, context, cfg=None):
    from .config import config, is_enabled     # noqa: PLC0415 (機能 OFF なら読まない)

    settings = context.settings
    if not is_enabled(settings):
        return None                             # 機能 OFF: 1 行も通らない (R9)

    cfg = cfg or config(settings)
    decisions = decisions_module.load(timeline)
    if not decisions_module.needs_mask(decisions, cfg):
        _logger.info("ぼかす対象の指定が無いためマスクを作りません")
        return None

    cache_path = _cache_path(timeline, context, decisions)
    analysis = store.load(cache_path, decisions.get("fingerprint") or None)
    if analysis is None:
        # 解析結果が無い = ぼかせない。**黙って素で出さない**ため呼び出し側へ知らせる (§5.9)
        _logger.error("ぼかしの解析結果が見つからないためマスクを作れません: %s", cache_path)
        context.blur_mask_failed = True
        return None

    out_path = context.allocate_intermediate("blur_mask.mkv")
    try:
        return build(timeline, analysis, decisions, cfg, out_path,
                     on_progress=context.progress_subcallback("ぼかしマスク生成"))
    except Exception as error:                  # noqa: BLE001 (出力自体は止めない / §5.9)
        _logger.exception("ぼかしマスクの生成に失敗しました: %s", error)
        context.blur_mask_failed = True
        return None


# 解析結果の置き場を決める。指定に書かれていればそれを優先する。
#
# 探す順は次のとおり。
#   1. cache      … プロジェクトからの相対 (プロジェクトごと移しても効く / §5.2.2)
#   2. cache_abs  … 解析したときの絶対パス。**アーカイブ用のサブ Timeline で効く**。
#                   サブ Timeline を書き出す PipelineContext は project_path を持たず、
#                   相対パスの起点が無いため、これが無いと解析結果を見つけられない。
#   3. 既定の置き場 (プロジェクトの隣 / working_dir)
def _cache_path(timeline, context, decisions):
    project_path = getattr(context, "project_path", None)
    for candidate in _cache_candidates(decisions, project_path, context.working_dir):
        if candidate and os.path.isfile(candidate):
            return candidate
    return store.cache_path_for(project_path, context.working_dir)


def _cache_candidates(decisions, project_path, working_dir):
    cached = decisions.get("cache")
    if cached:
        if os.path.isabs(cached):
            yield cached
        else:
            base = os.path.dirname(project_path) if project_path else working_dir
            yield os.path.join(base, cached)
    if decisions.get("cache_abs"):
        yield decisions["cache_abs"]
    yield store.cache_path_for(project_path, working_dir)


# Timeline・解析結果・指定から、Timeline 時間のマスク動画を 1 本作る。
# ぼかす対象が 1 つも無ければ None を返す (= フィルタを足さない)。
def build(timeline, analysis, decisions, cfg, out_path, on_progress=None):
    if not _PIL_AVAILABLE:
        _logger.error("PIL が使えないためぼかしマスクを作れません")
        return None

    plan = BlurPlan(timeline, analysis, decisions, cfg)
    if not plan.has_blur():
        _logger.info("ぼかす対象が無いためマスクを作りません")
        return None

    fps = int(timeline.fps or 60)
    duration = float(timeline.duration_sec())
    total_frames = max(int(round(duration * fps)), 1)
    width, height = mask_size(timeline, cfg)

    _logger.info("ぼかしマスク: %d フレーム / %dx%d / ぼかす %d 件 / 守る %d 件",
                 total_frames, width, height,
                 len(plan.tracks_with_role(ROLE_BLUR)), len(plan.tracks_with_role(ROLE_KEEP)))

    painter = _MaskPainter(plan, timeline, cfg, width, height)
    frames = _iter_mask_frames(painter, timeline, total_frames, fps, on_progress)
    if _PYAV_AVAILABLE:
        result = _encode_pyav(frames, out_path, fps, width, height)
    else:
        result = _encode_ffmpeg(frames, out_path, fps, width, height, cfg)
    painter.log_summary()
    return result


# 1 フレームぶんの最終マスクだけを作る (指定画面の「仕上がり表示」用 / resolve3 §5.5.4)。
# 書き出しと同じ計算を通すため、表示と出力が食い違わない。
# 戻り値: グレースケールの PIL Image (mask_size の大きさ)。PIL が無ければ None。
def frame_mask(timeline, analysis, decisions, cfg, timeline_sec):
    if not _PIL_AVAILABLE:
        return None
    width, height = mask_size(timeline, cfg)
    plan = BlurPlan(timeline, analysis, decisions, cfg)
    painter = _MaskPainter(plan, timeline, cfg, width, height)
    located = TimeMap.from_timeline(timeline).to_source(float(timeline_sec))
    if located is None:
        return painter.blank
    return painter.paint(*located)


# マスクの大きさ (キャンバスに mask_scale を掛け、偶数へそろえる)
def mask_size(timeline, cfg):
    scale = float(cfg["render"]["mask_scale"])
    width = max(int(timeline.width * scale) // 2 * 2, 2)
    height = max(int(timeline.height * scale) // 2 * 2, 2)
    return width, height


# ------------------------------------------------------------------
# どこをぼかすかの計画
# ------------------------------------------------------------------

# ぼかす対象のトラックを集める (互換のために残す。判定の本体は plan.BlurPlan)。
# 戻り値 {"shapes": [{"media_id","kind","samples","start","end"}, …]}
def build_plan(timeline, analysis, decisions, cfg):
    plan = BlurPlan(timeline, analysis, decisions, cfg)
    pad = float(cfg["render"]["pad_sec"])
    shapes = []
    for entry in plan.tracks_with_role(ROLE_BLUR):
        samples = [s for s in entry["track"].get("samples", []) if _valid_sample(s)]
        if not samples:
            continue
        shapes.append({
            "media_id": str(entry["track"].get("media_id") or ""),
            "kind": entry["kind"],
            "samples": samples,
            # 取りこぼし対策として前後へ pad_sec ぶん伸ばす (§5.4 補足)
            "start": float(samples[0]["t"]) - pad,
            "end": float(samples[-1]["t"]) + pad,
        })
    return {"shapes": shapes}


def _valid_sample(sample):
    try:
        return float(sample["w"]) > 0 and float(sample["h"]) > 0
    except (KeyError, TypeError, ValueError):
        return False


# ------------------------------------------------------------------
# 1 フレームぶんの絵 (ver5 resolve3 §3.4 / §5.4.2)
# ------------------------------------------------------------------

# 出力フレームを 1 枚ずつ作って返す (グレースケールの PIL Image)
def _iter_mask_frames(painter, timeline, total_frames, fps, on_progress):
    timemap = TimeMap.from_timeline(timeline)
    for index in range(total_frames):
        located = timemap.to_source(index / float(fps))
        image = painter.blank if located is None else painter.paint(*located)
        if on_progress is not None and (index % 30 == 0 or index == total_frames - 1):
            on_progress((index + 1) / float(total_frames), {})
        yield image


# 1 フレームのマスクを描く。
#
#   ぼかす層   B = ぼかす形を塗る → (輪郭で塗った分だけ) 膨張 → フェザー
#   ぼかさない層 K = 守る形を塗る (膨張もフェザーもしない。keep_margin_ratio があれば膨張)
#   最終マスク   M = B × (1 − K)
#
# フェザーは B にだけ掛け、K で削った後には掛けない。後からぼかすと、削った境界から
# ぼかしが守る人物へ染み出すため (「ぼかさない」を優先する / resolve3 §3.4)。
class _MaskPainter:

    def __init__(self, plan, timeline, cfg, width, height):
        self._plan = plan
        self._timeline = timeline
        self._width = width
        self._height = height
        self._scale_x = width / float(timeline.width)
        self._scale_y = height / float(timeline.height)
        self._feather = max(float(cfg["render"]["feather_ratio"]) * width, 0.0)
        self._margin_ratio = float(cfg["render"]["margin_ratio"])
        self._shape_kind = str(cfg["render"]["shape"])
        # 膨らませる量 (マスクの px)。輪郭 = 動きの遅れの吸収 / 守る形 = keep_margin_ratio
        self._dilate_px = max(float(cfg["silhouette"]["dilate_ratio"]) * width, 0.0)
        self._keep_dilate_px = max(float(cfg["render"]["keep_margin_ratio"]) * width, 0.0)
        # 何も描かないフレームは同じ絵を使い回す (作り直さない)。
        # 出力の大半は「ぼかす対象が映っていない」フレームのため、ここが効く。
        self.blank = Image.new("L", (width, height), 0)
        self._counts = {"silhouette": 0, "fallback": 0}

    # 素材のその時刻のマスクを返す
    def paint(self, media_id, source_sec):
        shapes = self._plan.shapes_at(media_id, source_sec)
        blur_shapes = [s for s in shapes if s["role"] == ROLE_BLUR]
        if not blur_shapes:
            return self.blank

        blur_layer = Image.new("L", (self._width, self._height), 0)
        draw = ImageDraw.Draw(blur_layer)
        for shape in blur_shapes:
            if shape["silhouette"] is not None:
                # 輪郭は人物の大きさと動きに応じた余白で膨らませる (resolve3 §3.3.3)
                self._fill(draw, shape, expand=False,
                           grow_px=max(self._dilate_px, shape.get("grow", 0.0) * self._scale_x))
                self._counts["silhouette"] += 1
            else:
                # 矩形系は margin_ratio で広げたうえで、動いた量だけさらに広げる
                self._fill(draw, shape, expand=True,
                           grow_px=shape.get("motion", 0.0) * self._scale_x)
                if shape["kind"] == "person" and self._shape_kind == "silhouette":
                    self._counts["fallback"] += 1

        if self._feather > 0.5:
            # 境界をぼかす = alphamerge したときに自然に溶ける (§3.5)
            blur_layer = blur_layer.filter(ImageFilter.GaussianBlur(self._feather))

        keep_shapes = [s for s in shapes if s["role"] == ROLE_KEEP]
        if not keep_shapes:
            return blur_layer

        keep_layer = Image.new("L", (self._width, self._height), 0)
        keep_draw = ImageDraw.Draw(keep_layer)
        for shape in keep_shapes:
            # 守る形は keep_margin_ratio に加え、激しく動いた時刻だけ動いた量ぶん広げる (plan の grow)
            self._fill(keep_draw, shape, expand=False,
                       grow_px=self._keep_dilate_px + shape.get("grow", 0.0) * self._scale_x)
        # M = B × (1 − K)
        blurred = np.asarray(blur_layer, dtype=np.uint16)
        keep = np.asarray(keep_layer, dtype=np.uint16)
        result = (blurred * (255 - keep) // 255).astype(np.uint8)
        return Image.fromarray(result, mode="L")

    # 1 つの形を白く塗る。
    #   expand  : 枠を margin_ratio ぶん広げるか (ぼかす矩形系だけ広げる。守る形は広げない)
    #   grow_px : 形をさらに外側へ膨らませる量 (マスクの px)。
    #             画像全体に MaxFilter を掛けると 1 枚 20ms、角を丸めた太線でも 1 枚 7ms かかる (実測) ため、
    #             多角形は頂点を外側へずらした多角形を塗り、矩形系は枠を広げて同じ効果を出す
    def _fill(self, draw, shape, expand, grow_px=0.0):
        rect = shape["rect"]
        if expand:
            rect = geometry.expand_rect(rect, self._margin_ratio, self._timeline.width)
        rect = geometry.clamp_rect(rect, self._timeline.width, self._timeline.height)
        if rect is None:
            return

        relative = shape["silhouette"] if shape["silhouette"] is not None else shape["outline"]
        if relative is not None:
            # 輪郭は広げていない枠へ、手描きの形は (ぼかすなら) 広げた枠へ当てはめる
            base = shape["rect"] if shape["silhouette"] is not None else rect
            points = [(px * self._scale_x, py * self._scale_y)
                      for px, py in contour.to_absolute(relative, base)]
            if len(points) >= 3:
                draw.polygon(points, fill=255)
                if grow_px >= 0.5:
                    draw.polygon([tuple(p) for p in contour.offset(points, grow_px)], fill=255)
                return

        x, y, width, height = rect
        grow = grow_px if grow_px >= 0.5 else 0.0
        box = (x * self._scale_x - grow, y * self._scale_y - grow,
               (x + width) * self._scale_x + grow, (y + height) * self._scale_y + grow)
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            return
        if shape["kind"] != "person" or self._shape_kind == "rect":
            draw.rectangle(box, fill=255)
        elif self._shape_kind == "ellipse":
            draw.ellipse(box, fill=255)
        else:
            # rounded / silhouette (輪郭が無い時刻) は角丸
            radius = min(box[2] - box[0], box[3] - box[1]) * 0.2
            draw.rounded_rectangle(box, radius=radius, fill=255)

    # 輪郭で塗れた割合をログへ出す (resolve3 §5.12)
    def log_summary(self):
        total = self._counts["silhouette"] + self._counts["fallback"]
        if total and self._shape_kind == "silhouette":
            _logger.info("ぼかしマスク: 人物の輪郭 %d%% (残りは四角)",
                         int(round(100.0 * self._counts["silhouette"] / total)))


# その時刻の矩形を線形補間で求める (互換のために残す。本体は plan.sample_rect_at)
def _rect_at(shape, source_sec):
    return sample_rect_at(shape["samples"], source_sec)


# ------------------------------------------------------------------
# 書き出し
# ------------------------------------------------------------------

# PyAV で可逆コーデック (ffv1) のグレースケール動画にする (§3.5 案 C)
# ほぼ真っ黒の絵が続くため数 MB 以下に収まる。
def _encode_pyav(frames, out_path, fps, width, height):
    container = av.open(out_path, mode="w")
    try:
        stream = container.add_stream("ffv1", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "gray"
        for image in frames:
            # gray の ndarray から直に作る。from_image は一度 RGB へ起こしてから
            # swscale で gray へ戻すため、1 フレームあたりの費用が数倍になる。
            frame = av.VideoFrame.from_ndarray(np.asarray(image, dtype=np.uint8),
                                               format="gray")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()

    _logger.info("ぼかしマスクを書き出しました: %s (%.1f MB)",
                 os.path.basename(out_path), _size_mb(out_path))
    return out_path


# FFmpeg へ rawvideo をパイプして書き出す (PyAV が無い環境)
def _encode_ffmpeg(frames, out_path, fps, width, height, cfg):
    ffmpeg_cfg = {}
    command = [
        ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}", "-r", str(fps),
        "-i", "-",
        "-c:v", "ffv1", "-pix_fmt", "gray",
        out_path,
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=no_window_creationflags())
    try:
        for image in frames:
            process.stdin.write(image.convert("L").tobytes())
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.wait()

    if process.returncode != 0 or not os.path.exists(out_path):
        _logger.error("ぼかしマスクの書き出しに失敗しました (FFmpeg 経路)")
        return None
    _logger.info("ぼかしマスクを書き出しました: %s (%.1f MB)",
                 os.path.basename(out_path), _size_mb(out_path))
    return out_path


def _size_mb(path):
    try:
        return os.path.getsize(path) / 1024.0 / 1024.0
    except OSError:
        return 0.0
