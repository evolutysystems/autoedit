# Resolve 出力のオーケストレーション (resolve20 §5.3/§5.4/§5.5/§5.7, resolve21 §5.4-§5.7)
# 両画面 (クリップ用 字幕一覧 / アーカイブ用 結果画面) から呼び出され、
#   ① 編集点 (無音カットで残す区間) と字幕・スタイル・タイムライン諸元を収集し、
#   ② fcpxml_builder で FCPXML を生成し (字幕は mode に応じて caption/title へ変換)、
#   ③ 出力先へ原子的に書き出す (一時ファイル→成功時 rename)。SRT サイドカーも併記。
# 既存フロー (焼き込み・採点・パイプライン) には一切干渉しない読み取り専用機能。
import os

from ..archive import config as archive_config
from ..exceptions import AutoEditError, ExportError
from ..modules import ffmpeg_runner, output_profile, subtitle_generator
from ..timeline.timemap import TimeMap
from ..utils.logger import get_logger
from . import fcpxml_builder, srt_writer
from .config import resolve_config

_logger = get_logger(__name__)

# 生成拡張子 (FCPXML / SRT サイドカー)
_EXTENSION = ".fcpxml"
_SRT_EXTENSION = ".srt"
# 書き出し途中の一時ファイル拡張子 (部分ファイルを残さない / §7)
_TEMP_SUFFIX = ".tmp"
# イントロカードのバッファを出力へ含める最小尺 (clip_writer._build_intro_card と同一判定)
_MIN_INTRO_SEC = 0.1
# ASS テンキー配置 → 水平/垂直の寄せ判定 (§5.5)
_ALIGN_LEFT = (1, 4, 7)
_ALIGN_RIGHT = (3, 6, 9)
_ALIGN_BOTTOM = (1, 2, 3)
_ALIGN_TOP = (7, 8, 9)
# 字幕の出力方式の妥当値 (resolve21 §5.4)
_VALID_SUBTITLE_MODES = ("caption", "title", "both")
# 位置近似・演出非対応・運用案内などの周知ログを 1 回だけ出すためのフラグ (§8)
_warned_once = {
    "position": False, "intro": False,
    "subtitle_mode": False, "caption_format": False, "track_style": False,
}


# 出力ボタンを表示するかどうか (export.resolve.enabled)
def is_enabled(settings):
    return resolve_config(settings)["enabled"]


# ------------------------------------------------------------------
# 出力先の解決 (§5.7)
# ------------------------------------------------------------------

# 出力ディレクトリを解決する (general.output_directory。未設定ならソースと同じ場所)
def _output_dir(settings, source_path):
    out_dir = settings.get("general", {}).get("output_directory", "") if settings else ""
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        return out_dir
    return os.path.dirname(os.path.abspath(source_path))


# 出力ファイルパスを組み立てる ({prefix}_{stem}.fcpxml。prefix 空なら {stem}.fcpxml)
def default_output_path(settings, source_path, prefix):
    stem = os.path.splitext(os.path.basename(source_path))[0]
    name = f"{prefix}_{stem}{_EXTENSION}" if prefix else f"{stem}{_EXTENSION}"
    return os.path.join(_output_dir(settings, source_path), name)


# ------------------------------------------------------------------
# スタイル変換 (§5.5)
# ------------------------------------------------------------------

# ASS 色 (&HAABBGGRR) を ("#RRGGBB", 不透明度) へ変換する
# ASS は AA=00 が不透明・FF が透明のため反転して返す。"#RRGGBB" 表記もそのまま受ける。
def _ass_color_to_hex_alpha(value):
    text = str(value or "").strip()
    if text.startswith("#") and len(text) == 7:
        return text, 1.0
    if text.upper().startswith("&H"):
        digits = text[2:].rstrip("&")
        if len(digits) == 8:
            alpha = 1.0 - int(digits[0:2], 16) / 255.0
            bb, gg, rr = digits[2:4], digits[4:6], digits[6:8]
            return f"#{rr}{gg}{bb}".upper(), alpha
        if len(digits) == 6:
            bb, gg, rr = digits[0:2], digits[2:4], digits[4:6]
            return f"#{rr}{gg}{bb}".upper(), 1.0
    return "#000000", 1.0


