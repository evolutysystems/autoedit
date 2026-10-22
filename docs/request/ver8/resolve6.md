# ver8 resolve6 — テロップがカットの頭からずれる (発話とテロップの位置合わせ)

対象: `autoedit` (Stretheus クライアント) / Timeline 経路
関連: `docs/request/ver3/resolve.md` §7.3, `docs/error/20260627/resolve.md`,
      `docs/error/20260812/resolve2.md` §4, `docs/request/ver8/resolve5.md` 第2部

作成日: 2026-10-20 (main `3f15736`)
追補: **第3部 §10 以降** — request6.md 「(追加)」(第2部の実装でテロップが細かく裁断された) への修正設計
追補2: **第4部 §18** — 第3部の実装でクリップ頭の 1 文字が抜ける件の原因と対策

---

## 0. 要望と結論

> Timeline 画面でテロップと発話のタイミングがずれている原因を教えてほしい。
> 映像＆音は発話のタイミングでいい感じに切れているのに、テロップは
> 基本的にそのカットの**前から**流れている。カットの先頭とテロップの先頭が
> ぴったり同じ時間にあるのが好ましい。毎回手で位置調整していて面倒。

### 0.1 結論 (原因)

ずれているのは「テロップの時刻」ではなく、**テロップを切る単位**です。

映像カットの単位は **無音 0.6 秒以上で区切った有音区間** (`keep_segments`)。
テロップの単位は **Whisper が返す認識セグメント** (文の区切り)。
この 2 つは別の基準で切られており、一致する保証がどこにもありません。

そして `map_items_to_timeline()` は、カットを跨いだ認識セグメントを
**「最初のかけらの開始 〜 最後のかけらの終了」で 1 本にまとめます**
(`src/modules/subtitle_generator.py`)。詰めた軸の上では前後のカットが
連続するため、`TimeMap._merge_adjacent()` が必ず 1 本に繋げてしまう。

結果、**1 つのテロップが前のカットの途中から始まり、次のカットまで流れる**。
これが「カットの前からテロップが流れる」の正体で、現状これは
**テストで仕様として固定されています** (§2-1)。

さらに `adjust_display_timing()` の規則 R3 (「2 秒以内に次の発話があれば
次の発話開始まで表示」) が、前のテロップをカット境界を越えて
次のカットの頭まで残します (§2-3)。頭で切り替わらないのはこれが原因です。

### 0.2 結論 (直し方)

**テロップをカットを跨がせないようにし、各カットの「先頭テロップ」の開始だけを
カット頭へぴったり合わせる。**

1 カットの中にテロップが何本あってもよく (3 本以上が普通)、
**本数は一切変えません**。2 本目以降は従来どおり認識の発話タイミングで出します。
詳細と図は §3-3。

単語タイムスタンプ (`subtitle.word_timestamps` は既定で ON) を活かし、
カットを跨いだ認識セグメントを**単語単位で分けて**各カットへ配る。
そのうえで開始をカット頭へ吸着させ、終了をカット終端で止める。

加えて、すでに作ったプロジェクトや手編集後のために、Timeline 編集画面へ
**「字幕をカット頭へ揃える」** (Undo 可) を足す。

### 0.3 実装状況 (2026-10-20)

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | 単語タイムスタンプを捨てずに持つ (`WhisperTextSource._word_spans`) | 完了 |
| 2 | カット跨ぎを単語単位で分ける (`split_items_by_keep_segments`) | 完了 |
| 3 | 開始をカット頭へ揃える (`align_items_to_cut_heads`) | 完了 |
| 4 | 表示をカット終端で止める (`adjust_display_timing(cut_bounds=)`) | 完了 |
| 5 | `recognize_for_timeline()` の処理順序の組み直し | 完了 |
| 6 | `TimeMap.timeline_bounds()` | 完了 |
| 7 | 編集画面のコマンド `AlignSubtitlesToCutHeads` (Undo 可) | 完了 |
| 8 | 右クリックメニュー・`Ctrl+Shift+A`・状態表示 | 完了 |
| 9 | 設定 5 キー (`DEFAULT_SETTINGS` + `setting.json`) | 完了 |
| 10 | 英語対訳 6 件 (`catalog_en.py`) | 完了 |
| 11 | `tests/test_subtitle_cut_alignment.py` (20 件) | 完了 |
| 12 | `tests/test_timeline_commands.py` へ整列コマンド (11 件) | 完了 |

`python -m unittest discover -s tests` → **1361 件 OK** (従来 1330 + 新規 31)。

---

# 第1部 — 現状分析

## 1. 時間軸の系譜

Timeline 経路 (`pipeline_runner._run_timeline`) の時刻は 3 つの軸を通ります。

| # | 軸 | 作る場所 | 中身 |
| --- | --- | --- | --- |
| 1 | 素材時間 | 元動画 (ラウドネス正規化後。映像は `-c:v copy` なので軸は同一) | 認識はここで行う |
| 2 | 詰めた軸 | `map_items_to_timeline()` | 残す区間だけを連結した軸 |
| 3 | Timeline 時間 | `builder.build()` | 詰めた軸 + OP の尺 (`body_offset`) |

重要なのは **②の詰めた軸で、映像クリップと字幕クリップが同じ原点を共有する**
ことです。`builder.build()` は

* 映像: `cursor += duration` の累積で `keep_segments` を順に並べる
* 字幕: `timeline_start = start + body_offset` (一律加算のみ)

としているため、軸そのものはズレていません。OP の尺も両方へ同じだけ乗ります。
**つまりズレは「軸の計算」ではなく「区間の切り方」で起きています。**

### 1-1. 映像カットの頭はどこで決まるか

```
silence_cutter.detect_edit_points()
  → detect_silence(input, last_cut_db, cut_min_silence_sec)   # 既定 -20dB / 0.6s
  → build_keep_segments(silence_ranges, total)                # 無音の補集合
```

`keep_segments` の各頭 = **「0.6 秒以上続いた無音が終わった瞬間」**
(= 音量が `last_cut_db` を上回った時刻)。パディングは一切足していません
(`build_keep_segments` は補集合をそのまま返す)。
つまり**カット頭は「音が戻った瞬間」そのもの**で、ここは正しい。

### 1-2. テロップの区間はどこで決まるか

```
WhisperTextSource.extract()
  → model.transcribe(..., vad_filter=True,
                     vad_parameters={min_silence_duration_ms: 500,
                                     speech_pad_ms: 100},
                     word_timestamps=True)
  → _speech_bounds(seg)         # 先頭語 start 〜 末尾語 end
  → [{"start", "end", "text"}]  ← 単語情報はここで捨てている
```

区間の切り目は **Whisper の認識セグメント境界** = 文・句の区切りです。
`_speech_bounds()` で語頭/語尾の無音は除いていますが、**セグメントを
どこで切るかは変えていません**。

### 1-3. 詰めた軸へ写すところ

```python
# src/modules/subtitle_generator.py
def map_items_to_timeline(timeline_items, keep_segments):
    timemap = TimeMap.from_segments(keep_segments)
    for entry in timeline_items or []:
        pieces = timemap.split_interval(entry["start"], entry["end"])
        if not pieces:
            dropped += 1
            continue
        # 先頭の開始〜末尾の終了で 1 区間にする
        mapped.append({**entry, "start": pieces[0][0], "end": pieces[-1][1]})
```

`TimeMap.split_interval()` は残す区間ごとの重なりを取り、
最後に `_merge_adjacent()` で連続するかけらを繋ぎます。
**詰めた軸では隣り合うカットは必ず連続している**ので、
カットを跨いだ区間は例外なく 1 本に繋がります。

---

## 2. 原因

### 2-1. 原因A — カットを跨いだ認識セグメントが「前のカットの途中」から始まる (**本命・確定**)

既存テストがこの挙動をそのまま固定しています。

```python
# tests/test_subtitle_timeline_map.py
_KEEP = [(0.0, 5.0), (10.0, 15.0)]      # 詰めた軸では 2 本目の頭 = 5.0

def test_across_cut_is_merged_into_one(self):
    mapped, dropped = self._map([_item(4.0, 11.0)])
    self.assertAlmostEqual(mapped[0]["start"], 4.0, places=6)   # ← 2 本目の頭より 1.0 秒手前
    self.assertAlmostEqual(mapped[0]["end"],   6.0, places=6)
```

2 本目のカットの頭は 5.0 なのに、そのカットの発話を含むテロップは
**4.0 から出ます**。要望の「カットの前から流れている」と完全に一致します。

#### なぜ跨ぎが頻発するのか (ここが肝)

`whisper_vad_filter = True` で **Silero VAD が無音を除いた音声をモデルへ渡します**。

```python
kwargs["vad_filter"] = True
kwargs["vad_parameters"] = {"min_silence_duration_ms": 500, "speech_pad_ms": 100}
```

つまり**モデルは 0.6 秒の「間」を音響的に観測できません**。
間が消えた音声の上では文が途切れないので、Whisper は
「間の前の語」と「間の後の語」を**同じセグメントに入れます**。
その後 faster-whisper が時刻だけを素材時間へ戻すため、
**セグメントは素材上ではカットを跨いだ形で返ってきます**。

`whisper_condition_on_previous_text = False` にしても、
セグメント境界の決まり方 (文単位) は変わりません。
**跨ぎは例外ではなく通常形**です。だから利用者は「毎回」直すことになります。

### 2-2. 原因B — カット頭と一致させる処理がどこにも無い (**確定**)

`split_interval()` は「カット済み区間に落ちた開始」をカット頭へ丸めます
(`overlap_start = max(start, entry.source_in)`)。
そのため**跨ぎでないテロップは偶然ちょうど頭に乗ります**。しかしこれは
クランプの副作用であって、意図した整列ではありません。

* 認識の開始がカット頭より**後ろ**なら、その差はそのまま残る
  → カット頭に間ができる (テロップが遅れて出る)
* 跨いでいる場合はクランプが効かない (前のカットに実体があるため)
  → §2-1

**「カット頭に合わせる」という規則が 1 行も無い**のが原因Bです。

### 2-3. 原因C — 前のテロップがカット境界を越えて次のカットの頭まで残る (**確定**)

```python
# adjust_display_timing() 規則 R3
if has_next and (next_start - speech_end) <= max_hold_sec:
    disp_end = next_start          # 次の発話開始まで表示し、隙間なく切り替える
```

`display_max_hold_sec = 2.0` のため、次のテロップが次のカットにあると、
**前のテロップは境界を越えて次のカットの頭まで出続けます**。
「カットの頭ではもう次のテロップに変わっていてほしい」という感覚に対し、
現状は「カットの頭ではまだ前のテロップが出ている」。
これも「テロップが前から流れている」と見えます。

### 2-4. 原因D — カット頭と発話頭は物理的に別物 (**素材依存・決めの問題**)

| | 基準 | 傾向 |
| --- | --- | --- |
| カット頭 | 音量が `last_cut_db` (既定 **-20dB**) を上回った瞬間 | 閾値が高い = **遅め** |
| 認識の発話頭 | Silero VAD + `speech_pad_ms = 100` + 単語 DTW | 感度が高い = **早め** |

数十〜数百 ms 食い違うのは正常で、**どちらが「正しい発話頭」とも言えません**。
だから「どちらに合わせるか」を決める必要があります。
利用者が見ているのは映像が切れている位置なので、
**カット頭を正とする**のが唯一自然な決めです (§3 R2)。

