# コメント役割のテロップが背景ボックスからはみ出す 原因と修正設計書

現象の報告: 利用者より
> にくまるフォントでテロップの役割をコメントにした際に、背景の黒い部分の横幅が
> 足りておらず、文字がはみ出てしまっている。

本書は `docs/CLAUDE.md`「実装前に設計を行うこと」に従う **設計書** であり、
実装は本書のレビュー後に行う。

---

## 1. 結論（先に要点）

コメント背景の箱の幅は、**フォントを一切見ずに設定の固定係数で見積もっている**。

```python
# src/modules/comment_decor.py  text_extent()
full = eff_cfg["comment_bg_char_width_full"]   # 全角 1 文字 = font_size の何倍か
half = eff_cfg["comment_bg_char_width_half"]   # 半角 1 文字 = font_size の何倍か
for char in line:
    em += full if _is_full_width(char) else half
widest = max(widest, em * size)
```

この係数は**フォントごとに必要な値が 1.5 倍も違う**。
現在の設定値 `full = 0.78` は**游ゴシック向けの値**であり、
にくまるフォントには **0.926** が必要なため、全角だけで 1.19 倍足りない。
さらに `half = 0.45` は「半角は全角の半分」という前提だが、
にくまるフォントの英数字は**プロポーショナル**（0.366〜0.926 em）で、
`W` では 2.06 倍足りない。縁取り（`outline_width = 10`）の張り出しも入っていない。

実測した結果、**どの文字列でも箱から文字がはみ出していた**（§3）。

---

## 2. 現在の設定と Style

`src/settings/setting.json`（利用者の現行値）

| 設定 | 値 |
|---|---|
| `subtitle.font_family` | `07NikumaruFont` |
| `subtitle.font_size` | 130 |
| `subtitle.outline_width` | 10 |
| `subtitle.comment_bg_char_width_full` | **0.78** |
| `subtitle.comment_bg_char_width_half` | **0.45** |
| `subtitle.comment_bg_padding_px` | 24 |
| `subtitle.comment_margin_l` | 240 |
| `subtitle.max_line_length` | 20 |

生成される ASS Style（コメント）:

```
Comment,07NikumaruFont,130,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,10,1,4,240,40,60,1
                                                            太字       ScaleX/Y   Spacing  Outline
```

ScaleX / ScaleY は 100、Spacing は 0 のため、横幅に効くのは Fontsize と
フォントの送り幅、そして Outline だけである。

---

## 3. 実測（libass の実出力で確認）

1920x1080 の黒画面へ実際に焼き込み、**文字インクの右端**を 1px 単位で測った
（背景とアイコンは消し、文字だけを描いた）。`comment_margin_l = 240` が文字の左端。

| 文字列 | 箱の右端 | 文字インクの右端 | **はみ出し** | 箱の幅 | 必要な幅 |
|---|---|---|---|---|---|
| あいうえお | 772 | 840 | **68 px** | 756 | 848 |
| これはコメントです | 1177 | 1317 | **140 px** | 1161 | 1325 |
| wwwww | 557 | 742 | **185 px** | 541 | 750 |
| やばいwww | 744 | 899 | **155 px** | 728 | 907 |
| WWWWW | 557 | 789 | **232 px** | 541 | 797 |
| 8888888888 | 850 | 1031 | **181 px** | 834 | 1039 |

**純粋な日本語でもはみ出す**（68〜140 px）。英数字が混じると 150〜230 px に広がる。

### 3-1. libass の縮尺（確定）

全角 1 文字の実送り幅を測ると:

```
全角 1 文字 = 120.33 px / Fontsize 130 = 0.9256 em
フォントの advance = 1.0000 em (unitsPerEm = 1000)
libass の縮尺 = unitsPerEm / (usWinAscent + usWinDescent) = 1000 / 1080 = 0.9259
```

実測 0.9256 と計算値 0.9259 が一致する。
`src/gui/timeline/font_metrics.py` が既に書いている
「libass は **usWinAscent + usWinDescent が Fontsize になる**ようスケールする」
という性質が、横方向にもそのまま効いている。

つまり **全角 1 文字の正しい係数は `unitsPerEm / (usWinAscent + usWinDescent)`** である。

### 3-2. フォントごとに必要な係数（1.5 倍の開き）

