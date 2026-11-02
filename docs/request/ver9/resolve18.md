# ver9 resolve18 — サブスクリプション画面を 3 段フローにし Google ログインを追加する

発端: 2026-11-02 のチャット要望
　　　「Twitch アカウントを持っていないユーザーも多い。その人たちにもサブスクしてもらいたい」
　　　＋画面の指定
　　　　① サブスクリプションボタンを押下すると、**Twitch と Google のログインボタン**が表示される
　　　　② **Twitch** からログインした場合、たつみっくのサブスクを行っているか確認する。
　　　　　 行っていれば、**サブスクリプション画面を消しメイン画面に戻る**。
　　　　　 サブスクリプションボタンが押せなくなり紫色のチェックマークが出る (既存表示)。
　　　　　 行っていなければ**購入ボタン**が表示され、押下で Stripe の決済画面 (既存処理)
　　　　　 ③ **Google** からログインした場合、購入ボタンが表示される。そこからは既存処理
対象: `src/gui/subscription_window.py` / `src/gui/main_window.py` /
　　　`src/services/stretheus_auth.py` / `src/services/billing.py` (定数のみ) /
　　　`src/i18n/catalog_en.py`
API 側の設計: `StretheusAPI/docs/request/resolve6.md` (**先に必要**)
関連: `ver6/resolve2.md` §4.1 · §4.6 · §6 (真理値表) / `ver9/resolve17.md` (反映の経路)
調査日: 2026-11-02
種別: **機能追加の設計 → 実装完了**
状態: **2026-11-02 実装完了 (実機確認待ち)**。単体テスト 2,237 件 OK。実装記録は §9

---

## 0. 先に結論

**`purchase_blocked_reason()` は変更しない。** 真理値表 (`ver6/resolve2.md §6`) と
その単体テスト (`tests/test_billing.py` の 7 件) をそのまま残せる。

変えるのは**画面側の解釈だけ**である。いまは返ってきたコードを
「購入ボタンの有効 / 無効 + 注記」に落としているが、これを
**「画面の段 (stage) の選択」**に変える。要望の ①②③ はこの段に素直に対応する。

| `purchase_blocked_reason()` の戻り値 | 新しい段 | 画面 |
| --- | --- | --- |
| `BLOCK_NOT_LOGGED_IN` | **S0 未ログイン** | **Twitch / Google の 2 ボタン** (要望 ①) |
| `BLOCK_UNKNOWN` | S1 確認中 | 「加入状態を確認しています…」 |
| `BLOCK_TWITCH` | **S2 Twitch サブ** | **画面を閉じてメイン画面へ戻る** (要望 ②) |
| `BLOCK_STRIPE` | S3 Stripe 加入中 | 加入中の表示。購入ボタンは出さない |
| `BLOCK_UNAVAILABLE` | S4 受付停止 | 受付停止の注記 |
| `""` (空) | **S5 購入可能** | **購入ボタン** (要望 ②後半 / ③) |

**要望 ② と ③ は同じ分岐で実現される。** Google 利用者は `TwitchSub` に
なる経路が無い (`resolve6.md §5`) ため、ログイン後は必ず S3 / S4 / S5 のいずれかで、
**③ の「購入ボタンが表示される」は S5 そのもの**である。
Google 専用の分岐は 1 つも要らない。

---

## 1. 現状の画面

```
[サブスクリプション]            ← タイトル
月額 980 円 (税込)              ← price_label (サーバーから配布)
ポイントを消費せず…            ← description_label
状態: 未加入                    ← state_label
[Twitch でログイン]             ← twitch_button  (常に見えている)
[購入]                          ← purchase_button (常に見えている / 無効)
[更新]                          ← refresh_button (resolve17 §4.4)
Twitch でログインすると購入できます。  ← note_label
```

**ボタンが 2 つ並び、未ログインでは片方が必ず無効**という形である。
要望 ① の「ログインボタンが表示される」は、**段ごとに出すものを変える**
という指示と読む。

---

## 2. 新しい画面 — 段ごとに出すものを変える

### S0 未ログイン (要望 ①)

