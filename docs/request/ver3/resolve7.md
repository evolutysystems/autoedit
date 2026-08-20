# resolve7（ver3） — 画像オーバーレイの大きさ変更 / Timeline の保存と再編集 / スクラブ音声 修正設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request7.md`（**（追加）分の「再生ヘッドドラッグ中の音声」を含む**）
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/CLAUDE.md`「いきなり実装を開始しない」）。
* 対象画面は **クリップ用 `TimelineEditorDialog`**。アーカイブ用 `ArchiveTimelineDialog` は
  前者の派生（resolve5 §5.4）のため、**大きさ変更（要望 1）とスクラブ音声（追加要望）は
  1 箇所の実装で両方に効く**。
  **保存・再編集（要望 2）はクリップ用のみを対象**とする。理由は §3-6（素材が一時ファイルのため）。
* 記載の実装位置はすべて本リポジトリの実コードを読んで確認した（2026-08-18 時点）。
  推測で書いた箇所は無い。決めきれない点は推測実装せず **§9 確認事項** に列挙した。
* **setting.json は本書では書き換えない**。追加キーは `DEFAULT_SETTINGS` へ足し、
  `load_settings()` の既定マージで自動補完させる（`settings_window.py:593`）。

---

## 1. 要望（request7.md）と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **C1** | Timeline に載せた**画像の大きさ**をプレビュー画面で変更できるようにする | 機能追加 |
| **C2** | その際 **縦横比は固定**にする | 制約 |
| **C3** | **Timeline（編集過程）を保存**できるようにする | 機能追加 |
| **C4** | 保存したものを **main_window から選んで再編集**できるようにする | 機能追加 |
| **C5** | 編集画面を閉じるとき、**未保存の編集があればポップアップ**を出す | 機能追加 |
| **C10** | **再生ヘッドをドラッグしている間も音を出す**（ゆっくり動かせばスローで鳴るイメージ）＝ スクラブ（ジョグ）再生 | 機能追加 |

要望文に無いが、実装に必ず要る論点:

| ID | 論点 | 理由 |
|---|---|---|
| **C6** | 大きさを**数値でも指定**できる（インスペクタ）／**既定へ戻す** | ドラッグだけでは 100% や原寸へ正確に戻せない |
| **C7** | 大きさを**出力（レンダリング）へ通す** | 画面で変えても出力に出なければ意味が無い → §2.1 のとおり**既に通っている** |
| **C8** | 再編集した結果を **書き出しまで到達させる** | 「開いて編集できる」だけでは成果物が出ない |
| **C9** | 保存したプロジェクトが参照する**素材（中間ファイル）の復旧** | 現状のままでは開いても V1 が全滅する（§2.3）。**本要望の最大の壁** |
| **C11** | 再生ヘッドドラッグの**開始と終了を画面へ伝える** | 現状はドラッグ中かどうかを誰も知らない（`set_playhead` が飛んでくるだけ）。鳴らし始め／止めの契機が無い |
| **C12** | スクラブ位置の**音声を即座に用意する** | 音声はチャンク生成（ffmpeg）が要る。掴んだ瞬間に手元に無ければ鳴らない |

---

## 2. 現状分析

### 2.1 画像は「置ける・動かせる」が「大きさは変えられない」

| 層 | 現状 | 位置 |
|---|---|---|
| モデル | `Transform.scale` を**既に持っている**（キャンバス幅に対する比率） | `model.py:90` |
| 出力 | `scale={target_w}:-2` で**既に scale を消費している** | `renderer.py:405` |
| プレビュー描画 | `scale` から表示幅を決めて描いている（読むだけ） | `preview_items.py:117` |
| 操作 | 位置は `MoveOverlay` で変えられる。**大きさを変えるコマンドが無い** | `commands.py:766` |
| インスペクタ | 欄は**字幕専用**。画像を選ぶと「位置を既定へ戻す」しか出ない | `timeline_editor_dialog.py:454-530` |
| D&D 直後 | `Transform()` = `scale 1.0` = **キャンバス幅いっぱい** | `commands.py:592` |

つまり **足りないのは操作（コマンドと UI）だけ**で、モデルにも出力にも手を入れる必要が無い。

**縦横比（C2）について重要な事実**:

* `Transform.scale` は**スカラー 1 つ**（`scale_x` / `scale_y` は無い）。
  したがって **縦横比は構造的に固定**であり、「固定を実装する」のではなく
  **「固定でない指定を UI から与えない」**だけでよい。
* 出力側の `scale={target_w}:-2` の `-2` は「アスペクト比を保ち、高さを偶数へ丸める」指定。
  よって**出力も最初から縦横比固定**である。丸めにより最大 1px（＝映像高さの 0.1% 未満）
  だけ画面の見えと差が出るが、これは既存挙動であり本要望の範囲では変更しない。

### 2.1.1 大きさの消費者は renderer だけ

* `resolve_export.build_timeline_spec()`（`resolve_export.py:460`）は **元動画のクリップと字幕しか
  FCPXML へ入れない**。オーバーレイは対象外である旨を WARNING で出している（`resolve_export.py:482`）。
* したがって **大きさの変更は DaVinci Resolve 出力に影響しない**。既存の出力結果は 1 バイトも変わらない。

### 2.2 保存は「既にできている」。読み戻す経路が無いだけ

`src/timeline/project_io.py`（368 行）には **保存・読込・検証・版差吸収がすべて揃っている**。

| 機能 | 実装 | 本番コードから呼ばれているか |
|---|---|---|
| 書き出し `save()` / `to_json()` | あり | **呼ばれている**（`pipeline_runner.py:181` / `:228`） |
| 読み込み `load()` / `from_json()` / `from_dict()` | あり | **呼ばれていない**（テストのみ） |
| 検証 `validate()` | あり | `from_dict` 内で常に実行 |
| マイグレーション `migrate()` | あり（v1 のみ） | 同上 |

* 実は **毎回のパイプライン実行でプロジェクト JSON が既に出力されている**
  （`<出力先>/<元動画名>.timeline.json`、`project_io.py:48 default_project_path`）。
  Timeline 構築直後と「決定」後の 2 回、上書き保存している。
* ファイルは残っているのに、**開く手段が無いだけ**である。
  `project_io.py:5` のコメント自身が「アプリが読み戻す経路は持たない（回答 Q6: 常に作り直し）。
  ただし将来のプロジェクト再編集機能へそのまま繋がるよう実装しておく」と宣言している。
  **本要望はその「将来」そのもの**であり、下地は用意済みである。

足りないもの:

1. 編集画面の**保存ボタン / Ctrl+S**（現状は保存操作が UI に無い）
2. main_window の**開く導線**
3. 読み戻したあと**書き出しまで到達する経路**（§3-4）
4. **素材の復旧**（§2.3）

### 2.2.1 使われていない設定キー

| キー | 宣言 | 実装 |
|---|---|---|
| `timeline.autosave_sec` | `settings_window.py:257`（既定 0） | **未使用**。コメントに「読み戻し経路が無いため」と書かれている |
| `timeline.keep_project_file` | `settings_window.py:258`（既定 true） | **未使用** |

本要望で読み戻し経路ができるため、`autosave_sec` は**実装の意味が生まれる**（§5.9 / 任意）。

### 2.3 いちばんの壁 —— 保存済みプロジェクトの素材は一時ファイル

```
build() が m1 として登録する素材 = context.current_video_path()
   = ラウドネス正規化後の中間ファイル (working_dir/loudness_normalized.mp4 相当)
                                        ↑ PipelineContext の TemporaryDirectory
                                        ↑ cleanup() で消える (pipeline_context.py:129)
```

* `timeline.source` には `input_path`（利用者が選んだ元動画）と `media_path`（中間ファイル）の
  **両方が入っている**（`builder.py:323`）。`input_path` は消えない。
* 保存済み JSON を後から開くと、`m1` の `path` が存在しないため
  `_validate_media()` が**該当クリップを片っ端から `enabled=False` にする**（`project_io.py:241`）。
  V1 が全滅し、開いても何も映らず、書き出しもできない。
* 一方 **OP/ED・D&D で足した画像や動画は利用者のパス**を指しているため、通常そのまま残る。
  消えるのは実質 `m1` だけである。

救いになる事実:

* ラウドネス正規化は **`-c:v copy`**（`loudness_normalizer.py:211`）。
  **映像は無劣化コピーで、時間軸は元動画と完全に同一**。
  したがって `source_in / source_out`（＝編集点）は**元動画に対してそのまま使える**。
* 正規化は入力と設定が同じなら**決定的**（2 パス測定 + 線形適用）。
  よって **元動画から作り直せば、保存時と同じ素材が再現できる**。

### 2.4 閉じるときの確認は「破棄しますか」だけ

```python
# timeline_editor_dialog.py:352
def reject(self):
    if self.controller.is_dirty():
        answer = QMessageBox.question(self, "編集を破棄しますか", …Yes/No…)
