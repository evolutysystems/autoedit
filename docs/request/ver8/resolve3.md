# ver8 resolve3 — Timeline 画面の大きさ・位置・モニターを覚える

対象: `autoedit` (Stretheus クライアント)
関連: `docs/request/ver3/resolve5.md` §3-7 / §7 (ドック配置の記憶) / `docs/request/ver8/resolve2.md`

作成日: 2026-10-19

---

## 0. 要望と結論

> 毎回 Timeline 画面で画面サイズの大きさを変更している。
> 画面やドックの大きさ、マルチディスプレイの場合は表示していたモニターを、
> Timeline 画面を閉じるときに保存し、再度開いた際に同じ大きさで開くことは可能か。

**可能。実装した。**

| # | 対象 | 対応 |
| --- | --- | --- |
| 画面の大きさ | 閉じるときに控え、次に開くとき復元する (§3-1) |
| 画面の位置 | 同上 (左上の座標まで戻る) |
| 表示していたモニター | 同上。`saveGeometry()` の控えにモニターの識別が入るため、開いていた側のモニターへ戻る (§3-1) |
| 最大化していたか | 同上 (最大化で閉じれば最大化で開く) |
| ドックの大きさ・配置 | **従来から保存済み** (`archive_dock_state` / ver3 resolve5 §7)。今回は書き込みを画面の大きさと 1 回の保存へまとめた (§3-3) |
| モニター構成が変わった場合 | 画面外になる位置は主モニターの中央へ戻す (§3-2) |

### 0.1 実装状況 (2026-10-19)

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | 復元 (`_restore_geometry`) と保存 (`_collect_geometry`) | 完了 |
| 2 | 画面外へ開かないための引き戻し (`_pull_into_screen`) | 完了 |
| 3 | 大きさとドック配置を 1 回の保存へまとめる (`_save_window_state`) | 完了 |
| 4 | 設定キー 3 件を追加 (`setting.json` / `settings_window.py` / `builder.py`) | 完了 |
| 5 | `tests/test_timeline_window_geometry.py` (15 件) | 完了 |

---

## 1. 現状分析

`TimelineEditorDialog.__init__()` は毎回 `setting.json` の既定サイズで開いていた。

```python
self.resize(ui_cfg["window_width"], ui_cfg["window_height"])
```

大きさも位置も控えていないため、利用者が広げても次に開くと既定へ戻っていた。
一方、アーカイブ用画面の採点グラフ (ドック) だけは `QMainWindow.saveState()` の
控えを `timeline.ui.archive_dock_state` へ覚えており、辺・幅・高さは復元できていた
(ver3 resolve5 §7)。つまり**ドックは覚えるのに窓は覚えない**状態だった。

---

## 2. 方針

- 記憶は Qt の `saveGeometry()` / `restoreGeometry()` に任せる。
  大きさ・位置・最大化・**どのモニターに居たか**がこの 1 つの控えに入るため、
  モニターごとの座標計算を自分で持たない (ずれの元を作らない)。
- 控えは base64 文字列にして `setting.json` へ入れる (`archive_dock_state` と同じ流儀)。
- **画面の種別ごとに別で覚える**。クリップ用とアーカイブ用では要る広さが違う
  (アーカイブ用は採点グラフのドックを載せる)。
- 記憶が読めない・復元できない・今のモニター構成では画面外になる、のどの場合も
  **既定サイズ / 主モニターへ落とす**。窓を掴めず編集できない状態を作らない。

---

## 3. 変更点

### 3-1. 復元と保存

`src/gui/timeline/timeline_editor_dialog.py`

| メソッド | 役割 |
| --- | --- |
| `_geometry_key()` | 記憶を分ける鍵。`_project_kind()` (`"clip"` / `"archive"`) をそのまま使う |
| `_restore_geometry(ui_cfg)` | 既定サイズを置いてから控えを復元する。`__init__` の `resize()` を置き換えた |
| `_collect_geometry(store)` | `saveGeometry()` を base64 で控える。前回と同じなら書かない |

```python
state = str((ui_cfg["window_geometry"] or {}).get(self._geometry_key(), "") or "")
...
if not self.restoreGeometry(raw):
    _logger.info("画面の大きさを復元できなかったため既定で開きます")
    self.resize(ui_cfg["window_width"], ui_cfg["window_height"])
```

復元は `_build_ui()` より前に行う。ドック配置の復元 (`restoreState()`) は画面の
構築中に走るため、**窓の大きさが決まった後にドックを配分する**順序になり、
覚えたドックの大きさがそのまま出る。

### 3-2. 画面外へ開かないための引き戻し

