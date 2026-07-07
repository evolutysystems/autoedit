# resolve10 — テロップ役割別スタイル化（配信者／サブ／コメント：塗り色＋アウトライン色）＋コメント先頭ラベル 要望設計書

## 0. 本書の位置づけ
`docs/request/request10.md` の要望に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理）に従い、本書レビュー後に実装します。

---

## 1. 要望（request10.md 要約）
1. 現状 `settings_window.py` ではテロップ色を **1色** しか選べない。これを **3色（塗り色）** にする。
   - **配信者カラー**（＝現状の色）
   - **サブカラー**
   - **コメントカラー**
2. **（追加2）** `settings_window.py` の **アウトラインカラーも役割別に3つ** 作成する。
   - **配信者アウトラインカラー**（＝現状の `outline_color`）
   - **サブアウトラインカラー**
   - **コメントアウトラインカラー**
3. テロップ一覧画面（字幕編集画面）で、「**使用**」の右隣に以下の項目を追加し、一覧に **チェックボックス（またはラジオボタン）** を置く。
   - **配信者** / **サブ** / **コメント**
4. 上記は **1行につき1つしか選択できない**（排他選択）。
5. 選択した役割の **塗り色＋アウトライン色の両方** をそのテロップに適用する（追加2で対応が拡張）。
   - 配信者 → 配信者カラー ＋ 配信者アウトラインカラー
   - サブ → サブカラー ＋ サブアウトラインカラー
   - コメント → コメントカラー ＋ コメントアウトラインカラー
6. **（追加）** 一覧で「**コメント**」を選択した行は、**テロップ出力時に先頭へ「`コメント：`」を付与し改行**する。
   すなわち焼き込みテロップは次の見た目になる（1行目は固定ラベル、2行目以降が本文）。
   ```
   コメント：
   <本文>
   ```
   ※ 対象はコメント役割のみ。配信者・サブにはラベルを付与しない。
   ※ 付与は**出力(焼き込み)時のみ**。編集一覧の本文セルは本文のみ表示する（ラベルは二重付与しない）。

---

## 2. 現状分析（関連コードの実測）

| 対象 | 現状 |
|---|---|
| `src/settings/setting.json` L12 | 色は `subtitle.own_subtitle_color`（例 `"#01D5ED"`）の **1キーのみ**。 |
| `settings_window.py` DEFAULT_SETTINGS L99-142 | `subtitle.own_subtitle_color` のみ定義。 |
| 同 `_build_subtitle_tab` L432-437 | 「文字色」ラベル＋`_make_color_picker(self.color_edit, with_alpha=False)`（HTML `#RRGGBB`）で **1色分**の UI。 |
| 同 `_load_to_ui` L702 / `_collect_settings` L801 | `own_subtitle_color` を単一で読込／保存。 |
| `subtitle_editor_dialog.py` L23-27 | 列は `時間(0)/字幕(1)/使用(2)` の3列固定（`_COLUMN_HEADERS`）。 |
| 同 `_populate` L160-183 / `result_items` L199-213 | items は `{"start","end","text","use"}`。role の概念なし。 |
| `subtitle_generator.py` `FontProfile` L51-97 | ASS Style を **単一 "Default"** で生成。`PrimaryColour` は `color_hex`（=own_subtitle_color）1色。 |
| 同 `build_subtitle_file` L301-327 | `Style: Default` を1行だけ出力し、全 `Dialogue` が `Default` を参照。 |
| 同 `_review_timeline` L527-561 | レビュー結果から `use=True` のみ焼込タイムライン `{start,end,text}` を構築（role なし）。 |
| 同 `run` L583-601 | `FontProfile(color_hex=own_subtitle_color, ...)` を1色で構築。 |
| データ経路 | GUI: `main_window.SubtitleReviewBridge` → `SubtitleEditorDialog` → `result_items()` → `_review_timeline`。CLI/レビュー無効時は role 選択画面が無い。 |

**結論**: 「色の複数保持」「一覧での役割選択」「役割→色の焼き込み反映」の3層に手を入れる。ASS は **役割ごとに Style を3つ定義し、Dialogue 行ごとに参照 Style を切替える**方式が既存構造と親和的（インラインタグより保守容易）。

