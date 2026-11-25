# AI 編集で保存したプロジェクトが一覧に出ない 原因と修正設計書

現象の報告: `docs/error/20261125/error.md`

本書は `docs/CLAUDE.md`「実装前に設計を行うこと」に従う **設計書** である。

---

## 1. 結論（先に要点）

原因は 2 つあり、どちらも「AI 編集の経路 D が、既存の『続きから』の約束を
1 つ守っていない」ことに帰着する。

| # | 原因 | 症状 |
|---|---|---|
| C1 | プロジェクトの種別をファイル名だけで決めており、AI 編集の名前 (`.ai_<出力先>`) が
`archive_suffix` (".archive") で終わらないため **クリップ用と判定される** | 一覧に出ない |
| C2 | AI 編集の完了処理が作ったプロジェクトを **履歴へ積まない** | 「続きから」に出ない |

## 2. 証拠

### 2.1 C1 — 名前による種別判定

```python
# src/timeline/project_io.py:641
def _name_kind(path, suffix, archive_suffix):
    ...
    return KIND_ARCHIVE if stem.lower().endswith(archive_suffix.lower()) else KIND_CLIP
```

AI 編集のプロジェクト名は `ai_runner._default_project_path()` (`src/pipeline/ai_runner.py:305`)
が `name_suffix=".ai_" + target` で作るため、語幹は `vod_2938816056.ai_youtube_long` である。
`.archive` で終わらないので `KIND_CLIP` になる。

この判定は一覧の候補集めで絞り込みに使われる。

```python
# src/timeline/project_io.py:607
if kind:
    paths = [p for p in paths if _name_kind(p, suffix, archive_suffix) == kind]
```

* アーカイブタブ (`archive_tab.py:1213` = `KIND_ARCHIVE`) → ここで落ちる。
* クリップ用タブ (`main_window.py:884` = `KIND_CLIP`) → 名前判定では通るが、
  概要読み込み後に中身の種別と食い違うとして行が取り除かれる
  (`project_library_dialog.py:282`。中身は `source.archive` を持つため `KIND_ARCHIVE`)。

結果として**どちらの一覧にも出ない**。

`project_io.project_kind()` (:77) は中身 (`source.archive` の有無) で正しく判定できており、
保存された JSON も `source.archive` を持っている。**判定の材料はあるのに使われていない**のが
C1 の正体である。

### 2.2 C2 — 履歴へ積んでいない

```python
# src/gui/archive_tab.py:1125
def _on_ai_done(self, outcome):
    ...
    self._start_resume(paths[0])        # 1 本目を開くだけ
```

`project_io.remember_recent()` を呼んでいない。クリップ用・アーカイブ用の手編集は
いずれも保存時に `timeline_editor_dialog._remember_recent()` (:1246) を通しており、
**作った側が履歴へ積む**のが既存の約束である (ver9 resolve14 追補)。
経路 D だけがこれを守っていない。

走査フォルダが空の設定 (本件の環境) では履歴が一覧の唯一の元であるため、
積まないことがそのまま「どこからも開けない」になる。

## 3. 影響範囲

| 経路 | 影響 |
|---|---|
| AI 編集 (経路 D) | **本件**。3 本すべてが一覧に出ない。2 本目以降は履歴にも無い |
| アーカイブ手動編集 | 影響なし (`.archive` 名で保存され、保存時に履歴へ積む) |
| 1 本編集 (クリップ用) | 影響なし |
| 規約外の名前で手動保存したアーカイブプロジェクト | C1 と同じ理由で一覧に出ない (本修正で救われる) |

## 4. 修正方針

### R1. 履歴の種別は「どの履歴に入っていたか」で決める（本丸）

履歴は `recent` (クリップ用) と `recent_archive` (アーカイブ用) に分かれており、
**積んだ側が種別を決めて入れている**。つまり履歴に入っているパスの種別は
ファイル名を見るより先に分かっている。これを使う。

| パスの出どころ | 種別の決め方 |
|---|---|
| 履歴 `recent` | `KIND_CLIP` |
| 履歴 `recent_archive` | `KIND_ARCHIVE` |
| 走査フォルダ | 従来どおり `_name_kind` (ファイル名) |

`scan_projects` は候補を集めるときに出どころを覚え、`kind` の絞り込みでこれを使う。
**ファイルを 1 つも読まない**ため一覧を開く速さは変わらない。
最終確認は従来どおり `read_summary` → `project_library_dialog._on_summary` の
中身による照合が行う (名前も履歴も間違っていれば、そこで取り除かれる)。

変更は `src/timeline/project_io.py` の `scan_projects` に限る。
`_name_kind` は走査フォルダ用として**そのまま残す** (既存テストの前提を壊さない)。

### R2. AI 編集が作ったプロジェクトを履歴へ積む（安全網）

`archive_tab._on_ai_done()` が、開く 1 本だけでなく**作られた全部**を積み、
`save_settings` で書き戻す。経路 D は 1 回の実行で 2 種類のプロジェクトを作るため、
積み先を種別で分ける。

| 出力先 | 仕上がり | 中身 | 積み先 | 開く画面 |
|---|---|---|---|---|
| `youtube_long` | 横 `<VOD>.ai_youtube_long.timeline.json` | `source.archive` を持つ = アーカイブ用 | `recent_archive` | アーカイブタブ |
| `youtube_shorts` / `tiktok` | 縦 `<VOD>.ai_<出力先>.timeline_vertical.timeline.json` | `source.archive` を持たない = **クリップ用** | `recent` | **クリップ用タブ** |

