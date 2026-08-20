# resolve5（ver3） — アーカイブ用 Timeline 編集画面 修正設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request5.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/CLAUDE.md`「いきなり実装を開始しない」）。
* 目的は **アーカイブ切り抜きの編集画面を、クリップ用と同じ Timeline 編集画面に置き換え、
  採点グラフを載せる**こと。採点ロジック・切り出し・焼き込み・結合の各処理は変更しない。
* 記載の判断はすべて **実装済みコードの実測**に基づく。決めきれない点は推測実装せず
  **§9 確認事項** に列挙した。
* **確認事項 Q1〜Q7 はすべて回答済み**（§1.1）。本書の内容で実装に着手できる。

---

## 1. 要望（request5.md）と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **A1** | 「クリップ用」と同じ Timeline 編集画面を「アーカイブ用」でも表示する | 機能追加 |
| **A2** | アーカイブ固有として **採点グラフ**を表示する | 機能追加 |
| **A3** | 採点グラフは **画面最上部**に置く（「いまのところは」＝暫定配置） | 配置 |

要望文に無いが、実装に必ず要る論点（現行アーカイブ画面が持っている機能の行き先）:

| ID | 論点 | 現行の担い手 |
|---|---|---|
| **A4** | クリップの **使用可否**（チェックを外すと焼き込み対象外） | `ArchiveResultWindow` のクリップリスト |
| **A5** | クリップごとの **テーマ文字列**（イントロカード＋左上タグ / resolve19） | `SubtitleEditorWidget` のテーマ欄 |
| **A6** | **DaVinci Resolve 出力**（使用クリップ全件・VOD 基準） | `ArchiveResultWindow` の出力ボタン |
| **A7** | 採点グラフのマーカクリック → 対象クリップへ移動 | `ScoreGraphWidget.clip_selected` |

### 1.1 レビュー回答による確定事項（2026-08-10）

| # | 決定 | 反映先 |
|---|---|---|
| **Q6** | **Timeline 上には選ばれたクリップを全て並べる**（1 本の Timeline） | §3-1 / §5.3 |
| **Q1** | ↑により **案A（全クリップを 1 本に並べる）** を採用。当初回答の「切り替え方式（案B）」は Q6 と両立しないため差し替える（§1.2） | §3-1 |
| **Q2** | 使用可否は **チェックで除外**（Timeline から消さない） | §3-4 |
| **Q3** | Resolve 出力は **現行どおり**（使用クリップ全件・VOD 基準） | §5.7 |
| **Q4** | 採点グラフの **再生ヘッド追従は入れない**（クリップ区間の強調まで） | §3-5 |
| **Q5** | プロジェクト JSON の保存は **どちらでもよい** → **保存する**（§3-6 の理由による） | §3-6 |
| **Q7** | 採点グラフは **ドック化**。既定は最上部、左右下へ移動・切り離し可、配置は次回へ引き継ぐ | §3-7 |

### 1.2 Q1（案B）から Q6（案A）へ差し替えた理由

初版では案B（クリップごとに Timeline を持ち画面で切り替える）を推奨した。理由は
「案A ではテーマ演出・個別出力・結合設定が壊れる」ことだった。
**この前提は §3-8 の分割方式で解消できる**ことが分かったため、Q6 のご指示どおり案A を採る。

* 1 本の Timeline に並べても、**レンダリング時にクリップ境界で切り直して**
  従来の `_burn_one → _decorate_clip → _combine` へ流せる（実測確認済み / §3-8）。
* 利用者から見た「切り替え」（グラフのマーカで対象クリップへ移動）は案A でも成立する。
  変わるのは内部表現だけで、**Q1 の回答が意図した操作感は失われない**。
* 案A の方が構造は単純になる（Timeline も Undo 履歴も 1 つ。画面の作り替えが不要）。

---

## 2. 現状分析（実測）

### 2.1 クリップ用（ver3 Timeline 経路）

`pipeline_runner._run_timeline()`（`src/pipeline/pipeline_runner.py:150`）:

```
音声解析・正規化 → 音量解析 → 無音「検出」(編集点) → 音声認識
 → builder.build() で Timeline 構築 → project_io.save()
 → timeline_review_callback (= TimelineReviewBridge → TimelineEditorDialog)
 → renderer.render() → output_writer.run()
```

* `builder.build(input_path, media_path, keep_segments, subtitle_items, settings, profile, …)`
  （`src/timeline/builder.py:248`）は **OP → 本編（残す区間）→ ED** の V1、リンク A1、S1 字幕を組む。
  **素材 1 本ぶん**を組む関数のため、アーカイブ（素材 N 本）ではそのままは使えない（§5.3）。
* `renderer.render(timeline, context)`（`src/timeline/renderer.py:34`）は Timeline だけを入力に
  1 本の動画を書き出す。`context` から使うのは **settings / working_dir / allocate_intermediate /
  progress_subcallback / set_current_video_path** のみ（= `PipelineContext` を作れば流用できる）。
