# resolve11（ver3） — コメント字幕の専用トラック化 / アイコン表示 / 「コメント：」撤去 / 既定配置 設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request11.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「実装前に設計を行うこと」）。
* 記載した実装位置・挙動はすべて本リポジトリの実コードを読んで確認した（2026-08-24 時点）。
  決めきれない点は推測実装せず §9 へ列挙し、**Q1〜Q12 のすべてについて 2026-08-24 に回答をもらって確定した**（§0.1 / §0.2 / §9）。
  **未確定事項は残っていない。**
* **setting.json は本書では書き換えない**。追加キーは `DEFAULT_SETTINGS`
  （`settings_window.py:144` の `subtitle` / `vertical`）と `builder.timeline_config()` の既定へ足し、
  `load_settings()` の既定マージで自動補完させる（resolve7 / resolve9 / resolve10 と同じ方針）。
* ハードコード禁止（`docs/claude.md`）に従い、アイコンの大きさ・間隔・配置・トラック名は
  すべて `setting.json` から変えられる形にする（§7）。

### 0.1 確認事項の回答（2026-08-24 / 本書はこの回答で改訂済み）

初版 §9 で挙げた 9 点はすべて回答をもらった（**Q1〜Q9 確定**）。
回答の全文と、それによる本書の変更点は **§9** にまとめてある。設計が動いたのは次の 2 点だけ:

| # | 回答 | 本書の変更 |
|---|---|---|
| **A-1** | **Q7: 縦動画のアイコンは 50×50 にする** | `vertical.comment_icon_size_px` の既定を `100` → **`50`**、それに伴い `vertical.comment_margin_l` を `190` → **`140`**（`40 + 50 + 50`）へ改めた（§6.1.1 / §7.2） |
| **A-2** | **Q4: 「コメント：」撤去だけでなく、今回の対応範囲そのものが全経路** | アイコン表示・役割別配置も **通常パイプライン / 切り抜き / 各プレビュー**まで含めて*必須スコープ*と明記した。§11 段階 7 は「余力があれば」ではなく**必須**（§4 / §11） |

残る 7 点（Q1・Q2・Q3・Q5・Q6・Q8・Q9）は**初版の採用案がそのまま承認**されたため、
本文の設計に変更はない。Q9（同時に複数のコメントは出さない）により、
**コメント用トラックは S2 の 1 本で足りる**ことが確定した（S3 以降は作らない / §9-Q9）。

### 0.2 追加要望（2026-08-24 / rev2）

> アイコンとコメントを含んで背景を付けてください。
> 角丸の四角で背景色は黒の透明度 50% でお願いします。

**要件 C13** として本書へ取り込んだ（§1）。設計上の追加・変更は次のとおり:

| # | 追記した箇所 | 内容 |
|---|---|---|
| **B-1** | §2.8（新規） | 現状分析: ASS の箱（`BorderStyle=3`）は**角丸にできず、アイコンも囲えない**ことの確認 |
| **B-2** | §3 **D-9**（新規） | 背景は **ASS のベクター描画（`\p1`）で別 Dialogue 行として描く**。画像を作らない・入力を増やさない |
| **B-3** | §3 **D-10**（新規） | 背景の大きさは**文字送り幅の推定**で求める。**推定値をプレビューと焼き込みの唯一の計算元**にして、両者を必ず一致させる |
| **B-4** | §5.11（新規） | 背景ボックスの幾何・ASS 描画文字列・Qt 側の描画・重ね順 |
| **B-5** | §5.5 / §4 / §11 | 新規モジュール名を `comment_icon.py` → **`comment_decor.py`**（背景とアイコンの両方を扱うため）へ改めた |
| **B-6** | §6.1 / §6.1.1 | 図を背景ボックス入りに描き直し、余白と角丸の既定値を明示 |
| **B-7** | §7.4（新規） | 背景の設定キー（色・角丸半径・内側余白・文字幅推定の係数） |
| **B-8** | §9-Q5 / §9.0 | Resolve 書き出しには**背景も含めない**ことを明記。新たな確認事項 Q10〜Q12 を追加し、**同日「一旦、採用案で OK」の回答を得て確定**（角丸 24px / 余白 24px / 推定方式 / はみ出しはクランプ＋警告）|

---

## 1. 要望と要件 ID

| ID | 要望（原文） | 種別 |
|---|---|---|
| **C1** | Timeline 編集画面で字幕を「コメント」に設定した場合、**コメントをもう一つの字幕トラックとして扱うことは可能か？** | 質問＋機能追加 |
| **C2** | `src/comment_icon.png` を **100×100 くらい**で表示する。表示個所は**コメント字幕の左横**。コメント字幕から **50px 程左に空けて**配置する。**画像がはみ出さないように**注意 | 機能追加 |
| **C3** | 「コメント」に設定すると字幕の頭に付く **「コメント：」を削除する** | 挙動変更 |
| **C4** | 「コメント」にした場合、**デフォルトの配置設定を中央の左に寄せる** | 挙動変更 |
| **C13** | **アイコンとコメントを含んで背景を付ける**。**角丸の四角**で、背景色は**黒の透明度 50%**（rev2 / §0.2） | 機能追加 |

**C1 への回答（結論）**： **可能。しかも「やるべき」**。
モデル・描画・保存はすでに **複数字幕トラックを前提に書かれており**（§2.2）、
S1 決め打ちなのは *生成側と一部の UI だけ* である。さらに現行実装では
**同一トラック内のクリップは重なれない**（§2.3）ため、
「通常字幕とコメントを同時に出す」には **別トラックが構造的に必須**である。
C2〜C4（コメントだけ左中央に置き、アイコンを添える）は
「通常字幕と同時に画面へ出る」ことが前提の要望なので、C1 は C2〜C4 の土台になる。

### 1.1 要望文に無いが実装に必ず要る論点

| ID | 論点 | 理由 |
|---|---|---|
| **C5** | **役割を変えたときにトラックをどう移すか** | 役割は「クリップの属性」、トラックは「置き場所」。連動させないと S1 にコメントが残る |
| **C6** | **移動先が埋まっていたら** | 字幕トラックは重なりを許さない（`project_io.py:439`）。移動が失敗し得る |
| **C7** | **アイコンをどうやって焼き込むか** | 字幕は `ass` フィルタ、画像は `overlay` フィルタで、**経路がまったく別**（§2.6） |
| **C8** | **ドラッグで位置を変えたコメント字幕のアイコン位置** | `\pos` は現在 **文字列の中央**を基準にしており、左端が求まらない（§3-4） |
| **C9** | **「コメント：」撤去の適用範囲** | ラベルは Timeline だけでなく**通常パイプライン・切り抜き・Resolve 書き出し**にも出る（§2.4） |
| **C10** | **既存 setting.json の `comment_label`** | 既定値を変えても `_merge_with_defaults` はユーザー値を温存するため**消えない**（§8） |
| **C11** | **縦動画（1080×1920）でのアイコンと余白** | 配置・余白は縦タブが上書きする（`subtitle_generator.py:481`） |
| **C12** | **凍結配布（PyInstaller）での画像の解決** | `src/comment_icon.png` は同梱しないと exe から見えない（§5.10） |
| **C14** | **背景の大きさをどう決めるか** | 文字の実寸を決めるのは libass で、**Python 側からは測れない**。アイコンだけなら左端が分かれば足りたが、背景は**文字の右端・上下端まで**要る（§3 D-10） |
| **C15** | **背景・文字・アイコンの重ね順** | 背景が文字やアイコンを隠したら意味がない。ASS の Layer と ffmpeg の合成順の**両方**を決める必要がある（§5.11-4） |

---

## 2. 現状分析（実コードの確認結果）

### 2.1 「コメント」は *役割(role)* であって *トラック* ではない

```python
# src/gui/timeline/timeline_editor_dialog.py:51
_ROLE_CHOICES = [("配信者", "streamer"), ("サブ", "sub"), ("コメント", "comment")]
```

インスペクタの「役割」コンボが `SubtitleClip.role`（`model.py:281`）を書き換えるだけで、
クリップは **S1 に置かれたまま**。役割が効くのは
**色（`role_colors`）・縁の色（`role_outline_colors`）・ASS の Style 名**の 3 点のみ
（`subtitle_generator.py:83` `ROLE_STYLE = {"streamer": "Streamer", "sub": "Sub", "comment": "Comment"}`）。

### 2.2 複数字幕トラックは「モデル・描画・保存」がすでに対応済み

| 箇所 | 実装 | 複数 S に対応しているか |
|---|---|---|
| `Timeline.subtitle_tracks()` | `model.py:608` | ✅ 全字幕トラックを返す |
| `Timeline.subtitle_clips()` | `model.py:693` | ✅ 全トラックを走査して時系列順に返す |
| `Timeline.overlay_elements()` | `model.py:670` | ✅ 全字幕トラックを z_order 順に混ぜる |
| Timeline 画面の行組み立て | `timeline_view.py:137 build_rows()` | ✅ `sorted(subtitle_tracks(), key=index)` を上から並べる |
| トラックヘッダの描画 | `timeline_view.py:263` | ✅ `row["track"].id` を出すだけ |
| 保存・読み込み | `project_io.py:147` / `:260` | ✅ `tracks` を汎用に往復する（スキーマ変更不要） |
| レンダリング | `renderer.py:386 _build_layers()` | ✅ 連続する字幕要素を 1 つの ASS グループへまとめる |

つまり **S2 を足すだけで「並んで表示され、保存され、焼き込まれる」**。
一方、**S1 決め打ち**なのは次の 5 箇所だけ：

| # | 箇所 | 現状 |
|---|---|---|
| A | `builder.build()` | `builder.py:480` で `Subtitle 1` を 1 本だけ作る |
| B | `AddSubtitleClip._resolve_track()` | `commands.py:852` `base_subtitle_track()` へ落とす |
| C | `PasteClips._resolve_track()` | `commands.py:1070` コピー元 ID → 無ければ `base_subtitle_track()` |
| D | アーカイブのクリップ分割 | `archive/timeline_builder.py:283` で **字幕トラックを 1 本に潰す** |
| E | インスペクタの役割変更 | `timeline_editor_dialog.py:1087` 役割だけ書き換える（移動しない） |

### 2.3 同一トラック内は重なれない ＝ 別トラックが要る決定的な理由

