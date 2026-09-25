# 縦動画の切り抜き指定 (ver5 resolve9 §5.2 / §5.3)
#
# 「ソースのどこを切り抜いて、縦キャンバスのどこへ置くか」だけを持つ。
# 切り抜いた動画ファイルは作らず、書き出しのときに FFmpeg の crop で適用する (§3.1)。
#
# 枠 → フィルタ文字列 / プレビューの配置 の計算は**このモジュールだけ**が持つ。
# 画面と出力で計算が分かれると、見たとおりに書き出されなくなるため (§4-3)。
#
# 指定は Timeline の source["crop"] に入る。project_io は source をそのまま
# 書き出して読み戻すため、往復の処理を足さずに後方互換を保てる (§2.3 / R12)。
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 書式の版。読めない版は「指定なし」として扱う (§5.3)
VERSION = 1

# source の中のキー
KEY = "crop"

# 切り抜き方
MODE_SINGLE = "single"      # 枠 1 つ。余りは背景で埋める
MODE_SPLIT = "split"        # 枠 2 つ。上下に積んでキャンバスを埋める
_MODES = (MODE_SINGLE, MODE_SPLIT)

# 背景の埋め方 (single のみ意味を持つ)
BG_BLUR = "blur"
BG_BLACK = "black"
_BACKGROUNDS = (BG_BLUR, BG_BLACK)

# 分割したときの上枠の高さの比 (キャンバス高に対して)。1080x1920 なら 640 / 1280 になる。
_SPLIT_TOP_RATIO = 1.0 / 3.0

# 設定が空だったときの既定値 (setting.json の vertical.crop と同じ)
_DEFAULTS = {
    "background": BG_BLUR,
    "blur_radius": 20,
    "blur_power": 2,
    "max_scale": 2.0,
    "project_suffix": "_vertical",
    "close_gaps": True,
}


# vertical.crop を平坦化して返す
def config(settings):
    vertical = settings.get("vertical", {}) if isinstance(settings, dict) else {}
    section = vertical.get("crop", {}) if isinstance(vertical, dict) else {}

    background = str(section.get("background", _DEFAULTS["background"]) or "").strip()
    if background not in _BACKGROUNDS:
        if background:
            _logger.warning("未知の vertical.crop.background のため既定を使用します: %s", background)
        background = _DEFAULTS["background"]

    return {
        "background": background,
        "blur_radius": _clamp_int(section.get("blur_radius"), _DEFAULTS["blur_radius"], 1, 100),
        "blur_power": _clamp_int(section.get("blur_power"), _DEFAULTS["blur_power"], 1, 10),
        "max_scale": _clamp(section.get("max_scale"), _DEFAULTS["max_scale"], 1.0, 8.0),
        "project_suffix": str(section.get("project_suffix",
                                          _DEFAULTS["project_suffix"]) or ""),
        "close_gaps": bool(section.get("close_gaps", _DEFAULTS["close_gaps"])),
    }


# ------------------------------------------------------------------
# 読み書き
# ------------------------------------------------------------------

# Timeline から指定を読む。無い・読めない場合は None (書き出しを止めない / §4-4)
def load(timeline):
    source = getattr(timeline, "source", None)
    layout = (source or {}).get(KEY) if isinstance(source, dict) else None
    if not isinstance(layout, dict):
        return None
    if int(layout.get("version") or 0) != VERSION:
        _logger.info("読めない書式の切り抜き指定のため無視します: version=%s", layout.get("version"))
        return None
    if str(layout.get("mode") or "") not in _MODES:
        return None
    return layout


# Timeline へ指定を書く (None を渡すと取り除く)
def store(timeline, layout):
    source = dict(getattr(timeline, "source", None) or {})
    if layout is None:
        source.pop(KEY, None)
    else:
        source[KEY] = layout
    timeline.source = source


# ------------------------------------------------------------------
# ジオメトリ
# ------------------------------------------------------------------

# 縦キャンバス上の置き場所 (mode ごとに決まる)。戻り値: [(x, y, w, h), ...]
def dest_rects(mode, canvas_width, canvas_height):
    canvas_width = max(int(canvas_width or 0), 2)
    canvas_height = max(int(canvas_height or 0), 2)
    if mode != MODE_SPLIT:
        return []

    top_height = _even(canvas_height * _SPLIT_TOP_RATIO)
    return [
        (0, 0, canvas_width, top_height),
        (0, top_height, canvas_width, canvas_height - top_height),
    ]


