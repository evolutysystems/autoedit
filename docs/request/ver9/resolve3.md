# ver9 resolve3 — テロップ 1 本ごとの文字寄せ (左/中央/右) をインスペクタで選べるようにする

対象: `autoedit` (Stretheus クライアント) / Timeline 編集画面のインスペクタ
要望: `docs/request/ver9/request3.md` (「(追加) 配置ではなく、テロップの文字寄せのことです。」を含む)
関連: `docs/request/ver3/resolve6.md` §3 (テロップ個別の文字色・縁の色 = 1 本ごとの上書きの先例),
      `docs/request/ver3/resolve11.md` §3 D-4/D-10 (コメントの `\an4` 規約と文字幅の推定),
      `docs/request/ver3/resolve.md` §8.3 (位置上書き `\pos`),
      `docs/request/ver6/resolve3.md` §4.7-3 (プレビューの行寄せ `_line_align`),
      `docs/error/20261023/resolve.md` §5 案A (`font_advance` = フォントから読む送り幅),
      `docs/request/ver9/resolve2.md` (直前の要望。インスペクタ/メニューの改修規約)

作成日: 2026-10-26 (main `115e257` / v1.6.1 + resolve2 実装中)
状態: **設計のみ。実装はレビュー後に行う** (CLAUDE.md「いきなり実装を開始しない」)

---

## 0. 要望と結論

> Timeline 画面でテロップを選択した際に、寄せ方の選択肢を追加してください。
> デフォルトは設定画面から設定されたものを選択しているようにしてください。
> 現在何が選択されているかどうかわかるようにし、以下 3 つの選択肢から
> 1 つしか選択できないようにしてください。
> できればコンボボックスではなく、エクセルのような 3 つ並んだボタンが好ましい。
> - 左寄せ / 中央寄せ / 右寄せ
>
> **(追加) 配置ではなく、テロップの文字寄せのことです。**

### 0.1 「文字寄せ」の定義 (この設計で実現するもの)

**テロップの表示位置 (左右・上下) は一切動かさず、複数行の行どうしの揃えだけを変える。**

```text
【中央寄せ = 現状】            【左寄せ】                  【右寄せ】
┌──── 画面 1920 ────┐   ┌──── 画面 1920 ────┐   ┌──── 画面 1920 ────┐
│                   │   │                   │   │                   │
│    これは短い行      │   │  これは短い行        │   │      これは短い行    │
│  こちらはずっと長い行  │   │  こちらはずっと長い行  │   │  こちらはずっと長い行  │
└───────────────────┘   └───────────────────┘   └───────────────────┘
   行ごとに中央へ              行頭がそろう               行末がそろう
```

- **一番長い行は 1px も動かない。** 短い行だけがその行に合わせて寄る。
- 上下の段 (下段/中段/上段) と余白 (`margin_l` / `margin_r` / `margin_v`) は触らない。
- **1 行だけのテロップでは見た目が変わらない** (行が 1 本しかないため)。
  効くのは折り返しで 2 行以上になったテロップ (既定 `subtitle_max_lines: 2`)。

### 0.2 結論 (やること)

**テロップ 1 本ごとの「文字寄せ」上書き (`SubtitleClip.align`) を新設**し、
インスペクタに **3 つ並んだ排他ボタン**を置く。

| | 内容 |
| --- | --- |
| 持ち方 | `SubtitleClip.align` = `""` / `"left"` / `"center"` / `"right"`。`""` = **設定に従う** (既定) |
| 既定表示 | `""` のときは設定の「表示位置」(`alignment` / 縦なら `vertical.alignment`) の左右成分のボタンが押された状態で出る (要望「デフォルトは設定画面から設定されたもの」/ §R3) |
| 現在値 | `QPushButton` の `checked` 表示 (アクセント色) でひと目で分かる |
| 排他 | `QButtonGroup` (`setExclusive(True)`) で**必ず 1 つだけ**選択される |
| 出力 (ASS) | **行の箱 (= 一番長い行) を今と同じ場所に固定したまま、箱の中で行を寄せる**。箱の幅は `font_advance` で実測する (§1.6 で実機検証済み) |
| 後方互換 | 未指定のテロップは `\an` もイベント余白も出さない = **生成 ASS がバイト一致** (§6) |

### 0.3 前回提出した resolve3 からの変更点 (要望「(追加)」の反映)

| | 前回 (配置として設計) | 今回 (文字寄せとして設計) |
| --- | --- | --- |
| 左寄せの意味 | テロップ全体が左余白 `margin_l` から始まる (= **位置が動く**) | **位置は動かない。**行頭がそろうだけ |
| ASS の作り | `\an{段+1}` を足すだけ | `\an` + **イベント単位の余白 / `\pos` の補正**で箱を固定 |
| 必要な前提 | なし | **最長行の文字幅の実測** (`font_advance` / §1.3) |
| ドラッグ済みテロップ | 対象外 (ボタン無効) | **対象にできる** (`\an4`/`\an6` + `\pos` 補正 / §1.6 検証3) |
| 1 行テロップ | 見た目が変わる | 見た目は変わらない (仕様 / §R2) |

---

## 1. 現状分析

### 1.1 行の寄せは「配置 (`\an`) の左右成分」に従っている

ASS では配置と行の寄せが同じ 1 つの数値に同居している。

```text
7 8 9   上段 (左 中央 右)      段 = 上下位置 / 列 = 行の寄せ
4 5 6   中段                  既定 subtitle.alignment = 2 → 下段・中央
1 2 3   下段                  → 2 行テロップは各行が中央へ寄る (= 現状)
```

| 経路 | 参照 |
| --- | --- |
| 焼き込み (ASS Style) | `subtitle_generator.to_ass_style_named` L205 (`placement_for_role` の第 1 要素) |
| 役割別の配置 | `subtitle_generator.placement_for_role` L187 (`comment` のみ `comment_alignment`) |
| プレビューの行寄せ | `preview_items._line_alignment` L608 → `_text_path` L452 |
| DaVinci Resolve 連携 | `resolve_export._align_name` L96 → title の `alignment` 属性 |
| 縦動画の差し替え | `subtitle_generator.build_effective_subtitle_cfg` L864 |

**クリップ 1 本ごとに寄せを持つ仕組みは今どこにも無い**
(`font` / `font_size` / `color` / `outline_color` / 位置だけが 1 本ごとの上書きを持つ)。

### 1.2 プレビューは既に「箱の位置」と「行の寄せ」を分けて持っている

```python
# preview_items.py L452 _text_path()  ← 行ごとに箱 (_text_width = 最長行) の中で寄せている
if self._line_align == "right":
    left = self._text_width - width
elif self._line_align == "center":
    left = (self._text_width - width) / 2.0
else:
    left = 0.0
```

`_place()` (箱をどこへ置くか) と `_line_align` (箱の中で行をどう寄せるか) が
**既に分離済み**。つまり**プレビュー側は `_line_alignment` の戻り値を差し替えるだけで
要望どおりに動く** (箱の位置には手を触れない)。

### 1.3 文字幅を実測する資産が既にある (`src/modules/font_advance.py`)

