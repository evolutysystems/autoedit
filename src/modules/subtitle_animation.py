# テロップアニメーション (docs/request/ver12/resolve2.md)
#
# Word Pop / Bounce / Big Word / Fade In / Fade Out を ASS の上書きタグへ落とす。
# 「単語をどう切るか」「どこへ置くか」「いつ・どれだけ動かすか」の計算は
# **このモジュールだけが持つ**。焼き込み (subtitle_generator) とプレビュー
# (gui/timeline/preview_items) の両方がここを呼ぶため、2 か所へ書くと必ずずれる
# (compositor / sound_mix と同じ方針)。
#
# 【設計の要点】
#   ・単語ごとに動かすものは **単語 1 つ = 独立した Dialogue イベント** にする。
#     行の中で文字を拡大すると後続の文字が押し出されて行全体が揺れるため
#     (resolve2 §1)、各語へ \pos を持たせて行を組み直させない。
#   ・単語の位置はフォントの実測送り幅 (font_advance) で求める。読めないフォントでは
#     単語単位をあきらめ、**行全体の動き**へ落とす (黙って崩さない)。
#   ・ASS の \t は位置を動かせない (色・大きさ・透明度などのみ)。上下動は \move を
#     段に分けたイベントで表す。
#
# subtitle_generator から import されるため、逆向きの import はしない
# (ASS 色文字列の組み立てだけ循環 import を避けるためここへ小さく持つ / comment_decor と同じ)。
from ..utils.logger import get_logger
from . import font_advance

_logger = get_logger(__name__)

# アニメーションの種類 (timeline.model と同じ値。model を import しないのは
# modules 層が timeline 層へ依存しないため)
ANIM_WORD_POP = "word_pop"
ANIM_BOUNCE = "bounce"
ANIM_BIG_WORD = "big_word"
ANIMATIONS = (ANIM_WORD_POP, ANIM_BOUNCE, ANIM_BIG_WORD)

# 設定が渡されなかったときの既定 (settings_window.DEFAULT_SETTINGS と同じ値)
_DEFAULTS = {
    "enabled": True,
    "word_pop_grow_ms": 80,
    "word_pop_settle_ms": 120,
    "word_pop_scale": 130,
    "emphasis_words": (),
    "emphasis_color": "#FFD34D",
    "emphasis_scale": 115,
    "bounce_height_ratio": 0.35,
    "bounce_ms": 420,
    "bounce_segments": 3,
    "bounce_word_stagger_ms": 60,
    "big_word_scale": 260,
    "big_word_min_ms": 240,
    "big_word_position": "center",
    "fade_in_ms": 200,
    "fade_out_ms": 200,
    "max_words_per_clip": 24,
    "max_events_per_clip": 96,
}

# ASS の行送り。libass は Fontsize が ascent+descent に一致するよう字を縮尺するため、
# 行は Fontsize ぶんだけ進む (gui/timeline/font_metrics.line_advance_px と同じ規約)。
_LINE_ADVANCE_RATIO = 1.0

# これ未満の表示時間は作らない (ASS の分解能は 10ms)
_MIN_EVENT_SEC = 0.02

# 1 度だけ出す警告の記録
_warned = set()


def _warn_once(key, message, *args):
    if key in _warned:
        return
    _warned.add(key)
    _logger.warning(message, *args)


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------

# subtitle.animation を既定で補完して返す
def config(subtitle_cfg):
    values = ((subtitle_cfg or {}).get("animation") or {})
    cfg = {**_DEFAULTS, **values}
    return cfg


# 機能そのものが有効か (false なら呼び出し側は従来どおり 1 行書く)
def is_enabled(cfg):
    return bool((cfg or {}).get("enabled", True))


# そのエントリの動きの種類 ("" = 動かさない)
def animation_of(entry):
    value = str((entry or {}).get("animation", "") or "").strip().lower()
    return value if value in ANIMATIONS else ""


# 動きかフェードのどれかが付いているか
def has_animation(entry):
    entry = entry or {}
    return bool(animation_of(entry) or entry.get("fade_in") or entry.get("fade_out"))


