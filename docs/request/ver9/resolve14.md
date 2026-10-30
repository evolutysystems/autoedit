# ver9 resolve14 — 元 VOD を手放すと「縦動画プロジェクト作成」が使えない — 素材を複製して自己完結させる

発端: 2026-10-29 のチャット報告「アーカイブ用切り抜きの Timeline 画面から、新規縦動画プロジェクト作成ができなくなっている」
対象: `src/gui/timeline/archive_timeline_dialog.py` / `vertical_project_dialog.py` / `src/timeline/vertical_builder.py` / `src/timeline/crop.py`
調査日: 2026-10-29 (main `4755926` / v1.7.0 + resolve12・resolve13 の変更)
種別: **修正設計書**。§1 は現行コードと実データで確認済み
前提: ver5 resolve10 (縦動画プロジェクト) / docs/error/fromUser/resolve.md §6-6 (元 VOD の整理) / ver9 resolve12 (同じ根の不具合)

---

## 0. 先に結論

### 0.1 3 行で

1. **メニューが出ない条件は「元 VOD が実在しないこと」。**
   `ArchiveTimelineDialog._can_make_vertical()` が `os.path.exists(vod)` を要求しており、
   偽だと右クリックメニューに項目そのものを足さない。
2. **元 VOD は既定で自動整理される。** `keep_vod=false` のまま保存して開き直すと、
   アーカイブタブを開いた時点でゴミ箱へ送られる。**保存 → 再起動でこの機能だけ消える。**
   ver9 resolve12 (イントロカード) と同じ根で、縦動画はその対応から漏れていた。
3. **元 VOD を要求していた前提は、もう成り立っていない。** 要求の理由は
   「アーカイブ用 Timeline が指す中間ファイルが実行の終わりに消えるから」だったが、
   `archive_keep_media=true` により保存済みプロジェクトの素材は `.media/` に残る。
   **素材を縦プロジェクトの隣へ複製すれば、元 VOD も元プロジェクトも要らなくなる** (利用者の指示)。

### 0.2 何が変わるか

| | 現在 (v1.7.0) | 変更後 |
| --- | --- | --- |
| 元 VOD がある | 作れる (元 VOD へ張り替え) | 作れる (**素材を複製**) |
| 元 VOD が無い (保存済み) | **メニューが出ない** | 作れる (素材を複製) |
| 元 VOD が無い (未保存) | メニューが出ない | 作れる (素材を複製) |
| 作った縦プロジェクトの依存 | 元 VOD が消えると開けない | **何にも依存しない** |

---

## 1. 原因 (調査結果)

### 1.1 メニューを出さない判定

```python
# src/gui/timeline/archive_timeline_dialog.py:181
def _can_make_vertical(self):
    if not super()._can_make_vertical():
        return False
    vod = project_resume.recorded_vod_path(self.controller.timeline)
    return bool(vod) and os.path.exists(vod)      # ← ここで落ちる
```

偽のとき `timeline_view._add_vertical_actions()` は何も足さずに戻るため、
**項目が消え、理由も出ない**。

### 1.2 元 VOD は自動で片づけられる

`archive_tab._sweep_old_sources()` → `vod_retention.sweep_unneeded()` が、
アーカイブタブを開いた時点で「自己完結したプロジェクトの元 VOD」をゴミ箱へ送る
(resolve.md §6-6 / 既定 `keep_vod=false`)。

報告のプロジェクト `vod_2912229276` でも、`archive_work` に元 VOD は無く
ゴミ箱にある (ver9 resolve12 §1.3 で確認済み)。

### 1.3 元 VOD を要求していた理由と、その前提の崩れ

ver5 resolve10 の設計は次のとおりだった。

> アーカイブ用 Timeline が参照しているのは「実行の終わりに消えるクリップ単位の
> 中間ファイル」で、それを指したまま縦プロジェクトを保存しても後から開けない。
> そこで縦プロジェクトを作るときだけ、素材を元 VOD 1 本へ張り替える。

ところが現在は `timeline.project.archive_keep_media=true` が既定で、
**保存済みプロジェクトの素材は `<プロジェクト>.media/` へ複製され、`media.path` もそこを指す**。
報告のプロジェクトにも `m1_normalized.mp4`〜`m16_normalized.mp4` が揃っている。
つまり「中間ファイルだから消える」という前提は、保存済みプロジェクトでは崩れている。

### 1.4 どちらに張り替えても依存は残る

| 方式 | 依存 | 消えると |
| --- | --- | --- |
| 元 VOD へ張り替え (現行) | 元 VOD | **自動整理で消える** → 開けない |
| `.media/` を指すだけ | 元のアーカイブプロジェクト | 元を消すと開けない |
| **素材を複製** (本設計) | なし | — |

### 1.5 テストが現仕様を固定している

`tests/test_vertical_dialog.py::test_archive_gate_needs_vod` が
「元 VOD が無ければメニューを出さない」を検査している。
当時は意図どおりだったが、元 VOD を手放す運用と噛み合わなくなった。本設計で更新する。

---

## 2. 設計方針

### 2.1 やること

1. **縦プロジェクトの素材を `<縦プロジェクト>.media/` へ複製する** (§3.1)。
   保存した時点で自己完結し、元 VOD も元プロジェクトも要らなくなる。
2. **元 VOD への張り替えをやめる** (§3.2)。既定を「複製」にし、設定で従来へ戻せるようにする。
3. **メニューの条件を「素材があるか」へ変える** (§3.3)。出せないときは**押せない項目として出し**、
   理由を伝える (黙って消えると今回のように原因が分からない)。

### 2.2 やらないこと

- **元 VOD の自動整理はそのまま。** 45GB 滞留を解消した方針を崩さない。
- **`vod_rebase` を削除しない。** 設定で選べる経路として残す (§3.2)。
  元 VOD が手元にあり、容量を使いたくない運用のため。
- **素材を切り詰めない。** 使う区間だけ切り出すと再エンコードか精度の問題が出る。
  アーカイブ素材はセクション単位 (ほぼ全部使う) なので、ファイル単位の複製で足りる。
- **オーバーレイ (D&D で足した画像・動画) は複製しない。** 縦プロジェクトへは
  そもそも運ばれない (`_warn_dropped` が警告する)。

---

## 3. 実装設計

### 3.1 素材を縦プロジェクトの隣へ複製する

`vertical_builder` に複製を足す。縦 Timeline の `media_pool` は
`build()` が `copy.deepcopy` で作っているため、**パスを書き換えても元 Timeline を壊さない**。

```python
# src/timeline/vertical_builder.py
# 縦プロジェクトが参照する素材を <プロジェクト>.media/ へ複製して自己完結させる
# (ver9 resolve14 §3.1)
#
# アーカイブ用の素材は、未保存なら一時ファイル、保存済みでも元プロジェクトの
# .media/ にある。どちらも「縦プロジェクトだけを残す」と開けなくなるため、
# 作成時にここへ写す。複製できなかったものは警告へ積み、作成そのものは続ける。
# 戻り値: (複製した件数, 複製したバイト数)
def copy_media_beside(timeline, project_path, settings, warnings=None):
```

- 複製先の名前は既存の作法に合わせ `<media_id>_<元のファイル名>`
  (`timeline_editor_dialog._copy_media_beside_project` と同じ)。
- 既に同じ大きさのファイルがあれば複製し直さない (作り直しで数百 MB を書かない)。
- 複製後に `media.path` を差し替え、`source["media_path"]` も合わせる。
- 失敗しても**縦プロジェクトの作成は続ける** (元のパスを指したまま保存し、警告を出す)。

呼ぶ場所は `VerticalProjectDialog._on_create()` の **`project_io.save()` の直前**
(保存する JSON に複製後のパスを載せるため)。待ちカーソルと進捗の文言を出す。

### 3.2 元 VOD への張り替えをやめる (設定で戻せる)

`ArchiveTimelineDialog._vertical_rebase()` は、設定が `copy` (既定) なら `None` を返す。

```python
def _vertical_rebase(self, clips):
    if crop.config(self._settings)["media_mode"] != crop.MEDIA_REBASE:
        return None            # 素材は複製して自己完結させる (resolve14 §3.1)
    return vod_rebase.plan(self.controller.timeline, self._settings, clips)
```

