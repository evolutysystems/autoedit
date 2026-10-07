# ver6 resolve3 — プレビューと出力の差異をなくし、プレビューを滑らかにする (修正設計書)

対象: `autoedit` (本書の実装範囲のみ。API / homepage への影響は無い)
入力: 本セッションの要望 —「timeline 上のプレビュー画面と、実際に出力された動画との差異が激しい。
とにかく差異をなくしつつ、プレビュー画面がスムーズに動くようにしたい」
前提: `docs/request/ver3/resolve.md` §6.4 / §6.5 (プレビューの初期設計)、
同 `resolve6` (再生の応答改善)、`ver5/resolve4` §5.8 (ぼかしのプレビュー反映)、
`ver5/resolve9` §5.6 / §5.8 (縦動画クロップのプレビュー)

作成日: 2026-09-30

---

## 0. 要望と対応方針の対応表

| # | 要望 | 対応 | 節 |
| --- | --- | --- | --- |
| A | プレビューと出力の差異をなくす | 合成の計算を `src/timeline/compositor.py` へ集約し、プレビューも**出力と同じフィルタ断片**で絵を作る | §3.1 / §4.1 |
| A-1 | 字幕の見た目が違う | Qt の近似描画をやめ、libass (ASS) でプロキシへ焼き込む。ASS は**本番寸法の PlayRes のまま**使うので相似が保証される | §3.3 / §4.4 |
| A-2 | 縦動画の背景ぼかしがプレビューに出ない | プロキシは `crop.filter_chain` をそのまま通す (現状は `crop.preview_rects` で黒のまま) | §3.3 |
| A-3 | 透かしがプレビューに出ない | プロキシに `watermark_overlay.build_chain` を足す | §4.1 / §4.5 |
| A-4 | ぼかしが停止中しか反映されない | プロキシでは常に `blur_overlay.build_chains` を通す | §4.1 |
| A-5 | 無音カットのフェードがプレビューに出ない | プロキシはベース抽出のフィルタ (フェード込み) を通す | §4.1 |
| A-6 | 「高精度プレビュー」自体がクロップと透かしを通していない | 第3の実装を削除し、`compositor` の 1 枚絵モードへ置き換える | §4.6 |
| B | プレビューが滑らかに動かない | 事前レンダしたプロキシ mp4 を `QMediaPlayer` で再生する。毎コマの PyAV デコードと GUI スレッド合成を再生経路から外す | §3.4 / §4.3 |
| B-1 | `play_fps: 15` の頭打ち | プロキシ再生では不要になる。近似経路 (縮退時) の既定だけ 30 へ上げる | §5 |
| B-2 | 編集してから絵が出るまで待たされる | タイル単位の差分レンダ + 未レンダ区間は近似描画を下書きとして出す | §3.5 / §4.2 |

### 0.1 変更するファイル

| ファイル | 種別 | 内容 |
| --- | --- | --- |
| `src/timeline/compositor.py` | **新規** | 合成のフィルタ断片を組み立てる唯一の場所。キャンバス寸法を引数で受ける |
| `src/timeline/proxy_render.py` | **新規** | タイル単位のプロキシ生成 (1 タイル = 1 回の ffmpeg 実行) |
| `src/gui/timeline/proxy_manager.py` | **新規** | タイルの鍵計算・キャッシュ・優先順・ワーカー管理 (GUI 寄りの都合はここへ閉じる) |
| `src/timeline/renderer.py` | 変更 | `_video_filters` / `_composite` の自前計算を `compositor` 呼び出しへ置換 |
| `src/gui/timeline/preview_panel.py` | 変更 | プロキシ再生経路の追加。`_hq_overlay_chains` / `_hq_blur_chains` を削除し `compositor` へ寄せる。`_apply_crop` の毎フレーム確保をやめる |
| `src/gui/timeline/preview_items.py` | 変更 | 近似描画は「下書き」専用へ格下げ。字幕の影とアウトラインの太さを ASS に寄せる (§4.7) |
| `src/gui/timeline/timeline_editor_dialog.py` | 変更 | `ProxyManager` の生成・破棄と、レンダ済みバーの表示 |
| `src/gui/timeline/timeline_view.py` | 変更 | ルーラー下にレンダ済みバーを描く |
| `src/modules/comment_decor.py` | 変更 | `scale` 引数を追加 (絶対 px を含むためプロキシ比を掛ける必要がある / §3.3) |
| `src/timeline/crop.py` | 変更 | `filter_chain` の `boxblur` 半径をキャンバス比で決める (現状は絶対 px) |
| `src/timeline/frame_source.py` | 変更 | `reformat` に色空間を明示する (§4.7-4) |
| `src/settings/setting.json` | 変更 | `timeline.preview.proxy` セクションを追加 |
| `src/timeline/builder.py` | 変更 | `timeline_config` に `preview.proxy` の読み出しを追加 |
| `tests/test_compositor.py` | **新規** | 同じ Timeline から本番寸法とプロキシ寸法のグラフを作り、**相似であること**を検査 |
| `tests/test_proxy_render.py` | **新規** | タイル鍵の同一性・差分無効化・境界のオフセット |
| `tests/test_preview_playback.py` | 変更 | プロキシ再生への切り替えと縮退の回帰 |
| `tests/test_comment_decor.py` | 変更 | `scale` を掛けた配置が本番の相似になること |
| `tests/test_crop_layout.py` | 変更 | `boxblur` 半径がキャンバス比で決まること |

---

## 1. 調査で分かった前提 (ここを踏まえて設計する)

### 1.1 合成の計算が 3 箇所にある

| # | 実装 | 場所 | 使う所 |
| --- | --- | --- | --- |
| 1 | FFmpeg filtergraph (本番) | `src/timeline/renderer.py:340` `_video_filters` / `:610` `_composite` | 出力 |
| 2 | Qt 近似描画 | `src/gui/timeline/preview_items.py` 全体 | Timeline 編集画面のプレビュー |
| 3 | FFmpeg filtergraph (第 3 実装) | `src/gui/timeline/preview_panel.py:1128` `_hq_overlay_chains` / `:1102` `_hq_blur_chains` | 「高精度プレビュー」 |

