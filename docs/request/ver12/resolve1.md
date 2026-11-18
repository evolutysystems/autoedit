# ver12 resolve1 — Timeline へ SE (効果音) を配置し、音量を調節できるようにする

要望: `docs/request/ver12/request1.md` (2026-11-18)
前提: `docs/request/ver3/resolve.md` §6.2〜§6.8 (Timeline のモデル・編集・D&D)
　　　`docs/request/ver3/resolve6.md` (プレビュー音声チャンク)
　　　`docs/request/ver6/resolve3.md` (プレビューのプロキシ)
　　　`docs/CLAUDE.md` (実装前に設計 / ハードコード禁止 / 既存実装を破壊しない)
対象: `src/timeline/*` / `src/gui/timeline/*` / `src/archive/timeline_builder.py` / `src/settings/*` / `tests/*`
状態: **実装済み** (2026-11-18)

---

## 0. 先に結論

**「音声トラック A1」とは別に、SE 専用のトラック種別 `sound` を新設し、
そこへ固有の時刻を持つ `SoundClip` を置けるようにする。**

* A1 の `AudioClip` は **V1 のリンク**という不変条件 (`model.py:430`) を保ったまま、
  1 行も意味を変えない。音ズレが起きない今の作りを壊さない。
* `SoundClip` は `Clip` の派生にする。移動・トリム・分割・削除・リップル・吸着・
  Undo/Redo は**既存のコマンドがそのまま効く** (新規コマンドは「追加」と「音量」だけ)。
* 出力は**最後に 1 回だけ音声を差し替えるパス**を足す (`-c:v copy`)。
  既存のベース構築・合成・字幕焼き込みのコマンドは 1 文字も変わらない。
* 音量は `gain_db` (dB)。インスペクタのスライダ (連続値) と右クリックの刻みの両方から触る。

---

## 1. 現状 — なぜ今は置けないか

| # | 場所 | 事実 |
| --- | --- | --- |
| (1) | `src/timeline/model.py:430-476` | `AudioClip` は `link_clip` 必須。開始・尺・ソース位置はすべてリンク先の映像クリップからの導出値で、**自前の時刻を持たない** |
| (2) | `src/timeline/project_io.py:470` | `_validate_audio_links` が、リンク先の無い音声クリップを読み込み時に**捨てる** |
| (3) | `src/timeline/media_probe.py:19-20,48` | 受理する拡張子は動画と画像のみ。mp3 / wav は D&D しても無視される |
| (4) | `src/timeline/renderer.py:682,751` | 出力の音声は「ベース (V1) の音声」+「映像オーバーレイの音声」の amix だけ |
| (5) | `src/timeline/audio_source.py:168` | プレビュー音声は V1 の断片を concat しているだけ |
| (6) | `src/timeline/proxy_render.py:246` | プロキシのタイルも V1 + 映像オーバーレイのみ |

つまり **音だけの素材を置く器が無い**。これが要望の本体である。

## 2. 方針の比較

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | `AudioClip` に「リンク無しのときは自前の時刻を使う」分岐を足す | **不採用**。(1) の不変条件が崩れ、`Track.sort_clips` / `_validate_audio_links` / `duration_sec` / リップルの除外 (`commands.py:549`) が全部「どちらの音声か」で分岐する。既存の音ズレ対策の根拠が読めなくなる |
| B | 音声付き動画としてオーバーレイ (V2 以降) に置かせる | **不採用**。映像トラックを 1 本消費し、音だけの素材 (mp3) は置けず、音量も変えられない。要望 A・B を満たさない |
| C | **トラック種別 `sound` を新設し、`SoundClip(Clip)` を置く** | **採用**。A1 に触らない。移動・トリム・分割・削除・吸着・Undo は `Clip` 用の既存コマンドがそのまま効く |

### 案 C で `SoundClip` を `Clip` の派生にする理由

