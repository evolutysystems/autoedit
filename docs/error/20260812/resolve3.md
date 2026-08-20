# アーカイブ用切り抜き テロップずれ（書き出し軸の累積ずれ）修正設計書

対象現象: `docs/error/20260812/error.md`（VOD `https://www.twitch.tv/videos/2847676012`）
解析結果: `docs/error/20260812/Analyze2.md`（2 回目の解析・実測）
前提: 同 `resolve2.md`（1 回目の修正設計・**適用済み**。字幕を理想軸へそろえる修正）

> **本書のファイル名について**
> 依頼は「`Analyze2.md` を基に `resolve2.md` を作成」だったが、`resolve2.md` は
> **1 回目の修正設計書（適用済み・現行の正）**として既に存在し、
> ソース中のコメント（`subtitle_generator.py` / `archive/timeline_builder.py` の
> 「20260812 resolve2.md §4」「同 §5-1」など）からも参照されている。
> 上書きすると実装の根拠が失われるため、**2 巡目の修正設計を `resolve3.md` として追加**した。
> `Analyze.md → resolve.md（初版・破棄）→ resolve2.md（正）` / `Analyze2.md → resolve3.md` の対応になる。

`docs/claude.md`「実装前に設計を行うこと」に従う修正設計書である。
記載の実装位置はすべて本リポジトリの実コードを読んで確認した（2026-08-19 時点）。
`Analyze2.md` §8 の未確定事項 Q1〜Q7（＋本書追加の Q8）には **2026-08-19 に利用者の回答を得た**。
確定内容は §9 にまとめ、本書はその回答に沿って実装する（Phase 0・Phase 3 は実施しない / §6）。

---

## 1. 結論（先に要点）

**直すのは書き出し側**である。字幕の時刻（モデル）は正しい（Analyze2 §3-1）。

| 原因（Analyze2） | 対処 | 変更箇所 |
|---|---|---|
| **X.** 中間パートを **AAC** で書き、concat デマルチプレクサで `-c copy` 連結しているため、**1 部品につき 21.3ms（1024 サンプル）** 音声が伸びて累積する | **中間パートの音声を非圧縮（`pcm_s16le` / `.mov`）にする**。AAC 化は焼き込み時の 1 回だけにする | `src/modules/ffmpeg_runner.py`（変更 1a）/ `src/timeline/renderer.py`（変更 1b・1c） |
| **Y.** クリップ尺がフレーム境界に乗らず、映像側の丸めで **+3.5ms/部品** の尺誤差が出る | **絶対フレーム番号でタイル化する**（各部品の尺を「終端フレーム − 開始フレーム」で決める）。差分を足し込まないので累積しない。あわせて **`-t` は切り捨てて渡す**（§5.3-1） | `src/timeline/renderer.py`（変更 2） |
| **Z.** ずれても誰も気づけない | **連結後の実尺とモデル尺を突き合わせ、1 フレームを超えたら WARNING を出す** | `src/timeline/renderer.py`（変更 3） |

* `Analyze2` §6 の **案 1（PCM）＋案 2（フレーム量子化）** を採用する。案 3・4・5 は採らない（§3-3〜3-5）。
* 効果は解析側で実測済み。**案 1 だけで境界の段差は 0.0ms**、案 2 を併せると尺も一致する（Analyze2 §6 の表）。
* 症状 A（重なり）・B（クリックで飛ぶ）は 1 回目の対策で解消済み。本書の対象外（§9 Q1）。

### 1-1. 全体像（変更後の流れ）

```
renderer.render(timeline, context)
 └ _render_base()
    ├ 各部品を書き出す          tl_part_00000.mov  … 変更 1b: 音声 pcm_s16le / 容器 .mov
    │   ├ _extract_clip()        -t は絶対フレーム差から算出 … 変更 2
    │   └ _render_gap()          同上
    ├ concat_files(-c copy)     timeline_base.mov  … 容器だけ変更。連結の既知対策はそのまま
    └ 実尺 vs モデル尺の照合    差が 1 フレーム超なら WARNING … 変更 3
 └ _render_overlays_and_subtitles()
    ├ 字幕/オーバーレイ有り → 従来どおり焼き込み（ここで初めて AAC 化。プライミングは 1 回だけ）
    └ どちらも無し           → _finalize_base() で mp4/AAC へ変換 … 変更 1c（新規・必須）
```

**要点**: 部品の段階で AAC を作らない。AAC は「完成品を 1 本作るときに 1 回だけ」にする。
そうすると 21.3ms は先頭に 1 回だけ乗り、**部品数に比例して増えることが無くなる**。

---

## 2. 前提の確認（コード実読）

### 2.1 書き出し経路は 1 本しかない

`archive/clip_writer._render_clips()`（`clip_writer.py:550`）は
クリップごとに `renderer.render()` を呼ぶ（`:574`）。通常パイプラインも同じ `render()` を通る。
**したがって renderer を直せばアーカイブ用・クリップ用の両方が同時に直る**（Analyze2 §5 / Q7）。

### 2.2 中間パートは AAC で書かれている

`_extract_clip()`（`renderer.py:137`）と `_render_gap()`（`renderer.py:245`）はどちらも
`ffmpeg_runner.build_encode_options()` を使う。その中身は次のとおりで、
**音声コーデックが `setting.json` の `ffmpeg.audio_codec`（現在 `aac`）で固定される**。

