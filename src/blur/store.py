# 解析結果キャッシュの読み書き (ver5 resolve2 §5.2.1 / §5.3.4)
#
# 解析結果は**重い**ため timeline.source へは置かない (§4-2)。
# Undo のたびに deepcopy されて画面が固まるため、別ファイルへ持つ。
#
# 置き場はプロジェクトファイルの隣 (<プロジェクト名>.blur.json)。
# プロジェクト未保存なら working_dir (実行終了で消える)。
#
# 整合性は指紋 (fingerprint) で確かめる。合わなければ捨てて解析し直す。
# キャッシュを消しても、指定 (source["blur"]) が残っていれば解析し直して復元できる (§8-7)。
import base64
import hashlib
import io
import json
import os

from ..utils.logger import get_logger
from ..version import __version__

_logger = get_logger(__name__)

# キャッシュの書式版。増やしたら古いキャッシュは読まずに作り直す。
#   2: 人物のまとめ方の修正・輪郭 (sil)・追加した枠の形 (outline / shape_hash) (ver5 resolve3)
SCHEMA_VERSION = 2

# プロジェクトファイルの隣へ置くときの拡張子
CACHE_SUFFIX = ".blur.json"


# プロジェクトのパスから解析結果の置き場を決める。
# プロジェクトが未保存なら working_dir の中へ置く (実行終了で消えてよい)。
def cache_path_for(project_path, working_dir):
    if project_path:
        base = os.path.splitext(project_path)[0]
        return base + CACHE_SUFFIX
    return os.path.join(working_dir or "", "timeline" + CACHE_SUFFIX)


# 解析結果の指紋を作る (§5.3.4)
#   元動画のパス + サイズ + 更新時刻 + キャンバス幅 x 高さ + sample_fps + モデル名 + モデルの版
# source.media_path (正規化後の一時ファイル) は毎回変わるため使わない。
# input_path (元動画) を使うことで、開き直しても同じ指紋になり解析を省ける。
def fingerprint(timeline, cfg, media_paths=None):
    parts = [f"schema={SCHEMA_VERSION}"]

    paths = list(media_paths or [])
    if not paths:
        source = timeline.source or {}
        candidate = source.get("input_path") or source.get("media_path") or ""
        if candidate:
            paths = [candidate]

    for path in sorted(str(p) for p in paths if p):
        parts.append(f"path={path}")
        try:
            stat = os.stat(path)
            parts.append(f"size={stat.st_size}")
            parts.append(f"mtime={int(stat.st_mtime)}")
        except OSError:
            parts.append("size=? mtime=?")

    parts.append(f"canvas={timeline.width}x{timeline.height}")
    parts.append(f"sample_fps={float(cfg['analysis']['sample_fps']):.3f}")
    parts.append(f"detector={cfg['model']['detector']}")
    parts.append(f"detector_input={cfg['model']['detector_input']}")
    parts.append(f"reid={cfg['model']['reid']}")
    # モデルファイルの更新時刻も入れる = 差し替えたら解析し直す
    parts.append(f"models={_model_stamp(cfg)}")
    # 人物のまとめ方 (ver5 resolve3 §5.8)。変えたら人物 ID が変わるため解析し直す
    parts.append(f"same_box={cfg['analysis']['same_box_containment']:.3f},"
                 f"{cfg['analysis']['same_box_center_ratio']:.3f}")
    # 輪郭 (ver5 resolve3 §5.2.3)。人物の形が輪郭のときだけ入れる。
    # 矩形系どうしの切り替えは塗り方が変わるだけなので、解析し直さない。
    parts.append(f"silhouette={silhouette_stamp(cfg)}")

    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# 輪郭の解析条件を 1 つの文字列にする。輪郭を使わない・モデルが無いときは "off"。
def silhouette_stamp(cfg):
    from . import models              # noqa: PLC0415 (循環 import を避けるため関数内で読む)
    from .config import SHAPE_SILHOUETTE  # noqa: PLC0415

    if str(cfg["render"]["shape"]) != SHAPE_SILHOUETTE:
        return "off"
    sil = cfg["silhouette"]
    stamps = []
    for key in ("model", "decoder"):
        path = models.resolve_path(sil[key])
        if path is None:
            return "off"
        try:
            stat = os.stat(path)
            stamps.append(f"{stat.st_size}:{int(stat.st_mtime)}")
        except OSError:
            return "off"
    return (f"{sil['format']}:{sil['input']}:{sil['every_n_samples']}:{sil['points']}:"
            f"{sil['threshold']}:" + ",".join(stamps))