* `renderer._build_segments()` は `timeline.base_clips()` を使う。**`enabled=False` のクリップは
  レンダリング対象から自動的に外れる**（`model.py:639`）。使用可否（A4）にそのまま使える。
* 画面は `TimelineEditorDialog(timeline, settings, work_dir, asr_audio_path)`。
  構成は `上部 = QSplitter[プレビュー | インスペクタ] / 下部 = TimelinePanel / 最下段 = ボタン列`。

### 2.2 アーカイブ用（現行 R2 一括フロー）

`ArchiveTabWidget._on_analyze_done()` → `ArchiveClipWorker` → `clip_writer.write_clips()`
（`src/archive/clip_writer.py:503`）:

```
① prepare  : TOP5 の各クリップを 区間切り出し → ラウドネス正規化 → 音量解析 → 無音カット(実カット) → 文字起こし
             → prepared[] = {index,start,end,score,prepared_path,profile,eff_cfg,items,keep_segments}
② レビュー  : result_callback(prepared, curve) = ArchiveResultBridge → ArchiveResultWindow
             → edited[] = {index, items, theme, use}
③ burn     : クリップごとに 字幕焼き込み(_burn_one) → テーマ演出(_decorate_clip) → parts 化
④ 結合     : [Opening?] + (intro?+本編)群 + [Ending?] を 1 本へ (_build_combine_parts / _combine)
```

* **無音カットは実カット**（`_silence_cut()` が `silence_cutter.run()` を呼び `prepared_path` を作る）。
  ただし残す区間は `keep_segments` として戻り値に含まれており、
  **Timeline 構築に必要な材料は既に揃っている**。
* テーマ演出はクリップ単位の ffmpeg 合成（`_build_intro_card` / `_build_tag_overlay`）で、
  **焼き込み後の本編に対して**適用される。Timeline の表現力の外にある。
* 出力は `combine.enabled` / `keep_individual` によって
  「結合 1 本」「個別クリップ群」「両方」の 3 通りに分岐する。

### 2.3 2 経路の差分（Timeline 化に効くところ）

| 観点 | クリップ用 | アーカイブ用 |
|---|---|---|
| 素材 | 入力動画 1 本 | VOD 1 本から TOP5 区間を切り出した **クリップ N 本** |
| 無音カット | 編集点のみ（実カットしない） | **実カット済み**（`prepared_path`） |
| 字幕の時間軸 | 本編を詰めた後の時間軸 | 同左（`prepared_path` 基準）＝ **同じ規約** |
| OP/ED | Timeline の V1 先頭・末尾 | **結合時に 1 回だけ**（クリップごとには付けない） |
| 追加演出 | なし | **イントロカード / 左上タグ**（クリップごと） |
| 出力 | 1 本 | 結合 1 本 / 個別 N 本 / 両方 |

### 2.4 採点グラフ（A2 / A3）

`ScoreGraphWidget`（`src/gui/score_graph_widget.py`）は QtCharts 製で、
`set_data(curve, clips)` に **VOD 時間軸**の窓スコア列と TOP5 マーカを与える。
マーカ／折れ線クリックで最寄りクリップの `index` を `clip_selected` で通知する。
**このウィジェットは無改造で再利用できる**（横軸は VOD 秒のまま）。

---

## 3. 方式選定

### 3-1.【核心】Timeline の持ち方 ── **1 本に全クリップを並べる（案A）**

回答 Q6 のとおり、選ばれたクリップを **TOP 順に 1 本の Timeline へ並べる**。

```
V1: [clip1 の残す区間…] [clip2 の残す区間…] [clip3 の残す区間…] …
A1: 各 V1 クリップにリンク
S1: 各クリップの字幕 (クリップの開始位置ぶんだけ後ろへずらす)
```

* 各 V1 クリップの `origin` に **どのアーカイブクリップ由来か**を書く:

```python
origin = {"type": "silence_cut", "archive_clip_index": 3, "segment_index": 0}
```

* この印があるので、**レンダリング時にクリップ単位へ切り直せる**（§3-8）。
  テーマ演出・個別出力・結合設定はすべて現行のまま生き残る。
* OP/ED は Timeline に入れない（結合時に 1 回だけ付ける現行仕様のまま / §3-3）。

得られること・失うもの:

| | 内容 |
|---|---|
| 得 | クリップを跨いだ編集（並べ替え・別クリップへの素材差し込み・またぎトリム）ができる |
| 得 | Timeline も Undo 履歴も 1 つで済み、画面の作り替え（切り替え処理）が要らない |
| 注意 | クリップの境界は `origin.archive_clip_index` の**連続した並び**で決まる。ある区間を別クリップの間へ移動すると、その区間は独立したグループになる（＝そのクリップのテーマが適用される。§3-8 の grouping 規則） |

### 3-2. Timeline に載せる素材

prepare フェーズは `raw.mp4 →(正規化) normalized.mp4 →(無音カット) prepared_path` を作る。
メディアプールには **クリップごとの `normalized.mp4`（無音カット前・正規化済み）** を
`m1…mN` として登録する。

