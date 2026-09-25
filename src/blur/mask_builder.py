# マスク動画の生成 (ver5 resolve8 §5.8)
#
# Timeline 時間のグレースケール動画を 1 本作る。白い所だけがぼける。
# 「ボカす」指定が 1 つも無ければ **マスクを作らない** (= フィルタを 1 文字も足さない / R14)。
#
# 手順:
#   1. TimeMap を作り、出力フレームごとに「どの素材の何秒か」を引く
#   2. その時刻に効いている指定を **重ね順のまま**集める (resolve8 §3.2)
#   3. 指定ごとに形を作り、フェザーして α にする
#   4. 黒地の上へ順に合成する (ボカす = 255 / ボカさない = 0)
#   5. PyAV で可逆コーデック (ffv1/.mkv) へ書き出す。無ければ FFmpeg へパイプする
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
from .plan import ROLE_BLUR, BlurPlan

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
# 機能 OFF / ぼかす対象なしのいずれでも None を返す (= 従来どおりの出力)。
def prepare(timeline, context, cfg=None):
    from .config import config, is_enabled     # noqa: PLC0415 (機能 OFF なら読まない)

    settings = context.settings
    if not is_enabled(settings):
        return None                             # 機能 OFF: 1 行も通らない (R14)

    cfg = cfg or config(settings)
    decisions = decisions_module.load(timeline)
    if not decisions_module.needs_mask(decisions):
        _logger.info("ぼかす指定が無いためマスクを作りません")
        return None

    cache_path = _cache_path(timeline, context, decisions)
    tracks = _tracks_for(timeline, context, decisions, cfg, cache_path)

    out_path = context.allocate_intermediate("blur_mask.mkv")
    try:
        return build(timeline, tracks, decisions, cfg, out_path,
                     on_progress=context.progress_subcallback("ぼかしマスク生成"),
                     context=context)
    except Exception as error:                  # noqa: BLE001 (出力自体は止めない)
        _logger.exception("ぼかしマスクの生成に失敗しました: %s", error)
        context.blur_mask_failed = True
        return None


# 書き出しに使う追従結果を用意する (ver5 resolve8 §5.8)。
#
# 指定画面で追い終えていれば、足りない区切りは無いのでここでは何も走らない。
# 足りなければ **ここで追う**。追えなくてもキーフレームの位置は塗れるため、
# ぼかしそのものは必ず掛かる (安全側)。
def _tracks_for(timeline, context, decisions, cfg, cache_path):
    tracks = store.load(cache_path, store.settings_key(cfg, timeline))
    if not decisions_module.needs_tracking(decisions):
        return tracks                           # 全面ぼかし・固定の囲みだけなら追従は要らない

    media_ids = decisions_module.media_ids(decisions)
    if tracks is None:
        tracks = store.new_tracks(timeline, cfg, media_ids)
    else:
        store.drop_stale_media(tracks, timeline)
        store.merge_media(tracks, timeline, media_ids)

    report = context.progress_subcallback("ぼかしの追従")
    try:
        from . import tracker                   # noqa: PLC0415 (必要なときだけ読む)

        added = tracker.ensure_tracks(
            timeline, context.settings, tracks, decisions, cfg,
            on_progress=lambda ratio: report(ratio, {}))
    except Exception as error:                  # noqa: BLE001 (出力自体は止めない)
        _logger.exception("ぼかしの追従に失敗しました: %s", error)
        added = 0
    if added and cache_path:
        store.save(cache_path, tracks)
    return tracks


# 「ボカす」指定のうち、1 フレームも塗れないものの件数。
# 塗れない = 黙って素で出ることになるため、利用者へ知らせる (resolve8 §4-6)。
# 「ボカさない」が効かないだけならぼけたまま出るので知らせない (安全側)。
def _unpaintable_blur_specs(plan, decisions):
    count = 0
    for spec in (decisions or {}).get("specs", []):
        if spec.get("mode") != decisions_module.BLUR:
            continue
        if spec.get("kind") == decisions_module.KIND_FRAME:
            continue                            # 画面全体は必ず塗れる
        span = spec.get("span") or {}
        middle = (float(span.get("start", 0.0)) + float(span.get("end", 0.0))) / 2.0
        if plan.rect_at(spec, middle) is None and not decisions_module.keys_of(spec):
            count += 1
    return count


# 追従結果の置き場を決める。指定に書かれていればそれを優先する。
#
# 探す順は次のとおり。
#   1. cache      … プロジェクトからの相対 (プロジェクトごと移しても効く)
#   2. cache_abs  … 追ったときの絶対パス。**アーカイブ用のサブ Timeline で効く**。
#                   サブ Timeline を書き出す PipelineContext は project_path を持たず、
#                   相対パスの起点が無いため、これが無いと追従結果を見つけられない。
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


