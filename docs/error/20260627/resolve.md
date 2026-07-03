# テロップ表示タイミングを発話に同期する 修正設計書

対象エラー: `docs/error/20260627/error.md`
対象ソース: `src/modules/subtitle_generator.py`（主因）/ `src/settings/settings_window.py`・`src/settings/setting.json`（設定追加）
本書はレビュー用の**修正設計書**であり、実装は承認後に行う（`docs/CLAUDE.md` の方針に準拠）。

> 前回（`docs/error/20260626/resolve.md`）は「言っていない字幕（ハルシネーション）」の除去を扱った。
> 本書は別問題で、**実発話に対するテロップの「表示する時間帯」**を扱う。カット自体は問題なし。

---

## 1. 事象

1つのカット内に発話が含まれるとき、その発話に対するテロップが**まだ発話していない区間でも表示され続ける**。
要望は「**発話しているタイミングに合わせて表示**」すること。具体ルールは以下。

- R1: 発話のタイミングでテロップを表示する。
- R2: テロップを表示し、**次の発話が無ければ最大2秒**表示して削除する。
- R3: **2秒以内に次の発話がある場合**は、（消さずに）そのまま次のテロップ表示へ切り替える。

---

## 2. 原因調査

### 2-1. 結論（主因）

**テロップの表示区間（Start/End）が発話の実時間に整合していない**ことが原因。現状は以下の二点で「発話前/発話後の無音」までテロップが居座る。

1. **セグメント単位の粗いタイムスタンプ**
   `WhisperTextSource.extract()` は faster-whisper の**セグメント**境界 `seg.start`/`seg.end` をそのまま採用している（`subtitle_generator.py` L202–L210）。セグメント境界は語頭・語尾の無音や息継ぎを含むため、テロップが**発話の前後にはみ出して**表示される。

   ```python
   # 現状 (L203-L210 抜粋)
   for seg in segments:
       ...
       timeline.append({"start": float(seg.start), "end": float(seg.end), "text": text})
   ```

2. **表示終了に上限・整形が無い**
   `build_subtitle_file()` は `entry["start"]`/`entry["end"]` を無加工で ASS の `Dialogue` 行へ変換する（L302–L307）。連続セグメントは `end[i] ≒ start[i+1]` で隣接しがちなため、画面上は**テロップが途切れず連続**し、「発話していない間も前のテロップが残る」状態になる。表示終了の最大値（R2 の2秒）や、次発話への連結（R3）といった制御は存在しない。

   ```python
   # 現状 (L302-L307 抜粋)
   for entry in timeline:
       start = _format_ass_time(entry["start"])
       end = _format_ass_time(entry["end"])
       ...
       body_lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
   ```

### 2-2. 寄与要因（resolve7 の経緯）

`docs/request/resolve7.md` により、**発話区間によるテロップ絞り込み**（`_gate_with_settings()` → `gate_timeline_by_speech()`）は無効化された（`run()` L547–L554 がコメントアウト）。これは「`trim`/`split` で発話区間に丸め込む」機構で、本来は発話タイミングへ寄せる役割を持っていた。無効化により `run()` は `extract()` の素のタイムラインをそのまま `_review_timeline()`→`build_subtitle_file()` へ渡している（L558, L564）。

> 本書は resolve7 の方針（無音カットを音量解析の単一閾値へ一本化／dB閾値による発話ゲートを廃止）を**尊重**し、`speech_threshold_db` 等の**dB閾値には依存しない**方法で表示タイミングを整える（§3）。`_gate_with_settings()` 系は温存のまま触らない。

---

## 3. 修正方針

dB閾値の再導入（resolve7 と矛盾）や追加の FFmpeg 解析を増やさず、**Whisper の単語タイムスタンプ**で発話の実時間を取得し、**表示区間整形ルール**で R1〜R3 を満たす。対策は2層。

### 対策A（主）：単語タイムスタンプで「発話の頭・末」へ整合（R1）

faster-whisper の `transcribe(..., word_timestamps=True)` を有効化し、各セグメントの

