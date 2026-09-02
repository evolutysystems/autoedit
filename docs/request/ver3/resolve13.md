# resolve13（ver3） — アーカイブ Timeline 編集画面へのセクション追加機能 設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request13.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「実装前に設計を行うこと」）。
* 調査は **2026-09-01 時点の実コード**を読んで行った。推測で書いた箇所は無く、
  要望文から一意に決められない論点はすべて §9「確認事項」へ挙げた
  （`docs/claude.md`「不明点がある場合は推測実装せず設計書へ記載すること」）。
* **採点方式（`archive/scoring.py`）には一切触れない**。要望どおり、
  セクション選定のロジックは現状のままとする（§5.9）。
* 追加する設定値は `setting.json` の `archive.section_add` へ置く
  （`docs/claude.md`「ハードコードは禁止」／§7）。

---

## 1. 要望（request13.md）と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **G1** | アーカイブ用 Timeline 編集画面に**セクションの追加機能**を設ける | 新機能 |
| **G2** | 採点方式によるセクション選定は**変更しない** | 制約 |
| **G3** | 追加したセクションを **元動画（VOD）の時系列として正しい位置**へ挿入する | 新機能 |
| **G3-a** | どのセクションよりも先頭 → Timeline の先頭へ挿入 | 〃 |
| **G3-b** | どのセクションよりも後ろ → Timeline の最後へ挿入 | 〃 |
| **G3-c** | セクションとセクションの間 → 既存セクションの間へ挿入 | 〃 |
| **G4** | 追加したセクションに対して**字幕等の処理を全て行う** | 新機能 |
| **G5** | 既存セクションと**被っていればマージする** | 新機能 |

要望文に無いが、実装に必ず要る論点:

| ID | 論点 | 理由 |
|---|---|---|
| **G6** | 追加区間を**どう指定させるか**（UI） | 要望は「セクションを指定して」までで、指定手段が決まっていない |
| **G7** | セクション番号（`archive_clip_index`）を**振り直すか** | 番号は V1 クリップ・字幕・テーマ・使用可否の紐付けキー。振り直すと全参照が壊れる（§2.3） |
| **G8** | 追加処理は**数十秒〜数分かかる**（切り出し→正規化→音声認識） | 編集画面はメインスレッドで動く。同期実行すると画面が固まる（§2.5） |
| **G9** | 追加操作を **Undo/Redo に載せる**こと | 全編集はコマンド経由という規約（`timeline_controller.py` 冒頭）。ここだけ例外にはできない |
| **G10** | マージ時に**既存セクションへの編集**（トリム・字幕修正・オーバーレイ）をどう扱うか | 要望は「マージする」だけ。作り直すと利用者の編集が消える |
| **G11** | 追加セクションの**スコア**をどう決めるか | 採点を通っていないため `score` が無い。クリップバー・採点グラフが参照する |

---

## 2. 現状分析

### 2.1 セクションはどう作られ、どう Timeline に載るか

```
archive/pipeline.analyze()                     ← 採点（G2: 触らない）
  └─ scoring.select_top_events()               scoring.py:139
       → clips = [{index,start,end,score,use}] （start/end は VOD 秒）
            ↓
archive/clip_writer.write_clips()              clip_writer.py:635
  ├─ ① _prepare_clips()                        clip_writer.py:405  ← 1 セクションぶんの下ごしらえ
  ├─ ② build_archive_timeline(prepared, …)     timeline_builder.py:61
  ├─ ③ result_callback(prepared, curve, timeline)  ← ここで編集画面が開く
  └─ ④ finish_clips() → _render_clips()        clip_writer.py:707 / 568
```

編集画面（`ArchiveTimelineDialog`）は ③ の位置で開く。
**この時点で VOD・作業ディレクトリ・`clip_settings` はすべて生きている**（§2.6）。

### 2.2 セクション番号は既に「VOD 時系列順」で振られている

`build_archive_timeline` のコメントは「TOP 順に並べる」と書いているが、
実際に渡ってくる `prepared` は **VOD の開始時刻順**である。

```python
# scoring.py:150-163（select_top_events の後半）
chosen.sort(key=lambda w: w["start"])        # ← VOD 開始秒で並べ替え
...
merged = _merge_time_sections(padded)
for idx, m in enumerate(merged, 1):          # ← 番号は時系列順に 1,2,3…
```

したがって **「Timeline の並び順 = VOD の時系列順」は既存の不変条件**であり、
G3 はこの不変条件を追加時にも守る、という要求に還元できる。新しい概念は要らない。

### 2.3 セクションの実体は 3 か所に分散している

| # | 置き場 | 持っているもの | Undo 対象 |
|---|---|---|---|
| 1 | Timeline の V1 / A1 / S1 クリップ | 映像・音声・字幕の実体。`origin[ORIGIN_ARCHIVE_INDEX]` でセクション番号を持つ（`timeline_builder.py:42`） | **される** |
| 2 | `timeline.source["archive"]["clips"][]` | `index / vod_start / vod_end / score / media_id / theme / media_role` | **される**（`commands._snapshot` が `source` を deepcopy / `commands.py:95`） |
| 3 | `ArchiveTimelineDialog._prepared` | `normalized_path / normalized_duration / items / eff_cfg / profile / keep_segments` | **されない**（ダイアログのローカル変数） |

セクション番号 `archive_clip_index` は 1 と 2 を繋ぐ唯一のキーであり、
`split_by_clip`（`timeline_builder.py:245`）・`SetArchiveClipEnabled`・`SetArchiveClipTheme`・
`clip_range`・`is_clip_enabled` がすべてこれで引く。**番号の振り直しは全参照の破壊を意味する**（G7）。