| 箇所 | 実装 | 効果 |
|---|---|---|
| 追加 | `commands.py:867 _available_duration()` | 既存字幕の内側には**追加できない** / 次の字幕の手前で尺を切る |
| 移動 | `commands.py:181 _clamp_position()` | 同一トラックの他クリップと**重ならない位置へ丸める** |
| 読み込み | `project_io.py:439 _validate_overlaps()` | 重なっていたら**後ろへ押し出して補正する**（警告付き） |

トラックが違えば上記はすべて素通りする（`_validate_overlaps` はトラック単位）。
**「通常字幕を出しながら、同時にコメントを左中央へ出す」は S1 のままでは実現できない。**

### 2.4 「コメント：」ラベルは 3 箇所で付いている

| # | 箇所 | コード | 効く画面／出力 |
|---|---|---|---|
| L1 | ASS 生成 | `subtitle_generator.py:658` `text = f"{comment_label}\\N{text}"` | **本番の焼き込み全部**（通常パイプライン・Timeline・切り抜き・高精度プレビュー・字幕編集画面のプレビュー） |
| L2 | Timeline プレビュー | `preview_items.py:280` `text = f"{comment_label}\n{text}"` | Timeline 編集画面の Qt 近似描画 |
| L3 | Resolve/FCPXML 書き出し | `resolve_export.py:165` `text = f"{comment_label}{text}"` | 書き出したタイトル・SRT の文字列 |

供給元は 1 つ（`FontProfile.comment_label` / `subtitle_generator.py:98`）で、
値は `subtitle.comment_label`（既定 `"コメント："`）。
要望文は「Timeline 画面で」と書かれているが、**同じ 1 つの設定値が全経路を貫いている**。

### 2.5 配置は「全役割で共通の 1 つ」しか持てない

```python
# src/modules/subtitle_generator.py:151 to_ass_style_named()
f"{self.border_style},{self.outline_width},{_ASS_SHADOW},"
f"{self.alignment},{self.margin_l},{self.margin_r},{self.margin_v},"
```

`iter_role_styles()`（`:165`）は Streamer/Sub/Comment の 3 Style を出すが、
**差し替えているのは塗り色とアウトライン色だけ**で、
`alignment` / `margin_*` は 3 つとも同じ値になる。
プレビュー側（`preview_items.py:56 default_subtitle_anchor()`）も
`eff_cfg["alignment"]` を役割に関係なく読む。
→ **C4 は「役割別の配置を持てるようにする」ところから要る。**

### 2.6 画像の焼き込み経路と字幕の焼き込み経路はまったく別

| 経路 | 実装 | 使うフィルタ |
|---|---|---|
| 字幕のみ | `renderer.py:441 _burn_subtitles_only()` → `subtitle_generator.py:711 burn_subtitle()` | `-vf fps,…,subtitles='…'`（**入力 1 本**） |
| 字幕＋画像 | `renderer.py:469 _composite()` | `-filter_complex` に `-loop 1 -i 画像` を足して `overlay` |

**ASS には画像を置けない**（libass は埋め込み画像を描かない）ため、
アイコンは必ず `overlay` 側で描く。
ただし `_composite()` は **オーバーレイ 1 クリップにつき入力 1 本**を足す作りなので、
コメント 1 件ごとに入力を足すと数百入力になり破綻する。→ §3 D-6 で別方式を採る。

### 2.7 同梱アセットの解決方式は前例がある

```python
# src/gui/main_window.py:992
def _resolve_app_icon_path():
    if getattr(sys, "frozen", False):
        base = os.path.join(getattr(sys, "_MEIPASS", ""), "src", "gui")
    else:
        base = os.path.dirname(os.path.abspath(__file__))
```

`main_window.spec:23` の `('gui/app.ico', 'src/gui')` と対になっている。
`comment_icon.png` も**同じ方式**で解決・同梱する（§5.10）。
なお対象ファイルは実測 **300×300 / RGBA（アルファ有り）/ 32.9KB** で、縮小して使うのに問題ない。

### 2.8 「文字の後ろの箱」は ASS にもあるが、今回の要望には使えない（C13 / C14）

ASS の `BorderStyle` には文字の背後に箱を敷く機能があり、Style 行にも
`back_color`（既定 `&H64000000`）という**それ用の色がすでにある**
（`settings_window.py:174` / `subtitle_generator.py:151`）。しかし今回の要望には使えない:

| 要望 | `BorderStyle=3`（不透明ボックス）でできるか |
|---|---|
| **角丸**の四角 | ❌ できない。libass の箱は**直角の矩形だけ** |
| **アイコンも含んで**囲う | ❌ できない。箱は**文字の外接矩形**にしか付かず、ffmpeg で重ねるアイコンは範囲外 |
| 黒・透明度 50% | ⭕ できる（`back_color` のアルファ） |
| 行ごとではなく**ひとかたまり**で囲う | △ 実装依存（行ごとに箱が付く見え方になりやすい） |

さらに `border_style` は **Style 行の共通項目**なので、これを 3 にすると
**配信者・サブの字幕にも箱が付いてしまう**（`to_ass_style_named()` は 3 役割で同じ値を出す / §2.5）。

**結論**: 背景は libass の箱機能ではなく、**自前で描く**必要がある（§3 D-9）。

### 2.9 文字の実寸は Python から測れない（C14）

* libass はフォントを自前で読み、字送り・カーニングを決めて描く。**その結果は ffmpeg プロセスの中にしか無い**。
* Qt（`QFontMetrics`）で測る手はあるが、
  ① `docs/claude.md`「GUI と編集処理の分離」に反する
  ② CLI 経路・書き出し経路には Qt が無い
  ③ **Qt と libass で結果が一致しない**（`preview_items.py:6` に「Qt による近似であり libass と完全一致しない」と明記済み）
  ため採れない。
* 一方で**行数だけは厳密に分かる**。ASS ヘッダが `WrapStyle: 2`
  （`subtitle_generator.py:629`）＝**自動折り返しをしない**設定のため、
  行は `\N` で切った数がそのまま出る。
* 折り返し自体は `wrap_lines()`（`:285`）が**文字数**（`min_line_length` 15 / `max_line_length` 20）で行う。
  → **「1 行あたり最大何文字か」は設定値として既知**であり、文字送り幅を推定する足場になる（§3 D-10）。

---

## 3. 設計方針（決定事項）

### D-1. コメント専用の字幕トラック **S2「Comment」** を、役割に連動して自動運用する

* S1 = 通常字幕（配信者／サブ）、**S2 = コメント**。
* S2 は **必要になったとき初めて作る**（既存プロジェクトに空行を増やさない）。
* 役割 `comment` ⇄ それ以外の切り替えで **クリップを S1/S2 間で移す**（C5）。
* 移せなかった場合（移動先が埋まっている）は **役割変更だけ通す**（C6 / D-2）。
* `timeline.subtitle_tracks.role_track_enabled = false` で **従来どおり 1 本運用へ戻せる**。

**「トラックが役割を決める」のではなく「役割がトラックを決める」**。
出力（色・Style・配置・アイコン）は**あくまで `clip.role` が決める**。
トラックは編集の都合（重なりを許す置き場所）でしかない。
この向きにしておくと、トラック移動が失敗しても**出力は絶対に壊れない**。

### D-2. 役割変更は「役割の書き換え＋トラック移動」を **1 コマンド**にする

`CommandStack` はスナップショット方式（`commands.py:87`）なので、
複合操作でも **Undo 1 手**で戻る（`EditSubtitles` と同じ理屈）。

### D-3. 役割別の配置を持てるようにする（C4）

* `FontProfile` に **役割別の alignment / margin** を持たせ、`iter_role_styles()` で Style ごとに出し分ける。
* コメントの既定は **`comment_alignment = 4`（ASS an4 ＝ 垂直中央・左寄せ）** ＝「中央の左」。
* コメントの左余白の既定は **`comment_margin_l = 190`**。これは偶然の数字ではなく
  **`margin_l(40) + アイコン幅(100) + 間隔(50)`** の合計で、
  こうすると**アイコンの左端がちょうど通常の左余白 40px に揃う**（§6.1 の図）。

### D-4. コメント字幕は **左端アンカー**で固定する（C8 の解決）

現在、ドラッグで位置を付けた字幕は `\an5\pos(x,y)` ＝ **文字列の中央**を指定している
（`subtitle_generator.py:547`）。中央基準だと **左端 = 中央 − 文字幅/2** となり、
**文字幅を知らないとアイコンを置けない**。文字幅は libass が決めるので Python 側では求まらない
（Qt で測る案は `docs/claude.md`「GUI と編集処理の分離」に反するうえ、CLI 経路で Qt が無い）。

**決定**：**役割が `comment` のときだけ `\an4\pos(x,y)`（左中央アンカー）にする。**
これで `pos_x` が **そのまま文字列の左端**になり、アイコン位置が一意に決まる。
プレビュー側も同じアンカー規約で描き・同じ規約で位置を保存する（§5.7）。

* 副作用: **既に手でドラッグしてあるコメント字幕**は、開き直すと
  「文字幅の半分」ぶん右へ動いて見える（アンカーの意味が変わるため）。
  コメント役割は今回から使い方が変わるため影響は小さく、**許容で確定した**（§9-Q3）。
* 併せて、**コメントの配置は左寄せ 3 種（an1/an4/an7）に限定**する。
  右寄せ・中央寄せは文字幅なしに左端が求まらず、アイコンが必ずズレるため、
  不正値は警告して `4` へ落とす（§7 の検証）。

### D-5. 装飾（背景・アイコン）の幾何は **1 箇所でだけ**計算する

`src/modules/comment_decor.py`（新規・Qt 非依存の純 Python）に置き、
**プレビュー（Qt）・焼き込み（ffmpeg）・高精度プレビュー**が同じ関数を呼ぶ。
2 箇所で計算すると「プレビューと出力がズレる」という一番タチの悪いバグになる。

```
アイコン右端  = 文字列の左端 − gap
アイコン左端  = 文字列の左端 − gap − size
アイコン中心Y = 文字列の垂直中心
```

| 状態 | 文字列の左端 X | 文字列の垂直中心 Y |
|---|---|---|
| 位置指定なし・an4（既定） | `comment_margin_l` | `H / 2`（**厳密**。an4/5/6 は MarginV を見ない） |
| 位置指定なし・an7 | `comment_margin_l` | `comment_margin_v + 行高/2`（**近似**） |
| 位置指定なし・an1 | `comment_margin_l` | `H − comment_margin_v − 行高/2`（**近似**） |
| 位置指定あり（ドラッグ済み） | `(pos_x + 1) × W / 2` | `(1 − pos_y) × H / 2`（**厳密**、D-4 による） |