**縦を作った出力先は縦だけを積む。** 横は縦が参照する素材 (`<横>.media`) の
持ち主として残るが、仕上がりではないため一覧へは出さない。縦を作れなかったとき
(`_make_vertical` が "" を返したとき) だけ横を積み、「どこからも開けない」結果に
ならないようにする。選別は `_ai_history_targets()` が行う。

縦 (`shorts` / `tiktok` の仕上がり) を `recent` へ積むのは、縦動画プロジェクト
作成画面と同じ判断である。`timeline_editor_dialog` は「アーカイブ用の画面から
作っても、開くのはクリップ用タブの『続きから』だから」として常に `KIND_CLIP` で
積んでいる (ver9 resolve14 追補)。縦は素材を複製して自己完結しているため
(`crop.MEDIA_COPY`)、クリップ用タブからそのまま開ける。

* 並びは「1 本目が先頭」になるよう後ろの出力先から積む。
* 積んだあとアーカイブタブの「続きから」コンボを作り直す
  (`resume_row.set_settings`)。クリップ用タブは一覧を開くたびに
  `main_window._refresh_recent_projects()` が設定を読み直すため、
  書き戻しだけで足りる。
* 履歴の保存に失敗しても編集結果は残っているため、警告ログだけで続行する
  (`timeline_editor_dialog._remember_recent` と同じ扱い)。

R1 だけでも既に履歴にあるものは出るようになるが、**AI 編集した直後に出る**ようにし、
`shorts` / `tiktok` を辿れるようにするには R2 が要る。

### R3. 採点グラフの線（別件・同時に直す）

AI 編集で開いたプロジェクトでは採点グラフの折れ線が出ない。
`ai_runner.run_plan` が `build_archive_timeline` へ `curve` を渡しておらず、
`source.archive.curve` が空で保存されるためである (既存経路 `clip_writer.py:1177` は渡している)。
`run_ai` は `analysis["curve"]` を持っているので、`run_plan` へ引数を 1 本通す。

| ファイル | 変更 |
|---|---|
| `src/pipeline/ai_runner.py` | `run_plan(..., curve=None)` を追加し `build_archive_timeline(..., curve=curve)` へ渡す / `run_ai` が `analysis.get("curve")` を渡す |

### R4. 採らなかった案

| 案 | 却下理由 |
|---|---|
| AI 編集のプロジェクト名を `.ai_<出力先>.archive.timeline.json` にする | 名前判定は正しくなるが、**既に保存済みのものは救えない** (本件の 3 本が開けないまま)。素材フォルダ (`.media`)・`plan.json`・`ai-base.json`・レビュー PDF の名前まで連鎖して変わり、`review_report.py` の名前の解釈 (2 段の拡張子を落とす) も直す必要がある。得るものに対して波及が大きい |
| `_name_kind` に `.ai_` を直接書く | 出力先 (`youtube_long` / `shorts` / `tiktok` …) が増えるたびに書き換えが要る。種別はファイル名の規約 (`archive_suffix`) で表すという既存の約束からも外れる。ハードコード禁止 (`docs/CLAUDE.md`) にも反する |
| 一覧の候補集めで全件の中身を読んで種別を決める | `library.max_items` は 100 で、1 件 200〜400 KB の JSON を同期で読むことになる。一覧を開くたびに数十 MB の読み取りが走る。中身による確認は既に `_on_summary` が背景スレッドで行っている |
| AI 編集の完了時に 3 本すべてを開く | 画面が 3 枚開く。開くのは 1 本目のままにし、残りは一覧・コンボから選ばせる |
| 走査フォルダに `archive_work` を既定で足す | 作業フォルダは消す対象であり (`vod_retention`)、一覧に出し続ける場所ではない。設定で足せる口 (`library.extra_dirs`) は既にある |

## 5. 変更するファイル

| ファイル | 変更 |
|---|---|
| `src/timeline/project_io.py` | `scan_projects` が候補の出どころ (どの履歴か) を覚え、`kind` の絞り込みに使う |
| `src/gui/archive_tab.py` | `_on_ai_done` が作られた全プロジェクトを履歴へ積む (`_remember_ai_projects` を新設) |
| `src/pipeline/ai_runner.py` | `run_plan` に `curve` を通す (R3) |
| `tests/test_error_20261125_project_list.py` | **新規**。下記 |

設定の追加は無い。

## 6. テスト

| # | 確認 |
|---|---|
| T1 | `recent_archive` に積んだ `.ai_youtube_long.timeline.json` が `scan_projects(kind=archive)` に出る |
| T2 | 同じパスは `scan_projects(kind=clip)` には出ない |
| T3 | `recent` に積んだクリップ用プロジェクトは従来どおり `kind=clip` にだけ出る |
| T4 | 走査フォルダから拾ったものは従来どおりファイル名で判定される (`.archive` → archive / それ以外 → clip) |
| T5 | `kind` を渡さなければ履歴・走査フォルダのすべてが返る (従来どおり) |
| T6 | 横のプロジェクトを `recent_archive` へ積み、1 本目が先頭に来る (縦が無ければ `recent` は触らない) |
| T8 | 縦のプロジェクトは `recent` へ積む (横と混ざらない) |
| T8-2 | `recent` へ積んだ縦のプロジェクトがクリップ用の一覧に出る |
| T9 | 縦がある出力先は縦だけ、縦が無い出力先 (`youtube_long`) は横を積む |
| T9-2 | 縦を作れなかった出力先は横を積む (開けない結果にしない) |
| T7 | `run_plan` が `build_archive_timeline` へ `curve` を渡し、保存された JSON の `source.archive.curve` が空でない |
