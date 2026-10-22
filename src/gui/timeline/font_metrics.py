# フォントの縦メトリクスを読み、libass (VSFilter 互換) と同じ字の大きさを出す
# (ver8 resolve8 §5)。
#
# なぜ要るか:
#   Qt の setPixelSize(n) は em を n px にするが、libass は
#   「usWinAscent + usWinDescent が Fontsize になる」ようスケールする。
#   同梱フォント (07にくまるフォント) では 1080/1000 = 1.08 倍の差があり、
#   同じ Fontsize でもプレビューの字だけが約 8% 大きく見えていた。
#   行送りも同様で、Qt 側は pixelSize x 1.2 固定だったため行数ぶん誤差が溜まる。
#
# 立ち位置:
#   出力側 (libass) は別プロセスで自前にメトリクスを読むため、本モジュールは
#   **プレビュー専用**である。ASS の生成・compositor・comment_decor には一切
#   関与しない (箱の推定値は出力と共有したままにする)。
#
# 読めない環境では必ず None を返し、呼び出し側は従来の em 基準へ落ちる。
# プレビューを落とさないことを最優先にする。
import struct

from PySide6.QtGui import QRawFont

from ...utils.logger import get_logger

_logger = get_logger(__name__)

# メトリクスの取り方
#   "libass" : usWinAscent + usWinDescent を Fontsize とみなす (出力と一致)
#   "em"     : em = Fontsize (従来の Qt 近似)
MODE_LIBASS = "libass"
MODE_EM = "em"

# 同じフォントなら結果は変わらないため覚えておく (鍵: 書体名 + 太字 + 斜体)
_CACHE = {}
# 読めなかったフォントを 1 回だけログに出すための控え
_WARNED = set()

# sfnt テーブルの必要長とオフセット (OpenType 仕様)
_HEAD_MIN = 20          # head.unitsPerEm  = uint16 @18
_OS2_MIN = 78           # OS/2.sTypoAscender..usWinDescent
_HHEA_MIN = 10          # hhea.ascender/descender/lineGap = int16 x3 @4


def _cache_key(font):
    return (font.family(), bool(font.bold()), bool(font.italic()),
            font.styleName())


# フォントの縦メトリクスを読む。読めなければ None。
#   upem        : unitsPerEm (head)
#   win_height  : usWinAscent + usWinDescent (OS/2) = libass が Fontsize とみなす量
#   hhea_height : ascender - descender + lineGap (hhea) = 1 行の送り幅の素
#   typo_height : sTypoAscender - sTypoDescender + sTypoLineGap (OS/2)
#                 USE_TYPO_METRICS が立つフォントへ将来対応するために持つ (§16 Q12)
def vertical_metrics(font):
    key = _cache_key(font)
    if key in _CACHE:
        return _CACHE[key]
    metrics = _read_metrics(font)
    if metrics is None and key not in _WARNED:
        _WARNED.add(key)
        _logger.debug("フォントの縦メトリクスを読めませんでした "
                      "(em 基準で描きます): %s", font.family())
    _CACHE[key] = metrics
    return metrics


def _read_metrics(font):
    try:
        raw = QRawFont.fromFont(font)
        if raw is None or not raw.isValid():
            return None
        head = bytes(raw.fontTable("head") or b"")
        os2 = bytes(raw.fontTable("OS/2") or b"")
        hhea = bytes(raw.fontTable("hhea") or b"")
        if len(head) < _HEAD_MIN or len(os2) < _OS2_MIN or len(hhea) < _HHEA_MIN:
            return None
        upem = struct.unpack(">H", head[18:20])[0]
        typo_asc, typo_desc, typo_gap = struct.unpack(">hhh", os2[68:74])
        win_asc, win_desc = struct.unpack(">HH", os2[74:78])
        hhea_asc, hhea_desc, hhea_gap = struct.unpack(">hhh", hhea[4:10])
    except Exception:  # noqa: BLE001 (環境差で読めないことは普通にある)
        return None

    win_height = int(win_asc) + int(win_desc)
    if upem <= 0 or win_height <= 0:
        return None
    return {
        "upem": int(upem),
        "win_height": win_height,
        "hhea_height": int(hhea_asc) - int(hhea_desc) + int(hhea_gap),
        "typo_height": int(typo_asc) - int(typo_desc) + int(typo_gap),
    }


# ASS Fontsize (size_px) に対し、libass と同じ字の大きさになる Qt の pixelSize。
#   = size_px x upem / (usWinAscent + usWinDescent)
# メトリクスが読めない / mode が "em" のときは size_px をそのまま返す。
def libass_pixel_size(font, size_px, mode=MODE_LIBASS):
    size_px = _positive(size_px)
    if size_px <= 0 or mode != MODE_LIBASS:
        return size_px
    metrics = vertical_metrics(font)
    if metrics is None:
        return size_px
    scaled = round(size_px * metrics["upem"] / metrics["win_height"])
    return max(int(scaled), 1)


# 1 行あたりの送り幅 (px)。
#   mode="em"     : size_px x ratio (従来。ratio = comment_bg_line_height_ratio)
#   mode="libass" : usWinAscent + usWinDescent ぶん積む = size_px そのもの
#
# libass は「usWinAscent + usWinDescent が Fontsize になる」ようスケールしたうえで、
# その量ぶん行を送る。したがって行送りは Fontsize と等しくなる。
# 同梱フォントでの ink 実測 (1/2/3 行・位置指定あり/なし) では、この基準で
# libass と高さが完全一致し、縦位置のずれも 0 になった (ver8 resolve8 §6)。
# hhea の行送り (= 1000/1080 x Fontsize) では 1 行あたり約 3px 足りなかった。
#
# メトリクスが読めないときは従来値 (size_px x ratio) へ落ちる。字の大きさも
# em 基準のまま (= 大きめ) になるため、行送りだけ詰めると行が重なるためである。
def line_advance_px(font, size_px, mode=MODE_LIBASS, ratio=1.2):
    size_px = _positive(size_px)
    ratio = float(ratio) if float(ratio) > 0 else 1.2
    fallback = size_px * ratio
    if size_px <= 0 or mode != MODE_LIBASS:
        return fallback
    if vertical_metrics(font) is None:
        return fallback
    return size_px


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0
