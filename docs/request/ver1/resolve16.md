# resolve16 — フォントプレビュー / フォント追加 / テロップ個別フォント 要望設計書

## 0. 本書の位置づけ
`docs/request/request15.md` の要望に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要なライブラリを追加しない／後方互換を失わない）に従い、本書レビュー後に実装します。

---

## 1. 要望（request15.md 要約）

### A. `settings_window.py`（字幕タブ）のフォント機能追加
1. **フォントのプレビュー** — 現在設定しているフォント／設定したフォントを画面上でプレビューしたい。可能なら **フォント選択ドロップダウン自体にそのフォントを適用**し、プレビュー代わりにできると最もスマート。
2. **フォントの追加** — `settings_window.py` からフォントを追加できるようにする。**対応できるフォントファイルのみ**受け付ける。**追加可能なフォントファイルの指定を注意書きとして同じカラムに小さく記載**する。

### B. テロップ一覧画面（`SubtitleEditorDialog`）の機能追加
3. **テロップごとにフォント・フォントサイズを設定**できるようにする。
   - デフォルトは `settings_window.py`（＝`subtitle.font_family` / `subtitle.font_size`）で設定した値。
   - 変更したい行だけドロップダウンを選ぶイメージ。→ ドロップダウンには **Blank（＝デフォルト使用）** を選べるようにする。
   - フォントサイズは **小さめのテキストボックス**。

---

## 2. 現状分析（関連コードの実測）

| 対象 | 現状 | 本要望上の問題 |
|---|---|---|
| `settings_window.py` L603-608 | フォント種類は `QComboBox`（`QFontDatabase.families()` を addItems）。`setEditable(False)`。**プレビュー無し**（項目・現在値ともに UI 標準フォントで描画） | フォントの見た目が分からない |
| `settings_window.py` `_set_font_family` L1037-1044 | 一覧に無いファミリは補完追加して選択維持 | フォント**ファイル**の追加口は無い（インストール済のみ列挙） |
| フォント追加 | **存在しない** | 未インストールフォントを使えない |
| `subtitle_generator.FontProfile` L67-159 | フォント（family/size）は**全役割共通の単一値**。役割別に差し替わるのは**色（塗り・アウトライン）のみ** | テロップ個別のフォント差し替え手段が無い |
| `build_subtitle_file` L468-505 | 役割ごとに `Style: Streamer/Sub/Comment` を出力し、各 `Dialogue` は `role` で Style を選ぶだけ | Dialogue 単位のフォント上書き手段が無い |
| `SubtitleEditorDialog` L34-42 | 列は `時間/字幕/使用/配信者/サブ/コメント`。`result_items()`（L270-288）は `start/end/text/use/role` を返す | フォント／サイズの列・返却フィールドが無い |
| `_review_timeline` L724-765 | 編集結果から `start/end/text/role` のみ再構成（`font`/`font_size` は保持しない） | 個別フォントを焼き込みへ伝播できない |
| `burn_subtitle` L523-569 | `subtitles='{safe_path}'`。**`fontsdir` 指定なし** → libass は OS のインストール済フォントのみ解決 | 追加フォントファイルを**焼き込み側（ffmpeg/libass）が見つけられない** |
| フォント保存先 | `fonts/` 等の格納概念は**無い**（grep 実測でヒット無し） | 追加フォントの置き場・登録機構が未定義 |

**重要な構造的事実（設計の要点）**:
`settings_window.py`（PySide6）と焼き込みの `ffmpeg`（libass）は **別プロセス**。Qt 側で `QFontDatabase.addApplicationFont()` してもそれは Qt アプリ内だけの登録で、**ffmpeg サブプロセスからは見えない**。よって「フォント追加」を**プレビューだけでなく実際の焼き込みまで機能させる**には、**追加フォントファイルを所定ディレクトリに保存し、① Qt へ都度登録（プレビュー用）② `subtitles` フィルタへ `fontsdir` を渡す（焼き込み用）** の両輪が必要（§4.3）。

---

## 3. 設計方針（全体）

