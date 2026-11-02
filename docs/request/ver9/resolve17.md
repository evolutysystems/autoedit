# ver9 resolve17 — Stripe の加入が画面へ即時反映されない / 加入後も購入ボタンが押せる

発端: 2026-11-02 のチャット要望
　　　「Stripe で支払いを行い成功したが、**サブスクリプションのボタンが押下できる**のが問題。
　　　また、加入したのに**画面に即時反映されない**のはユーザーに大きな不安を与える。
　　　再起動でしか解決できないのであれば、Stripe からのレスポンスを受け取って
　　　**自動で再起動するべき**」
対象: `src/gui/main_window.py` (`_on_open_subscription` / `_refresh_subscription_button`) /
　　　`src/gui/subscription_window.py` / `src/gui/points_indicator.py` /
　　　`src/gui/billing_workers.py`
調査日: 2026-11-02 (`StretheusAPI` `9b44f49` のデプロイ直後 / live 決済 1 件成功後)
種別: **不具合の原因調査と修正設計 → 実装完了**。§1 は現行コードで確認済み
状態: **2026-11-02 実装完了 (実機確認待ち)**。単体テスト 2,224 件 OK。実装記録は §10

---

## 0. 先に結論

**主原因は 1 行の信号の選び間違いで、サーバ側は正しく動いている。**

`main_window.py:1306` が `SubscriptionWindow.destroyed` に
「閉じたら残高を取り直す」処理をつないでいる。しかし
**この画面は閉じても破棄されない** (`WA_DeleteOnClose` を設定しておらず、
`self._subscription_window` が Python 側の参照を持ち続ける) ため、
**`destroyed` はウィンドウを閉じた時点では発火しない。**
次に発火するのは**次回この画面を開き直したとき**か**アプリ終了時**である。

加えて、支払いの反映待ち (`ActivationWorker`) が
**サブスクリプション画面の寿命に縛られている**。`closeEvent` が
`requestInterruption()` を呼ぶので、**画面を閉じると待ち合わせが打ち切られる** (§1.3)。

この 2 つが重なると、支払い後の状態更新の経路が**すべて途切れる**。
残るのは `points_indicator` の定期更新だけで、その間隔は **既定 300 秒** である。
つまり「ブラウザで支払う → アプリに戻る → 画面を閉じる」という自然な操作をすると、
**最大 5 分間、未加入のままの表示と押せる購入ボタンが残る。**
「再起動でしか直らない」のは、起動時に `refresh(force=True)` が走るからである。

| # | 欠陥 | 影響 |
| --- | --- | --- |
| 1 | `destroyed` は画面を閉じても発火しない (§1.2) | **閉じた時点の取り直しが起きない。これが主原因** |
| 2 | 反映待ちが画面の寿命に縛られている (§1.3) | 閉じると待ち合わせが消え、`subscription_changed` が飛ばない |
| 3 | `refresh()` が実行中を理由に要求を黙って捨てる (§1.4) | 更新要求が重なると新しい値を取り逃がす |
| 4 | 同じ欠陥が設定画面にもある (§1.6) | ログアウト後も残高インジケータが更新されない |

### 0.1 自動再起動は採らない

**再起動は不要であり、このアプリでは有害である (§2)。**

* クライアントは**再起動でしか直らない状態を持っていない**。加入状態は
  `GET /api/points` の応答そのもので、`balance(force=True)` が毎回取り直す (§2.1)。
* **出力時の透かしはサーバ権威**で、画面の表示が古くても**誤った出力は起きない** (§2.2)。
* Stretheus は**編集中・出力中の状態を抱える編集アプリ**で、`_running_tabs` が
  示すとおり出力ジョブが走っていることがある。Webhook 由来の外部イベントで
  勝手に再起動すると、**作業と実行中の出力を壊す** (§2.3)。

求められているのは「再起動」ではなく「**即時反映**」なので、
**更新の経路を直して即時にする**のが正しい修正である (§3)。
それでも最後の保険として再起動を残す場合の条件は §2.4 に書く。

---

## 1. 現状の仕組み (調査結果)

### 1.1 支払い後に表示が変わる経路は 3 本しかない

