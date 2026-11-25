# AI 編集が最後の検証で全部巻き戻って停止する (現象報告)

報告日: 2026-11-24 / 開発環境での実行

> 1 本動画を AI 編集にかけてみたら、エラーによって停止しました。

## 確認した事実

| 項目 | 値 |
|---|---|
| ログ | `D:\develop\autoedit\src\gui\logs\autoedit_20261124.log` (GUI を `src/gui` から起動したため `logs/` ではなくこちらへ出ている) |
| 実行 | 2026-11-24 01:30:21 開始 → 02:07:26 失敗 (約 37 分) |
| 対象 VOD | Twitch `2938439428` / 10.7 GB / 画質 source (2560x1440@60fps) |
| 採点 | 窓 222 件 → 9 セクション (clip1〜clip9) |
| 作業先 | `C:\Users\evolu\AppData\Local\Stretheus\archive_work` |
| 用途 | `youtube_long` (EditPlan のシーン 4 件) |

VOD 取得・採点・9 セクションぶんの Whisper 認識・ラウドネス正規化まで完走し、
**最後の Timeline 検証 (V15) で積んだ手を全て巻き戻して停止**した。
出力は 1 本も得られていない。

## 停止したところ

```
2026-11-24 02:07:26,326 [WARNING] src.timeline.project_io: クリップ c33 の尺 (0.031s) が下限 (0.050s) 未満のため除外します
2026-11-24 02:07:26,326 [WARNING] src.timeline.project_io: 音声クリップ a33 のリンク先 c33 が存在しないため除外します (該当区間は無音になります)
2026-11-24 02:07:26,329 [INFO]    src.ai.plan_validator: EditPlan の検証: 変更 8 件 (落とした 5 件) … / V15 timeline drop=2
2026-11-24 02:07:26,329 [ERROR]   src.gui.archive_tab: AI の編集に失敗
Traceback (most recent call last):
  File "D:\develop\autoedit\src\gui\archive_tab.py", line 399, in run
    result = ai_runner.run_ai(
  File "D:\develop\autoedit\src\pipeline\ai_runner.py", line 444, in run_ai
    run = run_plan(
  File "D:\develop\autoedit\src\pipeline\ai_runner.py", line 159, in run_plan
    timeline, report = plan_applier.apply_plan(
  File "D:\develop\autoedit\src\ai\plan_applier.py", line 72, in apply_plan
    _validate_timeline(timeline, stack, cfg, report, state)
  File "D:\develop\autoedit\src\ai\plan_applier.py", line 340, in _validate_timeline
    raise AiPlanError(tr("AI の編集を適用できなかったため元の状態へ戻しました。"))
src.exceptions.AiPlanError: AI の編集を適用できなかったため元の状態へ戻しました。
```

## 同じ実行で起きていた別件 (停止の原因ではない)

AI 呼び出しは 4 回すべて失敗しており、この実行の AI 処理は実質ゼロである。

```
2026-11-24 01:51:51,735 [WARNING] src.services.api_client: Stretheus API が 503 を返しました。
2026-11-24 01:51:51,735 [WARNING] src.ai.scene_finder: シーン評価のバッチ 0 を既存の採点へ落とします (縮退可): AI の処理に失敗しました。
2026-11-24 01:51:52,200 [WARNING] src.services.api_client: Stretheus API が 503 を返しました。
2026-11-24 01:51:52,200 [WARNING] src.ai.scene_finder: シーン評価のバッチ 1 を既存の採点へ落とします (縮退可): AI の処理に失敗しました。
2026-11-24 01:51:52,200 [INFO]    src.ai.scene_finder: シーン評価: 9 件中 0 件を AI が評価 (用途 long)
2026-11-24 01:51:52,614 [WARNING] src.services.api_client: Stretheus API が 503 を返しました。
2026-11-24 01:51:52,614 [WARNING] src.ai.planner: 構成の呼び出しに失敗したため手元で組みます: AI の処理に失敗しました。
2026-11-24 01:51:53,027 [WARNING] src.services.api_client: Stretheus API が 503 を返しました。
2026-11-24 01:51:53,027 [WARNING] src.ai.planner: 編集指示の呼び出しに失敗したため手元で組みます: AI の処理に失敗しました。
…
2026-11-24 02:07:26,336 [INFO]    src.ai.usage_log: AI 合計: calls=0 …
```

* 縮退そのものは設計どおりに働いている (`ai_unavailable` → ローカルで構成)。
* ただし**縮退した plan が出したカットで V15 に落ちた**ため、
  「AI は何もしていないのに 37 分かけて失敗した」という結果になった。
* 02:09:30 の `src.services.billing: … (HTTP 404)` は停止後の別件で、本件とは無関係。

## 再現の形

AI 編集を通す Timeline の V1 に、**0.01 秒より長く `timeline.min_clip_sec`
(既定 0.05 秒) より短いクリップが 1 本でも載っている**こと。
今回は無音カットが刻んだ 0.031 秒の有音区間がそのクリップになった。

原因と修正設計は `docs/error/20261124/resolve.md`。