# ASS テンキー配置からテキスト整列 (left/center/right) を返す
def _align_name(alignment):
    if alignment in _ALIGN_LEFT:
        return "left"
    if alignment in _ALIGN_RIGHT:
        return "right"
    return "center"


# ASS テンキー配置から caption の placement (上下) を返す (resolve21 §5.4)
# ITT の placement は上下左右 4 値のみのため、上段系のみ top・それ以外は bottom とする。
def _caption_placement(alignment):
    if alignment in _ALIGN_TOP:
        return "top"
    return "bottom"


# 字幕の出力方式を検証して返す (不正値は WARNING を 1 回出して "caption" / §6)
def _subtitle_mode(cfg):
    mode = cfg["subtitle"]["mode"]
    if mode in _VALID_SUBTITLE_MODES:
        return mode
    if not _warned_once["subtitle_mode"]:
        _warned_once["subtitle_mode"] = True
        _logger.warning(
            "未対応の字幕出力方式 export.resolve.subtitle.mode=%s のため caption で出力します",
            mode)
    return "caption"


# caption のロール文字列を組み立てる (書式は ITT のみ実装 / resolve21 §5.4)
def _caption_role(cfg):
    caption_format = cfg["subtitle"]["caption_format"]
    if caption_format.upper() != "ITT" and not _warned_once["caption_format"]:
        _warned_once["caption_format"] = True
        _logger.warning(
            "未対応の caption 書式 export.resolve.subtitle.caption_format=%s のため "
            "ITT で出力します", caption_format)
    return f"iTT?captionFormat=ITT.{cfg['subtitle']['language']}"


# ASS の配置(alignment)＋余白(margin) をタイトルの位置座標へ近似変換する (§5.5)
# 水平: 左寄せ→margin_l / 右寄せ→width-margin_r / 中央→width/2
# 垂直: 下→height-margin_v / 上→margin_v / 中央→height/2
# 変換係数 (座標空間・原点) は export.resolve.title 設定に従う (ハードコード回避)。
def _title_position(alignment, margins, width, height, title_cfg):
    margin_l, margin_r, margin_v = margins
    if alignment in _ALIGN_LEFT:
        x_px = float(margin_l)
    elif alignment in _ALIGN_RIGHT:
        x_px = float(width - margin_r)
    else:
        x_px = width / 2.0

    if alignment in _ALIGN_BOTTOM:
        y_px = float(height - margin_v)
    elif alignment in _ALIGN_TOP:
        y_px = float(margin_v)
    else:
        y_px = height / 2.0

    # 原点 (既定=画面中央・Y は上方向を正) へ移す
    if title_cfg.get("origin", "center") == "center":
        dx = x_px - width / 2.0
        dy = height / 2.0 - y_px
    else:
        dx = x_px
        dy = y_px

    if title_cfg.get("coord_space", "normalized") == "normalized":
        return (dx / (width / 2.0), dy / (height / 2.0))
    return (dx, dy)


# 表示テキストを組み立てる: ASS の改行 \N を実改行へ戻し、
# コメント役割は先頭ラベルを付ける (§10-8。caption/title/SRT で共通)
def _item_text(item, font_profile):
    text = str(item.get("text", "")).replace("\\N", "\n")
    if str(item.get("role", "streamer")) == "comment" and font_profile.comment_label:
        text = f"{font_profile.comment_label}{text}"
    return text


# 出力対象の字幕 items を出現順に返す (使用チェック済み・本文が空でないもののみ)
def _used_items(items):
    for order, item in enumerate(items or [], 1):
        if not item.get("use", True):
            continue
        if not str(item.get("text", "")).strip():
            continue
        yield order, item


