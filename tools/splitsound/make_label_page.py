# Split Sound 実験: 正解データ作成用のローカル作業ページを作る
# (docs/request/ver10/resolve_ss1.md §3.3 / truth_schema.md)
#
# やること
#   ① C0 (ベースライン) の認識結果を叩き台として下書き JSONL を書く
#   ② candidates/<素材>.json があれば**区分の候補**を当て込む
#   ③ 認識結果の**空き区間** (どの行にも入っていない時間) を抜き出す
#      → そこに発話が隠れていると取りこぼし率 (M5) と CER (M3) がずれるため、
#        ページ上で「行を足す」ことができるようにする
#   ④ 以上を埋め込んだ HTML を素材フォルダへ置く
#      → ブラウザで analysis_source.wav を鳴らしながら確認・修正し、
#        「JSONL を書き出す」で正解データの形のまま取り出せる
#
# HTML は**ローカル専用**である (音声ファイルを相対パスで読むため)。
# 公開したりアップロードしたりはしない。
import argparse
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402

TRUTH_DIR = os.path.join(common.REPO_ROOT, "docs", "request", "ver10", "ss1_truth")
CANDIDATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candidates")

# これより短い空きは出さない (息継ぎ程度の隙間を並べても確認の邪魔になる)
_DEFAULT_GAP_SEC = 1.5

# 空き区間の音量を測る粒度 (秒) と、「何か鳴っている」とみなす下限 (dBFS)
_GAP_FRAME_SEC = 0.1
_DEFAULT_GAP_LOUD_DB = -50.0


# 認識結果の本文を人が読める形へ直す
# WhisperTextSource は字幕の体裁として ASS の改行記号 (\N) を入れるため、空白へ戻す。
# これを残すと CER の正解にも混ざり、誤差として数えられてしまう。
def clean_text(text):
    return re.sub(r"\s+", " ", re.sub(r"\\[Nn]", " ", str(text or ""))).strip()


# C0 の認識結果 (叩き台) を読む
def load_draft_items(material_id):
    path = os.path.join(common.condition_dir(material_id, "c0_baseline"), "result.json")
    if not os.path.exists(path):
        raise RuntimeError(
            f"{path} がありません。先に "
            f"run_conditions.py --materials {material_id} --conditions c0_baseline "
            f"を (--no-asr なしで) 実行してください")
    result = common.load_json(path)
    items = (result.get("asr") or {}).get("items") or []
    if not items:
        raise RuntimeError("認識結果が空です (asr.items が無い)")
    return items, result


# 区分の候補を読む (無ければ空)
def load_candidates(material_id):
    path = os.path.join(CANDIDATE_DIR, f"{material_id}.json")
    if not os.path.exists(path):
        return {}, {}
    doc = common.load_json(path)
    return doc.get("items") or {}, doc


# 認識区間を「語の時刻」で切り直す (2026-11-12 追加 / --by-words)
#
# 【なぜ要るか】認識の区間は VAD の都合で実発話より大幅に長くなる。
# S3 (F1 視聴) では区間ベースの発話率 87.2% に対し語ベースは 34.7% で、
# **区間をそのまま下書きにすると「喋っていない時間」まで host として記録され、
# M1 / M2 が 2.5 倍過大になる**。語の時刻で切れば実発話に近い行になる。
#
# 切る間隔は truth_schema.md の決め「0.3 秒未満の間は切らない」に合わせる。
# 語の本文が無い item (word_timestamps 無しで認識したもの) は区間のまま残す。
#
# 戻り値: (行の一覧, 行ごとの「元の item の index」)
def split_items_by_words(items, max_gap):
    rows = []
    origins = []

    def flush(item, group, index):
        rows.append({
            "start": round(float(group[0]["start"]), 3),
            "end": round(float(group[-1]["end"]), 3),
            "text": "".join(str(w.get("text", "") or "") for w in group),
        })
        origins.append(index)

    for index, item in enumerate(items):
        words = [w for w in (item.get("words") or [])
                 if w.get("start") is not None and w.get("end") is not None]
        if not words:
            rows.append(item)
            origins.append(index)
            continue
        group = [words[0]]
        for word in words[1:]:
            if float(word["start"]) - float(group[-1]["end"]) >= max_gap:
                flush(item, group, index)
                group = [word]
            else:
                group.append(word)
        flush(item, group, index)
    return rows, origins