各フォントのテーブルを直接読んで求めた値:

| フォント | upem | usWinAsc+Desc | **全角の正しい係数** | `W` の advance | **`W` に必要な半角係数** |
|---|---|---|---|---|---|
| 07にくまるフォント | 1000 | 1080 | **0.926** | 0.926 em | **0.857** |
| 游ゴシック (YuGothR) | 2048 | 2636 | **0.777** | 0.930 em | 0.723 |
| メイリオ (meiryo) | 2048 | 3072 | **0.667** | 1.001 em | 0.667 |
| MS ゴシック (msgothic) | 256 | 256 | **1.000** | 0.500 em | 0.500 |
| Segoe UI | 2048 | 2724 | 0.752 | 0.934 em | 0.702 |

**現在の設定 `full = 0.78` は游ゴシックの 0.777 とほぼ一致する。**
当時のフォント（`build_font_profile` の既定は `Yu Gothic UI`）に合わせて
調整された値と考えられる。にくまるフォントへ変えた時点で 0.926 が必要になり、
設定が追従していないため**フォントを変えたことが直接の引き金**になっている。

`half = 0.45` が正しいのは MS ゴシックのような**本当に半角幅**のフォントだけで、
英数字がプロポーショナルなフォント（にくまる・游ゴシック・メイリオ）では
常に足りない。

### 3-3. にくまるフォントの英数字の送り幅（抜粋）

| 文字 | advance (em) | 現在の `half` | 比 |
|---|---|---|---|
| `W` | 0.926 | 0.45 | **2.06** |
| `w` | 0.846 | 0.45 | **1.88** |
| `A` | 0.706 | 0.45 | 1.57 |
| `8` / `1` | 0.660 | 0.45 | 1.47 |
| 空白 | 0.500 | 0.45 | 1.11 |
| `!` | 0.446 | 0.45 | 0.99 |
| `i` | 0.366 | 0.45 | **0.81**（逆に広すぎ） |

固定値はどこかで必ず外れる。`iii` では現在の推定 176 px に対して
必要なのは 132 px で、**44 px 無駄に広い**箱になる。

---

## 4. 原因（4 つに分解）

### 原因A: 全角の係数が libass の実寸と合っていない（主因）

`comment_bg_char_width_full = 0.78` に対し、にくまるフォントは **0.926** が必要。
全角だけで 1.19 倍不足する。純日本語でも 68〜140 px はみ出す理由がこれ。

正しい値は `unitsPerEm / (usWinAscent + usWinDescent)` で**フォントから計算できる**。

### 原因B: 半角を固定係数で数えている

`_is_full_width()`（East Asian Width）で全角／半角の 2 値に振り分け、
半角はすべて `0.45` として数えている。プロポーショナルな英数字では
`W` で 2.06 倍、`i` で 0.81 倍と、**過少・過大の両方へ外れる**。

Twitch のコメントは `wwwww` `8888` `GG` のような英数字の連打が多く、
最も目立つ形で不足が出る。

### 原因C: 縁取りの張り出しが推定に入っていない

`outline_width = 10` のため、文字インクは字形の左右へそれぞれ 10 px はみ出す。
`text_extent()` は送り幅だけを見ており、**左右 10 px ずつ（計 20 px）が未計上**。
`comment_bg_padding_px = 24` が偶然吸収しているだけで、余白を 0 に設定すると
縁取りが必ず箱の外へ出る。

### 原因D: 行の長さが文字数固定で、使える幅を見ていない（併発・別件）

`max_line_length = 20` は**文字数**での折り返し（`wrap_by_length` は `len()` 比較）。
font_size 130 では 1 行 20 文字 = 20 x 0.926 x 130 = **2407 px** で、
左端 240 px から始まると右端は 2647 px となり **キャンバス幅 1920 を超える**。

この場合 `_background_box_raw` は箱をキャンバス幅へ詰め（`w = min(w, canvas_w)`）、
WARNING を 1 回出すが、**文字そのものは `WrapStyle: 2`（自動折返し無し）のため
画面外へ流れる**。font_size 130 で収まるのは約 13 文字である。

`subtitle.comment_margin_r`（= 40）は定義されているが `comment_decor` では
**一度も使われていない**（ASS Style の MarginR へ入るだけで、`\pos` 指定時は
libass が無視する）。折り返しの長さが使える幅から決まっていない。

