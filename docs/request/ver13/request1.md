# ver13 request1 — セクション追加で無音カットの有無を選べるようにする (要望)

要望日: 2026-11-25

## 要望

> セクション追加で無音カットをするかどうかの判断をチェックボックスで行えるようにしてください。

## 背景 (調査で分かった現状)

無音カットの ON/OFF は**アーカイブタブのチェックボックス**で決まり、その実行のあいだだけ
`archive.clip_pipeline.silence_cut` の値として持ち回る (`setting.json` へは書き戻さない)。

セクション追加 (`gui/timeline/archive_timeline_dialog._on_add_section`) は、画面が持っている
`clip_settings` をそのまま `clip_writer.prepare_one_clip` へ渡すため、

* **追加のときに選べない。** 区間ごとに「ここは無音カットしたくない」ができない。
* **保存したプロジェクトを開き直した場合、値の出どころが変わる。**
  `archive/project_resume.py:321` は `settings["archive"]["clip_pipeline"]` から
  `clip_settings` を組むため、**作ったときの選択ではなく `setting.json` の値** (既定 `true`) に従う。
  画面にその表示も無いので、利用者から見ると「勝手に切られた / 切られなかった」になる。

設計は `docs/request/ver13/resolve1.md`。