# ------------------------------------------------------------------
# フェード (種類と併用できる)
# ------------------------------------------------------------------

# \fad(in,out) を返す (どちらも無ければ空文字)
#
# フェードはテロップ全体に掛かるため、単語ごとのイベントにも同じタグを付ける。
# コメントの背景 (別イベント) へも同じものを付けると箱と文字が一緒に消える。
def fade_tag(entry, cfg):
    entry = entry or {}
    cfg = config(None) if cfg is None else cfg
    fade_in = _to_int(cfg.get("fade_in_ms"), 200) if entry.get("fade_in") else 0
    fade_out = _to_int(cfg.get("fade_out_ms"), 200) if entry.get("fade_out") else 0
    if fade_in <= 0 and fade_out <= 0:
        return ""
    # 表示時間より長いフェードは入れない (出た瞬間に消えるため)
    duration_ms = max(
        int((_to_float(entry.get("end"), 0.0) - _to_float(entry.get("start"), 0.0))
            * 1000), 0)
    if duration_ms > 0 and fade_in + fade_out > duration_ms:
        scale = duration_ms / float(fade_in + fade_out)
        fade_in = int(fade_in * scale)
        fade_out = int(fade_out * scale)
    return f"\\fad({max(fade_in, 0)},{max(fade_out, 0)})"


# ------------------------------------------------------------------
# 単語の切り方 (resolve2 §2.1)
# ------------------------------------------------------------------

# 本文を単語へ切り、テロップ開始からの相対秒を付けて返す
#
# 戻り値: [{"text", "offset", "duration", "emphasis", "line"}]
#   offset   : テロップ開始からの相対秒 (その語が現れる時刻)
#   duration : その語の発話長 (秒)
#   line     : 何行目に載る語か (0 起点)
#   emphasis : 強調する語か
#
# 切り方は 3 段:
#   ① 認識由来で本文が未編集    → origin["words"] の単語時刻をそのまま使う
#   ② 本文を手直し済み・手入力  → BudouX で句へ切り、尺を文字数で按分
#   ③ BudouX が無い            → 句読点と空白で切り、同じく按分
def split_words(entry, cfg=None):
    entry = entry or {}
    cfg = config(None) if cfg is None else cfg
    lines = _lines_of(entry)
    plain = "".join(lines)
    if not plain:
        return []

    start = _to_float(entry.get("start"), 0.0)
    end = _to_float(entry.get("end"), 0.0)
    duration = max(end - start, 0.0)
    if duration <= 0:
        return []

    pieces = _words_from_asr(entry, plain, duration)
    if pieces is None:
        pieces = _words_from_text(plain, duration)
    if not pieces:
        return []

    emphasis = _emphasis_set(entry, cfg)
    return _assign_lines(pieces, lines, emphasis)


# 本文の行 (ASS の \N と実改行の両方を区切りとして扱う)
def _lines_of(entry):
    body = str((entry or {}).get("text", "") or "")
    body = body.replace("\\N", "\n").replace("\\n", "\n")
    return [line for line in body.split("\n")]


# ① 認識由来の単語時刻を使う (本文と一致しなければ None)
#
# origin["words"] は**素材時間**のため、「先頭語からの相対位置」で突き合わせる
# (commands._char_index_from_words と同じ規約)。
def _words_from_asr(entry, plain, duration):
    words = (entry or {}).get("words")
    if not words:
        return None
    try:
        joined = "".join(str(w.get("text", "")) for w in words).strip()
    except AttributeError:
        return None
    if joined.replace(" ", "") != plain.replace(" ", ""):
        return None            # 手直し済み → 按分へ落ちる
    first = _to_float(words[0].get("start"), 0.0)
    pieces = []
    for word in words:
        text = str(word.get("text", ""))
        if not text.strip():
            continue
        offset = max(_to_float(word.get("start"), 0.0) - first, 0.0)
        length = max(_to_float(word.get("end"), 0.0)
                     - _to_float(word.get("start"), 0.0), 0.0)
        if offset >= duration:
            offset = max(duration - _MIN_EVENT_SEC, 0.0)
        pieces.append({"text": text.strip(),
                       "offset": offset,
                       "duration": min(length, max(duration - offset, 0.0))})
    return pieces or None


