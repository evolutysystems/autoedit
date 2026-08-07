# resolve7: 音量解析による単一閾値化と確認ダイアログ（無音カット前）

`docs/request/request7.md` への要望設計書。

> ## 要望（原文）
> 無音カットを行う前に音量解析を実施し、設定変更の可否を求めるダイアログを出力する。
> ダイアログにはテキストボックスを置き、発話区間の最低dbを表示する。編集可能。
> 発話区間の最低dbを表示し、OKであればそれを settings_window.py の字幕タブの発話検出閾値(db) にする。
>
> **(追加)**
> 既存の無音音量閾値(db), 無音最小継続時間, 発話検出閾値(db)で使用している処理をコメントアウトして残しておく。
> 今回の音量解析で設定したdb以下をカット、db以上を動画として使用する方針とする。
> settings_window.py もコメントアウトした項目を一旦削除する。
>
> **(さらに追加)**
> 音量解析する際に発話が0.8秒以下であった場合、無視する。

本書はレビュー用の設計書であり、**実装は本書の承認後に行う**（`docs/CLAUDE.md` の方針に準拠）。

---

## 1. 要件の整理

| # | 要件 | 解釈 / 補足 |
|---|---|---|
| R1 | 無音カット**実行前**に音量解析を行う | `silence_cutter.run()` の前段に「音量解析」工程を挿入。解析対象は無音カット前の入力動画。 |
| R2 | 設定変更の可否を求めるダイアログを出す | GUI（`main_window.py`）経由実行時にモーダル表示。CLI/ヘッドレス時は表示しない。 |
| R3 | 編集可能なテキストボックスに**発話区間の最低dB**を表示 | 解析で得た最低dB（整数）を `QLineEdit`（整数バリデータ）に初期表示し、手修正可能にする。 |
| R4 | OK ならその値を閾値として採用 | 確定した**単一のdB値**を、新方針の無音カット閾値として使用する（後述 R6 と統合）。 |
| R5（追加） | 既存3項目を使う処理を**コメントアウトして残す** | `silence_cut.noise_threshold_db`・`silence_cut.min_silence_duration_sec`・`subtitle.speech_threshold_db` を参照する処理を、削除せずコメントアウトで温存する。 |
| R6（追加） | 解析で決めたdB**以下=カット／以上=動画使用** | 無音カットの判定を、解析確定dB1つに一本化する（「以下=無音→除去」「以上=有音→残す」）。 |
| R7（追加） | `settings_window.py` から該当項目を**削除** | 「無音音量閾値(dB)」「無音最小継続時間(秒)」「発話検出閾値(dB)」の UI 項目を画面から削除する。 |
| R8（さらに追加） | 音量解析で**0.8秒以下の発話は無視** | 発話候補区間のうち**継続時間が0.8秒以下**のものは測定対象から除外する（最低dB算出に含めない）。極短ノイズによる過小な閾値を防ぐ。 |

### 1.1 方針転換の要点（最重要）

従来は **3つのパラメータ**で制御していた：

| 従来パラメータ | setting.json キー | タブ | 役割 |
|---|---|---|---|
| 無音音量閾値(dB) | `silence_cut.noise_threshold_db` | 一般 | 無音カットの無音判定閾値 |
| 無音最小継続時間(秒) | `silence_cut.min_silence_duration_sec` | 一般 | 無音とみなす最小継続秒 |
| 発話検出閾値(dB) | `subtitle.speech_threshold_db` | 字幕 | テロップ表示の絞り込み閾値 |

本要望（追加）により、これらを使う処理は**コメントアウトで温存**し、**音量解析で確定した単一のdB値**へ一本化する。確定dBを境界に「dB以下＝カット／dB以上＝動画として使用」とする（R6）。

> **整合性メモ**：原文 R4「字幕タブの発話検出閾値にする」と、追加 R7「発話検出閾値の項目を削除」は一見矛盾する。本書は**追加要件を優先**し、「確定dBは UI 項目ではなく、解析→ダイアログで都度決まる単一閾値として扱う」と解釈する。値の保存先は §9「不明点」で確認する。

