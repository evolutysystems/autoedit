# resolve15（ver3） — メイン画面のデザイン修正 / クリップ用タブの再構成 修正設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request15.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「実装前に設計を行うこと」）。
* 調査は **2026-09-04 時点の実コード**を読み、
  チェックボックスと QComboBox については **実際に Qt で描画してピクセルを数え**、
  修正案も**同じ方法で描かせて効果を確認**したうえで書いている（§2.2 / §2.3）。
* 画面イメージは `docs/request/ver3/resolve15.html`（本書と対）。
  部品の見た目は `docs/layout/theme.css`（部品カタログの実体）をそのまま読み込んでいるため、
  カタログと同じ規則で描かれている。
* 変更は **メイン画面まわりに限定**する。Timeline 編集画面・設定画面・アーカイブ用タブの
  **画面構成は変えない**（QSS の共通部分だけは全画面へ効く / §8）。

---

## 1. 要望（request15.md）と要件 ID

### 1-1. メイン（全体のデザイン）

| ID | 要望 | 種別 |
|---|---|---|
| **D1** | 設定ボタンとタブのウィンドウがくっついている。**設定ボタンの下に余白**を作る | 不具合に近い見た目 |
| **D2** | チェックすると背景色は変わるが**チェックマークが付かない**。オレンジになったらチェックマークも付ける | 不具合 |
| **D3** | QComboBox の**「▽」の部分が浮き出ている**。`docs/layout/index.html` の部品カタログと同じにする | 不具合 |

### 1-2. クリップ用タブ（画面構成の作り直し）

上から順に並べる。

| ID | 要望 | 種別 |
|---|---|---|
| **C1** | 画面**上部 60%** に「**ドラッグ＆ドロップでファイルを選択**」を置く。D&D を表すアイコンがあるとなお良い | 新規 |
| **C2** | 既存の**ファイル選択欄と「参照...」ボタンを隠す** | 変更 |
| **C3** | **画面のどこへ D&D してもファイルが選択される** | 変更 |
| **C4** | D&D したら **文言がファイル名に変わる**。ファイル名の右に **× ボタン**が出て、押すと選択が消えて文言が戻る | 新規 |
| **C5** | 既存の「編集の続き」行（コンボ・開く・一覧）を**削除**する | 削除 |
| **C6** | 代わりに「**続きから**」ボタンを**画面中央**に置く。押すと既存の「一覧...」と同じ動作。選択すると**文言がプロジェクト名に変わり**、右の × で戻せる | 新規 |
| **C7** | 次に**実行ボタン**（デザインは既存のまま・**画面中央**）→ **プログレスバー**（既存のまま）→ **実行状況ラベル**（既存のまま）の順に置く | 変更 |
| **C8** | 「続きから」ボタンの**横幅を実行ボタンに合わせる**（画面イメージのレビューで追加） | 変更 |

### 1-3. 要望文に無いが実装に必ず要る論点

| ID | 論点 | 理由 |
|---|---|---|
| **X1** | 「上部 60%」の**基準になるウィンドウの高さ**が今は存在しない | `MainWindow` は `resize()` を呼ばず中身に合わせて開く（実測 680×303）。基準が無いと比率が決まらない（§2.7） |
| **X2** | 「動画」と「プロジェクト」の**選択が排他**であること | 文言は 1 つしかない。両方選べると、どちらが実行されるのか画面から分からなくなる（§3-5） |
| **X3** | **実行ボタンの振り分け** | 選んだものが動画なら従来のパイプライン、プロジェクトなら「編集の続き」。実行の入口が 1 つになるため必ず要る（§5.6） |
| **X4** | 「一覧」から選んだときに**すぐ実行しない**こと | 現行は選んだ瞬間に走り出す。要望は「文言に名前が出る」なので、実行は実行ボタンに委ねる（§2.6 / §3-6） |
| **X5** | `ProjectResumeRow` を消したあとの**種別違いプロジェクトの受け渡し** | `MainWindow._switch_to_kind` がクリップ用タブの `resume_row` を直接触っている。消すと黙って何もしなくなる（§2.8） |

---

## 2. 現状分析

### 2.1 設定ボタンがペインに接している理由（D1）

設定ボタンは `QTabWidget` のコーナーウィジェットとして、余白を挟まずに置かれている。

```python
# main_window.py:850-855
self.settings_button = QPushButton()
self.settings_button.setIconSize(QSize(theme.BUTTON_ICON_PX, theme.BUTTON_ICON_PX))
self.settings_button.setToolTip("設定")
theme.mark_icon_button(self.settings_button)
self.settings_button.clicked.connect(self._on_open_settings)
self.tabs.setCornerWidget(self.settings_button, Qt.TopRightCorner)   # ← 直接置いている
```

`QTabWidget` はコーナーウィジェットをタブバーの高さいっぱいに置くため、
ボタンの下辺とペイン（タブの中身の面）の上辺が接する。
QSS 側に余白を作れる指定は無い（`QTabBar` の規則はタブにしか効かない）。

**参考**: HTML 版のカタログでは既にこの点を補正して描いている。

```css
/* docs/layout/theme.css:381 */
.tabbar .corner { margin-left: auto; padding-bottom: 4px; }
```

実装だけが `padding-bottom` に相当するものを持っていない、という状態である。

### 2.2 チェックマークが付かない理由（D2）— 実測

QSS はチェック時に**背景と枠の色を変えるだけ**で、印を描いていない。

```
/* theme.py:822-831 */
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: {border.width} solid {glass.border};
    border-radius: 3px;
    background: {glass.bg.strong};
}
QCheckBox::indicator:checked { background: {accent}; border-color: {accent}; }
```

`::indicator` に QSS を当てた時点で Qt は既定のチェック印を描かなくなる
（`theme.py:820` のコメントが、枠を自前で描くことになった経緯を記録している）。
チェック印も同じ理由で消えている。

**実測**（`QT_QPA_PLATFORM=offscreen` / `ui.theme_mode=dark` / `QCheckBox.grab()` の
左 20×24px を色ごとに数えた結果）:

| 状態 | 主な色と画素数 | 判定 |
|---|---|---|
| チェック済み（現行） | `#E8A15C` **236** ＋縁の中間色のみ | アクセントの塗りだけ。`accent.on`(`#1B1B1F`) の画素は **0** = 印が無い |
| 未チェック（現行） | `#1E1E1E` 190 / `#525252` 32 | 枠と面は出ている（従来どおり） |
| チェック済み（**修正案**） | `#E8A15C` **184** / `#1B1B1F` **14** ＋中間色 | 塗りの上に印が乗り、オレンジの画素が 52 減った = **チェックマークが描かれている** |

修正案は「`::indicator:checked` へ `image:` を与える」だけで、
枠・色・大きさの規則は現行のままにしている（§3-1）。

### 2.3 QComboBox の「▽」が浮き出る理由（D3）— 実測

QSS は `QComboBox` 本体だけを塗り、**`::drop-down`（右端の押しボタン）を書いていない**
（`theme.py:703-722`）。書かれていないサブコントロールは Qt のスタイルが既定の
見た目で描くため、右端だけが独立した押しボタンとして残る。

**実測**（`QComboBox` 160×26px を `grab()` し、右端 30px の行を読んだ結果）:

| 行 | 右端 15px の色 | 本体の色 |
|---|---|---|
| y=5 | `#4F4F4F` | `#383838` |
| y=13 | `#4F4F4F`（中央に `#B6B4B5` の三角） | `#383838` |
| y=20 | `#4F4F4F` | `#383838` |

