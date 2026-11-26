# 解析データの集約 (docs/AiEditor/resolve1.md §5 / R2)
#
# AI へ渡すのは**解析済みの構造化データだけ**である。音声・映像そのものは送らない
# (コスト・容量・プライバシー / §5.5)。
#
# ここは既存の解析を**呼ぶ側**であり、採点アルゴリズムを再実装しない (§4)。
#   archive.pipeline.analyze  → duration / curve / clips / audio_scan
#   archive.comment_source    → 正規化コメント
#   archive.clip_writer       → 候補区間の切り出しと文字起こし (§5.3)
#
# 時刻はすべて**元 VOD の秒**で持つ。素材時間・Timeline 時間への変換は
# plan_applier だけが行う (§9.2)。
#
# 文字起こし以外は Qt を引かない。切り出しと文字起こしの既定の実装だけは
# archive/clip_writer (→ modules/subtitle_generator → PySide6) を使うため、
# **関数の中で import する** (差し替えればテストは Qt 無しで通る)。
import copy
import os

from ..utils.logger import get_logger
from . import config as ai_config

_logger = get_logger(__name__)

# 候補区間を文字起こしするときの作業ファイル名
_CUT_NAME = "ai_cand{index}.mp4"


# AI へ渡す唯一の入力 (§5.1)。ローカルには完全版を持ち、送るのは §5.4 の縮約後。
class AnalysisBundle:

    def __init__(self, source=None, cells=None, curve=None, candidates=None,
                 transcript=None, comments=None, profile=None, moments=None):
        self.source = dict(source or {})
        self.cells = list(cells or [])
        self.curve = list(curve or [])
        self.candidates = list(candidates or [])
        # 候補区間だけの文字起こし (VOD 秒へ戻してある / §5.3)
        self.transcript = list(transcript or [])
        self.comments = list(comments or [])
        # 編集プロファイルの要約 (§13)。無ければ None
        self.profile = profile
        # AI の採点スイープが見つけた山 (docs/AiEditor/resolve2.md §6.2)。
        # スイープを行っていなければ空。構成と後段の編集が「どこが山か」に使う。
        self.moments = list(moments or [])

    @property
    def duration_sec(self):
        return float(self.source.get("duration_sec", 0.0) or 0.0)

    def candidate(self, index):
        for item in self.candidates:
            if int(item.get("index", -1)) == int(index):
                return item

        return None

    # 候補の index の一覧 (評価のバッチを組むのに使う)
    def indexes(self):
        return [int(item["index"]) for item in self.candidates]


# 採点を AI 用の本数で流すための実行用設定コピー (§7.1)。
#
# 切り抜き (経路 B) の archive.scoring.top_n は「出力するクリップ数」であり、
# AI の候補数とは別物である。設定ファイルは書き換えない。
def analysis_settings(settings):
    cfg = ai_config.ai_config(settings)["candidates"]
    copied = copy.deepcopy(settings if isinstance(settings, dict) else {})
    scoring = copied.setdefault("archive", {}).setdefault("scoring", {})
    scoring["top_n"] = max(int(cfg.get("top_n") or 0), 1)
    window = float(cfg.get("window_sec") or 0.0)
    if window > 0:
        scoring["window_sec"] = int(window)
    if cfg.get("allow_overlap"):
        # 窓の重なりを許す切り替えは archive/scoring.select_top_events の内部
        # (_overlaps) にあり、設定で外せない。採点側を変えずに実現できないため
        # 当面は効かない。必要になったら scoring 側へ切り替えを足す (§7.1)。
        _logger.info("ai.candidates.allow_overlap は現在の採点では未対応のため無視します")

    return copied


