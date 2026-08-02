# resolve22 — YouTube 向けラウドネス正規化（音声の解析と編集） 修正設計書

## 0. 本書の位置づけ
`docs/request/ver2/request22.md` に対する **修正設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要ライブラリを追加しない／後方互換を失わない／不明点は推測実装せず設計書へ記載する）に従い、本書レビュー後に実装します。

---

## 1. 要望（request22.md）

| # | 要望 | 補足 |
|---|---|---|
| A1 | **クリップ用・アーカイブ切り抜き用のどちらでも、まず最初に音声の解析と編集を行う** | 各パイプラインの先頭工程として組み込む |
| A2 | **YouTube 用にオーディオレベルを解析し、次の値へ修正する**<br>・True Peak : **−1.0 dBFS**<br>・Integrated : **−14.0 LUFS** | YouTube の推奨ラウドネス基準（−14 LUFS）への正規化 |

> **用語の注記**：True Peak の単位は正確には **dBTP**（dBFS はサンプルピークの単位）。FFmpeg `loudnorm` の `TP` パラメータへ −1.0 を設定するため、目標値としては要望どおり −1.0 で一致する。

---

## 2. 現状分析（既存コードの実測）

> 行番号は調査時点。

### 2.1 クリップ用パイプライン（音声レベルは無加工）

`pipeline_runner.run_pipeline`（`pipeline_runner.py:33-89`）の工程順：

```
入力 → 音量解析(カット閾値確認・resolve7) → ①無音カット → ②フルテロップ → ③OP結合 → ④ED結合 → 出力
```

- ラウドネスの測定・補正工程は**存在しない**。出力音量は入力素材の音量に依存する。
- 進捗は `total_steps=4`（`:96-103`）で分母管理。工程は `context.begin_step/end_step` で刻む。
- 中間ファイルは `PipelineContext.allocate_intermediate`（`pipeline_context.py:77-80`）で作業ディレクトリへ置かれ、`cleanup` で自動削除される。

### 2.2 アーカイブ切り抜きパイプライン（同じく無加工）

`clip_writer.write_clips`（`clip_writer.py:457`）→ `_prepare_clips`（`:325-352`）の per クリップ工程：

```
_cut_region (元VODから区間切り出し・-c copy / :48-61)
  → _silence_cut (無音カット / :291-294)
  → _transcribe (文字起こし)
  → (一括レビュー) → _burn_one (焼き込み) → _decorate_clip (テーマ演出) → 結合
```

- 音声レベルは全工程で無加工（焼き込み・演出の再エンコードで AAC 変換されるのみ）。
- イントロカード（`_build_intro_card` `:188-225`）は**元 VOD から別途切り出して**生成され、`mute_intro`（既定 `false`）が有効な場合のみ無音化される（`:215-217`）。

### 2.3 既存の「音量解析」（resolve7）との関係 — **目的が異なる**

- `volume_analyzer.analyze_min_speech_db`（`volume_analyzer.py:70`）は volumedetect（RMS/最大dB）で「**無音カットの閾値**」を算出するもので、EBU R128 ラウドネス（LUFS/True Peak）の測定・補正機能ではない。
- したがって本要望は**別モジュール**（`loudness_normalizer`）として新設する。ただし**実行順序が重要**：カット閾値の解析（resolve7）は「正規化後の音声」に対して行うべきである（正規化でゲインが変わると閾値の前提が崩れるため）。→ **正規化 → 音量解析 → 無音カット** の順とする（§5.3）。
- **副次効果**：アーカイブ経路は固定閾値（`silence_cut.noise_threshold_db`）で無音判定しているため、先に −14 LUFS へ揃えることで **VOD ごとの音量差による無音判定のブレが緩和**される。

### 2.4 再利用できる資産

| 資産 | 用途 |
|---|---|
| `ffmpeg_runner.execute`（`run_ffmpeg_progress` 内蔵 / `:205-220`） | 正規化 2 パスの進捗付き実行（CLAUDE.md「進捗管理は run_ffmpeg_progress」に適合） |
| `ffmpeg_runner.probe_duration`（`:101`） | 進捗の総尺・測定パスの分母 |
| `ffmpeg_runner.get_ffmpeg_exe` / `get_ffprobe_exe` | 実行ファイル解決（同梱 FFmpeg 対応） |
| `ffmpeg.audio_codec`（既定 `aac`）・`ffmpeg.audio_sample_rate`（既定 `48000` / `settings_window.py:213,219`） | 2 パス目の音声エンコード設定（**新規キーを重複追加しない**） |
| `PipelineContext.allocate_intermediate` / `set_current_video_path` | クリップ用の中間ファイル差し替え |
| `_prepare_clips` の `clip_dir`（クリップ専用サブフォルダ） | アーカイブ用の中間ファイル置き場（一時領域・自動削除） |