**右端だけが一段明るい面（`#4F4F4F`）になっている**のが「浮き出ている」の正体である。
カタログ側にはこの面が無く、文字色の三角だけが載っている。

```css
/* docs/layout/theme.css:322-323 — 部品カタログの QComboBox */
.combo .combo-text { flex: 1; ... }
.combo .combo-arrow { color: var(--text-secondary); font-size: 9px; }
```

`::drop-down` を透明・枠なしにして矢印だけを描かせた結果（同じ計測）:

| 行 | 右端 30px |
|---|---|
| y=5 | `#383838` 一色（**明るい面が消えた**） |
| y=13 | `#383838` の中に `#B9AEB2`（= `text.secondary`）の三角 |
| y=20 | `#383838` 一色 |

**カタログと同じ「面なし・`text.secondary` の三角だけ」になった。**

> 計測は offscreen（Fusion スタイル）で行った。実機の Windows スタイルでは
> 押しボタンの描き方が違うが、**「`::drop-down` を書かないと素のスタイルが描く」
> という原因は共通**で、修正（透明・枠なしを明示する）もスタイルに依存しない。

### 2.4 クリップ用タブの現在の構成

`ClipTabWidget._build_ui`（`main_window.py:480-529`）は上から順にこうなっている。

| 順 | 部品 | 実装 |
|---|---|---|
| 1 | 入力欄 + 「参照...」 | `input_edit` / `browse_button`（`main_window.py:484-492`） |
| 2 | 実行ボタン（左寄せ・幅 96px） | `run_button` + `addStretch(1)`（`main_window.py:498-504`） |
| 3 | 編集の続き（コンボ + 開く + 一覧） | `ProjectResumeRow`（`main_window.py:509-514`） |
| 4 | プログレスバー | `progress_bar`（`main_window.py:517-519`） |
| 5 | 実行状況ラベル | `status_label` + スピナー（`main_window.py:520-529`） |

要望の並びは **1 を大きな D&D 領域に置き換え、3 を「続きから」ボタンに置き換え、
2 を 3 の下へ移して中央寄せ**にするもの。4・5 は現状のままである。

### 2.5 「選択」が 2 か所に分かれている

現状、ユーザーが選べるものは 2 つあり、持ち場所も別々である。

| 選ぶもの | 持ち場所 | 実行の入口 |
|---|---|---|
| 入力動画 | `input_edit`（テキスト） | 実行ボタン → `_on_run` → `PipelineWorker` |
| 保存済みプロジェクト | `resume_row` のコンボ | 「開く...」→ `_start_resume` → `ProjectResumeWorker` |

**両方を同時に選べてしまうが、実行の入口が別なので今は矛盾しない。**
要望では文言も実行ボタンも 1 つになるため、**排他にしないと成立しない**（X2 / §3-5）。

### 2.6 「一覧」から選ぶとその場で実行が始まる（X4）

```python
# main_window.py:608-610
def _on_library_open(self, path):
    self.resume_row.select(path)
    self._start_resume(path)      # ← 選んだ瞬間に走り出す
```

要望 C6 は「選択すると文言にプロジェクト名が**表示される**」であり、実行とは書いていない。
また C7 で実行ボタンが独立して置かれるため、**選択と実行は分ける**のが要望に沿う。

なお D&D でプロジェクトを落としたときは、**既に選択だけ**で止めてある。

```python
# main_window.py:675-680
# 落としただけで長い処理が始まらないよう、実行は「開く...」で明示的に行わせる。
def _select_project(self, path):
    self.resume_row.select(path)
```

つまり「選択と実行を分ける」判断は既にこの画面の中にあり、
一覧だけが例外になっている。**例外の方を揃える**のが筋である。

### 2.7 「上部 60%」の基準が今は無い（X1）

`MainWindow` は `resize()` を呼ばない。実測（offscreen）で:

```
window sizeHint       : 680 x 303
window minimumSizeHint: 680 x 303
```

* 横 680px は**アーカイブ用タブの中身**で決まっている（クリップ用は 210px の入力欄しか無い）。
* 縦 303px は中身の合計。**この高さの 60% は約 180px** で、D&D 領域としては小さい。

したがって **比率を意味のある大きさにするには、ウィンドウの初期サイズを決める必要がある**。
Timeline 編集画面は同じ問題を `setting.json` で解決している
（`timeline.ui.window_width / window_height` = 1280×820）。メイン画面にも同じ形で持たせる（§7）。

> 実測値は offscreen 環境のもの（日本語フォントが無く行が低い）。実機ではもう少し大きくなるが、
> 「比率の基準が無い」という結論は変わらない。

### 2.8 `ProjectResumeRow` を消すと壊れるところ（X5）

クリップ用タブの `resume_row` を外から触っている箇所がある。

```python
# main_window.py:872-878（MainWindow._switch_to_kind）
tab = (self.archive_tab if kind == project_io.KIND_ARCHIVE else self.clip_tab)
row = getattr(tab, "resume_row", None)
if row is None:
    return                 # ← 消すと黙ってここで戻る
self.tabs.setCurrentWidget(tab)
row.select(project_path)
```

これは「アーカイブ用タブでクリップ用プロジェクトを選んでしまったとき、
クリップ用タブへ渡す」経路（ver3 resolve9 §3-4）である。
`getattr` のおかげでクラッシュはしないが、**タブ切り替えも選択も起きなくなる**。
タブ側に共通の受け口を用意して置き換える（§5.7）。

### 2.9 影響範囲（QSS の共通部分）

D2・D3 は QSS の共通規則のため、**アプリ全体の QComboBox / QCheckBox に効く**。

| 部品 | 生成箇所 | 個数 |
|---|---|---|
| `QCheckBox` | `archive_tab.py` / `archive_timeline_dialog.py` / `settings_window.py` | 18 |
| `QComboBox` | 上記 + `project_library_dialog.py` / `project_resume_row.py` / `subtitle_editor_dialog.py` / `timeline_editor_dialog.py` | 12 |

いずれも「印が付く」「右端の面が消える」だけで、**位置も大きさも動かない**（§8）。

一覧・表の中のチェック（`Qt.ItemIsUserCheckable` / `subtitle_editor_dialog.py:343` ほか 3 か所）は
`QCheckBox::indicator` ではなく**ビューのチェック印**であり、今回の QSS の対象外。
現状どおり Qt が描く（§5.9）。

---

## 3. 方式選定

### 3-1. チェックマークの描き方（D2）

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | 記号を **PNG へ描いて一時フォルダへ置き**、`QCheckBox::indicator:checked { image: url(...) }` で載せる | QSS の枠・色・大きさの規則をそのまま活かせる。実測で効果を確認済み（§2.2）。色はテーマから引くのでライト/ダーク両対応 |
| B | `QProxyStyle` で `PE_IndicatorCheckBox` を自前描画する | QSS が `::indicator` に規則を持つ間、描画は `QStyleSheetStyle` が担い**基底スタイルへ回ってこない**。現に今も印が描かれていないのがその証拠 |
| C | `QCheckBox::indicator` の QSS を撤去して素のスタイルに戻す | 印は戻るが**枠と面も素に戻る**。ver3 resolve5 で「未チェックだと何も見えない」ため自前で枠を描いた経緯（`theme.py:820`）を元に戻すことになる |
| D | チェック用の画像素材をリポジトリへ追加する | 明暗 2 色ぶん必要で、アクセント色を設定で変えると合わなくなる。本プロジェクトは記号を描いて `QIcon` 化する方針（`theme.glyph_icon` / resolve4 §3-5）で素材を増やしていない |

