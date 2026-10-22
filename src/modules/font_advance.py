# フォントの横メトリクス (送り幅) をフォントファイルから読む
# (docs/error/20261023/resolve.md §5 案A)
#
# なぜ要るか:
#   コメント字幕の背景ボックスの幅は、従来 設定の固定係数で見積もっていた。
#     全角 1 文字 = comment_bg_char_width_full x font_size
#     半角 1 文字 = comment_bg_char_width_half x font_size
#   ところが正しい係数は**フォントごとに 1.5 倍も違う** (resolve.md §3-2)。
#     07にくまるフォント 0.926 / 游ゴシック 0.777 / メイリオ 0.667 / MS ゴシック 1.000
#   さらに英数字がプロポーショナルなフォントでは「半角 = 全角の半分」も成り立たない
#   (にくまるフォントの W は 0.926em で、固定値 0.45 の 2 倍を超える)。
#   そのため文字が背景からはみ出していた。
#
# ここで読む 2 つの量:
#   ① 文字ごとの送り幅 (hmtx の advanceWidth / em)
#   ② libass の縮尺 unitsPerEm / (usWinAscent + usWinDescent)
#      libass は「usWinAscent + usWinDescent が Fontsize になる」ようスケールするため、
#      Fontsize 100 でも em は 100px にならない (gui/timeline/font_metrics.py と同じ性質)。
#   文字列の幅 = Σ送り幅(em) x 縮尺 x Fontsize で、libass の実出力と +2〜+10px で一致する
#   (resolve.md §5。常にわずかに広い側 = 文字がはみ出さない側へ外れる)。
#
# 立ち位置:
#   comment_decor から呼ぶため **Qt にも ffmpeg にも依存しない純 Python** として書く。
#   読めない環境・読めないフォントでは必ず None を返し、呼び出し側は従来の
#   固定係数へ落ちる (プレビューと出力を落とさないことを最優先にする)。
import os
import struct

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 読めるフォントファイルの拡張子 (settings_window.FONT_FILE_EXTENSIONS と同じ。
# PySide 依存を避けるため独立に持つ)
FONT_FILE_EXTENSIONS = (".ttf", ".otf", ".ttc")

# cmap に無い文字の送り幅 (em)。全角として数える = 安全側 (狭く見積もらない)。
_UNKNOWN_ADVANCE_EM = 1.0

# name テーブルから書体名として拾う nameID
#   1  = Font Family name
#   16 = Typographic Family name (可変ウェイトの族名)
# name テーブルから拾う (族名, スタイル名) の nameID の組。
#
# 1 つの書髒は 2 通りの名乗り方を持つ。
#   (1, 2)   = 4 スタイル単位の族名とスタイル名 ("Yu Gothic UI Light" + "Regular")
#   (16, 17) = ウェイトをまとめた族名とウェイト名  ("Yu Gothic UI" + "Light")
# この組を**混ぜずに**持つことが要点で、混ぜると Light の書髒が
# 「Yu Gothic UI の Regular」に見えてしまう。
_NAME_ID_PAIRS = ((1, 2), (16, 17))

# (書体名, 太字か) → FontAdvance / None (読めなかった記録も残して再探索しない)
_CACHE = {}
# フォルダ → {(正規化した書体名, 太字か): (パス, TTC 内の番号)}
_INDEX = {}


# テスト用: 覚えた内容をすべて捨てる
def reset_cache():
    _CACHE.clear()
    _INDEX.clear()


# 書体名を突き合わせる形へそろえる (大文字小文字と空白の違いを無視する)
def _normalize(name):
    return "".join(str(name or "").split()).casefold()


# src/settings ディレクトリの絶対パス (custom_fonts_dir の相対解決の基準)。
# settings_window.SETTINGS_DIR と同一だが、PySide 依存を避けるため独立に算出する
# (archive/config._settings_dir と同じ方針)。
def _settings_dir():
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings")


# フォントファイルを探すフォルダを順に返す (resolve.md §5 案A ①)
#   ① subtitle.custom_fonts_dir   = libass へ fontsdir で渡しているのと同じ場所。
#                                   同梱フォントはここで当たるため探索は 1 フォルダで済む
#   ② subtitle.font_file_dirs     = 利用者が足せる置き場 (既定は空)
#   ③ OS のフォントフォルダ        = 利用者が OS のフォントを選んだ場合
def search_dirs(eff_cfg):
    cfg = eff_cfg or {}
    dirs = []

    rel = str(cfg.get("custom_fonts_dir", "fonts") or "fonts")
    dirs.append(rel if os.path.isabs(rel) else os.path.join(_settings_dir(), rel))

    for extra in (cfg.get("font_file_dirs") or []):
        if extra:
            dirs.append(str(extra))

    for key in ("WINDIR", "SystemRoot"):
        base = os.environ.get(key, "")
        if base:
            dirs.append(os.path.join(base, "Fonts"))
            break
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))

    seen = set()
    ordered = []
    for path in dirs:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


