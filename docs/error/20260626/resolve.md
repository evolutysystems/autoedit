# 字幕一覧に「言っていない字幕」が大量に出る不具合 修正設計書

対象エラー: `docs/error/20260626/error.md`
対象ソース: `src/modules/subtitle_generator.py`（主因）/ `src/pipeline/pipeline_context.py`・`src/modules/silence_cutter.py`（一時ファイル調査）
本書はレビュー用の**修正設計書**であり、実装は承認後に行う（`docs/CLAUDE.md` の方針に準拠）。

---

## 1. 事象

字幕解析後の「字幕編集（一覧）ダイアログ」に、実際には発話していない字幕が大量に表示される。
特徴的なのは**同一フレーズが等間隔で連続**している点。

```
0:09:50.10 → 0:10:20.08 ご視聴ありがとうございました。
0:10:20.10 → 0:10:50.08 ご視聴ありがとうございました。
0:10:50.10 → 0:11:20.08 ご視聴ありがとうございました。
0:11:20.10 → 0:11:50.08 ご視聴ありがとうございました。
```

各エントリの尺は約 **30秒**、開始時刻も約 **30秒間隔**で、同一テキストが並ぶ。

---

## 2. 原因調査

### 2-1. 結論（主因）

**Whisper（faster-whisper）の「ハルシネーション（幻聴）」**が原因。
無音・極小音量・非発話（BGM/環境音のみ）の区間に対し、Whisper は学習データに頻出する定型句
（「ご視聴ありがとうございました」等）を**実在しない字幕として生成**する。

根拠：
- **30秒の等間隔**は Whisper の処理窓（30秒チャンク）と一致する。非発話チャンクごとに同一の幻聴字幕が1件ずつ出る、典型的なハルシネーション挙動である。
- 同一テキストの反復は、`transcribe` の既定 `condition_on_previous_text=True` により「直前の出力を次チャンクの文脈に与える」ことで**反復ループ**を誘発する既知の症状でもある。
- 当該フレーズ「ご視聴ありがとうございました」は日本語Whisperで最も多い幻聴語の一つ。

### 2-2. 寄与要因（本リポジトリ側の変更）

`src/modules/subtitle_generator.py` の `run()` で、従来は**発話区間によるテロップ絞り込み**
（`_gate_with_settings()` → `speech_threshold_db` で無発話区間の字幕を除外）が有効だった。
resolve7 の要望によりこの絞り込みを**コメントアウトで無効化**したため、

- これまでは「発話区間外の幻聴字幕」が絞り込みで自動的に落ちていた
- 無効化後はそのフィルタが効かず、**幻聴字幕がそのまま一覧へ流入**するようになった

該当箇所（現状・無効化済み）:

```python
# 要望(resolve7)により無効化。将来戻す可能性があるため処理を温存する。
# if subtitle_cfg.get("speech_gate_enabled", True):
#     timeline = _gate_with_settings(...)
```

さらに、`setting.json` の `volume_analysis.last_cut_db = -39`（現行値）は無音判定として**非常に低い**ため、
無音カットがほとんど効かず（-39dB 未満のみ無音扱い）、低レベルの非発話音声が大量に残る。
結果として Whisper が幻聴を起こす素地（残存した無発話区間）が増えている。

### 2-3. Whisper 呼び出しの現状（抑制策が未設定）

`WhisperTextSource.extract()` の認識呼び出しは以下のみで、**ハルシネーション抑制オプションが一切無い**。

```python
segments, _info = model.transcribe(
    input_path, language=language, beam_size=self._beam_size
)
```

- `vad_filter`（無音をVADで除外）未使用
- `condition_on_previous_text`（既定True＝反復を誘発）未調整
- `no_speech_threshold` / `compression_ratio_threshold` / `log_prob_threshold` 等の足切り未調整
- 生成タイムラインに対する**重複・反復除去が無い**

### 2-4. 一時ファイル残留の可能性（ユーザー仮説）→ 該当しない

ユーザー指摘の「一時ファイルが残っている可能性」を調査した結果、**本事象の原因ではない**と判断する。