`commands.py` の編集処理は、時刻を持つノードを `isinstance(clip, Clip)` で判定して
`source_in` / `source_out` を動かしている (`commands.py:562,835,848,862,871`)。
`Clip` の派生にすれば、**トリム・分割・リップルの規約を 1 行も書き足さずに共有できる**。
`z_order` / `transform` も継承するが、SE トラックは映像トラックでも字幕トラックでも
ないため `Timeline.overlay_elements()` (`model.py:775`) に入らず、合成には出てこない。

---

## 3. 対策一覧

| # | 内容 | 置き場 |
| --- | --- | --- |
| **R1** | トラック種別 `sound` と `SoundClip(Clip)` を追加する | `timeline/model.py` |
| **R2** | 音声ファイルを素材として受理する (`MEDIA_AUDIO`) | `timeline/media_probe.py` |
| **R3** | SE の追加・音量・ミュートのコマンド | `timeline/commands.py` |
| **R4** | SE トラック行の表示・D&D・右クリック・波形 | `gui/timeline/timeline_view.py` |
| **R5** | インスペクタの音量スライダ | `gui/timeline/timeline_editor_dialog.py` |
| **R6** | 出力へ SE を混ぜる最終パス | `timeline/sound_mix.py` (新規) / `timeline/renderer.py` |
| **R7** | プレビュー音声へ SE を混ぜる | `timeline/audio_source.py` |
| **R8** | プロキシのタイルへ SE を混ぜる | `timeline/proxy_render.py` |
| **R9** | 保存・復元・検証・素材コピー | `timeline/project_io.py` ほか |
| **R10** | コピー＆ペースト | `timeline/clipboard.py` / `commands.py` |
| **R11** | 縦プロジェクトへの引き継ぎ | `timeline/vertical_builder.py` |
| **R12** | 設定項目 (既定値) | `settings/settings_window.py` / `timeline/builder.py` |
| **R13** | 取説・設計書の更新 | `docs/howto/*` / `docs/design/design.md` |

---

## 4. R1 データモデル (`src/timeline/model.py`)

```python
TRACK_SOUND = "sound"          # SE 専用トラック (A1 の音声トラックとは別物)
MEDIA_AUDIO = "audio"          # 音だけの素材
ORIGIN_USER_SE = "user_se"     # 由来 (記録専用)
BASE_SOUND_TRACK_ID = "SE1"
```

### 4.1 `SoundClip(Clip)`

```python
class SoundClip(Clip):
    # Timeline 上に固有の時刻を持つ音のノード (SE)。
    # Clip と同じ timeline_start / duration / source_in / source_out を持ち、
    # 加えて音量 (gain_db) とミュートを持つ。映像を持たないため z_order と
    # transform は使わない (継承はするが常に既定値)。
    def __init__(self, clip_id, media_id, timeline_start, duration,
                 source_in=0.0, source_out=None, gain_db=0.0, muted=False,
                 enabled=True, origin=None): ...
```

* `to_dict()` は `Clip` の辞書へ `"gain_db"` と `"muted"` を足す。`transform` / `z_order` は書かない。
* `from_dict()` は欠落時 0.0 / False。
* `copy()` は `SoundClip` を返す (分割・貼り付けが `clip.copy()` を使うため必須)。

### 4.2 `MediaRef`

* `kind == MEDIA_AUDIO` を扱えるようにする。`is_image()` は False、
  `max_source_sec()` は素材尺 (= 既存のまま動く)。
* `is_audio()` を足す (画面のラベルと検証で使う)。

### 4.3 `Track` / `Timeline`

| 追加 | 内容 |
| --- | --- |
| `Track.is_sound()` | `kind == TRACK_SOUND` |
| `Track.from_dict` | `kind == "sound"` のとき `SoundClip.from_dict` で読む |
| `Timeline.sound_tracks()` | index 昇順 |
| `Timeline.sound_clips(only_enabled=True)` | 全 SE トラックのノードを時系列順に返す (出力・プレビューの入力) |
| `Timeline.next_sound_track_id()` | `SE2`, `SE3` … |
| `Timeline.duration_sec()` | **SE トラックを含めない** (下記) |

