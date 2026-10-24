# quality resolve1 — アーカイブ用切り抜きの品質改善設計

対象: `autoedit` (Stretheus クライアント) の**アーカイブ用切り抜き経路のみ**
(`src/archive/` + `archive.clip_pipeline.timeline_review=true` の Timeline 経路)
要望元: `docs/quality/qua1.md` (自動カット / 自動文字起こし / 音量調整)
関連: `docs/request/ver8/resolve6.md` (テロップ単位・カット頭整列),
      `docs/error/20260811/resolve.md` §2-3・§4-2 (切り抜きの字幕軸),
      `docs/error/20260626/resolve.md` (幻聴字幕),
      `docs/request/ver3/resolve16.md` (ストリームマーカー),
      `docs/request/ver3/resolve19.md` (テーマ演出・イントロカード)

作成日: 2026-10-23 (main `e38e085`)

> **本書の範囲**: 通常の 1 本編集 (`pipeline_runner`) ではなく、
> **アーカイブタブから VOD を採点して TOP クリップを切り抜く経路**だけを対象とする。
> 切り抜き経路は本流と同じ `silence_cutter` / `subtitle_generator` / `loudness_normalizer` を
> 呼ぶが、**呼び方と順序が違う**ため、本流には無い固有の問題が重なっている。

---

## 0. 結論

切り抜き経路の品質は「**どの区間を選ぶか (採点)**」で決まり、その採点が
**音量だけで盛り上がりを判定している**ため、要望の 3 点はいずれもここに帰着します。

| # | 要望 | 切り抜き経路での答え | 根本原因 |
| --- | --- | --- | --- |
| 1 | 発話を認識し、配信者が喋っているタイミングで切れているか | **できていない** | 採点の「感情点」は正規化した `mean_db` の平均 + 大声セル加点 − 無音セル減点のみ。声か否かを区別しない (§2.2 A1) |
| 2 | ゲームや他の音声を含めていないか | **区別していない。むしろゲーム音が高得点を取る** | 45 秒セルの `max_db` は単発の効果音・爆発音で決まり、`silence_ratio` はゲーム音で常に 0 になるため減点が効かない (§2.2 A2) |
| 3 | 文字起こしの精度向上に必要なものは何か | **(a) 固有名詞辞書 (b) 信頼度の可視化 (c) 置換辞書** | faster-whisper の精度系パラメータを既定のまま使っている (§3.2) |
| 4 | テロップの区切りがおかしい (結合・分割の手作業) | **設定 1 つの二重用途で区切りが崩れている** | 切り抜きも `recognize_for_timeline` を共有するため、`subtitle.min_line_length=5` がテロップ下限まで下げている (§3.3 C1) |
| 5 | YouTube に合わせた最適な音量調節ができているか | **クリップ単位では合うが、出来上がった 1 本では合っていない** | 正規化が**クリップごと個別**。イントロカード・OP/ED は未正規化で、結合後は一度も測っていない (§4.2) |

### 0.1 切り抜き経路だけに存在する重大な不具合

| 記号 | 症状 | 影響 |
| --- | --- | --- |
| **A3** | `emotion_points` の `big_laugh` / `surprise` / `cry` / `anger` が `setting.json` にあるのに**コードが読んでいない** | 笑い・驚きは一切検出されていない。設定を触っても何も起きない |
| **A4** | コメントの `emotes` を正規化しているのに**採点で使っていない**。「草」「笑」も未カウント | Twitch の笑いエモート (LUL / KEKW 等) が盛り上がり判定に効かない |
| **E2** | イントロカードの音声が**正規化を通っていない** (元 VOD の生音量) | テーマを付けた全クリップで、**イントロが本編より約 5 dB 大きい** (実測 I=-9.2 → 目標 -14) |
| **E1** | ラウドネス正規化が**クリップごと個別** | 静かなクリップと盛り上がったクリップが同じ音量に揃えられ、**切り抜きの山場が潰れる** |
| **A6** | `cut_region` が `-c copy` | 採点した区間と実際の切り抜き区間が**最大 1 GOP (配信 VOD では概ね 2 秒) ずれる** |

### 0.2 最優先で入れるべき 3 件 (費用対効果の順)

1. **イントロカードを正規化経路に乗せる** (§4.3 F2) — 数行。
   テーマ付き切り抜きで必ず起きている音量段差が消える。
2. **テロップ下限文字数を独立キーへ分離** (§3.4 G1) — 1 行。
   「認識セグメントそのまま」の細かさが直り、結合の手作業が激減する。
3. **採点を「声」ベースへ寄せる** (§2.3 F1) — 中規模。
   同梱済みの Silero VAD で発話率を特徴に加えるだけで、
   「ゲーム音が大きいだけの区間」が上位から落ちる。

### 0.3 実装と検証の状況 (2026-10-23)

段 0〜段 2 を実装し、実際の VOD 1 本で動作を確認しました。
**結果と、そこで見つけた不具合・新しい問題は §12 にまとめています。**

| 区分 | 内容 |
| --- | --- |
| 実装済み | 段 0 (計測の埋め込み) / 段 1 全件 / 段 2 のうち F6 以外 |
| 動作確認済み | G5 (実寸折り返し) / G1 (テロップ下限) / F9 (VOD 単位の正規化) / F13 (適用方式の記録) — §12.1 |
| 未通過 | F8 / F10 / F11 は焼き込み・出力の工程のため、出力まで回すと検証できる |
| **見つけた不具合** | **`origin` の実装漏れ (§12.2-1) / テロップが 76.8 秒に伸びる回帰 (§12.2-2)** — いずれも修正済み |
| **新しい問題** | **A9 ゲーム内ボイスがテロップになる (§12.3)** — F1 (VAD) では解決しない |
| 残課題 | H1 3 行になるテロップ (§12.4) — **修正済み** |
| **実装したが不採用** | **F15 話者照合 (§12.5〜§12.7)** — ONNX モデル (WeSpeaker) まで入れて実測したが、**この素材では成立しない**ことが確定 (§12.7)。**ONNX 実装は取り除きました** (§12.8)。枠組みと MFCC 方式だけ残し、既定は `off` |
| **対策を入れないと決めた** | **A9 ゲーム内ボイス** — F15 (話者照合) は実測で不成立 (§12.7)、**F16 (音量で要確認マーク) は不採用** (§12.9)。当面は**編集画面で手で落とす運用**になります |

### 0.4 回答に基づく決定事項 (2026-10-23)

§9 の不明点のうち Q1〜Q7・Q9 に回答を得たため、本書の方針を以下に確定します。
未回答は **Q8 (音声ビットレートの目標値)** の 1 件のみで、
これも未回答のまま **256k 既定**で進められます (現状の約 128kbps より確実に改善するため)。

| # | 回答 | 本書への反映 |
| --- | --- | --- |
| **Q1** | 配信タイトル・ゲーム名を `initial_prompt` へ差し込んでよい | **採用**。B1 を「Twitch API の取得値を自動で種にする」前提で確定 (§3.2 B1) |
| **Q2** | **案 B′** (発話区間の擬似感情特徴) | **採用**。案 B (SER モデル) は**不採用**、案 C は見送り (§2.3 F3 / §2.4.5) |
| **Q3** | **1 本にまとめて出力**し、手直しを経てそのまま YouTube へ上げる | `loudness_scope="vod"` を既定とし、**F10 (最終出力の測定・補正) を必須**へ格上げ (§4.3 F9 / F10) |
| **Q4** | テロップの結合 / 分割は **Timeline 編集画面だけ** | **確定**。ArchiveResultWindow には出さない = G4 の実装が半分になる (§3.4 G4) |
| **Q5** | 「2 行で画面外に出ないこと」が条件。**フォント次第なので文字数ではなくテロップ(実寸)次第** | **ご指摘どおりです**。文字数は代理指標にすぎません。**G5 (実寸 px での折り返し) を新設**し、G1 の文字数下限は px 化までの暫定とします (§3.4 G5) |
| **Q6** | `cut_region` の精度は**今後の課題として後回し** | F5 を **§11 今後の課題へ退避**。実施しない (§11-1) |
| **Q7** | 評価セットは**実装してテストした後に報告でよい** | **ブロッカーから外す**。素材の事前提供は求めず、**計測を実装へ埋め込む** (§6.0 / §6.1)。`origin["asr_text"]` を 1 行足すだけで普段の編集が正解データになる |
| **Q9** | 入力は **`twitch-dl` 取得の VOD が主** | 音声はミックス 1 本のみ → **F14 (トラック選択) は効かないため保留**、案 B (SER) も**不採用**が確定 (§11-2) |

> **Q3 の回答で音量の設計が変わります。** 「1 本にまとめてそのまま YouTube へ」なら、
> 聴き手が体験するのは**結合後の 1 本**だけです。クリップごとに -14 LUFS へ揃える
> 現状 (E1) は目的と噛み合わず、かつ**誰も最終ファイルを測っていない** (E3) 状態で
> 公開されることになります。`"vod"` スコープ + F10 の最終測定が揃って初めて
> 「YouTube に合わせた音量」が保証されます。

### 0.5 調査方法

- 実装読解: `src/archive/{pipeline,features,scoring,comment_source,clip_writer,config}.py`、
  `src/modules/{silence_cutter,volume_analyzer,subtitle_generator,loudness_normalizer,concat_processor}.py`
- 設定実値: `src/settings/setting.json` の `archive` / `subtitle` / `volume_analysis` / `loudness` / `ffmpeg`
- 実行ログ: `logs/autoedit_2026*.log` (閾値・区間数・ラウドネス実測値)
- 依存の実機確認: faster-whisper **1.2.1** / `WhisperModel.transcribe` が受け付ける 34 引数と
  `VadOptions` の既定値を実環境で列挙

---

# 第1部 対象経路の全体像

## 1.1 処理の流れ

```
[採点フェーズ]  archive/pipeline.analyze()
  VOD/ローカル mp4
    ↓ features.extract_cells()          45 秒セルごとに mean_db / max_db / 無音率
    ↓ comment_source.aggregate_into_cells()  セルごとの ｗ数・コメント数
    ↓ scoring.score_window()            180 秒窓 (= 4 セル) を採点
    ↓ scoring.select_top_events()       TOP10 を貪欲選択 → 連続/重なりを統合
  クリップ候補 (= 切り抜く区間)

[prepare フェーズ]  archive/clip_writer.prepare_one_clip()  ← クリップごとに繰り返す
    ↓ cut_region()                      -c copy で区間切り出し
    ↓ loudness_normalizer.normalize_file()   ★クリップ単位で -14 LUFS へ
    ↓ _apply_volume_analysis()          ★クリップ単位でカット閾値を測り直す
    ↓ _prepare_edit_points()            silence_cutter.detect_edit_points()
                                        + subtitle_generator.recognize_for_timeline()
  編集点 + 字幕 items

[レビュー]  Timeline 編集画面 / ArchiveResultWindow

[burn フェーズ]  archive/clip_writer._render_clips() / _burn_clips()
    ↓ renderer.render()                 字幕焼き込み・合成
    ↓ _build_tag_overlay()              左上テーマタグ
    ↓ _build_intro_card()               ★元 VOD から 3 秒 (正規化を通らない)
    ↓ _build_combine_parts()            OP + クリップ群 + ED
    ↓ concat_processor.concat()          ★全再エンコード (-b:a 指定なし)
  最終 1 本
```

★ = 本流 (`pipeline_runner`) と異なる、切り抜き固有の扱い。

## 1.2 本流との違い (ここが固有問題の源)

| 項目 | 本流 (1 本編集) | 切り抜き |
| --- | --- | --- |
| ラウドネス正規化 | 素材 1 本に 1 回 | **クリップごとに個別** (`clip_writer.py:528`) |
| カット閾値の確定 | ダイアログで利用者が確認 | **ダイアログ無し・クリップごとに自動採用** (`_apply_volume_analysis`) |
| 区間の切り出し | `extract_segment` (再エンコード・フレーム正確) | **`cut_region` (`-c copy`・キーフレーム単位)** |
| 最終結合 | `renderer` が PCM 中間で 1 回だけ AAC 化 | **`concat_processor.concat` が全パーツを再エンコード** |
| OP/ED | 本流と同じ | クリップには付けず結合時に 1 回 (`build_clip_settings`) |
| 固有の追加要素 | — | **イントロカード / 左上テーマタグ** (正規化外) |

## 1.3 現在の設定実値

| セクション | キー | 値 |
| --- | --- | --- |
| `archive.scoring` | `window_sec` / `slide_sec` | 180 / 45 |
| | `top_n` | 10 |
| | `clip_pad_sec` | 0 |
| | `loud_percentile` | 0.8 |
| | `silence_ratio_threshold` | 0.6 |
| | `method_a.weights` | emotion 0.55 / comment 0.45 |
| | `method_a.emotion_points` | loud 10 / long_silence -8 / **big_laugh 15・surprise 8・cry 15・anger 12 (未使用)** |
| `archive.clip_pipeline` | `silence_cut` / `timeline_review` | true / true (既定) |
| `archive.markers` | `before_sec` / `after_sec` | 120 / 120 |
| `archive.intro_card` | `buffer_sec` / `mute_intro` | 3.0 / **false** |
| `volume_analysis` | `last_cut_db` / `metric` / `min_region_sec` | -20 / mean / 0.8 |
| `subtitle` | `min_line_length` / `max_line_length` / `subtitle_max_lines` | **5** / 20 / 2 |
| `loudness` | `target_i` / `target_tp` / `audio_bitrate` | -14.0 / -1.0 / "192k" |
| `ffmpeg` | `audio_codec` / (ビットレート) | aac / **指定なし** |

---

# 第2部 切り抜き区間の選定と自動カット

> 切り抜きにおける「自動カット」は 2 段階ある。
> **① どの 3 分を切り抜くか (採点)** と **② その中の無音をどう詰めるか (無音カット)**。
> 要望①②は①に対する問いであり、②は本流と同じ仕組みを使っている。

## 2.1 ① 区間選定 (採点) の現状

`scoring.score_window()` が 1 窓 (180 秒 = 4 セル) を採点する式は以下だけです。

```python
base    = (窓内セルの正規化 mean_db の平均) * 100.0        # 0..100
loud    = loud_points * (max_db >= 80 パーセンタイル のセル数)   # +10/セル
silent  = long_silence_points * (silence_ratio >= 0.6 のセル数)  # -8/セル
emotion = clamp(base + loud + silent, 0, 100)

comment = clamp(ｗ総数 * 1 + 急増ボーナス 10, 0, 100)

total   = emotion * 0.55 + comment * 0.45
```

つまり **感情点 = 音量そのもの**です。コードのコメントにも
「R1 は大声/無音のみ検出可能」と明記されています (`scoring.py`)。

## 2.2 問題点 (区間選定)

### A1. 「声」を判定していない — 要望①の答え: できていない

`mean_db` / `max_db` は `volume_analyzer.measure_region_volume()`
(= FFmpeg `volumedetect`) の出力で、**音の大きさしか表しません**。
配信者の声・ゲーム BGM・効果音・視聴者の BGM がすべて同じ 1 本のミックスに
混ざったまま、区別なく点になります。

| 状況 | 現状の採点 | 望ましい採点 |
| --- | --- | --- |
| 配信者が静かに喋る + ゲームが静か | **低得点** (選ばれない) | 内容次第で選ぶ |
| 配信者が黙っている + ゲームの戦闘 BGM が大きい | **高得点** (選ばれる) | 選ばない |
| 配信者が叫ぶ + ゲーム音も大きい | 高得点 (正しいが偶然) | 選ぶ |

→ 現状の切り抜きは「**音が大きい 3 分**」を選んでおり、要望の
「**配信者が盛り上がっている 3 分**」を選んでいません。

> **訂正 (§12.3-1)**: この項目を当初「**声かゲーム音か**を区別していない」問題として
> 書きましたが、実際にはもう 1 つ別の区分がありました。
> **ゲーム内のボイスは音響的には人の声そのもの**なので、F1 (VAD) を入れても
> 「配信者の声」と「ゲーム内の声」は分かれません。
> 採点の歪み (A1) と、テロップがゲーム内ボイスを拾う問題 (**A9** / §12.3) は
> **別の問題**として扱います。