行高 ≒ `フォントサイズ × 行数`（行数 = `\N` の数 + 1）。既定の an4 では**近似を一切使わない**。

**はみ出し防止（C2 の「はみ出さないように」）は二重にかける**:

1. **既定値で構造的に防ぐ** — `comment_margin_l = 190` なのでアイコン左端は 40px、はみ出しようがない。
2. **描画直前に必ずクランプする** — `x = max(x, 0)`、`y = clamp(y, 0, H − size)`。
   クランプが起きたら **1 回だけ WARNING を出す**（黙って詰めると原因が追えない）。

### D-6. 焼き込みは **入力を増やさず** `movie` ソースフィルタ＋位置グループで行う（C7）

コメント 1 件ごとに `-i` を足すのは破綻する（§2.6）。代わりに:

* アイコンを **`movie` フィルタ**で filtergraph の内側から 1 回だけ読む（入力本数は増えない）。
* **表示位置が同じコメントはひとまとめ**にし、`enable` を論理和で連結する。
  既定運用（誰も位置をドラッグしていない）なら **位置グループは 1 つ ＝ overlay も 1 本**で済む。
* 位置が違うぶんだけ `split` で複製して overlay を鎖状に足す。

```
# 例: 位置グループが 1 つ（既定運用）のとき
fps=60,subtitles='C\:/work/timeline_subtitle.ass'[vsub];
movie='C\:/app/src/comment_icon.png',format=rgba,
  scale=w=100:h=100:force_original_aspect_ratio=decrease,
  pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000[cic0];
[vsub][cic0]overlay=40:490:enable='between(t,3.10,5.40)+between(t,9.00,11.20)'
```

* `scale` + `pad` で **素材の縦横比に関係なく必ず size×size の箱**になるので、幾何が確定する。
* `overlay` は既定 `eof_action=repeat` のため、**1 フレームの画像でも最後まで表示され続ける**。
* `-vf` は「入力 1・出力 1」のフィルタグラフなら**ラベル付きの複数チェーンを書ける**ので、
  `burn_subtitle()` の**コマンド構造は変えずに済む**（`-filter_complex` へ作り替えない）。
* **長さ対策**: コメントが多いと `enable` 式が数十 KB になり得る。
  文字数が `comment_icon_filter_script_chars`（既定 8000）を超えたら
  **`-filter_script:v <ファイル>` へ逃がす**。コマンドライン長の上限に当たらない。
* アイコンが 0 件のときは **フィルタ文字列を一切足さない** ＝ **現行と完全に同一のコマンド**になる（後方互換）。

### D-7. プレビューのアイコンは **字幕アイテムの子**にする

`SubtitleOverlayItem`（`preview_items.py:274`）の**子 `QGraphicsPixmapItem`** として足す。
親を動かせば子も付いてくるので、**ドラッグ中もアイコンが字幕から離れない**。
ただし `_OverlayMixin.mouseReleaseEvent`（`preview_items.py:111`）は
`sceneBoundingRect().center()` を位置として通知しており、
**子を足すと境界矩形にアイコンが含まれて位置がズレる**。
→ `anchor_scene_point()` を切り出し、
**コメントは「文字だけの矩形の左中央」、それ以外は従来どおり中央**を返すようにする（D-4 と一致）。

### D-8. 「コメント：」は **全経路から外す**。ただし設定で戻せる（C3 / C9）

* L1〜L3（§2.4）の付与処理は**残したまま**、`comment_label` の**既定を `""` にする**
  （空文字なら現行コードは既に付与しない ＝ `if … and font_profile.comment_label`）。
* 既存 setting.json に残る `"コメント："` は
  `_normalize_legacy_values()`（`settings_window.py:750`）に**移行処理を足して空へ寄せる**（§8）。
* 「削除」を *コードの削除* ではなく *既定値の変更＋移行* で行う理由:
  1. `docs/claude.md`「既存実装を破壊しない」「ハードコード禁止」。
  2. ラベルを再び出したくなったら setting.json だけで戻せる。
  3. `tests/test_subtitle_color.py:99`（`test_comment_label_after_override`）が**そのまま通る**。

### D-9. 背景は **ASS のベクター描画**で描く（C13 / C15）

角丸の箱を出す方法は 3 つ考えられるが、**ASS の描画（`\p1`）が唯一まっとうに収まる**:

| 案 | 方法 | 判定 |
|---|---|---|
| A | `BorderStyle=3`（libass の箱） | ❌ 角丸にできず、アイコンを囲えない（§2.8） |
| B | 角丸 PNG を都度生成して `overlay` | ❌ 画像生成に外部ライブラリ（Pillow）が要る。手書き PNG エンコーダを足すのも本筋でない（`docs/claude.md`「不要なライブラリを追加しない」）。**大きさごとに別画像**が要るのも重い |
| **C** | **ASS の描画コマンド（`\p1` + ベジェ）で角丸矩形を 1 本の Dialogue として出す** | ⭕ **採用**。画像を作らない・入力を増やさない・libass が PlayRes 基準で綺麗に描く・色と透明度は `\1c` / `\1a` でそのまま指定できる |

```
Dialogue: 0,0:00:03.10,0:00:05.40,Comment,,0,0,0,,{\an7\pos(40,440)\1c&H000000&\1a&H80&\bord0\shad0\p1}m 24 0 l 1136 0 b 1160 0 1160 0 1160 24 l 1160 176 b 1160 200 1160 200 1136 200 l 24 200 b 0 200 0 200 0 176 l 0 24 b 0 0 0 0 24 0{\p0}
```

* `\1a&H80&` ＝ **透明度 50%**（ASS のアルファは反転表記。`&H80` = 128/255 ≒ 50%）。
* `\1c&H000000&` ＝ **黒**。`\bord0\shad0` で Style の縁・影が箱に付かないようにする。
* 角丸はベジェ `b`（制御点を角に置く一般的な書き方）で描く。半径は設定値（既定 24px）。
* **画像でもフィルタでもないので、`extra_chains` も入力本数も一切増えない。**
  アイコン（D-6）とは独立に効くので、**アイコンを切っても背景だけ出せる**。

### D-10. 背景の大きさは **文字送り幅の推定**で求め、その推定値を**唯一の計算元**にする（C14）

文字の実寸は測れない（§2.9）ので推定する。日本語ゴシックは**全角＝1em の等幅**に極めて近いため、
次の単純な規則で十分な精度が出る:

```
1 文字の送り幅 = font_size × (全角なら comment_bg_char_width_full(=0.78)
                              半角なら comment_bg_char_width_half(=0.45))
行の幅   = Σ 文字の送り幅            （\N で切った行ごとに合計）
文字幅   = max(行の幅)               （最も長い行）
行の高さ = font_size × comment_bg_line_height_ratio(=1.2)
文字高   = 行数 × 行の高さ           （行数は \N の数 + 1 で厳密）
```

**係数の既定値は実測で決めた**（実装時に libass で実際に描いて 1 文字あたりの実寸を測った）:

| フォント | 全角 1 文字 | 半角 1 文字 |
|---|---|---|
| **Yu Gothic UI**（同梱の既定） | 0.55（かな）〜 **0.75**（漢字） | 0.33 〜 0.41 |
| Yu Gothic | 0.75 〜 0.77 | 0.35 〜 0.48 |
| Meiryo | 0.65 〜 0.67 | 0.32 〜 0.42 |
| MS ゴシック（等幅） | **0.97 〜 0.99** | 0.48 〜 0.50 |

「全角 = 1.0em」は *字送り* としては正しいが、Yu Gothic 系は左右のアキ（サイドベアリング）が
大きく、**実際に描かれる幅は 0.75em 程度**しかない。1.0 のままだと箱が横に 300px 以上余り、
「文字を囲っている」ようには見えなかったため、**既定フォントの実測値に合わせて 0.78 / 0.45** とした。
等幅フォント（MS ゴシック等）へ変えたときは **1.0 / 0.5 へ戻す**（設定 2 値のみ / §9.0-a）。

**プレビュー（Qt）も、この推定値で描く。** Qt には `QFontMetrics` があり実測できるが、**あえて使わない**:

* 使うと **プレビューの箱と焼き込みの箱が別の大きさになる**。
  「画面で合わせたのに出力がズレる」は今回いちばん避けたい壊れ方で、D-5 の方針にも反する。
* Qt の実測値もどのみち libass とは一致しない（§2.9）。**どちらも近似なら、2 つの近似を持つ意味がない。**
* 代わりに **内側余白（`comment_bg_padding_px` 既定 24px）を厚めに取り**、推定が多少外れても
  文字がはみ出して見えないようにする。推定は**切り上げ**る（狭い側に外さない）。

**あわせて、箱もキャンバスからはみ出させない**（C2 の「はみ出さないように」と同じ規約）。
はみ出す場合は**箱の側をクランプし、1 回だけ WARNING を出す**（§5.11-3）。

---

## 4. 影響範囲一覧

| # | ファイル | 変更内容 | 要件 |
|---|---|---|---|
| 1 | `src/modules/comment_decor.py` | **新規**。背景ボックスとアイコンの幾何計算・ASS 描画・ffmpeg チェーン生成 | C2/C13 |
| 2 | `src/modules/subtitle_generator.py` | 役割別 alignment/margin、`\an4\pos`、`comment_label` 既定を空へ、`burn_subtitle` に追加フィルタ口、**背景 Dialogue の生成と Layer 可変化**（§5.11-4/5） | C2/C3/C4/C13 |
| 3 | `src/timeline/model.py` | `subtitle_track_for_role()` / `COMMENT_SUBTITLE_TRACK_ID` を追加 | C1 |
| 4 | `src/timeline/commands.py` | `ChangeSubtitleRole` 新設、`AddSubtitleClip`・`PasteClips` の役割別トラック解決 | C1 |
| 5 | `src/timeline/builder.py` | `_append_subtitles` の役割別振り分け、`timeline_config` に `subtitle_tracks` を追加 | C1 |
| 6 | `src/timeline/renderer.py` | ASS グループの直後へアイコン overlay を挿す（`_composite` / `_burn_subtitles_only`） | C2 |
| 7 | `src/gui/timeline/timeline_controller.py` | `change_subtitle_role()` / `add_subtitle(role=…)` | C1 |
| 8 | `src/gui/timeline/timeline_editor_dialog.py` | 役割コンボ → `change_subtitle_role()`、失敗時の案内 | C1 |
| 9 | `src/gui/timeline/timeline_view.py` | コメントトラックでの「字幕追加」を role=comment に | C1 |
| 10 | `src/gui/timeline/preview_items.py` | 役割別アンカー、アイコン子アイテム、**背景の角丸パス子アイテム**（§5.11-6） | C2/C3/C4/C13 |
| 11 | `src/gui/timeline/preview_panel.py` | 高精度プレビューにアイコンを反映（**必須** / §9-Q4） | C2 |
| 12 | `src/gui/subtitle_preview_widget.py` | 字幕編集画面プレビューにアイコンを反映（**必須** / §9-Q4） | C2 |
| 13 | `src/archive/clip_writer.py` | 切り抜き焼き込みにアイコンを反映（**必須** / §9-Q4） | C2 |
| 14 | `src/archive/timeline_builder.py` | クリップ分割で**字幕トラック構成を保つ** | C1 |
| 15 | `src/export/resolve_export.py` | コメントの配置を役割別値で近似 | C3/C4 |
| 16 | `src/settings/settings_window.py` | `DEFAULT_SETTINGS` 追加、レガシー移行 | 全部 |
| 17 | `src/main_window.spec` / `src/main.spec` | `comment_icon.png` の同梱 | C2 |
| 18 | `tests/` | 新規 3 本＋既存の追随 | §10 |

