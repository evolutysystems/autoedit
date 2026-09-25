# 追従結果キャッシュの読み書き (ver5 resolve8 §3.5 / §5.6)
#
# 追従結果は**重い**ため timeline.source へは置かない (§4-2)。
# Undo のたびに deepcopy されて画面が固まるため、別ファイルへ持つ。
# プロジェクトに残るのはキーフレームだけで、キャッシュを消しても追い直せば元に戻る。
#
# 置き場はプロジェクトファイルの隣 (<プロジェクト名>.blur.json)。
# プロジェクト未保存なら working_dir (実行終了で消える)。
#
# 整合性は 2 段で確かめる。
#   1. settings_key (キャンバス・追う間隔・検出モデル) が違う … 全部捨てて作り直す
#   2. media[id].key (素材の由来) が違う                    … その素材の追従結果だけ捨てる
# さらに区切りごとの指紋 (hash) で「キーフレームを動かした所だけ」追い直す (§3.5)。
import hashlib
import json
import os

from ..utils.logger import get_logger
from ..version import __version__

_logger = get_logger(__name__)

# キャッシュの書式版。増やしたら古いキャッシュは読まずに作り直す。
#   2: 人物のまとめ方の修正・輪郭 (sil)・追加した枠の形 (ver5 resolve3)
#   3: 人物 ID の無い枠も保存・素材ごとの由来キー (ver5 resolve4)
#   4: 人物へまとめる処理を廃止・部分解析 (ver5 resolve7)
#   5: 人物の自動検出を廃止。囲みごと・区切りごとの追従結果だけを持つ (ver5 resolve8)
SCHEMA_VERSION = 5

# プロジェクトファイルの隣へ置くときの拡張子
CACHE_SUFFIX = ".blur.json"


# プロジェクトのパスから追従結果の置き場を決める。
# プロジェクトが未保存なら working_dir の中へ置く (実行終了で消えてよい)。
def cache_path_for(project_path, working_dir):
    if project_path:
        base = os.path.splitext(project_path)[0]
        return base + CACHE_SUFFIX
    return os.path.join(working_dir or "", "timeline" + CACHE_SUFFIX)


# 素材の「由来」を表す文字列を返す (ver5 resolve4 §3.6.2 / §5.12.1)
#
# **一時ファイルのパスと更新時刻は使わない。** 中間ファイル (正規化後の素材・
# アーカイブの切り出し) は実行のたびに別の一時フォルダへ作り直されるため、
# それを鍵にすると開き直すたびに指紋が変わり、追従結果を必ず捨ててしまう。
#
# 代わりに「どの元動画の、どの範囲から、どう作られた素材か」を鍵にする。
#   アーカイブ : 元 VOD の版 + vod_start / vod_end + media_role
#   クリップ   : 元動画の版 + media_role
#   その他     : media.path の版 (利用者のファイルは消えないため従来どおりでよい)
# いずれにも「幅 x 高さ x 尺」を混ぜる = 復元がずれていたら別物として追い直す。
def media_key(timeline, media):
    if media is None:
        return "none"
    source = timeline.source if isinstance(getattr(timeline, "source", None), dict) else {}
    role = str(source.get("media_role", "") or "")
    parts = []

    entry = _archive_clip_of(source, getattr(media, "id", ""))
    if entry is not None:
        parts.append(f"vod={_file_stamp(_archive_vod_path(source))}")
        parts.append(f"span={float(entry.get('vod_start', 0.0)):.3f}"
                     f"-{float(entry.get('vod_end', 0.0)):.3f}")
        parts.append(f"role={entry.get('media_role', 'normalized')}")
    elif _is_derived_media(source, media):
        parts.append(f"input={_file_stamp(source.get('input_path'))}")
        parts.append(f"role={role or 'normalized'}")
    else:
        # 利用者のファイルをそのまま使っている素材 (OP/ED・画像・外部素材)
        parts.append(f"file={_file_stamp(getattr(media, 'path', ''))}")

    parts.append(f"size={int(getattr(media, 'width', 0) or 0)}"
                 f"x{int(getattr(media, 'height', 0) or 0)}")
    parts.append(f"dur={float(getattr(media, 'duration_sec', 0.0) or 0.0):.2f}")
    return "|".join(parts)


