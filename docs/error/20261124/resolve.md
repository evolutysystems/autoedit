# AI 編集が最後の検証で全部巻き戻って停止する 原因と修正設計書

現象の報告: `docs/error/20261124/error.md`

本書は `docs/CLAUDE.md`「実装前に設計を行うこと」に従う **設計書** である。

対象ログ: `D:\develop\autoedit\src\gui\logs\autoedit_20261124.log`
対象 VOD: Twitch `2938439428` (`%LOCALAPPDATA%\Stretheus\archive_work`)

---

## 1. 結論（先に要点）

**クリップの最小尺の下限が「作る側」と「検証する側」で食い違っている。**

| 場所 | 閾値 | 役割 |
|---|---|---|
| `src/modules/silence_cutter.py:97` `build_keep_segments` | `> 0.01` | 残す区間 (編集点) の採用 |
| `src/archive/timeline_builder.py:45` `_MIN_SEGMENT_SEC` | `0.01` | V1 クリップの生成 (アーカイブ) |
| `src/timeline/builder.py:42` `_MIN_SEGMENT_SEC` | `0.01` | V1 クリップの生成 (1 本編集) |
| `src/timeline/project_io.py:450` `_validate_clips` | `< 0.05` で**削除** | 復元・保存時の検証 |

下限は `timeline.min_clip_sec` = **0.05** 秒 (`src/settings/setting.json:277` /
`settings_window.py:759`)。つまり **0.01 〜 0.05 秒の区間は「クリップとして作られる」が
「検証で必ず消される」**。今回の `c33` = 0.031 秒がちょうどこの穴に落ちた。

そして消えたことが**致命扱いになる**。`src/ai/plan_applier.py` の
`_validate_timeline` は「validate でクリップが 1 本でも消えたら plan の適用で
壊れた形になった」と判定し、積んだ手を全て undo して `AiPlanError` を投げる。

```python
# plan_applier.py (現状のコメント)
# project_io.validate は「直して続ける」実装であり例外を投げない。
# そのため**クリップが消えたか**を見て判定する。消えたなら plan の適用で
# 壊れた形になったということなので、積んだ手を全て戻して失敗にする。
```

この前提が**「元から下限を割っていたクリップ」には成立しない**。
`c33` は plan が一度も触っていないクリップであり、plan の有無にかかわらず
初回の validate で消える。それを「plan が壊した」と読んだため、
37 分かけた処理が丸ごと捨てられた。

## 2. 証拠

### 2.1 消えたのは 2 本で、どちらも plan と無関係

```
[WARNING] project_io: クリップ c33 の尺 (0.031s) が下限 (0.050s) 未満のため除外します
[WARNING] project_io: 音声クリップ a33 のリンク先 c33 が存在しないため除外します
[INFO]    plan_validator: … V15 timeline drop=2
```

`a33` は `c33` が消えた結果として `_validate_audio_links` が落としたものである。
実体は `c33` 1 本。

### 2.2 リップル削除が原因ではない

同じ時刻にリップル削除が走っている。

```
[INFO] src.timeline.commands: リップル: 76.238〜77.313s (1.07s) を除去 / 削除 6 件・短縮 2 件・移動 212 件
```

しかしこれは `c33` を短くしていない。根拠は 2 つある。

1. `commands.py` の `ripple_remove_range` は**自分が触ったクリップについては
   `min_clip_sec` 未満を自分で落とす**。ログの「削除 6 件」がそれである。
   仮に `c33` がこのリップルで 0.031 秒になったのなら、ここで消えて
   `project_io` 側の WARNING は出ない。
2. 同関数は末尾で `_drop_orphan_audio_clips` を呼ぶ。
   `c33` がここで消えていれば `a33` も同時に落ち、
   `project_io` の「リンク先が存在しない」WARNING も出ない。

両方の WARNING が `project_io` から出ている事実が、
**`c33` は Timeline 構築の時点で既に 0.031 秒であり、リップルは触っていない**
ことを示している。

### 2.3 0.031 秒がどこで生まれたか

```
[INFO] src.modules.silence_cutter: 編集点検出: 無音 34 件 / 残す区間 34 件 (閾値=-36dB, 最小無音=0.60s, 総尺=182.1s)
[INFO] src.archive.timeline_builder: アーカイブ Timeline 構築: 4 クリップ / V1 160 本 / S1 135 件 / 全長 356.4s
```

`detect_edit_points` → `build_keep_segments` の `> 0.01` を通った
0.031 秒の有音区間が、`build_archive_timeline` の
`<= _MIN_SEGMENT_SEC` (= 0.01) も通り、V1 の 1 本になった。

### 2.4 構築と適用のあいだに validate が 1 度も走らない

`build_archive_timeline` は validate を呼ばない。`ai_runner.run_plan` →
`apply_plan` の `_validate_timeline` が**この Timeline に対する最初の validate**
である。そのため「元からあった不正」と「plan が作った不正」が区別されないまま
前者が致命として扱われた。

### 2.5 AI 側の 503 との関係

