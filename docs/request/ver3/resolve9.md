# resolve9（ver3） — アーカイブ用 Timeline の保存・再編集 / プロジェクト一覧画面（開く・リネーム・削除）修正設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request9.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/CLAUDE.md`「実装前に設計を行うこと」）。
* 本書は resolve7（クリップ用の保存・再編集）の続きにあたる。resolve7 §3-6 で
  **意図的に対象外**とした「アーカイブ切り抜き用画面の保存」を、resolve7 §11 の道筋に沿って実装する設計である。
* 後半（§3-8 以降 / §5.12 以降）は追加要望の**プロジェクト一覧画面**（V1 のサムネイル付き / 開く・リネーム・削除）。
  当初 §5.9 に置いていた「行の削除ボタン」は、一覧画面へ寄せる形へ改訂した（§3-11）。
* **2026-08-20 の回答（§9）を反映済み**。特に **Q1「開くのに作るのと同じくらい待つなら保存の意味が無い」**
  を受けて、素材の復旧方式を**全面的に作り直した**（§3-1: 音声サイドカー案 D。10 本で 3〜5 分 → 10〜30 秒）。
  ほかに削除のゴミ箱経由化（Q2 / §3-7）と、一覧をタブごとに分ける改訂（Q3 / §5.12）を入れた。
* 記載した実装位置はすべて本リポジトリの実コードを読んで確認した（2026-08-20 時点）。
  さらに §2.2 の「保存しても開けない」は **実際にコードを走らせて確認した実測値**であり、推測ではない。
  決めきれない点は推測実装せず **§9 確認事項** に列挙した。
* **setting.json は本書では書き換えない**。追加キーは `DEFAULT_SETTINGS`（`settings_window.py:282`）へ足し、
  `load_settings()` の既定マージで自動補完させる（resolve7 と同じ方針）。
* **実装済み（2026-08-20）**。§10 の Phase 1〜10 をすべて実装し、自動テスト 435 件（新規 42 件）が通っている。
  §3-1 案 D は実 ffmpeg で往復を検証した（復元後の**映像・音声とも MD5 が保存時と一致**）。
  実装中に分かった実測値は §3-1 / §5.11 へ反映済み。設計から変えた点は §12 に列挙した。

---

## 1. 要望（request9.md）と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **E1** | **アーカイブ用 Timeline を保存**できるようにする | 機能追加 |
| **E2** | 保存したものを開き直して**再編集 → 書き出しまで**到達させる | 機能追加 |
| **E3** | 保存済みプロジェクトを画面から**削除**できるようにする | 機能追加 |
| **E10** | 編集した Timeline を**一覧画面で表示**する。各行に **Timeline の V1 の一部分（フレーム）を出す** | 機能追加 |
| **E11** | 一覧から **開く・リネーム・削除**ができるようにする | 機能追加 |
| **E12** | そのために**必要であれば画面の大きさを変更**する | 制約（許可） |

要望文に無いが、実装に必ず要る論点:

| ID | 論点 | 理由 |
|---|---|---|
| **E4** | **素材（クリップごとの中間ファイル）の復旧** | 現状のまま保存しても、開いた瞬間 V1 が全滅する（§2.2 実測）。**本要望の最大の壁** |
| **E5** | **Timeline モデルの外にあるデータ**（テーマ・採点グラフ・クリップ区間）の保存 | 保存しても再現できないと「開けても同じ画面にならない」 |
| **E6** | クリップ用プロジェクトと**アーカイブ用プロジェクトの見分け・振り分け** | 開く経路（レンダラ / 書き出し方）が根本的に違う。取り違えると壊れる |
| **E7** | **保存先ファイル名の衝突** | 現状の既定名は「元動画名 + .timeline.json」。同じ VOD からクリップ用とアーカイブ用が同名になる |
| **E8** | 削除の**対象範囲**（本体・自動保存・複製素材フォルダ・履歴） | 本体だけ消すと自動保存や `.media` フォルダ（数百 MB）が孤児として残る |
| **E9** | 削除の**安全性**（取り消せない操作） | 誤操作で編集成果が消える。確認と対象の明示が要る |
| **E13** | **一覧に出す母集合**（履歴だけか、フォルダを走査するか） | 履歴（`recent`）は「明示保存したもの」しか積まれない。一覧が履歴だけだと**大半が出てこない** |
| **E14** | **サムネイルの作り方**（素材が消えている前提で V1 の絵をどう出すか） | E4 と同じ壁。保存済みプロジェクトの V1 素材は一時ファイルで、一覧を開く時点では存在しない |
| **E15** | **リネームの波及**（自動保存・`.media` フォルダ・JSON 内の素材パス・履歴） | ファイル名を変えるだけでは `.media` フォルダを見失い、**開けないプロジェクトになる** |
| **E16** | **一覧の応答性** | プロジェクト JSON は字幕を含むと数百 KB になる。全件を同期で読むと一覧が開かない |

---

## 2. 現状分析

### 2.1 アーカイブ用は保存 UI が「出ない」実装になっている（意図的）

`ArchiveTimelineDialog` は基底のフックを `False` で上書きしている。

```python
# src/gui/timeline/archive_timeline_dialog.py:112
def _project_save_enabled(self):
    return False
```

これが `timeline_editor_dialog.py:84` の `self._save_enabled` に効き、保存に関する機能が一括で無効になる。

| 機能 | 基底の位置 | アーカイブ用での現状 |
|---|---|---|
| 「保存」「名前を付けて保存...」ボタン | `timeline_editor_dialog.py:156` | 生成されない（`save_button = None`） |
| Ctrl+S / Ctrl+Shift+S | `:279-281` | ショートカット未登録 |
| 自動保存（`timeline.autosave_sec`） | `:469` | タイマーを起動しない |
| タイトルの未保存印 `*` | `:374` | 出さない |
| 閉じるときの 3 択（C5） | `:577` | 従来の 2 択「編集を破棄しますか」のまま |

つまり **不具合ではなく仕様**であり、E1 は「フックを `True` に戻す」だけでは成立しない（§2.2）。

### 2.2 【実測】保存を通しても、開き直すと V1 が全滅する

アーカイブ用と同じ形の Timeline（クリップ 2 本 / `source.archive` 付き / 素材は各クリップの
`normalized.mp4`）を組み、保存 → 素材削除 → 読み込み → 復旧 → 検証を実際に走らせた結果:

```
① 保存: OK (3905 bytes)
   source のキー: ['archive', 'duration_sec', 'input_path']
   archive セクション保持: True
   media_id (本編素材の印): '<無し>'
   media_role: '<無し>'
② 復旧: {'recovered': [], 'missing': ['m1', 'm2'], 'renormalized': False}
   V1 クリップ 生存 0 本 / 無効化 2 本
   保存されたテーマ: 無し
```

読み取れること:

1. **JSON 化そのものは成功する**。`source.archive`（採点・VOD 区間）も `origin.archive_clip_index` も往復する
   （`project_io.from_dict` は `source` をそのまま復元し、`Clip.from_dict` は `origin` を復元する）。
   したがって `split_by_clip()`（書き出し時のクリップ分割）は再読込後も機能する。
2. **素材が復旧できない**。`media_recovery.recover()` の「③ 本編素材の作り直し」は
   `source["media_id"]` に一致する 1 件しか対象にしない（`media_recovery.py:60`）。
   アーカイブ用 Timeline は `source` に `media_id` を入れていない（`archive/timeline_builder.py:124`）ため、
   全メディアが「④ 既知フォルダ探索」→「⑤ 再リンク」へ落ちる。
3. 結果、`project_io._validate_media()`（`project_io.py:329`）が該当クリップを `enabled=False` にし、
   **空の Timeline** になる。
4. **テーマが保存されない**。`_themes` は Timeline の外（ダイアログのメンバ）にあり
   （`archive_timeline_dialog.py:60` / `:192`）、JSON に含まれない。

### 2.3 素材はどこで消えるか（E4 の原因）

```
clip_writer.write_clips()                              (clip_writer.py:617)
└─ with tempfile.TemporaryDirectory(prefix="archive_clips_") as workdir:   (:639)
     └─ _prepare_clips()                               (:400)
          clip_dir = workdir/clip{index}               (:412)
          raw.mp4        ← _cut_region(VOD, start, end)         (:414 / 実体 :52)
          normalized.mp4 ← loudness_normalizer.normalize_file()  (:419)
                            ↑ これが Timeline の素材 (normalized_path)
```

`workdir` は `with` を抜けた時点で削除される。**アーカイブ用 Timeline が指す素材は必ず消える**。
クリップ用（`pipeline_runner`）も中間ファイルは消えるが、あちらは「元動画から作り直す」復旧経路がある
（resolve7 §3-5）。アーカイブ用にはそれが無い、というのが差の全て。

### 2.4 Timeline モデルの外にあるデータ（E5）

| データ | 現在の置き場 | 保存されるか |
|---|---|---|
| クリップ区間・採点（VOD 時間） | `timeline.source["archive"]["clips"]` | **される**（`timeline_builder.py:124`） |
| クリップ由来の印 | `clip.origin["archive_clip_index"]` | **される** |
| 使用可否 | `clip.enabled`（V1 クリップ） | **される** |
| **テーマ** | `ArchiveTimelineDialog._themes`（画面のメンバ） | されない |
| **採点グラフの窓スコア列（curve）** | `ArchiveTimelineDialog._curve`（画面のメンバ） | されない |

### 2.5 再編集の受け皿が無い（E2 / E6）

`main_window` の「編集の続き」は `run_from_project()`（`pipeline_runner.py:89`）へ入り、
**クリップ用** `TimelineEditorDialog` を開いて `renderer.render()` で 1 本の動画を出す。
アーカイブ用に必要な「クリップ単位の分割 → テーマ演出 → 個別出力 → 結合」
（`clip_writer._render_clips()` :550 以降）へは戻れない。

また `ProjectResumeWorker`（`main_window.py:408`）はプロジェクトの種別を見ずに `run_from_project` を呼ぶ。
アーカイブ用プロジェクトを選ぶと、**種別違いのまま処理が走る**（§3-4 で塞ぐ）。

### 2.6 保存済みプロジェクトを消す手段が無い（E3）

「編集の続き」の行にあるのは `QComboBox` と「開く...」だけ（`main_window.py:547-558`）。
削除は無い。付随して次も残り続ける。

| 残るもの | パス | 作られる条件 |
|---|---|---|
| プロジェクト本体 | `<出力先>/<元動画名>.timeline.json` | パイプライン実行のたび自動保存（`pipeline_runner.py:272`） |
| 自動保存 | `<同>.autosave.json`（`project_io.py:62`） | `timeline.autosave_sec > 0` |
| 複製素材フォルダ | `<同>.media/`（`project_io.py:72`） | `timeline.project.keep_media = true`。**動画 1 本ぶんのディスク** |
| 履歴 | `setting.json` の `timeline.project.recent` | 画面から明示保存したとき（`timeline_editor_dialog.py:510`） |

さらに、**一覧して見渡す手段も無い**（E10）。コンボに並ぶのは履歴だけで、しかも履歴は
「編集画面で明示的に保存したもの」しか積まれない（`_remember_recent()` は `save_project()` からのみ呼ばれる）。
一方で**プロジェクト本体はパイプライン実行のたびに自動保存されている**（`pipeline_runner.py:272`）。
つまり **出力フォルダには履歴に出てこないプロジェクトが溜まり続けている**。これが E13 の理由。

### 2.7 V1 の絵は「素材が消えていても」出せるのか（E14）

一覧にサムネイルを出すには V1 のフレームが要るが、§2.3 のとおり V1 素材は消えている。
実コードを確認したところ、次の 3 つが揃っているため**出せる**。

| 使えるもの | 位置 | 何に使えるか |
|---|---|---|
| `frame_source.create_frame_source(settings)` / `frame_at(media, source_sec)` | `timeline/frame_source.py:316` / `:37` | 任意のメディアの任意時刻を `(w, h, rgb24)` で取れる。PyAV が無い環境は ffmpeg へ自動フォールバック |
| RGB → `QPixmap` の変換 | `gui/timeline/preview_panel.py:471` と同じ 2 行 | そのまま一覧のアイコンに使える |
| 元動画のパス | `source.input_path`（クリップ用） / `source.archive.vod_path`（アーカイブ用） | **素材が消えていても元動画は残っている**ため、そこから同じ絵を取れる |

