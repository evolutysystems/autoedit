# アーカイブ切り抜き用の Timeline 構築・分割 (docs/request/ver3/resolve5.md §3-1 / §3-8)
#
# クリップ用 (src/timeline/builder.py) は素材 1 本ぶんの Timeline を組むが、
# アーカイブは VOD から切り出した複数クリップを扱うため、ここで専用の構築を行う。
#
#   build_archive_timeline() : 選ばれたクリップを TOP 順に 1 本の Timeline へ並べる
#   split_by_clip()          : 編集済み Timeline をクリップ単位へ切り直す (書き出し用)
#   to_export_entry()        : クリップ 1 件を Resolve 出力の entry 形式へ戻す
#
# 「1 本に並べて編集し、書き出しでクリップ単位へ戻す」ことで、
# クリップを跨いだ編集を可能にしつつ、テーマ演出・個別出力・結合といった
# 既存の書き出し処理 (clip_writer) をそのまま使い続けられる。
from ..timeline import commands, media_probe
from ..timeline.builder import timeline_config
from ..timeline.model import (
    BASE_AUDIO_TRACK_ID,
    BASE_SUBTITLE_TRACK_ID,
    BASE_VIDEO_TRACK_ID,
    BASE_Z_ORDER,
    DEFAULT_ROLE,
    DEFAULT_SUBTITLE_Z_ORDER,
    ORIGIN_ASR,
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    SubtitleClip,
    Timeline,
    Track,
)
from ..timeline.timemap import TimeMap
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 極短の区間は Timeline に載せない (builder._MIN_SEGMENT_SEC と同じ)
_MIN_SEGMENT_SEC = 0.01

# クリップ由来を記録する origin のキー。書き出し時の分割にこれを使う (§3-8)
ORIGIN_ARCHIVE_INDEX = "archive_clip_index"


# ==================================================================
# 構築
# ==================================================================

