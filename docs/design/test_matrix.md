# テスト項目一覧と不足の精査

最終更新日: 2026-11-06 / 対象: v1.8.2 (`docs/design/design.md` と対) 

本書は「設計書に書かれている振る舞いが、どのテストで守られているか」を対応づけ、
**抜けている観点**を優先度付きで挙げたものである。

---

## 0. 実行方法と現状

```powershell
# PySide6 を入れた環境で実行する (開発端末では Python 3.12 + PySide6 6.11)
python -m unittest discover -s tests
```

| 項目 | 実測 (2026-11-06) |
|---|---|
| テストファイル | 119 (`tests/test_*.py`) |
| テストメソッド | **2272** |
| 結果 | **全件成功 (失敗 0 / エラー 0 / スキップ 0)** 約 37 秒 |
| フレームワーク | 標準 `unittest` のみ (pytest は未導入) |
| 外部依存 | ネットワーク・実 FFmpeg 実行・実モデル推論はすべて差し替え済み |

---

## 1. 設計章 ↔ テスト対応表

| 設計書の章 | 守っているテスト (代表) | 判定 |
|---|---|---|
| §3.1 経路 A (Timeline モード) 全体 | `test_watermark_integration` (run_pipeline 通し) / `test_clip_tab_selection` / `test_pipeline_disk_guard` / `test_quality_resolve3` | ◯ |
| §3.2 経路 A' 従来フロー (physical) | `test_combine_stream_copy` (concat) / `test_settings_migration` (mode 既定) | △ 通しの検証が無い |
| §3.3 経路 C 再開 (`run_from_project`) | `test_media_recovery` / `test_archive_project_resume` / `test_timeline_project_io` / `test_autosave_default` | **△ ランナーの通しが無い** (§2-①) |
| §3.4 経路 B アーカイブ切り抜き | `test_audio_scan` / `test_quality_resolve1` / `test_archive_prepare` / `test_archive_timeline` / `test_archive_section_add` / `test_combine_stream_copy` / `test_vod_range` / `test_vod_reuse` / `test_vod_retention` / `test_legacy_cache` / `test_twitch_quality` / `test_archive_markers` / `test_intro_source_refetch` / `test_archive_intro_source` | ◯ |
| §3.5 縦動画 | `test_vertical_builder` / `test_vertical_archive` / `test_vertical_render` / `test_vertical_dialog` / `test_vertical_subtitle_layout` / `test_vertical_subtitle_tracks` / `test_vertical_media_copy` / `test_crop_layout` / `test_crop_all_media` | ◯ |
| §3.6 Resolve 出力 | `test_fcpxml_builder` / `test_resolve_export` / `test_watermark_resolve_export` / `test_subtitle_align` | ◯ |
| §3.7 進捗 (`run_ffmpeg_progress`) | `test_quality_resolve3` / `test_renderer_timing` | △ 進捗タイムアウトの検証が無い (§2-⑤) |
| §4.2 正規化 | `test_loudness_normalizer` | ◯ |
| §4.2 音量解析・カット閾値 | `test_quality_resolve1` | △ 確定値の保存経路が薄い (§2-③) |
| §4.2 無音カット / 編集点 | `test_quality_resolve1` / `test_subtitle_cut_alignment` / `test_section_add_offset` | ◯ |
| §4.2 音声認識・テロップ整形 | `test_quality_resolve1` / `test_subtitle_length_units` / `test_subtitle_time_format` / `test_subtitle_vad_params` / `test_speaker_filter` / `test_font_advance` / `test_comment_decor` / `test_comment_bg_width` / `test_comment_placement` | ◯ |
| §4.2 焼き込み・合成 | `test_compositor` / `test_render_direct_burn` / `test_renderer_timing` / `test_proxy_render` / `test_output_canvas` / `test_base_zoom` | ◯ |
| §4.2 ぼかし | `test_blur_core` / `_decisions` / `_plan` / `_track` / `_cache` / `_render` / `_preview` / `_contour` / `_dialog` / `_renderer_integration` | ◯ |
| §4.2 透かし | `test_watermark_overlay` / `test_watermark_integration` / `test_watermark_archive` / `test_watermark_resolve_export` | ◯ |
| §4.2 出力配置 (`output_writer`) | `test_watermark_integration` のみ | **△ 単体テストが無い** (§2-②) |
| §4.3 Timeline モデル・編集 | `test_timeline_model` / `test_timeline_commands` (99) / `test_timeline_clipboard` / `test_timeline_snap` / `test_command_stack_clean` / `test_timemap` | ◯ |
| §4.3 プロジェクト JSON | `test_timeline_project_io` / `test_project_library` / `test_project_media_copy` / `test_recent_project_paths` | ◯ |
| §4.3 テロップトラック | `test_subtitle_track_edit` / `test_subtitle_track_color` / `test_subtitle_new_track` / `test_subtitle_comment_track` / `test_subtitle_role_track_keep` / `test_subtitle_split_text` | ◯ |
| §4.3 プレビュー | `test_preview_playback` (53) / `test_subtitle_preview_metrics` / `test_waveform` / `test_waveform_draw` / `test_timeline_drag_perf` | ◯ |
| §5.3 文字色の優先順 | `test_subtitle_color` / `test_subtitle_track_color` | ◯ |
| §6 設定の既定値・移行 | `test_settings_migration` (47) / `test_autosave_default` / `test_theme` | ◯ |
| §7.1 認証・JWT | `test_stretheus_auth` / `test_device_id` | △ `api_client` 単体が無い (§2-①) |
| §7.2 ポイント・透かし判定 | `test_points_service` / `test_points_ui` / `test_points_refresh_pending` / `test_points_indicator_shutdown` | ◯ |
| §7.2 サブスクリプション | `test_billing` / `test_subscription_ui` / `test_subscription_stages` / `test_subscription_reflect` | ◯ |
| §7.3 ログ送信 | `test_telemetry` / `test_log_payloads` / `test_log_queue` / `test_log_sender` | ◯ |
| §7.4 Twitch | `test_twitch_quality` / `test_archive_markers` / `test_archive_login_restore` | △ `twitch_source` の失敗系が薄い |
| §7.5 自動更新 | **なし** | **× 未テスト** (§2-①) |
| §8 例外・容量・後始末 | `test_disk_space` / `test_workspace_sweep` / `test_trash` / `test_pipeline_disk_guard` | ◯ |
| §9 ログ出力 | **なし** (`utils/logger` 未テスト) | △ 影響は小 |
| §10 多言語化 | `test_i18n` (対訳の欠落検出) | ◯ |
| §11 配布・同梱物 | `test_licenses` | △ ライセンス manifest のみ |
| §12 テスト方針 | 本書 | — |

