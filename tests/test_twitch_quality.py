# VOD の取得画質の決定 (src/archive/twitch_source.resolve_quality) の単体テスト
# 実行: python -m unittest discover -s tests
# 背景 (docs/error/20260810/resolve.md):
#   twitch-dl の "source" は「最高画質」ではなく group_id=="chunked" の完全一致のため、
#   source を持たない VOD では取得が失敗していた。希望画質が無いときは
#   利用できる最高画質へ落とす。ネットワークは使わない (一覧を与えて判定だけを見る)。
import unittest

from src.archive import twitch_source


def _playlist(name, group_id, resolution, is_source=False):
    return {"name": name, "group_id": group_id, "resolution": resolution,
            "is_source": is_source}


# 実際に失敗した VOD 2833484601 の一覧 (source 無し / error 20260810 §2-2)
_NO_SOURCE = [
    _playlist("1080p60", "1080p60", "1920x1080"),
    _playlist("720p60", "720p60", "1280x720"),
    _playlist("360p", "360p30", "640x360"),
    _playlist("Audio Only", "audio_only", None),
]

# source を持つ通常の VOD
_WITH_SOURCE = [
    _playlist("Source", "chunked", "1920x1080", is_source=True),
    _playlist("720p60", "720p60", "1280x720"),
    _playlist("Audio Only", "audio_only", None),
]


class ResolveQualityTest(unittest.TestCase):

    # source がある VOD は従来どおり "source" を渡す (コマンドが変わらない)
    def test_keeps_source_when_available(self):
        self.assertEqual(twitch_source.resolve_quality(_WITH_SOURCE, "source"), "source")

    # source が無い VOD は利用できる最高画質へ落ちる (今回の不具合)
    def test_falls_back_to_best_when_no_source(self):
        self.assertEqual(twitch_source.resolve_quality(_NO_SOURCE, "source"), "1080p60")

    # 実在する画質を指定したときはそのまま使う
    def test_keeps_existing_named_quality(self):
        self.assertEqual(twitch_source.resolve_quality(_NO_SOURCE, "720p60"), "720p60")

    # group_id での指定も実在扱いにする (360p は name と group_id が違う)
    def test_accepts_group_id(self):
        self.assertEqual(twitch_source.resolve_quality(_NO_SOURCE, "360p30"), "360p30")

    # 実在しない画質を指定したときも最高画質へ落ちる
    def test_falls_back_for_unknown_quality(self):
        self.assertEqual(twitch_source.resolve_quality(_NO_SOURCE, "1440p"), "1080p60")

    # 音声のみは映像が無いため選ばない
    def test_never_selects_audio_only(self):
        only_audio = [_playlist("Audio Only", "audio_only", None)]
        self.assertEqual(twitch_source.resolve_quality(only_audio, "source"), "source")

    # 一覧を取れなかったときは希望画質のまま (従来と同じコマンドになる)
    def test_returns_preferred_when_list_empty(self):
        self.assertEqual(twitch_source.resolve_quality([], "source"), "source")
        self.assertEqual(twitch_source.resolve_quality(None, "1080p60"), "1080p60")

    # 解像度が同じならフレームレートの高い方を採る
    def test_prefers_higher_fps_at_same_resolution(self):
        playlists = [
            _playlist("1080p", "1080p30", "1920x1080"),
            _playlist("1080p60", "1080p60", "1920x1080"),
        ]
        self.assertEqual(twitch_source.resolve_quality(playlists, "source"), "1080p60")

    # 解像度が取れない一覧でも画質名から順位を付けられる
    def test_ranks_by_name_when_resolution_missing(self):
        playlists = [
            _playlist("480p30", "480p30", None),
            _playlist("720p60", "720p60", None),
        ]
        self.assertEqual(twitch_source.resolve_quality(playlists, "source"), "720p60")

    # 空文字・None の希望画質は "source" と同じ扱いにする
    def test_empty_preferred_means_source(self):
        self.assertEqual(twitch_source.resolve_quality(_WITH_SOURCE, ""), "source")
        self.assertEqual(twitch_source.resolve_quality(_NO_SOURCE, None), "1080p60")


class DownloadConfigTest(unittest.TestCase):

    # 既定で自動フォールバックが有効 (キーが無い既存 setting.json でも動く)
    def test_default_enables_fallback(self):
        from src.archive.config import download_config
        self.assertTrue(download_config({})["vod_quality_fallback"])
        self.assertTrue(download_config({"archive": {"download": {}}})["vod_quality_fallback"])

    # 明示的に無効化できる (切り戻し)
    def test_can_disable_fallback(self):
        from src.archive.config import download_config
        cfg = download_config({"archive": {"download": {"vod_quality_fallback": False}}})
        self.assertFalse(cfg["vod_quality_fallback"])


if __name__ == "__main__":
    unittest.main()
