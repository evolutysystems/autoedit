# テロップの縁取りの白がそのまま背景になる 原因と修正設計書

現象の報告: `docs/error/20261122/error.md`

本書は `docs/CLAUDE.md`「実装前に設計を行うこと」に従う **設計書** である。

対象動画: `C:\Users\evolu\Documents\AutoEdit\output\archive_vod_2930581402_combined.mp4`
対象設定: `%LOCALAPPDATA%\Programs\AutoEdit\_internal\src\settings\setting.json`
対象ログ: `%LOCALAPPDATA%\Programs\AutoEdit\logs\autoedit_20261121.log`

---

## 1. 結論（先に要点）

**アプリの不具合で色が変わったのではない。利用者の `setting.json` の
`subtitle.border_style` が `1`（縁取り + ドロップシャドウ）から
`3`（不透明ボックス）へ変わっており、ASS の `BorderStyle=3` は
「背景色 (`back_color`)」ではなく**縁の色 (`outline_color`)**で箱を塗る**ため、
白い縁取り (`&H00FFFFFF`) がそのまま白い箱になっていた。

```
subtitle.border_style = 3        ← これが原因
subtitle.outline_color = &H00FFFFFF   (白)
subtitle.outline_width = 10
subtitle.back_color = &H64000000      (半透明の黒 / 今回は影にしか効かない)
```

そして**その値になったことを利用者が知る手段がアプリの中に 1 つも無い**。

* 設定画面の「縁取りスタイル」は `QComboBox` で、**カーソルが載っているだけで
  ホイール 1 ノッチで値が変わる**（実測。後述 §2.4）。選択肢は 2 つしかないため、
  1 ノッチで `1` → `3` へ飛ぶ。
* Timeline のプレビューは `border_style` を**一切見ていない**ため、画面では
  これまでどおり縁取りで表示される（§2.5）。
* 焼き込み側もログに何も残さない。

したがって「**気付けないまま 1 本まるごと焼き上がる**」。

---

## 2. 証拠

### 2.1 出力フレーム

`archive_vod_2930581402_combined.mp4` の 45 秒地点。赤文字の背後に、
**行ごとに独立した**白い不透明な矩形が出ている。
行ごとに別の箱になるのは `BorderStyle=3` の描き方そのもの
（箱は 1 行ずつの外接矩形に付く / `ver3 resolve11 §2.8` の表と一致）。

### 2.2 設定値（インストール版・現物）

```
subtitle.border_style  = 3        ← 既定は 1
subtitle.outline_color = &H00FFFFFF
subtitle.outline_width = 10
```

リポジトリの既定（`src/settings/setting.json` / `DEFAULT_SETTINGS`）は `1`。
配布物へ同梱される設定（ビルド時に作る `stretheus-shipped-settings-*`）も `1`。
**利用者の設定だけが `3`** だった。

### 2.3 libass の挙動（再現して確認）

`BorderStyle` 以外をまったく同じにした 2 つの Style を 1 枚へ焼いた。

```
Style: Box,Arial,40,&H000000FF,&H00FFFFFF,&H64000000,...,3,10,1,...
Style: Out,Arial,40,&H000000FF,&H00FFFFFF,&H64000000,...,1,10,1,...
```

* `BorderStyle=3` → **白い矩形**（= `OutlineColour` で塗りつぶし）
* `BorderStyle=1` → 白い縁取り

`BackColour`（設定画面の「背景色」）は `BorderStyle=3` でも**箱の色にはならず、
影の色にしか効かない**。これは ASS/libass の仕様であり、
`ver1/resolve.md:87` の対応表（`back_color` → `BackColour`）どおりの挙動である。
つまり**設定画面の見た目から想像できる挙動と食い違っている**
（「不透明ボックス」を選ぶと、背景色ではなく縁の色が箱の色になる）。

### 2.4 いつ `3` になったか