---

## 2. 現状調査（関連実装）

| 対象 | 内容（本要望に関係する箇所） |
|---|---|
| `src/modules/silence_cutter.py` | `run()` が `noise_threshold_db` と `min_silence_duration_sec` を読み、`detect_silence(input, noise, min_dur, cfg)`→`build_keep_segments()` で残す区間を決定。**R5/R6 のコメントアウト・差し替え対象。** |
| `src/modules/subtitle_generator.py` | `_gate_with_settings()` が `speech_threshold_db` を使いテロップ絞り込み。`run()` から `speech_gate_enabled` 時に呼ぶ。**R5 のコメントアウト対象。** |
| `src/pipeline/pipeline_runner.py` | `run_pipeline()` が工程を順次実行。`subtitle_review_callback` を `PipelineContext` へ注入する仕組みあり。**音量解析工程の追加先。** |
| `src/pipeline/pipeline_context.py` | 工程間 DTO。コールバック保持と進捗正規化。 |
| `src/gui/main_window.py` | ワーカースレッド実行＋`SubtitleReviewBridge`（ワーカー→メインスレッドのモーダル橋渡し）。**同パターンで音量ダイアログを実装可能。** |
| `src/gui/subtitle_editor_dialog.py` | モーダル `QDialog` の実装例。 |
| `src/settings/settings_window.py` | UI 構築・`DEFAULT_SETTINGS`・`load_settings`/`save_settings`。一般タブに無音音量閾値/無音最小継続時間、字幕タブに発話検出閾値の入力欄あり。**R7 の削除対象。** resolve6 で「項目削除」の前例あり（定数/参照/UI行/load/collect/DEFAULTS を一括除去）。 |
| `src/modules/ffmpeg_runner.py` | `get_ffmpeg_exe()`/`probe_duration()`/`execute()` 等のヘルパー。 |

**結論**：ダイアログ橋渡しは resolve3 の `SubtitleReviewBridge` を転用、区間検出は `silence_cutter.detect_silence`/`build_keep_segments` を再利用でき、UI 項目削除は resolve6 の手順を踏襲できる。

---

## 3. 全体設計

### 3.1 処理位置

`run_pipeline()` 冒頭、**無音カット工程の直前**に「音量解析・閾値確認」を追加する。

```
パイプライン開始
  └─（新規）音量解析・閾値確認   ← 入力動画(無音カット前)を解析しダイアログ表示
  ① 無音カット（新方針）          ← 確定dB「以下=カット / 以上=使用」
  ② フルテロップ生成             ← 発話検出による絞り込みはコメントアウト(R5)
  ③ オープニング結合
  ④ エンディング結合
  出力
```

### 3.2 データフロー

```
[Worker Thread]                         [Main Thread (GUI)]
run_pipeline
  └ volume_analyzer.analyze_min_speech_db(input, settings)
       └ 発話候補区間検出 → 各区間の音量測定 → 最低dB(整数)
  └ context.volume_analysis_callback(measured_db)
       │ （ブリッジ経由で emit、Event 待機）
       │ ───────────────────────────────▶ VolumeThresholdDialog
       │                                    テキストボックス初期値 = measured_db（編集可）
       │                                    [ OK ] / [ 変更しない ]
       │ ◀─────────────────────────────── 返却: 確定dB(int) or None
  └ 確定dB(int) を取得できた場合:
       settings["volume_analysis"]["last_cut_db"] = 確定dB   （メモリ更新）
       settings_window.save_settings(settings)               （setting.json 永続化）
       context にも当該実行のカット閾値として保持
  ① silence_cutter.run(context)
       └ 新方針: detect_silence(input, 確定dB, 継続秒, cfg) → dB以下を除去/以上を残す
```

---

## 4. 「発話区間の最低dB」の測定方式

閾値を決めるために閾値が要るチキン＆エッグを避け、**緩い基準で発話候補区間を切り出し→各区間の音量を実測→最小値**を採る。

### 4.1 推奨アルゴリズム（案A：区間別 volumedetect）

