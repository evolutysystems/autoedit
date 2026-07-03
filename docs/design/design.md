# 自動編集パイプライン 設計書

最終更新日: 2026-05-31
対象: autoedit プロジェクト (フルテロップ動画編集 1クリック自動化)

---

## 1. システム概要

### 1.1 目的
入力動画 1 本に対し、以下 4 工程を 1 クリックで自動実行するパイプラインを提供する。

1. 無音カット
2. フルテロップ生成
3. オープニング・エンディング結合
4. 動画出力

### 1.2 想定実行環境
- OS: Windows 11
- Python: 3.14
- GUI フレームワーク: PySide6 (既存 `settings_window.py` で採用済み)
- 外部ツール: FFmpeg (PATH 配置を前提)
- 既存依存: 既存 `setting.json` / `settings_window.py` を破壊しない

### 1.3 非機能要件
- 既存設定 (`general`, `subtitle`) を維持し、追加項目のみマージする方式で互換性を維持
- 設定はすべて `src/settings/setting.json` 経由で外部化 (ハードコード禁止)
- 進捗は `run_ffmpeg_progress` を介して表示 (※実体未確認のため後述「不明点」参照)

---

## 2. 処理フロー

### 2.1 全体フロー (1 クリック実行時)

```
[開始]
   │
   ▼
[1] 設定読込 (setting.json)
   │
   ▼
[2] 入力動画選択 / 取得
   │
   ▼
[3] 無音カット処理 ─── (silence_cutter)
   │     └─ FFmpeg silencedetect → セグメント抽出 → 結合
   ▼
[4] フルテロップ生成 ─── (subtitle_generator)
   │     ├─ 音声認識 (faster-whisper, エンジン差替可能)
   │     ├─ ASS 字幕ファイル生成 (タイムライン + 文字色)
   │     └─ FFmpeg subtitles/ass フィルタで ASS を焼き込み
   │   ※テロップ ON/OFF 設定で本工程をスキップ可
   ▼
[5] オープニング結合 ─── (concat_processor)
   │     └─ 未設定時はスキップ
   ▼
[6] エンディング結合 ─── (concat_processor)
   │     └─ 未設定時はスキップ
   ▼
[7] 動画出力 (output_writer)
   │
   ▼
[終了]
```

### 2.2 進捗表示
- 各工程開始時にステップ名を通知
- FFmpeg 実行は `run_ffmpeg_progress` 経由で進捗率を取得 (実体仕様は要確認)

---

## 3. モジュール構成

```
src/
├── main.py                     # エントリーポイント (新規)
├── pipeline/                   # パイプライン制御 (新規)
│   ├── __init__.py
│   ├── pipeline_runner.py      # 全工程のオーケストレーション
│   └── pipeline_context.py     # 各工程間で受け渡す中間データ
├── modules/                    # 既存 (各処理モジュール格納)
│   ├── __init__.py
│   ├── silence_cutter.py       # ① 無音カット (新規)
│   ├── subtitle_generator.py   # ② フルテロップ生成 (新規)
│   ├── concat_processor.py     # ③ オープニング / エンディング結合 (新規)
│   ├── output_writer.py        # ④ 出力 (新規)
│   └── ffmpeg_runner.py        # FFmpeg 共通実行ラッパ (新規)
├── settings/                   # 既存
│   ├── setting.json            # 既存 (項目追加)
│   └── settings_window.py      # 既存 (項目追加)
└── utils/                      # 新規
    ├── __init__.py
    ├── logger.py               # ログ出力
    └── progress.py             # run_ffmpeg_progress ラッパ (要調査)
```

### 3.1 モジュール責務一覧

| モジュール | 責務 |
|---|---|
| `pipeline_runner` | 各工程を順次呼び出し、`PipelineContext` を介して入出力パスを引き継ぐ |
| `pipeline_context` | 入力/中間/出力パス、設定値、進捗コールバックを保持する DTO |
| `silence_cutter` | 無音区間を検出し、有音区間のみ結合した中間動画を生成 |
| `subtitle_generator` | テロップ用テキストとタイミングを生成し、動画に焼き込む |
| `concat_processor` | OP / 本編 / ED を FFmpeg concat demuxer で結合 |
| `output_writer` | 最終ファイルを所定パスに書き出し |
| `ffmpeg_runner` | FFmpeg コマンド組み立て・進捗監視を一元化 |
| `logger` | ファイル + コンソールへログ出力 |

