# ver8 resolve5 — Timeline からの Resolve 出力が TypeError で落ちる

対象: `autoedit` (Stretheus クライアント)
関連: `docs/request/ver5/resolve.md` (ポイント・透かし / §5.5 R12)、
`docs/request/ver8/resolve4.md`

作成日: 2026-10-19
状態: **実装済み (2026-10-19)**

---

## 0. 要望と結論

> Timeline 画面から DaVinci Resolve に出力する際に以下のエラーが発生しています。
> `export_archive_result() got an unexpected keyword argument 'watermark_confirm_callback'`
> これを解決してください。なにがネックなのかも教えてください。

**原因は「引数の中継漏れ」1 点。** 透かし確認フック
(`watermark_confirm_callback`) を受け取るのは最終合流点の
`resolve_export.export_spec()` だけで、GUI と `export_spec()` の間にある
**薄いラッパー 3 本がこの引数を持っていない**。GUI 側はラッパーへ渡すため、
呼んだ瞬間に `TypeError` になる。

### 0.1 再現 (2026-10-19 / main `4b52aaa`)

```text
export_spec           -> OK
export_timeline       -> MISSING
export_clip_review    -> MISSING
export_archive_result -> MISSING

repro: export_archive_result() got an unexpected keyword argument 'watermark_confirm_callback'
repro: export_timeline() got an unexpected keyword argument 'watermark_confirm_callback'
repro: export_clip_review() got an unexpected keyword argument 'watermark_confirm_callback'
```

**報告された 1 画面だけの問題ではない。Resolve 出力ボタンを持つ 4 画面すべてが
同じ理由で落ちている。**

### 0.2 実装状況 (2026-10-19)

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | `export_timeline` へ `watermark_confirm_callback` を追加・中継 | 完了 |
| 2 | `export_clip_review` へ追加・中継 | 完了 |
| 3 | `export_archive_result` へ追加・中継 | 完了 |
| 4 | `tests/test_watermark_resolve_export.py` へ `EntryPointRelayTest` 3 件を追記 (計 18 件) | 完了 |
| 5 | 全テスト (`python -m unittest discover -s tests`) 1302 件 | OK |

§4-2 の 3 は GUI 4 箇所 (§1-1 の表と同じ 4 件) を `ast` で拾えており、
修正前の署名ならこのテストが落ちることを確認済み。

---

## 1. 現状分析

### 1-1. 呼び出しの経路

```
GUI (_on_export_resolve)
  └─ run_resolve_export(self, lambda confirm: ...)
        └─ export_timeline / export_clip_review / export_archive_result   ← ここで TypeError
              └─ export_spec(..., points=, job_type=, watermark_confirm_callback=)
                    └─ points_service.reserve → confirmed(reservation, callback)
```

| # | 画面 | 呼び出し元 | 呼ぶ関数 | 結果 |
| --- | --- | --- | --- | --- |
| 1 | Timeline 編集 (クリップ) | `src/gui/timeline/timeline_editor_dialog.py:1215` | `export_timeline` | TypeError |
| 2 | Timeline 編集 (アーカイブ) | `src/gui/timeline/archive_timeline_dialog.py:664` | `export_archive_result` | TypeError |
| 3 | アーカイブ結果画面 | `src/gui/archive_result_window.py:253` | `export_archive_result` | TypeError |
| 4 | 字幕一覧 | `src/gui/subtitle_editor_dialog.py:584` | `export_clip_review` | TypeError |

### 1-2. 引数の持ち主

| 関数 | 場所 | `points` | `job_type` | `watermark_confirm_callback` |
| --- | --- | --- | --- | --- |
| `export_spec` | `src/export/resolve_export.py:656` | あり | あり | **あり** |
| `export_timeline` | 同 `:546` | あり | 内部決定 | **無し** |
| `export_clip_review` | 同 `:803` | あり | 内部決定 | **無し** |
| `export_archive_result` | 同 `:818` | あり | 内部決定 | **無し** |

### 1-3. 混入した経緯

`git log -L` で追うと、コミット `53fa617`
(「横動画からshort動画のクリッププロジェクトを作成できる機能追加」) で

- GUI 4 箇所へ `points=` と `watermark_confirm_callback=` を**追加**
- ラッパー 3 本へは `points` と `job_type` **だけ**を追加

という非対称な変更が入っている。`points` は足したのに
`watermark_confirm_callback` を足し忘れた、というのがそのままの事実。

---

## 2. ネック (なぜ起きて、なぜ気づけなかったか)

### 2-1. 本質的なネック: 「同じ引数を 2 段で手書きする」構造

`export_spec()` は「すべての入口が通る唯一の合流点」として設計されている
(ver5 resolve §5.5)。ところが入口は 3 本のラッパーに分かれており、
**合流点に引数を足すたびに、ラッパー 3 本へ同じ引数を手で書き足す**必要がある。
足し忘れても Python は定義時に何も言わず、**その画面のボタンを押した瞬間まで
エラーが出ない**。これが今回の直接の穴であり、同じ事故が次も起きる構造。

### 2-2. テストが守っていなかった

`tests/test_watermark_resolve_export.py:234`
`test_entry_points_choose_the_job_type` はラッパー 3 本を通るが、

```python
resolve_export.export_archive_result("vod.mp4", [{"index": 1}], {})
```

と **`watermark_confirm_callback` を渡さずに**呼び、`job_type` の値だけを見ている。
つまり「GUI が実際に渡している引数の形」は 1 件もテストされていない。
GUI 側の呼び出しは `QDialog` のメソッド内のラムダなので、単体テストからは踏まれない。

### 2-3. エラーが「出力できない」以外の形で見えにくい

`run_resolve_export()` (`src/gui/subtitle_editor_dialog.py:98-108`) が
`except Exception` で受けてダイアログに文字列を出し `None` を返すため、
利用者には「出力に失敗しました」としか見えず、落ちた原因が引数だと分からない。
ログには `_logger.exception` が残る。

### 2-4. 直らなかった場合の二次被害 (重要)

`points.confirmed()` は

```python
if not watermark_required(reservation) or confirm is None:
    return True
```

となっており、**コールバックが `None` だと「確認せず透かしを入れて出力」になる**。
したがってラッパーへ `watermark_confirm_callback=None` を足すだけで
「エラーは消えたが、ポイント不足時に無確認で透かしが入る」という
ver5 resolve §5.6 R12 違反になり得る。**引数を足すだけでなく、
`export_spec` へ確実に渡し切ることまでが修正の範囲**。

なお現状は `export_spec` 呼び出し前に `TypeError` になるため、
ポイントの予約は発生しておらず**ポイントの消費漏れ・残留予約は無い**。

---

## 3. 方針

1. ラッパー 3 本へ `watermark_confirm_callback=None` を足し、`export_spec` へ中継する。
   引数名・既定値は `export_spec` と揃える (GUI 側は無変更で済む)。
2. `**kwargs` での一括転送は**採らない**。綴り間違いを黙って飲み込み、
   「渡したつもりで渡っていない」= §2-4 の無確認透かしに化けるため。
   明示引数のままにして、代わりに §4-2 の署名テストで守る。
3. 既存の呼び出し (引数を渡さない既存テスト・将来の CLI) を壊さないため、
   既定値 `None` の末尾追加のみとし、位置引数の順番は変えない。

---

## 4. 変更点

### 4-1. `src/export/resolve_export.py`

3 本とも「引数を 1 つ足して `export_spec` へ渡す」だけ。ロジック変更は無い。

```python
# Timeline 用 (ver3): 編集画面から呼ぶ一括エクスポート
# watermark_confirm_callback: ポイント不足で透かしが入るときの確認フック
#   (渡さないと無確認で透かし入り出力になるため、GUI からは必ず渡す / ver5 resolve §5.6 R12)
def export_timeline(timeline, settings, overwrite_confirm=None, points=None,
                    watermark_confirm_callback=None):
    ...
    return export_spec(spec, dest, settings=settings, overwrite_confirm=overwrite_confirm,
                       points=points, job_type=_job_type(timeline),
                       watermark_confirm_callback=watermark_confirm_callback)
```