---

## 5. 修正案

### 案A（推奨）: フォントから送り幅を実測する

**固定係数をやめ、フォントファイルから advance を読んで積む。**

```
幅 = Σ advance_em(文字) x (unitsPerEm / (usWinAscent + usWinDescent)) x font_size
     + outline_width x 2
```

#### 実現性は確認済み

追加ライブラリ無し・Qt 非依存の純 Python で sfnt を読む試作を書き、
`QRawFont` の実測値と一致することを確かめた:

| 文字 | 試作パーサ | QRawFont (pixelSize=100) |
|---|---|---|
| あ / 漢 / ー | 1.0000 | 1.00 |
| `W` | 0.9260 | 0.93 |
| `w` | 0.8460 | 0.85 |
| `A` | 0.7060 | 0.71 |
| `i` | 0.3660 | 0.37 |

さらに、このモデルが **libass の実出力を当てられる**ことも確認した
（§3 と同じ測り方。モデル値 − 実測値）:

| 文字列 | 実測 | モデル | 差 | 現推定 |
|---|---|---|---|---|
| あいうえお | 600 | 601.9 | **+1.9** | 508 |
| これはコメントです | 1077 | 1083.3 | **+6.3** | 913 |
| wwwww | 502 | 509.2 | **+7.2** | 293 |
| やばいwww | 659 | 666.6 | **+7.6** | 480 |
| WWWWW | 549 | 557.3 | **+8.3** | 293 |
| 8888888888 | 791 | 794.4 | **+3.4** | 586 |
| iii | 122 | 132.2 | **+10.2** | 176 |

差は常に **+2〜+10 px（＝わずかに広い側）** で、
「狭い側に外すと文字がはみ出す」という `text_extent` の方針（§3 D-10）に合う。

#### 変更内容

**① 新規 `src/modules/font_advance.py`**（純 Python / Qt 非依存 / 追加ライブラリ無し）

- sfnt（TTF / OTF / TTC）の `head` `OS/2` `hhea` `hmtx` `cmap` `name` を読む
- `metrics_for(family, settings)` → `{"upem", "win_height", "advance_em(char)"}`
  / 読めなければ **必ず `None`**（プレビューも出力も落とさない）
- フォントファイルの解決順（見つかった時点で確定し、プロセス内でキャッシュ）
  1. `subtitle.custom_fonts_dir`（**libass へ `fontsdir` で渡しているのと同じ場所**。
     同梱のにくまるフォントはここにあるので、報告の事例は 1 件の読み込みで済む）
  2. `%WINDIR%\Fonts` と `%LOCALAPPDATA%\Microsoft\Windows\Fonts`
  3. `subtitle.font_file_dirs`（追加の置き場。既定は空）
  - 突き合わせは `name` テーブルの family（nameID 1 / 16）
- `comment_decor` が読む側になるため、`comment_decor` の
  「Qt にも ffmpeg にも依存しない純 Python」という性質は保たれる

**② `comment_decor.text_extent()` の変更**

```python
metrics = font_advance.metrics_for(_font_family(item, eff_cfg), eff_cfg)
if metrics is not None and eff_cfg.get("comment_bg_measure_font", True):
    k = metrics["upem"] / metrics["win_height"]
    em = sum(metrics["advance_em"](char) for char in line) * k
else:
    em = sum(full if _is_full_width(char) else half for char in line)   # 従来
```

- `item["font"]`（個別フォント指定）と役割別フォントにも対応する
- `comment_bg_char_width_full` / `_half` は**フォールバック用に残す**（互換）

**③ 縁取りの張り出しを入れる（原因C）**

内容の矩形を左右へ `outline_width` 広げてから余白を足す。

```python
bleed = _to_float(eff_cfg.get("outline_width"), 0.0)
left  = min(icon["x"], text_left - bleed) if icon else text_left - bleed
right = text_left + extent["width"] + bleed
```

**④ 設定追加**（`src/settings/settings_window.py` の既定 + 読み出し側）

| 設定 | 既定 | 意味 |
|---|---|---|
| `subtitle.comment_bg_measure_font` | `true` | フォントの実測を使う。`false` で従来の固定係数へ戻せる |
| `subtitle.comment_bg_include_outline` | `true` | 縁取りの張り出しを箱に入れる |
| `subtitle.font_file_dirs` | `[]` | フォントファイルの追加の探索先 |

