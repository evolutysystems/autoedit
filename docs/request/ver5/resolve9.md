# resolve9（ver5） — 横動画から縦動画プロジェクトを作る 詳細設計書

## 0. 本書の位置づけ

* 対象: `D:/develop/StretheusPlan/plan.md` の **P3「横動画から縦動画プロジェクトを作成」**（基本設計）。
  本書はその詳細設計にあたる。要望は `request9.md`。
* 調査は **2026-09-25 時点の実コード**を読んで行った。plan.md の記述と実コードが食い違う箇所は §2 で訂正している。
* 要望から一意に決められない論点は §10「確認事項」へ挙げた
  （`docs/claude.md`「不明点がある場合は推測実装せず設計書へ記載すること」）。

### 0.1 一言でいうと

**「ソースのどこを切り抜くか」だけをプロジェクトに覚えさせ、切り抜きは書き出しのときに行う。**

縦プロジェクトは横プロジェクトと同じ `Timeline` であり、違いは次の 3 点しかない。

| 項目 | 横 | 縦 |
| --- | --- | --- |
| キャンバス | 1920x1080 | 1080x1920 |
| `orientation` | `"landscape"` | `"portrait"` |
| `source["crop"]` | 無し | **切り抜きの指定 (本書で新設)** |

素材は元動画をそのまま参照する。**切り抜いた動画ファイルは作らない。**
作ると (a) 書き出すまで時間がかかり、(b) 枠を直すたびに作り直しになり、(c) 中間ファイルが増える。
既存の「ぼかし」も同じ考え方で、指定だけを持ってレンダリング時に適用している (ver5 resolve8)。

### 0.2 この設計で**やらないこと**

