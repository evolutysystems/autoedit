# 品質レビューと自動修正 (docs/AiEditor/resolve1.md §12 / R10)
#
# 役ごとに 1 回ずつ呼び、指摘 (issues) を集める。前置き (EditPlan + 解析の要約) は
# **役が変わっても同じ**にしてキャッシュへ乗せ、役の視点は payload へ入れる (§12.1 / §6.4)。
#
# 要点:
#   ・`qa` 役は**機械で測れるものは機械で測る** (§12.2)。AI へ渡すのは測った結果だけ。
#   ・採る指摘は重大度の下限と件数で絞る (§12.4)。上限に達したら止めて利用者へ渡す。
#   ・**最終承認は必ず人間** (§12.4 末尾)。レビューが通っても自動で出力しない。
#   ・修正は EditPlan に対して決定的に当てる (op ごとに決まった操作だけ)。
#     AI に Timeline を触らせない。
#
# 設計 §12.4 のループは「適用 → レビュー → 修正 → 適用」だが、実装では
# **EditPlan に対してレビューを回し、Timeline への適用は最後に 1 回**にした。
# 1 往復ごとに素材を作り直す (ffmpeg) コストが現実的でないためである。
# フレームは元 VOD から抜くので適用前でもレビューに回せる (§12.5)。
import copy

from ..exceptions import AiError
from ..utils.logger import get_logger
from . import config as ai_config
from . import plan_schema, prompts, vision
from .plan_validator import PlanReport
from .provider import AiRequest

_logger = get_logger(__name__)

# 役 (§12.1)
ROLE_DIRECTOR = "director"
ROLE_CRITIC = "critic"
ROLE_VIEWER = "viewer"
ROLE_PLATFORM = "platform"
ROLE_QA = "qa"
ROLES = (ROLE_DIRECTOR, ROLE_CRITIC, ROLE_VIEWER, ROLE_PLATFORM, ROLE_QA)

# より強いモデルを希望する役 (§6.3。model_key で希望を分ける)
_MODEL_KEYS = {ROLE_CRITIC: ai_config.MODEL_KEY_REVIEW_CRITIC}

# 映像を見せる役 (§12.5。director だけ最大 3 枚)
_FRAMES_PER_ROLE = {ROLE_DIRECTOR: 3}

# 前置きを使い回す意思表示 (§6.4)
_CACHE_PREFIX = "review-v1"

# テロップ 1 本の最短・最長の目安 (機械の検査 / §12.2)
_SUBTITLE_MIN_SEC = 0.5
_SUBTITLE_MAX_SEC = 8.0


# 1 回のレビューの結果。
class ReviewResult:

    def __init__(self, plan, issues=None, applied=None, rounds=0, checks=None,
                 report=None):
        self.plan = plan
        # 集まった指摘 (採らなかったものも含む)
        self.issues = list(issues or [])
        # 実際に EditPlan へ当てた指摘
        self.applied = list(applied or [])
        self.rounds = int(rounds)
        # 機械の測定結果 (§12.2)
        self.checks = list(checks or [])
        self.report = report

    # 未解決の critical (画面に出す / §12.4)
    @property
    def unresolved(self):
        applied = {issue["id"] for issue in self.applied}

        return [issue for issue in self.issues
                if issue["severity"] == "critical" and issue["id"] not in applied]


# レビューと修正を回す。返り値は ReviewResult.
#
# plan      : 検証済みの EditPlan
# scenes    : RankedScene のリスト (critic へ渡す評価。viewer へは渡さない)
# provider  : None ならレビューしない (機械の検査だけ行う)
# input_path: フレームを抜く元 VOD ("" なら画像なし)
def review(plan, bundle, scenes, provider, settings, target, input_path="",
           workdir="", report=None, extract=None, cancel_cb=None):
    cfg = ai_config.ai_config(settings)["review"]
    report = report if report is not None else PlanReport()
    checks = qa_checks(plan, settings, target)
    current = copy.deepcopy(plan)

    if provider is None or not cfg.get("enabled"):
        return ReviewResult(plan=current, checks=checks, report=report)

    issues = []
    applied = []
    rounds = max(int(cfg.get("max_rounds") or 0), 0)
    done = 0
    for round_index in range(rounds):
        if cancel_cb is not None and cancel_cb():
            break
        found = _ask_roles(current, bundle, scenes, provider, settings, target,
                           checks, input_path, workdir, report, extract)
        issues.extend(found)
        chosen = select_issues(found, settings)
        if not chosen:
            # 直すところが無い = そこで止める (無駄に回さない)。
            done = round_index + 1
            break
        current, taken = apply_issues(current, chosen, settings, report)
        applied.extend(taken)
        done = round_index + 1
        if not taken:
            break

    _logger.info("レビュー: %d 往復 / 指摘 %d 件 / 採用 %d 件 / 機械の検査 %d 件",
                 done, len(issues), len(applied), len(checks))

    return ReviewResult(plan=current, issues=issues, applied=applied, rounds=done,
                        checks=checks, report=report)