**A を採用する。**
QSS の `image:` は**ファイルパスしか受け付けない**（データ URI は使えない）ため、
描いた PNG を置く場所が要る。置き場所は `tempfile.gettempdir()` とする
——`updater.py:86` が更新ファイルの置き場所に使っているのと同じ考え方で、
インストール先が書き込み不可（Program Files）でも成立する。

描く記号は**フォントではなく `QPainter` の線**（`drawPolyline`）にする。
`theme.glyph_icon` は記号フォントに頼るが、14px の枠に収めるチェック印は
フォント依存だと環境で太さが揃わないため、ここだけは図形で描く。

### 3-2. QComboBox の矢印（D3）

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | `::drop-down` を**透明・枠なし**にし、`::down-arrow` に §3-1 と同じ方法で描いた三角を載せる | 実測でカタログと同じ見た目になることを確認済み（§2.3）。三角の色は `text.secondary` でカタログと一致 |
| B | `::drop-down` だけ透明にし、矢印は素のスタイルに任せる | 浮き出しは消えるが、矢印の色がスタイル任せになりテーマに追随しない（ダークで沈む） |
| C | 矢印を出さない | 押せることが分からなくなる。カタログにも三角がある |

**A を採用する。** アセットの作り方は §3-1 と共通の仕組みに乗せる（1 か所で作って 1 か所で捨てる）。

生成に失敗した場合は `image:` の規則を**丸ごと出力しない**。
チェックは「印なしの塗り（＝現行の見た目）」、矢印は「素のスタイルの三角」に戻るだけで、
**起動は妨げない**（`theme.apply` の設計方針 §7 と同じ）。

### 3-3. 設定ボタンの余白（D1）

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | ボタンを**余白付きの入れ物ウィジェットで包んで**コーナーへ置く | Qt の素直なやり方。余白の値を 1 か所（`theme`）に置け、他の画面がコーナーを使うときも同じ関数で揃う |
| B | `QTabWidget::tab-bar` などへ QSS で余白を入れる | コーナーウィジェットは QSS の余白指定の対象外で、タブ側の余白しか動かない |
| C | ボタン自身に `setContentsMargins` する | `QPushButton` の contentsMargins は中身（アイコン）を動かすだけで、外側の余白にはならない |

**A を採用する。** 余白は **6px**（Qt のレイアウト既定間隔と同じ）とする。
カタログの `padding-bottom: 4px`（§2.1）も 6px へ揃える。

### 3-4. 「上部 60%」の作り方（C1 / X1）

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | ウィンドウの初期サイズを `setting.json` で持ち、`resizeEvent` で **D&D 領域の高さ = タブページの高さ × 0.60** に追従させる | 大きさを変えても常に 60%。比率も最小高も設定で変えられる |
| B | レイアウトの伸長係数（`addWidget(w, 60)` / 残り 40）で配分する | Qt の伸長係数は**余った空間の配分**であって全体比ではない。他の部品が固有の高さを持つため 60% にならない |
| C | 固定の px（例 280px）にする | ウィンドウを広げても領域が育たない。要望は「上部 60% を占める大きさ」 |

**A を採用する。** ただし無条件に 60% を強いると、
**残りの部品（続きから・実行・進捗・ラベル）が入らない**場合にウィンドウの最小高が押し上がる。
そこで `resizeEvent` では次のように決める。

```
target = max(drop_zone_min_height_px, floor(タブページの高さ × drop_zone_ratio))
残り   = タブページの高さ - target
if 残り < 残りの部品の最小高:
    target = max(drop_zone_min_height_px, タブページの高さ - 残りの部品の最小高)
```

「60% を守る」より「**下の部品が消えない**」を優先する（縮めたときに実行ボタンが
見えなくなる方が実害が大きい）。この優先順位は §9 Q3 で確認する。

### 3-5. 選択の持ち方（X2）

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | `_selection = (種別, パス)` の **1 つだけ**を持ち、動画かプロジェクトのどちらかしか入らない | 文言が 1 つ・実行ボタンが 1 つという要望の形とそのまま一致する。× は `_selection` を空にするだけ |
| B | 動画とプロジェクトを別々に持ち、実行時に優先順位で決める | 画面には片方しか出ないのに、内部では両方生きている。ユーザーから見て「どちらが動くか」が分からない |

**A を採用する。**
隠す `input_edit` は**表示用の控え**として残し、値の正は `_selection` に置く（§5.6）。

### 3-6. 「続きから」で一覧を開いた後の動き（C6 / X4）

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | 一覧の「開く」は**選択だけ**にし、実行は実行ボタンに任せる | 要望どおり（文言に名前が出る）。D&D の既存方針（§2.6）とも揃う |
| B | 現行どおり選んだ瞬間に実行する | 実行ボタンが「押しても何も起きないボタン」になる場面が生まれる |

**A を採用する。** 変更するのは**クリップ用タブの経路だけ**で、
アーカイブ用タブの `_on_library_open`（`archive_tab.py:613`）は触らない（§5.9）。

---

## 4. 設計方針

1. **QSS の共通部分（D2 / D3）は `theme.py` の中だけで直す。** 各画面は無改造。
2. **画像素材はリポジトリへ追加しない。** 記号は実行時に描いて一時フォルダへ置く（§3-1）。
   作れなかったときは規則ごと出さず、現行の見た目へ落ちる。
3. **クリップ用タブの選択は 1 つ**（動画 or プロジェクト）。文言・× ・実行ボタンはすべてそこを見る（§3-5）。
4. **隠す部品は消さない。** `input_edit` / `browse_button` は残し、
   `ui.main_window.show_file_row`（既定 `false`）で戻せるようにする（§5.6 / §7）。
5. **寸法・比率は `setting.json`**、**デザイン定数は `theme.py`** に置く（§7 の方針表）。
6. `ProjectResumeRow` は**アーカイブ用タブが使い続ける**ため残す。
   種別チェックだけを関数として切り出し、クリップ用タブと共有する（§5.8）。

---

## 5. 詳細設計

### 5.1 変更ファイル一覧

| ファイル | 変更内容 | 規模 |
|---|---|---|
| `src/gui/theme.py` | QSS 規則の追加（`::drop-down` / `::down-arrow` / `#dropArea`）、生成アセット、コーナー余白ヘルパ、`DEFAULT_UI_SETTINGS` へ `main_window` 追加 | 約 120 行 |
| `src/gui/file_drop_area.py` | **新規**。D&D 領域ウィジェット（アイコン・文言・× ボタン） | 約 120 行 |
| `src/gui/main_window.py` | `ClipTabWidget` の画面構成・選択モデル・実行の振り分け、`MainWindow` の初期サイズ / D&D 転送 / `_switch_to_kind` | 約 130 行の入れ替え |
| `src/gui/archive_tab.py` | `select_project(path)` の追加のみ（`resume_row.select` を呼ぶ 3 行） | 3 行 |
| `src/gui/project_resume_row.py` | 種別チェックを module 関数へ切り出す（振る舞いは同じ） | 約 20 行 |
| `src/settings/setting.json` | `ui.main_window` セクションの追加 | 7 行 |
| `docs/layout/theme.css` / `docs/layout/main_window.html` | カタログを実装へ追随させる（`.corner` の余白・クリップ用タブの構成） | 約 40 行 |
| `tests/test_theme.py` | QSS 規則とアセット生成のテスト追加 | 約 40 行 |
| `tests/test_clip_tab_selection.py` | **新規**。選択モデル・実行の振り分け・比率の計算 | 約 90 行 |

