# ver9 resolve13 — テロップの色を「役割」から「トラック」へ — 役割コントロールの廃止・S1〜S3 の常設・ヘッダーでの文字色指定

発端: 2026-10-29 のチャット要望 (v1.7.0 でテロップトラックを使い始めてからの実作業の手数)
対象: `src/gui/timeline/` (timeline_view / timeline_editor_dialog) / `src/timeline/` (model / commands / builder / compositor) /
　　　`src/modules/subtitle_generator.py` / `src/export/resolve_export.py` / `src/gui/timeline/preview_items.py`
調査日: 2026-10-29 (main `4755926` / v1.7.0)
種別: **要望の設計書 (実装前)**。§1 は現行コードの調査結果
前提: ver3 resolve11 (役割別トラック) / ver9 resolve2 (テロップトラックの追加) / ver9 resolve10 (役割変更の自動移動)

---

## 0. 先に結論

### 0.1 要望 (原文の要点)

| # | 要望 |
| --- | --- |
| R1 | テロップノードを選択して「役割」から選ぶ方式をやめる。**役割コントロールを削除** |
| R2 | 最初から**テロップトラック S1〜S3 を表示**する |
| R3 | **S2 はコメント**。分かるように S2 のヘッダーに**ユーザーアイコン**を置く |
| R4 | **テロップトラックのヘッダーで文字色を指定**できるようにする。色の選び方は**設定画面と同じコントロール**。設定した色がそのトラックの既定色になる。S4 以降の追加トラックにも効く |
| R5 | テロップを**個別で文字色指定した場合は個別が優先** |

### 0.2 3 行で

1. **役割 (role) は消さず、トラックから決める。** role は色だけでなくコメントのアイコン・背景・
   配置も決めており、出力 (ASS / Resolve) とプロジェクト形式の両方に通っている。
   UI から「役割」を消し、**どのトラックに載っているかで role が決まる**一方通行にする。
2. **色の優先順を 1 本の関数へ寄せる。** `クリップ個別 → トラック → 役割 (設定) → 既定` の 4 段。
   いま色の解決は**焼き込み・プレビュー・Resolve 出力の 3 か所に散っている**ため、
   ここを共通化しないとトラック色が「プレビューだけ効く」状態になる。
3. **手数が「3 手 → 0 手」になる。** いまは *役割をサブに変更 → 文字色を変更 → ドラッグで整理* の 3 手。
   変更後は **「S3 に置く」だけ**で、色はトラックが決める。

### 0.3 いまの手数と、変更後

| | 現在 (v1.7.0) | 変更後 |
| --- | --- | --- |
| サブのテロップを作る | S1 に出る → 役割を「サブ」へ → 文字色を入力 → S3 へドラッグ | **S3 に置く** (色はトラックの既定) |
| コメントにする | 役割を「コメント」へ (自動で S2 へ移動) | **S2 に置く** |
| 1 本だけ色を変える | 文字色欄に入力 | 変わらず (個別が優先 / R5) |

---

## 1. 現状の仕組み (調査結果)

### 1.1 「役割」は 4 つのことを決めている

`role` は `streamer` / `sub` / `comment` の 3 値 (`model.DEFAULT_ROLE` = streamer)。

| 決めているもの | 実装 |
| --- | --- |
| 文字色・縁の色 | `subtitle_generator.FontProfile.role_colors` / `role_outline_colors` |
| 表示位置 (コメントだけ左寄りの中央) | `FontProfile.role_placements` / `_comment_placement` |
| コメントのアイコンと背景 | `modules/comment_decor.py` (`comment_icon.png`) / `preview_items.py:45` |
| 置かれるトラック | `commands.ensure_subtitle_track` (comment → S2) |

**色だけを変えたい利用者が「サブ」を選ばされている**のが今回の発端である。
`sub` は「色が違うだけの役割」で、位置もアイコンも `streamer` と同じ。

### 1.2 色の決まり方 (現在は 3 段)