- 表示開始 = **最初の単語の `start`**（語頭の無音を除去 → 発話のタイミングで表示）
- 発話終了 = **最後の単語の `end`**（語尾の無音を除去）

を採用する。`words` が `None`/空のセグメントは従来どおり `seg.start`/`seg.end` へフォールバックする（全損回避）。

**環境確認（実機 2026-06-27 時点）**

| 確認項目 | 結果 |
|---|---|
| `faster_whisper` バージョン | **1.2.1** |
| `WhisperModel.transcribe` の `word_timestamps` 引数 | **あり** |
| `Segment.words`（`Optional[List[Word]]`） | **あり** |
| `Word` の属性 | `start, end, word, probability` |

→ 追加パッケージ不要で採用可能。

#### 対策A-2（補強・任意）：セグメント内の長い無音で分割 — **不採用（確定）**

1つのセグメントが「発話→長い間→発話」を含む場合、最初〜最後の単語で丸めても間の無音中に表示が残り得る。
これを単語間ギャップで分割する案も検討したが、**レビューにより不採用**（§6-5）。まずは対策A（語頭/語尾整合）＋対策B（2秒ルール）のシンプル構成とし、必要が生じた段階で再検討する。

### 対策B（主）：表示時間ルール（最大2秒ホールド・次発話へ連結）（R2/R3）

新規関数 `adjust_display_timing(timeline, max_hold_sec, min_duration_sec)` を `run()` 内、`build_subtitle_file()` の前に適用する。入力は対策Aで得た発話実時間（`start`=発話頭, `end`=発話末）。`start` 昇順で各エントリ `i` の**表示区間**を決める。

```
disp_start = start[i]                      # 発話のタイミングで表示 (R1)

if 次エントリ i+1 がある:
    gap = start[i+1] - end[i]              # 発話末から次発話頭までの無音
    if gap <= max_hold_sec:
        disp_end = start[i+1]              # 2秒以内に次発話 → 隙間なく次テロップへ切替 (R3)
    else:
        disp_end = end[i] + max_hold_sec   # 次まで2秒超 → 発話後 最大2秒で削除 (R2)
else:
    disp_end = end[i] + max_hold_sec       # 次の発話なし → 最大2秒表示して削除 (R2)

# 安全化: disp_end は次の disp_start を超えない / disp_end > disp_start を保証
```

- `max_hold_sec`（既定 **2.0**）= R2/R3 の「2秒」。
- `min_duration_sec`（既定 **0.5** 目安）= 極短表示のチラつき防止の下限。ただし `gap <= max_hold_sec` で次発話に切られる場合は**次発話を優先**（下限で次発話に食い込まない）。

これにより、**発話前の先行表示**（語頭はみ出し）と**発話後の居座り**（2秒超のテール）の双方が解消し、R1〜R3 を満たす。

#### 適用位置

`run()` で **`_review_timeline()` の前**に `adjust_display_timing()` を適用する（L558 の直前）。
→ 字幕編集（レビュー）画面の Start/End が**最終的な表示タイミングと一致**し、ユーザーが実表示で確認・微修正できる（採否は §6-4）。

```
extract()  ──(対策A: 単語境界で start/end)──▶ timeline
        ──(対策B: adjust_display_timing)──▶ 表示区間に整形
        ──▶ _review_timeline() ──▶ build_subtitle_file() ──▶ burn_subtitle()
```

---

## 4. 変更対象と設定追加

### 4-1. コード変更（`src/modules/subtitle_generator.py`）

| 箇所 | 変更概要 |
|---|---|
| `WhisperTextSource.__init__` | `self._word_timestamps`（既定 True）等を設定から取り込み。 |
| `WhisperTextSource._transcribe` | `kwargs["word_timestamps"] = self._word_timestamps` を追加。 |
| `WhisperTextSource.extract` | timeline 構築時、`seg.words` から `start`=先頭語 start／`end`=末尾語 end を採用（`words` 無し時は `seg.start`/`seg.end` フォールバック）。 |
| 新規 `adjust_display_timing()` | §3 対策B のルールで表示区間を整形（純粋関数・テスト容易）。 |
| `run()` | `_review_timeline()` の前（L558 直前）で `adjust_display_timing()` を適用。 |

