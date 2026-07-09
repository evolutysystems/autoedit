# resolve12 — ①無音カットのON/OFF設定追加 ②動画選択のドラッグ&ドロップ調査 要望設計書

## 0. 本書の位置づけ
`docs/request/request12.md` の要望に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要なライブラリを追加しない）に従い、本書レビュー後に実装します。

要望は独立した2件です。
1. **無音カットの実行可否を設定項目として追加**（字幕は生成するが無音カットはしない、を選べるようにする）。
2. **動画ファイル選択をドラッグ&ドロップで実行できるかの調査**（実現可否）。

---

## 1. 要望（request12.md 要約）
1. 字幕は生成するが無音カットはしなくてよい。**設定項目に無音カットの可否を追加する**。
2. 動画ファイルの選択を**ドラッグ&ドロップで実行できるか調査**してほしい。

---

# 要望① 無音カットのON/OFF設定追加

## 2. 現状分析（関連コードの実測）

| 対象 | 現状 |
|---|---|
| `src/pipeline/pipeline_runner.py` L46-53 | パイプラインは `_apply_volume_analysis`（音量解析・カット閾値確認ダイアログ）→ `silence_cutter.run` の順で**必ず無音カットを実行**する。ON/OFF の概念なし。 |
| `src/modules/silence_cutter.py` L396-461 `run(context)` | `silence_cut` / `volume_analysis` 設定を読み、無音検出→有音区間結合を実行し `context.set_current_video_path(output_path)` で結果を次工程へ渡す。**先頭に有効/無効判定が無い**。 |
| `src/settings/setting.json` L60-68 `silence_cut` | `noise_threshold_db` / `min_silence_duration_sec` / `fade_enabled` / `fade_duration_sec` / `extract_mode` / `batch_size` / `concat_reencode`。**`enabled` キーは無い**。 |
| `src/settings/settings_window.py` L165-171 `DEFAULT_SETTINGS["silence_cut"]` | 上記同様、`enabled` の既定なし。 |
| 同 L442-462 `_build_general_tab`（無音カット節） | UI は「フェード ON/OFF」「フェード時間(秒)」のみ。無音カット自体の ON/OFF トグルは無い。 |
| 同 L834/L846-847（load）・L954-957（collect） | `silence_cut` の load/collect は fade 系のみ扱う。 |
| **参考: 既存の ON/OFF 実装パターン** `src/modules/subtitle_generator.py` L738-741 | `if not subtitle_cfg.get("enabled", True): return context.current_video_path()` で**工程をスキップ（入力をそのまま次工程へ通す）**。テロップ側は既にこの方式で ON/OFF 済み。 |

**結論**：テロップ生成は既に `subtitle.enabled` で ON/OFF 可能であり、**同じパターンを `silence_cut.enabled` として無音カットへ横展開**すればよい。変更は「設定キー追加」「`silence_cutter.run` 先頭のスキップ判定」「音量解析ダイアログのスキップ連動」「設定画面の UI 1点追加」に閉じる。

## 3. 設計方針（要望①）

1. 新規キー **`silence_cut.enabled`（bool, 既定 `true`）** を追加する（ハードコード禁止・setting.json 管理）。既定 `true` により**既存挙動を維持**（後方互換）。
2. `silence_cutter.run` の**先頭でスキップ判定**する。無効時はログを出して `context.current_video_path()` を返し、**入力動画をそのまま次工程（テロップ生成）へ通す**（subtitle_generator の既存パターンに合わせる）。
3. **音量解析・カット閾値確認ダイアログ（`_apply_volume_analysis`）を無音カット無効時はスキップ**する。当ダイアログは「無音カットの閾値(dB)」を確定するためのものであり、無音カットしないなら閲覧・確定させる意味が無く、ユーザーに無用な操作を強いるため。
4. 設定画面（一般タブ・無音カット節）に **「無音カットを有効にする」チェックボックス**を追加する（既存の fade トグルと同じ並び）。
5. 進捗表示（`begin_step/end_step`）は現状のまま。無効時もステップ枠は残り進捗バーが素早く進むだけで、テロップ側スキップ時と同じ挙動（表示上の破綻なし）。

## 4. 実装詳細（要望①）