# ②③ 本文から句へ切り、尺を文字数で按分する
def _words_from_text(plain, duration):
    chunks = _split_phrases(plain)
    if not chunks:
        return []
    total = sum(len(c) for c in chunks) or 1
    pieces = []
    cursor = 0.0
    for chunk in chunks:
        share = duration * (len(chunk) / float(total))
        pieces.append({"text": chunk, "offset": cursor, "duration": share})
        cursor += share
    return pieces


# 句へ切る (BudouX があれば文節、無ければ句読点と空白)
def _split_phrases(plain):
    parser = _budoux_parser()
    if parser is not None:
        try:
            chunks = [c for c in parser.parse(plain) if c.strip()]
            if chunks:
                return chunks
        except Exception:  # noqa: BLE001 (任意依存の失敗で編集を止めない)
            _warn_once("budoux", "BudouX で句へ切れないため記号で区切ります")
    chunks, current = [], ""
    for char in plain:
        current += char
        if char in "、。！？!?,.　 ":
            if current.strip():
                chunks.append(current.strip())
            current = ""
    if current.strip():
        chunks.append(current.strip())
    return chunks or [plain]


_BUDOUX = None
_BUDOUX_TRIED = False


def _budoux_parser():
    global _BUDOUX, _BUDOUX_TRIED
    if _BUDOUX_TRIED:
        return _BUDOUX
    _BUDOUX_TRIED = True
    try:
        import budoux                       # noqa: PLC0415 (任意依存)
        _BUDOUX = budoux.load_default_japanese_parser()
    except Exception:  # noqa: BLE001
        _BUDOUX = None
    return _BUDOUX


# 強調する語の集合 (設定の一覧 + テロップ 1 本ごとの指定 / resolve2 §2.2)
def _emphasis_set(entry, cfg):
    words = set()
    for value in (cfg or {}).get("emphasis_words") or ():
        text = str(value or "").strip()
        if text:
            words.add(text)
    raw = str((entry or {}).get("emphasis", "") or "")
    for separator in ("、", "，", ",", "　"):
        raw = raw.replace(separator, " ")
    for text in raw.split(" "):
        text = text.strip()
        if text:
            words.add(text)
    return words


# その語が強調対象か (部分一致。日本語の語境界は認識任せのため完全一致では当たらない)
def _is_emphasis(text, emphasis):
    if not emphasis:
        return False
    target = str(text or "").strip().lower()
    if not target:
        return False
    for word in emphasis:
        key = word.lower()
        if key and (key in target or target in key):
            return True
    return False


# 単語を行へ割り当てる (折り返しをまたぐ語は行の境目で分ける)
def _assign_lines(pieces, lines, emphasis):
    # 行ごとの残り文字数
    remains = [len(line) for line in lines]
    out = []
    index = 0
    for piece in pieces:
        text = piece["text"]
        while text and index < len(remains):
            if remains[index] <= 0:
                index += 1
                continue
            take = min(len(text), remains[index])
            head, text = text[:take], text[take:]
            remains[index] -= take
            out.append({
                "text": head,
                "offset": piece["offset"],
                "duration": piece["duration"],
                "line": index,
                "emphasis": _is_emphasis(head, emphasis),
            })
        if text:
            # 行の文字数より語が多い (本文と行の組み立てが食い違う): 最終行へ寄せる
            out.append({
                "text": text,
                "offset": piece["offset"],
                "duration": piece["duration"],
                "line": max(len(lines) - 1, 0),
                "emphasis": _is_emphasis(text, emphasis),
            })
    return out


# ------------------------------------------------------------------
# 単語の位置 (プレビューと共有する中間表現)
# ------------------------------------------------------------------