同じ式 (正規化座標 → overlay 左上座標の `(x+1)*W/2`、`transform.scale` → `scale={W*s}:-2`) が
**3 回書かれている**。片方を直しても他方へ伝わらないため、機能を足すたび差異が増える構造になっている。
現に第 3 実装はクロップ (`crop.filter_chain`) と透かしを通しておらず、
**「高精度プレビュー」と名乗りながら縦動画プロジェクトでは出力と一致しない**。

`crop.py` は冒頭で「枠 → フィルタ文字列 / プレビューの配置 の計算は**このモジュールだけ**が持つ」と
宣言している (§4-3)。本書はこの規約を合成全体へ広げる。

### 1.2 いま食い違っているものの一覧

| 項目 | プレビュー | 出力 | 根拠 |
| --- | --- | --- | --- |
| **字幕** | Qt `QGraphicsSimpleTextItem` | libass (ASS) | `preview_items.py:319` / `renderer.py:519` |
| 　└ 影 | **描いていない** | `Shadow=1` | `subtitle_generator.py:31` `_ASS_SHADOW = 1` |
| 　└ アウトライン | `pen.setWidth(W)` = 中心線ストローク → **見た目の太さが約半分**・字が細る | ASS `Outline=W` = 字の**外側**に W px | `preview_items.py:351` |
| 　└ 行送り / MarginV | Qt の `boundingRect` 基準 | libass のフォントメトリクス基準 | `preview_items.py:_place` |
| **縦クロップの背景** | 黒のまま | `boxblur` のぼかし背景 | `crop.py:273` `preview_rects` (背景を作らない) / `:235` `filter_chain` / `preview_panel.py:477` に「1 コマごとに重くなるため掛けない」と明記 |
| **透かし** | **出ない** | 残高不足時に焼き込む | `src/gui/` に `watermark_overlay` の参照が無い |
| **ぼかし** | 停止中のみ PIL ガウス。再生中は赤いマーカー | ffmpeg `gblur` / モザイク | `blur/preview.py` 冒頭 / `preview_panel.py:408` |
| **フェード** | 反映なし | 無音カットの `fade=t=in/out` | `renderer.py:363` |
| **fps** | `play_fps: 15` で頭打ち | `output_fps: 60`。さらに `_tile_duration` でフレーム量子化 | `preview_panel.py:859` / `renderer.py:169` |
| **音声** | 24kHz / mono / PCM チャンク | 48kHz / stereo / loudnorm + AAC | `setting.json` `timeline.preview.audio_*` |
| **色** | PyAV `to_ndarray(format="rgb24")` (色空間指定なし = BT.601 既定になり得る) | ソースのタグを保った yuv420p | `frame_source.py:_to_rgb` |

### 1.3 滑らかでない理由

1. `preview_panel.py:859` で映像更新を `play_fps=15` に頭打ちしている。
2. バッファが無い。`_FrameWorker` は「最新の要求だけを処理する」ため、
   デコードが間に合わないぶんは**そのまま脱落**する。先読みキューが無いので
   デコード待ちがそのまま表示のガタつきになる。
3. `_apply_crop` (`preview_panel.py:477`) が**毎フレーム GUI スレッドで**
   キャンバス寸法 (縦なら 1080x1920) の `QPixmap` を確保し `QPainter` で描き直している。
4. `_on_frame_ready` が毎フレーム `QImage(...).copy()` + `QPixmap.fromImage()` で全画素コピーしている。
5. `_rebuild_overlays` がプレイヘッド移動ごとに `QGraphicsItem` を作り直す。
   動画オーバーレイは GUI スレッドで同期デコードするため、顔ぶれが変わる瞬間に必ず引っかかる
   (現状は「顔ぶれが変わらなければ作り直さない」で緩和しているだけ)。

つまり**「リアルタイム合成を GUI スレッドで毎コマやる」構造そのもの**が上限になっている。
近似描画を作り込む方向では §1.2 の差異も §1.3 の重さも同時には解けない。

### 1.4 幸運: 既存のフィルタ組み立ては、ほぼプロキシ対応済み

キャンバス寸法を引数で受け、比率で計算している箇所は、**プロキシ寸法を渡すだけで相似になる**。
調査した結果は以下のとおり。

| モジュール | 判定 | 根拠 |
| --- | --- | --- |
| `blur_overlay.build_chains` | **そのまま使える** | `blur/config.py:184` `blur_sigma` が `canvas_width * 0.00025 * strength` で幅に比例。マスク動画も `scale={canvas_width}:{canvas_height}` で戻している |
| `watermark_overlay.build_chain` | **そのまま使える** | `_geometry` が `scale_ratio` / `margin_ratio` の比率計算 |
| `renderer._composite` のオーバーレイ | **そのまま使える** | 位置は正規化座標 `(x+1)*W/2`、大きさは `transform.scale` = キャンバス幅比 |
| ASS (libass) | **そのまま使える** | `subtitle_generator.py:722` で `PlayResX/Y` に**本番寸法**を書いている。libass が PlayRes → 実フレーム寸法へ自動でスケールするため、フォントサイズも余白も完全に相似になる |
| `crop.filter_chain` | **ほぼ使える** | crop 矩形はソース px なので影響なし。`scale` / `pad` / `vstack` はキャンバス寸法から導出。**`boxblur={radius}:{power}` の半径だけ絶対 px** なので比を掛ける必要がある |
| `comment_decor` | **要修正** | `font_size` / `comment_margin_l` / `comment_icon_size_px` / `comment_icon_gap_px` が絶対 px で、それをキャンバス寸法と混ぜて計算している (`:116` `text_extent` / `:142` `_text_anchor` / `:166` `icon_box` / `:196` `background_box`) |

**要修正は 2 箇所だけ**。プロキシ化は思ったより安い。

### 1.5 `CommandStack` はスナップショット方式である