### A2. ゲーム音が「大声加点」と「無音減点」の両方を無効化する

2 つの補正項がどちらもゲーム音で壊れます。

**(a) 大声加点 (`loud`) が常に入る**
`loud_threshold()` は全セルの `max_db` の 80 パーセンタイルをしきい値にします。
`max_db` は **45 秒セル内のピーク 1 点**なので、効果音・銃声・爆発音・通知音が
1 回鳴れば満たされます。ゲーム実況では**ほぼ全セルが「大声セル」**になり、
加点が区間の差を付けません。

**(b) 無音減点 (`long_silence`) が一度も入らない**
`silence_ratio` は `volume_analysis.base_noise_db = -30 dB` 固定の
`silencedetect` 結果からの按分です。ゲーム音が鳴っていれば -30 dB を下回らないため
**`silence_ratio` はほぼ 0**、`>= 0.6` の条件は成立しません。
つまり「配信者が黙っている 45 秒」はゲーム音があるだけで減点されません。

→ 結果、`emotion` は実質 `base` (= 音量平均) 1 本になります。

### A3. 感情点の設定が死んでいる

`setting.json` には以下があります。

```jsonc
"emotion_points": {
  "big_laugh": 15, "loud": 10, "surprise": 8,
  "cry": 15, "anger": 12, "long_silence": -8
}
```

ところが `scoring.score_window()` が読むのは **`loud` と `long_silence` の 2 つだけ**です。

```python
loud_points          = float(points.get("loud", 10))
long_silence_points  = float(points.get("long_silence", -8))
# big_laugh / surprise / cry / anger はどこからも参照されない
```

→ **笑い・驚き・泣き・怒りは一切検出されていません。**
設定画面や `setting.json` でこれらを変えても出力は 1 ビットも変わりません。
「設定があるのに効かない」状態は、利用者が調整を試みるほど混乱するため、
検出を実装するか、**少なくとも未実装であることをコメント・UI で明示**すべきです。

### A4. コメントの笑いエモートと「草」が未カウント

`comment_source` は Twitch のエモート ID を正規化して `emotes` に持っています。

```python
return {"offset_sec": ..., "user": ..., "text": ..., "emotes": emotes}
```

しかし `aggregate_into_cells()` が集計するのは `comment_count` と
`count_w_chars(text)` の 2 つだけで、**`emotes` はどこでも使われません**。

```python
cells[idx]["comment_count"] += 1
cells[idx]["w_count"] += count_w_chars(c["text"], runs_only)
# emotes は捨てられる
```

`count_w_chars()` 自体はよくできており (全角ｗを常に笑い扱い / `world` のような
英単語を除外)、ここは評価できます。足りないのは:

- **笑いエモート** (LUL / KEKW / 草 / Pog など) — `emotes` にあるのに未使用
- **「草」「笑」「くさ」** — 日本語配信の笑い表現の主力なのに未カウント
- **連続投稿の勢い** — `comment_count` は急増ボーナス (一律 +10) にしか使われない

### A5. ローカル入力ではスコアが音量 100% になり、満点が 55 になる

チャットを取得できない入力 (ローカル mp4 / 取得失敗) では
`comments` が空なので `comment = 0` に固定されます。

```python
total = emotion * 0.55 + comment * 0.45   # comment=0 → total = emotion * 0.55
```

- **順位付けは音量のみ**になる (A1 の問題がそのまま出る)
- **表示スコアの上限が 55 点**になる。v1.6.0 で出せるようにした採点グラフも
  同じ値域になるため、「満点でも 55」という読み取りにくい目盛りになっている

→ コメントが無いときは重みを正規化して 0〜100 に収めるべきです (§2.3 F4)。

### A6. `cut_region` が `-c copy` で、採点した区間とずれる

```python
cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", input_path,
       "-t", f"{duration:.3f}", "-c", "copy",
       "-avoid_negative_ts", "make_zero", dest]
```

`-c copy` は**キーフレームより細かく切れません**。`-ss` を `-i` の前に置いているため
実際の開始は**要求時刻の直前のキーフレーム**になり、`-t` もそこから数えられます。
配信 VOD のキーフレーム間隔は概ね 2 秒なので、**切り抜きの頭と尻が最大 2 秒前へずれます**。

ストリームマーカー由来の区間 (前後 120 秒) では相対的に無害ですが、
`archive.section_add.min_length_sec = 1.0` で 1 秒のセクションを足せる仕様では
**区間長と同じオーダーのずれ**になります。

> なお `-c copy` は「同じ引数なら出力が決定的になる」ことを狙った意図的な選択で
> (プロジェクト再開時の素材復旧で同じ関数を使うため / `ver3 resolve9` §3-1)、
> 単純に再エンコードへ変えると再開時の互換を壊します。§2.3 F5 で両立案を示します。

### A7. 特徴抽出がセルごとに FFmpeg プロセスを起こす

`features.extract_cells()` はセル 1 つにつき `measure_region_volume()` を 1 回呼び、
その中で `subprocess.run` が走ります。

```python
for i in range(n_cells):
    result = volume_analyzer.measure_region_volume(input_path, start, end, ffmpeg_cfg)
```

- 2 時間 VOD / `slide_sec=45` → `n_cells = 160` → **FFmpeg 160 プロセス**
- 加えて全長 1 パスの `silencedetect` が 1 回

`astats` + `metadata=print` を使えば**全セルの RMS/ピークを 1 パス**で取れます。
採点は「切り抜きを始める前の待ち時間」そのものなので、ここは体感に直結します。

### A9. ゲーム内のボイスがテロップになる (2026-10-23 の検証で判明)

実測で **111 件のテロップのうち 24 件が明らかにゲーム内音声**でした
(無線通信・カットシーンの台詞)。詳細と実測データは **§12.3** にあります。

- ゲーム内ボイスは**人の声そのもの**なので、F1 (VAD) も音源分離も効きません。
- VOD 入力では音声がミックス 1 本しか無いため、F14 (トラック選択) も使えません。
- 切り分けの手掛かりを 3 つ実測し、**区間音量だけが有効** (17 dB の差) でした
  (ステレオ定位・帯域はいずれも重なって使えない / §12.3-2)。
- 対策として **F15** (話者照合) と **F16** (音量で要確認マーク) を検討しましたが、
  **F15 は実測で不成立 (§12.7)、F16 は不採用 (§12.9)** となりました。
  当面は編集画面で手で落とす運用になります。

### A8. ② クリップ内の無音カットは閾値がクリップごとに変わる

`_apply_volume_analysis()` は**クリップごとに**`analyze_min_speech_db()` を実行し、
測定値をそのまま採用します (本流と違いダイアログは出さない)。

```python
va_cfg["last_cut_db"] = int(measured)
_logger.info("clip%d: 音量解析によりカット閾値 %d dB を採用 (…ダイアログ無し)", ...)
```

ここに 2 つの弱点があります。

**(a) 閾値がクリップ間で揃わない**
同じ 1 本の出力に入る 10 クリップが、それぞれ別の閾値でカットされます。
クリップ A は息継ぎまで詰まり、クリップ B は全く詰まらない、という不統一が起きます。

**(b) 短いクリップでは測定が崩壊しやすい**
`analyze_min_speech_db()` は `min_region_sec = 0.8` 超の区間を測り、
**代表値の最小値**を閾値にします。180 秒クリップでは区間数が少なく、
1 つ静かな区間があるだけで閾値がノイズフロアまで落ちます。
本流のログに同じ崩壊が記録されています。

```
2026-09-17 09:31:33 発話区間の最低dB=-41 (mean, 測定 3/3 区間)
2026-09-17 09:31:46 編集点検出: 無音 0 件 / 残す区間 1 件 (閾値=-41dB, 最小無音=0.60s, 総尺=17.8s)
```

同じ素材で閾値 -24 dB のときは「無音 4 件 / 残す区間 3 件」でした。
**閾値の決め方が結果を支配している**ことが分かります。
区間数がさらに少ない切り抜きクリップでは、この崩壊がより起きやすくなります。

## 2.3 改善案 (区間選定と自動カット)

### F1. 採点に「発話率」を加える (最重要)

Silero VAD は **faster-whisper 1.2.1 に同梱**されており
(`faster_whisper.vad.VadOptions`)、依存の追加なしに使えます
(`docs/claude.md` の「不要なライブラリを追加しない」を満たす)。

```
現状: セル特徴 = mean_db / max_db / silence_ratio / w_count / comment_count
提案: セル特徴 = 上記 + speech_ratio (VAD が発話と判定した時間の割合)
```

- `features.extract_cells()` に **1 回だけ** VAD を走らせ (全長 16kHz モノラル)、
  発話区間をセルへ按分する (`_silence_overlap()` と同じ按分ロジックを再利用)。
- `scoring.score_window()` に発話点を追加:

```python
speech_points = float(points.get("speech", 20))     # 新設定
speech_ratio  = 窓内セルの speech_ratio の平均
emotion = base + loud*loud_cells + long_silence*silent_cells + speech_points*speech_ratio
```

- さらに **音量ベースを「発話区間だけの音量」へ差し替える**のが本筋です。
  セル内の VAD 発話区間の RMS を `mean_db` の代わりに使えば、
  ゲーム音の大小が `base` を動かさなくなります。
  新設定 `archive.scoring.method_a.loudness_source`:
  `"cell"` (従来) / `"speech"` (発話区間のみ)。
- 新設定 `archive.scoring.vad`: `enabled` (true)、`threshold` (0.5)、
  `min_speech_ms` (250)、`min_silence_ms` (600)。
- 注意: 2 時間素材で数十秒〜数分かかります。A7 の 1 パス化と合わせて実装し、
  進捗を必ず返すこと (`run_ffmpeg_progress` の対象外のため、セル数ベースの自前進捗)。

### F2. 大声加点と無音減点を直す

- **大声加点**: `max_db` (セル内ピーク 1 点) ではなく
  **「`max_db` がしきい値を超えた時間の割合」**で測る。
  `astats` の 1 秒刻み `Peak_level` を使えば F6 の 1 パス化で同時に取れます。
  新設定 `archive.scoring.loud_ratio_threshold` (既定 0.2 = セルの 2 割以上)。
- **無音減点**: 固定 -30 dB ではなく **VAD の `speech_ratio`** で判定する。
  `silence_ratio_threshold` の意味を「無音率」から「非発話率」へ移すのが素直です
  (既存キーは `silence_ratio` のまま残し、`silence_source: "vad"` で切り替える)。

### F3. 感情点を実装するか、未実装を明示する

`big_laugh` / `surprise` / `cry` / `anger` の 4 つは**現状どこからも読まれていません**。
取り得る道は 4 つありましたが、**Q2 の回答により案 B′ を採用**します (§9.1)。

| 案 | 内容 | 規模 | 新規依存 |
| --- | --- | --- | --- |
| **案 A** | コメント側の特徴で代替する。`big_laugh` = ｗ/草/笑いエモートの急増、`surprise` = 「えっ」「うそ」等の語の急増 | 小 | なし |
| **案 B′ (推奨)** | **発話区間の擬似感情特徴** (ピッチ上昇・発話音量の突出・立ち上がりの急峻さ)。同梱の `torchaudio` で完結する (§2.4) | 中 | **なし** |
| 案 B | 音声感情認識 (SER) モデルを導入する。**発話区間のみで実行する** (§2.4) | 中〜大 | モデル重み 1 つ |
| 案 C | 4 キーを `setting.json` から外し、未実装であることを明示する | 極小 | なし |

いずれにせよ**現状の「設定があるのに効かない」状態は解消すべき**です。
案 A と案 B′ は**併用できます** (コメント = 視聴者の反応 / 擬似感情特徴 = 配信者の反応)。

> **決定 (Q2 の回答)**: **案 B′ を採用**します。案 B (SER モデル) は Q9 の回答
> (入力は VOD が主 = 音声はミックス 1 本) により**精度が出ないため不採用**です (§2.4.3)。
> `archive.scoring.emotion.mode = "pseudo"` を目標の既定とし、
> 案 A (コメント特徴) は F4 と一緒に入れて併用します。

### F4. コメント採点を強化する

- **笑いエモートを数える** (A4): `emotes` を捨てずにセルへ集計する。
  新設定 `archive.scoring.method_a.comment.laugh_emotes`
  (既定 `["LUL","KEKW","OMEGALUL","Pog","PogChamp","4Head"]` / 配列・大小区別なし)。
  `emote_point` (既定 1) で点に換算。
- **「草」「笑」を数える** (A4): `count_w_chars()` と同じ層に
  `count_laugh_words(text, words)` を足す。
  新設定 `comment.laugh_words` (既定 `["草","笑","くさ","ｗｗ"]`)。
- **重みを正規化する** (A5): コメントが無いときは
  `total = emotion` (= 0〜100) へ寄せる。

```python
w_e = float(weights.get("emotion", 0.55))
w_c = float(weights.get("comment", 0.45))
if not cfg.get("comment_spike", False):     # コメント未取得
    w_e, w_c = 1.0, 0.0                     # 音量のみで 0..100 へ正規化
total = emotion * w_e + comment * w_c
```

新設定 `archive.scoring.method_a.normalize_weights` (既定 true / false で従来挙動)。
採点グラフの目盛りが 0〜100 で読めるようになります。

### F5. 切り抜き区間をフレーム正確にする (再開互換を保ったまま)

`cut_region` を `-c copy` から変える際は、プロジェクト再開時の素材復旧
(`project_resume`) が**同じ関数で同じ出力を再現する**前提を壊さないことが条件です。

- 新設定 `archive.cut_mode`: `"copy"` (従来 / 既定) / `"accurate"` (再エンコード)。
- `"accurate"` では `extract_segment()` 相当 (`-ss` を `-i` の前・再エンコード・
  `-fps_mode cfr`) を使う。本流が既に使っている関数なので挙動は実証済み。
- **プロジェクトへ `cut_mode` を記録**し、再開時はその値で復旧する
  (記録が無い既存プロジェクトは `"copy"` とみなす = 完全後方互換)。
- 代償: 切り出しに時間がかかります。既定を `"copy"` のままにし、
  「区間を正確に切る」チェックとして UI へ出すのが無難です。

### F6. 特徴抽出を 1 パスへまとめる

```
現状: silencedetect 1 パス + volumedetect × セル数 (2 時間で 160 プロセス)
提案: astats (metadata) 1 パス で RMS/ピークを 1 秒刻みに取得し、セルへ按分
      + (F1 を入れる場合) VAD 1 パス
```

- `-af astats=metadata=1:reset=<cell_sec>,ametadata=print:file=-` で
  セル単位の `RMS_level` / `Peak_level` が 1 パスで取れます。
- 新設定 `archive.scoring.feature_mode`: `"per_cell"` (従来) / `"single_pass"` (新)。
- 効果: 採点の待ち時間が目に見えて縮みます (プロセス起動 160 回 → 1〜2 回)。

### F7. クリップ間でカット閾値を揃える

A8 への対処です。

- 新設定 `archive.clip_pipeline.cut_db_scope`:
  - `"per_clip"` (従来): クリップごとに測る
  - `"vod"` (推奨の新既定候補): **VOD 全体で 1 回**測った閾値を全クリップへ使う
  - `"saved"`: `volume_analysis.last_cut_db` を固定で使う
- `"vod"` は採点フェーズで既に VOD 全長を解析しているため、
  **F6 の 1 パスで得た分布から分位点を取るだけ**で追加コストがほぼゼロです。
- 併せて `volume_analysis.metric` に `"percentile"` を追加し
  (`percentile` 既定 10)、**最小値採用による崩壊**を止めます。
  これは本流と共通の修正ですが、区間数が少ない切り抜きでは効果がより大きいです。

### F14. 音声トラックの選択を設定に出す (ローカル複数トラック入力のみ有効)

採点・無音カット・波形表示・音声ソースのすべてが **`0:a:0` 決め打ち**です。

```
src/modules/silence_cutter.py:571   -map 0:a:0
src/timeline/audio_source.py:253    -map 0:a:0
src/gui/timeline/waveform.py:198    -map 0:a:0
src/archive/features.py             detect_silence / measure_region_volume 経由で [0:a]
```

- 新設定 `general.audio_track_index` (既定 0 = 従来) と、入力選択時の
  「音声トラック」選択 (`ffprobe` のトラック一覧を出す)。
