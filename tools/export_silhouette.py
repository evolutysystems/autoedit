# 身体の輪郭モデル (MobileSAM) を ONNX へ書き出す (ver5 resolve3 §3.1 / Phase 0)
#
# **開発時に 1 回だけ**行う作業。できた .onnx を src/models/ へ置けば、
# 配布物へ torch も MobileSAM のコードも増えない (resolve3 Q8)。
# 依存を配布物へ混ぜないよう、**変換専用の仮想環境**で行う (手順は src/models/README.md)。
#
#   python -m venv --system-site-packages exportenv         (torch の CPU 版が入った Python から)
#   exportenv\Scripts\python -m pip install --no-deps timm onnx onnxscript onnx_ir ml_dtypes
#   exportenv\Scripts\python -m pip install --no-deps torchvision --index-url https://download.pytorch.org/whl/cpu
#   git clone --depth 1 https://github.com/ChaoningZhang/MobileSAM.git tools/weights/silhouette/MobileSAM
#   exportenv\Scripts\python tools/export_silhouette.py --verify
#
# 重み (tools/weights/silhouette/MobileSAM/weights/mobile_sam.pt) はリポジトリへ入れない (.gitignore 参照)。
#
# 入出力の約束 (src/blur/silhouette.py の format="mobilesam" と必ず揃えること):
#   エンコーダ  silhouette_encoder.onnx
#     入力  image            float32[1, 3, 1024, 1024]  RGB / 長辺を 1024 へ縮小し右と下を 0 で詰める
#                                                     / ImageNet の平均と分散 (0〜255 の尺度) で正規化済み
#     出力  image_embeddings float32[1, 256, 64, 64]
#   デコーダ  silhouette_decoder.onnx (MobileSAM 同梱の SamOnnxModel / 1 枚だけ返す)
#     入力  image_embeddings float32[1, 256, 64, 64]
#           point_coords     float32[1, N, 2]   1024 へ縮小した座標。枠は (左上, 右下) の 2 点
#           point_labels     float32[1, N]      枠の左上 = 2 / 右下 = 3
#           mask_input       float32[1, 1, 256, 256]  使わないので 0
#           has_mask_input   float32[1]               0
#           orig_im_size     float32[2]               (高さ, 幅) 出力マスクの大きさ
#     出力  masks            float32[1, 1, H, W]      logit (0 より大きい所が人物)
#           iou_predictions  float32[1, 1]
import argparse
import os
import sys
import time

# リポジトリのルート (tools/ の 1 つ上)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SOURCE_DIR = os.path.join(_ROOT, "tools", "weights", "silhouette", "MobileSAM")
_DEFAULT_WEIGHTS = os.path.join(_SOURCE_DIR, "weights", "mobile_sam.pt")
_DEFAULT_ENCODER = os.path.join(_ROOT, "src", "models", "silhouette_encoder.onnx")
_DEFAULT_DECODER = os.path.join(_ROOT, "src", "models", "silhouette_decoder.onnx")

# エンコーダの入力の一辺 (setting.json の blur.silhouette.input と揃える)
_INPUT = 1024
# ONNX の演算セット (onnxruntime 1.24 で読める範囲)
_OPSET = 17


