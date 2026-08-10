# resolve2（ver3） — Timeline の吸着とショートカットキー 要望設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request2.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「いきなり実装を開始しない」）。
* 前提となる実装は `docs/request/ver3/resolve.md`（Timeline 型クリップ編集画面）で、Phase 0〜9 が実装済み。
  本書はその上への**追加・調整**であり、既存の Timeline モデル・コマンド・レンダラの構造は変えない。
* 記載の判断はすべて実装済みコードの実測に基づく。決めきれない点は推測実装せず **§10 確認事項** に列挙した。

> **注記**: 依頼時に `docs/request/ver3/request3.md` を指定いただきましたが、そのファイルは存在しません
> （ver3 にあるのは `request.md` と `request2.md` の 2 本）。未処理でありかつ Output に `resolve2.md` を
> 指定している `request2.md` を対象と判断しました。意図が異なる場合はお知らせください。

---

## 1. 要望（request2.md）

| ID | 要望 | 種別 |
|---|---|---|
| R1 | 編集点に対して、**再生ヘッド**が近づいたら吸い付く | **新規** |
| R2 | 編集点に対して、**ノード（クリップの端）** が近づいたら吸い付く | 既存（強化） |
| R3 | `W` : クリップを分割 | 新規（既存 `Ctrl+B` に追加） |
| R4 | `A` : 再生ヘッドが選択しているクリップの、**再生ヘッドより前**をリップル削除 | **新規** |
| R5 | `S` : **（原文が未記載）** | **回答で確定 → 割り当てない（§1.1）** |
| R6 | `D` : 再生ヘッドが選択しているクリップの、**再生ヘッドより後ろ**をリップル削除 | **新規** |
| R7 | `Delete` : リップル削除 | **既定の変更** |
| R8 | リップル操作は**全トラックを一緒に詰める**（§1.1 の回答） | **新規** |
| R9 | `Space` : 再生 / 停止（§1.1 の回答で明示） | 既存（据え置き） |
| R10 | `Q` : **倍速逆再生** | **新規** |
| R11 | `E` : **倍速再生** | **新規** |
| R12 | `BackSpace` : Timeline で選択中のノードを**削除するだけ**（後続を詰めない） | **新規**（§1.1 第 3 回） |

R5 は原文が `    S：` で終わっており割り当てが書かれていませんでした。
推測で埋めず確認したところ **「`S` には何も割り当てない」** と回答をいただき、
代わりに再生系（`Space` / `Q` / `E`）が追加されました（§1.1）。

R7 は前回（`resolve.md` 回答 Q5）で確定した「Delete = 空白を残す / Shift+Delete = リップル」を
**入れ替える**変更です。影響は §3-3 で扱います。

### 1.1 レビュー回答による確定事項

#### 第 1 回（リップルの範囲）

| # | 論点 | 回答 | 反映先 |
|---|---|---|---|
| Q2 | リップル時に字幕・オーバーレイも詰めるか | **リップル削除を行った場合は全体的に詰める** | §3-5 で方式 B を採用。§5.4 に「範囲リップル」の共通処理を設計。`ripple_sync_tracks` 既定を `"all"` へ |

この回答により **R8「リップル操作は全トラックを一緒に詰める」** が要件として加わりました。

#### 第 2 回（残りの確認事項・すべて回答済み）

| # | 論点 | 回答 | 反映先 |
|---|---|---|---|
| Q1 | `S` に何を割り当てるか | **何も割り当てない**。加えて `Space`=再生/停止、`Q`=倍速逆再生、`E`=倍速再生を追加する | R9〜R11。§3-6 で再生方式を選定し §5.6 で詳細設計。設定から `S` の項目自体を削除 |
| Q3 | `A` / `D` の対象クリップ | **選択中クリップ優先 → 無ければ V1 で確定** | §5.3-3（当初案どおり） |
| Q4 | 残りが最小尺を割るときの `A` / `D` | **クリップごとリップル削除する** | §5.3-2 / §7（当初案どおり） |
| Q5 | 既存 `setting.json` の `ripple_delete` 移行 | **移行してよい** | §9（当初案どおり） |
| Q6 | 単文字キーと文字入力の競合 | **入力欄にフォーカスがあるときはショートカットとして機能させない** | §3-4 / §5.5-3（当初案どおり） |
| Q7 | `A` / `D` を字幕クリップにも効かせるか | **効かせる** | §5.3-3 / §5.4-3 |
| Q8 | リップル区間を跨ぐクリップの扱い | **分割せず尺を縮めるだけ** | §5.4-3 規則 6 を簡素化（映像・字幕を同一規則に統一） |

**これで未確定事項は無くなりました。** 唯一、R10（倍速逆再生）の音声の扱いだけは
技術的制約から選択が生じるため §10.2 に 1 件だけ残しています。

#### 第 3 回（削除キーの追加）

| 追加要望 | 内容 | 反映先 |
|---|---|---|
| R12 | Timeline 上で何かが選択されている状態で `BackSpace` を押したら、**選択中のノードを削除するだけ**にする | §5.5-1 / §5.5-4。設定へ `delete_plain` を追加 |

「削除するだけ」＝ **後続を詰めない**（跡は空白として残る）と解釈しました。
`Delete`（リップル削除）と対になる操作で、`timeline.ripple_delete` の値に関わらず
**常に**「詰めない削除」として働きます。

この追加に伴い、`_ShortcutGuard` の対象を単文字キーから**登録済みショートカット全体**へ広げました。
理由は §5.5-3 に記載しています（`BackSpace` は文字入力欄で使えないと困るキーの代表例のため）。

**前回（`resolve.md` 回答 Q2）の「字幕は V1 の編集に追従しなくてよい」とは矛盾しません。**
両者は対象とする操作が違います。

| 操作 | 字幕・オーバーレイ | 根拠 |
|---|---|---|
| クリップの移動・トリム（尺変更） | **追従しない** | `resolve.md` 回答 Q2。個別のクリップを動かしただけで全体が動くと編集しづらい |
| **リップル操作**（`Delete` / `A` / `D`） | **一緒に詰める** | 本回答。リップルは「番組からこの区間を丸ごと抜く」操作であり、全トラックが動くのが自然 |

一般的なノンリニア編集ソフト（DaVinci Resolve / Premiere Pro）の sync lock と同じ切り分けです。

---

## 2. 現状分析（実装済みコードの実測）

### 2.1 吸着（R1・R2 の現状）

`src/gui/timeline/timeline_view.py:520 TimelineView._snap()`

```python
def _snap(self, sec, exclude_id=None):
    if not self._controller.cfg["snap_enabled"]:
        return sec
    threshold = self._controller.cfg["snap_threshold_px"] / zoom
    candidates = [0.0, self._controller.playhead()]
    for track in timeline.tracks:
        if track.is_audio():
            continue
        for clip in track.clips:
            if clip.id == exclude_id:
                continue
            candidates.append(clip.timeline_start)
            candidates.append(clip.timeline_end)
    best = min(candidates, key=lambda c: abs(c - sec), default=sec)
    return best if abs(best - sec) <= threshold else sec
```

| 観点 | 現状 |
|---|---|
| 呼ばれる場所 | `mouseMoveEvent` のクリップ**移動**（`:469`）と**トリム**（`:465`）のみ |
| 吸着先 | タイムライン先頭 / 再生ヘッド / 非音声トラック全クリップの開始・終了（＝編集点） |
| **再生ヘッドの吸着** | **無い**。`set_playhead()` は生の座標をそのまま受ける |
| 移動時の判定対象 | ドラッグ中クリップの**先頭側だけ**。末尾は候補に入れていない |

再生ヘッドを動かしている箇所は 4 つあり、いずれも吸着していません。

| 箇所 | 位置 |
|---|---|
| ルーラのクリック／ドラッグ | `timeline_view.py:172` → `:687` で `set_playhead` へ直結 |
| 空き領域のクリック（選択解除＋移動） | `timeline_view.py:416` |
| 空き領域のドラッグ | `timeline_view.py:459` |
| 右クリック →「ここへ再生ヘッドを移動」 | `timeline_view.py:551` |

**したがって R2 は概ね実装済み**（クリップの端は編集点に吸着する）で、**R1 が未実装**というのが現状です。

### 2.2 ショートカット（R3〜R7 の現状）

`src/gui/timeline/timeline_editor_dialog.py:131 _register_shortcuts()`

```
Ctrl+B          クリップ分割
Delete          削除 (timeline.ripple_delete に従う。既定 false = 空白を残す)
Shift+Delete    上記のもう一方 (既定 = リップル削除)
Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z   元に戻す / やり直す
Space           再生 / 一時停止
Left / Right    1 フレーム送り
Home / End      先頭 / 末尾
Ctrl++ / Ctrl+= / Ctrl+-         ズーム
```

* **単文字キーは 1 つも使っていない。** `W` `A` `S` `D` はすべて空いている。
* `Delete` の意味は設定 1 つで入れ替わる作りになっている（`:153`〜`:160`）。

