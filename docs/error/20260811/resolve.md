# アーカイブ用切り抜きでテロップと音声がズレる不具合 調査・修正設計書（A案）

対象現象: 「アーカイブ用」の切り抜きで、テロップ（字幕）と音声が Timeline 画面上でもズレる。
主対象ソース: `src/archive/clip_writer.py`（`_prepare_clips` / `_silence_cut` / `_transcribe`）
関連ソース: `src/archive/timeline_builder.py`（`build_archive_timeline`）／
`src/modules/silence_cutter.py`（`extract_segment` / `cut_and_concat_seek` / `detect_edit_points` / `extract_audio_segments`）／
`src/modules/subtitle_generator.py`（`recognize_for_timeline`）／`src/pipeline/pipeline_runner.py`（`_run_timeline`）
本書は `docs/CLAUDE.md` 方針に準拠した**修正設計書**であり、実装は承認後に行う。

---

## 1. 結論（先に要点）

**アーカイブ用だけ「字幕（items）の時間軸」と「Timeline が V1/A1 を並べる時間軸」が別物になっている。**

* クリップ用（`pipeline_runner._run_timeline`）は**実カットせず**、`keep_segments` の音声だけを連結した
  `asr_audio.m4a` に対して音声認識する。得られる時刻は Timeline の軸と一致する。
* アーカイブ用（`clip_writer._prepare_clips`）は**実カットした `silence_cut.mp4` に対して認識**している。
  実カット後ファイルの尺は `sum(end - start)` と一致せず、**区間ごとに +21〜36ms 長くなり累積**する。
* Timeline 側（`build_archive_timeline`）は `end - start` の理想値で V1 を積み上げるため、
  **クリップの後方へ行くほど字幕が遅れる**。

修正は **「アーカイブ用も実カットをやめ、クリップ用と同じ 2 段構え（編集点検出 → 区間音声のみで認識）に揃える」**（A案）。
Timeline 経路では実カット後ファイル（`prepared_path`）は音声認識と寸法判定にしか使われておらず、
レンダリングは `normalized_path` を参照しているため、**実カットそのものが不要**である。

---

## 2. 原因の特定

### 2-1. クリップ用（正しく合っている側）の作り

`src/pipeline/pipeline_runner.py:163-179`

```python
# ① 無音検出 (実カットせず編集点だけを求める / §7.2)
keep_segments, edit_meta = silence_cutter.detect_edit_points(context)
# ② 音声認識 (残す区間の音声のみに対して実行 / §7.3)
items, _eff_cfg = subtitle_generator.recognize_for_timeline(context, keep_segments)
# ③ Timeline 構築
timeline = timeline_builder.build(
    context.input_path, context.current_video_path(), keep_segments, items, ...)
```

`recognize_for_timeline`（`src/modules/subtitle_generator.py:1048-1112`）は
`silence_cutter.extract_audio_segments`（同 `silence_cutter.py:539`）で
**映像を捨てた（`-vn`）音声のみ**を区間ごとに切り出して連結し、それを認識する。
`timeline_builder.build`（`src/timeline/builder.py:326-339`）は同じ `keep_segments` を
`end - start` で積み上げるため、**認識時刻と V1 の位置が同一の軸**になる。

### 2-2. アーカイブ用（ズレている側）の作り

`src/archive/clip_writer.py:385-401`

```python
prepared_path, keep_segments = _silence_cut(normalized, clip_settings, clip_dir)  # ← 実カットする
profile = output_profile.resolve_output_profile(prepared_path, settings)
eff_cfg = subtitle_generator.build_effective_subtitle_cfg(subtitle_cfg, vertical_cfg, profile)
items    = _transcribe(prepared_path, settings, eff_cfg)   # ← 実カット後 mp4 の時間軸で認識
```

一方 `src/archive/timeline_builder.py:87-103` は理想値で並べる。

