# resolve18 — アーカイブ切り抜きTOP5に「無音カット＋テロップ編集画面」を適用 要望設計書

## 0. 本書の位置づけ
`docs/request/ver2/request18.md`（flow17 R1 フローの修正）に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要なライブラリを追加しない／後方互換を失わない／不明点は推測実装せず設計書へ記載する）に従い、本書レビュー後に実装します。

---

## 1. 要望（request18.md ＋ 追加回答 2026-07-22）

- **ピックアップした TOP5 クリップは、クリップ選択後に、既存の「クリップ用」と同じように「無音カット」と「テロップ編集画面」を出す**ようにする。
- **TOP5 クリップは最後に全て結合して 1 つの動画ファイルにする**（追加要望）。
- **確認事項の回答**：① 音量/カット閾値ダイアログは**出さない**／③ クリップ編集のキャンセルは**当該クリップのみスキップして継続**／④ **OP/ED は付ける**（＝**結合した 1 本の最終動画**に付ける）。

つまり、アーカイブ採点で選んだ各クリップを**現行クリップ用と同じ体験**（無音カット → テロップ編集 → 焼き込み）で処理し、**それらを結合し、先頭にオープニング・末尾にエンディングを付けた 1 本の動画**を出力する。

---

## 2. 現状分析（R1 実装の実測）

| 対象 | 現状の挙動 | 本要望上の問題 |
|---|---|---|
| `src/archive/clip_writer.py` `write_clips()` | 各クリップ: 区間を切り出し → `PipelineContext(..., subtitle_review_callback=None)` → **`subtitle_generator.run()` だけ**を実行して焼き込み → 出力名 `archive_...` で保存 | **無音カットが無い**／**テロップ編集画面が出ない**（`subtitle_review_callback=None` かつ `silence_cutter.run` を呼んでいない） |
| `src/gui/archive_tab.py` `ArchiveClipWorker` | `clip_writer.write_clips(input, settings, clips)` を呼ぶだけ。**レビュー/音量のブリッジを渡していない** | ワーカースレッドからメインスレッドのダイアログを開く橋渡しが無いため、そもそも編集画面を出せない |
| 現行クリップ用 `run_pipeline()`（`src/pipeline/pipeline_runner.py:33`） | ① 音量解析・カット閾値確認（`_apply_volume_analysis`, `volume_analysis_callback`）② 無音カット（`silence_cutter.run`）③ フルテロップ＋**編集画面**（`subtitle_generator.run` ＋ `subtitle_review_callback`）④ OP/ED 結合 ⑤ 出力 | ここに**再利用すべき正解フロー**が既にある |
| ブリッジ機構（`src/gui/main_window.py`） | `SubtitleReviewBridge`（`items→編集結果` をメインスレッドの `SubtitleEditorDialog` で処理）、`VolumeThresholdBridge`（カット閾値ダイアログ） | アーカイブ側でも**そのまま流用可能** |

**要点**：現行クリップ用の「無音カット＋編集画面」は `run_pipeline()` と 2 つのブリッジ（`SubtitleReviewBridge` / `VolumeThresholdBridge`）で成立している。R1 のアーカイブ切り抜きは**これらを使わずに焼き込みだけ**していたため、要望の体験になっていない。→ **選択後の各クリップを `run_pipeline` 相当の流れ（無音カット＋レビュー）に通す**のが本設計。

---

## 3. 設計方針（全体）