3 が Undo に載らないことは、追加機能では実害になる。
「追加 → Undo」でセクションが Timeline から消えても `_prepared` には残り、
クリップバー・採点グラフ・`result_data()`（`archive_timeline_dialog.py:285`）が
存在しないセクションを表示・出力しようとする。**設計で吸収する必要がある**（§3-6）。

### 2.4 1 セクションを用意する処理（G4 の実体）

`_prepare_clips`（`clip_writer.py:405`）のループ 1 周ぶんが、そのままセクション 1 件の下ごしらえである。

```
cut_region(VOD, start, end, raw.mp4)                 区間切り出し（ストリームコピー・速い）
  → loudness_normalizer.normalize_file()             ラウドネス正規化（再エンコード・重い）
  → _apply_volume_analysis()                         カット閾値の確定
  → output_profile.resolve_output_profile()          縦/横の判定
  → subtitle_generator.build_effective_subtitle_cfg()
  → _prepare_edit_points()                clip_writer.py:353
       ├─ silence_cutter.detect_edit_points()        無音カットの編集点（実カットはしない）
       └─ subtitle_generator.recognize_for_timeline()  音声認識 → 字幕 items（最も重い）
```

**G4「字幕等の処理は全て行う」は、この一連をそのまま新セクションへ適用すること**と読める。
処理を書き写すのではなく、この本体を関数へ切り出して共有するのが唯一の正解である
（写すと必ず片方だけ直されて挙動が割れる）。

### 2.5 編集画面はワーカースレッドを止めてメインスレッドで動く

`ArchiveResultBridge`（`archive_result_window.py:264`）は、
ワーカースレッドから `result_requested` を **QueuedConnection** で投げ、
`threading.Event.wait()` で自分を止めている。画面はメインスレッドで `exec()` される。

```
ArchiveClipWorker (別スレッド)      メインスレッド
  write_clips()
    result_callback(...)  ──emit──►  _on_requested() → ArchiveTimelineDialog.exec()
    event.wait() でブロック          （ここで利用者が編集）
```

つまり **編集画面の中で重い処理を直接呼ぶと GUI が完全に固まる**。
セクション追加は §2.4 のとおり音声認識まで走るため、
**専用の QThread ＋ モーダル進捗ダイアログが必須**（G8）。
既存の `ArchiveAnalyzeWorker` / `ArchiveClipWorker`（`archive_tab.py:87 / 163`）が
同じ形（`progress` / `finished_ok` / `failed` シグナル）を持っており、書き方はそれに倣える。

### 2.6 編集画面が今持っていないもの

`ArchiveTimelineDialog.__init__`（`archive_timeline_dialog.py:57`）が受け取るのは
`timeline / prepared / curve / source_path / settings / work_dir / project_path / created_at / mode`。

| 必要なもの | 現状 | 備考 |
|---|---|---|
| VOD のパス | **ある**（`source_path`） | 切り出し元 |
| 作業ディレクトリ | **無い** | `work_dir` は「プレビュー一時ファイル置き場」で、`config.resolve_work_dir()` の永続ディレクトリ（`archive_tab.py:569`）。`write_clips` の `TemporaryDirectory` とは別物 |
| `clip_settings` | **無い** | OP/ED を無効化した設定コピー。`build_clip_settings()` は公開済み（`clip_writer.py:74`）のため画面側でも作れる |

新セクションの `normalized.mp4` は **書き出し（`_render_clips`）まで生きている必要がある**。
`write_clips` の `TemporaryDirectory` 配下へ置くのが寿命として正しく、
永続ディレクトリへ置くとゴミが溜まり続ける。したがって **`workdir` を画面へ渡す配線が要る**（§5.7）。

### 2.7 Undo/Redo はスナップショット方式で `source` まで含む

```python
# commands.py:95-111
def _snapshot(timeline):
    return {"tracks": [...], "media_pool": list(timeline.media_pool),
            "source": copy.deepcopy(timeline.source)}
```

**メディアプール・全トラック・`source` が控えられている**ため、
「メディアを足し、V1/A1/S1 クリップを足し、`source.archive.clips` へ 1 件足す」操作は
**コマンドとして書けば追加の逆操作を書かずに Undo できる**（G9）。
ディスク上の `normalized.mp4` は残るが、作業ディレクトリごと消えるため実害は無い。

### 2.8 書き出しはセクション番号ではなく Timeline の並び順で行われる

`split_by_clip`（`timeline_builder.py:245`）は V1 を `timeline_start` 順に走査し、
`archive_clip_index` が変わったところで区切る。返り値も時系列順。
`_render_clips`（`clip_writer.py:568`）はその順に書き出し、`_build_combine_parts` がその順で結合する。

**したがって出力の並びは Timeline の並びで決まり、番号の大小は関係しない。**
これは G7（番号を振り直さない）を選べる根拠になる。

### 2.9 既に使える部品

| 部品 | 位置 | 使いどころ |
|---|---|---|
| `make_room_for_range()` | `commands.py:478` | 途中へ挿入するとき、以降の全要素を右へずらす（G3-c）。音声トラックとロック中トラックは自動で除外される |
| `_append_subtitles()` | `timeline_builder.py:190` | 追加セクションの字幕を `offset` 付きで S1 へ載せる。クリップ尺での打ち切りも実装済み |
| `_merge_time_sections()` | `scoring.py:114` | 「接触も重なりとみなす」既存の統合規約。G5 の判定をこれに揃えれば規約が 2 つに割れない |
| `renderer.render()` | `renderer.py:40` | 素材はクリップごとに `timeline.media_by_id()` で引く。**1 セクションが複数メディアで構成されていても動く**（§3-4 の前提） |
| `clip_range()` / `clip_entry()` | `timeline_builder.py:437 / 403` | 挿入位置の算出 |

