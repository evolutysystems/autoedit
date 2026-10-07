# ver7 resolve — 未ログイン利用者をポイント制の対象にする (autoedit クライアント側)

対象: `autoedit` (本書の実装範囲)
API 側の設計: `StretheusAPI/docs/request/resolve4.md`
関連: `autoedit/docs/request/ver5/resolve.md` (ポイント制の基本設計)、`StretheusPlan/plan.md` P7

作成日: 2026-10-07

---

## 0. 要望と結論

> 今の状態だと、未ログインユーザーはポイント関係なくアプリを使えてしまって使いたい放題に
> なっているので、未ログインユーザーは未サブスクリプションユーザーと同じくポイント制にする。

ver5 resolve §4-5 では「未ログインのユーザーは従来どおり無償で使える (ポイントを通らず
watermark も入らない)」と決めていた。本書でこの決定を**取り消す**。

| | 変更前 | 変更後 |
| --- | --- | --- |
| 未ログイン | 消費なし / watermark なし | **月 150pt・クリップ 25pt・アーカイブ 100pt・残高 0 で watermark** |
| ログイン (未サブスク) | 同上の条件で消費 | 変更なし |
| サブスク会員 | 消費なし / watermark なし | 変更なし |

### 0.1 実装状況 (2026-10-07)

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | `src/services/device_id.py` (端末 ID の導出と保存) | 完了 |
| 2 | `src/services/auth_store.py` に `protect` / `unprotect` を公開 | 完了 |
| 3 | `src/services/stretheus_auth.py` に匿名セッション (`ensure_session`) | 完了 |
| 4 | `src/services/points.py` の未ログイン分岐を撤去 | 完了 |
| 5 | `src/gui/points_indicator.py` (未ログインでも残高を出す) | 完了 |
| 6 | `src/gui/account_tab.py` (未ログインでも残高・履歴を出す) | 完了 |
| 7 | `tests/test_device_id.py` (6 件) | 完了 |
| 8 | `tests/test_stretheus_auth.py` に匿名セッション 5 件を追加 | 完了 |
| 9 | `tests/test_points_service.py` の未ログイン系を差し替え (24 件) | 完了 |
| 10 | `tests/test_points_ui.py` / `tests/test_subscription_ui.py` の更新 | 完了 |
| — | API 側 (`StretheusAPI`) の実装 | 完了 (単体 89 / 結合 19) — `StretheusAPI/docs/request/resolve4.md` |
| — | **dev / 本番への API デプロイ後の実疎通確認** | **未実施 (API のデプロイが要る)** |

`python -m unittest discover -s tests` → **1102 件 OK**。

---

## 1. 方針: 台帳はサーバーに置く

ポイントの権威はサーバーにある (ver5 resolve §3)。未ログインでもこれを変えない。
ローカルに台帳を持つと、ファイルを消すだけで無限に使えてしまい、要望を満たさない。

そのため「未ログイン」も**サーバー上の利用者**として扱う。クライアントは端末 ID を
API へ渡し (`POST /api/auth/device`)、匿名ユーザーの JWT を受け取る。以降の
`GET /api/points` と予約 / 確定 / 解放は、ログイン済みの場合と**まったく同じ経路**を通る。

```text
[出力ジョブ開始]
   ↓
ensure_session()        ← JWT が無ければ POST /api/auth/device (端末 ID)
   ↓  ├─ 成功         → 以降は従来と同じ (予約 → watermark 判定 → commit)
   │  └─ 失敗 (断)    → オフラインと同じ扱い (watermark 入りで出力)
   ↓
POST /api/points/reservations
```

消費経路 (`pipeline_runner` / `clip_writer` / `project_resume` / `resolve_export`) は
**1 行も変えていない**。これらは `points.reserve()` の戻り値だけを見ており、
未ログインの扱いは `PointsService` の内側で解決している。

### 1.1 採らなかった案

| 案 | 却下の理由 |
| --- | --- |
| 未ログインは常に watermark (0pt 扱い) | 「未サブスクと同じ」にならない。無償の 150pt が消え、試用もできなくなる |
| 未ログインは出力を禁止する | 試用のハードルが上がる。要望は「ポイント制にする」であり禁止ではない |
| ローカルに台帳を持つ | ファイルを消せばリセットできる。要望の「使いたい放題」を解決しない |

---

## 2. 端末 ID (`src/services/device_id.py`)

匿名ユーザーを特定する鍵。**MachineGuid から導出する**。

```python
device_id() = base64url(sha256("stretheus-device-v1:" + MachineGuid))   # 43 文字
```

* `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` を `winreg` で読む (標準ライブラリのみ)。
* **ファイルに保存しない。** アプリを再インストールしても、設定を消しても同じ ID になり、
  残高が戻らない。保存する方式だと「ファイルを消す → 150pt 復活」が成立してしまう。
* MachineGuid そのものは送らない。アプリ固有の文字列を混ぜた要約だけを送るため、
  API から端末の識別子は復元できない。