- **`0:a:0` の決め打ちを 1 か所のヘルパ
  (`ffmpeg_runner.audio_map_spec(settings)`) に集約**してから差し替えるのが安全。
- **Twitch VOD には効きません** (ミックス 1 本しか無い / §2.4.3)。
  OBS の複数トラック録画をローカル入力する運用でのみ意味を持ちます。
  その運用があるなら、マイク単独トラックを選べるだけで
  A1 (声を区別しない) が**F1 を入れる前に大きく改善します**。
  ただし **Q9 の回答 (入力は VOD が主) により本書では保留**します (§11-2)。

### F16. 音量を「要確認」の判定へ加える (不採用 — §12.9)

ゲーム内ボイスのテロップ (A9) を**自動では消さず**、B2 の色分けへ音量を足して
利用者が一括で落とせるようにします。実測では配信者とゲームの区間音量に
**17 dB の差**があり、しきい値 -34 dB でゲームの 92% に印が付きます (§12.3-3)。

- 認識時にテロップの**発話区間の音量** (単語の時刻で区切った区間の mean dB) を測り、
  `origin["speech_db"]` へ残す
- `subtitle.low_level_db` (既定 -38) を下回るテロップを B2 と同じ色分けの対象にする
- 編集画面へ「**要確認だけ表示**」フィルタと「**要確認を一括で使用しない**」を足す
- **自動で捨てない**のが要点です。-34 dB ではゲームの 92% を除ける一方、
  配信者の声も 36% 失います (§12.3-3) ため、判断は利用者へ残します。
- 新設定 `subtitle.low_level_db` / `subtitle.flag_low_level` (既定 true)

### F15. 話者照合で配信者以外を落とす (A9 への本筋 / 要判断)

配信者は VOD 全体で**一貫した 1 人の声**、ゲームは**多数の別人の声**という
非対称性を使います。詳細は **§12.5** にあります。

1. 発話区間ごとに**声紋 (speaker embedding)** を取る (ECAPA-TDNN / x-vector 等)
2. クラスタリングし、**最も発話時間が長いクラスタ = 配信者**とみなす
3. そのクラスタから遠い発話を落とす (または「サブ」役割にして利用者へ委ねる)

- **依存は ONNX モデル 1 つだけ**。`onnxruntime` は既に配布物へ入っており、
  `src/blur/models.py` の作法 (遅延 import / モデルが無ければ機能を落とす) が使えます。
- **感情認識 (案 B / §11-3) と違い背景音に強い**: 必要なのは「同じ声か違う声か」の
  相対判定で、配信者の声は数百回聞けるため中心が安定します。
- 新設定 `subtitle.speaker_filter` (`"off"` / `"flag"` / `"drop"`) /
  `subtitle.speaker_model` / `subtitle.speaker_threshold`
- **実測の結果、この素材では不採用です (§12.7)。** 実装は残します。

---

## 2.4 補足 — 音声感情認識は発話区間のみで実行できるか (質問への回答)

> **質問**: 2.3 F3 の音声感情認識 (案 B) は、発話区間のみ実行できるか？

### 2.4.1 結論

**できます。むしろ発話区間のみに限定するのが実装の前提条件です。**
ただし**それだけではゲーム音の混入は解決しません** (§2.4.3)。

### 2.4.2 発話区間のみに限定すべき 3 つの理由

**(1) SER モデルの入力粒度がそもそも発話単位**

音声感情認識のモデルは **1〜4 秒程度の発話単位**で学習されています。
45 秒のセルを丸ごと入力するのは設計上の誤りで、

- 1 つのセルに複数の感情が混ざり、出力が平均化されて意味を失う
- セルの大半が無音・ゲーム音のみなら、そちらに引きずられる

F1 (Silero VAD) が返す発話区間は、**ちょうどモデルが期待する入力粒度**になります。
つまり F1 を入れることが案 B の前提であり、F1 を入れたなら発話区間は既に手元にあります。

**(2) 計算量が実用域に落ちる**

2 時間 VOD の全長 7200 秒を推論するのは非現実的です。
発話区間のみ + セルごとの代表サンプルに絞れば、推論量は桁で落ちます。

| 方式 | 推論対象の音声長 (2 時間 VOD) |
| --- | --- |
| 全長を推論 | 7,200 秒 |
| 発話区間のみ (発話率 50% と仮定) | 約 3,600 秒 |
| **発話区間のみ + セルごとに最長 3 本 × 各 4 秒** | **約 1,920 秒** (160 セル × 12 秒) |

採点はセル単位の代表値しか要らないため、**セル内の全発話を推論する必要はありません**。
「そのセルで最も長い発話 N 本」を採れば、セルの性格は十分に表せます。

- 新設定 `archive.scoring.emotion.max_samples_per_cell` (既定 3)
- 新設定 `archive.scoring.emotion.sample_max_sec` (既定 4.0)
- 新設定 `archive.scoring.emotion.min_sample_sec` (既定 1.0) — これ未満の発話は推論しない
  (SER モデルが短すぎる入力で不安定になるのを避ける)

**(3) 無音への誤検出が構造的に消える**

無音・ゲーム音のみの区間に感情ラベルが付くことがなくなります。
これは A2 (ゲーム音が大声加点を常に入れてしまう) と同じ構造の問題を、
**最初から作らない**という意味を持ちます。

### 2.4.3 ただし — 発話区間に限定しても解決しない問題

**VAD は「いつ声があるか」しか教えません。「声だけが鳴っているか」は教えません。**

発話区間として切り出した音声も、**ゲーム音・BGM・効果音と混ざったまま**です。
SER は背景音・特に音楽に非常に弱く、混入した音声では学習時の分布から外れて
精度が落ちます。つまり:

| 問題 | 発話区間のみに限定すると |
| --- | --- |
| 計算量が非現実的 | **解決する** (§2.4.2-2) |
| 無音に感情ラベルが付く | **解決する** (§2.4.2-3) |
| 入力粒度がモデルと合わない | **解決する** (§2.4.2-1) |
| **ゲーム音・BGM が声に混ざる** | **解決しない** |

そして**切り抜き経路では、この最後の 1 行を解く手段がほぼありません**。

| 入力 | 音声の構成 | マイク分離 |
| --- | --- | --- |
| **Twitch VOD (`twitch-dl` 取得)** | HLS の **ミックス 1 本 (AAC ステレオ)** | **原理的に不可能** |
| ローカル mp4 (OBS 複数トラック録画) | Track1=ミックス / Track2=マイク / … | 可能だが**現状コードが `0:a:0` 固定** |

- VOD を入力する運用では、配信時点で既にミックスされているため
  **マイクだけを取り出す道がありません**。残るのは音源分離 (新規依存・数百 MB・
  実行時間大 / 本書では採用を提案していません) だけです。
- ローカルの複数トラック録画を入力する運用なら分離は不要ですが、
  `silence_cutter` / `features` / `waveform` / `audio_source` が
  **すべて `0:a:0` 決め打ち**なのでトラック選択の実装が先に必要です (F14)。

→ **したがって、VOD を入力する運用では案 B (SER) に着手すべきではありません。**
精度が出ない前提で重みファイルを足すことになります。
この判断は §9 **Q9** (どの入力を使う運用か) の回答に依存します。

### 2.4.4 依存コストの再評価 (当初の記述を訂正)

本書の初版では案 B を「新規依存 + 数百 MB」と書きましたが、実装を調べた結果
**もっと軽いことが分かりました**。

| 要素 | 状況 |
| --- | --- |
| 推論ランタイム | **`onnxruntime` (32 MB) は既に配布物へ同梱済み** |
| ランタイムの作法 | **`src/blur/models.py` に完成している** — 遅延 import / セッション共有 (ロック付き) / モデルが無ければ機能を落として理由を画面へ出す |
| モデルの置き場 | **`src/models/` が既に存在** (`yolox_tiny.onnx` 20 MB ほか計 63 MB + README) |
| 追加で必要なもの | **モデル重みファイル 1 つだけ** |

つまり案 B は「推論基盤の新規導入」ではなく、
**既にある ONNX 基盤へモデルを 1 つ足すだけ**です。
`blur/models.py` の `load_session()` / `resolve_path()` / `availability()` を
そのまま流用でき、`blur` と同じく「モデルが無ければ感情点 0 で続行」にできます。

```python
# 流用できる既存の作法 (src/blur/models.py)
session = models.load_session(models.resolve_path(cfg["emotion"]["model"]),
                              cfg["emotion"]["providers"])
if session is None:
    return 0.0          # モデルが無ければ感情点を 0 にして続行 (blur と同じ)
```

- 新設定 `archive.scoring.emotion.model` (既定 `"models/ser.onnx"` / 空で無効)
- 新設定 `archive.scoring.emotion.providers` (既定 `[]` = CPU。`blur.model.providers` と同じ形)

> なお `torch` (322 MB) も配布物に入っていますが、**`src/` のどこからも import されていません**
> (`grep -rn "import torch" src --exclude-dir=dist` が 0 件)。
> blur も含めて推論は全て `onnxruntime` で行っているため、
> **PyTorch 版の SER を持ち込む必要はありません**。ONNX で統一するのが筋です。

### 2.4.5 案 B′ — モデルを足さずに発話区間から「擬似感情特徴」を作る (推奨)

案 B の前提 (F14 または音源分離) が満たせない場合でも、**発話区間だけを使って
「配信者が盛り上がっているか」を測る特徴は、モデル無しで作れます**。

同梱の `torchaudio` で以下が使えることを実機で確認しました
(torch 2.11.0+cpu / torchaudio 2.11.0+cpu)。

| API | 用途 | 確認 |
| --- | --- | --- |
| `torchaudio.functional.detect_pitch_frequency` | 基本周波数 (F0) の推定 | 利用可 |
| `torchaudio.functional.loudness` | ITU-R BS.1770 ラウドネス | 利用可 |
| `torchaudio.transforms.MFCC` / `MelSpectrogram` | スペクトル特徴 | 利用可 |

これを使った擬似感情特徴は 3 つです。いずれも**発話区間のみで計算**します。

| 特徴 | 定義 | 何を捉えるか |
| --- | --- | --- |
| **ピッチ上昇** | 発話区間の F0 中央値 − 配信者全体の F0 中央値 (半音換算) | 興奮・驚き (声が高くなる) |
| **発話音量の突出** | 発話区間の RMS − 発話区間全体の RMS 平均 (dB) | 叫び・大声 (自分の平常比) |
| **立ち上がりの急峻さ** | 発話頭 300ms の RMS 傾き | 驚き・笑いの開始 |

- **ゲーム音の混入に強い**: F0 は声の基本周波数なので、BGM が乗っても声の帯域
  (概ね 80〜400 Hz) に絞れば比較的追えます。「発話区間の RMS − 自分の平均」という
  **相対値**で見るため、ゲーム音による底上げも相殺されます。
  A1 で指摘した「絶対音量で測るから壊れる」問題を構造的に避けられます。
- **モデル重みも新規依存もゼロ**。`torchaudio` は同梱済みで、
  `blur/models.py` と同じ遅延 import にすれば起動時間も増えません。
- 新設定 `archive.scoring.emotion.mode`:
  `"none"` (従来 / 既定) / `"pseudo"` (案 B′) / `"ser"` (案 B)
- 新設定 `archive.scoring.method_a.emotion_points.pitch_up` (既定 8) /
  `loud_burst` (既定 10) / `onset` (既定 5)

**推奨する順序**: F1 (VAD) → 案 B′ (擬似感情特徴) まで入れて評価し、
それで不足し、かつ A2 でマイク単独トラックが取れる運用なら案 B (SER) へ進む。


---

# 第3部 自動文字起こし (切り抜き経路)

## 3.1 現状

`archive.clip_pipeline.timeline_review` は既定 `true`、`timeline.enabled` も `true` なので、
切り抜きは **Timeline 経路**を通ります (`config.use_timeline_review()`)。

```
_prepare_edit_points(normalized, clip_settings, clip_dir, profile)
  ├ silence_cutter.detect_edit_points(ctx)        → keep_segments (クリップ相対)
  └ subtitle_generator.recognize_for_timeline(ctx, keep_segments)
        ├ extract_audio_segments()                → プレビュー用音声
        ├ text_source.extract(クリップ全長)        → 素材時間の認識結果
        ├ regroup_items_by_length()               → テロップ単位を文字数で再構成
        ├ map_items_to_timeline()                 → 詰めた軸へ写像
        ├ align_items_to_cut_heads()              → カット頭へ整列
        └ adjust_display_timing()                 → 表示タイミング整形
```

**本流と完全に同じ関数**を通るため、認識精度・テロップ単位の課題は共通です。
切り抜き固有の違いは「対象が 180 秒のクリップである」ことだけで、これは有利に働きます
(全長 2 時間を認識しないため、`clip_timestamps` による範囲限定の必要性は本流より低い)。

> 従来画面経路 (`timeline_review=false`) では `_transcribe()` が使われ、
> **`regroup_items_by_length()` を通りません**。つまりテロップ単位は
> 「認識セグメントそのまま」になります。既定では通らない退路ですが、
> 切り替えると区切りの挙動が変わる点は把握しておく必要があります。

認識パラメータは `WhisperTextSource` が渡す 7 つだけです。

| 引数 | 値 | 出所 |
| --- | --- | --- |
| `language` | "ja" | `subtitle.language` |
| `beam_size` | 8 | `subtitle.whisper_beam_size` |
| `condition_on_previous_text` | **False** | `subtitle.whisper_condition_on_previous_text` |
| `no_speech_threshold` | 0.6 | `subtitle.whisper_no_speech_threshold` |
| `word_timestamps` | True | `subtitle.word_timestamps` |
| `vad_filter` | True | `subtitle.whisper_vad_filter` |
| `vad_parameters` | `min_silence_duration_ms=500`, `speech_pad_ms=100` | 後者は定数 `_VAD_SPEECH_PAD_MS` |

モデルは `large-v3` / CPU / `int8`。

## 3.2 問題点と改善案 (認識精度)

### B1. ドメイン知識をモデルに渡していない → `initial_prompt` / `hotwords`

faster-whisper 1.2.1 は 34 引数を受け付けますが、**精度に直結する以下が未使用**です。

| 未使用の引数 | 何に効くか |
| --- | --- |
| `initial_prompt` | 文体・表記・固有名詞の事前提示 |
| `hotwords` | 固有名詞・ゲーム用語の強制候補化 |
| `hallucination_silence_threshold` | 無音区間の幻聴を落とす |
| `repetition_penalty` / `no_repeat_ngram_size` | 反復ループの抑止 |
| `prompt_reset_on_temperature` | 文脈引き継ぎの安全弁 |

ゲーム実況の誤認識は**固有名詞・ゲーム用語・配信者特有の言い回し**が大半です。
`initial_prompt` と `hotwords` が**最も安く効く精度改善**になります。

- 新設定 `subtitle.initial_prompt` (文字列 / 空で無効)、
  `subtitle.hotwords` (空白区切りの 1 文字列)、
  `subtitle.vocabulary` (配列 → `hotwords` へ結合)
- 実装: `WhisperTextSource.__init__` で読み、`_transcribe()` の `kwargs` へ追加
  (空文字なら渡さない = 従来と完全一致)
- **切り抜きでは VOD の配信タイトル・ゲーム名が Twitch API から取れる**ため、
  `twitch_source` の取得値を `initial_prompt` の種として自動で差し込めます。
  これは本流には無い、切り抜き固有の有利な点です。

> **決定 (Q1 の回答)**: **差し込みます**。`archive.scoring` ではなく認識側の設定として
> `subtitle.initial_prompt_from_vod` (既定 true) を置き、
> 「`<配信タイトル>`。ゲーム: `<ゲーム名>`。」の形で `initial_prompt` の先頭へ付けます。
> 利用者が `subtitle.initial_prompt` に書いた文は**その後ろへ連ねる** (両方効く)。
> 取得できなかった場合は何も付けない = 従来と同一の挙動へ落ちます。

### B2. 信頼度を捨てている → 直す場所が分からない

faster-whisper は各セグメントに `avg_logprob` / `no_speech_prob` / `compression_ratio`、
各単語に `probability` を返しますが、`_recognize()` は
`text` / `start` / `end` / `words` だけを拾い、**すべて捨てています**。

