# 保存済みプロジェクトが参照する素材の復旧 (docs/request/ver3/resolve7.md §3-5 / §5.8)
#
# プロジェクト JSON の本編素材は「ラウドネス正規化後の中間ファイル」を指している。
# 中間ファイルは PipelineContext の一時ディレクトリにあり、実行の終わりに消えるため、
# 保存済みプロジェクトを後から開くと本編クリップが全滅してしまう。
#
# ここで開く前に素材を探し直す。判定順は次のとおり:
#   1. そのまま在る                       → 何もしない (OP/ED・画像は通常ここで終わる)
#   2. プロジェクトからの相対パスで在る    → そのパスを採用 (フォルダごと移した場合)
#   3. 本編素材 (中間ファイル) が無い      → 元動画から作り直す / 元動画を直接使う
#   4. 直前に差し替えたフォルダに同名で在る → そのパスを採用 (2 件目以降を聞き直さない)
#   5. それ以外が無い                     → 再リンクを求める (未注入なら従来どおり無効化)
#
# 3 が成立する根拠: ラウドネス正規化は映像を -c:v copy で通すため時間軸が元動画と
# 完全に同一で、かつ入力と設定が同じなら決定的 (2 パス測定 + 線形適用) である。
# よって元動画から作り直せば、保存時と同じ素材が再現できる (§2.3)。
import os
import tempfile

from ..modules import loudness_normalizer
from ..utils.logger import get_logger
from .builder import timeline_config

_logger = get_logger(__name__)

# 作り直した本編素材のファイル名 (作業ディレクトリ内)
_RECOVERED_NAME = "recovered_source.mp4"


# 素材を開ける状態へ復旧する
# timeline        : project_io.load_project(validate_timeline=False) で読んだもの
# project_path    : 読み込んだプロジェクトファイルのパス (相対パス解決の基準)
# settings        : setting.json
# context         : PipelineContext (進捗と作業ディレクトリに使う。無くても動く)
# relink_callback : 見つからない素材を利用者へ尋ねるフック (GUI 経路のみ)
#                   形式: callback({"media_id","path","kind"}) -> 新しいパス / None
# source_recover  : 呼び出し側が用意する復旧フック (ver3 resolve9 §5.5-b)。
#                   アーカイブ切り抜き用は「VOD から切り直して音声を貼り直す」を
#                   ここへ差し込む。timeline 層が archive 層を import しないための構造。
#                   形式: callback(media, source) -> 復旧したパス / None
# 戻り値: {"recovered": [media_id…], "missing": [media_id…], "renormalized": bool}
def recover(timeline, project_path, settings, context=None, relink_callback=None,
            source_recover=None):
    cfg = timeline_config(settings)["project"]
    source = dict(timeline.source or {})
    body_id = str(source.get("media_id", "") or "")
    result = {"recovered": [], "missing": [], "renormalized": False}
    # 利用者が差し替え先に選んだフォルダ。素材をまとめて別フォルダへ移した場合、
    # 2 件目以降は同じフォルダに同じ名前で見つかるため聞き直さない。
    known_dirs = []
    # どのクリップからも参照されていない素材は、無くても Timeline は成立する。
    # 尋ねても利用者にできることが無いため、差し替えを求めない (ver5 resolve6 §3.3)。
    used = timeline.used_media_ids()

    for media in timeline.media_pool:
        # ① そのまま在る
        if media.path and os.path.exists(media.path):
            continue

        # ①' 誰も使っていない素材 (セクション追加や削除で残った残骸) は飛ばす。
        # 「使わない」にしただけのクリップが参照している素材は使用中に数えるため、
        # 使用可否を戻したときに素材が無い、という事態にはならない。
        if media.id not in used:
            _logger.info("使われていない素材のため復旧しません: %s (%s)",
                         media.id, media.path)
            continue

        # ② プロジェクトファイルからの相対パス
        found = _by_relative_path(media, project_path)
        if found:
            _logger.info("素材を相対パスで見つけました: %s → %s", media.id, found)
            media.path = found
            result["recovered"].append(media.id)
            continue

        # ③ 本編素材 (中間ファイル) の作り直し
        if media.id == body_id:
            recovered, renormalized = _recover_body(source, settings, cfg, context)
            if recovered:
                media.path = recovered
                source["media_path"] = recovered
                timeline.source = source
                result["recovered"].append(media.id)
                result["renormalized"] = result["renormalized"] or renormalized
                continue

        # ③' 呼び出し側が用意した復旧 (アーカイブ用: VOD から切り直す / resolve9 §5.5)
        if source_recover is not None:
            recovered = source_recover(media, source)
            if recovered:
                media.path = recovered
                result["recovered"].append(media.id)
                continue

        # ④ 直前に差し替えたフォルダで同じ名前のファイルを探す
        found = _by_known_dirs(media, known_dirs)
        if found:
            _logger.info("素材を差し替え済みのフォルダで見つけました: %s → %s",
                         media.id, found)
            media.path = found
            result["recovered"].append(media.id)
            continue

        # ⑤ 再リンク (コールバック未注入なら何もしない = 従来どおり validate が無効化する)
        relinked = _ask_relink(media, relink_callback)
        if relinked:
            media.path = relinked
            folder = os.path.dirname(os.path.abspath(relinked))
            if folder not in known_dirs:
                known_dirs.append(folder)
            result["recovered"].append(media.id)
            continue
        _logger.warning("素材が見つかりません: %s (%s)", media.id, media.path)
        result["missing"].append(media.id)

    return result