```python
for seg_index, (start, end) in enumerate(segments):
    duration = float(end) - float(start)          # ← keep_segments の理想値
    video_track.clips.append(Clip(clip_id, media.id, cursor, duration, ...))
    cursor += duration
```

字幕は `src/archive/timeline_builder.py:161-172` でそのまま載せる。

```python
subtitle_track.clips.append(SubtitleClip(
    timeline.next_id("s"),
    timeline_start=start + offset,   # ← start は「実カット後 mp4」の時刻
    ...))
```

**「実カット後 mp4 の時刻」を「理想値で積み上げた軸」へそのまま置いている**のがズレの正体。

### 2-3. なぜ実カット後 mp4 の尺が理想値と一致しないか

`setting.json` は `silence_cut.extract_mode = "seek"` のため
`cut_and_concat_seek()` → `extract_segment()`（`src/modules/silence_cutter.py:175-218`）が動く。

```python
cmd = [ffmpeg, "-y", "-hide_banner", "-ss", f"{start:.3f}", "-i", input_path,
       "-t", f"{seg_duration:.3f}"]
...
cmd += [*ffmpeg_runner.build_encode_options(ffmpeg_settings),
        "-fps_mode", "cfr", "-r", f"{fps}", "-avoid_negative_ts", "make_zero", seg_path]
```

* `-fps_mode cfr -r 60` … 区間長がフレーム境界（1/60 秒）へ量子化される
* `-c:a aac` … 区間ごとに AAC のプライミング（1024 サンプル = 21.3ms @48kHz）が付く
* `_concat_demux(..., reencode=False)` … `-c copy` 連結なので、**各区間の余剰がそのまま累積**する

### 2-4. 実測（本プロジェクトと同じコマンド形で検証）

60fps・48kHz の 30 秒素材に対し、フレーム境界に乗らない 10 区間（理想合計 **24.2360s**）で検証した。

| 経路 | 生成物 | 実尺 | 理想との差 |
|---|---|---|---|
| アーカイブ用 | `extract_segment` × 10 + concat copy | **24.5270s** | **+0.2910s（累積）** |
| クリップ用 | `extract_audio_segments` × 10 + concat copy | 24.2573s | +0.0213s（先頭 1 回のみ・累積しない） |

区間ごとの実測（要求 → 実尺）:

```
req=2.566 → 2.5877   req=1.615 → 1.6377   req=2.736 → 2.7710   req=2.448 → 2.4710
req=2.885 → 2.9210   req=1.839 → 1.8710   req=2.594 → 2.6210   req=2.284 → 2.3210
req=2.544 → 2.5710   req=2.725 → 2.7544
```

**常に正方向（長くなる方向）**で、区間あたり平均 +29ms。
クリップ内に無音カットが 20 箇所あれば末尾で **0.6 秒前後**の遅れになる。
「クリップ先頭は合っていて、後ろへ行くほどズレが広がる」という症状と一致する。

なお連結後ファイルは `video start_time=0.021 / audio start_time=0.000` となっており、
実カット後 mp4 の中でも映像と音声がわずかにずれている。

### 2-5. 波及範囲

| 箇所 | 影響 |
|---|---|
| Timeline プレビュー | 字幕が後方へ行くほど遅れる（本件の主症状） |
| 書き出し | `_render_clips`（`clip_writer.py:509-512`）は Timeline をそのままレンダリングするため**同じ量ずれる** |
| `origin.source_start` | `_append_subtitles` の `TimeMap.to_source()`（`timeline_builder.py:147-160`）がズレた時刻を理想軸として逆算するため、記録される素材内時刻も同じだけ狂う |
| Resolve 出力 | `to_export_entry` は Timeline の字幕時刻をそのまま使うため同じ量ずれる |

---

## 3. 修正方針の比較と選定

