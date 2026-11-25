# 採点スイープ (docs/AiEditor/resolve2.md §4 / S1)
#
# 全長を chunk_sec ごとに区切り、**映像 (静止画) と音量の数値とコメントだけ**を
# AI へ見せて「山」を列挙させる。既存の採点 (音量とｗ数の線形和) が候補にしない
# 場面 — 静かに面白い会話、画面だけで笑える場面、コメントの薄い配信 — を拾うのが目的。
#
# 要点:
#   ・文字起こしは使わない (§0 の 6)。全長の Whisper は CPU で 1 時間級になる。
#     文字起こしは候補が決まってから候補区間だけに行う (従来どおり)。
#   ・フレームは 1 回のデコードで抜く (vision.frames_every)。
#   ・1 回の呼び出しに送る画像は images_per_request まで (サーバーの上限 / §7.3)。
#   ・**順位は AI に作らせない。** 軸ごとの点を返させ、重みは設定が持つ (§5.3)。
#   ・チャンクが 1 つ失敗しても残りで続ける。全滅したら空を返し、
#     呼び出し側は機械採点で続行する (§9)。
#
# Qt は引かない。プロバイダーは呼び出し側が渡す (テストは差し替える)。
import base64
import os

from ..exceptions import AiError
from ..utils.logger import get_logger
from . import config as ai_config
from . import plan_schema, prompts, remote, vision
from .provider import AiImage, AiRequest

_logger = get_logger(__name__)

# 前置きを使い回す意思表示 (resolve1.md §6.4)。同じ役・同じ版の前置きはキャッシュへ乗る。
_CACHE_PREFIX = "scene_scan-v1"

# コメントを送るときの 1 件あたりの上限 (長文の荒らしで予算を食わないため)
_MAX_COMMENT_CHARS = 120


# 山 1 件。plan_schema.normalize_scan が返す dict をそのまま持つ。
class Moment:

    def __init__(self, start, end, peak=None, beats=None, scores=None,
                 title_line="", why="", evidence=None, scan_score=0.0):
        self.start = round(float(start), 3)
        self.end = round(float(end), 3)
        self.peak = float(peak) if peak is not None else round((self.start + self.end) / 2.0, 3)
        # 起こり → 山 → 落ち (後段の「自然な編集」が切り位置に使う / §15)
        self.beats = dict(beats or {})
        self.scores = dict(scores or {})
        self.title_line = str(title_line or "")
        self.why = str(why or "")
        self.evidence = dict(evidence or {})
        # 重み付けした総合点 (AI は順位を作らない / §5.3)
        self.scan_score = round(float(scan_score or 0.0), 3)

    def as_dict(self):
        return {"start": self.start, "end": self.end, "peak": self.peak,
                "beats": self.beats, "scores": self.scores,
                "title_line": self.title_line, "why": self.why,
                "evidence": self.evidence, "scan_score": self.scan_score}

    def __repr__(self):
        return "Moment({0:.1f}-{1:.1f}, {2:.1f}, {3!r})".format(
            self.start, self.end, self.scan_score, self.title_line)