# 字幕 item 1 件を title 辞書へ変換する (フォント/サイズ/色/装飾/位置)
# item 個別の font/font_size を優先し、無ければ FontProfile (設定) の既定を使う。
def _title_from_item(item, offset, font_profile, canvas, title_cfg, name):
    width, height = canvas
    role = str(item.get("role", "streamer"))
    # 塗り・縁とも item 個別指定を優先する (resolve6 §3-5)。無ければ役割の色。
    stroke_hex, stroke_alpha = _ass_color_to_hex_alpha(
        item.get("outline_color")
        or font_profile.role_outline_colors.get(role, font_profile.outline_color)
    )
    text = _item_text(item, font_profile)
    # 役割別の配置 (コメントは中央の左) を近似位置へ反映する (ver3 resolve11 §5.9)。
    # アイコンと背景は Resolve 側の枠に収まらないため出力しない (§9-Q5)。
    alignment, margin_l, margin_r, margin_v = font_profile.placement_for_role(role)
    return {
        "offset": offset,
        "duration": max(float(item.get("end", 0.0)) - float(item.get("start", 0.0)), 0.0),
        "text": text,
        "font": item.get("font") or font_profile.family,
        "font_size": item.get("font_size") or font_profile.size,
        "color": (item.get("color")
                  or font_profile.role_colors.get(role, font_profile.color_hex)),
        "stroke_color": stroke_hex,
        "stroke_alpha": stroke_alpha,
        "stroke_width": font_profile.outline_width,
        "bold": font_profile.bold,
        "italic": font_profile.italic,
        "underline": font_profile.underline,
        "align": _align_name(alignment),
        "position": _title_position(
            alignment, (margin_l, margin_r, margin_v), width, height, title_cfg,
        ),
        "name": name,
    }


# 使用チェック済みの字幕 items を title 群へ変換する
# base_offset: このクリップ本編がタイムライン上で始まる時刻 (items 時間はクリップ相対)。
def _titles_from_items(items, base_offset, font_profile, canvas, title_cfg, name_prefix):
    return [
        _title_from_item(
            item, base_offset + float(item.get("start", 0.0)),
            font_profile, canvas, title_cfg, f"{name_prefix}{order}",
        )
        for order, item in _used_items(items)
    ]


# 字幕 item 1 件を caption 辞書へ変換する (resolve21 §5.4)
# スタイルは ITT で表現できる範囲 (フォント/サイズ/役割色/太字/斜体/下線) のベストエフォート。
def _caption_from_item(item, offset, font_profile, name):
    role = str(item.get("role", "streamer"))
    return {
        "offset": offset,
        "duration": max(float(item.get("end", 0.0)) - float(item.get("start", 0.0)), 0.0),
        "text": _item_text(item, font_profile),
        "font": item.get("font") or font_profile.family,
        "font_size": item.get("font_size") or font_profile.size,
        # 塗りは item 個別指定を優先する (resolve6 §3-5)
        "color": (item.get("color")
                  or font_profile.role_colors.get(role, font_profile.color_hex)),
        "bold": font_profile.bold,
        "italic": font_profile.italic,
        "underline": font_profile.underline,
        # 役割別の配置 (コメントは中央の左) を反映する (ver3 resolve11 §5.9)
        "placement": _caption_placement(font_profile.placement_for_role(role)[0]),
        "name": name,
    }


# 使用チェック済みの字幕 items を caption 群へ変換する (resolve21 §5.4)
def _captions_from_items(items, base_offset, font_profile, name_prefix):
    return [
        _caption_from_item(
            item, base_offset + float(item.get("start", 0.0)),
            font_profile, f"{name_prefix}{order}",
        )
        for order, item in _used_items(items)
    ]


# 使用チェック済みの字幕 items を SRT サイドカー用 entries へ変換する (resolve21 §5.6)
# 時刻はタイムライン時刻 (= base_offset + クリップ相対)。テキストはラベル付与済み。
def _srt_entries_from_items(items, base_offset, font_profile):
    entries = []
    for _order, item in _used_items(items):
        start = base_offset + float(item.get("start", 0.0))
        entries.append({
            "start": start,
            "end": start + max(
                float(item.get("end", 0.0)) - float(item.get("start", 0.0)), 0.0),
            "text": _item_text(item, font_profile),
        })
    return entries


# テーマ演出 (中央テーマ / 左上タグ) の title 辞書を作る (§5.4)
# ブラー・黒帯は FCPXML で表現できないためテキストと区間のみを移送する。
def _theme_title(theme, card, subtitle_cfg, canvas, title_cfg, offset, duration, kind, name):
    width, height = canvas
    if kind == "intro":
        font = card["title_font_family"] or subtitle_cfg.get("font_family", "Yu Gothic UI")
        size = card["title_font_size"]
        color = card["title_color"]
        alignment = 5  # 中央 (an5 相当)
        margins = (0, 0, 0)
    else:
        font = card["tag_font_family"] or subtitle_cfg.get("font_family", "Yu Gothic UI")
        size = card["tag_font_size"]
        color = card["tag_color"]
        alignment = 7  # 左上 (an7 相当)
        margins = (card["tag_margin_l"], 0, card["tag_margin_v"])
    return {
        "offset": offset,
        "duration": duration,
        "text": str(theme),
        "font": font,
        "font_size": size,
        "color": color,
        "stroke_color": "#000000",
        "stroke_alpha": 1.0,
        "stroke_width": 0,
        "bold": True,
        "italic": False,
        "underline": False,
        "align": _align_name(alignment),
        "position": _title_position(alignment, margins, width, height, title_cfg),
        "name": name,
    }


# ------------------------------------------------------------------
# タイムライン組み立て (§5.2)
# ------------------------------------------------------------------

# 残す区間 [(start,end), ...] を asset-clip 用のクリップ列へ変換する
# source_offset: 区間がクリップ相対の場合に足し込む元ソース上の開始位置。
def _clips_from_segments(segments, name_prefix, source_offset=0.0):
    clips = []
    for order, segment in enumerate(segments or [], 1):
        start = float(segment[0])
        duration = max(float(segment[1]) - start, 0.0)
        if duration <= 0:
            continue
        clips.append({
            "start": source_offset + start,
            "duration": duration,
            "name": f"{name_prefix}{order}",
        })
    return clips


# 元ソースの総尺を取得する (失敗時は 0.0 = builder 側でタイムライン全長を代用)
def _probe_duration(source_path, settings):
    try:
        return float(ffmpeg_runner.probe_duration(source_path, settings.get("ffmpeg", {})))
    except (AutoEditError, OSError, TypeError, ValueError):
        _logger.warning("ソース総尺の取得に失敗 (タイムライン全長で代用): %s", source_path)
        return 0.0


# ソースパスの妥当性を検証する (未特定/不在はエクスポート中止 / §7)
def _validate_source(source_path):
    if not source_path:
        raise ExportError("元動画のパスが特定できないため Resolve 出力を中止しました。")
    if not os.path.exists(source_path):
        raise ExportError(f"元動画が見つかりません: {source_path}")


# spec の共通部 (諸元・ソース・イベント名) を作る
def _base_spec(source_path, settings, profile, cfg, project_name):
    fps = ffmpeg_runner.get_output_fps(settings.get("ffmpeg", {}))
    return {
        "version": cfg["fcpxml_version"],
        "event_name": cfg["event_name"],
        "project_name": project_name,
        "fps": fps,
        "width": int(profile["width"]),
        "height": int(profile["height"]),
        "source": {
            "path": os.path.abspath(source_path),
            "name": os.path.splitext(os.path.basename(source_path))[0],
            "duration": _probe_duration(source_path, settings),
        },
        "title_effect_uid": cfg["title_effect_uid"],
        "caption_role": _caption_role(cfg),
        "clips": [],
        "titles": [],
        "captions": [],
        "srt_entries": [],
    }


# 位置近似についての周知ログを 1 回だけ出す (§8)
def _warn_position_once():
    if _warned_once["position"]:
        return
    _warned_once["position"] = True
    _logger.warning(
        "字幕位置は配置(alignment)＋余白からの近似です。"
        "Resolve 取り込み時に位置が中央へ戻る場合があります (フォント/サイズ/色は保持)。"
    )


# テーマ演出の表現差についての周知ログを 1 回だけ出す (§8)
def _warn_intro_once():
    if _warned_once["intro"]:
        return
    _warned_once["intro"] = True
    _logger.warning(
        "テーマ演出はテキストと区間のみを出力します "
        "(ブラー・黒帯などの映像エフェクトは FCPXML で表現できません)。"
    )


