# トラッキングぼかしのモデル置き場 (ver5 resolve2 §3.7)

このフォルダへ `.onnx` を置くと、トラッキングぼかし機能が使えるようになる。
**ファイルが無い場合、機能は自動で無効になる**（設定画面に理由が出るだけで、
アプリの起動にも既存の書き出しにも影響しない / §8-6）。

| ファイル | 中身 | ライセンス | 入手方法 |
| --- | --- | --- | --- |
| `yolox_tiny.onnx` | 人物検出 (YOLOX-Tiny / 入力 416x416) | Apache-2.0 | [YOLOX](https://github.com/Megvii-BaseDetection/YOLOX) の公式配布 ONNX をそのまま置く |
| `osnet_x0_25.onnx` | 全身 ReID (OSNet-x0.25 / 入力 128x256) | MIT | `python tools/export_osnet.py` で `.pth` から変換する |
| `silhouette_encoder.onnx` | 人物の輪郭の画像エンコーダ (MobileSAM / 入力 1024x1024) | Apache-2.0 | `tools/export_silhouette.py` で `.pt` から変換する (ver5 resolve3 §3.1) |
| `silhouette_decoder.onnx` | 人物の輪郭のデコーダ (MobileSAM / 枠を与えて形を返す) | Apache-2.0 | 同上 |

輪郭モデル (`silhouette_*.onnx`) が無い場合は、**人物を角の丸い四角でぼかす**（ぼかし機能そのものは使える）。

## 入手手順

### yolox_tiny.onnx

```powershell
Invoke-WebRequest "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx" -OutFile "src/models/yolox_tiny.onnx"
```

### osnet_x0_25.onnx

torch / torchreid を配布物へ混ぜないよう、**変換専用の仮想環境**で行う。
PyPI の `torchreid` は公式版ではないため、公式の GitHub から入れる
（Cython の拡張をビルドするため Microsoft C++ Build Tools が要る）。

```powershell
python -m venv D:\venv\osnet-export
D:\venv\osnet-export\Scripts\Activate.ps1
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install numpy Cython onnx onnxruntime gdown
pip install --no-build-isolation git+https://github.com/KaiyangZhou/deep-person-reid.git

# 重み: torchreid Model Zoo「MSMT17 (combineall=True)」の osnet_x0_25
gdown 1Kkx2zW89jq_NETu4u42CFZTMVD5Hwm6e -O tools/weights/osnet_x0_25_msmt17.pth

# 書き出して PyTorch 版と突き合わせる（「PyTorch 版と一致しました。」で完了）
python tools/export_osnet.py --verify
deactivate
```

### silhouette_encoder.onnx / silhouette_decoder.onnx

MobileSAM の公式の重みを ONNX へ書き出す。torch と MobileSAM のコードを配布物へ混ぜないよう、
**変換専用の仮想環境**で行う（torch の CPU 版が入った Python から作る）。

```powershell
python -m venv --system-site-packages D:\venv\sam-export
D:\venv\sam-export\Scripts\python -m pip install --no-deps timm onnx onnxscript onnx_ir ml_dtypes
D:\venv\sam-export\Scripts\python -m pip install --no-deps torchvision --index-url https://download.pytorch.org/whl/cpu
git clone --depth 1 https://github.com/ChaoningZhang/MobileSAM.git tools/weights/silhouette/MobileSAM

# 書き出して PyTorch 版と突き合わせる（最大誤差が 1e-3 以下なら終了コード 0）
D:\venv\sam-export\Scripts\python tools/export_silhouette.py --verify
```

候補の比較 (2026-09-17 / 依頼者の素材 1920x1080・5 人・CPU 7 スレッド):

| 候補 | 1 枚あたり | 大きさ | 結果 |
| --- | --- | --- | --- |
| **MobileSAM** | エンコーダ約 510ms + 1 人約 50ms | 44.5MB | **採用** |
| EfficientSAM (Ti) 公式 ONNX | 約 2,170ms (5 人) | 41.4MB | 輪郭の出来は同程度だが 2 倍遅い |
| MobileSAM の int8 量子化 | 速くならない | 15.8MB | 元の輪郭との一致度 (IoU) が平均 0.69 まで落ちる |

`.onnx` と `tools/weights/` は容量が大きいためリポジトリへは入れない（`.gitignore` 参照）。
リリース時は `src/main.spec` の `datas` がこのフォルダの `*.onnx` を拾って同梱する。

モデルを差し替えたら `setting.json` の `blur.model` を合わせること。
出力の解釈方法は `detector_format` / `reid_format` で切り替わる（§3.7.3）。
同梱物を増やしたときは `tools/license_manifest.json` へも追記し、
`python tools/collect_licenses.py` で `licenses/` を作り直す（§5.10）。