コメント背景の箱の幅を出すために導入済み (`docs/error/20261023/resolve.md` §5 案A)。

- フォントファイルの `hmtx` から送り幅を読み、libass の縮尺
  `unitsPerEm / (usWinAscent + usWinDescent)` を掛けて px を出す。
- **Qt にも ffmpeg にも依存しない純 Python**。読めないときは `None` を返す。
- 探索先は 同梱 `fonts` → `font_file_dirs` → OS のフォントフォルダ。
- 入口は `comment_decor.text_extent(item, eff_cfg, text=...)`
  → `{"width": 最長行の幅, "height", "lines", "measured"}`。
  **コメント専用ではなく汎用**で、`\N` も実改行も行として数える。

→ 文字寄せに必要な「最長行の幅」はこれで取れる。新しい測定機構は作らない。

### 1.4 インスペクタの現状 (`timeline_editor_dialog._InspectorPanel`)

```text
字幕クリップ（n 件選択）
開始 0:12.345 / 尺 2.00 秒 …
┌ 字幕     [ 複数行テキスト ]
│ 役割     [ 配信者 ▼ ]
│ フォント [ (設定のフォント) ▼ ]
│ サイズ   [ 既定 ]
│ 文字色   [ ■ #FFFFFF ]
│ 縁の色   [ ■ &H00FFFFFF ]
└ ※ 文字色・縁の色は選択中の n 件すべてに適用されます…
[ 位置を既定へ戻す ]
```

- 1 件ごとの項目 (`font`/`font_size`) は `edit_subtitle`、複数へ効く項目 (色) は
  `edit_subtitles` という使い分けが既にある (L1842〜)。
- `show_clip()` L1640 が選択のたびに値を流し込み、`self._updating` で
  「流し込み中のシグナルでコマンドを積まない」を守っている。
- `theme` の QSS に **`QPushButton:checked` がアクセント色で既に定義済み** (L807)
  → **チェック可能ボタンを 3 つ並べるだけで「エクセルのような」見た目になる** (追加 QSS 不要)。

### 1.5 コマンドは追加不要 (`EditSubtitle` / `EditSubtitles` が汎用)

```python
# commands.py L1412 / L1550
for key, value in self._fields.items():
    if not hasattr(clip, key):
        continue
    if getattr(clip, key) != value:
        setattr(clip, key, value)
```

`SubtitleClip` に `align` を足せば `edit_subtitles(ids, align="left")` がそのまま効き、
Undo も 1 手で入る。**新コマンドは作らない。**
`setattr` は値を検証しないため、`align` は**プロパティにして setter で正規化**する (§4.1)。

### 1.6 実機検証 (同梱 ffmpeg / libass + `font_advance`)

推測で方式を選ばないため、以下を実際に描画して確認した。

#### 検証0 — libass は `Justify` スタイル拡張を**実装していない**

`[V4+ Styles]` の Format 行へ `Justify` を足し、値 0/1/2/3 と無指定の 5 通りを
1920x1080 の 2 行テロップで描画 → **5 枚すべて同一ハッシュ** (`WrapStyle` 0 / 2 の両方で同じ)。
対照として `Alignment` を 2→1 に変えた場合は描画が変わる (= 検証系は正しい)。

→ **「配置を動かさずに行だけ寄せる」ASS の標準機能は無い。** 自前で箱を作る必要がある。

#### 検証1 — 位置指定なし: `\an{段+列}` + イベント単位の MarginL/MarginR

条件: Yu Gothic UI 太字 / `font_size` 100 / `outline` 10 / `MarginL=MarginR=40` /
2 行 (`これは短い行` / `こちらはずっと長い行です`)。
`font_advance` の実測で最長行 = **735.0px**、余白内 1840px → 余り **1105px**。
左寄せは `{\an1}` + イベント `MarginL = 40 + 1105/2 = 593`。

| 描画 | 行1 (短い) 左端〜右端 | 行2 (最長) 左端〜右端 | 上下 |
| --- | --- | --- | --- |
| 中央寄せ (現状) | 765 〜 1155 | 597 〜 1323 | 行1 838-906 / 行2 935-1006 |
| **左寄せ** | **597** 〜 987 | **597** 〜 1323 | **同一** |
| **右寄せ** | 933 〜 **1323** | 596 〜 **1322** | **同一** |

→ **最長行は動かず (597→597 / 誤差 0〜1px)、短い行の行頭・行末がそろう。上下も不動。**

#### 検証2 — 本番の設定値でも余白に収まる

`font_size` 130 / 全角 20 文字 (`subtitle.max_line_length` の上限) の実測幅:

| font_size | 最長行の幅 | 余白内の幅 | 余り |
| --- | --- | --- | --- |
| 130 | 1634.1 | 1840 | **+205.9** (寄せられる) |
| 100 | 1257.0 | 1840 | +583.0 |
| 90 (縦動画) | 1131.3 | 1840 | +708.7 |

→ 既定値では成立する。ただし**幅の広いフォント・大きな `font_size` では余りが
0 以下になり得る**ため、そのときは寄せを諦める仕様にする (§R8)。

#### 検証3 — 位置ドラッグ済み (`\pos`) でも寄せられる

現状の通常テロップは `\an5\pos(cx,cy)` (中央アンカー)。
これを 左寄せ = `\an4\pos(cx - 幅/2, cy)` / 右寄せ = `\an6\pos(cx + 幅/2, cy)` にする。

| 描画 | 行1 (短い) | 行2 (最長) | 上下 |
| --- | --- | --- | --- |
| 中央 (現状) | 765 〜 1155 | 597 〜 1323 | 458-526 / 555-626 |
| **左寄せ (an4)** | **597** 〜 986 | **597** 〜 1323 | **同一** |
| **右寄せ (an6)** | 934 〜 **1324** | 597 〜 **1323** | **同一** |

→ 段が中段系 (4/5/6) のままなので**上下は不変**。最長行も不動。
   **前回設計で「対象外」とした位置ドラッグ済みテロップも対象にできる。**

---

## 2. 設計方針

1. **箱を動かさない。** 「一番長い行がいま居る場所」を箱の基準として固定し、
   その中で行を寄せる。これが要望「配置ではなく文字寄せ」の実装上の定義 (§0.1)。
2. **測定は既存資産 1 本に寄せる。** 箱の幅は `comment_decor.text_extent`
   (= `font_advance` の実測) のみを使う。新しい測定機構は作らない (§1.3)。
3. **測れないときは寄せない。** フォントを読めない / 余白に収まらない場合は
   **現状の描画をそのまま出す** (警告を 1 回だけログへ)。
   固定係数の推定 (フォントにより 1.5 倍ずれる) で箱をずらすと**テロップの位置が
   ぶれる**ため、位置が動くより「寄らない」方を選ぶ (§R8)。
4. **未指定なら完全に従来どおり。** `to_item()` にキーを足さない →
   ASS・プレビュー・Resolve 連携の出力が 1 バイトも変わらない (§6)。
5. **判定と計算は 1 か所に置く。** 「設定 + 文字寄せ → 実効の `\an` と箱」の計算は
   `subtitle_generator` の関数へ集め、ASS / プレビュー / Resolve が同じ関数を呼ぶ。
