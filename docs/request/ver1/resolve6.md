# resolve6: 未使用項目の洗い出し と 設定画面の横幅縮小

`docs/request/request6.md` への回答。`src/settings/settings_window.py` が画面表示している項目のうち、
パイプライン側（`src/modules`・`src/pipeline`・`src/main.py`・`src/gui`）で**実際には参照されていない項目**を列挙し、
あわせて設定画面の横幅を縮小した。

---

## 1. 調査方法

各タブの入力ウィジェットに対応する `setting.json` のキーを、`settings_window.py` を除く
全ソース（`src/**`、`build/` 配下のビルド成果物は除外）で参照箇所を grep し、
読み出して処理に使われているかを確認した。

| 区分 | 主な消費元 |
|---|---|
| general | `main.py` / `gui/main_window.py` / `modules/concat_processor.py` / `modules/output_writer.py` |
| silence_cut | `modules/silence_cutter.py` |
| subtitle / font | `modules/subtitle_generator.py` |

---

## 2. 実際に使用していない表示項目

| 画面の項目名 | 設定キー | タブ | 状況 |
|---|---|---|---|
| 配信スタイル | `subtitle.broadcast_style` | 字幕 | **完全に未使用**。`settings_window.py` と `setting.json` 以外に参照が無く、どの処理にも渡らない。 |
| しきい値 | `subtitle.threshold` | 字幕 | **完全に未使用**。同上。無音判定は `silence_cut.noise_threshold_db`、テロップ絞り込みは `subtitle.speech_threshold_db` を使用しており、この `threshold` はどこからも読まれない。 |
| 最小発話無視時間 | `subtitle.min_speech_ignore_time` | 字幕 | **実質未使用（死にコード）**。`silence_cutter.py:391` で `min_silence_duration_sec` のフォールバック値として渡されるのみ。`silence_cut.min_silence_duration_sec` は UI / `DEFAULT_SETTINGS` で常に値を持つため、このフォールバックには到達せず、入力値が処理に影響することはない。 |

### 補足
- `subtitle.review_on_empty_skip` は `setting.json` と `DEFAULT_SETTINGS` には存在するが**画面には表示されていない**ため、本リストの対象外（「表示している項目」ではない）。
- 上記以外の表示項目（動画/オープニング/エンディング/出力ディレクトリ、無音カット系、テロップ生成・字幕編集・発話検出・Whisper デバイス/精度・各フォント/装飾/余白/表示位置 等）は、いずれもパイプライン側で参照され使用されていることを確認済み。

### 取り扱いについて（削除実施済み）
ユーザー承認のもと、上記3項目を以下のとおり削除した（`py_compile` と JSON 妥当性で検証済み）。

**`src/settings/settings_window.py`**
1. 定数 `BROADCAST_STYLES` / `PLACEHOLDER_THRESHOLD` / `PLACEHOLDER_MIN_IGNORE` を削除
2. ウィジェット参照初期化 `style_combo` / `threshold_edit` / `min_ignore_edit` を削除
3. `_build_subtitle_tab` の「配信スタイル」「しきい値」「最小発話無視時間」行を削除
4. `_load_to_ui` / `_collect_settings` の対応行を削除
5. `DEFAULT_SETTINGS["subtitle"]` の `broadcast_style` / `threshold` / `min_speech_ignore_time` を削除

**`src/settings/setting.json`**
- `subtitle.broadcast_style` / `subtitle.threshold` / `subtitle.min_speech_ignore_time` を削除

**`src/modules/silence_cutter.py`**
- `run()` の `subtitle_cfg.get("min_speech_ignore_time")` フォールバック参照を削除。
  併せて未使用になった `subtitle_cfg = settings.get("subtitle", {})` も削除（`min_silence_duration_sec` の決定には `silence_cut.min_silence_duration_sec` のみを使用）。

---

## 3. 設定画面の横幅縮小（実施済み）

### 原因
ウィンドウの実効最小幅は `WINDOW_WIDTH` ではなく、**「項目名ラベル幅 + 入力欄の最小幅 + 参照ボタン + 余白」**で決まる。
従来 `INPUT_FIELD_MIN_WIDTH = 280` が大きく、`WINDOW_WIDTH = 350` を指定しても実際にはそれより広く開いていた。

### 変更内容（`src/settings/settings_window.py`）

| 定数 | 変更前 | 変更後 |
|---|---|---|
| `WINDOW_WIDTH` | 350 | **300** |
| `COLUMN_LABEL_WIDTH` | 120 | **100** |
| `INPUT_FIELD_MIN_WIDTH` | 280 | **160** |

横幅を支配する `INPUT_FIELD_MIN_WIDTH` を主に縮小した。長い項目名ラベルは Qt が内容に応じて自動で広がるため、
`COLUMN_LABEL_WIDTH` は最小値の引き下げのみで文字切れは発生しない。高さ（`WINDOW_HEIGHT`）と各項目の機能は変更していない。
