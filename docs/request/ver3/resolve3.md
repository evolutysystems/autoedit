# resolve3（ver3） — UI のガラスモーフィズム化 要望設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request3.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「いきなり実装を開始しない」）。
* 本件は **見た目だけの変更**であり、編集ロジック・パイプライン・出力結果には一切手を入れない。
  焼き込まれる動画・ASS・FCPXML は現状とバイト単位で同一のままとする（§9）。
* 記載の判断は実装済みコードと実行環境の実測に基づく。決めきれない点は
  推測実装せず **§10 確認事項** に列挙した。

---

## 1. 要望（request3.md）

> 全体的にデザインをガラスモーフィズムにしてください。

要望はこの 1 文で、適用範囲・配色・明暗の指定はありません。
本書では次のように解釈し、確定は §10 に回します。

| ID | 解釈した要件 | 根拠 |
|---|---|---|
| R1 | 半透明のパネルを基調にする | ガラスモーフィズムの基本要素 |
| R2 | パネル背後をぼかす（すりガラス） | 同上。**Qt での実現に制約あり → §3-1** |
| R3 | 細く明るい境界線で層を分ける | 同上 |
| R4 | 奥行き（影・重なり）を表現する | 同上 |
| R5 | 彩度のある背景の上に重ねる | すりガラスは背後に色が無いと成立しない |
| R6 | 「全体的に」＝アプリの全画面へ適用する | 要望の文言 |
| R7 | 編集内容の視認性を損なわない | 動画編集ソフトとしての前提（§3-4） |

### 1.1 レビュー回答による確定事項（2026-08-07）

| # | 論点 | 回答 | 反映先 |
|---|---|---|---|
| Q1 | 適用範囲 | **全画面** | R6 のとおり。19 クラスすべてを対象にする（§11 で段階適用） |
| Q2 | 明暗 | **ダークかそうでないかは Windows の設定による** | **R8 として新規要件化。** ライト/ダークの 2 セットを用意し OS 設定に追従する（§3-6 / §5.2） |
| Q3 | アクセント色 | **赤** | R9。§5.2 でライト/ダーク別の赤を定義 |
| Q3（続き） | 編集画面の扱い | **Timeline の背景の黒色部分のみ適用する** | **R10 として新規要件化。** クリップ自体はガラス化しない（§5.7 を全面的に見直し） |

これにより要件が 3 つ増えました。

| ID | 追加要件 | 本書での扱い |
|---|---|---|
| R8 | **Windows のライト/ダーク設定に追従する**（切り替えにも追随する） | §3-6 / §5.2 / §5.10 |
| R9 | **アクセント色は赤** | §5.2。ただし既存の意味色との衝突に注意が必要（§10-Q10） |
| R10 | **編集画面では Timeline の「背景の黒色部分」だけをガラスにする**（クリップは現状の見た目を維持） | §5.7 |

#### R10 の解釈について

「編集画面は Timeline の背景の黒色部分のみ適用する」を、
**「Timeline 領域の中でガラス化するのはトラックの黒い背景だけで、
クリップ（本編・OP/ED・字幕・音声の帯）は現状の見た目のまま残す」** と解釈しました。

当初案（§5.7 初版）ではクリップも半透明＋角丸にする予定でしたが、これを取りやめます。
結果として §3-4 で挙げた「編集点の境界が判別できること」という懸念は**自動的に解消**されます。

> もし「編集画面ではガラスを Timeline の背景にだけ使い、
> 周囲のパネル（インスペクタ・ボタン列）にはガラスを使わない」という意図でしたら、
> §5.7 の適用範囲がさらに狭まります。§10-Q11 で確認します。

---

## 2. 現状分析（実装済みコードの実測）

### 2.1 対象となる画面

| 分類 | クラス | ファイル |
|---|---|---|
| メイン | `MainWindow` / `ClipTabWidget` | `gui/main_window.py:623` / `:404` |
| 設定 | `SettingsWindow` | `settings/settings_window.py:644`（約 1,700 行・タブ多数） |
| Timeline 編集（ver3） | `TimelineEditorDialog` / `_InspectorPanel` | `gui/timeline/timeline_editor_dialog.py:42` / `:366` |
| Timeline 描画 | `TimelinePanel` / `TimelineView` / `TimelineRuler` / `TrackHeaderWidget` | `gui/timeline/timeline_view.py:690` / `:225` / `:113` / `:193` |
| プレビュー | `PreviewPanel` / `_HighQualityDialog` | `gui/timeline/preview_panel.py:141` / `:776` |
| 旧字幕編集（存置） | `SubtitleEditorDialog` / `SubtitleEditorWidget` / `SubtitlePreviewWidget` | `gui/subtitle_editor_dialog.py:480` / `:215` / `gui/subtitle_preview_widget.py:107` |
| アーカイブ | `ArchiveTabWidget` / `ArchiveResultWindow` / `ArchiveResultDialog` / `ScoreGraphWidget` | `gui/archive_tab.py:179` ほか |
| 小ダイアログ | `VolumeThresholdDialog` | `gui/volume_threshold_dialog.py:24` |

**合計 19 クラス。** 「全体的に」を額面どおり取るとこれら全部が対象になります。

### 2.2 現在のスタイル指定

**テーマ機構は存在しません。** アプリは Qt 既定のパレット（OS のライト/ダークに追従）で動いており、
個別の `setStyleSheet` が **12 か所**に散在しているだけです。

| 箇所 | 内容 |
|---|---|
| `gui/subtitle_preview_widget.py:169` | `background:#111; color:#aaa;` |
| `gui/subtitle_preview_widget.py:216` / `timeline/preview_panel.py:304`, `:792` / `timeline/timeline_view.py:764` / `timeline/timeline_editor_dialog.py:386` | `color:#888;`（補足テキスト） |
| `gui/timeline/timeline_editor_dialog.py:381` | `font-weight:bold;` |
| `settings/settings_window.py:966`, `:1183`, `:1200` | 注記テキストの色 |
| `settings/settings_window.py:1321` | 見出しの下線 `border-bottom: 1px solid #888;` |
| `settings/settings_window.py:1454` | **色見本ボタンを動的に生成**（`_update_swatch`） |

`settings_window.py:1454` の色見本だけは**設定値そのものを表示する機能**であり、
テーマで塗り替えてはいけません（§5.5）。

### 2.3 QSS が効かない自前描画ウィジェット（最大の作業対象）

以下は `QPainter` で直接描画しており、**Qt Style Sheet がまったく効きません**。

| ウィジェット | 描画内容 | 色の持ち方 |
|---|---|---|
| `TimelineView` / `TimelineRuler` / `TrackHeaderWidget` | トラック・クリップ・ルーラ・再生ヘッド | `timeline_view.py:40`〜`:53` に **`QColor` 定数 13 個を直書き** |
| `PreviewPanel`（`QGraphicsScene`） | 映像フレーム＋オーバーレイ | `preview_items.py:22` ほか |
| `ScoreGraphWidget` | 採点グラフ（QtCharts） | QtCharts 独自のテーマ API |

```python
# timeline_view.py:40-53 （現状。すべて不透明のフラットカラー）
_COLOR_BODY = QColor(58, 108, 168)
_COLOR_OPENING = QColor(126, 87, 168)
...
_COLOR_TRACK_BG = QColor(38, 38, 40)
_COLOR_RULER_BG = QColor(30, 30, 32)
```

**この 13 定数をテーマから引くように変えることが、Timeline をガラス化する前提**になります。

### 2.4 実行環境の実測（§3 の前提）