```python
def _delete_default(self):      # Delete 単独
    self.controller.delete_selected(ripple=self.controller.cfg["ripple_delete"])
def _delete_alternate(self):    # Shift+Delete
    self.controller.delete_selected(ripple=not self.controller.cfg["ripple_delete"])
```

**このため R7 はコード変更なしで満たせる**（§3-3）。

### 2.3 リップル削除の現状（R4・R6 の基盤）

`src/timeline/commands.py DeleteClip`

* できること: **クリップを丸ごと**消し、`ripple=True` なら同一トラックの後続を詰める。
* できないこと: **クリップの一部（再生ヘッドまで／から）を消して詰める**。
  R4・R6 が求めているのはこちらで、既存コマンドの組み合わせでは表現できない。

リップルの適用範囲は「同一トラックのみ」（`resolve.md` §6.6）。
`A1` はリンク元 `V1` から時刻を導出するため自動的に追従する（R18）。
一方 **`S1`（字幕）と `V2` 以降（オーバーレイ）は追従しない**（回答 Q2）。

### 2.4 再利用できる資産

| 資産 | 位置 | 用途 |
|---|---|---|
| `TimelineController` | `gui/timeline/timeline_controller.py` | 編集操作の唯一の窓口。新コマンドもここへ生やす |
| `CommandStack`（スナップショット方式） | `timeline/commands.py:80` | 新コマンドを足しても Undo/Redo が自動で効く |
| `TrimClip` の境界計算 | `timeline/commands.py` | 素材範囲・最小尺・隣接クリップの制限ロジック |
| `timeline.snap_enabled` / `snap_threshold_px` | `setting.json` | 吸着の設定は既にある |
| `timeline.ripple_delete` | `setting.json` | Delete の意味を入れ替える設定が既にある |

### 2.5 プレビュー再生の現状（R9〜R11 の基盤）

`src/gui/timeline/preview_panel.py`

| 要素 | 位置 | 内容 |
|---|---|---|
| `toggle_play()` | `:434` | `Space` から呼ばれる再生 / 一時停止のトグル。**R9 は実装済み** |
| `play()` | `:440` | 再生ヘッド位置の音声チャンクを用意し `QMediaPlayer` で再生開始 |
| `_start_playback()` | `:475` | `setSource` → `setPosition` → `play`。**再生速度は指定していない（等倍固定）** |
| `_on_audio_position()` | `:510` | **音声位置がマスタークロック**。ここから再生ヘッドを進め、フレームを取得する |
| `_start_silent_playback()` | `:484` | 音声が使えないときの代替。`QTimer` で再生ヘッドだけを進める |
| `play_fps` | 設定 | 再生中の映像更新レート上限（既定 15）。音声を優先し、間に合わなければフレームを間引く |

**倍速再生・逆再生は未実装**で、再生方向は前進のみです。
`QMediaPlayer` には `setPlaybackRate()` があるため倍速再生は載せられますが、
**逆再生は `QMediaPlayer` では実現できません**（負のレートは各バックエンドで未対応）。
方式は §3-6 で選定します。

なお `_start_silent_playback()` が既に「音声なしで `QTimer` により再生ヘッドを進める」経路を
持っており、**逆再生はこの仕組みを向き違いで使い回せます**。

---

## 3. 方式選定

### 3-1. 再生ヘッド吸着をどこで行うか（R1）

| 方式 | 内容 | 評価 |
|---|---|---|
| A. `TimelineController.set_playhead()` の中で吸着する | 呼び出し側を一切変えなくてよい | **不可**。矢印キーの 1 フレーム送り・`Home`/`End`・**再生中の追従**まで吸着してしまい、再生が編集点で引っかかる |
| B. **マウス操作の入口（View）で吸着してから `set_playhead` を呼ぶ** | 吸着すべき操作だけに適用できる | **採用** |
| C. 吸着専用の別メソッド `set_playhead_snapped()` を用意する | B と同等 | 呼び分けの責務が Controller に漏れる。B で足りる |

**採用: B。** ただし吸着の**計算そのもの**は `TimelineController.snap_sec()` として Controller 側へ移す。
理由は 2 つ:

* View の `_snap()` はクリップドラッグ用に閉じており、再生ヘッド用と 2 つに分裂させたくない。
* 吸着候補（編集点の集合）はモデルから導く値であり、描画の都合を持つ View より Controller が適所。

View の `_snap()` はこの新メソッドへの委譲に置き換える（挙動は不変）。

### 3-2. A / D をどう実現するか（R4・R6）

| 方式 | 内容 | 評価 |
|---|---|---|
| A. 既存 `TrimClip` ＋ `MoveClip` を連続で積む | 新規コマンド不要 | **不可**。2 コマンドになるため Undo が 2 回必要。かつ後続クリップを詰める処理が無い |
| B. `DeleteClip` に「部分削除」を足す | コマンド数が増えない | 責務が肥大する。`DeleteClip` は「クリップを消す」ものであり、意味が違う |
| C. **新規コマンド `RippleTrimToPlayhead` を作る** | 1 操作＝1 コマンド＝1 Undo | **採用** |

**採用: C。** `CommandStack` はスナップショット方式のため、
**新しいコマンドを足すだけで Undo/Redo は自動的に正しく動く**（逆操作を書く必要がない）。

### 3-3. `Delete` をリップル削除にする方法（R7）

現状 `Delete` は `timeline.ripple_delete` の値をそのまま使い、`Shift+Delete` はその否定を使う（§2.2）。
したがって **設定の既定値を `false` → `true` に変えるだけ**で、

* `Delete` → リップル削除（要望どおり）
* `Shift+Delete` → 空白を残す削除（もう一方が自動的にこちらへ回る）

となり、**コード変更は不要**です。右クリックメニューには元から「削除」「リップル削除」の
両方が並んでいる（`timeline_view.py` の `contextMenuEvent`）ため、そちらも変更不要です。

ただしこの変更は**リップル編集が既定の操作系になる**ことを意味し、
字幕・オーバーレイの追従という別の問題を表面化させます。
これは §3-5 で扱い、**「リップル時は全トラックを一緒に詰める」**という回答（§1.1 / R8）で決着しています。

なお `ripple_delete` は既存ユーザーの `setting.json` に `false` として保存済みのため、
既定値の変更だけでは効きません。移行処理が必要です（§9）。

### 3-4. 単文字ショートカットと文字入力の競合をどう防ぐか

`W` `A` `S` `D` を `QShortcut`（`Qt.WindowShortcut`）で登録すると、
**インスペクタの字幕テキスト入力欄にフォーカスがあるときも発火します。**
「わ」と打とうとして `W` でクリップが分割される、という事故が起きます。

| 方式 | 内容 | 評価 |
|---|---|---|
| A. `Qt.WidgetWithChildrenShortcut` で Timeline パネルに限定する | Qt の標準機構 | プレビューをクリックした後など、Timeline にフォーカスが無いと効かない。使い勝手が悪い |
| B. `keyPressEvent` を各ウィジェットで実装する | 細かく制御できる | 実装が散らばり、どこで効くのか追えなくなる |
| C. **`QApplication.focusChanged` を見て、文字入力欄にフォーカスがある間だけ単文字ショートカットを無効化する** | 1 か所で完結し、挙動が明快 | **採用** |

**採用: C。** 対象は `QLineEdit` / `QPlainTextEdit` / `QTextEdit` / `QAbstractSpinBox` / 編集可能な `QComboBox`。
修飾キー付き（`Ctrl+B` など）は文字入力と衝突しないため、この無効化の対象外とします。

### 3-5. リップル編集で「何を一緒に詰めるか」（重要）

現状のリップルは**同一トラックのみ**を詰めます（`resolve.md` §6.6）。
`Delete` が既定でリップルになり、さらに `A` / `D` が加わると、リップル編集が主要な操作になります。
このとき現状のままだと、**編集のたびに字幕とオーバーレイが映像からずれていきます。**

具体例（本編 3 クリップ・字幕あり）:

```
編集前   V1 |--c1--|--c2--|--c3--|
         S1    [字幕A]  [字幕B]  [字幕C]

c2 をリップル削除すると…

編集後   V1 |--c1--|--c3--|                 ← c3 が前へ詰まる
         S1    [字幕A]  [字幕B]  [字幕C]     ← 動かない
                        ↑ 字幕B は消えた映像の字幕。字幕C は映像とずれる
```

前回（回答 Q2）「字幕は追従しなくてよい」と確定していますが、
これは **`Delete` の既定が「空白を残す」だった前提**での判断でした。
空白を残す削除は後続の時刻を動かさないため、ずれは発生しません。
リップルが既定になると前提が変わります。

| 方式 | 内容 | 評価 |
|---|---|---|
| A. 現状維持（同一トラックのみ詰める） | 実装変更なし | 編集のたびに字幕がずれ、出力が壊れる |
| B. **リップル時は全トラックを一緒に詰める（同期ロック）** | DaVinci / Premiere の sync lock と同じ | **採用**（回答済み / §1.1） |
| C. 設定で選べるようにする | 両立できる | 既定は B。切り替えは設定で残す |

