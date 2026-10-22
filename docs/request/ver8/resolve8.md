# ver8 resolve8 — プレビューと出力を一致させる / 再生の不具合 (修正設計)

対象: `autoedit` / Timeline 編集画面のプレビュー (`src/gui/timeline/preview_panel.py`,
      `src/gui/timeline/preview_items.py`)
関連: `docs/request/ver8/request8.md` (調査記録・本書の入力),
      `docs/request/ver6/resolve3.md` (プロキシ・compositor の設計),
      `docs/request/ver3/resolve11.md` §3 (字幕の基準点), `docs/request/ver3/resolve12.md` §2.3

作成日: 2026-10-22 (main `3f15736`)
状態: **実装済み** (第2部の設計どおり。§6 の行送りの基準だけ実測で確定させた)
追補: **第3部 §19 以降** — 実装記録・ink 実測の結果・設計からの変更点

---

## 0. 要望と結論

> プレビュー画面と出力動画の**字幕の大きさ・配置**、**画像を置いたときの配置・大きさ**が
> 一致していればよい。一致していれば、プレビューは 30FPS の 540p でも構わない。
> あわせて、(A) クリップ境界で画面が一瞬暗転してチカチカする、
> (B) 再生ヘッドを置いて Space を押すと再生ヘッドがかなり前へ戻る、を直したい。

### 0.1 結論 (何を直すか)

`request8.md` の実測により、原因は**解像度ではなく描画方式**であることが確定している。
したがって**プレビューの解像度・fps は一切変えない** (下げる余地は既に使い切られている / §1.3)。
直すのは次の 5 点である。

| # | 症状 | 原因 | 本書の対策 |
| --- | --- | --- | --- |
| S1 | 字幕が出力より約 8% 大きい | libass は Fontsize を `usWinAscent + usWinDescent` 基準でスケールするが、Qt は em 基準 (`preview_items.py:361-363`) | §5 フォントメトリクス換算 |
| S2 | 位置指定なしの字幕が行あたり約 13px 上へずれる | 行送りが `pixelSize x 1.2` 固定 (`preview_items.py:391`) 対 libass の実メトリクス | §6 行送りの基準替え |
| S3 | `subtitle.spacing` (字間) がプレビューに出ない | Qt 側に該当処理が無い | §7 `setLetterSpacing` |
| B | 再生開始で再生ヘッドが戻る | `setSource()` 直後の `setPosition()` が非同期ロード中で効かず、`positionChanged(0)` を再生ヘッドへ書き戻している (`preview_panel.py:962-977` / `1066-1073`) | §8 シーク完了まで書き戻さない |
| A | クリップ境界・タイル境界の暗転とチカチカ | タイル継ぎ目の `setSource()` で映像が空になる / タイル完成ごとの全再構築 / フレーム取得失敗で黒塗り / デバウンス未実装 | §9 (A-fix-7/4/5/6/2 を採用) |

**画像オーバーレイは既に一致している** (`request8.md` §15。式が同一で差は最大 1px) ため、本書では触らない。

### 0.2 結論 (方針)

1. **出力側 (`compositor` / `renderer` / `proxy_render` / ASS) は一切変更しない。**
   出力が正解であり、プレビューを出力へ寄せる。`tests/test_compositor.py` /
   `tests/test_proxy_render.py` の相似性検査が壊れないことを担保にする。
2. `3202df2` (プレビュー精度向上) は**巻き戻さない**。巻き戻すと「大きさ・配置の一致」という
   要望の本体が失われる (`request8.md` §13・§17 R3)。
3. 字幕は `request8.md` §10 **案D (Qt 近似のスケール基準を libass へ合わせる)** を本線とする。
   **案A (字幕レイヤを libass で描いて重ねる)** は本書では**設計のみ・実装保留** (§15)。
   案D で S1・S2 が消え、残るのは字形選択・カーニングの差 (視認困難) になるため、
   94ms/枚のプロセス起動と当たり判定の作り替えを今の段階では背負わない。
4. 不具合 (A)(B) は `preview_panel.py` 内に閉じる。`compositor` / `proxy_render` には触らない。

### 0.3 実装順 (コミット単位)

| # | 内容 | 単独で入れられるか |
| --- | --- | --- |
| 1 | §8 不具合 (B) の修正 + テスト | はい (最優先・影響が局所) |
| 2 | §9 A-fix-7 (黒塗りをやめる) + A-fix-4 (タイル完成の間引き) | はい |
| 3 | §5 / §6 / §7 字幕メトリクス + テスト | はい |
| 4 | §9 A-fix-5 (オーバーレイ再構築の抑制) + A-fix-6 (デバウンス) | 2 の後 |
| 5 | §9 A-fix-2 (継ぎ目で直前フレームを残す) | 2 の後 |
| 6 | §10 死に設定の整理 | いつでも |

---

# 第1部 — 原因の確定と追加調査

## 1. 前提の再確認 (`request8.md` の要点)

### 1.1 座標系は既に揃っている

* `sceneRect = (0, 0, timeline.width, timeline.height)` = ASS の `PlayResX/Y` と一致
  (`preview_panel.py:254-272`, `subtitle_generator.py:754-764`)
* `\pos` の変換式と基準点は Qt 側と小数まで一致 (`preview_items.py:129-132`)
* `Outline` の張り出しは 3 / 10 / 20 px すべて完全一致

→ **ズレているのは座標でも解像度でもなく、グリフの描画スケールと行送りだけ**である。

### 1.2 差異が見えるのは「タイル未生成の間」だけ

プロキシタイルが在る区間は libass で焼いた絵がそのまま出る (`preview_panel.py:517-535` /
`_set_draft_visible(False)`)。しかし `tile_key()` は字幕の文字・役割・フォント・サイズ・色・位置を
すべてハッシュへ入れる (`proxy_render.py:150-161`) ため、**字幕を触った直後は必ず Qt 近似が出る**。
字幕を編集している最中こそ大きさ・配置を確認したい場面なので、ここが一致していないと要望を満たさない。

### 1.3 解像度・fps は下げる余地が無い

| 項目 | 現行値 | 1920x1080 換算 | 要求 (30fps / 540p) |
| --- | --- | --- | --- |
| `proxy.scale` | 0.5 | 960x540 | 満たす |
| `playback_width` | 960 | 960x540 | 満たす |
| `play_fps` | 30 | 30fps | 満たす |

→ 本書では解像度・fps を**変更しない**。プロキシのエンコード fps を 60 から 30 へ落とす案は
「どのフレームに何が写っているか」の一致を捨てるため**不採用** (§16 Q1)。

## 2. 原因の確定

### 2-1. 原因S1 — Fontsize のスケール基準が違う (**確定・実測あり**)

`preview_items.py:361-363`

```python
pixel_size = int(clip.font_size or font_profile.size or 48)
font.setPixelSize(pixel_size)        # ← em = pixel_size px
```

Qt の `setPixelSize(n)` は **em を n px** にする。
libass (VSFilter 互換) は **`usWinAscent + usWinDescent` が Fontsize になる**ようスケールする。

同梱フォント `src/settings/fonts/07にくまるフォント.otf` を直接読んだ結果 (本書作成時に再実測):

```
unitsPerEm (head)              = 1000
hhea  ascender/descender/gap   = 860 / -140 / 0   → height 1000
OS/2  sTypo asc/desc/gap       = 860 / -140 / 0   → height 1000
OS/2  usWinAscent/usWinDescent = 860 /  220       → 合計 1080  ★
```

| | Fontsize=130 のときの実 em |
| --- | --- |
| libass | `130 x 1000 / 1080` = **120.37 px** |
| Qt 現状 | **130 px** |

比 `1080 / 1000 = 1.0800` が、`request8.md` §8 の ink 実測比 **1.079〜1.084** と一致する。
**フォント依存**であり、`winAscent + winDescent == unitsPerEm` のフォントでは差が出ない。

### 2-2. 原因S2 — 行送りの基準が違う (**確定**)

`preview_items.py:391`

```python
self._line_height = pixel_size * _line_height_ratio(eff_cfg)   # 130 x 1.2 = 156 px
```

`comment_bg_line_height_ratio` (既定 1.2) は**コメント背景の箱の高さの推定値**のための比であり
(`comment_decor.text_extent`)、libass が実際に行を積む量ではない。
libass は実フォントのメトリクスで積むため、行数に比例して誤差が溜まる
(`request8.md` §8: 1 行 -13.5px / 2 行 -26.5px)。