6. **設定画面は変えない。** 既定の出どころは既存の「表示位置」のまま (§R3)。
7. **新コマンドを作らない** (`EditSubtitles` が汎用 / §1.5)。

---

## 3. 仕様

### R1 置き場所と見た目

インスペクタの「サイズ」と「文字色」の間へ 1 行追加する。

```text
│ サイズ   [ 既定 ]
│ 文字寄せ [ 左寄せ ][ 中央寄せ ][ 右寄せ ]     ← 新規。押されている 1 つがアクセント色
│ 文字色   [ ■ #FFFFFF ]
```

- 3 つの `QPushButton` (`setCheckable(True)`) を `QHBoxLayout` で隙間 0 に並べ、
  `QButtonGroup` で排他にする。選択中は既存 QSS の `:checked` で着色される。
- 幅は `timeline.ui.subtitle_align_button_width_px` (既定 56)。ハードコードしない。
- 行のラベルは `tr("文字寄せ")`。ツールチップ:

```text
複数行になったテロップの行の揃えです
テロップの表示位置 (左右・上下) は動きません。1 行のテロップでは見た目は変わりません
```

### R2 値と意味

| 値 | 意味 | 効果 |
| --- | --- | --- |
| `""` | 設定の「表示位置」に従う (既定) | タグを出さない (従来と同一) |
| `"left"` | 左寄せ | 行頭をそろえる (箱の左端へ) |
| `"center"` | 中央寄せ | 行ごとに箱の中央へ |
| `"right"` | 右寄せ | 行末をそろえる (箱の右端へ) |

- **箱** = 幅が「一番長い行の幅」で、位置は現状の最長行の位置と同じ矩形。
- **1 行だけのテロップでは見た目が変わらない** (仕様。ボタンは押せるし保存もされる)。
- 上下の段・余白は変わらない。

### R3 既定の表示 (要望「デフォルトは設定画面から設定されたもの」)

`align == ""` のとき、**設定の「表示位置」の左右成分**のボタンを押した状態で出す
(いまの libass の実挙動がそれであるため、見えている寄せと一致する)。

```text
subtitle.alignment = 2 (下段・中央)     → 「中央寄せ」が押された状態
subtitle.alignment = 1 (下段・左)       → 「左寄せ」
縦動画 (vertical.alignment = 5)         → 「中央寄せ」
コメント役割 (comment_alignment = 4)    → 「左寄せ」
```

使う設定は**プレビューと同じ実効設定** (`build_effective_subtitle_cfg` 適用後) とし、
縦動画プロジェクトでは縦の値を見る。

### R4 押したときの動き

| 操作 | 結果 |
| --- | --- |
| 設定と違うボタンを押す | `align` にその値を保存 (`edit_subtitles`) / Undo 1 手 |
| 設定どおりのボタンを押す (未指定の状態から) | `align` に**明示値として保存**する (後から設定を変えてもこの 1 本は動かない) |
| 既に押されているボタンをもう一度押す | `timeline.subtitle_align.clear_on_reclick=true` (既定) なら `""` へ戻す = 設定追従。`false` なら何も起きない |

### R5 役割ごとの扱い

| 役割 | 扱い |
| --- | --- |
| 配信者 / サブ | そのまま対象。箱は設定の配置から決まる (§4.2-b) |
| コメント | 既定では対象外 (ボタン無効)。`allow_comment_role=true` で解禁 (§7.1) |

コメントを既定で外す理由: コメントは `\an4` (左アンカー) + アイコン + 黒背景の
位置が「文字の左端 = `comment_margin_l`」で組まれており (`comment_decor._text_anchor`)、
**箱の左端を動かさない限り破綻はしない**ものの、アイコン・背景との見え方の
作り込みが別途必要なため、今回は range 外とする。ツールチップ:

```text
コメントはアイコンと背景の配置が決まっているため、文字寄せを変えられません
```

### R6 位置をドラッグしたテロップも対象 (§1.6 検証3)

`clip.transform.is_positioned()` が真でも寄せられる。
保存している位置 (= 箱の中心) は変えず、`\an4` / `\an6` と `\pos` の x 補正で表現する。
→ **ドラッグした位置は動かない。**

### R7 複数選択

色と同じ扱い。`timeline.subtitle_align.apply_to_selection=true` (既定) のとき
**選択中の字幕すべて**へ 1 コマンドで適用する (Undo 1 手)。
対象外 (コメント役割) が混ざっていれば飛ばし、件数をステータスへ出す (§R9)。
表示は先頭 1 件の値 (色と同じ。「複数値」状態は持たない)。
複数選択時の案内文を「文字色・縁の色・文字寄せは選択中の…」へ更新する。

### R8 寄せられない条件 (安全側に倒す / §2-3)

| 条件 | 挙動 |
| --- | --- |
| フォントの送り幅が読めない (`measured=False`) | 寄せない (現状の描画)。WARNING を 1 回 |
| 最長行が余白内に収まらない (余り ≤ 0) | 寄せない (現状の描画)。INFO を 1 回 |
| 行が 1 本だけ | タグを出さない (出しても見た目が同じため / 出力を汚さない) |
| 実効の寄せが設定と同じ | タグを出さない (= 従来と同一出力) |

いずれも**テロップの位置は現状どおり**で、文字寄せだけが効かない。
`align` の値は保持する (フォントを入れ直せば次回から効く)。

### R9 ステータス通知

```text
文字寄せを「左寄せ」にしました
3 件の文字寄せを「右寄せ」にしました
文字寄せを設定に戻しました
3 件の文字寄せを「中央寄せ」にしました (1 件はコメントのため変えていません)
```

### R10 Undo / Redo

`EditSubtitles` 1 件 = Undo 1 手。履歴ラベルは既存の「字幕の編集（n 件）」。
ボタンは `clicked` (利用者の操作) で発火させ、`show_clip()` の流し込み中は
`self._updating` で抑止する (フォントコンボと同じ規約)。

### R11 保存・読み込み

`SubtitleClip.to_dict()` に `"align"` を足す。旧プロジェクト (キー無し) は
`from_dict` で `""` = 設定追従。コピー＆ペースト (`clipboard`) は
`to_dict`/`from_dict` を使うため自動で引き継ぐ。

### R12 出力への反映

| 経路 | 反映 | 方法 |
| --- | --- | --- |
| 焼き込み (ASS) | ○ | `\an` + イベント余白 / `\pos` 補正 (§4.2) |
| プレビュー (Qt) | ○ | `_line_alignment` の差し替えのみ (§4.3) |
| 高画質プレビュー / プロキシ | ○ | `to_item()` 経由で同じ ASS を通る (変更不要) |
| DaVinci Resolve (title) | ○ | title の `alignment` 属性だけを差し替え。**位置は変えない** (§4.4) |
| DaVinci Resolve (caption/ITT) | × | ITT に行の寄せが無い (既存制約) |
| SRT 書き出し | × | SRT に配置情報が無い (既存どおり) |
| アーカイブ切り抜き | ○ | `to_item()` 経由 (`archive/timeline_builder.py` L1116) |