```
① 反映待ちの成功
   SubscriptionWindow._on_checkout_url (286 行)
     → _start_activation (302) → ActivationWorker
       → billing.wait_for_activation (billing.py:217)
          3 秒間隔 / 最大 180 秒で GET /api/points を叩き unlimited を見る
       → done(True) → _on_activation (309)
          → self.reload()                 … この画面の表示
          → subscription_changed.emit()   → main_window:1302
                                          → points_indicator.refresh(force=True)
                                          → balance_changed → _refresh_subscription_button

② 画面を閉じたときの取り直し
   SubscriptionWindow.destroyed → main_window:1306
     → points_indicator.reload() → refresh(force=True)

③ 定期更新
   points_indicator の QTimer (points_indicator.py:161-164)
     既定 300 秒 (settings の points.balance_refresh_sec / 下限 30 秒)
```

**① と ② が両方とも機能しない**ため、実際には ③ だけが残る。

### 1.2 【主原因】`destroyed` は画面を閉じても発火しない

```python
# main_window.py:1304-1307
# 「main_window に戻ってきた際に」加入状態を反映する (要望 ①後 / ②後)。
# ログイン / 支払いはブラウザ側で進むため、閉じた時点で取り直す。
self._subscription_window.destroyed.connect(
    lambda *_a: self.points_indicator.reload())
```

コメントの意図は正しいが、**信号の選択が誤っている**。

* `SubscriptionWindow` は `Qt.WA_DeleteOnClose` を設定していない
  (`subscription_window.py` に `setAttribute` は 1 つも無い)。
* `self._subscription_window` が Python 側の参照を保持し続ける
  (`_on_open_subscription` が代入し、`isVisible()` の判定に使う)。

したがって**閉じても C++ オブジェクトは生きており、`destroyed` は飛ばない**。
実際に飛ぶのは次のいずれかで、どちらも手遅れである。

| 発火する瞬間 | なぜ手遅れか |
| --- | --- |
| 次に `_on_open_subscription` が新しい窓を代入したとき | **その時点では新しい窓が自分で `reload()` している**ので意味が無い |
| アプリ終了時 | 終了処理中。`points_indicator.reload()` は解放済みを見て何もしない (同関数の `isValid` 判定) |

> `WA_DeleteOnClose` を付けて `destroyed` を活かす選択は**採らない**。
> 参照を持ったまま破棄すると、再オープン時の `isVisible()` が解放済みの
> C++ オブジェクトを触って `RuntimeError` になる。これは
> `points_indicator.reload()` が `isValid()` で防いでいるのと同じ事故で、
> 同じ罠を 1 つ増やすことになる。§4.1 の `closed` シグナルを使う。

### 1.3 反映待ちが画面の寿命に縛られている

```python
# subscription_window.py:329-336 (closeEvent)
if self._activation_worker is not None and self._activation_worker.isRunning():
    self._activation_worker.requestInterruption()
```

`wait_for_activation` は `should_continue=lambda: not self.isInterruptionRequested()`
で駆動されるため (`billing_workers.py:78-79`)、**中断要求が入ると即座に `False` を返す**。

結果として `_on_activation(False)` が走り、

* `subscription_changed` は **emit されない**
* 代わりに「お支払いの反映を確認できませんでした」の注記が設定されるが、
  **画面は閉じているので利用者には見えない**

**「ブラウザで支払い、アプリへ戻り、用が済んだので画面を閉じる」という
もっとも自然な操作が、反映の仕組みを自分で壊す**構造になっている。

画面を開いたままにしていれば ① は正しく動く。
今回の報告はこの差で説明がつく。

### 1.4 `refresh()` が実行中を理由に要求を捨てる

```python
# points_indicator.py:170-175
def refresh(self, force=False):
    if self._worker is not None and self._worker.isRunning():
        return      # ← 捨てられる。再試行も保留もしない
```

`subscription_changed` → `refresh(force=True)` が、
定期更新や別経路の取得と重なると**黙って捨てられる**。
保留フラグも再試行も無いため、**次は 300 秒後**になる。

①②が直ったあとも、この取り逃がしは残る。

### 1.5 メイン画面のボタンは残高が無いと押せるまま

```python
# main_window.py:1318-1327
def _refresh_subscription_button(self, balance):
    self._subscription_kind = theme.subscription_check_kind(balance)
    subscribed = bool(self._subscription_kind)
    ...
    self.subscription_button.setEnabled(not subscribed and not self._running_tabs)
```