---

## 3. 用語・役割キー（内部識別子）
UI 表示は日本語、内部・setting.json のキーは英字で統一する（既存 value/text 分離方針に倣う）。

| 役割 | 内部キー(role) | 塗り色キー(subtitle.*) | アウトライン色キー(subtitle.*) | ASS Style 名 |
|---|---|---|---|---|
| 配信者 | `streamer` | `own_subtitle_color`（**既存キーを流用**） | `outline_color`（**既存キーを流用**） | `Streamer` |
| サブ | `sub` | `sub_subtitle_color`（新規） | `sub_outline_color`（新規） | `Sub` |
| コメント | `comment` | `comment_subtitle_color`（新規） | `comment_outline_color`（新規） | `Comment` |

> **後方互換の要**: 配信者の塗り色/アウトライン色は既存 `own_subtitle_color` / `outline_color` をそのまま使う。既存 setting.json・既存挙動（単一スタイル時代）は「全行=配信者」とみなせば完全一致する。
>
> **色形式の違い（重要）**: 塗り色は HTML `#RRGGBB`（アルファ無し）、アウトライン色は ASS `&HAABBGGRR`（アルファ有り）で保持する（既存 UI の `with_alpha` 区分を踏襲）。役割別化後も各形式は据え置く。

---

## 4. 設定（setting.json / DEFAULT_SETTINGS）

### 4.1 追加キー（`subtitle` セクション）
```jsonc
// 塗り色 (HTML #RRGGBB)
"own_subtitle_color":     "#01D5ED",       // 既存: 配信者カラー
"sub_subtitle_color":     "#FFF100",       // 新規: サブカラー（既定は提案値）
"comment_subtitle_color": "#FFFFFF",       // 新規: コメントカラー（既定は提案値）
// アウトライン色 (ASS &HAABBGGRR)  ← 追加2
"outline_color":          "&H00000000",    // 既存: 配信者アウトラインカラー
"sub_outline_color":      "&H00000000",    // 新規: サブアウトラインカラー（既定は提案値）
"comment_outline_color":  "&H00000000",    // 新規: コメントアウトラインカラー（既定は提案値）
// コメント先頭ラベル (追加)
"comment_label":          "コメント：",     // 新規: コメント選択時に先頭付与するラベル
```
- `DEFAULT_SETTINGS["subtitle"]` に上記の**新規5キー**（`sub_subtitle_color`/`comment_subtitle_color`/`sub_outline_color`/`comment_outline_color`/`comment_label`）を追加する。`own_subtitle_color`・`outline_color` は既存キーを流用（配信者用）。
- 既存 `setting.json` に各キーが無くても `_merge_with_defaults`（L204-211）が**自動補完**するため、旧設定ファイルはそのまま動作する。
- 既定色は **提案値**。確定色はユーザー指定に差し替え可（CLAUDE.md「推測で setting.json を変更しない」に配慮し、既定はコード側 DEFAULT_SETTINGS でのみ定義、実 setting.json への直書きは要望確定後）。サブ/コメントのアウトライン既定は現行 `outline_color` と同じ黒（`&H00000000`）を提案値とする。
- **`comment_label`**: 追加要望（ラベル）の文言を**ハードコードせず設定管理**する（CLAUDE.md 制約）。空文字にすればラベル無効化＝ラベルを付けない。既定は `"コメント："`。設定画面 UI には出さず setting.json で管理する（本要望はラベルの可変化までは求めていないため、UI 追加は将来拡張 §12 に記載）。

---

## 5. 設定画面（settings_window.py）

### 5.1 UI 変更（`_build_subtitle_tab`）
**塗り色**（HTML `#RRGGBB`・`with_alpha=False`）を1項目→3項目、**アウトライン色**（ASS `&HAABBGGRR`・`with_alpha=True`）を1項目→3項目へ拡張する。