# ---- 役ごとの呼び出し (§12.1) -------------------------------------------

def _ask_roles(plan, bundle, scenes, provider, settings, target, checks,
               input_path, workdir, report, extract):
    cfg = ai_config.ai_config(settings)
    roles = cfg["review"].get("roles") or {}
    scene_ids = [scene["id"] for scene in plan.get("scenes") or []]
    system = prompts.render(prompts.REVIEW, language=_language(settings),
                            max_chars=int(cfg["scene_eval"]["max_reason_chars"]))

    issues = []
    for role in ROLES:
        if not roles.get(role):
            continue
        images = []
        if role == ROLE_DIRECTOR and input_path:
            images = vision.frames_for_plan(
                input_path, plan, settings, scenes=scenes, workdir=workdir,
                per_scene=_FRAMES_PER_ROLE.get(role, 0), extract=extract)
        request = AiRequest(
            role=ai_config.ROLE_REVIEW, system=system,
            payload=_payload(role, plan, bundle, scenes, target, checks, settings),
            schema=plan_schema.review_schema(
                scene_ids, max_detail_chars=int(cfg["scene_eval"]["max_reason_chars"])),
            images=images, cache_prefix_id=_CACHE_PREFIX,
            model_key=_MODEL_KEYS.get(role, ai_config.ROLE_REVIEW),
            max_output_tokens=cfg["max_output_tokens"])
        try:
            completion = provider.complete_json(request)
            found, notes = plan_schema.normalize_review(
                completion.result, scene_ids=scene_ids, role=role,
                max_detail_chars=int(cfg["scene_eval"]["max_reason_chars"]))
        except AiError as e:
            # 1 役が落ちても他の役は続ける (§11.2 の縮退)。
            report.add("R2", role, plan_schema.ACTION_DROP,
                       getattr(e, "code", None) or type(e).__name__)
            _logger.warning("レビュー (%s) に失敗しました: %s", role, e)
            continue
        report.extend(notes)
        issues.extend(found)

    return issues


# 役ごとの payload。**前置きは共通で、ここだけが役によって変わる** (§12.1)。
def _payload(role, plan, bundle, scenes, target, checks, settings):
    payload = {
        "role": role,
        "role_instruction": prompts.REVIEW_ROLES.get(role, ""),
        "target": target,
        "edit_plan": {"scenes": plan.get("scenes") or [],
                      "subtitles": plan.get("subtitles") or [],
                      "sounds": plan.get("sounds") or [],
                      "notes": plan.get("notes", "")},
        "total_sec": round(sum(float(scene["source_end"]) - float(scene["source_start"])
                               for scene in plan.get("scenes") or []), 1),
        "limits": _target_limits(settings, target),
    }
    if role != ROLE_VIEWER:
        # viewer には予備知識を渡さない (初見で読ませる / §12.1)。
        payload["scene_scores"] = [
            {"scene_ref": scene.index, "scores": scene.scores,
             "summary": scene.summary} for scene in scenes or []]
    if role in (ROLE_CRITIC, ROLE_VIEWER):
        payload["transcript"] = _transcript_for(plan, bundle)
    if role == ROLE_QA:
        # qa へは機械の測定結果だけを渡す (§12.2)。
        payload["measurements"] = checks
    presets = (ai_config.ai_config(settings)["sounds"].get("presets") or {})
    payload["sound_presets"] = sorted(presets.keys())

    return payload


def _transcript_for(plan, bundle):
    lines = []
    for scene in plan.get("scenes") or []:
        start = float(scene["source_start"])
        end = float(scene["source_end"])
        lines.append({
            "scene_id": scene["id"],
            "lines": [item for item in (bundle.transcript if bundle else [])
                      if float(item["end"]) > start and float(item["start"]) < end],
        })

    return lines


def _target_limits(settings, target):
    values = ai_config.ai_config(settings)["plan"].get(target) or {}

    return {"min_sec": float(values.get("min_sec") or 0.0),
            "max_sec": float(values.get("max_sec") or 0.0),
            "max_scenes": int(values.get("max_scenes") or 0)}


# ---- §12.2 機械で測れるものは機械で測る --------------------------------