# Timeline・追従結果・指定から、Timeline 時間のマスク動画を 1 本作る。
# ぼかす対象が 1 つも無ければ None を返す (= フィルタを足さない)。
def build(timeline, tracks, decisions, cfg, out_path, on_progress=None, context=None):
    if not _PIL_AVAILABLE:
        _logger.error("PIL が使えないためぼかしマスクを作れません")
        return None

    plan = BlurPlan(timeline, tracks, decisions, cfg)
    if not plan.has_blur():
        _logger.info("ぼかす対象が無いためマスクを作りません")
        return None

    missing = _unpaintable_blur_specs(plan, decisions)
    if missing:
        _logger.error("ぼかし: 位置が決まらないため、ボカす指定 %d 件が効きません", missing)
        if context is not None:
            context.blur_mask_failed = True

    fps = int(timeline.fps or 60)
    duration = float(timeline.duration_sec())
    total_frames = max(int(round(duration * fps)), 1)
    width, height = mask_size(timeline, cfg)

    blur_specs = sum(1 for s in plan.specs if s.get("mode") == decisions_module.BLUR)
    _logger.info("ぼかしマスク: %d フレーム / %dx%d / 指定 %d 件 (ボカす %d / ボカさない %d)",
                 total_frames, width, height, len(plan.specs),
                 blur_specs, len(plan.specs) - blur_specs)

    painter = _MaskPainter(plan, timeline, cfg, width, height)
    frames = _iter_mask_frames(painter, timeline, total_frames, fps, on_progress)
    if _PYAV_AVAILABLE:
        return _encode_pyav(frames, out_path, fps, width, height)
    return _encode_ffmpeg(frames, out_path, fps, width, height, cfg)


# 1 フレームぶんの最終マスクだけを作る (指定画面の表示用)。
# 書き出しと同じ計算を通すため、表示と出力が食い違わない。
# 戻り値: グレースケールの PIL Image (mask_size の大きさ)。PIL が無ければ None。
def frame_mask(timeline, tracks, decisions, cfg, timeline_sec):
    if not _PIL_AVAILABLE:
        return None
    width, height = mask_size(timeline, cfg)
    plan = BlurPlan(timeline, tracks, decisions, cfg)
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
# 1 フレームぶんの絵
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


# 1 フレームのマスクを描く (ver5 resolve8 §3.2 / §5.8)。
#
#   M = 0 (何も効いていなければ真っ黒 = ぼかさない)
#   効いている指定を重ね順に 1 つずつ合成する:
#       α = 形を塗る → feather ぶんフェザー   (0〜255)
#       v = 255 (ボカす) / 0 (ボカさない)
#       M = M×(1−α/255) + v×(α/255)
#
# 規則は「後から足した指定が上」の 1 つだけ。
class _MaskPainter:

    def __init__(self, plan, timeline, cfg, width, height):
        self._plan = plan
        self._timeline = timeline
        self._width = width
        self._height = height
        self._scale_x = width / float(timeline.width)
        self._scale_y = height / float(timeline.height)
        self._margin_ratio = float(cfg["render"]["margin_ratio"])
        self._shape_kind = str(cfg["render"]["shape"])
        # 指定が 1 つも効いていないフレームは、この絵を使い回す (作り直さない)。
        # ぼかさないクリップの時刻はすべてこれになる = 出力の大半で費用ゼロ。
        self.blank = Image.new("L", (width, height), 0)

    # 素材のその時刻のマスクを返す
    def paint(self, media_id, source_sec):
        layers = self._plan.layers_at(media_id, source_sec)
        if not layers:
            return self.blank

        mask = np.zeros((self._height, self._width), dtype=np.uint16)
        for layer in layers:
            shape = layer["shape"]
            value = 255 if layer["mode"] == ROLE_BLUR else 0
            if shape["kind"] == decisions_module.KIND_FRAME:
                # 画面全体は塗るまでもない (一番よく通る経路なので特別扱いする)
                mask[:] = value
                continue
            alpha = self._alpha_of(shape)
            if alpha is None:
                continue
            mask = (mask * (255 - alpha) + value * alpha) // 255
        return Image.fromarray(mask.astype(np.uint8), mode="L")

    # 形 1 つを「塗る → フェザー」した α にする。塗れなければ None。
    def _alpha_of(self, shape):
        layer = Image.new("L", (self._width, self._height), 0)
        if not self._fill(ImageDraw.Draw(layer), shape):
            return None
        feather = float(shape.get("feather", 0.0)) * self._scale_x
        if feather > 0.5:
            layer = layer.filter(ImageFilter.GaussianBlur(feather))
        return np.asarray(layer, dtype=np.uint16)

    # 1 つの形を白く塗る。戻り値: 塗れたら True
    def _fill(self, draw, shape):
        rect = shape["rect"]
        relative = shape.get("outline")
        if relative is None and self._margin_ratio > 0:
            # 設定で余白を取る場合だけ広げる (既定は 0 = 囲んだとおり)
            rect = geometry.expand_rect(rect, self._margin_ratio, self._timeline.width)
        clamped = geometry.clamp_rect(rect, self._timeline.width, self._timeline.height)
        if clamped is None:
            return False

        if relative is not None:
            # v4 以前から引き継いだ自由な囲み。形は枠に当てはめて塗る
            points = [(px * self._scale_x, py * self._scale_y)
                      for px, py in contour.to_absolute(relative, shape["rect"])]
            if len(points) >= 3:
                draw.polygon(points, fill=255)
                return True

        x, y, width, height = clamped
        box = (x * self._scale_x, y * self._scale_y,
               (x + width) * self._scale_x, (y + height) * self._scale_y)
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            return False
        if self._shape_kind == "ellipse":
            draw.ellipse(box, fill=255)
        elif self._shape_kind == "rounded":
            radius = min(box[2] - box[0], box[3] - box[1]) * 0.2
            draw.rounded_rectangle(box, radius=radius, fill=255)
        else:
            draw.rectangle(box, fill=255)
        return True


# ------------------------------------------------------------------
# 書き出し
# ------------------------------------------------------------------

# PyAV で可逆コーデック (ffv1) のグレースケール動画にする
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
