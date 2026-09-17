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