# EditPlan から測れる問題を返す。AI へ渡すのは「測った結果」だけにする。
#
# ここで測るのは時間と本数の話に限る (音量の実測は素材が要るため書き出し時の
# 既存の検証に任せる / modules/loudness_normalizer)。
def qa_checks(plan, settings, target):
    limits = _target_limits(settings, target)
    scenes = plan.get("scenes") or []
    checks = []

    total = sum(float(scene["source_end"]) - float(scene["source_start"])
                for scene in scenes)
    if limits["max_sec"] and total > limits["max_sec"]:
        checks.append(_check("duration", "major",
                             "合計 {0:.1f} 秒が上限 {1:.0f} 秒を超えている".format(
                                 total, limits["max_sec"])))
    if limits["min_sec"] and total < limits["min_sec"]:
        checks.append(_check("duration", "minor",
                             "合計 {0:.1f} 秒が目安 {1:.0f} 秒に届いていない".format(
                                 total, limits["min_sec"])))

    by_scene = {}
    for item in plan.get("subtitles") or []:
        by_scene.setdefault(item["scene_id"], []).append(item)

    for scene in scenes:
        length = float(scene["source_end"]) - float(scene["source_start"])
        if length < 1.0:
            checks.append(_check("scene", "major",
                                 "シーン {0} が {1:.2f} 秒しかない".format(
                                     scene["id"], length),
                                 scene_id=scene["id"]))
        subtitles = sorted(by_scene.get(scene["id"]) or [],
                           key=lambda item: item["source_start"])
        checks.extend(_subtitle_checks(scene, subtitles))

    sounds = plan.get("sounds") or []
    if len(sounds) > 0:
        checks.append(_check("sound", "minor",
                             "効果音が {0} 件ある".format(len(sounds))))

    return checks


def _subtitle_checks(scene, subtitles):
    checks = []
    cursor = None
    for item in subtitles:
        start = float(item["source_start"])
        end = float(item["source_end"])
        length = end - start
        if cursor is not None and start < cursor - 0.0015:
            checks.append(_check("subtitle", "major",
                                 "テロップが {0:.1f}s で重なっている".format(start),
                                 scene_id=scene["id"], at=start))
        if length < _SUBTITLE_MIN_SEC:
            checks.append(_check("subtitle", "minor",
                                 "テロップが {0:.2f} 秒しか出ない".format(length),
                                 scene_id=scene["id"], at=start))
        if length > _SUBTITLE_MAX_SEC:
            checks.append(_check("subtitle", "minor",
                                 "テロップが {0:.1f} 秒出続ける".format(length),
                                 scene_id=scene["id"], at=start))
        cursor = end

    return checks


def _check(kind, severity, detail, scene_id="", at=None):
    return {"kind": str(kind), "severity": str(severity), "detail": str(detail),
            "scene_id": str(scene_id or ""),
            "at": round(float(at), 1) if at is not None else None}


# ---- §12.4 採る指摘を絞る ----------------------------------------------

# 重大度の下限と件数で絞り、優先度順に返す。
def select_issues(issues, settings):
    cfg = ai_config.ai_config(settings)["review"]
    minimum = str(cfg.get("min_severity") or "major")
    limit = max(int(cfg.get("max_issues_per_round") or 0), 0)

    chosen = [issue for issue in issues
              if plan_schema.meets_severity(issue["severity"], minimum)
              and issue["fix"]["op"] != "none"]
    chosen.sort(key=lambda issue: (issue["priority"],
                                   -plan_schema.SEVERITY_RANK.get(
                                       issue["severity"], 0)))

    return chosen[:limit] if limit else chosen


# ---- 修正を EditPlan へ当てる ------------------------------------------

# 指摘の fix を EditPlan へ当てる。返り値は (新しい plan, 当てた指摘).
#
# **決定的な操作だけを行う。** 当てられない指摘は当てずに記録へ残す
# (無理に解釈して別の編集をしない)。
def apply_issues(plan, issues, settings, report=None):
    report = report if report is not None else PlanReport()
    updated = copy.deepcopy(plan)
    applied = []
    for issue in issues:
        if _apply_one(updated, issue, settings, report):
            applied.append(issue)
        else:
            report.add("R4", issue["id"], plan_schema.ACTION_DROP,
                       issue["fix"]["op"])

    return updated, applied


def _apply_one(plan, issue, settings, report):
    fix = issue["fix"]
    op = fix["op"]
    scene = _scene_of(plan, fix.get("target") or issue.get("scene_id"))

    if op == "trim":
        return _apply_trim(plan, scene, fix, report, issue)
    if op == "drop":
        return _apply_drop(plan, scene)
    if op == "reorder":
        return _apply_reorder(plan, fix)
    if op == "edit_subtitle":
        return _apply_edit_subtitle(plan, scene, fix)
    if op == "add_subtitle":
        return _apply_add_subtitle(plan, scene, fix)
    if op == "add_sound":
        return _apply_add_sound(plan, scene, fix, settings)
    if op == "set_animation":
        return _apply_set_animation(plan, scene, fix)

    return False


