# 設計書: 字幕タブの色設定をカラーピッカー化する

`docs/request/request4.md` の要望に対する設計書。CLAUDE.md の方針に従い、本書のレビュー後に実装する。

---

## 1. 概要

`src/settings/settings_window.py` の「字幕」タブには色を指定する項目が 3 か所あり、現状はいずれも
`QLineEdit` への**手入力**である。色形式（`#RRGGBB` や `&HAABBGGRR`）を手で書くのは誤りやすく、
仕上がり色も想像しづらい。

これを解決するため、**各色入力欄の隣に「色を選択」ボタンを追加**し、押下で `QColorDialog`
（PySide6 標準のカラーピッカー）を開いて視覚的に色を選べるようにする。さらに**現在値を示す
色見本（スウォッチ）**をボタン上に表示し、選択結果は従来どおり同じ `QLineEdit` へ書き戻す。

| 対象項目（字幕タブ） | 保存キー | 現状の入力形式 | カラーピッカー対応 |
|---|---|---|---|
| 文字色 | `own_subtitle_color` | `#RRGGBB`（例 `#ED1C24`） | ○（アルファ無し） |
| アウトラインの色 | `outline_color` | ASS `&HAABBGGRR`（例 `&H00000000`） | ○（アルファ有り） |
| 背景/影の色 | `back_color` | ASS `&HAABBGGRR`（例 `&H64000000`） | ○（アルファ有り） |

### 1.1 設計方針（CLAUDE.md 準拠）

- **既存実装を破壊しない**。`QLineEdit` は残し、**保存キー・保存値の形式は一切変えない**
  （`own_subtitle_color` は `#RRGGBB`、`outline_color`/`back_color` は `&HAABBGGRR` のまま）。
  カラーピッカーは「`QLineEdit` に文字列を書き込む補助 UI」に徹する。手入力も従来どおり可能。
- **新規ライブラリを追加しない**。`QColorDialog`／`QColor` は既存採用の **PySide6**
  （`PySide6.QtWidgets` / `PySide6.QtGui`）に含まれており、追加依存は不要。
- **ハードコードを避ける**。色の既定値は既存の `DEFAULT_SETTINGS` / プレースホルダー定数を流用し、
  本機能のために新しい色定数を増やさない。
- **後方互換**。`_load_to_ui` / `_collect_settings` のキーと保存ロジックは変更しない。空欄・不正値時の
  フォールバックも既存の振る舞い（`subtitle_generator` 側 `_safe_ass_color` / `to_ass_color`）を尊重する。

---

## 2. 現状分析

### 2.1 3 つの色欄の生成箇所（`settings_window.py`）

いずれも「字幕」タブ `_build_subtitle_tab()` 内で素の `QLineEdit` として生成されている。

| 項目 | 生成箇所 | ウィジェット参照 | プレースホルダー定数 |
|---|---|---|---|
| 文字色 | `:442`（`self.color_edit`） | `self.color_edit` | `PLACEHOLDER_COLOR = "#ED1C24"`（`:52`） |
| アウトラインの色 | `:471`（`self.outline_color_edit`） | `self.outline_color_edit` | `PLACEHOLDER_OUTLINE_COLOR = "&H00000000"`（`:53`） |
| 背景/影の色 | `:485`（`self.back_color_edit`） | `self.back_color_edit` | `PLACEHOLDER_BACK_COLOR = "&H64000000"`（`:54`） |

### 2.2 値の入出力（変更しない部分）

- 読込: `_load_to_ui()` が `setting.json` の文字列を **そのまま** 各 `QLineEdit` へ流し込む
  （`:651` 文字色 / `:670` アウトライン / `:672` 背景）。
- 保存: `_collect_settings()` が各 `QLineEdit.text().strip()` を **そのまま** 保存する
  （`:749` / `:764` / `:766`）。

→ カラーピッカーは「ダイアログで選んだ色を所定形式の文字列へ変換して `QLineEdit` に `setText()` する」だけでよく、
読込・保存の経路には一切手を入れない。

### 2.3 色形式の差異（設計上の最重要点）

3 欄は**形式が 2 種類**ある。ピッカー⇔文字列の相互変換を形式ごとに用意する必要がある。