# 選ばれたクリップを TOP 順に 1 本の Timeline へ並べる (§3-1)
# prepared      : clip_writer._prepare_clips の戻り値 (TOP 順)
#                 {index,start,end,score,normalized_path,normalized_duration,
#                  keep_segments,items,profile,...}
#                 Timeline 経路では実カットを行わないため prepared_path は空文字であり、
#                 素材は normalized_path (無音カット前・正規化済み) を参照する。
# clip_settings : OP/ED を無効化した設定コピー (_build_clip_settings の戻り値)。
#                 これを渡すことで OP/ED は Timeline へ入らない (§3-3)。
# source_path   : 元 VOD のパス (記録用)
# curve         : 採点グラフの窓スコア列 (ver3 resolve9 §3-2)。
#                 保存したプロジェクトを開き直したときもグラフを出せるよう
#                 source.archive.curve へ間引いて残す。
def build_archive_timeline(prepared, clip_settings, source_path="", curve=None):
    cfg = timeline_config(clip_settings)
    ffmpeg_cfg = (clip_settings or {}).get("ffmpeg", {})
    profile = next((p.get("profile") for p in prepared if p.get("profile")), None)
    canvas_w = int(profile["width"]) if profile else int(ffmpeg_cfg.get("output_width", 1920))
    canvas_h = int(profile["height"]) if profile else int(ffmpeg_cfg.get("output_height", 1080))
    orientation = (profile or {}).get("orientation", "landscape")
    fps = int(ffmpeg_cfg.get("output_fps", 60) or 60)

    timeline = Timeline(
        fps=fps, width=canvas_w, height=canvas_h, orientation=orientation,
        zoom_px_per_sec=cfg["default_zoom_px_per_sec"],
    )
    video_track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    audio_track = Track(BASE_AUDIO_TRACK_ID, TRACK_AUDIO, 1, name="Audio 1",
                        link_track=BASE_VIDEO_TRACK_ID)
    subtitle_track = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    timeline.tracks = [video_track, audio_track, subtitle_track]

    cursor = 0.0
    clip_meta = []
    for entry in prepared or []:
        media = media_probe.probe(
            entry["normalized_path"], timeline.next_id("m"), clip_settings, cfg["media"])
        duration_hint = float(entry.get("normalized_duration") or 0.0)
        if duration_hint > 0:
            media.duration_sec = duration_hint
        timeline.media_pool.append(media)

        segments = _segments_of(entry, media)
        clip_start = cursor
        for seg_index, (start, end) in enumerate(segments):
            duration = float(end) - float(start)
            if duration <= _MIN_SEGMENT_SEC:
                continue
            clip_id = timeline.next_id("c")
            video_track.clips.append(Clip(
                clip_id, media.id, cursor, duration,
                source_in=float(start), source_out=float(end),
                z_order=BASE_Z_ORDER,
                # どのアーカイブクリップ由来かを残す (書き出し時の分割に使う / §3-8)
                origin={"type": ORIGIN_SILENCE_CUT,
                        ORIGIN_ARCHIVE_INDEX: entry["index"],
                        "segment_index": seg_index},
            ))
            if media.has_audio:
                audio_track.clips.append(AudioClip(timeline.next_id("a"), clip_id))
            cursor += duration

        # 字幕はクリップの開始位置ぶんだけ後ろへずらし、クリップの尺で打ち切る
        # (builder._append_subtitles と同じ規約 + 20260812 resolve2.md §5-1)
        append_subtitles(timeline, subtitle_track, entry.get("items"),
                         clip_start, cursor - clip_start, segments,
                         media.id, entry["index"])

        clip_meta.append({
            "index": entry["index"],
            "vod_start": float(entry.get("start", 0.0)),
            "vod_end": float(entry.get("end", 0.0)),
            "score": float(entry.get("score", 0.0)),
            "timeline_start": clip_start,
            "timeline_end": cursor,
            "media_id": media.id,
            # テーマは Timeline モデルに持たせず、ここへ持たせて保存対象にする
            # (ver3 resolve9 §3-2 / §3-3)
            "theme": str(entry.get("theme", "") or ""),
            # 素材が「正規化後」か「切り出しそのまま」か。開き直すときに
            # 音声サイドカーを貼るか、正規化をやり直すかの判断に使う (§3-1)
            "media_role": str(entry.get("media_role", "normalized") or "normalized"),
        })

    # 採点グラフとの対応付け (VOD 時間) を残す。編集で失われないよう Timeline 側に持たせる。
    timeline.source = {
        "input_path": source_path,
        "duration_sec": cursor,
        "archive": {
            "vod_path": source_path,
            "clips": clip_meta,
            # 採点グラフを再現するための窓スコア列 (ver3 resolve9 §3-2)
            "curve": _trim_curve(curve),
        },
    }
    timeline.normalize()
    _logger.info(
        "アーカイブ Timeline 構築: %d クリップ / V1 %d 本 / S1 %d 件 / 全長 %.1fs",
        len(clip_meta), len(video_track.clips), len(subtitle_track.clips),
        timeline.duration_sec(),
    )
    return timeline


# 採点グラフの窓スコア列を保存用へ間引く (ver3 resolve9 §3-2)
# グラフが使うのは start/end/total で、内訳 (emotion/comment) は表示の補助。
# 窓は slide_sec ごとのため 5 時間の VOD でも 400 件程度に収まる。
def _trim_curve(curve):
    trimmed = []
    for entry in (curve or []):
        if not isinstance(entry, dict):
            continue
        trimmed.append({
            "start": float(entry.get("start", 0.0)),
            "end": float(entry.get("end", 0.0)),
            "emotion": float(entry.get("emotion", 0.0)),
            "comment": float(entry.get("comment", 0.0)),
            "total": float(entry.get("total", 0.0)),
        })
    return trimmed


# クリップ内の残す区間を返す。無音カット無効 (keep_segments=None) ならクリップ全長。
def _segments_of(entry, media):
    segments = entry.get("keep_segments")
    if segments:
        return [(float(s), float(e)) for s, e in segments]
    duration = float(entry.get("normalized_duration") or media.duration_sec or 0.0)
    return [(0.0, duration)] if duration > _MIN_SEGMENT_SEC else []


