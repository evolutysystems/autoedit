# AiEditor resolve3 — 採点を AI の点で行う (経路 D の採点モード)

前提: `docs/AiEditor/resolve2.md` (特に §6.1 / §14.0 / §14.2)

利用者の指示 (2026-11-26):

> AI を有効にしている場合は採点そのものを AI にやらせてください。それでテストしてみたい。
>
> 全体の流れとして以下のようにしてください。`scoring.html` の「全体の流れ」を参照してください。
> AI のフローに関しては ' を付けます。（例：①'）
> **①' ① ② ②' ④ 候補セクション ③'**
> 現状呼ばれていませんが、実行するようにしてください。
>
> でも機械式の処理は残しておいてください。削除しないでね、あとで使うかもしれないから

番号は `StretheusPlan/scoring.html` §01 の図に対応する。

| 番号 | 工程 |
|---|---|
| ① | 全長 1 パス音声解析 (`archive/features.scan_audio`) |
| ② | セル特徴 45 秒 (`archive/features.extract_cells`) |
| ③ | 窓の採点 = 方式 A (`archive/scoring.score_window`) |
| ④ | セクション選択 (`archive/scoring.select_top_events`) |
| ①' | 採点スイープ sceneScan (`ai/scene_scanner.scan`) |
| ②' | シーン評価 sceneEval (`ai/scene_finder.evaluate`) |
| ③' | 構成・レビュー (`ai/planner` + `ai/reviewer`) |

---

## 0. 先に結論

1. **方式 A (③) は消さない。** 窓の採点はこれまでどおり全窓に対して走らせ、
   **`total` だけを AI の点へ差し替える**。`emotion` / `comment` は機械の値のまま残す。
   指示の並びに ③ が無いのは「機械の点で順位を決めない」という意味として実装し、
   計算そのものは残す (利用者の追加指示「機械式の処理は残しておいてください」)。
2. **採点モードを設定で持つ** (`ai.scan.scoring_mode`)。
   `ai_only` (今回の指示 / 既定) / `blend` (resolve2 §6.1 の混合) / `machine` (従来)。
   **1 行も削らずに前の挙動へ戻せる**ことを設定で保証する。
3. **①' をアプリで実際に呼ぶ。** 呼び出し口は GUI の採点 (`archive_tab._on_analyze`)。
   resolve2 §14.0 は「経路 D では採点が実際に混ざる」と書いたが、
   GUI が採点を済ませてから `run_ai(analysis=...)` へ渡すため**一度も呼ばれていなかった** (§1)。
4. **②' を有効化する** (`ai.scene_eval.enabled = true`)。ただし位置は**候補セクションの後**とする。
   文字起こしが要るためで、指示の並びから唯一ずらす点である (§3.3)。
5. **同意文面を版 2 へ上げる。** 全長から静止画を送るようになるため (resolve2 §10)。
6. **経路 B (AI 無効) は従来と完全に同じ。** `scan` を渡さず、`total` は方式 A のまま。

---

## 1. なぜ ①' は呼ばれていなかったか

| 場所 | 現状 |
|---|---|
| `gui/archive_tab.py:150` | `pipeline.analyze(input_path, settings, ...)` を **`scan=` 無し**で呼ぶ |
| `gui/archive_tab.py:399` | その結果を `ai_runner.run_ai(analysis=result)` へ渡す |
| `pipeline/ai_runner.py:380` | `if analysis is None:` のときだけ `_scan_runner()` を作る → **常に素通り** |

`ai.scan.enabled = true` でも、`ai.scan.ai_weight = 0.3` でも結果は変わらない。
2026-11-25 の実測 (resolve2 §14.1 / §14.2) は `tools/ai_scan_probe.py` 経由であり、
**アプリ本体では一度も走っていない**。2026-11-26 のログ (`AI の山 0 件`) がその証拠である。

したがって本件の主作業は新規実装ではなく**呼び出しの追加**である。

---

## 2. なぜ方式 A を残すのか

resolve2 §2 案 A (既存採点を捨てる) の却下理由はそのまま有効である。

| 残す理由 | 中身 |
|---|---|
| `audio_scan` の供給元 | 無音カットの閾値・ラウドネス・書き出しが使う (①② は消せない) |
| 採点グラフの内訳 | `emotion` / `comment` の 2 本は画面が読む。AI の点はこれを作れない |
| `best_window_for_range` | セクション追加とマーカー由来セクションが「重なる窓の最大 total」を使う。窓の形が変わらなければ規則の作り直しが要らない |
| 縮退先 | AI が落ちた日・同意しない日・AI 無効の日は `total` が方式 A のまま = 現状と同じ |
| 同点の解き先 | 山は実測で全長の 3.3% しかない。山に重ならない窓は AI の点が 0 で全部同点になる (§3.2) |