AI 呼び出しは 4 回すべて 503 で、plan はローカル縮退で組まれた
(`P4 video_plan drop=ai_unavailable default=local` ほか / `error.md` 参照)。
縮退 plan が `A4 scenes[1] cut=after:76.238` を出し、そのリップルが §2.2 の
ログを生んでいる。**503 は V15 の失敗の原因ではない**が、
「AI が何もしていない実行でも 37 分を使い切って失敗する」という最悪の形に
重なった。503 の再試行・待ちの設計は本書の範囲外とする (§7 P1)。

## 3. 影響範囲

| 経路 | 影響 |
|---|---|
| アーカイブ AI 編集 (`ai_runner` → `apply_plan`) | **停止する**。今回の現象 |
| アーカイブ手動編集 (Timeline 画面) | 停止しない。保存・復元時の validate が `c33` を黙って落とすだけ (該当 0.031 秒が無音になる) |
| 1 本編集 (`pipeline_runner` → `timeline.builder`) | 同じ下限の食い違いがあるため同じクリップが作られる。AI 編集を通さない限り停止はしない |
| 実カット (`silence_cutter.run`) | クリップを作らないため無関係 |

0.01〜0.05 秒の有音区間は「無音最小継続時間 0.60 秒」の無音に挟まれた
極短の発話・物音で起こる。**特定の VOD に固有の事故ではなく、
いつでも再発する**。

## 4. 修正方針

### R1. 下限を `timeline.min_clip_sec` へ一本化する（作る側を直す・本丸）

**下限は「編集点を作るところ」1 か所で掛ける。** 編集点がクリップの素になるため、
ここで落としておけば、以降 (認識・V1 の積み上げ・TimeMap) が全て同じ区間列を
見ることになり、どこもずれない。

| ファイル | 変更 |
|---|---|
| `src/modules/silence_cutter.py` | `build_keep_segments(silence_ranges, total_duration, min_segment_sec=_MIN_KEEP_SEC)` に引数を追加 (既定は従来の 0.01 = 既存の呼び出しは不変)。`detect_edit_points` が `timeline_min_clip_sec(settings)` を渡す。落とした件数をログへ |

**builder 側 (`timeline/builder.py` / `archive/timeline_builder.py`) では掛けない。**
当初は「V1 クリップの採用も `min_clip_sec` で判定する」設計にしたが、
実装して確かめた結果これは**新しい不具合になる**ので採らない (理由は §4 R4)。
代わりに、下限をここで見てはいけない理由をコメントとして両方の該当箇所へ残す。

`_MIN_SEGMENT_SEC` (0.01) は**残す**。字幕の打ち切り・区間の差分計算など
「ほぼ 0 か」を見るだけの用途に使われており、クリップの下限とは意味が違う。
ここを動かすとテロップの打ち切り規則
(`docs/error/20260812/Analyze.md §9-1`) まで変わる。

落としたときはログに残す。今回は映像とクリップ id から逆算するしかなかった。

```
下限 (0.050s) 未満のため編集点にしなかった区間: 1 件
```

### R2. 「元からあった不正」を plan の失敗にしない（検証側を直す・安全網）

`apply_plan` の入口で、**plan を適用する前に**「validate が落とすクリップ」を
調べておき、`_validate_timeline` の判定からそれを除く。

* `src/timeline/project_io.py` に `invalid_clip_ids(timeline, min_clip_sec)` を新設する。
  **Timeline を変えずに**、`_validate_clips` / `_validate_audio_links` と
  同一の規則で「落ちるクリップの id」を返す。
  判定の規則が 2 つに分かれないよう、`_validate_clips` の条件は
  `_clip_drop_reason()` へ切り出して両者で共有する。
* `apply_plan` は `validate=True` のときだけこれを呼び、`state["pre_invalid"]` に持つ。
* `_validate_timeline` は `before = _clip_ids(timeline) - state["pre_invalid"]` とし、
  **plan のせいで消えたクリップだけ**を致命と判定する。
* 元からあった不正は `report.add("V15", "timeline.before", ACTION_DROP, 件数)` として
  記録に残す (致命ではないが見えるようにする)。

R1 を入れれば `c33` はそもそも生まれないが、R2 は

* すでに保存済みのプロジェクト (下限未満のクリップを含む `.timeline.json`)
* Timeline 画面で手編集した結果

にも効く。**37 分の処理が既存クリップ 1 本で全損する構造そのもの**を直すのが R2 である。

### R3. なぜ「適用前に validate を 1 回走らせる」のではないのか

一見こちらが簡単に見えるが採らない。

* `project_io.validate` は Timeline を**書き換える**。適用前に走らせると、
  落ちたクリップはコマンドスタックに載らないため**巻き戻しても戻らない**。
  本当に plan が壊したときの復旧 (`test_a_loss_at_validation_rolls_back` が
  守っている性質) が崩れる。
* 下限未満のクリップを消すと V1 に 0.031 秒の隙間が残る。R1 で最初から
  作らないほうが形がきれいである。

