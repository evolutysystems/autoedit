# サブプロセス関連の共通ユーティリティ
# GUI(windowed)から FFmpeg/ffprobe 等のコンソール系プロセスを起動する際に
# 新規コンソール窓(cmd)が表示されるのを防ぐための起動フラグを提供する。
import subprocess


# サブプロセス起動時にコンソール窓を出さないための creationflags を返す
# Windows: CREATE_NO_WINDOW / 非Windows: 0 (フラグ無し・無害)
# CREATE_NO_WINDOW は Windows 専用のため getattr で存在時のみ取得する
def no_window_creationflags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