# 字幕 items を字幕トラックへ載せる
# items の時刻は「残す区間を詰めた後」の時間軸 (recognize_for_timeline が素材時間から
# TimeMap で写すため、上の V1 の積み上げと同一の軸になる)。
# クリップの開始位置ぶんだけ offset を一律で加える。
#
# クリップ尺 (clip_duration) を越える表示は終端で打ち切る (20260812 resolve2.md §5-1)。
# アーカイブはクリップを隙間なく連結するため、越えた分はそのまま次クリップの領域へ
# 重なって表示され、書き出しでも次クリップの冒頭に焼かれてしまう
# (docs/error/20260812/Analyze.md §4)。整形の「発話末 +2 秒」がここに現れる。
#
# 由来の素材内時刻は TimeMap で逆算して origin へ残す (クリップ用と同じ扱い)。
def append_subtitles(timeline, subtitle_track, items, offset, clip_duration,
                     segments, media_id, clip_index):
    timemap = TimeMap.from_segments(segments, media_id=media_id)
    trimmed = 0
    dropped = 0
    for item in (items or []):
        start = float(item.get("start", 0.0))
        end = float(item.get("end", 0.0))
        # クリップ終端で打ち切る (docs/error/20260812/Analyze.md §9-1 Q1: 短縮)
        if end > clip_duration:
            end = clip_duration
            trimmed += 1
        duration = end - start
        if duration <= _MIN_SEGMENT_SEC:
            # 打ち切った結果ほぼ残らない = 実質クリップの外側 → 載せない
            dropped += 1
            continue
        origin = {"type": ORIGIN_ASR, ORIGIN_ARCHIVE_INDEX: clip_index}
        source_start = timemap.to_source(start)
        source_end = timemap.to_source(end)
        if source_start is not None:
            origin["source_start"] = round(source_start[1], 3)
        if source_end is not None:
            origin["source_end"] = round(source_end[1], 3)
        subtitle_track.clips.append(SubtitleClip(
            timeline.next_id("s"),
            timeline_start=start + offset,
            duration=duration,
            text=item.get("text", ""),
            role=item.get("role", DEFAULT_ROLE),
            font=item.get("font", ""),
            font_size=item.get("font_size"),
            use=item.get("use", True),
            z_order=DEFAULT_SUBTITLE_Z_ORDER,
            origin=origin,
        ))
    if trimmed or dropped:
        _logger.info(
            "clip%s: クリップ終端で打ち切った字幕 %d 件 / 載せなかった字幕 %d 件",
            clip_index, trimmed, dropped)


# ==================================================================
# 分割 (書き出し用 / §3-8)
# ==================================================================

# 編集済み Timeline をクリップ単位の Timeline へ切り直す。
# 戻り値: [(archive_clip_index, Timeline)] を時系列順に。
#
# 分割規則:
#   1. V1 を timeline_start 順に見て、archive_clip_index が変わったところで区切る
#      (同じクリップ由来でも離れて置かれていれば別グループになる)
#   2. グループの範囲に入る字幕・オーバーレイを同じグループへ入れる
#   3. グループ内の全要素からグループ先頭の時刻を引いて 0 起点へ直す
#   4. 有効なクリップが 1 本も無いグループ (= 使用しないクリップ) は捨てる
def split_by_clip(timeline):
    base = timeline.base_video_track()
    if base is None:
        return []

    groups = []
    current = None
    for clip in sorted(base.clips, key=lambda c: c.timeline_start):
        index = clip.origin.get(ORIGIN_ARCHIVE_INDEX)
        if current is None or current["index"] != index:
            current = {"index": index, "clips": []}
            groups.append(current)
        current["clips"].append(clip)

    result = []
    for group in groups:
        enabled = [c for c in group["clips"] if c.enabled]
        if not enabled:
            _logger.info("clip%s は使用しないため書き出しから除外します", group["index"])
            continue
        result.append((group["index"], _build_group_timeline(timeline, enabled)))
    return result