# 字幕トラックのスタイル設定手順の案内を 1 回だけ出す (resolve21 §5.5)
def _inform_track_style_once():
    if _warned_once["track_style"]:
        return
    _warned_once["track_style"] = True
    _logger.info(
        "字幕トラックのスタイルは Resolve の字幕トラックヘッダー選択 → インスペクタ → "
        "Track Style で一括設定できます (Resolve 19 以降は Text+ スタイルも指定可)。"
    )


# ------------------------------------------------------------------
# クリップ用の spec 生成 (R1 / §5.3)
# ------------------------------------------------------------------

# 字幕一覧画面 (クリップ用) の内容から spec を作る
# source_path   : 元入力動画 (main_window が保持し SubtitleReviewBridge 経由で受け取る)
# keep_segments : 無音カットで残した区間 (元ソース相対)。None/空なら全長 1 クリップ (§7)
# items         : 字幕一覧の編集結果 (時間は無音カット後動画=タイムライン基準 / §5.2)
def build_clip_spec(source_path, keep_segments, items, settings, profile=None):
    _validate_source(source_path)
    cfg = resolve_config(settings)
    profile = profile or output_profile.resolve_output_profile(source_path, settings)
    eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
        settings.get("subtitle", {}), settings.get("vertical", {}), profile)
    font_profile = subtitle_generator.build_font_profile(eff_cfg)
    canvas = (int(profile["width"]), int(profile["height"]))

    stem = os.path.splitext(os.path.basename(source_path))[0]
    spec = _base_spec(source_path, settings, profile, cfg, stem)

    segments = list(keep_segments or [])
    if not segments:
        # 無音カット未実行/編集点なし → 全長 1 クリップとして出力する (§5.8 OFF 時もここを通る)
        total = spec["source"]["duration"] or _timeline_end(items)
        segments = [(0.0, total)]
        _logger.info("編集点が無いため全長 1 クリップとして出力します")

    spec["clips"] = _clips_from_segments(segments, "cut")

    # 字幕は mode に応じて caption (字幕トラック) / title (Text+) へ変換する (resolve21 §5.4)
    mode = _subtitle_mode(cfg)
    if mode in ("caption", "both"):
        spec["captions"] = _captions_from_items(items, 0.0, font_profile, "字幕")
    if mode in ("title", "both"):
        spec["titles"] = _titles_from_items(
            items, 0.0, font_profile, canvas, cfg["title"], "字幕")
    # SRT サイドカー用 entries は mode に関わらず収集する (書き出し可否は export_spec / §5.6)
    spec["srt_entries"] = _srt_entries_from_items(items, 0.0, font_profile)
    if spec["titles"]:
        _warn_position_once()
    return spec


# items の最終終了時刻を返す (総尺不明時のフォールバック)
def _timeline_end(items):
    return max((float(i.get("end", 0.0)) for i in (items or [])), default=0.0)


# ------------------------------------------------------------------
# Timeline 用の spec 生成 (ver3 / resolve.md §8.5)
# ------------------------------------------------------------------