| 項目 | 実測値 | 意味 |
|---|---|---|
| Windows | build **26200**（Windows 11） | Mica / Acrylic の API が使える（22621 以降） |
| PySide6 | **6.11.0** | — |
| `QGraphicsBlurEffect` | 利用可 | ただし**ウィジェット自身**をぼかすもので、背景はぼかせない |
| `dwmapi.DwmSetWindowAttribute` | 存在する | Windows ネイティブのすりガラスを要求できる |
| `user32.SetWindowCompositionAttribute` | 存在する | 旧世代の Acrylic API（Win10 系） |
| QSS の `rgba()` / `border-radius` / `border` | 適用可（エラーなし） | 半透明パネルと角丸は QSS で表現できる |
| `WA_TranslucentBackground` / `FramelessWindowHint` | 利用可 | ウィンドウ自体の透過・フレームレス化が可能 |
| `QStyleHints.colorScheme()` | **利用可**（`Light` / `Dark` / `Unknown`） | **OS のライト/ダーク設定を取得できる（R8 の土台）** |
| `QStyleHints.colorSchemeChanged` | **利用可** | OS 側で切り替えられたときに**実行中でも追随できる** |
| `QStyleHints.setColorScheme()` | 利用可 | 設定でモードを固定したいときに使える |
| **現在の Windows 設定** | `AppsUseLightTheme = 1` → **ライトモード** | **開発機は現在ライト。** ライト側の作り込みが先に目に入る |

> `colorScheme()` はプラットフォームプラグインが `offscreen` のとき `Unknown` を返しました。
> 実機（`windows` プラグイン）では `Light` / `Dark` が返る想定ですが、
> `Unknown` が返る場合に備えてレジストリ
> （`HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize\AppsUseLightTheme`）
> を読む二段構えにします（§5.10）。

---

## 3. 方式選定

### 3-1. 「すりガラス（背景ぼかし）」をどう実現するか ── 本件の核心

まず前提として重要な事実があります。

> **Qt Widgets には CSS の `backdrop-filter` に相当する機能がありません。**
> QSS で書けるのは「半透明の塗り」までで、**背後の映像をぼかすことはできません。**

したがって「本物のすりガラス」を得る手段は限られます。

| 方式 | 半透明 | 背景ぼかし | 適用範囲 | 追加依存 | 評価 |
|---|---|---|---|---|---|
| A. **QSS（`rgba()` + `border-radius` + `border`）** | ○ | **×** | QSS が効く標準ウィジェット全部 | なし | **基盤として採用** |
| B. **Windows ネイティブ（DWM Acrylic / Mica）** | ○ | **◎ 本物** | **ウィンドウ背景のみ**（内部パネルはぼけない） | なし（`ctypes`） | **Windows で上乗せ採用** |
| C. 自前 backdrop blur（親を `QPixmap` 化 → `QGraphicsBlurEffect` → パネル背景へ描画） | ○ | ○ | 任意のパネル | なし | **不採用**（§下記） |
| D. 外部ライブラリ（`BlurWindow` / `PyQt-Frameless-Window` 等） | ○ | ○ | 広い | **あり** | **却下**（`claude.md`「不要なライブラリを追加しない」） |

**採用: A を基盤とし、Windows では B を上乗せする。C・D は採らない。**

#### C を採らない理由

C は「親ウィジェットを毎回画像化してぼかす」方式で、**再描画のたびに全面のコストがかかります**。
本アプリでは次の 2 か所が致命的です。

* `TimelineView`: 長尺動画では **1,000 個超のクリップ**を描画し、ドラッグ・ズーム・
  再生ヘッド追従で毎フレーム再描画される
* `PreviewPanel`: 再生中は最大 `play_fps`（既定 15）回／秒でフレームが差し替わる

ここに毎回ぼかしを掛けると、編集操作の応答性が確実に落ちます。
**「見た目のための変更で編集が重くなる」のは本末転倒**と判断しました。

#### 結果として得られる見え方（正直な記載）

| 部位 | ぼかし | 実現方法 |
|---|---|---|
| **ウィンドウの背景**（デスクトップが透ける部分） | **本物のぼかし** | B（Windows のみ） |
| **ウィンドウ内のパネル**（ツールバー・インスペクタ・ダイアログ） | **ぼかしなし**。半透明＋境界線＋ハイライトで「ガラスらしさ」を作る | A |

つまり **内部パネルは「すりガラスの見立て」** になります。
一般的な Web のガラスモーフィズムと完全に同じにはなりません。この差は仕様として明記します。

### 3-2. 背景をどう用意するか（R5）

すりガラスは「背後に色があること」で成立します。現状の背景は Qt 既定の無地です。

| 方式 | 内容 | 評価 |
|---|---|---|
| A. **斜めグラデーション＋アクセントの光球** | `QLinearGradient` と `QRadialGradient` を最背面に描く | **採用**。素材ファイル不要・軽量・どの環境でも同じ見え方 |
| B. 壁紙画像を同梱 | 見栄えは作りやすい | 配布物が増える。解像度依存。`claude.md`「不要なファイルを増やさない」の精神に反する |
| C. 背景なし（OS のデスクトップを透過） | Windows なら B（§3-1）で実現 | 単独では成立しない。A のフォールバックとして併用 |

**採用: A。** Windows で `DwmSetWindowAttribute` が成功した場合は
デスクトップのぼかしがその下に見えるため、グラデーションの不透明度を下げて併用します。

### 3-3. 自前描画ウィジェット（Timeline / プレビュー）をどうするか

QSS が効かないため、**色を 1 か所へ集約して描画コードがそこから引く**しかありません。

| 方式 | 評価 |
|---|---|
| A. 各ファイルの `QColor` 定数を手で書き換える | 設定で切り替えられない。`claude.md`「ハードコードは禁止」に反する |
| B. **`src/gui/theme.py` を新設し、全ウィジェットがそこから色を引く** | **採用**。QSS も同じトークンから生成するため、見た目が一貫する |

**採用: B。** `theme.py` が「デザイントークンの唯一の出どころ」になります（§5.2）。

### 3-4. 可読性をどう担保するか（R7）── 譲れない前提

半透明は**必ず文字と図形のコントラストを下げます**。本アプリは動画編集ソフトであり、
次の 3 つは装飾より優先されます。

1. **字幕テキストが読めること**（インスペクタ・旧字幕一覧）
2. **プレビュー映像が正しい色・明るさで見えること**（焼き込み結果の判断材料）
3. **Timeline のクリップ境界が判別できること**（編集点の位置がすべて）

したがって次を設計上の制約とします。

| 制約 | 内容 |
|---|---|
| 文字の背後 | ガラス層を重ねない。文字は**十分不透明な面の上**に置く（§5.7） |
| コントラスト比 | 本文 **4.5:1 以上**、見出し **3:1 以上**（WCAG AA 相当）を満たす色を選ぶ |
| **プレビュー映像** | **ガラスを一切重ねない。** 枠だけをガラスにし、中身は素通し（§5.6） |
| Timeline のクリップ | 半透明にしても**境界線は不透明**にして輪郭を保つ |

### 3-5. タイトルバーをどう扱うか

本格的なガラスモーフィズムではタイトルバーも意匠に含めたくなりますが、
`FramelessWindowHint` で自前タイトルバーにすると **リサイズ・スナップ・最大化・
ダブルクリック最大化・タスクバー連携をすべて再実装**することになります。

| 方式 | 評価 |
|---|---|
| A. **OS のタイトルバーのまま、DWM で色だけ合わせる**（`DWMWA_CAPTION_COLOR`） | **採用**。実装量が小さく、OS の作法を壊さない |
| B. フレームレス＋自前タイトルバー | 見た目の自由度は高いが、windowing の再実装が必要でリスクが大きい |

**採用: A。** B が必要かは §10-Q5 で確認します。

### 3-6. ライト / ダークの追従（R8）

「ダークかそうでないかは Windows の設定による」という回答により、
**2 セットの配色を用意して OS 設定に追従する**ことが要件になりました。

#### 3-6-1 OS 設定の取得方法

