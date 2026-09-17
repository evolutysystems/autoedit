# 解析の入口 — 対象区間の決定・デコード・進捗・キャンセル (ver5 resolve2 §5.3)
#
# Timeline から「出力に含まれる区間」だけを集め、sample_fps 間隔で 1 枚ずつ取り出して
# 検出 → 特徴抽出 → トラッキング を回す。2 時間の VOD から 5 分ぶんを切り抜く場合、
# 解析量は **5 分ぶん**で済む (音声認識が残す区間だけを認識するのと同じ考え方)。
#
# デコードは PyAV の前進デコード。使えない環境では FFmpeg へ rawvideo をパイプする
# (frame_source が既に同じ二段構えになっている)。
import os
import subprocess

import numpy as np

from ..modules import ffmpeg_runner
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from . import detector, reid, store
from .tracker import Tracker, cluster

_logger = get_logger(__name__)

# PyAV は任意依存として扱う (未導入・import 失敗時は FFmpeg 方式へ倒す)
try:
    import av
    _PYAV_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PYAV_AVAILABLE = False

# 隣接する区間をまとめるときの許容 (この秒数以内なら 1 区間にする)
_MERGE_GAP_SEC = 1.0


# Timeline から解析対象の区間を決め、検出 → トラッキング → クラスタリングまで行う。
#   timeline    : 解析対象の Timeline
#   settings    : setting.json 全体
#   cache_path  : 解析結果の保存先 (None なら保存しない)
#   on_progress : (0.0〜1.0, ラベル) を受け取るコールバック
#   cancel      : True を返したら中断する callable
# 戻り値: 解析結果の辞書。中断したら None。
def analyze(timeline, settings, cache_path=None, on_progress=None, cancel=None):
    from .config import config              # noqa: PLC0415 (循環 import を避ける)

    cfg = config(settings)
    spans = collect_spans(timeline)
    if not spans:
        _logger.info("ぼかし解析: 対象の区間がありません")
        return None

    media_paths = [_media_path(timeline, media_id) for media_id, _ranges in spans]
    fingerprint = store.fingerprint(timeline, cfg, media_paths)

    # 既に同じ指紋の結果があれば解析しない (2 回目以降は省略できる / §5.3.4)
    cached = store.load(cache_path, fingerprint)
    if cached is not None:
        _logger.info("ぼかし解析: キャッシュを再利用します")
        return cached

    analysis = store.new_analysis(timeline, cfg, fingerprint)
    total_sec = sum(end - start for _media, ranges in spans for start, end in ranges)
    done_sec = 0.0
    tracklets = []
    next_index = 0

    for media_id, ranges in spans:
        media = timeline.media_by_id(media_id)
        if media is None or not media.path or not os.path.isfile(media.path):
            _logger.warning("ぼかし解析: 素材が見つからないため飛ばします: %s", media_id)
            done_sec += sum(end - start for start, end in ranges)
            continue

        tracker = Tracker(cfg, media_id, start_index=next_index)
        for start, end in ranges:
            result = _analyze_span(
                media, start, end, cfg, tracker, settings,
                lambda ratio, base=done_sec, length=end - start: _report(
                    on_progress, (base + ratio * length) / max(total_sec, 1e-6)),
                cancel, analysis)
            done_sec += end - start
            if result is None:
                _logger.info("ぼかし解析: 中断しました")
                return None

        tracklets.extend(tracker.finish())
        next_index = tracker.next_index()

    identities = cluster(tracklets, cfg)
    analysis["identities"] = [_identity_to_dict(identity) for identity in identities]
    analysis["tracks"] = [track.to_dict() for track in tracklets if track.identity]

    attach_thumbs(analysis)

    _report(on_progress, 1.0)
    if cache_path:
        store.save(cache_path, analysis)
    return analysis


# 解析対象の区間を集める (§5.3.2)
# V1 のベースクリップが参照する (media_id, source_in, source_out) を集め、
# 同じ素材の重なり・隣接をまとめる。OP / ED は対象外にする。
# 戻り値: [(media_id, [(開始秒, 終了秒), …]), …]
def collect_spans(timeline):
    by_media = {}
    for clip in timeline.base_clips():
        if clip.is_opening_or_ending():
            continue                            # 毎回同じ定型素材のため解析しない
        start = float(clip.source_in)
        end = float(clip.source_out)
        if end - start <= 0:
            continue
        by_media.setdefault(clip.media_id, []).append((start, end))

    spans = []
    for media_id, ranges in by_media.items():
        spans.append((media_id, _merge_ranges(ranges)))
    return spans


# 重なり・隣接する区間をまとめる (同じ所を 2 回デコードしない)
def _merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start - merged[-1][1] <= _MERGE_GAP_SEC:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _media_path(timeline, media_id):
    media = timeline.media_by_id(media_id)
    return media.path if media is not None else ""


def _report(on_progress, ratio):
    if on_progress is not None:
        on_progress(min(max(float(ratio), 0.0), 1.0), {})


# ------------------------------------------------------------------
# 1 区間の処理 (§5.3.3)
# ------------------------------------------------------------------