* 理由: `keep_segments` は「`normalized.mp4` の中で残す区間」であり、そのまま
  `source_in` / `source_out` に使える。実カット済みの `prepared_path` を素材にすると
  **一度カットした部分を戻せない**。
* `prepared_path` は文字起こしとプレビューに引き続き使う（`items` の時間軸はこちら基準）。
* 現行 `_silence_cut()` は `normalized.mp4` のパスを返していないため、
  `prepared[]` へ `normalized_path` を追加する（§5.2）。
* 元 VOD を 1 つの素材にする案は採らない。クリップごとのラウドネス正規化
  （`loudness_normalizer.normalize_file`）の結果が使われなくなるため。

### 3-3. OP/ED を Timeline に含めない方法 → **`builder.py` の改造は不要**

`builder.resolve_material()` は `general.opening_enabled` / `ending_enabled` を見る
（`builder.py:215-236`）。アーカイブ側は `_build_clip_settings()` が作る **`clip_settings`** で動き、
そこでは既に次が設定済みである（`clip_writer.py:66-79`）:

```python
general["opening_enabled"] = False   # OP/ED は結合段階で 1 回だけ付ける (resolve18 §3-2)
general["ending_enabled"] = False
general["output_directory"] = workdir
```

Timeline 構築でも同じ `clip_settings` を使えば OP/ED は入らない。
`builder.py` へ引数を足す必要はなく、クリップ用への影響はゼロ。

### 3-4. テーマ(A5)・使用可否(A4) の置き場所

Timeline 編集画面には「クリップ全体に効く属性」を置く場所が無いため、
**採点グラフの下に “クリップバー” を新設**して受ける（§5.4）。

* **対象クリップ**は「再生ヘッドが乗っているグループ」とする。グラフのマーカや
  クリップ選択コンボで移動すると、そのままバーの表示も切り替わる（選択状態を別に持たない）。
* **使用可否**は、そのグループの V1 クリップの `enabled` を落とす方式にする（回答 Q2）。
  * Timeline 上ではグレー表示になる（`timeline_view._clip_color()` が
    `enabled=False` を灰色で描く既存挙動）。
  * レンダリング対象からも自動で外れる（`base_clips()` / §2.1）。
  * **クリップを消さない**ので、チェックを戻せば元通りになる。
* **テーマ**は Timeline のモデルではなく画面側の辞書 `{clip_index: theme}` で持ち、
  完了時に `edited[]` へ載せる（現行と同じ扱い）。

### 3-5. 採点グラフと Timeline の連動（A7 / 回答 Q4）

* **グラフ → Timeline**: `clip_selected(index)` で **そのクリップの先頭へ再生ヘッドを移動**し、
  Timeline を該当位置へスクロールする（`TimelinePanel._ensure_visible()` が既にある）。
* **Timeline → グラフ**: 再生ヘッドが乗っているクリップの VOD 区間を**帯で強調**する。
  更新は「乗っているクリップが変わったとき」だけに絞る。
* **再生ヘッド追従（現在位置の線）は入れない**（回答 Q4）。QtCharts の再描画が重く、
  Timeline のドラッグ中に引きずられるため。

### 3-6. プロジェクト JSON の保存（回答 Q5 = どちらでもよい → **保存する**）

クリップ用は `project_io.save()` で編集内容を JSON に残している。アーカイブでも保存する。

* 理由: アーカイブは **VOD の DL から採点・文字起こしまでで数十分**かかる。
  焼き込みで失敗したとき、編集内容が消えると同じ時間を再度払うことになる。
* 保存先は `project_io.default_project_path(settings, input_path)`（＝ VOD 名基準）。
  失敗しても処理は止めない（現行 `_save_project()` と同じ扱い）。
* 素材パス（`normalized.mp4`）は一時領域のため、**保存した JSON を後から開いても素材は無い**。
  記録用と割り切り、読み込み機能は今回作らない（§9.1 の注記）。

### 3-7. 採点グラフのドック化（A3 / 回答 Q7）

「いまのところは最上部」という含みのため、**位置を固定せず動かせる形**にする。

**制約**: `QDockWidget` は `QMainWindow` にしか追加できない。一方でレビュー画面は
`dialog.exec()` でワーカーをブロックする必要があり `QDialog` を外せない。

**実測で確認した解法**（`QDialog` の中に `QMainWindow` を子ウィジェットとして置く）:

```
QDialog (exec() できる)
└─ QVBoxLayout
   ├─ QMainWindow            ← _wrap_content() が返す器
   │   ├─ DockWidget "採点グラフ"      (既定 = 上。左右下へ移動可・切り離し可)
   │   └─ CentralWidget = クリップバー + プレビュー/インスペクタ + Timeline
   └─ ボタン列 (元に戻す / やり直す / Resolve 出力 / 完了 / 中止)
```

実測結果（2026-08-10・offscreen で確認）:

