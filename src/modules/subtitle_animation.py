# テロップアニメーション (docs/request/ver12/resolve2.md / resolve2-add.md)
#
# 震える / 発光登場 / つっこみ / 発光ブラー / 衝撃1 / 衝撃2 / ぴかっと / ポヨン /
# 飛び込みツッコミ / 2回ジャンプ / 黒ブラー拡大 / 黒ブラー縮小 の 12 種と
# Fade In / Fade Out を
# ASS の上書きタグへ落とす。
#
# 「いつ・どれだけ動かすか」の計算は**このモジュールだけが持つ**。焼き込み
# (subtitle_generator) とプレビュー (gui/timeline/preview_items) の両方がここを呼ぶため、
# 2 か所へ書くと必ずずれる (compositor / sound_mix と同じ方針)。
#
# 【設計の要点 (resolve2-add §3)】
#   ・動きはすべて**キーフレーム**(時刻と見た目の状態の組) で表す。
#     状態 = 位置のずれ・拡大率・回転・ぼかし・不透明度・白/黒への寄せ・縁の太り・
#     表示範囲 (左右の切り抜き)。キーとキーの間は直線で補間する。
#   ・ASS ではキーの区間ごとに 1 イベントを出し、位置は \move、それ以外は \t で
#     次のキーの値へ補間させる (libass も直線補間)。プレビューは同じキーを
#     state_at() で補間して描くため、焼き込みと同じ動きになる。
#   ・なめらかな減速 (イーズ) は直線区間を細かく刻んで近似する (\t の加速度指定は
#     プレビューとの一致を崩すため使わない)。
#   ・1 本のテロップは「層 (layer)」の重ねで表す。本文の層のほかに、衝撃1 の光の箱
#     (box)・衝撃2 の残像・ぴかっとの光の帯 (shine) を別の層として重ねる。
#   ・塊の左右の端 (ぴかっとの帯の範囲) はフォントの実測送り幅 (font_advance) で求める。
#
# subtitle_generator から import されるため、逆向きの import はしない
# (ASS 色文字列の組み立てだけ循環 import を避けるためここへ小さく持つ / comment_decor と同じ)。
import math

from ..utils.logger import get_logger
from . import font_advance

_logger = get_logger(__name__)

# アニメーションの種類 (timeline.model と同じ値。model を import しないのは
# modules 層が timeline 層へ依存しないため)。並びは画面のコンボの並び。
ANIM_SHAKE = "shake"                    # 震える
ANIM_GLOW_IN = "glow_in"                # 発光登場
ANIM_TSUKKOMI = "tsukkomi"              # つっこみ
ANIM_GLOW_BLUR = "glow_blur"            # 発光ブラー
ANIM_SHOCK1 = "shock1"                  # 衝撃1
ANIM_SHOCK2 = "shock2"                  # 衝撃2
ANIM_SHINE = "shine"                    # ぴかっと
ANIM_POYON = "poyon"                    # ポヨン
ANIM_DIVE_TSUKKOMI = "dive_tsukkomi"    # 飛び込みツッコミ
ANIM_DOUBLE_JUMP = "double_jump"        # 2回ジャンプ
ANIM_BLACK_BLUR_ZOOM_IN = "black_blur_zoom_in"    # 黒ブラー拡大
ANIM_BLACK_BLUR_ZOOM_OUT = "black_blur_zoom_out"  # 黒ブラー縮小
ANIMATIONS = (
    ANIM_SHAKE, ANIM_GLOW_IN, ANIM_TSUKKOMI, ANIM_GLOW_BLUR, ANIM_SHOCK1,
    ANIM_SHOCK2, ANIM_SHINE, ANIM_POYON, ANIM_DIVE_TSUKKOMI, ANIM_DOUBLE_JUMP,
    ANIM_BLACK_BLUR_ZOOM_IN, ANIM_BLACK_BLUR_ZOOM_OUT,
)

# 層の種類
LAYER_TEXT = "text"     # 本文 (残像・光の帯も本文の写し)
LAYER_BOX = "box"       # 本文の後ろの四角い光 (衝撃1)

