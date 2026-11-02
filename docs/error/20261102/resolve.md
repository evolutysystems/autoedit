# 無音カットが効かない（カット閾値 -102 dB への崩壊）原因と修正設計書

現象の報告: 利用者より
> なんか無音カットができていないように感じます。
> 発話の最小平均dbを基準にカットできていますか？ video は vod_2916963434 です。

本書は `docs/CLAUDE.md`「実装前に設計を行うこと」に従う **設計書** であり、
実装は本書のレビュー後に行う。

対象ログ: `%LOCALAPPDATA%\Programs\AutoEdit\logs\autoedit_20261102.log`（インストール版 v1.7.1）
対象素材: `%LOCALAPPDATA%\Stretheus\archive_work\vod_2916963434.mp4`（10249.9s / 1280x720 / AAC 48kHz）

---

## 1. 結論（先に要点）

「発話区間の最低dBを基準にカットする」仕組み自体は設計どおり動いている。
壊れているのは **その最低dBを測る側**、`src/archive/features.py` の全長 1 パス解析である。

```python
# src/archive/features.py:98-105  scan_audio()
audio_filter = (
    "silencedetect=noise={noise}dB:d={dur},"
    + "asetnsamples=n={n}:p=0,"
    + "astats=metadata=1:reset=1,"
    + "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-,"   # ← 1 本目
    + "ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-"   # ← 2 本目
)
```

`ametadata=print:file=-` を **2 本**並べているため、2 つのフィルタが
**同じ標準出力をそれぞれ独自のバッファで書く**。約 830 行ごと（バッファ境界）に
行が途中で混ざって壊れ、値が落ちる。

`_parse_per_sec` は RMS と Peak を**出現順に突き合わせ、配列インデックス＝秒**と
みなすため、落ちた分だけ**時刻が後ろへずれ続ける**。
結果、本来は普通に発話している区間へ無音部の値が割り当たり、
`metric="mean"`（最小値採用）がその 1 件を拾って閾値が **-102 dB** まで崩壊した。

-102 dB では silencedetect が何も無音と判定しないため、
**全クリップで「無音 0 件」＝無音カットが 1 秒も効いていない**。

---

## 2. ログに残っている事実

```
10:32:53 全長 1 パス解析: 10249.9s / 1 秒値 10241 件 / 無音区間 1948 件
10:32:54 測定区間を標本化: 1385 → 100 区間 (長い順)
10:32:54 発話区間の代表dB=-102 (mean, 測定 100/1385 区間 / 1 パス解析から)
10:32:54 VOD 全体のカット閾値 -102 dB を全クリップで共有 (F7)
10:34:09 clip1: VOD 全体のカット閾値 -102 dB を使用 (F7)
10:34:09 編集点検出: 無音 0 件 / 残す区間 1 件 (閾値=-102dB, 最小無音=0.60s, 総尺=814.1s)
10:46:59 編集点検出: 無音 0 件 / 残す区間 1 件 (閾値=-102dB, 最小無音=0.60s, 総尺=302.1s)
10:52:28 編集点検出: 無音 0 件 / 残す区間 1 件 (閾値=-102dB, 最小無音=0.60s, 総尺=302.1s)
10:58:55 編集点検出: 無音 0 件 / 残す区間 1 件 (閾値=-102dB, 最小無音=0.60s, 総尺=301.5s)
```

注目点は **「1 秒値 10241 件」** である。
音声ストリームは 10249.87s / 480464 AAC フレーム（= 491,995,136 サンプル / 48000Hz）で
連続しており、`asetnsamples=n=48000` なら **10250 件**出るのが正しい。
9 件足りないこと自体が、出力が壊れている証拠である。

---

## 3. 実測による原因の特定

同じフィルタを同じ素材へ手で流し、標準出力を保存して数えた。

| 項目 | 実測 |
|---|---|
| 実フレーム数（= 本来の 1 秒値の件数） | **10250** |
| `RMS_level=` の行数 | 10246 |
| `Peak_level=` の行数 | 10248 |
| `_parse_per_sec` が取れた件数 | 10241 |
| 書式が壊れた行 | 多数（約 830 行ごと） |

壊れ方の実例（1 行の中で 2 本の出力が混ざっている）:

```
lavfi.astats.Overall.RMS_level=-tats.Overall.Peak_level=-34.485645
lavfi.astats.Overall.Peak_level=-4avfi.astats.Overall.RMS_level=-24.883228
frame:4verall.Peak_level=-17.720492
lavfi.as18  pts:20064000 pts_time:418
```

### 3.1 これが代表dBを壊す経路

`features.region_levels(per_sec, start, end)` は

```python
lo = max(int(math.floor(float(start) / _SCAN_STEP_SEC)), 0)   # _SCAN_STEP_SEC = 1.0
hi = min(int(math.ceil(float(end) / _SCAN_STEP_SEC)), len(per_sec))
```