### 2.10 `source.archive.clips[].timeline_start/end` は編集で古くなる

`build_archive_timeline`（`timeline_builder.py:116-130`）が構築時に一度書くだけで、
以降どのコマンドも更新しない。利用者がクリップを動かした時点で実態とずれる。
実際 `clip_range()` は V1 クリップから計算し直しており、この値を使っていない。

**挿入位置の算出でこの値を信用してはならない。**
時系列の順序付けには `vod_start/vod_end`（VOD 基準・編集で変わらない）を使い、
Timeline 上の実位置は `clip_range()` で取り直す（§5.5）。

---

## 3. 方式選定

### 3-1. 追加区間の指定方法（G6）

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | 開始・終了を **H:MM:SS で入力するダイアログ**。初期値は採点グラフで最後にクリックした位置／再生ヘッドから埋める | 実装が軽く、確実に指定できる。既存 UI を壊さない |
| B | 採点グラフ上を**ドラッグして範囲選択** | 直感的だが、QtCharts にラバーバンド選択は無く自前実装が要る。既存の `clicked`→`clip_selected` ハンドラ（`score_graph_widget.py:182`）とも競合する。**§11 の将来拡張へ回す** |
| C | VOD 全体を再生できる別プレビュー窓で範囲指定 | 別のプレビュー基盤を作ることになり、要望に対して過大 |

**A を採用する。** ただし「グラフを見ながら数値を決める」導線は残したいため、
グラフのクリック位置を初期値として引き継ぐ（§5.3）。B は §9 Q1 で確認する。

### 3-2. 挿入位置の決定（G3）

VOD 時刻で順序を決め、Timeline 上の実位置は `clip_range()` で取り直す（§2.10）。

```
新セクション [ns, ne]（VOD 秒）に対し、既存セクションを vod_start 昇順に見て
  最初に  ns < 既存.vod_start  となるセクション S を探す
    見つかった  → S の clip_range()[0] へ挿入        （G3-a: S が先頭なら 0.0 = Timeline 先頭）
    見つからない → Timeline の末尾へ追加             （G3-b）
```

途中へ挿入する場合は `make_room_for_range()` で挿入点以降を右へずらす（G3-c）。
セクションは隙間なく並んでいるため挿入点は必ずセクション境界に一致し、
既存クリップが分割されることはない。

### 3-3. セクション番号は振り直さない（G7）

| 案 | 判定 |
|---|---|
| **A（採用）** 新番号 = `max(既存) + 1` を振り、並び順は Timeline 位置で表す | §2.3 の全参照（V1 origin・字幕 origin・テーマ・使用可否）を壊さない。§2.8 より出力順にも影響しない |
| B | 時系列順に 1..N へ振り直す | V1・S1 の `origin` を全書き換えする必要があり、`_prepared` とも整合を取らねばならない。得られるのは「番号が並ぶ」ことだけ |

**A を採用する。** 代償として、クリップバーと個別出力ファイル名（`clip{index}`）の番号が
時系列と一致しなくなる。クリップバーは **Timeline 位置順に並べ、VOD 区間も併記する**ため
実用上は読める（表示は既に `clip3 0:12:30→0:15:30 点62.1` 形式）。§9 Q2 で確認する。

### 3-4. マージ方式（G5・G10）—— 本書で最も重い判断

前提として、重なり判定は `scoring._merge_time_sections`（`scoring.py:114`）と同じ規約にする。
すなわち **接触（`ns <= 既存.vod_end` かつ `既存.vod_start <= ne`）も統合対象**とする。
規約を 2 つ持つと、採点で統合された結果と手動追加の結果が食い違う。

| 案 | 内容 | 判定 |
|---|---|---|
| A | 和集合 `[min(ns,es), max(ne,ee)]` を**まるごと prepare し直し**、既存セクションを置き換える | 実装は素直だが、**重なったセクションへの編集（トリム・字幕修正・オーバーレイ・テーマ）が全部消える**。処理時間も区間全体ぶんかかる |
| **B（採用）** | **足りない差分だけ prepare** し、既存セクションと同じ番号を付けて隣接配置する。メタデータ（`vod_start/vod_end`・`prepared.start/end`）だけ和集合へ更新する | 既存の編集がそのまま残る。処理も差分ぶんで済む。§2.9 のとおり renderer はセクションが複数メディアでも動く |
| C | マージせず別セクションとして並べる | 要望 G5 に反する |
| D | 重なりを検出したら利用者へ A/B を選ばせる | 毎回問われるのは煩わしい。既定を B にし、A は設定で切り替える（§7）ほうが良い |

**B を採用する。** 具体的には:

```
新 [ns, ne] が既存セクション群 S1..Sk（vod_start 昇順・すべて重なり/接触）に触れるとき、
  代表番号 = S1 の番号（最も小さい vod_start のもの）
  ① 前方差分 [ns, S1.vod_start)        … あれば prepare して S1 の直前へ挿入
  ② セクション間の隙間 (Si.vod_end, Si+1.vod_start) … あれば prepare して両者の間へ挿入
  ③ 後方差分 (Sk.vod_end, ne]          … あれば prepare して Sk の直後へ挿入
  ④ S1..Sk と①②③のすべての archive_clip_index を代表番号へ揃える
  ⑤ source.archive.clips を 1 件へ畳み、vod_start/vod_end を和集合へ更新
  ⑥ prepared[代表番号].start/end も和集合へ更新
```