`export_clip_review` / `export_archive_result` も同形
(`job_type` はそれぞれ `JOB_CLIP` / `JOB_ARCHIVE` のまま)。

| 関数 | 行 | 変更 |
| --- | --- | --- |
| `export_timeline` | `:546` | 引数追加 + 中継 |
| `export_clip_review` | `:803` | 引数追加 + 中継 |
| `export_archive_result` | `:818` | 引数追加 + 中継 |

### 4-2. `tests/test_watermark_resolve_export.py` へ追記

§2-2 の穴を閉じる。**「GUI が渡す形で呼べること」と「中継されること」の両方**を見る。

| # | テスト | 内容 |
| --- | --- | --- |
| 1 | `test_entry_points_relay_watermark_callback` | ラッパー 3 本を `watermark_confirm_callback=sentinel` 付きで呼び、`export_spec` が受けた値が同一オブジェクトであること |
| 2 | `test_entry_points_accept_gui_keywords` | `inspect.signature` で 3 本が `export_spec` の公開キーワード (`overwrite_confirm` / `points` / `watermark_confirm_callback`) を欠いていないこと = 次の足し忘れを定義時点で落とす |
| 3 | `test_gui_export_calls_match_signature` | GUI 4 箇所が渡すキーワード名が、呼ぶラッパーの署名に存在すること (`ast` で呼び出しを拾い `inspect` と突き合わせる / 画面を起動しない) |

3 は「GUI の呼び出しと関数定義のズレ」という今回の事故そのものを検出する。

### 4-3. 変更しないもの

- `setting.json` / `settings_window.py`: 設定項目は増えない。
- GUI 4 箇所: すでに正しい形で渡しているため**無変更**。
- `export_spec` / `points.py` / `watermark_overlay.py`: 無変更。

---

## 5. 変更ファイル

| ファイル | 内容 |
| --- | --- |
| `src/export/resolve_export.py` | ラッパー 3 本へ `watermark_confirm_callback` を追加・中継 (各 2 行) |
| `tests/test_watermark_resolve_export.py` | 中継・署名・GUI 突き合わせの 3 件を追記 |

---

## 6. 確認手順

1. `python -m unittest tests.test_watermark_resolve_export tests.test_resolve_export`
2. 4 画面それぞれで「DaVinci Resolve ファイル出力」→ FCPXML (+ SRT) が出ること
   - Timeline 編集 (クリップ / アーカイブ)、アーカイブ結果画面、字幕一覧
3. ポイント残高が足りない状態で出力 → **確認ダイアログが出る**こと、
   「いいえ」で出力されず予約が cancel されること (ver5 resolve §5.5 R4)
4. 残高が足りる状態で出力 → 確認が出ず、透かしの無い FCPXML が出ること

---

## 7. 残件・確認事項

| # | 内容 | 扱い |
| --- | --- | --- |
| 1 | `run_resolve_export` の `except Exception` が原因を隠す (§2-3)。`TypeError` 等のプログラムミスは利用者向け文言ではなく「不具合」として出す/ログ誘導する案 | 本件の範囲外。別途要相談 |
| 2 | 入口ラッパーが 3 本に分かれている構造自体の整理 (引数を 1 箇所に集める) | 影響範囲が広いため今回は §4-2 のテストで代替 |

---
---

# 第2部 — 取り込み後に「字幕と音声がずれる」 (request5 追加分)

作成日: 2026-10-19
状態: **設計 (未実装)**
対象: `src/export/fcpxml_builder.py` / `src/export/resolve_export.py` /
`src/modules/ffmpeg_runner.py` / `src/settings/setting.json`

---

## 8. 要望と結論

> 動画のカットは問題なくインポートできて、字幕も問題なくインポートできました。
> しかしインポート時に字幕と音声がずれてしまいます。何かいい解決案ありますか？

**結論: 「カットの時間軸」と「字幕の時間軸」が別々に作られているため。**
両者は本来同じタイムラインを指すはずだが、

- カット (`asset-clip`) のタイムライン位置 = **整数フレームへ丸めた尺の累積**
- 字幕 (`caption` / `title` / SRT) のタイムライン位置 = **浮動小数の秒を独立に丸めた値**

となっており、丸め誤差がカット数ぶん積み上がる。音声はカット側に付いて動くため、
**カットが増える後半ほど字幕だけが前後にずれる**。これはコードで再現できる確定要因で、
60fps・600 カットで平均 81ms / 最大 317ms、30fps・1200 カットでは最大 1.07s になる (§10-1)。

さらに FCPXML の fps が**元動画の実フレームレートではなく出力設定
(`ffmpeg.output_fps`、既定 60) をそのまま書いている**ため、素材が 29.97/59.94 (NTSC) や
30fps のときに追加のズレが乗る (§10-2)。

いい解決案としては **「フレームグリッドを 1 本に統一する」**
(量子化を 1 箇所に集め、字幕は担当クリップの先頭からの相対位置でフレーム化する) が本筋で、
これだけで累積がゼロになり誤差は常に 0.5 フレーム以内 (60fps で 8.3ms) に収まる。
副作用はなく、設定追加も不要。§11 以降に設計をまとめる。

---

## 9. 現状分析 — 字幕の時刻は「どの軸」で作られているか

### 9-1. 時間軸の系譜

```
元動画 (実時間 秒)
  |
  +- 音声認識は「素材そのまま」に対して行う (無音カット前)
  |    src/modules/subtitle_generator.py:1313 recognize_for_timeline
  |    -> 素材時間の items     ※継ぎ接ぎ音声の認識は 20260812 で廃止済み
  |
  +- TimeMap で「詰めた軸」へ写す (src/timeline/timemap.py / 全て float・丸め無し)
  |    詰めた軸 = 残す区間の尺を浮動小数で足し上げた理想タイムライン
  |
  +- Timeline へ載る (builder.py:698 _append_subtitles)
       字幕クリップ timeline_start = 詰めた軸の秒
       映像クリップ timeline_start = 区間尺の浮動小数累積
```

ここまでは**映像・音声・字幕がすべて同じ「理想軸 (float 秒)」に乗っており、ズレは無い**。
ズレは FCPXML を書く最後の 1 段で生まれる。

### 9-2. FCPXML 側の時間軸

| 対象 | 位置の決め方 | 該当コード |
| --- | --- | --- |
| カット (`asset-clip`) | `offset_f += round(尺×fps)` の**整数フレーム累積** | `fcpxml_builder.py:123` `_quantize_clips` |
| 字幕 (`caption`/`title`) | `round(理想軸の秒×fps)` を**独立に**算出 | `fcpxml_builder.py:338` `_place_text_element` |
| 透かし (`video`) | 同上 | 同 `:338` |
| SRT サイドカー | **理想軸の秒そのまま** (量子化なし) | `resolve_export.py:274` → `:780` |
| 音声 | カットに付随 (`asset-clip` が `hasAudio="1"`) | `fcpxml_builder.py:231` |

`_place_text_element` は求めた `start_f` を `_assign_to_clips` (`:142`) へ渡し、
そこで**フレーム軸のクリップ列**と突き合わせて担当クリップを決める。
つまり「float で丸めた絶対フレーム」を「累積誤差を含むフレーム軸」へ当てているため、
**どのクリップに属するかの判定ごと**ずれていく。

---

## 10. 原因と切り分け

### 10-1. 原因 A: 量子化が非対称で誤差が累積する (**確定・本命**)

```
clips 側 : offset_k = Σ_{i<k} round(d_i × fps)        ← 各区間を丸めてから足す
字幕側   : frame    = round( (Σ_{i<k} d_i) × fps )    ← 足してから丸める
```

この 2 つは一致しない。区間ごとに最大 ±0.5 フレームの差が出て、それが積み上がる。

**実測 (本設計時に計算)**