| 案 | 内容 | 精度 | 副次効果 | 判定 |
|---|---|---|---|---|
| **A案** | アーカイブ用も実カットをやめ、`detect_edit_points` + `recognize_for_timeline` に揃える | **クリップ用と同一（誤差は先頭 21ms の定数のみ）** | 実カット再エンコードが丸ごと不要になり prepare が大幅短縮 | **採用** |
| B案 | 実カット後ファイルの実尺で補正 TimeMap を作り、認識時刻を理想軸へ写像する | 区間境界に残差が残る（区間ごとの誤差は非線形） | 実カットのコストは残る | 不採用 |
| C案 | `extract_segment` で区間端をフレーム境界へ丸め、`concat_reencode=true` にする | 誤差は減るが 0 にはできない | 再エンコード 2 回で更に遅くなる。クリップ用パイプラインにも影響 | 不採用 |

A案の成立根拠（調査済み）:

* Timeline 経路のレンダリングは `PipelineContext(input_path=prepared["normalized_path"])` +
  `renderer.render(sub_timeline, context)`（`clip_writer.py:509-512`）であり、`prepared_path` を見ていない。
* `prepared_path` の参照箇所は以下 3 つだけで、いずれも **Timeline 経路では通らない**。
  * `_burn_one`（`clip_writer.py:436`）… 従来画面（`timeline_review=false`）専用
  * `archive_result_window._on_row_changed`（`archive_result_window.py:191-192`）… 従来画面のプレビュー
  * `output_profile.resolve_output_profile`（`clip_writer.py:386`）… 寸法しか見ないため `normalized` で代替可能
* 画面の分岐（`ArchiveResultBridge._on_requested` / `archive_result_window.py:296-301`）は
  `timeline is not None` で決まり、これは `config.use_timeline_review(settings)` と一致する。
  **Timeline 画面から従来画面へフォールバックする経路は存在しない**ため、Timeline 経路で
  実カットを省いても従来画面が素材を失うことはない。

---

## 4. 修正設計

### 4-1. 全体像

```
【Before / Timeline 経路】
 VOD ─_cut_region→ raw.mp4 ─normalize→ normalized.mp4
                                          ├─ silence_cutter.run() ─→ silence_cut.mp4 ─ASR→ items  ★別軸
                                          │        └→ keep_segments
                                          └─ build_archive_timeline(keep_segments で V1 を積む)   ★理想軸
                                                      ↑ items をそのまま載せる → ズレる

【After / Timeline 経路】
 VOD ─_cut_region→ raw.mp4 ─normalize→ normalized.mp4
                                          ├─ detect_edit_points()        → keep_segments   （実カットしない）
                                          ├─ recognize_for_timeline()    → items           （区間音声のみ・同一軸）
                                          └─ build_archive_timeline(keep_segments で V1 を積む)
                                                      ↑ 同じ軸なのでズレない

【従来経路 (timeline_review=false)】 … 一切変更しない
```

### 4-2. 変更点 1: `_prepare_clips` を経路で分岐する

**ファイル**: `src/archive/clip_writer.py`

引数に `use_timeline` を追加し、Timeline 経路と従来経路で準備方法を分ける。
従来経路のコードは順序も含めて現状のまま残す（`docs/CLAUDE.md`「既存実装を破壊しない」）。