# グループ 1 つぶんを 0 起点の Timeline として組み直す
def _build_group_timeline(timeline, clips):
    offset = min(c.timeline_start for c in clips)
    end = max(c.timeline_end for c in clips)

    sub = Timeline(
        fps=timeline.fps, width=timeline.width, height=timeline.height,
        orientation=timeline.orientation, zoom_px_per_sec=timeline.zoom_px_per_sec,
    )
    # メディアプールは参照を共有する (書き出しは読むだけのため複製しない)
    sub.media_pool = list(timeline.media_pool)

    video_track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    audio_track = Track(BASE_AUDIO_TRACK_ID, TRACK_AUDIO, 1, name="Audio 1",
                        link_track=BASE_VIDEO_TRACK_ID)
    subtitle_track = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    sub.tracks = [video_track, audio_track, subtitle_track]
    # 元の字幕トラック構成 (S1 / コメント用 S2 …) をそのまま複製する
    # (ver3 resolve11 §5.9)。1 本へ潰すと S1 と S2 の字幕が同じトラックで重なり、
    # 読み込み時に _validate_overlaps が片方を右へ押し出してしまう。
    subtitle_tracks = {subtitle_track.id: subtitle_track}
    for track in sorted(timeline.subtitle_tracks(), key=lambda t: t.index):
        if track.id in subtitle_tracks:
            continue
        copied = Track(track.id, TRACK_SUBTITLE, track.index, name=track.name)
        subtitle_tracks[track.id] = copied
        sub.tracks.append(copied)

    for clip in sorted(clips, key=lambda c: c.timeline_start):
        moved = clip.copy()
        moved.timeline_start = clip.timeline_start - offset
        video_track.clips.append(moved)
        # リンクした音声も一緒に運ぶ (時刻は持たないためコピーするだけ)
        linked = timeline.audio_clip_for(clip.id)
        if linked is not None:
            audio_track.clips.append(linked.copy())

    # 範囲に交差する字幕 (グループの外へ出る分は打ち切る / 20260812 resolve2.md §5-2)
    # 開始時刻だけで振り分けると、境界を跨ぐ字幕が丸ごと隣のクリップへ移ってしまう
    # (docs/error/20260812/Analyze.md §4-3)
    for track in timeline.subtitle_tracks():
        for subtitle in track.clips:
            start = max(subtitle.timeline_start, offset)
            stop = min(subtitle.timeline_end, end)
            if stop - start <= _MIN_SEGMENT_SEC:
                continue
            moved = subtitle.copy()
            moved.timeline_start = start - offset
            moved.duration = stop - start
            # 元のトラックに対応する側へ入れる (役割ごとの重なりを保つ)
            subtitle_tracks.get(track.id, subtitle_track).clips.append(moved)

    # 範囲に入るオーバーレイ (V2 以降。D&D で足した素材など)
    base_id = timeline.base_video_track().id if timeline.base_video_track() else None
    for track in timeline.video_tracks():
        if track.id == base_id:
            continue
        moved_clips = []
        for clip in track.clips:
            if offset - 1e-6 <= clip.timeline_start < end - 1e-6:
                moved = clip.copy()
                moved.timeline_start = clip.timeline_start - offset
                moved_clips.append(moved)
        if moved_clips:
            overlay = Track(track.id, TRACK_VIDEO, track.index, name=track.name,
                            enabled=track.enabled, locked=track.locked,
                            clips=moved_clips)
            sub.tracks.append(overlay)

    sub.normalize()
    return sub


# ==================================================================
# 編集コマンド
# ==================================================================

# クリップ 1 件ぶんの V1 クリップをまとめて有効/無効にする (resolve5 §3-4)
# 「このクリップを使用する / しない」のチェックに対応する。
# 無効にしたクリップは Timeline 上で灰色になり、書き出しからも外れる
# (renderer._build_segments が base_clips() を使うため)。
# 履歴はスナップショット方式のため、複数クリップの変更でも Undo は 1 回で戻る。
class SetArchiveClipEnabled(commands.Command):

    label = "クリップ使用の切り替え"

    def __init__(self, clip_index, enabled):
        self._clip_index = clip_index
        self._enabled = bool(enabled)

    def apply(self, timeline):
        track = timeline.base_video_track()
        if track is None:
            return False
        changed = False
        for clip in track.clips:
            if clip.origin.get(ORIGIN_ARCHIVE_INDEX) != self._clip_index:
                continue
            if clip.enabled != self._enabled:
                clip.enabled = self._enabled
                changed = True
        return changed


# クリップ 1 件のテーマを変更する (ver3 resolve9 §3-3)
# テーマは Timeline モデルではなく source.archive.clips[].theme に持つため、
# Undo と「未保存の印」に載せるにはコマンド経由で書き換える必要がある。
# (履歴のスナップショットは source も控えている / commands._snapshot)
class SetArchiveClipTheme(commands.Command):

    label = "テーマの変更"

    def __init__(self, clip_index, theme):
        self._clip_index = clip_index
        self._theme = str(theme or "").strip()

    def apply(self, timeline):
        entry = clip_entry(timeline, self._clip_index)
        if entry is None:
            return False
        if str(entry.get("theme", "") or "") == self._theme:
            return False        # 変化なし → 履歴を汚さない
        entry["theme"] = self._theme
        return True


