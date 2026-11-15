# ver11 resolve2 — 出力キャンバスの解決値が setting.json へ焼き付き、以降の出力が極端に縮む問題の修正

要望: 「アーカイブ切り抜きから動画を出力すると極端に画質が落ちる。設定の問題か処理の問題か確認する」(2026-11-15)
前提: `docs/quality/resolve3.md` R2 (`output_scale_mode="source"` と `apply_canvas_to_settings` を入れた経緯)
　　　`docs/request/ver8/resolve3.md` (編集画面の大きさ・位置・分割位置の記憶)
　　　`docs/CLAUDE.md` (実装前に設計 / ハードコード禁止 / 既存実装を破壊しない)
対象: `src/modules/output_profile.py` / `src/settings/settings_window.py`
状態: **実装済み** (2026-11-15)

---

## 0. 先に結論

**「その実行だけの出力キャンバス解決値」が setting.json へ保存されてしまい、
以降のすべての出力が、別の素材向けに解決された寸法まで縮むようになっていた。**

利用者の配布版 (v1.8.3) の実設定:

```
_internal/src/settings/setting.json  (更新 2026-11-10 20:28:15)
  "output_width": 606          ← 既定は 1920
  "output_height": 1080
  "output_reference_height": 1080   ← setting.json へ保存されないはずのキー
```

`output_scale_mode="source"` では `output_width/height` が**出力の上限枠**として効くため、
この値のままでは次のようになる (`resolve_canvas` を実素材で実行した実測)。

| 素材 | 壊れた設定での出力 | 本来 |
| --- | --- | --- |
| **1920x1080 (VOD)** | **606x340** (面積比 5.6%) | 1920x1080 |
| 1280x720 | 606x340 | 1280x720 |
| 2560x1440 | 606x340 | 1920x1080 |

多段エンコード自体は主因ではない (実素材 10 秒 / 配布版同梱 FFmpeg / crf20・medium で
中間 → 字幕焼き込み → テーマタグ → 透かしの 4 段を再現: VMAF 94.1 → 92.1 → 90.3 → 88.9)。

## 1. 原因

| # | 場所 | すること |
| --- | --- | --- |
| ① | `pipeline/pipeline_runner.py:90` | `apply_canvas_to_settings` が解決値を**実行用コピー**の `ffmpeg.output_*` へ書き戻す |
| ② | `pipeline/pipeline_runner.py:471` | その `context.settings` をそのまま Timeline 編集画面へ渡す |
| ③ | `gui/timeline/timeline_editor_dialog.py:1195 / 1268` | 画面を閉じるとき `save_settings(self._settings)` で**丸ごと保存**する (最近使ったプロジェクト / 画面の大きさ・位置) |

① は「実行用コピーにだけ書く」前提で deepcopy しているが、③ がそのコピーを
保存の入力にしてしまうため、前提が崩れる。
`apply_canvas_to_settings` のコメントが「setting.json へは保存されない」と書いている
`output_reference_height` が実ファイルに現れていることが、この経路の動かぬ証拠である。

汚染した実行はログから特定できる (2026-11-10)。

```
20:23:54 出力キャンバス: 素材等倍 606x1080@60fps (素材 1278x2272@60fps / 上限 1920x1080@60fps)
20:28:15 パイプライン中断: ユーザーによるキャンセル     ← setting.json の更新時刻と一致
```

縦素材 (1278x2272) 1 本を編集画面で開いて閉じただけで、以後のアーカイブ切り抜きが
606 幅に潰れるようになった。**キャンセルしても保存される** (画面の状態の記憶は
決定/取消と無関係に走る) ため、出力していなくても起きる。

## 2. 併発していた不具合 — 縦素材を横の枠で収めている

上のログの `上限 1920x1080` が示すとおり、`resolve_canvas` は**素材の向きに関わらず
`ffmpeg.output_width/height` (横向きの枠) で収めている**。縦素材では高さ側が支配するため、

```
1278x2272 → ratio = min(1, 1920/1278, 1080/2272) = 0.475 → 606x1080
```

と、縦素材そのものも不要に縮む。縦の出力キャンバスは `vertical.output_width/height`
(1080x1920) であり、上限もそちらで見るのが筋である。
今回の 606 はこの計算から出た値なので、原因としては ①②③ と地続きである。

