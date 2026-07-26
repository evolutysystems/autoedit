# TOP5 クリップの処理と結合 (request18 / resolve18 + テーマ演出 resolve19 + R2 一括フロー flow17)
# R2 (flow17): クリップごとに編集画面を出す方式をやめ、
#   ① prepare: 全クリップを 区間切り出し → 無音カット → 文字起こし し、タイムラインを用意。
#   ② 一括レビュー: 1つの結果画面 (ArchiveResultWindow) で全クリップの字幕・テーマを編集。
#   ③ burn: 各クリップを 字幕焼き込み → テーマ有りは intro/tag (resolve19) → 結合。
# 共有パイプライン (run_pipeline/subtitle_generator/silence_cutter) のロジックは変更せず、
# 標準関数を直接呼び出して prepare/burn を分割する。
# 全 VOD を文字起こしせず採用区間のみ処理する点は従来どおり。
import copy
import os
import shutil
import tempfile

from ..modules import (
    concat_processor,
    ffmpeg_runner,
    output_profile,
    silence_cutter,
    subtitle_generator,
)
from ..modules.subtitle_generator import _format_ass_time, _hex_to_ass_color
from ..pipeline.pipeline_context import PipelineContext
from ..settings.settings_window import resolve_fonts_dir
from ..utils.logger import get_logger
from . import config

_logger = get_logger(__name__)


# 出力ディレクトリを解決する (general.output_directory。未設定/不在なら入力と同じ場所)
def _resolve_output_dir(settings, input_path):
    out_dir = settings.get("general", {}).get("output_directory", "")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        return out_dir
    return os.path.dirname(os.path.abspath(input_path))


# 秒をファイル名に使える表記へ整形する (H-MM-SS。コロンは Windows で不可のため使わない)
def _fmt_ts(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}-{m:02d}-{s:02d}"


# 区間 [start,end] を切り出す (ストリームコピーで高速に。焼き込み側で再エンコードされる)
def _cut_region(input_path, start, end, dest, ffmpeg_cfg):
    duration = max(end - start, 0.0)
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y",
        "-ss", f"{start:.3f}",
        "-i", input_path,
        "-t", f"{duration:.3f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        dest,
    ]
    ffmpeg_runner.execute(cmd, total_duration=duration)
    return dest


# クリップ処理用の設定コピーを作る (OP/ED は付けない・出力は一時領域・無音カット/レビューは設定に従う)
def _build_clip_settings(settings, workdir, clip_pipe):
    clip_settings = copy.deepcopy(settings)
    general = clip_settings.setdefault("general", {})
    # OP/ED は各クリップには付けない (結合段階で1回だけ付ける / resolve18 §3-2)
    general["opening_enabled"] = False
    general["ending_enabled"] = False
    # 各クリップの出力は一時領域へ (最終結合まで出力先を汚さない)
    general["output_directory"] = workdir
    # 無音カット / テロップ編集画面の ON/OFF (既定 True = 要望どおり)
    clip_settings.setdefault("silence_cut", {})["enabled"] = bool(clip_pipe.get("silence_cut", True))
    clip_settings.setdefault("subtitle", {})["review_enabled"] = bool(clip_pipe.get("subtitle_review", True))
    return clip_settings


# ------------------------------------------------------------------
# テーマ演出 (resolve19)
# ------------------------------------------------------------------

# テーマ文字列を ASS の Dialogue Text 用に整形する (改行→\N。復帰は除去)
def _escape_ass_text(text):
    return str(text).replace("\r", "").replace("\n", "\\N")


# ass フィルタのオプション文字列を組み立てる (burn_subtitle と同一の2段階エスケープ)。
# 追加フォント(fonts_dir)があれば fontsdir を付けて libass に解決させる。
def _ass_filter_opt(ass_path, fonts_dir):
    safe = ass_path.replace("\\", "/").replace(":", "\\:")
    opt = f"ass='{safe}'"
    if fonts_dir and os.path.isdir(fonts_dir):
        safe_dir = fonts_dir.replace("\\", "/").replace(":", "\\:")
        opt += f":fontsdir='{safe_dir}'"
    return opt


# 不透明度(0..1)を ASS のアルファ2桁HEX へ変換する (00=不透明 / FF=透明)
def _alpha_hex(opacity):
    clamped = max(0.0, min(1.0, opacity))
    a = int(round((1.0 - clamped) * 255))
    return f"{a:02X}"


