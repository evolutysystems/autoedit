# フレーム 1 枚へ、書き出しと同じぼかしを当てる (ver5 resolve4 §3.4 / §5.6)
#
# ver5 resolve8 では相手が「解析結果 (人物の枠)」から「追従結果 (囲み)」へ変わった。
# 計算の中身は同じで、set_tracks で差し替える。
#
# 指定画面と Timeline 編集画面のプレビューは、これまで「赤い目印」や「赤い塗り」で
# ぼかす範囲を示すだけだった (resolve2 §5.7 / resolve3 §5.5.4)。
# 実際にぼけた絵が見えないと、強さ・輪郭の当たり方・「ぼかさない」で削られた形が
# 書き出すまで分からない。ここでは**書き出しと同じマスク (mask_builder) と
# 同じ効果 (blur.render.mode / strength)** を通して、画面と出力を一致させる。
#
# 速さのための決め事 (実測は resolve4 §2.6):
#   ・BlurPlan は指定が変わったときだけ作り直す (毎回作ると 1 枚 20.6ms かかる)
#   ・マスクが真っ黒なフレームは**入力をそのまま返す** (出力の大半がこれ)
#   ・ぼかすのはマスクが白い所の外接矩形だけ (全面 24.7ms → 枠だけ 10.4ms)
#   ・切り出しは 3σ ぶん広げる (切り口でガウスが折り返して境界に筋が出るのを防ぐ)
#
# PIL が無い環境ではぼかせない。その場合も**入力をそのまま返す** (画面は落とさない)。
from ..utils.logger import get_logger
from .config import MODE_PIXELATE, blur_sigma
from .geometry import source_to_canvas_transform
from .plan import BlurPlan

_logger = get_logger(__name__)

try:
    from PIL import Image, ImageFilter
    _PIL_AVAILABLE = True
except Exception:                                   # noqa: BLE001 (任意依存)
    _PIL_AVAILABLE = False

# ガウスの裾を拾うために切り出しへ足す余白 (σ の何倍か)
_CROP_SIGMA = 3.0

# 縮小してからぼかす境目 (ver5 resolve7 §5.10)。
# クリップ全体をぼかすようになり、切り出しがほぼ全画面になることが増えた。
# ガウスは σ に比例して重いため、大きい切り出しは縮めてからぼかす。
# 1/2 に縮めれば σ も半分でよく、費用はおおむね 1/4 になる。表示の確認にはこの精度で足りる。
_DOWNSCALE_MIN_PX = 640 * 360
_DOWNSCALE_MAX = 0.5


# ぼかしを当てられる環境か (画面の「実際のぼかし」を選べるかの判定に使う)
def is_available():
    return _PIL_AVAILABLE


