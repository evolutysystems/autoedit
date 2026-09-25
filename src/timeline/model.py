# Timeline データモデル (docs/request/ver3/resolve.md §6.1.3 / §6.2)
# 編集情報の唯一のソース (§4-1)。Qt に依存しない純 Python として実装し、
# GUI / CLI / テストのいずれからも同じモデルを扱えるようにする (§4-2)。
#
# 時刻はすべて秒 (float) で保持する (§3-3)。フレーム量子化はレンダリング直前にのみ行い、
# 誤差を累積させないため各時刻の絶対値に対して独立に丸める。

# ── トラック種別 (§6.3.1)
TRACK_VIDEO = "video"
TRACK_AUDIO = "audio"
TRACK_SUBTITLE = "subtitle"

# ── メディア種別
MEDIA_VIDEO = "video"
MEDIA_IMAGE = "image"

# ── クリップの由来 (§6.2.3)。記録専用で、編集やレンダリングの分岐には使わない。
ORIGIN_SILENCE_CUT = "silence_cut"
ORIGIN_OPENING = "opening"
ORIGIN_ENDING = "ending"
ORIGIN_ASR = "asr"
ORIGIN_USER_MEDIA = "user_media"
# Timeline の右クリックメニューから手で足した字幕 (認識由来と区別するための記録)
ORIGIN_USER_SUBTITLE = "user_subtitle"

# ── 字幕の役割 (既存 subtitle_generator と同じキー)
DEFAULT_ROLE = "streamer"
# コメント役割。専用の字幕トラックへ置き分ける対象 (ver3 resolve11 §3 D-1)
ROLE_COMMENT = "comment"

# ベースクリップ (V1 の本編・OP・ED) の描画順。オーバーレイは 1 以上を使う (§6.5-2)。
BASE_Z_ORDER = 0
# レイヤー振り直しの刻み幅。間に挿入する将来操作で全体再計算を避けるため 10 刻みにする。
Z_ORDER_STEP = 10
# 字幕クリップの既定 z_order (画像より前面に出したい既定運用に合わせる)
DEFAULT_SUBTITLE_Z_ORDER = 100

# ベーストラック / 既定トラックの ID
BASE_VIDEO_TRACK_ID = "V1"
BASE_AUDIO_TRACK_ID = "A1"
BASE_SUBTITLE_TRACK_ID = "S1"
# コメント専用の字幕トラック ID (ver3 resolve11 §5.1)。設定で変更できる。
COMMENT_SUBTITLE_TRACK_ID = "S2"


# 値を float へ寄せる (不正値は既定へフォールバック)
def _to_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# 値を float か None へ寄せる (None を「未指定」として保持したい項目用)
def _to_float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# 値を int へ寄せる (不正値は既定へフォールバック)
def _to_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


