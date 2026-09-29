# ver6 resolve — Stripe によるサブスクリプション (autoedit クライアント側)

対象: `autoedit` (本書の実装範囲) / `StretheusAPI` (契約のみ定義。実装は別途)
関連: `StretheusPlan/plan.md` 横断論点 #3、`homepage/doc/request/resolve5.md`

作成日: 2026-09-28

---

## 0. 実装状況 (2026-09-28)

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | `src/services/billing.py` (契約 / Checkout / ポータル / 反映待ち) | 完了 |
| 2 | `src/gui/account_tab.py` にサブスクリプション節を追加 | 完了 |
| 3 | `src/settings/setting.json` に `billing` セクション | 完了 |
| 4 | `tests/test_billing.py` (縮退とポーリングの単体テスト 17 件) | 完了 |
| — | API 側 (`StretheusAPI`) の実装 | **完了 (2026-09-28)** — `StretheusAPI/docs/request/resolve3.md` |
| — | **実 Stripe での疎通確認** | **未実施 (API とアカウント審査が要る)** |

API 側の実装は完了したが、**Stripe アカウントの審査と Price の作成が済むまで
`Stripe:PriceLabel` などが空のため `enabled = false`** が返る。その間クライアントは
**サブスクリプション節を出さない**。
`GET /api/billing/config` が 404 / オフラインを返す状態でも、
アカウントタブは今までどおり開き、既存の動作は変わらない (§4.4)。

---

## 1. 前提の確認

### 1.1 すでにあるもの

| 仕組み | 実体 | 本件での役割 |
| --- | --- | --- |
| 認可コードフロー + JWT | `services/stretheus_auth.py` | Stripe の API 呼び出しも同じ JWT で認証する |
| HTTP クライアント | `services/api_client.py` | 401 リフレッシュ・ProblemDetails の code 化をそのまま使う |
| ポイント / サブスク判定 | `services/points.py` | **サブスクの状態はここから読む**。専用の状態 API を増やさない |
| サブスク判定のキャッシュ | `points.py` の `_remember_subscription` | オフライン時の救済。Stripe でも同じ経路に載る |
| アカウントタブ | `gui/account_tab.py` | サブスク導線の置き場所。Twitch 用のボタンが既にある |
| ライセンス抽象 | API 側 `ILicenseProvider` | Stripe 実装を 1 本足すだけで済む設計になっている |

### 1.2 ここで増やさないもの

* **カード情報に触れるコードは一切書かない。** 決済は Stripe がホストする画面
  (Checkout / カスタマーポータル) で完結させ、クライアントは URL をブラウザで
  開くだけにする。PySide6 側に決済フォームを持たせると PCI DSS の対象になる。
* **サブスク状態の専用エンドポイントを作らない。** `GET /api/points` が既に
  `unlimited` と `subscriptionType` を返しており、残高インジケータと
  アカウントタブがそれを読んでいる。ここへ Stripe の情報を載せる。
* **ライブラリを追加しない。** `stripe` の Python SDK はクライアントには不要
  (秘密鍵を配布実行ファイルへ入れることになるため、そもそも入れてはいけない)。
  使うのは標準ライブラリと既存の `ApiClient` だけ。

---

## 2. 全体の流れ

```
[autoedit]                    [StretheusAPI]              [Stripe]
    |                               |                         |
    |-- GET /api/billing/config --->|                         |
    |<-- {enabled, priceLabel} -----|                         |
    |                               |                         |
  (「登録」を押す)                   |                         |
    |-- POST checkout-session ----->|-- Checkout Session 作成 ->|
    |<-- {url} ---------------------|<-- url -----------------|
    |                               |                         |
  ブラウザで url を開く -------------------------------------->|
    |                               |                         |
    |                               |<-- Webhook (署名付き) ---|
    |                               |  サブスクを有効化          |
    |                               |                         |
  (ポーリング)                       |                         |
    |-- GET /api/points ----------->|                         |
    |<-- {unlimited: true} ---------|                         |
    |  → 画面を「加入中」に更新        |                         |
```

**要点は「クライアントは決済の完了を直接知らない」こと。** ブラウザ側の成功画面と
Webhook は非同期で、順序も保証されない。そこで **クライアントは `GET /api/points`
を短い間隔で叩いて `unlimited` が立つのを待つ** (§4.5)。成功画面へ戻る仕組み
(ローカル HTTP サーバでの受信) は**作らない**。ログインの認可コードと違い、
Stripe の完了は Webhook でしか確定しないため、戻ってきても待つことに変わりはなく、
ポート競合 (§6.3 の implicit flow と同じ問題) を増やすだけになる。