class BlurPreview:

    #   timeline / tracks / decisions / cfg : BlurPlan と同じもの
    def __init__(self, timeline, tracks, decisions, cfg):
        self._timeline = timeline
        self._tracks = tracks or {}
        self._decisions = decisions
        self._cfg = cfg
        self._plan = None
        self._painter = None
        self._transforms = {}
        self._failed = False            # 一度失敗したら以降は素通しにする

    # 指定が変わったら呼ぶ (次に使うときに作り直す)
    def invalidate(self, decisions=None):
        if decisions is not None:
            self._decisions = decisions
        self._plan = None
        self._painter = None

    # 追従結果を差し替える (囲みを足した・追い直したあと)
    def set_tracks(self, tracks):
        self._tracks = tracks or {}
        self.invalidate()

    # 判定 (指定画面が枠の一覧にも使う)
    def plan(self):
        if self._plan is None:
            self._plan = BlurPlan(self._timeline, self._tracks, self._decisions, self._cfg)
        return self._plan

    # その時刻の最終マスク (「ボカさない範囲 (緑)」表示用)。PIL Image or None
    def mask(self, media_id, source_sec):
        painter = self._get_painter()
        if painter is None:
            return None
        try:
            return painter.paint(media_id, source_sec)
        except Exception:                           # noqa: BLE001 (表示で落とさない)
            _logger.debug("ぼかしのマスクを作れませんでした", exc_info=True)
            return None

    # 素材解像度のフレームへぼかしを当てて返す。
    #   frame_rgb     : RGB のバイト列 (高さ x 幅 x 3)
    #   width, height : frame_rgb の大きさ (素材ピクセル)
    #   media_id      : その素材の ID (マスクの座標変換に使う)
    #   source_sec    : 素材の時刻
    # ぼかす対象が無い・失敗した場合は **入力をそのまま返す** (同一オブジェクト)。
    def apply(self, frame_rgb, width, height, media_id, source_sec):
        if not frame_rgb or self._failed or not _PIL_AVAILABLE:
            return frame_rgb
        try:
            mask = self.mask(media_id, source_sec)
            if mask is None:
                return frame_rgb
            frame_mask = self._mask_for_frame(mask, media_id, width, height)
            if frame_mask is None:
                return frame_rgb
            box = frame_mask.getbbox()
            if box is None:
                return frame_rgb            # ぼかす所が無いフレーム (大半がこれ)
            image = Image.frombytes("RGB", (int(width), int(height)), bytes(frame_rgb))
            return self._blur_inside(image, frame_mask, box, width).tobytes()
        except Exception as error:                  # noqa: BLE001 (画面を落とさない / §5.9)
            self._failed = True
            _logger.warning("プレビューへぼかしを反映できません: %s (目印の表示へ戻します)", error)
            return frame_rgb

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_painter(self):
        if not _PIL_AVAILABLE or self._failed:
            return None
        if self._painter is None:
            from . import mask_builder              # noqa: PLC0415 (循環 import を避ける)

            width, height = mask_builder.mask_size(self._timeline, self._cfg)
            self._painter = mask_builder._MaskPainter(   # noqa: SLF001 (同じ層の道具)
                self.plan(), self._timeline, self._cfg, width, height)
        return self._painter

    # キャンバス座標のマスクを、素材解像度のフレームへ写す (ver5 resolve4 §5.6.2)
    #
    # マスクはキャンバス (timeline.width x height) を mask_scale で縮めたもの。
    # フレームは素材の解像度で、キャンバスへ載るときに「収まる方の倍率で縮小 + 中央寄せ」
    # (レターボックス) が掛かる。その逆をたどって、マスクから素材の映っている部分だけを
    # 切り出し、フレームの大きさへ広げる。
    def _mask_for_frame(self, mask, media_id, width, height):
        transform = self._transform(media_id)
        if transform is None:
            return None
        scale, offset_x, offset_y = transform
        mask_w, mask_h = mask.size
        ratio_x = mask_w / float(self._timeline.width or 1)
        ratio_y = mask_h / float(self._timeline.height or 1)
        left = offset_x * ratio_x
        top = offset_y * ratio_y
        right = mask_w - left
        bottom = mask_h - top
        if right - left < 1 or bottom - top < 1:
            return None
        cropped = mask.crop((int(round(left)), int(round(top)),
                             int(round(right)), int(round(bottom))))
        return cropped.resize((int(width), int(height)), Image.BILINEAR)

    def _transform(self, media_id):
        if media_id not in self._transforms:
            media = self._timeline.media_by_id(media_id)
            self._transforms[media_id] = (
                None if media is None
                else source_to_canvas_transform(media, self._timeline.width,
                                                self._timeline.height))
        return self._transforms[media_id]

    # マスクの白い所だけをぼかして貼り戻す。
    # box を 3σ 広げて切り出すのは、切り口でガウスが折り返して境界に筋が出るため。
    # 切り出しが大きいときは縮小してからぼかす (ver5 resolve7 §5.10)。
    def _blur_inside(self, image, mask, box, frame_width):
        # 強さはキャンバス幅を基準に決まっているため、フレームの解像度へ換算する
        sigma = blur_sigma(self._timeline.width, self._cfg) * (
            float(frame_width) / float(self._timeline.width or 1))
        sigma = max(sigma, 0.5)
        pad = int(sigma * _CROP_SIGMA) + 1
        crop = (max(box[0] - pad, 0), max(box[1] - pad, 0),
                min(box[2] + pad, image.width), min(box[3] + pad, image.height))
        if crop[2] - crop[0] < 2 or crop[3] - crop[1] < 2:
            return image

        part = image.crop(crop)
        if str(self._cfg["render"]["mode"]) == MODE_PIXELATE:
            # モザイク: blur_overlay.build_chains と同じ式 (粗く縮めて最近傍で戻す)
            block = max(int(sigma), 2)
            small = (max(part.width // block, 1), max(part.height // block, 1))
            effect = part.resize(small, Image.NEAREST).resize(part.size, Image.NEAREST)
        else:
            effect = self._gaussian(part, sigma)
        image.paste(Image.composite(effect, part, mask.crop(crop)), crop)
        return image

    # ガウスぼかし。大きい絵は縮小 → ぼかし → 拡大で済ませる (§5.10)
    def _gaussian(self, part, sigma):
        if part.width * part.height < _DOWNSCALE_MIN_PX:
            return part.filter(ImageFilter.GaussianBlur(sigma))
        scale = _DOWNSCALE_MAX
        small = (max(int(part.width * scale), 1), max(int(part.height * scale), 1))
        blurred = part.resize(small, Image.BILINEAR).filter(
            ImageFilter.GaussianBlur(max(sigma * scale, 0.5)))
        return blurred.resize(part.size, Image.BILINEAR)