### 4.1 `silence_cutter.run` 先頭にスキップ判定を追加（`src/modules/silence_cutter.py` L396-400 付近）
```python
def run(context):
    settings = context.settings
    silence_cfg = settings.get("silence_cut", {})
    ffmpeg_cfg = settings.get("ffmpeg", {})

    # 無音カット ON/OFF (resolve12)。無効時は入力をそのまま次工程へ通す。
    if not silence_cfg.get("enabled", True):
        _logger.info("無音カットスキップ (silence_cut.enabled=false)")
        return context.current_video_path()
    ...
```
> `set_current_video_path` を呼ばず現在パスを返すことで、テロップ生成は**元の入力動画**に対して実行される（subtitle_generator の enabled=false と同一挙動）。

### 4.2 音量解析ダイアログのスキップ連動（`src/pipeline/pipeline_runner.py`）
`_apply_volume_analysis`（L105-）の早期 return 条件に無音カット無効を追加する。
```python
def _apply_volume_analysis(context):
    settings = context.settings
    va_cfg = settings.setdefault("volume_analysis", {})

    # 無音カット無効時は閾値確認自体が不要のためスキップ (resolve12)
    if not settings.get("silence_cut", {}).get("enabled", True):
        return

    # 機能無効時は何もしない (既存)
    if not va_cfg.get("enabled", True):
        return
    ...
```
> 代替案として `run_pipeline` 側で `_apply_volume_analysis(context)` 呼び出しをガードする方法もあるが、判定を `_apply_volume_analysis` 内に閉じ込める方が呼び出し側の見通しが良く、既存の早期 return 群と一貫する。

### 4.3 設定既定値の追加（`src/settings/settings_window.py` L165-171）
```python
"silence_cut": {
    "enabled": True,          # 新規(resolve12): 無音カット ON/OFF
    "noise_threshold_db": -30,
    "min_silence_duration_sec": 0.6,
    "fade_enabled": False,
    "fade_duration_sec": 0.05,
    "extract_mode": "seek",
},
```
> 旧 `setting.json`（`enabled` キー無し）で起動しても、`_merge_with_defaults`（L283-284 付近）が `enabled=True` を**自動補完**するため既存ファイルはそのまま動作する（既定 ON＝従来挙動）。

### 4.4 設定画面 UI（`src/settings/settings_window.py` `_build_general_tab` 無音カット節 L446 付近）
既存「フェード」トグルの**直前**に無音カット ON/OFF を配置する。
```
無音カット   [☑ 無音カットを有効にする]   ← 新規 silence_enabled_check
フェード     [☑ セグメント境界にフェードを付与する]  ← 既存
フェード時間(秒) [__0.05__]                       ← 既存
```
- ウィジェット参照初期化（L331 付近, `self.fade_enabled_check = None` の近傍）へ `self.silence_enabled_check = None` を追加。
- `_build_general_tab` 無音カット節（L446 付近）へ：
  ```python
  # 無音カット ON/OFF (resolve12)
  self.silence_enabled_check = QCheckBox("無音カットを有効にする")
  grid.addWidget(self._make_column_label("無音カット"), row, 0)
  grid.addWidget(self.silence_enabled_check, row, 1)
  row += 1
  ```
- `_load_to_ui`（L846 付近）へ：
  ```python
  self.silence_enabled_check.setChecked(bool(silence_cut.get("enabled", True)))
  ```
- `_collect_settings`（L954-957 `silence_cut` update 辞書）へ：
  ```python
  "enabled": self.silence_enabled_check.isChecked(),
  ```

### 4.5 実 setting.json への反映
`src/settings/setting.json` の `silence_cut` へ `"enabled": true` を追記する。CLAUDE.md「推測で setting.json を変更しない」に配慮し、**追記する値は既定と同じ `true`（＝現行挙動を一切変えない）**ため互換性は保たれる。既定補完で動作するため、この追記は必須ではないが**明示のため推奨**。

## 5. データフロー（無音カット ON/OFF）
```
setting.json(silence_cut.enabled)
  │
run_pipeline
  ├─ _apply_volume_analysis
  │     └─ silence_cut.enabled=false → ダイアログを出さずスキップ (resolve12)
  └─ silence_cutter.run
        ├─ enabled=true  → 従来どおり無音検出→有音区間結合→set_current_video_path
        └─ enabled=false → ログ出力し current_video_path() を返す(入力を素通し)
              └─ 後続の subtitle_generator は元入力に対しテロップ生成
```

---

# 要望② 動画選択のドラッグ&ドロップ調査

## 6. 調査結論（実現可否）

**結論：実現可能。低コストで実装でき、追加ライブラリは不要。**