`src/timeline/commands.py:127` の `CommandStack` は、コマンドごとの逆操作ではなく
Timeline 全体のスナップショットで Undo/Redo を実現している。
したがって「このコマンドが汚した時間範囲」を正確に取り出す仕組みは**無い**。
本書ではこれを無理に足さず、§3.5 の**内容ハッシュ方式**を採る。

### 1.6 音声チャンク生成は、タイル方式の良い手本になっている

`src/timeline/audio_source.py:141` `_collect_pieces` → `:179` `_render_pieces` は
「区間 [start, end] を構成する断片を集め、`-ss`/`-t` 付きの入力を並べて 1 回の ffmpeg で concat する」
という作りになっている。しかもコメントに
「**1 入力 + atrim にしてはならない**。入力シークが効かず素材の先頭からデコードするため
後方の区間では逆に数倍遅くなる (実測 7.62s)」という実測付きの教訓が残っている。
映像タイルもこの流儀をそのまま踏襲する (§4.2)。

---

## 2. 方針 — 何を一致させ、何を一致させないか

差異を「ゼロ」と書くと検証できないため、範囲を先に決める。

### 2.1 一致させる (受け入れ基準に入れる)

* **配置**: オーバーレイ・字幕・コメントアイコン・透かしの位置
* **大きさ**: 拡大率・フォントサイズ・アイコン寸法・背景箱の寸法
* **描画**: 字幕のグリフ・アウトライン・影 (libass を通すため)
* **効果**: ぼかし (対象・強さ・輪郭)、クロップの背景、フェード
* **時間**: どのフレームに何が写っているか (フレーム量子化まで含めて一致させる)

### 2.2 一致させない (意図的)

* **解像度**: プロキシは既定でキャンバスの 1/2 (1080x1920 → 540x960)。
  「見え方を確かめる」のに原寸は要らない。
* **エンコード品質**: プロキシは `ultrafast` / 高 CRF。ブロックノイズは出る。
* **エンコード世代**: 本番は「ベース連結 → 合成 → 最終化」の複数パス構成で、
  A/V 同期のための実測に基づく対策 (中間 PCM、`_duration_arg` の切り捨て、
  `-avoid_negative_ts`) が積み上がっている (`renderer.py:184` のコメント)。
  **この構成には手を入れない。** プロキシは 1 パスで作る。
  したがって微細なノイズ・色の量子化は一致しない。
* **音声品質**: プロキシ音声は再生用の品質のまま。loudnorm は掛けない
  (掛けると編集中ずっと解析コストが乗る)。§10 論点 2 に残す。

この線引きが本書の肝である。「一致」を画素の完全一致と定義すると達成不能になり、
「見え方の一致」と定義すれば検証可能かつ安く済む。

---

## 3. アーキテクチャ

### 3.1 合成計算を `compositor.py` へ集約する

```python
# 合成のフィルタ断片を組み立てる唯一の場所 (ver6 resolve3 §3.1)
#
# 出力 (renderer) とプレビュー (proxy_render) の両方がここだけを呼ぶ。
# 計算が 2 箇所に分かれると「見たとおりに書き出されない」が必ず再発するため、
# crop.py と同じ規約 (計算はこのモジュールだけが持つ) をここへ広げる。


# 合成先のキャンバス (本番 / プロキシの違いはこれ 1 つで表す)
#   scale : 本番キャンバスに対する比。絶対 px の設定値を換算するためだけに使う。
#           ASS は PlayRes が本番寸法のまま libass が追従するので掛けてはならない。
class CanvasProfile:

    def __init__(self, width, height, fps, scale=1.0): ...

    @staticmethod
    def production(timeline, ffmpeg_cfg): ...

    @staticmethod
    def proxy(timeline, ffmpeg_cfg, scale): ...


# クリップ 1 件をキャンバス寸法へ正規化する映像フィルタ (クロップ・正規化・フェード)
def base_filters(timeline, media, clip, profile, fade_sec, duration, settings): ...

# オーバーレイ 1 件ぶんの (入力引数, フィルタ断片, 出力ラベル)
def overlay_chain(timeline, clip, media, profile, input_index, step, with_enable=True): ...

# 字幕 1 グループぶんの (ass パス, items, ass フィルタ断片)
def subtitle_chain(timeline, context, clips, profile, name, fonts_dir): ...

# コメントアイコン (comment_decor へ profile.scale を渡す)
def icon_chains(items, eff_cfg, profile, in_label, out_label, prefix, with_enable=True): ...

# ぼかし / 透かし (既存モジュールへ profile の寸法を渡すだけの薄い包み)
def blur_chains(context, profile, in_label, out_label, prefix="bl"): ...
def watermark_chain(context, profile, in_label, out_label, input_index): ...
```

* `renderer.py` は**多パス構成を保ったまま**、断片の組み立てだけをここへ委譲する
  (`_video_filters` → `base_filters`、`_composite` のオーバーレイ部 → `overlay_chain`)。
  A/V 同期に効く部分 (`_build_segments` / `_duration_arg` / concat / 最終化) は**触らない**。
* プレビューは同じ関数を `profile.scale < 1.0` で呼ぶ。
* 「差異が生えない」ことを担保するのは §8.1 の相似テストである。
  同じ Timeline から本番プロファイルとプロキシプロファイルのグラフを作り、
  数値がすべて `scale` 倍になっていることを機械的に検査する。

### 3.2 `CanvasProfile` の決め方

```
本番    : CanvasProfile(timeline.width, timeline.height, output_fps, scale=1.0)
プロキシ : CanvasProfile(even(timeline.width * s), even(timeline.height * s), output_fps, scale=s)
```

* **`fps` は本番と同じにする。** 軽くしたいのは解像度であって時間軸ではない。
  fps を変えるとフレーム量子化 (`_tile_duration`) の結果が変わり、
  §2.1 の「どのフレームに何が写っているか」が崩れる。