# テロップ 1 本の単語レイアウトを返す (測れなければ None = 行全体の動きへ落とす)
#
# 戻り値: {"size": ASS Fontsize, "line_height": px,
#          "words": [{"text","cx","cy","offset","duration","emphasis"}]}
#   cx / cy は**その語の中心**(px)。ASS では \an5\pos(cx,cy)、Qt では同じ点を中心に描く。
def layout(entry, cfg, eff_cfg, font_profile, video_width, video_height,
           anchor=None):
    words = split_words(entry, cfg)
    if not words:
        return None
    size = _font_size(entry, eff_cfg, font_profile)
    metrics = _metrics(entry, eff_cfg, font_profile)
    if metrics is None:
        _warn_once("unmeasured",
                   "フォントの送り幅を読めないため、テロップの動きを行単位にします "
                   "(subtitle.font_family / テロップ個別のフォントを見直すと解消します)")
        return None

    lines = _lines_of(entry)
    line_height = size * _LINE_ADVANCE_RATIO
    widths = [metrics.text_width(line, size) for line in lines]
    block_width = max(widths) if widths else 0.0
    block_height = line_height * max(len(lines), 1)

    box = _block_box(entry, anchor, block_width, block_height,
                     video_width, video_height)
    if box is None:
        return None
    left, top, align_column = box

    # 行ごとの左端 (libass は \an の水平成分に従って各行を寄せる)
    line_left = []
    for width in widths:
        if align_column == "left":
            line_left.append(left)
        elif align_column == "right":
            line_left.append(left + block_width - width)
        else:
            line_left.append(left + (block_width - width) / 2.0)

    # 行ごとに語を左から積む
    cursor = list(line_left)
    placed = []
    for word in words:
        index = min(max(int(word.get("line", 0)), 0), max(len(lines) - 1, 0))
        width = metrics.text_width(word["text"], size)
        cx = cursor[index] + width / 2.0
        cy = top + (index + 0.5) * line_height
        cursor[index] += width
        placed.append({**word, "cx": cx, "cy": cy, "width": width})
    return {"size": size, "line_height": line_height, "words": placed,
            "block": {"left": left, "top": top,
                      "width": block_width, "height": block_height}}


# 文字の塊の左上と行の寄せ方を返す ((left, top, "left"|"center"|"right"))
def _block_box(entry, anchor, block_width, block_height,
               video_width, video_height):
    if anchor is None:
        return None
    an = _to_int(anchor.get("an"), 2)
    x = _to_float(anchor.get("x"), 0.0)
    y = _to_float(anchor.get("y"), 0.0)
    # 水平: 1/4/7 = 左 / 3/6/9 = 右 / それ以外 = 中央
    if an in (1, 4, 7):
        left, column = x, "left"
    elif an in (3, 6, 9):
        left, column = x - block_width, "right"
    else:
        left, column = x - block_width / 2.0, "center"
    # 垂直: 1〜3 = 下段 (基準は下端) / 4〜6 = 中段 / 7〜9 = 上段
    if an <= 3:
        top = y - block_height
    elif an <= 6:
        top = y - block_height / 2.0
    else:
        top = y
    return left, top, column


def _font_size(entry, eff_cfg, font_profile):
    size = _to_int((entry or {}).get("font_size"), 0)
    if size > 0:
        return size
    size = _to_int((eff_cfg or {}).get("font_size"), 0)
    if size > 0:
        return size
    return _to_int(getattr(font_profile, "size", 48), 48) or 48


def _metrics(entry, eff_cfg, font_profile):
    family = (str((entry or {}).get("font", "") or "")
              or str((eff_cfg or {}).get("font_family", "") or "")
              or str(getattr(font_profile, "family", "") or ""))
    if not family:
        return None
    bold = bool((eff_cfg or {}).get("bold", getattr(font_profile, "bold", False)))
    return font_advance.metrics_for(family, eff_cfg, bold=bold)


# ------------------------------------------------------------------
# ASS イベントの組み立て
# ------------------------------------------------------------------

