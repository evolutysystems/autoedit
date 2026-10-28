# ver9 resolve11 — 「クリックしてから 1 秒」の正体 (字幕プレビューのパス合併)

報告: 2026-10-28 のチャット
> ver9/resolve9.md に関してですが、やはりどうしても重いです。
> クリックしてから動作するのに 1 秒かかります。他の対策はないですか？

対象: `src/gui/timeline/preview_items.py` (原因) /
`src/timeline/builder.py` + `src/settings/settings_window.py` + `src/settings/setting.json` (設定) /
`src/gui/timeline/preview_panel.py` (受け渡し)
調査日: 2026-10-28 (main `115e257` / v1.6.1 + ver9 resolve2〜resolve10 実装後)
計測材料: 実プロジェクト
`C:\Users\evolu\AppData\Local\Stretheus\archive_work\vod_2912229276.archive.timeline.json`
(尺 1704s / 要素 363 件 / テロップ 362 件 / 字幕トラック 4 本 / 素材 9 件すべて実体あり)
計測環境: **実画面** (3440x1440 / DPR 1.0 / ウィンドウ最大化 3440x1369)。
resolve9 と違い `offscreen` ではなく実機のウィンドウで測った
種別: **調査報告 + 修正記録**。§2 の数値はすべて実測。**実装済み** (§4)

---

## 0. 先に結論

### 0.1 3 行で

1. **原因は `QPainterPath.united()`。** 字幕プレビューの縁 (アウトライン) を
   作るのに**パスのブール演算**を使っており、しかも **paint のたびに**計算して
   いた。利用者の設定 (縁 10px) では 26 文字のテロップ 1 本で **733ms**、
   46 文字で **1752ms** かかる (§2.4)。
2. **resolve9 はここを測っていなかった。** resolve9 は「`SubtitleOverlayItem` の
   **生成**」を 0.04ms/件と測って「オーバーレイは無罪」と結論したが、
   重いのは生成ではなく **paint** だった。再生ヘッドを動かすと
   字幕 paint 1 回で中央 **138〜291ms** / 最大 **916ms** (§2.3)。
3. **パス演算をやめ、幅 2W のペンでストロークする描き方へ変えた。**
   クリック応答は **90%点 539ms → 128ms / 最大 1088ms → 160〜219ms** (§4.3)。
   設定 `timeline.preview.subtitle_outline_mode = "union"` で従来へ戻せる。

### 0.2 なぜ resolve9 では見つからなかったのか

| resolve9 が測ったもの | 実際の症状 |
| --- | --- |
| **Timeline の再描画** 3.5〜7.7ms | Timeline の描画は今回も 8〜13ms。**無罪のまま** |
| **`SubtitleOverlayItem` の生成** 0.04ms/件 | 生成は確かに速い。**paint を測っていなかった** |
| `offscreen` / ウィンドウ 1600x700 | 実機・最大化 (3440x1369) では塗る面積が 4.3 倍 |
| 「中央値」での評価 | 体感は**中央値ではなく裾**。中央 44ms に対し 90%点 539ms |

resolve9 の「トラック複数化は原因ではない」という結論自体は正しい
(字幕トラックを増やしても Timeline の描画は 1 行 0.2ms しか増えない)。
ただし**プレビュー側では同時に出るテロップが増えるぶん paint が増える**ため、
「トラックを増やしてから重くなった気がする」という体感には根拠があった。
1 本 733ms の paint が 2 本同時に出れば、それだけで 1.5 秒になる。

### 0.3 実測 (クリック → 画面が更新されるまで / 最大化・実画面)

再生ヘッドを毎回ちがう位置へ動かす操作を 25 回。

| | 中央 | 90%点 | 最大 | 字幕 paint 中央 | 字幕 paint 最大 |
| --- | --- | --- | --- | --- | --- |
| **修正前** | 44 ms | **539 ms** | **1088 ms** | 138〜291 ms | **916 ms** |
| **修正後** | 38 ms | **128 ms** | **219 ms** | 71 ms | 99 ms |