```
--- 塗り色 (with_alpha=False) ---
配信者カラー           [____#01D5ED____] [■]   ← 現 color_edit を流用・ラベル改称
サブカラー             [____#FFF100____] [■]   ← 新規 sub_color_edit
コメントカラー         [____#FFFFFF____] [■]   ← 新規 comment_color_edit
--- アウトライン色 (with_alpha=True) ---  ← 追加2
配信者アウトラインカラー  [__&H00000000__] [■]  ← 現 outline_color_edit を流用・ラベル改称
サブアウトラインカラー    [__&H00000000__] [■]  ← 新規 sub_outline_color_edit
コメントアウトラインカラー [__&H00000000__] [■]  ← 新規 comment_outline_color_edit
```
- 既存 `self.color_edit`（＝配信者塗り）／`self.outline_color_edit`（＝配信者アウトライン）はそのまま利用し、ラベルのみ「文字色」→「配信者カラー」、「アウトラインの色」→「配信者アウトラインカラー」に改称。
- 塗り: `self.sub_color_edit` / `self.comment_color_edit` を新設し `self._make_color_picker(edit, with_alpha=False)` を再利用。
- アウトライン: `self.sub_outline_color_edit` / `self.comment_outline_color_edit` を新設し `self._make_color_picker(edit, with_alpha=True)` を再利用（色見本ボタン・手入力追従・アルファ選択も既存機構のまま）。
- `__init__` のウィジェット参照初期化に **計4参照**（塗り2＋アウトライン2）を追加。
- 配置は「アウトラインの太さ」設定の近傍に3アウトライン欄をまとめる（既存レイアウトの並びを尊重）。

### 5.2 読込・保存
- `_load_to_ui`（L702 付近）:
  ```python
  # 塗り色
  self.color_edit.setText(subtitle.get("own_subtitle_color", ""))
  self.sub_color_edit.setText(subtitle.get("sub_subtitle_color", ""))
  self.comment_color_edit.setText(subtitle.get("comment_subtitle_color", ""))
  # アウトライン色 (追加2)
  self.outline_color_edit.setText(subtitle.get("outline_color", ""))
  self.sub_outline_color_edit.setText(subtitle.get("sub_outline_color", ""))
  self.comment_outline_color_edit.setText(subtitle.get("comment_outline_color", ""))
  ```
- `_collect_settings`（L801 付近, `subtitle` の update 辞書）:
  ```python
  # 塗り色
  "own_subtitle_color": self.color_edit.text().strip(),
  "sub_subtitle_color": self.sub_color_edit.text().strip(),
  "comment_subtitle_color": self.comment_color_edit.text().strip(),
  # アウトライン色 (追加2)
  "outline_color": self.outline_color_edit.text().strip(),
  "sub_outline_color": self.sub_outline_color_edit.text().strip(),
  "comment_outline_color": self.comment_outline_color_edit.text().strip(),
  ```
  ※ 既存 `outline_color` の読込/保存行は既にあるため、行の重複追加に注意（サブ/コメント2行のみ新設）。
- プレースホルダ定数を塗り2色分＋アウトライン2色分追加（ハードコード回避。既存 `PLACEHOLDER_OUTLINE_COLOR` を流用可）。

---

## 6. テロップ一覧画面（subtitle_editor_dialog.py）

### 6.1 列構成の変更
```
時間(0) / 字幕(1) / 使用(2) / 配信者(3) / サブ(4) / コメント(5)
```
- `_COL_STREAMER=3`, `_COL_SUB=4`, `_COL_COMMENT=5` を追加。`_COLUMN_HEADERS` に `"配信者","サブ","コメント"` を追記。
- 役割3列は `ResizeToContents`。

### 6.2 排他選択の実装（1行1択）
QTableWidget のチェックボックスは相互に独立するため、**行ごとに `QButtonGroup`（排他）＋各セルに `QRadioButton`** を配置して「1行1つ」を保証する。

- 役割キー→列の対応リストを定数化：
  ```python
  _ROLE_COLUMNS = [("streamer", _COL_STREAMER), ("sub", _COL_SUB), ("comment", _COL_COMMENT)]
  ```
