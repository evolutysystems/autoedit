# Stretheus API へのログイン疎通を手動で確認するための小さな CLI
# (StretheusAPI docs/request/resolve2.md §9 Phase 4 の完了条件)
#
#   python -m src.services.login_check                        setting.json の api.base_url へ
#   python -m src.services.login_check --base-url http://localhost:5000
#   python -m src.services.login_check --logout
#
# 既定ブラウザで Twitch の認可画面が開く。許可すると JWT を保存し、GET /api/auth/me を呼ぶ。
# GUI を起動せずに確認できるよう、設定は setting.json を直接読む (PySide6 に依存しない)。
# トークンは表示しない。
import argparse
import json
import os
import sys

from ..exceptions import AutoEditError, ReauthRequiredError
from .config import api_config
from .stretheus_auth import StretheusAuth

_SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings", "setting.json")


def _load_settings():
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stretheus API のログイン疎通を確認する")
    parser.add_argument("--base-url", help="接続先。省略時は setting.json の api.base_url")
    parser.add_argument("--timeout", type=int, default=180, help="認可待ちの秒数 (既定 180)")
    parser.add_argument("--logout", action="store_true", help="保存済みの JWT を失効させて終了する")
    args = parser.parse_args(argv)

    config = api_config(_load_settings())
    base_url = args.base_url or config["base_url"]
    auth = StretheusAuth(base_url, config["timeout_sec"])
    print(f"接続先: {base_url}")

    try:
        if args.logout:
            auth.logout()
            print("ログアウトしました。")
            return 0

        if auth.is_logged_in():
            print("保存済みの JWT を使用します。")
        else:
            user = auth.login(timeout=args.timeout)
            print(f"ログインしました: {user.get('displayName')} (Twitch ID {user.get('twitchUserId')})")

        try:
            profile = auth.client.get("/api/auth/me")
        except ReauthRequiredError:
            # 接続先を変えた場合など、保存済みトークンをこのサーバーが検証できないとき。
            print("保存済みの JWT は使えませんでした。ログインし直します。")
            user = auth.login(timeout=args.timeout)
            print(f"ログインしました: {user.get('displayName')} (Twitch ID {user.get('twitchUserId')})")
            profile = auth.client.get("/api/auth/me")
        print("GET /api/auth/me →", json.dumps(profile, ensure_ascii=False))
        expires_at = auth.session_expires_at()
        print("セッション期限:", expires_at.isoformat() if expires_at else "不明")
        return 0
    except AutoEditError as e:
        print(f"失敗: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