---

## 1. どう切り分けたか

resolve9 は「描画が重い」で止まっていたため、**クリック 1 回を関数単位で分解**した。

1. まず操作別に応答を測った (選択・再生ヘッド・ズーム・色変更・Undo)。
   → **再生ヘッド移動と字幕クリップの選択だけが遅い**。
2. `offscreen` では 12〜37ms しか出なかったため、**実画面・最大化**で測り直した。
   → 中央 28〜62ms に対し**最大 630ms** のスパイクが出た。
   **体感はこのスパイク**であり、中央値を見ていては見つからない。
3. クリック 1 回の GUI スレッドの時間を関数単位で計測した。

```
クリック 25 回: 中央 42 / 90%点 521 / 最大 671 ms / 合計 4142 ms

GUI スレッドの内訳 (合計の大きい順):
   字幕アイテム paint      合計 3106 ms /  11 回 / 1 回 282.4 / 最大 623
   Timeline 描画           合計  458 ms /  52 回 / 1 回   8.8 / 最大  17
   プレビュー: 再生ヘッド      合計   14 ms /  25 回 / 1 回   0.6
   プレビュー: オーバーレイ再構築  合計   14 ms /  16 回 / 1 回   0.9
   プレビュー: フレーム受取     合計    5 ms /  11 回 / 1 回   0.5
```

**合計 4142ms のうち 3106ms (75%) が字幕 1 種類の paint。** ここで打ち止めになった。

### 1.1 途中で潰した仮説 (いずれも無罪)

| 仮説 | 実測 | 判定 |
| --- | --- | --- |
| フレームのデコードが重い (resolve9 §5 の本命) | ワーカースレッドで動いており GUI を止めない。単体では seek+追いデコード+RGB で **46ms** | 無罪 (§1.2) |
| 波形の取得 (ffmpeg 2 本) がデコードを圧迫する | 波形 OFF でもスパイクは消えない (90%点 524ms) | 無罪 |
| 動画オーバーレイの同期デコード (`_overlay_pixmap`) | この案件のオーバーレイは画像 (png) のため呼ばれない。ただし**動画オーバーレイでは GUI スレッドで同期デコードする**設計のまま (§6 の残課題) | 今回は無罪 |
| Undo のスナップショット (`copy.deepcopy(timeline.source)`) | `source` は JSON 20KB 相当。1ms 未満 | 無罪 |
| 保存・読み込み / トラック構成 | 往復しても構成は保たれる。Timeline 描画も 8〜13ms | 無罪 |
| 半透明ウィンドウ (DWM Acrylic) の合成 | `ui.background.translucent_window = false`。ダイアログ全体の repaint も 17ms | 無罪 |

### 1.2 デコードは「もともと速い」ことの確認

素材は Twitch VOD のストリームコピー (`-c:v copy`) で、**キーフレームは 2 秒間隔**
(1920x1080 / 30fps / 3.2Mbps / H.264 High)。1 回の seek で追いデコードするのは
最大 60 枚だが、実測は 32 枚・34ms だった。

| 試した条件 | seek | 追いデコード | RGB 変換 | 計 |
| --- | --- | --- | --- | --- |
| 現状 (thread_type=AUTO / 原寸 RGB) | 5.9 | 33.8 (31.9 枚) | 6.4 | **46.1 ms** |
| thread_type=FRAME / count=8 | 3.3 | 39.7 | 5.4 | 48.4 ms |
| 追いデコードを NONREF で飛ばす | 6.0 | 34.7 | 6.4 | 47.0 ms |
| RGB を 1280 幅へ縮小 | 0.7 | 39.4 | 9.5 | 49.6 ms |

**どれも効かない = デコードは既に妥当**。resolve9 §7 の「対策1〜4」
(cache_frames / 先読み / プロキシ) は**体感の主因ではなかった**。
`cache_frames` は既に 8 → 64 へ上げてあり、それも効いている
(20 回の移動でデコードが走るのは 3〜7 回だけ)。

