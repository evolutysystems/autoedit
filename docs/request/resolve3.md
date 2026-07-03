# 設計書: 字幕編集画面の追加（フルテロップ後の確認・編集ステップ）

`docs/request/request3.md` の要望に対する設計書。CLAUDE.md の方針に従い、本書のレビュー後に実装する。

---

## 1. 概要

現状の問題は「誤訳が多い」こと。音声認識（faster-whisper）の結果をそのまま焼き込むため、
誤認識・誤変換がそのまま完成動画に残る。

これを解決するため、**パイプラインのフルテロップ工程の途中に「字幕編集画面」を挿入**し、
焼き込み前に人手で字幕テキストを修正・取捨選択できるようにする。

| 項目 | 内容 |
|---|---|
| 画面を開くタイミング | フルテロップ工程で**テキスト生成（Whisper＋発話ゲート）が完了した直後／ASS 焼き込みの前** |
| 画面の内容 | 生成したテロップを一覧表示（時間 / 字幕 / 使用） |
| 「時間」 | そのテロップが動画のどの区間に表示されるか（開始〜終了）を表示。編集不可 |
| 「字幕」 | その区間に表示する字幕テキスト。**編集可能** |
| 「使用」 | チェックボックス。チェックされた字幕のみ焼き込む。**初期値は全件チェック** |
| 「字幕決定」ボタン | 押下で編集結果を確定し、続きのパイプライン（焼き込み→OP/ED 結合→出力）を続行 |

### 1.1 設計上の最重要論点（headless との両立）

現状のパイプラインは `src/main.py`（CLI）からヘッドレス実行され、進捗はコンソールへ出力される
（`src/main.py:81`）。`run_pipeline` も GUI を一切前提にしていない（`src/pipeline/pipeline_runner.py:24`）。
**一方、字幕編集画面はユーザー操作待ち（ブロッキング）を伴う GUI** であり、CLI 一括処理では開けない。

そこで本設計では、編集画面を**直接呼び出すのではなく「レビューコールバック（フック）」として注入**する。

- **GUI 実行経路**: 専用ランチャ（`src/gui/main_window.py`・新設）からパイプラインをワーカースレッドで動かし、
  フック発火時に編集画面（モーダル）を表示して待機する（§10-1 確定）。
- **CLI／ヘッドレス実行経路**: フックを注入しない（`None`）。この場合は**全テロップをそのまま使用**して従来どおり無人で完了する。

これにより「既存実装を破壊しない」（CLAUDE.md 制約）を満たしつつ、GUI 経路でのみ編集画面が開く。

設計方針（CLAUDE.md 準拠）:

- ハードコードを排し、画面の有効/無効や挙動は `setting.json` から取得する。
- 既存関数シグネチャ（`build_subtitle_file` / `burn_subtitle` / `run_pipeline`）の後方互換を保つ。
- 新規ライブラリは追加しない（GUI は既存採用の **PySide6** を流用：`src/settings/settings_window.py:8`）。
- 不足キーは `DEFAULT_SETTINGS` と `.get(key, 既定値)` で吸収する（旧 `setting.json` でも動作）。

---

## 2. 現状分析

### 2.1 フルテロップ工程の処理順（`src/modules/subtitle_generator.py:366` `run()`）

現状 `run()` は次の順で**一気通貫**に処理し、途中に介入点が無い。

```
extract()        音声認識でタイムライン生成 [{start,end,text}, ...]   (:409)
  ↓
_gate_with_settings()  発話区間でテロップを絞り込み（resolve2 で実装済）   (:420)
  ↓
build_subtitle_file()  ASS ファイル生成   (:425)
  ↓
burn_subtitle()        ASS を動画へ焼き込み   (:426)
  ↓
set_current_video_path()  次工程へ受け渡し   (:431)
```

→ **`_gate_with_settings()` と `build_subtitle_file()` の間**に編集ステップを挿入するのが最小改修点。
タイムライン構造 `{start, end, text}` は編集画面の入力にそのまま使える。

### 2.2 タイムラインのデータ構造

各エントリは `{"start": float, "end": float, "text": str}`（`text` 内の改行は `\N`）。
`build_subtitle_file`（`:216`）はこの配列を順に `Dialogue:` 行へ展開する。
**「使用」フラグは現状存在しない**ため、本設計で追加する（§3）。

### 2.3 実行経路の現状

- CLI のみ（`main.py`）。GUI からパイプラインを起動する経路は未実装。
- `settings_window.py` は設定専用ウィンドウで、パイプライン起動ボタンは持たない。
- したがって「字幕編集画面を出すには GUI 実行経路が要る」。本設計では編集画面と
  最小限の GUI 実行経路（フック注入＋ワーカースレッド）を併せて設計する（§7）。

