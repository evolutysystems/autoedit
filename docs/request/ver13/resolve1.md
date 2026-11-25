# ver13 resolve1 — セクション追加の無音カット選択 設計

要望: `docs/request/ver13/request1.md`

本書は `docs/CLAUDE.md`「実装前に設計を行うこと」に従う **設計書** である。

---

## 1. 結論

セクション追加ダイアログ (`gui/timeline/section_add_dialog.py`) に
**「無音カット」のチェックボックスを 1 つ足し、その区間だけの上書きとして渡す。**

* 既定値は**その画面が今使っている値** (`clip_settings["silence_cut"]["enabled"]`)。
  設定画面にも `setting.json` にも新しいキーを足さない (`docs/CLAUDE.md` ハードコード禁止・既存キー優先)。
* 選択は `setting.json` へ書き戻さない。アーカイブタブの無音カットチェックと同じ流儀
  (`archive_tab._apply_silence_cut_option` のコメント「setting.json へは書き戻さない」)。
* 画面が持つ `clip_settings` は**書き換えない**。区間ごとに使う設定のコピーを作る。

## 2. 現状の経路

```
_on_add_section
  → SectionAddDialog (開始/終了だけを聞く)
  → _plan_add (重なりの解決・統合の予告)
  → jobs = [{index, start, end, score}, …]
  → _SectionPrepareWorker(… self._clip_settings …)
      → clip_writer.prepare_one_clip(source, settings, clip_settings, job, …)
          → _prepare_edit_points(normalized, clip_settings, …)
              → silence_cutter.detect_edit_points(ctx)   ← ctx.settings = clip_settings
                   silence_cut.enabled = false なら「全長 1 区間」を返す = 切らない
```

**切るか切らないかの分かれ目は `clip_settings["silence_cut"]["enabled"]` の 1 か所**である
(`clip_writer.build_clip_settings:191` が `archive.clip_pipeline.silence_cut` から入れる)。
ここだけを区間ごとに差し替えればよく、`silence_cutter` には触らない。

## 3. 変更の設計

### R1. ダイアログにチェックボックスを足す

`SectionAddDialog.__init__` に引数 `silence_cut_default` (既定 `True`) を足し、
「終了」の下、案内文の上へチェックボックスを置く。

```
開始  [ 1:16:30 ]
終了  [ 1:18:00 ]
[x] 無音カット
長さ 0:01:30
```

* ラベルはアーカイブタブと同じ `tr("無音カット")` にする (同じものを別の言葉で呼ばない)。
* ツールチップは「この区間だけ無音カットの有無を決めます。追加後も Timeline で戻せます」。
  実際に戻せる (編集点方式であり、素材は無音カット前の `normalized_path` を参照している)。
* 入力検証には関わらないので `_on_changed` には繋がない。
* 取り出し口は `silence_cut_enabled()`。`selected_range()` と同じ形にそろえる。

### R2. 区間ごとの上書きとして運ぶ

* `_on_add_section` がチェックの値を読み、各 job へ `"silence_cut": bool` を入れる。
  job は既に `index` / `start` / `end` / `score` を持つ辞書であり、入れ物は増やさない。
* `_SectionPrepareWorker` は job ごとに使う `clip_settings` を選ぶ。
  値は 2 通りしか無いので、**真偽値をキーにしたコピーのキャッシュ**を 1 つ持つ
  (`copy.deepcopy` を区間の数だけ走らせない)。
* 画面の `self._clip_settings` は変えない。変えると次回の追加・プレビュー・保存へ漏れる。

```python
# job の指定で無音カットの有無だけを差し替えた clip_settings を返す。
# 画面が持つ設定は書き換えない (次の追加へ漏らさない)。値は 2 通りなので覚えておく。
def _clip_settings_for(self, job):
    want = job.get("silence_cut")
    if want is None:
        return self._clip_settings
    want = bool(want)
    if want == bool(self._clip_settings.get("silence_cut", {}).get("enabled", True)):
        return self._clip_settings
    cached = self._settings_variants.get(want)
    if cached is None:
        cached = copy.deepcopy(self._clip_settings)
        cached.setdefault("silence_cut", {})["enabled"] = want
        self._settings_variants[want] = cached
    return cached
```

### R3. ログへ残す

区間の準備ログへ「無音カット: ON/OFF」を出す。
2026-11-25 の調査では「無音カットが ON だったのか OFF だったのか」をログから
判定するのに `編集点検出` 行の有無で逆算する必要があった。選べるようにするなら、
**選んだ結果をログに残す**こと。

```
セクション準備開始: 2 区間 / 合計 180.0s / 無音カット ON
```

### R4. 採らなかった案

| 案 | 却下理由 |
|---|---|
| 設定画面へ項目を足す | 要望は「追加のときに判断したい」。設定は実行ごとに開き直すものではない |
| 画面の `clip_settings` を直接書き換える | 次の追加・保存・プレビューへ漏れる。1 区間の指定が画面全体の状態になる |
| `silence_cutter.detect_edit_points` に引数を足す | 1 本編集・従来画面も同じ関数を通る。影響範囲が要望に対して広すぎる |
| プロジェクトへ「この区間は無音カットしない」を保存する | 編集点方式では**結果 (`keep_segments`) が既に保存されている**。指定を別に持つと二重管理になる |
| 追加後に Timeline 上で切り直せるようにする | 既にできる (編集点は画面で戻せる)。要望は「追加の時点で決めたい」である |

## 4. 変更するファイル

| ファイル | 変更 |
|---|---|
| `src/gui/timeline/section_add_dialog.py` | `silence_cut_default` 引数・チェックボックス・`silence_cut_enabled()` |
| `src/gui/timeline/archive_timeline_dialog.py` | ダイアログへ既定値を渡す / job へ `silence_cut` を入れる / ワーカーが区間ごとに設定を選ぶ / ログ |
| `src/i18n/catalog_en.py` | 新しい文言の英訳 (`tests/test_i18n.py::test_catalog_covers_the_source` が未登録を落とす) |
| `tests/test_ver13_section_silence_cut.py` | **新規**。下記 |

設定の追加は無い。

## 5. テスト

| # | 確認 |
|---|---|
| T1 | `SectionAddDialog` の既定が `silence_cut_default` に従う (ON / OFF の両方) |
| T2 | チェックを外すと `silence_cut_enabled()` が `False` を返す |
| T3 | `silence_cut_enabled()` が入力検証に影響しない (「追加」の可否が変わらない) |
| T4 | ワーカーが `job["silence_cut"]=False` のとき `silence_cut.enabled=False` の設定で `prepare_one_clip` を呼ぶ |
| T5 | `job["silence_cut"]=True` (画面の値と同じ) のときは**同一オブジェクト**を渡す (無駄なコピーをしない) |
| T6 | 区間が複数あり指定が混在しても、それぞれ正しい設定で呼ばれる |
| T7 | ワーカーが画面の `clip_settings` を書き換えない |
| T8 | `job` に `silence_cut` が無い (従来の呼び出し) ときは画面の値をそのまま使う (回帰) |