| 確認項目 | 結果 |
|---|---|
| `QDialog` 内の `QMainWindow` へ `addDockWidget()` | 可（既定 `TopDockWidgetArea`） |
| `addDockWidget(RightDockWidgetArea, dock)` で右へ移動 | 可 |
| `saveState()` / `restoreState()` | 可（本例で 82 バイト。設定へ保存できる大きさ） |
| テーマ適用 | **ドックのタイトルバーだけ素の外観のまま**。QSS に `QDockWidget` の指定が無いため（§5.9 で追加） |

* 既定配置は **上（`TopDockWidgetArea`）** ＝ 要望 A3 のとおり。
* 配置は `saveState()` の結果を `setting.json` へ保存し、次回復元する（§7）。
* クリップバーは**ドックに入れず中央側へ固定**する。グラフを閉じても
  使用可否・テーマの操作が残るようにするため。

### 3-8. レンダリング ── **クリップ境界で切り直して従来の焼き込みへ流す**

案A の要。編集済み Timeline を **`origin.archive_clip_index` が連続する塊**へ分け、
塊ごとに 0 起点の Timeline を作って `renderer.render()` に掛ける。

```
編集済み Timeline (1 本)
  └─ split_by_clip()  … archive_clip_index の連続で分割・各グループを 0 起点へ再配置
       ├─ clip1 の Timeline → renderer.render() → body1 → _decorate_clip(テーマ1) → parts
       ├─ clip2 の Timeline → renderer.render() → body2 → _decorate_clip(テーマ2) → parts
       └─ …
             └─ _build_combine_parts() → _combine()   ← 現行のまま
```

**モデル操作だけで成立することを実測で確認済み**（2026-08-10）:

```
全長 30.0 秒 / V1 6 本 / S1 6 本
グループ: [(1, 2), (2, 2), (3, 2)]
  clip1: 0.0-10.0s → 0起点へ再配置 / V1 2本 / S1 2本 / 先頭字幕 1.0s
  clip2: 10.0-20.0s → 0起点へ再配置 / V1 2本 / S1 2本 / 先頭字幕 1.0s
  clip3: 20.0-30.0s → 0起点へ再配置 / V1 2本 / S1 2本 / 先頭字幕 1.0s
```

分割規則:

1. V1 のクリップを `timeline_start` 順に走査し、`archive_clip_index` が変わったところで区切る
   （＝ 同じクリップ由来でも離れて置かれていれば別グループになる）。
2. グループの範囲 `[先頭クリップの開始, 末尾クリップの終了]` に入る
   S1 字幕・V2 以降のオーバーレイを同じグループへ入れる。
3. グループ内の全要素から `先頭クリップの開始` を引いて 0 起点へ直す。
4. `enabled=False` のクリップだけのグループ（＝使用しないクリップ）は **グループごと捨てる**。
5. A1 は V1 へのリンクのため、V1 と一緒に運ぶだけでよい。

* グループ内に隙間があるときの扱い（黒で埋めるか詰めるか）は
  既存の `render.gap_policy` に従う。クリップ用と同じ挙動になる。
* `renderer` / `_decorate_clip` / `_combine` は **一切改造しない**。

---

## 4. 設計方針

1. **採点・切り出し・テーマ演出・結合には手を入れない**。変わるのは「レビュー画面」と
   「クリップ本編の作り方（焼き込み → Timeline レンダリング）」の 2 点のみ。
2. **切り戻せること**。`archive.clip_pipeline.timeline_review = false`（既定 true）で
   現行の `ArchiveResultWindow` に戻せる。`timeline.enabled = false` のときも旧画面を使う。
3. **クリップ用の挙動を変えない**。`builder.build()` / `renderer.py` は無改造、
   `TimelineEditorDialog` の拡張はフック追加のみ。
4. **設定値をハードコードしない**（`docs/CLAUDE.md`）。ドック配置・クリップバーの寸法は
   `setting.json` へ置く（§7）。
5. **失敗してもクリップを失わない**。あるグループのレンダリングが失敗しても、
   そのクリップだけスキップしてログへ残し、残りを処理する。

---

## 5. 詳細設計

### 5.1 新規・変更ファイル一覧（予定）

| 種別 | ファイル | 内容 |
|---|---|---|
| 新規 | `src/archive/timeline_builder.py` | prepared[] → 1 本の Timeline 構築 / `split_by_clip()` / `to_export_entry()` |
| 新規 | `src/gui/timeline/archive_timeline_dialog.py` | `ArchiveTimelineDialog`（採点グラフのドック＋クリップバー） |
| 変更 | `src/gui/timeline/timeline_editor_dialog.py` | `_wrap_content()` / `_decide_button_text()` フック追加（既定挙動は不変） |
| 変更 | `src/archive/clip_writer.py` | `prepared[]` に `normalized_path` 追加 / Timeline 経路の分岐 / `edited` 新形式 |
| 変更 | `src/gui/archive_result_window.py` | `ArchiveResultBridge` に画面切り替え（新旧）を追加 |
| 変更 | `src/gui/theme.py` | `QDockWidget` の QSS（§5.9） |
| 変更 | `src/timeline/builder.py`（`timeline_config` のみ） | `ui.archive_*` の既定補完。`build()` 本体は**変更しない** |
| 変更 | `src/settings/setting.json` | §7 の追加キー |

