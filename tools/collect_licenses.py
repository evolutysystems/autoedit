# 同梱物のライセンス表記を licenses/ へ集める (ver5 resolve2 §5.10)
#
# 作られた licenses/ は、リリース手順で dist/Stretheus/ へそのままコピーし、
# インストールフォルダの直下 (exe と同じ階層) へ置く。_internal/ の下ではない。
# 利用者が開いて読む場所であるため、PyInstaller の datas には入れない (§5.10.2)。
#
#   python tools/collect_licenses.py            licenses/ を作り直す
#   python tools/collect_licenses.py --check    不足があれば終了コード 1 (リリース前の確認用)
#   python tools/collect_licenses.py --scan-bundle
#       凍結配布 (src/dist/Stretheus/_internal) を調べ、manifest に載っていない
#       同梱物を報告する。依存を増やしたときの取りこぼし防止。
#
# ライセンス全文は**配布元のファイルをそのまま**入れる。翻訳も要約もしない。
# pip 配布物は dist-info に入っている全文をコピーし、そこに無いもの
# (Qt / FFmpeg / Python / モデル) だけ tools/license_texts/ から持ってくる。
import argparse
import importlib.metadata as metadata
import json
import os
import re
import shutil
import sys

# リポジトリのルート (tools/ の 1 つ上)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MANIFEST = os.path.join(_ROOT, "tools", "license_manifest.json")
_TEXT_DIR = os.path.join(_ROOT, "tools", "license_texts")
_OUT_DIR = os.path.join(_ROOT, "licenses")
_BUNDLE_DIR = os.path.join(_ROOT, "src", "dist", "Stretheus", "_internal")

# dist-info の中でライセンス全文とみなすファイル名 (大文字小文字は無視する)
_LICENSE_PATTERN = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)
# 収集しない拡張子 (キャッシュや実体のないもの)
_SKIP_SUFFIXES = (".pyc", ".py")

# 依存関係をたどるときに無視する配布物 (ビルド専用 / 実行時には同梱されない)
_BUILD_ONLY = {"pip", "setuptools", "wheel", "pyinstaller", "pyinstaller-hooks-contrib"}


# ------------------------------------------------------------------
# manifest
# ------------------------------------------------------------------

def _load_manifest():
    with open(_MANIFEST, encoding="utf-8") as handle:
        return json.load(handle)


# 配布物名の表記ゆれ (大文字小文字 / - と _) を吸収する鍵
def _key(name):
    return re.sub(r"[-_.]+", "-", str(name or "")).strip().lower()


# ------------------------------------------------------------------
# 同梱する pip 配布物の割り出し
# ------------------------------------------------------------------

# インストール済み配布物を「正規化した名前 → Distribution」で引けるようにする
def _installed():
    found = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            found.setdefault(_key(name), dist)
    return found


# Requires-Dist から依存先の配布物名だけを取り出す
# 環境マーカー付き (extra == "dev" など) は同梱されないため落とす。
def _requirements(dist):
    names = []
    for raw in (dist.requires or []):
        if ";" in raw:
            requirement, marker = raw.split(";", 1)
            if "extra" in marker:
                continue        # 追加機能用の依存は入れない
        else:
            requirement = raw
        name = re.split(r"[\s\[<>=!~(]", requirement.strip(), 1)[0]
        if name:
            names.append(name)
    return names


# roots から依存をたどり、同梱される配布物の一覧を作る
def _closure(roots, extra, installed):
    seen = {}
    missing = []
    queue = list(roots) + list(extra)
    # 明示的に指定したもの (roots / python_extra) だけは、見つからなければ報告する
    roots_keys = {_key(name) for name in queue}
    while queue:
        name = queue.pop(0)
        key = _key(name)
        if key in seen or key in _BUILD_ONLY:
            continue
        dist = installed.get(key)
        if dist is None:
            # インストールされていない依存 = 環境条件で外れたもの (古い Python 向けの
            # backports など)。凍結配布にも入らないため、未収録として数えない。
            if key in roots_keys:
                missing.append(name)
            seen[key] = None
            continue
        seen[key] = dist
        queue.extend(_requirements(dist))
    return {k: v for k, v in seen.items() if v is not None}, missing


# ------------------------------------------------------------------
# ライセンス全文の取り出し
# ------------------------------------------------------------------

