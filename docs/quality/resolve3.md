# quality resolve3 — 書き出し時間の修正設計書

- 対象の調査結果: `docs/quality/resolve2.md`
- 作成日: 2026-10-30
- 更新日: 2026-11-01 (§13 の判断 6 件を確定し、R2 に高解像度素材の上限定義と
  テロップ相対サイズの付帯対応を追記)
- **実装完了日: 2026-11-01** — R1〜R11 / R13 の全件と §13-4 の本流適用まで実装済み。
  実装状況は §14 を参照。
- 範囲: resolve2.md の R1〜R13 のうち **R12 (GPU / QSV エンコード) を除く全件**
- 前提: 実装は本書のレビュー後に行う (`docs/CLAUDE.md` 「実装前に設計を行うこと」)

---

## 0. 概要

### 0.1 何を直すか

resolve2.md の実測により、アーカイブ切り抜きの書き出し 88 分のうち

- **73% が「同じ映像の 3 回エンコード」**
- **解析パスが映像まで復号している / 全長を 3 回流している**
- **進捗が 4 箇所で数分間止まり、中止手段が無い**

ことが分かった。本書はその 12 件 (R1〜R11・R13) の修正設計をまとめる。

### 0.2 効果 (6 セクション / 1800 秒 / resolve2.md の実機)

| 段階 | 採点 | 書き出し | 取得込みの通し |
| --- | --- | --- | --- |
| 現状 | 260 秒 | 5262 秒 (88 分) | 5693 秒 (**95 分**) |
| フェーズ 1 (R1/R2/R4/R5/R6/R7/R8 + R9〜R11/R13) | **13 秒** | **1907 秒 (32 分)** | 2091 秒 (**35 分**) |
| フェーズ 2 (+ R3a / R3b) | 13 秒 | **1604 秒 (27 分)** | 1788 秒 (**30 分**) |

**画質を落とす変更は 1 件も含まない。** R2 は拡大処理を無くすため画質は上がり、
R1 は再エンコード世代を 1 つ減らす。

上表は 720p30 の素材 (resolve2.md の実機) の値である。
**1080p 以上の配信アーカイブ (1080p / 2K / 4K) では R2 の分 (−1848 秒) が効かない。**
出力は従来どおり 1920x1080 になるためで、その場合の書き出しは
フェーズ 1 で 3755 秒 (63 分) 相当になる (R1/R4〜R8 の短縮のみ)。詳細は §3 R2 / §13-1。

また §13-5 の判断により **R3b は既定 OFF** とした。
フェーズ 2 の −303 秒のうち既定で効くのは R3a の −215 秒である。

### 0.3 フェーズ分け

| フェーズ | 内容 | 判断の根拠 |
| --- | --- | --- |
| **1-a** | R1 / R4 / R6 / R8 / R10 | 数行〜数十行。既存の挙動を変えずに無駄だけを落とす |
| **1-b** | R9 / R13 / R11 | 「中断」を止める。時間短縮とは独立に必要 |
| **1-c** | R2 / R5 / R7 | 設定キーを増やす。切り戻し経路を必ず持たせる |
| **2** | R3a / R3b | レンダリング経路の構造に触る。1-c の効果を測ってから入れる |

---

## 1. 変更一覧

| # | 内容 | 主な変更対象 | 規模 | 効果 (1800 秒換算) |
| --- | --- | --- | --- | --- |
| R1 | 最終結合をストリームコピーへ | `archive/clip_writer.py` `modules/ffmpeg_runner.py` | 小 | −1205 秒 |
| R2 | 出力キャンバスを素材等倍にできるようにする | `modules/output_profile.py` `timeline/compositor.py` `modules/subtitle_generator.py` `pipeline/pipeline_runner.py` | 中 | −1848 秒 |
| R3a | 中間ファイルのエンコード設定を分離する | `modules/ffmpeg_runner.py` `pipeline/pipeline_runner.py` | 小 | −215 秒 |
| R3b | 区間 1 件のクリップでベース映像構築を省く | `timeline/renderer.py` `modules/subtitle_generator.py` | 中 | −88 秒 |
| R4 | 解析パスに `-vn` を付ける | `modules/silence_cutter.py` `modules/volume_analyzer.py` | 小 | 採点 −190 / 書き出し −100 秒 |
| R5 | 全長解析を 1 パスにまとめ、採点結果を書き出しへ引き継ぐ | `archive/features.py` `archive/pipeline.py` `modules/volume_analyzer.py` | 中 | 採点 −247 / 書き出し −122 秒 |
| R6 | Whisper モデルをプロセス内で使い回す | `modules/subtitle_generator.py` | 小 | −38 秒 |
| R7 | ラウドネス測定を採用区間だけにする | `modules/loudness_normalizer.py` `archive/clip_writer.py` | 小 | −102 秒 |
| R8 | プレビュー音声の抽出を遅延させる | `modules/subtitle_generator.py` | 小 | −41 秒 |
| R9 | 進捗を実際に流し、中止できるようにする | `utils/progress.py` `pipeline/pipeline_context.py` `archive/clip_writer.py` `gui/archive_tab.py` | 中 | 0 秒 (体感) |
| R10 | 脱落クリップを完了時に明示する | `archive/clip_writer.py` | 小 | 0 秒 (信頼性) |
| R11 | 出力設定を設定画面へ出す | `settings/settings_window.py` | 中 | 0 秒 (利用者が選べる) |
| R13 | 書き出し対象の尺と想定時間を見せる | `gui/archive_tab.py` | 小 | 0 秒 (見積もり) |

---

## 2. 処理フロー (変更前 → 変更後)

### 2.1 採点フェーズ

```
【変更前】 259.5 秒
  probe_duration
  silencedetect (全長 / 映像も復号)                     127.2 秒
  volumedetect × 138 セル (1 セル 1 プロセス / 映像も復号) 132.0 秒
  → 採点・窓積分・TOP5 選択                               0.3 秒

【変更後】 13.2 秒  (R4 + R5)
  probe_duration
  1 パス (-vn):                                          13.2 秒 ★実測
    silencedetect=noise=-30dB:d=0.3
    → asetnsamples=n=48000 (1 秒) → astats(reset=1)
    → ametadata=print (RMS_level / Peak_level)
  → 1 秒値をセルへ按分 → 採点・窓積分・TOP5 選択
  → 戻り値へ audio_scan を同梱 (書き出しへ引き継ぐ / R5)
```

### 2.2 書き出しフェーズ

```
【変更前】 5262 秒
  analyze_vod
    silencedetect (全長・採点と同一引数)     99 秒  ← 二重実行
    volumedetect × 100 区間                  23 秒
    loudnorm 測定 (全長)                    144 秒
  クリップごと (6 本)
    cut_region (-c copy)                      2 秒
    normalize_file (loudnorm 適用)           73 秒
    detect_edit_points                       29 秒
    extract_audio_segments (プレビュー用)    41 秒  ← 認識には使わない
    WhisperModel 構築 × 6                    45 秒  ← 毎回読み直し
    文字起こし                              949 秒
  レンダリング (6 本)
    ベース映像構築   1080p60 再エンコード①  1300 秒
    字幕焼き込み     1080p60 再エンコード②  1340 秒
  最終結合           1080p60 再エンコード③  1217 秒  ← 恒等変換の再エンコード

【変更後 (フェーズ 2 まで)】 1604 秒
  analyze_vod
    採点の audio_scan を再利用                0 秒  (R5)
    loudnorm 測定 (採用区間 1800 秒のみ)     42 秒  (R7)
  クリップごと (6 本)
    cut_region                                2 秒
    normalize_file                           73 秒
    detect_edit_points                       29 秒
    (プレビュー音声は初回再生まで作らない)    0 秒  (R8)
    WhisperModel 構築 × 1                     8 秒  (R6)
    文字起こし                              949 秒
  レンダリング (6 本)
    ベース映像構築 720p30 / veryfast 中間     88 秒  (R2+R3a+R3b)
      └ 区間 1 件のクリップは丸ごと省略            (R3b)
    字幕焼き込み   720p30 medium             402 秒  (R2)
  最終結合         ストリームコピー           12 秒  (R1)
```

---

## 3. 各変更の詳細設計

### R1. 最終結合をストリームコピーへ

#### 目的

`clip_writer._combine` が `concat_processor.concat` (concat フィルタ + 全体再エンコード) を
呼んでいる。結合する部品は直前の字幕焼き込みが出した同一規格の mp4 なので、
`scale`/`pad`/`setsar`/`fps`/`format` はすべて恒等変換であり、再エンコードは丸ごと無駄である。

#### 変更対象

| ファイル | 対象 | 変更 |
| --- | --- | --- |
| `modules/ffmpeg_runner.py` | — | `probe_stream_params()` を**新設** |
| `modules/silence_cutter.py` | `concat_files()` | `total_duration` / `on_progress` を省略可引数として追加 |
| `archive/clip_writer.py` | `_combine()` | 規格が揃っていればコピー結合、違えば従来経路 |
| `archive/config.py` | `combine_config` 相当 | `archive.combine.reencode` を読む |

#### 設計

```python
# modules/ffmpeg_runner.py (新設)

# 結合可否の判定に使う主要ストリーム情報を ffprobe で取る (resolve3 R1)
# concat デマルチプレクサのストリームコピーは「同じコーデック・同じ規格」が前提で、
# 違う物を繋ぐと再生できない成果物になる。繋ぐ前にここで必ず確かめる。
# 戻り値: {"v_codec","width","height","pix_fmt","fps","a_codec","sample_rate","channels"}
#   取得できない項目は None。probe 自体の失敗は FFmpegError を送出する。
def probe_stream_params(input_path, ffmpeg_settings):
    ...  # ffprobe -show_streams -select_streams v:0 / a:0 を 1 回で取る
```

```python
# archive/clip_writer.py

# パーツを結合して final を作る (resolve3 R1)
#
# 部品 (字幕焼き込みの出力) は同一規格のため、既定ではストリームコピーで繋ぐ。
# OP/ED のように利用者が用意した任意規格が混ざる場合だけ再エンコードへ落とす。
# archive.combine.reencode=true なら常に従来どおり再エンコードする (切り戻し)。
def _combine(parts, final, ffmpeg_cfg, settings=None, on_progress=None):
    if len(parts) < 2:
        shutil.copy(parts[0], final)      # 従来どおり
        return final

    total_dur = _total_duration(parts, ffmpeg_cfg)
    if _can_stream_copy(parts, ffmpeg_cfg, settings):
        silence_cutter.concat_files(parts, final, ffmpeg_cfg, reencode=False,
                                    total_duration=total_dur, on_progress=on_progress)
        _logger.info("結合: ストリームコピー (%d 部品 / %.1fs)", len(parts), total_dur)
    else:
        concat_processor.concat(parts, final, ffmpeg_cfg,
                               total_duration=total_dur, on_progress=on_progress)
        _logger.info("結合: 規格が揃わないため再エンコード (%d 部品)", len(parts))
    return final


# 全部品の規格が一致し、ストリームコピーで繋げるか
# 1 件でも取得に失敗したら False (安全側 = 再エンコード)。
def _can_stream_copy(parts, ffmpeg_cfg, settings):
    if (settings or {}).get("archive", {}).get("combine", {}).get("reencode", False):
        return False
    try:
        first = ffmpeg_runner.probe_stream_params(parts[0], ffmpeg_cfg)
        for path in parts[1:]:
            if ffmpeg_runner.probe_stream_params(path, ffmpeg_cfg) != first:
                return False
    except AutoEditError:
        _logger.warning("部品の規格を確かめられないため再エンコードで結合します")
        return False
    return all(first.get(key) for key in ("v_codec", "a_codec", "width", "height"))
```

