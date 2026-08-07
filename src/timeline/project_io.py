# プロジェクト JSON の読み書き・検証・マイグレーション
# (docs/request/ver3/resolve.md §6.2)
#
# 現時点では「実行内容の記録」と「外部連携」のための書き出しが主用途で、
# アプリが読み戻す経路は持たない (回答 Q6: 常に作り直し)。ただし将来の
# プロジェクト再編集機能へそのまま繋がるよう、読み込み・検証・版差吸収まで実装しておく。
import json
import os
from datetime import datetime

from ..exceptions import TimelineError
from ..utils.logger import get_logger
from .model import (
    BASE_AUDIO_TRACK_ID,
    MediaRef,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    Timeline,
    Track,
    round_sec,
)

_logger = get_logger(__name__)

# 本実装が書き出すスキーマ版 (§6.2.2)
SCHEMA_VERSION = 1

# 書き出し途中の一時ファイル拡張子 (部分ファイルを残さない / resolve_export と同方針)
_TEMP_SUFFIX = ".tmp"

# 最小クリップ尺の既定 (setting.json timeline.min_clip_sec が渡されなかった場合)
_DEFAULT_MIN_CLIP_SEC = 0.05

# 重複判定の許容誤差 (秒)。
# 時刻はミリ秒精度で書き出す (§6.2.3) ため、開始時刻と「直前クリップの終端」を
# それぞれ独立に丸めると最大 1ms 程度ずれる。この差を重複とみなすと、
# 正常に書き出したファイルを読み戻すだけで警告と微修正が発生してしまう。
_OVERLAP_TOLERANCE_SEC = 0.0015


# ------------------------------------------------------------------
# 出力先の解決 (§6.2.1)
# ------------------------------------------------------------------

# プロジェクト JSON の出力パスを組み立てる
# timeline.project_dir が空なら general.output_directory、それも空なら入力動画と同じ場所。
def default_project_path(settings, input_path, timeline_cfg=None):
    cfg = timeline_cfg if timeline_cfg is not None else (settings or {}).get("timeline", {})
    out_dir = cfg.get("project_dir", "") or \
        (settings or {}).get("general", {}).get("output_directory", "")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    else:
        out_dir = os.path.dirname(os.path.abspath(input_path))
    suffix = cfg.get("project_suffix", ".timeline.json") or ".timeline.json"
    stem = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(out_dir, f"{stem}{suffix}")


# ------------------------------------------------------------------
# 書き出し
# ------------------------------------------------------------------

# Timeline を JSON 文字列へ変換する (§6.2.2)
def to_json(timeline, generator="", created_at=None):
    now = datetime.now().isoformat(timespec="seconds")
    edit_points = dict(timeline.edit_points or {})
    # cut_segments は導出値。keep_segments から毎回作り直す (§6.2.3)
    keep = edit_points.get("keep_segments") or []
    edit_points["keep_segments"] = [[round_sec(s), round_sec(e)] for s, e in keep]
    edit_points["cut_segments"] = [
        [round_sec(s), round_sec(e)]
        for s, e in derive_cut_segments(keep, edit_points.get("source_duration_sec", 0.0))
    ]

    data = {
        "schema_version": SCHEMA_VERSION,
        "generator": generator,
        "created_at": created_at or now,
        "updated_at": now,
        "timeline": {
            "fps": timeline.fps,
            "width": timeline.width,
            "height": timeline.height,
            "orientation": timeline.orientation,
            "duration_sec": round_sec(timeline.duration_sec()),
            "playhead_sec": round_sec(timeline.playhead_sec),
            "zoom_px_per_sec": round(timeline.zoom_px_per_sec, 3),
        },
        "source": _source_to_dict(timeline.source),
        "edit_points": edit_points,
        "media_pool": [media.to_dict() for media in timeline.media_pool],
        "tracks": [track.to_dict() for track in timeline.tracks],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


# source セクションを丸めて整形する
def _source_to_dict(source):
    data = dict(source or {})
    if "duration_sec" in data and data["duration_sec"] is not None:
        data["duration_sec"] = round_sec(data["duration_sec"])
    return data


# 残す区間の補集合 (取り除く区間) を求める (§6.2.2 cut_segments)
def derive_cut_segments(keep_segments, total_duration):
    cuts = []
    cursor = 0.0
    for start, end in sorted(keep_segments or []):
        if float(start) > cursor + 1e-6:
            cuts.append((cursor, float(start)))
        cursor = max(cursor, float(end))
    total = float(total_duration or 0.0)
    if total > cursor + 1e-6:
        cuts.append((cursor, total))
    return cuts


# Timeline をプロジェクト JSON として書き出す (一時ファイル → 成功時 rename)
# 失敗しても実行は止めない方針のため、呼び出し側で握りつぶせるよう例外は投げるが
# pipeline 側では WARNING に留める (§10)。
def save(timeline, dest_path, generator=""):
    content = to_json(timeline, generator=generator)
    temp_path = dest_path + _TEMP_SUFFIX
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, dest_path)
    except OSError as e:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise TimelineError(f"プロジェクトファイルの書き出しに失敗しました: {e}") from e
    _logger.info("プロジェクト保存: %s", dest_path)
    return dest_path


