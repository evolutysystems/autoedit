# ストリームマーカーの正規化・セクション化 (ver3 resolve16 §5.2)
# Twitch Helix `GET /helix/streams/markers` の応答、または内部正規化フォーマットの
# どちらでも受け付け、採点結果へ必ず足す「強制セクション」の区間へ変換する。
# 取得(ネットワーク)は twitch_auth.get_video_markers が担い、ここは変換だけを行う
# (comment_source と同じ分担。ML/外部依存は使わない純粋 Python)。
#
# 内部正規化フォーマット:
#   [{"offset_sec": 1234.0, "description": "神回", "id": "106b8d…"}]
#   offset_sec = VOD 先頭からの相対秒。Helix の position_seconds と同じ基準
#   (VOD は全長を取得しているため 0 秒 = 配信開始 / resolve16 §2.4)。
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 極短の区間はセクションにしない (timeline_builder._MIN_SEGMENT_SEC と同値)
_MIN_SECTION_SEC = 0.01

# 説明が空のマーカーに使う既定ラベル (ログとセクションの印に使う)
_DEFAULT_LABEL = "マーカー"


# Helix 応答 (data[].videos[].markers[]) から marker 配列を取り出す
def _markers_of_helix(payload):
    out = []
    for user in (payload.get("data") or []):
        if not isinstance(user, dict):
            continue
        for video in (user.get("videos") or []):
            if not isinstance(video, dict):
                continue
            markers = video.get("markers") or []
            if isinstance(markers, list):
                out.extend(markers)
    return out


# Helix の 1 マーカー(dict)を内部正規化フォーマットへ変換する。
# 期待キー: position_seconds (配信開始からの相対秒) / description / id
def _normalize_helix_marker(marker):
    if not isinstance(marker, dict):
        return None
    try:
        offset = float(marker.get("position_seconds"))
    except (TypeError, ValueError):
        return None
    return {
        "offset_sec": offset,
        "description": str(marker.get("description", "") or ""),
        "id": str(marker.get("id", "") or ""),
    }


# 既に内部正規化フォーマットの 1 要素 (offset_sec を持つ dict) を安全に整える。
def _coerce_normalized(item):
    if not isinstance(item, dict) or "offset_sec" not in item:
        return None
    try:
        offset = float(item["offset_sec"])
    except (TypeError, ValueError):
        return None
    return {
        "offset_sec": offset,
        "description": str(item.get("description", "") or ""),
        "id": str(item.get("id", "") or ""),
    }


# 生データ (Helix 応答 dict / marker 配列 / 内部正規化配列) を内部正規化配列へ統一する。
# position_seconds を持てば Helix 形式、offset_sec を持てば内部形式として扱う。
# 壊れた要素は捨て、offset_sec 昇順にソートして返す (取得方式の差を吸収する)。
def normalize_markers(raw):
    if isinstance(raw, dict):
        raw = _markers_of_helix(raw)
    if not isinstance(raw, list):
        return []

    normalized = []
    for item in raw:
        if isinstance(item, dict) and "position_seconds" in item:
            conv = _normalize_helix_marker(item)
        else:
            conv = _coerce_normalized(item)
        if conv is not None:
            normalized.append(conv)
    normalized.sort(key=lambda m: m["offset_sec"])
    return normalized


# マーカーから「必ず残す区間」を作る (要望 I2: 前後 2 分)。
# duration_sec でクランプし、0 秒未満・動画尾を越える指定を丸める。
# ここでは重なりの統合を行わない。統合は scoring._merge_time_sections が
# 採点結果と一緒に 1 回だけ行う (規約を 2 つ持たないため / resolve16 §3-3)。
# 戻り値: [{"start","end","label"}] (start 昇順)
def to_sections(markers, before_sec, after_sec, duration_sec):
    duration = float(duration_sec or 0.0)
    if duration <= 0:
        return []
    before = max(float(before_sec or 0.0), 0.0)
    after = max(float(after_sec or 0.0), 0.0)

    sections = []
    dropped = 0
    for marker in normalize_markers(markers):
        offset = marker["offset_sec"]
        # VOD の外を指すマーカーは使えない (配信断で VOD が分かれた回など)
        if offset < 0 or offset >= duration:
            dropped += 1
            continue
        start = max(0.0, offset - before)
        end = min(duration, offset + after)
        if end - start <= _MIN_SECTION_SEC:
            dropped += 1
            continue
        sections.append({
            "start": start,
            "end": end,
            "label": marker["description"] or _DEFAULT_LABEL,
        })
    if dropped:
        _logger.warning("VOD 外のマーカーを %d 件除外しました (尺 %.1fs)", dropped, duration)
    sections.sort(key=lambda s: s["start"])
    return sections