# source.archive セクションを返す (無ければ空辞書)
def archive_section(timeline):
    source = getattr(timeline, "source", None) or {}
    archive = source.get("archive") if isinstance(source, dict) else None
    return archive if isinstance(archive, dict) else {}


# source.archive.clips から 1 件を引く (無ければ None)
def clip_entry(timeline, clip_index):
    for entry in (archive_section(timeline).get("clips") or []):
        if isinstance(entry, dict) and entry.get("index") == clip_index:
            return entry
    return None


# クリップのテーマ (未設定は空文字)
def clip_theme(timeline, clip_index):
    entry = clip_entry(timeline, clip_index)
    return str((entry or {}).get("theme", "") or "")


# クリップ 1 件が使用状態か (V1 クリップが 1 本でも有効なら使用中とみなす)
def is_clip_enabled(timeline, clip_index):
    track = timeline.base_video_track()
    if track is None:
        return False
    return any(c.enabled for c in track.clips
               if c.origin.get(ORIGIN_ARCHIVE_INDEX) == clip_index)


# 指定時刻に乗っているクリップの index を返す (どのクリップにも乗っていなければ None)
def clip_index_at(timeline, timeline_sec):
    track = timeline.base_video_track()
    if track is None:
        return None
    for clip in track.clips:
        if clip.timeline_start <= timeline_sec < clip.timeline_end:
            return clip.origin.get(ORIGIN_ARCHIVE_INDEX)
    return None


# 指定クリップの Timeline 上の範囲を返す ((開始, 終了) / 無ければ None)
def clip_range(timeline, clip_index):
    track = timeline.base_video_track()
    if track is None:
        return None
    clips = [c for c in track.clips
             if c.origin.get(ORIGIN_ARCHIVE_INDEX) == clip_index]
    if not clips:
        return None
    return min(c.timeline_start for c in clips), max(c.timeline_end for c in clips)


# ==================================================================
# セクションの追加 (ver3 resolve13)
# ==================================================================
#
# 編集画面から「元動画の区間」を指定してセクションを足す。挿入位置は元動画の
# 時系列で決め、既存セクションと重なる (または接する) 場合は 1 つへ統合する。
#
#   plan_section_add()  : 何を用意し、何と統合するかを決める (副作用なし)
#   AddArchiveSection   : 用意できた素材を Timeline へ入れるコマンド (Undo 対象)
#
# 採点方式 (archive/scoring.py) には一切触れない。追加は「採点結果へ後から足す」
# 操作であり、スコアは既存の窓スコア列から推定する (estimate_section_score)。

# セクションの重なり判定の許容誤差
_SECTION_EPS = 1e-6


# source.archive.clips のうち Timeline 上に実体があるものを vod_start 昇順で返す。
# 実体が無い (= 全クリップを消された) セクションは並びの基準にしない。
def sections_by_vod(timeline):
    rows = []
    for entry in (archive_section(timeline).get("clips") or []):
        if not isinstance(entry, dict) or entry.get("index") is None:
            continue
        if clip_range(timeline, entry.get("index")) is None:
            continue
        rows.append(entry)
    rows.sort(key=lambda e: float(e.get("vod_start", 0.0)))
    return rows


# 未使用のセクション番号 (既存の最大 + 1)。
# 番号は振り直さない (resolve13 §3-3): 番号は V1/字幕の origin・テーマ・使用可否を
# 繋ぐ唯一のキーであり、振り直すと全参照が壊れる。並び順は Timeline 位置が表す。
def next_section_index(timeline):
    used = [int(entry.get("index"))
            for entry in (archive_section(timeline).get("clips") or [])
            if isinstance(entry, dict) and entry.get("index") is not None]
    return (max(used) + 1) if used else 1


# 区間 [start, end] から covered (既存セクションの区間) を差し引いた残りを返す。
# 「まだ素材が無く、これから用意しなければならない区間」がこれにあたる。
def _subtract_covered(start, end, covered):
    out = []
    cursor = start
    for cover_start, cover_end in sorted(covered):
        if cover_start > cursor + _MIN_SEGMENT_SEC:
            out.append((cursor, min(cover_start, end)))
        cursor = max(cursor, cover_end)
        if cursor >= end - _MIN_SEGMENT_SEC:
            break
    if end > cursor + _MIN_SEGMENT_SEC:
        out.append((cursor, end))
    return [(s, e) for s, e in out if e - s > _MIN_SEGMENT_SEC]