```
クリップ個別 (clip.color / SubtitleClip.color)
  → 役割色 (subtitle.own_subtitle_color / sub_subtitle_color / comment_subtitle_color)
    → 配信者色 (color_hex)
```

個別色は ASS のインライン上書き (`_inline_color_override` / `\1c&H…`) で入り、
役割色は ASS の Style (`Streamer` / `Sub` / `Comment`) として焼かれる。

### 1.3 色の解決が 3 か所に散っている

| 場所 | コード |
| --- | --- |
| 焼き込み (ASS) | `subtitle_generator._inline_color_override()` + `FontProfile.iter_role_styles()` |
| プレビュー | `preview_items.py:396` `clip.color or font_profile.role_colors.get(role, …)` |
| Resolve 出力 | `resolve_export.py:222` / `:261` (同じ式) |

**同じ優先順を 3 回書いている。** トラック色を足すなら、ここを 1 つにまとめるのが先である。

### 1.4 トラックと役割は「相互に」縛り合っている

- トラック → 役割: `timeline_view._role_for_track()` — コメント用トラックで足したテロップは `comment`
- 役割 → トラック: `commands.ensure_subtitle_track()` + `auto_move_on_role_change` — 役割を変えると移動
- ドラッグの制限: `_can_drop_subtitle()` — **コメント役割は S2 から出せず、通常テロップは S2 に入れない**

役割コントロールを消すと、**この相互参照のうち「役割 → トラック」が不要になる**。
同時に、ドラッグの制限を外さないと「S1 のテロップを S2 に移す」手段が無くなる (§3.7)。

### 1.5 トラックは必要になるまで作られない

- S1 (`BASE_SUBTITLE_TRACK_ID`) は Timeline 構築時に必ず作る。
- **S2 はコメント役割のテロップが現れたときだけ**作られる (`ensure_subtitle_track` / ver3 resolve11 §5.3)。
- S3 以降は右クリックの「テロップトラックを追加」でのみ作られる (`create_subtitle_track` / `new_track_min_index=3`)。

R2 (S1〜S3 を最初から出す) はこの方針の変更にあたる。

### 1.6 トラックヘッダーは「描画だけ」で、ウィジェットを持たない

`TrackHeaderWidget.paintEvent()` が行を塗り、`row["track"].id` を文字で描いているだけ
(`timeline_view.py:352`)。子ウィジェットは 1 つも無い。寸法は次のとおり:

| 値 | 既定 | 出どころ |
| --- | --- | --- |
| ヘッダー幅 | **88px** | `ui.header_width_px` |
| テロップ行の高さ | **40px** | `ui.subtitle_track_height_px` |

設定画面と同じ `ColorField` は [色見本 28px][入力欄][既定へ] の横並びで、**88px には収まらない**。
R4 を満たす形は §3.5 と §4.4 で検討する。

---

## 2. 設計方針

### 2.1 やること

1. **Track に文字色を持たせる** (`Track.color`)。保存形式へ 1 キー足す (§3.1)。
2. **色の優先順を 1 つの関数へ寄せる**。`個別 → トラック → 役割 → 既定` (§3.2)。
3. **S1〜S3 を Timeline 構築時に作る** (§3.3)。
4. **S2 のヘッダーにユーザーアイコン**を描く (§3.4)。
5. **ヘッダーに色見本を出し、クリックで `ColorField` のポップアップ**を開く (§3.5)。
6. **インスペクタの「役割」コンボを外し**、役割はトラックから決める (§3.6)。
7. **ドラッグの役割制限を外す**。S2 へ落ちたら `comment`、出たら `streamer` (§3.7)。

### 2.2 やらないこと

- **`role` そのものを消さない。** プロジェクト形式 (`SubtitleClip.role`)・ASS の Style・
  Resolve 出力・コメント装飾がすべて役割を見ている。消すと旧プロジェクトが壊れる。
  **UI から消し、トラックから決めるだけ**にする。