---

## 3. API の契約 (StretheusAPI 側で実装する / 本書の範囲外)

### 3.1 `GET /api/billing/config`

認証不要。クライアントが「サブスク導線を出してよいか」を判断するためだけに使う。

```json
{
  "enabled": true,
  "provider": "Stripe",
  "priceLabel": "月額 980 円 (税込)",
  "manageable": true
}
```

| フィールド | 意味 |
| --- | --- |
| `enabled` | Stripe の設定が揃っていれば `true`。未設定なら `false` |
| `provider` | 表示用。`"Stripe"` |
| `priceLabel` | 表示用の価格文字列。**クライアントに価格をハードコードしない** |
| `manageable` | カスタマーポータルが使えるなら `true` |

`priceLabel` をサーバーから配る理由は、価格改定のたびに**配布済みの exe が
古い価格を表示し続ける**のを避けるため。homepage 側は `legalData.ts` の
`SUBSCRIPTION.priceLabel` が定義元なので、API 側の設定値と**同じ文字列**を
入れる運用にする (§8.3)。

### 3.2 `POST /api/billing/checkout-session`

要認証。応答の `url` をブラウザで開く。

```json
{ "url": "https://checkout.stripe.com/c/pay/...", "expiresAt": "2026-09-28T12:00:00Z" }
```

* 既にサブスク中のユーザーが呼んだ場合は **409 `already_subscribed`** を返す。
  クライアントはこれを「加入済み」として扱い、ポータルへ誘導する。

### 3.3 `POST /api/billing/portal-session`

要認証。解約・カード変更・請求書の確認を行うカスタマーポータルの URL を返す。

```json
{ "url": "https://billing.stripe.com/p/session/..." }
```

* Stripe の顧客がまだ無いユーザーは **404 `no_subscription`**。

### 3.4 Webhook とライセンス判定 (API 側)

* `POST /api/billing/webhook` — 認証なし。**Stripe の署名検証が必須**。
  受ける種類は `checkout.session.completed` /
  `customer.subscription.updated` / `customer.subscription.deleted` の 3 つ。
* `StripeLicenseProvider : ILicenseProvider` を追加する。
  `SubscriptionType => "Stripe"`、**`Order => -10`**。
  既存の `TwitchLicenseProvider` は `Order => 0` なので Stripe が先に評価される。
  両方に該当する利用者には、**本人が課金していて解約もできる Stripe の方**を
  表示したいため (Twitch サブは本人が管理できない)。
* `LicenseService` は変更不要。プロバイダを 1 本登録するだけで済む。

---

## 4. クライアント側の設計

### 4.1 `src/services/billing.py`

```
BillingConfig      … config の応答を表す軽い値オブジェクト
BillingService     … 契約の取得 / Checkout / ポータル / 反映待ち
```

| メソッド | 役割 | 失敗時 |
| --- | --- | --- |
| `config(force=False)` | `GET /api/billing/config`。既定 1 時間キャッシュ | 無効な `BillingConfig` を返す (例外を投げない) |
| `is_available()` | 導線を出してよいか | `False` |
| `start_checkout()` | Checkout の URL を取得して**返す** | 例外を投げる (押した操作なので理由を出す) |
| `open_portal()` | ポータルの URL を取得して**返す** | 例外を投げる |
| `wait_for_activation(...)` | `GET /api/points` を叩き `unlimited` が立つのを待つ | `False` (タイムアウト) |

`start_checkout` / `open_portal` は **URL を返すだけ**でブラウザは開かない。
ブラウザを開くのは GUI の責務 (`QDesktopServices`) とし、サービス層を
PySide6 に依存させない。`login_check.py` / `seed_points.py` と同じく、
GUI なしでも動かせる状態を保つ。

### 4.2 契約情報の取得も必ずワーカースレッドで行う

`config()` は HTTP を伴う。GUI スレッドから直接呼ぶと、**API へ到達できないときに
タイムアウト (既定 15 秒) ぶん画面が固まる**。アカウントタブは開くたびにこれを
必要とするため、残高・履歴と同じく `QThread` (`BillingConfigWorker`) で取りに行き、
結果が届いてから節を描く。

節を描くのに必要な情報は 2 つあり、**届くタイミングが違う**。

| 情報 | 出どころ |
| --- | --- |
| 価格・導線を出してよいか | `GET /api/billing/config` (`BillingConfigWorker`) |
| 加入種別 (`None` / `Stripe` / `TwitchSub`) | `GET /api/points` (`BalanceWorker`) |