### 2-5. 原因E — 跨ぎを残したまま手編集すると直らない (**確定・二次被害**)

リップル削除は音声以外の全トラック (= 字幕も) を対象にします
(`commands.ripple_target_tracks`)。ただし区間を跨ぐクリップの扱いは

```python
# commands._apply_range_removal() 規則 6
if head > _EPS and tail > _EPS:
    clip.duration -= delta          # 分割せず尺を縮める
```

**跨ぎ字幕は「分割せず尺だけ縮む」ので、開始は前のカットに残り続けます**。
生成時に跨ぎを解消しておかないと、編集するたびに手直しが必要になる。
要望の「毎回位置調整している」はここまで含めた構造的な帰結です。

### 2-6. 原因の切り分け表

| 見え方 | 原因 | 根拠 |
| --- | --- | --- |
| テロップが 1 カットぶん前から流れ、次のカットまで続く | A | §2-1 |
| カット頭に数百 ms の間があってからテロップが出る | B + D | §2-2 / §2-4 |
| カット頭で前のテロップがまだ残っている | C | §2-3 |
| 手で直した後、別の編集で再びずれる | E | §2-5 |
| 全編が一定量ずれる | **該当しない** | 軸は共有している (§1) |

> 全編一定のオフセットではないことが切り分けの要点です。
> 「カットごとにずれ方が違う」= 区間の切り方の問題であって、
> 軸・fps・量子化の問題ではありません (そちらは resolve5 第2部で対処済み)。

---

# 第2部 — 修正設計

## 3. 方針

| # | 規則 | 内容 |
| --- | --- | --- |
| R1 | **カットを跨がせない** | 認識セグメントがカットを跨ぐときは、単語タイムスタンプで分けて各カットへ配る。**1 カット内のテロップの本数は制限しない** (§3-3) |
| R2 | **先頭だけカット頭へ** | 各カットの**先頭**テロップの開始を、そのカットの頭へぴったり合わせる (許容差内)。2 本目以降は認識の発話頭のまま |
| R3 | **終端で止める** | テロップの終了をそのカットの終端で止める (カット頭で必ず切り替わる) |
| R4 | **安全側へ倒す** | 単語タイムスタンプが取れない素材/エンジンでは従来どおり 1 本で通す。設定 1 つで旧挙動へ戻せる |
| R5 | **後からでも直せる** | Timeline 編集画面に「字幕をカット頭へ揃える」(Undo 可) を足す |

### 3-1. なぜ「単語単位で分ける」のか

跨ぎテロップの前半は、**本当に前のカットで話された語**です。
だから「開始を後ろのカット頭へ動かす」だけでは、前半の語が
話していない位置に出てしまいます。テキストごと分けるのが唯一正しい。

`word_timestamps = True` は既定で ON、`_speech_bounds()` が単語を読んでいる
ので、**必要な情報はすでに手元にあります。捨てているだけ**です。

### 3-2. 処理順序 (ここを間違えると効かない)

```
extract()                      素材時間 + words
  ↓ ★新 split_items_by_keep_segments()   跨ぎを単語単位で分ける (素材時間のまま)
map_items_to_timeline()        詰めた軸へ (跨ぎが無いので merge は起きない)
  ↓ ★新 align_items_to_cut_heads()       開始をカット頭へ吸着
adjust_display_timing(..., cut_bounds=)  ★終了をカット終端でクランプ
  ↓
items → builder.build()        body_offset を一律加算 (整列は保たれる)
```

* 分割は**写像より前**。素材時間でないとカット境界と単語を突き合わせられない。
* 整列は**整形より前**。整形は `next_start` から終了を決めるため、
  先に開始を確定させないと伸ばし先が旧位置になる。
* **整列は開始だけを触る**。終了は後段の整形が作り直すので、
  整列処理が尺の整合を気にする必要がない (実装が小さく済む)。

### 3-3. 1 カットに複数のテロップがある場合 (これが通常形)

**「1 カット = 1 テロップ」にはしません。** 1 つのカット (= 0.6 秒以上の無音で
区切られた有音区間) は十数秒〜数十秒になることがあり、その中に認識セグメントが
3 本以上入るのが普通です。**この本数は一切変えません。**

R1 の分割が本数を増やすのは **「カットを跨いだセグメント」だけ**で、
カット内の区切り (= 認識セグメントの境界) には触りません。
R2 の吸着も **カットごとに 1 回だけ**です。

```
詰めた軸        ┌─ カット2 の頭                        ┌─ カット3 の頭
V1   … カット1 ─┤ カット2 (例 12 秒)                   │ カット3 …
                │                                      │
S1   … ────────┤ [ テロップa ][ テロップb ][ テロップc ]┤ [ テロップd ] …
                 ↑ カット頭へ    ↑ 発話頭    ↑ 発話頭    ↑ カット頭へ
                   吸着 (R2)       のまま      のまま        吸着 (R2)
                                             終端はカット2 の
                                             終端で止まる (R3)
```

| 対象 | 開始 | 終了 |
| --- | --- | --- |
| カットの**先頭**テロップ (a) | **カット頭へ吸着** (許容差内 / R2) | 次のテロップの開始 (現行 R3 のまま) |
| カット内の **2 本目以降** (b) | 認識の発話頭のまま (**触らない**) | 次のテロップの開始 (現行 R3 のまま) |
| カットの**最後**のテロップ (c) | 同上 | **カット終端で止める** (R3) |

「先頭テロップ」の判定は `align_items_to_cut_heads()` の `taken` 集合
(カットごとに 1 回だけ吸着する) が担います。
幻聴字幕が落ちて 2 本目が繰り上がって先頭になった場合も、
カット頭との差が `align_tolerance_sec` (既定 0.6 秒) を超えていれば吸着しないため、
**関係ない位置のテロップがカット頭へ飛ぶことはありません**。

---

## 4. 変更点

### 4-1. `src/modules/subtitle_generator.py` — (a) 単語を捨てない

```python
    # セグメントの発話実時間(先頭語start〜末尾語end)を返す (対策A)
    def _speech_bounds(self, seg):
        ...                                     # 変更なし

    # 単語の区間列を返す (ver8 resolve6 §4-1)
    # カットを跨いだセグメントを単語単位で分けるために使う。
    # 取れない (word_timestamps=False / モデルが返さない) 場合は空リスト。
    @staticmethod
    def _word_spans(seg):
        spans = []
        for w in (getattr(seg, "words", None) or []):
            if w.start is None or w.end is None:
                continue
            spans.append({"start": float(w.start), "end": float(w.end),
                          "text": str(getattr(w, "word", ""))})
        return spans
```

`_recognize()` で `words` を載せる。**取れたときだけ**載せるため、
旧エンジン・旧設定では辞書の形が従来と 1 バイトも変わらない。

```python
            start, end = self._speech_bounds(seg)
            entry = {"start": start, "end": end, "text": text}
            words = self._word_spans(seg)
            if words:
                entry["words"] = words          # 跨ぎ分割用 (使い終わったら落ちる)
            timeline.append(entry)
```

> **`words` が他へ漏れないことの確認**
> 旧 (物理カット) 経路の `run()` は `adjust_display_timing()` を通し、
> そこで `{"start","end","text"}` を作り直すため `words` は消える。
> Timeline 経路も `recognize_for_timeline()` の最後で item を組み直す。
> したがって `words` が `SubtitleClip` / ASS / Resolve 出力へ届くことはない。

### 4-2. `src/modules/subtitle_generator.py` — (b) 跨ぎを単語単位で分ける

```python
# 認識セグメントを「残す区間」ごとに分ける (ver8 resolve6 §3-1 / R1)
# 入力・出力はどちらも素材時間。カットを跨がないセグメントは一切触らない。
#
# ・跨いでいない          → そのまま通す (従来と完全に同一)
# ・跨いでいて単語が無い  → そのまま通す (安全側 / R4)
# ・跨いでいて単語がある  → カットごとの字幕へ分ける
#
# 分けた各片の start/end は「その片に入った単語の実時間」。keep_segments の端では
# なく単語の端を使うことで、カット内での遅れ (原因B) を整列処理へ正しく渡す。
#
# 戻り値: (items, 分割で増えた件数)
def split_items_by_keep_segments(items, keep_segments, wrap=None,
                                 min_piece_sec=0.0):
    out = []
    added = 0
    for item in (items or []):
        spans = _keep_spans_of(item, keep_segments)
        words = item.get("words")
        if len(spans) <= 1 or not words:
            out.append(item)
            continue
        pieces = _words_by_span(words, spans, min_piece_sec)
        if len(pieces) <= 1:
            out.append(item)
            continue
        for part in pieces:
            text = "".join(w["text"] for w in part).strip()
            if not text:
                continue
            out.append({**item,
                        "start": min(w["start"] for w in part),
                        "end": max(w["end"] for w in part),
                        "text": wrap(text) if wrap else text,
                        "words": part})
        added += len(pieces) - 1
    return out, added
```

補助関数:

| 関数 | 役割 |
| --- | --- |
| `_keep_spans_of(item, keep_segments)` | item と重なる残す区間を昇順で返す (2 本以上 = 跨ぎ) |
| `_words_by_span(words, spans, min_piece_sec)` | 単語を区間へ振り分ける。所属は**単語の中点**で決める (境界に跨った単語は長いほうへ入る)。先頭の片の発話長が `min_piece_sec` 未満なら**その片の語を次の片の先頭へ寄せる** (§4-2-1) |

* テキストの連結は `"".join()`。faster-whisper の `word.word` は
  空白を使う言語では先頭に空白を含むため、日本語でも英語でも
  これで元の字面に戻る。
* 分けた片は**改行をやり直す必要がある** (元の `\N` は元のテキスト長で
  折ったもの)。`wrap` には
  `lambda t: wrap_lines(t, min_line_length, max_line_length, wrap_engine)`
  を渡す。

#### 4-2-1. `min_piece_sec` の意図 (要望への直球)

跨ぎセグメントの先頭に、前のカットの**末尾の 1 語だけ**が残ることがある
(VAD が無音を除いた結果、句の切れ目がずれるため)。
そのまま分けると前のカットに 1 語だけの短いテロップができ、
利用者から見れば「やっぱり前から出ている」になる。

`min_piece_sec` (既定 **0.3**) 未満の先頭片は、その語を**次の片の先頭へ寄せる**。

* テキストは失わない
* 「カット頭とテロップ頭がぴったり」が保たれる
* 寄せる量は 0.3 秒未満なので体感のズレにならない

`0.0` にすれば寄せずにそのまま分ける (厳密派向け)。

### 4-3. `src/modules/subtitle_generator.py` — (c) 開始をカット頭へ揃える

```python
# 各カットの先頭字幕の開始をカット頭へ合わせる (ver8 resolve6 R2)
# cut_bounds: 詰めた軸のカット境界 [(head, tail), ...] (TimeMap.timeline_bounds())
# tolerance_sec: 寄せる許容差。0 以下で無効 (= 従来どおり何もしない)
#
# 触るのは start だけ。end は後段の adjust_display_timing() が作り直すため、
# ここで尺や重なりの面倒を見る必要がない (§3-2)。
#
# 寄せるのは「そのカットで最初に出るテロップ」1 本だけ (taken 集合)。
# 1 カットに 3 本でも 5 本でもテロップが入るのが通常形で、2 本目以降は
# 認識の発話頭のまま残す (頭へ吸わせると 1 本目と同時に出てしまう / §3-3)。
def align_items_to_cut_heads(items, cut_bounds, tolerance_sec):
    if tolerance_sec <= 0 or not cut_bounds or not items:
        return items, 0
    aligned = 0
    out = []
    taken = set()
    for entry in sorted(items, key=lambda e: e["start"]):
        index = _cut_index_at(cut_bounds, entry["start"])
        if index is None:
            out.append(entry)
            continue
        head = cut_bounds[index][0]
        gap = entry["start"] - head
        if index not in taken and _EPS < gap <= tolerance_sec:
            entry = {**entry, "start": head}
            aligned += 1
        taken.add(index)
        out.append(entry)
    return out, aligned
```

