# Request

Timeline 画面のプレビューについて、以下を調査した。実装はまだ行わない (調査・設計の記録のみ)。

1. プレビューを表示している方法 (特に映像と字幕が表示されているとき)
2. 動画の出力方法 (FFmpeg の drawtext 等)
3. プレビュー解像度と出力解像度
4. ASS の PlayResX / PlayResY が 1920x1080 か
5. QGraphicsScene の座標範囲と QGraphicsView の変換倍率
6. Qt 側の字幕位置と ASS の `\pos(x,y)` / `\an` の基準点の照合
7. Qt 側のフォントサイズと ASS 側の Fontsize / `\fs` の照合
8. libAss によるプレビュー画面への字幕レンダリングは可能か。プレビューと出力の差異はレンダリング方式の違いが原因ではないか。libAss を使った場合の影響

# 調査結果

## 1. プレビューの表示方法

### 土台

`src/gui/timeline/preview_panel.py:254-272`

```python
self._scene = QGraphicsScene(self)
self._scene.setSceneRect(0, 0, self._canvas[0], self._canvas[1])
...
self._view.setBackgroundBrush(Qt.black)
```

`_canvas = (controller.timeline.width, controller.timeline.height)` (`preview_panel.py:166-170`)。
つまり **シーン座標 = 出力ピクセル座標**。ウィジェットへは `fitInView(KeepAspectRatio)` で縮小表示するだけ (`preview_panel.py:1418-1421`)。

### 映像 (ベースフレーム) は 2 系統

| 系統 | 仕組み |
|---|---|
| (1) 素材直読み (既定) | `PyAvFrameSource` が PyAV でプロセス内デコード (`src/timeline/frame_source.py:117-`)。常駐スレッド `_FrameWorker` が「最新の要求だけ」処理し古い要求は捨てる (`preview_panel.py:88-130`)。結果 `(w, h, rgb24)` → `QImage(Format_RGB888)` → `QPixmap` → `BaseFrameItem` (z=-1000)。拡縮は `setScale`、`QPixmap.scaled` は使わない (`preview_panel.py:607-641`) |
| (2) プロキシ (タイル) | `ProxyManager` が 4 秒タイル単位で ffmpeg 事前レンダ (`src/timeline/proxy_render.py`)。切り抜き・ぼかし・字幕・コメントアイコン・透かしまで焼き込み済み。停止中はタイルから PyAV で 1 フレーム抜き、再生中は `QGraphicsVideoItem` でタイル動画をそのまま再生 (`preview_panel.py:441-453`) |

連続再生では seek し直さず前フレーム位置から読み進める (seek 約 134ms → 前進デコード 9〜11ms / `frame_source.py:112-116`)。

### 字幕は FFmpeg/libass ではなく Qt で ASS の見え方を再現

`SubtitleOverlayItem` (`QGraphicsSimpleTextItem` 派生 / `src/gui/timeline/preview_items.py:348-565`) が自前 `paint()` で描画する。

- ASS の `Fontsize` は PlayRes 単位 = キャンバス px として `font.setPixelSize()`
- 縁取り: Qt の pen (中心線ストローク) は使わず `QPainterPathStroker` を幅 2W にして字の下へ敷く
- 影: 縁込みのシルエットを ASS Shadow ぶん右下へずらす
- 行送りは `comment_bg_line_height_ratio` (既定 1.2)、行ごとの寄せは `\an` の水平成分に合わせる
- コメント役は角丸背景とアイコンを子アイテムとして持つ

→ **停止中の通常表示は「実デコードした映像」+「Qt で近似描画した字幕」の重ね合わせ**。字幕アイテムは選択・ドラッグ・右クリック操作が可能。
プロキシタイルが在るときは字幕が焼き込み済みなので `set_draft_visible(False)` で下書き描画を止め、二重描画を防ぐ。

### 高精度プレビューボタン

`preview_panel.py:1252-1345`。本番と同じ `compositor` のフィルタチェーンで ffmpeg を 1 回走らせ PNG を 1 枚生成しダイアログ表示する (切り抜き → ぼかし → オーバーレイ → ass → アイコン → 透かしの順)。これが「正解の絵」。

### 音声

`AudioChunkSource` が ffmpeg でチャンクを事前生成し、`QMediaPlayer` をマスタークロックにして再生、映像がその位置に追従する (`preview_panel.py:1066-`)。

## 2. 動画の出力方法

**drawtext は使っていない。ASS ファイル + FFmpeg `ass` フィルタ (libass)。**

- `compositor.subtitle_chain_for_items()` が ASS を書き出し、`compositor.ass_option()` が `ass='<path>':fontsdir='<settings/fonts>'` を組み立てる (`src/timeline/compositor.py:292-321`)。`fontsdir` は別プロセスの libass に追加フォントを解決させるため必須
- 字幕編集画面側のプレビューだけ `subtitles='...'` フィルタ (`src/modules/subtitle_generator.py:901-933`)

### パイプライン (`src/timeline/renderer.py`)

1. クリップ毎にセグメント書き出し — `-ss` を `-i` 前に置いた高速シーク、`-vf` に `scale=W:H:force_original_aspect_ratio=decrease, pad, setsar=1, fps, format=yuv420p` (縦動画は代わりに `crop.filter_chain` が正規化を兼ねる)、`-fps_mode cfr -r <fps>`、無音素材には `anullsrc` を合成 (`renderer.py:280-338`)
2. concat で連結
3. 合成を 1 パス — `-filter_complex` に鎖状につなぐ (`renderer.py:540-660`)
   - ぼかし (必ず先頭。後ろだと字幕・アイコンまでぼける)
   - 画像/動画オーバーレイ (`overlay` / `adelay` + `amix`)
   - `ass` (z_order 昇順にグループごと)
   - コメントアイコン (`movie=` で読むため入力本数は増えない)
   - 透かし (必ず最後)
4. エンコード: `libx264` / `preset medium` / `crf 20` / `aac` 48kHz 2ch / `-fps_mode cfr -r 60`

合成断片の計算は `src/timeline/compositor.py` だけが持ち、出力 (renderer)・プロキシ (proxy_render)・高精度プレビューが同じ関数を呼ぶ。相似性は `tests/test_compositor.py` が機械的に検査する。

## 3. プレビュー解像度と出力解像度

### 出力解像度 (`src/settings/setting.json`)

| 項目 | 値 |
|---|---|
| 横動画 | `ffmpeg.output_width/height` = 1920x1080、`output_fps` = 60 |
| 縦動画プロジェクト | `vertical.output_width/height` = 1080x1920 |
| 実体 | `timeline.width/height` (プロジェクト作成時に上記からコピー / `builder.py:573-579`) |