| 区間数 | 1.009s 等長の例 (60fps) | 乱数尺 (平均 2.5s) 末尾ズレ 60fps | 同 30fps |
| --- | --- | --- | --- |
| 10 | 5 フレーム = 83ms | — | — |
| 100 | 46 フレーム = 767ms | 平均 40ms / 最大 150ms | 平均 71ms / 最大 267ms |
| 300 | 138 フレーム = 2.30s | 平均 62ms / 最大 233ms | 平均 125ms / 最大 433ms |
| 600 | — | 平均 81ms / 最大 317ms | 平均 179ms / 最大 700ms |
| 1200 | — | 平均 120ms / 最大 500ms | 平均 255ms / 最大 1067ms |

- 等長区間 (端数が同符号に偏る) は**系統誤差**になり最悪 0.5 フレーム×区間数まで伸びる。
- 無音カットは数百〜千区間になるため、実運用では**数十〜数百 ms が常態**。
  フルテロップでは 100ms でも「口と字幕が合っていない」と分かる。
- **症状はカットが多い後半ほど大きくなる** = 今回の報告と一致する。

**副作用として既に警告が出ている可能性がある。**
量子化タイムラインが理想軸より短くなった場合、終端付近の字幕が
`fcpxml_builder.py:152` のクランプへ落ち、ログに

```
タイムライン終端を超える字幕をクリップ末尾へクランプしました (丸め誤差の端数など)。
```

が出る。**このログが出ていれば原因 A は確定**なので、まずログを確認したい (§15-1)。

### 10-2. 原因 B: FCPXML の fps が元動画の実フレームレートでない (**確定**)

`resolve_export.py:362`

```python
fps = ffmpeg_runner.get_output_fps(settings.get("ffmpeg", {}))   # setting.json: ffmpeg.output_fps (既定 60)
```

元動画を ffprobe しているのは**尺だけ** (`:344` `_probe_duration`) で、
フレームレートは一度も見ていない。この fps が

- `<format frameDuration="1/60s">` (シーケンス **および素材 asset** の宣言 / `:224`)
- 全時刻の量子化単位

の両方になる。問題は 2 つ。

| # | 状況 | 起きること |
| --- | --- | --- |
| 1 | 素材 30fps / 設定 60fps | 1/60 刻みの時刻を Resolve が素材のフレーム境界 (1/30) へ丸め直す → 1 箇所あたり最大 33ms、A と同じく累積する |
| 2 | 素材 29.97 / 59.94 (NTSC) | `frames_to_text` (`:65`) は**整数 fps 専用**で `1001/30000s` 系を表現できない。60 と宣言した時点で 0.1% の比例ズレ → 10 分で 0.6s、30 分で 1.8s、60 分で 3.6s |

2 は「**時間に比例して増える**」ので A と区別できる。素材が YouTube/配信アーカイブ由来だと
29.97/59.94 は珍しくない。

### 10-3. 原因 C: SRT サイドカーの軸が FCPXML と違う (**確定**)

`_srt_entries_from_items` (`resolve_export.py:274`) は理想軸の秒をそのまま書く。
FCPXML タイムラインは §10-1 のとおり量子化累積なので、**SRT を Resolve へ別途読み込むと
A と同じズレがそのまま乗る**。字幕を SRT 経由で取り込んでいる場合、FCPXML 側を直しても
SRT を直さないと症状が残る。

### 10-4. 原因 D: 素材が VFR (可変フレームレート) (**素材依存・FCPXML では解決不能**)

OBS・画面収録・配信アーカイブは VFR になりやすく、本プロジェクトでも
`silence_cutter.py:205` に

```
# 各区間を CFR (固定フレームレート) で固定する (resolve 20260630 対策B)。
# VFR ソースを CFR 非強制で抽出→コピー連結すると区間境界で TS が破損し…
```

と**過去に VFR で事故った履歴**が残っている。FCPXML は元動画を直接参照するため、
Resolve は取り込み時に VFR 素材を CFR へ conform する。このとき
「autoedit が見ている実 PTS 秒」と「Resolve のフレーム番号×1/fps」は**非線形にずれる**ので、
秒で位置を書く FCPXML では原理的に合わせられない。

→ 対処は「**事前に CFR 化した素材で編集を始める**」しかない。
設計としては**判定して警告を出す**ところまでを範囲とする (§12-3)。

### 10-5. 原因 E: 全編一定のオフセット (コンテナ側)

MP4 の `start_time` / edit list / AAC のプライミング (1024 サンプル ≒ 21ms) を
FFmpeg は補正して読むが、Resolve は扱いが異なる場合がある。
この場合**全編が同じ量 (20〜100ms 程度) ずれる**。
A〜D を直しても一定量残るならこれで、避難口として手動オフセットを用意する (§12-4)。

### 10-6. 切り分け表 (ズレ方 → 原因)

| ズレ方 | 原因 | 対処 |
| --- | --- | --- |
| **後半ほど大きい / カットが多い所で増える** | A (量子化累積) | §12-1 (本命) |
| 時間に**比例**して増える (10 分で約 0.6s = 0.1%) | B-2 (NTSC 29.97/59.94) | §12-2 |
| 後半ほど大きい + 素材が 30fps で設定が 60fps | B-1 | §12-2 |
| FCPXML は合うが SRT だけずれる | C | §12-1 |
| 不規則・区間ごとに飛ぶ | D (VFR) | 素材を CFR 化 (§12-3 で警告) |
| **全編一定量** | E | §12-4 の手動オフセット |

---

## 11. 方針

1. **フレームグリッドを 1 本にする。** 量子化は `fcpxml_builder` の 1 箇所
   (`_quantize_clips`) だけが行い、字幕・透かし・SRT は**すべてその結果から導く**。
   具体的には「字幕は担当クリップの先頭からの相対時間でフレーム化する」。
   → 誤差はクリップ内の 0.5 フレーム以内に閉じ、**累積しない**。
   本設計時の検証: 現行は 300 カットで最大 251ms / 1200 カットで最大 571ms、
   修正後はどちらも **8.3ms (60fps の半フレーム) 固定**。
2. **タイムベースは元動画の実フレームレートを基準にする。** ffprobe の `r_frame_rate` を
   **有理数のまま** (30000/1001 など) 扱い、FCPXML へもそのまま書く。
   取得できないときだけ従来どおり `ffmpeg.output_fps` へフォールバックする。
   どれを使うかは `setting.json` で選べるようにし、既定を `"source"` とする。
3. **直らない分の避難口を 1 つだけ用意する。** 一定オフセット (§10-5) 用に
   `export.resolve.subtitle_offset_sec` を設ける。既定 0.0 = 無効。
4. **次に同じ報告が来たとき原因が即分かるログを出す。** 選んだタイムベース、
   VFR 疑い、理想軸と量子化軸の最大差 (ms) を出力時に 1 行で残す。
5. 既存の出力は壊さない。整数 fps のケースでは**現在と同一の XML**
   (`"30/60s"` 形式も含め) を出す。既存テスト 20 件 (`test_fcpxml_builder`) /
   24 件 (`test_resolve_export`) はそのまま通ることを条件とする。

---

## 12. 変更点

### 12-1. `src/export/fcpxml_builder.py` — 軸の統一 (本命・最優先)

**(a) `_quantize_clips` に理想軸の秒も持たせる**

```python
# クリップ列を整数フレームへ量子化し、タイムライン位置を量子化済み尺の累積で確定する
# 戻り値: [{"name", "offset_f", "start_f", "dur_f", "offset_s", "dur_s"}, ...]
#   offset_s/dur_s = 量子化前の理想軸 (字幕時刻と同じ軸)。字幕の担当クリップ判定に使う。
def _quantize_clips(clips, fps):
    ...
    cursor_f = 0
    cursor_s = 0.0
    for index, clip in enumerate(clips, 1):
        dur_s = max(float(clip.get("duration", 0.0) or 0.0), 0.0)
        dur_f = max(to_frames(dur_s, fps), 1)
        quantized.append({..., "offset_s": cursor_s, "dur_s": dur_s})
        cursor_f += dur_f
        cursor_s += dur_s
```