---

## 3. 方式選定（ラウドネス正規化の実現手段）

| 方式 | 精度 | ダイナミクス | コスト | 判定 |
|---|---|---|---|---|
| **FFmpeg `loudnorm` 2 パス（linear=true）** | ◎（実測値を与えた線形ゲイン適用。Integrated/TP を高精度で達成） | ◎（線形＝音質変化最小） | 音声デコード 2 回（測定+適用）。映像は `-c:v copy` で無劣化・高速 | **採用（既定）** |
| FFmpeg `loudnorm` 1 パス（dynamic） | ○（目標へ動的補正） | △（コンプレッション的挙動で音質が変わり得る） | 音声デコード 1 回 | 設定で選択可（`two_pass: false`） |
| volumedetect＋`volume`/`alimiter` 手動計算 | △（LUFS でなく RMS 基準。TP 保証が煩雑） | ○ | 低 | 不採用 |
| 外部ライブラリ（pyloudnorm 等） | ◎ | ◎ | **新規依存**＋デコードの二重実装 | 不採用（CLAUDE.md「不要なライブラリを追加しない」） |

### 採用方針
- **FFmpeg `loudnorm`（EBU R128）2 パス・linear モード**を既定とする。
  - **1 パス目（測定）**：`-af loudnorm=I=-14:TP=-1.0:LRA=11:print_format=json -f null -` を実行し、stderr 末尾の JSON（`input_i` / `input_tp` / `input_lra` / `input_thresh` / `target_offset`）を取得。
  - **2 パス目（適用）**：`loudnorm=I=-14:TP=-1.0:LRA=11:measured_I=…:measured_TP=…:measured_LRA=…:measured_thresh=…:offset=…:linear=true` を `-c:v copy`（映像無劣化）＋音声再エンコード（`ffmpeg.audio_codec`）で適用。
- `loudnorm` は内部 192kHz へアップサンプルするため、出力サンプルレートを既存設定 `ffmpeg.audio_sample_rate`（48000）で**明示**する（`-ar`）。
- 測定の結果、線形適用で True Peak が目標を超える場合は `loudnorm` 自身がゲインを制限する（仕様どおり。ログへ実測値を残す §5.8）。

---

## 4. 設計方針（全体）

1. **新規モジュール `src/modules/loudness_normalizer.py` に閉じる**（測定・フィルタ組み立て・正規化実行・パイプライン工程 `run(context)`）。新規サードパーティ依存なし。
2. **クリップ用**：`run_pipeline` の**先頭工程**（音量解析 resolve7 より前）に追加し、正規化済み中間ファイルへ `context.set_current_video_path` で差し替える。**以降の既存工程は無改変**でそのまま正規化済み動画を処理する。
3. **アーカイブ用**：**各クリップの切り出し直後**（`_cut_region` の次・無音カットの前）に適用する。VOD 全体への適用は音声のみ再エンコードでも長尺で高コストであり、切り抜き結果の品質には**クリップ区間の正規化で十分**なため（§10-1 で確認）。クリップ単位で −14 LUFS になるため、結合後の全体もほぼ −14 LUFS となる。
4. **設定は setting.json 新設 `loudness` 節**で管理（目標値・ON/OFF・パス数）。目標値のハードコード禁止。
5. **失敗時はスキップして継続**：正規化は品質改善の工程であり、測定・適用の失敗で動画全体を失わない（既存の演出失敗時と同じ思想 / `clip_writer._decorate_clip` 参照）。元パスのまま次工程へ進み WARNING を残す（§7）。
6. **後方互換**：`loudness.enabled=false` で完全に従来挙動。既定は `true`（要望どおり既定で正規化）。
7. **映像は常に `-c:v copy`**：正規化中間の映像ストリームは元と同一のため、タイムスタンプ・尺・keep_segments（Resolve 出力の編集点）との整合は保たれる（§5.3 注記）。

