# 合成のフィルタ断片を組み立てる唯一の場所 (ver6 resolve3 §3.1)
#
# 出力 (renderer) とプレビュー (proxy_render / 高精度プレビュー) の両方が
# ここだけを呼ぶ。計算が 2 箇所に分かれると「見たとおりに書き出されない」が
# 必ず再発するため、crop.py と同じ規約 (計算はこのモジュールだけが持つ) を
# 合成全体へ広げたもの (§1.1 / §11-1)。
#
# 【この規約を守るための約束】
#   ・新しい効果を足すときは、必ずここへ関数を足して renderer と
#     proxy_render の両方から呼ぶこと。片方へ直接書いてはならない。
#   ・寸法は引数の CanvasProfile だけから取る。timeline.width / height を
#     直接見ると本番寸法が焼き付き、プロキシが相似にならない。
#     唯一の例外は ASS (PlayRes) で、理由は subtitle_chain に書いた。
#   ・相似であることは tests/test_compositor.py が機械的に検査する (§8.1)。
import os

from ..modules import (
    blur_overlay,
    comment_decor,
    ffmpeg_runner,
    subtitle_generator,
    watermark_overlay,
)
from ..utils.logger import get_logger
from . import crop
from .media_probe import needs_normalize

_logger = get_logger(__name__)

# プロキシ倍率の許容範囲 (§5)。これ以下へ落としても素材のデコードが支配的で速くならず、
# これ以上はプロキシの意味が無い。
PROXY_SCALE_MIN = 0.25
PROXY_SCALE_MAX = 1.0


# ------------------------------------------------------------------
# 合成先のキャンバス
# ------------------------------------------------------------------

# 合成先のキャンバス (本番 / プロキシの違いはこれ 1 つで表す / §3.2)
#
#   width / height    : 合成先の寸法 (偶数)
#   fps               : 合成先の fps。**プロキシでも本番と同じ値を使う**。
#                       軽くしたいのは解像度であって時間軸ではなく、fps を変えると
#                       フレーム量子化の結果が変わって「どのフレームに何が写っているか」
#                       が崩れる (§3.2)。
#   scale             : 本番キャンバスに対する比。**絶対 px の設定値を換算するためだけ**に使う。
#                       ASS は PlayRes が本番寸法のまま libass が追従するので掛けてはならない。
#   production_*      : 本番キャンバスの寸法。絶対 px の設定値 (コメントアイコンなど) は
#                       本番寸法で計算してから scale を掛ける。こうすると「Qt の実測を
#                       使わず推定値だけを見る」という comment_decor の設計意図が
#                       プロキシでも保たれる (§3.3)。
class CanvasProfile:

    def __init__(self, width, height, fps, scale=1.0,
                 production_width=None, production_height=None):
        self.width = int(width)
        self.height = int(height)
        self.fps = fps
        self.scale = float(scale)
        self.production_width = int(production_width or width)
        self.production_height = int(production_height or height)

    # 本番 (出力) のプロファイル
    @staticmethod
    def production(timeline, ffmpeg_cfg):
        return CanvasProfile(timeline.width, timeline.height,
                             ffmpeg_runner.get_output_fps(ffmpeg_cfg))

    # プロキシ (確認用の低解像度) のプロファイル
    @staticmethod
    def proxy(timeline, ffmpeg_cfg, scale):
        ratio = min(max(float(scale), PROXY_SCALE_MIN), PROXY_SCALE_MAX)
        return CanvasProfile(
            _even(timeline.width * ratio), _even(timeline.height * ratio),
            ffmpeg_runner.get_output_fps(ffmpeg_cfg), scale=ratio,
            production_width=timeline.width, production_height=timeline.height)

    # 本番と同じ寸法か (= 出力用か)
    def is_production(self):
        return abs(self.scale - 1.0) < 1e-9

    # 本番キャンバス基準の px を、このキャンバスの px へ換算する
    def px(self, value):
        if self.is_production():
            return int(value)
        return int(round(float(value) * self.scale))

    def size(self):
        return (self.width, self.height)

    def __repr__(self):
        return (f"CanvasProfile({self.width}x{self.height} @{self.fps}fps "
                f"scale={self.scale:g})")