#### 効果

- **どのフォントでも正しい**。にくまる・游ゴシック・メイリオ・利用者追加フォントすべて
- `iii` のように**無駄に広い箱も直る**
- プレビュー（`preview_items`）は引き続き `comment_decor.background_box()` を使うため、
  「プレビューと出力が同じ箱」という性質（§3 D-5）はそのまま保たれる。
  プレビューは文字を既に `QFontMetricsF.horizontalAdvance` の実測で描いているので、
  **画面でも文字が箱に収まるようになる**
- 設定の手調整（`comment_bg_char_width_*`）が不要になる

#### 懸念

- 初回のフォント解決で、システムフォントを探す場合に `name` テーブルを
  順に読む。同梱フォント（今回の事例）は 1 件で当たるため影響しない。
  読み込みは family 単位でキャッシュする
- libass が実際に選ぶフォントファイルと、こちらが選ぶファイルが食い違う可能性
  （同名 family が複数ある場合）。`fontsdir` を先に見ることで実運用では一致する

### 案B（暫定・設定値だけで直す）

コードを触らず、**フォントに合わせて係数を入れ替える**。

| 設定 | 現在 | にくまるフォント向け |
|---|---|---|
| `comment_bg_char_width_full` | 0.78 | **0.93**（= 1000/1080 を切り上げ） |
| `comment_bg_char_width_half` | 0.45 | **0.86**（= `W` 0.926 em x 0.926） |

- 利点: 即日反映できる。原因A・B の不足は消える
- 欠点:
  - 原因C（縁取り 20 px）は残る
  - `iii` のような細い英数字で箱が 2.5 倍広くなる
  - **フォントを変えるたびに再調整が必要**（今回の不具合そのものを再発させる）

→ 案A を入れるまでの**つなぎ**として扱い、案A の既定値では使わない。

### 案C（却下）: 文字クラス別の係数表を設定に持つ

英数字・記号・かなへ係数を分ける案。フォント依存である点が変わらず、
設定項目が増えるだけで案A の下位互換にしかならないため採らない。

---

## 6. 原因D（はみ出しとは別件）について

font_size 130 / `comment_margin_l` 240 では 1 行 13 文字で画面幅に達するのに、
`max_line_length` は 20 文字である。**文字そのものが画面外へ流れる**。
箱の幅を直しても、この行は箱がキャンバス幅へ詰められるため収まらない。

対処の方向（**どれを採るかは §8 の確認事項**）:

1. **警告だけ出す**: 使える幅（`canvas_w - comment_margin_l - comment_margin_r`）を
   超える行を検出し、`max_line_length` か `font_size` を下げるよう促す（最小の変更）
2. **幅基準で折り返す**: `wrap_lines` を文字数ではなく実測幅で折り返す
   （案A の `font_advance` があれば実装できる。影響範囲は配信者テロップにも及ぶ）
3. **コメントだけ上限文字数を別に持つ**: `subtitle.comment_max_line_length` を足す

---

## 7. テスト方針

`tests/test_comment_decor.py` / `tests/test_comment_placement.py` に追加
（既存テストが固定係数を前提にしている箇所は、フォールバック経路として残す）。

新規 `tests/test_font_advance.py`

- 同梱の `07にくまるフォント.otf` から `upem = 1000` / `win_height = 1080` を読むこと
- `あ` = 1.0000 em / `W` = 0.9260 em / `i` = 0.3660 em を読むこと（§5 の表）
- `cmap` に無い文字・壊れたファイル・存在しないファミリでは `None` を返すこと
- TTC（`meiryo.ttc` など）でも先頭フォントを読めること（環境に無ければ skip）

新規 `tests/test_comment_bg_width.py`

- にくまるフォント + font_size 130 で、§3 の表の文字列について
  `text_extent()["width"]` が**実測値以上**になること（はみ出さないこと）
- `iii` で従来の推定（176）より**狭く**なること（無駄な広さが消えること）
- `comment_bg_measure_font = false` で従来の値に戻ること（互換）
- `outline_width` を増やすと箱が左右へ広がること（原因C）
- フォントが見つからない設定では例外にならず従来の係数へ落ちること

