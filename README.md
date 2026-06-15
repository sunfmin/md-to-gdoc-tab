# md-to-gdoc-tab

Idempotently sync a local Markdown file into a specific tab of a Google Doc, in one `batchUpdate`. Treats the `.md` file as source of truth and the tab as a rendered view.

Packaged as a [Claude Code](https://docs.claude.com/en/docs/claude-code) [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) (`SKILL.md`) — but `render.py` is a standalone Python script and runs fine without Claude Code. Install it under `~/.claude/skills/md-to-gdoc-tab/` and Claude Code (or any compatible agent runtime) will pick it up automatically.

## Why

Google Docs API is purely imperative — every edit is an index-based op (`insertText`, `deleteContentRange`, `updateParagraphStyle`, `createParagraphBullets`, …). There is no declarative endpoint that takes a desired body. Hand-driving the API is painful and easy to break. None of the existing wrappers ([`markgdoc`](https://github.com/awesomeadi00/MarkGDoc), [`timwis/markdown-to-google-doc`](https://github.com/timwis/markdown-to-google-doc), pandoc, Drive API's [server-side Markdown importer](https://workspaceupdates.googleblog.com/2024/07/import-and-export-markdown-in-google-docs.html)) understand the [tabs feature](https://developers.google.com/workspace/docs/api/how-tos/tabs) or replace a tab in place idempotently.

This tool fills that gap.

## Install

```bash
git clone https://github.com/sunfmin/md-to-gdoc-tab.git ~/.claude/skills/md-to-gdoc-tab
```

Or anywhere else if you don't use Claude Code — just adjust the path you call `render.py` with.

### Prerequisites

You need [`gws`](https://github.com/googleworkspace/cli) (Google Workspace CLI), authenticated:

```bash
brew install googleworkspace-cli   # or: npm i -g @googleworkspace/cli
gws auth login                     # interactive — opens browser
gws auth status | grep token_valid # must be true
```

## Use

```bash
python3 ~/.claude/skills/md-to-gdoc-tab/render.py \
    --md  /path/to/source.md \
    --doc-id <DOC_ID> \
    --tab-id <TAB_ID>
```

Add `--dry-run` to print the generated `batchUpdate` request JSON without calling the API.

### Finding the tab ID

Open the doc, click the tab you want, look at the URL — it ends in `tab=t.XXXXXXX`. That's the `tabId`.

## Markdown subset supported

| Markdown                | Google Doc result                          |
| ----------------------- | ------------------------------------------ |
| `# Title`               | TITLE-styled paragraph                     |
| `## Heading`            | HEADING_1                                  |
| `### Heading`           | HEADING_2                                  |
| Blank-line-separated text | NORMAL_TEXT paragraph                    |
| `- item`                | Bullet at level 0 (`•`)                    |
| `    - sub-item` (4 spaces) | Bullet at level 1 (`◦`)               |
| `        - deeper` (8 spaces) | Bullet at level 2 (`■`)             |
| `---` (horizontal rule) | Skipped                                    |
| GFM table (`\| a \| b \|`) | Real Google Docs table — bold header, fixed-width cols (last block only) |
| YAML frontmatter at top | Stripped before parsing                    |

Not yet supported: images, code blocks, links, inline `**bold**` / `*italic*`. PRs welcome.

## How it works

One `batchUpdate` per sync, containing in order:

1. `deleteContentRange` over the tab body (leaves only the residual trailing `\n`).
2. `insertText` with the full rendered content. Sub-bullets are prefixed with `\t` characters — one per nesting level.
3. `deleteParagraphBullets` over the body (defence-in-depth against inherited bullet attrs on the residual paragraph).
4. `updateParagraphStyle` per heading/title paragraph.
5. `createParagraphBullets` per contiguous bullet group. **Leading `\t` characters drive the nesting level**; the API consumes them while assigning `nestingLevel`.

Re-running with the same Markdown converges the tab to the same state.

### Two Google Docs API quirks worth knowing

1. **`createParagraphBullets` resets `indentStart`.** Passing `indentStart` before/after has no effect on `nestingLevel` — the only signal that survives is the count of leading **tab characters** in the inserted text. The API reads them, sets `nestingLevel = tab_count`, and strips them.
2. **`createParagraphBullets`'s `range.endIndex` is inclusive at the upper boundary** for `NORMAL_TEXT` paragraphs (headings get filtered). If you pass `range.endIndex` equal to the start of the next paragraph and that paragraph is normal text, it gets a bullet too. This script ends bullet ranges at `last_bullet_start + 1` (one char inside the last bullet) to avoid the leak.

## Limitations

- Replaces the **entire target tab body** — comments/suggestions on that tab are lost. (The rest of the doc, including other tabs, is untouched.)
- One tab per call. To sync multiple tabs, run multiple times.
- One trailing empty paragraph at the end of the rebuilt tab (the body's residual `\n` from before the rewrite, which Docs doesn't let you delete). Cosmetic.

## License

MIT. See `LICENSE`.