1. **既存資産の最大再利用**。各 TOP5 クリップを**現行の `run_pipeline()` に通す**（クリップ用と同一の 無音カット＋テロップ編集＋焼き込み）。新規パイプラインは作らない。
2. **OP/ED は各クリップには付けず、最終結合動画に 1 回だけ付ける**（回答④＋結合要望）。クリップ処理用の**設定コピー**で `opening_enabled=false`/`ending_enabled=false` にして `run_pipeline` へ渡し、**結合段階**で `[Opening] + clip1..clipK + [Ending]` を 1 本に結合する（§4.2.1）。OP/ED の有無は**現行の `general.opening_enabled`/`ending_enabled` ＋素材有無**で判定（クリップ用と同一ルール）。
3. **音量/カット閾値ダイアログは出さない**（回答①）。クリップ処理に `volume_analysis_callback` を**渡さない**（＝保存済み `last_cut_db` で無音カット）。編集画面のみクリップ毎に表示。
4. **クリップ選択 UI は現状維持**。採点後の簡易結果ダイアログ（TOP5 の使用可否選択）はそのまま。**「完了」後に、選択した各クリップを順に**「無音カット → テロップ編集画面 → 焼き込み」へ通し、**最後に結合**する（§4.1）。
5. **ワーカー→メインスレッドの橋渡しを流用**。`ArchiveClipWorker` に `SubtitleReviewBridge` を注入し、クリップごとに編集画面（`SubtitleEditorDialog`）を出す（§4.3）。
6. **最終出力は 1 本**。`archive_<元名>_combined.mp4` を出力先へ配置（§4.2.1）。個別クリップは中間ファイル（一時領域）として結合後に破棄する（保持オプションは §8-6）。
7. **設定は setting.json 管理**。工程スイッチ（無音カット／編集画面／結合／OP/ED）を `archive.clip_pipeline`・`archive.combine` で切替可能にする（ハードコード回避・§4.5）。既定は要望どおり「無音カット ON・編集画面 ON・結合 ON・OP/ED は general 設定に従う」。
8. **後方互換**：現行クリップ用フローは一切変更しない。変更は `archive` 配下（`clip_writer` / `archive_tab` / 設定追加）に限定する。

---

## 4. 詳細設計

### 4.1 変更後のアーカイブ処理フロー

```
[アーカイブタブ]
  採点開始 → 採点(方式A) → 簡易結果ダイアログ(TOP5使用可否) → 「完了」
        │
        ▼  ArchiveClipWorker (ワーカースレッド)
  選択した各クリップ n=1..K について順に:
     ① 区間 [start,end] を切り出し (現行どおりストリームコピー)
     ② run_pipeline(切り出しclip, clip用settings[OP/ED off・出力先=一時領域],
                    subtitle_review_callback = SubtitleReviewBridge,  ← テロップ編集画面
                    volume_analysis_callback = None)                  ← 音量ダイアログ出さない(①)
         └ 内部で: (音量ダイアログ無し) → 無音カット → フルテロップ＋編集画面 → 出力(一時)
        └ 編集をキャンセル(PipelineCancelled) → 当該クリップのみスキップ(③) して次へ
     → burned_clip[n] (一時領域)
  最後に (全クリップ処理後):
     ③ 結合: parts = [Opening?] + burned_clip[1..K] + [Ending?]
             concat_processor.concat(parts, final)   ← OP/ED を1回だけ付与し1本に結合(④)
     ④ final を 出力先へ archive_<元名>_combined.mp4 として配置
        ▼
  完了メッセージ (結合1本の出力パス / スキップ件数)
```

- **クリップごとに 1 回**「テロップ編集画面」が開く（音量ダイアログは出さない＝回答①）。K 件選べば K 回、**順番に**表示される（`run_pipeline` が同期実行のため自然に直列化）。
- 編集画面・無音カットの中身は**クリップ用と完全に同一**（`SubtitleEditorDialog` の列/挙動、無音カットの閾値・パラメータ）。
- **最終成果物は 1 本**（結合済み・OP/ED 付き）。

### 4.2 `clip_writer.write_clips()` の改修（クリップ処理）

現在の「切り出し → `subtitle_generator.run` だけ」を、**「切り出し → `run_pipeline`（クリップ用と同一の工程）」**へ置き換え、**各クリップの焼き込み結果を一時領域に集める**。

