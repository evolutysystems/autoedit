# resolve8（ver3） — テロップが発話より手前に出る問題（VAD 余白）修正設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request8.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「いきなり実装を開始しない」）。
* 調査は **2026-08-19 時点の実コードと、実際に導入されている faster-whisper 1.2.1 のソース**を
  読んで行った。推測で書いた箇所は無く、実測していない数値は「理論値」と明記した。
* 変更は **`src/modules/subtitle_generator.py` の 1 箇所（VAD パラメータ）のみ**。
  Timeline・レンダラ・出力・保存形式には一切触れない。
* **setting.json は本書では書き換えない**。要望どおり設定項目も増やさない（§3-3 / §7）。

---

## 1. 要望（request8.md）と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **D1** | テロップが発話より手前（体感 0.5 秒ほど）に出る現象の**原因を特定**する | 調査 |
| **D2** | 手前ズレを **固定値で 300ms 削る** | 不具合修正 |
| **D3** | **設定項目としては出さない**（一旦は固定値） | 制約 |

要望文に無いが、実装に必ず要る論点:

| ID | 論点 | 理由 |
|---|---|---|
| **D4** | 300ms 削った結果、**語頭を取りこぼさない**こと | 余白は本来「VAD の検出遅れで語頭が切れるのを防ぐ」ためにある。削り過ぎると認識文字列そのものが欠ける |
| **D5** | 影響が **アーカイブ用 Timeline だけでなく全経路に及ぶ**ことの明示 | テロップ時刻は共通の音声認識層で作られる。焼き込み結果も変わる |
| **D6** | 固定値の**置き場所**（`docs/claude.md`「ハードコードは禁止」との整合） | 生の数値を式へ埋めると規約に反する |

---

## 2. 現状分析

### 2.1 テロップ時刻はどこで決まるか（アーカイブ用 Timeline 経路）

```
clip_writer._prepare_edit_points()            (clip_writer.py:348)
  ├─ silence_cutter.detect_edit_points()      → keep_segments (残す区間)
  └─ subtitle_generator.recognize_for_timeline()          (subtitle_generator.py:1138)
       ├─ WhisperTextSource.extract()                     (subtitle_generator.py:324)
       │    ├─ _transcribe()  … faster-whisper へ投げる   (subtitle_generator.py:378)
       │    └─ _speech_bounds() … 先頭語 start 〜 末尾語 end を採用 (subtitle_generator.py:366)
       ├─ map_items_to_timeline() … 素材時間 → 詰めた軸へ写像 (subtitle_generator.py:1114)
       └─ adjust_display_timing() … 表示タイミング整形       (subtitle_generator.py:878)
                     ↓
timeline_builder._append_subtitles()          (archive/timeline_builder.py:158)
  → SubtitleClip(timeline_start = start + offset)          (archive/timeline_builder.py:182)
```

* `_append_subtitles` が足しているのは **クリップの開始位置ぶんの `offset` だけ**。
* `adjust_display_timing`（§2.2）が動かすのは **終了時刻だけ**で、開始は `entry["start"]` のまま
  （`subtitle_generator.py:885` `disp_start = entry["start"]`）。
* つまり **Timeline 上のテロップ開始 = Whisper が返した先頭語の start** であり、
  パイプライン側で手前へずらしている箇所は **一つも無い**。

### 2.2 「発話の手前にバッファを持つ」設定はコード上に存在しない

`subtitle` セクションで前方に余白を足し得るキーを全て確認した。

| キー | 現在値 | 実際の作用 | 手前ズレの原因か |
|---|---|---|---|
| `display_min_duration_sec` | 0.5 | 表示が短すぎるときに**終了を後ろへ**伸ばす下限（`subtitle_generator.py:898`） | **×**（開始は動かない） |
| `display_max_hold_sec` | 2.0 | 発話末＋2 秒で消す／2 秒以内に次発話があれば**次発話の開始まで保持** | **×**（前のテロップが伸びるだけ） |
| `speech_pad_sec` | 0.0 | `gate_timeline_by_speech()` の前後パディング（`subtitle_generator.py:777`） | **×**（値 0 かつ経路が無効） |
| `speech_gate_enabled` | true | **resolve7 の要望で呼び出しごとコメントアウト済み**（`subtitle_generator.py:1054-1055`） | **×**（設定は残っているが効いていない） |
| `clip_pad_sec` | 0 | 切り抜き区間そのものの前後余白（`archive/scoring.py:153`） | **×**（クリップ境界の話。テロップ相対位置は不変） |