### 5.2 `theme.py` — QSS 規則の追加（D2 / D3 / C1）

`_QSS_TEMPLATE` へ次を足す。既存の規則は 1 行も消さない。

```
/* ドロップダウンの押しボタンを消し、矢印だけを見せる (ver3 resolve15 §2.3)。
   ::drop-down を書かないとスタイルが既定の押しボタンを描き、右端だけ一段明るい面
   (実測 #4F4F4F / 本体 #383838) になって浮き出て見える。部品カタログ
   (docs/layout/theme.css .combo) は面を持たず三角だけのため、それに合わせる。 */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: {combo.dropdown.width};
    border: none;
    background: transparent;
}
QComboBox::down-arrow { width: {combo.arrow.size}; height: {combo.arrow.size}; }

/* ドラッグ&ドロップ領域 (ver3 resolve15 C1)。破線で「ここへ落とせる」ことを示す。
   ドラッグ中は dropActive でアクセント色へ変える (受理できることの合図)。 */
#dropArea {
    background-color: {glass.bg};
    border: {border.width} dashed {glass.border};
    border-radius: {radius.panel};
}
#dropArea[dropActive="true"] {
    background-color: {glass.bg.hover};
    border-color: {accent.line};
}
```

画像を使う規則は**別テンプレート**にし、アセットが作れたときだけ後ろへ足す。

```python
# 生成アセットを使う規則 (作れなかったときは丸ごと出力しない / §3-2)。
# パスは空白や日本語を含み得るため必ず引用符で囲む。
_QSS_ASSET_TEMPLATE = """
QCheckBox::indicator:checked { image: url("{asset.check}"); }
QComboBox::down-arrow { image: url("{asset.arrow}"); }
"""
```

`build_qss` は次のようになる。

```python
def build_qss(settings=None):
    resolved = tokens(settings)
    resolved.update(_SHAPE_EXTRA)          # combo.dropdown.width / combo.arrow.size
    qss = _PLACEHOLDER_RE.sub(replace, _QSS_TEMPLATE)
    assets = _indicator_assets(settings)   # 失敗時は {}
    if assets:
        resolved.update(assets)
        qss += _PLACEHOLDER_RE.sub(replace, _QSS_ASSET_TEMPLATE)
    return qss
```

プレースホルダ正規表現 `[a-z][a-z0-9.]*`（`theme.py:836`）は
`combo.arrow.size` / `asset.check` をそのまま拾えるため、**正規表現は変更しない**。

### 5.3 `theme.py` — 記号アセットの生成

```python
# QSS の image: url(...) はファイルパスしか受け付けないため、記号を PNG へ描いて
# 一時フォルダへ置き、そのパスを QSS へ埋め込む (ver3 resolve15 §3-1)。
# 置き場所に tempfile を使うのは updater と同じ理由 (インストール先が書き込み不可でも動く)。
_ASSET_DIR_NAME = "stretheus_theme"
_ASSET_SCALE = 3          # 高 DPI 用に実寸の 3 倍で描き、QSS の width/height で縮める
_asset_cache = {}         # (モード, 印の色, 矢印の色) -> {"asset.check": path, "asset.arrow": path}


# チェック印・下向き三角を描いて PNG のパスを返す。作れなければ {} を返す。
def _indicator_assets(settings=None):
    resolved = tokens(settings)
    key = (mode(settings), resolved["accent.on"], resolved["text.secondary"])
    cached = _asset_cache.get(key)
    if cached is not None:
        return cached
    try:
        directory = os.path.join(tempfile.gettempdir(), _ASSET_DIR_NAME)
        os.makedirs(directory, exist_ok=True)
        stamp = hashlib.md5("|".join(key).encode("utf-8")).hexdigest()[:8]
        paths = {
            "asset.check": _write_check_png(directory, stamp, key[1]),
            "asset.arrow": _write_arrow_png(directory, stamp, key[2]),
        }
    except Exception:  # noqa: BLE001 (印が出ないだけで起動は妨げない / §3-2)
        _logger.exception("チェック印・矢印の生成に失敗しました (印なしで続行します)")
        return {}
    _asset_cache[key] = paths
    return paths
```

* 色は**テーマから引く**。チェック印は `accent.on`（アクセント塗りの上に載せる文字色）、
  矢印は `text.secondary`（カタログの `.combo-arrow` と同じ）。
* ファイル名に色のハッシュを入れるため、**明暗を切り替えても取り違えない**。
* `invalidate_cache()` に `_asset_cache.clear()` を足す（テーマ貼り替えで引き直す）。
  ファイル自体は残るが、同じ色なら同じ名前になるため増え続けない。

描画（いずれも `QPixmap` + `QPainter`、`Antialiasing` 有効）:

| アセット | 図形 | 一辺 |
|---|---|---|
| `check_<色>.png` | 3 点の折れ線 `(0.20,0.52) → (0.42,0.74) → (0.80,0.26)`。線幅は一辺の 0.18、端と角は丸 | `CHECK_INDICATOR_PX × _ASSET_SCALE` = 42px |
| `arrow_<色>.png` | 3 点の三角 `(0.08,0.30) → (0.92,0.30) → (0.50,0.74)` の塗り | `COMBO_ARROW_PX × _ASSET_SCALE` = 27px |

QSS 側は実寸（14px / 9px）を指定するため、Qt が縮小して描く。

追加する定数:

```python
# チェック印・ドロップダウン矢印 (ver3 resolve15)。QSS の ::indicator と必ず同じ値にする。
CHECK_INDICATOR_PX = 14
COMBO_ARROW_PX = 9
COMBO_DROPDOWN_WIDTH_PX = 18
# タブ右上コーナーの下余白 (ver3 resolve15 D1)。Qt のレイアウト既定間隔と同じ 6px。
TAB_CORNER_BOTTOM_MARGIN_PX = 6
# ドラッグ&ドロップ領域 (ver3 resolve15 C1)
DROP_AREA = "dropArea"
DROP_GLYPH = "⬇"        # ⬇ ここへ落とす
CLEAR_GLYPH = "✕"       # ✕ 選択を消す
DROP_ICON_PX = 44
```

### 5.4 `theme.py` — コーナーウィジェットの余白（D1）

```python
# タブ右上のコーナーへウィジェットを置く (ver3 resolve15 D1)。
# QTabWidget はコーナーをタブバーの高さいっぱいに置くため、直接入れると
# ボタンの下辺がペインへ接する。余白付きの入れ物で包んでから渡す。
def install_tab_corner(tab_widget, widget, corner=Qt.TopRightCorner):
    holder = QWidget(tab_widget)
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, TAB_CORNER_BOTTOM_MARGIN_PX)
    layout.setSpacing(0)
    layout.addWidget(widget)
    tab_widget.setCornerWidget(holder, corner)
    return holder
```

呼び出し側（`main_window.py:855`）は 1 行の置き換えで済む。

```python
-        self.tabs.setCornerWidget(self.settings_button, Qt.TopRightCorner)
+        theme.install_tab_corner(self.tabs, self.settings_button)
```

ボタン自身への参照（`self.settings_button`）はそのまま使えるため、
有効/無効の切り替え（`_on_tab_running_changed`）とアイコン再描画（`_refresh_settings_icon`）は無改造。

### 5.5 `src/gui/file_drop_area.py`（新規 / C1・C4）