# 出力キャンバス寸法(W,H)と fps を ffmpeg 設定から取得する
def _canvas(ffmpeg_cfg):
    w = int(ffmpeg_cfg.get("output_width", 1920))
    h = int(ffmpeg_cfg.get("output_height", 1080))
    fps = ffmpeg_runner.get_output_fps(ffmpeg_cfg)
    return w, h, fps


# 出力キャンバスへ正規化する scale+pad+setsar チェーンを返す (アスペクト維持・中央寄せ)
def _normalize_chain(w, h):
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )


# イントロカードの中央テーマ ASS を書き出す (an5 中央・大サイズ / resolve19 §4.4)
def _write_intro_ass(path, theme, card, subtitle_cfg, w, h, end_sec):
    font = card["title_font_family"] or subtitle_cfg.get("font_family", "Yu Gothic UI")
    size = card["title_font_size"]
    primary = _hex_to_ass_color(card["title_color"])
    end = _format_ass_time(end_sec)
    text = _escape_ass_text(theme)
    content = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Title,{font},{size},{primary},&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,1,3,0,5,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,{end},Title,,0,0,0,,{text}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# 左上テーマタグの ASS を書き出す (an7 左上・小サイズ・半透明背景 / resolve19 §4.5)
def _write_tag_ass(path, theme, card, subtitle_cfg, w, h, end_sec):
    font = card["tag_font_family"] or subtitle_cfg.get("font_family", "Yu Gothic UI")
    size = card["tag_font_size"]
    primary = _hex_to_ass_color(card["tag_color"])
    # 背景の不透明度>0 のときは不透明ボックス(BorderStyle=3)で背景を敷き可読性を確保する。
    # 0 以下なら縁取り(BorderStyle=1)のみとする。
    if card["tag_bg_opacity"] > 0:
        back = f"&H{_alpha_hex(card['tag_bg_opacity'])}000000"
        border_style, outline = 3, 0
    else:
        back = "&H00000000"
        border_style, outline = 1, 2
    end = _format_ass_time(end_sec)
    text = _escape_ass_text(theme)
    ml, mv = card["tag_margin_l"], card["tag_margin_v"]
    content = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Tag,{font},{size},{primary},&H00000000,{back},"
        f"-1,0,0,0,100,100,0,0,{border_style},{outline},0,7,{ml},0,{mv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,{end},Tag,,0,0,0,,{text}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# イントロカード(既定1.5秒)を生成する (resolve19 §4.4)。
# 元 VOD の [max(0,start-buffer), start] を切り出し、出力キャンバスへ正規化した上で
# ブラー+黒帯(全幅・上下余白)+中央テーマを焼く。バッファを確保できない(素材端)なら None。
def _build_intro_card(source, clip_start, theme, card, subtitle_cfg,
                      ffmpeg_cfg, fonts_dir, workdir, index):
    start_buf = max(0.0, clip_start - card["buffer_sec"])
    length = clip_start - start_buf
    if length < 0.1:
        _logger.info("clip%d: 開始前バッファを確保できないためイントロを省略", index)
        return None
    w, h, fps = _canvas(ffmpeg_cfg)
    ass_path = os.path.join(workdir, f"intro{index}.ass")
    _write_intro_ass(ass_path, theme, card, subtitle_cfg, w, h, length)
    filter_spec = (
        f"{_normalize_chain(w, h)},"
        f"gblur=sigma={card['blur_sigma']},"
        f"drawbox=x=0:y={card['margin_top_px']}:w=iw:"
        f"h=ih-{card['margin_top_px']}-{card['margin_bottom_px']}:"
        f"color={card['box_color']}@{card['box_opacity']}:t=fill,"
        f"fps={fps},"
        f"{_ass_filter_opt(ass_path, fonts_dir)}"
    )
    out = os.path.join(workdir, f"intro{index}.mp4")
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-ss", f"{start_buf:.3f}", "-t", f"{length:.3f}",
        "-i", source,
        "-vf", filter_spec,
    ]
    if card["mute_intro"]:
        # 音声ストリームは残しつつ無音化する (結合時の音声ストリーム整合を保つ)
        cmd += ["-af", "volume=0"]
    cmd += [
        *ffmpeg_runner.build_encode_options(ffmpeg_cfg),
        "-fps_mode", "cfr", "-r", str(fps),
        out,
    ]
    ffmpeg_runner.execute(cmd, total_duration=length)
    _logger.info("clip%d: イントロカード生成 (%.2fs, テーマ=%s)", index, length, theme)
    return out


