# 出力プロファイル解決モジュール (縦動画対応 / request14 / resolve14)
# 入力動画の表示寸法から向き(縦/横)を判定し、以降の全工程が参照する
# 出力プロファイル(キャンバス寸法・縦フラグ)を1回だけ解決する。
# 横(landscape)は既存挙動=1920x1080、縦(portrait)は YouTube Shorts / TikTok 向けの
# 9:16 キャンバスを採用する。判定・解析に失敗した場合は安全側=横として続行する。
import copy

from ..exceptions import AutoEditError
from ..utils.logger import get_logger
from . import ffmpeg_runner

_logger = get_logger(__name__)

# 実行用の設定コピーにだけ現れるキー (ver11 resolve2 R1)
#
# apply_canvas_to_settings が書き戻すのは「その実行だけの解決値」であり、
# setting.json へ保存してはならない。保存してしまうと、別の素材向けに解決した
# 寸法が上限枠として焼き付き、以降の出力がそこまで縮む
# (実測: 縦素材 1 本で 606x1080 が焼き付き、1920x1080 の VOD が 606x340 になった)。
#
# 上書き前の保存値を OUTPUT_CANVAS_BACKUP_KEY へ控え、保存の出口
# (settings_window.save_settings → without_runtime_canvas) が元の値へ戻して書き出す。
OUTPUT_CANVAS_BACKUP_KEY = "output_canvas_saved"
# 解決値で上書きするキー (控えと復元の対象)
OUTPUT_CANVAS_KEYS = ("output_width", "output_height", "output_fps")
# テロップの絶対 px が前提にしている基準の高さ (実行時にだけ足すキー)
OUTPUT_REFERENCE_KEY = "output_reference_height"

# 縦動作が無効/失敗時のフォールバック既定 (横=landscape の標準寸法)
_DEFAULT_LANDSCAPE_WIDTH = 1920
_DEFAULT_LANDSCAPE_HEIGHT = 1080
# 縦(portrait)の既定キャンバス (9:16 / §8-1 確定: 1080x1920 固定)
_DEFAULT_PORTRAIT_WIDTH = 1080
_DEFAULT_PORTRAIT_HEIGHT = 1920


# 横(landscape)プロファイルを生成する
# 横は既存の ffmpeg.output_width/height をそのまま用い、挙動を変えない。
# fps を戻り値へ足す (resolve3 R2)。既存キーは 1 つも変えない。
def _landscape_profile(ffmpeg_cfg, width=None, height=None, fps=None):
    out_w = int(ffmpeg_cfg.get("output_width", _DEFAULT_LANDSCAPE_WIDTH))
    out_h = int(ffmpeg_cfg.get("output_height", _DEFAULT_LANDSCAPE_HEIGHT))
    profile = {
        "is_portrait": False,
        "orientation": "landscape",
        "width": int(width if width else out_w),
        "height": int(height if height else out_h),
        "fps": int(fps if fps else ffmpeg_runner.get_output_fps(ffmpeg_cfg)),
    }
    _attach_reference(profile, reference_height(ffmpeg_cfg))
    return profile


# 縦(portrait)プロファイルを生成する
# キャンバス寸法は vertical.output_width/height (既定 1080x1920) から取得する。
def _portrait_profile(vertical_cfg, ffmpeg_cfg=None, fps=None):
    out_h = int(vertical_cfg.get("output_height", _DEFAULT_PORTRAIT_HEIGHT))
    profile = {
        "is_portrait": True,
        "orientation": "portrait",
        "width": int(vertical_cfg.get("output_width", _DEFAULT_PORTRAIT_WIDTH)),
        "height": out_h,
        # 縦は寸法を固定するが fps だけは素材等倍に従う (resolve3 R2)
        "fps": int(fps if fps else ffmpeg_runner.get_output_fps(ffmpeg_cfg or {})),
    }
    _attach_reference(profile, out_h)
    return profile


# テロップの絶対 px が前提にしている「基準の高さ」を返す (resolve3 R2 の付帯対応)
#
# 通常は ffmpeg.output_height (= 利用者が font_size を調整したときに見ていた高さ)。
# apply_canvas_to_settings が output_height を解決値で上書きするため、
# 上書き前の値を output_reference_height へ控えてある場合はそちらを使う。
# このキーは実行用の設定コピーにだけ現れ、setting.json へは保存されない。
def reference_height(ffmpeg_cfg):
    cfg = ffmpeg_cfg or {}
    try:
        reference = int(cfg.get(OUTPUT_REFERENCE_KEY) or 0)
    except (TypeError, ValueError):
        reference = 0
    if reference > 0:
        return reference
    try:
        return int(cfg.get("output_height", _DEFAULT_LANDSCAPE_HEIGHT))
    except (TypeError, ValueError):
        return _DEFAULT_LANDSCAPE_HEIGHT


