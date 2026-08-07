# resolve9: 字幕手動改行・コンソール窓抑制・稼働スピナー表示

`docs/request/request9.md` への要望設計書。

> ## 要望（原文）
> 以下3つの要望がある。
> - 無音カット後の字幕一覧で、改行を行ったら字幕に反映されるようにしてほしい。
>   Alt + Enter もしくは Shift + Enter で改行できるようにしたい。既存の15文字改行は継続する。
> - 処理実行中にコマンドプロンプトがウィンドウで出てこないようにしてほしい。
> - 処理実行中にちゃんと動いていることを証明するため、main_window.py のステータスかウィンドウ上に
>   記号で証明したい。Claude Code を実行すると動いている記号のようなものが欲しい。

本書はレビュー用の設計書であり、**実装は本書の承認後に行う**（`docs/CLAUDE.md` の方針に準拠）。

> **レビュー回答反映済み（§F）**：1. 確定時に15文字再ラップする（案②）／2. settings_window の「開始」ボタンを削除する／3. コンソール抑止ヘルパは新規ファイルに切り出す／4. スピナーは `status_label` のみに表示する。

---

## 0. 要件サマリと影響範囲

| # | 要望 | 主対象ファイル | ロジック変更 |
|---|---|---|---|
| **R1** | 字幕一覧で Alt+Enter / Shift+Enter による**手動改行**を焼き込みへ反映。**確定時に15文字再ラップ**を併用 | `src/gui/subtitle_editor_dialog.py` / `src/modules/subtitle_generator.py` | 改行編集UI＋再ラップ |
| **R2** | 処理実行中に**コンソール窓（cmd）を表示させない**。あわせて **settings_window の「開始」ボタンを削除** | FFmpeg 起動4か所 ＋ 新規 `src/utils/proc.py` ＋ `src/settings/settings_window.py` | 起動フラグ付与・ボタン削除 |
| **R3** | 処理実行中の**稼働証明スピナー**（Claude Code 風の回転記号）を `status_label` に表示 | `src/gui/main_window.py` | スピナー追加 |

`setting.json` の推測変更は行わない。既存コードの削除は R2 の「開始」ボタン（明示要望）に限定する（`docs/CLAUDE.md`）。

---

# A. R1 — 字幕編集一覧での手動改行（Alt+Enter / Shift+Enter）＋確定時15文字再ラップ

## A-1. 現状調査

| 対象 | 内容 |
|---|---|
| `subtitle_editor_dialog.py` `_populate` (L100-123) | 字幕列を `item["text"].replace("\\N", "\n")` で **ASS改行 `\N` → 実改行**へ変換し表示（L109）。行高は `resizeRowsToContents()`。 |
| 同 `result_items` (L135-149) | 確定時 `text_item.text().replace("\n", "\\N")` で **実改行 → `\N`** へ復元（L141）。 |
| `subtitle_generator.py` `_split_lines` (L255-265) | **15文字ごとに `\N` 挿入**。**音声認識時**（`_recognize` L196）に適用済み。`max_line_length`（既定15）は `setting.json.subtitle.max_line_length`（L143）。 |
| 同 `_review_timeline` (L514-546) | ダイアログ結果を受け、使用チェック分のみ焼込タイムラインへ。ここで `subtitle_cfg` を保持（`max_line_length` 取得可能）。 |

### A-1.1 データの往復は成立、原因はエディタが「単一行」

`\N`⇔`\n` の相互変換は全経路実装済みだが、`QTableWidget` の「字幕」セルは既定 delegate により **単一行 `QLineEdit`** で編集され、**改行を入力する手段が物理的に無い**（Enter も Alt/Shift+Enter もすべて「確定」扱い）。これが R1 未達の根本原因。

## A-2. 設計（改行入力UI）

`subtitle_editor_dialog.py` 内に追加（新規ファイルは作らない）。

1. **複数行エディタ `_MultilineCellEdit(QPlainTextEdit)`**：`keyPressEvent` で Enter 系を振り分け。
   - **Alt+Enter / Shift+Enter** → 改行挿入（R1）
   - **修飾なし Enter** → コミット＆クローズ（表UXの「確定」を維持）
   - IME 変換中の Enter は IME へ委譲。`Esc` は既定どおり破棄。
2. **delegate `_MultilineTextDelegate(QStyledItemDelegate)`**：`createEditor`（複数行エディタ）/`setEditorData`/`setModelData`（`toPlainText()` を書戻し→ `result_items()` の `"\n"→"\N"` に乗る）/`updateEditorGeometry`。
3. **`_build_ui` で適用**：`table.setItemDelegateForColumn(_COL_TEXT, ...)` ＋ `table.setWordWrap(True)`。
4. **コミット後の行高再計算**：`resizeRowToContents`（または全行）。
5. キー定義は**名前付き定数**（例 `_NEWLINE_MODIFIERS = Qt.AltModifier | Qt.ShiftModifier`）。