# 追従結果が「同じ条件で作られたか」を表す鍵 (ver5 resolve8 §5.6)
#   書式版 + キャンバスの寸法 + 追う間隔 + 検出モデルの版
#
# **キャンバスの寸法を入れる。**座標は「キャンバスへレターボックスで載せた後の
# 正規化座標」で持つため、縦横が変われば同じ数値が別の場所を指す。
#
# **素材の由来は入れない。**素材ごとの妥当性は media[id].key で個別に見るため
# (「素材 A を差し替えたが素材 B はそのまま」で B の結果を捨てずに済む)。
def settings_key(cfg, timeline=None):
    parts = [
        f"schema={SCHEMA_VERSION}",
        f"canvas={int(getattr(timeline, 'width', 0) or 0)}"
        f"x{int(getattr(timeline, 'height', 0) or 0)}",
        f"sample_fps={float(cfg['track']['sample_fps']):.3f}",
        f"detector={cfg['model']['detector']}",
        f"detector_input={cfg['model']['detector_input']}",
        # モデルファイルの更新時刻も入れる = 差し替えたら追い直す
        f"models={_model_stamp(cfg)}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# キャンバスの大きさ (控えとして残す)
def canvas_of(timeline):
    return {"width": int(timeline.width), "height": int(timeline.height)}


# source.archive.clips から、その素材を作った切り出しの情報を返す (無ければ None)
def _archive_clip_of(source, media_id):
    archive = source.get("archive") if isinstance(source, dict) else None
    if not isinstance(archive, dict) or not media_id:
        return None
    for entry in (archive.get("clips") or []):
        if isinstance(entry, dict) and str(entry.get("media_id") or "") == str(media_id):
            return entry
    return None


# アーカイブの元 VOD のパス (archive.vod_path → source.input_path の順)
def _archive_vod_path(source):
    archive = source.get("archive") if isinstance(source, dict) else None
    if isinstance(archive, dict) and archive.get("vod_path"):
        return str(archive["vod_path"])
    return str((source or {}).get("input_path") or "")


# その素材が「元動画から作られた中間ファイル」か (= 実行のたびに作り直される素材か)。
# source.media_id があればそれで判断し、無ければ media_path との一致で判断する。
def _is_derived_media(source, media):
    if not isinstance(source, dict) or not source.get("input_path"):
        return False
    declared = str(source.get("media_id") or "")
    if declared:
        return str(getattr(media, "id", "")) == declared
    media_path = str(source.get("media_path") or "")
    path = str(getattr(media, "path", "") or "")
    if media_path and path:
        return os.path.normcase(os.path.abspath(media_path)) == os.path.normcase(
            os.path.abspath(path))
    return False


# ファイルの版 (サイズと更新時刻)。見つからなければ "?" (指紋は作れる)。
# **パスそのものは混ぜない。**同じ中身のファイルを別の場所へ置いても同じ指紋にする。
def _file_stamp(path):
    if not path:
        return "-"
    try:
        stat = os.stat(str(path))
    except OSError:
        return "?"
    return f"{stat.st_size}:{int(stat.st_mtime)}"


# 同梱モデルの版 (ファイルサイズと更新時刻)
def _model_stamp(cfg):
    from . import models              # noqa: PLC0415 (循環 import を避けるため関数内で読む)

    path = models.resolve_path(cfg["model"]["detector"])
    if path is None:
        return "-"
    try:
        stat = os.stat(path)
    except OSError:
        return "?"
    return f"{stat.st_size}:{int(stat.st_mtime)}"


# ------------------------------------------------------------------
# 読み書き
# ------------------------------------------------------------------

# 追従結果を書き出す。失敗しても例外にしない (キャッシュは無くても動く)。
def save(path, tracks):
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(tracks, handle, ensure_ascii=False)
    except (OSError, TypeError, ValueError) as error:
        _logger.warning("ぼかしの追従結果を保存できませんでした: %s (%s)", path, error)
        return False
    _logger.info("ぼかしの追従結果を保存しました: %s", os.path.basename(path))
    return True


# 追従結果をそのまま読む (版も指紋も見ない)。
# 「なぜ追い直すのか」を説明するために古い結果でも中身を見たい場合に使う。
def load_any(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# 追従結果を読む。無い・壊れている・条件が違うなら None を返す。
#   expected_key : settings_key(cfg, timeline) の値。違えば捨てる
def load(path, expected_key=None):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        _logger.warning("ぼかしの追従結果が読めないため作り直します: %s (%s)", path, error)
        return None

    if not isinstance(data, dict):
        _logger.warning("ぼかしの追従結果の形式が不正なため作り直します: %s", path)
        return None
    if int(data.get("schema", 0)) != SCHEMA_VERSION:
        _logger.info("ぼかしの追従結果の版が違うため作り直します: %s", path)
        return None
    if expected_key and data.get("settings_key") != expected_key:
        _logger.info("追従の設定が変わったため、ぼかしの追従結果を作り直します")
        return None
    if not isinstance(data.get("tracks"), dict):
        data["tracks"] = {}
    return data


# 追従結果の入れ物を作る (tracker が埋める)
#   media_ids を渡すと素材ごとの由来キーも残す = その素材だけ作り直せる
def new_tracks(timeline, cfg, media_ids=None):
    return {
        "schema": SCHEMA_VERSION,
        "generator": f"Stretheus {__version__}",
        "settings_key": settings_key(cfg, timeline),
        "canvas": canvas_of(timeline),
        "sample_fps": float(cfg["track"]["sample_fps"]),
        "media": media_snapshot(timeline, media_ids),
        "tracks": {},
    }


# 追った素材の控え {media_id: {"key","width","height","duration"}}
def media_snapshot(timeline, media_ids=None):
    snapshot = {}
    for media_id in sorted({str(i) for i in (media_ids or []) if str(i or "")}):
        media = timeline.media_by_id(media_id)
        if media is None:
            continue
        snapshot[media_id] = {
            "key": media_key(timeline, media),
            "width": int(getattr(media, "width", 0) or 0),
            "height": int(getattr(media, "height", 0) or 0),
            "duration": round(float(getattr(media, "duration_sec", 0.0) or 0.0), 2),
        }
    return snapshot


# 指紋が合わなかったとき、「どの素材の何が変わったか」を人が読める文にする。
# 理由が分かれば利用者が原因を切り分けられる。
def describe_mismatch(tracks, timeline, media_ids=None):
    before = (tracks or {}).get("media") or {}
    if not before:
        return "前回の追従結果に素材の控えが無いため、追従をやり直します"

    after = media_snapshot(timeline, media_ids or list(before.keys()))
    reasons = []
    for media_id, old in sorted(before.items()):
        new = after.get(media_id)
        if new is None:
            reasons.append(f"{media_id}: 素材が見つかりません")
            continue
        if new.get("key") == old.get("key"):
            continue
        if (int(new.get("width", 0)) != int(old.get("width", 0))
                or int(new.get("height", 0)) != int(old.get("height", 0))):
            reasons.append(
                f"{media_id}: 解像度が変わりました "
                f"({old.get('width')}x{old.get('height')} → {new.get('width')}x{new.get('height')})")
        elif abs(float(new.get("duration", 0.0)) - float(old.get("duration", 0.0))) > 0.05:
            reasons.append(
                f"{media_id}: 尺が変わりました "
                f"({old.get('duration')} 秒 → {new.get('duration')} 秒)")
        else:
            reasons.append(f"{media_id}: 元動画か切り出し範囲が変わりました")
    for media_id in sorted(set(after) - set(before)):
        reasons.append(f"{media_id}: 追っていない素材が増えました")

    if not reasons:
        return "設定 (追う間隔・検出モデル・キャンバスの寸法) が変わったため、追従をやり直します"
    return "素材が変わったため、追従をやり直します: " + " / ".join(reasons)


# 由来キーがいまの Timeline と一致する素材 ID の集合。
# 一致しない素材の追従結果は当てにならないので、呼び出し側が捨てる。
def valid_media(tracks, timeline, media_ids=None):
    before = (tracks or {}).get("media") or {}
    after = media_snapshot(timeline, media_ids or list(before.keys()))
    return {media_id for media_id, entry in before.items()
            if after.get(media_id, {}).get("key") == entry.get("key")}


# 由来の変わった素材の追従結果を捨てる。戻り値: 捨てた素材 ID の一覧
def drop_stale_media(tracks, timeline):
    if not tracks:
        return []
    valid = valid_media(tracks, timeline)
    stale = [media_id for media_id in ((tracks.get("media") or {}).keys())
             if media_id not in valid]
    if not stale:
        return []
    dropped = set(stale)
    tracks["tracks"] = {spec_id: entry for spec_id, entry in (tracks.get("tracks") or {}).items()
                        if str(entry.get("media_id") or "") not in dropped}
    tracks["media"] = {k: v for k, v in (tracks.get("media") or {}).items() if k not in dropped}
    _logger.info("ぼかし: 素材が変わったため %s の追従結果を捨てました", " / ".join(sorted(dropped)))
    return sorted(dropped)


# ------------------------------------------------------------------
# 区切り (resolve8 §3.5)
# ------------------------------------------------------------------

# その指定・その指紋の区切りを返す (無ければ None)
def segment_of(tracks, spec_id, segment_hash):
    entry = (tracks or {}).get("tracks", {}).get(str(spec_id))
    if not isinstance(entry, dict):
        return None
    for segment in entry.get("segments") or []:
        if str(segment.get("hash") or "") == str(segment_hash):
            return segment
    return None


# 追い直しが必要な区切り (指紋の合うものが無いもの) を返す
def missing_segments(tracks, wanted):
    return [segment for segment in wanted or []
            if segment_of(tracks, segment["spec_id"], segment["hash"]) is None]


# 追った結果を入れる (同じ指紋があれば置き換える)
def put_segment(tracks, segment, result):
    entry = tracks.setdefault("tracks", {}).setdefault(
        str(segment["spec_id"]), {"media_id": str(segment["media_id"]), "segments": []})
    entry["media_id"] = str(segment["media_id"])
    kept = [s for s in entry.get("segments") or []
            if str(s.get("hash") or "") != str(result.get("hash") or "")]
    kept.append(result)
    entry["segments"] = kept
    return entry


# その指定の追従結果を捨てる
def drop_spec(tracks, spec_id):
    (tracks or {}).get("tracks", {}).pop(str(spec_id), None)


# 使わなくなった追従結果を捨てる。
#   decisions : いまの指定 (消した指定の分を捨てる)
#   wanted    : いま必要な区切り (古い指紋の分を捨てる)
def drop_unused(tracks, decisions, wanted):
    if not tracks:
        return
    alive = {str(spec.get("id")) for spec in (decisions or {}).get("specs", [])}
    hashes = {}
    for segment in wanted or []:
        hashes.setdefault(str(segment["spec_id"]), set()).add(str(segment["hash"]))

    for spec_id in list((tracks.get("tracks") or {}).keys()):
        if spec_id not in alive:
            tracks["tracks"].pop(spec_id, None)
            continue
        entry = tracks["tracks"][spec_id]
        keep = hashes.get(spec_id)
        if keep is None:
            continue                    # 追わない指定 (固定) はそのまま残す
        entry["segments"] = [s for s in entry.get("segments") or []
                             if str(s.get("hash") or "") in keep]


# 素材の控えへ、今回の対象素材を足す (指定が増えたとき)
def merge_media(tracks, timeline, media_ids):
    media = tracks.setdefault("media", {})
    for media_id, entry in media_snapshot(timeline, media_ids).items():
        media[media_id] = entry
    return media