# 書体名からメトリクスを引く。読めなければ None (呼び出し側は従来の係数へ落ちる)。
#
# bold を見るのは、太字の書髒を持つフォントでは英数字の送り幅が
# ウェイトで 5% ほど違うため (游ゴシック UI の W は Regular 0.915 / Bold 0.966 em)。
# libass も ASS Style の Bold に従って書髒を選ぶので、それに合わせる。
# 太字の書髒が無いフォント (同梱のにくまるなど) は libass が標準の書髒を
# 太らせて使い、送り幅は変わらない (実測済み) ため、見つからなければ
# ある方の書髒へ落とす。
def metrics_for(family, eff_cfg, bold=None):
    key = _normalize(family)
    if not key:
        return None
    if bold is None:
        bold = bool((eff_cfg or {}).get("bold", False))
    bold = bool(bold)
    if (key, bold) in _CACHE:
        return _CACHE[(key, bold)]
    found = None
    for folder in search_dirs(eff_cfg):
        index = _index_dir(folder)
        hit = index.get((key, bold)) or index.get((key, not bold))
        if hit is None:
            continue
        found = load(hit[0], hit[1])
        if found is not None:
            break
    if found is None:
        _logger.info("フォントの送り幅を読めませんでした (固定係数で見積もります): %s",
                     family)
    else:
        _logger.info("フォントの送り幅を読みました: %s%s "
                     "(upem %d / 縮尺 %.4f) %s",
                     family, " 太字" if bold else "", found.upem, found.scale,
                     os.path.basename(found.path))
    _CACHE[(key, bold)] = found
    return found


# フォルダ 1 つぶんの「(書体名, 太字か) → ファイル」の対応を作る
# (一度作ったら覚えておく)。
# 名前を見るだけなので name テーブルしか読まない (8MB の TTC でも数 KB で済む)。
def _index_dir(folder):
    cache_key = os.path.normcase(os.path.abspath(folder or ""))
    if cache_key in _INDEX:
        return _INDEX[cache_key]
    table = {}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        _INDEX[cache_key] = table
        return table
    best = {}
    for name in names:
        if os.path.splitext(name)[1].lower() not in FONT_FILE_EXTENSIONS:
            continue
        path = os.path.join(folder, name)
        for index, groups in _faces_in_file(path):
            for families, styles in groups:
                bold = _is_bold_style(styles)
                score = _style_score(styles, bold)
                for family in families:
                    key = (_normalize(family), bold)
                    if key not in best or score > best[key][0]:
                        best[key] = (score, path, index)
    for key, (_score, path, index) in best.items():
        table[key] = (path, index)
    _INDEX[cache_key] = table
    return table


# ファイル内の各書髒の名前を返す。
# 戻り値: [(TTC 内の番号, [({族名…}, {スタイル名…})…])…]
def _faces_in_file(path):
    result = []
    try:
        with open(path, "rb") as f:
            for index in range(_face_count(f)):
                tables = _table_directory(f, index)
                if tables is None or "name" not in tables:
                    continue
                groups = _font_names(f, tables["name"])
                if groups:
                    result.append((index, groups))
    except OSError:
        return []
    except (struct.error, ValueError, UnicodeDecodeError):
        _logger.debug("フォントの名前を読めませんでした: %s", path)
        return []
    return result


# スタイル名から太字の書髒かを見る ("Bold" / "Bold Italic" など)
def _is_bold_style(styles):
    return any("bold" in _normalize(style) for style in styles)


# 同じ (書体名, 太字か) に負ける書髒が複数あるときの優先度。
#
# 游ゴシック UI は Light / Regular / Medium / Bold が別ファイルにあり、
# ファイル名順で拾うと Light が当たってしまう (W が 0.895 em で
# Regular 0.915 em より**狭い** = 文字が箱からはみ出す側へ外れる)。
# libass は Bold が無ければ標準ウェイト (Regular) を選ぶため、それに合わせる。
# 斜体の書髒は同じウェイトの正体があるなら後回しにする。
def _style_score(styles, bold):
    text = " ".join(_normalize(style) for style in styles)
    score = 1
    if not bold and "regular" in text:
        score = 2
    if "italic" in text or "oblique" in text:
        score -= 1
    return score