| 方式 | 内容 | 評価 |
|---|---|---|
| A. **`QStyleHints.colorScheme()`** | Qt が OS 設定を解決してくれる。`colorSchemeChanged` で切り替えにも追随できる | **採用**（主） |
| B. レジストリ直読み（`AppsUseLightTheme`） | Windows 専用。確実だが切り替えの通知が無い | **フォールバックとして併用**（A が `Unknown` のとき） |
| C. OS のパレット（`QPalette.Window` の明度）から推測 | 追加 API 不要 | 推測でしかなく、スタイル次第で外れる |

**採用: A を主・B をフォールバック。** どちらも取れない場合はダークを既定とします
（動画編集ソフトの慣例に合わせるため）。

#### 3-6-2 ライトモードのガラスをどう作るか

ダークとライトでは**ガラスの作り方が逆になります**。ここは単純な色の反転では済みません。

| | ダークモード | ライトモード |
|---|---|---|
| 背景 | 濃紺〜紫のグラデーション | 明るいグレー〜青白のグラデーション |
| ガラスの塗り | **白を低い不透明度で**（`rgba(255,255,255,0.07)`） | **白を高い不透明度で**（`rgba(255,255,255,0.55)`） |
| 境界線 | 白 16% | 白 75%（明るい縁で浮かせる） |
| 影 | 黒 35% | 黒 12%（ライトでは影を弱くしないと汚れて見える） |
| 文字 | ほぼ白 | ほぼ黒 |

ライトで白の不透明度を上げるのは、**下地が明るいと低不透明度の白がほとんど見えず、
ガラスとして成立しないため**です。逆にダークで不透明度を上げると白く濁ります。

#### 3-6-3 Timeline はモードに追従させるか

**追従させません。Timeline は常にダーク基調とします。**

理由は 2 つあります。

1. 回答 Q3 が **「Timeline の背景の黒色部分」** と述べており、
   Timeline の下地が黒であることを前提にしている
2. DaVinci Resolve / Premiere Pro をはじめ、**ノンリニア編集ソフトの Timeline は
   OS 設定によらず常に暗い**。映像の色を正しく判断するための業界慣例でもある

したがって、ライトモードでは「明るいウィンドウの中に、暗い Timeline パネルが載る」
という見え方になります。これは意図した設計です。

#### 3-6-4 切り替えのタイミング

| 方式 | 評価 |
|---|---|
| A. 起動時に 1 回だけ判定する | OS 側で切り替えても次回起動まで反映されない |
| B. **`colorSchemeChanged` を購読して即座に貼り替える** | **採用**。QSS の再適用と再描画だけで済み、状態は失われない |

**採用: B。** `theme.apply()` は何度呼んでも安全な作りにし、
シグナル受信時に QSS・パレット・自前描画の色キャッシュを作り直します（§5.10）。
**編集中の Timeline の内容は一切失われません**（見た目の貼り替えのみ）。

---

## 4. 設計方針（全体）

1. **見た目だけを変える。** レイアウト・ウィジェット構成・シグナル配線・編集ロジックには触れない。
   出力される動画・ASS・FCPXML は現状と同一（§9）。
2. **デザイントークンを 1 か所に集約する。** `src/gui/theme.py` が唯一の出どころ。
   QSS も自前描画も同じトークンを読む。
3. **ハードコードしない。** 配色・不透明度・角丸・アクセント色は `setting.json` から変更できる。
4. **いつでも戻せる。** `ui.theme = "system"` で従来の Qt 既定へ完全に戻る。
5. **できないことは正直に扱う。** 内部パネルの背景ぼかしは Qt では不可能（§3-1）。
   「見立て」であることを明記し、ネイティブぼかしは取れたら上乗せする。
6. **可読性は装飾より優先する。**（§3-4）
7. **重くしない。** 描画コストが増える箇所（Timeline）は影・角丸を最小限にする（§5.8）。
8. **OS の明暗設定に従う（R8）。** 配色はライト/ダークの 2 セットを持ち、
   起動時と OS 側の切り替え時の両方で追従する。設定で固定もできる。
9. **Timeline は常にダーク（R8 の例外）。** 編集ソフトの慣例と回答 Q3 の文言に従う（§3-6-3）。
10. **Timeline のクリップは今の見た目を保つ（R10）。**
    ガラスにするのは背景（黒い部分）だけ。編集点の判別しやすさを最優先する。

---

## 5. 詳細設計

### 5.1 新規・変更ファイル一覧（予定）

| ファイル | 変更内容 |
|---|---|
| `src/gui/theme.py` | **新規**。デザイントークン（ライト/ダーク 2 セット）・OS 明暗の判定・QSS 生成・ネイティブ効果の適用・背景描画ヘルパ |
| `src/gui/main_window.py` | 起動時に `theme.apply(app)` を呼ぶ。`colorSchemeChanged` を購読して貼り替え（R8）。背景描画を `MainWindow.paintEvent` へ |
| `src/gui/timeline/timeline_view.py` | `QColor` 定数のうち**背景系のみ**テーマ参照へ置換（R10）。**クリップ 6 色と再生ヘッド・選択枠は現状維持** |
| `src/gui/timeline/preview_panel.py` | 枠のみガラス化。インライン `setStyleSheet` をテーマのクラス指定へ |
| `src/gui/timeline/timeline_editor_dialog.py` | インスペクタをガラスパネル化。インライン指定を除去 |
| `src/gui/timeline/preview_items.py` | 選択枠の色をテーマ参照へ |
| `src/settings/settings_window.py` | 注記・見出しのインライン指定を除去。**色見本ボタンは対象外**（§5.5） |
| `src/gui/subtitle_editor_dialog.py` / `subtitle_preview_widget.py` | インライン指定を除去 |
| `src/gui/archive_tab.py` / `archive_result_window.py` / `score_graph_widget.py` | 同上。グラフは QtCharts のテーマ API で配色 |
| `src/gui/volume_threshold_dialog.py` | ガラスダイアログ化（QSS のみで完結） |
| `src/settings/settings_window.py`（`DEFAULT_SETTINGS`） | `ui` セクションを追加（§6） |
| `tests/test_theme.py` | **新規**。トークン解決・QSS 生成・コントラスト比の検証 |

**新規ファイルは `theme.py` とテスト 1 本のみ。** 残りは既存への置換で収まります。

### 5.2 デザイントークン（`theme.py`）

**ライトとダークの 2 セット**を持ち、OS 設定で切り替えます（R8 / §3-6）。
アクセントは**赤**とします（R9）。形状トークンは両モード共通です。