> 補足: `speech_gate_enabled` は `setting.json:20` に `true` のまま残っており、設定画面にも欄がある。
> **実装が無効化されているのに設定だけ生きている**状態なので、利用者からは「効いている設定」に見える。
> 整理は §11（本書では触らない）。

### 2.3 真因 — faster-whisper の VAD が発話チャンクを前後 400ms 膨らませている

`_transcribe()` は VAD へ **`min_silence_duration_ms` しか渡していない**。

```python
# src/modules/subtitle_generator.py:389-392（現状）
if self._vad_filter:
    # 内蔵 Silero VAD で無音/非発話区間を認識対象から除外する
    kwargs["vad_filter"] = True
    kwargs["vad_parameters"] = {"min_silence_duration_ms": self._vad_min_silence_ms}
```

渡さなかったキーはライブラリ既定が使われる（`faster_whisper/vad.py:36-42`）。

| VadOptions | 既定値 | 本プロジェクトの指定 |
|---|---|---|
| `threshold` | 0.5 | 未指定（既定） |
| `min_speech_duration_ms` | 0 | 未指定（既定） |
| `max_speech_duration_s` | inf | 未指定（既定） |
| `min_silence_duration_ms` | 2000 | **500**（`setting.json:54`） |
| **`speech_pad_ms`** | **400** | **未指定 → 400 が効いている** ← 真因 |

`speech_pad_ms` は「切り出した発話チャンクの**前後を各 400ms 膨らませる**」指定である
（`vad.py:163` で開始を `speech_pad_samples` ぶん手前へ、`vad.py:173-180` で終了を後ろへ広げる。
ただし隣接チャンク間の無音が `2 * speech_pad_ms` 未満のときは、その無音を折半して分け合う
= `vad.py:166-176`）。

処理の流れ（`transcribe.py:884-891` / `transcribe.py:1010`）:

1. `get_speech_timestamps()` で **前後 400ms 膨らませた**発話チャンク列を得る
2. `collect_chunks()` でチャンクだけを連結した音声を作り、Whisper へ渡す
3. `restore_speech_timestamps()`（`transcribe.py:1844`）で、得られた時刻を
   **チャンク境界を基準に元の時間軸へ線形に戻す**

結果、**チャンク先頭の 400ms は「無音だが認識対象に含まれている」**。
Whisper は `word_timestamps=True`（`setting.json:57`）の DTW アライメントで
先頭語の start をこの無音側へ寄せがちで、`_speech_bounds()` はその先頭語 start を
そのまま採用する（`subtitle_generator.py:366-375`）。

**したがって「発話の手前のバッファ」の正体は `speech_pad_ms` の既定 400ms である。**

* 理論上の手前ズレ: `min(400ms, 直前の無音 ÷ 2)` = **概ね 0.3〜0.4 秒**
* 体感 0.5 秒との差は Whisper のアライメント誤差ぶんと考えられる（実測は §10-1）

### 2.4 手前ズレが「必ず」出るように見える理由

`min_silence_duration_ms = 500` のため、**0.5 秒以上の間があるたびにチャンクが割れる**。
配信の喋りでは息継ぎのたびに割れるため、ほぼ全てのテロップが「チャンク先頭」になり、
毎回 400ms の余白を先頭に抱える。要望文の「**必ず**手前にバッファを持って」という
見え方と一致する。

### 2.5 影響を受ける経路（この 1 行は全経路で共有されている）

| 経路 | 入口 | 本修正の影響 |
|---|---|---|
| アーカイブ用 Timeline | `clip_writer._prepare_edit_points` → `recognize_for_timeline`（`:1138`） | **あり**（本要望の対象） |
| アーカイブ用 従来画面（`timeline_review=false`） | `clip_writer._transcribe`（`clip_writer.py:371`） | **あり** |
| 通常パイプライン（焼き込み） | `subtitle_generator` 焼き込み経路（`:1027`） | **あり** |
| DaVinci Resolve 出力 | 上記いずれかの字幕時刻を書き出す | **あり**（時刻が約 0.3 秒後ろへ寄る） |

いずれも「テロップが発話に合う方向」への変化であり、劣化する経路は無い。
ただし **既存の出力結果とはバイト一致しなくなる**ため §8 に明記する。

