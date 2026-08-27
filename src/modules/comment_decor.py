# コメント字幕の装飾: 背景ボックスとアイコン (docs/request/ver3/resolve11.md §5.5 / §5.11)
# プレビュー(Qt)・焼き込み(ffmpeg)・高精度プレビューが同じ幾何を使うための唯一の計算元。
# ここで一度だけ計算し、全経路が同じ数値を見ることで「画面と出力がズレる」ことを防ぐ (§3 D-5)。
#
# Qt にも ffmpeg にも依存しない純 Python として書く (テストから直接叩ける)。
# subtitle_generator からこのモジュールを import するため、逆向きの import はしない
# (ASS 色文字列の分解だけは循環 import を避けるためここへ小さく持つ)。
import math
import os
import sys
import unicodedata

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 対象の役割 (この役割の字幕にだけ背景とアイコンを付ける)
ROLE_COMMENT = "comment"

# 同梱アイコンのファイル名 (src/ 直下に置く / §5.10)
_DEFAULT_ICON_NAME = "comment_icon.png"

# ASS の配置 (テンキー) → 垂直方向の寄せ
_ALIGN_BOTTOM = (1, 2, 3)
_ALIGN_TOP = (7, 8, 9)

# 同じ警告を出し続けないための記録 (毎フレームの再描画でログを埋めないため)
_warned = set()


# 同一メッセージは 1 回だけ WARNING を出す
def _warn_once(key, message, *args):
    if key in _warned:
        return
    _warned.add(key)
    _logger.warning(message, *args)


# テスト用: 警告の抑止記録を消す
def reset_warnings():
    _warned.clear()


def _to_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, low, high):
    if high < low:
        return low
    return max(low, min(value, high))


# 全角として数える文字か (East Asian Width が F/W/A のもの)
# 日本語ゴシックでは曖昧幅(A: ○ × 等)も全角送りになるため全角側へ寄せる。
def _is_full_width(char):
    return unicodedata.east_asian_width(char) in ("F", "W", "A")


# 役割がコメントか (role 未指定は配信者扱い)
def is_comment(item):
    return str((item or {}).get("role", "")) == ROLE_COMMENT


# アイコン画像の実体パスを凍結/非凍結の双方で解決する (§5.10)
# 非凍結: src/comment_icon.png (このファイルの 1 つ上の階層)
# 凍結  : datas で同梱した _internal/src/comment_icon.png を sys._MEIPASS 基準で解決する
#         (app.ico と同じ方式 / main_window._resolve_app_icon_path)
# 設定 comment_icon_path が絶対パスならそれを優先する。見つからなければ None。
def resolve_icon_path(eff_cfg):
    configured = str((eff_cfg or {}).get("comment_icon_path", "") or "").strip()
    if configured and os.path.isabs(configured):
        return configured if os.path.exists(configured) else None
    if getattr(sys, "frozen", False):
        base = os.path.join(getattr(sys, "_MEIPASS", ""), "src")
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, configured or _DEFAULT_ICON_NAME)
    return path if os.path.exists(path) else None


# 字幕 1 件のフォントサイズ (個別指定 > 設定の既定)
def _font_size(item, eff_cfg):
    size = _to_int((item or {}).get("font_size"), 0)
    if size > 0:
        return size
    return _to_int((eff_cfg or {}).get("font_size"), 48) or 48


# 表示される行の一覧 (ASS の改行 \N / \n と実改行をすべて改行として扱う)
def _lines(text):
    normalized = str(text or "").replace("\\N", "\n").replace("\\n", "\n")
    return normalized.split("\n")