```
# 方針（実装はレビュー後）
clip_settings = deepcopy(settings)
# OP/ED は各クリップには付けない (結合段階で1回だけ付ける / §3-2)
clip_settings["general"]["opening_enabled"] = False
clip_settings["general"]["ending_enabled"] = False
# 各クリップの出力は一時領域へ (最終結合まで出力先を汚さない)
clip_settings["general"]["output_directory"] = <temp_dir>

burned = []
for n, clip in enumerate(used_clips, 1):
    raw = _cut_region(input, clip.start, clip.end)          # 現行どおり (copy)
    try:
        out = run_pipeline(
            raw, clip_settings,
            progress_cb=<sub-progress>,
            subtitle_review_callback=review_cb,   # ← 注入時: 無音カット＋編集画面あり
            volume_analysis_callback=None,        # ← 回答①: 音量ダイアログは出さない
        )
        burned.append(out)
    except PipelineCancelled:
        _logger.info("clip%d: 編集キャンセル → スキップ", n)   # 回答③: 当該クリップのみskip
        continue
```

- `run_pipeline` は内部で **無音カット → フルテロップ＋編集画面 → 出力** を実行（OP/ED は上記フラグでスキップ、音量ダイアログは callback 無しでスキップ）。
- `write_clips(..., review_callback=None)` を**任意引数**にする：
  - **GUI 実行**：ブリッジを渡す → 無音カット＋編集画面あり（要望どおり）。
  - **コールバック無し（CLI/テスト）**：`run_pipeline` は編集画面をスキップし全件自動焼き込み（＝従来相当・後方互換）。無音カットは設定に従い実行。

> **代替案（不採用）**：`run_pipeline` を使わず `silence_cutter.run`→`subtitle_generator.run` を直接呼ぶ方式。工程順・将来変更の二重管理になるため **`run_pipeline` 再利用**を採る（§3-1）。

### 4.2.1 結合（TOP5 → 1本 ＋ OP/ED）

全クリップ処理後、焼き込み済みクリップを**時系列順に結合**し、OP/ED を付けて 1 本にする。**既存 `concat_processor.concat(parts, output, ffmpeg_cfg)`**（複数動画を解像度/SAR/fps 正規化して結合）を再利用する。

```
# OP/ED は現行ルール(general フラグ＋素材有無)で判定 (concat_processor.run と同一思想)
parts = []
if opening_enabled and is_available(opening_video):  parts.append(opening_video)
parts += burned                                       # clip1..clipK (時系列順)
if ending_enabled and is_available(ending_video):    parts.append(ending_video)

final = os.path.join(out_dir, f"{prefix}_{stem}_combined.mp4")
if len(parts) >= 2:
    concat_processor.concat(parts, final, ffmpeg_cfg, total_duration=Σdur, on_progress=...)
elif len(parts) == 1:
    shutil.move(parts[0], final)      # 1本のみ(OP/ED無し・クリップ1件)ならそのまま
return [final]                        # 最終成果物は結合1本
```

- **OP/ED は結合1本に対して 1 回だけ**付く（回答④）。`opening_enabled`/`ending_enabled`/`opening_video`/`ending_video` は**現行 general 設定を流用**（ユーザーが設定画面で有効化・素材指定していれば付く）。
- `concat` は全 part を `ffmpeg.output_width/height/fps/audio_sample_rate` へ正規化するため、クリップ・OP・ED の解像度差は吸収される（横想定）。**縦クリップの結合**は横キャンバスへレターボックスされ得る点を明記（§8-5）。
- 使用クリップが 0 件 → 結合をスキップし「候補なし」を通知。
- 個別クリップ（`burned`）は一時領域に置き、結合後に破棄（保持オプションは §8-6）。

### 4.3 `ArchiveClipWorker`（`archive_tab.py`）へのブリッジ注入

現行クリップ用（`main_window._on_run`）と同じく、**ワーカー起動時に 2 ブリッジを生成**し、`write_clips` へ渡す。

```
# ArchiveTabWidget 側 (メインスレッド) でブリッジを生成し worker へ渡す
self._review_bridge = SubtitleReviewBridge(
    parent_window=self,
    default_font=subtitle_cfg.get("font_family",""),
    default_size=subtitle_cfg.get("font_size"),
    font_families=list(QFontDatabase.families()),
)
worker = ArchiveClipWorker(input, settings, confirmed,
                           review_callback=self._review_bridge)
# 回答①: 音量/カット閾値ダイアログは出さない → VolumeThresholdBridge は注入しない
```

