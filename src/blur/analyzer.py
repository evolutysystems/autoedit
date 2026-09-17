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
from . import decisions as decisions_module
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
# 輪郭の推論がこの枚数続けて失敗したら、その解析では輪郭をやめる (ver5 resolve3 §5.12)
_SILHOUETTE_MAX_FAILURES = 10
# 輪郭の持ち越し (_carry_silhouettes): 相関を取る縮小率・人物のまわりに足す余白・採用する PSR の下限
_CARRY_SCALE = 0.25
_CARRY_PAD = 0.15
_CARRY_MIN_PSR = 8.0
# 持ち越すのは、枠の幅・高さの変化がこの割合以内のときだけ (大きく変わったら姿勢が変わった)
_CARRY_MAX_RESIZE = 0.2


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

    decisions = decisions_module.load(timeline)

    # 既に同じ指紋の結果があれば解析しない (2 回目以降は省略できる / §5.3.4)
    cached = store.load(cache_path, fingerprint)
    if cached is not None:
        _logger.info("ぼかし解析: キャッシュを再利用します")
        ensure_added_tracks(timeline, settings, cached, decisions, cfg, cache_path, cancel)
        return cached

    analysis = store.new_analysis(timeline, cfg, fingerprint)
    analysis["_silhouette"] = _silhouette_state(cfg)
    analysis["silhouette"] = ({"format": cfg["silhouette"]["format"],
                               "every_n_samples": cfg["silhouette"]["every_n_samples"],
                               "points": cfg["silhouette"]["points"]}
                              if analysis["_silhouette"]["enabled"] else None)
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

    # 「同じ人物にする」の指定を当て込む (ver5 resolve3 §2.5 (b) / §5.8 (2))
    identities = cluster(tracklets, cfg, merges=decisions.get("merges"))
    analysis["identities"] = [_identity_to_dict(identity) for identity in identities]
    analysis["tracks"] = [track.to_dict() for track in tracklets if track.identity]

    attach_thumbs(analysis)
    analysis.pop("_silhouette", None)

    # 追加した枠の追従は解析結果と一緒に消えるため、ここで作り直す (ver5 resolve3 §2.5 (c))
    ensure_added_tracks(timeline, settings, analysis, decisions, cfg, None, cancel)

    _report(on_progress, 1.0)
    if cache_path:
        store.save(cache_path, analysis)
    return analysis


# 指定にある領域・手動の枠のうち、追従トラックが無いものを作る (ver5 resolve3 §5.7 (3))。
# 何か足したら (cache_path があれば) 保存する。戻り値: 足したトラックの件数
def ensure_added_tracks(timeline, settings, analysis, decisions, cfg, cache_path=None,
                        cancel=None):
    from . import manual_tracker, region_tracker  # noqa: PLC0415 (循環 import を避ける)

    if not (decisions or {}).get("regions"):
        return 0
    added = 0
    try:
        added += region_tracker.build_region_tracks(
            timeline, settings, analysis, decisions, cfg, cancel=cancel)
        added += manual_tracker.build_manual_tracks(
            timeline, settings, analysis, decisions, cfg, cancel=cancel)
    except Exception as error:                  # noqa: BLE001 (追従の失敗で解析結果を捨てない)
        _logger.exception("追加した枠の追従を作り直せませんでした: %s", error)
    if added and cache_path:
        store.save(cache_path, analysis)
    return added


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
        silhouettes = _silhouettes(image, boxes, cfg, analysis.get("_silhouette"))
        tracker.update(sec, boxes, embeddings, silhouettes)
        _carry_silhouettes(sec, image, boxes, tracker, cfg, analysis.get("_silhouette"))

        # 見本画像は解析中にしか作れない (後からでは素材を開き直すことになる)
        _keep_thumb(thumbs, tracker, image, boxes, thumb_px)
        on_progress(min((sec - start) / length, 1.0))

    on_progress(1.0)
    return True


# 輪郭を作るかどうかの状態 (解析 1 回ぶん)。人物の形が輪郭で、モデルが使えるときだけ作る。
def _silhouette_state(cfg):
    from . import silhouette                    # noqa: PLC0415 (使うときだけ読む)
    from .config import SHAPE_SILHOUETTE        # noqa: PLC0415

    enabled = False
    if str(cfg["render"]["shape"]) == SHAPE_SILHOUETTE:
        enabled, reason = silhouette.availability(cfg)
        _logger.info("ぼかし解析: %s", reason)
    return {"enabled": enabled, "index": 0, "failures": 0,
            "every_n": int(cfg["silhouette"]["every_n_samples"])}


