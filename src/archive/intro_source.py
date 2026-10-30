# イントロカードの素を「足りないぶんだけ」用意する (ver9 resolve12 §3.3〜§3.5)
#
# 背景:
#   イントロカード (ブラー+黒帯+中央テーマ) はクリップ開始の buffer_sec 秒**手前**の
#   映像から作る。この区間はクリップ素材 (vod_start 始まり) に入っていないため、
#   素は「元 VOD」か「保存しておいた控え (<media_id>_intro.mp4)」のどちらかしかない。
#   元 VOD は既定で自動整理され (resolve.md §6-6)、控えは保存時に作る (§9-7) が、
#   その保存フックが配線されていなかったため**どちらも無い**プロジェクトが生まれた。
#
# ここでやること:
#   書き出しの直前に、**テーマが入っているセクションだけ**を見て、素が無ければ
#   その区間を twitch-dl で取り直して控えを作る。取得範囲はイントロカードの設計
#   そのもの ([開始 - buffer_sec, 開始]) で、1 セクションあたり 3〜6MB で済む。
#
# 通信するのは次をすべて満たすセクションだけである:
#   ・使用する (use=True) / テーマが入っている
#   ・控えがまだ無い / 元 VOD も手元に無い
#   ・取得元 (video_id) が分かっていて、取り直しが設定で有効
#
# 作った控えは <プロジェクト>.media/ へ残すため、次回の書き出しでは取得が起きない。
import os

from ..exceptions import DiskSpaceError, PipelineCancelled
from ..i18n import tr
from ..modules import ffmpeg_runner
from ..utils.logger import get_logger
from . import clip_writer, config, vod_range
from . import timeline_builder as archive_timeline

_logger = get_logger(__name__)

# 見送った理由 (ログとテストで使う。利用者へは出さない)
SKIP_NO_BUFFER = "no_buffer"        # VOD の先頭すぎて手前を確保できない
SKIP_NO_SOURCE = "no_source"        # 取得元が分からない / 取り直しが無効
SKIP_FAILED = "failed"              # 取得・切り出しに失敗した


# テーマが入っていて素が要るセクションを選ぶ。
# テーマの有無は編集画面の結果 (edited) が正。edited が無い経路 (CLI・テスト) では
# 準備データ側の theme へ落ちる。
def _targets(prepared, edited):
    themes = {}
    for row in (edited or []):
        if not isinstance(row, dict) or not row.get("use", True):
            continue
        themes[row.get("index")] = str(row.get("theme", "") or "").strip()
    targets = []
    for entry in (prepared or []):
        index = entry.get("index")
        theme = (themes.get(index) if edited
                 else str(entry.get("theme", "") or "").strip())
        if not theme:
            continue
        targets.append(entry)
    return targets


# 同梱 ffmpeg のディレクトリ (twitch-dl は VOD の扱いに ffmpeg を PATH から探す)
def _ffmpeg_dir(ffmpeg_cfg):
    try:
        return os.path.dirname(ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg or {}))
    except Exception:  # noqa: BLE001 (見つからなくても取得は試せる)
        return None


# 控えの置き場。プロジェクトの素材フォルダがあればそこへ残す (次回は取得しない)。
# 保存していないプロジェクトでは作業フォルダへ置く = その実行かぎりになる。
def _dest_path(timeline, index, media_dir, workdir):
    folder = media_dir or workdir
    if not folder:
        return ""
    media_id = archive_timeline.intro_media_id(timeline, index)
    if media_id:
        return clip_writer.intro_source_path(folder, media_id)
    return os.path.join(folder, f"intro_{index}.mp4")