1. **既存を壊さない追加のみ**。`subtitle.font_family` / `subtitle.font_size` は**デフォルト値として不変**。新規は「追加フォント格納ディレクトリ」と、テロップ編集画面の**任意上書き列**のみ。
2. **プレビュー**は ①ドロップダウン**全項目**を**その項目自身のフォントで描画**（`Qt.FontRole`）＋ ②選択中フォントの**サンプル文字プレビュー欄**の二本立て（要望1「ドロップダウン自体に適用」を主、視認補助にサンプル欄）。項目描画 OFF のオプションは設けない（§8-1 確定）。
3. **フォント追加**は「ファイル選択 → 対応拡張子検証 → 格納ディレクトリへコピー → Qt へ登録 → 一覧へ反映」。対応拡張子の**注意書きを同カラムに小さく**表示（要望2）。
4. **焼き込み連動**：`burn_subtitle` の `subtitles` フィルタに `fontsdir=<格納ディレクトリ>` を付与し、追加フォントを libass が解決できるようにする（§4.3）。
5. **テロップ個別フォント**は **ASS インライン上書きタグ `{\fn...\fs...}`** で実現（役割別 Style モデルは維持）。Blank／空欄は**タグを出さず Style を継承**＝従来挙動。データは `timeline` エントリに任意フィールド `font`/`font_size` を追加して伝播（§4.4）。
6. **設定は setting.json 管理**。格納ディレクトリのパス等は `setting.json` に持たせハードコードを避ける（§4.5）。
7. **新規ライブラリ非導入**。PySide6（`QFontDatabase.addApplicationFont` / `QFileDialog`）と既存 ffmpeg/libass のみで実現。

---

## 4. 詳細設計

### 4.1 フォントプレビュー（要望1 / `settings_window.py`）

**① ドロップダウン項目を各フォントで描画（主案）**
`font_family_combo` の各項目に、その項目自身のフォントを `Qt.FontRole` で設定する。項目リスト自体がプレビューになる（要望の「一番スマート」案）。

```
# 方針（実装はレビュー後）
for i in range(combo.count()):
    family = combo.itemText(i)
    combo.setItemData(i, QFont(family, PREVIEW_POINT_SIZE), Qt.FontRole)
# 選択中の見た目もそのフォントにする（現在値プレビュー）
def _apply_combo_preview_font(combo):
    combo.setFont(QFont(combo.currentText(), PREVIEW_POINT_SIZE))
combo.currentIndexChanged.connect(lambda: _apply_combo_preview_font(combo))
```

- `PREVIEW_POINT_SIZE` は定数（例 12）で定義しハードコード回避。焼き込みサイズ（`font_size`）とは無関係の**画面表示専用**。
- **全項目を自フォントで描画（§8-1 確定）**。`QFontDatabase.families()` は環境により数百件だが、`FontRole` の一括設定は初回（画面構築時）のみで許容範囲。項目描画 OFF のオプションは設けない。

**② サンプル文字プレビュー欄（補助）**
フォント種類行の直下に、選択中フォントで**固定サンプル文字**を描画する `QLabel` を置く。日本語・英数の視認用。

```
# サンプル文字列は定数化（ハードコード回避）
PREVIEW_SAMPLE_TEXT = "あいうえお 永 ABCabc 0123"
```
- 選択変更・フォント追加時に `preview_label.setFont(QFont(family, PREVIEW_POINT_SIZE))` で更新。
- ラベルは高さ固定・省スペースにし、`WINDOW_WIDTH=300` を崩さない範囲で配置。

### 4.2 フォント追加（要望2 / `settings_window.py`）

フォント種類行の付近に **「フォント追加…」ボタン**と**注意書きラベル**を追加する。

**対応フォントファイル（libass/FreeType が焼き込みで解決可能な形式）**
| 拡張子 | 種別 |
|---|---|
| `.ttf` | TrueType |
| `.otf` | OpenType |
| `.ttc` | TrueType Collection |

> `.woff/.woff2/.eot` 等の Web フォント、`.fon`（旧ビットマップ）は libass 焼き込みで不安定なため**非対応**（§8-2 確定：`.ttf/.otf/.ttc` 限定）。

**注意書き（同カラムに小さく／要望2）**
フォント追加ボタンと同じグリッドセル内に、小さめフォントの補足ラベルを配置する。

```
FONT_ADD_FILTER = "フォントファイル (*.ttf *.otf *.ttc)"
FONT_ADD_NOTE   = "追加できるのは .ttf / .otf / .ttc のみ"
# 例: note_label.setStyleSheet("color:#888; font-size:11px;")
```