def _apply_trim(plan, scene, fix, report, issue):
    if scene is None:
        return False
    start = fix["source_start"]
    end = fix["source_end"]
    if start is None or end is None or end <= start:
        return False
    # 元の区間の中へしか詰められない (伸ばす指摘は当てない = 素材が無い)。
    if start < scene["source_start"] - 1e-6 or end > scene["source_end"] + 1e-6:
        report.add("R4", issue["id"], plan_schema.ACTION_DROP, "outside")

        return False
    scene["source_start"] = round(float(start), 3)
    scene["source_end"] = round(float(end), 3)
    plan["subtitles"] = [item for item in plan.get("subtitles") or []
                         if item["scene_id"] != scene["id"]
                         or (float(item["source_end"]) > scene["source_start"]
                             and float(item["source_start"]) < scene["source_end"])]

    return True


def _apply_drop(plan, scene):
    if scene is None or len(plan.get("scenes") or []) <= 1:
        return False
    plan["scenes"] = [item for item in plan["scenes"] if item["id"] != scene["id"]]
    plan["subtitles"] = [item for item in plan.get("subtitles") or []
                         if item["scene_id"] != scene["id"]]
    plan["sounds"] = [item for item in plan.get("sounds") or []
                      if item["scene_id"] != scene["id"]]
    for position, item in enumerate(plan["scenes"]):
        item["order"] = position + 1

    return True


def _apply_reorder(plan, fix):
    order = [int(value) for value in fix.get("order") or []]
    scenes = plan.get("scenes") or []
    if sorted(order) != sorted(scene["order"] for scene in scenes):
        return False
    by_order = {scene["order"]: scene for scene in scenes}
    plan["scenes"] = [by_order[value] for value in order]
    for position, scene in enumerate(plan["scenes"]):
        scene["order"] = position + 1

    return True


def _apply_edit_subtitle(plan, scene, fix):
    text = str(fix.get("text") or "").strip()
    if scene is None or not text:
        return False
    target = _subtitle_at(plan, scene, fix)
    if target is None:
        return False
    target["text"] = text

    return True


def _apply_add_subtitle(plan, scene, fix):
    text = str(fix.get("text") or "").strip()
    start = fix["source_start"]
    end = fix["source_end"]
    if scene is None or not text or start is None or end is None or end <= start:
        return False
    if start < scene["source_start"] - 1e-6 or end > scene["source_end"] + 1e-6:
        return False
    plan.setdefault("subtitles", []).append({
        "scene_id": scene["id"], "source_start": round(float(start), 3),
        "source_end": round(float(end), 3), "text": text,
        "track_role": plan_schema.DEFAULT_TRACK_ROLE, "animation": "",
        "fade_in": False, "fade_out": False, "color": "", "align": "",
        "reason": "レビューの指摘"})

    return True


def _apply_add_sound(plan, scene, fix, settings):
    preset = str(fix.get("preset") or "").strip()
    presets = ai_config.ai_config(settings)["sounds"].get("presets") or {}
    if scene is None or not preset or preset not in presets:
        # 手元に無い音の提案は EditPlan へ入れない。PDF の提案として残す (§12.6)。
        return False
    offset = fix["source_start"]
    at = (float(offset) - scene["source_start"]) if offset is not None else 0.0
    if at < 0 or at >= scene["source_end"] - scene["source_start"]:
        return False
    plan.setdefault("sounds", []).append({
        "scene_id": scene["id"], "offset_sec": round(at, 3), "preset": preset,
        "gain_db": 0.0, "reason": "レビューの指摘"})

    return True


def _apply_set_animation(plan, scene, fix):
    animation = str(fix.get("animation") or "").strip()
    if scene is None or (animation and animation not in plan_schema.ANIMATIONS):
        return False
    target = _subtitle_at(plan, scene, fix)
    if target is None:
        return False
    if plan_schema.is_comment_role(target["track_role"]):
        # コメント役割に動きは付かない (§9.4)。
        return False
    target["animation"] = animation

    return True


def _scene_of(plan, target):
    key = str(target or "")
    for scene in plan.get("scenes") or []:
        if scene["id"] == key:
            return scene

    return None


# 指摘が指すテロップ (時刻が指定されていればその中で最も近いもの)。
def _subtitle_at(plan, scene, fix):
    candidates = [item for item in plan.get("subtitles") or []
                  if item["scene_id"] == scene["id"]]
    if not candidates:
        return None
    at = fix.get("source_start")
    if at is None:
        return candidates[0]

    return min(candidates,
               key=lambda item: abs(float(item["source_start"]) - float(at)))


def _language(settings):
    value = str((settings or {}).get("subtitle", {}).get("language", "ja") or "ja")

    return "日本語" if value.startswith("ja") else value