# AnalysisBundle を組む。
#
# analysis : archive.pipeline.analyze の戻り値 {"duration","curve","clips","audio_scan"}
# source   : {"video_id","title","game","width","height","fps"} (分かるものだけ)
# comments : comment_source.normalize_comments 済みのコメント
# transcript: transcribe_candidates の戻り値 (無ければ空)
def build(analysis, settings, source=None, comments=None, transcript=None,
          profile=None, cells=None):
    data = analysis if isinstance(analysis, dict) else {}
    cfg = ai_config.ai_config(settings)
    duration = float(data.get("duration", 0.0) or 0.0)

    info = dict(source or {})
    info["duration_sec"] = round(duration, 1)
    candidates = _candidates_of(data.get("clips"), cfg["candidates"])

    bundle = AnalysisBundle(
        source=info,
        # セルは解析の内部値であり analyze は返さない。渡されたときだけ持つ
        # (features.extract_cells を直接呼ぶ経路のため)。
        cells=cells,
        curve=data.get("curve"),
        candidates=candidates,
        transcript=transcript,
        comments=comments,
        profile=profile,
        moments=data.get("moments"))
    _logger.info("AnalysisBundle: 候補 %d 件 / 窓 %d 件 / 文字起こし %d 件 / コメント %d 件 / AI の山 %d 件",
                 len(bundle.candidates), len(bundle.curve), len(bundle.transcript),
                 len(bundle.comments), len(bundle.moments))

    return bundle


# 採点結果から候補を作る。スコア下限で切る (既定 0 = 無効 / §7.1)。
def _candidates_of(clips, cfg):
    minimum = float(cfg.get("min_score") or 0.0)
    candidates = []
    for clip in clips or []:
        if not isinstance(clip, dict):
            continue
        score = float(clip.get("score", 0.0) or 0.0)
        if minimum > 0 and score < minimum:
            continue
        candidates.append({
            "index": int(clip.get("index", len(candidates) + 1)),
            "start": round(float(clip.get("start", 0.0) or 0.0), 1),
            "end": round(float(clip.get("end", 0.0) or 0.0), 1),
            "score": round(score, 1),
            "emotion": round(float(clip.get("emotion", 0.0) or 0.0), 1),
            "comment": round(float(clip.get("comment", 0.0) or 0.0), 1),
            "marker": bool(clip.get("marker")),
            "marker_labels": [str(label) for label in (clip.get("marker_labels") or [])],
        })

    return candidates


# ---- §5.3 候補だけ文字起こしする ----------------------------------------

# 候補区間を切り出して文字起こしし、**VOD 秒へ戻した** [{"start","end","text"}] を返す。
#
# 手順 (§5.3): cut_region → cut_origin.measure → 文字起こし → 原点を足して VOD 秒へ。
# 正規化も無音カットも**しない** (テキストが欲しいだけ)。
# 重なる候補は区間を統合して 1 回だけ流す。
#
# cut / measure / transcribe / filter_speakers を渡すとそれを使う (テストと Qt 回避)。
def transcribe_candidates(input_path, settings, candidates, workdir,
                          duration_sec=0.0, ffmpeg_cfg=None, cut=None, measure=None,
                          transcribe=None, filter_speakers=None,
                          progress_cb=None, cancel_cb=None):
    cfg = ai_config.ai_config(settings)
    if not cfg["privacy"].get("include_transcript", True):
        _logger.info("ai.privacy.include_transcript=false のため文字起こしを送りません")

        return []

    ranges = merge_ranges(
        [(float(item["start"]) - float(cfg["candidates"]["transcribe_pad_sec"]),
          float(item["end"]) + float(cfg["candidates"]["transcribe_pad_sec"]))
         for item in candidates or []], gap=0.0,
        low=0.0, high=float(duration_sec or 0.0))
    if not ranges:
        return []

    tools = _transcribe_tools(cut, measure, transcribe, filter_speakers)
    ffmpeg = ffmpeg_cfg if ffmpeg_cfg is not None else (settings or {}).get("ffmpeg", {})
    eff_cfg = _transcribe_cfg(settings, cfg)
    os.makedirs(workdir, exist_ok=True)

    items = []
    for position, (start, end) in enumerate(ranges):
        if cancel_cb is not None and cancel_cb():
            break
        if progress_cb is not None:
            progress_cb(position / max(len(ranges), 1), position + 1, len(ranges))
        dest = os.path.join(workdir, _CUT_NAME.format(index=position))
        try:
            tools["cut"](input_path, start, end, dest, ffmpeg)
            # 切り出しの原点は要求位置とずれる (最大 1 GOP)。実測して足し戻す。
            origin = float(tools["measure"](input_path, start, ffmpeg) or start)
            recognized = tools["transcribe"](dest, settings, eff_cfg) or []
            recognized = tools["filter_speakers"](recognized, dest, eff_cfg, ffmpeg)
        except Exception as e:  # noqa: BLE001 (文字起こしの失敗で経路を止めない)
            _logger.warning("候補 %.1f〜%.1f の文字起こしに失敗しました: %s", start, end, e)
            continue
        finally:
            _remove(dest)

        for entry in recognized:
            text = str(entry.get("text", "") or "").strip()
            if not text:
                continue
            items.append({"start": round(origin + float(entry.get("start", 0.0)), 1),
                          "end": round(origin + float(entry.get("end", 0.0)), 1),
                          "text": text})

    items.sort(key=lambda item: item["start"])
    budget = cfg["budget"]
    merged = _merge_transcript(
        items, float(budget["merge_transcript_gap_sec"]),
        max_sec=float(budget.get("merge_transcript_max_sec", 0.0) or 0.0),
        max_chars=int(budget.get("merge_transcript_max_chars", 0) or 0))
    _logger.info("候補の文字起こし: 区間 %d 本 → %d 件", len(ranges), len(merged))

    return merged