「元動画から取っても同じ絵になる」根拠は §3-1 と同じ。ラウドネス正規化は映像を `-c:v copy` で通すので
**時間軸が元動画と完全に一致する**（resolve7 §2.3）。したがって V1 クリップの `source_in` を
元動画の時刻としてそのまま使える。アーカイブ用は VOD 時刻 = `vod_start + source_in` で引ける
（`-c copy` の切り出しはキーフレーム境界へ丸まるため最大 1 GOP ぶんズレ得るが、サムネイルとしては無視できる）。

なお、実際にキャッシュを作る処理は `frame_source` ではなく **ffmpeg で JPEG を 1 枚書く**方式にする。
理由は §5.15（`FfmpegFrameSource` は raw バッファの大きさを決めるために `media.width/height` を要求するが、
元動画へフォールバックした場合その寸法は Timeline に記録されていないため）。

---

## 3. 設計方針

### 3-1【E4】素材は「音声だけ保存しておき、映像は VOD から切り直して合体させる」

> **回答 Q1 による改訂**（2026-08-20）。初版は「開くときに 2 パスの正規化をやり直す」案 B を採用していたが、
> 「**開くのに作るのと同じくらい待つなら保存の意味が無い**」との指摘を受け、案 D を新設して既定にした。

案を 4 つ比較した。

| 案 | 内容 | 保存時のディスク | 開くまで（クリップ 1 本 3 分・10 本の目安） | 判定 |
|---|---|---|---|---|
| A | 保存時に `normalized.mp4` を丸ごと複製 | **動画まるごと**（約 1〜2 GB） | 0 秒（そのまま在る） | △ 既定にできない。`keep_media` として残す |
| B | 開くときに VOD から切り直して**正規化し直す** | 0 | 切り出し + **2 パス正規化 16.1 秒** ×10 本 = **2 分 42 秒**（実測） | △ 初版の案。フォールバックとして残す |
| C | 正規化を**書き出しまで先送り**し、編集は生の切り出しで行う | 0 | 数秒 ×10 本 | ✕ 待ちが消えず書き出しへ移るだけ。しかも編集中の音量が本番と違う |
| **D** | 保存時に**正規化済みの音声だけ**を残し、開くときに **VOD からの切り出し映像と多重化**する | **音声のみ**（実測 3 MB / 3 分クリップ） | 切り出し + 多重化 = **0.23 秒 ×10 本 = 2 秒**（実測） | **◎ 採用（既定）** |

#### 案 D が成立する根拠（実コードで確認）

`loudness_normalizer.normalize_file()` の適用パスは次のコマンドを組む（`loudness_normalizer.py:207-216`）。

```
ffmpeg -y -i <raw.mp4> -af loudnorm=… -c:v copy -c:a aac -b:a 192k -ar 48000 <normalized.mp4>
```

* **映像は `-c:v copy`**。つまり `normalized.mp4` の映像ストリームは、切り出した `raw.mp4` の映像と**同一のバイト列**。
  → 映像は保存しなくてよい。VOD から同じ引数で切り直せば同じものが手に入る。
* **加工されているのは音声だけ**（AAC 192kbps / 48kHz）。MP4 の音声トラックはそのまま `.m4a` へ
  **再エンコードなしで抜き出せる**（`-vn -c:a copy`）。
* したがって保存時に音声だけ取っておけば、開くときは
  `切り出し(-c copy)` + `映像と音声の多重化(-c copy)` の**コピー 2 回**で復元できる。
  再エンコードも測定も無い。**結果は保存時の `normalized.mp4` と実質同一**
  （映像は同一バイト列、音声は同一 AAC ストリームをそのまま戻すため、エンコーダ遅延も込みで一致する）。

```
保存時   normalized.mp4 ──(-vn -c:a copy)──> <project>.media/clip1.m4a   ← 数秒・数 MB
開くとき VOD ──(-ss/-t -c copy)──> raw.mp4 ─┐
                                            ├─(-map 0:v -map 1:a -c copy)──> normalized.mp4
         <project>.media/clip1.m4a ─────────┘
```

#### 復旧の優先順位（`project_resume` の実装順）

| 順 | 条件 | 手段 | 速さ |
|---|---|---|---|
| ① | `.media/` に素材の複製がある（`keep_media=true` で保存） | そのまま使う | 即時 |
| ② | `.media/` に**音声サイドカー**がある（`keep_audio=true`・**既定**） | 切り出し + 多重化 | 数秒/クリップ |
| ③ | サイドカーが無い（旧プロジェクト / 手で消した） | 切り出し + **2 パス正規化**（案 B） | 数十秒/クリップ |
| ④ | 正規化が無効だった（`media_role="original"`） | 切り出しのみ | 数秒/クリップ |
| ⑤ | VOD が無い | 再リンクで VOD を選ばせる → ①〜④ をやり直す | — |

③ が成立する根拠（案 B の根拠。フォールバック時に効く）:

1. 切り出しは `-ss <start> -i <VOD> -t <duration> -c copy`（`clip_writer.py:52`）。
   **再エンコードしない**ため、同じ入力・同じ引数なら**出力は決定的**。
2. `vod_start` / `vod_end` は保存済み（`source.archive.clips[]`）。切り出しは `f"{start:.3f}"` の精度で
   引数化されるため、JSON の丸めでズレない。
3. ラウドネス正規化は 2 パス測定 + 線形適用で決定的（resolve7 §2.3）。同じ設定なら同じ音になる。

#### 「作るのと同じくらい待つ」わけではない（Q1 への補足）

初版 §5.11 の「切り抜き本編と同じくらい待つ場合がある」は**言い過ぎ**だった。実際の内訳は次のとおりで、
案 D では**再編集で作り直す工程が事実上ゼロ**になる。

| 工程 | 通常の切り抜き実行 | 再編集（案 B・初版） | 再編集（案 D・採用） |
|---|---|---|---|
| VOD の採点（窓スコア） | あり | なし | なし |
| クリップ切り出し | あり | あり（数秒） | あり（数秒） |
| ラウドネス正規化 | あり | **あり（2 パス）** | **なし（コピー）** |
| 無音検出 | あり | なし | なし |
| **音声認識（Whisper）** | **あり（最も重い）** | なし | なし |
| 編集画面 | あり | あり | あり |
| 書き出し | あり | あり | あり |

### 3-2【E5】Timeline の外にあるデータは `source.archive` へ入れる

`timeline.source` は `project_io._source_to_dict()` が**辞書をそのまま書き出す**（`project_io.py:132`）ため、
任意キーを足しても **schema_version は 1 のまま**でよく、`migrate()` も不要。
既に `archive` セクションが存在するので、その中へ次を足す（§5.1）。

* `clips[].theme` … クリップごとのテーマ
* `curve` … 採点グラフの窓スコア列（`{start,end,emotion,comment,total}` だけへ間引く）
* `media_role` … 素材が「正規化後」か「切り出しそのまま」か（復旧時に再正規化するかの判断に使う）

### 3-3【E5】テーマは「編集コマンド」にする

現在のテーマ入力は履歴にも未保存判定にも乗っていない（`archive_timeline_dialog.py:189`）。
保存を入れる以上、**テーマだけ直して閉じた場合に「未保存です」と言えないと事故になる**。

`SetArchiveClipTheme` コマンドを追加し、`timeline.source["archive"]["clips"][i]["theme"]` を書き換える。
これに伴い `commands._snapshot()` / `_restore()`（`commands.py:50` / `:66`）へ `source` を含める
（現状はどのコマンドも `source` を触らないため、含めても既存挙動は変わらない）。
得られるもの: **Undo が効く / 未保存の印が付く / 保存すれば JSON に載る**。

### 3-4【E6】プロジェクト種別は「archive セクションの有無」で判定し、経路を分ける

`project_io.project_kind(data|timeline)` を足す。`source.archive` があれば `"archive"`、無ければ `"clip"`。

| 選んだ場所＼種別 | クリップ用プロジェクト | アーカイブ用プロジェクト |
|---|---|---|
| クリップ用タブの「編集の続き」 | 従来どおり `run_from_project()` | 開かない。案内を出し、**アーカイブタブへ切り替えて選択状態にする** |
| アーカイブ用タブの「編集の続き」 | 開かない。案内を出し、**クリップ用タブへ切り替えて選択状態にする** | `archive/project_resume.run_from_archive_project()` |

履歴も分ける。`timeline.project.recent`（クリップ用・既存）と `timeline.project.recent_archive`（新規）。
種別ごとに違うリストへ積むことで、各タブのコンボには**その種別のものだけ**が並ぶ。

### 3-5【E7】保存先の既定名を分ける

現状の既定名は `default_project_path()`（`project_io.py:48`）＝ `<出力先>/<元動画名>.timeline.json`。
アーカイブ用は入力が VOD なので、**同じ VOD をクリップ用にも流すと同名になる**。
アーカイブ用の既定名を `<VOD名>.archive.timeline.json` にする（識別子は
`timeline.project.archive_suffix`、既定 `".archive"`）。

* 接尾辞 `.timeline.json`（`timeline.project_suffix`）は保ったままなので、D&D 判定
  （`main_window.py:744` 付近）やファイルフィルタは変更不要。
* 同じ VOD から 2 回アーカイブ切り抜きをすると既定名は衝突する。**上書き前提**（クリップ用と同じ挙動）とし、
  分けたい場合は「名前を付けて保存」を使う。

### 3-6【E8】削除は「本体 + 自動保存 + 複製素材 + 履歴」を 1 操作で

本体だけ消すと §2.6 の付随物が孤児になる。削除は次を 1 操作で行う。

1. 複製素材フォルダ `<name>.media/`（存在し、かつ `timeline.project.delete_media_dir=true` のとき）
2. 自動保存 `<name>.autosave.json`
3. プロジェクト本体
4. `setting.json` の履歴（`recent` / `recent_archive`）から除去 → `save_settings()`

### 3-7【E9】削除は**ゴミ箱経由**にする（回答 Q2）

* 外部ライブラリ（`send2trash`）は追加しない。**標準ライブラリの `ctypes` で Windows のシェル API
  `SHFileOperationW` を呼ぶ**（`FO_DELETE` + `FOF_ALLOWUNDO`）。実装は `src/utils/trash.py`（新規 / §5.10-b）。
* ゴミ箱へ送れない状況（ネットワークドライブ・ゴミ箱が無効・長すぎるパス・Windows 以外）では
  **黙って完全削除しない**。「ゴミ箱へ送れませんでした。完全に削除しますか?」と聞き直す（§5.10）。
* 確認ダイアログには**消えるものを実パスと合計サイズで列挙**する。既定ボタンは「キャンセル」。
  ゴミ箱経由なので文言は「削除します（ゴミ箱へ移動します）」とし、元に戻せることを明記する。
* 実行中（`_set_running(True)`）・編集画面が開いている間は削除を無効化する。
* 本体が既に無い場合は削除ではなく「**履歴から消す**」だけを行い、文言もそう出す。

### 3-8【E10 / E13】一覧の母集合は「履歴 + 保存フォルダの走査」

履歴だけでは §2.6 のとおり大半が出てこない。次の和集合を母集合にする（重複は絶対パスで排除）。

1. `timeline.project.recent` と `recent_archive`（明示保存したもの）
2. `timeline.project_dir`（空なら `general.output_directory`）の直下を走査し、
   `timeline.project_suffix`（`.timeline.json`）で終わるファイル
3. 走査対象へ加えたい追加フォルダ `timeline.project.library.extra_dirs`（既定 `[]`）

* **再帰探索はしない**（回答 Q9: 直下のみ）。出力先の下に大量の動画・中間物があると走査が重くなるため。
  深く掘りたい場合は `extra_dirs` へ明示する。
* 自動保存ファイル（`*.autosave.json`）は**一覧に出さない**。本体の行に「自動保存あり」の印として出す。
* 一覧に出たものは**履歴へは積まない**（履歴は「明示保存したもの」の意味を保つ）。
* 上限 **100 件**（回答 Q12 / `library.max_items`）。超えた分は更新日時の古いものから落とし、
  件数表示に「他 n 件（更新日時の新しい 100 件を表示しています）」と出す。**黙って切らない**。