**箱は推定・文字は実メトリクス**という構造は出力側 (libass + `comment_decor`) でも同じである。
よってプレビューも「箱は `comment_decor` の推定のまま・文字は実メトリクス」にするのが一致の方向であり、
`comment_decor` は**変更しない**。

### 2-3. 原因S3 — `Spacing` が未反映 (**確定・現設定では無害**)

`subtitle.spacing` は設定画面の入力欄 (`settings_window.py:2439` / `2690`) から変更でき、
ASS Style の `Spacing` へ入る (`subtitle_generator.py:201`)。
Qt 側に対応処理が無いため、**0 以外にするとプレビューにだけ反映されない**。

### 2-4. 原因B — シーク前の `positionChanged(0)` を書き戻している (**確定**)

```python
# preview_panel.py:962-977 (_start_playback)
self._player.setSource(QUrl.fromLocalFile(path))   # 非同期ロード
self._player.setPosition(offset_ms)                # ロード未完 = isSeekable() False で無視される
self._player.play()
self._set_rate(rate)                               # ここで _rate > 0 になる

# preview_panel.py:1066-1073 (_on_audio_position)
if self._rate <= 0:
    return                                         # ← 上で _rate を立てた後なので通ってしまう
sec = self._chunk_start + position_ms / 1000.0     # position_ms = 0 → chunk_start
...
self._controller.set_playhead(sec)                 # 再生ヘッドが chunk_start へ戻る
```

`LoadedMedia` を待って seek し直す処理は存在しない (`request8.md` §21.3: `git log -S "LoadedMedia"`
/ `-S "isSeekable"` ともにヒット 0 件)。

戻り先 = そのとき `QMediaPlayer` へ載せたファイルの先頭に対応する Timeline 時刻:

| 経路 | `chunk_start` | 戻る距離 |
| --- | --- | --- |
| プロキシタイル (`_play_forward` → `_proxy_tile`) | タイル開始秒 (4 秒グリッド) | 最大 4 秒 |
| 認識用音声 (`audio_source.py:78-80` が `(path, 0.0)` を返す) | 0.0 | **先頭まで** |
| 通常の音声チャンク (`build(start, ...)`) | 要求した `start` | 0 (症状が出ない) |

### 2-5. 原因A — 4 つの機構の合成 (**切り分け未完・確度順**)

`request8.md` §22.1 の候補のうち、コードから確定できるものと推定のものを分ける。

| # | 機構 | 確度 | 本書の扱い |
| --- | --- | --- | --- |
| A-3 | `_on_tile_ready(_tile_index)` が**タイル番号を見ずに** `_refresh_all()` → `_rebuild_overlays()` が全アイテムを破棄・再生成 (`preview_panel.py:428-432` / `698-742`)。再生成には動画オーバーレイの GUI スレッド同期デコードを含む (`_overlay_pixmap`) | **確定** (コードから自明) | §9 A-fix-4 / A-fix-5 |
| A-5 | `update_debounce_ms` (60) が定義・検証のみで未参照。`_on_playhead_moved` は毎回同期で `_request_frame()` + `_rebuild_overlays()` (`preview_panel.py:395-400`) | **確定** (死に設定) | §9 A-fix-6 |
| A-1 | タイル継ぎ目で `_start_playback()` → `setSource()`。ロード中は `QGraphicsVideoItem` に出す絵が無く黒。`_set_proxy_playing(True)` が `setVideoOutput()` を再度呼びシンクを張り直す (`preview_panel.py:979-997`) | 高 (再生中のみ) | §9 A-fix-2 (+ 最小対応) |
| A-4 | `_on_frame_failed` → `show_blank()` がキャンバス全面を黒で塗る (`preview_panel.py:692-695` / `preview_items.py:599-605`)。クリップ境界では前進デコードが外れるため発生しうる | 中 | §9 A-fix-7 |
| A-2 | タイル有無で絵の出どころ (タイル 960x540 ↔ 素材原寸) と字幕下書きの表示が切り替わる (`preview_panel.py:517-542`) | 高 (停止・スクラブ中) | §5/§6 で切替時の段差が縮む。根本解決は案A (§15) |

**診断上の穴 (D3)**: `frame_at()` が例外なしで `None` を返す経路と `_on_frame_failed` は
ログを 1 行も出さない。黒塗りが実際に起きたかを事後に確認できない。→ §9 A-fix-7 でログを足す。

## 3. 追加調査 (本書で新たに確認したこと)

### 3-1. フォントの縦メトリクスは Qt から取得できる (**実測済み**)

案D の前提は「`unitsPerEm` と `usWinAscent/usWinDescent` を実行時に取れること」である。
`QRawFont.fontTable()` で生テーブルが読めることを、本プロジェクトの PySide6 で確認した。

```python
rf = QRawFont.fromFont(font)          # QFont から
head = bytes(rf.fontTable("head"))    # 54 bytes
os2  = bytes(rf.fontTable("OS/2"))    # 96 bytes
upem = struct.unpack(">H", head[18:20])[0]              # 1000
win_asc, win_desc = struct.unpack(">HH", os2[74:78])    # 860 / 220
```

* `QRawFont.fontTable` の存在: **True** (PySide6 / `QT_QPA_PLATFORM=offscreen` で確認)
* アプリ追加フォント (`QFontDatabase.addApplicationFont` 済み) でも読める
* **フォントファイルのパスを知る必要が無い** = システムフォント (游ゴシック等) でも同じ経路で取れる

→ 当初候補だった「`settings/fonts` を自前でパースして family → path の表を作る」は**不要**。
`register_fonts_in_dir()` (`settings_window.py:1344`) にも手を入れない。

### 3-2. 行送りの残差 (**要再計測。実装時の確認事項**)

hhea / sTypo の `ascender - descender + lineGap` はいずれも 1000 (= upem) で、
libass の em (120.37px) に換算すると **行送り 120.37px**。
一方 `request8.md` §8 の ink 実測から逆算した libass の行送りは **約 127px** (2 行 254 - 1 行 127)。
**約 6.6px/行の残差**があり、ink の二値化誤差か、libass が別の値 (例: `winAscent + winDescent`
= 130px) を使っているかの判別が付いていない。

| 候補 | Fontsize=130 での行送り | ink 実測との差 |
| --- | --- | --- |
| 現状 (`pixelSize x 1.2`) | 156.0 px | **+29** |
| hhea / sTypo 基準 | 120.37 px | -6.6 |
| `winAscent + winDescent` 基準 (= Fontsize) | 130.0 px | +3.0 |

いずれでも現状より桁で改善するが、**どちらを既定にするかは実装時に ink 再計測で決める**
(§13 T6 / `request8.md` 付録の `check_ink.py` 手順)。本書は実装を 1 行の切り替えで済む形
(`§6` の `line_advance_px()`) に設計し、判断を実装時へ渡す。

---

# 第2部 — 修正設計

## 4. 全体方針

```
出力 (正解)                    プレビュー (寄せる側)
──────────────────────────     ────────────────────────────────
ASS + libass (別プロセス)  ◀── §5/§6/§7 Qt 近似のスケール基準を libass へ合わせる
compositor が断片を一元計算 ◀── 変更しない (依存だけ)
proxy_render (タイル)      ◀── 変更しない。継ぎ目の扱いだけ直す (§9)
```

**変更するファイル**

| ファイル | 変更 |
| --- | --- |
| `src/gui/timeline/font_metrics.py` | **新規**。フォントの縦メトリクス取得と換算 (§5) |
| `src/gui/timeline/preview_items.py` | `SubtitleOverlayItem` のフォント構築・行送り・字間 (§5-§7) |
| `src/gui/timeline/preview_panel.py` | 不具合 (B)(A) の修正 (§8・§9) |
| `src/timeline/builder.py` | `timeline_config()` の `preview` セクション (§11) |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` (§11) |
| `src/settings/setting.json` | 同上 (§11) |
| `tests/test_subtitle_preview_metrics.py` | **新規** (§13) |
| `tests/test_preview_playback.py` | 追記 (§13) |

**変更しないファイル** (明示): `compositor.py` / `renderer.py` / `proxy_render.py` /
`proxy_manager.py` / `subtitle_generator.py` / `comment_decor.py` / `crop.py` /
`frame_source.py` / `audio_source.py`。

## 5. S1 — フォントスケールを libass 基準へ換算

### 5-1. 新規モジュール `src/gui/timeline/font_metrics.py`

```python
# フォントの縦メトリクスを読み、libass (VSFilter 互換) と同じ字の大きさを出す。
#
# なぜ要るか: Qt の setPixelSize(n) は em を n px にするが、libass は
# 「usWinAscent + usWinDescent が Fontsize になる」ようスケールする。
# 同じ Fontsize でも字の大きさが異なり (同梱フォントで約 8%)、プレビューだけ
# 字が大きく見えていた (ver8 resolve8 §2-1)。
#
# 出力側 (libass) は別プロセスで自前にメトリクスを読むため、ここは
# **プレビュー専用**であり、ASS の生成や compositor には一切関与しない。