```python
# ドラッグ&ドロップでファイルを選ぶ領域 (docs/request/ver3/resolve15.md §5.5)
#
# 何も選ばれていないときは案内文とアイコンを出し、選ばれたら名前と × ボタンへ変える。
# 落とす操作そのものは親 (タブ) が受け取る。この部品は「今何が選ばれているか」を
# 見せるだけで、ファイルの妥当性判断や実行には関与しない。
class FileDropArea(QWidget):

    cleared = Signal()          # × が押された

    def __init__(self, placeholder, parent=None): ...
    def set_selection(self, name, tooltip=""): ...   # 名前を出し × を表示する
    def clear(self): ...                             # 案内文へ戻し × を隠す
    def has_selection(self): ...
    def set_drop_active(self, active): ...           # ドラッグ中の枠色 (dropActive)
    def set_busy(self, busy): ...                    # 実行中は × を無効化
    def refresh_theme(self): ...                     # 明暗切り替え時にアイコンを描き直す
```

**構成**（縦中央そろえ）:

```
┌──────────────────────── #dropArea (破線) ────────────────────────┐
│                                                                  │
│                            ⬇  (44px)                             │  ← 未選択のときだけ表示
│                                                                  │
│              ドラッグ＆ドロップでファイルを選択                   │
│      （選択後）  2026-09-03_stream.mp4    [ ✕ ]                  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

* 外枠は `setObjectName(theme.DROP_AREA)` + `setAttribute(Qt.WA_StyledBackground, True)`
  （`theme.mark_panel` と同じ作法。QSS の `#dropArea` が当たる）。
* アイコンは `theme.glyph_icon(theme.DROP_GLYPH, theme.color("text.secondary"), theme.DROP_ICON_PX)`
  から `QPixmap` を取り出して `QLabel` へ載せる。**選択後は隠す**（名前を主役にする）。
* 文言の `QLabel` は中央そろえ。長い名前は `QFontMetrics.elidedText` で中央省略し、
  **フルパスを `setToolTip` に入れる**（名前だけでは同名ファイルを見分けられないため）。
* × は `QPushButton` に `theme.mark_icon_button` を当て、
  アイコンは `theme.glyph_icon(theme.CLEAR_GLYPH, ...)`。`setToolTip("選択を取り消す")`。
* `cleared` を出すだけで、**自分では消さない**（消すかどうかは親が決める / §5.6）。

### 5.6 `ClipTabWidget` — 画面構成と選択モデル（C1〜C7 / X2 / X3）

#### 5.6-1 `_build_ui` の並び

```python
def _build_ui(self):
    root = QVBoxLayout(self)

    # ① 上部 60%: ドラッグ&ドロップ領域 (C1)
    self.drop_area = FileDropArea("ドラッグ＆ドロップでファイルを選択")
    self.drop_area.cleared.connect(self._clear_selection)
    root.addWidget(self.drop_area)

    # 隠したファイル選択欄と参照ボタン (C2)。消さずに残し、設定で戻せるようにする。
    # ui.main_window.show_file_row = true にすると従来どおりの行が出る。
    self.input_edit = QLineEdit()
    self.input_edit.setPlaceholderText("動画ファイルをここにドラッグ&ドロップ、または「参照...」")
    self.input_edit.editingFinished.connect(self._on_input_edited)
    self.browse_button = QPushButton("参照...")
    self.browse_button.clicked.connect(self._on_browse)
    input_row = QHBoxLayout()
    input_row.addWidget(self.input_edit)
    input_row.addWidget(self.browse_button)
    root.addLayout(input_row)
    show_row = theme.main_window_config(self._settings)["show_file_row"]
    self.input_edit.setVisible(show_row)
    self.browse_button.setVisible(show_row)

    # ② 続きから (C6)。押すと一覧を開く = 既存「一覧...」と同じ動作。
    # 幅は実行ボタンと同じにする (C8)。中央に縦へ 2 つ積むため、幅が違うと
    # 左右の端が揃わず不揃いに見える。塗りは付けない (主要動作は実行ボタンだけ)。
    self.resume_button = QPushButton("続きから")
    self.resume_button.setToolTip("保存した Timeline プロジェクトを一覧から選びます")
    self.resume_button.setFixedWidth(theme.PRIMARY_ACTION_BUTTON_WIDTH_PX)
    self.resume_button.clicked.connect(self._open_library)
    root.addLayout(self._centered(self.resume_button))

    # ③ 実行 (C7)。デザインは従来のまま (アクセント塗り・幅 96px) で中央へ置く。
    self.run_button = QPushButton()
    self.run_button.clicked.connect(self._on_run)
    theme.setup_primary_action_button(self.run_button, theme.RUN_GLYPH, "実行")
    root.addLayout(self._centered(self.run_button))

    # ④ 進捗バー / ⑤ 実行状況ラベル (従来のまま)
    self.progress_bar = QProgressBar()
    self.progress_bar.setRange(0, 100)
    root.addWidget(self.progress_bar)
    self.status_label = QLabel("待機中")
    root.addWidget(self.status_label)
    ...スピナーは従来どおり...


# 1 個のボタンを画面中央に置く行を作る (C6 / C7)
def _centered(self, widget):
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(widget)
    row.addStretch(1)
    return row
```

`ProjectResumeRow` の生成と 3 本のシグナル接続（`main_window.py:509-514`）は**削除**する（C5）。

**ボタン幅について（C8）**

「続きから」と実行ボタンは**画面中央に縦へ 2 つ並ぶ**ため、幅が違うと左右の端が揃わない。
そこで既存の `theme.PRIMARY_ACTION_BUTTON_WIDTH_PX`（96px）を「続きから」にも使う。
**新しい定数は作らない**——2 つの幅を別々に持つと、片方だけ直したときに再びずれるため。

| ボタン | 文字幅（9pt / 4 文字） | 左右の余白 + 枠 | 必要幅 | 指定幅 |
|---|---|---|---|---|
| 続きから | 約 48px | 14 + 14 + 2 = 30px | **約 78px** | **96px** |
| 実行（記号 ▶） | アイコン 18px | 6 + 6 + 2 = 14px | 約 32px | 96px |

余裕は約 18px あるため、**文字が切れることはない**。
将来もっと長い文言に変える場合は、この定数を上げれば**両方が同時に**広がる。

`theme.PRIMARY_ACTION_BUTTON_WIDTH_PX` のコメントは
「主要動作ボタンの幅」から「**画面中央に縦へ積むボタンの共通幅**」へ書き換える
（アーカイブ用タブの「採点開始」も従来どおりこの値を使う）。

#### 5.6-2 上部 60% の追従（C1 / §3-4）

```python
# D&D 領域を「タブページの高さ × 比率」に合わせる (resolve15 §3-4)。
# 下の部品 (続きから・実行・進捗・ラベル) が入らなくなる場合は、そちらを優先して縮める。
def resizeEvent(self, event):
    super().resizeEvent(event)
    self._apply_drop_zone_height()


def _apply_drop_zone_height(self):
    cfg = theme.main_window_config(self._settings)
    minimum = cfg["drop_zone_min_height_px"]
    target = max(minimum, int(self.height() * cfg["drop_zone_ratio"]))
    # 残りの部品が要る高さ (レイアウトの最小高から D&D 領域ぶんを引く)
    rest = self.layout().minimumSize().height() - self.drop_area.minimumHeight()
    if self.height() - target < rest:
        target = max(minimum, self.height() - rest)
    self.drop_area.setFixedHeight(target)
```