**種別による絞り込み（回答 Q3: タブごとに別の一覧）**

一覧はタブごとに分ける。クリップ用タブの一覧にはクリップ用だけ、アーカイブタブの一覧にはアーカイブ用だけを出す。
種別は最終的に `project_kind()`（＝ JSON を読む）でしか確定しないため、次の 2 段で扱う。

| 段 | 判定 | 使い道 |
|---|---|---|
| 一次（即時） | **ファイル名**。`archive_suffix`（`.archive`）を含むか（§3-5） | 骨組みの行をすぐ出す |
| 確定（背景） | `read_summary()` の `kind` | 一致しない行は**取り除く**。一致する行は情報を埋める |

「名前を付けて保存」で規約から外れた名前を付けた場合、その行は一瞬出てから消える（または後から現れる）。
実害は無く、**確定判定は必ず JSON 側**であることを保証する。

### 3-9【E14】サムネイルは「保存時に作る」＋「無ければ元動画から作る」の 2 段構え

| 段 | いつ | どこから | 速さ |
|---|---|---|---|
| ① | **保存時**（`save_project()` の中） | 編集画面が既に開いている素材（プレビューのフレーム取得をそのまま使う） | 最速・確実 |
| ② | **一覧を開いたときに①が無ければ** | 元動画（`source.input_path` / `archive.vod_path`）を V1 クリップの由来時刻でシーク | 1 枚あたり数十〜数百 ms。背景スレッドで後追い |
| ③ | ①②とも取れない（元動画も無い） | プレースホルダ（種別アイコン + 「素材なし」） | 即時 |

* 出力は**キャッシュ**へ書く。置き場は `timeline.project.library.thumbnail_dir`（既定 `"project_thumbs"`、
  相対指定は `src/settings/` 基準）。`archive.download.work_dir` と同じ解決方針で、
  **利用者の出力フォルダを汚さない**。
* キャッシュ名は `<プロジェクト絶対パスの sha1>.jpg`。**プロジェクトの更新時刻がキャッシュより新しければ作り直す**。
* 何枚出すか: `library.thumbnail_count`（既定 **3**）。V1 の有効クリップ全体を等間隔に割った位置から取り、
  横に並べて 1 枚の帯（フィルムストリップ）にする。`1` にすれば単一サムネイルになる。
  これが要望の「**Timeline の V1 の一部分を一覧上に表示**」にあたる。
* 取得位置は**クリップの中央付近**を使う（先頭は暗転・切り替わりに当たりやすいため）。

### 3-10【E11 / E15】リネームは「4 点セット」で行う

プロジェクト名を変えるとき、次を**まとめて**行わないと開けないプロジェクトになる。

| # | 対象 | 理由 |
|---|---|---|
| 1 | 本体 `<old>.timeline.json` → `<new>.timeline.json` | 本体 |
| 2 | 自動保存 `<old>.autosave.json` → `<new>.autosave.json` | 名前は本体から導出される（`project_io.py:62`）ため、置いていくと**別プロジェクトの自動保存として拾われる** |
| 3 | 複製素材 `<old>.media/` → `<new>.media/` | フォルダ名は本体から導出される（`project_io.py:72`）ため、置いていくと**素材を見失う** |
| 4 | **JSON 内の素材パスの張り替え** | `media.path` は絶対パスで `<old>.media/…` を指している。3 を行うと**全部リンク切れになる**。`path_rel` も併せて書き直す |

* 4 は JSON を**生の辞書のまま**読み書きする（`Timeline` へ通すと未知キーが落ちる可能性があるため）。
  対象は `media_pool[].path` / `media_pool[].path_rel` / `source.media_path` のうち、
  旧 `.media` フォルダ配下を指しているものだけ。
* 途中で失敗したら**そこまでの改名を巻き戻す**（1→2→3 の順に進め、失敗時は逆順で戻す）。
* 履歴（`recent` / `recent_archive`）の該当エントリも新しいパスへ差し替える。
* 入力値の検査: 空文字不可 / `\ / : * ? " < > |` を含む場合は不可 / 同名が既に在る場合は不可。
  接尾辞（`.timeline.json`）は**利用者に入力させず**アプリが付ける（一覧に出す名前も接尾辞を外した「表示名」）。

### 3-11【E12 / E16】画面の大きさと応答性

* 一覧は **メインウィンドウを大きくせず、独立したダイアログ**（`ProjectLibraryDialog`）として出す。
  メインウィンドウは `resize()` を呼ばずレイアウト任せで小さく保たれており（`main_window.py`）、
  ここへ一覧を埋め込むと**通常操作の画面が常時大きくなる**ため。
  要望の「必要であれば画面の大きさを変更してよい」は、この**ダイアログ側の既定サイズ**として使う
  （`library.window_width` = 980 / `library.window_height` = 620。編集画面 1280×820 より一回り小さい）。
* **ダイアログはタブごとに開く**（回答 Q3）。同じクラスに `kind`（`"clip"` / `"archive"`）を渡し、
  タイトルと母集合をそれに合わせる。種別の絞り込みコンボは置かない（タブが絞り込みそのものになるため）。
* 「編集の続き」の行には **「一覧...」ボタン**を足す。§5.9 で設計した行の削除ボタンは**一覧側へ移す**
  （破壊的な操作の入口を 2 か所に置かない / §5.10 改訂）。
* 応答性（E16）: 一覧の骨組み（表示名・更新日時・サイズ・自動保存の有無）は `os.stat` だけで**即座に**出す。
  種別・V1 クリップ数・尺・サムネイルは**背景スレッドで後追い**して行を更新する（種別が食い違った行は取り除く / §3-8）。
  JSON の解析は 1 件ずつ行い、失敗した行は「壊れています」と出して**一覧全体は落とさない**。

---

## 4. 変更ファイル一覧

| 区分 | ファイル | 内容 | 節 |
|---|---|---|---|
| 変更 | `src/timeline/commands.py` | `_snapshot`/`_restore` に `source` を含める | §5.3 |
| 変更 | `src/timeline/project_io.py` | `project_kind()` 追加 / `default_project_path()` に `name_suffix` 引数 / `rename_project()`・`delete_project()`・`scan_projects()`・`read_summary()` 追加 | §5.1 / §5.9 / §5.13 |
| 変更 | `src/timeline/media_recovery.py` | `recover()` に `source_recover` フックを追加 | §5.5 |
| 変更 | `src/archive/timeline_builder.py` | `build_archive_timeline(..., curve=None)` / `theme`・`media_role` を `source.archive` へ / `SetArchiveClipTheme`・`clip_entry()`・`clip_theme()` 追加 | §5.2 / §5.3 |
| 変更 | `src/archive/clip_writer.py` | `_cut_region`→`cut_region` / `_build_clip_settings`→`build_clip_settings` / レビュー後の処理を `finish_clips()` へ切り出し / `prepared` へ `media_role` | §5.6 |
| **新規** | `src/archive/project_resume.py` | アーカイブ用プロジェクトの素材復旧と再編集実行 | §5.5 / §5.7 |
| **新規** | `src/timeline/media_sidecar.py` | 正規化済み音声の抜き出し・多重化（案 D の中核） | §5.5-a |
| **新規** | `src/utils/trash.py` | ファイル・フォルダをゴミ箱へ送る（`ctypes` + `SHFileOperationW`） | §5.10-b |
| 変更 | `src/gui/timeline/timeline_editor_dialog.py` | 保存周りへフックを 4 つ追加（既定は現行と同一） | §5.4 |
| 変更 | `src/gui/timeline/archive_timeline_dialog.py` | `_project_save_enabled` の上書きを削除 / フック実装 / テーマをコマンド化 | §5.4 |
| **新規** | `src/gui/project_resume_row.py` | 「編集の続き」行（コンボ + 開く + **一覧...**）の共有ウィジェット | §5.9 |
| **新規** | `src/gui/project_library_dialog.py` | **プロジェクト一覧画面**（サムネイル付き / 開く・リネーム・削除） | §5.12 / §5.14 |
| **新規** | `src/gui/project_thumbnail.py` | V1 のフレームからサムネイル帯を作る・キャッシュする | §5.15 |
| 変更 | `src/gui/timeline/timeline_editor_dialog.py`（再掲） | 保存時にサムネイルを作る（`save_project()` の末尾） | §5.15 |
| 変更 | `src/gui/main_window.py` | クリップ用タブの行を共有ウィジェットへ置換 / 一覧からの種別振り分け / タブ切替 | §5.8 / §5.9 / §5.16 |
| 変更 | `src/gui/archive_tab.py` | 「編集の続き」行を追加 / `ArchiveResumeWorker` | §5.8 |
| 変更 | `src/gui/archive_result_window.py` | `ArchiveResultBridge` に `mode` を通す（再編集で開いたことを画面へ伝える） | §5.7 |
| 変更 | `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ新規キー 4 件 | §6 |
| 変更 | `src/timeline/builder.py` | `timeline_config()["project"]` へ新規キーの読み出し | §6 |
| 新規 | `tests/test_archive_project_resume.py` | 保存→復旧→検証の往復 | §8 |

---

## 5. 詳細設計

### 5.1 `source.archive` スキーマ（schema_version は 1 のまま）

```jsonc
"source": {
  "input_path": "D:/vod/xxxx.mp4",
  "duration_sec": 812.4,
  "archive": {
    "vod_path": "D:/vod/xxxx.mp4",
    "media_role": "normalized",        // 追加: "normalized" | "original"
    "curve": [                          // 追加: 採点グラフ用 (間引き済み)
      {"start": 0.0, "end": 180.0, "emotion": 41.2, "comment": 8.0, "total": 26.2}
    ],
    "clips": [
      {"index": 1, "vod_start": 1200.0, "vod_end": 1380.0, "score": 72.3,
       "timeline_start": 0.0, "timeline_end": 175.2, "media_id": "m1",
       "theme": ""}                     // 追加
    ]
  }
}
```

* 既存キー（`vod_path` / `clips[].index,vod_start,vod_end,score,timeline_start,timeline_end,media_id`）は**そのまま**。
* 追加キーはすべて任意。**旧プロジェクトを読んでも壊れない**（`theme` 欠落 = 空、`curve` 欠落 = グラフが空、
  `media_role` 欠落 = `"normalized"` とみなす）。
* `curve` のサイズ見積り: 窓は 1 セル（= `slide_sec`、既定 45 秒）ずつスライドする
  （`scoring.build_cell_windows()`）。5 時間の VOD で約 400 要素 → JSON でおよそ 30KB。許容範囲（回答 Q4: 保存する）。

`project_io` へ追加する判定関数:

```python
# プロジェクトの種別を返す ("archive" / "clip")
# アーカイブ切り抜き用は source.archive を持つ (ver3 resolve9 §3-4)。
def project_kind(data_or_timeline):
    ...  # dict なら data["source"], Timeline なら timeline.source を見る
```

### 5.2 `build_archive_timeline()` の変更

```python
def build_archive_timeline(prepared, clip_settings, source_path="", curve=None):
```

* `clip_meta` に `"theme": ""` を足す。
* `timeline.source["archive"]` へ `"curve"` と `"media_role"` を足す。
* `media_role` は `prepared` から決める。`_prepare_clips()` で
  `"media_role": "normalized" if normalized != raw else "original"` を記録する
  （`loudness_normalizer.normalize_file()` は無効・音声無し・測定失敗のとき入力パスをそのまま返す
  ため、パス比較で正しく判定できる。`loudness_normalizer.py:165-191`）。
  クリップごとに違う値になり得るが、実際には設定は共通なので**全クリップの多数決ではなく
  「1 つでも `original` があれば `original`」**とはせず、`clips[].media_role` として**クリップ単位で**持つ。
* 呼び出し元 `clip_writer.write_clips()`（`:657`）は既に `curve` を受け取っているので、そのまま渡す。

### 5.3 テーマのコマンド化と履歴スナップショット

`src/archive/timeline_builder.py` へ追加:

```python
# source.archive.clips から 1 件を引く (無ければ None)
def clip_entry(timeline, clip_index): ...

# クリップのテーマ (未設定は空文字)
def clip_theme(timeline, clip_index): ...