### プレビュー解像度

| 場面 | 解像度 |
|---|---|
| シーン座標系 (字幕・画像の配置基準) | 出力解像度そのまま。画面上の見た目サイズは `fitInView` で縮小表示されるだけ |
| 停止中のフレーム | 素材の原寸でデコードし `setScale` でキャンバスへフィット (縮小指定なし) |
| 再生中のフレーム | `timeline.preview.playback_width` = 960 px 幅へ縮小デコード (素材幅が 960 以下なら原寸)。`play_fps` = 30、`playback_rate` = 2.0、`playback_skip_frame` = none |
| プロキシ | `timeline.preview.proxy.scale` = 0.5 → 960x540 (縦なら 540x960)。**fps は本番と同じ** (解像度だけ落とす。fps を変えるとフレーム量子化が崩れるため)。`crf 28` / `preset ultrafast` / タイル 4 秒 |
| 高精度プレビュー | 原寸 (= 出力と同じ) の PNG 1 枚 |
| プレビュー音声 | 24000 Hz / 1ch / `pcm_s16le` (`audio_chunk_sec` 30、起動用 6 秒) |

### 未使用の設定 (要整理)

`timeline.preview.width` = 960 は設定・検証 (`builder.py:437`) に存在し「プレビュー描画の基準幅」とコメントされているが、**現在のコードからは参照されていない**。フレーム取得に実際に効くのは `playback_width` のみで、設定画面で変えても停止中の解像度は変わらない。

## 4. ASS の PlayResX / PlayResY

**ハードコードではなく `timeline.width / height` がそのまま入る。横プロジェクトなら 1920x1080 で正しい。**

`src/modules/subtitle_generator.py:754-764`

```python
def build_subtitle_file(timeline, font_profile, output_path,
                        video_width=1920, video_height=1080, subtitle_cfg=None):
    header = ("[Script Info]\nScriptType: v4.00+\n"
              f"PlayResX: {video_width}\n"
              f"PlayResY: {video_height}\n"
              "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n" ...)
```

呼び出し元は全て明示指定で、既定の 1920x1080 には落ちない (`compositor.py:307-312` / `subtitle_generator.py:1298` / `subtitle_preview_widget.py:338` / `clip_writer.py:553`)。

現在の `setting.json` で実際に生成した結果:

| プロジェクト | PlayResX / PlayResY |
|---|---|
| 横 | **PlayResX: 1920 / PlayResY: 1080** |
| 縦 (vertical) | PlayResX: 1080 / PlayResY: 1920 |

**重要な設計点**: `compositor.subtitle_chain_for_items()` はプロキシ生成時も本番寸法を PlayRes に入れる (`compositor.py:305-313` の「唯一の例外は ASS (PlayRes)」)。960x540 のプロキシフレームに PlayRes 1920x1080 の ASS を焼くと libass が `frame/PlayRes` 倍率で追従し、`ScaledBorderAndShadow: yes` により縁・影も一緒に縮む。結果として **Qt シーン座標・ASS PlayRes・出力フレームの 3 つが同じ座標系で揃っている**。

## 5. QGraphicsScene の座標範囲と QGraphicsView の変換倍率

- **座標範囲**: `sceneRect = (0, 0, 1920, 1080)` = PlayRes と完全一致
- **変換倍率**: `fitInView(KeepAspectRatio)` のみ。ホイールズーム等のハンドラは無く `AnchorUnderMouse` は実質未使用 → 変換は等倍スケール + 平行移動だけ (回転・せん断なし)

実測 (1920x1080 のシーン):

| ウィジェット | viewport | m11 | m22 | `min(vp/W, vp/H)` | `min((vp-4)/W, (vp-4)/H)` |
|---|---|---|---|---|---|
| 800x450 | 798x448 | 0.411111 | 0.411111 | 0.414815 | **0.411111** |
| 800x450 (バー有時) | 784x434 | 0.398148 | 0.398148 | 0.401852 | **0.398148** |
| 640x360 | 624x344 | 0.314815 | 0.314815 | 0.318519 | **0.314815** |

- m11 == m22 (アスペクト比は崩れていない)
- 倍率は理論値より常に約 0.9% 小さい。原因は Qt の `fitInView` が**片側 2px (上下左右計 4px) の余白を内部で引く**ため。`(viewport_h - 4) / 1080` と完全一致する。実害はほぼ無いが、「画面上で測った px x 倍率」で逆算すると微小にずれる
- スクロールバーは描画が落ち着いた後は非表示

## 6. Qt 側の字幕位置 vs ASS の `\pos` / `\an`

### 変換式は同一

| | 実装 |
|---|---|
| ASS | `px = (x+1)*W/2` / `py = (1-y)*H/2` (`subtitle_generator.py:657-658`) |
| Qt | `normalized_to_pixel()` = `((x+1)*W/2, (1-y)*H/2)` (`preview_items.py:129-132`) |

### 基準点の対応

| 役割 | ASS | Qt (`_place` / `anchor_scene_point`) |
|---|---|---|
| streamer / sub | `\an5\pos(px,py)` (中央) | `left = cx - w/2, top = cy - h/2` → 矩形中心 = (cx,cy) |
| comment | `\an4\pos(px,py)` (左中央) | `left = cx, top = cy - h/2` → 矩形左中央 = (cx,cy) |

実測 (同一クリップで ASS と Qt を両方生成して照合):

```
pos_x=0.0,  pos_y=-0.5          → ASS {\an5\pos(960.0,810.0)}   Qt anchor=(960.0,810.0)
pos_x=0.25, pos_y=0.25          → ASS {\an5\pos(1200.0,405.0)}  Qt anchor=(1200.0,405.0)
pos_x=-0.5, pos_y=0.0 (comment) → ASS {\an4\pos(480.0,540.0)}   Qt anchor=(480.0,540.0)
縦: (540,1440) / (675,720) / (270,960) も全て一致
```

### 位置指定なし (Style の Alignment + Margin)

| ケース | ASS Style | Qt 実測 |
|---|---|---|
| 横 `Alignment=2, MarginV=60` | 下中央、下端から 60px | `pos=(635,708)` + `h=312` → 下端 1020 = 1080-60、中心 X 960 |
| 縦 `Alignment=5, MarginV=320` | 中央、MarginV は libass が無視 | `pos=(315,852)` + `h=216` → 中心 Y 960 = 1920/2 (margin_v 未使用) |

式の上では一致している。実際のズレは §8 の実測を参照。

## 7. Qt のフォントサイズ vs ASS の Fontsize / `\fs`

