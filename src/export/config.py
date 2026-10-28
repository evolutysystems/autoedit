# Resolve 出力の設定読み出し (resolve20 §6)
# ハードコード回避のため setting.json の export セクションを参照する。
# _merge_with_defaults はセクション単位の浅いマージのため、入れ子キーの欠落は
# ここで安全に既定へフォールバックする (archive/config.py と同型)。


# export.resolve を1つの辞書へ平坦化して返す
def resolve_config(settings):
    export = settings.get("export", {}) if isinstance(settings, dict) else {}
    resolve = export.get("resolve", {}) if isinstance(export, dict) else {}
    title = resolve.get("title", {}) if isinstance(resolve, dict) else {}
    subtitle = resolve.get("subtitle", {}) if isinstance(resolve, dict) else {}
    return {
        # 出力ボタンの表示 (false でボタン非表示 = 完全後方互換)
        "enabled": bool(resolve.get("enabled", True)),
        # 出力形式 ("fcpxml" のみ実装。"edl_srt"/"otio" は将来拡張 / §11-5)
        "format": str(resolve.get("format", "fcpxml") or "fcpxml"),
        "fcpxml_version": str(resolve.get("fcpxml_version", "1.9") or "1.9"),
        # FCPXML の event 名 (Resolve 取り込み時の分類名)
        "event_name": str(resolve.get("event_name", "Stretheus") or "Stretheus"),
        # Text+ として解釈させる effect UID (空= Basic Title 相当へフォールバック / §10-9)
        "title_effect_uid": str(resolve.get("title_effect_uid", "") or ""),
        # クリップ用の編集点取得方式 ("context"=PipelineContext 保持値 / §10-3 確定)
        "clip_edit_points": str(resolve.get("clip_edit_points", "context") or "context"),
        # アーカイブのイントロ/タグ演出をテキストとして出力へ含めるか (§5.4)
        "include_intro_card": bool(resolve.get("include_intro_card", True)),
        # Timeline 経路のクリップ配置 (resolve5 §19-4)
        # "preserve"=Timeline 上の位置をそのまま出力 (空きは gap) / "pack"=先頭から詰める (従来)
        "timeline_layout": str(resolve.get("timeline_layout", "preserve") or "preserve"),
        # V1 の拡大率・表示位置を adjust-transform として書くか (ver9 resolve6 §4.8)。
        # 既定 true。Resolve 側の解釈が食い違ったときに false で止められるようにしてある
        # (false なら従来と同じ FCPXML になる)。
        "write_base_transform": bool(resolve.get("write_base_transform", True)),
        # クリップ用出力のファイル名接頭辞 (空文字なら接頭辞なし)
        "clip_prefix": str(resolve.get("clip_prefix", "clip") or ""),
        # 位置変換の係数 (§5.5)
        "title": {
            # "normalized"=±1 正規化座標 / "pixel"=キャンバス px
            "coord_space": str(title.get("coord_space", "normalized") or "normalized"),
            # Resolve 座標の原点 ("center"=画面中央)
            "origin": str(title.get("origin", "center") or "center"),
        },
        # 字幕の出力方式 (resolve21 §5.4-§5.6)
        "subtitle": {
            # "caption"=字幕トラック (既定) / "title"=Text+ / "both"=両方
            "mode": str(subtitle.get("mode", "caption") or "caption"),
            # caption の書式 (実装は "ITT" のみ。将来 CEA-608)
            "caption_format": str(subtitle.get("caption_format", "ITT") or "ITT"),
            # caption ロールの言語コード (iTT?captionFormat=ITT.{language})
            "language": str(subtitle.get("language", "ja") or "ja"),
            # SRT サイドカーを併せて出力するか
            "srt_sidecar": bool(subtitle.get("srt_sidecar", True)),
            # 先頭 0 秒へ空白字幕を 1 件足すか (resolve5 §19-2c)。
            # 取り込み側が「先頭の空きを詰めて最初の字幕を 0 秒へ寄せる」場合の保険。
            "lead_anchor": bool(subtitle.get("lead_anchor", False)),
            # 先頭アンカーの本文。空文字だと取り込み時に捨てられるため既定は全角空白。
            "lead_anchor_text": str(subtitle.get("lead_anchor_text", "　") or "　"),
        },
    }