`subscription_check_kind` は `balance` の `subscriptionType` だけを見る
(`theme.py:1578-1584`)。**残高が取れていなければ空文字 = 未加入扱い**で、
ボタンは押せるままチェックマークも付かない。

これは「この画面のボタンは入口であって関門ではない」という
意図された設計 (同関数のコメント) であり、**それ自体は変えない**。
ただし §1.2〜1.4 で状態が 5 分間更新されないため、
**意図が「一瞬の猶予」ではなく「5 分間の誤表示」になっている**。

なお、押せてしまった購入ボタンを実際に押しても二重課金は起きない。
サブスクリプション画面が契約情報と加入種別をそろえてから
`purchase_blocked_reason` で `BLOCK_STRIPE` を返し、
さらにサーバが `409 already_subscribed` で断る
(`StretheusAPI` `BillingService.CreateCheckoutSessionAsync`)。
**実害は「不安」と「無駄な操作」であって、課金事故ではない。**

### 1.6 同じ欠陥が設定画面にもある

```python
# main_window.py:1288-1290
self._settings_window.destroyed.connect(
    lambda *_a: self.points_indicator.reload())
```

**まったく同じ形**である。`SettingsWindow` も `WA_DeleteOnClose` を持たず、
`self._settings_window` が参照を保持する。したがって
**アカウントタブでログアウトして設定画面を閉じても、残高インジケータは更新されない**
(ver5 resolve §5.6 の意図が効いていない)。

今回の調査で判明した**同一原因の別症状**なので、同時に直す。

---

## 2. 自動再起動を採らない理由

### 2.1 再起動が必要な状態をクライアントは持っていない

加入状態の保持場所は 1 つで、すべて**実行中に取り直せる**。

| 保持場所 | 更新のされ方 |
| --- | --- |
| `PointsService._balance` (`points.py:165`) | `balance(force=True)` が毎回 `GET /api/points` で上書きする (151-169 行) |
| `points_indicator` の表示 | `balance_changed` で描き直す (191-197 行) |
| メイン画面のボタン | `_refresh_subscription_button` で描き直す |
| サブスクリプション画面 | `reload()` で取り直す |

**起動時にだけ読む値は 1 つも無い。**
再起動が「効く」のは、起動時に `refresh(force=True)` が走る (`points_indicator.py:167`)
からにすぎない。**同じことを再起動せずに行えるのが §4 の修正である。**

### 2.2 出力時の透かしはサーバ権威なので誤出力は起きない

> **⚠ 2026-11-02 訂正: 本節の結論は誤りだった。**
> サーバが権威であることは正しいが、**そのサーバ自身がライセンス判定を
> 10 分キャッシュしており、Webhook がそれを破棄していなかった**。
> `POST /api/points/reserve` も `GET /api/points` と同じキャッシュを通るため、
> **加入直後の最大 10 分は透かしが入る / ポイントが消費される**。
> 原因と修正は `StretheusAPI/docs/request/resolve7.md`。
> 以下は訂正前の記述として残す。
>
> **この誤りが §2 の判断自体を変えるわけではない** (再起動は依然として不要)。
> ただし「表示の遅れだけで成果物に影響しない」という実害の見積もりは
> 1 段上げる必要があった。

表示が古いあいだに出力しても、透かしの有無は**サーバが決める**。

```python
# services/points.py:219-220 (reserve の応答)
reservation.watermark_required = bool(response.get("watermarkRequired"))
reservation.unlimited = bool(response.get("unlimited"))
```

`POST /api/points/reserve` が毎回判定するため、
~~**加入済みなのに透かしが入る、という出力事故は起きない。**~~
→ **サーバ側のキャッシュが古い場合は起きる (上記の訂正)。**

クライアント側で `last_balance()` を見ているのは
`timeline/timeline_editor_dialog.py:1475-1494` の**先読み (編集画面の事前警告)** だけで、
取れていなければ安全側 (透かしあり) に倒す実装になっている。
~~**表示の遅れは「不安」の問題であって、成果物の問題ではない。**~~
この切り分けは利用者への説明にも使える。

### 2.3 編集アプリを勝手に再起動する害

* **出力ジョブが走っていることがある。** `_running_tabs` は実行中タブを集合で持ち、
  実行中は設定ボタンとサブスクリプションボタンを無効化している。
  再起動はこの実行を中断する。
