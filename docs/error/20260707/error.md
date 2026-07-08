# Error
Webからダウンロードしてインストール後、起動した後に以下のようなエラーが発生しました。そして起動することができませんでした。
Failed to execute script 'main_window' due to  unhandled exeption:
'utf-8' codec can't decode byte 0x83 in position 594: invalid start byte

Traceback (most recent call last):
  File "main_window.py", line 490, in <module>
  File "main_window.py", line 482, in main
  File "main_window.py", line 256, in __init__
  File "src\settings\settings_window.py", line 219, in load_settings
  File "<frozen codecs>", line 322, in decode
UnicodeDecodeError: 'utf-8' codec can't decode byte 0x83 in position 594: invalid start byte

# Output
resolve.mdを修正設計書として出力してください。