# Timeline (ver3) から spec を作る
# 編集点と字幕という材料はクリップ用と同じなので、変換は既存関数をそのまま再利用する。
# FCPXML は単一ソース前提のため、元動画以外のクリップ (OP/ED・追加メディア) は
# 出力に含めず、含めなかった件数をログで明示する (黙って落とさない)。
def build_timeline_spec(timeline, settings, profile=None):
    source_path = (timeline.source or {}).get("input_path", "")
    _validate_source(source_path)
    cfg = resolve_config(settings)
    profile = profile or {
        "is_portrait": timeline.orientation == "portrait",
        "orientation": timeline.orientation,
        "width": timeline.width, "height": timeline.height,
    }
    eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
        settings.get("subtitle", {}), settings.get("vertical", {}), profile)
    font_profile = subtitle_generator.build_font_profile(eff_cfg)
    canvas = (int(timeline.width), int(timeline.height))

    stem = os.path.splitext(os.path.basename(source_path))[0]
    spec = _base_spec(source_path, settings, profile, cfg, stem)

    body_media_id = (timeline.source or {}).get("media_id")
    all_clips = timeline.base_clips()
    body_clips = [c for c in all_clips if c.media_id == body_media_id]
    skipped = len(all_clips) - len(body_clips)
    if skipped:
        _logger.warning(
            "元動画以外のクリップ %d 件 (オープニング/エンディング/追加メディア) は "
            "FCPXML に含めません。Resolve 側で追加してください。", skipped)
    if not body_clips:
        raise ExportError("元動画のクリップが 1 つも残っていないため出力できません。")

    segments = [(c.source_in, c.source_out) for c in body_clips]
    spec["clips"] = _clips_from_segments(segments, "cut")

    # 字幕は「出力する範囲だけで詰め直したタイムライン」の時刻へ写像する
    items = _timeline_subtitle_items(timeline, segments, body_media_id)
    mode = _subtitle_mode(cfg)
    if mode in ("caption", "both"):
        spec["captions"] = _captions_from_items(items, 0.0, font_profile, "字幕")
    if mode in ("title", "both"):
        spec["titles"] = _titles_from_items(
            items, 0.0, font_profile, canvas, cfg["title"], "字幕")
    spec["srt_entries"] = _srt_entries_from_items(items, 0.0, font_profile)
    if spec["titles"]:
        _warn_position_once()
    return spec


# 字幕クリップを「出力対象クリップだけを連結したタイムライン」の時刻へ直す
# 元動画以外の上に載っている字幕 (OP 中のテロップなど) は出力しない。
def _timeline_subtitle_items(timeline, segments, body_media_id):
    full_map = TimeMap.from_clips(timeline.base_clips(), body_media_id=body_media_id)
    export_map = TimeMap.from_segments(segments, media_id=body_media_id)

    items = []
    dropped = 0
    for clip in timeline.subtitle_clips():
        resolved = full_map.to_source(clip.timeline_start)
        if resolved is None or resolved[0] != body_media_id:
            dropped += 1
            continue
        start = export_map.to_timeline(resolved[1], media_id=body_media_id)
        if start is None:
            dropped += 1
            continue
        item = clip.to_item()
        item["start"] = start
        item["end"] = start + clip.duration
        items.append(item)
    if dropped:
        _logger.warning("元動画上に無い字幕 %d 件は FCPXML に含めません", dropped)
    return items


# Timeline 用 (ver3): 編集画面から呼ぶ一括エクスポート
# 戻り値: 出力パスの list ([fcpxml] または [fcpxml, srt]) / None (上書きしない選択)
def export_timeline(timeline, settings, overwrite_confirm=None):
    spec = build_timeline_spec(timeline, settings)
    source_path = (timeline.source or {}).get("input_path", "")
    dest = default_output_path(settings, source_path, resolve_config(settings)["clip_prefix"])
    return export_spec(spec, dest, settings=settings, overwrite_confirm=overwrite_confirm)


# ------------------------------------------------------------------
# アーカイブ用の spec 生成 (R2 / §5.4)
# ------------------------------------------------------------------