- **設定の役割別カラー (配信者 / サブ / コメント) を消さない。** トラック色が未設定のときの
  既定として残る = 旧プロジェクトの見え方が変わらない。
- **縁 (アウトライン) の色はトラックに持たせない。** 要望は文字色のみ (§4.3 で拡張余地を記載)。
- **コメントのアイコン・背景・配置の規則は変えない。** 変わるのは「誰が role を決めるか」だけ。
- **S2 を増減させない。** コメントは 1 本のままとする。

### 2.3 「役割」は無くさず、トラックから決める

```
                変更前                        変更後
  利用者 → [役割コンボ] → role → トラック     利用者 → トラックへ置く → role
                            ↘ 色                                    ↘ 色はトラックが持つ
```

役割の決まり方 (変更後):

| トラック | role | 見た目 |
| --- | --- | --- |
| S1 | `streamer` | 通常 |
| S2 (`comment_track_id`) | `comment` | アイコン + 背景 + コメント用の配置 |
| S3 以降 | `streamer` | 通常。**色はトラックの既定色** |

`sub` は**新しくは付かなくなる**が、値としては残す (旧プロジェクトの表示・出力のため)。

---

## 3. 実装設計

### 3.1 Track に文字色を持たせる

```python
# src/timeline/model.py  Track.__init__
# このトラックに載るテロップの既定の文字色 (HTML #RRGGBB)。
# 空文字 = 役割の色 (設定) に従う = 従来どおり (ver9 resolve13 §3.2)
self.color = str(color or "")
```

- `to_dict()` は**空でないときだけ**載せる (旧ファイルとの差分を増やさない / `SubtitleClip.color` と同じ作法)。
- `from_dict()` は欠落時に空文字。**旧プロジェクトはそのまま開ける。**
- 字幕トラック以外 (映像・音声) では使わない。

### 3.2 色の解決を 1 か所へ

`src/timeline/subtitle_style.py` (新規) か `model` のモジュール関数として置く:

```python
# テロップ 1 本の文字色を決める (ver9 resolve13 §3.2)
#   ① クリップ個別 (clip.color)        … 利用者が 1 本だけ変えた色 (R5)
#   ② トラックの既定 (track.color)      … ヘッダーで決めた色 (R4)
#   ③ 役割の色 (設定の役割別カラー)      … 従来の振る舞い
#   ④ 配信者色                          … 最後の砦
def resolve_text_color(clip, track, role_colors, fallback):
```

- 既存 3 か所 (§1.3) をこの関数へ差し替える。
- **焼き込みでは「トラック色」も個別色と同じインライン上書きとして出す** (§3.8)。
  ASS の Style を増やす案は §4.6 で比較する。

### 3.3 S1〜S3 を最初から出す

`commands.ensure_initial_subtitle_tracks(timeline, cfg)` (新規) を用意し、
Timeline を作る 2 経路から呼ぶ:

| 経路 | 呼ぶ場所 |
| --- | --- |
| クリップ用 | `timeline/builder.py` の `build_timeline()` (字幕を並べた直後) |
| アーカイブ用 | `archive/timeline_builder.py` の `build_archive_timeline()` |

作るもの (いずれも**空のトラック**):

| ID | index | 名前 | 役割 | 初期色 |
| --- | --- | --- | --- | --- |
| S1 | 1 | Subtitle 1 | streamer | 空 (= 設定の配信者色) |
| S2 | 2 | Comment | comment | 空 (= 設定のコメント色) |
| S3 | 3 | Subtitle 3 | streamer | **§4.1 で判断** (サブ色を入れるか) |

- 本数は設定 `subtitle_tracks.initial_tracks` (既定 3) で変えられるようにする。
  **1 にすれば v1.7.0 以前と同じ**になる (切り戻し)。
- 既存プロジェクトを開いたときも不足分を作る。空トラックが増えるだけで、
  保存しなければプロジェクトは変わらない (**要判断: §4.5**)。

