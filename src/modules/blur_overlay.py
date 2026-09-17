# ぼかしの焼き込み (ver5 resolve2 §3.5 / §5.5.1)
#
# 対象は毎フレーム動くため、マスク動画を作って alphamerge で合成する (§3.5 案 2)。
# マスクは movie= ソースフィルタで filtergraph の内側から読むため、**-i は増えない**
# (comment_decor.build_icon_chains と同じ手口 / §2.3)。
#
#   movie='<mask.mkv>',format=gray,scale=<W>:<H>[bmask];
#   [<in>]split=2[bkeep][bsrc];
#   [bsrc]gblur=sigma=<S>[bblur];
#   [bblur][bmask]alphamerge[bblura];
#   [bkeep][bblura]overlay=0:0:eof_action=pass[<out>]
#
# 焼き込みの経路は 2 つある (watermark_overlay と同じ作り)。
#   build_chains : 既に合成 (overlay) や焼き込みが走る経路へ混ぜ込む。再エンコードは増えない
#   apply        : どちらも走らない経路で単独パスとして適用する
# どちらを通っても、出力の直前に is_required で「未適用のまま出していないか」を確認する。
import os

from ..utils.logger import get_logger
from . import ffmpeg_runner

_logger = get_logger(__name__)


# ぼかしが必要か (マスクがあり、まだ適用していない)。
# ぼかし機能を通らない呼び出し (CLI / テスト) では属性が無く False になる。
def is_required(context):
    if not getattr(context, "blur_mask_path", None):
        return False
    return not getattr(context, "blur_applied", False)


# マスク生成に失敗したまま出力しようとしていないか (§5.9)
# 「ぼかすと指定したのに素で出た」を防ぐため、呼び出し側はこれが True のとき
# 利用者へ確認を出す。**黙って続行しない。**
def has_failed(context):
    return bool(getattr(context, "blur_mask_failed", False))


# 合成のフィルタチェーンへ足す (再エンコードを増やさない経路)。
#   mask_path : マスク動画のパス
#   in_label  : 入力ラベル (例 "[0:v]")
#   out_label : 出力ラベル (空文字なら -vf の最終出力として無ラベルにする)
#   prefix    : 中間ラベルの接頭辞。1 つの filtergraph で複数回呼ぶときの衝突よけ
# 戻り値: (チェーンの一覧, 出力ラベル)。マスクが無ければ ([], in_label)。
#
# **必ずチェーンの先頭へ足すこと。** 後ろへ足すと字幕やコメントアイコンまでぼける (R8)。
def build_chains(mask_path, in_label, out_label, canvas_width, canvas_height, cfg,
                 prefix="bl"):
    if not mask_path or not os.path.isfile(mask_path):
        _logger.warning("ぼかしマスクが見つからないため、ぼかしを省略します: %s", mask_path)
        return [], in_label

    from ..blur.config import blur_sigma, MODE_PIXELATE   # noqa: PLC0415 (機能 OFF なら読まない)

    mask = f"[{prefix}mask]"
    keep = f"[{prefix}keep]"
    source = f"[{prefix}src]"
    blurred = f"[{prefix}blur]"
    merged = f"[{prefix}alpha]"

    if str(cfg["render"]["mode"]) == MODE_PIXELATE:
        # モザイク: 一度粗く縮めてから最近傍で戻す
        block = max(int(blur_sigma(canvas_width, cfg)), 2)
        effect = (f"scale=iw/{block}:-1:flags=neighbor,"
                  f"scale=iw*{block}:ih*{block}:flags=neighbor")
    else:
        effect = f"gblur=sigma={blur_sigma(canvas_width, cfg):.2f}"

    # split を先頭に置く。-vf (簡易フィルタグラフ) では**最初のチェーンの
    # ラベル無し入力**が映像入力に結び付くため、movie= (入力を持たないソース) を
    # 先頭にすると in_label が空のときに結び付け先を失う。
    chains = [
        f"{in_label}split=2{keep}{source}",
        # マスクはキャンバスより小さく作ってあるため、ここで戻す (§3.5)
        f"movie='{_escape_filter_path(mask_path)}',format=gray,"
        f"scale={int(canvas_width)}:{int(canvas_height)}{mask}",
        f"{source}{effect}{blurred}",
        f"{blurred}{mask}alphamerge{merged}",
        # eof_action=pass は必須。付けないとマスクが尽きた後、FFmpeg は最後の
        # マスクを永久に繰り返す (= 以降ずっと同じ位置がぼける)。
        f"{keep}{merged}overlay=0:0:eof_action=pass{out_label}",
    ]
    return chains, out_label


# 完成した動画へ単独パスで焼き込む (合成も焼き込みも走らない経路)。
# 戻り値: 出力パス。マスクが無ければ入力をそのまま返す。
def apply(input_path, output_path, mask_path, canvas_size, cfg, ffmpeg_cfg,
          total_duration=0.0, on_progress=None):
    canvas_width, canvas_height = canvas_size
    chains, _label = build_chains(
        mask_path, "[0:v]", "[v]", canvas_width, canvas_height, cfg)
    if not chains:
        return input_path

    command = [
        ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg),
        "-y", "-hide_banner",
        "-i", input_path,
        "-filter_complex", ";".join(chains),
        "-map", "[v]",
        # 音声はそのまま通す。無音の入力でも失敗しないよう任意扱いにする。
        "-map", "0:a?",
        "-c:a", "copy",
    ]
    command += _video_encode_options(ffmpeg_cfg)
    command.append(output_path)

    _logger.info("ぼかしを焼き込みます: %s", os.path.basename(output_path))
    ffmpeg_runner.execute(
        command,
        total_duration=total_duration,
        on_progress=on_progress,
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
    )
    return output_path


# 出力直前の保険 (§5.5.3)。未適用なら単独パスで焼き込む。
# 経路が増えても「出力の直前に必ず確認する」この 1 点で守れる。
def ensure_applied(context, video_path, canvas_size, ffmpeg_cfg, total_duration=0.0):
    if not is_required(context):
        return video_path

    from ..blur.config import config            # noqa: PLC0415 (機能 OFF なら読まない)

    output_path = context.allocate_intermediate("blur_applied.mp4")
    result = apply(
        video_path, output_path, context.blur_mask_path, canvas_size,
        config(context.settings), ffmpeg_cfg,
        total_duration=total_duration,
        on_progress=context.progress_subcallback("ぼかし焼き込み"),
    )
    context.blur_applied = True
    return result


# ass / movie フィルタ用にパスをエスケープする (burn_subtitle と同一の 2 段階エスケープ)
def _escape_filter_path(path):
    return str(path).replace("\\", "/").replace(":", "\\:")


# 映像のエンコード設定。音声は copy するため -c:a は含めない。
def _video_encode_options(ffmpeg_cfg):
    options = ffmpeg_runner.build_encode_options(ffmpeg_cfg)
    if "-c:a" in options:
        index = options.index("-c:a")
        options = options[:index] + options[index + 2:]

    return options + ["-pix_fmt", "yuv420p"]
