# ver6 resolve2 — サブスクリプション導線をメイン画面へ出す (修正設計書)

対象: `autoedit` (本書の実装範囲) / `StretheusAPI` (契約のみ定義。実装は別途)
入力: `docs/request/ver6/request2.md`
前提: `docs/request/ver6/resolve.md` (Stripe 対応。本書はその**画面構成の変更**にあたる)

作成日: 2026-09-29

---

## 0. 要望と対応方針の対応表

| # | 要望 (request2.md) | 対応 | 節 |
| --- | --- | --- | --- |
| A | 設定画面にある「サブスクリプションを登録する画面」をメイン画面の設定ボタンの左へ置く | コーナーへ「サブスクリプション」ボタンを追加し、押すと新設の画面を開く | §2.1 |
| B | サブスクリプション画面を新規で作成する | `src/gui/subscription_window.py` を新設 | §2.2 / §4.1 |
| C | そこに Twitch の Login ボタンと購入ボタンを置く | 同画面に 2 つ並べる | §2.2 |
| ① | ログインしたアカウントが Twitch の `tatsumic` をサブスクしているなら購入ボタンを押せなくする | `GET /api/points` の `subscriptionType == "TwitchSub"` で無効化 | §3 / §6 |
| ①後 | ① 後にメイン画面へ戻るとサブスクリプションボタンが無効・文言にチェックマーク (色 `#9147FF`) | `theme.check_icon("twitch")` をボタンのアイコンとして付け、無効化する | §4.2 / §4.5 |
| ② | 購入ボタンで既存のサブスク処理 (Stripe Checkout) を走らせる | 既存 `BillingService.start_checkout()` と反映待ちをそのまま使う | §4.1 |
| ②後 | ② 後にメイン画面へ戻るとサブスクリプションボタンが無効・文言にチェックマーク | 同上。色はテーマの `success` (§10 論点 2) | §4.2 / §4.5 |
| ③ | 設定画面のアカウントタブにサブスク状態と履歴を表示する | 「サブスクリプション状態」節と「サブスクリプション履歴」表を追加 | §2.3 / §4.4 |

### 0.1 変更するファイル

| ファイル | 種別 | 内容 |
| --- | --- | --- |
| `src/gui/subscription_window.py` | **新規** | サブスクリプション画面 (Twitch ログイン / 購入) |
| `src/gui/billing_workers.py` | **新規** | 課金まわりのワーカー (`account_tab.py` から移設 + 追加) |
| `src/gui/main_window.py` | 変更 | コーナーへボタン追加・加入状態でのチェックマークと無効化 |
| `src/gui/points_indicator.py` | 変更 | `balance_changed` シグナルを追加 (残高の二重取得を避ける) |
| `src/gui/account_tab.py` | 変更 | 登録導線を撤去し、状態節と履歴表を追加 |
| `src/gui/theme.py` | 変更 | `CHECK_GLYPH` / `check_icon()` / Twitch ボタンの見た目 |
| `src/services/billing.py` | 変更 | `subscription()` と `purchase_blocked_reason()` を追加 |
| `src/settings/settings_window.py` | 変更 | `DEFAULT_SETTINGS` に `billing` を追加 (**既存の欠陥の修正** / §5.1) |
| `tests/test_billing.py` | 変更 | 判定関数と `subscription()` の単体テストを追加 |
| `tests/test_subscription_ui.py` | **新規** | ボタンの状態遷移とチェックマークの色 |
| `tests/test_settings_migration.py` | 変更 | `billing` セクションが保存で消えない回帰テスト |

---

## 1. 調査で分かった前提 (ここを踏まえて設計する)

### 1.1 Stretheus のログインは、すでに Twitch のログインである

`services/stretheus_auth.py` のログインは **Twitch の認可コードフロー**で、
`id.twitch.tv/oauth2/authorize` を開き、API (`POST /api/auth/twitch/login`) が
JWT を返す。ユーザー情報にも `twitchUserId` が入る。
`archive/twitch_auth.py` の implicit フロー (Helix 直叩き用) とは別物で、
**両者の間でトークンを受け渡さない**という取り決めがある。

したがって要望の「Twitch の Login ボタン」は、**既存の Stretheus ログインそのもの**
として実装する。新しいログイン機構は作らない。画面上の文言を
「Twitch でログイン」にし、Twitch のブランド色を与えて、それが Twitch の
ログインであることを示す。

### 1.2 加入種別はすでに `GET /api/points` が返している

`points.py` が受け取る残高に `unlimited` と `subscriptionType`
(`"None"` / `"Stripe"` / `"TwitchSub"`) が入っており、`billing.py` の
`TYPE_TWITCH = "TwitchSub"` がそれを指している。判定の権威はサーバー側にあり、
透かしを入れるかどうかも同じ値で決まる (ver5 resolve §4-3 / R9)。