# 1 区間をデコードしながら検出・追跡する。中断したら None を返す。
def _analyze_span(media, start, end, cfg, tracker, settings, on_progress, cancel, analysis):
    sample_fps = float(cfg["analysis"]["sample_fps"])
    length = max(end - start, 1e-6)
    thumb_px = int(cfg["spec"]["thumb_px"])
    # 人物 ID ごとの見本画像は「最初に大きく映った 1 枚」を使う
    thumbs = analysis.setdefault("_thumbs", {})

    frames = _iter_frames(media, start, end, sample_fps, settings)
    for sec, image in frames:
        if cancel is not None and cancel():
            return None

        boxes = detector.detect(image, cfg)
        embeddings = reid.embed(image, boxes, cfg) if boxes else []
        tracker.update(sec, boxes, embeddings)

        # 見本画像は解析中にしか作れない (後からでは素材を開き直すことになる)
        _keep_thumb(thumbs, tracker, image, boxes, thumb_px)
        on_progress(min((sec - start) / length, 1.0))

    on_progress(1.0)
    return True


# tracklet ごとに「一番大きく映った」矩形の見本を控える
def _keep_thumb(thumbs, tracker, image, boxes, thumb_px):
    for index, box in enumerate(boxes):
        track = _track_of_box(tracker, box)
        if track is None:
            continue
        area = float(box[2]) * float(box[3])
        current = thumbs.get(track.id)
        if current is not None and current[0] >= area:
            continue
        encoded = store.encode_thumb(image, box, thumb_px)
        if encoded:
            thumbs[track.id] = (area, encoded)


# 直近のサンプルがこの矩形になっている tracklet を探す
def _track_of_box(tracker, box):
    for track in tracker.active_tracks():
        last = track.last_box()
        if last is None:
            continue
        if (abs(last[0] - box[0]) < 1e-6 and abs(last[1] - box[1]) < 1e-6
                and abs(last[2] - box[2]) < 1e-6):
            return track
    return None


def _identity_to_dict(identity):
    return {
        "id": identity["id"],
        "total_sec": round(float(identity["total_sec"]), 2),
        "main": bool(identity["main"]),
        "embedding": [round(float(v), 5) for v in (identity["vector"]
                                                   if identity["vector"] is not None else [])],
        "thumb": "",
        "tracks": [track.id for track in identity["tracks"]],
    }


# ------------------------------------------------------------------
# デコード (§5.3.3)
# ------------------------------------------------------------------

# 区間を sample_fps の間隔で 1 枚ずつ返す (公開。region_tracker からも使う)
def iter_frames(media, start, end, sample_fps, settings):
    yield from _iter_frames(media, start, end, sample_fps, settings)


# 区間を sample_fps の間隔で 1 枚ずつ返す。RGB の ndarray を (時刻, 画像) で返す。
# PyAV が使えれば前進デコード、駄目なら FFmpeg へ rawvideo をパイプする。
def _iter_frames(media, start, end, sample_fps, settings):
    if _PYAV_AVAILABLE:
        try:
            yield from _iter_frames_pyav(media, start, end, sample_fps)
            return
        except Exception as error:              # noqa: BLE001 (壊れた素材でも解析を続ける)
            _logger.warning("PyAV でのデコードに失敗したため FFmpeg へ切り替えます: %s", error)
    yield from _iter_frames_ffmpeg(media, start, end, sample_fps, settings)


# PyAV で区間をデコードする (frame_source と同じ手口。seek してから前進デコード)
def _iter_frames_pyav(media, start, end, sample_fps):
    interval = 1.0 / max(sample_fps, 0.01)
    container = av.open(media.path)
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        # 目的時刻の手前のキーフレームまで戻ってから前進デコードする
        if stream.time_base:
            offset = int(max(start, 0.0) / float(stream.time_base))
            container.seek(offset, stream=stream, any_frame=False, backward=True)

        next_sec = start
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            sec = float(frame.pts * stream.time_base)
            if sec < start - interval:
                continue
            if sec > end:
                break
            if sec + 1e-6 < next_sec:
                continue
            yield sec, frame.to_ndarray(format="rgb24")
            next_sec = max(sec, next_sec) + interval
    finally:
        container.close()


# FFmpeg へ rawvideo をパイプさせて 1 枚ずつ読む (PyAV が無い環境)
def _iter_frames_ffmpeg(media, start, end, sample_fps, settings):
    ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
    width = int(getattr(media, "width", 0) or 0)
    height = int(getattr(media, "height", 0) or 0)
    if width <= 0 or height <= 0:
        _logger.warning("素材の寸法が分からないため解析できません: %s", media.path)
        return

    interval = 1.0 / max(sample_fps, 0.01)
    command = [
        ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg), "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(start, 0.0):.3f}", "-i", media.path,
        "-t", f"{max(end - start, 0.0):.3f}",
        "-vf", f"fps={sample_fps}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    frame_bytes = width * height * 3
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=no_window_creationflags())
    try:
        index = 0
        while True:
            raw = process.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                break
            image = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            yield start + index * interval, image
            index += 1
    finally:
        if process.stdout is not None:
            process.stdout.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


# 解析結果へ見本画像を当て込む (解析の最後に 1 回だけ呼ぶ)
# tracklet ごとに控えた見本のうち、その人物で一番大きいものを採用する。
def attach_thumbs(analysis):
    thumbs = analysis.pop("_thumbs", {})
    if not thumbs:
        return analysis
    by_track = {track["id"]: track for track in analysis.get("tracks", [])}
    for identity in analysis.get("identities", []):
        best = None
        for track_id in identity.get("tracks", []):
            if track_id not in by_track:
                continue
            candidate = thumbs.get(track_id)
            if candidate and (best is None or candidate[0] > best[0]):
                best = candidate
        identity["thumb"] = best[1] if best else ""
    return analysis