# 文字の外接矩形を推定する (§3 D-10)
# 【重要】実測ではなく推定。文字の実寸を決めるのは libass で Python からは測れないため、
#   日本語ゴシックが全角 = 1em の等幅に近いことを利用して font_size 比で見積もる。
#   プレビュー(Qt)も焼き込みも「この推定値だけ」を見るので、両者は必ず一致する。
#   ASS ヘッダは WrapStyle: 2 (自動折り返し無し) のため、行数は \N の数 + 1 で厳密。
# 戻り値 {"width": px, "height": px, "lines": 行数}
def text_extent(item, eff_cfg, text=None):
    raw = text if text is not None else (item or {}).get("text", "")
    lines = _lines(raw)
    size = _font_size(item, eff_cfg)
    full = _to_float((eff_cfg or {}).get("comment_bg_char_width_full"), 1.0)
    half = _to_float((eff_cfg or {}).get("comment_bg_char_width_half"), 0.5)
    ratio = _to_float((eff_cfg or {}).get("comment_bg_line_height_ratio"), 1.2)

    widest = 0.0
    for line in lines:
        em = 0.0
        for char in line:
            em += full if _is_full_width(char) else half
        widest = max(widest, em * size)
    # 推定は必ず切り上げる (狭い側に外すと文字が背景からはみ出して見えるため)
    return {
        "width": int(math.ceil(widest)),
        "height": int(math.ceil(len(lines) * size * ratio)),
        "lines": len(lines),
    }


# 文字列の「左端 X」と「垂直中心 Y」を求める (§3 D-5 の表)
#   位置指定あり (ドラッグ済み): \an4\pos がそのまま左中央アンカー = 厳密
#   位置指定なし・an4 (既定)   : 左端 = comment_margin_l / 中心 = キャンバス高さの半分 = 厳密
#   位置指定なし・an1 / an7    : 垂直位置のみ行高からの近似
def _text_anchor(item, eff_cfg, canvas_w, canvas_h, extent):
    pos_x = (item or {}).get("pos_x")
    pos_y = (item or {}).get("pos_y")
    if pos_x is not None and pos_y is not None:
        return ((float(pos_x) + 1.0) * canvas_w / 2.0,
                (1.0 - float(pos_y)) * canvas_h / 2.0)

    left = _to_float((eff_cfg or {}).get("comment_margin_l"),
                     _to_float((eff_cfg or {}).get("margin_l"), 40.0))
    alignment = _to_int((eff_cfg or {}).get("comment_alignment"), 4)
    margin_v = _to_float((eff_cfg or {}).get("comment_margin_v"),
                         _to_float((eff_cfg or {}).get("margin_v"), 60.0))
    half_height = extent["height"] / 2.0
    if alignment in _ALIGN_TOP:
        mid = margin_v + half_height
    elif alignment in _ALIGN_BOTTOM:
        mid = canvas_h - margin_v - half_height
    else:
        mid = canvas_h / 2.0
    return left, mid


# アイコン 1 件ぶんの配置を返す。表示しない場合は None。
# 戻り値 {"x": 左端px, "y": 上端px, "size": 一辺px, "clamped": bool}
def icon_box(item, eff_cfg, canvas_w, canvas_h):
    if not is_comment(item):
        return None
    if not (eff_cfg or {}).get("comment_icon_enabled", True):
        return None
    size = _to_int((eff_cfg or {}).get("comment_icon_size_px"), 100)
    if size <= 0:
        return None
    size = min(size, int(min(canvas_w, canvas_h)))
    gap = max(_to_int((eff_cfg or {}).get("comment_icon_gap_px"), 50), 0)

    extent = text_extent(item, eff_cfg)
    text_left, text_mid = _text_anchor(item, eff_cfg, canvas_w, canvas_h, extent)
    x = text_left - gap - size
    y = text_mid - size / 2.0
    # はみ出し防止 (要望「画像がはみ出さないように注意」)。詰めたら 1 回だけ警告する。
    cx = _clamp(x, 0.0, float(canvas_w - size))
    cy = _clamp(y, 0.0, float(canvas_h - size))
    clamped = abs(cx - x) > 0.01 or abs(cy - y) > 0.01
    if clamped:
        _warn_once(
            "icon_clamp",
            "コメントアイコンがキャンバスに収まらないため位置を詰めました "
            "(要求 x=%.1f y=%.1f → x=%.1f y=%.1f)。"
            "subtitle.comment_margin_l を大きくすると解消します", x, y, cx, cy)
    return {"x": cx, "y": cy, "size": size, "clamped": clamped}