# 返す値はフォント 1 つにつき不変のため、(family, bold, italic) で覚える。
def vertical_metrics(font):
    """upem / win_ascent / win_descent / hhea_height / typo_height を返す。
    読めないときは None (呼び出し側は従来どおり em 基準へ落とす)。"""

def libass_pixel_size(font, size_px):
    """ASS Fontsize (size_px) に対し、libass と同じ字の大きさになる
    Qt の pixelSize を返す。= round(size_px * upem / (win_asc + win_desc))
    メトリクスが読めない / 値が不正なときは size_px をそのまま返す。"""

def line_advance_px(font, size_px, mode, ratio):
    """1 行あたりの送り幅 (px) を返す。§6 参照。"""
```

実装要点:

| 項目 | 内容 |
| --- | --- |
| 取得経路 | `QRawFont.fromFont(font)` → `fontTable("head")` / `fontTable("OS/2")` / `fontTable("hhea")` |
| オフセット | `head[18:20]` = `unitsPerEm` (uint16) / `OS/2[68:74]` = `sTypoAscender,Descender,LineGap` (int16 x3) / `OS/2[74:78]` = `usWinAscent,usWinDescent` (uint16 x2) / `hhea[4:10]` = `ascender,descender,lineGap` (int16 x3) |
| 長さ検査 | `len(head) >= 20` / `len(os2) >= 78` / `len(hhea) >= 10` を満たさなければ `None` |
| 値の検査 | `upem > 0` かつ `win_asc + win_desc > 0`。満たさなければ `None` |
| 例外 | `QRawFont` が無効・テーブル無し・`struct.error` はすべて `None` へ倒し、**プレビューを落とさない** (既存の `_make_item` と同じ思想) |
| キャッシュ | モジュール内 `dict`。鍵は `(font.family(), font.bold(), font.italic())`。フォントは実行中に増減しうるが、同じ鍵で結果が変わることは無い |
| ログ | 読めなかったときだけ `_logger.debug` (1 フォント 1 回)。WARNING にはしない (システムフォントでは普通に起こりうる) |

### 5-2. `SubtitleOverlayItem` の変更 (`preview_items.py:360-396`)

```python
# 変更前
pixel_size = int(clip.font_size or font_profile.size or 48)
font.setPixelSize(pixel_size)

# 変更後
# ass_size は ASS の Fontsize (= 出力と共有する値)。位置・箱の推定はこれを使う。
# draw_size は「libass と同じ字の大きさ」になる Qt の pixelSize (§5-1)。
ass_size = int(clip.font_size or font_profile.size or 48)
draw_size = (font_metrics.libass_pixel_size(font, ass_size)
             if metrics_mode == "libass" else ass_size)
font.setPixelSize(max(int(draw_size), 1))
```

* `metrics_mode` は `timeline.preview.subtitle_metrics_mode` (§11) を
  `PreviewPanel` から渡す。`SubtitleOverlayItem.__init__` へ**キーワード引数を 1 つ追加**し、
  既定は `"libass"`。既存の呼び出し側は `preview_panel._make_item` の 1 箇所のみ
  (`preview_panel.py:737-740`)。
* `_line_widths` / `_text_width` の計測は `QFontMetricsF(font)` のまま。
  フォントが縮むので ink 幅が libass へ寄る (実測 662 → 約 615 相当)。
* `_text_path()` の `metrics.ascent()/descent()` も縮んだフォントの値になる (整合)。

### 5-3. 波及 (意図した変化)

| 対象 | 変化 | 判断 |
| --- | --- | --- |
| `text_rect()` / 選択枠 / 当たり判定 | 約 8% 小さくなる | **狙いどおり**。libass の ink に寄る |
| `anchor_scene_point()` | 中心 (コメントは左中央) の定義は不変。矩形が縮むだけで、保存される正規化座標の意味は変わらない | **データ移行は不要**。既存プロジェクトの `pos_x/pos_y` はそのまま使える |
| コメント背景の箱・アイコン | `comment_decor` の推定値を使い続ける (Qt の実測は使わない / `preview_items.py:466-470` のコメントの方針を維持) | 出力と同じ推定 = 一致 |
| 画像オーバーレイ | 無関係 | 変更なし |

### 5-4. 「元へ戻す」口

`subtitle_metrics_mode = "em"` で従来の挙動 (`setPixelSize(ass_size)`・行送り `x1.2`・字間なし) に
完全に戻る。フォントによって見え方が悪化した場合の退避口として残す。

## 6. S2 — 行送りを実メトリクス基準へ

```python
# font_metrics.line_advance_px()
#   mode="em"     : size_px * ratio      (従来。ratio = comment_bg_line_height_ratio)
#   mode="libass" : size_px              (= usWinAscent + usWinDescent ぶん)
```

**実装時の確定 (§19-2 の ink 実測)**: libass は「`usWinAscent + usWinDescent` が
Fontsize になる」ようスケールしたうえで**その量ぶん行を送る**ため、行送りは
Fontsize と等しい。§3-2 で候補にした hhea 基準 (`size_px x hhea_height / win_height`)
では 1 行あたり約 3px 足りず、位置指定なしの字幕で行数に比例したずれが残った。
`win` 基準にすると ink の高さ・中心が libass と**完全一致**する。

`SubtitleOverlayItem.__init__`:

```python
# 変更前
self._line_height = pixel_size * _line_height_ratio(eff_cfg)
# 変更後
self._line_height = font_metrics.line_advance_px(
    font, ass_size, metrics_mode, _line_height_ratio(eff_cfg))
```

* `_text_height = len(lines) * self._line_height` はそのまま。行数ぶん累積していた誤差が消える。
* **`comment_decor.text_extent` は変更しない**。箱の高さは出力と同じ推定のままにする (§2-2)。
* `mode="libass"` で `font_metrics` が `None` を返したら `size_px * ratio` に落ちる (従来値)。
  字の大きさも em 基準 (= 大きめ) のままになるため、行送りだけ詰めると行が重なる。
  **両方そろって初めて意味がある**ので、フォールバックは必ず両方まとめて戻す。

## 7. S3 — `Spacing` (字間) の反映

```python
spacing = int(eff_cfg.get("spacing", 0) or 0)      # ASS Style の Spacing と同じ値
if spacing:
    font.setLetterSpacing(QFont.AbsoluteSpacing, float(spacing))
```

* ASS の `Spacing` は PlayRes 単位の絶対値であり、PlayRes = キャンバス px なのでそのまま px で渡せる。
* `QFontMetricsF.horizontalAdvance()` は `letterSpacing` を含むため、`_line_widths` は自動で追従する。
* **要検証**: VSFilter / libass は各文字の**後ろ**に字間を入れる (行末にも 1 つ分付く) 実装のため、
  行幅が 1 字間ぶん広くなる可能性がある。Qt の `AbsoluteSpacing` も同様に末尾へ付く挙動のため
  おそらく一致するが、`spacing != 0` の設定で ink 比較を 1 件行って確認する (§13 T7)。
* 現設定は `0` のため、既定では**何も変わらない** (回帰リスクなし)。

## 8. B — 再生開始で再生ヘッドが戻る

### 8-1. 方針 (`request8.md` §21.6 の B-fix-3 = 両方入れる)

1. **ロード完了を待って seek し直す** (正しい位置から鳴らす)
2. **シークが完了するまで再生ヘッドを書き戻さない** (取りこぼしても戻らない)

### 8-2. `_start_playback()` の変更 (`preview_panel.py:962-977`)

```python
def _start_playback(self, path, chunk_start, from_proxy=False):
    rate = self._pending_rate or 1.0
    self._chunk_start = float(chunk_start)
    self._last_frame_at = None
    # 目標位置を覚える。ロードが終わるまで seek は効かず、その間に来る
    # positionChanged(0) を再生ヘッドへ書き戻すと先頭 (またはタイル頭) へ
    # 戻ってしまう (ver8 resolve8 §2-4)。
    self._seek_target_sec = max(self._controller.playhead(), self._chunk_start)
    offset_ms = int(max(self._seek_target_sec - self._chunk_start, 0.0) * 1000)
    self._seek_offset_ms = offset_ms
    self._seek_pending = offset_ms > 0          # offset 0 なら待つ必要がない
    self._set_proxy_playing(from_proxy)
    self._player.setSource(QUrl.fromLocalFile(path))
    self._player.setPosition(offset_ms)        # 既にシーク可能ならこれで足りる
    self._player.setPlaybackRate(rate)
    self._player.play()
    self._set_rate(rate)
