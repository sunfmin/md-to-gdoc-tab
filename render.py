#!/usr/bin/env python3
"""Render a local Markdown file into a specific Google Doc tab, idempotently.

Strategy: clear the target tab body, then rebuild it from the Markdown source
in one batchUpdate call (deleteContentRange + insertText + updateParagraphStyle
+ createParagraphBullets). Re-running with the same Markdown is a no-op (the
doc converges on the markdown content).

Nesting trick: createParagraphBullets infers each paragraph's bullet level
from the count of LEADING TAB CHARACTERS in the inserted text. The API
consumes the tabs after assigning levels. So we just prefix sub-items with
`\\t` per level and let the API handle it — this is the only reliable way
to get true nestingLevel != 0 (and therefore the ◦/■ glyphs from
BULLET_DISC_CIRCLE_SQUARE).

Limitations:
- Replaces the entire target tab body — any in-doc comments/suggestions on
  that tab are lost (the rest of the doc, including other tabs, is untouched).
- Markdown subset supported: `# TITLE`, `## H1`, `### H2`, paragraphs,
  unordered bullets (`- item`), nested bullets (indent in multiples of 4
  spaces or one tab per level). No tables, images, code blocks, links, or
  inline formatting (bold/italic) — these can be added later.
- YAML frontmatter (`---\\n...\\n---\\n` at file top) is stripped before parsing.

Prerequisite: `gws` CLI authenticated. Run `gws auth login` if `gws auth
status` shows `token_valid: false`.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


# --- Markdown parsing -----------------------------------------------------

# Block tuples:
#   ('title', text)
#   ('h1', text)        -> Google "HEADING_1"
#   ('h2', text)        -> Google "HEADING_2"
#   ('para', text)
#   ('bullet', level, text)
#   ('table', rows)     -> rows is list[list[str]]; rows[0] is the header.
#                          Rendered as a real Google Docs table. Only supported
#                          as the LAST block (see render_table / main).

def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _is_table_separator(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.match(r"^:?-+:?$", c) for c in cells)


def _split_table_cells(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + len("\n---\n"):]


def parse_markdown(md: str):
    blocks = []
    para_buf: list[str] = []
    table_buf: list[list[str]] = []

    def flush_para():
        if para_buf:
            blocks.append(("para", " ".join(para_buf).strip()))
            para_buf.clear()

    def flush_table():
        if table_buf:
            blocks.append(("table", [r[:] for r in table_buf]))
            table_buf.clear()

    for raw in md.split("\n"):
        line = raw.rstrip()

        # GFM table rows: accumulate contiguous `| ... |` lines into one block.
        # The `| --- | --- |` separator row is recognised and dropped.
        if _is_table_row(line):
            flush_para()
            if not _is_table_separator(line):
                table_buf.append(_split_table_cells(line))
            continue
        else:
            flush_table()

        m = re.match(r"^# (.+)$", line)
        if m:
            flush_para(); blocks.append(("title", m.group(1).strip())); continue
        m = re.match(r"^## (.+)$", line)
        if m:
            flush_para(); blocks.append(("h1", m.group(1).strip())); continue
        m = re.match(r"^### (.+)$", line)
        if m:
            flush_para(); blocks.append(("h2", m.group(1).strip())); continue

        # Horizontal rule: skip entirely
        if re.match(r"^---+$", line):
            flush_para(); continue

        # Bullet (with optional indent)
        m = re.match(r"^([ \t]*)- (.+)$", line)
        if m:
            flush_para()
            indent = m.group(1)
            # Tabs count as 1 level each; spaces grouped by 4
            level = indent.count("\t") + (len(indent.replace("\t", "")) // 4)
            blocks.append(("bullet", level, m.group(2).strip()))
            continue

        if not line:
            flush_para(); continue

        para_buf.append(line)

    flush_para()
    flush_table()
    return blocks


# --- batchUpdate request construction -------------------------------------

STYLE_MAP = {"title": "TITLE", "h1": "HEADING_1", "h2": "HEADING_2"}


def text_for(block) -> str:
    """Text for a block, including its leading tabs (for bullets) but NOT
    the trailing newline. Caller appends \\n."""
    kind = block[0]
    if kind == "bullet":
        _, level, text = block
        return ("\t" * level) + text
    return block[1]


def build_requests(blocks, tab_id, insert_at, body_end_existing):
    """Construct the batchUpdate request list.

    Args:
      blocks: parsed Markdown blocks.
      tab_id: target tab.
      insert_at: index inside the tab where the body starts (always 1 — the
        tab's leading SectionBreak occupies [0, 1)).
      body_end_existing: endIndex of the current body's last element. We
        delete [insert_at, body_end_existing - 1) to clear, leaving the
        single residual \\n at body_end_existing - 1.
    """
    requests = []

    # 1. Clear existing body (if non-trivial).
    if body_end_existing - insert_at > 1:
        requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": insert_at,
                    "endIndex": body_end_existing - 1,
                    "tabId": tab_id,
                }
            }
        })

    # 2. Build content. Each block contributes "<text>\n". This causes one
    # trailing empty paragraph at the end of the tab (cosmetic; acceptable).
    pieces = [text_for(b) + "\n" for b in blocks]
    insert_text = "".join(pieces)
    requests.append({
        "insertText": {
            "location": {"index": insert_at, "tabId": tab_id},
            "text": insert_text,
        }
    })

    # 2b. Wipe any inherited bullet attributes across the newly inserted
    # content. After deleteContentRange leaves a residual final newline, that
    # paragraph keeps its old bullet metadata; the just-inserted text inherits
    # it because the insertion target paragraph is the residual one. Clear
    # everything, then re-apply bullets selectively in step 5.
    final_end = insert_at + len(insert_text) + 1  # +1 for residual \n
    requests.append({
        "deleteParagraphBullets": {
            "range": {"startIndex": insert_at, "endIndex": final_end - 1, "tabId": tab_id},
        }
    })

    # 3. Compute absolute range of each block-paragraph in the doc after
    # insertion (delete happens first, so insert_at is 1 and indices line up).
    offsets = []
    cursor = insert_at
    for piece in pieces:
        offsets.append((cursor, cursor + len(piece)))  # [start, end)
        cursor += len(piece)

    # 4. updateParagraphStyle for headings & title.
    for (start, end), block in zip(offsets, blocks):
        named = STYLE_MAP.get(block[0])
        if named:
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end, "tabId": tab_id},
                    "paragraphStyle": {"namedStyleType": named},
                    "fields": "namedStyleType",
                }
            })

    # 5. createParagraphBullets per contiguous bullet group. Leading tabs in
    # the inserted text drive nesting level; the API consumes them.
    #
    # Range trick: end at last_bullet_start + 1 (one char inside the LAST
    # bullet) instead of last_bullet_end. Google's createParagraphBullets
    # is unexpectedly inclusive at the upper boundary — using last_bullet_end
    # (= start of the next paragraph) also bullets that next paragraph if
    # it's NORMAL_TEXT. Headings happen to be filtered, which masks the bug
    # in most cases; only a bullet-group-followed-by-paragraph triggers it.
    # Ending at last_bullet_start + 1 covers all bullets in the group via
    # paragraph overlap while staying strictly inside the last bullet.
    group_first_start = None
    last_bullet_start = None
    for (start, end), block in zip(offsets, blocks):
        if block[0] == "bullet":
            if group_first_start is None:
                group_first_start = start
            last_bullet_start = start
        else:
            if group_first_start is not None:
                requests.append({
                    "createParagraphBullets": {
                        "range": {"startIndex": group_first_start, "endIndex": last_bullet_start + 1, "tabId": tab_id},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                })
                group_first_start = None
    if group_first_start is not None:
        requests.append({
            "createParagraphBullets": {
                "range": {"startIndex": group_first_start, "endIndex": last_bullet_start + 1, "tabId": tab_id},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }
        })

    return requests


# --- gws CLI helpers ------------------------------------------------------

def _gws_json(args: list[str]):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"gws failed (rc={r.returncode})\n")
        sys.stderr.write(f"STDERR: {r.stderr}\n")
        sys.stderr.write(f"STDOUT: {r.stdout}\n")
        sys.exit(r.returncode)
    out = r.stdout
    # Strip leading "Using keyring backend: keyring" line if present.
    if out.startswith("Using keyring"):
        out = out.split("\n", 1)[1] if "\n" in out else out
    return json.loads(out)


def _gws_batch(doc_id: str, requests: list):
    return _gws_json([
        "gws", "docs", "documents", "batchUpdate",
        "--params", json.dumps({"documentId": doc_id}),
        "--json", json.dumps({"requests": requests}),
    ])


def find_tab(tabs, target_id):
    for t in tabs:
        if t.get("tabProperties", {}).get("tabId") == target_id:
            return t
        for ct in t.get("childTabs", []) or []:
            r = find_tab([ct], target_id)
            if r:
                return r
    return None


def fetch_tab(doc_id: str, tab_id: str):
    """Return the full tab dict for the given tab (with content)."""
    doc = _gws_json([
        "gws", "docs", "documents", "get",
        "--params", json.dumps({"documentId": doc_id, "includeTabsContent": True}),
    ])
    tab = find_tab(doc.get("tabs", []), tab_id)
    if tab is None:
        sys.exit(f"Tab {tab_id!r} not found in doc {doc_id}")
    return tab


def fetch_tab_body_range(doc_id: str, tab_id: str):
    """Return (insert_at, body_end) for the given tab. insert_at is 1
    (tab's SectionBreak occupies [0,1)). body_end is the endIndex of the
    last content element in the tab body."""
    tab = fetch_tab(doc_id, tab_id)
    body = tab["documentTab"]["body"]["content"]
    end_indices = [el["endIndex"] for el in body if "endIndex" in el]
    return 1, max(end_indices)


# --- table rendering (multi-phase) ----------------------------------------

TABLE_COL_WIDTH_PT = 250  # matches the Template tab's table columns


def render_table(doc_id: str, tab_id: str, rows):
    """Append `rows` (list[list[str]], rows[0] = header) as a real Google
    Docs table at the end of the tab body.

    Multi-phase because a table's cell indices aren't knowable until the
    table exists in the doc:
      1. insertTable (empty N x M) at the end of the tab segment.
      2. Re-fetch -> read each cell's content start index.
      3. Set fixed column widths + insert each cell's text in DESCENDING
         index order (so earlier insertions don't shift later cells).
      4. Re-fetch -> bold the header row over its now-final indices.
    """
    nrows = len(rows)
    ncols = max(len(r) for r in rows)

    # Phase 1: empty table at end of segment (placement needs no index math).
    _gws_batch(doc_id, [{
        "insertTable": {
            "endOfSegmentLocation": {"tabId": tab_id},
            "rows": nrows,
            "columns": ncols,
        }
    }])

    # Phase 2: locate the (last) table and each cell's content start index.
    table_el = _last_table(fetch_tab(doc_id, tab_id))
    table_start = table_el["startIndex"]
    cells = []  # (content_start_index, text)
    for ri, row in enumerate(table_el["table"]["tableRows"]):
        for ci, cell in enumerate(row["tableCells"]):
            cstart = cell["content"][0]["startIndex"]
            text = rows[ri][ci] if ci < len(rows[ri]) else ""
            cells.append((cstart, text))

    # Phase 3: column widths, then cell text descending (indices stay valid).
    reqs = [{
        "updateTableColumnProperties": {
            "tableStartLocation": {"index": table_start, "tabId": tab_id},
            "columnIndices": [ci],
            "tableColumnProperties": {
                "widthType": "FIXED_WIDTH",
                "width": {"magnitude": TABLE_COL_WIDTH_PT, "unit": "PT"},
            },
            "fields": "widthType,width",
        }
    } for ci in range(ncols)]
    for cstart, text in sorted(cells, key=lambda c: -c[0]):
        if text:
            reqs.append({
                "insertText": {"location": {"index": cstart, "tabId": tab_id}, "text": text}
            })
    _gws_batch(doc_id, reqs)

    # Phase 4: bold the header row (re-fetch for post-insert indices).
    header = _last_table(fetch_tab(doc_id, tab_id))["table"]["tableRows"][0]
    bold = []
    for ci, cell in enumerate(header["tableCells"]):
        text = rows[0][ci] if ci < len(rows[0]) else ""
        if not text:
            continue
        cstart = cell["content"][0]["startIndex"]
        bold.append({
            "updateTextStyle": {
                "range": {"startIndex": cstart, "endIndex": cstart + len(text), "tabId": tab_id},
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        })
    if bold:
        _gws_batch(doc_id, bold)
    return nrows, ncols


def _last_table(tab):
    content = tab["documentTab"]["body"]["content"]
    tables = [el for el in content if "table" in el]
    if not tables:
        sys.exit("expected a table in the tab body but found none")
    return tables[-1]


# --- main -----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--md", required=True, help="Path to source markdown file")
    ap.add_argument("--doc-id", required=True, help="Google Doc ID")
    ap.add_argument("--tab-id", required=True, help="Tab ID inside the doc")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print batchUpdate request JSON, don't call the API")
    args = ap.parse_args()

    md_text = strip_frontmatter(Path(args.md).read_text())
    blocks = parse_markdown(md_text)

    # A table is rendered structurally (insertTable), so it can't ride along
    # in the text insert. Only a single trailing table is supported.
    table_rows = None
    if blocks and blocks[-1][0] == "table":
        table_rows = blocks.pop()[1]
    if any(b[0] == "table" for b in blocks):
        sys.exit("render.py: a Markdown table is only supported as the LAST block of the file")

    insert_at, body_end = fetch_tab_body_range(args.doc_id, args.tab_id)
    requests = build_requests(blocks, args.tab_id, insert_at, body_end)

    if args.dry_run:
        print(json.dumps(requests, indent=2))
        if table_rows:
            cols = max(len(r) for r in table_rows)
            print(f"\n# (live phase) + table {len(table_rows)} rows x {cols} cols "
                  f"appended after the text rebuild")
        return

    resp = _gws_batch(args.doc_id, requests)
    rev = resp.get("writeControl", {}).get("requiredRevisionId", "?")
    print(f"OK (text). {len(requests)} requests applied. revisionId: {rev}")

    if table_rows:
        nr, nc = render_table(args.doc_id, args.tab_id, table_rows)
        print(f"OK (table). {nr} rows x {nc} cols rendered.")


if __name__ == "__main__":
    main()