# 基準キャンバス (テロップの絶対 px が前提にしている寸法) をプロファイルへ足す
# (resolve3 R2 の付帯対応)。
#
# font_size / margin / comment_icon_size_px は**絶対 px** で、ASS の PlayRes を
# 実キャンバスに合わせると 1080p 未満の素材で文字だけが大きくなる。
# PlayRes を基準キャンバスへ固定し、libass の相似変換に任せれば見た目が一致する
# (編集画面のプロキシが既に使っている手口 / timeline/compositor.py の subtitle_chain)。
#
#   scale       = 実キャンバスの高さ / 基準の高さ  (1080p 以上では 1.0)
#   reference_h = 基準の高さ (= 設定の output_height)
#   reference_w = 実キャンバスと同じアスペクト比になる幅
def _attach_reference(profile, reference_height):
    height = int(profile["height"])
    width = int(profile["width"])
    ref_h = int(reference_height or 0)
    if ref_h <= 0 or height <= 0 or height >= ref_h:
        # 等倍以上 (縮めていない) なら基準は実キャンバスそのもの = 従来と同一の ASS
        profile["reference_width"] = width
        profile["reference_height"] = height
        profile["scale"] = 1.0
        return profile
    scale = height / float(ref_h)
    profile["reference_width"] = _even(width / scale)
    profile["reference_height"] = ref_h
    profile["scale"] = scale
    return profile