### 5.2 prepare フェーズの追加情報（`clip_writer._prepare_clips`）

既存の鍵は変えず（旧画面がそのまま動く）、次を足す。

```python
prepared.append({
    …現行どおり…,
    # Timeline の素材にする「無音カット前・正規化済み」クリップ
    "normalized_path": normalized,
    "normalized_duration": ffmpeg_runner.probe_duration(normalized, ffmpeg_cfg),
})
```

### 5.3 Timeline の構築（`src/archive/timeline_builder.py`）

`builder.build()` は素材 1 本ぶんを組む関数のためそのままは使えない。同じ構築規則
（トラック構成・ID 採番・字幕の時刻規約）に**倣った専用関数**を置く。

```python
# 選ばれたクリップを TOP 順に 1 本の Timeline へ並べる (resolve5 §3-1)
# prepared : _prepare_clips の戻り値 (TOP 順)
# clip_settings : OP/ED 無効の設定コピー (§3-3)
def build_archive_timeline(prepared, clip_settings):
    …
    cursor = 0.0
    for entry in prepared:
        media = media_probe.probe(entry["normalized_path"], timeline.next_id("m"),
                                  clip_settings, cfg["media"])
        timeline.media_pool.append(media)
        clip_start = cursor
        for seg_index, (start, end) in enumerate(entry["keep_segments"] or
                                                 [(0.0, entry["normalized_duration"])]):
            duration = end - start
            if duration <= _MIN_SEGMENT_SEC:
                continue
            clip_id = timeline.next_id("c")
            video_track.clips.append(Clip(
                clip_id, media.id, cursor, duration,
                source_in=start, source_out=end, z_order=BASE_Z_ORDER,
                # どのアーカイブクリップ由来かを残す (レンダリング時の分割に使う / §3-8)
                origin={"type": ORIGIN_SILENCE_CUT,
                        "archive_clip_index": entry["index"],
                        "segment_index": seg_index},
            ))
            if media.has_audio:
                audio_track.clips.append(AudioClip(timeline.next_id("a"), clip_id))
            cursor += duration
        # 字幕はクリップ先頭ぶんだけ後ろへずらす (builder._append_subtitles と同じ規約)
        _append_subtitles(timeline, subtitle_track, entry["items"], clip_start,
                          entry["keep_segments"])
    timeline.source["archive"] = {
        "vod_path": …, "clips": [{"index","vod_start","vod_end","score",
                                  "timeline_start","timeline_end"} …],
    }
```

* 構築は **ワーカースレッド側（`write_clips` の prepare 直後）**で行う。
  `media_probe.probe()` は ffprobe 起動を伴い、GUI スレッドでは画面が固まるため。
* 結果は `payload["timeline"]` として画面へ渡す。

### 5.4 画面（`ArchiveTimelineDialog`）

```
┌───────────────────────────────────────────────┐
│ ▤ 採点グラフ                            ⧉ ✕ │ ← ドック (既定=上 / 移動・切り離し可)
├───────────────────────────────────────────────┤
│ [clip3 12:34→13:10 点82.1 ▼] [☑使用] テーマ[__]│ ← クリップバー (中央側に固定)
├───────────────────────────────────────────────┤
│ プレビュー            │ インスペクタ           │
├───────────────────────────────────────────────┤
│ Timeline  [clip1…][clip2…][clip3…]             │ ← 全クリップが並ぶ
├───────────────────────────────────────────────┤
│ [元に戻す][やり直す]      [Resolve出力][完了][中止]│
└───────────────────────────────────────────────┘
```

```python
class ArchiveTimelineDialog(TimelineEditorDialog):

    # timeline: build_archive_timeline の結果 / prepared: クリップのメタ / curve: 窓スコア列
    def __init__(self, timeline, prepared, curve, source_path, settings, work_dir, parent=None):
        self._prepared = list(prepared)
        self._curve = curve or []
        self._themes = {p["index"]: "" for p in prepared}
        self._current = prepared[0]["index"] if prepared else None
        super().__init__(timeline, settings, work_dir, parent=parent)
        # 再生ヘッドの移動でクリップバーの対象を切り替える (§3-4)
        self.controller.playhead_moved.connect(self._on_playhead_moved)

    # 主要部を QMainWindow で包み、採点グラフをドックとして載せる (§3-7)
    def _wrap_content(self, content):
        host = QMainWindow()
        host.setCentralWidget(content)
        self.graph = ScoreGraphWidget()
        self.graph.set_data(self._curve, self._clips_meta())
        self.graph.clip_selected.connect(self.jump_to_clip)
        self.graph_dock = QDockWidget("採点グラフ")
        self.graph_dock.setObjectName("archiveScoreGraph")   # saveState の識別子 (必須)
        self.graph_dock.setWidget(self.graph)
        host.addDockWidget(_dock_area(self._ui_cfg), self.graph_dock)
        self._dock_host = host
        self._restore_dock_state()
        return host

    # 指定クリップの先頭へ再生ヘッドを移動する (グラフのマーカ / コンボから)
    def jump_to_clip(self, clip_index):
        start = self._clip_range(clip_index)[0]
        self.controller.set_playhead(start)      # TimelinePanel が可視域へ追従する

    # 使用可否: そのクリップ由来の V1 クリップの enabled をまとめて落とす (§3-4)
    def _on_use_toggled(self, checked):
        self.controller.set_archive_clip_enabled(self._current, checked)

    # 完了時の戻り値: [{"index","theme","use"}] + 編集済み Timeline
    def result_data(self): …

    def done(self, code):
        self._save_dock_state()
        super().done(code)
```