### 1.3 コーナーには既に 2 つ載っている

`main_window.py:1092` の `theme.install_tab_corner` は**ウィジェットの一覧**を
受け取れる (ver5 resolve §5.6 で 2 個になった)。残高インジケータと設定ボタンが
並んでおり、ここへ 3 つめを足すだけで済む。

### 1.4 `setting.json` の `billing` セクションは今のコードでは消える

`settings_window.DEFAULT_SETTINGS` に `billing` が無いため、
`_merge_with_defaults()` (`settings_window.py:1149`) が
**`DEFAULT_SETTINGS` に無いセクションを落とす**。
`load_settings()` → `save_settings()` が一度でも走ると `billing` が消え、
`BillingService` はモジュール既定値で動き続ける (値が同じため今は動作に出ていない)。
実際に作業ツリーでは既に消えている (`git diff src/settings/setting.json`)。
本件で `billing` を触る前に、ここを直す (§5.1)。

---

## 2. 画面構成

### 2.1 メイン画面 (`main_window.py`)

```
┌───────────────────────────────────────────────────────────────────────┐
│ クリップ用 │ アーカイブ切り抜き用    残り 120 pt / 次回リセット 10/11  [サブスクリプション] [⚙] │
├───────────────────────────────────────────────────────────────────────┤
│                          (タブの中身)                                   │
```

* 「サブスクリプション」ボタンは**設定ボタンの左**、残高インジケータの右へ置く。
  `install_tab_corner(self.tabs, [points_indicator, subscription_button, settings_button])`。
* 未加入: 文言のみ・押せる。押すとサブスクリプション画面が開く。
* 加入中: **文言の左へチェックマーク**を付け、ボタンを**無効**にする。
  * Twitch サブ → `#9147FF` (Twitch のブランド色)
  * Stripe → テーマの `success` (ダーク `#6BE0A8` / ライト `#1B7A4B`)
* 出力の実行中は設定ボタンと同様に無効にする。理由: 実行中に加入状態が変わると、
  走っている予約の透かし判定 (予約時にサーバーが決めた値) と画面の表示が食い違う。

> **幅の注意**: メイン画面の既定幅は 700px (`ui.main_window.width_px`)。
> タブ 2 枚 + 残高 + 新ボタン + 歯車が収まらないと QTabWidget はタブバーを縮めて
> スクロールボタンを出す。文言は「サブスクリプション」で実装し、
> **700px でスクロールボタンが出ないことを実機で確認する** (§8.2-1)。
> 収まらない場合の逃げは「サブスク」への短縮とし、幅の既定値は変えない
> (利用者が広げた値を上書きしてしまうため)。

### 2.2 サブスクリプション画面 (新規 `subscription_window.py`)

設定画面と同じ**独立したトップレベルウィンドウ** (`QWidget`) とする。
モーダルにしない。ログインの認可待ちが最大 180 秒、支払いの反映待ちが最大 180 秒あり、
モーダルで待つと「アプリが固まった」と見える (ver6 resolve §4.8 と同じ理由)。

```
┌─ サブスクリプション ────────────────────────┐
│ サブスクリプション                           │ ← 見出し (mark_title)
│ 月額 980 円 (税込)                           │ ← config.price_label
│ ポイントを消費せず、透かしの入らない出力を    │ ← 説明 (mark_note)
│ 回数無制限で行えます。                       │
│                                              │
│ 状態: 未ログイン                             │ ← 状態ラベル
│                                              │
│ [   Twitch でログイン   ]                    │ ← Twitch 紫の塗り (#twitchButton)
│ [        購入         ]                      │ ← 主要動作 (mark_primary)
│                                              │
│ ログインすると購入できます。                 │ ← 注記 (無効の理由・手続きの案内)
└──────────────────────────────────────────────┘
```

* **価格・説明は `GET /api/billing/config` から取る。** 配布済みの exe へ価格を
  焼き込まない (ver6 resolve §3.1)。
* 契約情報が取れない (API 未実装・オフライン) 間は、購入ボタンを**無効**にし
  理由を注記に出す。アカウントタブのように節ごと隠すのではなく、
  **この画面はそれ自体が目的なので空にしない**。
* ウィンドウの寸法は `settings_window.WINDOW_WIDTH` と同じ扱いでモジュール定数に置く
  (`WINDOW_WIDTH = 420` / `WINDOW_HEIGHT = 320`)。姉妹画面と流儀を揃える。

### 2.3 設定画面のアカウントタブ (`account_tab.py`)

登録の導線は**メイン画面へ移す**ため、アカウントタブからは「サブスクリプションに登録」
ボタンを外す。**「サブスクリプションを管理」(カスタマーポータル) は残す。**
メイン画面のボタンは加入すると無効になるため、残さないと解約・カード変更へ
たどり着けなくなる。役割を次のように分ける。