---

## 3. データモデル拡張（タイムラインへ「使用」を付与）

編集画面の入出力で用いる拡張エントリを定義する。既存の `{start,end,text}` に `use` を加えるだけで、
`use` が無い旧データは「使用扱い（True）」として後方互換に解釈する。

```python
# 編集画面で扱うテロップ項目
{
    "start": float,   # 開始秒（編集不可・表示のみ）
    "end":   float,   # 終了秒（編集不可・表示のみ）
    "text":  str,     # 字幕テキスト（編集可能）
    "use":   bool,    # 焼き込みに使用するか（初期値 True）
}
```

- `build_subtitle_file` へ渡す直前に `use == True` のものだけへ絞り込む（`use` キー自体は無視されるので
  `build_subtitle_file` のシグネチャ変更は不要）。
- 「時間」列の表示は `H:MM:SS.cc`（既存 `_format_ass_time` `:228` と同形式）を流用して整合させる。

---

## 4. setting.json 定義（subtitle セクション追加項目）

既存キーは維持し、以下を追加する。

| キー（新規） | 型 | 既定値 | 役割 |
|---|---|---|---|
| `review_enabled` | bool | `true` | 字幕編集画面の表示 ON/OFF（GUI 経路でのみ有効。CLI ではフック未注入のため無効） |
| `review_on_empty_skip` | bool | `true` | 「使用」が 0 件のまま決定された場合にテロップ焼き込みをスキップするか |

> **設計判断（既定値）**
> `review_enabled=true` としても、CLI 実行ではフックを注入しないため画面は開かない（§1.1）。
> GUI 実行経路でのみ画面が出る。明示的に GUI でも常時スキップしたい場合に `false` にできるよう設定化する。

### setting.json 追記イメージ（subtitle セクション・抜粋）

```json
"subtitle": {
    "enabled": false,
    "engine": "whisper",
    "speech_gate_enabled": true,
    "review_enabled": true,
    "review_on_empty_skip": true
}
```

---

## 5. 処理フロー（編集ステップ挿入後）

```
[フルテロップ工程 / subtitle_generator.run()]

  extract()  ──────────────►  タイムライン [{start,end,text}, ...]
        │
  _gate_with_settings() ───►  発話ゲート後タイムライン（resolve2）
        │
        ├─ レビューフック有り？(context.subtitle_review_callback)
        │     ├─ 無し（CLIヘッドレス）→ 全件 use=True とみなしそのまま続行
        │     └─ 有り（GUI）→ ★字幕編集画面を開く★
        │                       ・一覧表示（時間/字幕/使用）
        │                       ・テキスト編集 / 使用チェック操作
        │                       ・「字幕決定」押下まで待機
        │                       → 編集済みタイムライン [{start,end,text,use}] を返す
        │
        ├─ use==True のみへ絞り込み
        │     └─ 0件 かつ review_on_empty_skip → テロップ焼き込みをスキップ（§8）
        │
  build_subtitle_file() ──►  ASS 生成
        │
  burn_subtitle() ────────►  焼き込み
        │
  set_current_video_path() ► 次工程（OP/ED 結合）へ
```

要望シナリオ（誤訳の修正）:

```
編集前:  [0:00:04.00-0:00:06.00] "こんにちわ世解"     ← 誤訳
         [0:00:08.00-0:00:09.00] "えーっと"          ← 不要

編集後:  [0:00:04.00-0:00:06.00] "こんにちは世界"  使用[✓]   ← テキスト修正
         [0:00:08.00-0:00:09.00] "えーっと"        使用[ ]   ← 使用を外す
  → 焼き込み対象は1件目のみ
```

---

## 6. subtitle_generator.py 修正設計

### 6.1 レビューフックの呼び出し（`run()` `:420` 付近）

`_gate_with_settings()` の直後、`build_subtitle_file()` の前にフック呼び出しを挿入する。

```python
# 発話区間でテロップを絞り込み（resolve2: 既存）
if subtitle_cfg.get("speech_gate_enabled", True):
    timeline = _gate_with_settings(...)

# 字幕編集画面（レビュー）フック ── GUI 経路でのみ注入される
timeline = _review_timeline(context, timeline, subtitle_cfg)
if not timeline:
    # 使用 0 件 → テロップをスキップして元動画を次工程へ（§8）
    _logger.info("使用するテロップが無いため焼き込みをスキップ")
    return context.current_video_path()

build_subtitle_file(timeline, font_profile, subtitle_path)
```