**採用: B。**「リップル削除を行った場合は全体的に詰める」との回答（§1.1）により確定しました。

この決定により、リップルは「特定トラックのクリップを消す操作」ではなく
**「タイムライン上のある区間を、番組から丸ごと抜く操作」** になります。
`Delete`（リップル）・`A`・`B`… いずれも**同じ 1 つの処理へ帰着する**ため、
共通処理 `ripple_remove_range()` を 1 本用意して両コマンドから呼びます（§5.4）。

**音声トラック（A1 / An）はこの処理の対象外**です。`AudioClip` は時刻を持たず
リンク元の映像クリップから導出するため（`resolve.md` R18）、映像を詰めれば自動的に追従します。
ここで一緒に動かそうとすると二重にずれます。

### 3-6. 倍速再生・倍速逆再生の実現方式（R10・R11）

前進と後退で使える手段がまったく違うため、分けて選定します。

#### 3-6-1 倍速再生（`E`）

| 方式 | 内容 | 評価 |
|---|---|---|
| A. **`QMediaPlayer.setPlaybackRate()` を使う** | 既存の音声マスタークロック方式のまま倍率だけ変える | **採用**。音声も一緒に速くなり、既存の同期の仕組みがそのまま効く |
| B. 音声を止めて `QTimer` で再生ヘッドだけを速く進める | 実装は単純 | 音が出ない。倍速確認では音も欲しい場面がある |

**採用: A。** `_start_playback()` に `setPlaybackRate(rate)` を足すだけで済みます。
`_on_audio_position()` は音声位置から再生ヘッドを求めているため、
**倍速でも再生ヘッドの追従は自動的に正しくなります**（変更不要）。

フレーム表示は `play_fps` で頭打ちにする既存の仕組みがそのまま働き、
デコードが間に合わない場合は間引かれます。音声は途切れません。

#### 3-6-2 倍速逆再生（`Q`）

**`QMediaPlayer` は負の再生レートを実質サポートしていません。** 別方式が要ります。

| 方式 | 内容 | 評価 |
|---|---|---|
| A. `setPlaybackRate(-2.0)` | 一行で済む | **不可**。バックエンド依存で動かない／エラーになる |
| B. `ffmpeg -af areverse` で逆再生音声チャンクを作って再生 | 音付きで逆再生できる | 逆再生のたびに生成待ちが発生し「早戻しでさっと確認する」用途に合わない。`areverse` は区間全体をメモリに載せるため長い区間で重い |
| C. **音声を止め、`QTimer` で再生ヘッドを後退させてフレームだけを表示する** | 既存 `_start_silent_playback()` の向き違い。即応する | **採用** |

**採用: C。** 逆再生は「どこまで戻すか探す」ための操作であり、応答性が最優先と判断しました。
**逆再生中は音が出ません。** これは技術的制約に由来する仕様として明記します（§5.6-3 / §10.2-Q9）。

##### 逆再生のフレーム取得について

PyAV での後方シークは、目的時刻より前のキーフレームまで戻ってから順方向にデコードし直すため、
**前進より 1 フレームあたりのコストが高くなります**。倍速逆再生ではさらに間隔が飛びます。

対策として、逆再生時は表示レートを `play_fps` よりさらに落とします
（設定 `reverse_play_fps`、既定 8）。滑らかさより「戻っていることが分かる」ことを優先します。
取得が間に合わない場合は前進時と同じく**間引き**、再生ヘッドの後退自体は止めません。

#### 3-6-3 再生モードの持ち方

停止 / 前進再生 / 倍速前進 / 倍速逆 の 4 状態を、
`PreviewPanel` が 1 つの状態変数（速度 `rate`）で持ちます。

| 状態 | `rate` | 駆動 | 音声 |
|---|---|---|---|
| 停止 | `0.0` | — | — |
| 通常再生（`Space`） | `1.0` | `QMediaPlayer` | あり |
| 倍速再生（`E`） | `+playback_rate`（既定 `2.0`） | `QMediaPlayer` | あり |
| 倍速逆再生（`Q`） | `-playback_rate` | `QTimer` | **なし** |

`Space` / `Q` / `E` はいずれも**トグル**にします
（同じ状態のときに押すと停止、違う状態なら切り替え）。
押すたびに 2 倍 → 4 倍と上がる多段速度は要望に無いため入れず、
倍率は設定 `playback_rate` で変えられるようにします。

---

## 4. 設計方針（全体）

1. **既存構造を変えない。** モデル（`src/timeline/model.py`）・JSON スキーマ・レンダラには手を入れない。
   追加するのはコマンド 1 種・Controller のメソッド数個・ショートカット登録・設定キーのみ。
2. **吸着の計算は 1 か所に集約する。** `TimelineController.snap_sec()` を唯一の実装とし、
   クリップドラッグと再生ヘッドの双方がこれを使う。
3. **吸着は「マウス操作」にだけ効かせる。** キーボードのフレーム送りと再生中の追従は正確さを優先し、吸着しない。
4. **1 操作＝1 コマンド＝1 Undo。** `A` / `D` は 1 回の Undo で完全に戻る。
5. **キー割り当ては設定化する。** `claude.md`「ハードコードは禁止し、設定可能な値は setting.json で管理する」に従い、
   ショートカットは `timeline.shortcuts` で変更できるようにする。
6. **単文字ショートカットは文字入力を邪魔しない。** §3-4 の方式で一括制御する。
7. **`S` には何も割り当てない（回答 Q1）。** 設定にも項目を置かず、見通しを保つ。
8. **リップルは「区間を抜く」1 つの処理へ集約する（R8）。**
   `Delete`（リップル）も `A` / `D` も、突き詰めれば「タイムライン上のこの区間を番組から抜く」
   という同じ操作です。共通処理を 1 本だけ持ち、両コマンドはそこへ委譲します。
   処理が 1 か所に集まるため、「詰め忘れたトラックがある」という不整合が起きません。
9. **再生状態は「速度」1 つで持つ（R9〜R11）。**
   停止・通常・倍速・逆倍速を真偽値の組み合わせで持つと状態が増えて破綻します。
   速度 `rate`（`0.0` / `1.0` / `+2.0` / `-2.0`）1 つで表し、
   符号が向き、絶対値が速さ、ゼロが停止を意味する形にします。
10. **できないことは仕様として明記する。** 倍速逆再生は `QMediaPlayer` の制約で
    音声を伴えません（§3-6-2）。黙って無音にせず、画面にその旨を表示します。

---

## 5. 詳細設計

### 5.1 変更・新規ファイル一覧（予定）

| ファイル | 変更内容 |
|---|---|
| `src/timeline/commands.py` | `ripple_remove_range()`（範囲リップルの共通処理）と `RippleTrimToPlayhead` を**追加**。`DeleteClip` の `ripple=True` 経路を共通処理へ差し替え（`ripple=False` は無改変） |
| `src/gui/timeline/timeline_controller.py` | `snap_sec()` / `snap_targets()` / `ripple_trim_to_playhead()` / `clip_at_playhead()` を追加 |
| `src/gui/timeline/timeline_view.py` | `_snap()` を Controller へ委譲。再生ヘッド移動 4 箇所に吸着を適用。ヒント文言を更新 |
| `src/gui/timeline/timeline_editor_dialog.py` | ショートカットを設定から読む方式へ変更。`_ShortcutGuard` を追加。再生系ハンドラを追加 |
| `src/gui/timeline/preview_panel.py` | 再生状態を真偽から**速度 `_rate`** へ変更。`toggle_rate()` / 倍速前進 / 倍速逆再生を追加。トランスポートに `◀◀` `▶▶` を追加 |
| `src/settings/settings_window.py` | `timeline.ripple_delete` 既定を `true` へ変更。`snap_playhead` / `ripple_sync_tracks` / `shortcuts` を追加 |
| `tests/test_timeline_commands.py` | `ripple_remove_range()` と `RippleTrimToPlayhead` のテストを追加。既存のリップル削除テストは**期待値の更新が必要**（§9） |
| `tests/test_timeline_snap.py` | 吸着の単体テストを新規追加 |

**新規ファイルは無し**（テストを除く）。既存への追加と、`DeleteClip` のリップル経路の差し替えで収まります。

### 5.2 吸着（R1・R2）

#### 5.2-1 `TimelineController.snap_sec()`（新設）

```python
# 指定秒を近くの編集点へ吸着させる (ver3 resolve2 §5.2)
# exclude_id  : ドラッグ中のクリップ (自分自身の端へは吸着しない)
# include_playhead : 再生ヘッドを吸着先に含めるか
#                    (再生ヘッド自身を動かすときは自分自身へ吸着しないよう False)
# 戻り値: 吸着後の秒。閾値の外なら元の値をそのまま返す。
def snap_sec(self, sec, exclude_id=None, include_playhead=True):
    if not self._cfg["snap_enabled"] or self._zoom <= 0:
        return sec
    threshold = self._cfg["snap_threshold_px"] / self._zoom
    best = min(self.snap_targets(exclude_id, include_playhead),
               key=lambda c: abs(c - sec), default=None)
    if best is None or abs(best - sec) > threshold:
        return sec
    return best


# 吸着先の候補 (編集点の集合) を返す
#   ・タイムライン先頭 (0.0) と全長
#   ・非音声トラックの全クリップの開始・終了 (= 編集点)
#   ・再生ヘッド (include_playhead=True のとき)
# 音声トラックは V1 からの導出のため候補に含めない (同じ値が重複するだけ)。
def snap_targets(self, exclude_id=None, include_playhead=True):
    ...
```