- 作業ディレクトリは実行毎に `tempfile.TemporaryDirectory(prefix="autoedit_")` で**新規生成**され、
  パイプライン終了時に `PipelineContext.cleanup()` で削除される（`pipeline_context.py`）。
  → 前回実行の中間ファイルを次回が拾う経路が無い。
- 無音カットの中間 `silence_seg_*.mp4` / `silence_batch_*.mp4` は各処理の `finally` で削除される
  （`silence_cutter.py`）。仮に残っても**今回の作業用一時ディレクトリ内**であり字幕生成へは混入しない。
- 字幕一覧の表示元は**メモリ上の `timeline`**（`text_source.extract()` の戻り値）であり、
  既存 `subtitle.ass` を読み込んで表示しているわけではない。
  → 古いASS等の残留が一覧へ出ることはない。
- 何より「**同一テキストが30秒間隔で反復**」という症状は、ファイル重複（多様な内容が二重化する）では説明できず、
  Whisper 幻聴の固有パターンである。

> 補足：一時ファイル残留は別観点（ディスク逼迫・デバッグ容易性）では確認の価値があるが、本字幕不具合とは無関係。

---

## 3. 修正方針

resolve7 の「単一閾値・発話絞り込み無効化」の方向性を尊重しつつ、**Whisper 段で幻聴を抑制**することを主対策とする。
（絞り込み復活に依存しないため、resolve7 と矛盾しない。）

対策は多層で行う（いずれも `setting.json` で設定可能化し、ハードコードを避ける）。

### 対策A（主）：Whisper のハルシネーション抑制オプションを付与

`WhisperTextSource.__init__` で設定を取り込み、`transcribe()` へ渡す。

- `vad_filter=True`：内蔵 Silero VAD で**無音・非発話区間を認識対象から除外**（幻聴の発生源を断つ最有効策）。
  `vad_parameters`（`min_silence_duration_ms` 等）も設定可能にする。
- `condition_on_previous_text=False`：直前出力を文脈に与えないことで**反復ループを抑止**。
- `no_speech_threshold` / `log_prob_threshold` / `compression_ratio_threshold`：低信頼セグメントの足切り（既定踏襲＋設定可）。

```python
# 例（実装イメージ）
segments, _info = model.transcribe(
    input_path,
    language=language,
    beam_size=self._beam_size,
    vad_filter=self._vad_filter,                       # 既定 True
    vad_parameters=self._vad_parameters,               # 例 {"min_silence_duration_ms": 500}
    condition_on_previous_text=self._condition_prev,   # 既定 False
    no_speech_threshold=self._no_speech_threshold,     # 既定 0.6
)
```

### 対策B（保険）：幻聴定型句のブロックリスト除去

VAD を通っても残る定型句に備え、**フレーズ・ブロックリスト**で除去する。
既存のフィラー除去機構（`_strip_fillers` / `subtitle.fillers`）と同方式で、`subtitle.hallucination_phrases` を追加する。

- 完全一致（前後空白除去後）でブロックリストに一致するセグメントは `timeline` から除外。
- 既定値の例：`["ご視聴ありがとうございました", "ご視聴ありがとうございました。", "チャンネル登録お願いします"]`。

### 対策C（補強）：反復・異常尺セグメントのガード

- **連続重複除去**：直前セグメントとテキストが同一で時間的に隣接する場合は除外（反復ループの残渣対策）。
- 任意：テキスト長に対し尺が異常に長いセグメント（例：1文字あたりN秒超）を低信頼として除外（閾値は設定可能、既定は無効でもよい）。

> 対策A（特に `vad_filter`）で大半は解消する見込み。B/C は取りこぼし対策の保険。

### （任意・要確認）対策D：発話絞り込みの限定的復活

resolve7 で無効化した `_gate_with_settings()` を、設定フラグで**任意に再有効化**できる余地を残す案。
ただし resolve7 の方針（UI から発話検出閾値を削除）と整合させる必要があるため、**既定は無効**とし、採否はレビューで判断する（§6 不明点）。

---

## 4. 変更対象と設定追加

### 4-1. コード変更

| ファイル | 変更概要 |
|---|---|
| `src/modules/subtitle_generator.py` | `WhisperTextSource` に VAD/反復抑制オプションの取り込みと `transcribe()` への引数追加（対策A）。`extract()` 後に幻聴ブロックリスト除去＋連続重複除去を適用（対策B/C）。 |

