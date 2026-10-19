# ver8 resolve4 — プレビューと Timeline の分割位置を覚える

対象: `autoedit` (Stretheus クライアント)
関連: `docs/request/ver8/resolve3.md` (画面の大きさ・位置の記憶 / §6 の残件)

作成日: 2026-10-19

---

## 0. 要望と結論

> プレビュー画面と Timeline のスプリッタも毎回直しているので、ここも同じく保存して
> 再度開く際は同じように開くようにしてほしい。

**実装した。** resolve3 で残件としていた分割位置の記憶 (resolve3 §6) を、
画面の大きさ・位置と同じ枠組みで足した。

| # | 分割 | 記憶の名前 | 従来 |
| --- | --- | --- | --- |
| 1 | プレビュー＋インスペクタ / Timeline (上下) | `"main"` | 毎回 `timeline.ui.split_ratio` から組み直し |
| 2 | プレビュー / インスペクタ (左右) | `"upper"` | 毎回 4 : 1 で組み直し |

要望は 1 だが、2 も同じ「毎回直す」対象 (インスペクタの幅) で、同じ 1 つの
仕組みで覚えられるため併せて対応した。

### 0.1 実装状況 (2026-10-19)

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | 復元 (`_restore_splitters`) と保存 (`_collect_splitters`) | 完了 |
| 2 | 上段スプリッタの保持 (`self._upper_splitter`) | 完了 |
| 3 | 設定キー 1 件を追加 (`setting.json` / `settings_window.py` / `builder.py`) | 完了 |
| 4 | `tests/test_timeline_window_geometry.py` へ 8 件を追記 (計 23 件) | 完了 |

---

## 2. 方針

- 記憶は `QSplitter.saveState()` / `restoreState()` に任せる
  (大きさの記憶が `saveGeometry()` なのと揃える)。控えは base64 で `setting.json` へ。
- **窓の大きさを先に復元してあるため** (resolve3 §3-1)、控えたピクセル幅が
  そのまま同じ見た目になる。先に窓を既定サイズで開いてから分割を戻すと、
  後の窓リサイズで比率が引き伸ばされてずれる。
- 覚えるかどうかは `remember_window_geometry` と**同じスイッチ**で決める。
  利用者から見れば「画面のレイアウトを覚える」1 つの機能のため。
- 画面の種別 (`"clip"` / `"archive"`) ごとに別で覚える (大きさと同じ理由)。
- 読めない控えは捨てて `split_ratio` から組んだ既定のままにする。

---

## 3. 変更点

### 3-1. 復元

`src/gui/timeline/timeline_editor_dialog.py` `_build_ui()`

上下スプリッタの既定 (`split_ratio`) を組んだ直後に `_restore_splitters()` を呼ぶ。
控えが無ければ何もしないため、初回は従来とまったく同じ見た目で開く。

```python
self._splitter.setStretchFactor(0, max(int(ratio * 100), 1))
self._splitter.setStretchFactor(1, max(int((1 - ratio) * 100), 1))
self._restore_splitters(ui_cfg)
```

左右スプリッタは `upper` のまま捨てていたので `self._upper_splitter` として持つ。

| メソッド | 役割 |
| --- | --- |
| `_splitters()` | 覚える分割の表 (`{"main": …, "upper": …}`) |
| `_restore_splitters(ui_cfg)` | 控えを 1 つずつ復元する (読めないものは飛ばす) |
| `_collect_splitters(store)` | `saveState()` を base64 で控える。前回と同じなら書かない |

### 3-2. 保存

resolve3 §3-3 でまとめた `_save_window_state()` に 1 行足すだけで済む。
画面の大きさ・分割位置・ドック配置で変化があったときに `save_settings()` が
**1 回**呼ばれる。

```python
changed = self._collect_geometry(store)
if self._collect_splitters(store):
    changed = True
if self._collect_window_state(store):
    changed = True
```

---

## 4. 設定 (setting.json)

```jsonc
"timeline": {
  "ui": {
    // 分割位置の控え (画面の種別 → "main"/"upper" → saveState() の base64)
    "splitter_state": {},
    // 覚えるかどうかは大きさ・位置と同じこのスイッチで決める
    "remember_window_geometry": true
  }
}
```

`split_ratio` は「記憶が無いときの比率」として引き続き使う。既存の設定ファイルに
`splitter_state` が無くても `builder.timeline_config()` が既定で補完する。

---

## 5. 変更ファイル

| ファイル | 内容 |
| --- | --- |
| `src/gui/timeline/timeline_editor_dialog.py` | 分割位置の復元・保存 / 上段スプリッタの保持 |
| `src/timeline/builder.py` | `splitter_state` の検証 (`_splitter_store`) |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ 1 件 |
| `src/settings/setting.json` | 既定値 1 件 |
| `tests/test_timeline_window_geometry.py` | `SplitterMemoryTest` 6 件 + 設定 2 件 |

---

## 8. テスト

`tests/test_timeline_window_geometry.py` (offscreen の Qt / 追記ぶん 8 件、計 23 件)

| # | 検査 |
| --- | --- |
| 1 | 上下の分割位置を控え、次に開くと戻すこと |
| 2 | 左右 (プレビュー / インスペクタ) も覚えること |
| 3 | クリップ用とアーカイブ用を別に覚えること |
| 4 | 読めない控えは無視して既定の比率のままにすること |
| 5 | `remember_window_geometry=false` で読まず書かないこと |
| 6 | 分割位置が変わっていなければ書き直さないこと |
| 7 | `splitter_state` の既定が空の辞書であること |
| 8 | 壊れた `splitter_state` (辞書でない・空文字・文字列でない) を捨てること |