def _transcribe_tools(cut, measure, transcribe, filter_speakers):
    if None not in (cut, measure, transcribe, filter_speakers):
        return {"cut": cut, "measure": measure, "transcribe": transcribe,
                "filter_speakers": filter_speakers}

    # 既定の実装は Qt を引くため、要求されたときだけ読み込む。
    from ..archive import clip_writer, cut_origin
    from ..modules import speaker_filter

    def _filter(items, path, eff_cfg, ffmpeg_cfg):
        # 話者照合は既定 "off" である (ゲーム音が乗る VOD では声紋が話者を
        # 表さなかった / docs/quality/resolve1.md §12.6)。AI 用に勝手に
        # "drop" へ上げない。利用者が有効にしていれば効く。
        if str(eff_cfg.get("speaker_filter", "off") or "off") == "off":
            return items
        filtered, _stats = speaker_filter.apply(items, path, eff_cfg, ffmpeg_cfg)

        return filtered or items

    return {"cut": cut or clip_writer.cut_region,
            "measure": measure or cut_origin.measure,
            "transcribe": transcribe or clip_writer._transcribe,
            "filter_speakers": filter_speakers or _filter}


# 文字起こしだけに使う実効設定。無音カットもテロップの整形も通さない。
def _transcribe_cfg(settings, cfg):
    subtitle = dict((settings or {}).get("subtitle", {}) or {})
    subtitle["enabled"] = True
    if not cfg["transcribe"].get("speaker_filter", True):
        subtitle["speaker_filter"] = "off"

    return subtitle


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        _logger.debug("作業ファイルを消せませんでした: %s", path)


# 重なる・隣接する区間を統合する (scoring._merge_time_sections と同じ考え方)。
def merge_ranges(ranges, gap=0.0, low=None, high=None):
    cleaned = []
    for start, end in ranges or []:
        a = float(start)
        b = float(end)
        if low is not None:
            a = max(a, float(low))
        if high is not None and float(high) > 0:
            b = min(b, float(high))
        if b - a > 0:
            cleaned.append((a, b))
    cleaned.sort()

    merged = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1] + float(gap or 0.0):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    return merged