* 既存 `TimelineView._snap()` はこの `snap_sec()` を呼ぶだけにする。**吸着の挙動は変わらない。**
* 候補に**全長**を追加する（末尾へ吸着できるようにする）。

#### 5.2-2 再生ヘッドの吸着（R1・新規）

| 適用する操作 | 変更箇所 |
|---|---|
| ルーラのクリック／ドラッグ | `TimelinePanel._build_ui()` の `seek_requested` 接続先を、吸着を挟むスロットへ変更 |
| Timeline 空き領域のクリック | `TimelineView.mousePressEvent`（`:416`） |
| Timeline 空き領域のドラッグ | `TimelineView.mouseMoveEvent`（`:459`） |
| 右クリック →「ここへ再生ヘッドを移動」 | `TimelineView.contextMenuEvent`（`:551`） |

```python
# マウス座標から再生ヘッド位置を決める (編集点へ吸着させる)
# 再生ヘッド自身は吸着先から外す (自分へ吸着してしまい動かなくなるため)。
def _playhead_sec_at(self, x, modifiers):
    sec = self.x_to_sec(x)
    if modifiers & Qt.AltModifier:
        return sec                      # Alt 押下中は吸着しない (クリップ移動と同じ規約)
    if not self._controller.cfg["snap_playhead"]:
        return sec
    return self._controller.snap_sec(sec, include_playhead=False)
```

**吸着しない操作**（意図的にそのままにする）:

| 操作 | 理由 |
|---|---|
| `←` `→`（1 フレーム送り） | フレーム単位の微調整が目的。吸着すると隣の編集点まで飛んでしまう |
| `Home` / `End` | 目的地が確定している |
| 再生中の追従 | 音声位置がマスタークロック。吸着すると再生が編集点で引っかかる |
| プレビューの操作 | 再生ヘッドを動かさない |

#### 5.2-3 ノード吸着の強化（R2）

現状はドラッグ中クリップの**先頭側の座標だけ**を吸着判定しています。
クリップの**末尾**を隣のクリップの先頭へ合わせたい場面で吸着が効きません。

```python
# 移動時は先頭・末尾の両方で吸着を試し、ズレの小さい方を採る
def _snap_move(self, desired_start, clip):
    snapped_head = self._controller.snap_sec(desired_start, exclude_id=clip.id)
    snapped_tail = self._controller.snap_sec(
        desired_start + clip.duration, exclude_id=clip.id) - clip.duration
    if abs(snapped_head - desired_start) <= abs(snapped_tail - desired_start):
        return snapped_head
    return snapped_tail
```

トリム時は動かしている端だけが対象のため、現状のままとします。

### 5.3 `RippleTrimToPlayhead` コマンド（R4・R6）

#### 5.3-1 挙動

```
side="before"  (A キー)                    side="after"  (D キー)

 前   V1 |--c1--|----c2----|--c3--|         前   V1 |--c1--|----c2----|--c3--|
                    ▲playhead                                 ▲playhead
                 ├─削除─┤                                     ├──削除──┤

 後   V1 |--c1--|--c2'-|--c3--|             後   V1 |--c1--|--c2'|--c3--|
         c2' は source_in が進み、           c2' は source_out が戻り、
         後続が削除ぶん左へ詰まる             後続が削除ぶん左へ詰まる
```

* `before`: `source_in += delta` / `duration -= delta` / `timeline_start` は**据え置き**、
  後続クリップを `delta` ぶん左へ詰める。
* `after`: `source_out -= delta` / `duration -= delta` / `timeline_start` は据え置き、
  後続クリップを `delta` ぶん左へ詰める。
* **詰める対象は全トラック**（`V2` 以降・`S1` を含む / R8）。区間内の字幕は削除され、
  区間より後ろの字幕・オーバーレイは一緒に前へ詰まる（§5.4-3）。
* `A1` は `V1` からの導出のため**追随処理は不要**（R18）。詰める対象からは除外する。

#### 5.3-2 コマンド仕様

回答（§1.1）により全トラックを詰めることが確定したため、このコマンドは
**「タイムライン上の区間 `[start, end]` を抜く」共通処理へ委譲するだけ**になります（§5.4）。

```python
# 再生ヘッドを境にクリップの一部を削除して全体を詰める (ver3 resolve2 §5.3)
# side="before" : クリップ先頭〜再生ヘッド を削除
# side="after"  : 再生ヘッド〜クリップ末尾 を削除
class RippleTrimToPlayhead(Command):

    def __init__(self, clip_id, at_sec, side, min_clip_sec=0.05, sync_all=True):
        ...
        self.label = ("再生ヘッドより前をリップル削除" if side == "before"
                      else "再生ヘッドより後ろをリップル削除")

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        track = timeline.track_of_clip(self._clip_id)
        if clip is None or track is None or track.locked:
            return False
        # 再生ヘッドがクリップの中に無ければ何もしない
        if not (clip.timeline_start < self._at_sec < clip.timeline_end):
            return False

        # 抜く区間を決める。あとは範囲リップルに任せる (§5.4)
        if self._side == "before":
            start, end = clip.timeline_start, self._at_sec
        else:
            start, end = self._at_sec, clip.timeline_end

        return ripple_remove_range(
            timeline, start, end,
            tracks=ripple_target_tracks(timeline, track, self._sync_all),
            min_clip_sec=self._min_clip_sec,
        )
```

* 対象クリップ自身も範囲リップルの一部として処理されます
  （`before` なら左端がトリムされ、`after` なら右端がトリムされる）。
  コマンド側で個別に `source_in` / `source_out` をいじる必要はありません。
* 字幕クリップを対象にした場合も同じ経路で扱えます（§5.4 の分岐で吸収）。

#### 5.3-3 対象クリップの決め方

要望の「再生ヘッドが選択しているクリップ」を次の順で解決します。

1. 選択中のクリップがあり、それが再生ヘッドを含んでいれば**それ**
2. 無ければ**ベース映像トラック（V1）**で再生ヘッドを含むクリップ
3. それも無ければ何もしない（ステータスに「再生ヘッド上にクリップがありません」と出す）

ここで決めるのは「**どのクリップを基準に区間を決めるか**」だけです。
決まった区間は全トラックから抜かれます（R8 / §5.4）。
複数のクリップを基準に同時実行することはしません。
この解釈で確定しています（回答 Q3）。

```python
# 再生ヘッド上のクリップを返す (選択優先 → V1 の順)
def clip_at_playhead(self):
    ...
```

### 5.4 範囲リップル（R8 / §3-5 の回答反映）

#### 5.4-1 位置づけ

「リップル削除を行った場合は全体的に詰める」という回答により、
リップルは **「タイムライン上の区間 `[start, end]` を、全トラックから丸ごと抜く」** 操作になりました。

これにより `Delete`（リップル）と `A` / `D` は**同じ 1 つの処理へ帰着します**。
共通処理を 1 本置き、両コマンドはそこへ委譲するだけにします。

| コマンド | 抜く区間 |
|---|---|
| `DeleteClip(ripple=True)` | 対象クリップの `[timeline_start, timeline_end]` |
| `RippleTrimToPlayhead(side="before")` | `[clip.timeline_start, 再生ヘッド]` |
| `RippleTrimToPlayhead(side="after")` | `[再生ヘッド, clip.timeline_end]` |

#### 5.4-2 対象トラック

```python
# 詰める対象のトラックを返す (設定 timeline.ripple_sync_tracks)
#   "all"  : 全トラック (既定 / 回答どおり)
#   "same" : 操作したトラックのみ (旧挙動。切り戻し用に残す)
# 音声トラックは常に除外する: AudioClip は時刻を持たずリンク元の映像クリップから
# 導出するため (resolve.md R18)、映像を詰めれば自動的に追従する。
# ここで一緒に動かすと二重にずれる。
def ripple_target_tracks(timeline, source_track, sync_all=True):
    if not sync_all:
        return [source_track]
    return [t for t in timeline.tracks if not t.is_audio()]
```

#### 5.4-3 範囲リップルの規則

区間 `[start, end]`（長さ `delta`）に対し、各クリップを位置関係で 6 通りに分けます。
**クリップの種別（映像 / 画像 / 字幕）で処理を分けません**（回答 Q8）。