⑥ は必須である。`_decorate_clip` → `_build_intro_card`（`clip_writer.py:197`）が
**`clip["start"]` を使って VOD からイントロカードを切り出す**ため、
更新を忘れるとマージ後もイントロが古い位置の絵になる。
個別出力のファイル名（`_copy_individual`）も `clip['start']/['end']` を使う。

④ により S1..Sk と差分が Timeline 上で連続した同一番号の並びになるため、
`split_by_clip`（連続する同一 index を 1 グループにする / §2.8）が
**そのまま 1 セクションとして扱う**。分割ロジックの変更は不要。

新区間が既存セクションへ完全に含まれる場合（`es <= ns` かつ `ne <= ee`）は
追加するものが無い。エラーにせず「既にセクションに含まれています」と案内して何もしない。

### 3-5. 重い処理の実行方法（G8）

専用 `QThread`（`_SectionPrepareWorker`）＋ モーダル進捗ダイアログ。
`archive_tab` のワーカーと同じシグナル構成（`progress(float,str)` / `finished_ok(object)` / `failed(str)`）にする。

**キャンセルは工程の切れ目でのみ受け付ける。** 音声認識（faster-whisper）は
途中で止める API を持たないため、実行中の中断はできない。
「キャンセル押下 → 次の工程へ入る前に中止」という粒度になることを UI の文言で明示する（§9 Q5）。

### 3-6. `_prepared` の持ち方（§2.3 の 3 番）

| 案 | 判定 |
|---|---|
| **A（採用）** 画面が見せるセクション一覧を **`source.archive.clips` から導出**し、`_prepared` は「番号 → 準備済みデータ」の**キャッシュ**に降格する | `source` は Undo に載る（§2.7）ため、追加を Undo すれば一覧から自動的に消える。キャッシュに残る古いエントリは参照されないだけで無害 |
| B | `_prepared` も Undo のたびに巻き戻す | `CommandStack` にモデル外の状態を持ち込むことになり、スナップショット方式の前提が崩れる |

**A を採用する。** 影響を受けるのは
`_build_clip_bar` / `_clips_meta` / `_entry` / `result_data` / `_on_export_resolve`
（いずれも現在 `self._prepared` を直接なめている / `archive_timeline_dialog.py:151・175・243・285・297`）。

なお **書き出し側は `prepared` を必要とする**（`_render_clips` が
`prepared_by_index[clip_index]["normalized_path"]` を使う）。
そのため画面は編集結果に **`prepared` も添えて返す**（§5.7）。

---

## 4. 設計方針

1. **採点方式には触れない**（G2）。追加は「採点結果へ後から足す」操作として、既存の下流だけを共有する。
2. **1 セクションの下ごしらえを 1 つの公開関数へ切り出し、初回構築と追加で共有する**（G4）。
   処理を書き写さない。
3. **挿入は VOD 時刻で順序を決め、Timeline 位置は実クリップから取り直す**（G3 / §2.10）。
4. **セクション番号は振り直さない**（G7）。並び順は Timeline 位置が表す。
5. **マージは差分のみ prepare し、既存セクションの編集を保つ**（G5 / G10）。
6. **追加は 1 つのコマンドとして積み、Undo で完全に戻る**（G9）。
7. **重い処理は必ず別スレッド**（G8）。
8. **画面が見せる一覧は `source.archive.clips` から導出する**（§3-6）。
9. 新しい設定は `archive.section_add` へ置き、機能ごと無効化できるようにする（§7）。

---

## 5. 詳細設計

### 5.1 変更ファイル一覧

| ファイル | 変更内容 | 規模 |
|---|---|---|
| `src/archive/clip_writer.py` | `_prepare_clips` のループ本体を `prepare_one_clip()` として公開・切り出し。`write_clips` が `workdir`/`clip_settings` を `result_callback` へ渡し、戻り値の `prepared` を受け取る | 中（既存処理は移動のみ） |
| `src/archive/timeline_builder.py` | `AddArchiveSection` コマンドを追加。`_append_subtitles` を `append_subtitles` として公開 | 大（新規コマンド） |
| `src/archive/config.py` | `section_add_config()` を追加 | 小 |
| `src/archive/project_resume.py` | `run_from_archive_project` が `workdir`/`clip_settings` を渡し、`prepared` を受け取る | 小 |
| `src/gui/archive_result_window.py` | `ArchiveResultBridge` が `workdir`/`clip_settings` を中継 | 小 |
| `src/gui/timeline/archive_timeline_dialog.py` | 「セクション追加…」ボタン、準備ワーカー、一覧の導出元を `source.archive.clips` へ変更 | 大 |
| `src/gui/timeline/section_add_dialog.py` | **新規**。区間指定ダイアログ | 小 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["archive"]["section_add"]` を追加 | 小 |
| `tests/test_archive_section_add.py` | **新規**。§10-2 の単体テスト | 中 |

### 5.2 設定の追加（`config.section_add_config`）

```python
# セクション追加 (ver3 resolve13 §7)
def section_add_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    add = archive.get("section_add", {})
    scoring = archive.get("scoring", {})
    return {
        "enabled": bool(add.get("enabled", True)),
        # 追加ダイアログの既定尺。採点の窓幅へ揃える (別々に持つと意味が割れる)
        "default_length_sec": float(add.get("default_length_sec",
                                            scoring.get("window_sec", 180))),
        "min_length_sec": float(add.get("min_length_sec", 1.0)),
        # 重なり時にマージするか (false = 別セクションとして並べる / 切り戻し用)
        "merge_on_overlap": bool(add.get("merge_on_overlap", True)),
        # マージ方式 "delta"=差分だけ用意 (既定) / "rebuild"=和集合を作り直す (§3-4 案A)
        "merge_mode": ("rebuild"
                       if str(add.get("merge_mode", "")).strip() == "rebuild"
                       else "delta"),
    }