**(b) 理想軸の秒 → 量子化フレーム軸 への写像を追加する**

```python
# 理想軸 (区間尺の浮動小数累積) の秒を、クリップ列と同じフレーム軸へ写す (§10-1)
# 担当クリップの先頭からの相対時間だけをフレーム化するため、
# クリップ数が増えても誤差は 0.5 フレーム以内に留まり累積しない。
def _timeline_frames(fclips, seconds, fps):
    if not fclips:
        return 0
    value = max(float(seconds or 0.0), 0.0)
    for fc in fclips:
        if value < fc["offset_s"] + fc["dur_s"]:
            local_f = min(to_frames(value - fc["offset_s"], fps), fc["dur_f"])
            return fc["offset_f"] + max(local_f, 0)
    # 理想軸の終端以降 (丸め端数) は最終クリップの末尾へ寄せる (クランプは _assign_to_clips)
    last = fclips[-1]
    return last["offset_f"] + last["dur_f"]
```

**(c) `_place_text_element` を (b) 経由にする**

```python
def _place_text_element(clip_elements, fclips, timeline_f, item, fps, appender, style_seq):
    if not clip_elements:
        return
    # 字幕/透かしの時刻はクリップと同じ軸へ写してからフレーム化する (§10-1)
    offset_s = float(item.get("offset", 0.0) or 0.0)
    dur_s = max(float(item.get("duration", 0.0) or 0.0), 0.0)
    start_f = _timeline_frames(fclips, offset_s, fps)
    end_f = _timeline_frames(fclips, offset_s + dur_s, fps)
    pieces = _assign_to_clips(fclips, start_f, max(end_f, start_f + 1), timeline_f)
```

開始と終了を同じ写像に通すため、カットを跨ぐ字幕は**跨いだ分だけ短くなる**
(これまでは尺をそのまま足していたため跨ぎ時に伸びていた)。分割・枝番・クランプの
既存ロジック (`_assign_to_clips`) は変更しない。

**(d) 有理数 fps に対応する (§10-2 の 2)**

`spec["fps"]` は従来どおり数値も受けつつ、`(分子, 分母)` も受けられるようにする。
変換は 1 箇所に閉じる。

```python
# 実フレームレートの既知な有理数表現 (NTSC 系は 1001 分母で表す)
_RATIONAL_FPS = {
    23.976: (24000, 1001), 29.97: (30000, 1001),
    47.952: (48000, 1001), 59.94: (60000, 1001), 119.88: (120000, 1001),
}

# fps を (分子, 分母) へ正規化する。整数 fps は (fps, 1) = 現行と同一表記になる。
def normalize_fps(value): ...

# 整数フレームを FCPXML の有理数時刻表記へ整形する
#   整数 fps : 30 フレーム / 60fps        -> "30/60s"       (現行と同一)
#   NTSC     : 30 フレーム / 30000/1001  -> "30030/30000s"
def frames_to_text(frames, fps):
    num, den = normalize_fps(fps)
    ticks = int(frames) * den
    if ticks <= 0:
        return "0s"
    if ticks % num == 0:
        return f"{ticks // num}s"
    return f"{ticks}/{num}s"
```

`<format frameDuration>` は `f"{den}/{num}s"` (60fps → `"1/60s"` = 現行同一 /
29.97 → `"1001/30000s"`)。`to_frames` は `round(秒 × num / den)` とする。

**(e) SRT 用に写像を公開する**

```python
# 理想軸の秒 -> 量子化タイムラインの秒 への写像関数を返す (SRT サイドカー用 / §10-3)
# spec["clips"] から作るため、FCPXML と SRT が必ず同じ軸になる。
def timeline_time_mapper(spec): ...
```

### 12-2. `src/export/resolve_export.py` — タイムベースの決定と SRT の軸合わせ

| 箇所 | 変更 |
| --- | --- |
| `_base_spec` (`:361`) | `fps` を `_resolve_timebase()` で決める。実測値はログ用に `spec["source"]["fps_text"]` へ残す |
| 新規 `_resolve_timebase` | `export.resolve.timebase` に従い `"source"` → ffprobe 実測 (有理数) / `"output_fps"` → 現行 / 数値 → その値。取得失敗は WARNING を出して `output_fps` へフォールバック |
| `_probe_duration` (`:344`) | 新しい `ffmpeg_runner.probe_media_timing()` 1 回で尺・fps・VFR 疑い・`start_time` をまとめて取得する (ffprobe の呼び出し回数は増やさない) |
| `_write_srt_sidecar` (`:780`) | `fcpxml_builder.timeline_time_mapper(spec)` を通した時刻で SRT を書く (§10-3) |
| `export_spec` (`:660`) | ① 字幕系 (`captions` / 字幕由来 `titles` / `srt_entries`) へ `subtitle_offset_sec` を一律加算 ② 診断ログ 1 行を出す |

オフセットは「全入口が通る唯一の合流点」= `export_spec` で 1 回だけ適用する (§2-1 の教訓)。
透かし (`overlays`) とテーマ演出には**掛けない**。字幕由来かどうかを区別するため、
`_titles_from_items` が作る dict へ `"kind": "subtitle"` を付け、
`_theme_title` は `"kind": "theme"` とする (`fcpxml_builder` は未知キーを無視するので影響なし)。

**診断ログ (出力ごとに 1 行)**

```
Resolve 出力タイムベース: 30000/1001 (29.970fps / 実測) / 出力設定 60fps /
  VFR 疑い: なし / 容器 start_time: 0.000s / 軸差の最大: 8.3ms (クリップ 412 件)
```

### 12-3. `src/modules/ffmpeg_runner.py` — 実フレームレートの取得

```python
# 元動画の時間軸情報を 1 回の ffprobe でまとめて取得する (resolve5 §12-2)
# 戻り値: {"duration": float, "fps": (num, den), "avg_fps": (num, den),
#          "start_time": float, "vfr_suspected": bool}
# 取得できない項目は 0 / None とし、呼び出し側のフォールバックに任せる (パイプラインを止めない)。
def probe_media_timing(input_path, ffmpeg_settings):
    # ffprobe -v error -select_streams v:0
    #         -show_entries stream=r_frame_rate,avg_frame_rate
    #         -show_entries format=duration,start_time -of json
    ...
```

- `r_frame_rate` と `avg_frame_rate` の差が 1% を超えたら `vfr_suspected = True`
  → 呼び出し側が WARNING を出す (§10-4)。VFR は FCPXML では直せないため、
  ログで「素材を CFR 化してから編集してください」と案内するに留める。
- 既存の `probe_duration` / `probe_dimensions` は**残す** (他経路が使用中のため)。
- `src/timeline/media_probe.py:82` の `_parse_fps` は float へ潰すため流用しない
  (NTSC を表現できない)。有理数のまま返す新規実装とし、重複する旨をコメントへ残す。

### 12-4. `src/settings/setting.json` — 追加する設定 (3 項目)

```json
"export": {
  "resolve": {
    "timebase": "source",
    "subtitle_offset_sec": 0.0,
    "drift_warn_ms": 20
  }
}
```

| キー | 既定 | 意味 |
| --- | --- | --- |
| `timebase` | `"source"` | FCPXML のタイムベース。`"source"`=元動画の実測 fps / `"output_fps"`=従来 (`ffmpeg.output_fps`) / 数値=固定指定 |
| `subtitle_offset_sec` | `0.0` | 字幕・SRT へ一律加算する秒 (§10-5 の避難口。負値可) |
| `drift_warn_ms` | `20` | 理想軸と量子化軸の最大差がこの値を超えたら WARNING |

`src/export/config.py` の `resolve_config()` へ 3 キーを追加する
(既存同様、欠落時は既定へフォールバックするので**既存の setting.json は無変更で動く**)。

### 12-5. `src/settings/settings_window.py`

Resolve 出力タブへ 2 項目を追加する (`drift_warn_ms` は内部用なので画面には出さない)。

