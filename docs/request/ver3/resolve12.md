# resolve12（ver3） — Timeline 編集画面「再生ヘッドを戻して再生すると再生されない」不具合 修正設計書

## 0. 本書の位置づけ

* 対象要望: 口頭要望（`request12.md` は存在しない）
  > Timeline 編集画面で、最初にプレビュー再生した時は再生されるが、
  > 少し再生ヘッドを手で戻して再度プレビュー再生した場合は再生されない。この現象を検証してほしい。
* 本書は **原因調査の結果と、その修正設計**。実装は本書のレビュー後に着手する
  （`docs/claude.md`「実装前に設計を行うこと」）。
* 調査は **2026-09-01 時点の実コード**を読み、さらに
  **本番コード `PreviewPanel._on_audio_position` をそのまま呼び出す再現スクリプト**と
  **PySide6 6.11.0 の QMediaPlayer 実機検証**の 2 本で裏を取った。推測で書いた箇所は無い。
* 変更は **`src/gui/timeline/preview_panel.py` の 3 箇所のみ**。
  Timeline モデル・コマンド・レンダラ・出力・保存形式には一切触れない。
* **setting.json は本書では書き換えない**。新しい設定項目も増やさない（§3-3 / §7）。

---

## 1. 要望と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **E1** | 「再生ヘッドを手で戻して再生すると再生されない」現象の**原因を特定**する | 調査 |
| **E2** | 戻した位置から**即座に**プレビューが動き出すようにする | 不具合修正 |

要望文に無いが、実装に必ず要る論点:

| ID | 論点 | 理由 |
|---|---|---|
| **E3** | 戻す操作の**経路が 3 つある**（スクラブ / キーボード / 倍速逆再生）ことの明示 | 同じ真因が全経路で顕在化する。1 経路だけ直すと再発する |
| **E4** | 既存のフレーム間引き（`play_fps` 上限）を**壊さない**こと | 間引きは「音声を優先し、映像が間に合わなければ捨てる」ための機構（resolve6 §3-9）。無効化してはならない |
| **E5** | リセット値に**生の数値を埋めない**こと | `docs/claude.md`「ハードコードは禁止」との整合 |

---

## 2. 現状分析

### 2.1 プレビューの映像はどこで更新されるか

プレビューの絵・再生ヘッドの縦線・時刻ラベルは、**すべて `playhead_moved` シグナル 1 本**から更新される。

```
TimelineController.set_playhead()             # timeline_controller.py:358
    └─ playhead_moved.emit(value)
         └─ PreviewPanel._on_playhead_moved()  # preview_panel.py:356
              ├─ self._request_frame()         # 映像フレームの取得要求
              ├─ self._rebuild_overlays()      # 字幕・画像オーバーレイの並べ直し
              └─ self._update_time_label()     # 「00:15.000 / 00:24.200」の更新
```

`set_playhead()` は **値が変わらなければ何もしない**（`timeline_controller.py:361`）。
したがって **`set_playhead()` が呼ばれない = プレビューが完全に固まる**。

### 2.2 再生中のマスタークロックは QMediaPlayer

前進再生では QMediaPlayer をマスタークロックにし、`positionChanged` を受けて
再生ヘッドを進める（resolve.md §6.4-5）。ここが唯一の駆動源である。

```python
# src/gui/timeline/preview_panel.py:739-756（現状）
def _on_audio_position(self, position_ms):
    if self._rate <= 0:
        return
    sec = self._chunk_start + position_ms / 1000.0
    total = self._controller.timeline.duration_sec()
    if sec >= total:
        self.pause()
        self._controller.set_playhead(total)
        return
    # 映像の更新は play_fps で頭打ちにする (音声を優先し、間に合わなければ間引く)
    min_interval = 1.0 / max(int(self._cfg["play_fps"]), 1)
    if sec - self._last_frame_at >= min_interval:
        self._last_frame_at = sec
        self._controller.set_playhead(sec)
    ...
```

### 2.3 真因 —— `_last_frame_at` が単調増加しかせず、巻き戻らない

`_last_frame_at` は「最後に映像を更新した秒」を持つ間引き用の状態である。
`grep` で全出現を洗ったところ、**代入は 2 箇所しか無い**。