# 偶数へ丸める (yuv420p は奇数の幅・高さを扱えない / compositor._even と同じ規則)
def _even(value):
    return max(int(value) // 2 * 2, 2)


# 入力動画から出力プロファイルを解決する (パイプライン開始時に1回だけ呼ぶ)
# 判定基準:
#   ・vertical.enabled=false        → 常に横 (縦対応の完全無効化)
#   ・height > width               → 縦 (portrait)
#   ・それ以外(正方形含む / §8-3)  → 横 (landscape)
# ffprobe 失敗時は横として続行し、パイプラインを止めない (§6 フォールバック)。
def resolve_output_profile(input_path, settings):
    ffmpeg_cfg = settings.get("ffmpeg", {})
    vertical_cfg = settings.get("vertical", {})

    # 縦対応が無効なら判定せず横で確定する
    if not vertical_cfg.get("enabled", True):
        _logger.info("縦動画対応が無効 (vertical.enabled=false) のため横で処理します")
        return _landscape_profile(ffmpeg_cfg)

    try:
        width, height = ffmpeg_runner.probe_dimensions(input_path, ffmpeg_cfg)
    except AutoEditError as e:
        # 寸法取得失敗時は安全側=横で続行する
        _logger.warning("動画寸法の取得に失敗したため横として処理します: %s", e)
        return _landscape_profile(ffmpeg_cfg)

    # 高さが幅を上回る場合のみ縦。正方形(等値)は横扱い (§8-3)。
    if height > width:
        profile = _portrait_profile(vertical_cfg, ffmpeg_cfg)
        _logger.info(
            "縦動画と判定 (入力=%dx%d → 出力=%dx%d)",
            width, height, profile["width"], profile["height"],
        )
        return profile

    _logger.info("横動画と判定 (入力=%dx%d)", width, height)
    return _landscape_profile(ffmpeg_cfg)


# ------------------------------------------------------------------
# 出力キャンバスの解決 (resolve3 R2)
# ------------------------------------------------------------------

# 素材の表示寸法・fps から出力キャンバスを決める (resolve3 R2)
#
# ffmpeg.output_scale_mode:
#   "source" : 素材等倍。拡大もフレーム複製もしない (推奨)
#   "fixed"  : 従来どおり output_width/height/output_fps をそのまま使う
#
# "source" でも output_width/height/output_fps は**上限**として効く。
# 上限を超える素材はアスペクト比を保ったまま枠へ収めて縮小し、拡大は決して行わない。
#   2560x1440 (2K) → 1920x1080 (= 現状と同一) / 1280x720 → 1280x720 (等倍)
#
# 戻り値: {"width","height","fps","mode","source_width","source_height","source_fps"}
def resolve_canvas(input_path, settings):
    ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
    out_w = int(ffmpeg_cfg.get("output_width", _DEFAULT_LANDSCAPE_WIDTH))
    out_h = int(ffmpeg_cfg.get("output_height", _DEFAULT_LANDSCAPE_HEIGHT))
    out_fps = ffmpeg_runner.get_output_fps(ffmpeg_cfg)
    mode = str(ffmpeg_cfg.get("output_scale_mode", "source") or "source").strip().lower()

    fixed = {"width": out_w, "height": out_h, "fps": out_fps, "mode": "fixed",
             "source_width": None, "source_height": None, "source_fps": None}
    if mode != "source":
        _logger.info("出力キャンバス: 固定 %dx%d@%dfps (output_scale_mode=%s)",
                     out_w, out_h, out_fps, mode)
        return fixed

    try:
        src_w, src_h = ffmpeg_runner.probe_dimensions(input_path, ffmpeg_cfg)
    except AutoEditError as e:
        _logger.warning(
            "素材の寸法を取れないため固定キャンバス %dx%d@%dfps を使います: %s",
            out_w, out_h, out_fps, e)
        return fixed

    src_fps = _probe_fps(input_path, ffmpeg_cfg)
    # 上限枠は素材の向きに合わせる (ver11 resolve2 R4)。
    # 縦素材を横の枠 (1920x1080) で収めると高さ側が支配して極端に縮む
    # (実測: 1278x2272 → 606x1080)。縦の出力キャンバスは vertical.output_* なので
    # 上限もそちらで見る。横・正方形は従来どおり ffmpeg.output_* を使う。
    limit_w, limit_h = out_w, out_h
    vertical_cfg = (settings or {}).get("vertical", {}) or {}
    if src_h > src_w and vertical_cfg.get("enabled", True):
        limit_w = int(vertical_cfg.get("output_width", _DEFAULT_PORTRAIT_WIDTH))
        limit_h = int(vertical_cfg.get("output_height", _DEFAULT_PORTRAIT_HEIGHT))
    # 上限枠へ収める倍率 (1.0 を超えない = 拡大しない)
    ratio = min(1.0,
                limit_w / float(src_w) if src_w > 0 else 1.0,
                limit_h / float(src_h) if src_h > 0 else 1.0)
    width = _even(src_w * ratio)
    height = _even(src_h * ratio)
    if width < 2 or height < 2:
        # 解決後の寸法が異常なら固定へ落とす (安全側)
        _logger.warning("解決した出力寸法が不正 (%sx%s) のため固定キャンバスを使います",
                        width, height)
        return fixed

    fps = _round_fps(src_fps, ffmpeg_cfg)
    fps = min(fps, out_fps) if fps > 0 else out_fps
    _logger.info(
        "出力キャンバス: 素材等倍 %dx%d@%dfps (素材 %dx%d@%sfps / 上限 %dx%d@%dfps)",
        width, height, fps, src_w, src_h,
        f"{src_fps:.4g}" if src_fps else "不明", limit_w, limit_h, out_fps)
    return {"width": width, "height": height, "fps": fps, "mode": "source",
            "source_width": src_w, "source_height": src_h, "source_fps": src_fps}


# 解決したキャンバスを「実行用の設定コピー」へ書き戻す (resolve3 R2)
#
# ffmpeg.output_width/height/output_fps を読む箇所は 15 以上あるため、
# 入口で 1 回だけ解決してコピーへ書き戻す。これで既存の読み手は 1 行も変えずに解決値を見る。
# setting.json の保存値は変えない (deepcopy するため)。
# canvas を直接渡すと解決を飛ばせる (保存済みプロジェクトの再編集用)。
def apply_canvas_to_settings(settings, input_path=None, canvas=None):
    if canvas is None:
        if not input_path:
            return settings
        canvas = resolve_canvas(input_path, settings)
    resolved = copy.deepcopy(settings or {})
    ffmpeg_cfg = resolved.setdefault("ffmpeg", {})
    # 上書き前の高さを基準として控える (テロップの絶対 px の拠り所 / R2 付帯対応)。
    # 2 回通しても基準がずれないよう、既に控えてあれば残す。
    ffmpeg_cfg.setdefault(OUTPUT_REFERENCE_KEY, reference_height(ffmpeg_cfg))
    # 上書き前の保存値を控える (ver11 resolve2 R1)。
    # このコピーが画面から保存されても、保存の出口で元の値へ戻せるようにする。
    # 2 回通しても最初の控えを残す (setdefault)。
    ffmpeg_cfg.setdefault(
        OUTPUT_CANVAS_BACKUP_KEY,
        {key: ffmpeg_cfg[key] for key in OUTPUT_CANVAS_KEYS if key in ffmpeg_cfg})
    ffmpeg_cfg["output_width"] = int(canvas["width"])
    ffmpeg_cfg["output_height"] = int(canvas["height"])
    ffmpeg_cfg["output_fps"] = int(canvas["fps"])
    # Timeline の軸はここで書き戻した output_fps から作られる
    # (timeline/builder.build と archive/timeline_builder.build_archive_timeline が
    #  どちらも ffmpeg.output_fps を読む)。別のキーを増やす必要はない。
    return resolved


# ------------------------------------------------------------------
# 実行時の解決値を保存値から外す (ver11 resolve2 R2 / R3)
# ------------------------------------------------------------------

# 保存してよい設定 (実行時の解決値を含まないもの) を返す (ver11 resolve2 R2)
#
# settings_window.save_settings だけがこれを使う。**保存の出口 1 か所で守る**のが要点で、
# save_settings を呼ぶ画面が増えても (現在 7 か所)、解決済みコピーを渡された時点で
# 保存値が壊れることはなくなる。
#
# 実行時キーが 1 つも無ければ元のオブジェクトをそのまま返す = 従来と完全に同一。
# 含むときはコピーを直して返し、**渡された設定は書き換えない**
# (実行中の工程が見ている解決値を壊さないため)。
def without_runtime_canvas(settings):
    if not isinstance(settings, dict):
        return settings
    ffmpeg_cfg = settings.get("ffmpeg")
    if not isinstance(ffmpeg_cfg, dict):
        return settings
    if OUTPUT_CANVAS_BACKUP_KEY not in ffmpeg_cfg and OUTPUT_REFERENCE_KEY not in ffmpeg_cfg:
        return settings
    cleaned = copy.deepcopy(settings)
    restore_runtime_canvas(cleaned.get("ffmpeg"))
    return cleaned


# ffmpeg 設定から実行時の解決値を取り除き、保存値へ戻す (ver11 resolve2 R2 / R3)
# 受け取った辞書をその場で直す。直したら True を返す (移行処理が保存要否の判断に使う)。
#
# 控えが無いのに基準の高さだけがある設定は、**控えを持たない版が焼き付けた保存値**である
# (ver11 resolve2 §3 R3)。元の幅は残っていないため、基準の高さと既定の縦横比から戻す。
def restore_runtime_canvas(ffmpeg_cfg):
    if not isinstance(ffmpeg_cfg, dict):
        return False
    has_backup = OUTPUT_CANVAS_BACKUP_KEY in ffmpeg_cfg
    has_reference = OUTPUT_REFERENCE_KEY in ffmpeg_cfg
    if not has_backup and not has_reference:
        return False

    backup = ffmpeg_cfg.pop(OUTPUT_CANVAS_BACKUP_KEY, None)
    reference = ffmpeg_cfg.pop(OUTPUT_REFERENCE_KEY, None)
    if isinstance(backup, dict) and backup:
        for key in OUTPUT_CANVAS_KEYS:
            if backup.get(key) is not None:
                ffmpeg_cfg[key] = backup[key]
        return True

    # 控えを持たない版が焼き付けた寸法を、基準の高さから復元する
    try:
        height = int(reference or 0)
    except (TypeError, ValueError):
        height = 0
    if height > 0:
        ffmpeg_cfg["output_height"] = height
        ffmpeg_cfg["output_width"] = _even(
            height * _DEFAULT_LANDSCAPE_WIDTH / float(_DEFAULT_LANDSCAPE_HEIGHT))
        _logger.info(
            "保存されていた出力キャンバスの解決値を復元しました: %dx%d",
            ffmpeg_cfg["output_width"], ffmpeg_cfg["output_height"])
    return True


# 素材の fps を ffprobe で取る (取れなければ 0.0 / resolve3 R2)
def _probe_fps(input_path, ffmpeg_cfg):
    try:
        params = ffmpeg_runner.probe_stream_params(input_path, ffmpeg_cfg)
    except AutoEditError:
        _logger.debug("素材の fps を取れませんでした (設定値を使います)", exc_info=True)
        return 0.0
    raw = str(params.get("fps") or "")
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            num, den = float(num), float(den)
        except ValueError:
            return 0.0
        return num / den if den else 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


# 素材の fps を標準値へ丸める (resolve3 R2)
#
# 実測ログに 28.8fps が出ている。非整数の fps をそのまま使うと
# CFR 化 (-fps_mode cfr -r <fps>) の前提が崩れるため、最も近い標準値を選ぶ。
# 候補 (ffmpeg.output_fps_standards) が空なら丸めず素材値を四捨五入して使う。
def _round_fps(source_fps, ffmpeg_cfg):
    if not source_fps or source_fps <= 0:
        return 0
    candidates = ffmpeg_cfg.get("output_fps_standards")
    if candidates is None:
        candidates = [24, 25, 30, 50, 60]
    values = []
    for value in candidates:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            values.append(number)
    if not values:
        return max(int(round(source_fps)), 1)
    return min(values, key=lambda v: abs(v - source_fps))
