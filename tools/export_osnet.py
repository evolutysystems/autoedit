# OSNet-x0.25 を ONNX へ書き出す (ver5 resolve2 §3.7.2 / Phase 0)
#
# **開発時に 1 回だけ**行う作業。できた .onnx を src/models/ へ置けば、
# 配布物へ torchreid も torch も増えない (R10 を維持する)。
# 依存を配布物へ混ぜないよう、**変換専用の仮想環境**で行う (手順は src/models/README.md)。
#
#   pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
#   pip install numpy Cython onnx onnxruntime gdown
#   pip install --no-build-isolation git+https://github.com/KaiyangZhou/deep-person-reid.git
#     (PyPI の torchreid は公式版ではないため使わない。ビルドに C++ Build Tools が要る)
#   gdown 1Kkx2zW89jq_NETu4u42CFZTMVD5Hwm6e -O tools/weights/osnet_x0_25_msmt17.pth
#   python tools/export_osnet.py --verify   src/models/osnet_x0_25.onnx を作って突き合わせる
#
# 完了条件 (Phase 0): 同じ画像で PyTorch 版と**出力ベクトルが一致する** (誤差 1e-4 以下)。
#
# 入出力の約束 (src/blur/reid.py と必ず揃えること):
#   入力   float32[1, 3, 256, 128] (縦 256 x 横 128) / RGB / 0〜1 へ割る
#   正規化 ImageNet の mean (0.485, 0.456, 0.406) / std (0.229, 0.224, 0.225)
#   出力   float32[1, 512] (使う側で L2 正規化する)
import argparse
import os
import sys

# リポジトリのルート (tools/ の 1 つ上)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_OUT = os.path.join(_ROOT, "src", "models", "osnet_x0_25.onnx")

# 既定で使う重み。MSMT17 は撮影条件の幅が広く、屋外配信への一般化が効きやすい (§3.7.2)。
# torchreid の Model Zoo「MSMT17 (combineall=True) -> Market1501 & DukeMTMC-reID」の
# osnet_x0_25 (Google Drive)。torchreid は重みの名前から取ってくる機能を持たないため、
# 手で落としたファイルを指す。重みはリポジトリへ入れない (.gitignore 参照)。
_DEFAULT_WEIGHTS = os.path.join(_ROOT, "tools", "weights", "osnet_x0_25_msmt17.pth")
_WEIGHTS_DRIVE_ID = "1Kkx2zW89jq_NETu4u42CFZTMVD5Hwm6e"

# 入力の大きさ (幅, 高さ)。setting.json の blur.model.reid_input と揃える。
_INPUT_W = 128
_INPUT_H = 256

# 学習時の分類器。学習データの人数ぶんの出力を持ち、推論では使わないため読み捨てる。
_CLASSIFIER_PREFIX = "classifier."


# torchreid のモデルを組み立てて重みを読む
def _build_model(weights):
    if not os.path.isfile(weights):
        print(f"重みのファイルがありません: {weights}")
        print("torchreid の Model Zoo から落としてください:")
        print(f"  gdown {_WEIGHTS_DRIVE_ID} -O {_DEFAULT_WEIGHTS}")
        raise SystemExit(2)
    try:
        import torch                            # noqa: PLC0415
        import torchreid                        # noqa: PLC0415
    except ImportError as error:
        print("torchreid と torch が要ります (この作業のときだけ):", error)
        print("  手順はこのファイルの先頭のコメントを参照")
        raise SystemExit(2) from error

    model = torchreid.models.build_model(
        name="osnet_x0_25",
        num_classes=1,              # 推論しか使わないため分類器の出力数は何でもよい
        loss="softmax",
        pretrained=False,
    )
    _load_weights(torch, model, weights)

    model.eval()
    # 特徴ベクトルだけを出させる (分類器を通さない)
    if hasattr(model, "classifier"):
        model.classifier = torch.nn.Identity()
    return torch, model