```
[サブスクリプション]
月額 980 円 (税込)
ポイントを消費せず、透かしの入らない出力を回数無制限で行えます。

ログインしてからお申し込みいただけます。

[Twitch でログイン]     ← 紫 (#9147FF / theme.TWITCH_BUTTON)
[Google でログイン]     ← 新規。既定のボタン色
                          ※ API が未設定なら出さない (§3)

Twitch のサブスクリプションをご利用中の方は、Twitch でログインすると
特典がそのまま適用されます。
```

* **購入ボタンと「更新」ボタンは出さない。** 未ログインでは押しても意味が無く、
  無効のボタンを見せるより消したほうが「次にやること」が 1 つに決まる。
* **Twitch を先に置く。** Twitch サブの特典を持つ人が Google を選んでしまうと
  特典を受けられない (別の `User` 行になる / `resolve6.md §9-5`)。
  注記でも誘導する。

### S1 確認中

```
状態: 確認中…
[更新]
加入状態を確認しています…
```

ログイン直後、`GET /api/points` と `GET /api/billing/config` の
**両方が揃うまで**この段にいる (`ver6/resolve2.md §4.2` の方針を維持)。
ログインボタンも購入ボタンも出さない。

### S2 Twitch サブで特典適用中 (要望 ②)

**画面を閉じてメイン画面へ戻る。** メイン画面側は既存の表示がそのまま働く
(`_refresh_subscription_button` → 紫のチェックマーク + ボタン無効)。

```python
# 判定が付いた時点で閉じる。表示を作らずに閉じるため、
# この段では widget を一切触らない。
```

### S3 Stripe で加入中

```
状態: 加入中 (Stripe)
[更新]
すでにサブスクリプションに加入しています。
解約・カードの変更は設定画面の「アカウント」から行えます。
```

### S4 受付停止 (Stripe 未設定 / `config` が取れない)

```
状態: 未加入
[更新]
現在サブスクリプションのお申し込みを受け付けていません。しばらくしてからお試しください。
```

### S5 購入可能 (要望 ②後半 / ③)

```
状態: 未加入
[購入]
[更新]
お支払いはブラウザ (Stripe) で行います。
```

押下後は**既存処理のまま** —
`BillingUrlWorker("checkout")` → ブラウザで Stripe → `checkout_started` を emit し
メイン画面が反映を待つ (`resolve17 §4.2`)。

---

## 3. Google ボタンを出す条件

**API が Google を提供できないときは出さない。**

`GET /api/auth/google/authorize-params` が **503 + `google_unavailable`** を返す
構成では (`resolve6.md §4` / §6)、押しても必ず失敗する。
`GET /api/billing/config` の `enabled` で購入導線を出し分けているのと同じ考え方で、
**押せるのに失敗するボタンを作らない** (`plan.md §7.8.5` の教訓)。

| 取得結果 | Google ボタン |
| --- | --- |
| 200 | **出す** |
| 503 / 通信不能 | **出さない** (Twitch のみ) |
| 取得前 | 出さない (取れた時点で現れる) |

取得は `S0` に入ったときに 1 回だけ行い、結果をキャッシュする
(`BillingService.config()` と同じ扱い。既定 3600 秒)。
**例外は投げない。** 取れなければ `False` として扱う。

---

## 4. 実装設計

### 4.1 `stretheus_auth.py` — プロバイダを引数にする

```python
# プロバイダごとのパス (resolve18 §4.1)
_AUTHORIZE_PARAMS_PATHS = {
    "twitch": "/api/auth/twitch/authorize-params",
    "google": "/api/auth/google/authorize-params",
}
_LOGIN_PATHS = {
    "twitch": "/api/auth/twitch/login",
    "google": "/api/auth/google/login",
}

PROVIDER_TWITCH = "twitch"
PROVIDER_GOOGLE = "google"
```

```python
def login(self, timeout=180, open_browser=True, provider=PROVIDER_TWITCH): ...
```

**`_build_authorize_url` は変更しない。** 現状すでに
`clientId` / `redirectUri` / `scopes` / `authorizeUrl` を**API から受け取って
組み立てている**ため、プロバイダ非依存である (`ver6/resolve2.md §2.4` の設計が効いている)。

`_receive_code` も変更しない。`redirectUri` のポートを見て待ち受ける作りのまま。

**`is_logged_in()` も変更しない。**