1. 総尺取得：`ffmpeg_runner.probe_duration()`。
2. 発話候補区間検出（既存関数を再利用）：
   - `silence_cutter.detect_silence(input, base_noise_db, base_min_dur, ffmpeg_cfg)`
   - `silence_cutter.build_keep_segments(silence, total)` → 有音(発話候補)区間。
   - ここで使う `base_noise_db`/`base_min_dur` は「解析専用パラメータ」とし `volume_analysis` セクションから取得（既存キーをコメントアウトするため、解析側は独自キーを持つ。§8 参照）。
3. 短い発話を除外（R8）：継続時間が **`min_region_sec`（既定 0.8 秒）以下の区間は無視**する。
   「0.8秒以下を無視」＝採用条件は「継続時間 > `min_region_sec`」（境界の0.8秒も無視＝厳密超過）。
   極短ノイズ・息継ぎ等を最低dB算出から除外し、過小な閾値を防ぐ。
4. 各区間の平均音量(RMS)を測定：
   - `ffmpeg -hide_banner -ss {start} -t {dur} -i input -af volumedetect -f null -`
   - stderr の `mean_volume: -XX.X dB` をパース。
5. **最低dB = 各区間 mean_volume の最小値**（最も静かに発話している区間の平均音量）。
6. `round` で整数化（閾値は整数で扱う）。

### 4.2 実行コスト対策

- 測定対象を「一定長以上」に限定、区間数に上限（`max_regions`）。超過時は長い区間から標本抽出し、標本化した旨を `logging` へ明示（暗黙の打ち切りを残さない）。

### 4.3 代替案（将来最適化・本実装は非採用）

- 案B：単一パス `astats`（`metadata=1:reset=N` ＋ `ametadata=print`）で窓ごと RMS を一括取得し Python 側で最小値算出。FFmpeg 起動1回で済むが stderr パースが複雑。§4.1 がボトルネック化した際の置換候補。

---

## 5. 新方針の無音カット（R6）

確定dBを唯一の基準に、無音カットを次のとおり再構成する。

- **判定**：`silencedetect=noise={確定dB}dB:d={継続秒}` で「確定dB以下が一定時間続く区間＝無音」を検出。
- **採否**：無音区間を除去し、それ以外（確定dB以上＝有音）を動画として連結（既存 `build_keep_segments`→`cut_and_concat_seek`/`batched` をそのまま利用）。
- 既存の `noise_threshold_db` を読む行は**コメントアウト**し、確定dBを使う行へ差し替える（§6）。

### 5.1 継続時間（`d=`）の扱い ※要確認

`silencedetect` は `d=`（最小無音継続秒）が必須。追加要件は「無音最小継続時間で使用している処理をコメントアウト」とするが、機構上 `d` は必要。本書では以下を提案し §9 で確認する：

- **推奨**：継続秒は `volume_analysis.cut_min_silence_sec`（新キー、UI 非表示）から取得して `d=` に与える。これにより「無音最小継続時間(UI項目)」は削除しつつ、機構的に必要な値は設定で管理（ハードコード回避）。
- 代替：モジュール定数で固定（簡易だが調整不可）。

---

## 6. コメントアウト対象の既存処理（R5：削除せず温存）

| ファイル | 箇所 | 対応 |
|---|---|---|
| `src/modules/silence_cutter.py` | `run()` の `noise_threshold_db = silence_cfg.get(...)` および `detect_silence(..., noise_threshold_db, min_silence_duration_sec, ...)` の旧呼び出し | 旧行を**コメントアウト**し、確定dB＋`cut_min_silence_sec` を使う新行に差し替え。`min_silence_duration_sec = _coerce_float(...)` も旧基準ぶんはコメントアウト。 |
| `src/modules/subtitle_generator.py` | `run()` の `if subtitle_cfg.get("speech_gate_enabled"...): timeline = _gate_with_settings(...)` と `_gate_with_settings()` 内の `speech_threshold_db` 参照 | 発話絞り込み呼び出しを**コメントアウト**（関数定義自体は温存）。テロップは絞り込みなしで全件→字幕編集画面へ。 |

