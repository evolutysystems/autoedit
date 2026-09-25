# resolve7（ver5） — ぼかしの作り直し (クリップ単位のぼかし / 初手の自動処理の廃止 / 外す指定 / 人物統一の廃止) 修正設計書

## 0. 本書の位置づけ

* 対象: `docs/request/ver5/request7.md` と、その後の追加指示
  「**そもそも初手のボカシ処理がいらないかもしれない。選択したクリップに対してボカシが必要なら行う判定にすればいい**」。
  本書は現状分析と修正設計を兼ねる。
* 調査は **2026-09-23 時点の実コード (v1.5.0 + ver5 resolve3 / resolve4 / resolve6 実装後)** を読んで行った。
  本文中の行番号・既定値はその時点のもの。
* 本書は設計として書き、レビュー後に実装した (`docs/claude.md`「実装前に設計を行うこと」)。
  **実装の結果は §0.5「実装状況」にまとめてある。**
* **数値の扱い**: 本書の処理時間・マスクの大きさは *計算による見積もり*であり、**実測ではない**。
  見積もりの式はその場に書いた。実測は §8.4 で取り、外れていれば §7 の既定値を見直す。
* 要望から一意に決められない論点は §10「確認事項」へ挙げた。
  特に **#1 (ぼかすクリップを誰が決めるか)** と **#2 (適用範囲の既定)** は、決めないと操作の手数が変わる。

### 0.1 要望への回答 (要約)

| # | 要望 | 現状の何が問題か | どう直すか |
| --- | --- | --- | --- |
| ⓪ | **初手のぼかし処理はいらない。選択したクリップに、必要なら掛ける** | 画面を開いた瞬間に**動画全体の解析**が走り (`analysis.auto_start` 既定 true)、その結果から「主役以外をぼかす」が自動で決まる。使わないクリップまで解析し、要らないぼかしが勝手に付く (§2.1 / §2.7) | **ぼかしは既定 OFF。**「このクリップをぼかす」と立てたクリップだけが対象になる。立てるまで**解析もマスク生成も 1 行も走らない** (§3.1) |
| ① | ぼかしをかけるのではなく、**全体にかけて外す部分を選ぶ** | ぼかすのは「検出できた人物の枠」だけ。検出漏れ・短い映り込み・人物以外は**素で出る** (§2.2) | 立てたクリップは**画面全体がぼける**。そこから「外す」指定で穴を開ける (§3.1 / §3.2) |
| ② | 外すときは**輪郭から少し余裕をもって**外す | 「ぼかさない」層は resolve3 §3.4 の決めごとで**膨張もフェザーもしない**。輪郭が数 px ずれるだけでぼかしが顔へかぶる (§2.3) | 外す形を膨張させ、その外側でフェザーする。大きさ・動きに比例させ、枠が途切れた前後も保持する (§3.4) |
| ③ | **人物の統一処理を無くす** (精度が低い) | 全体クラスタリング (`tracker.cluster`) が全身 ReID の距離で枠を人物へまとめている。別人が 1 人になる / 同じ人が割れる (§2.4) | `cluster` / `identities` / `merges` / `splits` / 主役を廃止。指定の対象は「検出された枠」と「手で囲んだ枠」だけにする (§3.6) |
| ④ | 外す・付けるは**選択したクリップに対して**行う | 人物への指定は人物 ID 単位、`regions` は素材の全区間。どちらも動画全体に効く (§2.5) | 指定に「効く区間 (scope)」を持たせる。既定は選択中クリップの素材区間 (§3.5) |

### 0.2 一言でいうと

**何もしなければ、ぼかしは 1 フレームも掛からない。**

クリップを選んで「ぼかす」と言ったときだけ、**そのクリップの画面全体**がぼける。
そこから見せたい人・物を選んで**少し大きめに穴を開ける**。
穴を開ける相手は「枠」そのもので、人物へまとめる処理 (統一) は使わない。
解析は**ぼかすと決めたクリップの、外したいときだけ**走る。

### 0.3 本書の初版から変わったところ

初版では「Timeline 全体を既定で全面ぼかしにする」と設計していた。
追加指示を受けて、**全面ぼかしを「クリップに立てるスイッチ」へ降ろした**。

| | 初版 | 本版 |
| --- | --- | --- |
| 既定 | 動画全体がぼける | **何もぼけない** |
| ぼかす単位 | Timeline 全体 (`base` 設定) | **クリップ (指定として立てる)** |
| 解析 | 開いた瞬間に動画全体 | **ぼかすクリップで、外したいときだけ** |
| 指定の種類 | 基底 + 外す + 塗る | **すべて「指定 (mark)」1 種類に統一** |
| `blur.render.base` 設定 | 新設 | **不要になったので入れない** |

クリップに立てたスイッチも「画面全体を対象にした指定」として表すことで、
**基底という概念そのものが要らなくなった**。仕組みが 1 つ減る。

### 0.4 実装状況 (2026-09-23)

**§6 の Phase 1〜12 をすべて実装した。**確認事項は §10 のとおり「すべて推奨どおり」で決定した。
全テスト (862 件) が通る。ぼかしの旧テスト 87 件を新しい方式へ書き直し、
人物の統合・分割・割り当てのテスト (`tests/test_blur_assign.py`) は機能ごと削除した。

実装で本書の案から変えたことは次の 1 点。

| 項目 | 本書の案 | 実装 | 理由 |
| --- | --- | --- | --- |
| 「枠をぼかす」指定の相手が見つからないとき | §10 #8 では「解析できなければ外す指定が効かないだけ = 安全」とした | **確認を出す** (`blur_mask_failed`) | 「**枠を**ぼかす」と指定した枠が解析結果に無い場合は、外す指定と違って**素で出てしまう**。resolve2 §5.9 の「黙って素で出さない」を守るため、この 1 ケースだけ従来どおり確認を出す (`mask_builder._unmatched_blur_marks`) |

### 0.5 この作り直しで落とすもの (正直に)

resolve2〜resolve4 で積んだ次は、統一の廃止と道具立ての変更で**意味を失うため落とす**。
残したいものがあれば §10 #4 で戻せる。

| 落とすもの | 由来 | なぜ落ちるか |
| --- | --- | --- |
| 人物一覧 (見本画像つき) | resolve2 §5.6 | 人物という層が無くなる。代わりに「このクリップの枠」一覧を出す |
| 主役 = 一番映っている人物は常にぼかさない | resolve2 R3 | 人物ごとの合計秒数で決まるため、人物が無いと定義できない |
| 「選んだ 2 人を同じ人物にする」(merges) | resolve2 §5.6.6 | 統一そのもの |
| 「新しい人物にする」/「元の人物へ戻す」(splits) | resolve3 §3.5 | 統一の誤りを手で直す機能 |
| 「人物へ割り当て」(assign) | resolve4 §3.3 | 同上 |
| 既定方針 `blur_others` / `manual_only` | resolve2 §9-1 | 既定でぼかすことをやめたため、方針という設定自体が要らない |
| 画面を開いた直後の自動解析 | resolve2 §3.6 案 2 | 初手の処理の廃止 (⓪) |
| 人物の名前 | resolve4 R1 | **落とさない。**名前を付ける相手を「人物」から「枠」へ移して残す (§5.6.4) |

---

## 1. 要望と要件 ID

| ID | 要件 | 出どころ |
| --- | --- | --- |
| **R0** | 既定ではぼかしを一切掛けない。解析もマスク生成も走らせない | 追加指示 ⓪ |
| R0-2 | 「このクリップをぼかす」をクリップ単位で立てられる。複数選択・場面単位でも立てられる | 追加指示 ⓪ |
| R0-3 | 解析は「ぼかすと立てたクリップで、枠が必要になったとき」にその区間だけ走る | 追加指示 ⓪ |
| **R1** | ぼかすと立てたクリップは**画面全体**がぼける。そこから外す所を選ぶ | request7 ① |
| R1-2 | 外した範囲の中を、さらに「ぼかす」で塗り直せる (重ね順で決まる) | request7 ① の含意 |
| **R2** | 外す形は輪郭より外側へ余裕を取る。余裕の量は設定で変えられる | request7 ② |
| R2-2 | 枠が一瞬途切れても、外した状態が点滅しない | request7 ② の含意 (§2.3 (c)) |
| **R3** | 人物の統一処理 (全体クラスタリング・統合・分割・割り当て・主役) を廃止する | request7 ③ |
| R3-2 | 廃止しても、指定の対象が分かる見せ方を用意する (見本画像・名前) | request7 ③ の含意 |
| **R4** | 「外す / 付ける」は選択したクリップに対して効く | request7 ④ |
| R4-2 | 必要なら、場面 (セクション) 単位・素材全体へ広げられる | request7 ④ の含意 (§3.5) |
| R5 | 旧プロジェクト (`source.blur` version ≤ 3) を開いても落ちない | 既存の互換要件 |
| R6 | 機能 OFF・指定なしのときはフィルタを 1 行も足さない (resolve2 R9 の維持) | 既存 |

---

## 2. 現状分析

### 2.1 初手の処理は「画面を開いた瞬間の全体解析」から始まっている

`timeline_editor_dialog._start_blur_analysis` が、Timeline 編集画面を開いた直後に走る。

```python
# timeline_editor_dialog.py (抜粋)
if not cfg["analysis"]["auto_start"]:
    self.blur_button.setEnabled(True)
    ...
    return
self._blur_cache_path = self._find_blur_cache(store)
self.blur_button.setText("ぼかし解析中… 0%")
self._blur_worker = BlurAnalysisWorker(
    self.controller.timeline, self._settings, self._blur_cache_path, parent=self)
```

`analysis.auto_start` は既定 `true`。対象は `analyzer.collect_spans(timeline)` =
**出力に含まれるベースクリップの全区間**である。

つまり現状は、

1. 画面を開く → **動画全体**を検出 + 特徴抽出 + 輪郭で走査する
2. 終わったら `SetBlurAnalysis` で `default_policy`（既定 `blur_others`）を書き込む
3. その時点で「**主役以外はぼかす**」が既に有効になっている

利用者が何も指示していないのに、解析が走り、ぼかす対象が決まっている。
追加指示「初手のボカシ処理がいらない」はここを指している。

**費用の見積もり** (実測ではない / §8.4 で測る):

```text
10 分の切り抜き × sample_fps 10 (setting.json の現行値) = 6,000 枚
YOLOX-tiny 416px を CPU で 1 枚 30〜60ms と置くと …… 3〜6 分
+ ReID (枠の数ぶん) + 輪郭 (4 枚に 1 枚)
```

**ぼかしを 1 か所も使わない動画でも、この時間が掛かっている。**

### 2.2 ぼかすのは「検出できた人物の枠」だけ

