# 設計書: テロップ（字幕）フォント設定項目の追加

`docs/request/request.md` の要望に対する設計書。CLAUDE.md の方針に従い、本書のレビュー後に実装する。

---

## 1. 概要

テロップ（フルテロップ字幕）に関するフォント・スタイル項目を設定画面から変更できるようにする。
あわせて以下を行う。

- `src/settings/setting.json` の `subtitle` セクションに新規キーを追加する。
- `src/modules/subtitle_generator.py` の字幕生成処理が新規キーを参照するよう修正する。
- `src/settings/settings_window.py` の設定画面に**タブ**を追加し、「一般」「字幕」に分割する。

設計方針（CLAUDE.md 準拠）:

- ハードコードを排し、ASS Style 行の各値を `setting.json` から取得する。
- 既存キー（`own_subtitle_color` / `font_family` / `font_size` / `max_line_length` / `enabled`）は**名称を維持**し、後方互換を保つ。
- 不足キーは `DEFAULT_SETTINGS` と `_merge_with_defaults`、各所の `.get(key, 既定値)` で吸収する（旧 `setting.json` でも動作）。

---

## 2. 現状分析

### 2.1 ASS Style 行（`subtitle_generator.py:139-144`）

現在、ASS の Style 行は大半がハードコードされている。

```
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour,
        Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,
        BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{family},{size},{primary},&H00000000,&H64000000,
       0,0,0,0,100,100,0,0,1,3,1,2,40,40,60,1
```

| ASS 項目 | 現状 | 取得元 |
|---|---|---|
| Fontname | `{family}` | `font_family`（設定済） |
| Fontsize | `{size}` | `font_size`（設定済） |
| PrimaryColour | `{primary}` | `own_subtitle_color`（設定済・`#RRGGBB`→`&H` 変換） |
| OutlineColour | `&H00000000` | **ハードコード** → 今回設定化 |
| BackColour | `&H64000000` | **ハードコード** → 今回設定化 |
| Bold/Italic/Underline/StrikeOut | `0,0,0,0` | **ハードコード** → 今回設定化 |
| ScaleX/ScaleY | `100,100` | ハードコード（要望対象外＝固定維持） |
| Spacing | `0` | **ハードコード** → 今回設定化 |
| Angle | `0` | **ハードコード** → 今回設定化 |
| BorderStyle | `1` | **ハードコード** → 今回設定化 |
| Outline | `3` | **ハードコード** → 今回設定化（アウトラインの太さ） |
| Shadow | `1` | ハードコード（要望対象外＝固定維持） |
| Alignment | `2` | **ハードコード** → 今回設定化 |
| MarginL/R/V | `40,40,60` | **ハードコード** → 今回設定化 |
| Encoding | `1` | ハードコード（要望対象外＝固定維持） |

→ 太字部分を `setting.json` 化する。要望に含まれない ScaleX/ScaleY・Shadow・Encoding は定数として固定維持する。

### 2.2 設定画面（`settings_window.py`）

現状は `QGridLayout` の単一画面でタブは無い。字幕系入力は
言語 / 配信スタイル / しきい値 / 最小発話無視時間 / 自分の字幕色 / テロップ ON/OFF のみ。
フォント関係（大きさ・種類・縁取り等）の入力欄が存在しない。

---

## 3. setting.json 定義（subtitle セクション追加項目）

既存キーは維持し、以下を追加する。値は ASS Style 行へ直接展開できる型・形式とする。