```python
# ガラスモーフィズムのデザイントークン (resolve3 §5.2)
# 配色・不透明度は setting.json の ui セクションから上書きできる。

# 両モード共通 (形状)
SHAPE = {
    "radius.panel":   "16px",
    "radius.control": "10px",
    "border.width":   "1px",
}

DARK = {
    # ── 背景 (最背面。すりガラスが乗る土台 / §3-2)
    "bg.from":         "#14111A",   # 左上 (ほぼ無彩の暗色 / §5.2-4)
    "bg.to":           "#221A20",   # 右下
    "bg.glow.a":       "#6E2A18",   # 光球 1 (暖色・控えめ)
    "bg.glow.b":       "#1E2440",   # 光球 2 (寒色。背景が赤一色になるのを避ける)

    # ── ガラス層: 暗い下地に「白を薄く」乗せる (§3-6-2)
    "glass.bg":        "rgba(255,255,255,0.07)",
    "glass.bg.strong": "rgba(255,255,255,0.12)",   # 入力欄・文字の背後 (§3-4)
    "glass.bg.hover":  "rgba(255,255,255,0.14)",
    "glass.border":    "rgba(255,255,255,0.16)",
    "glass.highlight": "rgba(255,255,255,0.28)",   # 上辺 1px のハイライト
    "glass.shadow":    "rgba(0,0,0,0.35)",

    # ── テキスト
    "text.primary":    "#F4F1F2",
    "text.secondary":  "#B9AEB2",
    "text.disabled":   "#7A6E72",

    # ── アクセント (赤 / R9)。アプリアイコンの色をそのまま使う (§5.2-1)
    "accent":          "#DC0810",   # 塗り・線 兼用 (白文字 5.14:1 / 暗背景 3.66:1)
    "accent.bright":   "#F75610",   # 暗背景でのみ使う朱橙 (暗背景 5.65:1)
    "accent.hover":    "#E91E10",
    "accent.pressed":  "#BD0110",
    "danger":          "#DC0810",   # 色では分けず形で分ける (§5.2-2)
    "warning":         "#FFC46B",
    "success":         "#6BE0A8",
}

LIGHT = {
    # ── 背景: 明るいグレー〜青白
    "bg.from":         "#F5F4F6",
    "bg.to":           "#EAE7EC",
    "bg.glow.a":       "#F7DCD0",   # 光球 (暖色・淡く)
    "bg.glow.b":       "#E2E4EF",   # 光球 (寒色)

    # ── ガラス層: 明るい下地では「白を濃く」乗せないと成立しない (§3-6-2)
    "glass.bg":        "rgba(255,255,255,0.55)",
    "glass.bg.strong": "rgba(255,255,255,0.75)",
    "glass.bg.hover":  "rgba(255,255,255,0.85)",
    "glass.border":    "rgba(255,255,255,0.80)",
    "glass.highlight": "rgba(255,255,255,0.95)",
    "glass.shadow":    "rgba(0,0,0,0.12)",         # ライトでは影を弱くする

    # ── テキスト
    "text.primary":    "#1B1B1F",
    "text.secondary":  "#5A555C",
    "text.disabled":   "#9A949B",

    # ── アクセント (赤 / R9)。ダークと同じアイコン色を使う (明背景 4.68:1 で成立)
    "accent":          "#DC0810",
    "accent.bright":   "#DC0810",   # 明背景では朱橙 (3.04) が弱いため主色を使う
    "accent.hover":    "#E91E10",
    "accent.pressed":  "#BD0110",
    "danger":          "#BD0110",   # 明背景では濃い方が読める
    "warning":         "#B26A00",
    "success":         "#1B7A4B",
}
```

#### 5.2-1 アクセント色の根拠 ── アプリアイコンからの抽出と実測

`src/gui/app.ico`（256×256）を解析したところ、
**黒 → 深紅 → 赤 → オレンジ**のグラデーションで構成された「S」字であることが分かりました。
有彩色ピクセル 21,498 個の内訳は次のとおりです。

| 色相帯 | 代表色 | 画素数 |
|---|---|---|
| 345〜360°（深紅） | **`#DC0810`** | 10,251（最大） |
| 0〜15°（赤） | `#E91E10` | 7,358 |
| 15〜30°（朱橙） | `#F75610` | 3,839 |
| 30〜45°（橙） | `#FB840A` | 50 |

この 4 色と調整候補について、**コントラスト比を実測**しました。

| 色 | 白文字 | 暗背景 `#14111A` | 明背景 `#F5F4F6` | 判定 |
|---|---|---|---|---|
| **`#DC0810`（アイコン主色）** | **5.14** | **3.66** | **4.68** | **全条件を満たす唯一の色** |
| `#BD0110`（濃紅） | 6.61 | 2.85 ✗ | 6.02 | 暗背景で沈む |
| `#E91E10`（赤） | 4.51 | 4.18 | 4.10 | 白文字が下限ぎりぎり |
| `#F75610`（朱橙） | 3.34 ✗ | 5.65 | 3.04 | **塗りに使えない**が暗背景で最も映える |
| `#FB840A`（橙） | 2.50 ✗ | 7.54 | 2.27 ✗ | 明背景で見えない |

> 判定基準: 白文字 ≥ 4.5:1（本文相当 / WCAG AA）、背景との比 ≥ 3.0:1（UI 部品の識別 / WCAG AA）

**結論: `#DC0810` をアクセントの主色に採用します。**
アイコンから取れる色のうち、**ライト・ダーク両モードで全条件を満たすのはこの 1 色だけ**でした。
ブランドカラーそのものが機能要件も満たしているため、調整色を作る必要がありません。

`#F75610`（朱橙）は白文字を載せられませんが暗背景で最も映えるため、
**ダークモードの線・フォーカス・グラデーション終端**に限って使います。

#### 5.2-2 主要ボタンはアイコンと同じグラデーションにする

QSS は `qlineargradient()` を書けるため、**アイコンの配色をそのままボタンへ持ち込めます**。

```css
#primaryButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 {accent},          /* #DC0810 深紅 */
                                stop:1 {accent.bright});  /* #F75610 朱橙 */
    color: #FFFFFF;
    border: none;
}
```

白文字のコントラストはグラデーションの**最も明るい端**で決まるため、
`#F75610` 側でも 3.34:1 と本文基準を下回ります。
そこで **グラデーションは「決定」「実行」など短い語のボタンに限定**し、
長い文言のボタンは単色 `#DC0810`（5.14:1）にします。

#### 5.2-3 危険操作は色ではなく形で分ける

アクセント自体が赤のため、**「赤＝危険」という区別が使えません**。
色相で分けようとすると（例: 危険＝より濃い赤）判別しづらく、
彩度を落とすと無効状態に見えてしまいます。

そこで**塗りと輪郭で分けます**。

| 種別 | 見た目 | 例 |
|---|---|---|
| 主要動作 | **塗り**（グラデーション or `#DC0810`）＋白文字 | 「決定」「実行」 |
| 通常動作 | ガラス（半透明）＋通常文字 | 「参照...」「元に戻す」 |
| **破壊的動作** | **輪郭のみ**（`#DC0810` の枠線＋同色の文字、塗りなし） | 「削除」「破棄」「中断」 |

色に頼らず**文言でも意味が伝わる**ようにします（「削除」「破棄」など）。

#### 5.2-4 背景はアクセントを埋もれさせない配色にする

アクセントが赤である以上、**背景まで赤に寄せるとアクセントが目立たなくなります**。
アイコンは「黒 → 赤 → オレンジ」で**黒が大きな面積を占めている**ため、
背景は**ほぼ無彩の暗色**にし、暖色の光球は控えめに、対に寒色の光球を置きます。

```python
# ダーク: ほぼ無彩の暗色 + 控えめな暖色 + 対の寒色 (アクセントを立たせる)
"bg.from":   "#14111A",   "bg.to":     "#221A20",
"bg.glow.a": "#6E2A18",   # 暖色 (アイコンの橙を反映・低不透明度で置く)
"bg.glow.b": "#1E2440",   # 寒色 (背景が赤一色になるのを避ける)

# ライト: 明るい無彩 + 淡い暖色 + 淡い寒色
"bg.from":   "#F5F4F6",   "bg.to":     "#EAE7EC",
"bg.glow.a": "#F7DCD0",
"bg.glow.b": "#E2E4EF",
```

**Timeline 専用トークン**は §3-6-3 のとおり OS 設定に追従せず、常にダーク側を使います。

```python
# Timeline は常にダーク基調 (§3-6-3)。ライトモードでも切り替えない。
TIMELINE = {
    # ガラス化するのは背景だけ (R10 / §5.7)
    "timeline.bg":        "rgba(20,18,24,0.55)",   # トラック領域の下地 (ガラス)
    "timeline.track":     "rgba(255,255,255,0.04)",
    "timeline.track.alt": "rgba(255,255,255,0.06)",
    "timeline.ruler":     "rgba(255,255,255,0.08)",
    "timeline.grid":      "rgba(255,255,255,0.16)",
    "timeline.text":      "#ECECEC",
    # ↓ クリップ・再生ヘッド・選択枠は現状維持 (R10)
    #   timeline_view.py の既存 QColor 定数をそのまま使う
}
```

公開 API:

```python
# 現在の明暗モードを返す ("dark" | "light")。設定が "auto" なら OS 設定を見る (§5.10)
def mode(settings=None): ...

# 設定と明暗モードを反映したトークン辞書を返す
def tokens(settings=None): ...

# トークンを QColor で引く (自前描画ウィジェット用 / §3-3)
def color(name, settings=None): ...

# アプリ全体へ適用する (QSS + パレット)。何度呼んでも安全 (§3-6-4)
def apply(app, settings=None): ...

# OS の明暗切り替えを購読する (R8 / §5.10)
def watch_color_scheme(app, on_changed, settings=None): ...

# ウィンドウへネイティブのすりガラスを要求する (Windows のみ / §5.4)
def apply_native_backdrop(widget, settings=None) -> bool: ...

# 背景グラデーションを描く (MainWindow.paintEvent から呼ぶ / §3-2)
def paint_background(painter, rect, settings=None): ...

# ガラスパネルとして扱うウィジェットに付ける objectName
PANEL = "glassPanel"
PANEL_STRONG = "glassPanelStrong"
```

### 5.3 QSS の構成

`QApplication.setStyleSheet()` に **1 枚だけ**適用します。
個別ウィジェットの `setStyleSheet` は原則廃止し、`objectName` とウィジェット種別で当てます。

```css
/* 土台。ウィンドウ自体は背景を描かない (paintEvent が描く / §3-2) */
QWidget { color: {text.primary}; font-size: 13px; }

/* ガラスパネル (objectName="glassPanel") */
#glassPanel {
    background-color: {glass.bg};
    border: {border.width} solid {glass.border};
    border-radius: {radius.panel};
}
/* 文字・入力を載せる濃いめのガラス (§3-4) */
#glassPanelStrong {
    background-color: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    border-radius: {radius.panel};
}

QPushButton {
    background-color: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    border-radius: {radius.control};
    padding: 6px 14px;
}
QPushButton:hover   { background-color: {glass.bg.hover}; border-color: {glass.highlight}; }
QPushButton:pressed { background-color: {accent.pressed}; color: #FFFFFF; }
QPushButton:disabled{ color: {text.disabled}; background-color: rgba(255,255,255,0.04); }
QPushButton:default { border-color: {accent}; }
/* 主要動作 (objectName="primaryButton") はアイコンのグラデーションで塗る (§5.2-2) */
#primaryButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 {accent}, stop:1 {accent.bright});
    color: #FFFFFF; border: none;
}
#primaryButton:hover    { background: {accent.hover}; }
#primaryButton:pressed  { background: {accent.pressed}; }
/* 破壊的動作 (objectName="dangerButton") は塗らず輪郭で示す (§5.2-3) */
#dangerButton           { background: transparent; color: {danger};
                          border: {border.width} solid {danger}; }
#dangerButton:hover     { background-color: rgba(220,8,16,0.12); }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    border-radius: {radius.control};
    padding: 4px 8px;
    selection-background-color: {accent};
}
QLineEdit:focus, QPlainTextEdit:focus { border-color: {accent}; }

QTabWidget::pane { background: {glass.bg}; border: {border.width} solid {glass.border};
                   border-radius: {radius.panel}; }
QTabBar::tab { background: transparent; padding: 8px 18px; color: {text.secondary}; }
QTabBar::tab:selected { background: {glass.bg.strong}; color: {text.primary};
                        border-top-left-radius: {radius.control};
                        border-top-right-radius: {radius.control}; }

QTableWidget { background: transparent; gridline-color: {glass.border};
               alternate-background-color: rgba(255,255,255,0.04); }
QHeaderView::section { background: {glass.bg.strong}; border: none;
                       border-bottom: 1px solid {glass.border}; padding: 6px; }

QProgressBar { background: {glass.bg}; border: {border.width} solid {glass.border};
               border-radius: {radius.control}; text-align: center; }
QProgressBar::chunk { background: {accent}; border-radius: {radius.control}; }

QScrollBar:vertical, QScrollBar:horizontal { background: transparent; }
QScrollBar::handle { background: {glass.bg.hover}; border-radius: 5px; }

QMenu { background: {glass.bg.strong}; border: {border.width} solid {glass.border};
        border-radius: {radius.control}; }
QMenu::item:selected { background: {accent.pressed}; }

QToolTip { background: {glass.bg.strong}; color: {text.primary};
           border: {border.width} solid {glass.border}; }
```

**注記テキスト**（現在 12 か所で `color:#888;` を直書きしているもの）は
`objectName="noteLabel"` に統一し、QSS 側で `{text.secondary}` を当てます。

### 5.4 ネイティブのすりガラス（Windows / §3-1 B）

```python
# Windows 11 のシステム背景効果 (Acrylic) を要求する (resolve3 §5.4)
# 失敗しても致命的ではない (グラデーション背景で成立する) ため False を返すだけにする。
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20   # タイトルバーをダークにする
_DWMWA_SYSTEMBACKDROP_TYPE = 38       # Windows 11 22621+
_DWMWA_CAPTION_COLOR = 35             # タイトルバーの色 (§3-5 A)
_DWMSBT_TRANSIENTWINDOW = 3           # Acrylic 相当

def apply_native_backdrop(widget, settings=None):
    if sys.platform != "win32" or not _config(settings)["native_backdrop"]:
        return False
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        # ダークタイトルバー → 背景効果 → タイトルバー色 の順に要求する
        ...
    except Exception:
        _logger.info("ネイティブの背景効果は利用できません (グラデーション背景で表示します)")
        return False
    return True
```

* 呼び出しは **ウィンドウが表示された後**（`showEvent`）でないと `winId()` が有効になりません。
* 効果が乗った場合はウィンドウ背景を透過させる必要があるため、
  `paint_background()` の不透明度を下げます（設定 `ui.background.opacity_with_backdrop`）。
* **実機で効き方を確認する必要があります**（§10-Q6 / §11 段 2 の完了条件）。
  効かない場合もグラデーション背景で成立するため、機能不全にはなりません。

### 5.5 設定画面の色見本は対象外

`settings_window.py:1454` の `_update_swatch()` は
**ユーザーが設定した字幕の色そのものを表示する機能**です。

```python
button.setStyleSheet(
    f"background-color: {color.name()}; min-width: 28px; border: 1px solid #888;")
```

ここをテーマで塗り替えると**設定値が見えなくなります**。
枠線の色だけテーマ参照に変え、**塗り（`background-color`）はそのまま**にします。

同種の「値そのものを表示している色」は他に無いことを確認済みです。

### 5.6 プレビューの扱い（§3-4）

**映像の上にはガラスを一切重ねません。**

```
┌─ glassPanel（枠だけガラス）─────────────┐
│ ┌───────────────────────────────┐ │
│ │                                       │ │
│ │      映像フレーム（素通し・加工なし）      │ │
│ │                                       │ │
│ └───────────────────────────────┘ │
│ [◀◀][▶][▶▶]  00:01:23  [🔊]━━  ← ここはガラス │
└───────────────────────────────────┘
```

* `_PreviewGraphicsView` の `backgroundBrush` は**黒のまま**（現状維持）。
  映像の周囲に色を付けると、焼き込み結果の色判断を誤らせます。
* 枠（`QGraphicsView` の外周）とトランスポート列だけをガラスにします。
* オーバーレイの選択枠（`preview_items._SELECTION_COLOR` = 現在は琥珀 `#FFD65C`）は
  **現状のまま**にします。映像の上に置く操作用の目印であり、
  赤アクセントへ寄せると赤い映像の上で見失うためです（§10-Q10）。

同じ理由で `_HighQualityDialog`（本番と同じ描画の確認窓）も、
画像そのものには手を加えません。

### 5.7 Timeline の描画 ── 背景の黒い部分だけをガラスにする（R10）

回答 Q3「編集画面は Timeline の背景の黒色部分のみ適用する」に従い、
**ガラス化するのはトラックの下地だけ**とします。クリップ・再生ヘッド・選択枠は**現状のまま**です。

