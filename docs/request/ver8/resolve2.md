# ver8 resolve2 — 採点グラフ (採点結果タブ) の出し直しと 1 クリック切替

対象: `autoedit` (Stretheus クライアント)
関連: `docs/request/ver3/resolve5.md` §3-7 / §7 (採点グラフのドック化)

作成日: 2026-10-19

---

## 0. 要望と結論

> アーカイブ切り抜き後の Timeline 画面では上部に採点結果タブが出るが、
> このタブを誤って削除 (× で閉じる) してしまうと、アプリを再起動しても
> 出てこなかった。これを再度表示する設定が設定画面にあるか。
> 無い場合は、採点を使ったアーカイブ切り抜きでは必ず最初に表示する。
> また、このタブを 1 クリックで表示・非表示に切り替えられるか。

| # | 回答 |
| --- | --- |
| 設定画面に再表示の設定はあるか | **無い**。ドックの配置は `timeline.ui.archive_dock_state` へ自動で覚えるだけで、設定画面の項目にはしていなかった。 |
| 閉じたまま覚えてしまうか | **覚えていた**。`QMainWindow.saveState()` はドックの**表示 / 非表示も含めて**保存するため、閉じた状態が次回以降も復元されていた。しかも出し直す口が画面のどこにも無かった。 |
| 必ず最初に表示できるか | した (§3-1)。配置 (どの辺・幅・高さ) は従来どおり覚え、**可視状態だけ**は毎回「表示」に戻す。 |
| 1 クリックで切り替えられるか | した (§3-2)。クリップバー右端の「採点グラフ」トグルボタン 1 クリック、またはショートカット **G** 1 打鍵 (§3-3)。 |

### 0.1 実装状況 (2026-10-19)

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | 配置の復元後に採点グラフを必ず表示 (`archive_timeline_dialog._wrap_content`) | 完了 |
| 2 | クリップバーへ表示切替トグル (`_build_clip_bar` / `_bind_graph_toggle`) | 完了 |
| 3 | 表示切替ショートカット (基底の `_register_extra_shortcuts` フック) | 完了 |
| 4 | 設定キー 2 件を追加 (`setting.json` / `settings_window.py` / `builder.py`) | 完了 |
| 5 | 英語の対訳 1 件 (`src/i18n/catalog_en.py`) | 完了 |
| 6 | `tests/test_archive_score_graph_toggle.py` (9 件) | 完了 |

---

## 1. 現状分析

採点グラフは `ArchiveTimelineDialog._wrap_content()` が `QMainWindow` へ
`QDockWidget` として載せている (ver3 resolve5 §3-7)。利用者が辺を動かせるよう
配置を `saveState()` で base64 にして `timeline.ui.archive_dock_state` へ記憶し、
次回の起動時に `restoreState()` で戻している。

`saveState()` の記憶には**ドックの可視状態も入る**。そのため × で閉じた直後に
画面を閉じると「非表示」が記憶され、以降は毎回閉じた状態で復元されていた。
ドックを出し直す手段 (メニューバー・`toggleViewAction` を出す口) は無かったため、
利用者からは「消したら二度と出てこない」ように見えていた。

---

## 2. 方針

- 配置の記憶はそのまま使う (辺・幅・高さ・切り離しは覚え続ける)。
- **可視状態は記憶に従わせない**。採点結果の確認が主目的の画面なので、
  開いたときは必ず見える状態から始める。
- 閉じられること自体は残す (Timeline を広く使いたいときに畳める)。
  代わりに**常に見える場所へ出し直す口**を置き、1 クリック / 1 打鍵で戻す。

---

## 3. 変更点

### 3-1. 開いたときは必ず表示する

`src/gui/timeline/archive_timeline_dialog.py` `_wrap_content()`

```python
self._restore_dock_state(ui_cfg)
if bool(ui_cfg["archive_graph_dock_always_show"]):
    self.graph_dock.show()
```

