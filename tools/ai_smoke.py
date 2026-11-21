# AI 代行 (Stretheus API 経由) の疎通確認 (docs/AiEditor/resolve1.md §6)
#
# サーバー側 (StretheusAPI) の Ai:Enabled を true にしたときに、
# クライアントから本当に呼べるかを**最小の 1 回**で確かめるための開発用スクリプト。
# 配布物には含めない (tools/ 配下)。
#
#   python tools/ai_smoke.py                    構成を見る (GET /api/ai/config)
#   python tools/ai_smoke.py --count            見積りを見る (POST /api/ai/count)
#   python tools/ai_smoke.py --complete         1 回だけ判断させる (POST /api/ai/complete)
#   python tools/ai_smoke.py --complete --role plan
#   python tools/ai_smoke.py --complete --enable  setting.json を書き換えずに ai.enabled を有効にする
#
# 事前に `python -m src.services.login_check` でログインしておくこと。
# AI はサーバー側で既定「ログイン必須」であり、匿名セッションでは使えない。
#
# 鍵はクライアントに無い (サーバーだけが持つ)。このスクリプトも鍵を扱わない。
import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.ai import config as ai_config                      # noqa: E402
from src.ai import profile as ai_profile                    # noqa: E402
from src.ai.provider import AiRequest, StretheusProvider, should_degrade  # noqa: E402
from src.ai.usage_log import UsageTracker                   # noqa: E402
from src.exceptions import AiError, AutoEditError           # noqa: E402
from src.services.config import api_config                  # noqa: E402
from src.services.stretheus_auth import StretheusAuth       # noqa: E402
from src.settings.settings_window import load_settings      # noqa: E402

# 役割ごとの最小の入力。**本物の解析データは送らない** (疎通の確認だけが目的)。
_PAYLOADS = {
    "scene_eval": {
        "candidates": [
            {"id": "c1", "start_sec": 10.0, "end_sec": 25.0, "base_score": 7.2,
             "transcript": "ここで大きな笑いが起きた"},
            {"id": "c2", "start_sec": 120.0, "end_sec": 140.0, "base_score": 5.1,
             "transcript": "淡々と進行している場面"},
        ],
    },
    "plan": {
        "target": "youtube_shorts",
        "scenes": [{"id": "c1", "start_sec": 10.0, "end_sec": 25.0, "score": 8.0}],
    },
    "classify": {"text": "これはテロップの整形テストです"},
}

# 期待する JSON の形 (構造化出力)。サーバーがそのままモデルへ渡す。
_SCHEMAS = {
    "scene_eval": {
        "type": "object",
        "additionalProperties": False,
        "required": ["scenes"],
        "properties": {
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "funny", "reason"],
                    "properties": {
                        "id": {"type": "string"},
                        "funny": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    },
    "plan": {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "order"],
        "properties": {
            "title": {"type": "string"},
            "order": {"type": "array", "items": {"type": "string"}},
        },
    },
    "classify": {
        "type": "object",
        "additionalProperties": False,
        "required": ["label"],
        "properties": {"label": {"type": "string"}},
    },
}

_SYSTEMS = {
    "scene_eval": "Score each candidate scene for how funny it is from 0 to 10.",
    "plan": "Lay out a short video from the given scenes.",
    "classify": "Classify the given text into one short label.",
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="AI 代行 (Stretheus API 経由) の疎通を確認する")
    parser.add_argument("--base-url", help="接続先。省略時は setting.json の api.base_url")
    parser.add_argument("--role", choices=sorted(_PAYLOADS), default="scene_eval",
                        help="試す役割 (既定 scene_eval)")
    parser.add_argument("--complete", action="store_true", help="1 回だけ判断させる")
    parser.add_argument("--count", action="store_true", help="送る前の見積りを見る")
    parser.add_argument("--enable", action="store_true",
                        help="setting.json を書き換えず、この実行だけ ai.enabled を有効にする")
    parser.add_argument("--project-id", help="記録へ紐づけるプロジェクト ID (任意)")
    parser.add_argument("--learn", action="store_true",
                        help="保存済みプロジェクトから編集プロファイルを作り直す "
                             "(AI を呼ばずに統計だけでも作れる / §13)")
    parser.add_argument("--profile-name", default="既定",
                        help="--learn で作るプロファイルの名前")
    parser.add_argument("--profile-target", default="youtube_long",
                        help="--learn で作るプロファイルの対象 (既定 youtube_long)")
    args = parser.parse_args(argv)

    settings = load_settings(persist_new_keys=False)
    if args.enable:
        settings.setdefault("ai", {})["enabled"] = True

    # 学習 (統計) は AI を呼ばないため ai.enabled を要求しない (§13.2 の ①)。
    if args.learn:
        return _learn(settings, args)

    if not ai_config.is_enabled(settings):
        print("クライアント側の ai.enabled が false です。"
              "--enable を付けるか setting.json を変更してください。")
        return 1

    config = api_config(settings)
    base_url = args.base_url or config["base_url"]
    auth = StretheusAuth(base_url, config["timeout_sec"])
    print(f"接続先: {base_url}")

    if not auth.is_logged_in():
        print("未ログインです。先に `python -m src.services.login_check` を実行してください。")
        print("(AI はサーバー側で既定ログイン必須であり、匿名セッションでは使えません)")
        return 1

    user = auth.user() or {}
    print(f"ユーザー: {user.get('displayName')} (内部 ID {user.get('id')})")

    tracker = UsageTracker(settings, project_id=args.project_id)
    provider = StretheusProvider(auth, settings=settings, tracker=tracker)

    try:
        if not _show_config(provider):
            return 1

        request = _build_request(args)

        if args.count:
            print()
            _show_count(provider, request)

        if args.complete:
            print()
            _show_completion(provider, request)
            print()
            tracker.log_summary()
    except AiError as e:
        code = getattr(e, "code", None)
        print(f"AI エラー: code={code} {e}")
        print("縮退の対象: " + ("はい (既存の採点結果で続行する)" if should_degrade(e) else "いいえ"))

        return 1
    except AutoEditError as e:
        print(f"エラー: {type(e).__name__} {e}")

        return 1

    return 0