# 偶数へ丸める (yuv420p は奇数の幅・高さを扱えない / crop._even と同じ理由)
def _even(value):
    return max(int(value) // 2 * 2, 2)


# ------------------------------------------------------------------
# ① ソース → キャンバス段 (クロップ・正規化・フェード)
# ------------------------------------------------------------------

# クリップ 1 件をキャンバス寸法へ正規化する映像フィルタ (§4.1)
#
# 正規化チェーンの内容は concat_processor.concat と同一にする。
# プロキシは「ここで縮める」ことで以降のフィルタとエンコードがすべて軽くなる (§3.3)。
def base_filters(timeline, media, clip, profile, fade_sec=0.0,
                 duration=None, settings=None, window=None):
    filters = list(still_filters(timeline, media, profile, settings))

    # 無音カットのフェード設定 (呼び出し側が settings から解決して渡す / renderer §8.2)
    # フェードアウトの開始位置は実際に書き出す尺を基準にする (resolve3 §5.3)
    duration = clip.duration if duration is None else duration
    piece_duration = duration
    fade_in = fade_out = True
    if window is not None:
        # 時間窓 (プロキシのタイル) で切った断片。
        # **フェードの有無はクリップ全体で判断する。** 断片の尺で判断すると、
        # タイルの継ぎ目ごとに暗転が入って出力と食い違う (本書の目的に反する)。
        start = max(float(window[0]), clip.timeline_start)
        end = min(float(window[1]), clip.timeline_end)
        piece_duration = end - start
        duration = clip.duration
        frame = 1.0 / max(float(profile.fps or 1.0), 1.0)
        fade_in = (start - clip.timeline_start) <= frame
        fade_out = (clip.timeline_end - end) <= frame
    if fade_sec > 0 and duration > fade_sec * 2:
        if fade_in:
            filters.append(f"fade=t=in:st=0:d={fade_sec}")
        if fade_out:
            fade_out_start = piece_duration - fade_sec
            filters.append(f"fade=t=out:st={fade_out_start:.3f}:d={fade_sec}")
    return filters


# 素材をキャンバスへ収めるところまでのフィルタ (フェードを含まない)
#
# 停止中の 1 枚絵 (高精度プレビュー) はこちらを使う。クロップを通すため、
# 縦動画プロジェクトでも出力と同じ枠・同じぼかし背景になる (§4.6)。
def still_filters(timeline, media, profile, settings=None):
    # 縦動画の切り抜き (ver5 resolve9 §5.5)。指定が無ければ None が返り、
    # 以降は従来どおりの正規化になる = コマンドが 1 文字も変わらない (R15)。
    chain = crop_chain(timeline, media, profile, settings)
    if chain:
        # 切り抜きが正規化を兼ねる (キャンバス寸法・fps・画素形式まで揃えて出す)
        return [chain]
    if not needs_normalize(media, profile.width, profile.height, profile.fps):
        return []
    _warn_normalize_once(media, profile)
    return [
        f"scale={profile.width}:{profile.height}:force_original_aspect_ratio=decrease",
        f"pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        f"fps={profile.fps}",
        "format=yuv420p",
    ]


# 縦動画の切り抜きのフィルタを返す (ver5 resolve9 §5.5 / 旧 renderer._crop_chain)
#
# 指定が無い / 別の素材に対する指定 / 枠が素材の外を指している場合は None を返し、
# 従来の正規化へ落とす。開けない・書き出せないプロジェクトを作らないため (§4-4)。
# boxblur の半径は絶対 px なので profile.scale を渡して換算する (§3.3)。
def crop_chain(timeline, media, profile, settings=None):
    layout = crop.load(timeline)
    if layout is None:
        return None
    if not crop.is_valid(layout, media, profile.width, profile.height):
        _logger.warning("切り抜きの指定が素材と噛み合わないため、従来の正規化で出力します")
        return None

    # 背景の種類は指定に入っている (作ったときの見え方を保つ)。
    # ぼかしの強さだけは設定から読む。
    cfg = crop.config(settings or {})
    cfg["background"] = str(layout.get("background") or cfg["background"])
    return crop.filter_chain(layout, profile.width, profile.height, profile.fps, cfg,
                             scale=profile.scale)


# 正規化を適用した旨を 1 回だけ通知する (§11 ログ方針)
_warned_normalize = set()


def _warn_normalize_once(media, profile):
    key = (media.id, profile.width, profile.height, profile.fps)
    if key in _warned_normalize:
        return
    _warned_normalize.add(key)
    _logger.info(
        "素材を正規化: %s (%dx%d %.3gfps → %dx%d %sfps)",
        os.path.basename(media.path), media.width, media.height, media.fps or 0,
        profile.width, profile.height, profile.fps,
    )


# ------------------------------------------------------------------
# ② キャンバス上段 (オーバーレイ・字幕・アイコン・ぼかし・透かし)
# ------------------------------------------------------------------

# オーバーレイ 1 件ぶんの断片を返す (§4.1 / 旧 renderer._composite の組み立て)
#
#   in_label    : つなぐ先の入力ラベル
#   input_index : この素材が filtergraph で何番目の入力になるか
#   step        : 中間ラベルの連番 (呼び出し側が通して管理する)
#   with_enable : False なら表示時間 (enable) と PTS ずらしを付けない = 1 枚絵モード。
#                 高精度プレビュー (停止中の 1 フレーム) がこちらを使う。
#   still_sec   : 1 枚絵モードで切り出す Timeline 時刻 (with_enable=False のとき必須)
#   time_offset : 時間軸の原点をずらす秒数。プロキシのタイルは Timeline の途中から
#                 始まるため、enable と setpts をタイル開始ぶん引く (§3.5)。
#   window      : (開始, 終了) の時間窓。タイルの外へ出た区間は切り落とし、
#                 素材の読み出し位置も窓の始まりへ合わせる (§3.5)。
#                 省略時はクリップ全体を使う = 従来と完全に同じ文字列。
#   prefix      : 中間ラベルの接頭辞 (1 つの filtergraph で衝突させないため)
#
# 戻り値 (入力引数, フィルタ断片, 出力ラベル, 次の step)
def overlay_chain(clip, media, profile, in_label, input_index, step,
                  with_enable=True, still_sec=None, time_offset=0.0,
                  window=None, prefix=""):
    # 窓の中で実際に見える区間 (窓が無ければクリップ全体)
    visible_start = clip.timeline_start
    visible_end = clip.timeline_end
    if window is not None:
        visible_start = max(visible_start, float(window[0]))
        visible_end = min(visible_end, float(window[1]))

    input_args = []
    if media.is_image():
        if with_enable:
            input_args += ["-loop", "1", "-framerate", str(profile.fps),
                           "-t", f"{visible_end - time_offset:.3f}",
                           "-i", media.path]
        else:
            input_args += ["-i", media.path]
    elif with_enable:
        if window is None:
            source_in, length = clip.source_in, clip.duration
        else:
            # 窓の始まりに合わせて素材の読み出し位置を進める (入力シークを効かせる)
            source_in = clip.source_in + (visible_start - clip.timeline_start)
            length = visible_end - visible_start
        input_args += ["-ss", f"{source_in:.3f}",
                       "-t", f"{length:.3f}", "-i", media.path]
    else:
        # 1 枚絵: その時刻に写っているコマだけを取る
        offset = max(float(still_sec or 0.0) - clip.timeline_start, 0.0)
        input_args += ["-ss", f"{clip.source_in + offset:.3f}", "-i", media.path]

    overlay_label = f"[{prefix}ov{step}]"
    # 拡大率をキャンバス比で解決する (transform.scale = キャンバス幅に対する比率)
    target_w = max(int(profile.width * float(clip.transform.scale or 1.0)), 2)
    scale_chain = f"scale={target_w}:-2"
    if float(clip.transform.opacity or 1.0) < 1.0:
        scale_chain += (f",format=rgba,colorchannelmixer="
                        f"aa={float(clip.transform.opacity):.3f}")
    if with_enable and not media.is_image():
        # 動画オーバーレイは Timeline 上の開始位置へ PTS をずらす
        scale_chain += f",setpts=PTS+{visible_start - time_offset:.3f}/TB"
    chains = [f"[{input_index}:v]{scale_chain}{overlay_label}"]

    # 正規化座標 (-1〜1・Y は上が正) を overlay の左上座標へ変換する
    pos_x = (float(clip.transform.x or 0.0) + 1.0) * profile.width / 2.0
    pos_y = (1.0 - float(clip.transform.y or 0.0)) * profile.height / 2.0
    label = f"[{prefix}v{step}]"
    overlay = f"overlay={pos_x:.1f}-w/2:{pos_y:.1f}-h/2"
    if with_enable:
        enable = (f"between(t,{visible_start - time_offset:.3f},"
                  f"{visible_end - time_offset:.3f})")
        overlay += f":enable='{enable}'"
    chains.append(f"{in_label}{overlay_label}{overlay}{label}")
    return input_args, chains, label, step + 1


# 実効字幕設定と FontProfile を解決する (縦動画の上書きを反映する)
def subtitle_profile(context):
    settings = context.settings
    profile = getattr(context, "output_profile", None)
    eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
        settings.get("subtitle", {}), settings.get("vertical", {}), profile)
    return eff_cfg, subtitle_generator.build_font_profile(eff_cfg)


