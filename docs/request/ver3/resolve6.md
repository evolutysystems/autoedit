# resolve6（ver3） — 字幕の色指定 / プレビュー再生の応答改善 修正設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request6.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/CLAUDE.md`「いきなり実装を開始しない」）。
* 対象画面は **クリップ用 `TimelineEditorDialog` とアーカイブ用 `ArchiveTimelineDialog`**。
  アーカイブ用は前者の派生（resolve5 §5.4）のため、**インスペクタへの追加は 1 箇所で両方に効く**。
* 記載の数値はすべて **本リポジトリの実測**（2026-08-16 / 素材 `output/archive_vod_2844372771_combined.mp4`
  = H.264 1920x1080 60fps / 883 秒）。推測で書いた値は無い。
* 決めきれない点は推測実装せず **§9 確認事項** に列挙した。
  **Q1・Q6 は回答済み**（§1.1）。残るのは実装を止めない補足事項のみで、本書の内容で着手できる。

---

## 1. 要望（request6.md）と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **B1** | 字幕を選択して編集する際に **文字の色**を変更できるようにする | 機能追加 |
| **B2** | 同じく **アウトラインの色**を変更できるようにする | 機能追加 |
| **B3** | **再生を押してもなかなか再生されない**。画質を落としてでも再生してほしい | 性能改善 |

要望文に無いが、実装に必ず要る論点:

| ID | 論点 | 理由 |
|---|---|---|
| **B4** | 個別指定した色を **焼き込み（ASS）へ通す** | 画面で変えても出力に出なければ意味が無い |
| **B5** | 個別指定した色を **プレビューの近似描画へ通す** | 変えた結果がその場で見えないと選べない |
| **B6** | 個別指定した色を **DaVinci Resolve 出力へ通す** | 既存の Resolve 出力と食い違わせない |
| **B7** | **既定へ戻す**手段 | 一度触ると設定の役割色へ戻せないのは退行 |
| **B8** | **複数選択した字幕へまとめて適用**（回答 Q1） | 1 件ずつ塗り直すのは実用にならない |

### 1.1 レビュー回答による確定事項（2026-08-16）

| # | 決定 | 反映先 |
|---|---|---|
| **Q1** | 複数選択への**一括適用を可能にする** | §3-10 / §5.5 / §5.10（要件 B8） |
| **Q6** | 再生中の映像縮小を**既定で有効**にする（`playback_width = 960`） | §3-6 / §5.7 / §5.8 / §7 |

* Q6 により、R6（縮小）は「任意・既定 OFF の逃げ道」から **既定の再生経路**へ格上げする。
  実装フェーズも Phase 4 → **Phase 3**（主因対策と同じ回）へ移す（§6）。
* 縮小は**再生中だけ**で、停止中・スクラブ中は従来どおり原寸で取得する（§5.7）。
  出力動画には一切影響しない（§8）。

---

## 2. 現状分析（実測）

### 2.1 字幕の色は「役割（配信者/サブ/コメント）ごと」にしか変えられない

現行の色の持ち方（`src/modules/subtitle_generator.py`）:

```
setting.json subtitle.own_subtitle_color / sub_subtitle_color / comment_subtitle_color   … 塗り (HTML #RRGGBB)
setting.json subtitle.outline_color      / sub_outline_color  / comment_outline_color    … 縁 (ASS &HAABBGGRR)
      ↓ build_font_profile()
FontProfile.role_colors / role_outline_colors
      ↓ iter_role_styles()
ASS の [V4+ Styles] に Streamer / Sub / Comment の 3 スタイルを出力
      ↓
Dialogue 行は entry["role"] に対応する Style 名を参照する
```

* **クリップ 1 件ごとの色を持つ場所がモデルに無い**（`SubtitleClip` は
  `text/role/font/font_size/use/z_order/transform` のみ / `model.py:272`）。
* 一方 **フォントとサイズは既に個別指定できている**。仕組みは
  `_inline_overrides()`（`subtitle_generator.py:536`）が Dialogue 本文の先頭へ
  `{\fnフォント\fs48}` を前置する方式。**色も同じ仕組みに乗せられる**
  （ASS のインライン上書きタグ `\1c`（塗り）/ `\3c`（縁）/ `\3a`（縁の透明度））。
* インスペクタ（`timeline_editor_dialog.py:407 _InspectorPanel`）には
  字幕・役割・フォント・サイズの 4 欄がある。**ここに 2 行足すのが素直**。
* 設定画面には既にカラーピッカー一式がある（`settings_window._make_color_picker` /
  `_choose_color` / `_parse_color` / `_format_color` / `_update_swatch`）。
  ただし **`SettingsWindow` のメソッドとして閉じている**ため、そのままでは他画面から使えない。

### 2.1.1 複数選択は既にできている（B8 の下地）

一括適用（B8）のために新しい選択の仕組みを作る必要は無い。**選択は既に複数対応**である。

| 経路 | 実装 |
|---|---|
| Timeline 上で **Ctrl+クリック** | `timeline_view.py:617` → `controller.toggle_select()` |
| プレビュー上の **ラバーバンド選択** | `preview_panel.py:439 _on_scene_selection_changed` → `controller.select(ids)` |
| 保持 | `TimelineController._selected_ids`（list）/ `selected_ids()` |

* 足りないのは **UI 側だけ**。`selection_changed` は `selected_clip()`（＝**先頭 1 件**）しか渡さず、
  インスペクタもその 1 件しか見ていない（`timeline_editor_dialog.py:286 / 489`）。
* Undo は **スナップショット方式**（`commands.py:50 _snapshot`）のため、
  **1 つのコマンドで N 件変更すれば履歴も 1 手**になる。逆操作を書く必要も無い（§5.10）。

### 2.2 再生が遅い理由（B3）── 実測で 2 つに切り分けた

「▶ を押してから音が出るまで（起動待ち）」と「再生中の映像の追従（毎フレーム）」は
別の原因で、**効き目が大きいのは後者**だった。

#### (a) 映像 ── 1 フレームごとに seek し直している

`PyAvFrameSource._decode_at()`（`frame_source.py:95`）は要求のたびに
`container.seek(...)` → 直前のキーフレームから目的時刻までデコード、を毎回行う。
再生は `play_fps=15` で 1 秒に 15 回この要求を出すため、**毎回 GOP 先頭から読み直す**。

| 方式 | 1 枚あたり | 出せる fps |
|---|---:|---:|
| **現行（毎回 seek + rgb24 1920x1080）** | **134 ms** | 7.5 |
| 前進デコード（seek せず続きを読む） | **9〜11 ms** | 90〜110 |
| 前進デコード + 960x540 へ縮小 | 12 ms | 83 |
| 前進デコード（RGB 化なし＝デコードのみ） | 6 ms | 166 |
| 前進デコード + `skip_frame=NONREF` | 8 ms | 125 |
| ffmpeg フォールバック（1 枚ごとにプロセス起動） | **186 ms** | 5.4 |
| 常駐 ffmpeg パイプ 1920x1080 | 11.7 ms（初回 164 ms） | 85 |
| 常駐 ffmpeg パイプ 960x540 | 8.7 ms（初回 153 ms） | 115 |

> **これが「なかなか再生されない」の主因。** 要求 15 fps に対し供給 7.5 fps しか無く、
> フレームワーカーは常に飽和する。ワーカーは最新要求だけを処理する作りのため
> 画面は「止まったように見えて時々飛ぶ」動きになり、
> さらに GIL の奪い合いで GUI スレッド（音声位置の処理・オーバーレイ再構築）まで巻き込む。
> **前進デコードにするだけで 134 ms → 9 ms（約 15 倍）**になる。

* 縮小は**デコード費用を下げない**（デコード後の sws 変換が増えるぶん、むしろ 9→12 ms）。
  効くのは Qt 側（後述 (c)）で、本機・本素材では差し引きほぼ中立。**主因の対策は R1 であり、
  「画質を落とす」は主因の対策ではない**。ただし重い素材・遅い機体では Qt 側の費用が
  画素数に比例するため効く。回答 Q6 により **既定で有効**にする（§3-6 R6）。
* PyAV が使えない環境では **1 枚 186 ms** で、再生は原理的に成立しない。
  常駐パイプ方式なら 8.7〜11.7 ms で成立する（§3-8）。

#### (b) 音声 ── 断片ごとに ffmpeg を起動している