**`duration_sec()` に SE を含めない理由**: 含めると「本編の後ろに置いた SE」で
出力尺が伸び、映像の無い黒画面が生える。SE は本編の尺の中で鳴るものとし、
はみ出した分は切る (出力・プレビューとも `duration=first` で自然にそうなる)。

---

## 5. R2 音声素材の受理 (`src/timeline/media_probe.py`)

* 設定 `timeline.media.audio_extensions` を追加 (既定
  `[".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"]`)。
* `classify()` は画像 → 音声 → 動画の順に判定し、音声なら `MEDIA_AUDIO` を返す。
* `probe()` は音声素材のとき `width=height=0` / `fps=0.0` / `has_audio=音声ストリームの有無`。
  尺は `format.duration` (既存の経路と同じ)。
* `default_clip_duration()` は素材尺をそのまま返す (既存の分岐で足りる)。
* `needs_normalize()` は映像の正規化判定のため、音声素材では呼ばない。

---

## 6. R3 編集コマンド (`src/timeline/commands.py`)

### 6.1 追加 — `AddSoundClip`

```
AddSoundClip(media, timeline_start, duration, track_id=None, cfg=None)
```

1. 素材をメディアプールへ登録 (同一パスは再利用 / `AddMediaClip` と同じ規約)
2. 置き先の SE トラックを決める
   * `track_id` 指定があり、ロックされておらず、その区間が空いていればそこ
   * 無ければ SE トラックを index 昇順に見て、空いている最初のトラック
   * どれも空いていなければ新しい SE トラックを作る (上限
     `timeline.sound_tracks.max_tracks`、既定 4。超えたら追加せず警告 → 画面は
     ステータスへ「SE トラックの上限に達しました」)
3. `SoundClip` を作って `created_clip_id` を公開する (呼び出し側が選択する)

**重なりは許さない** (同一トラック内)。重ねたいときは SE トラックを足す。
`_clamp_position` (`commands.py:381`) が同一トラックの重なりを自動で避けるため、
移動の規約は映像クリップと完全に同じになる。

### 6.2 `AddMediaClip` の受け口

D&D は種別を知らずに入ってくるため、`TimelineController.add_media()` の入口で
**素材が音声なら `AddSoundClip` へ振り分ける** (`timeline_controller.py:343`)。
`AddMediaClip` 自体は変えない (映像・画像専用のまま)。

### 6.3 音量とミュート

`SetAudioGain` / `SetAudioMuted` (`commands.py:2141,2160`) の `isinstance(clip, AudioClip)`
を `(AudioClip, SoundClip)` へ広げる。どちらも `gain_db` / `muted` を同じ意味で持つため、
コマンドを 2 つに割る理由が無い。

* 連続値 (スライダ) は **操作が終わった時点で 1 回だけ**コマンドを積む
  (`_CommitOnFocusOutEdit` と同じ流儀 / `sliderReleased`)。
  1 px ごとに積むと Undo 履歴が使い物にならなくなる。

### 6.4 既存コマンドへの波及

| コマンド | SE への効き方 | 変更 |
| --- | --- | --- |
| `MoveClip` / `TrimClip` / `SplitClip` | `Clip` 派生のため**そのまま効く**。トリムは素材尺が上限 (`_source_limit`) | 無し |
| `DeleteClip` (plain / ripple) | そのまま効く | 無し |
| `ripple_target_tracks` (`:549`) | 現在は「音声トラック以外すべて」。SE トラックは**含まれる** = リップルで一緒に詰まる | 無し (意図どおり) |
| `MoveOverlay` / `ResizeOverlay` / `ChangeZOrder` | SE は `transform` / `z_order` を使わないため対象外にする | `isinstance` のガードへ `SoundClip` を足す |

> リップルで SE も詰める (要望の付随事項 3 の回答)。本編を詰めたのに SE だけ
> 元の位置に残ると、合わせた場所からずれるため。SE だけ動かしたくない場合は
> トラックのロックで止められる。

---

## 7. R4 画面 (`src/gui/timeline/timeline_view.py`)

### 7.1 行の並び