* `setObjectName()` は **必須**。`saveState()` はドックを objectName で識別するため、
  未設定だと復元できない（Qt が警告を出す）。
* 使用可否の一括切り替えは **コマンド経由**（`TimelineController` に
  `set_archive_clip_enabled()` を足す）にして、Undo で戻せるようにする。
  既存の `SetClipEnabled` コマンドを複数クリップへ適用する形にする。

### 5.5 `TimelineEditorDialog` への拡張（非破壊）

案A では Timeline を差し替えないため、**フックは 2 つだけ**で足りる
（初版で必要としていた `load_timeline()` は不要になった）。

```python
# 主要部 (プレビュー/インスペクタ/Timeline) を包む器を返す。
# 既定はそのまま返す = 現行と同じ素の構成。アーカイブ経路だけが
# QMainWindow で包んで採点グラフのドックを足す (resolve5 §3-7)。
def _wrap_content(self, content):
    return content

# 決定ボタンの文言 (アーカイブは「完了（切り抜き＋字幕焼き込み）」)
def _decide_button_text(self):
    return "決定"
```

`_build_ui()` は、現行 `root` へ直接積んでいるスプリッタを **1 つのウィジェットにまとめてから**
`root` へ入れる形へ整理する。

```python
content = QWidget()                     # ← スプリッタ (上部 + Timeline) をこの中へ
…現行どおり content の中に組み立てる…
root.addWidget(self._wrap_content(content), 1)
root.addLayout(button_row)              # ボタン列は従来どおり root 直下 (ドックの外)
```

クリップ用は `_wrap_content()` が `content` をそのまま返すため、
**ウィジェット階層が 1 段深くなるだけで見た目・挙動は現行と同じ**。

### 5.6 焼き込み（`clip_writer`）の分岐

```python
# 編集済み Timeline をクリップ単位へ切り直し、レンダリング → テーマ演出 → パーツ化する。
# 返り値の形 (burned) は現行の _burn_clips と同一のため、結合以降は無改造で流せる。
def _render_clips(timeline, edited, prepared_by_index, settings, clip_settings,
                  ffmpeg_cfg, workdir, progress_cb, card, fonts_dir):
    burned = []
    groups = archive_timeline_builder.split_by_clip(timeline)   # §3-8
    for pos, (clip_index, sub_timeline) in enumerate(groups, 1):
        meta = prepared_by_index.get(clip_index)
        ed = edited_by_index.get(clip_index, {})
        if meta is None or not ed.get("use", True):
            continue
        clip_dir = os.path.join(workdir, f"clip{clip_index}")
        ctx = PipelineContext(input_path=meta["normalized_path"],
                              settings=clip_settings, working_dir=clip_dir)
        try:
            renderer.render(sub_timeline, ctx)
        except Exception:      # noqa: BLE001 (1 クリップの失敗で全体を止めない / §4-5)
            _logger.exception("clip%d のレンダリングに失敗 → このクリップを飛ばします", clip_index)
            continue
        body = ctx.current_video_path()
        parts, body = _decorate_clip(input_path, body, {...}, ed.get("theme", ""), card, …)
        burned.append({"clip": {...}, "body": body, "parts": parts})
    return burned
```

* 旧画面（`timeline_review=false`）のときは現行 `_burn_clips()` をそのまま使う。
  `edited` に `items` が来るか `timeline` が来るかで分岐する。
* CLI／テスト経路（`result_callback=None`）は、構築した Timeline をそのまま
  `_render_clips()` へ流す（画面なしでも同じ出力になる）。

### 5.7 Resolve 出力（A6）── **現行仕様を維持**（回答 Q3）

* 出す内容は現行どおり **使用クリップ全件・VOD 基準**（`export_archive_result`）。
* 渡す `entries` は現行 `ArchiveResultWindow._export_entries()` と同じ形:

```python
{"index", "start", "end", "keep_segments", "items", "theme", "eff_cfg"}
```