# 保存済みプロジェクトから編集プロファイルを作り直す (§13.2)。
#
# AI を呼ばずに統計だけで作れる (呼べない環境でも骨格はできる)。
# 言語化 (仮説) までやりたいときは --learn と一緒にログインしておく。
def _learn(settings, args):
    from src.timeline import project_io

    # scan_projects は {"paths": [...], "truncated": n} を返す。
    found = project_io.scan_projects(settings, kind=project_io.KIND_ARCHIVE)
    paths = list(found.get("paths") or []) if isinstance(found, dict) else list(found)
    print(f"保存済みプロジェクト: {len(paths)} 件")
    if not paths:
        print("測れるプロジェクトがありません (アーカイブ用を 1 つ以上保存してください)。")

        return 1

    provider = None
    if ai_config.is_enabled(settings):
        auth = StretheusAuth(api_config(settings)["base_url"],
                             api_config(settings)["timeout_sec"])
        if auth.is_logged_in():
            provider = StretheusProvider(auth, settings=settings,
                                         tracker=UsageTracker(settings))
    profile, added = ai_profile.refresh(
        settings, paths, provider=provider, name=args.profile_name,
        target=args.profile_target)
    if profile is None:
        print("プロファイルを作れませんでした (ai.learning.enabled を確認してください)。")

        return 1

    print(f"プロファイル: {profile['slug']} "
          f"(プロジェクト {profile['sample']['projects']} 件)")
    for key, value in sorted(profile["stats"].items()):
        print(f"  {key:22s}: {value}")
    for item in profile["hypotheses"]:
        print(f"  仮説 {item['id']}: {item['text']} "
              f"({item['confidence']} / 根拠 {item['evidence']} 件)")
    if provider is None:
        print("(AI を呼んでいないため仮説は作っていません。"
              "ai.enabled=true + ログインで言語化まで行います)")
    print(f"追加した仮説: {len(added)} 件")

    return 0


def _build_request(args):
    return AiRequest(
        role=args.role,
        system=_SYSTEMS[args.role],
        payload=_PAYLOADS[args.role],
        schema=_SCHEMAS[args.role],
        # 前置きを使い回す意思表示。同じ値で続けて呼ぶとキャッシュが効く。
        cache_prefix_id="smoke-" + args.role + "-v1",
        project_id=args.project_id)


def _show_config(provider):
    config = provider.server_config()
    if config is None:
        # 直前の警告ログに HTTP の状態が出る。よくある原因を並べておく。
        print("サーバーの構成を取得できませんでした。考えられる原因:")
        print("  ・HTTP 404  : 接続先に /api/ai/* がまだ無い (サーバーが未デプロイ)")
        print("  ・HTTP 401  : 再ログインが必要 (python -m src.services.login_check)")
        print("  ・接続不可  : 接続先 (api.base_url) と回線を確認する")

        return False

    print()
    print("サーバーの構成 (GET /api/ai/config):")
    print(f"  enabled      : {config.enabled}")
    print(f"  chargeMode   : {config.charge_mode} (課金: {config.charging})")
    print(f"  requireLogin : {config.require_login}")
    print(f"  allowedModels: {', '.join(config.allowed_models) or '(なし)'}")
    for role, values in sorted(config.roles.items()):
        print(f"  role {role:10s}: {values.get('model')} / {values.get('effort')} "
              f"/ max {values.get('maxOutputTokens')}")
    if config.pricing:
        print(f"  pricing      : {json.dumps(config.pricing, ensure_ascii=False)}")
    if config.limits:
        print(f"  limits       : {json.dumps(config.limits, ensure_ascii=False)}")

    if not config.enabled:
        print()
        print("サーバー側の AI が無効です (Ai:Enabled = false または鍵が未設定)。")

        return False

    return True


def _show_count(provider, request):
    estimate = provider.count_tokens(request)
    if estimate is None:
        print("見積りを取得できませんでした。")

        return

    print("見積り (POST /api/ai/count):")
    print(f"  model              : {estimate.model}")
    print(f"  inputTokens        : {estimate.input_tokens}")
    print(f"  estimatedOutput    : {estimate.estimated_output_tokens}")
    print(f"  estimatedPoints    : {estimate.estimated_points}")
    print(f"  estimatedUsd       : {estimate.estimated_usd}")


def _show_completion(provider, request):
    completion = provider.complete_json(request)
    usage = completion.usage

    print("結果 (POST /api/ai/complete):")
    print(f"  model        : {completion.model}")
    print(f"  stopReason   : {completion.stop_reason}")
    print(f"  durationMs   : {completion.duration_ms}")
    if completion.job_id:
        print(f"  jobId        : {completion.job_id} (202 で受理され、ポーリングで受け取った)")
    print(f"  usage        : input={usage.input_tokens} output={usage.output_tokens} "
          f"cacheRead={usage.cache_read_input_tokens} "
          f"cacheCreate={usage.cache_creation_input_tokens}")
    print(f"  points       : charged={completion.points_charged} "
          f"estimated={completion.points_estimated}")
    print("  result       : " + json.dumps(completion.result, ensure_ascii=False)[:600])


if __name__ == "__main__":
    raise SystemExit(main())
