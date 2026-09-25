# FCPXML 生成 (resolve20 §5.6 / resolve21 §5.2-§5.4)
# DaVinci Resolve へ「取り込み(Import)」できる 1 ファイルを組み立てる。
#   ・カット編集点 = 元ソースを参照する asset-clip の並び (spine)
#   ・字幕        = <caption> (ITT・字幕トラック) または <title> (Text+ 相当)
#   ・テーマ演出  = <title> (Text+ 相当)
# 接続要素 (title/caption) は FCPXML 仕様に従い「担当する asset-clip の子要素」とし、
# offset は親クリップのローカル時間 (ソース start 基準) で表す (resolve21 §5.2)。
# 時間は全て整数フレームへ量子化してから累積し、丸め誤差を蓄積させない (§5.3)。
# 本モジュールは副作用を持たない純関数群とし、単体で検証できるようにする
# (ファイル書き出し・設定読み出しは resolve_export が担当する)。
import pathlib
import xml.etree.ElementTree as ET

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# resources 内の固定 ID (1 ソース + 1 フォーマット + 1 タイトル effect のみ生成する)
_FORMAT_ID = "r1"
_ASSET_ID = "r2"
_EFFECT_ID = "r3"

# 既定のタイトル effect UID (Text+ 用 UID 未確定時のフォールバック / resolve20 §10-9)
# Resolve は取り込み時に Basic Title を Text+ 相当のテキストへ変換する。
DEFAULT_TITLE_EFFECT_UID = (
    ".../Titles.localized/Bumper:Opener.localized/"
    "Basic Title.localized/Basic Title.moti"
)
# effect 要素の表示名 (Resolve のタイムライン上でのエフェクト名)
_TITLE_EFFECT_NAME = "Basic Title"

# caption の既定ロール (ITT・日本語 / resolve21 §5.4)
DEFAULT_CAPTION_ROLE = "iTT?captionFormat=ITT.ja"

# 生成する XML の既定値 (spec 側の欠落を安全に補完する)
_DEFAULT_VERSION = "1.9"
_DEFAULT_FPS = 60
_DEFAULT_WIDTH = 1920
_DEFAULT_HEIGHT = 1080
# 色文字列が不正な場合のフォールバック (白)
_DEFAULT_RGBA = (1.0, 1.0, 1.0, 1.0)
# タイトルを重ねるレーン番号 (正値 = 親クリップへ接続する connected clip)
_TITLE_LANE = 1

# 透かしはタイトルより上のレーンへ置く (ver5 resolve §5.5)
_OVERLAY_LANE = 2

# 重なり素材 (透かし) の resources id。asset と format を 2 つずつ採番する
_OVERLAY_ASSET_ID = "r10"
_OVERLAY_FORMAT_ID = "r11"

# クランプ発生の周知ログを 1 回だけ出すためのフラグ (resolve21 §7)
_warned_once = {"clamp": False}


# 秒を整数フレームへ量子化する (§5.3。不正値は 0 フレーム)
def to_frames(seconds, fps):
    try:
        return int(round(float(seconds) * int(fps)))
    except (TypeError, ValueError):
        return 0


# 整数フレームを FCPXML の有理数時刻表記へ整形する (例 60fps の 30 フレーム → "30/60s")
def frames_to_text(frames, fps):
    frames = int(frames)
    if frames <= 0:
        return "0s"
    if frames % fps == 0:
        return f"{frames // fps}s"
    return f"{frames}/{fps}s"


# 秒を FCPXML の有理数時刻表記へ量子化して返す (例 60fps → "123/60s")
# 全時刻を最近傍フレームへ丸めることで Resolve 側のフレーム境界と一致させる (§5.2)。
def format_time(seconds, fps):
    fps = int(fps) if int(fps or 0) > 0 else _DEFAULT_FPS
    return frames_to_text(max(to_frames(seconds, fps), 0), fps)


# 尺を 1 フレーム以上に丸めて返す (長さ 0 のクリップ/タイトルを作らない)
def format_duration(seconds, fps):
    fps = int(fps) if int(fps or 0) > 0 else _DEFAULT_FPS
    return frames_to_text(max(to_frames(seconds, fps), 1), fps)