```python
# src/modules/ffmpeg_runner.py:224-230
def build_encode_options(ffmpeg_settings):
    return [
        "-c:v", ffmpeg_settings.get("video_codec", "libx264"),
        "-preset", ffmpeg_settings.get("preset", "medium"),
        "-crf", str(ffmpeg_settings.get("crf", 20)),
        "-c:a", ffmpeg_settings.get("audio_codec", "aac"),
    ]
```

この関数は**焼き込み・連結・区間抽出でも共有されている**（`silence_cutter.extract_segment` /
`concat_processor.concat` / `subtitle_generator.burn_subtitle`）。
最終出力は AAC のままにしたいので、**この関数は変えず、中間用の別関数を足す**（§5.2）。

### 2.3 連結は concat デマルチプレクサ + `-c copy`

```python
# src/timeline/renderer.py:116-119
silence_cutter.concat_files(
    part_paths, output_path, ffmpeg_cfg,
    reencode=cfg["render"]["concat_reencode"],   # 既定 false
)
```

`concat_files` は `_concat_demux()`（`silence_cutter.py:338`）への薄い委譲で、
`-fflags +genpts` / `-avoid_negative_ts make_zero` / `-max_interleave_delta 0` という
既知対策を持つ。**次のファイルの開始位置は「前のファイルの尺」で決まる**ため、
音声が 21.3ms 長い部品を並べると、その 21.3ms がそのまま中身のずれとして積み上がる。

### 2.4 部品の尺はフレーム境界に乗っていない

`_extract_clip()` は `-t f"{duration:.3f}"`（`renderer.py:157`）で秒指定する。
`duration` は無音カットが返した端数付きの値（例 5.0041 秒）で、60fps のフレーム
（16.667ms）境界には乗らない。映像はフレーム単位に丸められるため、
Analyze2 §3-3 の「端数あり = +24.6ms/部品」の差分（21.3 → 24.6 の +3.3ms）はここで生じる。

`Timeline` にはフレーム量子化の道具が**既にある**が、レンダリングでは使っていない。

```python
# src/timeline/model.py:723-733
def to_frames(self, sec):  ...   # 秒 → フレーム番号 (絶対値で丸めるので累積しない)
def from_frames(self, frames): ...
def quantize(self, sec): ...
```

### 2.5 クリップ間の結合は別方式で、累積しない

`clip_writer._combine()`（`clip_writer.py:757`）は `concat_processor.concat()` を使う。
これは **concat フィルタ + 再エンコード**で、各入力に `asetpts=N/SR/TB` を掛け直す
（`concat_processor.py:54-56`）。1 プロセスで作るためプライミングは 1 回しか乗らない。

* → **クリップをまたぐ累積は無い**。Analyze2 §3-2 の「クリップ毎にリセット」と整合する。
* → 逆に言えば、**累積は `renderer._render_base()` の中だけで起きている**。本書はここだけを直す。

### 2.6 同じ機構が残る場所（今回は対象外）

| 場所 | 状況 | 本書の扱い |
|---|---|---|
| `silence_cutter.extract_segment` + `cut_and_concat_seek`（`silence_cutter.py:175` / `:223`） | 同じく AAC 部品 → `_concat_demux`。従来画面経路（`timeline_review=false`）と通常の無音カットで使う | **今回は触らない**（§9 Q2 で確定）。字幕も同じ物理軸で作るため実害は出ていないが、尺は伸びている |
| `cut_and_concat_batched`（`:270`） | 上のバッチ版。中間バッチも同じ | 同上 |
| `clip_writer` のイントロカード / タグ焼き込み | 個別に mp4 を作り `_combine` で再エンコード連結 | 対象外（§2.5 のとおり累積しない） |

---

## 3. 方式選定

### 3-1.【採用】案 1 — 中間パートの音声を `pcm_s16le` にする

* **原因を直接断つ**。AAC エンコーダ遅延は「AAC にエンコードするから」生じる。
  部品段階で AAC を作らなければ、部品の音声尺は要求尺とサンプル単位で一致する。
* 解析側の対照実験で **境界の段差 0.0ms** を実測済み（Analyze2 §6）。
* 実装は `-c:a` の差し替えと容器の変更だけで、連結・焼き込みのコマンド構成は変わらない。
* **容器を `.mp4` から `.mov` へ変える必要がある**。MP4 マルチプレクサは `pcm_s16le` の
  タグを持たず、`Could not find tag for codec pcm_s16le` で失敗するためである。
  `.mov` は同じ h264 + PCM をそのまま格納でき、concat デマルチプレクサも扱える。
* 代償は**中間ファイルの容量**（48kHz / 16bit / ステレオ = 約 11.5MB 分）。
  部品は連結後すぐ削除される（`renderer.py:122-129` の `finally`）ため、
  ピークは「1 クリップぶんの部品 ＋ 連結後のベース」= 18 分クリップで約 410MB（§9 Q4）。

### 3-2.【採用】案 2 — フレーム量子化は「絶対フレーム番号でタイル化」する

単純に各部品の尺を個別に丸めると、丸め残差が部品数ぶん**ランダムウォークで積もる**。
`model.py:723` のコメントどおり **絶対時刻をフレーム番号へ直してから差を取る**。