---

## 2. 原因

### 2.1 該当コード (修正前)

`src/gui/timeline/preview_items.py` `SubtitleOverlayItem.paint()`

```python
def paint(self, painter, option, widget=None):
    ...
    path = self._text_path()          # ← 文字のパスは作り置きしてある (速い)
    ...
    stroke = None
    if self._outline_width > 0:
        stroker = QPainterPathStroker()
        stroker.setWidth(float(self._outline_width) * 2.0)
        stroker.setJoinStyle(Qt.RoundJoin)
        stroker.setCapStyle(Qt.RoundCap)
        stroke = stroker.createStroke(path).united(path)   # ★ここ
    ...
```

問題は 2 つ重なっている。

1. **`united()` はパスのブール演算**で、文字のパス (26 文字で約 2500 要素 /
   46 文字で約 4400 要素) に対しては桁違いに遅い。
2. **paint のたびに計算している。** 文字・フォント・縁の太さは生成後に変わらない
   のに、毎回作り直していた。`_text_path()` には作り置き (`_text_path_cache`) が
   あるのに、その先の合併には無かった。

### 2.2 なぜ縁を合併していたのか (元の意図)

ASS の `Outline=W` は「字の**外側**に W px」である。Qt の pen は中心線ストロークの
ため、同じ太さを出すには幅 2W にして内側へ出たぶんを字の塗りで覆う
(ver6 resolve3 §4.7-1)。合併していたのは**影のシルエット**を 1 枚の形にするため
で、影が半透明 (設定 `back_color = &H64000000`) のとき、縁と字を別々に塗ると
重なった部分だけ濃くなるのを避ける意図だった。

### 2.3 実測 (アプリ内)

```
従来 (union)   クリック 中央 44 / 90%点 539 / 最大  690 ms  |  字幕 paint 中央 138.2 / 最大 643.7 ms
```

別の試行では字幕 paint 中央 291ms / 最大 **916ms**。テロップの文字数で変わる。

### 2.4 実測 (`united()` 単体 / 縁 10px = 利用者の設定)

| テロップ | パス要素数 | createStroke だけ | **createStroke + united (従来)** | ペンでストローク |
| --- | --- | --- | --- | --- |
| 短い (12 文字) | 567 | 0.2 ms | **54.2 ms** | 0.2 ms |
| 普通 (26 文字) | 2571 | 0.8 ms | **733.3 ms** | 1.2 ms |
| 長い (46 文字) | 4435 | 1.5 ms | **1752.1 ms** | 2.1 ms |

**`createStroke()` は速く、`united()` だけが遅い。** 縁を 4px にしても
179ms / 431ms で、オーダーは変わらない。

> **罠**: 高速化のつもりで `path.simplified()` を挟むと **2489ms / 11028ms** と
> さらに桁が上がる。パスの正規化は絶対に paint 経路へ入れないこと。

---

## 3. 対策 (実装したもの)

### 3.1 方針

| # | 方針 | 理由 |
| --- | --- | --- |
| **D-1** | paint からパスのブール演算を無くす | 文字数に対して非線形に効くため、長いテロップで必ず破綻する |
| **D-2** | 縁は**幅 2W のペンでストローク**して描く | `QPainter.strokePath()` はラスタライザが直接塗るためパス演算が要らない。太さの規約 (外側に W px) は変わらない |
| **D-3** | 形を作る場合も**作り置き**する | 文字・font・縁の太さは生成後に変わらない |
| **D-4** | 従来の描き方は設定で残す | 見比べたいとき・差が問題になったときの逃げ道 (本プロジェクトの流儀) |

### 3.2 実装

```python
# 幅 2W の丸い継ぎ目のペン (縁・影のストローク用 / ver9 resolve11 §3)
def _stroke_pen(self, color):
    pen = QPen(color, float(self._outline_width) * 2.0)
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    return pen
```