| | 実装 | 実測 (横/縦) |
|---|---|---|
| ASS Style Fontsize | `font_profile.size` (`subtitle_generator.py:196`) | 130 / 90 |
| Qt | `font.setPixelSize(int(clip.font_size or font_profile.size or 48))` (`preview_items.py:361-363`) | 130 / 90 |
| クリップ個別 | `\fs72` (`subtitle_generator.py:722-724`) | Qt `pixelSize=72` |

値としては一致している。`ScaleX/ScaleY = 100` で無補正、PlayRes = 出力フレーム寸法なので libass 側の `frame/PlayRes` 倍率も 1。
ただし **「同じ数値 = 同じ字の大きさ」ではなかった** (§8 原因①)。

付随する装飾:

| 項目 | ASS | Qt |
|---|---|---|
| Bold | `-1` | `font.setBold(True)` 一致 |
| Outline | `10` | `QPainterPathStroker(width=20)` を字の下へ敷く = 外側 10px 一致 |
| Shadow | `1` | `ass_shadow_px()` を共有 (定義元を 1 箇所に集約) 一致 |
| WrapStyle | `2` (自動折返しなし) | `QGraphicsSimpleTextItem` も自動折返しなし、`\N`→`\n` のみ 一致 |
| Spacing | `0` | **Qt 側に該当処理なし (未反映)**。現在 0 なので実害はないが、字間を設定するとプレビューにだけ反映されない |

## 8. プレビューと出力の差異 — 実測と根本原因

同じ ASS・同じフォント・同じ設定で、**libass が描いた画素**と **Qt 近似が描いた画素**を同条件で比較した (追加フォント登録済み = 実アプリと同条件、1920x1080、`Outline=10`)。

| ケース | libass ink | Qt ink | 中心のズレ | サイズのズレ |
|---|---|---|---|---|
| 1行 `\pos(960,540)` | 615x127 | 662x136 | dx +0.5 / dy -0.5 | 幅 +47 / 高 +9 |
| 2行 `\pos(960,540)` | 619x254 | 667x289 | dx 0.0 / dy -0.5 | 幅 +48 / 高 +35 |
| 3行 `\pos(960,540)` | 128x387 | 137x448 | dx +0.5 / dy -0.5 | 幅 +9 / 高 +61 |
| 1行 位置なし (align2) | 615x127 | 662x136 | dx +0.5 / **dy -13.5** | 幅 +47 / 高 +9 |
| 2行 位置なし (align2) | 619x254 | 667x289 | dx 0.0 / **dy -26.5** | 幅 +48 / 高 +35 |

### 原因(1) 文字が約 8% 大きい — 最大の差異

縁取り幅を変えて張り出し量を分離したところ、**縁取りの描画は完全に一致**していた:

| Outline | libass の張り出し | Qt の張り出し |
|---|---|---|
| 3 | +3.0 px | +3.0 px |
| 10 | +10.0 px | +10.0 px |
| 20 | +20.0 px | +20.0 px |

差は**グリフ形状そのもの**。`Outline=0` で libass **595x107** に対し Qt **642x116** — 比率 **1.079 / 1.084**。

フォント (`07にくまるフォント.otf`) のメトリクスを直接読むと:

```
unitsPerEm           = 1000
hhea  asc/desc       = 860 / -140   → (asc-desc)/upem = 1.0000
OS/2  winAsc/winDesc = 860 /  220   → (winAsc+winDesc)/upem = 1.0800  ★
```

実測比 1.079〜1.084 と **1.0800 が一致**する。これは libass (VSFilter 互換) が **Fontsize を em ではなく「`usWinAscent + usWinDescent` が Fontsize になる」ようスケールする**挙動による。

| | 130 指定時の実 em サイズ |
|---|---|
| libass | 130 x 1000/1080 = **120.4 px** |
| Qt `setPixelSize(130)` | **130 px** |

→ **プレビューの文字は出力より常に約 8% 大きい**。PlayRes も `\pos` も `\fs` も一致していたのに見た目が合わない理由がこれ。
この倍率は**フォント依存** (`winAscent+winDescent == upem` のフォントなら差は出ない)。

### 原因(2) 位置指定なしの縦位置が行数に比例してズレる

`Alignment=2 / MarginV=60` で 1行 -13.5px、2行 -26.5px (約 行あたり 13px)。
Qt が行送りを `pixelSize x 1.2 = 156px` 固定で積むのに対し、libass は実フォントのメトリクスで積むため行数ぶん累積する。

### 原因(3) `Spacing` (字間) が Qt 側未反映

現在 `Spacing=0` なので実害なし。

## 9. libass でプレビューを描けるか

### 環境調査の結果

| 経路 | libass | 備考 |
|---|---|---|
| 同梱 `src/ffmpeg/ffmpeg.exe` | **有効** | `--enable-libass --enable-libfreetype --enable-libharfbuzz --enable-libfribidi --enable-fontconfig`。`ass` / `subtitles` フィルタ在り |
| PyAV 17.0.0 同梱 FFmpeg (プレビューのデコーダ) | **無し** | `ass` / `subtitles` / `drawtext` すべて `False` (実測)。**プロセス内で libass は今のままでは不可** |
| libass.dll 単体 | **無し** | 同梱 ffmpeg は `--enable-static` ビルドなので DLL が存在しない |

→ **別プロセスの ffmpeg.exe を介せば libass で描ける / プロセス内では描けない。**

### 既存の libass 経路 (2 つ)

1. **プロキシタイル** (`proxy.mode="tile"`、既定 ON) — 4 秒タイルを ffmpeg で事前レンダし、字幕は libass で焼き込み済み。タイルがある区間は `_set_draft_visible(False)` で Qt 近似を消しタイルの絵を出す (`preview_panel.py:517-535`)
2. **高精度プレビューボタン** — 本番と同一チェーンで PNG を 1 枚生成

つまり「停止してタイルが揃えば、既に出力と同じ絵が出ている」。Qt 近似が見えるのは**タイル未生成の間だけ**。
ただし `tile_key()` には字幕の文字・役割・フォント・サイズ・色・位置が全部入っている (`proxy_render.py:150-161`) ため、**字幕を 1 文字でも触れば必ずタイル作り直し** → 編集中はずっと Qt 近似を見ることになる。差異が問題になるのはこの場面。

## 10. 方式の比較 (未実装 / 検討案)

### 案A: 字幕レイヤだけを libass で描いて Qt に重ねる (本命)

透明キャンバスに `ass` フィルタだけ掛けて RGBA を得る。**動画デコードを一切含まない**のが要点。

```
ffmpeg -f lavfi -i "color=c=black@0.0:s=1920x1080:r=1,format=rgba" \
       -vf "ass='...':fontsdir='...':alpha=1" -frames:v 1 out.png
```