#### 判定に使う項目

`v_codec` / `width` / `height` / `pix_fmt` / `fps` / `a_codec` / `sample_rate` / `channels`。
**`fps` は `r_frame_rate` の文字列をそのまま比較する** (`60/1` と `60000/1001` を同一とみなさない)。

#### エラー処理

- `probe_stream_params` の失敗は握らず、`_can_stream_copy` が `False` を返して
  従来経路 (再エンコード) へ落とす。成果物は必ず出る。
- コピー結合が `FFmpegError` で失敗した場合は **再エンコードで 1 回だけ再試行する**。
  再試行も失敗したら従来どおり例外を送出する。

#### ログ

```
[INFO] 結合: ストリームコピー (7 部品 / 1803.2s)
[INFO] 結合: 規格が揃わないため再エンコード (8 部品) ← OP/ED 混在時
[WARNING] コピー結合に失敗したため再エンコードで繋ぎ直します: ...
```

#### 効果

1217 秒 → **約 12 秒** (resolve2.md §2.4 のベース連結 0.9 秒 / 2 部品からの換算)。

---

### R2. 出力キャンバスを素材等倍にできるようにする

#### 目的

横動画は素材の寸法を見ずに `ffmpeg.output_width/height` (1920x1080) と
`ffmpeg.output_fps` (60) を使うため、720p30 の VOD が毎パス拡大・倍フレーム化される
(画素スループット 4.5 倍)。

#### 設計の要点 — 「解決値を実行用の設定へ書き戻す」

`output_width` / `output_height` / `output_fps` を読む箇所は 15 以上あり
(`ffmpeg_runner.get_output_fps` / `compositor` / `renderer` / `burn_subtitle` /
`concat_processor` / `timeline_builder` / `silence_cutter` / `resolve_export` ほか)、
すべてを書き換えるのは破壊的である。

そこで **実行の入口で 1 回だけ解決し、実行用にコピーした設定へ書き戻す**。
`setting.json` の保存値は触らない (`build_clip_settings` が既に `deepcopy` している前例に従う)。
これにより**既存の読み手は 1 行も変えずに解決値を見る**。

#### 変更対象

| ファイル | 対象 | 変更 |
| --- | --- | --- |
| `modules/output_profile.py` | — | `resolve_canvas()` / `apply_canvas_to_settings()` を新設 |
| `modules/output_profile.py` | `_landscape_profile()` | `fps` を profile へ追加 (既存キーは不変) |
| `archive/clip_writer.py` | `_write_clips()` | 先頭で `apply_canvas_to_settings` を通す |
| `archive/project_resume.py` | `_run_from_archive_project()` | **保存済み Timeline の寸法/fps** を設定へ書き戻す |
| `timeline/compositor.py` | `CanvasProfile.production/proxy` | fps を `timeline.fps` 優先で取る |
| `gui/archive_result_window.py` | `_open_timeline_window()` | 編集画面へ解決済み設定を渡す |

#### 設計

```python
# modules/output_profile.py (新設)

# 素材の表示寸法・fps から出力キャンバスを決める (resolve3 R2)
#
# ffmpeg.output_scale_mode:
#   "source" : 素材等倍。拡大もフレーム複製もしない (推奨)
#   "fixed"  : 従来どおり output_width/height/output_fps をそのまま使う
# "source" でも output_width/height/output_fps は**上限**として効く
# (4K の素材を 4K で書き出して時間が爆発するのを防ぐ)。
# 戻り値: {"width","height","fps","mode","source_width","source_height","source_fps"}
def resolve_canvas(input_path, settings):
    ...


# 解決したキャンバスを「実行用の設定コピー」へ書き戻す (resolve3 R2)
#
# ここで書き戻すことで、ffmpeg.output_* を読む既存の全工程が解決値を見る。
# setting.json の保存値は変えない (deepcopy するため)。
# canvas を直接渡すと解決を飛ばせる (保存済みプロジェクトの再編集用)。
def apply_canvas_to_settings(settings, input_path=None, canvas=None):
    ...
```

#### fps の丸め (必須)

実測ログに `1280x720 28.8fps` が出ている。非整数の fps をそのまま使うと
CFR 化 (`-fps_mode cfr -r <fps>`) の前提が崩れるため、**標準値へ丸める**。

```
新設定 ffmpeg.output_fps_standards (既定 [24, 25, 30, 50, 60])
  素材 fps に最も近い値を選ぶ。候補が空なら丸めず素材値をそのまま使う。
  例: 28.8 → 30 / 29.61 → 30 / 59.94 → 60
```

ハードコードを避けるため候補は `setting.json` に持つ (`docs/CLAUDE.md` の制約)。

#### 寸法の丸め

`yuv420p` は奇数の幅・高さを扱えないため**偶数へ切り下げる**。
`timeline/compositor.py` の `_even()` と同じ規則を使う (実装は `output_profile` 側に持ち、
`compositor` へ依存させない)。

#### 高解像度素材 (1440p / 2160p) の扱い — 上限の定義

`output_scale_mode="source"` でも `ffmpeg.output_width/height/output_fps` は**上限**として効く。
上限を超える素材は**アスペクト比を保ったまま上限枠へ収めて縮小**する。
**拡大は決して行わない** (これが R2 の目的である)。

```
scale  = min(1.0, output_width / source_width, output_height / source_height)
width  = even(source_width  * scale)
height = even(source_height * scale)
fps    = min(round_to_standard(source_fps), output_fps)
```

| 素材 (配信解像度) | 現状 (`fixed`) | 変更後 (`source` / 上限 1920x1080@60) | 書き出し時間 |
| --- | --- | --- | --- |
| 1280x720@30 | 1920x1080@60 へ**拡大** | **1280x720@30** (等倍) | **約 0.30 倍** |
| 1920x1080@60 | 1920x1080@60 | 1920x1080@60 (等倍) | 変わらない |
| **2560x1440@60 (2K)** | 1920x1080@60 へ縮小 | **1920x1080@60** (上限で縮小) | **現状と同じ** |
| 3840x2160@60 (4K) | 1920x1080@60 へ縮小 | 1920x1080@60 (上限で縮小) | 現状と同じ |

**2K 配信のアーカイブは、出力が現状と 1 ピクセルも変わらない。**
上限 1920x1080 に当たり、今と同じ 1080p へ縮小されるためである。
したがって **2K で書き出しが遅くなることはない** (R1/R3a/R4〜R8 の短縮だけが効く)。

2K の画素をそのまま残したい場合は `output_width/height` を 2560x1440 へ上げれば残るが、
画素スループットが 1.78 倍になり**書き出し時間も約 1.8 倍**になる。
YouTube へ上げる前提では 1080p で足りるため、**既定では上げない**。

#### テロップ・コメントアイコンの相対サイズ (R2 の必須付帯対応)

**本書の初版で見落としていた点である。これを直さないと 1080p 未満の素材で
テロップだけが大きくなる。**

`subtitle.font_size` / `margin_l/r/v` / `outline_width` / `comment_icon_size_px` /
`comment_icon_gap_px` / イントロカードの `title_font_size` は**絶対 px** で、
1080p を前提に調整された値である。
一方 `build_subtitle_file` は ASS の `PlayResX/Y` を**出力キャンバス寸法に一致させている**
(`modules/subtitle_generator.py:1830` / `archive/clip_writer.py:234,270`)。

`fixed` ではキャンバスが常に 1920x1080 なので問題は出ない。
`source` で 1280x720 になると `PlayResY` が 720 になり、
font_size 48 が占める高さが 4.4% → 6.7% (**1.5 倍**) へ変わる。

起きること / 起きないことを分けて書く。

| 項目 | 変わるか | 理由 |
| --- | --- | --- |
| テロップの**表示タイミング** | **変わらない** | 字幕は秒で持っており寸法と無関係 |
| テロップの**位置** | **変わらない** | 個別位置は正規化座標 (-1..1) を `video_width/height` で px へ戻すため寸法に追随する (`_inline_position_override`) |
| 文字と余白の**相対サイズ** | **変わる (要対応)** | 絶対 px のため |

つまり**「映像とテロップがズレる」ことは無い**。直す必要があるのは相対サイズだけである。

##### 対処 — 既存のプロキシと同じ手口を使う

編集画面のプロキシは**同じ問題を既に解いている**。
`timeline/compositor.py:439` のコメントが方針を明文化している。

> 【重要】**CanvasProfile を受け取らない。** ASS の PlayRes は本番寸法のままにし、
> libass が PlayRes → 実フレーム寸法へ自動で合わせるのに任せる。
> こうするとプロキシでもフォントサイズ・余白・行送りが完全に相似になる。

R2 でも同じにする。**PlayRes は「基準キャンバス」に固定し、実フレームだけ等倍にする。**
libass が PlayRes → フレーム寸法へ相似変換するため、見た目は 1080p と完全に相似になる。

```
基準キャンバス (reference) = 高さは ffmpeg.output_height、アスペクト比は実キャンバスと同じ
  scale       = canvas_height / output_height      (上限に当たる 1080p 以上では 1.0)
  reference_h = output_height
  reference_w = even(canvas_width / scale)

例: 素材 1280x720   → canvas 1280x720  / reference 1920x1080 / scale 0.667
例: 素材 2560x1440  → canvas 1920x1080 / reference 1920x1080 / scale 1.0 (現状と同一)
例: 素材 1440x1080  → canvas 1440x1080 / reference 1440x1080 / scale 1.0
```

- **新しい設定キーは作らない。** 基準高さは既存の `ffmpeg.output_height` をそのまま使う
  (= 利用者が font_size を調整したときに見ていた高さ)。縦動画は `vertical.output_height`。
- `resolve_output_profile` の戻り値へ `reference_width` / `reference_height` / `scale` を
  追加する (既存キーは不変)。
- ASS の `PlayResX/Y` へ `reference_width/height` を渡す。
  対象は `subtitle_generator.run()` の `build_subtitle_file(video_width=, video_height=)` と
  `clip_writer._write_intro_ass()` の `w, h`。
- コメントアイコンは配置を基準キャンバスで計算し `scale` を掛ける。
  `comment_decor.build_icon_chains` は既に `scale=` を持つため
  `compositor.icon_chains` と同じ呼び方にするだけで済む。
- ウォーターマークは `scale_ratio` / `margin_ratio` でキャンバス幅からの比率で求めている
  (`watermark_overlay._geometry`) ため**対応不要**。
  ぼかしもマスクをキャンバスへ `scale` するため不要。
- `_normalize_chain(w, h)` や `-s` など**実フレーム寸法を使う箇所は実キャンバスのまま**にする。
  ここへ基準寸法を混ぜると縦横が合わなくなる。

##### 受け入れ確認 (必須)

720p30 の素材で `fixed` と `source` の両方を書き出し、下記を確認する。

1. テロップの文字高・行送り・下余白が**フレーム高に対する比率で一致**する
2. コメントアイコンと本文の間隔が比率で一致する
3. イントロカードのテーマ文字が比率で一致する
4. 任意のテロップ 3 本の表示開始・終了フレームが一致する (時間軸のズレが無いこと)

1〜3 が崩れる場合は `output_scale_mode="fixed"` へ戻せる (§11)。

#### 縦動画は対象外

`vertical.enabled=true` で縦と判定した場合は従来どおり `vertical.output_width/height`
(1080x1920) 固定にする。切り抜き位置の計算 (`vertical.crop`) が寸法に依存するため。
fps だけは `output_scale_mode="source"` に従う。

#### 保存済みプロジェクトの再編集

プロジェクトファイルは `fps` / `width` / `height` を保持している
(`timeline/project_io.py`)。再編集では**素材から再解決してはならない**
(保存時と違うキャンバスになり、字幕位置・クロップがずれる)。
`_run_from_archive_project` では読み込んだ Timeline の値を
`apply_canvas_to_settings(settings, canvas=...)` で書き戻す。

