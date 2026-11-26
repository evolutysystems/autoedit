# 面白シーン抽出 (docs/AiEditor/resolve1.md §7 / R4)
#
# 3 段階に分かれる。
#   第 1 段階 候補抽出   既存の採点をそのまま使う (archive/scoring は変更しない)
#   第 2 段階 AI 評価    候補を batch_size 件ずつ 8 軸で採点させる
#   第 3 段階 統合       重なりを統合し、**設定の重み**で順位を決める
#
# 要点:
#   ・単一の「面白さ」を聞かない (§7.2)。軸を分けると再現性が出て、学習にも使える。
#   ・順位そのものを AI に作らせない (§7.3)。重みは設定が持つ。
#   ・既存の採点も混ぜる (base_score_weight)。AI が落としても音量・コメントが
#     極端に高い区間は残る = AI の誤判断で面白い場面を失わない。
#   ・AI が失敗したら**既存の採点だけで順位を付けて続行する** (§11.2 の縮退)。
#
# Qt は引かない。プロバイダーは呼び出し側が渡す (テストは MockProvider)。
import json
import os

from ..utils.logger import get_logger
from . import config as ai_config
from . import context_builder, plan_schema, prompts
from .plan_validator import PlanReport
from .provider import AiRequest, should_degrade
from ..exceptions import AiError

_logger = get_logger(__name__)

# 用途 (重みの組 / §7.3)
USE_LONG = "long"
USE_SHORTS = "shorts"

# 評価を保存する版 (形が変わったら上げて作り直す)
EVAL_VERSION = 1

# 前置きを使い回す意思表示 (§6.4)。同じ役・同じ版の前置きはキャッシュへ乗る。
_CACHE_PREFIX = "scene_eval-v1"


# 候補 1 件の評価結果。
class RankedScene:

    def __init__(self, index, start, end, scores=None, summary="", reason="",
                 needed_context=None, recommended_use=None, peak=None,
                 base_score=0.0, evaluated=False):
        self.index = int(index)
        self.start = round(float(start), 3)
        self.end = round(float(end), 3)
        self.scores = dict(scores or {})
        self.summary = str(summary or "")
        self.reason = str(reason or "")
        self.needed_context = needed_context
        self.recommended_use = list(recommended_use or [])
        self.peak = peak
        # 既存の採点 (archive/scoring の total)
        self.base_score = round(float(base_score or 0.0), 3)
        # AI が評価したか (False = 縮退で既存の採点だけ)
        self.evaluated = bool(evaluated)
        # 第 3 段階で入れる総合点
        self.rank_score = 0.0

    @property
    def duration_sec(self):
        return max(self.end - self.start, 0.0)

    def to_dict(self):
        return {"index": self.index, "start": self.start, "end": self.end,
                "scores": self.scores, "summary": self.summary,
                "reason": self.reason, "needed_context": self.needed_context,
                "recommended_use": self.recommended_use, "peak": self.peak,
                "base_score": self.base_score, "evaluated": self.evaluated,
                "rank_score": self.rank_score}

    @staticmethod
    def from_dict(data):
        scene = RankedScene(
            index=data.get("index", 0), start=data.get("start", 0.0),
            end=data.get("end", 0.0), scores=data.get("scores"),
            summary=data.get("summary", ""), reason=data.get("reason", ""),
            needed_context=data.get("needed_context"),
            recommended_use=data.get("recommended_use"), peak=data.get("peak"),
            base_score=data.get("base_score", 0.0),
            evaluated=bool(data.get("evaluated")))
        scene.rank_score = float(data.get("rank_score", 0.0) or 0.0)

        return scene