---

## 3. 方式選定

### 3-1. 直し方の比較 —— VAD の余白を削る（採用）

| 案 | 内容 | 判定 |
|---|---|---|
| **A** 一律オフセット減算 | 得られた start に `+0.3s` を足して後ろへずらす | **却下**。原因ではなく症状を隠す。ズレ量は `min(pad, 無音÷2)` で可変なので、一律に足すと無音が短い箇所で発話に食い込む |
| **B** 発話ゲートの復活 | 無効化済みの `gate_timeline_by_speech()` を戻し、音量閾値で発話区間へ丸める | **却下**。resolve7 で「無音カットの単一閾値へ一本化する」と決めた方針の逆戻り。ffmpeg 解析が 1 パス増えて遅くなる |
| **C** `speech_pad_ms` を縮める | 原因そのものを断つ。認識対象の音声から語頭の無音を外す | **採用**。1 行。ズレ量が可変である問題も同時に消える |
| **D** `word_timestamps` を切る | 先頭語 start ではなくセグメント境界を使う | **却下**。セグメント境界はさらに粗く、20260627 resolve.md「対策 A」で導入した改善を捨てることになる |

### 3-2. 削り幅は「300ms 削って 100ms」とする（D2/D4）

* 要望は「固定値で 300ms 削る」。**現在効いている値が 400ms** なので `400 - 300 = 100ms` とする。
* **0ms にしない理由**: Silero VAD の判定窓は 512 サンプル（16kHz で 32ms）刻みで、
  立ち上がりの弱い語（ハ行・サ行、小声の入り）では検出が数十 ms 遅れる。
  0 にすると語頭が認識音声から欠け、**テロップの文字自体が落ちる**恐れがある（D4）。
* 100ms は「VAD の検出遅れは吸収できるが、テロップの手前ズレとしては知覚されにくい」水準。
  字幕の早出しは 100ms 程度なら違和感が出にくい。
* 実機で語頭欠けが出た場合の戻し先は **200ms**（＝削り 200ms）。§9 Q1。

### 3-3. 固定値の置き場所 —— モジュール定数（D3/D6）

* `docs/claude.md` の制約「ハードコードは禁止し、設定可能な値は setting.json で管理する」と、
  要望 D3「設定項目としては出さない」が正面から当たる。
* 折衷として **モジュール先頭の名前付き定数**とし、由来と根拠をコメントへ残す。
  マジックナンバーを式へ直接書くことは避けつつ、設定キーは増やさない。
* 将来設定化する場合の受け皿は既に整っている。`whisper_vad_filter` /
  `whisper_vad_min_silence_ms` は **`setting.json` にはあるが設定画面には出していない**
  「ファイル専用キー」であり（`settings_window.py:193-194`。GUI 側に入力欄は無い）、
  同じ扱いで `whisper_vad_speech_pad_ms` を足すのは後から 3 行で済む。§11。

---

## 4. 設計方針

1. **原因の 1 点だけを直す**。テロップ時刻を後から補正する仕組みは足さない。
2. **既存の防御を壊さない**。VAD 付き認識が失敗したら VAD 無しで再試行する現行のフォールバック
   （`subtitle_generator.py:400-406`）はそのまま活かす。万一キーが受け付けられなくても
   テロップは全損せず VAD 無しで続く。
3. **ログで追えるようにする**。VAD 有効時のログへ余白値を出し、実機の挙動を後から検証できるようにする。
4. **設定ファイル・保存形式・UI は触らない**。
5. 数値の根拠（既定 400ms／削り 300ms／語頭欠け回避）は**コメントに残す**。

---

## 5. 詳細設計

### 5.1 変更ファイル一覧

| ファイル | 変更 | 行数目安 |
|---|---|---|
| `src/modules/subtitle_generator.py` | 定数追加 + `_transcribe()` の `vad_parameters` へ 1 キー追加 + ログ 1 行 | +8 / -3 |
| `tests/test_subtitle_vad_params.py`（新規） | `_transcribe()` が渡す VAD パラメータの検証 | 新規 40 行程度 |

**変更なし**: `setting.json` / `settings_window.py` / `clip_writer.py` /
`timeline_builder.py` / `builder.py` / `renderer.py` / `resolve_export.py` / モデル・保存形式。