```python
def is_logged_in(self):
    return bool(self.has_session() and not self._data.get("anonymous"))
```

Google のセッションは `anonymous` が False なので、**そのまま「ログイン済み」になる**。
画面とサブスク判定がここを見ているため、ここを触らずに済むのは重要である。

保存データ (`auth.dat`) に **`provider` を 1 つ足す**。
どちらでログインしたかを画面に出す (`Twitch ログイン済み: 名前` /
`Google ログイン済み: 名前`) ためで、判定には使わない。
**無い場合は `twitch` と見なす** (既存の `auth.dat` を壊さない)。

### 4.2 Google ログインの可否を問う窓口

`billing.py` に置く (`BillingService` が `config()` で同じことをしている)。
**新しいサービスクラスは作らない。**

```python
_GOOGLE_PARAMS_PATH = "/api/auth/google/authorize-params"

# Google ログインを提供できる構成か。例外は投げない (ver9 resolve18 §3)。
# 503 (未設定) もオフラインも False として返す。
def google_available(self):
    ...
```

> `BillingService` に認証の問い合わせを置くのは本来は筋が違う。
> ただし**この値を使うのはサブスクリプション画面だけ**で、
> 同画面は既に `BillingService` を持っている。クラスを 1 つ増やすほうが
> 画面の依存を増やす。`login_check.py` を切り出すときに一緒に移す。

### 4.3 `subscription_window.py` — 段で描き分ける

`_apply_state()` を**段を決めてから描く**形に変える。

```python
# 画面の段 (ver9 resolve18 §0)
_S_LOGIN = "login"          # S0 未ログイン
_S_CHECKING = "checking"    # S1 確認中
_S_TWITCH_SUB = "twitch"    # S2 Twitch サブ → 閉じる
_S_SUBSCRIBED = "stripe"    # S3 Stripe 加入中
_S_UNAVAILABLE = "closed"   # S4 受付停止
_S_PURCHASE = "purchase"    # S5 購入可能

# purchase_blocked_reason() のコードから段を決める。
# **真理値表そのものは変えない** (ver6 resolve2 §6 / §4.6)。
_STAGES = {
    billing_module.BLOCK_NOT_LOGGED_IN: _S_LOGIN,
    billing_module.BLOCK_UNKNOWN: _S_CHECKING,
    billing_module.BLOCK_TWITCH: _S_TWITCH_SUB,
    billing_module.BLOCK_STRIPE: _S_SUBSCRIBED,
    billing_module.BLOCK_UNAVAILABLE: _S_UNAVAILABLE,
    "": _S_PURCHASE,
}
```

```python
def _apply_state(self):
    reason = billing_module.purchase_blocked_reason(
        self._billing_config, self._subscription_type, self._is_logged_in())
    stage = _STAGES.get(reason, _S_CHECKING)

    # 手続き中 (ブラウザでの支払い待ち) は段を動かさない
    if self._in_progress:
        stage = _S_PURCHASE

    if stage == _S_TWITCH_SUB:
        self._close_for_twitch_subscription()
        return

    self._show_stage(stage)
```

`_show_stage` が widget の `setVisible` をまとめて行う。

| widget | S0 | S1 | S2 | S3 | S4 | S5 |
| --- | --- | --- | --- | --- | --- | --- |
| `price_label` / `description_label` | ○ | ○ | — | ○ | ○ | ○ |
| `state_label` | — | ○ | — | ○ | ○ | ○ |
| `twitch_button` | **○** | — | — | — | — | — |
| `google_button` | **○** (§3) | — | — | — | — | — |
| `purchase_button` | — | — | — | — | — | **○** |
| `refresh_button` | — | ○ | — | ○ | ○ | ○ |
| `note_label` | ○ | ○ | — | ○ | ○ | ○ |

### 4.4 S2 で閉じる (要望 ②)

```python
# Twitch サブで特典が適用されている場合は、この画面に用が無い。
# メイン画面のボタンが無効になり紫のチェックマークが付く (既存表示)。
def _close_for_twitch_subscription(self):
    if self._closing_for_subscription:
        return                      # 二重に閉じない
    self._closing_for_subscription = True
    # メイン画面へ先に知らせる。閉じたあとでは closed しか飛ばない。
    self.subscription_changed.emit()
    self.close()
```