# 候補を評価して順位を付ける。返り値は (RankedScene のリスト, PlanReport).
#
# provider が None のときは AI を呼ばず、既存の採点だけで順位を付ける (縮退)。
# project_path を渡すと評価を <project>.ai.json へ残し、次回はそれを使う (§7.2)。
def evaluate(bundle, provider, settings, use=USE_LONG, project_path="",
             reuse=True, report=None, progress_cb=None, cancel_cb=None):
    cfg = ai_config.ai_config(settings)
    report = report if report is not None else PlanReport()
    if not bundle.candidates:
        return [], report

    # ai.scene_eval.enabled = false なら AI を呼ばず、既存の採点だけで順位を付ける。
    # 既定で切っている理由は docs/error/20261125c/resolve.md §2.6:
    # 採点スイープ (sceneScan) が映像を見て山を挙げるようになったため、
    # 文字起こしと数値だけで候補を再採点するこの工程は役割が重なる。
    #
    # 保存済みの評価 (<project>.ai.json) も使わない。切ったのに前回の AI の点が
    # 効くと「何が順位を決めたのか」が読めなくなる。
    if not cfg["scene_eval"].get("enabled", True):
        if provider is not None:
            _logger.info("シーン評価は設定で切られています (既存の採点だけで順位を付けます)")
        provider = None
        reuse = False

    cached = load_eval(project_path) if (reuse and project_path) else {}
    scenes = {}
    for scene in _from_cache(cached, bundle):
        scenes[scene.index] = scene
    if scenes:
        _logger.info("保存済みのシーン評価を使います: %d 件 (%s)",
                     len(scenes), os.path.basename(plan_schema.eval_path_for(project_path)))

    pending = [index for index in bundle.indexes() if index not in scenes]
    if provider is not None and pending:
        _evaluate_batches(bundle, provider, settings, cfg, pending, scenes,
                          report, progress_cb, cancel_cb)

    # 評価できなかった候補は既存の採点だけで残す (落とさない)。
    for index in bundle.indexes():
        if index not in scenes:
            scenes[index] = _unevaluated(bundle.candidate(index))

    ranked = rank(list(scenes.values()), settings, use=use)
    if project_path:
        save_eval(ranked, project_path, bundle)

    evaluated = sum(1 for scene in ranked if scene.evaluated)
    _logger.info("シーン評価: %d 件中 %d 件を AI が評価 (用途 %s)",
                 len(ranked), evaluated, use)

    return ranked, report


def _evaluate_batches(bundle, provider, settings, cfg, pending, scenes, report,
                      progress_cb, cancel_cb):
    groups = [group for group in context_builder.batches(bundle, settings)
              if any(index in pending for index in group)]
    language = _language(settings)
    max_chars = int(cfg["scene_eval"]["max_reason_chars"])
    system = prompts.render(prompts.SCENE_EVAL, language=language,
                            max_chars=max_chars)

    for position, group in enumerate(groups):
        if cancel_cb is not None and cancel_cb():
            break
        if progress_cb is not None:
            progress_cb(position / max(len(groups), 1), position + 1, len(groups))
        payload = context_builder.for_prompt(bundle, settings, indexes=group)
        request = AiRequest(
            role=ai_config.ROLE_SCENE_EVAL, system=system, payload=payload,
            schema=plan_schema.scene_eval_schema(
                candidate_count=_max_index(bundle) + 1, max_reason_chars=max_chars),
            cache_prefix_id=_CACHE_PREFIX, max_output_tokens=cfg["max_output_tokens"])
        try:
            completion = provider.complete_json(request)
            evaluated, notes = plan_schema.normalize_scene_eval(
                completion.result, candidate_count=_max_index(bundle) + 1,
                max_reason_chars=max_chars)
        except AiError as e:
            # 失敗したバッチは既存の採点へ落とす。他のバッチは続ける (§11.2)。
            report.add("E2", "batch[{0}]".format(position), plan_schema.ACTION_DROP,
                       getattr(e, "code", None) or type(e).__name__)
            _logger.warning("シーン評価のバッチ %d を既存の採点へ落とします (%s): %s",
                            position, "縮退可" if should_degrade(e) else "要確認", e)
            continue
        report.extend(notes)

        for item in evaluated:
            candidate = bundle.candidate(item["candidate_index"])
            if candidate is None:
                continue
            scenes[int(candidate["index"])] = _ranked_of(candidate, item, report)


# AI の評価を候補へ重ねる。区間は候補の範囲内へ収める (勝手に伸ばさせない / §8.1)。
def _ranked_of(candidate, item, report):
    start = item["start"] if item["start"] is not None else candidate["start"]
    end = item["end"] if item["end"] is not None else candidate["end"]
    low = float(candidate["start"])
    high = float(candidate["end"])
    clipped_start = min(max(float(start), low), high)
    clipped_end = min(max(float(end), low), high)
    if clipped_end - clipped_start <= 0:
        clipped_start, clipped_end = low, high
        report.add("E2", "candidate[{0}]".format(candidate["index"]),
                   plan_schema.ACTION_DEFAULT, "range")
    elif clipped_start != float(start) or clipped_end != float(end):
        report.add("E2", "candidate[{0}]".format(candidate["index"]),
                   plan_schema.ACTION_CUT, "range")

    return RankedScene(
        index=candidate["index"], start=clipped_start, end=clipped_end,
        scores=item["scores"], summary=item["summary"], reason=item["reason"],
        needed_context=item["needed_context"],
        recommended_use=item["recommended_use"], peak=item["peak"],
        base_score=candidate["score"], evaluated=True)


