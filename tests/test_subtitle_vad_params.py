# 音声認識へ渡す VAD パラメータ (src/modules/subtitle_generator.py) の単体テスト
# 実行: python -m unittest discover -s tests
# ver3 resolve8 §10-2:
#   ・speech_pad_ms を明示して渡すこと (未指定だと既定 400ms で語頭の無音まで
#     認識対象に入り、テロップが発話より手前に出る)
#   ・min_silence_duration_ms は設定値がそのまま届くこと
#   ・VAD 無効時は VAD 関連のキーを一切渡さないこと (従来どおり)
#   ・VAD 付きで失敗したら VAD 無しで再試行する既存フォールバックが壊れていないこと
# WhisperModel はダミーへ差し替え、実際の認識は行わない。
import unittest

from src.modules.subtitle_generator import _VAD_SPEECH_PAD_MS, WhisperTextSource


# transcribe() の呼び出し内容を記録するだけのダミーモデル
class _FakeModel:

    def __init__(self, fail_times=0):
        self.calls = []
        self._fail_times = fail_times

    def transcribe(self, input_path, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self._fail_times:
            raise RuntimeError("VAD の初期化に失敗しました (テスト)")
        return ([], None)


class VadParameterTest(unittest.TestCase):

    def _source(self, **overrides):
        settings = {"whisper_vad_filter": True, "whisper_vad_min_silence_ms": 500}
        settings.update(overrides)
        return WhisperTextSource(settings)

    # 既定: 余白は 300ms 削った 100ms を明示して渡す (resolve8 §3-2)
    def test_speech_pad_is_passed(self):
        model = _FakeModel()
        self._source()._transcribe(model, "dummy.wav", "ja")
        self.assertEqual(model.calls[0]["vad_parameters"],
                         {"min_silence_duration_ms": 500, "speech_pad_ms": 100})
        self.assertEqual(_VAD_SPEECH_PAD_MS, 100)

    # 無音長は設定値がそのまま届き、余白は定数のまま
    def test_min_silence_comes_from_settings(self):
        model = _FakeModel()
        self._source(whisper_vad_min_silence_ms=800)._transcribe(model, "dummy.wav", "ja")
        self.assertEqual(model.calls[0]["vad_parameters"],
                         {"min_silence_duration_ms": 800, "speech_pad_ms": 100})

    # VAD 無効時は VAD 関連のキーを渡さない (完全に現行どおり)
    def test_vad_disabled_passes_no_vad_keys(self):
        model = _FakeModel()
        self._source(whisper_vad_filter=False)._transcribe(model, "dummy.wav", "ja")
        self.assertNotIn("vad_filter", model.calls[0])
        self.assertNotIn("vad_parameters", model.calls[0])

    # VAD 付きで失敗したら VAD 無しで再試行する (既存フォールバックの回帰)
    def test_falls_back_without_vad(self):
        model = _FakeModel(fail_times=1)
        self._source()._transcribe(model, "dummy.wav", "ja")
        self.assertEqual(len(model.calls), 2)
        self.assertIn("vad_parameters", model.calls[0])
        self.assertNotIn("vad_filter", model.calls[1])
        self.assertNotIn("vad_parameters", model.calls[1])
        # 認識そのものの指定は 2 回目も維持される
        self.assertEqual(model.calls[1]["language"], "ja")
        self.assertIn("word_timestamps", model.calls[1])

    # VAD 無効時の失敗はフォールバックせずそのまま送出する
    def test_failure_without_vad_is_raised(self):
        model = _FakeModel(fail_times=1)
        source = self._source(whisper_vad_filter=False)
        with self.assertRaises(RuntimeError):
            source._transcribe(model, "dummy.wav", "ja")
        self.assertEqual(len(model.calls), 1)


if __name__ == "__main__":
    unittest.main()