実出力との突き合わせ（手動確認手順として本書に残す）

- 1920x1080 の黒画面へ焼き込み、文字インクの右端が箱の右端を超えないこと
  （§3 と同じ測り方。ffmpeg と QImage だけで測れる）

---

## 8. 確認事項（推測実装しない）

1. **原因D の対処**を §6 の 1 / 2 / 3 のどれにするか。
   2（幅基準の折り返し）は配信者テロップの見た目も変わるため、
   影響の大きさから別案件に分けたい。
2. 案B（設定値の暫定変更）を**今すぐ入れるか**。案A の実装まで待てるなら、
   設定を 2 度変えずに済む。
3. 案A で**システムフォントの探索を入れるか**。同梱フォントだけに限る
   （`custom_fonts_dir` のみ）なら実装も探索コストも小さいが、
   利用者が OS のフォントを選んだ場合は従来の固定係数へ落ちる。
4. `comment_bg_line_height_ratio = 1.2` は箱の**高さ**の係数だが、libass の行送りは
   Fontsize そのもの（`font_metrics.line_advance_px` の記述どおり）で、
   こちらは**高さが余る側**に外れている。今回の報告（横幅）とは別のため触っていないが、
   案A と同時に実測へ寄せるか。
5. `comment_margin_r` は `comment_decor` で未使用である。箱の右端をこの余白で
   止める（= 画面端に寄りすぎないようにする）仕様にするか、設定を廃止するか。

---

## 9. 実装（案A / 2026-10-23）

利用者の指示により案A を実装した。案B（設定値の暫定変更）は入れていない
（案A の既定で実測が効くため、設定を 2 度変える必要がなくなった）。
§8 の確認事項 1（原因D）・4（箱の高さ）・5（`comment_margin_r`）は本件の範囲外として
手を付けていない。確認事項 3（システムフォントの探索）は**入れた**
（OS のフォントを選んだ場合も実測が効くようにするため。走査の実測は 41ms / §9-3）。

### 9-1. 変更したファイル

| ファイル | 変更 |
|---|---|
| `src/modules/font_advance.py` | **新規**。sfnt (TTF/OTF/TTC) から送り幅と縦メトリクスを読む（純 Python / Qt 非依存 / 追加ライブラリ無し） |
| `src/modules/comment_decor.py` | `text_extent` が実測で幅を求める。`_background_box_raw` が縁取りの張り出しを入れる |
| `src/settings/settings_window.py` | `comment_bg_measure_font` / `comment_bg_include_outline` / `font_file_dirs` を追加。固定係数のコメントを書き替え |
| `src/settings/setting.json` | 同じ 3 つを追加（既存値は変えない） |
| `tests/test_comment_decor.py` | 幅に関わる既存テストを**固定係数のフォールバック経路**（`_legacy_cfg`）へ向けた。縁取りの張り出しのテストを追加 |
| `tests/test_font_advance.py` | **新規** 23 件 |
| `tests/test_comment_bg_width.py` | **新規** 14 件 |

`comment_bg_char_width_full` / `_half` は**設定に残した**。フォントを読めない環境・
`comment_bg_measure_font = false` のときのフォールバックに使う（既存の値も変えていない）。

### 9-2. 設計からの追加: 書体（ウェイト）の選び方

設計時に見落としていた点が実装中に出たため、仕様を足した。

**① 太字の書体を選ぶ**

ASS Style の `Bold` が立っていると libass は太字の書体を選ぶ。太字の書体を持つ
フォントでは英数字の送り幅がウェイトで変わる（游ゴシック UI の `W` は
Regular 0.934em / Bold 0.966em）。`metrics_for(..., bold=)` で
`subtitle.bold` と同じ書体を選ぶようにした。
太字の書体が無いフォント（同梱のにくまるなど）は libass が標準の書体を太らせて使い、
**送り幅は変わらない**（実測で確認済み）ため、あるほうの書体へ落とす。

**② `name` テーブルの (族名, スタイル名) は組で持つ**

1 つの書体は 2 通りの名乗り方を持つ。

```
nameID (1, 2)   = 4 スタイル単位の族名とスタイル名   ("Yu Gothic UI Light" + "Regular")
nameID (16, 17) = ウェイトをまとめた族名とウェイト名 ("Yu Gothic UI"       + "Light")
```

