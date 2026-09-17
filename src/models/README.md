# トラッキングぼかしのモデル置き場 (ver5 resolve2 §3.7)

このフォルダへ `.onnx` を置くと、トラッキングぼかし機能が使えるようになる。
**ファイルが無い場合、機能は自動で無効になる**（設定画面に理由が出るだけで、
アプリの起動にも既存の書き出しにも影響しない / §8-6）。

| ファイル | 中身 | ライセンス | 入手方法 |
| --- | --- | --- | --- |
| `yolox_tiny.onnx` | 人物検出 (YOLOX-Tiny / 入力 416x416) | Apache-2.0 | [YOLOX](https://github.com/Megvii-BaseDetection/YOLOX) の公式配布 ONNX をそのまま置く |
| `osnet_x0_25.onnx` | 全身 ReID (OSNet-x0.25 / 入力 128x256) | MIT | `python tools/export_osnet.py` で `.pth` から変換する |

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

`.onnx` と `tools/weights/` は容量が大きいためリポジトリへは入れない（`.gitignore` 参照）。
リリース時は `src/main.spec` の `datas` がこのフォルダの `*.onnx` を拾って同梱する。

モデルを差し替えたら `setting.json` の `blur.model` を合わせること。
出力の解釈方法は `detector_format` / `reid_format` で切り替わる（§3.7.3）。
同梱物を増やしたときは `tools/license_manifest.json` へも追記し、
`python tools/collect_licenses.py` で `licenses/` を作り直す（§5.10）。