実測: **1 枚あたり約 94ms** (5 回 0.472 秒、プロセス起動込み)。縁・影・コメント背景まで正しく透過描画されることを確認済み。

| 影響 | 内容 |
|---|---|
| 一致 | 文字・縁・影・字形選択・シェーピングまで画素単位で出力と一致。§8 の原因(1)(2)(3) が全て消える |
| 実装の一本化 | `compositor.subtitle_chain_for_items()` / `ass_option()` をそのまま呼べるので「計算は compositor だけが持つ」既存規約を壊さない |
| キャッシュ | 字幕の内容・位置・スタイルが変わらない限り同じ絵。クリップ単位でキャッシュでき、スクラブ中は再生成ゼロ |
| 課題: 当たり判定 | PNG は選択・ドラッグできない。現在の `SubtitleOverlayItem` を「描画せず当たり判定だけ持つ不可視アイテム」として残す必要あり (`draft_visible` の仕組みを流用可能) |
| 課題: ドラッグ追従 | 94ms はドラッグ中のリアルタイム追従には遅い。ドラッグ中は Qt 近似 → 離したら libass 版へ差し替えの二段構えが必要 (プロキシと同じ「近似 → 正解」の作法) |
| 課題: 再生中 | 30fps 再生には間に合わない。再生中はプロキシタイル or Qt 近似のまま |
| 課題: メモリ | 1920x1080 RGBA = 8.3MB/枚。同時表示される字幕本数ぶん。PNG を介さず rawvideo を stdout で受ければディスク I/O も省ける |
| 課題: プロセス数 | 94ms の大半はプロセス起動。気になるなら ffmpeg 常駐化もあるが複雑化する |

### 案B: プロキシの守備範囲を広げる (追加実装ほぼ不要)

`lookahead_tiles` を増やす / `tile_sec` を短くする / 編集停止後のタイル生成 debounce を詰める。

- 既存の仕組みなので最も安全
- タイルは動画のデコード + エンコードを含むため、字幕だけ変えても 4 秒ぶん作り直し = 案 A より桁違いに重い
- 字幕ドラッグ中には原理的に追従できない

### 案C: プロセス内 libass (ctypes / PyAV 再ビルド)

- 要: `libass.dll` + freetype / harfbuzz / fribidi / fontconfig 一式の同梱と自前 ctypes バインド (メンテされた Python バインディングは存在しない)、または libass 有効な FFmpeg DLL ビルド
- 1 枚 1ms 未満でフレーム単位にリアルタイム描画でき、再生中も完全一致にできる
- 配布物とビルド管理のコストが大きく、`licenses/` のライセンス整理も要る
- 現実的には費用対効果が合いにくい

### 案D: Qt 近似のままスケール係数だけ直す (最小コスト・即効)

```python
# preview_items.py:361-363 を
font.setPixelSize(pixel_size)
# → フォントの winAscent+winDescent 基準へ換算
font.setPixelSize(round(pixel_size * upem / (win_ascent + win_descent)))
```

加えて行送りを `pixelSize x 1.2` 固定から実測 `lineSpacing` ベースへ寄せれば原因(2) も縮む。

- 数行の修正で 8% の差がほぼ消える (最大の見た目差が解消)
- `upem` / `winAscent` / `winDescent` の取得が要る (`QRawFont`、または OS/2 テーブルを直読み。OTF/TTF のパースは 40 行程度)
- あくまで別実装の近似のままで、字形選択・カーニング・シェーピングまでは一致しない。フォント差し替えのたびに検証が要る

### 推奨

**案D を先に入れて (即効で 8% 差を潰す)、本命として案A。**

案 A は `compositor` を経由するので「合成の計算は 1 か所」という既存の設計規約を守れる。字幕レイヤ単独なら動画デコードが不要でキャッシュも効くため、プロキシを重くせずに「停止中は出力と画素一致」を達成できる。
ドラッグ中だけ Qt 近似に落とす二段構えは、既に `set_draft_visible()` と「表示は近似です」のステータス表示という形で仕組みが揃っているので、新しい概念を足さずに実装できる。

## 11. 照合結果まとめ

| 確認項目 | 結果 |
|---|---|
| PlayRes | 一致。横 1920x1080 / 縦 1080x1920。プロキシでも本番寸法で統一 |
| Scene / View | シーン範囲は PlayRes と同一、等倍スケールのみ。`fitInView` が片側 2px の余白を取るため倍率が約 0.9% 小さい |
| `\pos` / `\an` | 変換式・基準点ともに一致 (実測で小数まで一致) |
| Fontsize / `\fs` | 設定値としては一致。ただし **libass と Qt でスケール基準が異なり実描画が約 8% 違う** |
| 縁取り / 影 | 一致 (Outline 3/10/20 で張り出しが ±3/±10/±20 px と完全一致) |
| 位置指定なしの縦位置 | **行あたり約 13px ずれる** (行送りの基準差) |
| Spacing | **Qt 未反映** (現設定は 0 のため無影響) |

### 残課題 (実装時に扱う)

1. 字幕の実描画サイズが約 8% 大きい (§8 原因(1)) — 案 D または案 A
2. 位置指定なしの縦位置が行数に比例してずれる (§8 原因(2)) — 案 D または案 A
3. `Spacing` が Qt 未反映 (§7)
4. `timeline.preview.width` が未参照の死に設定 (§3)

## 付録: 検証方法

検証に使ったスクリプトと生成物:

- `check_ass.py` — 現行設定で ASS を生成し PlayRes / Style 行を確認
- `check_qt.py` — Qt 側の `sceneRect` / `transform` / `text_rect` / `pixelSize` を実測
- `check_fit.py` — `fitInView` の余白とスクロールバーの挙動を確認
- `check_ink.py` — libass と Qt の実描画画素 (ink bbox) を同条件で比較
- `check_outline.py` — 縁取り幅ごとに張り出しを比較し、グリフ形状の差と縁取り描画の差を分離
- `ass_layer.png` — 字幕レイヤ単独の透過描画 (案 A の実物)

いずれもセッション用の一時ディレクトリに置いたもので、リポジトリには含めていない。
再検証が必要なら同じ手順 (現行 `setting.json` を読み、`build_subtitle_file` で ASS を作り、同梱 `ffmpeg.exe` の `ass` フィルタで透過 PNG を描き、`SubtitleOverlayItem` を offscreen で描画して ink bbox を比較) で再現できる。

---

# 追記 (2026-10-21): プレビュー品質の巻き戻し検討

## 12. 追加の要望

