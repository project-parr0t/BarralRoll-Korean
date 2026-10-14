from __future__ import annotations

import hashlib
import json
import statistics
import unicodedata
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.qu2cuPen import Qu2CuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont


FONT_LAYOUT = (
    ("Barbaro/Barbaro.ufont", "Barbaro.ufont", 900),
    ("Barbaro/Barbaro_Punta.ufont", "Barbaro_Punta.ufont", 900),
    ("DIN/D-DIN.ufont", "D-DIN.ufont", 400),
    ("DIN/D-DIN-Bold.ufont", "D-DIN-Bold.ufont", 700),
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_used_codepoints(checkpoint: dict) -> set[int]:
    return {
        ord(character)
        for row in checkpoint["entries"]
        for character in row["target"]
        if not unicodedata.category(character).startswith("C")
    }


def names_snapshot(font: TTFont) -> list[tuple[int, int, int, int, str]]:
    return sorted(
        (
            record.nameID,
            record.platformID,
            record.platEncID,
            record.langID,
            record.toUnicode(),
        )
        for record in font["name"].names
    )


def metric_snapshot(font: TTFont) -> dict[str, int]:
    hhea = font["hhea"]
    os2 = font["OS/2"]
    result = {
        "head.unitsPerEm": font["head"].unitsPerEm,
        "head.xMin": font["head"].xMin,
        "head.yMin": font["head"].yMin,
        "head.xMax": font["head"].xMax,
        "head.yMax": font["head"].yMax,
        "hhea.ascent": hhea.ascent,
        "hhea.descent": hhea.descent,
        "hhea.lineGap": hhea.lineGap,
        "hhea.advanceWidthMax": hhea.advanceWidthMax,
        "hhea.minLeftSideBearing": hhea.minLeftSideBearing,
        "hhea.minRightSideBearing": hhea.minRightSideBearing,
        "hhea.xMaxExtent": hhea.xMaxExtent,
        "hhea.caretSlopeRise": hhea.caretSlopeRise,
        "hhea.caretSlopeRun": hhea.caretSlopeRun,
        "hhea.caretOffset": hhea.caretOffset,
        "OS2.xAvgCharWidth": os2.xAvgCharWidth,
        "OS2.usWeightClass": os2.usWeightClass,
        "OS2.usWidthClass": os2.usWidthClass,
        "OS2.fsSelection": os2.fsSelection,
        "OS2.ySubscriptXSize": os2.ySubscriptXSize,
        "OS2.ySubscriptYSize": os2.ySubscriptYSize,
        "OS2.ySubscriptXOffset": os2.ySubscriptXOffset,
        "OS2.ySubscriptYOffset": os2.ySubscriptYOffset,
        "OS2.ySuperscriptXSize": os2.ySuperscriptXSize,
        "OS2.ySuperscriptYSize": os2.ySuperscriptYSize,
        "OS2.ySuperscriptXOffset": os2.ySuperscriptXOffset,
        "OS2.ySuperscriptYOffset": os2.ySuperscriptYOffset,
        "OS2.yStrikeoutSize": os2.yStrikeoutSize,
        "OS2.yStrikeoutPosition": os2.yStrikeoutPosition,
        "OS2.sTypoAscender": os2.sTypoAscender,
        "OS2.sTypoDescender": os2.sTypoDescender,
        "OS2.sTypoLineGap": os2.sTypoLineGap,
        "OS2.usWinAscent": os2.usWinAscent,
        "OS2.usWinDescent": os2.usWinDescent,
    }
    for optional in ("sxHeight", "sCapHeight"):
        if hasattr(os2, optional):
            result[f"OS2.{optional}"] = getattr(os2, optional)
    return result


def outline_snapshot(font: TTFont, glyph_order: list[str]) -> dict[str, str]:
    if "glyf" in font:
        table = font["glyf"]
        return {
            name: sha256_bytes(
                table[name].compile(table, recalcBBoxes=False, optimizeSize=False)
            )
            for name in glyph_order
        }
    top = font["CFF "].cff.topDictIndex[0]
    return {
        name: sha256_bytes(bytes(top.CharStrings[name].bytecode))
        for name in glyph_order
    }


def glyph_bounds(glyph_set, glyph_name: str):
    pen = BoundsPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    return pen.bounds


def add_glyf_glyph(font: TTFont, glyph_name: str, recording, transform) -> None:
    pen = TTGlyphPen(None)
    recording.replay(TransformPen(pen, transform))
    glyph = pen.glyph()
    font["glyf"].glyphs[glyph_name] = glyph
    glyph.recalcBounds(font["glyf"])


def add_cff_glyph(
    font: TTFont,
    glyph_name: str,
    width: int,
    recording,
    transform,
) -> None:
    cff = font["CFF "].cff
    top = cff.topDictIndex[0]
    char_strings = top.CharStrings
    pen = T2CharStringPen(width, None)
    cubic_pen = Qu2CuPen(pen, max_err=0.5, all_cubic=True)
    recording.replay(TransformPen(cubic_pen, transform))
    char_string = pen.getCharString(
        private=top.Private,
        globalSubrs=cff.GlobalSubrs,
        optimize=True,
    )
    index = len(char_strings.charStringsIndex)
    char_strings.charStrings[glyph_name] = index
    char_strings.charStringsIndex.append(char_string)
    top.charset.append(glyph_name)


def add_cmap_mapping(font: TTFont, codepoint: int, glyph_name: str) -> None:
    added = False
    for table in font["cmap"].tables:
        if not table.isUnicode():
            continue
        if table.format == 4 and codepoint > 0xFFFF:
            continue
        table.cmap[codepoint] = glyph_name
        added = True
    if not added:
        raise RuntimeError(f"no_compatible_unicode_cmap:{codepoint}")


def update_legacy_tables(font: TTFont, new_metrics: dict[str, tuple[int, int]]) -> None:
    upm = font["head"].unitsPerEm
    if "LTSH" in font:
        for glyph_name in new_metrics:
            font["LTSH"].yPels[glyph_name] = 0
    if "hdmx" in font:
        for ppem, widths in font["hdmx"].hdmx.items():
            for glyph_name, (advance, _) in new_metrics.items():
                widths._map[glyph_name] = len(widths._array)
                widths._array.append(
                    min(255, max(0, round(advance * ppem / upm)))
                )


def merge_one(base_path: Path, output_path: Path, weight: int, source: TTFont, used: set[int]) -> dict:
    font = TTFont(base_path, recalcBBoxes=False, recalcTimestamp=False)
    original_order = list(font.getGlyphOrder())
    original_cmap = dict(font.getBestCmap())
    original_hmtx = {name: tuple(font["hmtx"][name]) for name in original_order}
    original_ascii = {
        codepoint: tuple(font["hmtx"][original_cmap[codepoint]])
        for codepoint in range(0x20, 0x7F)
        if codepoint in original_cmap
    }
    original_metrics = metric_snapshot(font)
    original_names = names_snapshot(font)
    original_outlines = outline_snapshot(font, original_order)

    missing = sorted(used - set(original_cmap))
    source_cmap = source.getBestCmap()
    source_missing = sorted(set(missing) - set(source_cmap))
    if source_missing:
        raise RuntimeError(f"source_font_missing_codepoints:{len(source_missing)}")

    source_glyphs = source.getGlyphSet()
    bounds = [glyph_bounds(source_glyphs, source_cmap[cp]) for cp in missing]
    nonempty = [value for value in bounds if value is not None]
    source_x_min = min(value[0] for value in nonempty)
    source_y_min = min(value[1] for value in nonempty)
    source_x_max = max(value[2] for value in nonempty)
    source_y_max = max(value[3] for value in nonempty)

    upm = font["head"].unitsPerEm
    hhea = font["hhea"]
    head = font["head"]
    margin_y = round(upm * 0.05)
    target_y_min = max(head.yMin, hhea.descent + margin_y)
    target_y_max = min(head.yMax, hhea.ascent - margin_y)
    scale_y = (target_y_max - target_y_min) / (source_y_max - source_y_min)
    y_offset = target_y_min - source_y_min * scale_y

    representative = "M" if ord("M") in original_cmap else "0"
    representative_width = font["hmtx"][original_cmap[ord(representative)]][0]
    target_advance_cap = min(
        round(upm * 0.82),
        max(round(upm * 0.75), representative_width),
    )
    margin_x = round(upm * 0.05)
    scale_x = min(
        scale_y,
        (target_advance_cap - 2 * margin_x) / (source_x_max - source_x_min),
    )

    glyph_order = list(original_order)
    new_metrics: dict[str, tuple[int, int]] = {}
    for codepoint in missing:
        source_name = source_cmap[codepoint]
        recording = DecomposingRecordingPen(source_glyphs)
        source_glyphs[source_name].draw(recording)
        bounds_value = glyph_bounds(source_glyphs, source_name)
        source_advance = source["hmtx"][source_name][0]
        if bounds_value is None:
            advance = max(1, round(source_advance * scale_x))
            x_offset = 0
        else:
            x_min, _, x_max, _ = bounds_value
            outline_width = max(0, round((x_max - x_min) * scale_x))
            proportional = round(source_advance * scale_x)
            advance = min(
                target_advance_cap,
                max(outline_width + 2 * margin_x, proportional),
            )
            x_offset = round((advance - (x_min + x_max) * scale_x) / 2)
        glyph_name = f"uni{codepoint:04X}.r5ko"
        transform = (scale_x, 0, 0, scale_y, x_offset, y_offset)
        if "glyf" in font:
            add_glyf_glyph(font, glyph_name, recording, transform)
        else:
            add_cff_glyph(font, glyph_name, advance, recording, transform)
        glyph_order.append(glyph_name)
        new_metrics[glyph_name] = (advance, 0)
        font["hmtx"][glyph_name] = (advance, 0)
        add_cmap_mapping(font, codepoint, glyph_name)

    font.setGlyphOrder(glyph_order)
    font["maxp"].numGlyphs = len(glyph_order)
    update_legacy_tables(font, new_metrics)
    os2 = font["OS/2"]
    os2.usFirstCharIndex = min(font.getBestCmap())
    os2.usLastCharIndex = min(0xFFFF, max(font.getBestCmap()))
    os2.recalcUnicodeRanges(font)
    os2.recalcCodePageRanges(font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font.save(output_path, reorderTables=False)
    font.close()

    merged = TTFont(output_path, recalcBBoxes=False, recalcTimestamp=False)
    merged_order = list(merged.getGlyphOrder())
    merged_cmap = dict(merged.getBestCmap())
    merged_metrics = metric_snapshot(merged)
    merged_names = names_snapshot(merged)
    merged_outlines = outline_snapshot(merged, original_order)
    mapping_mismatches = sum(
        merged_cmap.get(codepoint) != glyph_name
        for codepoint, glyph_name in original_cmap.items()
    )
    advance_mismatches = sum(
        tuple(merged["hmtx"][name]) != value
        for name, value in original_hmtx.items()
    )
    ascii_advance_mismatches = sum(
        tuple(merged["hmtx"][merged_cmap[codepoint]]) != value
        for codepoint, value in original_ascii.items()
    )
    outline_mismatches = sum(
        merged_outlines.get(name) != value
        for name, value in original_outlines.items()
    )
    metric_diffs = {
        key: [value, merged_metrics.get(key)]
        for key, value in original_metrics.items()
        if merged_metrics.get(key) != value
    }
    coverage_missing = sorted(used - set(merged_cmap))
    added_composite_count = 0
    broken_components: list[str] = []
    if "glyf" in merged:
        order_set = set(merged_order)
        for glyph_name in merged_order:
            glyph = merged["glyf"][glyph_name]
            if glyph.isComposite():
                if glyph_name not in original_order:
                    added_composite_count += 1
                for component in glyph.components:
                    if component.glyphName not in order_set:
                        broken_components.append(f"{glyph_name}:{component.glyphName}")
    else:
        # Drawing every CFF glyph proves that all local/global subroutine
        # references and newly generated charstrings can be interpreted.
        glyph_set = merged.getGlyphSet()
        for glyph_name in merged_order:
            pen = BoundsPen(glyph_set)
            glyph_set[glyph_name].draw(pen)

    if not all(
        (
            mapping_mismatches == 0,
            advance_mismatches == 0,
            ascii_advance_mismatches == 0,
            outline_mismatches == 0,
            not metric_diffs,
            merged_names == original_names,
            not coverage_missing,
            not broken_components,
            added_composite_count == 0,
        )
    ):
        raise RuntimeError(f"merged_font_validation_failed:{base_path.name}")

    added_advances = [merged["hmtx"][merged_cmap[cp]][0] for cp in missing]
    result = {
        "file": output_path.name,
        "format": "TrueType-glyf" if "glyf" in merged else "OpenType-CFF",
        "weight_instance": weight,
        "base_bytes": base_path.stat().st_size,
        "base_sha256": sha256_file(base_path),
        "merged_bytes": output_path.stat().st_size,
        "merged_sha256": sha256_file(output_path),
        "original_glyphs": len(original_order),
        "added_glyphs": len(missing),
        "merged_glyphs": len(merged_order),
        "used_codepoints": len(used),
        "used_codepoints_covered": len(used) - len(coverage_missing),
        "coverage_missing": len(coverage_missing),
        "preexisting_mapping_mismatches": mapping_mismatches,
        "preexisting_advance_mismatches": advance_mismatches,
        "ascii_digit_advance_mismatches": ascii_advance_mismatches,
        "preexisting_outline_mismatches": outline_mismatches,
        "metric_diffs": metric_diffs,
        "internal_names_preserved": merged_names == original_names,
        "broken_composite_references": len(broken_components),
        "added_composite_glyphs": added_composite_count,
        "scale_x": round(scale_x, 6),
        "scale_y": round(scale_y, 6),
        "target_y_range": [target_y_min, target_y_max],
        "added_advance_min": min(added_advances),
        "added_advance_median": statistics.median(added_advances),
        "added_advance_max": max(added_advances),
        "structural_reload_and_draw": "pass",
        "gate": "pass",
    }
    merged.close()
    return result


def source_metadata(source: TTFont, source_font: Path) -> dict:
    names: dict[str, list[str]] = {}
    for record in source["name"].names:
        if record.nameID not in (0, 1, 2, 3, 4, 5, 6, 13, 14):
            continue
        value = record.toUnicode()
        bucket = names.setdefault(str(record.nameID), [])
        if value not in bucket:
            bucket.append(value)
    return {
        "family": names.get("1", [""])[0],
        "version": names.get("5", [""])[0],
        "copyright": names.get("0", [""])[0],
        "license": "SIL Open Font License 1.1",
        "license_url": names.get("14", [""])[0],
        "bytes": source_font.stat().st_size,
        "sha256": sha256_file(source_font),
        "source_kind": "release-asset-Noto-variable-TrueType",
    }


def merge_fonts(
    *,
    base_root: Path,
    output_root: Path,
    source_font: Path,
    checkpoint: dict,
) -> dict:
    if not source_font.is_file():
        raise RuntimeError("noto_source_missing")
    used = load_used_codepoints(checkpoint)
    source_variable = TTFont(source_font, recalcTimestamp=False)
    source_cmap = source_variable.getBestCmap()
    if used - set(source_cmap):
        raise RuntimeError("noto_source_does_not_cover_used_codepoints")
    metadata = source_metadata(source_variable, source_font)
    static_sources = {
        weight: instantiateVariableFont(
            TTFont(source_font, recalcTimestamp=False),
            {"wght": weight},
            inplace=False,
            optimize=True,
        )
        for weight in sorted({spec[2] for spec in FONT_LAYOUT})
    }
    results = [
        merge_one(
            base_root / relative,
            output_root / output_name,
            weight,
            static_sources[weight],
            used,
        )
        for relative, output_name, weight in FONT_LAYOUT
    ]
    for font in static_sources.values():
        font.close()
    source_variable.close()
    status_counts = {
        status: sum(row["status"] == status for row in checkpoint["entries"])
        for status in ("reviewed", "source_preserved")
    }
    report = {
        "schema": "parr0t.r5.font-merge/v1",
        "tool": {
            "name": "fontTools",
            "version": __import__("fontTools").version,
        },
        "source": metadata,
        "license_file": {
            "path": "OFL-1.1.txt",
        },
        "translation_input": {
            "rows": len(checkpoint["entries"]),
            "statuses": status_counts,
            "used_codepoints": len(used),
            "hangul_codepoints": sum(0xAC00 <= cp <= 0xD7A3 for cp in used),
        },
        "fonts": results,
        "gate": "pass" if all(item["gate"] == "pass" for item in results) else "fail",
    }
    return report