```

* 選択肢が **Yes / No の 2 択**で、**「保存する」が無い**。
* `× ボタン`も `QDialog` の既定で `reject()` に落ちるため同じ経路。ここは既に正しい。
* `is_dirty()` は `execute()` で `True` になるだけで、**戻す手段が無い**（`timeline_controller.py:55/101`）。
  保存しても「未保存」のままになるため、そのままでは C5 に使えない。

### 2.5 再生ヘッドのドラッグは「完全に無音」

再生ヘッドを動かす経路は 2 つあり、**どちらも音には一切触れていない**。

| 経路 | 実装 | 終了の通知 |
|---|---|---|
| タイムルーラのドラッグ | `TimelineRuler.mouseMoveEvent` → `seek_requested` → `controller.set_playhead`（`timeline_view.py:220/233/936`） | **無い**（`mouseReleaseEvent` を実装していない） |
| トラック area の空き領域のドラッグ | `_DRAG_PLAYHEAD` → `controller.set_playhead`（`timeline_view.py:603/650`） | `mouseReleaseEvent` はあるが、`_DRAG_PLAYHEAD` は**何もせず抜ける**（`:699`） |

* `set_playhead()` は `playhead_moved` を出すだけで（`timeline_controller.py:270`）、
  受け手の `PreviewPanel._on_playhead_moved` は**フレーム取得・オーバーレイ・時刻表示しか更新しない**
  （`preview_panel.py:342`）。音を鳴らす経路そのものが存在しない。
* つまり足りないのは 2 つ:
  1. **ドラッグの開始・終了を知る手段**（C11）
  2. **その間、位置に追従して音を鳴らす仕組み**（C10/C12）

#### 2.5.1 鳴らすための材料（音声チャンク）は既にある

* 音声は `AudioChunkSource` が Timeline から ffmpeg で切り出して生成し、
  `QMediaPlayer` が再生する（`preview_panel.py:624 _start_playback`）。
* 生成済みチャンクは **3 本までキャッシュ**され、`cached(start, length)` で即座に引ける
  （`audio_source.py:75` / `_chunk_limit`）。
* 既定の音声形式は **24kHz mono の WAV（`pcm_s16le`）**（`settings_window.py` timeline.preview）。
  非圧縮 PCM のため **`setPosition()` の位置決めが正確**で、スクラブ用途に都合が良い。
* resolve6 の実測（同 §2.2(b)）より、**短いチャンクは十分速く作れる**:

| 生成するもの | 実測 |
|---|---:|
| 6 秒ぶん（断片 3 本・wav 24kHz mono） | **0.10 s** |
| 30 秒ぶん（断片 15 本・wav 24kHz mono） | 0.42 s |
| 30 秒ぶん（断片 40 本・wav 24kHz mono） | 0.76 s |

* さらに**未編集のあいだは認識用音声（Timeline 全長ぶん）をそのまま使える**
  （`audio_source.py:62 _can_reuse_initial`）。この状態なら**掴んだ瞬間にどこでも鳴る**。
* したがって C12 は「新しい生成方式を作る」話ではなく、
  **既存のチャンク機構へ短い長さで要求を出し、無い間は黙る**という設計で足りる。

#### 2.5.2 気をつける必要がある既存の作り

| 箇所 | 内容 | スクラブでの扱い |
|---|---|---|
| `_on_audio_position`（`preview_panel.py:698`） | 音声位置をマスタークロックにして**再生ヘッドを動かす**。`self._rate <= 0` なら即 return | スクラブは `_rate` を 0 のままにする → **音が再生ヘッドを動かさない**（マウスだけが動かす） |
| `_on_audio_status`（同 `:727`） | チャンク終端で次チャンクへ継ぐ。`_rate <= 0` なら即 return | 同上。スクラブでは働かない |
| `_on_audio_error`（同 `:747`） | 失敗すると**無音タイマー再生を始めてしまう** | スクラブ中は再生を始めないようガードが要る |
| `_set_rate`（同 `:660`） | `playing_changed` を出し、**Timeline とインスペクタを丸ごと無効化**する | スクラブは再生ではないため `playing_changed` を**出さない**（ドラッグ中に UI が固まる副作用を避ける） |
| `逆再生は音なし`（resolve2 §3-6-2） | `QMediaPlayer` が負の再生レートに非対応 | スクラブも同じ制約を受ける（§3-7 で回避策を決める） |

---

## 3. 方式選定

### 3-1.【C1/C2】大きさは「四隅ハンドル + スカラー scale」で変える

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | プレビュー上の**四隅**にハンドルを出し、ドラッグで `transform.scale` を変える | 縦横比が構造的に固定される。既存モデル・出力に変更なし |
| B | `scale_x` / `scale_y` を新設し、辺ハンドルも出す | 縦横比固定という要望に反する方向。モデル・出力・JSON すべてに波及 |
| C | インスペクタの数値入力のみ | 「プレビュー画面で配置を設定する（その延長で大きさも）」という要望に合わない |

* **辺（上下左右）のハンドルは出さない**。出すと「縦だけ伸ばす」操作を利用者に見せてしまう。
  **四隅だけ**にすることが、縦横比固定の UI 上の担保である。
* 数値入力（インスペクタ）は**併設**する（C6）。ドラッグでは 100%・原寸へ正確に戻せないため。

### 3-2.【C1】リサイズの基準点は「中心固定」

* このアプリのオーバーレイは **`transform.x/y` が中心**を指す（`preview_items.py:123` / `renderer.py:416`）。
* 中心固定にすると **大きさを変えても位置が動かない**。
  「まず置いて、あとから大きさを合わせる」という本要望の作業順に素直に合う。
* 対角固定（掴んだ角の反対側を固定）にする場合は、`ResizeOverlay` が `x/y` も同時に持てば実現できる
  （コマンドの引数を増やすだけ）。どちらが良いかは **§9 Q1** で確認する。既定は中心固定とする。

### 3-3.【C1】ドラッグ中は `setScale`、確定時に描き直す

* 現行は `QPixmap.scaledToWidth(..., SmoothTransformation)` で拡縮している（`preview_items.py:119`）。
  これは綺麗だがコストが高く、**ドラッグ中に毎フレーム実行してはならない**
  （resolve6 §5.8 で「拡縮は `setScale` で行い `QPixmap.scaled` は使わない」と決めた理由と同じ）。
* よって:
  * **ドラッグ中** … `QGraphicsItem.setScale()` で見た目だけ追従（描画時処理・ほぼ無料）
  * **確定時** … `ResizeOverlay` を 1 回積む → `timeline_changed` → `_rebuild_overlays()` が
    `scaledToWidth` で作り直す（現行と同じ綺麗な絵に戻る）
* 履歴も同じ規約に従う。**ドラッグ中はコマンドを積まない**（`MoveOverlay` と同じ / resolve.md §6.5-1）。

### 3-4.【C3/C4/C8】再編集は「既存パイプラインの後半だけを走らせる」

新しい編集画面や新しいレンダラは作らない。**既存の `_review_timeline` → `renderer` → `output_writer`
をそのまま使い、前半（正規化・無音検出・音声認識・Timeline 構築）を飛ばす**入口を足す。

```
main_window / クリップ用タブ
  ├ ▶ 実行 ─────────────────→ run_pipeline()        （従来 / 無変更）
  └ 編集の続きを開く ────────→ run_from_project()    （追加）
                                    │
                                    ├ ① プロジェクト読込   project_io.load_project()
                                    ├ ② 素材の復旧        media_recovery.recover()      ← §3-5
                                    ├ ③ Timeline 編集画面  _review_timeline()（既存関数をそのまま）
                                    ├ ④ レンダリング       renderer.render()（既存）
                                    └ ⑤ 出力              output_writer.run()（既存）
```

* ③ は **既存の `TimelineReviewBridge` をそのまま使う**（`main_window.py:185`）。
  ワーカースレッド → メインスレッドの橋渡し・モーダル表示・キャンセル扱いすべて再利用できる。
* ④⑤ は引数が `(timeline, context)` と `(context)` だけなので、`PipelineContext` を
  `input_path = source.input_path` で組み直せばそのまま動く。
* 進捗は `run_ffmpeg_progress` 経由の既存経路をそのまま通る（`docs/CLAUDE.md` の要件）。

### 3-5.【C9】素材の復旧 —— 既定は「元動画から作り直す」

| 案 | 内容 | 長所 | 短所 | 判定 |
|---|---|---|---|---|
| **A（採用・既定）** | 開くときに `source.input_path` から**ラウドネス正規化をやり直す** | 追加のディスク消費ゼロ／保存時と同一の素材が再現できる | 開くのに 1 パスぶんの時間がかかる | **既定** |
| B（任意設定） | **保存時に中間ファイルをプロジェクトの隣へ複製**しておく | 開くのが速い | 動画 1 本ぶんのディスクを恒久的に消費する | `keep_media=true` で選択可 |
| C（任意設定） | 中間ファイルの代わりに**元動画をそのまま使う** | 即座に開ける | **音量が正規化前になる**＝保存時と出力が変わる | `use_source` で選択可 |

* 判定順（`media_recovery.recover()`）:
  1. `media.path` がそのまま在る → 何もしない（OP/ED・画像は通常ここで終わる）
  2. **プロジェクトファイルからの相対パス**（保存時に併記する `path_rel`）で見つかる → 採用
     （プロジェクトごとフォルダを移した場合に効く）
  3. 対象が **本編素材**（`media.id == source.media_id`）で、`source.media_path != source.input_path`
     （＝中間ファイルだった証拠）かつ `input_path` が在る → **方針に従って復旧**
     （`renormalize` … 案 A ／ `use_source` … 案 C）
  4. それ以外（OP/ED・画像の移動・削除） → **再リンク画面**（GUI 経路のみ / §5.8）。
     コールバック未注入なら**従来どおり**該当クリップを無効化して WARNING（現行 `_validate_media` の挙動）
* 正規化のやり直しは `loudness.enabled=false` の環境では**そもそも中間ファイルが作られない**
  （`loudness_normalizer.py:239` が何もせず戻る）ため、判定 3 に入らない。この場合 `media_path == input_path`
  で保存されているので、判定 1 で解決する。

### 3-6.【対象範囲】アーカイブ用画面は今回対象外

`ArchiveTimelineDialog` を保存対象から外す理由（推測ではなく実装上の事実）:

* アーカイブ用 Timeline の素材は **クリップごとに切り出した一時ファイル**
  （`archive/timeline_builder.py:80` が `entry["normalized_path"]` を probe している）で、
  `clip_writer.write_clips()` の `TemporaryDirectory` 内に **N 本**ある（`clip_writer.py:639`）。
  復旧するには VOD からの再切り出し＋再正規化を N 本ぶん行う必要がある。
* さらに、**Timeline モデルの外に状態がある**。`prepared`（採点・区間）・`curve`（採点グラフ）・
  `_themes`（クリップごとのテーマ文字列）は JSON に含まれない（`archive_timeline_dialog.py:59-63`）。
* したがって「保存 UI だけ継承させる」と、**開けないファイルを作れてしまう**。
  基底へフック `_project_save_enabled()`（既定 `True`）を置き、アーカイブ用は `False` を返して
  保存 UI を出さない。閉じるときの確認は従来どおり（破棄 Yes/No）とする。
* 対応する場合の道筋は §11 将来拡張に書く。可否は **§9 Q4** で確認する。

### 3-7.【C10】スクラブ音声は「粒（グレイン）を撒く」方式にする

「ドラッグ位置の音を鳴らす」実装には 3 つの選択肢がある。

| 案 | 内容 | 長所 | 短所 | 判定 |
|---|---|---|---|---|
| **A（採用）** | 一定間隔（既定 60ms）で `setPosition(再生ヘッド位置)` し、**その場から等倍で鳴らし続ける**。次の間隔でまた位置を合わせ直す | 実装が小さい／**音程が変わらない**／**逆方向のドラッグでも鳴る**／バックエンド依存が無い | ゆっくり動かすと同じ 60ms を繰り返す「ジョグ音」になる | **採用** |
| B | ドラッグ速度に `playbackRate` を合わせる（0.3 倍なら 0.3 倍速） | 「スロー再生」の語感に最も近い | **音程が下がる**／Windows の QMediaPlayer は低レート（< 0.5）で音を出さないことがある／**逆方向は原理的に不可**（負レート非対応 / resolve2 §3-6-2） | 設定で選べるようにするが既定にしない |
| C | 自前で PCM を読み、リサンプルして `QAudioSink` へ流す | 完全に自由（逆再生・任意速度） | **音声パイプラインを新規に作ることになる**。既存の QMediaPlayer 経路と二重になり、要望に対して過剰 | 採らない |

* **案 A は NLE のジョグ（スクラブ）そのもの**である。要望の
  「ゆっくり動かせばスローで音声がでるイメージ」は、**動かした量ぶんだけ音が進む**
  という体験を指しており、案 A はそれを満たす（動かさなければ鳴らない・速く動かせば速く進む）。
* **止まったら黙る**。`scrub_hold_ms`（既定 120ms）動きが無ければ一時停止する。
  掴んだまま止めているのに同じ 60ms がループし続けるのは不快なため。
* **逆方向のドラッグでも鳴る**。粒は常に前方向へ再生するため、
  左へ動かしても「その位置の音」が聞こえる。厳密な逆再生音ではないが、
  **現状の倍速逆再生（完全に無音）より確実に良い**。厳密な逆再生は §11 将来拡張。
* 案 B は `scrub_rate_mode="match"` として設定で選べるようにする（既定 `"grain"`）。
  環境依存で無音になり得るため既定にはしない。

### 3-8.【C11/C12】ドラッグの開始・終了は Controller に集約する

* ドラッグ経路は 2 つある（ルーラ / トラック area・§2.5）。**両方に音の制御を書かない**。
  `TimelineController` に **`begin_scrub()` / `end_scrub()` / `scrub_changed(bool)`** を置き、
  ビュー側は「掴んだ・離した」を伝えるだけにする。音は従来どおり `PreviewPanel`
  （＝`QMediaPlayer` を持つ唯一の場所）が受け持つ。
* **取りこぼしへの保険**: マウスを画面外で離す・別ウィンドウへフォーカスが移る等で
  `mouseReleaseEvent` が来ない場合に音が鳴りっぱなしにならないよう、
  スクラブ用タイマーが毎回 `QApplication.mouseButtons()` を見て、
  **ボタンが押されていなければ自動で終了**する。
* **音の用意（C12）** は次の順で、**待たせない・嘘を鳴らさない**を守る:
  1. `AudioChunkSource.cached(playhead, grain)` に当たる → 即座に鳴らす
     （未編集なら認識用音声が全長を覆うため、ここで必ず当たる / §2.5.1）
  2. 当たらない → **短いチャンク（既定 4 秒）**を 1 本だけ非同期で要求し、
     届くまでは**黙る**（既存の `_AudioWorker` と `_audio_job` の仕組みをそのまま使う）
  3. ドラッグ位置が現在のチャンクの外へ出た → **いったん黙って**次のチャンクを要求する
     （範囲外の位置を鳴らすと「別の場所の音」が出てしまうため）
  4. 生成要求は**同時に 1 本まで**。ドラッグ中に要求を積み上げない
     （既存の `_audio_worker.isRunning()` ガードと同じ規約）
* **映像の追従**は既存の仕組みに任せる。フレームワーカーは最新要求だけを処理するため
  （`preview_panel.py:80`）、速いドラッグでも詰まらない。
  ただしスクラブ中は再生中と同じ理由で軽い方が良いため、
  `frame_source.set_playing(True)` 相当（resolve6 R6 の縮小・R1 の前進デコード）を
  **スクラブ中も有効**にする（設定 `scrub_low_quality_frames`、既定 true）。
  ドラッグを終えた瞬間に原寸で取り直すのも再生停止時と同じ扱いにする。

---

## 4. 設計方針

1. **モデルと出力には手を入れない**。`Transform.scale` も `renderer` も既に正しい（§2.1）。
   今回足すのは**操作・UI・入出力の導線**だけである。
2. **既存の編集規約に完全に乗せる**。UI はコマンドを積む以外の方法でモデルを触らない
   （`TimelineController` が唯一の窓口）。大きさ変更も Undo/Redo が一様に効く。
3. **保存形式は増やさない**。`schema_version` は **1 のまま**。追加するのは
   `MediaRef.path_rel` と `source.media_role` という**任意キーのみ**で、
   `from_dict` は未知キーを無視するため旧ファイル・旧アプリの双方と共存できる（§8）。
4. **既存の自動保存を止めない**。パイプラインが今も行っている 2 回の保存（`pipeline_runner.py:181/:228`）
   はそのまま残し、手動保存はそこへ**重ねる**形にする。
5. **開けないファイルを作らせない**。保存できる画面 = 開ける画面に限定する（§3-6）。
6. **音の経路を二重に持たない**。スクラブ音声は新しい再生機を作らず、
   既存の `QMediaPlayer` と `AudioChunkSource` をそのまま使う。
   再生（`_rate` を使う経路）とスクラブは同じ機器を奪い合うため、**掴んだら再生は止める**。
7. ハードコードしない。追加する寸法・上限・方針はすべて `setting.json`（§7）。

---

## 5. 詳細設計

### 5.1 新規・変更ファイル一覧（予定）

| 区分 | ファイル | 内容 | 要件 |
|---|---|---|---|
| 変更 | `src/timeline/commands.py` | `ResizeOverlay` 追加 | C1 |
| 変更 | `src/gui/timeline/preview_items.py` | 四隅ハンドル `_ResizeHandleItem` / `ImageOverlayItem` の対応 | C1/C2 |
| 変更 | `src/gui/timeline/preview_panel.py` | ハンドルの配線・再生中の無効化・右クリック「大きさ」／**スクラブ音声の駆動** | C1/C10 |
| 変更 | `src/gui/timeline/timeline_view.py` | **ドラッグの開始・終了を Controller へ伝える**（ルーラ / トラック area） | C11 |
| 変更 | `src/gui/timeline/timeline_controller.py` | `resize_overlay()` / `mark_saved()` / `is_modified()` / **`begin_scrub()`・`end_scrub()`・`scrub_changed`** | C1/C5/C11 |
| 変更 | `src/gui/timeline/timeline_editor_dialog.py` | インスペクタの大きさ欄・保存ボタン・Ctrl+S・タイトル・閉じる確認 | C1/C3/C5/C6 |
| 変更 | `src/gui/timeline/archive_timeline_dialog.py` | `_project_save_enabled()` を `False` に | §3-6 |
| 変更 | `src/timeline/project_io.py` | `path_rel`/`media_role` の書き出し・`load_project()`・`created_at` 維持・`validate` 抑止 | C3/C9 |
| **新規** | `src/timeline/media_recovery.py` | 素材の復旧（相対パス → 再正規化 → 再リンク） | C9 |
| **新規** | `src/gui/timeline/missing_media_dialog.py` | 見つからない素材の再リンク画面（任意・Phase 5） | C9 |
| 変更 | `src/pipeline/pipeline_runner.py` | `run_from_project()` 追加・`_review_timeline` の戻り値拡張 | C4/C8 |
| 変更 | `src/gui/main_window.py` | 「編集の続きを開く」導線・`ProjectResumeWorker`・再リンク橋渡し | C4 |
| 変更 | `src/timeline/builder.py` | `timeline_config()` に `overlay` / `project` セクションを追加 | 全般 |
| 変更 | `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ新キー追加 | 全般 |
| 変更なし | `src/timeline/model.py` / `renderer.py` / `export/resolve_export.py` | `scale` は既に通っているため無変更 | C7 |