`src/blur/plan.py` `BlurPlan._role`。

```python
policy = self._decisions.get("default_policy") or self._cfg["default_policy"]
if policy == decisions_module.POLICY_BLUR_OTHERS:
    return ROLE_BLUR
return None                     # manual_only で未指定 = 対象外
```

したがって次はすべて素で出る。

| 素で出る場面 | なぜ |
| --- | --- |
| 検出器が人物を出せなかった時刻 | YOLOX の `detector_score` (既定 0.4) 未満。後ろ姿・小さい・見切れ |
| `min_track_sec` (既定 0.6 秒) より短く映った人物 | `Tracker._finish` が誤検出として捨てる |
| 人物ではない写り込み (名札・画面・書類・車のナンバー) | 検出対象が人物クラスだけ |
| 枠は出たが輪郭がずれた時刻 | `shape=silhouette` で輪郭が体から外れる |

resolve2 以降、この漏れは「余白を足す」「動いた量だけ広げる」「輪郭を四角へ落とす」と
**塗る側を太らせる**ことで潰してきた (`margin_ratio` / `dilate_ratio` / `margin_box_ratio` /
`motion_lookahead_sec` / `fast_motion_box_ratio` の 5 つが全部そのための設定)。
これは**漏れの原因 (検出できないこと) を直していない**ため、いくら太らせても構造的に残る。

要望①は、この構造を裏返す。**塗り残しを 0 にできる唯一の方法は「全部塗ること」**である。

### 2.3 「ぼかさない」は輪郭ちょうどで削っている (要望② の原因)

`mask_builder._MaskPainter.paint` の合成は resolve3 §3.4 の決めごとで動いている。

```text
ぼかす層     B = 形を塗る → 膨張 → フェザー
ぼかさない層 K = 形を塗る          … 膨張もフェザーもしない
最終マスク   M = B × (1 − K)
```

`K` を膨張させない理由は resolve3 §3.4 にこう書いてある。

> K は**膨張させない**。ぼかさない人物の輪郭ちょうどで削る。余白を足すと、隣のぼかす人物の身体が見えてしまう

設定 `blur.render.keep_margin_ratio` は用意されているが **既定 0.0**、
`blur.render.keep_motion_margin` も **既定 false**。出荷状態では余裕が一切ない。

要望②「外したい人物にボカシがかぶってしまう」の原因は 3 つある。

| # | 原因 | 詳細 |
| --- | --- | --- |
| (a) | 削る形が輪郭ちょうど | 輪郭モデル (MobileSAM 系) の境界は数 px 内側に出やすい。髪の毛・肩の縁が `B` 側に残る |
| (b) | フェザーが `B` にしか掛からない | `B` は `feather_ratio` (既定 0.006 = 1920px で 11.5px) ぼかしてから `K` で削る。`K` の外側では `B` のフェザーの裾が立ち上がるため、境界の外 11px 前後はぼけたまま人物の縁にかぶる |
| (c) | 枠が途切れると外れる | `shapes_at` は `role == ROLE_BLUR` のときだけ `pad_sec` (既定 0.2 秒) 前後へ伸ばす。**`keep` は伸ばさない**ため、枠が 1 サンプル切れた瞬間だけ穴が閉じ、顔がぼける |

`(c)` は「画面全体をぼかす」方式では**もっと目立つ**。
いまは「穴が閉じても背景がぼけるだけ」だが、新方式では「閉じた瞬間に人物がぼける」。
R2-2 として先に潰す。

### 2.4 人物の統一処理はどこで効いているか (要望③)

統一は 2 段構えで、**解析側と指定側の両方**に散っている。

| 場所 | 何をしているか |
| --- | --- |
| `tracker.cluster()` | 全枠の平均 ReID ベクトルを貪欲に統合し `p1, p2, …` を振る |
| `tracker._appears_together()` / `_same_box()` | 同じフレームに並ぶ枠は統合しない安全弁 |
| `tracker.apply_merges()` | 「同じ人物にする」の指定を当て込む |
| `tracker._to_identities()` | 合計秒数の降順に並べ、先頭を**主役**にする。`max_identities` (既定 50) で打ち切る |
| `analyzer.analyze()` | `cluster` を呼び `analysis["identities"]` を作る |
| `plan.BlurPlan._effective_identities()` | `splits` を当て込んで人物一覧を作り直し、主役を決める |
| `plan.BlurPlan._role()` | 主役なら暗黙に `keep` |
| `decisions.py` | `identities` / `merges` / `splits` / `names[].id` |
| `blur_spec_dialog.py` | 人物一覧・統合ボタン・分割・割り当て・主役変更の確認 |

依頼者の言う「精度が低い」は `cluster` の結果である。全身 ReID は
**同じ場所・同じ照明・似た服装**で距離が縮む。`merge_threshold` (既定 0.30) を上げれば別人が混ざり、
下げれば同じ人が割れる。resolve3 §5.8 で `_appears_together` を足して「並んで映る 2 人」は救ったが、
時間的に離れた別人は救えない。**しきい値 1 本で決める方式の限界**であり、調整で解決しない。

### 2.5 指定はすべて動画全体に効く (要望④ の原因)

| 指定 | 効く範囲 | 根拠 |
| --- | --- | --- |
| 人物への `blur` / `keep` | **その人物 ID の全枠** = 全素材・全セクション | `commands.SetBlurDecision` のコメント「人物 ID に対する指定のため、**全セクションへ同時に効く**」 |
| 領域 (場所) | **その素材の全区間**。`region_tracker._tracks_for_region` は `collect_spans` の全区間を追う | `region_tracker.py` 206〜 |
| 手で足した枠 (人物・物) | アンカーを含む**セクション 1 つ** | `manual_tracker._section_of` (`manual_tracker.py` 86) |
| 削除した枠 (`excluded`) | アンカーに当たる枠 1 本 | `decisions.anchor_matches` |

`decisions.load` が返す辞書には**区間という概念が 1 つも無い**。
`identities` は `{人物 ID: モード}` の辞書、`regions` は形と追従方法だけ。
「このクリップだけ外す」は今の書式では表現できない。**書式を変えるしかない。**

依頼者の運用 (アーカイブ切り抜き) では 1 本の VOD から複数のセクションを切り出す。
セクションごとに映っている人が違うため、「動画全体に効く」は実用上かなり困る。

### 2.6 マスクは「黒地に白を描く」前提で最適化されている

```python
# mask_builder.py (抜粋)
# 何も描かないフレームは同じ絵を使い回す (作り直さない)。
# 出力の大半は「ぼかす対象が映っていない」フレームのため、ここが効く。
self.blank = Image.new("L", (width, height), 0)
```

新方式では「**ぼかすと立てたクリップの時刻だけ**白から始まり、それ以外は黒のまま」になる。
つまり**この最適化はそのまま生きる** (ぼかさないクリップは `blank` の使い回し)。
初版の「Timeline 全体が白」案より、ここは素直になった。

いっぽう `blur_overlay.build_chains` は `gblur` を**入力全体**に掛けてから `alphamerge` で抜いている。
マスクの中身が変わってもフィルタ式は**一切変わらない**。**ここは作り直さなくてよい。**

### 2.7 「必要なら行う」を機械に判定させられるか

追加指示の「ボカシが必要なら行う判定」を**自動判定**と読むこともできる。
しかしそれは成立しない。

* 「必要か」を機械が判定する唯一の手掛かりは**人物が映っているか**である。
* それを知るには**検出を走らせる**必要がある。= 初手の自動解析が復活する。
* しかも「映っている = ぼかすべき」ではない。配信者本人・許可済みの出演者は映っていてもぼかさない。

したがって **「必要かどうかは利用者が決める」**。
機械にできるのは判断の**補助** (「このクリップには人物が N 人映っています」と教える) までで、
それも解析を走らせた後にしか言えない。補助を付けるかは §10 #1 で確認する。

### 2.8 既存の資産で使えるもの

作り直しといっても、下回りはほぼそのまま使える。

| 使えるもの | どこ | 新設計での役割 |
| --- | --- | --- |
| 検出 + 区間内トラッキング | `detector.py` / `tracker.Tracker` | そのまま。**枠がそのまま指定の対象になる** |
| 輪郭 (silhouette) | `silhouette.py` / `contour.py` | そのまま。**外す形**として使う |
| アンカー (`track_anchor` / `anchor_matches`) | `decisions.py` | そのまま。解析し直しても指定が生き残る仕組み |
| 領域・手動の追従 | `region_tracker.py` / `manual_tracker.py` | そのまま。追う範囲だけ scope に合わせる |
| 解析の区間指定 | `analyzer.collect_spans` / `_analyze_span` | **区間を外から渡せるようにするだけ**でクリップ単位の解析になる |
| 素材ごとの由来キー | `store.media_key` / `media_snapshot` | **部分解析の妥当性判定に流用できる** (§3.7) |
| マスク動画の書き出し | `mask_builder._encode_pyav` / `_encode_ffmpeg` | そのまま |
| 焼き込み | `modules/blur_overlay.py` | **変更なし** |
| プレビューへの反映 | `preview.BlurPreview` | ほぼそのまま |
| Undo/Redo | `commands.Command` + `_snapshot` の deepcopy | そのまま |

---

## 3. 方式選定

### 3.1 ぼかしをどの単位で掛けるか (Q1 / R0 / R1)

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | 従来どおり、検出した人物だけぼかす | ✕ 要望①で否定済み。塗り残しが構造的に残る (§2.2) |
| B | Timeline 全体を既定で全面ぼかしにする (**本書 初版の案**) | ✕ 使わない動画・使わないクリップまでぼける。マスクも常に必要 |
| C | **クリップごとに「ぼかす」を立て、立てたクリップだけ画面全体をぼかす** | ◎ **採用** |

**採用: 案 C。**

```text
既定             … 何も立っていない = マスクを作らない = FFmpeg に 1 行も足さない (R6)
クリップに「ぼかす」… そのクリップの時間だけ、画面全体が白いマスクになる
「外す」指定       … その上に重ねて穴を開ける
```

**「ぼかす」も指定 (mark) として表す。**

画面全体を対象にした指定を 1 件足すだけで、クリップのぼかしが表現できる。

```json
{ "id": "m1", "mode": "blur",
  "target": { "kind": "frame" },
  "scope":  { "media_id": "clip1", "start": 60.0, "end": 96.5, "width": "clip",
              "clip_id": "c12", "label": "クリップ 3" } }
```

これで**指定の仕組みが 1 つに揃う**。基底という概念も `base` 設定も要らない。

| やりたいこと | 指定 |
| --- | --- |
| このクリップをぼかす | `mode: blur` / `target: frame` / `scope: クリップ` |
| その中の人を見せる | `mode: keep` / `target: track` / `scope: クリップ` |
| 見せた中の一部を隠す | `mode: blur` / `target: region` / `scope: クリップ` |
| この場面まるごとぼかす | `mode: blur` / `target: frame` / `scope: 場面` |

### 3.2 指定の重ね順 (Q2 / R1-2)

いまの合成規則 `M = B × (1 − K)` は**「ぼかさない」が無条件に勝つ**。
これだと「画面全体をぼかす」と「人を外す」が両立しない (外す方が常に勝って全部見えてしまう…
ではなく、そもそも全体をぼかす手段が無い)。また「外した中をまた塗る」も作れない。

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | 今までどおり keep が常に勝つ | ✕ R1-2 が作れない |
| B | 対象の種類で優先を決める (frame < region < track など) | △ 規則を覚えないと結果が読めない |
| C | **指定した順に重ねる (後から足したものが上)** | ◎ **採用** |

**採用: 案 C。**規則は 1 つだけ。

```text
M = 0 (何も指定が無ければ真っ黒 = ぼかさない)
効いている指定を並び順に 1 つずつ重ねる:
    α = その指定の形 (膨張 + フェザー済み / 0〜255)
    v = 255 (ぼかす) または 0 (ぼかさない)
    M = M × (1 − α/255) + v × (α/255)
```

* 利用者から見た規則は「**最後にやった操作が勝つ**」。
  クリップをぼかす → 人を外す → その中の名札を塗る、が積んだ順に効く。
* 指定の一覧で「上へ / 下へ」で並べ替えられる (§5.8.3)。
* resolve3 §3.4 の「ぼかさない優先」は、新しい規則で**自然に再現される**
  (ぼかす指定を先に、外す指定を後に足すため)。

### 3.3 解析をいつ走らせるか (Q3 / R0-3)

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | 今までどおり、画面を開いたら全体を解析 | ✕ 追加指示 ⓪ の否定対象 |
| B | 手動ボタンだけ (`auto_start = false` 相当) | △ 押すと結局全体が走る |
| C | **ぼかすと立てたクリップで、枠が要るときだけ、その区間を解析** | ◎ **採用** |

**採用: 案 C。**走らせる条件を絞る。

| 操作 | 解析 |
| --- | --- |
| Timeline 編集画面を開く | **走らない** |
| クリップに「ぼかす」を立てる | **走らない** (画面全体を塗るだけ。枠は要らない) |
| 書き出す | **走らない** (外す指定が無ければ枠は要らない) |
| ぼかし指定画面を開く | 対象クリップに解析結果が無ければ、**そのクリップの区間だけ**走る |
| 指定画面で別のクリップへ移る | そのクリップが未解析なら、**その区間だけ**走る |

**費用の見積もり** (§2.1 と同じ置き方 / 実測は §8.4):

```text
30 秒のクリップ × sample_fps 10 = 300 枚 × 30〜60ms ≒ 9〜18 秒
(10 分の全体解析 3〜6 分 に対して 1/20 前後)
```

クリップを見ながら待てる長さに収まる。進捗はダイアログに出す。

**部分解析の持ち方**は §3.7 で決める。

### 3.4 「余裕」をどう取るか (Q4 / R2)

要望②「輪郭からすこし余裕をもって外してください」を 3 つに分けて実装する。

**(1) 形を外側へ膨らませる**

ぼかす側の `_grow` (plan.py) と同じ考え方を、外す側にも入れる。

```text
外す形の余裕 grow_keep
  = max(keep_margin_ratio × キャンバス幅,                  … 画面に対する最低限の太さ
        keep_margin_box_ratio × min(枠の幅, 枠の高さ))      … 大写しの人物ほど太く
    + 動いた量 (keep_motion_margin が true のとき)
  上限 = keep_max_margin_box_ratio × max(枠の幅, 枠の高さ)   … 背景まで出さない
  下限 = keep_feather_px                                   … フェザーが輪郭の内側へ食い込まない
```

既定値の根拠 (キャンバス 1920x1080 のとき):

| 設定 | 既定 | 1920px でいくつか | 理由 |
| --- | --- | --- | --- |
| `keep_margin_ratio` | 0.010 | 19px | 輪郭モデルの境界ずれ (数 px) + 隣のぼかしのフェザー裾 (11.5px) を超える太さ |
| `keep_margin_box_ratio` | 0.10 | 枠の短辺の 10% (短辺 200px なら 20px) | 手前に大写しの人物ほど腕・髪の動きが大きく見えるため |
| `keep_max_margin_box_ratio` | 0.5 | 枠の長辺の 50% | 広げすぎて背景まで素通しにしない |
| `keep_motion_margin` | **true** (現 false) | — | 輪郭が動きに追いつかないぶんを吸収する |

**(2) 膨らませた外側でフェザーする**

いまはフェザーを `B` にしか掛けていない (resolve3 §3.4 — 削った境界からぼかしが染み出すため)。
新しい合成 (§3.2) では**指定ごとに自前のフェザーを持つ**ので、この問題が起きない。

```text
外す形 = 輪郭 → grow_keep ぶん膨張 → keep_feather_ratio ぶんフェザー
```

`grow_keep ≥ keep_feather_px` を保証する (上の下限) ため、**輪郭そのものの位置では必ず完全に外れる**。
フェザーはその外側でだけ効き、ぼかしへなだらかに戻る = 切り抜き感が出ない。

**(3) 枠が途切れた前後も保持する (R2-2)**

`shapes_at` の `pad` を役割ごとに分ける。

```text
ぼかす形 … pad_sec       (既定 0.2 秒 / 現行どおり)
外す形   … keep_hold_sec (既定 0.3 秒 / 新規)
```

枠が 1〜2 サンプル切れても穴が開いたままになるため、人物が点滅してぼけない。
伸ばしている間は端のサンプルの矩形を保持する (`plan._rect_between` の既存の挙動)。

**副作用 (明記しておく)**: 余裕を取るぶん、**人物のまわりの背景も素通しになる**。
これは要望どおりの動作である。背景を見せたくない場合は `keep_margin_ratio` を下げる
(設定画面から変えられるようにする / §5.12)。

### 3.5 クリップ単位のスコープをどう覚えるか (Q5 / R4)

| 案 | 覚え方 | 判定 |
| --- | --- | --- |
| A | クリップ ID (`clip.id`) | ✕ クリップを分割・削除すると指定が片側にしか残らない / 消える。無音カットや編集のたびに壊れる |
| B | **素材の時間区間 `(media_id, start, end)`** | ◎ **採用**。分割・移動・並べ替えに強い |
| C | Timeline の時間区間 | ✕ 前のクリップを 1 本削るだけで全部ずれる |

**採用: 案 B。**指定を作った時点で、**基準にしたクリップの素材区間**を写し取って持つ。
クリップ ID は表示 (「クリップ 3」) のためだけに一緒に控える。判定には使わない。

**適用範囲は 3 段階から選べる** (R4-2)。既定は要望どおり「このクリップ」。

| 範囲 | 区間の作り方 | 使う場面 |
| --- | --- | --- |
| `clip` (既定) | そのクリップの `(media_id, source_in, source_out)` | 要望④ そのもの |
| `section` | そのクリップを含む「場面」全体。アーカイブ用は同じ `origin.archive_index` のクリップ群、クリップ用は `analyzer.collect_spans` が返す連続区間 1 つ | **無音カットで 1 つの場面が数十クリップに割れている場合の救済** |
| `media` | その素材の全区間 | 最初から最後まで同じ扱いにしたいとき |

`section` を用意する理由: 依頼者の運用では無音カットが走るため、
1 つの場面が数十本のベースクリップへ割れる。
「このクリップだけ」しか無いと、同じ場面を何十回もぼかすことになる。
どちらを既定にするかは §10 #2 で確認する。

**複数選択との関係**: Timeline 編集画面で複数のクリップを選んで「ぼかす」と、
選んだクリップの数だけ指定を足す (各クリップの区間で 1 件ずつ)。
隣り合うクリップは区間をつないで 1 件にまとめる (§5.9)。

### 3.6 統一を外した後、何を外す対象にするか (Q6 / R3)

| 案 | 対象 | 判定 |
| --- | --- | --- |
| A | 枠 (tracklet) 1 本。アンカーで覚える | ◎ **採用**。解析結果にそのまま存在する単位。アンカーの仕組み (resolve3 §3.5) を流用できる |
| B | 囲んだ範囲だけ。枠を使わない | ✕ 人が動くと追えない。輪郭も使えない = 要望②と噛み合わない |
| C | 画面の固定領域 | ✕ 同上 |

**採用: 案 A。**指定の対象は 3 つになる。

```text
kind = "frame"   画面全体 (クリップをぼかすスイッチ / §3.1)
kind = "track"   検出された枠。アンカー (素材・時刻・矩形) で覚える
kind = "region"  手で囲んで足した枠 (既存の regions。場所 / 人物・物の 2 種類)
```

**枠が割れる問題**への手当て:

統一を外すと、遮蔽や見切れで 1 人が複数の枠に割れる。
そのたびに外し直すのは手間なので、次の 2 つで受ける。

1. **ReID を「枠の継続の救済」としてだけ残す**。
   `Tracker._match` の埋め込み距離による復帰は、統一 (離れた場面をまたいで同一人物とみなす) ではなく
   「一瞬の遮蔽で飛んだ枠を同じ枠として続ける」処理であり、精度の問題が出にくい。
   全体クラスタリング (`cluster`) だけを外す。
2. 指定画面で**枠を囲むと、囲みに入った枠すべてに同じ指定が付く** (既存の `identities_in_path` を流用)。
   割れていても 1 回のドラッグでまとめて外せる。

あわせて、**ReID モデルを任意扱いにする** (`models.availability` の必須から外す)。
無ければ IoU だけで追う (枠は割れやすくなるが動く)。
`silhouette` が既に同じ扱い (無ければ四角へ落ちる) という前例がある。

### 3.7 部分解析の持ち方 (Q7 / R0-3)

クリップ単位で解析すると、キャッシュは「**一部だけ解析済み**」になる。

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | クリップごとに別ファイル | ✕ ファイルが増える。resolve4 §10 #11 で「1 つにまとめる」と決めた経緯に反する |
| B | **1 ファイルに「解析済み区間」を持たせて追記する** | ◎ **採用** |

**採用: 案 B。**

```jsonc
{
  "schema": 4,
  "settings_key": "canvas=1920x1080|sample_fps=10.000|detector=…|models=…|silhouette=…",
  "media": { "clip1": { "key": "vod=…|span=…|size=…|dur=…", "width": 1920, … } },
  "spans": { "clip1": [[60.0, 96.5], [120.0, 138.2]] },   // 解析済みの素材区間
  "tracks": [ … ]
}
```

