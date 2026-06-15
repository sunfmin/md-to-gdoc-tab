---
name: md-to-gdoc-tab
description: Idempotently sync a local Markdown file into a specific tab of a Google Doc — clear-and-rebuild the tab body in one batchUpdate. Use when the user wants to (1) push edits made in a local md file back to a shared Google Doc, (2) treat md as source of truth for a doc, (3) avoid the index-juggling pain of per-operation Google Docs API edits, or (4) get true hierarchical bullets (•/◦/■) in a Google Doc programmatically. Triggers on phrases like "sync md to Google Doc", "render markdown to gdoc", "update Google Doc from local file", "push the charter back to Docs".
---

# md-to-gdoc-tab

Render a local Markdown file into a specific tab inside a Google Doc. Re-running with the same Markdown converges the doc to the same state (idempotent).

## When to use

- A local `.md` file is the source of truth and a Google Doc tab should mirror it.
- The user has been making destructive edits via `gws docs documents batchUpdate` and wants out of the index-juggling pain.
- The user wants real nested bullets (`•` → `◦` → `■`) in a Google Doc, which `createParagraphBullets` does *not* give you when called naively.

## When NOT to use

- The Google Doc has comments / suggestions on the target tab that need to be preserved. (Clear+rebuild loses positional comments.)
- The Markdown uses features outside the supported subset (images, code blocks, links, inline bold/italic). Either extend `render.py` first, or use Google Docs UI's "Paste from Markdown". (GFM tables ARE supported, but only as the file's last block.)
- Updating across multiple tabs in one go. Run the skill once per tab.

## Prerequisites

```bash
gws auth status | grep token_valid   # must be true
# If not, the user runs:  gws auth login   (interactive — browser)
```

## Usage

```bash
python3 ~/.claude/skills/md-to-gdoc-tab/render.py \
    --md  /path/to/source.md \
    --doc-id <GOOGLE_DOC_ID> \
    --tab-id <TAB_ID>
```

Use `--dry-run` to inspect the generated batchUpdate JSON without calling the API.

### Finding the tab ID

Open the doc and click the tab you want — the URL will end with `tab=t.XXXXXXX`. That's the `tabId`.

### Example

```bash
python3 ~/.claude/skills/md-to-gdoc-tab/render.py \
    --md  /path/to/source.md \
    --doc-id <DOC_ID> \
    --tab-id <TAB_ID>
```

## How it works (so you can debug)

One `batchUpdate` containing, in order:

1. `deleteContentRange` over the tab body (everything except the final residual `\n`).
2. `insertText` with the full rendered content. Sub-bullets are prefixed with `\t` characters — one per nesting level.
3. `updateParagraphStyle` per heading/title paragraph (`TITLE`, `HEADING_1`, `HEADING_2`).
4. `createParagraphBullets` per contiguous bullet group. **The leading `\t` characters drive the nesting level**, and the API consumes them while assigning `nestingLevel`. This is the only reliable technique for multi-level bullets — passing `indentStart` before/after `createParagraphBullets` does NOT work (the API resets indent on creation).

Order matters: deleteContentRange wipes the body so indices in subsequent requests can be computed against the fresh insert. The whole batch is transactional — either all succeed or all fail.

### Known quirk: `createParagraphBullets` boundary

`createParagraphBullets` is unexpectedly inclusive at `range.endIndex`. If the range ends exactly at the start of the next paragraph (i.e. `range.endIndex == nextPara.startIndex`), that next paragraph gets a bullet too — UNLESS it's styled as a heading (HEADING_1 etc.), in which case Google silently filters it out. So a bullet group followed by a HEADING_1 looks correct, but a bullet group followed by a NORMAL_TEXT paragraph leaks bullets onto that paragraph. The fix used here: end the range at `last_bullet_start + 1` (one char inside the last bullet) — that's still enough to bullet every paragraph in the group via paragraph overlap, but the boundary is strictly interior to the last bullet.

## Markdown subset supported (v1)

| Markdown                | Google Doc result                          |
| ----------------------- | ------------------------------------------ |
| `# Title`               | TITLE-styled paragraph                     |
| `## Heading`            | HEADING_1                                  |
| `### Heading`           | HEADING_2                                  |
| Blank-line-separated text | NORMAL_TEXT paragraph (no inline formatting) |
| `- item`                | Bullet at level 0 (`•`)                    |
| `    - sub-item` (4 spaces) | Bullet at level 1 (`◦`)               |
| `        - deeper` (8 spaces) | Bullet at level 2 (`■`)             |
| `---` (horizontal rule) | Skipped                                    |
| GFM table (`\| a \| b \|`) | Real Google Docs table — bold header, fixed-width cols. **Last block only** (rendered structurally via a multi-phase insertTable, see below) |
| YAML frontmatter at top | Stripped before parsing                    |

Not yet supported: images, code blocks, links, inline `**bold**` / `*italic*`. Extend `render.py` and add a row above when you add support.

### Tables (how they render)

A trailing GFM table can't ride along in the body `insertText` (a Docs table is structural), so `render_table()` runs after the text rebuild in extra phases: (1) `insertTable` at the end of the tab segment; (2) re-fetch to read each cell's content index; (3) set fixed column widths + insert each cell's text in **descending index order** so earlier inserts don't shift later cells; (4) re-fetch and bold the header row. Re-runs are still idempotent — the body clear in phase 1 of the text rebuild deletes the prior table too.

## Cosmetic quirk

The rebuilt tab ends with one empty paragraph (the body's residual `\n` from before the rewrite, which Docs doesn't let you delete). Visually one blank line at the end of the tab. Acceptable for v1; can be eliminated by skipping the final `\n` on the last block in `render.py` and re-computing the last paragraph's range — left as a future improvement.

## Failure modes & fixes

- **`token_valid: false`** — user runs `gws auth login`.
- **`Tab '<id>' not found`** — `tab-id` typo, or doc has no tabs (rare for newer docs). Verify in the URL.
- **`Invalid requests[N]`** — usually means the markdown produced a paragraph longer than the doc's current body. Re-fetch with `--dry-run` and inspect indices.
- **Nesting collapses to level 0** — leading `\t` got stripped by Markdown parser. Make sure source md uses literal tabs OR 4-space indents and `render.py` is converting them.