```

### 5.3 区間指定ダイアログ（`section_add_dialog.py`・新規）

```
┌ セクションの追加 ───────────────────────────┐
│ 元動画の区間を指定してください（VOD 全長 5:12:40）    │
│   開始  [ 1:02:30 ]   終了  [ 1:05:30 ]              │
│   長さ  0:03:00                                      │
│   ※ 既存セクションと重なる場合は 1 つに統合されます   │
│                              [ 追加 ]  [ キャンセル ] │
└──────────────────────────────────────────────┘
```

* 入力は `H:MM:SS` / `MM:SS` / 秒数のいずれも受ける（`_fmt` の逆変換を用意する）。
* 初期値: 採点グラフで最後にクリックした時刻（無ければ現在の再生ヘッドが乗るセクションの VOD 時刻）を
  開始とし、終了 = 開始 + `default_length_sec`。VOD 全長でクランプする。
* 検証: `0 <= 開始 < 終了 <= VOD 全長` かつ `終了 - 開始 >= min_length_sec`。
* 重なりが起きる場合は「統合されます」の旨と統合後の区間を **追加前に**表示する（破壊的操作の予告）。

VOD 全長は `ffmpeg_runner.probe_duration(vod, ffmpeg_cfg)` を画面初期化時に 1 回だけ取り、
`self._vod_duration` へ持つ。取得できなければ上限チェックを外す（機能を止めない）。

### 5.4 1 セクションの下ごしらえを共有する（G4）

`_prepare_clips` のループ本体をそのまま関数へ出す。**処理内容は 1 行も変えない。**

```python
# clip_writer.py（新規公開関数）
# セクション 1 件を下ごしらえする (切り出し→正規化→音量解析→編集点検出→文字起こし)。
# _prepare_clips のループ本体をそのまま切り出したもの。初回構築と、編集画面からの
# セクション追加 (ver3 resolve13) で同じ処理を使うために公開する。
# 書き写すと片方だけ直されて挙動が割れるため、必ずこの関数を経由すること。
def prepare_one_clip(input_path, settings, clip_settings, clip, ffmpeg_cfg, workdir,
                     use_timeline=False, progress_cb=None, saved_db=None):
    ...  # 現行 clip_writer.py:414-473 (ループ本体) をそのまま移す
    return {...}   # 現行と同一のキー構成


def _prepare_clips(input_path, settings, clip_settings, used, ffmpeg_cfg, workdir,
                   progress_cb, use_timeline=False):
    prepared = []
    saved_db = settings.get("volume_analysis", {}).get("last_cut_db")
    for pos, clip in enumerate(used, 1):
        ...  # 進捗計算だけ残す
        prepared.append(prepare_one_clip(
            input_path, settings, clip_settings, clip, ffmpeg_cfg, workdir,
            use_timeline=use_timeline, progress_cb=..., saved_db=saved_db))
    return prepared
```

追加時は `use_timeline=True` 固定で呼ぶ（編集画面は Timeline 経路でしか開かないため）。
`clip` には `{"index": 新番号, "start": ns, "end": ne, "score": 推定スコア}` を渡す。

**推定スコア（G11）**: 採点していないため、`curve`（窓スコア列）のうち
`[ns, ne]` と重なる窓の `total` の最大値を採る。窓が 1 つも無ければ `0.0`。
新しい採点は行わない（G2 を侵さない）。

### 5.5 挿入コマンド `AddArchiveSection`（`timeline_builder.py`）

```python
# 準備済みのセクションを VOD 時系列の正しい位置へ挿入する (ver3 resolve13 §3-2)
# entries : prepare_one_clip の戻り値の一覧。マージ時は差分ぶんが複数入る (§3-4 案B)
# merge_into : マージ先の代表セクション番号 (単独追加なら None)
# merged_span: マージ後の VOD 和集合 (start, end)。merge_into が None なら新区間そのもの
class AddArchiveSection(commands.Command):

    label = "セクションの追加"

    def apply(self, timeline):
        ...