#### `CanvasProfile` の fps

```python
# timeline/compositor.py
@staticmethod
def production(timeline, ffmpeg_cfg):
    # fps は Timeline (= プロジェクトに保存された軸) を唯一の正とする (resolve3 R2)。
    # 設定だけを見ると、保存済みプロジェクトを設定変更後に開いたとき軸がずれる。
    fps = getattr(timeline, "fps", 0) or ffmpeg_runner.get_output_fps(ffmpeg_cfg)
    return CanvasProfile(timeline.width, timeline.height, fps)
```

#### エラー処理

- `probe_dimensions` が失敗したら `mode="fixed"` と同じ値へ落とす (既存の安全側と同じ)。
- fps が取れない / 0 以下なら `output_fps` の設定値を使う。
- 解決後の寸法が 2 未満になる異常値は `fixed` へ落とす。

#### ログ

```
[INFO] 出力キャンバス: 素材等倍 1280x720@30fps (素材 1280x720@28.8fps / 上限 1920x1080@60fps)
[INFO] 出力キャンバス: 固定 1920x1080@60fps (output_scale_mode=fixed)
[WARNING] 素材の寸法を取れないため固定キャンバス 1920x1080@60fps を使います
```

#### 効果

ベース映像構築 1300 → 390 秒 / 字幕焼き込み 1340 → 402 秒 (率 ×0.300 / 60 秒ベンチ 14.0÷46.6)。
最終結合は R1 で消えるため合計 **−1848 秒**。

---

### R3a. 中間ファイルのエンコード設定を分離する

#### 目的

ベース映像 (`tl_part_*` → `timeline_base.mov`) は**直後に字幕焼き込みで再エンコードされる
中間物**である。そこへ最終出力と同じ `preset=medium` を使うのは労力の無駄である。

#### 変更対象

`modules/ffmpeg_runner.py` の `build_intermediate_encode_options()`。

```python
# 中間ファイル (連結前の部品) 用のエンコード設定を返す
# resolve3 R3a: 中間物は直後に焼き込みで再エンコードされるため、
# 最終出力と同じ preset を使う必要がない。
# intermediate_preset / intermediate_crf が空のときは従来と完全に同一の内容を返す。
def build_intermediate_encode_options(ffmpeg_settings, audio_codec=None):
    preset = str(ffmpeg_settings.get("intermediate_preset", "") or "").strip() \
             or ffmpeg_settings.get("preset", "medium")
    crf = str(ffmpeg_settings.get("intermediate_crf", "") or "").strip() \
          or str(ffmpeg_settings.get("crf", 20))
    ...
```

#### 設定

```
ffmpeg.intermediate_preset (既定 "")   空 = preset を使う (従来)。推奨値 "veryfast"
ffmpeg.intermediate_crf    (既定 "")   空 = crf を使う (従来)。推奨値 "18"
```

**crf は最終より下げる (= 高品質にする)**。中間で落ちた情報は最終で戻らないため、
速度を取るぶん量子化は緩める。`veryfast` + `crf 18` は実測で `medium` + `crf 20` の
0.45 倍の時間 (60 秒ベンチ 6.3 秒 ÷ 14.0 秒 / 720p30)。

#### 効果

ベース映像構築 390 → **176 秒** (−215 秒)。

#### リスク

中間で 1 世代ぶん劣化が増える。`crf 18` で相殺する想定だが、
**既定は空 (従来値) とし、推奨値は設定画面のプリセット (R11) 経由で入れる**。

---

### R3b. 区間 1 件のクリップでベース映像構築を省く

#### 目的

残す区間が 1 件のクリップでは、ベース映像構築は
「素材を `-ss`/`-t` で切ってキャンバスへ正規化してエンコードする」だけで、
続く字幕焼き込みが同じ映像をもう一度エンコードする。1 パス省ける。

#### 適用条件 (すべて満たすときだけ)

| # | 条件 | 理由 |
| --- | --- | --- |
| 1 | V1 のクリップが 1 本 | 2 本以上は PCM 中間 + demuxer 連結が A/V 同期対策 (20260812 resolve3) として要る |
| 2 | ギャップが無い | 黒+無音の挿入が要らない |
| 3 | オーバーレイのメディアクリップが無い (字幕のみ) | `_composite` 経路は多入力のため対象外 |
| 4 | 素材が静止画でない / 音声を持つ / ミュートでない | 無音生成 (`anullsrc`) の 2 入力構成を避ける |
| 5 | 音声ゲインが 0 | `-af` の組み立てを増やさない |
| 6 | クロップ・V1 拡大が無い | `crop_chain` / `base_zoom` は正規化を兼ねるため別扱い |
| 7 | ぼかしマスクが無い | ぼかしは `pre_chains` を既に使うため鎖が競合する |

アーカイブ切り抜きの既定経路 (正規化済み素材・字幕のみ・ゲイン 0) は 1 と 2 以外を満たす。
resolve2.md の実測では 2 本中 1 本 (尺で 50.4%) が条件を満たした。

#### 変更対象

| ファイル | 対象 | 変更 |
| --- | --- | --- |
| `timeline/renderer.py` | `render()` | 直結できるかを判定し、ベースを作らず素材を渡す |
| `timeline/renderer.py` | — | `_direct_source()` を新設 (条件判定 + 入力引数とフィルタの組み立て) |
| `timeline/renderer.py` | `_burn_subtitles_only()` | 入力引数と前段フィルタを受け取れるようにする |
| `modules/subtitle_generator.py` | `burn_subtitle()` | `input_args=None` を省略可引数として追加 |

```python
# modules/subtitle_generator.py
# input_args: 入力の前へ置く引数 (["-ss","12.300","-t","180.000"] など / resolve3 R3b)。
#   None なら従来と完全に同一のコマンドになる。
def burn_subtitle(input_path, subtitle_path, output_path, ffmpeg_settings,
                  total_duration=0.0, on_progress=None, target_size=None,
                  fonts_dir=None, extra_chains=None, filter_script_path=None,
                  filter_script_chars=0, pre_chains=None, pre_out_label="[vpre]",
                  input_args=None):
```

正規化チェーンは既存の `pre_chains` 機構へ乗せる
(`compositor.base_filters` の結果を `[0:v]<filters>[vpre]` の 1 チェーンにする)。
**ぼかしが `pre_chains` を使うため、条件 7 で排他にしている。**

#### 設定

```
timeline.render.direct_burn (既定 true)  false で従来どおり必ずベース映像を作る (切り戻し)
```

#### 検証の要点 (テスト必須)

- 直結した場合と従来経路の**出力尺が一致する** (`_verify_base_duration` と同じ許容 = 1 フレーム)
- 字幕の時刻がずれない (`tests/test_renderer_timing.py` に直結モードのケースを追加)
- 条件を 1 つでも外した Timeline では従来経路を通る (コマンドが 1 文字も変わらない)

#### 効果

R3a 適用後のベース映像構築 176 秒 → **88 秒** (実測構成の 50.4% が対象)。
カット閾値が resolve2.md §2.5 の状態 (ほぼ切れない) なら全クリップが対象になり 0 秒に近づく。

---

### R4. 解析パスに `-vn` を付ける

#### 変更対象

```python
# modules/silence_cutter.py : detect_silence()
cmd = [
    ffmpeg, "-hide_banner",
    "-i", input_path,
    "-vn",                      # ← 追加 (音声解析に映像の復号は不要 / resolve3 R4)
    "-af", f"silencedetect=noise={noise_threshold_db}dB:d={min_duration_sec}",
    "-f", "null", "-",
]

# modules/volume_analyzer.py : measure_region_volume()
cmd = [
    ffmpeg, "-hide_banner",
    "-ss", f"{start:.3f}",
    "-t", f"{duration:.3f}",
    "-i", input_path,
    "-vn",                      # ← 追加
    "-af", "volumedetect",
    "-f", "null", "-",
]
```

#### 妥当性

出力は `-f null -` で捨てており、使っているのは stderr のログだけである。
映像ストリームは結果に一切寄与しない。`loudness_normalizer.measure_loudness` が
既に同じ理由で `-vn` を付けている。

#### 効果 (実測)

| 処理 | `-vn` なし | `-vn` あり | 倍率 |
| --- | --- | --- | --- |
| `silencedetect` 600 秒 | 7.7 秒 | 0.7 秒 | 11.0 |
| `volumedetect` 45 秒 (5 本平均) | 0.76 秒 | 0.21 秒 | 3.6 |

採点 −約 190 秒 / 書き出し −約 100 秒。**R5 を入れると呼び出し自体が減るため、
R5 より先に入れても後に入れても取りこぼしが無いように両方へ付ける。**

---

### R5. 全長解析を 1 パスにまとめ、採点結果を書き出しへ引き継ぐ

#### 目的

2 つの無駄を同時に消す。

1. 採点の特徴抽出が **セル 1 個につき 1 プロセス** (138 プロセス / 132 秒)
   — resolve1.md §2.3 **F6** が未実装
2. 全長の `silencedetect` が **採点と書き出しで同一引数で 2 回** (127 + 99 秒)
   — resolve1.md §2.3 **F7** が「追加コストほぼゼロ」と前提した条件が崩れている

#### 1 パスのコマンド (実測済み)

```
ffmpeg -hide_banner -nostats -i <VOD> -vn -af
  "silencedetect=noise=-30dB:d=0.3,
   asetnsamples=n=48000:p=0,
   astats=metadata=1:reset=1,
   ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-,
   ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-"
  -f null -
```

**実測: 全長 6170.7 秒を 13.2 秒で処理し、1 秒ごとの RMS 6171 件 / Peak 6167 件 /
無音区間 1644 件を同時に得た。**

設計上の注意点 (実測で確かめた仕様):

- `astats` の `reset=N` は**フレーム数**であって秒ではない。
  音声フレームは 1024 サンプル (21.3 ms) なので、`reset=45` は 45 秒ではなく約 1 秒。
  **`asetnsamples` で 1 フレーム = 1 秒 (48000 サンプル) に揃えてから `reset=1`** とする。
- `asetnsamples` は `silencedetect` の**後ろ**へ置く。
  前に置くと `silencedetect` が 1 秒粒度でしか判定できなくなる。
- `ametadata=print:file=-` は**標準出力**へ出る (`silencedetect` は標準エラー)。
  両方を読む必要があるため `subprocess.run(capture_output=True)` のままでよい。
- 無音部では `RMS_level=-inf` / `Peak_level=-inf` が出る。**必ず弾く**。
- 1 秒値の標準出力は全長 1h43m で約 1.0 MB。パイプで受けて問題ない大きさである。

#### 変更対象

| ファイル | 対象 | 変更 |
| --- | --- | --- |
| `archive/features.py` | — | `scan_audio()` を新設 (1 パス解析) |
| `archive/features.py` | `extract_cells()` | `feature_mode` で 1 パス / 従来を切り替え |
| `archive/pipeline.py` | `analyze()` | 戻り値へ `audio_scan` を追加 |
| `modules/volume_analyzer.py` | `analyze_min_speech_db()` | `scan=None` を追加し、あれば 1 秒値から算出 |
| `archive/clip_writer.py` | `write_clips()` / `analyze_vod()` | `vod_scan=None` を受け取り下へ渡す |
| `gui/archive_tab.py` | `_on_analyze_done()` | `audio_scan` を `ArchiveClipWorker` へ渡す |

#### データ構造