### 5.2 大きさ変更コマンド（`commands.py`）

```python
# プレビュー上のハンドルドラッグ / インスペクタで大きさを変える (resolve7 §3-1)
# 縦横比は transform.scale がスカラー 1 つであることで構造的に保たれる。
# ドラッグ確定時に 1 回だけ積む (ドラッグ中は積まない = 履歴を汚さない / MoveOverlay と同じ)。
class ResizeOverlay(Command):

    label = "大きさの変更"

    # scale: キャンバス幅に対する比率 (1.0 = キャンバス幅いっぱい)
    def __init__(self, clip_id, scale, min_scale=0.02, max_scale=4.0):
        self._clip_id = clip_id
        self._scale = float(scale)
        self._min = float(min_scale)
        self._max = float(max_scale)

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        # 字幕の大きさはフォントサイズで表すため、ここでは扱わない (transform は位置のみ)
        if clip is None or isinstance(clip, SubtitleClip) or not hasattr(clip, "transform"):
            return False
        # ベース (V1) のクリップはキャンバスへ合わせて描かれ transform を見ない (renderer.py:344)
        track = timeline.track_of_clip(clip.id)
        base = timeline.base_video_track()
        if track is None or (base is not None and track.id == base.id):
            return False
        value = min(max(self._scale, self._min), self._max)
        if abs(float(clip.transform.scale or 1.0) - value) <= _EPS:
            return False
        clip.transform.scale = value
        return True
```

`TimelineController`:

```python
# オーバーレイの大きさを変える (resolve7 §5.2)
def resize_overlay(self, clip_id, scale):
    overlay = self._cfg["overlay"]
    return self.execute(commands.ResizeOverlay(
        clip_id, scale, overlay["min_scale"], overlay["max_scale"]))
```

### 5.3 四隅ハンドル（`preview_items.py`）

```python
# 拡大縮小ハンドル (四隅)。ビューは fitInView でキャンバス全体を縮めて表示するため、
# ItemIgnoresTransformations を付けて「画面上で常に同じ大きさ」に見えるようにする。
class _ResizeHandleItem(QGraphicsRectItem):

    def __init__(self, owner, corner, size_px):
        …
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setCursor(Qt.SizeFDiagCursor if corner in ("tl", "br") else Qt.SizeBDiagCursor)

    # 押した瞬間に親の移動を止める (掴んだ角でクリップごと動いてしまわないように)
    def mousePressEvent(self, event):
        self._owner.begin_resize()
        event.accept()

    def mouseMoveEvent(self, event):
        self._owner.update_resize(event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event):
        self._owner.finish_resize()
        event.accept()
```

`ImageOverlayItem` の追加分:

```python
# 大きさ確定を伝える先 (PreviewPanel が設定する / move_finished と同じ規約)
resize_finished = None

# 中心を固定したまま、カーソル位置から新しい拡大率を求める (resolve7 §3-2)
#   カーソルが角へ吸い付くよう、横・縦それぞれで必要な拡大率の大きい方を採る。
#   縦横比は「幅から高さを決める」ため常に保たれる。
def _scale_from_cursor(self, scene_pos):
    canvas_w, canvas_h = self._canvas_size
    center = self.sceneBoundingRect().center()
    dx = abs(scene_pos.x() - center.x())
    dy = abs(scene_pos.y() - center.y())
    ratio = self._native_height / max(self._native_width, 1)   # 素材の縦横比
    scale_x = 2.0 * dx / canvas_w
    scale_y = 2.0 * dy / (canvas_w * ratio)
    return max(scale_x, scale_y)
```

* ドラッグ中は `setScale(new / current)` で見た目だけ更新する（§3-3）。
* 確定時に `resize_finished(clip_id, new_scale)` を 1 回だけ呼ぶ。
* ハンドルは **選択中のときだけ**表示する（`itemChange` の `ItemSelectedHasChanged` で切り替え）。
* **字幕（`SubtitleOverlayItem`）にはハンドルを出さない**。字幕の大きさは
  既存の「サイズ」欄（フォントサイズ）で変える。二重の指定手段を作らない。

### 5.4 プレビュー側の配線（`preview_panel.py`）

```python
# _rebuild_overlays() 内。位置と同じ規約で大きさの確定も受ける。
item.move_finished = self._on_overlay_moved
item.resize_finished = self._on_overlay_resized          # 追加
item.set_resizable(not self.is_playing())                # 再生中は掴めない

# ハンドル確定 → コマンドを積む
def _on_overlay_resized(self, clip_id, scale):
    self._controller.resize_overlay(clip_id, scale)
```

* 再生中は編集を止める既存方針（§4-8）に合わせ、`_set_rate()` の再生状態変化で
  ハンドルの有効・無効を切り替える。
* 右クリックメニュー（`preview_panel.py:478`）へ「大きさ」サブメニューを足す:
  `原寸` / `50%` / `100%（画面幅）`。字幕を右クリックしたときは出さない。

### 5.5 インスペクタの大きさ欄（`timeline_editor_dialog.py`）

現行は字幕用の欄しか無いため、**オーバーレイ用の欄をもう 1 つの器として足す**
（字幕用 `subtitle_widget` と同じ作りで、表示・非表示を切り替える）。

```
┌ 追加メディア ───────────────┐
│ c12                            │
│ 開始 0:00:12.00 / 尺 5.00 秒   │
│ 素材 logo.png                  │
├────────────────────────────────┤
│ 大きさ [ 100.0 ] %             │   ← QDoubleSpinBox (editingFinished で確定)
│        1920 × 1080 px 相当     │   ← 目安表示 (theme.mark_note)
│  [原寸]  [画面幅に合わせる]     │
├────────────────────────────────┤
│      [位置を既定へ戻す]         │   ← 既存
└────────────────────────────────┘
```

```python
# 「原寸」= 素材のピクセル数どおりの大きさ (canvas 幅に対する比率へ直す)
def _scale_for_native(self, clip):
    media = self._controller.timeline.media_by_id(clip.media_id)
    if media is None or not media.width:
        return None
    return float(media.width) / float(self._controller.timeline.width)
```

* 表示条件: `Clip` かつ **ベース映像トラック以外**（＝オーバーレイ）。
  ベース（本編・OP・ED）は `renderer` が `transform` を見ないため（`renderer.py:344`）、
  **欄自体を出さない**。出すと「変えたのに出力が変わらない」という嘘になる。