```
部品 i の尺 = from_frames( to_frames(部品 i の終端時刻) - to_frames(部品 i の開始時刻) )
```

こうすると部品はモデルの時間軸をフレーム単位で隙間なく敷き詰め、
**全体の誤差は最大でも半フレーム（60fps で 8.3ms）で、部品数に依存しない**。

* モデル（Timeline）は書き換えない。**レンダリング時に尺を決めるだけ**にする。
  こうすれば編集画面の表示・保存済みプロジェクト・FCPXML 出力は一切変わらない。
* 字幕・オーバーレイの時刻はモデルのまま（焼き込みは連結後の 1 本に対して行う）。
  最大 8.3ms の差は残るが、これは**フレーム 1 枚未満**であり知覚できない。

### 3-3.【不採用】案 3 — ベース映像を 1 プロセスの concat フィルタで作る

* 原理的には最も筋が良いが、**入力数の上限と巨大フィルタグラフのハング**という既知の問題がある。
  そのために `cut_and_concat_batched`（`silence_cutter.py:270`）を作った経緯があり、
  今回の clip3 は **81 部品**、通常パイプラインでは 400 区間に達しうる。
* 案 1 で症状が完全に消えることが実測済みである以上、リスクの高い作り替えを選ぶ理由が無い。
* 将来 `_render_base` を作り替える場合の候補としては残す（§11）。

### 3-4.【不採用】案 4 — 焼き込み前に実尺を測って字幕時刻を写す

* 対症療法であり、**部品の実尺を全部 probe する**ぶん遅くなる。
* 字幕（ASS）だけでなく `_composite()` の `overlay=…:enable='between(t,…)'`（`renderer.py:424`）と
  `setpts=PTS+…`（`renderer.py:412`）も同じ写像を通す必要があり、変更点が増える。
* **案 1・2 が使えなかった場合の保険**としてのみ残す（§9 Q3 の結果次第）。

### 3-5.【不採用】案 5 — 既存スイッチ `timeline.render.concat_reencode = true`

* **理屈の上では解消しない。** concat デマルチプレクサは、次のファイルのパケットを
  「前のファイルの尺ぶん」ずらしたタイムスタンプで出力する。再エンコードは
  **そのタイムスタンプ列をそのまま作り直す**だけなので、境界に入った 21.3ms の隙間は残る
  （無音や重複フレームとして固定される）。
* 加えて全長の再エンコードで時間が伸びる。
* Analyze2 §8 Q3 の実測は取れていないが、案 1 が原因そのものを断つため実測は実装の前提にならない。
  **理屈による棄却のまま確定**とし、Phase 0（実測）は省略する（§9 Q3 / §6）。

### 3-6. 修正範囲

**`renderer` のみ**を対象とする（§9 Q2 で確定）。
`renderer` は Timeline 経路の唯一の書き出し口であり、**アーカイブ用切り抜きとクリップ用（通常）
パイプラインの両方が同時に直る**（§2.1 / §9 Q7）。経路を見分ける分岐は作らない。
`silence_cutter` の実カット経路は同じ機構を持つが、字幕も同じ物理軸で作るため実害が出ていない。
共通化できる形（`ffmpeg_runner` に中間用の関数を置く）で実装だけしておき、
**適用は行わない**（Phase 3 は見送り / §9 Q2）。

---

## 4. 設計方針

1. **原因の 2 点（AAC プライミング / フレーム端数）だけを断つ。** 時刻を後から補正する仕組みは足さない。
2. **最終出力の規格は変えない。** 完成品は従来どおり h264 + AAC の mp4。変わるのは中間ファイルだけ。
3. **既存の連結対策を共有し続ける。** `concat_files`（`+genpts` / `avoid_negative_ts` /
   `max_interleave_delta`）はそのまま使う。新しい連結処理は書かない。
4. **モデルを書き換えない。** 量子化はレンダリング時の尺計算に閉じる。保存形式・FCPXML・編集画面は不変。
5. **切り戻せるようにする。** 中間音声コーデックと量子化は `setting.json` の
   `timeline.render` へ持たせ、値を戻せば従来動作へ復帰できる（`docs/claude.md`「ハードコード禁止」）。
6. **再発を検知できるようにする。** 連結後の実尺とモデル尺の差をログへ出す。

---

## 5. 詳細設計

### 5.1 変更ファイル一覧

| ファイル | 変更 |
|---|---|
| `src/modules/ffmpeg_runner.py` | 中間ファイル用のエンコード設定・拡張子を返す関数を追加（変更 1a） |
| `src/timeline/renderer.py` | 部品の容器・音声（1b）／最終化（1c）／フレームタイル化（2）／`-t` の切り捨て（§5.3-1）／実尺照合（3） |
| `src/timeline/builder.py` | `timeline_config()["render"]` に 3 キーを追加 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` に同 3 キーを追加（画面には出さない） |
| `tests/test_renderer_timing.py`（新規） | 尺のタイル化・`-t` 書式・中間エンコード設定・既定値補完の単体テスト |

**変更なし**: `silence_cutter`（§9 Q2 により今回は対象外）/ `concat_processor` / `subtitle_generator` /
`archive/clip_writer` / `archive/timeline_builder` / `model.py` / `project_io.py` / `resolve_export.py`。

### 5.2 変更 1a — 中間ファイル用のエンコード設定（`ffmpeg_runner.py`）

```python
# 中間ファイル (連結前の部品) 用のエンコード設定を返す (20260812 resolve3 §3-1)
# 音声を非圧縮にするのは、AAC のエンコーダ遅延 (1024 サンプル = 21.3ms) が
# 部品ごとに尺へ乗り、concat デマルチプレクサでの連結時に部品数ぶん累積するため。
# 最終出力は焼き込み時に 1 回だけ AAC 化されるので、プライミングも 1 回しか乗らない。
# codec に空文字 / "aac" 等を指定すると従来どおりの挙動へ戻せる。
def build_intermediate_encode_options(ffmpeg_settings, audio_codec=None):
    options = build_encode_options(ffmpeg_settings)
    if not audio_codec:
        return options
    # build_encode_options の末尾 "-c:a <codec>" を差し替える
    return options[:-1] + [audio_codec]
