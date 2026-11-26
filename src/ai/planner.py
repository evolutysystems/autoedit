# 動画の企画・構成と編集指示 (docs/AiEditor/resolve1.md §8 / §9 / R5)
#
# 2 段に分ける。
#   ① VideoPlan  どのシーンをどの順で、どんな狙いで見せるか (§8.1)
#   ② EditPlan   それを既存の編集エンジンへの指示へ落とす (§9)
#
# 長尺と短尺は**別々に呼ぶ** (§8.1)。同じ VideoPlan を使い回さない。
# どちらの呼び出しも失敗したら**AI 無しで組んだ案へ落として完走する** (§11.2 の縮退)。
# 落ちた段は PlanReport に残り、画面には件数だけが出る。
#
# 時刻は元 VOD の秒。EditPlan の project はこちらで埋める (モデルに素材の情報を
# 作らせない)。形の検証と素材との整合は plan_validator が見る。
from ..exceptions import AiError
from ..utils.logger import get_logger
from . import config as ai_config
from . import plan_schema, prompts
from .plan_validator import PlanReport
from .provider import AiRequest
from .scene_finder import USE_LONG, USE_SHORTS

_logger = get_logger(__name__)

# 前置きを使い回す意思表示 (§6.4)。役と版が同じなら前方一致でキャッシュへ乗る。
_CACHE_PLAN = "plan-v1"
_CACHE_EDIT = "edit_plan-v1"

# ターゲットごとの用途 (重みの組 / §7.3)
_USE_OF_TARGET = {
    plan_schema.TARGET_YOUTUBE_LONG: USE_LONG,
    plan_schema.TARGET_YOUTUBE_SHORTS: USE_SHORTS,
    plan_schema.TARGET_TIKTOK: USE_SHORTS,
}


# ターゲットに対応する重みの組。
def use_of(target):
    return _USE_OF_TARGET.get(target, USE_LONG)


# 構成 → 編集指示までを通して作る。返り値は (EditPlan, PlanReport).
#
# provider が None なら AI を呼ばず、評価の順位だけで組む (縮退)。
def build(bundle, scenes, provider, settings, target, report=None, model=""):
    report = report if report is not None else PlanReport()
    limits = _limits(settings, target)
    usable = [scene for scene in scenes if scene.duration_sec > 0]
    if not usable:
        return None, report

    video_plan = None
    if provider is not None:
        video_plan = _ask_video_plan(bundle, usable, provider, settings, target,
                                     limits, report)
    if video_plan is None:
        video_plan = fallback_video_plan(usable, settings, target)
        report.add("P4", "video_plan", plan_schema.ACTION_DEFAULT, "local")

    edit_plan = None
    if provider is not None:
        edit_plan = _ask_edit_plan(bundle, usable, video_plan, provider, settings,
                                   target, report, model=model)
    if edit_plan is None:
        edit_plan = fallback_edit_plan(bundle, usable, video_plan, settings, target)
        report.add("P4", "edit_plan", plan_schema.ACTION_DEFAULT, "local")

    _logger.info("構成: %s / section %d 件 → EditPlan のシーン %d 件 (%s)",
                 video_plan.get("title", ""), len(video_plan.get("sections") or []),
                 len(edit_plan.get("scenes") or []), target)

    return edit_plan, report


# ---- ① VideoPlan (§8.1) -------------------------------------------------

def _ask_video_plan(bundle, scenes, provider, settings, target, limits, report):
    cfg = ai_config.ai_config(settings)
    max_chars = int(cfg["scene_eval"]["max_reason_chars"])
    roles = plan_schema.SECTION_ROLE_ORDER.get(target, plan_schema.SCENE_ROLES)
    # 前置きは出力先に依らない (キャッシュを 3 出力先で共有する / §6.4)。
    # 出力先ごとに変わる値は payload へ入れる。
    system = prompts.render(prompts.PLAN, language=_language(settings),
                            max_chars=max_chars)
    request = AiRequest(
        role=ai_config.ROLE_PLAN, system=system,
        payload=_plan_payload(bundle, scenes, limits, target, roles),
        schema=plan_schema.video_plan_schema(target, max_reason_chars=max_chars),
        cache_prefix_id=_CACHE_PLAN, max_output_tokens=cfg["max_output_tokens"])
    try:
        completion = provider.complete_json(request)
        video_plan, notes = plan_schema.normalize_video_plan(
            completion.result, target, max_reason_chars=max_chars)
    except AiError as e:
        report.add("P4", "video_plan", plan_schema.ACTION_DROP,
                   getattr(e, "code", None) or type(e).__name__)
        _logger.warning("構成の呼び出しに失敗したため手元で組みます: %s", e)

        return None
    report.extend(notes)

    return _fit_sections(video_plan, scenes, limits, report)