| 形式 | 例 | 構造 | アルファ |
|---|---|---|---|
| HTML hex（文字色） | `#ED1C24` | `#RRGGBB` | 無し（不透明固定） |
| ASS（アウトライン/背景） | `&H64000000` | `&HAABBGGRR`（**BGR 並び**・先頭が AA） | 有り |

**ASS のアルファは反転仕様**である点に注意（`subtitle_generator.py` の ASS 生成と整合）。

- ASS: `AA=00` が**不透明**、`AA=FF` が**完全透明**。
- Qt(`QColor`): `alpha=255` が**不透明**、`alpha=0` が**完全透明**。
- したがって相互変換時に **`ass_alpha = 255 - qcolor.alpha()`**（およびその逆）で反転する。

また ASS は色を **BGR 並び**（`&HAA BB GG RR`）で格納する（`subtitle_generator.to_ass_color()`
が `#RRGGBB → &H00BBGGRR` としているのと同じ並び）。変換時に R/B を入れ替える。

---

## 3. UI 設計（色欄＋ピッカーボタン）

### 3.1 レイアウト

各色欄を「`QLineEdit` ＋ 色選択ボタン」の横並びに変更する。既存のディレクトリ/ファイル選択欄
（`_make_dir_picker` / `_make_file_picker`、`:586` / `:597`）と同じ `QHBoxLayout` パターンを踏襲する。

```
[ #ED1C24            ] [■ 色を選択 ]   ← 文字色
[ &H00000000         ] [■ 色を選択 ]   ← アウトラインの色
[ &H64000000         ] [■ 色を選択 ]   ← 背景/影の色
```

- ボタンの「■」部分は**現在値の色見本（スウォッチ）**。`QLineEdit` の値が変わるたびに再描画する。
- 既存の `_make_column_label`（項目名ラベル）・`grid` への追加方法は他項目と同一。
  これまで `grid.addWidget(self.color_edit, row, 1)` だった行を
  `grid.addLayout(self._make_color_picker(self.color_edit, with_alpha=False), row, 1)` に置き換える。

### 3.2 共通ファクトリ `_make_color_picker()`（新規）

ディレクトリ/ファイル選択ファクトリに倣い、色欄用の横並びレイアウトを生成する共通メソッドを追加する。

```python
# 色入力欄 + カラーピッカー起動ボタン(色見本付き)を生成する
# with_alpha=True のとき ASS(&HAABBGGRR) 形式、False のとき HTML(#RRGGBB) 形式として扱う
def _make_color_picker(self, line_edit, with_alpha):
    line_edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
    button = QPushButton()
    button.clicked.connect(lambda: self._choose_color(line_edit, with_alpha))
    # 入力欄の値が変わったら色見本を更新する(手入力にも追従)
    line_edit.textChanged.connect(lambda: self._update_swatch(button, line_edit, with_alpha))
    self._update_swatch(button, line_edit, with_alpha)  # 初期表示
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(line_edit)
    layout.addWidget(button)
    return layout
```

> `with_alpha` で「HTML hex 形式（文字色）」か「ASS 形式（アウトライン/背景）」かを切り替える。
> 文字色はアルファを持たないため `QColorDialog` のアルファ選択も無効化する。

### 3.3 ピッカー起動 `_choose_color()`（新規）

```python
# カラーピッカーを開き、選択結果を所定形式の文字列で line_edit へ書き戻す
def _choose_color(self, line_edit, with_alpha):
    initial = self._parse_color(line_edit.text(), with_alpha)  # 現在値を QColor へ
    options = QColorDialog.ShowAlphaChannel if with_alpha else QColorDialog.ColorDialogOptions()
    color = QColorDialog.getColor(initial, self, "色を選択", options)
    if color.isValid():
        line_edit.setText(self._format_color(color, with_alpha))
```

- ダイアログ初期色は現在の `QLineEdit` 値から復元する（不正・空欄なら既定色）。
- アウトライン/背景のみ `ShowAlphaChannel` を付与し、透明度も選べるようにする。
- 「キャンセル」時は `isValid()` が偽となり、`QLineEdit` は変更しない（既存値を保持）。

---

## 4. 色変換ヘルパ設計（settings_window.py に新規追加）

`subtitle_generator.py` の変換は**焼き込み用（ASS 生成）**であり、設定画面側で再利用するには
逆変換（文字列→`QColor`）も要る。ここでは設定画面内に**双方向の変換ヘルパ**を持たせる
（責務分離。`subtitle_generator` 側は変更しない）。