### R13 機能 OFF

- `timeline.subtitle_align.enabled=false` → インスペクタに行を出さない (値は保持)。
- `subtitle.line_align.enabled=false` → 焼き込み側で寄せを無効化する (値は保持)。

UI と出力で別のスイッチにするのは、設定の置き場所が違う (UI = `timeline` /
焼き込み = `subtitle` セクションしか見えない) ためで、どちらも既定 `true`。

### R14 右クリックメニューには足さない

同じ状態を 2 か所で見せると不整合の種になるため、今回はインスペクタのみ
(将来足すなら resolve2 と同じ流儀で `_add_align_actions` の隣へサブメニュー 1 つ / Q3)。

---

## 4. 実装設計

### 変更一覧

| ファイル | 変更 |
| --- | --- |
| `src/timeline/model.py` | `SubtitleClip.align` (プロパティ) / `to_dict` / `from_dict` / `copy` / `to_item` |
| `src/modules/subtitle_generator.py` | 寄せの正規化・箱の算出・`\an` とイベント余白・`\pos` 補正・ログ |
| `src/gui/timeline/preview_items.py` | `_line_alignment` がクリップの `align` を見る |
| `src/export/resolve_export.py` | `_title_from_item` が item の `align` を `align` 属性だけへ反映 |
| `src/gui/timeline/timeline_controller.py` | 実効字幕設定の取得口 `effective_subtitle_cfg()` を追加 |
| `src/gui/timeline/timeline_editor_dialog.py` | インスペクタに 3 ボタン追加 + ハンドラ |
| `src/timeline/builder.py` | `timeline.subtitle_align` / `ui.subtitle_align_button_width_px` の検証 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ同項目 + `subtitle.line_align` |
| `src/settings/setting.json` | 配布既定値 |
| `src/i18n/catalog_en.py` | 新規文言の英訳 |
| `tests/test_subtitle_align.py` | 新規 (Qt 非依存) |

### 4.1 `src/timeline/model.py`

```python
# テロップ 1 本ごとの文字寄せ (ver9 resolve3 §R2)
# "" = 設定の表示位置 (subtitle.alignment / vertical.alignment) に従う。
SUBTITLE_ALIGNS = ("left", "center", "right")


# 文字寄せを正規化する (未知の値・None・大文字混在はすべて "" = 設定に従う)
def _to_align(value):
    text = str(value or "").strip().lower()
    return text if text in SUBTITLE_ALIGNS else ""
```

```python
    def __init__(self, ..., color="", outline_color="", align=""):
        ...
        # このクリップだけの文字寄せ (left/center/right)。"" = 設定に従う (resolve3 §R2)
        self.align = align

    # 文字寄せは setattr (EditSubtitle) から直接入るため setter で正規化する (§1.5)
    @property
    def align(self):
        return self._align

    @align.setter
    def align(self, value):
        self._align = _to_align(value)
```

- `copy()` / `to_dict()` / `from_dict()` へ `align` を追加。
- `to_item()` は**値があるときだけ**載せる (色と同じ / 未指定なら辞書が従来と完全一致):

```python
        # 文字寄せは指定があるときだけ載せる (未指定なら現行と完全に同一の辞書 / §R12)
        if self.align:
            item["align"] = self.align
```

### 4.2 `src/modules/subtitle_generator.py`

#### (a) 共通ヘルパ

```python
# テンキー配置の左右成分 (ver9 resolve3 §1.1)
_ALIGN_COLUMNS = ("left", "center", "right")


# 文字寄せ (left/center/right) を正規化する。未知・空は "" (設定に従う)
def normalize_align(value):
    text = str(value or "").strip().lower()
    return text if text in _ALIGN_COLUMNS else ""


# テンキー配置 (1〜9) から寄せを取り出す (インスペクタの既定表示用 / §R3)
def align_of_alignment(alignment):
    value = _to_int(alignment, 2)
    if not 1 <= value <= 9:
        value = 2
    return _ALIGN_COLUMNS[(value - 1) % 3]


# 段 (上下) を保ったまま左右成分だけ差し替えたテンキー配置を返す
def alignment_with_align(alignment, align):
    value = _to_int(alignment, 2)
    key = normalize_align(align)
    if not key or not 1 <= value <= 9:
        return value
    return (value - 1) // 3 * 3 + 1 + _ALIGN_COLUMNS.index(key)
```

#### (b) 箱とタグの算出 (この関数が設計の中核)

```python
# 1 エントリぶんの文字寄せタグとイベント余白を返す (ver9 resolve3 §1.6)
#
# 【重要】テロップの位置は動かさない。「一番長い行がいま居る矩形」を箱として固定し、
#   箱の中で行を寄せる。箱の幅は font_advance の実測 (comment_decor.text_extent)。
#   測れない / 箱が余白に収まらない / 行が 1 本だけ のときは何も出さない (§R8)。
#
# 戻り値 (タグ文字列, margin_l, margin_r)
#   タグ   : "\\an1" など (位置指定ありの場合は "\\an4\\pos(x,y)" ごと差し替える)
#   margin : Dialogue 行のイベント余白 (0 = Style の余白を使う = 従来どおり)
def line_align_override(entry, subtitle_cfg, font_profile, video_width, video_height,
                        text=None):
    align = normalize_align(entry.get("align"))
    if not align or subtitle_cfg is None:
        return "", 0, 0
    if not subtitle_cfg.get("line_align", {}).get("enabled", True):
        return "", 0, 0
    role = str(entry.get("role", _DEFAULT_ROLE))
    alignment, margin_l, margin_r, _margin_v = font_profile.placement_for_role(role)
    if align == align_of_alignment(alignment):
        return "", 0, 0                     # 実効が同じなら出さない (従来と同一出力)

    extent = comment_decor.text_extent(entry, subtitle_cfg, text=text)
    if extent["lines"] <= 1:
        return "", 0, 0                     # 1 行なら見た目が変わらない
    if not extent["measured"]:
        _warn_once("line_align_measure",
                   "フォントの送り幅を読めないため文字寄せを適用しません: %s",
                   entry.get("font") or subtitle_cfg.get("font_family"))
        return "", 0, 0
    width = float(extent["width"])
    ...
```

箱の求め方 (位置指定なし / §1.6 検証1):

```python
    # 箱の左端 = いまの最長行の左端 (設定の表示位置から決まる)
    available = video_width - margin_l - margin_r
    slack = available - width
    if slack <= 0:
        _logger.info("テロップが余白に収まらないため文字寄せを適用しません "
                     "(幅 %.0f > 余白内 %d)", width, available)
        return "", 0, 0
    if alignment in _ALIGN_LEFT_COLUMN:         # 1/4/7
        box_left = float(margin_l)
    elif alignment in _ALIGN_RIGHT_COLUMN:      # 3/6/9
        box_left = float(video_width - margin_r - width)
    else:                                       # 2/5/8 (既定)
        box_left = margin_l + slack / 2.0
    # 箱をイベント余白で表現し、箱の中の寄せを \an の左右成分で決める
    event_l = int(round(box_left))
    event_r = int(round(video_width - (box_left + width)))
    return f"\\an{alignment_with_align(alignment, align)}", event_l, event_r
```