---

## 4. クラス構成

### 4.1 主要クラス図 (テキスト表現)

```
PipelineRunner
 ├─ uses → SilenceCutter
 ├─ uses → SubtitleGenerator
 ├─ uses → ConcatProcessor
 ├─ uses → OutputWriter
 ├─ uses → FFmpegRunner
 └─ holds → PipelineContext

PipelineContext
 ├─ input_path : str
 ├─ working_dir : str
 ├─ intermediate_paths : dict[str, str]
 ├─ output_path : str
 ├─ settings : dict
 └─ progress_callback : Callable

SilenceCutter
 └─ run(context) -> str (中間動画パス)

SubtitleGenerator
 ├─ TextSource (抽象)
 │   └─ WhisperTextSource (faster-whisper による音声認識実装)
 ├─ FontProfile  ← フォント差替を吸収するための値オブジェクト
 └─ run(context) -> str
       音声認識 → ASS 生成 → ASS 焼き込み の順で処理

ConcatProcessor
 └─ run(context, position: Literal["opening", "ending"]) -> str

OutputWriter
 └─ run(context) -> str

FFmpegRunner
 ├─ execute(command, on_progress)
 └─ build_command(...)
```

### 4.2 クラス設計方針
- 各処理クラスは `run(context)` を持つ共通インタフェース風に統一し、`PipelineRunner` からポリモーフィックに呼び出す
- `SubtitleGenerator` は将来のフォント変更要件のため、`FontProfile` で family / size / color を抽象化
- 認識エンジンは `TextSource` 抽象を介して差替可能とし、既定実装として `WhisperTextSource` (faster-whisper) を提供する (参照元 `../StreamPipeline/dev` と同一エンジン)
- `subtitle.engine` 設定で利用エンジンを選択し、`"none"` 指定または faster-whisper 未導入時はテロップ工程を安全にスキップする
- 処理順は claude.md ② の指示どおり「音声認識で ASS ファイルを生成 → ASS を焼き込み」とする

---

## 5. 関数一覧

### 5.1 pipeline_runner.py

| 関数 | 引数 | 戻り値 | 説明 |
|---|---|---|---|
| `run_pipeline` | `input_path: str, settings: dict, progress_cb` | `str` (出力パス) | 1 クリック実行の本体 |
| `_prepare_context` | `input_path, settings` | `PipelineContext` | 一時ディレクトリ準備等 |
| `_cleanup` | `context` | `None` | 中間ファイル後処理 |

### 5.2 silence_cutter.py

| 関数 | 説明 |
|---|---|
| `detect_silence(input_path, threshold, min_duration)` | FFmpeg silencedetect 出力をパース |
| `build_keep_segments(silence_ranges, total_duration)` | 残す区間 (有音) のリストを生成 |
| `cut_and_concat(input_path, keep_segments, output_path, fade)` | 区間抽出→結合 |
| `run(context)` | 上記を順次実行する公開関数 |

### 5.3 subtitle_generator.py

| 関数 | 説明 |
|---|---|
| `resolve_text_source(settings)` | `subtitle.engine` に応じた `TextSource` 実装を返す (未対応 / 未導入 / "none" 時は None) |
| `WhisperTextSource.extract(input_path, language)` | faster-whisper で音声認識し、`start/end/text` のタイムラインを返す |
| `build_subtitle_file(timeline, font_profile, output_path)` | 認識結果から ASS 字幕ファイルを生成 |
| `burn_subtitle(input_path, subtitle_path, output_path)` | FFmpeg subtitles フィルタで ASS を焼き込み |
| `run(context)` | テロップ ON/OFF 判定の上で「音声認識 → ASS 生成 → 焼き込み」を実行 |

### 5.4 concat_processor.py