**`subscription_changed` を先に emit する。** メイン画面はこれを
`points_indicator.refresh(force=True)` につないでおり (`resolve17 §4.2`)、
チェックマークはその結果で付く。`close()` が先だと `closed` →
`points_indicator.reload()` も走り、**同じ取得が 2 回走る**。
`resolve17 §4.3` の保留処理が 1 回に畳むので事故にはならないが、
順序を決めておく。

**再入防止のフラグが要る。** `_apply_state()` は残高と契約情報が別々に届くたび
呼ばれるため、**S2 の判定は 2 回以上起こりうる**。
`close()` → `closeEvent` → `closed` emit の途中で `_apply_state()` が
走る経路もあるため、フラグ無しでは `close()` が二重に走る。

> **判断が要る点: 無言で閉じるか、1 行伝えるか。**
> 要望は「画面を消しメイン画面に戻る」なので**無言で閉じる**設計にした。
> ただし利用者から見ると「ログインしたら画面が消えた」だけで、
> **特典が適用されたという事実はチェックマークしか伝えていない**。
> メイン画面のボタンの tooltip は既に「サブスクリプションに加入済みです」に
> なるが、tooltip は能動的に見ないと読めない。
> 1 行の通知を出すかは §6 で選択とする。

### 4.5 `main_window.py`

**変更なしで動く。** 既存の結線がそのまま働く。

| 既存の結線 | 本件での働き |
| --- | --- |
| `subscription_changed` → `points_indicator.refresh(force=True)` | S2 で閉じる直前の emit を受けてチェックマークを付ける |
| `closed` → `points_indicator.reload()` | 閉じたあとの取り直し (`resolve17 §1.2`) |
| `checkout_started` → `_start_activation_wait` | S5 からの購入の反映待ち (`resolve17 §4.2`) |

### 4.6 文言 (`catalog_en.py` へ追加)

| 日本語 | 用途 |
| --- | --- |
| `Google でログイン` | S0 のボタン |
| `Google ログイン済み: {name}` | ログイン済み表示 |
| `ログインしてからお申し込みいただけます。` | S0 の注記 |
| `Twitch のサブスクリプションをご利用中の方は、Twitch でログインすると特典がそのまま適用されます。` | S0 の誘導 (§2) |

`tests/test_i18n.py` が `tr("…")` の直書きを対訳表と突き合わせるため、
**追加を忘れるとテストが落ちて気付ける。**

---

## 5. 変更ファイル

| ファイル | 変更 |
| --- | --- |
| `src/services/stretheus_auth.py` | パスをプロバイダ別の辞書へ。`login(provider=...)`。`auth.dat` に `provider` を保存 (無ければ `twitch` と見なす) |
| `src/services/billing.py` | `google_available()` を追加 (§4.2)。**`purchase_blocked_reason` は変更しない** |
| `src/gui/billing_workers.py` | `GoogleAvailableWorker` を追加 (HTTP を GUI スレッドで行わない既存方針) |
| `src/gui/points_indicator.py` | `LoginWorker` に `provider` を渡せるようにする |
| `src/gui/subscription_window.py` | 段による描き分け (§4.3)。`google_button`。S2 で閉じる (§4.4)。`retranslate` |
| `src/gui/main_window.py` | **変更なし** (§4.5) |
| `src/i18n/catalog_en.py` | §4.6 の 4 件 |
| `src/settings/settings_window.py` (アカウントタブ) | ログイン済み表示にプロバイダを添える。**ログアウトの文言は変更不要** |

## 6. 判断が要る点

| # | 論点 | 選択肢 | 推奨 |
| --- | --- | --- | --- |
| 1 | S2 で閉じるときに通知するか | (a) 無言で閉じる / (b) 1 行の通知を出してから閉じる | **(b)** — 「ログインしたら画面が消えた」に見えるのを避ける。`QMessageBox` ではなくメイン画面側の短い表示が望ましい |
| 2 | S0 に Google を出す順序 | (a) Twitch → Google / (b) Google → Twitch | **(a)** — Twitch サブの特典を取り逃がさせない (§2) |
| 3 | アカウントタブからも Google ログインできるようにするか | (a) する / (b) サブスクリプション画面のみ | **(a)** — 「ログイン」はアカウントタブの役目で、片方だけ Google を知らないのは不整合 |

