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
        _append_subtitles(timeline, subtitle_track, entry.get("items"),
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
def _append_subtitles(timeline, subtitle_track, items, offset, clip_duration,
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
