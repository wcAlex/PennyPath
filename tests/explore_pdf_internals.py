"""A guided tour of what is actually *inside* a PDF.

Both ``explore_pdfplumber.py`` and ``explore_pymupdf4llm.py`` treated the PDF
as a black box that emits text. This script opens it up. We use PyMuPDF
(``import pymupdf``) because it gives the cleanest access to the underlying
PDF objects — pages, fonts, drawings, content streams, structure tree.

By the end you should understand:

  * what the PDF format actually defines (and what it does not),
  * why text extraction on bank statements is messy,
  * why "table detection" is hard (sometimes impossible) by design,
  * what each PyMuPDF view of a page is for, and when to reach for it.

Run it:

    pip install pymupdf
    python tests/explore_pdf_internals.py
"""

import sys
import textwrap
from collections import Counter
from pathlib import Path

import pymupdf

PDF_PATH = Path(__file__).resolve().parent.parent / "data" / "statements" / "20250104-statements-0418-.pdf"

# Most discussion focuses on the first transactions page (0-indexed: 2).
ACTIVITY_PAGE = 2


def banner(title: str):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# --------------------------------------------------------------------------
# CHAPTER 1 — what the document declares about itself
# --------------------------------------------------------------------------

def chapter1_document_facts(doc):
    banner("CH 1 — Document-level facts (the PDF header & trailer)")

    print("\nEvery PDF file starts with '%PDF-X.Y' and ends with a 'trailer' that")
    print("references a 'catalog' object. From those you get:\n")

    m = doc.metadata or {}
    print(f"  PDF version (from header)        : {m.get('format', '?')}")
    print(f"  Producer                         : {(m.get('producer') or '').strip() or '(none)'}")
    print(f"  Creator                          : {m.get('creator') or '(none)'}")
    print(f"  Title / Author                   : {m.get('title') or '(none)'} / "
          f"{m.get('author') or '(none)'}")
    print(f"  Creation / Modified              : {m.get('creationDate')} / {m.get('modDate')}")
    print(f"  Encryption                       : {m.get('encryption') or 'none'}")
    print(f"  Pages                            : {doc.page_count}")
    print(f"  Has interactive form (AcroForm)  : {doc.is_form_pdf}")
    print(f"  Has saved JavaScript             : {doc.has_links() or False}")
    print(f"  Tagged / accessible structure    : "
          f"{'yes' if _has_struct_tree(doc) else 'no'}")

    print("""
What this tells us:
  * The producer is 'OpenText Output Transformation Engine' — a bulk
    statement-generation pipeline used by many banks. These engines emit
    PDFs that are visually correct but semantically opaque (see CH 5).
  * The PDF is *encrypted* (40-bit RC4) but opens without a password. That
    flavour of encryption restricts permissions (printing, copying), not
    confidentiality. PyMuPDF respects 'allow' bits but can still read text.
  * There is NO structure tree (CH 8). The document carries no machine-
    readable notion of 'heading', 'paragraph', 'table cell'. Everything
    visible is just text + lines drawn at coordinates. Reconstruction of
    structure is the consumer's problem.""")


def _has_struct_tree(doc) -> bool:
    """A tagged PDF has a /StructTreeRoot in its catalog. Probe with xref."""
    try:
        cat_xref = doc.pdf_catalog()
        return "/StructTreeRoot" in doc.xref_object(cat_xref)
    except Exception:
        return False


# --------------------------------------------------------------------------
# CHAPTER 2 — pages, geometry, and the coordinate system
# --------------------------------------------------------------------------

def chapter2_geometry(doc):
    banner("CH 2 — Pages, the coordinate system, and page geometry")

    print("""
A PDF organises pages in a 'page tree'. Each page object declares:
  * MediaBox: the physical paper rectangle, in *points* (1 pt = 1/72 inch).
  * CropBox / TrimBox / BleedBox: optional sub-rectangles for print.
  * Rotate: 0, 90, 180, or 270.
  * Resources: the fonts, images, and XObjects the page may reference.
  * Contents: one or more byte streams of drawing instructions (CH 7).

Coordinates: in raw PDF, the origin (0,0) is the BOTTOM-LEFT of the page,
y increases upward — opposite to most graphics libraries. PyMuPDF flips this
for you, so Rect coordinates and bbox tuples use a top-left origin.
""")
    for i in range(doc.page_count):
        p = doc[i]
        r = p.rect
        w_in, h_in = r.width / 72, r.height / 72
        print(f"  page {i + 1}: rect={tuple(round(v,1) for v in r)} pts  "
              f"= {w_in:.2f} x {h_in:.2f} in   rotation={p.rotation}")
    print("""
Note our pages are 522 x 1008 pts = 7.25 x 14.00 in — a custom legal-ish
size the statement printer uses, NOT US Letter (612 x 792). PDFs are
*physical-paper-first*; the tools that read them are at the mercy of
whatever paper the producer chose.""")


