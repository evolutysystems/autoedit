# 静止物 (建物など) の追従 (ver5 resolve2 §3.4)
#
# 建物は COCO のクラスに無く、汎用の検出器では出せない。そこで
# **利用者が囲った領域を、位相相関で平行移動追従させる**。
#
#   ・囲ったパスの外接矩形をテンプレートとして切り出す (グレースケール・縮小)
#   ・次のサンプル時刻のフレームに対し、FFT による位相相関で平行移動量を求める
#   ・相関のピークの突き出し方 (PSR) がしきい値を下回ったら「見失った」とみなし、
#     hold_sec のあいだ直前の位置を保持してから打ち切る
#
# 限界: 拡大縮小・回転には追従しない。ズームが入る素材では follow="fixed" を選び、
# margin_ratio で余白を持たせて漏れを防ぐ (§3.4)。
import numpy as np

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# PSR を求めるとき、ピークの周りのこの半径 (px) は裾野として数えない
_PSR_GUARD_PX = 5


# RGB 画像をグレースケールの float 配列にする (ITU-R BT.601 の重み)
def to_gray(image):
    array = np.asarray(image, dtype=np.float32)
    if array.ndim == 2:
        return array
    return array[:, :, 0] * 0.299 + array[:, :, 1] * 0.587 + array[:, :, 2] * 0.114


# 最近傍で縮小する (速度のため。相関は縮小した絵で取る)
def _shrink(image, scale):
    if scale >= 1.0:
        return image
    height, width = image.shape[:2]
    new_h = max(int(height * scale), 8)
    new_w = max(int(width * scale), 8)
    rows = np.clip((np.arange(new_h) * (height / float(new_h))).astype(np.int32), 0, height - 1)
    cols = np.clip((np.arange(new_w) * (width / float(new_w))).astype(np.int32), 0, width - 1)
    return image[rows][:, cols]


# 2 枚の同じ大きさの絵から平行移動量を求める (位相相関)。
# 戻り値 (dx, dy, ピークの強さ 0.0〜1.0, PSR)。
#
# 位相相関は「振幅を捨てて位相だけを見る」ため、明るさの変化に強く、
# カメラのパン・手ぶれのような平行移動をはっきり 1 点のピークとして出せる。
#
# 一致の判定には**ピークの高さではなく PSR** (ピークが裾野の標準偏差の何倍突き出ているか) を使う。
# ピークの高さは絵の変化量で大きく下がり、人が動くだけの固定カメラでも 0.07 程度になる
# (ずれは正しく 0,0 と出ている)。PSR は同じ場面で 50 以上、無関係な動画で 15 以下だった。
def phase_correlate(reference, target):
    if reference.shape != target.shape or reference.size == 0:
        return 0.0, 0.0, 0.0, 0.0

    height, width = reference.shape
    # 窓を掛けて端の不連続 (FFT が周期だと思い込む段差) による偽ピークを抑える
    window = np.outer(np.hanning(height), np.hanning(width)).astype(np.float32)
    a = np.fft.rfft2((reference - reference.mean()) * window)
    b = np.fft.rfft2((target - target.mean()) * window)

    cross = a * np.conj(b)
    magnitude = np.abs(cross)
    # 振幅で割る = 位相だけを残す
    spectrum = cross / np.maximum(magnitude, 1e-9)
    correlation = np.fft.irfft2(spectrum, s=reference.shape)

    index = int(np.argmax(correlation))
    peak_y, peak_x = divmod(index, width)
    peak = float(correlation.flat[index])

    # 折り返し: 半分を超えるずれは負の方向として読む
    dy = peak_y - height if peak_y > height // 2 else peak_y
    dx = peak_x - width if peak_x > width // 2 else peak_x
    return float(dx), float(dy), max(min(peak, 1.0), 0.0), _psr(correlation, peak_y, peak_x, peak)


# ピークの周り (_PSR_GUARD_PX) を除いた裾野に対し、ピークが標準偏差の何倍高いか
def _psr(correlation, peak_y, peak_x, peak):
    height, width = correlation.shape
    rows = [(peak_y + d) % height for d in range(-_PSR_GUARD_PX, _PSR_GUARD_PX + 1)]
    cols = [(peak_x + d) % width for d in range(-_PSR_GUARD_PX, _PSR_GUARD_PX + 1)]
    sidelobe = np.ones(correlation.shape, dtype=bool)
    sidelobe[np.ix_(rows, cols)] = False
    values = correlation[sidelobe]
    if values.size == 0:
        return 0.0
    return max((peak - float(values.mean())) / max(float(values.std()), 1e-9), 0.0)