# プロジェクトファイルからの相対パスで探す (保存時に併記した path_rel を使う)
def _by_relative_path(media, project_path):
    rel = getattr(media, "path_rel", "")
    if not rel or not project_path:
        return None
    candidate = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(project_path)), rel))
    return candidate if os.path.exists(candidate) else None


# 利用者が差し替え先に選んだフォルダに、同じ名前のファイルが無いか探す
def _by_known_dirs(media, known_dirs):
    name = os.path.basename(str(media.path or ""))
    if not name:
        return None
    for folder in known_dirs:
        candidate = os.path.join(folder, name)
        if os.path.exists(candidate):
            return candidate
    return None


# 本編素材を復旧する。戻り値: (パス or None, 作り直したか)
#   ・元動画が無ければ復旧できない (None)
#   ・保存時が中間ファイルでなければ元動画をそのまま使う
#   ・方針が "use_source" なら元動画をそのまま使う (速いが音量が正規化前になる)
#   ・既定 "renormalize" は元動画からラウドネス正規化をやり直す
def _recover_body(source, settings, cfg, context):
    input_path = str(source.get("input_path", "") or "")
    if not input_path or not os.path.exists(input_path):
        _logger.warning("元動画が見つからないため本編素材を復旧できません: %s", input_path)
        return None, False

    if not _was_normalized(source):
        # 正規化が無効・スキップされた実行。元動画がそのまま素材だった。
        _logger.info("本編素材は元動画そのものだったため、そのまま使います: %s", input_path)
        return input_path, False

    if str(cfg["missing_media_policy"]) == "use_source":
        _logger.info("設定に従い元動画をそのまま使います (音量は正規化前): %s", input_path)
        return input_path, False

    output_path = _work_path(context)
    _logger.info("本編素材を元動画から作り直します: %s", input_path)
    on_progress = (context.progress_subcallback("素材の復旧 実行中…")
                   if context is not None else None)
    # 失敗・スキップ時は normalize_file が input_path を返す (処理は止めない / §7)
    result = loudness_normalizer.normalize_file(
        input_path, output_path, settings, on_progress=on_progress)
    return result, result != input_path


# 保存時の本編素材が中間ファイル (正規化後) だったか
# media_role の記録があればそれに従い、無い旧ファイルは media_path と input_path の
# 一致で判定する (resolve7 §3-5 判定 3)。
def _was_normalized(source):
    role = str(source.get("media_role", "") or "")
    if role:
        return role == "normalized"
    media_path = str(source.get("media_path", "") or "")
    input_path = str(source.get("input_path", "") or "")
    if not media_path or not input_path:
        return False
    return os.path.abspath(media_path) != os.path.abspath(input_path)


# 作り直した素材の置き場を決める。
# PipelineContext があれば中間ファイルとして登録し、実行の終わりに一緒に消えるようにする。
def _work_path(context):
    if context is not None and hasattr(context, "allocate_intermediate"):
        return context.allocate_intermediate(_RECOVERED_NAME)
    return os.path.join(tempfile.mkdtemp(prefix="autoedit_recover_"), _RECOVERED_NAME)


# 見つからない素材の差し替えを利用者へ尋ねる (GUI 経路のみ / コールバック未注入なら None)
def _ask_relink(media, relink_callback):
    if not callable(relink_callback):
        return None
    try:
        answer = relink_callback({
            "media_id": media.id,
            "path": media.path,
            "kind": media.kind,
        })
    except Exception:  # noqa: BLE001 (再リンク画面の失敗で読み込みを止めない)
        _logger.exception("素材の再リンクに失敗しました (該当クリップは無効化されます)")
        return None
    if answer and os.path.exists(answer):
        return answer
    return None