# 値を int か None へ寄せる (字幕の個別フォントサイズ用。None = デフォルト使用)
def _to_int_or_none(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# 秒値を JSON 書き出し用にミリ秒精度へ丸める (§6.2.3)
def round_sec(value):
    return round(_to_float(value), 3)


# 変形パラメータ (プレビューの表示と overlay フィルタが共有する / §6.2.3)
# x/y はキャンバス中心を原点とする正規化座標 (-1.0〜1.0)。
# 字幕クリップでは x/y のみ使用し、None は「設定の alignment/margin に従う」を表す。
class Transform:

    def __init__(self, x=0.0, y=0.0, scale=1.0, rotation=0.0, opacity=1.0):
        self.x = x
        self.y = y
        self.scale = scale
        self.rotation = rotation
        self.opacity = opacity

    # 位置が明示指定されているか (字幕の \pos 付与判定に使う / §8.3)
    def is_positioned(self):
        return self.x is not None and self.y is not None

    # 映像/画像クリップ用の辞書 (全項目)
    def to_dict(self):
        return {
            "x": round(_to_float(self.x), 4),
            "y": round(_to_float(self.y), 4),
            "scale": round(_to_float(self.scale, 1.0), 4),
            "rotation": round(_to_float(self.rotation), 4),
            "opacity": round(_to_float(self.opacity, 1.0), 4),
        }

    # 字幕クリップ用の辞書 (位置のみ。None を保持する)
    def to_position_dict(self):
        return {
            "x": None if self.x is None else round(float(self.x), 4),
            "y": None if self.y is None else round(float(self.y), 4),
        }

    def copy(self):
        return Transform(self.x, self.y, self.scale, self.rotation, self.opacity)

    # 映像/画像クリップ用の復元 (欠落は既定値)
    @staticmethod
    def from_dict(data):
        data = data or {}
        return Transform(
            x=_to_float(data.get("x"), 0.0),
            y=_to_float(data.get("y"), 0.0),
            scale=_to_float(data.get("scale"), 1.0),
            rotation=_to_float(data.get("rotation"), 0.0),
            opacity=_to_float(data.get("opacity"), 1.0),
        )

    # 字幕クリップ用の復元 (x/y は None を保持する)
    @staticmethod
    def from_position_dict(data):
        data = data or {}
        return Transform(
            x=_to_float_or_none(data.get("x")),
            y=_to_float_or_none(data.get("y")),
        )


# メディアプールの 1 件 (元動画 / OP / ED / D&D 素材 / §6.2.2)
class MediaRef:

    def __init__(self, media_id, kind, path, duration_sec=None,
                 width=0, height=0, fps=0.0, has_audio=False, path_rel=""):
        self.id = media_id
        self.kind = kind                      # MEDIA_VIDEO / MEDIA_IMAGE
        self.path = path
        # プロジェクトファイルからの相対パス (ver3 resolve7 §5.9)。
        # 保存時に project_io が書き込み、開くときに素材を探す 2 番目の手がかりに使う。
        # プロジェクトと素材をまとめて別フォルダへ移した場合に効く。
        self.path_rel = str(path_rel or "")
        self.duration_sec = duration_sec      # 画像は None (尺を持たない)
        self.width = _to_int(width)
        self.height = _to_int(height)
        self.fps = _to_float(fps)
        self.has_audio = bool(has_audio)

    # 静止画かどうか (尺を無限に伸ばせる素材か)
    def is_image(self):
        return self.kind == MEDIA_IMAGE

    # トリムの上限となる素材尺 (画像は上限なし= None)
    def max_source_sec(self):
        if self.is_image():
            return None
        return self.duration_sec

    def to_dict(self):
        data = {
            "id": self.id,
            "kind": self.kind,
            "path": self.path,
            "duration_sec": None if self.duration_sec is None else round_sec(self.duration_sec),
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 4),
            "has_audio": self.has_audio,
        }
        # 任意キー。持っているときだけ書き出す (旧ファイル・旧アプリと共存させるため)
        if self.path_rel:
            data["path_rel"] = self.path_rel
        return data

    @staticmethod
    def from_dict(data):
        return MediaRef(
            media_id=str(data.get("id", "")),
            kind=str(data.get("kind", MEDIA_VIDEO)),
            path=str(data.get("path", "")),
            duration_sec=_to_float_or_none(data.get("duration_sec")),
            width=data.get("width", 0),
            height=data.get("height", 0),
            fps=data.get("fps", 0.0),
            has_audio=bool(data.get("has_audio", False)),
            path_rel=data.get("path_rel", ""),
        )