## 7. テスト計画

### 7.1 単体テスト

**`purchase_blocked_reason` の既存 7 件はそのまま通る** (関数を変えないため)。
追加するのは段のマッピングと Google ボタンの出し分けである。

| # | 内容 | 新規 / 既存 |
| --- | --- | --- |
| 1 | 6 つのコードが 6 つの段へ一意に対応する (`_STAGES` の網羅) | 新規 |
| 2 | S0 で `twitch_button` が見え、`purchase_button` が**見えない** | 新規 |
| 3 | `google_available=False` なら S0 で `google_button` が**見えない** | 新規 |
| 4 | `google_available=True` なら見える | 新規 |
| 5 | S5 で `purchase_button` が見えて押せる | 新規 |
| 6 | S3 で `purchase_button` が見えない | 新規 |
| 7 | **S2 で `subscription_changed` が emit され、画面が閉じる** | 新規 |
| 8 | **S2 の判定が 2 回起きても `close()` は 1 回** (§4.4 の再入) | 新規 |
| 9 | `test_logged_out_shows_the_twitch_login` が S0 の形に合うよう修正 | **既存を修正** |
| 10 | `tests/test_i18n.py` の対訳表網羅 | 既存 (自動) |

### 7.2 実機確認

| # | 手順 | 期待 |
| --- | --- | --- |
| 1 | 未ログインで画面を開く | Twitch / Google の 2 ボタン。購入ボタンは無い |
| 2 | **Twitch (たつみっくのサブ有り) でログイン** | **画面が閉じ、メイン画面のボタンが無効 + 紫のチェックマーク** (要望 ②) |
| 3 | Twitch (サブ無し) でログイン | 購入ボタンが出る (要望 ②後半) |
| 4 | **Google でログイン** | **購入ボタンが出る** (要望 ③) |
| 5 | 4 からそのまま購入 | 既存処理で決済画面が開く。反映でボタンが無効化される (`resolve17`) |
| 6 | **Google で 2 時間以上使い続ける** | **ログアウトされない** (`resolve6.md §1` の失効バグの確認) |
| 7 | Google の API 未設定で画面を開く | Google ボタンが出ない。Twitch だけで従来どおり動く |

> **6 を飛ばさない。** API 側 `resolve6.md §1` の失効バグは
> **ログイン直後には再現せず、JWT の期限 (120 分) を越えて初めて出る。**

## 8. 残課題

1. **API を先に出す必要がある。** `resolve6.md` の実装とデプロイが済むまで
   Google ボタンは 503 で出ないため、クライアントを先に出しても害は無い (§3)。
   **順序はどちらでもよいが、確認は API 側が先。**
2. **アカウント統合はしない。** 同じ人が Twitch と Google でログインすると
   別の利用者になり、**ポイント台帳も Stripe の加入も別になる**
   (`resolve6.md §9-5`)。S0 の注記で Twitch 利用者を Twitch へ誘導するのは
   この取り違えを減らすためだが、**防げはしない**。
   「すでに Twitch で加入済みの人が Google でログインして二重に買う」経路が
   残るので、気になるなら購入前に確認を挟む設計が別途必要になる。
3. **Twitch VOD 自動取得は Twitch 専用のまま。** Google でログインした
   利用者はローカル mp4 モードを使う (`resolve6.md §9-4`)。

---

## 9. 実装記録 (2026-11-02)

### 9.1 設計からの変更点 1 件 — ログイン済みアカウントの表示

**§4.3 の表のとおりログインボタンを S0 だけに出したところ、
「どのアカウントでログインしているか」が画面から消えた。**

従来は無効化した Twitch ボタンに `Twitch ログイン済み: <名前>` を出していたが、
S1〜S5 ではボタン自体を隠すため、この情報の置き場が無くなる。
既存テスト `test_it_shows_who_is_logged_in` がこれを検出した。

**状態ラベルの 2 行目へ移した。**

```
状態: 未加入
Twitch ログイン済み: たつみっく
```