### 6.2 レビュー適用ヘルパの追加

```python
# 字幕編集画面（レビュー）を適用する
# context にレビューコールバックが注入されていれば呼び出し、編集結果で置き換える。
# 注入が無い（CLI/ヘッドレス）場合は全件使用としてそのまま返す。
def _review_timeline(context, timeline, subtitle_cfg):
    # 設定で無効化されていればスキップ
    if not subtitle_cfg.get("review_enabled", True):
        return timeline
    review_cb = getattr(context, "subtitle_review_callback", None)
    if review_cb is None:
        return timeline  # CLI など：画面なし＝全件使用

    # 編集画面へ渡す入力を生成（初期値は全件チェック）
    items = [{"start": e["start"], "end": e["end"], "text": e["text"], "use": True}
             for e in timeline]
    edited = review_cb(items)  # 「字幕決定」まで待機し編集結果を返す

    # キャンセル（None）時はパイプライン全体を中断（§10-2 確定）
    if edited is None:
        raise PipelineCancelled("字幕編集がキャンセルされたためパイプラインを中断します")

    # 使用チェックされたエントリのみを残す
    used = [{"start": e["start"], "end": e["end"], "text": e["text"]}
            for e in edited if e.get("use", True)]

    # 使用 0 件 → テロップ無しで続行（§10-3 確定）。呼び出し側でスキップさせる
    if not used:
        return []
    return used
```

- 戻り値が空リスト `[]` のときは §6.1 でテロップ焼き込みをスキップする（テロップ無しで続行）。
- キャンセルは `PipelineCancelled`（新規例外）で表現し、`run_pipeline` が捕捉して中断する（§9）。
- `build_subtitle_file` / `burn_subtitle` のシグネチャは**変更しない**（`use` キーは渡す前に除去）。
- `review_on_empty_skip` は「使用 0 件時にスキップするか」のスイッチとして残すが、既定 `true`（テロップ無し続行）で運用する。

---

## 7. パイプライン／実行経路の修正設計

### 7.1 PipelineContext へのフック注入（`src/pipeline/pipeline_context.py`）

`__init__` に `subtitle_review_callback=None` を追加し、属性として保持する。

```python
def __init__(self, input_path, settings, progress_callback=None,
             working_dir=None, total_steps=4, subtitle_review_callback=None):
    ...
    # 字幕編集画面フック（GUI 実行時のみ注入。None ならレビュー無し）
    self.subtitle_review_callback = subtitle_review_callback
```

既定 `None` のため、既存の生成箇所はすべて従来どおり動作する（後方互換）。

### 7.2 run_pipeline の引数追加（`src/pipeline/pipeline_runner.py:24`）

`run_pipeline(input_path, settings, progress_cb=None)` に
`subtitle_review_callback=None` を**末尾追加**し、`_prepare_context` を経て Context へ渡す。
位置引数の並びは不変のため、既存呼び出し（`main.py:81`）はそのまま動く。

```python
def run_pipeline(input_path, settings, progress_cb=None, subtitle_review_callback=None):
    ...
    context = _prepare_context(input_path, settings, progress_cb, subtitle_review_callback)
```

### 7.3 GUI 実行経路（新規）

字幕編集画面は GUI 上でしか開けないため、**専用ランチャ `src/gui/main_window.py` を新設**する（§10-1 確定）。
ランチャは「入力動画の選択／実行ボタン／進捗表示／字幕編集画面の橋渡し」を担う。
パイプラインは**ワーカースレッド**で実行し、フック発火時に**メインスレッドへ橋渡し**してモーダル表示する。

```
[GUI メインスレッド: main_window.py]          [ワーカースレッド: run_pipeline]
   実行ボタン押下                                   subtitle_generator.run()
   PipelineWorker(QThread) 起動 ───────────────►   ...発話ゲート完了...
                                                    review_cb(items) を呼ぶ
   review_requested シグナル受信  ◄──────(emit)──   （ここで待機 = QWaitCondition / Event）
   SubtitleEditorDialog.exec()
   ユーザーが編集・字幕決定
   結果を共有領域へ格納し wake  ──────(wake)─────►   review_cb が結果を返す
                                                    build_subtitle_file()→burn_subtitle()
   progress シグナルで進捗バー更新 ◄───────────────  以降の工程…
   finished シグナルで完了通知    ◄───────────────  出力完了
```

橋渡しの実体（`SubtitleReviewBridge`）:

- ワーカースレッドから呼ばれる callable。`review_requested` シグナルを emit し、
  `QWaitCondition`（または `threading.Event`）で結果が来るまでブロックする。
- メインスレッドのスロットがダイアログを `exec()` し、確定結果を共有変数へ書いて wake する。
- この callable を `run_pipeline(..., subtitle_review_callback=bridge)` に渡す。

> **設計判断（スレッド構成）**
> Whisper／FFmpeg は長時間処理のためメインスレッドで回すと UI が固まる。
> よってパイプラインはワーカースレッド、編集画面はメインスレッドで `exec()` する標準構成とする。
> GUI 実行経路は専用ランチャ `src/gui/main_window.py`（新設）に置く（§10-1 確定）。

「字幕決定」と「キャンセル」の確定結果は次のとおり橋渡しする。

- **字幕決定**: ダイアログを `accept()` → 編集結果 `[{start,end,text,use}]` を共有領域へ格納して wake →
  `review_cb` がその結果を返す。
- **キャンセル**: ダイアログを `reject()` → `review_cb` は `None` を返す → `_review_timeline` が
  `PipelineCancelled` を送出 → ワーカースレッドの `run_pipeline` で捕捉され中断（§9）。

### 7.4 CLI 実行経路（`main.py`）

変更不要（フックを渡さない）。CLI 一括処理では編集画面は開かず、全テロップをそのまま使用して従来どおり完了する。

---

## 8. 字幕編集画面の設計（新規 GUI モジュール）

新規ファイル: `src/gui/subtitle_editor_dialog.py`（専用ランチャ `main_window.py` と同じ `src/gui/` に配置）。

### 8.1 ウィジェット構成

`QDialog` 上に以下を配置する（既存 `settings_window.py` と同じ PySide6・スタイル方針を踏襲）。

| 要素 | ウィジェット | 内容 |
|---|---|---|
| 一覧 | `QTableWidget`（3列・行数=テロップ数） | 時間 / 字幕 / 使用 |
| 時間 列 | `QTableWidgetItem`（編集不可フラグ） | `H:MM:SS.cc → H:MM:SS.cc` 表示 |
| 字幕 列 | 編集可能セル（複数行は `\N`↔改行を相互変換して表示） | テキスト編集 |
| 使用 列 | セルの `Qt.ItemIsUserCheckable`（または `QCheckBox` セルウィジェット） | 初期 `Checked` |
| 全選択/全解除 | `QCheckBox` または 2 ボタン（任意・利便用） | ヘッダ操作 |
| 字幕決定 | `QPushButton`（`accept()` に接続） | 編集確定＝パイプライン続行 |
| キャンセル | `QPushButton`（`reject()` に接続） | §10-2 確定：パイプライン全体を中断（`PipelineCancelled`） |

> ウィンドウ右上の「×」やウィンドウクローズも `reject()` 相当（中断）として扱う。

### 8.2 入出力 I/F

```python
class SubtitleEditorDialog(QDialog):
    # items: [{"start","end","text","use"}]  ← 初期値（use は全件 True）
    def __init__(self, items, parent=None): ...

    # 「字幕決定」確定後に編集結果を返す
    # 戻り値: [{"start","end","text","use"}]（start/end は不変、text/use は編集反映）
    def result_items(self) -> list: ...
```

- 「時間」列はセルを編集不可（`flags() & ~Qt.ItemIsEditable`）にして start/end の改変を防ぐ。
- 「字幕」列の改行は表示時に `\N → \n`、確定時に `\n → \N` へ戻す（ASS 整合）。
- 「使用」初期値は全行 `Checked`（要望：初期値は一覧上の全てをチェック）。

### 8.3 表示テキスト整形ヘルパ

時間表示は既存 `_format_ass_time`（`subtitle_generator.py:228`）を再利用し、画面・ASS で表記を揃える。

---

## 9. 後方互換・エラー処理

- **CLI／既存呼び出し**: フック未注入のため画面は開かず、全テロップをそのまま使用＝従来挙動を完全維持。
- 旧 `setting.json`（新規キー無し）でも `.get(key, 既定値)` で `review_enabled=true` 等に補完され動作する
  （ただし CLI ではフックが無いため画面は出ない）。
- `build_subtitle_file` / `burn_subtitle` / 既存 `run_pipeline` 位置引数の互換を保つ（引数は末尾追加のみ）。
- 編集画面で全テロップの「使用」を外して「字幕決定」した場合、テロップ焼き込みをスキップし、
  発話ゲート前の動画をそのまま次工程へ渡す（テロップ無し動画として完成。§10-3 確定）。