```python
# 全クリップを prepare する (切り出し→編集点検出/無音カット→文字起こし)。ダイアログは出さない。
# use_timeline : True = Timeline 経路。実カットせず編集点だけを求め、区間音声のみで認識する
#                (docs/error/20260811/resolve.md §4-2)。
#                False = 従来画面経路。これまでどおり実カットしてから認識する。
# 戻り値: [{"index","start","end","score","prepared_path","profile","eff_cfg","items",
#           "keep_segments","normalized_path","normalized_duration"}]
def _prepare_clips(input_path, settings, clip_settings, used, ffmpeg_cfg, workdir,
                   progress_cb, use_timeline=False):
    ...
    for pos, clip in enumerate(used, 1):
        # (ここまでは現状と同じ: clip_dir 作成 → _cut_region → normalize_file → _apply_volume_analysis)

        if use_timeline:
            # Timeline 経路: 実カットしない。字幕の時間軸を Timeline の軸へ一致させる (§4-3)
            if progress_cb:
                progress_cb((pos - 1) / total * 0.55,
                            f"クリップ {pos}/{total} を準備中…（編集点検出・文字起こし）")
            # 出力プロファイルは寸法だけを見るため、実カット前の normalized で判定できる
            profile = output_profile.resolve_output_profile(normalized, settings)
            eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
                subtitle_cfg, vertical_cfg, profile)
            prepared_path = ""          # 実カット後ファイルは作らない (従来画面専用のため)
            keep_segments, items, normalized_duration = _prepare_edit_points(
                normalized, clip_settings, clip_dir, profile)
        else:
            # 従来画面経路: 現状のまま (実カット後クリップをプレビュー・焼き込みに使う)
            if progress_cb:
                progress_cb((pos - 1) / total * 0.55,
                            f"クリップ {pos}/{total} を準備中…（文字起こし）")
            prepared_path, keep_segments = _silence_cut(normalized, clip_settings, clip_dir)
            profile = output_profile.resolve_output_profile(prepared_path, settings)
            eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
                subtitle_cfg, vertical_cfg, profile)
            items = _transcribe(prepared_path, settings, eff_cfg)
            normalized_duration = ffmpeg_runner.probe_duration(normalized, ffmpeg_cfg)

        prepared.append({
            "index": clip["index"], "start": clip["start"], "end": clip["end"],
            "score": clip.get("score", 0.0),
            # Timeline 経路では空文字。参照するのは従来画面 (_burn_one / 結果画面プレビュー) だけ
            "prepared_path": prepared_path, "profile": profile,
            "eff_cfg": eff_cfg, "items": items,
            "keep_segments": keep_segments,
            "normalized_path": normalized,
            "normalized_duration": normalized_duration,
        })
```

補足:

* `normalized_duration` は Timeline 経路では `detect_edit_points` が返す
  `meta["source_duration_sec"]`（= `probe_duration(normalized)`）を流用する。
  **`ffprobe` の重複呼び出しが 1 回減る**。従来経路は現状どおり `probe_duration` を呼ぶ。
* プロファイル判定元を `prepared_path` → `normalized` へ変えるのは Timeline 経路のみ。
  `resolve_output_profile` は `probe_dimensions` しか見ず（`src/modules/output_profile.py:48-74`）、
  `extract_segment` はスケーリングしないため寸法は同値であり、判定結果は変わらない。

### 4-3. 変更点 2: 新規ヘルパー `_prepare_edit_points`

**ファイル**: `src/archive/clip_writer.py`（`_silence_cut` の直後に追加）

クリップ用パイプライン `_run_timeline` と**同じ関数を同じ順序で**呼ぶ。
ここが「クリップ用と同じ仕上がりになること」の保証点になる。

```python
# Timeline 経路の準備: 実カットせず編集点と字幕を得る (docs/error/20260811/resolve.md §4-3)
#
# クリップ用 (pipeline_runner._run_timeline) と同じ 2 段構えにすることで、
# 字幕の時刻を「残す区間を詰めた理想の軸」= build_archive_timeline が V1/A1 を並べる軸
# と一致させる。実カット後ファイルに対して認識すると、区間ごとの CFR 量子化と
# AAC プライミングが累積して後方ほど字幕が遅れる (§2-3)。
#
# 戻り値: (keep_segments, items, 素材尺)
#   keep_segments : normalized 相対の残す区間。無音カット無効時は全長 1 区間
#   items         : [{"start","end","text","use","role","font","font_size"}]
def _prepare_edit_points(normalized, clip_settings, clip_dir, profile):
    context = PipelineContext(input_path=normalized, settings=clip_settings,
                              working_dir=clip_dir)
    # 縦動画のとき字幕設定を縦用へ切り替えるため、認識前にプロファイルを渡す
    context.output_profile = profile
    keep_segments, meta = silence_cutter.detect_edit_points(context)
    items, _eff_cfg = subtitle_generator.recognize_for_timeline(context, keep_segments)
    kept = sum(float(e) - float(s) for s, e in keep_segments)
    _logger.info(
        "編集点準備: 残す区間 %d 件 / 想定尺 %.2fs (素材 %.2fs)",
        len(keep_segments), kept, float(meta.get("source_duration_sec") or 0.0),
    )
    return keep_segments, items, float(meta.get("source_duration_sec") or 0.0)
```