# --------------------------------------------------------------------------
# CHAPTER 3 — five views of the same text
# --------------------------------------------------------------------------

def chapter3_text_views(doc):
    banner("CH 3 — Five views of the same text (PyMuPDF extraction modes)")

    page = doc[ACTIVITY_PAGE]
    print(f"\nWe'll extract the same region of page {ACTIVITY_PAGE + 1} five ways.\n")

    # 3a. "text" — best-effort flat text
    raw = page.get_text("text")
    sample = "\n".join(raw.splitlines()[10:16])
    print("--- (a) get_text('text') — the simplest, what most tools wrap ---")
    print(textwrap.indent(sample, "    "))

    # 3b. "words" — every word with its bounding box and reading order
    words = page.get_text("words")
    print(f"\n--- (b) get_text('words') — {len(words)} words, each with (x0,y0,x1,y1) ---")
    print("    First 4 words on the page (x0, y0, x1, y1, text, block, line, word_no):")
    for w in words[:4]:
        print("     ", w)
    print("    Use this when you need to find text by POSITION (e.g. 'word to the\n"
          "    right of \"Account Number:\"').")

    # 3c. "blocks" — paragraph-like chunks
    blocks = page.get_text("blocks")
    print(f"\n--- (c) get_text('blocks') — {len(blocks)} blocks (paragraph-ish) ---")
    for b in blocks[:2]:
        x0, y0, x1, y1, text, bno, btype = b
        print(f"    bbox=({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}) "
              f"type={'text' if btype==0 else 'image'} -> {text[:60]!r}")
    print("    Useful for coarse region detection (header vs body).")

    # 3d. "dict" — fully structured spans with font / size / colour / bbox
    page_dict = page.get_text("dict")
    a_text_span = next(
        (span for blk in page_dict["blocks"] if blk["type"] == 0
         for line in blk["lines"] for span in line["spans"]
         if span["text"].strip()),
        None,
    )
    print("\n--- (d) get_text('dict') — full tree: blocks > lines > spans ---")
    print("    Each span carries font name, size, flags, colour, and bbox.")
    if a_text_span:
        print(f"    Example span: text={a_text_span['text']!r}")
        print(f"                  font={a_text_span['font']}  size={a_text_span['size']:.2f}"
              f"  flags={a_text_span['flags']}  bbox={a_text_span['bbox']}")
    print("    This is what pymupdf4llm runs on top of when it builds Markdown.")

    # 3e. "rawdict" — same as dict, but each span exploded to individual glyphs
    raw_dict = page.get_text("rawdict")
    a_char_span = next(
        (span for blk in raw_dict["blocks"] if blk["type"] == 0
         for line in blk["lines"] for span in line["spans"]
         if span.get("chars")),
        None,
    )
    print(f"\n--- (e) get_text('rawdict') — same shape but with per-glyph 'chars' ---")
    if a_char_span:
        print(f"    First 3 chars of one span (each has its own bbox + origin):")
        for ch in a_char_span["chars"][:3]:
            print(f"      {ch['c']!r}  bbox={tuple(round(v,1) for v in ch['bbox'])}  "
                  f"origin={tuple(round(v,1) for v in ch['origin'])}")
    print("    Use this when you need glyph-level positioning — for instance to\n"
          "    detect overprinted faux-bold (two glyphs drawn at the same origin).")


# --------------------------------------------------------------------------
# CHAPTER 4 — drawings: where "tables" come from (or do not)
# --------------------------------------------------------------------------