# 1 つの領域を、与えられたフレーム列で追従させる。
#   anchor_image : 囲んだ時点のフレーム (RGB ndarray / 素材ピクセル)
#   anchor_rect  : 囲みの外接矩形 (x, y, w, h) 素材ピクセル座標
#   frames       : [(時刻, 画像), …] の反復子 (時刻の昇順)
#   cfg          : config.config() の戻り値
# 戻り値: [{"t","x","y","w","h","score"}, …] のサンプル列。
#
# follow="fixed" なら相関を取らず、同じ位置のサンプルを並べるだけにする。
def track_region(anchor_image, anchor_rect, frames, cfg, anchor_sec=0.0):
    region_cfg = cfg["region"]
    scale = float(region_cfg["search_scale"])
    threshold = float(region_cfg["match_psr"])
    hold_sec = float(region_cfg["hold_sec"])
    fixed = str(region_cfg["follow"]) == "fixed"

    x, y, width, height = (float(v) for v in anchor_rect)
    samples = [{"t": float(anchor_sec), "x": x, "y": y, "w": width, "h": height, "score": 1.0}]
    if anchor_image is None:
        return samples

    reference = _shrink(to_gray(anchor_image), scale)
    lost_since = None

    for sec, image in frames:
        if fixed:
            samples.append({"t": float(sec), "x": x, "y": y,
                            "w": width, "h": height, "score": 1.0})
            continue

        target = _shrink(to_gray(image), scale)
        if target.shape != reference.shape:
            # 素材の途中で解像度が変わった = 追えない。固定扱いで続ける。
            samples.append({"t": float(sec), "x": x, "y": y,
                            "w": width, "h": height, "score": 0.0})
            continue

        dx, dy, peak, psr = phase_correlate(reference, target)
        if psr < threshold:
            # 見失った。hold_sec のあいだは直前の位置を保持し、超えたら打ち切る
            if lost_since is None:
                lost_since = float(sec)
            if float(sec) - lost_since > hold_sec:
                _logger.info("領域を見失ったため追従を打ち切ります (%.1f 秒地点)", sec)
                break
            samples.append({"t": float(sec), "x": x, "y": y,
                            "w": width, "h": height, "score": peak})
            continue

        lost_since = None
        # 縮小した絵で求めたずれを元の大きさへ戻す
        x = x - dx / max(scale, 1e-6)
        y = y - dy / max(scale, 1e-6)
        samples.append({"t": float(sec), "x": x, "y": y,
                        "w": width, "h": height, "score": peak})
        reference = target

    return samples


# 別の素材 (アーカイブの他セクション) に同じ対象が映っているか調べる (§5.6.4-3)
# 先頭フレーム全体と相関を取り、PSR がしきい値以上なら「映っている」とみなす。
# 戻り値: (映っているか, ずらした矩形, ピークの強さ)
def match_in_frame(anchor_image, anchor_rect, image, cfg):
    region_cfg = cfg["region"]
    scale = float(region_cfg["search_scale"])
    threshold = float(region_cfg["match_psr"])

    reference = _shrink(to_gray(anchor_image), scale)
    target = _shrink(to_gray(image), scale)
    if reference.shape != target.shape:
        return False, tuple(anchor_rect), 0.0

    dx, dy, peak, psr = phase_correlate(reference, target)
    if psr < threshold:
        return False, tuple(anchor_rect), peak

    x, y, width, height = (float(v) for v in anchor_rect)
    moved = (x - dx / max(scale, 1e-6), y - dy / max(scale, 1e-6), width, height)
    return True, moved, peak


# ------------------------------------------------------------------
# 指定された領域を解析結果へ取り込む (§5.6.4)
# ------------------------------------------------------------------

# 囲まれた領域を、解析対象の全区間へ当て込んで kind="region" のトラックを作る。
#
#   1. 囲んだフレームからテンプレートを切り出す (anchor_sec の絵)
#   2. 同じ素材の他の区間 … 前後へ位相相関で追従する
#   3. 他の素材 (アーカイブの他セクション) … 各フレームで相関を取り、
#      PSR がしきい値以上なら「同じ建物が映っている」として位置を出す
#   4. 得られた軌跡を kind="region" のトラックとしてキャッシュへ足す
#
#   timeline    : 対象の Timeline
#   settings    : setting.json
#   analysis    : 解析結果 (書き換える)
#   decisions   : ぼかし指定 (regions を見る)
#   on_progress : (0.0〜1.0) を受け取るコールバック
#   cancel      : True を返したら中断する callable
# 戻り値: 追加したトラックの件数
def build_region_tracks(timeline, settings, analysis, decisions, cfg,
                        on_progress=None, cancel=None):
    from . import analyzer                      # noqa: PLC0415 (循環 import を避ける)

    regions = [r for r in (decisions or {}).get("regions", []) if r.get("path")]
    if not regions:
        return 0

    existing = {str(t.get("identity")) for t in analysis.get("tracks", [])
                if str(t.get("kind")) == "region"}
    targets = [r for r in regions if str(r.get("id")) not in existing]
    if not targets:
        return 0

    spans = analyzer.collect_spans(timeline)
    added = 0
    for index, region in enumerate(targets):
        if cancel is not None and cancel():
            return added
        tracks = _tracks_for_region(
            timeline, settings, region, spans, cfg,
            lambda ratio, base=index: _report(
                on_progress, (base + ratio) / float(len(targets))),
            cancel)
        analysis.setdefault("tracks", []).extend(tracks)
        added += len(tracks)

    _report(on_progress, 1.0)
    _logger.info("領域の追従: %d 件のトラックを追加しました", added)
    return added