| 時刻 | ファイル | `border_style` |
|---|---|---|
| 2026-11-15 19:28 | `setting.json.bak_20261115`（アプリが作った控え） | **1** |
| 2026-11-15 20:12 | `setting.user.json`（更新インストール時の控え） | **3** |
| 2026-11-21 23:53 | 現行の `setting.json` | **3** |

この 44 分のあいだに変わった値は 3 つだけで、
`ffmpeg.output_width` 606 → 1920（= `ver11 resolve2` の焼き付き修正を確認した操作）、
`subtitle.border_style` 1 → 3、`timeline.ui.window_geometry.clip`（窓の位置）である。
**設定画面を開いて保存した 1 回の操作の中で、意図せず `border_style` が動いた**
と読むのが自然である。

`QComboBox` は Windows 既定でホイールが効く。実測:

```
QComboBox に項目 (1, 3) を入れ、ホイール 1 ノッチぶんのイベントを送る
→ currentData() は 1 から 3 へ変わり、currentIndexChanged と activated の
   両方が飛ぶ
```

設定画面は保存ボタンを押した時点で `border_style_combo.currentData()` を
そのまま書くため（`settings_window.py:3640`）、**ホイールが当たったことに
気付かずに保存すると、そのまま `3` が永続化される**。
設定画面には `wheelEvent` を潰す仕組みも、`QScrollArea` も無い
（`grep wheelEvent src/settings` の該当は 0 件）。

以降は更新インストールが利用者の `setting.json` を引き継ぎ、
`_merge_with_defaults` も利用者値を温存するため、v1.8.5 / v1.8.6 でも `3` のまま残る。

### 2.5 なぜ画面で気付けないか

`src/gui/timeline/preview_items.py` に `border_style` は**出てこない**
（`grep border_style src/gui` の該当は 0 件）。プレビューは常に
「縁取り + 影」で描くため、設定が `3` でもプレビューは今までどおりに見える。
**プレビューと出力が食い違う**のが、この事故が最後まで見つからなかった直接の理由。

---

## 3. 「前も似たような事例を修正したはず」との関係

利用者の記憶は正しい。ただし**直した範囲がここに届いていない**。

| 過去の修正 | 直した所 | 今回に効かない理由 |
|---|---|---|
| `ver9/resolve10.md §2.4 / §3.5`<br>インスペクタのコンボがホイール・矢印で動き、テロップが勝手に S1 へ移る | Timeline インスペクタの**役割コンボ**を `currentIndexChanged` → `activated` へ | ① 対象が Timeline インスペクタだけで、**設定画面は対象外**<br>② そもそも `activated` ではホイールを止められない。Qt の `QComboBox::wheelEvent` は値を変えたうえで `activated` **も**出す（§2.4 の実測）。「コマンドを飛ばさない」対策であって「値を変えさせない」対策ではなかった |
| `ver11/resolve2`<br>出力キャンバスの解決値が `setting.json` へ焼き付く | `save_settings` の出口で実行時キーを剥がし、起動時に自己修復 | 「保存の出口で守る」考え方は同じだが、`border_style` は**利用者が選べる正規の設定値**なので、出口で勝手に戻すわけにはいかない |

まとめると、**「設定画面のコンボはホイールで勝手に変わる」という穴が
一度も塞がれていなかった**。今回たまたま当たったのが、
出力への影響が最も派手な「縁取りスタイル」だった、という関係である。

---

## 4. 修正方針

### R1. 設定画面の入力ウィジェットをホイールで変えられなくする（再発防止・本丸）

新しく `src/gui/wheel_guard.py` を置き、`QComboBox` と `QAbstractSpinBox`
（= `QSpinBox` / `QDoubleSpinBox`）へ共通のイベントフィルタを入れて
**ホイールイベントを捨てる**。値を変える口は

* クリックして一覧から選ぶ
* フォーカスを当てたうえでの矢印キー

の 2 つに残す。ホイールで設定を変えたい利用者はいないと判断する
（`ver9 resolve10 §3.6 Q3` でも「コンボをホイールで変えられなくして問題ない」と
既に結論が出ている）。