# 本編クリップの全長に左上テーマタグを焼く (resolve19 §4.5)。
# 出力キャンバスへ正規化しつつタグを重ねる (タグ尺=本編の実尺)。
def _build_tag_overlay(body_in, theme, card, subtitle_cfg,
                       ffmpeg_cfg, fonts_dir, workdir, index):
    w, h, fps = _canvas(ffmpeg_cfg)
    duration = ffmpeg_runner.probe_duration(body_in, ffmpeg_cfg)
    ass_path = os.path.join(workdir, f"tag{index}.ass")
    _write_tag_ass(ass_path, theme, card, subtitle_cfg, w, h, duration)
    filter_spec = (
        f"fps={fps},{_normalize_chain(w, h)},"
        f"{_ass_filter_opt(ass_path, fonts_dir)}"
    )
    out = os.path.join(workdir, f"clip{index}_tagged.mp4")
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-i", body_in,
        "-vf", filter_spec,
        *ffmpeg_runner.build_encode_options(ffmpeg_cfg),
        "-fps_mode", "cfr", "-r", str(fps),
        out,
    ]
    ffmpeg_runner.execute(cmd, total_duration=duration)
    _logger.info("clip%d: 左上テーマタグを焼き込み", index)
    return out


# 焼き込み済みクリップにテーマ演出を適用する (resolve19)。
# テーマ有り: [イントロ?, タグ付き本編] を返す。テーマ無し/機能無効: [out] を返す。
# 戻り値: (parts, body) — parts=結合用の時系列パーツ列 / body=個別出力用の本編パス。
# 演出の途中失敗はクリップ全損を避けるため握りつぶし、可能な範囲で継続する。
def _decorate_clip(source, out, clip, theme, card, subtitle_cfg,
                   ffmpeg_cfg, fonts_dir, workdir):
    index = clip["index"]
    if not theme or not card["enabled"]:
        return [out], out
    # 左上タグ (本編全長)
    try:
        body = _build_tag_overlay(out, theme, card, subtitle_cfg,
                                  ffmpeg_cfg, fonts_dir, workdir, index)
    except Exception:  # noqa: BLE001 (演出失敗で本編を失わない)
        _logger.exception("clip%d: タグ焼き込みに失敗 → 素の本編で継続", index)
        return [out], out
    # イントロカード (開始前 buffer_sec)
    try:
        intro = _build_intro_card(source, clip["start"], theme, card, subtitle_cfg,
                                  ffmpeg_cfg, fonts_dir, workdir, index)
    except Exception:  # noqa: BLE001
        _logger.exception("clip%d: イントロ生成に失敗 → タグ付き本編のみで継続", index)
        intro = None
    parts = [intro, body] if intro else [body]
    return parts, body


# ------------------------------------------------------------------
# prepare フェーズ (R2 flow17): 全クリップを切り出し→無音カット→文字起こし
# ------------------------------------------------------------------

# 無音カットを最小 PipelineContext で実行する (extract_mode/fade/batch 等の既存挙動を流用)。
# silence_cut.enabled=false のときは元パスをそのまま返す。
def _silence_cut(raw, clip_settings, clip_dir):
    ctx = PipelineContext(input_path=raw, settings=clip_settings, working_dir=clip_dir)
    silence_cutter.run(ctx)
    return ctx.current_video_path()


# 文字起こし→表示タイミング整形で編集用タイムライン(items)を得る (subtitle_generator 標準関数)。
# subtitle 無効/エンジン無しは空。失敗時も空で継続(クリップ全損を避ける)。
def _transcribe(prepared_path, settings, eff_cfg):
    if not eff_cfg.get("enabled", True):
        return []
    text_source = subtitle_generator.resolve_text_source(settings, eff_cfg)
    if text_source is None:
        return []
    language = eff_cfg.get("language", "ja")
    try:
        timeline = text_source.extract(prepared_path, language)
    except Exception:  # noqa: BLE001 (文字起こし失敗でクリップを失わない)
        _logger.exception("文字起こしに失敗 → 空字幕で継続")
        return []
    max_hold = float(eff_cfg.get("display_max_hold_sec", 2.0))
    min_dur = float(eff_cfg.get("display_min_duration_sec", 0.5))
    timeline = subtitle_generator.adjust_display_timing(timeline, max_hold, min_dur)
    # 編集画面用 items (全件 use=True, 役割は既定=配信者)
    return [
        {"start": e["start"], "end": e["end"], "text": e["text"],
         "use": True, "role": "streamer"}
        for e in timeline
    ]


