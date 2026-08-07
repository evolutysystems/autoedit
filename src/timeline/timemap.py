# 元動画時間 ↔ Timeline 時間の写像 (docs/request/ver3/resolve.md §6.3.2)
# ベース映像トラック (V1) のクリップ列から都度構築する軽量オブジェクト。
#   ・プレビュー: 再生ヘッド (Timeline 時間) → 表示すべき素材とその時刻
#   ・字幕配置  : 音声認識結果 (元動画時間) → Timeline 時間
# カット済み区間 (無音として取り除いた部分) を跨ぐ区間は複数へ分割して返す。
# 詰めた結果 Timeline 上では連続するため、同一テキストを分割配置しても表示は途切れない。

# 浮動小数の比較に使う許容誤差 (ミリ秒精度で保持するため 1e-6 で十分)
_EPS = 1e-6


# 写像の 1 エントリ (Timeline 上の 1 クリップに対応する)
class _Entry:

    def __init__(self, timeline_start, duration, source_in, media_id, clip=None):
        self.timeline_start = float(timeline_start)
        self.duration = float(duration)
        self.source_in = float(source_in)
        self.media_id = media_id
        self.clip = clip

    @property
    def timeline_end(self):
        return self.timeline_start + self.duration

    @property
    def source_out(self):
        return self.source_in + self.duration


class TimeMap:

    # entries は _Entry の列 (Timeline 時間の昇順)。
    # body_media_id は「元動画」を指すメディア ID。to_timeline の既定対象になる。
    def __init__(self, entries, body_media_id=None):
        self._entries = sorted(entries, key=lambda e: e.timeline_start)
        self._body_media_id = body_media_id

    # ------------------------------------------------------------------
    # 構築
    # ------------------------------------------------------------------

    # 残す区間 [(source_start, source_end), ...] から構築する (無音カット直後の状態)
    # Timeline 時間は区間尺の累積。base_offset は先頭にずらす秒数 (OP の尺など)。
    @staticmethod
    def from_segments(keep_segments, media_id=None, base_offset=0.0):
        entries = []
        cursor = float(base_offset)
        for start, end in (keep_segments or []):
            duration = float(end) - float(start)
            if duration <= _EPS:
                continue
            entries.append(_Entry(cursor, duration, float(start), media_id))
            cursor += duration
        return TimeMap(entries, body_media_id=media_id)

    # Timeline のクリップ列から構築する (編集後の状態)
    # body_media_id 省略時は最も総尺の長いメディアを本編とみなす。
    @staticmethod
    def from_clips(clips, body_media_id=None):
        entries = [
            _Entry(c.timeline_start, c.duration, c.source_in, c.media_id, clip=c)
            for c in (clips or []) if c.duration > _EPS
        ]
        if body_media_id is None:
            body_media_id = _dominant_media_id(entries)
        return TimeMap(entries, body_media_id=body_media_id)

    # Timeline のベーストラックから構築する近道
    @staticmethod
    def from_timeline(timeline):
        body = (timeline.source or {}).get("media_id")
        return TimeMap.from_clips(timeline.base_clips(), body_media_id=body)

    # ------------------------------------------------------------------
    # 写像
    # ------------------------------------------------------------------

    # Timeline 時刻を含むエントリを返す (空白部分は None)
    def _entry_at(self, timeline_sec):
        value = float(timeline_sec)
        for entry in self._entries:
            if entry.timeline_start - _EPS <= value < entry.timeline_end - _EPS:
                return entry
        # 最終フレーム (終端ちょうど) は最後のエントリの末尾として扱う
        if self._entries:
            last = self._entries[-1]
            if abs(value - last.timeline_end) <= _EPS:
                return last
        return None

    # Timeline 時間 → (media_id, ソース時間)。空白 (クリップ無し) は None を返す。
    def to_source(self, timeline_sec):
        entry = self._entry_at(timeline_sec)
        if entry is None:
            return None
        offset = min(max(float(timeline_sec) - entry.timeline_start, 0.0), entry.duration)
        return entry.media_id, entry.source_in + offset

    # Timeline 時刻を含むクリップを返す (from_clips で構築した場合のみ有効)
    def clip_at(self, timeline_sec):
        entry = self._entry_at(timeline_sec)
        return None if entry is None else entry.clip

    # ソース時間 → Timeline 時間。カット済み区間内なら None を返す。
    # media_id 省略時は本編メディアを対象にする。
    def to_timeline(self, source_sec, media_id=None):
        target = media_id if media_id is not None else self._body_media_id
        value = float(source_sec)
        for entry in self._entries:
            if target is not None and entry.media_id != target:
                continue
            if entry.source_in - _EPS <= value < entry.source_out - _EPS:
                return entry.timeline_start + (value - entry.source_in)
        return None

    # ソース区間 [start, end] を Timeline 上の区間列へ分割する。
    # カットを跨ぐ場合は複数返る。交差が無ければ空リスト。
    def split_interval(self, source_start, source_end, media_id=None):
        target = media_id if media_id is not None else self._body_media_id
        start = float(source_start)
        end = float(source_end)
        if end - start <= _EPS:
            return []

        pieces = []
        for entry in self._entries:
            if target is not None and entry.media_id != target:
                continue
            overlap_start = max(start, entry.source_in)
            overlap_end = min(end, entry.source_out)
            if overlap_end - overlap_start <= _EPS:
                continue
            tl_start = entry.timeline_start + (overlap_start - entry.source_in)
            tl_end = entry.timeline_start + (overlap_end - entry.source_in)
            pieces.append((tl_start, tl_end))

        return _merge_adjacent(sorted(pieces))

    # ------------------------------------------------------------------
    # 情報
    # ------------------------------------------------------------------

    # Timeline 全長 (エントリの最終終端)
    def timeline_duration(self):
        return self._entries[-1].timeline_end if self._entries else 0.0

    # 元素材の合計尺 (本編メディアぶんのみ)
    def source_duration(self):
        return sum(
            e.duration for e in self._entries
            if self._body_media_id is None or e.media_id == self._body_media_id
        )

    def __len__(self):
        return len(self._entries)


# エントリ列から総尺が最大のメディア ID を返す (本編の推定)
def _dominant_media_id(entries):
    totals = {}
    for entry in entries:
        totals[entry.media_id] = totals.get(entry.media_id, 0.0) + entry.duration
    if not totals:
        return None
    return max(totals.items(), key=lambda kv: kv[1])[0]


# 隣接・連続する区間をまとめる (カットを跨いだ分割が Timeline 上で連続する場合)
def _merge_adjacent(pieces):
    merged = []
    for start, end in pieces:
        if merged and start - merged[-1][1] <= _EPS:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged
