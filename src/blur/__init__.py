# トラッキングぼかし (ver5 resolve8)
#
# マウスで囲んだ場所を追いかけ、出力へぼかしを焼き込むための一式。
# 機能が無効 (blur.enabled = false) のときは、このパッケージのモジュールを
# 1 つも import しない経路になるよう、呼び出し側で先に設定を見ること (§4-1)。
#
#   config.py       setting.json の blur セクションを平坦化して返す
#   decisions.py    指定 (source["blur"]) の読み書き。全面ぼかしと囲み + キーフレーム
#   plan.py         その時刻に何をぼかすかを決める唯一の場所 (キーフレーム + 追従の合成)
#   tracker.py      囲んだ場所の追従 (キーフレームで区切った区間ごと)
#   store.py        追従結果キャッシュの読み書き (指紋による整合性確認)
#   frames.py       素材の区間を 1 枚ずつデコードする
#   correlate.py    位相相関で 2 枚の絵のずれを求める
#   detector.py     人物検出 (ONNX)。追従の助けとして使う
#   models.py       モデルファイルの解決と ONNX セッションの生成 (遅延 import)
#   mask_builder.py 指定 + 追従結果 → マスク動画の生成
#   preview.py      フレーム 1 枚へ書き出しと同じぼかしを当てる
#   geometry.py     素材ピクセル ↔ キャンバス ↔ 正規化座標の変換
#   contour.py      v4 以前から引き継いだ自由な囲みの形