切り抜きは **1 回の出力で 10 クリップ分の字幕をまとめてレビューする**作りなので、
「どこが怪しいか」が分からないと確認の手間が本流の 10 倍になります。

- `entry["confidence"] = avg_logprob`、`entry["no_speech_prob"]`、単語ごとの
  `probability` を保持する
- `regroup_items_by_length()` で束ねた単語の確率の最小値・平均を引き継ぐ
- `SubtitleClip.origin["confidence"]` へ保存 (`origin` は dict でそのまま JSON へ入るため
  `project_io` は無改修)
- **ArchiveResultWindow / Timeline 編集画面で低信頼度のテロップを色分け**し、
  「要確認だけ表示」フィルタと「次の要確認へジャンプ」を付ける
- 閾値 `subtitle.low_confidence_logprob` (既定 -0.6) /
  `subtitle.low_confidence_word_prob` (0.4)

### B3. 置換辞書が無い → 同じ誤りを毎回直している

認識後のテキストへの置換が一切ありません (`strip_fillers` によるフィラー除去のみ)。
配信者名・ゲーム名・専門用語の誤認識は**毎回同じ形で出る**ため、辞書 1 つで消えます。
切り抜きは同じ配信者の VOD を繰り返し処理するため、**本流より効果が大きい**項目です。

- 新設定 `subtitle.replacements`: `[{"from": "...", "to": "...", "regex": false}, ...]`
- 適用箇所: `_item_from_words()` / `WhisperTextSource._recognize()` の本文整形
  (`strip_fillers` と同じ層 = `_refine()`)。**改行より前**に適用すること
- B1 の `hotwords` が「認識前の予防」、これが「認識後の治療」

### B4. 計算精度が `int8` 固定

`_resolve_compute_type()` は `float16` を `int8` へ丸めるだけで、
CPU でも使える `int8_float32` / `int16` / `float32` を**選べなくしています**。

- `int8_float32` / `int16` / `float32` を通し、`float16` のみ `int8_float32` へ正規化
- 既定は `int8` のまま (互換)。GUI に「認識精度 (速度優先 / 標準 / 精度優先)」の 3 択
- 切り抜きは 1 クリップ 180 秒 × 10 本 = 30 分相当なので、
  **全長 2 時間を認識する本流より精度優先モードが現実的**です

### B5. 文脈を切っているため同音異義・漢字選択が弱い

`condition_on_previous_text=False` は反復ループ対策で入ったものですが、
**文脈に基づく漢字変換・同音異義の判断を捨てています**。

- `condition_on_previous_text=True` へ戻し、同時に
  `prompt_reset_on_temperature=0.5` (既定)、
  `hallucination_silence_threshold` (新設定 `whisper_hallucination_silence_sec`, 2.0)、
  `no_repeat_ngram_size` (新設定, 既定 0) を入れる
- **A/B 評価が必須** (§6)。切り戻せるよう設定キーのままにする

## 3.3 問題点 (テロップの区切り)

### C1. `min_line_length` の二重用途で下限が壊れている — 結合の手作業の主因

`recognize_for_timeline()` は `subtitle.min_line_length` を
「**改行の下限**」と「**テロップの下限文字数**」の両方に使っています。

```python
min_len   = int(eff_cfg.get("min_line_length", 15))   # 改行の下限
...
min_chars = max(min(min_len, max_chars), 0)            # テロップの下限 ← 同じ値
```

`DEFAULT_SETTINGS` は 15 ですが、現在の `setting.json` は **5**。
`regroup_items_by_length()` の判定は

```python
if over or far or (enough and (crossed or ended or boundary)):
    flush()
```

で、`enough = buf_len >= min_chars` が 5 文字でほぼ常に真、
`boundary` (認識セグメントの先頭) も常に真です。したがって

> **実質「認識セグメントそのまま」= `subtitle_unit="segment"` と同じ細かさになっている**

本流のログが実効値を裏付けています。

```
2026-10-20 19:55:26 テロップ単位を文字数で再構成: 2 件 (下限 5 / 上限 40 文字)
```

### C2. 間 (ポーズ) を区切りの判断に使っていない

単語タイムスタンプを持っているのに、**語と語の間の時間**が区切り判定に入っていません。
`far` の `merge_max_cut_gap_sec` だけが時間を見ますが、

- 既定 **0.0 で無効**
- 見ているのは「**取り除かれた無音**の長さ」だけで、残った 0.3〜0.5 秒の息継ぎは無視

人が「ここで切る」と感じる最大の手掛かりは**間**です。

### C3. 上限超過時の切り方が「文字数ハード分割」

上限 (`max_line_length` 20 × `subtitle_max_lines` 2 = 40 文字) を超えると
次の単語から新テロップになります。単語境界ではあるものの**文節境界ではありません**。
BudouX は `wrap_lines()` の中 (= 1 テロップ内の改行) でしか使われず、
**テロップの切り目選びには使われていません**。

### C4. 編集画面にテロップの「結合」「分割」が無い

- **結合コマンドが存在しません** (`timeline/commands.py` に `Merge*` 系なし)。
  2 本を 1 本にするには、片方を削除 → 残りを伸ばす → 本文を打ち直す手順になります。
- **分割は本文を複製します**。`split_clip_at()` は `SubtitleClip` を `copy()` して
  尺を割るだけなので、**両方のクリップに全文が入ります**。

```python
if isinstance(clip, SubtitleClip):
    new_clip = clip.copy()          # ← 本文がそのまま複製される
    new_clip.timeline_start = at_sec
    new_clip.duration = clip.duration - offset
    clip.duration = offset
```

→ 要望の「テロップを結合したり、分割する作業」は、
**UI 側に専用操作が無いこと**が相当部分を占めています。
自動生成の質とは**独立に**直せます。

## 3.4 改善案 (テロップの区切り)

### G1. テロップ下限文字数を独立キーへ分離 (最優先・最小の変更 / G5 までの暫定)

```python
# 変更前
min_chars = max(min(min_len, max_chars), 0)
# 変更後
min_chars = int(eff_cfg.get("subtitle_min_chars", max(min_len, 12)))
min_chars = max(min(min_chars, max_chars), 0)
```

- 新設定 `subtitle.subtitle_min_chars` (既定 16 を提案 / 未設定時は従来式へフォールバック)
- 影響: `tests/test_subtitle_length_units.py` の期待値を見直す
- **これだけで「1 文が息継ぎの数だけ裁断される」症状が止まります。**

> **位置づけ (Q5 の回答を受けて)**: これは**1 行で効く暫定の手当て**です。
> 本質的な解は G5 の実寸 (px) 制御で、そちらが入れば既定値 16 は
> **px 下限から逆算される**ようになります。G1 を先に入れる価値は、
> 「結合の手作業が今すぐ減る」ことと「G5 のフォールバック経路になる」ことの 2 点です。

### G5. 折り返しと上限を「文字数」から「実寸 (px)」へ移す (Q5 の回答)

> **ご指摘のとおりです。** 文字数は実寸の代理指標にすぎず、フォントによって
> 1 文字の送り幅は **1.5 倍も違います**。`docs/error/20261023/resolve.md` §3-2 の実測:

| フォント | 全角 1 文字の送り幅 (em) |
| --- | --- |
| 07にくまるフォント | 0.926 |
| MS ゴシック | 1.000 |
| 游ゴシック | 0.777 |
| メイリオ | 0.667 |

つまり同じ「20 文字」でも、メイリオと MS ゴシックでは**実寸が 1.5 倍違います**。
さらに英数字がプロポーショナルなフォントでは「半角 = 全角の半分」も成り立ちません
(にくまるフォントの `W` は 0.926em で、固定係数 0.45 の 2 倍を超える)。
`subtitle.font_size` は現在 **130** なので、この差はそのまま画面外へのはみ出しになります。

#### 既に実寸計測の仕組みがあります

`src/modules/font_advance.py` が**本日 (2026-10-23) のコメント背景の修正で入っており**
(`docs/error/20261023/resolve.md` §5 案A)、そのまま流用できます。

```python
# 文字列が ASS Fontsize=size で描かれたときの幅 (px)
metrics = font_advance.metrics_for(family, eff_cfg, bold=bool(eff_cfg.get("bold")))
width_px = metrics.text_width(text, font_size)
```

- **libass の実出力と +2〜+10px で一致**し、**常にわずかに広い側へ外れる**
  (= 文字がはみ出さない側へ外れる) ことが確認済みです。
- **Qt にも ffmpeg にも依存しない純 Python** で、読めないフォントでは `None` を返し
  呼び出し側が従来の係数へ落ちる作法になっています。
- 現在の利用者は `comment_decor` (コメント背景の箱の幅) だけです。

#### 提案

折り返しと上限を px で決め、文字数キーは**フォントが読めないときのフォールバック**に降格します。

```python
# 1 行に使える幅 (px)
usable = canvas_w - margin_l - margin_r - outline_width * 2
# 1 本のテロップに使える幅 = 1 行の幅 × 最大行数
max_width_px = usable * subtitle_max_lines
```

- 新設定 `subtitle.wrap_mode`: `"length"` (従来 / 既定) / `"width"` (新・px 基準)
- 新設定 `subtitle.min_line_ratio` (既定 0.5) — 1 行の下限を「使える幅のこの割合」で決める
  (短すぎる行を避ける。現 `min_line_length` の px 版)
- `wrap_by_budoux()` の `len(line) <= max_line_length` 判定を
  `metrics.text_width(line, size) <= usable` へ差し替える (文節境界で折る規則は変えない)
- `regroup_items_by_length()` の `over` 判定も同じく px へ移す
  (= G3 の文節境界で切る規則と自然に噛み合う)
- `metrics` が `None` (フォントが読めない) のときは G1 の文字数経路へ落ちる

#### 副次的な利点

- **縦動画の上書きが不要になります**。`_VERTICAL_OVERRIDE_KEYS` は現在
  `min_line_length` / `max_line_length` を縦用の値で差し替えていますが、
  px 基準なら**キャンバス幅から自動で決まる**ため、この 2 キーを上書き対象から外せます
  (request14 の設定が 2 つ減る)。
- **フォントを変えても画面外に出ません**。要望の条件
  「2 行で表示したときに画面外に出ないこと」が、設定の調整ではなく**仕様として保証されます**。
- プレビュー (`gui/timeline/preview_items.py`) と焼き込みが
  同じ `font_advance` を基準にすれば、**プレビュー一致の精度も上がります**。

#### 留意点

- `font_advance.metrics_for()` はフォントファイルを探して読むため、
  **初回はフォルダ走査のコストがかかります** (結果はキャッシュされます)。
  テロップ 1 本ごとに呼ばず、**認識開始時に 1 回取得して使い回すこと**。
- 縁取り (`outline_width` = 現在 **10**) は字形の外側へ張り出すため、
  `comment_decor._outline_bleed()` と同じ考え方で左右に 2 倍ぶん引くこと。
- 行間 (`comment_bg_line_height_ratio` = 1.2) は**高さ**の話なので、
  2 行が縦にも収まるかは別途確認が必要です
  (`font_size` 130 × 1.2 × 2 行 = 312px / 1080 なので現状は余裕があります)。

### G2. 間 (ポーズ) を第一の区切り信号にする

- 新設定 `subtitle.split_min_pause_sec` (0.35): 下限達成後、語間がこれ以上空いたら切る
- 新設定 `subtitle.merge_max_pause_sec` (0.20): これ未満の間では**切らない**

```python
pause  = float(word["start"]) - last_end if last_end is not None else 0.0
paused = pause >= split_min_pause_sec
glued  = pause < merge_max_pause_sec        # 間が無い = 切らない
if over or far or (enough and not glued and (paused or crossed or ended or boundary)):
    flush()
```

### G3. 上限超過は文節境界で切る

- `over` で `flush()` する前に **BudouX の文節境界まで戻る**。
  `_get_budoux_parser()` は既にあるので、積んだ本文を `parse()` して
  最後の文節境界以降の単語を次のテロップへ繰り越す
- 新設定 `subtitle.split_on_phrase_boundary` (既定 True)
- 単語列と文節列は**文字数の累積で突き合わせる**のが単純かつ確実

### G4. テロップの「結合」「分割」コマンドを追加

- **`MergeSubtitles`** (Undo 可): 選択した 2 本以上を 1 本にする。
  時刻は最初の `timeline_start` 〜 最後の `timeline_end`、本文は時間順に連結して
  `wrap_lines()` で再改行、役割・色は先頭クリップを採用、
  `origin["merged_from"]` に元 ID を残す
- **`SplitSubtitle`** (Undo 可): 再生ヘッド位置で本文も分ける。
  **`SubtitleClip.origin["words"]` に単語列を保存**しておき、
  再生ヘッド以前/以降で本文を分割して再改行する。
  単語列が無い (手入力) 場合は文字数按分 + 確認
- ショートカット: 結合 `Ctrl+M` / 分割は既存の分割キーを字幕トラックで本文対応に
- 雛形: `EditSubtitle` / `EditSubtitles` / `AlignSubtitlesToCutHeads`

> **決定 (Q4 の回答)**: **Timeline 編集画面だけに出します**。
> ArchiveResultWindow には実装しません。コマンド (`MergeSubtitles` / `SplitSubtitle`) は
> Timeline の `CommandStack` に乗る前提で書けるため、**実装は当初見積りの半分**になります
> (結果画面向けの別実装と、2 画面で挙動を揃える手間が不要)。

---

# 第4部 音量調整 (切り抜き経路)

## 4.1 現状

```
クリップごと: cut_region(-c copy) → loudness_normalizer.normalize_file(raw → normalized)
                                     I=-14.0 / TP=-1.0 / LRA=11.0 / 2 パス linear
                                     -c:v copy / -c:a aac -b:a 192k
      ↓
      (無音カット・字幕焼き込み・テーマタグ)
      ↓
イントロカード: 元 VOD から 3 秒を切り出して加工        ← 正規化を通らない
      ↓
結合: [OP] + [intro + 本編] × N + [ED]                 ← OP/ED も正規化を通らない
      concat_processor.concat() が全パーツを再エンコード  ← -b:a 指定なし
      ↓
最終 1 本                                              ← ラウドネスを一度も測らない
```

実測ログ例: `入力ラウドネス: I=-9.2 LUFS / TP=-0.3 dBTP / LRA=5.1 LU → 目標 I=-14.0 / TP=-1.0 (offset 0.4 dB)`
→ 本編には **-4.8 dB** のゲインが掛かります。

## 4.2 問題点

### E1. クリップごとに個別正規化され、切り抜きの山場が潰れる

`normalize_file()` は**クリップ 1 本ずつ**に対して走ります
(`clip_writer.py:528`)。各クリップが独立に -14 LUFS へ揃えられるため:

| クリップ | 元の性格 | 正規化後 |
| --- | --- | --- |
| 静かな会話 (元 -20 LUFS) | 落ち着いた場面 | **+6 dB されて -14** |
| 絶叫・盛り上がり (元 -8 LUFS) | 山場 | **-6 dB されて -14** |

→ **「盛り上がって切り抜いた」という編集意図が音量から消えます。**
切り抜き集では、1 本の中での相対的な起伏が残っている方が自然です。

本流 (1 本編集) は素材 1 本に 1 回しか掛けないため、この問題は**切り抜き固有**です。

### E2. イントロカードが正規化を通らず、本編より約 5 dB 大きい

`_build_intro_card()` は**元 VOD (または保存した控え) から直接**切り出します。

```python
if source and os.path.exists(source):
    input_args = ["-ss", f"{start_buf:.3f}", "-i", source, "-t", f"{length:.3f}"]
...
if card["mute_intro"]:
    cmd += ["-af", "volume=0"]      # mute_intro=false なら音声はそのまま
```

現在の設定は `archive.intro_card.mute_intro = false` なので、
**イントロの 3 秒は元 VOD の生音量のまま**流れます。
一方で本編は -14 LUFS へ下げられています。

> 実測例 (VOD I=-9.2 / 目標 -14) では、**イントロが本編より約 4.8 dB 大きい**。
> テーマを付けた全クリップの頭で、毎回この段差が起きます。

これは聴感上はっきり分かる差で、**テーマ演出を使うほど目立つ**不具合です。