```
190:        self._last_frame_at = 0.0      ← __init__ での初期化（1 回だけ）
750:        if sec - self._last_frame_at >= min_interval:
751:            self._last_frame_at = sec  ← _on_audio_position の中だけ
```

つまり **`_last_frame_at` は再生開始・停止・シークのいずれでもリセットされない**。
一度到達した秒数から下がることが無い。

その結果、判定式 `sec - self._last_frame_at >= min_interval` は
**再生開始位置が前回の到達点より手前だと、右辺どころか負の値になる**。

| 場面 | `_last_frame_at` | 開始 `sec` | `sec - _last_frame_at` | `>= 0.0667` | 結果 |
|---|---|---|---|---|---|
| 初回再生（0s から） | 0.0 | 0.0 | 0.0 → 以降 正 | 満たす | **更新される** ✅ |
| 20s まで再生 → 15s へ戻して再生 | **20.0 のまま** | 15.0 | **−5.0** | 満たさない | **更新されない** ❌ |

音声は正しい位置から鳴っているのに `set_playhead()` が一度も呼ばれないため、
§2.1 のとおり **映像・再生ヘッド・時刻表示のすべてが停止して見える**。
沈黙は `sec` が `_last_frame_at + min_interval` を越えるまで続く
= **戻した秒数ぶんそのまま固まる**。

### 2.4 「初回だけ動く」ことの説明

`__init__` の初期値 `0.0`（`preview_panel.py:190`）と、
画面を開いた直後の再生ヘッド位置 `0.0` が**たまたま一致している**ためである。
初回再生は必ず判定を通る。要望文の「最初にプレビュー再生した時は再生される」と完全に一致する。

なお、**手で「進めて」から再生した場合は正常に動く**（差が正になるため）。
要望が「手で戻した場合」に限定されているのも、この真因で説明がつく。

### 2.5 再現（本番コードをそのまま呼んで確認）

`PreviewPanel._on_audio_position` を **未改変のまま**、`self` だけスタブに差し替えて
`positionChanged` 相当の通知を流した結果:

```
[1回目] 0s→20s 再生   → 再生ヘッドの更新 200 回 / _last_frame_at = 20.0
[手動]  再生ヘッドを 15.0s へ戻す （_rate=0 のため _last_frame_at は 20.0 のまま）
[2回目] 15s→21s 再生  → 最初に動いた位置 = 20.1s ／ 無反応 5.1 秒
```

戻した量に比例して沈黙が伸びる。頭まで戻した場合はクリップ全長ぶん固まり、
体感としては「まったく再生されない」になる。

### 2.6 影響を受ける「戻す」経路（E3）

再生ヘッドを戻す経路は 3 つあり、**いずれも `_last_frame_at` を触らない**ため全滅する。

| # | 経路 | 実装 | `_last_frame_at` |
|---|---|---|---|
| 1 | ルーラ／トラックのドラッグ（スクラブ） | `_on_scrub_changed()` `preview_panel.py:819` → `_scrub_tick()` | スクラブ中は `_rate == 0` で `_on_audio_position` が早期 return するため**据え置き** |
| 2 | キーボードでのコマ戻し | `TimelineController.step_playhead()` `timeline_controller.py:368` | `set_playhead()` を直接呼ぶだけで**据え置き** |
| 3 | 倍速逆再生（◀◀） | `_step_backward()` `preview_panel.py:606` | `set_playhead()` を直接呼ぶだけで**据え置き** |

3 の逆再生は特に分かりにくい。**◀◀ で戻したあと ▶ を押すと同じく固まる**。

### 2.7 シロだった仮説 —— `setSource` → `setPosition` の順序

`_start_playback()`（`preview_panel.py:661`）は、ロード完了前に seek を投げる
Qt の典型的な罠に見える形をしている。

```python
self._player.setSource(QUrl.fromLocalFile(path))   # 非同期ロードが始まる
self._player.setPosition(offset_ms)                # ロード完了前の seek？
self._player.play()
```

さらに、未編集 Timeline では認識用音声を再利用するため
`AudioChunkSource.cached()` が**常に同じパス**を返す（`audio_source.py:78-80`）。
= 2 回目以降は「同じファイルを再度 `setSource` する」形になり、疑わしさが増す。

**PySide6 6.11.0 で実機検証した結果、これは問題なかった。**