* 寸法は偶数へ丸める (`crop._even` と同じ理由 / yuv420p)。
* `scale` は**フィルタの数値を換算するためだけ**に持つ。
  ASS は PlayRes が本番寸法のまま libass が追従するので、`scale` を掛けてはならない (§1.4)。

### 3.3 プロキシで縮めるのはどの段か

```
[素材デコード] → ① ソース→キャンバス段 → ② キャンバス上段 → [エンコード]
                   crop / scale / pad       overlay / ass / icon / blur / watermark
```

* **① で縮める。** `crop.filter_chain` と正規化チェーンの出力寸法をプロキシ寸法にする。
  crop 矩形はソース px なので変換不要 (crop してから scale するため)。
  これ以降のすべてのフィルタとエンコードが 1/4 画素になる。
* **② はプロキシ寸法を渡すだけで相似になる** (§1.4)。例外は 2 つ:
  * `crop.filter_chain` の `boxblur` 半径 → `blur_radius * scale` へ変える。
    併せて設定値の意味を「本番キャンバスに対する px」と明記する。
  * `comment_decor` → `scale` 引数を足し、`icon_box` / `background_box` が
    **本番キャンバスで計算した結果に `scale` を掛けて返す**ようにする。
    計算自体を本番寸法で行うのが重要で、こうすれば
    「Qt の実測を使わず推定値だけを見るので両者は必ず一致する」という
    `comment_decor.py:110` の設計意図 (§3 D-10) がプロキシでも保たれる。
* 素材のデコードは縮められない (crop 矩形がソース座標で決まるため)。
  ここが残るコストであり、§3.5 の先読みで隠す。

### 3.4 再生は「レンダ済みプロキシを `QMediaPlayer` に食わせる」

* 映像・音声が同じファイルに入るので、A/V 同期のための自前調整が不要になる。
* `positionChanged` からプレイヘッドを進める仕組み (`preview_panel.py:849`) は**そのまま流用**する。
  現状すでに音声がマスタークロックなので、ソースが音声チャンクからプロキシ mp4 へ変わるだけ。
* 毎コマの PyAV デコード・`_apply_crop`・`_rebuild_overlays` は**再生経路から外れる**。
  `play_fps` の頭打ち (§1.3-1) も不要になる。
* PyAV / `FrameSource` は**残す**。用途は次の 2 つへ縮退する。
  * 未レンダ区間の下書き表示 (§3.5)
  * `proxy.mode = "off"` で縮退したときの従来経路 (§7)
* スクラブ音声 (`_scrub_tick`) は現状のまま。プロキシから音だけ取る作りにもできるが、
  粒ごとに `setSource` し直す現在の実装と噛み合わないため本書の範囲外とする (§10 論点 3)。

### 3.5 タイル分割と内容ハッシュによる差分レンダ

プロジェクト全体を一度にレンダすると編集のたびに数十秒待つことになる。
固定長タイルに切り、**変わったタイルだけ**作り直す。

```
タイル n = Timeline 時間 [n * tile_sec, (n+1) * tile_sec)
```

**無効化の判定は「内容ハッシュ」で行う。** §1.5 のとおり `CommandStack` は
スナップショット方式で、コマンドから汚れ範囲を正確に取り出せない。
そこで各タイルについて、そのタイルの絵を決める入力すべてを並べてハッシュを取る。

```
tile_key(n) = sha1(
    profile (寸法・fps・scale),
    そのタイルに重なる V1 クリップ (media_id, source_in, timeline_start, duration, gain, muted),
    そのタイルに重なるオーバーレイ (media_id, transform, z_order, opacity),
    そのタイルに重なる字幕 (text, role, font, size, color, transform, use),
    source["crop"] レイアウト,
    ぼかしの判定結果 (blur decisions のハッシュ),
    watermark_required,
    影響する設定 (subtitle / vertical.crop / blur.render / watermark / ffmpeg.output_fps),
)
```

* ハッシュが一致するタイルは**そのまま再利用**する。Undo / Redo で元の状態へ戻れば
  キャッシュが自然に効き直す。これがスナップショット方式との相性が良い理由である。
* 汚れ範囲の追跡コードを書かないので、**書き忘れによる「古い絵が残る」不具合が原理的に起きない**。
  `commands.py` がコマンドごとの逆操作ではなくスナップショットを選んだのと同じ判断
  (「正しさをデータ側で担保する」) を踏襲する。
* ハッシュ計算はタイル数ぶんの辞書走査で済む。10 分 / `tile_sec=4.0` で 150 タイル程度。

**タイル境界の扱い**:

* タイルは Timeline 範囲で切るので、境界に跨るクリップは両方のタイルで
  それぞれの必要区間だけ抽出する (`audio_source._collect_pieces` と同じ / §1.6)。
* `overlay` の `enable='between(t,...)'`、動画オーバーレイの `setpts=PTS+...`、
  ぼかしマスクの `movie=` 読み出しは**すべてタイル開始時刻ぶんオフセットする**。
  ここを忘れると境界でオーバーレイが消える・ぼかしがずれる。§8.1 のテスト対象にする。
* 境界は必ずフレーム境界に乗せる (`timeline.to_frames` / `from_frames` を使う)。
  `tile_sec` が fps の整数倍にならない値でも成立させるため、
  タイル境界の秒はフレーム番号から逆算する。

**進め方**:

* プレイヘッドを含むタイルを最優先。次に前方 `lookahead_tiles` 本、その後に後方。
* 同時実行は 1 本 (ffmpeg は内部でスレッドを使うため、並列にしても速くならず
  編集操作の応答を奪う)。設定で増やせるようにはする。
* **未レンダのタイルは現在の Qt 近似描画を「下書き」として出す。**
  出来上がったら差し替える。待たされる感じが消え、既存コードが縮退経路として活きる。
* タイムラインのルーラー下に**レンダ済みバー**を出す (レンダ済み = 実線 / 生成中 = 点滅 /
  未レンダ = 無地)。「どこが正解の絵か」を利用者が判断できるようにする。

---

## 4. 実装の詳細

### 4.1 `src/timeline/compositor.py` (新規)