**追加フロー**
```
1. QFileDialog.getOpenFileName(filter=FONT_ADD_FILTER) でファイル選択
2. 拡張子を許可集合(.ttf/.otf/.ttc)で検証。不正 → QMessageBox 警告して中断
3. 格納ディレクトリ（§4.5）へコピー（同名は上書き確認 or リネーム）
4. QFontDatabase.addApplicationFont(コピー先) で Qt に登録
   → 取得したファミリ名を font_family_combo に無ければ追加し、選択状態にする
5. §4.1 のプレビュー（FontRole / サンプル欄）を更新
```

- 追加フォントは**焼き込みでも使う**ため、Qt 登録だけで終わらせず**格納ディレクトリへ実ファイルを残す**（§4.3 の `fontsdir` が参照する）。
- アプリ再起動後も使えるよう、**起動時に格納ディレクトリ内の対応フォントを全て `addApplicationFont` する**初期化を追加（§4.5）。

### 4.3 焼き込み側の追加フォント解決（`subtitle_generator.burn_subtitle`）

`subtitles` フィルタに **`fontsdir`** を付与し、libass が格納ディレクトリのフォントを解決できるようにする。**これが無いと追加フォントは焼き込みで反映されない**（§2 重要事実）。

```
# 現状 (L551):
#   filter_spec = f"setpts=N/FRAME_RATE/TB,{scale_chain}subtitles='{safe_path}'"
# 変更方針:
#   fontsdir をエスケープ（safe_path と同じ 2 段階エスケープ: '\' → '/', ':' → '\:'）し
#   subtitles='...':fontsdir='...' として付与する。
#   格納ディレクトリが存在しない/空なら fontsdir を付けない（従来と完全同一）。
```

- **エスケープは既存 `safe_path` と同一規則**（`\`→`/`、`:`→`\:`）を流用（ドライブレター混入で EINVAL になる既知問題を回避）。
- 格納ディレクトリ未作成時は `fontsdir` を付与しない → **横・縦・既存挙動は完全不変**。
- libass は `fontsdir` 内フォントに加え OS インストール済フォントも従来どおり解決する（インストール済フォントの挙動は変わらない）。

### 4.4 テロップ個別フォント・サイズ（要望3 / 編集画面〜焼き込み）

#### 4.4.1 UI（`SubtitleEditorDialog`）
列を 2 つ追加する（役割列の右）。

| 追加列 | ウィジェット | 既定 | 意味 |
|---|---|---|---|
| `フォント` | `QComboBox`（`setCellWidget`） | 先頭 **Blank** を選択 | Blank＝デフォルト（`subtitle.font_family`）を使う。選択時のみ上書き |
| `サイズ` | 小さめ `QLineEdit`（`QIntValidator`） | 空欄 | 空欄＝デフォルト（`subtitle.font_size`）。数値時のみ上書き |

- フォント列 `QComboBox` の項目は **先頭に Blank（`""`）＋ `QFontDatabase.families()`（追加フォント含む）**。§4.1 と同様に `Qt.FontRole` で各項目を自フォント描画にできる（プレビュー共通化）。
- サイズ列は幅を小さく固定（例 `setMaximumWidth`）し、テーブル横幅の肥大を防ぐ。
- 既定 Blank／空欄なので、**何も触らなければ従来と完全同一の焼き込み結果**（後方互換）。
- ダイアログ生成時に**デフォルト値（`subtitle.font_family`/`font_size`）を受け取り**、Blank 選択時の実効値をユーザーに示すため、列ヘッダ脇か説明ラベルに「Blank＝設定画面のフォント（例: Yu Gothic UI / 130）」を**併記する**（§8-4 確定）。

#### 4.4.2 データ伝播
- `SubtitleEditorDialog.__init__(items, default_font=..., default_size=..., font_families=...)` を**任意引数追加**（既存呼び出しは既定 None で従来動作）。
- `result_items()` の各要素に **任意フィールド** を追加：
  ```
  {"start","end","text","use","role",
   "font": <str or "">,        # Blank は "" （＝デフォルト）
   "font_size": <int or None>} # 空欄は None （＝デフォルト）
  ```
- `main_window` の `SubtitleReviewBridge` は `items`/戻り値を素通しするだけなので、`font`/`font_size` フィールドは**自動的に透過**（改修不要 or 最小）。ダイアログ生成箇所（L91）で `default_font`/`default_size`/`font_families` を渡す小改修のみ。
- `_review_timeline`（L724-765）の `used` 再構成で `font`/`font_size` を**保持**する（現状は落としている）。

#### 4.4.3 焼き込み（`build_subtitle_file`）— インライン上書きタグ
役割別 Style（Streamer/Sub/Comment）は**維持**し、`font`/`font_size` が指定された Dialogue のみ本文先頭に ASS 上書きタグを付与する。

```
# build_subtitle_file の各 entry 生成時（方針）
override = ""
fn = entry.get("font")          # "" は無指定
fs = entry.get("font_size")     # None は無指定
if fn:  override += f"\\fn{fn}"
if fs:  override += f"\\fs{int(fs)}"
if override:
    text = "{" + override + "}" + text   # コメントラベル付与より前に前置