# 映像/画像クリップ (§6.2.2)
# timeline_start は Timeline 時間、source_in/out は素材内の時間。
class Clip:

    def __init__(self, clip_id, media_id, timeline_start, duration,
                 source_in=0.0, source_out=None, z_order=BASE_Z_ORDER,
                 enabled=True, transform=None, origin=None):
        self.id = clip_id
        self.media_id = media_id
        self.timeline_start = _to_float(timeline_start)
        self.duration = _to_float(duration)
        self.source_in = _to_float(source_in)
        # source_out 省略時は source_in + duration (速度変更は未対応 / §6.2.3)
        self.source_out = _to_float(
            source_out if source_out is not None else self.source_in + self.duration)
        self.z_order = _to_int(z_order, BASE_Z_ORDER)
        self.enabled = bool(enabled)
        self.transform = transform or Transform()
        self.origin = dict(origin or {})

    # Timeline 上の終了時刻
    @property
    def timeline_end(self):
        return self.timeline_start + self.duration

    # 指定 Timeline 時刻を含むか (終端は含まない)
    def contains(self, timeline_sec):
        return self.timeline_start <= timeline_sec < self.timeline_end

    # 由来の種別 (UI の色分け・ラベル用 / §6.2.3)
    def origin_type(self):
        return str(self.origin.get("type", ""))

    # OP/ED クリップか (自動配置由来のもの)
    def is_opening_or_ending(self):
        return self.origin_type() in (ORIGIN_OPENING, ORIGIN_ENDING)

    def copy(self):
        return Clip(
            self.id, self.media_id, self.timeline_start, self.duration,
            self.source_in, self.source_out, self.z_order, self.enabled,
            self.transform.copy(), dict(self.origin),
        )

    def to_dict(self):
        return {
            "id": self.id,
            "media_id": self.media_id,
            "timeline_start": round_sec(self.timeline_start),
            "duration": round_sec(self.duration),
            "source_in": round_sec(self.source_in),
            "source_out": round_sec(self.source_out),
            "z_order": self.z_order,
            "enabled": self.enabled,
            "transform": self.transform.to_dict(),
            "origin": dict(self.origin),
        }

    @staticmethod
    def from_dict(data):
        return Clip(
            clip_id=str(data.get("id", "")),
            media_id=str(data.get("media_id", "")),
            timeline_start=data.get("timeline_start", 0.0),
            duration=data.get("duration", 0.0),
            source_in=data.get("source_in", 0.0),
            source_out=data.get("source_out"),
            z_order=data.get("z_order", BASE_Z_ORDER),
            enabled=data.get("enabled", True),
            transform=Transform.from_dict(data.get("transform")),
            origin=data.get("origin"),
        )