---

## 5. 詳細設計

### 5.1 新規／変更ファイル一覧（予定）

| 種別 | ファイル | 内容 |
|---|---|---|
| 新規 | `src/modules/loudness_normalizer.py` | 測定（1 パス目 JSON 抽出）／フィルタ文字列組み立て（純関数）／正規化実行（2 パス）／パイプライン工程 `run(context)`／音声ストリーム有無判定 |
| 変更 | `src/pipeline/pipeline_runner.py` | 先頭工程「音声解析・正規化」を追加。`total_steps` 4→**5** |
| 変更 | `src/archive/clip_writer.py` | `_prepare_clips` で `_cut_region` 直後に正規化を呼ぶ（進捗ラベル追加） |
| 変更 | `src/settings/settings_window.py` | `DEFAULT_SETTINGS["loudness"]` 追加（§6） |
| 変更 | `src/settings/setting.json` | `loudness` 既定値追加 |
| 新規 | `tests/test_loudness_normalizer.py` | フィルタ文字列組み立て・loudnorm JSON 抽出・スキップ判定の単体テスト（FFmpeg 実行不要の純関数を対象） |

> 設定画面（settings_window の UI）への項目追加は本要望に含めない（`volume_analysis`／`export` と同じく setting.json 管理。必要なら §10-6）。

### 5.2 `loudness_normalizer` の API

| 関数 | 内容 |
|---|---|
| `build_loudnorm_filter(targets, measured=None)` | `loudnorm=I=…:TP=…:LRA=…[:measured_I=…:…:offset=…:linear=true][:print_format=json]` を組み立てる**純関数**（単体テスト対象） |
| `measure_loudness(input_path, targets, ffmpeg_cfg, on_progress=None)` | 1 パス目を `-f null -` で実行し、stderr 末尾の JSON ブロックを抽出して dict（`input_i`/`input_tp`/`input_lra`/`input_thresh`/`target_offset`）を返す。抽出不能は `None` |
| `has_audio_stream(input_path, ffmpeg_cfg)` | `ffprobe -select_streams a` で音声ストリーム有無を返す（無ければ正規化をスキップ） |
| `normalize_file(input_path, output_path, settings, on_progress=None)` | 測定→適用の一括実行。**成功時は output_path、スキップ/失敗時は input_path を返す**（呼び出し側は戻り値をそのまま次工程へ渡すだけでよい） |
| `run(context)` | クリップ用パイプライン工程。`enabled` 判定→測定→適用→`context.set_current_video_path(出力)` |

**2 パス目のコマンド構成**（`normalize_file` 内）：

```
ffmpeg -y -i <in>
  -af "loudnorm=I=-14.0:TP=-1.0:LRA=11.0:measured_I=…:measured_TP=…:measured_LRA=…:measured_thresh=…:offset=…:linear=true"
  -c:v copy                       ← 映像は無劣化コピー (高速・タイムライン不変)
  -c:a {ffmpeg.audio_codec}       ← 既定 aac
  -b:a {loudness.audio_bitrate}   ← 既定 192k (§6)
  -ar {ffmpeg.audio_sample_rate}  ← 既定 48000 (loudnorm の内部 192kHz 化を明示的に戻す)
  <out>
```

- 進捗は `ffmpeg_runner.execute(cmd, total_duration=probe_duration(...), on_progress=…)` で通知（run_ffmpeg_progress 準拠）。測定パスも同様に総尺で進捗が出る。
- `two_pass: false` の場合は measured を与えない 1 パス（dynamic）適用とする。

### 5.3 クリップ用の組み込み（A1 前半）

`run_pipeline` の工程順を次のとおりとする（**「まず最初に」音声の解析と編集**）：

```
入力
→ ⓪ 音声解析・正規化 (新設・loudness_normalizer.run)     ← 本要望
→ 音量解析 (カット閾値確認・resolve7 / 正規化後の音声に対して実施)
→ ① 無音カット → ② フルテロップ → ③ OP結合 → ④ ED結合 → 出力
```