---

## 2. 不足している観点 (優先度順)

### ① 高: 一度も名前が出てこないモジュールのうち、ロジックを持つもの

テスト全文を走査した結果、**21 モジュール**が 1 度も参照されていない。
うち次の 3 つは純ロジックで検証しやすく、壊れると利用者に直接影響する。

| モジュール | 内容 | 入れるべきテスト |
|---|---|---|
| `src/services/api_client.py` | ベース URL 結合 / Bearer 付与 / **401 で 1 回だけリフレッシュ** / `ProblemDetails` の `code` を見た例外化 (`ReauthRequiredError` / `ApiOfflineError`) | 偽の opener を渡し、401 → リフレッシュ → 再送が 1 回で止まること、5xx・接続失敗が `ApiOfflineError` になること、トークンがログへ出ないこと |
| `src/utils/updater.py` | GitHub Releases の応答解析 / バージョン比較 / インストーラ取得 | `v1.8.2` と `v1.10.0` の比較 (文字列比較では誤判定する)、応答が壊れていても例外を外へ出さないこと、ダウンロード未完了時に起動しないこと (`docs/error/20260704`) |
| `src/gui/failure_message.py` | 失敗通知の文面づくり (`docs/error/fromUser/resolve.md` §6-12-3 で追加) | `FFmpegError` / `DiskSpaceError` / `TwitchError` それぞれで対象ファイル名と理由が文面へ入ること |

加えて **`pipeline_runner.run_from_project` の通しテストが無い** (`run_from_project` という
名前がテストに 1 度も出てこない)。経路 C は「アーカイブ用プロジェクトを弾く」「自動保存から
復元しても保存先は本体のまま」「再開でもポイントを予約する」という固有の約束を持つため、
`run_pipeline` と同じ流儀 (`test_watermark_integration`) の通しテストを 1 本入れるべきである。