### 3.4 S2 のヘッダーにユーザーアイコン

素材は**既にある** `src/comment_icon.png` (コメント焼き込みで使うものと同一)。
`comment_decor.resolve_icon_path()` が凍結/非凍結の両方を解決するため、これを再利用する。

```
┌──────────────┐
│ ▣  S1        │   ▣ = 色見本 (§3.5)
├──────────────┤
│ ▣ 👤 S2      │   👤 = comment_icon.png を行高に合わせて描く
├──────────────┤
│ ▣  S3        │
└──────────────┘
```

- `paintEvent` 内で `QPixmap` を描く。読み込みは 1 回だけ行い、以後は使い回す。
- 見つからない場合はアイコン無しで続行する (**焼き込み側と同じ割り切り**)。
- 行高 40px のため、アイコンは **20〜24px 角**を上下中央に置く。

### 3.5 ヘッダーでの色指定

ヘッダー幅 88px に `ColorField` をそのまま置けないため (§1.6)、**2 段構え**にする。

1. **ヘッダーに色見本を描く** (`paintEvent` で 12×12px 程度の矩形)。
   トラック色が空なら「既定」を表す破線の枠にする (`color_field.apply_swatch` の
   `allow_empty` と同じ見せ方に揃える)。
2. **色見本をクリックするとポップアップ**が開く。中身は**設定画面と同じ `ColorField`**
   (`with_alpha=False` = HTML `#RRGGBB`)。`color_committed` を受けてコマンドを積む。

```python
# 新規コマンド (Undo 対象)
class SetSubtitleTrackColor(commands.Command):
    label = "テロップトラックの文字色"
```

- ポップアップは `QMenu` へ `QWidgetAction` で載せるか、フレームレスの小ダイアログにする
  (どちらでも `ColorField` をそのまま使える)。
- **トラック色を変えると、そのトラックに載っている個別指定の無いテロップの色が一斉に変わる** (R4)。
  プレビューの再描画は既存の Timeline 変更通知に乗る。
- S4 以降の追加トラックも同じ仕組みで色を持てる (R4 後段)。

### 3.6 役割コントロールの撤去

- `timeline_editor_dialog` の `self.role_combo` と `form.addRow(tr("役割"), …)`、
  `_on_role_changed()` を外す (`_ROLE_CHOICES` は旧プロジェクト表示のため残すか削除するかを §4.2 で判断)。
- インスペクタには代わりに**「トラック」行 (読み取り専用)** を出し、
  「このテロップは S3 に載っている」ことを見せる。色の出どころが分かるようにするため。
- 役割は `timeline_view._role_for_track()` が唯一の決定点になる。

### 3.7 ドラッグの制限を外し、落ちた先で役割を合わせる

`_can_drop_subtitle()` の「コメント役割は S2 から出られない / 通常テロップは S2 に入れない」
を撤廃し、**どのテロップもどのテロップトラックへも落とせる**ようにする。
移動コマンド (`move_subtitle_to_track`) の中で役割を落ち先に合わせる:

```
S2 へ落ちた   → role = comment  (アイコン・背景・コメント配置が付く)
S2 から出た   → role = streamer (通常テロップに戻る)
S1 ⇄ S3…     → role は streamer のまま
```

これで **R1 の「役割コントロールを削除」しても、コメント化の手段が残る**。
`auto_move_on_role_change` / `role_move_scope` は役割変更 UI が無くなるため出番が無くなる
(設定は残すが既定のまま触らない / §4.5)。

### 3.8 焼き込み・プレビュー・Resolve 出力への通し方

`to_item()` は**クリップ単位でトラックを知らない**ため、呼び出し側で色を埋める。
呼び出しは 6 か所:

| ファイル | 行 | 用途 |
| --- | --- | --- |
| `timeline/compositor.py` | 424 | 焼き込み (本線) |
| `timeline/proxy_render.py` | 476 | プロキシ |
| `gui/timeline/preview_items.py` | 536 | プレビュー |
| `gui/timeline/preview_panel.py` | 1495 | プレビュー (高品質) |
| `export/resolve_export.py` | 644 / 665 | Resolve 出力 |
| `archive/timeline_builder.py` | 1288 | アーカイブ経路 |

方針: **`to_item()` に `track_color=""` 引数を足し**、呼び出し側が所属トラックの色を渡す。
`item["color"]` が空のときだけトラック色を入れる = **既存の下流 (ASS 生成・Resolve) は無改造**。

```python
def to_item(self, track_color=""):
    ...
    color = self.color or str(track_color or "")
    if color:
        item["color"] = color
```

こうするとインライン上書きの経路 (`_inline_color_override`) にそのまま乗るため、
**焼き込み・プレビュー・Resolve の 3 系統が同時に揃う**。

### 3.9 設定

| キー | 既定 | 意味 |
| --- | --- | --- |
| `timeline.subtitle_tracks.initial_tracks` | `3` | 最初から出すテロップトラックの本数 (**新規**。1 で従来どおり) |
| `timeline.subtitle_tracks.header_color_enabled` | `true` | ヘッダーでの色指定を出すか (**新規**。切り戻し用) |
| `timeline.subtitle_tracks.role_control_enabled` | `false` | インスペクタに役割コンボを出すか (**新規**。true で従来の操作に戻せる) |
| `timeline.ui.track_color_swatch_px` | `12` | ヘッダーの色見本の大きさ (**新規**) |
| `timeline.subtitle_tracks.comment_icon_px` | `20` | S2 ヘッダーのアイコンの大きさ (**新規**) |
| `timeline.subtitle_tracks.drag_between_tracks` | `true` | 既存。§3.7 で制限が外れる |
| `subtitle.sub_subtitle_color` ほか役割色 | 既存 | トラック色が空のときの既定として残る |

**ハードコードは置かない** (CLAUDE.md の制約)。既定値は `builder._subtitle_tracks_config()` で補う。

---

## 4. 判断が要る点 (推測で実装しない)

### 4.1 S3 の初期色を「サブ色」にするか — **要判断**

要望の発端は「毎回役割をサブに変更し、文字色を変更していた」である。
**S3 に設定の `sub_subtitle_color` を初期値として入れておけば、その手間がゼロになる。**

| 案 | 内容 | 評価 |
| --- | --- | --- |
| A | S3 の初期色 = `subtitle.sub_subtitle_color` | 発端にそのまま応える。**推奨** |
| B | S3 の初期色 = 空 (= 配信者色) | まっさらだが、結局ヘッダーで色を入れる 1 手が要る |

A を推奨するが、S3 を「サブ用」と決め打ちしてよいかは利用者の運用次第のため確認したい。

### 4.2 既存プロジェクトの `role=sub` をどう見せるか — **要判断**

役割コンボを消すと、既に `sub` が付いたテロップの役割を**画面から変える手段が無くなる**。

| 案 | 内容 |
| --- | --- |
| C | 何もしない。`sub` のまま表示・出力する (色は設定のサブ色)。**推奨** |
| D | 開いたときに `sub` → `streamer` へ寄せ、色をトラック色へ移す (見え方が変わる可能性) |

C は「開いたら色が変わっていた」が起きない。D は整理されるが、**既存の出力を変えてしまう**。

### 4.3 縁 (アウトライン) の色もトラックで持つか — **要判断**

要望は文字色のみ。ただし現行のインスペクタには「縁の色」もあり、文字色と対で使う。
トラックにも `outline_color` を持たせるかは、実装の形としては同じ (1 キー増やすだけ)。
**まずは文字色だけ**で実装し、必要なら後から足す案を推奨する。

### 4.4 ヘッダーの色 UI の形 — **要判断**