# "#RRGGBB" を FCPXML の RGBA (0-1 実数4値・空白区切り) へ変換する
def hex_to_rgba(hex_color, alpha=1.0):
    value = str(hex_color or "").strip().lstrip("#")
    if len(value) != 6:
        r, g, b, a = _DEFAULT_RGBA
        return _format_rgba(r, g, b, alpha if alpha is not None else a)
    try:
        r = int(value[0:2], 16) / 255.0
        g = int(value[2:4], 16) / 255.0
        b = int(value[4:6], 16) / 255.0
    except ValueError:
        r, g, b, _a = _DEFAULT_RGBA
    return _format_rgba(r, g, b, alpha)


# RGBA 実数を FCPXML の属性値表記へ整形する
def _format_rgba(r, g, b, a):
    alpha = 1.0 if a is None else max(0.0, min(1.0, float(a)))
    return f"{r:.6f} {g:.6f} {b:.6f} {alpha:.6f}"


# ローカル絶対パスを file:// URI へ変換する (Windows のドライブレターも正しく符号化する)
def _file_uri(path):
    try:
        return pathlib.Path(str(path)).absolute().as_uri()
    except (ValueError, OSError):
        # URI 化できない場合でも生成は止めず、そのまま文字列として埋め込む
        return str(path)


# ------------------------------------------------------------------
# タイムラインのフレーム量子化とアンカリング (resolve21 §5.2/§5.3)
# ------------------------------------------------------------------

# クリップ列を整数フレームへ量子化し、タイムライン位置を量子化済み尺の累積で確定する
# 戻り値: [{"name", "offset_f"(タイムライン位置), "start_f"(ソース in 点), "dur_f"}, ...]
def _quantize_clips(clips, fps):
    quantized = []
    cursor_f = 0
    for index, clip in enumerate(clips, 1):
        dur_f = max(to_frames(clip.get("duration", 0.0), fps), 1)
        quantized.append({
            "name": str(clip.get("name") or f"cut{index}"),
            "offset_f": cursor_f,
            "start_f": max(to_frames(clip.get("start", 0.0), fps), 0),
            "dur_f": dur_f,
        })
        cursor_f += dur_f
    return quantized


# タイムライン区間 [start_f, end_f) を担当クリップへ割り当てる (§5.2)
# 戻り値: [(クリップ index, 親ローカル offset_f, dur_f), ...]
#   ・クリップ境界を跨ぐ場合は分割する (1 フレーム未満の断片は破棄)
#   ・タイムライン終端以降に落ちた場合は終端側へクランプして WARNING (1 回 / §7)
def _assign_to_clips(fclips, start_f, end_f, timeline_f):
    if not fclips:
        return []
    if end_f <= start_f:
        end_f = start_f + 1
    if start_f >= timeline_f:
        # タイムライン外 (丸め誤差の端数など) → 最終クリップ側へクランプする
        duration_f = end_f - start_f
        start_f = max(timeline_f - duration_f, 0)
        end_f = timeline_f
        if not _warned_once["clamp"]:
            _warned_once["clamp"] = True
            _logger.warning(
                "タイムライン終端を超える字幕をクリップ末尾へクランプしました "
                "(丸め誤差の端数など)。")
    pieces = []
    for index, fc in enumerate(fclips):
        overlap_start = max(start_f, fc["offset_f"])
        overlap_end = min(end_f, fc["offset_f"] + fc["dur_f"])
        if overlap_end - overlap_start >= 1:
            # 親ローカル時間 = 親のソース in 点 + (タイムライン時刻 - 親のタイムライン位置)
            local_f = fc["start_f"] + (overlap_start - fc["offset_f"])
            pieces.append((index, local_f, overlap_end - overlap_start))
        elif overlap_end > overlap_start:
            _logger.debug("1 フレーム未満の字幕断片を破棄しました")
    return pieces


# クリップ列の合計尺 (タイムライン全長・フレーム) を返す
def _total_frames(fclips):
    return sum(fc["dur_f"] for fc in fclips)


