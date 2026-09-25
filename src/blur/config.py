# setting.json の blur セクションを読み、型と範囲を整えて返す (ver5 resolve8 §7)
#
# 呼び出し側は「設定が壊れていても既定で動く」ことだけを期待してよい。
# 値の範囲外・型違い・欠落はすべてここで既定へ落とす (画面を落とさない / §4-5)。
#
# ver5 resolve8 で構成を入れ替えた。
#   ・"analysis" / "region" / "manual" を **"track" 1 つ**へまとめた (囲みの追従だけになったため)
#   ・"silhouette" (身体の輪郭) と model.reid を廃止した
#   ・"spec" を **"editor"** へ改名した (データ側の「指定 (specs)」と紛れないため)
# 旧キーしか無い設定でも動くよう、ここで読み替える (§7「旧キーの読み替え」)。
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# ぼかしの種類
MODE_GAUSSIAN = "gaussian"
MODE_PIXELATE = "pixelate"
_MODES = (MODE_GAUSSIAN, MODE_PIXELATE)

# 囲みの塗り方 (ver5 resolve8 §3.6)。囲んだ矩形をそのまま塗るのが既定。
SHAPE_RECT = "rect"
SHAPE_ROUNDED = "rounded"
SHAPE_ELLIPSE = "ellipse"
_SHAPES = (SHAPE_RECT, SHAPE_ROUNDED, SHAPE_ELLIPSE)
# 廃止した塗り方の読み替え先 (身体の輪郭は無くなった / resolve8 §0.3)
_SHAPE_ALIASES = {"silhouette": SHAPE_ROUNDED}

# 指定画面のフレームの見せ方 (ver5 resolve8 §5.12.1)
PREVIEW_BLUR = "blur"     # 実際にぼかした絵を出す (既定)
PREVIEW_KEEP = "keep"     # ボカさない範囲を緑で塗る
PREVIEW_NONE = "none"     # 枠だけ
_PREVIEW_MODES = (PREVIEW_BLUR, PREVIEW_KEEP, PREVIEW_NONE)
# 廃止した表示モードの読み替え先 ("mask" = ぼかす範囲を赤。全面ぼかしでは役に立たない)
_PREVIEW_ALIASES = {"mask": PREVIEW_KEEP}

# 囲みの追従方法 (ver5 resolve8 §5.2)
FOLLOW_TRACK = "track"     # 追いかける (既定)
FOLLOW_FIXED = "fixed"     # 動かさない (追えなかった枠の逃げ道)
_FOLLOWS = (FOLLOW_TRACK, FOLLOW_FIXED)

# 設定が空だったときの既定値 (settings_window.DEFAULT_SETTINGS["blur"] と同じ)
_DEFAULTS = {
    "enabled": False,
    "preview_marker": True,
    # Timeline 編集画面のプレビューで、停止中に実際のぼかしを反映する (ver5 resolve4 §5.8)
    "preview_blur": True,
    "model": {
        "detector": "models/yolox_tiny.onnx",
        "detector_format": "yolox",
        "detector_input": 416,
        "detector_pad_value": 114,
        # 追従の助けとして使うため、拾いやすいしきい値にする (ver5 resolve8 §7)
        "detector_score": 0.2,
        "detector_nms": 0.5,
        "providers": ["CPUExecutionProvider"],
    },
    # 囲みの追従 (ver5 resolve8 §3.4)
    "track": {
        "sample_fps": 10.0,       # 追う間隔
        "auto_start": True,       # 囲んだ直後に自動で追う
        "search_ratio": 1.0,      # 枠の何倍ぶん広げて探すか
        "min_iou": 0.3,           # 検出枠を同じ相手とみなす重なり
        "match_psr": 8.0,         # 位相相関の合格ライン
        "hold_sec": 1.0,          # 分からない状態を我慢する秒数
    },
    "render": {
        "mode": MODE_GAUSSIAN,
        "strength": 50,
        "shape": SHAPE_RECT,
        "margin_ratio": 0.0,      # 囲みの大きさは利用者が決めるため既定は 0
        "feather_ratio": 0.006,   # 縁のなじませ (ぼかす / ボカさない 共通)
        "mask_scale": 0.25,
    },
    # 指定画面のふるまい (旧 "spec")
    "editor": {
        "preview_mode": PREVIEW_BLUR,
        "show_overlays": True,
        "step_frames": 10,        # Shift + ← → で送るコマ数
        "frame_cache": 32,        # 指定画面のフレームキャッシュ枚数
        "handle_px": 10,          # 大きさを変えるハンドルの当たり判定 (画面 px)
        "min_size_ratio": 0.01,   # 囲みの最小の大きさ (キャンバス幅比)
        "name_max_len": 32,       # 指定の名前の最大文字数
    },
}