```

**apply の手順**

1. **挿入点の決定**（差分 1 件ごと）
   `source.archive.clips` を `vod_start` 昇順に並べ、差分の VOD 開始より後に始まる
   最初のセクションを探す。見つかれば `clip_range(timeline, その番号)[0]`、
   見つからなければ `timeline.duration_sec()`（末尾 / G3-b）。
   **`timeline_start` は使わない**（§2.10）。

2. **場所の確保**（挿入点が末尾でないとき / G3-c）
   base 以外も含めた全映像トラック・全字幕トラックに対して
   `commands.make_room_for_range(timeline, track, at, at + 尺, min_clip_sec, policy)` を呼ぶ。
   音声トラックとロック中トラックは同関数が自動で除外する。
   `source.archive.clips[].timeline_start/end` は §2.10 のとおり既に信用していないため更新しない
   （ここで中途半端に直すと「たまに合っている」状態になり、かえって危険）。

3. **メディアの登録**
   `media_probe.probe(entry["normalized_path"], timeline.next_id("m"), clip_settings, cfg["media"])`。
   `normalized_duration` があれば `media.duration_sec` を上書きする（構築時と同じ扱い）。

4. **V1 / A1 クリップの生成**
   `_segments_of(entry, media)` で残す区間を取り、`build_archive_timeline` と同じ
   `origin={"type": ORIGIN_SILENCE_CUT, ORIGIN_ARCHIVE_INDEX: 番号, "segment_index": i}` で
   base 映像トラックへ積む。`media.has_audio` なら `AudioClip` も作る。

5. **字幕の生成**
   公開した `append_subtitles(timeline, base_subtitle_track, entry["items"], offset=挿入点,
   clip_duration=セクション尺, segments, media.id, 番号)` を呼ぶ。
   構築時と同じ関数のため、クリップ終端での打ち切りも同じ規約で効く。

6. **`source.archive.clips` の更新**
   単独追加なら 1 件 append。マージなら §3-4 ④⑤のとおり代表番号へ畳み、
   `vod_start/vod_end` を和集合へ更新する。

7. 変化があれば `True` を返す（`CommandStack.push` が `timeline.normalize()` を呼ぶ）。

**マージ時の番号統一（§3-4 ④）** も同じコマンド内で行う。
V1 クリップと字幕クリップの `origin[ORIGIN_ARCHIVE_INDEX]` を代表番号へ書き換えるだけで、
`_snapshot` が全クリップを `copy()` しているため Undo で完全に戻る。

### 5.6 経路の配線（§2.6 の穴を塞ぐ）

新引数はすべて **キーワード引数＋既定値** とし、既存の呼び出し・テストを壊さない。

```python
# clip_writer.write_clips（②の直後）
review = result_callback(prepared, curve or [], timeline,
                         workdir=workdir, clip_settings=clip_settings)
...
if isinstance(review, dict):
    timeline = review.get("timeline") or timeline
    edited   = review.get("clips") or []
    # 画面でセクションが追加されていれば prepared も差し替わる (resolve13 §3-6)
    prepared = review.get("prepared") or prepared
```

`project_resume.run_from_archive_project` も同様に渡し、同様に受け取る。

`ArchiveResultBridge.__call__` は `workdir` / `clip_settings` を payload へ載せ、
`_open_timeline_window` が `ArchiveTimelineDialog` へ渡す。
`_open_legacy_window`（従来画面）は無視する（従来画面に追加機能は載せない）。

`ArchiveTimelineDialog.__init__` へ `clip_workdir=None, clip_settings=None` を足す。
**どちらかが `None` なら「セクション追加…」ボタンを出さない**。
CLI/テスト経路や従来画面からの流用で落ちないための保険であり、
機能が使えないことは UI に現れるだけで、既存動作は一切変わらない。

### 5.7 画面側の実装（`archive_timeline_dialog.py`）

**a. クリップバーへボタンを 1 つ足す**（`_build_clip_bar`）

```python
self.add_section_button = QPushButton("セクション追加…")
self.add_section_button.setToolTip(
    "元動画の区間を指定してセクションを追加します。\n"
    "元動画の時系列に合わせた位置へ挿入され、既存セクションと重なる場合は統合されます。")
self.add_section_button.clicked.connect(self._on_add_section)
```

`section_add_config(settings)["enabled"]` が false、または §5.6 の前提が欠けるときは生成しない。

**b. 一覧の導出元を変える（§3-6 案A）**

```python
# 画面が見せるセクション一覧。source.archive.clips から作るため Undo に追随する。
# Timeline 上の位置順に並べる (番号は振り直さないため昇順にならない / §3-3)
def _sections(self):
    timeline = self.controller.timeline
    rows = []
    for entry in (archive_timeline.archive_section(timeline).get("clips") or []):
        span = archive_timeline.clip_range(timeline, entry.get("index"))
        if span is None:
            continue                      # Timeline 上に実体が無い = 表示しない
        rows.append({"index": entry.get("index"),
                     "start": float(entry.get("vod_start", 0.0)),
                     "end": float(entry.get("vod_end", 0.0)),
                     "score": float(entry.get("score", 0.0)),
                     "timeline_start": span[0]})
    rows.sort(key=lambda r: r["timeline_start"])
    return rows
```

`_build_clip_bar` のコンボ・`_clips_meta`・`_entry`・`result_data`・`_on_export_resolve` を
これに載せ替える。`_prepared` は `self._prepared_by_index`（番号 → 準備済みデータ）へ降格し、
`normalized_path` などが要るときだけ引く。

コンボは `timeline_changed` で作り直す（既に `_sync_clip_bar` が接続済み / `archive_timeline_dialog.py:70`）。

**c. 追加の流れ**

```
_on_add_section()
  ├─ SectionAddDialog で [ns, ne] を得る（キャンセルなら終わり）
  ├─ 既存セクションとの重なりを判定（scoring と同じ規約 / §3-4）
  │    完全に含まれる → 案内して終わり
  │    重なる         → 代表番号と差分区間の一覧を作る
  │    重ならない     → 差分は [ns, ne] の 1 件
  ├─ _SectionPrepareWorker を起動（差分ごとに prepare_one_clip）
  │    モーダル進捗ダイアログ（切り出し / 正規化 / 音量解析 / 編集点検出 / 文字起こし）
  ├─ finished_ok → controller.execute(AddArchiveSection(...))  ← ここで初めて Timeline が変わる
  ├─ _prepared_by_index を更新し、クリップバー・採点グラフを作り直す
  └─ 追加したセクションの先頭へ再生ヘッドを移動（jump_to_clip）