| 場所 | 役割 |
| --- | --- |
| メイン画面 → サブスクリプション画面 | **加入する** (Twitch ログイン / 購入) |
| 設定画面 → アカウント | **状態を確認する・履歴を見る・管理する** (解約など) |

```
[アカウント]
ログイン中: たつみっく
残り 120 pt / 次回リセット 10/11
[ログアウト] [更新]
[Twitch のサブスクページを開く]          ← 既存 (points.subscribe_url。役割が違うため残す)

[サブスクリプション状態]                  ← 新規 (③)
種別: Twitch サブスクリプション (tatsumic)
状態: 有効
次回更新: 2026/10/28
[サブスクリプションを管理]                ← 既存 (Stripe 加入中のみ)
登録はメイン画面の「サブスクリプション」から行えます。   ← 注記

[サブスクリプション履歴]                  ← 新規 (③)
| 日時        | 種別   | 内容               |
| 09/28 12:03 | Stripe | 加入 (月額 980 円) |
| 08/28 12:03 | Stripe | 更新               |

[ポイント履歴]                            ← 既存。見出しを「履歴」から改名して区別する
| 日時 | 内容 | 増減 | 残高 |
```

---

## 3. 「`tatsumic` をサブスクしているか」をどこで判定するか

### 3.1 採る案: サーバーの判定 (`GET /api/points` の `subscriptionType`)

Twitch ログイン後に `PointsService.balance(force=True)` を取り直し、
`subscriptionType == "TwitchSub"` なら購入ボタンを無効にする。

採る理由は **判定を 1 つにするため**である。透かしを入れるか・ポイントを引くかは
サーバーが決めており (R9)、Twitch サブの判定はすでにそこに載っている。
クライアントが別の経路で判定を持つと、
「クライアントは Twitch サブだから買わなくてよいと言うのに、出力すると透かしが入る」
という食い違いが起こり得る。**購入させないという判断と、特典を与える判断は
同じ値から出さなければならない。**

### 3.2 採らない案: クライアントから Helix を直接叩く

`archive/twitch_auth.py` と同じ implicit フローでログインし、
`GET /helix/subscriptions/user?broadcaster_id=…` を自分で叩く案は採らない。

| 理由 | 内容 |
| --- | --- |
| 判定が 2 つになる | §3.1 のとおり、特典の判定とずれたときに説明できない |
| スコープとトークンが増える | `user:read:subscriptions` を持つ別のトークンを保存することになる |
| ポート衝突 | implicit フローは 3737 を使い、認可コードフローと同時に走らせられない (ver6 resolve §6.3 / `stretheus_auth.login_in_progress`) |
| 迂回できる | クライアント内の判定は改変できる。UX の制御としては働くが、根拠にはならない |

### 3.3 API 側が `TwitchSub` を返せない場合の縮退 (要確認事項 / §10 論点 1)

`subscriptionType` が `"TwitchSub"` になるには、API 側が
(a) Twitch の認可で `user:read:subscriptions` を要求しており、
(b) 判定対象のチャンネル (`tatsumic` / user id `669964096`) を設定として持ち、
(c) `TwitchLicenseProvider` がそれで判定している、
の 3 つが要る。`autoedit` 側からは (a)〜(c) を確認できない。

**API 側が未対応でも本書の画面実装は成立する** (その場合 ① が効かず、
Twitch サブの利用者にも購入ボタンが押せるままになる)。
API 側の対応を待つのが本筋で、待てない場合の代替は次のとおり。
**これは第 2 案であり、採る場合は §3.2 の不利を承知で選ぶこと。**

```
setting.json (代替案を採る場合にだけ足す)
"billing": {
  "twitch_channel": "tatsumic",        // 表示と判定に使うチャンネル
  "twitch_channel_id": "669964096"     // Helix の broadcaster_id (空なら login から引く)
}
```

`archive.auth.client_id` の既存トークンとは別に
`user:read:subscriptions` 付きで implicit ログインし、
`GET /helix/subscriptions/user` の 404 / 200 で判定する。
**この判定は購入ボタンの出し分けにのみ使い、透かしの判断には一切使わない。**

---

## 4. 実装の詳細

### 4.1 `src/gui/subscription_window.py` (新規)

```
SubscriptionWindow(QWidget)
    subscription_changed = Signal()      ← 加入が確認できたとき (メイン画面へ通知)

    __init__(points, settings, parent=None)
    reload()                             ← 状態の取り直し (開いたとき / 反映後)
    _on_twitch_login()                   ← LoginWorker (既存) を回す
    _on_purchase()                       ← BillingUrlWorker("checkout") → 反映待ち
    _apply_state()                       ← ボタンの有効/無効と文言を決める
```

使うワーカーはすべて既存のものを流用する。**新しい通信経路は増やさない。**