```

```python
# 中間ファイルの拡張子を決める (pcm_s16le は mp4 に格納できないため .mov を使う)
def intermediate_suffix(audio_codec, default=".mp4"):
    return ".mov" if str(audio_codec or "").startswith("pcm_") else default
```

### 5.3 変更 2 — 部品の尺を絶対フレーム番号から決める（`renderer._build_segments`）

`_build_segments()`（`renderer.py:65-77`）に、モデル時刻ではなく**フレーム番号**で
タイル化した尺を持たせる。`timeline.to_frames()` / `from_frames()` は既存（`model.py:724-729`）。

```python
# V1 のクリップ列とギャップから、レンダリング単位のセグメント列を作る
# quantize=True のときは各セグメントの尺を「終端フレーム − 開始フレーム」で決める
# (20260812 resolve3 §3-2)。絶対フレーム番号の差を取るため丸め誤差が累積しない。
def _build_segments(timeline, cfg):
    segments = []
    gap_policy = cfg["render"]["gap_policy"]
    quantize = cfg["render"]["frame_quantize"]
    cursor = 0.0
    cursor_frame = 0          # 直前セグメントの終端 (フレーム番号)
    for clip in timeline.base_clips():
        gap = clip.timeline_start - cursor
        if gap > _MIN_RENDER_SEC and gap_policy == "black":
            end_frame = timeline.to_frames(clip.timeline_start)
            segments.append({
                "kind": "gap",
                "duration": _tile_duration(timeline, cursor_frame, end_frame, gap, quantize),
            })
            cursor_frame = end_frame if quantize else cursor_frame
        start_frame = timeline.to_frames(clip.timeline_start)
        end_frame = timeline.to_frames(clip.timeline_end)
        segments.append({
            "kind": "clip",
            "clip": clip,
            "duration": _tile_duration(
                timeline, start_frame, end_frame, clip.duration, quantize),
        })
        cursor = clip.timeline_end
        cursor_frame = end_frame
    return segments


# セグメントの尺を求める。quantize=False なら従来どおりモデルの秒をそのまま使う。
def _tile_duration(timeline, start_frame, end_frame, raw_duration, quantize):
    if not quantize:
        return raw_duration
    frames = max(end_frame - start_frame, 1)
    return timeline.from_frames(frames)
```

`_render_base()` / `_extract_clip()` / `_render_gap()` は
`segment["clip"].duration` ではなく **`segment["duration"]` を使う**ように統一する
（現状は `renderer.py:101-102` と `:142` の 2 箇所で `clip.duration` を見ている）。
`_extract_clip()` には `duration` を引数で渡す。

> フェード（`_video_filters` の `fade=t=out:st=…`）も同じ `duration` を基準にする。
> `clip.source_in` は量子化しない（±8ms は素材内の位置であり、同期には影響しない）。

#### 5.3-1. `-t` の書式（実装時に判明した追加要件・設計へ反映）

**フレーム量子化だけでは不十分だった。** `-t` へ渡す秒数の**書式**まで決める必要がある。

60fps のフレーム境界は有限小数で表せない（301 フレーム = 5.0166666… 秒）。
現行実装の `f"{duration:.3f}"` はもちろん、`.6f` で四捨五入した `5.016667` でも
**境界より上へ丸まる**。FFmpeg の打ち切り条件は「フレームの PTS < `-t`」という厳密比較のため、
1 マイクロ秒でも超えると**次のフレームまで書き出してしまう**。

実測（本リポジトリの `_extract_clip` をそのまま呼んで計測）:

| `-t` | 映像フレーム数 | 映像尺 | 音声尺 | ファイル尺 |
|---|---:|---:|---:|---:|
| `5.016667`（四捨五入） | **302** | 5.033333 | 5.016667 | 5.033333 |
| `5.016666`（切り捨て） | **301** | 5.016667 | 5.016667 | 5.016667 |

上段では**映像だけが 1 フレーム長い**。concat デマルチプレクサは長い方（＝映像）の尺で
次のファイルを置くため、その 1 フレーム（16.7ms）ぶん音声に穴が空き、**再び累積する**。
実際、切り捨てを入れる前の実測では 12 部品で +50.8ms 残っていた（うち 3 部品が該当）。

対処: **`-t` は必ず切り捨ててマイクロ秒 6 桁で渡す**。専用関数 `_duration_arg()` を置く。

```python
# -t (尺) へ渡す文字列を作る (20260812 resolve3 §5.3-1)
def _duration_arg(duration):
    micro = max(math.floor(float(duration) * 1_000_000), 0)
    return f"{micro // 1_000_000}.{micro % 1_000_000:06d}"