当初は 1/16 を族名、2/17 をスタイル名としてまとめて集めていたが、それだと
Light の書体が「Yu Gothic UI の Regular」に見えてしまい、**Light が当たっていた**
（`W` が 0.895em で Regular 0.934em より**狭い** = 文字がはみ出す側へ外れる）。
組を混ぜずに持つよう直した結果、正しい書体が当たるようになった。

```
Yu Gothic UI   + Regular → YuGothM.ttc face1   (Microsoft は UI Regular をここへ入れている)
Yu Gothic UI   + Bold    → YuGothB.ttc face1
Yu Gothic      + Regular → YuGothR.ttc face0
Yu Gothic UI Light       → YuGothL.ttc face1
```

同じ鍵に複数の書体が当たる場合はスタイル名で優先度を付ける
（標準ウェイトを優先し、斜体は後回し）。

### 9-3. 確認した結果

**① 同梱フォント（報告された条件そのまま）**

実 libass 出力の文字インクと、修正後の箱を突き合わせた
（1920x1080 / font_size 130 / 縁取り 10 / 余白 24 / アイコン有り）。

| 文字列 | 箱の右端 | 字の右端 | 右の余裕 | 修正前のはみ出し |
|---|---|---|---|---|
| あいうえお | 876 | 840 | **+36** | −68 |
| これはコメントです | 1358 | 1317 | **+41** | −140 |
| wwwww | 784 | 742 | **+42** | −185 |
| やばいwww | 941 | 899 | **+42** | −155 |
| WWWWW | 832 | 789 | **+43** | −232 |
| 8888888888 | 1069 | 1031 | **+38** | −181 |
| iii | 407 | 362 | **+45** | （逆に広すぎ） |

**すべて箱の内側に収まった。**

**② 他のフォント・ウェイトでも収まること**

同じ測り方で、最小の余裕（px）:

| フォント | font_size | 太字 | 最小の余裕 |
|---|---|---|---|
| 07にくまるフォント | 130 | あり / なし | +36 |
| 游ゴシック UI | 96 | あり / なし | +36 |
| メイリオ | 96 | あり | +36 |
| MS ゴシック | 96 | なし | +36 |

**③ プレビュー（Qt 描画）でも収まること**

プレビューは文字を `QFontMetricsF.horizontalAdvance` の実測で描き、箱は
`comment_decor.background_box()` を使う。両者を突き合わせると:

| 文字列 | Qt の実測 | 箱の推定 | 余裕 |
|---|---|---|---|
| あいうえお | 600.0 | 602 | +2.0 |
| これはコメントです | 1080.0 | 1084 | +4.0 |
| wwwww | 510.0 | 510 | +0.0 |
| WWWWW | 555.0 | 558 | +3.0 |
| 8888888888 | 790.0 | 795 | +5.0 |

推定が実測を下回らないため、**画面でも文字が箱に収まる**。
（箱はこれに余白 24 と縁取り 10 を足すため、見た目の余裕はさらに広い。）

**④ 読み込みの時間**

| 条件 | 時間 |
|---|---|
| 同梱フォントのみ（`custom_fonts_dir` で当たる = 報告の事例） | 0 ms |
| `C:\Windows\Fonts` の走査込み（初回） | 41 ms |
| 2 件目以降（走査済み・書体単位で記憶） | 2 ms |

`name` テーブルだけを読む（8MB の TTC でも数 KB）ため、走査しても一瞬で済む。

**⑤ テスト**

全体 **1589 件 pass**（新規 37 件 / 既存 1552 件）。

### 9-4. 残っている件（本件では触っていない）

- **原因D**（`max_line_length` が文字数固定で、font_size 130 では 1 行 20 文字が
  キャンバス幅を超える）。箱の幅が実測になったことで、超えた行では
  「コメント背景がキャンバスに収まらないため詰めました（推定幅 Npx / 使える幅 1920px）」
  の警告が**正しい数値で**出るようになった。対処は §6 の 1 / 2 / 3 から選ぶ必要がある。
- `comment_bg_line_height_ratio = 1.2`（箱の**高さ**。libass の行送りは Fontsize
  そのものなので高さが余る側に外れている）。
- `comment_margin_r` が `comment_decor` で未使用。