| 目的 | ワーカー | 出どころ |
| --- | --- | --- |
| Twitch ログイン | `LoginWorker` | `points_indicator.py` (既存) |
| 加入種別の取得 | `BalanceWorker` | `points_indicator.py` (既存) |
| 価格・可否の取得 | `BillingConfigWorker` | `billing_workers.py` (移設) |
| Checkout の URL | `BillingUrlWorker` | `billing_workers.py` (移設) |
| 支払いの反映待ち | `ActivationWorker` | `billing_workers.py` (移設) |

#### ワーカーを `billing_workers.py` へ移す理由

`BillingConfigWorker` / `BillingUrlWorker` / `ActivationWorker` は今
`account_tab.py` (設定画面のタブ) にある。新しい画面がそこから import すると、
**メイン画面の部品が設定画面のタブに依存する**ことになる。
`points_indicator.py` と同じ位置づけの `src/gui/billing_workers.py` へ移し、
`account_tab.py` と `subscription_window.py` の双方がそこから読む。
`TransactionsWorker` (ポイント履歴) はアカウントタブ専用なので動かさない。

#### 押したあとの流れ (① Twitch ログイン)

1. 「Twitch でログイン」を押す → ボタンを無効化し「ブラウザで許可してください…」。
   `stretheus_auth.login_in_progress()` が真なら押させない
   (残高インジケータ側のログインと同時に走るとポートが開けない)。
2. `LoginWorker` が成功 → `reload()`。
3. `BalanceWorker` と `BillingConfigWorker` の**両方**の結果がそろってから
   `_apply_state()` を呼ぶ。片方だけで描くと、Twitch サブの利用者に
   購入ボタンが一瞬押せる状態が見える (ver6 resolve §4.2 と同じ理由)。
4. `subscriptionType == "TwitchSub"` → 購入ボタンを無効にし、注記に
   「Twitch のサブスクリプションで特典が適用されています。購入は不要です。」
5. `subscription_changed` を発火。メイン画面がチェックマークを付ける。

#### 押したあとの流れ (② 購入)

ver6 resolve §4.8 の流れをそのまま使う。
`BillingUrlWorker("checkout")` → `QDesktopServices.openUrl` →
`ActivationWorker` で `unlimited` が立つのを待つ → 立ったら `reload()` と
`subscription_changed`。立たなかった場合は「確認できませんでした」と出す
(支払いを取りやめてブラウザを閉じた場合もここへ来るため、失敗とは書かない)。

反映待ちは最大 180 秒動く。`closeEvent` で `requestInterruption()` して
`wait(5000)` する (`account_tab.py` と同じ後始末)。

### 4.2 `main_window.py` の変更

```python
# サブスクリプション画面を開くボタン (request2 A)。設定ボタンの左へ置く。
self.subscription_button = QPushButton("サブスクリプション")
self.subscription_button.setIconSize(QSize(theme.BUTTON_ICON_PX, theme.BUTTON_ICON_PX))
self.subscription_button.clicked.connect(self._on_open_subscription)

theme.install_tab_corner(
    self.tabs,
    [self.points_indicator, self.subscription_button, self.settings_button],
    spacing=theme.BUTTON_ICON_PX // 2)
```

| 追加/変更するもの | 内容 |
| --- | --- |
| `_on_open_subscription()` | `_on_open_settings()` と同じ流儀。既に開いていれば前面へ出すだけ。参照を `self._subscription_window` に持つ (GC で即閉じるのを防ぐ / resolve4 M3) |
| `_refresh_subscription_button(balance)` | 加入種別からアイコンと有効/無効を決める |
| `_on_tab_running_changed()` | 設定ボタンと同様に実行中は無効化 |
| `_on_theme_changed()` | チェックマークを描き直す (`QPixmap` へ焼くためテーマに追随しない / resolve4 §5.10-3) |

```python
# 加入状態をボタンへ反映する (request2 ①後 / ②後)。
# 加入中は押せなくし、文言の左へ種別ごとの色のチェックマークを出す。
def _refresh_subscription_button(self, balance):
    kind = theme.subscription_check_kind(balance)   # "twitch" / "stripe" / 未加入は ""
    self.subscription_button.setIcon(
        theme.check_icon(kind, self._settings) if kind else QIcon())
    subscribed = bool(kind)
    self.subscription_button.setEnabled(not subscribed and not self._running_tabs)
    self.subscription_button.setToolTip(
        "サブスクリプションに加入済みです" if subscribed else "サブスクリプションの登録")
```

#### 加入状態をどこから受け取るか (残高を二重に取らない)

`GET /api/points` を読む場所を増やさない。`PointsIndicator` に
`balance_changed = Signal(object)` を足し、`_on_loaded()` で表示に使った残高を
そのまま流す。メイン画面はそれを購読する。