関数の一覧と役割は §3.1 に示した。移設の内訳は以下。

| 移す元 | 移す先 | 備考 |
| --- | --- | --- |
| `renderer._video_filters` | `compositor.base_filters` | `timeline.width` / `.height` の参照を `profile.width` / `.height` へ置換 |
| `renderer._crop_chain` | `compositor` 内部 | `profile` を受けて `boxblur` 半径へ `scale` を掛ける |
| `renderer._composite` のオーバーレイ組み立て | `compositor.overlay_chain` | `with_enable=False` で 1 枚絵モード (旧・高精度プレビュー) にも使える |
| `renderer._write_ass` + ass フィルタ指定 | `compositor.subtitle_chain` | `fontsdir` の付与もここへ寄せる |
| `comment_decor.build_icon_chains` 呼び出し | `compositor.icon_chains` | `profile.scale` を渡す |
| `blur_overlay.build_chains` / `watermark_overlay.build_chain` 呼び出し | `compositor.blur_chains` / `watermark_chain` | 既存モジュールは既にプロキシ対応済み (§1.4) のため寸法を渡すだけ |

### 4.2 `src/timeline/proxy_render.py` (新規)

```python
# プロキシ (低解像度の確認用動画) をタイル単位で作る (ver6 resolve3 §3.5)
#
# 1 タイル = 1 回の ffmpeg 実行。ベース連結・合成・焼き込みを分けず 1 パスで作る。
# 本番 (renderer) の多パス構成は A/V 同期の実測対策が積まれているため触らない。


# タイル 1 枚を作る。戻り値: 生成したパス
#   tile_index : タイル番号
#   profile    : CanvasProfile (プロキシ寸法)
def render_tile(timeline, context, tile_index, tile_sec, profile, cfg): ...


# タイルの内容ハッシュ (§3.5)。一致すればキャッシュを再利用できる
def tile_key(timeline, context, tile_index, tile_sec, profile, settings): ...


# タイルの Timeline 範囲 (フレーム境界に乗せた秒)
def tile_range(timeline, tile_index, tile_sec, fps): ...
```

**1 タイルのコマンド構成** (`audio_source._render_pieces` の流儀 / §1.6):

```
ffmpeg -y
  # V1 の断片ごとに -ss / -t 付きの入力を並べる (入力シークを効かせる)
  -ss <source_in+off> -t <len> -i <素材>          … 断片ぶん繰り返す
  # オーバーレイの追加入力
  -ss <...> -t <...> -i <素材>
  -filter_complex "
     # ① 断片ごとに base_filters を当て、concat で 1 本へ繋ぐ
     # ② ぼかし (チェーンの先頭 / blur_overlay の R8)
     # ③ オーバーレイと字幕を z_order 順に (compositor.overlay_chain / subtitle_chain)
     # ④ 透かし (チェーンの最後)
  "
  -c:v libx264 -preset <proxy.preset> -crf <proxy.crf>
  -pix_fmt yuv420p -fps_mode cfr -r <fps>
  -c:a aac -b:a <proxy.audio_bitrate> -ar 48000 -ac 2
  -movflags +faststart
  <tile_path>
```

* **ギャップ (`gap_policy="black"`)** は `color=c=black` の lavfi 入力で埋める
  (`renderer._render_gap` と同じ寸法・fps)。
* **`enable` / `setpts` / `movie=` はタイル開始時刻ぶん引く** (§3.5)。
* タイルの尺は `tile_range` が返すフレーム数から決め、`renderer._duration_arg` と
  同じ切り捨てで `-t` に渡す (`renderer.py:184` の実測対策を共有する)。

### 4.3 `src/gui/timeline/proxy_manager.py` (新規)

```python
# プロキシのキャッシュとワーカー管理 (ver6 resolve3 §3.5)
# GUI 寄りの都合 (優先順・シグナル・キャンセル) はここへ閉じる。


class ProxyManager(QObject):

    # タイルが 1 枚できた (tile_index)
    tile_ready = Signal(int)
    # 進捗表示の更新要求 (レンダ済みバーの塗り直し)
    coverage_changed = Signal()

    def __init__(self, controller, context, work_dir, cfg, parent=None): ...

    # Timeline が変わったら呼ぶ。ハッシュを取り直し、変わったタイルを捨てて積み直す
    def invalidate(self): ...

    # プレイヘッド位置が変わったら呼ぶ。生成の優先順を組み替える
    def set_focus(self, sec): ...

    # その時刻を含むタイルのパス (無ければ None)
    def tile_at(self, sec): ...

    # レンダ済み区間の一覧 [(start, end), ...] (レンダ済みバー用)
    def coverage(self): ...

    def shutdown(self): ...
```

* ワーカーは `QThread` 1 本。`_FrameWorker` と同じ「最新の要求だけを処理する」流儀ではなく
  **キュー方式**にする (作ったタイルは捨てずに残るため)。
* 生成中のタイルの鍵が無効化されたら、そのプロセスを止めて次へ進む。
* キャッシュは `work_dir` 配下のファイル。上限 `proxy.cache_tiles` 枚を超えたら
  プレイヘッドから遠いものを消す。画面クローズ時 (`shutdown`) に全削除する。

### 4.4 `preview_panel.py` の変更

**再生 (`_play_forward`)**:

```
プロキシが在る → QMediaPlayer.setSource(タイル) で再生。
                 タイル終端で次タイルへ継ぐ (現在のチャンク継ぎと同じ仕組み)。
プロキシが無い → 従来の音声チャンク経路で再生し、映像は近似描画のまま (縮退)。
```

**停止中の表示 (`_request_frame`)**:

```
プロキシが在る → タイルからその 1 フレームを取り出して表示 (PyAV で 1 枚 seek)。
                 このとき近似描画のオーバーレイ・字幕アイテムは**隠す** (二重描画になるため)。
プロキシが無い → 従来どおり素材から 1 フレーム + 近似描画を重ねる (下書き)。
```

**削除するもの**:

* `_hq_overlay_chains` / `_hq_blur_chains` (第 3 実装 / §1.1) を削除し、
  「高精度プレビュー」ボタンは `compositor` の 1 枚絵モードを呼ぶだけにする (§4.6)。
* `play_fps` による間引き (`:859`) はプロキシ再生時には通らない。
  縮退経路のためにコードは残す。

**軽量化 (縮退経路でも効く / §1.3)**:

* `_apply_crop`: キャンバス寸法の `QPixmap` を 1 枚だけ持ち回り、
  毎フレームの確保をやめる。`_canvas_pixmap` として保持し、`fill` で再利用する。
* `_on_frame_ready`: `QImage(...).copy()` をやめる。
  `_FrameWorker` が渡す `bytes` は不変なので、参照を保持すればコピーは不要。
  (`QImage` が参照するバッファの寿命をアイテム側で持つ。)

### 4.5 透かしをプレビューへ出す

`context.watermark_required` は出力ジョブ開始時にサーバーの予約応答で決まる
(`ver5/resolve` §5.4)。Timeline 編集画面の時点では**まだ予約していない**ため、
プレビューは「今出力したら透かしが入るか」を残高から先読みする必要がある。

* `src/services/points.py` のキャッシュ済み残高と単価から
  `would_watermark()` を引き、`ProxyManager` へ渡す。
* **推測で `false` にしてはならない。** 分からないときは `true` (透かし有り) で描く。
  「入らないと思っていたら入っていた」が最も損害の大きい失敗であり、
  `renderer._confirm_blur_failure` と同じ「安全側に倒す」方針に揃える。
* 残高が変わったら (`points_indicator.balance_changed`) タイル鍵が変わるので
  自動で再レンダされる (§3.5 のハッシュに `watermark_required` を含めるため)。

### 4.6 「高精度プレビュー」の位置づけ

プロキシが入れば「停止中の絵」も出力と一致するため、ボタンの存在意義は
「**原寸で確かめる**」だけになる。

* ボタンは残す。押すと `compositor` を `profile=production` で呼び、
  1 フレームを PNG で描いて別窓に出す (現在の `_HighQualityDialog` をそのまま使う)。
* クロップと透かしを**通す** (現状の欠落を直す / §1.1)。
* ツールチップを「現在のフレームを原寸・本番設定で描き直して表示します」へ変更する。
  現在の「画面上の字幕は Qt による近似表示です」という但し書きは不要になる。

### 4.7 近似描画 (下書き) 側の小修正

プロキシが在れば表に出ないが、縮退経路と未レンダ区間では残る。
差異が大きい順に直す。

1. **字幕の影を描く** (`preview_items.py`)。ASS `Shadow=1`・`ScaledBorderAndShadow: yes` に合わせ、
   右下へ `outline_width` と同じ向きのオフセットで影を置く。
2. **アウトラインを外側だけにする**。`pen.setWidth(W)` (中心線 = 見た目 W/2) をやめ、
   `QPainterPathStroker` で幅 `2W` のストロークを作って**字より下に**描く。
   これで ASS の「字の外側に W px」と同じ太さになる。
3. **行送りを ASS へ寄せる**。`comment_decor.py:116` `text_extent` が
   `comment_bg_line_height_ratio` で行高を決めているので、
   字幕の行送りもこの値を使う (箱と文字がずれない)。
4. **色空間を明示する** (`frame_source.py`)。`frame.reformat(..., format="rgb24")` に
   `src_colorspace` を渡す。指定しないと swscale の既定 (BT.601) で変換され、
   BT.709 素材の色がわずかにずれる。

### 4.8 `renderer.py` の変更 (最小限)

* `_video_filters` → `compositor.base_filters` へ委譲。関数自体は薄い包みとして残し、
  既存テスト (`test_renderer_timing.py` / `test_vertical_render.py`) の呼び出しを壊さない。
* `_composite` のオーバーレイ組み立て → `compositor.overlay_chain` へ委譲。
* `_crop_chain` → `compositor` 側へ移動。
* **触らないもの**: `_build_segments` / `_tile_duration` / `_duration_arg` /
  `_render_base` の concat / `_finalize_base` / `_verify_base_duration`。
  ここは A/V 同期の実測対策そのものであり、本書の目的 (見え方の一致) とは無関係。

---

## 5. `setting.json`

`timeline.preview` の下へ `proxy` セクションを足す。既存キーは一切変えない。

```json
"timeline": {
  "preview": {
    "play_fps": 30,
    "proxy": {
      "mode": "tile",
      "scale": 0.5,
      "preset": "ultrafast",
      "crf": 28,
      "tile_sec": 4.0,
      "lookahead_tiles": 3,
      "workers": 1,
      "cache_tiles": 64,
      "audio_bitrate": "96k",
      "coverage_bar": true,
      "draft_overlay": true
    }
  }
}
```

| キー | 既定 | 意味 |
| --- | --- | --- |
| `mode` | `"tile"` | `"tile"` = タイル差分レンダ / `"off"` = 従来の近似描画のみ (縮退) |
| `scale` | `0.5` | 本番キャンバスに対する比。`0.25`〜`1.0` へクランプ |
| `preset` / `crf` | `ultrafast` / `28` | プロキシのエンコード。**画質を上げても一致度は上がらない** (§2.2) |
| `tile_sec` | `4.0` | タイル長 (秒)。短いほど再レンダが速いがプロセス起動回数が増える |
| `lookahead_tiles` | `3` | プレイヘッド前方の先行生成本数 |
| `workers` | `1` | 同時生成本数。増やしても速くならず編集の応答を奪うため既定 1 |
| `cache_tiles` | `64` | 保持枚数。超えたらプレイヘッドから遠い順に削除 |
| `audio_bitrate` | `"96k"` | プロキシ音声のビットレート |
| `coverage_bar` | `true` | ルーラー下のレンダ済みバーを出すか |
| `draft_overlay` | `true` | 未レンダ区間で近似描画を下書きとして出すか |