# spec から FCPXML 文字列を生成して返す
# spec = {
#   "version": str, "event_name": str, "project_name": str,
#   "fps": int, "width": int, "height": int,
#   "source": {"path": 絶対パス, "name": str, "duration": float},
#   "clips":  [{"start": ソース内イン点秒, "duration": 尺秒, "name": str}, ...]  ← spine の順に連結
#   "titles": [{"offset": タイムライン秒, "duration": 尺秒, "text": str,
#               "font": str, "font_size": int, "color": "#RRGGBB",
#               "stroke_color": "#RRGGBB", "stroke_alpha": float, "stroke_width": int,
#               "bold": bool, "italic": bool, "underline": bool,
#               "align": "left"|"center"|"right", "position": (x, y) or None,
#               "name": str}, ...]
#   "captions": [{"offset": タイムライン秒, "duration": 尺秒, "text": str,
#                 "font": str, "font_size": int, "color": "#RRGGBB",
#                 "bold": bool, "italic": bool, "underline": bool,
#                 "placement": "bottom"|"top"|"left"|"right", "name": str}, ...]
#   "overlays": [{"path": 素材の絶対パス, "name": str,
#                 "offset": タイムライン秒, "duration": 尺秒,
#                 "source_width": int, "source_height": int, "scale": float,
#                 "position": (x, y), "opacity": float}, ...]  ← 透かし (ver5 resolve §5.5)
#   "caption_role": str (例 "iTT?captionFormat=ITT.ja"),
#   "title_effect_uid": str,
# }
def build_fcpxml(spec):
    spec = spec or {}
    fps = int(spec.get("fps") or _DEFAULT_FPS)
    if fps <= 0:
        fps = _DEFAULT_FPS
    width = int(spec.get("width") or _DEFAULT_WIDTH)
    height = int(spec.get("height") or _DEFAULT_HEIGHT)
    clips = [c for c in (spec.get("clips") or []) if float(c.get("duration", 0) or 0) > 0]
    titles = list(spec.get("titles") or [])
    captions = list(spec.get("captions") or [])
    overlays = [o for o in (spec.get("overlays") or [])
                if float(o.get("duration", 0) or 0) > 0 and o.get("path")]
    caption_role = str(spec.get("caption_role") or DEFAULT_CAPTION_ROLE)
    source = spec.get("source") or {}

    # 以降の時間計算は全て整数フレームで行う (§5.3)
    fclips = _quantize_clips(clips, fps)
    timeline_f = _total_frames(fclips)

    root = ET.Element("fcpxml", {"version": str(spec.get("version") or _DEFAULT_VERSION)})

    # ---- resources (フォーマット / ソース素材 / タイトル effect) ----
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format", {
        "id": _FORMAT_ID,
        "name": f"AutoEdit{width}x{height}p{fps}",
        "frameDuration": f"1/{fps}s",
        "width": str(width),
        "height": str(height),
    })

    # asset の duration は元ソース全長 (不明ならタイムライン全長で代用する)
    source_f = max(to_frames(source.get("duration", 0.0), fps), 0) or timeline_f
    asset = ET.SubElement(resources, "asset", {
        "id": _ASSET_ID,
        "name": str(source.get("name") or "source"),
        "start": "0s",
        "duration": frames_to_text(max(source_f, 1), fps),
        "hasVideo": "1",
        "hasAudio": "1",
        "format": _FORMAT_ID,
    })
    ET.SubElement(asset, "media-rep", {
        "kind": "original-media",
        "src": _file_uri(source.get("path", "")),
    })

    if titles:
        ET.SubElement(resources, "effect", {
            "id": _EFFECT_ID,
            "name": _TITLE_EFFECT_NAME,
            "uid": str(spec.get("title_effect_uid") or DEFAULT_TITLE_EFFECT_UID),
        })

    # 重なり素材 (透かし) の asset / format。静止画のため duration は 0s とする。
    for index, overlay in enumerate(overlays):
        asset_id, format_id = _overlay_ids(index)
        ET.SubElement(resources, "format", {
            "id": format_id,
            "name": f"AutoEditOverlay{index + 1}",
            "width": str(int(overlay.get("source_width") or width)),
            "height": str(int(overlay.get("source_height") or height)),
        })
        overlay_asset = ET.SubElement(resources, "asset", {
            "id": asset_id,
            "name": str(overlay.get("name") or "overlay"),
            "start": "0s",
            "duration": "0s",
            "hasVideo": "1",
            "videoSources": "1",
            "format": format_id,
        })
        ET.SubElement(overlay_asset, "media-rep", {
            "kind": "original-media",
            "src": _file_uri(overlay.get("path", "")),
        })

    # ---- library / event / project / sequence ----
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": str(spec.get("event_name") or "Stretheus")})
    project = ET.SubElement(event, "project", {"name": str(spec.get("project_name") or "timeline")})
    sequence = ET.SubElement(project, "sequence", {
        "format": _FORMAT_ID,
        "duration": frames_to_text(max(timeline_f, 1), fps),
        "tcStart": "0s",
        "tcFormat": "NDF",
    })
    spine = ET.SubElement(sequence, "spine")

    # 本線: keep 区間ごとに元ソースを参照するクリップを連結する (= カット編集点 / §5.2)
    # offset は量子化済み尺の累積 (整数フレーム) で確定し、隙間/重なりを作らない (§5.3)
    clip_elements = []
    for fc in fclips:
        element = ET.SubElement(spine, "asset-clip", {
            "ref": _ASSET_ID,
            "name": fc["name"],
            "offset": frames_to_text(fc["offset_f"], fps),
            "start": frames_to_text(fc["start_f"], fps),
            "duration": frames_to_text(fc["dur_f"], fps),
        })
        clip_elements.append(element)

    # 接続要素は担当クリップの子要素とし、offset は親ローカル時間で表す (§5.2)。
    # text-style-def の id は文書全体で一意にする (分割断片ごとに採番)。
    style_seq = {"n": 0}

    # 字幕 caption (字幕トラック / §5.4)
    for caption in captions:
        _place_text_element(
            clip_elements, fclips, timeline_f, caption, fps,
            lambda parent, piece, item, sid: _append_caption(
                parent, item, sid, fps, piece, caption_role),
            style_seq,
        )

    # 字幕/テーマ title (Text+ 相当)
    for title in titles:
        _place_text_element(
            clip_elements, fclips, timeline_f, title, fps,
            lambda parent, piece, item, sid: _append_title(parent, item, sid, fps, piece),
            style_seq,
        )

    # 重なり素材 (透かし)。字幕より上のレーンへ置く (ver5 resolve §5.5)
    for index, overlay in enumerate(overlays):
        asset_id, _format_id = _overlay_ids(index)
        _place_text_element(
            clip_elements, fclips, timeline_f, overlay, fps,
            lambda parent, piece, item, _sid, ref=asset_id: _append_overlay(
                parent, item, ref, fps, piece),
            style_seq,
        )

    ET.indent(root, space="  ")
    _compact_text_nodes(root)
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n{body}\n'