# クリップ 1 件のテーマを変更する (ver3 resolve9 §3-3)
# テーマは Timeline モデルではなく source.archive へ持つため、
# Undo・未保存判定へ載せるにはコマンド経由で書き換える必要がある。
class SetArchiveClipTheme(commands.Command):
    label = "テーマの変更"

    def __init__(self, clip_index, theme):
        self._clip_index = clip_index
        self._theme = str(theme or "").strip()

    def apply(self, timeline):
        entry = clip_entry(timeline, self._clip_index)
        if entry is None or str(entry.get("theme", "")) == self._theme:
            return False        # 変化なし → 履歴を汚さない
        entry["theme"] = self._theme
        return True
```

`src/timeline/commands.py`:

```python
def _snapshot(timeline):
    return {
        "tracks": [...],                       # 現行のまま
        "media_pool": list(timeline.media_pool),
        "source": copy.deepcopy(timeline.source),    # 追加 (resolve9 §3-3)
    }

def _restore(timeline, snapshot):
    ...                                        # 現行のまま
    timeline.source = snapshot["source"]       # 追加
```

* コスト: 編集 1 回につき `source`（`curve` 約 400 要素）の deepcopy が 1 度。実測で 1ms 未満のオーダーであり、
  ユーザ操作の粒度（ドラッグ確定・分割）から見て無視できる。
* クリップ用の Timeline は `source` が小さい辞書のため、影響は事実上ゼロ。

### 5.4 編集画面の変更

#### (a) 基底 `TimelineEditorDialog` へフックを 4 つ追加（既定は現行と同一の結果）

| フック | 既定（クリップ用） | アーカイブ用 |
|---|---|---|
| `_project_save_enabled()` | `True`（既存） | **上書きを削除して `True` を使う** |
| `_default_project_path()` | `project_io.default_project_path(settings, input_path)` | `name_suffix=timeline.project.archive_suffix` を渡す |
| `_project_kind()` | `"clip"` | `"archive"` → `_remember_recent()` の書き込み先を決める |
| `_media_to_copy()` | 本編素材 1 件（現行 `_copy_media_beside_project` の対象） | **V1 が参照する全メディア** |
| `_title_base()` | `"Timeline 編集"` | `"切り抜き編集"` |

`_copy_media_beside_project()`（`:422`）は現在「`source.media_id` の 1 件だけ」を複製する。
`_media_to_copy()` を経由する形へ整理し、**複製先の名前を `f"{media.id}_{basename}"` にする**
（アーカイブ用は全クリップが `normalized.mp4` という同名のため、そのままでは衝突する）。
クリップ用は `keep_media=true` のときの複製ファイル名が変わるが、`media.path` / `path_rel` を
同時に書き換えるため**開き直しに影響は無い**（過去に複製済みのファイルは相対パス解決で従来どおり見つかる）。

`_remember_recent()`（`:510`）は `_project_kind()` に応じて `recent` / `recent_archive` へ積む。

**保存時に足す 2 つ**（`save_project()` の末尾。どちらも失敗しても保存は成功扱いにする）:

1. **音声サイドカーの書き出し**（`keep_audio=true` のとき / §3-1 案 D / §5.5-a）。
   `_media_to_copy()` が返す各メディアについて `media_sidecar.export_audio()` を呼ぶ。
   クリップ 10 本で数秒。進捗はプレビュー下のステータスへ「音声を保存しています…」と出す。
2. **サムネイルの生成**（§5.15）。素材が生きている状態なので確実で速い。

#### (b) `ArchiveTimelineDialog` の変更

```python
# 削除する
def _project_save_enabled(self):
    return False
```

* `self._themes`（`:60`）を廃止し、テーマは `archive_timeline.clip_theme(self.controller.timeline, index)` から読む。
* `_on_theme_changed()`（`:189`）は `self.controller.execute(SetArchiveClipTheme(index, text))` を呼び、
  `_update_history_buttons()` を続ける（`_on_use_toggled` と同じ形）。
* `result_data()`（`:254`）の `theme` も `clip_theme()` から取る。
* `_on_export_resolve()`（`:266`）の `self._themes.get(...)` も同様。
* `accept()`（`:286`）は現在 `super(TimelineEditorDialog, self).accept()` で**基底の accept を飛ばしている**。
  保存を有効化するため、飛ばす前に基底と同じ後始末を入れる。

```python
def accept(self):
    if not self.controller.timeline.base_clips():
        self.preview.set_status("使用するクリップがありません")
        return
    # 決定後は書き出し側が本体を上書き保存するため自動保存は不要になる (基底 accept と同じ)
    if self._save_enabled:
        self._discard_autosave()
    super(TimelineEditorDialog, self).accept()
```

* `__init__` に `project_path` / `created_at` / `mode` を受け取って基底へ渡す（再編集で開いたときに
  「保存先」と「初回作成時刻」と「閉じる確認の文言」を正しくするため）。

#### (c) 画面の見え方

保存ボタン・名前を付けて保存ボタンがクリップ用と同じ位置に並ぶ。ウィンドウタイトルは
`切り抜き編集 — <プロジェクト名>*` になる。**それ以外の既存挙動（採点グラフ・クリップバー）は不変**。

### 5.5 素材の復旧

#### (a) 音声サイドカー `src/timeline/media_sidecar.py`（新規 / §3-1 案 D の中核）

```python
# 正規化済み音声のサイドカー (ver3 resolve9 §3-1 案D)
#
# ラウドネス正規化は映像を -c:v copy で通し、音声だけを AAC へ焼き直す
# (loudness_normalizer.py:207-216)。つまり保存が要るのは音声だけで、映像は
# 元動画から同じ引数で切り直せば同じものが手に入る。
# ここでは「音声だけ抜く」「映像と音声を貼り合わせる」の 2 つを提供する。

# 正規化済み素材から音声だけを再エンコードなしで抜き出す
#   ffmpeg -i <src> -vn -c:a copy -map 0:a:0 <dest>
# 戻り値: 書き出したパス / 音声が無ければ None
def export_audio(src_path, dest_path, ffmpeg_cfg): ...

# 映像と音声を再エンコードなしで多重化する
#   ffmpeg -i <video> -i <audio> -map 0:v:0 -map 1:a:0 -c copy <dest>
def mux(video_path, audio_path, dest_path, ffmpeg_cfg): ...

# 音声コーデックからサイドカーと復元先の拡張子を決める
#   aac/alac → (".m4a", ".mp4") / それ以外 → (".mka", ".mkv")
# MP4 に入らないコーデック (opus 等) へ設定を変えられても壊れないようにするため。
def suffixes_for(ffmpeg_cfg): ...
```

* 置き場は **`<プロジェクト名>.media/`**（既存の `media_dir_suffix`）。
  こうすると**リネーム（§5.14）と削除（§5.10）が既に対応済み**になり、扱いが増えない。
* ファイル名は `f"{media.id}_audio{suffix}"`（例: `m1_audio.m4a`）。メディア ID で引けるようにする。
* 大きさの目安: AAC 192kbps ≒ **1 分あたり約 1.4 MB**（実測 3.0 MB / 3 分）。
  3 分クリップ 10 本で約 30 MB。`keep_media`（映像ごと複製）の数十分の 1。
* **サイドカーの正当性チェック**: 復元時に「サイドカーの尺」と「保存された `media.duration_sec`」を比べ、
  0.5 秒（`library` とは別に `project.sidecar_tolerance_sec`）を超えてズレていたら使わずに
  ③（再正規化）へ落とす。VOD を差し替えた・設定を変えたといった食い違いを検知するため。

#### (b) `media_recovery.recover()` にフックを 1 つ足す

```python
def recover(timeline, project_path, settings, context=None, relink_callback=None,
            source_recover=None):
```

判定順の ③ と ④ の間へ差し込む（既存の ①②③④⑤ は不変）。

```python
        # ③ 本編素材 (中間ファイル) の作り直し … 現行のまま (クリップ用)

        # ③' 呼び出し側が用意した復旧 (アーカイブ用: VOD から切り直す / resolve9 §5.5)
        if source_recover is not None:
            recovered = source_recover(media, source)
            if recovered:
                media.path = recovered
                result["recovered"].append(media.id)
                result["renormalized"] = True
                continue
```

`media_recovery`（`src/timeline/`）が `src/archive/` を import しない形にするための構造。
依存の向きは今までどおり **archive → timeline** の一方向を保つ。

#### (c) `src/archive/project_resume.py`（新規）

```python
# アーカイブ切り抜き用プロジェクトの再編集 (ver3 resolve9)
#
# 保存されたアーカイブ用 Timeline が参照する素材 (クリップごとの中間ファイル) は
# 実行の終わりに消えている。映像は VOD から同じ引数で切り直し、音声は保存して
# おいたサイドカーを貼り直して復元する (§3-1 案D)。どちらもコピーのため速い。
# サイドカーが無い旧プロジェクトは、正規化をやり直して復元する (案B)。

def is_archive_project(timeline) -> bool
def archive_section(timeline) -> dict

# media_recovery.recover へ渡す復旧関数を作る
# 戻り値: callable(media, source) -> 復旧したパス / None
def make_media_recover(timeline, settings, workdir, progress_cb=None): ...

# ダイアログと書き出しが要る prepared 相当を Timeline から組み直す
# 戻り値: [{"index","start","end","score","normalized_path","normalized_duration"}]
def rebuild_prepared(timeline): ...

def run_from_archive_project(project_path, settings, progress_cb=None,
                             result_callback=None, relink_callback=None,
                             restore_path=None): ...
```

`make_media_recover()` が返す関数の処理（1 メディアぶん）:

| 手順 | 内容 | 失敗時 |
|---|---|---|
| 1 | `archive.clips` から `media_id` が一致する entry を探す | 見つからない → `None`（従来の再リンクへ落とす） |
| 2 | VOD パス（`archive.vod_path` → 無ければ `source.input_path`）の実在を確認 | 無い → `None`（VOD 自体の差し替えは §5.7 ① で先に済ませる） |
| 3 | `clip_dir = workdir/clip{index}` を作り、`cut_region(vod, vod_start, vod_end, clip_dir/raw.mp4)` | 例外 → 呼び出し元へ送出（クリップ 1 本の失敗＝素材欠落として扱う） |
| 4 | `media_role == "original"`（正規化されていなかった） → `raw` をそのまま使って終了 | — |
| 5 | **音声サイドカーが在る** → `media_sidecar.mux(raw, サイドカー, clip_dir/normalized<ext>)`。§5.5-a の尺チェックに通れば採用 | 多重化に失敗 / 尺が合わない → 6 へ落とす |
| 6 | サイドカーが無い（旧プロジェクト等）: `missing_media_policy == "renormalize"` なら `normalize_file(raw, clip_dir/normalized.mp4, settings)`、`use_source` なら `raw` のまま | 正規化失敗時は `normalize_file` が入力パスを返す（音量は正規化前） |
| 7 | 復旧したパスを返す。**`duration_sec` は再 probe しない**（保存済みの値を保つ） | — |

5 と 6 のどちらを通ったかは `result` へ記録し（`"from_sidecar"` / `"renormalized"`）、
ログと進捗表示の文言を変える（「音声を復元中…」/「音量を正規化中…」）。**利用者が待ち時間の理由を追えるようにする。**

`rebuild_prepared()` は `archive.clips` と復旧後の `media_pool` から次を組む。

```python
{"index": 1, "start": 1200.0, "end": 1380.0, "score": 72.3,
 "normalized_path": "<復旧後のパス>", "normalized_duration": 180.0}