* `AudioClip` / `SubtitleClip` では非表示（従来どおり）。

### 5.6 保存の状態管理（未保存判定 / C5）

`CommandStack` へ「保存点」を持たせる。Qt の `QUndoStack.setClean()` と同じ考え方。

```python
class CommandStack:

    def __init__(self, limit=100):
        …
        # 保存済みの位置 (= その時点の undo スタックの深さ)。None = もう一致し得ない
        self._clean_depth = 0

    def push(self, timeline, command):
        …
        # 保存点より浅い位置から新しい操作を積む = 保存点を含む枝を捨てた
        # → 以後どう操作しても保存時の状態には戻れないため、印を無効化する
        if self._clean_depth is not None and len(self._undo) < self._clean_depth:
            self._clean_depth = None
        self._undo.append((command.label, snapshot))
        if len(self._undo) > self._limit:
            self._undo.pop(0)
            # 先頭を捨てると深さの基準がずれるため保存点も 1 つ手前へ寄せる
            if self._clean_depth is not None:
                self._clean_depth -= 1
                if self._clean_depth < 0:
                    self._clean_depth = None
        …

    # 現在保存済みの状態か (Undo で保存点まで戻った場合も True になる)
    def is_clean(self):
        return self._clean_depth is not None and self._clean_depth == len(self._undo)

    def mark_clean(self):
        self._clean_depth = len(self._undo)
```

`TimelineController`:

```python
# 未保存の編集があるか (閉じるときの確認に使う / resolve7 §5.6)
def is_modified(self):
    return not self._stack.is_clean()

# 保存できた時点で呼ぶ
def mark_saved(self):
    self._stack.mark_clean()
    self.saved_state_changed.emit()      # タイトルの * を消すため
```

* 既存の `is_dirty()` は**残す**（`_dirty` は「一度でも編集したか」で、
  プレビュー音声の再利用判定などに使える別概念のため）。閉じる確認だけ `is_modified()` に替える。
* **再生ヘッドとズームは未保存扱いにしない**。JSON には保存されるが（`project_io.py:88-89`）、
  見ていた位置が違うだけでポップアップを出すのは煩わしいため。

### 5.7 編集画面の保存 UI（C3/C5）

**ボタン列**（`timeline_editor_dialog.py:115` の列へ追加）:

```
[元に戻す] [やり直す]        …        [保存] [名前を付けて保存…] [Resolve出力] [決定] [キャンセル]
```

**ショートカット**（`timeline.shortcuts` へ追加。`_shortcut_config()` は既定から
マージするため、既存 setting.json に項目が無くても自動で有効になる）:

| 動作 | 既定キー |
|---|---|
| `save` | `Ctrl+S` |
| `save_as` | `Ctrl+Shift+S` |

**タイトル**: `Timeline 編集 — <プロジェクト名>*`（`*` は未保存）。

```python
# 現在の Timeline をプロジェクトファイルへ書き出す (resolve7 §5.7)
# path 省略時は現在のプロジェクトパス、それも無ければ既定のパスを使う。
# 戻り値: 保存できたら True (失敗しても画面は閉じない / 閉じる確認から使うため)
def save_project(self, path=None, ask=False):
    target = path or self._project_path or project_io.default_project_path(
        self._settings, (self.controller.timeline.source or {}).get("input_path", ""))
    if ask:
        target, _ = QFileDialog.getSaveFileName(
            self, "Timeline を保存", target,
            f"Timeline プロジェクト (*{suffix});;すべてのファイル (*)")
        if not target:
            return False
    try:
        # 初回作成時刻は引き継ぐ (上書きのたびに created_at が変わらないように)
        project_io.save(self.controller.timeline, target,
                        generator=f"Stretheus {__version__}",
                        created_at=self._created_at)
    except TimelineError as e:
        QMessageBox.warning(self, "保存できません", str(e))
        return False
    self._project_path = target
    self.controller.mark_saved()
    self._update_window_title()
    self._remember_recent(target)      # setting.json の最近使った一覧へ積む
    return True
```

**閉じるときの確認（C5）**:

```python
def reject(self):
    if not self._confirm_close():
        return
    super().reject()

# 未保存があれば 3 択で確認する。閉じてよければ True。
def _confirm_close(self):
    if not self._project_cfg["confirm_on_close"] or not self.controller.is_modified():
        return True
    box = QMessageBox(self)
    box.setWindowTitle("保存していない編集があります")
    box.setText("Timeline に保存していない編集があります。")
    # パイプライン実行中は閉じると処理も止まる。再編集中は保存済みファイルが残る。
    box.setInformativeText(
        "閉じると編集内容は失われ、処理も中断されます。" if self._mode == "pipeline"
        else "閉じると保存していない編集は失われます。")
    save = box.addButton("保存して閉じる", QMessageBox.AcceptRole)
    discard = box.addButton("保存せずに閉じる", QMessageBox.DestructiveRole)
    cancel = box.addButton("編集に戻る", QMessageBox.RejectRole)
    box.setDefaultButton(cancel)          # 誤操作で消えないよう既定は「戻る」
    box.exec()
    if box.clickedButton() is cancel:
        return False
    if box.clickedButton() is save:
        return self.save_project()        # 保存に失敗したら閉じない
    return True
```

* **「決定」ではポップアップを出さない**。決定後はパイプライン側が必ず上書き保存するため
  （`pipeline_runner.py:228`）、未保存の状態にならない。
* 保存先を「名前を付けて保存」で変えていた場合に備え、決定時の戻り値でパスを返す（§5.10）。
* アーカイブ用は `_project_save_enabled()` が `False` のため、
  保存ボタン・ショートカットを作らず、確認も従来の Yes/No のままにする。

### 5.8 素材の復旧（`media_recovery.py` / 新規）

```python
# 保存済みプロジェクトが参照する素材を、開ける状態へ復旧する (resolve7 §3-5)
#   1. そのまま在る                       → 何もしない
#   2. プロジェクトからの相対パスで在る    → そのパスを採用
#   3. 本編素材 (中間ファイル) が無い      → 元動画から作り直す / 元動画を直接使う
#   4. それ以外が無い                     → 再リンク要求 (未注入なら従来どおり無効化)
# 戻り値: {"recovered": [...], "missing": [...], "renormalized": bool}
def recover(timeline, project_path, settings, context=None, relink_callback=None):
    …
```

* **② 相対パス**: 保存時に `MediaRef.to_dict()` へ `path_rel`（プロジェクトファイルからの相対）を
  併記しておき、復旧時に `os.path.join(os.path.dirname(project_path), path_rel)` を試す。
  プロジェクトと素材をまとめて別フォルダ・別 PC へ移した場合に効く。
* **③ 再正規化**: `loudness_normalizer.normalize_file(input_path, work_dir/…, settings,
  on_progress=context.progress_subcallback("素材の復旧"))` を使う。
  FFmpeg 実行はすべて `ffmpeg_runner.execute`（= `run_ffmpeg_progress`）経由のため、
  進捗はそのまま既存のバーへ出る。
* **④ 再リンク**: `missing_media_dialog.MissingMediaDialog`（Phase 5 / 任意）。
  一覧で「参照…」を押して差し替える。差し替えなければ従来どおり該当クリップを無効化して続行する。
* 復旧は **`validate()` の前**に行う必要がある。`from_dict()` は内部で `validate()` を呼び、
  素材が無いクリップを `enabled=False` にしてしまうため（`project_io.py:241`）。
  よって `from_dict(data, validate_timeline=False)` を追加し、復旧後に `validate(timeline)` を呼ぶ。
  既定は `True` のままなので既存の呼び出し（テスト含む）は一切変わらない。

### 5.9 プロジェクト I/O の追加分（`project_io.py`）

```python
# 保存: MediaRef に path_rel を併記する / source に media_role を残す
#   media_role = "normalized" (中間ファイル) | "original" (元動画そのもの)
#   → 開くときに「作り直すべき素材か」を推測ではなく記録から判断できる。
#     旧ファイル (キー無し) は media_path != input_path かどうかで判定する。
def to_json(timeline, generator="", created_at=None, project_path=None): …

# 読み込み: Timeline と メタ情報 (created_at / generator / schema_version) を返す
# 既存の load() は無変更で残す (テストと外部呼び出しの互換のため)
def load_project(path, min_clip_sec=…, validate_timeline=True):
    return timeline, meta
```

* `autosave_sec > 0` のとき、編集画面が `<プロジェクト>.autosave.json` へ定期保存する（任意・Phase 5）。
  開くときに本体より新しい autosave があれば「復元しますか」を出す。
  既定 0（無効）のため、実装しても既存挙動は変わらない。