### E3. OP/ED も正規化を通らず、結合後は一度も測っていない

`_build_combine_parts()` は `general.opening_video` / `ending_video` を
**そのままパーツ列へ追加**します。OP/ED は固定素材なので一度合わせれば済みますが、
現状は合わせる仕組みが無く、**本編 -14 LUFS との差が出たままになります**。

さらに結合後の最終ファイルのラウドネスを**測る工程がありません**。

- YouTube は「-14 LUFS より大きい動画を**下げる**」だけで、小さい動画は**上げません**。
  最終値が -16 LUFS なら、他の動画より小さく聞こえたまま公開されます。
- 1 本の中で OP → 本編 → ED の段差があっても気付けません。

### E4. 最終 AAC のビットレート指定が抜けている

`loudness.audio_bitrate = "192k"` が効くのは**破棄される中間ファイル**だけです
(`loudness_normalizer.py:213`)。その音声は後で再デコードされ、
最終結合で改めて AAC へエンコードされますが、
`concat_processor.concat()` が使う `build_encode_options()` は
**`-b:a` を渡していません**。

```
$ grep -rn '"-b:a"' src --include=*.py
src/modules/loudness_normalizer.py:213      ← 破棄される中間ファイル
src/timeline/proxy_render.py:431            ← プレビュー用
```

```python
def build_encode_options(ffmpeg_settings):
    return ["-c:v", ..., "-preset", ..., "-crf", ..., "-c:a", "aac"]   # -b:a が無い
```

→ 最終出力は FFmpeg の `aac` 既定ビットレート (ステレオで概ね 128kbps) になります。
**切り抜き経路は結合で必ず全再エンコードが走るため、本流より確実にこの経路を通ります。**
`loudness.audio_bitrate` という設定が**事実上死んでいる**のも設計の不整合です。

### E5. 短いクリップでは loudnorm の測定が信頼できない

EBU R128 の Integrated は一定の長さを要します。
`archive.section_add.min_length_sec = 1.0` で**1 秒のセクション**を足せる仕様では、
そのクリップの `loudnorm` 測定はほぼ無意味です
(ゲート後に有効なブロックが残らず、極端なゲインが掛かる恐れがある)。

### E6. `linear=true` が実際に線形適用されたか記録していない

`loudnorm` は線形ゲインが True Peak 目標を破る場合に
**黙ってダイナミックモードへ落ちます**。2 パス目の出力を解析していないため、
**ダイナミック処理が掛かったことに気付けません** (= 意図せず音の質感が変わる)。

### E7. True Peak 目標 -1.0 dBTP は AAC には少し強気

AAC エンコード後は符号化誤差で True Peak が 0.5〜1 dB 程度**上振れ**し得ます。
切り抜き経路は**結合でもう一度 AAC を通る**ため、本流より上振れの機会が多いです。

## 4.3 改善案 (音量調整)

### F8. イントロカードを正規化経路に乗せる (最優先・数行)

E2 への対処です。選べる実装が 2 つあります。

| 案 | 内容 | 規模 |
| --- | --- | --- |
| **案 A (推奨)** | `_build_intro_card()` の出力へ `loudness_normalizer.normalize_file()` を掛ける | 小 |
| 案 B | 本編の正規化で得た `offset` (ゲイン dB) を控えておき、イントロへ `volume=<同じ dB>` を掛ける | 小 |
| 案 C | `mute_intro` の既定を `true` にする | 極小 (演出が変わる) |

**案 B が最も素直**です。イントロは本編と同じ VOD の数秒前なので、
本編に掛けたゲインをそのまま適用すれば**本編と完全に連続した音量**になります
(3 秒では `loudnorm` の測定自体が信頼できないため、案 A より適切)。

- `prepare_one_clip()` で `normalize_file()` の適用ゲインを `prepared["gain_db"]` へ控える
  (`loudness_normalizer` が `measured` と `target_i` から算出できる値を返すようにする)
- `_build_intro_card()` に `gain_db` を渡し、`-af volume=<gain_db>dB` を付ける
- 新設定 `archive.intro_card.match_body_level` (既定 true / false で従来挙動)

### F9. 正規化の単位を選べるようにする

E1 への対処です。

- 新設定 `archive.clip_pipeline.loudness_scope`:
  - `"per_clip"` (従来): クリップごとに -14 LUFS へ揃える
  - `"vod"` (推奨の新既定候補): **VOD 全体で 1 回**測ったゲインを全クリップへ適用する
    → クリップ間の相対的な起伏が保たれる
  - `"output"`: クリップは触らず、**結合後の 1 本**に対して正規化する
    → 最も素直だが、結合後にもう 1 パス必要
- `"vod"` は採点フェーズで全長を既に読んでいるため、
  そこで `ebur128` を同時に取れば追加コストがほぼゼロです (F6 の 1 パスに同乗できる)。

> **決定 (Q3 の回答)**: 切り抜きは **1 本にまとめて出力し、そのまま YouTube へ**上げる運用のため、
> **`loudness_scope = "vod"` を目標の既定**とします。聴き手が体験するのは結合後の 1 本だけで、
> クリップごとに -14 LUFS へ揃える現状 (E1) は**山場を潰すだけで誰の役にも立ちません**。
> `"output"` (結合後に 1 回) も正しい答えですが、`"vod"` ならクリップ間の起伏が残り、
> かつ F10 の最終測定で出力の絶対値も保証できるため、**`"vod"` + F10 の組み合わせ**を推します。

### F10. 最終出力を測って必要なら補正する (Q3 の回答により必須)

E3 への対処です。

> **決定 (Q3 の回答)**: 「手直しを経てそのまま YouTube へ上がる」運用では、
> **出力されたファイルがそのまま公開物になります**。現状は OP/ED・イントロを含む
> 結合後のラウドネスを**誰も測っていない**ため、-14 LUFS から外れたまま公開され得ます。
> よって F10 は「あると良い」ではなく**必須**として扱い、段 2 へ繰り上げます。

- 新設定 `loudness.verify_output` (既定 true): 結合後に `ebur128` / `loudnorm` 1 パスで測定し、
  **結果をログと結果画面へ出す**
- 新設定 `loudness.correct_output` (既定 true): 目標との差が
  `loudness.correct_tolerance_lu` (既定 0.5 LU) を超えたら
  `volume=<diff>dB` + `alimiter` で**音声のみ再エンコード**して合わせる
  (`-c:v copy` なので映像は無劣化・短時間)
- 新設定 `loudness.normalize_op_ed` (既定 false): OP/ED にも同じ目標で正規化を掛ける

### F11. 最終 AAC のビットレートを設定から渡す

E4 への対処です。

- 新設定 `ffmpeg.audio_bitrate` (既定 `"256k"` を提案)
- `build_encode_options()` / `burn_subtitle()` / `renderer._finalize_base()` から渡す
  (切り抜きの結合は `build_encode_options()` を通るため、1 箇所の修正で効きます)
- `loudness.audio_bitrate` はこれを参照するだけにして重複を消す

### F12. 短いクリップの正規化を飛ばす

E5 への対処です。

- 新設定 `loudness.min_duration_sec` (既定 5.0): これ未満の素材は正規化をスキップし、
  その旨をログへ出す (`normalize_file()` の冒頭で判定)
- F9 の `"vod"` スコープを既定にすれば、この問題は自動的に解消します
  (測定対象が VOD 全体になるため)

### F13. 2 パス目の測定 JSON も解析する / TP 目標を下げる

- E6: 2 パス目にも `print_format=json` を付けて `output_i` / `output_tp` /
  `normalization_type` をログへ出す。線形で適用できなかった場合は警告を 1 行
- E7: `loudness.target_tp` の既定を **-1.5** にする
  (YouTube 推奨の -1.0 は「配信される最終形」の値。投稿素材側は余裕を取る)

---

# 第5部 まとめ

## 5. 問題と改善案の対応表

| 記号 | 問題 | 改善案 | 段 |
| --- | --- | --- | --- |
| A1 | 採点が声を区別しない | F1 発話率を特徴に加える / F14 トラック選択 | 3 |
| A2 | 大声加点が常に入り、無音減点が入らない | F2 加点/減点の判定を直す | 3 |
| A3 | 感情点 4 キーが未実装のまま設定にある | F3 案 A / 案 B′ (擬似感情特徴) / 案 C (§2.4) | 2 |
| A4 | 笑いエモート・「草」が未カウント | F4 コメント採点の強化 | 2 |
| A5 | ローカル入力でスコア上限が 55 | F4 重みの正規化 | 1 |
| A6 | `cut_region` が `-c copy` でずれる | F5 `cut_mode` の追加 | **保留 (§11-1)** |
| A7 | 特徴抽出がセルごとにプロセス起動 | F6 1 パス化 | 2 |
| A8 | カット閾値がクリップごとに変わる/崩壊する | F7 `cut_db_scope` + 分位点 | 2 |
| **A9** | **ゲーム内のボイスがテロップになる** (§12.3) | F15 話者照合 = **実測で不成立 (§12.7)** / F16 音量で要確認 = **不採用 (§12.9)** | **対策なし (§12.9)** |
| B1 | 固有名詞を渡していない | `initial_prompt` / `hotwords` | 2 |
| B2 | 信頼度を捨てている | 保持 + 編集画面で色分け | 1 |
| B3 | 置換辞書が無い | `subtitle.replacements` | 2 |
| B4 | `int8` 固定 | 精度の選択肢を通す | 4 |
| B5 | 文脈を切っている | A/B 評価のうえ戻す | 4 |
| C1 | テロップ下限が改行下限と同一キー | **G1 独立キーへ分離** (暫定) → **G5 実寸へ** | **1 → 2** |
| **C5** | **文字数制御がフォント依存の実寸を無視している** (Q5) | **G5 折り返しを px へ移す** | **2** |
| C2 | 間を区切りに使っていない | G2 ポーズ判定 | 2 |
| C3 | 上限超過が文字数ハード分割 | G3 文節境界 + **G5 実寸 (px)** | 2 |
| C4 | 結合/分割コマンドが無い | G4 コマンド追加 | 2 |
| E1 | クリップごと個別正規化 | F9 `loudness_scope` | 3 |
| E2 | **イントロが本編より約 5 dB 大きい** | **F8 本編ゲインを合わせる** | **1** |
| E3 | OP/ED 未正規化・結合後未測定 | F10 最終測定・補正 | 3 |
| E4 | 最終 AAC が 128kbps | **F11 `ffmpeg.audio_bitrate`** | **1** |
| E5 | 短いクリップの測定が不正確 | F12 下限尺でスキップ | 2 |
| E6 | 線形/ダイナミックの記録が無い | F13 2 パス目を解析 | 4 |
| E7 | TP -1.0 が強気 | F13 -1.5 へ | 4 |
| **H1** | **3 行になるテロップがある** (§12.4) | 折り返し後の行数で切り直す | **完了** |

## 5.1 優先順位 (推奨の実施順)

| 段 | 内容 | 規模 | 期待効果 |
| --- | --- | --- | --- |
| **0** | §6.1 計測の埋め込み (`origin["asr_text"]` + 信頼度 + 単語列) | **極小** | 普段の編集が正解データになる。B2 / G4 と同じ場所なのでまとめて入れる |
| **1** | F8 イントロの音量を本編へ合わせる | **極小** | 聴いて分かる段差が消える |
| **1** | G1 テロップ下限の分離 | **極小** | 結合の手作業が激減 |
| **1** | F11 最終 AAC ビットレート | **極小** | 音質の明確な改善 |
| **1** | F4 重みの正規化 (A5) | **極小** | 採点グラフが 0〜100 で読める |
| **1** | B2 信頼度の保持 + 編集画面の色分け | 中 | 10 クリップ分の確認が軽くなる |
| **2** | F6 特徴抽出の 1 パス化 | 中 | 採点の待ち時間が縮む |
| **2** | F7 カット閾値を VOD 単位 + 分位点へ | 中 | クリップ間のカットが揃う |
| **2** | F4 笑いエモート・「草」の集計 | 小 | 盛り上がり判定が日本語配信に合う |
| **2** | F3 感情点の扱いを決める (案 C なら極小) | 小 | 「効かない設定」を解消 |
| **2** | B1 `initial_prompt` / `hotwords` + B3 置換辞書 | 中 | 固有名詞の誤りが消える |
| **2** | G2 ポーズによる区切り + G3 文節境界 | 中 | 区切りが自然になる |
| **2** | G4 結合 / 分割コマンド | 中 | 残る手作業そのものを軽くする |
| **2** | F12 短いクリップの正規化スキップ | 極小 | 1 秒セクションの暴走を防ぐ |
| **2** | **G5 折り返しを実寸 (px) へ** (Q5) | 中 | **フォントを変えても画面外に出ない** |
| **2** | **F9 正規化を VOD 単位へ** (Q3) | 中 | 切り抜きの起伏が残る |
| **2** | **F10 最終出力の測定・補正** (Q3 により必須) | 中 | **YouTube 上の音量が保証される** |
| — | ~~F15 話者照合~~ | 大 | **実測で不採用 (§12.7)**。実装は残す |
| — | ~~F16 音量で要確認マーク~~ | 小 | **不採用 (§12.9)** |
| **3** | F1 発話率を採点へ + F2 加点/減点の修正 | 大 | **「盛り上がり」の判定が成立する** |
| **3** | F3 案 B′ 擬似感情特徴 (ピッチ・音量の突出 / Q2 で採用) | 中 | 叫び・興奮を声から捉える (依存ゼロ) |
| **4** | B4 / B5 / F13 | 中 | 仕上げ |
| — | F5 `cut_mode` / F14 トラック選択 / F3 案 B (SER) | — | **保留 (§11)** |

段 1 は **F8 + G1 + F11 + F4(重み) で合計 1 日以内**に入り、
聴感・手作業・画面表示の 3 方面で効果が分かります。

## 6. 評価 — 計測を実装に埋め込む (Q7 の回答を反映)

要望に「**1 テロップが問題なく完了しているのは全体の 30% 未満**」という数字があります。
これは現在、**人の体感でしか測れません**。切り抜きは 1 回の実行で
採点 → 10 クリップ分の準備まで走るため、目視確認のコストが特に高い経路です。

### 6.0 結論 — 評価セットの事前提供は不要です (Q7)

> **質問**: 9.2 の Q7 は実装してテストした後に報告でよいか？

**はい。Q7 はブロッカーから外します。** ただし改善項目は 2 種類に分かれ、
扱いが違います。

#### (A) 評価セット不要 — 合否が数値・テストで決まるもの → 実装 → テスト → 報告でよい

段 1・段 2 のほぼ全部がここに入ります。理由は単純で、これらは
**「良くなったか」ではなく「仕様違反が直ったか」**だからです。合否が客観的に決まります。

| 項目 | 合否の決め方 | 必要なもの |
| --- | --- | --- |
| F8 イントロ段差 | 出力の intro / 本編頭を `ebur128` で測り差が 1 LU 未満 | 出力 1 本 |
| F11 ビットレート | `ffprobe` で `bit_rate` を見る | 出力 1 本 |
| F10 最終ラウドネス | 出力の Integrated が -14 ± 0.5 LU | 出力 1 本 |
| **G5 px 折り返し** | **全テロップの全行で `text_width(line) <= usable`** | **単体テスト (フォント数種)** |
| G1 テロップ下限 | 既存 `test_subtitle_length_units.py` を拡張 | 単体テスト |
| F4 重みの正規化 | `score_window` の純関数テスト (満点 100 になる) | 単体テスト |
| F6 1 パス化 | 旧経路とセル特徴が一致 + 実時間を比較 | VOD 1 本 (自己比較) |
| F7 閾値 | 「無音 0 件」の崩壊が起きないこと | VOD 1 本 |
| G4 結合 / 分割 | Undo / Redo と本文の分かれ方 | 単体テスト |
| B1 / B2 / B3 | プロンプト組み立て・信頼度の引き継ぎ・置換 | 単体テスト |
| F12 / F13 | 短尺スキップ・2 パス目のログ・TP 目標 | 単体テスト + 出力 1 本 |

とくに **Q5 の条件「2 行で画面外に出ない」は述語 (真偽が決まる命題)** なので、
評価セットを見て判断するものではなく **テストで固定するのが適切**です。
`font_advance` が libass と +2〜+10px で一致することは既に確認済みなので、
「はみ出さないこと」をそのまま assert できます。