- `_populate` で各行につき:
  ```python
  group = QButtonGroup(self)          # 行内で排他
  group.setExclusive(True)
  self._role_groups.append(group)     # 行→group を保持（result 取得用）
  for role_key, col in _ROLE_COLUMNS:
      radio = QRadioButton()
      radio.setChecked(item.get("role", "streamer") == role_key)
      cell = _centered_widget(radio)  # QHBoxLayout で中央寄せしたコンテナ
      self.table.setCellWidget(row, col, cell)
      group.addButton(radio, col)     # id に列番号を持たせ、選択列を逆引き可能に
  ```
- 既定選択は **配信者(streamer)**（＝旧挙動＝現状色）。
- 「全選択/全解除」ボタン（使用列）は現状維持。役割の一括変更ボタンは要望外のため追加しない（将来拡張として §11 に記載）。

### 6.3 入出力 I/F 変更
- 入力 items に `role`（既定 `"streamer"`）を許容（未指定でも動作）。
- `result_items()`（L199-213）は各行に **`role`** を追加して返す：
  ```python
  # 戻り値: [{"start","end","text","use","role"}]
  checked_id = self._role_groups[row].checkedId()   # 列番号
  role = _col_to_role(checked_id)  # 未選択(-1)時は "streamer" にフォールバック
  ```
- コメント（先頭 docstring・L92）を新 I/F へ更新。

> `main_window.SubtitleReviewBridge`（L71-88）は items をそのまま受渡すだけのため **変更不要**（`role` はそのまま透過する）。

---

## 7. 焼き込み反映（subtitle_generator.py）

### 7.1 FontProfile を「役割別スタイル」対応へ拡張
- `FontProfile.__init__` に **役割別・塗り色マップ** と **役割別・アウトライン色マップ（追加2）** を追加（既存引数は不変・後方互換）：
  ```python
  # role_colors:        {"streamer":"#..", "sub":"#..", "comment":"#.."}  (HTML #RRGGBB)
  #   省略/欠落キー → 既存 color_hex（配信者塗り色）へフォールバック
  # role_outline_colors:{"streamer":"&H..","sub":"&H..","comment":"&H.."} (ASS &HAABBGGRR)
  #   省略/欠落キー → 既存 outline_color（配信者アウトライン色）へフォールバック
  def __init__(self, ..., role_colors=None, role_outline_colors=None):
      ...
      self.role_colors = self._normalize_role_colors(role_colors)
      self.role_outline_colors = self._normalize_role_outline_colors(role_outline_colors)
  ```
- ASS Style 行を **役割別・名前付き（塗り色＋アウトライン色を差替）** で出せるヘルパを追加：
  ```python
  # 指定 Style 名・指定塗り色(HTML)・指定アウトライン色(ASS) で Style 値を生成
  def to_ass_style_named(self, style_name, color_hex, outline_color): ...
  # 役割→ASS Style名 の対応
  ROLE_STYLE = {"streamer": "Streamer", "sub": "Sub", "comment": "Comment"}
  ```
- アウトライン色の正規化は既存 `_safe_ass_color()`（`&H` 検証・不正時フォールバック）を役割ごとに適用する。塗り色は既存 `_hex_to_ass_color()` を流用。
- 既存 `to_ass_color()` は配信者塗り色用として維持。
- **（追加：コメントラベル）** コメント役割の先頭ラベルも FontProfile に保持し、コメント描画（塗り色＋アウトライン色＋ラベル）を1箇所へ集約する：
  ```python
  def __init__(self, ..., role_colors=None, role_outline_colors=None, comment_label="コメント："):
      ...
      self.comment_label = comment_label or ""   # 空文字ならラベル無効
  ```

### 7.2 build_subtitle_file を役割別 Style 出力へ
- ヘッダに **3つの Style 行**（`Streamer`/`Sub`/`Comment`）を出力。各役割で **PrimaryColour（塗り）と OutlineColour（アウトライン）を差替**える（追加2）：
  ```
  Style: Streamer,<family>,<size>,<配信者塗り>,<配信者アウトライン>,...
  Style: Sub,     <family>,<size>,<サブ塗り>,  <サブアウトライン>,...
  Style: Comment, <family>,<size>,<コメント塗り>,<コメントアウトライン>,...
  ```
  ※ 色（塗り＋アウトライン）以外（フォント・アウトライン太さ・配置・余白・背景等）は全役割で共通（要望は塗り色とアウトライン色のみ差別化）。
  ※ `iter_role_styles()` は各役割につき `to_ass_style_named(名前, role_colors[役割], role_outline_colors[役割])` を返す。