* 子の固定高を変えるだけなので、親の大きさは変わらず**再帰しない**。
* `minimumSize()` はレイアウトが持つ実際の最小高で、
  ボタンやラベルの実測に追随する（フォントサイズが変わっても正しく効く）。

#### 5.6-3 選択モデル（X2 / X3）

```python
# 選択は 1 つだけ (resolve15 §3-5)。("video" | "project", パス) か (None, "")。
# 文言・× ・実行ボタンはすべてここを見る。
_KIND_VIDEO = "video"
_KIND_PROJECT = "project"
```

| メソッド | 動き |
|---|---|
| `_select_video(path)` | `_selection = (_KIND_VIDEO, path)` / `input_edit.setText(path)` / `drop_area.set_selection(basename, path)` |
| `select_project(path)` | 種別を確かめてから `_selection = (_KIND_PROJECT, path)` / `drop_area.set_selection(basename, path)` / `input_edit.clear()` |
| `_clear_selection()` | `_selection = (None, "")` / `input_edit.clear()` / `drop_area.clear()` / ステータスを「待機中」へ |

**どれも最後に相手側を消す**ため、2 つが同時に立つことはない。
`select_project` を公開名にしているのは `MainWindow` から呼ぶため（§5.7 / X5）。

#### 5.6-4 実行の振り分け（X3）

```python
# 実行ボタン: 選ばれているものに応じて経路を分ける (resolve15 §3-5)
def _on_run(self):
    kind, path = self._selection
    if kind is None:
        QMessageBox.warning(
            self, "入力エラー",
            "動画ファイルをドラッグ&ドロップするか、「続きから」でプロジェクトを選んでください。")
        return
    if kind == _KIND_PROJECT:
        if not os.path.exists(path):
            QMessageBox.warning(self, "開けません",
                                f"プロジェクトファイルが見つかりません。\n{path}")
            return
        self._start_resume(path)      # 既存の再編集経路 (無改造)
        return
    if not os.path.exists(path):
        QMessageBox.warning(self, "入力エラー", "存在する入力動画を指定してください。")
        return
    self._run_pipeline(path)          # 既存 _on_run の中身をそのまま切り出したもの
```

`_run_pipeline(path)` は現行 `_on_run`（`main_window.py:683-724`）の
入力チェック以降を**そのまま**移したもの。ブリッジの組み立て・ワーカー起動は 1 行も変えない。

#### 5.6-5 一覧・D&D からの選択（C6 / C3 / C4）

```python
# 「続きから」= 既存「一覧...」と同じ動作 (C6)。
# 一覧が無効な設定 (timeline.project.library.enabled = false) のときは
# ファイル選択ダイアログへ落とす (ボタンが死ににならないようにする)。
def _open_library(self):
    self._settings = load_settings()
    if not timeline_config(self._settings)["project"]["library"]["enabled"]:
        path = self._browse_project()
        if path:
            self.select_project(path)
        return
    dialog = ProjectLibraryDialog(project_io.KIND_CLIP, self._settings, parent=self)
    dialog.open_requested.connect(self.select_project)   # 選ぶだけ。実行はしない (§3-6)
    dialog.exec()


# ドロップ: 動画 → 入力、プロジェクト → 続きから。どちらも「選ぶだけ」(C3 / C4)
def dropEvent(self, event):
    self.drop_area.set_drop_active(False)
    project = self._dropped_project_path(event)
    if project:
        self.select_project(project)
        event.acceptProposedAction()
        return
    path = self._dropped_video_path(event)
    if path:
        self._select_video(path)
        event.acceptProposedAction()
    else:
        event.ignore()
```

`dragEnterEvent` は受理できるときに `drop_area.set_drop_active(True)`、
`dragLeaveEvent` で `False` へ戻す（枠がアクセント色になる = 落とせる合図）。
受理条件（`_is_acceptable_drop` / 実行中は拒否・拡張子判定）は**現行のまま**。

`_on_library_open` と `_select_project`（`main_window.py:608-610 / 675-680`）は
`select_project` に統合されて消える。`_refresh_recent_projects` は
`resume_row` を持たなくなるため「設定を読み直すだけ」に変える
（一覧ダイアログへ最新の履歴を渡すために必要 / `_set_running(False)` から呼ばれる）。

`_set_running` の対象も差し替える。

```python
-        self.browse_button.setEnabled(not running)
-        self.input_edit.setEnabled(not running)
-        self.resume_row.set_busy(running)
+        self.browse_button.setEnabled(not running)   # 隠していても状態は揃えておく
+        self.input_edit.setEnabled(not running)
+        self.resume_button.setEnabled(not running)
+        self.drop_area.set_busy(running)
```

### 5.7 `MainWindow` の変更

| # | 変更 | 理由 |
|---|---|---|
| 1 | `self.resize(cfg["width_px"], cfg["height_px"])` を `_build_ui` の後に呼ぶ | 比率の基準になる高さを与える（X1 / §2.7） |
| 2 | `theme.install_tab_corner(self.tabs, self.settings_button)` へ置き換え | 設定ボタンの下余白（D1 / §5.4） |
| 3 | `_switch_to_kind` を `resume_row` 直参照から `tab.select_project(path)` へ | `resume_row` を消したクリップ用タブでも動く（X5 / §2.8） |
| 4 | `setAcceptDrops(True)` + `dragEnterEvent` / `dropEvent` を現在のタブへ転送 | タブバーの帯へ落としても選べる = 「画面のどこにでも」（C3） |

3 は次のようにする。アーカイブ用タブにも同名のメソッドを足す
（`ArchiveTabWidget.select_project(path)` → `self.resume_row.select(path)` を呼ぶ 3 行）。

```python
def _switch_to_kind(self, kind, project_path):
    tab = (self.archive_tab if kind == project_io.KIND_ARCHIVE else self.clip_tab)
    selector = getattr(tab, "select_project", None)
    if selector is None:
        return
    self.tabs.setCurrentWidget(tab)
    selector(project_path)
```

4 は次のとおり。子（タブ）が受理した場合は親まで来ないため、**二重に選ばれることはない**。

```python
# タブバーの帯など、タブの中身の外へ落とされたぶんを現在のタブへ回す (C3)
def dragEnterEvent(self, event):
    tab = self.tabs.currentWidget()
    handler = getattr(tab, "dragEnterEvent", None)
    if handler is None:
        event.ignore()
        return
    handler(event)
```

`dropEvent` も同じ形で転送する。

### 5.8 `project_resume_row.py` — 種別チェックの共有

`ProjectResumeRow._check_kind`（`project_resume_row.py:166-188`）の中身を
module 関数へ出し、クラスからも呼ぶ。**振る舞いは変えない**。

```python
# 種別が違うプロジェクトを開こうとしていないか確かめる (ver3 resolve9 §3-4)。
# クリップ用とアーカイブ用は開く経路も書き出し方も違うため、取り違えると壊れる。
# 戻り値: (種別が一致したか, 実際の種別)。一致しないときは案内を出したうえで種別を返す。
def confirm_project_kind(parent, path, kind):
    ...現行 _check_kind と同じ処理...
```

クリップ用タブの `select_project` はこれを使う。
一致しなければ `switch_tab_requested` を出して相手のタブへ回す（従来と同じ流れ）。

### 5.9 変わらないもの（意図的に触らない）