# 字幕クリップ (§6.2.2)
# 時刻は Timeline 時間。V1 の編集には追従しない (回答 Q2 / §6.3.1)。
# origin.source_start/source_end に由来の元動画時刻を残し、将来の再同期に備える。
class SubtitleClip:

    def __init__(self, clip_id, timeline_start, duration, text="", role=DEFAULT_ROLE,
                 font="", font_size=None, use=True, z_order=DEFAULT_SUBTITLE_Z_ORDER,
                 transform=None, origin=None, color="", outline_color=""):
        self.id = clip_id
        self.timeline_start = _to_float(timeline_start)
        self.duration = _to_float(duration)
        self.text = str(text or "")
        self.role = str(role or DEFAULT_ROLE)
        self.font = str(font or "")
        self.font_size = _to_int_or_none(font_size)
        self.use = bool(use)
        self.z_order = _to_int(z_order, DEFAULT_SUBTITLE_Z_ORDER)
        # 位置上書き (x/y が None なら設定の alignment/margin に従う / §8.3)
        self.transform = transform or Transform(x=None, y=None)
        self.origin = dict(origin or {})
        # このクリップだけの文字色 (HTML #RRGGBB)。空文字 = 役割の色に従う (resolve6 §3-2)
        self.color = str(color or "")
        # このクリップだけの縁の色 (ASS &HAABBGGRR)。空文字 = 役割の色に従う (resolve6 §3-2)
        self.outline_color = str(outline_color or "")

    @property
    def timeline_end(self):
        return self.timeline_start + self.duration

    def contains(self, timeline_sec):
        return self.timeline_start <= timeline_sec < self.timeline_end

    def origin_type(self):
        return str(self.origin.get("type", ""))

    def copy(self):
        return SubtitleClip(
            self.id, self.timeline_start, self.duration, self.text, self.role,
            self.font, self.font_size, self.use, self.z_order,
            self.transform.copy(), dict(self.origin),
            self.color, self.outline_color,
        )

    # 既存資産 (build_subtitle_file / resolve_export) が扱う item 形式へ変換する
    # 既存の {"start","end","text","use","role","font","font_size"} と互換。
    # 位置を明示指定しているときだけ pos_x/pos_y を足す (未指定なら既存と完全に同一の辞書)。
    def to_item(self):
        item = {
            "start": self.timeline_start,
            "end": self.timeline_end,
            "text": self.text,
            "use": self.use,
            "role": self.role,
            "font": self.font,
            "font_size": self.font_size,
        }
        if self.transform.is_positioned():
            item["pos_x"] = self.transform.x
            item["pos_y"] = self.transform.y
        # 色は個別指定があるときだけ載せる (未指定なら現行と完全に同一の辞書 / resolve6 §5.2)
        if self.color:
            item["color"] = self.color
        if self.outline_color:
            item["outline_color"] = self.outline_color
        return item

    def to_dict(self):
        return {
            "id": self.id,
            "timeline_start": round_sec(self.timeline_start),
            "duration": round_sec(self.duration),
            "text": self.text,
            "role": self.role,
            "font": self.font,
            "font_size": self.font_size,
            "use": self.use,
            "z_order": self.z_order,
            "transform": self.transform.to_position_dict(),
            "origin": dict(self.origin),
            # 個別の色 (未指定は空文字。旧ファイルに無くても from_dict で空文字になる)
            "color": self.color,
            "outline_color": self.outline_color,
        }

    @staticmethod
    def from_dict(data):
        return SubtitleClip(
            clip_id=str(data.get("id", "")),
            timeline_start=data.get("timeline_start", 0.0),
            duration=data.get("duration", 0.0),
            text=data.get("text", ""),
            role=data.get("role", DEFAULT_ROLE),
            font=data.get("font", ""),
            font_size=data.get("font_size"),
            use=data.get("use", True),
            z_order=data.get("z_order", DEFAULT_SUBTITLE_Z_ORDER),
            transform=Transform.from_position_dict(data.get("transform")),
            origin=data.get("origin"),
            color=data.get("color", ""),
            outline_color=data.get("outline_color", ""),
        )


# 音声クリップ (§6.3.4 / R18)
# 【重要】固有の時刻を持たない。開始・尺・ソース位置はすべて link_clip 先の
# 映像クリップから導出する。これにより映像編集との音ズレが構造的に起こらない。
class AudioClip:

    def __init__(self, clip_id, link_clip, gain_db=0.0, muted=False):
        self.id = clip_id
        self.link_clip = link_clip
        self.gain_db = _to_float(gain_db)
        self.muted = bool(muted)

    # リンク先の映像クリップを返す (欠落時は None)
    def linked(self, timeline):
        return timeline.clip_by_id(self.link_clip)

    # 以下 3 つはすべてリンク先からの導出値 (自前の値は持たない)
    def timeline_start(self, timeline):
        video = self.linked(timeline)
        return None if video is None else video.timeline_start

    def duration(self, timeline):
        video = self.linked(timeline)
        return None if video is None else video.duration

    def source_in(self, timeline):
        video = self.linked(timeline)
        return None if video is None else video.source_in

    def copy(self):
        return AudioClip(self.id, self.link_clip, self.gain_db, self.muted)

    # 音声クリップは時刻フィールドを持たない (§6.2.3)
    def to_dict(self):
        return {
            "id": self.id,
            "link_clip": self.link_clip,
            "gain_db": round(self.gain_db, 3),
            "muted": self.muted,
        }

    @staticmethod
    def from_dict(data):
        return AudioClip(
            clip_id=str(data.get("id", "")),
            link_clip=str(data.get("link_clip", "")),
            gain_db=data.get("gain_db", 0.0),
            muted=data.get("muted", False),
        )