- 各 `Dialogue` 行の Style フィールドを **entry の role で切替**、かつ **コメント役割は先頭にラベル行を付与**（追加要望5）：
  ```python
  role = entry.get("role", "streamer")
  text = entry["text"].replace("\n", "\\N")
  # コメント役割のみ先頭に「コメント：」＋改行(\N)を付与（本文は既に \N 折返し済み）
  if role == "comment" and font_profile.comment_label:
      text = f"{font_profile.comment_label}\\N{text}"
  style_name = font_profile.style_for_role(role)   # 未知 role は Streamer
  body_lines.append(f"Dialogue: 0,{start},{end},{style_name},,0,0,0,,{text}")
  ```
- `role` 欠落 entry（CLI/レビュー無効経路）は `streamer` 既定 → **配信者色＝旧挙動**（ラベルも付かない）。

### 7.3 role をタイムラインへ引き回す（`_review_timeline` L527-561）
- レビュー画面へ渡す items に `role` 初期値を付与（既定 streamer）：
  ```python
  items = [{"start":e["start"],"end":e["end"],"text":e["text"],"use":True,
            "role": e.get("role","streamer")} for e in timeline]
  ```
- 使用抽出時に `role` を保持：
  ```python
  used = [{"start":e["start"],"end":e["end"],
           "text": wrap_by_length(e["text"], max_len),
           "role": e.get("role","streamer")}
          for e in edited if e.get("use", True)]
  ```
- CLI/レビュー無効・コールバック無し経路（L529-535）は従来どおり role 無し timeline を返す → build 側で streamer 既定に落ちる（後方互換）。

> `adjust_display_timing`（L489）・`_apply_padding`（L388）はレビュー**前**に走るため role を扱う必要はない（これらは start/end/text のみ再構築）。役割はレビューでユーザーが確定する。

### 7.4 run() で役割別色（塗り＋アウトライン）・ラベルを FontProfile へ渡す（L583-601）
```python
font_profile = FontProfile(
    ...,
    color_hex=subtitle_cfg.get("own_subtitle_color", "#FFFFFF"),     # 配信者塗り(既存)
    outline_color=subtitle_cfg.get("outline_color", _DEFAULT_OUTLINE_COLOR),  # 配信者アウトライン(既存)
    role_colors={   # 塗り色 (HTML)
        "streamer": subtitle_cfg.get("own_subtitle_color", "#FFFFFF"),
        "sub":      subtitle_cfg.get("sub_subtitle_color", ""),
        "comment":  subtitle_cfg.get("comment_subtitle_color", ""),
    },
    role_outline_colors={   # アウトライン色 (ASS &HAABBGGRR) ← 追加2
        "streamer": subtitle_cfg.get("outline_color", ""),
        "sub":      subtitle_cfg.get("sub_outline_color", ""),
        "comment":  subtitle_cfg.get("comment_outline_color", ""),
    },
    comment_label=subtitle_cfg.get("comment_label", "コメント："),  # 追加(ラベル)
)
```
※ 塗り色は欠落時 `_normalize_role_colors` が配信者塗り色へ、アウトライン色は欠落時 `_normalize_role_outline_colors` が配信者アウトライン色へフォールバックする。

### 7.5 コメントラベルの付与位置と折返しの関係（追加要望5・設計判断）
- **付与箇所は `build_subtitle_file`**（焼き込み直前）とする。`_review_timeline` の `wrap_by_length`（15文字自動改行）は**本文にのみ**掛かり、
  ラベルは wrap 後に別行（`コメント：\N` の後に本文）として付くため、**ラベルが折返しに巻き込まれない**。
- 付与は**出力時のみ**。編集一覧の本文セルにはラベルを表示しないため、再編集・再焼き込みでも**二重付与は起きない**
  （タイムラインの `text` は本文のみを保持し、ラベルは build 時に一度だけ合成する）。