# 解析サンプル N 枚に 1 枚で、枠ごとの輪郭を作る (ver5 resolve3 §3.2 / §5.3.3)。
# 戻り値: boxes と同じ並びの encode 済み輪郭 (作らない枚は None)
def _silhouettes(image, boxes, cfg, state):
    if not state or not state["enabled"] or not boxes:
        return None
    index = state["index"]
    state["index"] += 1
    if index % max(state["every_n"], 1):
        return None

    from . import contour, silhouette           # noqa: PLC0415

    try:
        shapes = silhouette.segment(image, boxes, cfg)
    except Exception as error:                  # noqa: BLE001 (輪郭の失敗で解析を止めない)
        _logger.debug("輪郭の推論に失敗しました: %s", error, exc_info=True)
        shapes = []
    if not shapes or all(shape is None for shape in shapes):
        state["failures"] += 1
        if state["failures"] >= _SILHOUETTE_MAX_FAILURES:
            # 続けて失敗する = 環境の問題。以降は作らず、矩形で塗る (§5.12)
            state["enabled"] = False
            _logger.warning("輪郭を %d 枚続けて作れなかったため、以降は四角でぼかします",
                            state["failures"])
        return None
    state["failures"] = 0
    return [contour.encode(shape) if shape is not None else None for shape in shapes]


# 輪郭を作らなかった枚へ、直前の輪郭を「動いたぶんだけずらして」持ち越す (ver5 resolve3 §3.3 の補強)。
#
# 輪郭は N 枚に 1 枚しか作らない (時間の都合)。その間に手ぶれや体の移動があると、
# 枠の補間だけでは輪郭が人物からずれ、「ぼかさない」人物の顔がぼける (実素材で確認)。
# そこで、輪郭を作った枚と今の枚で人物のまわりを縮小して位相相関を取り、平行移動ぶんだけ輪郭をずらす。
# 相関が弱い (PSR が低い) 枚は持ち越さない = 前後の本物の輪郭か矩形で塗る。
# ずらした輪郭からさらにずらすと誤差が積もるため、基準は常に「本物の輪郭を作った枚」にする。
def _carry_silhouettes(sec, image, boxes, tracker, cfg, state):
    if not state or not state["enabled"] or not boxes:
        return
    from . import contour                       # noqa: PLC0415
    from .region_tracker import _shrink, phase_correlate, to_gray  # noqa: PLC0415

    carry = state.setdefault("carry", {})
    max_gap = float(cfg["silhouette"]["max_gap_sec"])
    small = None
    for box in boxes:
        track = _track_of_box(tracker, box)
        if track is None or not track.samples:
            continue
        sample = track.samples[-1]
        rect = (sample["x"], sample["y"], sample["w"], sample["h"])
        if small is None:
            small = _shrink(to_gray(image), _CARRY_SCALE)
        if sample.get("sil"):
            relative = contour.decode(sample["sil"])
            if relative is not None:
                carry[track.id] = {"t": float(sec), "gray": small, "rect": rect,
                                   "points": contour.to_absolute(relative, rect)}
            continue
        base = carry.get(track.id)
        if base is None or float(sec) - base["t"] > max_gap:
            continue
        # 枠の形が大きく変わった = 姿勢が変わった (かがむ・腕を広げる)。ずらしても形が合わないため持ち越さない
        if not _similar_size(base["rect"], rect):
            continue
        shift = _local_shift(base["gray"], small, base["rect"], rect, phase_correlate)
        if shift is None:
            continue
        moved = [(px + shift[0], py + shift[1]) for px, py in base["points"]]
        relative = contour.normalize(contour.to_relative(moved, rect),
                                     int(cfg["silhouette"]["points"]))
        sample["sil"] = contour.encode(relative)


# 2 つの枠の幅・高さが、どちらも _CARRY_MAX_RESIZE 以内の変化か
def _similar_size(before, after):
    for index in (2, 3):
        previous = max(float(before[index]), 1e-6)
        if abs(float(after[index]) / previous - 1.0) > _CARRY_MAX_RESIZE:
            return False
    return True


# 2 枚の縮小画像で、人物のまわり (前後の枠を合わせて広げた範囲) の平行移動を求める。
# 戻り値 (dx, dy) 素材ピクセル。相関が弱ければ None。
def _local_shift(before, after, rect_before, rect_after, phase_correlate):
    if before.shape != after.shape:
        return None
    height, width = after.shape[:2]
    left = min(rect_before[0], rect_after[0])
    top = min(rect_before[1], rect_after[1])
    right = max(rect_before[0] + rect_before[2], rect_after[0] + rect_after[2])
    bottom = max(rect_before[1] + rect_before[3], rect_after[1] + rect_after[3])
    pad_x = (right - left) * _CARRY_PAD
    pad_y = (bottom - top) * _CARRY_PAD
    x0 = int(max((left - pad_x) * _CARRY_SCALE, 0))
    y0 = int(max((top - pad_y) * _CARRY_SCALE, 0))
    x1 = int(min((right + pad_x) * _CARRY_SCALE, width))
    y1 = int(min((bottom + pad_y) * _CARRY_SCALE, height))
    if x1 - x0 < 16 or y1 - y0 < 16:
        return None
    dx, dy, _peak, psr = phase_correlate(before[y0:y1, x0:x1], after[y0:y1, x0:x1])
    if psr < _CARRY_MIN_PSR:
        return None
    # region_tracker と同じ符号: 基準 → 今 の移動は (-dx, -dy)
    return -dx / _CARRY_SCALE, -dy / _CARRY_SCALE


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