```

### 8-3. ロード完了での seek やり直し (`_on_audio_status`)

`mediaStatusChanged` は既に `_on_audio_status` へ繋がっている (`preview_panel.py:341`)。
`EndOfMedia` 分岐の**前**に次を足す。

```python
def _on_audio_status(self, status):
    if self._rate <= 0 or self._player is None:
        return
    # ソースのロードは非同期で、setSource() 直後の setPosition() は
    # isSeekable() が False のため無視される。読み込めた時点で seek し直す。
    if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
        if self._seek_pending and self._seek_offset_ms > 0:
            self._player.setPosition(self._seek_offset_ms)
    if status == QMediaPlayer.EndOfMedia:
        ...   # 既存のまま
```

* `EndOfMedia` から次タイル・次チャンクへ継ぐときは `_start_playback()` を通るため、
  目標位置は常にそこで立て直される。
* タイル長より offset が長い (タイル境界をまたいだ要求) 場合、`setPosition` はクランプされる。
  §8-4 の到達判定が働かないままになるのを避けるため、到達判定には**時間の猶予**を設ける。

### 8-4. 到達するまで再生ヘッドを書き戻さない (`_on_audio_position`)

```python
def _on_audio_position(self, position_ms):
    if self._rate <= 0:
        return
    sec = self._chunk_start + position_ms / 1000.0
    # シーク完了前の通知 (多くは position=0) は捨てる。書き戻すと再生ヘッドが
    # チャンク先頭へ戻る (ver8 resolve8 §2-4)。
    if self._seek_pending:
        tolerance = 2.0 / max(int(self._cfg["play_fps"]), 1)
        if sec < self._seek_target_sec - tolerance:
            return
        self._seek_pending = False
    ...   # 以降は既存のまま
```

* 許容差は `play_fps` から導く (固定値を置かない / `CLAUDE.md` のハードコード禁止)。
* `pause()` でも `self._seek_pending = False` にして状態を残さない。
* `_start_silent_playback()` (タイマー再生) は `QMediaPlayer` を使わないため関与しない。
* 初期化: `__init__` に `self._seek_pending = False` / `self._seek_target_sec = 0.0` /
  `self._seek_offset_ms = 0` を追加 (テストのスタブと同じ属性)。

### 8-5. 残るケース

`audio_source` の認識用音声経路 (`chunk_start = 0.0`) では offset が大きく、ロード完了まで
最大数百 ms 再生ヘッドが止まって見える。**戻るよりは止まるほうが受け入れられる**と判断する
(戻ると編集位置を失う)。§8-3 の seek やり直しで実際の音も正しい位置から鳴る。

## 9. A — 暗転・チカチカ

確定している機構から順に入れる。切り分け (§16 Q5-Q7) の結果を待たずに入れられるものを先に置く。

### 9-1. A-fix-7 — 取得失敗で黒く塗らない (`_on_frame_failed`)

```python
def _on_frame_failed(self, job_id):
    if job_id != self._frame_job:
        return
    # 「取得できなかった」と「そこに映像が無い」は別物。前者で黒く塗ると
    # クリップ境界で暗転して見える (ver8 resolve8 §2-5 A-4)。
    # 直前のフレームを残し、ギャップのときだけ黒にする (_request_frame 側で実施)。
    _logger.warning("フレームを取得できませんでした (直前のフレームを表示します): %.3f",
                    self._controller.playhead())
```

* ギャップ (`source_at_playhead()` が `None`) の黒塗りは `_request_frame()` 側に既にあり、
  そこは**変更しない** (`preview_panel.py:536-538`)。
* 併せて D3 (診断の穴) を埋める: 上の WARNING と、`BaseFrameItem.show_blank()` を呼ぶ
  ギャップ経路に `_logger.debug` を 1 行足す。暗転の事後確認ができるようになる。

### 9-2. A-fix-4 — タイル完成で全再描画しない (`_on_tile_ready`)

```python
def _on_tile_ready(self, tile_index):
    if self.is_playing():
        return
    # いま見ている時刻のタイルでなければ、画面を作り直す理由が無い。
    # lookahead_tiles=3 のぶん先読みが完成するたびに全再構築していた
    # (ver8 resolve8 §2-5 A-3)。
    if self._proxy is not None:
        current = proxy_render.tile_index_at(self._controller.playhead(),
                                             self._proxy.tile_sec())
        if int(tile_index) != current:
            return
    self._refresh_all()
```

* `tile_index` は既にシグナルで渡ってきている (`proxy_manager.py:116` / `343` / `371`)。
* `proxy_render.tile_index_at` は `proxy_manager` が使っているものと同一 (計算の二重定義をしない)。
* 「準備中…」の案内 (`_update_proxy_status`) は `_request_frame()` の中で更新されるため、
  間引いた回では更新されない。表示中タイルが完成した回で必ず更新されるので実害はない。

### 9-3. A-fix-5 — オーバーレイは顔ぶれ・内容が変わったときだけ作り直す

現状、再生中だけ `ids == self._overlay_ids` の最適化が効く (`preview_panel.py:707`)。
停止中も同じ判定を使えるように、**内容を含む署名**へ広げる。

```python
# 作り直しの要否を決める署名 (ver8 resolve8 §9-3)
#   ・字幕: 見た目を決める値すべて (文字・役割・フォント・サイズ・色・位置・z)
#   ・画像/動画: 素材・拡大率・位置・不透明度・z
#   ・動画オーバーレイだけは「表示する時刻」も入れる (絵が時刻で変わるため)。
#     play_fps で量子化し、停止中の 1 コマ送りでは必ず変わるようにする。
def _overlay_signature(self, elements):
    ...
```

```python
def _rebuild_overlays(self):
    ...
    signature = self._overlay_signature(elements)
    if signature == self._overlay_signature_cache:
        return
    self._overlay_signature_cache = signature
    ...  # 以降は既存の破棄→再生成
```

* `ids == self._overlay_ids` による再生中の分岐は**署名判定に置き換える** (二重の仕掛けを残さない)。
  再生中は動画オーバーレイの絵が止まる既存挙動を維持するため、**再生中は署名へ時刻成分を入れない**
  (`is_playing()` のときは量子化時刻を 0 に固定する)。
* `_on_timeline_changed()` → `_refresh_all()` の経路でも、編集内容は署名に入っているので
  必要なときだけ再生成される。
* 選択状態は `_sync_selection()` が別に反映しているため署名に含めない。

### 9-4. A-fix-6 — `update_debounce_ms` を実際に使う (D2 の解消)

```python
def _on_playhead_moved(self, sec):
    if self._proxy is not None:
        self._proxy.set_focus(sec)
    self._update_time_label()        # 時刻表示は即時 (軽い・遅れると違和感が出る)
    self._schedule_refresh()         # 絵とオーバーレイはデバウンスする

# update_debounce_ms が 0 なら即時 = 従来動作
def _schedule_refresh(self):
    delay = int(self._cfg["update_debounce_ms"])
    if delay <= 0 or self.is_playing():
        self._request_frame()
        self._rebuild_overlays()
        return
    self._refresh_timer.start(delay)   # QTimer(singleShot=True) を __init__ で作る