* `keep_segments` と `items` は **編集済み Timeline から作り直す**
  （`timeline_builder.to_export_entry()`）。§3-8 の分割結果をそのまま使えるため、
  グループごとに:
  * `keep_segments` … V1 クリップの `(source_in, source_out)` を `timeline_start` 順に。
    D&D で足した素材（`origin.type == "user_media"`）は元 VOD の区間ではないため**除外**。
  * `items` … S1 の `SubtitleClip` を `{"start","end","text","use","role","font","font_size"}` へ戻す
    （グループ先頭を 0 とした時刻＝現行 `items` と同じ規約）。

### 5.8 スレッド構成（現行と同型）

`ArchiveResultBridge` を拡張し、設定に応じて **新画面 / 旧画面**のどちらかを開く。
ワーカースレッドを `threading.Event` でブロックし、メインスレッドで画面を開く構造は現行のまま。

### 5.9 ドックのテーマ対応（実測で判明した積み残し）

`src/gui/theme.py` の QSS には `QDockWidget` の指定が無く、**タイトルバーだけ素の外観**
（明るいグレー）で浮く。次を `_QSS_TEMPLATE` へ足す:

```css
/* ドック (アーカイブ画面の採点グラフ)。ガラス面の中で浮かないよう周囲へ揃える */
QDockWidget { color: {text.primary}; }
QDockWidget::title {
    background: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    padding: 4px 8px;
}
```

* 閉じる／切り離しのボタンのアイコンは既定のままにする（消すと操作できなくなるため）。
  配色が破綻する場合のみ差し替えを検討し、実機で確認する（§10.2）。
* `theme.is_enabled()` が false（`ui.theme = "system"`）のときは従来どおり OS 既定の外観。

**チェック欄の枠**（実装時に判明・2026-08-10）:
QSS を当てた時点で Qt は `QCheckBox` の既定の枠を描かなくなり、
**未チェックだと四角が一切見えない**（チェック時も素のレ点のみ）。
クリップバーの「このクリップを使用する」が押せる場所として認識できないため、
`QCheckBox::indicator` を QSS へ追加する。

```css
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: {border.width} solid {glass.border};
    border-radius: 3px;
    background: {glass.bg.strong};
}
QCheckBox::indicator:hover    { border-color: {glass.highlight}; }
QCheckBox::indicator:checked  { background: {accent}; border-color: {accent}; }
QCheckBox::indicator:disabled { border-color: {text.disabled}; }
```

> これは本画面に限らず**アプリ全体のチェック欄**に効く（設定画面なども枠が出るようになる）。
> 従来は枠が見えない状態だったため、見た目の改善方向の変更になる。

**QtCharts の所有権**（実装時に判明・2026-08-10）:
`QAreaSeries(upper, lower)` は上下の `QLineSeries` の所有権を取らない。
Python 側で参照を手放すと解放され、**描画時にアクセス違反で落ちる**。
帯と一緒に参照を保持すること（`ScoreGraphWidget._highlight_bounds`）。

---

## 6. 実装手順（フェーズ分け）

| Phase | 内容 | 完了条件 |
|---|---|---|
| 1 | `TimelineEditorDialog` へ `_wrap_content()` / `_decide_button_text()` | クリップ用の画面・挙動が変わらないこと（既存テスト全通過） |
| 2 | `archive/timeline_builder.py`（構築・`split_by_clip`・`to_export_entry`）と `prepared[normalized_path]` | 単体テスト（§10.1）が通る |
| 3 | `ArchiveTimelineDialog`（ドック＋クリップバー＋グラフ連動） | 画面が開き、マーカで再生ヘッドが飛ぶ |
| 4 | `clip_writer._render_clips()` と `edited` 新形式 | 出力が旧経路と同等（§10.3） |
| 5 | 設定キー追加・QSS・切り戻し確認 | `timeline_review=false` で旧画面に戻る |

> Phase 1 と Phase 2 は独立しているため並行して進められる。
> `builder.build()` / `renderer.py` には触れないため、クリップ用は全 Phase を通して無改造。

---

## 7. setting.json 定義（追加分）

```jsonc
"archive": {
    "clip_pipeline": {
        "silence_cut": true,
        "subtitle_review": true,
        "volume_dialog": false,
        // 追加: true=Timeline 編集画面 / false=従来の一括結果画面 (切り戻し用)
        "timeline_review": true
    }
},
"timeline": {
    "ui": {
        // 追加: 採点グラフドックの既定位置 ("top"/"left"/"right"/"bottom")
        "archive_graph_dock_area": "top",
        // 追加: 採点グラフドックの既定の高さ (px。上下配置のとき)
        "archive_graph_dock_height_px": 220,
        // 追加: クリップバーの選択欄の最小幅 (px)
        "archive_clip_selector_width_px": 260,
        // 追加: 利用者が動かしたドック配置 (QMainWindow.saveState の Base64)。
        //       画面を閉じるときに書き込み、次回開いたときに復元する。
        //       空文字なら archive_graph_dock_area の既定配置から始まる。
        "archive_dock_state": ""
    }
}
```

* 既定値は `timeline_config()` / `archive.config` 側で補完し、
  **キーが無い既存 setting.json でも動く**ようにする（`request_autoupdate.md §8` と同じ方針）。