`play_fps` の既定を 15 → 30 へ上げる。これは `mode="off"` と未レンダ区間にだけ効く。
`timeline_config` (`src/timeline/builder.py:326` の `"preview"` ブロック) に
`proxy` の読み出しとクランプを追加する。`DEFAULT_SETTINGS` への追加も忘れずに行う
(`ver6/resolve2.md` §5.1 で判明した「セクションが保存で消える」欠陥を繰り返さないため)。

---

## 6. 状態と表示の対応表

| プロキシの状態 | 映像 | 音声 | レンダ済みバー | 編集操作 |
| --- | --- | --- | --- | --- |
| レンダ済み・停止中 | タイルから 1 フレーム (出力と一致) | — | 実線 | 可 |
| レンダ済み・再生中 | タイルを `QMediaPlayer` で再生 | プロキシの音声 | 実線 | 停止 (現行方針のまま) |
| 生成中 | 近似描画の下書き + 「プレビューを準備中…」 | 従来の音声チャンク | 点滅 | 可 |
| 未生成 (順番待ち) | 近似描画の下書き | 従来の音声チャンク | 無地 | 可 |
| `mode="off"` | 近似描画 (従来どおり) | 従来の音声チャンク | 出さない | 可 |
| 生成に失敗 | 近似描画 + 状態表示に理由 | 従来の音声チャンク | 無地 | 可 |

下書きを出している間は、状態表示 (`set_status`) に
「プレビューを準備中… (表示は近似です)」を出す。**近似を見ているのか
正解を見ているのかを、利用者が必ず判別できるようにする。** これが無いと
「一致させた」と言いながら実際は近似を見ていた、という最悪の混乱が起きる。

---

## 7. エラーと縮退

| 事象 | 扱い |
| --- | --- |
| ffmpeg が無い / 起動できない | `mode="off"` と同じ従来経路。状態表示に 1 回だけ案内 |
| タイル生成が失敗 (returncode != 0) | そのタイルだけ諦める。近似描画のまま。ログに ffmpeg の stderr を残す |
| 同じタイルが 3 回続けて失敗 | `mode="off"` 相当へ落とし、以降このセッションではプロキシを作らない |
| 素材が見つからない | 既存の `media_recovery` / `missing_media_dialog` に任せる。プロキシ側は該当タイルを諦める |
| ディスクが足りない | タイル生成の失敗として扱う。`cache_tiles` を超えた削除は通常どおり行う |
| `QtMultimedia` が使えない | プロキシは作るが再生はできない。停止中の 1 フレーム表示だけ使う (現在の `_MULTIMEDIA_AVAILABLE` 分岐をそのまま踏襲) |
| 巨大プロジェクト (タイル数が多い) | ハッシュ計算だけ全タイルぶん行い、生成は優先順に従う。全部作り終える必要はない |

**方針**: プロキシは「あると嬉しいもの」であり、無くても編集画面は今までどおり開いて動く。
どの失敗経路でも `mode="off"` 相当へ落ちるだけで、既存の動作は変わらない
(`docs/claude.md`「既存実装を破壊しない」)。

---

## 8. 検証

### 8.1 単体テスト

**`tests/test_compositor.py` (新規) — ここが本書の中核**

| # | 内容 |
| --- | --- |
| 1 | 同じ Timeline から `production` と `proxy(scale=0.5)` のグラフを作り、**オーバーレイの `scale=` と `overlay=` の数値がすべて 1/2 になる**こと |
| 2 | ASS のパスとフィルタ指定が**両者で同一**であること (PlayRes を換算していないこと) |
| 3 | `comment_decor` のアイコン・背景箱の座標と寸法が 1/2 になること |
| 4 | `crop.filter_chain` の `boxblur` 半径が 1/2 になること |
| 5 | `blur_chains` の `gblur=sigma=` が 1/2 になること (既存の比例計算で自動的にそうなる) |
| 6 | `watermark_chain` の `scale=` と余白が 1/2 になること |
| 7 | `scale=1.0` のとき、生成される文字列が**現行 `renderer` の出力と完全一致**すること (回帰) |

7 が重要。これが通れば「出力側の挙動は 1 文字も変わっていない」と言える。

**`tests/test_proxy_render.py` (新規)**

| # | 内容 |
| --- | --- |
| 1 | `tile_range` がフレーム境界に乗ること (`tile_sec` が fps の整数倍でない場合も) |
| 2 | `tile_key` が同じ Timeline で安定し、関係ないタイルの編集では変わらないこと |
| 3 | クリップを 1 つ動かしたとき、**重なるタイルの鍵だけ**が変わること |
| 4 | Undo で元へ戻すと鍵が元の値へ戻ること (キャッシュが効き直す) |
| 5 | `watermark_required` / ぼかしの判定 / 関係する設定値を変えると鍵が変わること |
| 6 | タイル 2 枚目以降の `enable='between(t,...)'` と `setpts` が**タイル開始ぶんオフセット**されること |
| 7 | タイル境界に跨るクリップが両タイルで正しい区間を抽出すること |

**変更するテスト**

* `tests/test_preview_playback.py`: プロキシ在り / 無しの再生経路分岐、`mode="off"` の縮退
* `tests/test_comment_decor.py`: `scale` を掛けた結果が本番の相似であること
* `tests/test_crop_layout.py`: `boxblur` 半径がキャンバス比になること
* `tests/test_settings_migration.py`: `preview.proxy` が保存で消えない回帰

実行: `python -m unittest discover -s tests`

### 8.2 手動確認

差異の確認は**必ず同じフレームを並べて**行う。片方だけ見て「だいたい合っている」は不可。