# 設定が渡されなかったときの既定 (settings_window.DEFAULT_SETTINGS と同じ値)。
# 長さは ms、大きさの比は文字サイズ (px) に対する比、拡大率は %。
_DEFAULTS = {
    "enabled": True,
    # なめらかな減速を何区間の直線で近似するか (多いほど滑らか・イベントが増える)
    "ease_segments": 6,
    # 震える
    "shake_amplitude_ratio": 0.05,
    "shake_step_ms": 50,
    "shake_blur": 2.0,
    # 発光登場
    "glow_in_ms": 450,
    "glow_blur": 10.0,
    "glow_border": 6.0,
    # つっこみ
    "tsukkomi_ms": 150,
    "tsukkomi_scale": 135,
    "tsukkomi_rotate": 6.0,
    # 発光ブラー
    "glow_blur_ms": 450,
    "glow_blur_stretch": 170,
    # 衝撃1
    "shock1_ms": 600,
    "shock1_box_color": "#FFFFFF",
    "shock1_box_opacity": 0.75,
    "shock1_box_padding_ratio": 0.35,
    "shock1_box_blur": 8.0,
    # 衝撃2
    "shock2_ms": 600,
    "shock2_scale": 170,
    "shock2_opacity": 0.5,
    "shock2_blur": 3.0,
    # ぴかっと
    "shine_delay_ms": 100,
    "shine_ms": 400,
    "shine_width_ratio": 0.18,
    "shine_opacity": 0.9,
    "shine_blur": 3.0,
    # ポヨン
    "poyon_ms": 400,
    "poyon_start_scale": 60,
    "poyon_overshoot": 20,
    # 飛び込みツッコミ
    "dive_ms": 150,
    "dive_scale": 170,
    "dive_wobble_ms": 300,
    "dive_rotate": 4.0,
    # 2回ジャンプ
    "jump_ms": 600,
    "jump_height_ratio": 0.35,
    "jump_second_ratio": 0.6,
    # 黒ブラー拡大 / 縮小
    "black_blur_ms": 500,
    "black_blur": 10.0,
    "black_blur_zoom_in_scale": 60,
    "black_blur_zoom_out_scale": 150,
    # 表示範囲の端に足す余白 (文字サイズに対する比 / 縁・影・ぼかしを切らないため)
    "clip_margin_ratio": 0.3,
    # Fade (種類と併用できる)
    "fade_in_ms": 200,
    "fade_out_ms": 200,
    # 保護: 1 本のテロップで出す Dialogue 行の上限。超えそうな連続した動き
    # (震える) は刻みを粗くして収める (resolve2-add §6)。
    "max_events_per_clip": 120,
}

# ASS の行送り。libass は Fontsize が ascent+descent に一致するよう字を縮尺するため、
# 行は Fontsize ぶんだけ進む (gui/timeline/font_metrics.line_advance_px と同じ規約)。
# libass で実測: 2 行の \an2\pos(x,300) と \an5\pos(x,252) (Fontsize 48) が同じ位置。
_LINE_ADVANCE_RATIO = 1.0

# これ未満の区間はイベントにしない (ASS の分解能は 10ms)
_MIN_EVENT_SEC = 0.02

# 何も動かしていない状態 (キーフレームの既定値)
#   dx, dy   : 位置のずれ (px / 下が正)
#   sx, sy   : 拡大率 (%)
#   rot      : 回転 (度 / 反時計回りが正 = ASS の \frz)
#   blur     : ぼかし (px / \blur)
#   opacity  : 不透明度 (0.0〜1.0)
#   white    : 塗りと縁を白へ寄せる量 (0.0〜1.0 / 発光)
#   dark     : 塗りと縁を黒へ寄せる量 (0.0〜1.0 / 黒ブラー)
#   bord     : 縁を太らせる量 (px / 発光の広がり)
#   clip_l, clip_r : 文字の塊の幅に対する表示範囲 (0.0〜1.0 / 左端から)
NEUTRAL = {
    "dx": 0.0, "dy": 0.0, "sx": 100.0, "sy": 100.0, "rot": 0.0, "blur": 0.0,
    "opacity": 1.0, "white": 0.0, "dark": 0.0, "bord": 0.0,
    "clip_l": 0.0, "clip_r": 1.0,
}
_PROPS = tuple(NEUTRAL.keys())

# 1 度だけ出す警告の記録
_warned = set()


def _warn_once(key, message, *args):
    if key in _warned:
        return
    _warned.add(key)
    _logger.warning(message, *args)


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------

# subtitle.animation を既定で補完して返す
def config(subtitle_cfg):
    values = ((subtitle_cfg or {}).get("animation") or {})
    return {**_DEFAULTS, **values}


# 機能そのものが有効か (false なら呼び出し側は従来どおり 1 行書く)
def is_enabled(cfg):
    return bool((cfg or {}).get("enabled", True))


# そのエントリの動きの種類 ("" = 動かさない / 廃止した種類も "")
def animation_of(entry):
    value = str((entry or {}).get("animation", "") or "").strip().lower()
    return value if value in ANIMATIONS else ""


# 動きかフェードのどれかが付いているか
def has_animation(entry):
    entry = entry or {}
    return bool(animation_of(entry) or entry.get("fade_in") or entry.get("fade_out"))


def _ms(cfg, key):
    return max(_to_float((cfg or {}).get(key), _DEFAULTS[key]), 0.0) / 1000.0


def _num(cfg, key):
    return _to_float((cfg or {}).get(key), _DEFAULTS[key])


# ------------------------------------------------------------------
# フェード (種類と併用できる)
# ------------------------------------------------------------------