* **タイムラインの編集状態を抱えている。** 未保存の編集を失う。
* **再起動の仕組み自体が無い。** 現在あるのは `QApplication.quit()` (`main_window.py:1501`)
  だけで、自己再実行は実装されていない。**凍結 (PyInstaller / onedir) 環境での
  自己再起動は `sys.executable` の扱いが絡み、新しい失敗モードを持ち込む。**
* **引き金が外部イベントである。** Stripe の Webhook がサーバへ届いたことを
  クライアントがポーリングで知る、という非同期の経路で再起動を起こすと、
  **利用者が何もしていない瞬間にアプリが落ちて立ち上がる**ことになる。

### 2.4 それでも再起動を残すなら

不要と考えるが、採るならこの形に限る。**無言の自動再起動は入れない。**

| 条件 | 理由 |
| --- | --- |
| 反映待ちが**タイムアウトした場合のみ** | 正常系では §4 の修正で即時反映される |
| `_running_tabs` が**空のときのみ** | 出力中に落とさない |
| **未保存の編集が無いときのみ** | 作業を失わない |
| **ダイアログで利用者に尋ねる** (「再起動すると確実に反映されます」) | 勝手に落とさない |

実装するとしても §4 の後に、別の要望として扱う。

---

## 3. 改善方針

1. **反映待ちをアプリ側 (メイン画面) が持つ。** 画面を閉じても待ち合わせを続ける (§4.2)。
2. **`destroyed` ではなく `closed` シグナルで閉じた瞬間を拾う** (§4.1 / §4.5)。
3. **`refresh()` の取り逃がしを無くす** (§4.3)。
4. **利用者が自分で確かめられる「更新」を置く** (§4.4)。

1 と 2 は冗長に見えるが、**役割が違うので両方入れる**。
1 は「支払いの反映」を確実にし、2 は「ログイン / ログアウトなど画面内の任意の変更」を拾う。

---

## 4. 実装設計

### 4.1 `SubscriptionWindow` にシグナルを 2 本足す

```python
class SubscriptionWindow(QWidget):

    # 加入が確認できた (既存)
    subscription_changed = Signal()
    # ブラウザでの支払いを開始した。反映待ちはメイン画面が引き取る (§4.2)
    checkout_started = Signal()
    # この画面が閉じられた。destroyed は閉じても飛ばないため別に用意する (§1.2)
    closed = Signal()
```

`closeEvent` の先頭で `self.closed.emit()` を出す。
**反映待ちの `requestInterruption()` は外す** (メイン画面が引き取るため)。
残高・契約情報・URL の取得を待ち合わせる既存の後始末はそのまま残す。

```python
def closeEvent(self, event):
    self.closed.emit()
    for worker in (self._url_worker, self._config_worker, self._balance_worker):
        if worker is not None and worker.isRunning():
            worker.wait(5000)
    super().closeEvent(event)
```

`_on_checkout_url` (286 行) は、自前で `_start_activation()` する代わりに
`checkout_started.emit()` を出す。画面を開いたままの利用者への表示は
メイン画面から戻る `subscription_changed` で更新される。

### 4.2 反映待ちをメイン画面が持つ

`_on_open_subscription` の結線を差し替える。

```python
self._subscription_window = SubscriptionWindow(self._points, self._settings)
# 画面を開いたまま加入が確認できた場合にも印を付ける (既存)
self._subscription_window.subscription_changed.connect(
    lambda: self.points_indicator.refresh(force=True))
# 支払いの反映待ちはメイン画面が引き取る。画面を閉じても待ち続ける (resolve17 §1.3)
self._subscription_window.checkout_started.connect(self._start_activation_wait)
# 閉じた時点で取り直す。destroyed は閉じても飛ばない (resolve17 §1.2)
self._subscription_window.closed.connect(
    lambda: self.points_indicator.reload())
self._subscription_window.show()
```

メイン画面側に待ち合わせを持つ。