```

**`items` / `eff_cfg` / `keep_segments` / `profile` は入れない**。理由（実コードで確認済み）:

| 消費者 | 使うキー | 再編集で足りるか |
|---|---|---|
| `ArchiveTimelineDialog` のクリップバー・採点グラフ | `index/start/end/score` | ○ |
| `clip_writer._render_clips()`（`:550`） | `normalized_path` / `index` / `start` / `end` | ○（字幕は Timeline の `SubtitleClip` として保存済み） |
| `archive_timeline.to_export_entry()`（Resolve 出力） | `start`/`end`/`eff_cfg` | ○ `eff_cfg` 欠落時は `resolve_export.py:572` が既定設定から組み直す |

### 5.6 `clip_writer` の切り出し（挙動不変のリファクタ）

| 現行 | 変更後 | 理由 |
|---|---|---|
| `_cut_region()`（`:52`） | `cut_region()`（公開） | `project_resume` から呼ぶ |
| `_build_clip_settings()`（`:69`） | `build_clip_settings()`（公開） | 同上 |
| `write_clips()` の「③ burn 〜 結合」（`:688-733`） | `finish_clips(input_path, settings, clip_settings, timeline, edited, prepared_by_index, workdir, progress_cb, card, fonts_dir)` へ切り出し | 再編集経路と**同じ**書き出しを通す |

`write_clips()` は「prepare → build → review → `finish_clips()`」を呼ぶだけになる。
**出力ファイル名・結合・OP/ED・keep_individual の挙動は一切変えない。**

### 5.7 再編集の実行経路 `run_from_archive_project()`

```
① プロジェクト読込 (validate_timeline=False)
     project_io.load_project(restore_path or project_path)
     種別が archive でなければ InputError「アーカイブ切り抜き用のプロジェクトではありません」
② VOD の確認
     source.input_path が無ければ relink_callback で選ばせ、source/archive の両方へ書き戻す
③ with TemporaryDirectory(prefix="archive_clips_") as workdir:
     clip_settings = clip_writer.build_clip_settings(settings, workdir, clip_pipe)
     ④ 素材の復旧 (工程1)
          media_recovery.recover(timeline, project_path, settings, context,
                                 relink_callback=relink_callback,
                                 source_recover=make_media_recover(...))
          project_io.validate(timeline, min_clip_sec=cfg["min_clip_sec"])
     ⑤ prepared = rebuild_prepared(timeline)
     ⑥ 編集画面 (工程2)
          review = result_callback(prepared, archive.curve, timeline)   # 既存ブリッジと同じ形
          None ならキャンセル (PipelineCancelled)
          timeline = review["timeline"] / edited = review["clips"]
     ⑦ 確定後の上書き保存 (クリップ用 _review_timeline と同じ扱い / 回答 Q6)
     ⑧ 書き出し (工程3)
          clip_writer.finish_clips(...) → 出力パスの一覧を返す
```

* `result_callback` は既存の `ArchiveResultBridge`（`archive_result_window.py:264`）をそのまま使う。
  `mode="resume"` を持たせ、`_open_timeline_window()`（`:309`）が
  `ArchiveTimelineDialog(..., project_path=..., created_at=..., mode="resume")` を作れるようにする。
* `relink_callback` は既存の `MediaRelinkBridge`（`main_window.py:262`）をそのまま使う。
* 素材の復旧は `TemporaryDirectory` の中で行うので、**再編集後も一時ファイルは残らない**
  （現行の切り抜きと同じ寿命管理）。

### 5.8 アーカイブタブへ「編集の続き」を足す

`ArchiveTabWidget._build_ui()`（`archive_tab.py:225`）の実行ボタン行の下へ `ProjectResumeRow`（§5.9）を置く。

```python
class ArchiveResumeWorker(QThread):       # ArchiveClipWorker と同じ形
    progress = Signal(float, str)
    finished_ok = Signal(object)          # 出力パスの一覧
    cancelled = Signal()
    failed = Signal(str)
```

`run()` は `project_resume.run_from_archive_project(...)` を呼ぶだけ。
完了通知は既存の `_on_clip_done()`（`:521`）を再利用する。
`_set_running()`（`:542`）へ行の有効・無効を足す。

### 5.9 「編集の続き」行の共有ウィジェット `ProjectResumeRow`

> **§3-11 による改訂**: 当初この行に削除ボタンを置く設計だったが、一覧画面（§5.12）を作るため
> **削除・リネームは一覧側へ寄せ**、この行には「一覧...」ボタンを置く。破壊的な操作の入口を 2 か所に作らない。

クリップ用タブとアーカイブタブで**同じ行**を使うため、共有ウィジェットへ括り出す。
既存のクリップ用の見た目・文言・挙動は**そのまま**（「一覧...」ボタンが 1 つ増えるだけ）。

```python
# src/gui/project_resume_row.py
# 「編集の続き」の 1 行 (コンボ + 開く + 一覧) を提供する共有ウィジェット (ver3 resolve9)
class ProjectResumeRow(QWidget):

    resume_requested = Signal(str)      # 「開く...」で選ばれたパス
    library_requested = Signal()        # 「一覧...」で一覧画面を開きたい
    changed = Signal()                  # 履歴が変わった (一覧側の削除・リネームの後など)

    # kind        : "clip" / "archive" (履歴キーと種別ガードに使う)
    # settings_ref: 呼び出し側の設定辞書 (履歴の読み書きに使う。save_settings で永続化)
    def __init__(self, kind, settings_ref, parent=None): ...

    def refresh(self): ...              # 履歴からコンボを作り直す (現行 _refresh_recent_projects 相当)
    def select(self, path): ...         # D&D されたものを選択状態にする (現行 _select_project 相当)
    def set_busy(self, busy): ...       # 実行中は全部無効化
```

* 履歴キー: `kind == "archive"` なら `timeline.project.recent_archive`、それ以外は `recent`。
* 「開く...」押下時に**種別を判定**する（§3-4）。種別違いなら開かず、
  `wrong_kind_selected(path, kind)` シグナルで親（`MainWindow`）へ通し、タブを切り替えて
  相手側の行に `select(path)` させる（§9 Q5）。
* `timeline.project.library.enabled = false` のときは「一覧...」ボタンを作らない（従来どおりの行になる）。

`ClipTabWidget` 側は `project_combo` / `resume_button` / `_refresh_recent_projects()` /
`_select_project()` を `ProjectResumeRow` へ委譲する。`_on_resume` / `_start_resume` の中身は不変。

### 5.10 削除の詳細（実行場所は一覧画面 / §5.12）

#### (a) 手順

```
「削除」押下
  ├─ 対象パス = 一覧で選択中の行 (複数選択可 / §5.12)
  ├─ 収集: 本体 / 自動保存 (<name>.autosave.json) / 複製素材フォルダ (<name>.media/)
  │        それぞれ存在するものだけ。合計サイズも数える
  ├─ 本体が存在しない場合
  │     「一覧から消しますか」→ Yes なら履歴からの除去のみ
  └─ 存在する場合
        QMessageBox (Question / 既定=キャンセル)
          「次のファイルをゴミ箱へ移動します。」
          ・D:/out/xxxx.timeline.json (12 KB)
          ・D:/out/xxxx.autosave.json (12 KB)
          ・D:/out/xxxx.media/ (43 MB)
          「ゴミ箱から元に戻せます。」
          [ゴミ箱へ移動] [キャンセル]
        → trash.send_to_trash([複製素材フォルダ, 自動保存, 本体])   ← 1 回の API 呼び出しでまとめて送る
        → ゴミ箱へ送れなかったものがあれば
             「次のファイルをゴミ箱へ送れませんでした。完全に削除しますか?」(既定=いいえ)
             → はい なら os.remove / shutil.rmtree で完全削除
             → いいえ ならそのまま残す (一覧にも残る)
        → 履歴から除去 → save_settings() → refresh()
```

* まとめて 1 回で送るのは、ゴミ箱の確認・進捗ダイアログが 3 回出るのを避けるため
  （`SHFileOperationW` は複数パスを一度に受け取れる）。
* `timeline.project.delete_media_dir = false` のときは `.media/` を対象から外す（確認の一覧にも出さない）。
* `timeline.project.delete_button = false` のときはボタン自体を作らない（一覧は閲覧と開くだけになる）。
* `timeline.project.delete_to_trash = false` にすると初版どおりの完全削除になる（確認文言も切り替える）。
* サムネイルのキャッシュ（§5.15）はゴミ箱へ送らず**その場で消す**（再生成できる中間物のため）。
  消せなくても削除は成功扱いにする。
* 実体は `project_io.delete_project(path, cfg, use_trash=True)` に置き、GUI は確認と結果表示だけを行う。

#### (b) `src/utils/trash.py`（新規 / 回答 Q2）

```python
# ファイル・フォルダをゴミ箱へ送る (ver3 resolve9 §3-7 / 回答 Q2)
#
# 外部ライブラリ (send2trash) は docs/CLAUDE.md の方針により追加しない。
# 標準ライブラリの ctypes から Windows のシェル API SHFileOperationW を呼ぶ。
# 非 Windows・API 失敗時は False を返し、完全削除するかは呼び出し側が利用者へ確認する。

FO_DELETE = 0x0003
FOF_SILENT = 0x0004            # 進捗ダイアログを出さない
FOF_NOCONFIRMATION = 0x0010    # 「ゴミ箱へ移しますか」を出さない (アプリ側で確認済み)
FOF_ALLOWUNDO = 0x0040         # ← これがゴミ箱行きの指定
FOF_NOERRORUI = 0x0400         # エラーダイアログは出さず戻り値で受ける

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),      # "path1\0path2\0\0" (ダブル NUL 終端)
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),      # FILEOP_FLAGS = WORD
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]

# ゴミ箱が使えるか (Windows で shell32 を読めるか)
def is_available(): ...

