# 透かし (watermark) の焼き込み (ver5 resolve §5.3)
#
# 残高が足りない出力には透かしを入れる。入れるかどうかは**サーバーの応答だけ**で決まり
# (R9)、このモジュールは「入れると決まったものをどう描くか」だけを持つ。
#
# 大きさと余白は**キャンバス幅に対する比率**で決める (R8)。横 1920 と縦 1080 で
# 同じ見え方にするためで、ピクセル値を設定に持たせない。
#
# 焼き込みの経路は 2 つある (ver5 resolve §3.1)。
#   build_chain : 既に合成 (overlay) が走る経路へ混ぜ込む。再エンコードは増えない
#   apply       : 合成が走らない経路で単独パスとして適用する
# どちらを通っても、出力の直前に is_required で「未適用のまま出していないか」を確認する。
import os

from ..utils.logger import get_logger
from . import ffmpeg_runner

_logger = get_logger(__name__)

# 素材の既定パス (src/watermark.png)。凍結配布でも src と同じ構成で入る。
_ASSET_NAME = "watermark.png"

# 設定が空だったときの既定値 (setting.json の watermark セクションと同じ)
_DEFAULTS = {
    "scale_ratio": 0.25,
    "opacity": 0.75,
    "position": "bottom_right",
    "margin_ratio": 0.02,
}

# position → overlay の座標式。W/H はベース、w/h は透かしの寸法、M は余白 (px)。
_POSITIONS = {
    "bottom_right": ("W-w-{m}", "H-h-{m}"),
    "bottom_left": ("{m}", "H-h-{m}"),
    "top_right": ("W-w-{m}", "{m}"),
    "top_left": ("{m}", "{m}"),
}


# watermark セクションを平坦化して返す。未知の position は既定へ落とす。
def config(settings):
    section = settings.get("watermark", {}) if isinstance(settings, dict) else {}
    position = str(section.get("position", _DEFAULTS["position"]) or "").strip()
    if position not in _POSITIONS:
        if position:
            _logger.warning("未知の watermark.position のため既定を使用します: %s", position)
        position = _DEFAULTS["position"]

    return {
        "scale_ratio": _clamp(section.get("scale_ratio"), _DEFAULTS["scale_ratio"], 0.01, 1.0),
        "opacity": _clamp(section.get("opacity"), _DEFAULTS["opacity"], 0.0, 1.0),
        "position": position,
        "margin_ratio": _clamp(section.get("margin_ratio"), _DEFAULTS["margin_ratio"], 0.0, 0.5),
    }


# 透かし素材の絶対パスを返す。見つからなければ None。
def asset_path():
    # src/modules/watermark_overlay.py → src/watermark.png
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _ASSET_NAME)
    return path if os.path.isfile(path) else None


# 透かしを入れる必要があるか (必要かつ未適用なら True)。
# ポイント機能を通らない呼び出し (CLI / テスト) では両方の属性が無く False になる。
def is_required(context):
    if not getattr(context, "watermark_required", False):
        return False
    return not getattr(context, "watermark_applied", False)


# 合成のフィルタチェーンへ足す (再エンコードを増やさない経路)。
# 戻り値: (入力引数, チェーン文字列の一覧, 出力ラベル)。素材が無ければ (None, [], in_label)。
def build_chain(in_label, out_label, canvas_width, canvas_height, cfg, input_index):
    path = asset_path()
    if path is None:
        _logger.warning("透かし素材が見つからないため透かしを省略します: %s", _ASSET_NAME)
        return None, [], in_label

    width, margin = _geometry(canvas_width, cfg)
    label = "[wm]"

    chains = [
        f"[{input_index}:v]scale={width}:-1,format=rgba,"
        f"colorchannelmixer=aa={cfg['opacity']:.3f}{label}",
        f"{in_label}{label}overlay={_overlay_expr(cfg['position'], margin)}{out_label}",
    ]

    # 静止画を 1 フレームだけ読み、尺はベース側に合わせる。
    return ["-i", path], chains, out_label


# 完成した動画へ単独パスで焼き込む (合成が走らない経路)。
# 戻り値: 出力パス。素材が無ければ入力をそのまま返す。
def apply(input_path, output_path, canvas_size, cfg, ffmpeg_cfg,
          total_duration=0.0, on_progress=None):
    path = asset_path()
    if path is None:
        _logger.warning("透かし素材が見つからないため透かしを省略します: %s", _ASSET_NAME)
        return input_path

    canvas_width, _canvas_height = canvas_size
    width, margin = _geometry(canvas_width, cfg)

    filter_complex = (
        f"[1:v]scale={width}:-1,format=rgba,"
        f"colorchannelmixer=aa={cfg['opacity']:.3f}[wm];"
        f"[0:v][wm]overlay={_overlay_expr(cfg['position'], margin)}[v]"
    )

    command = [
        ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg),
        "-y",
        "-i", input_path,
        "-i", path,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        # 音声はそのまま通す。無音の入力でも失敗しないよう任意扱いにする。
        "-map", "0:a?",
        "-c:a", "copy",
    ]
    command += _video_encode_options(ffmpeg_cfg)
    command.append(output_path)

    _logger.info("透かしを焼き込みます: %s", os.path.basename(output_path))
    ffmpeg_runner.execute(
        command,
        total_duration=total_duration,
        on_progress=on_progress,
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
    )

    return output_path