# ------------------------------------------------------------------
# 読み込み (§6.2.4)
# ------------------------------------------------------------------

# JSON 文字列から Timeline を復元する
def from_json(text, min_clip_sec=_DEFAULT_MIN_CLIP_SEC):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise TimelineError(f"プロジェクトファイルの形式が不正です: {e}") from e
    return from_dict(data, min_clip_sec=min_clip_sec)


# dict から Timeline を復元する (検証・マイグレーション込み)
def from_dict(data, min_clip_sec=_DEFAULT_MIN_CLIP_SEC):
    if not isinstance(data, dict):
        raise TimelineError("プロジェクトファイルの形式が不正です (辞書ではありません)")

    version = data.get("schema_version")
    data = migrate(data, version)

    meta = data.get("timeline", {}) or {}
    timeline = Timeline(
        fps=meta.get("fps", 60),
        width=meta.get("width", 1920),
        height=meta.get("height", 1080),
        orientation=meta.get("orientation", "landscape"),
        source=data.get("source", {}),
        edit_points=data.get("edit_points", {}),
        media_pool=[MediaRef.from_dict(m) for m in data.get("media_pool", [])],
        tracks=[Track.from_dict(t) for t in data.get("tracks", [])],
        playhead_sec=meta.get("playhead_sec", 0.0),
        zoom_px_per_sec=meta.get("zoom_px_per_sec", 40.0),
    )
    validate(timeline, min_clip_sec=min_clip_sec)
    timeline.normalize()
    return timeline


# ファイルから Timeline を読み込む
def load(path, min_clip_sec=_DEFAULT_MIN_CLIP_SEC):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        raise TimelineError(f"プロジェクトファイルを読み込めません: {e}") from e
    return from_json(text, min_clip_sec=min_clip_sec)


# ------------------------------------------------------------------
# マイグレーション (§6.2.4)
# ------------------------------------------------------------------

# 版差を吸収する。未知の (より新しい) 版は明示して中止する。
def migrate(data, version):
    if version is None:
        raise TimelineError("プロジェクトファイルに schema_version がありません")
    try:
        version = int(version)
    except (TypeError, ValueError) as e:
        raise TimelineError(f"schema_version が不正です: {version}") from e

    if version > SCHEMA_VERSION:
        raise TimelineError(
            f"このバージョンでは開けないプロジェクトです "
            f"(ファイル: v{version} / 対応: v{SCHEMA_VERSION})。アプリを更新してください。"
        )

    # 旧版 → 現行版へ連鎖適用する (現時点では v1 のみのため何もしない)
    while version < SCHEMA_VERSION:
        migrator = _MIGRATIONS.get(version)
        if migrator is None:
            raise TimelineError(f"v{version} からの移行方法が定義されていません")
        data = migrator(data)
        version += 1
    return data


# 版ごとの移行関数 (v1 → v2 が必要になったらここへ追加する)
_MIGRATIONS = {}


# ------------------------------------------------------------------
# 検証 (§6.2.4)
# ------------------------------------------------------------------

# 復元した Timeline を検証し、救済できるものは補正して WARNING を出す。
# 救済できないものだけ TimelineError にする。
def validate(timeline, min_clip_sec=_DEFAULT_MIN_CLIP_SEC):
    _validate_media(timeline)
    _validate_keep_segments(timeline)
    _validate_clips(timeline, min_clip_sec)
    _validate_audio_links(timeline)
    _validate_overlaps(timeline)
    return timeline