# フェードの長さ (ms) を (イン, アウト) で返す
#
# 表示時間より長いフェードは入れない (出た瞬間に消えるため)。
# ASS タグ (fade_tag / _event_fade_tag) とプレビューの不透明度 (fade_alpha) が
# 同じ値を見るためにここへ 1 つだけ置く。
def fade_ms(entry, cfg):
    entry = entry or {}
    cfg = config(None) if cfg is None else cfg
    fade_in = _to_int(cfg.get("fade_in_ms"), 200) if entry.get("fade_in") else 0
    fade_out = _to_int(cfg.get("fade_out_ms"), 200) if entry.get("fade_out") else 0
    fade_in = max(fade_in, 0)
    fade_out = max(fade_out, 0)
    if fade_in <= 0 and fade_out <= 0:
        return 0, 0
    duration_ms = max(
        int((_to_float(entry.get("end"), 0.0) - _to_float(entry.get("start"), 0.0))
            * 1000), 0)
    if duration_ms > 0 and fade_in + fade_out > duration_ms:
        scale = duration_ms / float(fade_in + fade_out)
        fade_in = int(fade_in * scale)
        fade_out = int(fade_out * scale)
    return fade_in, fade_out


# \fad(in,out) を返す (どちらも無ければ空文字)
#
# 動きを付けていない 1 行 (とコメントの背景) に使う。
def fade_tag(entry, cfg):
    fade_in, fade_out = fade_ms(entry, cfg)
    if fade_in <= 0 and fade_out <= 0:
        return ""
    return f"\\fad({fade_in},{fade_out})"


# 動きの区間ごとのイベントへ付けるフェード (\fade の 7 引数形)
#
# 動きはキーの区間ごとに別イベントへ分かれるため、\fad(in,out) を各イベントへ
# 付けると区間ごとに点滅する。\fade の時刻はイベント開始からの相対 ms で、
# 負の値も効く (libass で実測 / resolve2-add §2)。テロップ開始からのずれ
# (offset_ms) を引いて、テロップ全体で 1 回だけフェードするようにする。
def _event_fade_tag(fade_in, fade_out, duration_ms, offset_ms):
    if fade_in <= 0 and fade_out <= 0:
        return ""
    a1 = 255 if fade_in > 0 else 0
    a3 = 255 if fade_out > 0 else 0
    t1 = -offset_ms
    t2 = fade_in - offset_ms
    t3 = duration_ms - fade_out - offset_ms
    t4 = duration_ms - offset_ms
    return f"\\fade({a1},0,{a3},{t1},{t2},{t3},{t4})"


# その時刻のテロップの不透明度 (0.0〜1.0 / プレビュー用)
#   t_rel: テロップ開始からの相対秒
# \fad / \fade と同じ直線補間で求める。
def fade_alpha(entry, cfg, t_rel):
    fade_in, fade_out = fade_ms(entry, cfg)
    if fade_in <= 0 and fade_out <= 0:
        return 1.0
    t_ms = max(_to_float(t_rel, 0.0), 0.0) * 1000.0
    duration_ms = max(
        (_to_float((entry or {}).get("end"), 0.0)
         - _to_float((entry or {}).get("start"), 0.0)) * 1000.0, 0.0)
    alpha = 1.0
    if fade_in > 0 and t_ms < fade_in:
        alpha = t_ms / float(fade_in)
    if fade_out > 0 and duration_ms > 0:
        remain = duration_ms - t_ms
        if remain < fade_out:
            alpha = min(alpha, max(remain, 0.0) / float(fade_out))
    return min(max(alpha, 0.0), 1.0)


# ------------------------------------------------------------------
# キーフレーム
# ------------------------------------------------------------------

# 既定値へ差分を重ねた状態を返す
def _state(**values):
    state = dict(NEUTRAL)
    for key, value in values.items():
        state[key] = float(value)
    return state


# 3 次の減速 (はじめ速く、終わりにゆっくり止まる)
def _ease_out(u):
    u = min(max(u, 0.0), 1.0)
    return 1.0 - (1.0 - u) ** 3


# 時刻の関数 fn(t) をキーフレームへ刻む (t = 0 〜 length を segments 区間)
def _sample(fn, length, segments, start=0.0):
    segments = max(int(segments), 1)
    if length <= 0:
        return [(start, fn(0.0))]
    return [(start + length * i / segments, fn(length * i / segments))
            for i in range(segments + 1)]


# 2 つの状態を u (0〜1) で直線補間する
def _lerp_state(a, b, u):
    return {key: a[key] + (b[key] - a[key]) * u for key in _PROPS}


# キー列の時刻 t における状態 (キーの外は端の状態を保つ)
def state_at(keys, t):
    if not keys:
        return dict(NEUTRAL)
    if t <= keys[0][0]:
        return dict(keys[0][1])
    for index in range(len(keys) - 1):
        t0, s0 = keys[index]
        t1, s1 = keys[index + 1]
        if t <= t1:
            if t1 - t0 <= 1e-9:
                return dict(s1)
            return _lerp_state(s0, s1, (t - t0) / (t1 - t0))
    return dict(keys[-1][1])