| 関数 | 説明 |
|---|---|
| `is_available(path)` | パスが空 or 未存在ならスキップ判定 |
| `concat(parts, output_path)` | FFmpeg concat demuxer 実行 |
| `run(context, position)` | OP/ED どちらかを結合 |

### 5.5 output_writer.py

| 関数 | 説明 |
|---|---|
| `resolve_output_path(input_path, output_dir, suffix)` | 衝突しない出力ファイル名を生成 |
| `finalize(intermediate_path, output_path)` | 中間ファイルを最終位置へ配置 |

### 5.6 ffmpeg_runner.py

| 関数 | 説明 |
|---|---|
| `execute(command, total_duration, on_progress)` | FFmpeg 実行 + 進捗パイプ |
| `probe_duration(path)` | ffprobe で総尺取得 |

### 5.7 utils/logger.py

| 関数 | 説明 |
|---|---|
| `get_logger(name)` | ファイル + コンソール出力ロガー取得 |

---

## 6. setting.json 定義

### 6.1 現状 (既存)
```json
{
    "general": {
        "video_directory": "...",
        "opening_video": "...",
        "ending_video": "..."
    },
    "subtitle": {
        "language": "ja",
        "broadcast_style": "ソロ",
        "threshold": "0.6",
        "min_speech_ignore_time": "0.6",
        "own_subtitle_color": "#ED1C24"
    }
}
```

### 6.2 拡張案 (追加項目のみ)
既存項目は維持し、以下のセクションを追加する。
`settings_window.py` の `DEFAULT_SETTINGS` にも対応追加を必須とする (互換維持)。

```json
{
    "general": {
        "video_directory": "...",
        "opening_video": "...",
        "ending_video": "...",
        "output_directory": ""
    },
    "subtitle": {
        "language": "ja",
        "broadcast_style": "ソロ",
        "threshold": "0.6",
        "min_speech_ignore_time": "0.6",
        "own_subtitle_color": "#ED1C24",
        "enabled": true,
        "font_family": "Yu Gothic UI",
        "font_size": 48,
        "engine": "whisper",
        "whisper_model": "large-v3",
        "whisper_device": "cpu",
        "whisper_compute_type": "int8",
        "whisper_beam_size": 8,
        "max_line_length": 15,
        "remove_fillers": false
    },
    "silence_cut": {
        "noise_threshold_db": -30,
        "min_silence_duration_sec": 0.6,
        "fade_enabled": false,
        "fade_duration_sec": 0.05
    },
    "ffmpeg": {
        "executable": "ffmpeg",
        "ffprobe_executable": "ffprobe",
        "video_codec": "libx264",
        "audio_codec": "aac",
        "preset": "medium",
        "crf": 20
    },
    "logging": {
        "log_dir": "logs",
        "level": "INFO"
    }
}
```

#### subtitle セクション 音声認識項目の意味
- `engine`: 音声認識エンジン識別子。`"whisper"` で faster-whisper を使用、`"none"` でテロップ工程をスキップ
- `whisper_model` / `whisper_device` / `whisper_compute_type` / `whisper_beam_size`: faster-whisper の `WhisperModel` / `transcribe` パラメータ (参照元 `../StreamPipeline/dev` に準拠)
- `max_line_length`: ASS 1 行あたりの最大文字数 (超過時は `\N` で改行)
- `remove_fillers`: フィラー(口癖)を除去するか
- 文字色は既存 `own_subtitle_color` を `FontProfile` 経由で ASS の PrimaryColour に反映する

### 6.3 互換性方針
- 起動時に欠落キーは `_merge_with_defaults` 相当でデフォルト補完
- 既存キー (`subtitle.threshold` 等) の意味・型は変更しない
- `subtitle.enabled` 欠落時は `true` を採用 (既存環境の動作維持)
- `subtitle.engine` 欠落時は `"whisper"` を採用。ただし faster-whisper 未導入環境では警告を出してテロップ工程をスキップし、パイプライン全体は継続する

---

## 7. エラー処理方針