```
- **Blank／空欄 → タグ無し → Style（役割）の値を継承**（＝従来挙動、後方互換）。
- コメント役割の `コメント：\N` ラベル付与（L497-498）との順序：**上書きタグを本文最先頭**に置き、**`コメント：` ラベル部にも個別フォントを適用する**（§8-5 確定）。
- 個別サイズは **ASS 実ピクセル値**（設定 `font_size` と同一単位。横1080/縦1920 で見え方が変わる点は許容）。上限バリデーションは設けない（§8-6 確定）。
- 縦動画（request14）では `vertical.font_size` が全体サイズを上書きするが、**個別 `\fs` は inline が優先**するため意図どおり個別サイズが勝つ。挙動として明記（§7）。
- インライン `\fn` のフォントが libass 未解決の場合は libass の既定フォントへフォールバック（＝追加フォントは §4.3 の `fontsdir` で解決させる）。

### 4.5 setting.json 追加案（追加のみ・既定は現行維持）

追加フォントの**格納ディレクトリ**を設定化する（ハードコード回避）。個別フォント値は**編集画面のその場データ**であり永続設定は不要（デフォルト元は既存 `subtitle.font_family`/`font_size`）。

```jsonc
"subtitle": {
  // …既存キーは不変…
  "custom_fonts_dir": "fonts"   // 追加フォント格納ディレクトリ（SETTINGS_DIR 相対 = settings/fonts）
}
```
- `settings_window.py` の `DEFAULT_SETTINGS["subtitle"]` に `custom_fonts_dir` を追加（`_merge_with_defaults` が欠落補完し後方互換維持）。
- **パス解決規則（§8-3 確定）**：`custom_fonts_dir="fonts"` を **`SETTINGS_DIR` 相対**で解決＝**`settings/fonts`**。全実行で共有する（§8-7 確定：全プロジェクト共有）。
- **凍結配布の注意**：PyInstaller 配布で当該ディレクトリが読み取り専用の場合、フォント追加（コピー）は失敗し得る。その際は §5 のフォールバックで全損を避け、実装時に実機で書込可否を確認する。
- `burn_subtitle`（§4.3）と起動時登録（§4.2）は、この設定から解決した**同一ディレクトリ**を参照する。

### 4.6 変更対象ファイル一覧（予定）

| ファイル | 変更概要 |
|---|---|
| `src/settings/settings_window.py` | フォント combo のプレビュー（FontRole＋サンプル欄）、「フォント追加」ボタン＋注意書き、起動時に格納フォント登録、`DEFAULT_SETTINGS["subtitle"]["custom_fonts_dir"]` 追加 |
| `src/gui/subtitle_editor_dialog.py` | `フォント`/`サイズ` 列追加、`__init__` に default_font/default_size/font_families 追加、`result_items()` に font/font_size 追加 |
| `src/gui/main_window.py` | ダイアログ生成時に既定フォント・サイズ・ファミリ一覧を渡す（L91 周辺の小改修） |
| `src/modules/subtitle_generator.py` | `_review_timeline` で font/font_size 保持、`build_subtitle_file` でインライン `\fn\fs` 付与、`burn_subtitle` に `fontsdir` 付与 |
| `src/settings/setting.json` | `subtitle.custom_fonts_dir` 追加（既定値） |

---

## 5. エラー処理・フォールバック方針
- **非対応フォントファイル選択** → 拡張子検証で弾き `QMessageBox` 警告。処理は中断し既存状態を維持。
- **`addApplicationFont` 失敗（-1）** → 警告表示し、その1件をスキップ（他は継続）。起動時一括登録では失敗ファイルをログ警告して読み飛ばす。
- **格納ディレクトリ作成不可（凍結・権限）** → フォント追加は失敗として通知。焼き込み側は `fontsdir` を付けず従来挙動で継続（テロップ全損を避ける／既存フォールバック思想と同一）。
- **個別フォントが libass 未解決** → libass 既定フォントへフォールバック（`\fn` 指定は無害）。
- **個別サイズ非数値** → `QIntValidator` で抑止。抜けた場合も `int()` 失敗時は無指定（デフォルト）として扱う。
- 例外階層は既存 `AutoEditError` 系を流用（新規例外は増やさない）。

## 6. ログ出力方針
- フォント追加：追加ファイル名・登録ファミリ名・格納先を INFO。失敗は WARNING。
- 焼き込み：`fontsdir` 使用時にディレクトリと登録フォント数を INFO。
- 個別フォント適用：上書き件数（例「フォント個別指定 N 件 / サイズ個別指定 M 件」）を INFO。

## 7. 影響範囲・後方互換
- **プレビュー**：表示のみの変更で焼き込み結果に影響なし。
- **フォント追加**：格納ディレクトリ未作成なら `fontsdir` を付けない＝**既存挙動不変**。作成後も OS インストール済フォントの解決は不変。
- **個別フォント**：編集画面で**何も指定しなければ**タグ非付与＝**焼き込み結果は従来同一**。指定時のみ inline `\fn\fs` が該当 Dialogue に付く。
- **縦動画（request14）との関係**：個別 `\fs` は `vertical.font_size` の全体上書きより**inline が優先**する（意図どおり）。この優先順位を仕様として明記。
- **新規依存なし**（PySide6 / 既存 ffmpeg・libass のみ）。CLAUDE.md「不要なライブラリを追加しない」遵守。

---

## 8. 不明点・確認事項 → **回答確定（2026-07-17）**

| # | 項目 | 回答（確定） |
|---|---|---|
| 1 | ドロップダウン各項目の自フォント描画 | **全項目を自フォントで描画**（`Qt.FontRole`）＋サンプル欄。項目描画OFFのオプションは設けない |
| 2 | 対応フォント拡張子 | **`.ttf/.otf/.ttc` 限定**（`.woff` 等は除外） |
| 3 | 追加フォント格納先の解決 | **`settings/fonts`（`SETTINGS_DIR` 相対）で確定**。`custom_fonts_dir="fonts"` |
| 4 | 編集画面での「Blank＝デフォルト」表記 | **併記する**（実効デフォルト値 例: Yu Gothic UI / 130 を画面に明記） |
| 5 | コメント役割ラベルへの個別フォント適用 | **ラベル部にも個別フォントを適用**（上書きタグを本文最先頭に置き `コメント：` にも乗せる） |
| 6 | 個別サイズの単位・上限 | **ASS 実ピクセル値**（設定 `font_size` と同一単位）。上限バリデーションは設けない |
| 7 | 追加フォントの永続範囲 | **全プロジェクトで共有**（`settings/fonts` を全実行で共通参照） |

> ✅ 上記確定（2026-07-17）により本設計は実装フェーズへ移行する。

> **補足（#3 の凍結配布時の注意）**: `settings/fonts` は開発実行では問題なく書き込める。PyInstaller 凍結配布で `_internal` 配下が読み取り専用になる環境では**フォント追加（コピー）が失敗し得る**。その場合は §5 のフォールバック（追加は失敗通知・焼き込みは `fontsdir` 無しで継続）で全損を避ける。凍結配布での書込可否は実装時に実機確認する。

---

## 9. 段階実装（レビュー後）
1. **プレビュー**（§4.1）：combo の FontRole＋サンプル欄。表示のみで低リスク。
2. **フォント追加**（§4.2/4.5）＋**焼き込み `fontsdir`**（§4.3）：格納ディレクトリ解決（§8-3 確定後）→ 追加・起動時登録・libass 連動。
3. **テロップ個別フォント/サイズ**（§4.4）：編集画面 2 列追加 → データ伝播 → inline `\fn\fs`。
4. 実素材で縦・横・追加フォント・個別指定の焼き込み確認。

実装着手は本設計書のレビュー後とする（CLAUDE.md「実装は設計書レビュー後に行う」遵守）。
