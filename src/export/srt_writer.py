# SRT サイドカー生成 (resolve21 §5.6)
# タイムライン時刻の字幕 entries から SRT 文字列を組み立てる純関数。
# Resolve の「Import Subtitle」(メディアプール右クリック) で字幕トラックへ確実に
# 取り込める形式であり、FCPXML の caption 取り込みが不調な環境のフォールバックとなる。
# スタイル情報は持たないため、フォント等は Resolve 側のトラックスタイルで設定する (§5.5)。
#
# SRT は「1 本のトラックを時刻の昇順に並べたもの」でなければならない。呼び出し側は
# 字幕トラックごとに entries を連結してくるため (本文トラックの後にコメントトラックが
# 続く)、ここで必ず時刻順へ並べ替える。並べ替えずに出すと時刻が逆行する SRT になり、
# Resolve や他の字幕ツールが取り込みに失敗したり位置を取り違える。
# 1 フレーム未満の重なり (テロップ整形の丸め) は後ろの開始時刻へ詰めて解消する。
# それ以上の重なりは別トラックの同時表示なので、SRT では表現できない旨を警告する。
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 詰めて解消する重なりの上限 (秒)。60fps の 1 フレーム強。
_SNAP_OVERLAP_SEC = 0.02


# 秒を SRT の時刻表記 (HH:MM:SS,mmm) へ整形する
def format_srt_time(seconds):
    try:
        total_ms = int(round(max(float(seconds), 0.0) * 1000))
    except (TypeError, ValueError):
        total_ms = 0
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


# entries から SRT 文字列を組み立てる
# entries: [{"start": 秒, "end": 秒, "text": str (実改行可),
#            "keep_blank": bool (任意)}, ...] (タイムライン時刻・出現順)
# テキストが空の entry と尺が 0 以下の entry は出力しない。連番は 1 起点。
# keep_blank=True の entry だけは空白のままでも残す
# (先頭 0 秒のアンカー字幕。意図的に空白なので「空だから落とす」対象外 / resolve5 §19-2c)。
def build_srt(entries):
    cues = []
    for entry in entries or []:
        text = str(entry.get("text", "")).strip("\n")
        if not text.strip() and not entry.get("keep_blank"):
            continue
        start = float(entry.get("start", 0.0) or 0.0)
        end = float(entry.get("end", 0.0) or 0.0)
        if end <= start:
            continue
        cues.append((start, end, text))

    # 時刻の昇順へ (同時刻は渡された順を保つ安定ソート)
    cues.sort(key=lambda c: (c[0], c[1]))
    cues = _resolve_overlaps(cues)

    blocks = [
        f"{number}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n"
        for number, (start, end, text) in enumerate(cues, 1)
    ]
    return "\n".join(blocks)


# 重なりを解消した cue 列を返す
# ・1 フレーム未満のわずかな重なり → 直前の終了を次の開始へ詰める (丸めによるもの)
# ・それ以上の重なり → 別トラックの同時表示。SRT は 1 本しか持てないので時刻はそのまま
#   残し、件数を 1 回だけ警告する (黙って落とさない)。
def _resolve_overlaps(cues):
    fixed = []
    simultaneous = 0
    for start, end, text in cues:
        if fixed:
            prev_start, prev_end, prev_text = fixed[-1]
            if prev_end > start:
                if prev_end - start <= _SNAP_OVERLAP_SEC:
                    fixed[-1] = (prev_start, start, prev_text)
                else:
                    simultaneous += 1
        fixed.append((start, end, text))
    if simultaneous:
        _logger.warning(
            "同時に表示される字幕 %d 件は SRT では重なったまま出力します "
            "(SRT は 1 トラックしか持てません)。重なりを分けたい場合は FCPXML の "
            "字幕 (caption) をそのまま使ってください。", simultaneous)
    return [(s, e, t) for s, e, t in fixed if e > s]
