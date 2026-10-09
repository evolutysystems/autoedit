# サブスクリプション (Stripe) のクライアント側ファサード (ver6 resolve §4)
#
# クライアントは**カード情報に一切触れない**。決済は Stripe がホストする画面
# (Checkout / カスタマーポータル) で行い、ここが受け持つのは
#   ・導線を出してよいかの判定 (GET /api/billing/config)
#   ・その画面を開くための URL の取得
#   ・支払いがサーバーへ反映されるまでの待ち合わせ
# の 3 つだけである。
#
# 支払いの完了はクライアントへ直接は届かない。Stripe → API の Webhook で
# 確定するため、反映は GET /api/points の unlimited をポーリングして知る
# (ver6 resolve §2 / §4.4)。
#
# ブラウザを開くのは呼び出し側 (GUI) の責務とし、このモジュールは PySide6 に
# 依存しない。login_check.py / seed_points.py と同じく GUI なしでも動かせる。
import datetime
import time

from ..exceptions import ApiError, AutoEditError
from ..i18n import tr
from ..utils.logger import get_logger

_logger = get_logger(__name__)

_CONFIG_PATH = "/api/billing/config"
_CHECKOUT_PATH = "/api/billing/checkout-session"
_PORTAL_PATH = "/api/billing/portal-session"
_SUBSCRIPTION_PATH = "/api/billing/subscription"
_POINTS_PATH = "/api/points"

# API が返すエラーコード (ver6 resolve §6)
CODE_ALREADY_SUBSCRIBED = "already_subscribed"
CODE_NO_SUBSCRIPTION = "no_subscription"

# サブスクリプションの種別 (API の subscriptionType)
TYPE_NONE = "None"
TYPE_STRIPE = "Stripe"
TYPE_TWITCH = "TwitchSub"

# 購入ボタンを押せない理由のコード (ver6 resolve2 §4.6 / §6)。
# 押せるなら空文字。文言は画面側が持ち、ここはコードだけを返す。
BLOCK_NOT_LOGGED_IN = "not_logged_in"   # ログインしていない (JWT が無いと購入できない)
BLOCK_UNKNOWN = "unknown"               # 加入状態が分からない (取得前・オフライン)
BLOCK_UNAVAILABLE = "unavailable"       # 受付停止 (API 未実装・Stripe 未設定)
BLOCK_TWITCH = "twitch"                 # Twitch サブで特典適用中 (request2 ①)
BLOCK_STRIPE = "stripe"                 # すでに Stripe で加入中

# 設定の既定値
_DEFAULT_CONFIG_CACHE_SEC = 3600
_DEFAULT_POLL_SEC = 3
_DEFAULT_TIMEOUT_SEC = 180

# ポーリング間隔の下限 (設定が極端な値でも API を叩き続けない)
_MIN_POLL_SEC = 1.0


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# GET /api/billing/config の応答。
# 取得できなかった場合は enabled=False のインスタンスを作って同じ形で扱う。
class BillingConfig:

    def __init__(self, enabled=False, provider="", price_label="", manageable=False):
        self.enabled = bool(enabled)
        self.provider = str(provider or "")
        self.price_label = str(price_label or "")
        self.manageable = bool(manageable)

    @classmethod
    def from_response(cls, response):
        response = response or {}
        return cls(
            enabled=response.get("enabled"),
            provider=response.get("provider"),
            price_label=response.get("priceLabel"),
            manageable=response.get("manageable"),
        )

    # 導線を出してよいか。価格の表示文が無いものは不完全とみなして出さない。
    def is_available(self):
        return self.enabled and bool(self.price_label)


# 未取得・取得失敗を表す無効な設定 (呼び出し側の None 判定を無くす)
_DISABLED = BillingConfig()


# 購入ボタンを押せるかを決める (ver6 resolve2 §4.6 / §6 の真理値表)。
# 押せるなら空文字、押せないなら BLOCK_* のいずれかを返す。
#
# 要望 ① (Twitch サブスク中は購入させない) の分岐を画面の中へ埋めないため、
# PySide6 に依存しない純関数として切り出す。真理値表をそのまま単体テストできる。
#
# config            … BillingConfig。まだ取れていなければ None
# subscription_type … "None" / "Stripe" / "TwitchSub"。まだ取れていなければ None
# logged_in         … Stretheus (Twitch) へログイン済みか
def purchase_blocked_reason(config, subscription_type, logged_in):
    # 購入には JWT が要る。ログインしていなければまずログインさせる。
    if not logged_in:
        return BLOCK_NOT_LOGGED_IN

    # 契約情報と加入種別のどちらかが欠けている間は押させない。
    # 片方だけで判断すると、加入中の利用者へ一瞬だけ購入ボタンを開いてしまう
    # (ver6 resolve §4.2 と同じ理由)。オフラインでもここへ来る。
    if config is None or subscription_type is None:
        return BLOCK_UNKNOWN

    # Stripe が未設定 / API が未実装なら、そもそも受け付けられない。
    if not config.is_available():
        return BLOCK_UNAVAILABLE

    # Twitch サブスクリプションで特典が適用されている間は購入不要 (request2 ①)。
    if subscription_type == TYPE_TWITCH:
        return BLOCK_TWITCH

    if subscription_type == TYPE_STRIPE:
        return BLOCK_STRIPE

    return ""


