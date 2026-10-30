# テロップの見た目を決める値の優先順を 1 か所へまとめる (ver9 resolve13 §3.2)
#
# 文字色の出どころは 4 つある:
#   ① クリップ個別 (SubtitleClip.color)  … 利用者が 1 本だけ変えた色。最優先
#   ② トラックの既定 (Track.color)        … ヘッダーで決めた色
#   ③ 役割の色 (設定の役割別カラー)       … 従来の振る舞い
#   ④ 配信者色 (FontProfile.color_hex)    … 最後の砦
#
# ①③④ の判定は、これまで焼き込み・プレビュー・Resolve 出力の 3 か所へ
# 同じ式で書かれていた。② を足すにあたり、式をここへ寄せて 1 つにする。
# 同じ優先順を 3 回書いたままだと「プレビューだけ色が効く」ような食い違いが起きる。
#
# ① と ② の合成は SubtitleClip.to_item(track_color=…) が行う (item["color"] へ入る)。
# 焼き込みはその item からインライン上書きを作るため、下流は無改造で ② に対応する。


# トラックの既定色 (テロップトラック以外・未設定は空文字)
def track_color(track):
    if track is None:
        return ""
    return str(getattr(track, "color", "") or "")


# クリップ ID → そのトラックの既定色 の対応を作る (ver9 resolve13 §3.8)
#
# テロップ 1 本ずつ track_of_clip() を引くと全トラック走査を繰り返すことになるため、
# まとめて作る側を既定の口にする。色を決めていないトラックは載せない =
# 既定のままなら空の辞書になり、呼び出し側は従来と同じ値を使う。
def track_color_map(timeline):
    mapping = {}
    if timeline is None:
        return mapping
    for track in timeline.subtitle_tracks():
        color = track_color(track)
        if not color:
            continue
        for clip in track.clips:
            mapping[clip.id] = color
    return mapping


# クリップ 1 本が載っているトラックの既定色 (単発の問い合わせ向け)
def track_color_for(timeline, clip):
    if timeline is None or clip is None:
        return ""
    return track_color(timeline.track_of_clip(getattr(clip, "id", "")))


# 文字色を 4 段の優先順で決める (①→②→③→④)。本モジュールの中核。
#
# individual  : クリップ個別の色 (空文字 = 指定なし)
# from_track  : トラックの既定色 (空文字 = 指定なし)
# role        : 役割キー (streamer / sub / comment)
# role_colors : 役割キー → 色 の辞書 (FontProfile.role_colors)
# fallback    : どれも決まらないときの色 (FontProfile.color_hex)
def resolve_color(individual, from_track, role, role_colors, fallback=""):
    if individual:
        return individual
    if from_track:
        return from_track
    return (role_colors or {}).get(str(role or ""), fallback) or fallback


# テロップ 1 本の文字色を決める (クリップとトラックを渡す版)
def resolve_text_color(clip, track, role_colors, fallback=""):
    return resolve_color(
        str(getattr(clip, "color", "") or ""), track_color(track),
        getattr(clip, "role", ""), role_colors, fallback)


# item 形式 (to_item の戻り) から文字色を決める (③④ だけを担う)
#
# item["color"] には ① と ② が合成済みで入っているため、ここでは
# 「個別もトラックも無いときに役割色へ落ちる」ところだけを引き受ける。
def resolve_item_color(item, role_colors, fallback=""):
    item = item or {}
    return resolve_color(
        str(item.get("color", "") or ""), "", item.get("role", ""),
        role_colors, fallback)


__all__ = [
    "resolve_color",
    "resolve_item_color",
    "resolve_text_color",
    "track_color",
    "track_color_for",
    "track_color_map",
]