| 対象 | 理由 |
|---|---|
| `src/gui/archive_tab.py` の画面構成 | 要望はクリップ用タブのみ。`select_project` の 3 行だけ足す |
| `ProjectResumeRow` 本体 | アーカイブ用タブが使い続ける。種別チェックの切り出し以外は無改造 |
| `ProjectLibraryDialog` | 一覧そのものは変えない。**受け側**が実行しなくなるだけ |
| パイプライン・ワーカー・各ブリッジ | `_run_pipeline` / `_start_resume` の中身は 1 行も変えない |
| プログレスバー・実行状況ラベル・スピナー | 要望どおり「デザインは既存と同じ」（C7） |
| 表・一覧の中のチェック印（`Qt.ItemIsUserCheckable`） | `QCheckBox::indicator` とは別のサブコントロール。現状どおり Qt が描く（§2.9） |
| `src/comment_icon.png` などの画像素材 | 増やさない。記号は実行時に描く（§3-1） |
| 設定画面・Timeline 編集画面の画面構成 | QSS の共通部分（矢印・チェック印）だけが変わる |

---

## 6. 実装手順

1. `theme.py` に定数・QSS 規則・アセット生成・`install_tab_corner`・`DEFAULT_UI_SETTINGS.main_window` を足す（§5.2〜§5.4）。
2. `setting.json` へ `ui.main_window` を書く（§7）。
3. `src/gui/file_drop_area.py` を作る（§5.5）。
4. `project_resume_row.py` の種別チェックを関数へ出す（§5.8）。
5. `ClipTabWidget` を組み替える（§5.6）。実行の中身は移すだけで書き換えない。
6. `MainWindow` の 4 点と `ArchiveTabWidget.select_project` を直す（§5.7）。
7. `docs/layout/theme.css` / `docs/layout/main_window.html` を実装へ追随させる
   （`.corner` の余白 4→6px、クリップ用タブの構成、`.combo` の注記）。
8. テストを足す（§10-2 / §10-3）。
9. 実機で §10-1 を確認する。

---

## 7. setting.json 定義（追加分）

置き場所の方針:

| 種類 | 置き場所 | 理由 |
|---|---|---|
| ウィンドウの大きさ・比率（利用者が変えたくなる値） | **`setting.json`** | `timeline.ui.window_width` と同じ扱い |
| 記号・アイコンの px、余白などのデザイン定数 | **`theme.py` の定数** | `PRIMARY_ACTION_BUTTON_WIDTH_PX` / `BUTTON_ICON_PX` と同じ扱い。QSS の値と対で意味を持つため、離すと食い違う |

`ui` セクションへ次を足す（`theme.DEFAULT_UI_SETTINGS` にも同じ既定を書く）。

```json
"main_window": {
    "width_px": 700,
    "height_px": 520,
    "drop_zone_ratio": 0.60,
    "drop_zone_min_height_px": 140,
    "show_file_row": false
}
```

| キー | 既定 | 意味 |
|---|---|---|
| `width_px` | 700 | メイン画面の初期幅。現行の最小幅（実測 680）より広くしないと効かない（§2.7） |
| `height_px` | 520 | メイン画面の初期高。比率の基準になる |
| `drop_zone_ratio` | 0.60 | D&D 領域が占めるタブページ高の比率（要望の「上部 60%」）。当初 0.40 だったが、画面下部に余白が残るため追加要望で 0.60 へ広げた |
| `drop_zone_min_height_px` | 140 | 縮めたときの下限。これ以上は小さくしない |
| `show_file_row` | false | 隠したファイル選択欄と「参照...」を出すか（C2 の「隠す」を設定で戻せるようにしたもの） |

`_merge_with_defaults`（`settings_window.py:788-800`）は不足キーを自動で補うため、
**既存の `setting.json` への移行処理は要らない**（新規キーのみのため。
`tests/test_settings_migration.py` の `test_nested_defaults_are_filled` が担保している）。

---

## 8. 互換性・非破壊の担保

| 観点 | 影響 |
|---|---|
| アーカイブ用タブ | 画面構成は変わらない。チェックとコンボの見た目だけ直る（`select_project` の追加は内部用） |
| 設定画面 / Timeline 編集画面 / 字幕編集画面 | 画面構成は不変。QComboBox の右端の面が消え、チェックに印が付くだけ |
| コンボの**大きさ・位置** | 変わらない。`::drop-down` は既に確保されている領域で、枠と背景を消すだけ |
| チェックの**大きさ・位置** | 変わらない。14px / 角丸 3px / 枠色はそのまま |
| チェックの**動作** | 変わらない。見た目だけ |
| 実行・再編集の処理 | 変わらない。入口が実行ボタン 1 つに集まるだけで、`PipelineWorker` / `ProjectResumeWorker` の呼び方は同じ |
| 保存済みプロジェクト | 影響なし。読み書きに触れていない |
| 履歴（`timeline.project.recent`） | 影響なし。書き込みは Timeline 編集画面側。クリップ用タブは**表示しなくなるだけ**で、一覧からは従来どおり選べる |
| D&D の受理条件 | 変わらない。拡張子判定（`_VIDEO_EXTENSIONS` / `project_suffix`）と実行中の拒否はそのまま |
| ライト/ダークの切り替え | 追随する。アセットは色ごとにキャッシュし、`invalidate_cache()` で引き直す（§5.3） |
| `ui.theme = "system"` | 影響なし。QSS を当てないため、素の Qt が従来どおりチェック印も矢印も描く |
| アセットが作れない環境 | 起動できる。印は現行どおり塗りのみ、矢印は素のスタイルへ落ちる（§3-2） |

---

## 9. 確認事項

| # | 内容 | 本書の判断 |
|---|---|---|
| **Q1** | 「上部 60%」は**タブの中身（ペインの内側）の高さ**に対する 60% という理解でよいか | その前提で設計した。ウィンドウ全体（タイトルバー・タブバーを含む）を基準にすると、見えている領域では 60% より小さく見える |
| **Q2** | メイン画面の**初期サイズ 700×520** でよいか | 現行は 680×303（実測）。60% を意味のある大きさ（実測 280px）にするための値。`ui.main_window` で変えられる |
| **Q3** | ウィンドウを小さくして 60% が取れなくなったとき、**下の部品（実行ボタン等）を優先**して D&D 領域を縮めてよいか | 優先する。実行ボタンが見えなくなる方が実害が大きい（§3-4） |
| **Q4** | 「続きから」で一覧から選んだあと、**実行ボタンを押して初めて処理が始まる**形でよいか | その形にした。要望文が「選択すると文言に名前が表示される」までしか書いていないため（§3-6） |
| **Q5** | 動画とプロジェクトは**同時に選べない**（後から選んだ方で置き換わる）形でよいか | 排他にした。文言も実行ボタンも 1 つのため（§3-5） |
| **Q6** | D&D のアイコンは **Unicode の記号（⬇）**でよいか。専用の絵を用意するか | 記号にした。既存の設定・実行ボタンと同じ方式（`theme.glyph_icon`）で素材を増やさない。絵にしたい場合は素材をもらえれば差し替える |
| **Q7** | 実行状況ラベルも**中央寄せ**にするか | しない。要望が「デザインは既存と同じ」のため左寄せのまま。中央寄せの指定があるのはボタン 2 つだけ（C6 / C7） |
| **Q8** | 隠したファイル選択欄を `ui.main_window.show_file_row` で**戻せるようにする**方針でよいか | そうした。要望は「隠す」であって「無くす」ではないため、コードを消さずに設定で切り替える |
| **Q9** | クリップ用タブから「編集の続き」行が消えることで、**履歴（最近使ったプロジェクト）が画面に出なくなる**が問題ないか | 一覧（「続きから」）に同じものが並ぶため機能は失われない。ワンクリックで直近を開く導線だけが無くなる |