**両方そろうまで節を出さない。** 片方だけで描くと、加入済みの利用者に
「登録」ボタンが一瞬見える。押されれば 409 になるだけだが、
課金まわりで誤解を与える表示は出さないほうがよい。

### 4.3 `config()` を例外にしない理由

アカウントタブは**開くたびに** `config()` を呼ぶ。API が未実装 (404) でも、
オフラインでも、ここで例外を投げるとタブが開かなくなる。ポイント処理と同じく
「課金まわりの失敗で既存機能を壊さない」を最優先にする
(ver5 resolve.md §4-3 と同じ方針)。

一方 `start_checkout` / `open_portal` は**利用者が明示的に押した操作**なので、
黙って何も起きないより理由を出した方がよい。ここだけは例外を通す。

### 4.4 API 未実装時のふるまい

| 状況 | `config()` の結果 | 画面 |
| --- | --- | --- |
| API に `/api/billing/config` が無い (404) | `enabled = False` | サブスク節を出さない |
| オフライン | `enabled = False` | 同上 |
| `enabled = false` (Stripe 未設定) | `enabled = False` | 同上 |
| 正常 | `enabled = True` | サブスク節を出す |

**今日の dev 環境ではすべて 1 行目に該当する。** 画面は現状と同じ見た目になり、
API を実装してデプロイした時点で自動的に節が現れる。クライアントの再配布は要らない。

### 4.5 反映待ち (`wait_for_activation`)

```
interval_sec 秒おきに GET /api/points を叩き、unlimited が true になったら True。
timeout_sec を超えたら False。
```

* 既定は **3 秒おき・最大 180 秒** (`setting.json`)。Webhook は通常数秒で届く。
* ポーリングは `PointsService.balance(force=True)` を使う。残高キャッシュを
  素通りさせる必要があるため。副作用としてサブスク判定のキャッシュも更新される。
* 失敗 (タイムアウト) は異常ではない。**ブラウザを閉じて支払いをやめた場合も
  ここに来る。** そのため文言は「確認できませんでした」であって「失敗」ではない。
* 通信エラーは握って続行する。1 回の失敗で待つのをやめると、
  Wi-Fi の瞬断で「加入したのに反映されない」ように見えてしまう。

### 4.6 アカウントタブの変更 (`gui/account_tab.py`)

既存の「Twitch のサブスクページを開く」ボタンは**そのまま残す**。Twitch サブの
判定は引き続き有効で、役割が違う。その下に Stripe の節を足す。

```
[サブスクリプション]                ← 見出し
月額 980 円 (税込)                  ← config.priceLabel
ポイントを消費せず、透かしの入らない出力を回数無制限で行えます。
[ サブスクリプションに登録 ]         ← 未加入のとき
[ サブスクリプションを管理 ]         ← 加入中のとき (manageable のみ)
```

状態の出し分けは `GET /api/points` の `unlimited` / `subscriptionType` で行う。

| `subscriptionType` | 表示 | ボタン |
| --- | --- | --- |
| `None` | 未加入 | 登録 |
| `Stripe` | 加入中 (Stripe) | 管理 |
| `TwitchSub` | Twitch サブスクで特典適用中 | **登録**を出す (§4.7) |

#### 4.7 Twitch サブスク中の利用者に「登録」を出す理由

Twitch サブは切れる。切れた瞬間に透かしが入るようになるため、
「Twitch サブが切れても続けたい」利用者には Stripe を選ばせたい。
ただし**二重課金の注意書きを添える**。ここを黙って出すと
「今も無制限なのに課金させられた」という苦情になる。

文面: 「現在は Twitch サブスクリプションの特典で無制限にご利用いただけます。
Stripe のサブスクリプションに登録すると、Twitch サブスクが切れたあとも
継続してご利用いただけます (両方に加入した場合、料金は二重に発生します)。」

### 4.8 押したあとの流れ (GUI)

1. ボタンを無効化し「ブラウザで手続きしてください…」に変える。
2. `BillingService.start_checkout()` をワーカースレッドで呼ぶ (HTTP のため)。
3. 返った URL を `QDesktopServices.openUrl` で開く。
4. `wait_for_activation` をワーカースレッドで回す。
   進行中は「支払いの反映を待っています…」を出す。
5. `True` → `reload()` して「加入中」へ。`False` → 断り書きを出し、
   「更新」ボタンで再確認できる旨を添える。

