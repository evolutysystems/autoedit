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

# Current Directory Structure
/docs
    /design
        design.md
        claude.md
/src
    /modules
    /settings
        setting.json
        settings_window.py

# Requirements
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
まず設計を行うこと。
以下を作成する。
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
- プロジェクト構造を調査
- 関連ソースを調査
- 現状分析
- 設計書作成

調査対象ソースは以下
- ../StreamPipeline/dev

実装は設計書レビュー後に行う。