### 4.1 文字列 → QColor（ダイアログ初期色／色見本用）

```python
# 設定文字列を QColor へ変換する(不正・空欄時は既定色を返す)
# with_alpha=True: ASS &HAABBGGRR / False: HTML #RRGGBB
def _parse_color(self, text, with_alpha):
    s = (text or "").strip()
    try:
        if with_alpha:
            # ASS: &HAABBGGRR (BGR 並び・アルファ反転)
            hex_ = s[2:] if s.upper().startswith("&H") else s
            hex_ = hex_.rjust(8, "0")[-8:]
            aa, bb, gg, rr = hex_[0:2], hex_[2:4], hex_[4:6], hex_[6:8]
            color = QColor(int(rr, 16), int(gg, 16), int(bb, 16))
            color.setAlpha(255 - int(aa, 16))   # ASS→Qt はアルファ反転
            return color
        # HTML: #RRGGBB
        color = QColor(s if s.startswith("#") else "#" + s)
        return color if color.isValid() else QColor(PLACEHOLDER_COLOR)
    except (ValueError, TypeError):
        # フォールバック既定色(プレースホルダー定数を流用しハードコードを避ける)
        return self._parse_color(
            PLACEHOLDER_COLOR if not with_alpha else PLACEHOLDER_OUTLINE_COLOR, with_alpha
        )
```

### 4.2 QColor → 文字列（保存形式へ）

```python
# QColor を設定保存形式の文字列へ変換する
# with_alpha=True: ASS &HAABBGGRR / False: HTML #RRGGBB
def _format_color(self, color, with_alpha):
    if with_alpha:
        aa = 255 - color.alpha()             # Qt→ASS はアルファ反転
        return "&H{:02X}{:02X}{:02X}{:02X}".format(aa, color.blue(), color.green(), color.red())
    return "#{:02X}{:02X}{:02X}".format(color.red(), color.green(), color.blue())
```

### 4.3 色見本の更新 `_update_swatch()`

```python
# ボタン上の色見本を現在の入力値で塗り替える(手入力・ピッカー双方に追従)
def _update_swatch(self, button, line_edit, with_alpha):
    color = self._parse_color(line_edit.text(), with_alpha)
    # 不透明度はボタン背景では無視し、色相のみ提示(視認性優先)
    button.setStyleSheet(
        f"background-color: {color.name()}; min-width: 28px; border: 1px solid #888;"
    )
    button.setText("")  # 色面のみ。ラベルは項目名側で表現
```

> **設計判断（色見本の透明度表現）**
> ボタンの背景色で半透明（アルファ）を厳密に表現するのは難しいため、色見本は**色相のみ**を提示する。
> 透明度は `QLineEdit` の `&HAA...` 値と `QColorDialog`（アルファ付き）で確認・設定する運用とする。

---

## 5. 各色欄の修正設計（差分イメージ）

### 5.1 文字色（HTML hex・アルファ無し）

```python
# 変更前(:445-446)
grid.addWidget(self._make_column_label("文字色"), row, 0)
grid.addWidget(self.color_edit, row, 1)

# 変更後
grid.addWidget(self._make_column_label("文字色"), row, 0)
grid.addLayout(self._make_color_picker(self.color_edit, with_alpha=False), row, 1)
```

### 5.2 アウトラインの色（ASS・アルファ有り）

```python
# 変更後(:474-475 相当)
grid.addWidget(self._make_column_label("アウトラインの色"), row, 0)
grid.addLayout(self._make_color_picker(self.outline_color_edit, with_alpha=True), row, 1)
```

### 5.3 背景/影の色（ASS・アルファ有り）

```python
# 変更後(:488-489 相当)
grid.addWidget(self._make_column_label("背景/影の色"), row, 0)
grid.addLayout(self._make_color_picker(self.back_color_edit, with_alpha=True), row, 1)
```

- `self.color_edit` / `self.outline_color_edit` / `self.back_color_edit` の生成・参照・プレースホルダー
  設定はそのまま残す（`_make_color_picker` がレイアウトに取り込む）。
- インポートに `QColorDialog`（`PySide6.QtWidgets`）と `QColor`（`PySide6.QtGui`）を追加する。

---

## 6. 後方互換・エラー処理