| 表示名 | 対応キー | ウィジェット |
| --- | --- | --- |
| タイムベース | `export.resolve.timebase` | コンボ (元動画に合わせる / 出力設定に合わせる) |
| 字幕オフセット (秒) | `export.resolve.subtitle_offset_sec` | 小数スピン (-5.00〜+5.00 / 0.01 刻み) |

### 12-6. テスト

`tests/test_fcpxml_builder.py` へ追記 (軸の統一):

| # | テスト | 内容 |
| --- | --- | --- |
| 1 | `test_subtitle_offset_does_not_drift_over_many_clips` | 1.009s×100 クリップ。最後の字幕が担当クリップ内の正しいフレームへ入ること (**現行実装では 46 フレームずれる**) |
| 2 | `test_subtitle_anchored_to_same_clip_as_ideal_axis` | 理想軸の秒が属するクリップと、生成 XML で親になったクリップが一致すること |
| 3 | `test_ntsc_timebase_is_rational` | `fps=(30000,1001)` で `frameDuration="1001/30000s"`、30 フレームが `"30030/30000s"` になること |
| 4 | `test_integer_fps_output_is_unchanged` | 整数 fps では現行と同一文字列 (`"30/60s"` / `"1/60s"`) になること = 後方互換の固定 |
| 5 | `test_subtitle_spanning_cut_is_trimmed` | カットを跨ぐ字幕の尺が、跨いだ分だけ短くなること |

`tests/test_resolve_export.py` へ追記:

| # | テスト | 内容 |
| --- | --- | --- |
| 6 | `test_srt_sidecar_uses_quantized_timeline` | SRT の時刻が FCPXML のクリップ軸と一致すること (§10-3) |
| 7 | `test_timebase_source_uses_probed_fps` | `timebase="source"` で ffprobe 実測値 (29.97) が spec へ入ること (probe はモック) |
| 8 | `test_timebase_falls_back_to_output_fps` | probe 失敗時に `ffmpeg.output_fps` へ落ち、WARNING が出ること |
| 9 | `test_subtitle_offset_setting_shifts_only_subtitles` | オフセットが字幕/SRT のみに乗り、クリップ・透かし・テーマへは乗らないこと |
| 10 | `test_vfr_source_logs_warning` | VFR 疑いで WARNING が出ること (出力自体は続行する) |

### 12-7. 変更しないもの

- `src/timeline/timemap.py` / `builder.py` / `subtitle_generator.py`:
  理想軸は float のままで正しい。**量子化はエクスポート時にだけ行う**方針を崩さない。
- `silence_cutter.py` の切り出し (`-ss`/`-t`/`-fps_mode cfr`): 焼き込み出力の挙動は変えない。
- GUI 4 画面の呼び出し: 無変更。
- `points.py` / `watermark_overlay.py`: 無変更 (透かしは A の修正で位置も揃う)。

---

## 13. 変更ファイル

| ファイル | 内容 | 規模 |
| --- | --- | --- |
| `src/export/fcpxml_builder.py` | 軸の統一 (`_quantize_clips` / `_timeline_frames` / `_place_text_element`)、有理数 fps、`timeline_time_mapper` 公開 | 中 (約 70 行) |
| `src/export/resolve_export.py` | タイムベース決定、SRT の軸合わせ、オフセット適用、診断ログ | 中 (約 60 行) |
| `src/modules/ffmpeg_runner.py` | `probe_media_timing()` 追加 | 小 (約 40 行) |
| `src/export/config.py` | 設定 3 キーの読み出し | 小 |
| `src/settings/setting.json` | 設定 3 キーの既定値 | 小 |
| `src/settings/settings_window.py` | 設定 2 項目の UI | 小 |
| `tests/test_fcpxml_builder.py` | テスト 5 件 | 小 |
| `tests/test_resolve_export.py` | テスト 5 件 | 小 |

実装順は **§12-1 (A) → §12-2 の SRT (C) → §12-2/12-3 の fps (B) → §12-4/12-5 (E の避難口)**。
A だけで効果が出る見込みのため、A を入れた時点で一度実機確認したい。

---

## 14. 確認手順

1. `python -m unittest tests.test_fcpxml_builder tests.test_resolve_export tests.test_watermark_resolve_export`
2. 全テスト `python -m unittest discover -s tests` (現状 1302 件) が通ること。
3. **A の回帰確認**: カットが 300 件以上ある素材で Timeline 画面から出力し、
   生成 XML の最後の `caption` の `offset` が担当クリップの範囲内にあること。
   ログに「タイムライン終端を超える字幕を…クランプ」が**出なくなる**こと。
4. **実機**: Resolve へ取り込み、
   - 先頭 / 中間 / 末尾の 3 点で口の動きと字幕が合うこと (**末尾が肝**)
   - 字幕トラックと SRT (別読み込み) の時刻が一致すること
5. 29.97fps 素材で `<format frameDuration="1001/30000s">` が出て、Resolve の
   タイムラインが 29.97 として開くこと。
6. 既存の 60fps 素材で、修正前後の XML 差分が**字幕の offset だけ**であること
   (クリップ・透かし・スタイルは不変)。

---

## 15. 残件・確認事項

実機の素材が無いと確定できない点をここへ残す (推測実装はしない)。

| # | 内容 | 必要なもの |
| --- | --- | --- |
| 1 | **ズレ方の形** (一定量か / 後半ほど大きいか / 時間比例か)。§10-6 の切り分けに直結する | 先頭・中間・末尾それぞれの「ずれている秒数」。出力時のログ全文 (クランプ警告の有無) |
| 2 | **元動画の実フレームレート**と VFR かどうか | `ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate,avg_frame_rate -show_entries format=duration,start_time -of json <元動画>` の出力 |
| 3 | 字幕は FCPXML の字幕トラックで見ているか、SRT を別読み込みしているか (§10-3 の該当有無) | 取り込み手順 |
| 4 | Resolve のバージョンと、取り込み時に「フレームレート不一致」の確認ダイアログが出たか | — |
| 5 | Resolve が接続要素 (`caption`/`title`) の `offset` を親ローカル時間として解釈しているかの実機検証。仕様どおりなら A の修正で解決するが、Resolve 側の解釈差があれば `spine` 直下の独立トラックへ出す案へ切り替える | 実機 (A 修正後の再測定で判断) |
| 6 | VFR 素材だった場合の CFR 化 (取り込み前の前処理をパイプラインへ組み込むか、案内に留めるか) | 方針の合意 |
| 7 | `timebase` の既定を `"source"` にすることで、60fps 固定運用の既存ユーザーのタイムラインが素材 fps で開くようになる。これを既定として良いか | 方針の合意 |

---
---

# 第3部 — 先頭に余白があると、余白ぶん丸ごとずれる (request5 追加分・2 回目の指摘)

作成日: 2026-10-19
状態: **実装済み (2026-10-19)** — 実装状況は §16-1、実装時の設計との差異は §16-2
位置づけ: **本命はこちら。** 第2部 (§8-§15) の量子化累積は「後半ほど数十〜数百 ms」の
別件として残るが、**今回の「大きなズレ」の原因は第3部で確定**。優先順位は §19-6 で更新する。

---

## 16. 指摘と結論

> 字幕インポート時に、一番最初に余白があるときが問題です。余白があると
> インポート時に余白分を無視して Timeline に置いてしまい、そこで大きなズレになってしまいます。

**原因が特定できました。`build_timeline_spec()` が Timeline 上のクリップ位置を捨てて、
尺だけを 0 秒から詰め直しているためです。** (`src/export/resolve_export.py:501`)

```python
segments = [(c.source_in, c.source_out) for c in body_clips]   # ← timeline_start を捨てている
spec["clips"] = _clips_from_segments(segments, "cut")           # ← 0 秒から順に詰める
```

Timeline のクリップは `timeline_start` (タイムライン上の位置) を持っているのに、
エクスポートは **`source_in` / `source_out` (素材のどこを使うか) だけ**を取り出し、
`_clips_from_segments` → `_quantize_clips` が**先頭から隙間なく連結**する。
その結果、