# 連続する短文を結合する (§5.4 / 上限は docs/error/20261125c/resolve.md §2)。
#
# 上限が無かったため、連続した発話では**全部が 1 件に潰れていた**
# (実測: 99 件 → 1 件 / 149.1 秒 / 1,538 文字)。そうなると AI は
# 「その 149 秒のどこで言ったか」を知れず、テロップの時刻は推測になる。
#
# max_sec / max_chars のどちらかを超えるなら結合せず新しい件にする。
# 0 を渡せば従来どおり無制限 (回帰の逃げ道)。
def _merge_transcript(items, gap, max_sec=0.0, max_chars=0):
    merged = []
    for item in items:
        last = merged[-1] if merged else None
        if last is not None and item["start"] - last["end"] <= gap:
            end = max(last["end"], item["end"])
            text = (last["text"] + " " + item["text"]).strip()
            over_sec = max_sec > 0 and end - last["start"] > max_sec
            over_chars = max_chars > 0 and len(text) > max_chars
            if not (over_sec or over_chars):
                merged[-1] = {"start": last["start"], "end": end, "text": text}
                continue
        merged.append(dict(item))

    return merged


# ---- §5.4 トークン予算と縮約 --------------------------------------------

# AI へ渡す payload を組む (候補を絞ることもできる)。
#
# 共通部分 (source / profile / 全候補の要約) を**先**に置き、候補ごとの可変部分を
# 後ろへ置く。プロンプトキャッシュは前方一致のため、この順序を崩すと効かない (§6.4)。
def for_prompt(bundle, settings, indexes=None):
    cfg = ai_config.ai_config(settings)
    budget = cfg["budget"]
    chosen = [item for item in bundle.candidates
              if indexes is None or int(item["index"]) in set(indexes)]

    payload = {
        "source": _source_summary(bundle.source),
        "candidates_overview": [
            {"index": item["index"], "start": item["start"], "end": item["end"],
             "score": item["score"]}
            for item in bundle.candidates],
    }
    if bundle.profile:
        payload["profile"] = bundle.profile

    payload["candidates"] = [
        _candidate_payload(bundle, item, cfg, budget) for item in chosen]

    return payload


def _source_summary(source):
    # 送るのはタイトル・ゲーム名・寸法まで。パス・端末名・利用者の情報は送らない (§5.5)。
    return {"video_id": str(source.get("video_id", "") or ""),
            "title": str(source.get("title", "") or ""),
            "game": str(source.get("game", "") or ""),
            "duration_sec": round(float(source.get("duration_sec", 0.0) or 0.0), 1),
            "width": int(source.get("width", 0) or 0),
            "height": int(source.get("height", 0) or 0),
            "fps": round(float(source.get("fps", 0.0) or 0.0), 1)}


def _candidate_payload(bundle, item, cfg, budget):
    start = float(item["start"])
    end = float(item["end"])
    pad = float(budget["cell_pad_sec"])

    return {
        "index": item["index"],
        "start": round(start, 1),
        "end": round(end, 1),
        "base_score": item["score"],
        "emotion": item["emotion"],
        "comment_score": item["comment"],
        "marker_labels": item["marker_labels"],
        "cells": _cells_in(bundle.cells, start - pad, end + pad),
        "curve": _curve_in(bundle.curve, start, end, int(budget["curve_window_span"])),
        "transcript": _transcript_in(bundle.transcript, start, end),
        "comments": _comments_in(bundle.comments, start, end, cfg, budget),
    }


def _cells_in(cells, start, end):
    chosen = []
    for cell in cells or []:
        if float(cell.get("end", 0.0)) <= start or float(cell.get("start", 0.0)) >= end:
            continue
        chosen.append({
            "start": round(float(cell.get("start", 0.0)), 1),
            "end": round(float(cell.get("end", 0.0)), 1),
            "max_db": _round_or_none(cell.get("max_db")),
            "mean_db": _round_or_none(cell.get("mean_db")),
            "silence_ratio": round(float(cell.get("silence_ratio", 0.0) or 0.0), 2),
            "comment_count": int(cell.get("comment_count", 0) or 0),
            "w_count": int(cell.get("w_count", 0) or 0),
            "laugh_word_count": int(cell.get("laugh_word_count", 0) or 0),
            "laugh_emote_count": int(cell.get("laugh_emote_count", 0) or 0),
        })

    return chosen