def main(argv=None):
    parser = argparse.ArgumentParser(description="MobileSAM を ONNX へ書き出す")
    parser.add_argument("--weights", default=_DEFAULT_WEIGHTS)
    parser.add_argument("--encoder", default=_DEFAULT_ENCODER)
    parser.add_argument("--decoder", default=_DEFAULT_DECODER)
    parser.add_argument("--verify", action="store_true",
                        help="書き出した ONNX と PyTorch 版の出力を突き合わせる")
    args = parser.parse_args(argv)

    if not os.path.isdir(_SOURCE_DIR):
        print(f"MobileSAM のソースがありません: {_SOURCE_DIR}", file=sys.stderr)
        return 1
    sys.path.insert(0, _SOURCE_DIR)

    import torch
    from mobile_sam import sam_model_registry
    from mobile_sam.utils.onnx import SamOnnxModel

    model = sam_model_registry["vit_t"](checkpoint=args.weights)
    model.eval()

    os.makedirs(os.path.dirname(args.encoder), exist_ok=True)
    image = torch.randn(1, 3, _INPUT, _INPUT, dtype=torch.float32)
    with torch.no_grad():
        torch.onnx.export(
            model.image_encoder, (image,), args.encoder, export_params=True,
            opset_version=_OPSET, do_constant_folding=True, dynamo=False,
            input_names=["image"], output_names=["image_embeddings"])
    print(f"エンコーダを書き出しました: {args.encoder}")

    decoder = SamOnnxModel(model, return_single_mask=True)
    embed_dim = model.prompt_encoder.embed_dim
    embed_size = model.prompt_encoder.image_embedding_size
    mask_size = [4 * x for x in embed_size]
    dummy = {
        "image_embeddings": torch.randn(1, embed_dim, *embed_size, dtype=torch.float),
        "point_coords": torch.randint(low=0, high=_INPUT, size=(1, 2, 2), dtype=torch.float),
        "point_labels": torch.tensor([[2.0, 3.0]], dtype=torch.float),
        "mask_input": torch.zeros(1, 1, *mask_size, dtype=torch.float),
        "has_mask_input": torch.tensor([0.0], dtype=torch.float),
        "orig_im_size": torch.tensor([270.0, 480.0], dtype=torch.float),
    }
    with torch.no_grad():
        torch.onnx.export(
            decoder, tuple(dummy.values()), args.decoder, export_params=True,
            opset_version=_OPSET, do_constant_folding=True, dynamo=False,
            input_names=list(dummy.keys()),
            output_names=["masks", "iou_predictions", "low_res_masks"],
            dynamic_axes={"point_coords": {1: "num_points"}, "point_labels": {1: "num_points"}})
    print(f"デコーダを書き出しました: {args.decoder}")

    if args.verify:
        return _verify(args, dummy)
    return 0


# 突き合わせ用の入力: 縦横のグラデーションと縞を、エンコーダの前処理と同じ尺度へ正規化したもの
def _smooth_image():
    import numpy as np

    ys, xs = np.mgrid[0:_INPUT, 0:_INPUT].astype(np.float32) / _INPUT
    red = 255.0 * xs
    green = 255.0 * ys
    blue = 127.5 * (1.0 + np.sin(xs * 12.0) * np.cos(ys * 9.0))
    rgb = np.stack([red, green, blue], axis=0)
    mean = np.array([123.675, 116.28, 103.53], dtype=np.float32).reshape(3, 1, 1)
    std = np.array([58.395, 57.12, 57.375], dtype=np.float32).reshape(3, 1, 1)
    return ((rgb - mean) / std)[None].astype(np.float32)


# PyTorch 版と ONNX 版の出力を突き合わせる (誤差 1e-3 以下を合格とする)
def _verify(args, dummy):
    import numpy as np
    import onnxruntime
    import torch
    from mobile_sam import sam_model_registry
    from mobile_sam.utils.onnx import SamOnnxModel

    # 書き出しに使ったモデルは torch.onnx.export が内部状態 (TinyViT の注意バイアスの控え) を
    # 変えてしまい、そのまま比べると 0.6 ほどずれる。読み込み直したモデルと比べる。
    model = sam_model_registry["vit_t"](checkpoint=args.weights)
    model.eval()
    decoder = SamOnnxModel(model, return_single_mask=True)

    encoder_session = onnxruntime.InferenceSession(args.encoder,
                                                   providers=["CPUExecutionProvider"])
    decoder_session = onnxruntime.InferenceSession(args.decoder,
                                                   providers=["CPUExecutionProvider"])
    # 乱数ではなく、絵らしい滑らかな入力で突き合わせる
    image = torch.from_numpy(_smooth_image())
    with torch.no_grad():
        expected_embed = model.image_encoder(image).numpy()
        expected_masks = decoder(*dummy.values())[0].numpy()
    started = time.perf_counter()
    actual_embed = encoder_session.run(None, {"image": image.numpy()})[0]
    elapsed = time.perf_counter() - started
    actual_masks = decoder_session.run(None, {k: v.numpy() for k, v in dummy.items()})[0]

    embed_gap = float(np.max(np.abs(expected_embed - actual_embed)))
    mask_gap = float(np.max(np.abs(expected_masks - actual_masks)))
    print(f"エンコーダの最大誤差 {embed_gap:.2e} / デコーダの最大誤差 {mask_gap:.2e} "
          f"/ エンコーダ 1 回 {elapsed * 1000:.0f}ms")
    return 0 if embed_gap <= 1e-3 and mask_gap <= 1e-3 else 1


if __name__ == "__main__":
    sys.exit(main())