- 先頭の余白 (オープニングぶんの空き・先頭の空白) → **丸ごと消える**
- 途中の余白 (削除・無効化したクリップの穴、OP/ED や追加メディアの区間) → **丸ごと詰まる**

字幕も同じ「詰めた軸」へ写し直して出力している (`:520` `_timeline_subtitle_items`) ため
**FCPXML 単体では整合して見える**。しかし利用者側の Resolve タイムラインには余白がある
(オープニングを置く・置いてある) ので、**字幕だけが余白ぶん前に出る**。
オープニングは通常 5〜15 秒なので「大きなズレ」になる。

**解決案: 詰め直しをやめ、Timeline の座標をそのまま FCPXML へ写す。**
空いている区間は FCPXML の `<gap>` (空白の正式な表現) として出力する。
こうすると Resolve のタイムラインが autoedit の Timeline と 1:1 になり、
**空いた先頭へオープニングをドラッグで置くだけ**で完成する。
副産物として、絶対位置で書くため第2部 §10-1 の累積ズレも Timeline 経路では消える。

### 16-1. 実装状況 (2026-10-19)

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | `fcpxml_builder`: `clips[i]["offset"]` (明示位置) 対応・位置順ソート | 完了 |
| 2 | `fcpxml_builder`: `_fill_gaps` で先頭/途中の余白を `<gap>` として出力 | 完了 |
| 3 | `fcpxml_builder`: 全長を「最終要素の終端」へ変更 (`_total_frames`) | 完了 |
| 4 | `resolve_export`: `build_timeline_spec` を位置保持へ (`_clips_from_base_clips`) | 完了 |
| 5 | `resolve_export`: 字幕を Timeline 座標のまま使う (`_timeline_items_as_is`) | 完了 |
| 6 | `resolve_export`: 除外クリップの WARNING へ「余白 (gap) として出力する」を追記 | 完了 |
| 7 | `resolve_export`: 先頭アンカー (`_add_lead_anchor`) | 完了 (既定 OFF) |
| 8 | `watermark_overlay`: 透かしを本編の span だけに乗せる (`_body_span`) | 完了 |
| 9 | 設定 3 キー (`timeline_layout` / `subtitle.lead_anchor` / `lead_anchor_text`) | 完了 (`config.py` / `setting.json` / `DEFAULT_SETTINGS`) |
| 10 | テスト 14 件追記 (builder 7 / resolve_export 6 / watermark 1) | 完了 |
| 11 | 全テスト `python -m unittest discover -s tests` 1316 件 | OK (既存 1302 件 + 14 件) |

**生成結果の確認** (OP 4 秒 + 本編 5 秒 + 穴 2 秒 + 本編 3 秒 / 字幕 3 件):

```xml
<sequence format="r1" duration="14s" tcStart="0s" tcFormat="NDF">
  <spine>
    <gap name="余白" offset="0s" start="0s" duration="4s">
      <caption ... offset="90/60s" duration="1s" name="字幕1">OP の上の字幕</caption>
    </gap>
    <asset-clip ref="r2" name="cut1" offset="4s" start="0s" duration="5s">
      <caption ... offset="21/60s" duration="126/60s" name="字幕2">本編の字幕</caption>
    </asset-clip>
    <gap name="余白" offset="9s" start="0s" duration="2s" />
    <asset-clip ref="r2" name="cut2" offset="11s" start="20s" duration="3s">
      <caption ... offset="1230/60s" duration="1s" name="字幕3">穴の後の字幕</caption>
    </asset-clip>
  </spine>
</sequence>
```

- 先頭に OP ぶんの `<gap>` が残り、本編は `offset="4s"` から始まる (従来は `0s`)
- 本編の字幕は親ローカル `21/60s` = ソース 0s + (4.35 − 4.0) → **Timeline と同じ位置**
- 途中の穴も `<gap offset="9s" duration="2s">` として残り、後続がずれない
- OP の上に載っていた字幕も `<gap>` の子として出力される (従来は破棄していた)

### 16-2. 実装時に設計から変えた点

| # | 設計 (§19) | 実装 | 理由 |
| --- | --- | --- | --- |
| 1 | `settings_window.py` へ設定 2 項目の UI を追加 (§19-4) | **UI は追加せず、`DEFAULT_SETTINGS` への既定値追加のみ** | `settings_window.py` に Resolve 出力の設定画面は存在せず、`export.resolve` の全キーが `setting.json` 管理 (`watermark` / `points` と同じ扱い)。新しいタブを起こすのは本件の範囲を超えるため見送り。必要なら別途対応する (§22-6) |
| 2 | (記載なし) | `srt_writer.build_srt` へ `keep_blank` を追加 | `build_srt` は「本文が空白だけの entry を落とす」仕様 (既存テストが固定)。先頭アンカーは意図的に空白なので、明示フラグのみ例外とした |
| 3 | (記載なし) | 完了ログの「タイムライン尺」を余白込みの全長へ (`_timeline_length`) | 余白を保つと「尺の総和」が実際の全長と一致しなくなるため |
| 4 | `test_watermark_covers_body_span_only` を `test_resolve_export.py` へ | `test_watermark_resolve_export.py` へ追加 | 透かしの既存テストと同じ場所に置いた |
| 5 | (記載なし) | 余白上の字幕を落とさなくなった | §18-3 の方針どおり。従来の WARNING 「元動画上に無い字幕 N 件は FCPXML に含めません」は `pack` 時のみ出る |


---

## 17. 原因の詳細

### 17-1. 証拠 (コード)

| # | 箇所 | 内容 |
| --- | --- | --- |
| 1 | `src/timeline/model.py:699` `base_clips()` | クリップを `timeline_start` 昇順で返す。**位置情報はここまで正しく持っている** |
| 2 | `src/timeline/model.py:760` `normalize()` | **並べ替えだけで詰めない** = 余白 (gap) は Timeline の正当な状態 |
| 3 | `src/export/resolve_export.py:501` | `timeline_start` を**使わずに捨てる** |
| 4 | `src/export/resolve_export.py:328` `_clips_from_segments` | 位置を持たない「尺の列」へ変換 |
| 5 | `src/export/fcpxml_builder.py:123` `_quantize_clips` | `cursor_f += dur_f` で**先頭から隙間なく**並べる |
| 6 | `src/export/resolve_export.py:520` `_timeline_subtitle_items` | 字幕も `export_map` (詰めた軸) へ写すので、出力内部では整合する |

`model.py:768` には「差分を足し込まないため誤差が累積しない」とあり、
**Timeline モデル側は絶対位置で持つ設計**になっている。
エクスポートだけが累積方式に戻してしまっている、という形。

### 17-2. 「余白」が生まれる経路

| 経路 | 余白の正体 | 現状 |
| --- | --- | --- |
| オープニング素材を設定している | V1 先頭の OP クリップ (`origin.type = "opening"`) | `:490` で**除外**され、空いた先頭は詰められる |
| エンディング素材 | 末尾の ED クリップ | 除外 (末尾なので位置ズレは起きない) |
| 追加メディア (本編以外) を途中に置いた | その区間 | 除外 → 穴が詰まる |
| Timeline でクリップを削除 / 無効化した | その穴 (`base_clips` は `enabled=False` を返さない) | 穴が詰まる |
| Timeline でクリップを右へ移動した | 先頭・途中の空き | 詰まる |

`:493` の `skipped` で「元動画以外のクリップ N 件は FCPXML に含めません」と WARNING を
出しているが、**「その位置の余白も消える」ことは出力にも案内にも現れていない**。
ここが今回の見落とし。

### 17-3. 症状の出方 (見分け方)

| 状況 | ズレ量 | 向き |
| --- | --- | --- |
| 先頭に OP (10 秒) がある | **全編一律 10 秒** | 字幕が**早く**出る (= 余白が無視された分) |
| 先頭 OP + 途中に穴 (3 秒) | 穴の前は 10 秒、穴の後は 13 秒 | 同上・穴ごとに段階的に増える |
| 余白なし (先頭から本編) | 0 | 第2部 §10-1 の数十〜数百 ms だけが残る |