`AudioChunkSource.build()`（`audio_source.py:75`）は、チャンクを構成する断片ごとに
ffmpeg を 1 本ずつ起動し、最後に concat する。無音カット後の Timeline は
**1 クリップ 1〜3 秒**が並ぶため、既定の 30 秒チャンクで断片は 15〜40 本になる。

| 方式（30 秒ぶん） | 断片 15 本 | 断片 40 本 |
|---|---:|---:|
| **現行（断片ごとに ffmpeg + concat）** | **2.13〜2.26 s** | 約 6 s（外挿） |
| 1 入力 + `atrim` で一括（**採らない**） | **7.62 s** | ─ |
| **断片ごとに `-ss` 入力 + `concat` フィルタ（1 プロセス）** | **0.97 s** | ─ |
| 同上・24kHz mono aac | 0.64 s | 1.61 s（48k stereo aac） |
| 同上・24kHz mono wav | **0.42 s** | **0.76 s** |
| 起動用 6 秒だけ先に作る（断片 3 本・aac） | 0.25 s | ─ |
| 起動用 6 秒だけ先に作る（断片 3 本・wav） | **0.10 s** | ─ |

* `atrim` 一括（1 入力）は **素材の先頭からデコードするため逆に遅い**（800 秒地点で 7.62 s）。
  1 プロセス化するなら **断片ごとに `-ss` 付きの入力を並べる**方式でなければならない。
* さらに 2 つ、待ちを増やしている作りがある:
  * **編集のたびに全部捨てている**。`_on_timeline_changed → invalidate()` で生成済みを破棄し、
    チャンクは 1 本しか持たない（`_drop_current_chunk`）。**少し戻して再生し直すたびに作り直す**。
  * **チャンクの終わり際でキャッシュが当たらない**。`_play_forward` は
    `cached(start, audio_chunk_sec=30)` と 30 秒ぶんの被覆を要求するため、
    チャンク終端の 5 秒手前で押すと **残り 5 秒は手元にあるのに全部作り直す**。
    実際には終端で `EndOfMedia` → 次チャンクへ継ぐ経路がある（`_on_audio_status`）ので、
    起動時に 30 秒ぶんの被覆を要求する必要が無い。
* アーカイブ用は `ArchiveTimelineDialog.__init__` が `asr_audio_path` を渡していない
  （`archive_timeline_dialog.py:64`）ため、**認識用音声の再利用（初回だけ待ち時間ゼロ）も効かない**。

#### (c) 表示 ── フレームごとの Qt 変換と、オーバーレイの毎回作り直し

| 処理 | 実測 |
|---|---:|
| QImage 化 + QPixmap 化（1920x1080） | 3.7 ms |
| 同（960x540） | 1.3 ms |
| **960x540 を 1920x1080 へ拡大（FastTransformation）** | **4.5 ms** |

* `_on_frame_ready`（`preview_panel.py:368`）は素材寸法がキャンバスと違うときだけ
  `SmoothTransformation` で拡縮する。**縮小フレームを渡すとここで拡大され、かえって重くなる**。
  縮小を入れるなら **Qt では拡大せず、アイテム側の `setScale()` で見せる**必要がある（§3-6）。
* `_rebuild_overlays()`（同 387）は再生ヘッドが動くたびに
  **全オーバーレイを削除して作り直す**。字幕だけなら軽いが、
  **映像オーバーレイ（V2 以降）があると `_overlay_pixmap()` が GUI スレッドで
  `frame_source.frame_at()` を直接呼ぶ**（同 428）。現行の 134 ms がそのまま
  GUI スレッドの停止時間になる（前進デコード化後も 1 枚ぶんは残る）。

### 2.3 使われていない設定キー

`timeline.preview.width`（既定 960）と `timeline.preview.update_debounce_ms`（既定 60）は
`builder.timeline_config()` で正規化されるだけで **どこからも参照されていない**。
本件で `width` は「再生中の縮小幅」として意味を与える案があるが、
**既存キーの意味を後から変えると既定値 960 が黙って効き始める**ため採らない（§3-6 / §7）。

### 2.4 設定キー補完の抜け（今回ついでに直す）

resolve5 で足した `timeline.ui.archive_graph_dock_area` / `archive_graph_dock_height_px` /
`archive_clip_selector_width_px` / `archive_dock_state` は **`setting.json` にはあるが
`settings_window.DEFAULT_SETTINGS` に無い**。`_fill_timeline_nested_defaults()` は
DEFAULT_SETTINGS を基準に補完するため、**旧 setting.json を持つ利用者のファイルには
これらのキーが現れない**（動作は `timeline_config()` の既定で成立している）。
今回追加するキーは **DEFAULT_SETTINGS と setting.json の両方**へ入れ、
ついでに上記 4 キーも DEFAULT_SETTINGS へ追記する。

---

## 3. 方式選定

### 3-1.【B1/B2】色は「クリップ個別の上書き」として持つ ── 役割 Style は残す

**案A（採用）: `SubtitleClip` に `color` / `outline_color` を足し、ASS のインラインタグで上書きする**

```
Style（役割ごと・現行のまま）    ← フォント/サイズ/太さ/配置/余白/縁の太さ/背景 と「既定の色」
  ＋
Dialogue 本文先頭のインラインタグ ← このクリップだけの色（指定があるときだけ）
```

* 既にフォント・サイズ・位置が同じ仕組みで動いており、**新しい概念を持ち込まない**。
* 空文字 = 未指定 = **役割の色に従う**。全クリップ未指定なら
  **出力される ASS は現行と 1 バイトも変わらない**（`_inline_overrides` が空文字を返す）。
* Undo/Redo は既存 `EditSubtitle`（`commands.py:795`）が `setattr` の汎用実装のため **無改造で効く**。

**案B（採らない）: 色ごとに Style を増やす**
… 使われた色の数だけ `[V4+ Styles]` を動的生成する。ASS としては正統だが、
Style 名の採番・重複排除・Resolve 出力側の対応が増え、得るものが無い。

**案C（採らない）: 役割を増やす**
… 「役割」は色以外（コメントラベル付与など）も担っており、色のために増やすと意味が濁る。

### 3-2. 色の保存形式 ── 塗りは `#RRGGBB` / 縁は `&HAABBGGRR`

**設定画面と同じ形式に揃える**（`settings_window` の `with_alpha` の区別と一致）。

| 項目 | 形式 | 例 | 理由 |
|---|---|---|---|
| 文字色 `color` | HTML `#RRGGBB` | `#FFE24B` | `subtitle.own_subtitle_color` と同形式。透明度は持たない |
| 縁の色 `outline_color` | ASS `&HAABBGGRR` | `&H00202020` | `subtitle.outline_color` と同形式。**透明度を持てる** |
| 未指定 | `""`（空文字） | | 役割の色に従う |

* 既存の変換関数をそのまま使える: `_hex_to_ass_color()` / `_safe_ass_color()`
  （`subtitle_generator.py:57 / 42`）、`ass_color_to_qcolor()`（`preview_items.py:28`）。
* JSON へは文字列としてそのまま載る（`SubtitleClip.to_dict`）。

### 3-3.【B4】ASS への通し方 ── インラインタグを `_inline_overrides` へ足す

ASS の上書きタグ（libass 準拠）:

| 対象 | タグ | 値 |
|---|---|---|
| 塗り | `\1c&HBBGGRR&` | RGB を **BGR 並び**にした 6 桁。末尾の `&` が要る |
| 縁 | `\3c&HBBGGRR&` | 同上 |
| 縁の透明度 | `\3a&HAA&` | ASS の α（`00`=不透明 / `FF`=透明） |

出力例（位置 → フォント → サイズ → 色 の順に前置する）:

```
Dialogue: 0,0:00:01.00,0:00:03.00,Streamer,,0,0,0,,{\fnKosugi Maru\fs64\1c&H4BE2FF&\3c&H202020&\3a&H00&}こんばんは
```

* **順序は「位置 → フォント → サイズ → 塗り → 縁」に固定**する。ASS の上書きタグは
  同一ブロック内なら順不同だが、ログ・差分・目視確認のため決めておく。
* `outline_color` が 8 桁（`&HAABBGGRR`）のときは `\3c` と `\3a` を**必ず対で**出す。
  6 桁（`&HBBGGRR`）なら `\3c` のみ（Style の α を引き継ぐ）。
* 縁の**太さ**（`Outline`）は Style の値のまま。今回は色だけを対象とする（§9 Q3）。