# 配布物に同梱されているライセンス関連ファイルを (出力名, 中身) の列で返す
def _license_texts(dist):
    texts = []
    for entry in (dist.files or []):
        name = str(entry)
        if not _LICENSE_PATTERN.search(name) or name.endswith(_SKIP_SUFFIXES):
            continue
        try:
            body = entry.read_text(encoding="utf-8")
        except Exception:                     # noqa: BLE001 (読めないものは飛ばす)
            continue
        if not (body or "").strip():
            continue
        texts.append((os.path.basename(name), body))
    return texts


# manifest の text 指定 ("spdx:..." / "file:...") を全文へ解決する
def _resolve_text(spec):
    if not spec:
        return None
    if spec.startswith("spdx:"):
        path = os.path.join(_TEXT_DIR, spec[len("spdx:"):] + ".txt")
    elif spec.startswith("file:"):
        path = os.path.join(_ROOT, spec[len("file:"):])
    else:
        path = os.path.join(_ROOT, spec)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# 配布物のライセンス名 (メタデータ由来。無ければ分類子から拾う)
def _license_name(dist):
    expression = dist.metadata.get("License-Expression")
    if expression:
        return expression.strip()
    for classifier in dist.metadata.get_all("Classifier") or []:
        if classifier.startswith("License :: "):
            return classifier.rsplit("::", 1)[-1].strip()
    value = (dist.metadata.get("License") or "").strip()
    # 全文がそのまま License に入っている配布物があるため 1 行目だけ使う
    return value.splitlines()[0].strip() if value else ""


# ------------------------------------------------------------------
# 書き出し
# ------------------------------------------------------------------