## A-3. 設計（確定時の15文字再ラップ＝§F-1 案②・確定）

**手動改行を尊重しつつ、長い行は必ず15文字で折る**。実装は**バックエンド一元化**とし、GUI に `max_line_length` を渡さずに済ませる。

### A-3.1 共通ラップ関数を新設（reuse）

`subtitle_generator.py` にモジュール関数を追加し、`_split_lines` もこれへ委譲（挙動不変・重複排除）。

```python
# テキストを最大文字数で改行(\N)する。既存の \N は「強制改行」として尊重し、
# 各区間をさらに max_line_length ごとに分割して連結する（手動改行＋15文字ラップの併用）。
def wrap_by_length(text, max_line_length):
    if max_line_length <= 0 or not text:
        return text
    chunks = []
    for segment in text.split("\\N"):          # 手動/既存の強制改行で一旦分割
        if len(segment) <= max_line_length:
            chunks.append(segment)
            continue
        s = segment
        while len(s) > max_line_length:        # 長い区間を15文字ごとに折る
            chunks.append(s[:max_line_length])
            s = s[max_line_length:]
        if s:
            chunks.append(s)
    return "\\N".join(chunks)
```

- `_split_lines(text)` は `return wrap_by_length(text, self._max_line_length)` に置換（認識時の既存15文字改行は不変＝R1後段「継続」）。

### A-3.2 確定時に再ラップを適用（`_review_timeline`）

ダイアログ確定直後（＝「確定時」）に、使用エントリの text へ `wrap_by_length` を適用する。編集結果 text は既に `\N` 化済みのため、手動改行を強制改行として維持したまま15文字ラップが加わる。

```python
# _review_timeline 内: 使用チェック分を組み立てる箇所（現 L537-541）
max_len = int(subtitle_cfg.get("max_line_length", 15))
used = [
    {"start": e["start"], "end": e["end"], "text": wrap_by_length(e["text"], max_len)}
    for e in edited
    if e.get("use", True)
]
```

- これにより **確定時に再ラップ**（§F-1）を満たしつつ、`build_subtitle_file`（L309）の `\n→\N` 安全網とも整合。
- GUI（`subtitle_editor_dialog.py`）側に設定値を渡す必要がなく、影響を最小化。

## A-4. 想定挙動

```
セル編集: Shift+Enter / Alt+Enter=改行, Enter=確定, Esc=破棄
確定（字幕決定）→ result_items() が "\n"→"\N"
             → _review_timeline が wrap_by_length で「手動改行＋15文字ラップ」を適用 → 焼込
```

---

# B. R2 — 処理実行中のコンソール窓を出さない ＋「開始」ボタン削除

## B-1. 現状調査（コンソール窓の原因）

本体 GUI は `main_window.spec` で **`console=False`（windowed）** ビルド（L41）。この windowed プロセスから**コンソール系サブプロセス（FFmpeg/ffprobe）を起動すると、Windows が新規コンソール窓を割り当てて黒い cmd 窓が点滅**する。原因は以下4か所が **`creationflags` 未指定**であること。

| 起動箇所 | 種別 | 用途 |
|---|---|---|
| `src/utils/progress.py:43` | `subprocess.Popen` | 本体エンコード（`run_ffmpeg_progress`） |
| `src/modules/silence_cutter.py:37` | `subprocess.run` | `silencedetect`（無音検出） |
| `src/modules/volume_analyzer.py:39` | `subprocess.run` | `volumedetect`（音量解析） |
| `src/modules/ffmpeg_runner.py:61` | `subprocess.run` | `ffprobe`（総尺取得） |

> 音声認識（faster-whisper）は **ライブラリ内実行でサブプロセスを起こさない**ため対象外。

## B-2. 設計（コンソール抑止・新規ヘルパ＝§F-3 確定）

### B-2.1 新規ファイル `src/utils/proc.py`（§F-3 確定）

```python
# サブプロセス関連の共通ユーティリティ
import subprocess


# サブプロセス起動時にコンソール窓を出さないためのフラグを返す
# Windows: CREATE_NO_WINDOW / 非Windows: 0（フラグ無し・無害）
def no_window_creationflags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
```

- `CREATE_NO_WINDOW`（`0x08000000`）は Windows 専用のため `getattr` で存在時のみ取得。他OSは `0`。

### B-2.2 4か所へ `creationflags` を付与