```python
# 支払いがサーバーへ反映されるのを待つ。サブスクリプション画面を閉じても続ける。
# 親をメイン画面にするため、画面の破棄でスレッドが道連れにならない。
def _start_activation_wait(self):
    if self._activation_worker is not None and self._activation_worker.isRunning():
        return
    billing = BillingService(self._points, self._settings)
    self._activation_worker = ActivationWorker(billing, parent=self)
    self._activation_worker.done.connect(self._on_activation_done)
    self._activation_worker.start()

def _on_activation_done(self, activated):
    if not activated:
        # 支払いを取りやめた場合もここへ来る。失敗とは書かない。
        return
    # 残高を取り直す。ボタンの印と可否は balance_changed 経由で付く。
    self.points_indicator.refresh(force=True)
    # 画面が開いたままなら、そちらの表示も作り直す。
    window = self._subscription_window
    if window is not None and isValid(window) and window.isVisible():
        window.reload()
```

`isValid` は `main_window.py` に**まだ import されていない**。
`points_indicator.py:21` と同じ `from shiboken6 import isValid` を足す
(終了処理中に解放済みのウィジェットを触らないための既存の作法に合わせる)。

**アプリ終了時の後始末が必要になる。** メイン画面の `closeEvent` で
`requestInterruption()` → `wait()` を行う (最大 180 秒走るスレッドを
親ごと破棄すると Qt が異常終了する。`subscription_window.closeEvent` の
既存コメントと同じ理由)。

> `BillingService` をメイン画面でも作ることになる。
> `PointsService` を共有しているので状態は分かれない
> (`BillingService` が持つのは契約情報のキャッシュだけで、
> 加入判定は `PointsService` 側の残高を見る)。
> 気になる場合は `SubscriptionWindow._billing` を渡す形でもよいが、
> **画面が閉じたあとも使うため、画面の属性へ依存させない方が安全である。**

### 4.3 `refresh()` の取り逃がしを無くす

```python
def refresh(self, force=False):
    if self._worker is not None and self._worker.isRunning():
        # 取り逃がさない。実行中の取得が終わったら、もう一度取りに行く。
        # 捨てると次の更新が定期更新 (既定 300 秒) まで来ない (resolve17 §1.4)。
        self._pending_force = self._pending_force or force
        self._pending = True
        return

    self._pending = False
    self._pending_force = False
    self._worker = BalanceWorker(self._points, force=force, parent=self)
    self._worker.loaded.connect(self._on_loaded)
    self._worker.start()
```

`_on_loaded` の末尾で保留を処理する。

```python
def _on_loaded(self, balance):
    ...
    self.balance_changed.emit(shown)
    if self._pending:
        force = self._pending_force
        self._pending = False
        self._pending_force = False
        self.refresh(force=force)
```

**保留は 1 回だけ持つ** (キューにしない)。
同じ `GET /api/points` を並べても意味が無く、
取りたいのは「最新を 1 回」だけである。

### 4.4 「更新」ボタンと文言

サブスクリプション画面に **「更新」ボタン**を 1 つ置く。
押すと `reload()` が走る (通信は既存の経路のまま)。

反映待ちがタイムアウトしたときの注記を、行動が分かる文へ変える。

```
現在: お支払いの反映を確認できませんでした。手続きが完了している場合は、
      しばらくしてからこの画面を開き直してください。

変更: お支払いの反映をまだ確認できていません。お支払いが完了している場合は、
      「更新」を押すと確認できます。
```

**この 1 つで「不安」はほぼ解消する**。利用者が自分で確かめる手段が無いことが、
待たされることそのものより不安を生む。

### 4.5 設定画面の同じ欠陥

`SettingsWindow` (`src/settings/settings_window.py:1870`) にも `closed` シグナルを足し、
`main_window.py:1289` の `destroyed` を `closed` へ差し替える (§1.6)。

```python
self._settings_window.closed.connect(
    lambda: self.points_indicator.reload())
```

`destroyed` の結線は**残さない**。残すと終了時にも飛び、
`points_indicator.reload()` の `isValid()` 判定に頼ることになる。

**この画面は `closeEvent` も `Signal` も現在持っていないため、3 点の追加が要る。**

| 追加 | 内容 |
| --- | --- |
| import | `from PySide6.QtCore import Qt, QUrl` (8 行) へ `Signal` を足す |
| シグナル | `class SettingsWindow(QWidget)` に `closed = Signal()` |
| `closeEvent` | 新規に追加し、`self.closed.emit()` → `super().closeEvent(event)` |

`SubscriptionWindow` と違い、待ち合わせるワーカーは持っていないため
`closeEvent` は emit だけでよい。