- コメントは日本語で「要望(resolve7)により無効化。将来戻す可能性があるため温存」と明記する（`docs/CLAUDE.md` のコメント方針）。
- コメントアウトにより未参照となる設定キーは setting.json 上は残しても害はない（§8 で方針確認）。

---

## 7. settings_window.py からの項目削除（R7）

resolve6 と同じ手順で、以下3項目を画面から削除する。

| 削除する UI 項目 | タブ | 関連ウィジェット参照 | プレースホルダー定数 | DEFAULT/JSON キー |
|---|---|---|---|---|
| 無音音量閾値(dB) | 一般 | `noise_threshold_edit` | `PLACEHOLDER_NOISE_THRESHOLD` | `silence_cut.noise_threshold_db` |
| 無音最小継続時間(秒) | 一般 | `min_silence_duration_edit` | `PLACEHOLDER_MIN_SILENCE` | `silence_cut.min_silence_duration_sec` |
| 発話検出閾値(dB) | 字幕 | `speech_threshold_edit` | `PLACEHOLDER_SPEECH_THRESHOLD` | `subtitle.speech_threshold_db` |

削除対象コード（各項目について）：
1. 定数（プレースホルダー）定義
2. `__init__` のウィジェット参照初期化
3. `_build_general_tab` / `_build_subtitle_tab` の該当行（ラベル＋入力欄）
4. `_load_to_ui` の該当 setText 行
5. `_collect_settings` の該当書き出し行

> `DEFAULT_SETTINGS`/`setting.json` のキー自体を削除するかは §9 で確認。コメントアウトしたモジュール処理が将来復活し得る点、解析新方針が当該キーを使わない点を踏まえ、**キーは残置（UI のみ削除）**を推奨する。

---

## 8. setting.json 変更（提案）

既存キーは**残置**（UI からは削除、モジュール側はコメントアウト）。解析用に新セクションを追加する。

```jsonc
"volume_analysis": {
    "enabled": true,            // 無音カット前の音量解析＋確認ダイアログを行うか
    "base_noise_db": -30,       // 発話候補区間を切り出す緩い基準(解析専用)
    "base_min_silence_sec": 0.3,// 同上・区間検出の最小無音秒
    "min_region_sec": 0.8,      // 測定対象とする区間の最小長(秒)。これ以下は無視 (R8)
    "max_regions": 100,         // 測定対象区間数の上限(超過時は長い順に標本抽出)
    "metric": "mean",           // 区間代表音量: "mean"(RMS)/"max"(ピーク)
    "cut_min_silence_sec": 0.6, // 新方針の無音カットで使う d=(最小無音継続秒)
    "last_cut_db": -28          // 直近にダイアログで確定したカット閾値(dB)。OK時に上書き保存
}
```

- `DEFAULT_SETTINGS["volume_analysis"]` にも同値を定義し、欠落時は `_merge_with_defaults` で補完。
- `enabled=false` で本機能全体を無効化でき、（コメントアウト前の挙動には戻らないが）解析・ダイアログをスキップできる。

---

## 9. 不明点・確認事項（レビュー対象）

`docs/CLAUDE.md`「不明点は推測実装せず設計書へ記載」に従い明記する。

1. **確定dBの保存先**：**setting.json に保存する（確定済み）。** UI 項目（発話検出閾値）は削除するが、確定dBは新キー `volume_analysis.last_cut_db` に永続化する。ダイアログ OK 時に `settings_window.save_settings()` でメモリ＋`setting.json` の両方へ反映し、CLI 実行・次回起動時はこの保存値を既定として参照する。
2. **継続時間 `d=` の扱い**（§5.1）：UI 削除後の `silencedetect d=` の供給元。推奨は `volume_analysis.cut_min_silence_sec`。
3. **既存キーの残置/削除**：`silence_cut.noise_threshold_db` 等を setting.json/DEFAULT から残すか削除するか。本書は「UI のみ削除・キー残置」を推奨。
4. **テロップ発話絞り込みの停止可否**：`speech_threshold_db` 処理コメントアウトにより、テロップは全件（字幕編集画面で取捨）になる。これで良いか。
5. **「最低dB」の定義**：本書は「各発話候補区間の平均音量(RMS)の最小値」と解釈（`metric` で mean/max 切替可）。「区間ピークの最小」等の別解釈の可否。
6. **CLI 挙動**：CLI ではダイアログ不可。§9-1 の確定に伴い、**保存済み `volume_analysis.last_cut_db` を既定として使用**する想定。未保存（初回）時に解析値を自動採用するか、解析自体スキップするかは要確認。
7. **進捗工程化**：音量解析を `total_steps` の1工程に含めるか（暫定はステータス文言のみ）。