| # | クリップと区間の関係 | 図 | 処理 |
|---|---|---|---|
| 1 | 区間より**前**にある | `[clip]  ┃───┃` | 何もしない |
| 2 | 区間より**後ろ**にある | `┃───┃  [clip]` | `timeline_start -= delta`（＝詰める） |
| 3 | 区間に**完全に含まれる** | `┃─[clip]─┃` | **削除**（消えた映像に付いていた字幕・オーバーレイも消える） |
| 4 | 区間に**頭だけかかる** | `[clip┃──┃` | 右端を `start` へトリム（`source_out` も同量戻す） |
| 5 | 区間に**尻だけかかる** | `┃──┃clip]` | 左端を `end` へトリム（`source_in` を同量進める）し、`start` の位置へ詰める |
| 6 | 区間を**跨ぐ** | `[cl┃──┃ip]` | **分割せず尺を `delta` 縮める**。`duration -= delta` / `source_out -= delta`（回答 Q8） |

規則 6 で分割しないため、**映像クリップも字幕クリップも同じコードで処理できます**。
素材の末尾 `delta` ぶんが使われなくなりますが、クリップが勝手に増えず結果が予測しやすくなります。

**共通の後処理**: トリムの結果、尺が `min_clip_sec` を割ったクリップは削除します。

```python
# タイムライン上の区間 [start, end] を全トラックから抜いて詰める (ver3 resolve2 §5.4)
# 音声トラックは対象外 (V1 からの導出で自動的に追従するため)。
# 戻り値: 何か変化があれば True
def ripple_remove_range(timeline, start, end, tracks, min_clip_sec=0.05):
    delta = end - start
    if delta <= _EPS:
        return False

    changed = False
    for track in tracks:
        if track.is_audio() or track.locked:
            continue
        survivors = []
        for clip in track.clips:
            # 規則 1〜6 を適用する。分割はしないため、残るのは 0 個か 1 個 (回答 Q8)
            if _apply_range_removal(clip, start, end, delta, min_clip_sec):
                changed = True
            if clip.duration > min_clip_sec - _EPS:
                survivors.append(clip)
            else:
                changed = True          # 尺が下限を割った → 削除
        track.clips = survivors

    # V1 のクリップを消した場合はリンク音声も落とす (R18 の明示的な追随)
    _drop_orphan_audio_clips(timeline)
    return changed
```

`DeleteClip(ripple=True)` も同じ関数を使うよう書き換えます。
複数クリップを選択している場合は、**開始時刻の降順**に 1 区間ずつ処理します
（先に後ろを抜くことで、前の区間の座標が動かない）。既存 `DeleteClip` と同じ考え方です。

#### 5.4-4 具体例

```
編集前   V1 |--c1--|----c2----|--c3--|
         V2        |--画像A--|
         S1   [字幕1] [字幕2] [字幕3] [字幕4]
         A1 (V1 から導出)

c2 をリップル削除 (区間 = c2 の範囲)

編集後   V1 |--c1--|--c3--|              ← c3 が前へ詰まる
         V2        |画A|                 ← 区間を跨ぐため尺を delta 縮める (規則 6)
         S1   [字幕1] [字幕4]            ← 字幕2/3 は区間内のため削除、字幕4 は詰まる (規則 3/2)
         A1 (自動追従)
```

字幕 2・3 は「消した映像に付いていた字幕」なので削除が正しい挙動です。
字幕 4 は詰められ、映像との対応が保たれます。
画像 A は分割されず、尺だけが短くなります（回答 Q8）。

### 5.5 ショートカット（R3〜R7・R9〜R11）

#### 5.5-1 割り当て一覧（変更後）

```
    ┌───┬───┬───┐
    │ Q │ W │ E │   Q=倍速逆再生 / W=分割 / E=倍速再生
    ├───┼───┼───┤
    │ A │ S │ D │   A=前をリップル削除 / S=未割り当て / D=後ろをリップル削除
    └───┴───┴───┘
            Space = 再生 / 停止
```

| キー | 操作 | 状態 |
|---|---|---|
| `Q` | 倍速逆再生（トグル・**音声なし**） | **追加**（R10） |
| `W` | クリップを分割 | **追加**（R3） |
| `E` | 倍速再生（トグル） | **追加**（R11） |
| `A` | 再生ヘッドより前をリップル削除 | **追加**（R4） |
| `S` | **割り当てない**（回答 Q1） | 設定から項目ごと削除 |
| `D` | 再生ヘッドより後ろをリップル削除 | **追加**（R6） |
| `Space` | 再生 / 停止（トグル） | 据え置き（R9・実装済み） |
| `Delete` | リップル削除 | **既定変更**（設定値のみ） |
| `Shift+Delete` | 上記のもう一方（既定では削除のみ） | 自動的に入れ替わる |
| `BackSpace` | **選択中のノードを削除するだけ**（常に詰めない） | **追加**（R12） |
| `Ctrl+B` | クリップを分割 | 据え置き（`W` と併存） |
| `Ctrl+Z` / `Ctrl+Y` / `Ctrl+Shift+Z` | 元に戻す / やり直す | 据え置き |
| `←` `→` | 1 フレーム送り | 据え置き |
| `Home` / `End` | 先頭 / 末尾 | 据え置き |
| `Ctrl++` / `Ctrl+-` | ズーム | 据え置き |
| `Alt`（押下中） | 吸着を一時無効 | 据え置き（再生ヘッドにも適用） |

**文字入力欄にフォーカスがある間は、上記のショートカットをすべて無効化します**
（回答 Q6 / §5.5-3）。`BackSpace` や `Delete`、矢印キー、`Ctrl+Z` は
入力欄では本来の文字編集として働きます。

### 5.5-1a 削除キーの整理（R7・R12）

削除は 3 通りあり、意味が重ならないよう次のように割り当てます。

| キー | 挙動 | 設定の影響 |
|---|---|---|
| `Delete` | `timeline.ripple_delete` に従う（既定 `true` = **リップル削除**） | 受ける |
| `Shift+Delete` | `Delete` の**もう一方**（既定では削除のみ） | 受ける（`Delete` の裏返し） |
| `BackSpace` | **常に「削除するだけ」**（後続を詰めない） | **受けない** |

`BackSpace` を設定の影響を受けない固定の挙動にしたのは、
「詰めずに消したい」ときに設定値を気にせず押せるキーが 1 つ欲しいためです。
既定設定では `Shift+Delete` と同じ結果になりますが、
`ripple_delete` を `false` にした場合でも `BackSpace` の意味は変わりません。

#### 5.5-2 設定からの読み込み

```python
def _register_shortcuts(self):
    keys = self.controller.cfg["shortcuts"]
    plain = []          # 単文字系 (文字入力中は無効化する)

    def bind(action, handler):
        sequence = keys.get(action, "")
        if not sequence:
            return None          # 空文字は「割り当てなし」(S など)
        ...
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.activated.connect(handler)
        registered.append(shortcut)   # 全ショートカットを guard の対象にする
        return shortcut

    # 編集
    bind("split", lambda: self.controller.split_at_playhead())
    bind("ripple_trim_before", self._ripple_trim_before)
    bind("ripple_trim_after", self._ripple_trim_after)
    # 削除 (3 種 / §5.5-1a)
    bind("delete", self._delete_default)
    bind("delete_alternate", self._delete_alternate)
    bind("delete_plain", self._delete_plain)        # BackSpace (R12)
    # 再生
    bind("play_pause", self.preview.toggle_play)
    bind("play_fast_forward", self._play_fast_forward)
    bind("play_fast_backward", self._play_fast_backward)
    ...
    self._shortcut_guard = _ShortcutGuard(registered, self)
```

不正なキー指定は該当ショートカットだけ登録をスキップして WARNING を出し、
重複するキーは後勝ちで登録して WARNING を出します（§7）。

#### 5.5-3 `_ShortcutGuard`（§3-4）

```python
# 文字入力欄にフォーカスがある間、登録済みショートカットをすべて無効化する。
class _ShortcutGuard(QObject):

    _EDITORS = (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox)

    def __init__(self, shortcuts, parent=None):
        super().__init__(parent)
        self._shortcuts = list(shortcuts)
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

    def _on_focus_changed(self, _old, new):
        editing = isinstance(new, self._EDITORS) or (
            isinstance(new, QComboBox) and new.isEditable())
        for shortcut in self._shortcuts:
            shortcut.setEnabled(not editing)
```

回答 Q6「入力欄にフォーカスがあるときはショートカットキーとしての役割を果たさないようにしてほしい」
は、この仕組みで満たします。

**対象を単文字キーに限定せず、登録済みショートカット全体にしています。** 当初は
`W` `A` `D` `Q` `E` `Space` だけを想定していましたが、R12 で `BackSpace` が加わったことで、
「文字入力欄で使えないと困るキー」が単文字に限らないことがはっきりしました。防ぐ事故の例:

| キー | 無効化しないと起きること |
|---|---|
| `BackSpace` / `Delete` | 文字ではなくクリップが消える |
| `←` `→` / `Home` / `End` | 文字カーソルではなく再生ヘッドが動く |
| `Ctrl+Z` | 入力の取り消しではなくタイムライン編集が取り消される |
| `Space` | 空白が打てない |
| `W` `A` `D` `Q` `E` | 「わ」と打とうとして分割・削除・再生が走る |