def _plan_payload(bundle, scenes, limits, target, roles=()):
    return {
        "source": {"video_id": str(bundle.source.get("video_id", "") or ""),
                   "title": str(bundle.source.get("title", "") or ""),
                   "game": str(bundle.source.get("game", "") or ""),
                   "duration_sec": bundle.duration_sec},
        "target": target,
        # 役割の並び。前置きではなくここへ入れる (出力先ごとに変わるため / §6.4)
        "section_roles": list(roles),
        "limits": limits,
        "profile": bundle.profile,
        "scenes": [_scene_payload(bundle, scene) for scene in scenes],
    }


def _scene_payload(bundle, scene):
    return {
        "scene_ref": scene.index,
        "start": scene.start,
        "end": scene.end,
        "duration_sec": round(scene.duration_sec, 1),
        "scores": scene.scores,
        "summary": scene.summary,
        "reason": scene.reason,
        "recommended_use": scene.recommended_use,
        "peak": scene.peak,
        "needed_context": scene.needed_context,
        "transcript": _transcript_in(bundle, scene.start, scene.end),
    }


# 構成の区間をシーンの範囲へ収める。範囲外の section は落とす (§8.1 の検証は §10 だが、
# 「存在しないシーンを指した」のはここで分かるため先に落とす)。
def _fit_sections(video_plan, scenes, limits, report):
    by_ref = {scene.index: scene for scene in scenes}
    sections = []
    for position, section in enumerate(video_plan.get("sections") or []):
        where = "sections[{0}]".format(position)
        scene = by_ref.get(section.get("scene_ref"))
        if scene is None:
            scene = _nearest(scenes, section.get("start"))
            if scene is None:
                report.add("P4", where, plan_schema.ACTION_DROP, "scene_ref")
                continue
            section["scene_ref"] = scene.index
            report.add("P4", where + ".scene_ref", plan_schema.ACTION_DEFAULT,
                       str(scene.index))
        start = min(max(float(section["start"]), scene.start), scene.end)
        end = min(max(float(section["end"]), scene.start), scene.end)
        if end - start <= 0:
            start, end = scene.start, scene.end
            report.add("P4", where, plan_schema.ACTION_DEFAULT, "range")
        elif start != section["start"] or end != section["end"]:
            report.add("P4", where, plan_schema.ACTION_CUT, "range")
        section["start"] = round(start, 3)
        section["end"] = round(end, 3)
        sections.append(section)

    video_plan["sections"] = sections or fallback_video_plan(
        scenes, None, video_plan["target"], limits=limits)["sections"]

    return video_plan


# 時刻が近いシーン (scene_ref を外したときの受け皿)。
def _nearest(scenes, start):
    try:
        value = float(start)
    except (TypeError, ValueError):
        return None

    return min(scenes, key=lambda scene: abs(scene.start - value), default=None)


# AI 無しで組む構成 (縮退)。順位の上から尺が埋まるまで並べ、役割を割り当てる。
def fallback_video_plan(scenes, settings, target, limits=None):
    limits = limits or _limits(settings, target)
    roles = plan_schema.SECTION_ROLE_ORDER.get(target, plan_schema.SCENE_ROLES)
    chosen = []
    total = 0.0
    for scene in scenes:
        if limits["max_scenes"] and len(chosen) >= limits["max_scenes"]:
            break
        if limits["max_sec"] and total + scene.duration_sec > limits["max_sec"]:
            continue
        chosen.append(scene)
        total += scene.duration_sec
    if not chosen and scenes:
        chosen = [scenes[0]]

    # 時間順に並べ替える (話の流れを壊さない)。先頭と末尾だけ役割を分ける。
    chosen.sort(key=lambda scene: scene.start)
    sections = []
    for position, scene in enumerate(chosen):
        sections.append({
            "role": _role_for(roles, position, len(chosen)),
            "scene_ref": scene.index,
            "start": scene.start,
            "end": scene.end,
            "reason": scene.summary or "",
        })

    return {
        "target": target,
        "title": (chosen[0].summary if chosen else "")[:40],
        "concept": "既存の採点と AI の評価で上位のシーンを時間順に並べた",
        "duration_sec": round(total, 1),
        "sections": sections,
        "direction": {"subtitle": "full", "emphasis_scenes": [],
                      "sound_effects": "light", "bgm": "none",
                      "vertical_crop": target in plan_schema.VERTICAL_TARGETS},
    }


def _role_for(roles, position, count):
    if not roles:
        return plan_schema.DEFAULT_SCENE_ROLE
    if position == 0:
        return roles[0]
    if position == count - 1:
        return roles[-1]
    middle = [role for role in roles if role in ("body", "context", "intro")]

    return middle[0] if middle else plan_schema.DEFAULT_SCENE_ROLE


# ---- ② EditPlan (§9) ----------------------------------------------------