# 旧キーからの読み替え表 (新しいキー → 旧セクションと旧キー / ver5 resolve8 §7)
_LEGACY = {
    ("track", "sample_fps"): ("analysis", "sample_fps"),
    ("track", "auto_start"): ("analysis", "auto_start"),
    ("track", "search_ratio"): ("manual", "search_ratio"),
    ("track", "min_iou"): ("manual", "min_iou"),
    ("track", "match_psr"): ("manual", "match_psr"),
    ("track", "hold_sec"): ("region", "hold_sec"),
    ("model", "detector_score"): ("manual", "detector_score"),
    ("editor", "preview_mode"): ("spec", "preview_mode"),
    ("editor", "show_overlays"): ("spec", "show_overlays"),
    ("editor", "name_max_len"): ("spec", "name_max_len"),
}


# blur セクションを平坦化して返す。未知・範囲外の値は既定へ落とす。
def config(settings):
    section = settings.get("blur", {}) if isinstance(settings, dict) else {}
    if not isinstance(section, dict):
        section = {}

    model = _Reader(section, "model")
    track = _Reader(section, "track")
    render = _Reader(section, "render")
    editor = _Reader(section, "editor")
    model_defaults = _DEFAULTS["model"]
    track_defaults = _DEFAULTS["track"]
    render_defaults = _DEFAULTS["render"]
    editor_defaults = _DEFAULTS["editor"]

    return {
        "enabled": bool(section.get("enabled", _DEFAULTS["enabled"])),
        "preview_marker": bool(section.get("preview_marker", _DEFAULTS["preview_marker"])),
        "preview_blur": bool(section.get("preview_blur", _DEFAULTS["preview_blur"])),
        "model": {
            "detector": _text(model.get("detector"), model_defaults["detector"]),
            "detector_format": _text(model.get("detector_format"),
                                     model_defaults["detector_format"]).lower(),
            # 入力の一辺は 32 の倍数でなければ検出器のグリッドと合わない
            "detector_input": _multiple_of(model.get("detector_input"), 32,
                                           model_defaults["detector_input"], 128, 1280),
            "detector_pad_value": _int(model.get("detector_pad_value"),
                                       model_defaults["detector_pad_value"], 0, 255),
            "detector_score": _float(model.get("detector_score"),
                                     model_defaults["detector_score"], 0.01, 0.99),
            "detector_nms": _float(model.get("detector_nms"),
                                   model_defaults["detector_nms"], 0.05, 0.95),
            "providers": _providers(model.get("providers")),
        },
        "track": {
            "sample_fps": _float(track.get("sample_fps"),
                                 track_defaults["sample_fps"], 0.5, 30.0),
            "auto_start": bool(_default(track.get("auto_start"), track_defaults["auto_start"])),
            "search_ratio": _float(track.get("search_ratio"),
                                   track_defaults["search_ratio"], 0.1, 5.0),
            "min_iou": _float(track.get("min_iou"), track_defaults["min_iou"], 0.01, 0.99),
            "match_psr": _float(track.get("match_psr"),
                                track_defaults["match_psr"], 1.0, 1000.0),
            "hold_sec": _float(track.get("hold_sec"), track_defaults["hold_sec"], 0.0, 10.0),
        },
        "render": {
            "mode": _choice(render.get("mode"), _MODES,
                            render_defaults["mode"], "blur.render.mode"),
            "strength": _int(render.get("strength"), render_defaults["strength"], 1, 100),
            "shape": _shape(render.get("shape")),
            "margin_ratio": _float(render.get("margin_ratio"),
                                   render_defaults["margin_ratio"], 0.0, 0.5),
            "feather_ratio": _float(render.get("feather_ratio"),
                                    render_defaults["feather_ratio"], 0.0, 0.1),
            "mask_scale": _float(render.get("mask_scale"),
                                 render_defaults["mask_scale"], 0.05, 1.0),
        },
        "editor": {
            "preview_mode": _preview_mode(editor.get("preview_mode")),
            "show_overlays": bool(_default(editor.get("show_overlays"),
                                           editor_defaults["show_overlays"])),
            "step_frames": _int(editor.get("step_frames"),
                                editor_defaults["step_frames"], 1, 120),
            "frame_cache": _int(editor.get("frame_cache"),
                                editor_defaults["frame_cache"], 1, 256),
            "handle_px": _int(editor.get("handle_px"), editor_defaults["handle_px"], 4, 40),
            "min_size_ratio": _float(editor.get("min_size_ratio"),
                                     editor_defaults["min_size_ratio"], 0.001, 0.5),
            "name_max_len": _int(editor.get("name_max_len"),
                                 editor_defaults["name_max_len"], 1, 200),
        },
    }