- `total_steps` を 4→**5** とし、`begin_step("音声解析・正規化")`〜`end_step` で刻む。工程内の進捗は測定パス 0〜50%・適用パス 50〜100% を `progress_subcallback` で全体進捗へ配分する。
- 中間ファイル：`context.allocate_intermediate("loudness_normalized.mp4")`（cleanup で自動削除）。
- **keep_segments／Resolve 出力との整合**：正規化は `-c:v copy` のため映像ストリーム・総尺・タイムスタンプは元入力と同一。無音カットが算出する `keep_segments`（正規化後動画相対）は**元入力相対と同値**であり、Resolve 出力（resolve20/21）のカット編集点は従来どおり成立する。
- **無音カット OFF（`silence_cut.enabled=false`）でも正規化は実行**する（独立した工程。要望 A1 は無音カットの有無に依存しない）。

### 5.4 アーカイブ用の組み込み（A1 後半）

`_prepare_clips`（`clip_writer.py:325-352`）の per クリップ工程を次のとおりとする：

```
_cut_region (raw.mp4 切り出し・-c copy)
→ 音声解析・正規化 (新設): raw.mp4 → clip{n}/normalized.mp4   ← 本要望 (クリップの最初の編集)
→ _silence_cut → _transcribe → (レビュー) → 焼き込み → 演出 → 結合
```

- 呼び出しは `loudness_normalizer.normalize_file(raw, os.path.join(clip_dir, "normalized.mp4"), settings)` の 1 行を挿入し、戻り値を `_silence_cut` へ渡す（スキップ/失敗時は raw がそのまま返るため分岐不要）。
- 中間ファイルはクリップ専用サブフォルダ（一時領域）に置かれ、`TemporaryDirectory` が自動削除する。
- **VOD 全体ではなくクリップ単位で正規化する理由**（§10-1）：
  - コスト：数時間の VOD 全体の音声デコード×2 パス＋全体 remux は重い。クリップ単位なら合計尺は採用区間のみ。
  - 品質：Integrated ラウドネスは「その出力単位」で −14 LUFS になるべき値であり、クリップ（＝視聴単位）ごとの正規化が YouTube 向けの意図に合致する。各クリップが −14 LUFS のため結合版もほぼ −14 LUFS となる。
- 進捗ラベル：「クリップ n/m を準備中…（音声正規化）」を `_prepare_clips` の進捗刻みへ追加（既存の 0〜0.55 配分内）。
- **イントロカード（テーマ演出）の音声**：**対象外（確定 2026-07-31・§10-3）**。`mute_intro=true` なら無音でありラウドネス非該当。`mute_intro=false` の場合もイントロ 1.5 秒には正規化・ゲイン補正を行わない（`_build_intro_card` は無改変。1.5 秒の短尺は Integrated 測定が不安定であり、演出部分の音量差は許容とする）。
- **OP/ED 素材**：ユーザーが用意した完成素材のため**対象外（確定 2026-07-31・§10-4）**（正規化しない）。

### 5.5 スキップ条件（解析結果に基づく分岐）

| 条件 | 挙動 |
|---|---|
| `loudness.enabled=false` | 工程ごとスキップ（ログ INFO） |
| 音声ストリーム無し | スキップ（INFO。映像のみ素材で落ちない） |
| 1 パス目の測定失敗（JSON 抽出不能・FFmpeg 失敗） | 正規化をスキップし元パスで継続（WARNING / §7） |
| 測定値が既に目標に十分近い | **スキップしない**（そのまま 2 パス目を適用。線形ゲイン≒0dB で無害・出力の一貫性を優先） |

### 5.6 ログ出力（§A2 の「解析」結果の可視化）

- 測定結果を INFO で明示：`入力ラウドネス: I=-9.2 LUFS / TP=-0.3 dBTP / LRA=5.1 LU → 目標 I=-14.0 / TP=-1.0 (適用ゲイン -4.8 dB)`
- 適用完了 INFO（出力パス・所要）。スキップ理由（無効/音声なし/測定失敗）を INFO/WARNING。
- 既存 `utils/logger` を流用。

---

## 6. setting.json 追加案（**追加のみ**・既存キー不変）

`DEFAULT_SETTINGS` に新規トップレベル `loudness` を追加（`_merge_with_defaults` が欠落補完＝後方互換）。