# 語で切った行のうち、本文が極端に短いものを隣へ併合する (2026-11-12 追加)
#
# 【なぜ要るか】faster-whisper の語は日本語では 1 文字ずつ返ることがあり、しかも
# 語の時刻が飛ぶため「ね」「ッ」だけの行ができる (S3 では 106 行中 28 行)。
# CER にも M1 にも寄与せず、確認の邪魔になるだけなので、**間が近い隣へ寄せる**。
#
# 間が `max_gap` を超えるときは寄せない (離れた無音を抱き込むと時刻が過大になるため)。
# 戻り値: (行の一覧, 行ごとの元 index)
def merge_tiny_rows(rows, origins, min_chars, max_gap):
    if not rows:
        return rows, origins

    def body(row):
        return len(str(row.get("text", "")).strip()) > min_chars

    merged = []
    kept_origins = []
    index = 0
    while index < len(rows):
        # 同じ認識区間から出た行をひとまとめに取る
        source = origins[index]
        group = []
        while index < len(rows) and origins[index] == source:
            group.append(dict(rows[index]))
            index += 1

        bodies = [row for row in group if body(row)]
        if not bodies:
            # 区間ぜんぶが断片 (「2」「1」のような短い発話) → 1 行にまとめる
            head = dict(group[0])
            head["end"] = round(max(float(r["end"]) for r in group), 3)
            head["text"] = "".join(str(r.get("text", "")) for r in group)
            merged.append(head)
            kept_origins.append(source)
            continue

        # 断片は**時刻を捨てて**最も近い本体へ本文だけ吸収させる。
        # faster-whisper は区間の最初の語を区間の開始時刻へ強制的に合わせるため、
        # 実発話より早い時刻の断片ができる (「マ」10.18 / 次の語「ジ」16.76)。
        # その時刻を残すと**喋っていない時間を host として記録してしまう**。
        # 寄せ先は**元の語順**で決める (時刻の近さで本文を足すと語が入れ替わり、
        # CER の正解がずれる: 「アウト側」→「ウアト側」)。
        # いったん本体ごとに「前に付く断片 / 後に付く断片」を出現順で集め、最後に繋ぐ。
        before_parts = {id(row): [] for row in bodies}
        after_parts = {id(row): [] for row in bodies}
        for position, row in enumerate(group):
            if body(row):
                continue
            before = [b for b in group[:position] if body(b)]
            after = [b for b in group[position + 1:] if body(b)]
            to_after = float(after[0]["start"]) - float(row["end"]) if after else None
            to_before = float(row["start"]) - float(before[-1]["end"]) if before else None
            if after and (to_before is None or to_after <= to_before):
                before_parts[id(after[0])].append(row)
            else:
                after_parts[id(before[-1])].append(row)

        for row in bodies:
            pre = before_parts[id(row)]
            post = after_parts[id(row)]
            row["text"] = ("".join(str(r.get("text", "")) for r in pre)
                           + str(row.get("text", ""))
                           + "".join(str(r.get("text", "")) for r in post))
            # 時刻は**すぐ隣の断片だけ**取り込む。離れた断片の時刻は捨てる
            # (喋っていない時間を host として記録しないため)。
            if pre and float(row["start"]) - float(pre[-1]["end"]) <= max_gap:
                row["start"] = round(min(float(row["start"]),
                                         float(pre[-1]["start"])), 3)
            if post and float(post[0]["start"]) - float(row["end"]) <= max_gap:
                row["end"] = round(max(float(row["end"]), float(post[0]["end"])), 3)
            merged.append(row)
            kept_origins.append(source)
    return merged, kept_origins


# 区分の候補を「分割後の行」の index へ写す
# 1 つの認識区間から分かれた行は、同じ発話なので同じ候補を引き継ぐ。
def remap_candidates(candidates, origins):
    if not candidates:
        return {}
    mapped = {}
    for position, source in enumerate(origins):
        hint = candidates.get(str(source))
        if hint:
            mapped[str(position)] = hint
    return mapped


# どの行にも入っていない時間 (空き区間) を求める
# ここに発話が隠れていると「認識が落とした発話」が正解にも載らず、
# M5 (取りこぼし率) が実態より良く出てしまう。
def find_gaps(items, duration, min_sec=_DEFAULT_GAP_SEC):
    gaps = []
    cursor = 0.0
    for item in sorted(items, key=lambda i: float(i["start"])):
        start = float(item["start"])
        if start - cursor >= min_sec:
            gaps.append({"start": round(cursor, 2), "end": round(start, 2)})
        cursor = max(cursor, float(item["end"]))
    if duration - cursor >= min_sec:
        gaps.append({"start": round(cursor, 2), "end": round(duration, 2)})
    return gaps


# 空き区間ごとに「音が鳴っているか」を測る (2026-11-12 追加)
#
# 【なぜ要るか】S3 では空き区間が 34 件 / 合計 220s あるが、実測では非発話区間のうち
# 何か鳴っているのは 15% 程度 (36s) で、**残りは本当の無音**である (result_ss1.md §11)。
# 34 件すべてを聞くのは無駄なので、「音がある空き区間」だけに絞れるようにする。
#
# この切り分けは指標に直接効く。
#   ・音が鳴っている空き区間を取りこぼす → `game` 行が足りず **M2 の分母が欠ける**
#   ・本当の無音を `game` にしてしまう   → **M6 (完全な無音に出たテロップ) が測れない**
# どちらも「耳で全部確認する」以外に防ぎようが無いので、音量で当たりを付ける。
def measure_gaps(material_id, gaps, settings, floor_db=_DEFAULT_GAP_LOUD_DB):
    if not gaps:
        return gaps
    path = os.path.join(common.material_dir(material_id), "analysis_source.wav")
    if not os.path.exists(path):
        return gaps
    samples = common.read_audio(path, settings, channels=1)
    mono = samples[0] if samples.ndim > 1 else samples
    frame = int(common.OUT_SAMPLE_RATE * _GAP_FRAME_SEC)
    count = len(mono) // frame
    if count <= 0:
        return gaps
    block = mono[:count * frame].reshape(count, frame)
    rms = np.sqrt((block.astype(np.float64) ** 2).mean(axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-12))
    for gap in gaps:
        begin = max(int(float(gap["start"]) / _GAP_FRAME_SEC), 0)
        finish = min(int(float(gap["end"]) / _GAP_FRAME_SEC), len(db))
        if finish <= begin:
            continue
        window = db[begin:finish]
        loud = window >= floor_db
        gap["median_db"] = round(float(np.median(window)), 1)
        gap["loud_sec"] = round(float(loud.sum()) * _GAP_FRAME_SEC, 1)
        gap["loud_ratio"] = round(float(loud.mean()), 3)
    return gaps