* `gap < 0` は起きない (`split_interval` が頭へクランプしているため)。
* `gap > tolerance_sec` は動かさない。長いカットの途中から始まる発話
  (BGM や物音で頭が有音判定されたカット) を頭へ吸わせない安全弁。

### 4-4. `src/modules/subtitle_generator.py` — (d) 終了をカット終端で止める

`adjust_display_timing()` に `cut_bounds=None` を足す。**既定 None = 現行と同一**
なので、旧 (物理カット) 経路は 1 行も挙動が変わらない。

```python
def adjust_display_timing(timeline, max_hold_sec=2.0, min_duration_sec=0.0,
                          cut_bounds=None):
    ...
        # チラつき防止の下限 (現行どおり)
        if min_duration_sec > 0 and (disp_end - disp_start) < min_duration_sec:
            ...

        # カットを跨がせない (ver8 resolve6 R3)
        # カット頭で必ず次のテロップへ切り替わる。disp_start が終端ちょうどの
        # ときだけは潰れるのでクランプしない (下の不正区間判定へ任せる)。
        if cut_bounds is not None:
            tail = _cut_tail_at(cut_bounds, disp_start)
            if tail is not None and tail - disp_start > _EPS:
                disp_end = min(disp_end, tail)
```

### 4-5. `src/modules/subtitle_generator.py` — (e) `recognize_for_timeline()` の組み直し

```python
    # ── カットを跨ぐ認識結果を単語単位で分ける (ver8 resolve6 R1)
    if eff_cfg.get("split_across_cut", True):
        min_len = int(eff_cfg.get("min_line_length", 15))
        max_len = int(eff_cfg.get("max_line_length", 20))
        engine = str(eff_cfg.get("wrap_engine", "budoux")).strip().lower()
        timeline_items, added = split_items_by_keep_segments(
            timeline_items, keep_segments,
            wrap=lambda t: wrap_lines(t, min_len, max_len, engine),
            min_piece_sec=float(eff_cfg.get("split_min_piece_sec", 0.3)))
        _logger.info("カット跨ぎの字幕を分割: %d 件増 (計 %d 件)",
                     added, len(timeline_items))

    # ── 素材時間 → 詰めた軸へ写す (20260812 resolve2.md §4)   ※現行どおり
    timeline_items, dropped = map_items_to_timeline(timeline_items, keep_segments)
    ...

    # ── 開始をカット頭へ揃える (ver8 resolve6 R2)
    cut_bounds = TimeMap.from_segments(keep_segments).timeline_bounds()
    if eff_cfg.get("align_to_cut_head", True):
        timeline_items, n = align_items_to_cut_heads(
            timeline_items, cut_bounds,
            float(eff_cfg.get("align_tolerance_sec", 0.6)))
        _logger.info("字幕の開始をカット頭へ整列: %d 件", n)

    # ── 表示タイミング整形 (カット境界で止める / R3)
    timeline_items = adjust_display_timing(
        timeline_items, max_hold_sec, min_duration_sec,
        cut_bounds=None if eff_cfg.get("hold_across_cut", False) else cut_bounds)
```

ログを 2 行足すのは意図的です。**効いているかどうかが実行ログだけで判る**
ようにしておかないと、素材によって効かないときの切り分けができません。

### 4-6. `src/timeline/timemap.py` — 境界の取り出し (追加のみ)

```python
    # 詰めた軸のクリップ境界 [(head, tail), ...] を昇順で返す (ver8 resolve6 §4-6)
    # 字幕をカット頭へ揃える / カットを跨がせない判定に使う。
    def timeline_bounds(self):
        return [(e.timeline_start, e.timeline_end) for e in self._entries]
```

`split_interval()` / `_merge_adjacent()` には**手を入れません**。
跨ぎは §4-2 で解消済みなので、写像の挙動を変える必要がない
(= 既存 9 件のテストがそのまま効き続ける)。

### 4-7. `src/timeline/commands.py` — `AlignSubtitlesToCutHeads` (R5)

既存プロジェクトと手編集後の救済。**揃え先は `keep_segments` ではなく
ベース映像トラック (V1) のクリップ**にします。手編集で分割・削除した後は
V1 のクリップこそが「いまのカット」であり、`keep_segments` は古いため。

```python
# 字幕の開始を V1 クリップ (カット) の頭へ揃える (ver8 resolve6 R5)
# clip_ids 省略時はすべての字幕トラックの全クリップが対象。
# OP/ED のクリップは揃え先にしない (素材の頭に字幕を吸わせない)。
class AlignSubtitlesToCutHeads(Command):

    def __init__(self, clip_ids=None, tolerance_sec=0.6,
                 clamp_to_cut_end=True, min_clip_sec=0.05):
        ...
        self.label = tr("字幕をカット頭へ揃える")

    def apply(self, timeline):
        bounds = [(c.timeline_start, c.timeline_end)
                  for c in timeline.base_clips()
                  if not c.is_opening_or_ending()]
        if not bounds:
            return False
        changed = False
        for track in timeline.subtitle_tracks():
            if track.locked:
                continue
            prev_end = None                      # 同一トラックの直前クリップ終端
            for clip in sorted(track.clips, key=lambda c: c.timeline_start):
                ...                              # 1 クリップぶんの整列
            track.sort_clips()
        return changed
```

1 クリップぶんの規則:

| # | 規則 |
| --- | --- |
| 1 | 開始を含むカットを探す。無ければ触らない |
| 2 | 頭との差が `tolerance_sec` 以内なら開始を頭へ寄せる。**終了はそのまま** (= 尺が伸びる)。生成時の整列も終了を動かさないため、挙動を揃える。頭に間を残さないのが目的なので尺を保つ意味はない |
| 3 | そのカットの**先頭**クリップ以外は寄せない (1 カット内の 2 本目以降は位置をそのまま / §3-3) |
| 4 | `clamp_to_cut_end` なら終了をカット終端で止める |
| 5 | 直前クリップと重なるなら寄せない (`prev_end` を超えない) |
| 6 | 尺が `min_clip_sec` を割るなら触らない |

Undo はスナップショット方式 (`CommandStack`) なので、**N 件の整列が 1 手で戻ります**。
逆操作を書く必要はありません。

### 4-8. GUI

| ファイル | 変更 |
| --- | --- |
| `src/gui/timeline/timeline_controller.py` | `align_subtitles_to_cut_heads(clip_ids=None)` を足す (`execute()` へ委譲。既存 `edit_subtitles()` と同じ薄いラッパ) |
| `src/gui/timeline/timeline_view.py` | 右クリックメニュー: 字幕クリップ上に「カット頭へ揃える」、字幕トラックの空き領域に「すべての字幕をカット頭へ揃える」 |
| `src/gui/timeline/timeline_editor_dialog.py` | `_register_shortcuts()` へ `bind("align_subtitles", ...)` を 1 行。整列件数を `preview.set_status()` へ出す |
| `src/timeline/builder.py` | `_DEFAULT_SHORTCUTS` へ `"align_subtitles": "Ctrl+Shift+A"` (既存の割り当てと重複なし) |

> 文言はすべて `tr()` 経由 (ver8 resolve §1)。
> 英語対訳を `src/i18n/catalog_en.py` へ追記する。

---

## 5. 設定 (`subtitle` セクション)

`DEFAULT_SETTINGS` (`src/settings/settings_window.py`) と
`src/settings/setting.json` の両方へ足します。`_merge_with_defaults()` が
起動時に補完するため、**更新インストールでも新しいキーが届きます**
(`tests/test_settings_migration.py` の前提)。

| キー | 既定 | 意味 |
| --- | --- | --- |
| `split_across_cut` | `true` | カットを跨ぐ認識結果を単語単位で分ける (R1) |
| `split_min_piece_sec` | `0.3` | 前のカットに残る片がこれ未満なら次のカットへ寄せる (§4-2-1) |
| `align_to_cut_head` | `true` | 各カットの先頭字幕の開始をカット頭へ揃える (R2) |
| `align_tolerance_sec` | `0.6` | 揃える許容差。`0` で無効 |
| `hold_across_cut` | `false` | `true` で従来どおりカットを越えて表示を保持 (R3 を無効化) |

### 5-1. `align_tolerance_sec = 0.6` の根拠

カット頭は「**`cut_min_silence_sec` (既定 0.6 秒) 以上の無音の直後**」と
定義されています。したがって頭から 0.6 秒以上離れた発話頭は、
**別の発話**と見るのが定義に忠実です。`volume_analysis.cut_min_silence_sec`
と同じ値を既定にしておけば、閾値を変えたときの意味も揃います。

### 5-2. UI には出さない

`display_max_hold_sec` / `display_min_duration_sec` と同じ扱いで、
設定画面には項目を出さず `setting.json` で管理します
(既存の「テロップ表示タイミング整形」と同じ系統の値であり、
UI へ出すと調整項目が増えすぎる)。

---

## 6. 変更ファイル

| ファイル | 変更 |
| --- | --- |
| `src/modules/subtitle_generator.py` | `_word_spans()` 追加 / `_recognize()` へ `words` / `split_items_by_keep_segments()` 追加 / `align_items_to_cut_heads()` 追加 / `adjust_display_timing()` へ `cut_bounds` / `recognize_for_timeline()` の組み直し |
| `src/timeline/timemap.py` | `TimeMap.timeline_bounds()` 追加 (追加のみ) |
| `src/timeline/commands.py` | `AlignSubtitlesToCutHeads` 追加 |
| `src/timeline/builder.py` | `_DEFAULT_SHORTCUTS` へ 1 件 |
| `src/gui/timeline/timeline_controller.py` | `align_subtitles_to_cut_heads()` 追加 |
| `src/gui/timeline/timeline_view.py` | 右クリックメニュー 2 項目 |
| `src/gui/timeline/timeline_editor_dialog.py` | ショートカット登録・状態表示 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ 5 キー |
| `src/settings/setting.json` | 同 5 キー |
| `src/i18n/catalog_en.py` | 新規文言の対訳 |
| `tests/test_subtitle_cut_alignment.py` | **新規** (§7-1) |
| `tests/test_timeline_commands.py` | 整列コマンドの試験を追記 (§7-2) |

### 6-1. 変更しないもの (重要)

| | 理由 |
| --- | --- |
| `silence_cutter` (編集点の算出) | カット頭は正しい (§1-1)。閾値もパディングも触らない |
| `map_items_to_timeline()` / `TimeMap.split_interval()` | 跨ぎは前段で解消するため、写像は現行のままで足りる。既存 9 件のテストを守る |
| `builder._append_subtitles()` | `body_offset` の一律加算のみ。整列は詰めた軸で完了している |
| `renderer` / `compositor` / `build_subtitle_file()` | `SubtitleClip.to_item()` をそのまま焼くため、Timeline の見た目 = 出力が保たれる |
| 旧 (物理カット) 経路 `run()` | 実カット後の動画を認識するため跨ぎは原理的に起きない。`cut_bounds` を渡さないので挙動不変 |
| Resolve / FCPXML 出力 | 字幕時刻の作り方を変えないため resolve5 第2部の対策と独立 |

