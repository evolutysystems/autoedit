# テロップの縁取りの白がそのまま背景になる (現象報告)

報告日: 2026-11-22 / 利用者報告

> 今バージョン 1.8.6 か 5 で動画を作成したんですが、縁取りの白がそのまま背景に
> なってしまっています。
> 対象動画:
> `C:\Users\evolu\Documents\AutoEdit\output\archive_vod_2930581402_combined.mp4`
> 前も似たような事例を修正したはずなのですが、戻っているのはなぜですか？

## 確認した事実

| 項目 | 値 |
|---|---|
| 対象動画 | `archive_vod_2930581402_combined.mp4` (2026-11-22 01:09 出力 / 743,948,794 byte) |
| 元プロジェクト | `C:\Users\evolu\Documents\AutoEdit\output\vod_2930581402.archive.timeline.json` |
| ログ | `%LOCALAPPDATA%\Programs\AutoEdit\logs\autoedit_20261121.log` (書き出しは 2026-11-22 00:05〜01:09) |
| 見え方 | 赤文字のテロップの背後に、**行ごとに**白い不透明な四角が出る |

原因と修正設計は `docs/error/20261122/resolve.md`。