# 追加操作の計画を立てる (副作用なし / resolve13 §3-2・§3-4)
#
# 重なり判定は scoring._merge_time_sections と同じ規約にする (接触も統合)。
# 規約を 2 つ持つと、採点で統合された結果と手動追加の結果が食い違うため。
#
# 戻り値 (追加するものが無ければ None):
#   {"ranges"       : [(開始, 終了)]  これから用意する VOD 区間 (昇順・既存と重ならない)
#    "merge_indexes": [番号]          統合される既存セクション (昇順 / 単独追加なら空)
#    "target_index" : 番号 or None    統合先 (= merge_indexes[0] / 単独追加なら None)
#    "span"         : (開始, 終了)    追加後のセクションの VOD 区間 (統合時は和集合)}
def plan_section_add(timeline, start_sec, end_sec, merge_on_overlap=True):
    start = float(start_sec)
    end = float(end_sec)
    if end - start <= _MIN_SEGMENT_SEC:
        return None

    touched = []
    if merge_on_overlap:
        for entry in sections_by_vod(timeline):
            entry_start = float(entry.get("vod_start", 0.0))
            entry_end = float(entry.get("vod_end", 0.0))
            # 接触も重なりとみなす (scoring._merge_time_sections と同規約)
            if start <= entry_end + _SECTION_EPS and entry_start <= end + _SECTION_EPS:
                touched.append(entry)

    if not touched:
        return {"ranges": [(start, end)], "merge_indexes": [],
                "target_index": None, "span": (start, end)}

    span_start = min(start, min(float(e.get("vod_start", 0.0)) for e in touched))
    span_end = max(end, max(float(e.get("vod_end", 0.0)) for e in touched))
    covered = [(float(e.get("vod_start", 0.0)), float(e.get("vod_end", 0.0)))
               for e in touched]
    ranges = _subtract_covered(span_start, span_end, covered)
    if not ranges:
        return None                      # 既存セクションに完全に含まれている
    return {
        "ranges": ranges,
        "merge_indexes": [e.get("index") for e in touched],
        "target_index": touched[0].get("index"),
        "span": (span_start, span_end),
    }


# 追加区間のスコアを窓スコア列から推定する (resolve13 §5.4)
# 採点はやり直さない (要望 G2)。区間に重なる窓の total の最大値を採り、
# 重なる窓が 1 つも無ければ 0.0 とする。
def estimate_section_score(curve, start_sec, end_sec):
    best = 0.0
    for entry in (curve or []):
        if not isinstance(entry, dict):
            continue
        window_start = float(entry.get("start", 0.0))
        window_end = float(entry.get("end", 0.0))
        if window_start < float(end_sec) and float(start_sec) < window_end:
            best = max(best, float(entry.get("total", 0.0)))
    return round(best, 1)


# 追加・統合の結果を prepared (書き出しが使う準備済みデータ) へ反映する。
# 統合時は代表セクションの 1 件だけを残し、VOD 区間を和集合へ広げる。
# start/end はイントロカードの切り出し位置 (clip_writer._build_intro_card) と
# 個別出力のファイル名に使われるため、更新を忘れると統合後もイントロが
# 古い位置の絵になる (resolve13 §3-4 ⑥)。
def apply_prepared_after_add(prepared, plan, new_entries):
    rows = [dict(row) for row in (prepared or [])]
    target = (plan or {}).get("target_index")
    if target is None:
        return rows + list(new_entries or [])

    span_start, span_end = plan["span"]
    merged = set(plan.get("merge_indexes") or [])
    merged.discard(target)
    out = []
    for row in rows:
        if row.get("index") == target:
            row["start"] = span_start
            row["end"] = span_end
            out.append(row)
        elif row.get("index") in merged:
            continue                     # 代表へ畳んだため落とす
        else:
            out.append(row)
    return out