def _ask_edit_plan(bundle, scenes, video_plan, provider, settings, target, report,
                   model=""):
    cfg = ai_config.ai_config(settings)
    max_chars = int(cfg["scene_eval"]["max_reason_chars"])
    presets = sorted((cfg["sounds"].get("presets") or {}).keys()) \
        if cfg["sounds"].get("enabled") else []
    # 前置きは出力先に依らない (VideoPlan と同じ / §6.4)。target は payload にある。
    # テロップの扱い (ai.plan.subtitle_mode)。前置きが変わるため、
    # キャッシュの前方一致の鍵にもモードを入れる (混ぜない)。
    mode = plan_schema.subtitle_mode(cfg["plan"].get("subtitle_mode"))
    system = prompts.render(
        prompts.EDIT_PLAN, language=_language(settings),
        animations=", ".join(plan_schema.ANIMATIONS),
        presets=", ".join(presets) if presets else "(なし)",
        subtitle_rule=prompts.subtitle_rule(mode))
    request = AiRequest(
        role=ai_config.ROLE_PLAN, system=system,
        payload=_edit_payload(bundle, scenes, video_plan, target),
        schema=plan_schema.edit_plan_schema(target, max_reason_chars=max_chars),
        cache_prefix_id=_CACHE_EDIT + "-" + mode,
        max_output_tokens=cfg["max_output_tokens"])
    try:
        completion = provider.complete_json(request)
    except AiError as e:
        report.add("P4", "edit_plan", plan_schema.ACTION_DROP,
                   getattr(e, "code", None) or type(e).__name__)
        _logger.warning("編集指示の呼び出しに失敗したため手元で組みます: %s", e)

        return None

    return _merge_edit_plan(completion.result, bundle, settings, target,
                            model=completion.model or model)


def _edit_payload(bundle, scenes, video_plan, target):
    by_ref = {scene.index: scene for scene in scenes}
    sections = []
    for section in video_plan.get("sections") or []:
        scene = by_ref.get(section.get("scene_ref"))
        sections.append({
            "role": section.get("role"),
            "scene_ref": section.get("scene_ref"),
            "start": section.get("start"),
            "end": section.get("end"),
            "reason": section.get("reason", ""),
            "summary": scene.summary if scene else "",
            "peak": scene.peak if scene else None,
            "transcript": _transcript_in(bundle, section.get("start"),
                                         section.get("end")),
        })

    return {
        "source": {"video_id": str(bundle.source.get("video_id", "") or ""),
                   "duration_sec": bundle.duration_sec},
        "target": target,
        "video_plan": {"title": video_plan.get("title", ""),
                       "concept": video_plan.get("concept", ""),
                       "direction": video_plan.get("direction", {})},
        "sections": sections,
    }


# モデルの応答から EditPlan を組む。project は**こちらで埋める** (§9.1)。
def _merge_edit_plan(result, bundle, settings, target, model=""):
    data = result if isinstance(result, dict) else {}
    plan = plan_schema.new_plan(
        target,
        {"kind": "vod" if bundle.source.get("video_id") else "local",
         "video_id": bundle.source.get("video_id", ""), "path": ""},
        duration_sec=bundle.duration_sec, model=model)
    plan["scenes"] = data.get("scenes") or []
    plan["subtitles"] = data.get("subtitles") or []
    plan["sounds"] = data.get("sounds") or []
    editing = data.get("editing")
    if isinstance(editing, dict):
        plan["editing"].update({key: value for key, value in editing.items()
                                if key in plan["editing"]})
    plan["notes"] = str(data.get("notes", "") or "")
    if not plan["scenes"]:
        return None

    return plan


# AI 無しで組む編集指示 (縮退)。
#
# シーンの区間と並びだけを指示する。テロップは**既存の自動生成がそのまま残る**ため
# 何も書かない (本文は文字起こしが正 / §19 の 6)。SE も付けない。
def fallback_edit_plan(bundle, scenes, video_plan, settings, target):
    plan = plan_schema.new_plan(
        target,
        {"kind": "vod" if bundle.source.get("video_id") else "local",
         "video_id": bundle.source.get("video_id", ""), "path": ""},
        duration_sec=bundle.duration_sec)
    plan["scenes"] = [
        {"id": "sc{0}".format(position + 1),
         "source_start": float(section["start"]),
         "source_end": float(section["end"]),
         "role": section.get("role") or plan_schema.DEFAULT_SCENE_ROLE,
         "order": position + 1,
         "reason": str(section.get("reason", "") or "")}
        for position, section in enumerate(video_plan.get("sections") or [])]
    plan["notes"] = str(video_plan.get("concept", "") or "")

    return plan


# ---- 小物 ----------------------------------------------------------------

def _limits(settings, target):
    cfg = ai_config.ai_config(settings)["plan"]
    values = cfg.get(target) or {}

    return {"min_sec": float(values.get("min_sec") or 0.0),
            "max_sec": float(values.get("max_sec") or 0.0),
            "max_scenes": int(values.get("max_scenes") or 0)}


def _transcript_in(bundle, start, end):
    try:
        low = float(start)
        high = float(end)
    except (TypeError, ValueError):
        return []

    return [{"start": item["start"], "end": item["end"], "text": item["text"]}
            for item in bundle.transcript
            if float(item["end"]) > low and float(item["start"]) < high]


def _language(settings):
    value = str((settings or {}).get("subtitle", {}).get("language", "ja") or "ja")

    return "日本語" if value.startswith("ja") else value