```python
self.points_indicator.balance_changed.connect(self._refresh_subscription_button)
```

* 起動直後・定期更新 (既定 300 秒)・出力の完了後・ログイン直後は、
  いずれも既に `PointsIndicator` が残高を取り直している。**追加の通信は 0 件**。
* サブスクリプション画面を閉じたときは
  `destroyed` → `points_indicator.refresh(force=True)` を呼ぶ
  (設定画面と同じ流儀)。ここで 1 回だけ通信が増える。
  これが要望の「main_window に戻ってきた際には」に相当する。
* 画面を開いたままでも反映されるよう、`subscription_changed` シグナルでも
  同じ処理を呼ぶ。
* 未ログインのあいだは残高が届かないため、ボタンは「押せる・印なし」のままになる。
  未加入なので表示として正しい。

> **メイン画面のボタンは入口であって、関門ではない。** 加入済みかどうかの
> 正しい判断は、サブスクリプション画面が両方の情報をそろえてから行う (§4.1)。
> ボタンが一瞬押せても、開いた先で購入は無効になる。

### 4.3 `points_indicator.py` の変更

```python
class PointsIndicator(QWidget):
    login_changed = Signal(bool)
    # 表示に使った残高 (取れなければ最後に取得できた値 / None)。
    # メイン画面のサブスクリプションボタンがこれを見る。GET /api/points を
    # 読む場所を増やさないため、取得はここ 1 か所に保つ (ver5 resolve §6.4)。
    balance_changed = Signal(object)
```

`_on_loaded()` の末尾で
`self.balance_changed.emit(balance or self._points.last_balance())` を発火する。
`_apply_state()` は変更しない。

### 4.4 `account_tab.py` の変更 (③)

| 変更 | 内容 |
| --- | --- |
| 撤去 | `checkout_button` (「サブスクリプションに登録」)。代わりに注記で導線を案内する |
| 撤去 | `ActivationWorker` の呼び出し (購入がこのタブから消えるため反映待ちも不要) |
| 残す | `portal_button` (「サブスクリプションを管理」)・Twitch サブページのボタン |
| 追加 | 「サブスクリプション状態」節 (種別・状態・次回更新日) |
| 追加 | 「サブスクリプション履歴」表 (`SubscriptionWorker`) |
| 改名 | ポイント履歴の見出しを「履歴」→「ポイント履歴」 |

状態は 2 つの情報から作る。**種別は `GET /api/points`** (権威。既存)、
**期間と履歴は `GET /api/billing/subscription`** (新設。§9.1)。
後者が取れない (API 未実装・オフライン) ときは、
**種別だけを出し、履歴の表は出さない**。ver6 resolve §4.3 と同じ方針で、
課金まわりの失敗でタブが開かなくなることを避ける。

| `subscriptionType` | 種別の表示 |
| --- | --- |
| `None` | 未加入 |
| `Stripe` | サブスクリプション加入中 (Stripe) |
| `TwitchSub` | Twitch サブスクリプション (`twitchChannel` があればチャンネル名を添える) |

### 4.5 `theme.py` の変更 (チェックマークと Twitch ボタン)

```python
# チェックマーク (加入済みの印 / request2 ①後 ②後)
CHECK_GLYPH = "✓"          # U+2713

# Twitch のブランド色。利用者が変える値ではなく Twitch の規定で決まっているため
# 設定値にはせず、ここ 1 か所へ集約する。
TWITCH_COLOR = "#9147FF"
TWITCH_COLOR_HOVER = "#A970FF"
TWITCH_COLOR_PRESSED = "#772CE8"
TWITCH_BUTTON = "twitchButton"      # QSS の #twitchButton
```

```python
# 加入種別からチェックマークの種類を返す ("twitch" / "stripe" / 未加入は "")
def subscription_check_kind(balance): ...

# 加入済みの印を QIcon で返す。
#   twitch … Twitch のブランド色 (#9147FF)
#   stripe … テーマの success (ダーク #6BE0A8 / ライト #1B7A4B)
def check_icon(kind, settings=None): ...
```

#### 無効化したボタンで色が抜ける問題

`QIcon(pixmap)` は**無効時の絵をスタイルに自動生成させる**ため、灰色に褪せる。
加入中のボタンは無効にするので、そのままでは `#9147FF` が見えなくなる。
`check_icon()` は同じ `QPixmap` を `QIcon.Normal` と `QIcon.Disabled` の
両方へ明示的に登録して、色を保ったままにする。
`glyph_icon()` は他の用途で使われているため触らず、`check_icon()` を別に置く。

#### Twitch ボタンの見た目

`_QSS_TEMPLATE` へ規則を 1 つ足す。`#previewCanvas` (常に黒) と同じ扱いで、
**意図的にテーマの対象外**とする。ブランド色は明暗で変えない。