# 全クリップを prepare する (切り出し→無音カット→文字起こし)。ダイアログは出さない。
# 戻り値: [{"index","start","end","score","prepared_path","profile","eff_cfg","items"}]
def _prepare_clips(input_path, settings, clip_settings, used, ffmpeg_cfg, workdir, progress_cb):
    prepared = []
    total = len(used)
    subtitle_cfg = clip_settings.get("subtitle", {})
    vertical_cfg = settings.get("vertical", {})
    for pos, clip in enumerate(used, 1):
        if progress_cb:
            progress_cb((pos - 1) / total * 0.55, f"クリップ {pos}/{total} を準備中…（文字起こし）")
        # クリップ専用の作業サブフォルダ (無音カット出力名の衝突を防ぐ)
        clip_dir = os.path.join(workdir, f"clip{clip['index']}")
        os.makedirs(clip_dir, exist_ok=True)
        raw = os.path.join(clip_dir, "raw.mp4")
        _cut_region(input_path, clip["start"], clip["end"], raw, ffmpeg_cfg)
        prepared_path = _silence_cut(raw, clip_settings, clip_dir)
        profile = output_profile.resolve_output_profile(prepared_path, settings)
        eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
            subtitle_cfg, vertical_cfg, profile)
        items = _transcribe(prepared_path, settings, eff_cfg)
        prepared.append({
            "index": clip["index"], "start": clip["start"], "end": clip["end"],
            "score": clip.get("score", 0.0),
            "prepared_path": prepared_path, "profile": profile,
            "eff_cfg": eff_cfg, "items": items,
        })
        _logger.info("clip%d prepare 完了 (字幕 %d 件)", clip["index"], len(items))
    return prepared


# ------------------------------------------------------------------
# burn フェーズ (R2 flow17): 編集済み字幕を焼き込み→テーマ演出→パーツ化
# ------------------------------------------------------------------

# 編集済み items を焼き込み対象タイムラインへ整形する (_review_timeline 相当)。
# 使用チェックのみ残し、wrap_lines を再適用。役割/個別フォントは保持。
def _finalize_timeline(items, eff_cfg):
    min_len = int(eff_cfg.get("min_line_length", 15))
    max_len = int(eff_cfg.get("max_line_length", 20))
    engine = str(eff_cfg.get("wrap_engine", "budoux")).strip().lower()
    used = []
    for e in items:
        if not e.get("use", True):
            continue
        used.append({
            "start": e["start"], "end": e["end"],
            "text": subtitle_generator.wrap_lines(e.get("text", ""), min_len, max_len, engine),
            "role": e.get("role", "streamer"),
            "font": e.get("font", ""),
            "font_size": e.get("font_size"),
        })
    return used


# 編集済みタイムラインを prepared クリップへ焼き込む (build_subtitle_file + burn_subtitle)。
# 使用字幕が0件なら焼き込まず prepared_path をそのまま返す。
def _burn_one(prepared, items, settings, ffmpeg_cfg, fonts_dir):
    eff_cfg = prepared["eff_cfg"]
    profile = prepared["profile"]
    used_timeline = _finalize_timeline(items, eff_cfg)
    src = prepared["prepared_path"]
    if not used_timeline:
        _logger.info("clip%d 使用字幕0件 → 焼き込みスキップ", prepared["index"])
        return src
    font_profile = subtitle_generator.build_font_profile(eff_cfg)
    canvas_w = int(profile["width"])
    canvas_h = int(profile["height"])
    target_size = (canvas_w, canvas_h) if profile.get("is_portrait") else None
    clip_dir = os.path.dirname(src)
    ass_path = os.path.join(clip_dir, "subtitle.ass")
    out_path = os.path.join(clip_dir, "burned.mp4")
    subtitle_generator.build_subtitle_file(
        used_timeline, font_profile, ass_path,
        video_width=canvas_w, video_height=canvas_h)
    total_dur = ffmpeg_runner.probe_duration(src, ffmpeg_cfg)
    subtitle_generator.burn_subtitle(
        src, ass_path, out_path, ffmpeg_cfg,
        total_duration=total_dur, target_size=target_size, fonts_dir=fonts_dir)
    return out_path


