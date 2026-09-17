# トラッキングぼかし (ver5 resolve2)
#
# 人物・建物を追跡し、出力へぼかしを焼き込むための一式。
# 機能が無効 (blur.enabled = false) のときは、このパッケージのモジュールを
# 1 つも import しない経路になるよう、呼び出し側で先に設定を見ること (§4-1)。
#
#   config.py          setting.json の blur セクションを平坦化して返す
#   models.py          モデルファイルの解決と ONNX セッションの生成 (遅延 import)
#   detector.py        人物検出 (ONNX)。画像 → [矩形, スコア]
#   reid.py            全身 ReID (ONNX)。切り抜き画像 → 特徴ベクトル
#   tracker.py         区間内トラッキング (IoU + 埋め込み) と全体クラスタリング
#   region_tracker.py  静止物 (建物) の位相相関による追従
#   analyzer.py        解析の入口。対象区間の決定・デコード・進捗・キャンセル
#   store.py           解析結果キャッシュの読み書き (指紋による整合性確認)
#   decisions.py       指定 (source["blur"]) の読み書きと、囲みパスの当て込み
#   mask_builder.py    トラック + 指定 → マスク動画の生成
#   geometry.py        素材ピクセル ↔ キャンバスピクセルの変換