```
┌ TimelinePanel ────────────────────────────────┐
│ ｜0:00     0:30     1:00     ← ルーラ（ガラス）        │
│ ┌────┬────────────────────────────────┐ │
│ │ S1 │      ▭字幕  ▭字幕                        │ │  ← 背景 = ガラス（黒＋半透明）
│ │ V1 │ ▨OP ▬▬▬▬▬▬▬▬▬▬▬▬▬ ▨ED   │ │  ← クリップ = 現状の見た目（不透明）
│ │ A1 │ ▨   ▬▬▬▬▬▬▬▬▬▬▬▬▬ ▨      │ │
│ └────┴────────────────────────────────┘ │
└──────────────────────────────────────────────┘
   ↑ ヘッダもガラス          ↑ 背景越しにウィンドウ背景が透ける
```

#### 5.7-1 変更する定数（背景系のみ）

| 定数 | 現在 | 変更後 | 効果 |
|---|---|---|---|
| `_COLOR_TRACK_BG` | `QColor(38,38,40)` 不透明 | `{timeline.track}` = `rgba(255,255,255,0.04)` | 下地が透け、ウィンドウ背景がうっすら見える |
| `_COLOR_TRACK_BG_ALT` | `QColor(44,44,47)` | `{timeline.track.alt}` = `rgba(255,255,255,0.06)` | 交互の縞は維持 |
| `_COLOR_RULER_BG` | `QColor(30,30,32)` | `{timeline.ruler}` = `rgba(255,255,255,0.08)` | ルーラもガラス |
| `_COLOR_GRID` | `QColor(64,64,68)` | `{timeline.grid}` | 罫線 |
| `_COLOR_TEXT` | `QColor(236,236,236)` | `{timeline.text}` | ほぼ同値。トークン経由にするだけ |

トラック領域の最背面には `{timeline.bg}` = `rgba(20,18,24,0.55)` を敷きます。
**完全な透明にはしません。**下地が明るいとクリップの色が沈んで見分けにくくなるためです。
この 55% の黒が「Timeline の背景の黒色部分」に相当し、ここがガラスになります。

#### 5.7-2 変更しない定数（R10）

| 定数 | 理由 |
|---|---|
| `_COLOR_BODY` / `_COLOR_OPENING` / `_COLOR_ENDING` / `_COLOR_OVERLAY` / `_COLOR_SUBTITLE` / `_COLOR_AUDIO` | **クリップの見た目は現状維持。** 半透明化も角丸化もしない |
| `_COLOR_DISABLED` | 同上 |
| `_COLOR_PLAYHEAD`（赤） | 位置を示す機能色。アクセントが赤になっても**変えない**（§10-Q10） |
| `_COLOR_SELECTED_BORDER`（琥珀） | 選択の目印。赤にすると再生ヘッドと紛れる（§10-Q10） |

クリップの描画コード（`_paint_clip`）は**手を入れません**。
角丸・半透明・上辺ハイライトの追加はすべて取りやめます。

これにより次の利点が得られます。

* **編集点の境界が今と同じ精度で判別できる**（§3-4 の懸念が解消）
* **描画コストが増えない**（クリップは 1,000 個超あるため効果が大きい / §5.8）
* 変更が背景の 5 定数だけに収まり、回帰の危険が小さい

### 5.8 パフォーマンスへの配慮

Timeline は長尺で **1,000 個超のクリップ**を描くため、装飾のコストが効きます。
R10 によりクリップ自体は無改変になったため、**当初想定していたコスト増はほぼ無くなりました**。

| 対策 | 内容 |
|---|---|
| クリップは無改変 | **R10。** 角丸・半透明・ハイライトを追加しない（最大の効果） |
| 画面外スキップ | **既存実装済み**（`timeline_view.py` の `if x2 < -2 or x1 > self.width()+2: continue`）。維持する |
| 背景は矩形塗りのみ | 半透明の `fillRect` はトラック数ぶん（数個）で、クリップ数に比例しない |
| 影を使わない | `QGraphicsDropShadowEffect` は Timeline 内では**使わない** |
| ぼかしを使わない | §3-1 C の不採用理由のとおり |

影は**ウィンドウ級のパネル（インスペクタ・ダイアログ）にのみ**使い、
数が多い要素には使いません。

### 5.9 適用の流れ

```
main() / MainWindow.__init__
  ├ theme.apply(app, settings)          ← QSS + パレットを一括適用
  │   ├ mode(settings) で明暗を決める (auto なら OS 設定 / §5.10)
  │   ├ tokens(settings) で設定を反映
  │   ├ app.setStyleSheet(build_qss(tokens))
  │   └ app.setPalette(build_palette(tokens))   ← QSS が効かない箇所の保険
  └ theme.watch_color_scheme(app, self._on_theme_changed)   ← R8 (§5.10)

MainWindow.showEvent
  └ theme.apply_native_backdrop(self)   ← winId() が有効になってから (§5.4)

MainWindow.paintEvent
  └ theme.paint_background(painter, self.rect())   ← グラデーション + 光球

各ダイアログ (__init__ の末尾)
  └ objectName を付けるだけ (QSS が自動で当たる)
```

`theme.apply()` は `QApplication` に対して 1 回呼ぶだけで、
**以後に作られるダイアログにも自動で当たります**（QSS はアプリ全体に継承されるため）。
これにより 19 クラスすべてに手を入れる必要はなく、
**ガラスパネルにしたい箇所へ `objectName` を付ける作業**が中心になります。

### 5.10 ライト / ダークの切り替え（R8 / §3-6）

#### 5.10-1 明暗の決定

```python
# 現在の明暗モードを返す ("dark" | "light")
# 設定 ui.theme_mode: "auto" (既定・OS 追従) / "dark" / "light"
def mode(settings=None):
    configured = _config(settings)["theme_mode"]
    if configured in ("dark", "light"):
        return configured
    # 1) Qt に聞く (プラットフォームプラグインが解決してくれる)
    scheme = QApplication.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Light:
        return "light"
    if scheme == Qt.ColorScheme.Dark:
        return "dark"
    # 2) Unknown のときは Windows のレジストリを見る (§2.4)
    resolved = _read_windows_apps_theme()
    if resolved is not None:
        return resolved
    # 3) それも取れなければダーク (編集ソフトの慣例)
    return "dark"
```

`_read_windows_apps_theme()` は
`HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize\AppsUseLightTheme`
を読み、`1` ならライト・`0` ならダークを返します。読めなければ `None`。
Windows 以外や失敗時は例外を握りつぶします。

#### 5.10-2 OS 側の切り替えへの追随

```python
# OS の明暗切り替えを購読する。設定が "auto" のときだけ効く。
def watch_color_scheme(app, on_changed, settings=None):
    if _config(settings)["theme_mode"] != "auto":
        return
    app.styleHints().colorSchemeChanged.connect(lambda _s: on_changed())
```

`MainWindow._on_theme_changed()` の処理:

1. `theme.apply(app, settings)` を呼び直す（QSS・パレットを貼り替え）
2. `theme.invalidate_cache()` で自前描画用の色キャッシュを捨てる
3. 開いているウィンドウへ `update()` を送って再描画

**編集中の状態（Timeline の内容・選択・再生ヘッド位置・Undo 履歴）は一切失われません。**
見た目の貼り替えだけを行います。

`theme.apply()` は**何度呼んでも安全**な作りにします（副作用は QSS とパレットの上書きのみ）。

#### 5.10-3 モード別の見え方

| 部位 | ライトモード | ダークモード |
|---|---|---|
| ウィンドウ背景 | 明るいグレー〜青白のグラデーション | 暖色寄りの黒〜暗赤紫 |
| パネル | 白いガラス（不透明度 55〜75%） | 白を薄く乗せたガラス（7〜12%） |
| 文字 | ほぼ黒 | ほぼ白 |
| アクセント | `#DC0810`（アイコン主色） | `#DC0810` ＋ 線に `#F75610`（アイコン朱橙） |
| **Timeline** | **常にダーク**（§3-6-3） | **常にダーク** |
| プレビュー映像 | **無加工**（§5.6） | **無加工** |