---

## 5. 詳細設計

### 5.1 モデル（`src/timeline/model.py`）

```python
# コメント専用の字幕トラック ID (ver3 resolve11 §3 D-1)
COMMENT_SUBTITLE_TRACK_ID = "S2"
# 役割キー (既存の subtitle_generator と同じ値)
ROLE_COMMENT = "comment"
```

```python
    # 役割に対応する字幕トラックを返す (無ければ None)。作成はここでは行わない (§5.2)
    # enabled=False のときは常に既定トラック (S1) を返す = 旧挙動。
    def subtitle_track_for_role(self, role, comment_track_id=COMMENT_SUBTITLE_TRACK_ID,
                                enabled=True):
        if not enabled or str(role or "") != ROLE_COMMENT:
            return self.base_subtitle_track()
        track = self.track_by_id(comment_track_id)
        if track is not None and track.is_subtitle():
            return track
        # ID を設定で変えられている場合に備え「S1 以外の字幕トラック」で拾い直す
        base = self.base_subtitle_track()
        for candidate in sorted(self.subtitle_tracks(), key=lambda t: t.index):
            if base is None or candidate.id != base.id:
                return candidate
        return None
```

`base_subtitle_track()`（`model.py:629`）は `index` 最小 ＝ S1 を返す。
S2 の `index` は 2 なので、**`build_rows()` は必ず S1 → S2 の順で上から並べる**（`timeline_view.py:141`）。

### 5.2 コマンド（`src/timeline/commands.py`）

#### 5.2-1 トラックの用意（共通ヘルパ）

```python
# 役割に対応する字幕トラックを返す。無ければ作る (ver3 resolve11 §5.2)
# cfg: timeline_config()["subtitle_tracks"]
def ensure_subtitle_track(timeline, role, cfg):
    track = timeline.subtitle_track_for_role(
        role, cfg["comment_track_id"], cfg["role_track_enabled"])
    if track is not None:
        return track
    if not cfg["role_track_enabled"] or role != ROLE_COMMENT:
        track = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    else:
        track = Track(cfg["comment_track_id"], TRACK_SUBTITLE,
                      cfg["comment_track_index"], name=cfg["comment_track_name"])
    timeline.tracks.append(track)
    return track
```

#### 5.2-2 `ChangeSubtitleRole`（新設）

```python
# 字幕の役割を変え、必要ならコメント用トラックへ移す (ver3 resolve11 §3 D-1/D-2)
# 【重要】役割の書き換えは必ず成功させ、トラック移動は best-effort とする。
#   移動できなくても role さえ正しければ 色・Style・配置・アイコンは正しく出る。
#   ここで役割変更ごと失敗させると「色は変えられるのに役割は変えられない」ことになる。
class ChangeSubtitleRole(Command):

    label = "字幕の役割変更"

    def __init__(self, clip_id, role, subtitle_tracks_cfg, min_clip_sec=0.05):
        ...
        self.moved = False          # 呼び出し側の案内文言用
        self.move_blocked = False   # 移動先が埋まっていた

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if not isinstance(clip, SubtitleClip):
            return False
        changed = clip.role != self._role
        clip.role = self._role
        if not self._cfg["role_track_enabled"] or not self._cfg["auto_move_on_role_change"]:
            return changed
        source = timeline.track_of_clip(clip.id)
        target = ensure_subtitle_track(timeline, self._role, self._cfg)
        if target is None or source is None or target.id == source.id:
            return changed
        if target.locked or _overlaps_any(target, clip.timeline_start, clip.timeline_end):
            self.move_blocked = True
            _logger.info("移動先 %s が空いていないため役割だけ変更しました: %s",
                         target.id, clip.id)
            return changed
        source.remove_clip(clip.id)
        target.clips.append(clip)
        target.sort_clips(timeline)
        self.moved = True
        return True
```

* `_overlaps_any(track, start, end)` は `_EPS` 込みの区間交差判定（`commands.py` の既存規約に合わせる）。
* **移動しても `clip.id` は変えない**。選択・Undo・アーカイブ index の紐付けが切れないようにする。
* 位置指定（`transform.x/y`）は**触らない**。役割を変えたら勝手に画面上を飛ぶ、は避ける。

#### 5.2-3 `AddSubtitleClip`（`commands.py:817`）

* コンストラクタは既に `role=DEFAULT_ROLE` を受け取れるので**引数は増やさない**。
* `_resolve_track()`（`:852`）を `ensure_subtitle_track(timeline, self._role, cfg)` 経由へ変える。
  `track_id` が明示されていればそれを優先する（右クリックで S2 の空白を指した場合）。

#### 5.2-4 `PasteClips._resolve_track()`（`commands.py:1070`）

* 現行は「コピー元のトラック ID → 無ければ `base_subtitle_track()`」。
* **ID が見つからないときのフォールバックだけ**役割別へ変える
  （別プロジェクトへ貼った時に、コメントが S1 へ落ちるのを防ぐ）。
  ID が生きているときの挙動は resolve10 のまま。

### 5.3 生成側（`src/timeline/builder.py`）

* `build()`（`:436`）: S1 は**従来どおり必ず作る**。S2 は `_append_subtitles` が必要になった時だけ作る。
* `_append_subtitles()`（`:561`）: `item["role"]` を見て `ensure_subtitle_track()` の返り値へ載せる。
* ログを 1 行足す: `S1 %d 字幕 / S2(コメント) %d 字幕`。
* `timeline_config()`（`:184`）に `subtitle_tracks` を追加（§7.3）。
  `timeline` セクションは `_fill_timeline_nested_defaults()`（`settings_window.py:802`）が
  **入れ子まで補完する**ので、入れ子辞書にしてよい。

### 5.4 ASS 生成（`src/modules/subtitle_generator.py`）

#### 5.4-1 役割別の配置

```python
# 役割別の配置・余白 (ver3 resolve11 §3 D-3)。
# 未指定の役割は共通値 (alignment/margin_*) をそのまま使う = 旧挙動と同一。
class FontProfile:
    def __init__(self, ..., role_placements=None):
        self._role_placements = self._normalize_placements(role_placements)

    # 役割の (alignment, margin_l, margin_r, margin_v) を返す
    def placement_for_role(self, role):
        return self._role_placements.get(role, self._role_placements[_DEFAULT_ROLE])
```

* `to_ass_style_named()`（`:151`）へ placement を渡し、`iter_role_styles()`（`:165`）で役割ごとに出す。
* `build_font_profile()`（`:442`）で
  `comment` の placement を `comment_alignment` / `comment_margin_l|r|v` から組む。
  **キーが無ければ共通値**へ落とす（旧 setting.json でも挙動が変わらない）。

#### 5.4-2 位置指定タグ（D-4）

```python
def _inline_position_override(entry, video_width, video_height):
    ...
    # コメントはアイコンを左横へ確実に置くため左中央アンカー (\an4) にする。
    # 他の役割は従来どおり中央アンカー (\an5) = 既存の生成結果と完全に一致する。
    anchor = 4 if str(entry.get("role", "")) == "comment" else 5
    return f"\\an{anchor}\\pos({px:.1f},{py:.1f})"
```

#### 5.4-3 `comment_label` の既定（D-8）

* `FontProfile.__init__` の既定引数 `comment_label="コメント："` → `comment_label=""`。
* `build_font_profile()`: `subtitle_cfg.get("comment_label", "")`。
* **付与処理（`:658`）は残す**。

#### 5.4-4 `burn_subtitle()` に追加フィルタの口を開ける

```python
def burn_subtitle(input_path, subtitle_path, output_path, ffmpeg_settings,
                  total_duration=0.0, on_progress=None, target_size=None,
                  fonts_dir=None, extra_chains=None, filter_script_path=None):
```

* `extra_chains` が空 ＝ **現行と 1 文字も違わないコマンド**を組む（後方互換の要）。
* 空でなければ `subtitles=…` の出力へラベルを付け、チェーンを `;` で連結する。
* 出来上がった `-vf` 文字列が閾値を超え、かつ `filter_script_path` が渡されていれば
  そこへ書き出して `-filter_script:v` を使う（D-6）。

### 5.5 装飾モジュール（`src/modules/comment_decor.py` / 新規）

```python
# コメント字幕の装飾: 背景ボックスとアイコン (ver3 resolve11 §3 D-5 / D-9 / D-10)
# プレビュー(Qt)・焼き込み(ffmpeg)・高精度プレビューが同じ幾何を使うための唯一の計算元。
# Qt にも ffmpeg にも依存しない純 Python として書く (テストから直接叩ける)。
# 背景まわりの API は §5.11-1 にまとめてある。

# アイコン画像の実体パスを凍結/非凍結の双方で解決する
# (main_window._resolve_app_icon_path と同方式 / §5.10)。無ければ None。
def resolve_icon_path(subtitle_cfg): ...

# 1 件ぶんの配置を返す。表示しない場合は None。
#   戻り値 {"x": 左端px, "y": 上端px, "size": 一辺px, "clamped": bool}
def icon_box(item, eff_cfg, canvas_w, canvas_h): ...

# ffmpeg のフィルタチェーン断片を返す (in_label → out_label)。
#   戻り値 (chains: list[str], count: int, groups: int)
def build_icon_chains(items, eff_cfg, canvas_w, canvas_h, in_label, out_label): ...
```

`icon_box()` の中身（D-5 の表をそのまま実装）:

```python
    if str(item.get("role", "")) != "comment" or not eff_cfg.get("comment_icon_enabled", True):
        return None
    size = int(eff_cfg.get("comment_icon_size_px", 100))
    gap = int(eff_cfg.get("comment_icon_gap_px", 50))
    # 文字列の左端と垂直中心を求める (位置指定があれば \an4\pos がそのまま左中央)
    if item.get("pos_x") is not None and item.get("pos_y") is not None:
        text_left = (float(item["pos_x"]) + 1.0) * canvas_w / 2.0
        text_mid = (1.0 - float(item["pos_y"])) * canvas_h / 2.0
    else:
        text_left = float(eff_cfg.get("comment_margin_l", 190))
        text_mid = _anchor_mid_y(item, eff_cfg, canvas_h)
    x, y = text_left - gap - size, text_mid - size / 2.0
    # はみ出し防止 (要望「画像がはみ出さないように注意」)。詰めたら 1 回だけ警告する。
    cx, cy = _clamp(x, 0.0, canvas_w - size), _clamp(y, 0.0, canvas_h - size)
    return {"x": cx, "y": cy, "size": size, "clamped": (cx != x or cy != y)}
```

`build_icon_chains()` は `icon_box()` の結果を `(round(x,1), round(y,1))` でグループ化し、
グループ数ぶんの `split` と `overlay` を組む（D-6 の例のとおり）。
**件数とグループ数は INFO で必ず出す**（黙って減らさない）:
`コメントアイコン: 24 件 / 位置 1 種類`。

### 5.6 焼き込みの各経路

| # | 経路 | 差し込み位置 |
|---|---|---|
| 1 | `renderer._burn_subtitles_only()`（`renderer.py:441`） | `_write_ass()` に渡した items から chains を作り、`burn_subtitle(..., extra_chains=…)` |
| 2 | `renderer._composite()`（`renderer.py:469`） | 字幕グループの `ass` チェーンの**直後**に挿す。`current` ラベルを付け替えるだけ |
| 3 | `subtitle_generator.run()`（`:1099`） | 通常パイプライン。`build_subtitle_file` へ渡す `timeline` が items そのもの |
| 4 | `archive/clip_writer._burn_one()`（`:500`） | 切り抜き。`used_timeline` が items |
| 5 | `preview_panel._show_high_quality()`（`:909`） | 高精度プレビュー。items は 1 件ぶん（start/end を 0/1 に潰した後）で、`enable` は付けない |
| 6 | `subtitle_preview_widget`（`:333`） | 字幕編集画面のプレビュー |

いずれも「items（`SubtitleClip.to_item()` 互換の辞書配列）＋ eff_cfg ＋ キャンバス寸法」から
`build_icon_chains()` を呼ぶだけで、**判断ロジックは 1 箇所に閉じる**。

`_composite()` は**入力を 1 本も増やさない**（`movie` ソースのため）ので、
`inputs` / `audio_labels` の採番（`renderer.py:474-480`）に手を入れなくてよい。

### 5.7 Timeline プレビュー（`src/gui/timeline/preview_items.py`）

1. `default_subtitle_anchor(eff_cfg, w, h)` → **`role` 引数を足す**。
   `comment` なら `comment_alignment` / `comment_margin_*` を読む（無ければ共通値）。
2. `SubtitleOverlayItem.__init__`:
   * `comment_label` の付与（`:280-281`）は**そのまま残す**（既定が空なので付かない / D-8）。
   * 役割が comment かつアイコンが有効なら、`QGraphicsPixmapItem` を**子として**足す。
     `QPixmap(...).scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)`。
     ピクスマップは**クラス変数でキャッシュ**する（再生中に毎回ディスクから読まない）。
   * 子アイテムは `ItemIsSelectable` / `ItemIsMovable` を**付けない**（アイコン単体は掴めない）。
3. `_place()`（`:313`）: comment は **左中央アンカー**で置く（D-4）。
   子アイコンの位置は親の item 座標で `(-gap-size, (textHeight-size)/2)`。
4. `_OverlayMixin` に `anchor_scene_point()` を切り出し、
   `mouseReleaseEvent`（`:111`）はそれを使う。
   * 既定実装: `sceneBoundingRect().center()`（従来どおり）
   * `SubtitleOverlayItem`（comment）: **文字だけの矩形の左中央**
     （`QGraphicsSimpleTextItem.boundingRect()` を `mapToScene` する。子は含めない）
5. `PreviewPanel._on_overlay_moved()`（`preview_panel.py:493`）は変更不要
   （渡ってくる座標の意味が変わるだけで、正規化変換は同じ）。

### 5.8 インスペクタ／Timeline 画面

* `_on_role_changed()`（`timeline_editor_dialog.py:1087`）
  → `controller.change_subtitle_role(clip.id, role)`。
* 戻り値で **状態を案内**する（既存の `status_message` 経路を使う）:
  * 移動できた: `コメントトラック(S2)へ移しました`
  * 移動できなかった: `同じ時間にコメントがあるため、役割だけ変更しました`
* `timeline_view._add_subtitle()`（`:853`）: 右クリックしたトラックが
  コメントトラックなら `role="comment"` で追加する（`add_subtitle(..., role=…)`）。
* トラックヘッダ（`timeline_view.py:274`）は `track.id` を出しているので **"S2" と出る**。
  設定 `comment_track_name` は保存データ側の名前で、ヘッダ表記は変えない（既存の見た目を保つ / §9-Q8）。

### 5.9 書き出し・アーカイブ

* `resolve_export.py:165` のラベル付与は**残す**（既定が空なので出ない）。
* `resolve_export._title_position()`（`:132`）／`_align_name()`（`:88`）へ
  **役割別 placement** を渡す。コメントだけ左中央として近似位置を計算する。
* **アイコンは Resolve/FCPXML へは出さない**（画像を別クリップとして構成する必要があり、
  現行の「タイトル＋キャプション」出力の枠に収まらない）。**含めないことで確定**（§9-Q5）。
* `archive/timeline_builder._build_group_timeline()`（`:270`）:
  現在 `Subtitle 1` を 1 本作って**全字幕トラックを流し込んでいる**（`:299`）。
  これだと S1 と S2 のコメントが同じトラックで重なり、
  読み込み時に `_validate_overlaps()` が**片方を右へ押し出してしまう**。
  → **元 Timeline の字幕トラック構成（id/index/name）をそのまま複製**し、
  クリップは**元のトラックに対応する側へ**入れる。

### 5.10 同梱（`src/main_window.spec` / `src/main.spec`）

```python
        ('comment_icon.png', 'src'),   # コメント字幕のアイコン (ver3 resolve11 §5.10)
```

`resolve_icon_path()` は
凍結時 `os.path.join(sys._MEIPASS, "src", "comment_icon.png")`、
非凍結時 `<repo>/src/comment_icon.png`（`comment_decor.py` の 1 つ上の階層）を見る。
**見つからなければアイコンを描かず、WARNING を 1 回出して処理は続ける**
（画像が無いだけで書き出しが止まるのは割に合わない）。

### 5.11 背景ボックス（C13 / rev2）

#### 5.11-1 API（`comment_decor.py` へ追加）

```python
# 文字の外接矩形を推定する (ver3 resolve11 §3 D-10)
# 実測ではなく推定。プレビューも焼き込みもここだけを見るので、両者は必ず一致する。
#   戻り値 {"width": px, "height": px, "lines": 行数}
def text_extent(item, eff_cfg): ...

# 背景ボックス (アイコン + 文字 + 内側余白) を返す。出さない場合は None。
#   戻り値 {"x", "y", "w", "h", "radius", "clamped"}
def background_box(item, eff_cfg, canvas_w, canvas_h): ...

# 背景の ASS Dialogue 行を返す (角丸矩形のベクター描画 / D-9)
def build_background_event(item, eff_cfg, canvas_w, canvas_h, style_name): ...

# 角丸矩形の \p1 描画コマンド文字列 (w, h, r → "m … b …")
def rounded_rect_path(w, h, r): ...
```

#### 5.11-2 幾何

`icon_box()`（§5.5）が返す矩形と、`text_extent()` の推定を**合わせて囲う**:

```python
    pad = int(eff_cfg.get("comment_bg_padding_px", 24))
    ext = text_extent(item, eff_cfg)
    icon = icon_box(item, eff_cfg, canvas_w, canvas_h)   # 無効なら None
    text_left, text_mid = _text_anchor(item, eff_cfg, canvas_w, canvas_h)  # §5.5 と同じ計算
    # 中身 (アイコン + 文字) の外接矩形
    left = icon["x"] if icon else text_left
    right = text_left + ext["width"]
    half = max(ext["height"], icon["size"] if icon else 0) / 2.0
    # 内側余白を足して箱にする
    box = {"x": left - pad, "y": text_mid - half - pad,
           "w": (right - left) + pad * 2, "h": half * 2 + pad * 2,
           "radius": int(eff_cfg.get("comment_bg_radius_px", 24))}
```

* **アイコンが無効／画像が見つからないときは文字だけを囲う**（箱は自動的に狭くなる）。
* 角丸半径は箱の短辺の半分を超えないよう丸める（`r = min(r, w/2, h/2)`）。超えると描画が破綻するため。

#### 5.11-3 はみ出し防止

アイコンと同じ規約でクランプする（D-10）:

```python
    box["x"] = _clamp(box["x"], 0.0, max(canvas_w - box["w"], 0.0))
    box["y"] = _clamp(box["y"], 0.0, max(canvas_h - box["h"], 0.0))
    # 箱そのものがキャンバスより大きい場合は幅・高さも詰める
    box["w"] = min(box["w"], canvas_w)
    box["h"] = min(box["h"], canvas_h)
```

**箱を詰めても文字は詰まらない**（文字の位置は ASS が決める）ため、
クランプが起きた ＝ **設定の見直しが要るサイン**として WARNING を出す:
`コメント背景がキャンバスに収まらないため詰めました (推定幅 1350px / 使える幅 900px)`。

#### 5.11-4 重ね順（C15）

**下から順に「背景 → 文字 → アイコン」**。実現方法が 2 段構えになるので明示しておく:

| 要素 | 描く場所 | 重ね順の決まり方 |
|---|---|---|
| 背景（角丸の箱） | ASS `Dialogue` / **Layer 0** | 同じ ASS の中で Layer が小さいほど下 |
| コメント本文 | ASS `Dialogue` / **Layer 1**（コメントのみ引き上げる） | 背景より上 |
| アイコン | ffmpeg `overlay`（`ass` フィルタの**後**） | ASS の出力全体の上に載る（§5.6） |

