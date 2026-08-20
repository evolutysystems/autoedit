# プロジェクト JSON の読み書き・検証・マイグレーション
# (docs/request/ver3/resolve.md §6.2)
#
# 書き出しは「実行内容の記録」と「外部連携」に加え、編集画面からの保存にも使う。
# 読み戻しは load_project() が担い、pipeline_runner.run_from_project() から
# 「保存した編集の続き」として開き直せる (ver3 resolve7 §3-4)。
import json
import os
import shutil
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

# プロジェクトの種別 (ver3 resolve9 §3-4)
KIND_CLIP = "clip"
KIND_ARCHIVE = "archive"

# ファイル名に使えない文字 (Windows)。リネームの検査に使う (resolve9 §3-10)
_INVALID_NAME_CHARS = '\\/:*?"<>|'


# ------------------------------------------------------------------
# 出力先の解決 (§6.2.1)
# ------------------------------------------------------------------

# プロジェクト JSON の出力パスを組み立てる
# timeline.project_dir が空なら general.output_directory、それも空なら入力動画と同じ場所。
# name_suffix: 元動画名の後ろへ挟む識別子 (アーカイブ用の ".archive" など / resolve9 §3-5)。
#              同じ元動画からクリップ用とアーカイブ用を作っても名前が衝突しないようにする。
def default_project_path(settings, input_path, timeline_cfg=None, name_suffix=""):
    cfg = timeline_cfg if timeline_cfg is not None else (settings or {}).get("timeline", {})
    out_dir = cfg.get("project_dir", "") or \
        (settings or {}).get("general", {}).get("output_directory", "")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    else:
        out_dir = os.path.dirname(os.path.abspath(input_path))
    suffix = cfg.get("project_suffix", ".timeline.json") or ".timeline.json"
    stem = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(out_dir, f"{stem}{str(name_suffix or '')}{suffix}")


# プロジェクトの種別を返す ("archive" / "clip" / ver3 resolve9 §3-4)
# アーカイブ切り抜き用は source.archive を持つ (archive/timeline_builder.py が入れる)。
# 開く経路 (レンダラ・書き出し方) が根本的に違うため、取り違えないよう 1 か所で判定する。
# 受け取れるもの: プロジェクト JSON の dict / Timeline / source の dict
def project_kind(data_or_timeline):
    source = None
    if isinstance(data_or_timeline, dict):
        # プロジェクト JSON なら source を、source 自体ならそのまま見る
        source = data_or_timeline.get("source", data_or_timeline)
    else:
        source = getattr(data_or_timeline, "source", None)
    if isinstance(source, dict) and isinstance(source.get("archive"), dict):
        return KIND_ARCHIVE
    return KIND_CLIP


# 自動保存ファイルのパス (<プロジェクト>.autosave.json / ver3 resolve7 §5.9)
# 本体とは別ファイルにして、本体を上書きせずに編集途中を残す。
def autosave_path(project_path, suffix=".autosave.json"):
    if not project_path:
        return ""
    return os.path.splitext(project_path)[0] + (suffix or ".autosave.json")


# 素材の複製先フォルダ (<プロジェクト名>.media / ver3 resolve7 §3-5 案B)
# プロジェクトの接尾辞 (.timeline.json) は 2 段の拡張子のため、splitext では
# ".timeline" が残る。接尾辞に一致する場合はそれを丸ごと落とす。
def media_dir_path(project_path, project_suffix=".timeline.json",
                   dir_suffix=".media"):
    if not project_path:
        return ""
    name = os.path.basename(project_path)
    if project_suffix and name.endswith(project_suffix):
        stem = name[:-len(project_suffix)]
    else:
        stem = os.path.splitext(name)[0]
    return os.path.join(os.path.dirname(os.path.abspath(project_path)),
                        f"{stem}{dir_suffix or '.media'}")


# ------------------------------------------------------------------
# 書き出し
# ------------------------------------------------------------------

# Timeline を JSON 文字列へ変換する (§6.2.2)
# created_at   : 初回作成時刻 (上書き保存で引き継ぐ / resolve7 §5.7)
# project_path : 保存先。渡すと素材へ相対パス (path_rel) を併記する (resolve7 §5.9)
def to_json(timeline, generator="", created_at=None, project_path=None):
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
        "media_pool": [_media_to_dict(media, project_path)
                       for media in timeline.media_pool],
        "tracks": [track.to_dict() for track in timeline.tracks],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