```

* タイマー発火で `_request_frame()` + `_rebuild_overlays()` を呼ぶ。
* 再生中は従来どおり即時 (間引きは `_on_audio_position` 側の `play_fps` が担当)。
* スクラブ中は `scrub_low_quality_frames` の軽い取得と組み合わさる。60ms の遅延で
  ドラッグが重く感じる場合は設定値を 0 にできる (既存キーのまま・新規キー不要)。

### 9-5. A-fix-2 — タイル継ぎ目で直前のフレームを残す

```python
# _set_proxy_playing(True) の中
# 新ソースのロード中は QGraphicsVideoItem に出す絵が無い。先に切り替えると
# 継ぎ目で必ず黒が 1 回見える (ver8 resolve8 §2-5 A-1)。
# ロードが終わるまでベースフレーム (直前の絵) を出したままにする。
```

| 変更 | 内容 |
| --- | --- |
| 切り替えの遅延 | `_set_proxy_playing(True)` では `setVideoOutput()` までを行い、`_show_video_item(True)` は `_on_audio_status` が `BufferedMedia` を受けた時点で呼ぶ |
| シンクの張り直し回避 (A-fix-1 の最小対応) | `self._player.videoOutput() is self._video_item` なら `setVideoOutput()` を呼ばない |
| 戻し | `pause()` / `_on_proxy_gave_up()` の経路は現状のまま (`_show_video_item(False)`) |

タイルを跨いで 1 本に連結する A-fix-1 本体は**本書では採らない** (§15)。

### 9-6. A-2 (タイル有無の段差) について

絵の精細さの段差 (960x540 ↔ 原寸) は仕様として残す。
**字幕が出たり消えたり大きさが変わる段差**は §5/§6 で縮む (約 8% → ほぼ 0)。
完全に消すには案A (字幕も libass で描く) が必要 (§15)。

## 10. 死に設定の整理

| # | 対象 | 対応 |
| --- | --- | --- |
| D1 | `timeline.preview.width` (960) — 定義・検証のみで未参照 | **削除**。`DEFAULT_SETTINGS` (`settings_window.py:518`) / `setting.json` / `builder.py:437` の 3 箇所。既存ユーザーの `setting.json` に残っても `timeline_config()` が読まないため無害 (`_merge_with_defaults` は利用者の値を温存するだけ / `settings_window.py:1281-1290`)。設定画面に UI は無いため移行処理も不要 |
| D2 | `timeline.preview.update_debounce_ms` (60) — 未参照 | **使う** (§9-4)。キーと既定値は現状維持 |
| D3 | 黒塗りの発生が追えない | **ログを足す** (§9-1) |
| D4 | `_on_tile_ready(_tile_index)` が引数を使っていない | **使う** (§9-2) |

## 11. 設定の追加・変更

### 11-1. 追加する 1 キー

| キー | 既定 | 意味 |
| --- | --- | --- |
| `timeline.preview.subtitle_metrics_mode` | `"libass"` | `"libass"` = 字の大きさ・行送り・字間を libass 基準へ合わせる (§5-§7) / `"em"` = 従来の Qt 近似 |

* `setting.json` の `timeline.preview` へ追加。
* `settings_window.DEFAULT_SETTINGS["timeline"]["preview"]` へ同じ既定を追加
  (欠落キーは `_fill_timeline_nested_defaults` が補完する)。
* `builder.timeline_config()` の `preview` セクションで検証:

```python
# 字幕プレビューのメトリクス基準 (ver8 resolve8 §5)。
# "libass" = 出力 (libass) と同じ字の大きさ・行送りへ寄せる (既定)
# "em"     = 従来の Qt 近似 (em 基準・行送り x1.2)
"subtitle_metrics_mode": ("em" if str(
    preview.get("subtitle_metrics_mode", "libass")).strip().lower() == "em"
    else "libass"),
