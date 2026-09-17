# setting.json の blur セクションを読み、型と範囲を整えて返す (ver5 resolve2 §7)
#
# 呼び出し側は「設定が壊れていても既定で動く」ことだけを期待してよい。
# 値の範囲外・型違い・欠落はすべてここで既定へ落とす (画面を落とさない / §4-5)。
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 囲っていない人物の扱い (§9-1)
POLICY_BLUR_OTHERS = "blur_others"    # 主役以外はぼかす (既定)
POLICY_MANUAL_ONLY = "manual_only"    # 囲ったものだけぼかす
_POLICIES = (POLICY_BLUR_OTHERS, POLICY_MANUAL_ONLY)

# ぼかしの種類
MODE_GAUSSIAN = "gaussian"
MODE_PIXELATE = "pixelate"
_MODES = (MODE_GAUSSIAN, MODE_PIXELATE)

# 人物の塗り方。silhouette は輪郭モデルで身体の形に沿わせる (ver5 resolve3 §3.1)。
# 輪郭が取れない時刻・モデルが無い環境では rounded と同じ塗りへ落ちる (§3.3)。
SHAPE_SILHOUETTE = "silhouette"
SHAPE_ROUNDED = "rounded"
_SHAPES = (SHAPE_SILHOUETTE, SHAPE_ROUNDED, "rect", "ellipse")

# 輪郭モデルの前後処理の種類 (§5.3.2)
_SILHOUETTE_FORMATS = ("mobilesam", "efficientsam", "sam2")

# 領域の追従方法 (§3.4)
FOLLOW_TRACK = "track"
FOLLOW_FIXED = "fixed"
_FOLLOWS = (FOLLOW_TRACK, FOLLOW_FIXED)

# 設定が空だったときの既定値 (settings_window.DEFAULT_SETTINGS["blur"] と同じ)
_DEFAULTS = {
    "enabled": False,
    "default_policy": POLICY_BLUR_OTHERS,
    "preview_marker": True,
    "model": {
        "detector": "models/yolox_tiny.onnx",
        "detector_format": "yolox",
        "detector_input": 416,
        "detector_pad_value": 114,
        "detector_score": 0.4,
        "detector_nms": 0.5,
        "reid": "models/osnet_x0_25.onnx",
        "reid_format": "osnet",
        "reid_input": [128, 256],
        "reid_dim": 512,
        "providers": ["CPUExecutionProvider"],
    },
    "analysis": {
        "sample_fps": 5.0,
        "auto_start": True,
        "min_track_sec": 0.6,
        "iou_threshold": 0.3,
        "embed_threshold": 0.35,
        "merge_threshold": 0.30,
        "max_identities": 50,
        # 同じ時刻の 2 つの枠を「1 人への重複した枠」とみなす条件 (ver5 resolve3 §5.8)。
        # 小さい枠がこの割合以上入っていて、かつ横の中心のずれが小さい枠の幅のこの倍率以内。
        "same_box_containment": 0.85,
        "same_box_center_ratio": 0.5,
    },
    "region": {
        "follow": FOLLOW_TRACK,
        "search_scale": 0.5,
        "match_psr": 25.0,
        "hold_sec": 1.0,
    },
    "render": {
        "mode": MODE_GAUSSIAN,
        "strength": 50,
        "margin_ratio": 0.06,
        "feather_ratio": 0.006,
        "pad_sec": 0.2,
        "mask_scale": 0.25,
        "shape": SHAPE_SILHOUETTE,
        # ぼかさない層を広げる量 (キャンバス幅比)。0 = 形ちょうどで削る (ver5 resolve3 §3.4)
        "keep_margin_ratio": 0.0,
        "keep_motion_margin": False,
    },
    # 身体の輪郭 (ver5 resolve3 §3.1〜§3.3)
    "silhouette": {
        "model": "models/silhouette_encoder.onnx",
        "decoder": "models/silhouette_decoder.onnx",
        "format": "mobilesam",
        "input": 1024,
        "threshold": 0.0,
        "every_n_samples": 4,
        "points": 64,
        "min_fill_ratio": 0.15,
        "max_gap_sec": 1.0,
        "dilate_ratio": 0.01,
        "margin_box_ratio": 0.15,
        "max_margin_box_ratio": 0.5,
        "motion_lookahead_sec": 0.3,
        "fast_motion_box_ratio": 0.15,
    },
    # 手で足した人物・物の追従 (ver5 resolve3 §3.6)
    "manual": {
        "detector_score": 0.2,
        "search_ratio": 1.0,
        "min_iou": 0.3,
        "fixed_span_sec": 2.0,
        "match_psr": 8.0,
    },
    "spec": {
        "hit_ratio": 0.5,
        "thumb_px": 96,
    },
}