#### (B) 判断材料が要るもの — ただし「先に正解データを作る」必要はない

| 項目 | なぜ数値だけで決まらないか |
| --- | --- |
| F1 発話率を採点へ | 「どの 3 分が良いか」は編集上の判断 |
| F2 大声加点 / 無音減点のしきい値 | `loud_ratio_threshold` 0.2 等は当て推量 |
| F3 案 B′ の重み | `pitch_up` 8 / `loud_burst` 10 / `onset` 5 は当て推量 |
| B5 文脈の引き継ぎを戻す | 精度向上と反復ループのトレードオフ |

この 4 つも**事前に正解データを用意する必要はありません**。
実装して**いつもの VOD を 1 本流し、Before / After を並べて報告 → 見て判断**で足ります。
重みの調整はその結果を受けて 2 周目に回します。

### 6.1 Q7 を「素材の提供」から「計測の埋め込み」へ置き換える

さらに踏み込むと、**普段の編集作業をそのまま正解データにできます**。

現状 `_append_subtitles()` (`src/timeline/builder.py:767`) が `origin` に残すのは
`{type, source_start, source_end}` だけで、**元の認識テキストを保存していません**。

```python
origin = {"type": ORIGIN_ASR}
origin["source_start"] = ...
origin["source_end"]   = ...
# ← 認識結果そのもの (item["text"]) は残らない
```

ここに **1 行足すだけ**で、利用者が手直しした結果が自動的に正解データになります。

```python
origin["asr_text"] = item.get("text", "")   # 生成時の認識結果を控える
```

これで以下が**保存済みプロジェクト JSON から自動で算出できます**。

| 指標 | 算出方法 | 専用素材 |
| --- | --- | --- |
| **無修正テロップ率** | `clip.text == clip.origin["asr_text"]` の割合 | **不要** |
| 文字単位の修正量 | 2 つの文字列の編集距離 | **不要** |
| 採用率 | クリップの `use` フラグ | **不要** |
| 結合 / 分割の回数 | `CommandStack` のラベル集計 | **不要** |
| 最終 Integrated / TP | 出力を `ebur128` で測る (F10 で実装) | **不要** |
| 区間一致率 (IoU) | 自動 TOP10 と、手で残した区間の比較 | **不要** |

- **保存場所は B2 (`origin["confidence"]`) と G4 (`origin["words"]`) と同じ**なので、
  3 つまとめて入れれば追加コストはほぼゼロです。
- `origin` は dict でそのまま JSON へ入るため `project_io` は無改修 (§10 参照)。
- **使うほどデータが溜まります**。評価セットを作る手間が消え、
  「30% 未満」が改善しているかを**実作業の記録から継続的に追える**ようになります。

> **したがって Q7 への回答は**: 素材の事前提供は求めません。
> 上記 1 行 + F10 の測定を実装に含め、**実装 → テスト → 報告**で進めます。
> (B) の 4 項目だけ、報告時に Before / After を並べてご判断いただきます。

### 6.1.1 旧案: 専用の評価セット (参考として残す)

- 代表 VOD 2〜3 本 (ゲーム音が大きい / 雑談中心 / 複数人) + チャット JSON
- 各 VOD の**正解切り抜き区間**と**正解テロップ**
- 動画自体はリポジトリへ入れず、`tests/fixtures/quality/` 相当にメタのみ置く

§6.1 の計測で足りなくなった場合 (例: F1 の重み調整を自動探索したい) に戻ってきます。

### 6.2 指標

| 指標 | 定義 | 対象 |
| --- | --- | --- |
| 区間一致率 (IoU) | 自動 TOP10 と正解区間の重なり | 採点 (第2部) |
| 採用率 | 結果画面で `use=True` のまま出力されたクリップの割合 | 採点の実用性 |
| **無修正テロップ率** | 1 文字も直さず通ったテロップ / 全テロップ | 要望の「30%」を直接測る |
| CER / WER | 文字・単語誤り率 | 認識精度 (第3部) |
| 区切り F1 | 正解の区切り時刻と ±0.3 秒で一致した割合 | テロップ単位 (第3部) |
| 結合 / 分割操作数 | 編集画面での結合・分割の回数 | 手作業の実コスト |
| 最終 Integrated / TP | 結合後ファイルの実測 | 音量 (第4部) |
| **イントロ/本編の段差** | イントロ 3 秒と本編頭 10 秒の Momentary 差 | E2 の検証 |
| 採点所要時間 | VOD 尺に対する採点フェーズの実時間比 | F6 の検証 |

### 6.3 仕組み

- `tools/eval_archive.py` を 1 本置く (VOD + 正解 → 指標を CSV 出力)。既存 `tools/` を踏襲
- 編集画面の操作回数は `src/services/telemetry.py` に乗せられますが、
  **送信ではなくローカル集計**で十分です

## 7. リスクと切り戻し

- **すべての新機能を `setting.json` のキーで ON/OFF 可能にする**。既定値は
  「従来と完全一致」から始め、評価 (§6) で改善が確認できたものだけ既定を変える
  (`docs/claude.md` の「既存実装を破壊しない」「ハードコード禁止」)。
- **G1 は既定値の変更**なので `tests/test_subtitle_length_units.py` の期待値が変わります。
  旧挙動は `subtitle_min_chars` を旧式に設定したケースとして別テストで保持してください。
- **F5 (`cut_mode`)** はプロジェクト再開時の素材復旧 (`project_resume`) の前提
  (同じ引数で同じ出力が再現できる) に触れます。**プロジェクトへ `cut_mode` を記録**し、
  記録が無い既存プロジェクトは `"copy"` とみなしてください。
- **F9 (`loudness_scope="vod"`)** は `prepared["media_role"]` の判定
  (`normalized != raw` で正規化を通ったか判断している) に影響します。
  スコープを変えるときは `media_role` の決め方も合わせて見直すこと
  (音声サイドカーの貼り直し判断に使われています)。
- **F1 / F6** は採点の実行時間とメモリに影響します。2 時間 VOD で必ず実測し、
  進捗が止まって見えないこと (watchdog に殺されないこと) を確認してください。
- **B5 (`condition_on_previous_text=True`)** は `docs/error/20260626/resolve.md` の
  反復ループ対策を緩める変更です。評価セットで反復が出ないことを確認するまで
  **既定は False のまま**にしてください。
- **F3 案 C (4 キーを削除)** は既存 `setting.json` との互換に触れます。
  `_merge_with_defaults` は未知キーを残すため害はありませんが、
  設定画面に出ている場合は表示も消す必要があります。
- **F3 案 B (SER モデル)** は §2.4.3 のとおり**入力がミックス 1 本だと精度が出ません**。
  重みファイルを足す前に Q9 を確定させてください。
  実装するときは `blur/models.py` と同じ「モデルが無ければ機能を落として続行」に倒し、
  **感情点 0 で採点が完走すること**をテストで固定すること (blur の前例に倣う)。
- **F14 (トラック選択)** は `0:a:0` の決め打ちを 4 ファイルから 1 ヘルパへ集約する変更です。
  波形表示・プレビュー音声・採点・無音カットが**同じトラックを見ていること**が崩れると
  「波形と音が違う」類の分かりにくい不整合になります。
  集約と差し替えは 1 コミットで行い、既存プロジェクトは索引 0 とみなしてください。

## 8. 追加予定の設定キー (一覧)

```jsonc
"general": {
  "audio_track_index": 0                      // F14 使用する音声トラック (VOD には効かない)
},
"archive": {
  "cut_mode": "copy",                         // F5  保留 (§11-1)。当面は "copy" のまま
  "scoring": {
    "feature_mode": "per_cell",               // F6  "single_pass" で astats 1 パス
    "loud_ratio_threshold": 0.2,              // F2  大声を「時間の割合」で見る
    "silence_source": "level",                // F2  "vad" で非発話率へ
    "vad": {
      "enabled": true,                        // F1
      "threshold": 0.5,                       // F1
      "min_speech_ms": 250,                   // F1
      "min_silence_ms": 600                   // F1
    },
    "method_a": {
      "loudness_source": "cell",              // F1  "speech" で発話区間のみの音量
      "normalize_weights": true,              // F4  コメント無しなら 0..100 へ正規化
      "emotion_points": { "speech": 20 },     // F1  発話率への加点
      "comment": {
        "laugh_emotes": ["LUL", "KEKW", "OMEGALUL", "Pog"],  // F4
        "emote_point": 1,                     // F4
        "laugh_words": ["草", "笑", "くさ"]    // F4
      },
      "emotion_points": {
        "pitch_up": 8,                        // F3 案B′ ピッチ上昇
        "loud_burst": 10,                     // F3 案B′ 発話音量の突出
        "onset": 5                            // F3 案B′ 立ち上がりの急峻さ
      }
    },
    "emotion": {                              // F3 / §2.4
      "mode": "none",                         //   "pseudo"=案B′ (Q2 で採用 / 目標既定)
      "max_samples_per_cell": 3,              //   セルごとに推論する発話の本数
      "sample_max_sec": 4.0,                  //   1 本あたりの上限 (秒)
      "min_sample_sec": 1.0                   //   これ未満の発話は推論しない
    }
  },
  "clip_pipeline": {
    "cut_db_scope": "per_clip",               // F7  "vod" で VOD 単位の閾値
    "loudness_scope": "vod"                   // F9  Q3 により "vod" を目標の既定とする
  },
  "intro_card": {
    "match_body_level": true                  // F8  ★最優先: 本編ゲインを合わせる
  }
},
"volume_analysis": {
  "metric": "percentile",                     // F7  既存キーに値を追加
  "percentile": 10                            // F7
},
"subtitle": {
  "initial_prompt": "",                       // B1
  "hotwords": "",                             // B1
  "vocabulary": [],                           // B1
  "whisper_hallucination_silence_sec": 2.0,   // B5
  "whisper_no_repeat_ngram_size": 0,          // B5
  "low_confidence_logprob": -0.6,             // B2
  "low_confidence_word_prob": 0.4,            // B2
  "replacements": [],                         // B3
  "subtitle_min_chars": 16,                   // G1 ★最優先 (G5 までの暫定)
  "wrap_mode": "length",                      // G5 "width" で実寸 (px) 折り返し
  "min_line_ratio": 0.5,                      // G5 1 行の下限 (使える幅に対する割合)
  "initial_prompt_from_vod": true,            // B1 配信タイトル・ゲーム名を自動で種にする (Q1)
  "split_min_pause_sec": 0.35,                // G2
  "split_max_pause_sec": 1.0,                 // G2 下限に関わらず切る間の上限 (§12.2-2)
  "speaker_filter": "off",                    // F15 "flag" (印を付ける) / "drop" (落とす)
  "speaker_backend": "onnx",                  // F15 "mfcc" はモデル不要だが分離できず (§12.6)
  "speaker_model": "models/speaker.onnx",     // F15 要入手 (src/models/README.md)
  "speaker_providers": [],                    // F15 空 = CPU
  "speaker_threshold": 0.45,                  // F15 コサイン類似度の下限
  "speaker_core_ratio": 0.5,                  // F15 代表の声を求める際に残す割合
  "speaker_min_sec": 0.6,                     // F15 これより短い発話は判定しない
  "merge_max_pause_sec": 0.20,                // G2
  "split_on_phrase_boundary": true            // G3
},
"ffmpeg": {
  "audio_bitrate": "256k"                     // F11 ★最終出力に反映
},
"loudness": {
  "verify_output": true,                      // F10
  "correct_output": true,                     // F10
  "correct_tolerance_lu": 0.5,                // F10
  "normalize_op_ed": false,                   // F10
  "min_duration_sec": 5.0,                    // F12
  "target_tp": -1.5                           // F13 (既定値の変更)
}
```

`whisper_compute_type` は既存キーのまま、受け付ける値を
`int8` / `int8_float32` / `int16` / `float32` へ広げます (B4)。

## 9. 不明点と回答 (2026-10-23 時点)

### 9.1 回答済み (方針確定)

| # | 内容 | 回答 | 反映先 |
| --- | --- | --- | --- |
| **Q1** | 配信タイトル・ゲーム名を `initial_prompt` の種に自動で差し込んでよいか | **差し込んでよい** | §3.2 B1 (`subtitle.initial_prompt_from_vod`) |
| **Q2** | 感情点 4 キーの扱い (案 A / B′ / B / C) | **案 B′** (発話区間の擬似感情特徴) | §2.3 F3 / §2.4.5。案 B は不採用 |
| **Q3** | ラウドネス正規化の単位 | **1 本にまとめて出力し、そのまま YouTube へ** → `"vod"` + F10 必須 | §4.3 F9 / F10 |
| **Q4** | 結合 / 分割操作をどの画面に出すか | **Timeline 編集画面だけ** | §3.4 G4 (実装が半分に) |
| **Q5** | テロップ 1 本の目安文字数 | **「2 行で画面外に出ないこと」。フォント次第なので文字数ではなくテロップ(実寸)次第** | **§3.4 G5 を新設** (px 折り返し)。G1 は暫定へ |
| **Q6** | `cut_region` の精度 (F5) | **今後の課題として後回し** | **§11-1 へ退避** (実施しない) |
| **Q7** | 評価セット用の VOD を事前に提供いただけますか | **実装・テストの後に報告でよい** (事前提供は不要) | **§6.0 / §6.1** — 計測を実装へ埋め込む方式へ変更 |
| **Q9** | 切り抜きの入力 | **`twitch-dl` 取得の VOD が主** | **§11-2 へ退避** (F14 保留 / 案 B 不採用) |

### 9.2 未回答 (判断待ち)

| # | 内容 | 影響 | 備考 |
| --- | --- | --- | --- |
| **Q8** | 最終出力の音声ビットレート (F11) の目標値。YouTube 投稿なら 256k / 384k いずれでも実害はありませんが、ファイルサイズとの兼ね合いで決めてください | F11 (段 1) | 未回答でも **256k 既定**で進められます (現状の約 128kbps より確実に改善するため) |


---

## 10. 参照した実装箇所

| 機能 | ファイル:行 | 内容 |
| --- | --- | --- |
| 採点の統括 | `src/archive/pipeline.py:18-82` | `analyze` (特徴 → 採点 → TOP 選択) |
| セル特徴抽出 | `src/archive/features.py:28-72` | `extract_cells` (セルごとに `volumedetect`) |
| 窓の採点 | `src/archive/scoring.py:52-110` | `score_window` (`loud` / `long_silence` のみ使用) |
| 大声しきい値 | `src/archive/scoring.py:34-41` | `loud_threshold` (`max_db` の 80 パーセンタイル) |
| TOP 選択・統合 | `src/archive/scoring.py:170-237` | `select_top_events` |
| コメント集計 | `src/archive/comment_source.py:138-154` | `aggregate_into_cells` (`emotes` 未使用) |
| 笑いカウント | `src/archive/comment_source.py:27-46` | `count_w_chars` |
| 区間切り出し | `src/archive/clip_writer.py:62-77` | `cut_region` (`-c copy`) |
| クリップ準備 | `src/archive/clip_writer.py:513-582` | `prepare_one_clip` (正規化 → 閾値 → 編集点) |
| クリップ正規化 | `src/archive/clip_writer.py:528` | `normalize_file` (クリップ単位) |
| カット閾値 | `src/archive/clip_writer.py:403-425` | `_apply_volume_analysis` (ダイアログ無し) |
| 編集点と字幕 | `src/archive/clip_writer.py:447-467` | `_prepare_edit_points` |
| 従来画面の認識 | `src/archive/clip_writer.py:470-492` | `_transcribe` (`regroup` を通らない) |
| イントロカード | `src/archive/clip_writer.py:282-335` | `_build_intro_card` (正規化外) |
| 結合パーツ | `src/archive/clip_writer.py:984-1011` | `_build_combine_parts` / `_combine` |
| 経路の判定 | `src/archive/config.py:245-262` | `clip_pipeline_config` / `use_timeline_review` |
| 採点設定 | `src/archive/config.py:37-55` | `method_a_config` |
| 結合の再エンコード | `src/modules/concat_processor.py:30-78` | `concat` (`-b:a` が無い) |
| エンコード設定 | `src/modules/ffmpeg_runner.py:314-320` | `build_encode_options` (`-b:a` が無い) |
| 閾値解析 | `src/modules/volume_analyzer.py:68-140` | `analyze_min_speech_db` (mean の最小値) |
| 認識パラメータ | `src/modules/subtitle_generator.py:363-517` | `WhisperTextSource` |
| 認識結果の取り込み | `src/modules/subtitle_generator.py:417-441` | `_recognize` (信頼度を捨てている) |
| テロップ単位 | `src/modules/subtitle_generator.py:1681-1744` | `regroup_items_by_length` |
| 実効値の決定 | `src/modules/subtitle_generator.py:1855-1880` | `min_chars` の二重用途 |
| 正規化 | `src/modules/loudness_normalizer.py:130-230` | 2 パス loudnorm |
| 字幕分割 | `src/timeline/commands.py:270-303` | `split_clip_at` (本文を複製) |
| 字幕クリップ | `src/timeline/model.py:285-380` | `origin` は dict でそのまま JSON へ入る |