---

## 5. 変更ファイル

| ファイル | 変更 |
| --- | --- |
| `src/gui/subscription_window.py` | `checkout_started` / `closed` シグナル追加。`closeEvent` から反映待ちの中断を外す。`_start_activation` / `_on_activation` / `_activation_worker` を撤去。「更新」ボタンと文言 (§4.1 / §4.4) |
| `src/gui/main_window.py` | `_on_open_subscription` の結線差し替え。`_start_activation_wait` / `_on_activation_done` 追加。`closeEvent` で待ち合わせの後始末。`isValid` の import。設定画面の `destroyed` → `closed` (§4.2 / §4.5) |
| `src/gui/points_indicator.py` | `refresh()` の保留処理 (§4.3) |
| `src/settings/settings_window.py` | `Signal` の import · `closed` シグナル · `closeEvent` の新規追加 (§4.5) |
| `src/gui/billing_workers.py` | 変更なし (`ActivationWorker` をそのまま使う) |
| `src/services/billing.py` | 変更なし |

**設定値の追加は無い。** `points.balance_refresh_sec` (既定 300) も変えない。
定期更新は保険であって、即時反映は §4.2 が担うためである。

## 6. テスト計画

### 6.1 単体テスト

| 対象 | 内容 |
| --- | --- |
| `points_indicator.refresh` の保留 | 実行中に `refresh(force=True)` を呼ぶと、完了後にもう一度取得が走る。保留は 1 回に畳まれる (§4.3) |
| `purchase_blocked_reason` | 既存の真理値表 (ver6 resolve2 §6) に変更なし。回帰確認のみ |

`ActivationWorker` / ウィンドウの結線は `QThread` と Qt のイベントループを伴うため、
既存の方針どおり単体テストの対象にしない。**確認は 6.2 の実機手順で行う。**

### 6.2 実機確認 (加入済みアカウントでも大半を確認できる)

| # | 手順 | 期待 |
| --- | --- | --- |
| 1 | サブスクリプション画面を開き、**閉じる** | 残高インジケータが取り直される (§1.2 の修正確認。ログ 1 行で分かる) |
| 2 | 設定画面のアカウントタブでログアウトし、**閉じる** | 残高インジケータが匿名の表示へ変わる (§1.6) |
| 3 | 購入ボタンを押し、ブラウザを開いたまま**画面を閉じる** | 反映待ちが動き続ける (アプリのログに `ActivationWorker` の周回が残る) |
| 4 | 3 のまま支払いを完了する | **画面を閉じていても**メイン画面のボタンが押せなくなり、チェックマークが付く |
| 5 | 3 のままブラウザを閉じ、支払わない | 180 秒後に静かに終わる。エラーダイアログを出さない |
| 6 | 画面を開いたまま支払いを完了する | 従来どおり画面とボタンの両方が更新される (回帰) |
| 7 | アプリ終了を反映待ちの最中に行う | 異常終了しない (§4.2 の後始末) |

**4 は未加入のアカウントが必要で、実費 980 円がかかる。**
1〜3 と 5〜7 は加入済みのアカウントでも確認できるため、
**4 以外を先に通し、4 は次の未加入アカウントの機会に回してよい。**

## 7. 互換性

* 配布済みクライアントへの影響は無い (サーバの応答は変えない)。
* `setting.json` の追加キーは無い。
* `GET /api/billing/subscription` が未実装のままでも動く
  (本件は `GET /api/points` の `subscriptionType` / `unlimited` だけを見ている)。

## 8. 残課題

1. **`GET /api/billing/subscription` が未実装。** 加入直後に
   「次回更新日」を出せないため、反映の確認が `unlimited` の有無だけになる。
   実装すれば §4.4 の「更新」で次回更新日まで出せる
   (`StretheusAPI/docs/request/resolve3.md` §10 / `StretheusPlan` の実装残課題)。
2. **Webhook の到着が 180 秒を超えた場合**は、依然として「更新」か定期更新 (300 秒) 待ちになる。
   実測では Webhook は数秒で届いており、180 秒は十分広い。
   足りないと分かった時点で `billing.activation_timeout_sec` (設定値) を伸ばす。
3. **§2.4 の条件付き再起動**は本書では実装しない。§4 を入れたあとに
   なお不安が残るかを利用者に確認してから判断する。