| キー | 型 | 既定値 | 対応 ASS 項目 | 備考 |
|---|---|---|---|---|
| `enabled`（既存） | bool | `true` | （工程 ON/OFF） | テロップ生成有効化 |
| `own_subtitle_color`（既存） | str | `#ED1C24` | PrimaryColour | `#RRGGBB` 形式。既存 `to_ass_color()` で変換 |
| `font_size`（既存） | int | `48` | Fontsize | フォントの大きさ |
| `font_family`（既存） | str | `Yu Gothic UI` | Fontname | フォント種類 |
| `max_line_length`（既存） | int | `15` | （自動改行） | 1行最大文字数 |
| `outline_color`（新規） | str | `&H00000000` | OutlineColour | **ASS 形式 `&HBBGGRR` を直接入力**（要望の例に準拠） |
| `outline_width`（新規） | int | `3` | Outline | アウトラインの太さ(px) |
| `back_color`（新規） | str | `&H64000000` | BackColour | ASS 形式 `&HAABBGGRR`（背景/影色） |
| `bold`（新規） | bool | `false` | Bold | True→`-1` / False→`0` に変換 |
| `italic`（新規） | bool | `false` | Italic | 同上 |
| `underline`（新規） | bool | `false` | Underline | 同上 |
| `strikeout`（新規） | bool | `false` | StrikeOut | 同上 |
| `spacing`（新規） | int | `0` | Spacing | 文字間隔 |
| `angle`（新規） | int | `0` | Angle | 回転角度(度) |
| `border_style`（新規） | int | `1` | BorderStyle | `1`=縁取り+影 / `3`=不透明ボックス |
| `alignment`（新規） | int | `2` | Alignment | テンキー配置 1〜9 |
| `margin_l`（新規） | int | `40` | MarginL | 左余白 |
| `margin_r`（新規） | int | `40` | MarginR | 右余白 |
| `margin_v`（新規） | int | `60` | MarginV | 下（上段配置時は上）余白 |

> **設計判断（色の入力形式）**
> 文字色 `own_subtitle_color` は既存仕様を尊重し `#RRGGBB` 形式のまま（内部で `&H` へ変換）。
> アウトライン色・背景色は要望の例が ASS 形式（`&H00000000` / `&H64000000`）であり、
> 透明度(アルファ)指定も必要なため **ASS 形式の文字列をそのまま保存・展開**する。
> 入力形式が項目で異なる点は UI のプレースホルダーで明示する（§8 で確認事項として記載）。

> **設計判断（Bold 等の真偽値変換）**
> ASS 仕様では Bold/Italic/Underline/StrikeOut は「`-1`=有効 / `0`=無効」。
> チェックボックス（bool）で保存し、ASS 展開時に `-1`/`0` へ変換する。

### setting.json 追記イメージ（subtitle セクション）

```json
"subtitle": {
    "language": "ja",
    "broadcast_style": "ソロ",
    "threshold": "0.6",
    "min_speech_ignore_time": "0.6",
    "own_subtitle_color": "#ED1C24",
    "enabled": true,
    "font_family": "Yu Gothic UI",
    "font_size": 48,
    "max_line_length": 15,
    "outline_color": "&H00000000",
    "outline_width": 3,
    "back_color": "&H64000000",
    "bold": false,
    "italic": false,
    "underline": false,
    "strikeout": false,
    "spacing": 0,
    "angle": 0,
    "border_style": 1,
    "alignment": 2,
    "margin_l": 40,
    "margin_r": 40,
    "margin_v": 60,
    "engine": "whisper",
    "whisper_model": "large-v3",
    "whisper_device": "cpu",
    "whisper_compute_type": "int8",
    "whisper_beam_size": 8,
    "remove_fillers": false
}
```

---

## 4. subtitle_generator.py 修正設計

### 4.1 `FontProfile` の拡張（`:16-29`）

現在 `family` / `size` / `color_hex` のみを保持する `FontProfile` を拡張し、
ASS Style に必要な全項目を保持させる。`build_subtitle_file` はここから値を取得する。

追加保持する属性（既定値は §3 と一致）:

```
outline_color, outline_width, back_color,
bold, italic, underline, strikeout,
spacing, angle, border_style, alignment,
margin_l, margin_r, margin_v
```

- 既存メソッド `to_ass_color()`（PrimaryColour 用 `#RRGGBB`→`&H` 変換）は維持。
- bool→ASS フラグ変換ヘルパ（例: `_ass_flag(value)` が True で `-1`, False で `0`）を追加。
- `outline_color` / `back_color` は ASS 形式文字列をそのまま保持。
  空文字・不正形式時は既定値（`&H00000000` / `&H64000000`）へフォールバック。