```python
        else:
            # 既定の経路: パス演算をせず、幅 2W のペンでストロークする (§3)。
            # 影も同じ形 (ストローク + 字) をずらして置く。重なる内側は
            # このあと不透明な縁と字で覆われるため、二重に乗っても見えない。
            if self._shadow_px > 0:
                painter.translate(self._shadow_px, self._shadow_px)
                if self._outline_width > 0:
                    painter.strokePath(path, self._stroke_pen(self._shadow_color))
                painter.fillPath(path, QBrush(self._shadow_color))
                painter.translate(-self._shadow_px, -self._shadow_px)
            if self._outline_width > 0:
                painter.strokePath(path, self._stroke_pen(self._outline_color))
        painter.fillPath(path, self.brush())
```

`"union"` を選んだときは従来どおり合併するが、その形も
`_outline_paths()` で**作り置き**するようにした (paint のたびには計算しない)。

設定 (`timeline.preview`):

| キー | 既定 | 意味 |
| --- | --- | --- |
| `subtitle_outline_mode` | `"pen"` | `"pen"` = 幅 2W のペンでストローク (パス演算なし)。`"union"` = `QPainterPath.united()` で合併してから塗る (従来) |

### 3.3 見た目の差 (全画素で照合した結果)

1920x1080 のキャンバスへ同じテロップを描き、`union` と `pen` を全画素で比べた
(同じ描き方を 2 回描けば差 0 になることを対照として確認済み)。

| テロップ | 差のある画素 | 全体比 | 最大差 | 差 32 超 |
| --- | --- | --- | --- | --- |
| 短い (12 文字) | 4,268 | 0.206% | 107 | 127 (0.006%) |
| 普通 (26 文字) | 10,967 | 0.529% | 118 | 458 (0.022%) |
| 長い (46 文字) | 17,759 | 0.856% | 133 | 816 (0.039%) |
| コメント役割 | 3,764 | 0.182% | 17 | 0 |

**差は縁の輪郭に沿ったアンチエイリアス 1 画素ぶん**で、影の有無で変わらないこと
も確認した (影を 0px にしても同じ差が出る = 半透明の重ね塗りが原因ではない)。
`QPainterPathStroker` の曲線分割とラスタライザ内部のストローカで、
丸い継ぎ目の端が 1 画素含まれるかどうかが変わるためである。
**形も太さも位置も同一**で、差 32 超の画素は全体の 0.006〜0.039%。

**完全一致ではない**ことは明記しておく。プレビューはもともと Qt による近似表示で、
出力 (libass) とは別物である (ver6 resolve3 §4.7)。1 秒待たされることと
輪郭 1 画素の違いを秤にかけ、既定を `"pen"` とした。
厳密に見比べたいときは `"union"` へ切り替える。

### 3.4 試して採らなかったもの

| 案 | 実測 | 判定 |
| --- | --- | --- |
| `addPath` + `WindingFill` で合併領域を 1 回で塗る | 2.1ms (union の 800 分の 1) | 速いが `pen` と同じ輪郭差が出るうえ、形が 1 枚に閉じないため影の重なりが読みにくい。`pen` の方が素直 |
| `QGraphicsItem.DeviceCoordinateCache` | 90%点 133 → 145ms | **効かない** (pen 化の後では paint 自体が軽く、キャッシュの管理費が勝つ) |
| `ItemCoordinateCache` | 90%点 133 → 137ms | 同上 |
| `path.simplified()` を挟む | 11028ms | 論外 (§2.4 の罠) |
| デコードのスレッド数・NONREF・縮小 | 46 → 47〜53ms | 効かない (§1.2) |

---

## 4. 実装記録 (2026-10-28)

### 4.1 変更したファイル