`_account_text()` を切り出し、`stretheus_auth.provider()` で Twitch / Google を
見分ける。**Twitch と Google では受けられる特典が違う**
(Twitch サブの特典は Twitch でログインした場合のみ) ため、
見分けが付かないと「なぜ特典が出ないのか」が利用者に分からない。

同じ理由でアカウントタブの表示も `ログイン中: <名前> (Twitch)` /
`(Google)` へ変えた (§5 の表に含めていた項目)。

### 9.2 変更したファイル

| ファイル | 変更 |
| --- | --- |
| `src/services/stretheus_auth.py` | `PROVIDER_TWITCH` / `PROVIDER_GOOGLE`。パスを辞書化。`login(provider=...)`。`_apply(provider=...)` が `auth.dat` に記録。`provider()` を追加 (記録が無い既存ファイルは `twitch` と見なす) |
| `src/services/billing.py` | `google_available()` と専用キャッシュ。**`purchase_blocked_reason` は未変更** |
| `src/gui/billing_workers.py` | `GoogleAvailableWorker` を追加 |
| `src/gui/points_indicator.py` | `LoginWorker(provider=...)` (既定 `twitch` なので既存の呼び出しは不変) |
| `src/gui/subscription_window.py` | `_STAGES` / `_STAGE_MESSAGES` / `_LOGIN_HINT`。`_show_stage`。`google_button`。`_start_login` に統合。`_close_for_twitch_subscription` (再入防止付き)。`_account_text` (§9.1) |
| `src/gui/account_tab.py` | ログイン中の表示にプロバイダを添える (§9.1) |
| `src/gui/main_window.py` | **変更なし** (§4.5 のとおり既存の結線で動いた) |
| `src/i18n/catalog_en.py` | 9 件追加 |
| `tests/test_subscription_stages.py` | **新規 13 件** |
| `tests/test_subscription_ui.py` | 既存 2 件を新しい挙動へ更新。`_FakeAuth.provider()` を追加 |
| `tests/test_subscription_reflect.py` · `tests/test_points_ui.py` | `_FakeAuth.provider()` を追加 |

### 9.3 テスト結果

```
py -m unittest discover -s tests
Ran 2237 tests in 18.097s
OK
```

実装前の基準は 2,224 件 (`resolve17 §10.3`)。**増えた 13 件が新規テスト。**

既存テストの更新は 2 件で、**どちらも意図した挙動変更による**。

| テスト | 変更理由 |
| --- | --- |
| `test_a_twitch_subscriber_cannot_purchase` | 注記を出す代わりに**画面を閉じる**ようになった (要望 ②)。閉じる挙動は新規テストで確認 |
| `test_it_shows_who_is_logged_in` | アカウント表示が Twitch ボタンから状態ラベルへ移った (§9.1) |

**`purchase_blocked_reason` の真理値表テスト (`tests/test_billing.py` 7 件) は
1 行も変えていない。** 設計の狙いどおり、変えたのは画面側の解釈だけである。

### 9.4 新規テストで押さえた点

| # | 内容 |
| --- | --- |
| 1 | 6 つのコードが**6 つの異なる段**へ対応すること (漏れと重複の両方を検出) |
| 2 | S2 以外の段には必ず注記があること |
| 3 | 未ログインで 2 ボタンが出て、購入ボタンと「更新」が出ないこと |
| 4 | `google_available=False` で Google ボタンが出ないこと (§3) |
| 5 | **S2 で `subscription_changed` を出してから閉じること** (要望 ②) |
| 6 | **S2 の判定が 3 回起きても emit と close は 1 回** (§4.4 の再入) |
| 7 | **Google でログインすると購入ボタンが出ること** (要望 ③) |
| 8 | Google の利用者に Twitch ボタンが出ないこと |
| 9 | Google の利用者のアカウント表示 (§9.1) |

### 9.5 残り: 実機確認

§7.2 の 7 項目。**API 側 (`StretheusAPI/docs/request/resolve6.md`) の
デプロイと Google Cloud の設定が先**になる。

それまでは `authorize-params` が 503 / 404 を返すため
**Google ボタンが出ず、Twitch だけで従来どおり動く** (§3)。
クライアントを先に出しても害は無い。

**項目 6 (Google で 2 時間以上使い続ける) を飛ばさないこと。**
API 側の失効バグは JWT の期限を越えて初めて現れる。