```

* 切り捨て幅は 1 マイクロ秒未満（= 0.048 サンプル）なので、音声のサンプル数は目標値へ丸められ、
  **映像尺と音声尺が完全に一致する**（上表の下段）。
* 割り切れる尺（`5.000000` など）は境界ちょうどで良い。打ち切りが厳密比較のため増えない。
* 適用先は部品生成の `-t` 5 箇所（`_extract_clip` の 3 箇所 / `_render_gap` の 2 箇所）。
  `-ss`（`source_in`）は従来どおり 3 桁のまま（素材内の位置であり累積しない）。

### 5.4 変更 1b — 部品を PCM / `.mov` で書き出す（`renderer._render_base` ほか）

```python
    # 中間パートの音声コーデック (既定 pcm_s16le / resolve3 §3-1)
    audio_codec = cfg["render"]["intermediate_audio_codec"]
    suffix = ffmpeg_runner.intermediate_suffix(audio_codec)
    output_path = context.allocate_intermediate(f"timeline_base{suffix}")
    ...
            part_path = os.path.join(work_dir, f"tl_part_{index:05d}{suffix}")
```

`_extract_clip()` / `_render_gap()` の `build_encode_options(ffmpeg_cfg)` を
`build_intermediate_encode_options(ffmpeg_cfg, audio_codec)` へ置き換える（引数で渡す）。

* **ベース出力も `.mov` になる**点に注意。`timeline_base.mp4` を名指ししている箇所は
  `renderer.py:82` のみで、外部からは `context.current_video_path()` 経由で参照されるため影響は無い
  （grep 済み）。
* 連結（`concat_files`）のコマンドは変更しない。`.mov` + h264 + PCM の `-c copy` 連結は
  デマルチプレクサの前提（全ファイルが同一コーデック）を満たす。

### 5.5 変更 1c — 字幕もオーバーレイも無い場合の最終化（新規・必須）

`_render_overlays_and_subtitles()` は、層が無いとき **ベースをそのまま返す**
（`renderer.py:296-298`）。また `_burn_subtitles_only()` は使用字幕が 0 件のとき同様に返す
（`:331-334`）。このままだと **PCM 音声の `.mov` が最終成果物**になってしまい、
`clip_writer._copy_individual()`（`clip_writer.py:591`）が `.mov` を `.mp4` 名で複製するなど破綻する。

```python
# 焼き込みもオーバーレイも無いとき、中間形式 (PCM/.mov) を最終形式へ直す (resolve3 §5.5)
# 映像は再エンコードせずコピーし、音声だけ設定どおり (既定 aac) へ変換する。
def _finalize_base(context, base_path, cfg, ffmpeg_cfg):
    if not base_path.lower().endswith(".mov"):
        return base_path            # 従来設定 (中間も mp4/AAC) のときは何もしない
    output_path = context.allocate_intermediate("timeline_final.mp4")
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-i", base_path,
        "-c:v", "copy",
        "-c:a", ffmpeg_cfg.get("audio_codec", "aac"),
        "-ar", str(ffmpeg_cfg.get("audio_sample_rate", 48000)), "-ac", "2",
        "-movflags", "+faststart",
        output_path,
    ]
    ffmpeg_runner.execute(
        cmd, total_duration=..., on_progress=context.progress_subcallback("最終化"),
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
    )
    return output_path
```

呼び出し箇所は 2 つ（層が無いとき / 使用字幕が 0 件のとき）。
映像は `-c:v copy` なので、実質「音声だけの変換」であり数秒で終わる。

### 5.6 変更 3 — 連結後の実尺を照合する（`renderer._render_base` 末尾）

```python
    # 出来上がったベースの実尺とモデル尺を突き合わせる (resolve3 §1 の Z)
    # 1 フレームを超える差はタイムスタンプの累積ずれを疑うべき兆候として残す。
    try:
        actual = ffmpeg_runner.probe_duration(output_path, ffmpeg_cfg)
    except Exception:  # noqa: BLE001 (計測失敗で書き出しを止めない)
        actual = None
    if actual is not None:
        tolerance = 1.0 / ffmpeg_runner.get_output_fps(ffmpeg_cfg)
        diff = actual - total
        level = _logger.warning if abs(diff) > tolerance else _logger.info
        level("ベース映像の尺: モデル %.3fs / 実測 %.3fs (差 %+.3fs / %d 部品)",
              total, actual, diff, len(segments))