妥当性の判定を 2 段にする。

1. `settings_key` (キャンバス・`sample_fps`・モデル) が違う → **全部捨てて作り直す**
2. `media[id].key` (`store.media_key` の値 / 素材の由来) が違う → **その素材の区間と枠だけ捨てる**

これで「素材 A を差し替えたが素材 B はそのまま」を正しく扱える。
いまの「指紋 1 本が違えば全部捨てる」より細かく、**解析し直しの回数が減る**。

必要な区間が `spans` に含まれていなければ、**足りない区間だけ**解析して追記する。
`analyzer._merge_ranges` をそのまま使って区間をまとめる。

### 3.8 旧プロジェクトをどう扱うか (Q8 / R5)

`source.blur` の版を **3 → 4** に上げる。

| 旧データ | 新設計での扱い |
| --- | --- |
| `default_policy: blur_others` | **無視する。**新方式では「何も立てない = ぼかさない」。**旧プロジェクトを開くとぼかしが消える**ため、指定画面のボタンに注意書きを出す (§5.13) |
| `default_policy: manual_only` | 無視 (もともと指定した所だけだったので、実害が小さい) |
| `identities` の `keep` | **捨てる。**新方式では「ぼかす」が立っていない = 全部見えるので、`keep` は意味を持たない |
| `identities` の `blur` | §10 #3 で決める。**推奨は「その人物の枠を含むクリップに『ぼかす』を立てる」**まではせず、捨てて案内だけ出す |
| `regions` (形・追従方法) | **そのまま引き継ぐ**。`mode` は指定 (mark) へ移し、適用範囲は `media` |
| `excluded` (隠した枠) | **そのまま引き継ぐ** (アンカーの形は変わらない) |
| `names` | **アンカーを持つものだけ引き継ぐ**。人物 ID しか持たない項目は捨てる (§5.6.4) |
| `merges` / `splits` | **捨てる** (統一の廃止) |

**旧プロジェクトは、開いた時点でぼかしが掛からない状態になる。**
これは「初手の処理をやめる」という指示から避けられない。
黙って変えず、指定画面を開いたときに 1 度だけ次を出す。

> 以前のぼかしの指定は、新しい方式へそのまま移せませんでした。
> ぼかしたいクリップを選んで「このクリップをぼかす」を押してください。
> 追加した枠と名前は引き継いでいます。

---

## 4. 設計方針

* **既定は「何もしない」。**機能が有効でも、指定が 1 つも無ければ解析もマスク生成もフィルタも走らない (R0 / R6)。
* **仕組みは 1 つにする。**クリップのぼかしも、人を外すのも、領域を塗るのも、すべて「指定 (mark)」。
  基底・方針・主役といった別立ての概念を置かない。
* **判定は 1 か所に置く** (resolve3 §4-5 の踏襲)。
  指定画面・プレビューの目印・マスク生成は `blur.plan` から同じ結果を引く。
  役割が時刻で変わる (scope があるため) ので、API を `entry["role"]` (固定) から
  `plan.marks_at(media_id, source_sec)` (時刻つき) へ変える。
* **重い処理は必要になってから。**解析はクリップ単位・要求時。マスクは指定のある時刻だけ。
* **失っても壊れない方へ倒す。**枠が取れない・輪郭が無い・モデルが無い場合でも、
  「ぼかすと立てたクリップは必ず全面がぼける」。分からないときは**安全側 (ぼける)** になる。
* **設定は `setting.json` に置く** (`docs/claude.md` の制約)。余裕の量・適用範囲の既定はすべて設定値。
* **コメントは日本語**、関数には目的を書く (同制約)。
* **既存実装を破壊しない**: `blur_overlay` / マスクの書き出し / `frame_source` / `commands` の枠組みは触らない。

---

## 5. 詳細設計

### 5.1 モジュール構成 (変更のあるものだけ)

| ファイル | 変更 | 内容 |
| --- | --- | --- |
| `src/blur/config.py` | 中 | `default_policy` 廃止、余裕の設定 6 件追加、`analysis.merge_threshold` / `max_identities` / `same_box_*` 削除、`auto_start` の意味変更 |
| `src/blur/decisions.py` | **大** | 書式 version 4。`marks` 新設。`identities` / `merges` / `splits` 削除。scope の作成・判定を追加 |
| `src/blur/plan.py` | **大** | 人物の層を削除。指定を重ね順のまま時刻で引く |
| `src/blur/mask_builder.py` | **大** | `_MaskPainter` を「順番に重ねる」方式へ。外す形の膨張・フェザー |
| `src/blur/tracker.py` | **大** | `cluster` / `_to_identities` / `apply_merges` / `_appears_together` / `_same_box` を削除 |
| `src/blur/analyzer.py` | **大** | 区間を外から渡せるようにする。`identities` を作らない。部分解析の追記 |
| `src/blur/store.py` | **大** | `SCHEMA_VERSION` 3 → 4。`settings_key` / `spans` / `tracks[].thumb` 新設、`identities` 廃止 |
| `src/blur/models.py` | 小 | ReID を必須から外す |
| `src/blur/preview.py` | 小 | 変更はほぼ無い (§5.10) |
| `src/blur/region_tracker.py` / `manual_tracker.py` | 小 | 追う範囲を指定の scope に合わせる |
| `src/timeline/commands.py` | **大** | ぼかし系コマンドを入れ替え |
| `src/gui/timeline/blur_spec_dialog.py` | **大** | 画面の作り直し |
| `src/gui/timeline/timeline_editor_dialog.py` | **大** | 自動解析の廃止。クリップの右クリックに「このクリップをぼかす」 |
| `src/gui/timeline/timeline_view.py` (クリップ表示) | 小 | ぼかすクリップに印を出す |
| `src/gui/timeline/blur_canvas.py` | 小 | 外した枠を緑で描く |
| `src/settings/settings_window.py` | 中 | 「既定方針」コンボを削除。余裕の設定を追加 |
| `src/settings/setting.json` | 中 | §7 のとおり |

### 5.2 データモデル — `source["blur"]` version 4

```jsonc
{
  "version": 4,
  "cache": "output/vod_xxxx.blur.json",
  "cache_abs": "D:/.../vod_xxxx.blur.json",

  // 指定。並び順 = 重ね順 (後ろほど上 / §3.2)
  "marks": [
    { "id": "m1", "mode": "blur",
      "target": { "kind": "frame" },
      "scope":  { "media_id": "clip1", "start": 60.0, "end": 96.5,
                  "width": "clip", "clip_id": "c12", "label": "クリップ 3" } },

    { "id": "m2", "mode": "keep",
      "target": { "kind": "track",
                  "anchor": { "media_id": "clip1", "t": 73.2, "rect": [820, 140, 260, 640] } },
      "scope":  { "media_id": "clip1", "start": 60.0, "end": 96.5,
                  "width": "clip", "clip_id": "c12", "label": "クリップ 3" } },

    { "id": "m3", "mode": "blur",
      "target": { "kind": "region", "id": "r4" },
      "scope":  { "media_id": "clip1", "start": 60.0, "end": 96.5,
                  "width": "clip", "clip_id": "c12", "label": "クリップ 3" } }
  ],

  "regions":  [ { "id": "r4", "kind": "object", "label": "追加 1", "media_id": "clip1",
                  "anchor_sec": 78.0, "path": [[0.41,0.22]], "follow": "track" } ],
  "excluded": [ { "media_id": "clip1", "t": 12.4, "rect": [10, 20, 30, 40] } ],
  "names":    [ { "anchor": { "media_id": "clip1", "t": 73.2, "rect": [820,140,260,640] },
                  "name": "スタッフA" } ],
  "region_seq": 4,
  "mark_seq": 3
}
```

v3 からの変更:

| キー | v3 | v4 |
| --- | --- | --- |
| `default_policy` | `blur_others` / `manual_only` | **削除** (方針という概念をやめた) |
| `identities` | `{人物 ID: mode}` | **削除** → `marks` |
| `merges` / `splits` | あり | **削除** |
| `marks` | — | **新設** (並び順 = 重ね順) |
| `regions[].mode` | あり | **削除** (指定は `marks` が持つ) |
| `names[].id` | あり | **削除** (アンカーのみ) |
| `mark_seq` | — | **新設** (ID を再利用しない。`region_seq` と同じ考え方) |
| `fingerprint` | あり | **削除** (妥当性は解析結果側の `settings_key` / `media[].key` で判定する / §3.7) |

**大きさ**: 指定 1 件は 200 バイト程度。
クリップ 1 本ごとに「ぼかす」+「外す」を立てると、100 クリップで 200 件 = 約 40KB。
resolve2 §5.2.2 の「`source` には 1KB 程度」という当初の想定は超えるが、
`regions[].path` (1 件 64 点 = 約 1.5KB) が既に超えているため新しい問題ではない。
`marks` が 1000 件を超えたら警告ログを出すに留める。

### 5.3 `decisions.py` — 新しい API

