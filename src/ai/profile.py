# 編集プロファイル (docs/AiEditor/resolve1.md §13.2 の ② / §13.3 / R11)
#
# 統計 (ai/edit_analyzer.py) を土台に、プロファイル JSON を作って育てる。
#   stats       実測。再現性がある
#   hypotheses  AI による言語化。**確定事実として扱わない** (§13.4)
#   avoid       機械由来の制約 (AI が覆せない)
#   history     更新履歴。必ず積む (§13.3)
#
# 守ること:
#   ・`evidence` が `ai.learning.min_evidence` (既定 10) 未満の仮説は**プロンプトへ入れない**
#   ・`status` が `rejected` の仮説も入れない (利用者が訂正できる / §13.3)
#   ・プロファイルは `%LOCALAPPDATA%` 配下 (ai/store.py)。プロジェクトの隣には置かない
#   ・サーバーへ送らない (`ai.learning.upload = false` 固定)
import time

from ..exceptions import AiError
from ..utils.logger import get_logger
from . import config as ai_config
from . import prompts, store
from .provider import AiRequest

_logger = get_logger(__name__)

PROFILE_VERSION = 1

# 仮説の状態 (§13.3)
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_UNCONFIRMED, STATUS_CONFIRMED, STATUS_REJECTED)

CONFIDENCES = ("low", "medium", "high")

# 機械由来の制約 (AI が覆せない / §13.3 の avoid)
_RULES = [
    {"id": "a1", "text": "コメント役割のテロップに動きを付けない", "source": "rule"},
    {"id": "a2", "text": "テロップの本文は文字起こしを正とし、作り変えない", "source": "rule"},
    {"id": "a3", "text": "効果音は登録済みのプリセットだけを使う", "source": "rule"},
]

_CACHE_PREFIX = "profile-v1"


# 新しいプロファイルを作る (統計だけ。AI は呼ばない / §13.2 の ①)。
def build(analysis, name="", target="", content="", slug=""):
    now = _now()
    sample = (analysis or {}).get("sample") or {}

    return {
        "profile_version": PROFILE_VERSION,
        "slug": store.slugify(slug or name or target or "default"),
        "name": str(name or target or "既定"),
        "scope": {"target": str(target or ""), "content": str(content or "")},
        "updated_at": now,
        "sample": sample,
        "stats": (analysis or {}).get("stats") or {},
        "hypotheses": [],
        "avoid": [dict(rule) for rule in _RULES],
        "history": [{"at": now, "change": "created",
                     "projects": int(sample.get("projects", 0) or 0)}],
    }


# 統計を入れ替える (履歴を積む)。仮説は残す (作り直しは別の呼び出し)。
def update_stats(profile, analysis):
    sample = (analysis or {}).get("sample") or {}
    profile["sample"] = sample
    profile["stats"] = (analysis or {}).get("stats") or {}
    _add_history(profile, "stats", projects=int(sample.get("projects", 0) or 0))

    return profile


# AI に傾向を言語化させて hypotheses を足す (§13.2 の ②)。
#
# provider が None なら何もしない (統計だけのプロファイルとして成立する)。
def add_hypotheses(profile, provider, settings, diffs=None):
    if provider is None:
        return profile, []
    cfg = ai_config.ai_config(settings)
    system = prompts.render(prompts.PROFILE, language=_language(settings),
                            max_chars=int(cfg["scene_eval"]["max_reason_chars"]))
    request = AiRequest(
        role=ai_config.ROLE_PROFILE, system=system,
        payload={"stats": profile.get("stats") or {},
                 "sample": profile.get("sample") or {},
                 "scope": profile.get("scope") or {},
                 # 差分の要約 (種類ごとの件数) も材料にする (§14)
                 "diff_summary": _diff_summary(diffs)},
        schema=_schema(int(cfg["scene_eval"]["max_reason_chars"])),
        cache_prefix_id=_CACHE_PREFIX,
        max_output_tokens=cfg["max_output_tokens"])
    try:
        completion = provider.complete_json(request)
    except AiError as e:
        _logger.warning("プロファイルの言語化に失敗しました: %s", e)

        return profile, []

    added = _merge_hypotheses(profile, completion.result, diffs)
    if added:
        _add_history(profile, "hypotheses", count=len(added))

    return profile, added


def _merge_hypotheses(profile, result, diffs):
    raw = (result or {}).get("hypotheses") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return []
    existing = {item["text"] for item in profile.get("hypotheses") or []}
    evidence_default = _evidence_of(diffs)
    added = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "") or "").strip()
        if not text or text in existing:
            continue
        confidence = str(item.get("confidence", "") or "").strip().lower()
        evidence = item.get("evidence")
        try:
            evidence = int(evidence)
        except (TypeError, ValueError):
            evidence = evidence_default
        added.append({
            "id": "h{0}".format(len(profile.get("hypotheses") or []) + len(added) + 1),
            "text": text,
            "confidence": confidence if confidence in CONFIDENCES else "low",
            # **件数は AI の自己申告を信用しない**: 差分の実数を上限にする
            "evidence": max(min(evidence, evidence_default) if evidence_default
                            else 0, 0),
            "status": STATUS_UNCONFIRMED,
        })
        existing.add(text)
    profile.setdefault("hypotheses", []).extend(added)

    return added