したがって「変えずに調べる」`invalid_clip_ids` を置く。

### R4. 採らなかった案

| 案 | 却下理由 |
|---|---|
| `timeline.min_clip_sec` を 0.01 へ下げる | 0.031 秒のクリップが書き出しまで残る。60fps で 2 フレーム未満であり、トリム操作の下限としても小さすぎる。`min_clip_sec` は「これ未満へはトリムできない」値 (`settings_window.py:759`) であって、事故を避けるために動かす値ではない |
| `_MIN_SEGMENT_SEC` 自体を `min_clip_sec` に置き換える | §R1 のとおり用途が違う。テロップの打ち切り規則まで変わり `docs/error/20260812` の決定を壊す |
| **builder (`build` / `build_archive_timeline`) で下限未満の区間を落とす** | **字幕がずれる。**`keep_segments` (= `_segments_of` の戻り値) は V1 の積み上げだけでなく `_append_subtitles` / `append_subtitles` の `TimeMap.from_segments` にもそのまま渡っている。クリップ側だけ区間を落とすと V1 の軸が 0.031 秒詰まり、認識済み item の時刻 (落とす前の軸で作られている) と食い違う。**`docs/error/20260811` / `20260812` のテロップずれを作り直すことになる**。<br>対して現状の「クリップを作って検証で消す」は、消えた跡が**隙間として残り**軸が動かないため、字幕はずれない。古いプロジェクトを開いた場合に残る 0.031 秒の隙間は、軸のずれよりはるかに軽い |
| `_validate_timeline` を「消えても続ける」に変える | plan が本当に Timeline を壊した場合 (重なり・ソース範囲の破壊) を見逃す。巻き戻しの仕組み自体は正しいので、**判定の基準**だけを直す (R2) |
| 下限未満の区間を隣の区間へ繋いで救う | 無音を 0.031 秒ぶん残すことになり、無音カットの意味が崩れる。区間の統合は `cut_min_silence_sec` の仕事であり、下限の判定に混ぜない |
| `silence_cutter.run` (実カット) 側も `min_clip_sec` で切る | 実カットはクリップを作らず、下限は無関係。出力動画の尺が最大 0.05 秒/区間ぶん変わるだけの変更になり、既存の仕上がりを壊す |

## 5. 変更するファイル

| ファイル | 変更 |
|---|---|
| `src/modules/silence_cutter.py` | `build_keep_segments` に `min_segment_sec` を追加 / `timeline_min_clip_sec()` を新設 / `detect_edit_points` が下限を渡し、落とした件数をログへ |
| `src/archive/timeline_builder.py` | **コメントのみ**。`_MIN_SEGMENT_SEC` は下限ではないこと、ここで下限を掛けると字幕がずれること (§4 R4) を残す |
| `src/timeline/builder.py` | **コメントのみ**。同上 |
| `src/timeline/project_io.py` | `invalid_clip_ids()` を新設 / `_validate_clips` の条件を `_clip_drop_reason()` へ切り出して共有 |
| `src/ai/plan_applier.py` | `apply_plan` が適用前の不正を `state["pre_invalid"]` へ控える / `_validate_timeline` の判定から除き、記録へ残す |
| `tests/test_error_20261124_min_clip_sec.py` | **新規**。下記 |

設定の追加は無い (`timeline.min_clip_sec` は既存)。

## 6. テスト

| # | 確認 |
|---|---|
| T1 | `build_keep_segments` の既定は従来どおり 0.01 秒で切る (既存呼び出しの不変) |
| T2 | `build_keep_segments(min_segment_sec=0.05)` は 0.031 秒の区間を落とす |
| T3 | `detect_edit_points` が `timeline.min_clip_sec` を下限として使う |
| T4 | 下限を掛けた編集点から組んだ Timeline は `project_io.validate` で 1 本も消えない (作る側と検証側の一致) |
| T5 | `build_archive_timeline` は下限で区間を落とさない = 軸を詰めない (§4 R4 の保証) |
| T6 | `timeline.builder` も同様に軸を詰めない |
| T7 | `invalid_clip_ids` は下限未満のクリップとそのリンク音声を返し、**Timeline を変えない** |
| T8 | 下限未満のクリップを含む Timeline に `apply_plan` しても `AiPlanError` にならず、plan が適用される (本件の再現と修正の確認) |
| T9 | plan が本当に壊した場合は従来どおり巻き戻して `AiPlanError` (既存 `test_a_loss_at_validation_rolls_back` が通ること) |

## 7. 設計だけ（実装は保留）

| 項目 | 内容 |
|---|---|
| P1 | Stretheus API の 503 に対する再試行・待ち。`scene_eval` が 9 件中 0 件でも 37 分ぶんの処理を続ける現状は、縮退としては正しいが利用者にとって得にならない。「AI が全滅したら開始前に知らせる / 中止を選ばせる」までを含めて別途設計する |
| P2 | `V15` で失敗したときの案内文。現在は「元の状態へ戻しました」だけで、何が原因か分からない。消えたクリップ id と理由を画面へ出す |
