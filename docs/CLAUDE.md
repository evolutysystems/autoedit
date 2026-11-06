# Goal
フルテロップ動画編集を1クリックで実行できる自動編集パイプラインを構築する。

# Context
- Python 3.14を使用する
- FFmpegの進捗管理には run_ffmpeg_progress を使用する
- 設定値は src/settings/setting.json に保存される
- src/settings/settings_window.py は各種設定値を管理する画面である
- プロジェクト構造および既存コードを必ず調査してから設計すること

# Constraints
- コメントアウトは日本語を使用する
- 関数には目的が分かるコメントを記載する
- 既存実装を破壊しない
- 不要なライブラリを追加しない
- ハードコードは禁止し、設定可能な値は setting.json で管理する
- 実装前に設計を行うこと
- 不明点がある場合は推測実装せず設計書へ記載すること

# Documents
- 現行の設計書は docs/design/design.md (視覚版は design.html)
- 文書全体の索引と「現行 / 履歴」の区別は docs/README.md
- docs/request, docs/error, docs/quality, docs/security, docs/reduce はその時点の記録 (履歴)
  であり、現行仕様の根拠として単独で引用しない

# Current Directory Structure
/docs
    README.md               ドキュメント索引 (現行 / 履歴の区別)
    CLAUDE.md               本書 (開発の決まり + 初期要件)
    HowToRelease.md         リリース手順
    /design                 現行設計書 (design.md / design.html / design.css / test_matrix.md)
    /howto                  利用者マニュアル・Timeline 機能ガイド・PR 文
    /layout                 画面レイアウトの HTML/CSS 版
    /request /error /quality /security /reduce /analyze   履歴文書
/src
    main.py  version.py  exceptions.py
    /pipeline               1本編集・再開のオーケストレーション
    /modules                FFmpeg / Whisper を叩く単機能
    /timeline               編集情報の唯一のソースとレンダリング (Qt 非依存)
    /archive                VOD 採点・切り抜き
    /blur                   トラッキングぼかし
    /export                 DaVinci Resolve 出力
    /services               認証・ポイント・課金・ログ送信
    /gui                    PySide6 の画面 (/gui/timeline に編集画面)
    /i18n                   表示文言 (ja / en)
    /settings               setting.json / settings_window.py (DEFAULT_SETTINGS)
    /utils                  ログ・進捗・容量・ゴミ箱・自動更新
/tests                      標準 unittest (python -m unittest discover -s tests)
/installer                  Inno Setup のインストーラ (README.md が配布の正典)
/tools                      ライセンス収集などの補助スクリプト

# Requirements (初期要件 / 2026-05-31 時点の記録)
> 本節は v1.0 を作るときの要件であり、**現行仕様ではない**。
> 現在はアーカイブ切り抜き・Timeline 編集画面・縦動画・ぼかし・Resolve 出力・
> ログイン/ポイント/サブスクまで広がっている。現行仕様は docs/design/design.md を参照。
> 以下は「最初に何を求めたか」を残すために保持している。

以下を1クリックで実行可能な動画編集パイプラインを構築する。
① 無音カット
    入力動画を解析し、setting.json に定義された条件に従って無音区間を除去する。
    例：
        無音判定閾値
        最小無音時間
        フェード有無
    上記パラメータは setting.json から取得する。

② フルテロップ生成
    無音カット後の動画に対してフルテロップを生成する。
    要件：
    音声認識でASSファイルを作成し、テロップを焼き込む
    setting.json に保存された文字色を使用する
    将来的なフォント変更に対応できる構造とする
    テロップ生成処理をモジュール化する
    テロップON/OFFを設定可能にする

③ オープニング・エンディング結合
    setting.json に登録された動画素材を利用して結合する。
    構成：
        Opening
        ↓
        編集済み動画
        ↓
        Ending
    要件：
        オープニング未設定時はスキップ
        エンディング未設定時はスキップ
        結合処理はFFmpegで行う

④ パイプライン統合
    以下の順で実行する。
        動画入力
        無音カット
        フルテロップ生成
        オープニング結合
        エンディング結合
        動画出力
    各工程の進捗を run_ffmpeg_progress で表示する。

# Design Requirements
まず設計を行うこと。設計書は既にあるため、**新規作成ではなく docs/design/design.md の更新**で行う。
対象ファイル。
    docs/design/design.md
以下を含める。
    システム概要
    処理フロー図
    モジュール構成図
    クラス構成
    関数一覧
    setting.json 定義
    エラー処理方針
    ログ出力方針
    将来拡張方針
    
docs/design/design.html
design.md の内容を視覚的に整理したHTMLを作成する。
    要件：
        フローチャート
        モジュール構成図
        設定項目一覧
        ダークテーマ対応

docs/design/design.css
    design.html 用のスタイルシートを作成する。

# Expected Output
以下を出力する。
- docs/design/design.md
- docs/design/design.html
- docs/design/design.css
必要なディレクトリ・ファイルが不足している場合は追加提案を行う。

# Do Not
- いきなり実装を開始しない
- 既存コードを削除しない
- 推測で setting.json を変更しない
- 既存設定との互換性を失わない
- 不要なライブラリを導入しない

# First Step
最初に以下を実施する。
- プロジェクト構造を調査 (docs/README.md → docs/design/design.md の順に読む)
- 関連ソースを調査
- 現状分析
- 設計書 (docs/design/design.md または docs/request/verN/resolveM.md) の作成・更新

調査対象ソースは src/ と tests/ とする。
初期要件にあった参照元 ../StreamPipeline/dev は現在ローカルに存在しないため、
推測の根拠にしてはならない (faster-whisper の採用と run_ffmpeg_progress は
src/modules/subtitle_generator.py / src/utils/progress.py で実装済み)。

実装は設計書レビュー後に行う。