# アニメ付きエントリを Dialogue の列へ展開する
#
# 戻り値: [{"start": 絶対秒, "end": 絶対秒, "text": ASS 本文}]
#   空リストなら呼び出し側は従来どおり 1 行だけ書く (フェードのみ・機能 OFF・
#   単語へ切れなかった場合)。
#
# anchor: {"an": int, "x": px, "y": px} = テロップの基準点。
#   呼び出し側 (subtitle_generator) が既存の計算 (default_anchor / pos_x,pos_y) で渡す。
def events(entry, cfg, eff_cfg, font_profile, video_width, video_height,
           anchor=None):
    cfg = config(None) if cfg is None else cfg
    if not is_enabled(cfg):
        return []
    kind = animation_of(entry)
    if not kind:
        return []

    start = _to_float(entry.get("start"), 0.0)
    end = _to_float(entry.get("end"), 0.0)
    if end - start <= _MIN_EVENT_SEC:
        return []

    words = split_words(entry, cfg)
    if not words:
        return []
    limit = max(_to_int(cfg.get("max_words_per_clip"), 24), 1)
    too_many = len(words) > limit

    fade = fade_tag(entry, cfg)
    if kind == ANIM_BIG_WORD:
        # Big Word は位置を測らない (画面中央へ 1 語ずつ出すだけ)
        return _big_word_events(entry, cfg, eff_cfg, font_profile, words,
                                video_width, video_height, fade)

    plan = None if too_many else layout(
        entry, cfg, eff_cfg, font_profile, video_width, video_height, anchor=anchor)
    if plan is None:
        # 単語単位をあきらめ、行全体の動きへ落とす (resolve2 §10)
        if too_many:
            _warn_once("too_many_words",
                       "単語が多いテロップは行単位の動きにします (上限 %d 語)", limit)
        return _line_events(entry, cfg, eff_cfg, font_profile, kind,
                            video_width, video_height, anchor, fade)

    if kind == ANIM_WORD_POP:
        result = _word_pop_events(entry, cfg, plan, fade)
    else:
        result = _bounce_events(entry, cfg, plan, fade)

    max_events = max(_to_int(cfg.get("max_events_per_clip"), 96), 1)
    if len(result) > max_events:
        _warn_once("too_many_events",
                   "イベントが多いテロップは行単位の動きにします (上限 %d 件)",
                   max_events)
        return _line_events(entry, cfg, eff_cfg, font_profile, kind,
                            video_width, video_height, anchor, fade)
    return result


# Word Pop: 単語 1 つ = 1 イベント。語の時刻に現れ、拡大してから元の大きさへ戻る。
# 現れた語はテロップの終わりまで残る (読み返せるように)。
def _word_pop_events(entry, cfg, plan, fade):
    start = _to_float(entry.get("start"), 0.0)
    end = _to_float(entry.get("end"), 0.0)
    grow = max(_to_int(cfg.get("word_pop_grow_ms"), 80), 0)
    settle = max(_to_int(cfg.get("word_pop_settle_ms"), 120), 0)
    scale = max(_to_int(cfg.get("word_pop_scale"), 130), 100)
    emphasis_color = _ass_color(cfg.get("emphasis_color"))
    emphasis_scale = max(_to_int(cfg.get("emphasis_scale"), 115), 100)
    base_size = plan["size"]

    events_out = []
    for word in plan["words"]:
        word_start = start + word["offset"]
        if word_start >= end - _MIN_EVENT_SEC:
            word_start = max(end - _MIN_EVENT_SEC, start)
        tags = [f"\\an5\\pos({word['cx']:.1f},{word['cy']:.1f})"]
        if word["emphasis"]:
            # 強調語は色と大きさを変える (resolve2 §2.2)
            tags.append(f"\\c{emphasis_color}")
            tags.append(f"\\fs{int(round(base_size * emphasis_scale / 100.0))}")
        if fade:
            tags.append(fade)
        # 拡大 → 元の大きさへ戻す (\t は時刻がイベント開始からの相対 ms)
        tags.append(f"\\fscx{scale}\\fscy{scale}")
        if grow > 0:
            tags.append(f"\\t(0,{grow},\\fscx{scale}\\fscy{scale})")
        tags.append(f"\\t({grow},{grow + max(settle, 1)},\\fscx100\\fscy100)")
        events_out.append({
            "start": word_start,
            "end": end,
            "text": "{" + "".join(tags) + "}" + _escape(word["text"]),
        })
    return events_out