* MachineGuid が読めない環境だけ、乱数 (`secrets.token_urlsafe`) を
  `%LOCALAPPDATA%\Stretheus\device.dat` へ保存して使う。DPAPI で暗号化するが、
  **暗号化できない場合は平文で保存する**。保存しないと毎回 ID が変わり、残高が戻って
  しまうためである。この ID で辿れるのは匿名のポイント台帳だけで、個人情報は含まない
  (JWT は従来どおり、暗号化できなければ保存しない / `auth_store.py`)。

### 2.1 割り切り

同じ端末の Windows ユーザーが複数いる場合、台帳は**端末で 1 つ**になる。
利用者ごとに分けたい場合はログインしてもらう。厳しい側に倒すのは、
「別の Windows ユーザーを作れば 150pt が増える」経路を作らないためである。

---

## 3. 認証 (`src/services/stretheus_auth.py`)

セッションは 2 種類になった。保存先 (`auth.dat`) と JWT の扱いは共通で、
`anonymous` フラグだけが増える。フラグは**サーバーの応答** (`user.isAnonymous`) で決める。

| メソッド | 意味 |
| --- | --- |
| `is_logged_in()` | **Twitch ログイン済みか。** 匿名セッションでは `False` |
| `has_session()` | 種別を問わず JWT を持っているか (API を呼べるか) |
| `is_anonymous()` | 端末に紐づく匿名セッションか |
| `ensure_session()` | JWT を確保する。未ログインなら匿名セッションを作る。作れなければ `False` |
| `login_anonymously()` | `POST /api/auth/device`。ブラウザは開かない |

`is_logged_in()` の意味を**変えていない**ことが重要である。画面のログイン導線と
サブスクリプションの判定 (`subscription_window` / `account_tab` / `billing`) はここを
見ており、匿名セッションを「ログイン済み」にすると、加入できない利用者に
購入ボタンを出してしまう。

* `ensure_session()` は `_session_lock` で直列化する。同時に 2 本作っても片方が捨てられる。
* Twitch ログインすると `_apply()` が匿名セッションを置き換える (`anonymous = False`)。
* ログアウトすると JWT を捨てる。次の出力で `ensure_session()` が匿名セッションを作り、
  **同じ端末の元の台帳へ戻る** (端末 ID が変わらないため)。

---

## 4. ポイント (`src/services/points.py`)

変更は 4 か所だけで、消費のしかた自体は変えていない。

| 箇所 | 変更 |
| --- | --- |
| `reserve()` | 「未ログインは無償」の分岐を撤去し、`ensure_session()` へ置き換えた |
| `balance()` | `is_logged_in()` → `ensure_session()`。未ログインでも残高を返す |
| `flush_pending()` | `is_logged_in()` → `has_session()` |
| `_subscription_from_cache()` | 匿名セッションでは常に `False` |

### 4.1 オフラインの扱い

`ensure_session()` が失敗した場合 (= サーバーへ到達できない) は、ver5 resolve §3.3 の
オフラインと同じに倒す。すなわち **watermark 入りで出力する**。
サブスク会員だけは直近の判定キャッシュ (72h) で救済する。

ただし**匿名セッションのあいだはキャッシュで救済しない** (`_subscription_from_cache`)。
サブスク会員がログアウトした直後に、古い判定で watermark を外せてしまうためである。

### 4.2 セッション失効時の再送

JWT のセッションが失効していた場合 (401 `reauth_required`)、`_post_reservation()` が
**作り直して 1 回だけ再送する**。ここで諦めると、次の出力まで watermark が入ってしまう。
冪等キー (`clientJobId`) は同じで良い (利用者が変わるため衝突しない)。

### 4.3 画面

| 画面 | 未ログインでの表示 |
| --- | --- |
| 残高インジケータ | **この端末の残高**を出す。「ログイン」ボタンも残す。ツールチップで切り替わる旨を案内 |
| アカウントタブ | 「ログインしていません (この端末のポイント)」+ 残高・単価・履歴。サブスクの節は出さない |
| 透かしの確認 | 変更なし。予約の応答が `watermarkRequired` のときだけ出す (R9 / R12) |

サブスクリプションの節を未ログインで出さないのは、匿名では加入できず
(API が `login_required` で断る)、状態の照会先も無いためである。

---

## 5. 壊していないこと

* 消費経路 (`pipeline_runner` / `clip_writer` / `project_resume` / `resolve_export`) は無変更。
* `points.reserve/commit/cancel` の呼び出し規約 (`service = None` で素通り) は無変更。
  CLI / テストからの出力は従来どおりポイントを通らない。
* `is_logged_in()` の意味は無変更。サブスクリプション画面・課金導線は影響を受けない。
* アーカイブ取得用の Twitch 認証 (`src/archive/twitch_auth.py`、implicit flow) とは無関係。

---

## 6. 残り

* API を dev / 本番へデプロイしたあとの実疎通確認 (匿名で 150pt → 消費 → 0pt で watermark)。
* 同一端末での「ログイン → ログアウト → 残高が元の匿名台帳へ戻る」確認。
