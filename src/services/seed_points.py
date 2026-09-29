# dev 環境へポイント制の実データを投入し、取得できることを確認するための CLI
# (StretheusPlan p1_plan.md Phase 7 のスモークテストを、ポイント経路だけ厚くしたもの)
#
#   python -m src.services.seed_points              full シナリオ (残高を 0 まで使い切る)
#   python -m src.services.seed_points --scenario basic   25pt だけ消費する
#   python -m src.services.seed_points --show       投入せず、残高と履歴を表示する
#   python -m src.services.seed_points --base-url http://localhost:5000
#
# 事前に `python -m src.services.login_check` でログインしておくこと (JWT を使う)。
# 実際の出力は行わず、API だけを叩く。GUI を起動せずに使えるよう、設定は
# setting.json を直接読む (login_check と同じ流儀で PySide6 に依存しない)。
# トークンは表示しない。
import argparse
import json
import os
import uuid

from ..exceptions import ApiError, AutoEditError
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


# 残高の 1 行表示。予約中と利用可能額まで出す (予約が漏れていれば available で気付ける)。
def _format_wallet(wallet):
    return (f"balance={wallet.get('balance')} reserved={wallet.get('reserved')} "
            f"available={wallet.get('available')}")


# GET /api/points を呼び、残高の現況を表示する。
def _show_balance(client, label):
    balance = client.get("/api/points")
    costs = balance.get("costs", {})
    print(f"[{label}] {_format_wallet(balance)} grant={balance.get('monthlyGrant')} "
          f"unlimited={balance.get('unlimited')} ({balance.get('subscriptionType')})")
    print(f"           単価: clip={costs.get('clip')} archive={costs.get('archive')} / "
          f"次回リセット {balance.get('nextResetAt')}")
    return balance


# 予約を 1 件作る。client_job_id を渡せば再送 (冪等性の確認) になる。
def _reserve(client, job_type, output_type, client_job_id=None):
    job_id = client_job_id or str(uuid.uuid4())
    body = {
        "jobType": job_type,
        "outputType": output_type,
        "clientJobId": job_id,
    }
    reservation = client.post("/api/points/reservations", body=body)
    mark = " [watermark]" if reservation.get("watermarkRequired") else ""
    print(f"  予約   {job_type}/{output_type} amount={reservation.get('amount')}{mark} "
          f"→ {_format_wallet(reservation.get('wallet', {}))}")
    return reservation


# 予約を確定する (出力が完成したときに相当)。
def _commit(client, reservation):
    reservation_id = reservation["reservationId"]
    result = client.post(f"/api/points/reservations/{reservation_id}/commit")
    print(f"  commit consumed={result.get('consumedAmount')} status={result.get('status')} "
          f"→ {_format_wallet(result.get('wallet', {}))}")
    return result


# 予約を解放する (出力が失敗・中断したときに相当)。
def _cancel(client, reservation):
    reservation_id = reservation["reservationId"]
    result = client.post(f"/api/points/reservations/{reservation_id}/cancel")
    print(f"  cancel status={result.get('status')} "
          f"→ {_format_wallet(result.get('wallet', {}))}")
    return result


# GET /api/points/transactions を呼び、台帳を新しい順に表示する。
# 台帳に載るのは確定残高を動かした出来事だけ (Grant / Commit / Adjust)。
# 予約と解放は残高を動かさないため行が増えない。
def _show_transactions(client, limit):
    page = client.get(f"/api/points/transactions?limit={limit}")
    items = page.get("items", [])
    print(f"[台帳] {len(items)} 件 (新しい順)")
    for item in items:
        flags = []
        if item.get("watermarked"):
            flags.append("watermarked")
        if item.get("unlimited"):
            flags.append("unlimited")
        suffix = f" [{' '.join(flags)}]" if flags else ""
        print(f"  {item.get('createdAt')} {item.get('kind'):7} "
              f"amount={item.get('amount'):>5} after={item.get('balanceAfter'):>4} "
              f"job={item.get('jobType') or '-'}/{item.get('outputType') or '-'}{suffix}")
    if page.get("nextCursor"):
        print(f"  (続きあり cursor={page['nextCursor']})")
    return items