### 3-4.【B5】プレビュー近似描画への通し方

`SubtitleOverlayItem.__init__`（`preview_items.py:136`）は
`font_profile.role_colors` / `role_outline_colors` から色を引いている。
ここを **「クリップの指定があればそちら、無ければ役割の色」**に変えるだけ（2 行）。

```python
# クリップ個別の色があれば優先する (resolve6 §3-4)。空文字なら役割の色に従う。
fill = clip.color or font_profile.role_colors.get(role, font_profile.color_hex)
outline = clip.outline_color or font_profile.role_outline_colors.get(
    role, font_profile.outline_color)
```

* `ass_color_to_qcolor()` は `#RRGGBB` と `&H…` の両方を解釈できる（実装済み）ため
  変換の追加は不要。
* 「高精度プレビュー」（`_show_high_quality`）は `clip.to_item()` → `build_subtitle_file` を通るため
  **本番と同じ色**が出る（§3-3 の変更が自動的に効く）。

### 3-5.【B6】Resolve 出力への通し方

`_title_from_item` / `_caption_from_item`（`resolve_export.py:182 / 226`）は
`font_profile.role_colors` から色を引いている。**item 個別指定を優先**へ変える
（`font` / `font_size` が既にそうなっているのと同じ形）。

```python
"color": item.get("color") or font_profile.role_colors.get(role, font_profile.color_hex),
```

縁（title のみ）は `_ass_color_to_hex_alpha()` に **item の値を優先して**渡す。
SRT には色の概念が無いため変更しない。

### 3-6.【B3】再生の改善 ── 効き目の順に 5 段構え

| # | 施策 | 実測での効き目 | 副作用 |
|---|---|---|---|
| **R1** | **前進デコード**（連続再生では seek しない） | 134 → 9〜11 ms/枚 | 無し（後方・遠方への移動は従来どおり seek） |
| **R2** | **音声チャンクを 1 プロセス化**（断片ごとに `-ss` 入力 + `concat` フィルタ） | 2.26 → 0.97 s | 無し |
| **R3** | **起動用の短いチャンク**（先に数秒だけ作って鳴らし、続きは先読み） | 0.97 → 0.10〜0.25 s | 先読みが間に合わないと継ぎ目で途切れる（§3-7 で抑える） |
| **R4** | **プレビュー音声の品質を落とす**（24kHz mono / wav） | 0.97 → 0.42 s | 音は劣化する（**編集用の確認音のため許容**／設定で戻せる） |
| **R5** | **チャンクを複数持つ**（LRU）・被覆判定を緩める | 「少し戻して再生」の待ちがゼロに | 一時ファイルが数本増える（終了時に削除） |

「画質を落としてでも」への直接の回答は **R6・R7**。回答 Q6 により **R6 は既定で有効**にする。

| # | 施策 | 実測 | 既定 |
|---|---|---|---|
| **R6** | **再生中だけ**映像を縮小する（`playback_width`）。**Qt では拡大せず `setScale()` で見せる** | Qt 側 3.7 → 1.3 ms／デコード側は 9 → 12 ms | **`960`（有効・回答 Q6）** |
| **R7** | 再生中だけ `skip_frame=NONREF`（参照されないフレームを捨てる） | 11 → 8 ms/枚 | `"none"`（無効） |

**R6 を既定 ON にするにあたっての注意（実測に基づく）**

* 本機・本素材（1080p60 H.264）では **差し引きほぼ中立**（Qt で −2.4 ms、デコードで +3 ms）。
  効くのは **重い素材（4K・高ビットレート）と遅い機体**で、そこでは Qt 側の費用が
  画素数に比例して増えるため縮小がそのまま効く。**利用者の指示により既定 ON とする**。
* **`_on_frame_ready` の拡大処理を必ず外すこと**（§5.8）。現状の作りのまま縮小フレームを渡すと
  4.5 ms の拡大が増えて **縮小した意味が消えるどころか逆効果**になる（§2.2(c)）。
* **縮小するのは再生中だけ**。停止・スクラブ中は原寸で取得するため、
  **止めた瞬間に鮮明な絵へ戻る**（字幕位置の微調整など、精度が要る操作は原寸で行える）。
* 縦動画（1080x1920）でも `playback_width` は**幅**の指定として扱い、高さは縦横比から求める。
  素材の幅がこの値以下なら縮小しない（拡大は決してしない）。

### 3-7. 前進デコードの規則（R1 の詳細）

`PyAvFrameSource` に「直前にどこまで読んだか」を持たせ、次の要求が
**同じ素材の少し先**なら seek せず続きを読む。

```
要求 (media, t)
 ├ キャッシュに在る                     → そのまま返す（現行どおり）
 ├ 同じ素材 かつ 0 <= t - 直前位置 <= sequential_decode_sec
 │                                      → seek せず decode を継続（前進デコード）
 └ それ以外（別素材 / 後戻り / 遠くへ跳ぶ）→ 現行どおり seek してから読む
```

* `sequential_decode_sec`（既定 2.0 秒）は「seek するより読み進めた方が速い距離」。
  60fps で 2 秒 = 120 枚 ≒ 0.7 s ぶんのデコードで、キーフレーム seek（実測 134 ms）より
  得になる範囲を素直に取った値。設定で変えられる（§7）。
* デコード用ジェネレータ（`container.decode(stream)`）を素材ごとに保持する。
  seek したら作り直す。素材を閉じるときに捨てる。
* 倍速再生（`playback_rate=2.0`）でも 1 ティックあたりの前進は
  `2.0 / 15 = 0.13 秒`のため同じ経路に乗る。倍速逆再生は後戻りのため従来どおり seek。
* **スクラブ（ドラッグ）は今までどおり seek**。前進デコードは連続再生のための最適化で、
  飛び回る操作には効かない（効かせようとすると誤って長い前進デコードを踏む）。

### 3-8. PyAV が無い環境（R8 / 任意）

`FfmpegFrameSource` は 1 枚 186 ms で、**再生には使えない**。
`frame_pipe_fallback=true`（既定）のとき、**再生中だけ**常駐 ffmpeg を 1 本立て、
`fps=<play_fps>` で rawvideo を標準出力へ流させて順に読む（実測 8.7〜11.7 ms/枚・初回 153〜164 ms）。

```
再生開始 → ffmpeg -ss <開始> -i <素材> -vf fps=15,scale=W:-2 -f rawvideo -pix_fmt rgb24 -
           を 1 本起動し、フレームを順に読む
再生停止 / 再生ヘッドの手動移動 / 素材の切り替わり → プロセスを落として従来の 1 枚取得へ戻す
```

* Timeline のクリップ境界をまたぐたびにプロセスを張り替える必要があるため、
  **R1〜R5 とは独立した後段のフェーズ**にする（§6 Phase 5）。
* PyAV が使える環境では一切通らない。

### 3-9. オーバーレイ再構築の抑制（R9）

`_rebuild_overlays()` を「毎回作り直し」から「**差分更新**」へ変える。

* 再生ヘッド位置で表示すべき要素の ID 集合を作り、**前回と同じなら作り直さない**
  （字幕は表示中ずっと同じ要素なので、ほとんどのティックで何もしなくなる）。
* 映像オーバーレイだけは中身（フレーム）が毎ティック変わるが、
  **再生中は更新しない**（`playing` 中は最後に取得した絵のまま）とする。
  GUI スレッドでの同期デコード（§2.2(c)）を再生中に踏まないための割り切り。
  停止・スクラブ時は従来どおり毎回更新する。
* 本件は **映像オーバーレイを載せていない通常の編集では体感差が出ない**ため、
  R1〜R5 の後に回す（§6 Phase 4）。

### 3-10.【B8】複数選択した字幕へまとめて適用する（回答 Q1）

選択の仕組みは既にある（§2.1.1）ので、**「複数件を 1 コマンドで変える」入口を足すだけ**でよい。

```
Timeline で Ctrl+クリック / プレビューでラバーバンド選択
   → controller.selected_ids() = [s3, s7, s12]
インスペクタで色を確定
   → controller.edit_subtitles([s3, s7, s12], color="#FFE24B")
        → commands.EditSubtitles … 1 回の apply() で 3 件を書き換える
             → スナップショット方式なので Undo は 1 手で 3 件とも戻る
```

**適用範囲の規則**