```python
# 書式版
VERSION = 4

# 指定の値
BLUR = "blur"      # ぼかす
KEEP = "keep"      # ぼかさない (外す)

# 指定の対象 (ver5 resolve7 §3.6)
TARGET_FRAME = "frame"    # 画面全体 = そのクリップをぼかすスイッチ
TARGET_TRACK = "track"    # 検出された枠 (アンカーで覚える)
TARGET_REGION = "region"  # 手で囲んで足した枠

# 指定が効く広さ (ver5 resolve7 §3.5)
SCOPE_CLIP = "clip"        # そのクリップの素材区間 (既定)
SCOPE_SECTION = "section"  # その場面 (アーカイブは同じセクション / クリップ用は連続区間)
SCOPE_MEDIA = "media"      # その素材の全区間


# 指定 (mark) を足した新しい decisions を返す。
# **一覧の末尾へ足す = 一番上に重なる** (後からやった操作が勝つ / §3.2)
def with_mark(decisions, mark): ...

# 指定を消す / モードを変える / 適用範囲を変える / 重ね順を 1 つ動かす
def without_mark(decisions, mark_id): ...
def with_mark_mode(decisions, mark_id, mode): ...
def with_mark_scope(decisions, mark_id, scope): ...
def with_mark_moved(decisions, mark_id, delta): ...    # delta = +1 (上へ) / -1 (下へ)

# 指定 ID を採番する (m1, m2, …)。
# 一度使った番号は二度と使わない (region_seq と同じ理由 / resolve3 §5.7)
def next_mark_id(decisions): ...


# クリップから適用範囲を作る (ver5 resolve7 §3.5)
#   timeline : 対象の Timeline
#   clip     : 基準にするベースクリップ (選択中のクリップ / 再生位置のクリップ)
#   width    : SCOPE_CLIP / SCOPE_SECTION / SCOPE_MEDIA
# 戻り値: {"media_id","start","end","width","clip_id","label"}
#
# クリップ ID だけを覚えず **素材の区間**を写し取るのは、クリップを分割・移動しても
# 指定が生き残るようにするため (§3.5 案 A を採らない理由)。
def scope_for(timeline, clip, width):
    media_id = str(clip.media_id)
    if width == SCOPE_MEDIA:
        start, end = 0.0, media_duration(timeline, media_id)
    elif width == SCOPE_SECTION:
        start, end = section_span(timeline, clip)
    else:
        start, end = float(clip.source_in), float(clip.source_out)
    return {"media_id": media_id, "start": round(start, 3), "end": round(end, 3),
            "width": width, "clip_id": str(clip.id),
            "label": scope_label(timeline, clip, width)}


# そのクリップが属する「場面」の素材区間 (ver5 resolve7 §3.5)
#   アーカイブ用 : 同じ origin[ORIGIN_ARCHIVE_INDEX] を持つベースクリップの min〜max
#   クリップ用   : analyzer.collect_spans が返す連続区間のうち、このクリップを含むもの
# どちらにも当たらなければクリップ自身の区間を返す。
def section_span(timeline, clip): ...


# その指定が、この素材のこの時刻に効くか
def scope_contains(scope, media_id, source_sec):
    if str((scope or {}).get("media_id") or "") != str(media_id):
        return False
    return float(scope["start"]) <= float(source_sec) <= float(scope["end"])


# 隣り合う・重なる scope を 1 つにまとめる (クリップを複数選んでぼかしたとき / §5.9)
def merge_scopes(scopes, gap_sec=0.04): ...


# マスクを作る必要があるか。
# 「ぼかす」指定が 1 つも無ければ作らない (= FFmpeg に 1 行も足さない / R0 / R6)。
def needs_mask(decisions, cfg=None):
    return any(m.get("mode") == BLUR for m in (decisions or {}).get("marks", []))


# 枠 (検出結果) が必要か。
# 「ぼかす」だけなら解析は要らない。外す指定 (target=track) があるときだけ必要 (§3.3)。
def needs_analysis(decisions):
    return any(m.get("target", {}).get("kind") == TARGET_TRACK
               for m in (decisions or {}).get("marks", []))


# 解析が必要な素材区間 (ver5 resolve7 §3.3)。
# 「外す」指定が効く区間だけを返す = ぼかすだけのクリップは解析しない。
def analysis_spans(decisions): ...
```

### 5.4 `plan.py` — 指定を時刻で引く

人物 (identity) の層を削り、**指定 (mark) と枠**だけにする。

```python
# ぼかしの計画 (ver5 resolve7 §5.4)
#
# 役割は **時刻によって変わる**。指定が効く区間 (scope) を持つようになったため、
# 同じ枠でも「このクリップでは外れている / 隣のクリップではぼける」が起こる。
# そのため entry["role"] (固定) をやめ、marks_at(media_id, source_sec) にした。
#
# 指定画面・プレビューの目印・マスク生成は、すべてここから同じ結果を引く
# (判定は 1 か所 / resolve3 §4-5)。
class BlurPlan:

    def __init__(self, timeline, analysis, decisions, cfg):
        ...
        # 指定を「重ね順 (= 一覧の順)」のまま持つ。描画もこの順で行う
        self._marks = list(decisions.get("marks", []))
        self.entries = [self._entry(track) for track in (analysis or {}).get("tracks", [])]

    # その時刻に効いている指定を、**重ね順のまま**返す (ver5 resolve7 §3.2)
    # 戻り値: [{"mark","mode","shape"}, …] 先頭が一番下
    def marks_at(self, media_id, source_sec):
        layers = []
        for mark in self._marks:
            if not decisions_module.scope_contains(mark["scope"], media_id, source_sec):
                continue
            shape = self._shape_of(mark, media_id, source_sec)
            if shape is None:
                continue        # 枠がまだ解析されていない / その時刻には映っていない
            layers.append({"mark": mark, "mode": mark["mode"], "shape": shape})
        return layers

    # マスクが要るか (ぼかす指定が 1 つでもあるか)
    def has_blur(self):
        return any(m["mode"] == decisions_module.BLUR for m in self._marks)
```

`_shape_of` は対象の種類ごとに形を作る。

| `target.kind` | 形 | 余裕 |
| --- | --- | --- |
| `frame` | キャンバス全体の矩形 | 不要 (膨張もフェザーも 0) |
| `track` | アンカーに当たる枠 (`anchor_matches`) のその時刻の矩形。輪郭があれば輪郭 | `keep` なら `grow_keep` / `blur` なら従来の `_grow` |
| `region` | 領域 ID の追従トラックの矩形 + 手描きの囲み (既存の `outline`) | 同上 |

```python
    # 外す形の余裕 (ver5 resolve7 §3.4 (1))
    def _grow_keep(self, canvas_rect, motion, feather_px):
        width, height = float(canvas_rect[2]), float(canvas_rect[3])
        base = max(self._keep_margin_ratio * self._timeline.width,
                   self._keep_margin_box_ratio * min(width, height))
        if self._keep_motion:
            base += motion
        limit = self._keep_max_box_ratio * max(width, height)
        # フェザーが輪郭の内側へ食い込まないよう、最低でもフェザーぶんは膨らませる
        return max(min(base, limit), feather_px)
```

**削除する API**: `identities` / `main_id` / `base_label` / `default_label` /
`identity_anchor` / `identity_of` / `_effective_identities` / `_role` /
`tracks_with_role` / `UNASSIGNED_LABEL`。

**残す API**: `shapes_at` (指定画面が、指定の付いていない枠も表示するために使う) /
`entry_of` / `excluded_entries` / `untracked_regions` / `labels` (枠の名前) / `sample_rect_at`。

### 5.5 `mask_builder.py` — 重ね塗り

```python
# 1 フレームのマスクを描く (ver5 resolve7 §3.2)
#
#   M = 0 (何も効いていなければ真っ黒 = ぼかさない)
#   効いている指定を重ね順に 1 つずつ合成する:
#       α = 形を塗る → grow ぶん膨張 → feather ぶんフェザー   (0〜255)
#       v = 255 (ぼかす) / 0 (ぼかさない)
#       M = M×(1−α/255) + v×(α/255)
#
# 「ぼかさない」が常に勝つ旧規則 (resolve3 §3.4) は廃止した。
# クリップ全体をぼかして一部を外す方式では優先を固定できないため。
# 新しい規則は「後から足した指定が上」の 1 つだけで、旧規則を包含する。
class _MaskPainter:

    def __init__(self, plan, timeline, cfg, width, height):
        ...
        # 指定が 1 つも効いていないフレームは、この絵を使い回す (作り直さない)。
        # ぼかさないクリップの時刻はすべてこれになる = 出力の大半で費用ゼロ。
        self.blank = Image.new("L", (width, height), 0)

    def paint(self, media_id, source_sec):
        layers = self._plan.marks_at(media_id, source_sec)
        if not layers:
            return self.blank

        mask = np.zeros((self._height, self._width), dtype=np.uint16)
        for layer in layers:
            shape = layer["shape"]
            if shape["kind"] == decisions_module.TARGET_FRAME:
                # 画面全体は塗るまでもない (一番多い経路なので特別扱いする)
                mask[:] = 255 if layer["mode"] == ROLE_BLUR else 0
                continue
            alpha = self._alpha_of(shape)                 # ndarray uint16 (0〜255)
            value = 255 if layer["mode"] == ROLE_BLUR else 0
            mask = (mask * (255 - alpha) + value * alpha) // 255
        return Image.fromarray(mask.astype(np.uint8), mode="L")

    # 形 1 つを「膨張 → フェザー」した α を作る
    def _alpha_of(self, shape):
        layer = Image.new("L", (self._width, self._height), 0)
        self._fill(ImageDraw.Draw(layer), shape, grow_px=shape["grow"] * self._scale_x)
        feather = shape["feather"] * self._scale_x
        if feather > 0.5:
            layer = layer.filter(ImageFilter.GaussianBlur(feather))
        return np.asarray(layer, dtype=np.uint16)
```

`_fill` は現行のものをほぼそのまま使う (多角形は頂点を外へずらす / 矩形は枠を広げる)。
`expand` 引数は `grow_px` に統合する (`margin_ratio` は「ぼかす形」の grow に足し込む)。

**速さの見積もり** (実測は §8.4):

| フレーム | 費用 |
| --- | --- |
| ぼかさないクリップ | `blank` の使い回し = 実質 0 (現行と同じ) |
| ぼかすだけのクリップ | `mask[:] = 255` = ndarray の代入 1 回。約 130KB (480x270) の書き込み |
| 外す指定が 1〜3 件 | 1 件あたり「塗り + `GaussianBlur`」。マスクは `mask_scale` 0.25 で 480x270 のため 1 件 1ms 未満と見ている |

**マスク動画の大きさ**: ぼかすクリップの間だけ白くなる。
ffv1 は一様な絵をよく縮めるため、数 MB 以下の見込み。

### 5.6 統一の廃止

#### 5.6.1 `tracker.py`

**削除**: `cluster()` / `_to_identities()` / `apply_merges()` / `_appears_together()` /
`_same_box()` / `_samples_by_time()` / `_first_start()` / `_assign()`。

**残す**: `Tracklet` / `Tracker` / `mean_embedding`。
`Tracklet.identity` は「その枠が属する対象の ID」と定義し直し、
人物の枠では**自分の枠 ID を入れる** (`t12`)。領域・手動の枠は従来どおり領域 ID (`r4`)。
こうすると `plan` / 画面 / `store` の「`identity` で引く」コードがそのまま動き、
変更が `cluster` の削除だけで済む。

`Tracker._match` の埋め込み距離による救済は**そのまま残す** (§3.6)。
ReID が使えない環境では `embeddings` が空になり、IoU だけで追う。

#### 5.6.2 `analyzer.py`

```python
# 解析の入口 (ver5 resolve7 §3.3)
#   spans : 解析する素材区間 [(media_id, [(開始, 終了), …]), …]。
#           None なら従来どおり collect_spans(timeline) = 出力に含まれる全区間。
#           クリップ単位の解析では、そのクリップの区間だけを渡す。
#   analysis : 追記先の解析結果 (既にあるもの)。None なら新しく作る。
def analyze(timeline, settings, cache_path=None, on_progress=None, cancel=None,
            spans=None, analysis=None):
    ...
    # 人物へまとめる処理は廃止した。枠がそのまま指定の対象になる (§3.6)
    for track in tracklets:
        track.identity = track.id
    analysis["tracks"] = _without_spans(analysis.get("tracks", []), spans) + \
                         [track.to_dict() for track in tracklets]
    analysis["spans"] = _merge_spans(analysis.get("spans", {}), spans)
    _logger.info("ぼかし解析: 枠 %d 本を検出しました (区間 %s)", len(tracklets), _span_text(spans))
```