外部モニターを外した・解像度を変えた後に前回の座標へ開くと、窓が画面の外に出て
掴めなくなる。復元後に、今あるモニターのどれかと**十分に重なっているか**を見て、
重なっていなければ主モニターの中央へ戻す (必要なら主モニターに収まるまで縮める)。

```python
for screen in QGuiApplication.screens():
    shown = screen.availableGeometry().intersected(frame)
    if shown.width() >= margin and shown.height() >= margin:
        return
```

「十分」の閾値は `timeline.ui.window_min_visible_px` (既定 120px)。端が数 px
かかっているだけでは掴めないため、面の見え方で判定する。

### 3-3. 保存は 1 回にまとめる

閉じるときに書くものが「画面の大きさ」と「ドック配置」の 2 つになったため、
`setting.json` を 2 度書かないよう 1 回へまとめた。

- 基底の `done()` が `_save_window_state()` を呼ぶ (窓を閉じる前に控える)。
- `_save_window_state()` は `timeline.ui` を集めて渡し、変化があったときだけ
  `save_settings()` を 1 回呼ぶ。
- 派生画面は `_collect_window_state(store)` フックで相乗りする。
  アーカイブ用の `_save_dock_state()` はこのフックへ移した
  (`ArchiveTimelineDialog.done()` の上書きは不要になり削除)。

記憶に失敗しても画面は閉じる (`_save_window_state` が例外を飲んでログに残す)。

---

## 4. 設定 (setting.json)

```jsonc
"timeline": {
  "ui": {
    // 画面の大きさ・位置・モニターを覚える (false で毎回 window_width/height)
    "remember_window_geometry": true,
    // 画面の種別ごとの控え ("clip" / "archive" → saveGeometry() の base64)
    "window_geometry": {},
    // 復元した窓がこれ未満しか見えないときは主モニターへ戻す
    "window_min_visible_px": 120
  }
}
```

既存の設定ファイルに 3 件が無くても `builder.timeline_config()` が既定で補完する
ため、更新前の `setting.json` のままでも動く (互換維持)。`window_width` /
`window_height` は「記憶が無いときの大きさ」として引き続き使う。

---

## 5. 変更ファイル

| ファイル | 内容 |
| --- | --- |
| `src/gui/timeline/timeline_editor_dialog.py` | 復元・保存・引き戻し (§3-1 / §3-2) / 保存のまとめとフック (§3-3) |
| `src/gui/timeline/archive_timeline_dialog.py` | ドック配置の保存をフックへ移設 (`done()` の上書きを削除) |
| `src/timeline/builder.py` | 設定 3 件の既定補完・検証 (`_geometry_store`) |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ 3 件 |
| `src/settings/setting.json` | 既定値 3 件 |
| `tests/test_timeline_window_geometry.py` | 新規 (15 件) |

---

## 6. 限界 (今回やっていないこと)

- **プレビューと Timeline の分割比 (スプリッタ)** は従来どおり
  `timeline.ui.split_ratio` から毎回組む。要望は窓とドックだったため触っていない。
  覚えるなら `QSplitter.saveState()` を同じ枠組み (`_collect_window_state` と
  `window_geometry` と同じ流儀) で足せる。
  → **ver8 resolve4 で対応した** (`docs/request/ver8/resolve4.md`)。
- メイン画面・設定画面など Timeline 以外の窓は対象外。

---

## 8. テスト

`tests/test_timeline_window_geometry.py` (offscreen の Qt で実行 / 15 件)

| # | 検査 |
| --- | --- |
| 1 | 閉じたときの大きさを控え、次に開くと復元すること (§3-1) |
| 2 | 記憶が無ければ既定サイズで開くこと |
| 3 | クリップ用とアーカイブ用を別に覚えること (§3-1) |
| 4 | base64 として読めない控えは既定へ落ちること |
| 5 | base64 だが中身が壊れた控えも既定へ落ちること |
| 6 | `remember_window_geometry=false` で読まず書かないこと |
| 7 | 大きさが変わっていなければ書き直さないこと |
| 8 | 派生画面ぶん (ドック配置) も 1 回の保存に相乗りすること (§3-3) |
| 9 | 保存に失敗しても例外を投げないこと |
| 10 | 画面外の位置は主モニターの中へ戻すこと (§3-2) |
| 11 | 画面内の位置は動かさないこと |
| 12 | モニターより大きい窓は収まるまで縮めること (§3-2) |
| 13 | 設定 3 件の既定値 |
| 14 | 壊れた `window_geometry` を捨てること |
| 15 | `window_min_visible_px` が正の整数へ寄ること |

復元の検査はモニターの大きさに依存する (`restoreGeometry()` は画面内へ丸める)
ため、期待値は実行環境の `availableGeometry()` から求めている。