| 項目 | 対象 | 理由 |
|---|---|---|
| **文字色 / 縁の色** | **選択中の字幕クリップすべて** | 本要望（B8） |
| 字幕テキスト | 先頭 1 件のみ（現行どおり） | 文面はクリップごとに違う。まとめて同じ文にする操作は事故になる |
| 役割 / フォント / サイズ | 先頭 1 件のみ（現行どおり） | 要望の範囲外。既存挙動を変えない（§9 Q8 で拡張の要否を確認） |

* **選択に字幕以外（映像クリップ・音声クリップ）が混ざっていても落ちない**。
  `EditSubtitles` は `SubtitleClip` 以外を黙って読み飛ばす（既存 `EditSubtitle` と同じ判定）。
* 選択した字幕の色が**バラバラのとき**、インスペクタの表示は
  **先頭 1 件の値**とし、見出しに「3 件選択」を出す（§5.5）。
  「複数値」を表す特別な状態は持たない ── 一括適用は「今出ている値で塗り直す」操作として
  一貫させ、押すまで何も変わらないため誤操作にならない。
* Undo ラベルは件数を含める（例:「字幕の編集（3 件）」）。何が戻るのか押す前に分かるようにする。

---

## 4. 設計方針

1. **既定の出力を変えない。** 色を一度も指定しなければ ASS も FCPXML も現行と同一。
2. **設定でいつでも切り戻せる。** 音声の 1 プロセス化・前進デコード・縮小・skip_frame は
   すべて `setting.json` のキーで従来動作へ戻せる（§7）。
3. **ハードコードしない**（`docs/CLAUDE.md`）。しきい値・品質・寸法はすべて設定値にする。
4. **クリップ用とアーカイブ用で作り分けない。** インスペクタも再生も共通部への変更で両方に効く。
5. **失敗しても編集を止めない。** 音声生成の失敗は現行どおり無音再生へ倒す。
   前進デコードで目的フレームに届かなかったら seek してやり直す。

---

## 5. 詳細設計

### 5.1 新規・変更ファイル一覧（予定）

| 種別 | ファイル | 内容 | 要件 |
|---|---|---|---|
| 新規 | `src/gui/color_field.py` | 色の入力欄 + 色見本ボタン + 「既定へ戻す」をまとめた小部品と、`#RRGGBB` ⇄ `&HAABBGGRR` の変換関数 | B1/B2/B7 |
| 変更 | `src/timeline/model.py` | `SubtitleClip` へ `color` / `outline_color` を追加（`copy`/`to_dict`/`from_dict`/`to_item`） | B1/B2 |
| 変更 | `src/timeline/commands.py` | `EditSubtitles`（複数件を 1 コマンドで変更）を追加。`EditSubtitle` は残す | B8 |
| 変更 | `src/gui/timeline/timeline_controller.py` | `edit_subtitles(clip_ids, **fields)` を追加 | B8 |
| 変更 | `src/modules/subtitle_generator.py` | `_inline_overrides()` に色タグを追加（`_inline_color_override()` を新設） | B4 |
| 変更 | `src/gui/timeline/preview_items.py` | `SubtitleOverlayItem` がクリップの色を優先 | B5 |
| 変更 | `src/export/resolve_export.py` | `_title_from_item` / `_caption_from_item` が item の色を優先 | B6 |
| 変更 | `src/gui/timeline/timeline_editor_dialog.py` | インスペクタに「文字色」「縁の色」を追加／複数選択時の見出しと一括適用 | B1/B2/B7/B8 |
| 変更 | `src/settings/settings_window.py` | `_parse_color`/`_format_color` を `color_field` へ委譲（挙動不変）／DEFAULT_SETTINGS へ新キー | B1/B3 |
| 変更 | `src/timeline/frame_source.py` | 前進デコード（R1）／縮小（R6）／`skip_frame`（R7）／常駐パイプ（R8・Phase 5） | B3 |
| 変更 | `src/timeline/audio_source.py` | 1 プロセス化（R2）／起動用チャンク（R3）／品質設定（R4）／チャンク LRU（R5） | B3 |
| 変更 | `src/gui/timeline/preview_panel.py` | 起動用チャンクの要求・被覆判定の緩和・縮小フレームの表示（R3/R5/R6）／オーバーレイ差分更新（R9） | B3 |
| 変更 | `src/gui/timeline/archive_timeline_dialog.py` | 認識用音声を再利用できるようにする（§5.9） | B3 |
| 変更 | `src/timeline/builder.py`（`timeline_config` のみ） | 新キーの既定補完。`build()` 本体は**変更しない** | B3 |
| 変更 | `src/settings/setting.json` | §7 の追加キー | B1〜B3 |

### 5.2 モデル（`SubtitleClip`）

```python
class SubtitleClip:

    def __init__(self, clip_id, timeline_start, duration, text="", role=DEFAULT_ROLE,
                 font="", font_size=None, use=True, z_order=DEFAULT_SUBTITLE_Z_ORDER,
                 transform=None, origin=None, color="", outline_color=""):
        …
        # このクリップだけの文字色 (HTML #RRGGBB)。空文字 = 役割の色に従う (resolve6 §3-2)
        self.color = str(color or "")
        # このクリップだけの縁の色 (ASS &HAABBGGRR)。空文字 = 役割の色に従う
        self.outline_color = str(outline_color or "")
```

* `copy()` / `to_dict()` / `from_dict()` へ 2 項目を追加する。
* `to_item()` は **指定があるときだけキーを足す**（`pos_x`/`pos_y` と同じ扱い）。
  これにより未指定時の item 辞書は現行と完全に同一になる。

```python
    def to_item(self):
        item = {…現行どおり…}
        if self.transform.is_positioned():
            item["pos_x"], item["pos_y"] = self.transform.x, self.transform.y
        # 個別指定があるときだけ載せる (未指定なら現行と同一の辞書 / resolve6 §5.2)
        if self.color:
            item["color"] = self.color
        if self.outline_color:
            item["outline_color"] = self.outline_color
        return item
```

* `project_io` は `Track.to_dict()`／`from_dict()` 経由のため**変更不要**。
  `schema_version` は **1 のまま据え置く**。増えたキーは欠落時に空文字へ落ちるので、
  旧ファイルを読んでも壊れない（`migrate()` の追加も不要）。

### 5.3 ASS 生成（`subtitle_generator`）

```python
# ASS 色文字列 (&HAABBGGRR / &HBBGGRR) を (BBGGRR, AA) へ分解する
# AA が無い形式では alpha に None を返す (Style の透明度を引き継ぐ)
def _split_ass_color(value):
    text = str(value or "").strip().upper()
    if not text.startswith("&H"):
        return None, None
    digits = text[2:].rstrip("&")
    if len(digits) == 8:
        return digits[2:], digits[0:2]
    if len(digits) == 6:
        return digits, None
    return None, None


# 1 エントリぶんの色上書きタグを返す (塗り → 縁 の順 / resolve6 §3-3)
# entry["color"]        : HTML #RRGGBB (塗り)
# entry["outline_color"]: ASS &HAABBGGRR (縁。透明度を持てる)
# どちらも未指定なら空文字を返す = 既存の生成結果と完全に一致する。
def _inline_color_override(entry):
    override = ""
    color_hex = entry.get("color", "")
    if color_hex:
        # _hex_to_ass_color は "&H00BBGGRR" を返すため、先頭の AA を落として \1c 形式にする
        override += f"\\1c&H{_hex_to_ass_color(color_hex)[4:]}&"
    bgr, alpha = _split_ass_color(entry.get("outline_color", ""))
    if bgr:
        override += f"\\3c&H{bgr}&"
        if alpha is not None:
            override += f"\\3a&H{alpha}&"
    return override
```

`_inline_overrides()` の末尾へ連結する（順序は §3-3 のとおり）:

```python
def _inline_overrides(entry, video_width, video_height):
    position = _inline_position_override(entry, video_width, video_height)
    override = ""
    …フォント・サイズ（現行どおり）…
    override += _inline_color_override(entry)     # ← 追加
    if not position and not override:
        return ""
    return "{" + position + override + "}"
```

* `build_subtitle_file()` の INFO ログへ **色の個別指定件数**も足す
  （現行のフォント／サイズ件数と同じ形式 / `subtitle_generator.py:606`）。

### 5.4 共有部品（`src/gui/color_field.py` 新規）

設定画面のカラーピッカーはロジックが `SettingsWindow` のメソッドに閉じている。
**同じ挙動の関数をモジュール関数として切り出し、設定画面はそれを呼ぶだけにする**
（＝設定画面の見た目・挙動は変わらない）。