* `_without_spans` … 同じ区間を解析し直したときに古い枠を捨てる。
* `attach_thumbs` は**枠ごとに**見本画像を付ける (`tracks[].thumb`)。
  `_keep_thumb` は既に枠単位で集めているので、集約処理が無くなるだけで簡単になる。
* `analysis["identities"]` は**書かない**。
  `max_identities` による打ち切りも無くなるため、resolve4 §5.5.4 の
  「人物未割り当ての枠」という概念も消える。

#### 5.6.3 `store.py`

* `SCHEMA_VERSION` 3 → **4**。
* `new_analysis` から `identities` を削除、`settings_key` / `spans` を追加。
* `fingerprint()` を `settings_key()` へ置き換える (素材の由来を含めない。素材は `media[].key` で個別に見る / §3.7)。
* `identities_by_id` / `main_identity_id` / `put_identity_thumb` を
  `tracks_by_id` / `put_track_thumb` へ置き換える。
* 新設: `valid_media(analysis, timeline)` … 由来キーが一致する素材 ID の集合。
  `missing_spans(analysis, timeline, wanted)` … まだ解析していない区間。

#### 5.6.4 名前 (R3-2)

resolve4 R1 の「人物の名前」は残す。付ける相手を**枠**に変える。

```python
# decisions.names の項目 (v4)
{"anchor": {"media_id": "clip1", "t": 73.2, "rect": [...]}, "name": "スタッフA"}
```

v3 では `{"id": "p3", "name": "…", "anchor": {...}}` だった。
`anchor` は resolve4 §3.1 で「人物 ID が振り直されても名前が付いて回るように」入れたもので、
**統一を外した今はこちらだけで足りる**。`id` しか持たない項目は読み込み時に捨てる。

名前は画面の表示だけに効く。出力には影響しない。

### 5.7 オンデマンド解析 (`blur_spec_dialog` + `analyzer`)

```text
指定画面を開く / 対象クリップが変わる
        ↓
decisions.needs_analysis() が false (外す指定がまだ無い)
        ↓ yes                                    ↓ no
   何もしない (枠を出さない)              store.missing_spans() で足りない区間を出す
        ↓                                        ↓ 空でなければ
「人物を外す」を押す or 枠を見たい ───────→  その区間だけ解析 (進捗ダイアログ)
                                                 ↓
                                        解析結果へ追記 → store.save
```

* 画面を開いた直後は**枠を出さない**。「このクリップの枠を調べる」ボタンを押すか、
  `blur.analysis.auto_start` が true なら**対象クリップに入った時点で自動で**調べる。
  `auto_start` の意味を「Timeline 全体を開いたら解析」から
  「**指定画面で対象クリップを表示したら、そのクリップだけ解析**」へ変える。
* 解析中もフレームの表示・「このクリップをぼかす」の操作はできる (枠が出ないだけ)。
* 書き出し (`mask_builder.prepare`) では、`needs_analysis` が true なのに
  必要な区間が未解析なら**そこで解析してからマスクを作る** (黙って外す指定を無視しない)。

### 5.8 ぼかし指定画面 (`blur_spec_dialog.py`)

#### 5.8.1 画面構成

```text
┌─────────────────────────────────────────────┬──────────────────────────┐
│                                             │ このクリップの枠  [調べる]│
│                                             │ ┌──┬────────────────────┐│
│        フレーム (BlurCanvas)                │ │[見本] 枠 1  0:12〜0:18││
│                                             │ │[見本] 枠 2  0:14〜0:31││
│   ・ぼかすクリップなら全面ぼけて見える       │ └──┴────────────────────┘│
│   ・外した所だけ素で見える                   │                          │
│   ・枠は緑 (外す) / 赤 (ぼかす) / 灰 (未指定)│ 指定 (下にあるものが優先) │
│                                             │ ┌────────────────────────┐│
│                                             │ │ ぼかす / クリップ3 / 全体││
│                                             │ │ 外す   / クリップ3 / 枠1││
│                                             │ └────────────────────────┘│
│                                             │ [上へ] [下へ] [削除]      │
└─────────────────────────────────────────────┴──────────────────────────┘
 [◀] ─────────── スクラバ ───────────── [▶]  0:01:14
 対象: クリップ 3 (0:01:12〜0:01:40)  [☑ このクリップをぼかす]  適用範囲:[このクリップ▼]
 操作: (●) 外す / ( ) ぼかす     追加: [人物・物] [場所]      表示: [実際のぼかし ▼]
 選択中: 枠 1 (0:12〜0:18) …… [外す] [ぼかす] [名前を変更] [枠を隠す]
```

旧画面からの変更:

| 旧 | 新 |
| --- | --- |
| 人物一覧 (全動画の人物) | **このクリップの枠**一覧 (対象クリップに映る枠だけ・見本画像つき) |
| 追加した枠・場所 一覧 | **指定 (marks)** 一覧へ統合。並べ替え可能 |
| 削除した枠 一覧 | 残すが最小化。意味を「画面に出さない枠」へ変更 (§5.8.4) |
| 「同じ人物にする」「新しい人物にする」「人物へ割り当て」 | **削除** |
| 「指定: ぼかす / ぼかさない」ラジオ | 「操作: 外す / ぼかす」へ改名 |
| 「主役以外は自動でぼかします」の表示 | **削除** |
| — | **「対象: クリップ N」「このクリップをぼかす」「適用範囲」**を追加 |

#### 5.8.2 対象クリップ

* 画面を開いたとき、Timeline 編集画面で**選択中のクリップ**を対象にする
  (`controller.selected_clip()` → 無ければ `clip_at_playhead()` → 無ければ先頭のベースクリップ)。
* スクラバを動かして別のクリップへ入ったら、対象クリップも自動で切り替える。
  「対象:」「このクリップをぼかす」「枠一覧」を作り直す。
* 「適用範囲」コンボ (`このクリップ` / `この場面` / `この素材全体`) の既定は
  `blur.spec.default_scope` (§7)。**これから作る指定にだけ効く**。既存の指定は一覧から個別に変える。

#### 5.8.3 指定の作り方

| 操作 | 結果 |
| --- | --- |
| 「このクリップをぼかす」を入れる | `mark {mode: blur, target: frame, scope: 現在の適用範囲}` を**先頭へ**足す (全体は一番下に来るのが自然なため) |
| 「このクリップをぼかす」を外す | その `frame` の指定を消す。**上に載っていた外す指定は残す** (また入れれば効く) |
| 枠をクリックして「外す」 | `mark {mode: keep, target: track, anchor}` を末尾へ足す (= 一番上) |
| 枠をダブルクリック | 「外す」と「ぼかす」を入れ替える (指定が無ければ「外す」を足す) |
| フレーム上をドラッグして囲む | 囲みに入った枠すべてに同じ指定を足す (`identities_in_path` を流用 / §3.6) |
| 囲みの中に枠が無い | 「人物・物を追加」「場所を追加」と同じ扱いで領域を足し、同時に指定も足す |
| 一覧で「上へ / 下へ」 | 重ね順を変える |
| 一覧で「削除」 | その指定だけ消す (枠や領域は残る) |

すべて `commands.Command` として積むため、Timeline 編集画面の「元に戻す」と
この画面の Ctrl+Z / Ctrl+Y で戻せる (現行の作法を維持)。

#### 5.8.4 「枠を隠す」(旧「枠を削除」)

`excluded` は**画面に出さないだけ**の意味に変える。
新方式では、誤検出の枠があっても**出力は変わらない** (指定を付けなければ何も起きない) ため、
「削除」という強い言葉をやめる。

#### 5.8.5 表示モード

| 値 | 表示 | 変更 |
| --- | --- | --- |
| `blur` (既定) | 書き出しと同じぼかしを当てた絵 | 変更なし |
| `keep` | **外した範囲を緑で塗る** | 旧 `mask` (ぼかす範囲を赤) を置き換える。ぼかすクリップでは画面全体が赤くなり役に立たないため |
| `none` | 枠だけ | 変更なし |

実装は `preview.mask_to_rgba` に反転を足すだけ (`255 - value` を α にする)。
旧 `mask` は読み込み時に `keep` へ読み替える。

### 5.9 Timeline 編集画面 (`timeline_editor_dialog.py`)

* **自動解析を廃止する。**`_start_blur_analysis` と `BlurAnalysisWorker` の呼び出しをやめ、
  「ぼかし指定...」ボタンは**常に押せる**状態にする (モデルが無い場合だけ理由をツールチップへ)。
  解析は指定画面の中でクリップ単位に走る (§5.7)。
* **クリップの右クリックメニューに追加する** (R0-2 / 「選択したクリップに対して」)。

  | 項目 | 動作 |
  | --- | --- |
  | このクリップをぼかす | 選択中のクリップそれぞれに `frame` の指定を足す。隣り合うクリップは `merge_scopes` で 1 件にまとめる |
  | この場面をぼかす | 適用範囲 `section` で 1 件足す |
  | ぼかしを外す | そのクリップに掛かっている `frame` の指定を消す |
  | ぼかし指定... | そのクリップを対象にして指定画面を開く |

* **クリップに印を出す。**ぼかす指定が掛かっているクリップは、クリップの表示に小さな印
  (帯・アイコン) を出す。どこをぼかすのか Timeline を見ただけで分かるようにする。
* `_update_blur_markers` (赤い目印) は**「外した所」を緑の枠で示す**へ変更。
  ぼかすクリップでは「ぼかす所」を示しても画面全体になるため。
* 停止中のプレビューへ実際のぼかしを反映する経路 (`preview_blur` / resolve4 §5.8) はそのまま。

### 5.10 プレビューの速さ (`preview.py`)

現行の速さは「マスクが真っ黒なフレームは入力をそのまま返す」
「白い所の外接矩形だけぼかす」の 2 つで成り立っている (resolve4 §2.6)。

新方式では、

* **ぼかさないクリップ** … マスクが真っ黒 → **現行どおり素通し** (費用ゼロ)
* **ぼかすクリップ** … 画面全体が白 → 外接矩形も画面全体になり、全面をぼかす

後者のために、**縮小してからぼかす**経路を足す。
素材解像度でガウスを掛けず、`mask_scale` と同じ倍率へ縮小 → ぼかす → 元へ戻す。
ガウスは σ に比例して重いため、1/4 に縮めれば σ も 1/4 でよく、費用はおおむね 1/16 になる。
プレビューは見た目の確認用なので、この精度で足りる (書き出しは従来どおり原寸)。