# テーマが入っているセクションのイントロ素材を、足りないぶんだけ用意する。
#
# prepared   : rebuild_prepared の結果。作れたものは intro_source を埋めて返す
# edited     : 編集画面の結果 [{"index","theme","use"}]
# timeline   : 取得元 (vod_source) と素材の控えを引くため
# media_dir  : <プロジェクト>.media/ (空なら workdir へ置く)
# workdir    : 取得素材の一時置き場
# cancel_cb  : 中止を問い合わせる callable (区間の切れ目で効く)
# 戻り値: {"filled": 用意できた件数, "fetched": 取得した件数,
#          "skipped": [(index, 理由)]}
def ensure_intro_sources(prepared, edited, timeline, settings, media_dir="",
                         workdir="", ffmpeg_cfg=None, progress_cb=None,
                         cancel_cb=None):
    summary = {"filled": 0, "fetched": 0, "skipped": []}
    card = config.intro_card_config(settings)
    if not card["enabled"] or card["buffer_sec"] <= 0:
        return summary                  # イントロカードを使わない設定
    targets = _targets(prepared, edited)
    if not targets:
        return summary                  # テーマが 1 件も入っていない = 通信しない

    # 元 VOD が手元にあるなら書き出しが直接切れる (取りに行かない)
    vod = archive_timeline.recorded_vod_path(timeline)
    if vod and os.path.exists(vod):
        return summary

    ffmpeg_cfg = ffmpeg_cfg if ffmpeg_cfg is not None else (settings or {}).get("ffmpeg", {})
    refetch = config.section_add_config(settings)["refetch"]
    source = archive_timeline.resolve_vod_source(timeline, settings)
    ffmpeg_dir = _ffmpeg_dir(ffmpeg_cfg)
    total = len(targets)

    for pos, entry in enumerate(targets, 1):
        if cancel_cb is not None and cancel_cb():
            _logger.info("イントロ素材の用意を中止しました (%d/%d)", pos - 1, total)
            break
        index = entry.get("index")
        kept = str(entry.get("intro_source", "") or "")
        if kept and os.path.exists(kept):
            summary["filled"] += 1
            continue                    # 控えが既にある
        start = float(entry.get("start", 0.0))
        if start < card["buffer_sec"]:
            # VOD の先頭すぎて手前を確保できない (元 VOD があっても作れない区間)
            summary["skipped"].append((index, SKIP_NO_BUFFER))
            continue
        if not refetch["enabled"] or not source["video_id"]:
            summary["skipped"].append((index, SKIP_NO_SOURCE))
            continue
        dest = _dest_path(timeline, index, media_dir, workdir)
        if not dest:
            summary["skipped"].append((index, SKIP_NO_SOURCE))
            continue

        def phase(message, _pos=pos):
            if progress_cb:
                progress_cb((_pos - 1) / total,
                            tr("イントロ用の映像を取得しています… ({pos}/{total}) {phase}",
                               pos=_pos, total=total, phase=str(message)))

        if progress_cb:
            progress_cb((pos - 1) / total,
                        tr("イントロ用の映像を取得しています… ({pos}/{total}) {phase}",
                           pos=pos, total=total, phase=tr("準備中")))
        try:
            # 取得範囲はイントロカードが使う区間そのもの。余裕 (pad) は
            # fetch_range が自前で足すため、ここでは要求区間だけを渡す。
            fetched = vod_range.fetch_range(
                source["video_id"], start - card["buffer_sec"], start,
                workdir, settings, quality=source["quality"],
                auth_token="", ffmpeg_dir=ffmpeg_dir, progress_cb=phase,
                cancel_cb=cancel_cb, tag=f"intro{index}",
                vod_duration_sec=source["vod_duration_sec"])
            summary["fetched"] += 1
            # 取得素材の 0 秒は VOD の origin_sec 秒目。その分だけずらして切る
            # (元 VOD から切ったときと同じフレームになる / resolve5 §4.5)
            made = clip_writer.export_intro_source(
                fetched["path"], start, card["buffer_sec"], dest, ffmpeg_cfg,
                source_origin_sec=float(fetched["origin_sec"]))
        except DiskSpaceError:
            # 容量不足はここで諦める (次の区間も同じ結果になる)。書き出しは続ける
            _logger.warning("空き容量が足りないためイントロ素材の用意を打ち切ります")
            summary["skipped"].append((index, SKIP_FAILED))
            break
        except PipelineCancelled:
            _logger.info("イントロ素材の用意を中止しました (%d/%d)", pos - 1, total)
            break
        except Exception:  # noqa: BLE001 (カードが無くても本編は出す)
            _logger.warning("clip%s のイントロ素材を取得できませんでした "
                            "(カード無しで続けます)", index, exc_info=True)
            summary["skipped"].append((index, SKIP_FAILED))
            continue

        if not made:
            summary["skipped"].append((index, SKIP_FAILED))
            continue
        entry["intro_source"] = made
        summary["filled"] += 1
        _logger.info("clip%s のイントロ素材を用意しました: %s", index, made)

    if summary["fetched"]:
        _logger.info("イントロ用に %d 区間を取り直しました (用意 %d 件 / 見送り %d 件)",
                     summary["fetched"], summary["filled"], len(summary["skipped"]))
    return summary