* Layer は `comment_bg_layer`(0) / `comment_text_layer`(1) で設定可能にする。
  **同一 Layer 内の描画順に依存しない**（libass の実装依存を避ける）ため、明示的に分ける。
* 配信者・サブの字幕は **Layer 0 のまま**＝現行と同一。
  コメントだけ Layer 1 になるが、配置が違う（下中央 vs 中央左）ので実害はない。
  手でドラッグして重ねた場合はコメントが前面になる。
* `build_subtitle_file()`（`subtitle_generator.py:623`）は現在すべて `Dialogue: 0,` 固定なので、
  **Layer を可変にする**（既定 0 ＝ 現行と同一出力）。

#### 5.11-5 生成箇所

`build_subtitle_file()` のループ内で、**コメント役割の entry の直前に背景行を 1 本足す**:

```python
    for entry in timeline:
        ...
        # コメントは背景 (角丸の箱) を 1 本手前に出す (ver3 resolve11 §5.11)
        bg = comment_decor.build_background_event(
            entry, eff_cfg, video_width, video_height, style_name)
        if bg:
            body_lines.append(bg)
        body_lines.append(f"Dialogue: {layer},{start},{end},{style_name},,0,0,0,,{text}")
```

* **ASS を作る場所は 1 つ**（`build_subtitle_file`）なので、
  ここへ入れるだけで **§9.1 の全経路（通常パイプライン・Timeline・切り抜き・各プレビュー）に一度に効く**。
  アイコン（フィルタ側）のように経路ごとの差し込みが要らない。
* `build_subtitle_file()` は `eff_cfg` を受け取っていないため、**引数を 1 つ増やす**
  （`subtitle_cfg=None`。省略時は背景を描かない ＝ 既存呼び出しは挙動不変）。
  呼び出し元 6 箇所（§5.6 の表）で `eff_cfg` を渡す。

#### 5.11-6 プレビュー（Qt 側）

* `SubtitleOverlayItem` に **`QGraphicsPathItem` の子**（角丸矩形）を足す。
  `path.addRoundedRect(...)` / `setBrush(QColor(0, 0, 0, 128))` / `setPen(Qt.NoPen)`。
* **`setZValue(-1)`** にする。Qt は負の Z を持つ子アイテムを**親より先（下）に描く**ため、
  「背景 → 文字」の順が保証される。アイコンの子は Z 0 のまま ＝ 文字と同じかそれより上。
* 大きさは `background_box()` の返り値をそのまま使う（**Qt の実測は使わない** / D-10）。
* `anchor_scene_point()`（D-7）は**文字だけの矩形**を見るので、
  背景を足しても**ドラッグで保存される位置は変わらない**。

---

## 6. 画面仕様（見た目）

### 6.1 位置関係（1920×1080・既定値）

```
 x=0     16   40                    190
 |       |    |<--- 100px --->|<50px>|
 |    ,--+----+---------------+------+-------------------------------+--,
 |   (   |    +---------------+      | ここにコメント本文が入ります  |   )  <- 背景: 角丸 r=24
 |   (   |    |   アイコン    |      | 2 行目もこの左端から始まる    |   )     黒 / 透明度 50%
 |   (   |    |   100x100     |      +-------------------------------+   )  <- an4: 垂直中央 (y=540)
 |    `--+----+---------------+------------------------------------------'
 |       |<-24                                                       24->|
 |                                        +------------------+
 |                                        |  通常字幕 (an2)  |  <- 従来どおり下中央 (margin_v=60)
 +----------------------------------------+------------------+-----------
                        comment_margin_l = 40 + 100 + 50 = 190
```

* アイコン左端 = `190 − 50 − 100 = 40` ＝ **通常字幕の左余白と同じ 40px** に自然に揃う。
* **背景ボックス**は「アイコン左端 − 24」から「文字右端 + 24」まで、
  高さは「アイコンと文字の高いほう + 24×2」。角丸半径 24px、黒・透明度 50%（C13）。
  例: 2 行 × フォント 48px・最長 20 文字なら **x=16, y=458, w=947, h=164**
  （文字幅の推定 = 20 × 0.78 × 48 ≒ 749px / D-10 の実測係数）。
* 背景がアイコンより **24px だけ外へ出る**ので、`comment_margin_l = 190` の既定でも
  箱は x=16 に収まり、**キャンバスからはみ出さない**（§5.11-3）。
* 通常字幕は下中央、コメントは中央左なので、**同時に出しても重ならない**。

### 6.1.1 縦動画（1080×1920・Q7 の回答を反映）

```
 x=0  16   40             140
 |    |    |<-- 50px -->|<50px>|
 |  ,-+----+------------+------+----------------------------+--,
 | (  |    +------------+      | ここにコメント本文が入る   |   )  <- 背景: 角丸 r=24
 | (  |    |  アイコン  |      | 2 行目もこの左端から始まる |   )     黒 / 透明度 50%
 | (  |    |   50x50    |      +----------------------------+   )  <- an4: 垂直中央 (y=960)
 |  `-+----+------------+---------------------------------------'
 |
 +--------------------------------------------------------------
              comment_margin_l = 40 + 50 + 50 = 140
```

* アイコンだけ 50×50 へ縮め、**間隔 50px は横と共通**（要望で指定があったのは大きさのみ）。
* 左余白を 140 にすると、**アイコン左端はやはり 40px** で、横動画と同じ見え方に揃う。
* 背景の余白・角丸・色は横と共通の既定（縦専用の上書きキーは設けない）。
  例: 2 行 × フォント 90px なら **x=16, y≈828, h≈264**。
  ただし縦は**幅の余裕が小さい**ため、長いコメントで箱がクランプされやすい（§9-Q12）。
* 切り替えは `build_effective_subtitle_cfg()`（`subtitle_generator.py:489`）が
  縦プロファイル時にのみ `comment_*` を縦値へ差し替えることで行う（§7.2）。
  **横動画のときは縦の値を一切見ない**ので、横の見た目は §6.1 のまま変わらない。

### 6.2 Timeline の行

```
+----+----------------------------------------------+
| S1 | [ 通常字幕 ][ 通常字幕 ][   通常字幕   ]      |  <- 従来からある行
| S2 |     [   コメント   ]      [  コメント  ]      |  <- 役割=コメントにすると現れる行
| V2 | ...                                          |
| V1 | ...                                          |
| A1 | ...                                          |
+----+----------------------------------------------+
```

* S2 は**コメントが 1 件も無ければ現れない**。
* 行の高さは既存の `timeline.ui.subtitle_track_height_px`（既定 40）を共用する。

---

## 7. setting.json の追加・変更キー

### 7.1 `subtitle` セクション（フラット。既存の `comment_*` 命名に合わせる）

| キー | 既定 | 意味・検証 |
|---|---|---|
| `comment_label` | `""` ← **変更**（旧 `"コメント："`） | コメント本文の先頭ラベル。空で無効（C3） |
| `comment_alignment` | `4` | コメントの ASS 配置。**左寄せ 1/4/7 のみ**。他は警告して `4` へ（D-4） |
| `comment_margin_l` | `190` | `margin_l + アイコン幅 + 間隔` が既定の根拠（§6.1） |
| `comment_margin_r` | `40` | |
| `comment_margin_v` | `60` | an4 では未使用（an1/an7 用） |
| `comment_icon_enabled` | `true` | false で **アイコンを一切描かない**（＝旧挙動＋ラベル無し） |
| `comment_icon_path` | `""` | 空 = 同梱の `src/comment_icon.png`。絶対パスで差し替え可 |
| `comment_icon_size_px` | `100` | 1〜キャンバス短辺。範囲外は既定へ |
| `comment_icon_gap_px` | `50` | 0 以上。負値は 0 へ |
| `comment_icon_filter_script_chars` | `8000` | この長さを超えたら `-filter_script:v` へ逃がす（D-6） |

### 7.2 `vertical` セクション（縦動画の上書き / C11）

| キー | 既定 | 備考 |
|---|---|---|
| `comment_alignment` | `4` | 横と同じ（垂直中央・左寄せ） |
| `comment_margin_l` | **`140`** | `margin_l(40) + アイコン幅(50) + 間隔(50)`。**アイコン左端はやはり 40px に揃う**（§6.1.1） |
| `comment_margin_r` | `40` | |
| `comment_margin_v` | `320` | 横の `margin_v` と同じ考え方（an4 では未使用） |
| `comment_icon_size_px` | **`50`** | **Q7 の回答**。縦は 1080 幅のため 100px だと大きすぎる |
| `comment_icon_gap_px` | `50` | 間隔は横と同じ。要望で指定があったのは**大きさだけ**のため変えない |

`subtitle_generator._VERTICAL_OVERRIDE_KEYS`（`:481`）へ上記 6 キーを追加する。

### 7.3 `timeline` セクション（入れ子。`_fill_timeline_nested_defaults` が補完する）

```jsonc
"subtitle_tracks": {
  "role_track_enabled": true,        // false で従来の 1 トラック運用へ戻す
  "comment_track_id": "S2",
  "comment_track_name": "Comment",
  "comment_track_index": 2,
  "auto_move_on_role_change": true   // false なら役割だけ変えてトラックは動かさない
}
```

`timeline_config()`（`builder.py:184`）で **型と値を検証**して返す
（`_positive_int` / `_one_of` の既存ヘルパを使う。`comment_track_id` が空文字なら `"S2"` へ）。

### 7.4 `subtitle` セクション — 背景ボックス（C13 / rev2）

| キー | 既定 | 意味・検証 |
|---|---|---|
| `comment_bg_enabled` | `true` | false で背景を描かない（アイコンだけ／文字だけにできる） |
| `comment_bg_color` | `"&H80000000"` | **黒・透明度 50%**（要望の値）。既存の `back_color` と同じ **ASS `&HAABBGGRR`** 形式で、`AA=80` = 128/255 ≒ 50% 透過。不正値は既定へ（既存の `_safe_ass_color` を再利用） |
| `comment_bg_radius_px` | `24` | 角丸半径。0 で直角。`min(w/2, h/2)` を超える値は自動で丸める（§5.11-2） |
| `comment_bg_padding_px` | `24` | 中身（アイコン＋文字）の外側に足す内側余白。推定誤差の吸収も兼ねる（D-10） |
| `comment_bg_char_width_full` | **`0.78`** | 全角 1 文字の幅（font_size 比）。**既定フォント Yu Gothic UI の実測値**（D-10 の表）。等幅フォントでは `1.0` にする |
| `comment_bg_char_width_half` | **`0.45`** | 半角 1 文字の幅（font_size 比）。同上（等幅では `0.5`） |
| `comment_bg_line_height_ratio` | `1.2` | 行高 ÷ font_size |
| `comment_bg_layer` | `0` | 背景の ASS Layer（§5.11-4） |
| `comment_text_layer` | `1` | コメント本文の ASS Layer。**背景より大きい値**であること（小さければ警告して `bg_layer + 1` へ） |

