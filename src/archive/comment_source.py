# コメント(チャット)ソースの正規化・集計 (flow17 R3 / resolve17 §4.3.2・§4.4.1)
# twitch-dl の `chat json` 出力、または内部正規化フォーマットのどちらでも受け付け、
# 採点で使う「セルごとの ｗ数・コメント数」へ集計する。ML/外部依存は使わない (純粋 Python)。
#
# 内部正規化フォーマット (resolve17 §4.3.2):
#   [{"offset_sec": 1234.5, "user": "name", "text": "ｗｗｗ", "emotes": ["Kappa"]}]
#   offset_sec = VOD 先頭からの相対秒。
import json
import re

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 「笑い」を表す文字 (全角ｗ/半角w 双方・大文字含む)。resolve17 §4.4.1「ｗ/w の総数」。
# 英単語中の w による過大評価を避けるため、既定では「笑い文字のみで構成された連なり」を数える
# (下記 _LAUGH_RUN_RE を用いる)。単純な総数が欲しい場合は count_w_chars(text, runs_only=False)。
_LAUGH_CHARS = "wｗWＷ"
# 笑い文字(＋句読点/長音などのノイズ)だけで構成された 1 文字以上の連なりを検出する。
# 例: "ｗｗｗ" / "www" / "草www" の "www" 部分。英単語 "wow"/"world" は前後に他英字があり不一致。
_LAUGH_RUN_RE = re.compile(r"[wｗWＷ]+")
# ある連なりを「笑い」とみなすために、その前後が英字でないこと (単語の一部でないこと) を要求する。
_ALPHA_RE = re.compile(r"[A-Za-z]")


# テキスト中の「笑い」ｗ/w 文字数を数える (resolve17 §4.4.1)。
# runs_only=True (既定): 前後が英字でない ｗ/w の連なりのみを笑いとして加算し、
#   "world" 等の英単語を誤カウントしない。全角ｗ は常に笑い扱い。
# runs_only=False: テキスト中の ｗ/w を単純に総数カウントする。
def count_w_chars(text, runs_only=True):
    if not text:
        return 0
    if not runs_only:
        return sum(text.count(ch) for ch in _LAUGH_CHARS)

    total = 0
    for m in _LAUGH_RUN_RE.finditer(text):
        run = m.group(0)
        # 連なりが全角ｗを含むなら常に笑い。半角のみの連なりは、前後が英字でない時のみ笑いとみなす。
        has_fullwidth = ("ｗ" in run) or ("Ｗ" in run)
        prev_ch = text[m.start() - 1] if m.start() > 0 else ""
        next_ch = text[m.end()] if m.end() < len(text) else ""
        boundary_ok = not (_ALPHA_RE.match(prev_ch or "") or _ALPHA_RE.match(next_ch or ""))
        if has_fullwidth or boundary_ok:
            total += len(run)
    return total


# twitch-dl の 1 コメント(dict)を内部正規化フォーマットへ変換する。
# 期待キー: contentOffsetSeconds / commenter.displayName / message.fragments[].text / .emote.emoteID
def _normalize_twitchdl_comment(comment):
    if not isinstance(comment, dict):
        return None
    offset = comment.get("contentOffsetSeconds")
    if offset is None:
        return None
    message = comment.get("message") or {}
    fragments = message.get("fragments") or []
    text_parts = []
    emotes = []
    for frag in fragments:
        if not isinstance(frag, dict):
            continue
        text_parts.append(frag.get("text") or "")
        emote = frag.get("emote")
        if emote and isinstance(emote, dict) and emote.get("emoteID"):
            emotes.append(str(emote["emoteID"]))
    commenter = comment.get("commenter") or {}
    user = commenter.get("displayName") if isinstance(commenter, dict) else ""
    return {
        "offset_sec": float(offset),
        "user": str(user or ""),
        "text": "".join(text_parts),
        "emotes": emotes,
    }


# 既に内部正規化フォーマットの 1 要素 (offset_sec を持つ dict) を安全に整える。
def _coerce_normalized(item):
    if not isinstance(item, dict) or "offset_sec" not in item:
        return None
    try:
        offset = float(item["offset_sec"])
    except (TypeError, ValueError):
        return None
    emotes = item.get("emotes") or []
    if not isinstance(emotes, list):
        emotes = []
    return {
        "offset_sec": offset,
        "user": str(item.get("user", "") or ""),
        "text": str(item.get("text", "") or ""),
        "emotes": [str(e) for e in emotes],
    }


# 生データ (twitch-dl chat json の dict / コメント配列 / 内部正規化配列) を内部正規化配列へ統一する。
# 取得方式の差を吸収する (resolve17 §4.3.2)。offset_sec 昇順にソートして返す。
def normalize_comments(raw):
    # twitch-dl `chat json` は {"video":..., "video_comments":..., "comments":[...]} 形式。
    if isinstance(raw, dict) and "comments" in raw:
        raw = raw.get("comments") or []
    if not isinstance(raw, list):
        return []

    normalized = []
    for item in raw:
        if isinstance(item, dict) and "contentOffsetSeconds" in item:
            conv = _normalize_twitchdl_comment(item)
        else:
            conv = _coerce_normalized(item)
        if conv is not None:
            normalized.append(conv)
    normalized.sort(key=lambda c: c["offset_sec"])
    return normalized


# チャット JSON ファイルを読み込んで内部正規化配列を返す (twitch-dl 形式/内部形式の双方対応)。
# 読み込み/パース失敗は空配列で返し、採点は音声のみで継続できるようにする (resolve17 §5)。
def load_comments(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        _logger.warning("チャットJSON読み込みに失敗 (コメント無しで継続): %s", e)
        return []
    comments = normalize_comments(raw)
    _logger.info("チャット読み込み: %d件 (%s)", len(comments), path)
    return comments


# 動画全体の平均コメント数/分を返す (急増ボーナス判定の基準)。duration<=0 や無コメントなら 0。
def average_comments_per_min(comments, duration_sec):
    if not comments or duration_sec <= 0:
        return 0.0
    return len(comments) / (duration_sec / 60.0)


# 正規化コメントを各セルへ集計し、cells[i] の "w_count"/"comment_count" を更新する (in-place)。
# セル添字はセル幅 cell_sec からの割り当て (offset // cell_sec)。範囲外は端セルへクランプ。
# runs_only は count_w_chars に渡す (笑い連なりのみカウントするか)。
def aggregate_into_cells(comments, cells, cell_sec, runs_only=True):
    if not comments or not cells or cell_sec <= 0:
        return cells
    n = len(cells)
    for c in comments:
        idx = int(c["offset_sec"] // cell_sec)
        if idx < 0:
            idx = 0
        elif idx >= n:
            idx = n - 1
        cells[idx]["comment_count"] = cells[idx].get("comment_count", 0) + 1
        cells[idx]["w_count"] = cells[idx].get("w_count", 0) + count_w_chars(c["text"], runs_only)
    return cells