> 最近実装した「プレビュー画面のクオリティを上げる」処理を元に戻すかもしれない。
> とにかく**プレビュー画面と出力動画の字幕の大きさ・配置、画像を置いたときの配置・大きさが一緒なら問題ない**。
> 全然それであればプレビューは 30FPS の 540p とかでもいい。

実装はまだ行わない。以下は巻き戻しの可否を判断するための調査結果。

## 13. 前提の確認 — 「クオリティを上げる処理」の正体

該当するのは以下のコミットと設計書である。

| 対象 | 内容 |
|---|---|
| `3202df2` プレビュー画面の精度向上 | 本体の実装 (24 ファイル / `compositor.py`・`proxy_render.py`・`proxy_manager.py` を新規追加) |
| `7597763` リリースビルド v1.5.6 | 上記の配布 |
| `docs/request/ver6/resolve3.md` | 設計書。表題は「**プレビューと出力の差異をなくし**、プレビューを滑らかにする」 |

**重要**: これは画質を上げる実装ではなく、**プレビューを出力と一致させるための実装**である。
`ver6/resolve3.md §2.1` が受け入れ基準として挙げているのは、まさに今回の要望と同じ項目である。

```
### 2.1 一致させる (受け入れ基準に入れる)
* 配置: オーバーレイ・字幕・コメントアイコン・透かしの位置
* 大きさ: 拡大率・フォントサイズ・アイコン寸法・背景箱の寸法
* 描画: 字幕のグリフ・アウトライン・影 (libass を通すため)
* 効果: ぼかし (対象・強さ・輪郭)、クロップの背景、フェード
* 時間: どのフレームに何が写っているか
```

そして `§2.2` で「**一致させない (意図的)**」としているのが解像度・エンコード品質である。

```
### 2.2 一致させない (意図的)
* 解像度: プロキシは既定でキャンバスの 1/2 (1080x1920 → 540x960)
* エンコード品質: プロキシは ultrafast / 高 CRF
```

つまり **この実装の設計方針は、今回の要望とまったく同じ線引きで既に作られている**。
したがって「クオリティを上げる処理を戻す」= `3202df2` を巻き戻すと、要望の前段 (大きさ・配置の一致) そのものが失われる。巻き戻しは要望と逆行する。

## 14. 要求水準 (30FPS / 540p) は現行設定で既に満たされている

| 項目 | 現行値 | 1920x1080 換算 | 要求 (30fps / 540p) |
|---|---|---|---|
| `timeline.preview.proxy.scale` | 0.5 | **960x540 = 540p** | 満たす |
| `timeline.preview.playback_width` | 960 | **960x540 = 540p** | 満たす |
| `timeline.preview.play_fps` | 30 | **30fps** | 満たす |
| `timeline.preview.proxy.preset` / `crf` | ultrafast / 28 | 低品質・高速 | 満たす |

→ **解像度・fps の面では、要求より重い設定は使っていない。**
「画質を落として軽くする」余地は既に使い切られており、ここを下げても得るものは少ない。

残る「原寸」はプロキシ未生成時の停止フレームのみ (素材を原寸デコードして `setScale` でキャンバスへ収める)。
`frame_source.PyAvFrameSource._scaled_width()` は**再生中だけ** `playback_width` へ縮小する実装のため、停止中は原寸で取る。ここを常時 540p にすることは可能だが、停止中こそ字幕位置を確認する場面なので画質を落とす利点は小さい。

### 唯一「30fps になっていない」もの

プロキシタイルの**エンコード fps は本番と同じ 60** である (`CanvasProfile.proxy()` が `ffmpeg_runner.get_output_fps()` をそのまま使う)。
これは `ver6/resolve3.md §3.2` の意図的な判断で、理由は「軽くしたいのは解像度であって時間軸ではなく、fps を変えるとフレーム量子化の結果が変わって**どのフレームに何が写っているか**が崩れる」。

30fps へ落とすこと自体は可能だが、`§2.1` の「時間の一致」を捨てることになり、カット境界が 1 フレームずれて見える場合が出る。
今回の要望 (大きさ・配置が一致すればよい) の範囲では許容できる可能性があるが、**「カットの切れ目がプレビューと出力で 1 フレーム違う」を受け入れるかどうかの判断が要る** (未決事項 Q1)。

## 15. 画像オーバーレイの配置・大きさの一致状況

要望にある「画像を置いたときの配置・大きさ」について、出力側とプレビュー側の計算を突き合わせた (コード読みによる照合)。

| | 出力 (`compositor.overlay_chain` / `compositor.py:219-273`) | プレビュー (`ImageOverlayItem` / `preview_items.py:242-275`) |
|---|---|---|
| 大きさ | `target_w = int(profile.width * transform.scale)` → `scale={target_w}:-2` | `target_width = int(canvas_size[0] * base_scale)` → `pixmap.scaledToWidth(target_width)` |
| 位置 | `pos_x = (x+1)*W/2` / `pos_y = (1-y)*H/2` → `overlay={pos_x}-w/2:{pos_y}-h/2` (中心基準) | `normalized_to_pixel(x, y)` → `setPos(cx - w/2, cy - h/2)` (中心基準) |
| 不透明度 | `colorchannelmixer=aa=<opacity>` | `setOpacity(opacity)` |

**式は一致している。** 差は出力側の高さが `-2` (アスペクト維持 + 偶数丸め) であるのに対しプレビューは丸めない点だけで、**最大 1px**。実用上は無視できる。

つまり**画像オーバーレイについては、プロキシが無い Qt 近似の状態でも既に一致している**。
字幕のような約 8% のズレ (§8) は画像には発生しない。フォント固有のスケール基準の差が原因だったためである。

## 16. 結論 — 差異の原因は解像度ではなく Qt 近似描画

§8 で実測したとおり、現在の差異は次の 2 つに集約される。**いずれも解像度とは無関係**である。

| 差異 | 原因 | 画像オーバーレイ |
|---|---|---|
| 字幕が約 8% 大きい | libass が Fontsize を `usWinAscent+usWinDescent` 基準でスケールするのに対し Qt は em 基準 | 発生しない |
| 位置指定なしの字幕が行あたり約 13px 上へずれる | 行送りが `pixelSize x 1.2` 固定 vs 実フォントメトリクス | 発生しない |

そして**この差異が見えるのは、プロキシタイルが未生成で Qt 近似 (下書き) を表示している間だけ**である。
タイルが生成されれば libass で焼いた絵がそのまま出るため、大きさも配置も一致する。
ただし `tile_key()` は字幕の文字・位置・スタイルを全てハッシュに含む (`proxy_render.py:150-161`) ので、**字幕を編集した直後は必ず Qt 近似が出る**。