### ② 中: 単体テストが無い主要モジュール

| モジュール | 現状 | 入れるべきテスト |
|---|---|---|
| `src/modules/output_writer.py` | `test_watermark_integration` が間接的に通るだけ | `resolve_output_path` の衝突時連番、出力先未指定時は入力と同じ場所、`finalize` の移動失敗時の扱い |
| `src/modules/volume_analyzer.py` | `test_quality_resolve1` が一部のみ | 測定値が得られないときのフォールバック、`metric` / `percentile` の切替 |
| `src/archive/comment_source.py` | `test_quality_resolve1` が笑い語の一部のみ | twitch-dl の `chat json` 形式の取り込み、セル集計、平均コメント数/分 |

### ③ 中: 「確定値が保存される」経路

カット閾値の確定値 (`volume_analysis.last_cut_db`) は無音カットの単一閾値であり、
**-102 dB へ崩壊する不具合** (`docs/error/20261102`) の震源である。
`pipeline_runner._apply_volume_analysis` の分岐 (OK / 変更しない / 解析失敗 / CLI でダイアログ未注入)
を直接検証するテストが無いため、1 本足すこと。

### ④ 中: GUI テストの `PySide6` ガードが不統一

`src/gui` を import する **30 ファイル中 10 ファイル**だけが `_QT_AVAILABLE` +
`@unittest.skipUnless` のガードを持ち、残り 20 ファイルはガード無しで import している。
そのため **PySide6 が無い環境では 20 ファイルが import エラーになる**。

```
# 設計書が標準とする Python 3.14 (PySide6 未導入) での実測
py -3.14 -m unittest tests.test_theme      → OK (skipped=39)   ガードあり
py -3.14 -m unittest tests.test_points_ui  → FAILED (errors=1) ガードなし
```

* **方針**: `src/gui` を import するテストは全ファイルで
  `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` + `try/except` +
  `@unittest.skipUnless(_QT_AVAILABLE, ...)` に揃える (`tests/test_theme.py` が手本)。
* 併せて、PySide6 をどの Python で入れるかを決める必要がある
  (設計上は 3.14、開発端末の実環境は 3.12)。

### ⑤ 低: タイムアウト・中断系

* `ffmpeg.progress_timeout_sec` を超えて無応答のときに打ち切ること (`utils/progress`)
* アーカイブ採点・書き出しの `cancel_cb` による中止が `PipelineCancelled` になり、
  ポイントが戻ること (ポイント側は `test_points_service` で担保済み、通しは未検証)

### ⑥ 低: テストが無くてよいもの (記録)

| 対象 | 理由 |
|---|---|
| `src/services/login_check.py` / `seed_points.py` | 開発者用の疎通確認 CLI |
| `src/gui/log_workers.py` / `billing_workers.py` | Qt のスレッド橋渡しのみ (中身は被テストのサービス層) |
| `src/utils/logger.py` / `stage_timer.py` | 標準 `logging` の薄いラッパ |
| `src/gui/color_field.py` / `file_drop_area.py` / `project_resume_row.py` ほかの小部品 | 表示のみ。振る舞いは呼び出し側のテストで通る |

### ⑦ 付随: 参照されていない画面モジュール (対応済み)

`src/gui/archive_result_dialog.py` は `src/` と `tests/` のどこからも参照されていなかった
(flow17 R1 の簡易ダイアログ。R2 の `ArchiveResultWindow` に置き換わったまま残っていた)。
`docs/reduce/Nouse.md` §2.1 でも「完全に未使用・削除の安全度 高」と判定されていたため、
**2026-11-06 に削除した** (削除後に全 2272 件のテストが成功することを確認)。
同種の残骸が出たときも、テストを足すのではなく削除の可否を判断する。

---

## 3. テストを足すときの約束

* ファイル先頭に「どの要望・どの障害に対するテストか」をコメントで書く (既存の流儀)。
* ネットワーク・実 FFmpeg・実モデルは呼ばない。差し替えで応答だけを与える。
* `src/gui` を使うテストは §2-④ のガードを必ず付ける。
* 新しい設定キーを足したら `test_settings_migration` に既定値と移行の検証を足す。