# .pth を読んでモデルへ流し込む。
# load_state_dict(strict=False) は**形の違い**までは見逃さないため、分類器 (人数ぶんの出力) で
# 必ず止まる。分類器だけを捨て、それ以外が 1 つでも欠けたら中断する
# (欠けたまま書き出すと、初期値のままの層が混ざった「それらしいが使えない」モデルになる)。
def _load_weights(torch, model, path):
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    except Exception:                           # noqa: BLE001 (古い保存形式は pickle の中身を含む)
        # Model Zoo の公式配布物に限って、中身を実行する読み方を許す
        print("weights_only で読めなかったため、通常の読み込みに切り替えます "
              "(公式配布の重みだけに使うこと)")
        checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    state = checkpoint.get("state_dict", checkpoint)

    expected = model.state_dict()
    loaded = {}
    discarded = []
    for key, value in state.items():
        name = key[len("module."):] if key.startswith("module.") else key
        if name in expected and tuple(expected[name].shape) == tuple(value.shape):
            loaded[name] = value
        else:
            discarded.append(name)

    missing = [name for name in expected
               if name not in loaded and not name.startswith(_CLASSIFIER_PREFIX)]
    if missing:
        print(f"重みが OSNet-x0.25 と合いません ({len(missing)} 件の層が読めません):")
        for name in missing[:10]:
            print(f"  {name}")
        raise SystemExit(1)

    model.load_state_dict(loaded, strict=False)
    print(f"重みを読みました: {len(loaded)} 件"
          f" (読み捨て {len(discarded)} 件: {', '.join(discarded) or 'なし'})")


# ONNX へ書き出す
def export(weights, out_path):
    torch, model = _build_model(weights)

    dummy = torch.randn(1, 3, _INPUT_H, _INPUT_W, dtype=torch.float32)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    torch.onnx.export(
        model, dummy, out_path,
        input_names=["images"], output_names=["features"],
        # 1 フレームに何人いても 1 回で回せるよう、件数だけ可変にする
        dynamic_axes={"images": {0: "batch"}, "features": {0: "batch"}},
        opset_version=12,
        # torch 2.9 以降の既定 (dynamo=True) は onnxscript を要求し、opset も勝手に上げる。
        # 従来の TorchScript 方式で書き出す (onnx パッケージは要る)。
        dynamo=False,
    )
    size_mb = os.path.getsize(out_path) / 1024.0 / 1024.0
    print(f"書き出しました: {out_path} ({size_mb:.2f} MB)")
    return out_path


# PyTorch 版と ONNX 版の出力が一致するか確かめる (Phase 0 の完了条件)
def verify(weights, out_path):
    try:
        import numpy as np                      # noqa: PLC0415
        import onnxruntime                      # noqa: PLC0415
    except ImportError as error:
        print("確認には numpy と onnxruntime が要ります:", error)
        return 2

    torch, model = _build_model(weights)
    sample = torch.randn(1, 3, _INPUT_H, _INPUT_W, dtype=torch.float32)
    with torch.no_grad():
        expected = model(sample).numpy()

    session = onnxruntime.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    actual = session.run(None, {session.get_inputs()[0].name: sample.numpy()})[0]

    if expected.shape != actual.shape:
        print(f"出力の形が違います: PyTorch {expected.shape} / ONNX {actual.shape}")
        return 1
    diff = float(np.max(np.abs(expected - actual)))
    print(f"出力の最大差: {diff:.2e} (次元 {actual.shape[1]})")
    if diff > 1e-4:
        print("誤差が大きすぎます (完了条件は 1e-4 以下)")
        return 1
    print("PyTorch 版と一致しました。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="OSNet-x0.25 を ONNX へ書き出す (トラッキングぼかしの ReID モデル)")
    parser.add_argument("--weights", default=_DEFAULT_WEIGHTS,
                        help="学習済み重み (.pth) のパス")
    parser.add_argument("--out", default=_DEFAULT_OUT, help="書き出し先の .onnx")
    parser.add_argument("--verify", action="store_true",
                        help="書き出した後に PyTorch 版と出力を突き合わせる")
    args = parser.parse_args(argv)

    export(args.weights, args.out)
    print()
    print("次にやること:")
    print(f"  1. {args.out} が src/models/ にあることを確認する")
    print("  2. licenses/ を作り直す: python tools/collect_licenses.py")
    print("     (torchreid の LICENSE は配布元のファイルへ差し替えること)")
    print("  3. どの重みから作ったかを licenses/README.txt の版へ記入する")

    if args.verify:
        return verify(args.weights, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