「**一番最初に余白があるときが問題**」= 先頭 OP/空きがあるケース、
「**大きなズレ**」= OP の尺そのまま、という報告と完全に一致する。

### 17-4. なぜ「カットも字幕も問題なくインポートできた」のか

FCPXML の中では clips も captions も同じ「詰めた軸」で書かれているため、
**FCPXML だけを素のまま取り込むと整合している**。ズレが見えるのは

- 取り込んだ後にオープニングを先頭へ置いた (= 本編が余白ぶん右へ動く) とき
- SRT サイドカーを、余白のあるタイムラインへ別途 Import Subtitle したとき
- autoedit の Timeline 画面で見えている位置と見比べたとき

であり、これも報告と一致する。

---

## 18. 方針

1. **Timeline 経路は「詰め直さない」。** 各ベースクリップを自分の `timeline_start` の
   位置へ書き、空き区間は `<gap>` として明示する。
   → Resolve のタイムラインが autoedit の Timeline と同じ座標になる。
2. **字幕も写像しない。** 字幕クリップは既に Timeline 座標 (`timeline_start`) を
   持っているのでそのまま使う。`TimeMap` による詰め直し (`_timeline_subtitle_items`) は不要になる。
3. **余白に載っている字幕は捨てない。** `<gap>` へ接続要素として付ける
   (FCPXML の `gap` は接続要素を持てる)。現状「元動画上に無い字幕 N 件は含めません」で
   消していたぶんが、位置を保ったまま出力されるようになる。
4. **絶対位置で書く。** `offset` を累積ではなく各クリップの位置から直接量子化するので、
   Timeline 経路では第2部 §10-1 の累積ズレが構造的に発生しない。
5. **クリップ用 (字幕一覧) / アーカイブ用 (結果画面) は現状維持。**
   こちらは「残す区間の列」しか情報が無く、詰めるのが仕様どおり
   (余白の概念が無い)。第2部 §12-1 のアンカリング修正だけを適用する。
6. **旧挙動へ戻せるようにする。** `export.resolve.timeline_layout`
   (`"preserve"` 既定 / `"pack"` 従来) を設定へ追加する。余白が無い Timeline では
   どちらでも**出力は同一**になるため、既定変更の影響は余白があるときだけに限定される。

---

## 19. 変更点

### 19-1. `src/export/fcpxml_builder.py` — 明示位置と `<gap>`

**(a) `spec["clips"][i]["offset"]` (タイムライン秒) を任意キーとして受ける**

```python
# クリップ列を整数フレームへ量子化する
#   offset 有り (Timeline 経路): 各クリップの位置を直接量子化する (累積しない / §18-4)
#   offset 無し (クリップ/アーカイブ経路): 従来どおり量子化済み尺の累積で並べる
def _quantize_clips(clips, fps):
    quantized = []
    cursor_f = 0
    for index, clip in enumerate(clips, 1):
        dur_f = max(to_frames(clip.get("duration", 0.0), fps), 1)
        offset = clip.get("offset")
        offset_f = cursor_f if offset is None else max(to_frames(offset, fps), 0)
        quantized.append({..., "offset_f": offset_f, "dur_f": dur_f})
        cursor_f = offset_f + dur_f
    return sorted(quantized, key=lambda fc: fc["offset_f"])
```

**(b) 空き区間を `<gap>` として出力する**

```python
# spine の空き区間を <gap> として埋める (FCPXML の空白の正式表現 / §18-1)
# ・先頭の余白   : オープニングを置く場所として残す
# ・途中の余白   : 削除/無効化したクリップ・追加メディアの区間
# gap も接続要素 (字幕/透かし) の親になれるため、余白上の字幕を捨てずに出力できる。
def _fill_gaps(fclips):
    ...
```

- `<gap name="余白" offset="..." start="0s" duration="..."/>` を spine 直下へ挿入する。
- `fclips` へ `{"is_gap": True}` の要素として混ぜ、`_assign_to_clips` /
  `_place_text_element` は**今のまま**動く (gap は `start_f = 0` 扱い)。
- クリップが重なっていた場合 (V1 では通常起きない) は WARNING を出し、先着優先で並べる。

**(c) シーケンス全長の決め方**

```python
# タイムライン全長 = 最終要素の終端 (従来は尺の総和。余白を含むため総和では足りない)
def _total_frames(fclips):
    return max((fc["offset_f"] + fc["dur_f"] for fc in fclips), default=0)
```

余白が無い場合は総和と一致するため、**既存出力は不変**。

### 19-2. `src/export/resolve_export.py` — 位置を保って spec を作る

**(a) `build_timeline_spec` (`:473`)**

```python
    # Timeline 上の位置をそのまま写す (詰め直さない / resolve5 §18-1)
    # オープニング等を除外した「空き」は <gap> として残り、Resolve 側で素材を置ける。
    if cfg["timeline_layout"] == "preserve":
        spec["clips"] = [{
            "offset": c.timeline_start,          # ← これまで捨てていた位置
            "start": c.source_in,
            "duration": c.source_out - c.source_in,
            "name": f"cut{i}",
        } for i, c in enumerate(body_clips, 1)]
        items = _timeline_items_as_is(timeline)  # 字幕は Timeline 座標のまま
    else:
        # 従来 (詰める)。余白が無い Timeline では preserve と同一結果になる。
        segments = [(c.source_in, c.source_out) for c in body_clips]
        spec["clips"] = _clips_from_segments(segments, "cut")
        items = _timeline_subtitle_items(timeline, segments, body_media_id)
```

**(b) 新規 `_timeline_items_as_is(timeline)`**

```python
# 字幕クリップを Timeline 座標のまま items へ変換する (写像しない / §18-2)
# 余白 (OP 区間など) に載っている字幕も位置を保って出力する (gap へ接続される / §18-3)。
def _timeline_items_as_is(timeline):
    items = []
    for clip in timeline.subtitle_clips():
        item = clip.to_item()
        item["start"] = clip.timeline_start
        item["end"] = clip.timeline_start + clip.duration
        items.append(item)
    return items
```

- `_timeline_subtitle_items` (`:520`) は `"pack"` 用に**残す** (削除しない)。
- 除外したクリップの WARNING (`:494`) へ「その区間は余白 (gap) として出力します」を追記する。

**(c) 先頭アンカー字幕 (保険 / 任意)**

上記で「autoedit の Timeline と同じ座標」になるが、**Resolve 側の字幕インポータが
「先頭の空きを詰めて最初の字幕を 0 秒へ寄せる」挙動を持っていた場合はこれでも残る**。
その保険として、先頭に 1 フレームの空白字幕を置けるようにする。

| 設定 | 既定 | 内容 |
| --- | --- | --- |
| `export.resolve.subtitle.lead_anchor` | `false` | 最初の字幕が 0 秒より後のとき、0 秒へ空白字幕 (caption / SRT) を 1 件足す |
| `export.resolve.subtitle.lead_anchor_text` | `"　"` (全角空白) | アンカーの本文。空文字だと取り込み時に捨てられる実装があるため既定は全角空白 |

**どちらが効いているかの判定**: §19-1/19-2 を入れて出力し、
*(1)* Resolve のタイムラインで**本編クリップの開始位置に余白が残っている**か →
残っていれば autoedit 側は正しく出せている。
*(2)* それでも字幕だけ 0 秒へ寄るなら Resolve の正規化挙動 → `lead_anchor = true` で解決する。

### 19-3. `src/modules/watermark_overlay.py` — 透かしの区間 (必須の追従修正)

`add_resolve_clip` (`:184`) は `duration = 尺の総和` / `offset = 0.0` で透かしを置いている。
余白を保つと「総和」では足りず、先頭余白にも透かしが乗る。

```python
    # 透かしは本編の先頭から末尾までを覆う (余白ぶんは含めない / resolve5 §19-3)
    starts = [float(c.get("offset") or 0.0) for c in clips]
    ends = [float(c.get("offset") or 0.0) + float(c.get("duration") or 0.0) for c in clips]
    offset, duration = min(starts), max(ends) - min(starts)
```