と、**インデックスをそのまま秒として扱う**。
値が 9 件落ちていれば後半は最大 9 秒ずれる。実際にずれた結果が次である。

```
区間 9293.9-9298.3 (4.4s)  mean=-101.7 dB  1秒値=[-106.7, -117.9, -119.3, -119.1, -112.4, -94.2]
```

この区間は `silencedetect=noise=-30dB:d=0.3` で**無音と判定されなかった区間**、
すなわち「発話候補」である。発話候補の中身が -119 dB（ほぼデジタル無音）に
なること自体が矛盾しており、読み出し位置がずれていることを示す。
-119 dB の実体は VOD 冒頭 0〜45s の無音部の値である（silencedetect も
`silence_start: 0 / silence_end: 45.040625` を出している）。

### 3.2 最小値採用が崩壊を増幅する

`src/modules/volume_analyzer.py:146`

```python
representative = min(values)      # metric="mean" / "max" のとき
```

標本 100 区間のうち壊れた値は 1 件でも、最小値採用なのでそれが即採用される。

| 標本 100 区間の mean_db（壊れたまま） | 値 |
|---|---|
| 最小値 | **-101.7** |
| 下位 10% 点 | -33.3 |
| 中央値 | -26.7 |
| 最大値 | -23.2 |

下位 10 件: `-101.7, -77.8, -71.1, -67.2, -64.7, -55.4, -54.6, -52.4, -42.8, -39.0`

---

## 4. 二次原因: `volume_analysis.metric` の旧値が残り続ける

`docs/quality/resolve1.md` F7 は「**最小値採用による崩壊**を止める」ために
`metric="percentile"` を追加し、リポジトリの既定も `percentile` になっている。

```python
# src/settings/settings_window.py:482-496  DEFAULT_SETTINGS
"metric": "percentile",   # 下側分位点を採る (推奨)。外れ値で崩壊しない
"percentile": 10,
```

しかし利用者のインストール版 `setting.json` は `"metric": "mean"` のままである。

```
C:\Users\evolu\AppData\Local\Programs\AutoEdit\_internal\src\settings\setting.json
  "metric": "mean",
  "percentile": 10,
  "last_cut_db": -36
```

`load_settings` → `_merge_with_defaults` は**既存のユーザー値を温存する**ため、
キーが既に存在する `metric` は新既定へ入れ替わらない。
`_normalize_legacy_values` にも `metric` の項目が無い。
したがって **v1.8.1 へ更新しても `"mean"` のまま**で、F7 の対策が効かない。

これは FFmpeg 実行ファイルの裸名（error 20260709）や
`subtitle.comment_label`（ver3 resolve11）と**同じ型の不具合**である。

---

## 5. 修正後の値（検証済み）

`ametadata` を 1 本にまとめ、`pts_time` で秒へ対応付ける方式を手で流して確かめた。

```
-af silencedetect=noise=-30dB:d=0.3,
    asetnsamples=n=48000:p=0,
    astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level+RMS_level,
    ametadata=print:file=-
```

出力は 1 フレームにつき必ず 3 行で、壊れた行は 1 件も無い。

```
frame:0    pts:0       pts_time:0
lavfi.astats.Overall.Peak_level=-107.506452
lavfi.astats.Overall.RMS_level=-119.362262
```

| | 現状 | 修正後 |
|---|---|---|
| 1 秒値の件数 | 10241 / 10250（破損あり） | **10250 / 10250（破損なし）** |
| RMS 有効値 | — | 10250 |
| 最小値（metric=mean 相当） | **-101.7 dB** | **-30.4 dB** |
| 下位 10% 点（metric=percentile） | -33.3 dB | **-26.4 dB** |

全 1385 区間の分布（修正後）: `min=-37.9 / p10=-28.6 / p25=-26.8 / median=-25.5`

### 5.1 カット量の見積もり

clip1 の範囲（88〜900s / 812s）へ実際に silencedetect を流した結果:

| 閾値 | 無音件数 | 無音合計 | 区間に対する割合 |
|---|---|---|---|
| -30 dB（修正後 metric=mean） | 180 件 | 433.8s | **53.4%** |
| -26 dB（修正後 metric=percentile） | 191 件 | 453.1s | **55.8%** |
| -102 dB（現状） | **0 件** | 0.0s | **0.0%** |

本来 812s → 約 360s に詰まるべきものが、まったく詰まっていない。
（本番は正規化後クリップ（+約 6.6 dB）を測るため絶対値は上に寄るが、相対関係は同じ）

---

## 6. 影響範囲