# 用意できたセクション素材を Timeline へ挿入するコマンド (resolve13 §5.5)
#
# entries       : clip_writer.prepare_one_clip の戻り値の一覧。
#                 統合時は「足りない差分」ぶんが複数入る (§3-4 案B)。
#                 各要素は固有の index を持つ (挿入位置の算出を汚さないため)。
# clip_settings : media_probe へ渡す設定 (OP/ED 無効化済みのコピー)
# plan          : plan_section_add の戻り値
#
# 履歴はスナップショット方式のため逆操作は書かない。メディアプール・全トラック・
# source がまとめて控えられており、Undo で完全に戻る (commands._snapshot)。
class AddArchiveSection(commands.Command):

    label = "セクションの追加"

    def __init__(self, entries, clip_settings, plan, min_clip_sec=0.05):
        self._entries = list(entries or [])
        self._clip_settings = clip_settings or {}
        self._plan = plan or {}
        self._min_clip_sec = float(min_clip_sec)

    def apply(self, timeline):
        video = timeline.base_video_track()
        if video is None or not self._entries:
            return False
        cfg = timeline_config(self._clip_settings)
        audio = timeline.audio_track_for(video.id)
        subtitle = self._subtitle_track(timeline)
        if subtitle is None:
            return False

        # 挿入位置の基準は「追加前の」セクションの並び。追加した差分を基準に含めると
        # 位置が自分自身へ引きずられるため、最初に 1 度だけ控える。
        anchors = sections_by_vod(timeline)
        # archive_section() は source.archive が無いとき使い捨ての {} を返すため、
        # そこへ書いても Timeline へ残らない。アーカイブ用でなければ何もしない。
        archive = archive_section(timeline)
        if not archive:
            _logger.warning("アーカイブ用 Timeline ではないためセクションを追加できません")
            return False
        meta = archive.setdefault("clips", [])

        added = False
        for entry in sorted(self._entries, key=lambda e: float(e.get("start", 0.0))):
            if self._insert_one(timeline, entry, cfg, video, audio, subtitle,
                                anchors, meta):
                added = True
        if not added:
            return False

        self._merge_sections(timeline, meta)
        return True

    # 字幕は構築時と同じ S1 へ載せる (build_archive_timeline と揃える)
    def _subtitle_track(self, timeline):
        track = timeline.track_by_id(BASE_SUBTITLE_TRACK_ID)
        if track is not None and track.is_subtitle():
            return track
        tracks = sorted(timeline.subtitle_tracks(), key=lambda t: t.index)
        return tracks[0] if tracks else None

    # 差分 1 件を Timeline へ入れる
    def _insert_one(self, timeline, entry, cfg, video, audio, subtitle, anchors, meta):
        media = media_probe.probe(
            entry["normalized_path"], timeline.next_id("m"),
            self._clip_settings, cfg["media"])
        duration_hint = float(entry.get("normalized_duration") or 0.0)
        if duration_hint > 0:
            media.duration_sec = duration_hint

        segments = [(float(s), float(e)) for s, e in _segments_of(entry, media)
                    if float(e) - float(s) > _MIN_SEGMENT_SEC]
        duration = sum(e - s for s, e in segments)
        if duration <= _MIN_SEGMENT_SEC:
            _logger.warning("追加セクション clip%s は尺が短すぎるため入れません",
                            entry.get("index"))
            return False
        timeline.media_pool.append(media)

        at = self._insert_position(timeline, float(entry.get("end", 0.0)), anchors)
        self._make_room(timeline, at, duration)

        cursor = at
        for seg_index, (start, end) in enumerate(segments):
            clip_id = timeline.next_id("c")
            video.clips.append(Clip(
                clip_id, media.id, cursor, end - start,
                source_in=start, source_out=end,
                z_order=BASE_Z_ORDER,
                origin={"type": ORIGIN_SILENCE_CUT,
                        ORIGIN_ARCHIVE_INDEX: entry["index"],
                        "segment_index": seg_index},
            ))
            if media.has_audio and audio is not None:
                audio.clips.append(AudioClip(timeline.next_id("a"), clip_id))
            cursor += end - start

        # 字幕は構築時と同じ関数を通す (クリップ尺での打ち切り規約もそのまま効く)
        append_subtitles(timeline, subtitle, entry.get("items"), at, cursor - at,
                         segments, media.id, entry["index"])

        meta.append({
            "index": entry["index"],
            "vod_start": float(entry.get("start", 0.0)),
            "vod_end": float(entry.get("end", 0.0)),
            "score": float(entry.get("score", 0.0)),
            "timeline_start": at,
            "timeline_end": cursor,
            "media_id": media.id,
            "theme": str(entry.get("theme", "") or ""),
            "media_role": str(entry.get("media_role", "normalized") or "normalized"),
        })
        _logger.info(
            "セクション追加: clip%s を %.2fs へ挿入 (VOD %.1f-%.1f / 尺 %.2fs)",
            entry["index"], at, entry.get("start", 0.0), entry.get("end", 0.0), duration)
        return True

    # 元動画の時系列に合う Timeline 上の挿入位置を返す (resolve13 §3-2)
    #   ・自分より後ろから始まる最初の既存セクションの手前へ入れる
    #     (どのセクションよりも先頭なら、先頭セクションの位置 = Timeline の先頭)
    #   ・見つからなければ末尾へ足す
    # source.archive.clips の timeline_start は編集で古くなるため使わない (§2.10)。
    # 実位置は clip_range() で取り直す。
    def _insert_position(self, timeline, range_end, anchors):
        for entry in anchors:
            if float(entry.get("vod_start", 0.0)) >= range_end - _SECTION_EPS:
                span = clip_range(timeline, entry.get("index"))
                if span is not None:
                    return span[0]
        return timeline.duration_sec()

    # 途中へ入れる場合は、その位置から後ろを尺ぶん右へずらす (resolve13 §3-2)
    # 音声トラックとロック中トラックは make_room_for_range が自動で除外する。
    # 挿入位置は必ずセクション境界のため、既存クリップが分割されることはない。
    def _make_room(self, timeline, at, duration):
        if at >= timeline.duration_sec() - _SECTION_EPS:
            return
        for track in list(timeline.tracks):
            if track.is_audio():
                continue
            commands.make_room_for_range(
                timeline, track, at, at + duration,
                min_clip_sec=self._min_clip_sec, shift=duration)

    # 統合: 触れた既存セクションと追加した差分を代表番号へ揃え、メタを 1 件へ畳む
    def _merge_sections(self, timeline, meta):
        target = self._plan.get("target_index")
        if target is None:
            return
        merged = set(self._plan.get("merge_indexes") or [])
        merged.update(entry["index"] for entry in self._entries)
        merged.discard(target)
        if not merged:
            return

        # V1 / 字幕の origin を代表番号へ書き換える
        # (クリップは _snapshot が copy() しているため Undo で完全に戻る)
        for track in timeline.tracks:
            for clip in track.clips:
                origin = getattr(clip, "origin", None)
                if isinstance(origin, dict) and origin.get(ORIGIN_ARCHIVE_INDEX) in merged:
                    origin[ORIGIN_ARCHIVE_INDEX] = target

        span_start, span_end = self._plan["span"]
        kept = []
        for entry in meta:
            if entry.get("index") == target:
                entry["vod_start"] = span_start
                entry["vod_end"] = span_end
                kept.append(entry)
            elif entry.get("index") in merged:
                continue                 # 代表へ畳む
            else:
                kept.append(entry)
        meta[:] = kept
        _logger.info("セクション統合: clip%s へ %d 件を畳み込み (VOD %.1f-%.1f)",
                     target, len(merged), span_start, span_end)