- `comment_label` が空文字の場合はラベルを付けない（＝色分けのみ）。
- 対象は `role == "comment"` の行に限定。配信者・サブには付与しない。

---

## 8. データフロー（役割の流れ）
```
setting.json(塗り3色＋アウトライン3色) ──► run(): FontProfile(role_colors, role_outline_colors)
音声認識 timeline{start,end,text}
   └► adjust_display_timing / (発話ゲートは無効化中)     … role 未付与
   └► _review_timeline:
         items{...,use=True, role="streamer"(既定)} ─► SubtitleEditorDialog
              ユーザーが行ごとに 配信者/サブ/コメント を排他選択
         result_items(){...,use,role}
         used{start,end,text,role}
   └► build_subtitle_file:
         [V4+ Styles] Streamer/Sub/Comment を「塗り色＋アウトライン色」違いで3定義
         Dialogue 各行 → role に対応する Style を参照
                       → role=="comment" は先頭に「コメント：\N」を合成（追加:ラベル）
   └► burn_subtitle → 焼き込み（役割ごとに塗り色＋アウトライン色、コメントは先頭ラベル付き）
```

---

## 9. 後方互換・エラー処理方針
- **既存 setting.json**: 新5キー（`sub_subtitle_color`/`comment_subtitle_color`/`sub_outline_color`/`comment_outline_color`/`comment_label`）は merge で自動補完。既存 `own_subtitle_color`（配信者塗り）・`outline_color`（配信者アウトライン）は不変。
- **アウトライン色（追加2）**: サブ/コメントのアウトライン色未設定→配信者アウトライン色（`outline_color`）へフォールバック。不正 `&H` 値は既存 `_safe_ass_color()` により既定黒へフォールバック。
- **コメントラベル（追加:ラベル）**: `comment_label` 未設定→既定「コメント：」。空文字→ラベル無し。付与は出力時のみ・コメント役割のみ・build で一度だけ合成のため二重付与や折返し巻き込みは発生しない。
- **CLI / レビュー無効 / faster-whisper 未導入**: role 選択画面を通らず、全行 `streamer` 既定＝**現状と完全に同じ挙動**（配信者の塗り＋アウトライン）。
- **色文字列の不正値**: 塗りは `_hex_to_ass_color()` の6桁検証、アウトラインは `_safe_ass_color()` の `&H` 検証を踏襲し、不正時はフォールバック。役割別の各色に同ロジックを適用。
- **役割未選択（万一 group が全 OFF）**: `checkedId()==-1` → `streamer` にフォールバック（テロップが消えることを防ぐ）。
- **既存 ASS 単一 Style 参照との差異**: 全 Dialogue が必ず既定義の Style 名（Streamer/Sub/Comment）を参照するため、未定義 Style 参照は発生しない。

---

## 10. 変更ファイル一覧
| ファイル | 変更内容 | 節 |
|---|---|---|
| `src/settings/setting.json` | 塗り2キー（`sub_subtitle_color`/`comment_subtitle_color`）＋**アウトライン2キー（`sub_outline_color`/`comment_outline_color`）**＋`comment_label` 追加（色の既定は提案値・要確認） | §4 |
| `src/settings/settings_window.py` | DEFAULT_SETTINGS 5キー追加（塗り2＋アウトライン2＋`comment_label`）／プレースホルダ定数追加／**塗りピッカー2＋アウトラインピッカー2 追加**＋既存ラベル改称（配信者カラー/配信者アウトラインカラー）／load・collect 拡張／ウィジェット参照4追加。※`comment_label` は UI 非表示・設定値の維持のみ | §4,5 |
| `src/gui/subtitle_editor_dialog.py` | 役割3列追加／行ごと排他 `QButtonGroup`＋`QRadioButton`／`_populate`・`result_items` に role 反映／中央寄せヘルパ・列定数追加（**追加2による変更なし**：役割選択 UI は塗り/アウトライン共通） | §6 |
| `src/modules/subtitle_generator.py` | `FontProfile` に `role_colors`／**`role_outline_colors`**／`comment_label`＋`to_ass_style_named(名前,塗り,アウトライン)`／`build_subtitle_file` を3 Style 出力（塗り＋アウトライン差替）＋Dialogue の Style 切替＋コメント先頭ラベル合成／`_review_timeline` で role 引回し／`run()` で role_colors・role_outline_colors・comment_label 供給 | §7 |
| `src/gui/main_window.py` | **変更なし**（items を透過するのみ） | §6.3 |