`build_rows()` (`timeline_view.py:179`) の末尾へ SE トラックを足す。
上から **字幕 → 追加映像 → V1 → A1 → SE1 …**。行の高さは
`timeline.sound_tracks.track_height_px` (既定 44)。

### 7.2 見た目

* 塗り色はテーマへ `timeline.clip.sound` を足す (無い場合は音声クリップ色の派生)。
* ラベルは素材のファイル名 (拡張子なし) + ミュート時は `(ミュート)`。
* 波形は既存の `_paint_waveform` を**素材と `source_in` を引数に取る形へ小さく切り出して**
  共有する。`WaveformCache.peaks_for(media)` は音声のみの素材でもそのまま動く
  (`-vn -map 0:a:0` でデコードしているため / `waveform.py:191`)。
* ゲインは既存どおり波形の高さへ反映する (`timeline_view.py:746` と同じ式)。

### 7.3 D&D

`dropEvent` (`timeline_view.py:1477`) を次のようにする。

* 受理拡張子に音声を足す (`media_probe.is_supported` が R2 で対応済み)。
* 落とした行が SE トラックなら、その**トラック**と**落とした時刻**へ置く。
* 落とした行が SE トラック以外 (または行の外) でも、**素材が音声なら SE として置く**
  (映像トラックへ音は置けないため)。置き先は空いている SE トラック。
* 複数ファイルを落としたときは、映像/画像は従来どおり、音声は SE として、
  それぞれ時系列に並べる。

### 7.4 右クリック

SE ノードのメニュー:

```
ここで分割
コピー / 再生ヘッドへ貼り付け
──
ミュート / ミュート解除
音量 ▸  +6 dB / +3 dB / 0 dB / -3 dB / -6 dB      ← 刻みは設定 gain_presets_db
──
削除のみ (空白を残す)   Backspace
リップル削除            Delete
```

* 空き領域 (SE トラック) の右クリックには **「SE を追加...」** を置き、
  ファイル選択ダイアログからも置けるようにする (D&D を知らなくても辿り着ける)。
* トラックヘッダの右クリックへ「SE トラックを追加 / このトラックを削除」を足す
  (`TrackHeaderWidget.contextMenuEvent:490` の字幕トラック削除と同じ規約。
  中身が空か、確認ダイアログで同意したときだけ消す)。

### 7.5 ロック・有効/無効

トラックの `enabled` / `locked`、ノードの `enabled` は字幕・映像と同じ意味で効かせる
(`enabled=False` の SE は鳴らない)。

---

## 8. R5 インスペクタ (`src/gui/timeline/timeline_editor_dialog.py`)

選択が `SoundClip` のとき、専用の欄を出す (字幕欄・オーバーレイ欄と同じ作り)。

| 欄 | 操作 | 反映先 |
| --- | --- | --- |
| 素材名 / 開始 / 尺 | 表示のみ | `info_label` |
| **音量** | スライダ (`gain_min_db`〜`gain_max_db`、既定 -40〜+12 dB) + 数値表示 | `SetAudioGain` (離したときに 1 回) |
| **ミュート** | チェックボックス | `SetAudioMuted` |

* 値は `gain_step_db` (既定 0.5 dB) 刻み。`0 dB` へ戻すボタンを右に置く。
* 複数選択時は先頭 1 件の値を出し、操作は選択中の SE すべてへ効かせる
  (色の一括適用 `_selected_subtitle_ids` と同じ流儀)。

---

## 9. R6 出力 (`src/timeline/sound_mix.py` 新規 / `renderer.py`)

### 9.1 置き場所

`renderer.render()` の **最後** (`renderer.py:112` の透かし適用の直後、
`context.set_current_video_path(result_path)` の直前) へ 1 段足す。

```
… → 字幕焼き込み → ぼかし保険 → 最終化 → 透かし保険 → ★SE ミックス → 出力
```