各 `Popen`/`run` に `creationflags=no_window_creationflags()` を追加（他引数不変）。標準出力/標準エラーは従来どおり `PIPE`/`capture_output` のため、**ログ・進捗・stderr_tail は影響なし**（窓のみ抑止）。

```python
# progress.py（Popen）/ silence_cutter.py・volume_analyzer.py・ffmpeg_runner.py（run）
# それぞれ from ..utils.proc import no_window_creationflags を追加し
# creationflags=no_window_creationflags() を渡す
```

## B-3. 設計（「開始」ボタン削除＝§F-2 確定）

`settings_window.py` の「開始」ボタンは `CREATE_NEW_CONSOLE` で `main.py` を別コンソール起動する経路。要望どおり**削除**する（R2 の主旨＝処理中コンソール撲滅にも合致）。

### B-3.1 削除対象

| 箇所 | 内容 | 対応 |
|---|---|---|
| L296-299・L303 | 「開始」`QPushButton` 生成・`clicked.connect(self._on_start)`・`button_layout.addWidget(start_button)` | 削除（「保存」ボタンは残す） |
| L865-894 | `_on_start` メソッド全体 | 削除 |
| L31 | `MAIN_SCRIPT = ...`（`_on_start` 専用） | 他参照が無ければ削除 |
| L3 | `import subprocess`（`_on_start` 専用） | 他参照が無ければ削除 |

- **注意**：`import sys` は `main()` の `QApplication(sys.argv)`（L899）で使用中のため**残す**。`os` は他所参照があるため残す。削除前に各シンボルの残参照をグレップで確認してから外す。
- 削除後、設定画面はタブ外に **「保存」ボタンのみ**となる。パイプライン実行は `main_window.py`（GUI ランチャ）の「実行」ボタンに一本化される（既存機能で充足）。

---

# C. R3 — 稼働証明スピナー（Claude Code 風・status_label のみ＝§F-4 確定）

## C-1. 現状調査

`main_window.py` の状態表示は **`status_label`（`QLabel`）と `progress_bar`** のみ（L234-238）。`_on_progress`（L294-296）で `ratio`/`label` を反映するが、**音声認識など進捗率が細かく出ない工程では動かず「止まって見える」**。稼働アニメーションは存在しない。

## C-2. 設計（`QTimer` 駆動スピナー・§F-4 確定：status_label のみ）

1. **定数**
   ```python
   # 稼働アニメーションのコマ（Claude Code 風）と更新間隔
   _SPINNER_FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
   _SPINNER_INTERVAL_MS = 120
   ```
2. **タイマー生成（`_build_ui` 末尾）**：`QTimer`（interval=120ms, `timeout`→`_tick_spinner`）、`self._spinner_index=0`、`self._spinner_message=""`。
3. **開始/停止を実行状態に同期**：`_set_running(True)` で `start()`、`False` で `stop()`。`_on_progress` は `self._spinner_message = label` の更新のみ（実描画はタイマーへ一本化＝ちらつき防止）。
   ```python
   def _tick_spinner(self):
       frame = _SPINNER_FRAMES[self._spinner_index % len(_SPINNER_FRAMES)]
       self._spinner_index += 1
       self.status_label.setText(f"{frame} {self._spinner_message}")
   ```
4. **完了/失敗/中断**：`_set_running(False)` でタイマー停止後、`status_label` を確定文言（「完了: …」等）へ上書き（記号を残さない）。
5. スピナーは**メインスレッドの `QTimer`** で駆動し、進捗が来ない工程でも一定間隔で回り続け**生存を示す**（要望の主旨）。**表示先は `status_label` のみ**（ウィンドウタイトル等へは出さない＝§F-4）。

> フレーム集合（点字ブレイル `⠋⠙…`）は Windows 標準フォントで表示可能。万一の表示崩れに備え ASCII 版 `["|","/","-","\\"]` へ差し替えできるよう定数化（§F-5・既定は上記ブレイル）。

---

# D. 変更ファイル一覧

| 区分 | ファイル | 変更概要 | 要望 |
|---|---|---|---|
| 変更 | `src/gui/subtitle_editor_dialog.py` | 複数行エディタ＋delegate 追加、字幕列へ適用、コミット後の行高再計算 | R1 |
| 変更 | `src/modules/subtitle_generator.py` | `wrap_by_length()` 新設、`_split_lines` を委譲、`_review_timeline` で確定時再ラップ | R1 |
| 新規 | `src/utils/proc.py` | `no_window_creationflags()` | R2 |
| 変更 | `src/utils/progress.py` | `Popen` に `creationflags` 付与 | R2 |
| 変更 | `src/modules/silence_cutter.py` | `subprocess.run` に `creationflags` 付与 | R2 |
| 変更 | `src/modules/volume_analyzer.py` | 同上 | R2 |
| 変更 | `src/modules/ffmpeg_runner.py` | ffprobe `run` に `creationflags` 付与 | R2 |
| 変更 | `src/settings/settings_window.py` | 「開始」ボタン・`_on_start`・`MAIN_SCRIPT`・不要 `import subprocess` を削除 | R2 |
| 変更 | `src/gui/main_window.py` | スピナー `QTimer`／定数／開始・停止／`_tick_spinner`、`_on_progress` をメッセージ更新へ | R3 |
| 変更なし | `pipeline_runner.py` / `setting.json` | ロジック・設定は不変 | 全 |