---

## 10. テスト計画

### 10-1. 実機確認

| # | 手順 | 期待 |
|---|---|---|
| 1 | アプリを起動する | ウィンドウが 700×520 で開き、**設定ボタンの下にペインとの隙間**がある（D1） |
| 2 | 設定画面を開き、チェックボックスを 1 つ入れる | オレンジの塗りの上に**チェックマークが出る**（D2） |
| 3 | 同じ画面のコンボを見る | 右端に**押しボタンの面が無く**、`text.secondary` の三角だけがある（D3 / カタログと同じ） |
| 4 | OS をライト↔ダークで切り替える | チェック印と矢印の色が追随する |
| 5 | クリップ用タブを見る | 上部に破線の領域があり「ドラッグ＆ドロップでファイルを選択」と ⬇ が中央に出る（C1）。ファイル選択欄と「参照...」は**見えない**（C2）。「編集の続き」行が**無い**（C5） |
| 6 | 動画をタブの**空いている場所・ラベルの上・タブバーの帯**へ落とす | どこでも受理され、文言がファイル名に変わる（C3 / C4）。ドラッグ中は枠がアクセント色になる |
| 7 | ファイル名の右の × を押す | 選択が消え、文言と ⬇ が戻る（C4） |
| 8 | 「続きから」を押す | プロジェクト一覧が開く（既存「一覧...」と同じ画面 / C6） |
| 9 | 一覧で 1 つ開く | **実行は始まらず**、文言がプロジェクト名に変わる（C6 / Q4） |
| 10 | その状態で実行ボタンを押す | 「編集の続き」として再開する（従来の「開く...」と同じ結果 / X3） |
| 11 | 動画を選んだ状態で実行ボタンを押す | 従来どおりパイプラインが動く |
| 12 | 動画を選んだあとに「続きから」でプロジェクトを選ぶ | 文言がプロジェクト名に置き換わり、実行はプロジェクト側になる（Q5） |
| 13 | ウィンドウを縦に伸縮する | D&D 領域が高さの 60% を保つ。縮めても実行ボタン・進捗・ラベルは消えない（Q3） |
| 14 | 実行中にドラッグしてみる | 受理されない（従来どおり）。× も押せない |
| 15 | アーカイブ用タブでクリップ用プロジェクトを選ぶ | クリップ用タブへ切り替わり、**文言にその名前が入る**（X5） |
| 16 | アーカイブ用タブの画面 | 構成は従来のまま。チェックと矢印だけ直っている |
| 17 | `ui.theme` を `"system"` にして起動 | 素の Qt の見た目へ戻る（チェック印も矢印も OS 標準） |

### 10-2. `tests/test_theme.py` への追加

| # | テスト | 検証内容 |
|---|---|---|
| 1 | `test_combo_dropdown_is_flat` | QSS に `QComboBox::drop-down` があり `border: none` と `background: transparent` を含む（D3） |
| 2 | `test_checked_indicator_has_image` | QSS に `QCheckBox::indicator:checked` の `image:` がある（D2）。アセットが作れない環境ではスキップ |
| 3 | `test_indicator_assets_are_created` | `_indicator_assets` が実在するパスを 2 つ返し、同じ設定で 2 回呼ぶと**同じパス**（キャッシュ） |
| 4 | `test_indicator_assets_differ_by_mode` | ライトとダークで**別のファイル名**になる（色の取り違えが起きない） |
| 5 | `test_qss_without_assets_still_valid` | アセット生成を失敗させても `build_qss` が例外を出さず、`image:` を含まない QSS を返す（§3-2） |
| 6 | `test_no_placeholder_remains`（既存） | 追加したプレースホルダも解決されること（既存テストがそのまま担保する） |
| 7 | `test_default_settings_contains_ui` の隣に追加 | `DEFAULT_UI_SETTINGS["main_window"]` の 5 キーが揃っている |

### 10-3. `tests/test_clip_tab_selection.py`（新規）

`QT_QPA_PLATFORM=offscreen` で `ClipTabWidget` を作って確かめる
（`src.gui.main_window` の import は実測 0.9 秒で、テストに乗せられる）。

| # | テスト | 検証内容 |
|---|---|---|
| 1 | `test_file_row_is_hidden` | `input_edit` / `browse_button` が非表示（C2）。`show_file_row: true` にすると表示される |
| 2 | `test_video_selection_shows_name` | `_select_video` で D&D 領域の文言がファイル名になり、× が出る（C4） |
| 3 | `test_clear_restores_placeholder` | × 相当（`_clear_selection`）で文言が案内文へ戻り、`_selection` が空（C4） |
| 4 | `test_selection_is_exclusive` | 動画 → プロジェクトの順に選ぶと `_selection` がプロジェクトだけになる（X2 / Q5） |
| 5 | `test_run_dispatches_by_kind` | `_start_resume` / `_run_pipeline` を差し替え、選択の種別どおりに呼ばれる（X3） |
| 6 | `test_run_without_selection_warns` | 未選択で実行しても**ワーカーが起動しない**（`QMessageBox` は差し替えて確認） |
| 7 | `test_drop_zone_follows_ratio` | 高さ 500 にすると D&D 領域が 300（= 0.60）になる |
| 8 | `test_drop_zone_respects_minimum` | 高さ 200 にしても `drop_zone_min_height_px` を下回らず、他の部品の最小高が確保される（Q3） |
| 9 | `test_resume_row_is_gone` | クリップ用タブに `resume_row` 属性が無く、`select_project` がある（C5 / X5） |
| 10 | `test_center_buttons_share_width` | 「続きから」と実行ボタンの幅が**一致**し、どちらも `PRIMARY_ACTION_BUTTON_WIDTH_PX` である（C8） |
| 11 | `test_resume_label_fits_in_width` | 「続きから」の文字が指定幅に収まる（`sizeHint().width() <= 96`）。フォントが変わっても切れないことの担保 |

### 10-4. 既存テストの回帰

```
python -m unittest discover -s tests
```

`tests/test_theme.py`（QSS の括弧の釣り合い・プレースホルダ残り・コントラスト比）と
`tests/test_settings_migration.py`（既定の入れ子補完）が通ること。

---

## 11. 将来拡張（本書では実装しない）

| # | 内容 | 備考 |
|---|---|---|
| **F1** | アーカイブ用タブも同じ D&D 領域にする | `FileDropArea` は種別を知らない部品として作るため、そのまま載せられる。入力ソース（ローカル / Twitch VOD）の切り替えと並べ方の整理が要る |
| **F2** | 直近のプロジェクトを 1 クリックで開く導線 | 「編集の続き」のコンボが無くなったぶん（Q9）。D&D 領域の下に「最近のプロジェクト」を数件並べる案 |
| **F3** | 生成アセットの後片付け | 一時フォルダに色ごとの PNG が残る（1 色あたり 2 ファイル・数百バイト）。アクセント色を何度も変えると増えるため、起動時に古いものを消す掃除を入れてもよい |
| **F4** | D&D 領域に**サムネイル**を出す | 選んだ動画の 1 コマを出すと取り違えが減る。`project_thumbnail.py` の仕組みが流用できる |
| **F5** | 部品カタログ（`docs/layout/`）の自動追随 | 今回のように実装とカタログを手で揃えているため、QSS からカタログの CSS を生成する仕組みがあると差が出ない |