# フォントファイルを読んで FontAdvance を作る。読めなければ None。
# index は TTC (複数の書体を 1 ファイルへ入れた形式) の中の番号。
def load(path, index=0):
    try:
        with open(path, "rb") as f:
            tables = _table_directory(f, index)
            if tables is None:
                return None
            head = _read_table(f, tables.get("head"), 20)
            os2 = _read_table(f, tables.get("OS/2"), 78)
            hhea = _read_table(f, tables.get("hhea"), 36)
            if head is None or os2 is None or hhea is None:
                return None
            upem = struct.unpack_from(">H", head, 18)[0]
            win_asc, win_desc = struct.unpack_from(">HH", os2, 74)
            metric_count = struct.unpack_from(">H", hhea, 34)[0]
            if upem <= 0 or win_asc + win_desc <= 0 or metric_count <= 0:
                return None
            hmtx = _read_table(f, tables.get("hmtx"), metric_count * 4)
            cmap = _read_table(f, tables.get("cmap"), 4)
            if hmtx is None or cmap is None:
                return None
    except OSError:
        return None
    except (struct.error, ValueError):
        _logger.debug("フォントのメトリクスを読めませんでした: %s", path)
        return None
    lookup = _CharToGlyph(cmap)
    if not lookup.usable():
        return None
    return FontAdvance(path, index, upem, win_asc + win_desc,
                       hmtx, metric_count, lookup)


# ------------------------------------------------------------------
# 1 書体ぶんの横メトリクス
# ------------------------------------------------------------------
class FontAdvance:

    def __init__(self, path, index, upem, win_height, hmtx, metric_count, lookup):
        self.path = path
        self.index = index
        self.upem = int(upem)
        self.win_height = int(win_height)
        # libass の縮尺。Fontsize に対する em の比 (resolve.md §3-1)
        self.scale = float(upem) / float(win_height)
        self._hmtx = hmtx
        self._metric_count = int(metric_count)
        self._lookup = lookup
        self._advances = {}

    # 文字 1 つの送り幅 (em)。cmap に無い文字は全角として数える (安全側)。
    def advance_em(self, char):
        code = ord(char)
        cached = self._advances.get(code)
        if cached is not None:
            return cached
        gid = self._lookup.glyph(code)
        if gid <= 0:
            value = _UNKNOWN_ADVANCE_EM
        else:
            at = min(gid, self._metric_count - 1) * 4
            value = struct.unpack_from(">H", self._hmtx, at)[0] / self.upem
        self._advances[code] = value
        return value

    # 文字列の送り幅の合計 (em)
    def text_em(self, text):
        return sum(self.advance_em(char) for char in str(text or ""))

    # 文字列が ASS Fontsize=size で描かれたときの幅 (px)
    #   = Σ送り幅(em) x 縮尺 x Fontsize
    def text_width(self, text, size):
        return self.text_em(text) * self.scale * float(size)


# ------------------------------------------------------------------
# sfnt (TTF / OTF / TTC) の最小限の読み取り
# ------------------------------------------------------------------

# ファイルに入っている書体の数 (TTC 以外は 1)
def _face_count(f):
    f.seek(0)
    header = f.read(12)
    if len(header) < 12:
        return 0
    if header[0:4] == b"ttcf":
        return struct.unpack_from(">I", header, 8)[0]
    return 1