# トラック (§6.3.1)
class Track:

    def __init__(self, track_id, kind, index, name="", enabled=True, locked=False,
                 is_base=False, link_track=None, clips=None):
        self.id = track_id
        self.kind = kind
        self.index = _to_int(index, 1)
        self.name = name or track_id
        self.enabled = bool(enabled)
        self.locked = bool(locked)
        # ベース (V1) は最背面固定で、レイヤー変更の対象外 (§6.5-2)
        self.is_base = bool(is_base)
        # 音声トラックがどの映像トラックへリンクしているか (R18)
        self.link_track = link_track
        self.clips = list(clips or [])

    def is_video(self):
        return self.kind == TRACK_VIDEO

    def is_audio(self):
        return self.kind == TRACK_AUDIO

    def is_subtitle(self):
        return self.kind == TRACK_SUBTITLE

    # 時系列順に整列する (編集後は必ず呼ぶ)
    def sort_clips(self, timeline=None):
        if self.is_audio():
            # 音声は導出値で並べる (リンク欠落は末尾へ寄せる)
            def key(clip):
                start = clip.timeline_start(timeline) if timeline is not None else None
                return float("inf") if start is None else start
            self.clips.sort(key=key)
        else:
            self.clips.sort(key=lambda c: c.timeline_start)

    def clip_by_id(self, clip_id):
        for clip in self.clips:
            if clip.id == clip_id:
                return clip
        return None

    def remove_clip(self, clip_id):
        for i, clip in enumerate(self.clips):
            if clip.id == clip_id:
                return self.clips.pop(i)
        return None

    def to_dict(self):
        data = {
            "id": self.id,
            "kind": self.kind,
            "index": self.index,
            "name": self.name,
            "enabled": self.enabled,
            "locked": self.locked,
            "clips": [clip.to_dict() for clip in self.clips],
        }
        if self.is_video():
            data["is_base"] = self.is_base
        if self.is_audio() and self.link_track:
            data["link_track"] = self.link_track
        return data

    @staticmethod
    def from_dict(data):
        kind = str(data.get("kind", TRACK_VIDEO))
        if kind == TRACK_AUDIO:
            clips = [AudioClip.from_dict(c) for c in data.get("clips", [])]
        elif kind == TRACK_SUBTITLE:
            clips = [SubtitleClip.from_dict(c) for c in data.get("clips", [])]
        else:
            clips = [Clip.from_dict(c) for c in data.get("clips", [])]
        return Track(
            track_id=str(data.get("id", "")),
            kind=kind,
            index=data.get("index", 1),
            name=data.get("name", ""),
            enabled=data.get("enabled", True),
            locked=data.get("locked", False),
            is_base=data.get("is_base", False),
            link_track=data.get("link_track"),
            clips=clips,
        )