# 字幕 1 グループぶんの (ass パス, items, ass フィルタ断片) を返す (§4.1)
#
# 【重要】**CanvasProfile を受け取らない。** ASS の PlayRes は本番寸法のままにし、
#   libass が PlayRes → 実フレーム寸法へ自動で合わせるのに任せる (§3.2 / §1.4)。
#   こうするとプロキシでもフォントサイズ・余白・行送りが完全に相似になる。
#   ここへ scale を持ち込むと二重に縮んで字だけ小さくなるため、
#   引数として受け取れないようにしてある。
def subtitle_chain(timeline, context, clips, name, fonts_dir=None):
    eff_cfg, font_profile = subtitle_profile(context)
    items = [clip.to_item() for clip in clips if clip.use]
    ass_path, option = subtitle_chain_for_items(
        timeline, items, eff_cfg, font_profile,
        context.allocate_intermediate(name), fonts_dir)
    return ass_path, items, option


# items から ASS を書き出して (パス, ass フィルタ断片) を返す
#
# PipelineContext を持たない呼び出し (停止中の 1 枚絵プレビューなど) 向け。
# PlayRes を本番寸法のままにするのは subtitle_chain と同じ理由 (§3.2)。
def subtitle_chain_for_items(timeline, items, eff_cfg, font_profile, ass_path,
                             fonts_dir=None):
    subtitle_generator.build_subtitle_file(
        items, font_profile, ass_path,
        video_width=timeline.width, video_height=timeline.height,
        # コメントの背景 (角丸の箱) を出すため実効字幕設定を渡す (resolve11 §5.11-5)
        subtitle_cfg=eff_cfg,
    )
    return ass_path, ass_option(ass_path, fonts_dir)