# キー列をテロップの尺で切る (尺の外のキーは捨て、尺の端の状態を補う)
def _cut_keys(keys, duration):
    out = [(t, s) for t, s in keys if t < duration - 1e-9]
    if keys and keys[-1][0] > duration - 1e-9 and (not out or out[-1][0] < duration):
        out.append((duration, state_at(keys, duration)))
    return out


# 層を 1 つ作る
#   keys    : [(テロップ開始からの相対秒, 状態)]
#   hold    : 最後のキーのあと、テロップの終わりまで残すか (光や残像は消える)
#   kind    : LAYER_TEXT / LAYER_BOX
def _layer(keys, hold=True, kind=LAYER_TEXT):
    return {"kind": kind, "keys": keys, "hold": bool(hold)}


# ------------------------------------------------------------------
# 種類ごとの動き (resolve2-add §4)
# ------------------------------------------------------------------

# テロップ 1 本の動きを層の列で返す
#
#   size      : 文字サイズ (ASS Fontsize = px)
#   duration  : 表示秒
#   video_width: 画面幅 (px)
# 戻り値: {"layers": [...]} (層は下から順)。種類が無ければ None。
def spec(kind, cfg, size, duration, video_width):
    cfg = config(None) if cfg is None else cfg
    builder = _BUILDERS.get(kind)
    if builder is None or duration <= _MIN_EVENT_SEC:
        return None
    size = max(_to_float(size, 48.0), 1.0)
    layers = builder(cfg, size, float(duration), float(video_width))
    # 尺を超えるキーを切る (短いテロップでは動きの途中で終わる)
    for layer in layers:
        layer["keys"] = _cut_keys(layer["keys"], duration)
    return {"kind": kind, "layers": layers}


# 本文がずっと静止している層 (光や残像を重ねる種類の土台)
def _static_text():
    return _layer([(0.0, _state())])


# 1 種類 = 1 関数。戻り値は層の列 (下から)。
def _build_shake(cfg, size, duration, video_width):
    amplitude = _num(cfg, "shake_amplitude_ratio") * size
    blur = _num(cfg, "shake_blur")
    step = max(_ms(cfg, "shake_step_ms"), 0.02)
    # 刻みが細かすぎてイベントの上限を超えるなら刻みを粗くする (resolve2-add §6)
    budget = max(_to_int(cfg.get("max_events_per_clip"), 120), 4)
    step = max(step, duration / budget)
    keys = []
    count = int(duration / step) + 1
    for index in range(count + 1):
        t = min(index * step, duration)
        # 決まった擬似乱数 (同じテロップは毎回同じ揺れ = プレビューと出力が一致)
        dx = amplitude * math.sin(index * 12.9898 + 1.3) * (1 if index % 2 else -1)
        dy = amplitude * math.sin(index * 78.233 + 0.7)
        keys.append((t, _state(dx=dx, dy=dy, blur=blur if index % 3 == 1 else 0.0)))
        if t >= duration:
            break
    return [_layer(keys)]


def _glow_state(cfg, amount, **extra):
    return _state(white=amount, blur=_num(cfg, "glow_blur") * amount,
                  bord=_num(cfg, "glow_border") * amount, **extra)


def _build_glow_in(cfg, size, duration, video_width):
    length = _ms(cfg, "glow_in_ms")
    keys = _sample(lambda t: _glow_state(cfg, 1.0 - _ease_out(t / length) if length else 0.0),
                   length, _num(cfg, "ease_segments"))
    return [_layer(keys)]


def _build_tsukkomi(cfg, size, duration, video_width):
    length = _ms(cfg, "tsukkomi_ms")
    scale = _num(cfg, "tsukkomi_scale")
    rotate = _num(cfg, "tsukkomi_rotate")
    keys = [(0.0, _state(sx=scale, sy=scale, rot=rotate)),
            (length * 0.6, _state(sx=100 - (scale - 100) * 0.08,
                                  sy=100 - (scale - 100) * 0.08, rot=-rotate * 0.3)),
            (length, _state())]
    return [_layer(keys)]


def _build_glow_blur(cfg, size, duration, video_width):
    length = _ms(cfg, "glow_blur_ms")
    stretch = _num(cfg, "glow_blur_stretch")
    # 横長の光のぼかしから字が出てくる (ASS のぼかしは縦横同じため横へ伸ばして近似)
    def fn(t):
        rest = 1.0 - _ease_out(t / length) if length else 0.0
        return _glow_state(cfg, rest, sx=100 + (stretch - 100) * rest)
    return [_layer(_sample(fn, length, _num(cfg, "ease_segments")))]


def _build_shock1(cfg, size, duration, video_width):
    length = _ms(cfg, "shock1_ms")
    opacity = min(max(_num(cfg, "shock1_box_opacity"), 0.0), 1.0)
    blur = _num(cfg, "shock1_box_blur")
    # 本文の後ろに四角い光が出て、広がりながら消える
    box = _layer([(0.0, _state(opacity=opacity, blur=blur)),
                  (length, _state(opacity=0.0, blur=blur, sx=115, sy=115))],
                 hold=False, kind=LAYER_BOX)
    return [box, _static_text()]