---

## 7. テスト

### 7-1. `tests/test_subtitle_cut_alignment.py` (新規)

`_KEEP = [(0.0, 5.0), (10.0, 15.0)]` (詰めた軸: 2 本目の頭 = 5.0) で統一。

| # | 内容 | 期待 |
| --- | --- | --- |
| 1 | 跨ぎ + 単語あり → 分割 | 2 件。2 件目の開始がカット頭 5.0 と一致 |
| 2 | 跨ぎ + 単語なし | 1 件 (従来どおり / R4) |
| 3 | 跨いでいない | 入力と同一の辞書 (余計に触らない) |
| 4 | 分割したテキストの合計 | 元のテキストから語が失われない |
| 5 | `split_min_piece_sec` | 0.2 秒の先頭片が次の片の先頭へ寄る / `0.0` なら寄らない |
| 6 | 整列: 頭との差 0.2s (許容差内) | 開始がカット頭と一致 |
| 7 | 整列: 頭との差 1.5s (許容差外) | 動かさない |
| 8 | 整列: 同じカットの 2 本目 | 動かさない |
| 9 | 整列: `tolerance = 0` | 入力と同一 |
| 10 | 境界クランプ | 終了がカット終端を越えない |
| 11 | `cut_bounds=None` | `adjust_display_timing()` が現行と完全に同一 (回帰) |
| 12 | 通し (`recognize_for_timeline` 相当の順序) | すべてのテロップが 1 カット内に収まり、各カットの先頭テロップの開始が頭と一致 |
| 13 | **1 カット内に 3 件** (§3-3) | **件数が 3 件のまま変わらない**。先頭のみ開始がカット頭、2・3 件目の開始は入力と同一 |
| 14 | 同 #13 の 3 件目 | 終了がカット終端を越えない。2 件目の終了は 3 件目の開始と一致 (現行 R3 のまま) |
| 15 | 分割 → 写像の通し | 後ろの片の開始がカット頭と一致する (§2-1 の症状が消えること) |
| 16 | 組み直した本文への整形 | `wrap` (フィラー除去＋改行) が分割後の本文へ掛かる |
| 17 | すでに頭に乗っている字幕 | 件数に数えない (変化なし) |
| 18 | カットの外の時刻 | 触らない |

実装: 20 件。

### 7-2. `tests/test_timeline_commands.py` へ追記

| # | 内容 | 期待 |
| --- | --- | --- |
| 1 | 許容差内の字幕 | V1 クリップの頭と一致 |
| 2 | 許容差外 | 動かさない |
| 3 | 直前の字幕と重なる位置 | 動かさない (重なりを作らない) |
| 4 | OP クリップ | 揃え先にしない |
| 5 | ロック済みトラック | 触らない |
| 6 | Undo | N 件が 1 手で元へ戻る |
| 7 | 揃うものが無い | `execute()` が `False` (履歴に積まない) |
| 8 | 1 カット内に 3 件 | 件数が変わらず、先頭 1 件だけが揃う (§3-3) |
| 9 | 終端クランプ | 終了がカット終端で止まる |
| 10 | `clamp_to_cut_end=False` | 終端で止めない (旧挙動) |
| 11 | `clip_ids` 指定 | 指定した字幕だけが対象になる |
| 12 | 許容差 0 | 何もしない |