### 4.2 `build_subtitle_file` の Style 行生成（`:130-158`）

ハードコードの Style 行を `font_profile` 属性から組み立てる。Format 行（列定義）は不変。
Style 行のみ各値を差し替える。

```
Style: Default,
  {family},{size},{primary},{outline_color},{back_color},
  {bold},{italic},{underline},{strikeout},
  100,100,                       ← ScaleX/ScaleY 固定（要望対象外）
  {spacing},{angle},
  {border_style},{outline_width},
  1,                             ← Shadow 固定（要望対象外）
  {alignment},
  {margin_l},{margin_r},{margin_v},
  1                              ← Encoding 固定
```

ScaleX/ScaleY/Shadow/Encoding は**モジュール定数**として定義し、マジックナンバーを避ける。

### 4.3 `run()` の FontProfile 生成（`:210-214`）

`FontProfile` 生成箇所で `subtitle_cfg` から新規キーを `subtitle_cfg.get(key, 既定値)` で読み取り渡す。
欠落時も安全に動作させ、旧 `setting.json` との後方互換を保つ。

---

## 5. settings_window.py 修正設計（タブ化）

### 5.1 タブ構成

`QTabWidget` を導入し、2 タブに再編する。

- **「一般」タブ**: 動画ディレクトリ / オープニング動画 / エンディング動画 / 出力ディレクトリ
  （現「一般設定」グループを移設）
- **「字幕」タブ**: 字幕・フォント関係を集約
  - 言語 / 配信スタイル / しきい値 / 最小発話無視時間（既存）
  - テロップ ON/OFF（既存）
  - 文字色 / フォントの大きさ / フォント種類 / 1行最大文字数
  - アウトラインの色 / アウトラインの太さ / 背景・影の色
  - 太字 / 斜体 / 下線 / 打消し線（チェックボックス）
  - 文字間隔 / 回転角度
  - 縁取りスタイル（ドロップダウン）/ 表示位置（ドロップダウン）
  - 余白 左 / 右 / 下

> 保存ボタンはタブ外（ウィンドウ下部）に共通配置し、全タブの値を一括保存する。

### 5.2 追加ウィジェットと対応キー

| ラベル | ウィジェット | 保存キー |
|---|---|---|
| 文字色 | `QLineEdit`（既存「自分の字幕色」を流用） | `own_subtitle_color` |
| フォントの大きさ | `QLineEdit`（整数バリデーション） | `font_size` |
| フォント種類 | `QComboBox`（インストール済フォント一覧） | `font_family` |
| 1行最大文字数 | `QLineEdit`（数値） | `max_line_length` |
| アウトラインの色 | `QLineEdit`（placeholder `&H00000000`） | `outline_color` |
| アウトラインの太さ | `QLineEdit`（数値） | `outline_width` |
| 背景/影の色 | `QLineEdit`（placeholder `&H64000000`） | `back_color` |
| 太字 | `QCheckBox` | `bold` |
| 斜体 | `QCheckBox` | `italic` |
| 下線 | `QCheckBox` | `underline` |
| 打消し線 | `QCheckBox` | `strikeout` |
| 文字間隔 | `QLineEdit`（数値） | `spacing` |
| 回転角度 | `QLineEdit`（数値） | `angle` |
| 縁取りスタイル | `QComboBox`（value/text 対応） | `border_style` |
| 表示位置 | `QComboBox`（value/text 対応） | `alignment` |
| 余白 左 | `QLineEdit`（数値） | `margin_l` |
| 余白 右 | `QLineEdit`（数値） | `margin_r` |
| 余白 下 | `QLineEdit`（数値） | `margin_v` |

#### フォント種類ドロップダウン

`QFontDatabase.families()`（PySide6 では静的メソッド）でインストール済フォント名一覧を取得し、
`QComboBox` に設定する。読込時は `font_family` の値を `findText` で選択し、一覧に無い場合は
`addItem` で補完してから選択して設定値を保持する。保存時は `currentText()` を `font_family` に格納する。

#### ドロップダウン定義（value と表示文字列を分離）