# Bounce: 単語 1 つを数段の \move で上下させ、最後に止める。
# ASS の \t は位置を動かせないため段に分ける (resolve2 §1)。
def _bounce_events(entry, cfg, plan, fade):
    start = _to_float(entry.get("start"), 0.0)
    end = _to_float(entry.get("end"), 0.0)
    total_ms = max(_to_int(cfg.get("bounce_ms"), 420), 60)
    segments = min(max(_to_int(cfg.get("bounce_segments"), 3), 1), 6)
    height = _to_float(cfg.get("bounce_height_ratio"), 0.35) * plan["size"]
    stagger = max(_to_int(cfg.get("bounce_word_stagger_ms"), 60), 0) / 1000.0

    events_out = []
    for index, word in enumerate(plan["words"]):
        base = start + word["offset"] + index * stagger
        if base >= end - _MIN_EVENT_SEC:
            base = max(end - _MIN_EVENT_SEC, start)
        cx, cy = word["cx"], word["cy"]
        head = "\\an5" + (fade if fade else "")
        cursor = base
        # 段ごとに「上がって下がる」を振幅を減らしながら繰り返す (バネの減衰)
        span = (total_ms / 1000.0) / segments
        for step in range(segments):
            amplitude = height * (0.5 ** step)
            up_end = min(cursor + span / 2.0, end)
            down_end = min(cursor + span, end)
            if up_end - cursor > _MIN_EVENT_SEC:
                events_out.append({
                    "start": cursor, "end": up_end,
                    "text": ("{" + head
                             + f"\\move({cx:.1f},{cy:.1f},{cx:.1f},{cy - amplitude:.1f},"
                               f"0,{int((up_end - cursor) * 1000)})}}"
                             + _escape(word["text"])),
                })
            if down_end - up_end > _MIN_EVENT_SEC:
                events_out.append({
                    "start": up_end, "end": down_end,
                    "text": ("{" + head
                             + f"\\move({cx:.1f},{cy - amplitude:.1f},{cx:.1f},{cy:.1f},"
                               f"0,{int((down_end - up_end) * 1000)})}}"
                             + _escape(word["text"])),
                })
            cursor = down_end
            if cursor >= end - _MIN_EVENT_SEC:
                break
        # 収まったあとはテロップの終わりまで静止して残す
        if end - cursor > _MIN_EVENT_SEC:
            events_out.append({
                "start": cursor, "end": end,
                "text": ("{" + head + f"\\pos({cx:.1f},{cy:.1f})}}"
                         + _escape(word["text"])),
            })
    return events_out


# Big Word: 画面中央へ 1 語ずつ大きく出す。前の語は消える (置き換わる)。
def _big_word_events(entry, cfg, eff_cfg, font_profile, words,
                     video_width, video_height, fade):
    start = _to_float(entry.get("start"), 0.0)
    end = _to_float(entry.get("end"), 0.0)
    scale = max(_to_int(cfg.get("big_word_scale"), 260), 100)
    min_sec = max(_to_int(cfg.get("big_word_min_ms"), 240), 0) / 1000.0
    size = int(round(_font_size(entry, eff_cfg, font_profile) * scale / 100.0))
    cx = video_width / 2.0
    cy = video_height / 2.0

    events_out = []
    for index, word in enumerate(words):
        word_start = start + word["offset"]
        # 次の語が出るまで (最後の語はテロップの終わりまで) 出しておく
        if index + 1 < len(words):
            word_end = start + words[index + 1]["offset"]
        else:
            word_end = end
        word_end = max(word_end, word_start + max(word["duration"], min_sec))
        word_end = min(word_end, end)
        if word_end - word_start <= _MIN_EVENT_SEC:
            continue
        tags = [f"\\an5\\pos({cx:.1f},{cy:.1f})\\fs{size}"]
        if fade:
            tags.append(fade)
        events_out.append({
            "start": word_start, "end": word_end,
            "text": "{" + "".join(tags) + "}" + _escape(word["text"]),
        })
    return events_out