- `SubtitleReviewBridge` は `main_window.py` にある既存クラス。**アーカイブタブから import して流用**（新規実装しない）。`VolumeThresholdBridge` は**注入しない**（回答①：音量ダイアログを出さない）。
- ブリッジは `QueuedConnection` でメインスレッドにダイアログを開き、ワーカーを待機させる既存機構のため、**クリップごとに順番に編集画面が出る**（`run_pipeline` が同期実行のため自然に直列化）。
- 追加フォント登録（`register_fonts_in_dir(resolve_fonts_dir(settings))`）も現行クリップ用と同様に採点/切り抜き前に実行する（個別フォントを編集画面へ反映・resolve16 整合）。

### 4.4 クリップ中断（レビューのキャンセル）の扱い

現行 `run_pipeline` は編集画面キャンセルで `PipelineCancelled` を送出し**そのパイプラインを中断**する。アーカイブは**複数クリップ**を回すため、**回答③により**：

- `PipelineCancelled` を **そのクリップだけスキップ**して次のクリップへ進む（他クリップは継続）。
- 結合は**スキップ後に残ったクリップ**で行う。全件スキップ（残 0）なら結合せず「候補なし/全スキップ」を通知。
- 最後に「結合 1 本を出力／スキップ M 件」を通知。

### 4.5 setting.json 追加案（追加のみ・既定は要望どおり）

`archive` に**クリップ処理と結合のスイッチ**を追加する（ハードコード回避）。

```jsonc
"archive": {
  // …既存(enabled/download/scoring/output)は不変…
  "clip_pipeline": {
    "silence_cut": true,        // 各クリップで無音カットを行う (回答: ON)
    "subtitle_review": true,    // 各クリップでテロップ編集画面を出す (回答: ON)
    "volume_dialog": false      // 音量/カット閾値ダイアログを出すか (回答①: false)
  },
  "combine": {
    "enabled": true,            // TOP5 を結合して1本にする (追加要望: ON)
    "opening_ending": true,     // 結合1本に OP/ED を付けるか (回答④: ON。実体は general フラグ＋素材有無)
    "keep_individual": false,   // 個別クリップも残すか (既定: 残さない / §8-6)
    "combined_suffix": "combined"  // 出力名 archive_<元名>_<suffix>.mp4
  }
}
```

- `silence_cut`/`subtitle_review` の実体は**現行設定**（`silence_cut.enabled`/`subtitle.review_enabled`）で制御されるため、`clip_pipeline` は「クリップ処理時にそれらを ON/OFF する上書き指示」として使う（clip 用 settings コピーへ反映）。`volume_dialog=false` で音量ダイアログを出さない（callback 非注入）。
- `combine.opening_ending=true` かつ **`general.opening_enabled`/`ending_enabled` が ON かつ素材有** のとき、結合1本に OP/ED を付ける（クリップ用と同一ルール）。＝ユーザーが設定画面で OP/ED を有効化していれば付く。
- `settings_window.DEFAULT_SETTINGS["archive"]` に `clip_pipeline`/`combine` を追加（`_merge_with_defaults` が欠落補完＝後方互換）。

### 4.6 変更対象ファイル一覧（予定）