`QComboBox.addItem(表示テキスト, userData=value)` を用い、保存時に `currentData()` を取得する。
読込時は value から該当 index を引いて選択する（`findData`）。

縁取りスタイル:

```
1 → 縁取り + ドロップシャドウ
3 → 不透明ボックス
```

表示位置:

```
1 下段（左）   2 下段（中央）  3 下段（右）
4 中段（左）   5 中段（中央）  6 中段（右）
7 上段（左）   8 上段（中央）  9 上段（右）
```

### 5.3 数値入力の扱い

- 数値項目は既存 UI と統一して `QLineEdit` + `QIntValidator` を採用する。
- 保存時に `int()` 変換し、失敗時は既定値へフォールバックする
  （`_on_save` 内にヘルパ `_to_int(text, default)` を新設）。

### 5.4 保存・読込ロジック

- `DEFAULT_SETTINGS["subtitle"]`（`:54-70`）へ §3 の新規キー既定値を追加する。
- `_load_to_ui`（`:312-330`）に新規項目の UI 反映を追加する。
- `_on_save`（`:340-366`）の `settings["subtitle"].update({...})` に新規キーを追加する。
- 既存の**マージ保存方式**（UI 外セクション `silence_cut`/`ffmpeg`/`logging` を破壊しない）は維持する。

---

## 6. 後方互換・エラー処理

- 旧 `setting.json`（新規キー無し）でも、`_merge_with_defaults` と `.get(key, default)` により
  既定値で補完されて動作する（既存挙動を再現）。
- 色文字列（`outline_color`/`back_color`）が不正な場合は既定の ASS 値へフォールバックし、ASS 生成を壊さない。
- 数値項目が空・非数値の場合も既定値へフォールバックする。
- 例外時の方針は既存 `SubtitleError` 機構を踏襲し、新規例外は追加しない。

---

## 7. 変更対象ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/settings/setting.json` | `subtitle` に新規キー追加（§3） |
| `src/settings/settings_window.py` | `QTabWidget` 化、字幕タブに入力追加、`DEFAULT_SETTINGS`・`_load_to_ui`・`_on_save` 更新 |
| `src/modules/subtitle_generator.py` | `FontProfile` 拡張、`build_subtitle_file` の Style 行を設定値から生成、`run()` 更新 |

> 既存の関数シグネチャ `build_subtitle_file(timeline, font_profile, output_path, ...)` は変更しない。
> 追加情報はすべて `FontProfile` 属性として渡すため呼び出し側互換を保てる。

---

## 8. 不明点・確認事項（推測実装せず記載）

1. **色の入力形式の混在**: 文字色は `#RRGGBB`、アウトライン/背景色は `&H...` と形式が異なる。
   要望の例に従いこの仕様としたが、UI 統一の観点で全て ASS 形式（または全て `#`）に揃える案も可能。
   統一を希望する場合は指示を頂きたい。
2. **数値入力ウィジェット**: 既存 UI に合わせ `QLineEdit`+バリデータを採用予定。`QSpinBox` 希望なら変更する。
3. **ScaleX/ScaleY・影(Shadow)・Encoding**: 要望に無いため固定（定数化）とした。これらも設定化するか確認したい。
4. **フォント種類**: インストール済フォント一覧から選択する `QComboBox` とする（`QFontDatabase.families()` で取得）。
   既定値 `Yu Gothic UI` が一覧に存在する場合は初期選択する。保存値が未インストール（一覧に無い）の場合は、
   その値を一覧へ補完追加して選択状態を維持し、設定値を失わない。

---

## 9. 実装ステップ（レビュー後）

1. `setting.json` に新規キー追加。
2. `settings_window.py`: `DEFAULT_SETTINGS` 更新 → `QTabWidget` 化 → 字幕タブ UI 追加 → `_load_to_ui` / `_on_save` 更新。
3. `subtitle_generator.py`: `FontProfile` 拡張 → 定数定義 → `build_subtitle_file` Style 行生成 → `run()` 更新。
4. 旧 `setting.json` / 新 `setting.json` 双方での起動・保存・ASS 生成を確認。