- 本アプリの GUI は **PySide6（Qt）** で実装されている（`src/gui/main_window.py` L31-44）。Qt は**ドラッグ&ドロップを標準機能として提供**しており、`QWidget.setAcceptDrops(True)` と `dragEnterEvent` / `dropEvent` のオーバーライドのみで実装できる。
- 入力パスは既に `QLineEdit`（`self.input_edit`, L279）で管理され、参照ボタン（`_on_browse`, L329-335）が `setText(path)` するだけの単純構造。ドロップ時も**同じ `self.input_edit.setText(path)` を呼ぶだけ**で既存の実行フロー（`_on_run` が `input_edit.text()` を読む, L338-339）にそのまま乗る。
- 新規依存は不要（PySide6 に内包）。CLAUDE.md「不要なライブラリを追加しない」に適合。

## 7. 現状分析（要望②）

| 対象 | 現状 |
|---|---|
| `src/gui/main_window.py` L251 `MainWindow(QWidget)` | Qt ウィジェット。`setAcceptDrops` 未設定のため現状はドロップを受け付けない。 |
| 同 L273-285 入力動画行 | `QLabel("入力動画:")` + `self.input_edit(QLineEdit)` + `browse_button`。パスは `input_edit` に集約。 |
| 同 L329-335 `_on_browse` | ファイルダイアログで選択し `self.input_edit.setText(path)`。ドロップ時の設定先と共通化できる。 |
| 同 L49 `_VIDEO_FILE_FILTER` | `*.mp4 *.mov *.avi *.mkv *.flv *.wmv`。ドロップ時の拡張子検証に流用できる。 |
| 同 L338-342 `_on_run` | `input_edit.text()` を読み `os.path.exists` で検証。ドロップで `input_edit` に入れば既存検証がそのまま効く。 |
| 同 L366-374 `_set_running` | 実行中は `input_edit` 等を `setEnabled(False)` にする。ドロップも実行中は無効化すべき（下記 §8.3）。 |

## 8. 実装設計（要望②：実装まで行う場合）

> 本要望は「調査してほしい」であり、実装可否の判断はレビュー後とする。以下は**実装する場合の設計**（追加のみ・既存破壊なし）。

### 8.1 ドロップ受け入れの有効化（`MainWindow.__init__` / `_build_ui`）
```python
# ウィンドウ全体でファイルのドロップを受け付ける (resolve12)
self.setAcceptDrops(True)
```

### 8.2 イベントハンドラの追加（`MainWindow` にメソッド追加）
```python
# ドラッグされたものが「単一のローカル動画ファイル」のときのみドロップを受理する
def dragEnterEvent(self, event):
    if self._is_acceptable_drop(event):
        event.acceptProposedAction()
    else:
        event.ignore()

# ドロップされた動画ファイルパスを入力欄へ設定する
def dropEvent(self, event):
    path = self._dropped_video_path(event)
    if path:
        self.input_edit.setText(path)
        event.acceptProposedAction()
    else:
        event.ignore()

# ドロップ内容が受理可能な動画ファイルか判定する (拡張子・実行中でないこと)
def _is_acceptable_drop(self, event):
    if self._worker is not None and self._worker.isRunning():
        return False  # 実行中はドロップを受け付けない
    return self._dropped_video_path(event) is not None

# MIME からローカル動画ファイルパスを1件取り出す (非対応なら None)
def _dropped_video_path(self, event):
    mime = event.mimeData()
    if not mime.hasUrls():
        return None
    for url in mime.urls():
        local = url.toLocalFile()
        if local and os.path.splitext(local)[1].lower() in _VIDEO_EXTENSIONS:
            return local
    return None
```

### 8.3 拡張子集合の定数化（`_VIDEO_FILE_FILTER` の近傍 L49）
ハードコード回避と `_VIDEO_FILE_FILTER` との二重管理防止のため、受理拡張子を定数化する。
```python
# ドロップ受理対象の動画拡張子 (_VIDEO_FILE_FILTER と整合させる)
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv"}
```

### 8.4 UI 上の導線（任意・推奨）
プレースホルダー文言に D&D 可能である旨を追記し、発見性を上げる（機能追加ではなく文言のみ）。
```python
self.input_edit.setPlaceholderText("動画ファイルをここにドラッグ&ドロップ、または「参照...」")
```
> 既存は `video_directory` をプレースホルダーに使用（L278-280）。文言変更の要否は §12 未確定事項で確認。