```python
# 色文字列 ⇄ QColor の変換 (設定画面と Timeline インスペクタで共有する / resolve6 §5.4)
# with_alpha=True : ASS &HAABBGGRR (BGR 並び・α 反転) / False: HTML #RRGGBB
def parse_color(text, with_alpha, fallback=None): …
def format_color(color, with_alpha): …


# 「色見本ボタン + 入力欄 + 既定へ戻す」を 1 つにまとめた小部品
# 値が空文字のときは「既定 (役割の色)」を表し、色見本は淡いハッチで示す。
class ColorField(QWidget):

    # 利用者が確定したときだけ出す (Undo 履歴を 1 打鍵ごとに埋めないため)
    color_committed = Signal(str)

    def __init__(self, with_alpha, swatch_width_px, parent=None): …
    def set_value(self, text): …       # 画面から値を流し込む (シグナルは出さない)
    def value(self): …                 # 現在値 ("" = 既定)
```

* `settings_window._parse_color` / `_format_color` / `_update_swatch` は
  **本体を `color_field` の関数へ委譲する**（メソッドは残す＝呼び出し側は無変更）。
* 色見本の枠線色をテーマから引く挙動（`settings_window.py:1552`）はそのまま移す。

### 5.5 インスペクタ（`_InspectorPanel`）

```
┌ インスペクタ (1 件選択) ─────┐   ┌ インスペクタ (複数選択) ─────┐
│ 字幕クリップ                 │   │ 字幕クリップ（3 件選択）      │ ← 見出しに件数 (B8)
│ s12 / 開始 0:00:12.30 / 尺 2.10 秒│   │ s3 ほか 2 件 / 先頭 0:00:04.10 │
│ 字幕   [ こんばんは        ] │   │ 字幕   [ こんばんは        ] │ ← 先頭 1 件だけに効く
│ 役割   [ 配信者          ▼] │   │ 役割   [ 配信者          ▼] │ ← 同上
│ フォント[ (設定のフォント) ▼] │   │ フォント[ (設定のフォント) ▼] │ ← 同上
│ サイズ [ 既定             ] │   │ サイズ [ 既定             ] │ ← 同上
│ 文字色 [■][ #FFE24B ][既定へ] │   │ 文字色 [■][ #FFE24B ][既定へ] │ ← 選択中 3 件へ一括 (B8)
│ 縁の色 [■][ &H00202020 ][既定へ]│   │ 縁の色 [■][ &H00202020 ][既定へ]│ ← 同上
│ [位置を既定へ戻す]           │   │ ※ 色は選択中の 3 件すべてに適用 │ ← 案内 (誤解防止)
└──────────────────┘   └──────────────────┘
```

```python
        # 文字色 (塗り)。空欄 = 役割の色に従う (resolve6 §3-1)
        self.color_field = ColorField(
            with_alpha=False,
            swatch_width_px=int(ui_cfg["subtitle_color_swatch_width_px"]))
        self.color_field.setToolTip(
            "選択中の字幕の文字色です。空欄にすると設定の役割色に戻ります\n"
            "複数選択しているときは選択中すべてに適用されます")
        self.color_field.color_committed.connect(self._on_color_changed)
        form.addRow("文字色", self.color_field)

        # 縁 (アウトライン) の色。ASS 形式のため透明度も指定できる
        self.outline_field = ColorField(
            with_alpha=True,
            swatch_width_px=int(ui_cfg["subtitle_color_swatch_width_px"]))
        self.outline_field.color_committed.connect(self._on_outline_color_changed)
        form.addRow("縁の色", self.outline_field)
```

色は **選択中の字幕クリップすべて**へ適用する（B8 / §3-10）。

```python
    # 選択中の字幕クリップ ID を並び順で返す (色の一括適用の対象 / resolve6 §3-10)
    # 字幕以外 (映像・音声クリップ) が混ざっていても落ちないよう、ここで振り分ける。
    def _selected_subtitle_ids(self):
        timeline = self._controller.timeline
        ids = [i for i in self._controller.selected_ids()
               if isinstance(timeline.clip_by_id(i), SubtitleClip)]
        # 選択が空 = プレビュー側だけで選んでいる場合に備え、表示中のクリップへ倒す
        if not ids and isinstance(self._clip, SubtitleClip):
            ids = [self._clip.id]
        return ids

    def _on_color_changed(self, value):
        if self._updating:
            return
        self._controller.edit_subtitles(self._selected_subtitle_ids(), color=value)

    def _on_outline_color_changed(self, value):
        if self._updating:
            return
        self._controller.edit_subtitles(
            self._selected_subtitle_ids(), outline_color=value)
```

* `show_clip()` で `self.color_field.set_value(clip.color)` / `outline_field.set_value(clip.outline_color)`
  を `_updating` ガードの中で行う（現行のフォント・サイズと同じ）。**表示するのは先頭 1 件の値**。
* 見出し・案内の更新も `show_clip()` で行う。件数は `_selected_subtitle_ids()` の長さから出す。
  1 件のときは現行と同じ表示（「字幕クリップ」＋案内なし）にして、**単一選択の見た目を変えない**。
* `selection_changed` は先頭 1 件しか運ばないが、**件数は `controller.selected_ids()` から
  その場で引ける**ためシグナルの変更は不要（`selection_changed` の引数を変えると
  既存の接続先すべてに影響するため触らない）。
* `edit_subtitles` → `EditSubtitles` → `timeline_changed` → プレビュー再描画、で
  **その場で色が変わって見える**（B5 は §3-4 の 2 行で成立する）。
* `ColorField` の入力欄は `QLineEdit` のため、**ショートカット無効化（`_ShortcutGuard`）が
  そのまま効く**（`timeline_editor_dialog.py:370` の `_EDITORS` に `QLineEdit` が入っている）。

### 5.6 音声チャンク生成（`audio_source`）

#### (1) 1 プロセス化（R2）

```python
# 断片列を 1 回の ffmpeg 実行で書き出す (resolve6 §3-6 R2)
# 断片ごとに -ss/-t 付きの入力を並べ、concat フィルタで 1 本へ繋ぐ。
# 【重要】1 入力 + atrim にしてはならない。入力シークが効かず素材の先頭から
#         デコードするため、後方の区間では逆に数倍遅くなる (実測 7.62s / §2.2(b))。
def _render_pieces(self, pieces, out_path):
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    labels = []
    for index, piece in enumerate(pieces):
        if piece["kind"] == "silence":
            cmd += ["-f", "lavfi", "-t", f"{piece['duration']:.3f}",
                    "-i", f"anullsrc=channel_layout=stereo:sample_rate={rate}"]
        else:
            cmd += ["-ss", f"{piece['source_in']:.3f}", "-t", f"{piece['duration']:.3f}",
                    "-i", piece["path"]]
        labels.append(f"[{index}:a]")
    graph = "".join(labels) + f"concat=n={len(pieces)}:v=0:a=1[cat]"
    # 断片ごとのゲインは concat の前に掛ける (指定がある断片だけ volume を挟む)
    …
    cmd += ["-filter_complex", graph, "-map", "[out]",
            "-c:a", codec, "-ar", str(rate), "-ac", str(channels), out_path]
    ffmpeg_runner.execute(cmd, progress_timeout_sec=…)
```

* ゲイン（`gain_db`）がある断片は、その入力のラベルへ `volume` を挟んでから concat する。
* `audio_build_mode="per_piece"` のときは**現行の実装をそのまま使う**（切り戻し用）。
* 断片が 1 本だけのときは concat フィルタを使わず現行の単純な抽出にする（最速）。

#### (2) 起動用チャンク（R3）と被覆判定の緩和

```
▶ 押下
 ├ 既存チャンクで start をまかなえる            → 即再生（現行どおり）
 └ まかなえない
     ├ 起動用チャンク (audio_startup_chunk_sec 秒) を生成 → 再生開始   … 実測 0.10〜0.25 s
     └ 直後に本チャンク (audio_chunk_sec 秒) を先読み生成 → 継ぎ目で切り替え
```

* `PreviewPanel._play_forward()` の被覆要求を
  `cached(start, audio_chunk_sec)` → **`cached(start, audio_startup_chunk_sec)`** に緩める。
  終端まで再生したら現行の `EndOfMedia` 経路が次チャンクへ継ぐ（`preview_panel.py:670`）。