| # | 手順 | 期待 |
| --- | --- | --- |
| 1 | 横動画プロジェクトを開き、字幕・画像オーバーレイ・コメントを置く | プレビューがプロキシへ切り替わり、レンダ済みバーが伸びる |
| 2 | 任意のフレームでプレビューのスクリーンショットを取り、同じ位置を出力から切り出して並べる | 字幕の**位置・太さ・影**、オーバーレイの位置と大きさが一致 |
| 3 | 縦動画プロジェクト (クロップ `single` / 背景 `blur`) で 2 を繰り返す | **背景のぼかしがプレビューにも出る**。枠の位置が一致 |
| 4 | クロップ `split` で 2 を繰り返す | 上下の枠の比と境界位置が一致 |
| 5 | ぼかし指定のあるプロジェクトを**再生しながら**見る | 再生中もぼけて見える (赤いマーカーではない) |
| 6 | 残高 0 のアカウントでプレビューを見る | **透かしがプレビューに出る** |
| 7 | 無音カットのフェードを有効にして編集点を見る | プレビューでもフェードする |
| 8 | 10 分のプロジェクトを再生する | コマ落ちせず滑らかに再生される。音ズレしない |
| 9 | クリップを 1 つ動かす | 動かした周辺のバーだけ消え、数秒で戻る。他の区間は消えない |
| 10 | Undo / Redo を繰り返す | バーが消えたまま残らない (鍵が戻ってキャッシュが効く) |
| 11 | `proxy.mode = "off"` にする | 従来の近似プレビューで動く (既存動作の退路) |
| 12 | ffmpeg を一時的に外して開く | 画面は開き、近似プレビューで動く。案内が出る |

---

## 9. 段階導入と受け入れ基準

一度に全部は入れない。各フェーズ単独で出荷できる形にする。

### Phase 0 — 一致化と軽量化の小修正 (XS / 半日〜1 日)

対象: §4.7 (近似描画の 4 点) と §4.4 の軽量化 2 点、`play_fps` 15 → 30。

**受け入れ基準**: 既存テスト全件パス。字幕の影とアウトラインの太さが
出力と見分けにくくなる。再生が明確に滑らかになる。

### Phase 1 — `compositor.py` への集約 (M / 2〜3 日)

対象: §4.1 / §4.8 / §4.6。**この時点でプロキシは作らない。**
出力側のリファクタと、第 3 実装の削除と、高精度プレビューの欠落 (クロップ・透かし) の修正だけ。

**受け入れ基準**: `tests/test_compositor.py` の 7 番 (`scale=1.0` で現行と完全一致) が通る。
縦動画プロジェクトで高精度プレビューと出力が一致する。
出力されるコマンドが変わっていないことをログで確認する。

ここまでで「止めて確認する」用途の差異はほぼ解消する。**先にここを切ってよい。**

### Phase 2 — プロキシのタイルレンダ (L / 1 週間程度)

対象: §4.2 / §4.3 / §4.4 の再生経路 / §4.5 / §5 / §3.5。

**受け入れ基準**: §8.2 の手動確認 1〜12 が全項目パス。
`tests/test_proxy_render.py` 全件パス。`mode="off"` で従来動作へ戻れる。

### Phase 3 — 近似描画の縮退 (M / 2〜3 日)

対象: `preview_items.py` を**選択枠とドラッグハンドルだけ**へ縮退させ、
絵はプロキシに一任する。`draft_overlay` の下書きも「輪郭だけ」へ簡素化する。

**受け入れ基準**: 近似描画のコードが縮み、以後の機能追加で
「プレビューに実装し忘れる」経路が構造的に消える。

---

## 10. 要確認 / 残る論点

### 論点 1 — プロキシの既定 `scale` を 0.5 にするか 0.33 にするか

1080x1920 の 1/2 は 540x960。1 タイル 4 秒の生成時間は素材のデコードが支配的なので、
`scale` を下げても劇的には速くならない見込み。一方、字幕の細部 (アウトラインの太さ) を
確かめるには 540 幅は欲しい。**既定 0.5 を提案するが、実素材で測って決めたい。**
測定項目: 4K 素材 / 1080p 素材それぞれで `scale` 0.25 / 0.33 / 0.5 のタイル生成時間。

### 論点 2 — プロキシ音声に loudnorm を掛けるか

出力は `loudness_normalizer` を通るが、プロキシは通さない設計にしている (§2.2)。
音量感を合わせたい要望があるなら、**一度測った loudnorm のパラメータを
プロキシで再利用する** (2 pass の 1 pass 目を共有する) 形にできる。
編集中ずっと解析コストが乗るため、既定では掛けない方針を提案する。

### 論点 3 — スクラブ音声をプロキシから取るか

現在のスクラブ (`_scrub_tick`) は 60ms ごとに短いチャンクを作って鳴らしている。
プロキシから音だけ取る作りにもできるが、粒ごとに `setSource` し直す現在の実装と
噛み合わない。本書の範囲外とし、現状維持とする。

### 論点 4 — アーカイブ切り抜き画面 (`archive_timeline_dialog.py`) へも入れるか

同じ `PreviewPanel` を使っているため自動的に恩恵を受けるが、
アーカイブは 1 プロジェクトで複数クリップを扱うためタイル数が増える。
`proxy.cache_tiles` の既定で足りるかは実素材で確認したい。

### 論点 5 — ぼかしマスク動画の寸法

`blur/mask_builder` はマスク動画を「キャンバスより小さく」作り、
`blur_overlay.build_chains` が `scale` で戻している。
プロキシ寸法へ戻す場合、マスクがプロキシより大きいことがあり得る。
拡大ではなく縮小になるので実害は無いはずだが、
**輪郭の階段が目立たないか実素材で確認する**。

---

## 11. 本書が「差異を二度と生えさせない」ために置く仕掛け

設計として最も重要なのはここである。差異は必ず
「**新機能が出力側だけに実装される**」という形で再発する。

1. **計算の置き場を 1 つにする** (`compositor.py`)。`crop.py` が既にこの規約を
   宣言しており、本書はそれを合成全体へ広げるだけである。
2. **相似テストで機械的に縛る** (`test_compositor.py` の 1〜6)。
   効果を 1 つ足したら、その効果が `compositor` を通っているかをテストが落として教える。
3. **「近似を見ている」ことを画面に出す** (§6)。
   利用者が正解と下書きを区別できる限り、混乱は事故にならない。