### 8.5 挙動仕様
- **受理**：単一のローカル動画ファイル（対応拡張子）→ `input_edit` にパット設定。以降は既存フローと同一。
- **非受理**：非ファイル（URL/テキスト等）・非対応拡張子・複数ファイルの1件目が非対応 → ドロップ無効（カーソルが拒否表示）。
- **実行中**：`_worker` 稼働中はドロップ不可（`input_edit` 無効化と整合）。
- 実在チェックは既存 `_on_run`（L340）がそのまま担うため、ドロップ側で重複検証はしない（存在しないパスは実行時に弾かれる）。

---

## 9. 変更ファイル一覧

### 要望①（無音カット ON/OFF）
| ファイル | 変更内容 | 節 |
|---|---|---|
| `src/modules/silence_cutter.py` | `run` 先頭に `silence_cut.enabled` スキップ判定を追加（無効時は入力素通し）。 | §4.1 |
| `src/pipeline/pipeline_runner.py` | `_apply_volume_analysis` に無音カット無効時のスキップを追加。 | §4.2 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["silence_cut"]` に `enabled` 追加。UI チェックボックス追加・ウィジェット参照追加・load/collect 拡張。 | §4.3,4.4 |
| `src/settings/setting.json` | `silence_cut` に `"enabled": true` を追記（値は現行挙動と同一）。 | §4.5 |

### 要望②（ドラッグ&ドロップ：実装する場合のみ）
| ファイル | 変更内容 | 節 |
|---|---|---|
| `src/gui/main_window.py` | `setAcceptDrops(True)`／`dragEnterEvent`・`dropEvent`・判定ヘルパ追加／`_VIDEO_EXTENSIONS` 定数追加／（任意）プレースホルダー文言変更。 | §8 |

> 要望②は追加のみで既存メソッドの削除・改変を伴わない。

---

## 10. 後方互換・エラー処理方針
- **既定 ON**：`silence_cut.enabled` 既定 `true`。旧 setting.json は `_merge_with_defaults` が自動補完し**従来と完全に同一挙動**。
- **無音カット無効時**：入力動画を素通しし、テロップ生成のみ実行。音量解析ダイアログも出さない（無用な操作を排除）。処理は正常継続（工程スキップであり異常終了ではない）。
- **D&D 非対応データ**：受理せずドロップ無効化（クラッシュしない）。実在検証は既存 `_on_run` に委譲。
- **D&D 実行中**：`_worker` 稼働チェックでドロップを抑止し、実行中の入力書き換えを防ぐ。
- **既存機能非破壊**：両要望とも「追加」中心。無音カット処理本体・字幕処理・参照ボタン等の既存経路は不変。

## 11. テスト観点（レビュー後の検証項目）

### 要望①
1. 設定画面で「無音カットを有効にする」を **OFF** → 保存 → 実行。**無音カットが行われず**（尺が縮まない）、テロップは元動画に対して生成・焼き込みされる。
2. OFF 時、**音量解析・カット閾値確認ダイアログが出ない**。
3. ON（既定）で従来どおり無音カットが実行される（回帰なし）。
4. 旧 setting.json（`enabled` 無し）で起動 → 既定 ON で従来動作、エラーなし。
5. OFF 時に OP/ED 結合・出力まで正常完了する（素通し動画が後続工程で破綻しない）。
6. 設定の保存/再読込で `silence_cut.enabled` が正しく往復する。

### 要望②（実装した場合）
7. 対応拡張子の動画をウィンドウへドロップ → `input_edit` にパスが入り、そのまま実行できる。
8. 非対応拡張子・フォルダ・テキストのドロップは拒否表示になり `input_edit` は変化しない。
9. 実行中はドロップを受け付けない。
10. 参照ボタン経由の従来選択が引き続き動作する（回帰なし）。

## 12. 未確定事項（要ユーザー確認）
- **要望②の実装要否**：request12 は「実現できるか調査してほしい」。本書の結論は**実現可能（低コスト・追加依存なし）**。§8 の実装まで進めてよいか。
- **`input_edit` のプレースホルダー文言変更（§8.4）**の要否（現行は入力ディレクトリ表示）。
- **無音カット OFF 時の音量解析ダイアログ抑止（§4.2）**：本設計は「無音カットしないなら閾値確認も不要」として**抑止**を既定とする。仮に閾値の測定・記録のみ残したい要件があれば別途調整。
- **UI ラベル文言**：「無音カットを有効にする」で問題ないか（既存の「テロップ生成を有効にする」に語感を合わせている）。