**合成 (`_composite`) の amix へ混ぜない理由**: `_composite` はオーバーレイか字幕が
あるときしか走らない (`renderer.py:741` 「有効なオーバーレイが無いため合成をスキップ」)。
そこへ入れると「字幕を全部消した案件では SE だけ鳴らない」という穴ができる。
最後に 1 段置けば、どの経路を通っても必ず鳴る。

### 9.2 コストと画質

* 映像は **`-c:v copy`** (再エンコードしない)。画質は 1 ミリも落ちない。
* 音声だけ焼き直す。実時間のごく一部で終わる。
* SE が 1 つも無い案件では**この段自体を呼ばない** (コマンドは 1 本も増えない)。

### 9.3 2 段構成

1. **SE だけのミックス音声を作る** (`se_mix.wav` / pcm_s16le)

   ```
   ffmpeg -y -f lavfi -t <総尺> -i anullsrc=channel_layout=stereo:sample_rate=<sr>
          -ss <source_in> -t <dur> -i <SE1>  …
          -filter_complex
            "[1:a]volume=<gain>dB,aformat=sample_rates=<sr>:channel_layouts=stereo,
                  adelay=<ms>|<ms>[s1]; …
             [0:a][s1][s2]…amix=inputs=N:duration=first:dropout_transition=0:normalize=0[out]"
          -map "[out]" -c:a pcm_s16le se_mix.wav
   ```

   * `normalize=0` が肝。既定 (`normalize=1`) は入力数で割るため、
     SE を足すほど全体が小さくなる。
   * 1 パスの入力上限 `max_mix_inputs` (既定 32) を超える場合は、
     32 本ずつ中間 wav を作って段階的に混ぜる (ノード数に上限を作らない)。

2. **本編の音声と混ぜて差し替える**

   ```
   ffmpeg -y -i <本編> -i se_mix.wav
          -filter_complex "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
          -map 0:v -c:v copy -map "[aout]" -c:a <audio_codec> <bitrate> -movflags +faststart out.mp4
   ```

   * 本編に音声ストリームが無い場合 (保険) は `anullsrc` を基底にする。
   * `duration=first` で、本編より後ろへはみ出した SE は切られる (§4.3)。

### 9.4 対象から外すノード

`enabled=False` / `muted=True` / 素材が見つからない / 尺が `min_clip_sec` 未満。
素材欠落は WARNING を出して**その SE だけ**落とす (出力は止めない / 既存の例外方針)。

---

## 10. R7 プレビュー音声 (`src/timeline/audio_source.py`)

`_collect_pieces` は V1 の断片列のままにし、**concat したあとに SE を混ぜる**。

* `_render_pieces` (1 プロセス方式 / 既定) …… `concat=…[out]` の後ろへ
  `sound_mix.preview_chains()` が返す SE 入力と amix を継ぎ足す。
  チャンク開始を 0 とした相対時刻で `adelay` を掛ける。
* `_render_pieces_each` (`per_piece` 方式 / 切り戻し用) …… 連結後のファイルへ
  §9.3-2 と同じミックスパスを 1 回掛ける。
* **認識用音声の再利用を止める条件へ「SE がある」を足す** (`audio_source.py:69`)。
  再利用した音声には SE が入っていないため、置いた直後に鳴らなくなる。
* 編集のたびに `invalidate()` が走る (`preview_panel.py:412`) ため、
  音量を変えればチャンクは作り直される = スライダの結果がそのまま聞ける。

スクラブ音声も同じチャンクを使うため、追加の対応は要らない。

---

## 11. R8 プロキシ (`src/timeline/proxy_render.py`)

* `_build_command` の `mix_labels` へ、タイル区間 `[start, end)` に重なる SE を足す
  (映像オーバーレイの音声と同じ作り / `proxy_render.py:386`)。
  `adelay` はタイル開始からの相対、`-ss/-t` で素材側を切る。
* **`tile_key` へ SE を足す** (`proxy_render.py:122`)。
  ここへ足し忘れると、音量を変えても古いタイルが鳴り続ける。

  ```
  parts.append("se|" + "|".join([media_id, source_in, timeline_start, duration,
                                 gain_db, muted, enabled]))
  ```