# メディアの実在を確認する。欠落は該当クリップを無効化して継続する (§10)。
def _validate_media(timeline):
    missing = set()
    for media in timeline.media_pool:
        if not media.path or not os.path.exists(media.path):
            missing.add(media.id)
    if not missing:
        return
    _logger.warning(
        "メディアの実体が見つかりません (%d 件)。該当クリップを無効化します: %s",
        len(missing), ", ".join(sorted(missing)),
    )
    for track in timeline.video_tracks():
        for clip in track.clips:
            if clip.media_id in missing:
                clip.enabled = False


# 編集点が昇順・非重複・総尺内であることを確認する
def _validate_keep_segments(timeline):
    keep = timeline.edit_points.get("keep_segments") or []
    total = float(timeline.edit_points.get("source_duration_sec") or 0.0)
    cleaned = []
    cursor = 0.0
    for segment in keep:
        try:
            start, end = float(segment[0]), float(segment[1])
        except (TypeError, ValueError, IndexError):
            _logger.warning("編集点の形式が不正なため無視します: %r", segment)
            continue
        if end <= start:
            _logger.warning("編集点の区間が不正なため無視します: %.3f→%.3f", start, end)
            continue
        if start < cursor - 1e-6:
            _logger.warning("編集点が重複しているため補正します: %.3f → %.3f", start, cursor)
            start = cursor
            if end <= start:
                continue
        if total > 0 and end > total + 1e-6:
            _logger.warning("編集点が総尺を超えるため丸めます: %.3f → %.3f", end, total)
            end = total
            if end <= start:
                continue
        cleaned.append([round_sec(start), round_sec(end)])
        cursor = end
    timeline.edit_points["keep_segments"] = cleaned


# クリップの尺・ソース範囲を確認する
def _validate_clips(timeline, min_clip_sec):
    limit = float(min_clip_sec or _DEFAULT_MIN_CLIP_SEC)
    for track in timeline.tracks:
        if track.is_audio():
            continue
        survivors = []
        for clip in track.clips:
            if clip.duration < limit - 1e-6:
                _logger.warning(
                    "クリップ %s の尺 (%.3fs) が下限 (%.3fs) 未満のため除外します",
                    clip.id, clip.duration, limit)
                continue
            if track.is_video() and clip.source_out <= clip.source_in + 1e-6:
                _logger.warning(
                    "クリップ %s のソース範囲が不正なため除外します (in=%.3f out=%.3f)",
                    clip.id, clip.source_in, clip.source_out)
                continue
            survivors.append(clip)
        track.clips = survivors


# 音声クリップのリンク先が実在することを確認する (R18 / §6.3.4)
def _validate_audio_links(timeline):
    for track in timeline.audio_tracks():
        survivors = []
        for clip in track.clips:
            if timeline.clip_by_id(clip.link_clip) is None:
                _logger.warning(
                    "音声クリップ %s のリンク先 %s が存在しないため除外します "
                    "(該当区間は無音になります)", clip.id, clip.link_clip)
                continue
            survivors.append(clip)
        track.clips = survivors


# 同一トラック内でクリップが時間重複していないことを確認する (重複は後勝ちで詰める)
def _validate_overlaps(timeline):
    for track in timeline.tracks:
        if track.is_audio():
            continue  # 音声は映像からの導出のため重複は起こらない
        track.sort_clips(timeline)
        cursor = None
        for clip in track.clips:
            if cursor is not None and clip.timeline_start < cursor - _OVERLAP_TOLERANCE_SEC:
                _logger.warning(
                    "トラック %s でクリップ %s が重複しているため %.3f へ移動します",
                    track.id, clip.id, cursor)
                clip.timeline_start = cursor
            cursor = clip.timeline_end


# 音声トラックが無い Timeline へ既定の A1 を足す (読み込み時の保険)
def ensure_base_audio_track(timeline):
    if timeline.audio_tracks():
        return timeline
    base = timeline.base_video_track()
    if base is None:
        return timeline
    timeline.tracks.append(Track(
        BASE_AUDIO_TRACK_ID, TRACK_AUDIO, 1, name="Audio 1", link_track=base.id))
    return timeline


# 参照だけのために公開しておく (呼び出し側の import を 1 本に保つ)
__all__ = [
    "SCHEMA_VERSION",
    "TRACK_AUDIO",
    "TRACK_SUBTITLE",
    "TRACK_VIDEO",
    "default_project_path",
    "derive_cut_segments",
    "ensure_base_audio_track",
    "from_dict",
    "from_json",
    "load",
    "migrate",
    "save",
    "to_json",
    "validate",
]