class BillingService:

    def __init__(self, points, settings=None):
        self._points = points

        config = (settings or {}).get("billing", {}) if isinstance(settings, dict) else {}
        self._cache_sec = float(config.get("config_cache_sec",
                                           _DEFAULT_CONFIG_CACHE_SEC) or 0)
        self._poll_sec = max(float(config.get("activation_poll_sec",
                                              _DEFAULT_POLL_SEC) or 0), _MIN_POLL_SEC)
        self._timeout_sec = float(config.get("activation_timeout_sec",
                                             _DEFAULT_TIMEOUT_SEC) or 0)

        self._config = None
        self._config_at = None

    @property
    def auth(self):
        return self._points.auth

    # ---- 設定 -------------------------------------------------------------
    # サブスクリプションの契約情報を返す。
    # **例外は投げない**。API 未実装 (404) もオフラインも enabled=False として返す
    # (ver6 resolve §4.2)。アカウントタブは開くたびにこれを呼ぶため、
    # ここで失敗するとタブごと開かなくなる。
    def config(self, force=False):
        if not force and self._config is not None and self._config_at is not None:
            if (_now() - self._config_at).total_seconds() < self._cache_sec:
                return self._config

        try:
            # 認証不要。未ログインでも価格を見せられるようにする。
            response = self._points.auth.client.get(_CONFIG_PATH, authenticated=False)
        except AutoEditError as e:
            # 404 = API が未実装。それ以外もここへ来るが、扱いは同じ。
            _logger.info("サブスクリプションの契約情報を取得できませんでした: %s", e)
            self._config = _DISABLED
            self._config_at = _now()
            return self._config

        self._config = BillingConfig.from_response(response)
        self._config_at = _now()
        return self._config

    # 導線を出してよいか。ログインしていなくても価格は出せるが、
    # 登録ボタンはログイン後にしか押せない (JWT が要る)。
    def is_available(self):
        return self.config().is_available()

    # ---- 決済画面の URL ---------------------------------------------------
    # Checkout の URL を返す。**利用者が押した操作のため例外は通す** (§4.2)。
    # 既に加入している場合は ApiError(code=already_subscribed)。
    def start_checkout(self):
        response = self._points.auth.client.post(_CHECKOUT_PATH)
        url = str((response or {}).get("url") or "")
        if not url:
            raise ApiError(tr("決済ページの URL を取得できませんでした。"))
        _logger.info("Stripe の決済ページを取得しました。")
        return url

    # カスタマーポータル (解約・カード変更・請求書) の URL を返す。
    # Stripe の顧客がまだ無い場合は ApiError(code=no_subscription)。
    def open_portal(self):
        response = self._points.auth.client.post(_PORTAL_PATH)
        url = str((response or {}).get("url") or "")
        if not url:
            raise ApiError(tr("サブスクリプション管理ページの URL を取得できませんでした。"))
        _logger.info("Stripe のカスタマーポータルを取得しました。")
        return url

    # ---- 加入状態と履歴 ---------------------------------------------------
    # GET /api/billing/subscription の応答をそのまま返す (ver6 resolve2 §4.4 / §9.1)。
    #   {type, status, cancelAtPeriodEnd, currentPeriodEnd, twitchChannel, history[]}
    #
    # config() と同じく**例外を投げない**。アカウントタブは開くたびにこれを呼ぶため、
    # ここで例外を通すとタブごと開かなくなる。取れなければ None を返し、
    # 呼び出し側は GET /api/points の種別だけを出す。
    def subscription(self):
        try:
            return self._points.auth.client.get(_SUBSCRIPTION_PATH) or {}
        except AutoEditError as e:
            # 404 = API が未実装。オフラインもここへ来るが、扱いは同じ。
            _logger.info("サブスクリプションの状態を取得できませんでした: %s", e)
            return None

    # ---- 反映待ち ---------------------------------------------------------
    # 支払いがサーバーへ反映される (unlimited が立つ) のを待つ。
    #
    # 支払いを取りやめてブラウザを閉じた場合もタイムアウトで False になる。
    # 異常ではないため例外は投げず、呼び出し側は「確認できなかった」と伝える。
    #
    # should_continue … 中断させるためのフック (画面を閉じたら False を返す)。
    # on_tick         … 1 周ごとに呼ばれる。経過秒を渡す (進捗表示用)。
    def wait_for_activation(self, should_continue=None, on_tick=None,
                            timeout_sec=None, poll_sec=None):
        timeout = float(timeout_sec if timeout_sec is not None else self._timeout_sec)
        interval = max(float(poll_sec if poll_sec is not None else self._poll_sec),
                       _MIN_POLL_SEC)
        if timeout <= 0:
            return False

        deadline = time.monotonic() + timeout
        while True:
            if should_continue is not None and not should_continue():
                return False

            if self.is_subscribed():
                _logger.info("サブスクリプションの反映を確認しました。")
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _logger.info("サブスクリプションの反映を %.0f 秒待ちましたが確認できませんでした。",
                             timeout)
                return False

            if on_tick is not None:
                on_tick(timeout - remaining)

            # 残り時間より長く眠らない (最後の 1 回が間延びしないようにする)。
            time.sleep(min(interval, remaining))

    # 現在サブスク中か。通信できなければ False (例外は投げない)。
    # 残高キャッシュを素通りさせるため force=True で取り直す。
    def is_subscribed(self):
        try:
            balance = self._points.balance(force=True)
        except AutoEditError as e:
            # 瞬断で待つのをやめない。次の周回で取り直す。
            _logger.debug("サブスクリプションの状態を確認できませんでした: %s", e)
            return False
        return bool((balance or {}).get("unlimited"))

    # 現在の種別 (None / Stripe / TwitchSub)。取得できなければ None を返す。
    def subscription_type(self):
        balance = self._points.last_balance()
        if not balance:
            return TYPE_NONE
        return str(balance.get("subscriptionType") or TYPE_NONE)