* `archive_dock_state` は利用者の操作結果を書き戻す唯一のキー。保存は
  `settings_window.save_settings()` を使い、**失敗しても画面は閉じる**（ログのみ）。
* 復元に失敗した場合（`restoreState()` が `False`）は既定位置へフォールバックする。
* `timeline.enabled=false` のときは `timeline_review` の値に関わらず旧画面を使う。

---

## 8. 互換性・非破壊の担保

| 対象 | 保証内容 |
|---|---|
| クリップ用パイプライン | 変更なし。`builder.build()` / `renderer.py` は無改造、画面フックは既定で素通し |
| 採点（`archive/pipeline.py`・`scoring.py`） | 変更なし |
| prepare フェーズ | 鍵の**追加**のみ。既存の鍵・値・順序は不変 |
| テーマ演出 / 結合 / 個別出力 | `_decorate_clip` 以降は無変更（§3-8 の分割で従来の入力形に戻す） |
| 旧アーカイブ結果画面 | 残置。`timeline_review=false` で復帰できる |
| 既存 setting.json | 追加キーは既定補完。未記載でも動作する |

---

## 9. 確認事項

### 9.1 すべて回答済み（2026-08-10）

Q1〜Q7 は §1.1 のとおり確定した。実装を止める未確定事項は無い。

補足として本書で決めた運用上の割り切りを 2 点記す（異論があれば実装前に指示が要る）。

* **保存したプロジェクト JSON は記録用**。素材が一時領域の `normalized.mp4` のため、
  後から開いても再編集はできない（§3-6）。再編集まで求める場合は、
  素材を出力先へ残す設計が別途必要になる。
* **クリップ境界は `archive_clip_index` の連続で決まる**（§3-8）。ある区間を
  別クリップの間へ移動すると独立したグループになり、テーマ・使用可否の単位もそこで分かれる。

---

## 10. テスト計画

### 10.1 単体（`tests/test_archive_timeline.py` 新規）

**構築**

* `keep_segments` → V1 クリップの `source_in/out` と尺が一致する
* クリップが **TOP 順に隙間なく並ぶ**（`timeline_start` が前クリップの終端と一致）
* 字幕がクリップ先頭ぶんだけずれて S1 に載る
* `keep_segments=None`（無音カット無効）でクリップ全長 1 本になる
* `clip_settings`（OP/ED 無効）で **OP/ED クリップが増えない**（§3-3 の根拠確認）
* 各 V1 クリップの `origin.archive_clip_index` が正しい

**分割（`split_by_clip`）**

* グループ数・各グループの V1/S1 本数が構築時と一致する
* 各グループが **0 起点**へ再配置される（先頭クリップ・先頭字幕の時刻）
* `enabled=False` だけのグループが除外される
* クリップを跨いで移動したクリップが**独立したグループ**になる（§9.1 の割り切りの確認）

**逆変換（`to_export_entry`）**

* 移動・トリム・分割後の Timeline から `keep_segments` が `timeline_start` 順に出る
* `user_media` 由来のクリップが除外される

**非破壊**

* `test_timeline_commands.py` ほか既存テスト … 全通過

### 10.2 結合（手動・GUI）

* [ ] 採点 → 新画面が開き、**最上部に採点グラフのドック**、Timeline に**全クリップが並ぶ**
* [ ] ドックを左／右／下へドラッグ移動でき、切り離しもできる
* [ ] ドックを閉じてもクリップバー（使用可否・テーマ）は残り、編集を続けられる
* [ ] 閉じて開き直すとドック配置が復元される
* [ ] ドックのタイトルバーが周囲のガラス面から浮いていない。閉じる/切り離しボタンが押せる
* [ ] グラフのマーカクリックで**そのクリップの先頭へ再生ヘッドが飛ぶ**
* [ ] 再生ヘッドを動かすとクリップバーの対象が切り替わる
* [ ] 使用チェックを外すと Timeline 上でグレーになり、出力に含まれない。戻すと元に戻る
* [ ] テーマを入れたクリップにイントロカード＋左上タグが付く（現行と同じ見え方）
* [ ] クリップを跨いだ編集（並べ替え・素材の差し込み）ができる
* [ ] 完了 → 結合 1 本が出力され、`keep_individual` / `combine.enabled=false` も現行どおり
* [ ] Resolve 出力が使用クリップ全件ぶん出て、編集後のカット位置が反映されている
* [ ] `timeline_review=false` で従来の一括結果画面に戻る
* [ ] キャンセルで出力なし・一時ファイルが残らない
* [ ] クリップ用の編集画面が**従来どおり**（ドック無し・見た目と操作が不変）

### 10.3 非破壊の突き合わせ

同一 VOD・同一設定で、**編集を一切行わずに完了**した場合の出力を新旧経路で比較する。
無音カットの区間・字幕の焼き位置・テーマ演出・結合順序が一致すること
（再エンコード経路が変わるためバイト一致は求めない。尺・カット位置・字幕表示時刻で照合する）。