# source セクションを丸めて整形する
# media_role は「本編素材が何だったか」の記録 (resolve7 §5.9):
#   "normalized" = ラウドネス正規化後の中間ファイル (一時ファイルなので後で消える)
#   "original"   = 元動画そのもの (正規化が無効・スキップされた場合)
# 開くときに「作り直すべき素材か」を推測ではなく記録から判断できるようにする。
# 旧ファイル (キー無し) は media_path != input_path かどうかで判定する。
def _source_to_dict(source):
    data = dict(source or {})
    if "duration_sec" in data and data["duration_sec"] is not None:
        data["duration_sec"] = round_sec(data["duration_sec"])
    if data.get("media_path") and data.get("input_path") and "media_role" not in data:
        data["media_role"] = ("original"
                              if os.path.abspath(data["media_path"])
                              == os.path.abspath(data["input_path"])
                              else "normalized")
    return data


# メディア 1 件を辞書へ (プロジェクトからの相対パスを併記する / resolve7 §5.9)
# プロジェクトと素材をまとめて別フォルダ・別 PC へ移した場合に効く。
# 相対パスを作れない場合 (別ドライブ・パス未設定) は併記しない。
def _media_to_dict(media, project_path=None):
    data = media.to_dict()
    if not project_path or not media.path:
        return data
    try:
        data["path_rel"] = os.path.relpath(
            os.path.abspath(media.path), os.path.dirname(os.path.abspath(project_path)))
    except ValueError:
        pass
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
def save(timeline, dest_path, generator="", created_at=None, project_path=None):
    content = to_json(timeline, generator=generator, created_at=created_at,
                      project_path=project_path or dest_path)
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
def from_json(text, min_clip_sec=_DEFAULT_MIN_CLIP_SEC, validate_timeline=True):
    return from_dict(_parse(text), min_clip_sec=min_clip_sec,
                     validate_timeline=validate_timeline)