* `context.cleanup()` は**呼ばない**。中間物は `clip_dir`（= `workdir` 配下）にあり、
  `write_clips` の `TemporaryDirectory` が最後にまとめて消すため（`_render_clips` の
  `PipelineContext` と同じ扱い）。
* `detect_edit_points` は `context.settings["volume_analysis"]["last_cut_db"]` を読む。
  直前の `_apply_volume_analysis` が同じ `clip_settings` を書き換えているため、
  **クリップごとに測定した閾値がそのまま使われる**（現状の挙動と同じ）。
* `import` 追加: `from ..modules import silence_cutter`（`_silence_cut` 用に既に import 済みなら不要）。

### 4-4. 変更点 3: `write_clips` からフラグを渡す

**ファイル**: `src/archive/clip_writer.py:578-592`

`use_timeline_review` の判定を prepare より前に一度だけ行い、prepare と
`build_archive_timeline` の両方で同じ値を使う（判定の二重化を避ける）。

```python
        clip_settings = _build_clip_settings(settings, workdir, clip_pipe)
        # Timeline 経路かどうかを先に確定する (prepare の作り方が変わるため / §4-4)
        use_timeline = config.use_timeline_review(settings)

        # ① prepare: 全クリップを 切り出し→編集点検出(または無音カット)→文字起こし
        prepared = _prepare_clips(
            input_path, settings, clip_settings, used, ffmpeg_cfg, workdir, progress_cb,
            use_timeline=use_timeline)
        if not prepared:
            _logger.info("準備できたクリップが0件")
            return []

        timeline = None
        if use_timeline:
            timeline = archive_timeline.build_archive_timeline(
                prepared, clip_settings, source_path=input_path)
```

以降（②一括レビュー / ③burn / 結合）は**無変更**。

### 4-5. 変更点 4: コメントの修正

**ファイル**: `src/archive/timeline_builder.py:143-145`

ロジックは変更しないが、items の時間軸の由来が変わるため説明を実態に合わせる。

```python
# 字幕 items を字幕トラックへ載せる
# items の時刻は「残す区間を詰めた後」の時間軸 (clip_writer._prepare_edit_points が
# 区間音声のみを連結して認識するため、V1 の積み上げと同一の軸になる)。
# クリップの開始位置ぶんだけ offset を一律で加える。
# 由来の素材内時刻は TimeMap で逆算して origin へ残す (クリップ用と同じ扱い)。
```

同 `:50-54` の `_prepare_clips` 戻り値の説明にも `prepared_path` が
Timeline 経路では空である旨を追記する。

### 4-6. 変更しないもの

* `src/modules/silence_cutter.py` … `extract_segment` / `cut_and_concat_seek` は従来経路と
  クリップ用パイプラインが使い続けるため一切変更しない。
* `src/archive/timeline_builder.py` の構築・分割・逆変換ロジック（コメントのみ変更）。
* `src/gui/timeline/archive_timeline_dialog.py`、`src/gui/archive_result_window.py`。
* `src/settings/setting.json` … **新規設定項目なし**。既存の
  `archive.clip_pipeline.timeline_review` / `silence_cut.*` をそのまま使う。

---