### 5.10 パイプライン側（`pipeline_runner.py`）

```python
# 保存済みプロジェクトから再編集して書き出す (resolve7 §3-4)
# project_path: <元動画名>.timeline.json
# 戻り値: 出力動画パス
def run_from_project(project_path, settings, progress_cb=None,
                     timeline_review_callback=None, media_relink_callback=None):
    timeline, meta = project_io.load_project(project_path, validate_timeline=False)
    input_path = (timeline.source or {}).get("input_path", "")
    context = PipelineContext(input_path, settings, progress_cb, total_steps=2,
                              timeline_review_callback=timeline_review_callback)
    context.output_profile = {…}          # Timeline のキャンバスから復元する
    context.project_path = project_path
    try:
        # ① 素材の復旧 (足りなければ作り直す / 再リンクを求める)
        context.begin_step("素材の復旧")
        media_recovery.recover(timeline, project_path, settings, context,
                               relink_callback=media_relink_callback)
        project_io.validate(timeline, min_clip_sec=…)
        context.end_step("素材の復旧")

        # ② Timeline 編集画面 (既存関数をそのまま使う。キャンセルは PipelineCancelled)
        timeline = _review_timeline(context, timeline)
        context.timeline = timeline

        # ③ レンダリング → ④ 出力 (いずれも既存)
        context.begin_step("Timeline レンダリング")
        renderer.render(timeline, context)
        context.end_step("Timeline レンダリング")
        return output_writer.run(context)
    finally:
        _cleanup(context)
```

`_review_timeline()` の戻り値を拡張する（**後方互換あり**）:

```python
# 画面が「名前を付けて保存」で保存先を変えていたら、確定後の保存もそちらへ行う。
# 戻り値は Timeline (従来) / {"timeline", "project_path"} (追加) のどちらでもよい。
edited = callback({...})
if isinstance(edited, dict):
    context.project_path = edited.get("project_path") or context.project_path
    edited = edited.get("timeline")
```

* アーカイブ経路のコールバックが既に dict を返している（`clip_writer.py:667`）ため、
  「Timeline か dict か」を受ける書き方はこのリポジトリの既存作法である。

### 5.11 main_window 側の導線（C4）

`ClipTabWidget` の実行ボタン列の下へ 1 行足す（タブ構成・既存の行は変更しない）:

```
[動画ファイルをここにドラッグ&ドロップ、または「参照...」] [参照...]
[▶]
編集の続き: [ ~/output/sample.timeline.json      ▼ ] [開く...]      ← 追加
[progress bar]
待機中
```

* コンボは `timeline.project.recent`（最近使ったプロジェクト・最大 `recent_limit` 件）。
  空のときは行ごと非表示にせず、「（保存されたプロジェクトはありません）」を出して
  「開く…」だけ押せる状態にする。
* 「開く…」は `QFileDialog.getOpenFileName`（初期フォルダ = `timeline.project_dir`
  → `general.output_directory` → 直近のプロジェクトのフォルダ）。
* **`.timeline.json` の D&D も受ける**。既存の `_dropped_video_path()` は動画拡張子しか見ていない
  （`main_window.py:495`）ため、プロジェクト接尾辞にも一致させて分岐する。
* 実行は既存の `PipelineWorker` と同じ形の `ProjectResumeWorker`（`run_from_project` を呼ぶだけ）。
  進捗・完了・キャンセル・失敗の通知と `_set_running()` は現行と共通化する。
* `TimelineReviewBridge` はそのまま流用する（`main_window.py:185`）。

### 5.12 スクラブの状態管理（`timeline_controller.py` / `timeline_view.py`）

```python
class TimelineController(QObject):

    # 再生ヘッドのドラッグ中か (スクラブ音声の開始・終了 / resolve7 §3-8)
    scrub_changed = Signal(bool)

    # 再生ヘッドを掴んだ (ルーラ / トラック area の両方から呼ぶ)
    def begin_scrub(self):
        if self._scrubbing:
            return
        self._scrubbing = True
        self.scrub_changed.emit(True)

    # 離した (取りこぼしに備え PreviewPanel 側からも呼べるようにしておく)
    def end_scrub(self):
        if not self._scrubbing:
            return
        self._scrubbing = False
        self.scrub_changed.emit(False)

    def is_scrubbing(self):
        return self._scrubbing
```

ビュー側は「掴んだ・離した」を伝えるだけ:

```python
# TimelineRuler ── ルーラは mouseReleaseEvent を持っていないため新設する
def mousePressEvent(self, event):
    self._controller.begin_scrub()          # 追加
    self._emit_seek(event.position().x(), event.modifiers())

def mouseReleaseEvent(self, event):         # 新設
    self._controller.end_scrub()

# TimelineTrackArea ── 空き領域を掴んだときだけスクラブ扱いにする
if hit is None:
    self._controller.clear_selection()
    self._drag = _DRAG_PLAYHEAD
    self._controller.begin_scrub()          # 追加
    …

def mouseReleaseEvent(self, event):
    …
    if drag == _DRAG_PLAYHEAD or clip_id is None or preview is None:
        self._controller.end_scrub()        # 追加 (既存の早期 return の直前)
        self.update()
        return
```

* **クリップのドラッグ（移動・トリム）ではスクラブしない**。鳴らすのは再生ヘッドを掴んだときだけ。
* キーボード操作（`←/→`・Home/End）や再生中の自動追従では鳴らさない。
  `begin_scrub()` を通らないため自然にそうなる。

### 5.13 スクラブ音声の駆動（`preview_panel.py`）

```python
# 再生ヘッドのドラッグ中だけ、位置に追従して短い音を鳴らし続ける (resolve7 §3-7)
#   ・一定間隔で「今の再生ヘッド位置」へ合わせ直し、その場から等倍で鳴らす
#   ・動きが止まったら黙る / 手元に音が無い区間でも黙る (嘘の位置を鳴らさない)
#   ・self._rate は 0 のまま = 音声は再生ヘッドを動かさない (§2.5.2)
def _on_scrub_changed(self, active):
    if not self._cfg["scrub_audio_enabled"] or self._player is None:
        return
    if active:
        self.pause()                       # 再生中に掴んだら再生は止める (機器は 1 台)
        self._scrub_last_sec = None
        self._scrub_idle_ms = 0
        self._audio_output.setVolume(self._base_volume * self._cfg["scrub_volume"])
        self._frame_source.set_playing(bool(self._cfg["scrub_low_quality_frames"]))
        self._scrub_timer.start(int(self._cfg["scrub_interval_ms"]))
        self._scrub_prepare()              # 手元に無ければ短いチャンクを 1 本要求する
        return
    self._scrub_timer.stop()
    self._player.pause()
    self._audio_output.setVolume(self._base_volume)
    self._frame_source.set_playing(False)
    self._request_frame()                  # 離した瞬間に原寸で取り直す (停止時と同じ扱い)

# 1 粒ぶんの処理 (既定 60ms ごと)
def _scrub_tick(self):
    # 保険: 取りこぼしでボタンが離れていたら自分で終わる (§3-8)
    if not (QApplication.mouseButtons() & Qt.LeftButton):
        self._controller.end_scrub()
        return

    sec = self._controller.playhead()
    moved = (self._scrub_last_sec is None
             or abs(sec - self._scrub_last_sec) >= self._cfg["scrub_min_delta_sec"])
    if not moved:
        # 止まっている間は黙る (同じ 60ms をループさせない)
        self._scrub_idle_ms += int(self._cfg["scrub_interval_ms"])
        if self._scrub_idle_ms >= int(self._cfg["scrub_hold_ms"]):
            self._player.pause()
        return
    self._scrub_idle_ms = 0
    self._scrub_last_sec = sec

    hit = self._audio_source.cached(sec, self._grain_sec())
    if hit is None:
        self._player.pause()               # 手元に無い区間 → 黙って用意する
        self._scrub_prepare()
        return
    path, chunk_start = hit
    if path != self._scrub_source:         # チャンクが替わったときだけ差し替える
        self._player.setSource(QUrl.fromLocalFile(path))
        self._scrub_source = path
        self._chunk_start = float(chunk_start)
    self._player.setPosition(int(max(sec - self._chunk_start, 0.0) * 1000))
    self._player.setPlaybackRate(self._scrub_rate(sec))
    if self._player.playbackState() != QMediaPlayer.PlayingState:
        self._player.play()

# 速度。既定 "grain" は等倍 (音程が変わらない)。"match" はドラッグ速度に追従させる。
def _scrub_rate(self, sec):
    if str(self._cfg["scrub_rate_mode"]) != "match":
        return 1.0
    speed = abs(sec - (self._scrub_prev_sec or sec)) / (self._cfg["scrub_interval_ms"] / 1000.0)
    self._scrub_prev_sec = sec
    return min(max(speed, 0.5), 2.0)       # 環境依存を避けるため範囲を絞る
```