def _build_shock2(cfg, size, duration, video_width):
    length = _ms(cfg, "shock2_ms")
    scale = _num(cfg, "shock2_scale")
    opacity = min(max(_num(cfg, "shock2_opacity"), 0.0), 1.0)
    blur = _num(cfg, "shock2_blur")
    # 大きな半透明の残像が縮みながら消える (本文の上に重ねる)
    def fn(t):
        u = _ease_out(t / length) if length else 1.0
        size_now = scale + (100 - scale) * u
        return _state(sx=size_now, sy=size_now, opacity=opacity * (1.0 - u), blur=blur)
    ghost = _layer(_sample(fn, length, _num(cfg, "ease_segments")), hold=False)
    return [_static_text(), ghost]


def _build_shine(cfg, size, duration, video_width):
    delay = _ms(cfg, "shine_delay_ms")
    length = _ms(cfg, "shine_ms")
    width = max(_num(cfg, "shine_width_ratio"), 0.01)
    opacity = min(max(_num(cfg, "shine_opacity"), 0.0), 1.0)
    blur = _num(cfg, "shine_blur")
    # 白い本文の写しを細い帯で切り抜き、帯を左から右へ流す
    def band(u):
        center = -width + (1.0 + 2.0 * width) * u
        return _state(white=1.0, opacity=opacity, blur=blur,
                      clip_l=center - width / 2.0, clip_r=center + width / 2.0)
    keys = [(delay, band(0.0)), (delay + length, band(1.0))]
    return [_static_text(), _layer(keys, hold=False)]


def _build_poyon(cfg, size, duration, video_width):
    length = _ms(cfg, "poyon_ms")
    start = _num(cfg, "poyon_start_scale")
    over = _num(cfg, "poyon_overshoot")
    # 小さく出て、大きく弾み、行き過ぎを小さくしながら収まる
    values = [start, 100 + over, 100 - over * 0.4, 100 + over * 0.15, 100.0]
    keys = [(length * i / (len(values) - 1), _state(sx=v, sy=v))
            for i, v in enumerate(values)]
    return [_layer(keys)]


def _build_dive_tsukkomi(cfg, size, duration, video_width):
    dive = _ms(cfg, "dive_ms")
    wobble = _ms(cfg, "dive_wobble_ms")
    scale = _num(cfg, "dive_scale")
    rotate = _num(cfg, "dive_rotate")
    # 大きく飛び込んで縮み、首を振るように揺れて止まる
    keys = [(0.0, _state(sx=scale, sy=scale, opacity=0.0)),
            (dive * 0.3, _state(sx=scale - (scale - 100) * 0.3,
                                sy=scale - (scale - 100) * 0.3)),
            (dive, _state(sx=96, sy=96))]
    swings = [rotate, -rotate * 0.6, rotate * 0.3, 0.0]
    for index, angle in enumerate(swings):
        keys.append((dive + wobble * (index + 1) / len(swings),
                     _state(rot=angle, sx=100, sy=100)))
    return [_layer(keys)]


def _build_double_jump(cfg, size, duration, video_width):
    length = _ms(cfg, "jump_ms")
    height = _num(cfg, "jump_height_ratio") * size
    second = _num(cfg, "jump_second_ratio")
    half = length / 2.0

    # 放物線 2 つ (2 回目は低い)
    def fn(t):
        if t < half:
            u, peak = t / half if half else 1.0, height
        else:
            u, peak = (t - half) / half if half else 1.0, height * second
        return _state(dy=-peak * 4.0 * u * (1.0 - u))
    segments = max(int(_num(cfg, "ease_segments")), 2) * 2
    return [_layer(_sample(fn, length, segments))]


def _black_blur(cfg, start_scale):
    length = _ms(cfg, "black_blur_ms")
    blur = _num(cfg, "black_blur")

    # 黒くぼけた字が大きさを変えながら色とピントを取り戻す
    def fn(t):
        rest = 1.0 - _ease_out(t / length) if length else 0.0
        scale = 100 + (start_scale - 100) * rest
        return _state(dark=rest, blur=blur * rest, sx=scale, sy=scale,
                      opacity=1.0 - 0.4 * rest)
    return [_layer(_sample(fn, length, _num(cfg, "ease_segments")))]


def _build_black_blur_zoom_in(cfg, size, duration, video_width):
    return _black_blur(cfg, _num(cfg, "black_blur_zoom_in_scale"))


def _build_black_blur_zoom_out(cfg, size, duration, video_width):
    return _black_blur(cfg, _num(cfg, "black_blur_zoom_out_scale"))