# 一括レビュー結果 edited を各クリップへ焼き込み、テーマ演出を適用してパーツ化する。
# edited: [{"index","items","theme","use"}] / prepared_by_index: index→prepared。
# 戻り値: burned = [{"clip","body","parts"}]
def _burn_clips(input_path, edited, prepared_by_index, settings, ffmpeg_cfg,
                workdir, progress_cb, card, fonts_dir):
    burned = []
    subtitle_cfg = settings.get("subtitle", {})
    used_edited = [e for e in edited if e.get("use", True)]
    total = max(1, len(used_edited))
    for pos, ed in enumerate(used_edited, 1):
        prepared = prepared_by_index.get(ed.get("index"))
        if prepared is None:
            continue
        if progress_cb:
            progress_cb(0.6 + (pos - 1) / total * 0.3,
                        f"クリップ {pos}/{len(used_edited)} を焼き込み中…")
        out = _burn_one(prepared, ed.get("items", []), settings, ffmpeg_cfg, fonts_dir)
        theme = (ed.get("theme") or "").strip()
        clip_meta = {"index": prepared["index"], "start": prepared["start"],
                     "end": prepared["end"]}
        parts, body = _decorate_clip(
            input_path, out, clip_meta, theme, card, subtitle_cfg,
            ffmpeg_cfg, fonts_dir, workdir)
        burned.append({"clip": clip_meta, "body": body, "parts": parts})
    return burned


# keep_individual 用: 本編クリップを出力先へ複製する (イントロは含めない)
def _copy_individual(entry, out_dir, prefix, stem):
    clip = entry["clip"]
    name = f"{prefix}_{stem}_clip{clip['index']}_{_fmt_ts(clip['start'])}-{_fmt_ts(clip['end'])}.mp4"
    dest = os.path.join(out_dir, name)
    shutil.copy(entry["body"], dest)
    return dest


# 結合しない設定での個別出力: テーマ有りは [イントロ, 本編] を1本へ結合、無しは本編を複製
def _write_individual(entry, out_dir, prefix, stem, ffmpeg_cfg):
    clip = entry["clip"]
    name = f"{prefix}_{stem}_clip{clip['index']}_{_fmt_ts(clip['start'])}-{_fmt_ts(clip['end'])}.mp4"
    dest = os.path.join(out_dir, name)
    _combine(entry["parts"], dest, ffmpeg_cfg)  # 1本ならコピー / 2本(intro+本編)なら結合
    return dest