```
1回目: offset=0 で再生            → 0.8秒後 position = 512 ms
2回目: 同じファイルを再度 setSource し offset=15000 で再生
       play() 直後                → position = 15000 ms
       0.8秒後                    → position = 15597 ms   ← seek は正しく効いている
```

音声は戻した位置から正しく鳴る。**真因は §2.3 の `_last_frame_at` 単独**である。

### 2.8 影響を受けない経路（意図的に確認した）

| 経路 | 理由 |
|---|---|
| 無音フォールバック再生 `_start_silent_playback()` `preview_panel.py:674` | QTimer から `set_playhead()` を直接呼び、`_on_audio_position` を経由しない |
| チャンク終端での継ぎ足し `_on_audio_status()` EndOfMedia | 次チャンクは**必ず前方**のため差が正になる |
| スクラブ中の音（`_scrub_tick`） | `_rate == 0` のまま再生ヘッドを動かさない設計（resolve7 §2.5.2）。もともと `_on_audio_position` を通らない |

---

## 3. 方式選定

### 3-1. 直し方の比較

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | 再生開始・停止の時点で `_last_frame_at` を**リセット**する | 間引き機構（E4）を保ったまま、状態の持ち越しだけを断つ。変更が局所 |
| B | 判定を `abs(sec - self._last_frame_at) >= min_interval` に変える | 巻き戻りは通るが、**巻き戻り方向にも間引きが効いてしまう**。かつ「最後に更新した秒」という変数の意味が壊れる |
| C | `set_playhead()` を毎通知で呼び、間引きをやめる | E4 に反する。resolve6 §3-9 の設計（音声優先）を捨てることになる |
| D | 再生ヘッドが動くたび（`_on_playhead_moved`）に `_last_frame_at` を同期する | 再生中も毎フレーム呼ばれる経路であり、自分で自分を書き換える循環になる。間引きが常に素通りする |

**A を採用する。** 不具合の本質は「間引き用の状態が再生セッションを跨いで持ち越されること」であり、
セッション境界でリセットするのが最小かつ正しい。

### 3-2. リセットは「再生開始」と「停止」の 2 点で行う

* **`_start_playback()`（`preview_panel.py:661`）** —— 前進再生の**唯一の合流点**。
  `_play_forward()` / `_on_chunk_ready()` / `_on_audio_status()` の 3 経路が必ずここを通る。
  ここ 1 箇所で E2 は満たせる。
* **`pause()`（`preview_panel.py:685`）** —— 保険。
  逆再生（§2.6-3）やスクラブで戻したあとの残留値を確実に断つ。
  `toggle_rate()` は再生の切り替え前に必ず `pause()` を通るため、二重の安全網になる。

片方だけでも動くが、**両方入れる**。経路が 3 つある（E3）以上、合流点 1 点に賭けない。

### 3-3. リセット値は「番兵 `None`」とする（E5）

`self._last_frame_at = playhead - 1.0` のような書き方は、
`1.0` の根拠が式に埋まった**ハードコード**であり `docs/claude.md` に反する。
`playhead - min_interval` と書けば設定由来にはなるが、
「1 通知目を必ず通す」という意図が式から読み取れない。

そこで **`None` を「まだ 1 度も更新していない」の番兵**として使う。
意図がそのまま型に出るうえ、新しい設定項目も定数も増えない（要望どおり）。

---

## 4. 設計方針

1. `_last_frame_at` の意味を **「最後に映像を更新した秒。`None` = このセッションでまだ未更新」** に拡張する。
2. `None` のときは **間引き判定を素通りさせ、必ず 1 通知目で `set_playhead()` を呼ぶ**。
3. 再生開始（`_start_playback`）と停止（`pause`）で `None` へ戻す。
4. **`play_fps` による間引きの挙動は変えない**（E4）。2 通知目以降は従来どおり。
5. 設定項目・定数を増やさない（E5）。

---

## 5. 詳細設計

### 5.1 変更ファイル一覧

| ファイル | 変更内容 | 行数 |
|---|---|---|
| `src/gui/timeline/preview_panel.py` | `_last_frame_at` の初期値と意味の変更 / `_on_audio_position` の判定 / `_start_playback`・`pause` でのリセット | 3 箇所・実質 +5 行 |
| `tests/test_preview_playback.py` | 回帰テストの追加（§10-2） | 新規クラス 1 つ |

### 5.2 初期化の変更（`preview_panel.py:190`）