| ファイル | 変更内容 |
| --- | --- |
| `src/gui/timeline/preview_items.py` | `OUTLINE_PEN` / `OUTLINE_UNION` 追加 / `SubtitleOverlayItem.__init__` へ `outline_mode` / `_stroke_pen()` 追加 / `paint()` を 2 経路へ / `_outline_paths()` (union 用の作り置き) 追加 |
| `src/timeline/builder.py` | `preview.subtitle_outline_mode` の検証 (未知の値は `"pen"` へ) |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ `subtitle_outline_mode: "pen"` |
| `src/settings/setting.json` | 同キーを追記 |
| `src/gui/timeline/preview_panel.py` | `_make_item()` から `outline_mode` を渡す |
| `tests/test_subtitle_outline_mode.py` | **新規** (9 件) |

### 4.2 テスト

`tests/test_subtitle_outline_mode.py`:

| 内容 | 期待 |
| --- | --- |
| 既定の設定値 | `"pen"` (builder / `DEFAULT_SETTINGS` の両方) |
| `"union"` を指定 | そのまま通る |
| 未知の値 (`"nope"`) | `"pen"` へ寄る |
| `"pen"` で描く | **`QPainterPath.united()` が 1 度も呼ばれない** (2 回描いても 0 回) |
| `"union"` で描く | `united()` は**合計 1 回だけ** (paint のたびには呼ばない) |
| `_outline_paths()` | 2 回目は同じオブジェクトを返す (作り置き) |
| 縁 0px | 両方の経路で例外を出さない / 影は字の形 |
| 同じ描き方を 2 回 | 絵が完全に一致する (§3.3 の対照) |

`python -m unittest discover -s tests` → **1968 件 OK** (新規 9 件を含む)。

### 4.3 効果 (実画面・最大化 3440x1369 / 同一プロジェクト)

```
従来 (union)   クリック 中央 44 / 90%点 539 / 最大 690 ms  |  字幕 paint 中央 138.2 / 最大 643.7 ms (14 回)
修正後 (pen)   クリック 中央 38 / 90%点 128 / 最大 219 ms  |  字幕 paint 中央  71.6 / 最大  98.6 ms (8 回)
修正後 再試行   クリック 中央 39 / 90%点 138 / 最大 160 ms  |  字幕 paint 中央  81.5 / 最大 102.1 ms (9 回)
```

* **90%点 539 → 128ms (4.2 倍)**
* **最大 1088 → 160〜219ms (5〜7 倍)**
* 「クリックしてから 1 秒」は**出なくなった**

---

## 5. まだ残っている重さ (次に効く順)

修正後も字幕 paint は 1 回 **71〜81ms** 残る。これは `united()` ではなく
**実際のラスタライズ** (縁 20px 幅のストローク + 字の塗り + 影を、
3193x1082 へ拡大した座標系でアンチエイリアス付きで塗る) の費用である。

| 順 | 対策 | 見込み | 規模 |
| --- | --- | --- | --- |
| **1** | **縁の太さを見直す** (`subtitle.outline_width = 10`)。ストロークの費用は太さにほぼ比例する。8 へ落とすだけで 2 割ほど軽くなる | 中 | **設定値のみ** |
| **2** | **プレビューの表示倍率を抑える** (最大化で 3193px 幅へ拡大して塗っている)。プレビューの上限幅を設け、それ以上は拡大表示にする | 中 | 小 |
| **3** | 字幕を**キャンバス解像度 (1920) で 1 枚の画像へ焼いてから拡大**する。塗りの面積が 2.7 分の 1 になる | 大 | 中 |
| **4** | **動画オーバーレイの同期デコードを直す** (`preview_panel._overlay_pixmap`)。今は GUI スレッドで `frame_at` を呼んでおり、**フレームワーカーと同じ `FrameSource` を鍵なしで共有**している。V2 以降へ**動画**を載せた案件では、1 クリックごとに 50〜100ms の同期デコードが入り、ワーカーとデコーダ状態を奪い合う可能性がある (今回の案件は画像オーバーレイのため顕在化しなかった) | 中 | 中 |
| — | resolve9 §7 の対策 (先読み・プロキシ・GPU 描画) | — | **急がない**。§1.2 のとおりデコードは 46ms で、体感の主因ではなかった |