| 案 | 内容 | 評価 |
| --- | --- | --- |
| E | ヘッダーに色見本を描き、クリックで `ColorField` のポップアップ (§3.5) | 幅 88px のままで済む。**推奨** |
| F | `header_width_px` を 140px 程度へ広げ、`ColorField` を実ウィジェットとして埋め込む | 要望の字面に最も近いが、ヘッダーが全面的にウィジェット化され、行の増減ごとに配置し直す実装が要る |
| G | ヘッダーの右クリックメニューに「文字色…」を足す | 実装は最小。ただし色が**常に見えない**ため R4 の意図から外れる |

E を推奨する (色見本が常に見え、選ぶときは設定画面と同じ `ColorField`)。

### 4.5 既存プロジェクトを開いたときに S2・S3 を足すか — **要判断**

| 案 | 内容 |
| --- | --- |
| H | 開いた時点で不足分を作る (空トラックが増えるだけ)。**推奨** |
| I | 新規作成時だけ作る (既存プロジェクトは従来どおり S1 のみ) |

H なら「続きから」でも同じ操作感になる。空トラックは保存するまでファイルに現れない。

### 4.6 トラック色を ASS の Style にするか、インライン上書きにするか

| 案 | 内容 | 評価 |
| --- | --- | --- |
| J | インライン上書き (`\1c&H…`)。個別色と同じ経路 (§3.8) | 下流が無改造。**推奨** |
| K | トラックごとに ASS Style を作る | ASS は小さくなるが、Style 名の払い出し・Resolve 側の対応が増える |

J で問題が出るのは「ほぼ全テロップにインライン上書きが付く」場合の ASS の肥大だが、
1 本あたり 15 文字程度で、既存の位置上書き (`\pos`) のほうがはるかに大きい。

---

## 5. 変更ファイルと段階

| 段階 | ファイル | 内容 |
| --- | --- | --- |
| **1 (土台)** | `src/timeline/model.py` | `Track.color` / `to_dict` / `from_dict` / `to_item(track_color=…)` |
| 1 | `src/timeline/subtitle_style.py` (新規) | `resolve_text_color()` — 4 段の優先順 |
| 1 | `src/timeline/commands.py` | `SetSubtitleTrackColor` / `ensure_initial_subtitle_tracks()` |
| **2 (出力)** | `src/timeline/compositor.py` / `proxy_render.py` | `to_item()` へトラック色を渡す |
| 2 | `src/gui/timeline/preview_items.py` / `preview_panel.py` | 同上 + 色解決を共通関数へ |
| 2 | `src/export/resolve_export.py` | 同上 |
| 2 | `src/archive/timeline_builder.py` | 同上 + アーカイブ経路の初期トラック |
| **3 (画面)** | `src/gui/timeline/timeline_view.py` | ヘッダーの色見本・S2 アイコン・ポップアップ・ドラッグ制限の撤廃 |
| 3 | `src/gui/timeline/timeline_controller.py` | トラック色コマンドの受け口 |
| 3 | `src/gui/timeline/timeline_editor_dialog.py` | 役割コンボの撤去 / 「トラック」表示 |
| **4 (設定)** | `src/timeline/builder.py` / `settings_window.py` / `setting.json` | §3.9 の新規キー |
| 4 | `src/i18n/catalog_en.py` | 新規文言の対訳 |

段階 1〜2 だけでは画面が変わらない (トラック色は常に空 = 現行と同じ見え方)。
**段階 3 で初めて利用者から見える。**

---

## 6. テスト計画

### 6.1 色の優先順 (新規 `tests/test_subtitle_track_color.py`)

| 条件 | 期待 |
| --- | --- |
| 個別色あり / トラック色あり | **個別色** (R5) |
| 個別色なし / トラック色あり | トラック色 |
| 個別色なし / トラック色なし / role=sub | 設定のサブ色 (従来どおり) |
| すべて空 | 配信者色 |
| トラック色が不正な文字列 | 役割色へ落ちる (画面を壊さない) |

### 6.2 出力の 3 系統が揃うこと