## 11. 今後の課題 (保留 / 本書では実施しない)

> 回答を受けて**意図的に見送った項目**の控えです。後から「なぜ入れなかったのか」を
> 辿れるようにするため、根拠ごと残します。

### 11-1. 切り抜き区間のフレーム精度 (F5 / A6) — Q6 により後回し

- **症状**: `cut_region` が `-c copy` のため、採点した区間と実際の切り抜きが
  **最大 1 GOP (配信 VOD では概ね 2 秒) 前へずれる** (§2.2 A6)。
- **保留の理由**: Q6 の回答により今後の課題とする。
  前後 120 秒のマーカー区間や 180 秒の採点区間では相対的に影響が小さく、
  再エンコードへ変えると切り出し時間が増えるうえ、
  **プロジェクト再開時の素材復旧 (`project_resume`) の決定性**にも触れるため。
- **着手するときの条件**: `archive.section_add.min_length_sec` (現在 1.0) のような
  **短い区間を日常的に使い始めたら**、ずれが区間長と同じオーダーになるため優先度が上がります。
- **着手するときの設計**: §2.3 F5 に記載済み
  (`archive.cut_mode` の追加 + プロジェクトへの `cut_mode` 記録による後方互換)。

### 11-2. 音声トラックの選択 (F14) — Q9 により保留

- **保留の理由**: 入力が `twitch-dl` 取得の VOD であるため、
  **音声は配信時点でミックスされた 1 本 (AAC ステレオ) しか存在しません**。
  トラックを選べるようにしても選ぶ対象がありません (§2.4.3)。
- **着手するときの条件**: OBS の複数トラック録画をローカル入力する運用を始めたとき。
  そのときは `0:a:0` 決め打ちを 1 ヘルパへ集約する F14 が**最も安い勝ち筋**になります
  (マイク単独トラックを選べるだけで A1 がほぼ解消する)。

### 11-3. 音声感情認識 (F3 案 B / SER モデル) — Q2・Q9 により不採用

- **不採用の理由**: 発話区間のみでの実行は可能で、`onnxruntime` と
  `src/blur/models.py` の作法も揃っているため**実装コストは低い** (§2.4.4)。
  しかし入力がミックス 1 本では**ゲーム音・BGM が声に重なったまま**モデルへ入り、
  SER は背景音に弱いため精度が出ません (§2.4.3)。
- **代替**: 案 B′ (ピッチ上昇・発話音量の突出・立ち上がりの急峻さ) を採用。
  相対値で測るためゲーム音の底上げを相殺でき、依存も増えません (§2.4.5)。
- **着手するときの条件**: 11-2 と同じ (マイク単独トラックが取れる運用になったとき)。

### 11-4. 音源分離 — 本書では提案しない

- 新規依存・数百 MB・実行時間大で `docs/claude.md` の
  「不要なライブラリを追加しない」に触れます。
- VOD 入力で声を分離する唯一の手段ではありますが、
  案 B′ で「相対値で測る」ことにより**分離せずに済ませる**方針を採ります。

---

## 12. 検証結果 (2026-10-23 / `docs/quality/kensyou`)

段 0〜段 2 を実装したアプリで実際の VOD を 1 本処理し、Timeline 画面まで到達した
状態のプロジェクトを検証しました。

| 項目 | 値 |
| --- | --- |
| 素材 | `vod_2887226037.mp4` (Twitch VOD / 1,747 秒 / 1920×1080 / 60fps) |
| 音声 | **AAC ステレオ 1 本のみ** (Q9 の前提どおり。マイク単独トラックは存在しない) |
| 採点結果 | 10 クリップ / スコア 41.0 など |
| 生成テロップ | **111 件** |
| ゲーム内容 | 空戦ゲーム (無線通信・カットシーンの音声が常時流れる) |

### 12.1 効いていることを確認できた実装

| 項目 | 確認方法 | 結果 |
| --- | --- | --- |
| **G5** 実寸 (px) 折り返し | 出力テロップの 1 行の文字数 | 中央値 **17 文字** / 最大 25 文字 = 1820px 基準で折れている (文字数モードなら一律 20 文字) |
| **G1** テロップ下限 | 1 テロップの文字数 | 中央値 **22 文字** (下限 16 文字が効いている) |
| **F9** VOD 単位の正規化 | ログ | 「ラウドネス測定を省略し VOD 全体の実測値を使用 (F9): I=-9.2 LUFS」 |
| **F13** 適用方式の記録 | ログ | 「適用ゲイン: **-4.80 dB** (線形)」= F8 がイントロへ渡す値が取れている |

> F8 / F10 / F11 は焼き込み・出力の工程で働くため、**今回の実行 (Timeline 画面まで) では
> まだ通っていません**。出力まで回していただければ検証できます (§6.0 (A) の表)。

### 12.2 検証で見つけた不具合 2 件 (いずれも修正済み)

#### 12.2-1. `origin` に新フィールドが入っていなかった (実装漏れ)

`asr_text` / `confidence` / `word_confidence` / `words` が **111 件すべてで 0 件**でした。

- **原因**: 切り抜きは `src/archive/timeline_builder.py` で Timeline を組むのに、
  §6.1 / B2 の実装を `src/timeline/builder.py` (本流) 側にしか入れていなかった。
- **影響**: 信頼度の色分け (B2)・無修正テロップ率の計測 (§6.1)・
  テロップ分割の語境界 (G4) がいずれも働かない。
- **対策**: `archive/timeline_builder.py` の `_append_subtitles` へ本流と同じ内容を追加。

#### 12.2-2. テロップが最大 76.8 秒に伸びていた (G1 による回帰)

```
76.8s / 17 文字  「22発あぶねーってパルコネルを撃沈」
51.2s / 17 文字  「いいよマジか当たると思ったのにでも」
46.1s / 18 文字  「くらった広告代理店の方すごい大変そう」
→ 表示尺 中央値 7.6 秒 / 最大 76.8 秒 / 10 秒超が 42 件 (111 件中)
```

- **原因**: `regroup_items_by_length` の区切り判定が
  `if over or far or (enough and soft)` の形で、
  **文字数の下限 (`enough`) を満たすまで間・文末・カット境界のどれも効かない**。
  G1 で下限を 5 → 16 文字へ上げたため、疎な発話が「16 文字たまるまで」
  何十秒でも 1 本へ繋がるようになった。
  (下限 5 文字の頃も同じ穴はあったが、すぐ下限に達するため目立たなかった)
- **対策**: `subtitle.split_max_pause_sec` (既定 **1.0 秒**) を追加し、
  **下限文字数に関わらず、間がこれ以上空いたら必ず切る**ようにした。
  回帰テスト 4 件付き (`tests/test_quality_resolve1.py::PauseCeilingTest`)。

### 12.3 ゲーム内ボイスがテロップになる (新規の問題 A9)

> **利用者の指摘**: 「Timeline 画面で見た限りでは、ゲーム内のボイスに反応して
> テロップが生成されています。これは仕方ないことなのでしょうか？」

111 件のテロップのうち、内容から判別できたものは **ゲーム内音声 24 件 / 配信者 14 件**
(残り 73 件は両者が混ざるなどで判別困難)。実例:

```
ゲーム  「ジョーカー隊、着艦せよジョーカー1、レーダーを発進した着艦体制に入れ」
ゲーム  「こちらは近距離通信のみ使用可能こちらホワイトバレー基地オーディンだ」
ゲーム  「任せろり同直消火器が敵編隊らしきものを捕捉対空戦闘、空襲に備えよ」
配信者  「東京ゲームショーの時に思ったけど結構重いんだよな」
配信者  「いやーもうF14でしょこれはこれはもうF14しかねえ」
```

#### 12.3-1. 本書 §2.2 A1 の枠組みが不十分だった (訂正)

A1 を「**声かゲーム音か**を区別していない」問題として書きましたが、
実際に起きているのは「**配信者の声かゲーム内の声か**」という別の区分でした。
ゲーム内のボイスは**音響的には人の声そのもの**なので:

| 対策 | ゲーム内ボイスに効くか | 理由 |
| --- | --- | --- |
| **F1** Silero VAD (発話検出) | **効かない** | 声なので発話として検出される |
| 音源分離 (§11-4) | **効かない** | 声なので分離しても残る |
| **F14** マイクトラック選択 | 効くが**VOD では不可能** | ミックス 1 本しか無い (Q9 / §11-2) |

→ **F1 を入れてもこの問題は解決しません。** A1 (ゲーム音が盛り上がりの採点を
歪める) と A9 (ゲーム内ボイスがテロップになる) は**別の問題**として扱う必要があります。

#### 12.3-2. 切り分けの手掛かりを 3 つ実測した

人が判別したテロップについて、`docs/quality/kensyou` の VOD を実測しました。

| 手掛かり | ゲーム | 配信者 | 判定 |
| --- | --- | --- | --- |
| **ステレオ定位** (サイド−ミッド) | 平均 -20.0 dB / 範囲 -36.5〜-7.5 | 平均 -31.5 dB / 範囲 -38.1〜-26.1 | **重なりが大きく使えない** |
| **帯域** (低域−中域) | 平均 -4.1 dB / 範囲 -9.6〜+6.0 | 平均 -1.7 dB / 範囲 -10.5〜+2.8 | **ほぼ同一。使えない** |
| **帯域** (高域−中域) | 平均 -18.4 dB | 平均 -19.5 dB | **ほぼ同一。使えない** |
| **区間音量** (ミッドの mean) | 中央値 **-46.5 dB** / 範囲 -54.6〜-25.6 | 中央値 **-29.6 dB** / 範囲 -61.3〜-20.5 | **17 dB の差。かなり効く** |

- ステレオ定位は「マイクは中央・ゲーム音は定位を持つ」という読みでしたが、
  **ゲームの無線通信も中央に定位している**ため分かれませんでした。
- 帯域は「無線ボイスは帯域制限されている」という読みでしたが、
  この作品のボイスは**フルバンドの収録**で加工が浅く、分かれませんでした。
- 音量が効くのは、**配信者がマイクをゲーム音より上に出してミックスしている**ためです。

#### 12.3-3. 音量のしきい値を振った分離性能

```
   閾値     配信者を残す   ゲームを除く
  -50 dB      12/14 (86%)    3/24 (13%)
  -46 dB      11/14 (79%)   13/24 (54%)
  -42 dB      10/14 (71%)   17/24 (71%)
  -38 dB      10/14 (71%)   17/24 (71%)
  -34 dB       9/14 (64%)   22/24 (92%)   ← ゲームを最も多く除ける点
  -30 dB       7/14 (50%)   22/24 (92%)
  -26 dB       1/14 ( 7%)   23/24 (96%)
```

**自動で捨てるには危険**です (-34 dB でゲームの 92% を除ける一方、配信者も 36% 失う)。
外れる実例:

```
ゲームなのに大きい  48:26 -25.6 dB 「マイガー敵変態が侵入なんとかしのいてくれ」
                   46:24 -27.4 dB 「ジョーカー隊、着艦せよ」
配信者なのに小さい  56:26 -61.3 dB 「対危なかったと思うんだけどな今の絶」
                   56:32 -53.2 dB 「対しよかったでしょ出たーあーでもミサイル撃っちゃったな」
```

配信者側が小さく出るのは、**テロップの区間全体を平均しているため**です
(長いテロップは無音を含んで平均が下がる)。発話区間に絞って測れば差は開きます。

### 12.4 3 行になるテロップがある (H1 / 修正済み)

`subtitle_max_lines = 2` にもかかわらず **3 行のテロップが 3 件**ありました。

```
42文字 / 3行  「港中止クイーン隊、ジョーカー隊、\N緊急発艦AX、リーラー、まず出ろ続いて\Nクイーン1から」
30文字 / 3行  「イーン隊早く帰ったらどうぞ1478兵隊集積所へ\Nの\N攻撃を完了早」   ← 1 文字の行
```

- **原因**: 1 本のテロップに使える幅 (`measure.scaled(2)` = 1 行 × 2) と、
  実際の折り返し (1 行ごとの幅) を**独立に計算している**ため。
  テロップとしては 2 行分の幅に収まっていても、BudouX の文節境界が早く来ると
  1 行が短くなり、結果として 3 行に折れる。
- **各行は上限 1820px に収まっている**ので「画面外に出ない」という要望の条件は
  満たしていますが、`subtitle_max_lines` の意図からは外れています。
#### 対策 (実施済み)

1. **行数そのもので上限を見る**: `regroup_items_by_length` の上限判定
   (`_fits_within`) に、整形関数を実際に走らせて**折り返し後の行数**を数える経路を
   足しました。`subtitle_max_lines` を超えるならそこで切ります。
   毎語ごとに BudouX を走らせると重いため、**最終行に入ってからだけ**数えます
   (`_near_last_line`)。
2. **短すぎる行を前の行へ寄せる**: `_merge_short_lines` を追加し、
   3 文字未満の行は前の行へ足します (足して上限を超える場合は寄せません。
   無理に詰めて画面外へ出すより、短い行で見せる方が害が小さいため)。

- 新設定は不要です (既存の `subtitle_max_lines` を使います)。
- `max_lines=0` を渡せば従来どおり幅だけで判断します (切り戻し経路)。
- 回帰テスト 6 件を追加
  (`tests/test_quality_resolve1.py::MaxLinesTest` / `ShortLineTest`)。

### 12.5 A9 への対策案

#### F16. 音量を「要確認」の判定へ加える (不採用 — §12.9)

**自動では消さず**、B2 の色分けに音量を足して利用者が一括で落とせるようにします。

- 認識時に、テロップの**発話区間の音量** (単語の時刻で区切った区間の mean dB) を測り
  `origin["speech_db"]` へ残す
- `subtitle.low_level_db` (既定 -38) を下回るテロップを B2 と同じ色分けの対象にする
- 編集画面に「**要確認だけ表示**」フィルタと「**要確認を一括で使用しない**」を足す
- 利点: 誤判定で配信者の声を失わない。ゲームの 7〜9 割に印が付く (§12.3-3)
- 新設定 `subtitle.low_level_db` / `subtitle.flag_low_level` (既定 true)

#### F15. 話者照合で配信者以外を落とす (実装済み / この素材では不採用 — §12.7)

配信者は VOD 全体で**一貫した 1 人の声**、ゲームは**多数の別人の声**です。
この非対称性を使うのが確実な解です。

1. 発話区間ごとに**声紋 (speaker embedding)** を取る (ECAPA-TDNN / x-vector 等)
2. 埋め込みをクラスタリングし、**最も発話時間が長いクラスタ = 配信者**とみなす
3. そのクラスタから遠い発話を落とす (または「サブ」役割にして利用者へ委ねる)

- **依存は ONNX モデル 1 つだけ**です。`onnxruntime` (32MB) は既に配布物へ入っており、
  `src/blur/models.py` に遅延 import / セッション共有 / モデルが無ければ機能を落とす
  作法が完成しています (§2.4.4 と同じ条件)。
- **感情認識 (案 B / §11-3) と違い、背景音に強い**: 必要なのは「同じ声か違う声か」の
  相対判定で、しかも配信者の声は VOD 全体で数百回聞けるため中心が安定します。