| 機能 | 影響 | 理由 |
|---|---|---|
| 無音カット（編集点検出） | **全面的に無効化** | 閾値 -102 dB → 無音 0 件 |
| アーカイブ採点の感情スコア | **ずれる** | `_cells_from_scan` が同じ `per_sec` から `mean_db` / `max_db` を作る |
| セルの無音率 `silence_ratio` | 影響なし | 標準エラーの silencedetect ログから作るため破損しない |
| `feature_mode="per_cell"`（切り戻し） | 影響なし | `volumedetect` をセル毎に起動する別経路 |
| 保存済みプロジェクト | 残らない | `per_sec` はメモリ内のみ（`archive_tab` の `_vod_scan`）。再採点で回復する |
| ラウドネス正規化 | 影響なし | `loudnorm` の別測定 |

---

## 7. 修正設計

### R1. `ametadata` を 1 本に統合する（原因の除去）

`src/archive/features.py:98-105`

- `ametadata=print:key=...` 2 本を **`ametadata=print:file=-` 1 本**へ置き換える。
- 代わりに `astats` 側で出力キーを絞る:
  `astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level+RMS_level`
  - キーを絞らないと 1 フレームにつき約 30 キー出て標準出力が肥大するため必須。
  - 同梱 FFmpeg（`src/ffmpeg/ffmpeg.exe` / gyan 2026-03-12 build）で動作確認済み。
  - 万一このオプションを解さないビルドでは `scan_audio` が `FFmpegError` を投げ、
    `extract_cells` の既存 `except` が**セル別測定へ落ちる**（現状と同じ安全弁）。
- 併せてキーの分割出力を前提にしたコメント（`features.py:19` / `:80`）を直す。

### R2. 秒の対応付けを `pts_time` 基準にする（暗黙前提の廃止）

`src/archive/features.py:146-151` `_parse_per_sec`

- `frame:<n>  pts:<n>  pts_time:<sec>` のヘッダ行を読み、
  `index = int(round(pts_time / _SCAN_STEP_SEC))` で**その秒へ直接格納**する。
- 欠損した秒は `{"rms": None, "peak": None}` の穴として残す
  （`energy_mean_db` は `None` を弾くため、穴があっても平均は壊れない）。
- **ヘッダ行が 1 件も無い入力は、従来どおり出現順で対応付ける**
  （`tests/test_audio_scan.py` の既存ケースを壊さないための後方互換）。
- 追加する正規表現（既存 2 本と並べて置く）:
  `_RE_SCAN_FRAME = re.compile(r"^frame:\d+\s+pts:\S+\s+pts_time:(-?[0-9.]+)")`
- `region_levels` / `_cells_from_scan` は変更不要（インデックス＝秒が正しくなるため）。

### R3. `metric` の旧既定値を現行既定へ正規化する

`src/settings/settings_window.py` `_normalize_legacy_values`

- `_migrate_volume_metric(merged)` を追加し、`_normalize_legacy_values` から呼ぶ。
- **旧既定値 `"mean"` のときだけ** `"percentile"` へ寄せる。
  `"max"` は利用者が意図して選べる値なので触らない
  （`comment_label` の移行と同じ「既知の旧既定値だけ」という方針）。
- 既存の仕組みに乗るため、次回起動時に `setting.json` へ 1 度だけ書き戻される。

### R4. 閾値の下限ガード（**不採用** — 利用者判断により実装しない）

R1/R2 で破損は消えるが、「発話候補区間の代表dBが、その区間を切り出すのに使った
`base_noise_db` を大幅に下回る」のは**定義上ありえない**ため、保険を置きたい。

- `src/modules/volume_analyzer.py` `analyze_min_speech_db` の末尾で、
  `representative < base_noise_db + cut_db_floor_margin_db` なら
  **採用せず `min_db=None` を返し WARNING を出す**（呼び出し側は保存済み
  `last_cut_db` で継続する既存経路に乗る）。
- 新設定 `volume_analysis.cut_db_floor_margin_db`（既定 **-10**）。
  ハードコード禁止の方針に従い `setting.json` と `DEFAULT_SETTINGS` へ持つ。
  既定 -10 なら下限は -40 dB となり、本件の実測値（min -37.9 / p10 -28.6）には触らない。
- 値を黙って丸める（クランプ）案は採らない。誤った測定を気づかせないため。

### R5. テスト

`tests/test_audio_scan.py` へ追加（FFmpeg は起動しない。文字列を食わせるだけ）。

1. `pts_time` ヘッダ付きの出力を、飛び番込みで正しい秒へ入れること
2. ヘッダ無しの旧形式は従来どおり出現順で対応付くこと（後方互換）
3. 行が壊れた出力（本件の実物を縮めたもの）を食わせても、
   **壊れた行を捨てて残りの秒が正しい位置に入ること**