| ファイル | 変更概要 |
|---|---|
| `src/archive/clip_writer.py` | `write_clips` を「各クリップ: 切り出し → `run_pipeline`（無音カット＋編集画面, OP/ED off, 出力=一時領域）→ 一時に集約」→「最後に `concat_processor.concat` で `[OP]+clips+[ED]` を 1 本へ結合し `archive_<元名>_combined.mp4` を出力」に改修。`review_callback` 引数追加。`PipelineCancelled` は当該クリップskip |
| `src/gui/archive_tab.py` | `ArchiveClipWorker` に `review_callback` を追加し `write_clips` へ渡す。`ArchiveTabWidget` で `SubtitleReviewBridge` を生成・注入＋フォント登録。完了通知は「結合1本のパス/スキップ件数」 |
| `src/gui/main_window.py` | `SubtitleReviewBridge` を archive 側から import 可能に（既存クラスの再利用のみ。ロジック変更なし） |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["archive"]` に `clip_pipeline`/`combine` 追加 |
| `src/settings/setting.json` | `archive.clip_pipeline`/`archive.combine` 既定値追加 |

---

## 5. エラー処理・フォールバック方針
- **編集画面キャンセル**（`PipelineCancelled`）→ §4.4 の方針（既定：当該クリップのみスキップして継続）。
- **コールバック未注入（CLI/テスト）** → `run_pipeline` は編集画面をスキップし自動焼き込み（無音カットは設定どおり）。＝R1 相当の後方互換。
- **無音カットで全区間が無音判定 等の空結果** → 既存 `silence_cutter`/パイプラインの挙動に委譲（現行クリップ用と同一）。
- 例外は既存 `AutoEditError` 系を流用し、GUI へ集約通知（`ArchiveClipWorker.failed`）。

## 6. ログ出力方針
- クリップごとに「clipN: 無音カット/テロップ編集/焼き込み 実行」を INFO。
- スキップ（キャンセル）件数・出力件数を INFO（例「出力 N 件／スキップ M 件」）。

## 7. 影響範囲・後方互換
- **現行クリップ用フロー（`run_pipeline` / main_window）は不変**。本改修は `archive` 配下と設定追加のみ。
- **R1 の採点・TOP5 選択・出力命名は不変**。変わるのは「完了後の各クリップ処理」に **無音カット＋編集画面**が入る点のみ。
- **新規依存なし**。既存の `run_pipeline` / ブリッジ / `SubtitleEditorDialog` の再利用。

---

## 8. 確認事項 → **回答反映（2026-07-22）**

| # | 項目 | 回答（確定） |
|---|---|---|
| 1 | 音量/カット閾値ダイアログをクリップ毎に出すか | **出さない**（`VolumeThresholdBridge` 非注入・保存済み `last_cut_db` で無音カット） |
| 2 | 編集画面はクリップ毎に都度か | **クリップ毎に順番**（`run_pipeline` 同期実行で自然に直列。K 件＝K 回） |
| 3 | 1クリップの編集キャンセル時 | **当該クリップのみスキップして継続**（残クリップで結合） |
| 4 | OP/ED | **付ける（結合した1本の最終動画に1回だけ）**。実体は現行 `general.opening_enabled`/`ending_enabled`＋素材有無 |
| 5 | 縦動画対応（残論点） | 各クリップの無音カット/焼き込みは `output_profile` で自動対応。ただし**結合は横キャンバスへ正規化**（`concat` が output_width/height に統一）。縦VOD由来クリップはレターボックスされ得る点は仕様として明記（R1 は横想定） |
| 6 | 個別クリップの保持（新規） | **既定は残さない**（`combine.keep_individual=false`）。結合1本のみ出力。要望あれば true で個別も保存可 |

> 上記確定により実装フェーズへ移行する（CLAUDE.md「実装は設計書レビュー後に行う」）。

---

## 9. 段階実装（レビュー後）
1. `clip_writer.write_clips` を改修：各クリップを `run_pipeline`（OP/ED off・出力=一時領域・`volume_analysis_callback=None`）で処理し一時に集約 → `PipelineCancelled` は skip。`review_callback` 引数追加。
2. **結合**：`concat_processor.concat` で `[OP?]+clips+[ED?]` を 1 本に結合し `archive_<元名>_combined.mp4` を出力（§4.2.1）。個別クリップは破棄（`keep_individual` で保持可）。
3. `ArchiveClipWorker`／`ArchiveTabWidget` に `SubtitleReviewBridge` 注入＋フォント登録。完了通知を「結合1本＋スキップ件数」に。
4. `archive.clip_pipeline`/`archive.combine` 設定追加（§4.5）。
5. 検証：TOP5 選択 → 各クリップで **無音カット＋テロップ編集画面**が現行クリップ用と同様に出て編集結果が焼き込みへ反映 → **全て結合＋OP/ED 付きの 1 本**が出力される。キャンセル時のスキップ、結合順（時系列）、出力命名 `archive_<元名>_combined.mp4` を確認。

> 本改修は flow17 の **R1 の一部修正**として扱い、リリースする場合は R1（v1.2.0）に同梱、または追補パッチ（v1.2.x）とする。