# 1 コンポーネントぶんのフォルダを書く。戻り値は README 用の情報。
def _write_component(out_dir, entry):
    os.makedirs(out_dir, exist_ok=True)
    for filename, body in entry["texts"]:
        with open(os.path.join(out_dir, filename), "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(body)
    return entry


# README.txt (日本語の一覧) を書く。メモ帳で開けるよう UTF-8 BOM + CRLF にする。
def _write_readme(entries, unresolved):
    lines = [
        "Stretheus に同梱している第三者ソフトウェアのライセンス",
        "",
        "このフォルダには、Stretheus が利用している第三者ソフトウェアの",
        "ライセンス全文を収録しています。各ソフトウェアの著作権は",
        "それぞれの権利者に帰属します。",
        "",
        "このファイルは tools/collect_licenses.py が生成しています。",
        "内容を直したい場合は tools/license_manifest.json を編集してください。",
        "",
        "=" * 72,
        "",
    ]
    for entry in entries:
        lines.append(f"[{entry['name']}]" + (f"  {entry['usage']}" if entry.get("usage") else ""))
        if entry.get("version"):
            lines.append(f"  版         : {entry['version']}")
        if entry.get("url"):
            lines.append(f"  配布元     : {entry['url']}")
        if entry.get("source_url"):
            lines.append(f"  ソース     : {entry['source_url']}")
        lines.append(f"  ライセンス : {entry.get('license') or '(下記ファイルを参照)'}")
        files = " / ".join(f"{entry['id']}\\{name}" for name, _body in entry["texts"])
        lines.append(f"  全文       : {files}")
        for index, note in enumerate(entry.get("note") or []):
            label = "  備考       : " if index == 0 else "               "
            lines.append(label + note)
        if entry.get("verify_on_bundle"):
            lines.append("  ※ 同梱物を更新したときは、配布元の LICENSE / NOTICE を")
            lines.append("     そのまま上書きしてください。")
        lines.append("")

    if unresolved:
        lines += [
            "=" * 72,
            "",
            "【未収録】次の同梱物はライセンス全文を取得できませんでした。",
            "リリース前に tools/license_manifest.json へ出どころを追加してください。",
            "",
        ]
        lines += [f"  - {name}" for name in unresolved]
        lines.append("")

    path = os.path.join(_OUT_DIR, "README.txt")
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write("\n".join(lines))
    return path


# ------------------------------------------------------------------
# 本体
# ------------------------------------------------------------------

# manifest とインストール済み配布物から、書き出す内容を組み立てる
def collect():
    manifest = _load_manifest()
    installed = _installed()
    overrides = {_key(k): v for k, v in (manifest.get("python_overrides") or {}).items()}

    dists, missing = _closure(
        manifest.get("python_roots") or [], manifest.get("python_extra") or [], installed)

    entries = []
    unresolved = list(missing)

    # pip 配布物
    for key in sorted(dists):
        dist = dists[key]
        name = dist.metadata["Name"]
        override = overrides.get(key) or {}
        texts = _license_texts(dist)
        forced = _resolve_text(override.get("text"))
        if forced is not None:
            # 配布物が全文を持たない (Qt など) ため、こちらで用意した全文を使う
            texts = [("LICENSE.txt", forced)] + [t for t in texts if t[0] != "LICENSE.txt"]
        if not texts:
            unresolved.append(f"{name} (全文が見つかりません)")
            continue
        entries.append({
            "id": name,
            "name": name,
            "version": dist.metadata["Version"],
            "url": _project_url(dist),
            "license": override.get("license") or _license_name(dist),
            "usage": override.get("usage", ""),
            "note": _as_list(override.get("note")),
            "texts": texts,
        })

    # pip 以外 (FFmpeg / Python / Qt / モデル)
    for component in manifest.get("components") or []:
        body = _resolve_text(component.get("text"))
        if body is None:
            unresolved.append(f"{component.get('name')} (全文が見つかりません: {component.get('text')})")
            continue
        entries.append({
            "id": component["id"],
            "name": component.get("name") or component["id"],
            "version": component.get("version", ""),
            "url": component.get("url", ""),
            "source_url": component.get("source_url", ""),
            "license": component.get("license", ""),
            "usage": component.get("usage", ""),
            "note": _as_list(component.get("note")),
            "verify_on_bundle": bool(component.get("verify_on_bundle")),
            "texts": [("LICENSE.txt", body)],
        })

    entries.sort(key=lambda e: e["name"].lower())
    return entries, unresolved


def _as_list(value):
    if not value:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [str(value)]


# 配布物のホームページ URL (メタデータの表記ゆれを吸収する)
def _project_url(dist):
    home = (dist.metadata.get("Home-page") or "").strip()
    if home:
        return home
    for raw in dist.metadata.get_all("Project-URL") or []:
        label, _sep, url = raw.partition(",")
        if label.strip().lower() in ("homepage", "source", "repository", "documentation"):
            return url.strip()
    return ""


# licenses/ を作り直す (既存の中身は毎回捨てる = 消えた依存が残らない)
def write(entries, unresolved):
    if os.path.isdir(_OUT_DIR):
        shutil.rmtree(_OUT_DIR)
    os.makedirs(_OUT_DIR, exist_ok=True)
    for entry in entries:
        _write_component(os.path.join(_OUT_DIR, entry["id"]), entry)
    return _write_readme(entries, unresolved)


# 凍結配布に入っているのに manifest から漏れている同梱物を報告する
def scan_bundle(entries):
    if not os.path.isdir(_BUNDLE_DIR):
        print(f"凍結配布が見つかりません (先にビルドしてください): {_BUNDLE_DIR}")
        return []
    known = {_key(e["id"]) for e in entries}
    packages = metadata.packages_distributions()
    unknown = []
    for name in sorted(os.listdir(_BUNDLE_DIR)):
        path = os.path.join(_BUNDLE_DIR, name)
        if not os.path.isdir(path) or name.endswith(".dist-info"):
            continue
        if name.startswith("_") or name in ("src", "ffmpeg") or name.endswith(".libs"):
            continue
        dists = packages.get(name) or [name]
        if not any(_key(d) in known for d in dists):
            unknown.append(name)
    return unknown


def main(argv=None):
    parser = argparse.ArgumentParser(description="同梱物のライセンス表記を licenses/ へ集める")
    parser.add_argument("--check", action="store_true",
                        help="書き出さずに不足だけを報告する (不足があれば終了コード 1)")
    parser.add_argument("--scan-bundle", action="store_true",
                        help="凍結配布を調べ、manifest に無い同梱物を報告する")
    args = parser.parse_args(argv)

    entries, unresolved = collect()

    if args.scan_bundle:
        unknown = scan_bundle(entries)
        if unknown:
            print("manifest に載っていない同梱物:")
            for name in unknown:
                print(f"  - {name}")
        else:
            print("凍結配布の同梱物はすべて manifest に載っています。")

    if args.check:
        print(f"収録対象: {len(entries)} 件")
        for name in unresolved:
            print(f"  未収録: {name}")
        return 1 if unresolved else 0

    readme = write(entries, unresolved)
    print(f"licenses/ を更新しました: {len(entries)} 件")
    for name in unresolved:
        print(f"  未収録: {name}")
    print(f"  一覧: {readme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