補助:

```python
# スクラブ用に短いチャンクを 1 本だけ用意する (同時要求は 1 本まで / §3-8)
def _scrub_prepare(self):
    if self._audio_worker is not None and self._audio_worker.isRunning():
        return
    self.set_status("音声を準備中…")
    self._request_chunk(self._controller.playhead(), autoplay=False,
                        length_sec=float(self._cfg["scrub_chunk_sec"]))
```

* `_on_audio_error` へガードを 1 行足す。スクラブ中の失敗で
  **無音タイマー再生を始めない**ようにする（§2.5.2）。
* `playing_changed` は出さない。**ドラッグ中に Timeline とインスペクタが無効化されない**
  （出すと、掴んで離すたびに UI がちらつく）。
* `QtMultimedia` が無い環境（`_MULTIMEDIA_AVAILABLE = False`）では
  `self._player is None` のため**何も起きない**。既存の「再生ボタンを無効化」と同じ扱い。
* アーカイブ用画面は `asr_audio_path` を渡していない（resolve6 §2.2(b) 末尾）ため、
  最初のスクラブで 0.1 秒ほどの生成待ちが入る。2 回目以降はキャッシュに当たる。

---

## 6. 実装手順（フェーズ分け）

| Phase | 内容 | 単独でリリースできるか |
|---|---|---|
| **1** | C1/C2/C6: `ResizeOverlay` / 四隅ハンドル / インスペクタの大きさ欄 | **できる**（モデル・出力・JSON に影響しない） |
| **2** | C10/C11/C12: スクラブ音声（`scrub_changed` / グレイン再生） | **できる**（他のどの要件にも依存しない） |
| **3** | C5 + C3 の保存側: 保存点管理 / 保存ボタン・Ctrl+S / 閉じる 3 択 | **できる**（開く導線が無くてもファイルは残る） |
| **4** | C4/C8/C9: `load_project` / `media_recovery` / `run_from_project` / main_window 導線 | できる |
| **5** | 任意: 再リンク画面 / `keep_media` / `autosave_sec` / プロジェクト D&D | できる |

Phase 1・2 と Phase 3〜5 は互いに独立しており、どれか 1 つだけの先行リリースが可能。
**Phase 1 と Phase 2 は出力にもファイル形式にも影響しない**ため、先に出しても差し戻しの心配が無い。

---

## 7. setting.json 定義（追加分）

`DEFAULT_SETTINGS["timeline"]` へ以下を足す。**既存キーは 1 つも変更しない**。
`load_settings()` の既定マージにより、利用者の `setting.json` には次回起動時に自動追記される。

```jsonc
"timeline": {
  // ── オーバーレイ (画像・動画) の大きさ (resolve7 §5.2/§5.3) ──
  "overlay": {
    // 拡大率の下限・上限 (キャンバス幅に対する比率)
    "min_scale": 0.02,
    "max_scale": 4.0,
    // プレビュー上の四隅ハンドルの一辺 (画面ピクセル。ビューの拡大率に依らず一定)
    "resize_handle_px": 10,
    // インスペクタの「大きさ」欄の刻み (%)
    "scale_step_percent": 1.0,
    // D&D で置いた直後の大きさ
    //   "fit_width" = キャンバス幅いっぱい (現行の挙動 / 既定)
    //   "native"    = 素材のピクセル数どおり (キャンバスより大きければ収める)
    "default_scale_mode": "fit_width"
  },

  // ── プロジェクトの保存・再編集 (resolve7 §5.7〜§5.11) ──
  "project": {
    // 編集画面に保存ボタン・Ctrl+S を出す (false で従来どおり保存操作なし)
    "save_button": true,
    // 未保存のまま閉じようとしたら確認する
    "confirm_on_close": true,
    // 保存時に参照素材をプロジェクトの隣へ複製する (true は動画 1 本ぶんのディスクを使う)
    "keep_media": false,
    // 素材が見つからないときの方針
    //   "renormalize" = 元動画からラウドネス正規化をやり直す (既定 / 保存時と同じ素材になる)
    //   "use_source"  = 元動画をそのまま使う (速いが音量が正規化前になる)
    //   "ask"         = 開くときに選ばせる
    "missing_media_policy": "renormalize",
    // main_window の「編集の続き」に出す履歴
    "recent_limit": 10,
    "recent": []
  },

  // ── スクラブ (再生ヘッドドラッグ中の音声 / resolve7 §3-7・§5.13) ──
  // 既存の timeline.preview セクションへ追加する
  "preview": {
    // false で従来どおり完全に無音のドラッグに戻る
    "scrub_audio_enabled": true,
    // 1 粒の間隔 (ms)。短いほど追従が細かくなるが setPosition の回数が増える
    "scrub_interval_ms": 60,
    // 手元に音が無いときに作る短いチャンクの長さ (秒)。実測 6 秒で 0.10 s (resolve6 §2.2(b))
    "scrub_chunk_sec": 4.0,
    // 動きが止まってから黙るまでの時間 (ms)
    "scrub_hold_ms": 120,
    // これ未満の移動は「止まっている」とみなす (秒)
    "scrub_min_delta_sec": 0.01,
    // 通常再生に対する音量比 (スクラブ音は耳に付きやすいので下げられるようにする)
    "scrub_volume": 0.8,
    // "grain" = 等倍で粒を撒く (既定 / 音程が変わらず逆方向でも鳴る)
    // "match" = ドラッグ速度に再生レートを合わせる (音程が変わる / 環境により無音になり得る)
    "scrub_rate_mode": "grain",
    // スクラブ中も再生中と同じ軽い取得 (縮小・前進デコード) を使う
    "scrub_low_quality_frames": true
  },

  // ── ショートカット (既存 shortcuts へ 2 つ追加) ──
  "shortcuts": {
    "save": "Ctrl+S",
    "save_as": "Ctrl+Shift+S"
  }
}
```

* 既存の `timeline.autosave_sec` / `timeline.keep_project_file` は**新設せずそのまま使う**
  （§2.2.1 / Phase 5）。同じ意味のキーを二重に持たせない。
* `project.recent` はアプリが書き換える唯一のキー。`archive_dock_state` と同じく
  `save_settings()` で永続化する（`archive_timeline_dialog.py:232` に前例がある）。

---

## 8. 互換性・非破壊の担保

| 観点 | 担保 |
|---|---|
| `schema_version` | **1 のまま**。追加するのは任意キー（`path_rel` / `media_role`）だけで、`from_dict` は未知キーを無視する |
| 旧プロジェクト JSON | `path_rel` が無い場合は判定 ② を飛ばし、`media_role` が無い場合は `media_path != input_path` で判定する（§3-5） |
| 既存の自動保存 | `pipeline_runner.py:181` / `:228` の 2 回はそのまま。手動保存はそこへ重ねるだけ |
| 既存の `project_io.load()` | **無変更**。`load_project()` を別に足す。テスト（`tests/test_timeline_project_io.py`）はそのまま通る |
| `from_dict()` | `validate_timeline` 引数の既定は `True`。既存呼び出しの挙動は同一 |
| 出力（動画） | `renderer` は無変更。大きさは既に `scale` を見ている。**同じ Timeline なら出力は 1 バイトも変わらない** |
| DaVinci Resolve 出力 | オーバーレイは元から対象外（§2.1.1）。無変更 |
| 従来フロー | `timeline.enabled=false` / `silence_cut.mode="physical"` の経路には一切触れない |
| アーカイブ画面 | 保存 UI を出さないフックを足すだけ。大きさ変更は継承でそのまま使える |
| setting.json | 追加のみ。既存値は `_merge_with_defaults()` が温存する |
| 字幕 | 字幕クリップはリサイズ対象外。`SubtitleClip.transform` は位置のみのままで、ASS 生成も無変更 |
| 再生・倍速・逆再生 | `_rate` を使う既存経路には触れない。スクラブは `_rate = 0` のまま動く（§2.5.2） |
| スクラブ音声 | `scrub_audio_enabled=false` で**完全に現行の挙動**（無音のドラッグ）へ戻せる |
| 音声チャンク | 生成方式・キャッシュ本数・形式は無変更。スクラブは既存の `cached()` / `build()` を短い長さで呼ぶだけ |
| QtMultimedia 非搭載環境 | `self._player is None` で何も起きない。既存の「再生ボタン無効化」と同じ扱い |

---

## 9. 確認事項

実装を始める前に決めたい点。**Q1〜Q4・Q9 は挙動が変わるため回答が要る**。
その他は本書の案で進めてよいが、異なる希望があれば実装前に指示が必要。

