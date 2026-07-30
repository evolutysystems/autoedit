# SRT サイドカー生成 (resolve21 §5.6)
# タイムライン時刻の字幕 entries から SRT 文字列を組み立てる純関数。
# Resolve の「Import Subtitle」(メディアプール右クリック) で字幕トラックへ確実に
# 取り込める形式であり、FCPXML の caption 取り込みが不調な環境のフォールバックとなる。
# スタイル情報は持たないため、フォント等は Resolve 側のトラックスタイルで設定する (§5.5)。


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
# entries: [{"start": 秒, "end": 秒, "text": str (実改行可)}, ...] (タイムライン時刻・出現順)
# テキストが空の entry と尺が 0 以下の entry は出力しない。連番は 1 起点。
def build_srt(entries):
    blocks = []
    number = 1
    for entry in entries or []:
        text = str(entry.get("text", "")).strip("\n")
        if not text.strip():
            continue
        start = float(entry.get("start", 0.0) or 0.0)
        end = float(entry.get("end", 0.0) or 0.0)
        if end <= start:
            continue
        blocks.append(
            f"{number}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n"
        )
        number += 1
    return "\n".join(blocks)