# 機能が有効か (設定を読むだけ。モデルの有無は models.availability で見る)
def is_enabled(settings):
    section = settings.get("blur", {}) if isinstance(settings, dict) else {}
    return bool(section.get("enabled", False)) if isinstance(section, dict) else False


# ぼかしの sigma / モザイク片の大きさ (キャンバス幅から求める)
# strength 1〜100 を「キャンバス幅 x 0.00025 x strength」へ写す。
# 1920px・strength 50 で sigma 24 相当。比率で決めるため縦動画でも見え方が揃う。
def blur_sigma(canvas_width, cfg):
    width = max(int(canvas_width or 0), 2)
    return max(width * 0.00025 * float(cfg["render"]["strength"]), 1.0)


# 新しいキーを読み、無ければ旧キーを読む道具 (ver5 resolve8 §7)
class _Reader:

    def __init__(self, section, name):
        self._name = name
        value = section.get(name)
        self._values = value if isinstance(value, dict) else {}
        self._section = section

    def get(self, key):
        if key in self._values:
            return self._values[key]
        legacy = _LEGACY.get((self._name, key))
        if legacy is None:
            return None
        old_section = self._section.get(legacy[0])
        if isinstance(old_section, dict) and legacy[1] in old_section:
            return old_section[legacy[1]]
        return None


# 囲みの塗り方。廃止した "silhouette" は "rounded" へ読み替える
def _shape(value):
    text = str(value if value is not None else "").strip()
    text = _SHAPE_ALIASES.get(text, text)
    return _choice(text, _SHAPES, _DEFAULTS["render"]["shape"], "blur.render.shape")


# 指定画面の表示モード。廃止した "mask" は "keep" へ読み替える
def _preview_mode(value):
    text = str(value if value is not None else "").strip()
    text = _PREVIEW_ALIASES.get(text, text)
    return _choice(text, _PREVIEW_MODES, _DEFAULTS["editor"]["preview_mode"],
                   "blur.editor.preview_mode")


def _default(value, default):
    return default if value is None else value


def _text(value, default):
    text = str(value if value is not None else "").strip()
    return text or default


def _choice(value, allowed, default, label):
    text = str(value if value is not None else "").strip()
    if text in allowed:
        return text
    if text:
        _logger.warning("未知の %s のため既定を使用します: %s", label, text)
    return default


def _float(value, default, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:            # NaN は既定へ落とす
        return default
    return min(max(number, low), high)


def _int(value, default, low, high):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


# 32 の倍数へそろえた整数 (検出器の入力サイズ用)
def _multiple_of(value, unit, default, low, high):
    number = _int(value, default, low, high)
    return max(int(round(number / unit)) * unit, unit)


# onnxruntime の実行プロバイダ。空なら CPU にする。
def _providers(value):
    if isinstance(value, (list, tuple)):
        names = [str(v).strip() for v in value if str(v or "").strip()]
        if names:
            return names
    return list(_DEFAULTS["model"]["providers"])
