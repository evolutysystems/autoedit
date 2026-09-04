# 画面レイアウト (HTML / CSS 版)

PySide6 で実装済みの 3 画面のデザイン・レイアウトを HTML/CSS へ置き換えたもの。
デザインの確認・共有用の資料であり、アプリの実装からは参照されない。

`index.html` をブラウザで開くと 3 画面へ移動できる。
各ページ右上の「ライト / ダーク切り替え」で `ui.theme_mode` の両モードを確認できる。

## ファイル構成

| ファイル | 内容 | 実装元 |
| --- | --- | --- |
| `index.html` | 一覧 + デザイントークン + 部品カタログ | `src/gui/theme.py` |
| `theme.css` | 共通スタイル (トークン・部品規則) | `theme.py` の `DARK` / `LIGHT` / `SHAPE` / `TIMELINE` / `_QSS_TEMPLATE` |
| `main_window.html` | メイン画面 (最初の実行画面) | `src/gui/main_window.py`, `src/gui/archive_tab.py`, `src/gui/project_resume_row.py` |
| `timeline_editor.html` | Timeline 編集画面 | `src/gui/timeline/timeline_editor_dialog.py`, `preview_panel.py`, `timeline_view.py` |
| `settings_window.html` | 設定画面 | `src/settings/settings_window.py` |

## 値の出どころ

ハードコードを避けるため、色・角丸・寸法はすべて実装側の定義から写している。

### 色 / 形状 (`theme.css` の CSS 変数)

* `theme.DARK` / `theme.LIGHT` … 背景・ガラス層・テキスト・アクセント
* `setting.json` の `ui` セクション … `accent_color`, `corner_radius_px` (16), `control_radius_px` (10),
  `glass.dark` / `glass.light` の不透明度, `background`, `timeline_backdrop_opacity`
* 派生トークン (`accent.on` / `accent.line` / `danger.line`) は `theme.tokens()` が
  コントラスト比から自動導出した値をそのまま書き写している。
  - ダーク: `accent.line = #E8A15C` / `danger.line = #E8A15C`
  - ライト: `accent.line = #B98049` / `danger.line = #B88049` (明背景で 3:1 を満たすまで暗く寄せた値)
  - 両モード共通: `accent.on = #1B1B1F` (アクセントの上に載せる文字色)
* `theme.TIMELINE` … Timeline の下地・トラック・ルーラ・罫線
* `timeline_view.py` の `_COLOR_*` … クリップ・再生ヘッド・選択枠 (ガラス化の対象外・不透明のまま)

### 部品の余白・角丸 (`theme.css` のクラス)

`theme._QSS_TEMPLATE` を 1 対 1 で置き換えている。

| QSS | CSS |
| --- | --- |
| `#glassPanel` / `#glassPanelStrong` | `.panel` / `.panel-strong` |
| `QWidget[flatPanel="true"]` | `.flat` |
| `#noteLabel` / `#sectionTitle` | `.note` / `.section-title` |
| `QPushButton` (padding 6px 14px / radius 10px) | `.btn` |
| `QPushButton[iconOnly="true"]` (padding 6px 6px) | `.btn--icon` |
| `#primaryButton` / `#primaryButtonSolid` / `#dangerButton` | `.btn--primary` / `.btn--primary-solid` / `.btn--danger` |
| `QLineEdit` ほか入力欄 (padding 4px 8px) | `.field` / `.textarea` / `.combo` |
| `QTabWidget::pane` / `QTabBar::tab` (padding 8px 18px) | `.tabpane` / `.tab` |
| `QTabWidget[firstTabSelected="true"]::pane` | `.tabpane.first-selected` |
| `QProgressBar` / `QSlider` / `QScrollBar` | `.progress` / `.slider` / `.scrollbar` |
| `QCheckBox::indicator` (14px / radius 3px) | `.check .box` |
| `QSplitter::handle` | `.splitter-h` / `.splitter-v` |
| `#previewCanvas` (常に黒) | `.preview-canvas` |

### 寸法

| 値 | 出どころ |
| --- | --- |
| Timeline ウィンドウ 1280 x 820 | `setting.json timeline.ui.window_width / window_height` |
| 上下分割比 55 : 45 | `timeline.ui.split_ratio` = 0.55 |
| トラック高 56px / 字幕トラック高 40px | `timeline.ui.track_height_px` / `subtitle_track_height_px` |
| トラックヘッダ幅 88px | `timeline.ui.header_width_px` |
| ルーラ高 26px | `timeline_view.RULER_HEIGHT` |
| ズームボタン幅 40px | `timeline.ui.zoom_button_width_px` |
| 実行 / 採点開始ボタン幅 96px | `theme.PRIMARY_ACTION_BUTTON_WIDTH_PX` |
| 設定ウィンドウ 300 x 760 | `settings_window.WINDOW_WIDTH / WINDOW_HEIGHT` |
| 項目名ラベル 100px / 入力欄 160px | `settings_window.COLUMN_LABEL_WIDTH / INPUT_FIELD_MIN_WIDTH` |
| カラー欄 104px / アウトラインカラー欄 140px | `COLOR_FIELD_MIN_WIDTH` / `OUTLINE_COLOR_FIELD_MIN_WIDTH` |
| Qt レイアウトの既定余白 9px / 間隔 6px | `QVBoxLayout` の既定値 |

## 実装との差異 (意図的なもの)

Qt と HTML の描画モデルの違いから、以下だけは近似になっている。

* **背景の光球** — 実装は `QRadialGradient` を `max(幅, 高さ) x 半径比` で描く。
  CSS では中心を `left` / `top` で置き、`transform: translate(-50%, -50%)` で中心合わせし、
  直径を親幅の 124% (= 0.62 x 2) / 116% (= 0.58 x 2) とした正円で再現している。
  縦長の設定ウィンドウだけは高さ基準になるため `.glow--tall` を併用する。
* **すりガラス** — Qt Widgets には `backdrop-filter` に相当する機能が無く、実装は
  「半透明 + 境界線 + ハイライト」で見立てている。CSS 版も同じ見立てに揃え、
  `backdrop-filter` は使っていない (実装より綺麗に見えてしまうため)。
* **設定ウィンドウの横幅** — 実装値の 300px は「項目名ラベル + 入力欄」で決まる最小幅で、
  役割別カラー行 (3 列) は実際には横へ広げて使う。ページ上は中身が収まる 520px で描き、
  最小幅の内訳を注記している。
* **メイン画面の横幅** — 実装は `resize()` を呼ばず中身に合わせて開くため、
  ページ上は代表値として 620px を指定している。
* **スピナー / 波形 / 高精度プレビュー** — 動きや実データを伴う表示は静的な見立てに置き換えている。

## 未収録の画面

今回の対象は「メイン → Timeline 編集 → 設定」の 3 画面。
以下は同じトークン・同じ部品規則で構成される派生・付随画面のため含めていない。

* アーカイブ用 Timeline 編集 (`src/gui/timeline/archive_timeline_dialog.py`)
  — Timeline 編集画面を `QMainWindow` で包み、上部へ採点グラフのドックを足したもの
* 字幕編集 (`src/gui/subtitle_editor_dialog.py`)
* プロジェクト一覧 (`src/gui/project_library_dialog.py`)
* アーカイブ結果 (`src/gui/archive_result_window.py`)
* 素材の再リンク (`src/gui/timeline/missing_media_dialog.py`)、音量解析 (`src/gui/volume_threshold_dialog.py`)
