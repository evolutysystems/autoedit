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
from . import decisions as decisions_module
from . import geometry, store

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
# 機能 OFF / 指定なし / キャッシュ不一致のいずれでも None を返す (= 従来どおりの出力)。
def prepare(timeline, context, cfg=None):
    from .config import config, is_enabled     # noqa: PLC0415 (機能 OFF なら読まない)

    settings = context.settings
    if not is_enabled(settings):
        return None                             # 機能 OFF: 1 行も通らない (R9)

    cfg = cfg or config(settings)
    decisions = decisions_module.load(timeline)
    if not decisions_module.has_any(decisions):
        _logger.debug("ぼかしの指定が無いためマスクを作りません")
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

    plan = build_plan(timeline, analysis, decisions, cfg)
    if not plan["shapes"]:
        _logger.info("ぼかす対象が無いためマスクを作りません")
        return None

    fps = int(timeline.fps or 60)
    duration = float(timeline.duration_sec())
    total_frames = max(int(round(duration * fps)), 1)
    width = max(int(timeline.width * float(cfg["render"]["mask_scale"])) // 2 * 2, 2)
    height = max(int(timeline.height * float(cfg["render"]["mask_scale"])) // 2 * 2, 2)

    _logger.info("ぼかしマスク: %d フレーム / %dx%d / 対象 %d 件",
                 total_frames, width, height, len(plan["shapes"]))

    frames = _iter_mask_frames(plan, timeline, cfg, total_frames, fps, width, height, on_progress)
    if _PYAV_AVAILABLE:
        return _encode_pyav(frames, out_path, fps, width, height)
    return _encode_ffmpeg(frames, out_path, fps, width, height, cfg)


# ------------------------------------------------------------------
# どこをぼかすかの計画
# ------------------------------------------------------------------

# ぼかす対象のトラックを集める。
# 戻り値 {"shapes": [{"media_id","kind","samples","start","end"}, …]}
def build_plan(timeline, analysis, decisions, cfg):
    main_id = store.main_identity_id(analysis)
    regions = {str(r.get("id")): r for r in (decisions.get("regions") or [])}
    pad = float(cfg["render"]["pad_sec"])

    shapes = []
    for track in (analysis or {}).get("tracks", []):
        kind = str(track.get("kind", "person"))
        if kind == "person":
            identity = str(track.get("identity") or "")
            if not identity:
                continue
            if not decisions_module.should_blur_identity(identity, decisions, cfg, main_id):
                continue
        else:
            region = regions.get(str(track.get("region") or track.get("identity") or ""))
            if region is None or not decisions_module.should_blur_region(region):
                continue

        samples = [s for s in track.get("samples", []) if _valid_sample(s)]
        if not samples:
            continue
        shapes.append({
            "media_id": str(track.get("media_id") or ""),
            "kind": kind,
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
# 1 フレームぶんの絵
# ------------------------------------------------------------------

# 出力フレームを 1 枚ずつ作って返す (グレースケールの PIL Image)
def _iter_mask_frames(plan, timeline, cfg, total_frames, fps, width, height, on_progress):
    timemap = TimeMap.from_timeline(timeline)
    scale_x = width / float(timeline.width)
    scale_y = height / float(timeline.height)
    feather = max(float(cfg["render"]["feather_ratio"]) * width, 0.0)
    margin_ratio = float(cfg["render"]["margin_ratio"])
    shape_kind = str(cfg["render"]["shape"])
    # 素材ごとの座標変換は毎フレーム作り直さない (同じ素材なら同じ変換)
    transforms = {}

    # 何も描かないフレームは同じ絵を使い回す (作り直さない)。
    # 出力の大半は「ぼかす対象が映っていない」フレームのため、ここが効く。
    blank = Image.new("L", (width, height), 0)

    for index in range(total_frames):
        timeline_sec = index / float(fps)
        image = None
        draw = None
        drawn = 0

        located = timemap.to_source(timeline_sec)
        if located is not None:
            media_id, source_sec = located
            transform = transforms.get(media_id)
            if transform is None:
                media = timeline.media_by_id(media_id)
                transform = geometry.source_to_canvas_transform(
                    media, timeline.width, timeline.height)
                transforms[media_id] = transform

            for shape in plan["shapes"]:
                if shape["media_id"] and shape["media_id"] != media_id:
                    continue
                if not (shape["start"] <= source_sec <= shape["end"]):
                    continue
                rect = _rect_at(shape, source_sec)
                if rect is None:
                    continue
                rect = geometry.source_rect_to_canvas(rect, transform)
                rect = geometry.expand_rect(rect, margin_ratio, timeline.width)
                rect = geometry.clamp_rect(rect, timeline.width, timeline.height)
                if rect is None:
                    continue
                if draw is None:
                    image = Image.new("L", (width, height), 0)
                    draw = ImageDraw.Draw(image)
                _fill(draw, rect, scale_x, scale_y, shape["kind"], shape_kind)
                drawn += 1

        if image is None:
            image = blank
        elif feather > 0.5:
            # 境界をぼかす = alphamerge したときに自然に溶ける (§3.5)
            image = image.filter(ImageFilter.GaussianBlur(feather))

        if on_progress is not None and (index % 30 == 0 or index == total_frames - 1):
            on_progress((index + 1) / float(total_frames), {})
        yield image


# その時刻の矩形を線形補間で求める (samples は sample_fps 間隔しか持たない / §5.2.1)
def _rect_at(shape, source_sec):
    samples = shape["samples"]
    first = samples[0]
    last = samples[-1]
    # pad_sec ぶんはみ出した時間は端の矩形をそのまま使う
    if source_sec <= first["t"]:
        return (first["x"], first["y"], first["w"], first["h"])
    if source_sec >= last["t"]:
        return (last["x"], last["y"], last["w"], last["h"])

    previous = first
    for sample in samples:
        if sample["t"] >= source_sec:
            span = sample["t"] - previous["t"]
            ratio = (source_sec - previous["t"]) / span if span > 1e-9 else 0.0
            return (
                previous["x"] + (sample["x"] - previous["x"]) * ratio,
                previous["y"] + (sample["y"] - previous["y"]) * ratio,
                previous["w"] + (sample["w"] - previous["w"]) * ratio,
                previous["h"] + (sample["h"] - previous["h"]) * ratio,
            )
        previous = sample
    return (last["x"], last["y"], last["w"], last["h"])


# 1 つの対象を白く塗る。人物は角丸・領域は矩形を既定にする。
def _fill(draw, rect, scale_x, scale_y, kind, shape_kind):
    x, y, width, height = rect
    left = x * scale_x
    top = y * scale_y
    right = (x + width) * scale_x
    bottom = (y + height) * scale_y
    if right - left < 1 or bottom - top < 1:
        return

    box = (left, top, right, bottom)
    if kind != "person" or shape_kind == "rect":
        draw.rectangle(box, fill=255)
    elif shape_kind == "ellipse":
        draw.ellipse(box, fill=255)
    else:
        radius = min(right - left, bottom - top) * 0.2
        draw.rounded_rectangle(box, radius=radius, fill=255)


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