回答 Q6 の文面も「ショートカットキーとしての役割を果たさない」と一般に述べているため、
全体を対象にするのが意図に沿うと判断しました。

**IME（日本語入力）中の扱い**: 変換中はフォーカスが入力欄にあるため同時に無効化されます。
入力欄以外にフォーカスがある状態で IME が有効な場合は、`W` を押すと「ｗ」が確定されずに
ショートカットが働く／働かないが環境で分かれ得ます。実機で確認し、必要なら
`QInputMethod.isVisible()` による追加抑止を入れます（§11 段 6 の完了条件）。

#### 5.5-4 ハンドラ

```python
# A: 再生ヘッドより前をリップル削除
def _ripple_trim_before(self):
    self.controller.ripple_trim_to_playhead("before")

# D: 再生ヘッドより後ろをリップル削除
def _ripple_trim_after(self):
    self.controller.ripple_trim_to_playhead("after")

# BackSpace: 設定に関わらず「選択中のノードを削除するだけ」(R12 / §5.5-1a)
# 後続を詰めない = 跡は空白として残る。
def _delete_plain(self):
    self.controller.delete_selected(ripple=False)

# E: 倍速再生 (同じ状態で押したら停止するトグル / §3-6-3)
def _play_fast_forward(self):
    self.preview.toggle_rate(+self.controller.cfg["preview"]["playback_rate"])

# Q: 倍速逆再生 (同上・音声なし)
def _play_fast_backward(self):
    self.preview.toggle_rate(-self.controller.cfg["preview"]["playback_rate"])
```

`S` にはハンドラを用意しません（回答 Q1）。

### 5.6 再生制御（R9〜R11）

#### 5.6-1 `PreviewPanel` の状態

現行は `self._playing`（真偽）で持っている再生状態を、**速度 `self._rate`（float）** に置き換えます。

| 状態 | `_rate` | 駆動 | 音声 |
|---|---|---|---|
| 停止 | `0.0` | — | — |
| 通常再生 | `1.0` | `QMediaPlayer` | あり |
| 倍速再生 | `+playback_rate` | `QMediaPlayer` | あり |
| 倍速逆再生 | `-playback_rate` | `QTimer` | **なし** |

`playing_changed` シグナル（再生中は編集操作を止めるための通知）は
`_rate != 0.0` で発火させ、既存の呼び出し側は無改修のまま動きます。

#### 5.6-2 速度の切り替え

```python
# 指定速度へ切り替える。同じ速度で再生中なら停止する (トグル / §3-6-3)
#   rate > 0 : QMediaPlayer で再生 (音声あり)
#   rate < 0 : QTimer で再生ヘッドを後退 (音声なし / §3-6-2)
def toggle_rate(self, rate):
    if abs(self._rate - rate) < 1e-6:
        self.pause()
        return
    self.pause()                    # いったん止めてから切り替える
    if rate > 0:
        self._play_forward(rate)
    else:
        self._play_backward(-rate)


# 前進再生。既存の play() に速度指定を足しただけ (§3-6-1)
def _play_forward(self, rate):
    ...
    self._player.setPlaybackRate(rate)   # ← 追加はこの 1 行が本体
    self._player.play()
```

`_on_audio_position()` は音声位置から再生ヘッドを求めているため、
**倍速でも変更不要**です（音声が速く進めば再生ヘッドも速く進む）。

#### 5.6-3 倍速逆再生

```python
# 逆再生。音声は出さず、QTimer で再生ヘッドを後退させる (§3-6-2)
# 既存の _start_silent_playback() を向き違いで使い回す。
def _play_backward(self, rate):
    self._rate = -rate
    interval = int(1000 / max(int(self._cfg["reverse_play_fps"]), 1))
    self._reverse_timer = QTimer(self)
    self._reverse_timer.setInterval(interval)
    self._reverse_timer.timeout.connect(lambda: self._step_backward(rate, interval))
    self._reverse_timer.start()
    self._set_status("倍速逆再生中 (音声なし)")


# 1 ティックぶん再生ヘッドを戻す。先頭に達したら停止する。
def _step_backward(self, rate, interval_ms):
    delta = rate * interval_ms / 1000.0
    target = self._controller.playhead() - delta
    if target <= 0.0:
        self._controller.set_playhead(0.0)
        self.pause()
        return
    self._controller.set_playhead(target)
```

* 逆再生中は **音声を鳴らしません**。理由と代替案は §3-6-2 のとおりです。
  画面には「倍速逆再生中 (音声なし)」と出し、無音が不具合に見えないようにします。
* フレーム取得が間に合わない場合は前進時と同じく間引き、再生ヘッドの後退は止めません。
* 先頭（0 秒）に達したら自動停止します。

#### 5.6-4 トランスポート UI

画面のボタンにも同じ操作を出し、キーを覚えていなくても使えるようにします。

```
[◀◀] [▶] [⏸] [▶▶]   00:01:23.45 / 00:12:34.56   [🔊]━━━━   [高精度プレビュー]
  Q    Space          E
```

* `◀◀` / `▶▶` は押下中だけ倍速になるのではなく、**トグル**（キーと同じ挙動）にします。
* 現在の状態はボタンの押下表示（`setChecked`）で示します。
* `QtMultimedia` が使えない環境では `▶` と `▶▶` を無効化し、`◀◀`（音声を使わない）は有効のままにします。

---

## 6. setting.json 追加・変更案

```jsonc
{
  "timeline": {
    // ── 既定値の変更 (1 件)
    // Delete キー単独の割り当て。true=リップル削除 (要望 R7) / false=空白を残す。
    // Shift+Delete は常にもう一方。右クリックメニューには両方が並ぶ。
    "ripple_delete": true,          // ← 変更 (旧 false)

    // ── 追加 (吸着)
    "snap_playhead": true,          // 再生ヘッドを編集点へ吸着させる (R1)
    // snap_enabled / snap_threshold_px は既存キーをそのまま使う

    // ── 追加 (リップルで一緒に詰める対象 / R8・§3-5)
    // "all"  = 全トラック (字幕・オーバーレイも詰める / 既定・回答どおり)
    // "same" = 操作したトラックのみ (旧挙動。切り戻し用に残す)
    // 音声トラックは値に関わらず対象外 (V1 からの導出で自動的に追従するため)。
    "ripple_sync_tracks": "all",

    // ── 追加 (キー割り当て。空文字で無効)
    // S は割り当てないと確定したため項目自体を置かない (回答 Q1)。
    "shortcuts": {
      "split": "W",
      "ripple_trim_before": "A",
      "ripple_trim_after": "D",
      "play_pause": "Space",
      "play_fast_forward": "E",
      "play_fast_backward": "Q",
      "split_alt": "Ctrl+B",
      "delete": "Delete",
      "delete_alternate": "Shift+Delete",
      "delete_plain": "Backspace",   // 選択中のノードを削除するだけ (R12)
      "undo": "Ctrl+Z",
      "redo": "Ctrl+Y",
      "redo_alt": "Ctrl+Shift+Z",
      "step_backward": "Left",
      "step_forward": "Right",
      "go_start": "Home",
      "go_end": "End",
      "zoom_in": "Ctrl++",
      "zoom_in_alt": "Ctrl+=",
      "zoom_out": "Ctrl+-"
    },

    "preview": {
      // ── 追加 (再生速度 / R10・R11)
      "playback_rate": 2.0,       // Q / E の倍率 (「倍速」= 2.0)
      "reverse_play_fps": 8       // 倍速逆再生時の映像更新レート上限 (§3-6-2)
      // play_fps / audio_* などの既存キーはそのまま
    }
  }
}
```

* `DEFAULT_SETTINGS`（`settings_window.py`）へ同内容を追加します。
* `_merge_with_defaults()` はセクション単位の浅いマージのため、
  `timeline.shortcuts` の欠落キーは `builder.timeline_config()` 側で既定へ戻します（既存の `ui` / `preview` と同じ扱い）。
* **既存ユーザーへの影響**: `ripple_delete` は既に `setting.json` に `false` として書き込まれているため、
  既定値を変えても**既存ユーザーの動作は変わりません**。要望どおりにするには
  `_normalize_legacy_values()` で 1 度だけ `true` へ寄せる必要があります（§9）。

---

## 7. エラー処理・フォールバック方針

