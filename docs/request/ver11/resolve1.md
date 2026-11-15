# ver11 resolve1 — カット閾値の自動推定が低域ノイズで崩れる問題の修正

要望: 「S5 の閾値推定が -37dB を返す原因を調べ、修正を実装する」(2026-11-12)
前提: `docs/request/ver10/result_ss1.md` §12 (原因の調査と対策案の比較 / 実測値はすべてここ)
　　　`docs/quality/resolve1.md` §2.3 F7 (`metric="percentile"` を入れた経緯)
　　　`docs/CLAUDE.md` (実装前に設計 / ハードコード禁止 / 既存実装を破壊しない)
対象: `src/modules/volume_analyzer.py` の `analyze_min_speech_db`
状態: **実装済み** (2026-11-12)

---

## 0. 先に結論

**「ピークが立たないが常に鳴っている音」が発話候補区間に混ざり、カット閾値を
ノイズフロアまで引き下げていた。** 区間の `max` (ピーク音量) に下限を設けて外す。

| 素材 | 修正前 | 修正後 | 備考 |
| --- | --- | --- | --- |
| **S5** (F1 視聴 / 低域が強い) | **-37dB** | **-19dB** | 残存率 0.784 → 0.33 前後 / 調和平均 45.3% → 91.4% (-20dB 実測値) |
| S3 (F1 視聴) | -21dB | **-21dB** | **変わらない** (正解データでの最良値のまま) |
| S1 (IRL / 無音率 0%) | -17dB | **-17dB** | **変わらない** |

**壊れている 1 素材だけが直り、正常な 2 素材は 1dB も動かない。**

---

## 1. 現状の仕組み

`analyze_min_speech_db` は 4 段で「発話区間の代表 dB」を求め、それをカット閾値にする。

```text
① base_noise_db (既定 -30dB) で無音検出 → その補集合を「発話候補区間」にする
② min_region_sec (0.8 秒) 以下の区間を捨てる / max_regions (100) まで長い順に標本化
③ 各区間の代表音量を測る (metric="mean" なら mean_volume)
④ その下側分位点を採る (metric="percentile" / percentile=10)
```

④ の分位点は `docs/quality/resolve1.md` §2.3 F7 で入れたもので、
「1 区間でも静かなものがあると最小値が崩壊する」問題への対策である。これ自体は正しく働く。

## 2. 原因 (実測は ver10 `result_ss1.md` §12)

**① の無音判定は瞬間値で行われるため、「ピークは立たないが常に鳴っている音」を
無音と判定できない。** その区間が発話候補に入り、③④ の母集団を汚染する。

S5 の発話候補 70 区間を `max` で分けると二峰にきれいに分かれた。

| | 区間数 | 合計 | mean の範囲 | **max の範囲** |
| --- | --- | --- | --- | --- |
| **非発話 (低域ノイズ)** | **19 区間 (27%)** | 53.6s | -38.2 〜 -31.8 dB | **-28.8 〜 -23.7 dB** |
| 発話 | 51 区間 | 116.6s | -19.8 〜 -10.9 dB | **-4.9 〜 -1.1 dB** |

`max` に **19dB の空白**がある (-23.7 と -4.9 の間)。
人の声は子音・母音の立ち上がりでピークが立つが、定常ノイズは立たないためである。

候補の 27% がノイズなので、**10 パーセンタイルが完全にノイズ領域 (-37.2dB) に落ちていた。**
`percentile=10` は「少数の外れ値」には足りるが、**27% の汚染には足りない。**

> **なぜ `percentile` を上げる案を採らないか**
> `percentile=30` にすれば S5 は -20dB になるが、**S3 が -21dB → -20dB に動く**。
> S3 は正解データで最良閾値が -21dB だと確かめてある (`result_ss1.md` §11.3) ため、
> これは既存の正常な素材を悪くする。`max` で絞る案は S1 / S3 を 1dB も動かさない。

## 3. 対策

**③ で代表音量を集める前に、`max` が下限を下回る区間を母集団から外す。**

| | 決め |
| --- | --- |
| 設定キー | `volume_analysis.min_region_peak_db` |
| 既定値 | **-20** (dBFS)。S5 の空白 (-23.7 〜 -4.9) の内側で、発話側に寄せすぎない値 |
| 無効化 | `null` / 空文字 / キー無しで**従来動作** (切り戻し経路) |
| 効く範囲 | ③ の母集団のみ。① の無音検出 (`base_noise_db`) と ② の長さ条件は変えない |
| 安全弁 | **全区間が外れた場合は従来どおり全区間で算出する** (警告ログを出す)。素材全体が静かな場合に候補が消えて `min_db=None` になるのを防ぐ |
| 記録 | 戻り値へ `dropped_quiet_count` を足す (何区間外したか)。既存キーは変えない |

`max` の空白が 19dB あるため、-25dB 〜 -10dB のどこで切っても S5 の結果は同じになる
(`result_ss1.md` §12.3 の実測)。既定 -20dB はその中央寄りである。

### 3.1 2 つの測定経路の両方に効かせる

`analyze_min_speech_db` は区間の音量を 2 通りの方法で得る。**どちらも `max` を返す**ので、
同じ判定を 1 か所に書けば両方に効く。