---

## 11. テスト観点（レビュー後の検証項目）
1. 設定画面で塗り3色＋アウトライン3色を保存 → setting.json に塗り6キー相当（塗り `#RRGGBB` / アウトライン `&HAABBGGRR`）が保存される。色見本ボタンが各欄に追従する。
2. 一覧で 1行に 配信者/サブ/コメント のいずれか1つだけ選択できる（他を選ぶと前選択が外れる）。
3. 役割を混在させて焼き込み → 生成 ASS に Streamer/Sub/Comment の3 Style が定義され、各 Style の **PrimaryColour（塗り）と OutlineColour（アウトライン）が役割ごとに異なる**。各 Dialogue が選択役割の Style を参照し、実映像で行ごとに塗り色・縁色が変わる。
4. 既定（全行=配信者）で焼くと **従来と同一の見た目**（own_subtitle_color＋outline_color）になる（後方互換）。
5. 旧 setting.json（新キー無し）で起動 → 既定補完で動作、エラーなし。サブ/コメントのアウトライン未設定時は配信者アウトライン色になる。
6. CLI/レビュー無効経路で従来どおり全行=配信者（塗り＋アウトライン）で焼ける。
7. 不正な塗り/アウトライン色文字列を手入力 → それぞれフォールバック色で焼き込み（クラッシュしない）。
8. **（追加:ラベル）** コメント役割の行を焼くと、生成 ASS の該当 Dialogue が `コメント：\N<本文>` になり、実映像で1行目「コメント：」＋改行後に本文が表示される。配信者・サブ役割にはラベルが付かない。
9. **（追加:ラベル）** 本文が15文字超のコメント行でも「コメント：」が独立行として保たれ、本文の折返しに巻き込まれない。編集画面で再確定→再焼き込みしてもラベルが二重に付かない。
10. **（追加:ラベル）** `comment_label` を空文字にするとコメント行にラベルが付かない（色分けのみ）。

---

## 12. 将来拡張（要望外・任意）
- 役割の**一括設定**ボタン（全行を配信者/サブ/コメントへ）— 使用列の全選択ボタンと同様の利便機能。
- 役割ごとに**フォント・アウトライン太さ・配置・背景色**も個別化（現設計は塗り色＋アウトライン色を差別化。Style を役割別に持つため他属性の役割別化も同じ枠組みで拡張容易）。
- `comment_label` の設定画面 UI 化（現状は setting.json 管理のみ）。
- 役割数の増減（キャスト別カラー等）— `_ROLE_COLUMNS` / `ROLE_STYLE` / 色キー（塗り・アウトライン）の表駆動化で対応可能。

---

## 13. 未確定事項（要ユーザー確認）
- **サブ／コメントの塗り既定色**（本書提案: サブ `#FFF100`、コメント `#FFFFFF`）。確定色があれば差し替える。
- **（追加2）サブ／コメントのアウトライン既定色**（本書提案: いずれも配信者と同じ黒 `&H00000000`）。確定色があれば差し替える。
- 一覧の役割 UI は **ラジオボタン**で設計（「1行1択」を素直に満たすため）。チェックボックス希望なら排他制御を itemChanged で自作する案に切替可。役割選択は塗り・アウトラインで**共通**（1つの選択で両方適用）。
- **（追加:ラベル）コメントラベルの文言**（本書既定: `コメント：`）。全角コロン・半角スペース有無など、確定表記があれば `comment_label` を差し替える。
- **（追加:ラベル）ラベルの改行位置**: 本設計は「ラベル＝1行目 / 本文＝2行目以降」（ラベル直後に `\N`）。「`コメント：`＋本文を同一行に連結（改行なし）」を希望する場合は §7.2 の合成を `f"{label}{text}"` に変更する（要望文「改行する」に従い**改行あり**を既定とした）。