# JSON 文字列を辞書へ (形式不正はここで TimelineError にする)
def _parse(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise TimelineError(f"プロジェクトファイルの形式が不正です: {e}") from e


# dict から Timeline を復元する (検証・マイグレーション込み)
# validate_timeline=False にすると検証を行わない。素材の復旧 (media_recovery) を
# 先に走らせたい場合に使う。検証は「素材が無いクリップを無効化」してしまうため、
# 復旧より先に走らせてはならない (resolve7 §5.8)。既定は True で従来どおり。
def from_dict(data, min_clip_sec=_DEFAULT_MIN_CLIP_SEC, validate_timeline=True):
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
    if validate_timeline:
        validate(timeline, min_clip_sec=min_clip_sec)
    timeline.normalize()
    return timeline


# ファイルから Timeline を読み込む
def load(path, min_clip_sec=_DEFAULT_MIN_CLIP_SEC, validate_timeline=True):
    return from_json(_read(path), min_clip_sec=min_clip_sec,
                     validate_timeline=validate_timeline)


# ファイルから Timeline と付随情報を読み込む (resolve7 §5.9)
# 戻り値: (timeline, meta)
#   meta = {"created_at","updated_at","generator","schema_version","project_path"}
# created_at は上書き保存で引き継ぐために要る (保存のたびに作成時刻が変わらないように)。
def load_project(path, min_clip_sec=_DEFAULT_MIN_CLIP_SEC, validate_timeline=True):
    data = _parse(_read(path))
    if not isinstance(data, dict):
        raise TimelineError("プロジェクトファイルの形式が不正です (辞書ではありません)")
    timeline = from_dict(data, min_clip_sec=min_clip_sec,
                         validate_timeline=validate_timeline)
    meta = {
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "generator": data.get("generator", ""),
        "schema_version": data.get("schema_version"),
        "project_path": os.path.abspath(path),
    }
    _logger.info("プロジェクト読込: %s", path)
    return timeline, meta


# ファイルを読む (BOM 付きも読めるようにする)
def _read(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except OSError as e:
        raise TimelineError(f"プロジェクトファイルを読み込めません: {e}") from e


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


# ==================================================================
# プロジェクト一覧 (ver3 resolve9 §5.13)
# 走査・概要取得・リネーム・削除。GUI から切り離してテストできるよう、
# ここには画面に依存しない処理だけを置く。
# ==================================================================

# 一覧に出す候補を集める (§3-8)
# 履歴 (kind に応じて recent / recent_archive) + project_dir (無ければ
# general.output_directory) + library.extra_dirs を走査し、project_suffix で
# 終わるファイルの絶対パスを重複なく返す。自動保存ファイルは除外する。
# kind を渡すと「ファイル名による一次判定」で絞り込む (確定は read_summary / 回答 Q3)。
# 戻り値: {"paths": [絶対パス…(更新日時の新しい順)], "truncated": 打ち切った件数}
def scan_projects(settings, kind=None, timeline_cfg=None):
    cfg = timeline_cfg if timeline_cfg is not None else (settings or {}).get("timeline", {})
    project_cfg = cfg.get("project", {}) or {}
    library_cfg = project_cfg.get("library", {}) or {}
    suffix = cfg.get("project_suffix", ".timeline.json") or ".timeline.json"
    autosave_suffix = project_cfg.get("autosave_suffix", ".autosave.json") \
        or ".autosave.json"
    archive_suffix = project_cfg.get("archive_suffix", ".archive") or ".archive"

    found = {}          # 絶対パス(小文字) -> 絶対パス

    def add(path):
        if not path:
            return
        full = os.path.abspath(str(path))
        if not full.lower().endswith(suffix.lower()):
            return
        if full.lower().endswith(autosave_suffix.lower()):
            return      # 自動保存は一覧に出さない (本体の行へ印として出す)
        if not os.path.isfile(full):
            return
        found.setdefault(full.lower(), full)

    # ① 履歴 (明示保存したもの)
    for key in ("recent", "recent_archive"):
        for path in (project_cfg.get(key) or []):
            add(path)

    # ② 保存先フォルダ + ③ 追加フォルダ
    directories = []
    base_dir = cfg.get("project_dir", "") or \
        (settings or {}).get("general", {}).get("output_directory", "")
    if base_dir:
        directories.append(base_dir)
    directories.extend(library_cfg.get("extra_dirs") or [])
    recursive = bool(library_cfg.get("scan_recursive", False))
    for directory in directories:
        for path in _scan_dir(directory, suffix, recursive):
            add(path)

    paths = list(found.values())
    if kind:
        # 一次判定はファイル名。規約から外れた名前のものは read_summary が拾い直す。
        paths = [p for p in paths if _name_kind(p, suffix, archive_suffix) == kind]

    # 更新日時の新しい順。stat に失敗したものは末尾へ回す
    paths.sort(key=lambda p: _mtime(p), reverse=True)
    limit = int(library_cfg.get("max_items", 100) or 100)
    truncated = max(len(paths) - limit, 0)
    if truncated:
        _logger.info("プロジェクトが %d 件見つかりました (新しい %d 件を表示します)",
                     len(paths), limit)
        paths = paths[:limit]
    return {"paths": paths, "truncated": truncated}


# 1 フォルダぶんの走査 (再帰は library.scan_recursive のときだけ)
def _scan_dir(directory, suffix, recursive):
    results = []
    if not directory or not os.path.isdir(directory):
        return results
    try:
        if recursive:
            for root, _dirs, files in os.walk(directory):
                results.extend(os.path.join(root, name) for name in files
                               if name.lower().endswith(suffix.lower()))
        else:
            with os.scandir(directory) as entries:
                results.extend(entry.path for entry in entries
                               if entry.is_file()
                               and entry.name.lower().endswith(suffix.lower()))
    except OSError:
        _logger.warning("プロジェクトの走査に失敗しました (飛ばします): %s", directory)
    return results


# ファイル名からの種別の一次判定 (確定は read_summary の kind)
def _name_kind(path, suffix, archive_suffix):
    name = os.path.basename(path)
    if suffix and name.lower().endswith(suffix.lower()):
        stem = name[:-len(suffix)]
    else:
        stem = os.path.splitext(name)[0]
    return KIND_ARCHIVE if stem.lower().endswith(archive_suffix.lower()) else KIND_CLIP


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# プロジェクトの表示名 (接尾辞を外したファイル名 / 一覧とリネームで使う)
def display_name(path, timeline_cfg=None):
    suffix = (timeline_cfg or {}).get("project_suffix", ".timeline.json") \
        or ".timeline.json"
    name = os.path.basename(str(path or ""))
    if suffix and name.lower().endswith(suffix.lower()):
        return name[:-len(suffix)]
    return os.path.splitext(name)[0]


# プロジェクト 1 件の概要を読む (一覧の後追い表示用 / §5.13)
# 戻り値: {"path","name","kind","created_at","updated_at","generator","clip_count",
#          "duration_sec","input_path","frames":[{"path","sec"}…],
#          "size","has_autosave","has_media_dir","broken"}
# frames は V1 の有効クリップを等間隔に割った位置の (実在する動画のパス, その動画内の秒)。
# 素材が消えていれば元動画へ読み替える (§2.7)。
def read_summary(path, settings=None, timeline_cfg=None, frame_count=3):
    cfg = timeline_cfg if timeline_cfg is not None else (settings or {}).get("timeline", {})
    project_cfg = cfg.get("project", {}) or {}
    full = os.path.abspath(str(path))
    summary = {
        "path": full,
        "name": display_name(full, cfg),
        "kind": KIND_CLIP,
        "created_at": "", "updated_at": "", "generator": "",
        "clip_count": 0, "duration_sec": 0.0, "input_path": "",
        "frames": [], "size": _size(full),
        "has_autosave": os.path.exists(autosave_path(
            full, project_cfg.get("autosave_suffix", ".autosave.json"))),
        "has_media_dir": os.path.isdir(media_dir_path(
            full, cfg.get("project_suffix", ".timeline.json"),
            project_cfg.get("media_dir_suffix", ".media"))),
        "broken": False,
    }
    try:
        data = _parse(_read(full))
        timeline = from_dict(data, validate_timeline=False)
    except Exception:  # noqa: BLE001 (壊れた 1 件で一覧全体を止めない)
        _logger.warning("プロジェクトの概要を読めませんでした: %s", full)
        summary["broken"] = True
        return summary

    summary["created_at"] = str(data.get("created_at", "") or "")
    summary["updated_at"] = str(data.get("updated_at", "") or "")
    summary["generator"] = str(data.get("generator", "") or "")
    summary["kind"] = project_kind(data)
    source = timeline.source or {}
    summary["input_path"] = str(source.get("input_path", "") or "")
    clips = [c for c in timeline.base_clips()]
    summary["clip_count"] = len(clips)
    summary["duration_sec"] = float(timeline.duration_sec() or 0.0)
    summary["frames"] = plan_frames(timeline, frame_count)
    return summary


# サムネイルに使うフレーム位置を決める (§5.13)
# V1 の有効クリップを時系列に並べ、合計尺を count 等分した中央を取る。
# 素材が消えている場合は元動画へ読み替える (時間軸が一致するため / §2.7)。
def plan_frames(timeline, count=3):
    count = max(int(count or 1), 1)
    clips = sorted([c for c in timeline.base_clips()],
                   key=lambda c: c.timeline_start)
    if not clips:
        return []
    spans = []
    total = 0.0
    for clip in clips:
        duration = float(clip.duration or 0.0)
        if duration <= 0:
            continue
        spans.append((total, total + duration, clip))
        total += duration
    if total <= 0:
        return []

    source = timeline.source or {}
    frames = []
    for index in range(count):
        position = (index + 0.5) / count * total
        span = next((s for s in spans if s[0] <= position < s[1]), spans[-1])
        start, _end, clip = span
        source_sec = float(clip.source_in or 0.0) + (position - start)
        resolved = _resolve_frame_media(timeline, source, clip, source_sec)
        if resolved is not None:
            frames.append(resolved)
    return frames


# フレームを取り出す実ファイルと、その中での秒を決める
def _resolve_frame_media(timeline, source, clip, source_sec):
    media = timeline.media_by_id(clip.media_id)
    if media is not None and media.path and os.path.exists(media.path):
        return {"path": media.path, "sec": max(source_sec, 0.0)}

    # 素材が消えている → 元動画へ読み替える
    archive = source.get("archive") if isinstance(source, dict) else None
    if isinstance(archive, dict):
        vod = str(archive.get("vod_path", "") or source.get("input_path", "") or "")
        if vod and os.path.exists(vod):
            offset = 0.0
            clip_index = clip.origin.get("archive_clip_index")
            for entry in (archive.get("clips") or []):
                if entry.get("index") == clip_index:
                    offset = float(entry.get("vod_start", 0.0) or 0.0)
                    break
            return {"path": vod, "sec": max(source_sec + offset, 0.0)}
        return None

    input_path = str((source or {}).get("input_path", "") or "")
    if input_path and os.path.exists(input_path):
        # ラウドネス正規化は映像を -c:v copy で通すため元動画と時間軸が一致する
        return {"path": input_path, "sec": max(source_sec, 0.0)}
    return None


# プロジェクトに付随するファイルを集める (削除・リネームの対象 / §3-6 / §3-10)
# 戻り値: {"project","autosave","media_dir"} (存在しないものは空文字)
def related_paths(path, timeline_cfg=None, include_media_dir=True):
    cfg = timeline_cfg or {}
    project_cfg = cfg.get("project", {}) or {}
    full = os.path.abspath(str(path))
    autosave = autosave_path(full, project_cfg.get("autosave_suffix", ".autosave.json"))
    media_dir = media_dir_path(
        full, cfg.get("project_suffix", ".timeline.json"),
        project_cfg.get("media_dir_suffix", ".media"))
    return {
        "project": full if os.path.exists(full) else "",
        "autosave": autosave if autosave and os.path.exists(autosave) else "",
        "media_dir": media_dir if (include_media_dir and media_dir
                                   and os.path.isdir(media_dir)) else "",
    }


# プロジェクトの改名 (本体・自動保存・複製素材・JSON 内のパス / §3-10 / §5.14)
# new_stem は接尾辞を含まない表示名。戻り値: 新しいプロジェクトの絶対パス。
# 途中で失敗した場合はそこまでの改名を巻き戻してから例外を送出する。
def rename_project(path, new_stem, settings=None, timeline_cfg=None):
    cfg = timeline_cfg if timeline_cfg is not None else (settings or {}).get("timeline", {})
    project_cfg = cfg.get("project", {}) or {}
    suffix = cfg.get("project_suffix", ".timeline.json") or ".timeline.json"
    stem = str(new_stem or "").strip()

    if not stem:
        raise TimelineError("新しい名前を入力してください")
    if any(ch in stem for ch in _INVALID_NAME_CHARS):
        raise TimelineError(f"名前に次の文字は使えません: {_INVALID_NAME_CHARS}")
    old_path = os.path.abspath(str(path))
    if not os.path.exists(old_path):
        raise TimelineError(f"プロジェクトファイルが見つかりません: {old_path}")
    folder = os.path.dirname(old_path)
    new_path = os.path.join(folder, f"{stem}{suffix}")
    if os.path.normcase(new_path) == os.path.normcase(old_path):
        return old_path
    if os.path.exists(new_path):
        raise TimelineError(f"同じ名前のプロジェクトが既にあります: {os.path.basename(new_path)}")

    autosave_suffix = project_cfg.get("autosave_suffix", ".autosave.json")
    media_suffix = project_cfg.get("media_dir_suffix", ".media")
    old_autosave = autosave_path(old_path, autosave_suffix)
    new_autosave = autosave_path(new_path, autosave_suffix)
    old_media = media_dir_path(old_path, suffix, media_suffix)
    new_media = media_dir_path(new_path, suffix, media_suffix)

    done = []           # 巻き戻し用 (新, 旧)
    try:
        os.replace(old_path, new_path)
        done.append((new_path, old_path))
        if old_autosave and os.path.exists(old_autosave):
            os.replace(old_autosave, new_autosave)
            done.append((new_autosave, old_autosave))
        if old_media and os.path.isdir(old_media):
            if os.path.exists(new_media):
                raise TimelineError(
                    f"同じ名前の素材フォルダが既にあります: {os.path.basename(new_media)}")
            os.rename(old_media, new_media)
            done.append((new_media, old_media))
            # 素材フォルダを動かしたら JSON 内の参照も張り替える (これを忘れると開けなくなる)
            _rewrite_media_paths(new_path, old_media, new_media)
    except Exception:
        for current, original in reversed(done):
            try:
                os.replace(current, original)
            except OSError:
                _logger.exception("改名の巻き戻しに失敗しました: %s", current)
        raise

    _logger.info("プロジェクトを改名しました: %s → %s",
                 os.path.basename(old_path), os.path.basename(new_path))
    return new_path


# JSON 内の素材パスを新しい素材フォルダへ張り替える (§5.14 ③)
# 未知のキーを落とさないよう、生の辞書のまま読み書きする。
def _rewrite_media_paths(project_path, old_dir, new_dir):
    try:
        data = _parse(_read(project_path))
    except (TimelineError, OSError):
        _logger.warning("改名後の素材パスを張り替えられませんでした: %s", project_path)
        return
    if not isinstance(data, dict):
        return
    old_prefix = os.path.normcase(os.path.abspath(old_dir))
    new_abs = os.path.abspath(new_dir)
    folder = os.path.dirname(os.path.abspath(project_path))
    changed = False

    def swap(value):
        if not value:
            return None
        absolute = os.path.abspath(str(value))
        if os.path.normcase(absolute).startswith(old_prefix):
            return os.path.join(new_abs, os.path.relpath(absolute, old_dir))
        return None

    for media in (data.get("media_pool") or []):
        if not isinstance(media, dict):
            continue
        swapped = swap(media.get("path"))
        if swapped:
            media["path"] = swapped
            try:
                media["path_rel"] = os.path.relpath(swapped, folder)
            except ValueError:
                media.pop("path_rel", None)
            changed = True
    source = data.get("source")
    if isinstance(source, dict):
        swapped = swap(source.get("media_path"))
        if swapped:
            source["media_path"] = swapped
            changed = True
    if not changed:
        return
    try:
        with open(project_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        _logger.exception("改名後のプロジェクト保存に失敗しました: %s", project_path)


# プロジェクトの削除 (本体・自動保存・複製素材 / §5.10)
# use_trash=True ならゴミ箱へ送る (送れなかったものは failed へ入れて残す)。
# 戻り値: {"deleted": [パス…], "failed": [{"path","reason"}…], "trashed": bool}
def delete_project(path, timeline_cfg=None, delete_media_dir=True, use_trash=True):
    related = related_paths(path, timeline_cfg, include_media_dir=delete_media_dir)
    # 付随物 → 本体 の順。逆にすると失敗時にどのプロジェクトの残骸か分からなくなる
    targets = [related["media_dir"], related["autosave"], related["project"]]
    targets = [t for t in targets if t]
    result = {"deleted": [], "failed": [], "trashed": False}
    if not targets:
        return result

    if use_trash:
        from ..utils import trash
        if trash.is_available():
            outcome = trash.send_to_trash(targets)
            result["deleted"] = outcome["trashed"]
            result["failed"] = outcome["failed"]
            result["trashed"] = bool(outcome["trashed"])
            return result
        result["failed"] = [{"path": t, "reason": "ゴミ箱を利用できません"} for t in targets]
        return result

    for target in targets:
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            result["deleted"].append(target)
        except OSError as e:
            result["failed"].append({"path": target, "reason": str(e)})
    return result


# 履歴 (recent / recent_archive) からパスを取り除く。戻り値: 変更があったか
def forget_recent(settings, path, new_path=None):
    project_cfg = (settings or {}).setdefault("timeline", {}).setdefault("project", {})
    target = os.path.normcase(os.path.abspath(str(path)))
    changed = False
    for key in ("recent", "recent_archive"):
        entries = [str(p) for p in (project_cfg.get(key) or []) if p]
        updated = []
        for entry in entries:
            if os.path.normcase(os.path.abspath(entry)) == target:
                changed = True
                if new_path:
                    updated.append(os.path.abspath(new_path))
                continue
            updated.append(entry)
        if changed:
            project_cfg[key] = updated
    return changed


# 参照だけのために公開しておく (呼び出し側の import を 1 本に保つ)
__all__ = [
    "KIND_ARCHIVE",
    "KIND_CLIP",
    "SCHEMA_VERSION",
    "TRACK_AUDIO",
    "TRACK_SUBTITLE",
    "TRACK_VIDEO",
    "autosave_path",
    "default_project_path",
    "delete_project",
    "derive_cut_segments",
    "display_name",
    "ensure_base_audio_track",
    "forget_recent",
    "from_dict",
    "from_json",
    "load",
    "load_project",
    "media_dir_path",
    "migrate",
    "plan_frames",
    "project_kind",
    "read_summary",
    "related_paths",
    "rename_project",
    "save",
    "scan_projects",
    "to_json",
    "validate",
]