---

# E. エラー処理・整合性

| 事象 | 方針 |
|---|---|
| IME 変換確定 Enter とショートカット衝突（R1） | 変換未確定中は改行/確定に使わず IME へ委譲。 |
| 再ラップと手動改行の二重（R1） | `wrap_by_length` は既存 `\N` を強制改行として温存し、長区間のみ追加分割。空区間（`\N\N`）はそのまま保持。 |
| `creationflags` 付与でログ欠落懸念（R2） | 出力は `PIPE`/`capture_output` のままで**ログ・進捗・stderr_tail は不変**。 |
| 非Windows 実行（R2） | `getattr(..., 0)` で `0`＝無害。クロスプラットフォーム安全。 |
| 「開始」削除の副作用（R2） | 実行導線は `main_window.py` の「実行」に一本化。削除前に `MAIN_SCRIPT`/`subprocess` の残参照を確認し dead import を除去。 |
| スピナーが停止後も残る（R3） | `_set_running(False)` でタイマー停止＋確定文言で上書き。 |

---

# F. 確認事項（回答反映状況）

| # | 項目 | 回答/決定 |
|---|---|---|
| 1 | 手動編集後の15文字再ラップ | **確定時に再ラップする（案②）** → §A-3 |
| 2 | settings_window「開始」コンソール経路 | **「開始」ボタンを削除** → §B-3 |
| 3 | コンソール抑止ヘルパの配置 | **新規 `src/utils/proc.py`** → §B-2.1 |
| 4 | スピナー表示位置 | **`status_label` のみ** → §C-2 |
| 5 | スピナー記号セット/間隔 | 既定：ブレイル `⠋⠙…` / 120ms（ASCII 差し替え可・定数化）。異論あれば指定ください |
| 6 | 修飾キー | 既定：Alt+Enter・Shift+Enter 両対応で固定（`setting.json` 化なし）。異論あれば指定ください |

---

# G. 実装ステップ（承認後）

1. **R1**：`subtitle_editor_dialog.py` に複数行エディタ＋delegate を追加し字幕列へ適用（行高再計算）。`subtitle_generator.py` に `wrap_by_length()` を新設し `_split_lines` を委譲、`_review_timeline` に確定時再ラップを追加。
2. **R2**：`src/utils/proc.py` を新設。FFmpeg/ffprobe 起動4か所へ `creationflags` を付与。`settings_window.py` の「開始」ボタン・`_on_start`・不要 import/定数を削除。
3. **R3**：`main_window.py` にスピナー `QTimer`・定数・開始/停止・`_tick_spinner` を実装、`_on_progress` をメッセージ更新へ。
4. 動作確認（`/verify` 相当・windowed 配布版で）：
   - R1: Shift+Enter/Alt+Enter で改行→焼込 ASS に `\N` が入り、長行は15文字で再ラップされる。
   - R2: 実行中に cmd 窓が一切出ない（4工程）。設定画面に「開始」ボタンが無い。ログ/エラー要約は従来どおり。
   - R3: 実行中スピナーが回転し、認識工程でも回り続け、完了/失敗で停止する。

---

# H. まとめ

- **R1**：原因は字幕セルの編集エディタが単一行 `QLineEdit`（§A-1.1）。**複数行 delegate 化**で手動改行を可能にし、**確定時に `wrap_by_length` で「手動改行＋15文字ラップ」を併用**（§A-3・案②確定）。
- **R2**：windowed 本体から起動する FFmpeg/ffprobe **4か所が `creationflags` 未指定**で cmd 窓が出る。新規 `proc.py` の `CREATE_NO_WINDOW` を付与し、あわせて `CREATE_NEW_CONSOLE` を使う**「開始」ボタンを削除**（§B）。
- **R3**：`status_label` が静的で稼働が見えない。**`QTimer` 駆動スピナー**を `status_label` に表示し、進捗の無い工程でも生存を示す（§C）。
- `pipeline_runner.py`・`setting.json` は不変。既存削除は明示要望の「開始」ボタンに限定。