# ==================================================================
# Resolve 出力用の逆変換 (§5.7)
# ==================================================================

# クリップ 1 件を resolve_export.export_archive_result の entry 形式へ戻す。
# sub_timeline は split_by_clip の戻り値 (0 起点)。
# 戻り値: {"index","start","end","keep_segments","items","theme","eff_cfg"}
def to_export_entry(clip_index, sub_timeline, prepared, theme=""):
    keep_segments = []
    base = sub_timeline.base_video_track()
    if base is not None:
        for clip in sorted(base.clips, key=lambda c: c.timeline_start):
            # D&D で足した素材は元 VOD の区間ではないため除外する
            if clip.origin_type() != ORIGIN_SILENCE_CUT or not clip.enabled:
                continue
            keep_segments.append((float(clip.source_in), float(clip.source_out)))

    items = []
    for track in sub_timeline.subtitle_tracks():
        for subtitle in sorted(track.clips, key=lambda c: c.timeline_start):
            # クリップ用の Resolve 出力と同じ変換を使う (SubtitleClip.to_item)。
            # 個別指定 (位置・色) もそのまま出力へ運ばれる (ver3 resolve6 §3-5)。
            items.append(subtitle.to_item())

    return {
        "index": clip_index,
        "start": float((prepared or {}).get("start", 0.0)),
        "end": float((prepared or {}).get("end", 0.0)),
        "keep_segments": keep_segments or None,
        "items": items,
        "theme": theme or "",
        "eff_cfg": (prepared or {}).get("eff_cfg"),
    }