```jsonc
"loudness": {
  "enabled": true,          // ラウドネス正規化 ON/OFF (false で従来挙動)
  "target_i": -14.0,        // Integrated 目標 (LUFS)。YouTube 推奨 (要望 A2)
  "target_tp": -1.0,        // True Peak 目標 (dBTP) (要望 A2)
  "target_lra": 11.0,       // Loudness Range 目標 (LU)。loudnorm 既定値相当
  "two_pass": true,         // true=2パス測定+線形適用 (精度優先) / false=1パス dynamic
  "audio_bitrate": "192k"   // 2パス目の音声ビットレート
}
```

- **音声コーデック・サンプルレートは既存キーを流用**：`ffmpeg.audio_codec`（`aac`）・`ffmpeg.audio_sample_rate`（`48000`）。重複キーは作らない。
- 目標値を設定化することで YouTube 以外の基準（例: 放送 −24 LUFS）へも変更可能（ハードコード回避）。
- 既存セクション（`general`/`subtitle`/`silence_cut`/`volume_analysis`/`ffmpeg`/`archive`…）は**一切変更しない**。

---

## 7. エラー処理・フォールバック方針

- **正規化は「失敗しても本編を失わない」**：測定・適用いずれの失敗も例外を握って WARNING を出し、**元パスのまま次工程へ継続**する（`_decorate_clip` と同じ思想。パイプラインを中断しない）。
- 例外の型：FFmpeg 実行失敗は既存 `FFmpegError` をそのまま利用（新規例外は追加しない）。
- 音声ストリーム無し／機能無効はエラーではなく通常スキップ（§5.5）。
- 中間ファイルは一時領域のみに生成し、出力先を汚さない（クリップ用=context 作業Dir / アーカイブ=workdir 配下）。

## 8. 影響範囲・後方互換

- **`loudness.enabled=false` で完全従来挙動**（工程スキップ・中間ファイル生成なし）。既定 `true` のため、**既定挙動は「全出力が −14 LUFS / −1.0 dBTP に正規化される」に変わる**（本要望の意図どおり）。
- クリップ用は `total_steps` 4→5 により進捗表示の分母が変わるのみ（工程ロジックは無改変）。
- 処理時間の増加：クリップ用=入力全長の音声デコード×2＋remux（映像コピーのため軽量）。アーカイブ=採用クリップ尺のみ×2 パス。
- **resolve7（音量解析）との整合**：閾値解析は正規化後に実行されるため自動追従する。ただし保存済み `volume_analysis.last_cut_db` は「正規化前の音量水準」で確定された値のため、初回実行時にダイアログ初期値（測定値）が従来より変わり得る（§10-5）。
- **Resolve 出力（resolve20/21）**：FCPXML は**元 VOD/元入力を参照**するため、Resolve タイムライン上の音声は未正規化のまま（正規化は本アプリの出力動画に対する処理）。Resolve 側で揃えたい場合は Fairlight のノーマライズ機能を使う運用とする（§10-2）。
- 新規依存なし（FFmpeg 標準フィルタのみ）・spec 変更なし（`src/modules` 配下は通常 import で同梱）。

## 9. テスト方針

1. **単体テスト（FFmpeg 不要・純関数）**：
   - `build_loudnorm_filter`：目標値のみ／measured 付き（linear・offset）／print_format の組み立て。
   - loudnorm JSON 抽出：stderr サンプル文字列から `input_i` 等を取得。壊れた出力で `None`。
   - スキップ判定：`enabled=false`・音声なし・測定失敗時に入力パスがそのまま返ること（FFmpeg 呼び出しはスタブ化）。
2. **実機確認**：
   - 正規化後の出力を 1 パス目測定に掛け、`I≈-14.0±0.5 LUFS`・`TP≦-1.0 dBTP` を確認（クリップ用/アーカイブ用の両フロー）。
   - 無音カット・字幕タイミング・Resolve 出力の編集点が正規化前と一致すること（映像 `-c:v copy` の担保確認）。
   - YouTube アップロード後の「コンテンツ ラウドネス」表示が 0 dB 近辺になること（任意）。

## 10. 確認事項 → **回答反映（2026-07-31）**

> ユーザー回答により #1/#3/#4/#5/#6/#7 を確定。#2 は既定を採用。