### 5.2 定数の追加（`subtitle_generator.py` モジュール先頭付近）

```python
# Silero VAD が発話チャンクの前後へ足す余白 (ms) / resolve8 §2-3
# faster-whisper の既定は 400ms で、語頭側の無音まで認識対象へ含めてしまう。
# その無音へ先頭語の単語タイムスタンプが寄るため、テロップが発話より手前に出ていた。
# 既定から 300ms 削って 100ms とする (resolve8 §3-2):
#   - 0 にすると VAD の検出遅れで語頭が音声から欠け、テロップの文字自体が落ちる
#   - 100ms なら早出しとして知覚されにくく、検出遅れも吸収できる
# 語頭の取りこぼしが出るようなら 200ms へ戻す (resolve8 §9 Q1)。
_VAD_SPEECH_PAD_MS = 100
```

> 既存の定数（`_DEFAULT_ROLE` など）と同じくアンダースコア始まりのモジュール定数とし、命名を揃える。

### 5.3 `_transcribe()` の差分（`subtitle_generator.py:389-396`）

```python
        if self._vad_filter:
            # 内蔵 Silero VAD で無音/非発話区間を認識対象から除外する
            kwargs["vad_filter"] = True
            # speech_pad_ms を明示する。未指定だと既定 400ms で語頭の無音まで
            # 認識対象に入り、テロップが発話より手前に出る (resolve8 §2-3)。
            kwargs["vad_parameters"] = {
                "min_silence_duration_ms": self._vad_min_silence_ms,
                "speech_pad_ms": _VAD_SPEECH_PAD_MS,
            }
            _logger.info(
                "VAD フィルタ有効 (min_silence=%dms, speech_pad=%dms, "
                "condition_on_previous_text=%s)",
                self._vad_min_silence_ms, _VAD_SPEECH_PAD_MS,
                self._condition_on_previous_text,
            )
```

* `WhisperModel.transcribe()` は dict をそのまま `VadOptions(**vad_parameters)` へ展開する
  （`faster_whisper/transcribe.py:889`）ため、このキー名で確実に届く。
* 例外時の VAD 無しフォールバック（`:400-406`）は既存のまま。`kwargs.pop("vad_parameters")` が
  効くので、追加キーが原因で失敗しても安全に縮退する。

### 5.4 変わらないもの（意図的に触らない）

| 対象 | 理由 |
|---|---|
| `_speech_bounds()`（先頭語 start 採用） | 認識対象から余白が消えるので、この実装のまま意図どおりに効く |
| `adjust_display_timing()` | 開始は動かさず終了だけ整形する現行仕様で正しい。開始が約 0.3 秒後ろへ寄るぶん、**前のテロップが同じだけ長く残る**（R3 の「次発話まで保持」）ため、画面から字幕が消える空白は増えない |
| `map_items_to_timeline()` | 変更不要。むしろ start が発話寄りになることで、**無音カット区間に食い込んで切り詰められる字幕が減る** |
| `display_min_duration_sec` (0.5) | 終端の下限であり本件と無関係。値は据え置き |
| `speech_pad_sec` / `speech_gate_*` | 無効化済み経路。据え置き（§11） |

---

## 6. 実装手順

| Phase | 内容 | 完了条件 |
|---|---|---|
| **1** | 定数追加 + `_transcribe()` の 1 キー追加 + ログ | `tests/test_subtitle_vad_params.py` が通る。ログに `speech_pad=100ms` が出る |
| **2** | 実機確認（アーカイブ切り抜き 1 本） | §10-1 の実測でテロップ開始が発話頭 ±0.15 秒に入る。語頭欠けが無い |

Phase 2 で語頭欠けが観測された場合のみ定数を 200 へ変更する（他の変更は不要）。

---

## 7. setting.json 定義（追加分）

**追加なし。** 要望 D3 のとおり設定項目は増やさない。既存キーの値も変更しない。

```jsonc
// 参考: 本修正で「効いている値」が変わるのは以下だけ (ファイルには現れない)
// subtitle.whisper_vad_filter = true のとき
//   speech_pad_ms : 400 (faster-whisper 既定) → 100 (コード定数)
```

---

## 8. 互換性・非破壊の担保