```

修正前の clip3 ならここで `差 +2.4s` 前後が WARNING として残る。修正後は `±0.02s` 以内に入る。

### 5.7 触らないもの

| 対象 | 理由 |
|---|---|
| `concat_files` / `_concat_demux` | 既知対策の塊。呼び出し方も変えない |
| `clip_writer._combine` / `concat_processor.concat` | concat フィルタ + 再エンコードで累積しない（§2.5） |
| 字幕・オーバーレイの時刻計算 | モデルは正しい（Analyze2 §3-1）。触ると 1 回目の修正を壊す |
| `Timeline` モデル・保存形式・FCPXML | 量子化はレンダリング内に閉じる（§3-2） |
| `ffmpeg.audio_codec`（最終出力） | 完成品は従来どおり AAC |

---

## 6. 実装手順（フェーズ分け）

§9 の回答を受け、**実施するのは Phase 1 と Phase 2 のみ**とする。

| Phase | 内容 | 完了条件 | 実施 |
|---|---|---|---|
| **0** | `concat_reencode=true` の効果を実測し、案 5 の棄却を確定する | — | **省略**（§9 Q3）。案 1 が原因を断つため実測は前提にならない |
| **1** | 変更 1a・1b・1c（PCM 中間 + 最終化） | Analyze2 §7-4 の対照実験で境界の段差が **0.0ms**。字幕なし・オーバーレイなしのクリップでも mp4/AAC が出る | **実施** |
| **2** | 変更 2（フレームタイル化）+ 変更 3（実尺照合） | 実尺とモデル尺の差が **1 フレーム以内**。ログに WARNING が出ない | **実施**（§9 Q5） |
| **3** | `silence_cutter.extract_segment` / `cut_and_concat_seek` へ同じ中間設定を適用 | — | **見送り**（§9 Q2）。共有関数は用意するが呼び出さない |

Phase 1 だけで**症状は消える**（中身のずれが 0 になる）。Phase 2 は尺の一致と再発検知のための仕上げ。
Phase 1・2 はどちらも `renderer` に閉じるため、まとめて 1 度に実装する。

---

## 7. setting.json 定義（追加分）

```jsonc
"timeline": {
    "render": {
        // 既存
        "gap_policy": "black",
        "extract_mode": "seek",
        "concat_reencode": false,
        "overlay_enabled": true,

        // 追加 (resolve3 §3-1 / §3-2)
        // 連結前の中間パートの音声コーデック。AAC のエンコーダ遅延 (1024 サンプル = 21.3ms) が
        // 部品ごとに積み上がるのを避けるため既定は非圧縮。"aac" にすると従来動作へ戻る。
        "intermediate_audio_codec": "pcm_s16le",
        // 中間パートの容器。pcm_s16le は mp4 に格納できないため .mov を使う。
        "intermediate_container": ".mov",
        // 部品の尺をフレーム境界へタイル化する (絶対フレーム番号の差で決める)。
        // false にすると従来どおりモデルの秒をそのまま -t へ渡す。
        "frame_quantize": true
    }
}
```

* 既定値は `builder.timeline_config()`（`builder.py:246-251` の `"render"` ブロック）と
  `settings_window.DEFAULT_SETTINGS` の**両方**へ入れる。キーが無い既存 `setting.json` でも動く。
* **設定画面には出さない**。`timeline.render.*` は既に全キーがファイル専用である（現状も UI 無し）。
* `concat_reencode` は据え置き（意味を変えない）。

---

## 8. 互換性・非破壊の担保

| 観点 | 担保 |
|---|---|
| 最終出力の規格 | h264 + AAC の mp4 のまま。解像度・fps・CRF・音声レートいずれも不変 |
| 保存形式（`*.timeline.json`） | スキーマ・値ともに変更なし。既存プロジェクトはそのまま開ける |
| DaVinci Resolve 出力（FCPXML） | **完全に不変**。元 VOD を直接参照する経路で、本修正を通らない |
| 編集画面のプレビュー | **完全に不変**（Analyze2 §3-6 のとおり別経路） |
| 1 回目の修正（resolve2） | 触らない。字幕の理想軸はそのまま。**本修正は書き出し側を理想軸へ合わせる** |
| 従来画面経路（`timeline_review=false`） | **完全に不変**。`silence_cutter` を触らないため（§9 Q2） |
| 切り戻し | `intermediate_audio_codec` を `"aac"` に、`frame_quantize` を `false` にすれば従来動作 |
| 一時領域 | 中間パートが PCM になるぶん増える（§9 Q4）。部品は連結後すぐ削除する現行動作のまま |
| 既存の出力動画 | 作り直さない限り直らない（§9 Q6） |

---

## 9. 確認事項（Analyze2 §8 への回答 / 利用者確定済み）

2026-08-19 に利用者から回答を得た。**本章は方針案ではなく確定した決定**であり、
以下の内容で実装する（Q3 のみ回答が無いため §3-5 の判断を維持する）。

| No | 論点（Analyze2） | 回答 | 本書の決定 |
|---|---|---|---|
| **Q1** | 「テロップが重なる／クリックで飛ぶ」は今回も起きたか | **今回は起きていない** | **対象外で確定**。1 回目の対策（resolve2）で解消済みと判断する。SRT 369 件の重なり 0 件（Analyze2 §4）とも整合する。本書は「書き出し軸の累積ずれ」のみを扱う |
| **Q2** | 修正範囲は `renderer` だけか、`silence_cutter` にも及ぼすか | **まず `renderer` だけ** | **Phase 1・2 のみ実施**。`silence_cutter` の実カット経路（`extract_segment` / `cut_and_concat_seek` / `cut_and_concat_batched`）は**今回は触らない**。ただし中間用エンコード設定は `ffmpeg_runner` の共有関数として置き、後から適用できる形にする。**Phase 3 は見送り**（§6） |
| **Q3** | `concat_reencode=true` で解消するか | （回答なし） | **§3-5 の理屈による棄却を維持する**。案 1 が原因そのものを断つため、案 5 の実測は実装の前提にならない。**Phase 0 は省略**し、必要になった時点で実測する |
| **Q4** | PCM 中間による一時容量の増加を許容するか | **許容する** | **`intermediate_audio_codec` の既定を `pcm_s16le` で確定**。18 分クリップで部品計 約 207MB ＋ ベース 約 207MB（ピーク約 410MB）。部品は連結直後に削除する現行動作のまま。`flac` 案は採らない |
| **Q5** | フレーム量子化を入れるか。編集点が最大 1 フレーム動く | **入れる** | **`frame_quantize` の既定を `true` で確定**（Phase 2）。**モデルは書き換えず**レンダリング時の尺計算に閉じるため、編集画面上の編集点・保存済みプロジェクト・FCPXML は動かない。動くのは出力動画の各部品の切れ目のみ（最大半フレーム = 8.3ms） |
| **Q6** | 既に出力済みの動画の扱い | **自動補正しない** | **既存の出力ファイルには一切手を触れない**。作り直しは利用者判断。移行処理・再出力の誘導も入れない |
| **Q7** | クリップ用（通常）パイプラインも直してよいか | **クリップ用も同時に直す** | **同時に直す**。`pipeline_runner.py:189` と `clip_writer.py:574` は同じ `renderer.render()` を通るため、renderer の修正だけで両経路へ同時に効く（§2.1）。**設定でアーカイブ用だけに限定するような分岐は作らない**。無音カット区間が 400 本並ぶ長尺ほど効果が大きい |
| **Q8**（新規） | ベース中間が `.mov` になることで、外部ツール連携や中間ファイルを直接参照する運用に影響はないか | **影響なし** | 参照箇所は `renderer.py:82` のみ（grep 済み）。他は `context.current_video_path()` 経由。ただし最終成果物は `.mp4` である必要があるため（`output_writer.finalize` は `.mp4` 名へ move し、`clip_writer._copy_individual` は `.mp4` 名へコピーする）、**変更 1c（`_finalize_base`）は必須**とする |

## 10. テスト計画

### 10-1. 対照実験（Phase 1・2 の合格判定 / 実施済み・合格）

`renderer.render()` を実 FFmpeg で回して計測した（2026-08-19）。
5 秒ごとにビープを持つ 70 秒の素材を、**フレーム境界に乗らない 5.0041 秒のクリップ 12 本**
（隙間なし）へ切り、`silencedetect` でビープの立ち上がりを拾って素材と突き合わせた。
`before` は設定を従来値（`intermediate_audio_codec="aac"` / `frame_quantize=false`）へ
戻したもので、**同じコードの切り戻し経路**である。

| ビープ# | 素材 | 修正前 | ずれ | 修正後 | ずれ |
|---:|---:|---:|---:|---:|---:|
| 0 | 5.000 | 5.021 | **+21 ms** | 5.000 | **±0 ms** |
| 1 | 10.000 | 10.055 | **+55 ms** | 9.996 | −4 ms |
| 2 | 15.000 | 15.089 | **+89 ms** | 14.992 | −8 ms |
| 3 | 20.000 | 20.122 | **+122 ms** | 20.005 | +5 ms |
| 5 | 30.000 | 30.189 | **+189 ms** | 29.996 | −4 ms |
| 8 | 45.000 | 45.290 | **+290 ms** | 45.000 | ±0 ms |
| 11 | 60.000 | 60.391 | **+391 ms** | 60.005 | +5 ms |

* 修正前は **1 境界あたり +33.4ms で線形に増える**。clip3 の 81 部品へ引き延ばすと約 2.7 秒で、
  実際の症状（約 2.4 秒先行）と一致する。
* 修正後は **±8ms（半フレーム）の範囲で振れるだけで、増えていかない**。設計どおり。

全体尺（モデル 60.0492 秒）:

| | 実尺 | 差 |
|---|---:|---:|
| 修正前 | 60.4523s | **+0.4031s（+24.19 frames）** |
| 修正後 | 60.0500s | **+0.0008s（+0.05 frames）** |

修正後の +0.8ms は「モデル尺 60.0492 秒をフレーム量子化して 60.05 秒にした」ぶんで、
半フレーム以内に収まっている（§3-2 の想定どおり）。

部品ごとの内訳（`_duration_arg` 適用後）は、**全 12 部品で
要求フレーム数 = 映像フレーム数、かつ映像尺 = 音声尺**であり、
連結後も `video 60.050000 / audio 60.050000` と完全に一致した（ずれ 0.000s）。

### 10-2. 実機確認（Phase 2 の合格判定）

1. VOD 2847676012 を「アーカイブ用切り抜き」で再実行する（同じ `setting.json`）。
2. ログの「ベース映像の尺: モデル … / 実測 …」を確認する。**全クリップで差が 1 フレーム以内**。
3. clip3（18 分）の終盤を再生し、**テロップが発話と合っている**ことを確認する
   （修正前は約 2.4 秒先行）。
4. clip4 の先頭でも合っていることを確認する（クリップ間のリセットが消えている）。
5. 出力全体の尺がモデル（約 829.4 秒）と一致することを確認する（修正前は +2.12 秒）。

### 10-3. 単体テスト（新規 `tests/test_renderer_timing.py`）

| ケース | 期待 |
|---|---|
| `_build_segments` にフレーム端数を持つクリップ列を与える | 各セグメントの尺が `1/fps` の整数倍。**尺の総和 = 最終クリップの終端をフレーム量子化した値**（誤差が累積しない） |
| `frame_quantize=false` | 従来どおりモデルの秒がそのまま返る |
| ギャップを含む列 | ギャップも同じタイル化に従い、隙間・重なりが生じない |
| `build_intermediate_encode_options(cfg, "pcm_s16le")` | `-c:a pcm_s16le`。他のオプション（`-c:v` / `-preset` / `-crf`）は `build_encode_options` と同一 |
| `build_intermediate_encode_options(cfg, None)` | `build_encode_options` と完全に一致（切り戻し経路） |
| `intermediate_suffix("pcm_s16le")` / `("aac")` | `".mov"` / `".mp4"` |

### 10-4. 既存テストの回帰

`tests/test_timeline_model.py` / `test_timeline_project_io.py` / `test_archive_timeline.py` /
`test_resolve_export.py` / `test_fcpxml_builder.py` は**モデルと出力仕様に触れない**ため影響を受けない。
実行して緑であることを確認する。

---

## 11. 将来拡張（本書では実装しない）

* **`_render_base` の 1 プロセス化**（案 3）。部品ファイルそのものを無くせば累積は原理的に消える。
  入力数の上限とフィルタグラフのハング（`cut_and_concat_batched` を作った経緯）を
  バッチ分割で回避できる見通しが立ってから着手する。
* **`silence_cutter` の中間 PCM 化**（§9 Q2 で今回は見送った Phase 3）。`build_intermediate_encode_options`
  はそのまま使えるため、`extract_segment` / `cut_and_concat_seek` の呼び出しを差し替えるだけで済む。
  従来画面経路の尺も一致するようになる。
* **書き出し前セルフチェックの一般化**。変更 3 の照合を「部品ごとの実尺 vs 要求尺」まで広げると、
  どの部品で崩れたかまで特定できる。常時実行するには probe の回数が多いため、
  デバッグ設定として持たせるのが現実的。

---

## 12. 実装記録（2026-08-19 / Phase 1・2 適用済み）

§9 の回答に沿って実装した。Phase 0・Phase 3 は実施していない。

### 12-1. 実際の変更

| ファイル | 追加・変更した関数 |
|---|---|
| `src/modules/ffmpeg_runner.py` | `build_intermediate_encode_options()` / `intermediate_suffix()` を追加。`build_encode_options()` は**未変更**（最終出力は従来どおり AAC） |
| `src/timeline/renderer.py` | `_build_segments()` にフレームタイル化を追加、`_tile_duration()` / `_duration_arg()` / `_verify_base_duration()` / `_finalize_base()` を新設。`_render_base()` / `_extract_clip()` / `_render_gap()` / `_video_filters()` が新しい尺と中間コーデックを使うように変更 |
| `src/timeline/builder.py` | `timeline_config()["render"]` へ `intermediate_audio_codec` / `intermediate_container` / `frame_quantize` を追加 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["timeline"]["render"]` へ同 3 キーを追加 |
| `tests/test_renderer_timing.py` | 新規（14 ケース） |