| # | 項目 | 論点 | **回答（確定 2026-07-31）** |
|---|---|---|---|
| 1 | **アーカイブの正規化単位** | VOD 全体を先に正規化するか、クリップ切り出し直後（本設計）か | **確定：クリップ切り出し直後**（クリップ単位。高速・視聴単位で −14 LUFS。§5.4） |
| 2 | **Resolve 出力への反映** | FCPXML は元素材参照のため正規化が乗らない | 既定採用：対象外（Resolve 側 Fairlight で対応する運用） |
| 3 | **イントロカードの音声**（`mute_intro=false` 時） | 1.5 秒の短尺は単独測定が不安定 | **確定：対象外**（近似ゲインも適用しない。`_build_intro_card` は無改変・§5.4） |
| 4 | **OP/ED 素材** | ユーザー素材も正規化するか | **確定：対象外**（完成素材を尊重） |
| 5 | **`last_cut_db` の水準変化** | 正規化によりダイアログ初期値（測定 dB）が従来と変わる | **確定：許容**（resolve7 の解析が正規化後音声へ自動追従するのは正しい挙動） |
| 6 | **設定画面 UI** | `loudness` 節の UI を settings_window へ追加するか | **確定：追加しない**（setting.json 管理。`volume_analysis`/`export` と同方針） |
| 7 | **失敗時の挙動** | スキップ継続（本設計）か、パイプライン中断か | **確定：スキップ継続**（本編全損を避ける。WARNING で可視化） |

## 11. 段階実装（レビュー後）

1. **基盤**：`loudness_normalizer.py`＋`loudness` 設定＋単体テスト（フィルタ組み立て/JSON 抽出/スキップ判定）。
2. **クリップ用（A1 前半）**：`run_pipeline` へ工程追加（total_steps=5）→ 実動画で −14 LUFS / −1.0 dBTP 達成と既存工程（無音カット/字幕/OP/ED）の無影響を確認。
3. **アーカイブ用（A1 後半）**：`_prepare_clips` へ挿入 → 実 VOD で確認（クリップ単位の達成値・結合版の水準）。
4. **実測検証**：§9-2 の受け入れ確認（±0.5 LU / TP ≦ −1.0）。

実装着手は本設計書のレビュー後とする（CLAUDE.md「実装は設計書レビュー後に行う」遵守）。

---

## 12. 実装状況（2026-07-31）

本設計書（§10 の確定回答反映済み）に基づき段階 1〜3 を実装済み。段階 4（実測検証）は実機確認待ち。

| 項目 | 実装 | 備考 |
|---|---|---|
| 基盤モジュール | 済 | `src/modules/loudness_normalizer.py`（`loudness_config`/`build_loudnorm_filter`/`parse_loudnorm_json`/`has_audio_stream`/`measure_loudness`/`normalize_file`/`run`）。新規依存なし |
| A1 クリップ用（先頭工程） | 済 | `run_pipeline` の工程⓪「音声解析・正規化」（`total_steps=5`）。音量解析（resolve7）は正規化後に実行 |
| A1 アーカイブ用（クリップ単位・§10-1 確定） | 済 | `_prepare_clips` で `_cut_region` 直後に `normalize_file(raw → clip{n}/normalized.mp4)`。進捗ラベル「（音声正規化）」追加 |
| A2 目標値 | 済 | `loudness` 設定既定 `target_i=-14.0` / `target_tp=-1.0`（`target_lra=11.0`）。2 パス linear 適用・映像 `-c:v copy` |
| 設定追加 | 済 | `DEFAULT_SETTINGS`／`setting.json` に `loudness` 節（enabled/target_i/target_tp/target_lra/two_pass/audio_bitrate）。コーデック/サンプルレートは既存 `ffmpeg` 節を流用 |
| 失敗時スキップ継続（§10-7 確定） | 済 | 無効/音声なし/測定失敗/適用失敗のいずれも元パスで継続（WARNING・部分ファイル削除）。`normalize_file` の戻り値をそのまま次工程へ渡す設計 |
| 対象外の確定事項 | 済 | イントロカード音声（§10-3）・OP/ED（§10-4）は無改変。設定画面 UI 追加なし（§10-6） |
| 単体テスト | 済 | 12 件追加・全体 56 件（`python -m unittest discover -s tests`）全て成功 |

**残作業（§9-2 実測検証）**：実動画・実 VOD で正規化後の出力を測定し `I≈-14.0±0.5 LUFS`・`TP≦-1.0 dBTP` を確認する。無音カット編集点・字幕タイミング・Resolve 出力の不変も併せて確認する。