_BUILDERS = {
    ANIM_SHAKE: _build_shake,
    ANIM_GLOW_IN: _build_glow_in,
    ANIM_TSUKKOMI: _build_tsukkomi,
    ANIM_GLOW_BLUR: _build_glow_blur,
    ANIM_SHOCK1: _build_shock1,
    ANIM_SHOCK2: _build_shock2,
    ANIM_SHINE: _build_shine,
    ANIM_POYON: _build_poyon,
    ANIM_DIVE_TSUKKOMI: _build_dive_tsukkomi,
    ANIM_DOUBLE_JUMP: _build_double_jump,
    ANIM_BLACK_BLUR_ZOOM_IN: _build_black_blur_zoom_in,
    ANIM_BLACK_BLUR_ZOOM_OUT: _build_black_blur_zoom_out,
}


# 衝撃1 の光の箱の余白 (文字サイズに対する比) と色
def box_style(cfg):
    cfg = config(None) if cfg is None else cfg
    return (str(cfg.get("shock1_box_color") or "#FFFFFF"),
            max(_num(cfg, "shock1_box_padding_ratio"), 0.0))


# ------------------------------------------------------------------
# 位置 (焼き込み用)
# ------------------------------------------------------------------

# 本文の行 (ASS の \N と実改行の両方を区切りとして扱う)
def _lines_of(entry):
    body = str((entry or {}).get("text", "") or "")
    body = body.replace("\\N", "\n").replace("\\n", "\n")
    return body.split("\n")


def _font_size(entry, eff_cfg, font_profile):
    size = _to_int((entry or {}).get("font_size"), 0)
    if size > 0:
        return size
    size = _to_int((eff_cfg or {}).get("font_size"), 0)
    if size > 0:
        return size
    return _to_int(getattr(font_profile, "size", 48), 48) or 48


def _metrics(entry, eff_cfg, font_profile):
    family = (str((entry or {}).get("font", "") or "")
              or str((eff_cfg or {}).get("font_family", "") or "")
              or str(getattr(font_profile, "family", "") or ""))
    if not family:
        return None
    bold = bool((eff_cfg or {}).get("bold", getattr(font_profile, "bold", False)))
    return font_advance.metrics_for(family, eff_cfg, bold=bold)


# 動かすときの基準点を「文字の塊の上下中央」へ移す
#
# 拡大・回転は基準点を中心に掛かるため、下段基準 (\an1〜3) のままだと字が下端から
# 伸びる。上下だけ中央 (\an4〜6) へ移し、y を塊の高さの半分だけずらす
# (行数 x Fontsize = 塊の高さ / libass で実測)。左右の寄せは変えない
# (複数行の行ごとの寄せを保つため)。
# 戻り値 {"an", "x", "y", "column": "left"|"center"|"right", "height"}
def _center_anchor(anchor, line_count, size):
    an = _to_int(anchor.get("an"), 2)
    x = _to_float(anchor.get("x"), 0.0)
    y = _to_float(anchor.get("y"), 0.0)
    height = size * _LINE_ADVANCE_RATIO * max(line_count, 1)
    if an <= 3:
        y -= height / 2.0
    elif an >= 7:
        y += height / 2.0
    if an in (1, 4, 7):
        column, new_an = "left", 4
    elif an in (3, 6, 9):
        column, new_an = "right", 6
    else:
        column, new_an = "center", 5
    return {"an": new_an, "x": x, "y": y, "column": column, "height": height}


# 文字の塊の左右の端 (px)。測れなければ None。
def _block_span(center, widths):
    if not widths:
        return None
    width = max(widths)
    if center["column"] == "left":
        left = center["x"]
    elif center["column"] == "right":
        left = center["x"] - width
    else:
        left = center["x"] - width / 2.0
    return left, left + width


# ------------------------------------------------------------------
# ASS イベントの組み立て
# ------------------------------------------------------------------

# アニメ付きエントリを Dialogue の列へ展開する
#
# 戻り値: [{"start": 絶対秒, "end": 絶対秒, "text": ASS 本文}]
#   空リストなら呼び出し側は従来どおり 1 行だけ書く (フェードのみ・機能 OFF・
#   位置の基準が無い場合)。
#
# anchor: {"an": int, "x": px, "y": px} = テロップの基準点。
#   呼び出し側 (subtitle_generator) が既存の計算 (position_anchor) で渡す。
# base  : 色と縁の基準 (subtitle_generator.animation_base が組む)
#   {"fill": "RRGGBB", "outline": "BBGGRR", "a1": 0〜255, "a3": 0〜255,
#    "a4": 0〜255, "bord": px}
def events(entry, cfg, eff_cfg, font_profile, video_width, video_height,
           anchor=None, base=None):
    cfg = config(None) if cfg is None else cfg
    if not is_enabled(cfg):
        return []
    kind = animation_of(entry)
    if not kind or anchor is None:
        if kind and anchor is None:
            _warn_once("no_anchor",
                       "テロップの基準点が決まらないため動きを付けられません "
                       "(subtitle.explicit_position を true にすると解消します)")
        return []
    start = _to_float(entry.get("start"), 0.0)
    end = _to_float(entry.get("end"), 0.0)
    duration = end - start
    if duration <= _MIN_EVENT_SEC:
        return []

    lines = _lines_of(entry)
    if not "".join(lines):
        return []
    size = _font_size(entry, eff_cfg, font_profile)
    center = _center_anchor(anchor, len(lines), size)
    metrics = _metrics(entry, eff_cfg, font_profile)
    widths = ([metrics.text_width(line, size) for line in lines]
              if metrics is not None else [])
    span = _block_span(center, widths)

    plan = spec(kind, cfg, size, duration, video_width)
    if plan is None:
        return []
    fade_in, fade_out = fade_ms(entry, cfg)
    context = {
        "start": start, "duration": duration, "center": center, "span": span,
        "size": size, "video_width": video_width, "video_height": video_height,
        "base": base or {}, "fade": (fade_in, fade_out),
        "text": _escape("\\N".join(lines)), "cfg": cfg, "lines": lines,
        "widths": widths,
    }
    out = []
    for layer in plan["layers"]:
        if layer["kind"] == LAYER_BOX:
            out.extend(_box_events(layer, context))
        else:
            out.extend(_layer_events(layer["keys"], layer["hold"], context,
                                     context["text"]))
    return out


