# Timeline 編集画面パッケージ (ver3 / docs/request/ver3/resolve.md §6.1.1)
# 表示と入力のみを担い、編集ロジックは src/timeline (Qt 非依存) 側に置く。
# モデルの変更は必ず TimelineController 経由 (= コマンド) で行う。