# ass フィルタの指定文字列を組み立てる (fontsdir の付与もここへ寄せる / §4.1)
def ass_option(ass_path, fonts_dir=None):
    option = f"ass='{escape_filter_path(ass_path)}'"
    if fonts_dir and os.path.isdir(fonts_dir):
        option += f":fontsdir='{escape_filter_path(fonts_dir)}'"
    return option


# ass / subtitles フィルタと同じ 2 段階エスケープ ('\'→'/', ':'→'\:')
def escape_filter_path(path):
    return str(path).replace("\\", "/").replace(":", "\\:")


# コメントアイコンの断片 (§4.1)
#
# 配置の計算は**本番キャンバスで行い**、結果へ profile.scale を掛ける (§3.3)。
# font_size / comment_margin_l / comment_icon_size_px などが絶対 px のため、
# プロキシ寸法で計算すると余白だけが相対的に広がってしまう。
# 戻り値 (chains, 件数, 位置の種類数)
def icon_chains(items, eff_cfg, profile, in_label, out_label,
                with_enable=True, prefix="cic"):
    return comment_decor.build_icon_chains(
        items, eff_cfg, profile.production_width, profile.production_height,
        in_label=in_label, out_label=out_label, with_enable=with_enable,
        prefix=prefix, scale=profile.scale)


# ぼかしの断片 (§4.1)。マスクが無ければ空リストを返す。
#
# 呼び出し側はチェーンの**先頭**へ足すこと。後ろへ足すと字幕・アイコン・
# オーバーレイ素材までぼける (ver5 resolve2 R8)。
#   mask_path : マスクの PNG / 動画。出力は 1 本を使い回し、停止中の 1 枚絵は
#               そのフレームぶんを作って渡す。空なら何もしない。
#   seek_point: マスクの読み出し開始位置 (秒)。タイルの途中から作るとき、
#               マスクも同じだけ進めないとぼかす位置がずれる (§3.5)。
#
# 【設計】context ではなく settings を受け取る。プレビューは PipelineContext を
#   持たないため、context を要求すると結局プレビュー側に別実装が生えてしまう (§1.1)。
#   「もう焼いたか」(context.blur_applied) の判定は出力の多パス構成だけの都合なので
#   呼び出し側 (renderer) に残す。
# 戻り値 (chains, 出力ラベル)
def blur_chains(settings, mask_path, profile, in_label, out_label, prefix="bl",
                seek_point=0.0):
    if not mask_path:
        return [], in_label

    from ..blur.config import config            # noqa: PLC0415 (機能 OFF なら読まない)

    return blur_overlay.build_chains(
        mask_path, in_label, out_label, profile.width, profile.height,
        config(settings), prefix=prefix, seek_point=seek_point)


# 透かしの断片 (§4.1 / ver5 resolve §3.1)
#
# 呼び出し側はチェーンの**最後**へ足すこと。ぼかしと逆で、字幕・アイコン・
# オーバーレイの**上**へ重ねる必要がある。
#   required : 透かしが要るか。**呼び出し側が決める。**
#              出力は予約の応答 (watermark_overlay.is_required) で決まり、
#              プレビューは予約前なので残高からの先読みで決まる (§4.5)。
#              どちらの判定もここへ持ち込まない (判定の置き場を増やさないため)。
# 戻り値 (入力引数, chains, 出力ラベル)。要らない / 素材が無ければ ([], [], in_label)。
def watermark_chain(settings, profile, in_label, out_label, input_index,
                    required=False):
    if not required:
        return [], [], in_label

    args, chains, label = watermark_overlay.build_chain(
        in_label, out_label, profile.width, profile.height,
        watermark_overlay.config(settings), input_index)
    if not chains:
        # 素材が無い。保険の単独パスでも同じ結果になるため、ここでは足さない。
        return [], [], in_label
    return list(args or []), chains, label