# Resolve 書き出し用の透かしクリップの仕様を返す (ver5 resolve §5.3 / §5.5)。
# 焼き込みではなく、タイムラインの最上位レーンへ全長の素材クリップとして載せる。
# 位置はキャンバス px (左上原点) の**中心座標**で返し、座標系の変換は resolve_export が行う
# (タイトルと同じ規約を 1 か所で持つため)。素材が読めなければ None。
def resolve_clip_spec(canvas_width, canvas_height, duration_sec, cfg):
    path = asset_path()
    if path is None:
        _logger.warning("透かし素材が見つからないため透かしを省略します: %s", _ASSET_NAME)
        return None

    size = _png_size(path)
    if size is None:
        _logger.warning("透かし素材の寸法を読めないため透かしを省略します: %s", path)
        return None

    source_width, source_height = size
    width, margin = _geometry(canvas_width, cfg)
    height = max(int(round(width * source_height / float(source_width))), 1)
    canvas_width = max(int(canvas_width or 0), 2)
    canvas_height = max(int(canvas_height or 0), 2)

    if cfg["position"] in ("bottom_right", "top_right"):
        center_x = canvas_width - margin - width / 2.0
    else:
        center_x = margin + width / 2.0
    if cfg["position"] in ("bottom_right", "bottom_left"):
        center_y = canvas_height - margin - height / 2.0
    else:
        center_y = margin + height / 2.0

    return {
        "path": path,
        "name": "watermark",
        "offset": 0.0,
        "duration": float(duration_sec or 0.0),
        "source_width": source_width,
        "source_height": source_height,
        "width": width,
        "height": height,
        # 素材の原寸に対する倍率 (FCPXML の adjust-transform scale)
        "scale": width / float(source_width),
        "center_px": (center_x, center_y),
        "opacity": float(cfg["opacity"]),
    }


# spec へ透かしクリップを足して返す (ver5 resolve §5.5)。
# 素材が読めない / 尺が 0 のときは spec をそのまま返す (書き出し自体は止めない / §4-3)。
def add_resolve_clip(spec, settings):
    duration = sum(float(c.get("duration") or 0.0) for c in (spec.get("clips") or []))
    if duration <= 0:
        return spec

    overlay = resolve_clip_spec(
        spec.get("width"), spec.get("height"), duration, config(settings))
    if overlay is None:
        return spec

    spec.setdefault("overlays", []).append(overlay)
    _logger.info("Resolve 出力へ透かしクリップを追加しました (全長 %.2fs)", duration)
    return spec


# PNG の寸法を IHDR から読む (外部ライブラリを足さないため / docs/claude.md)
def _png_size(path):
    try:
        with open(path, "rb") as f:
            header = f.read(24)
    except OSError:
        return None

    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None

    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    return (width, height) if width > 0 and height > 0 else None


# 未適用のまま出力させない保険 (ver5 resolve §3.1 / §4-2)。
# 必要かつ未適用なら単独パスで焼き込み、そのパスを返す。不要ならそのまま返す。
def ensure_applied(context, video_path, canvas_size, ffmpeg_cfg, total_duration=0.0):
    if not is_required(context):
        return video_path

    output_path = context.allocate_intermediate("watermark_applied.mp4")
    result = apply(
        video_path, output_path, canvas_size, config(context.settings), ffmpeg_cfg,
        total_duration=total_duration,
        on_progress=context.progress_subcallback("透かし焼き込み"),
    )
    # 素材が無くて焼き込めなかった場合も適用済みとする。
    # 経路ごとに何度も同じ警告を出しても素材は現れないため (apply が警告済み)。
    context.watermark_applied = True
    return result


# 透かしの幅と余白 (px) をキャンバス幅から求める。
def _geometry(canvas_width, cfg):
    canvas_width = max(int(canvas_width or 0), 2)
    # 偶数にそろえる (yuv420p で奇数幅は扱えない)
    width = max(int(canvas_width * float(cfg["scale_ratio"])) // 2 * 2, 2)
    margin = int(canvas_width * float(cfg["margin_ratio"]))

    return width, margin


def _overlay_expr(position, margin):
    x_expr, y_expr = _POSITIONS[position]
    return f"{x_expr.format(m=margin)}:{y_expr.format(m=margin)}"


# 映像のエンコード設定。音声は copy するため -c:a は含めない。
def _video_encode_options(ffmpeg_cfg):
    options = ffmpeg_runner.build_encode_options(ffmpeg_cfg)
    if "-c:a" in options:
        index = options.index("-c:a")
        options = options[:index] + options[index + 2:]

    return options + ["-pix_fmt", "yuv420p"]


def _clamp(value, default, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default

    return min(max(number, low), high)