**`total` だけを差し替える**設計にすると、上記すべてが自動的に満たされる。
resolve2 §6.1 の `blend_ai_moments` が既にその形 (`total` だけ混ぜ、`emotion`/`comment` は残す) であり、
本件はその隣に `ai_only` 用の関数を 1 つ足すだけで済む。

---

## 3. 設計

### 3.1 流れ (ai_only モード / AI 有効時)

```
①  全長 1 パス解析            features.scan_audio
②  セル特徴 45 秒 × N         features.extract_cells
③  窓の採点 (方式 A)          scoring.score_window      ← 残す。machine_total として保持
①' 採点スイープ               scene_scanner.scan        ← 静止画 + 音量 + コメント → 山
    total = AI の点            scoring.score_by_ai_moments
④  セクション選択             scoring.select_top_events ← AI の点で貪欲。同点は machine_total で解く
    候補セクション
②' シーン評価                 scene_finder.evaluate     ← 候補 + 文字起こしで 8 軸 → 並べ替え
③' 構成・レビュー             planner / reviewer        → EditPlan
```

### 3.2 `score_by_ai_moments()` (新規 / `archive/scoring.py`)

`blend_ai_moments()` を `ai_weight = 1.0` で呼ぶのでは**解像度が出ない**。
resolve2 §14.2 の実測どおり「山に重なる窓が全て同点になる」ためである。
重なりの深さで差を付ける。

```
重なり率 = 重なり秒 / min(窓長, 山長)        (0..1 へ丸める)
total    = 山の scan_score × 重なり率
```

* 山を丸ごと含む窓は満点 (`scan_score`)、端をかすめる窓は比例して低い
* 重なる山が複数あるときは**最も点の高い 1 件**を採る (`_best_moment` と同じ規則)
* 山に重ならない窓は `total = 0.0`
* `emotion` / `comment` は**機械の値のまま**。`ai_score` / `moment_index` / `machine_total` を足す

`machine_total` は `blend_ai_moments()` にも足す (どちらのモードでも方式 A の点を失わない)。

### 3.3 ②' の位置 — 指示からずらす 1 点

指示の並びは `②' → ④` だが、実装は `④ → ②'` とする。

| | 入力 | 目的 |
|---|---|---|
| ①' sceneScan | 静止画 16 枚 + 音量 + コメント | 山を**見つける** (再現率) |
| ②' sceneEval | **文字起こし** + 数値 | 候補を**選ぶ** (適合率) |

文字起こしは「候補が決まってから候補区間だけに行う」(resolve2 §0-6。全長 Whisper は CPU で 1 時間級)。
②' を ④ の前へ出すと文字起こしが無く、①' と同じ材料で点を付け直すだけになり、
`docs/error/20261125c/resolve.md` §2.6 で切った理由 (役割の重複) がそのまま復活する。
よって ②' は候補セクションの後に置く。**「AI が点を付ける」という指示の意図は満たしている。**

### 3.4 ①' の呼び出し (`gui/archive_tab.py`)

* `_on_analyze()` (GUI スレッド) で使用可否を決める
  * `ai.enabled` かつ `ai.scan.enabled` のときだけ対象
  * 同意は**ここで取る** (`_ensure_ai_consent()`)。ワーカースレッドからダイアログを出さない
  * 同意しなければスイープを使わない = 方式 A のまま進む (採点自体は止めない)
* `ArchiveAnalyzeWorker` に `use_ai_scan` を渡し、ワーカー側で
  `provider` と `UsageTracker` を作って `pipeline.analyze(scan=...)` へ渡す
* 利用量はワーカー終了時に必ずログへ残す (`tracker.log_summary()`)

経路 B しか使わない利用者が `ai.enabled = false` のままなら、**ダイアログも課金も発生しない**。

### 3.5 `pipeline.analyze()` の分岐

```python
moments = _run_scan(...)                       # 失敗しても [] (従来どおり)
if moments:
    mode = scoring_mode(settings)              # ai.scan.scoring_mode
    if mode == MODE_AI_ONLY:
        curve = scoring.score_by_ai_moments(curve, moments)
    elif mode == MODE_BLEND:
        curve = scoring.blend_ai_moments(curve, moments, ai_scan_weight(settings))
    # MODE_MACHINE は何もしない (山は moments として後段へ渡るだけ)
```

`moments` が空のときは**どのモードでも方式 A のまま**。これが縮退である。

---

## 4. 設定 (`setting.json`)

