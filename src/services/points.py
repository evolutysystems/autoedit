# ポイント制のクライアント側ファサード (ver5 resolve §5.2)
#
# 出力操作 1 回につき「予約 → 成果物の完成で commit / それ以外で cancel」を行う。
# watermark を入れるかどうかは**サーバーの応答だけ**で決め、残高からは推測しない (R9)。
#
# ここでの最重要の約束は「**ポイント処理で出力を壊さない**」ことである。
# API 呼び出しの失敗はすべてこのモジュールの中で握り、呼び出し側へは
# 「watermark が要るか」だけを返す (ver5 resolve §4-3)。
#
# オフライン時は watermark を入れる。ただしサブスク会員だけは直近の判定をキャッシュして救済する
# (ver5 resolve §3.3)。残高と違いサブスク状態は消費して減るものではないため、
# キャッシュしても不整合が起きない。
import datetime
import json
import os
import threading
import uuid

from ..exceptions import ApiError, ApiOfflineError, AutoEditError
from ..utils.logger import get_logger
from .auth_store import default_auth_path
from .stretheus_auth import parse_timestamp

_logger = get_logger(__name__)

_POINTS_PATH = "/api/points"
_RESERVATIONS_PATH = "/api/points/reservations"

# ジョブ種別 (単価を決める)
JOB_CLIP = "clip"
JOB_ARCHIVE = "archive"

# 出力の種類 (単価には影響しない。履歴と分析のために記録する)
OUTPUT_VIDEO = "video"
OUTPUT_RESOLVE_PROJECT = "resolveProject"

# 残高の再取得を抑える既定間隔 (秒)
_DEFAULT_BALANCE_REFRESH_SEC = 300

# オフライン時にサブスク判定を信用する既定の時間
_DEFAULT_SUBSCRIPTION_CACHE_HOURS = 72


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# ---- 呼び出し側の分岐を減らす薄い関数 ------------------------------------
# service が None (ポイント処理を通さない呼び出し = CLI / テスト) でも安全に呼べる。
# 出力経路はこの 4 つだけを使い、None 判定を各所へ散らさない。

# 出力の開始時に予約する。service が無ければ None を返す。
def reserve(service, job_type, output_type, project_id=None):
    if service is None:
        return None
    return service.reserve(job_type, output_type, project_id=project_id)


# 成果物の完成時に確定する。
def commit(service, reservation):
    if service is not None and reservation is not None:
        service.commit(reservation)


# 失敗・中断時に解放する。
def cancel(service, reservation):
    if service is not None and reservation is not None:
        service.cancel(reservation)


# 予約の透かし要否。予約が無ければ入れない (未ログインは従来どおり / §4-5)。
def watermark_required(reservation):
    return bool(reservation is not None and reservation.watermark_required)


# 透かしが入る出力になることを利用者へ確認する (R12)。
# 確認は**予約の応答を見てから**行う。残高から先回りして出すと、サブスクや
# 遅延確定で実際には入らない場合にも出てしまう (§5.6)。
# フックが無い呼び出し (CLI / テスト) はそのまま続ける。
def confirmed(reservation, confirm):
    if not watermark_required(reservation) or confirm is None:
        return True
    return bool(confirm())


# 出力操作 1 回分の予約。
# 呼び出し側が見るのは watermark_required だけでよい。
class Reservation:

    def __init__(self, job_type, output_type, client_job_id=None, reservation_id=None,
                 watermark_required=False, unlimited=False, amount=0,
                 expires_at=None, offline=False):
        self.job_type = job_type
        self.output_type = output_type
        self.client_job_id = client_job_id or str(uuid.uuid4())
        self.reservation_id = reservation_id
        self.watermark_required = bool(watermark_required)
        self.unlimited = bool(unlimited)
        self.amount = int(amount or 0)
        self.expires_at = expires_at
        # サーバーへ予約できなかった (未ログイン・オフライン・エラー)
        self.offline = bool(offline)

    # commit / cancel を送る対象か
    def is_tracked(self):
        return bool(self.reservation_id)

    def to_queue_entry(self, action):
        return {
            "action": action,
            "reservationId": self.reservation_id,
            "clientJobId": self.client_job_id,
            "jobType": self.job_type,
            "outputType": self.output_type,
            "queuedAt": _now().isoformat(),
        }