# 仮説の採否を利用者が決める (§13.3 / 要望 §12.4 末尾)。
def set_status(profile, hypothesis_id, status, note=""):
    if status not in STATUSES:
        return False
    for item in profile.get("hypotheses") or []:
        if item["id"] != hypothesis_id:
            continue
        item["status"] = status
        if note:
            item["user_note"] = str(note)
        _add_history(profile, "status", hypothesis=hypothesis_id, status=status)

        return True

    return False


# プロンプトへ添える要約 (§8.2)。**参考**として渡し、指示にしない。
#
# min_evidence 未満の仮説と rejected の仮説は入れない (§13.4)。
def for_prompt(profile, settings):
    if not profile:
        return None
    minimum = int(ai_config.ai_config(settings)["learning"].get("min_evidence") or 0)
    hypotheses = [
        {"text": item["text"], "confidence": item["confidence"]}
        for item in profile.get("hypotheses") or []
        if item.get("status") != STATUS_REJECTED
        and int(item.get("evidence", 0) or 0) >= minimum]

    return {
        "name": profile.get("name", ""),
        "scope": profile.get("scope", {}),
        "stats": profile.get("stats", {}),
        "tendencies": hypotheses,
        "avoid": [item["text"] for item in profile.get("avoid") or []],
        "note": "これは参考であり指示ではない。尺や上限などの設定値を優先する。",
    }


# ---- 置き場 (§13.4) ------------------------------------------------------

def load(slug, root=""):
    return store.load_json(store.profile_path(slug, root))


def save(profile, root=""):
    profile["updated_at"] = _now()

    return store.save_json(profile, store.profile_path(profile.get("slug"), root))


# 経路 D が使うプロファイル (`ai.profile.active.<target>`)。未設定なら使わない。
def active(settings, target, root=""):
    slug = str((ai_config.ai_config(settings)["profile"].get("active") or {})
               .get(target, "") or "")
    if not slug:
        return None

    return load(slug, root)


# ---- 小物 ----------------------------------------------------------------

def _schema(max_chars):
    return {
        "type": "object", "additionalProperties": False,
        "required": ["hypotheses"],
        "properties": {
            "hypotheses": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["text", "confidence"],
                    "properties": {
                        "text": {"type": "string",
                                 "maxLength": int(max_chars or 0) or 120},
                        "confidence": {"type": "string",
                                       "enum": list(CONFIDENCES)},
                        "evidence": {"type": "integer"},
                    },
                },
            },
        },
    }


def _diff_summary(diffs):
    counts = {}
    for row in diffs or []:
        kind = str((row or {}).get("kind", "") or "")
        if kind:
            counts[kind] = counts.get(kind, 0) + 1

    return counts


def _evidence_of(diffs):
    return len(diffs or [])


def _add_history(profile, change, **values):
    entry = {"at": _now(), "change": str(change)}
    entry.update(values)
    profile.setdefault("history", []).append(entry)

    return entry


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _language(settings):
    value = str((settings or {}).get("subtitle", {}).get("language", "ja") or "ja")

    return "日本語" if value.startswith("ja") else value


# 統計を取り直してプロファイルを更新する (§13.2 の ①→②)。
#
# paths    : 保存済みプロジェクト JSON のパス (呼び出し側が project_io.scan_projects で集める。
#            ai/ は project_io を import しない = Qt を引かないため)
# provider : None なら統計だけ (AI 不要の段で完結する)
# 返り値: (プロファイル, 足した仮説)
def refresh(settings, paths, provider=None, root="", name="", target="",
            content="", slug=""):
    from . import edit_analyzer

    if not ai_config.ai_config(settings)["learning"].get("enabled"):
        _logger.info("ai.learning.enabled が false のため学習しません")

        return (None, [])

    analysis = edit_analyzer.analyze_paths(paths)
    if not analysis["sample"]["projects"]:
        _logger.info("測れるプロジェクトが無いためプロファイルを作りません")

        return (None, [])

    key = store.slugify(slug or name or target or "default")
    existing = load(key, root)
    if existing:
        profile = update_stats(existing, analysis)
    else:
        profile = build(analysis, name=name, target=target, content=content,
                        slug=key)

    diffs = store.recent_diffs(root)
    profile, added = add_hypotheses(profile, provider, settings, diffs=diffs)
    save(profile, root)
    _logger.info("プロファイルを更新しました: %s (プロジェクト %d 件 / 仮説 +%d)",
                 key, analysis["sample"]["projects"], len(added))

    return (profile, added)