## 5. setting.json 定義

**追加・変更なし。** 本修正で参照する既存キーは以下のとおり。

| キー | 既定 | 本修正での役割 |
|---|---|---|
| `archive.clip_pipeline.timeline_review` | `true` | true のとき実カットを省く新経路を通る |
| `archive.clip_pipeline.silence_cut` | `true` | `clip_settings.silence_cut.enabled` へ反映。false なら `detect_edit_points` が全長 1 区間を返す |
| `silence_cut.enabled` | `true` | 同上 |
| `volume_analysis.last_cut_db` | 実測値 | `detect_edit_points` の無音判定閾値（クリップごとに `_apply_volume_analysis` が更新） |
| `volume_analysis.cut_min_silence_sec` | `0.6` | 最小無音時間 |
| `subtitle.enabled` / `subtitle.engine` | — | `recognize_for_timeline` の認識可否・エンジン選択 |
| `silence_cut.extract_mode` / `batch_size` / `concat_reencode` | — | Timeline 経路では**不使用になる**（従来経路とクリップ用パイプラインでは引き続き有効） |

---

## 6. エラー処理方針

現行の「1 クリップの失敗で全体を止めない」方針を維持する。

| 事象 | 挙動 | 根拠 |
|---|---|---|
| 無音検出で有音区間 0 件 | `detect_edit_points` が全長 1 区間へフォールバックして続行 | `silence_cutter.py:510-513`（既存） |
| 無音カット無効（`silence_cut.enabled=false`） | 全長 1 区間を返す。`_segments_of` はこれをクリップ全長として扱う | `silence_cutter.py:501-505` |
| 認識用音声の抽出失敗 | 空字幕で続行（クリップは失わない） | `subtitle_generator.py:1075-1077`（既存） |
| 音声認識の失敗・エンジン無効 | 空字幕で続行 | `subtitle_generator.py:1063-1066, 1087-1089`（既存） |
| `probe_duration` 失敗 | `detect_edit_points` 内で `FFmpegError` が送出される。現行 `_silence_cut` も同様に例外で止まるため**挙動は変わらない** | — |

新規の例外捕捉は追加しない（既存関数が内部で握り潰す設計になっているため）。

### 6-1. `keep_segments` の値域変化に関する注意

現行の Timeline 経路では、無音カット無効時 `prepared["keep_segments"]` が `None` になり、
`_segments_of`（`timeline_builder.py:135-140`）が `normalized_duration` へフォールバックしていた。
A案では `detect_edit_points` が常に `[(0.0, total_duration)]` を返すため `None` にならない。

* `_segments_of` はどちらでも同じ 1 区間を作る（`total_duration` = `probe_duration(normalized)`）→ **影響なし**。
* `resolve_export` の「`keep_segments` が None なら全長」分岐（`resolve_export.py:419, 587`）は、
  Timeline 経路では `to_export_entry` が Timeline のクリップから `keep_segments` を組み直すため通らない
  （`timeline_builder.py:339-346`）→ **影響なし**。
* 従来経路は分岐が変わらないため `None` のまま → **影響なし**。

---

## 7. ログ出力方針

既存のログ粒度に合わせ、原因追跡に必要な数値を残す。

| 出力元 | レベル | 内容 |
|---|---|---|
| `silence_cutter.detect_edit_points`（既存） | INFO | `編集点検出: 無音 N 件 / 残す区間 M 件 (閾値=…dB, 最小無音=…s, 総尺=…s)` |
| `subtitle_generator.recognize_for_timeline`（既存） | INFO | `音声認識完了 (Timeline 用 N 件)` |
| `_prepare_edit_points`（**新規**） | INFO | `編集点準備: 残す区間 N 件 / 想定尺 X.XXs (素材 Y.YYs)` |
| `_prepare_clips`（既存の文言を拡張） | INFO | `clip%d prepare 完了 (字幕 %d 件 / 実カットなし)` — Timeline 経路であることを明示 |
| `build_archive_timeline`（既存） | INFO | `アーカイブ Timeline 構築: N クリップ / V1 … / S1 … / 全長 …s` |