# 25pt だけ消費する最小シナリオ。冪等性の確認まで行う。
def _scenario_basic(client):
    print("\n--- 1. クリップ出力が成功した (25pt 消費) ---")
    reservation = _reserve(client, "clip", "video")
    _commit(client, reservation)

    print("\n--- 2. クリップ出力が失敗した (消費なし) ---")
    cancelled = _reserve(client, "clip", "video")
    _cancel(client, cancelled)

    print("\n--- 3. 同じ clientJobId の再送 (二重消費しないこと) ---")
    job_id = str(uuid.uuid4())
    first = _reserve(client, "clip", "video", client_job_id=job_id)
    again = _reserve(client, "clip", "video", client_job_id=job_id)
    same = first["reservationId"] == again["reservationId"]
    print(f"  予約 ID が同一: {same}")
    _cancel(client, first)


# 残高を使い切り、watermark 付きの出力まで発生させるシナリオ。
# App Insights の customEvents (PointWatermarkRequired) を出すのが目的。
def _scenario_full(client):
    _scenario_basic(client)

    print("\n--- 4. アーカイブ出力が成功した (100pt 消費) ---")
    archive = _reserve(client, "archive", "video")
    _commit(client, archive)

    print("\n--- 5. 残高を使い切るまでクリップを出力する ---")
    # 残高不足になるまで繰り返す。1 回あたり clip の単価 (既定 25pt) を消費する。
    for attempt in range(1, 11):
        reservation = _reserve(client, "clip", "video")
        _commit(client, reservation)
        if reservation.get("watermarkRequired"):
            print(f"  → {attempt} 回目で残高不足になった (ここから watermark が入る)")
            break
        if reservation.get("unlimited"):
            print("  → サブスク判定 (unlimited) のため消費されない。残高は減らない")
            break
    else:
        print("  → 10 回試しても残高が尽きなかった (単価か付与量の設定を確認すること)")

    print("\n--- 6. 残高不足での Resolve 書き出し (0pt・watermark 付き) ---")
    resolve = _reserve(client, "clip", "resolveProject")
    _commit(client, resolve)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="dev 環境へポイント制の実データを投入し、取得できることを確認する")
    parser.add_argument("--base-url", help="接続先。省略時は setting.json の api.base_url")
    parser.add_argument("--scenario", choices=["basic", "full"], default="full",
                        help="basic は 25pt のみ消費。full は残高を使い切り watermark まで出す (既定)")
    parser.add_argument("--show", action="store_true",
                        help="投入せず、残高と履歴を表示するだけ")
    parser.add_argument("--limit", type=int, default=50, help="履歴の表示件数 (既定 50)")
    args = parser.parse_args(argv)

    config = api_config(_load_settings())
    base_url = args.base_url or config["base_url"]
    auth = StretheusAuth(base_url, config["timeout_sec"])
    print(f"接続先: {base_url}")

    if not auth.is_logged_in():
        print("未ログインです。先に `python -m src.services.login_check` を実行してください。")
        return 1

    user = auth.user() or {}
    print(f"ユーザー: {user.get('displayName')} (内部 ID {user.get('id')})")
    client = auth.client

    try:
        balance = _show_balance(client, "開始時")

        # サブスク会員は消費せず watermark も付かないため、消費系のデータは作れない。
        # 配信者本人のアカウントは Twitch が自分のチャンネルのサブスクとして返すため、
        # 常にここへ入る (StretheusAPI の TwitchLicenseProvider)。
        if balance.get("unlimited"):
            print("  ※ このアカウントは unlimited 判定です。予約は 0pt で記録され、"
                  "残高の減少と watermark のデータは作れません。")

        if not args.show:
            if args.scenario == "basic":
                _scenario_basic(client)
            else:
                _scenario_full(client)
            print()
            _show_balance(client, "終了時")

        print()
        _show_transactions(client, args.limit)
    except ApiError as e:
        print(f"API エラー: HTTP {e.status} code={e.code} {e}")
        return 1
    except AutoEditError as e:
        print(f"失敗しました: {e}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