| やらないこと | 理由 |
| --- | --- |
| クリップごとに違う切り抜き枠を持つ | 要望は「プロジェクト単位で 1 つ」。Twitch のクリップ切り出しと同じ体験にする (§3.2) |
| 枠のアニメーション (時間で動く切り抜き) | 要望に無い。必要なら後から `frames` へキーフレームを足せる形にしてある (§5.2) |
| オーバーレイ素材 (V2 以降) の引き継ぎ | 横基準の正規化座標が縦では破綻する (§3.6 / §10 #2) |
| アーカイブ切り抜き用 Timeline からの生成 | 参照している素材が実行の終わりに消えるため、開けないプロジェクトになる (§3.7 / §10 #4) |
| 縦プロジェクトからさらに縦を作る | 既に縦のものを切り抜く意味が無い。メニューを出さない (§10 #10) |

---

## 0.3 実装状況 (2026-09-25)

**§6 の Phase 1〜8 をすべて実装した。**確認事項 (§10) は「すべて推奨どおり」で決定した。
テストは 1010 件すべて通る (縦動画関連は 62 件)。

| Phase | 内容 | 結果 |
| --- | --- | --- |
| 1 | `crop.py` (書式・検証・ジオメトリ・フィルタ文字列) | 完了 |
| 2 | `renderer._video_filters` の分岐 | 完了 |
| 3 | `vertical_builder.py` | 完了 |
| 4 | `crop_canvas.py` | 完了 |
| 5 | `vertical_project_dialog.py` | 完了 |
| 6 | メニューからの起動 | 完了 |
| 7 | プレビューへの反映 | 完了 |
| 8 | `setting.json` / ドキュメント | 完了 |
| — | **実素材での確認** (§8.2 の受け入れ手順) | **未実施** (実素材が要るため) |

### 実装で本書の案から変えたこと

| # | 項目 | 本書の案 | 実装 | 理由 |
| --- | --- | --- | --- | --- |
| 1 | 全体 (single) の置き場所 | 「幅 1080 に収めて上下中央」 | **キャンバスへ収まるよう拡縮して中央** | 縦長の枠 (例 300x1000) を選ぶと幅基準ではキャンバスからはみ出す。既存の正規化と同じ「収めて中央」に揃えた |
| 2 | `filter_chain` の引数 | ぼかしの強さだけを渡す | **設定を平坦化した `cfg` を渡す** | 背景の種類・ぼかしの強さを 1 つにまとめた方が呼び出し側が短くなる |
| 3 | ぼかしの指定 (`source["blur"]`) | 引き継がない (警告のみ) | **引き継がないうえに `source` からも取り除く** | 警告だけでは指定が残り、縦キャンバスの別の場所がぼける |
| 4 | レンダラへの設定の渡し方 | — (本書に記載なし) | `_extract_clip` / `_video_filters` へ `settings` を追加し、`_render_base` が `context.settings` を渡す | ぼかしは `context` を持つ関数から呼ばれるが、`_video_filters` は持たない。引数 1 つで済ませた |

**横プロジェクトの出力は 1 バイトも変わらない。** 素材とキャンバスが一致する横の経路では
映像フィルタが 1 つも付かないことをテストで固定した (`test_vertical_render.py`)。

---

## 1. 要望と要件 ID

| ID | 要件 | 出どころ |
| --- | --- | --- |
| R1 | 横動画の編集中に、選んだクリップから縦プロジェクトを作れる | request9 ①②⑤ |
| R2 | 切り抜く範囲をダイアログで決める。左にソース、右に縦の仕上がりを出す | request9 ③④ |
| R3 | 「全体」と「分割」の 2 方式 | request9 ③ |
| R4 | 全体: 枠は最大 1080x1080。縦キャンバスへ収め、余りは背景で埋める | plan.md P3 |
| R5 | 分割: 上 1080x640 (27:16) / 下 1080x1280 (27:32) の比率固定。合計で 1920 ちょうど | plan.md P3 |
| R6 | 生成物は **クリップ用 (KIND_CLIP) の縦プロジェクト** (1080x1920) | plan.md P3 |
| R7 | 選んだクリップを `timeline_start` 昇順で 0 秒起点へ詰め直す | plan.md P3 |
| R8 | `media_pool` は参照されるものだけ。`path` / `path_rel` は引き継ぐ | plan.md P3 |
| R9 | リンク音声クリップを同じ対応関係で引き継ぐ | plan.md P3 |
| R10 | 選択区間に重なる字幕を時刻シフトして引き継ぐ | plan.md P3 |
| R11 | 枠はドラッグで移動、ハンドルで拡大縮小できる | request9 ④ |
| R12 | 切り抜きの指定は**後方互換**で保存する (旧アプリで開いても壊れない) | plan.md P3 |
| R13 | 書き出し時、切り抜きは**オーバーレイ・字幕より前**に適用する | plan.md P3 |
| R14 | 生成したプロジェクトは「クリップ用」タブの「続きから」で開ける | request9 ⑥ |
| R15 | **既存の横プロジェクトの出力は 1 バイトも変わらない** | request9 ⑦ |
| R16 | 拡大しすぎる枠を作らせない (分割の下枠は必ず拡大されるため) | plan.md P3「制約に注意」 |

---

## 2. 現状分析

### 2.1 縦キャンバスの足場は揃っている

| 要素 | 実体 | 状態 |
| --- | --- | --- |
| 縦の出力プロファイル | `src/modules/output_profile.py:33 _portrait_profile` | `vertical.output_width/height` (既定 1080x1920) から作る |
| `Timeline.orientation` | `src/timeline/model.py:523` | `"portrait"` を受け取れる |
| 縦向けの字幕上書き | `settings.vertical` | 出力時に効く (既存) |
| 縦では OP/ED を結合しない | `src/modules/concat_processor.py` | 既存 |
| プロジェクト種別 | `src/timeline/project_io.py:75 project_kind` | `source.archive` の有無で clip / archive を判定 |

**足りないのは「横ソースを縦へ切り抜く」概念だけ**である。

### 2.2 ベース映像はクリップ 1 本ずつ作って連結している

`src/timeline/renderer.py` の流れは次のとおり。

```
render()
  └ _render_base()            クリップごとに _extract_clip() → silence_cutter.concat_files() で連結
      └ _extract_clip()       -vf に _video_filters() の結果をカンマで連結して渡す
          └ _video_filters()  needs_normalize なら scale+pad+setsar+fps+format、続けてフェード
```

`_video_filters` (renderer.py:338) の正規化チェーンはこうなっている。

```python
filters.append(f"scale={timeline.width}:{timeline.height}:force_original_aspect_ratio=decrease")
filters.append(f"pad={timeline.width}:{timeline.height}:(ow-iw)/2:(oh-ih)/2")
filters.append("setsar=1")
filters.append(f"fps={fps}")
filters.append("format=yuv420p")
```

**切り抜きはここを差し替えるのが正しい。** 理由は 3 つ。

1. この時点ではまだオーバーレイも字幕も乗っていない = R13「切り抜きはそれらより前」を自動的に満たす。
2. 以降の工程 (`_composite` / 字幕焼き込み / ぼかし) は `timeline.width/height` を見るため、
   ベースが縦で出来上がっていれば**何も直さなくて済む**。
3. ギャップ (`_render_gap`) は元から `timeline.width/height` の黒を作るため、縦でも正しい。

**`-vf` は 1 本のカンマ区切りだが、分岐を含む filtergraph も渡せる** (入力 1 本・出力 1 本なら
`split` と `vstack` を使ってよい)。`_video_filters` の戻り値が 1 要素なら `",".join()` はその文字列
そのものになるため、**戻り値の形を変えずに分岐グラフを渡せる** (§5.5)。

### 2.3 `source` は自由なキーを足せる (ぼかしと同じ)

`project_io._source_to_dict` (project_io.py:193) は `dict(source)` をそのまま書き出し、
`from_dict` は `source=data.get("source", {})` でそのまま戻す。

つまり **`timeline.source["crop"]` は何もしなくても往復する**。
ぼかしが `source["blur"]` を使っているのと同じ仕組みで、R12 (後方互換) を追加コスト無しで満たせる。

> plan.md は `Timeline.crop_layout` という**専用の属性**を提案していたが、
> `to_dict` / `from_dict` / マイグレーションの 3 か所を触ることになる。
> 既に確立している `source` 配下の拡張に合わせる (§10 #5)。

### 2.4 右クリックメニューは画面 (ダイアログ) 側が項目を足す

`src/gui/timeline/timeline_view.py:851 _add_blur_actions` は、
**自分ではメニューを作らず、親ウィンドウの `blur_menu_actions(menu)` を呼ぶ**。

```python
owner = self.window()
adder = getattr(owner, "blur_menu_actions", None)
if adder is None:
    return
adder(menu)
```

縦プロジェクトも同じ形にする (`vertical_menu_actions(menu)`)。
`timeline_view` は「どのクリップを右クリックしたか」だけを知っていればよく、
プロジェクトの保存や設定は画面側が持っているためである。

### 2.5 枠を操作する部品は既にある (ぼかしのキャンバス)

`src/gui/timeline/blur_canvas.py` は `QGraphicsView` の上で

* フレーム画像を敷く (`set_frame`)
* 矩形を描く / 掴んで動かす / 角のハンドルで大きさを変える
* 正規化座標 (0〜1) と画面座標を変換する

を既に行っている。**ただし「ぼかす / ぼかさない」「複数指定」「キーフレーム」など
ぼかし固有の概念と結びついている**ため、そのまま使うと縦プロジェクト側の都合
(比率固定・枠は最大 2 つ・上限サイズ) を無理に押し込むことになる。

本書では **`blur_canvas.py` を参考にした軽量版 `crop_canvas.py` を新設**する (§3.4)。

### 2.6 プレビューはフレームをキャンバスへ収めているだけ

`src/gui/timeline/preview_panel.py:453 _on_frame_ready` は、取り出したフレームを
`self._canvas` (= `timeline.width/height`) へ比率維持で収めている。

縦プロジェクトを開くと**横のフレームが縦キャンバスの中央に小さく出る**ことになり、
書き出し結果と食い違う。切り抜きを反映する手当てが要る (§5.8)。

なお、ぼかしが `_apply_blur_preview` という差し込み口を既に持っているため、
同じ場所に切り抜きの反映を足せる。

### 2.7 plan.md から変わった前提

| plan.md の記述 | 実際 | 対応 |
| --- | --- | --- |
| `Timeline` に `crop_layout` を持たせる | `source` 配下なら往復処理が要らない | `source["crop"]` にする (§2.3 / §10 #5) |
| 呼び出し口は `timeline_view.py` のコンテキストメニュー (822 行付近) | メニューの項目は画面側が足す作り (§2.4) | `timeline_editor_dialog` に `vertical_menu_actions` を実装する |
| レンダラの「285 行付近」を分岐 | 実際の正規化は `_video_filters` (338 行) | そこを差し替える |
| 左プレビューは `preview_panel` / `frame_source` を流用 | フレーム取得は `src/blur/frames.py:65 first_frame` が使いやすい | `frames.first_frame` を使う (§5.6) |

---

## 3. 方式選定

### 3.1 切り抜きをいつ適用するか

| 案 | 内容 | 判定 |
| --- | --- | --- |
| 案 1 | 縦プロジェクトを作る時点で、切り抜き済みの動画ファイルを作る | **不採用**。作成に時間がかかり、枠を直すたびに作り直しになる |
| 案 2 | クリップの `transform` (拡大・位置) で表現する | **不採用**。`transform` はオーバーレイ用で、ベース映像には効かない |
| **案 3** | **指定だけ持ち、書き出し時に `crop` フィルタで適用する** | **採用**。ぼかしと同じ方式 (§0.1) |

### 3.2 枠はプロジェクト単位かクリップ単位か

**プロジェクト単位 (1 つ)** とする。要望とplan.md がそう定めており、
Twitch のクリップ切り出し (1 本の切り抜きに 1 つの構図) と同じ体験になる。

データ構造は将来クリップ単位へ広げられるよう、`frames` を**配列**にしておく (§5.2)。

### 3.3 全体 (single) の背景をどうするか

上下に余りが出るため、埋め方を決める必要がある (plan.md 未決 #4)。

| 案 | 見え方 | 費用 | 判定 |
| --- | --- | --- | --- |
| (a) ソースをぼかして拡大した背景 | Shorts で一般的。余白が目立たない | フィルタが 1 段増える (同じ入力を `split` するだけで**入力本数は増えない**) | **推奨** |
| (b) 黒 | 素直。上下が黒帯になる | 最安 | 設定で選べるようにする |
| (c) 単色指定 | (b) の一般化 | 設定項目が増える | 初版では入れない |

**(a) を既定とし、`vertical.crop.background` で `"blur"` / `"black"` を選べるようにする。**

### 3.4 枠を操作する画面をどう作るか

| 案 | 判定 |
| --- | --- |
| `blur_canvas.py` を共用する | **不採用**。ぼかしの指定 (種別・複数・キーフレーム) と結びついており、比率固定や枠数の上限を押し込むと双方が読みにくくなる |
| **`crop_canvas.py` を新設する** | **採用**。必要なのは「枠 1〜2 個・移動・リサイズ・比率固定・上限と下限」だけで、200 行程度で収まる |

### 3.5 どこまで拡大を許すか (R16)

分割の下枠は比率 27:32。1920x1080 のソースでは**高さ上限 1080 → 幅は最大 911px** となり、
出力の 1080 幅へ**必ず約 1.19 倍に拡大される**。これは避けられない。

避けられるのは「さらに小さい枠を選んで極端に拡大すること」なので、
**拡大率の上限を設定値 (既定 2.0 倍) とし、それを割る大きさへは縮められない**ようにする。
ダイアログ側で下限サイズとして効かせる (§5.7)。

### 3.6 オーバーレイ素材 (V2 以降) を引き継ぐか

`Clip.transform` の `x` / `y` は**キャンバス基準の正規化座標**、`scale` は**キャンバス幅に対する比率**である
(`renderer._composite`)。横 (1920 幅) 前提で置いた素材を縦 (1080 幅) へそのまま持ち込むと、
位置も大きさも意図から外れる。

**初版では引き継がず、選択に含まれていたら作成時に 1 度だけ知らせる** (§10 #2)。
黙って落とすと「入れたはずのロゴが無い」に直結するため、知らせることは必須とする。

### 3.7 アーカイブ切り抜き用 Timeline から作れるようにするか

作れないようにする。アーカイブ用 Timeline が参照しているのは
**実行の終わりに消えるクリップ単位の中間ファイル**で (`archive/project_resume.py` が VOD から作り直す仕組み)、
それを指した縦プロジェクトを保存しても開けない。

メニューは `project_io.project_kind(timeline) == KIND_CLIP` のときだけ出す (§10 #4)。

---

## 4. 設計方針

1. **横の出力経路に触らない。** 切り抜きの指定が無ければ、生成される FFmpeg コマンドは 1 文字も変わらない (R15)。
2. **指定だけを持つ。** 切り抜き済みの素材は作らない。元動画をそのまま参照する。
3. **計算は 1 か所に置く。** 枠 → FFmpeg のフィルタ / プレビューの配置 の変換は `src/timeline/crop.py` だけが持ち、
   画面とレンダラはそれを呼ぶ。画面と出力が食い違わないようにするため。
4. **壊れた指定は無視する。** 枠がソースの外を指していたり、数が合わない場合は
   「切り抜き無し」として扱い、書き出しを止めない (開けないプロジェクトを作らない)。
5. **生成元のプロジェクトは変更しない。** 縦プロジェクトの作成は読み取りだけで行う。

---

## 5. 詳細設計

### 5.1 モジュール構成

```text
src/timeline/
    crop.py                   新規。切り抜き指定の読み書き・検証・ジオメトリ計算 (画面とレンダラの共有)
    vertical_builder.py       新規。選択クリップから縦 Timeline を組み立てる
    renderer.py               手直し。_video_filters に切り抜きの分岐を足す
src/gui/timeline/
    crop_canvas.py            新規。ソースのフレーム上で枠を操作する QGraphicsView
    vertical_project_dialog.py 新規。枠決め + 縦プレビュー + 作成
    timeline_editor_dialog.py 手直し。メニュー項目と起動
    timeline_view.py          手直し。コンテキストメニューのフック (ぼかしと同じ形)
    preview_panel.py          手直し。縦プロジェクトのプレビューへ切り抜きを反映
src/settings/
    settings_window.py        手直し。vertical.crop の既定値
```

既存方針どおり**標準ライブラリと既に使っている PySide6 / FFmpeg のみ**で実装する。

### 5.2 データ構造 — `timeline.source["crop"]` 書式 v1

```jsonc
"crop": {
  "version": 1,
  "mode": "single",                       // "single" | "split"
  "media_id": "m1",                       // どの素材に対する枠か (取り違え防止)
  "source": [1920, 1080],                 // 枠を決めたときのソース寸法
  "background": "blur",                   // "blur" | "black" (single のみ意味を持つ)
  "frames": [
    // ソース上の切り抜き (px) と、縦キャンバス上の置き場所 (px)
    { "src": [420, 60, 1080, 1080], "dest": [0, 420, 1080, 1080] }
  ]
}
```

* **`src` はソースのピクセル座標** (x, y, w, h)。正規化しないのは、FFmpeg の `crop` が px を取るためと、
  「1080x1080 まで」という上限が px で決まっているため。
* `dest` は縦キャンバス上の配置 (x, y, w, h)。`single` では計算で決まるが、**保存しておく**。
  枠を決めたときの見え方を、後から設定を変えても再現できるようにするため。
* `source` を持つのは、別寸法の素材へ差し替えられたときに指定を捨てる判断に使うため (§5.3 検証)。
* 将来クリップ単位・キーフレームへ広げる場合は `frames` の要素へ `clip_id` / `at` を足せばよい。

`mode` ごとの `frames` の数と `dest`:

| mode | frames | dest (1080x1920 キャンバス) |
| --- | --- | --- |
| `single` | 1 | キャンバスへ収まるよう拡縮して上下左右の中央。枠 1080x1080 なら `[0, 420, 1080, 1080]` |
| `split` | 2 | 上 `[0, 0, 1080, 640]` / 下 `[0, 640, 1080, 1280]` |

### 5.3 `crop.py` の公開 API

```python
MODE_SINGLE = "single"
MODE_SPLIT = "split"

# 上枠・下枠の比率 (キャンバス 1080x1920 に対する固定値)
SPLIT_TOP = (1080, 640)      # 27:16
SPLIT_BOTTOM = (1080, 1280)  # 27:32

# Timeline から指定を読む。無ければ None。壊れていても None (書き出しを止めない / §4-4)
def load(timeline) -> dict | None

# Timeline へ指定を書く (source["crop"] を置き換える)
def store(timeline, layout) -> None

# 指定が canvas / media と噛み合うか。噛み合わなければ False
def is_valid(layout, media, canvas_width, canvas_height) -> bool

# mode と枠 (ソース px) から書式 v1 の dict を作る
def make_layout(mode, frames_src, media, canvas_width, canvas_height, background) -> dict

# single の dest を計算する (幅 1080 へ収めて上下中央)
def fit_single(src_rect, canvas_width, canvas_height) -> tuple

# 枠の最小サイズ (拡大率の上限から決まる / R16)
def min_src_size(mode, index, canvas_width, canvas_height, max_scale) -> tuple

# FFmpeg の映像フィルタ文字列を返す (レンダラが使う / §5.5)
def filter_chain(layout, canvas_width, canvas_height, fps, cfg) -> str

# プレビュー用: ソース画像をキャンバスへ配置する矩形の組を返す (画面が使う / §5.6)
def preview_rects(layout, canvas_width, canvas_height) -> list[dict]
```

**壊れた指定の扱い** (`is_valid` が False になる条件):

* `version` が未知 / `mode` が未知
* `frames` の数が `mode` と合わない (single=1 / split=2)
* 枠がソースの外を指している (負値・はみ出し)
* `media_id` が Timeline の素材に無い / `source` の寸法が今の素材と違う

いずれも**切り抜き無しとして扱う** (従来どおり縦キャンバスへ収める)。

### 5.4 `vertical_builder.py`

```python
# 選んだクリップから縦 Timeline を作る (元 Timeline は変更しない)
#   timeline      : 元 (横) の Timeline
#   clip_ids      : 選択された V1 クリップの ID
#   layout        : crop.make_layout() が作った指定
#   settings      : setting.json
#   close_gaps    : True なら選択クリップ間の空白を詰める (既定 True)
# 戻り値: (縦 Timeline, 警告メッセージの一覧)
def build(timeline, clip_ids, layout, settings, close_gaps=True) -> tuple
```

処理の順序:

1. **選択の整理** — `clip_ids` のうち V1 (ベース) のクリップだけを `timeline_start` 昇順に並べる。
   OP/ED クリップ (`is_opening_or_ending()`) は除く (縦では OP/ED を結合しないため / §2.1)。
2. **キャンバスの決定** — `output_profile._portrait_profile(settings["vertical"])` から 1080x1920 を得る。
   `fps` は元 Timeline を引き継ぐ (素材が同じなので変える理由が無い)。
3. **時間の詰め直し (R7)** — 先頭を 0 秒起点にする。
   `close_gaps=True` なら直前のクリップの終端へ詰め、False なら元の間隔を保ったまま平行移動する。
   各クリップの `source_in` / `duration` は変えない (素材の同じ範囲を使う)。
4. **メディアの絞り込み (R8)** — 使われる `media_id` だけを `media_pool` へコピーする。
   `path` / `path_rel` はそのまま引き継ぐ (`_media_to_dict` が保存時に相対パスを付け直す)。
5. **音声 (R9)** — `timeline.audio_clip_for(clip.id)` を新しいクリップ ID へ張り替えて A1 へ入れる。
   `muted` / `gain` はそのまま。
6. **字幕 (R10)** — 選択したクリップの区間に**重なる**字幕を、同じ時間差で移す。
   区間からはみ出す字幕は端で切り詰める。切り詰めた結果 0 秒以下になるものは捨てる。
   役割 (`role`) とトラック構成は元のまま引き継ぐ (縦用の見た目は `settings.vertical` が出力時に効かせる)。
7. **オーバーレイ (§3.6)** — 引き継がない。選択範囲に重なるオーバーレイがあれば警告へ積む。
8. **source の組み立て** — `input_path` / `media_path` / `media_id` / `duration_sec` は元 Timeline の
   `source` を引き継ぐ (「続きから」で開くのに要る / R14)。**`archive` キーは引き継がない**
   (クリップ用として開かせるため / §3.7)。`edit_points` も引き継がない (再開時の意味が変わるため)。
9. **切り抜きの指定** — `crop.store(new_timeline, layout)`。
10. **ぼかしの指定** — 引き継がない。ぼかしの座標はソース正規化座標で持っており
    (ver5 resolve8 §5.2)、切り抜き後のキャンバスとは基準が違う。警告へ積む。

### 5.5 レンダラへの差し込み

`_video_filters` (renderer.py:338) の冒頭へ分岐を足す。

```python
def _video_filters(timeline, media, clip, fps, fade_sec, duration=None):
    filters = []
    chain = _crop_chain(timeline, media, fps)          # 追加
    if chain:
        filters.append(chain)                          # 切り抜きが正規化を兼ねる
    elif needs_normalize(media, timeline.width, timeline.height, fps):
        ... 従来どおり ...
```

```python
# 切り抜きの指定があり、その素材に対するものなら FFmpeg のフィルタ文字列を返す。
# 指定が無い・噛み合わない場合は None (従来の正規化が使われる / §4-4)。
def _crop_chain(timeline, media, fps):
    layout = crop.load(timeline)
    if layout is None or not crop.is_valid(layout, media, timeline.width, timeline.height):
        return None
    return crop.filter_chain(timeline, layout, fps, ...)
```

生成されるフィルタ (キャンバス 1080x1920 / fps 60 の例):

**single + 黒背景**

```text
crop=1080:1080:420:0,
scale=1080:1920:force_original_aspect_ratio=decrease,
pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60,format=yuv420p
```

**single + ぼかし背景** (入力は 1 本のまま。`split` で枝分かれさせる)

```text
split=2[cbg][cfg];
[cbg]crop=1080:1080:420:0,scale=1080:1920:force_original_aspect_ratio=increase,
     crop=1080:1920,boxblur=20:2[cbgo];
[cfg]crop=1080:1080:420:0,scale=1080:1920:force_original_aspect_ratio=decrease[cfgo];
[cbgo][cfgo]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=60,format=yuv420p
```

**split**

```text
split=2[ctop][cbtm];
[ctop]crop=1080:640:420:0,scale=1080:640[ctopo];
[cbtm]crop=911:1080:500:0,scale=1080:1280[cbtmo];
[ctopo][cbtmo]vstack=inputs=2,setsar=1,fps=60,format=yuv420p
```

* `-vf` へ渡すのはこの 1 本だけ。後段のフェード (`fade=...`) はカンマで続けて足される
  (最後のフィルタに出力ラベルが無いため、そのまま鎖がつながる)。
* `crop` の引数は必ず整数にする (`crop.py` で丸める)。
* 奇数幅を作らない (`scale` の出力は固定値なので偶数が保証される)。

### 5.6 枠決めダイアログ (`vertical_project_dialog.py`)

```text
┌─ 縦動画プロジェクトの作成 ─────────────────────────────────┐
│  切り抜き方: ( ) 全体   (•) 分割        背景: [ ぼかし ▼ ]   │
│                                                              │
│  ┌─ ソース (1920x1080) ────────────┐  ┌─ 仕上がり ─────┐   │
│  │  ┌────────┐                     │  │ ┌────────────┐ │   │
│  │  │  上枠  │  ドラッグで移動      │  │ │    上枠    │ │   │
│  │  └────────┘  ハンドルで大きさ    │  │ ├────────────┤ │   │
│  │  ┌──────────┐                   │  │ │            │ │   │
│  │  │   下枠   │                   │  │ │    下枠    │ │   │
│  │  └──────────┘                   │  │ └────────────┘ │   │
│  └─────────────────────────────────┘  └────────────────┘   │
│  ◀ ━━━━━●━━━━━━━━━ ▶  (選択したクリップの中で位置を変える)   │
│                                                              │
│  対象: 3 クリップ / 合計 24.8 秒                              │
│  保存先: D:\out\vod_123_vertical.timeline.json  [ 変更... ]  │
│                                  [ キャンセル ] [ 作成 ]     │
└──────────────────────────────────────────────────────────────┘
```

* **フレームの取得**: `src/blur/frames.py:65 first_frame(media, sec, settings)` を使う。
  スライダを動かしたときだけ取り直す (ぼかしの指定画面と同じ扱い)。
  取れなかった場合は灰色の下地に枠だけを出す (画面は開いたままにする)。
* **右の仕上がり**: 取得済みのフレームを Qt 側で切り出して並べるだけ
  (`crop.preview_rects` の矩形へ `QPixmap.copy()` → `scaled()`)。FFmpeg は起動しない。
  **背景のぼかしはプレビューでは掛けない** (掛けると 1 コマごとに重くなる)。
  黒地の上に置き、「実際の書き出しでは背景がぼけます」と注記する。
* **保存先**: 既定は `project_io.default_project_path(settings, 元動画, name_suffix="_vertical")`。
  既存ファイルがあれば上書き確認を出す。
* **「作成」**: `vertical_builder.build()` → `project_io.save()` → 警告があればまとめて 1 回表示 →
  保存先を伝えて閉じる。**開く操作まではしない** (§10 #8)。

### 5.7 枠のキャンバス (`crop_canvas.py`)

```python
class CropCanvas(QGraphicsView):
    frames_changed = Signal()                    # 枠が動いた

    def set_frame(self, width, height, rgb_bytes)  # ソースのフレームを敷く
    def set_mode(self, mode)                       # single / split の切り替え
    def set_frames(self, rects)                    # 枠 (ソース px) を入れる
    def frames(self)                               # 枠 (ソース px) を返す
```

操作の規則:

| 操作 | single | split |
| --- | --- | --- |
| 枠の数 | 1 | 2 (上・下) |
| 移動 | 枠の中をドラッグ | 同じ |
| 大きさ | 四隅のハンドル。**上限 1080x1080** | 四隅のハンドル。**比率固定** (上 27:16 / 下 27:32) |
| 下限 | `crop.min_src_size()` (拡大率の上限から決まる / R16) | 同じ |
| はみ出し | ソースの内側へ収める | 同じ |
| 重なり | — | 上下の枠は重なってよい (同じ場面を別の大きさで見せる構図があるため) |

初期値:

* `single`: ソース中央の 1080x1080 (ソースが 1080 より低ければ高さいっぱい)。
* `split`: 上枠 = 上部中央に幅いっぱい (1920x1138 は入らないため高さ基準で収める)、
  下枠 = 中央下寄りに最大サイズ (911x1080)。

### 5.8 プレビューへの反映 (`preview_panel.py`)

`_on_frame_ready` で作った `pixmap` を、切り抜きの指定があれば**縦キャンバスへ合成したもの**へ差し替える。

```python
    def _on_frame_ready(self, job_id, width, height, data):
        ...
        pixmap = QPixmap.fromImage(image)
        pixmap = self._apply_crop(pixmap)        # 追加: 指定が無ければそのまま返る
```

`_apply_crop` は `crop.preview_rects()` の結果どおりに切り出して並べるだけ。
ダイアログの「仕上がり」と同じ関数を通すため、**編集画面とダイアログで見え方が揃う**。

背景のぼかしはここでも掛けない (§5.6 と同じ理由)。

### 5.9 メニューからの起動

`timeline_editor_dialog.py` へ追加する。

```python
    # 右クリックメニューへ縦動画プロジェクトの項目を足す (timeline_view から呼ばれる)
    def vertical_menu_actions(self, menu):
        if not self._can_make_vertical():
            return
        clips = self._vertical_target_clips()
        if not clips:
            return
        menu.addSeparator()
        count = len(clips)
        label = ("このクリップから縦動画プロジェクトを作成..." if count == 1
                 else f"選んだ {count} クリップから縦動画プロジェクトを作成...")
        menu.addAction(label, self._open_vertical_project)
```

* `_can_make_vertical()`: 元が**クリップ用**かつ**横**のときだけ True (§3.7 / §10 #10)。
* `_vertical_target_clips()`: 選択中の V1 クリップ (無ければ再生ヘッド上のクリップ)。ぼかしと同じ規約。
* `timeline_view.contextMenuEvent` の末尾に `self._add_vertical_actions(menu, clip, track)` を足す
  (中身は `_add_blur_actions` と同じ「親へ委譲」の形)。

---

## 6. 実装手順

**計算 → 出力 → 生成 → 画面**の順に積む。画面が無くても書き出しまで確かめられる形にする。

| Phase | 内容 | できあがりの確認 |
| --- | --- | --- |
| 1 | `crop.py` (書式・検証・ジオメトリ・フィルタ文字列) | 単体テスト (§8.1 A) |
| 2 | `renderer._video_filters` の分岐 | 単体テスト (§8.1 B)。**指定が無ければコマンドが従来と同一** |
| 3 | `vertical_builder.py` | 単体テスト (§8.1 C) |
| 4 | `crop_canvas.py` | 手動確認 (§8.3 ②③) |
| 5 | `vertical_project_dialog.py` | 手動確認 (§8.3 ①〜⑤) |
| 6 | メニューからの起動 (`timeline_editor_dialog` / `timeline_view`) | 手動確認 (§8.3 ①) |
| 7 | プレビューへの反映 (`preview_panel`) | 手動確認 (§8.3 ⑥) |
| 8 | `setting.json` / ドキュメント | 設定の往復 (§8.1 A) |

**Phase 3 で一度区切れる。** ここまでで「縦プロジェクトを作って書き出す」が
(画面を持たない経路で) 通しで動く。

---

## 7. setting.json 定義 (追加分)

```jsonc
"vertical": {
  // 既存: enabled / output_width / output_height / 字幕の上書き …
  "crop": {
    // 全体 (single) の背景。"blur" = ソースをぼかして敷く / "black" = 黒
    "background": "blur",
    // 背景ぼかしの強さ (boxblur の半径:回数)
    "blur_radius": 20,
    "blur_power": 2,
    // 枠をこれ以上拡大させない上限 (R16)。1.0 = 拡大させない
    "max_scale": 2.0,
    // 生成するプロジェクト名の接尾辞
    "project_suffix": "_vertical",
    // 選んだクリップ間の空白を詰めるか
    "close_gaps": true
  }
}
```

* すべて `DEFAULT_SETTINGS` へ持たせ、**設定画面には出さない** (枠はダイアログで決めるものであり、
  数値で触る項目ではない)。`background` だけはダイアログ上で切り替えられる (その回のみ有効)。

---

## 8. テスト計画

### 8.1 単体テスト (`python -m unittest discover -s tests`)

**A. `tests/test_crop_layout.py` (新規)**

| # | 確認 |
| --- | --- |
| A1 | `make_layout` が single で `dest` を幅 1080・上下中央に置く |
| A2 | `make_layout` が split で `dest` を [0,0,1080,640] / [0,640,1080,1280] にする |
| A3 | `store` → `to_json` → `from_json` → `load` で往復する (R12) |
| A4 | 旧プロジェクト (crop キー無し) は `load` が None を返す |
| A5 | 枠がソースの外・数が合わない・未知の mode は `is_valid` が False |
| A6 | 素材の寸法が変わっていたら `is_valid` が False |
| A7 | `min_src_size` が `max_scale` から下限を出す (split 下枠 = 1080/2.0 = 540 幅) |
| A8 | `filter_chain` が 3 分岐それぞれで期待どおりの文字列になる |
| A9 | `crop` の引数が整数に丸められる |

**B. `tests/test_vertical_render.py` (新規)**

| # | 確認 |
| --- | --- |
| B1 | 指定が無ければ FFmpeg コマンドが従来と**完全一致**する (R15) |
| B2 | single (黒) でベース抽出のコマンドへ `crop=…,scale=…,pad=…` が入る |
| B3 | single (ぼかし) で `split` / `boxblur` / `overlay` が入る |
| B4 | split で `vstack=inputs=2` が入る |
| B5 | 切り抜きが**フェードより前**に置かれる |
| B6 | 壊れた指定では従来の正規化チェーンに戻る (書き出しが止まらない) |
| B7 | ギャップ (`_render_gap`) は縦キャンバスの黒になる |

**C. `tests/test_vertical_builder.py` (新規)**

| # | 確認 |
| --- | --- |
| C1 | キャンバスが 1080x1920 / `orientation="portrait"` になる |
| C2 | 選択クリップが 0 秒起点へ詰まる (R7) / `close_gaps=False` では間隔が残る |
| C3 | `source_in` / `duration` は変わらない |
| C4 | `media_pool` が参照分だけになり `path` / `path_rel` を保つ (R8) |
| C5 | リンク音声が新しいクリップ ID へ張り替わる (R9) |
| C6 | 重なる字幕が時刻シフトされ、はみ出しは切り詰められる (R10) |
| C7 | オーバーレイは引き継がれず、警告が返る (§3.6) |
| C8 | `source.archive` / `edit_points` を引き継がない = `project_kind` が clip になる (R14) |
| C9 | 元 Timeline が変更されない |
| C10 | `crop` が保存される |

### 8.2 手動確認 (受け入れ手順)

**準備**: 1920x1080 の動画 1 本を通常どおり実行し、Timeline 編集画面を開く。

| # | 手順 | 期待 |
| --- | --- | --- |
| ① | V1 のクリップを 3 つ選び、右クリック → 「選んだ 3 クリップから縦動画プロジェクトを作成...」 | ダイアログが開き、左にフレーム・右に仕上がりが出る |
| ② | 「全体」で枠をドラッグ・リサイズ | 右の仕上がりが追随する。1080x1080 より大きくできない |
| ③ | 「分割」へ切り替え、上下の枠を動かす | 比率が変わらない。小さくしすぎられない |
| ④ | スライダを動かす | 別の時刻のフレームになる |
| ⑤ | 「作成」 | 保存先が表示され、オーバーレイがあれば警告が出る |
| ⑥ | 「クリップ用」タブの「続きから」で開く | 縦キャンバスで開き、プレビューが**切り抜き後の絵**になる |
| ⑦ | そのまま書き出す | 1080x1920 の動画ができ、プレビューと同じ構図になる |
| ⑧ | 元の横プロジェクトを開いて書き出す | 従来と同じ横動画ができる (R15) |

### 8.3 回帰確認

| 経路 | 確認 |
| --- | --- |
| 横プロジェクトの書き出し | FFmpeg コマンドが従来と同一 (B1) |
| 縦**動画**を入力にした従来の縦出力 | 影響なし (`crop` を持たないため) |
| アーカイブ切り抜き | メニューが出ない。出力は従来どおり |
| ぼかし | 縦プロジェクトへは引き継がれない。横では従来どおり |
| ポイント / 透かし | 縦でも従来どおり (キャンバス幅 1080 基準で透かしが入る) |

---

## 9. 影響範囲・互換性

### 9.1 触るファイル

| ファイル | 変更 |
| --- | --- |
| `src/timeline/crop.py` | **新規** |
| `src/timeline/vertical_builder.py` | **新規** |
| `src/gui/timeline/crop_canvas.py` | **新規** |
| `src/gui/timeline/vertical_project_dialog.py` | **新規** |
| `src/timeline/renderer.py` | `_video_filters` に分岐 (+1 関数) |
| `src/gui/timeline/timeline_editor_dialog.py` | メニュー項目と起動 |
| `src/gui/timeline/timeline_view.py` | メニューのフック (ぼかしと同じ 10 行) |
| `src/gui/timeline/preview_panel.py` | フレームへ切り抜きを反映 |
| `src/settings/settings_window.py` | `vertical.crop` の既定値 |
| `src/settings/setting.json` | 同上 |

### 9.2 互換性

1. **旧プロジェクトは何も変わらない。** `crop` キーが無ければ従来どおり。
2. **新しいプロジェクトを旧アプリで開いても壊れない。** `source` の未知キーは読み飛ばされ、
   縦キャンバス (1080x1920) の普通のプロジェクトとして開く (切り抜きは効かない)。
3. **設定は後方互換。** `vertical.crop` が無い設定ファイルでも既定値で動く。
4. **既存テストを壊さない。** 切り抜きの分岐は `crop` が無ければ通らない。

---

## 10. 確認事項

決めないと作るものが変わるものを上に置いた。

> **決定 (2026-09-25): #1〜#10 すべて「推奨」で進めた** (§0.3)。以下は判断の記録として残す。

| # | 論点 | 選択肢 | 推奨 |
| --- | --- | --- | --- |
| **1** | **全体 (single) の背景** (plan.md 未決 #4) | (a) ソースをぼかして敷く / (b) 黒 / (c) 単色指定 | **(a)**。Shorts で一般的で余白が目立たない。入力は増えず費用も小さい。設定で (b) も選べるようにする (§3.3) |
| **2** | **オーバーレイ素材 (V2 以降) を引き継ぐか** (plan.md 未決 #5) | (a) 引き継がない + 警告 / (b) 位置を縦へ読み替えて引き継ぐ | **(a)**。横基準の正規化座標を縦へ移す規則が一意に決まらない。まず切り抜きを成立させ、要望が出たら足す (§3.6) |
| **3** | **クリップ間の空白を詰めるか** (plan.md 未決 #6) | (a) 詰める / (b) 残す | **(a)**。離れた場面を選ぶ使い方が主で、空白をそのまま持ち込む意味が薄い。設定 `close_gaps` で戻せる |
| **4** | アーカイブ切り抜き用 Timeline からも作れるようにするか | (a) 作れない / (b) 作れる | **(a)**。参照素材が実行後に消えるため、保存しても開けないプロジェクトになる (§3.7) |
| **5** | 切り抜き指定の置き場 | (a) `source["crop"]` / (b) `Timeline.crop_layout` 属性 | **(a)**。往復処理を足さずに済み、ぼかし (`source["blur"]`) と揃う (§2.3) |
| 6 | 全体 (single) の縦位置 | (a) 上下中央に固定 / (b) 利用者が動かせる | **(a)**。初版は決め事を減らす。`dest` を保存しているため後から (b) へ広げられる |
| 7 | 拡大率の上限 | (a) 2.0 倍 / (b) 1.5 倍 / (c) 制限しない | **(a)**。分割の下枠は 1.19 倍が下限のため (b) では窮屈。(c) は極端な画質劣化を招く |
| 8 | 作成後の動き | (a) 保存してパスを知らせる / (b) そのまま縦プロジェクトを開く | **(a)**。編集中の横プロジェクトを閉じる判断まで自動で行わない |
| 9 | 生成したプロジェクトの名前 | (a) `<元動画名>_vertical.timeline.json` / (b) ダイアログで毎回入力 | **(a)** を既定にし、「変更...」で保存先を選べるようにする |
| 10 | 縦プロジェクトからさらに縦を作れるようにするか | (a) 作れない (メニューを出さない) / (b) 作れる | **(a)**。既に縦のものを切り抜く用途が無い |

---

## 付録 A. 用語

| 語 | 意味 |
| --- | --- |
| 枠 | ソースのどこを切り抜くかを表す矩形。`frames[].src` |
| 置き場所 | 縦キャンバス上のどこへ置くか。`frames[].dest` |
| 全体 (single) | 枠 1 つ。余りは背景で埋める |
| 分割 (split) | 枠 2 つ。上下に積んでキャンバスを埋める |
| 拡大率 | `dest` の幅 ÷ `src` の幅。1 より大きいと画質が落ちる |
