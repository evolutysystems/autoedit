# resolve8（ver5） — ぼかしの単純化 (囲む → 追う → キーフレームで直す) 修正設計書

## 0. 本書の位置づけ

* 対象: `docs/request/ver5/request8.md`
  「**ボカシ処理に関して、もっとシンプルでいい気がしてきました**」以下の ①〜⑩ と
  「**そしてこれらをクリップ内でのみ行う**」「**現状のボカし処理を大幅に変更してシンプルにしたい**」。
* 調査は **2026-09-25 時点の実コード (v1.5.0 + ver5 resolve3 / resolve4 / resolve6 / resolve7 実装後)**
  を読んで行った。本文中の行番号・既定値はその時点のもの。
* 本書は現状分析と修正設計を兼ねる。**実装は設計レビュー後**に行う
  (`docs/claude.md`「実装前に設計を行うこと」「いきなり実装を開始しない」)。
* **数値の扱い**: 本書の処理時間・削減量は *計算による見積もり*であり、**実測ではない**。
  見積もりの式はその場に書いた。実測は §8.5 で取り、外れていれば §7 の既定値を見直す。
* 要望から一意に決められない論点は §10「確認事項」へ挙げた
  (`docs/claude.md`「不明点がある場合は推測実装せず設計書へ記載すること」)。
  特に **#1 (人物の自動検出をやめるか)**・**#2 (身体の輪郭を落とすか)**・**#5 (囲みは矩形だけか)**
  は、決めないと作るものが変わる。

### 0.1 要望への回答 (要約)

| # | 要望 | 現状の何が問題か | どう直すか |
| --- | --- | --- | --- |
| ① | **選択したクリップに対してぼかし処理ウィンドウを開く** | 開ける。ただし画面の中身は「動画全体のスライダ」で、再生位置を動かすと対象クリップが勝手に入れ替わる (§2.7) | 開いたクリップに**閉じる**。スライダはそのクリップの区間だけ、コマ番号で刻む (§5.10.2) |
| ② | **キャンバス（動画内）をマウス等で囲む** | 囲めるが、囲む前に「操作」を 3 つ (指定する / 人物・物を追加 / 場所を追加) から選んでおく必要がある (§2.2) | 操作の選択を**無くす**。ドラッグ = いつでも新しい囲み (§5.10.3) |
| ③ | **囲んだ場所をトラッキングするように設定する** | 「人物・物を追加」なら囲んだ物を追い、「場所を追加」だとカメラの動きを追う。どちらを選んだかで結果が変わる (§2.2) | 追い方を **1 つに統一**。囲めば必ず追う (§3.4) |
| ④ | **一番最初に囲った場所に「ボカす / ボカさない」を聞く** | 聞かない。先に「このクリップをぼかす」を入れ、ラジオで「外す」を選んでから囲む手順 (§2.6) | 1 件目の囲みの直後に**ダイアログで聞く** (§5.10.4) |
| ⑤ | **「ボカす」= 囲った場所をぼかす** | できる (mark: mode=blur / target=region) | そのまま。指定 1 件 = 囲み 1 つ (§5.2) |
| ⑥ | **「ボカさない」= 画面全体をぼかし、囲った場所だけ外す**。以後は選んだ側で増やせる | 「このクリップをぼかす」を**別に**入れないと成立しない (§2.6) | 「ボカさない」を選んだ時点で**全面ぼかしを自動で足す** (§3.2) |
| ⑦ | **方向キーでコマ送りできる** | できない。スライダは 0.1 秒刻み、◀ ▶ は ±1.0 秒 (§2.5) | ← / → で 1 コマ。Shift で 10 コマ、Home / End でクリップの端 (§5.10.2) |
| ⑧ | **囲った指定場所を動かせる** | 動かせない。やり直すには削除して囲み直すしかない (§2.3) | 枠の内側をドラッグ = 移動。離した時刻にキーフレームを打つ (§5.10.5) |
| ⑨ | **コマ送りして手で動かしたら、そこにキーフレームを打ち、追従を直す** | キーフレームという概念が無い。追えなければ「固定にする / 取り消す」の 2 択 (§2.4) | **キーフレームを正とし、追従はその間を埋める**。打った所から追い直す (§3.3 / §5.4) |
| ⑩ | **指定場所は拡大・縮小ができる** | できない (§2.3) | 8 個のハンドルで拡大縮小。大きさもキーフレームが持つ (§5.10.5) |
| ⑪ | **これらをクリップ内でのみ行う** | 適用範囲が 3 種類 (クリップ / 場面 / 素材全体) あり、追従も「指定が無ければ素材の全区間」へ広がる (§2.7) | 適用範囲を**クリップ固定**にし、追従もその区間で打ち切る (§3.8) |
| ⑫ | **大幅に単純にしたい** | 層が 4 つ (検出 → 枠 → 指定 → マスク)、`src/blur` で 5,204 行 (§2.1 / §2.9) | 検出の層を外し、層を 2 つ (囲み → マスク) に。**約 2,750 行 / 3 ファイル + モデル 43MB を落とす** (§0.3 / §9.2) |

### 0.2 一言でいうと

**「囲む → ボカす / ボカさない を選ぶ → ずれたらコマ送りして直す」だけにする。**

```text
① クリップを選んで [ぼかし...] を開く
② キャンバスをドラッグして囲む
③ 囲んだ場所は自動で追いかける
④ 1 件目だけ「指定した場所をどうしますか？ [ボカす] [ボカさない]」
⑤ ボカす      → 囲んだ場所がぼける
⑥ ボカさない  → 画面全体がぼけ、囲んだ場所だけ素で見える
⑦⑧⑨⑩ ← / → でコマ送り。ずれていたら掴んで動かす・角を掴んで大きさを変える
          = その時刻にキーフレームが打たれ、そこから追従をやり直す
⑪ 全部そのクリップの中だけ
```

**人物を自動で検出して一覧から選ぶ、という層は無くなる。**
誰をぼかすかは利用者が囲んで決める。検出モデルは「囲んだ枠を追う助け」として裏で使うだけになる。

### 0.3 この作り直しで落とすもの (正直に)

resolve2〜resolve7 で積んだ次は、**人物という層が無くなるため意味を失う**。
残したいものがあれば §10 #1 / #2 で戻せる。