# 全長をスイープして山の一覧を返す (VOD 秒・開始順)。
#
# input_path  : 元 VOD
# settings    : setting.json
# provider    : ai/provider.AiProvider (None なら何もしない = 機械採点のまま)
# cells       : archive.features.extract_cells のセル (音量・無音・コメント数)
# comments    : comment_source.normalize_comments 済みのコメント
# duration_sec: 全長
# use         : 重みの組 ("long" / "shorts")
# workdir     : フレームの一時置き場
# frames      : フレーム抽出の差し替え口 (テストは ffmpeg を呼ばない)
# report      : plan_validator.PlanReport (記録を足す)
#
# 失敗しても例外を上げない。空の一覧を返し、呼び出し側は機械採点で続ける (§9)。
def scan(input_path, settings, provider, cells=None, comments=None,
         duration_sec=0.0, use="long", workdir="", frames=None,
         progress_cb=None, cancel_cb=None, report=None, start_sec=0.0):
    cfg = ai_config.ai_config(settings)
    scan_cfg = cfg["scan"]
    if not scan_cfg.get("enabled", True) or provider is None:
        _logger.info("採点スイープを行いません (設定 %s / プロバイダー %s)",
                     scan_cfg.get("enabled", True), provider is not None)

        return []

    total = float(duration_sec or 0.0)
    if total <= 0:
        return []

    picked = _extract_frames(input_path, workdir, settings, frames)
    if not picked:
        # 画像が無いまま文字だけで送らない。それでは要望 (動画を見る) を満たさない (§9)。
        _logger.warning("フレームを抜けなかったため採点スイープを飛ばします")

        return []

    chunks = plan_chunks(total, scan_cfg, start_sec=start_sec)
    _logger.info("採点スイープ: %d 区間 (1 区間 %.0fs) / フレーム %d 枚",
                 len(chunks), chunks[0][1] - chunks[0][0] if chunks else 0.0,
                 len(picked))

    wire_role, model_key = _roles_for(provider)
    image_limit = _image_limit(provider, scan_cfg)
    system = _system_prompt(settings, scan_cfg)
    schema = plan_schema.scene_scan_schema(
        max_title_chars=int(scan_cfg.get("max_title_chars") or 30),
        max_reason_chars=int(cfg["scene_eval"]["max_reason_chars"]))
    weights = _weights(scan_cfg, use)

    moments = []
    for position, (start, end) in enumerate(chunks):
        if cancel_cb is not None and cancel_cb():
            break
        if progress_cb is not None:
            progress_cb(position / max(len(chunks), 1), position + 1, len(chunks))
        found = _scan_chunk(provider, system, schema, settings, scan_cfg, cfg,
                            start, end, picked, cells, comments, weights,
                            position, report, wire_role, model_key, image_limit)
        moments.extend(found)

    _cleanup(picked)
    moments.sort(key=lambda m: m.start)
    _logger.info("採点スイープ完了: 山 %d 件", len(moments))

    return moments


# 線の上で使う役と、モデル・effort を引く鍵を決める。
#
# サーバーは役を許可リストで絞っており (resolve1-API.md「許可リスト外は 400」)、
# 2026-11-25 時点の許可は sceneEval / plan / review / profile / classify である。
# **scene_scan はまだ無い**ため、そのまま送ると validation_failed で全区間が落ちる
# (実測)。サーバーが役を足すまでは、意味の最も近い sceneEval で送る。
#   ・モデルと effort はクライアント側の ai.models.scene_scan / ai.effort.scene_scan
#     から引く (model_key を分ける)。送るモデルは変わらない。
#   ・サーバーが sceneScan を許可したら自動でそちらへ切り替わる。設定は要らない。
# 構成を引けないときも安全側 (sceneEval) に倒す。
def _roles_for(provider):
    want = ai_config.ROLE_SCENE_SCAN
    wire = remote.wire_role(want)
    allowed = _server_roles(provider)
    if allowed is None or wire in allowed:
        return want, want

    _logger.info("サーバーが %s をまだ受け付けないため %s として送ります",
                 wire, remote.wire_role(ai_config.ROLE_SCENE_EVAL))

    return ai_config.ROLE_SCENE_EVAL, want


# サーバーが許可している役 (引けなければ None = 判断しない)
def _server_roles(provider):
    getter = getattr(provider, "server_config", None)
    if getter is None:
        return None
    try:
        config = getter()
    except Exception as e:  # noqa: BLE001 (構成を引けなくてもスイープは試す)
        _logger.debug("AI の構成を引けませんでした: %s", e)

        return None
    roles = getattr(config, "roles", None)

    return set(roles) if isinstance(roles, dict) else None