* **縦動画用の上書きは設けない**（余白・角丸・色は横縦で共通が自然なため）。
  必要になったら `_VERTICAL_OVERRIDE_KEYS` へ足すだけで対応できる。
* 色を「黒・50%」以外にしたい場合はこの 1 キーで変えられる（ハードコード禁止 / §0）。

---

## 8. 後方互換と移行

| # | 事象 | 扱い |
|---|---|---|
| 1 | **既存 setting.json に `"comment_label": "コメント："` が残る** | `_merge_with_defaults()`（`settings_window.py:861`）は**ユーザー値を温存する**ため、既定を空にしただけでは消えない。`_normalize_legacy_values()`（`:750`）へ「`subtitle.comment_label` が既知のレガシー値 `"コメント："` なら空へ寄せる」を追加し、**次回起動で自己修復**させる（ffmpeg 実行ファイルのレガシー正規化と同じ手口） |
| 2 | **既存プロジェクト（.timeline.json）に S2 が無い** | スキーマ変更なし。S2 は必要時に作られる。`SCHEMA_VERSION` は**上げない** |
| 3 | **既存プロジェクトの S1 にコメント役割のクリップがある** | **移さない**（§9-Q2 で確定）。開いた時点では S1 のままで、色・配置・アイコンは role が決めるため**出力は正しい**。S2 へ移したければ役割コンボで付け直す |
| 4 | **ドラッグ済みコメント字幕のアンカー変更（D-4）** | 開き直すと文字幅の半分ぶん右へ動く。**許容で確定**（§9-Q3） |
| 5 | **`subtitle` の新キーが古い setting.json に無い** | `_merge_with_defaults` がセクション内キーを補完し、`_has_new_keys()`（`:875`）が真になるので**一度だけ保存**される |
| 6 | **アイコンが 0 件のとき** | `extra_chains` が空 → `-vf` は**現行と完全に同一の文字列**。`_composite()` も入力・ラベル採番が不変 |
| 7 | **`role_track_enabled=false`** | S2 を作らず、`ChangeSubtitleRole` は役割だけ変える ＝ **今日の挙動そのもの** |
| 8 | **`build_subtitle_file()` の引数追加（背景 / §5.11-5）** | 追加する `subtitle_cfg` は**省略可**。渡さなければ背景行を出さない ＝ **既存の呼び出し・既存の ASS 出力と完全に同一**。テストコード（`tests/test_subtitle_color.py:43`）もそのまま通る |
| 9 | **ASS の Layer 可変化（§5.11-4）** | 既定 `0` で現行と同一文字列。**コメント役割だけ** `comment_text_layer`(1) になる。配信者・サブの Dialogue 行はバイト単位で現行と一致する |
| 10 | **背景の設定キーが古い setting.json に無い** | 5 と同じく `_merge_with_defaults` が補完し、一度だけ保存される。`comment_bg_enabled=false` にすれば**背景だけ**を切れる |

---

## 9. 確認事項と回答（Q1〜Q12 すべて回答済み ／ 未確定なし）

初版で挙げた Q1〜Q9 の回答は次のとおり（**すべて確定**）。
rev2（背景 / C13）で追加した Q10〜Q12 も**採用案どおりで確定**した（**§9.0**）。
**本書に未確定事項は残っていない。**

| # | 論点 | **回答** | 本書の扱い |
|---|---|---|---|
| **Q1** | 「中央の左に寄せる」の解釈 | **垂直中央・左寄せで合っている。ただし「デフォルトがそれ」というだけ** | 採用案どおり `comment_alignment = 4`（an4）。**あくまで既定値**であり、`setting.json` の `comment_alignment`（左寄せ 1/4/7）と、クリップ個別のドラッグ位置で**いつでも変えられる**（§7.1 / D-4）。設計変更なし |
| **Q2** | 既存プロジェクトの S1 に残るコメントを開いた時に自動で S2 へ移すか | **移さなくていい** | 採用案どおり移行しない。開いた時点では S1 のままで、**出力（色・配置・アイコン）は role が決めるので正しく出る**（§8-3）。設計変更なし |
| **Q3** | ドラッグ済みコメント字幕のアンカー変更（D-4）で既存の位置が半文字ぶんズレる | **許容する** | 採用案どおり、コメントは `\an4\pos`（左中央アンカー）へ変更する。設計変更なし |
| **Q4** | 「コメント：」撤去の範囲 | **全経路。逆に今回の対応範囲も全経路** | 撤去（C3）だけでなく **アイコン（C2）・役割別配置（C4）も全経路が必須スコープ**であることを確定。§4 の 11〜13（高精度プレビュー／字幕編集画面プレビュー／切り抜き）と §11 段階 7 は**任意ではなく必須**。※ S2 トラック（C1）だけは Timeline のデータモデルにしか存在しない概念のため、字幕編集画面（表形式）は従来どおり「役割」列のまま（§9.1） |
| **Q5** | Resolve/FCPXML 書き出しへアイコンを含めるか | **含めない** | 採用案どおり。書き出しは文字（タイトル／キャプション）だけを出し、アイコンは Resolve 側で足す前提。ラベル撤去と配置の近似（§5.9）だけ追随する。**rev2 の背景（C13）も同じ理由で含めない**（Resolve のタイトルに角丸の箱を持たせる手段が無く、図形クリップを別途組む必要があるため） |
| **Q6** | アイコンはコメント字幕 1 件につき 1 個でよいか | **1 つにつき 1 個** | 採用案どおり。表示時間はその字幕クリップの表示時間と完全に一致（`enable=between(t, start, end)`）。連続コメントをまたいで出しっぱなしにはしない |
| **Q7** | 縦動画でのアイコンの大きさ | **縦であれば 50×50** | **変更あり**。`vertical.comment_icon_size_px = 50`、`vertical.comment_margin_l = 140`（= 40 + 50 + 50）へ改訂（§6.1.1 / §7.2）。間隔 50px は横と共通のまま |
| **Q8** | S2 のトラック名表示 | **S2 で良い** | 採用案どおり、トラックヘッダは ID（`S2`）を出す。`comment_track_name`（"Comment"）は保存データ上の名前としてのみ持つ |
| **Q9** | コメントが同時に 2 件以上ある場合 | **一旦、同時に複数のコメントを出すことは考えていない** | **コメント用トラックは S2 の 1 本で確定**。S3 以降を自動生成する仕組みは作らない。同時に出そうとした場合の挙動は §9.2 に明記する |

### 9.0 rev2（背景 / C13）の確認事項と回答

要望文で決まっているのは **「角丸」「黒」「透明度 50%」「アイコンとコメントを含む」** の 4 点で、
次の 3 点は書かれていないため本書で既定を決めた。
→ **2026-08-24 回答: 「一旦、採用案で OK」。3 点とも採用案どおりで確定。**

| # | 論点 | **回答（＝採用案で確定）** | 本書の扱い |
|---|---|---|---|
| **Q10** | **角丸の半径**と**内側余白** | 半径 **24px** / 余白 **24px**（1920×1080 基準。フォント 48px の 1/2） | §7.4 の `comment_bg_radius_px` / `comment_bg_padding_px` の既定として確定。**見た目の好みは setting.json の 2 値だけで調整できる**ので、実機で見てから詰め直せる（コード変更不要） |
| **Q11** | 背景の大きさを**文字送り幅の推定**で決めること（D-10） | **推定で決める**（プレビューと焼き込みを一致させるため、Qt の実測もあえて使わない） | D-10 のまま確定。英数字中心・絵文字多用で箱が合わなくなった場合は `comment_bg_char_width_half` を触るのが第一手（§9.0-a） |
| **Q12** | **長いコメント**で箱がキャンバス幅に収まらないケース | 箱をクランプして **WARNING を出す** | §5.11-3 のまま確定。**ただし「縦動画の既定値では計算上あふれる」という観測は残る**ので、運用で見る（§9.0-b） |

> 「一旦」の回答であることを踏まえ、**3 点とも後から setting.json だけで方針転換できる**形にしてある。
> Q10 は 2 値の調整、Q11 は係数の調整、Q12 は縦の `font_size` / `max_line_length` の調整で、
> **いずれも再実装は要らない**。

#### 9.0-a Q11 が外れたときの逃げ道（実装しないが、道は塞がない）

推定が合わない典型は「半角英数だけの長いコメント」と「絵文字」。
その場合の打ち手を、**設定だけで届く順**に並べておく:

1. `comment_bg_char_width_half` を実測に寄せて調整する（0.5 → 0.55 など）。
2. `comment_bg_padding_px` を厚くして誤差を吸収する。
3. それでも足りなければ「箱を**固定幅パネル**にする」案へ切り替える
   （左端 = アイコン左、右端 = `キャンバス幅 − comment_margin_r` で固定。文字量に依らず一定）。
   これは `background_box()` の中だけで完結するので、**`comment_bg_width_mode` を足すだけ**で足りる。
   本書では**実装しない**（要望は「アイコンとコメントを含む背景」であり、
   短いコメントで箱だけが横に長いのは意図とズレるため）。

#### 9.0-b Q12 の観測事項（背景が原因ではないが、背景で見えるようになる）

* 折り返しは `wrap_lines()` が**文字数**で行い（`subtitle_generator.py:285`）、
  ASS は `WrapStyle: 2` ＝ **自動折り返し無し**（`:629`）。
* さらに **Timeline で手入力したコメントは折り返し処理を通らない**ため、1 行が長いままになり得る。
* 計算上の目安:

  | プロファイル | 最大文字数 | フォント | 推定される最長行 | 使える幅 | 判定 |
  |---|---|---|---|---|---|
  | 横 1920×1080 | 20 | 48px | 約 960px | 約 1690px（`1920 − 190 − 40`） | ⭕ 収まる |
  | 縦 1080×1920 | 15 | 90px | 約 1350px | 約 900px（`1080 − 140 − 40`） | ❌ **計算上あふれる** |