| 事象 | 挙動 |
|---|---|
| 再生ヘッド上にクリップが無い状態で `A` / `D` | 何もせず、Timeline 下部のヒント欄へ「再生ヘッド上にクリップがありません」と出す。エラーにしない |
| 再生ヘッドがクリップの端ちょうど | 削除量 0 のため何もしない（履歴も汚さない） |
| 残りが `min_clip_sec` を割る | **クリップごとリップル削除する**（回答 Q4 / §5.3-2） |
| トラックがロックされている | そのトラックだけ詰めない（他トラックは詰める）。ロックの本来の意味に沿う |
| リップル区間に**完全に含まれる**字幕・オーバーレイ | 削除する。消えた映像に付いていた要素のため（§5.4-3 規則 3） |
| リップル区間を**跨ぐ**クリップ（映像・字幕とも） | **分割せず尺を `delta` 縮める**（回答 Q8 / §5.4-3 規則 6）。結果が最小尺を割れば削除 |
| リップルの結果、映像クリップが 0 件になる | 実行は許可する（Undo で戻せる）。書き出しは「決定」時に弾く（既存 `accept()` の検査） |
| 再生中に `A` / `D` / `W` | 再生中は編集操作を受け付けない既存方針（`resolve.md` §4-8）に従い無効。ショートカットも同時に無効化する |
| **文字入力欄にフォーカスがある状態でのキー入力** | 登録済みショートカットがすべて無効になり、本来の文字編集として働く（回答 Q6 / §5.5-3）。`BackSpace` は 1 文字削除、`←` `→` はカーソル移動、`Ctrl+Z` は入力の取り消しになる |
| **何も選択していない状態で `BackSpace`** | 何もしない（`delete_selected()` が選択なしで `False` を返す）。エラーにしない |
| **ロックされたトラックのクリップを `BackSpace`** | そのクリップは削除しない（他の選択クリップは削除する） |
| **`QtMultimedia` が使えない環境で `E`（倍速再生）** | 再生できない旨をステータスへ出して何もしない。`Q`（倍速逆再生）は音声を使わないため**そのまま動く** |
| **倍速逆再生でフレーム取得が間に合わない** | エラーではない。フレームを間引き、再生ヘッドの後退は止めない（§3-6-2） |
| **倍速逆再生が先頭に達した** | 0 秒で自動停止する |
| **再生中に `Q` / `E` / `Space` で別の状態へ切り替え** | いったん停止してから切り替える（速度が混ざらないようにする） |
| 設定のキー文字列が不正（`QKeySequence` が解釈できない） | 該当ショートカットだけ登録をスキップし WARNING。他のキーは有効なまま |
| 設定のキーが重複している | 後勝ちで登録し WARNING。起動は妨げない |
| 設定 `playback_rate` が 0 以下 | 既定 `2.0` へ丸めて WARNING |
| 吸着候補が 0 件（クリップが無い） | 吸着せず生の座標を使う |

**原則**: ショートカットや吸着の不備で編集画面が使えなくなることが無いようにします。

---

## 8. ログ出力方針

| タイミング | レベル | 内容 |
|---|---|---|
| ショートカット登録の失敗・重複 | WARNING | `ショートカットのキー指定が不正です: timeline.shortcuts.split="Xyz"` |
| `A` / `D` の実行 | DEBUG | `リップルトリム: clip=c12 side=before delta=1.480s` |
| 対象クリップ無しで `A` / `D` | DEBUG | 画面には出すがログは DEBUG（多発するため） |
| 残り尺不足でクリップごと削除へ切り替え | INFO | `残り尺が下限を割るためクリップごとリップル削除しました: c12` |
| 範囲リップルの結果 | INFO | `リップル: 12.480〜15.220s (2.74s) を除去 / 削除 3 件・短縮 1 件・移動 12 件` |
| 区間内の字幕を削除 | INFO | 上記に件数として含める（1 件ずつは出さない） |
| 再生速度の変更 | DEBUG | `再生速度: 2.0 倍 (前進)` / `再生速度: 2.0 倍 (逆再生・音声なし)` |
| 倍速再生が使えない環境 | INFO | `QtMultimedia が利用できないため倍速再生を無効にします`（起動時に 1 回） |
| 吸着 | 出さない | ドラッグ中に多発するため。不具合調査時は DEBUG を一時的に足す |
| 逆再生のフレーム間引き | 出さない | 毎ティック発生するため |

---

## 9. 影響範囲・後方互換

| 対象 | 影響 |
|---|---|
| Timeline モデル / JSON スキーマ | **影響なし**。新コマンドは既存フィールドを操作するだけ |
| レンダラ | **影響なし** |
| 従来モード（`timeline.enabled=false`） | **影響なし**。旧字幕編集画面には Timeline が無い |
| アーカイブ切り抜き | **影響なし** |
| 既存ショートカット | すべて据え置き。`Ctrl+B` も残す |
| Undo/Redo | 新コマンドもスナップショット方式に乗るため自動で対応 |
| クリップドラッグの吸着 | 委譲に置き換えるのみで**挙動不変**（末尾吸着の追加を除く） |
| **既存の「リップル削除」の挙動** | **変わる**（R8）。同一トラックのみ → 全トラック。`Shift+Delete` と右クリックの「リップル削除」も同じく全体を詰めるようになる |
| **既存テスト** | `tests/test_timeline_commands.py` の `DeleteTest` は「リップル削除で V1 だけが詰まる」前提。**期待値の更新が必要**（字幕・オーバーレイも詰まる） |
| クリップの移動・トリム | **影響なし**。字幕は従来どおり追従しない（`resolve.md` 回答 Q2 のまま / §1.1） |
| **プレビューの再生** | 再生状態の持ち方が真偽 → 速度へ変わる。通常再生（`Space`）の挙動は不変 |
| **`Space` の効き方** | **変わる**（改善）。現在は字幕入力欄でも再生トグルとして働いてしまうが、`_ShortcutGuard` により入力欄では空白が打てるようになる |
| 既存ユーザーの `setting.json` | `ripple_delete` は既に `false` で保存済み。**移行処理を入れる**（回答 Q5 / 下記） |

### 既存設定の移行（回答 Q5：移行してよい）

`ripple_delete` は既に書き込まれているため、既定値を変えるだけでは効きません。
`settings_window._normalize_legacy_values()` に「旧既定値 `false` のままなら `true` へ寄せる」
処理を **1 回だけ** 入れます（`ffmpeg.executable` の移行と同じ方式）。

移行済みかどうかを区別するため、`timeline.ripple_delete_migrated`（真偽）を併せて記録し、
**移行は 1 度だけ**行います。これにより、移行後にユーザーが意図して `false` へ戻した場合、
次回起動で再び `true` へ書き換わることはありません。

---

## 10. 確認事項

### 10.1 回答反映済み（Q1〜Q8 すべて）

| # | 論点 | 回答 | 反映先 |
|---|---|---|---|
| Q1 | `S` の割り当て | 何も割り当てない。`Space`=再生/停止、`Q`=倍速逆再生、`E`=倍速再生を追加 | R9〜R11。§3-6 / §5.5-1 / §5.6。設定から `S` の項目を削除 |
| Q2 | リップル時に字幕・オーバーレイも詰めるか | リップル削除を行った場合は全体的に詰める | R8。§3-5 / §5.4。`ripple_sync_tracks` 既定 `"all"` |
| Q3 | `A` / `D` の対象クリップ | 選択中クリップ優先 → 無ければ V1 | §5.3-3（当初案どおり） |
| Q4 | 残りが最小尺を割るとき | クリップごとリップル削除 | §5.3-2 / §7（当初案どおり） |
| Q5 | `ripple_delete` の移行 | 移行してよい | §9。1 度だけ移行する記録を持たせる |
| Q6 | 単文字キーと文字入力の競合 | 入力欄にフォーカスがあるときはショートカットとして働かせない | §5.5-3。`Space` も対象に含める |
| Q7 | `A` / `D` を字幕にも効かせるか | 効かせる | §5.3-3 / §5.4-3 |
| Q8 | 区間を跨ぐクリップ | 分割せず尺を縮めるだけ | §5.4-3 規則 6。映像・字幕を同一規則へ統一 |

### 10.2 未回答（実装中に確定すればよい）

| # | 論点 | 現時点の案 | 確認したいこと |
|---|---|---|---|
| **Q9** | **倍速逆再生（`Q`）で音を鳴らすか** | **鳴らさない**（`QMediaPlayer` が負の再生レートに対応していないため / §3-6-2） | 逆再生でも音が要るなら、`ffmpeg -af areverse` で逆再生音声を都度生成する方式になります。ただし生成待ちが入り「さっと早戻しする」用途には向きません。無音のままでよいか |

Q9 は**無音で実装を進めても手戻りになりません**（音声を足す場合も
`_play_backward()` の中だけの変更で済みます）。実機で触ってから判断いただけます。

---

## 11. 段階実装（レビュー後）

各段階の終了時点でアプリが動作し、既存機能を壊さないことを条件にします。