# 1 回の呼び出しで送る画像の上限。サーバーの上限 (maxImagesPerRequest) を優先する。
# 超えて送ると 400 になるため、クライアント側でも必ず切る (resolve2.md §7.3)。
def _image_limit(provider, scan_cfg):
    limit = max(int(scan_cfg.get("images_per_request") or 16), 1)
    getter = getattr(provider, "server_config", None)
    if getter is None:
        return limit
    try:
        limits = getattr(getter(), "limits", None) or {}
        server = int(limits.get("maxImagesPerRequest") or 0)
    except Exception:  # noqa: BLE001
        return limit
    if server > 0 and server < limit:
        _logger.info("画像の枚数をサーバーの上限 %d 枚に合わせます", server)

        return server

    return limit


# 全長をチャンクへ割る。max_calls を超えるなら 1 区間を伸ばす (打ち切らない / §4.1)。
def plan_chunks(duration_sec, scan_cfg, start_sec=0.0):
    begin = max(float(start_sec or 0.0), 0.0)
    total = float(duration_sec or 0.0) - begin
    if total <= 0:
        return []
    chunk = float(scan_cfg.get("chunk_sec") or 480.0)
    if chunk <= 0:
        chunk = 480.0
    limit = int(scan_cfg.get("max_calls") or 0)
    if limit > 0:
        needed = int(total // chunk) + (1 if total % chunk else 0)
        if needed > limit:
            chunk = total / float(limit)
            _logger.info("区間が %d 個になるため 1 区間を %.0fs へ広げます (上限 %d)",
                         needed, chunk, limit)

    chunks = []
    cursor = begin
    end = begin + total
    while cursor < end - 0.001:
        chunks.append((round(cursor, 3), round(min(cursor + chunk, end), 3)))
        cursor += chunk

    return chunks


# チャンク 1 つを評価する。失敗してもそのチャンクを捨てるだけ (§9)。
def _scan_chunk(provider, system, schema, settings, scan_cfg, cfg,
                start, end, picked, cells, comments, weights, position, report,
                wire_role=None, model_key=None, image_limit=0):
    where = "chunk[{0}]".format(position)
    images, shown = _images_for(picked, start, end, scan_cfg, image_limit)
    if not images:
        _note(report, "S0", where, plan_schema.ACTION_DROP, "no frame")

        return []

    payload = {
        "range": {"start": round(float(start), 1), "end": round(float(end), 1)},
        "frames": [round(float(at), 1) for at in shown],
        "levels": _levels_for(cells, start, end),
        "comments": _comments_for(comments, start, end, cfg),
    }
    request = AiRequest(
        role=wire_role or ai_config.ROLE_SCENE_SCAN, system=system,
        payload=payload, schema=schema, images=images,
        cache_prefix_id=_CACHE_PREFIX,
        max_output_tokens=cfg["max_output_tokens"],
        model_key=model_key or ai_config.ROLE_SCENE_SCAN)
    try:
        completion = provider.complete_json(request)
        found, notes = plan_schema.normalize_scan(
            completion.result, start, end,
            min_sec=float(scan_cfg.get("min_moment_sec") or 0.0),
            max_sec=float(scan_cfg.get("max_moment_sec") or 0.0),
            max_title_chars=int(scan_cfg.get("max_title_chars") or 30),
            max_reason_chars=int(cfg["scene_eval"]["max_reason_chars"]))
    except AiError as e:
        _note(report, "S0", where, plan_schema.ACTION_DROP,
              getattr(e, "code", None) or type(e).__name__)
        _logger.warning("採点スイープの区間 %d を飛ばします: %s", position, e)

        return []
    for item in notes or []:
        _note(report, item.get("check"), item.get("where"),
              item.get("action"), item.get("detail"))

    moments = []
    for item in found:
        moments.append(Moment(
            scan_score=plan_schema.scan_score(item.get("scores"), weights), **item))
    _logger.info("区間 %.0f-%.0fs: 山 %d 件 (画像 %d 枚)",
                 start, end, len(moments), len(images))

    return moments


# チャンクに入るフレームを images_per_request まで選び、送信用へ読み込む。
# 戻り値: (AiImage の一覧, その秒の一覧)
def _images_for(picked, start, end, scan_cfg, image_limit=0):
    inside = [frame for frame in picked if start <= frame.at_sec < end]
    limit = int(image_limit or 0)
    if limit <= 0:
        limit = max(int(scan_cfg.get("images_per_request") or 16), 1)
    if len(inside) > limit:
        # 端を残して均等に間引く (vision._thin_out と同じ考え方)
        last = len(inside) - 1
        keep = sorted({int(round(i * last / float(limit - 1))) for i in range(limit)}) \
            if limit > 1 else [0]
        inside = [inside[i] for i in keep]

    images = []
    shown = []
    for frame in inside:
        try:
            with open(frame.path, "rb") as handle:
                data = handle.read()
        except OSError as e:
            _logger.warning("フレームを読めませんでした: %s (%s)", frame.path, e)
            continue
        if not data:
            continue
        images.append(AiImage(media_type="image/jpeg",
                              data_base64=base64.b64encode(data).decode("ascii")))
        shown.append(frame.at_sec)

    return images, shown


# セルの数値 (音量・無音・コメント数) をチャンクぶんだけ渡す。
# 本文は含まない = 機械が測った値だけ。
def _levels_for(cells, start, end):
    rows = []
    for cell in cells or []:
        cell_start = float(cell.get("start", 0.0))
        if cell_start < start or cell_start >= end:
            continue
        rows.append({
            "t": round(cell_start, 1),
            "mean_db": _rounded(cell.get("mean_db")),
            "max_db": _rounded(cell.get("max_db")),
            "silence": round(float(cell.get("silence_ratio", 0.0)), 2),
            "comments": int(cell.get("comment_count", 0) or 0),
            "laughs": (int(cell.get("w_count", 0) or 0)
                       + int(cell.get("laugh_word_count", 0) or 0)
                       + int(cell.get("laugh_emote_count", 0) or 0)),
        })

    return rows


# チャンク内のコメント。**投稿者名は送らない** (resolve1.md §5.5 / §10)。
def _comments_for(comments, start, end, cfg):
    limit = int(cfg["budget"].get("max_comments_per_cell") or 20)
    inside = [c for c in comments or []
              if start <= float(c.get("offset_sec", 0.0)) < end]
    if len(inside) > limit:
        step = len(inside) / float(limit)
        inside = [inside[min(int(i * step), len(inside) - 1)] for i in range(limit)]

    return [{"t": round(float(c.get("offset_sec", 0.0)), 1),
             "text": str(c.get("text", "") or "")[:_MAX_COMMENT_CHARS]}
            for c in inside]


def _rounded(value):
    if value is None:
        return None

    return round(float(value), 1)


def _system_prompt(settings, scan_cfg):
    return prompts.render(
        prompts.SCENE_SCAN,
        min_sec=float(scan_cfg.get("min_moment_sec") or 0.0),
        max_sec=float(scan_cfg.get("max_moment_sec") or 0.0),
        max_chars=int(scan_cfg.get("max_title_chars") or 30),
        max_reason_chars=int(
            ai_config.ai_config(settings)["scene_eval"]["max_reason_chars"]),
        language=_language(settings))


# 出力言語。planner._language と同じ規約 (コードではなく言語名を書かせる)。
def _language(settings):
    value = str((settings or {}).get("subtitle", {}).get("language", "ja") or "ja")

    return "日本語" if value.startswith("ja") else value


# 重みの組 (用途ごと)。未知の用途は long を使う。
def _weights(scan_cfg, use):
    weights = (scan_cfg.get("weights") or {})

    return weights.get(use) or weights.get("long") or {}


def _extract_frames(input_path, workdir, settings, frames):
    extract = frames if frames is not None else vision.frames_every

    return extract(input_path, workdir, settings) or []


def _cleanup(picked):
    for frame in picked:
        try:
            if frame.path and os.path.exists(frame.path):
                os.remove(frame.path)
        except OSError:
            _logger.debug("フレームを消せませんでした: %s", frame.path)


def _note(report, check, where, action, detail=""):
    if report is None:
        return
    try:
        report.add(check, where, action, detail)
    except Exception:  # noqa: BLE001 (記録で本処理を壊さない)
        _logger.debug("記録を足せませんでした: %s %s", check, where)
