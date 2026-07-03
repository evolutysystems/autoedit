# 修正案 (resolve)

## 1. エラー概要

```
[Parsed_asetpts_3] filter output pad 0 (audio) と
[Parsed_concat_40] filter input pad 10 (video) の Media type mismatch
Error linking filters / Invalid argument (code=4294967274 = -22 / EINVAL)
```

無音カット処理 (`src/modules/silence_cutter.py`) の `cut_and_concat` が組み立てる
`filter_complex` 内で、`concat` フィルターへ渡すストリームの **接続順序** が誤っている。

---

## 2. 根本原因

`concat=n=N:v=1:a=1` フィルターは、入力パッドを
**「セグメントごとに [映像][音声] を交互」** に並べることを要求する。

| 入力パッド | 期待される型 |
|-----------|-------------|
| pad 0     | video (seg0) |
| pad 1     | audio (seg0) |
| pad 2     | video (seg1) |
| pad 3     | audio (seg1) |
| …         | …            |

つまり **偶数パッド = 映像 / 奇数パッド = 音声**。

ところが現行コード (`silence_cutter.py` 127-130行) は次のように
**「映像を全部まとめてから音声を全部」** の順で連結している。

```python
concat_filter = (
    "".join(concat_inputs_v + concat_inputs_a) +   # [v0][v1]...[a0][a1]...
    f"concat=n={n}:v=1:a=1[outv][outa]"
)
```

この結果、本来「映像」であるべき偶数パッド（例: pad 10）に
音声ストリーム（`asetpts` 出力）が接続され、型不一致で
`Error linking filters` が発生する。

- 50秒・無音の多い動画ほどセグメント数 (n) が増え、パッド番号が大きくなるため
  エラーメッセージの `pad 10` のように顕在化しやすい。
- `concat_processor.py` は `[{i}:v:0][{i}:a:0]` と正しく交互に並べているため問題なし。
  （= 同種バグはこのファイルにのみ存在）

---

## 3. 修正内容

`src/modules/silence_cutter.py` の `cut_and_concat` 内、concat 入力連結部を
**映像/音声をセグメント単位で交互** に並べるよう変更する。

### 修正前 (127-130行付近)

```python
n = len(keep_segments)
concat_filter = (
    "".join(concat_inputs_v + concat_inputs_a) +
    f"concat=n={n}:v=1:a=1[outv][outa]"
)
```

### 修正後

```python
n = len(keep_segments)
# concat フィルターはセグメント毎に [映像][音声] を交互に並べる必要がある
# (偶数パッド=映像 / 奇数パッド=音声)。まとめて並べると型不一致になる。
interleaved = "".join(
    v + a for v, a in zip(concat_inputs_v, concat_inputs_a)
)
concat_filter = interleaved + f"concat=n={n}:v=1:a=1[outv][outa]"
```

これにより生成される `filter_complex` の末尾が
`[v0][v1]...[a0][a1]...concat=...` から
`[v0][a0][v1][a1]...concat=...` に変わり、各パッドの型が一致する。

---

## 4. 修正後の確認手順

1. `python main.py` を再実行し、無音カット工程が正常完了することを確認する。
2. 出力動画に映像・音声が両方含まれ、同期が取れていることを確認する。
3. 検証用に、無音区間が多くセグメント数が多い動画
   （今回の 2560×1080 / 50秒）で再現していたことを確認する。
4. 余裕があれば `cut_and_concat` の単体テストを追加し、
   生成される `filter_complex` 文字列が `[v0][a0][v1][a1]...` 形式に
   なることを検証する（リグレッション防止）。

---

## 5. 補足 (今回の修正対象外だが要確認事項)

- 入力動画に音声トラックが存在しない場合、`[0:a]` 参照で別エラーになり得る。
  将来的には `ffprobe` で音声有無を判定し、音声無し時は `v=1:a=0` へ
  分岐する設計が望ましい（設計書のエラー処理方針に追記候補）。
- 解像度依存のバグではない（型不一致が原因のため、解像度は無関係）。