```css
/* Twitch のログインボタン (request2 C)。ブランド色のため明暗で変えない。 */
#twitchButton { background-color: #9147FF; color: #FFFFFF; border-color: #9147FF; }
#twitchButton:hover { background-color: #A970FF; border-color: #A970FF; }
#twitchButton:pressed { background-color: #772CE8; border-color: #772CE8; }
```

### 4.6 `services/billing.py` の変更

```python
# 購入ボタンを押せない理由のコード (押せるなら "")
BLOCK_NOT_LOGGED_IN = "not_logged_in"   # ログインしていない
BLOCK_UNKNOWN       = "unknown"         # 加入状態が分からない (オフライン等)
BLOCK_UNAVAILABLE   = "unavailable"     # 受付停止 (API 未実装・Stripe 未設定)
BLOCK_TWITCH        = "twitch"          # Twitch サブで特典適用中 (request2 ①)
BLOCK_STRIPE        = "stripe"          # すでに Stripe で加入中


# 購入できるかを決める純関数。PySide6 に依存しないため単体テストできる。
# 文言は GUI 側が持つ (ここはコードだけを返す)。
def purchase_blocked_reason(config, subscription_type, logged_in): ...


# GET /api/billing/subscription。状態と履歴を返す (③)。
# config() と同じく**例外を投げない**。取れなければ None を返し、
# アカウントタブは種別だけを出す。
def subscription(self): ...
```

`purchase_blocked_reason` を純関数として切り出すのは、
**要望 ① の分岐が画面の中に埋まらないようにするため**である。
真理値表 (§6) をそのまま単体テストにできる。

---

## 5. `setting.json`

### 5.1 まず `DEFAULT_SETTINGS` の欠落を直す (既存の欠陥)

`settings_window.DEFAULT_SETTINGS` へ `billing` を追加する。
値は ver6 resolve §5 と `billing.py` のモジュール既定と同じにする。

```python
    "billing": {
        # GET /api/billing/config の再取得間隔 (秒)
        "config_cache_sec": 3600,
        # 支払いの反映待ちのポーリング間隔 (秒)
        "activation_poll_sec": 3,
        # 支払いの反映待ちの上限 (秒)
        "activation_timeout_sec": 180,
    },
```

* 入れ子を持たない平らなキーだけにする。入れ子にすると
  `_fill_billing_nested_defaults()` を足す必要が出て、
  欠落キー補完の仕組みが 1 つ増える (`_fill_ui_nested_defaults` などと同じ轍)。
* 作業ツリーの `setting.json` から消えた `billing` は、これを入れれば
  次回起動の `_has_new_keys` → `save_settings` で自動的に戻る。
* **価格は置かない。** サーバーから配る (ver6 resolve §3.1)。
* **チェックマークの色も置かない。** Twitch はブランド色、Stripe はテーマの
  `success` トークンで、どちらも利用者が調整する値ではない (§4.5 / §10 論点 2)。

### 5.2 本件で足すキー

**無し。** §3.3 の代替案を採る場合にだけ `twitch_channel` /
`twitch_channel_id` を足す。

---

## 6. 状態と表示の対応表

`purchase_blocked_reason()` の真理値表。**これがそのまま §8.1 のテストになる。**

| ログイン | `config.is_available()` | `subscriptionType` | 理由コード | Twitch ボタン | 購入ボタン |
| --- | --- | --- | --- | --- | --- |
| × | — | — | `not_logged_in` | 有効「Twitch でログイン」 | 無効 |
| ○ | 取得前 | 取得前 | `unknown` | 無効 (ログイン済み表示) | 無効「確認中…」 |
| ○ | ○ | 取れない (オフライン) | `unknown` | 無効 | 無効 |
| ○ | × (未実装 / 未設定) | `None` | `unavailable` | 無効 | 無効 |
| ○ | ○ | `None` | `""` | 無効 | **有効** |
| ○ | ○ | `TwitchSub` | `twitch` | 無効 | **無効 (①)** |
| ○ | ○ | `Stripe` | `stripe` | 無効 | 無効 |

メイン画面のボタン:

| `subscriptionType` | チェックマーク | 色 | ボタン |
| --- | --- | --- | --- |
| 取れない / `None` | 無し | — | 有効 |
| `TwitchSub` | 有り | `#9147FF` (固定) | **無効** |
| `Stripe` | 有り | `success` (テーマ追随) | **無効** |

### 6.1 ver6 resolve §4.7 からの方針変更 (意図的)

ver6 resolve §4.7 は「Twitch サブの利用者にも**登録を出す**
(二重課金の注意書きを添える)」としていた。Twitch サブは切れるため、
切れたあとも続けたい人に Stripe を選ばせる狙いだった。