位置指定あり (§1.6 検証3) は `_inline_position_override` 側で扱う:

```python
# 位置指定時のアンカーと x を文字寄せに合わせる (resolve3 §R6 / §1.6 検証3)
#   中央 (既定)  : \an5\pos(cx, cy)          ← 従来
#   左寄せ       : \an4\pos(cx - 幅/2, cy)
#   右寄せ       : \an6\pos(cx + 幅/2, cy)
# 段は中段系 (4/5/6) のままなので上下位置は変わらない (実機検証済み)。
```

- `_ALIGN_LEFT_COLUMN = (1, 4, 7)` / `_ALIGN_RIGHT_COLUMN = (3, 6, 9)` を
  `preview_items` / `resolve_export` と同じ値で持つ (既存の重複に合わせる)。
- `generate_ass_file` の Dialogue 行は現在 `,,0,0,0,,` と余白を 0 固定で書いているため、
  ここへ `event_l` / `event_r` を差し込む。コメント背景の Dialogue 行 (`background`) は
  **従来どおり 0 のまま**にする (背景は `\pos` で絶対座標を持つため余白を見ない)。
- 寄せを適用した件数を既存の INFO 行 (「テロップ個別指定 …」) へ `文字寄せ n 件` として足す。

### 4.3 `src/gui/timeline/preview_items.py`

`_line_alignment` がクリップの指定を優先するだけ。**箱の位置 (`_place`) は触らない。**

```python
# 行ごとの水平の寄せを決める (ver6 resolve3 §4.7-3 / ver9 resolve3 §4.3)
#   クリップ個別の文字寄せがあればそれが最優先 (箱の位置は変えない)
#   位置指定あり: コメントは \an4 (左) / それ以外は \an5 (中央)
#   位置指定なし: Style の alignment に従う
def _line_alignment(clip, eff_cfg, canvas_size, role):
    align = subtitle_generator.normalize_align(getattr(clip, "align", ""))
    if align:
        return align
    ...  # 以降は現状のまま
```

**プレビューと焼き込みの一致について (重要):**
プレビューの箱の幅は Qt (`QFontMetricsF`) 実測、焼き込みの箱の幅は `font_advance` 実測で、
両者は数 px 違う。**左寄せ・右寄せでは箱の幅の差が行の寄せ位置の差として出る**
(中央寄せは現状と同じで差は出ない)。差は既存の近似 (グリフ形・字間) と同程度だが、
目に付く場合は「寄せが指定されたクリップだけ `comment_decor.text_extent` の幅を
箱として使う」へ寄せられる (`_add_comment_decor` が既にその方針 / 要確認 Q4)。
**`text_rect()` / `anchor_scene_point()` は変えない** (変えるとドラッグで保存する
位置の基準がずれるため)。

### 4.4 `src/export/resolve_export.py`

```python
    alignment, margin_l, margin_r, margin_v = font_profile.placement_for_role(role)
    # テロップ個別の文字寄せ (ver9 resolve3 §R12)。
    # title の alignment 属性 = テキストの寄せなので、ここだけ差し替える。
    # position (近似位置) は従来のまま = テロップの位置は動かさない。
    text_align = (subtitle_generator.normalize_align(item.get("align"))
                  or _align_name(alignment))
    ...
        "align": text_align,
        "position": _title_position(alignment, (margin_l, margin_r, margin_v),
                                    width, height, title_cfg),
```

`fcpxml_builder` L576 が `title.get("align")` をそのまま `alignment` 属性へ書くため、
これだけで Resolve 側のタイトルの文字寄せに乗る。

### 4.5 `src/gui/timeline/timeline_controller.py`

インスペクタが既定の寄せを求めるために実効字幕設定が必要。
`preview_panel` (L176) と同じ組み立てを 1 か所へ出す:

```python
    # 実効字幕設定 (縦動画の上書き反映済み) を返す (ver9 resolve3 §R3)
    # プレビューと同じ値を使うことで「ボタンの表示 = 見え方」を一致させる。
    def effective_subtitle_cfg(self):
        from ...modules import subtitle_generator  # noqa: PLC0415 (重い import を遅らせる)

        if self._eff_subtitle_cfg is None:
            self._eff_subtitle_cfg = subtitle_generator.build_effective_subtitle_cfg(
                self._settings.get("subtitle", {}), self._settings.get("vertical", {}),
                {"is_portrait": self._timeline.orientation == "portrait",
                 "width": self._timeline.width, "height": self._timeline.height},
            )
        return self._eff_subtitle_cfg
```

`__init__` へ `self._eff_subtitle_cfg = None` を追加。
**`preview_panel` は今回書き換えない** (既存の動作を触らないため / Q5)。

### 4.6 `src/gui/timeline/timeline_editor_dialog.py`

`QtWidgets` の import へ `QButtonGroup` を追加。`_build_ui` の「サイズ」の直後へ:

```python
        # テロップ 1 本ごとの文字寄せ (ver9 resolve3 §R1)。
        # コンボではなく 3 つ並んだ排他ボタン (エクセルの配置ボタンと同じ操作感)。
        # 既定 (align 未指定) は設定の表示位置から求めたボタンを押した状態で見せる (§R3)。
        self.align_buttons = {}
        self._align_group = None
        align_cfg = self._controller.cfg["subtitle_align"]
        if align_cfg["enabled"]:
            width = int(self._controller.cfg["ui"]["subtitle_align_button_width_px"])
            align_row = QWidget()
            align_layout = QHBoxLayout(align_row)
            align_layout.setContentsMargins(0, 0, 0, 0)
            align_layout.setSpacing(0)                # 隙間 0 で 1 つの帯に見せる
            self._align_group = QButtonGroup(self)
            self._align_group.setExclusive(True)      # 常に 1 つだけ選択される (要望)
            for value, label in (("left", tr("左寄せ")), ("center", tr("中央寄せ")),
                                 ("right", tr("右寄せ"))):
                button = QPushButton(label)
                button.setCheckable(True)
                button.setFixedWidth(width)
                # clicked = 利用者が押したときだけ。show_clip の流し込みでは発火しない
                button.clicked.connect(
                    lambda _checked=False, v=value: self._on_align_clicked(v))
                self._align_group.addButton(button)
                align_layout.addWidget(button)
                self.align_buttons[value] = button
            align_layout.addStretch(1)
            form.addRow(tr("文字寄せ"), align_row)
```

`show_clip()` の字幕分岐へ `self._show_align(clip)` を足す:

```python
    # 文字寄せボタンの表示を現在の値に合わせる (ver9 resolve3 §R3 / §R5)
    def _show_align(self, clip):
        if self._align_group is None:
            return
        cfg = self._controller.cfg["subtitle_align"]
        eff_cfg = self._controller.effective_subtitle_cfg()
        allowed = (clip.role != "comment") or cfg["allow_comment_role"]
        # 未指定なら設定 (縦動画なら縦の値) の表示位置から求めた寄せを押しておく
        current = (subtitle_generator.normalize_align(clip.align)
                   or subtitle_generator.align_of_alignment(
                       self._role_alignment(eff_cfg, clip.role)))
        tooltip = self._align_tooltip(clip, allowed)
        for value, button in self.align_buttons.items():
            button.setChecked(value == current)
            button.setEnabled(allowed)
            button.setToolTip(tooltip)
```