# 接続要素 1 件を担当クリップへ配置する (跨ぎ分割・命名・スタイル id 採番の共通処理)
def _place_text_element(clip_elements, fclips, timeline_f, item, fps, appender, style_seq):
    if not clip_elements:
        return
    start_f = max(to_frames(item.get("offset", 0.0), fps), 0)
    end_f = start_f + max(to_frames(item.get("duration", 0.0), fps), 1)
    pieces = _assign_to_clips(fclips, start_f, end_f, timeline_f)
    base_name = str(item.get("name") or "text")
    for order, (clip_index, local_f, dur_f) in enumerate(pieces, 1):
        style_seq["n"] += 1
        # 分割された場合のみ枝番を付ける (通常は元の名前のまま)
        name = base_name if len(pieces) == 1 else f"{base_name}_{order}"
        appender(
            clip_elements[clip_index],
            {"offset_f": local_f, "dur_f": dur_f, "name": name},
            item,
            f"ts{style_seq['n']}",
        )


# 整形インデントが title/caption の本文へ混入しないよう <text> 内の空白を取り除く
# (<text> は本文そのものを表すため、改行/字下げが字幕テキストとして解釈されるのを防ぐ)
def _compact_text_nodes(root):
    for text_element in root.iter("text"):
        text_element.text = None
        for child in text_element:
            child.tail = None


# 重なり素材 1 件ぶんの resources id (asset, format) を返す
def _overlay_ids(index):
    return f"r{10 + index * 2}", f"r{11 + index * 2}"