| 観点 | 担保 |
|---|---|
| 設定ファイル | 読み書きとも変更なし。既存 `setting.json` はそのまま使える |
| 保存形式（`*.timeline.json`） | スキーマ変更なし。既存プロジェクトはそのまま開ける |
| VAD 無効時（`whisper_vad_filter=false`） | `vad_parameters` を組み立てないため**完全に現行どおり** |
| 認識エンジンが whisper 以外 / `engine=none` | 通らない経路。影響なし |
| 認識失敗時 | 既存フォールバック（VAD 無しで再試行）を維持 |
| **出力の同一性** | **維持されない**。テロップの開始時刻が約 0.3 秒後ろへ寄るため、焼き込み動画・ASS・Resolve 出力の内容が変わる。これは本要望そのものであり意図した変化 |
| 切り戻し | 定数を 400 に戻せば完全に現行挙動へ戻る（1 行） |

---

## 9. 確認事項

| No | 内容 | 本書の既定 |
|---|---|---|
| **Q1** | 削り幅は 300ms（→ 残 100ms）でよいか。語頭欠けが出た場合は 200ms へ戻す方針でよいか | 100ms 採用。実機確認後に見直し |
| **Q2** | 焼き込み・Resolve 出力を含む**全経路**が同時に変わるが問題ないか（アーカイブ用だけへ限定することも可能だが、認識層を分岐させる必要があり複雑になるため推奨しない） | 全経路で適用 |
| **Q3** | `speech_gate_enabled` が「設定は生きているが実装は無効」の状態で残っている。設定画面から外すか、実装を復活させるか | 本書では触らない（§11） |
| **Q4** | 将来 `subtitle.whisper_vad_speech_pad_ms` として設定化するか（ファイル専用キーとして。GUI へは出さない） | 今回は行わない |

---

## 10. テスト計画

### 10-1. 実機確認（Phase 2）

1. アーカイブ切り抜きを 1 本実行し、Timeline を開く。
2. 波形の発話立ち上がりと、字幕トラックのクリップ左端を目視で比較する。
   * **合格**: 開始が発話頭の手前 0.15 秒以内（現状は 0.3〜0.5 秒手前）。
   * **不合格の型 A**: まだ大きく手前に出る → 原因が他にある。定数を 0 にして切り分ける。
   * **不合格の型 B**: 語頭の文字が落ちる（「そうですね」→「うですね」等）→ 定数を 200 へ。
3. 修正前後で同じ素材を通し、`logs/` の
   「テロップ表示タイミング整形: N → M 区間」の件数が大きく減っていないことを確認する
   （減る＝認識できなくなっている兆候）。

### 10-2. 単体テスト（新規 `tests/test_subtitle_vad_params.py`）

`WhisperModel` をダミーへ差し替え、`_transcribe()` が渡す kwargs を検証する。

| ケース | 期待 |
|---|---|
| `whisper_vad_filter=true` | `vad_parameters == {"min_silence_duration_ms": 500, "speech_pad_ms": 100}` |
| `whisper_vad_min_silence_ms=800` | `min_silence_duration_ms` が 800 になり、`speech_pad_ms` は 100 のまま |
| `whisper_vad_filter=false` | `vad_filter` / `vad_parameters` がどちらも kwargs に無い |
| 1 回目が例外を投げる | 2 回目は `vad_filter` / `vad_parameters` を外して呼ばれる（既存フォールバックの回帰） |

### 10-3. 既存テストの回帰

`tests/test_subtitle_timeline_map.py` / `tests/test_archive_prepare.py` /
`tests/test_timemap.py` は認識結果を固定値で与えているため**影響を受けない**。
実行して緑であることだけ確認する。

---

## 11. 将来拡張（本書では実装しない）

* **`subtitle.whisper_vad_speech_pad_ms` として設定化**する。`whisper_vad_filter` /
  `whisper_vad_min_silence_ms` と同じ「`setting.json` にはあるが設定画面には出さない」
  ファイル専用キーとして `DEFAULT_SETTINGS` へ追加すれば、`load_settings()` の既定マージで
  既存ファイルにも自動補完される。
* **`speech_gate_enabled` の整理**。実装が無効化されたまま設定だけ残っており、
  利用者からは効いて見える。設定画面から外すか、コメントアウトを削除して意図を確定させる。
* **語頭の追い込み**。さらに精度を上げるなら、単語タイムスタンプの先頭語だけを
  音量エンベロープで再吸着させる案があるが、ffmpeg 解析が 1 パス増えるため費用対効果は低い。