`rebase` が `None` のとき `vertical_builder.build()` は今の素材をそのまま指す
(既存の分岐。クリップ用プロジェクトと同じ扱いになる)。

### 3.3 メニューの条件と、出せないときの伝え方

```python
def _can_make_vertical(self):
    if not super()._can_make_vertical():
        return False
    if 複製モード:
        return True            # 素材はここにあるものを複製する
    vod = project_resume.recorded_vod_path(self.controller.timeline)
    return bool(vod) and os.path.exists(vod)
```

さらに `TimelineEditorDialog.vertical_menu_actions()` で、**対象クリップの素材が
1 つも見つからないとき**は無効な項目として出し、理由をラベルへ添える。

```
縦動画プロジェクトを作成...（素材が見つかりません）   ← 押せない状態
```

従来は条件を満たさないと項目ごと消えていたため、「前はあったのに」が分からなかった
(セクション追加で同じ失敗をして §4.6 で直した経緯がある / ver9 resolve5)。

### 3.4 設定

| キー | 既定 | 意味 |
| --- | --- | --- |
| `vertical.crop.media_mode` | `"copy"` | `copy` = 素材を複製して自己完結 / `rebase` = 元 VOD へ張り替え (従来) |
| `vertical.crop.keep_media` | `true` | `false` で複製しない (パスを指すだけ。容量を使いたくない場合) |

`keyframe_window_sec` (張り替えのキーフレーム探索幅) は `rebase` のときだけ使う値として残す。

---

## 4. 判断が要る点

**利用者の指示で確定済み**: 「素材を複製して自己完結」(§3.1 / §3.2 の既定 `copy`)。

残る小さな選択:

| 項目 | 決め |
| --- | --- |
| 複製する単位 | **ファイル単位** (区間の切り出しはしない / §2.2) |
| 複製の失敗 | 作成は続け、警告に出す (縦プロジェクト自体は作れる) |
| 容量の目安 | 対象クリップの素材ぶん。アーカイブ素材は 1 セクション数十〜百 MB |

---

## 5. 変更ファイル

| ファイル | 内容 |
| --- | --- |
| `src/timeline/vertical_builder.py` | `copy_media_beside()` を追加 |
| `src/timeline/crop.py` | `media_mode` / `keep_media` の読み出しと定数 |
| `src/gui/timeline/vertical_project_dialog.py` | 保存前に複製を呼ぶ・結果を案内へ出す |
| `src/gui/timeline/archive_timeline_dialog.py` | `_can_make_vertical` の条件・`_vertical_rebase` の出し分け |
| `src/gui/timeline/timeline_editor_dialog.py` | 素材が無いときは押せない項目として出す |
| `src/settings/setting.json` / `settings_window.py` | §3.4 の 2 キー |
| `src/i18n/catalog_en.py` | 新規文言の対訳 |
| `tests/test_vertical_dialog.py` | 現仕様 (VOD 必須) の検査を更新 |
| `tests/test_vertical_media_copy.py` | **新規**。複製と自己完結の検査 |

---

## 6. テスト計画

| 条件 | 期待 |
| --- | --- |
| 元 VOD が無い + 保存済み | メニューが出る / 作成できる |
| 元 VOD が無い + 未保存 (一時素材) | メニューが出る / 複製されるので後から開ける |
| 元 VOD がある | 既定では複製 (張り替えない) |
| `media_mode="rebase"` | 従来どおり張り替え、元 VOD が無ければ出さない |
| 複製後 | `media.path` が `<縦プロジェクト>.media/` を指す / 元 Timeline は変わらない |
| 2 回目の作成 | 同じ大きさのファイルは複製し直さない |
| 複製に失敗 | 例外にせず警告を積み、プロジェクトは保存される |
| `keep_media=false` | 複製しない (従来の参照のまま) |

手で確かめる: 元 VOD をゴミ箱へ送ったプロジェクトを開き、縦プロジェクトを作成 →
`<縦プロジェクト>.media/` に素材ができ、**元のアーカイブプロジェクトを消しても開ける**こと。

---

## 7. 互換性