---

## 10. ダイアログ仕様（UI）

```
┌─────────────────────────────────────────────┐
│ 音量解析 — カット閾値の確認                      │
├─────────────────────────────────────────────┤
│ 入力動画を解析しました。この dB 以下をカット、      │
│ 以上を動画として使用します。値は手修正できます。    │
│                                               │
│ 発話区間の最低dB :  [  -28  ] dB   ← 編集可     │
│                                               │
│ （測定値: -28 dB / 対象区間: 42）               │
│                                               │
│                       [ OK ]   [ 変更しない ]   │
└─────────────────────────────────────────────┘
```

- 整数のみ（`QIntValidator`、負値可）。空欄/不正で OK 時は測定値へフォールバック。
- 「OK」＝入力値を当該実行のカット閾値として確定し、`volume_analysis.last_cut_db` として `setting.json` へ保存（§9-1）。
- 「変更しない」/×＝保存済み `last_cut_db`（無ければ測定値）を既定として続行し、保存は行わない。**パイプラインは中断しない**（字幕編集画面のキャンセル＝中断とは挙動が異なる）。

---

## 11. 追加・変更モジュール

| 区分 | ファイル | 変更概要 |
|---|---|---|
| 新規 | `src/modules/volume_analyzer.py` | 発話候補区間検出＋区間音量測定＋最低dB算出（`analyze_min_speech_db` 他） |
| 新規 | `src/gui/volume_threshold_dialog.py` | カット閾値の確認・編集ダイアログ（`result_value()`→int/None） |
| 変更 | `src/gui/main_window.py` | `VolumeThresholdBridge` 追加、`PipelineWorker`/`_on_run` でコールバック受け渡し |
| 変更 | `src/pipeline/pipeline_runner.py` | 無音カット前に解析＋ダイアログ＋確定dB反映を追加 |
| 変更 | `src/pipeline/pipeline_context.py` | `volume_analysis_callback`／確定dB保持の属性追加 |
| 変更 | `src/modules/silence_cutter.py` | 旧 `noise_threshold_db`/`min_silence_duration_sec` 処理をコメントアウトし、確定dB方式へ差し替え（R5/R6） |
| 変更 | `src/modules/subtitle_generator.py` | `speech_threshold_db` による発話絞り込み呼び出しをコメントアウト（R5） |
| 変更 | `src/settings/settings_window.py` | 無音音量閾値/無音最小継続時間/発話検出閾値の UI 項目を削除（R7）、`volume_analysis` を DEFAULT へ追加 |
| 変更 | `src/settings/setting.json` | `volume_analysis` セクション追加（既存キーは残置／§9-3 で確定） |

既存コードの**削除はコメントアウトで温存**（R5）。UI 項目のみ削除（R7）。その他は追加・引数拡張（後方互換）に留める。

---

## 12. エラー処理方針

| 事象 | 方針 |
|---|---|
| 発話候補区間が0件 | 「解析不能」とし、ダイアログに既定値表示・注記。OK で手入力/既定値を採用、パイプライン継続。 |
| 一部区間の測定失敗 | 当該区間を除外し残りで最小値算出。全滅時は `min_db=None`、ログ警告。 |
| 解析全体の例外 | `FFmpegError` をログし、ダイアログをスキップして既定dBで無音カットへ（処理全体は止めない）。 |
| 確定dB永続化失敗（採用時） | `OSError` をログ。当該実行はメモリ値で継続。 |

ログは既存 `utils/logger.get_logger(__name__)` を使用し `silence_cutter` 同水準で出力する。
