# Timeline パッケージ (docs/request/ver3/resolve.md)
# 編集情報の唯一のソースである Timeline を、Qt に依存しない純 Python として提供する。
# GUI (src/gui/timeline) はこのパッケージを読み書きするだけで、編集ロジックを持たない。
#
# model        : Timeline / Track / Clip / SubtitleClip / AudioClip / MediaRef / Transform
# timemap      : TimeMap (元動画時間 ↔ Timeline 時間の写像)
# project_io   : プロジェクト JSON の読み書き・検証・マイグレーション
# builder      : 編集点 + 字幕 items → Timeline 構築
# commands     : 編集操作 (移動/トリム/分割/削除/レイヤー) と Undo/Redo
# renderer     : Timeline → FFmpeg → 動画生成
# frame_source : プレビュー用のフレーム取得 (PyAV / ffmpeg フォールバック)
# audio_source : プレビュー用の音声チャンク生成
# media_probe  : D&D されたメディアの種別・尺・寸法の判定
from .model import (
    AudioClip,
    Clip,
    MediaRef,
    SubtitleClip,
    Timeline,
    Track,
    Transform,
)
from .timemap import TimeMap

__all__ = [
    "AudioClip",
    "Clip",
    "MediaRef",
    "SubtitleClip",
    "Timeline",
    "TimeMap",
    "Track",
    "Transform",
]