# 同梱モデルの版 (ファイルサイズと更新時刻) を 1 つの文字列にする
def _model_stamp(cfg):
    from . import models              # noqa: PLC0415 (循環 import を避けるため関数内で読む)

    stamps = []
    for key in ("detector", "reid"):
        path = models.resolve_path(cfg["model"][key])
        if path is None:
            stamps.append("-")
            continue
        try:
            stat = os.stat(path)
            stamps.append(f"{stat.st_size}:{int(stat.st_mtime)}")
        except OSError:
            stamps.append("?")
    return ",".join(stamps)


# 解析結果を書き出す。失敗しても例外にしない (キャッシュは無くても動く)。
def save(path, analysis):
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(analysis, handle, ensure_ascii=False)
    except (OSError, TypeError, ValueError) as error:
        _logger.warning("ぼかしの解析結果を保存できませんでした: %s (%s)", path, error)
        return False
    _logger.info("ぼかしの解析結果を保存しました: %s", os.path.basename(path))
    return True


# 解析結果を読む。無い・壊れている・指紋が違うなら None を返す。
# 壊れていた場合は WARNING を残して捨てる (§5.9)。
def load(path, expected_fingerprint=None):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        _logger.warning("ぼかしの解析結果が読めないため作り直します: %s (%s)", path, error)
        return None

    if not isinstance(data, dict):
        _logger.warning("ぼかしの解析結果の形式が不正なため作り直します: %s", path)
        return None
    if int(data.get("schema", 0)) != SCHEMA_VERSION:
        _logger.info("ぼかしの解析結果の版が違うため作り直します: %s", path)
        return None
    if expected_fingerprint and data.get("fingerprint") != expected_fingerprint:
        _logger.info("素材か設定が変わったため、ぼかしの解析をやり直します")
        return None
    return data


# 解析結果の入れ物を作る (analyzer が埋める)
def new_analysis(timeline, cfg, fingerprint_value):
    return {
        "schema": SCHEMA_VERSION,
        "generator": f"Stretheus {__version__}",
        "fingerprint": fingerprint_value,
        "canvas": {"width": int(timeline.width), "height": int(timeline.height)},
        "sample_fps": float(cfg["analysis"]["sample_fps"]),
        "identities": [],
        "tracks": [],
    }


# 人物一覧に出す見本画像を base64 PNG にする (§5.2.1 thumb)
# PIL は配布物に入っているが、無い環境でも解析を止めないため任意扱いにする。
def encode_thumb(image, box, thumb_px):
    try:
        from PIL import Image                   # noqa: PLC0415 (任意依存の遅延 import)
    except Exception:                           # noqa: BLE001
        return ""

    try:
        height, width = image.shape[:2]
        x, y, w, h = (float(v) for v in box[:4])
        left = max(int(x), 0)
        top = max(int(y), 0)
        right = min(int(x + w), width)
        bottom = min(int(y + h), height)
        if right - left < 2 or bottom - top < 2:
            return ""
        crop = Image.fromarray(image[top:bottom, left:right])
        crop.thumbnail((int(thumb_px), int(thumb_px)))
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as error:                  # noqa: BLE001 (見本が作れなくても解析は続ける)
        _logger.debug("見本画像を作れませんでした: %s", error)
        return ""


# base64 PNG を bytes へ戻す (画面側で QPixmap にする)
def decode_thumb(encoded):
    if not encoded:
        return b""
    try:
        return base64.b64decode(encoded)
    except (ValueError, TypeError):
        return b""


# 解析結果から人物 ID → identity の辞書を作る
def identities_by_id(analysis):
    return {str(item.get("id")): item for item in (analysis or {}).get("identities", [])}


# 解析結果から主役の人物 ID を返す (居なければ None / R3)
def main_identity_id(analysis):
    for item in (analysis or {}).get("identities", []):
        if item.get("main"):
            return str(item.get("id"))
    return None
