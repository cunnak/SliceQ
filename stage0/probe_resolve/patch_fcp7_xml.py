# -*- coding: utf-8 -*-
"""
Stage 0.6-A: Patch the FCP7 XML produced in Stage 0 so DaVinci Resolve can
import it cleanly. Two known defects from the OTIO fcp adapter output:

  DEFECT-1  duplicate <rate> block inside each <clipitem>
  DEFECT-2  empty <format/> element  -> Resolve cannot infer sequence spec

This script:
  1. backs up the original,
  2. removes duplicate sibling <rate> blocks,
  3. fills <format/> with a proper sequence spec matching source.mp4,
  4. writes SliceQ_Stage0_Test.davinci.xml (a Resolve-targeted variant),
  5. reports a diff summary of what changed.
"""
import os
import re
import shutil
import xml.etree.ElementTree as ET
from xml.dom import minidom

HERE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # .../stage0
    "test_drafts",
)
SRC = os.path.join(HERE, "SliceQ_Stage0_Test.xml")
BAK = os.path.join(HERE, "SliceQ_Stage0_Test.original.xml")
OUT = os.path.join(HERE, "SliceQ_Stage0_Test.davinci.xml")

# source.mp4 measured: 1280x720, 30fps NDF, h264/aac, 12.0s
SPEC = {
    "width": 1280,
    "height": 720,
    "timebase": 30,
    "ntsc": "FALSE",
    "duration_frames": 360,   # 12s * 30fps
    "audio_rate": 48000,
    "audio_depth": 16,
    "audio_channels": 2,
}


def build_format(indent="        "):
    """Build a standard FCP7 <format> block."""
    xml = [
        "<format>",
        "  <samplecharacteristics>",
        "    <width>%d</width>" % SPEC["width"],
        "    <height>%d</height>" % SPEC["height"],
        "    <anamorphic>FALSE</anamorphic>",
        "    <pixelaspectratio>square</pixelaspectratio>",
        "    <fielddominance>none</fielddominance>",
        "    <rate>",
        "      <timebase>%d</timebase>" % SPEC["timebase"],
        "      <ntsc>%s</ntsc>" % SPEC["ntsc"],
        "    </rate>",
        "  </samplecharacteristics>",
        "  <audio>",
        "    <samplecharacteristics>",
        "      <depth>%d</depth>" % SPEC["audio_depth"],
        "      <samplerate>%d</samplerate>" % SPEC["audio_rate"],
        "    </samplecharacteristics>",
        "    <channelcount>%d</channelcount>" % SPEC["audio_channels"],
        "  </audio>",
        "</format>",
    ]
    # reindent to parent depth
    out = []
    for ln in xml:
        stripped = ln.lstrip()
        if not stripped:
            out.append("")
            continue
        lead = len(ln) - len(stripped)
        if lead == 0:
            out.append(indent + stripped)
        else:
            out.append(indent + ("  " * (lead // 2)) + stripped)
    return "\n".join(out)


def main():
    print("=" * 70)
    print("[0.6-A] Patch FCP7 XML for DaVinci Resolve import")
    print("=" * 70)

    if not os.path.isfile(SRC):
        print("  ERROR: source XML not found:", SRC)
        return
    if not os.path.isfile(BAK):
        shutil.copy2(SRC, BAK)
        print("  backup created ->", os.path.basename(BAK))
    else:
        print("  backup already exists ->", os.path.basename(BAK))

    with open(SRC, "r", encoding="utf-8") as f:
        raw = f.read()
    original_len = len(raw)

    # ---- DEFECT-1: collapse consecutive duplicate <rate> blocks ----
    rate_block = re.compile(
        r"(<rate>\s*<timebase>\d+</timebase>\s*<ntsc>\w+</ntsc>\s*</rate>)\s*\1",
        re.MULTILINE,
    )
    raw2, n_dup = rate_block.subn(r"\1", raw)
    print("  DEFECT-1 duplicate <rate> collapsed:", n_dup, "occurrence(s)")

    # ---- DEFECT-2: fill empty <format/> ----
    n_fmt = raw2.count("<format/>")
    n_fmt2 = raw2.count("<format />")
    raw2 = raw2.replace("<format/>", build_format(indent="                        "))
    raw2 = raw2.replace("<format />", build_format(indent="                        "))
    print("  DEFECT-2 empty <format/> filled:", n_fmt + n_fmt2, "occurrence(s)")

    # ---- sanity: must still parse as XML ----
    try:
        root = ET.fromstring(raw2)
        n_items = len(root.findall(".//clipitem"))
        seq_dur = root.find(".//sequence/duration")
        print("  XML re-parse OK: clipitems =", n_items,
              "| sequence duration =", seq_dur.text if seq_dur is not None else "?")
    except ET.ParseError as e:
        print("  ERROR: patched XML does not parse:", e)
        return

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(raw2)

    print("  length:", original_len, "->", len(raw2), "bytes")
    print("  written ->", os.path.basename(OUT))

    # ---- print the patched video format section for eyeball check ----
    print("-" * 70)
    print("patched <format> block (first occurrence):")
    m = re.search(r"<format>.*?</format>", raw2, re.DOTALL)
    if m:
        for ln in m.group(0).splitlines()[:24]:
            print("   ", ln.rstrip())
    print("=" * 70)


if __name__ == "__main__":
    main()