| 経路 | 取得方法 | max の出どころ |
| --- | --- | --- |
| 通常 | `measure_region_volume` (区間ごとに `volumedetect`) | `max_volume` |
| 1 パス解析 (`scan`) | `_region_levels_from_scan` → `archive.features.region_levels` | 1 秒ごとの `peak` の最大値 |

## 4. 実装

`src/modules/volume_analyzer.py` の `analyze_min_speech_db` ③ のループを変更した。

```python
# ピーク下限 (None で従来動作)
configured_peak = va_cfg.get("min_region_peak_db")
peak_floor = (_to_float(configured_peak, _DEFAULT_MIN_REGION_PEAK_DB)
              if configured_peak not in (None, "") else None)

values = []          # ピーク下限を満たした区間の代表音量
fallback_values = [] # 全区間 (安全弁)
dropped_quiet = 0
for (s, e) in regions:
    ...
    fallback_values.append(value)
    if peak_floor is not None and max_db is not None and max_db < peak_floor:
        dropped_quiet += 1
        continue
    values.append(value)

if not values and fallback_values:
    # 全部外れた = 素材全体が静か。従来どおり全区間で算出する
    values = fallback_values
    dropped_quiet = 0
```

### 4.1 変更したファイル

| ファイル | 変更 |
| --- | --- |
| `src/modules/volume_analyzer.py` | 上記。`_DEFAULT_MIN_REGION_PEAK_DB = -20.0` を追加。ログに外した区間数を出す |
| `src/settings/setting.json` | `volume_analysis.min_region_peak_db: -20` を追加 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` に同項目 (コメント付き) |
| `docs/design/design.md` | `setting.json` 定義の `volume_analysis` の説明を更新 |
| `tests/test_threshold_peak_floor.py` | 新規。下の 8 件 |

### 4.2 テスト

`scan` 経路 (1 パス解析) を使うと **ffmpeg を 1 回も呼ばずに** `analyze_min_speech_db` を
通せるため、合成した 1 秒値で検証している。

| テスト | 内容 |
| --- | --- |
| `test_noise_regions_drag_threshold_down` | ピーク下限**無し**では、ノイズ区間が混ざって閾値が -38dB に落ちる (問題の再現) |
| `test_peak_floor_excludes_noise_regions` | ピーク下限 -20dB で発話区間だけが残り、閾値が -18dB になる |
| `test_dropped_count_is_reported` | `dropped_quiet_count` に外した区間数が入る |
| `test_floor_is_insensitive_within_the_gap` | 下限を -25 / -20 / -15 / -10dB と変えても結果は同じ (`max` の空白の内側なら鈍感) |
| `test_all_regions_quiet_falls_back` | 全区間が下限未満なら**従来どおり全区間で算出**する (安全弁) |
| `test_absent_setting_keeps_legacy_behaviour` | キー無し / `null` / 空文字で従来と同じ値になる (切り戻し) |
| `test_speech_only_material_is_untouched` | 発話だけの素材では 1 区間も外れない (正常な素材を壊さない) |
| `test_invalid_setting_uses_default` | 不正値は既定 (-20) として扱う |

**実行結果 (2026-11-12): 新規 8 件すべて OK / 既存を含む全 2,327 件 OK。**

### 4.3 実素材での確認

`tools/splitsound/` の共通入力 (`analysis_source.wav`) に対して
`analyze_min_speech_db` を修正前後で回した実測。

| 素材 | 修正前 | 修正後 | 外した区間 |
| --- | --- | --- | --- |
| S1 (IRL / 無音率 0%) | -17 dB | **-17 dB** | 0 / 1 区間 |
| S3 (F1 視聴) | -21 dB | **-21 dB** | 0 / 48 区間 |
| **S5 (F1 視聴 / 低域が強い)** | **-37 dB** | **-19 dB** | **19 / 70 区間** |

修正前の値は `ver10 result_ss1.md` の記録 (S1 -17 / S3 -21 / S5 -37) と一致した。
**壊れていた S5 だけが直り、正常な 2 素材は 1dB も動いていない。**

## 5. 既存への影響

| | |
| --- | --- |
| 正常な素材 | **閾値は変わらない。** S1 (-17dB) / S3 (-21dB) で 1 区間も外れないことを実測済み (`result_ss1.md` §12.3) |
| アーカイブ切り抜き | `extract_cells` の採点は別経路 (`region_levels` を直接使う) で、本変更は通らない |
| 保存済みの設定 | キーが無ければ `DEFAULT_SETTINGS` の -20 が使われる。従来動作に戻したい場合は `null` を入れる |
| 配布物 | 設定 1 項目の追加のみ。依存ライブラリは増えない |

## 6. 残課題

- **S5 の正解データが無い。** 効果の確認は代理指標 (調和平均 45.3% → 91.4%) と
  「残存率 0.784 は発話率 32.3% に対して 2.4 倍残しすぎ」という事実に基づく。
  M1 / M2 での検証には S5 の正解データが要る (`result_ss1.md` §8.1 #3)。
  代理指標の**絶対値**は信用できない (S3 で最良閾値を 9dB 外した / 同 §11.4)。
- **残す区間の前後の余白 (パディング) は未着手。** S3 の recall は 0.7023 で、
  残す区間が発話の 70% しか覆えていない。これは閾値では動かない別課題
  (`result_ss1.md` §8.1 #5)。