- **キャンセル時はパイプライン全体を中断する**（§10-2 確定）。新規例外 `PipelineCancelled(AutoEditError)` を
  `src/exceptions.py` に追加し、`_review_timeline` から送出する。`run_pipeline` の既存 `except AutoEditError`
  （`pipeline_runner.py:61`）が捕捉して中断ログ後に再送出し、ランチャ側で「中断」通知として表示する。
  → 中間ファイルは既存 `finally: _cleanup(context)`（`:67`）で従来どおり後処理される。
- `PipelineCancelled` は「ユーザー意図による中断」であり異常終了ではないため、ランチャ側はエラーダイアログでなく
  中断メッセージとして扱うことを推奨する（GUI 側の表示方針）。

---

## 10. 設計判断（要望者回答を反映・確定）

当初の確認事項に対し、以下のとおり方針が確定した。本設計は本節の確定内容に従う。

1. **GUI 実行経路の置き場所** → **専用ランチャを新設**する。`src/gui/main_window.py` を新規作成し、
   入力動画の選択・実行ボタン・進捗表示・字幕編集画面の橋渡しを担う（既存 `SettingsWindow` には実行機能を持たせない）。
2. **キャンセル時の挙動** → **(b) パイプライン全体を中断**する。「字幕決定」せず画面を閉じた場合は
   新規例外 `PipelineCancelled` を送出し、`run_pipeline` で捕捉して処理を中断する（§6.2・§9）。
3. **「使用」0 件の扱い** → **テロップ無しで続行**する。全件のチェックを外して「字幕決定」した場合は
   テロップ焼き込みをスキップし、発話ゲート前の動画をそのまま次工程へ渡す（`review_on_empty_skip`）。
4. **時間の編集可否** → **まずは編集不可**とする。「時間」列は表示専用、編集対象は「字幕」列のみ。
5. **行の追加・削除** → **今回は対象外**。既存テロップの修正と取捨選択のみを提供する。
6. **プレビュー** → **今回は対象外**。焼き込み前の見え方確認機能は含めない。

---

## 11. 変更対象ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/settings/setting.json` | `subtitle` に `review_enabled` / `review_on_empty_skip` 追加（§4） |
| `src/exceptions.py` | `PipelineCancelled(AutoEditError)` を新規追加（§9・キャンセル中断用） |
| `src/modules/subtitle_generator.py` | `_review_timeline` 追加、`run()` に編集ステップ挿入、`PipelineCancelled` を import（§6） |
| `src/pipeline/pipeline_context.py` | `PipelineContext.__init__` に `subtitle_review_callback` 追加（§7.1） |
| `src/pipeline/pipeline_runner.py` | `run_pipeline` / `_prepare_context` にフック引数を末尾追加し Context へ伝播（§7.2） |
| `src/gui/__init__.py`（新規） | `gui` パッケージ初期化 |
| `src/gui/subtitle_editor_dialog.py`（新規） | 字幕編集画面（`QTableWidget` ベース・時間/字幕/使用・字幕決定）（§8） |
| `src/gui/main_window.py`（新規・§10-1 確定） | GUI 専用ランチャ＋`SubtitleReviewBridge`（ワーカースレッド/橋渡し）（§7.3） |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["subtitle"]` に新規キー既定値を追加（必要なら字幕タブへ ON/OFF トグル） |

> 既存の `build_subtitle_file` / `burn_subtitle` / `_gate_with_settings` は**変更せず再利用**する（破壊しない）。

---

## 12. 実装ステップ（レビュー後）

1. `setting.json` / `DEFAULT_SETTINGS` に新規キー追加（§4）。
2. `exceptions.py` に `PipelineCancelled` を追加（§9）。
3. `pipeline_context.py` / `pipeline_runner.py` にフック引数を追加（§7.1・7.2）。
4. `subtitle_generator.py` に `_review_timeline` を追加し `run()` へ編集ステップを挿入（§6）。
5. `src/gui/`（`__init__.py` / `subtitle_editor_dialog.py`）に編集画面を新規実装（§8）。
6. `src/gui/main_window.py`（専用ランチャ）＋`SubtitleReviewBridge`（ワーカースレッド/橋渡し）を実装（§7.3）。
7. GUI 経路で「フルテロップ後に画面が開く→編集→字幕決定→続行」「キャンセル→中断」、
   CLI 経路で「画面が開かず従来完了」を確認。
8. 全件チェック解除→「テロップ無しで続行」を確認（§10-3）。
9. 旧 `setting.json`（新規キー無し）でも例外なく動作することを確認。