| 落とすもの | 由来 | なぜ落ちるか |
| --- | --- | --- |
| 人物の枠の一覧 (見本画像つき) | resolve7 §5.6.3 | 指定の相手が「囲み」だけになるため、一覧に出すものが無い |
| 枠をクリック / 囲んで選んで外す | resolve2 §5.6.3 | 同上。囲みそのものが指定になる |
| 枠の名前 (アンカーで覚える) | resolve4 R1 / resolve7 §5.6.4 | **落とさない。**名前を付ける相手を「枠」から「囲み」へ移して残す (§5.2 `label`) |
| 隠した枠 / 戻す (`excluded`) | resolve7 §5.8.4 | 自動検出の枠が無いので隠す相手が無い |
| 身体の輪郭 (`silhouette`) | resolve3 §3.1〜§3.3 | 輪郭モデルの当てどころ (検出枠) が無くなる。囲んだ矩形が相手になる (§3.6 / §10 #2) |
| 人物同定 (ReID / `osnet`) | resolve2 §3.7 | resolve7 で「枠の継続の救済」だけになっていた。枠そのものが無くなるため用途ごと消える |
| 全体クラスタリング・統合・分割・主役 | resolve2 / resolve3 / resolve4 | resolve7 で既に廃止済み (本書で残骸も消す) |
| 適用範囲の選択 (この場面 / この素材全体) | resolve7 §3.5 | 要望⑪「クリップ内でのみ」。範囲はクリップ固定になる |
| 「場所を追加 / 人物・物を追加」の区別 | resolve3 §3.6 | 追い方を 1 つに統一する (§3.4) |
| 全画面の位相相関による「場所」の追従 | resolve2 §3.4 | 局所窓の追従で同じことができる (カメラが動けば窓の中身も動く) |
| 囲みの当て込み (`spec.hit_ratio`) | resolve2 §5.6.3 | 囲みの中の「枠」を探す処理が要らなくなる |
| 外す形の余裕 (`keep_margin_*`) | resolve7 §3.4 | 輪郭ちょうどで削る前提の補正。囲みの大きさは利用者が決めるため要らない (フェザーだけ残す) |
| 画面を開いた直後の自動解析 | resolve2 §3.6 | resolve7 で既に廃止済み。本書で `analysis.auto_start` の意味も整理する |

### 0.4 実装状況 (2026-09-25)

**§6 の Phase 1〜12 をすべて実装した。**確認事項 (§10) は「すべて推奨どおり」で決定した。
テストは 860 件すべて通る (ぼかし関連は 157 件へ入れ替え)。

| Phase | 内容 | 結果 |
| --- | --- | --- |
| 1 | `config.py` を v5 へ (旧キーの読み替えつき) | 完了 |
| 2 | `decisions.py` を書式 v5 へ + v4 以前の読み替え | 完了 |
| 3 | `analyzer.py` → `frames.py` / `region_tracker.py` → `correlate.py` / `reid.py`・`silhouette.py`・旧 `tracker.py` を削除 | 完了 |
| 4 | `store.py` を書式 v5 (区切りの指紋) へ | 完了 |
| 5 | `tracker.py` (囲みの追従) | 完了 |
| 6 | `plan.py` (キーフレーム + 追従の合成) | 完了 |
| 7 | `mask_builder.py` の入口と塗る形 | 完了 |
| 8 | `commands.py` の入れ替え + §2.8 の不具合の修正 | 完了 |
| 9 | `blur_canvas.py` (囲む / 掴む / ハンドル / コマ送り) | 完了 |
| 10 | `blur_spec_dialog.py` の書き直し | 完了 |
| 11 | `settings_window.py` / `setting.json` | 完了 |
| 12 | 同梱モデル・ライセンス表記・`models/README.md`・`HowToRelease.md` | 完了 |
| 13 | 実素材での確認と実測 (§8.5) | **未実施** (実素材が要るため) |

実装で本書の案から変えたことは次の 3 点。

| 項目 | 本書の案 | 実装 | 理由 |
| --- | --- | --- | --- |
| キーフレームの時刻の扱い | §5.4 では「大きさはキーの線形補間・位置は追従 + 誤差配分」 | **キーフレームの時刻 (±半コマ) では位置も大きさもキーそのものを返す** (`plan._key_near`) | §5.4 のままだと、区切りの終端のキー (次のキーが無い側) で追従の位置が使われ、**手で置いた位置が数 px ずれた**。「キーフレームは必ず通る」(§4-7) を実装で保証した |
| 「動かさない」指定の追従結果 | §5.4 (1) は「samples が無い / follow=fixed なら補間」 | `plan._segments_of` が `follow=fixed` の指定では**追従結果を 1 件も返さない** | 過去に追った結果がキャッシュに残っていると、固定にしたのに動いてしまう |
| 「ボカす」が効かないときの知らせ | §5.8 の表では「囲み + ボカす で追従が無ければ知らせる」 | **キーフレームの位置には必ず塗れる**ため知らせない (§8.1 C10 の期待に合わせた)。知らせるのは「位置がまったく決まらない」場合とマスク生成の失敗だけ | 追従が失敗しても素で出ることは無い。出るはずのない確認を出すと、利用者が確認に慣れてしまう |

**Phase 13 (実測) は実素材が必要なため未実施。**§8.5 の 5 項目は、実際の切り抜きで
「囲む → 追う → コマ送りで直す」を通したときに測る。見積もりから外れていれば
`track.sample_fps` (既定 10.0) と `editor.frame_cache` (既定 32) を見直す。

**入れ替えたテスト (157 件)。**

| ファイル | 件数 | 中身 |
| --- | --- | --- |
| `tests/test_blur_decisions.py` (新規) | 30 | 書式 v5 / 設定の読み替え / v4 移行 / コマンドの Undo |
| `tests/test_blur_plan.py` (書き直し) | 18 | キーフレーム + 追従の合成 (§5.4 の性質 1〜5) |
| `tests/test_blur_render.py` (書き直し) | 12 | マスクの重ね塗り・塗り方・フェザー |
| `tests/test_blur_track.py` (新規) | 11 | 区切りの作り方・指紋・位相相関だけの追従・見失い |
| `tests/test_blur_dialog.py` (新規) | 19 | 画面 (コマ送り・1 件目のダイアログ・キーフレーム・ハンドル) |
| `tests/test_blur_cache.py` (手直し) | 27 | 由来キー (据え置き) + 区切り単位のキャッシュ |
| `tests/test_blur_core.py` (縮小) | 17 | 設定・座標変換・YOLOX の後処理・モデル無し |
| `tests/test_blur_contour.py` (縮小) | 5 | v4 から引き継いだ自由な囲みの形 |
| `tests/test_blur_preview.py` (手直し) | 10 | 実際のぼかしの反映 |
| `tests/test_blur_renderer_integration.py` (手直し) | 8 | 出力経路への差し込み (据え置き) + 失敗時の確認 |
| 削除 | — | `test_blur_names.py` (枠の名前) / `test_blur_manual.py` (旧 2 種の追従) |

---

## 1. 要望と要件 ID

| ID | 要件 | 出どころ |
| --- | --- | --- |
| **R1** | ぼかし指定画面は「選択したクリップ」を対象に開き、その中だけを扱う | request8 ① / ⑪ |
| R1-2 | 画面の中で再生位置が別のクリップへ移らない (スライダの範囲がクリップの長さ) | request8 ① の含意 |
| **R2** | キャンバスをドラッグで囲める。囲む前にモードを選ぶ必要が無い | request8 ② |
| **R3** | 囲んだ場所は自動で追従する。追い方は 1 種類 | request8 ③ |
| R3-2 | 追えなかった・見失った場合は、黙って捨てず利用者へ知らせる | 既存の方針 (resolve2 §5.9) |
| **R4** | **1 件目**の囲みの直後に「ボカす / ボカさない」を聞く | request8 ④ |
| R4-2 | 2 件目以降は画面上の選択 (ボカす / ボカさない) に従う | request8 ⑥ |
| **R5** | 「ボカす」= 囲んだ場所がぼける | request8 ⑤ |
| **R6** | 「ボカさない」= 画面全体がぼけ、囲んだ場所だけ素で出る | request8 ⑥ |
| R6-2 | 「ボカさない」を選んだとき、全面ぼかしが無ければ自動で足す | request8 ⑥ の含意 |
| R6-3 | 後から足した指定が上に重なる (外した中をまた塗れる) | request8 ⑥ / resolve7 §3.2 の維持 |
| **R7** | 方向キーでコマ送りできる | request8 ⑦ |
| **R8** | 囲んだ場所をマウスで動かせる | request8 ⑧ |
| **R9** | 手で動かした時刻にキーフレームが打たれ、追従がその位置を通る | request8 ⑨ |
| R9-2 | キーフレームを打った所から追従をやり直す (打ち直しのたびに全部やり直さない) | request8 ⑨ の含意 |
| **R10** | 囲んだ場所を拡大・縮小できる | request8 ⑩ |
| **R11** | 指定・追従・マスクはすべてそのクリップの素材区間に閉じる | request8 ⑪ |
| **R12** | 仕組みを減らす。層・ファイル・設定・同梱モデルを削る | request8「大幅に変更してシンプルに」 |
| R13 | 旧プロジェクト (`source.blur` version ≤ 4) を開いても落ちない。移せるものは移す | 既存の互換要件 |
| R14 | 機能 OFF・指定なしのときは FFmpeg に 1 行も足さない | 既存 (resolve2 R9 / resolve7 R6) |
| R15 | 指定の変更はすべて Undo / Redo できる | 既存 (resolve2 §5.6.5) |

---

## 2. 現状分析

### 2.1 いまは層が 4 つあり、要望が要るのは下の 2 つだけ

```text
① 検出       YOLOX-tiny で人物の矩形を出す                detector.py / analyzer.py
② 枠         矩形を時間方向へつなぐ (IoU + ReID)          tracker.py / reid.py
             + 身体の輪郭を N 枚に 1 枚作る                silhouette.py / contour.py
             + 手で囲んだものを追う                        region_tracker.py / manual_tracker.py
③ 指定       「どの相手を・どの区間で・ぼかす / 外す」      decisions.py / plan.py
④ マスク     Timeline 時間のグレースケール動画            mask_builder.py → blur_overlay.py
```

要望 ①〜⑩ に **① と ② の人物部分は 1 つも出てこない**。
出てくるのは「囲む」「追う」「動かす」「大きさを変える」「コマ送り」だけである。

いまの画面は「検出できた枠を選ぶ」ことが主で、囲むのは**検出漏れの補助**という位置づけになっている
(`blur_spec_dialog._on_path_drawn`: 囲みの中の枠を探し、見つからなければ
「囲みの中に枠がありません。新しく足すときは『操作』で…」と返す)。
要望は**この主従を逆にする**ものである。

### 2.2 囲む道が 3 つに分かれている (R2 / R3)

`blur_spec_dialog._build_ui` の「操作:」ラジオ。

| ラジオ | 囲んだときの動き | 追い方 |
| --- | --- | --- |
| 指定する (既定) | 囲みの中の**既にある枠**へ指定を付ける。無ければ何も起きず注意文が出る | — |
| 人物・物を追加 | `KIND_OBJECT` の領域を作る | `manual_tracker`: 局所窓で検出 + 位相相関 |
| 場所を追加 | `KIND_PLACE` の領域を作る | `region_tracker`: **フレーム全体**の位相相関 (カメラの動き) |

* 囲む**前**に正しいラジオを選んでいないと、意図と違う結果になる。
  既定は「指定する」なので、**初めて囲んだ人は「囲みの中に枠がありません」と言われる。**
* 追い方が 2 つあるため、同じ「囲む」操作で挙動が変わる。
  要望③は「囲んだ場所をトラッキングする」の 1 通りしか求めていない。

### 2.3 囲んだ枠は、後から動かすことも大きさを変えることもできない (R8 / R10)

領域は `AddBlurRegion` で `path` (正規化座標の点列) を持って作られ、
**形を変えるコマンドが存在しない。**

```python
# src/timeline/commands.py で領域に対して用意されているのはこの 3 つだけ
AddBlurRegion / RemoveBlurRegion / SetBlurRegionFollow
```

画面にも移動・拡大縮小の操作が無い。`BlurCanvas` のマウス処理は

```python
# src/gui/timeline/blur_canvas.py mousePressEvent / mouseMoveEvent / mouseReleaseEvent
ドラッグ       → 囲みを描く (path_drawn)
クリック       → 枠を選ぶ
ダブルクリック → ぼかす / 外すの切り替え
```

の 3 つで、**掴んで動かす経路が無い** (`QGraphicsItem` も `ItemIsMovable` を立てていない)。
やり直す手段は「削除 → もう一度囲む」だけである。

### 2.4 キーフレームという概念が無い (R9)

`samples` (時刻と矩形の列) は**追従が作った結果**だけを持つ。
利用者が「ここはこの位置」と言う手段が無いため、追従がずれたら次の 2 択になる。

```python
# src/gui/timeline/blur_spec_dialog._add_region (追えなかったとき)
fixed_button = box.addButton(f"この位置に固定で置く ({where})", QMessageBox.AcceptRole)
box.addButton("取り消す", QMessageBox.RejectRole)
```

* 「固定で置く」= 前後 `manual.fixed_span_sec` (既定 2.0 秒) だけ同じ位置に置く。
  動いている相手には当たらない。
* **一部だけ直す**ことができない。10 秒のうち 2 秒だけずれていても、全部固定にするか諦めるかになる。

要望⑨はここを指している。

### 2.5 コマ送りが無い (R7)

```python
# src/gui/timeline/blur_spec_dialog.py 261-267
self.slider.setRange(0, max(int(self._timeline.duration_sec() * 10), 1))   # 0.1 秒刻み
self.slider.valueChanged.connect(lambda v: self._seek(v / 10.0))
self.prev_button.clicked.connect(lambda: self._step(-1.0))                 # ±1.0 秒
self.next_button.clicked.connect(lambda: self._step(1.0))
```

* 刻みは **0.1 秒**。fps 60 の素材では 1 コマ = 0.0167 秒なので、**到達できない時刻がある**。
* 方向キーはスライダにフォーカスがあるときに Qt の既定 (1 目盛 = 0.1 秒) で動くだけ。
  キャンバスにフォーカスがあるときは何も起きない (`BlurCanvas.keyPressEvent` は Delete だけ)。
* 「ずれている 1 コマを見つけて直す」作業ができない。

### 2.6 最初の囲みで「どうしますか」を聞いていない (R4 / R6)

いまの手順は次のとおり。

```text
1. 「このクリップをぼかす」チェックを入れる   ← これを忘れると 2. が効かない
2. 「指定:」ラジオで「外す」を選ぶ
3. 枠をクリック (または囲む)
```

* 1. を忘れると「外す」指定だけができ、一覧に **(効いていません)** と出る
  (`_mark_effective`)。**入れ忘れが構造的に起きる。**
* 要望④⑥は「囲んだ後に聞く」「ボカさないを選んだら全面ぼかしは勝手に付く」である。
  いまは順序が逆で、しかも利用者が 2 つの操作を結び付けて覚える必要がある。

### 2.7 「クリップ内でのみ」になっていない所が残る (R11)

| 箇所 | いまの動き |
| --- | --- |
| 適用範囲 (`scope.width`) | クリップ / 場面 / 素材全体の 3 種類。画面のコンボで選ぶ (resolve7 §3.5) |
| スライダ | **Timeline 全体**。クリップの外へも動ける |
| 対象クリップ | 再生位置が別のクリップに入ると `_seek` で**勝手に切り替わる** (resolve7 §5.8.2 の仕様) |
| 領域の追従範囲 | その領域を指す指定が無ければ `analyzer.collect_spans(timeline)` = **素材の全区間** (`region_tracker.spans_for_region` の fallback)。囲んだ直後は指定が無いため、**まず全区間を追う** |
| 追加した枠の追従 | `manual_tracker._section_of` で「アンカーを含む解析区間」まで広がる。無音カットでつながった区間は数分になることがある |

要望⑪は「クリップ内でのみ」なので、上のすべてをクリップの素材区間へ閉じることになる。

### 2.8 付随して見つかった不具合 (保存が失敗する)

```python
# src/gui/timeline/timeline_editor_dialog.py 780-785
def _update_blur_cache_location(self, cache_path, project_path):
    if not self._blur_analysis:
        return
    self.controller.execute(commands.SetBlurAnalysis(
        cache_path, self._blur_analysis.get("fingerprint", ""),   # ← 引数が 1 つ多い
        project_path=project_path))
```

`SetBlurAnalysis.__init__(self, cache_path, project_path=None)` は
resolve7 で `fingerprint` を外している (commands.py 1735-1745)。
このため **`project_path` に値が二重に渡り `TypeError` になる。**

* 通る条件: 解析結果を持っている (`_blur_analysis` が空でない) + プロジェクトを保存する。
* `_keep_blur_cache_beside_project` は `save_project` の `try` の**外**で呼ばれている
  (timeline_editor_dialog.py 725-728) ため、例外は保存処理の外へ抜ける。
* 本書の作業で `SetBlurAnalysis` 自体を作り直す (§5.9) ので、そこで直す。

### 2.9 いまのコード量 (§9.2 の削減の基準)

| ファイル | 行 | 本書での扱い |
| --- | --- | --- |
| `src/blur/decisions.py` | 778 | **書き直す** (書式 v5 / 約 330 行へ) |
| `src/blur/analyzer.py` | 522 | **解体** → デコードだけを `frames.py` (約 190 行) へ |
| `src/blur/plan.py` | 499 | **書き直す** (約 200 行へ) |
| `src/blur/store.py` | 466 | **書き直す** (約 260 行へ) |
| `src/blur/region_tracker.py` | 452 | **解体** → 相関の道具を `correlate.py` (約 120 行) へ |
| `src/blur/mask_builder.py` | 410 | 手直し (約 340 行) |
| `src/blur/config.py` | 352 | 手直し (約 250 行) |
| `src/blur/manual_tracker.py` | 284 | **改名・書き直し** → `tracker.py` (約 290 行) |
| `src/blur/silhouette.py` | 252 | **削除** (§10 #2) |
| `src/blur/tracker.py` (人物) | 211 | **削除** |
| `src/blur/preview.py` | 210 | ほぼそのまま |
| `src/blur/detector.py` | 200 | そのまま (追従の助けとして残す / §10 #3) |
| `src/blur/geometry.py` | 169 | 手直し (矩形の道具を足す) |
| `src/blur/contour.py` | 163 | 縮小 (自由な囲みの引き継ぎだけに使う) |
| `src/blur/models.py` | 115 | 縮小 (検出モデルだけ見る) |
| `src/blur/reid.py` | 104 | **削除** |
| `src/gui/timeline/blur_spec_dialog.py` | 1,369 | **書き直す** (約 900 行へ) |
| `src/gui/timeline/blur_canvas.py` | 390 | **書き直す** (約 600 行へ / ハンドル操作が増える) |
| `src/timeline/commands.py` のぼかし部 | 約 300 | 入れ替え (約 190 行へ) |
| 合計 | **約 7,250** | **約 4,500 (−2,750)** |

同梱モデルも減る。

| モデル | 大きさ | 本書での扱い |
| --- | --- | --- |
| `yolox_tiny.onnx` | 19.3 MB | 残す (追従の助け / §10 #3) |
| `osnet_x0_25.onnx` | 0.9 MB | **同梱から外す** |
| `silhouette_encoder.onnx` | 26.7 MB | **同梱から外す** (§10 #2) |
| `silhouette_decoder.onnx` | 15.7 MB | **同梱から外す** (§10 #2) |
| 減る量 | **43.3 MB** | `src/main_window.spec` の `glob('models/*.onnx')` から自然に落ちる |

---

## 3. 方式選定

### 3.1 指定の相手をどこまで絞るか (R2 / R12)

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | 現状どおり「検出枠」と「囲み」の両方を指定の相手にする | ✕ 画面が 2 本立てのままで、要望②③⑧⑩ が「補助側」の機能に留まる |
| B | **囲みだけを指定の相手にする** (人物の自動検出による一覧・選択を廃止) | ◎ **採用** |
| C | 検出枠は「候補の点線」として出すが、指定できるのは囲みだけ | △ 見た目は親切だが、解析を走らせる理由が残る (待ち時間が戻る) |

**採用: 案 B。**

* 要望 ①〜⑩ に「検出された人物を選ぶ」操作は**一度も出てこない**。
  出てくるのは囲む・追う・動かす・大きさを変える・コマ送りだけである。
* resolve7 で既に「人物へまとめる処理は精度が低い」として統一を廃止した。
  残っていた「枠を選んで外す」も、枠が途切れる・検出漏れが出るという同じ弱点を抱えている。
* **解析という工程が消える。**画面を開いても、囲むまで 1 フレームもデコードしない。

```text
いま  画面を開く → (調べる) → 枠の一覧 → 枠を選ぶ → 外す
本書  画面を開く → 囲む → 追う (囲んだ 1 件ぶんだけ)
```

**検出モデル (YOLOX) は捨てない。**「囲んだ枠を追う助け」として裏で使う (§3.4)。
利用者から見える機能ではなくなるため、モデルが無い環境でも相関だけで動く (§5.13)。

### 3.2 「ボカす / ボカさない」の聞き方と、全面ぼかしの表し方 (R4 / R5 / R6)

全面ぼかしは resolve7 と同じく **「画面全体を相手にした指定 1 件」** で表す。
新しい概念を足さないので、重ね合わせの規則も 1 つで済む。

| 操作 | できる指定 |
| --- | --- |
| 1 件目を囲んで **[ボカす]** | 囲み 1 件 (`mode=blur`) |
| 1 件目を囲んで **[ボカさない]** | 全面 1 件 (`kind=frame` / `mode=blur`) を**一番下**へ + 囲み 1 件 (`mode=keep`) |
| 2 件目以降に「ボカす」 | 囲み 1 件 (`mode=blur`) を一番上へ |
| 2 件目以降に「ボカさない」 | 囲み 1 件 (`mode=keep`) を一番上へ。全面ぼかしが無ければ**同時に足す** (R6-2) |

**重ね順は並び順のまま (後から足したものが上)。**resolve7 §3.2 の規則をそのまま引き継ぐ。

```text
M = 0 (指定が無ければ真っ黒 = ぼかさない)
効いている指定を並び順に 1 つずつ重ねる:
    α = その指定の形 (フェザー済み / 0〜255)
    v = 255 (ボカす) または 0 (ボカさない)
    M = M × (1 − α/255) + v × (α/255)
```

これで要望⑥の後半「外した中をまた塗る」も自然に成立する
(全面ぼかし → 人を外す → 人の手元の書類をぼかす、と積んだ順に効く)。

**1 件目だけダイアログで聞く理由。**

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | 毎回ダイアログで聞く | △ 要望④は「一番最初に囲った場所に対して」。10 件囲むと 10 回聞かれる |
| B | **1 件目はダイアログ、2 件目以降は画面のラジオに従う** | ◎ **採用** (要望④ + ⑥) |
| C | ダイアログは出さず、常にラジオ | ✕ 要望④の否定 |

**採用: 案 B。**1 件目のダイアログで選んだ側を、画面のラジオにもそのまま反映する
= 続けて同じことをするなら何も選び直さなくてよい (§10 #4)。

### 3.3 位置をどう持つか (R8 / R9 / R10)

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | 追従結果だけを持つ (現状) | ✕ ずれても直せない (§2.4) |
| B | キーフレームだけを持ち、間は線形補間 (追従なし) | △ 動く相手では 1 秒ごとに打つ必要があり手間。要望③の否定 |
| C | **キーフレームを正とし、その間を追従で埋める** | ◎ **採用** |

**採用: 案 C。**役割を分ける。

```text
大きさ … 利用者が決める (キーフレームが持ち、間は線形補間)
位置   … 機械が追う (キーフレームの間を追従で埋め、誤差はキーフレームで吸収する)
```

**大きさを追従で変えない理由。**

* 位相相関は原理上、拡大縮小・回転に追従しない (`region_tracker` の「限界」コメントのとおり)。
* 検出枠の大きさへ合わせると、利用者が⑩で決めた大きさが**勝手に変わる**。
  「囲んだ大きさは利用者のもの」という原則 (§4-7) と衝突する。

**位置の式。**

```text
キー   k1 < k2 < … < kn      時刻 t_i / 矩形 r_i = (x, y, w, h)  正規化キャンバス座標
区切り [span.start, t_1] (後ろ向き) / [t_i, t_{i+1}] (前向き) / [t_n, span.end] (前向き)

大きさ  size(t) = 隣り合うキーの (w, h) を線形補間
                  t < t_1 なら r_1 の、t > t_n なら r_n の大きさをそのまま使う

位置    中間の区切り [a, b] (a = t_i, b = t_{i+1})
          T(t) … 追従が出した中心。a から前向きに追ったので T(a) = r_i の中心
          d    = (r_{i+1} の中心) − T(b)                 ← 追従がためた誤差
          c(t) = T(t) + d × (t − a) / (b − a)            ← 誤差を区間全体へ配ってならす
        先頭の区切り [span.start, t_1]   c(t) = T(t)   (t_1 から後ろ向きに追ったもの)
        末尾の区切り [t_n, span.end]     c(t) = T(t)   (t_n から前向きに追ったもの)
        追従が無い (follow=fixed / 追えなかった)
          c(t) = 隣り合うキーの中心を線形補間。両端の外はそのキーのまま

rect(t) = size(t) を c(t) の中心へ置いた矩形
```

* **キーフレームの時刻では必ずキーの矩形になる** (t = t_i を代入すると `c = r_i の中心`)。
  「手で置いた場所が勝手にずれない」= 要望⑨ の肝。
* 誤差 `d` を区間へ配るので、次のキーフレームで**飛ばない**。
  追従が 3px ずれていれば、その 3px が区間の長さで按分される。
* キーフレームを 1 つ打つ = その前後 2 区切りだけ追い直せばよい (§3.5 / R9-2)。

**見失った先の扱い (R3-2 / §4-8)。**

| 指定 | 追従が途切れた先 | 理由 |
| --- | --- | --- |
| ボカす | **最後に追えた位置を保持**する | 消える = 素で出る。保持していれば少なくとも隠れ続ける |
| ボカさない | **そこで打ち切る** (穴が閉じる) | 穴が残る = 別の場所が素で出る。閉じればぼかしたまま |

どちらも「迷ったらぼける方」へ倒す。併せて画面に
「**ボカさない 2 は 0:04.12 で見失いました [ここへ移動]**」を出し、
利用者がコマ送りでキーフレームを打てるようにする (§5.10.6)。

### 3.4 追従のやり方 (R3)

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | フレーム全体の位相相関 (`region_tracker`) | △ カメラの動きしか追えない。人が動くと置いていかれる |
| B | **局所窓で「検出 + 位相相関」(`manual_tracker` の作り)** | ◎ **採用** |
| C | 相関フィルタ・光学フローのライブラリを足す | ✕ 「不要なライブラリを追加しない」(`docs/claude.md`) |

**採用: 案 B。**既に実装され、テスト (`tests/test_blur_manual.py`) もある作りを整理して使う。

```text
1 サンプルぶんの手順 (前の位置 rect、次のフレーム image)
  1. rect を search_ratio 倍へ広げた窓を切り出す
  2. 窓の中で検出器を走らせ、rect と IoU が min_iou 以上で一番重なる枠を探す
     → 見つかれば **その枠の中心** を採用する (大きさは rect のまま / §3.3)
  3. 見つからなければ、前のフレームの同じ窓と位相相関を取り、平行移動ぶんを足す
     → PSR が match_psr 未満なら「この 1 枚は分からない」として飛ばす
  4. 分からない状態が hold_sec 続いたら、その向きの追従を打ち切る (= 見失った)
```

現状からの変更点。

| # | 変更 | なぜ |
| --- | --- | --- |
| 1 | **検出枠への吸着 (`snap`) を廃止** | 囲んだ形が勝手に人物枠へ置き換わる。⑧⑩ の「囲みは利用者のもの」と衝突する |
| 2 | 検出枠からは**中心のずれだけ**採る | 同上 (大きさを変えない / §3.3) |
| 3 | 追う範囲を**キーフレームの区切り**に閉じる | R9-2 (部分だけ追い直す) / R11 (クリップ内) |
| 4 | 「場所 / 人物・物」の区別を廃止 | 局所窓の相関は、カメラが動けば窓の中身も動くため建物にも効く (§0.3) |
| 5 | 検出モデルが無くても動く | 手順 3 だけで追う。枠は途切れやすくなるが機能は成立する (§5.13) |

追う間隔は `track.sample_fps` (既定 10)。**出力の全フレームは追わない。**
間は §3.3 の補間で埋める (現状と同じ考え方。費用は §5.14)。

### 3.5 追従結果の置き場と、作り直しの単位 (R9-2 / R12)

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | プロジェクト (`source["blur"]`) に追従結果も入れる | ✕ Undo のたびに `deepcopy` される (resolve2 §4-2)。10 秒 × 10fps × 5 件で数千点 |
| B | **キャッシュ (`<プロジェクト>.blur.json`) に区切り単位で持つ** | ◎ **採用** |

**採用: 案 B。**置き場も名前も現状のまま使う (プロジェクトの隣 / `.blur.json`)。

区切り 1 つに**指紋 (`hash`)** を持たせる。

```text
hash = sha1( media_id / 向き / 区切りの開始・終了 / 両端キーの時刻と中心 / follow )[:16]
       ← 大きさ (w, h) は入れない。位置の追従は大きさに依らないため (§3.3 / §5.10.5)
```

| 操作 | 追い直す範囲 |
| --- | --- |
| 囲みを足した | その囲みの全区切り (最初は 2 つ: 前向きと後ろ向き) |
| キーフレームを 1 つ打った | **その前後の 2 区切りだけ** |
| キーフレームを動かした | 同じく前後 2 区切り |
| キーフレームを消した | つながった 1 区切り |
| 大きさだけ変えた | **追い直さない** (位置の追従は大きさに依らない / §3.3) |
| 囲みを消した | その囲みの分をキャッシュから消す |

* 「大きさだけ変えたら追い直さない」が効く場面は多い。⑩ の微調整で毎回待たされない。
* キャッシュを消しても**キーフレームはプロジェクトに残る**ので、追い直せば同じ結果に戻る
  (現状 §8-7 の性質を維持)。

### 3.6 囲みの形 (R2 / R8 / R10)

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | 現状どおり自由曲線 (点列) | ✕ 「掴んで動かす」「角で大きさを変える」の定義が曖昧になる |
| B | **矩形のドラッグだけ** | ◎ **採用** |
| C | 矩形と自由曲線の両方 | △ 操作の選択が復活する (§2.2 で消したもの) |

**採用: 案 B。**

* 要望②「キャンバスをマウス等で囲む」は矩形で満たせる。⑧⑩ が素直に定義できる。
* **塗る形は設定で選べる** (`render.shape` = `rect` / `rounded` / `ellipse`)。
  囲みは矩形で持ち、塗るときに角を丸める・楕円にする。既定は `rect` (囲んだとおり)。
* 旧データの自由曲線は「矩形 + 枠に対する相対の点列 (`outline`)」として引き継ぎ、
  表示と塗りにだけ使う (移動・拡大縮小すれば一緒に変形する)。**新規には作らない。**

### 3.7 コマ送り (R7)

**フレームを単位にする。**スライダの 1 目盛 = 1 コマ。

```text
クリップのコマ数  frames = round((clip.source_out − clip.source_in) × timeline.fps)
時刻 → コマ       index  = round((source_sec − clip.source_in) × fps)
コマ → 時刻       source_sec = clip.source_in + index / fps
```

`Timeline.sec_to_frames` / `frames_to_sec` が既にあるので、それを使う (新しい計算を持たない)。

| キー | 動き |
| --- | --- |
| ← / → | 1 コマ戻る / 進む |
| Shift + ← / → | `spec.step_frames` コマ (既定 10) |
| Home / End | クリップの先頭 / 最終コマ |
| Ctrl + ← / → | 選択中の囲みの**前後のキーフレーム**へ飛ぶ |

* 送り先は常にクリップの中へ丸める (R1-2)。
* **後退は前進より遅い。**PyAV は目的時刻の前のキーフレームまで戻って読み直すため
  1 枚 134ms かかる (ver3 resolve6 の実測)。前進デコードは 9〜11ms。
  この画面だけ `frame_source` のフレーム数キャッシュを広げて (既定 8 → 32)、
  行き来する数コマを取り直さないようにする (§5.12.2)。

### 3.8 クリップ内に閉じるやり方 (R11)

指定は **`span` (素材の区間)** を 1 つだけ持つ。適用範囲の種類は無くす。

```json
"span": { "start": 60.0, "end": 96.5, "clip_id": "c12", "label": "クリップ 3" }
```

* `start` / `end` は**そのクリップの `source_in` / `source_out` を写し取ったもの**。
* `clip_id` と `label` は表示用の目印。**判定には使わない。**

**なぜクリップ ID だけにしないか。**クリップを分割・移動・トリムすると ID が変わる / 増える。
素材の区間で覚えておけば、分割しても両方のクリップで同じぼかしが続く (resolve7 §3.5 と同じ理屈)。

閉じる所は 3 つある。

| どこ | どう閉じるか |
| --- | --- |
| マスク | `span` の外は 1 フレームも塗らない (`plan.layers_at` が `span` で切る) |
| 追従 | 区切りの端が `span.start` / `span.end` を超えない |
| 画面 | スライダの範囲がクリップのコマ数。対象クリップは**入れ替わらない** |

### 3.9 旧書式 (v4) の読み替え (R13)

| v4 | v5 へ | 備考 |
| --- | --- | --- |
| `marks` kind=`frame` (mode=blur) | 全面ぼかし 1 件。`scope` をそのまま `span` にする | `width` が `section` / `media` でも、その区間のままにする (勝手に狭めない) |
| `marks` kind=`region` + `regions[]` | 囲み 1 件。`keys` = 囲んだ時刻 1 点 (`anchor_sec` / 囲みの外接矩形)。`mode` / `follow` / `label` / `outline` を引き継ぐ | 追従は開き直した後に作り直す (キャッシュの schema が上がるため) |
| `marks` kind=`track` (人物の枠) | **捨てる** | 人物検出の層が無くなるため移し先が無い。件数を数えて 1 度だけ知らせる |
| `excluded` (隠した枠) | **捨てる** | 同上 |
| `names` (枠の名前) | **捨てる** | 名前の相手が枠だったため。囲みの名前 (`label`) は引き継ぐ |
| `cache` / `cache_abs` | そのまま引き継ぐ | ファイルは schema 5 で作り直す |

知らせ方は現状の `_warn_if_migrated` を書き換えて 1 度だけ出す。

```text
以前のぼかしの指定のうち、人物の枠を選んで指定したもの (3 件) は
新しい方式へ移せませんでした。

ぼかしたい場所・見せたい場所をマウスで囲み直してください。
手で囲んで足した枠 (2 件) と全面のぼかしは引き継いでいます。
```

---

## 4. 設計方針 (原則)

| # | 原則 | 具体 |
| --- | --- | --- |
| 4-1 | 機能 OFF なら `blur` パッケージを import すらしない | `renderer._prepare_blur_mask` / `timeline_editor_dialog._blur_enabled` の作りを維持 |
| 4-2 | 重いものは `source` に置かない | 追従結果はキャッシュへ。プロジェクトにはキーフレームだけ (§3.5) |
| 4-3 | 座標は 1 種類で持つ | 保存はすべて**正規化キャンバス座標 (0.0〜1.0)**。素材ピクセルとの変換は `geometry` だけが知る |
| 4-4 | 判定は 1 か所に集める | `plan.BlurPlan` が唯一の答え。画面・マスク・プレビューは同じ関数を呼ぶ |
| 4-5 | 例外で画面を落とさない | 追従・デコード・描画の失敗は理由を文字にして `status_label` へ |
| 4-6 | 黙って素で出さない | 「ボカす」が 1 フレームも塗れないときは出力前に確認を出す (現状 `blur_mask_failed` の維持) |
| 4-7 | **キーフレームは必ず通る** | 機械 (追従) が人の指示を上書きしない (§3.3) |
| 4-8 | 追えないときは安全側 | ボカす = 保持 / ボカさない = 打ち切り (§3.3) |
| 4-9 | ハードコードしない | 刻み・ハンドルの大きさ・最小の囲み・追従のしきい値はすべて `setting.json` (§7) |
| 4-10 | すべての変更を Undo できる | 画面の操作は 1 つ残らず `commands.Command` 経由 (§5.9) |

---

## 5. 詳細設計

### 5.1 モジュール構成

```text
src/blur/
  config.py        設定の読み出しと丸め                      手直し (§7)
  decisions.py     指定 (specs) の読み書き / 書式 v5          書き直し (§5.3)
  plan.py          その時刻の形を決める唯一の場所             書き直し (§5.4 / §5.7)
  tracker.py       囲みの追従 (旧 manual_tracker)             書き直し (§5.5)
  correlate.py     位相相関の道具 (旧 region_tracker の一部)  新規 (切り出し)
  frames.py        区間のデコード (旧 analyzer の後半)         新規 (切り出し)
  store.py         追従結果のキャッシュ / 書式 v5             書き直し (§5.6)
  mask_builder.py  マスク動画の生成                          手直し (§5.8)
  preview.py       1 枚へ書き出しと同じぼかしを当てる          ほぼ据え置き
  geometry.py      座標変換 + 矩形の道具                      手直し
  contour.py       相対の点列 (旧データの自由な囲み用)         縮小
  detector.py      人物検出 (追従の助け)                      据え置き
  models.py        モデルの解決 (検出モデルだけ)               縮小
  ── 削除 ──
  analyzer.py      (解体して frames.py へ)
  region_tracker.py(解体して correlate.py / tracker.py へ)
  tracker.py (人物)(削除。同名の新ファイルで置き換え)
  reid.py          (削除)
  silhouette.py    (削除 / §10 #2)

src/gui/timeline/
  blur_spec_dialog.py  ぼかし指定画面        書き直し (§5.10)
  blur_canvas.py       キャンバス            書き直し (§5.11)
  timeline_editor_dialog.py  入口と目印      手直し (§5.12)

src/timeline/
  commands.py      ぼかしのコマンド          入れ替え (§5.9)
```

**依存の向き** (循環を作らない)。

```text
gui ──→ plan ──→ decisions ──→ geometry
          │  └──→ store
          └──→ contour
tracker ──→ frames / correlate / detector / geometry
mask_builder ──→ plan / geometry / contour
preview ──→ mask_builder
```

`plan` は `tracker` を知らない (追従結果はキャッシュ経由で受け取るだけ)。
`tracker` は `decisions` を「区切りを作るため」にだけ読む。

### 5.2 データ構造 — `timeline.source["blur"]` 書式 v5

```json
{
  "version": 5,
  "cache": "myproject.blur.json",
  "cache_abs": "D:/works/myproject.blur.json",
  "specs": [
    {
      "id": "b1",
      "kind": "frame",
      "mode": "blur",
      "media_id": "clip1",
      "span": { "start": 60.0, "end": 96.5, "clip_id": "c12", "label": "クリップ 3" }
    },
    {
      "id": "b2",
      "kind": "area",
      "mode": "keep",
      "media_id": "clip1",
      "span": { "start": 60.0, "end": 96.5, "clip_id": "c12", "label": "クリップ 3" },
      "label": "店員",
      "follow": "track",
      "keys": [
        { "t": 62.000, "rect": [0.3010, 0.2080, 0.1400, 0.4020] },
        { "t": 64.517, "rect": [0.3550, 0.2110, 0.1400, 0.4020] }
      ]
    }
  ],
  "spec_seq": 2
}
```

| キー | 意味 | 備考 |
| --- | --- | --- |
| `version` | 書式版 (5) | v4 以前は読み替える (§3.9) |
| `cache` / `cache_abs` | 追従結果の置き場 (相対 / 絶対) | 現状の作りをそのまま維持 (アーカイブのサブ Timeline は絶対パスが要る) |
| `specs[]` | **指定の一覧。並び順 = 重ね順 (後が上)** | 1 件 = 「全面ぼかし」か「囲み」 |
| `specs[].id` | `b1`, `b2`, … | 一度使った番号は再利用しない (追従結果と取り違えないため) |
| `specs[].kind` | `frame` (画面全体) / `area` (囲み) | `frame` は `keys` を持たない |
| `specs[].mode` | `blur` (ボカす) / `keep` (ボカさない) | `frame` は常に `blur` |
| `specs[].media_id` | 対象の素材 | `span.media_id` を持たず、ここ 1 か所にする |
| `specs[].span` | 効く素材区間 + 表示用の目印 | §3.8。`clip_id` / `label` は判定に使わない |
| `specs[].label` | 表示名 (既定は「ボカす 1」「ボカさない 2」) | 利用者が変えられる (`name_max_len` で切る) |
| `specs[].follow` | `track` (追う) / `fixed` (動かさない) | 追えなかったときに `fixed` へ落とせる |
| `specs[].keys[]` | **キーフレーム**。`t` = 素材の時刻 / `rect` = 正規化キャンバス座標 | 時刻の昇順。最低 1 件 (囲んだ時点) |
| `specs[].outline` | 枠に対する相対の点列 (旧データの自由な囲み) | 新規には作らない (§3.6) |
| `spec_seq` | 採番の控え | |

**数の見積もり (プロジェクト JSON の重さ)。**

```text
キーフレーム 1 件 = {"t":…, "rect":[…]} ≒ 60 バイト
30 秒のクリップで丁寧に直しても 1 秒に 1 つ = 30 件 ≒ 1.8KB
囲み 5 件で 9KB。Undo の deepcopy でも問題にならない大きさ (§4-2)
```

追従結果 (1 秒に 10 点 × 座標) はここには**入れない** (§3.5)。

### 5.3 `decisions.py` (書き直し)

公開する道具。**すべて「新しい辞書を返す」純関数**にして、コマンドが差し替える形を保つ
(現状と同じ作法。`_snapshot` の deepcopy で Undo が効く)。

```python
VERSION = 5
BLUR, KEEP = "blur", "keep"                  # mode
KIND_FRAME, KIND_AREA = "frame", "area"      # kind
FOLLOW_TRACK, FOLLOW_FIXED = "track", "fixed"

# 読み書き
load(timeline)                     -> decisions      # v4 以前は _migrate で読み替え
store(timeline, decisions)         -> section

# 区間 (§3.8)
span_for(timeline, clip)           -> {"start","end","clip_id","label"}
span_contains(spec, media_id, sec) -> bool
clip_number(timeline, clip)        -> int            # 「クリップ 3」の 3

# 指定
next_spec_id(decisions)            -> "b7"
make_frame_spec(spec_id, media_id, span)                            -> spec
make_area_spec(spec_id, mode, media_id, span, t, rect, label, follow=FOLLOW_TRACK) -> spec
with_spec(decisions, spec, to_bottom=False)   -> decisions
without_spec(decisions, spec_id)              -> decisions
with_spec_mode(decisions, spec_id, mode)      -> decisions
with_spec_label(decisions, spec_id, label)    -> decisions
with_spec_follow(decisions, spec_id, follow)  -> decisions
with_spec_moved(decisions, spec_id, delta)    -> decisions   # 重ね順を 1 つ動かす

# キーフレーム (§5.4)
with_key(decisions, spec_id, t, rect, epsilon) -> decisions   # 同じ時刻なら置き換え
without_key(decisions, spec_id, t, epsilon)    -> decisions   # 最後の 1 件は消さない
keys_of(spec)                                  -> [key, …]    # 時刻の昇順
key_at(spec, t, epsilon)                       -> key | None

# 判定
specs_in_clip(decisions, clip)     -> [spec, …]      # その クリップの区間に重なるもの
frame_spec_at(decisions, media_id, sec) -> spec | None
has_any(decisions)                 -> bool
needs_mask(decisions)              -> bool           # mode=blur が 1 件でもあるか
needs_tracking(decisions)          -> bool           # kind=area / follow=track があるか
clean_label(text, max_len)         -> str
```

**キーフレームの時刻の扱い。**

* 画面はコマの時刻しか渡さない (§3.7) ので、`t` は常にコマ境界に乗る。
* 同じキーフレームかの判定は `epsilon = 0.5 / fps` (半コマ) で行う。
  浮動小数の丸めで「同じコマに 2 つのキー」ができるのを防ぐ。
* **最後の 1 件は消せない。**キーが 0 件になると囲みの位置が決まらないため、
  「キーフレームを消す」ではなく「指定を消す」を案内する (§5.10.6)。

**`with_key` の中身 (要点)。**

```python
def with_key(decisions, spec_id, t, rect, epsilon):
    updated = _copy(decisions)
    for spec in updated["specs"]:
        if str(spec.get("id")) != str(spec_id) or spec.get("kind") != KIND_AREA:
            continue
        keys = [k for k in spec["keys"] if abs(float(k["t"]) - float(t)) > epsilon]
        keys.append({"t": round(float(t), 3), "rect": _round4(rect)})
        spec["keys"] = sorted(keys, key=lambda k: float(k["t"]))
    return updated
```

### 5.4 その時刻の矩形を決める (`plan.rect_at`) — §3.3 の実装

```python
# spec のその時刻の矩形 (正規化キャンバス座標)。span の外なら None。
#   tracks : store が持つその指定の追従結果 (無ければ None)
def rect_at(self, spec, source_sec):
    if not decisions.span_contains(spec, spec["media_id"], source_sec):
        return None
    keys = decisions.keys_of(spec)
    if not keys:
        return None

    width, height = self._size_at(keys, source_sec)      # 大きさ = キーの線形補間
    center = self._center_at(spec, keys, source_sec)     # 位置 = 追従 + 誤差の配分
    if center is None:
        return None
    return (center[0] - width / 2.0, center[1] - height / 2.0, width, height)
```

**大きさ (`_size_at`)。**

```python
前後のキー k_before / k_after を取る
  どちらも無い          … あり得ない (keys は 1 件以上)
  片方だけ              … そのキーの (w, h)
  両方                  … ratio = (t - t_before) / (t_after - t_before) で線形補間
```

**位置 (`_center_at`)。**

```python
segment = その時刻を含む区切り (先頭 / 中間 / 末尾)
samples = tracks[spec.id] の中で hash が一致する区切りのサンプル列

(1) samples が無い / follow == "fixed"
        前後のキーの中心を線形補間 (片方だけならそのキーの中心)

(2) 中間の区切り [a, b] で samples がある
        T  = サンプル列を線形補間したその時刻の中心
        Tb = サンプル列の b における中心 (最後のサンプル)
        d  = (b のキーの中心) - Tb
        c  = T + d * (t - a) / (b - a)

(3) 先頭・末尾の区切りで samples がある
        c = T   (端のキーから離れる向きに追ったもの。誤差を配る相手が無い)

(4) samples が途中で切れている (見失った / status == "lost")
        最後のサンプルの時刻 L より後ろでは
          mode == blur  … c = (L の位置)      ← 保持 (§3.3 / §4-8)
          mode == keep  … None を返す         ← 穴を閉じる
        ただし (2) の中間区切りでは、L から b まで「L の位置 → b のキーの中心」を線形で結ぶ
        (次のキーが分かっているなら、そこへ向かわせた方が当たる)
```

**性質 (テストで押さえる / §8.1 B)。**

| # | 性質 |
| --- | --- |
| 1 | `t = キーの時刻` では必ずそのキーの矩形になる (追従の有無に関わらず) |
| 2 | 区切りの継ぎ目で矩形が飛ばない (両側から同じ値になる) |
| 3 | `span` の外は `None` (1 フレームも塗らない / R11) |
| 4 | 追従が無くても、キー 2 件あれば間が線形で埋まる (R9 の最低保証) |
| 5 | 見失った先は `blur` = 保持 / `keep` = 消滅 (§4-8) |

### 5.5 追従 (`tracker.py` / 書き直し)

#### 5.5.1 区切りの作り方

```python
# spec から追う区切りを作る。span とキーフレームで端が決まる (§3.5)
def segments_for(spec):
    keys = decisions.keys_of(spec)
    start, end = float(spec["span"]["start"]), float(spec["span"]["end"])
    out = []
    if keys[0]["t"] - start > MIN_SEGMENT_SEC:
        out.append(_segment(spec, "back", start, keys[0]["t"], keys[0], None))
    for before, after in zip(keys, keys[1:]):
        out.append(_segment(spec, "fwd", before["t"], after["t"], before, after))
    if end - keys[-1]["t"] > MIN_SEGMENT_SEC:
        out.append(_segment(spec, "fwd", keys[-1]["t"], end, keys[-1], None))
    return out
```

* `MIN_SEGMENT_SEC` = 1 サンプル間隔 (`1 / track.sample_fps`)。これより短い区切りは作らない
  (キーフレームだけで十分に埋まる)。
* 区切りの `hash` は §3.5 の式。**両端のキーの時刻と中心が入るので、キーを動かせば指紋が変わる。**

#### 5.5.2 1 区切りを追う

```python
def track_segment(timeline, settings, segment, cfg, on_progress=None, cancel=None):
    media = timeline.media_by_id(segment["media_id"])
    transform = geometry.source_to_canvas_transform(media, timeline.width, timeline.height)
    rect = geometry.normalized_rect_to_source(segment["start_rect"], transform, canvas)
    frames = (frames_module.forward(media, segment["start"], segment["end"], fps, settings)
              if segment["dir"] == "fwd"
              else frames_module.backward(media, segment["start"], segment["end"], fps, settings))
    samples, status, lost_sec = _Follower(cfg, settings).follow(frames, rect, cancel, on_progress)
    return {"hash": segment["hash"], "dir": segment["dir"],
            "start": segment["start"], "end": segment["end"],
            "status": status, "lost_sec": lost_sec,
            "samples": [_to_normalized(s, transform, canvas) for s in samples]}
```

`_Follower` は旧 `manual_tracker._Follower` を次の 2 点だけ変えて使う (§3.4)。

```python
# 変更 1: 吸着 (snap) を呼ばない — 囲んだ形をそのまま初期位置にする
# 変更 2: 検出枠は中心だけ採り、大きさは初期値を保つ
def _step(self, image, previous_image, rect):
    found = self._best_detection(image, rect, self._min_iou)     # 窓の中で検出
    if found is not None:
        cx = found[0] + found[2] / 2.0                           # 検出枠の中心
        cy = found[1] + found[3] / 2.0
        return (cx - rect[2] / 2.0, cy - rect[3] / 2.0, rect[2], rect[3])
    return self._correlate(previous_image, image, rect)           # 位相相関 (平行移動)
```

`status` の決め方。

| status | 条件 | 画面の見せ方 |
| --- | --- | --- |
| `ok` | 区切りの端まで追えた | 何も出さない |
| `lost` | 分からない状態が `track.hold_sec` 続いて打ち切った | 「0:04.12 で見失いました [ここへ移動]」 |
| `failed` | 1 点も追えなかった (素材が開けない / 窓が小さすぎる) | 「追いかけられませんでした [この位置に固定する]」 |

#### 5.5.3 まとめて走らせる入口

```python
# 指紋の合わない区切りだけを追い直す。戻り値: 追い直した区切りの数
def ensure_tracks(timeline, settings, tracks, decisions, cfg, on_progress=None, cancel=None):
    wanted = [seg for spec in decisions["specs"] if _needs(spec)
              for seg in segments_for(spec)]
    missing = store.missing_segments(tracks, wanted)
    for index, segment in enumerate(missing):
        if cancel is not None and cancel():
            break
        result = track_segment(timeline, settings, segment, cfg, …, cancel)
        store.put_segment(tracks, segment["spec_id"], segment["media_id"], result)
    store.drop_unused(tracks, decisions)      # 消した指定・古い区切りを捨てる
    return len(missing)
```

* 画面 (§5.10.7) と書き出し (§5.8) の**両方がこの 1 つを呼ぶ**。
  画面で追い終えていれば、書き出し側は `missing` が空なので何も走らない。
* 進捗は「区切りの数」で按分する (区切りごとの長さの差は無視する / 実用上十分)。

### 5.6 追従結果のキャッシュ (`store.py` / 書き直し)

```json
{
  "schema": 5,
  "generator": "Stretheus 1.6.0",
  "settings_key": "3f9c…",
  "canvas": { "width": 1920, "height": 1080 },
  "sample_fps": 10.0,
  "media": { "clip1": { "key": "input=…|size=1920x1080|dur=612.34", "width": 1920, "height": 1080, "duration": 612.34 } },
  "tracks": {
    "b2": {
      "media_id": "clip1",
      "segments": [
        { "hash": "8a1c…", "dir": "fwd", "start": 62.0, "end": 64.517,
          "status": "ok", "lost_sec": null,
          "samples": [ { "t": 62.0, "cx": 0.371, "cy": 0.409, "score": 1.0 }, … ] }
      ]
    }
  }
}
```

| 変更 | 内容 |
| --- | --- |
| `schema` 4 → **5** | 古いキャッシュは読まずに作り直す (人物の枠は使えないため) |
| `tracks` の形 | 配列 → **指定 ID を鍵にした辞書**。指定を消したらその項目を消すだけ |
| サンプルの中身 | `x, y, w, h` → **`cx, cy` (中心) だけ**。大きさは追わない (§3.3) |
| 座標 | 素材ピクセル → **正規化キャンバス座標**。`plan` が変換せずに使える (§4-3) |
| `spans` (解析済み区間) | **廃止**。区切りの `hash` が同じ役目を果たす |
| `_thumbs` / `thumb` | **廃止** (見本画像が要らなくなった) |
| `settings_key` | `schema` + **キャンバスの寸法** + `track.sample_fps` + 検出モデルの版 |

`settings_key` にキャンバスの寸法を入れるのは、正規化座標が
「キャンバスへレターボックスで載せた後の座標」だからである
(縦横が変われば同じ数値が別の場所を指す)。

公開する道具。

```python
SCHEMA_VERSION = 5
cache_path_for(project_path, working_dir)        # 現状どおり (<プロジェクト>.blur.json)
settings_key(cfg, timeline)
media_key(timeline, media)                       # 現状のまま (中間ファイルの由来で判定)
new_tracks(timeline, cfg, media_ids)
load(path, expected_key) / load_any(path) / save(path, tracks)
media_snapshot(timeline, media_ids) / valid_media / drop_stale_media / describe_mismatch
segment_of(tracks, spec_id, hash)      -> segment | None
missing_segments(tracks, wanted)       -> [segment, …]
put_segment(tracks, spec_id, media_id, result)
drop_spec(tracks, spec_id) / drop_unused(tracks, decisions)
```

`media_key` / `drop_stale_media` / `describe_mismatch` は resolve4 で
「中間ファイルのパスで指紋を作ると毎回捨てる」問題を解いた部分なので、**そのまま残す**。

### 5.7 `plan.py` の公開 API (書き直し)

```python
class BlurPlan:
    #   timeline  : 対象の Timeline
    #   tracks    : store.load の戻り値 (無くてもよい)
    #   decisions : decisions.load の戻り値
    #   cfg       : config.config の戻り値
    def __init__(self, timeline, tracks, decisions, cfg)

    # その時刻に効いている指定を重ね順のまま返す (マスク・プレビュー・目印が使う)
    #   [{"spec", "mode", "shape"}, …]   先頭が一番下
    #   shape = {"kind","rect"(キャンバス px),"outline","feather","grow","label"}
    def layers_at(self, media_id, source_sec)

    # 1 件の指定のその時刻の矩形 (正規化キャンバス座標 / §5.4)
    def rect_at(self, spec, source_sec)

    # 画面が使うもの
    def specs(self)                       # 並び順のまま
    def specs_in_clip(self, clip)
    def label_of(self, spec)              # 既定は「ボカす 1」/「ボカさない 2」/「画面全体」
    def describe(self, spec)              # 「ボカさない / 店員 / キー 4 点」
    def status_of(self, spec)             # {"status","lost_sec"} 追従の具合
    def has_blur(self)                    # mode=blur が 1 件でもあるか (マスクの要否)
```

`shapes_at` / `mode_of_track` / `mark_for_track` / `hidden_entries` /
`untracked_regions` / `default_label` / `_resolve_names` / `_entry_by_anchor` は**消える**
(相手が「囲み」だけになり、枠・アンカー・名前表が無くなるため)。

### 5.8 マスクの生成 (`mask_builder.py` / 手直し)

塗り方の骨は変えない (resolve7 §3.2 の重ね塗り)。変わるのは 3 か所。

| # | 変更 | 内容 |
| --- | --- | --- |
| 1 | 入口が読むもの | `analysis` → `tracks` (キャッシュ)。`_analysis_for` は `_tracks_for` へ |
| 2 | 足りない追従をここで作る | `tracker.ensure_tracks` を呼ぶ。画面で追い終えていれば 0 件で即戻る |
| 3 | 効かない指定の知らせ方 | 「ボカす」指定が 1 フレームも塗れないときだけ `context.blur_mask_failed = True` (§4-6) |

```python
def _tracks_for(timeline, context, decisions, cfg, cache_path):
    tracks = store.load(cache_path, store.settings_key(cfg, timeline))
    if not decisions_module.needs_tracking(decisions):
        return tracks                       # 全面ぼかしだけ = 追従は要らない
    if tracks is None:
        tracks = store.new_tracks(timeline, cfg, _media_ids(decisions))
    else:
        store.drop_stale_media(tracks, timeline)
    try:
        from . import tracker               # noqa: PLC0415 (必要なときだけ読む)
        tracker.ensure_tracks(timeline, context.settings, tracks, decisions, cfg,
                              on_progress=context.progress_subcallback("ぼかしの追従"))
    except Exception as error:              # noqa: BLE001 (出力自体は止めない)
        _logger.exception("ぼかしの追従に失敗しました: %s", error)
    if cache_path:
        store.save(cache_path, tracks)
    return tracks
```

**「効かない指定」の判定 (§4-6)。**

| 指定 | 追従が無いとどうなるか | 知らせるか |
| --- | --- | --- |
| `frame` / `blur` | 影響なし (追従に依らず必ず塗れる) | — |
| `area` / `blur` | キーが 1 件ならその位置に置かれる。区間全部は塗れない | **知らせる** (素で出る恐れ) |
| `area` / `keep` | 穴が開かない = ぼけたまま | 知らせない (安全側) |

`_MaskPainter` は据え置き。`shape["kind"] == "frame"` の特別扱い (配列へ一括代入) と、
「指定が 1 件も効いていないフレームは真っ黒の絵を使い回す」もそのまま残す (R14 の費用ゼロ経路)。

塗る形は `render.shape` で決める。

| `render.shape` | 塗り |
| --- | --- |
| `rect` (既定) | 囲んだ矩形のまま |
| `rounded` | 角丸 (半径 = 短辺 × 0.2) |
| `ellipse` | 楕円 |
| (`outline` を持つ指定) | 相対の点列を矩形へ当てはめた多角形 (旧データの引き継ぎ / §3.6) |

### 5.9 コマンド (`commands.py` / 入れ替え)

| 新しいコマンド | 用途 | Undo の単位 |
| --- | --- | --- |
| `AddBlurSpecs(specs, to_bottom=False)` | 指定を 1 件以上足す。**「ボカさない」の 1 件目は「全面 + 囲み」を 1 回で足す** | 1 操作 |
| `RemoveBlurSpecs(spec_ids)` | 指定を消す (キャッシュの掃除は次の追従で行う) | 1 操作 |
| `SetBlurSpecMode(spec_id, mode)` | ボカす ⇔ ボカさない | 1 操作 |
| `SetBlurKey(spec_id, t, rect)` | **移動・拡大縮小・キーフレームの追加を兼ねる** | ドラッグ 1 回 = 1 操作 |
| `RemoveBlurKey(spec_id, t)` | キーフレームを消す (最後の 1 件は消さない) | 1 操作 |
| `MoveBlurSpec(spec_id, delta)` | 重ね順を 1 つ動かす | 1 操作 |
| `SetBlurSpecFollow(spec_id, follow)` | 追う ⇔ 動かさない | 1 操作 |
| `RenameBlurSpec(spec_id, label)` | 表示名を変える | 1 操作 |
| `SetBlurCache(cache_path, project_path=None)` | 追従結果の置き場を書き留める (旧 `SetBlurAnalysis`) | 1 操作 |

**消えるコマンド** (12 個 → 9 個)。

```text
AddBlurMark / AddBlurMarks / RemoveBlurMark / SetBlurMarkMode / SetBlurMarkScope /
MoveBlurMark / AddBlurRegion / RemoveBlurRegion / SetBlurRegionFollow /
ExcludeBlurTracks / RestoreBlurTrack / RenameBlurTrack / SetBlurAnalysis
```

`SetBlurCache` は `SetBlurAnalysis` の置き換えで、引数は `(cache_path, project_path=None)`。
**§2.8 の不具合 (呼び出しの引数が 1 つ多い) は、呼び出し側を書き直すことで併せて直す。**

ドラッグ中の扱い。

```text
mousePressEvent   … 掴んだ位置を覚えるだけ (コマンドは積まない)
mouseMoveEvent    … キャンバス上の枠だけ動かす (仮表示)
mouseReleaseEvent … SetBlurKey を 1 回だけ積む
```

こうしないと 1 回のドラッグで数十のコマンドが積まれ、Ctrl+Z が数十回必要になる。

### 5.10 ぼかし指定画面 (`blur_spec_dialog.py` / 書き直し)

#### 5.10.1 画面の構え

```text
┌───────────────────────────────────────────────┬──────────────────────────┐
│                                               │ 指定 (下が優先)  [名前]  │
│                                               │  ① 画面全体をぼかす      │
│              キャンバス                        │  ② ボカさない / 店員     │
│     (ドラッグで囲む / 掴んで動かす /            │  ③ ボカす / 書類         │
│      角を掴んで大きさを変える)                  │        [▲][▼][削除]      │
│                                               ├──────────────────────────┤
│                                               │ キーフレーム (4)         │
│                                               │  ◆ 0:02.00   (囲んだ所)  │
│                                               │  ◆ 0:04.52               │
│                                               │  ◆ 0:06.18               │
│                                               │      [ここに打つ][削除]  │
├───────────────────────────────────────────────┴──────────────────────────┤
│ |◀  ◀ [■■■■■◆■■■■■■◆■■■■■■■■■■■■] ▶  ▶|   0:02.10  (コマ 126 / 1,830)  │
├──────────────────────────────────────────────────────────────────────────┤
│ 対象: クリップ 3 (0:01:02〜0:01:39)   これから: (◉ボカす ○ボカさない)     │
│ 表示: [実際のぼかし ▼]  [✓画像・字幕も表示]                              │
├──────────────────────────────────────────────────────────────────────────┤
│ 選択中: ボカさない / 店員    ⚠ 0:04.12 で見失いました [ここへ移動]        │
│                             [動かさない] [名前を変更] [削除]              │
├──────────────────────────────────────────────────────────────────────────┤
│ ドラッグで囲む / 枠を掴んで移動 / 角で大きさ / ← → でコマ送り    [閉じる] │
└──────────────────────────────────────────────────────────────────────────┘
```

現状から**消える部品**: 「このクリップの枠」一覧 + 「調べる」ボタン、
「隠した枠」一覧 + 「隠した枠も表示」 + 「戻す」、「操作」ラジオ 3 つ、
「適用範囲」コンボ 2 つ、「囲みを取り消す」、選択中の「外す / ぼかす / 指定を消す / 枠を隠す」。

**増える部品**: キーフレームの一覧、スライダ上のキーフレームの目印 (◆)、
コマ番号の表示、「これから」ラジオ、追従の具合の警告行。

#### 5.10.2 クリップの中に閉じる / コマ送り (R1 / R1-2 / R7)

```python
def __init__(self, controller, tracks=None, parent=None, cache_path=None,
             settings=None, clip=None):
    self._clip = clip                       # 呼び出し元が渡す。None なら開かない (§5.10.8)
    self._fps = int(self._timeline.fps or 60)
    self._frames = max(int(round((clip.source_out - clip.source_in) * self._fps)), 1)
    self.slider.setRange(0, self._frames - 1)   # 1 目盛 = 1 コマ
```

| 操作 | 実装 |
| --- | --- |
| スライダ | 値 = クリップ内のコマ番号。`source_sec = clip.source_in + index / fps` |
| ← / → | `_step(±1)` |
| Shift + ← / → | `_step(±editor.step_frames)` (既定 10) |
| Home / End | `0` / `frames - 1` |
| Ctrl + ← / → | 選択中の指定の前後のキーフレームへ |
| 時刻の表示 | `0:02.10 (コマ 126 / 1,830)` |

* すべて `max(0, min(index, frames - 1))` で丸める。**クリップの外へは出ない。**
* 対象クリップの入れ替えは**しない**。別のクリップを触りたければ画面を閉じて選び直す
  (要望⑪。現状の「勝手に切り替わる」挙動をやめる / §2.7)。
* ショートカットは `QShortcut` ではなく `keyPressEvent` で受ける
  (キャンバスのフォーカス有無で挙動が変わらないようにする / §2.5 の反省)。

#### 5.10.3 囲む (R2)

```text
キャンバスの「枠が無い所」でドラッグ → 矩形の囲み (ゴムバンド)
離す → editor.min_size_ratio (既定 0.01 = 画面幅の 1%) 未満なら捨てる (クリック扱い)
     → 1 件目なら §5.10.4 のダイアログ、2 件目以降は「これから」ラジオに従う
```

* 囲みは**その時刻のキーフレーム 1 件**を持って作られる (`keys = [{t: 今のコマ, rect}]`)。
* 作った直後に追従を走らせる (§5.10.7)。
* 「操作」ラジオは無いので、**囲む前に何も選ばなくてよい** (§2.2 の解消)。

#### 5.10.4 1 件目のダイアログ (R4 / R5 / R6)

```text
┌────────────────────────────────────────┐
│ 指定した場所をどうしますか？             │
│                                        │
│ ボカす     … 囲んだ場所だけをぼかします  │
│ ボカさない … 画面全体をぼかし、           │
│              囲んだ場所だけを残します     │
│                                        │
│          [ ボカす ] [ ボカさない ]      │
│                          (Esc = 取消)   │
└────────────────────────────────────────┘
```

```python
def _ask_first_mode(self):
    box = QMessageBox(self)
    box.setWindowTitle("ぼかし")
    box.setText("指定した場所をどうしますか？")
    box.setInformativeText("ボカす … 囲んだ場所だけをぼかします\n"
                           "ボカさない … 画面全体をぼかし、囲んだ場所だけを残します")
    blur_button = box.addButton("ボカす", QMessageBox.AcceptRole)
    keep_button = box.addButton("ボカさない", QMessageBox.AcceptRole)
    box.addButton("取り消す", QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is blur_button:
        return blur_decisions.BLUR
    if box.clickedButton() is keep_button:
        return blur_decisions.KEEP
    return None
```

* 出す条件: **そのクリップの区間に指定が 1 件も無いとき** (画面ごと・囲みごとではない)。
  一度作ってから全部消せば、また 1 件目として聞く。
* 選んだ側を「これから」ラジオへ反映する (R4-2 / §3.2)。
* 「ボカさない」なら `AddBlurSpecs([全面, 囲み])` を **1 回**積む (Undo 1 回で両方戻る)。

#### 5.10.5 動かす・大きさを変える (R8 / R9 / R10)

```text
枠の内側をドラッグ        → 平行移動
枠の 8 つのハンドルをドラッグ → 大きさを変える (角 = 縦横 / 辺 = 片方向)
Shift を押しながら角      → 縦横比を保つ
Alt を押しながら角        → 中心を動かさない
離した瞬間                → SetBlurKey(選択中の指定, 今のコマの時刻, 新しい矩形)
```

* **離した時刻にキーフレームが打たれる** (要望⑨)。既にその時刻にキーがあれば置き換える。
* 打たれたキーの前後 2 区切りだけ追い直す (§3.5)。待ち時間は 1〜2 秒程度 (§5.14)。
* **大きさだけ変えた場合は追い直さない。**位置の追従は大きさに依らないため、
  区切りの指紋には「両端キーの時刻と**中心**」だけを入れる (§3.5)。
  = ⑩ の微調整を繰り返しても待たされない。
* 矩形はキャンバスの内側へ丸める (`geometry.clamp_rect` の正規化版)。
* 最小の大きさは `editor.min_size_ratio`。それ未満には縮められない。

#### 5.10.6 一覧と警告

**指定の一覧** (重ね順。下が優先)。

```text
① 画面全体をぼかす
② ボカさない / 店員          キー 4 点
③ ボカす / 書類              キー 2 点  ⚠ 0:04.12 で見失いました
```

* クリックで選択 → キャンバスの枠も選択される。
* `▲` `▼` で重ね順、`削除` で指定を消す。
* 「画面全体をぼかす」も 1 件として並ぶので、**利用者が消せる** (§10 #7)。
* 「ボカさない」しか無い状態 (全面ぼかしを消した) では、その指定に
  「(ぼかしが掛かっていないため効いていません)」を添える (現状の `_mark_effective` を踏襲)。

**キーフレームの一覧**。

* 選択中の指定のキーを時刻順に並べる。クリックでその時刻へ飛ぶ (コマ単位)。
* `ここに打つ` = 今のコマに、いまの表示位置でキーを打つ (マウスを使わずに固定できる)。
* `削除` = そのキーを消す。**残り 1 件なら消さず**「指定そのものを削除してください」と案内する。

**追従の警告**。

| 具合 | 出す文言 | 添えるボタン |
| --- | --- | --- |
| `lost` | 「0:04.12 で見失いました」 | `[ここへ移動]` (その時刻へ飛ぶ) |
| `failed` | 「追いかけられませんでした」 | `[動かさない]` (`follow=fixed` にする) |
| 検出モデルが無い | 「人物検出モデルが無いため、模様の変化だけで追います」 | — |

#### 5.10.7 追従の走らせ方 (R3)

```python
def _retrack(self):
    if not self._cfg["track"]["auto_start"]:
        self.status_label.setText("[追う] を押すと囲んだ場所を追いかけます。")
        return
    progress = QProgressDialog("囲んだ場所を追いかけています…", "キャンセル", 0, 100, self)
    worker = BlurTrackWorker(self._timeline, self._settings, self._tracks,
                             self._decisions(), self._cfg, parent=self)
    …  # 現状の BlurAnalysisWorker と同じ作り (QThread + processEvents で待つ)
    store.save(self._cache_path, self._tracks)
    self._preview.set_tracks(self._tracks)
    self._refresh()
```

* 走る条件は「指紋の合わない区切りがあるとき」だけ。無ければ 1 フレームもデコードしない。
* `track.auto_start` が false なら、`[追う]` ボタンを押したときだけ走らせる (現状の作法を踏襲)。
* 走っている間もキャンセルできる (`QProgressDialog` の「キャンセル」)。
  キャンセルすると追えた所までが残り、残りは「見失った」と同じ扱いになる。

#### 5.10.8 入口と後始末

```python
# Timeline 編集画面から
dialog = BlurSpecDialog(self.controller, self._blur_tracks, parent=self,
                        cache_path=self._blur_cache_path, settings=self._settings,
                        clip=clips[0] if clips else None)
```

* クリップが選ばれていなければ「ぼかすクリップを選んでください」と出して**開かない**
  (現状は先頭のクリップを勝手に対象にする / `_first_clip`)。
* 閉じるとき (`done`) に追従スレッドを止め、`frame_source` を閉じる (現状どおり)。
* 追従結果は呼び出し元へ返す (`tracks()`)。置き場は `SetBlurCache` で指定へ書き留める。

### 5.11 キャンバス (`blur_canvas.py` / 書き直し)

#### 5.11.1 描くもの

| 層 (Z) | 中身 |
| --- | --- |
| 0 | フレーム (素材の 1 枚。必要なら実際にぼかした絵 / §5.12.1) |
| 5 | 「ボカさない範囲 (緑)」の重ね塗り (表示モードが `keep` のとき) |
| 6 | 画像・動画・字幕のオーバーレイ (現状どおり。出力ではぼけない層) |
| 10 | 指定の枠 (`blur` = 赤 / `keep` = 緑 / 全面 = 画面の縁に薄い赤) |
| 12 | 選択中の枠 (太線) |
| 13 | **ハンドル 8 個** (選択中のみ) |
| 14 | ラベル (「店員」など) + キーフレームの印 (◆ = このコマはキー) |
| 20 | ドラッグ中のゴムバンド |

#### 5.11.2 マウスの当たり判定

```python
def _hit(self, point):
    # 1. 選択中の枠のハンドル (角 4 + 辺 4)。ハンドルの当たり判定は editor.handle_px 四方
    # 2. 枠の内側 (小さい枠を優先 = 重なっていても選べる)
    # 3. どれでもなければ「新しい囲み」
```

* ハンドルの大きさは `editor.handle_px` (既定 10)。**表示倍率に合わせて拡大する**
  (キャンバスは `fitInView` で縮小表示されるため、画面上で常に同じ大きさに見えるようにする)。
* 重なった枠は「小さい方が手前」とする (現状 `keys_at` と同じ規則)。
* 発するシグナル。

```python
spec_selected   = Signal(str)                 # 枠を選んだ (指定 ID / "" で解除)
rect_committed  = Signal(str, tuple)          # 移動・大きさ変更を確定 (指定 ID, 正規化矩形)
area_drawn      = Signal(tuple)               # 新しい囲み (正規化矩形)
context_requested = Signal(str, QPoint)
delete_requested  = Signal()
step_requested    = Signal(int)               # ← → から (コマ数)
```

`path_drawn` (点列) は `area_drawn` (矩形) へ置き換わる。
`shape_activated` (ダブルクリックでぼかす/外すの切替) は残す (便利で、覚えなくても困らない)。

#### 5.11.3 キーフレームの見せ方

| 今のコマ | 枠の線 | 印 |
| --- | --- | --- |
| キーフレームの時刻 | 実線 | ◆ (塗り) |
| キーとキーの間 (追従あり) | 実線 (細) | ◇ (白抜き) |
| 追従が無い / 補間だけ | 破線 | ◇ |
| 見失った先 | 破線 + 灰 | ⚠ |

スライダの下にキーフレームの位置を ◆ で描く (`QSlider` の上に薄い目盛を重ねる)。
「どこにキーを打ったか」が一目で分かる = ⑨ の作業がしやすくなる。

### 5.12 プレビューと Timeline 編集画面 (`timeline_editor_dialog.py` / 手直し)

#### 5.12.1 指定画面の中のプレビュー

現状の 3 モードをそのまま残す (`editor.preview_mode`)。

| モード | 中身 |
| --- | --- |
| 実際のぼかし (既定) | `blur.preview.BlurPreview` で書き出しと同じマスク・同じ効果を当てる |
| ボカさない範囲 (緑) | 最終マスクを反転して緑で重ねる |
| 枠だけ | 何も重ねない (追従のずれを見るときに速い) |

`BlurPreview` は `analysis` を `tracks` に変えるだけで、中身は据え置き
(`set_analysis` → `set_tracks`)。

#### 5.12.2 コマ送りの応答

```python
self._frame_source = create_frame_source(
    self._settings,
    preview_cfg=dict(preview_cfg, cache_frames=int(cfg["editor"]["frame_cache"])))
```

* `frame_source` は `preview_cfg` を受け取れるので、**この画面だけキャッシュを広げる** (既定 32)。
  前後に数コマ行き来する作業で、後退のたびに 134ms 待たされるのを減らす。
* `sequential_decode_sec` は既定 (2.0) のまま = 1 コマ前進は seek せず読み進める (9〜11ms)。

#### 5.12.3 Timeline 編集画面

| いま | これから |
| --- | --- |
| ボタン「ぼかし指定... (2)」 | **「ぼかし... (2)」**。数字は「ぼかす」指定の件数 (現状どおり) |
| 右クリック「このクリップをぼかす」「この場面をぼかす」「ぼかしを外す」「ぼかし指定...」 | **「このクリップを全部ぼかす」「このクリップのぼかしを消す」「ぼかし...」** の 3 つ (§10 #9) |
| 停止中は実際のぼかしを反映 / 再生中は目印 | そのまま (`preview_marker` / `preview_blur`) |
| 目印は「ぼかしを外した所」を描く | そのまま (`layers_at` の `keep` を拾う) |
| `_load_blur_cache` が解析結果を読む | `store.load` で**追従結果**を読む (名前を `_load_blur_tracks` へ) |
| `_update_blur_cache_location` の引数が 1 つ多い (§2.8) | `SetBlurCache(cache_path, project_path=project_path)` で直す |

「この場面をぼかす」を落とすのは要望⑪ (クリップ内でのみ) のため。
複数クリップを選んで「このクリップを全部ぼかす」を押したときは、選んだクリップぶんの
全面ぼかしを 1 回のコマンドで足す (現状の `merge_scopes` は要らなくなる = クリップごとに 1 件)。

### 5.13 うまくいかないときの扱い

| 何が無い / 失敗した | どうなるか | 利用者への知らせ |
| --- | --- | --- |
| `blur.enabled` が false | ボタンを出さない。`blur` を import しない | — |
| 検出モデル (`yolox_tiny.onnx`) が無い | 追従は位相相関だけで動く | 画面下に 1 行 (「模様の変化だけで追います」) |
| `onnxruntime` が無い | 同上 | 同上 |
| PIL が無い | マスクを作れない = ぼかしを掛けられない | 画面で「実際のぼかし」を選べなくし、書き出し前に確認を出す |
| PyAV が無い | FFmpeg のパイプでデコードする (現状どおり) | — |
| 素材が見つからない | その指定は追えない (`status=failed`) | 「追いかけられませんでした」 |
| 追従が途中で切れた | §3.3 のとおり安全側 | 「0:04.12 で見失いました」 |
| 「ボカす」が 1 フレームも塗れない | `context.blur_mask_failed = True` | **書き出し前に確認ダイアログ** (現状の経路を維持) |
| マスクの書き出しに失敗 | 同上 | 同上 |

**原則は現状のまま**「例外で落とさない・黙って素で出さない」(§4-5 / §4-6)。

### 5.14 費用の見積もり (実測ではない / §8.5 で測る)

**追従 1 区切りぶん。**

```text
サンプル数 = 区切りの長さ × track.sample_fps
1 サンプルの費用
  デコード (PyAV 前進)                        約 5ms
  窓の切り出し + YOLOX (入力 416 / 窓は枠の 3 倍角)  30〜60ms
  位相相関 (256px 上限 / 検出できた枚は走らない)      約 2ms
合計 ≒ 35〜65ms/サンプル
```

| 場面 | 見積もり |
| --- | --- |
| 10 秒のクリップを端から端まで (100 サンプル) | **3.5〜6.5 秒** |
| キーフレームを 1 つ打ち直した (前後 2 区切り / 計 3 秒 = 30 サンプル) | **1〜2 秒** |
| 検出モデルが無い環境 (相関だけ / 1 サンプル 7ms) | 100 サンプルで **0.7 秒** |
| 囲み 5 件のクリップ (10 秒) を最初から | **18〜33 秒** (件数に比例) |

現状 (resolve7) は「クリップの区間を検出 + ReID + 輪郭で走査」していたので、
10 秒 × 10fps = 100 枚に対し 1 枚 100〜200ms (検出 + ReID + 輪郭 4 枚に 1 枚) ≒ **10〜20 秒**。
囲みが 1〜2 件なら**同等か速く**、囲みが多いほど不利になる。
`track.sample_fps` を下げれば比例して速くなる (§7 の選択肢に 5fps を残す)。

**マスクの生成**は現状と変わらない。

```text
指定が効いていないフレーム … 真っ黒の絵を使い回す (費用ゼロ)
全面ぼかしのフレーム       … 配列へ 255 を代入するだけ (1ms 未満)
囲み 1 件                  … 480x270 の矩形塗り + フェザー ≒ 1〜2ms
```

**画面の応答。**

| 操作 | 見積もり |
| --- | --- |
| 1 コマ進む (前進デコード + 実際のぼかし) | 9〜11ms + 10〜25ms ≒ **20〜36ms** |
| 1 コマ戻る (seek が必要 / キャッシュ外) | 134ms + 10〜25ms ≒ **145〜160ms** |
| 1 コマ戻る (キャッシュ内 / 32 コマ) | **10〜25ms** |
| 枠をドラッグ中 | 枠だけ動かす = 1ms 未満 (フレームは取り直さない) |

---

## 6. 実装手順

**先にデータと判定を固め、画面は最後にする。**途中で一度「通しで使える」所を作る。

| Phase | 内容 | できあがりの確認 |
| --- | --- | --- |
| 1 | `config.py` の設定を v5 へ (旧キーの読み替えつき / §7) | 単体テスト (§8.1 A) |
| 2 | `decisions.py` を書式 v5 へ。v4 以前の読み替え (§3.9 / §5.3) | 単体テスト (§8.1 A / §8.2) |
| 3 | `analyzer.py` を解体 → `frames.py`。`region_tracker.py` を解体 → `correlate.py`。`reid.py` / `silhouette.py` / 旧 `tracker.py` を削除 | import が通る / 既存テストの整理 |
| 4 | `store.py` を書式 v5 へ (区切りの指紋 / §5.6) | 単体テスト (§8.1 E) |
| 5 | `tracker.py` (囲みの追従 / §5.5) | 単体テスト (§8.1 D) |
| 6 | `plan.py` を書き直し (`rect_at` / `layers_at` / §5.4 / §5.7) | 単体テスト (§8.1 B) |
| 7 | `mask_builder.py` の入口と塗る形 (§5.8) | 単体テスト (§8.1 C) + 目視 |
| 8 | `commands.py` の入れ替え (§5.9)。`timeline_editor_dialog` の呼び出しを直す (§2.8 の不具合もここ) | Undo / Redo が効く。**この時点で「クリップを全部ぼかす」が通しで使える** |
| 9 | `blur_canvas.py` (囲む / 掴む / ハンドル / コマ送りのキー / §5.11) | 手動確認 (§8.3 の ②⑧⑩) |
| 10 | `blur_spec_dialog.py` の書き直し (§5.10) | 手動確認 (§8.3 の ①〜⑦⑨) |
| 11 | `settings_window.py` / `setting.json` (§7) | 設定の往復 (§8.1 A) |
| 12 | 同梱モデル・ライセンス表記・`models/README.md` の整理 (§9.4) | `tests/test_licenses.py` が通る |
| 13 | 実素材での確認と実測 (§8.5)、既定値の見直し | — |

**Phase 8 で一度区切れる。**ここまでで「クリップを選んで全部ぼかす」は完成し、
囲みが無くても実用になる。Phase 9 以降が要望②〜⑩ の作業である。

---

## 7. setting.json 定義 (変更分)

```jsonc
"blur": {
  "enabled": true,
  "preview_marker": true,
  "preview_blur": true,

  "model": {
    "detector": "models/yolox_tiny.onnx",
    "detector_format": "yolox",
    "detector_input": 416,
    "detector_pad_value": 114,
    "detector_score": 0.2,        // 0.4 → 0.2  追従の助けなので拾いやすくする (旧 manual.detector_score)
    "detector_nms": 0.5,
    "providers": ["CPUExecutionProvider"]
    // "reid" / "reid_format" / "reid_input" / "reid_dim" 削除 (§0.3)
  },

  // 新設: 囲みの追従。旧 "analysis" + "region" + "manual" を 1 つにまとめる (§3.4)
  "track": {
    "sample_fps": 10.0,           // 追う間隔 (旧 analysis.sample_fps)
    "auto_start": true,           // 囲んだ直後に自動で追う (旧 analysis.auto_start / 意味を限定)
    "search_ratio": 1.0,          // 枠の何倍ぶん広げて探すか (旧 manual.search_ratio)
    "min_iou": 0.3,               // 検出枠を同じ相手とみなす重なり (旧 manual.min_iou)
    "match_psr": 8.0,             // 位相相関の合格ライン (旧 manual.match_psr)
    "hold_sec": 1.0               // 分からない状態を我慢する秒数 (旧 region.hold_sec)
    // 旧 analysis.min_track_sec / iou_threshold / embed_threshold 削除 (人物トラッカーの設定)
    // 旧 region.follow / search_scale / match_psr 削除 (全画面追従の廃止)
    // 旧 manual.detector_score → model.detector_score / manual.fixed_span_sec 削除
  },

  "render": {
    "mode": "gaussian",
    "strength": 50,
    "shape": "rect",              // "silhouette" → "rect"  囲んだとおりに塗る (§3.6 / §5.8)
    "margin_ratio": 0.0,          // 0.06 → 0.0  囲みの大きさは利用者が決める
    "feather_ratio": 0.006,       // 縁のなじませ (ぼかす / ボカさない 共通の 1 つへ統合)
    "mask_scale": 0.25
    // "pad_sec" 削除            (キーフレームが区間を覆うため不要)
    // "keep_margin_ratio" / "keep_margin_box_ratio" / "keep_max_margin_box_ratio"
    // "keep_feather_ratio" / "keep_motion_margin" / "keep_hold_sec" 削除 (§0.3)
  },

  // 旧 "spec" を "editor" へ改名 (データ側の「指定 (specs)」と紛れないため)
  "editor": {
    "preview_mode": "blur",       // "blur" (実際のぼかし) / "keep" (緑) / "none" (枠だけ)
    "show_overlays": true,
    "step_frames": 10,            // 新規: Shift + ← → で送るコマ数 (§3.7)
    "frame_cache": 32,            // 新規: 指定画面のフレームキャッシュ枚数 (§5.12.2)
    "handle_px": 10,              // 新規: 大きさを変えるハンドルの当たり判定 (画面 px / §5.11.2)
    "min_size_ratio": 0.01,       // 新規: 囲みの最小の大きさ (キャンバス幅比 / §5.10.3)
    "name_max_len": 32            // 指定の名前の最大文字数 (相手が枠から指定へ移った)
    // "hit_ratio" 削除   (囲みの当て込みが無くなった)
    // "thumb_px" 削除    (見本画像が無くなった)
    // "default_scope" 削除 (適用範囲はクリップ固定 / §3.8)
  }

  // "analysis" / "region" / "manual" / "silhouette" セクションごと削除
}
```

**旧キーの読み替え (`config.py` の中で行う / 既存設定との互換性)。**

| 旧キー | 新キー | 備考 |
| --- | --- | --- |
| `analysis.sample_fps` | `track.sample_fps` | 新キーが無ければ旧キーを読む |
| `analysis.auto_start` | `track.auto_start` | 同上 |
| `region.hold_sec` | `track.hold_sec` | 同上 |
| `manual.search_ratio` / `min_iou` / `match_psr` | `track.*` | 同上 |
| `manual.detector_score` | `model.detector_score` | 同上 |
| `spec.preview_mode` / `show_overlays` / `name_max_len` | `editor.*` | 同上 |
| `render.shape = "silhouette"` | `render.shape = "rounded"` | 輪郭が無くなるため、一番近い塗り方へ落とす |
| 上記以外の削除キー | — | 読まない (残っていても無害) |

**範囲の丸め (`config.py`)。**

| キー | 範囲 | 範囲外のとき |
| --- | --- | --- |
| `model.detector_score` | 0.01〜0.99 | 丸める |
| `track.sample_fps` | 0.5〜30.0 | 丸める |
| `track.search_ratio` | 0.1〜5.0 | 丸める |
| `track.min_iou` | 0.01〜0.99 | 丸める |
| `track.match_psr` | 1.0〜1000.0 | 丸める |
| `track.hold_sec` | 0.0〜10.0 | 丸める |
| `render.shape` | `rect` / `rounded` / `ellipse` | 既定 `rect` (`silhouette` は `rounded` へ) |
| `render.margin_ratio` | 0.0〜0.5 | 丸める |
| `render.feather_ratio` | 0.0〜0.1 | 丸める |
| `render.mask_scale` | 0.05〜1.0 | 丸める |
| `editor.preview_mode` | `blur` / `keep` / `none` | 既定 `blur` (`mask` は `keep` へ) |
| `editor.step_frames` | 1〜120 | 丸める |
| `editor.frame_cache` | 1〜256 | 丸める |
| `editor.handle_px` | 4〜40 | 丸める |
| `editor.min_size_ratio` | 0.001〜0.5 | 丸める |
| `editor.name_max_len` | 1〜200 | 丸める |

**設定画面 (`settings_window.py`) の「ぼかし」タブ。**

| 項目 | 変更 |
| --- | --- |
| トラッキングぼかしを使用する | そのまま |
| 種類 (ぼかし / モザイク) | そのまま |
| 強さ | そのまま |
| 塗る形 | `BLUR_SHAPE_OPTIONS` から「身体の輪郭に沿う」を外し、四角 (既定) / 角丸 / 楕円の 3 つ |
| 外すときの余裕 (`BLUR_KEEP_MARGIN_OPTIONS`) | **削除** |
| 外した縁をなじませる | 「縁をなじませる」へ改名 (`render.feather_ratio` を 0 / 0.006 で切り替え) |
| 新しい指定の既定の適用範囲 (`BLUR_SCOPE_OPTIONS`) | **削除** (§3.8) |
| 追従の丁寧さ (`BLUR_SAMPLE_FPS_OPTIONS`) | そのまま (`track.sample_fps` へ書く) |
| コマ送りの飛び幅 | **新規** (`editor.step_frames` / 5 / 10 / 30 コマ) |
| 状態表示 (`_update_blur_status`) | 検出モデルの有無だけを見る (輪郭モデルの判定を外す) |
| ライセンス表記を開く | そのまま |

---

## 8. テスト計画

### 8.1 単体テスト (unittest / `python -m unittest discover -s tests`)

**A. 設定と書式 (`tests/test_blur_decisions.py` 新規 / 旧 `test_blur_names.py` を置き換え)**

| # | 内容 | 期待 |
| --- | --- | --- |
| A1 | v5 の `specs` を保存 → 読み戻し | 並び順・キーフレームまで一致する |
| A2 | `next_spec_id` は削除しても番号を戻さない | `b1` を消して足すと `b2` |
| A3 | `span_for(timeline, clip)` | クリップの `source_in`〜`source_out` と `clip_id` / `label` |
| A4 | `span_contains` の境界 | `start` / `end` ちょうどは含む |
| A5 | `with_key` で同じコマへ 2 回打つ | キーは 1 件 (置き換わる) |
| A6 | `with_key` の時刻は昇順に並ぶ | 間に打っても順番が保たれる |
| A7 | `without_key` で最後の 1 件を消す | 変化しない (消えない) |
| A8 | `with_spec_moved` で重ね順が動く | 端では動かない |
| A9 | `needs_mask` / `needs_tracking` | 全面ぼかしだけなら追従は不要 |
| A10 | `config.config` が旧キーを読む | `analysis.sample_fps` だけの設定で `track.sample_fps` に入る |
| A11 | `render.shape = "silhouette"` の設定 | `"rounded"` へ落ちる |
| A12 | 壊れた値 (文字列・NaN・範囲外) | すべて既定へ落ちる |

**B. 位置の計算 (`tests/test_blur_plan.py` 書き直し)**

| # | 内容 | 期待 |
| --- | --- | --- |
| B1 | キーフレームの時刻 (追従あり) | **そのキーの矩形になる** (§5.4 性質 1) |
| B2 | キーフレームの時刻 (追従なし) | 同上 |
| B3 | キー 2 件 + 追従なし | 間が線形補間になる |
| B4 | キー 2 件 + 追従が 3px ずれている | 誤差が区間へ按分され、次のキーで飛ばない (性質 2) |
| B5 | 大きさだけ違うキー 2 件 | 大きさが線形に変わり、位置は追従に従う |
| B6 | `span` の外 | `None` (性質 3) |
| B7 | 見失った先 / `mode=blur` | 最後に追えた位置を保持 |
| B8 | 見失った先 / `mode=keep` | `None` (穴が閉じる) |
| B9 | 見失った後に次のキーがある | 最後の位置から次のキーへ線形で結ぶ |
| B10 | `follow=fixed` | 追従結果があっても使わず、キーの補間だけ |
| B11 | `layers_at` の並び | `specs` の並び順のまま返る |
| B12 | レターボックスのある素材 (縦動画) | 正規化座標 → キャンバス px の変換が合う |

**C. マスク (`tests/test_blur_render.py` 書き直し)**

| # | 内容 | 期待 |
| --- | --- | --- |
| C1 | 指定なし | 真っ黒 (1 バイトも塗らない) |
| C2 | 全面ぼかしだけ | 真っ白 |
| C3 | 全面 + `keep` の囲み | 囲みの所だけ黒い穴 |
| C4 | 穴の中にさらに `blur` | 穴の中が白く塗り直される (R6-3) |
| C5 | 並び順を入れ替える | 後のものが勝つ |
| C6 | `span` の外の時刻 | 真っ黒 |
| C7 | `render.shape` = rect / rounded / ellipse | 角の塗られ方が変わる |
| C8 | `outline` を持つ指定 (旧データ) | 多角形で塗られる |
| C9 | `feather_ratio` | 形の外側でなだらかに落ちる |
| C10 | 「ボカす」の囲みで追従が 1 点しか無い | マスクは作られる + `blur_mask_failed` は立たない (キー位置には塗れるため) |

**D. 追従 (`tests/test_blur_track.py` / 旧 `test_blur_manual.py` を置き換え)**

| # | 内容 | 期待 |
| --- | --- | --- |
| D1 | 合成画像でパッチを一定速度で動かす | サンプルがパッチを追う (既存 `follows_moving_patch_by_correlation` を流用) |
| D2 | 途中でパッチを消す | `status = lost` / `lost_sec` がその時刻 |
| D3 | 検出モデルが無い環境 | 相関だけで追える (`detect` が空を返しても落ちない) |
| D4 | 追従は**大きさを変えない** | サンプルは中心だけ。矩形の大きさはキーのまま |
| D5 | `segments_for` | キー 3 件 + `span` で 4 区切り (後ろ向き 1 + 前向き 3) |
| D6 | 短すぎる区切り | 作られない (`MIN_SEGMENT_SEC` 未満) |
| D7 | キーの中心を動かす | その区切りの `hash` が変わる |
| D8 | キーの**大きさだけ**変える | `hash` は変わらない (§5.10.5) |
| D9 | `ensure_tracks` | 指紋の合う区切りは追い直さない |
| D10 | 途中でキャンセル | 追えた所までが残る |

**E. キャッシュ (`tests/test_blur_cache.py` 手直し)**

| # | 内容 | 期待 |
| --- | --- | --- |
| E1 | schema 4 のキャッシュを読む | `None` (作り直す) |
| E2 | `settings_key` が `sample_fps` / 検出モデル / キャンバス寸法で変わる | 変わったら読まない |
| E3 | `missing_segments` | 指紋の無い区切りだけ返る |
| E4 | `put_segment` → `segment_of` | 往復する |
| E5 | `drop_unused` | 消した指定の追従結果が消える |
| E6 | `drop_stale_media` | 由来の変わった素材の分だけ消える (既存テストを流用) |
| E7 | `media_key` 系 (中間ファイル・アーカイブ) | **据え置き** (既存テストをそのまま通す) |
| E8 | `related_paths` / 改名 / 削除でキャッシュが付いて回る | 既存テストをそのまま通す |

**F. コマンド (`tests/test_timeline_commands.py` へ追加)**

| # | 内容 | 期待 |
| --- | --- | --- |
| F1 | `AddBlurSpecs` (全面 + 囲み) → Undo | **1 回で両方消える** |
| F2 | `SetBlurKey` → Undo | キーが元の位置へ戻る |
| F3 | `SetBlurKey` を同じ時刻へ 2 回 → Undo 2 回 | 1 件ずつ戻る |
| F4 | `RemoveBlurSpecs` → Undo | 並び順も戻る |
| F5 | `MoveBlurSpec` → Undo | 並び順が戻る |
| F6 | `SetBlurCache` | `cache` (相対) と `cache_abs` の両方が入る |
| F7 | 指定が無い状態で `RemoveBlurSpecs` | `False` を返す (履歴を汚さない) |

**G. プレビュー (`tests/test_blur_preview.py` 手直し)**

| # | 内容 | 期待 |
| --- | --- | --- |
| G1 | 指定なしのフレーム | 入力をそのまま返す (同一オブジェクト) |
| G2 | 全面ぼかしのフレーム | 全体が変わる |
| G3 | 囲み 1 件 | その矩形だけ変わる |
| G4 | レターボックスのある素材 | ぼける場所が合う (既存テストを流用) |

**H. 削除するテスト**

```text
tests/test_blur_core.py  … reid / 人物トラッカー / 全体クラスタリング / 囲みの当て込みの分を削除
                            (detector / config / geometry の分は残す)
tests/test_blur_names.py … 廃止 (A へ統合。指定の名前として 2 件だけ残す)
tests/test_blur_contour.py … 縮小 (旧データの引き継ぎで使う encode/decode/to_absolute だけ残す)
```

### 8.2 移行テスト (`tests/test_blur_decisions.py` の後半)

| # | 入力 | 期待 |
| --- | --- | --- |
| M1 | v4: `marks` kind=frame (scope=clip) | 全面ぼかし 1 件。区間が一致 |
| M2 | v4: `marks` kind=frame (scope=media) | 区間はそのまま (勝手に狭めない) |
| M3 | v4: `marks` kind=region + `regions[]` | 囲み 1 件。キー 1 点 (`anchor_sec`) / `mode` / `label` / `outline` / `follow` を引き継ぐ |
| M4 | v4: `marks` kind=track | 捨てる。`migrated_dropped` に件数が入る |
| M5 | v4: `excluded` / `names` | 捨てる |
| M6 | v3 以前 (`identities` / `default_policy`) | 落ちずに空の指定として読める |
| M7 | 壊れた項目 (mode 不正 / span 無し / keys 空) | その項目だけ捨てる |
| M8 | v5 を読む | 読み替えが走らない (`migrated` が false) |

### 8.3 手動確認 (要望の受け入れ手順)

**準備**: 人物が歩いている 30 秒程度の素材を 1 本、無音カット後の Timeline に 3 クリップ。

| # | 手順 | 期待 (対応する要望) |
| --- | --- | --- |
| 1 | クリップ 2 を選び、右クリック → 「ぼかし...」 | クリップ 2 の先頭が出る。スライダの範囲がクリップ 2 の長さ (①⑪) |
| 2 | 人物をドラッグで囲む | ダイアログ「指定した場所をどうしますか？」(②④) |
| 3 | 「ボカさない」を押す | 画面全体がぼけ、囲んだ所だけ素で見える。指定が 2 件並ぶ (⑥) |
| 4 | 追従の進捗が出て終わる | 「囲んだ場所を追いかけています…」→ 枠が人物に付いて動く (③) |
| 5 | → を 10 回押す | 1 コマずつ進む。コマ番号が 1 ずつ増える (⑦) |
| 6 | 枠がずれているコマで枠を掴んで動かす | 枠が動き、キーフレームが 1 件増える。前後だけ追い直される (⑧⑨) |
| 7 | 角のハンドルで枠を広げる | 大きさが変わる。**追い直しは走らない** (⑩) |
| 8 | 別の人物を囲む | ダイアログは出ず、「これから: ボカさない」で外れる (④ の「一番最初」/ ⑥) |
| 9 | 「これから: ボカす」にして書類を囲む | 書類がぼける (⑤⑥) |
| 10 | Ctrl+Z を数回 | 操作単位で戻る (R15) |
| 11 | 閉じて Timeline のプレビューを見る | 停止中は実際にぼけた絵。再生中は外した所に目印 |
| 12 | クリップ 1 を選んで「ぼかし...」 | **クリップ 2 の指定は出ない**。1 件目のダイアログから始まる (⑪) |
| 13 | 保存 → 開き直す | 指定とキーフレームが残る。追従結果もキャッシュから読める (§2.8 の不具合が直っている) |
| 14 | 書き出す | 画面と同じ所がぼけている |
| 15 | クリップを分割して書き出す | 分割後も同じ所がぼける (`span` で覚えているため / §3.8) |

### 8.4 回帰確認 (壊していないこと)

| 経路 | 確認 |
| --- | --- |
| ぼかし OFF (`enabled = false`) | FFmpeg のコマンドにぼかしの行が 1 つも無い (R14 / 既存テスト `test_blur_renderer_integration`) |
| 指定なし | 同上 (マスクを作らない) |
| 合成が走る経路 / 走らない経路 / 出力直前の保険 | 3 つとも 1 回だけ適用される (既存テストを維持) |
| アーカイブ切り抜き (サブ Timeline) | `source["blur"]` が引き継がれ、`cache_abs` で追従結果が見つかる |
| プロジェクトの保存・復元・改名・削除 | `.blur.json` が付いて回る (既存テスト) |
| 自動保存 / Undo 履歴 | 指定の変更で「未保存」の印が付く |

### 8.5 実測して直す項目 (§5.14 の見積もりの検証)

| # | 測るもの | 見積もり | 外れたときの手当て |
| --- | --- | --- | --- |
| 1 | 10 秒 / 囲み 1 件の追従 | 3.5〜6.5 秒 | 5 秒を超えるなら `track.sample_fps` の既定を 5.0 へ |
| 2 | キーフレーム 1 つ打ち直し | 1〜2 秒 | 3 秒を超えるなら区切りの端を「キーから ±N 秒」に限る案を検討 |
| 3 | 1 コマ後退 | 145〜160ms | 300ms を超えるなら `editor.frame_cache` を増やす / 先読みを入れる |
| 4 | マスク生成 (3 分の出力 / 囲み 3 件) | 現状と同等 | 遅ければ `render.mask_scale` を下げる |
| 5 | 追従の当たり具合 (歩く人物 / 10 秒) | キーフレーム 2〜3 点で追い切れる | 外れるなら `track.search_ratio` / `match_psr` を調整 |

---

## 9. 影響範囲・互換性

### 9.1 触るファイル

| ファイル | 変更 |
| --- | --- |
| `src/blur/config.py` | 設定を v5 へ + 旧キーの読み替え |
| `src/blur/decisions.py` | **書き直し** (書式 v5) |
| `src/blur/plan.py` | **書き直し** |
| `src/blur/store.py` | **書き直し** (書式 v5) |
| `src/blur/tracker.py` | **書き直し** (旧 `manual_tracker` を土台に) |
| `src/blur/correlate.py` | **新規** (旧 `region_tracker` から切り出し) |
| `src/blur/frames.py` | **新規** (旧 `analyzer` から切り出し) |
| `src/blur/mask_builder.py` | 入口と塗る形 |
| `src/blur/preview.py` | `set_analysis` → `set_tracks` |
| `src/blur/geometry.py` | 正規化矩形の道具を追加 (`normalized_rect_to_canvas` など) |
| `src/blur/contour.py` | 縮小 (旧データの引き継ぎ用) |
| `src/blur/models.py` | 検出モデルだけ見る |
| `src/blur/analyzer.py` / `region_tracker.py` / `manual_tracker.py` / `reid.py` / `silhouette.py` | **削除** |
| `src/gui/timeline/blur_spec_dialog.py` | **書き直し** |
| `src/gui/timeline/blur_canvas.py` | **書き直し** |
| `src/gui/timeline/timeline_editor_dialog.py` | 入口・目印・右クリック・保存時の呼び出し (§2.8) |
| `src/gui/timeline/preview_panel.py` | `set_blur_preview` の相手が `tracks` になるだけ (呼び出しは不変) |
| `src/timeline/commands.py` | ぼかしのコマンドを入れ替え |
| `src/settings/settings_window.py` | 既定値・タブ・選択肢 |
| `src/settings/setting.json` | §7 |
| `src/models/README.md` | 落としたモデルの記述を外す |
| `tools/license_manifest.json` | `torchreid` / `MobileSAM` の項目を外す |
| `tests/test_blur_*.py` | §8.1 のとおり入れ替え |
| `tests/test_licenses.py` | `_REQUIRED` から `torchreid` を外す |
| `docs/HowToRelease.md` | 同梱モデルの説明 (§9.4) |

**触らないもの**: `src/modules/blur_overlay.py` (FFmpeg のフィルタ)、
`src/timeline/renderer.py` のぼかしの差し込み位置、`src/pipeline/*`、`src/timeline/project_io.py`。
**出力の仕組みは変えない**ので、既存の書き出し経路のテストはそのまま通る。

### 9.2 減るもの

実装後の**実測値**。

| 種類 | いま | これから | 差 |
| --- | --- | --- | --- |
| `src/blur` の行数 | 5,204 | **3,180** | **−2,024** |
| ぼかしの GUI の行数 (指定画面 + キャンバス) | 1,759 | **1,622** | −137 |
| ぼかしのコマンド | 13 個 | **9 個** | −4 |
| `blur` の設定キー (末端) | 61 | **29** | **−32** |
| 同梱モデル | 4 本 / 62.6MB | **1 本 / 19.3MB** | **−43.3MB** |
| ぼかしのモジュール | 17 ファイル | **14 ファイル** | −3 |

内訳 (`src/blur`)。

| ファイル | 行 | ファイル | 行 |
| --- | --- | --- | --- |
| `decisions.py` | 576 (←778) | `mask_builder.py` | 375 (←410) |
| `store.py` | 383 (←466) | `plan.py` | 342 (←499) |
| `tracker.py` | 302 (←284 + 452 の一部) | `config.py` | 273 (←352) |
| `detector.py` | 200 (据え置き) | `preview.py` | 197 (←210) |
| `frames.py` | 137 (新規 / ←522 の一部) | `geometry.py` | 131 (←169) |
| `models.py` | 113 (←115) | `correlate.py` | 76 (新規 / ←452 の一部) |
| `contour.py` | 56 (←163) | `__init__.py` | 19 |
| 削除: `analyzer.py` (522) / `region_tracker.py` (452) / `manual_tracker.py` (284) / 旧 `tracker.py` (211) / `silhouette.py` (252) / `reid.py` (104) | −1,825 | | |

### 9.3 互換性

| 対象 | どうなるか |
| --- | --- |
| v5 のプロジェクト | そのまま読める |
| v4 のプロジェクト | §3.9 のとおり読み替え。**人物の枠を選んだ指定は失われる** (1 度だけ知らせる) |
| v3 以前 | 空の指定として読める (落ちない / R13) |
| 旧 `.blur.json` (schema ≤ 4) | 読まずに作り直す。**追従はもう一度走る** |
| 旧 `setting.json` | 削除したキーは無視。移した値は読み替える (§7) |
| ぼかしを使っていないプロジェクト | 影響なし (`source["blur"]` が無いだけ) |
| 出力のフィルタチェーン | **変わらない** (`blur_overlay` はそのまま) |

**「人物の枠を選んだ指定が失われる」ことの重み。**
v1.5.0 (2026-09-23 リリース) 以降に作られたプロジェクトだけが該当する。
該当プロジェクトでは、開いたときに知らせを出し、**囲み直しを案内する**。
これを避けたい場合は §10 #1 で「検出枠の指定も残す」を選ぶことになるが、
その場合は §0.3 の大半が残るため、単純化の効果は半分以下になる。

### 9.4 リリース時の注意

| # | 作業 |
| --- | --- |
| 1 | `src/models/` から `osnet_x0_25.onnx` / `silhouette_*.onnx` を外す (`main_window.spec` は `glob('models/*.onnx')` なので、ファイルを置かなければ同梱されない) |
| 2 | `tools/license_manifest.json` から `torchreid` / `MobileSAM` の項目を消す。**`verify_on_bundle: true` が付いているため、ファイルが無いまま残すと検証で落ちる** |
| 3 | `licenses/torchreid` / `licenses/MobileSAM` を外す (`tools/collect_licenses.py` を流し直す) |
| 4 | `tests/test_licenses.py` の `_REQUIRED` から `torchreid` を外す |
| 5 | `tools/export_osnet.py` / `tools/export_silhouette.py` を削除 (§10 #2 で輪郭を残すなら `export_silhouette.py` は保持) |
| 6 | `src/models/README.md` の表を 1 行 (`yolox_tiny.onnx`) にする |
| 7 | `docs/HowToRelease.md` のモデル準備手順を更新 |

---

## 10. 確認事項

決めないと作るものが変わるものを上に置いた。

> **決定 (2026-09-25): #1〜#13 すべて「推奨」で進める** と指示を受けたため、
> 推奨欄のとおり実装した (§0.4)。以下は判断の記録として残す。

| # | 論点 | 選択肢 | 推奨 |
| --- | --- | --- | --- |
| **1** | **人物の自動検出による指定 (枠の一覧・見本画像・枠を選んで外す・隠す) を全部やめてよいか** | (a) やめる / (b) 残す (いまの画面に囲みの機能を足す) | **(a)** 要望に出てこない層で、精度の弱点も抱えている (§3.1)。(b) を選ぶと §0.3 の大半が残り、単純化の効果は半分以下になる |
| **2** | **身体の輪郭 (`silhouette` / MobileSAM 42MB) を落としてよいか** | (a) 落とす / (b) 残す (囲んだ矩形をモデルへ渡し、輪郭に沿わせる) | **(a)** まず落とす。囲みを枠として渡せばモデルはそのまま使えるので、必要になったら「囲みを輪郭に沿わせる」として戻せる (§3.6)。ただし**⑥ の「人物だけ素で見せる」は矩形の穴になる**ため、見た目に不満が出る可能性がある |
| **3** | 追従の助けとして検出モデル (YOLOX 19MB) を残すか | (a) 残す / (b) これも落とし、位相相関だけで追う | **(a)** 人物の追従精度が明らかに上がる。無ければ相関だけで動くので、(b) にしても機能は成立する (追従が切れやすくなる = キーフレームを打つ回数が増える) |
| **4** | 2 件目以降も「ボカす / ボカさない」のダイアログを出すか | (a) 出さない (画面のラジオ) / (b) 毎回出す | **(a)** 要望④は「一番最初に囲った場所に対して」。10 件囲んで 10 回聞かれるのは煩わしい (§3.2) |
| **5** | 囲みは矩形だけにするか | (a) 矩形だけ / (b) 自由曲線も残す | **(a)** ⑧⑩ (移動・拡大縮小) が素直に定義できる。塗る形は四角 / 角丸 / 楕円から選べる (§3.6) |
| 6 | 追従が切れた先の扱い | (a) ボカす = 保持 / ボカさない = 打ち切り / (b) どちらも打ち切り / (c) どちらも保持 | **(a)** どちらも「迷ったらぼける方」へ倒す (§3.3 / §4-8) |
| 7 | 「画面全体をぼかす」を一覧から消せるようにするか。最後の「ボカさない」を消したとき全面ぼかしも自動で外すか | (a) 消せる / 自動では外さない / (b) 自動で外す | **(a)** 「全面だけぼかす」も正当な使い方なので、勝手に外さない (§5.10.6) |
| 8 | 適用範囲をクリップ固定にしてよいか (この場面 / この素材全体を廃止) | (a) クリップ固定 / (b) 「この場面」を残す | **(a)** 要望⑪のとおり。同じ人が複数クリップに映る場合はクリップごとに囲む手間が増えるため、不足なら後から「隣のクリップへ複製」を足す |
| 9 | Timeline のクリップ右クリックに「このクリップを全部ぼかす」を残すか | (a) 残す / (b) 指定画面だけにする | **(a)** 「全部ぼかすだけ」が 1 操作で済む。指定画面を開かずに終われる (§5.12.3) |
| 10 | `track.sample_fps` の既定 | (a) 10.0 (現状) / (b) 5.0 | **(a)** まず 10 のままで §8.5 #1 を測り、5 秒を超えるなら 5.0 へ下げる |
| 11 | 追従は「位置だけ」を追い、大きさはキーフレームで決める方式でよいか | (a) その方式 / (b) 大きさも追う | **(a)** 位相相関は拡大縮小に追従しない。検出枠の大きさへ合わせると⑩ で決めた大きさが勝手に変わる (§3.3) |
| 12 | 旧プロジェクトで失われる指定 (人物の枠) の知らせ方 | (a) 開いたときに 1 度だけダイアログ / (b) ログだけ | **(a)** 黙って消すと「ぼかしたはずが素で出る」に直結する (§4-6) |
| 13 | `blur.enabled` の既定 | (a) `setting.json` の現行値 (true) のまま / (b) false へ戻す | **(a)** 既に true で配布済みのため変えない |

---

## 付録 A. 用語

| 用語 | 意味 |
| --- | --- |
| 指定 (spec) | 「この区間の・ここを・ぼかす / ぼかさない」1 件。全面ぼかしと囲みの 2 種類 |
| 囲み (area) | マウスで囲んだ矩形の指定。キーフレームを持つ |
| 全面ぼかし (frame) | 画面全体を相手にした指定。「ボカさない」を選ぶと自動で足される |
| キーフレーム (key) | 利用者が「この時刻はこの位置・この大きさ」と決めた点。追従より強い |
| 区切り (segment) | キーフレームと `span` の端で区切った追従の単位。指紋 (`hash`) を持つ |
| 追従 (track) | 区切りの中を、検出と位相相関で 1 サンプルずつ追う処理 |
| 区間 (span) | 指定が効く素材の時間範囲。クリップの `source_in`〜`source_out` を写したもの |
| マスク | Timeline 時間のグレースケール動画。白い所がぼける |
| 正規化キャンバス座標 | キャンバス (出力の画面) に対する 0.0〜1.0 の座標。保存はすべてこれ |