# AI が評価しなかった候補 (縮退)。軸は中立にして既存の採点だけで順位へ乗せる。
def _unevaluated(candidate):
    item = candidate or {"index": 0, "start": 0.0, "end": 0.0, "score": 0.0}

    return RankedScene(
        index=item.get("index", 0), start=item.get("start", 0.0),
        end=item.get("end", 0.0),
        scores={key: float(plan_schema.DEFAULT_SCORE) for key in plan_schema.SCORE_KEYS},
        base_score=item.get("score", 0.0), evaluated=False)


# ---- 第 3 段階: 統合・ランキング (§7.3) ----------------------------------

# 重なりを統合し、設定の重みで順位を決める。
def rank(scenes, settings, use=USE_LONG):
    cfg = ai_config.ai_config(settings)["scene_eval"]
    weights = (cfg.get("weights") or {}).get(use) or {}
    base_weight = float(cfg.get("base_score_weight") or 0.0)

    merged = merge_overlaps(scenes)
    for scene in merged:
        scene.rank_score = plan_schema.rank_score(
            scene.scores, weights, base_score=scene.base_score,
            base_weight=base_weight)
    merged.sort(key=lambda scene: (-scene.rank_score, scene.start))

    return merged


# 区間が重なる / 連続するシーンを 1 つへ統合する
# (archive/scoring._merge_time_sections と同じ規則)。
# 点は**高い側を残す** (統合で面白さが薄まらないようにする)。
def merge_overlaps(scenes, gap=0.0):
    ordered = sorted(scenes, key=lambda scene: (scene.start, scene.end))
    merged = []
    for scene in ordered:
        if merged and scene.start <= merged[-1].end + gap:
            merged[-1] = _merge_two(merged[-1], scene)
        else:
            merged.append(scene)

    return merged


def _merge_two(first, second):
    keep = first if _strength(first) >= _strength(second) else second
    other = second if keep is first else first
    keep.start = min(first.start, second.start)
    keep.end = max(first.end, second.end)
    keep.base_score = max(first.base_score, second.base_score)
    keep.evaluated = first.evaluated or second.evaluated
    for key in plan_schema.SCORE_KEYS:
        keep.scores[key] = max(float(keep.scores.get(key, 0.0)),
                               float(other.scores.get(key, 0.0)))
    for value in other.recommended_use:
        if value not in keep.recommended_use:
            keep.recommended_use.append(value)

    return keep


def _strength(scene):
    return (float(scene.scores.get("funny", 0.0)), scene.base_score)


# 用途に合う上位 N 件 (skip は外す)。
def top_scenes(scenes, limit=0, drop_skipped=True):
    chosen = [scene for scene in scenes
              if not (drop_skipped and "skip" in scene.recommended_use)]

    return chosen[:limit] if limit and limit > 0 else chosen


# ---- 評価の保存 (<project>.ai.json / §7.2) -------------------------------

def save_eval(scenes, project_path, bundle=None):
    path = plan_schema.eval_path_for(project_path)
    if not path:
        return ""
    data = {"eval_version": EVAL_VERSION,
            "source": dict((bundle.source if bundle else {}) or {}),
            "scenes": [scene.to_dict() for scene in scenes]}
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as e:
        # 保存できなくても評価そのものは使える。
        _logger.warning("シーン評価を保存できませんでした: %s", e)

        return ""

    return path


def load_eval(project_path):
    path = plan_schema.eval_path_for(project_path)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as e:
        _logger.warning("シーン評価を読めませんでした: %s", e)

        return {}
    if not isinstance(data, dict) or data.get("eval_version") != EVAL_VERSION:
        return {}

    return data


# 保存済みの評価のうち、今の候補と区間が合うものだけを使う。
# 候補の作り方 (top_n・窓長) を変えたら作り直す = 古い評価を黙って混ぜない。
def _from_cache(cached, bundle):
    scenes = []
    for row in (cached or {}).get("scenes") or []:
        if not isinstance(row, dict) or not row.get("evaluated"):
            continue
        candidate = bundle.candidate(row.get("index", -1))
        if candidate is None:
            continue
        if abs(float(candidate["start"]) - float(row.get("start", -1))) > 60.0:
            continue
        scenes.append(RankedScene.from_dict(row))

    return scenes


def _max_index(bundle):
    return max(bundle.indexes() or [0])


def _language(settings):
    value = str((settings or {}).get("subtitle", {}).get("language", "ja") or "ja")

    return "日本語" if value.startswith("ja") else value
