# AI 編集で保存したプロジェクトが一覧に出ず「続きから」編集できない (現象報告)

報告日: 2026-11-25 / 開発環境での実行

> AI 編集を行って保存を行ったのに、一覧に表示されないため続きから編集をすることができません。

## 確認した事実

| 項目 | 値 |
|---|---|
| ログ | `D:\develop\autoedit\src\gui\logs\autoedit_20261125.log` (GUI を `src/gui` から起動したためこちらへ出ている) |
| 実行 | 2026-11-25 02:17:12 開始 → 02:47:41 完了 (約 30 分) |
| 対象 VOD | Twitch `2938816056` / 7.4 GB / 画質 source (2560x1440@60fps) |
| 用途 | `youtube_long` / `youtube_shorts` / `tiktok` の 3 本 |
| 作業先 | `C:\Users\evolu\AppData\Local\Stretheus\archive_work` |

AI 編集は 3 本とも完走し、プロジェクトは保存されている。

```
02:44:47,984 [INFO] project_io: プロジェクト保存: …\vod_2938816056.ai_youtube_long.timeline.json
02:46:21,311 [INFO] project_io: プロジェクト保存: …\vod_2938816056.ai_youtube_shorts.timeline.json
02:47:41,160 [INFO] project_io: プロジェクト保存: …\vod_2938816056.ai_tiktok.timeline.json
02:47:41,171 [INFO] ai_runner: AI の経路 D 完了: (3 本)
```

それにもかかわらず、アーカイブタブの「一覧…」にこの 3 本が 1 本も出ない。

## 症状の切り分け

| 入口 | 結果 |
|---|---|
| アーカイブタブの一覧 (`ProjectLibraryDialog(KIND_ARCHIVE)`) | **出ない** |
| クリップ用タブの一覧 (`ProjectLibraryDialog(KIND_CLIP)`) | **出ない** (一瞬出て消える) |
| アーカイブタブの「続きから」コンボ | `youtube_long` の 1 本だけ出る。`shorts` / `tiktok` は出ない |

「続きから」に 1 本だけ出ているのは AI 編集の結果ではなく、**完了後に開いた Timeline 画面で
保存した**ことで `timeline_editor_dialog._remember_recent()` が走ったためである。
`shorts` / `tiktok` は開いていないため、どこからも辿れない。

## この環境で一覧の元になっているもの

`setting.json` の走査フォルダはどちらも空であり、**一覧の中身は履歴だけ**である。

```
timeline.project_dir          = ""
general.output_directory      = ""
timeline.project.library.extra_dirs = []
timeline.project.recent_archive = [ vod_2938816056.ai_youtube_long.timeline.json,
                                    vod_2938439428.archive.timeline.json, … ]
```

`project_io.remember_recent` のコメントが述べているとおりの状態である。

> 「続きから」(一覧・コンボ) が見るのはこの履歴と走査フォルダだけで、
> `project_dir` / `output_directory` が空の設定では走査フォルダが無い。

## 再現の形

AI 編集 (経路 D) を 1 回実行する。以後、

* 作られた 3 本はアーカイブタブの一覧に出ない (履歴に積まれていても出ない)。
* `shorts` / `tiktok` は履歴にも積まれないため、ファイル選択以外に開く道が無い。

原因と修正設計は `docs/error/20261125/resolve.md`。