対策 4 は**性能だけでなく正しさの問題**でもあるため、動画オーバーレイを使う前に
直しておきたい (別件として起票する価値がある)。

---

## 6. 計測の再現方法

### 6.1 クリック 1 回を関数単位で分解する

```python
import os, time, functools, threading, statistics, json, io
os.environ.pop("QT_QPA_PLATFORM", None)          # ★実画面で測る (offscreen では出ない)
from PySide6.QtWidgets import QApplication
from src.timeline import project_io
from src.gui.timeline.timeline_editor_dialog import TimelineEditorDialog
from src.gui.timeline import preview_items as pi

acc = {}
orig = pi.SubtitleOverlayItem.paint
def timed(self, painter, option, widget=None):
    t = time.perf_counter()
    try:
        return orig(self, painter, option, widget)
    finally:
        acc.setdefault("paint", []).append((time.perf_counter() - t) * 1000)
pi.SubtitleOverlayItem.paint = timed

app = QApplication.instance() or QApplication([])
settings = json.load(io.open("src/settings/setting.json", encoding="utf-8"))
timeline = project_io.load_project(PROJ)[0]
dlg = TimelineEditorDialog(timeline, settings, WORK, project_path=PROJ, mode="resume")
dlg.showMaximized()                               # ★最大化しないと裾が出ない
for _ in range(40):
    app.processEvents()
out = []
for i in range(1, 26):
    t = time.perf_counter()
    dlg.controller.set_playhead((i * 83.7) % timeline.duration_sec())
    app.processEvents(); dlg.repaint(); app.processEvents()
    out.append((time.perf_counter() - t) * 1000)
out.sort()
print("中央 %.0f / 90%%点 %.0f / 最大 %.0f ms" % (out[len(out)//2], out[int(len(out)*0.9)], out[-1]))
print("字幕 paint 中央 %.1f / 最大 %.1f ms" % (statistics.median(acc["paint"]), max(acc["paint"])))
```

**注意点 (resolve9 の限界だったところ)**:

* `offscreen` では測れない。実画面・**最大化**で測る。
* **中央値ではなく 90%点と最大**を見る。体感は裾で決まる。
* `project_io.load_project()` は `(timeline, meta)` のタプルを返す。
* 1 回だけ測ると暖機の影響を受ける。20 回以上回して分布で見る。

### 6.2 `united()` 単体

```python
from PySide6.QtGui import QFont, QFontMetricsF, QPainterPath, QPainterPathStroker
path = QPainterPath()
font = QFont("Yu Gothic UI"); font.setPixelSize(72); font.setBold(True)
path.addText(0, QFontMetricsF(font).ascent(), font, "違う違う違う、別にタダで")
st = QPainterPathStroker(); st.setWidth(20.0)        # 縁 10px → 幅 2W
st.setJoinStyle(Qt.RoundJoin); st.setCapStyle(Qt.RoundCap)
# 遅いのはこの 1 行 (26 文字で 733ms)
st.createStroke(path).united(path)
```

---

## 7. resolve9 への追記 (訂正)

| resolve9 の記述 | 本調査の結果 |
| --- | --- |
| §3「`SubtitleOverlayItem` の生成 0.04ms/件 → 体感には影響しない」 | 生成は正しい。しかし **paint は 1 回 138〜916ms** で、体感の 75% を占めていた |
| §9「本調査で新たな原因は見つからなかった」 | **原因はプレビューの字幕 paint にあった**。描画の総量ではなく裾を見れば分かった |
| §7「着手順: cache_frames → 波形の描き方 → 先読み → プロキシ」 | いずれも体感の主因ではない。`cache_frames` (実施済み) は効いているが、残りは急がない |
| §2「テロップトラックの複数化は原因ではない」 | Timeline の描画については正しい。ただし**プレビューでは同時に出るテロップの数だけ paint が増える**ため、体感が悪化する経路はあった |