実装: 11 件 (#1〜#12 のうち #8 は #3 と同じ試験で担保)。

### 7-3. 既存テスト

`tests/test_subtitle_timeline_map.py` は**そのまま通ります** (写像を変えないため)。
`test_across_cut_is_merged_into_one` には
「単語が取れないときの従来動作 (R4 のフォールバック)」である旨を
コメントで追記し、新しい分割テスト (§7-1 #1) との役割の違いを明示します。

実行: `python -m unittest discover -s tests` → **1361 件 OK** (従来 1330 + 新規 31)

---

## 8. 確認手順

1. 発話の間が 0.6 秒以上ある素材 (雑談配信など) で Timeline 経路を実行する。
2. 実行ログに次の 2 行が出ることを確認する。
   ```
   カット跨ぎの字幕を分割: N 件増 (計 M 件)
   字幕の開始をカット頭へ整列: K 件
   ```
   `N = 0` のままなら `word_timestamps` が効いていない (§9-2)。
3. Timeline 画面で、**V1 クリップの境界と S1 字幕クリップの左端が一致**して
   いることを目視する (ズームを上げると 1 フレーム単位で見える)。
4. 字幕クリップが **V1 クリップの境界を越えていない**ことを確認する。
5. プレビューを再生し、カットの切り替わりでテロップも同時に切り替わることを確認する。
6. 手で V1 を分割・リップル削除したあと、`Ctrl+Shift+A` (または右クリック →
   「すべての字幕をカット頭へ揃える」) で揃うこと、`Ctrl+Z` で 1 手で戻ることを確認する。
7. `subtitle.split_across_cut = false` / `align_to_cut_head = false` /
   `hold_across_cut = true` にすると**従来の見た目に戻る**ことを確認する
   (旧挙動への退路)。
8. 旧 (物理カット) 経路 (`silence_cut.mode = "physical"`) で出力し、
   テロップが従来と変わらないことを確認する。

---

## 9. 残件・確認事項

### 9-1. `origin.source_start` の意味が変わる

`builder._append_subtitles()` は `TimeMap.to_source()` で元動画時刻を逆算し
`origin.source_start` へ残しています (将来の再同期用)。
整列後はこれが「カット頭の素材時刻」になり、**認識が返した発話頭ではなくなります**。
記録用途のみで現状どこからも読まれていないため今回は触りませんが、
再同期機能を作るときは `origin` へ認識時刻を別キーで残す必要があります。

### 9-2. 単語タイムスタンプが取れない場合

`word_timestamps = false`、またはモデルが単語を返さない構成では
分割が働かず**従来どおり**になります (R4)。その場合は §2-1 のズレが残るため、
`align_to_cut_head` による整列だけが効きます (跨ぎは直りません)。
確認手順 2 でログを見るのはこの切り分けのためです。

### 9-3. 1 カット内のテロップの本数は変えない (仕様の確認)

§3-3 のとおり、1 カットに 3 本でも 5 本でもテロップが入ります。
R1 の分割が本数を増やすのは「カットを跨いだセグメント」だけで、
カット内の区切りには触りません。2 本目以降の開始は認識の発話頭のままです。

したがって本設計で**テロップがまとまる・減る・間引かれることはありません**。
実素材でカット内の本数が変わったら、それは `_keep_spans_of()` の判定が
誤ってカット内で分割を発火させている不具合として扱います
(§7-1 #3 / #13 / #14 がこれを守る)。

逆に「カット内でもテロップの切り替えをもっと詰めたい / 間を縮めたい」は
`display_max_hold_sec` / `display_min_duration_sec` の領域で、本書の対象外です。

### 9-4. `split_min_piece_sec` の既定値

0.3 秒は「体感でズレと判らない上限」として置いた値で、実素材での
確認が必要です。前のカットに短いテロップが出てしまう場合は上げ、
語が次のカットへ寄りすぎると感じる場合は下げる (`0.0` で寄せない)。

### 9-5. 縦動画 (`vertical`) 設定との関係

分割後の改行は `build_effective_subtitle_cfg()` 適用済みの
`eff_cfg` の `min_line_length` / `max_line_length` / `wrap_engine` で
やり直します。縦動画では縦用の文字数 (10〜15) が使われ、request14 の
方針と矛盾しません。

---

# 第3部 — 修正設計 (追補 / request6.md 「(追加)」への対応)

追記日: 2026-10-20 (第1部・第2部の実装を適用した状態からの差分設計)

第2部の設計は「カットの頭からテロップが始まる」ことには成功したが、
**テロップの単位をカットに従わせた**ため、文脈が続いているテロップが
細かく裁断された。第3部はこの単位の決め方だけを作り直す。
第1部 (現状分析) と、整列 (R2) ・右クリックメニュー (R5) は引き続き有効。

---

## 10. 追加要望と結論

> docs/request/ver8/resolve6.md をもとに実装してもらいましたがうまくいきませんでした。
> - クリップの先頭からテロップが始まっていたのでこれは OK
> - しかしテロップが細かく裁断され、文脈が続いているのに切られている個所が多い。
> - 上記の理由はクリップが切られていて、必ずテロップを 1 つ作成しようとしているからだと思われる。
> - 1 つのクリップに対して複数個のテロップが付く場合もあれば、1 つのテロップに複数個のクリップが付く場合もある。
> - そしてそのテロップの限界点は 1 行下限文字数と 1 行最大文字数に基づく。

### 10.1 結論 (原因)

ご指摘のとおり、**テロップの単位をカットに従わせたことが原因**です。

第2部の R1 (`split_items_by_keep_segments()`) は「カットを跨いだ認識セグメントを
カットごとに分ける」規則で、跨いだカットの数だけテロップが生まれます。
無音 0.6 秒で切る `keep_segments` は 1 文の中の息継ぎでも切れるため、
1 文が 3〜5 カットに跨るのは普通であり、そのまま 3〜5 本のテロップになります。

### 10.2 原因の内訳 (3 つが重なっている)

| # | 原因 | 場所 | 効果 |
| --- | --- | --- | --- |
| F | カットを跨いだセグメントを**必ず**カットごとに分ける (1 カット = 最低 1 本) | `subtitle_generator.split_items_by_keep_segments()` | 1 文が跨いだカット数だけのテロップへ裁断される (**本命**) |
| G | 表示終了を「**開始**を含むカットの終端」で打ち切る | `adjust_display_timing()` の `cut_bounds` クランプ | 跨ぎを許しても最初のカットの終端で消える |
| H | 整列に「直前のテロップがそのカット頭を覆っているか」の判定が無い | `align_items_to_cut_heads()` | 跨ぎを許すと次カット頭へ吸った字幕が前の字幕と重なり、R3 (次の開始まで表示) が前の字幕を打ち切る |

- **原因F の補足**: `split_min_piece_sec = 0.3` は「前のカットに残る片が 0.3 秒未満なら
  その語を次のカットの本文へ寄せる」緩和にすぎません。「ちょっと」のような
  1 秒前後の片は条件を満たさず、そのまま 1 本のテロップとして残ります。
- **原因G・H の補足**: F だけを外して「跨いだまま 1 本」にしても、G が最初のカットの
  終端で表示を切り、H が重なりを作って R3 が前を切ります。**3 つを同時に直す必要があります**。

### 10.3 結論 (直し方)

**テロップの単位は文字数で決める。カット境界は「切ってよい候補」へ格下げする。**

- 1 本のテロップの文字数は **下限 = `subtitle.min_line_length`**、
  **上限 = `subtitle.max_line_length` × `subtitle.subtitle_max_lines`** の範囲。
- カット境界に来ても、**下限に達していなければ切らずにカットを跨いで繋ぐ**
  (→ 1 つのテロップに複数のクリップが付く)。
- 下限に達していれば**カット境界で優先的に切る**
  (→ カット頭からテロップが始まる = 第2部の成果を保つ)。
- 上限を超えるときはカットの途中でも切る
  (→ 1 つのクリップに複数のテロップが付く)。
- 開始のカット頭への吸着 (R2) は、**そのカット頭が直前のテロップで覆われていないときだけ**行う。
- 表示終了のクランプ先は「**発話が終わったカット**の終端」へ変える (跨ぎを打ち切らない)。

### 10.4 新旧の規則

| 規則 | 第2部 (現状) | 第3部 (本設計) |
| --- | --- | --- |
| テロップの単位 | カット (跨いだら必ず分ける) | **文字数** (下限〜上限)。カット境界は切る候補 |
| 1 クリップ : テロップ | 常に 1 : 1 以上 | 1 : N (従来どおり複数本になる) |
| 1 テロップ : クリップ | 常に 1 : 1 | **1 : N (跨ぎを許す)** |
| 開始の整列 (R2) | 各カットの先頭字幕を吸着 | 同じ。ただし**カット頭が覆われていないとき**だけ |
| 表示終了 (R3) | 開始を含むカットの終端で打ち切り | **発話終了を含むカット**の終端で打ち切り |
| 認識セグメント境界 | テロップ境界として必ず残る | 下限文字数で判断 (短すぎる断片は次と繋ぐ / B5) |

### 10.5 実装状況 (2026-10-20 時点)

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | 文字数で単位を作る `regroup_items_by_length()` (B1〜B5) | 完了 |
| 2 | 単語 → カット番号の走査 `_cut_index_of_word()` | 完了 |
| 3 | `align_items_to_cut_heads()` に被り判定 (原因H) | 完了 |
| 4 | `adjust_display_timing()` のクランプ基準変更 (原因G) | 完了 |
| 5 | `recognize_for_timeline()` の `subtitle_unit()` 分岐 | 完了 |
| 6 | `AlignSubtitlesToCutHeads` の終端クランプ既定変更 | 完了 |
| 7 | 設定キー 4 件の追加 | 完了 |
| 8 | テスト (新規 `tests/test_subtitle_length_units.py` / 既存 2 ファイル追補) | 完了 (1384 件 OK) |

---

## 11. 新しいテロップ単位の定義 (R1-dash)

### 11-1. 文字数の下限と上限

| 値 | 由来 | 既定 | 意味 |
| --- | --- | --- | --- |
| 下限 `min_chars` | `subtitle.min_line_length` | 5 | これ未満の間は**切らない** (文末でもカット境界でも繋ぐ) |
| 上限 `max_chars` | `subtitle.max_line_length` × `subtitle.subtitle_max_lines` | 20 × 2 = 40 | これを超えるときは**必ず切る** |

文字数は改行記号 (`\N`) を含まない本文の長さで数えます。
`min_chars > max_chars` になる設定は `min_chars = max_chars` へ丸めます (設定事故の保険)。

> 現在の `setting.json` は `min_line_length = 5` / `max_line_length = 20`。
> **裁断がまだ多いと感じる場合は `min_line_length` を上げる**のが一次の調整です
> (下限が上がるほどカットを跨いで繋ぐ割合が増える)。§13-2 参照。

### 11-2. 区切りの判定 (優先順)

単語列を時間順に 1 語ずつ積み、次の語を積む前に判定します。

| 記号 | 条件 | 動作 |
| --- | --- | --- |
| B1 | 積んだ本文 + 次の語 が **上限超過** | **切る** (下限未達でも切る。上限は絶対) |
| B2 | **下限達成済み** かつ 次の語が**別のカット**に属する | **切る** (= カット頭から次のテロップが始まる) |
| B3 | **下限達成済み** かつ 直前の語が**文末記号**で終わる (`。．！？!?`) | 切る (`break_on_sentence_end = true` のとき) |
| B4 | 次の語との間で**取り除かれた無音が長い** (`merge_max_cut_gap_sec` 超) | 切る (下限未達でも切る。文脈が切れている) |
| B5 | **下限達成済み** かつ 次の語が**別の認識セグメント**の先頭 | 切る (別の文を 1 本へ混ぜない) |
| — | 上記以外 | **積む** (カット境界でも跨いで繋ぐ) |

認識セグメントの境界も「下限に達していれば切ってよい候補」の 1 つです (B5)。
下限未満の断片は次と繋ぐため「はい。」のような 3 文字が独立したテロップになることはなく、
逆に下限を満たしていれば別の文を 1 本へ混ぜません
(B5 が無いと、同じカットの中で別々の認識セグメントが上限文字数まで繋がり、
7 秒・2 文のテロップができてしまう。実装時の実測で判明 / §17-1)。

### 11-3. 図で見る

```
■ ケース1: 1 文が 3 カットに跨る (= 今回の不具合)

素材時間  ─────────────────────────────────────────────────────────▶
認識      「今日はですね ちょっと 話したいことがあって」 (1 セグメント / 21 文字)
keep      [== C1 ==]  …無音…  [= C2 =]  …無音…  [==== C3 ====]
単語       今日は ですね          ちょっと          話したいことがあって

第2部 ×   [T1:今日はですね][T2:ちょっと][T3:話したいことがあって]   ← 3 本に裁断
第3部 ○   [T1:今日はですねちょっと話したいことがあって            ]   ← 1 本 (21 ≦ 40)
           ↑ 開始は C1 の頭へ整列 (R2)      表示終了は C3 の終端まで (R3-dash)


■ ケース2: 1 カットに複数のテロップ (従来形。本設計でも変わらない)

詰めた軸  [=============== C1 (12 秒) ===============]
          [T1: 40 文字][T2: 40 文字][T3: 22 文字]
           ↑C1 の頭へ整列   ↑上限で切る   ↑上限で切る


■ ケース3: 混在 (通常はこうなる)

詰めた軸  [==== C1 ====][== C2 ==][======= C3 =======]
          [T1 (C1 内) ][T2 (C1→C2 跨ぎ)  ][T3 (C3) ]
           ↑C1 頭へ整列                    ↑C3 頭へ整列
                        ↑ C2 の頭は T2 が覆っている → C2 では整列しない (原因H の対策)
```

### 11-4. 擬似コード

```python
# テロップの単位を文字数で作り直す (ver8 resolve6 第3部 R1-dash)
#
# 入力・出力はどちらも素材時間。認識セグメントの境界は引き継がず、
# 単語列を下限〜上限文字数の範囲でまとめ直す (§11-2 の B1〜B4)。
# カットを跨ぐテロップが生まれるのは意図どおりで、跨ぎは後段の
# map_items_to_timeline() が詰めた軸の 1 区間へまとめる。
#
# wrap           : 本文の整形関数 (フィラー除去 + 改行)。単語から本文を組み直すため必要
# min_chars      : 下限文字数 (subtitle.min_line_length)
# max_chars      : 上限文字数 (max_line_length × subtitle_max_lines)
# max_cut_gap_sec: 取り除かれた無音がこれを超えたら文脈が切れたとみなす (0 で無制限)
#
# 戻り値: (items, テロップ件数)
def regroup_items_by_length(items, keep_segments, wrap=None,
                            min_chars=5, max_chars=40,
                            break_on_sentence_end=True,
                            max_cut_gap_sec=0.0):
    out = []
    buf = []        # 現在のテロップへ積んだ単語
    buf_len = 0     # 積んだ本文の文字数 (改行記号は数えない)
    last_cut = None # 直前語が属したカット番号
    last_end = None # 直前語の終了 (素材時間)
    cursor = 0      # keep_segments の走査位置 (単語もカットも時間順のため前進のみ)

    def flush():
        nonlocal buf, buf_len
        if buf:
            out.append(_item_from_words(buf, wrap))  # start/end は単語の実時間
        buf, buf_len = [], 0

    for item in (items or []):
        words = item.get("words")
        if not words:
            # 単語が取れない = 他と混ぜられない → そのまま 1 本で通す (R4 / 安全側)
            flush()
            out.append(item)
            last_cut = last_end = None
            continue
        boundary = True   # この認識セグメントの最初の語か (B5)
        for word in words:
            cut, cursor = _cut_index_of_word(word, keep_segments, cursor)
            if cut is None:
                continue  # カットされた無音の中の語は載せない (従来どおり)
            text = str(word.get("text", "")).strip()
            if not text:
                continue
            if buf:
                enough = buf_len >= min_chars
                crossed = (cut != last_cut)
                gap = float(word["start"]) - last_end if last_end is not None else 0.0
                over = buf_len + len(text) > max_chars
                far = max_cut_gap_sec > 0 and gap > max_cut_gap_sec
                ended = break_on_sentence_end and _ends_sentence(buf[-1])
                if over or far or (enough and (crossed or ended or boundary)):
                    flush()
            if not buf:
                buf_item = item          # 付随キーを引き継ぐ元セグメント
            buf.append(word)
            buf_len += len(text)
            boundary = False
            last_cut, last_end = cut, float(word["end"])
    flush()
    return out, len(out)
```

補助関数:

```python
# 単語が属するカット番号を返す (カットされた無音の中なら None)
# 単語列・keep_segments はどちらも時間順のため、走査位置 cursor を前進させるだけで済む。
# 中点が無音へ落ちた単語は重なりが最大のカットへ寄せる
# (文字を落とさないため。規則は第2部 §4-2 の _span_index_for_word() と同じ)
def _cut_index_of_word(word, keep_segments, cursor=0): ...

# 単語列から 1 テロップを作る (start/end は単語の実時間。本文は wrap で組み直す)
def _item_from_words(words, wrap): ...

# 単語の末尾が文末記号かどうか
_SENTENCE_END = ("。", "．", "！", "？", "!", "?")
def _ends_sentence(word): ...
```

### 11-5. 処理順序 (第2部 §3-2 からの差分)

```
音声認識 (素材時間 / 単語つき)
  ↓
[変更] テロップ単位の作り直し  regroup_items_by_length()   ← 素材時間のうちに行う
  ↓
素材時間 → 詰めた軸          map_items_to_timeline()      ← 跨ぎは 1 区間へまとまる (意図どおり)
  ↓
[変更] 開始をカット頭へ整列    align_items_to_cut_heads()   ← 被り判定を追加 (原因H)
  ↓
[変更] 表示タイミング整形      adjust_display_timing()      ← クランプ基準を発話終了側へ (原因G)
```

順序そのものは変えません。**単位を作る工程を写像より前に置く**点も同じです
(でないとカット境界と単語を突き合わせられない)。

---

## 12. 変更点

### 12-1. `src/modules/subtitle_generator.py` — (a) 単位の作り直しを追加

`regroup_items_by_length()` / `_cut_index_of_word()` / `_item_from_words()` /
`_ends_sentence()` を新規追加する (§11-4)。

`split_items_by_keep_segments()` と `_words_by_span()` / `_keep_spans_of()` /
`_span_index_for_word()` は**消さない**。`subtitle_unit = "cut"` (第2部の挙動) の
退路として残す (§13-1)。`_span_index_for_word()` は `_cut_index_of_word()` の
フォールバック (重なり最大のカットへ寄せる) でそのまま使う。

### 12-2. `src/modules/subtitle_generator.py` — (b) 整列に被り判定 (原因H)

```python
# 各カットの先頭字幕の開始をカット頭へ合わせる (R2 / 第3部で被り判定を追加)
#
# 跨ぎを許したことで「カット頭が直前のテロップに覆われている」状態が起きる。
# そこへ次のテロップを吸わせると重なり、adjust_display_timing() の R3
# (次の開始まで表示) が直前のテロップを境界で打ち切ってしまう (原因H)。
# そのため prev_end を見て、覆われているカット頭へは吸わせない。
def align_items_to_cut_heads(items, cut_bounds, tolerance_sec):
    ...
    prev_end = None
    for entry in sorted(items, key=lambda e: e["start"]):
        index = _cut_index_at(cut_bounds, entry["start"])
        ...
        head = cut_bounds[index][0]
        gap = float(entry["start"]) - head
        # 直前のテロップがカット頭を覆っているなら動かさない (重なりを作らない)
        covered = prev_end is not None and head < prev_end - _EPS
        if index not in taken and not covered and _EPS < gap <= tolerance_sec:
            entry = {**entry, "start": head}
            aligned += 1
        taken.add(index)
        prev_end = max(prev_end or 0.0, float(entry["end"]))
        out.append(entry)
```

`commands.AlignSubtitlesToCutHeads._align_one()` には既に同等の `prev_end`
判定が入っている (`src/timeline/commands.py:1477`)。生成側をそれに合わせる形。

### 12-3. `src/modules/subtitle_generator.py` — (c) クランプ基準の変更 (原因G)

```python
        # カットを跨がせない → 「発話が終わったカットの終端」で止める (第3部 R3-dash)
        # 第2部は開始を含むカットの終端で切っていたため、カットを跨ぐテロップが
        # 最初のカットの終端で消えていた (原因G)。基準を発話終了側へ移し、
        # 跨ぎは許したまま「次のカットへ余分に残り続ける」ことだけを防ぐ。
        if cut_bounds is not None:
            probe = max(float(speech_end) - _EPS, float(disp_start))
            tail = _cut_tail_at(cut_bounds, probe) or _cut_tail_at(cut_bounds, disp_start)
            if tail is not None and tail - disp_start > _EPS:
                disp_end = min(disp_end, tail)
```

これでも次のカットの頭は汚れません。

- 次のテロップが次カットの頭へ整列している → R3 で `disp_end = next_start` = カット頭。
- 次のテロップが `max_hold` より遠い → 発話が終わったカットの終端で止まる。
- 跨ぐテロップがカット頭を覆う場合 → そのカットから始まるテロップは無い (§11-3 ケース3)。

### 12-4. `src/modules/subtitle_generator.py` — (d) `recognize_for_timeline()` の分岐

```python
    # ── テロップの単位を決める (ver8 resolve6 第3部 R1-dash / 第2部 R1)
    # 写像より前 = 素材時間のうちに行う。でないとカット境界と単語を突き合わせられない。
    unit = subtitle_unit(eff_cfg)           # "length" / "cut" / "segment"
    if unit != _SUBTITLE_UNIT_SEGMENT:
        min_len = int(eff_cfg.get("min_line_length", 15))
        max_len = int(eff_cfg.get("max_line_length", 20))
        ...
        def _refine(text):                   # 認識時と同じ整形 (フィラー除去 + 改行)
            ...
        if unit == "length":
            max_chars = max(max_len * int(eff_cfg.get("subtitle_max_lines", 2)), 1)
            timeline_items, count = regroup_items_by_length(
                timeline_items, keep_segments, wrap=_refine,
                min_chars=min(min_len, max_chars), max_chars=max_chars,
                break_on_sentence_end=bool(eff_cfg.get("break_on_sentence_end", True)),
                max_cut_gap_sec=float(eff_cfg.get("merge_max_cut_gap_sec", 0.0)))
            _logger.info("テロップ単位を文字数で再構成: %d 件 (下限 %d / 上限 %d 文字)",
                         count, min(min_len, max_chars), max_chars)
        else:
            timeline_items, added = split_items_by_keep_segments(...)   # 第2部 R1 (旧挙動)
```

`subtitle_unit()` は後方互換を含む解決関数 (GUI からも使うため公開名)。

```python
# テロップ単位の決め方を解決する (後方互換つき)
#   subtitle_unit があればそれに従う ("length" / "cut" / "segment")
#   無い (旧 setting.json) → split_across_cut が false なら "segment"、true なら "length"
def subtitle_unit(subtitle_cfg): ...
```

### 12-5. `src/timeline/commands.py` — 終端クランプの既定を変える

`AlignSubtitlesToCutHeads.__init__` の `clamp_to_cut_end` 既定を
**`True` → `False`** へ変更する。手編集後の字幕クリップには「発話終了」の情報が無く、
クリップの終端をカット境界で切ると跨ぎテロップを壊すため
(`src/timeline/commands.py:1479`)。引数自体は残し、`"cut"` モード用の退路にする。

### 12-6. `src/gui/timeline/timeline_controller.py` — 引数の決め方

```python
        subtitle_cfg = self._settings.get("subtitle", {})
        command = commands.AlignSubtitlesToCutHeads(
            clip_ids,
            tolerance_sec=float(subtitle_cfg.get("align_tolerance_sec", 0.6)),
            # 跨ぎを許す単位 ("length") では終端を切らない (跨ぎテロップを壊すため)
            clamp_to_cut_end=(subtitle_generator.subtitle_unit(subtitle_cfg) == "cut"
                              and not bool(subtitle_cfg.get("hold_across_cut", False))),
            min_clip_sec=self._cfg["min_clip_sec"],
        )
```

規則を 2 箇所へ書かないため、判定は `subtitle_generator.subtitle_unit()` 1 箇所に置く。
GUI 層からの参照はメソッド内の遅延 import にする
(`proxy_manager` / `blur_spec_dialog` と同じ既存の作法。起動時の重い import を増やさない)。

### 12-7. 変更ファイル

| ファイル | 変更 |
| --- | --- |
| `src/modules/subtitle_generator.py` | `subtitle_unit()` / `regroup_items_by_length()` / `_cut_index_of_word()` / `_item_from_words()` / `_ends_sentence()` 追加、`align_items_to_cut_heads()` に被り判定、`adjust_display_timing()` のクランプ基準変更、`recognize_for_timeline()` の分岐 |
| `src/timeline/commands.py` | `AlignSubtitlesToCutHeads` の `clamp_to_cut_end` 既定を `False` |
| `src/gui/timeline/timeline_controller.py` | クランプ引数の決め方 (§12-6) |
| `src/settings/setting.json` | `subtitle_unit` / `subtitle_max_lines` / `break_on_sentence_end` / `merge_max_cut_gap_sec` 追加 |
| `src/settings/settings_window.py` | 同 4 キーを `DEFAULT_SETTINGS` へ (UI には出さない) |
| `tests/test_subtitle_length_units.py` | 新規 (§14-1) |
| `tests/test_subtitle_cut_alignment.py` | 仕様が変わる箇所を改訂 (§14-2) |
| `tests/test_timeline_commands.py` | クランプ既定の変更に追随 (§14-3) |

### 12-8. 変更しないもの (重要)

- `map_items_to_timeline()` / `TimeMap` / `timeline_bounds()` — 跨ぎを 1 区間へまとめる
  挙動が**本設計では正しい動作**になるため、手を入れない。
- `split_items_by_keep_segments()` とその補助 — `"cut"` モードの退路として残す。
- `builder._append_subtitles()` — 時刻をそのまま載せるだけで、クリップ境界を見ていない。
  カットを跨ぐ字幕クリップもそのまま 1 クリップで載る (DaVinci Resolve 出力も同じ)。
- 右クリックメニュー・`Ctrl+Shift+A`・`catalog_en.py` — 文言も操作も変えない。
- 焼き込み経路 (`run()` / `build_subtitle_file()`) と物理カット経路。
- 無音カットの判定 (`volume_analysis` / `silence_cutter`) — カットの位置は一切変えない。

---

## 13. 設定 (`subtitle` セクション)

### 13-1. 追加キー

| キー | 既定 | 意味 |
| --- | --- | --- |
| `subtitle_unit` | `"length"` | テロップ単位の決め方。`"length"` = 文字数 (本設計) / `"cut"` = カットごとに必ず 1 本 (第2部) / `"segment"` = 認識セグメントそのまま (ver8 以前) |
| `subtitle_max_lines` | `2` | 1 本のテロップの最大行数。上限文字数 = `max_line_length` × この値 |
| `break_on_sentence_end` | `true` | 下限達成後、文末記号 (`。．！？!?`) で区切る |
| `merge_max_cut_gap_sec` | `0.0` | 取り除かれた無音がこれを超えたら下限未達でも区切る。`0` で無制限 |

### 13-2. 既存キーの扱い

| キー | 現状値 | 第3部での意味 |
| --- | --- | --- |
| `min_line_length` | 5 | **テロップの下限文字数**を兼ねる。上げると跨ぎが増え、裁断が減る (一次の調整つまみ) |
| `max_line_length` | 20 | 1 行の上限。× `subtitle_max_lines` がテロップの上限文字数 |
| `split_across_cut` | `true` | `subtitle_unit` 未設定時の後方互換にのみ使う (`false` → `"segment"`) |
| `split_min_piece_sec` | 0.3 | `"cut"` モードでのみ有効 (`"length"` では使わない) |
| `align_to_cut_head` / `align_tolerance_sec` | `true` / 0.6 | 変更なし (R2) |
| `hold_across_cut` | `false` | `true` でカット終端のクランプを無効化 (R3-dash を無効化)。意味は変えない |
| `word_timestamps` | `true` | **必須**。`false` では単位を作り直せず `"segment"` 相当になる (§16-2) |
| `display_max_hold_sec` / `display_min_duration_sec` | 2.0 / 0.5 | 変更なし |

UI には出さない (表示タイミング整形と同系統のため `setting.json` で管理 / 第2部 §5-2 と同じ方針)。

### 13-3. `subtitle_max_lines = 2` の根拠

- 上限 40 文字 = 20 文字 × 2 行。`min_line_length = 5` / `max_line_length = 20` の
  折り返し設定で素直に 2 行へ収まる最大値。
- 1 行 (20 文字) まで下げると長い文が時間方向へ細かく割れ、
  今回の指摘 (裁断) を別の形で再発させる危険がある。
- 「1 本 = 必ず 1 行」にしたい場合は `1` を設定する (`max_chars = 20`)。

---

## 14. テスト

### 14-1. `tests/test_subtitle_length_units.py` (新規)

`regroup_items_by_length()` を素材時間の入力で直接検証する (認識は呼ばない)。

| # | 名前 | 検証 |
| --- | --- | --- |
| 1 | `test_short_pieces_across_cuts_become_one` | 下限未満の片はカットを跨いで 1 本になる (**今回の不具合の回帰テスト**) |
| 2 | `test_break_happens_at_the_cut_when_enough_chars` | 下限達成後はカット境界で切れる |
| 3 | `test_over_max_breaks_inside_one_cut` | 上限超過はカットの途中でも切れる (1 クリップに複数本) |
| 4 | `test_max_wins_over_min` | 下限未達でも上限超過なら切る |
| 5 | `test_sentence_end_breaks_after_min` | 下限達成後は文末記号で切れる |
| 6 | `test_sentence_end_under_min_is_kept` | 下限未達の文末では切らない |
| 7 | `test_sentence_break_can_be_disabled` | `break_on_sentence_end = false` |
| 8 | `test_long_removed_gap_breaks` | `merge_max_cut_gap_sec` 超で下限未達でも切る |
| 9 | `test_zero_gap_limit_is_unlimited` | `0` で無制限 |
| 10 | `test_items_without_words_pass_through` | 単語が無い item はそのまま 1 本 (R4) |
| 11 | `test_words_in_removed_silence_are_dropped` | 無音へ落ちた語は載せない |
| 12 | `test_text_is_not_lost` | 入力の文字が (無音の語を除いて) 全部出力に現れる |
| 13 | `test_wrap_is_applied_to_the_rebuilt_text` | 組み直した本文へ改行が掛かる |
| 14 | `test_times_come_from_words` | `start` / `end` が単語の実時間になる |
| 15 | `test_segment_boundary_breaks_after_min` | 下限達成後は認識セグメント境界で切れる (B5) |
| 16 | `test_segment_boundary_alone_does_not_break` | 下限未達なら認識セグメント境界では切れない |
| 17 | `PipelineOrderTest.test_pipeline_order_keeps_cut_head_and_crossing` | 再構成 → 写像 → 整列 → 整形 を通し、(a) カット頭と一致 (b) 跨ぎが残る (c) 本数が増えすぎない |
| 18 | `SubtitleUnitTest` (3 件) | `subtitle_unit()` の解決 (明示値 / 不正値 / 旧 setting.json) |

### 14-2. `tests/test_subtitle_cut_alignment.py` (改訂)

| クラス / テスト | 対応 |
| --- | --- |
| `SplitByKeepSegmentsTest` (8 件) | `"cut"` モードの仕様として**そのまま残す** (関数を残すため期待値も不変) |
| `AlignToCutHeadTest` | 既存 7 件は不変。`test_covered_cut_head_is_not_taken` を追加 (原因H) |
| `DisplayTimingWithCutBoundsTest` (既存 4 件) | **期待値の見直しは不要だった**。発話終了が開始と同じカットに入る既存ケースでは、新旧でクランプ先が一致する |
| `DisplayTimingWithCutBoundsTest` | `test_crossing_item_is_not_cut_at_the_first_cut_end` (跨ぎを打ち切らない) と `test_crossing_item_stops_at_the_speech_cut_end` (発話が終わったカットの終端は越えない) を追加 (原因G) |
| `EndToEndOrderTest.test_pipeline_order` | `"cut"` モードの通し確認として残す |

### 14-3. `tests/test_timeline_commands.py` (改訂)

`AlignSubtitlesTest.test_end_is_clamped_at_the_cut_end` は既定が `False` に
なるため `clamp_to_cut_end=True` を明示して検証する。
`test_clamp_can_be_disabled` は既定値の確認へ読み替え、
`test_end_is_not_clamped_by_default` (引数を省略 → 終端で切らない) へ改名する。

### 14-4. 実行

```
python -m unittest tests.test_subtitle_length_units tests.test_subtitle_cut_alignment
python -m unittest tests.test_timeline_commands tests.test_subtitle_timeline_map
python -m unittest discover -s tests      # 全体 (2026-10-20 実装時点で 1384 件 OK)
```

---

## 15. 確認手順

1. `setting.json` の `subtitle.subtitle_unit = "length"` (既定) で編集点モードを実行する。
2. ログで単位が作り直されていることを確認する。
   ```
   テロップ単位を文字数で再構成: N 件 (下限 5 / 上限 40 文字)
   字幕時刻を Timeline の軸へ写像: N → M 件 (...)
   字幕の開始をカット頭へ整列: K 件
   ```
3. Timeline 画面で以下を目視する。
   - V1 クリップの境界と、そこから始まる S1 字幕クリップの左端が一致している。
   - **文脈が続くテロップが V1 の境界を越えて 1 本で載っている** (= 今回の要望)。
   - 長い発話のカットでは 1 クリップに複数のテロップが載っている。
4. プレビューを再生し、(a) カットの切り替わりでテロップも切り替わる、
   (b) 跨ぎテロップがカット境界で消えない、を確認する。
5. `min_line_length` を 5 → 12 へ上げて再実行し、跨ぎが増えて本数が減ることを確認する
   (裁断の調整つまみが効くこと)。
6. `subtitle_unit = "cut"` で第2部の挙動へ、`"segment"` で ver8 以前の挙動へ戻ることを確認する。
7. 手編集 (分割・リップル削除) のあと `Ctrl+Shift+A` で揃うこと、`Ctrl+Z` で 1 手で戻ること、
   **跨ぎテロップが短縮されない**ことを確認する (§12-5)。
8. 旧 (物理カット) 経路 `silence_cut.mode = "physical"` でテロップが従来と変わらないことを確認する。

---

## 16. 残件・確認事項

### 16-1. 文字数の既定値は実素材で詰める

`min_line_length = 5` は現状値のまま下限として使う。実素材で
「まだ細かい」と感じたら上げる (10〜12 が次の候補)。
`subtitle_max_lines = 2` も同様に、長文の割れ方を見て 1 / 3 を試す。
**この 2 値だけで裁断の粒度が決まる**ようにしたのが本設計の主眼。

### 16-2. 単語タイムスタンプが取れない場合

`word_timestamps = false`、またはモデルが単語を返さない構成では
単位を作り直せず、認識セグメントのまま (ver8 以前) になる。
その場合は第1部 §2-1 のズレが残り、整列 (R2) だけが効く。
ログの再構成件数が 0 のままならここを疑う。

### 16-3. 文末記号は認識結果に依存する

Whisper の日本語出力は句点を落とすことがある。B3 が働かなくても
B1 (上限) と B2 (カット境界) で区切られるため破綻はしないが、
句点が無い素材では区切りがやや機械的になる。

### 16-4. 跨ぎテロップと手編集の相互作用

カットを跨ぐ字幕クリップが載った状態で V1 を分割・リップル削除すると、
`commands._apply_range_removal()` の規則 6 (区間を跨ぐクリップは分割せず尺を縮める /
`src/timeline/commands.py:348`) により字幕も縮む。これは従来どおりの挙動で、
意図した跨ぎが削除量ぶん短くなるだけなので許容する
(気になる場合は `Ctrl+Shift+A` で揃え直す)。

### 16-5. `origin.source_start` の意味 (第2部 §9-1 の続き)

跨ぎテロップでは `origin.source_start` が「最初のカット内の時刻」、
`origin.source_end` が「最後のカット内の時刻」になり、その間に
取り除かれた無音が挟まる。記録用途のみで現状どこからも読まれていないため
今回は触らないが、再同期機能を作るときは区間列として残す必要がある。

### 16-6. 縦動画 (`vertical`) 設定との関係

上限・下限は `build_effective_subtitle_cfg()` 適用済みの `eff_cfg` から取る。
縦動画では縦用の文字数 (10〜15) が使われるため、縦では横より
テロップが短く・本数が多くなる。request14 の方針と矛盾しない。

### 16-7. 第2部 §9-3 の「1 カット内の本数は変えない」との関係

第2部 §9-3 は「カット内の区切りには触らない」と書いたが、第3部では
**カット内でも上限文字数で区切る**ようになるため、この記述は第3部で置き換わる。
本数が変わるのは上限超過 (B1) と文末 (B3) のときだけで、
下限未満の断片が独立したテロップになることは無くなる。

---

## 17. 実装時に設計から変えた点 (2026-10-20 実装)

### 17-1. 区切り規則 B5 (認識セグメント境界) を追加した

設計当初は「認識セグメントの境界そのものは区切りにしない」としていたが、
既定設定 (下限 5 / 上限 20×2 = 40 文字) で実際に通したところ、
**同じカットの中で別々の認識セグメントが上限まで繋がり**、
0.55 秒の息継ぎを挟んだ 2 文が 1 本 (37 文字 / 7 秒) のテロップになった。

```
B5 なし: 2.00〜9.00  ちょっと話したいことがあって / それでですねまずは設定から見ていきます
B5 あり: 2.00〜5.00  ちょっと話したいことがあって      ← カット跨ぎ (1 テロップ : 2 クリップ)
         5.00〜9.00  それでですねまずは設定から見ていきます
```

裁断を直す設計で逆に「別の文がまとまる」のは要望の意図に反するため、
**下限達成後は認識セグメントの境界でも切る** (B5) を加えた。
下限未満の断片は従来どおり次と繋がるため、「はい。」のような 3 文字が
独立したテロップになることはない (§11-2 / §14-1 #15・#16 が両方を守る)。

### 17-2. 単位判定の置き場所

`commands.subtitle_unit_of()` を置く案から、
**`subtitle_generator.subtitle_unit()` 1 箇所**へ変更した。
設定の意味を解釈する処理を字幕モジュール側に集め、GUI (`timeline_controller`) からは
メソッド内の遅延 import で参照する (`proxy_manager` / `blur_spec_dialog` と同じ既存の作法)。

### 17-3. コード既定値

`recognize_for_timeline()` 内の `min_line_length` のフォールバックは
モジュール内の他の既定 (`WhisperTextSource`) と揃えて **15** とした
(`setting.json` は 5 を持つため実際には使われない)。

### 17-4. 期待値の見直しが不要だった既存テスト

`DisplayTimingWithCutBoundsTest` の既存 4 件は、発話終了が開始と同じカットへ入る
ケースだけだったため、クランプ基準を発話終了側へ移しても期待値が一致した
(§14-2)。跨ぎの挙動は新規 2 件で押さえている。

### 17-5. 実装後の確認結果

- `python -m unittest discover -s tests` → **1384 件 OK** (新規 20 件を含む)
- 既定設定で素材相当の入力を通し、(a) カット跨ぎのテロップが 1 本で残る
  (b) テロップの開始がカット頭と一致する (c) 別の文は混ざらない を確認

---

# 第4部 — カット頭の 1 文字が抜ける (追補2)

追記日: 2026-10-20 (第3部の実装を実機で確認した後の報告)

> 実機で試してイイ感じにはなってきましたが、どうもクリップの頭（１文字分）が
> よくテロップから抜けています。何が原因なんでしょうか？

---

## 18. 原因と対策

### 18-1. 結論 (原因・確定)

**カット頭の手前へこぼれた「語頭の 1 語」を、実装が黙って捨てています。**

原因は 2 段構えです。

| 段 | 内容 |
| --- | --- |
| 根本 | カット頭 (`keep_segments` の開始) と、音声認識が返す単語の開始が**別の基準**で決まっており、単語の方が先に始まる |
| 実装 | その「カットに重ならない語」を `_cut_index_of_word()` が `None` と返し、`regroup_items_by_length()` が `continue` で**文字ごと捨てる** (`src/modules/subtitle_generator.py:1526` / `:1621`) |

#### 根本: 2 つの時刻の決まり方が違う

| 側 | 決まり方 | 効果 |
| --- | --- | --- |
| カット頭 | FFmpeg `silencedetect` の `silence_end` = **レベルが `silence_cut.noise_threshold_db` (-27dB) を超えた時刻** (`silence_cutter.build_keep_segments()`) | 語頭の無声子音 (か・さ・た・は 行) や立ち上がりは -27dB 未満なので**無音側に残り、カットで消える** |
| 単語の開始 | Whisper (Silero VAD, `speech_pad_ms = 100` / `subtitle_generator.py:27`) が返す単語タイムスタンプ | 語頭の静かな部分**も含めて**始まる |

この差は実測で 0.05〜0.2 秒程度。**1 モーラ (100〜150ms) の語頭はカット頭の手前に
丸ごと入ってしまう**ことがあり、そうなるとカットとの重なりが 0 になります。

#### 実装: 重なり 0 の語は落ちる

`_cut_index_of_word()` は「単語の中点が入るカット」→「重なりが最大のカット」の
順で振り分け、**どのカットにも重ならなければ `None`** を返します。
`None` はカット済みの無音へ出た幻聴字幕を拾わないための規則ですが、
語頭がこぼれただけの語も同じ扱いで消えます。

再現 (カット = 素材 `[0,5)` と `[10,15)`。本番の関数をそのまま呼んだ結果):

```
単語 → カット番号の振り分け
  カット内                 10.50〜10.80 → カット1
  頭をまたぐ (前 0.1s)      9.90〜10.30 → カット1        ← 重なるので残る
  頭の直前に収まる (0.12s)  9.85〜 9.97 → None (落ちる)   ← これが今回の症状
  尻の直後                  5.05〜 5.20 → None (落ちる)   ← 語尾でも起きる

「かんたんです」の語頭「か」(9.88〜9.98) だけがこぼれた場合
  再構成 1 件: ['んたんです']        ← 「か」が消える
```

**「1 文字」が多い理由**: こぼれるのは語頭の 1 モーラで、しかも無声子音で始まる
語 (「か」「さ」「た」「は」…) ほど -27dB を割りやすい。語頭以外の単語は必ず
カットの中にあるため、抜けるのは**常にクリップの頭の 1 語**になります。

### 18-2. 副因 (同じ「文字が消える」結果になる経路)

`_cut_index_of_word()` の走査位置 `cursor` は前へしか進みません
(単語もカットも時間順である前提の高速化)。認識結果で単語の順序が前後すると、
走査位置が目的のカットを通り過ぎていて `None` になり、やはり文字が落ちます。

```
先に後ろの語 (10.2〜10.5) を見る → cursor=1
そのあと前の語 (4.0〜4.5) を渡す → None (落ちる)
```

実素材で起きる頻度は低い (faster-whisper の単語は基本的に時間順) ですが、
**落ち方が静かで気づけない**ため、保険を入れておくべき箇所です。

### 18-3. 対策 R6 — こぼれた語をカット端へ寄せる

**どのカットにも重ならない語は、カット端まで許容差以内なら最も近いカットへ寄せ、
時刻をそのカット内へ収める。** 許容差より離れた語は従来どおり落とす
(カット済みの無音へ出た幻聴字幕を拾わないため)。

```python
# カット頭/カット尻の外へこぼれた語を拾う許容差 (秒)
#   silencedetect は「レベルが閾値を超えた時刻」からカットを残すため、語頭の
#   無声子音や立ち上がり (閾値未満) は無音側へ落ちる。一方 Whisper は VAD の
#   speech_pad_ms=100 を足して認識するので、単語はカット頭より前から始まる。
#   この差で落ちた語を拾い直す (ver8 resolve6 第4部 R6)。
_DEFAULT_WORD_ATTACH_TOLERANCE_SEC = 0.25


# 単語を「属するカット番号」と「そのカット内へ収めた時刻」へ解決する (R6)
# 戻り値: (カット番号 or None, 収め直した単語 or None, 次の走査位置)
#
# ・どれかのカットに重なる語      → 従来どおり (中点 → 重なり最大) で決める
# ・重ならない語で端まで許容差内  → その端のカットへ寄せる (文字を落とさない)
# ・重ならない語で許容差より遠い  → None (幻聴字幕を拾わないため落とす)
#
# 時刻は必ず属するカットの中へ収める。こうしておくと
#   ・テロップの開始がカット頭とぴったり一致する (寄せた語が先頭になるため)
#   ・map_items_to_timeline() が区間を落とさない (尺 0 にならない)
def _resolve_word_cut(word, keep_segments, cursor=0,
                      attach_tolerance_sec=_DEFAULT_WORD_ATTACH_TOLERANCE_SEC):
    index, cursor = _cut_index_of_word(word, keep_segments, cursor)
    if index is None:
        index = _nearest_cut_within(word, keep_segments, attach_tolerance_sec)
        if index is None:
            return None, None, cursor
    return index, _clamp_word_to_cut(word, keep_segments[index]), cursor


# 語の前後で最も近いカットを返す (許容差を超えるなら None)
# 前後どちらも許容差内なら近い方。語頭のこぼれ (次のカットの頭) と
# 語尾のこぼれ (前のカットの尻) の両方を拾う。
def _nearest_cut_within(word, keep_segments, tolerance_sec): ...


# 語の時刻をカットの中へ収める (最低 _MIN_WORD_SEC の尺を残す)
def _clamp_word_to_cut(word, span): ...
```

`regroup_items_by_length()` 側は 3 行だけ変わります。

```python
            cut, word, cursor = _resolve_word_cut(
                word, keep_segments, cursor, attach_tolerance_sec)
            if cut is None:
                continue  # 許容差より遠い = カット済みの無音の中の語 (幻聴字幕)
```

副因 (§18-2) の保険は `_cut_index_of_word()` 側で閉じます。
走査位置から見つからなかったときは**先頭から 1 度だけ再走査**する
(通常は 1 回で当たるため、速度への影響は無い)。実装では走査本体を
`_cut_index_from(segments, from_index, ...)` へ切り出し、走査位置からの 1 回目と
先頭からの再走査で同じ規則を使う。見つかれば走査位置をそこへ戻す。

`_cut_index_of_word()` 自体は**重なりが無ければ従来どおり `None`** を返します
(寄せる判断は `_resolve_word_cut()` の責務)。こうしておくと
「重なりで決める」規則と「こぼれを拾う」規則が混ざらず、
それぞれ単体でテストできます。

### 18-4. 対策後の挙動 (試作で確認済み)

```
か      9.88〜 9.98 → カット2 へ 10.00〜10.05   ← 寄せる
んたん  10.00〜10.30 → カット2 へ 10.00〜10.30
です    10.30〜10.80 → カット2 へ 10.30〜10.80
結果: ['かんたんです'] / 開始 5.00 (カット2 の頭 = 5.00) / 落下 0 件
無音の真ん中の語 (幻聴): 落ちる (従来どおり)
```

副作用として**整列 (R2) がより正確**になります。寄せた語が先頭に来ると
テロップの開始が最初からカット頭と一致するため、許容差による吸着
(`align_tolerance_sec`) に頼る場面が減ります。

### 18-5. 設定

| キー | 既定 | 意味 |
| --- | --- | --- |
| `subtitle.word_attach_tolerance_sec` | `0.25` | カットの外へこぼれた語を拾う許容差 (秒)。`0` で従来どおり落とす |

**0.25 秒の根拠**

- 閾値の差で落ちる語頭は実測 0.05〜0.2 秒程度。VAD の `speech_pad_ms = 100` が上限の目安。
- `silence_cut.min_silence_duration_sec = 0.6` のため、取り除かれる無音は必ず 0.6 秒以上。
  前後の端から 0.25 秒ずつなら合計 0.5 秒 < 0.6 秒で、**無音の中央は必ず空く**
  = 無音の真ん中へ出た幻聴字幕は拾わない。
- 1 モーラ (100〜150ms) を 1 語ぶん拾うには十分。

### 18-6. テスト

`tests/test_subtitle_length_units.py` の `WordAttachTest` へ追加した。

| # | 名前 | 検証 |
| --- | --- | --- |
| 1 | `test_word_before_the_cut_head_is_kept` | カット頭の手前にこぼれた語が落ちない (**今回の症状の回帰テスト**) |
| 2 | `test_attached_word_starts_at_the_cut_head` | 寄せた語で始まるテロップの開始がカット頭と一致する |
| 3 | `test_word_after_the_cut_tail_is_kept` | 語尾のこぼれ (カット尻の直後) も拾う |
| 4 | `test_far_word_in_silence_is_still_dropped` | 無音の真ん中の語は従来どおり落ちる (幻聴対策の維持) |
| 5 | `test_zero_tolerance_keeps_the_old_behavior` | `word_attach_tolerance_sec = 0` で従来どおり落とす |
| 6 | `test_attached_word_is_not_lost_by_mapping` | 寄せた語だけのテロップでも写像で消えない (尺 0 にならない) |
| 7 | `test_out_of_order_word_is_not_dropped` | 走査位置が目的のカットを通り過ぎていても拾える (§18-2 の保険) |
| 8 | `test_out_of_order_text_is_not_lost` | 順序が前後した単語でも再構成で文字が落ちない |

### 18-7. 確認手順

1. 実機で編集点モードを実行し、**クリップ頭のテロップが 1 文字欠けていないこと**を
   目視する (「か」「さ」「た」「は」で始まる語を重点的に)。
2. カット済みの無音に幻聴字幕が復活していないことを確認する
   (「ご視聴ありがとうございました」等が増えていないか)。
3. `word_attach_tolerance_sec = 0` にすると症状が再発することを確認する (切り分け)。

### 18-8. それでも抜ける場合の調整 (閾値側)

R6 はテロップの**文字**を戻しますが、**音声**の語頭は無音カットで物理的に
消えたままです (-27dB 未満なのでほぼ聞こえませんが、破裂音の立ち上がりが
硬く聞こえることがあります)。音ごと残したい場合は無音カット側を緩めます。

| 設定 | 既定 | 変える方向 |
| --- | --- | --- |
| `silence_cut.noise_threshold_db` | -27 | **-30 など下げる** (より小さい音も有音とみなす → カット頭が語頭側へ戻る) |
| `subtitle.whisper_vad_min_silence_ms` | 500 | 語頭の取りこぼしが増えるようなら触る (resolve8 §9 Q1) |

`noise_threshold_db` を下げるとカットされる無音が減り、尺が伸びる副作用があります。
テロップだけを直したいなら R6、音も直したいなら閾値、という切り分けです。

### 18-9. 対象外

- `subtitle_unit = "cut"` (第2部の退路) の経路 (`_span_index_for_word()`) は
  同じ症状を持ちますが、既定経路ではないため今回は直しません。
- 焼き込み経路・物理カット経路は単語を扱わないため影響しません。

### 18-10. 実装状況

| # | 実装項目 | 状態 |
| --- | --- | --- |
| 1 | `_resolve_word_cut()` / `_nearest_cut_within()` / `_clamp_word_to_cut()` | 完了 |
| 2 | `regroup_items_by_length()` の差し替え (`attach_tolerance_sec` 引数) | 完了 |
| 3 | `_cut_index_of_word()` の再走査フォールバック (`_cut_index_from()` へ切り出し) | 完了 |
| 4 | 設定 `word_attach_tolerance_sec` の追加 (`setting.json` / `DEFAULT_SETTINGS`) | 完了 |
| 5 | テスト 8 件 (`WordAttachTest`) | 完了 |

### 18-11. 実装後の確認結果 (2026-10-20)

- `python -m unittest discover -s tests` → **1392 件 OK**
- 症状の再現ケース (語頭「か」が 9.88〜9.98 でカット頭 10.0 の手前へこぼれる)

```
対策前: ['んたんです']
対策後: ['かんたんです'] / 開始 5.00 (カット2 の頭) / 落下 0 件
無音の真ん中の語 (幻聴): 落ちる (従来どおり)
走査位置が進みすぎた場合 (cursor=1 で 4.0〜4.5 の語): カット0 (落ちない)
```

- `recognize_for_timeline()` を `setting.json` そのまま (許容差 0.25 / 単位 length) で
  通した結果。認識だけスタブ化し、他は本番コード:

```
   0.00〜 3.60 | こんにちは        ← カット頭
   5.00〜 7.80 | かんたんです      ← カット頭。語頭の「か」が残っている
```