# 結果画面 (アーカイブ用) の内容から spec を作る
# entries: [{"index","start","end","keep_segments","items","theme","eff_cfg"}]
#   start/end       = 元 VOD 相対のクリップ区間
#   keep_segments   = クリップ内の残す区間 (クリップ相対 / None なら区間全長)
#   items           = 編集済み字幕 (prepared=無音カット後クリップ基準)
def build_archive_spec(source_path, entries, settings, profile=None):
    _validate_source(source_path)
    cfg = resolve_config(settings)
    profile = profile or output_profile.resolve_output_profile(source_path, settings)
    canvas = (int(profile["width"]), int(profile["height"]))
    subtitle_cfg = settings.get("subtitle", {})
    card = archive_config.intro_card_config(settings)
    include_card = cfg["include_intro_card"] and card["enabled"]

    stem = os.path.splitext(os.path.basename(source_path))[0]
    spec = _base_spec(source_path, settings, profile, cfg, f"{archive_config.clip_prefix(settings)}_{stem}")
    mode = _subtitle_mode(cfg)

    clips = []
    titles = []
    captions = []
    srt_entries = []
    cursor = 0.0  # タイムライン上の現在位置 (秒)
    for entry in entries or []:
        index = entry.get("index")
        clip_start = float(entry.get("start", 0.0))
        theme = str(entry.get("theme", "") or "")
        # クリップ個別の実効字幕設定 (縦動画の上書き反映済み) を優先する
        eff_cfg = entry.get("eff_cfg") or subtitle_generator.build_effective_subtitle_cfg(
            subtitle_cfg, settings.get("vertical", {}), profile)
        font_profile = subtitle_generator.build_font_profile(eff_cfg)

        # イントロ (開始前 buffer_sec を元 VOD から戻した区間) + 中央テーマ文字
        if include_card and theme:
            intro_start = max(0.0, clip_start - card["buffer_sec"])
            intro_len = clip_start - intro_start
            if intro_len >= _MIN_INTRO_SEC:
                clips.append({
                    "start": intro_start, "duration": intro_len, "name": f"clip{index}_intro",
                })
                titles.append(_theme_title(
                    theme, card, subtitle_cfg, canvas, cfg["title"],
                    cursor, intro_len, "intro", f"テーマ{index}",
                ))
                cursor += intro_len
            _warn_intro_once()

        # 本編: クリップ内の残す区間を元 VOD 相対へ直して連結する (§5.2 をクリップ単位に適用)
        segments = list(entry.get("keep_segments") or [])
        if not segments:
            # 無音カット OFF / 編集点なし → 生のクリップ区間そのまま (§5.8)
            segments = [(0.0, max(float(entry.get("end", 0.0)) - clip_start, 0.0))]
        body_clips = _clips_from_segments(segments, f"clip{index}_cut", source_offset=clip_start)
        if not body_clips:
            continue
        body_duration = sum(c["duration"] for c in body_clips)
        clips.extend(body_clips)

        # 字幕 (items 時間は無音カット後クリップ基準 = 本編タイムライン相対)。
        # mode に応じて caption (字幕トラック) / title (Text+) へ変換する (resolve21 §5.4)
        if mode in ("caption", "both"):
            captions.extend(_captions_from_items(
                entry.get("items"), cursor, font_profile, f"字幕{index}-",
            ))
        if mode in ("title", "both"):
            titles.extend(_titles_from_items(
                entry.get("items"), cursor, font_profile, canvas, cfg["title"],
                f"字幕{index}-",
            ))
        srt_entries.extend(_srt_entries_from_items(entry.get("items"), cursor, font_profile))
        # 左上タグ (本編全長)
        if include_card and theme:
            titles.append(_theme_title(
                theme, card, subtitle_cfg, canvas, cfg["title"],
                cursor, body_duration, "tag", f"タグ{index}",
            ))
        cursor += body_duration

    spec["clips"] = clips
    spec["titles"] = titles
    spec["captions"] = captions
    spec["srt_entries"] = srt_entries
    if titles:
        _warn_position_once()
    return spec


# ------------------------------------------------------------------
# 書き出し (§5.7 / §7 / §8)
# ------------------------------------------------------------------