→ **要望を満たす正攻法は「プロキシを捨てる」ではなく「Qt 近似をやめる (= 字幕も libass で描く)」である。** §10 の案 A / 案 D がこれに当たる。

## 17. 取りうる選択肢

### R1. 巻き戻さない + 案D (最小コスト / 推奨)

`preview_items.py` の `setPixelSize()` をフォントメトリクス基準へ換算し、行送りを実測 `lineSpacing` ベースへ寄せる (§10 案 D)。

- 変更は 1 ファイル数行 + フォントメトリクス取得
- Qt 近似の状態でも字幕の大きさ・配置がほぼ一致する
- プロキシ・compositor はそのまま = 一致の仕組みを壊さない
- 画像は既に一致しているので追加対応不要

### R2. 巻き戻さない + 案A (本命 / 中コスト)

字幕レイヤだけを libass で描いて Qt に重ねる (§10 案 A、実測 94ms/枚・キャッシュ可)。

- 字幕が画素単位で一致する
- 当たり判定を不可視の Qt アイテムへ逃がす実装が要る
- ドラッグ中は近似 → 離したら差し替えの二段構え

### R3. 巻き戻す (非推奨)

`3202df2` を revert すると以下が失われる。

- `compositor.py` による合成計算の一元化 → 配置の計算が renderer / preview / proxy の 3 箇所へ戻る (`ver6/resolve3.md §1.1` が指摘していた状態)
- プロキシタイル = libass 焼き込み済みの絵 → **字幕の一致が常時失われ、§8 の約 8% 差が編集中ずっと出る**
- 縦動画プロジェクトの切り抜きをプレビューへ通す処理 → 横フレームが縦キャンバスの中央に小さく出るだけの状態へ戻る (`preview_panel.py:643-669` のコメント参照)
- 透かしのプレビュー表示
- `tests/test_compositor.py` (569 行) / `tests/test_proxy_render.py` (465 行) の相似性検査

**要望は「一致していれば画質は低くてよい」であり、巻き戻しは「画質も一致も失う」結果になる。** 画質を下げたいだけなら §14 のとおり設定値で済む (そして現行値が既に要求水準)。

### R4. 軽量化だけしたい場合に触る設定 (コード変更不要)

| 設定 | 現行 | 軽くする方向 | 影響 |
|---|---|---|---|
| `proxy.scale` | 0.5 | 0.25 (下限) | 480x270。`PROXY_SCALE_MIN=0.25` でクランプされる |
| `proxy.lookahead_tiles` | 3 | 1〜2 | 先読み減 = CPU 減 / 待ち増 |
| `proxy.workers` | 1 | 1 のまま | 既に最小 |
| `proxy.tile_sec` | 4.0 | 2.0 | 1 タイルが軽くなるがプロセス起動回数が増える |
| `playback_width` | 960 | 540 | 再生中のデコードが軽くなる |
| `proxy.cache_tiles` | 64 | 16〜32 | ディスク使用量が減る |

**fps を下げる場合のみコード変更が必要** (`CanvasProfile.proxy()` が `output_fps` を参照しているため)。時間軸の一致を捨てる判断が前提 (Q1)。

## 18. 未決事項

| # | 内容 |
|---|---|
| Q1 | プロキシの fps を 30 へ落とすか。落とすと「どのフレームに何が写っているか」の一致 (`ver6/resolve3.md §2.1` の受け入れ基準) を捨てることになり、カット境界が 1 フレームずれて見える場合がある。大きさ・配置だけで良いなら許容できるが、無音カットの切れ目確認に影響する可能性がある |
| Q2 | 停止中のフレームも 540p へ落とすか。字幕位置の確認は停止中に行うため、画質を落とす利点は小さいと考えるがどうか |
| Q3 | §8 原因(1)(2) への対応は R1 (案 D / 安い近似の改善) と R2 (案 A / libass 一本化) のどちらを採るか |
| Q4 | `timeline.preview.width` の死に設定 (§3) を削除するか、`playback_width` と統合するか |

## 19. この追記の保存先について

ユーザー指示は「request7.md に追記」だったが、`docs/request/ver8/` に `request7.md` は存在しない (ver8 は request5 / request6 / request8 で、7 は欠番)。
本件は本書 (`request8.md`) で調査した「プレビューと出力の差異」の続きであるため、本書へ追記した。
独立した `request7.md` として分けたい場合は §12 以降を切り出すこと。

---

# 追記2 (2026-10-21): 滑らか化に伴う操作性の不具合 (調査のみ / 未実装)

## 20. 報告された不具合

> 滑らかにしようとした結果、以下のような操作性が著しく悪くなっている。
>
> * (A) おそらくクリップの境界で画面が一瞬暗転し、画面がチカチカしているように見えて非常にストレス
> * (B) 再生ヘッドを指定した位置において Space を押して再生すると、再生ヘッドがかなり前の位置に移動する (戻る)。そして再生はその戻った位置から行われる

## 21. 不具合(B): 再生開始で再生ヘッドが戻る — 原因を特定

### 21.1 結論

**`QMediaPlayer` の `positionChanged` が、シーク完了前に `position=0` を 1 回通知し、それをそのまま再生ヘッドへ書き戻しているため。**
戻り先は「いま `QMediaPlayer` に載せたファイルの先頭に対応する Timeline 時刻」= `self._chunk_start` になる。

### 21.2 該当コード

`preview_panel.py:962-977` — `_start_playback()`

```python
def _start_playback(self, path, chunk_start, from_proxy=False):
    rate = self._pending_rate or 1.0
    self._chunk_start = float(chunk_start)
    self._last_frame_at = None
    offset_ms = int(max(self._controller.playhead() - self._chunk_start, 0.0) * 1000)
    self._set_proxy_playing(from_proxy)
    self._player.setSource(QUrl.fromLocalFile(path))   # ← 非同期ロード
    self._player.setPosition(offset_ms)                # ← ロード完了前なので効かない
    self._player.setPlaybackRate(rate)
    self._player.play()
    self._set_rate(rate)                               # ← ここで _rate > 0 になる
```

`preview_panel.py:1066-1073` — `_on_audio_position()` (`positionChanged` に接続 / `preview_panel.py:339`)

```python
def _on_audio_position(self, position_ms):
    if self._rate <= 0:
        return
    sec = self._chunk_start + position_ms / 1000.0
    ...
    if self._proxy_playing:
        self._controller.set_playhead(sec)   # ← position_ms=0 なら chunk_start へ戻る
        return
```

### 21.3 機構

2 つが重なっている。