# 残高照会と予約 / 確定 / 解放。アプリ全体で 1 つ持ち回る。
class PointsService:

    def __init__(self, auth, settings=None, store_dir=None):
        self._auth = auth
        self._lock = threading.Lock()

        config = (settings or {}).get("points", {}) if isinstance(settings, dict) else {}
        self._cache_hours = float(config.get("subscription_cache_hours",
                                             _DEFAULT_SUBSCRIPTION_CACHE_HOURS) or 0)
        self._refresh_sec = float(config.get("balance_refresh_sec",
                                             _DEFAULT_BALANCE_REFRESH_SEC) or 0)

        base_dir = store_dir or os.path.dirname(default_auth_path())
        self._state_path = os.path.join(base_dir, "points.json")
        self._queue_path = os.path.join(base_dir, "pending_commits.json")

        self._balance = None
        self._balance_at = None

    # 認証。画面のログイン状態表示とログイン / ログアウトに使う。
    @property
    def auth(self):
        return self._auth

    # ---- 残高 -------------------------------------------------------------
    # 残高を返す。オフライン・未ログインでは None。
    # force=False なら balance_refresh_sec の間は前回の値を返す。
    def balance(self, force=False):
        if not self._auth.is_logged_in():
            return None

        if not force and self._balance is not None and self._balance_at is not None:
            if (_now() - self._balance_at).total_seconds() < self._refresh_sec:
                return self._balance

        try:
            balance = self._auth.client.get(_POINTS_PATH)
        except AutoEditError as e:
            _logger.warning("ポイント残高を取得できませんでした: %s", e)
            return None

        self._balance = balance
        self._balance_at = _now()
        self._remember_subscription(bool(balance.get("unlimited")))

        return balance

    # 最後に取得できた残高 (オフライン表示用)。一度も取れていなければ None。
    def last_balance(self):
        return self._balance

    # ---- 予約 / 確定 / 解放 -----------------------------------------------
    # 出力の開始時に呼ぶ。例外は投げない。
    def reserve(self, job_type, output_type, project_id=None):
        reservation = Reservation(job_type, output_type)

        # 未ログインのユーザーは従来どおり無償で使える (ver5 resolve §4-5)。
        if not self._auth.is_logged_in():
            reservation.offline = True
            return reservation

        # 前回までに送れなかった commit / cancel をここで片付ける。
        self.flush_pending()

        body = {
            "jobType": job_type,
            "outputType": output_type,
            "clientJobId": reservation.client_job_id,
        }
        if project_id:
            body["projectId"] = str(project_id)

        try:
            response = self._auth.client.post(_RESERVATIONS_PATH, body)
        except ApiOfflineError as e:
            # サーバーへ到達できない。サブスク会員だけ救済する。
            subscribed = self._subscription_from_cache()
            reservation.offline = True
            reservation.unlimited = subscribed
            reservation.watermark_required = not subscribed
            _logger.warning("ポイントを予約できませんでした (オフライン): %s", e)
            return reservation
        except AutoEditError as e:
            # 想定外の応答。出力は止めず、安全側 (watermark あり) に倒す。
            reservation.offline = True
            reservation.watermark_required = True
            _logger.warning("ポイントを予約できませんでした: %s", e)
            return reservation

        response = response or {}
        reservation.reservation_id = response.get("reservationId")
        reservation.watermark_required = bool(response.get("watermarkRequired"))
        reservation.unlimited = bool(response.get("unlimited"))
        reservation.amount = int(response.get("amount") or 0)
        reservation.expires_at = parse_timestamp(response.get("expiresAt"))

        self._remember_subscription(reservation.unlimited)
        self._update_balance_from_wallet(response.get("wallet"))

        _logger.info(
            "ポイントを予約しました: %s/%s %dpt (watermark=%s)",
            job_type, output_type, reservation.amount, reservation.watermark_required)

        return reservation

    # 成果物の完成時に呼ぶ。例外は投げない。
    def commit(self, reservation):
        self._complete(reservation, "commit")

    # 失敗・中断時に呼ぶ。例外は投げない。
    def cancel(self, reservation):
        self._complete(reservation, "cancel")

    # 送れずに残っている commit / cancel を再送する。起動時と予約の直前に呼ぶ。
    def flush_pending(self):
        if not self._auth.is_logged_in():
            return

        pending = self._read_queue()
        if not pending:
            return

        remaining = []
        for entry in pending:
            if not self._send(entry.get("reservationId"), entry.get("action")):
                remaining.append(entry)

        if len(remaining) != len(pending):
            _logger.info("保留していたポイント処理を %d 件送信しました。",
                         len(pending) - len(remaining))
        self._write_queue(remaining)

    # ---- 内部 ------------------------------------------------------------
    def _complete(self, reservation, action):
        if reservation is None or not reservation.is_tracked():
            return

        if not self._send(reservation.reservation_id, action):
            # 送れなかった。次回の起動か次の出力で再送する (commit / cancel は冪等)。
            self._append_queue(reservation.to_queue_entry(action))

    # 送信できたら True。恒久的な失敗 (404 / 409 など) も True とし、キューへ残さない。
    def _send(self, reservation_id, action):
        if not reservation_id or action not in ("commit", "cancel"):
            return True

        try:
            response = self._auth.client.post(f"{_RESERVATIONS_PATH}/{reservation_id}/{action}")
        except ApiOfflineError as e:
            _logger.warning("ポイントの%sを送信できませんでした (再送します): %s", action, e)
            return False
        except ApiError as e:
            # 404 (存在しない) / 409 (既に確定・解放済み) は再送しても結果が変わらない。
            _logger.warning("ポイントの%sが拒否されました: %s", action, e)
            return True
        except AutoEditError as e:
            _logger.warning("ポイントの%sに失敗しました: %s", action, e)
            return True

        self._update_balance_from_wallet((response or {}).get("wallet"))
        return True

    def _update_balance_from_wallet(self, wallet):
        if not wallet or self._balance is None:
            return

        # 予約 / 確定の応答に含まれる残高で手元の値を更新する (再取得を省く)。
        for key in ("balance", "reserved", "available"):
            if key in wallet:
                self._balance[key] = wallet[key]
        self._balance_at = _now()

    # ---- サブスク判定のキャッシュ ----------------------------------------
    def _remember_subscription(self, subscribed):
        self._write_state({
            "subscribed": bool(subscribed),
            "checkedAt": _now().isoformat(),
        })

    def _subscription_from_cache(self):
        if self._cache_hours <= 0:
            return False

        state = self._read_state()
        if not state.get("subscribed"):
            return False

        checked_at = parse_timestamp(state.get("checkedAt"))
        if checked_at is None:
            return False

        age_hours = (_now() - checked_at).total_seconds() / 3600.0
        if age_hours > self._cache_hours:
            _logger.info("サブスク判定のキャッシュが古いため watermark を入れます。")
            return False

        return True

    # ---- 永続化 ----------------------------------------------------------
    def _read_state(self):
        return self._read_json(self._state_path, {})

    def _write_state(self, state):
        self._write_json(self._state_path, state)

    def _read_queue(self):
        queue = self._read_json(self._queue_path, [])
        return queue if isinstance(queue, list) else []

    def _write_queue(self, queue):
        self._write_json(self._queue_path, queue)

    def _append_queue(self, entry):
        with self._lock:
            queue = self._read_json(self._queue_path, [])
            if not isinstance(queue, list):
                queue = []
            queue.append(entry)
            self._write_json(self._queue_path, queue, locked=True)

    def _read_json(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _write_json(self, path, value, locked=False):
        def _write():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(value, f, ensure_ascii=False)
            except OSError as e:
                # 保存できなくても出力は続ける。
                _logger.warning("ポイント情報の保存に失敗: %s", e)

        if locked:
            _write()
            return

        with self._lock:
            _write()