# 背景ボックス (アイコン + 文字 + 内側余白) を返す。出さない場合は None。
# 戻り値 {"x", "y", "w", "h", "radius", "clamped"}
def background_box(item, eff_cfg, canvas_w, canvas_h, text=None):
    if not is_comment(item):
        return None
    if not (eff_cfg or {}).get("comment_bg_enabled", True):
        return None

    pad = max(_to_int((eff_cfg or {}).get("comment_bg_padding_px"), 24), 0)
    extent = text_extent(item, eff_cfg, text=text)
    text_left, text_mid = _text_anchor(item, eff_cfg, canvas_w, canvas_h, extent)
    icon = icon_box(item, eff_cfg, canvas_w, canvas_h)

    # 中身 (アイコン + 文字) の外接矩形を求めてから内側余白を足す
    left = min(icon["x"], text_left) if icon else text_left
    right = text_left + extent["width"]
    half = max(extent["height"] / 2.0, (icon["size"] / 2.0) if icon else 0.0)
    x = left - pad
    y = text_mid - half - pad
    w = (right - left) + pad * 2
    h = half * 2 + pad * 2

    # キャンバスに収める (文字の位置は ASS が決めるため、詰めるのは箱だけ)
    over = w > canvas_w or h > canvas_h
    w = min(w, float(canvas_w))
    h = min(h, float(canvas_h))
    cx = _clamp(x, 0.0, float(canvas_w) - w)
    cy = _clamp(y, 0.0, float(canvas_h) - h)
    clamped = over or abs(cx - x) > 0.01 or abs(cy - y) > 0.01
    if clamped:
        _warn_once(
            "bg_clamp",
            "コメント背景がキャンバスに収まらないため詰めました "
            "(推定幅 %dpx / 使える幅 %dpx)。"
            "font_size か max_line_length を下げると解消します",
            int(right - left + pad * 2), int(canvas_w))

    radius = max(_to_int((eff_cfg or {}).get("comment_bg_radius_px"), 24), 0)
    # 角丸が短辺の半分を超えると描画が破綻するため丸める
    radius = int(min(radius, w / 2.0, h / 2.0))
    return {"x": cx, "y": cy, "w": w, "h": h,
            "radius": radius, "clamped": clamped}


# 角丸矩形の ASS 描画コマンド (\p1 の中身) を組み立てる (§3 D-9)
# 角は制御点を角に置いたベジェ (b) で丸める。r=0 なら直角の矩形。
def rounded_rect_path(w, h, r):
    w = int(round(w))
    h = int(round(h))
    r = int(round(min(r, w / 2.0, h / 2.0))) if w > 0 and h > 0 else 0
    if w <= 0 or h <= 0:
        return ""
    if r <= 0:
        return f"m 0 0 l {w} 0 l {w} {h} l 0 {h}"
    return (
        f"m {r} 0 "
        f"l {w - r} 0 b {w} 0 {w} 0 {w} {r} "
        f"l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
        f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} "
        f"l 0 {r} b 0 0 0 0 {r} 0"
    )


# ASS 色文字列 (&HAABBGGRR / &HBBGGRR) を (BBGGRR, AA) へ分解する
# subtitle_generator._split_ass_color と同じ規約。循環 import を避けるためここへ持つ。
def _split_ass_color(value):
    text = str(value or "").strip().upper()
    if not text.startswith("&H"):
        return None, None
    digits = text[2:].rstrip("&")
    if not all(c in "0123456789ABCDEF" for c in digits):
        return None, None
    if len(digits) == 8:
        return digits[2:], digits[0:2]
    if len(digits) == 6:
        return digits, None
    return None, None