| # | 機構 |
|---|---|
| B-1 | `setSource()` 直後に `setPosition()` を呼んでいるが、`QMediaPlayer` のソース読み込みは**非同期**で、この時点では `mediaStatus` が `LoadingMedia`・`isSeekable()` が false のためシークが無視される。`mediaStatusChanged` → `LoadedMedia` を待って seek し直す処理は**存在しない** (`git log -S "LoadedMedia"` / `-S "isSeekable"` ともにヒット 0 件 = 一度も実装されたことがない) |
| B-2 | `_start_playback()` は同一スロット内で `_set_rate(rate)` まで走り切るため、`positionChanged` が配送される頃には必ず `self._rate > 0` になっている。したがって `_on_audio_position` の `if self._rate <= 0: return` ガードは**新ソース読み込み直後の `positionChanged(0)` を止められない**。その 1 回が `set_playhead(chunk_start + 0)` を実行する |

`_player.setSource()` は再生中に呼ばれると再生を止めて映像シンクを空にするため、**位置が戻るのと同時に絵も一度消える** (不具合 A とも関係する)。

### 21.4 戻り先の見積もり

`self._chunk_start` が何になるかは再生開始時にどの経路へ入るかで決まる (`_play_forward` / `preview_panel.py:861-891`)。

| 経路 | `chunk_start` | 戻る距離 |
|---|---|---|
| プロキシタイルが在る (`_proxy_tile(start)`) | そのタイルの開始秒 (4 秒グリッド) | **最大 4 秒** |
| 認識用音声が有効 (`AudioChunkSource.cached()` の `_initial_valid` 分岐 / `audio_source.py:78-80` が `return self._initial_audio_path, 0.0`) | **0.0** | **先頭まで (再生ヘッドの位置ぶん全部)** |
| 通常の音声チャンク (`build(start, ...)`) | 要求した `start` と一致 | 0 (症状が出ない) |

→ 「かなり前の位置に移動する」という症状は、**認識用音声が効いている状態 (`chunk_start = 0.0`) なら先頭へ、プロキシ再生なら最大 4 秒手前へ**戻ることに対応する。

### 21.5 なぜ今になって顕在化したか

従来の経路 (`_request_chunk(start)` → `build(start, ...)`) は **再生ヘッド位置ちょうどからチャンクを作っていた**ため `offset_ms == 0` となり、シークが効かなくても症状が出なかった。

`3202df2` でプロキシ再生 (`_proxy_tile(start)` → `_start_playback(tile[0], tile[1], from_proxy=True)`) が追加され、**再生ヘッドより手前から始まるファイルを載せる経路ができた**ことで、潜在していた B-1 / B-2 が表に出た。
`_initial_valid` 経路 (`chunk_start = 0.0`) も同じ構造のため、プロキシを切っても条件次第で再現する可能性がある。

### 21.6 修正方針の候補 (未実装)

| 案 | 内容 |
|---|---|
| B-fix-1 | `_on_audio_position` に「シーク完了までは再生ヘッドを書き戻さない」ガードを足す。`_start_playback` で目標位置 `self._seek_target_sec` を立てておき、`sec` がそれに達するまで `set_playhead` を呼ばない (達する前の通知は捨てる) |
| B-fix-2 | `mediaStatusChanged` で `LoadedMedia` / `BufferedMedia` を受け、そこで `setPosition(offset_ms)` をやり直してから `play()` する。`_on_audio_status` は既に `mediaStatusChanged` に繋がっているので受け口は在る |
| B-fix-3 | 両方 (B-fix-2 で実際に正しい位置から鳴らし、B-fix-1 で取りこぼしを防ぐ)。**推奨** |

いずれも `preview_panel.py` 内で閉じる。`compositor` / `proxy_render` には触らない。

## 22. 不具合(A): クリップ境界の暗転・チカチカ — 候補の切り分け

(B) と違い、**単一の原因に特定できていない。** 再現操作 (再生中か停止中か) で該当する機構が変わるため、候補を機構ごとに挙げ、切り分け手順を示す。

### 22.1 候補一覧

| # | 機構 | 発生条件 | 周期 | 確度 |
|---|---|---|---|---|
| A-1 | **タイル切り替えの暗転**。`_on_audio_status(EndOfMedia)` → `_start_playback(次タイル)` → `setSource()`。新ファイルのロード中は `QGraphicsVideoItem` に出すフレームが無く黒。さらに `_set_proxy_playing(True)` が `setVideoOutput()` を**再度**呼ぶためシンクが張り直される | プロキシ**再生中** | `tile_sec` ごと = **4 秒** | 高 |
| A-2 | **タイル有無の境界で表示経路が切り替わる**。`_request_frame()` はタイルの有無で (a) 絵の出どころを タイル(960x540) ↔ 素材原寸、(b) 字幕下書きを 非表示 ↔ 表示 に切り替える (`preview_panel.py:517-542`)。未レンダ区間との境目で**字幕が出たり消えたり、絵の精細さが変わる** | **停止中・スクラブ中** | タイル境界 = 4 秒 | 高 |
| A-3 | **タイル 1 枚の完成ごとに全オーバーレイを作り直す**。`_on_tile_ready()` は**タイル番号を見ずに** `_refresh_all()` を呼び (`preview_panel.py:428-432`)、`_rebuild_overlays()` が全アイテムを `removeItem` → 再生成する (`preview_panel.py:710-713`)。`lookahead_tiles=3` のため編集・シークのたびに連続で発火する。再生成には動画オーバーレイの **GUI スレッド同期デコード** (`_overlay_pixmap` → `frame_at` / `preview_panel.py:753-766`) が含まれる | **停止中** (再生中は `is_playing()` で抜ける) | タイル完成のたび | 高 |
| A-4 | **フレーム取得失敗で黒**。`_on_frame_failed` → `BaseFrameItem.show_blank()` がキャンバス全面を黒で塗る (`preview_items.py:599-605`)。クリップ境界では素材か `source_sec` が不連続になり `sequential_decode_sec=2.0` の前進デコードが外れて seek が要る。`_read_forward` が届かなければ `None` が返る | 停止中・非プロキシ再生中 | クリップ境界 | 中 |
| A-5 | **デバウンスが効いていない**。`timeline.preview.update_debounce_ms`(60) は `builder.py:438` と `settings_window.py:519` で**定義・検証されているだけで、どこからも参照されていない**。`_on_playhead_moved` は毎回同期で `_request_frame()` + `_rebuild_overlays()` を呼ぶ (`preview_panel.py:395-400`) | 常時 | 再生ヘッドが動くたび | 確定 (死に設定) |
| A-6 | **再生開始/停止でフレームキャッシュが全無効化**。`set_playing()` が `_decoders` / `_positions` をクリアし、`_scaled_width()` の戻り値変化で**キャッシュキーも変わる** (`frame_source.py:160-200`)。停止時は `_set_rate` が `_request_frame()` で原寸を取り直す | 再生 ⇄ 停止の切り替え | 1 回ずつ | 確定 (仕様) |