**request2 ① はこれを覆す。** Twitch サブ中は購入させない。
したがって次の点を文面で補う (§10 論点 4 に関わる)。

* 購入ボタンの注記:
  「Twitch のサブスクリプションで特典が適用されています。購入は不要です。
  Twitch のサブスクリプションが切れると、この画面から購入できるようになります。」
* 二重課金の注意書き (旧 §4.7 の文面) は不要になるため削除する。

---

## 7. エラーと縮退

| 状況 | クライアントの扱い |
| --- | --- |
| `GET /api/billing/config` が 404 / オフライン | サブスクリプション画面は開く。購入は無効、理由を注記 (§2.2) |
| `GET /api/points` が取れない | 加入状態は「不明」。購入は無効。メイン画面のボタンは印なし・押せる |
| `GET /api/billing/subscription` が 404 / オフライン | アカウントタブは種別だけ出し、履歴の表は出さない (§4.4) |
| Twitch ログインの認可待ちがタイムアウト | 既存 `LoginWorker.failed` の経路。「ログインできません」 |
| 別のログインが進行中 (ポート衝突) | `login_in_progress()` を見てボタンを押させない (§4.1) |
| Checkout が 409 `already_subscribed` | 「すでに加入しています」→ `reload()` して状態を作り直す |
| ポータルが 404 `no_subscription` | 管理ボタンを隠し、状態を作り直す |
| 401 / 期限切れ | 既存の `ReauthRequiredError` 経路。ログインを促す |
| 反映待ちがタイムアウト | 「確認できませんでした」。失敗とは書かない (支払いの取りやめもここへ来る) |

---

## 8. 検証

### 8.1 単体テスト

`tests/test_billing.py` へ追加 (PySide6 不要):

| # | 内容 | 期待 |
| --- | --- | --- |
| 12 | 未ログイン | `not_logged_in` |
| 13 | `config` 未取得 / 加入種別未取得 | `unknown` |
| 14 | `enabled = false` | `unavailable` |
| 15 | `priceLabel` が空 | `unavailable` |
| 16 | `subscriptionType = "None"` | `""` (押せる) |
| 17 | `subscriptionType = "TwitchSub"` | `twitch` (**要望 ①**) |
| 18 | `subscriptionType = "Stripe"` | `stripe` |
| 19 | `subscription()` が 404 | `None`、例外なし |
| 20 | `subscription()` がオフライン | `None`、例外なし |
| 21 | `subscription()` が正常 | `history` を含む dict |

`tests/test_subscription_ui.py` (新規。`test_points_ui.py` と同じ offscreen 方式):

| # | 内容 | 期待 |
| --- | --- | --- |
| 1 | 残高が `TwitchSub` | メイン画面のボタンが無効・アイコン有り |
| 2 | そのアイコンの色 | `#9147FF` (ライト・ダークの両方で同じ) |
| 3 | 残高が `Stripe` | アイコンの色がテーマの `success` (明暗で変わる) |
| 4 | 残高が `None` | 有効・アイコン無し |
| 5 | 残高が取れない (`None`) | 有効・アイコン無し |
| 6 | 実行中 (`_running_tabs` が空でない) | 無効 |
| 7 | 無効化してもアイコンが褪せない | `QIcon.Disabled` の絵が `Normal` と同一 (§4.5) |
| 8 | サブスクリプション画面: 未ログイン | 購入が無効・Twitch ボタンが有効 |
| 9 | サブスクリプション画面: `TwitchSub` | 購入が無効・注記に理由が出る |
| 10 | `PointsIndicator.balance_changed` | 残高の取得 1 回につき 1 回だけ発火 |

`tests/test_settings_migration.py` へ追加:

| # | 内容 | 期待 |
| --- | --- | --- |
| 1 | `DEFAULT_SETTINGS` に `billing` がある | §5.1 の 3 キー |
| 2 | `billing` を持つ設定を `_merge_with_defaults` に通す | 値が消えない (**§1.4 の回帰**) |
| 3 | 利用者が変えた `activation_timeout_sec` | 温存される |

### 8.2 手動確認

1. メイン画面を既定の 700px で開き、**タブバーにスクロールボタンが出ない**こと (§2.1)。
2. 未ログインで「サブスクリプション」を押す → 画面が開き、購入は無効で理由が出る。
3. **Twitch サブ (`tatsumic`) のアカウント**で Twitch ログイン →
   購入が押せない。閉じてメイン画面へ戻ると、ボタンが無効で `#9147FF` の
   チェックマークが付く (**要望 ①**)。
4. **未加入のアカウント**で購入 → Stripe のテストモードで支払い →
   反映後にメイン画面へ戻ると、ボタンが無効で `success` 色のチェックマークが付く
   (**要望 ②**)。