```python
# archive/features.py
# 全長 1 パスの解析結果 (resolve3 R5)。採点と書き出しの両方がこれを使う。
#   duration        : 総尺 (秒)
#   silence_ranges  : [(start, end)]  silencedetect の結果
#   per_sec         : [{"rms": float|None, "peak": float|None}]  1 秒ごと (index = 秒)
#   params          : 測定条件 {"noise_db","min_silence_sec","step_sec"}
#                     引き継いだ側がこれを照合し、設定が変わっていたら測り直す
```

#### 1 秒値からの導出

| 用途 | 従来 | 1 パス後 |
| --- | --- | --- |
| セルの `mean_db` | `volumedetect` の mean_volume | セル内 1 秒 RMS のエネルギー平均 |
| セルの `max_db` | `volumedetect` の max_volume | セル内 1 秒 Peak の最大値 |
| セルの `silence_ratio` | 無音区間の按分 (現状どおり) | 同じ |
| 発話区間の代表 dB (`analyze_min_speech_db`) | 区間ごとに `volumedetect` | 区間内 1 秒 RMS のエネルギー平均 → 分位点 |

**エネルギー平均**にすること (dB の算術平均ではなく `10*log10(mean(10**(db/10)))`)。
`volumedetect` の mean_volume は RMS なので、合わせないと値がずれる。

#### 引き継ぎの安全弁

```python
# 引き継いだ scan が今の設定と同じ条件で測られたものか照合する。
# 採点のあと設定画面で閾値を変えられた場合に、古い値で切ってしまうのを防ぐ。
def _scan_matches(scan, settings):
    want = {"noise_db": va_cfg.get("base_noise_db", -30),
            "min_silence_sec": float(va_cfg.get("base_min_silence_sec", 0.3))}
    return (scan or {}).get("params", {}).get("noise_db") == want["noise_db"] and ...
```

一致しなければ従来どおり測り直す (= 常に正しい値で動く)。

#### 設定

```
archive.scoring.feature_mode (既定 "single_pass")
  "single_pass" : 1 パス (astats) で抽出する
  "per_cell"    : セルごとに volumedetect を呼ぶ (従来 / 切り戻し)
```

resolve1.md F6 は既定 `"per_cell"` を提案していたが、**実測で 20 倍の差がある**ため
本書は `"single_pass"` を新既定として提案する (§13-2 の判断事項)。

#### 効果

採点 259.5 → **13.2 秒** / 書き出しの `analyze_vod` 音量解析 122 → **0 秒**。

---

### R6. Whisper モデルをプロセス内で使い回す

#### 変更対象

`modules/subtitle_generator.py` の `WhisperTextSource._recognize()`。

```python
# モデルは (モデル名, device, compute_type) ごとに 1 つだけ作って使い回す (resolve3 R6)。
# large-v3 は読み込みに 7.5 秒かかり、クリップ数ぶん繰り返すと丸ごと無駄になる。
# subtitle.whisper_model_cache=false で従来どおり毎回作る。
_model_cache = {}


def _get_model(whisper_model_cls, key, use_cache):
    if not use_cache:
        return whisper_model_cls(key[0], device=key[1], compute_type=key[2])
    model = _model_cache.get(key)
    if model is None:
        model = whisper_model_cls(key[0], device=key[1], compute_type=key[2])
        _model_cache.clear()        # 別設定のモデルは抱えない (メモリ 1 本ぶんに保つ)
        _model_cache[key] = model
    return model


# キャッシュを解放する (書き出し完了時・設定変更時に呼ぶ)
def release_models():
    _model_cache.clear()
```

#### 解放のタイミング

| 呼ぶ場所 | 理由 |
| --- | --- |
| `clip_writer._write_clips` の `finally` | 書き出しが終わればモデルは不要 (int8 で約 1.5 GB) |
| `pipeline_runner` の終了時 | 本流も同じ |
| 設定保存時 (`settings_window._on_save`) | モデル名・精度を変えたら作り直す |

#### 設定

```
subtitle.whisper_model_cache (既定 true)  false で従来どおり毎回読み込む
```

#### 効果

45 秒 → **7.5 秒** (−38 秒)。

---

### R7. ラウドネス測定を採用区間だけにする

#### 目的

`loudness_scope="vod"` は全長 6170 秒を流して 1 つのゲインを決めている。
しかし**出力に入るのは採用区間の 1800 秒だけ**である。
採用区間だけを測っても「全クリップへ同じゲインを掛ける」性質
(resolve1.md §4.2 E1 の狙い = クリップ間の起伏を保つ) は変わらず、
基準点が「VOD 全体」から「実際に出す部分」へ移るだけで、むしろ F10 (最終出力の検証) と揃う。

#### 実測

| 測定対象 | 所要 | 音声尺 |
| --- | --- | --- |
| 全長 (現状) | 144.5 秒 (パイプライン内) / 218.4 秒 / 377.9 秒 (単独 2 回) | 6170.7 秒 |
| 採用区間のみ (6 入力 concat) | 105.6 秒 (単独。同条件の全長は 218.4 秒) | 1800 秒 |

この工程は単一スレッドで**実行ごとのばらつきが大きい** (実時間比 16〜43 倍)。
尺に比例するため、**尺を 29.2% にすれば所要も 29.2% になる**と見るのが妥当である。
resolve2.md の基準値 144.5 秒に対して **約 42 秒**。

得られた JSON は `linear=true` の適用に必要な 5 項目すべてを含む
(実測: `input_i` `input_tp` `input_lra` `input_thresh` `target_offset`)。
**loudnorm をそのまま使うため正規化の計算は 1 行も変わらない。**

#### コマンド (実測済み)

```
ffmpeg -hide_banner -nostats
  -ss 1395.000 -t 180.000 -i <VOD>
  -ss 1755.000 -t 180.000 -i <VOD>
  ... (採用区間ぶん)
  -filter_complex "[0:a:0][1:a:0]...concat=n=6:v=0:a=1,
                   loudnorm=I=-14.0:TP=-1.5:LRA=11.0:print_format=json[ao]"
  -map "[ao]" -f null -
```

#### 変更対象

| ファイル | 対象 | 変更 |
| --- | --- | --- |
| `modules/loudness_normalizer.py` | `measure_loudness()` | `ranges=None` を追加。あれば多入力 concat で測る |
| `archive/clip_writer.py` | `analyze_vod()` | 採用区間 (`used`) を渡す |
| `archive/clip_writer.py` | `_prepare_clips()` | `analyze_vod` へ `used` を渡す |

```python
# 入力のラウドネスを測る (1 パス目)
# ranges: [(start, end)] を渡すと**その区間だけ**を繋いで測る (resolve3 R7)。
#   None なら従来どおり全長を測る。
#   区間数が多いと入力数が増えるため、上限 (loudness.measure_max_ranges) を超えたら
#   長い順に間引く。間引いた旨はログへ残す。
def measure_loudness(input_path, cfg, ffmpeg_cfg, total_duration=0.0,
                     on_progress=None, ranges=None):
```

#### 設定

```
loudness.measure_scope (既定 "selected")
  "selected" : 採用区間だけを測る
  "vod"      : 全長を測る (従来 / 切り戻し)
loudness.measure_max_ranges (既定 20)
  concat の入力数上限。超えたら長い順に間引く
```

`archive.clip_pipeline.loudness_scope` (既に存在) は「どの範囲で**揃えるか**」、
`loudness.measure_scope` は「どの範囲で**測るか**」で役割が違う。混同しないよう
設定画面の文言と設計書で区別する (§13-3 の判断事項)。

#### エラー処理

- 多入力 concat が失敗したら**全長測定へ 1 回だけ落ちる**。それも失敗したら
  従来どおり `None` を返し、クリップ単位の測定へ落ちる (既存の安全弁)。
- 区間が 0 件のときは全長測定を使う。

#### 効果

144 秒 → **約 42 秒** (−102 秒)。

---

### R8. プレビュー音声の抽出を遅延させる

#### 目的

`subtitle_generator.recognize_for_timeline` は**認識には使わない**
「プレビュー初回再生用の連結音声」を毎回作る (区間ごとに 1 プロセス / 換算 41 秒)。
再生されなければ捨てられる。

#### 変更対象

```python
# modules/subtitle_generator.py : recognize_for_timeline()

# プレビューの初回再生ソースを先に作るか (resolve3 R8)。
# 認識には使わないため、再生されなければ捨てられる。
# subtitle.asr_audio_prefetch=false のときは作らず、
# 代わりに「どう作るか」を context へ預けて初回再生時に作らせる。
if eff_cfg.get("asr_audio_prefetch", True):
    ... 従来どおり extract_audio_segments を呼ぶ ...
else:
    setter = getattr(context, "set_asr_audio_builder", None)
    if callable(setter):
        setter(lambda dest: silence_cutter.extract_audio_segments(
            source_path, keep_segments, dest, ffmpeg_cfg))
```

#### 変更対象 (受け側)

`pipeline/pipeline_context.py` に `set_asr_audio_builder()` / `build_asr_audio()` を追加し、
`asr_audio_path()` が未生成なら builder を 1 回だけ実行する。
プレビュー側 (`gui/timeline/preview_panel.py`) は既に
「無ければ都度生成へ切り替える」経路を持っているため、
**builder を用意できない場合も従来どおり動く**。

#### 設定

```
subtitle.asr_audio_prefetch (既定 true)  false で初回再生まで作らない
```

既定を `true` (従来) にする理由: プレビューを必ず触る利用者には先に作ったほうが速い。
R11 の「速度優先」プリセットで `false` を選ばせる。

#### 効果

41 秒 → **0 秒** (プレビューを触らない場合)。

---

### R9. 進捗を実際に流し、中止できるようにする

**これが「中断」への直接の対策である。** 時間は縮まないが、
20 分動かない画面を出さなくなる。

#### 9-1. レンダリング中の進捗を全体へ流す

```python
# pipeline/pipeline_context.py (追加)

# 工程分割を持たない呼び出し用に「単一工程」として扱う (resolve3 R9)
# progress_subcallback が返す比率が、そのまま 0..1 の工程内比率になる。
def use_single_step(self):
    self.total_steps = 1
    self.current_step = 1
```

```python
# archive/clip_writer.py : _render_clips()

# クリップ内の比率を全体の帯 [0.6 + (pos-1)*w, 0.6 + pos*w] へ写す (resolve3 R9)
def _clip_progress(ratio, label, _pos=pos, _w=0.3 / total):
    if progress_cb:
        progress_cb(0.6 + (_pos - 1 + max(0.0, min(1.0, ratio))) * _w,
                    f"クリップ {_pos}/{len(groups)} を書き出し中…（{label}）")

context = PipelineContext(input_path=prepared["normalized_path"],
                          settings=clip_settings, working_dir=clip_dir,
                          progress_callback=_clip_progress)
context.use_single_step()
```

`renderer` 側の `context.progress_subcallback("ベース映像構築")` /
`("字幕焼き込み")` がそのまま効くようになり、ラベルも工程名まで出る。

#### 9-2. 最終結合の進捗

```python
# archive/clip_writer.py : finish_clips()
_combine(parts, final, ffmpeg_cfg, settings=settings,
         on_progress=(lambda ratio, _kv: progress_cb(0.92 + ratio * 0.08, tr("結合中…")))
                     if progress_cb else None)
```

#### 9-3. VOD 全体の解析を 0% 固定にしない

```python
# archive/clip_writer.py : _prepare_clips()
#   analyze_vod へ (ratio, label) を渡せるようにし、0.00〜0.05 を割り当てる
vod_analysis = analyze_vod(
    input_path, settings, used=used, scan=vod_scan,
    progress_cb=(lambda ratio, label: progress_cb(ratio * 0.05, label))
                if progress_cb else None)
```

`measure_loudness` は `on_progress` を受け取れるので、そのまま繋ぐ。
R5 / R7 で合わせて 266 → 42 秒になるため、固まる時間そのものも短くなる。