`src/settings/setting.json` は**書き換えていない**。既存キーが欠けていても
`settings_window._fill_nested_defaults()` が読み込み時に既定値で補完するため、
利用者のファイルはそのままで新しい既定値が効く（キーは次回保存時にファイルへ現れる）。

### 12-2. 設計からの差分

| 項目 | 設計 | 実装 | 理由 |
|---|---|---|---|
| `-t` の書式 | 記載なし（`:.3f` のまま） | `_duration_arg()` で切り捨て 6 桁（§5.3-1） | **実測で判明**。四捨五入だと映像が 1 フレーム余分に出て累積が残った |
| `_finalize_base` の呼び出し位置 | `_render_overlays_and_subtitles` 内の 2 箇所 | `render()` から結果に対して 1 回 | 早期 return は 3 箇所（層なし / 使用字幕 0 件 / 有効オーバーレイ 0 件）ある。拡張子が `.mp4` なら何もしない実装にして、1 箇所で全経路をまかなう |
| `_finalize_base` の `-movflags +faststart` | 付ける | **付けない** | 焼き込み経路（`burn_subtitle`）が付けていない。ここだけ付けると成果物の形が経路で変わるため揃えた |
| 実尺照合 | `_render_base` 末尾へ直書き | `_verify_base_duration()` へ切り出し | `finally`（部品削除）より前に置く必要があり、関数化した方が読みやすいため |

### 12-3. 確認結果

* 単体テスト: `python -m unittest discover -s tests` → **346 件すべて成功**（新規 14 件を含む）。
* 実 FFmpeg での対照実験: §10-1 のとおり合格（累積ずれ 391ms → ±8ms、全体尺の差 +403ms → +0.8ms）。
* §10-2 の実機確認（VOD 2847676012 の再実行）は**未実施**。実行時はログの
  「ベース映像の尺: モデル … / 実測 … (差 …)」が WARNING になっていないことを確認する。

### 12-4. 切り戻し手順

`src/settings/setting.json` の `timeline.render` を次のように書き換えると従来動作へ戻る
（コードの変更は不要）。

```jsonc
"intermediate_audio_codec": "aac",   // 中間も AAC (従来)
"frame_quantize": false              // 尺はモデルの秒をそのまま使う (従来)
```