# 下書き JSONL を書く (候補があれば speaker に入れ、conf を添える)
def write_draft_jsonl(material_id, items, candidates, game_everywhere):
    os.makedirs(TRUTH_DIR, exist_ok=True)
    path = os.path.join(TRUTH_DIR, f"{material_id}.draft.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("// 下書き。speaker は**候補** (内容から読んだ推測) であり、"
                     "耳で確認して直してください\n")
        handle.write("// conf=\"low\" の行は判断がつかなかったもの。"
                     "確認の優先度が高いです\n")
        handle.write("// 本文 (text) を直すと CER の正解になります。"
                     "**聞こえたとおりに**書いてください (直し過ぎない)\n")
        if game_everywhere:
            handle.write(json.dumps(
                {"_meta": {"game_everywhere": True}}, ensure_ascii=False) + "\n")
        for index, item in enumerate(items):
            hint = candidates.get(str(index)) or {}
            row = {
                "start": item["start"], "end": item["end"],
                "speaker": hint.get("cand", ""),
                "text": clean_text(item["text"]),
            }
            if hint.get("conf"):
                row["conf"] = hint["conf"]
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #14161a; --panel: #1c1f26; --line: #2c313b; --text: #e6e8ec;
    --muted: #9aa3b2; --host: #3ddc97; --sub: #58a6ff; --game: #ff6b6b;
    --other: #f4c04e; --warn: #ffb02e;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font-family: "Yu Gothic UI", system-ui, sans-serif; font-size: 14px; }
  header { position: sticky; top: 0; background: var(--panel);
           border-bottom: 1px solid var(--line); padding: 10px 14px; z-index: 5; }
  h1 { font-size: 15px; margin: 0 0 8px; font-weight: 600; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  audio { width: 100%; max-width: 500px; }
  .stat { color: var(--muted); font-variant-numeric: tabular-nums; }
  .stat b { color: var(--text); }
  button { background: #262b34; color: var(--text); border: 1px solid var(--line);
           border-radius: 6px; padding: 5px 10px; cursor: pointer; font-size: 13px; }
  button:hover { background: #2f353f; }
  button[data-active="1"] { border-color: var(--warn); color: var(--warn); }
  table { width: 100%; border-collapse: collapse; }
  th, td { border-bottom: 1px solid var(--line); padding: 5px 8px; text-align: left;
           vertical-align: top; }
  th { position: sticky; top: 96px; background: var(--panel); color: var(--muted);
       font-weight: 600; font-size: 12px; z-index: 4; }
  tr.cur { background: #232834; }
  tr.lowconf td:nth-child(2) { border-left: 3px solid var(--warn); }
  tr.ok td:first-child { color: var(--host); }
  tr.hidden { display: none; }
  tr.gap { background: #171a20; color: var(--muted); }
  tr.gap td { border-bottom: 1px dashed var(--line); font-size: 13px; }
  tr.gap.filled { background: #1b2430; color: var(--text); }
  .t { font-family: Consolas, monospace; white-space: nowrap; color: var(--muted);
       cursor: pointer; font-variant-numeric: tabular-nums; }
  .t:hover { color: var(--text); text-decoration: underline; }
  .seg { width: 1%; font-variant-numeric: tabular-nums; }
  .pick { display: flex; gap: 4px; }
  .pick button { padding: 3px 7px; font-size: 12px; }
  .pick button[data-on="1"] { color: #14161a; font-weight: 700; }
  .pick button[data-on="cand"] { border-style: dashed; }
  .pick button.h[data-on="1"] { background: var(--host); border-color: var(--host); }
  .pick button.s[data-on="1"] { background: var(--sub); border-color: var(--sub); }
  .pick button.g[data-on="1"] { background: var(--game); border-color: var(--game); }
  .pick button.o[data-on="1"] { background: var(--other); border-color: var(--other); }
  textarea, input[type=number] { background: #0f1115; color: var(--text);
             border: 1px solid var(--line); border-radius: 6px; padding: 6px;
             font-family: inherit; font-size: 13px; }
  textarea { width: 100%; resize: vertical; }
  td.txt textarea { min-height: 34px; }
  input[type=number] { width: 86px; font-family: Consolas, monospace; }
  #out { margin: 14px; }
  #out textarea { height: 200px; font-family: Consolas, monospace; font-size: 12px; }
  .hint { color: var(--muted); margin: 10px 14px; line-height: 1.75; }
  .hint b { color: var(--text); }
  kbd { background: #262b34; border: 1px solid var(--line); border-radius: 4px;
        padding: 1px 5px; font-family: Consolas, monospace; font-size: 12px; }
  .tag { font-size: 11px; padding: 1px 5px; border-radius: 4px;
         border: 1px solid var(--line); color: var(--muted); margin-left: 6px; }
  .tag.low { color: var(--warn); border-color: var(--warn); }
  .tag.loud { color: var(--game); border-color: var(--game); }
  .tag.quiet { opacity: .55; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="row">
    <audio id="player" src="__AUDIO__" controls preload="metadata"></audio>
    <span class="stat">確認済み <b id="okc">0</b> / <span id="total">0</span>
      ・未確認の低確度 <b id="lowc">0</b> ・足した行 <b id="addc">0</b><br>
      host <b id="nh">0</b> ・sub <b id="ns">0</b>
      ・game <b id="ng">0</b> ・other <b id="no">0</b>
      ・本文を直した行 <b id="edc">0</b>
      ・聞き取れない <b id="unc">0</b></span>
  </div>
  <div class="row" style="margin-top:8px">
    <button id="only-low">低確度だけ表示</button>
    <button id="only-unconf">未確認だけ表示</button>
    <button id="only-gap">空き区間だけ表示</button>
    <button id="only-loud">音がある空きだけ</button>
    <button id="export">JSONL を書き出す</button>
    <button id="download">ファイルで保存</button>
    <button id="reset">候補に戻す</button>
  </div>
</header>

<div class="hint">
  <b>区分は候補が入っています</b> (破線 = 候補のまま未確認 / 実線 = 自分で決めた)。
  <span class="tag low">low</span> は候補の確度が低い行で、<b>まずここだけ確認すれば十分</b>です。<br>
  <b>本文を直すと CER (文字起こし精度) の正解になります。</b>
  直すときは<b>聞こえたとおり</b>に書いてください
  (言い直し・言い間違いもそのまま。きれいな日本語へ直すと、正しく認識できた方が誤りに数えられます)。<br>
  <b>聞き取れないときは <kbd>U</kbd> か <kbd>[?]</kbd> ボタン</b>。
  本文に <code>[?]</code> が入り、<b>その区間は CER の採点から外れます</b>
  (区分はそのまま残るので、残す/切るの判定には使われます)。
  一部だけ聞き取れないときも、聞こえた部分を書いて <code>[?]</code> を混ぜれば同じ扱いです
  (<b>その行は丸ごと採点から外れます</b>)。
  ほぼ聞き取れていて 1〜2 文字が怪しいだけなら、<code>[?]</code> は入れずに聞こえたとおり書いてください。<br>
  <b>同じ区間を 2 人が喋っているときは <kbd>＋人</kbd></b> で行を足し、それぞれの本文を書いてください
  (時刻が重なっていて構いません)。片方が聞き取れなければその行だけ <code>[?]</code> にします。<br>
  <b>灰色の行は「空き区間」</b>= どの行にも入っていない時間です。聞いて発話があったら
  「＋発話を足す」で行を足してください (認識が落とした発話がここに入ります)。<br>
  時刻クリック = その区間だけ再生 ／
  <kbd>1</kbd> host ・<kbd>2</kbd> sub ・<kbd>3</kbd> game ・<kbd>4</kbd> other ・
  <kbd>Enter</kbd> 候補のまま確認済みに ・<kbd>U</kbd> 聞き取れない ・<kbd>0</kbd> 取り消し ／
  <kbd>J</kbd> 次 ・<kbd>K</kbd> 前 ・<kbd>R</kbd> もう一度 ・<kbd>Space</kbd> 再生/停止<br>
  <b>host</b> = 残したい人の声 (配信者 3 人 + 会話相手の出展者・スタッフ) ／
  <b>sub</b> = 配信者を区別したいときだけ (評価では host と同じ) ／
  <b>game</b> = 人の声が無い環境音のみ ／ <b>other</b> = 場内アナウンス・通行人など残したくない人の声
</div>

<table>
  <thead><tr>
    <th class="seg">#</th><th>時刻</th><th>区分</th><th>本文 (host/sub のみ必須)</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>

<div id="out">
  <textarea id="jsonl" readonly placeholder="「JSONL を書き出す」を押すとここに出ます"></textarea>
</div>

<script>
const ITEMS = __ITEMS__;
const GAPS = __GAPS__;
const MATERIAL = "__MATERIAL__";
const GAME_EVERYWHERE = __GAME_EVERYWHERE__;
const KEY = "splitsound_truth_" + MATERIAL;
const SPEAKERS = { "1": "host", "2": "sub", "3": "game", "4": "other" };

// 保存形式: {seg: {<区間番号>: {...}}, gap: {<空き番号>: {...}}}
// 旧形式 ({<区間番号>: {...}}) を読み込んだときは seg へ移す。
function loadState() {
  let raw = {};
  try { raw = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { raw = {}; }
  if (raw && (raw.seg || raw.gap || raw.over)) {
    return { seg: raw.seg || {}, gap: raw.gap || {}, over: raw.over || {} };
  }
  return { seg: raw || {}, gap: {}, over: {} };
}
function saveState() {
  try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* 続行 */ }
}
let state = loadState();
let current = 0;            // ROWS の添字
let filter = "";

// 区間と空きを時刻順に 1 本へ並べる
const ROWS = [];
ITEMS.forEach((item, index) => ROWS.push({ kind: "seg", index: index, start: item.start }));
GAPS.forEach((gap, index) => ROWS.push({ kind: "gap", index: index, start: gap.start }));
ROWS.sort((a, b) => a.start - b.start);

const player = document.getElementById("player");
const tbody = document.getElementById("rows");
const fmt = (s) => {
  const m = Math.floor(s / 60), r = (s % 60).toFixed(2).padStart(5, "0");
  return m + ":" + r;
};
const segState = (i) => state.seg[i] || {};
const gapState = (i) => state.gap[i] || {};
const speakerOf = (i) => {
  const row = segState(i);
  return row.speaker !== undefined ? row.speaker : (ITEMS[i].cand || "");
};
const confirmedOf = (i) => !!segState(i).confirmed;
const textOf = (i) => {
  const row = segState(i);
  return row.text !== undefined ? row.text : ITEMS[i].text;
};

// 行を組む
ROWS.forEach((row, position) => {
  const tr = document.createElement("tr");
  tr.dataset.pos = position;
  if (row.kind === "seg") {
    const item = ITEMS[row.index];
    tr.innerHTML =
      '<td class="seg">' + (row.index + 1) + '</td>' +
      '<td class="t">' + fmt(item.start) + " – " + fmt(item.end) +
        (item.conf === "low" ? '<span class="tag low">low</span>' : '') + '</td>' +
      '<td><div class="pick">' +
        '<button class="h" data-sp="host">host</button>' +
        '<button class="s" data-sp="sub">sub</button>' +
        '<button class="g" data-sp="game">game</button>' +
        '<button class="o" data-sp="other">other</button>' +
        '<button class="u" title="聞き取れない (CER の採点から外す)">[?]</button>' +
        '<button class="dup" title="同じ区間でもう 1 人話している (行を足す)">＋人</button>' +
      '</div></td>' +
      '<td class="txt"><textarea rows="1"></textarea></td>';
    const area = tr.querySelector("textarea");
    area.value = textOf(row.index);
    area.addEventListener("input", () => {
      state.seg[row.index] = Object.assign({}, segState(row.index),
        { text: area.value });
      saveState(); render();
    });
    tr.querySelectorAll(".pick button").forEach((btn) => {
      if (!btn.dataset.sp) return;
      btn.addEventListener("click", () => setSpeaker(row.index, btn.dataset.sp));
    });
    tr.querySelector(".pick .u").addEventListener("click", () => {
      markUnclear(row.index);
      area.value = textOf(row.index);
    });
    tr.querySelector(".pick .dup").addEventListener("click", () => {
      addOverlap(row.index, tr);
    });
  } else {
    const gap = GAPS[row.index];
    tr.className = "gap";
    tr.innerHTML =
      '<td class="seg">–</td>' +
      '<td class="t">' + fmt(gap.start) + " – " + fmt(gap.end) +
        ' <span class="tag">空き ' + (gap.end - gap.start).toFixed(1) + 's</span>' +
        gapLoudTag(gap) + '</td>' +
      '<td class="cell-pick"></td>' +
      '<td class="cell-txt"></td>';
    renderGapCells(tr, row.index);
  }
  tr.querySelector(".t").addEventListener("click", () => playRow(position));
  tbody.appendChild(tr);
  if (row.kind === "seg") renderOverlapRows(row.index, tr);
});

// 空き区間に音が鳴っているか (measure_gaps の実測) を印で出す。
// 「音あり」の空きだけ聞けば足りるようにするための道具。
function gapLoudTag(gap) {
  if (gap.loud_sec === undefined || gap.loud_sec === null) return "";
  if (gap.loud_sec < 0.3) {
    return ' <span class="tag quiet">実測ほぼ無音</span>';
  }
  return ' <span class="tag loud">音あり ' + gap.loud_sec.toFixed(1) + 's</span>';
}

// 空き行の中身を組み直す (未使用 = ボタンだけ / 使用中 = 時刻+区分+本文)
function renderGapCells(tr, gapIndex) {
  const gap = GAPS[gapIndex];
  const saved = gapState(gapIndex);
  const pick = tr.querySelector(".cell-pick");
  const txt = tr.querySelector(".cell-txt");
  if (!saved.used) {
    pick.innerHTML = '<button class="add">＋発話を足す</button>';
    if (gap.loud_sec !== undefined && gap.loud_sec >= 0.3) {
      txt.textContent = "音が鳴っています (" + gap.loud_sec.toFixed(1)
        + "s / 中央 " + gap.median_db + "dB)。聞いて game / other / 発話を決めてください";
    } else if (gap.loud_sec !== undefined) {
      txt.textContent = "実測でほぼ無音です。そのままでよいです";
    } else {
      txt.textContent = "聞いて発話が無ければ、このままでよいです";
    }
    pick.querySelector(".add").addEventListener("click", () => {
      state.gap[gapIndex] = {
        used: true, speaker: "host", text: "",
        start: gap.start, end: gap.end,
      };
      saveState(); renderGapCells(tr, gapIndex); render();
    });
    return;
  }
  pick.innerHTML =
    '<div class="pick">' +
      '<button class="h" data-sp="host">host</button>' +
      '<button class="s" data-sp="sub">sub</button>' +
      '<button class="g" data-sp="game">game</button>' +
      '<button class="o" data-sp="other">other</button>' +
    '</div>' +
    '<div style="margin-top:4px"><button class="del">行を消す</button></div>';
  txt.innerHTML =
    '<div class="row" style="gap:6px;margin-bottom:4px">' +
      '開始 <input type="number" step="0.01" class="gs" value="' + saved.start + '">' +
      '終了 <input type="number" step="0.01" class="ge" value="' + saved.end + '">' +
    '</div>' +
    '<textarea rows="1" class="gt"></textarea>';
  txt.querySelector(".gt").value = saved.text || "";
  const update = (patch) => {
    state.gap[gapIndex] = Object.assign({}, gapState(gapIndex), patch);
    saveState(); render();
  };
  txt.querySelector(".gt").addEventListener("input", (e) => update({ text: e.target.value }));
  txt.querySelector(".gs").addEventListener("change",
    (e) => update({ start: Number(e.target.value) }));
  txt.querySelector(".ge").addEventListener("change",
    (e) => update({ end: Number(e.target.value) }));
  pick.querySelectorAll(".pick button").forEach((btn) => {
    btn.dataset.on = btn.dataset.sp === saved.speaker ? "1" : "0";
    btn.addEventListener("click", () => {
      update({ speaker: btn.dataset.sp });
      renderGapCells(tr, gapIndex);
    });
  });
  pick.querySelector(".del").addEventListener("click", () => {
    delete state.gap[gapIndex];
    saveState(); renderGapCells(tr, gapIndex); render();
  });
}

function setSpeaker(index, speaker) {
  const now = speakerOf(index);
  const next = (now === speaker && confirmedOf(index)) ? "" : speaker;
  state.seg[index] = Object.assign({}, segState(index),
    { speaker: next, confirmed: next !== "" });
  saveState(); render();
}

// 同じ区間でもう 1 人話している場合の行を足す
//
// 時刻が重なる行があってよい (評価は「残したい声」を union で見て、
// 本文は開始時刻の順に繋ぐ / truth_schema.md)。
// 既定は sub (共演者) で、区分は後から変えられる。
function addOverlap(index, tr) {
  const item = ITEMS[index];
  const key = String(nextOverlapKey());
  state.over[key] = { start: item.start, end: item.end, speaker: "sub", text: "" };
  saveState();
  renderOverlapRows(index, tr);
  render();
}

function nextOverlapKey() {
  let max = 0;
  Object.keys(state.over).forEach((k) => { max = Math.max(max, Number(k) + 1); });
  return max;
}

// ある区間に紐づく重なり行を、その行の下へ描き直す
function renderOverlapRows(index, tr) {
  // 既存の描画を消す
  let next = tr.nextSibling;
  while (next && next.classList && next.classList.contains("over-of-" + index)) {
    const target = next; next = next.nextSibling; target.remove();
  }
  const item = ITEMS[index];
  Object.keys(state.over).forEach((key) => {
    const saved = state.over[key];
    if (!saved || saved.start !== item.start) return;   // この区間に足したものだけ
    const extra = document.createElement("tr");
    extra.className = "gap filled over-row over-of-" + index;
    extra.innerHTML =
      '<td class="seg">＋</td>' +
      '<td class="t">同じ区間 (重なり)</td>' +
      '<td><div class="pick">' +
        '<button class="h" data-sp="host">host</button>' +
        '<button class="s" data-sp="sub">sub</button>' +
        '<button class="o" data-sp="other">other</button>' +
        '<button class="u2">[?]</button>' +
      '</div><div style="margin-top:4px"><button class="del">行を消す</button></div></td>' +
      '<td><textarea rows="1" class="ot"></textarea></td>';
    extra.querySelector(".ot").value = saved.text || "";
    const update = (patch) => {
      state.over[key] = Object.assign({}, state.over[key], patch);
      saveState(); renderOverlapRows(index, tr); render();
    };
    extra.querySelector(".ot").addEventListener("input",
      (e) => { state.over[key].text = e.target.value; saveState(); });
    extra.querySelectorAll(".pick button[data-sp]").forEach((btn) => {
      btn.dataset.on = btn.dataset.sp === saved.speaker ? "1" : "0";
      btn.addEventListener("click", () => update({ speaker: btn.dataset.sp }));
    });
    extra.querySelector(".u2").addEventListener("click",
      () => update({ text: "[?]", unclear: true }));
    extra.querySelector(".del").addEventListener("click", () => {
      delete state.over[key]; saveState(); renderOverlapRows(index, tr); render();
    });
    tr.parentNode.insertBefore(extra, tr.nextSibling);
  });
}

// 聞き取れなかった行にする ([?] を本文へ入れ、区分は「残したい声」のまま)
//
// 本文が作れないので CER の採点からは外れる (evaluate.py が [?] を見て両側から外す)。
// 残す / 切るの判定には使えるので、区分は消さない。
function markUnclear(index) {
  const speaker = speakerOf(index) || "host";
  state.seg[index] = Object.assign({}, segState(index),
    { speaker: speaker, confirmed: true, text: "[?]", unclear: true });
  saveState(); render();
}

function confirmCandidate(index) {
  state.seg[index] = Object.assign({}, segState(index),
    { speaker: speakerOf(index), confirmed: true });
  saveState(); render();
}

function render() {
  let ok = 0, low = 0, added = 0, edited = 0, unclear = 0;
  const counts = { host: 0, sub: 0, game: 0, other: 0 };
  tbody.querySelectorAll("tr").forEach((tr) => {
    // 重なり行 (＋人 で足した行) は ROWS に無く data-pos を持たない。
    // ここで弾かないと ROWS[NaN] を触って例外になり、**以降の表示更新が全部止まる**
    // (区分ボタンを押しても見た目が変わらなくなる)。
    if (tr.dataset.pos === undefined || tr.dataset.pos === "") return;
    const position = Number(tr.dataset.pos);
    const row = ROWS[position];
    if (!row) return;
    tr.classList.toggle("cur", position === current);
    if (row.kind === "seg") {
      const index = row.index;
      const speaker = speakerOf(index);
      const confirmed = confirmedOf(index);
      tr.querySelectorAll(".pick button[data-sp]").forEach((btn) => {
        btn.dataset.on = btn.dataset.sp === speaker ? (confirmed ? "1" : "cand") : "0";
      });
      tr.classList.toggle("ok", confirmed);
      tr.classList.toggle("lowconf", ITEMS[index].conf === "low" && !confirmed);
      const hideLow = filter === "low" && !(ITEMS[index].conf === "low" && !confirmed);
      const hideUnconf = filter === "unconf" && confirmed;
      tr.classList.toggle("hidden", hideLow || hideUnconf
                                    || filter === "gap" || filter === "loud");
      if (confirmed) ok++;
      if (ITEMS[index].conf === "low" && !confirmed) low++;
      if (speaker) counts[speaker] = (counts[speaker] || 0) + 1;
      if (segState(index).text !== undefined
          && segState(index).text !== ITEMS[index].text) edited++;
      if (segState(index).unclear
          || (segState(index).text || "").indexOf("[?]") >= 0) unclear++;
    } else {
      const saved = gapState(row.index);
      const loudSec = GAPS[row.index].loud_sec;
      const quiet = !(loudSec !== undefined && loudSec !== null && loudSec >= 0.3);
      tr.classList.toggle("filled", !!saved.used);
      tr.classList.toggle("hidden", filter === "low" || filter === "unconf"
                                    || (filter === "loud" && quiet));
      if (saved.used) {
        added++;
        if (saved.speaker) counts[saved.speaker] = (counts[saved.speaker] || 0) + 1;
      }
    }
  });
  document.getElementById("okc").textContent = ok;
  document.getElementById("lowc").textContent = low;
  document.getElementById("addc").textContent =
    added + Object.keys(state.over).length;
  document.getElementById("edc").textContent = edited;
  document.getElementById("unc").textContent = unclear;
  document.getElementById("total").textContent = ITEMS.length;
  document.getElementById("nh").textContent = counts.host || 0;
  document.getElementById("ns").textContent = counts.sub || 0;
  document.getElementById("ng").textContent = counts.game || 0;
  document.getElementById("no").textContent = counts.other || 0;
}

let stopAt = null;
function playRow(position) {
  current = position;
  const row = ROWS[position];
  const span = row.kind === "seg" ? ITEMS[row.index] : GAPS[row.index];
  player.currentTime = Math.max(span.start - 0.15, 0);
  stopAt = span.end + 0.15;
  player.play();
  const tr = tbody.querySelector('tr[data-pos="' + position + '"]');
  if (tr) tr.scrollIntoView({ block: "center", behavior: "smooth" });
  render();
}
player.addEventListener("timeupdate", () => {
  if (stopAt !== null && player.currentTime >= stopAt) { player.pause(); stopAt = null; }
});

function step(delta) {
  let position = current;
  for (let i = 0; i < ROWS.length; i++) {
    position += delta;
    if (position < 0 || position >= ROWS.length) return;
    const tr = tbody.querySelector('tr[data-pos="' + position + '"]');
    if (tr && !tr.classList.contains("hidden")) { playRow(position); return; }
  }
}

document.addEventListener("keydown", (event) => {
  if (event.target.tagName === "TEXTAREA" || event.target.tagName === "INPUT") return;
  const row = ROWS[current];
  if (SPEAKERS[event.key]) {
    if (row.kind === "seg") { setSpeaker(row.index, SPEAKERS[event.key]); step(1); }
    event.preventDefault();
  } else if (event.key === "Enter") {
    if (row.kind === "seg") { confirmCandidate(row.index); step(1); }
    event.preventDefault();
  } else if (event.key === "0") {
    if (row.kind === "seg") {
      state.seg[row.index] = Object.assign({}, segState(row.index),
        { speaker: "", confirmed: false });
      saveState(); render();
    }
    event.preventDefault();
  } else if (event.key === "u" || event.key === "U") {
    if (row.kind === "seg") { markUnclear(row.index); step(1); }
    event.preventDefault();
  } else if (event.key === "j" || event.key === "J") {
    step(1); event.preventDefault();
  } else if (event.key === "k" || event.key === "K") {
    step(-1); event.preventDefault();
  } else if (event.key === "r" || event.key === "R") {
    playRow(current); event.preventDefault();
  } else if (event.key === " ") {
    if (player.paused) { stopAt = null; player.play(); } else { player.pause(); }
    event.preventDefault();
  }
});

function buildJsonl() {
  const out = [];
  ITEMS.forEach((item, index) => {
    const speaker = speakerOf(index);
    if (!speaker) return;
    const keep = speaker === "host" || speaker === "sub";
    const row = { start: item.start, end: item.end, speaker: speaker,
                  text: keep ? textOf(index).trim() : "" };
    if (segState(index).unclear || row.text.indexOf("[?]") >= 0) row.unclear = true;
    if (!confirmedOf(index)) row.unconfirmed = true;
    out.push(row);
  });
  Object.keys(state.over).forEach((key) => {
    const saved = state.over[key];
    if (!saved || !saved.speaker) return;
    const keep = saved.speaker === "host" || saved.speaker === "sub";
    const row = { start: Number(saved.start), end: Number(saved.end),
                  speaker: saved.speaker, text: keep ? (saved.text || "").trim() : "",
                  overlap: true };
    if (saved.unclear || row.text.indexOf("[?]") >= 0) row.unclear = true;
    out.push(row);
  });
  Object.keys(state.gap).forEach((key) => {
    const saved = state.gap[key];
    if (!saved || !saved.used || !saved.speaker) return;
    const keep = saved.speaker === "host" || saved.speaker === "sub";
    out.push({ start: Number(saved.start), end: Number(saved.end),
               speaker: saved.speaker, text: keep ? (saved.text || "").trim() : "",
               added: true });
  });
  out.sort((a, b) => a.start - b.start);
  const lines = [];
  if (GAME_EVERYWHERE) {
    lines.push(JSON.stringify({ _meta: { game_everywhere: true } }));
  }
  out.forEach((row) => lines.push(JSON.stringify(row)));
  return lines.join("\\n") + "\\n";
}

document.getElementById("export").addEventListener("click", () => {
  document.getElementById("jsonl").value = buildJsonl();
  document.getElementById("jsonl").scrollIntoView({ behavior: "smooth" });
});
document.getElementById("download").addEventListener("click", () => {
  const blob = new Blob([buildJsonl()], { type: "application/x-ndjson" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = MATERIAL + ".jsonl";
  link.click();
});
document.getElementById("reset").addEventListener("click", () => {
  if (!confirm("自分で直した区分・本文・足した行を捨てて、候補の状態に戻します。よろしいですか?")) return;
  state = { seg: {}, gap: {}, over: {} }; saveState();
  tbody.querySelectorAll("tr.over-row").forEach((tr) => tr.remove());
  tbody.querySelectorAll("tr").forEach((tr) => {
    const row = ROWS[Number(tr.dataset.pos)];
    if (!row) return;
    if (row.kind === "seg") {
      tr.querySelector("textarea").value = ITEMS[row.index].text;
    } else {
      renderGapCells(tr, row.index);
    }
  });
  render();
});
function setFilter(name, button) {
  filter = filter === name ? "" : name;
  ["only-low", "only-unconf", "only-gap", "only-loud"].forEach((id) => {
    document.getElementById(id).dataset.active = "0";
  });
  button.dataset.active = filter === name ? "1" : "0";
  render();
}
document.getElementById("only-low").addEventListener("click",
  (e) => setFilter("low", e.target));
document.getElementById("only-unconf").addEventListener("click",
  (e) => setFilter("unconf", e.target));
document.getElementById("only-gap").addEventListener("click",
  (e) => setFilter("gap", e.target));
document.getElementById("only-loud").addEventListener("click",
  (e) => setFilter("loud", e.target));

render();
</script>
</body>
</html>
"""


# 作業ページを書く
def write_label_page(material_id, items, candidates, gaps, game_everywhere):
    folder = common.material_dir(material_id)
    audio = "analysis_source.wav"
    if not os.path.exists(os.path.join(folder, audio)):
        raise RuntimeError(f"{os.path.join(folder, audio)} がありません")
    payload = []
    for index, item in enumerate(items):
        hint = candidates.get(str(index)) or {}
        payload.append({
            "start": item["start"], "end": item["end"],
            "text": clean_text(item["text"]),
            "cand": hint.get("cand", ""), "conf": hint.get("conf", ""),
        })
    html = (_HTML
            .replace("__TITLE__", f"Split Sound 正解データ作成 — {material_id}")
            .replace("__AUDIO__", audio)
            .replace("__MATERIAL__", material_id)
            .replace("__GAME_EVERYWHERE__", "true" if game_everywhere else "false")
            .replace("__ITEMS__", json.dumps(payload, ensure_ascii=False))
            .replace("__GAPS__", json.dumps(gaps, ensure_ascii=False)))
    path = os.path.join(folder, "label.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)
    return path


def main():
    parser = argparse.ArgumentParser(
        description="正解データ作成用のローカル作業ページと下書き JSONL を作る")
    parser.add_argument("--materials", default="S1")
    parser.add_argument("--gap-sec", type=float, default=_DEFAULT_GAP_SEC, dest="gap_sec",
                        help="これ以上の空き区間を「発話を足せる行」として出す (秒)")
    parser.add_argument("--game-everywhere", action="store_true", default=None,
                        dest="game_everywhere",
                        help="全区間に環境音・ゲーム音が鳴っている素材として扱う "
                             "(省略時は C0 の無音率から自動判定)")
    parser.add_argument("--by-words", action="store_true", dest="by_words",
                        help="認識区間ではなく**語の時刻**で行を切る。"
                             "無音が多い素材 (S3 など) では区間が実発話より大幅に長いため、"
                             "これを付けないと時刻が過大になる")
    parser.add_argument("--word-gap", type=float, default=0.3, dest="word_gap",
                        help="--by-words のとき、語の間がこれ以上空いたら行を分ける (秒)")
    parser.add_argument("--gap-loud-db", type=float, default=_DEFAULT_GAP_LOUD_DB,
                        dest="gap_loud_db",
                        help="空き区間で「何か鳴っている」とみなす下限 (dBFS)")
    parser.add_argument("--min-chars", type=int, default=2, dest="min_chars",
                        help="--by-words のとき、本文がこの文字数以下の行は隣へ併合する")
    parser.add_argument("--merge-gap", type=float, default=1.0, dest="merge_gap",
                        help="短い行を隣へ併合する上限の間隔 (秒)。"
                             "これを超えて離れていれば併合しない")
    args = parser.parse_args()

    definition = common.load_json(common.materials_path())
    wanted = {x.strip() for x in args.materials.split(",") if x.strip()}
    for material in definition.get("materials", []):
        material_id = material["id"]
        if wanted and material_id not in wanted:
            continue
        items, result = load_draft_items(material_id)
        candidates, doc = load_candidates(material_id)
        duration = float(result.get("duration_sec") or 0.0)
        segments = len(items)
        if args.by_words:
            items, origins = split_items_by_words(items, args.word_gap)
            items, origins = merge_tiny_rows(
                items, origins, args.min_chars, args.merge_gap)
            candidates = remap_candidates(candidates, origins)
        gaps = find_gaps(items, duration, args.gap_sec)
        # 空き区間に音が鳴っているかを実測して添える (ページで絞り込めるようにする)
        gaps = measure_gaps(material_id, gaps, common.load_app_settings(),
                            args.gap_loud_db)

        # 自動判定: C0 で無音がほぼ出ない素材は「全区間に音が鳴っている」とみなす
        flag = args.game_everywhere
        if flag is None:
            kept = ((result.get("silence", {}).get("T1") or {}).get("kept_ratio") or 0.0)
            flag = float(kept) >= 0.98
        draft = write_draft_jsonl(material_id, items, candidates, flag)
        page = write_label_page(material_id, items, candidates, gaps, flag)
        low = sum(1 for v in candidates.values() if v.get("conf") == "low")
        gap_total = sum(g["end"] - g["start"] for g in gaps)
        covered = sum(float(i["end"]) - float(i["start"]) for i in items)
        if args.by_words:
            print(f"{material_id}: 認識区間 {segments} 件 → **語で分割して "
                  f"{len(items)} 行** (間 {args.word_gap}s 以上で切る) / "
                  f"候補 {len(candidates)} 件 (うち低確度 {low} 件) / "
                  f"game_everywhere={flag}")
        else:
            print(f"{material_id}: 区間 {len(items)} 件 / 候補 {len(candidates)} 件 "
                  f"(うち低確度 {low} 件) / game_everywhere={flag}")
        if duration:
            print(f"  行が覆う時間: {covered:.1f}s / {duration:.1f}s "
                  f"({covered / duration * 100:.1f}%)")
        print(f"  空き区間 ({args.gap_sec}s 以上): {len(gaps)} 件 / 合計 {gap_total:.1f}s")
        if doc.get("generated_by"):
            print(f"  候補の出どころ: {doc['generated_by']}")
        print(f"  作業ページ: {page}")
        print(f"  下書き    : {draft}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