# まとめてゴミ箱へ送る
# 戻り値: {"trashed": [パス…], "failed": [{"path","reason"}…]}
def send_to_trash(paths): ...
```

実装上の注意（推測ではなく API 仕様として既知の制約。実装時に実機で確認する / §9 Q14）:

* `pFrom` は**絶対パス**で、**ダブル NUL 終端**の連結文字列。相対パスは受け付けない。
* `SHFileOperationW` は **`MAX_PATH`（260 文字）を超えるパスを扱えない**。超える場合は
  `failed` に入れて完全削除の確認へ回す。
* 戻り値 0 が成功。非 0 はシェル固有のエラーコード（`0x7C` = パス不正 など）。`GetLastError` ではない。
* 構造体は 64bit でのパディングを既定に任せる（`_pack_` を指定しない）。
* `os.name != "nt"` では `is_available()` が `False` を返し、`send_to_trash()` は全件 `failed`。

### 5.11 進捗表示

`run_from_archive_project` は `PipelineContext` の工程分割（`total_steps`）に合わせる。

| 工程 | 進捗の範囲 | 表示 |
|---|---|---|
| ① 素材の復旧 | 0.00 – 0.55 | 「クリップ n/N を復元中…（切り出し / 音声の復元）」<br>サイドカーが無く再正規化へ落ちた場合は「（音量を正規化中）」 |
| ② 編集画面 | 0.55（停止） | 「編集中…」 |
| ③ 書き出し | 0.60 – 1.00 | 既存 `finish_clips` の進捗をそのまま使う |

所要時間（3 分クリップ 1 本あたりの実測値 / 720p・ローカル SSD）:

| 工程 | 実測 |
|---|---|
| 切り出し（`-c copy`） | 0.12 秒 |
| **音声の抜き出し（保存時）** | **0.09 秒** |
| **多重化（開くとき）** | **0.11 秒** |
| ラウドネス正規化（2 パス） | 16.06 秒 |

| 復旧の経路 | 10 本ぶんの ① |
|---|---|
| `.media/` に映像ごと複製（`keep_media=true`） | ほぼ 0 秒 |
| **音声サイドカー（既定）** | **約 2 秒** |
| サイドカー無し → 再正規化 | 約 2 分 42 秒 |

サイドカーが無くて再正規化になった場合は、その旨をログと進捗文言に出し、
**次に保存すればサイドカーが作られて以後は速くなる**ことをステータス行で案内する。

### 5.12 プロジェクト一覧画面 `ProjectLibraryDialog`（E10 / E11 / E12）

`src/gui/project_library_dialog.py`（新規）。モーダルダイアログ。既定 **980×620**
（`timeline.project.library.window_width` / `window_height`）。
背景は他の画面と同じく `theme.install_window_background(self)` を敷く。

**タブごとに開く**（回答 Q3）。`kind` を受け取り、タイトルと母集合をそれに合わせる。

| 開いた場所 | タイトル | 出るもの |
|---|---|---|
| クリップ用タブ | `Timeline プロジェクト一覧（クリップ用）` | クリップ用プロジェクトのみ |
| アーカイブ切り抜き用タブ | `Timeline プロジェクト一覧（アーカイブ用）` | アーカイブ用プロジェクトのみ |

```
┌ Timeline プロジェクト一覧（クリップ用） ─────────────────── 980 × 620 ┐
│ [検索 ____________]              並び (更新が新しい順 ▼)      [再読込] │
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │ ▐▓▓▐▓▓▐▓▓  2026-08-20_gameplay                      素材の複製あり │ │
│ │  (V1 の帯)  V1 12 クリップ / 8:42 / 更新 08-20 14:02 / 312 KB     │ │
│ │             D:/output/2026-08-20_gameplay.timeline.json  自動保存あり│ │
│ ├──────────────────────────────────────────────────────────────────┤ │
│ │ ▐▓▓▐▓▓▐▓▓  2026-08-19_talk                                        │ │
│ │  (V1 の帯)  V1 5 クリップ / 12:03 / 更新 08-19 22:10 / 1.1 MB     │ │
│ ├──────────────────────────────────────────────────────────────────┤ │
│ │ ▐ ? ▐      old_project                    ⚠ 読み込めません        │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│ 12 件                          [開く] [リネーム] [削除]      [閉じる] │
└────────────────────────────────────────────────────────────────────┘
```

#### (a) 一覧の作り

* `QListWidget`（`ListMode`）+ 自前の `QStyledItemDelegate`。
  行の高さ = サムネイル高 + 余白。左にサムネイル帯（`Qt.DecorationRole`）、右に 3 行のテキスト。
  `setItemWidget` は使わない（件数が増えるとウィジェット生成が重くなるため）。
* 1 行の情報:

| 位置 | 内容 | 出所 |
|---|---|---|
| 左 | **V1 の帯**（既定 3 コマ） | §5.15 |
| 1 行目 | 表示名（接尾辞を外したファイル名）＋ 右端に印（`素材の複製あり` = `.media/` が在る） | ファイル名 / `os.path.isdir` |
| 2 行目 | `V1 n クリップ / 尺 / 更新日時 / ファイルサイズ` | `read_summary()` / `os.stat` |
| 3 行目 | 絶対パス（薄字）＋ 印（`自動保存あり` / `⚠ 読み込めません`） | `os.path.exists` |

* 種別は列に出さない。**一覧そのものが種別ごと**のため（回答 Q3）。
* 選択は `ExtendedSelection`（削除だけ複数選択に対応）。**開く・リネームは 1 件選択時のみ有効**。
* 並び替え: 更新が新しい順（既定）/ 名前順 / サイズが大きい順。
* 絞り込み: 表示名・パスの部分一致検索のみ。
* キー割り当て: `Enter` = 開く / `F2` = リネーム / `Delete` = 削除 / `F5` = 再読込。
  右クリックメニューにも同じ 3 つ ＋「フォルダを開く」（`QDesktopServices.openUrl`）を出す。

#### (b) 読み込みの流れ（E16）

```
open(kind)
 ├ ① scan_projects(kind=…)      … os.scandir + 履歴。パスと stat だけ。ここまでは同期 (数 ms)
 │    → 名前が種別の規約に合う行を即表示 (表示名・更新日時・サイズ・印 / サムネイルは灰色の枠)
 └ ② _SummaryWorker (QThread)   … 1 件ずつ read_summary() → 種別を確定して行を更新
      ├ 種別が違った行     … 取り除く (規約外の名前で保存されたもの)
      ├ 一次判定から漏れた行 … 種別が一致したら追加する
      └ ③ サムネイル要求   … project_thumbnail.ensure() (キャッシュ命中なら即返る)
```

* ②③ はダイアログを閉じたら中断する（`requestInterruption()` を毎件チェック）。
* ②で例外が出た行は `broken=True` として「⚠ 読み込めません」を出す。**一覧全体は止めない**。
* 進行中は右下に「読み込み中… n/m」を出す。
* 上限（`max_items` = 100 / 回答 Q12）を超えた場合は「他 n 件」を件数表示へ添える。

#### (c) シグナル

```python
class ProjectLibraryDialog(QDialog):

    # 開く: 一覧で選ばれたプロジェクトを開きたい
    open_requested = Signal(str)          # project_path
    # 履歴・ファイルが変わった (削除・リネームの後)
    changed = Signal()

    # kind        : "clip" / "archive" (母集合とタイトルを決める / 回答 Q3)
    # settings_ref: 呼び出し側の設定辞書 (履歴の読み書きに使う)
    def __init__(self, kind, settings_ref, parent=None): ...
```

「開く」を押したらダイアログを閉じ、`open_requested` を**呼び出したタブ**へ投げる（§5.16）。
削除・リネームはダイアログ内で完結し、`changed` で行と「編集の続き」コンボを作り直させる。

### 5.13 `project_io` へ足す 4 関数（GUI から切り離してテストできる形にする）

```python
# 一覧に出す候補を集める (ver3 resolve9 §3-8)
# 履歴 (kind に応じて recent / recent_archive) + project_dir (無ければ
# general.output_directory) の直下 + library.extra_dirs を走査し、project_suffix で
# 終わるファイルの絶対パスを重複なく返す。自動保存ファイル (autosave_suffix) は除外する。
# kind を渡すと「ファイル名による一次判定」で絞り込む (確定は read_summary / 回答 Q3)。
# 更新日時の新しい順。max_items で打ち切り、打ち切った件数も返す。
# 戻り値: {"paths": [絶対パス…], "truncated": 打ち切った件数}
def scan_projects(settings, kind=None, timeline_cfg=None): ...

# プロジェクト 1 件の概要を読む (一覧の後追い表示用)
# 戻り値: {"path","name","kind","created_at","updated_at","generator",
#          "clip_count","duration_sec","input_path","frames":[{"path","sec"}…],
#          "has_autosave","has_media_dir","broken"}
# frames は V1 の有効クリップを等間隔に割った位置の (実在する動画のパス, その動画内の秒)。
# 素材が消えていれば元動画へ読み替える (§2.7)。
def read_summary(path, settings, timeline_cfg=None, frame_count=3): ...

# プロジェクトの改名 (本体・自動保存・複製素材・JSON 内のパス / §3-10 / §5.14)
# 戻り値: 新しいプロジェクトの絶対パス
def rename_project(path, new_stem, settings, timeline_cfg=None): ...

# プロジェクトの削除 (本体・自動保存・複製素材 / §5.10)
# 戻り値: {"deleted": [パス…], "failed": [{"path","reason"}…]}
def delete_project(path, timeline_cfg=None, delete_media_dir=True): ...
```

`read_summary()` の `frames` の決め方:

1. `load(path, validate_timeline=False)` で Timeline を得る（**検証しない**。素材が無いのは織り込み済み）。
2. 有効な V1 クリップを時系列に並べ、合計尺 `T` を出す。
3. `i = 0..N-1` について `t = (i + 0.5) / N * T` の位置に乗るクリップを探し、
   そのクリップ内オフセットから `source_sec = clip.source_in + オフセット` を求める。
4. 素材（`media.path`）が実在すればそれを使う。無ければ元動画へ読み替える。
   * クリップ用: `source.input_path` をそのまま、`source_sec` もそのまま（時間軸が一致するため / §2.7）。
   * アーカイブ用: `source.archive.vod_path`、`source_sec + そのクリップの vod_start`
     （クリップは `clip.origin["archive_clip_index"]` で特定する）。
5. 元動画も無ければ `frames` は空 → プレースホルダ表示。

### 5.14 リネームの手順（§3-10 の実装）

```
rename_project(path, new_stem, ...)
 ① 検査
     new_stem が空 / \ / : * ? " < > | を含む → TimelineError
     新しい本体パスが既に存在する → TimelineError
 ② 改名 (順に行い、失敗したらそこまでを逆順で戻す)
     1. <old>.timeline.json → <new>.timeline.json          (os.replace)
     2. <old>.autosave.json → <new>.autosave.json          (在れば)
     3. <old>.media/        → <new>.media/                 (在れば / os.rename)
 ③ JSON 内のパスの張り替え (3 を行った場合のみ)
     生の辞書で読み、media_pool[].path / media_pool[].path_rel / source.media_path のうち
     旧 .media フォルダ配下を指すものを新フォルダへ差し替えて書き戻す
 ④ 履歴の差し替え (recent / recent_archive の該当エントリ)
 ⑤ サムネイルキャッシュの改名 (best-effort。失敗しても次回作り直される)
```

* ②の 1 を先に行うのは、**本体さえ動けば残りは本体から導出できる**ため。
  途中失敗時に「本体は新名・付随物は旧名」という状態を残さないよう、巻き戻しを必ず入れる。
* 一覧の UI 側は `QInputDialog.getText()` で表示名（接尾辞なし）を尋ね、`rename_project()` を呼ぶだけ。
* 開いている編集画面があっても一覧は開けるが、**編集中のプロジェクトを改名すると
  その画面の保存先は旧パスのまま**になる。編集画面が開いている間は一覧の
  リネーム・削除を無効化する（§9 Q11）。

### 5.15 サムネイル `project_thumbnail.py`（新規）

```python
# Timeline の V1 からサムネイル (帯) を作り、キャッシュする (ver3 resolve9 §3-9)
#
# 保存済みプロジェクトの V1 素材は一時ファイルで、一覧を開く時点では消えている。
# そのため素材が無ければ元動画へ読み替えて同じ絵を取る (§2.7)。