# blur セクションを平坦化して返す。未知・範囲外の値は既定へ落とす。
def config(settings):
    section = settings.get("blur", {}) if isinstance(settings, dict) else {}
    if not isinstance(section, dict):
        section = {}

    model = _sub(section, "model")
    analysis = _sub(section, "analysis")
    region = _sub(section, "region")
    render = _sub(section, "render")
    spec = _sub(section, "spec")
    silhouette = _sub(section, "silhouette")
    manual = _sub(section, "manual")
    sil_defaults = _DEFAULTS["silhouette"]
    manual_defaults = _DEFAULTS["manual"]

    return {
        "enabled": bool(section.get("enabled", _DEFAULTS["enabled"])),
        "default_policy": _choice(section.get("default_policy"), _POLICIES,
                                  _DEFAULTS["default_policy"], "blur.default_policy"),
        "preview_marker": bool(section.get("preview_marker", _DEFAULTS["preview_marker"])),
        "model": {
            "detector": _text(model.get("detector"), _DEFAULTS["model"]["detector"]),
            "detector_format": _text(model.get("detector_format"),
                                     _DEFAULTS["model"]["detector_format"]).lower(),
            # 入力の一辺は 32 の倍数でなければ検出器のグリッドと合わない
            "detector_input": _multiple_of(model.get("detector_input"), 32,
                                           _DEFAULTS["model"]["detector_input"], 128, 1280),
            "detector_pad_value": _int(model.get("detector_pad_value"),
                                       _DEFAULTS["model"]["detector_pad_value"], 0, 255),
            "detector_score": _float(model.get("detector_score"),
                                     _DEFAULTS["model"]["detector_score"], 0.01, 0.99),
            "detector_nms": _float(model.get("detector_nms"),
                                   _DEFAULTS["model"]["detector_nms"], 0.05, 0.95),
            "reid": _text(model.get("reid"), _DEFAULTS["model"]["reid"]),
            "reid_format": _text(model.get("reid_format"),
                                 _DEFAULTS["model"]["reid_format"]).lower(),
            "reid_input": _size_pair(model.get("reid_input"), _DEFAULTS["model"]["reid_input"]),
            "reid_dim": _int(model.get("reid_dim"), _DEFAULTS["model"]["reid_dim"], 32, 4096),
            "providers": _providers(model.get("providers")),
        },
        "analysis": {
            "sample_fps": _float(analysis.get("sample_fps"),
                                 _DEFAULTS["analysis"]["sample_fps"], 0.5, 30.0),
            "auto_start": bool(analysis.get("auto_start", _DEFAULTS["analysis"]["auto_start"])),
            "min_track_sec": _float(analysis.get("min_track_sec"),
                                    _DEFAULTS["analysis"]["min_track_sec"], 0.0, 10.0),
            "iou_threshold": _float(analysis.get("iou_threshold"),
                                    _DEFAULTS["analysis"]["iou_threshold"], 0.01, 0.95),
            "embed_threshold": _float(analysis.get("embed_threshold"),
                                      _DEFAULTS["analysis"]["embed_threshold"], 0.01, 2.0),
            "merge_threshold": _float(analysis.get("merge_threshold"),
                                      _DEFAULTS["analysis"]["merge_threshold"], 0.01, 2.0),
            "max_identities": _int(analysis.get("max_identities"),
                                   _DEFAULTS["analysis"]["max_identities"], 1, 500),
            "same_box_containment": _float(analysis.get("same_box_containment"),
                                           _DEFAULTS["analysis"]["same_box_containment"],
                                           0.1, 1.0),
            "same_box_center_ratio": _float(analysis.get("same_box_center_ratio"),
                                            _DEFAULTS["analysis"]["same_box_center_ratio"],
                                            0.0, 5.0),
        },
        "region": {
            "follow": _choice(region.get("follow"), _FOLLOWS,
                              _DEFAULTS["region"]["follow"], "blur.region.follow"),
            "search_scale": _float(region.get("search_scale"),
                                   _DEFAULTS["region"]["search_scale"], 0.1, 1.0),
            "match_psr": _float(region.get("match_psr"),
                                _DEFAULTS["region"]["match_psr"], 1.0, 1000.0),
            "hold_sec": _float(region.get("hold_sec"), _DEFAULTS["region"]["hold_sec"], 0.0, 10.0),
        },
        "render": {
            "mode": _choice(render.get("mode"), _MODES,
                            _DEFAULTS["render"]["mode"], "blur.render.mode"),
            "strength": _int(render.get("strength"), _DEFAULTS["render"]["strength"], 1, 100),
            "margin_ratio": _float(render.get("margin_ratio"),
                                   _DEFAULTS["render"]["margin_ratio"], 0.0, 0.5),
            "feather_ratio": _float(render.get("feather_ratio"),
                                    _DEFAULTS["render"]["feather_ratio"], 0.0, 0.1),
            "pad_sec": _float(render.get("pad_sec"), _DEFAULTS["render"]["pad_sec"], 0.0, 5.0),
            "mask_scale": _float(render.get("mask_scale"),
                                 _DEFAULTS["render"]["mask_scale"], 0.05, 1.0),
            "shape": _choice(render.get("shape"), _SHAPES,
                             _DEFAULTS["render"]["shape"], "blur.render.shape"),
            "keep_margin_ratio": _float(render.get("keep_margin_ratio"),
                                        _DEFAULTS["render"]["keep_margin_ratio"], 0.0, 0.2),
            "keep_motion_margin": bool(render.get("keep_motion_margin",
                                                  _DEFAULTS["render"]["keep_motion_margin"])),
        },
        "silhouette": {
            "model": _text(silhouette.get("model"), sil_defaults["model"]),
            "decoder": _text(silhouette.get("decoder"), sil_defaults["decoder"]),
            "format": _choice(silhouette.get("format"), _SILHOUETTE_FORMATS,
                              sil_defaults["format"], "blur.silhouette.format"),
            "input": _multiple_of(silhouette.get("input"), 32, sil_defaults["input"], 256, 2048),
            "threshold": _float(silhouette.get("threshold"), sil_defaults["threshold"],
                                -20.0, 20.0),
            "every_n_samples": _int(silhouette.get("every_n_samples"),
                                    sil_defaults["every_n_samples"], 1, 30),
            "points": _int(silhouette.get("points"), sil_defaults["points"], 16, 256),
            "min_fill_ratio": _float(silhouette.get("min_fill_ratio"),
                                     sil_defaults["min_fill_ratio"], 0.0, 1.0),
            "max_gap_sec": _float(silhouette.get("max_gap_sec"), sil_defaults["max_gap_sec"],
                                  0.1, 30.0),
            "dilate_ratio": _float(silhouette.get("dilate_ratio"), sil_defaults["dilate_ratio"],
                                   0.0, 0.1),
            "margin_box_ratio": _float(silhouette.get("margin_box_ratio"),
                                       sil_defaults["margin_box_ratio"], 0.0, 1.0),
            "max_margin_box_ratio": _float(silhouette.get("max_margin_box_ratio"),
                                           sil_defaults["max_margin_box_ratio"], 0.0, 2.0),
            "motion_lookahead_sec": _float(silhouette.get("motion_lookahead_sec"),
                                           sil_defaults["motion_lookahead_sec"], 0.0, 2.0),
            "fast_motion_box_ratio": _float(silhouette.get("fast_motion_box_ratio"),
                                            sil_defaults["fast_motion_box_ratio"], 0.0, 5.0),
        },
        "manual": {
            "detector_score": _float(manual.get("detector_score"),
                                     manual_defaults["detector_score"], 0.01, 0.99),
            "search_ratio": _float(manual.get("search_ratio"), manual_defaults["search_ratio"],
                                   0.1, 5.0),
            "min_iou": _float(manual.get("min_iou"), manual_defaults["min_iou"], 0.01, 0.99),
            "fixed_span_sec": _float(manual.get("fixed_span_sec"),
                                     manual_defaults["fixed_span_sec"], 0.1, 60.0),
            "match_psr": _float(manual.get("match_psr"), manual_defaults["match_psr"],
                                1.0, 1000.0),
        },
        "spec": {
            "hit_ratio": _float(spec.get("hit_ratio"), _DEFAULTS["spec"]["hit_ratio"], 0.0, 1.0),
            "thumb_px": _int(spec.get("thumb_px"), _DEFAULTS["spec"]["thumb_px"], 32, 512),
        },
    }


