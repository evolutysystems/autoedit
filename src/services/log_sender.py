# 溜まったログの送信 (request1 / resolve1 §6 / §9)
#
# 約束は 3 つ。
#   ・**利用者の操作より優先度が低い** (R3 / R6)。停止を求められたら 1 件ごとに
#     見て打ち切り、sending に残った分は waiting へ戻す (§6.3)。
#   ・**例外を外へ出さない**。points.py と同じで、ログ送信でアプリを壊さない。
#   ・**依存順序を守る** (§3.1)。Sessions → Projects → 残り。プロジェクトが
#     未登録のまま ExportLogs を送ると 404 になる。
#
# 1 回の flush で投げるリクエストは 92 本までに抑える。受信系のレート制限は
# ユーザーあたり 120 本 / 分 (§3.4) で、溜まった分を一気に流すと 429 に当たる。
import threading
import time

from ..exceptions import ApiError, ApiOfflineError, AutoEditError, ReauthRequiredError
from ..utils.logger import get_logger
from . import log_payloads, log_queue

_logger = get_logger(__name__)

# 1 回の flush で扱うテーブルと件数の上限 (resolve1 §6.6)。**この順番で送る**。
_ORDER = (
    (log_queue.TABLE_SESSIONS, 10),
    (log_queue.TABLE_PROJECTS, 20),
    (log_queue.TABLE_EVENTS, log_payloads.MAX_EVENTS_PER_REQUEST * 2),
    (log_queue.TABLE_EXPORT_LOGS, 20),
    (log_queue.TABLE_PROCESSING_LOGS, 20),
    (log_queue.TABLE_ERRORS, 20),
)

# Errors だけ別のスイッチで止める (プライバシーポリシーの記載単位 / §10)
_ANALYTICS_TABLES = frozenset({
    log_queue.TABLE_SESSIONS,
    log_queue.TABLE_PROJECTS,
    log_queue.TABLE_EVENTS,
    log_queue.TABLE_EXPORT_LOGS,
    log_queue.TABLE_PROCESSING_LOGS,
})

_DEFAULT_MAX_REQUESTS = 92
_DEFAULT_INTERVAL_MS = 200

# 1 件の送信結果
_SENT = "sent"           # 成功。sent へ
_RELEASE = "release"      # 一時的な失敗。waiting へ戻して次へ進む
_BREAK = "break"          # 一時的な失敗。waiting へ戻してフェーズを打ち切る
_FAIL = "fail"            # 再送しても直らない。error へ


# 1 回の flush の結果 (呼び出し側の診断ログ用)。
class FlushResult:

    def __init__(self):
        self.sent = 0
        self.failed = 0
        self.requests = 0
        self.stopped = False
        self.offline = False
        self.skipped = False

    def __str__(self):
        return (f"sent={self.sent} failed={self.failed} requests={self.requests} "
                f"stopped={self.stopped} offline={self.offline}")


# 受信結果が「1 件も受け付けていない」か。応答が無い (本文なし) 場合は
# 判断できないため False とし、送信済みとして扱う。
def _all_rejected(response):
    if not isinstance(response, dict):
        return False
    if "accepted" not in response:
        return False
    return int(response.get("accepted") or 0) == 0 and int(response.get("rejected") or 0) > 0