4. R3 の移行: `metric="mean"` が `"percentile"` になり、`"max"` は変わらないこと

---

## 8. 既存実装への影響と互換性

- `scan_params` / `scan_matches` の照合キー（`noise_db` / `min_silence_sec` / `step_sec`）は
  変更しない。解析の測定条件は変わらないため、引き継ぎの判定に影響しない。
- 標準エラー側（silencedetect）の扱いは一切変えない。
  `ametadata` を無指定にすると silencedetect のメタデータ
  （`lavfi.silence_end` / `lavfi.silence_duration`）も標準出力へ出るが、
  正規表現が一致しないため無視される。`silence_start` はメタデータに出ないので、
  無音区間は従来どおり標準エラーから読む。
- `volumedetect` 経路（`measure_region_volume` / `feature_mode="per_cell"`）は無変更。
- 出力物の仕様変更は無い。既定値が変わるのは R3（`metric`）と R4（新規キー）のみ。

---

## 9. 検証条件（実装後に確認する）

| # | 確認内容 | 方法 |
|---|---|---|
| 1 | 1 秒値の件数が音声尺と一致する | vod_2916963434 で「1 秒値 10250 件」になること |
| 2 | 代表dBが妥当な範囲へ戻る | 「発話区間の代表dB=-26〜-30」になること |
| 3 | 無音カットが効く | clip1 で「無音 180 件前後」が出て、尺が約 45% に詰まること |
| 4 | 採点が変わること自体を確認 | 修正前後で TOP セクションが変わり得る（ずれが直るため正常） |
| 5 | 旧 `setting.json` の自己修復 | `metric="mean"` の設定で起動 → `percentile` へ書き換わること |
| 6 | 切り戻し経路 | `feature_mode="per_cell"` で従来どおり動くこと |

---

## 10. 未確定事項（推測実装しない項目）

1. **R4 を入れるか** → **不採用**（利用者判断）。実装しない。
2. **`metric` の推奨値** → R3 どおり `percentile` へ寄せる。
   本件の修正後は `mean`（最小値 -30.4 dB）でも `percentile`（-26.4 dB）でも
   成立するが、外れ値 1 件で崩壊しない側を既定とする。
   カットの強さは -26 dB の方が約 2.4 ポイント強い。
   移行済みフラグ (`volume_analysis.metric_migrated`) を持つため、
   利用者が `"mean"` へ戻した場合は二度と書き換えない。
3. **今回の vod_2916963434 の書き出しをやり直すか**。利用者の回答は
   「今は再実行しない」。解析結果は保存されないため、再採点すれば回復する。

---

## 11. 実装記録（2026-11-02）

R1 / R2 / R3 / R5 を実装し、R4 は不採用とした。

| 変更 | ファイル | 内容 |
|---|---|---|
| R1 | `src/archive/features.py` | `ametadata=print:file=-` を 1 本へ統合し、`astats` に `measure_perchannel=none:measure_overall=Peak_level+RMS_level` を付けた |
| R2 | `src/archive/features.py` | `_parse_per_sec` を `frame` ヘッダの `pts_time` 基準へ変更（`_parse_per_sec_by_frame` / `_parse_per_sec_in_order` / `_scan_index` を追加）。値の行は行頭〜行末の完全一致で拾い、壊れた行は捨てる |
| R3 | `src/settings/settings_window.py` | `_migrate_volume_metric` を追加し `_normalize_legacy_values` から呼ぶ。`volume_analysis.metric_migrated`（既定 false）を DEFAULT_SETTINGS と配布 `setting.json` へ追加 |
| R5 | `tests/test_audio_scan.py` | `_parse_per_sec` のテストを 5 件追加（pts_time 基準 / 飛び番の穴 / 壊れた行 / silencedetect メタデータ混入 / CRLF） |
| R5 | `tests/test_settings_migration.py` | `VolumeMetricMigrationTest` を追加（7 件） |

### 11.1 検証結果

§9 の確認条件を実機（vod_2916963434）とテストで確認した。

| # | 確認内容 | 結果 |
|---|---|---|
| 1 | 1 秒値の件数が音声尺と一致する | **10250 件 / 音声尺 10249.9s / 穴 0 件**（修正前 10241 件） |
| 2 | 代表dBが妥当な範囲へ戻る | **percentile: -26 dB / mean: -30 dB**（修正前 -102 dB） |
| 3 | 無音カットが効く | 閾値 -26 dB・最小無音 0.6s で clip1 (812s) に無音 **191 件 / 453.1s (55.8%)** |
| 5 | 旧 `setting.json` の自己修復 | `VolumeMetricMigrationTest` で確認 |
| 6 | 切り戻し経路 | `feature_mode="per_cell"` の既存テストが通る |

テストは `python -m unittest discover -s tests` で **2211 件すべて OK**。