間に合わない環境のために、`blur.preview_blur` を false にすれば
従来どおり「枠だけ」の表示へ落ちる (既存の逃げ道)。

書き出し側 (`blur_overlay`) は**変更しない**。
FFmpeg は元から全面に `gblur` を掛けてから `alphamerge` している。
**これまでもマスクが真っ黒な区間で `gblur` は走っていた**ため、
ぼかすクリップが増えても**書き出し時間はほとんど変わらない**見込みである (実測は §8.4)。

### 5.11 追従の範囲を scope に合わせる

`region_tracker.build_region_tracks` / `manual_tracker.build_manual_tracks` は
いま「その素材の全区間」「アンカーを含むセクション」を追っている。
指定に scope が付いたので、**その領域を指す指定の scope の和集合**だけを追う。

```python
# 領域を追う範囲を決める (ver5 resolve7 §5.11)
# その領域を指している指定 (marks) の scope をまとめたものだけ追う。
# 指定がまだ無い (追加直後) なら、アンカーを含むクリップの区間を追う。
def spans_for_region(timeline, decisions, region_id, cfg): ...
```

効果は 2 つ。追従の計算量が減る / 別の場面まで追いかけて誤爆する事故が減る。
適用範囲を後から広げたとき (`clip` → `media`) は、足りない区間だけ追い足す
(`analyzer.ensure_added_tracks` と同じ入口)。

### 5.12 設定画面 (`settings_window.py`)

| 項目 | 変更 |
| --- | --- |
| 「囲っていない人物の扱い」コンボ | **削除** (方針という概念をやめた) |
| — | 「**外すときの余裕**」スライダ (0〜5% / `keep_margin_ratio` を百分率で見せる) |
| — | 「**外した縁をなじませる**」チェック (`keep_feather_ratio` を 0 / 既定値で切り替え) |
| — | 「**指定の既定の適用範囲**」コンボ (`このクリップ` / `この場面` / `この素材全体`) |
| 「解析を自動で始める」 | 説明を「指定画面で表示中のクリップを自動で調べる」へ変更 |
| モデルの状態表示 | ReID が無くても「準備完了」と出す。`人物同定モデルが無いため、枠が途切れやすくなります` を添える |

### 5.13 エラー処理とログ

| 事象 | 扱い |
| --- | --- |
| ぼかす指定が 1 つも無い | マスクを作らない。フィルタを 1 行も足さない (R0 / R6)。情報ログのみ |
| 解析結果が無い / 足りない | **ぼかす指定 (frame) は掛かる。**外す指定だけが効かない。書き出し前にその区間を解析する (§5.7)。解析できなければ「外せないまま (= ぼけたまま) 出力する」= 安全側 |
| 検出モデルが無い | 「このクリップをぼかす」は使える。外す指定だけできない旨を指定画面で知らせる |
| ReID モデルが無い | 情報ログのみ。IoU だけで追う |
| 輪郭モデルが無い | 情報ログのみ。角丸の矩形で外す (現行と同じ落とし方) |
| PIL が無い | マスクを作れない。**ぼかす指定があるのに素で出るため、確認を出す** (`blur_mask_failed` / 現行どおり) |
| `marks` が 1000 件を超えた | 警告ログ (プロジェクト JSON が太る / §5.2) |
| v3 の指定を読んだ | 情報ログ + 指定画面で 1 度だけ案内 (§3.8) |

---

## 6. 実装手順

| Phase | 内容 | できあがりの確認 |
| --- | --- | --- |
| 1 | `decisions.py` の書式 v4 (marks / target / scope)、旧版の読み替え。`config.py` の設定 | 単体テスト (§8.1 A) |
| 2 | `tracker.py` から統一を削除。`analyzer.py` / `store.py` を枠ベース + 部分解析へ | 単体テスト (§8.1 E) |
| 3 | `plan.py` を marks ベースへ (`marks_at` / 余裕の計算) | 単体テスト (§8.1 B) |
| 4 | `mask_builder.py` の合成を重ね塗りへ | 単体テスト (§8.1 C) + 目視 |
| 5 | `commands.py` の入れ替え | Undo/Redo が効く |
| 6 | `timeline_editor_dialog.py` の自動解析廃止 + クリップ右クリック | **この時点で「クリップをぼかすだけ」が通しで使える** |
| 7 | `blur_spec_dialog.py` の作り直し + オンデマンド解析 | 手動確認 (§8.3) |
| 8 | `blur_canvas.py` / `preview.py` / クリップの印 | 手動確認 |
| 9 | `region_tracker` / `manual_tracker` の範囲を scope へ | 単体テスト (§8.1 D) |
| 10 | `settings_window.py` / `setting.json` | 設定の往復 |
| 11 | 旧プロジェクトの移行 (§3.8) | §8.2 |
| 12 | 実素材での確認と実測 (§8.4)、既定値の調整 | — |

**Phase 6 で一度区切れる。**ここまでで「クリップを選んでぼかす」だけは完成し、
外す指定が無くても実用になる (全面ぼかしのクリップが作れる)。
Phase 7 以降は「外す」ための作業である。

---

## 7. setting.json 定義 (変更分)

```jsonc
"blur": {
  "enabled": false,
  // "default_policy" は廃止 (ver5 resolve7 §3.1)
  "preview_marker": true,
  "preview_blur": true,

  "model": {
    // reid は任意になった (無ければ IoU だけで追う / §3.6)
    "reid": "models/osnet_x0_25.onnx"
  },

  "analysis": {
    "sample_fps": 10.0,
    // 意味を変更: 「指定画面で表示中のクリップを自動で調べる」(Timeline 全体の自動解析は廃止)
    "auto_start": true,
    "min_track_sec": 0.6,
    "iou_threshold": 0.3,
    "embed_threshold": 0.35
    // "merge_threshold" 削除     (全体クラスタリングの廃止)
    // "max_identities" 削除      (同上)
    // "same_box_containment" / "same_box_center_ratio" 削除 (_appears_together の廃止)
  },

  "render": {
    "mode": "gaussian",
    "strength": 50,
    "margin_ratio": 0.06,             // ぼかす形の余白 (現行どおり)
    "feather_ratio": 0.006,           // ぼかす形のフェザー (現行どおり)
    "pad_sec": 0.2,                   // ぼかす形を前後へ伸ばす秒数 (現行どおり)
    "mask_scale": 0.25,
    "shape": "silhouette",

    // --- 外す形の余裕 (ver5 resolve7 §3.4) ---
    "keep_margin_ratio": 0.010,       // 0.0 → 0.010   キャンバス幅比の最低の太さ
    "keep_margin_box_ratio": 0.10,    // 新規          枠の短辺に対する太さ
    "keep_max_margin_box_ratio": 0.5, // 新規          太さの上限 (枠の長辺比)
    "keep_feather_ratio": 0.006,      // 新規          外した縁のなじませ量
    "keep_motion_margin": true,       // false → true  動いた量ぶん広げる
    "keep_hold_sec": 0.3              // 新規          枠が途切れても外したままにする秒数
  },

  "spec": {
    "hit_ratio": 0.5,
    "thumb_px": 96,
    "name_max_len": 32,
    "preview_mode": "blur",           // "blur" / "keep" / "none" ("mask" は "keep" へ読み替え)
    "show_overlays": true,
    "default_scope": "clip"           // 新規: "clip" / "section" / "media"
  }
}
```

範囲の丸め (`config.py`):

| キー | 範囲 | 範囲外のとき |
| --- | --- | --- |
| `render.keep_margin_ratio` | 0.0〜0.2 | 丸める |
| `render.keep_margin_box_ratio` | 0.0〜1.0 | 丸める |
| `render.keep_max_margin_box_ratio` | 0.0〜2.0 | 丸める |
| `render.keep_feather_ratio` | 0.0〜0.1 | 丸める |
| `render.keep_hold_sec` | 0.0〜5.0 | 丸める |
| `spec.default_scope` | `clip` / `section` / `media` | 既定 `clip` |

---

## 8. テスト計画

### 8.1 単体テスト (pytest / 既存の `tests/test_blur_*.py` に足す)

**A. 書式と scope (`test_blur_decisions_v4.py` 新規)**

| # | 内容 | 期待 |
| --- | --- | --- |
| A1 | v4 の `marks` を保存 → 読み戻し | 並び順を含めて一致する |
| A2 | `next_mark_id` は削除しても番号を戻さない | `m1` を消して足すと `m2` |
| A3 | `scope_for(..., SCOPE_CLIP)` | クリップの `source_in`〜`source_out` |
| A4 | `scope_for(..., SCOPE_SECTION)` (アーカイブ) | 同じ `archive_index` のクリップ群の min〜max |
| A5 | `scope_for(..., SCOPE_SECTION)` (クリップ用) | `collect_spans` の連続区間 |
| A6 | `scope_contains` の境界 | `start` / `end` ちょうどは含む |
| A7 | `merge_scopes` | 隣り合うクリップの区間が 1 件にまとまる |
| A8 | v3 の指定を読む | `regions` / `excluded` / アンカー付き `names` が残り、`identities` / `merges` / `splits` / `default_policy` が消える |
| A9 | `needs_mask` | ぼかす指定ゼロで false、1 件で true |
| A10 | `needs_analysis` | `frame` だけなら false、`track` があれば true |
| A11 | `analysis_spans` | 外す指定が効く区間だけ返る |

**B. 計画 (`test_blur_plan.py` を修正)**

| # | 内容 | 期待 |
| --- | --- | --- |
| B1 | scope 外の時刻では指定が効かない | `marks_at` が空 |
| B2 | scope 内の時刻では効く | 1 件返る |
| B3 | 同じ枠に 2 つの指定 (別クリップ) | クリップごとに別の結果 |
| B4 | 重ね順 | 後ろの指定が `marks_at` の後ろに来る |
| B5 | `_grow_keep` の上限 | `keep_max_margin_box_ratio × 長辺` を超えない |
| B6 | `_grow_keep` の下限 | フェザー量を下回らない |
| B7 | `keep_hold_sec` | 枠の終端 + 0.3 秒までは形が返る |
| B8 | 解析結果が無くても `frame` の指定は形を返す | 画面全体の矩形 |

**C. マスク (`test_blur_render.py` を修正)**