| キー | 既定 | 意味 |
|---|---|---|
| `ai.scan.scoring_mode` | `"ai_only"` | **新規**。`ai_only` / `blend` / `machine` |
| `ai.scan.ai_weight` | `0.5` | `blend` のときの混合比 (0.3 → 0.5 / 利用者の指示 2026-11-26) |
| `ai.scene_eval.enabled` | `true` | `false` → `true` (②' を流れへ戻す) |
| `ai.consent.accepted_version` | — | 文面の版が 2 になるため**再同意が 1 回出る** |

`ai_weight` の既定は `ai_only` では使われないが、`blend` へ戻したときに効く。
0.5 は resolve2 §14.2 の実測で「上位 10 のうち 8 枠が山 2 件の近傍の重複窓」になる値であり、
**1 本での判断**という §14.2 の注意書きはそのまま有効である。

> **スイープ代を止める鍵は `scoring_mode` ではなく `ai.scan.enabled` である。**
> `machine` にしてもスイープ自体は走る (山は構成へ渡すため)。
> 時間と費用ごと止めたいときは `ai.scan.enabled = false`、
> または `ai.enabled = false` にする。

---

## 5. 懸念 (テストで確かめること)

1. **素材が枯れないか。** 山は実測で全長の 3.3% (216 秒 / 2 件)。`ai_only` では山に
   重ならない窓が全部 0 点になり、順位は「山の近傍 → あとは `machine_total` 順」になる。
   長尺 (180〜900 秒) の候補が足りるかを 1 本流して見る。
2. **誤検出が本当に落ちるか。** resolve2 §14.2 では `0-180` / `45-225` (エンジン音の飽和) が
   6 位・9 位へ落ちた。`ai_only` では 0 点になるため、さらに下へ落ちる見込み。
3. **採点グラフが読めるか。** 折れ線は AI の点 (山の近傍だけ盛り上がり、他は 0) になる。
   内訳の `emotion` / `comment` は機械の値のままなので、グラフ上で乖離して見える。
4. **時間と費用。** 採点 1 回あたり +約 3.6 分 / $0.15 (resolve2 §14.2 の実測)。
   採点をやり直すたびに発生する。

---

## 6. 影響範囲

| ファイル | 変更 |
|---|---|
| `src/archive/scoring.py` | `score_by_ai_moments()` 追加 / `machine_total` の保持 / `select_top_events` の同点解き |
| `src/archive/pipeline.py` | `scoring_mode()` 追加 / `analyze()` のモード分岐 |
| `src/gui/archive_tab.py` | `_on_analyze()` で同意と可否を決める / `ArchiveAnalyzeWorker` がスイープを渡す |
| `src/ai/consent.py` | 文面に「配信全体の静止画」を追加 / `CURRENT_VERSION` 1 → 2 |
| `src/ai/config.py` | `scoring_mode` の既定 / `ai_weight` 0.5 / `scene_eval.enabled` true |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` を同じ値へ |
| `src/settings/setting.json` | 同じ値へ |

**削除は無い。** 方式 A (`score_window` / `loudness_norm` / `loud_threshold` / `_weighted_total`) と
`blend_ai_moments()` はそのまま残り、設定だけで呼び分かれる。

---

## 7. テスト

| 観点 | 内容 |
|---|---|
| `score_by_ai_moments` | 山を丸ごと含む窓 = `scan_score` / 端をかすめる窓 = 比例 / 重ならない窓 = 0 / `emotion`・`comment` 不変 |
| 複数の山 | 重なる山が 2 件なら点の高い方を採る |
| 同点の解き | `select_top_events` が 0 点同士を `machine_total` の降順で並べる |
| モード分岐 | `machine` は山があっても `total` を変えない / `moments` が空ならどのモードでも方式 A |
| 既存互換 | `scan` を渡さない呼び出し (経路 B) の結果が従来と一致する |

---

## 8. 未決

| | 内容 |
|---|---|
| U1 | 採点のやり直しごとにスイープ代が出る。山を `<vod>.scan.json` へ保存して再利用するかは 1 本流してから決める |
| U2 | `ai_only` で長尺の素材が枯れるなら、`blend` (0.5) へ戻すか山の `max_moment_sec` を広げるかの二択になる |
| U3 | 採点グラフに AI の点と機械の点を**両方**描くか (今回は触らない) |

---

## 9. 追補 — 尺の基準を「出力尺」に統一し、候補を尺で積む (2026-11-26)

利用者の指示:

> 実行してみたところほとんどクリップが無く 1 分半のアーカイブ切り抜きになってしまいました。
> 値を設定するというよりは、14 分以上のアーカイブ切り抜きになるように、
> 山の点数の上からクリップを選択していって、14 分以上になったら選択を終了する形でお願いします。

### 9.1 1 分半になった原因は ④ ではなく ③' だった

実測 (VOD 2939293108 / 11247 秒 / 山 1 件):

| 工程 | 結果 |
|---|---|
| ④ 候補 | 6 セクション / **素材 1800 秒** — 十分に集まっている |
| ③' 構成 | 保存された EditPlan は 4 シーン / **素材 192.3 秒** |
| 出力 | **102.1 秒 (1 分 42 秒)** |

`ai.plan.youtube_long.min_sec = 180` を**素材秒**で満たした (192.3 ≥ 180) ため、
AI は目標達成と判断した。無音カットで約半分になり出力は 102 秒になった。

レビューの機械検査は尺不足を見ていたが `minor` だったため、
`ai.review.min_severity = major` に弾かれて誰も直さなかった。

**つまり ④ をいくら積んでも、③' が素材秒で満足してしまう限り短い切り抜きになる。**

### 9.2 決めたこと

1. **`min_sec` / `max_sec` の意味を「出力尺」(無音カット後の完成尺) に統一する。**
   素材尺への換算は `ai/config.source_sec(output_sec, kept_ratio)` が 1 か所で行う。
   「14 分」という数字は `ai.plan.youtube_long.min_sec = 840` の**1 か所だけ**に置く。
2. **残存率を解析から見積もる。** `archive/pipeline.kept_ratio(cells)` が
   セルの `silence_ratio` から出し、`analyze()` の戻り値と `AnalysisBundle` を通じて
   構成・レビュー・検証へ運ぶ。実測は素材 840s → 残す 430.6s (51%)。
3. **候補は尺で積む。** `select_top_events(..., target_total_sec=...)` が
   点の高い順に積み、目標に達したら終了する。`top_n` は使わない。
   目標 = 最長プリセットの `min_sec` ÷ 残存率 × `archive.scoring.target_headroom` (既定 2.0)。
   余裕を掛けるのは ③' に「選ぶ」余地を残すため。
4. **尺の下限未達を `major` にする。** `minor` では `min_severity` に弾かれて是正されない。

### 9.3 換算が入る場所

| 場所 | 何を換算するか |
|---|---|
| `ai/planner._limits()` | AI へ渡す `limits.min_sec` / `max_sec` (プロンプトの「合計の尺を収める」の基準) |
| `ai/reviewer._target_limits()` | レビューの payload とレビューの機械検査 |
| `ai/plan_validator._limits()` | V10 (合計尺の上限で後ろの body から落とす) |
| `archive/pipeline.candidate_target_sec()` | ④ が集める素材尺の目標 |

`kept_ratio = 1.0` を渡せば換算されない = 従来どおり素材尺として扱う
(手書きの EditPlan やテストの経路)。

### 9.4 期待する値 (VOD 2939293108 の条件で)

```
残存率            ≒ 0.5
候補の目標素材尺  = 840 ÷ 0.5 × 2.0 = 3360s   (窓 180s なら約 19 本)
構成へ渡す尺      = min 840 ÷ 0.5 = 1680s / max 1200 ÷ 0.5 = 2400s (素材)
出力の見込み      = 1680〜2400s × 0.5 = 840〜1200s = 14〜20 分
```

### 9.5 設定

| キー | 変更 | 意味 |
|---|---|---|
| `ai.plan.youtube_long.min_sec` | 180 → **840** | **出力尺**の下限 = 14 分 |
| `ai.plan.youtube_long.max_sec` | 900 → **1200** | 出力尺の上限 = 20 分 |
| `ai.plan.youtube_shorts/tiktok` | 15 / 60 のまま | 意味が出力尺になったため、以前より正しく 15〜60 秒へ収まる |
| `archive.scoring.target_headroom` | **新規 2.0** | 候補を集めるときの余裕 |

`max_scenes = 12` は据え置き。統合後のセクションは 180〜540 秒になるため
(実測: clip2 が 540 秒)、12 シーンで素材 1680 秒は組める。

### 9.6 残る懸念

| | 内容 |
|---|---|
| A | 残存率は `silence_ratio` (-30dB 基準) からの**見積り**。実カットはクリップごとの音量解析 (実測 -14〜-37dB) で決まるため、見積りが甘いと出力が目標を下回る。1 本流して `候補の積み上げ` と最終尺のログを突き合わせる |
| B | 候補が増えるぶん `ai_prepare` (クリップごとの Whisper) が伸びる。実測は 11 シーンで 11 分 |
| C | 山が 1〜2 件しか出ない間は、積み上げの大半が `machine_total` 順になる。「AI が採点」と呼べる比率を上げるには §5 の 3 案 (スイープの歩留まり改善) が必要 |