* `_KEY_VERSION` を 1 つ上げ、旧タイルを無効化する。

---

## 12. R9 保存・復元・素材 (`src/timeline/project_io.py` ほか)

| 箇所 | 対応 |
| --- | --- |
| `Track.to_dict/from_dict` | `kind="sound"` を素通しする (R1) |
| `_validate_media:399` | 素材欠落時にクリップを無効化する対象へ **SE トラックを足す** (現在は映像トラックのみ) |
| `_validate_clips:447` | SE トラックも検証する (`source_out <= source_in` を弾く条件は映像と同じ) |
| `_validate_audio_links:470` | **音声トラックだけ**を見ている。SE トラックは対象外 = 影響なし (リンクを持たないノードが捨てられない) |
| `_validate_overlaps:482` | SE トラックは重なり検証の対象 (音声トラックのみ除外のまま) |
| `used_media_ids:642` | `getattr(clip, "media_id")` で拾うため**変更不要**。SE の素材も「使用中」に数えられ、保存時の素材コピー (`keep_media`) と欠落復旧の対象になる |
| `media_recovery` / `missing_media_dialog` | SE の素材が見つからないときも同じ画面で差し替えられるようにする (扱いは映像素材と同じ) |
| `_export_audio_sidecars` | 対象は正規化済み本編のみ。SE は**元ファイルをそのままコピー**する (サイドカー不要) |

プロジェクト JSON の版 (`version`) は上げない。
SE トラックを持たない旧プロジェクトは「SE トラックが 0 本」として読める。
逆に、SE を含むプロジェクトを**旧アプリで開いた場合は保証しない**
(旧 `Track.from_dict` は未知の `kind` を映像クリップとして読む)。
この 1 点だけ後方互換ではないため `docs/README.md` へ明記する。

---

## 13. R10 コピー＆ペースト (`src/timeline/clipboard.py`)

* `KIND_SOUND = "sound"` を足し、`_build_item` で `gain_db` / `muted` を控える。
* `_resolve_targets` は `AudioClip` をリンク元へ読み替えている (`clipboard.py:23`)。
  `SoundClip` は**そのまま対象**にする (読み替えない)。
* `PasteClips` の置き先解決へ SE を足す: 貼り付け先は「同じ index の SE トラック →
  無ければ空いている SE トラック → 無ければ新規」。
  割り込み (`make_room_for_range`) の扱いは映像・字幕と同じ。

SE は「同じ音を何回も置く」使い方が多いため、コピー＆ペーストは初回から入れる。

---

## 14. R11 縦プロジェクト (`src/timeline/vertical_builder.py`)

* 設定 `timeline.sound_tracks.carry_to_vertical` (既定 true)。
* 字幕の移送 (`_copy_subtitles`) と同じ `offsets` を使い、選んだ区間に重なる SE を
  新しい時刻へ置き直す。区間を跨ぐ SE は**区間内に収まる分だけ**にする。
* 引き継げなかった SE があれば `warnings` へ 1 行積む (既存の警告表示へ乗る)。

---

## 15. R12 設定 (`settings/settings_window.py` の `DEFAULT_SETTINGS`)

```jsonc
"timeline": {
  "media": {
    // 既存 video_extensions / image_extensions に加えて
    "audio_extensions": [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"]
  },
  "sound_tracks": {
    "enabled": true,              // false で機能ごと従来どおり (行も出ない)
    "initial_tracks": 1,          // 最初から出しておく SE トラックの本数 (0 で必要時に作る)
    "track_id_prefix": "SE",
    "track_name_format": "SE {index}",
    "max_tracks": 4,
    "track_height_px": 44,
    "default_gain_db": 0.0,
    "gain_min_db": -40.0,
    "gain_max_db": 12.0,
    "gain_step_db": 0.5,
    "gain_presets_db": [6.0, 3.0, 0.0, -3.0, -6.0],   // 右クリックの刻み
    "mix_normalize": false,       // amix normalize (既定 false = 本編の音量を下げない)
    "max_mix_inputs": 32,         // 1 回の ffmpeg へ渡す SE の本数の上限
    "carry_to_vertical": true
  }
}
```