設定画面は `_add_setting_tabs()` がタブを組む唯一の入口で、言語切替
(`retranslate`) もここを通る。**その末尾 1 か所**で入れれば、以後タブや項目が
増えても自動で守られる（`save_settings` の出口 1 か所で守る `ver11 resolve2` と同じ流儀）。

### R2. 焼き込み時にログへ残す（見える化）

`build_subtitle_file` で `border_style == 3` のとき WARNING を 1 行出す。
箱の色が「縁の色」であることも書く。これで次に同じことが起きたときは
ログだけで即断できる（今回は映像から逆算するしかなかった）。

```
縁取りスタイル=不透明ボックス のため、テロップの背後に「縁の色」で塗った箱が出ます
(縁の色=&H00FFFFFF / 設定: subtitle.border_style=3)
```

### R3. 利用者の設定を `1` へ戻す（今回の復旧）

インストール版の `setting.json` の `subtitle.border_style` を `3` → `1` へ戻す。
戻す前のファイルは `setting.json.bak_20261122` として残す。
他の設定値には触れない。

### R4. 採らなかった案

| 案 | 却下理由 |
|---|---|
| `border_style == 3` を起動時に `1` へ自動で直す（`_normalize_legacy_values` へ足す） | `3` は設定画面に出している**正規の選択肢**で、意図して選んだ利用者の設定を奪う。`ver11 resolve2` が直した「実行時にしか意味の無い値」とは性格が違う |
| `BorderStyle=3` のとき `OutlineColour` に `back_color` を書いて「背景色が箱の色」になるようにする | ASS の意味を曲げることになり、`ver1 resolve` の対応表（`back_color` → `BackColour`）と、Resolve 出力・外部の .ass を読む経路との整合が崩れる。見え方の分かりにくさは R5 の注記で解く |
| 「不透明ボックス」を選択肢から消す | 既存利用者が選んでいる可能性がある設定を黙って消せない（`既存実装を破壊しない`） |

### R5. 設計だけ（実装は保留）

以下は**画面の見た目に手を入れる**変更のため、本書までとし実装はしない
（利用者の方針: 画面デザインの変更は設計書まで）。

| 項目 | 内容 |
|---|---|
| P1 | プレビュー (`preview_items.py`) を `border_style` に追従させ、`3` のときは文字の背後に縁の色の矩形を描く。**プレビューと出力の食い違いを無くす本質的な対策** |
| P2 | 設定画面の「縁取りスタイル」の下に注記を 1 行:「不透明ボックスを選ぶと、背景色ではなく**縁の色**で塗った箱が文字の背後に出ます」 |

---

## 5. 変更するファイル

| ファイル | 変更 |
|---|---|
| `src/gui/wheel_guard.py` | **新規**。`QComboBox` / `QAbstractSpinBox` のホイールを捨てるイベントフィルタと、まとめて入れる `install()` |
| `src/settings/settings_window.py` | `_add_setting_tabs()` の末尾で `wheel_guard.install(self.tabs)` を呼ぶ |
| `src/modules/subtitle_generator.py` | `build_subtitle_file` で `border_style == 3` のとき WARNING を出す |
| `tests/test_subtitle_border_style.py` | **新規**。下記 |

## 6. テスト

| # | 確認 |
|---|---|
| T1 | 既定の `border_style` が `1`（`DEFAULT_SETTINGS` / `src/settings/setting.json` の両方） |
| T2 | `border_style` が ASS の Style 行の 15 列目へそのまま出る（`1` / `3`） |
| T3 | `border_style=3` のとき WARNING が出る / `1` のときは出ない |
| T4 | ホイールガードを入れたコンボは、ホイールを送っても値が変わらない（`activated` も飛ばない） |
| T5 | ガードを入れても、クリック相当 (`setCurrentIndex`) では今までどおり値が変わる |
| T6 | ガードはスピンボックスにも効く |