# 層の中で一度でも既定値から外れる項目 (その項目だけタグを書く)
def _used_props(keys):
    used = set()
    for _t, state in keys:
        for key in _PROPS:
            if abs(state[key] - NEUTRAL[key]) > 1e-6:
                used.add(key)
    return used


# 1 つの層をキーの区間ごとのイベントへ展開する
#   point: 基準点 (省略時はテロップの基準点) / an: テンキー配置
def _layer_events(keys, hold, context, text, point=None, an=None):
    if not keys:
        return []
    center = context["center"]
    px, py = point if point is not None else (center["x"], center["y"])
    an = an if an is not None else center["an"]
    used = _used_props(keys)
    out = []
    for index in range(len(keys) - 1):
        t0, s0 = keys[index]
        t1, s1 = keys[index + 1]
        if t1 - t0 < _MIN_EVENT_SEC:
            continue
        out.append(_event(context, t0, t1, an, px, py, s0, s1, used, text))
    t_last, s_last = keys[-1]
    if hold and context["duration"] - t_last >= _MIN_EVENT_SEC:
        out.append(_event(context, t_last, context["duration"], an, px, py,
                          s_last, s_last, used, text))
    return out


# 区間 1 本ぶんのイベント (s0 で始まり s1 へ補間する)
def _event(context, t0, t1, an, px, py, s0, s1, used, text):
    duration_ms = int(round((t1 - t0) * 1000))
    tags = [f"\\an{an}"]
    x0, y0 = px + s0["dx"], py + s0["dy"]
    x1, y1 = px + s1["dx"], py + s1["dy"]
    if abs(x0 - x1) > 0.05 or abs(y0 - y1) > 0.05:
        tags.append(f"\\move({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f},0,{duration_ms})")
    else:
        tags.append(f"\\pos({x0:.1f},{y0:.1f})")
    tags.append(_static_tags(context, s0, used))
    moving = _static_tags(context, s1, used, changed_from=s0)
    if moving:
        tags.append(f"\\t(0,{duration_ms},{moving})")
    tags.append(_event_fade_tag(
        context["fade"][0], context["fade"][1],
        int(round(context["duration"] * 1000)), int(round(t0 * 1000))))
    return {"start": context["start"] + t0, "end": context["start"] + t1,
            "text": "{" + "".join(tags) + "}" + text}


# 状態をタグへ (used に入っている項目だけ / changed_from があれば変わった項目だけ)
def _static_tags(context, state, used, changed_from=None):
    def changed(*keys):
        if changed_from is None:
            return True
        return any(abs(state[k] - changed_from[k]) > 1e-6 for k in keys)

    base = context["base"]
    out = []
    if ("sx" in used or "sy" in used) and changed("sx", "sy"):
        out.append(f"\\fscx{state['sx']:.1f}\\fscy{state['sy']:.1f}")
    if "rot" in used and changed("rot"):
        out.append(f"\\frz{state['rot']:.2f}")
    if "blur" in used and changed("blur"):
        out.append(f"\\blur{max(state['blur'], 0.0):.2f}")
    if "bord" in used and changed("bord"):
        out.append(f"\\bord{max(_to_float(base.get('bord'), 0.0) + state['bord'], 0.0):.2f}")
    if ("white" in used or "dark" in used) and changed("white", "dark"):
        fill = _mix_rgb(base.get("fill", "FFFFFF"), state)
        outline = _mix_bgr(base.get("outline", "000000"), state)
        out.append(f"\\1c&H{_rgb_to_bgr(fill)}&\\3c&H{outline}&")
    if "opacity" in used and changed("opacity"):
        out.append(_alpha_tags(base, state["opacity"]))
    if ("clip_l" in used or "clip_r" in used) and changed("clip_l", "clip_r"):
        out.append(_clip_tag(context, state))
    return "".join(out)