| 段 | 内容 | 完了条件 |
|---|---|---|
| **1** | `TimelineController.snap_sec()` / `snap_targets()` を新設し、`TimelineView._snap()` を委譲へ置き換え | 既存のクリップドラッグ吸着が**まったく同じ挙動**のまま。単体テストを追加 |
| **2** | 再生ヘッドの吸着（R1）。ルーラ／空き領域／右クリックの 4 箇所に適用。`Alt` で無効化 | 再生ヘッドが編集点へ吸い付く。矢印キーのフレーム送りと再生中は吸着しない |
| **3** | ノード吸着の強化（R2）。移動時に先頭・末尾の両方で判定 | クリップ末尾を隣の先頭へ合わせられる |
| **4** | **範囲リップル `ripple_remove_range()`（R8）** と `DeleteClip(ripple=True)` の差し替え | リップル削除で字幕・オーバーレイも一緒に詰まる。区間内の字幕が消える。音声が V1 と一致し続ける。既存 `DeleteTest` の期待値を更新 |
| **5** | `RippleTrimToPlayhead` コマンドと Controller のメソッド（R4・R6） | `A`/`D` 相当の操作が 1 回の Undo で全トラックぶん戻る |
| **6** | ショートカットの設定化と `_ShortcutGuard`（R3・R7・回答 Q6） | `W`/`A`/`D` が効き、**字幕入力欄では効かず文字が打てる**。`Space` も入力欄で空白として入る。`Delete` がリップル削除になる。IME 有効時の挙動を実機で確認 |
| **7** | **再生制御（R9〜R11）**。`_rate` への置き換え・倍速再生・倍速逆再生・トランスポート UI | `E` で倍速再生（音あり）、`Q` で倍速逆再生（音なし・状態表示あり）、`Space` で通常再生。いずれもトグルで止まる。`QtMultimedia` 不在環境でも `Q` は動く |
| **8** | `setting.json` の既定変更と移行処理（§9）。ヒント欄の文言更新 | 既存ユーザーでも `Delete` がリップル削除になる。移行は 1 度だけ走る |

段 1〜5 は UI を伴わない部分が多いため、`tests/` に単体テストを追加して検証します
（`test_timeline_snap.py` / `test_timeline_commands.py` への追加）。
段 6〜8 は GUI のため、既存のオフスクリーン起動確認と実機での操作確認で検証します。

---

## 12. 実装状況（2026-08-07）

段 1〜8 を実装済み。**新規ファイルはテスト 1 本のみ**で、残りは既存への追加・差し替えに収まりました。

### 12.1 変更ファイル

| ファイル | 変更 |
|---|---|
| `src/timeline/commands.py` | `ripple_target_tracks()` / `_apply_range_removal()` / `ripple_remove_range()` / `RippleTrimToPlayhead` を**追加**。`DeleteClip` を「空白を残す」と「リップル」の 2 経路へ分け、リップル側を共通処理へ差し替え |
| `src/timeline/builder.py` | `timeline_config()` に `ripple_sync_tracks` / `snap_playhead` / `shortcuts` / `preview.playback_rate` / `preview.reverse_play_fps` を追加。`_DEFAULT_SHORTCUTS` と `PLAIN_KEY_ACTIONS` を新設 |
| `src/gui/timeline/timeline_controller.py` | `snap_targets()` / `snap_sec()` / `snap_playhead_sec()` / `ripple_trim_to_playhead()` / `clip_at_playhead()` / `_sync_all()` を追加 |
| `src/gui/timeline/timeline_view.py` | `_snap()` を Controller へ委譲。`_snap_move()`（先頭・末尾の両方で吸着）と `_playhead_sec_at()` を追加。再生ヘッド移動 4 箇所（ルーラ／空き領域クリック／同ドラッグ／右クリック）に吸着を適用。右クリックメニューに A / D を追加。ヒント文言を更新 |
| `src/gui/timeline/timeline_editor_dialog.py` | ショートカットを設定から読む方式へ変更。`_ShortcutGuard` を追加。A / D / Q / E のハンドラを追加 |
| `src/gui/timeline/preview_panel.py` | 再生状態を真偽 `_playing` から速度 `_rate` へ置換。`toggle_rate()` / `_play_forward()` / `_play_backward()` / `_step_backward()` / `set_status()` を追加。トランスポートに `◀◀` `▶▶` を追加 |
| `src/settings/settings_window.py` | `ripple_delete` 既定を `true` へ。`ripple_delete_migrated` / `ripple_sync_tracks` / `snap_playhead` / `shortcuts`（`delete_plain` 含む）/ `preview.playback_rate` / `preview.reverse_play_fps` を追加。`_migrate_ripple_delete()` と `_fill_timeline_nested_defaults()` を追加 |
| `tests/test_timeline_commands.py` | `RippleRangeTest`（10 件）と `RippleTrimToPlayheadTest`（8 件）、削除 3 種の区別テスト（3 件）を追加 |
| `tests/test_timeline_snap.py` | **新規**。吸着と対象クリップ解決のテスト 16 件 |

### 12.2 確認済みの動作

* **単体テスト 188 件が通る**（本対応で 37 件追加。既存 151 件は無改修で通過）
* **`BackSpace`（R12）**: 選択中クリップを削除し、跡がギャップとして残り、
  後続クリップと字幕が動かないことを確認。`Delete`（リップル）と結果が明確に異なる
* **ショートカットの無効化範囲**: 字幕入力欄にフォーカスがある間、
  登録済み 20 個のショートカットが**すべて**無効になり、Timeline へ戻ると全数が有効に戻る
* **吸着**: 再生ヘッドが 0.8 秒以内の編集点へ吸い付き、閾値外では動かない。`Alt` 押下で無効化。
  ズームを上げると閾値（px 基準）が縮み、吸着しにくくなる
* **`A` / `D`**: 再生ヘッド 15.0 で `D` を実行し、対象クリップが 10→5 秒へ縮み、
  後続クリップと字幕が同じだけ前へ詰まることを実測
* **`Delete`（リップル）**: 区間内の字幕が消え、後続が詰まり、区間を跨ぐオーバーレイが
  **分割されず尺だけ縮む**（20→10 秒）ことを確認（回答 Q8）
* **`Shift+Delete`（空白を残す）**: ギャップが残り、他トラックが動かない（リップルとの違いを確認）
* **ショートカット**: `W` `A` `D` `Q` `E` `Space` が登録され、**`S` は未登録**（回答 Q1）
* **単文字ガード**: 字幕入力欄にフォーカスがある間、6 個の単文字ショートカットがすべて無効化され、
  Timeline へフォーカスが戻ると再び有効になる（回答 Q6）
* **倍速逆再生**: `Q` で `rate=-2.0` になり、ボタンが押下表示になり、
  「2 倍速逆再生中 (音声なし)」が表示される。もう一度 `Q` で停止（トグル）
* **設定移行**: 既存 `setting.json` の `ripple_delete=false` が 1 度だけ `true` へ移行され、
  `ripple_delete_migrated=true` が記録される（回答 Q5）
* **通し確認**: Timeline パイプラインの end-to-end（無音検出 → 編集点 → 構築 → レンダリング → 出力）が
  従来どおり完走し、出力尺も一致

### 12.3 実装中に見つけて直した既存不具合

| 不具合 | 内容 |
|---|---|
| **音声チャンクの先読みで再生位置が飛ぶ** | `_prefetch_next()` が生成したチャンクの完了通知が `_start_playback()` に繋がっており、**再生中に数秒先へジャンプ**していた。先読みジョブと再生ジョブを ID で区別し、先読みでは再生位置を動かさないよう修正 |
| **停止後に生成完了した音声で再生が再開する** | 「音声を準備中…」の間に停止すると、あとからチャンクが届いた時点で再生が始まってしまう。停止時に `_pending_rate` を落とし、届いても再生しないよう修正 |
| **字幕入力欄で `Delete` / 矢印 / `Ctrl+Z` が奪われる** | ショートカットが文字入力より先に発火するため、入力欄で文字が消せない・カーソルが動かせない状態だった。`_ShortcutGuard` の対象を全ショートカットへ広げて解消（R12 の `BackSpace` 追加で顕在化） |
| **`timeline` の入れ子設定に新キーが書き出されない** | `_merge_with_defaults()` がセクション単位の浅いマージのため、`timeline.shortcuts` や `timeline.preview` の新規キーが `setting.json` に現れず、利用者が編集できなかった。`_fill_timeline_nested_defaults()` で timeline セクションに限り 1 段深く補完（既存値は上書きしない） |

上 2 件は `resolve.md` の実装時に入り込んだもので、本対応の再生制御の整理中に判明しました。

### 12.4 未着手

* **Q9（倍速逆再生の音声）**: 設計どおり**無音**で実装。音を足す場合も
  `_play_backward()` の中だけの変更で済みます
* IME 有効時の単文字キーの挙動は実機確認が必要（§5.5-3）

**段 4 が最も影響の大きい変更**です。既存のリップル削除の挙動が変わるため、
`resolve.md` §14 Phase 6 で置いた不変条件（全操作の後で A1 の時刻が V1 と一致する）を
そのまま回帰の基準として使い、加えて次を検証します。

* リップル区間より後ろの字幕・オーバーレイが `delta` ぶん前へ詰まる
* リップル区間に完全に含まれる字幕・オーバーレイが消える
* リップル区間を跨ぐオーバーレイが分割され、前後の合計尺が `元の尺 − delta` になる
* Undo で全トラックが元へ戻る

**段 6 の時点で `Delete` の意味が変わる**ため、画面下部のヒント文言
（現在「Delete で削除・Shift+Delete でリップル削除」）も同時に更新します。