# 機能が有効か (設定を読むだけ。モデルの有無は models.availability で見る)
def is_enabled(settings):
    section = settings.get("blur", {}) if isinstance(settings, dict) else {}
    return bool(section.get("enabled", False)) if isinstance(section, dict) else False


# ぼかしの sigma / モザイク片の大きさ (キャンバス幅から求める / §7)
# strength 1〜100 を「キャンバス幅 x 0.00025 x strength」へ写す。
# 1920px・strength 50 で sigma 24 相当。比率で決めるため縦動画でも見え方が揃う。
def blur_sigma(canvas_width, cfg):
    width = max(int(canvas_width or 0), 2)
    return max(width * 0.00025 * float(cfg["render"]["strength"]), 1.0)


def _sub(section, key):
    value = section.get(key)
    return value if isinstance(value, dict) else {}


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


# [幅, 高さ] の組。壊れていれば既定を返す。
def _size_pair(value, default):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        width = _int(value[0], default[0], 8, 2048)
        height = _int(value[1], default[1], 8, 2048)
        return [width, height]
    return list(default)


# onnxruntime の実行プロバイダ。空なら CPU にする。
def _providers(value):
    if isinstance(value, (list, tuple)):
        names = [str(v).strip() for v in value if str(v or "").strip()]
        if names:
            return names
    return list(_DEFAULTS["model"]["providers"])