**4 と 5 の間に画面を固めない。** 利用者はブラウザ側で数分かけることがあり、
モーダルで待つと「アプリが固まった」と見える。タブは操作可能なままにする。

---

## 5. `setting.json` の追加

```json
"billing": {
  "config_cache_sec": 3600,
  "activation_poll_sec": 3,
  "activation_timeout_sec": 180
}
```

| キー | 既定 | 意味 |
| --- | --- | --- |
| `config_cache_sec` | 3600 | `GET /api/billing/config` の再取得間隔 |
| `activation_poll_sec` | 3 | 反映待ちのポーリング間隔 |
| `activation_timeout_sec` | 180 | 反映待ちの上限 |

価格は**置かない**。サーバーから配る (§3.1)。既存の `points.subscribe_url`
(Twitch 用) はそのまま残す。

---

## 6. エラーの扱い

| 状況 | HTTP | クライアントの扱い |
| --- | --- | --- |
| 既に加入中 | 409 `already_subscribed` | 「すでに加入しています」→ 画面を更新 |
| 顧客が無い | 404 `no_subscription` | 「登録」側の導線へ戻す |
| 未ログイン / 期限切れ | 401 | 既存の `ReauthRequiredError` 経路。ログインを促す |
| API 未実装 | 404 (config) | 節ごと出さない (§4.4) |
| オフライン | — | 節ごと出さない。加入済みの表示は既存のキャッシュで維持される |

---

## 7. 検証

### 7.1 単体テスト (`tests/test_billing.py`)

| # | 内容 | 期待 |
| --- | --- | --- |
| 1 | `config()` が 404 | `enabled = False`、例外なし |
| 2 | `config()` がオフライン | `enabled = False`、例外なし |
| 3 | `config()` の結果がキャッシュされる | 2 回目は HTTP を呼ばない |
| 3b | `config(force=True)` で取り直す | HTTP を 2 回呼ぶ |
| 3c | `priceLabel` が空の応答 | `is_available()` が `False` |
| 4 | `is_available()` が `enabled=false` で `False` | — |
| 5 | `start_checkout()` が URL を返す | `https://checkout...` |
| 6 | `start_checkout()` が 409 で例外 | `ApiError`、`code = already_subscribed` |
| 7 | `wait_for_activation` が `unlimited` で `True` | ポーリング回数が最小 |
| 8 | `wait_for_activation` がタイムアウトで `False` | 例外なし |
| 9 | ポーリング中の通信エラーで止まらない | 次の周回へ進む |
| 10 | `should_continue` が `False` を返す | 一度も照会せずに戻る |
| 11 | `activation_timeout_sec = 0` | 待たずに `False` |

### 7.2 実環境 (API 実装後)

1. Stripe のテストモードで Checkout を通し、`GET /api/points` が
   `unlimited: true` / `subscriptionType: "Stripe"` を返すこと。
2. その状態で出力を 1 本行い、**ポイントが減らず透かしも入らない**こと。
3. カスタマーポータルで解約し、期間末以降に `unlimited: false` へ戻ること。
4. Webhook を止めた状態で支払い、`wait_for_activation` が
   タイムアウトしても画面が壊れないこと。

---

## 8. 本書の範囲外 (別途必要な作業)

### 8.1 StretheusAPI 側

* `BillingController` (config / checkout-session / portal-session / webhook)
* `StripeLicenseProvider` と DI 登録
* `StripeCustomer` テーブル (`UserId` ↔ Stripe の `customer_id` / `subscription_id`)
* Webhook の署名検証と冪等処理 (Stripe は同じイベントを再送する)
* Key Vault へ `Stripe--SecretKey` / `Stripe--WebhookSecret` / `Stripe--PriceId`
* Bicep / `appsettings` への設定追加

### 8.2 Stripe 側

* アカウント開設と審査 (特商法・プライバシー・返金の 3 ページが要る →
  `homepage/doc/request/resolve5.md` で実装済み)
* Product / Price (月額 980 円・JPY) の作成
* Webhook エンドポイントの登録
* カスタマーポータルの有効化と表示項目の設定

### 8.3 価格の二重管理について

価格は **Stripe の Price**・**API の `priceLabel`**・**homepage の
`SUBSCRIPTION`** の 3 箇所に現れる。API が Stripe から価格を取得して
`priceLabel` を組み立てれば 2 箇所に減らせるが、Stripe への問い合わせが
`GET /api/billing/config` のたびに発生する。**設定値として持ち、
価格改定時に 3 箇所を同時に直す**運用とし、手順を README へ書く。