#### 9-4. 文字起こしの進捗

faster-whisper の `transcribe()` が返すジェネレータを消費する `for` ループで、
**セグメントの終了時刻が進捗になる** (`seg.end / info.duration`)。
現状 `_info` は捨てられている。

```python
# modules/subtitle_generator.py : WhisperTextSource

# 認識の進捗を通知する (resolve3 R9)。
# faster-whisper は進捗を返さないが、セグメントを取り出すたびに
# 「どこまで認識したか」(seg.end) が分かるため、これを比率にする。
def set_progress(self, callback):
    self._progress = callback

# _recognize() のループ内
total = float(getattr(_info, "duration", 0.0) or 0.0)
if self._progress and total > 0:
    self._progress(min(1.0, float(seg.end) / total))
```

`_prepare_edit_points` 側で
`クリップ n/N を文字起こし中…（45%）` のラベルへ写す。
**これで 93 秒 (換算 160 秒) の無反応区間が消える。**

#### 9-5. 中止

```python
# utils/progress.py : run_ffmpeg_progress()
# cancel_cb: 進捗 1 行ごとに中止を問い合わせる (resolve3 R9)。
#   True が返ったらプロセスを終わらせて PipelineCancelled を送出する。
#   archive/twitch_source._run と同じ規約に揃える。
def run_ffmpeg_progress(command, total_duration=0.0, on_progress=None,
                        progress_timeout_sec=0, cancel_cb=None):
```

伝播経路:

```
GUI 中止ボタン
  → ArchiveClipWorker.cancel()         (フラグを立てるだけ)
  → write_clips(..., cancel_cb=...)
  → 工程の境目で確認 (クリップの切り替わり・結合の前) + ffmpeg の進捗 1 行ごと
  → PipelineCancelled
  → write_clips が points をキャンセル・telemetry へ cancelled を記録
  → GUI は「中止しました」を出す (エラーダイアログは出さない)
```

`ffmpeg_runner.execute` / `execute_capture` に `cancel_cb` を通し、
既存の呼び出し (省略時 `None`) は 1 文字も挙動が変わらない。

#### 9-6. 広い `except Exception` が中止と容量不足を飲み込まないようにする

現状、下記は `PipelineCancelled` と `DiskSpaceError` まで飲み込む。

| 箇所 | 現在の意図 |
| --- | --- |
| `clip_writer.py:395` `_decorate_clip` (タグ) | 演出失敗で本編を失わない |
| `clip_writer.py:404` `_decorate_clip` (イントロ) | 同上 |
| `clip_writer.py:435` `analyze_vod` | 失敗してもクリップ単位へ落ちる |
| `clip_writer.py:499` `_apply_volume_analysis` | 解析失敗でクリップを失わない |
| `clip_writer.py:572` `_transcribe` | 文字起こし失敗でクリップを失わない |
| `clip_writer.py:876` `_render_clips` | 1 クリップの失敗で全体を止めない |
| `features.py:42` 無音検出 | 無音率 0 で継続 |
| `subtitle_generator.py:2511` `recognize_for_timeline` | 認識失敗で処理全体を止めない |

すべてに共通の先頭 `except` を足す。

```python
except (PipelineCancelled, DiskSpaceError):
    # 中止と容量不足は「継続してはいけない」失敗なので必ず貫通させる
    # (resolve3 R9-6。export_intro_source が既に同じ扱いをしている)
    raise
except Exception:  # noqa: BLE001 (従来どおりの継続)
    ...
```

`clip_writer.export_intro_source` が既に `DiskSpaceError` を re-raise しており、
前例と規約が一致する。

#### 進捗の配分 (変更後)

| 区間 | 比率 | 内容 |
| --- | --- | --- |
| 0.00 – 0.05 | 5% | VOD 全体の解析 (音量 / ラウドネス) ★実比率で動く |
| 0.05 – 0.55 | 50% | クリップごとの準備 (正規化 / 編集点 / 文字起こし) ★認識の実比率で動く |
| 0.55 – 0.60 | 5% | Timeline 構築 + 結果画面 |
| 0.60 – 0.90 | 30% | クリップごとのレンダリング ★ベース/焼き込みの実比率で動く |
| 0.90 – 0.92 | 2% | 個別出力 (設定時のみ) |
| 0.92 – 1.00 | 8% | 最終結合 ★実比率で動く |

**「動かない区間」がゼロになる。**

---

### R10. 脱落クリップを完了時に明示する

#### 変更対象

```python
# archive/clip_writer.py : _render_clips()
skipped = []
...
except (PipelineCancelled, DiskSpaceError):
    raise
except Exception:  # noqa: BLE001
    _logger.exception("clip%s のレンダリングに失敗 → このクリップを飛ばします", clip_index)
    skipped.append(clip_index)
    continue
...
return burned, skipped
```

```python
# 書き出し完了時の進捗ラベル (resolve3 R10 でクリップ脱落を追加)
#
# テーマを入れたのにイントロが付かなかったクリップ (従来) と、
# レンダリングに失敗して落ちたクリップ (追加) を並べる。
# 黙って短い成果物を出すと、見るまで気づけない。
def _finish_label(burned, skipped=None):
    notes = []
    missing = [...]                    # 従来どおり
    if missing:
        notes.append(tr("clip {list} はイントロカードを作れませんでした", ...))
    if skipped:
        notes.append(tr("clip {list} は書き出しに失敗したため除きました", ...))
    return tr("完了") if not notes else tr("完了（{notes}）", notes="／".join(notes))
```

#### 1 本も残らなかった場合

`finish_clips` は `burned` が空なら `[]` を返し、`write_clips` が
「出力 0 件 = キャンセル扱い」にしている。
**脱落が原因で 0 件になった場合は例外にする** (キャンセルと区別する)。

```python
if not burned:
    if skipped:
        raise TimelineError(tr("すべてのクリップの書き出しに失敗しました (clip {list})", ...))
    _logger.info("焼き込み済みクリップが0件")
    return []
```

#### ログ

```
[WARNING] clip3 のレンダリングに失敗 → このクリップを飛ばします
[INFO] 結合出力: ... (使用5 / 除外1)
```

---

### R11. 出力設定を設定画面へ出す

#### 現状

`ffmpeg.output_width` / `output_height` / `output_fps` / `preset` / `crf` / `video_codec` は
設定画面にウィジェットが無く、`setting.json` を手で書くしか変更手段が無い。

#### 設計 — 細かい値ではなく 3 択のプリセット

設定画面「一般」タブへ **「出力」グループ**を追加する。

| ウィジェット | 設定キー | 選択肢 |
| --- | --- | --- |
| 出力サイズ (コンボ) | `ffmpeg.output_scale_mode` | 素材と同じ (推奨) / 1920x1080 固定 |
| 画質と速度 (コンボ) | `ffmpeg.quality_profile` | 速度優先 / 標準 (推奨) / 品質優先 |
| 注記ラベル | — | 選択に応じた想定時間を出す (R13 と同じ係数) |

`quality_profile` は**保存時に実値へ展開する**。利用者が `setting.json` を直接
編集している場合を壊さないため、プロファイルは「適用ボタン」ではなく
**保存時に `preset` / `crf` / `intermediate_preset` / `intermediate_crf` /
`asr_audio_prefetch` を書き換える**方式とする。

| プロファイル | `preset` | `crf` | `intermediate_preset` | `intermediate_crf` | `asr_audio_prefetch` | ベース + 焼き込み (1800 秒) |
| --- | --- | --- | --- | --- | --- | --- |
| 速度優先 | `veryfast` | 20 | `veryfast` | 18 | false | 約 357 秒 (6.0 分) |
| **標準** | `medium` | 20 | `veryfast` | 18 | true | 約 578 秒 (9.6 分) |
| 品質優先 | `medium` | 18 | (空) | (空) | true | 約 2640 秒 (44 分) |

右端の列は **R1 (コピー結合) と R2 (素材等倍) が入った状態**の「ベース映像構築 + 字幕焼き込み」で、
60 秒ベンチの比 (1080p60 medium = 46.6 秒を 1.0 とする) で換算した値である。
`intermediate_preset` を使う行は R3a (フェーズ 2) が前提になるため、
**R11 を R3a より先に入れる場合は「標準」の中間設定を空にしておく** (その場合 約 793 秒)。
「品質優先」は `output_scale_mode` も `fixed` に倒す = 従来と同じ出力になる。

`quality_profile="custom"` のときは展開せず、現在値をそのまま保つ
(= `setting.json` を手で書いた利用者の値を壊さない)。

#### マイグレーション

`_merge_with_defaults` で新キーが補われる既存の仕組みに乗せる。
`quality_profile` の既定は `"custom"` とし、**既存利用者の値は変えない**。
新規インストール時の既定だけ `"standard"` にする (`DEFAULT_SETTINGS` 側)。

→ ただし「既存利用者には何も起きない」のは R2 の恩恵も届かないことになる。
`output_scale_mode` は R2 の効果が大きいため**既存利用者にも `"source"` を既定として配る**。
この判断は §13-1 で確認する。

---

### R13. 書き出し対象の尺と想定時間を見せる

#### 目的

書き出し対象の尺は `top_n × window_sec` = 1800 秒で固定され、VOD の長さに依存しない。
利用者からはセクション数 (6 本) しか見えないため、30 分ぶんを書き出すことが分からない。

#### 設計

新しいダイアログは**出さない** (クリックを増やさない)。
採点完了直後の状態表示と進捗ラベルへ出す。

```python
# gui/archive_tab.py : _on_analyze_done()
total_sec = sum(c["end"] - c["start"] for c in clips)
estimate = archive_config.estimate_export_sec(self._settings, total_sec, len(clips))
self.status_label.setText(
    tr("{n} セクション / 合計 {len} / 想定 約 {est}",
       n=len(clips), len=_fmt_minutes(total_sec), est=_fmt_minutes(estimate)))
```

```python
# archive/config.py (新設)

# 書き出し時間の目安を返す (resolve3 R13)
# 係数は setting.json に持つ (機械によって 2 倍以上変わるためハードコードしない)。
# archive.estimate.sec_per_content_sec : 尺 1 秒あたりの秒数 (既定 1.06)
# archive.estimate.fixed_sec           : 尺に依存しない固定費 (既定 60)
# archive.estimate.sec_per_clip        : クリップ 1 本あたりの固定費 (既定 8)
def estimate_export_sec(settings, content_sec, clip_count):
    ...
```

既定値の根拠 (フェーズ 1 適用後 / resolve2.md の実機):
`(42 + 2 + 73 + 29 + 7.5 + 949 + 390 + 402 + 12) / 1800 ≈ 1.06` 秒/秒、固定費 42 秒。

#### 将来拡張

`archive.estimate` の係数を**実行後の実測で更新する**余地を残す
(telemetry が既に工程の所要時間を送っているため、同じ値をローカルへも残せる)。
本書では固定係数のみ実装する。

---

## 4. setting.json 定義 (追加・変更)