def cache_dir(settings): ...                     # 既定 src/settings/project_thumbs (無ければ作る)
def cache_paths(project_path, settings): ...     # [<sha1>_0.jpg, _1.jpg, …]
def is_fresh(project_path, settings): ...        # プロジェクトの更新時刻とキャッシュを比べる
def ensure(project_path, settings, summary=None): ...  # 無ければ作る。戻り値: 実在するJPEGの一覧
def build_pixmap(paths, settings): ...           # JPEG 群を横に並べた 1 枚の QPixmap にする
def discard(project_path, settings): ...         # 削除時に消す
def rename(old_path, new_path, settings): ...    # 改名時に付け替える
```

* **1 コマ = ffmpeg の 1 プロセス**。`FfmpegFrameSource`（`frame_source.py:262`）と同じ形で、
  `-hide_banner -loglevel error -ss <sec> -i <path> -frames:v 1 -vf scale=<W>:-2 -q:v 3 <out.jpg>` を
  `subprocess.run(..., creationflags=no_window_creationflags())` で叩く。
  * raw バッファ方式（`frame_source`）を使わない理由: バッファ長を決めるのに素材の縦横が要るが、
    **元動画へフォールバックした場合その寸法は Timeline に記録されていない**（§2.7 末尾）。
    JPEG ファイルとして書けば寸法を知らずに済み、そのままキャッシュにもなる。
  * PyAV の有無に関係なく動く（`ffmpeg_runner.get_ffmpeg_exe()` は既に必須依存）。
* 幅は `library.thumbnail_width_px`（既定 160）。高さは `-2` で素材のアスペクト比を保つ。
  帯の全体幅 = `thumbnail_width_px × thumbnail_count`。
* 失敗したコマは飛ばす。**1 コマも作れなければプレースホルダ**（種別アイコン + 「素材なし」）。
* 保存時（`save_project()` の末尾）にも `ensure()` を呼ぶ。素材が生きている状態なので確実で速い。
  失敗しても保存は成功扱い（サムネイルは飾りであり、無くても開ける）。
* キャッシュの上限: `library.thumbnail_cache_limit`（既定 200 プロジェクト ≒ 数十 MB）。
  超えたら**参照されていない古いファイルから**消す（一覧を開いたときに掃除する）。

### 5.16 一覧から開くときの振り分け（E6 との接続）

一覧はタブごとに開くため（回答 Q3）、**振り分けそのものが不要**になる。
`open_requested(path)` を受けるのは一覧を開いたタブ自身で、そのまま自分の再開処理へ入る。

| 開いた場所 | 受け手 |
|---|---|
| クリップ用タブ | `ClipTabWidget._start_resume(path)`（現行のまま） |
| アーカイブ切り抜き用タブ | `ArchiveTabWidget._start_resume(path)`（§5.8 で足す） |

* 受けたタブは `ProjectResumeRow.select(path)` を呼んでコンボの選択状態も合わせてから開始する。
* どちらかのタブが実行中（`_running_tabs` が空でない / `main_window.py:958`）のときは、
  一覧の「開く」を無効化する（二重起動を防ぐ）。`MainWindow` が現在の実行状態をタブへ渡す。
* これにより §3-4 の「種別違いを選んだときの案内」は、**一覧から開く限り発生しない**。
  コンボの「ファイルから選ぶ...」経由の取り違えだけが残り、そこは §5.9 のガードが受ける
  （タブ切替の要否は §9 Q5・未回答）。

---

## 6. 設定キー

`DEFAULT_SETTINGS`（`settings_window.py:282` の `timeline.project`）へ追加する。既存キーは変更しない。

**保存・再編集・削除（§3-5 / §3-6）**

| キー | 既定 | 意味 |
|---|---|---|
| `timeline.project.archive_suffix` | `".archive"` | アーカイブ用プロジェクトの既定名に挟む識別子（§3-5） |
| `timeline.project.recent_archive` | `[]` | アーカイブ用の「編集の続き」履歴（§3-4） |
| `timeline.project.keep_audio` | **`true`** | **保存時に正規化済み音声を `<name>.media/` へ残す**（§3-1 案 D / 回答 Q1）。`false` にすると開くときに再正規化になる |
| `timeline.project.sidecar_tolerance_sec` | `0.5` | サイドカーの尺と保存値のズレをどこまで許すか（§5.5-a） |
| `timeline.project.delete_button` | `true` | 一覧に削除ボタンを出す |
| `timeline.project.delete_to_trash` | **`true`** | **削除をゴミ箱経由にする**（回答 Q2）。`false` で完全削除 |
| `timeline.project.delete_media_dir` | `true` | 削除時に複製素材フォルダ `<name>.media/` も消す |

**一覧画面（§3-8 〜 §3-11 / `timeline.project.library`）**

| キー | 既定 | 意味 |
|---|---|---|
| `library.enabled` | `true` | 「一覧...」ボタンを出す（`false` で従来どおりの行） |
| `library.window_width` | `980` | 一覧画面の初期幅（E12） |
| `library.window_height` | `620` | 一覧画面の初期高さ（E12） |
| `library.extra_dirs` | `[]` | 走査へ加えるフォルダ（§3-8） |
| `library.scan_recursive` | `false` | 走査を再帰にするか（既定は直下のみ） |
| `library.max_items` | **`100`** | 一覧に出す上限（回答 Q12）。超えた分は更新日時の古いものから落とし、件数表示に「他 n 件」を出す |
| `library.sort` | `"updated_desc"` | 初期の並び（`updated_desc` / `name_asc` / `size_desc`） |
| `library.thumbnail_count` | `3` | V1 の帯に並べるコマ数（`1` で単一サムネイル / §3-9） |
| `library.thumbnail_width_px` | `160` | 1 コマの幅（高さはアスペクト比維持） |
| `library.thumbnail_dir` | `"project_thumbs"` | キャッシュ置き場（相対指定は `src/settings/` 基準） |
| `library.thumbnail_cache_limit` | `200` | キャッシュを保持するプロジェクト数の上限（§5.15） |
| `library.rename_enabled` | `true` | リネームボタンを出す |

`timeline_config()`（`builder.py:162`）の `"project"` へ同名の読み出しを足す（型と既定は上表のとおり）。
`library` は 2 段の入れ子（`timeline.project.library`）になるが、
`_fill_timeline_nested_defaults()`（`settings_window.py:712`）→ `_fill_dict_defaults()`（`:736`）が
**timeline セクションを再帰的に補完**するため、既存の `setting.json` にも自動で現れる。
本書で `setting.json` を直接書き換える必要は無い（実コードで確認済み）。

**流用する既存キー**: `save_button` / `confirm_on_close` / `keep_media` / `media_dir_suffix` /
`missing_media_policy` / `autosave_suffix` / `recent_limit` / `timeline.project_dir` /
`timeline.project_suffix` / `timeline.autosave_sec`。
アーカイブ用でもこれらがそのまま効く（= 設定画面の説明を書き換える必要が無い）。

---

## 7. 影響範囲・互換性

| 対象 | 影響 |
|---|---|
| クリップ用の実行・保存・再編集 | **なし**（フックの既定値は現行と同じ結果。`_media_to_copy` の複製ファイル名だけ変わるが `path`/`path_rel` も同時に書くため開き直しは同じ） |
| アーカイブ切り抜き（従来画面 `timeline_review=false`） | **なし**（`finish_clips` の切り出しは挙動不変。`_burn_clips` 経路は素通り） |
| アーカイブ切り抜き（Timeline 経路・通常実行） | 保存ボタンが増える。`source.archive` に `theme`/`curve`/`media_role` が載る。**書き出し結果は不変** |
| CLI / テスト（`result_callback` 未注入） | **なし**（`edited` の自動生成は現行のまま） |
| 既存の保存済みプロジェクト（クリップ用） | **読める**。`project_kind()` は `"clip"` を返し従来経路へ入る |
| resolve7 で作られた `.timeline.json` | 追加キーはすべて任意のため `schema_version` は 1 のまま。`migrate()` 不要 |
| 新版で保存 → 旧版で開く | クリップ用は問題なし。アーカイブ用は旧版に開く経路が無いだけ（壊れはしない） |
| クリップ用タブの「編集の続き」行 | 見た目は「一覧...」ボタンが 1 つ増えるだけ。コンボと「開く...」の挙動は不変 |
| メインウィンドウの大きさ | **変えない**。一覧は独立ダイアログ（§3-11） |
| 出力フォルダ | **汚さない**。サムネイルは `src/settings/project_thumbs/` へ置く（§3-9） |
| プロジェクトを一覧に出すこと自体 | 読むだけ。**JSON は書き換えない**（リネーム時の張り替えを除く / §5.14） |
| ffmpeg への依存 | 増えない（`get_ffmpeg_exe()` は既に必須）。PyAV の有無に関係なくサムネイルは作れる |
| 保存にかかる時間（アーカイブ用） | 音声サイドカーの書き出しぶん**数秒増える**（`-c:a copy` のため再エンコードは無い / `keep_audio=false` で従来どおり） |
| 保存で使うディスク（アーカイブ用） | `<name>.media/` に音声のみ **約 1.4 MB/分**（3 分 10 本で約 43 MB） |
| クリップ用の保存 | **変わらない**（`keep_audio` はアーカイブ用のみ既定 ON。クリップ用は §9 Q15） |
| ライブラリ依存 | **増えない**。ゴミ箱は `ctypes`（標準）で呼ぶ |
| 非 Windows 環境 | ゴミ箱は使えず `failed` になり、完全削除の確認へ回る（他の機能は同じ） |

---

## 8. テスト計画

### 8.1 自動テスト

| ファイル | 内容 |
|---|---|
| `tests/test_archive_project_resume.py`（新規） | ① `build_archive_timeline` → `project_io.save` → 素材削除 → `load_project` → `recover(source_recover=...)` → `validate` で **V1 が全本生存**すること（`cut_region` / `normalize_file` / `mux` はダミーファイルを作るスタブへ差し替え）<br>② 復旧が**サイドカー経路を通り、`normalize_file` を 1 度も呼ばない**こと（案 D の要）<br>③ サイドカーを消すと再正規化経路へ落ち、それでも V1 が生存すること<br>④ `rebuild_prepared()` が index/start/end/score/normalized_path を揃えること<br>⑤ アーカイブ用でないプロジェクトを渡すと `InputError` |
| `tests/test_timeline_project_io.py`（追記） | `source.archive`（`theme`/`curve`/`media_role` 込み）の往復。`project_kind()` の判定 |
| `tests/test_archive_timeline.py`（追記） | `SetArchiveClipTheme` の適用・変化なしで `False`・Undo で戻ること |
| `tests/test_command_stack_clean.py`（追記） | テーマ変更で `is_modified()` が `True` になり、`mark_saved()` で戻ること |
| `tests/test_media_sidecar.py`（新規） | ① `export_audio()` → `mux()` の往復で、**元の `normalized.mp4` と音声ストリームが一致**すること（`ffprobe` でコーデック・尺・ビットレートを比較）<br>② 音声が無い素材で `export_audio()` が `None` を返すこと<br>③ `suffixes_for()` が `aac` → `(.m4a, .mp4)`、`libopus` → `(.mka, .mkv)` を返すこと<br>④ 尺が食い違うサイドカーを渡すと復旧が③（再正規化）へ落ちること |
| `tests/test_trash.py`（新規） | ① Windows 上で一時ファイルを `send_to_trash()` に渡すと消え、`trashed` へ入ること<br>② 存在しないパス・`MAX_PATH` 超えのパスが `failed` に入り、**例外にならない**こと<br>③ 非 Windows では `is_available()` が `False`（`os.name` を差し替えて確認） |
| `tests/test_project_library.py`（新規） | ① `scan_projects()` が履歴 + フォルダ走査の和集合を重複なく返し、`*.autosave.json` を除くこと<br>② `read_summary()` が種別・クリップ数・尺・`frames` を返し、**素材が無いときは元動画へ読み替える**こと（クリップ用・アーカイブ用の両方）<br>③ `rename_project()` が本体・自動保存・`.media` を改名し、**JSON 内の `media.path` / `path_rel` / `source.media_path` を張り替える**こと。改名後に `load` → `validate` で素材が生きていること<br>④ `rename_project()` が不正名・同名衝突で `TimelineError` を投げ、**途中失敗時に巻き戻す**こと<br>⑤ `delete_project()` が 3 点を消し、`failed` を正しく返すこと（読み取り専用ファイルで検証）<br>⑥ 壊れた JSON を渡しても `read_summary()` が例外を投げず `broken=True` を返すこと |

### 8.2 手動確認（実 VOD が要るもの）

1. アーカイブ切り抜きを実行 → 編集画面で **保存** → タイトルの `*` が消える。
2. アプリを再起動 → アーカイブタブの「編集の続き」に出る → **開く** →
   **10〜30 秒**（クリップ 10 本）で **保存時と同じ Timeline**（クリップ・字幕・テーマ・使用可否・採点グラフ）が出る。
   ログに「音声を復元中」と出て、**ラウドネス正規化が走っていない**ことを確認する。
   `<name>.media/` の音声を消してからもう一度開くと、再正規化へ落ちて数分かかることも確認する（フォールバック）。
   保存前後の `normalized.mp4` の音声を聴き比べ、**音量が同じ**であることを確認する。
3. そのまま「完了（切り抜き＋字幕焼き込み）」→ **通常実行と同じ出力**が得られる。
4. テーマだけ変更して × で閉じる → **3 択のポップアップ**が出る。
5. `timeline.autosave_sec` を 30 にして編集途中で強制終了 → 次回「自動保存が見つかりました」が出る。
6. 「一覧...」→ **出力フォルダにあるプロジェクトが履歴に無いものも含めて並ぶ**。
   各行に **V1 の帯（3 コマ）** が出る（素材が消えていても元動画から作られる）。
7. 一覧で **開く** → 種別に応じたタブへ切り替わって再開する。
8. 一覧で **リネーム** → 表示名を変更 → 一覧が更新される → もう一度 **開く** と
   **素材リンクが切れずに開ける**（`.media` を使う設定 `keep_media=true` でも確認する）。
9. 一覧で **削除** → 確認に本体・自動保存・`.media` が出る → 削除後、ファイル・履歴・行が消え、
   **ゴミ箱に 3 つとも入っている**（元に戻せる）。ネットワークドライブ上でも試す（§9 Q14）。
10. クリップ用タブのコンボで「ファイルから選ぶ...」からアーカイブ用プロジェクトを選ぶ →
    案内が出てアーカイブタブへ切り替わる（逆も同様）。
11. 元動画を別の場所へ移してから一覧を開く → その行は**プレースホルダ**になり、他の行は正常に出る。

---

## 9. 確認事項（2026-08-20 に Q1〜Q3 / Q4 / Q6 / Q8 / Q9 / Q12 / Q13 の回答を受領）

**回答済み**

| # | 内容 | 回答 | 反映先 |
|---|---|---|---|
| **Q1** | 開き直すときの素材復旧をどうするか。初版は「VOD から切り直し + 2 パス再正規化」だった | **「作るのと同じくらい待つなら保存の意味が無い」→ 別案を出すこと** | **§3-1 を全面改訂**。保存時に**正規化済み音声だけ**を残し、開くときは「切り出し + 多重化」のコピー 2 回で復元する案 D を新設して既定にした（10 本で 3〜5 分 → **10〜30 秒**）。再正規化はサイドカーが無い場合のフォールバックへ降格（§5.5） |
| **Q2** | 削除をゴミ箱経由にするか | **ゴミ箱経由で OK** | §3-7 / §5.10。`ctypes` + `SHFileOperationW`（外部ライブラリ無し）。送れない場合だけ完全削除を聞き直す。`delete_to_trash` = `true` |
| **Q3** | 一覧を 1 画面にまとめるか、タブごとに分けるか | **タブごとに別の一覧** | §3-8 / §3-11 / §5.12 / §5.16。種別コンボは廃止。振り分けが不要になり §5.16 が単純化 |
| **Q4** | 採点グラフの `curve` をプロジェクトへ保存してよいか | **保存する** | §5.1（`source.archive.curve`） |
| **Q6** | アーカイブ用も「決定」後に自動で上書き保存するか | **自動で上書きする** | §5.7 ⑦ |
| **Q8** | 通常実行の途中でもプロジェクトを自動保存するか | **自動保存しない**（明示保存のみ） | §5.7 / §7 |
| **Q9** | 一覧の走査範囲 | **直下のみ** | §3-8（`scan_recursive` = `false`） |
| **Q12** | 一覧に出す上限 | **100 件** | §6（`library.max_items` = 100） |
| **Q13** | リネームの入力は表示名のみか | **拡張子は含めない** | §5.14（接尾辞はアプリが付ける） |

**未回答（本書の前提のまま進める）**

| # | 内容 | 本書の前提 |
|---|---|---|
| **Q5** | 種別違いのプロジェクトを選んだとき、**自動でタブを切り替える**か、案内だけ出して何もしないか | 案内 + タブ切替（選択状態にするところまで。実行はしない） |
| **Q7** | 元 VOD が消えている場合、再リンク画面で VOD を選ばせる設計にした。VOD を選び直したとき、`source.input_path` と `archive.vod_path` の**両方**を書き換えてよいか | 両方書き換える |
| **Q10** | V1 の帯は **3 コマ**でよいか（1 コマだけ / 5 コマ に変更可）。コマ数ぶん ffmpeg を起動するため、初回表示の時間に効く | 3 コマ |
| **Q11** | 編集画面が開いている間、一覧の**リネーム・削除を無効化**する方針でよいか（編集中のプロジェクトを改名すると保存先が旧パスのまま残るため）。無効化せず「編集中です」と弾く案もある | 無効化する |
| **Q14** | `SHFileOperationW` の実挙動（ネットワークドライブ・`MAX_PATH` 超え・ゴミ箱が無効なドライブ）は**実機で確認**が要る | **ローカルドライブでは確認済み**（ファイル・フォルダとも 1 回の呼び出しでゴミ箱へ入り、元に戻せる）。`MAX_PATH` 超えは送る前に弾いて完全削除の確認へ回す。ネットワークドライブは未確認（失敗しても `failed` に入るだけで、そこから完全削除を聞き直す） |
| **Q15** | 音声サイドカー（既定 ON）を**クリップ用プロジェクトにも広げる**か。クリップ用は素材が「元動画まるごとの正規化後」なので、サイドカーは尺に比例して大きくなる（1 時間で約 85 MB）代わりに、開くときの再正規化（数分）が消える | 本書ではアーカイブ用のみ。クリップ用は §11 の将来拡張（仕組みは共用できる形で作る） |

---

## 10. 実装順序（フェーズ）

| Phase | 内容 | 単体で確認できること |
|---|---|---|
| **1** | `source.archive` へ `theme`/`curve`/`media_role` を載せる（§5.1 / §5.2）＋ `project_kind()` | JSON の往復テストが通る |
| **2** | `SetArchiveClipTheme` と `_snapshot` への `source` 追加（§5.3） | テーマの Undo・未保存判定 |
| **3** | `clip_writer` の切り出し（`cut_region` / `build_clip_settings` / `finish_clips`）（§5.6） | **既存の切り抜きが従来どおり動く**（挙動不変のリファクタ） |
| **4a** | `timeline/media_sidecar.py`（§5.5-a） | 音声の抜き出し → 多重化の往復テスト（案 D の中核） |
| **4b** | `media_recovery` のフックと `archive/project_resume.py`（§5.5） | 保存 → 素材削除 → 復旧 → 検証の自動テスト（サイドカー経路・再正規化経路の両方） |
| **5** | 編集画面の保存対応（§5.4） | アーカイブ用で保存ボタンが出て保存できる（E1 完了） |
| **6** | アーカイブタブの「編集の続き」と実行経路（§5.7 / §5.8） | 開いて再編集し書き出せる（E2 完了） |
| **7** | `ProjectResumeRow`（コンボ + 開く + 一覧）（§5.9） | 行の見た目が整い、一覧の入口ができる |
| **8** | `project_io` の 4 関数（`scan_projects` / `read_summary` / `rename_project` / `delete_project`）（§5.13 / §5.14） | **GUI 抜きで**走査・概要・改名・削除の自動テストが通る |
| **9** | `project_thumbnail.py`（§5.15） | 保存済みプロジェクトから JPEG の帯が作られる（素材が消えていても） |
| **9b** | `utils/trash.py`（§5.10-b） | ゴミ箱へ送れる。失敗時に `failed` を返す（Q14 の実機確認もここ） |
| **10** | `ProjectLibraryDialog`（§5.12）とタブごとの起動（§5.16） | 一覧で開く・リネーム・削除ができる（E3 / E10 / E11 / E12 完了） |

Phase 3 までは**画面から見える変化が無い**（既存挙動の維持を確認する回）。
**Phase 7〜10（一覧まわり）は E1/E2 と独立している**ため、順序を入れ替えて先に実装できる。
一覧だけ先に欲しい場合は 7 → 8 → 9 → 10 の順に進めれば、アーカイブ用の保存が未実装でも
クリップ用プロジェクトに対して一覧・開く・リネーム・削除がすべて成立する
（アーカイブ用の行が一覧に現れるようになるのは Phase 5 以降）。

---

## 11. 将来拡張（本書では実装しない）

* **音声サイドカーをクリップ用へ広げる**（§9 Q15）: 仕組み（`media_sidecar.py`）は共用できる形で作るため、
  クリップ用の `_media_to_copy()` から呼ぶだけで、resolve7 の「開くときに再正規化」も同じく数秒になる。
  尺に比例してサイドカーが大きくなる（1 時間で約 85 MB）ため、既定 ON にするかは別途判断。
* **復旧結果のキャッシュ**: 一度復旧した素材を `<プロジェクト名>.media/` へ残し、2 回目以降は切り出しすら省く。
* **クリップの追加・削除**: 再編集中に「別の候補区間を足す」。`archive.curve` が保存されていれば
  採点グラフから区間を選べるので、素材の切り出しだけ足せば実現できる。
* **一覧からの複製（名前を付けて複製）**: リネームの仕組み（§5.14）がそのまま使える。
* **一覧のグリッド表示**: サムネイルを大きく並べるカード表示への切り替え。
  `QListWidget` の `IconMode` へ切り替えるだけで足りる（デリゲートは共用できる）。
* **タグ・お気に入り**: 一覧の絞り込みを名前以外へ広げる。`setting.json` 側に持たせるか、
  プロジェクト JSON の任意キーへ持たせるかの判断が要る。
* **サムネイルの手動指定**: 「この位置の絵を表紙にする」。再生ヘッド位置を
  `source.thumbnail_sec` として保存すれば `project_thumbnail` はそのまま使える。


---

## 12. 実装メモ（2026-08-20 / 設計から変えた点）

実装して分かった差分と、設計書の記述から意図的に変えた点を残す。

| # | 箇所 | 設計 | 実装 | 理由 |
|---|---|---|---|---|
| 1 | §5.12 一覧の描画 | `QListWidget` + 自前の `QStyledItemDelegate` | `QListWidget` + アイコン + 3 行テキスト（デリゲート無し） | 帯を `Qt.DecorationRole`、情報を改行入りテキストで置くだけで設計どおりの見た目になり、描画コードを持たずに済むため。行数が増えても `setItemWidget` を使わない方針は守っている |
| 2 | §5.9 種別違いの案内 | 案内 + タブ切替（§9 Q5 は未回答） | **実装した**。`ProjectResumeRow.wrong_kind_selected` → タブ側の `switch_tab_requested` → `MainWindow._switch_to_kind` | 一覧がタブごとになった（回答 Q3）ぶん、取り違えは「ファイルから選ぶ...」経由だけになった。そこだけ案内で止めるより移してやる方が短い |
| 3 | §5.5-b 復旧フックの位置 | `media_recovery.recover()` の ③ と ④ の間 | 同左（`source_recover` 引数） | 変更なし。timeline 層が archive 層を import しない構造を保っている |
| 4 | 素材の複製名 | `f"{media.id}_{basename}"` | 同左 | アーカイブ用は全クリップが `normalized.mp4` で衝突するため |
| 5 | `MediaRelinkBridge` の置き場 | `main_window.py`（既存） | `gui/timeline/missing_media_dialog.py` へ移動 | アーカイブタブからも使うため。`main_window` から import すると循環参照になる |
| 6 | `pipeline_runner.run_from_project` | （記述なし） | アーカイブ用プロジェクトを渡されたら `InputError` で弾く | 種別違いが CLI 経路から入ると空の Timeline を書き出してしまうため、入口を 1 つ増やして塞いだ |

### 検証結果（実 ffmpeg）

* **サイドカー往復**: 正規化済みファイルから音声を抜き、VOD から切り直した映像へ貼り直したところ、
  **映像ストリームの MD5・音声ストリームの MD5 とも保存時のファイルと完全一致**した（尺も一致）。
  §3-1 の「結果は保存時と実質同一」は実測で裏づけられている。
* **速度**: 3 分クリップ 1 本あたり 切り出し 0.12 秒 + 多重化 0.11 秒 = **0.23 秒**。
  同じクリップの再正規化は 16.06 秒。10 本なら **2 秒 対 2 分 42 秒**。
* **保存 → 復元の通し**: アーカイブ用編集画面で保存 →`.media/` にサイドカー 2 件 → 中間ファイルを削除 →
  読み込み・復旧・検証まで **0.42 秒**、V1 クリップ 2/2 が生存、`normalize_file` の呼び出しは 0 回。
* **ゴミ箱**: 実ファイル・実フォルダを 1 回の `SHFileOperationW` でゴミ箱へ移動できることを確認（Q14）。

### 追加した自動テスト（42 件）

| ファイル | 件数 | 内容 |
|---|---|---|
| `tests/test_archive_project_resume.py` | 10 | サイドカー経路で全クリップ生存・**`normalize_file` を呼ばない**こと / サイドカー欠落・多重化失敗時のフォールバック / `use_source` / `rebuild_prepared` / 種別違いと欠落ファイルの拒否 / 拡張子の決定 / 尺の食い違い検知 |
| `tests/test_project_library.py` | 20 | 走査（履歴 + フォルダの和集合・自動保存の除外・種別の一次判定・上限と切り捨て件数）/ 概要（種別・クリップ数・尺・素材が無いときの元動画への読み替え・アーカイブの `vod_start` 加算・壊れたファイル）/ リネーム（3 点の改名・**JSON 内パスの張り替えで開ける状態を保つ**・不正名・衝突・巻き戻し）/ 削除 / 履歴の除去と差し替え / 種別判定と既定名 |
| `tests/test_trash.py` | 6 | 実ファイルとフォルダのゴミ箱移動 / 欠落パス・長すぎるパス・空入力 / 非 Windows |
| `tests/test_archive_timeline.py`（追記） | 5 | テーマコマンドの適用・変化なし・未知クリップ / `curve` の間引き / `media_role` |
| `tests/test_command_stack_clean.py`（追記） | 2 | テーマ変更で未保存になり、Undo で `source` ごと戻ること |

既存 393 件と合わせて **435 件すべて成功**（`python -m unittest discover -s tests`）。