# 背景 (角丸の箱) の Dialogue 本文を返す。出さない場合は None。
# 時刻・Style 名・Layer を付けて 1 行にするのは呼び出し側 (build_subtitle_file) の仕事。
# 【重要】\bord0\shad0 と縁/影の透過で、Style の縁取りが箱に付かないようにする。
def build_background_text(item, eff_cfg, canvas_w, canvas_h, text=None):
    box = background_box(item, eff_cfg, canvas_w, canvas_h, text=text)
    if box is None:
        return None
    path = rounded_rect_path(box["w"], box["h"], box["radius"])
    if not path:
        return None
    bgr, alpha = _split_ass_color((eff_cfg or {}).get("comment_bg_color"))
    if bgr is None:
        bgr, alpha = "000000", "80"
    fill = f"\\1c&H{bgr}&"
    if alpha is not None:
        fill += f"\\1a&H{alpha}&"
    return (
        f"{{\\an7\\pos({box['x']:.1f},{box['y']:.1f}){fill}"
        f"\\bord0\\shad0\\3a&HFF&\\4a&HFF&\\p1}}{path}{{\\p0}}"
    )


# ass / subtitles フィルタと同じ 2 段階エスケープ ('\'→'/', ':'→'\:')
def _escape_filter_path(path):
    return str(path).replace("\\", "/").replace(":", "\\:")


# アイコンを合成する ffmpeg のフィルタチェーン断片を返す (§3 D-6)
#   items      : SubtitleClip.to_item() 互換の辞書配列 (start/end/role/pos_x/pos_y/…)
#   in_label   : 入力ラベル (例 "[vsub]")
#   out_label  : 出力ラベル (空文字なら -vf の最終出力として無ラベルにする)
#   with_enable: False なら表示時間の指定を付けない (静止画プレビュー用)
#   prefix     : 中間ラベルの接頭辞。1 つの filtergraph で複数回呼ぶときに衝突させないため
# 戻り値 (chains: list[str], count: 件数, groups: 位置の種類数)
#
# 【重要】アイコンは movie ソースフィルタで filtergraph の内側から読む。
#   -i を足す方式だとコメント 1 件ごとに入力が増えて破綻するため。
#   表示位置が同じコメントは 1 つの overlay へまとめ、enable を論理和で連結する
#   (既定運用では位置は 1 種類なので overlay も 1 本で済む)。
def build_icon_chains(items, eff_cfg, canvas_w, canvas_h,
                      in_label, out_label, with_enable=True, prefix="cic"):
    groups = {}
    size = None
    count = 0
    for item in (items or []):
        box = icon_box(item, eff_cfg, canvas_w, canvas_h)
        if box is None:
            continue
        size = box["size"]
        count += 1
        key = (round(box["x"], 1), round(box["y"], 1))
        groups.setdefault(key, []).append(
            (_to_float(item.get("start")), _to_float(item.get("end"))))
    if not groups:
        return [], 0, 0

    icon_path = resolve_icon_path(eff_cfg)
    if not icon_path:
        _warn_once("icon_missing",
                   "コメントアイコンの画像が見つからないため表示しません "
                   "(設定 subtitle.comment_icon_path で指定できます)")
        return [], 0, 0

    total = len(groups)
    source = (
        f"movie='{_escape_filter_path(icon_path)}',format=rgba,"
        f"scale=w={size}:h={size}:force_original_aspect_ratio=decrease,"
        f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
    )
    if total == 1:
        source += f"[{prefix}0]"
    else:
        source += ",split=" + str(total) + "".join(
            f"[{prefix}{i}]" for i in range(total))
    chains = [source]

    current = in_label
    for index, ((x, y), spans) in enumerate(groups.items()):
        label = out_label if index == total - 1 else f"[{prefix}o{index}]"
        overlay = f"overlay={x:.1f}:{y:.1f}"
        if with_enable:
            enable = "+".join(
                f"between(t,{start:.3f},{end:.3f})" for start, end in spans)
            overlay += f":enable='{enable}'"
        chains.append(f"{current}[{prefix}{index}]{overlay}{label}")
        current = label

    _logger.info("コメントアイコン: %d 件 / 位置 %d 種類", count, total)
    return chains, count, total