# 確定した TOP5 クリップを prepare→一括レビュー→burn→結合して 1 本の動画を出力する (R2 flow17)
# clips: [{index,start,end,score,use,...}] (use=False は除外)。
# result_callback(prepared, curve): 一括結果画面ブリッジ。編集済み [{index,items,theme,use}]
#   を返す。None ならキャンセル (出力なし)。未注入(CLI/テスト)は全件そのまま自動焼き込み。
# curve: 採点グラフ用の窓スコア列 (結果画面へ渡す)。
# 戻り値: 出力ファイルパスの一覧 (結合時は [..., 結合1本]、非結合時は個別クリップ群)。
def write_clips(input_path, settings, clips, progress_cb=None, result_callback=None, curve=None):
    ffmpeg_cfg = settings.get("ffmpeg", {})
    ffmpeg_runner.ensure_available(ffmpeg_cfg)

    used = [c for c in clips if c.get("use", True)]
    if not used:
        _logger.info("使用クリップが0件のため切り抜きをスキップ")
        return []

    archive_cfg = settings.get("archive", {})
    clip_pipe = archive_cfg.get("clip_pipeline", {})
    combine_cfg = archive_cfg.get("combine", {})
    # テーマ演出設定・追加フォントディレクトリ (resolve19)
    card = config.intro_card_config(settings)
    fonts_dir = resolve_fonts_dir(settings)

    out_dir = _resolve_output_dir(settings, input_path)
    prefix = config.clip_prefix(settings)
    stem = os.path.splitext(os.path.basename(input_path))[0]

    # Windows では焼き込み直後のファイルが一時的にロックされ得るため、
    # 一時ディレクトリ削除の失敗を無視する (削除失敗で処理成功が誤ってエラー扱いになるのを防ぐ)。
    with tempfile.TemporaryDirectory(prefix="archive_clips_", ignore_cleanup_errors=True) as workdir:
        clip_settings = _build_clip_settings(settings, workdir, clip_pipe)

        # ① prepare: 全クリップを 切り出し→無音カット→文字起こし (ダイアログ無し)
        prepared = _prepare_clips(
            input_path, settings, clip_settings, used, ffmpeg_cfg, workdir, progress_cb)
        if not prepared:
            _logger.info("準備できたクリップが0件")
            return []

        # ② 一括レビュー: 1つの結果画面で全クリップの字幕・テーマを編集
        if result_callback is not None:
            edited = result_callback(prepared, curve or [])
            if edited is None:
                _logger.info("結果画面でキャンセル → 出力なし")
                return []
        else:
            # CLI/テスト: 全件そのまま採用 (テーマ無し)
            edited = [
                {"index": p["index"], "items": p["items"], "theme": "", "use": True}
                for p in prepared
            ]

        # ③ burn: 各クリップを 字幕焼き込み→テーマ演出→パーツ化
        if progress_cb:
            progress_cb(0.6, "字幕焼き込み中…")
        prepared_by_index = {p["index"]: p for p in prepared}
        burned = _burn_clips(
            input_path, edited, prepared_by_index, settings, ffmpeg_cfg,
            workdir, progress_cb, card, fonts_dir)

        if not burned:
            _logger.info("焼き込み済みクリップが0件")
            return []

        # keep_individual: 個別クリップ(本編)も出力先へ残す
        outputs = []
        if combine_cfg.get("keep_individual", False):
            for entry in burned:
                outputs.append(_copy_individual(entry, out_dir, prefix, stem))

        # 各クリップの結合用パーツを時系列で平坦化 (テーマ有りは [intro, tagged])
        clip_parts = [path for entry in burned for path in entry["parts"]]

        # 結合しない設定なら個別出力のみ (keep_individual 未指定でも個別を出す)
        if not combine_cfg.get("enabled", True):
            if not outputs:
                for entry in burned:
                    outputs.append(_write_individual(entry, out_dir, prefix, stem, ffmpeg_cfg))
            _logger.info("個別クリップ %d 件を出力 (結合なし)", len(outputs))
            if progress_cb:
                progress_cb(1.0, "完了")
            return outputs

        # 結合: [Opening?] + (intro?+本編)群 + [Ending?] を 1 本へ (OP/ED を1回だけ)
        if progress_cb:
            progress_cb(0.92, "結合中…")
        parts = _build_combine_parts(settings, combine_cfg, clip_parts)

        suffix = combine_cfg.get("combined_suffix", "combined") or "combined"
        final = os.path.join(out_dir, f"{prefix}_{stem}_{suffix}.mp4")
        _combine(parts, final, ffmpeg_cfg)

        _logger.info("結合出力: %s (使用%d)", final, len(burned))
        if progress_cb:
            progress_cb(1.0, "完了")
        return outputs + [final]


# 結合パーツ [Opening?] + クリップ群 + [Ending?] を組み立てる
# clip_parts は時系列に平坦化済み (テーマ有りクリップは [intro, tagged] を含む / resolve19)。
# OP/ED は combine.opening_ending かつ general フラグ+素材有無で判定 (クリップ用と同一ルール)
def _build_combine_parts(settings, combine_cfg, clip_parts):
    parts = []
    general = settings.get("general", {})
    add_oped = bool(combine_cfg.get("opening_ending", True))
    opening = general.get("opening_video", "")
    ending = general.get("ending_video", "")
    if add_oped and general.get("opening_enabled", False) and concat_processor.is_available(opening):
        parts.append(opening)
    parts.extend(clip_parts)
    if add_oped and general.get("ending_enabled", False) and concat_processor.is_available(ending):
        parts.append(ending)
    return parts


# パーツを結合して final を作る (2本以上は concat、1本のみはコピー)
def _combine(parts, final, ffmpeg_cfg):
    if len(parts) >= 2:
        total_dur = 0.0
        for p in parts:
            try:
                total_dur += ffmpeg_runner.probe_duration(p, ffmpeg_cfg)
            except Exception:  # noqa: BLE001 (進捗総尺の取得失敗は致命でない)
                pass
        concat_processor.concat(parts, final, ffmpeg_cfg, total_duration=total_dur)
    else:
        # クリップ1本のみ・OP/ED 無し → そのまま出力先へ複製 (原本は一時領域で破棄)
        shutil.copy(parts[0], final)
    return final