| キー | 型 | 既定 | 用途 | 対応 |
| --- | --- | --- | --- | --- |
| `ffmpeg.output_scale_mode` | str | `"source"` | `"source"` = 素材等倍 / `"fixed"` = 従来 | R2 |
| `ffmpeg.output_fps_standards` | list[int] | `[24, 25, 30, 50, 60]` | 素材 fps を丸める候補 (空で丸めない) | R2 |
| `ffmpeg.intermediate_preset` | str | `""` | 中間ファイルの preset (空 = `preset`) | R3a |
| `ffmpeg.intermediate_crf` | str | `""` | 中間ファイルの crf (空 = `crf`) | R3a |
| `ffmpeg.quality_profile` | str | `"custom"` (新規は `"standard"`) | 設定画面の 3 択 | R11 |
| `timeline.render.direct_burn` | bool | `false` | 区間 1 件のときベース映像を省く (§13-5 の判断により様子見のため既定 OFF) | R3b |
| `archive.combine.reencode` | bool | `false` | `true` で従来の再エンコード結合 | R1 |
| `archive.scoring.feature_mode` | str | `"single_pass"` | `"per_cell"` で従来のセル別 volumedetect | R5 |
| `archive.estimate.sec_per_content_sec` | float | `1.06` | 想定時間の係数 | R13 |
| `archive.estimate.fixed_sec` | float | `60` | 想定時間の固定費 | R13 |
| `archive.estimate.sec_per_clip` | float | `8` | クリップ 1 本あたりの固定費 | R13 |
| `loudness.measure_scope` | str | `"selected"` | `"vod"` で従来どおり全長測定 | R7 |
| `loudness.measure_max_ranges` | int | `20` | 区間測定の入力数上限 | R7 |
| `subtitle.whisper_model_cache` | bool | `true` | モデルをプロセス内で使い回す | R6 |
| `subtitle.asr_audio_prefetch` | bool | `true` | プレビュー音声を先に作る | R8 |

#### 既存キーの扱い

- **1 つも削除・改名しない。**
- `ffmpeg.output_width` / `output_height` / `output_fps` は
  `output_scale_mode="source"` のとき**上限値**として使う (意味を足すだけ)。
- `archive.clip_pipeline.loudness_scope` (既存) は「揃える範囲」のまま。
  `loudness.measure_scope` (新) は「測る範囲」。両者は独立に効く。
- 追加は `settings_window.DEFAULT_SETTINGS` と `_merge_with_defaults` の既存仕組みで
  自動的に補われる。`_fill_nested_defaults` の対象セクション
  (`archive` / `timeline` / `ui` / `blur`) に `archive.estimate` を追加する。

---

## 5. 関数一覧

### 5.1 新設

| モジュール | 関数 | 役割 |
| --- | --- | --- |
| `modules/ffmpeg_runner.py` | `probe_stream_params(input_path, ffmpeg_settings)` | 結合可否の判定に使う主要ストリーム情報 |
| `modules/output_profile.py` | `resolve_canvas(input_path, settings)` | 出力キャンバス (寸法 + fps) の解決 |
| `modules/output_profile.py` | `apply_canvas_to_settings(settings, input_path=None, canvas=None)` | 解決値を実行用設定コピーへ書き戻す |
| `archive/features.py` | `scan_audio(input_path, settings, progress_cb=None)` | 全長 1 パスの音声解析 |
| `archive/config.py` | `estimate_export_sec(settings, content_sec, clip_count)` | 想定書き出し時間 |
| `archive/clip_writer.py` | `_can_stream_copy(parts, ffmpeg_cfg, settings)` | 部品の規格一致判定 |
| `timeline/renderer.py` | `_direct_source(timeline, context, cfg)` | ベース省略の条件判定と入力組み立て |
| `pipeline/pipeline_context.py` | `use_single_step()` | 単一工程として進捗を扱う |
| `pipeline/pipeline_context.py` | `set_asr_audio_builder(builder)` / `build_asr_audio()` | プレビュー音声の遅延生成 |
| `modules/subtitle_generator.py` | `release_models()` | Whisper モデルキャッシュの解放 |
| `modules/subtitle_generator.py` | `WhisperTextSource.set_progress(cb)` | 認識の進捗通知 |

### 5.2 シグネチャ変更 (すべて省略可引数の追加 = 既存呼び出しは不変)

| モジュール | 関数 | 追加する引数 |
| --- | --- | --- |
| `utils/progress.py` | `run_ffmpeg_progress` | `cancel_cb=None` |
| `modules/ffmpeg_runner.py` | `execute` / `execute_capture` | `cancel_cb=None` |
| `modules/silence_cutter.py` | `concat_files` | `total_duration=0.0`, `on_progress=None` |
| `modules/volume_analyzer.py` | `analyze_min_speech_db` | `scan=None` |
| `modules/loudness_normalizer.py` | `measure_loudness` | `ranges=None` |
| `modules/subtitle_generator.py` | `burn_subtitle` | `input_args=None` |
| `archive/clip_writer.py` | `write_clips` | `vod_scan=None`, `cancel_cb=None` |
| `archive/clip_writer.py` | `analyze_vod` | `used=None`, `scan=None` (progress_cb の引数を `(ratio, label)` へ) |
| `archive/clip_writer.py` | `_combine` | `settings=None`, `on_progress=None` |
| `archive/project_resume.py` | `run_from_archive_project` | `cancel_cb=None` |

### 5.3 挙動変更 (シグネチャは不変)

| モジュール | 関数 | 変更 |
| --- | --- | --- |
| `modules/silence_cutter.py` | `detect_silence` | `-vn` を追加 |
| `modules/volume_analyzer.py` | `measure_region_volume` | `-vn` を追加 |
| `modules/ffmpeg_runner.py` | `build_intermediate_encode_options` | 中間用 preset/crf を見る |
| `modules/output_profile.py` | `_landscape_profile` | 戻り値へ `fps` を追加 |
| `modules/output_profile.py` | `resolve_output_profile` | 戻り値へ `reference_width` / `reference_height` / `scale` を追加 (R2 付帯対応) |
| `modules/subtitle_generator.py` | `run` | ASS の `PlayResX/Y` へ基準キャンバスを渡し、アイコンへ `scale` を渡す |
| `archive/clip_writer.py` | `_write_intro_ass` の呼び出し | `w, h` へ基準キャンバスを渡す (実フレームの `_normalize_chain` は実キャンバスのまま) |
| `pipeline/pipeline_runner.py` | `run` | 入口で `apply_canvas_to_settings` を通す (§13-4) |
| `timeline/compositor.py` | `CanvasProfile.production` / `proxy` | fps を `timeline.fps` 優先に |
| `archive/features.py` | `extract_cells` | `feature_mode` で 1 パスへ分岐 |
| `archive/pipeline.py` | `analyze` | 戻り値へ `audio_scan` を追加 |
| `archive/clip_writer.py` | `_render_clips` | 進捗注入 / 脱落の収集 / 戻り値 `(burned, skipped)` |
| `archive/clip_writer.py` | `_finish_label` | 脱落クリップを文言へ |

### 5.4 削除

**なし。** 既存の関数・設定・経路は 1 つも消さない。

---

## 6. エラー処理方針

### 6.1 原則

| 分類 | 扱い |
| --- | --- |
| **中止** (`PipelineCancelled`) | 必ず貫通させる。途中成果物は破棄し、ポイントはキャンセル |
| **容量不足** (`DiskSpaceError`) | 必ず貫通させる (継続すると原因から離れた所で症状が出る) |
| **高速化の失敗** | 必ず**従来経路へ落ちて成果物を出す**。速度のための変更で成果物を失わせない |
| **それ以外** | 従来の方針を変えない |

### 6.2 「従来経路へ落ちる」一覧

| 変更 | 失敗したとき | 結果 |
| --- | --- | --- |
| R1 コピー結合 | 規格不一致 / probe 失敗 / コピー失敗 | 再エンコード結合 |
| R2 素材等倍 | 寸法/fps が取れない / 異常値 | `fixed` と同じ値 |
| R3b ベース直結 | 条件を満たさない | 従来のベース映像構築 |
| R5 1 パス解析 | `astats` の出力が読めない / 件数が合わない | `per_cell` へ落ちて測り直す |
| R5 引き継ぎ | `scan.params` が現設定と不一致 | その場で測り直す |
| R7 区間測定 | concat が失敗 | 全長測定 → さらに失敗ならクリップ単位 |
| R6 モデルキャッシュ | 構築失敗 | 従来どおりその場で構築 (例外はそのまま) |

### 6.3 新しい例外は作らない

既存の `FFmpegError` / `InputError` / `TimelineError` / `DiskSpaceError` /
`PipelineCancelled` で足りる。R10 の「全クリップ脱落」は `TimelineError` を使う。

---

## 7. ログ出力方針

既存の `utils/logger.get_logger` のみを使う (ライブラリを増やさない)。

| レベル | 出す内容 |
| --- | --- |
| INFO | 解決したキャンバス / 結合方式 / 1 パス解析の件数 / 区間測定の範囲 / モデルキャッシュの命中 / 脱落クリップ |
| WARNING | 従来経路へ落ちた事実とその理由 (必ず「何へ落ちたか」を書く) |
| ERROR | 貫通させる失敗 (容量不足・全クリップ脱落) |
| INFO | **工程ごとの所要時間** (新規。下記) |

### 7.1 工程の所要時間をログへ残す

resolve2.md の調査は本体を計装して測った。**次からは計装なしで比べられるようにする。**

```
[INFO] 書き出し工程の所要: 解析 42.1s / 準備 988.3s (文字起こし 949.0s) /
       レンダリング 490.2s (ベース 88.1s / 焼き込み 402.1s) / 結合 11.8s / 合計 1532.4s
```

`telemetry` は既に工程時間を送っているため、同じ値をローカルログへ 1 行出すだけでよい。
R13 の係数を実測で見直す材料にもなる。

---

## 8. 実装順序

| 順 | 項目 | 理由 | 検証 |
| --- | --- | --- | --- |
| 1 | R4 (`-vn`) | 数行・結果不変 | 既存テストが通ること + 採点時間の実測 |
| 2 | R1 (コピー結合) | 数行・効果最大 | 結合後の尺・音ズレ・OP/ED 混在 |
| 3 | R6 / R8 (モデル再利用 / 音声遅延) | 小さく独立 | 字幕件数が変わらないこと |
| 4 | R10 (脱落の明示) + R9-6 (`except` の見直し) | 以降の変更の失敗が見えるようになる | 意図的に 1 クリップを壊して文言を確認 |
| 5 | R9 (進捗 + 中止) | 以降の長い処理が観察できるようになる | 各区間で進捗が動くこと / 中止が効くこと |
| 6 | R2 (素材等倍 + テロップ相対サイズの付帯対応) | 効果最大。4・5 が入っていると検証しやすい | 出力寸法・fps・字幕位置・**文字の相対サイズ**・プレビュー一致 (§3 R2 の受け入れ確認) |
| 7 | R5 (1 パス解析) | 採点の体感が変わる | セル値が従来と一致すること (§10) |
| 8 | R7 (区間ラウドネス) | 音量の再確認が要る | 出力の LUFS が従来と同等 |
| 9 | R11 / R13 (設定画面 / 想定時間) | 実装後の値で係数を決められる | 保存・読込の往復 |
| 10 | R3a → R3b (中間設定 → ベース直結) | 構造に触るため最後。R3b は既定 OFF で入れる (§13-5) | 尺・字幕時刻の一致 (§10) |
| 11 | R2 / R3a を本流パイプラインへ適用 (§13-4) | 切り抜き経路で効果と安全を確かめた後に広げる | 本流の通しで寸法・fps・尺・テロップ位置が従来と一致 |

**6 と 7 の間で 1 回、通しの実測を取り直す** (resolve2.md と同じ手順 / §8 の計装)。

---

## 9. 効果の積算 (再掲 + 内訳)