* これは**背景の有無に関わらず今もそうなっている**（背景はそれを可視化するだけ）。
* 対応は**運用で判断する**: 実際に WARNING が出たら、
  縦の `vertical.font_size` を下げるか `vertical.max_line_length` を減らす（setting.json のみ）。
  先回りして既定値を変えることはしない（**今の縦動画の見た目を勝手に変えてしまうため**）。
* 実装時は **WARNING に推定幅と使える幅の両方を出す**（§5.11-3）ので、
  どちらをどれだけ削ればよいかがログから直接分かる。

### 9.1 Q4 の適用範囲（どこに何が効くか）

| 経路 | 「コメント：」撤去 | アイコン | **背景（rev2）** | 役割別配置（中央左） | S2 トラック |
|---|---|---|---|---|---|
| Timeline 編集画面（プレビュー） | ✅ | ✅ | ✅ | ✅ | ✅ |
| Timeline の書き出し（`renderer`） | ✅ | ✅ | ✅ | ✅ | ✅ |
| 通常パイプライン（`subtitle_generator.run`） | ✅ | ✅ | ✅ | ✅ | — ※ |
| 字幕編集画面のプレビュー（`subtitle_preview_widget`） | ✅ | ✅ | ✅ | ✅ | — ※ |
| アーカイブ切り抜き（`clip_writer`） | ✅ | ✅ | ✅ | ✅ | ✅（分割時にトラック構成を保つ / §5.9） |
| Resolve / FCPXML 書き出し | ✅ | ❌（Q5） | ❌（Q5） | ✅（近似） | — |

背景は **ASS 生成の 1 箇所（`build_subtitle_file`）**で出るため（§5.11-5）、
焼き込み系の 5 経路には**まとめて効く**。個別に差し込みが要るのはアイコン（フィルタ側）だけ。

※ 通常パイプラインと字幕編集画面は**表形式で「役割」列を持つだけ**でトラックの概念が無い（`subtitle_editor_dialog.py:64`）。
役割が `comment` の行は、Timeline を組む時点で `builder._append_subtitles()` が **S2 へ振り分ける**（§5.3）。
つまり「トラックが無い画面で付けた役割」も、Timeline へ来た瞬間に正しい置き場所へ入る。

### 9.2 Q9 の帰結（同時に複数のコメントを出さない前提で確定すること）

* コメント用トラックは **S2 の 1 本のみ**。S3 以降は作らない。
* S2 は他の字幕トラックと同じく**重なりを許さない**（`project_io.py:439`）。
  したがって **同時に表示できるコメントは 1 件**で、これは仕様として妥当。
* `ChangeSubtitleRole` の `move_blocked`（§5.2-2）は
  「**すでに同じ時間に別のコメントがある**」＝ 本来やりたくない操作をした場合にだけ起きる。
  そのとき役割だけ変えて S1 に残すのは、**編集内容を失わずに気付ける**ようにするため
  （`同じ時間にコメントがあるため、役割だけ変更しました` と案内する / §5.8）。
* 将来「同時に 2 件」が要るようになったら、`comment_track_id` を増やす形で拡張できる
  （`subtitle_track_for_role()` は ID を引数で受ける作りにしてある / §5.1）。

---

## 10. テスト計画

### 10.1 新規テスト

| ファイル | 内容 |
|---|---|
| `tests/test_comment_decor.py` | **アイコン**: ① 横 1920×1080 の既定値でアイコンが `(x=40, y=490, size=100)` になる ② **縦 1080×1920 の既定値で `(x=40, y=935, size=50)` になる**（Q7 / §6.1.1） ③ `comment_margin_l` を小さくしてもキャンバス外へ出ない（クランプ＋警告） ④ 位置指定ありのとき `pos_x` が文字左端として扱われる ⑤ 役割が comment 以外なら `None` ⑥ `build_icon_chains()` が同一位置を 1 グループへまとめ `enable` を論理和で連結する ⑦ 0 件なら空リスト<br>**背景（rev2）**: ⑧ 2 行 × 全角 14 文字・フォント 48 で箱が `(x=16, y=458, w=723, h=164)` になる ⑨ **アイコンを無効にすると箱が文字だけを囲う**（左端が `190−24`） ⑩ 半角だけの行は `char_width_half` で計算される ⑪ 角丸半径が短辺の半分を超えたら丸められる ⑫ 箱がキャンバスを超えるときクランプ＋警告 ⑬ `comment_bg_enabled=false` で `None` |
| `tests/test_subtitle_comment_track.py` | ① `ChangeSubtitleRole` で S2 が生えて移動する ② 移動先が埋まっていたら**役割だけ**変わり `move_blocked=True` ③ Undo で役割・トラックとも 1 手で戻る ④ `role_track_enabled=false` なら S2 を作らない ⑤ S1/S2 で時間が重なっても `_validate_overlaps` が動かさない ⑥ 保存→読み込みで S2 が往復する |
| `tests/test_comment_placement.py` | ① `iter_role_styles()` の Comment 行だけ `Alignment=4, MarginL=190` になる ② Streamer/Sub 行は**現行と完全一致** ③ comment の `\pos` が `\an4`、他は `\an5` ④ `comment_label=""` でラベルが付かない ⑤ 縦プロファイルで `comment_*` が縦値へ上書きされる ⑥ **コメント 1 件につき背景 Dialogue が 1 行だけ増え、Layer が `0`（背景）/`1`（本文）になる** ⑦ **配信者・サブの Dialogue 行は背景が付かず `Layer=0` のまま＝現行と完全一致** ⑧ **`subtitle_cfg` を渡さない呼び出しでは背景が出ない（既存呼び出しの挙動不変）** |

### 10.2 既存テストへの追随

| ファイル | 影響 |
|---|---|
| `tests/test_subtitle_color.py:35` | `FontProfile(comment_label="コメント：")` を明示指定しているので**そのまま通る**（`:99` の `test_comment_label_after_override` も同様） |
| `tests/test_resolve_export.py:23` | 設定辞書で `comment_label` を明示しているので**そのまま通る** |
| `tests/test_timeline_project_io.py` | S2 を含む往復ケースを 1 本足す |
| `tests/test_archive_timeline.py` | クリップ分割で S1/S2 が保たれることを 1 本足す |
| `tests/test_settings_migration.py` | 旧 `"コメント："` が空へ移行されることを 1 本足す |

### 10.3 手動確認

1. Timeline で字幕を「コメント」にする → **S2 が現れて移動し、頭の「コメント：」が消える**。
2. 同じ時刻に S1 の通常字幕を置く → **プレビューで下中央と中央左に同時に出る**。
3. アイコンが**左 40px の位置**に 100×100 で出て、本文との間が 50px 空く。
   **アイコンと本文をまとめて囲う角丸の黒い箱（透明度 50%）**が背後に出る（C13）。
4. コメントをプレビュー上でドラッグ → **アイコンと背景が付いてくる**。左端へ寄せても**画面外へ出ない**。
   1 行のコメント／4 行のコメントで**箱の高さが追従する**。
5. 書き出す → **プレビューと同じ位置**にアイコンが焼かれる
   （`_burn_subtitles_only` 経路・`_composite` 経路の両方 ＝ V2 に画像が無い場合／ある場合）。
6. **縦動画プロファイル**で 3〜5 を再確認。アイコンが **50×50** で出て、左端はやはり 40px（§6.1.1）。
   **長めのコメントで背景クランプの WARNING が出ないかログを見る**（§9.0-b）。
   出たら `vertical.font_size` / `vertical.max_line_length` の見直しを検討する（setting.json のみ）。
7. **通常パイプライン**（Timeline を使わない経路）で、字幕編集画面の「コメント」列を付けて書き出す
   → **Timeline 経路とまったく同じ見た目**になる（§9-Q4）。切り抜き（アーカイブ）でも同じ。
8. `comment_icon_enabled=false` → アイコンが消え、**背景は文字だけを囲う大きさに縮む**。
   `comment_bg_enabled=false` → 背景だけ消えてアイコンと文字は変わらない。
9. `role_track_enabled=false` → S2 が生えず、今までどおり動く。
10. 同じ時間に既にコメントがある位置で別の字幕を「コメント」にする
    → **役割だけ変わって S1 に残り、案内が出る**（§9.2）。

---

## 11. 実装順序（レビュー後）

| 段階 | 内容 | 単独で確認できること |
|---|---|---|
| **1** | 設定キー追加＋`timeline_config` 検証＋レガシー移行（§7 / §8） | 起動して setting.json に新キーが増える／`comment_label` が空になる |
| **2** | 役割別 placement と `\an4`（§5.4-1/2）＋ `comment_label` 既定（§5.4-3） | ASS を目視・単体テストで確認。**この時点で C3・C4 が完了** |
| **3** | `comment_decor.py`（§5.5 / §5.11）＋ 単体テスト | 背景・アイコンの幾何が数値で検証できる（画面・ffmpeg 不要） |
| **3.5** | **背景**（§5.11-1〜5：`background_box` / `rounded_rect_path` / ASS 行の生成と Layer 可変化） | **C13 の焼き込みが完了**。ASS を書き出すだけで確認でき、**フィルタもアイコンも不要**。この時点で全焼き込み経路に背景が乗る（§9.1） |
| **4** | `burn_subtitle` の口＋`renderer` 2 経路（§5.4-4 / §5.6-1,2） | **C2 の焼き込みが完了**。書き出して目視 |
| **5** | プレビュー（§5.7 / §5.11-6）＋高精度プレビュー | **C2・C13 の画面表示が完了**。3.5＋4 と一致するか比較 |
| **6** | S2 トラック（§5.1〜§5.3 / §5.8） | **C1 が完了** |
| **7** | 残りの経路（通常パイプライン・切り抜き・字幕編集プレビュー・Resolve・アーカイブ分割）（§5.6-3〜6 / §5.9） | 全経路で見た目が揃う。**§9-Q4 により必須**（省略不可） |
| **8** | spec 同梱（§5.10）＋ リリースビルドで確認 | 凍結配布でもアイコンが出る |

各段階は**それ単体で動く**ように並べてある。
段階 2 まで入れれば C3・C4 は出せるので、C1・C2 のレビューが長引いても要望の半分は先に届く。
背景（C13 / 段階 3.5）は **ASS の生成だけで完結する**ためアイコンと独立しており、
先に入れても後に入れても他の段階を止めない。
ただし **段階 7 は「余力があれば」ではなく必須**（§9-Q4「今回の対応範囲も全経路」）。
段階 4〜6 だけで止めると、Timeline から書き出した動画と通常パイプラインで書き出した動画で
コメントの見た目が食い違うため、**段階 8 まで通してひとまとまりの対応**とする。