# 行全体を動かす退避口 (単語の位置が測れない / 語数が多い場合)
#
# 単語ごとの \pos を使わないため、本文はいまの 1 行のままで、
# テロップ全体が「ポップ」または「バウンド」する。
def _line_events(entry, cfg, eff_cfg, font_profile, kind,
                 video_width, video_height, anchor, fade):
    start = _to_float(entry.get("start"), 0.0)
    end = _to_float(entry.get("end"), 0.0)
    text = _escape(str(entry.get("text", "") or "").replace("\n", "\\N"))
    if not text:
        return []

    if kind == ANIM_WORD_POP:
        grow = max(_to_int(cfg.get("word_pop_grow_ms"), 80), 0)
        settle = max(_to_int(cfg.get("word_pop_settle_ms"), 120), 0)
        scale = max(_to_int(cfg.get("word_pop_scale"), 130), 100)
        tags = [f"\\fscx{scale}\\fscy{scale}"]
        if grow > 0:
            tags.append(f"\\t(0,{grow},\\fscx{scale}\\fscy{scale})")
        tags.append(f"\\t({grow},{grow + max(settle, 1)},\\fscx100\\fscy100)")
        if fade:
            tags.append(fade)
        return [{"start": start, "end": end,
                 "text": "{" + "".join(tags) + "}" + text}]

    # Bounce (行単位)。基準点が分からないときは動かせないため諦める。
    if anchor is None:
        return []
    an = _to_int(anchor.get("an"), 2)
    x = _to_float(anchor.get("x"), 0.0)
    y = _to_float(anchor.get("y"), 0.0)
    size = _font_size(entry, eff_cfg, font_profile)
    height = _to_float(cfg.get("bounce_height_ratio"), 0.35) * size
    total_ms = max(_to_int(cfg.get("bounce_ms"), 420), 60)
    segments = min(max(_to_int(cfg.get("bounce_segments"), 3), 1), 6)
    head = f"\\an{an}" + (fade if fade else "")

    out = []
    cursor = start
    span = (total_ms / 1000.0) / segments
    for step in range(segments):
        amplitude = height * (0.5 ** step)
        up_end = min(cursor + span / 2.0, end)
        down_end = min(cursor + span, end)
        if up_end - cursor > _MIN_EVENT_SEC:
            out.append({"start": cursor, "end": up_end,
                        "text": ("{" + head
                                 + f"\\move({x:.1f},{y:.1f},{x:.1f},{y - amplitude:.1f},"
                                   f"0,{int((up_end - cursor) * 1000)})}}" + text)})
        if down_end - up_end > _MIN_EVENT_SEC:
            out.append({"start": up_end, "end": down_end,
                        "text": ("{" + head
                                 + f"\\move({x:.1f},{y - amplitude:.1f},{x:.1f},{y:.1f},"
                                   f"0,{int((down_end - up_end) * 1000)})}}" + text)})
        cursor = down_end
        if cursor >= end - _MIN_EVENT_SEC:
            break
    if end - cursor > _MIN_EVENT_SEC:
        out.append({"start": cursor, "end": end,
                    "text": "{" + head + f"\\pos({x:.1f},{y:.1f})}}" + text})
    return out


# ------------------------------------------------------------------
# 小物
# ------------------------------------------------------------------

# ASS の本文として安全な形にする (上書きタグの括弧だけ逃がす)
def _escape(text):
    return str(text or "").replace("{", "\\{").replace("}", "\\}")


# HTML 色 (#RRGGBB) → ASS 色 (&HBBGGRR&)
# subtitle_generator._hex_to_ass_color と同じ式。循環 import を避けるためここに持つ
# (comment_decor が ASS 色の分解を自分で持っているのと同じ理由)。
def _ass_color(color_hex):
    value = str(color_hex or "").strip().lstrip("#")
    if len(value) != 6:
        return "&HFFFFFF&"
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H{b}{g}{r}&".upper()


# コメント役割か (単語ごとの動きは背景の箱・アイコンが追従できないため除く)
def is_comment_role(entry):
    return str((entry or {}).get("role", "") or "") == "comment"


# 単語の数 (画面の注記用)
def word_count(entry, cfg=None):
    return len(split_words(entry, cfg))
