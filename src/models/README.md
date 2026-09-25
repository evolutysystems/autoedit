# トラッキングぼかしのモデル置き場 (ver5 resolve8 §9.4)

このフォルダへ `.onnx` を置くと、ぼかしの追従が検出モデルの助けを借りられるようになる。
**ファイルが無くてもぼかし機能は使える**（囲んだ場所を模様の変化＝位相相関だけで追うようになり、
追従が途切れやすくなるだけ。設定画面には理由が 1 行出る / ver5 resolve8 §5.13）。

| ファイル | 中身 | ライセンス | 入手方法 |
| --- | --- | --- | --- |
| `yolox_tiny.onnx` | 人物検出 (YOLOX-Tiny / 入力 416x416) | Apache-2.0 | [YOLOX](https://github.com/Megvii-BaseDetection/YOLOX) の公式配布 ONNX をそのまま置く |

ver5 resolve8 で次の 2 つは**廃止した**。`src/models/` に残っていても読み込まれず、
配布物にも同梱されない（`main_window.spec` が `yolox_tiny.onnx` だけを名前で拾う）。
ディスクを空けたい場合は削除してよい。

| 廃止したファイル | 由来 | なぜ要らなくなったか |
| --- | --- | --- |
| `osnet_x0_25.onnx` | 全身 ReID (OSNet) | 人物へまとめる処理を廃止し (ver5 resolve7)、人物の枠そのものも廃止した (resolve8) |
| `silhouette_encoder.onnx` / `silhouette_decoder.onnx` | 身体の輪郭 (MobileSAM) | 指定の相手が「マウスで囲んだ矩形」になり、輪郭モデルの当てどころが無くなった |

## 入手手順

### yolox_tiny.onnx

```powershell
Invoke-WebRequest "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx" -OutFile "src/models/yolox_tiny.onnx"
```

公式配布の重みをそのまま使う (release 0.1.1rc0)。NOTICE ファイルが配布元にある場合は、
`licenses/YOLOX/` へ併せて置くこと (`tools/license_manifest.json` が同梱を検証する)。

## 使われ方 (ver5 resolve8)

```text
囲んだ矩形 → 前の位置の周りを切り出す → YOLOX で人物を探す
   見つかった … その枠の**中心**へ移す (大きさは囲んだまま)
   無かった   … 前後 2 枚の位相相関で平行移動ぶんだけ補う
```

大きさを検出枠に合わせないのは、「囲んだ大きさは利用者が決めるもの」という決めごと
(ver5 resolve8 §3.3 / §4-7) のため。