* 起動用チャンクは**続けて本チャンクを先読みする**ため、`_request_chunk(autoplay=False)` を
  起動用の完了直後に 1 本投げる。

#### (3) 品質（R4）とキャッシュ（R5）

* 出力は `preview.audio_codec` / `audio_sample_rate` / `audio_channels` に従う
  （既定 = 24kHz mono `pcm_s16le`）。空文字・0 のときは `ffmpeg` セクションの値を使う
  （＝現行と同じ 48kHz stereo aac へ戻せる）。
* チャンクは 1 本ではなく **`audio_chunk_cache` 本（既定 3）まで保持**する。
  古いものから削除する。`invalidate()` は従来どおり全部捨てる。

### 5.7 フレーム取得（`frame_source`）

```python
class PyAvFrameSource(FrameSource):

    # cfg で挙動が変わる (resolve6 §3-7 / R1・R6・R7):
    #   sequential_decode_sec : この秒数以内の前進なら seek せず読み進める
    #   playback_width        : 再生中この幅へ縮小して返す (0 = 原寸 / 既定 960)
    #   playback_skip_frame   : "nonref"/"bidir" で再生中フレームを間引く
    def __init__(self, cache_frames=8, sequential_decode_sec=2.0,
                 playback_width=960, playback_skip_frame="none"): …

    # 再生の開始・終了を伝える (再生中だけ画質を落とすため)
    def set_playing(self, playing): …
```

* 素材ごとに `(container, stream, decoder, last_pts_sec)` を保持する。
* `frame_at()` の分岐は §3-7 の 3 経路。前進デコードで
  **目的時刻へ届かないままストリームが尽きたら seek へフォールバック**する。
* `playback_width > 0` かつ再生中のときだけ `frame.reformat(width=…, height=…, format="rgb24")`。
  高さは縦横比から偶数へ丸める。**素材の幅が `playback_width` 以下なら縮小しない**
  （拡大は決してしない ＝ 小さい素材で無駄にぼかさない）。
* **フレームキャッシュのキーに「縮小したかどうか」を含める**。含めないと、再生中に作った
  縮小フレームが停止後の原寸要求へ返ってしまい、止めても粗いままになる
  （キーは現行 `(media.id, 秒)` / `frame_source.py:73`）。
* `set_playing(False)` で `skip_frame` と縮小を戻し、**その場で現在位置を取得し直して
  原寸の絵へ差し替える**（停止した瞬間に鮮明になる / §3-6）。

### 5.8 プレビュー表示（`preview_panel`）

```python
    def _on_frame_ready(self, job_id, width, height, data):
        if job_id != self._frame_job:
            return
        image = QImage(data, width, height, width * 3, QImage.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image)
        # 【重要】縮小フレームを Qt で拡大し直さない (resolve6 §2.2(c))。
        # 拡大は 4.5ms/枚 かかり、縮小で浮いた 2.4ms を食い潰して逆効果になる。
        # キャンバスへの拡縮はアイテムの scale で行う (描画は Qt/GPU 側で済む)。
        scale = min(self._canvas[0] / pixmap.width(), self._canvas[1] / pixmap.height())
        self._base_item.setPixmap(pixmap)
        self._base_item.setScale(scale)
        self._base_item.setPos((self._canvas[0] - pixmap.width() * scale) / 2.0,
                               (self._canvas[1] - pixmap.height() * scale) / 2.0)
```

* `_set_rate()` から `self._frame_source.set_playing(self.is_playing())` を呼ぶ。
  停止へ遷移したときは続けて `_request_frame()` を投げ、原寸の絵へ戻す（§5.7）。
* **プレビュー上のオーバーレイ（字幕・画像）は縮小しない。** シーン座標はキャンバス実寸のまま、
  ベース映像だけが `setScale()` で拡大されて表示される。したがって
  **字幕の見た目・位置・ドラッグ操作は縮小の影響を受けない**。
* `_rebuild_overlays()` は §3-9 の差分更新へ（Phase 4）。

### 5.9 アーカイブ画面で認識用音声を再利用する

`ArchiveTimelineDialog` は `asr_audio_path` を渡していないため、
**初回再生でも必ずチャンク生成が走る**。アーカイブの prepare フェーズは
クリップごとに文字起こし用の音声を作っているが、Timeline は **全クリップを 1 本に並べた**
ものなので（resolve5 §3-1）、**クリップ 1 本ぶんの音声では Timeline 全長をまかなえない**。

したがって再利用はできない。代わりに **画面を開いた直後に先頭の起動用チャンクを
先読みしておく**（`showEvent` で `_request_chunk(0.0, autoplay=False)` を 1 本投げる）。
実測 0.10 s の生成のため、利用者が ▶ を押すころには手元にある。

* クリップ用（`TimelineEditorDialog`）は現行どおり認識用音声の再利用が効くため変更しない。
* この先読みは `preview.audio_prefetch_on_open`（既定 true）で止められる。

### 5.10 一括編集コマンド（`EditSubtitles`）── B8

```python
# 複数の字幕クリップを 1 手で変更する (resolve6 §3-10)
# Undo はスナップショット方式のため、N 件の変更がまとめて 1 手で戻る。
# 字幕以外の ID が混ざっていても黙って読み飛ばす (選択には映像クリップも入り得る)。
class EditSubtitles(Command):

    def __init__(self, clip_ids, **fields):
        self._clip_ids = [i for i in (clip_ids or []) if i]
        self._fields = fields
        # ラベルは件数入り (「元に戻す: 字幕の編集（3 件）」と出す)
        self.label = ("字幕の編集" if len(self._clip_ids) <= 1
                      else f"字幕の編集（{len(self._clip_ids)} 件）")

    def apply(self, timeline):
        changed = False
        for clip_id in self._clip_ids:
            clip = timeline.clip_by_id(clip_id)
            if not isinstance(clip, SubtitleClip):
                continue
            for key, value in self._fields.items():
                if not hasattr(clip, key) or getattr(clip, key) == value:
                    continue
                setattr(clip, key, value)
                changed = True
        return changed
```

* `label` は**インスタンス属性で上書き**する（既存コマンドはクラス属性だが、
  `CommandStack.push` は `command.label` を読むだけなので問題ない / `commands.py:95`）。
* 1 件も変わらなければ `False` を返す ＝ **履歴を汚さない**（既存 `EditSubtitle` と同じ規約）。
* 既存の `EditSubtitle` は**残す**。他の呼び出し元（テストを含む）を壊さないため。
  `EditSubtitles(ids)` は `EditSubtitle` の上位互換だが、置き換えは行わない。

```python
# TimelineController
    # 複数の字幕へまとめて適用する (resolve6 §3-10)
    def edit_subtitles(self, clip_ids, **fields):
        return self.execute(commands.EditSubtitles(clip_ids, **fields))
```

---

## 6. 実装手順（フェーズ分け）