- **保存形式は不変**。`own_subtitle_color`=`#RRGGBB`、`outline_color`/`back_color`=`&HAABBGGRR` のまま。
  `setting.json` の互換性・`subtitle_generator` 側の解釈（`to_ass_color` / `_safe_ass_color`）に影響しない。
- **手入力を維持**。ユーザーは従来どおり `QLineEdit` に直接入力でき、色見本は `textChanged` で追従する。
- **不正・空欄値**: `_parse_color` が既定色（プレースホルダー定数）へフォールバックし、ダイアログ・色見本が
  壊れない。保存値そのものは従来どおりユーザー入力（空欄なら空欄）を尊重し、最終フォールバックは
  焼き込み側 `_safe_ass_color` / `to_ass_color` に委ねる（二重防御）。
- **キャンセル**: `QColorDialog` キャンセル時は `QLineEdit` を変更しない。
- 新規例外は追加しない。例外は変換ヘルパ内で捕捉して既定色へ落とす。

---

## 7. 設計判断・確認事項

実装前に確認したい点を記す（推測実装は避け、回答後に確定する。CLAUDE.md 準拠）。

1. **文字色のアルファ対応**
   現状 `own_subtitle_color` は `#RRGGBB`（アルファ無し）。本設計では**従来仕様を維持**し、
   文字色ピッカーはアルファ非表示とする。→ もし文字色にも透明度が必要なら、保存形式の拡張
   （`#RRGGBBAA` か ASS 形式化）が必要になり別途検討。**今回は対象外**としてよいか。
2. **色見本の透明度表現**
   ボタンの色見本は**色相のみ**提示し、透明度はボタンに反映しない（§4.3）。この簡略化で問題ないか。
   （チェッカーボード背景で半透明を見せる案もあるが実装コスト増のため見送り想定。）
3. **手入力欄の残置**
   ピッカー追加後も `QLineEdit`（手入力）を**残す**前提。ASS の細かな値（既存資産との一致）を
   手で合わせたいケースを想定。手入力を撤去して「ボタンのみ」にする要望はないか。
4. **ASS 入力の許容範囲**
   `&HAABBGGRR`（8 桁）を正とし、`&HBBGGRR`（6 桁＝アルファ省略）も `_parse_color` で
   8 桁へ補完して受理する想定（先頭ゼロ詰め）。この緩い解釈でよいか。

> いずれも未確定の場合は本書の既定方針（1: 対象外 / 2: 色相のみ / 3: 残す / 4: 補完受理）で実装する。

---

## 8. 変更対象ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/settings/settings_window.py` | インポートに `QColorDialog`(QtWidgets)/`QColor`(QtGui) 追加。`_make_color_picker` / `_choose_color` / `_parse_color` / `_format_color` / `_update_swatch` を新規追加（§3・§4）。字幕タブの 3 色欄を `grid.addWidget(...edit...)` から `grid.addLayout(self._make_color_picker(...))` へ置換（§5） |

> `setting.json` / `DEFAULT_SETTINGS` / `_load_to_ui` / `_collect_settings` / `subtitle_generator.py` は
> **変更しない**（保存キー・形式・読込保存ロジックを維持）。本機能は設定画面 UI への追加のみで完結する。

---

## 9. 実装ステップ（レビュー後）

1. `settings_window.py` のインポートに `QColorDialog` / `QColor` を追加。
2. 変換ヘルパ `_parse_color` / `_format_color` を追加（§4.1・4.2／アルファ反転・BGR 並びに注意）。
3. 色見本更新 `_update_swatch` とファクトリ `_make_color_picker`、起動 `_choose_color` を追加（§3・§4.3）。
4. 字幕タブの 3 色欄（文字色 `with_alpha=False` / アウトライン・背景 `with_alpha=True`）を
   `_make_color_picker` 経由のレイアウトに差し替え（§5）。
5. 動作確認:
   - ピッカーで選んだ色が `#RRGGBB`（文字色）/ `&HAABBGGRR`（アウトライン・背景）で保存されること。
   - 既存 `setting.json` の値でダイアログ初期色・色見本が正しく復元されること（アルファ反転含む）。
   - 手入力→色見本追従、キャンセル→値不変、空欄・不正値→既定色フォールバックを確認。
   - 保存後に `subtitle_generator` の ASS 生成が従来どおり成立すること（焼き込み実行で確認）。