(第2部 §12-7 では「透かしは無変更」と書いたが、§19 の位置保持に伴い**ここだけ変更が必要**。)

### 19-4. `src/settings/setting.json` / `config.py` / `settings_window.py`

```json
"export": {
  "resolve": {
    "timeline_layout": "preserve",
    "subtitle": {
      "lead_anchor": false,
      "lead_anchor_text": "　"
    }
  }
}
```

| キー | 既定 | 意味 |
| --- | --- | --- |
| `timeline_layout` | `"preserve"` | `"preserve"`=Timeline の位置をそのまま出力 (余白は `<gap>`) / `"pack"`=従来 (0 秒から詰める) |
| `subtitle.lead_anchor` | `false` | 先頭 0 秒へ空白字幕を 1 件足す (§19-2c の保険) |
| `subtitle.lead_anchor_text` | `"　"` | アンカーの本文 |

`resolve_config()` へ 3 キーを追加 (欠落時は既定へフォールバックするので
**既存の setting.json は無変更で動く**)。設定画面には `timeline_layout` を
コンボ (「Timeline の位置を保つ」/「先頭から詰める」) として出し、
`lead_anchor` はチェックボックスとして出す。`lead_anchor_text` は画面に出さない。

### 19-5. テスト

`tests/test_fcpxml_builder.py`:

| # | テスト | 内容 |
| --- | --- | --- |
| 1 | `test_explicit_offset_is_used_as_timeline_position` | `clips[i]["offset"]` を与えると `asset-clip/@offset` がその位置になること (累積しないこと) |
| 2 | `test_leading_gap_is_emitted` | 先頭 10 秒が空くと `<gap offset="0s" duration="10s">` が spine 先頭へ入り、本編が `offset="10s"` から始まること |
| 3 | `test_middle_gap_is_emitted` | 途中の穴が `<gap>` になること |
| 4 | `test_subtitle_on_gap_is_anchored_to_gap` | 余白上の字幕が `<gap>` の子として出ること (捨てられないこと) |
| 5 | `test_sequence_duration_includes_gaps` | シーケンス全長が「最終要素の終端」になること |
| 6 | `test_output_unchanged_without_offsets` | `offset` を与えない従来 spec では現行と同一 XML になること (後方互換の固定) |

`tests/test_resolve_export.py`:

| # | テスト | 内容 |
| --- | --- | --- |
| 7 | `test_timeline_preserves_leading_opening_space` | OP 10 秒ありの Timeline を出力 → 本編クリップが 10 秒から始まり、字幕も 10 秒以降に出ること (**現行実装では 0 秒から始まる = 今回の不具合**) |
| 8 | `test_timeline_subtitles_keep_timeline_coordinates` | 字幕の時刻が Timeline の `timeline_start` と一致すること |
| 9 | `test_timeline_srt_matches_fcpxml_positions` | SRT の時刻も余白を含む Timeline 座標になること |
| 10 | `test_timeline_layout_pack_keeps_old_behavior` | `timeline_layout="pack"` で従来どおり 0 秒から詰まること |
| 11 | `test_watermark_covers_body_span_only` | 透かしが先頭余白を含まず、本編の先頭〜末尾を覆うこと |
| 12 | `test_lead_anchor_adds_zero_second_caption` | `lead_anchor=true` で 0 秒へ 1 フレームの空白 caption と SRT 1 件目が入ること |

### 19-6. 第2部との関係 (優先順位の更新)

| 順 | 項目 | 効果 | 備考 |
| --- | --- | --- | --- |
| **1** | §19-1/19-2 位置保持 + `<gap>` | **余白ぶんの大きなズレが解消** | 今回の本命 |
| 2 | §19-3 透かしの区間 | 1 と同時必須 | — |
| 3 | 第2部 §12-1 アンカリング (量子化の統一) | 残る数十〜数百 ms を 8.3ms 以下へ | クリップ用/アーカイブ用経路では引き続き必要。Timeline 経路は 1 で絶対位置になるため自然に解消 |
| 4 | 第2部 §12-2/12-3 タイムベース (実 fps) | NTSC 素材の比例ズレ | 素材が 29.97/59.94 のときのみ |
| 5 | §19-2c `lead_anchor` / 第2部 §12-4 `subtitle_offset_sec` | 残差の保険 | 1〜4 で消えなければ使う |

---

## 20. 変更ファイル

| ファイル | 内容 | 規模 |
| --- | --- | --- |
| `src/export/fcpxml_builder.py` | 明示 offset 対応、`<gap>` 生成、全長計算 | 中 (約 60 行) |
| `src/export/resolve_export.py` | `build_timeline_spec` の位置保持、`_timeline_items_as_is`、先頭アンカー、WARNING 追記 | 中 (約 50 行) |
| `src/modules/watermark_overlay.py` | 透かしの区間を本編の span へ | 小 (約 5 行) |
| `src/export/config.py` | 設定 3 キー | 小 |
| `src/settings/setting.json` | 既定値 3 キー | 小 |
| `src/settings/settings_window.py` | 設定 2 項目の UI | 小 |
| `tests/test_fcpxml_builder.py` | テスト 6 件 | 小 |
| `tests/test_resolve_export.py` | テスト 6 件 | 小 |

---

## 21. 確認手順

1. `python -m unittest tests.test_fcpxml_builder tests.test_resolve_export tests.test_watermark_resolve_export`
2. 全テスト `python -m unittest discover -s tests` が通ること。
3. **オープニングを設定した Timeline** で出力し、生成 XML が
   - `<spine>` の先頭に `<gap ... duration="(OP の尺)">` を持つこと
   - 最初の `asset-clip` の `offset` が OP の尺と一致すること
   - 最初の `caption` が OP の尺より後にあること
   - SRT の 1 件目も同じ時刻であること
4. **実機**: Resolve へ取り込み、
   - 本編の前に OP ぶんの空白があること → そこへ OP 素材を置く
   - 置いた後に**先頭・中間・末尾のすべてで**口と字幕が合うこと
   - 字幕を SRT で別途 Import Subtitle しても同じ位置に入ること
5. **オープニング無し**の Timeline で、修正前後の XML が**完全一致**すること (後方互換)。
6. 途中のクリップを 1 つ無効化して出力 → その区間が `<gap>` になり、後続の字幕がずれないこと。

---

## 22. 残件・確認事項

| # | 内容 | 扱い |
| --- | --- | --- |
| 1 | **OP/ED 素材そのものを FCPXML へ含めるか。** 現状は単一ソース前提 (`_ASSET_ID` 固定) のため除外している。§19 で「正しい尺の空き」が残るので手置き 1 操作で済むが、複数 asset 対応にすれば完全自動になる | 影響範囲が広いため今回は対象外。要望があれば別途設計 |
| 2 | Resolve が `<gap>` 上の接続要素 (字幕) を字幕トラックへ取り込むか (§18-3)。取り込まない場合は、余白上の字幕のみ従来どおり落として WARNING へ戻す | 実機確認。既定挙動は「出力する」で設計 |
| 3 | Resolve 側の字幕インポータが先頭の空きを正規化するか (§19-2c)。する場合は `lead_anchor` を既定 `true` へ | 実機確認 (§21-4 の結果で判断) |
| 4 | `timeline_layout` の既定を `"preserve"` にして良いか。余白が無い Timeline では出力が変わらないため影響は限定的だが、既に「詰まった出力」を前提に運用している場合は `"pack"` を選ぶ必要がある | 方針の合意 |
| 5 | 余白が極端に長い (例: 本編まで 10 分空いている) 場合の扱い。現設計はそのまま `<gap>` にする | 実害があれば閾値設定を検討 |
| 6 | **設定画面の UI (§16-2 の 1)。** `timeline_layout` / `lead_anchor` は `setting.json` 直編集のみで、設定画面には出していない (Resolve 出力のタブが未整備のため)。設定画面から触れるようにするかどうか | 方針の合意。必要なら Resolve 出力タブの新設として別途対応 |