* `_fill_timeline_nested_defaults` (`settings_window.py:1781`) が 1 段深く補完するため、
  **既存の setting.json にも自動で現れる**。利用者の設定は上書きしない。
* `timeline_config()` (`builder.py`) に `"sound_tracks"` セクションを足し、
  画面・コマンドはここだけを見る (ハードコード禁止 / `docs/CLAUDE.md`)。

---

## 16. 対象外 (今回やらない)

| # | 内容 | 理由 |
| --- | --- | --- |
| (1) | DaVinci Resolve (FCPXML) への SE 書き出し | `fcpxml_builder` は映像 1 本 + title/caption のレーン構成で、音声アセットのレーンを持たない。別の設計が要る。**SE があるときは「SE は Resolve へ引き継がれません」と画面へ出す** |
| (2) | フェードイン/アウト、ピッチ、ループ | 要望に無い。`SoundClip` へ任意キーとして後から足せる形にしておく |
| (3) | SE ライブラリ (よく使う音の一覧) | 要望に無い。まず置ける状態を作る |
| (4) | 本編の音量を SE に合わせて下げる (ダッキング) | 要望に無い |
| (5) | 出力尺を SE に合わせて伸ばす | §4.3 の理由により行わない |

## 17. テスト (`tests/`)

| ファイル | 検証 |
| --- | --- |
| `test_timeline_sound_model.py` | `SoundClip` の辞書往復 / `duration_sec` が SE で伸びない / `sound_clips()` の並び |
| `test_timeline_sound_commands.py` | 追加・移動 (重なり回避)・トリム (素材尺が上限)・分割・削除・リップル・音量・ミュート・Undo/Redo |
| `test_timeline_sound_io.py` | 保存 → 読み込みで位置・音量・ミュートが戻る / 素材欠落で無効化され、出力が止まらない / 旧プロジェクト (SE 無し) がそのまま読める |
| `test_sound_mix.py` | 組み立てた ffmpeg 引数に `volume=…dB` / `adelay` / `normalize=0` / `-c:v copy` が入る。SE 0 件ならパスを呼ばない。`max_mix_inputs` 超過で段分けされる |
| `test_proxy_render.py` (追記) | `tile_key` が音量変更で変わる |
| `test_timeline_clipboard.py` (追記) | SE のコピー＆ペースト |

既存テストは 1 件も変更しない (= 既存挙動を変えていないことの確認になる)。
実行は `python -m unittest discover -s tests`。

## 18. 実装順

1. R1 モデル + R12 設定 + 単体テスト (画面なしで完結する)
2. R2 素材受理 + R3 コマンド + テスト
3. R9 保存・復元 + テスト
4. R6 出力 (`sound_mix.py`) + テスト ← **ここまでで「鳴る」ことを実素材で確認する**
5. R4 / R5 画面 (D&D・右クリック・インスペクタ・波形)
6. R7 / R8 プレビュー (チャンク・プロキシ)
7. R10 コピペ / R11 縦 / R13 ドキュメント

各段で `python -m unittest discover -s tests` が全部通ることを確認してから次へ進む。

## 19. 実装前に確認したこと (2026-11-18 / 利用者回答)

| # | 質問 | 決定 |
| --- | --- | --- |
| Q0 | 音量は右クリックの段とインスペクタのスライダのどちらにするか | **両方入れる** (利用者回答)。段は即決用、スライダは再生しながらの追い込み用 |
| Q1 | SE トラックは最初から 1 本出しておくか | 出す (`initial_tracks: 1`) |
| Q2 | リップル削除で SE も詰めるか | 詰める (§6.4) |
| Q3 | 音量の既定範囲 | -40〜+12 dB / 0.5 dB 刻み |
| Q4 | 本編より後ろに置いた SE は切るか | 切る (§4.3) |
| Q5 | Resolve 出力への引き継ぎ | 後回し (§16 (1))。書き出し前に警告を出す |

## 20. 実装の結果 (設計との差分)