「想定尺」と「Timeline 全長」を突き合わせれば、同種のズレが再発したときに
ログだけで軸の不一致を検出できる。

---

## 8. 影響範囲・互換性

| 項目 | 影響 |
|---|---|
| 従来画面経路（`timeline_review=false`） | **完全に無変更**（分岐の else 側が現行コードそのもの） |
| クリップ用パイプライン | **無変更** |
| Timeline 経路の字幕位置 | **正しくなる**（これが本修正の目的） |
| Timeline 経路の映像・音声・カット点 | 変わらない（`keep_segments` の算出ロジックは `run()` と `detect_edit_points` で同一） |
| Resolve 出力（`.fcpxml`） | 字幕時刻が正しくなる。カット編集点は変わらない |
| 処理時間 | **クリップごとの実カット再エンコード（映像 1 本ぶん）が丸ごと不要になり、prepare が大幅に短縮**。代わりに音声のみの抽出が入るが桁が違う |
| 一時領域 | `silence_cut.mp4` と `silence_seg_*.mp4` が作られなくなり消費が減る |
| `prepared["prepared_path"]` | Timeline 経路では `""`。参照箇所（`_burn_one` / 結果画面プレビュー）は従来経路専用のため安全 |

---

## 9. テスト計画

### 9-1. 新規単体テスト `tests/test_archive_prepare.py`

`ffmpeg` を起こさずに分岐だけを検証する（既存 `tests/test_archive_timeline.py` と同じ方式で
モジュール属性を差し替える）。

| # | 観点 | 期待 |
|---|---|---|
| 1 | `use_timeline=True` で `silence_cutter.run` / `_silence_cut` が呼ばれない | 呼び出し回数 0 |
| 2 | `use_timeline=True` で `detect_edit_points` → `recognize_for_timeline` の順に呼ばれる | 呼び出し順が一致 |
| 3 | `use_timeline=True` の `prepared["items"]` が `recognize_for_timeline` の戻り値 | 一致 |
| 4 | `use_timeline=True` の `prepared["prepared_path"]` が `""`、`normalized_path` が正規化後パス | 一致 |
| 5 | `use_timeline=True` の `normalized_duration` が `meta["source_duration_sec"]` | 一致 |
| 6 | `use_timeline=False` で従来どおり `_silence_cut` + `_transcribe` が呼ばれる | 呼び出し回数 1 / 1 |
| 7 | `recognize_for_timeline` が空を返しても prepared が作られる | `items == []` で 1 件 |

### 9-2. 既存テストの回帰

* `tests/test_archive_timeline.py` … `build_archive_timeline` は無変更のため**そのまま通る**こと。
* `tests/test_resolve_export.py` / `tests/test_timeline_project_io.py` … 無変更のため通ること。
* `python -m unittest discover -s tests` 全体がグリーンであること。

### 9-3. 軸の一致を保証する追加テスト（`tests/test_archive_timeline.py` へ追記）

`keep_segments` と items が同一軸であることを、構築結果で検証する。

| # | 観点 | 期待 |
|---|---|---|
| 8 | 各クリップの最後の字幕が、そのクリップの Timeline 範囲を超えない | `subtitle.timeline_end <= clip_range()[1] + 1e-6` |
| 9 | 字幕の `origin.source_start` が `keep_segments` 内の時刻へ収まる | `TimeMap.to_source` が None を返さない |

### 9-4. 手動確認

1. Twitch VOD またはローカル動画で採点 → 切り抜きを実行し、Timeline 画面を開く。
2. **無音カットが多いクリップ**を選び、クリップ**末尾**の字幕とその位置の波形（A1）を突き合わせる。
   修正前は末尾ほど字幕が右へずれている。修正後は先頭・中間・末尾のいずれでも一致すること。