def chapter4_drawings(doc):
    banner("CH 4 — Drawings: lines, rectangles, and why tables are hard")

    print("""
A "table" in a PDF is not a table. It is a set of stroked or filled lines
that *visually frame* text. Some PDFs draw a full grid (one rectangle per
cell, or a lattice of horizontal + vertical strokes); others draw nothing
and rely on column alignment to imply structure. The detector you choose
must match what the producer actually drew.
""")
    for i in range(doc.page_count):
        page = doc[i]
        drawings = page.get_drawings()
        items = Counter()
        for d in drawings:
            for it in d.get("items", []):
                items[it[0]] += 1
        print(f"  page {i + 1}: {len(drawings):>3} path objects, "
              f"primitives: {dict(items) or '{}'}")
        # PDF path item codes: 'l' = line-to, 're' = rectangle, 'c' = bezier curve, 'qu' = quad.

    page = doc[ACTIVITY_PAGE]
    print(f"\nZoom in on the activity page (p.{ACTIVITY_PAGE + 1}) — the one with the\n"
          f"transaction list. Every line segment found on the page:\n")
    for d in page.get_drawings():
        for it in d.get("items", []):
            if it[0] == "l":  # ('l', p1, p2)
                p1, p2 = it[1], it[2]
                orient = ("horizontal" if abs(p1.y - p2.y) < 0.5
                          else "vertical" if abs(p1.x - p2.x) < 0.5
                          else "diagonal")
                print(f"  line  ({p1.x:6.1f},{p1.y:6.1f})  ->  ({p2.x:6.1f},{p2.y:6.1f})   "
                      f"{orient}, width={d.get('width',0):.2f}")

    print("""
Conclusion: the activity page contains a handful of horizontal rule lines
(two thick separators between header sections and a couple of thin underlines)
and ZERO vertical lines. There is no cell grid for a 'lines' table detector
to find. That is why both pdfplumber and PyMuPDF return 0 tables here with
the default 'lines' strategy — it isn't a bug, it's the PDF being honest.""")


# --------------------------------------------------------------------------
# CHAPTER 5 — fonts: why bank-statement text extraction is unreliable
# --------------------------------------------------------------------------

def chapter5_fonts(doc):
    banner("CH 5 — Fonts (and why text extraction is so noisy)")

    print("""
Each page lists the fonts it references in its /Resources/Font dictionary.
The font *type* matters a lot for text extraction:

  * Type1 / TrueType with standard encoding -> trivially extractable.
  * Type0 (composite) with a ToUnicode CMap -> extractable, including CJK.
  * Type3 -> a custom font whose glyphs are drawn with PDF operators. There
    is often NO ToUnicode mapping, so the bytes in the content stream are
    just opaque glyph indices. The extractor has to guess characters from
    glyph shapes — that's where doubled letters, '@¨¤@¤£' garbage and the
    'CChhaassee' artefact come from.
""")
    page = doc[ACTIVITY_PAGE]
    counts = Counter()
    for xref, ext, ftype, basefont, refname, encoding in page.get_fonts():
        counts[ftype] += 1
    print(f"  Fonts referenced on page {ACTIVITY_PAGE + 1} (by type):  {dict(counts)}")
    print()
    print(f"  {'name':<14} {'basefont':<14} {'type':<8} {'encoding':<20}")
    seen = set()
    for xref, ext, ftype, basefont, refname, encoding in page.get_fonts():
        if refname in seen:
            continue
        seen.add(refname)
        print(f"  {refname:<14} {basefont:<14} {ftype:<8} {encoding or '(none)':<20}")

    print("""
Look at the dominant fonts: F1..F13 are all Type3 with no encoding. Only
Helvetica (Type1, WinAnsi) extracts cleanly — and that's the small footer
text. The rest of the document is drawn in custom glyph fonts, which is
why PyMuPDF4LLM is *necessary* (not just nicer) on this statement: it has
heuristics for recovering Unicode from glyph shapes that the simpler
get_text('text') pipeline doesn't apply.""")


# --------------------------------------------------------------------------
# CHAPTER 6 — images
# --------------------------------------------------------------------------

def chapter6_images(doc):
    banner("CH 6 — Images (raster XObjects)")

    print("""
Images are stored as XObject dictionaries with a stream of pixel data. The
page's content stream references them by name ('/X32 Do' invokes XObject
X32 at the current transform). PyMuPDF lists them with format hints:
""")
    page = doc[ACTIVITY_PAGE]
    print(f"  page {ACTIVITY_PAGE + 1} references {len(page.get_images())} image XObject(s):")
    print("    (xref, smask, width, height, bpc, colorspace, alt_cs, name, filter)")
    for img in page.get_images():
        print("    ", img)
    print("""
'FlateDecode' is zlib; 'CCITTFaxDecode' is fax-style monochrome compression
(typical for logos and signature images on statements). To pull the bytes
you'd use doc.extract_image(xref) — out of scope here.""")


# --------------------------------------------------------------------------
# CHAPTER 7 — the raw content stream
# --------------------------------------------------------------------------