設計どおりに入れたもの以外で、実装時に判断した点を残す。

| # | 内容 | 理由 |
| --- | --- | --- |
| D1 | **SE の素材は `keep_media` でプロジェクトの隣へ複製しない** (§12 の記述を変更)。元のパスを指したままにする | 複製の対象は `_media_to_copy()` が返す本編素材だけで、D&D で足した動画・画像も複製していない。SE だけ別扱いにすると規約が割れる。素材が移動した場合は従来どおり差し替え画面 (音声ファイル用のフィルタを追加済み) で拾い直す |
| D2 | **SE を別の SE トラックへドラッグで移すことはできない** | トラック間ドラッグはテロップ専用の仕組み (`_update_drop_track`)。同一トラック内の移動・トリムは効くため、置き直しは「コピー → 貼り付け → 削除」で足りる。要望が出たら別途 |
| D3 | プロキシのタイルでは **本編の音と SE を別々の amix で混ぜる** | 既存の amix (オーバーレイ音声用) は `normalize` が既定 (=1) のまま。そこへ相乗りすると SE を置くほど本編が小さくなり、出力 (normalize=0) と音量が食い違う |
| D4 | `renderer` は SE ミックス後に**ミックス前の動画を解放する** | 中間ファイルのピークを 1 本分増やさないため (ベース映像の解放と同じ扱い) |
| D5 | 音声素材の `probe` は **寸法を見ずに 0 で確定**させる | mp3 のジャケット画像が video ストリームとして見え、素材の寸法として拾われてしまうため |
| D6 | **切り抜き (アーカイブ) の書き出しでも SE を鳴らす**ため、`archive/timeline_builder._build_group_timeline` でクリップごとの Timeline へ SE を写す | 切り抜きは V1 を archive_clip_index で割って 1 本ずつ `renderer.render` に掛ける。写さないと切り抜きの出力だけ SE が消える (字幕・オーバーレイと同じ扱いにそろえた) |

### クリップ用 / アーカイブ用の両方で成立すること (2026-11-18 / 利用者指示)

| 画面 | 確認した内容 |
| --- | --- |
| **クリップ用** (1 本編集 / `TimelineEditorDialog`) | SE1 行が出る → D&D で配置 → 音量 (右クリックの段 / インスペクタのスライダ) → 保存・復元 → `renderer.render` で出力に混ざる |
| **アーカイブ用** (切り抜き編集 / `ArchiveTimelineDialog`) | 同じ 4 点 + **クリップ分割後の書き出しにも乗る**。`ArchiveTimelineDialog` は `TimelineEditorDialog` の派生のため、行・D&D・右クリック・インスペクタは同じ実装が効く |

アーカイブ用は書き出しだけ経路が違う (V1 を `archive_clip_index` で割り、クリップ 1 本ずつ
`renderer.render` に掛けて最後に結合する)。そのため `_build_group_timeline` へ SE を写す
必要があった (D6)。境界をまたぐ SE は両側へ分かれるが、`source_in` を切った分だけ
進めるため、結合後は 1 つの音として続いて鳴る。
「使用しない」にしたクリップの範囲にある SE は、そのクリップごと書き出されない。

### 実機確認 (2026-11-18)

同梱 FFmpeg で、ステレオ素材 (6 秒) + SE 2 件を実際に書き出して測定した。

| 確認 | 結果 |
| --- | --- |
| 本編の音量が SE の有無で変わらないこと | 全区間で **±0.0 dB** (normalize=0 が効いている) |
| SE を置いた位置で音が増えること | 2.0〜3.0s で **+1.8 dB** (素材そのものの音量と一致) |
| 音量指定が効くこと | `+6 dB` → -18.1 dB / `-20 dB` → -44.1 dB (素材 -24.1 dB に対して設計どおり) |
| 画質 | 映像は `-c:v copy` のため再エンコードなし |

テストは `python -m unittest discover -s tests` で **2397 件すべて成功**
(SE 用に 6 ファイル・58 件を追加。既存テストは 1 件も変更していない)。