| # | 確認したいこと | 本書の案 |
|---|---|---|
| **Q1** | リサイズの基準点は **中心固定**でよいか（大きさを変えても位置が動かない）。それとも**掴んだ角の反対側を固定**（一般的な画像編集ソフトの挙動）にするか | 中心固定（§3-2） |
| **Q2** | 画像を D&D した**直後の大きさ**は現行どおり「キャンバス幅いっぱい」でよいか。**素材の原寸**で置く方が実用的だと思われる | 既定は現行維持（`default_scale_mode="fit_width"`）。設定 1 つで `native` へ切替可 |
| **Q3** | 保存済みプロジェクトを開くとき、消えた中間ファイルを**作り直す（時間がかかるが保存時と同じ音量）**か、**元動画をそのまま使う（速いが音量が正規化前）**か | 作り直す（`renormalize`） |
| **Q4** | **アーカイブ切り抜き用画面**の保存は今回対象外でよいか（§3-6 の理由による） | 対象外。将来拡張（§11）へ |
| **Q5** | 保存先の既定は現行の自動保存と同じ `<出力先>/<元動画名>.timeline.json` でよいか | よい（上書き確認は「名前を付けて保存」のときだけ） |
| **Q6** | 再編集して「決定」したときの出力名は、現行どおり `<元動画名>_edited.mp4` の**連番**（`_edited_1`, `_edited_2`…）でよいか | よい（`output_writer.py:26`） |
| **Q7** | `autosave_sec`（自動保存）は今回実装するか | Phase 5・任意。既定 0（無効）のまま |
| **Q8** | 「編集の続き」の入口は **main_window のクリップ用タブ内の 1 行**でよいか（新しいタブは作らない） | よい（§5.11） |
| **Q9** | スクラブ音は **等倍の粒（音程が変わらない・逆方向でも鳴る）** でよいか。それとも **ドラッグ速度に合わせて音程ごと遅くする**（＝文字どおりのスロー再生。ただし逆方向は鳴らせず、環境により無音になり得る）か | 等倍の粒（`scrub_rate_mode="grain"`。設定 1 つで `"match"` へ切替可 / §3-7） |
| **Q10** | 掴んだまま**動きを止めたら黙る**でよいか（同じ 120ms がループし続けない） | 黙る（`scrub_hold_ms=120` / §3-7） |
| **Q11** | 音の粒の間隔（既定 60ms）・スクラブ音量（既定 通常再生の 0.8 倍）はこの値でよいか。使ってみて調整したい場合は setting.json だけで変えられる | 既定のまま（§7） |

---

## 10. テスト計画

### 10.1 自動テスト（`tests/`）

| ファイル | 追加する検証 |
|---|---|
| `test_timeline_commands.py` | `ResizeOverlay`: 上下限で丸まる／同値なら履歴を汚さない（`False` を返す）／字幕とベースクリップでは効かない／Undo で元の `scale` へ戻る |
| `test_timeline_project_io.py` | `path_rel` / `media_role` を書き出す／`load_project()` が `created_at` を保つ／`validate_timeline=False` で欠落素材のクリップが無効化されない／旧 JSON（追加キー無し）がそのまま開ける |
| **新規** `test_media_recovery.py` | 判定順（そのまま在る → 相対パス → 再正規化 → 再リンク）／`media_path == input_path` の保存では再正規化しない／再リンクを断ったクリップだけが無効化される |
| **新規** `test_command_stack_clean.py` | 保存点の管理: 保存 → 変更 → `is_modified()` が真／Undo で保存点へ戻ると偽／保存点より前で分岐したら二度と偽にならない／履歴上限（100）を超えて古い手が捨てられても誤判定しない |
| `test_preview_playback.py`（既存へ追加） | スクラブ: `begin_scrub`/`end_scrub` で `scrub_changed` が 1 回ずつ出る／二重呼び出しで多重に出ない／`_rate` が 0 のままである（音声が再生ヘッドを動かさない）／動きが無い間は一時停止に入る／範囲外の位置ではチャンク要求のみ行い鳴らさない／要求は同時 1 本まで |

* 既存テストは 1 件も書き換えない（互換性の実証を兼ねる）。

### 10.2 手動確認（GUI）

**大きさ（C1/C2）**

1. 画像を Timeline へ D&D → プレビューで選択 → 四隅にハンドルが出る
2. 角をドラッグ → **縦横比を保ったまま**拡大縮小し、**中心が動かない**
3. 離した瞬間に絵が綺麗に描き直される（ぼやけたままにならない）
4. 「元に戻す」1 回で元の大きさへ戻る（ドラッグ中の中間状態が履歴に積まれていない）
5. インスペクタの `%` 入力・「原寸」・「画面幅に合わせる」がプレビューへ即反映される
6. **再生中はハンドルを掴めない**
7. 字幕を選んでもハンドルが出ない／本編クリップを選んでも「大きさ」欄が出ない
8. 「決定」して書き出した動画で、**画面で見たとおりの大きさ**になっている

**保存・再編集（C3〜C5）**

9. 編集 → タイトルに `*` が付く → `Ctrl+S` → `*` が消える
10. 未保存のまま `×` → **3 択のポップアップ**（保存して閉じる／保存せずに閉じる／編集に戻る）
11. 「保存して閉じる」→ 保存されたうえで閉じる。保存に失敗したら閉じない
12. 保存 → キャンセルでパイプライン中断 → main_window の「編集の続き」に出る
13. 選んで「開く…」→ **素材の復旧が進捗バーに出て**、編集画面が開く
14. 開いた直後に **再生ヘッドとズームが保存時のまま**復元されている
15. 追加した画像・変えた字幕の色・大きさがすべて保存時のまま残っている
16. さらに編集 → 「決定」→ レンダリング → `<元動画名>_edited_1.mp4` が出る
17. 元動画を別フォルダへ移してから開く → 再リンクを求められる（Phase 5）／
    Phase 4 時点では該当クリップが無効化され WARNING がログに出る
18. アーカイブ切り抜き用画面には**保存ボタンが出ない**（従来どおり動く）

**スクラブ音声（C10）**

19. タイムルーラを掴んでゆっくり動かす → **動かした量だけ音が進む**
20. 速く動かす → 音も速く進む。**手を止めると黙る**。離すと止まる
21. **左（逆方向）へ動かしても鳴る**
22. トラックの空き領域を掴んだドラッグでも同じように鳴る
23. クリップの移動・トリムのドラッグでは**鳴らない**
24. `←/→`・Home・End では鳴らない
25. 再生中に再生ヘッドを掴む → 再生が止まり、スクラブへ切り替わる
26. マウスを画面外へ出して離す → **鳴りっぱなしにならない**
27. ドラッグ中も Timeline とインスペクタが**無効化されない**（操作感が固まらない）
28. 編集直後（チャンク破棄後）に掴む → 一瞬「音声を準備中…」が出て、その後鳴る
29. `scrub_audio_enabled=false` にすると**完全に現行どおり無音**になる
30. QtMultimedia が無い環境でエラーにならない（黙って無音のまま動く）

---

## 11. 将来拡張（本書では実装しない）

* **アーカイブ切り抜きの保存**（§3-6）。道筋は次の 2 つを足すだけで、本書の仕組みに乗る。
  1. `prepared` の素材を VOD から**作り直す**復旧手段（`media_recovery` の判定 ③ の一般化。
     クリップの `origin.archive_clip_index` と VOD 上の区間は既に Timeline に記録されている）
  2. Timeline の外にある状態（`_themes` / `curve` / `prepared` の採点値）を
     プロジェクト JSON の `archive` セクションへ**サイドカーとして保存**する（任意キーのため v1 のまま可能）
* **プロジェクトのパッケージ化**（`.zip` に JSON + 素材をまとめる）。`keep_media` の発展形。
* **回転・不透明度の UI**。`Transform.rotation` / `opacity` はモデルにあるが操作手段が無い。
  `opacity` は `renderer.py:407` が既に消費している（`rotation` は未消費）。
* **オーバーレイの縦横比を意図的に崩す指定**（`scale_x` / `scale_y`）。
  本要望は逆（固定してほしい）のため、必要になるまで作らない。
* **厳密な逆方向スクラブ音・倍速逆再生の音**。`QMediaPlayer` が負のレートに非対応のため
  （resolve2 §3-6-2）、実現するには「区間を `areverse` で反転生成して鳴らす」か
  「PCM を自前で読んで `QAudioSink` へ流す」ことになる。
  本書の粒方式（§3-7）は前者の簡易版として十分機能するため、必要になるまで作らない。
* **スクラブ用の専用音声トラックの事前生成**（Timeline 全長ぶんを 1 本の低品質 WAV で先に作る）。
  チャンク境界での「黙り」が完全に無くなるが、長尺で生成時間とディスクを使う。
  現状は認識用音声の再利用で同じ効果が得られている（§2.5.1）ため保留。