# spec を FCPXML として dest_path へ書き出し、SRT サイドカーを併記する (resolve21 §5.6/§5.7)
# overwrite_confirm(path) -> bool : 既存ファイルがある場合の確認 (False で中止=None を返す)
# 生成失敗時は部分ファイルを残さない (一時ファイルへ書いて成功時のみ rename)。
# 戻り値: 書き出したパスの list ([fcpxml] または [fcpxml, srt]) / None (上書きしない選択)
def export_spec(spec, dest_path, settings=None, overwrite_confirm=None):
    cfg = resolve_config(settings or {})
    if cfg["format"] != "fcpxml":
        # EDL+SRT / OTIO は未実装 (§11-5)。黙って別形式を装わず FCPXML で出力する。
        _logger.warning(
            "未対応の出力形式 export.resolve.format=%s のため FCPXML で出力します", cfg["format"])

    if os.path.exists(dest_path):
        if overwrite_confirm is not None and not overwrite_confirm(dest_path):
            _logger.info("Resolve 出力を中止 (上書きしない): %s", dest_path)
            return None

    _logger.info(
        "Resolve 出力開始: %s (クリップ %d 件 / 字幕 caption %d 件 / title・テーマ %d 件 "
        "/ %dx%d %dfps)",
        dest_path, len(spec.get("clips", [])), len(spec.get("captions", [])),
        len(spec.get("titles", [])), spec.get("width"), spec.get("height"), spec.get("fps"),
    )
    try:
        content = fcpxml_builder.build_fcpxml(spec)
    except Exception as e:  # noqa: BLE001 (想定外データは ExportError へ集約する)
        _logger.exception("FCPXML の生成に失敗")
        raise ExportError(f"FCPXML の生成に失敗しました: {e}") from e

    _atomic_write(dest_path, content)
    written = [dest_path]

    # SRT サイドカー (失敗しても FCPXML は出力済みのため非致命 / §7)
    srt_path = _write_srt_sidecar(spec, dest_path, cfg)
    if srt_path:
        written.append(srt_path)
    if spec.get("captions") or srt_path:
        _inform_track_style_once()

    total = sum(c["duration"] for c in spec.get("clips", []))
    _logger.info("Resolve 出力完了: %s (タイムライン尺 %.2fs)", " / ".join(written), total)
    return written


# 一時ファイルへ書いて成功時のみ rename する (部分ファイルを残さない / §7)
def _atomic_write(dest_path, content):
    temp_path = dest_path + _TEMP_SUFFIX
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, dest_path)
    except OSError as e:
        # 書き込み途中の一時ファイルを残さない
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise ExportError(f"Resolve 出力ファイルの書き出しに失敗しました: {e}") from e


# SRT サイドカーを FCPXML と同名 (拡張子違い) で書き出す (resolve21 §5.6)
# 設定 OFF・字幕 0 件は出力しない。失敗は WARNING に留め、主産物 (FCPXML) を優先する。
def _write_srt_sidecar(spec, dest_path, cfg):
    if not cfg["subtitle"]["srt_sidecar"]:
        return None
    content = srt_writer.build_srt(spec.get("srt_entries"))
    if not content:
        return None
    srt_path = os.path.splitext(dest_path)[0] + _SRT_EXTENSION
    try:
        _atomic_write(srt_path, content)
    except ExportError as e:
        _logger.warning("SRT サイドカーの書き出しに失敗 (FCPXML は出力済み): %s", e)
        return None
    _logger.info("SRT サイドカー出力: %s (%d 件)", srt_path, len(spec.get("srt_entries") or []))
    return srt_path


# クリップ用 (R1): 字幕一覧画面から呼ぶ一括エクスポート
# export_context: {"source_path","keep_segments","settings","profile"} (SubtitleReviewBridge 由来)
# 戻り値: 出力パスの list ([fcpxml] または [fcpxml, srt]) / None (上書きしない選択)
def export_clip_review(export_context, items, overwrite_confirm=None):
    context = export_context or {}
    settings = context.get("settings") or {}
    source_path = context.get("source_path", "")
    spec = build_clip_spec(
        source_path, context.get("keep_segments"), items, settings,
        profile=context.get("profile"),
    )
    dest = default_output_path(settings, source_path, resolve_config(settings)["clip_prefix"])
    return export_spec(spec, dest, settings=settings, overwrite_confirm=overwrite_confirm)


# アーカイブ用 (R2): 結果画面から呼ぶ一括エクスポート
# 戻り値: 出力パスの list ([fcpxml] または [fcpxml, srt]) / None (上書きしない選択)
def export_archive_result(source_path, entries, settings, overwrite_confirm=None):
    spec = build_archive_spec(source_path, entries, settings)
    if not spec["clips"]:
        raise ExportError("出力対象のクリップがありません (使用クリップを1つ以上選択してください)。")
    dest = default_output_path(settings, source_path, archive_config.clip_prefix(settings))
    return export_spec(spec, dest, settings=settings, overwrite_confirm=overwrite_confirm)