- `compositor` が作る ASS にトラック色のインライン上書きが入ること
- `preview_items` が同じ色を返すこと
- `resolve_export` の title 辞書が同じ色を持つこと

**3 つを 1 つのテストで突き合わせる** (散っていた式を共通化したことの検査)。

### 6.3 トラックと役割

| 条件 | 期待 |
| --- | --- |
| 新規 Timeline | S1 / S2 / S3 が存在し、S2 の index と ID が設定どおり |
| `initial_tracks=1` | S1 のみ (v1.7.0 以前と同じ) |
| S1 のテロップを S2 へドラッグ | `role` が `comment` になる |
| S2 のテロップを S3 へドラッグ | `role` が `streamer` になる |
| S3 を削除 | 既存どおり可能。S1 / S2 は削除不可 |

### 6.4 保存・互換

- `Track.color` が空なら `to_dict()` に**現れない**こと (旧ファイルとの差分ゼロ)
- 色を設定したプロジェクトを保存 → 読み直して同じ色になること
- **v1.7.0 で保存したプロジェクトが開けること** (S2・S3 が無い / `color` キーが無い)

### 6.5 手で確かめる

1. 新規採点 → 編集画面に S1 / S2 / S3 が並び、S2 にユーザーアイコンが出る
2. S3 のヘッダーで色を選ぶ → S3 のテロップが一斉にその色になる
3. S3 の 1 本だけインスペクタで色を変える → **その 1 本だけ**変わる
4. 焼き込み結果とプレビューで色が一致する
5. 役割コンボが無いこと / S2 へドラッグするとアイコンと背景が付くこと

---

## 7. 互換性と移行

| 対象 | 扱い |
| --- | --- |
| v1.7.0 以前のプロジェクト | `Track.color` 無し = 全トラックが「既定」。**見え方は変わらない** |
| 既存の `role=sub` | そのまま表示・出力 (§4.2 案 C) |
| 既存の個別色 | 最優先のまま (R5) |
| 設定の役割別カラー | 残す。トラック色が空のときの既定として効く |
| `role_track_enabled=false` の運用 | 従来どおりトラック 1 本。初期トラックも 1 本に寄せる |

---

## 8. 残課題

| 項目 | 状況 |
| --- | --- |
| フォント・サイズのトラック既定 | 今回は色だけ。同じ仕組みで広げられるが、要望に無いため見送る |
| コメントトラックを 2 本以上 | 現行どおり 1 本。複数コメント運用の要望が出たら別途 |
| ヘッダーの行が増えたときの見やすさ | `max_tracks=8` まで増やせる。8 行 × 40px = 320px を占めるため、折りたたみの要望が出たら別途 |

---

## 10. 実装結果 (2026-10-29)

§4 の判断は**すべて推奨案**で確定した (4.1 S3=サブ色 / 4.2 既存の `sub` は触らない /
4.3 文字色のみ / 4.4 色見本 + `ColorField` ポップアップ / 4.5 開いたときに補う /
4.6 インライン上書き)。段階 1〜4 をすべて実装した。

### 10.1 変更したファイル