```python
# 現状
        self._last_frame_at = 0.0
```

```python
# 変更後
        # 最後に映像を更新した秒 (play_fps での間引き判定に使う)。
        # None = この再生セッションではまだ未更新 → 次の 1 通知は必ず通す。
        # 再生開始・停止でリセットしないと、戻して再生したとき前回の到達点を
        # 越えるまで映像が更新されない (resolve12 §2.3)。
        self._last_frame_at = None
```

### 5.3 判定の変更（`preview_panel.py:749-751`）

```python
# 現状
        min_interval = 1.0 / max(int(self._cfg["play_fps"]), 1)
        if sec - self._last_frame_at >= min_interval:
            self._last_frame_at = sec
            self._controller.set_playhead(sec)
```

```python
# 変更後
        min_interval = 1.0 / max(int(self._cfg["play_fps"]), 1)
        # 再生開始直後の 1 通知は間引かずに通す (戻して再生したときの固まり防止)
        if (self._last_frame_at is None
                or sec - self._last_frame_at >= min_interval):
            self._last_frame_at = sec
            self._controller.set_playhead(sec)
```

### 5.4 リセット箇所その 1 —— `_start_playback()`（`preview_panel.py:661`）

```python
    def _start_playback(self, path, chunk_start):
        rate = self._pending_rate or 1.0
        self._chunk_start = float(chunk_start)
        # 間引き用の到達点を捨てる。持ち越すと、前回より手前から再生を始めたときに
        # 前回の到達点を越えるまで再生ヘッドが動かない (resolve12 §2.3)
        self._last_frame_at = None
        offset_ms = int(max(self._controller.playhead() - self._chunk_start, 0.0) * 1000)
        ...
```

### 5.5 リセット箇所その 2 —— `pause()`（`preview_panel.py:685`）

```python
    def pause(self):
        if self._player is not None:
            self._player.pause()
        for name in ("_silent_timer", "_reverse_timer"):
            ...
        self._pending_rate = 0.0
        # 逆再生・スクラブで再生ヘッドが戻された場合の残留値も断つ (resolve12 §2.6)
        self._last_frame_at = None
        self._set_rate(0.0)
```

### 5.6 変わらないもの（意図的に触らない）

| 対象 | 理由 |
|---|---|
| `play_fps` による間引きそのもの | 2 通知目以降は完全に従来どおり。音声優先の設計（resolve6 §3-9）を維持（E4） |
| `_start_playback` の `setSource` → `setPosition` → `play` の順序 | §2.7 で問題ないことを実機確認済み。触る理由が無い |
| `AudioChunkSource` 一式 | 音声は正しい位置から鳴っている。原因ではない |
| スクラブ（`_scrub_tick` ほか） | `_rate == 0` を保つ設計（resolve7 §2.5.2）は正しい。据え置き |
| `setting.json` / `settings_window.py` | 新しい設定値は増やさない（E5） |

---

## 6. 実装手順

1. `preview_panel.py:190` の初期値を `None` にし、コメントを §5.2 のとおり書き換える。
2. `preview_panel.py:749-751` の判定に `is None` の分岐を足す（§5.3）。
3. `_start_playback()` にリセットを 1 行足す（§5.4）。
4. `pause()` にリセットを 1 行足す（§5.5）。
5. `tests/test_preview_playback.py` に回帰テストを追加する（§10-2）。
6. 実機で §10-1 の手順を確認する。

---

## 7. setting.json 定義（追加分）

**無し。** 新しい設定項目は増やさない。既存の `timeline.preview.play_fps`（既定 15）の
意味も変えない。

---

## 8. 互換性・非破壊の担保

| 観点 | 影響 |
|---|---|
| 保存形式（`*.timeline.json`） | 変更なし |
| Timeline モデル・コマンド・Undo/Redo | 変更なし |
| レンダラ・出力（焼き込み結果） | 変更なし。プレビュー表示のみの修正 |
| 初回再生の挙動 | 変わらない（従来も 1 通知目を通っていた） |
| 前方へ動かしてからの再生 | 変わらない |
| 再生中の間引き | 変わらない（2 通知目以降は同一の式） |
| 無音フォールバック再生 | 経路が異なるため影響なし（§2.8） |
| QtMultimedia が無い環境 | `_player is None` の分岐は従来どおり |