```

**準備が終わるまで Timeline へは一切触れない**のが重要である。
途中で失敗・キャンセルしても Timeline は無傷で、Undo 履歴も汚れない。

**d. 採点グラフ**

`self.graph.set_data(self._curve, self._clips_meta())` を追加後に呼び直す。
`curve` 自体は再採点しないため変わらない（G2）。追加セクションもマーカとして出る。

### 5.8 変わらないもの（意図的に触らない）

| 対象 | 理由 |
|---|---|
| `archive/scoring.py`・`archive/pipeline.py` | 採点方式は変更しない（G2） |
| `archive/features.py`・`comment_source.py` | 採点の入力。同上 |
| `split_by_clip` / `_render_clips` / `_build_combine_parts` | §3-4 ④により、マージ後も「連続する同一番号」の形に収まるため無改造で通る |
| `to_export_entry` / Resolve 出力 | `prepared` の形が変わらないため無改造 |
| 従来の一括結果画面（`ArchiveResultWindow`） | Timeline 経路専用の機能。従来画面には載せない |
| `project_io` の保存形式 | `source.archive.clips` の要素が増えるだけでスキーマは同じ（§8） |

---

## 6. 実装手順

1. `config.section_add_config()` と `DEFAULT_SETTINGS["archive"]["section_add"]` を足す（§5.2 / §7）。
2. `clip_writer._prepare_clips` のループ本体を `prepare_one_clip()` へ切り出す（§5.4）。
   **この時点で既存テストが全て通ることを確認する**（処理内容を変えていないことの担保）。
3. `timeline_builder._append_subtitles` を `append_subtitles` として公開する（別名を残さず呼び元も直す）。
4. `timeline_builder.AddArchiveSection` を実装する（§5.5）。
5. `write_clips` / `run_from_archive_project` / `ArchiveResultBridge` の配線を足す（§5.6）。
6. `section_add_dialog.py` を作る（§5.3）。
7. `ArchiveTimelineDialog` に `_sections()` 導出・ボタン・ワーカーを実装する（§5.7）。
8. `tests/test_archive_section_add.py` を書く（§10-2）。
9. 実機で §10-1 を確認する。

**2 と 3 は「切り出しただけ」の状態で一度止め、既存テストで担保してから 4 へ進む。**
新機能の不具合と、切り出しの事故を混ぜないため。

---

## 7. setting.json 定義（追加分）

```jsonc
"archive": {
  ...
  // セクション追加 (ver3 resolve13)
  "section_add": {
    "enabled": true,             // false で「セクション追加…」ボタンを出さない
    "default_length_sec": 180,   // 追加ダイアログの既定尺 (未指定は scoring.window_sec)
    "min_length_sec": 1.0,       // これより短い区間は追加させない
    "merge_on_overlap": true,    // 既存セクションと重なったら統合する (G5)
    "merge_mode": "delta"        // "delta"=差分だけ用意 (既定) / "rebuild"=和集合を作り直す
  }
}
```

`merge_mode: "rebuild"` は §3-4 案 A への切り戻し口である。
**本書では既定の `"delta"` のみ実装し、`"rebuild"` は §11 の将来拡張とする**
（設定キーだけ先に用意すると、動かない値が設定画面に並ぶため §9 Q3 で確認する）。

---

## 8. 互換性・非破壊の担保

| 観点 | 影響 |
|---|---|
| 採点結果・採点方式 | 変更なし（G2） |
| 保存形式（`*.archive.timeline.json`） | `source.archive.clips` の**要素が増えるだけ**。キー構成は同じ。旧バージョンで開いても未知のセクションが 1 件多いだけで読める |
| 既存プロジェクトを開く | 影響なし。追加操作をしなければ現状と完全に同一 |
| 書き出し（`split_by_clip` → `_render_clips` → 結合） | 無改造。§3-4 ④により追加・マージ後も既存の分割規則に収まる |
| Resolve 出力 | 無改造（`prepared` の形が同じ） |
| 従来の一括結果画面 | 影響なし（機能を載せない） |
| CLI / テスト経路（`result_callback=None`） | 影響なし。新引数はすべて既定値付き |
| `ArchiveTimelineDialog` の既存呼び出し | `clip_workdir`/`clip_settings` 未指定ならボタンが出ないだけ（§5.6） |
| Undo/Redo | 追加・マージとも 1 コマンドで完全に戻る（§2.7 / §5.5） |

---

## 9. 確認事項

要望文から一意に決められず、実装前に判断が要る点。

| # | 内容 | 本書の暫定案 |
|---|---|---|
| **Q1** | 区間の指定は**時刻入力**でよいか。採点グラフ上のドラッグ選択が要るか | 時刻入力（§3-1 案 A）を採用。グラフのドラッグは §11 F1 |
| **Q2** | セクション番号を振り直さないため、**個別出力のファイル名 `clip{N}` が時系列順にならない**（例: clip1, clip6, clip2…）。許容してよいか | 許容する前提で設計。気になる場合は出力時にのみ連番を振り直す案がある（§11 F2） |
| **Q3** | マージは**差分だけ用意**（既存セクションの編集を残す）で合っているか。それとも**和集合を作り直す**（編集は消えるが素材が 1 本にまとまる）か | 差分方式（§3-4 案 B）を既定とする。`merge_mode` の設定キーだけ用意し、`"rebuild"` は未実装 |
| **Q4** | 追加セクションの**スコア**は「区間に重なる窓スコアの最大値」でよいか | それで設計（§5.4）。採点はやり直さない（G2） |
| **Q5** | 追加処理のキャンセルは**工程の切れ目のみ**（音声認識の途中では止まらない）で許容できるか | 許容する前提。UI に明記する |
| **Q6** | **保存済みプロジェクトを開き直した再編集**でもセクション追加を使えるようにするか | 使えるようにする。VOD さえあれば同じ処理が通る（§5.6 で `run_from_archive_project` も配線） |
| **Q7** | 追加セクションの**テーマ**は空で始めてよいか。マージ時は既存セクションのテーマを引き継ぐか | 追加は空。マージ時は代表セクションのテーマを維持する |
| **Q8** | 追加した区間が **VOD の範囲外**（全長超過）を指した場合 | ダイアログで弾く。VOD 全長が取得できない場合のみ上限チェックを外す（§5.3） |

---

## 10. テスト計画

### 10-1. 実機確認

| # | 手順 | 期待 |
|---|---|---|
| 1 | 採点 → Timeline 編集画面 →「セクション追加…」で**先頭より前**の区間を追加 | Timeline の先頭へ入る（G3-a）。字幕も付く |
| 2 | **最後より後ろ**の区間を追加 | Timeline の末尾へ入る（G3-b） |
| 3 | **セクション 1 と 2 の間**の区間を追加 | 1 と 2 の間へ入り、以降が右へずれる（G3-c）。既存セクションの中身は変わらない |
| 4 | 既存セクションと**一部重なる**区間を追加 | 1 つのセクションへ統合され、クリップバーの区間表示が和集合になる（G5）。**既存側の字幕修正・トリムが残っている**（G10） |
| 5 | 既存セクションに**完全に含まれる**区間を追加 | 「既にセクションに含まれています」と案内され、Timeline は変わらない |
| 6 | 追加直後に **Ctrl+Z** | セクションが消え、クリップバー・採点グラフからも消える（G9 / §3-6） |
| 7 | Ctrl+Y で **Redo** | 元どおり戻る |
| 8 | 追加後に「完了」→ 書き出し | 追加セクションが Timeline の並び順どおりに結合される。字幕が焼かれている |
| 9 | 追加後に**保存** → 「編集の続き」から開き直す | 追加セクションが復元される（VOD から切り直される） |
| 10 | 追加処理中に**キャンセル** | Timeline は無傷。Undo 履歴も増えていない |
| 11 | 追加せずに従来どおり操作 | 現状と完全に同じ（退行が無いこと） |

### 10-2. 単体テスト（新規 `tests/test_archive_section_add.py`）

ffmpeg・音声認識は呼ばない。`prepare_one_clip` の戻り値を模したデータを直接与え、
**挿入位置・マージ・Undo の判定ロジックだけ**を検証する。

| # | テスト | 検証内容 |
|---|---|---|
| 1 | `test_insert_before_all` | 全セクションより前の区間 → Timeline 位置 0.0 へ入る（G3-a） |
| 2 | `test_insert_after_all` | 全セクションより後 → 末尾へ入る（G3-b） |
| 3 | `test_insert_between` | 間へ入り、後続の V1・S1 が尺ぶん右へずれる（G3-c） |
| 4 | `test_insert_keeps_vod_order` | 挿入後、Timeline 位置順に並べた `vod_start` が昇順のまま |
| 5 | `test_overlap_is_merged` | 重なる区間 → 代表番号へ統合され、`vod_start/vod_end` が和集合になる（G5） |
| 6 | `test_merge_preserves_existing_clips` | マージしても既存セクションの V1・字幕が置き換わらない（G10 / §3-4 案 B） |
| 7 | `test_merge_updates_prepared_span` | `prepared[代表].start/end` が和集合へ更新される（イントロカード対策 / §3-4 ⑥） |
| 8 | `test_contained_range_is_noop` | 完全に含まれる区間 → `apply` が `False`（履歴を汚さない） |
| 9 | `test_touching_range_is_merged` | 接触（`ne == es`）も統合される（`scoring._merge_time_sections` と同規約） |
| 10 | `test_split_by_clip_groups_merged_section` | マージ後に `split_by_clip` が 1 グループを返す（§3-4 ④の担保） |
| 11 | `test_undo_restores_timeline` | `CommandStack` で Undo すると V1・S1・`source.archive.clips` が完全に元へ戻る（G9） |
| 12 | `test_index_is_not_renumbered` | 既存セクションの番号が変わらない（G7 / §2.3 の参照破壊防止） |
| 13 | `test_estimated_score_from_curve` | 区間に重なる窓の最大 `total` が `score` になる（G11） |

### 10-3. 既存テストの回帰

```
python -m unittest discover -s tests
```

特に以下が通ること（§6 手順 2・3 の切り出しが無害であることの担保）:

* `tests/test_archive_timeline.py` — `build_archive_timeline` / `split_by_clip` の規約
* `tests/test_archive_prepare.py` — prepare フェーズ
* `tests/test_archive_project_resume.py` — 再編集経路
* `tests/test_resolve_export.py` — Resolve 出力

---

## 11. 将来拡張（本書では実装しない）

| # | 内容 | 備考 |
|---|---|---|
| **F1** | 採点グラフ上をドラッグして区間指定（§3-1 案 B） | QtCharts に範囲選択が無く、自前のラバーバンドが要る。既存の `clicked` ハンドラとの共存設計も別途必要 |
| **F2** | 出力時にのみセクション番号を時系列で振り直す（§9 Q2） | ファイル名だけの話であり、内部キーは触らずに済む |
| **F3** | `merge_mode: "rebuild"`（和集合を作り直すマージ / §3-4 案 A） | 既存編集を捨てる旨の確認ダイアログとセットで実装する |
| **F4** | 追加区間だけを再採点してスコアを求める | G2（採点方式を変更しない）との線引きを決めてから |
| **F5** | セクションの削除・分割 | 追加の対になる操作。要望に含まれないため本書では扱わない |