| Phase | 内容 | 完了条件 |
|---|---|---|
| **1** | モデル + ASS + プレビュー近似 + Resolve 出力の色対応（B1/B2/B4/B5/B6） | §10.1 の単体テストが通る。色未指定時の ASS が現行と同一 |
| **2** | `color_field.py` + インスペクタの 2 行 + `EditSubtitles`（B1/B2/B7/**B8**）、設定画面の委譲 | 画面で色を変え、その場でプレビューに反映され、**複数選択なら全件へ効き**、Undo で 1 手で戻る |
| **3** | 再生の主因対策 R1（前進デコード）+ R2/R3/R4/R5（音声）+ **R6（再生中の縮小・既定 ON / 回答 Q6）** | ▶ から音が出るまで 1 秒未満。映像が音に追従する。停止すると原寸へ戻る |
| **4** | R7（`skip_frame`・既定 OFF）+ R9（オーバーレイ差分更新） | 設定で切り替わる。既定では見た目が変わらない |
| **5** | R8（PyAV 非搭載環境の常駐パイプ） | PyAV を外した状態で再生できる |

> Phase 1・2（色）と Phase 3（再生）は独立しているため並行して進められる。
> Phase 4・5 は Phase 3 の効果を実機で確認してから要否を判断してよい。

---

## 7. setting.json 定義（追加分）

```jsonc
"timeline": {
    "ui": {
        // 追加: インスペクタの色見本ボタンの幅 (px)
        "subtitle_color_swatch_width_px": 28
    },
    "preview": {
        // ── 映像 (resolve6 §3-6 R1/R6/R7) ───────────────────────────
        // 直前に読んだ位置からこの秒数以内の前進なら seek せずに読み進める。
        // 0 にすると常に seek する = 従来動作へ戻る。
        "sequential_decode_sec": 2.0,
        // 再生中だけ映像をこの幅へ縮小して取得する (0 = 原寸のまま / 回答 Q6 により既定 960)。
        // 「画質を落としてでも再生したい」への対応。停止・スクラブ中は原寸に戻るため、
        // 粗くなるのは再生中だけ。素材の幅がこれ以下なら縮小しない (拡大はしない)。
        // 出力動画の画質には影響しない (プレビュー専用)。
        "playback_width": 960,
        // 再生中のフレーム間引き "none" | "nonref" | "bidir"。
        // nonref は他フレームから参照されない絵を捨てる (実測 11→8ms/枚)。
        "playback_skip_frame": "none",
        // PyAV が無い環境で、再生中だけ常駐 ffmpeg のパイプ読み出しを使う
        "frame_pipe_fallback": true,

        // ── 音声 (resolve6 §3-6 R2〜R5) ─────────────────────────────
        // "single_process" = 断片ごとに -ss 入力を並べて 1 回で作る (既定)
        // "per_piece"      = 断片ごとに ffmpeg を起動する従来方式 (切り戻し用)
        "audio_build_mode": "single_process",
        // ▶ を押したとき最初に作る短いチャンクの長さ (秒)。小さいほど早く鳴る。
        "audio_startup_chunk_sec": 6,
        // 保持しておくチャンクの本数。少し戻して再生し直すときの作り直しを防ぐ。
        "audio_chunk_cache": 3,
        // プレビュー音声の品質 (編集用の確認音のため既定は軽くしてある)。
        // 0 / 空文字なら ffmpeg セクションの値 (48000 / aac) を使う。
        "audio_sample_rate": 24000,
        "audio_channels": 1,
        "audio_codec": "pcm_s16le",
        // 画面を開いた直後に先頭の起動用チャンクを先読みする (アーカイブ用に効く)
        "audio_prefetch_on_open": true
    }
}
```

* 既定値は `builder.timeline_config()` と `settings_window.DEFAULT_SETTINGS` の**両方**へ入れる。
  キーが無い既存 `setting.json` でも動き、次回起動時にファイルへも現れる（§2.4）。
* あわせて **resolve5 で漏れていた `timeline.ui.archive_*` 4 キーを DEFAULT_SETTINGS へ追記**する。
* `timeline.preview.width` / `update_debounce_ms` は**触らない**。未使用のまま残す
  （意味を変えると既定値 960 が黙って効き始めるため / §2.3）。
* 字幕の色は **クリップ個別の値**であり設定値ではないため、`setting.json` には追加しない。
  役割ごとの既定色は従来どおり `subtitle.*_color` で設定する。

---

## 8. 互換性・非破壊の担保

| 対象 | 保証内容 |
|---|---|
| 色を一度も指定しない場合 | `to_item()` にキーが増えず、`_inline_overrides()` も空文字。**ASS / FCPXML は現行と完全に同一** |
| 旧プロジェクト JSON | `color` / `outline_color` の欠落は空文字へ落ちる。`schema_version` は 1 のまま |
| 旧アーカイブ結果画面（`timeline_review=false`） | 経路を通らないため変更なし（色欄も出ない） |
| 旧字幕編集画面（`SubtitleEditorDialog`） | 変更なし。items に色キーが付かないため出力も従来どおり |
| 設定画面 | `_parse_color`/`_format_color`/`_update_swatch` は委譲のみ。見た目・挙動は不変 |
| 焼き込み・結合・テーマ演出 | 変更なし |
| 既存のコマンド | `EditSubtitle` は残す。`EditSubtitles` は追加のみで、既存の呼び出し元・テストは無変更 |
| 単一選択時の操作感 | 変更なし。件数の表示も案内も出ない（**複数選択したときだけ**増える / §5.5） |
| 字幕テキスト・役割・フォント・サイズ | 一括の対象外。複数選択時も従来どおり先頭 1 件に効く（§3-10 / §9 Q8） |
| 再生まわり | すべて設定で従来動作へ戻せる（`audio_build_mode="per_piece"` / `sequential_decode_sec=0` / `playback_width=0` / `playback_skip_frame="none"`） |
| プレビューの見え方 | **再生中だけ**粗くなる（既定 960 幅）。停止・スクラブ中は原寸。字幕・画像オーバーレイは縮小の対象外（§5.8） |
| 出力動画の画質 | **変わらない**。R4・R6・R7 は**プレビュー専用**で、レンダリング経路（`renderer.py`）には一切触れない |

---

## 9. 確認事項

### 9.1 回答済み（2026-08-16）

| # | 確認事項 | 決定 |
|---|---|---|
| **Q1** | 色は 1 件ごとでよいか。**複数選択への一括適用**まで要るか | **一括適用を可能にする**（要件 B8 / §3-10・§5.5・§5.10） |
| **Q6** | 再生中の映像を**既定でも縮小するか**（`playback_width`） | **既定で有効にする**（`960` / §3-6・§5.7・§7）。R6 は Phase 3 へ前倒し |

### 9.2 実装を止めない補足（本書の案で進める。異論があれば実装前に指示が要る）

| # | 確認事項 | 本書の案 |
|---|---|---|
| **Q2** | 縁の色に **透明度（α）** を選べるようにしてよいか | 選べるようにする（設定画面の縁色と同じ ASS 形式のため自然） |
| **Q3** | 縁の**太さ**もクリップごとに変えたいか | 今回は入れない（要望は色のみ）。入れるなら `\bord` を同じ仕組みで足せる |
| **Q4** | 旧字幕編集画面（`SubtitleEditorDialog`）にも色欄を足すか | 足さない（要望は Timeline 編集画面） |
| **Q5** | プレビュー音声の既定品質を **24kHz mono wav** に落としてよいか（出力動画には影響しない） | 落とす。編集中の確認音のため。設定で 48kHz stereo aac へ戻せる |
| **Q7** | Phase 5（PyAV 非搭載環境の常駐パイプ）まで要るか | **不要と判断**（PyInstaller に PyAV のフックがあり同梱される / §11.3）。実機で最終確認する |
| **Q8** | Q1 を受けて、**役割 / フォント / サイズ**も複数選択へ一括適用するか | **今回は色のみ**（要望の範囲）。仕組み（`EditSubtitles`）は共通のため、`_on_role_changed` などの呼び先を差し替えるだけで後から広げられる |

### 9.3 本書で決めた割り切り

* **複数選択中の表示は先頭 1 件の値**。色がバラバラでも「複数値」の状態は持たない（§3-10）。
* **再生中の絵は粗くなる**（既定 960 幅・回答 Q6）。停止した瞬間に原寸へ戻す設計にしてあるため、
  字幕位置の微調整など精度が要る操作は止めた状態で行うことになる。

---

## 10. テスト計画

### 10.1 単体（新規 `tests/test_subtitle_color.py`）

**ASS 生成**

* `color` 指定 → 本文先頭に `{\1c&HBBGGRR&}` が付く（RGB→BGR の並び替えが正しい）
* `outline_color` が 8 桁 → `\3c` と `\3a` が対で付く
* `outline_color` が 6 桁 → `\3c` のみで `\3a` は付かない
* 位置・フォント・サイズ・色をすべて指定 → **順序が「位置 → フォント → サイズ → 塗り → 縁」**
* **色を指定しない items の出力が、変更前の出力と 1 バイトも変わらない**（回帰の要）
* 不正な色文字列（`"red"` / `"#FFF"` / `"&HZZ"`）→ タグを出さずに落ちない

**モデル**

* `to_item()` は未指定時に `color` / `outline_color` のキーを持たない
* `to_dict()` → `from_dict()` の往復で色が保たれる／欠落キーは空文字になる
* `copy()` が色を引き継ぐ
* `EditSubtitle(clip_id, color=…)` で値が変わり、Undo で戻る

**一括編集（B8 / `EditSubtitles`）**

* 3 件を指定 → 3 件とも色が変わる。**Undo 1 回で 3 件とも戻る**（履歴が 1 手しか増えない）
* 選択に映像クリップ・音声クリップが混ざっていても、字幕だけが変わり例外にならない
* 存在しない ID が混ざっていても落ちない
* すでに同じ色の 3 件を指定 → `False`（履歴を汚さない）
* 一部だけ違う色の 3 件を指定 → `True`。全件が指定色に揃う
* ラベルが「字幕の編集（3 件）」になる（1 件のときは「字幕の編集」）
* 空リストを指定 → `False`

**Resolve 出力**

* item の色が title の `color` / `stroke_color` / `stroke_alpha` に反映される
* 未指定 item は従来どおり役割色になる

**再生（実ファイル不要な範囲）**

* `sequential_decode_sec=0` で従来どおり毎回 seek する経路を通る
* `audio_build_mode="per_piece"` で従来のコマンド列を組み立てる
* 起動用チャンクの断片列が `audio_startup_chunk_sec` を超えない
* `playback_width=960`・再生中 → 縮小後の寸法が 960 幅・偶数高さになる（縦横比を保つ）
* `playback_width=960`・**停止中** → 原寸のまま返す
* 素材幅が 960 以下 → 縮小しない（拡大しない）
* 縮小フレームと原寸フレームが**同じキャッシュキーを共有しない**（止めたら原寸に戻る）

**非破壊**

* `tests/test_timeline_commands.py` ほか既存テスト … 全通過

### 10.2 結合（手動・GUI）── 色

* [ ] クリップ用の編集画面で字幕を選ぶと「文字色」「縁の色」が出る
* [ ] 色を変えると**プレビューの字幕がその場で変わる**
* [ ] 「既定へ」で空欄に戻り、設定の役割色に戻る
* [ ] Ctrl+Z で 1 手前の色に戻る／Ctrl+Y でやり直せる
* [ ] 「高精度プレビュー」で**本番と同じ色**が出る
* [ ] 決定 → 書き出した動画の該当字幕だけ色が変わっている（他は従来どおり）
* [ ] アーカイブ用の編集画面でも同じ操作ができ、焼き込み結果に出る
* [ ] Resolve 出力（.fcpxml）を Resolve で開き、色が反映されている
* [ ] 色欄にフォーカスがある間、W / A / D / Space などがショートカットとして働かない

**複数選択（B8）**

* [ ] Timeline 上で **Ctrl+クリック**して字幕を 3 件選ぶと、見出しが「字幕クリップ（3 件選択）」になる
* [ ] その状態で文字色を変えると **3 件すべて**変わる（プレビューでも確認できる）
* [ ] **Ctrl+Z 1 回で 3 件とも元に戻る**（3 回押す必要がない）／Ctrl+Y でやり直せる
* [ ] 「既定へ」も 3 件すべてに効く
* [ ] 字幕テキスト・役割・フォント・サイズは**先頭 1 件だけ**に効く（従来どおり）
* [ ] 映像クリップと字幕を一緒に選んだ状態で色を変えても落ちず、字幕だけ変わる
* [ ] プレビュー上のラバーバンド選択で複数選んでも同じように効く
* [ ] 1 件だけ選んでいるときの見た目・操作が従来と変わらない（件数表示も案内も出ない）

### 10.3 結合（手動・GUI）── 再生

* [ ] ▶ を押してから音が出るまで **1 秒未満**（編集直後・チャンク未生成の状態で）
* [ ] 再生中、映像が音にほぼ追従する（止まったまま飛ぶ挙動が消えている）
* [ ] チャンクの継ぎ目で音が途切れない
* [ ] 一時停止 → 少し戻して再生、で待ちが発生しない
* [ ] 倍速再生（E）・倍速逆再生（Q）が従来どおり動く
* [ ] スクラブ（ドラッグ）の反応が従来より悪くなっていない
* [ ] **再生中は絵が粗く、止めると鮮明に戻る**（既定 `playback_width=960` の想定どおりの挙動）
* [ ] 再生中も**字幕・画像オーバーレイは粗くならない**（縮小はベース映像だけ）
* [ ] 再生を止めた直後の 1 枚が**正しい絵**（skip_frame / 縮小を有効にした場合も）
* [ ] 縦動画（1080x1920）でも縮小後の縦横比が崩れない
* [ ] `playback_width=0` にすると再生中も原寸のまま（従来の見え方に戻る）
* [ ] `audio_build_mode="per_piece"` / `sequential_decode_sec=0` で従来動作に戻る
* [ ] 音声を持たない素材・ギャップ（無音）を含む位置から再生できる
* [ ] 画面を閉じたときに一時ファイル（`preview_audio_*`）が残らない

### 10.4 環境確認（Q7 の判断材料）

* [ ] 配布ビルド（`installer/` 生成物）で `timeline.preview` のステータス行に
      「PyAV が利用できないため…」が出ないこと。出るなら Phase 5 が必須になる。

### 10.5 非破壊の突き合わせ

* 色を一切指定せずに書き出した動画・ASS・FCPXML を、変更前と比較する
  （ASS は**テキスト一致**を求める。動画は尺・字幕表示時刻・見た目で照合する）。

---

## 11. 実装記録（2026-08-16）

Phase 1〜4 を実装した。Phase 5（R8）は §11.3 の理由で見送っている。

### 11.1 実装後の実測（設計時の見込みと同じ素材・条件）

| 対象 | 変更前 | 変更後 |
|---|---:|---:|
| 映像フレーム取得（再生時 15fps の連続要求） | 119 ms/枚 | **停止中 13 ms/枚 / 再生中（960 幅）10 ms/枚** |
| 音声チャンク 30 秒（断片 15 本） | 3.73 s | **0.30 s** |
| ▶ を押してから鳴るまで（起動用 6 秒チャンク） | 3.73 s | **0.12 s** |

* 前進デコードで得た絵は seek で得た絵と **10 枚すべて一致**（絵がずれていないことを確認）。
* 再生中は 960x540、停止すると 1920x1080 へ戻ることを確認。
* 生成した音声チャンクの尺は 30.000 秒 / 6.000 秒（従来方式は AAC のフレーム量子化で
  30.021 秒になっていた。1 プロセス方式のほうが尺も正確）。

### 11.2 実装中に判明したこと

* **`to_export_entry`（`src/archive/timeline_builder.py`）が items を手組みしていた**ため、
  そのままではアーカイブの Resolve 出力に色が乗らなかった。
  クリップ用と同じ `SubtitleClip.to_item()` を使う形へ直した（位置指定も一緒に運ばれるようになる）。
* **libass の解釈を実写で確認した**（黒背景へ焼いて画素を数えた）:
  `\1c&H00FF00&` → 緑文字 / `\3c&H0000FF&` → 赤い縁 / `\3a&H80&` → 半透明の縁。
  タグの並び順・BGR 変換・アルファ反転がいずれも意図どおりに効いている。
* `set_playing()` は開いている素材だけでなく **保持しているデコーダをすべて捨てる**必要がある
  （素材ごとに落とす実装だと、開いていない素材のデコーダが残り得る）。
* `BaseFrameItem.show_blank()` でも `setScale(1.0)` へ戻す必要がある
  （縮小フレームの直後に空白を出すと、拡大率が残ったままになる）。

### 11.3 Phase 5（R8 / PyAV 非搭載環境の常駐パイプ）を見送った理由 ── Q7 の回答

配布ビルドで PyAV が同梱されることを確認したため、常駐パイプは要らないと判断した。

* `src/timeline/frame_source.py` は `import av` をモジュール先頭で行っており、
  PyInstaller の解析対象になる。
* ビルド環境の PyInstaller 6.20.0 には **`hook-av.py`（pyinstaller-hooks-contrib）が入っており**、
  PyAV のバイナリ依存ごと収集される。
* したがって §10.4 の確認項目（配布ビルドで「PyAV が利用できないため…」が出ないこと）は
  満たされる見込み。**実機のインストーラで念のため確認**し、もし出るようなら Phase 5 を起こす。
* 設定キー `frame_pipe_fallback` は既定 true のまま残してある（Phase 5 を入れたときの入口）。

### 11.4 テスト

* 新規: `tests/test_subtitle_color.py`（21 件）/ `tests/test_preview_playback.py`（15 件）
* 既存 294 件を含め **330 件すべて通過**。
* GUI は offscreen で通し確認（クリップ用・アーカイブ用の両画面 / 設定画面）:
  色欄の表示、複数選択（3 件）への一括適用、Undo 1 回での復帰、映像クリップ混在時の安全性、
  「既定へ」での復帰、起動用チャンクの先読み、ASS への色の反映。