# 全体 (single) の置き場所。キャンバスへ収まるよう拡縮して中央へ置く。
def fit_single(src_rect, canvas_width, canvas_height):
    _x, _y, src_w, src_h = (int(v) for v in src_rect)
    canvas_width = max(int(canvas_width or 0), 2)
    canvas_height = max(int(canvas_height or 0), 2)
    src_w = max(src_w, 1)
    src_h = max(src_h, 1)

    scale = min(canvas_width / float(src_w), canvas_height / float(src_h))
    width = min(_even(src_w * scale), canvas_width)
    height = min(_even(src_h * scale), canvas_height)
    return ((canvas_width - width) // 2, (canvas_height - height) // 2, width, height)


# 枠に強いる縦横比 (幅 / 高さ)。single は自由なので None。
def aspect(mode, index, canvas_width, canvas_height):
    rects = dest_rects(mode, canvas_width, canvas_height)
    if not rects or index >= len(rects):
        return None
    _x, _y, width, height = rects[index]
    return width / float(height)


# 枠の最大サイズ。single はキャンバス幅の正方形まで (plan.md P3 / R4)。
def max_src_size(mode, index, canvas_width, canvas_height, media_width, media_height):
    media_width = max(int(media_width or 0), 1)
    media_height = max(int(media_height or 0), 1)
    if mode != MODE_SPLIT:
        limit = int(canvas_width or 0) or media_width
        return (min(limit, media_width), min(limit, media_height))

    # 分割は比率固定。素材へ収まる最大を返す。
    ratio = aspect(mode, index, canvas_width, canvas_height) or 1.0
    width = min(media_width, _even(media_height * ratio))
    height = _even(width / ratio)
    return (width, height)


# 枠の最小サイズ。拡大率の上限から決まる (R16 / §3.5)。
def min_src_size(mode, index, canvas_width, canvas_height, max_scale):
    max_scale = max(float(max_scale or 1.0), 1.0)
    if mode != MODE_SPLIT:
        # single はキャンバス幅を max_scale まで拡大してよい
        return (_even(int(canvas_width or 0) / max_scale), _even(int(canvas_width or 0) / max_scale))

    rects = dest_rects(mode, canvas_width, canvas_height)
    if index >= len(rects):
        return (2, 2)
    _x, _y, width, height = rects[index]
    return (_even(width / max_scale), _even(height / max_scale))


# 枠 (ソース px) から書式 v1 の指定を作る
#   frames_src : [(x, y, w, h), ...] single は 1 件 / split は 2 件
def make_layout(mode, frames_src, media, canvas_width, canvas_height, background=BG_BLUR):
    mode = mode if mode in _MODES else MODE_SINGLE
    background = background if background in _BACKGROUNDS else BG_BLUR
    dests = dest_rects(mode, canvas_width, canvas_height)

    frames = []
    for index, src in enumerate(frames_src):
        src_rect = _int_rect(src)
        dest = dests[index] if index < len(dests) else fit_single(
            src_rect, canvas_width, canvas_height)
        frames.append({"src": list(src_rect), "dest": list(dest)})

    return {
        "version": VERSION,
        "mode": mode,
        "media_id": str(getattr(media, "id", "") or ""),
        "source": [int(getattr(media, "width", 0) or 0), int(getattr(media, "height", 0) or 0)],
        "background": background,
        "frames": frames,
    }


# 指定が素材・キャンバスと噛み合うか。噛み合わなければ切り抜き無しとして扱う (§5.3)
def is_valid(layout, media, canvas_width, canvas_height):
    if not layout or media is None:
        return False

    mode = str(layout.get("mode") or "")
    frames = layout.get("frames") or []
    expected = 2 if mode == MODE_SPLIT else 1
    if mode not in _MODES or len(frames) != expected:
        return False

    media_id = str(layout.get("media_id") or "")
    if media_id and media_id != str(getattr(media, "id", "") or ""):
        return False

    width = int(getattr(media, "width", 0) or 0)
    height = int(getattr(media, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return False

    # 枠を決めたときと素材の寸法が変わっていたら、そのままでは当てられない
    source = layout.get("source") or []
    if len(source) == 2 and (int(source[0] or 0), int(source[1] or 0)) != (width, height):
        return False

    for frame in frames:
        rect = frame.get("src") or []
        if len(rect) != 4:
            return False
        x, y, w, h = (int(v) for v in rect)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
            return False

    return int(canvas_width or 0) > 0 and int(canvas_height or 0) > 0


# ------------------------------------------------------------------
# 出力 (FFmpeg) / プレビュー
# ------------------------------------------------------------------

# ベース抽出の映像フィルタを組み立てる (§5.5)。
# 戻り値は 1 本の filtergraph 文字列。入力は 1 本のままで、分岐は split で作る。
def filter_chain(layout, canvas_width, canvas_height, fps, cfg):
    mode = str(layout.get("mode") or MODE_SINGLE)
    frames = layout.get("frames") or []
    canvas_width = int(canvas_width)
    canvas_height = int(canvas_height)
    tail = f"setsar=1,fps={fps},format=yuv420p"

    if mode == MODE_SPLIT:
        dests = dest_rects(mode, canvas_width, canvas_height)
        top = _crop_filter(frames[0]["src"])
        bottom = _crop_filter(frames[1]["src"])
        return (
            "split=2[ctop][cbtm];"
            f"[ctop]{top},scale={dests[0][2]}:{dests[0][3]}[ctopo];"
            f"[cbtm]{bottom},scale={dests[1][2]}:{dests[1][3]}[cbtmo];"
            f"[ctopo][cbtmo]vstack=inputs=2,{tail}"
        )

    crop = _crop_filter(frames[0]["src"])
    fit = (f"scale={canvas_width}:{canvas_height}:force_original_aspect_ratio=decrease")
    if cfg.get("background") != BG_BLUR:
        return (f"{crop},{fit},"
                f"pad={canvas_width}:{canvas_height}:(ow-iw)/2:(oh-ih)/2,{tail}")

    # ぼかし背景: 同じ入力を split して、覆うまで拡大 → ぼかし → 前景を中央へ重ねる
    radius = int(cfg.get("blur_radius") or _DEFAULTS["blur_radius"])
    power = int(cfg.get("blur_power") or _DEFAULTS["blur_power"])
    return (
        "split=2[cbg][cfg];"
        f"[cbg]{crop},scale={canvas_width}:{canvas_height}:force_original_aspect_ratio=increase,"
        f"crop={canvas_width}:{canvas_height},boxblur={radius}:{power}[cbgo];"
        f"[cfg]{crop},{fit}[cfgo];"
        f"[cbgo][cfgo]overlay=(W-w)/2:(H-h)/2,{tail}"
    )


# プレビュー用の配置 (画面が同じ計算を使うため / §5.6 / §5.8)。
# 戻り値: [{"src": (x, y, w, h), "dest": (x, y, w, h)}, ...]
def preview_rects(layout, canvas_width, canvas_height):
    mode = str(layout.get("mode") or MODE_SINGLE)
    frames = layout.get("frames") or []
    dests = dest_rects(mode, canvas_width, canvas_height)

    rects = []
    for index, frame in enumerate(frames):
        src = _int_rect(frame.get("src") or (0, 0, 2, 2))
        if index < len(dests):
            dest = dests[index]
        else:
            dest = fit_single(src, canvas_width, canvas_height)
        rects.append({"src": src, "dest": tuple(int(v) for v in dest)})
    return rects


# ------------------------------------------------------------------
# 内部
# ------------------------------------------------------------------

def _crop_filter(rect):
    x, y, width, height = _int_rect(rect)
    return f"crop={width}:{height}:{x}:{y}"


def _int_rect(rect):
    x, y, width, height = (int(round(float(v))) for v in rect)
    return (x, y, max(width, 2), max(height, 2))


# 偶数へ丸める (yuv420p は奇数の幅・高さを扱えない)
def _even(value):
    return max(int(value) // 2 * 2, 2)


def _clamp(value, default, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


def _clamp_int(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)