# 不透明度を塗り・縁・影のアルファへ (元のアルファを保ったまま薄くする)
def _alpha_tags(base, opacity):
    opacity = min(max(opacity, 0.0), 1.0)
    out = []
    for name in ("1a", "3a", "4a"):
        original = min(max(_to_int(base.get(name), 0), 0), 255)
        value = int(round(255 - (255 - original) * opacity))
        out.append(f"\\{name}&H{value:02X}&")
    return "".join(out)


# 表示範囲 (左右の切り抜き)。塊の端を測れなければ画面幅で代用する。
def _clip_tag(context, state):
    span = context["span"] or (0.0, float(context["video_width"]))
    x1, x2 = clip_range(span[0], span[1], context["size"], context["cfg"], state)
    return f"\\clip({x1:.1f},0,{x2:.1f},{float(context['video_height']):.1f})"


# 表示範囲 (0〜1) を px へ直す (焼き込みとプレビューで共有)
#
# 縁・影・ぼかしは字の外へはみ出すため、0 と 1 を塊の端より余白ぶん外へ置く。
# 端だけ画面の端へ飛ばすと、区間の補間で光の帯の幅が変わってしまう (直線のまま保つ)。
def clip_range(left, right, size, cfg, state):
    margin = max(_num(cfg, "clip_margin_ratio"), 0.0) * max(_to_float(size, 0.0), 0.0)
    lo = left - margin
    width = (right + margin) - lo
    x1 = lo + width * state["clip_l"]
    x2 = lo + width * state["clip_r"]
    return x1, max(x1, x2)


# 衝撃1 の光の箱 (本文の塊より一回り大きい四角)
def _box_events(layer, context):
    keys = layer["keys"]
    if not keys:
        return []
    color, padding_ratio = box_style(context["cfg"])
    padding = padding_ratio * context["size"]
    span = context["span"]
    if span is None:
        # 送り幅を測れないときは「字数 x 文字サイズ」で幅を見積もる
        longest = max((len(line) for line in context["lines"]), default=1)
        width = longest * context["size"]
    else:
        width = span[1] - span[0]
    height = context["center"]["height"]
    w = width + padding * 2.0
    h = height + padding * 2.0
    center = context["center"]
    if span is not None:
        cx = (span[0] + span[1]) / 2.0
    elif center["column"] == "left":
        cx = center["x"] + width / 2.0
    elif center["column"] == "right":
        cx = center["x"] - width / 2.0
    else:
        cx = center["x"]
    drawing = f"m 0 0 l {w:.1f} 0 {w:.1f} {h:.1f} 0 {h:.1f}"
    ass_color = _rgb_to_bgr(_normalize_rgb(color))
    box_context = dict(context, base={"a1": 0, "a3": 0, "a4": 0, "bord": 0})
    out = []
    for event in _layer_events(keys, layer["hold"], box_context,
                               "{\\p1}" + drawing, point=(cx, center["y"]), an=5):
        # 箱は縁・影を持たない単色の図形
        event["text"] = _insert_tags(
            event["text"], f"\\bord0\\shad0\\1c&H{ass_color}&")
        out.append(event)
    return out


# 先頭の上書きブロックの末尾へタグを足す
def _insert_tags(text, tags):
    if text.startswith("{"):
        close = text.index("}")
        return text[:close] + tags + text[close:]
    return "{" + tags + "}" + text


# ------------------------------------------------------------------
# 色
# ------------------------------------------------------------------

def _normalize_rgb(value):
    text = str(value or "").strip().lstrip("#").upper()
    if len(text) != 6 or any(c not in "0123456789ABCDEF" for c in text):
        return "FFFFFF"
    return text


def _rgb_to_bgr(rgb):
    rgb = _normalize_rgb(rgb)
    return rgb[4:6] + rgb[2:4] + rgb[0:2]


# 白・黒へ寄せた色 (RRGGBB)
def _mix_rgb(rgb, state):
    rgb = _normalize_rgb(rgb)
    channels = [int(rgb[i:i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{_mix_channel(c, state):02X}" for c in channels)


def _mix_bgr(bgr, state):
    # 並びが違うだけで計算は同じ
    return _mix_rgb(bgr, state)


def _mix_channel(value, state):
    value = value + (255 - value) * min(max(state.get("white", 0.0), 0.0), 1.0)
    value = value * (1.0 - min(max(state.get("dark", 0.0), 0.0), 1.0))
    return int(round(min(max(value, 0.0), 255.0)))


# その状態の色 (#RRGGBB) をプレビュー用に返す
def mix_color(rgb, state):
    return "#" + _mix_rgb(rgb, state)


# ------------------------------------------------------------------
# 小物
# ------------------------------------------------------------------

# ASS の本文として安全な形にする (上書きタグの括弧だけ逃がす)
def _escape(text):
    return str(text or "").replace("{", "\\{").replace("}", "\\}")


# コメント役割か (背景の箱・アイコンが動きに追従できないためフェードのみ)
def is_comment_role(entry):
    return str((entry or {}).get("role", "") or "") == "comment"