| 工程 | 現状 | フェーズ 1 | フェーズ 2 | 効いた項目 |
| --- | --- | --- | --- | --- |
| VOD 全体 音量解析 | 122 | **0** | 0 | R5 (採点から引き継ぐ) |
| VOD 全体 ラウドネス測定 | 144 | **42** | 42 | R7 |
| 区間切り出し | 2 | 2 | 2 | — |
| 音声正規化 | 73 | 73 | 73 | — |
| 編集点検出 | 29 | 29 | 29 | — |
| プレビュー音声抽出 | 41 | **0** | 0 | R8 |
| Whisper モデル読込 | 45 | **8** | 8 | R6 |
| 文字起こし | 949 | 949 | 949 | — |
| ベース映像構築 | 1300 | **390** | **88** | R2 → R3a → R3b |
| 字幕焼き込み | 1340 | **402** | 402 | R2 |
| 最終結合 | 1217 | **12** | 12 | R1 |
| **書き出し合計** | **5262 秒 (88 分)** | **1907 秒 (32 分)** | **1604 秒 (27 分)** | |
| 採点 | 260 秒 | **13 秒** | 13 秒 | R4 + R5 |

上表のフェーズ 2 の「ベース映像構築 88」は R3b (既定 OFF / §13-5) を有効にしたときの値である。
既定のままなら R3a までで 175 秒、書き出し合計は 1692 秒 (28 分) になる。

フェーズ 2 まで入れると、**残りの 59% (949 秒) が文字起こし**になる。
認識は品質と直結するため本書では触らない (resolve2.md §9-2)。
そこから先を縮めたい場合は R12 ではなく**認識の設定**が次の論点になる。

---

## 10. テスト計画

既存の `tests/` へ追加する (pytest / 既存の書き方に合わせる)。

| 新規テスト | 確かめること |
| --- | --- |
| `test_combine_stream_copy.py` | 規格一致でコピー経路 / 不一致で再エンコード経路 / `reencode=true` で常に従来 / コピー失敗時に再エンコードへ再試行 |
| `test_output_canvas.py` | `source`/`fixed` の解決値 / fps の標準値丸め (28.8→30) / 偶数丸め / 上限クランプ (**2560x1440 → 1920x1080** / 3840x2160 → 1920x1080 / 1280x720 は拡大しない) / probe 失敗で `fixed` / 縦動画は 1080x1920 |
| `test_subtitle_reference_canvas.py` | 720p 等倍で ASS の `PlayResX/Y` が **1920x1080 のまま**になる / `scale` が 0.667 でアイコンへ渡る / 1080p 以上では `scale=1.0` で従来と完全に同一の ASS になる / 4:3 素材で基準のアスペクト比が実キャンバスと一致する |
| `test_output_canvas_resume.py` | 保存済みプロジェクトを開くと**素材から再解決しない** (Timeline の値を使う) |
| `test_audio_scan.py` | `astats` 出力のパース (`-inf`/`nan` を弾く) / 1 秒値→セル値の按分がエネルギー平均 / `per_cell` と `single_pass` で採点結果が一致する |
| `test_audio_scan_handoff.py` | `params` 不一致で測り直す / 一致で再測定しない |
| `test_loudness_ranges.py` | `ranges` 指定でコマンドが多入力 concat になる / 失敗で全長測定へ落ちる / 上限超過で間引く |
| `test_whisper_model_cache.py` | 同一設定で 1 回だけ構築 / 設定が違えば作り直す / `release_models` で解放 |
| `test_export_cancel.py` | `cancel_cb` が `True` を返すと `PipelineCancelled` が貫通する / 広い `except` が飲み込まない / ポイントがキャンセルされる |
| `test_export_progress.py` | 進捗が単調増加し、0.00→1.00 を**区間の飛びなしで**通る |
| `test_render_direct_burn.py` | 条件を満たすと `_render_base` を呼ばない / 出力尺が従来と一致 / 条件を外すと従来経路 |
| `test_skipped_clips.py` | 1 クリップ失敗で文言に出る / 全滅で `TimelineError` |

| 既存テストの追補 | 内容 |
| --- | --- |
| `test_renderer_timing.py` | 直結モードでも尺が 1 フレーム以内で一致すること |
| `test_settings_migration.py` | 新キーが補われ、既存値が壊れないこと (`direct_burn` の既定が `false` であること) |
| `test_quality_resolve1.py` | `feature_mode` 切り替えで F6/F7 の挙動が保たれること |

### 10.1 実機での受け入れ確認

resolve2.md と同じ VOD (`2913872726`) で通しを 1 回実行し、下記を確認する。

1. 出力の寸法・fps が 1280x720@30 になっている
1-b. テロップ・アイコン・イントロカードの文字が**フレーム高に対する比率で従来と一致**している
2. 字幕の位置と時刻が従来と一致している (目視 + 先頭/中間/末尾のフレーム比較)
3. 音量が従来と同等 (出力の `loudnorm` 測定値を比較)
4. 進捗が止まる区間が無い
5. 中止ボタンが 5 秒以内に効く
6. 所要時間がログの 1 行で確認できる

---

## 11. リスクと切り戻し

| 変更 | リスク | 切り戻し |
| --- | --- | --- |
| R1 | OP/ED の規格差で結合が壊れる | 規格一致を `ffprobe` で確認。`archive.combine.reencode=true` |
| R1 | mp4 の編集リストの違いで境界に不連続が出る | 既存の `+genpts` / `avoid_negative_ts` / `max_interleave_delta=0` をそのまま使う。受け入れ確認 2 で検証 |
| R2 | 非整数 fps で CFR 化が崩れる | `output_fps_standards` で丸める。`output_scale_mode=fixed` |
| R2 | 「1080p だったのが 720p になった」と見える | R11 で明示的に選ばせる。素材が 1080p の VOD は 1080p のまま。2K/4K は上限で 1080p になり現状と同一 |
| R2 | **テロップ・アイコンが相対的に大きくなる** (絶対 px + PlayRes 追随) | ASS の PlayRes を基準キャンバスへ固定し、アイコンへ `scale` を渡す (§3 R2)。受け入れ確認 1-b。`output_scale_mode=fixed` |
| R2 | 編集画面のプレビューと出力の寸法が食い違う | 編集画面へ解決済み設定を渡す。`CanvasProfile` の fps を Timeline 基準に |
| R3a | 中間で 1 世代ぶん劣化が増える | `crf 18` で相殺。既定は空 (従来値) |
| R3b | A/V 同期の既知対策 (20260812 resolve3) を踏み外す | 7 条件すべて一致のときのみ。`direct_burn=false` |
| R4 | なし (出力は破棄しており結果が変わらない) | `-vn` を外す |
| R5 | `astats` の値が `volumedetect` と微妙に違い、閾値が動く | テストで両モードの採点一致を確認。`feature_mode=per_cell` |
| R5 | 採点後に設定を変えられると古い値で切る | `params` を照合して不一致なら測り直す |
| R6 | モデルを抱えたままメモリを占有する (int8 約 1.5 GB) | 書き出し完了時に `release_models()`。`whisper_model_cache=false` |
| R7 | 測定基準が変わり、出力音量が従来とわずかに違う | 受け入れ確認 3 で比較。`loudness.measure_scope=vod` |
| R8 | プレビュー初回再生が遅くなる | 既定 `true` (従来)。速度優先プリセットでのみ `false` |
| R9 | 中止の貫通で、従来「継続していた」失敗が止まるようになる | 貫通させるのは `PipelineCancelled` と `DiskSpaceError` の 2 つだけ |
| R11 | プロファイル展開で手書きの値が上書きされる | `quality_profile="custom"` のときは展開しない |

---

## 12. 将来拡張方針

| 項目 | 内容 |
| --- | --- |
| 2K/4K をそのまま残す出力 | `output_width/height` を上げれば残るが書き出しは約 1.8 倍 (2K) になる。必要になってから R11 の設定画面に選択肢として出す |
| 想定時間の自己校正 | R13 の係数を実行後の実測で更新する。ログ 1 行 (§7.1) が材料になる |
| 1 パス解析の拡張 | `scan_audio` の 1 秒値は resolve1.md F1 (発話率の採点) の素にもなる。VAD を同じパスへ足せる |
| ベース直結の条件緩和 | R3b の条件 3 (オーバーレイ無し) は `_composite` 側も多入力で組めるため、将来は緩められる |
| GPU エンコード | R12 は**本書では実施しない**。R2 で x264 のまま 19 分になるため、必要になってから検討する |

---

## 13. 判断の結果 (2026-11-01 決定)

初版で保留していた 6 件について、以下のとおり決定した。
以降の節は本決定を反映済みである。

| # | 項目 | 決定 | 本書への反映 |
| --- | --- | --- | --- |
| 13-1 | `output_scale_mode` の既定 | **`"source"` (素材等倍) を採用** | §3 R2 に上限の定義と付帯対応を追記 |
| 13-2 | `feature_mode` の既定 | **`"single_pass"` で確定** | §4 のまま |
| 13-3 | ラウドネスのキー整理 | **保留** (今回は整理しない) | §13-3 に保留の扱いを明記 |
| 13-4 | 本流パイプラインへの R2/R3a 適用 | **適用する** | §1・§5.3・§8 (順 11)・§12 を更新 |
| 13-5 | `timeline.render.direct_burn` の既定 | **様子見 = 既定 `false`** | §4 の既定を `false` へ変更 |
| 13-6 | 中止時の成果物 | **提案どおり「何も出さない」** | §9/§10 のまま |

---

### 13-1. `output_scale_mode` の既定 → **`"source"` (決定)**

素材等倍を既定とする。拡大による軟化が無くなるため画質は上がり、
効果も最大 (−31 分) である。

#### 高解像度素材についての確認事項 (質疑)

> **Q. 2K 配信を行ったアーカイブはどうなるか。書き出しに時間がかかるか。
> 出力時にテロップと動画がズレないか。**

**A-1. 出力は現状と変わらない。**
`output_width/height` (1920x1080) は `"source"` でも**上限**として効く。
2560x1440 の素材はアスペクト比を保ったまま上限枠へ収められ、
**今と同じ 1920x1080 へ縮小される**。等倍になるのは 1080p 以下の素材だけである。

**A-2. 書き出しは遅くならない。**
処理する画素数が現状 (1080p) と同一のため、R2 による増減はゼロである。
2K のアーカイブでも R1 (コピー結合) / R3a / R4〜R8 の短縮はそのまま効く。
なお `output_width/height` を 2560x1440 へ上げれば 2K の画素を残せるが、
画素スループットが 1.78 倍になり**書き出し時間も約 1.8 倍**になるため既定では上げない。

**A-3. テロップと映像の「ズレ」は起きない。ただし相対サイズは要対応だった。**

- **表示タイミングのズレは起きない。** 字幕は秒で保持しており寸法と無関係である。
- **位置のズレも起きない。** 個別位置は正規化座標 (-1..1) を出力寸法で px へ戻すため
  寸法に追随する。
- 一方で `font_size` などが**絶対 px** かつ ASS の `PlayRes` が出力寸法に追随するため、
  **1080p 未満の素材では文字と余白が相対的に大きくなる**。
  これは初版の見落ちであり、§3 R2 に
  「テロップ・コメントアイコンの相対サイズ (R2 の必須付帯対応)」として設計を追記した。
  PlayRes を基準キャンバス (1920x1080) へ固定し、libass の相似変換に任せる
  (編集画面のプロキシが既に使っている手口と同じ)。
  **2K の素材は上限で 1080p になるため `scale=1.0`、すなわち ASS は従来と完全に同一である。**

#### 既存利用者への通知

更新後の初回起動でポップアップは**出さない**。R11 で出力設定を設定画面へ出すため、
そこで現在のモードが見えることと、§3 R2 のログ 1 行で足りると判断した。

---

### 13-2. `feature_mode` の既定 → **`"single_pass"` (決定)**

実測 259.5 秒 → 13.2 秒 (20 倍) であり、既定にする。
ただし `astats` の RMS と `volumedetect` の mean_volume は定義が完全同一ではないため、
**§10 の `test_audio_scan.py` で両モードの TOP 選択が一致することを確認してから既定にする**
という条件は維持する。一致しなかった場合のみ `"per_cell"` 既定へ戻す
(設定キーは両方残るため切り替えで済む)。