`restoreState()` の直後に `show()` を呼ぶ。配置の復元結果 (辺・サイズ) は
保たれたまま、可視状態だけが「表示」へ戻る。

閉じたままを覚えたい利用者向けに `timeline.ui.archive_graph_dock_always_show`
(既定 `true`) で切れるようにした (ハードコードの禁止 / `docs/CLAUDE.md`)。

### 3-2. クリップバーのトグルボタン (1 クリック)

クリップバー (クリップ選択・使用可否・テーマの行) の右端へ、チェック可能な
「採点グラフ」ボタンを置いた。クリップバーは中央側に固定されており、
ドックを閉じても必ず見える (ver3 resolve5 §5.5)。

押下状態は `QDockWidget.toggleViewAction()` と双方向に結線する。
これにより **× で閉じた・ショートカットで消した・配置を復元した**の
どの経路でもボタンの表示が実際の状態と一致する。
`setChecked()` / `setVisible()` は値が変わるときだけ通知するため循環しない。

### 3-3. ショートカット (1 打鍵)

キー割り当ては `timeline.shortcuts.toggle_score_graph` (既定 `G`)。
既存の割り当て (W / A / D / E / Q / Space / Ctrl 系) と衝突しない。

基底 `TimelineEditorDialog` へ派生画面用のフックを 1 つ足した。

```python
# 派生画面だけで使うショートカットを足すフック
def _register_extra_shortcuts(self, bind):
    return None
```

`_register_shortcuts()` の `_ShortcutGuard` 生成前に呼ぶため、
文字入力欄 (テーマ名・字幕) にフォーカスがある間は自動で無効化される
(ver3 resolve2 回答 Q6 と同じ扱い)。クリップ用の画面の挙動は変わらない。

---

## 4. 設定 (setting.json)

```jsonc
"timeline": {
  "ui": {
    // 記憶した配置が「閉じた」状態でも採点グラフを出し直す (false で従来どおり)
    "archive_graph_dock_always_show": true
  },
  "shortcuts": {
    // 採点グラフの表示切替
    "toggle_score_graph": "G"
  }
}
```

既存の設定ファイルに両キーが無くても `builder.timeline_config()` が既定で
補完するため、更新前の `setting.json` のままでも動く (互換維持)。
`archive_dock_state` の形式は変えていない。

---

## 5. 変更ファイル

| ファイル | 内容 |
| --- | --- |
| `src/gui/timeline/archive_timeline_dialog.py` | 必ず表示 (§3-1) / トグルの設置と結線 (§3-2) / 切替キー (§3-3) |
| `src/gui/timeline/timeline_editor_dialog.py` | `_register_extra_shortcuts()` フック (既定は何もしない) |
| `src/timeline/builder.py` | 設定 2 件の既定補完・検証 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ 2 件 |
| `src/settings/setting.json` | 既定値 2 件 |
| `src/i18n/catalog_en.py` | 「採点グラフの表示を切り替えます」の対訳 |
| `tests/test_archive_score_graph_toggle.py` | 新規 (9 件) |

---

## 8. テスト

`tests/test_archive_score_graph_toggle.py` (offscreen の Qt で実行)

| # | 検査 |
| --- | --- |
| 1 | ボタン 1 クリックでドックが消え、もう 1 クリックで戻る (§3-2) |
| 2 | ドックを × で閉じるとボタンの押下が外れ、ボタンから復帰できる (§3-2) |
| 3 | 結線時に閉じていれば、ボタンも押されていない状態から始まる |
| 4 | ショートカットで表示とボタンの両方が入れ替わる (§3-3) |
| 5 | フックが `toggle_score_graph` を登録する (§3-3) |
| 6 | ツールチップにキー割り当てを出す (未割り当てなら文言だけ) |
| 7 | `archive_graph_dock_always_show` の既定が `true`、`toggle_score_graph` の既定が `G` (§3-1) |
| 8 | `archive_graph_dock_always_show` を `false` にできる |
| 9 | キー割り当てを差し替えられる |