def _curve_in(curve, start, end, span):
    rows = []
    for window in curve or []:
        a = float(window.get("start", 0.0))
        b = float(window.get("end", 0.0))
        if b <= start or a >= end:
            continue
        rows.append({"start": round(a, 1), "end": round(b, 1),
                     "emotion": round(float(window.get("emotion", 0.0) or 0.0), 1),
                     "comment": round(float(window.get("comment", 0.0) or 0.0), 1),
                     "total": round(float(window.get("total", 0.0) or 0.0), 1)})
    if span > 0 and len(rows) > span * 2 + 1:
        # 候補の前後 N 窓だけに絞る (真ん中を残す)。
        middle = len(rows) // 2
        rows = rows[max(middle - span, 0):middle + span + 1]

    return rows


def _transcript_in(transcript, start, end):
    return [{"start": round(float(item["start"]), 1),
             "end": round(float(item["end"]), 1), "text": item["text"]}
            for item in transcript or []
            if float(item["end"]) > start and float(item["start"]) < end]


def _comments_in(comments, start, end, cfg, budget):
    include_user = bool(cfg["privacy"].get("include_comment_user"))
    limit = max(int(budget["max_comments_per_cell"]), 0)
    chosen = [item for item in comments or []
              if start <= float(item.get("offset_sec", -1.0)) <= end]
    if limit and len(chosen) > limit:
        # ｗ・笑い語・エモートを含むものを優先して間引く (§5.4)。
        chosen = sorted(chosen, key=_comment_rank)[:limit]
        chosen.sort(key=lambda item: float(item.get("offset_sec", 0.0)))

    rows = []
    for item in chosen:
        row = {"offset_sec": round(float(item.get("offset_sec", 0.0)), 1),
               "text": str(item.get("text", "") or "")}
        if include_user:
            row["user"] = str(item.get("user", "") or "")
        rows.append(row)

    return rows


# 間引きの優先度 (小さいほど残す)。
def _comment_rank(item):
    text = str(item.get("text", "") or "")
    emotes = item.get("emotes") or []
    laughs = text.count("w") + text.count("ｗ") + text.count("草") + len(emotes)

    return (-laughs, float(item.get("offset_sec", 0.0)))


def _round_or_none(value):
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


# ---- 概算トークンとバッチ分割 --------------------------------------------

# クライアント側の概算 (文字数 ÷ chars_per_token)。**分割の判断にだけ使う**。
# 正確なトークン数はサーバーが数え、ポイントの見積りと実績はサーバーの数値を正とする (§5.4)。
def estimate_tokens(payload, settings):
    cfg = ai_config.ai_config(settings)
    per_token = float(cfg["budget"]["chars_per_token"]) or 2.0
    text = _text_size(payload)

    return int(text / per_token) + 1


def _text_size(value):
    if isinstance(value, dict):
        return sum(len(str(key)) + _text_size(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_text_size(item) for item in value)

    return len(str(value))


# 候補を評価のバッチへ分ける (§7.2 の batch_size と §5.4 の max_input_tokens)。
#
# batch_size 件ずつに切り、概算が max_input_tokens を超えるバッチはさらに半分へ割る。
def batches(bundle, settings):
    cfg = ai_config.ai_config(settings)
    size = max(int(cfg["scene_eval"]["batch_size"]), 1)
    limit = max(int(cfg["budget"]["max_input_tokens"]), 0)
    indexes = bundle.indexes()
    groups = [indexes[at:at + size] for at in range(0, len(indexes), size)]

    result = []
    for group in groups:
        result.extend(_split_to_budget(bundle, settings, group, limit))

    return result


def _split_to_budget(bundle, settings, group, limit):
    if not group:
        return []
    if limit <= 0 or len(group) == 1:
        return [group]
    if estimate_tokens(for_prompt(bundle, settings, group), settings) <= limit:
        return [group]

    middle = len(group) // 2

    return (_split_to_budget(bundle, settings, group[:middle], limit)
            + _split_to_budget(bundle, settings, group[middle:], limit))