`_role_alignment` は「コメントなら `comment_alignment`、他は `alignment`」を返す小さな
ヘルパ (`preview_items.default_subtitle_anchor` と同じ優先順)。

```python
    # 文字寄せボタン。選択中の字幕すべてへ 1 コマンドで適用する (§R7 / Undo 1 手)
    def _on_align_clicked(self, value):
        if self._updating or not isinstance(self._clip, SubtitleClip):
            return
        cfg = self._controller.cfg["subtitle_align"]
        # 既に効いている値をもう一度押したら「設定に従う」へ戻す (§R4)
        if (cfg["clear_on_reclick"]
                and subtitle_generator.normalize_align(self._clip.align) == value):
            value = ""
        ids = (self._selected_subtitle_ids() if cfg["apply_to_selection"]
               else [self._clip.id])
        targets, skipped = self._align_targets(ids, cfg)
        if not targets:
            self.status_message.emit(tr("文字寄せを変えられるテロップがありません"))
            self._show_align(self._clip)        # 押された見た目を元へ戻す
            return
        self._controller.edit_subtitles(targets, align=value)
        self.status_message.emit(self._align_message(value, len(targets), skipped))
```

`_align_targets` はコメント役割を振り分けて除外件数を返す。
`_update_multi_note` の文言を「文字色・縁の色・文字寄せは…」へ更新する (§4.8)。

### 4.7 `src/timeline/builder.py`

```python
# テロップ 1 本ごとの文字寄せ (ver9 resolve3 §5) の UI 設定を検証して既定で補う
def _subtitle_align_config(values):
    values = values if isinstance(values, dict) else {}
    return {
        "enabled": bool(values.get("enabled", True)),
        # 選択中の字幕すべてへ効かせるか (文字色と同じ規約 / §R7)
        "apply_to_selection": bool(values.get("apply_to_selection", True)),
        # 同じボタンを押し直したら「設定に従う」へ戻すか (§R4)
        "clear_on_reclick": bool(values.get("clear_on_reclick", True)),
        # コメント役割でも文字寄せを変えられるようにするか (§R5 / §7.1)
        "allow_comment_role": bool(values.get("allow_comment_role", False)),
    }
```

`timeline_config` の `"subtitle_tracks"` の隣へ `"subtitle_align": …`、`ui` へ:

```python
            # 文字寄せボタン 1 つの幅 (ver9 resolve3 §R1)
            "subtitle_align_button_width_px": _positive_int(
                ui.get("subtitle_align_button_width_px"), 56),
```

### 4.8 `src/i18n/catalog_en.py`

| 原文 | 英訳 |
| --- | --- |
| `文字寄せ` | `Text align` |
| `左寄せ` | `Left` |
| `中央寄せ` | `Center` |
| `右寄せ` | `Right` |
| `複数行になったテロップの行の揃えです\nテロップの表示位置 (左右・上下) は動きません。1 行のテロップでは見た目は変わりません` | `How the lines of a multi-line subtitle line up.\nThe subtitle itself does not move, and a single-line subtitle looks unchanged` |
| `コメントはアイコンと背景の配置が決まっているため、文字寄せを変えられません` | `Comments keep their alignment because the icon and background are placed from the left edge` |
| `文字寄せを「{align}」にしました` | `Text align set to "{align}"` |
| `{count} 件の文字寄せを「{align}」にしました` | `Text align of {count} subtitles set to "{align}"` |
| `文字寄せを設定に戻しました` | `Text align reset to the setting` |
| `{count} 件の文字寄せを設定に戻しました` | `Text align of {count} subtitles reset to the setting` |
| ` ({count} 件はコメントのため変えていません)` | ` ({count} skipped: comment role)` |
| `文字寄せを変えられるテロップがありません` | `No subtitle can change its text align` |

既存文言の差し替え (1 件):

```text
'※ 文字色・縁の色は選択中の {count} 件すべてに適用されます（字幕・役割・フォント・サイズはこの 1 件のみ）'
→ '※ 文字色・縁の色・文字寄せは選択中の {count} 件すべてに適用されます（字幕・役割・フォント・サイズはこの 1 件のみ）'
```

`tests/test_i18n.py` が原文の一致を検査するため、旧キーは必ず同時に差し替える。

---

## 5. setting.json 定義 (追加分)

```json
"subtitle": {
    "line_align": {
        "enabled": true,
        "require_measured_font": true
    }
},
"timeline": {
    "subtitle_align": {
        "enabled": true,
        "apply_to_selection": true,
        "clear_on_reclick": true,
        "allow_comment_role": false
    },
    "ui": {
        "subtitle_align_button_width_px": 56
    }
}
```

| キー | 既定 | 意味 |
| --- | --- | --- |
| `subtitle.line_align.enabled` | `true` | 焼き込み (ASS) 側で文字寄せを効かせるか (§R13) |
| `subtitle.line_align.require_measured_font` | `true` | フォントの送り幅を実測できないときに寄せを諦めるか。`false` にすると固定係数の推定で寄せるが、**テロップの位置がぶれる恐れがある** (§2-3 / §R8) |
| `timeline.subtitle_align.enabled` | `true` | インスペクタに文字寄せの行を出すか |
| `timeline.subtitle_align.apply_to_selection` | `true` | 選択中の字幕すべてへ効かせるか (§R7) |
| `timeline.subtitle_align.clear_on_reclick` | `true` | 同じボタンの押し直しで「設定に従う」へ戻すか (§R4) |
| `timeline.subtitle_align.allow_comment_role` | `false` | コメント役割でも変えられるようにするか (§R5) |
| `timeline.ui.subtitle_align_button_width_px` | `56` | ボタン 1 つの幅 (px) |

**寄せの既定値そのものは追加しない。** 既存の `subtitle.alignment` /
`vertical.alignment` / `subtitle.comment_alignment` をそのまま使う (§R3)。
設定画面の改修も不要。