# video 要素 (重なり素材 = 透かし) を親クリップへ追加する (ver5 resolve §5.5)
# 静止画のため start は 0s 固定。位置と倍率は adjust-transform、不透明度は adjust-blend で表す。
def _append_overlay(parent, overlay, ref, fps, piece):
    element = ET.SubElement(parent, "video", {
        "ref": ref,
        "lane": str(_OVERLAY_LANE),
        "offset": frames_to_text(piece["offset_f"], fps),
        "duration": frames_to_text(piece["dur_f"], fps),
        "start": "0s",
        "name": piece["name"],
    })

    scale = float(overlay.get("scale") or 1.0)
    attrs = {"scale": f"{scale:.4f} {scale:.4f}"}
    position = overlay.get("position")
    if position is not None:
        # 位置は title と同じ近似 (Resolve 取り込みで中央へ戻る場合がある / §5.5)
        attrs["position"] = f"{float(position[0]):.4f} {float(position[1]):.4f}"
    ET.SubElement(element, "adjust-transform", attrs)

    opacity = float(overlay.get("opacity", 1.0) or 0.0)
    if opacity < 1.0:
        ET.SubElement(element, "adjust-blend", {"amount": f"{opacity:.3f}"})
    return element


# title 要素 (テキスト本文 + スタイル定義 + 位置パラメータ) を親クリップへ追加する
# piece: {"offset_f": 親ローカル時間, "dur_f": 尺, "name": 表示名}
def _append_title(parent, title, style_id, fps, piece):
    element = ET.SubElement(parent, "title", {
        "ref": _EFFECT_ID,
        "lane": str(_TITLE_LANE),
        "offset": frames_to_text(piece["offset_f"], fps),
        "duration": frames_to_text(piece["dur_f"], fps),
        "name": piece["name"],
    })

    # 位置 (近似)。Resolve 取り込みで中央へ戻る場合があるが可能な範囲で付与する (§5.5)
    position = title.get("position")
    if position is not None:
        ET.SubElement(element, "param", {
            "name": "Position",
            "value": f"{float(position[0]):.4f} {float(position[1]):.4f}",
        })

    # 本文 (実改行を保持したままテキストノードへ入れる)
    text_element = ET.SubElement(element, "text")
    styled = ET.SubElement(text_element, "text-style", {"ref": style_id})
    styled.text = str(title.get("text", ""))

    # スタイル定義 (フォント/サイズ/色/装飾/整列)
    style_def = ET.SubElement(element, "text-style-def", {"id": style_id})
    attrs = {
        "font": str(title.get("font") or "Yu Gothic UI"),
        "fontSize": str(int(title.get("font_size") or 48)),
        "fontFace": "Regular",
        "fontColor": hex_to_rgba(title.get("color"), 1.0),
        "alignment": str(title.get("align") or "center"),
    }
    if title.get("bold"):
        attrs["bold"] = "1"
    if title.get("italic"):
        attrs["italic"] = "1"
    if title.get("underline"):
        attrs["underline"] = "1"
    stroke_width = int(title.get("stroke_width") or 0)
    if stroke_width > 0:
        attrs["strokeColor"] = hex_to_rgba(
            title.get("stroke_color"), title.get("stroke_alpha", 1.0)
        )
        # FCPXML の strokeWidth は外向きを正値で表す
        attrs["strokeWidth"] = str(stroke_width)
    ET.SubElement(style_def, "text-style", attrs)
    return element


# caption 要素 (ITT 字幕) を親クリップへ追加する (resolve21 §5.4)
# Resolve 取り込みで字幕トラックへ変換される。lane は付けず role でトラック分類される。
# text-style は ITT で表現できる範囲 (フォント/サイズ/色/太字/斜体/下線) のベストエフォート。
def _append_caption(parent, caption, style_id, fps, piece, role):
    element = ET.SubElement(parent, "caption", {
        "role": role,
        "offset": frames_to_text(piece["offset_f"], fps),
        "duration": frames_to_text(piece["dur_f"], fps),
        "name": piece["name"],
    })

    # 本文 (placement は上下左右 4 値のみ / 位置詳細は Resolve のトラックスタイルで調整)
    text_element = ET.SubElement(element, "text", {
        "placement": str(caption.get("placement") or "bottom"),
    })
    styled = ET.SubElement(text_element, "text-style", {"ref": style_id})
    styled.text = str(caption.get("text", ""))

    style_def = ET.SubElement(element, "text-style-def", {"id": style_id})
    attrs = {
        "font": str(caption.get("font") or "Yu Gothic UI"),
        "fontSize": str(int(caption.get("font_size") or 48)),
        "fontColor": hex_to_rgba(caption.get("color"), 1.0),
    }
    if caption.get("bold"):
        attrs["bold"] = "1"
    if caption.get("italic"):
        attrs["italic"] = "1"
    if caption.get("underline"):
        attrs["underline"] = "1"
    ET.SubElement(style_def, "text-style", attrs)
    return element