# タイムライン本体 (編集情報の唯一のソース / §4-1)
class Timeline:

    def __init__(self, fps=60, width=1920, height=1080, orientation="landscape",
                 source=None, edit_points=None, media_pool=None, tracks=None,
                 playhead_sec=0.0, zoom_px_per_sec=40.0):
        self.fps = _to_int(fps, 60) or 60
        self.width = _to_int(width, 1920)
        self.height = _to_int(height, 1080)
        self.orientation = orientation
        # 元動画の情報 (input_path / media_path / duration_sec / width / height / fps)
        self.source = dict(source or {})
        # 無音カットの検出結果そのもの (§6.2.2 edit_points)
        self.edit_points = dict(edit_points or {})
        self.media_pool = list(media_pool or [])
        self.tracks = list(tracks or [])
        self.playhead_sec = _to_float(playhead_sec)
        self.zoom_px_per_sec = _to_float(zoom_px_per_sec, 40.0)
        # ID 採番のカウンタ (プロジェクト内で一意にする / §6.2.3)
        self._id_counters = {}

    # ------------------------------------------------------------------
    # ID 採番
    # ------------------------------------------------------------------

    # プレフィックス付きの一意な ID を払い出す (m1 / c1 / a1 / s1 …)
    def next_id(self, prefix):
        used = self._collect_ids()
        counter = self._id_counters.get(prefix, 0)
        while True:
            counter += 1
            candidate = f"{prefix}{counter}"
            if candidate not in used:
                self._id_counters[prefix] = counter
                return candidate

    # 現在使用中の全 ID (メディア + 全クリップ + トラック)
    def _collect_ids(self):
        used = {track.id for track in self.tracks}
        used.update(media.id for media in self.media_pool)
        for track in self.tracks:
            used.update(clip.id for clip in track.clips)
        return used

    # ------------------------------------------------------------------
    # 参照
    # ------------------------------------------------------------------

    def track_by_id(self, track_id):
        for track in self.tracks:
            if track.id == track_id:
                return track
        return None

    def media_by_id(self, media_id):
        for media in self.media_pool:
            if media.id == media_id:
                return media
        return None

    # どれかのクリップが参照している素材の ID (ver5 resolve6 §5.5)
    #
    # 使用可否 (enabled) は見ない。「使わない」にしただけのクリップが指す素材も
    # 使用中に数える (使用可否を戻したときに素材が無い、という事態を避けるため)。
    # ここに入らない素材は、無くても Timeline が成立する = 復旧も保存も要らない。
    def used_media_ids(self):
        used = set()
        for track in self.tracks:
            for clip in track.clips:
                media_id = getattr(clip, "media_id", None)
                if media_id:
                    used.add(media_id)
        return used

    # 同一パスの既存メディアを返す (D&D の重複登録を避ける / §6.8)
    def media_by_path(self, path):
        import os
        target = os.path.normcase(os.path.abspath(str(path)))
        for media in self.media_pool:
            if os.path.normcase(os.path.abspath(media.path)) == target:
                return media
        return None

    # 全トラックからクリップを ID で探す
    def clip_by_id(self, clip_id):
        for track in self.tracks:
            clip = track.clip_by_id(clip_id)
            if clip is not None:
                return clip
        return None

    # クリップが属するトラックを返す
    def track_of_clip(self, clip_id):
        for track in self.tracks:
            if track.clip_by_id(clip_id) is not None:
                return track
        return None

    def video_tracks(self):
        return [t for t in self.tracks if t.is_video()]

    def audio_tracks(self):
        return [t for t in self.tracks if t.is_audio()]

    def subtitle_tracks(self):
        return [t for t in self.tracks if t.is_subtitle()]

    # ベース映像トラック (V1)。未設定なら index 最小の映像トラックを返す。
    def base_video_track(self):
        for track in self.video_tracks():
            if track.is_base:
                return track
        tracks = sorted(self.video_tracks(), key=lambda t: t.index)
        return tracks[0] if tracks else None

    # ベース音声トラック (A1)
    def base_audio_track(self):
        base_video = self.base_video_track()
        for track in self.audio_tracks():
            if base_video is not None and track.link_track == base_video.id:
                return track
        tracks = sorted(self.audio_tracks(), key=lambda t: t.index)
        return tracks[0] if tracks else None

    # 既定の字幕トラック (S1)
    def base_subtitle_track(self):
        tracks = sorted(self.subtitle_tracks(), key=lambda t: t.index)
        return tracks[0] if tracks else None

    # 役割に対応する字幕トラックを返す (無ければ None。作成はここでは行わない)
    # コメント役割だけ専用トラック (既定 S2) へ置き分ける (ver3 resolve11 §5.1)。
    # enabled=False のときは常に既定トラック (S1) を返す = 従来どおりの 1 本運用。
    def subtitle_track_for_role(self, role,
                                comment_track_id=COMMENT_SUBTITLE_TRACK_ID,
                                enabled=True):
        if not enabled or str(role or "") != ROLE_COMMENT:
            return self.base_subtitle_track()
        track = self.track_by_id(comment_track_id)
        if track is not None and track.is_subtitle():
            return track
        # ID を設定で変えられている場合に備え「S1 以外の字幕トラック」で拾い直す
        base = self.base_subtitle_track()
        for candidate in sorted(self.subtitle_tracks(), key=lambda t: t.index):
            if base is None or candidate.id != base.id:
                return candidate
        return None

    # 映像トラックにリンクしている音声トラックを返す
    def audio_track_for(self, video_track_id):
        for track in self.audio_tracks():
            if track.link_track == video_track_id:
                return track
        return None

    # 映像クリップにリンクしている音声クリップを返す (R18)
    def audio_clip_for(self, video_clip_id):
        for track in self.audio_tracks():
            for clip in track.clips:
                if clip.link_clip == video_clip_id:
                    return clip
        return None

    # ------------------------------------------------------------------
    # 導出値
    # ------------------------------------------------------------------

    # タイムライン全長 (全トラックの最終終端 / §6.2.3 導出値)
    def duration_sec(self):
        end = 0.0
        for track in self.tracks:
            if track.is_audio():
                continue  # 音声は映像からの導出のため計算に含めない
            for clip in track.clips:
                end = max(end, clip.timeline_end)
        return end

    # ベーストラック上の有効クリップを時系列順に返す (レンダリング/TimeMap の入力)
    def base_clips(self, include_disabled=False):
        track = self.base_video_track()
        if track is None:
            return []
        clips = [c for c in track.clips if include_disabled or c.enabled]
        return sorted(clips, key=lambda c: c.timeline_start)

    # オーバーレイ要素 (V2 以降のクリップ + 字幕クリップ) を z_order 昇順で返す (§8.4)
    # ベース (V1) のクリップは常に最背面のため含めない。
    def overlay_elements(self, include_disabled=False):
        elements = []
        base = self.base_video_track()
        for track in self.video_tracks():
            if base is not None and track.id == base.id:
                continue
            if not track.enabled:
                continue
            for clip in track.clips:
                if include_disabled or clip.enabled:
                    elements.append(clip)
        for track in self.subtitle_tracks():
            if not track.enabled:
                continue
            for clip in track.clips:
                if include_disabled or clip.use:
                    elements.append(clip)
        # z_order 昇順 → 同値は開始時刻順で安定させる
        return sorted(elements, key=lambda c: (c.z_order, c.timeline_start))

    # 使用する字幕クリップを時系列順に返す (ASS 生成の入力 / §8.3)
    def subtitle_clips(self, only_used=True):
        clips = []
        for track in self.subtitle_tracks():
            if not track.enabled:
                continue
            for clip in track.clips:
                if not only_used or clip.use:
                    clips.append(clip)
        return sorted(clips, key=lambda c: c.timeline_start)

    # ベーストラック上の空白 (ギャップ) を [(start, end), ...] で返す (§8.2)
    def base_gaps(self):
        gaps = []
        cursor = 0.0
        for clip in self.base_clips():
            if clip.timeline_start > cursor + 1e-6:
                gaps.append((cursor, clip.timeline_start))
            cursor = max(cursor, clip.timeline_end)
        return gaps

    # 新しい映像トラックの ID を払い出す (D&D で自動生成する / §6.8)
    def next_video_track_id(self):
        index = max([t.index for t in self.video_tracks()] or [0]) + 1
        return f"V{index}", index

    # 新しい音声トラックの ID を払い出す
    def next_audio_track_id(self):
        index = max([t.index for t in self.audio_tracks()] or [0]) + 1
        return f"A{index}", index

    # 全トラックのクリップを時系列順へ整列する (編集後に呼ぶ)
    def normalize(self):
        for track in self.tracks:
            track.sort_clips(self)

    # ------------------------------------------------------------------
    # フレーム量子化 (§3-3)
    # ------------------------------------------------------------------

    # 秒をフレーム番号へ量子化する。差分を足し込まないため誤差が累積しない。
    def to_frames(self, sec):
        return int(round(_to_float(sec) * self.fps))

    # フレーム番号を秒へ戻す
    def from_frames(self, frames):
        return _to_int(frames) / float(self.fps)

    # 秒をフレーム境界へ吸着させた秒値を返す
    def quantize(self, sec):
        return self.from_frames(self.to_frames(sec))