```

* 未知の値は `"libass"` に倒す (既存の `scale` / `playback_skip_frame` と同じ作法)。
* 設定画面への UI 追加は**しない** (開発・退避用のため。既存の `playback_skip_frame` 等と同列)。

### 11-2. 削除する 1 キー

`timeline.preview.width` (§10 D1)。

### 11-3. バージョン

`setting.json` のバージョン方針・移行処理 (`_normalize_legacy_values`) に追加は不要。
新規キーは `_fill_timeline_nested_defaults` で補完され、`_has_new_keys` が保存を促す既存の流れに乗る。

## 12. i18n

**追加なし。** 本書の変更で新しい `tr()` 文言は増えない (status 表示の文言は既存のまま)。
`src/i18n/catalog_en.py` は変更しない。追加ログは日本語のまま (既存のログ方針に合わせる)。

## 13. テスト計画

`python -m unittest discover -s tests` で走る形にする。GUI を要するものは
`os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` を先頭に置く (既存の慣習)。

### 13-1. 新規 `tests/test_subtitle_preview_metrics.py`

| # | 内容 |
| --- | --- |
| T1 | `font_metrics.vertical_metrics()` が同梱フォントで `upem=1000 / win_asc=860 / win_desc=220` を返す |
| T2 | `libass_pixel_size(font, 130)` が `120` (= round(130 x 1000/1080)) を返す |
| T3 | メトリクスが読めないフォント (無効な `QFont`) では `size_px` をそのまま返す (落ちない) |
| T4 | `subtitle_metrics_mode="em"` のとき `SubtitleOverlayItem` の `font().pixelSize()` が従来値 (= Fontsize) になる |
| T5 | `"libass"` のとき `pixelSize` が Fontsize より小さく、`text_rect().width()` も縮む (従来値比 1.08 ± 0.02 の範囲) |
| T6 | 行送り: `"libass"` の `_text_height` が `行数 x Fontsize`、`"em"` では `行数 x Fontsize x 1.2` |
| T7 | `spacing=10` のとき `"libass"` の `text_rect().width()` が `spacing=0` より広く、増分が文字数 x 10 ± 10px に収まる |
| T8 | `anchor_scene_point()` が `\pos` の変換式どおり (位置指定あり: 中心 / コメント: 左中央) であることをモード両方で確認 (保存値の意味が変わらないことの固定) |

**ink 比較 (libass 実描画との画素比較) はテストに入れない。** 同梱 `ffmpeg.exe` の起動と
フォント環境に依存し、CI 的に不安定なため。§3-2 の行送り確定は `request8.md` 付録の
`check_ink.py` 手順で**実装時に 1 回手作業で**行い、結果を本書へ追記する。

### 13-2. 追記 `tests/test_preview_playback.py`

既存の `_StubPanel` (スタブ player / controller) を使う。属性 `_seek_pending` /
`_seek_target_sec` / `_seek_offset_ms` と `mediaStatus` をスタブへ足す。

| # | 内容 |
| --- | --- |
| T9 | `_start_playback(path, chunk_start=0.0)` 直後の `positionChanged(0)` で**再生ヘッドが動かない** (不具合B の回帰テスト。修正前は 0.0 へ戻る) |
| T10 | 同条件で `sec` が目標位置へ達した通知から再生ヘッドが動き出す |
| T11 | プロキシタイル経路 (`chunk_start=12.0` / 再生ヘッド 14.5) でも T9/T10 が成り立つ |
| T12 | `_on_audio_status(LoadedMedia)` で `player.position_ms` が `offset_ms` へ再設定される |
| T13 | `offset_ms == 0` のときは `_seek_pending` が立たず、1 通知目から再生ヘッドが進む (既存の `test_replay_after_rewind_moves_playhead` と矛盾しない) |
| T14 | `pause()` で `_seek_pending` が False に戻る |
| T15 | `_on_tile_ready(index)`: 再生ヘッドを含まないタイル番号では `_refresh_all` が呼ばれない / 含む番号では呼ばれる |
| T16 | `_overlay_signature`: 同じ内容なら再構築しない / 字幕の文字を変えたら再構築する / 停止中に動画オーバーレイの時刻が進めば再構築する / 再生中は時刻成分で再構築しない |
| T17 | `update_debounce_ms=0` で即時、`>0` でタイマー経由になる (`QTimer` の発火はスタブで検証) |

### 13-3. 既存テストへの影響

| テスト | 影響 |
| --- | --- |
| `tests/test_compositor.py` / `test_proxy_render.py` | 出力側は無変更 → **影響なし** (壊れたら設計違反) |
| `tests/test_preview_playback.py` の既存 29 件 | `_start_playback` に属性追加のみ。`test_start_playback_resets_last_frame_at` は `player.position_ms == 15000` を見ているため**そのまま通る** |
| `tests/test_settings_migration.py` | `timeline.preview` のキー増減を見る箇所があれば更新が必要 (実装時に確認) |

## 14. 受け入れ基準

| # | 基準 | 確認方法 |
| --- | --- | --- |
| AC1 | 字幕の**大きさ**がプレビューと出力で一致する (ink 幅・高さの差が ±2% 以内) | 同一クリップで「高精度プレビュー」PNG と画面を比較、または `check_ink.py` |
| AC2 | 位置指定なし字幕の**縦位置**が行数によらず一致する (1〜3 行で ±3px 以内) | 同上 |
| AC3 | 画像オーバーレイの配置・大きさが一致している (現状維持) | 目視 + `test_compositor.py` |
| AC4 | 再生ヘッドを置いて Space → **再生ヘッドが戻らない**。鳴り始めの位置も一致 | 手動 (タイル在り / 認識用音声在りの両方) |
| AC5 | クリップ境界で黒フレームが出ない (ギャップは黒のまま) | 手動 + 追加ログに WARNING が出ないこと |
| AC6 | 停止中、先読みタイルの完成でプレビューがチカチカしない | 手動 (`lookahead_tiles=3` のまま) |
| AC7 | `subtitle_metrics_mode="em"` で従来の見え方に戻る | T4/T6 |
| AC8 | `python -m unittest discover -s tests` が全件 OK (現行 1361 件 + 新規) | コマンド |

## 15. やらないこと (保留・不採用)

| # | 項目 | 判断 |
| --- | --- | --- |
| N1 | **案A** 字幕レイヤを libass で描いて Qt に重ねる | **保留**。案D で S1/S2/S3 が消えるため。残るのは字形選択・カーニングの差で、編集中の判断には影響しない。採る場合は「当たり判定だけ持つ不可視アイテム」「ドラッグ中は近似 → 離したら差し替え」「rawvideo を stdout で受ける」が前提 (`request8.md` §10 案A)。§5 の `subtitle_metrics_mode` に `"ass_layer"` を足す形で後から入れられるよう、モードを文字列にしてある |
| N2 | **案C** プロセス内 libass (ctypes / PyAV 再ビルド) | **不採用**。DLL 一式の同梱・ライセンス整理の費用に見合わない |
| N3 | `3202df2` の巻き戻し | **不採用** (`request8.md` §17 R3)。一致の仕組みそのものを失う |
| N4 | プロキシの fps を 60 → 30 | **不採用**。「どのフレームに何が写っているか」の一致を捨てる (§16 Q1) |
| N5 | 停止中フレームを 540p 化 | **不採用**。字幕位置の確認は停止中に行うため画質を落とす利点が小さい (§16 Q2) |
| N6 | A-fix-1 本体 (タイルを跨いで 1 本に連結して再生) | **保留**。A-fix-2 + シンク張り直しの回避で継ぎ目の黒が消えるかを先に確認する |
| N7 | `comment_decor` / ASS 側の推定式の見直し | **対象外**。箱は出力と同じ推定のままが一致の条件 |

## 16. 未決事項

`request8.md` §18 / §24 の Q1-Q9 への本書の立場と、残る確認事項。

| # | 内容 | 本書の立場 |
| --- | --- | --- |
| Q1 | プロキシ fps を 30 へ落とすか | **落とさない** (N4)。要望は大きさ・配置の一致であり、時間軸の一致を捨てる理由がない |
| Q2 | 停止中も 540p にするか | **しない** (N5) |
| Q3 | 案D と案A のどちらを採るか | **案D を実装、案A は保留** (§0.2 / N1) |
| Q4 | `timeline.preview.width` を消すか統合するか | **消す** (§10 D1)。`playback_width` が実効値であり、同義のキーを 2 つ残す理由がない |
| Q5 | 不具合A は再生中か停止中か | **両方に手当てする** (再生中: A-fix-2 / 停止中: A-fix-4・A-fix-5・A-fix-6)。切り分けを待たずに入れられる設計にした |
| Q6 | 暗転の周期は 4 秒 (タイル) かクリップ長か | **未確定**。§9-1 のログで事後に判別できるようになる。4 秒周期なら A-fix-2、クリップ長なら A-fix-7 が効く |
| Q7 | 継ぎ目で音も途切れるか | **未確定・要確認**。途切れている場合は A-fix-1 本体 (タイル連結) が必要になる (N6 の判断材料) |
| Q8 | (B) の戻り距離は先頭か数秒か | **どちらでも直る** (§8 は `chunk_start` に依らない) |
| Q9 | D1 / D2 の死に設定 | D1 削除・D2 実装 (§10) |
| **Q10** | §3-2 の行送りの残差 (hhea 基準 120.37px vs ink 実測 約 127px) をどちらに合わせるか | **解決** (§21-1)。`usWinAscent + usWinDescent` 基準 (= Fontsize) で ink の高さ・中心が完全一致した |
| **Q11** | `Spacing != 0` のとき行末に 1 字間ぶん付くか (libass / Qt の差) | **解決** (§21-3)。libass・Qt とも各文字の後ろへ入れる挙動で、幅も中心も一致した |
| **Q12** | `fsSelection` bit 7 (USE_TYPO_METRICS) が立つフォントで libass が sTypo 基準へ切り替わるか | **未検証**。同梱フォントでは立っていない。立っているフォントを持ち込まれた場合に備え、`vertical_metrics()` は `typo_height` も返す形にしておく (切り替えは将来の 1 行) |

## 17. 影響範囲とリスク

| リスク | 内容 | 緩和 |
| --- | --- | --- |
| R1 | フォントによって案D の換算が過剰/不足になる | `subtitle_metrics_mode="em"` で即座に戻せる (§5-4)。換算はフォントのテーブル実測値のみを使い、固定係数を持たない |
| R2 | `text_rect()` が縮むことで既存プロジェクトの字幕位置がずれて見える | 保存値は正規化アンカーで意味が不変 (§5-3)。**移行処理は不要**。出力結果は 1 bit も変わらない (ASS を変えないため) |
| R3 | A-fix-5 の署名漏れで「編集したのに画面が変わらない」 | 署名には見た目を決める値をすべて入れ、T16 で固定する。漏れが怖い項目 (将来の属性追加) は `to_item()` 由来の辞書から作ることで取りこぼしを減らす |
| R4 | A-fix-6 のデバウンスでスクラブが重く感じる | `update_debounce_ms=0` で従来動作。既定 60ms は元の設計意図どおりの値 |
| R5 | §8 の seek ガードで再生開始が一瞬止まって見える | 戻るより望ましい (§8-5)。`LoadedMedia` での seek やり直しで実際の待ちは数百 ms 以内 |
| R6 | `QRawFont.fontTable` が将来の Qt / 環境で空を返す | `None` で従来動作へ落ちる (§5-1)。プレビューは落ちない |

## 18. 実装時の作業手順

1. 本書をレビューし、§16 の Q6 / Q7 を手元の再現操作で確定させる (ログは §9-1 を先に入れてよい)
2. §8 (不具合B) → T9-T14 → 手動確認 AC4
3. §9-1 / §9-2 → T15 → 手動確認 AC5 / AC6
4. §5 / §6 / §7 → T1-T8 → `check_ink.py` で Q10 / Q11 を確定し、本書へ実測値を追記 → AC1 / AC2 / AC7
5. §9-3 / §9-4 → T16 / T17
6. §9-5 → 手動確認 (継ぎ目の黒と音の途切れ。Q7 の結論を本書へ追記)
7. §10 (死に設定) → `tests/test_settings_migration.py` の確認
8. `python -m unittest discover -s tests` 全件 → AC8

---

# 第3部 — 実装記録 (2026-10-22)

## 19. 実装状況

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | `src/gui/timeline/font_metrics.py` (新規。縦メトリクス取得と換算 / §5-1) | 完了 |
| 2 | S1 字幕のフォントスケールを libass 基準へ (`preview_items.py`) | 完了 |
| 3 | S2 行送りを `usWinAscent + usWinDescent` 基準へ (同上) | 完了 |
| 4 | S3 `Spacing` を `setLetterSpacing` で反映 (同上) | 完了 |
| 5 | 設定 `timeline.preview.subtitle_metrics_mode` (既定 `"libass"`) | 完了 |
| 6 | B 再生開始のシークガード + `LoadedMedia` での seek やり直し (`preview_panel.py`) | 完了 |
| 7 | A-fix-7 取得失敗で黒く塗らない + 黒塗りのログ (D3) | 完了 |
| 8 | A-fix-4 タイル完成は見ている区間だけ差し替える (D4) | 完了 |
| 9 | A-fix-5 オーバーレイは署名が変わったときだけ作り直す | 完了 |
| 10 | A-fix-6 `update_debounce_ms` を実際に使う (D2) | 完了 (§20-1 の変更あり) |
| 11 | A-fix-2 継ぎ目で直前フレームを残す + 映像シンクの張り直し回避 | 完了 |
| 12 | D1 `timeline.preview.width` の削除 (3 箇所) | 完了 |
| 13 | `tests/test_subtitle_preview_metrics.py` (新規 13 件) | 完了 |
| 14 | `tests/test_preview_playback.py` へ追記 (20 件) | 完了 |

`python -m unittest discover -s tests` → **1430 件 OK** (従来 1397 + 新規 33)。
i18n の追加は無し (§12 のとおり新しい `tr()` は増えていない)。

### 19-1. 変更したファイル

| ファイル | 変更 |
| --- | --- |
| `src/gui/timeline/font_metrics.py` | 新規 (約 140 行) |
| `src/gui/timeline/preview_items.py` | `SubtitleOverlayItem` に `metrics_mode` を追加。描画サイズ・行送り・字間 |
| `src/gui/timeline/preview_panel.py` | シークガード / 黒塗り / タイル差し替え / 署名 / デバウンス / 継ぎ目 |
| `src/timeline/builder.py` | `subtitle_metrics_mode` の検証を追加、`width` を削除、`update_debounce_ms` を非負に正規化 |
| `src/settings/settings_window.py` `src/settings/setting.json` | 同キーの追加・削除 |
| `tests/test_preview_playback.py` | スタブへ属性追加 (`_seek_pending` 他)、`_StubPlayer` を非同期ロード相当へ |

**出力側 (`compositor` / `renderer` / `proxy_render` / `subtitle_generator` / `comment_decor`) は 1 行も変更していない** (§0.2)。
`tests/test_compositor.py` / `tests/test_proxy_render.py` は無修正で通っている。

## 20. 設計からの変更点

### 20-1. デバウンスは「待つだけ」ではなくスロットルにした (§9-4)

設計のままトレーリングのみのデバウンスにすると、**スクラブ中 (再生ヘッドを掴んで
動かしている間) はずっと絵が更新されない**。`resolve7` のスクラブは「動かした量だけ
音と絵が進む」操作であり、止まるまで絵が出ないのは退行である。

そこで「前回の取り直しから `update_debounce_ms` 以上たっていれば即時、
そうでなければ残り時間だけ待って 1 回にまとめる」= 取り直しの頻度に上限を付ける
形にした (`_schedule_refresh` / `_refresh_now`)。

* 最初の 1 回は待たない (掴んだ瞬間に絵が出る)
* 連続した移動は 1 秒あたり最大 `1000 / update_debounce_ms` 回へ頭打ち (既定 60ms → 約 16 回/秒)
* `0` で従来どおり毎回即時、再生中も即時 (間引きは `play_fps` 側)

### 20-2. 行送りの基準 (§6 / Q10)

設計時の候補 (hhea 基準) ではなく `usWinAscent + usWinDescent` 基準が正解だった。
§19-2 の実測で確定させ、§6 を更新済み。

### 20-3. フォールバックの扱い (§5-1 / §6)

メトリクスが読めないときは**字の大きさと行送りの両方**を従来値へ戻す。
行送りだけ Fontsize へ詰めると、em 基準の大きい字が行間で重なるためである。

## 21. ink 実測の結果 (AC1 / AC2 / Q10 / Q11)

同梱 `ffmpeg.exe` の `ass` フィルタで字幕レイヤだけを透過 PNG へ描き (libass)、
同じ字幕を `SubtitleOverlayItem.paint()` でオフスクリーン描画して (Qt)、
アルファ > 16 の画素の外接矩形 (ink) を比べた。1920x1080 / Fontsize 130 / Outline 10。

### 21-1. 同梱フォント (07にくまるフォント / `usWinAscent+usWinDescent = 1080`, `upem = 1000`)

| ケース | libass ink | Qt `"libass"` | Qt `"em"` (従来) | 中心ずれ `"libass"` | 中心ずれ `"em"` |
| --- | --- | --- | --- | --- | --- |
| 1行 `\pos(960,540)` | 615x127 | **613x127** | 662x135 | +0.0 / +0.0 | +0.5 / -1.0 |
| 2行 `\pos` | 619x254 | **617x254** | 667x289 | +0.0 / +0.0 | +0.0 / -0.5 |
| 3行 `\pos` | 373x385 | **373x385** | 402x445 | +0.0 / +0.0 | -0.5 / -1.0 |
| 1行 位置なし (align2) | 615x127 | **613x127** | 662x135 | +0.0 / +0.0 | +0.5 / **-14.0** |
| 2行 位置なし | 619x254 | **617x254** | 667x289 | +0.0 / +0.0 | +0.0 / **-26.5** |
| 3行 位置なし | 373x385 | **373x385** | 402x445 | +0.0 / +0.0 | -0.5 / **-40.0** |

* 高さは**全ケースで完全一致**。幅の差は 0〜2px (0.3%)
* 位置指定なしの縦ずれは **-14〜-40px → ±0.0px**
* → **AC1 (±2% 以内) / AC2 (±3px 以内) を満たす**

### 21-2. システムフォント (既定は Yu Gothic UI)

| フォント | ケース | libass ink | Qt `"libass"` | Qt `"em"` | 中心ずれ `"libass"` | 中心ずれ `"em"` |
| --- | --- | --- | --- | --- | --- | --- |
| Yu Gothic UI (`win 2724 / upem 2048`) | 1行 `\pos` | 403x107 | **394x106** | 515x134 | -0.5 / +0.5 | +0.0 / +1.5 |
| Yu Gothic UI | 1行 位置なし | 403x107 | **394x106** | 515x134 | -0.5 / +0.5 | +0.0 / **-11.5** |
| Yu Gothic UI | 3行 位置なし | 237x366 | **231x365** | 298x445 | +0.0 / -0.5 | -0.5 / **-38.5** |
| Meiryo (`win 3072 / upem 2048`) | 1行 `\pos` | 451x98 | **449x95** | 660x131 | +0.0 / -0.5 | +0.5 / -3.5 |
| Meiryo | 3行 位置なし | 277x357 | **275x355** | 401x443 | +2.0 / +0.0 | +2.0 / **-42.0** |

* Yu Gothic UI は換算比が **1.33 倍**と大きく、従来は幅が 27.8% も大きかった (515 対 403)。
  換算後は 2.2% 差 (394 対 403) まで縮む。**AC1 の ±2% をわずかに超える**が、
  残差はグリフのシェーピング差 (Qt 対 libass) であり、案D の射程外である (§15 N1)。
* Meiryo は従来 46% 差 → 0.4% 差。縦位置はいずれも ±0.5px。

### 21-3. `Spacing` (Q11 / AC の追加確認)

| 文字数 / Spacing | libass ink 幅 | Qt ink 幅 | 差 | 中心ずれ |
| --- | --- | --- | --- | --- |
| 5字 / 0 | 615 | 613 | -2 | +0.0 |
| 5字 / 10 | 655 | 653 | -2 | +0.0 |
| 5字 / 25 | 714 | 713 | -1 | +0.5 |

幅の増え方 (`(文字数-1) x Spacing`) も中心位置も一致した。
**libass も Qt も字間を各文字の後ろへ入れる (行末のぶんは ink に出ない) 挙動が同じ**であり、
§7 の懸念 (行末に 1 つ余る) は実測上現れない。→ **Q11 解決**。

## 22. 環境についての注意 (テスト・検証時)

`QRawFont.fontTable()` は**プラットフォームプラグインによって結果が変わる**。

| プラットフォーム | システムフォント | アプリ追加フォント (`addApplicationFont`) |
| --- | --- | --- |
| `windows` (実機) | 読める | 読める |
| `offscreen` (テスト) | **空が返る** → 従来動作へフォールバック | 読める |

* したがって `tests/test_subtitle_preview_metrics.py` は**同梱フォントを登録して**検証する
  (システムフォント前提のテストは offscreen では意味を持たない)。
* 無効な `QRawFont` に対し `familyName()` を呼ぶと offscreen で異常終了することを確認した。
  `font_metrics._read_metrics()` は `isValid()` を見てから各テーブルを読み、長さと値を
  検査してから使う。この順序を崩さないこと。
* §21 の実測スクリプト (`check_ink.py` / `check_spacing.py` / `check_system_font.py`) は
  セッション用の一時ディレクトリに置いたもので、リポジトリには含めていない。
  再検証は「同梱 ffmpeg の `ass` フィルタで透過 PNG を描き、`SubtitleOverlayItem.paint()` を
  オフスクリーンへ描いて ink を比べる」手順で再現できる (`QT_QPA_PLATFORM` は**指定しない**)。

## 23. 残っている確認事項

| # | 内容 | 状態 |
| --- | --- | --- |
| AC4 | 再生ヘッドを置いて Space → 戻らない / 鳴り始めの位置も一致 | 自動テスト (T9-T14) は通過。**実機での手動確認が残る** |
| AC5 | クリップ境界で黒フレームが出ない | 実装済み。**暗転は再発中** (§24) |
| AC6 | 停止中、先読みタイルの完成でチカチカしない | 実装済み。**手動確認が残る** |
| Q6 | 暗転の周期 (4 秒 = タイル境界かクリップ長か) | **確定: タイル境界 (A-1)**。`proxy.mode="off"` で暗転が消えた (§24-7) |
| Q7 | 継ぎ目で音も途切れるか | 未確定。途切れる場合はタイル連結 (N6) の判断が必要 |
| Q12 | `fsSelection` bit 7 (USE_TYPO_METRICS) が立つフォントでの libass の基準 | 未検証。`vertical_metrics()` が `typo_height` を返すため、必要になれば 1 行で切り替えられる |
| — | Yu Gothic UI の幅 2.2% 差 (§21-2) | 案D の限界。気になる場合は案A (N1) へ進む判断材料とする |


---

## 24. 追補: 暗転が残る件の切り分け（2026-10-22 の実機ログ）

§9 の A-fix-2/4/5/6/7 を実装したあとも「暗転・点滅」が残るとの報告。
当日のログ (`src/gui/logs/autoedit_20261022.log`) から分かったこと。

### 24-1. 取得失敗由来ではない（A-4 は消えている）

A-fix-7 で入れた `フレームを取得できませんでした (直前のフレームを表示します)` は
**0 件**。フレーム取得失敗で黒く塗る経路は踏んでいない。

### 24-2. プロキシ自体が落ちて「諦め」に入っていた

```
[WARNING] src.timeline.proxy_render: プロキシのタイル生成に失敗しました (code=4294967274):
  [aost#0:1/aac] [enc:aac] Could not open encoder before EOF
  [aost#0:1/aac] Task finished with error code: -22 (Invalid argument)
  [out#0/mp4] Nothing was written into output file,
              because at least one of its streams received no packets.
[WARNING] src.gui.timeline.proxy_manager: プロキシを諦めました:
  プレビューの生成に繰り返し失敗したため、近似表示に切り替えます
```

17:22 と 19:10 の**両セッションで発生**。同じタイルが `_MAX_TILE_FAILURES`=3 回
失敗すると、以降そのセッションは**全区間が近似表示**になる (§7 の縮退)。
この状態なら暗転・点滅が残るのは筋が通る。緑のレンダ済みバーも伸びない。

失敗しているのは**音声**。`Could not open encoder before EOF` は
aac エンコーダがフレームを 1 枚も受け取らずに EOF を迎えたときの文言で、
映像側 (`libx264`) の `Terminating thread` と muxer の
`Nothing was written` はその**結果**。

### 24-3. 現行プロジェクトでは再現しない

保存済みプロジェクト (1396.2s / 350 タイル / 1087 クリップ) の
**全タイルの ffmpeg コマンドを計画だけ総なめ**し、
1 フレーム (1/30s) 未満の断片を含むタイルを 3 枚見つけた
（#33 に 0.021s、#85 に 0.031s、#134 に 0.030s）。

**これは原因ではなかった**。当該タイル (#0/#33/#85/#134) を実際に
ffmpeg で生成したところ **4 枚とも成功** (rc=0 / 220KB〜755KB)。

失敗したセッションの素材は一時フォルダ (`%TEMP%rchive_clips_*`) を
指していて既に消えているため、当時の状態は再現できない。

### 24-4. 追えなかった理由と対処

警告が**どのタイルで失敗したのかを出していなかった**。
出力名にはタイル番号が入っている (`tile_00033_<鍵>.mp4`) ため、これを警告へ足した。

```python
_logger.warning("プロキシのタイル生成に失敗しました (code=%s / %s): %s",
                process.returncode, os.path.basename(command[-1]), ...)
```

### 24-5. 次の再現で確かめること

| # | 見るもの | 判定 |
| --- | --- | --- |
| 1 | `プロキシを諦めました` が出るか | 出る → §24-2 の筋。`(code=… / tile_NNNNN_…)` の番号から該当区間を特定できる |
| 2 | 暗転するのは**緑のバーが無い / 薄い区間**か、**実線の区間でも**起きるか | 前者 → プロキシ不足そのもの / 後者 → 別の経路 (Q6 へ戻る) |
| 3 | `logging.level` を `DEBUG` にしたときの `show_blank` 経路 | ギャップ由来の黒塗りかどうかが分かる |

### 24-6. 実機の回答: バーが実線でも暗転する（2026-10-22）

> 暗転したときに緑のバーがあります。

**プロキシ不足ではない**。レンダ済み (= 絵が在る) 区間でも暗転している。
残る経路は §2-5 A-1 の**プロキシ再生の継ぎ目**:
`tile_sec` ごとに `setSource()` で次のタイルへ張り替えるため、
ロード中は `QGraphicsVideoItem` に出す絵が無い。
A-fix-2 で `BufferedMedia` まで待つようにしたが、**それでも残っている**。

#### 一旦プロキシを止める（利用者の要望）

> 一旦このレンダリングをする処理を無くすことはできますか？
> 非常に操作がしづらいので、いったんレンダリングしないようにしたい。

既存の `timeline.preview.proxy.mode` に `"off"` があり、
**設定 1 行で止まる**（コード変更なし / §7 の縮退と同じ経路 =
「プロキシを諦めました」のときに通る道で、今日のログでも実際に動いていた）。

```
src/settings/setting.json
  timeline.preview.proxy.mode: "tile" → "off"
```

| 止めると | |
| --- | --- |
| 無くなるもの | タイル生成の ffmpeg、継ぎ目の張り替え、**暗転・点滅**、緑のレンダ済みバー |
| 残るもの | プレビュー表示・再生・音声（近似表示での 1 コマ取り出しへ戻る） |
| 代償 | 絵が**出力と完全一致ではなくなる**（字幕の大きさ等は §5-§7 の libass 基準でほぼ一致する） |

`DEFAULT_SETTINGS` の既定は `"tile"` のまま。既存利用者の `setting.json` も
`"tile"` のままなので、**この変更はこの開発環境だけに効く**。

#### これは Q6 の決定実験になる

`"off"` で**暗転が消えれば**、原因はプロキシ再生の継ぎ目 (A-1) に確定し、
タイルを跨いで 1 本に連結する案 (N6 / §15 で本書では採らなかった本体) へ進む判断材料になる。
**消えなければ**、暗転は近似表示側 (`show_blank` / ギャップ) にもあることになり、
`logging.level = DEBUG` での再現が次の一手。

### 24-7. 結論: 原因はプロキシ再生の継ぎ目 (A-1) で確定（2026-10-22）

> 暗転は消えました

`timeline.preview.proxy.mode` を `"off"` にしただけで暗転が消えた。
**プロキシ再生の継ぎ目 (§2-5 A-1) が原因で確定**し、Q6（4 秒周期か
クリップ長か）も「**タイル境界 = 4 秒**」で決着した。

近似表示側 (`show_blank` / ギャップ) に暗転は無い。A-fix-7 の WARNING が
0 件だったこと (§24-1) と整合する。

#### A-fix-2 では足りなかった理由

A-fix-2 は「**ベースフレーム → 映像**へ切り替える初回」を `BufferedMedia` まで待たせる。
一方、**再生中のタイル継ぎ目**では `QGraphicsVideoItem` は既に表示されたままで、
`setSource()` が**その場で映像面を空にする**。待たせる対象が違っていた。

#### 残っている選択肢（未実装 / 要判断）

| 案 | 内容 | 規模 | 代償 |
| --- | --- | --- | --- |
| **a. 二重プレイヤー** | `QMediaPlayer` を 2 つ持ち、裏で次タイルを読み込んでおいて継ぎ目で表示を入れ替える | 中（`preview_panel` の再生経路） | メモリとデコーダ 2 本ぶん |
| **b. 停止中だけタイルを使う** | 再生は近似表示（= 今の `"off"` と同じ挙動）、停止・コマ送り・スクラブだけタイルの絵を使う | 小 | 再生中の絵が出力と完全一致ではない |
| **c. タイル連結 (N6)** | 再生用に複数タイルを 1 本へ繋ぐ | 大（§15 で本書では見送った本体） | 生成待ちが伸びる。継ぎ目は減るが消えない |

**推奨は b**。暗転の原因（継ぎ目そのもの）を無くしつつ、
プロキシの本来の値打ち（停止中に出力と同じ絵を確認できること）は残る。
a は絵の一致も保てるが、継ぎ目の入れ替えという**暗転と同じ機構を抱えたまま**精度を上げる形になる。