---

### 13-3. `loudness.measure_scope` と `archive.clip_pipeline.loudness_scope` の整理 → **保留**

- R7 (ラウドネス測定を採用区間だけにする) の**実装は行う**。
- キーの改名・統合は**行わない**。既存キーの改名は互換を壊すためである。
- 「測る範囲」(`loudness.measure_scope`) は**設定画面へ出さず内部設定に留める**。
  2 キー併存のまま進め、名前の整理は別途あらためて判断する。

---

### 13-4. 本流パイプライン (`pipeline_runner`) への R2 / R3a 適用 → **適用する (決定)**

同じ無駄が同じ理由で存在するため、本流にも適用する。

- 入口で `apply_canvas_to_settings` を 1 回通す (§5.3)。
- ただし**切り抜き経路で受け入れ確認を通した後に広げる**。
  実装順序は §8 の順 11 (最後) とし、本流の通しで寸法・fps・尺・テロップ位置が
  従来と一致することを確認する。
- 本流は全長を 1 本で処理するため、テロップの相対サイズの確認 (§3 R2 の受け入れ確認)
  も本流側で 1 回行う。

---

### 13-5. R3b の既定 (`timeline.render.direct_burn`) → **既定 `false` で様子見 (決定)**

A/V 同期は過去に何度も踏んだ地雷であるため、**既定 `false`** で出す。

- §4 の既定を `true` → `false` へ変更した。
- 実装はフェーズ 2 (§8 の順 10) で行い、受け入れ確認 2 を通す。
- 手元で `true` にして数本を確認し、問題が出なくなってから既定を変える。
- そのため **R3b の −88 秒は既定では効かない**。
  フェーズ 2 の効果 (−303 秒) のうち既定で効くのは R3a の −215 秒である。

---

### 13-6. 中止時の成果物の扱い → **提案どおり「何も出さない」(決定)**

中止では一時フォルダごと破棄し、成果物を残さない。
中途半端な成果物を成功として残すのは R10 (脱落を明示する) の方針と矛盾するためである。

---

## 14. 実装状況 (2026-11-01)

本書の全件を実装した。既存テスト 2083 件 + 新規 116 件 = **2199 件すべて成功**。

### 14.1 実装した項目

| # | 内容 | 主な実装 |
| --- | --- | --- |
| R1 | 最終結合をストリームコピーへ | `ffmpeg_runner.probe_stream_params` / `clip_writer._can_stream_copy` / `_combine` / `silence_cutter.concat_files` へ `total_duration` `on_progress` `cancel_cb` |
| R2 | 出力キャンバスを素材等倍に | `output_profile.resolve_canvas` / `apply_canvas_to_settings` / `reference_height` / `_attach_reference` / `_landscape_profile` へ `fps`・`reference_*`・`scale` / `compositor._timeline_fps` |
| R2 付帯 | テロップの相対サイズ | `subtitle_generator.reference_canvas` / `run` の PlayRes とアイコン `scale` / `clip_writer._ass_canvas` (イントロカード・タグ) / `_burn_one` |
| R3a | 中間ファイルのエンコード設定を分離 | `ffmpeg_runner.intermediate_preset` / `intermediate_crf` / `build_intermediate_encode_options` |
| R3b | 区間 1 件でベース映像を省く | `renderer._direct_source` / `_burn_subtitles_only(direct=)` / `subtitle_generator.burn_subtitle(input_args=)` / `builder.timeline_config` へ `direct_burn` |
| R4 | 解析パスに `-vn` | `silence_cutter.detect_silence` / `volume_analyzer.measure_region_volume` |
| R5 | 全長解析を 1 パスへ + 引き継ぎ | `features.scan_audio` / `scan_params` / `scan_matches` / `energy_mean_db` / `region_levels` / `_cells_from_scan` / `extract_cells` の `feature_mode` 分岐 / `pipeline.analyze` の `audio_scan` / `volume_analyzer.analyze_min_speech_db(scan=)` / `clip_writer.write_clips(vod_scan=)` |
| R6 | Whisper モデルの使い回し | `subtitle_generator._model_cache` / `_get_model` / `release_models` (書き出し完了・設定保存・本流 `_cleanup` で解放) |
| R7 | ラウドネス測定を採用区間だけに | `loudness_normalizer.measure_loudness(ranges=)` / `_pick_ranges` / `_measure_ranges` / `_run_measure` |
| R8 | プレビュー音声の遅延生成 | `subtitle_generator._prepare_asr_audio` / `pipeline_context.set_asr_audio_builder` `build_asr_audio` / `audio_source` の `initial_audio_builder` → `PreviewPanel` → ダイアログ → payload |
| R9 | 進捗を流し、中止できるようにする | `progress.run_ffmpeg_progress(cancel_cb=)` + `_ask_cancel` `_terminate` / `ffmpeg_runner.execute` `execute_capture` / `renderer._cancel_cb` / `pipeline_context.use_single_step` / `WhisperTextSource.set_progress` / `clip_writer._check_cancel` / GUI の中止ボタン |
| R9-6 | 広い `except` の貫通 | `clip_writer` 5 箇所 / `features` 1 箇所 / `subtitle_generator` 2 箇所に `except (PipelineCancelled, DiskSpaceError): raise` を先頭へ |
| R10 | 脱落クリップの明示 | `_render_clips` が `(burned, skipped)` を返す / `_finish_label(burned, skipped)` / 全滅時は `TimelineError` |
| R11 | 出力設定を設定画面へ | `settings_window` の「一般」タブへ「出力サイズ」「画質と速度」+ 注記 / `_collect_output_settings` / `QUALITY_PROFILE_VALUES` |
| R13 | 尺と想定時間を見せる | `archive_config.estimate_export_sec` / `archive_tab._on_analyze_done` の状態表示 / `_fmt_minutes` |
| §13-4 | 本流パイプラインへの適用 | `pipeline_runner.run_pipeline` の入口で `apply_canvas_to_settings` |

### 14.2 追加したテスト (新規 5 ファイル / 116 件)

| ファイル | 件数 | 対象 |
| --- | --- | --- |
| `tests/test_output_canvas.py` | 22 | R2 (上限クランプ・fps 丸め・偶数丸め・probe 失敗・基準キャンバス・縦動画) |
| `tests/test_audio_scan.py` | 22 | R5 (astats パース・エネルギー平均・両モードの一致・引き継ぎの照合) |
| `tests/test_combine_stream_copy.py` | 12 | R1 (規格一致・不一致・切り戻し・再試行・中止の貫通) |
| `tests/test_quality_resolve3.py` | 34 | R3a / R6 / R7 / R9 / R10 / R13 |
| `tests/test_render_direct_burn.py` | 14 | R3b (7 条件・既定 OFF・`input_args`) |
| `tests/test_subtitle_reference_canvas.py` | 12 | R2 付帯 (PlayRes 固定・アイコンの scale・1080p で ASS 不変) |

既存テストの追補: `tests/test_watermark_archive.py` のスタブを `**_kwargs` 受けに変更
(後から増える省略可引数でテストが壊れないようにする)。
`src/i18n/catalog_en.py` へ新規文言 29 件の対訳を追加 (`test_i18n` が網羅を検査する)。

### 14.3 実装時に設計から変えた点

| 項目 | 設計 | 実装 | 理由 |
| --- | --- | --- | --- |
| 基準キャンバスの持ち方 | 「基準高さは既存の `ffmpeg.output_height` をそのまま使う」 | 実行用コピーへ `ffmpeg.output_reference_height` を控える | `apply_canvas_to_settings` が `output_height` を解決値で上書きするため、上書き前の値を別に持たないと基準が失われる。`setting.json` へは保存されない実行時のみのキーで、`reference_height()` が無ければ `output_height` へ落ちる |
| `scan_audio` の無音ログ解析 | — | `silence_cutter.parse_silence_log` を公開入口として新設 | 同じログ形式の解析規則を 2 か所へ書かないため (実体は既存の `_parse_silence_log`) |
| `timeline.fps` への書き戻し | `apply_canvas_to_settings` が `timeline.fps` も置く | 置かない | `timeline/builder.build` と `archive/timeline_builder.build_archive_timeline` がどちらも `ffmpeg.output_fps` を読むため、`output_fps` の書き戻しだけで Timeline の軸が揃う。読まれないキーを増やさない |
| `timeline.render.direct_burn` | `setting.json` へ追加 | `builder.timeline_config` の `render` セクションへも追加 | `timeline_config` は `render` の既知キーだけを通すため、設定ファイルへ足すだけでは `renderer` へ届かない |
| 文字起こしの進捗の写し方 | 「`_prepare_edit_points` 側でラベルへ写す」 | クリップの帯 (0.05〜0.55 のうち 1 クリップぶん) へ認識比率をそのまま写す | 認識はクリップ準備の約 85% を占める最後の工程で、他の工程が帯の先頭に留まるため単調増加が保たれる |
| `_write_individual` | 変更なし | `settings=None` を追加 | 個別出力でも R1 のコピー結合の恩恵を受けられるようにした (省略時は従来どおり) |

### 14.4 既定で効くもの / 手元で切り替えるもの

| 項目 | 既定 | 備考 |
| --- | --- | --- |
| R1 コピー結合 | **有効** | `archive.combine.reencode=true` で従来へ戻す |
| R2 素材等倍 | **有効** (`output_scale_mode="source"`) | 1080p 以上の素材では出力が変わらない (§13-1) |
| R3a 中間設定の分離 | 無効 (`intermediate_preset`/`crf` が空) | R11 の「標準」「速度優先」を選ぶと展開される |
| R3b ベース直結 | **無効** (`direct_burn=false`) | §13-5 の判断。手元で確認後に既定を変える |
| R4 `-vn` | **有効** (切り替え無し) | 出力は破棄しており結果が変わらない |
| R5 1 パス解析 | **有効** (`feature_mode="single_pass"`) | `"per_cell"` で従来へ戻す |
| R6 モデル使い回し | **有効** | `subtitle.whisper_model_cache=false` で従来へ |
| R7 区間ラウドネス | **有効** (`measure_scope="selected"`) | `"vod"` で全長測定へ戻す |
| R8 プレビュー音声の遅延 | 無効 (`asr_audio_prefetch=true`) | R11 の「速度優先」で `false` になる |
| R9 進捗・中止 | **有効** (切り替え無し) | 中止ボタンはアーカイブタブの採点ボタン横 |
| R10 脱落の明示 | **有効** (切り替え無し) | — |
| R11 設定画面 | **有効** | `quality_profile` は既存利用者では `"custom"` のまま (手書きの値を壊さない) |
| R13 想定時間 | **有効** | 係数は `archive.estimate.*` |

### 14.5 残っている確認 (実機)

§10.1 の受け入れ確認は**実機での通し実行が必要なため未実施**。下記を 1 回確認すること。

1. 出力の寸法・fps が素材等倍になっている
2. テロップ・アイコン・イントロカードの文字がフレーム高に対する比率で従来と一致している
   (§3 R2 の受け入れ確認 1〜4)
3. 字幕の位置と時刻が従来と一致している
4. 音量が従来と同等 (出力の `loudnorm` 測定値を比較)
5. 進捗が止まる区間が無い / 中止ボタンが 5 秒以内に効く
6. 所要時間がログの 1 行で確認できる

また §13-2 の条件 (両モードの TOP 選択の一致) は `tests/test_audio_scan.py` の
`test_both_modes_select_the_same_top` で確認済みだが、**実機の VOD でも採点結果が
大きく動かないこと**を 1 回見ておくこと。動く場合は `feature_mode="per_cell"` へ戻せる。