`detect_speech_regions` / `_gate_with_settings`（コメントアウト済み）は温存（対策D の将来復活に備える）。

### 4-2. `setting.json`（`subtitle` セクションに追加）

```jsonc
"subtitle": {
    // ... 既存項目は不変 ...
    "whisper_vad_filter": true,                 // VADで無音/非発話を除外 (対策A)
    "whisper_vad_min_silence_ms": 500,          // VAD 最小無音長(ms)
    "whisper_condition_on_previous_text": false,// 反復ループ抑止 (対策A)
    "whisper_no_speech_threshold": 0.6,         // 無発話判定の足切り (既定踏襲)
    "hallucination_phrases": [                  // 幻聴ブロックリスト (対策B)
        "ご視聴ありがとうございました",
        "ご視聴ありがとうございました。"
    ],
    "drop_consecutive_duplicates": true         // 連続重複除去 (対策C)
}
```

- 既存キーは変更せず追加のみ（後方互換）。`DEFAULT_SETTINGS["subtitle"]` にも同既定値を定義し、欠落時は `_merge_with_defaults` で補完。
- すべて設定で ON/OFF・調整可能とし、ハードコードを避ける。

### 4-3. 関連（任意）

- `volume_analysis.last_cut_db = -39` は無音カットがほぼ効かない低さで、幻聴の素地を増やしている。
  本修正は閾値に依存せず効くが、運用上は再解析（ダイアログ）で適正値へ更新することを推奨（コード変更不要）。

---

## 5. エラー処理・ログ方針

- `vad_filter=True` で `onnxruntime` 等が必要となる環境では、VAD 初期化失敗時に**例外を握って VAD 無しへフォールバック**し、警告ログを出して処理継続する（テロップ全損を避ける）。
- ブロックリスト/重複除去で**全件除外**になった場合は、既存仕様（使用0件＝テロップ無しで続行）に倣い安全側で継続する。
- 除去件数（VAD前後・ブロックリスト・重複）を `INFO` で出力し、効果と過剰除去を検証可能にする。

---

## 6. 不明点・確認事項（レビュー対象）

1. **対策Dの採否**：発話絞り込み（`_gate_with_settings`）を設定フラグで再有効化する余地を残すか。本書は「既定無効・温存のみ」を推奨。
2. **VAD 依存関係**：実行環境で faster-whisper の `vad_filter` が利用可能か（必要パッケージの有無）。不可の場合は対策B/C を主対策へ格上げする。
   → **【確認済み・利用可能】** 実行環境で以下を検証した（2026-06-26 時点）。

   | 確認項目 | 結果 |
   |---|---|
   | `faster_whisper` 導入 / バージョン | あり / **1.2.1** |
   | `WhisperModel.transcribe` の `vad_filter` 引数 | **あり** |
   | `WhisperModel.transcribe` の `vad_parameters` 引数 | **あり** |
   | `onnxruntime` 導入 / バージョン | あり / **1.24.4** |
   | 同梱 Silero VAD モデルのロード（`faster_whisper.vad.get_vad_model()`） | **成功（`SileroVADModel`）** |

   → 追加パッケージのインストール不要で、**対策A（`vad_filter=True`）をそのまま主対策として採用可能**。
   対策B/C への格上げは不要（取りこぼし対策の保険として併用）。
3. **ブロックリスト既定値**：「ご視聴…」以外に既定登録すべき定型句（「チャンネル登録お願いします」等）の要否。
4. **異常尺ガード（対策C任意分）**の既定 ON/OFF と閾値。

---

## 7. 検証方針

1. 同一素材で修正前後を比較し、「ご視聴ありがとうございました」等の幻聴字幕が一覧から消えることを確認。
2. `vad_filter` ON/OFF で除去件数ログを比較し、実発話字幕が誤除去されていないこと（取りこぼし無し）を確認。
3. 反復ループ（同一文連続）が解消することを確認。
4. ブロックリストに実発話と紛らわしい語を入れた場合の誤除去挙動を確認（既定リストは安全側に限定）。
5. VAD 初期化失敗時にフォールバックして処理継続することを確認（異常系）。