`_gate_with_settings()`/`gate_timeline_by_speech()`/`detect_speech_regions()`（resolve7 で温存中）は**変更しない**。

### 4-2. 設定追加（`subtitle` セクション）

`setting.json` と `DEFAULT_SETTINGS["subtitle"]`（`settings_window.py` L107–L146）へ同値を追加。既存キーは不変（後方互換）。欠落時は `_merge_with_defaults()` が補完する。

```jsonc
"subtitle": {
    // ... 既存項目は不変 ...
    "word_timestamps": true,          // 単語境界で発話頭/末に整合 (対策A)
    "display_max_hold_sec": 2.0,      // 発話後の最大ホールド秒 / 次発話連結の閾値 (R2/R3)
    "display_min_duration_sec": 0.5   // 表示の最小尺(チラつき防止)。次発話切替時は無効
}
```

- すべて設定で ON/OFF・調整可能とし、ハードコードを避ける（`docs/CLAUDE.md` 準拠）。
- これらは既存 `whisper_*` 系と同様、**UI 非表示**（setting.json 管理）を既定とする。UI 項目化の要否は §6-3。

---

## 5. エラー処理・ログ方針

| 事象 | 方針 |
|---|---|
| `word_timestamps` で `seg.words` が None/空 | 当該セグメントのみ `seg.start`/`seg.end` へフォールバック。デバッグログ。 |
| `word_timestamps=True` の処理時間増 | 設定で `false` 化すれば従来のセグメント境界に戻せる（退避経路を残す）。 |
| `adjust_display_timing` で `disp_end <= disp_start` | 当該エントリを破棄（不正区間）。除外件数を `INFO`。 |
| timeline 空 | 既存仕様どおりスキップ（`run()` L541–L543）。 |

- 整形の前後件数・平均表示秒・フォールバック件数を `INFO` で出力し、過剰トリミング/居座りを検証可能にする。
- ログは既存 `utils/logger.get_logger(__name__)` を使用。

---

## 6. 不明点・確認事項（レビュー反映済み）

`docs/CLAUDE.md`「不明点は推測実装せず設計書へ記載」に従い明記する。**全項目レビュー確定済み（2026-06-27）。**

1. **「2秒」の起点** → **【確定】発話末（最後の単語 end）からの最大2秒**。原文「最大2秒そのテロップを表示」を「表示開始から通算2秒」と読むと発話が2秒超のとき発話中に消えてしまうため、発話末起点を採用する。
2. **`min_duration_sec`（チラつき下限）の要否** → **【確定】採用**（既定 0.5 秒）。次発話で切られる場合は次発話を優先。
3. **設定の UI 項目化** → **【確定】setting.json 管理（UI 非表示）**。`whisper_*` 同様、字幕タブには出さない。
4. **レビュー画面への反映タイミング** → **【確定】レビュー前に適用**。編集画面の Start/End を最終表示に一致させる。
5. **対策A-2（セグメント内分割）の採否** → **【確定】不採用（既定 OFF も設けない）**。対策A＋Bのシンプル構成とする。
6. **発話末の微小パディング** → **【確定】設けない**。発話末＋最大2秒ホールドで十分とみなす。

---

## 7. 検証方針

1. **R1**：発話開始前にテロップが先行表示されないこと（`disp_start` ＝ 発話頭）。語頭の無音はみ出しが消えることを確認。
2. **R2（単発）**：後続発話が無いテロップが、発話後**最大2秒**で消えること。
3. **R3（連結）**：2秒以内に次発話があるとき、隙間なく次テロップへ切り替わること（前テロップが残らない／消えてから出ない）。
4. **境界**：`gap` がちょうど 2.0 秒、負（重なり）のケースで `disp_end > disp_start` が保たれること。
5. **異常系**：`seg.words` 取得不可時にフォールバックし、テロップが全損しないこと。
6. **回帰**：`word_timestamps=false` で従来挙動（セグメント境界）に戻せること。