### 22.2 ログで確認したこと

| 確認 | 結果 |
|---|---|
| `フレーム取得に失敗しました` (`_logger.exception` / ERROR) | 直近 3 日分のログに **0 件**。デコード例外は起きていない |
| `素材を開けませんでした` (WARNING) | **0 件** |
| `前進デコードで届かなかったため seek し直します` / `デコード終了` | `_logger.debug` のため、`logging.level = "INFO"` では**そもそも出力されない** |

**診断上の穴**: `frame_at()` が例外なしで `None` を返す経路 (= A-4 の本体) は**ログを 1 行も出さない**。`_on_frame_failed` も同様。したがって「黒くなったかどうか」を事後に確認する手段が無い。切り分けのために暫定ログを足す必要がある (下記)。

### 22.3 切り分け手順 (実装前にやること)

1. `logging.level` を `DEBUG` にして再現操作を行い、`前進デコードで届かなかった` / `デコード終了` の出現時刻と暗転のタイミングを突き合わせる
2. `_on_frame_failed` と `BaseFrameItem.show_blank()` に暫定の WARNING ログを入れ、**黒塗りが実際に起きているか**を確定させる (A-4 の切り分け)
3. 「再生中に起きるか / 停止してシークしているときに起きるか」を分ける
   - **再生中だけ** → A-1 が主因。4 秒周期かどうかを確認する
   - **停止中・シーク中も** → A-2 / A-3 / A-5
4. `timeline.preview.proxy.mode` を `"off"` にして再現するか確認する
   - **消える** → A-1 / A-2 / A-3 (プロキシ由来)
   - **残る** → A-4 (素材デコード由来)

### 22.4 修正方針の候補 (未実装)

| # | 対象 | 内容 |
|---|---|---|
| A-fix-1 | A-1 | タイルの継ぎ目を `setSource()` で切り替えるのをやめる。タイルを跨いで 1 本にする (事前連結) か、`tile_sec` を伸ばして継ぎ目の回数を減らす。最小対応は「継ぎ目で `setVideoOutput()` を呼び直さない」(すでに同じシンクなら何もしない) |
| A-fix-2 | A-1 | 新ソースのロード中は直前のフレームを `BaseFrameItem` に残したまま見せ、`LoadedMedia` を受けてから `QGraphicsVideoItem` へ切り替える (黒を見せない) |
| A-fix-3 | A-2 | タイル有無で下書きの表示を切り替えるのをやめ、**常に同じ描画経路**にする。§10 案 A (字幕も libass で描く) を入れればこの切り替え自体が不要になる |
| A-fix-4 | A-3 | `_on_tile_ready(tile_index)` で**そのタイルが現在の再生ヘッドを含むときだけ** `_refresh_all()` する。含まないなら何もしない (引数は既に渡ってきている) |
| A-fix-5 | A-3 | `_rebuild_overlays()` を「顔ぶれが変わったときだけ作り直す」へ変更する (現在は `is_playing()` のときだけこの最適化が効いている)。停止中も同じ判定を使えば、タイル完成による再生成が止まる |
| A-fix-6 | A-5 | `update_debounce_ms` を実際に使う。`_on_playhead_moved` から `QTimer` 経由で `_request_frame()` を呼ぶ (設定値が既に在るので追加の設定項目は不要) |
| A-fix-7 | A-4 | `_on_frame_failed` で黒塗りせず、**直前のフレームを残す**。ギャップ (`source_at_playhead()` が `None`) のときだけ黒にする。「取得できなかった」と「そこに映像が無い」を区別する |

**A-fix-7 と A-fix-4 / A-fix-5 は単独でも効果が見込め、かつ副作用が小さい**ため、切り分け前でも先行して入れられる。

## 23. 付随して見つかった死に設定・不整合

| # | 内容 |
|---|---|
| D1 | `timeline.preview.width` (960) — 定義・検証のみで未参照 (§3)。実際に効くのは `playback_width` |
| D2 | `timeline.preview.update_debounce_ms` (60) — 定義・検証のみで未参照 (§22.1 A-5)。デバウンスは実装されていない |
| D3 | `frame_at()` が `None` を返す経路にログが無く、黒塗りの発生を事後確認できない (§22.2) |
| D4 | `_on_tile_ready(_tile_index)` が引数を使っていない。どのタイルが完成しても全再描画する (§22.1 A-3) |

D1 / D2 は「設定画面で変えても何も起きない」状態なので、使うか消すかの判断が要る。

## 24. 未決事項 (追記2 ぶん)

| # | 内容 |
|---|---|
| Q5 | 不具合 (A) は**再生中**に起きているか、**停止してシークしているとき**にも起きているか。§22.3 の切り分けに必要 |
| Q6 | 暗転は 4 秒周期 (タイル境界) か、クリップの長さに一致しているか。前者なら A-1 / A-2、後者なら A-4 |
| Q7 | プロキシ再生中、タイルの継ぎ目で**音も途切れていないか**。A-1 は映像だけでなく音声にも 4 秒ごとのギャップを生む構造のため |
| Q8 | 不具合 (B) の戻り距離は「先頭まで」か「数秒」か。先頭までなら `chunk_start = 0.0` の認識用音声経路 (§21.4)、数秒ならプロキシタイル経路 |
| Q9 | D1 / D2 の死に設定は、使う (デバウンス実装・基準幅の反映) か、設定項目ごと消すか |

## 25. 不具合と §12〜§19 (巻き戻し検討) の関係

不具合 (A)(B) はいずれも **`compositor` / ASS / 配置計算とは無関係**で、`preview_panel.py` の再生制御と表示切り替えに閉じている。

したがって §17 の整理は変わらない。

* **(A)(B) を直すために `3202df2` を巻き戻す必要はない。** 原因はプロキシという仕組みそのものではなく、その継ぎ目の扱い (`setSource` / `positionChanged` / `_on_tile_ready`) にある
* ただし **(A) の A-1 / A-2 はプロキシ再生の構造そのものに起因する**ため、「タイルを跨いで再生する」設計 (A-fix-1 / A-fix-2) に手を入れるか、プロキシ再生をやめて音声チャンク再生へ戻す (= 映像は Qt 近似) かの選択は残る
* 後者を選ぶ場合、§16 のとおり**字幕の約 8% のズレが再生中ずっと出る**ことになるので、§10 案 A / 案 D とセットで考える必要がある