def _report(on_progress, ratio):
    if on_progress is not None:
        on_progress(min(max(float(ratio), 0.0), 1.0))


# 1 つの領域について、全素材・全区間ぶんのトラックを作る
def _tracks_for_region(timeline, settings, region, spans, cfg, on_progress, cancel):
    from . import analyzer                      # noqa: PLC0415
    from . import geometry

    anchor_media_id = str(region.get("media_id") or "")
    anchor_sec = float(region.get("anchor_sec") or 0.0)
    anchor_media = timeline.media_by_id(anchor_media_id)
    if anchor_media is None:
        _logger.warning("領域の素材が見つからないため追従できません: %s", anchor_media_id)
        return []

    # 正規化座標 → キャンバス → 素材ピクセル (変換を知っているのは geometry だけ)
    canvas_path = geometry.normalized_path_to_canvas(
        region.get("path"), timeline.width, timeline.height)
    canvas_rect = geometry.polygon_bounds(canvas_path)
    if canvas_rect is None:
        return []
    transform = geometry.source_to_canvas_transform(
        anchor_media, timeline.width, timeline.height)
    anchor_rect = geometry.canvas_rect_to_source(canvas_rect, transform)

    anchor_image = _frame_at(analyzer, anchor_media, anchor_sec, settings)
    if anchor_image is None:
        _logger.warning("領域の基準フレームを取得できないため追従できません")
        return []

    sample_fps = float(cfg["analysis"]["sample_fps"])
    scale = float(cfg["region"]["search_scale"])
    threshold = float(cfg["region"]["match_psr"])
    reference = _shrink(to_gray(anchor_image), scale)

    tracks = []
    total = max(sum(len(ranges) for _media, ranges in spans), 1)
    done = 0
    for media_id, ranges in spans:
        media = timeline.media_by_id(media_id)
        if media is None:
            done += len(ranges)
            continue
        for start, end in ranges:
            if cancel is not None and cancel():
                return tracks
            samples = _samples_for_span(
                analyzer, media, start, end, sample_fps, settings,
                reference, anchor_rect, scale, threshold, cfg, cancel)
            done += 1
            on_progress(done / float(total))
            if len(samples) < 2:
                continue
            tracks.append({
                "id": f"{region['id']}_{media_id}_{int(start)}",
                "identity": str(region["id"]),
                "media_id": media_id,
                "kind": "region",
                "start_sec": round(samples[0]["t"], 3),
                "end_sec": round(samples[-1]["t"], 3),
                "samples": [
                    {"t": round(s["t"], 3), "x": round(s["x"], 1), "y": round(s["y"], 1),
                     "w": round(s["w"], 1), "h": round(s["h"], 1),
                     "score": round(s["score"], 3)}
                    for s in samples
                ],
            })
    return tracks


# 1 区間ぶんのサンプル列を作る。
# 各フレームで**基準の絵と**相関を取る (連鎖させない) ため、途中で見失っても
# 後から復帰できる。PSR がしきい値を下回るフレームは「映っていない」として落とす。
def _samples_for_span(analyzer, media, start, end, sample_fps, settings,
                      reference, anchor_rect, scale, threshold, cfg, cancel):
    hold_sec = float(cfg["region"]["hold_sec"])
    x0, y0, width, height = (float(v) for v in anchor_rect)

    samples = []
    lost_since = None
    for sec, image in analyzer.iter_frames(media, start, end, sample_fps, settings):
        if cancel is not None and cancel():
            break
        target = _shrink(to_gray(image), scale)
        if target.shape != reference.shape:
            continue                            # 解像度が違う素材は対象にしない

        dx, dy, peak, psr = phase_correlate(reference, target)
        if psr < threshold:
            # 見失った。hold_sec を超えたらこの区間の追従は打ち切る
            if lost_since is None:
                lost_since = float(sec)
            elif float(sec) - lost_since > hold_sec:
                break
            continue

        lost_since = None
        samples.append({
            "t": float(sec),
            "x": x0 - dx / max(scale, 1e-6),
            "y": y0 - dy / max(scale, 1e-6),
            "w": width, "h": height, "score": peak,
        })
    return samples


# 指定時刻の 1 枚を取り出す (基準の絵に使う)
def _frame_at(analyzer, media, sec, settings):
    for _t, image in analyzer.iter_frames(media, max(sec - 0.01, 0.0), sec + 0.5, 4.0,
                                          settings):
        return image
    return None