`_last_frame_at` は `PreviewPanel` の private 属性であり、
外部から参照している箇所は無い（`grep` で確認済み・出現は 3 箇所すべて同ファイル内）。

---

## 9. 確認事項

| # | 内容 | 本書の判断 |
|---|---|---|
| Q1 | 停止中に再生ヘッドを動かしたとき、映像は今でも即座に追従するか | する。停止中は `_on_playhead_moved` が直接 `_request_frame()` を呼ぶため、間引き判定を通らない（`preview_panel.py:356`）。今回の不具合は**再生開始後**にだけ出る |
| Q2 | 「1 通知目を必ず通す」ことで再生開始時の負荷が増えないか | 増えない。従来も初回再生では 1 通知目を通していた。1 フレーム取得が 1 回増えるだけ |
| Q3 | `pause()` でのリセットは冗長ではないか | 冗長だが残す。戻す経路が 3 つあり（§2.6）、合流点 1 点に依存させない |
| Q4 | 逆再生中に映像が飛び飛びになる問題は別に無いか | 逆再生は `reverse_play_fps`（既定 8）で `set_playhead()` を直接呼ぶ別経路。本書の対象外 |

---

## 10. テスト計画

### 10-1. 実機確認

| # | 手順 | 期待 |
|---|---|---|
| 1 | Timeline 編集画面を開き ▶ で 20 秒ほど再生 → 停止 | 正常に再生される（従来どおり） |
| 2 | 再生ヘッドをドラッグで 5 秒ぶん**手前**へ戻し ▶ | **その位置から即座に**映像・再生ヘッド・時刻が動き出す |
| 3 | 頭（0 秒）まで戻して ▶ | 即座に動き出す（修正前は全長ぶん固まる） |
| 4 | ◀◀（倍速逆再生）で戻したあと ▶ | 即座に動き出す |
| 5 | 再生ヘッドを**前方**へ動かして ▶ | 従来どおり正常（退行が無いこと） |
| 6 | 再生を最後まで流す（チャンクを跨ぐ） | 継ぎ目で止まらない（`_on_audio_status` 経路の退行が無いこと） |
| 7 | 再生中の映像がコマ落ちしていないか | `play_fps` の間引きが従来どおり効いている |

### 10-2. 単体テスト（`tests/test_preview_playback.py` へ追加）

`PreviewPanel._on_audio_position` を未改変のまま、`self` をスタブに差し替えて検証する
（`QApplication` も音声デバイスも要らない）。

| # | テスト | 検証内容 |
|---|---|---|
| 1 | `test_replay_after_rewind_moves_playhead` | `_last_frame_at=None` で 15s から再生 → **1 通知目で** `set_playhead(15.0)` が呼ばれる |
| 2 | `test_stale_last_frame_at_does_not_freeze` | 20s まで再生した状態から 15s へ戻して再生 → 沈黙が `min_interval` 以内（回帰の本体） |
| 3 | `test_frame_thinning_still_applies` | 連続通知では `play_fps` ぶんに間引かれる（E4 の退行防止） |
| 4 | `test_start_playback_resets_last_frame_at` | `_start_playback()` 後に `_last_frame_at is None` |
| 5 | `test_pause_resets_last_frame_at` | `pause()` 後に `_last_frame_at is None` |

修正前後のシミュレーション実測値（15s へ戻して 6 秒ぶん再生）:

```
修正前 : 更新 10 回 / 最初に動いた位置 20.1s / 無反応 5.1s
修正後 : 更新 60 回 / 最初に動いた位置 15.1s / 無反応 0.1s
```

### 10-3. 既存テストの回帰

```
python -m unittest discover -s tests
```

`tests/test_preview_playback.py` の既存クラス
（`TestFrameSourceConfig` / `TestAudioChunkSource` / `ScrubStateTest` / `ScrubConfigTest`）が
すべて通ること。今回の変更はこれらが触る範囲に入らない。

---

## 11. 将来拡張（本書では実装しない）

| # | 内容 | 備考 |
|---|---|---|
| F1 | 停止中も再生ヘッド移動をデバウンスして間引く | `update_debounce_ms` が設定にあるが `_on_playhead_moved` では未使用。高速ドラッグ時の負荷対策として別途検討 |
| F2 | 逆再生でも音を鳴らす | QMediaPlayer が負のレートに非対応（resolve2 §5.6-3）。別方式が要る |