class LogSender:

    def __init__(self, auth, settings=None, queue=None):
        self._auth = auth
        config = (settings or {}).get("telemetry", {}) if isinstance(settings, dict) else {}
        self._enabled = bool(config.get("enabled", True))
        self._send_errors = bool(config.get("send_errors", False))
        self._send_analytics = bool(config.get("send_analytics", True))
        self._max_requests = int(config.get("max_requests_per_flush",
                                            _DEFAULT_MAX_REQUESTS) or _DEFAULT_MAX_REQUESTS)
        self._interval_sec = max(0.0, float(config.get("request_interval_ms",
                                                       _DEFAULT_INTERVAL_MS) or 0) / 1000.0)
        self._sent_days = config.get("sent_retention_days",
                                     log_queue.DEFAULT_SENT_RETENTION_DAYS)
        self._error_days = config.get("error_retention_days",
                                      log_queue.DEFAULT_ERROR_RETENTION_DAYS)

        self._queue = queue or log_queue.LogQueue(limits={
            log_queue.TABLE_ERRORS: config.get("queue_limit_errors"),
            log_queue.TABLE_EVENTS: config.get("queue_limit_events"),
            "default": config.get("queue_limit_default"),
        })

        # 停止要求 (利用者が何か始めた)。GUI スレッドから立て、送信側が見る。
        self._stop = threading.Event()
        # flush の多重起動を防ぐ
        self._busy = threading.Lock()
        self._recovered = False

    @property
    def queue(self):
        return self._queue

    @property
    def enabled(self):
        return self._enabled

    # ---- 中断 (resolve1 §6.3) ---------------------------------------------
    # 利用者の操作が始まった。いま飛んでいる 1 リクエストの完了は待つ。
    # リクエストを途中で切ると、サーバーへ届いたかどうかが分からなくなり、
    # 冪等キーを持たないテーブルで重複が増える。
    def request_stop(self):
        self._stop.set()

    # 操作が終わった。次のトリガから送ってよい。
    def allow(self):
        self._stop.clear()

    def is_stopped(self):
        return self._stop.is_set()

    # ---- 起動時の後始末 (resolve1 §5.7 / §5.8) ----------------------------
    def startup(self):
        if self._recovered or not self._enabled:
            return
        self._recovered = True
        try:
            self._queue.recover()
            self._queue.purge(sent_days=self._sent_days, error_days=self._error_days)
        except OSError as e:
            _logger.warning("ログキューの後始末に失敗しました: %s", e)

    # ---- 送信 -------------------------------------------------------------
    # 溜まっているログを送る。例外は投げない。
    def flush(self):
        result = FlushResult()
        if not self._enabled:
            result.skipped = True
            return result

        if not self._busy.acquire(blocking=False):
            # 既に別のトリガで走っている。重ねて走らせない。
            result.skipped = True
            return result

        try:
            return self._flush(result)
        except Exception:  # noqa: BLE001 (ログ送信の失敗でアプリを壊さない)
            _logger.exception("ログ送信で想定外のエラー")
            return result
        finally:
            self._busy.release()

    def _flush(self, result):
        self.startup()

        if self._stop.is_set():
            result.stopped = True
            return result

        # 未ログインでも端末 ID で匿名セッションを作って送る (ver7 resolve §1)。
        # 作れなければオフライン。キューには手を付けない。
        if not self._auth.ensure_session():
            result.offline = True
            return result

        for table, limit in _ORDER:
            if not self._allowed(table):
                continue
            if self._stop.is_set():
                result.stopped = True
                break
            if result.requests >= self._max_requests:
                break

            if table == log_queue.TABLE_EVENTS:
                self._flush_events(result)
            else:
                self._flush_one_by_one(table, limit, result)

            if result.offline or result.stopped:
                break

        if result.sent or result.failed:
            _logger.info("ログを送信しました: %s", result)
        return result

    def _allowed(self, table):
        if table == log_queue.TABLE_ERRORS:
            return self._send_errors
        return self._send_analytics if table in _ANALYTICS_TABLES else True

    # ---- 1 件 1 リクエストのテーブル --------------------------------------
    def _flush_one_by_one(self, table, limit, result):
        budget = min(limit, max(0, self._max_requests - result.requests))
        entries = self._queue.pick(table, budget, skip=self._skip_for(table))
        if not entries:
            return

        for index, entry in enumerate(entries):
            if self._stop.is_set():
                self._queue.return_to_waiting(entries[index:], reason="利用者の操作を優先")
                result.stopped = True
                return

            outcome, reason = self._post(entry)
            result.requests += 1

            if outcome == _SENT:
                self._queue.mark_sent(entry)
                result.sent += 1
            elif outcome == _FAIL:
                self._queue.mark_error(entry, reason)
                result.failed += 1
            elif outcome == _BREAK:
                self._queue.release(entry, reason)
                self._queue.return_to_waiting(entries[index + 1:], reason=reason)
                result.offline = True
                return
            else:
                self._queue.release(entry, reason)
                result.failed += 1

            self._pace()

    # ---- AnalyticsEvents (まとめ送り / resolve1 §7.4) ----------------------
    def _flush_events(self, result):
        while result.requests < self._max_requests:
            if self._stop.is_set():
                result.stopped = True
                return

            entries = self._queue.pick(log_queue.TABLE_EVENTS,
                                       log_payloads.MAX_EVENTS_PER_REQUEST,
                                       skip=self._skip_for(log_queue.TABLE_EVENTS))
            if not entries:
                return

            body = [entry.body for entry in entries]
            outcome, reason, response = self._post_events(body)
            result.requests += 1

            if outcome == _SENT and _all_rejected(response):
                # 1 件も受け付けられなかった = 本文の作り方が間違っている。
                # 送り直しても通らないため error へ落とす。
                for entry in entries:
                    self._queue.mark_error(entry, reason="すべて検証で破棄されました")
                result.failed += len(entries)
                return
            if outcome == _SENT:
                # どの 1 件が reject されたかは応答から分からない。1 件でも
                # 受け付けられていればバッチ全体を sent とし、件数だけ残す。
                rejected = int((response or {}).get("rejected", 0) or 0)
                if rejected:
                    _logger.warning("Analytics イベント %d 件が検証で破棄されました。", rejected)
                for entry in entries:
                    self._queue.mark_sent(entry)
                result.sent += len(entries)
            elif outcome == _FAIL:
                for entry in entries:
                    self._queue.mark_error(entry, reason)
                result.failed += len(entries)
            elif outcome == _BREAK:
                self._queue.return_to_waiting(entries, reason=reason)
                result.offline = True
                return
            else:
                for entry in entries:
                    self._queue.release(entry, reason)
                result.failed += len(entries)
                return

            if len(entries) < log_payloads.MAX_EVENTS_PER_REQUEST:
                return
            self._pace()

    # ---- 依存の判定 (resolve1 §3.1 / §6.6) --------------------------------
    # 依存先 (Projects / Sessions) が waiting か sending に残っているあいだは
    # 送らない。送れば 404 になるか、関連付けが黙って外れる。
    def _skip_for(self, table):
        if table in (log_queue.TABLE_SESSIONS, log_queue.TABLE_PROJECTS):
            return None

        pending_projects = self._queue.pending_values(log_queue.TABLE_PROJECTS, "projectId")
        pending_sessions = self._queue.pending_values(log_queue.TABLE_SESSIONS, "sessionId")
        if not pending_projects and not pending_sessions:
            return None

        def _skip(data):
            depends = data.get("dependsOn") or {}
            project = depends.get("project")
            if project and str(project) in pending_projects:
                return True
            session = depends.get("session")
            return bool(session and str(session) in pending_sessions)

        return _skip

    # ---- HTTP -------------------------------------------------------------
    def _post(self, entry):
        try:
            self._auth.client.post(entry.path, entry.body)
            return _SENT, None
        except ApiOfflineError as e:
            # 接続できない / 5xx。縮退させて次のトリガへ回す。
            return _BREAK, str(e)
        except ReauthRequiredError as e:
            # api_client が 1 回リフレッシュして駄目だったもの。残りも同じになる。
            return _BREAK, str(e)
        except ApiError as e:
            return self._classify(e, entry)
        except AutoEditError as e:
            return _FAIL, str(e)

    def _post_events(self, body):
        try:
            response = self._auth.client.post(log_payloads.PATH_EVENTS, body)
            return _SENT, None, response
        except ApiOfflineError as e:
            return _BREAK, str(e), None
        except ReauthRequiredError as e:
            return _BREAK, str(e), None
        except ApiError as e:
            if e.status in (429, 409):
                return _BREAK, str(e), None
            if e.status == 400:
                return _FAIL, str(e), None
            return _RELEASE, str(e), None
        except AutoEditError as e:
            return _FAIL, str(e), None

    @staticmethod
    def _classify(error, entry):
        status = error.status
        if status in (429, 409):
            # 混雑・競合。api_client が 1 回待って再送した後でも駄目だった。
            return _BREAK, str(error)
        if status == 400:
            # 検証に落ちた。送り直しても通らない。
            return _FAIL, str(error)
        if status == 404:
            # 対象プロジェクトが未登録。Projects が待ち行列へ入れば次の flush で
            # 先に送られる (依存順序 / §6.6) ため、捨てずに waiting へ戻す。
            # 直らないまま attempts の上限に達したら error へ落ちる。
            if (entry.depends_on or {}).get("project"):
                return _RELEASE, str(error)
            # セッションの end で 404 = 開始が残っていない。送り直しても通らない。
            return _FAIL, str(error)
        if status and status >= 500:
            return _BREAK, str(error)
        return _RELEASE, str(error)

    # 回線と CPU を空ける。停止要求があれば待たずに戻る。
    def _pace(self):
        if self._interval_sec <= 0:
            return
        self._stop.wait(self._interval_sec)