| 区分 | 方針 |
|---|---|
| 設定不整合 | 起動時に検証し、ユーザに具体的なキー名を提示して中断 |
| 入力動画不存在 | パイプライン開始前にチェックし、即座に中断 |
| FFmpeg 失敗 | 戻り値 != 0 を例外化 (`FFmpegError`)、コマンドと stderr 末尾をログ |
| 工程内例外 | パイプラインは中断、中間ファイルは保持してデバッグ可能に |
| OP/ED 未設定 | 例外ではなく **スキップ** として正常継続 |
| 想定外例外 | 最上位で捕捉しユーザに「ログ参照」を案内 |

例外階層案:
```
AutoEditError
 ├─ ConfigError
 ├─ FFmpegError
 ├─ InputError
 └─ SubtitleError
```

---

## 8. ログ出力方針

- ライブラリ: Python 標準 `logging` のみ (追加依存なし)
- 出力先: コンソール + `logs/autoedit_YYYYMMDD.log`
- フォーマット: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
- レベル: `setting.json -> logging.level` で切替 (デフォルト INFO)
- FFmpeg コマンドは DEBUG レベルで全文出力、INFO では概要のみ
- 例外発生時は `logger.exception` でスタックトレース出力

---

## 9. 将来拡張方針

| 拡張対象 | 方針 |
|---|---|
| フォント変更 | `FontProfile` 値オブジェクト経由で UI/設定から差替 |
| 音声認識エンジン差替 | 既定 `WhisperTextSource` (faster-whisper)。`TextSource` 抽象経由で Vosk 等へ差替可能 (`subtitle.engine` で選択) |
| 配信スタイル別ルール | `subtitle.broadcast_style` を参照する Strategy で分岐 |
| 複数ファイル一括処理 | `PipelineRunner` を for ループで呼ぶ薄いラッパを追加 |
| GUI 連携 | `settings_window.py` に「実行」ボタンを追加し `run_pipeline` を呼ぶ |
| クラウド出力 | `OutputWriter` を抽象化し `LocalWriter` / `CloudWriter` を実装 |

---

## 10. 不明点 (推測実装せず本欄に記載)

claude.md の指示に従い、以下は **未確定** のため設計確定前にユーザ確認が必要。

| # | 項目 | 状況 | 想定対処 |
|---|---|---|---|
| 1 | `run_ffmpeg_progress` の実体 | claude.md に指定はあるが、参照先 `../StreamPipeline/dev` がローカルに存在しない (`D:\develop\StreamPipeline` 自体が無い) | 既存実装の所在を確認し、`utils/progress.py` で薄くラップする想定 |
| 2 | ~~テロップ生成のテキスト取得方法~~ | **解決済 (2026-05-31)**: claude.md ② の指示により音声認識で ASS を生成し焼き込む。エンジンは参照元 `../StreamPipeline/dev` と同じ faster-whisper を既定採用 | `WhisperTextSource` として実装済 |
| 3 | 配信スタイル「ソロ/複数人」の動作差 | 既存設定値の意味が未確認 | 既存仕様書/コードの所在をユーザに確認 |
| 4 | 出力ファイル命名規約 | 元ファイル名 + suffix で良いか確認要 | 暫定: `<original>_edited.mp4` |
| 5 | 一時ファイル配置 | `tempfile.TemporaryDirectory` で問題ないか | 暫定: OS 一時領域 |
| 6 | テロップ ON/OFF UI | `settings_window.py` への追加可否 | 追加前提で `subtitle.enabled` を設計に含めた |

---

## 11. 追加提案 (claude.md「必要なディレクトリ・ファイルが不足している場合は追加提案」を受けて)

| 追加対象 | 理由 |
|---|---|
| `src/main.py` | 1 クリック実行のエントリ。`settings_window.py` から呼び出す or 単独実行 |
| `src/pipeline/` パッケージ | パイプライン制御を `modules/` から分離し責務を明確化 |
| `src/utils/` パッケージ | ロガー・FFmpeg 進捗ラッパなど横断機能の置き場 |
| `logs/` ディレクトリ | ログ出力先 (起動時自動生成想定) |
| `tests/` ディレクトリ | 単体テスト用 (Python 標準 `unittest` を想定、新規依存なし) |

実装着手は本設計書のレビュー後とする (claude.md「実装は設計書レビュー後に行う」遵守)。