| # | 内容 | 期待 |
| --- | --- | --- |
| C1 | 指定なし | 全画素 0 |
| C2 | `frame` の `blur` 1 件 | 全画素 255 |
| C3 | scope 外の時刻 | 全画素 0 |
| C4 | `frame` + `keep` (枠 1 本) | 枠の中心が 0、画面の隅が 255 |
| C5 | `frame` + `keep` + `blur` (枠の中に小さく) | 小さい方の中心が 255 |
| C6 | 重ね順を入れ替える | C5 の結果が反転する |
| C7 | 余裕 | 輪郭の外 `grow` px の位置がまだ 0 (外れている) |
| C8 | フェザー | `grow` の外側で 0 → 255 へ連続的に上がる |
| C9 | 出力とプレビューの一致 | `mask_builder.frame_mask` と `preview.BlurPreview.mask` が同じ |

**D. 追従の範囲 (`test_blur_manual.py` に追加)**

| # | 内容 | 期待 |
| --- | --- | --- |
| D1 | scope が 1 クリップの領域 | そのクリップの区間だけトラックができる |
| D2 | scope を `media` へ広げる | 足りない区間が追い足される |

**E. 解析 (`test_blur_core.py` / `test_blur_cache.py` を修正)**

| # | 内容 | 期待 |
| --- | --- | --- |
| E1 | `analyze(..., spans=1 クリップ)` | その区間の枠だけできる |
| E2 | 続けて別のクリップを解析 | 前の枠が残り、`spans` が 2 件になる |
| E3 | 同じ区間を解析し直す | 古い枠が置き換わる (重複しない) |
| E4 | `settings_key` が変わる | 全部捨てて作り直す |
| E5 | 1 つの素材の由来キーだけ変わる | その素材の枠と区間だけ捨てる。他は残る |
| E6 | 戻り値に `identities` が無い | キーを持たない |
| E7 | `tracks[].identity` | 自分の枠 ID |
| E8 | `tracks[].thumb` | 見本画像が付く |
| E9 | ReID モデルが無い | 解析が成功する (IoU だけで追う) |

**削除するテスト**: `test_blur_assign.py` 全体、`test_blur_core.py` の `cluster` /
`apply_merges` / `_appears_together` / 主役に関するもの、`test_blur_names.py` の人物 ID による名前解決。

### 8.2 旧プロジェクトの移行

依頼者の `output/vod_2877176496.archive.timeline.json` (v3 の指定を持つ) を開き、

1. 落ちないこと
2. **ぼかしが掛からない状態**になること (§3.8 の想定どおり)
3. `regions` の形と `names` が残ること
4. 案内が 1 度だけ出ること
5. 保存して開き直すと v4 で読めること

### 8.3 手動確認

| # | 操作 | 期待 |
| --- | --- | --- |
| M1 | Timeline 編集画面を開く | **解析が走らない。**すぐ操作できる |
| M2 | クリップを右クリック →「このクリップをぼかす」 | クリップに印が付く。プレビューでそのクリップだけ全面ぼけ |
| M3 | 複数クリップを選んで同じ操作 | 選んだぶんだけ効く。隣り合うクリップは指定 1 件にまとまる |
| M4 | そのまま書き出す | **解析は走らない。**そのクリップだけぼけた動画が出る |
| M5 | 指定画面を開く | 対象クリップの区間だけ解析が走る (数秒〜十数秒) |
| M6 | 人物の枠をクリック →「外す」 | その人物だけ素で見える。**輪郭より一回り広く**外れている |
| M7 | 別のクリップへスクラブ | 「対象:」が変わり、M6 の指定は効いていない |
| M8 | 適用範囲を「この場面」にして外す | 同じ場面の他のクリップでも外れている |
| M9 | 外した範囲の中を囲んで「ぼかす」 | その部分だけまたぼける |
| M10 | 指定一覧で「下へ」 | M9 のぼかしが効かなくなる |
| M11 | 人物が一瞬遮蔽される所を再生 | 外れたままで点滅しない (R2-2) |
| M12 | Ctrl+Z | 指定が 1 つずつ戻る |
| M13 | ぼかしを 1 つも立てずに書き出す | FFmpeg のコマンドにぼかしのフィルタが 1 行も無い (R6) |

### 8.4 実測 (Phase 12)

見積もりで決めた値を確かめ、外れていれば §7 の既定を直す。

| 測るもの | 見積もり | 外れたときの手当て |
| --- | --- | --- |
| クリップ 1 本 (30 秒) の解析時間 | 9〜18 秒 (§3.3) | `sample_fps` を下げる / 進捗の見せ方を変える |
| マスク生成の 1 フレーム時間 (ぼかす + 外す 2 件) | 現行と同程度 | 層ごとのフェザーをまとめる / `mask_scale` を下げる |
| マスク動画の大きさ (ぼかすクリップ 5 分ぶん) | 数 MB 以下 | `mask_scale` を下げる |
| 書き出し全体の時間 | 現行と同程度 (§5.10) | — |
| 指定画面のスクラブ 1 枚 (全面ぼかし) | 30ms 以下 | §5.10 の縮小率を上げる |

### 8.5 回帰

* 機能 OFF (`blur.enabled = false`) でフィルタが 1 行も増えないこと
* **指定ゼロでもフィルタが 1 行も増えないこと** (R0 / 新規)
* ぼかし以外の全テストが通ること (現在 896 件)

---

## 9. 影響範囲・互換性

| 対象 | 影響 |
| --- | --- |
| `source.blur` (プロジェクト JSON) | **版が上がる (3 → 4)**。v4 を v1.5.0 以前で開くと指定が読めない (前方互換なし) |
| 解析キャッシュ (`*.blur.json`) | **版が上がる (3 → 4)**。既存のキャッシュは捨てられる。ただし新方式では**必要になるまで解析しない**ので、捨てても待たされない |
| 旧プロジェクトの出力 | **ぼかしが掛からなくなる** (§3.8)。開いたときに案内を出す |
| Timeline 編集画面を開く時間 | **短くなる** (全体解析が無くなる / §2.1) |
| 書き出し時間 | ぼかす指定が無ければ**現行より短い** (マスク生成が走らない)。指定があれば現行と同程度 (§5.10) |
| ぼかしを使わないプロジェクト | **影響なし + 速くなる** |
| ReID モデル | 任意になる。同梱は続ける (枠の継続に効くため) |
| `setting.json` | `default_policy` / `merge_threshold` / `max_identities` / `same_box_*` が無視される。既存ファイルはそのままで動く (未知のキーは読まないだけ) |
| ドキュメント | `docs/design/design.md` のぼかしの節を書き直す |

---

## 10. 確認事項

決めないと実装できない、または利用者の体験が変わるもの。
**推奨**を書いてあるので、異論が無ければそのまま進める。

| # | 論点 | 選択肢 | 推奨 |
| --- | --- | --- | --- |
| **1** | 「ぼかしが必要か」を機械が判定する補助を付けるか | (a) 付けない (利用者が決める) / (b) 解析したクリップについて「人物が N 人映っています」と出す / (c) 未解析のクリップも先読みして候補を出す | **(a)** をまず作り、(b) は解析済みのクリップで**ついでに出せる**ので Phase 7 で足す。(c) は初手の全体解析が復活するので採らない (§2.7) |
| **2** | 新しい指定の既定の適用範囲 | (a) このクリップ / (b) この場面 | **(a)** (要望④ の文字どおり)。ただし無音カットで場面が数十クリップに割れる運用では (b) の方が手数が少ない。設定 `spec.default_scope` でいつでも変えられる |
| **3** | 旧プロジェクトの `identities` の `blur` 指定を引き継ぐか | (a) 捨てて案内だけ / (b) その人物が映るクリップに「ぼかす」を立てる | **(a)**。(b) は「主役以外をぼかす」の暗黙分を再現できず、中途半端に一部だけぼけた状態になる。作り直してもらう方が安全 |
| **4** | 人物一覧・主役・統合・分割・割り当てをすべて落としてよいか | (a) 落とす / (b) 一部残す | **(a)**。すべて「人物へまとめる」ことが前提の機能で、統一を外すと定義できない |
| **5** | 外すときの余裕の既定値 | 画面幅の 1.0% + 枠の短辺の 10% | **この値で作り、§8.4 の実素材で調整する**。背景が見えすぎる場合は `keep_margin_box_ratio` を 0.05 へ |
| **6** | 「このクリップをぼかす」を外したとき、上に載っている「外す」指定をどうするか | (a) 残す (また入れれば効く) / (b) 一緒に消す | **(a)**。誤操作で作業が消えない方を選ぶ。一覧には残るので、要らなければ個別に消せる |
| **7** | `blur.analysis.auto_start` の既定 | (a) true (指定画面で表示中のクリップを自動で調べる) / (b) false (ボタンを押したときだけ) | **(a)**。対象がクリップ 1 本に縮んだので、自動でも待ち時間が短い。遅い環境向けに false も残す |
| **8** | 解析が間に合わないまま書き出したとき | (a) 書き出し前にその区間を解析する / (b) 外す指定を無視してぼかしたまま出す | **(a)**。ただし (a) が失敗したら (b) へ落ちる = **外し損ねてぼけたまま出る**。逆 (ぼかし損ねて素で出る) は起きない |

### #1 の詳細 — なぜ自動判定を採らないか

「ぼかしが必要か」を機械に言わせるには、**人物が映っているかを調べる = 検出を走らせる**しかない。
それは追加指示が否定した「初手の処理」そのものである。

しかも「映っている = ぼかすべき」ではない。
配信者本人・許可を得た出演者は映っていてもぼかさない。
**必要かどうかの基準は動画の外にある**ため、利用者が決めるしかない。

一方、**ぼかすと決めたクリップを解析したついでに**「このクリップには枠が N 本あります」と
出すことはできる (費用ゼロ)。これは §10 #1 (b) として Phase 7 で足す。

### #6 の詳細 — 外す指定が宙に浮く場合

「このクリップをぼかす」を外すと、その上に載っていた「外す」指定は
**効かないまま一覧に残る** (ぼかしが無いので外すものが無い)。
一覧では淡い色で「(ぼかしが掛かっていないため効いていません)」と添える。
また入れれば元どおり効く。

### #8 の詳細 — 安全側がどちらへ倒れるか

v3 までは「ぼかす対象が分からない = 何もぼかさない」だった。
そのため `mask_builder.prepare` は解析結果が無いと `blur_mask_failed` を立て、
**利用者へ確認を出してから**出力していた (resolve2 §5.9)。

v4 では「ぼかす」と「外す」が別々の指定になったため、**失敗の向きが変わる**。

| 失敗 | 結果 |
| --- | --- |
| 解析できない | 外す指定が効かない = **ぼけたまま出る** (安全) |
| 枠が取れない時刻がある | その時刻は外れない = **ぼけたまま出る** (安全) |
| PIL が無い | マスクそのものを作れない = **素で出る** (危険 / ここだけ確認を出す) |

つまり、確認を出す必要があるのは **PIL が無い場合だけ**になる。