# テーブルの位置と長さを返す {タグ: (開始位置, 長さ)}。読めなければ None。
def _table_directory(f, index=0):
    f.seek(0)
    header = f.read(12)
    if len(header) < 12:
        return None
    base = 0
    if header[0:4] == b"ttcf":
        count = struct.unpack_from(">I", header, 8)[0]
        if index >= count:
            return None
        f.seek(12 + index * 4)
        raw = f.read(4)
        if len(raw) < 4:
            return None
        base = struct.unpack(">I", raw)[0]
    f.seek(base + 4)
    raw = f.read(2)
    if len(raw) < 2:
        return None
    num_tables = struct.unpack(">H", raw)[0]
    f.seek(base + 12)
    raw = f.read(num_tables * 16)
    tables = {}
    for i in range(len(raw) // 16):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", raw, i * 16)
        try:
            tables[tag.decode("latin-1")] = (offset, length)
        except UnicodeDecodeError:
            continue
    return tables


# テーブルの中身を読む。必要な長さに足りなければ None。
def _read_table(f, entry, minimum):
    if entry is None:
        return None
    offset, length = entry
    if length < minimum:
        return None
    f.seek(offset)
    data = f.read(length)
    return data if len(data) >= minimum else None


# name テーブルから (族名, スタイル名) の組を拾う。
# すべての言語ぶん集めるので、英語名でも日本語名でも引ける。
# 戻り値: [({族名…}, {スタイル名…})…] (_NAME_ID_PAIRS の順)
def _font_names(f, entry):
    data = _read_table(f, entry, 6)
    if data is None:
        return []
    count, string_offset = struct.unpack_from(">HH", data, 2)
    collected = {}
    for i in range(count):
        at = 6 + i * 12
        if at + 12 > len(data):
            break
        platform, encoding, _lang, name_id, length, offset = \
            struct.unpack_from(">HHHHHH", data, at)
        if not any(name_id in pair for pair in _NAME_ID_PAIRS):
            continue
        start = string_offset + offset
        raw = data[start:start + length]
        if not raw:
            continue
        text = _decode_name(raw, platform, encoding)
        if text:
            collected.setdefault(name_id, set()).add(text)
    groups = []
    for family_id, style_id in _NAME_ID_PAIRS:
        families = collected.get(family_id)
        if not families:
            continue
        # スタイル名が無い組 (nameID 17 を持たないフォント) は
        # 4 スタイル側のスタイル名を借りる
        styles = collected.get(style_id) or collected.get(_NAME_ID_PAIRS[0][1]) or set()
        groups.append((families, styles))
    return groups


def _decode_name(raw, platform, encoding):
    # Windows (3) と Unicode (0) は UTF-16BE、Macintosh (1) Roman は latin-1
    codecs = ("utf-16-be",) if platform in (0, 3) else ("latin-1", "utf-16-be")
    for codec in codecs:
        try:
            return raw.decode(codec).strip("\x00").strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


# 文字コード → グリフ番号 (cmap format 4 / 12 を扱う)
class _CharToGlyph:

    def __init__(self, data):
        self._data = data
        self._format = 0
        self._sub = 0
        self._parse()

    def usable(self):
        return self._format in (4, 12)

    # 使うサブテーブルを選ぶ (Unicode が広く引けるものを優先する)
    def _parse(self):
        data = self._data
        try:
            count = struct.unpack_from(">H", data, 2)[0]
        except struct.error:
            return
        best = None
        priority = {(3, 10): 4, (0, 6): 4, (0, 4): 3, (3, 1): 2, (0, 3): 2}
        for i in range(count):
            at = 4 + i * 8
            if at + 8 > len(data):
                break
            platform, encoding, offset = struct.unpack_from(">HHI", data, at)
            score = priority.get((platform, encoding), 0)
            if not score or offset + 2 > len(data):
                continue
            fmt = struct.unpack_from(">H", data, offset)[0]
            if fmt not in (4, 12):
                continue
            if best is None or score > best[0]:
                best = (score, offset, fmt)
        if best is not None:
            _score, self._sub, self._format = best

    def glyph(self, code):
        if self._format == 4:
            return self._glyph_format4(code)
        if self._format == 12:
            return self._glyph_format12(code)
        return 0

    # format 4: 16bit の区間表。endCode の配列を二分探索する
    def _glyph_format4(self, code):
        if code > 0xFFFF:
            return 0
        data = self._data
        sub = self._sub
        try:
            seg_x2 = struct.unpack_from(">H", data, sub + 6)[0]
            segments = seg_x2 // 2
            ends = sub + 14
            starts = ends + seg_x2 + 2
            deltas = starts + seg_x2
            ranges = deltas + seg_x2
            low, high = 0, segments - 1
            while low <= high:
                mid = (low + high) // 2
                end = struct.unpack_from(">H", data, ends + mid * 2)[0]
                if code > end:
                    low = mid + 1
                    continue
                start = struct.unpack_from(">H", data, starts + mid * 2)[0]
                if code < start:
                    high = mid - 1
                    continue
                offset = struct.unpack_from(">H", data, ranges + mid * 2)[0]
                delta = struct.unpack_from(">h", data, deltas + mid * 2)[0]
                if offset == 0:
                    return (code + delta) & 0xFFFF
                at = ranges + mid * 2 + offset + (code - start) * 2
                gid = struct.unpack_from(">H", data, at)[0]
                return (gid + delta) & 0xFFFF if gid else 0
        except struct.error:
            return 0
        return 0

    # format 12: 32bit の区間表。グループを二分探索する
    def _glyph_format12(self, code):
        data = self._data
        sub = self._sub
        try:
            groups = struct.unpack_from(">I", data, sub + 12)[0]
            low, high = 0, groups - 1
            while low <= high:
                mid = (low + high) // 2
                at = sub + 16 + mid * 12
                start, end, gid = struct.unpack_from(">III", data, at)
                if code < start:
                    high = mid - 1
                elif code > end:
                    low = mid + 1
                else:
                    return gid + (code - start)
        except struct.error:
            return 0
        return 0


__all__ = [
    "FONT_FILE_EXTENSIONS",
    "FontAdvance",
    "load",
    "metrics_for",
    "reset_cache",
    "search_dirs",
]