def chapter7_content_stream(doc):
    banner("CH 7 — The raw content stream (PDF's drawing instructions)")

    print("""
Each page's visible content is one or more byte streams of postfix-style
drawing commands. A tiny operator vocabulary covers everything:

  STATE       q  Q          push / pop graphics state
              cm             concat transform matrix (placement, scale, rotate)
              gs             apply named ExtGState (alpha, blend mode, ...)
  COLOUR      RG rg          stroke / fill RGB color
              G  g           stroke / fill gray
  PATHS       m l c re       moveto, lineto, bezier, rectangle
              S f B          stroke, fill, fill+stroke
  TEXT        BT ... ET      begin / end text object
              Tf             set font + size
              Tm             set text matrix (text origin)
              Tj  TJ         show text string / show text array
  XOBJECTS    Do             paint a referenced image or form XObject

Below is the first 1200 bytes of the activity-page stream. You should
recognise: 'q ... cm ... Do Q' (drawing images), 'm ... l S' (a line),
'BT ... /F6 6 Tf ... Tm ... (string)Tj ... ET' (text).
""")
    page = doc[ACTIVITY_PAGE]
    contents = page.read_contents()
    print(f"  stream length: {len(contents):,} bytes\n")
    text = contents[:1200].decode("latin-1", errors="replace")
    print(textwrap.indent(text, "    "))

    print("""
Important: there is NO concept of 'line of text' or 'paragraph' in this
stream. Each Tj draws a string at an absolute position chosen by the
producer's layout engine. PyMuPDF, pdfplumber, etc. recover lines by
*clustering* glyphs whose y-coordinates are close — which is why oddly
laid out statements can return text in a surprising order.""")


# --------------------------------------------------------------------------
# CHAPTER 8 — structure tree (tagged PDFs)
# --------------------------------------------------------------------------

def chapter8_structure(doc):
    banner("CH 8 — Structure tree: the part of PDF that *is* semantic")

    has_struct = _has_struct_tree(doc)
    print(f"""
A 'tagged' or 'accessible' PDF (PDF/UA) carries a /StructTreeRoot in its
catalog: a tree of nodes such as <H1>, <P>, <Table>, <TR>, <TD>, <Figure>,
each linking to the marked-content sequences in the page streams that
actually render it. THIS is where PDF carries real semantic structure —
when it exists.

This document has a structure tree? {has_struct}

It does not, because the OpenText print engine targets visual fidelity for
mailing, not accessibility. The same is true of most consumer statements
and invoices. Tagged PDFs are common for:
  * government and accessibility-regulated publications,
  * documents exported from modern Word / LibreOffice / Pages,
  * PDFs explicitly produced for screen readers.

When a tagged tree IS present, you can walk it with:
    doc.get_xml_metadata()           # XMP metadata
    doc.xref_object(doc.pdf_catalog()) # find /StructTreeRoot xref
and then traverse /K (kids) entries, reading /S (structure type) of each
node. That gives you a tree of headings, tables and rows that you can
parse without any layout heuristics.""")


# --------------------------------------------------------------------------
# CHAPTER 9 — recap
# --------------------------------------------------------------------------

def chapter9_recap():
    banner("CH 9 — Putting it together")
    print("""
What PDF *natively* supports:
  ✓ Exact page geometry, colours, transforms.
  ✓ Text drawn at arbitrary (x, y) with specified font + size.
  ✓ Vector paths (lines, rectangles, curves) and raster images.
  ✓ Optional structure tree (PDF/UA) for accessibility.
  ✓ Optional metadata, outlines (TOC), bookmarks, links, form fields.

What PDF does NOT natively give you on a typical statement:
  ✗ A 'transactions' list. Just glyphs that happen to align in rows.
  ✗ A 'table'. Just lines and text in proximity.
  ✗ A 'paragraph'. Just glyphs clustered by y-coordinate.
  ✗ A reliable Unicode mapping when Type3 fonts are used.

Practical recipe for the Phase 1A statement ingester:
  1. Open with PyMuPDF (it has the best glyph-recovery heuristics for
     ugly bank PDFs full of Type3 fonts).
  2. Use page.get_drawings() to ask 'is this a ruled table?' before
     trying find_tables() with the 'lines' strategy.
  3. For known issuers with stable layouts, pdfplumber.extract_text() +
     regex is the cheapest, most predictable parser.
  4. For unknown issuers, pymupdf4llm.to_markdown() gives an LLM a much
     richer input than raw text (preserved emphasis, table shape, image
     placeholders) at no extra parsing cost.""")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    if not PDF_PATH.exists():
        sys.exit(f"PDF not found: {PDF_PATH}")
    print(f"Opening: {PDF_PATH.name}")
    doc = pymupdf.open(PDF_PATH)
    try:
        chapter1_document_facts(doc)
        chapter2_geometry(doc)
        chapter3_text_views(doc)
        chapter4_drawings(doc)
        chapter5_fonts(doc)
        chapter6_images(doc)
        chapter7_content_stream(doc)
        chapter8_structure(doc)
        chapter9_recap()
    finally:
        doc.close()


if __name__ == "__main__":
    main()