3. ログの `編集点検出: … 残す区間 M 件` を確認する。修正前のズレ量の目安は `M × 約 30ms`。
4. 「完了（切り抜き＋字幕焼き込み）」で書き出し、出力動画でも末尾の字幕が音声と合っていること。
5. `archive.clip_pipeline.timeline_review = false` に切り替え、従来画面が現状どおり動作すること
   （プレビューが再生でき、焼き込み結果の字幕が合っていること）。

---

## 10. 残存する既知の誤差・本件対象外の事項

| # | 事象 | 扱い |
|---|---|---|
| 1 | `extract_audio_segments` の連結でも先頭に AAC プライミング 1 フレーム（約 21ms）が残る | **許容**。クリップ用と同一で、累積しない定数のため知覚できない |
| 2 | `_cut_region`（`clip_writer.py:52-65`）が `-ss` + `-c copy` のためキーフレーム丸めが起き、`raw.mp4` の内容が要求区間と一致しない | **本件対象外（別件）**。テロップ⇔音声の相対ズレの原因ではないが、採点グラフのマーカと実映像の対応が最大数秒ずれる要因。別途 `-ss` を `-i` の後ろへ置く（精密シーク＋再エンコード）か、キーフレーム丸め量を `keep_segments` へ反映する設計が要る |
| 3 | 実カット後 mp4 の `video start_time=0.021 / audio start_time=0.000` による A/V 微ズレ | Timeline 経路では実カット後ファイルを使わなくなるため**解消**。従来経路には残る |
| 4 | 既存プロジェクト JSON（`.timeline.json`）に保存済みのズレた字幕時刻 | 自動補正はしない。アーカイブ用 Timeline はその回限りの一時データで永続化しないため実害なし |

---

## 11. 実装手順

1. `src/archive/clip_writer.py`
   1. `_prepare_edit_points()` を追加（§4-3）。必要なら `silence_cutter` の import を追加。
   2. `_prepare_clips()` に `use_timeline` 引数を追加し分岐を実装（§4-2）。
   3. `write_clips()` で `use_timeline` を確定して渡す（§4-4）。
   4. `_silence_cut()` / `_transcribe()` のコメントへ「従来画面経路専用」と明記。
2. `src/archive/timeline_builder.py` のコメント修正（§4-5）。
3. `tests/test_archive_prepare.py` を追加（§9-1）、`tests/test_archive_timeline.py` へ §9-3 を追記。
4. `python -m unittest discover -s tests` を実行して全件グリーンを確認。
5. §9-4 の手動確認を実施。

---

## 12. 未確定事項（実装前に判断が必要なもの）

`docs/CLAUDE.md`「不明点がある場合は推測実装せず設計書へ記載すること」に従い記載する。

| # | 論点 | 本書の想定 | 確認したいこと |
|---|---|---|---|
| Q1 | 準備中の進捗表示 | 現状どおり `_prepare_clips` の 3 段階のみとし、`PipelineContext` の `progress_callback` は既定（noop）のままとする（現行の `_silence_cut` も進捗を出していないため挙動は変わらない） | 認識用音声の抽出中に細かい進捗を出したい場合は、`progress_callback` を注入する実装へ変更する |
| Q2 | `prepared_path` を空文字にするか、キー自体を落とすか | **空文字**（`""`）とし、キーは残す。既存の `p.get("prepared_path", "")` 参照が例外にならないため | キーを落として `KeyError` で誤参照を早期検出したい方針であれば変更する |
| Q3 | 認識用音声（`asr_audio.m4a`）をプレビューで再利用するか | 再利用しない（アーカイブ用 Timeline のプレビューは `normalized_path` から直接再生できるため） | クリップ用と同様に初回再生ソースとして渡したい場合は `ArchiveTimelineDialog` への引き回しが追加で必要 |