ライトモードでは「明るいウィンドウの中に、暗い Timeline パネルが載る」見え方になります。
これは編集ソフトとして意図した設計です（§3-6-3）。

---

## 6. setting.json 追加案（追加のみ・既存キー不変）

```jsonc
{
  "ui": {
    // "glass" = ガラスモーフィズム (既定) / "system" = 従来の Qt 既定へ完全に戻す
    "theme": "glass",
    // 明暗 (R8 / §5.10)。"auto" = Windows の設定に追従 (既定) / "dark" / "light"
    "theme_mode": "auto",
    // Windows のネイティブすりガラス (§5.4)。効かない環境では自動的に無視される
    "native_backdrop": true,
    // アクセント色 (R9: 赤)。既定はアプリアイコンの主色 (§5.2-1 で実測して決定)
    "accent_color": "#DC0810",         // 塗り・線 兼用 (両モードで全条件を満たす唯一の色)
    "accent_bright_dark": "#F75610",   // ダークのみ: 線・グラデーション終端 (アイコンの朱橙)
    // 形状 (両モード共通)
    "corner_radius_px": 16,            // パネル
    "control_radius_px": 10,           // ボタン・入力欄
    // ガラス層の不透明度。ライトとダークで意味が逆になる (§3-6-2)
    "glass": {
      "dark":  { "bg": 0.07, "bg_strong": 0.12, "border": 0.16, "highlight": 0.28 },
      "light": { "bg": 0.55, "bg_strong": 0.75, "border": 0.80, "highlight": 0.95 }
    },
    // 背景 (§3-2 / §5.2-4)。アクセントを埋もれさせないため赤に寄せすぎない
    "background": {
      "dark":  { "from": "#14111A", "to": "#221A20",
                 "glow_a": "#6E2A18", "glow_b": "#1E2440" },
      "light": { "from": "#F5F4F6", "to": "#EAE7EC",
                 "glow_a": "#F7DCD0", "glow_b": "#E2E4EF" },
      "opacity": 1.0,                  // ネイティブ効果が無いとき
      "opacity_with_backdrop": 0.55    // ネイティブ効果が乗ったとき (デスクトップを透かす)
    },
    // Timeline は明暗に追従せず常にダーク (§3-6-3)。背景の不透明度だけ調整できる
    "timeline_backdrop_opacity": 0.55,
    // 可読性の下限。これを下回る不透明度は指定されても切り上げる (§3-4)
    "min_text_backdrop_opacity": 0.10
  }
}
```

* `DEFAULT_SETTINGS`（`settings_window.py`）へ同内容を追加します。
* 入れ子キーは前回追加した `_fill_timeline_nested_defaults()` と同じ考え方で、
  `ui` セクションにも欠落キーの補完を用意します（利用者が編集できるようにするため）。
* **設定画面（`SettingsWindow`）への項目追加は行いません**（`setting.json` で管理）。
  UI 追加の要否は §10-Q7 で確認します。

---

## 7. エラー処理・フォールバック方針

| 事象 | 挙動 |
|---|---|
| `ui.theme = "system"` | QSS を一切適用せず、従来の Qt 既定で起動する（完全な切り戻し） |
| OS の明暗が取得できない（`colorScheme()` が `Unknown` かつレジストリも読めない） | **ダークとして扱う**（編集ソフトの慣例）。INFO を 1 回出す（§5.10-1） |
| 実行中に OS の明暗が切り替わった | QSS とパレットを貼り替えて再描画する。**編集中の状態は失われない**（§5.10-2） |
| `ui.theme_mode` が `"auto"` 以外 | OS の切り替えを購読しない（固定モード） |
| ネイティブ背景効果が使えない（非 Windows / 古い Windows / API 失敗） | `False` を返して INFO を 1 回出し、**グラデーション背景で成立させる**。機能不全にしない |
| 色の指定が不正（`#GGG` など） | 該当トークンだけ既定へ戻して WARNING。他は生きたまま |
| 不透明度が範囲外（0 未満・1 超） | 0.0〜1.0 へ丸めて WARNING |
| 文字背後の不透明度が `min_text_backdrop_opacity` 未満 | **下限へ切り上げる**（可読性を装飾より優先 / §3-4） |
| QSS の生成に失敗 | QSS を適用せず既定の見た目で起動し、`exception` をログへ。**起動は妨げない** |
| QtCharts が使えない環境 | グラフのテーマ設定をスキップ（既存のグラフ表示可否の判定に従う） |

**原則**: テーマの不具合でアプリが起動できない・操作できない状態を作らないこと。
`theme.apply()` は全体を `try` で囲み、失敗しても素の見た目で動くようにします。

---

## 8. ログ出力方針

| タイミング | レベル | 内容 |
|---|---|---|
| テーマ適用 | INFO | `テーマを適用: glass / ライト (OS 設定に追従) / ネイティブ背景効果: 有効` |
| OS の明暗切り替えを検知 | INFO | `OS の配色が変わったためテーマを貼り替えます: ライト → ダーク` |
| 明暗が判定できない | INFO | `OS の配色を判定できないためダークで表示します` |
| ネイティブ効果が使えない | INFO | `ネイティブの背景効果は利用できません (グラデーション背景で表示します)` |
| 色・不透明度の不正値 | WARNING | `ui.accent_color が不正なため既定を使用します: "#GGG"` |
| 可読性の下限で切り上げ | WARNING | `文字背後の不透明度を下限 0.10 へ切り上げました (指定: 0.03)` |
| QSS 生成の失敗 | ERROR（`exception`） | 素の見た目で起動を継続する |
| 描画のたびのトークン解決 | 出さない | `paintEvent` で多発するため。トークンは起動時に 1 回解決してキャッシュする |

---

## 9. 影響範囲・後方互換

| 対象 | 影響 |
|---|---|
| **出力される動画 / ASS / FCPXML / SRT** | **影響なし。** テーマは画面表示のみに作用し、焼き込み・書き出し経路には一切関与しない |
| 編集ロジック（`src/timeline/`） | **影響なし**（Qt 非依存のまま） |
| パイプライン（`src/pipeline/` / `src/modules/`） | **影響なし** |
| レイアウト・ウィジェット構成 | **変えない**。色・角丸・境界線のみ |
| 既存テスト 188 件 | **影響なし**（見た目を検証しているテストは無い） |
| 設定画面の色見本 | **意図的に対象外**（§5.5）。設定値の表示機能を壊さない |
| プレビュー映像の色 | **変えない**（§5.6）。焼き込み結果の判断材料のため |
| 起動時間 | QSS 適用 1 回ぶんの増加のみ（数ミリ秒） |
| Timeline の描画性能 | **ほぼ影響なし。** R10 によりクリップの描画は無改変で、変わるのは背景の矩形塗りのみ（§5.8） |
| Timeline のクリップの見た目 | **変わらない**（R10）。編集点の判別しやすさは現状のまま |
| 再生ヘッド（赤）・選択枠（琥珀） | **変えない**。アクセントが赤になっても機能色は維持する（§10-Q10） |
| OS の明暗設定 | **追従する**（R8）。実行中の切り替えにも追随し、編集中の状態は失われない |
| 配布物サイズ | **増分なし**（画像素材を持たない / §3-2 A） |

**切り戻し手順**: `setting.json` の `ui.theme` を `"system"` にする（再起動のみ）。

---

## 10. 確認事項

### 10.1 回答反映済み

| # | 論点 | 回答 | 反映先 |
|---|---|---|---|
| Q1 | 適用範囲 | **全画面** | R6。全 19 クラスを対象に段階適用（§11） |
| Q2 | 明暗 | **Windows の設定による** | **R8。** ライト/ダーク 2 セット＋OS 追従（§3-6 / §5.2 / §5.10） |
| Q3 | アクセント色 | **赤** | R9。アプリアイコンから抽出した `#DC0810` に決定（§5.2-1 で実測） |
| Q3（続き） | 編集画面の扱い | **Timeline の背景の黒色部分のみ適用** | **R10。** クリップは無改変（§5.7）。これにより Q8 も同時に確定 |
| Q8 | Timeline のクリップ色 | （Q3 の回答により）**変更しない** | §5.7-2 |