| 対象 | 扱い |
| --- | --- |
| 既に作った縦プロジェクト | 変わらない (元 VOD / 元素材を指したまま) |
| `media_mode="rebase"` を選んだ運用 | 従来とまったく同じ |
| クリップ用からの縦動画作成 | 元動画は利用者のファイルで消えないため、複製対象から外す (従来どおり) |

---

## 8. 残課題

| 項目 | 状況 |
| --- | --- |
| 既に作った縦プロジェクトの救済 | 元 VOD を参照しているものは、元 VOD を戻さないと開けない。作り直しが要る |
| 複製の容量 | 対象クリップぶん。縦動画を何本も作ると積み上がるため、要望が出たら「区間だけ切り出す」案を検討 |

---

## 10. 実装結果 (2026-10-29)

利用者の指示どおり **「素材を複製して自己完結」** を既定 (`media_mode="copy"`) として実装した。

### 10.1 変更したファイル

| ファイル | 内容 |
| --- | --- |
| `src/timeline/vertical_builder.py` | `copy_media_beside()` を追加 (複製・参照の付け替え・警告) |
| `src/timeline/crop.py` | `MEDIA_COPY` / `MEDIA_REBASE` と `media_mode` / `keep_media` の読み出し |
| `src/gui/timeline/vertical_project_dialog.py` | 保存の直前に複製、複製しない素材 (`_keep_media_paths`) の指定 |
| `src/gui/timeline/archive_timeline_dialog.py` | 元 VOD の要求を撤去、`_vertical_blocked_reason()` と `_vertical_rebase()` の出し分け |
| `src/gui/timeline/timeline_editor_dialog.py` | 作れない理由があるときは**押せない項目として出す**、`_clip_media_exists()` |
| `src/settings/setting.json` / `settings_window.py` | `vertical.crop.media_mode` / `keep_media` |
| `src/i18n/catalog_en.py` | 新規文言 5 件の対訳 |
| `tests/test_vertical_media_copy.py` | **新規** 11 件 |
| `tests/test_vertical_dialog.py` | 「元 VOD が無ければ出さない」の検査を現仕様へ更新 (2 件) |

テストは **2035 件すべて成功** (追加前 2023 件 → 12 件追加)。

### 10.2 設計から変えた点

- **`_can_make_vertical()` を書き換えるのではなく、判定を 2 つに分けた。**
  〈この画面・Timeline が対象か〉= `_can_make_vertical()` (従来どおり) と、
  〈対象だが今は作れない理由〉= `_vertical_blocked_reason()` (新規)。
  前者が偽なら項目を出さず、後者に理由があれば**押せない項目として理由を添えて出す**。
  アーカイブ側の `_can_make_vertical()` オーバーライドは不要になったので削除した。
- **複製しない素材を `skip_paths` で渡す形にした。** 利用者の元動画 (クリップ用) と、
  `rebase` を選んだときの張り替え先 (元 VOD 10〜25GB) を複製しないため。
  設計では「オーバーレイは対象外」としか書いていなかった。

### 10.3 実データでの確認

報告のプロジェクト (`vod_2912229276` / 元 VOD はゴミ箱) で通しを確かめた:

| 項目 | 結果 |
| --- | --- |
| `media_mode` | `copy` |
| 縦動画の対象か | **True** (元 VOD が無くても対象) |
| 作れない理由 | なし |
| 張り替え指定 | `None` (複製するため張り替えない) |
| 複製 | 1 件 / 1.7MB |
| 保存された参照先 | `…\try_vertical.media\m13_m13_raw.mp4` (**縦プロジェクトの隣**・実在) |
| 開き直し | クリップ 1 本 / 尺 3.54 秒で読める |

元のアーカイブプロジェクトの `.media/` も元 VOD も参照していないため、
**縦プロジェクト単体で開ける**ことを確認した。

### 10.4 報告との対応

| 報告 | 結果 |
| --- | --- |
| 縦動画プロジェクト作成ができない | 元 VOD を要求しなくなったため**復活**。未保存のアーカイブ Timeline からも作れる |
| (併せて) 作った縦プロジェクトの寿命 | 素材を複製するため、元 VOD の自動整理にも元プロジェクトの削除にも影響されない |
| (併せて) 消えた理由が分からない | `rebase` を選んだ運用では、項目を消さずに「（元動画が見つかりません）」と添えて出す |