5. その状態で出力を 1 本行い、ポイントが減らず透かしも入らないこと。
6. 設定 → アカウントに状態と履歴が出る。解約は「管理」から行える (**要望 ③**)。
7. OS の明暗を切り替える → Twitch のチェックマークは `#9147FF` のまま、
   Stripe のチェックマークはテーマに追随する。
8. API を落として (オフラインで) 一連を開き、どの画面も開いたままであること。
9. 出力を実行中に、サブスクリプションボタンと設定ボタンが無効になること。

---

## 9. 本書の範囲外 (別途必要な作業)

### 9.1 `StretheusAPI` 側

#### `GET /api/billing/subscription` (新設。要認証)

要望 ③ の「状態と履歴」に使う。`GET /api/points` は残高のための API で、
履歴や期間を載せる場所ではないため分ける。

```json
{
  "type": "Stripe",
  "status": "active",
  "cancelAtPeriodEnd": false,
  "currentPeriodEnd": "2026-10-28T12:00:00Z",
  "twitchChannel": "tatsumic",
  "history": [
    { "at": "2026-09-28T12:03:00Z", "type": "Stripe", "event": "subscribed",
      "detail": "月額 980 円 (税込)" },
    { "at": "2026-08-28T12:03:00Z", "type": "Stripe", "event": "renewed", "detail": "" }
  ]
}
```

| フィールド | 意味 |
| --- | --- |
| `type` | `"None"` / `"Stripe"` / `"TwitchSub"`。`GET /api/points` と同じ値 |
| `status` | `"active"` / `"canceling"` / `"past_due"` / `"none"` |
| `currentPeriodEnd` | 次回更新日 (解約予定なら終了日) |
| `twitchChannel` | 表示用のチャンネル名。クライアントへチャンネル名を焼き込まないため |
| `history` | 新しい順。件数は API 側で上限を決める (画面はページ送りを設けない) |

* 未加入は 200 + `type: "None"` + 空の `history` を返す (404 にしない。
  クライアントが「未加入」と「API が無い」を区別できるようにするため)。
* Twitch サブの履歴は Twitch 側に無いため、API が判定した記録を残す運用が要る。
  残せない場合は `history` を空にしてよい (クライアントは表を出さない)。

#### `GET /api/billing/config` への追加

`twitchChannel` を追加する (サブスクリプション画面の文言に使う)。
無くても動く (チャンネル名を添えない文面になる)。

#### 要望 ① の前提 (§3.3 / §10 論点 1)

* Twitch の認可スコープに `user:read:subscriptions` を含める。
* 判定対象チャンネル (`tatsumic` / `669964096`) を設定として持つ。
* `TwitchLicenseProvider` が `GET /helix/subscriptions/user` で判定し、
  `subscriptionType = "TwitchSub"` を返す。

### 9.2 資料

`docs/layout/main_window.html` と `settings_window.html` は実装の写しなので、
コーナーの 3 つめのボタンとアカウントタブの 2 節を反映する。
サブスクリプション画面は「未収録の画面」(`docs/layout/README.md`) に倣って
今回は起こさないが、同 README の一覧へ 1 行足す。

---

## 10. 要確認 / 残る論点

推測で実装しない (claude.md の制約)。回答が欲しい順に並べる。

| # | 論点 | 本書の扱い | 回答が要る理由 |
| --- | --- | --- | --- |
| 1 | API 側は `tatsumic` へのサブスクを判定して `subscriptionType = "TwitchSub"` を返せるか (§3.3 の (a)(b)(c)) | サーバー判定を前提に実装する | **要望 ① が動くかどうかが決まる。** 未対応なら §3.3 の代替を採るか、API の対応を待つ |
| 2 | Stripe 側のチェックマークの色 | テーマの `success` を採る | 要望は Twitch の色だけを指定している。Stripe のブランド色 `#635BFF` は `#9147FF` と近く、並べたときに区別しにくいため採らなかった。ブランド色にしたい場合は指定が要る |
| 3 | 加入するとメイン画面のボタンが無効になり、この画面から解約へ行けなくなる | 要望どおり無効化し、解約は設定 → アカウントの「管理」に残す (§2.3) | 「有効のままチェックマークを出し、押すと管理画面を開く」方が親切。今回は要望の文面を優先した |
| 4 | Twitch サブが切れた月から透かしが入る旨の告知 | 画面の注記に入れる (§6.1) | 画面外での告知 (メール等) が要るかは本書の範囲外 |
| 5 | 要望 ③ の「履歴」 | **サブスクの履歴**と解釈した (§2.3) | ポイントの履歴であれば既に実装済みで、追加は見出しの改名だけになる |
| 6 | Twitch ログイン済みのときのボタン | 「Twitch ログイン済み: {名前}」で無効にする | 別アカウントへ切り替えたい場合は設定 → アカウントでログアウトしてから。切り替えをこの画面でもできるようにするかは要望次第 |