### 10.2 未回答（実装中に確定すればよい）

| # | 論点 | 現時点の案 | 確認したいこと |
|---|---|---|---|
| Q4 | 背景の作り方 | グラデーション＋光球（素材ファイル不要 / §3-2 A） | 壁紙画像を使いたいか。使う場合は素材をご提供いただく形になります |
| Q5 | タイトルバー | OS のものを残し、色だけ合わせる（§3-5 A） | フレームレス化して自前タイトルバーにしたいか（実装量が大きく増えます） |
| Q6 | ネイティブすりガラスの見え方 | Windows 11 で Acrylic を要求する | **実機で効き方を確認したい。**「思ったより透ける／透けない」の調整は実物を見てから |
| Q7 | 設定画面への項目追加 | `setting.json` のみで管理 | テーマ切替やアクセント色を設定画面に出すべきか |
| **Q10** | **赤アクセントの具体色と、既存の意味色との衝突** | **アクセント＝アイコン主色 `#DC0810`**（実測により決定 / §5.2-1）。再生ヘッド（赤）と選択枠（琥珀）は**変えない**。危険操作は色ではなく**輪郭ボタン**で分ける（§5.2-3） | 下記の住み分けでよいか |
| Q11 | R10 の適用範囲 | 「Timeline 領域の中では背景だけをガラスにする」と解釈（§1.1） | 「編集画面ではガラスを Timeline の背景だけに使い、**周囲のパネルにも使わない**」という意図でしたら、§5.7 の範囲がさらに狭まります |

#### Q10 の詳細 ── 赤が持つ既存の意味

アクセントを赤にすると、**アプリ内で既に赤が担っている意味と重なります**。

| 現在の用途 | 色 | 位置 |
|---|---|---|
| **再生ヘッド**（現在位置） | 赤 `#E85454` | `timeline_view.py:51` |
| **危険操作**（削除・中断の警告） | 赤 | ダイアログ文言・`danger` トークン |
| 選択枠 | 琥珀 `#FFD65C` | `timeline_view.py:47` / `preview_items.py:22` |

本設計では次のように住み分けます。

* **再生ヘッド＝赤のまま。** 位置を示す最重要の目印であり、NLE の慣例でもあるため変えません
* **選択枠＝琥珀のまま。** 赤にすると Timeline 上で再生ヘッドと紛れます
* **アクセント（赤）＝ボタン・フォーカス枠・タブ・進捗バーなど「操作系」にのみ使用**
* **危険操作＝淡い赤（`danger`）** にしてアクセントと区別。ただし色だけに頼らず、
  文言（「削除」「破棄」）で意味が伝わるようにします

つまり **「Timeline の中の赤＝再生ヘッド」「画面まわりの赤＝操作できるところ」** という
住み分けです。両者は領域が分かれており、色味も異なります。

| | 色 | 見え方 |
|---|---|---|
| 再生ヘッド | `#E85454` | **明るく淡い**赤。細い縦線 |
| アクセント | `#DC0810` | **深く鮮やかな**赤。面・枠 |

**再生ヘッドを変えない**ことを推奨しますが、より明確に分けたい場合は
**再生ヘッドを白 `#FFFFFF` にする**選択肢もあります
（DaVinci Resolve の再生ヘッドも明色系で、6 色のクリップすべてに対して最も見分けがつきます）。
ただし R10 で「Timeline はクリップも含め現状維持」と確定しているため、
本設計では**変更しない案を採っています**。

### 10.3 事前にお伝えしておきたい制約

**内部パネルの背景ぼかしは Qt では実現できません**（§3-1）。
Web のガラスモーフィズム作例のような「後ろの要素がぼやけて透ける」表現は、
ウィンドウの背景（デスクトップが透ける部分）でのみ本物になり、
ウィンドウ内のパネルは**半透明＋境界線＋ハイライトによる見立て**になります。

外部ライブラリを使えば実現手段はありますが、`claude.md` の
「不要なライブラリを追加しない」に反するため候補から外しています。
どうしても内部パネルもぼかしたい場合は、この方針の見直しが必要です（下記 Q9）。

| # | 論点 | 現時点の案 | 確認したいこと |
|---|---|---|---|
| Q9 | 内部パネルの背景ぼかし | **行わない**（Qt の制約 / 外部ライブラリ禁止） | 必須要件であれば、ライブラリ追加の可否を含めて再検討します |

---

## 11. 段階実装（レビュー後）

各段階の終了時点でアプリが動作し、既存機能を壊さないことを条件にします。
**どの段階でも `ui.theme = "system"` で従来の見た目へ戻せる**状態を保ちます。

| 段 | 内容 | 完了条件 |
|---|---|---|
| **1** | `src/gui/theme.py` を新設（**ライト/ダーク 2 セットの**トークン・明暗判定・QSS 生成・パレット・設定読み込み）＋ `setting.json` の `ui` セクション。まだ適用しない | `tokens()` / `mode()` / `build_qss()` の単体テストが通る。**両モードでコントラスト比が基準を満たす**（§3-4）。OS がライト・ダークどちらでも正しく判定できる |
| **2** | `theme.apply()` をメイン画面へ適用。背景グラデーション描画とネイティブ背景効果 | メイン画面がガラス調になる。**現在の環境（ライトモード）で先に確認**し、続けてダークでも確認。**実機でネイティブ効果の見え方を確認**（Q6） |
| **3** | **OS の明暗切り替えへの追随**（`colorSchemeChanged` / §5.10-2） | Windows の設定を切り替えると見た目が即座に貼り替わり、**編集中の状態が失われない** |
| **4** | 標準ウィジェットのインライン `setStyleSheet` を撤去し QSS へ集約（12 か所） | 見た目が一貫する。**設定画面の色見本が従来どおり設定値を表示する**（§5.5） |
| **5** | `TimelineView` / `TimelineRuler` / `TrackHeaderWidget` の**背景 5 定数のみ**テーマ参照へ（R10 / §5.7-1） | Timeline の背景がガラスになる。**クリップ・再生ヘッド・選択枠の見た目が現状と同一**であることを確認。描画性能が変わらないことを長尺で実測 |
| **6** | プレビューの枠・トランスポートをガラス化（**映像には手を入れない** / §5.6） | 映像の色・明るさが従来と同一であることを、同じフレームの画素値比較で確認 |
| **7** | Timeline 編集画面のインスペクタ・ダイアログをガラスパネル化 | 字幕テキストが読める（両モードでコントラスト比を満たす） |
| **8** | 設定画面・アーカイブ関連・旧字幕編集画面へ適用。QtCharts の配色 | 全 19 画面で一貫する（Q1: 全画面） |
| **9** | 実機確認と微調整。`docs/howto/howtouse.md` のスクリーンショット更新 | Q6 の調整結果を反映。リリース |

段 1 は UI を伴わないため `tests/test_theme.py` で検証します。
段 2 以降は**オフスクリーン起動確認＋実機の目視**が中心になります。

**注意を要する段:**

* **段 1〜3（ライト/ダーク 2 系統）**: 作業量が当初想定の約 2 倍になります。
  ライトモードのガラスは単なる色の反転では成立せず（§3-6-2）、
  白の不透明度を上げる別チューニングが要ります。
  **開発機は現在ライトモード**（§2.4）のため、ライト側から作り込みます。
* **段 5（Timeline）**: R10 によりクリップが無改変になったため、
  当初懸念していた描画性能と編集点の判別しやすさの問題は**設計段階で解消済み**です。
  それでも「現状と同一の見た目であること」を完了条件に置いて確認します。
* **段 6（プレビュー）**: 映像の色再現に関わるため、画素値の比較で確認します。