| ファイル | 内容 |
| --- | --- |
| `src/timeline/subtitle_style.py` | **新規**。`resolve_color` (4 段の優先順) / `track_color_map` ほか |
| `src/timeline/model.py` | `Track.color` (init / to_dict / from_dict) / `SubtitleClip.to_item(track_color=…)` |
| `src/timeline/commands.py` | `SetSubtitleTrackColor` / `ensure_initial_subtitle_tracks` / `role_for_subtitle_track`、`MoveSubtitleToTrack` と `MoveSubtitlesToNewTrack` が役割を落ち先へ合わせる、スナップショットへ `color` |
| `src/timeline/builder.py` | 新規設定 4 キー + `ui.track_color_swatch_px`、構築時に初期トラックをそろえる |
| `src/timeline/compositor.py` / `proxy_render.py` | 焼き込み・プロキシへトラック色を渡す |
| `src/gui/timeline/timeline_view.py` | ヘッダの色見本・S2 アイコン・`ColorField` ポップアップ、ドラッグ制限の撤廃、役割判定を `commands` へ委譲 |
| `src/gui/timeline/timeline_controller.py` | `set_subtitle_track_color` / `subtitle_track_color` |
| `src/gui/timeline/timeline_editor_dialog.py` | 役割コンボを設定で出し分け、代わりに「トラック」行、開いたときに初期トラックを補う |
| `src/gui/timeline/preview_items.py` / `preview_panel.py` / `blur_spec_dialog.py` / `blur_canvas.py` | プレビュー 3 経路へトラック色を通す |
| `src/export/resolve_export.py` | Resolve 出力へトラック色、色式を `subtitle_style` へ統一 |
| `src/archive/timeline_builder.py` | アーカイブ経路の初期トラック・書き出し用サブ Timeline への色の引き継ぎ |
| `src/settings/settings_window.py` / `setting.json` | §3.9 の 5 キー |
| `src/i18n/catalog_en.py` | 新規文言 9 件の対訳 |
| `tests/test_subtitle_track_color.py` | **新規** 33 件 |
| `tests/test_subtitle_track_edit.py` / `test_subtitle_role_track_keep.py` | 仕様変更への追随 (ドロップ制限の撤廃・役割コンボの出し分け) |

テストは **2023 件すべて成功** (追加前 1989 件 → 34 件追加)。

### 10.2 設計から変えた点

- **色解決の中核を「文字列を取る関数」にした。** 設計では `resolve_text_color(clip, track, …)`
  だけを想定していたが、プレビュー (`SubtitleOverlayItem`) はトラックではなく色文字列しか
  受け取れない。`resolve_color(individual, from_track, role, …)` を土台に置き、
  クリップ版・item 版をその薄いラッパにした。
- **`MoveSubtitlesToNewTrack` (右クリックの「新しいトラックへ移動」) でも役割を合わせた。**
  設計ではドラッグだけを挙げていたが、同じ経路でコメント役割のまま通常トラックへ移ると
  アイコンと背景だけが残ってしまう。
- **アーカイブの書き出し用サブ Timeline へトラック色を写す処理を足した。**
  `_build_group_timeline` はトラックを作り直すため、写さないと**書き出しだけ色が戻る**。
  設計の §3.8 では to_item の呼び出し箇所しか数えていなかった。

### 10.3 画面の確認

トラックヘッダをオフスクリーンで描画して確かめた (ヘッダー幅 88px / 行 40px):

```
S1  [破線の枠]  S1          ← 既定 (色は設定に従う)
S2  [破線の枠] 👤 S2         ← comment_icon.png を 20px で表示
S3  [■ #2ABCFF] S3          ← 設定のサブ色が初期値で入る
V1  (色見本なし) V1
```

`ensure_initial_subtitle_tracks` は `S1 / S2(Comment) / S3` を index 1/2/3 で作り、
S3 にだけ設定のサブ色 (`#2ABCFF`) が入ることを実データの設定で確認した。

### 10.4 要望との対応

| # | 要望 | 実装 |
| --- | --- | --- |
| R1 | 役割コントロールの削除 | インスペクタから撤去 (`role_control_enabled=false` が既定)。代わりに「トラック」行で出どころを表示 |
| R2 | S1〜S3 を最初から表示 | 新規・既存プロジェクトの両方でそろえる (`initial_tracks=3`) |
| R3 | S2 はコメント / ユーザーアイコン | ヘッダに `comment_icon.png` を表示。S2 へ落とすと役割がコメントになる |
| R4 | ヘッダで文字色を指定 / S4 以降にも適用 | 色見本 → `ColorField` ポップアップ。テロップトラックならどれでも持てる |
| R5 | 個別指定が優先 | `個別 → トラック → 役割 → 既定` の 4 段 (焼き込み・プレビュー・Resolve で共通) |