## 3. 対策

| # | 内容 | 置き場 |
| --- | --- | --- |
| **R1** | `apply_canvas_to_settings` は上書き前の保存値を控えへ残す | `output_profile` |
| **R2** | `save_settings` は保存の直前に実行時キーを取り除き、控えの値へ戻す | `settings_window` |
| **R3** | 既に壊れている setting.json を起動時に自己修復する | `settings_window` の移行処理 |
| **R4** | `resolve_canvas` の上限枠を素材の向きに合わせる | `output_profile` |

**R2 を「保存の出口」に置くことが要点である。** 画面やパイプラインの呼び出し側を
個別に直すと、`save_settings` を呼ぶ箇所 (現在 7 か所) が増えるたびに再発する。
出口 1 か所で守れば、解決済みコピーをどこから保存しても保存値は壊れない。

### R1 控えの持ち方

`ffmpeg.output_canvas_saved = {"output_width":…, "output_height":…, "output_fps":…}` を
上書きの**前**に 1 度だけ置く (`setdefault` なので 2 回通しても最初の控えが残る)。
キー名は `output_profile.RUNTIME_CANVAS_BACKUP_KEY` に定数で持ち、ハードコードしない。

### R2 保存時の除去

`without_runtime_canvas(settings)` が、

* 控えがあれば `output_width/height/output_fps` をその値へ戻す
* `output_reference_height` と控え自身を取り除く
* 実行時キーが 1 つも無ければ**元のオブジェクトをそのまま返す** (従来と完全に同一)

を行った**コピー**を返し、`save_settings` はそれを書き出す。
元の `settings` は変えない (実行中の工程が見ている解決値を壊さないため)。

### R3 自己修復

既に配布済みの版が焼き付けた設定には控えが無い。`output_reference_height` が
setting.json に存在すること自体が汚染の証拠なので、これを判定に使う。
元の幅は残っていないため、基準の高さ (`output_reference_height`) を高さに採り、
幅は既定の縦横比 (`_DEFAULT_LANDSCAPE_WIDTH/HEIGHT` = 1920x1080) から戻す。
実測ケース (reference=1080) では 1920x1080 へ復元される。

`load_settings` → `_normalize_legacy_values` の移行処理に `_migrate_runtime_canvas` を
足し、**起動時に 1 度だけ**直して保存する (既存の移行と同じ作法)。

### R4 縦素材の上限枠

`resolve_canvas` は probe で得た素材寸法が縦 (高さ > 幅) で `vertical.enabled` が真なら、
上限枠を `vertical.output_width/height` に切り替える。横素材・正方形は従来どおり。

```
1278x2272 → ratio = min(1, 1080/1278, 1920/2272) = 0.845 → 1080x1920
```

## 4. 影響範囲と切り戻し

* R1/R2 は**保存値にしか効かない**。実行中の解決値は 1 つも変わらないため、
  出力の内容 (フィルタ・コマンド) は従来と同一である。
* R3 は `output_reference_height` を持つ設定にだけ効く。正常な設定は 1 バイトも動かない。
* R4 は縦素材の出力寸法が変わる (606x1080 → 1080x1920)。**縦素材でのみ**挙動が変わる。
  横素材 (アーカイブ切り抜きの VOD を含む) は従来と同一。
* 切り戻しは R4 のみ `vertical.enabled=false` で従来動作へ戻る。

## 5. テスト (`tests/test_output_canvas.py` に追加)

| 観点 | 内容 |
| --- | --- |
| R1/R2 | 解決済みコピーを `save_settings` しても、保存値の `output_width/height/fps` が元のまま |
| R1/R2 | 保存したファイルに `output_reference_height` / 控えキーが残らない |
| R2 | 解決を通していない設定は `without_runtime_canvas` が同一オブジェクトを返す |
| R2 | `without_runtime_canvas` は渡された設定を書き換えない (実行中の値を壊さない) |
| R3 | 壊れた設定 (606x1080 + reference 1080) が 1920x1080 へ復元される |
| R3 | 正常な設定は移行で変化しない |
| R4 | 縦素材 1278x2272 が 1080x1920 になる / `vertical.enabled=false` では従来値 |
| R4 | 横素材は従来どおり (既存テストが継続して通ること) |