- 計算量は §2.4.2-2 と同じ考え方で絞れます (発話区間のみ・セルごとに代表サンプル)。
  埋め込みは SER より軽く、1 発話あたり数ミリ秒です。
- 新設定 `subtitle.speaker_filter` (`"off"` / `"flag"` / `"drop"`)、
  `subtitle.speaker_backend` (`"onnx"` / `"mfcc"`)、
  `subtitle.speaker_model` (`"models/speaker.onnx"`)、
  `subtitle.speaker_threshold` / `speaker_core_ratio` / `speaker_min_sec`

**実装は完了しています** (`src/modules/speaker_filter.py` / 単体テスト 19 件)。
WeSpeaker の ONNX モデル (ECAPA512_LM / ResNet34_LM) まで入れて実測しましたが、
**この素材 (VOD のミックス 1 本) では成立しませんでした** (§12.7)。

> 同じ発話を前半と後半に割った声紋の一致 (自己一致) が **0.13〜0.40** しか出ません
> (正常なら 0.85〜0.95)。**1 本のテロップの中ですら前半と後半が別人に見える**ため、
> 話者を同定する手がかりがありません。実装・特徴量・推論はいずれも正常です
> (同じ入力で +1.0000)。
>
> さらに「配信者が多数派」という前提も崩れ、**ゲーム側が代表になりました**。
> ゲームのボイスは別人でも同じ収録・処理を通るため声紋空間で密集するためです。

**既定は `off`、モデルは同梱しません。** 実装は残します
(マイク単独トラックの録画へ移れば、設定を `"flag"` にするだけで使えます)。
モデルの要件と入手手順は `src/models/README.md` にあります。

### 12.6 F15 を実測した結果 — MFCC 方式では分離できない

「まず試してみる」ご指示を受けて実装し、`docs/quality/kensyou` の VOD と
人が判別したラベル (配信者 14 件 / ゲーム 24 件) で測りました。

#### 12.6-1. 結果

| 方式 | 配信者を残す | ゲームを除く |
| --- | --- | --- |
| MFCC そのまま (L2 正規化のみ) | 7/14 (50%) | 19/24 (79%) |
| MFCC + centering (全体平均を引く) | 7/14 (50%) | 17/24 (71%) |
| MFCC + centering + 白色化 | 13/14 (93%) | 6/24 (25%) |
| **(比較) 区間音量による判定 = F16** | **9/14 (64%)** | **22/24 (92%)** |

**MFCC 方式は音量による判定より劣ります。** 類似度の分布も重なっていました。

```
配信者 14 件  類似度 中央値 +0.026 / 範囲 -0.660〜+0.476
ゲーム 24 件  類似度 中央値 -0.257 / 範囲 -0.745〜+0.639
```

誤り例:

```
ゲームなのに配信者に近い  +0.63「マイガー敵変態が侵入なんとかしのいてくれ」
配信者なのに遠い          -0.66「いや絶対完璧じゃなかった絶対フック引っかかってない」
```

#### 12.6-2. 原因

**背景にゲーム音が常に乗っているため、MFCC の統計量が「ミックス全体の音色」に
支配されます。** 正規化しただけではどのベクトルも同じ方向を向き
(コサイン類似度が全件 0.97〜0.99)、話者の差が埋もれていました。

全体平均を引く (centering) と中央値の差は出ましたが
(配信者 +0.026 / ゲーム -0.257)、分布の重なりは残りました。
MFCC の統計量 (supervector) は**クリーンな単独話者音声**向けの手法で、
常時 BGM が乗った素材には向きません。

#### 12.6-3. 実装でどう扱ったか

- **既定の方式を `"onnx"` にしました。** モデルが無いときは
  **MFCC へ勝手に落ちず、絞り込みを行いません** (ログに 1 行出る)。
  実測で分離できなかった方式を黙って使うと、
  「話者照合を入れたのに効かない」という分かりにくい状態になるためです。
- `"mfcc"` を明示的に選んだときだけ使えます (選ぶと警告が 1 行出ます)。
- centering は実装へ取り込みました (`_center`)。ONNX モデルでも
  背景音の共通成分を除く効果があるため、方式に関わらず効かせます。
- **既定は `speaker_filter="off"`** です。モデルを置いて `"flag"` にすると
  印が付き、精度を確かめてから `"drop"` へ上げられます。

#### 12.6-4. 次に必要なもの

**ONNX の話者照合モデル 1 つ**です。要件は
「入力 `[1, T]` の 16kHz モノラル波形 → 出力 `[1, D]` の埋め込み」だけで、
ECAPA-TDNN / x-vector / ReDimNet のいずれでも構いません。
[WeSpeaker](https://github.com/wenet-e2e/wespeaker) が ONNX を公開しています。

- 入手手順とライセンスの注意は `src/models/README.md` に書きました。
- 配置すれば **設定を `"flag"` にするだけで動きます** (コード変更は不要)。
- ネットワーク取得と配布ライセンスの確認が必要なため、
  **モデルの入手はご判断のうえお願いします。**

> なお §12.3-3 の実測では、区間音量による判定 (F16) が MFCC 方式より
> 精度が高く (ゲーム 92% 除去 / 配信者 64% 保持) 出ていましたが、
> **F16 は不採用と決まりました** (§12.9)。

> **結論 (利用者の問いへの答え)**: **仕方なくありません**が、F1 (VAD) では解決しません。
> 検討した 2 つの対策は、F15 (話者照合) が実測で不成立 (§12.7)、
> F16 (音量で要確認マーク) が不採用 (§12.9) となりました。
> **A9 に対する自動の対策は入れず、編集画面で手で落とす運用とします。**

### 12.7 ONNX モデルを入れて実測した結果 — 話者照合はこの素材では成立しない

「ONNX モデルを入れてテストする」ご指示を受けて実行しました。
**結論: 実装は正しく動いていますが、この素材では話者照合が成立しません。**

#### 12.7-1. 試したモデル

| モデル | 由来 | サイズ | 入出力 | ライセンス |
| --- | --- | --- | --- | --- |
| `voxceleb_ECAPA512_LM.onnx` | [WeSpeaker](https://github.com/wenet-e2e/wespeaker) | 24 MB | 入力 `feats [B, T, 80]` → 出力 `embs [B, 192]` | CC-BY-4.0 (VoxCeleb) |
| `voxceleb_resnet34_LM.onnx` | 同上 | 26 MB | 入力 `feats [B, T, 80]` → 出力 `embs [B, 256]` | CC-BY-4.0 (VoxCeleb) |

**WeSpeaker の ONNX は波形ではなく 80 次元の fbank を取ります。**
`speaker_filter.py` を `torchaudio.compliance.kaldi.fbank` で特徴量を作る形へ直し、
WeSpeaker の `infer_onnx.py` と同じ設定 (25ms/10ms・hamming・dither 0・
波形を `1<<15` 倍・フレーム方向の平均を引く CMN) に合わせました。
入力が 3 次元かつ最後の次元が 80 なら fbank、そうでなければ波形を渡すため、
別の作りのモデルにも当たります。

#### 12.7-2. 実装は正しく動いている

| 確認項目 | 結果 | 意味 |
| --- | --- | --- |
| 同じ入力を 2 回 | **+1.0000** | 実装は決定的で安定している |
| 無音 vs 発話 | +0.067 | モデルは音を正しく見ている |
| 声紋が取れた区間 | 111/111 件 | 特徴量・推論は全区間で成功 |
| 処理時間 | 111 区間で約 25 秒 | 実用的な速さ |

#### 12.7-3. しかし声紋が話者を表していない

**決定的なのは「自己一致」です。同じ発話を前半と後半に割って声紋を比べました。**
正常な話者照合なら同一話者なので **0.85〜0.95** になるはずですが:

```
区間音量と自己一致 (同一発話の前半 vs 後半 / 107 区間)
  小さい 1/3   音量 -46.7 dB → 自己一致 中央値 +0.402
  中くらい     音量 -38.7 dB → 自己一致 中央値 +0.339
  大きい 1/3   音量 -30.0 dB → 自己一致 中央値 +0.133   ← 大きいほど悪い
```

**同じ人が続けて喋っている 1 本のテロップの中ですら、前半と後半が別人に見えています。**
しかも音量が大きい区間ほど悪化します。大きい区間は
「配信者とゲーム音が同時に大きい場面」(戦闘・爆発・複数の声が重なる) だからです。

その結果、ラベル間の差はほとんど出ません:

| 組み合わせ | 類似度の中央値 |
| --- | --- |
| 配信者どうし | +0.309 |
| ゲームどうし | +0.277 |
| 配信者 vs ゲーム | +0.167 |

**配信者どうし (+0.309) が自己一致 (+0.293) とほぼ同じ**で、
「同一話者」の信号が雑音に埋もれています。

#### 12.7-4. さらに「多数派 = 配信者」も成り立たなかった

F15 は「配信者が最も長く喋る 1 人」という前提で代表の声を求めますが、
実測では**ゲーム側が代表になりました**。

```
代表との類似度 (centering なし): 配信者 +0.317 / ゲーム +0.511 → ゲームが代表
代表との類似度 (centering あり): 配信者 -0.212 / ゲーム +0.039 → ゲームが代表
```

ゲームのボイスは**別人でも同じ収録・同じ処理を通っている**ため、
声紋空間で密集します。一方、配信者の発話は場面ごとに背景が変わるため散らばります。
「多数派」を取ると密集しているゲーム側が選ばれてしまいます。

分離性能も実用に届きません。

| 方式 | 配信者を残す | ゲームを除く |
| --- | --- | --- |
| ECAPA512_LM (centering なし) | 2/14 (14%) | 23/24 (96%) |
| ECAPA512_LM (centering あり) | 3/14 (21%) | 21/24 (88%) |
| (参考) MFCC 方式 | 7/14 (50%) | 17/24 (71%) |
| **(参考) 区間音量 = F16** | **9/14 (64%)** | **22/24 (92%)** |

#### 12.7-5. 切り出し区間が正しいことの裏取り

「そもそも違う音を測っていないか」を確かめました。
**§12.3-3 の音量測定は、ここと同じ時刻計算**
(`clip.vod_start + origin.source_start`) で切り出しています。
その測定で配信者とゲームが **17 dB** 分離できていたので、
切り出し区間はラベルどおりの音を指しています。
つまり時刻の対応ではなく、**音そのものが混ざっている**ことが原因です。

#### 12.7-6. 結論と、実装をどう残したか

> **話者照合 (F15) は、VOD のミックス 1 本という入力では成立しません。**
> モデルの選択でも実装でもなく、**どの区間にも単一話者の綺麗な姿が無い**ためです。
> 自己一致が 0.13〜0.40 しか出ない音声から話者を同定する方法はありません。

- **ONNX 実装は取り除きました** (§12.8)。効果が無いと確定した経路を残すと、
  「設定があるのに効かない」状態 (A3 と同じ) を新しく作ってしまうためです。
- **枠組みと MFCC 方式は残します。** `src/modules/speaker_filter.py` は
  単体テスト 19 件つきで、**綺麗な音声なら動く**作りです。
  マイク単独トラックの録画 (F14 / §11-2) へ移る日が来れば、
  設定を `"flag"` にするだけで試せます。
- **既定は `speaker_filter="off"`** のままです。
  有効にすると「実測では分離できていない」旨の警告が 1 行出ます。
- 検証中に `blur/models.py` のセッションキャッシュがパスだけを鍵にしていたため、
  **同じ名前でモデルを差し替えても古いセッションが返る**問題を見つけ、
  更新時刻とサイズを鍵へ加えて直しました
  (2 つのモデルで数値が完全に一致する、という形で現れて気付きにくい問題でした)。

#### 12.7-7. 残る道

| 手段 | 見込み | 条件 |
| --- | --- | --- |
| ~~F16 音量で要確認マーク~~ | 実測では最良 (ゲーム 92% / 配信者 64%) | **不採用と決定 (§12.9)** |
| F14 マイク単独トラック + F15 | 原理的に確実 | OBS の複数トラック録画へ移る必要がある (Q9 で VOD 主と回答済み) |
| 音源分離 → F15 | 不明 | 新規依存・数百 MB。分離後も「ゲーム内の声」は残るため効果は限定的 |

**その後 F16 も不採用と決まりました (§12.9)。** A9 に対する自動の対策は入れません。

---

### 12.8 ONNX 実装を取り除いた (2026-10-23)

§12.7 の実測で「VOD のミックス 1 本では話者照合が成立しない」ことが確定したため、
**ONNX モデルを読む経路だけを取り除きました**。

| 取り除いたもの | 理由 |
| --- | --- |
| `_onnx_embedder` / `_compute_fbank` (fbank 特徴量) | 効果が無いと確定した経路。残すと「設定があるのに効かない」状態 (A3 と同じ) を新しく作る |
| 設定 `subtitle.speaker_backend` / `speaker_model` / `speaker_providers` | 同上 |
| `src/models/README.md` の話者照合モデルの節 | 同梱しないため (元の内容へ戻した) |

| 残したもの | 理由 |
| --- | --- |
| `src/modules/speaker_filter.py` の枠組みと MFCC 方式 | 綺麗な音声なら動く作り。マイク単独トラックへ移れば試せる (単体テスト 19 件) |
| 設定 `speaker_filter` / `speaker_threshold` / `speaker_core_ratio` / `speaker_min_sec` | 同上。既定は `off` |
| `origin["speaker_score"]` / `origin["speaker_other"]` と編集画面の色分け | 有効にしたときの表示経路。無効なら値が入らないため害は無い |
| **`blur/models.py` のキャッシュ修正** | **F15 とは独立した不具合の修正**。パスだけを鍵にしていたため、同名でモデルを差し替えても古いセッションが返っていた (ぼかしのモデルでも起きる) |

- 有効にすると「実測では分離できていない」旨の警告がログへ 1 行出ます。
- ダウンロードした検証用モデル (24〜26 MB) は配布物へ入れず、削除しました。
- テストは 1,694 件すべて通ります。

### 12.9 F16 (音量で要確認マーク) は不採用 (2026-10-23)

**利用者の判断により、音量で要確認マークを付ける対策 (F16) は入れません。**

これにより、**A9 (ゲーム内ボイスがテロップになる) に対する自動の対策は無くなります。**

| 対策 | 状態 |
| --- | --- |
| F1 Silero VAD | ゲーム内ボイスは人の声なので**効かない** (§12.3-1) |
| F14 マイクトラック選択 | VOD はミックス 1 本のため**使えない** (§11-2) |
| F15 話者照合 | ONNX モデルまで入れて実測したが**成立しない** (§12.7)。ONNX 実装は除去済み (§12.8) |
| **F16 音量で要確認マーク** | **不採用 (本節)** |
| 音源分離 | 提案していない。分離後も「ゲーム内の声」は残る (§11-4) |

#### 12.9-1. これで何が残るか

ゲーム内ボイスのテロップは**編集画面で手で落とす運用**になります。
実測では 111 件のうち**少なくとも 24 件**が明らかにゲーム内音声でした (§12.3)。

ただし、今回の実装で手作業そのものは軽くなっています。

| 実装済みの機能 | A9 の手作業への効き方 |
| --- | --- |
| **G4 テロップの結合 / 分割** (§3.4) | 不要なテロップを選んで落とす操作が素直になる (Ctrl+M / 本文も分かれる分割) |
| **B2 信頼度の色分け** (§3.2) | ゲーム内ボイスは認識の信頼度も低く出やすいため、色が付くものが一部重なる |
| **G1 / G2 / G5 テロップ単位** | ゲーム内ボイスと配信者の声が**1 本のテロップへ混ざりにくくなる**ため、落とす判断がしやすい |

#### 12.9-2. 再検討するとしたら

| 条件 | 見込み |
| --- | --- |
| **OBS の複数トラック録画へ移る** | F14 + F15 で原理的に解決する。現在は VOD が主 (Q9) |
| ゲーム内ボイスの手作業が実際に重い | F16 を入れ直す。実装は小規模で、設計は §12.5 に残してある |

**判断の記録**: 自動で消す対策は誤判定で配信者の声を失う恐れがあり
(F16 のしきい値 -34 dB では配信者の 36% を失う / §12.3-3)、
印を付けるだけでも画面の情報が増えます。
「自動で中途半端に判定するより、手で落とす」という選択です。