`settings_window.DEFAULT_SETTINGS` の `subtitle` / `timeline` へ同じ構造を足す
(更新インストールでは旧 `setting.json` が復元されるため / §9.1 #14)。

---

## 6. 既存挙動への影響と互換性

| 観点 | 影響 |
| --- | --- |
| 旧プロジェクト (`align` キー無し) | `from_dict` で `""` = 設定追従。見た目は完全に不変 |
| 文字寄せを触っていないテロップ | `to_item()` にキーが載らず、**生成 ASS がバイト一致** |
| ASS の Style 行 | 変更なし (上書きはインラインタグ + イベント余白のみ) |
| テロップの表示位置・上下・余白 | **変更なし** (箱を固定する設計 / §1.6 実測で確認) |
| 1 行のテロップ | 変更なし (タグを出さない / §R8) |
| ドラッグで動かしたテロップ | 保存位置は不変。アンカーと `\pos` を等価変換するのみ |
| コメントのアイコン・背景 | 変更なし (既定で対象外 / `comment_decor` は無改修) |
| 縦動画プロジェクト | 既定表示が `vertical.alignment` 由来。箱も縦の余白で求める |
| プロキシ / 高画質プレビュー | `to_item()` 経由で同じ ASS を通るため自動的に一致 |
| SRT / ITT caption | 変更なし (行の寄せを表現できない既存制約) |
| 旧版アプリで新プロジェクトを開く | `align` キーは未知キーとして無視される (= 設定どおりに描かれる) |

---

## 7. 既知の制約 (今回は変更しない)

### 7.1 コメント役割は既定で対象外

コメントは `\an4` (左アンカー) 前提でアイコン・黒背景を組んでいる
(`comment_decor._text_anchor`)。箱の左端を動かさないため破綻はしないが、
中央／右寄せにしたときのアイコンとの見え方を別途詰める必要があるため、
既定では無効 (`allow_comment_role=false`)。解禁した場合も**箱 = 背景の箱**は
現状どおりで、箱の中の行だけが寄る。

### 7.2 余白に収まらない / フォントを読めないときは寄らない

テロップの位置が動く方が害が大きいため、安全側に倒す (§R8)。
`font_size` 130 + 全角 20 文字 (既定の上限) は Yu Gothic UI で 1634px / 余白内 1840px の
ため成立するが、幅の広いフォントでは収まらないことがある (§1.6 検証2)。

### 7.3 プレビューと焼き込みで箱の幅の求め方が違う

プレビュー = Qt 実測 / 焼き込み = `font_advance` 実測。左寄せ・右寄せでは
その差 (数 px) が行の寄せ位置の差として見える (§4.3 / Q4)。

### 7.4 DaVinci Resolve 連携

title の文字寄せ属性には乗るが、位置は既存どおり「配置 + 余白からの近似」。
caption (ITT) には行の寄せが無いため反映しない。

### 7.5 字幕編集画面 (一覧) には出さない

対象は Timeline 編集画面のインスペクタのみ (要望「Timeline 画面で」)。
`subtitle_editor_dialog` は item 辞書を直接扱う別経路のため、必要なら別途設計する。

---

## 8. エラー処理・ログ方針

| 事象 | 扱い |
| --- | --- |
| 未知の値 (`"LEFT"` / `None` / 数値) | `align` setter が `""` へ正規化。例外は投げない |
| フォントの送り幅が読めない | WARNING を 1 回 (`_warn_once`)。寄せは諦める |
| 余白に収まらない | INFO を 1 回。寄せは諦める |
| 設定の `alignment` が 1〜9 外 | `alignment_with_align` がそのまま返す (既存の `_to_int` 既定に従う) |
| コメント役割への適用 | コマンドを積まず、ステータスへ件数を出す (§R9) |
| 対象が 0 件 | 「文字寄せを変えられるテロップがありません」/ ボタン表示を元へ戻す |

ログ (`subtitle_generator` の既存 INFO 行へ追記):

```text
テロップ個別指定 フォント 0 件 / サイズ 0 件 / 文字色 2 件 / 縁の色 0 件 / 文字寄せ 3 件
```

0 件のときは従来どおり何も出さない。

---

## 9. テスト計画

### 9.1 新規 `tests/test_subtitle_align.py` (Qt 非依存)

| # | 内容 |
| --- | --- |
| 1 | `alignment_with_align(2,"left")==1` / `(2,"right")==3` / `(5,"right")==6` / `(8,"left")==7` |
| 2 | `alignment_with_align(2,"")` と `(2,"bogus")` は `2` のまま (段を崩さない) |
| 3 | `align_of_alignment(1〜9)` が left/center/right を正しく返す |
| 4 | `SubtitleClip(align="LEFT").align=="left"` / `"bogus"`→`""` / `setattr` 経路も同じ |
| 5 | `align` 未指定の `to_item()` に `"align"` キーが**無い**こと (後方互換の要) |
| 6 | `generate_ass_file`: `align` 未指定の出力が本改修前の期待文字列と一致すること |
| 7 | 2 行 + `align="left"` → Dialogue 行が `{\an1}` で始まり、**イベント余白が
      `margin_l + (余白内 - 幅)/2`** になること (幅はテスト用に差し替えた測定値) |
| 8 | 同条件の `align="right"` → `{\an3}` と右側のイベント余白 |
| 9 | `align="center"` かつ設定 `alignment=2` → タグもイベント余白も出ない |
| 10 | **1 行**のテロップ → タグもイベント余白も出ない (§R8) |
| 11 | 幅が余白に収まらない (`measured` な幅 > 余白内) → 出ない + ログ |
| 12 | `measured=False` (フォント読めず) → 出ない + WARNING 1 回 |
| 13 | 位置指定あり + `align="left"` → `\an4\pos(cx-幅/2, cy)` になり、`cy` が不変 |
| 14 | コメント役割 → 既定で出ない / `allow_comment_role` 相当の設定で出る |
| 15 | `to_dict`→`from_dict` 往復と、`align` キー無し辞書からの復元が `""` |
| 16 | `commands.EditSubtitles([...], align="right")` が全件へ入り Undo で戻る |
| 17 | `timeline_config({})["subtitle_align"]` の既定と不正値の吸収 |
| 18 | `settings_window._merge_with_defaults` が `subtitle.line_align` /
      `timeline.subtitle_align` 欠落を補うこと |

### 9.2 既存テストへの追加

- `tests/test_settings_migration.py`: 上記 #18。
- `tests/test_resolve_export.py`: `align="right"` の item が title の `align="right"`
  になり、**`position` は変わらない**こと。
- `tests/test_comment_placement.py` / `test_comment_bg_width.py`: 無改修で通ること
  (`text_extent` の呼び出しを増やすだけで仕様は変えない)。
- `tests/test_i18n.py`: 新規文言を入れて通ること。

### 9.3 実機での一致確認 (§1.6 と同じ手順を回帰に使う)

`font_advance` + 同梱 ffmpeg で 2 行テロップを描画し、

1. 左寄せ: **最長行の左端が中央寄せ時と一致** (誤差 ≤ 2px) かつ短い行の左端が一致
2. 右寄せ: 最長行の右端が一致かつ短い行の右端が一致
3. 上下: 行の上端・下端が中央寄せ時と完全一致
4. 位置指定あり: 1〜3 が `\pos` でも成り立つ

(§1.6 の検証スクリプトをテストへ移す案は Q6。ffmpeg 実行を伴うため既定の
 `python -m unittest discover` からは外し、手動またはマーク付きで回す想定)

### 9.4 手動確認手順

1. 2 行になるテロップを選ぶ → 「文字寄せ」に設定どおり (既定は中央寄せ) が
   押された状態で出る。
2. 「左寄せ」を押す → プレビューで**テロップの位置は動かず**行頭がそろう。
   ステータスに「文字寄せを「左寄せ」にしました」。
3. 書き出し (焼き込み) して 2 と同じ見え方になること (プレビュー一致 / §7.3)。
4. Ctrl+Z で 1 手戻る。
5. 「左寄せ」をもう一度押す → 「文字寄せを設定に戻しました」。
6. 1 行だけのテロップで押す → 見た目は変わらない (保存はされる)。
7. テロップを 3 本選んで「右寄せ」→ まとめて動き、Undo 1 手で戻る。
8. 役割を「コメント」にする → ボタンが押せなくなり、ツールチップで理由が出る。
9. プレビュー上でドラッグしたテロップでも寄せられ、**ドラッグ位置が動かない**こと。
10. 縦動画プロジェクトで既定表示が `vertical.alignment` 由来になること。
11. 保存 → 開き直して残ること。コピー＆ペーストでも引き継ぐこと。
12. `subtitle.line_align.enabled=false` で焼き込みが現状どおりに戻ること。

---

## 10. 要確認事項 (推測実装せず確認したい点)

| # | 内容 | 本設計の既定 |
| --- | --- | --- |
| Q1 | 1 行だけのテロップでは見た目が変わらない仕様で良いか (文字寄せの定義上そうなる)。ボタンは押せて保存もされるが、折り返して 2 行になるまで効かない | 変わらない (§R2) |
| Q2 | 同じボタンの押し直しで「設定に従う」へ戻す操作は分かりにくくないか。「設定に戻す」を 4 つ目の小ボタンにする案もある (要望「3 つ」から外れる) | 押し直しで戻す |
| Q3 | 右クリックメニューにも文字寄せを出すか | 出さない (§R14) |
| Q4 | プレビューの箱の幅を Qt 実測のままにするか、寄せ指定時だけ焼き込みと同じ `font_advance` の幅へ合わせるか (§7.3) | Qt 実測のまま。目に付けば切り替え |
| Q5 | `preview_panel` の実効字幕設定の組み立てを `controller.effective_subtitle_cfg()` へ寄せる (重複を消す) か | 触らない (既存動作を変えない) |
| Q6 | §1.6 の ffmpeg 実描画チェックを自動テストへ入れるか (実行に数秒 + ffmpeg 必須) | 入れない (手動 / §9.3) |
| Q7 | コメント役割も寄せたいか (§7.1) | 対象外 (`allow_comment_role=false`) |

### 10.1 設計上こちらで確定した判断

- **文字寄せ = 行の揃え。テロップの位置は動かさない** (要望「(追加)」/ §0.1)。
- **箱の幅は `font_advance` の実測だけを使う。** 読めないときは寄せを諦める
  (固定係数で箱をずらすと位置がぶれるため / §2-3)。
- **位置ドラッグ済みのテロップも対象**にする (`\an4`/`\an6` + `\pos` 補正。
  上下・位置が動かないことを実機で確認済み / §1.6 検証3)。
- **`Justify` スタイル拡張は使わない** (同梱 libass が実装していない / §1.6 検証0)。
- **新コマンド・新しい測定機構・設定画面の新 UI は作らない。**
- **未指定なら ASS をバイト一致で保つ** (`to_item()` にキーを足さない)。

---

## 11. 実装手順 (レビュー後)

1. `model.py` — `SUBTITLE_ALIGNS` / `_to_align` / `align` プロパティ / 4 メソッド。
2. `subtitle_generator.py` — `normalize_align` / `align_of_alignment` /
   `alignment_with_align` / `line_align_override` / `_inline_position_override` の
   アンカー補正 / Dialogue 行のイベント余白 / ログ。
3. `tests/test_subtitle_align.py` の #1〜#16 を先に書いて緑にする
   (UI を触る前に出力の後方互換と箱の計算を固める)。
4. §9.3 の実機チェックを 1 回手で回し、§1.6 の数値を再現する。
5. `builder.py` + `settings_window.DEFAULT_SETTINGS` + `setting.json` — 設定 6 項目と幅。
6. `timeline_controller.py` — `effective_subtitle_cfg()`。
7. `timeline_editor_dialog.py` — 3 ボタン・`_show_align`・`_on_align_clicked`・
   `_align_targets`・`multi_note` 文言。
8. `preview_items.py` — `_line_alignment`。
9. `resolve_export.py` — `_title_from_item` (`align` 属性のみ)。
10. `catalog_en.py` — 新規文言と差し替え 1 件。`python -m unittest discover -s tests`。
11. §9.4 の手動確認 (特に 2・3・9 = 位置が動かないこととプレビュー一致)。

---

## 12. 実装記録 (2026-10-26 / 設計からの差分)

実装済み。設計どおりだが、実装中に次の 3 点を設計より具体化した。

1. **`line_align_override` → `resolve_line_align`** (`subtitle_generator`)。
   戻り値を dict (`align` / `width` / `an` / `margin_l` / `margin_r` / `skip`) にし、
   「効かせなかった理由」を呼び出し側へ返す形にした。警告は `_warn_once` の
   モジュール状態ではなく **ASS 1 本につき 1 回** `build_subtitle_file` の末尾で
   件数つきで出す (測れなかった件数が分かるほうが調べやすい)。
2. **位置ドラッグ済みのテロップにはイベント余白を出さない。**
   `\pos` があると libass は余白を見ないため不要で、
   「余白に収まるか」の判定も位置指定時は行わない (画面外へドラッグしたテロップでも
   寄せが効く)。出力は `{\an4\pos(…)}` のみで余白は `0,0,0` のまま。
3. **ボタンを押した直後にインスペクタの表示を引き直す** (`_show_align`)。
   排他ボタンは押し直しただけでは `checked` が外れないため、
   「設定に従う」へ戻したときに押され位置が更新されない。

### 12.1 実機の結果 (§9.3 / 同梱 ffmpeg + Yu Gothic UI / font_size 100 / 2 行)

| | 最長行のずれ | 行のそろい | 上下のずれ |
| --- | --- | --- | --- |
| 左寄せ (位置指定なし) | 0px | 0px | 0px |
| 右寄せ (位置指定なし) | 0px | 1px | 0px |
| 左寄せ (ドラッグ済み) | 0px | 0px | 0px |
| 右寄せ (ドラッグ済み) | 0px | 1px | 0px |

生成される Dialogue 行:

```text
寄せなし      : Dialogue: 0,…,Streamer,,0,0,0,,寄せなし\Nこちらは長い行です
左寄せ        : Dialogue: 0,…,Streamer,,682,682,0,,{\an1}左寄せ\Nこちらは長い行です
右寄せ        : Dialogue: 0,…,Streamer,,682,682,0,,{\an3}右寄せ\Nこちらは長い行です
ドラッグ済み  : Dialogue: 0,…,Streamer,,0,0,0,,{\an4\pos(681.5,540.0)}…
1 行だけ      : Dialogue: 0,…,Streamer,,0,0,0,,1行だけ
```

### 12.2 テスト

`tests/test_subtitle_align.py` (新規 33 件) と既存全体 (計 1749 件) が通る。
内訳は §9.1 の #1〜#18 に加えて、インスペクタの実操作 (3 ボタンの排他・押し直しで
設定へ戻る・複数選択への適用・コメントで無効・機能 OFF) と
Resolve 連携 (`align` だけ変わり `position` は不変) を含む。