## 9. 要望との対応

| 要望 | 本書での扱い |
| --- | --- |
| 加入後も購入ボタンが押せる | §1.2〜1.4 が原因。§4.1〜4.3 で、支払い反映と同時にボタンが無効化される。二重課金は元から起きない (§1.5) |
| 画面に即時反映されない | 同上。画面を閉じても反映待ちが生き、反映時にメイン画面が更新される (§4.2) |
| 再起動でしか解決できないなら自動再起動すべき | **再起動は不要**。再起動でしか直らない状態をクライアントは持っておらず (§2.1)、出力も誤らない (§2.2)。編集アプリを勝手に落とす害が大きい (§2.3)。採るなら条件付き・確認付きに限る (§2.4) |

---

## 10. 実装記録 (2026-11-02)

### 10.1 設計からの変更点 1 件

**§4.2 の「反映時に `window.reload()` を呼ぶ」を `window.apply_activation(activated)` に変えた。**

`reload()` だけでは `_in_progress` が戻らない。このフラグは購入ボタンを押した時点で
`True` になり、以前は画面側の `_on_activation` が戻していた。反映待ちをメイン画面へ
移したことで戻す主体がいなくなり、**反映後も購入ボタンが無効のまま、注記が
「反映を待っています…」で固まる**ところだった。

そのため `SubscriptionWindow` に公開メソッドを 1 つ置いた。

```python
# 反映待ちの結果をメイン画面から受け取る (§4.2)。
def apply_activation(self, activated):
    self._in_progress = False
    if activated:
        self.reload()
        return
    self._apply_state()
    self.note_label.setText(
        tr("お支払いの反映をまだ確認できていません。"
           "お支払いが完了している場合は、「更新」を押すと確認できます。"))
```

メイン画面は画面が生きていて表示中のときだけ呼ぶ。閉じられていれば
届ける表示が無いので呼ばない。

### 10.2 変更したファイル

| ファイル | 変更 |
| --- | --- |
| `src/gui/subscription_window.py` | `checkout_started` / `closed` シグナル追加。`_activation_worker` / `_start_activation` / `_on_activation` を撤去し `apply_activation` を追加。`closeEvent` が `closed` を emit し、**反映待ちを中断しない**。`ActivationWorker` の import を削除。「更新」ボタンと `retranslate` |
| `src/gui/main_window.py` | `BillingService` / `ActivationWorker` / `isValid` の import (絶対・相対の両ブロック)。`_activation_worker` 属性。`_start_activation_wait` / `_on_activation_done` を追加。`closeEvent` で反映待ちの後始末。サブスクリプション画面と設定画面の `destroyed` → `closed` |
| `src/gui/points_indicator.py` | `_pending` / `_pending_force` を追加。`refresh()` が取得中の要求を保留し、`_on_loaded` の末尾で取り直す |
| `src/settings/settings_window.py` | `Signal` の import · `closed` シグナル · `closeEvent` の新規追加 |
| `src/i18n/catalog_en.py` | 反映待ちの注記を差し替え。「更新」は既存 (`'更新': 'Refresh'`) |
| `tests/test_points_refresh_pending.py` | **新規 6 件。** 保留・畳み込み・`force` の粘着・保留が無いときの非再取得 |
| `tests/test_subscription_reflect.py` | **新規 7 件。** `closed` の発火 (閉じても生きている状態で)・`checkout_started`・画面が待ち合わせを持たないこと・`apply_activation` |

### 10.3 テスト結果

```
py -m unittest discover -s tests
Ran 2224 tests in 18.026s
OK
```

実装前の基準は 2,217 件。**増えた 13 件が新規テストで、既存の回帰は無い。**
`tests/test_i18n.py` の対訳表網羅検査も通っている
(新しい注記を `catalog_en.py` へ入れたため)。

`import src.gui.main_window` のスモークも確認した
(絶対 import と相対 import の 2 ブロックへ同じ import を足したため)。

### 10.4 残り: 実機確認

§6.2 の表のうち **4 (未加入アカウントでの支払い) 以外**は加入済みアカウントで確認できる。
とくに **1 (画面を閉じたら残高が取り直される)** が今回の主原因の直接確認になる。

**未実施。** 利用者の実機確認を経てクローズとする。
