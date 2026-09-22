# jlv

A terminal viewer for JSONL files (one JSON value per line).

Install the `jsonl-viewer` package; the command is still `jlv`:

```bash
uv tool install jsonl-viewer
jlv your-file.jsonl
```

Alternatively, use `pip install jsonl-viewer`. From a source checkout:

```bash
uv run jlv your-file.jsonl
```

Requires Python 3.12+ and Textual 8+. The left panel lists numbered records;
the right panel shows the selected record as indented JSON. Select a string
to read its decoded contents in a scrollable, word-wrapped inspector.

## Screenshots

Browse records alongside their formatted JSON:

![JSONL records in the left panel and the selected record's formatted JSON in the right panel](https://raw.githubusercontent.com/afourney/jlv/main/docs/imgs/jlv_main_ui.png)

Open a string to read its decoded, word-wrapped contents:

![String inspector showing decoded multi-paragraph text in a scrollable window](https://raw.githubusercontent.com/afourney/jlv/main/docs/imgs/jlv_string_window.png)

## Controls

| Key / action | Effect |
| --- | --- |
| Up / Down | Select the previous / next record or JSON line |
| Page Up / Page Down | Move by a page |
| Home / End | Move to the first / last row |
| Tab / Shift+Tab | Switch panels |
| Left / Right | Scroll horizontally |
| Enter or click | Inspect a selected string |
| Mouse wheel / scrollbars | Scroll the panel under the pointer |
| Escape | Close the string inspector |
| q | Quit, including from the inspector |

The inspector is read-only and supports text selection and Ctrl+C to copy.
The main panels also support text selection. The left-hand preview is capped
at 1,000 characters, but the right-hand document and decoded strings are not
truncated.

## Large files

Both main panels render only visible rows, rather than creating a widget for
every record or JSON field. The loader validates the entire file in one buffered
pass and builds a compact byte-offset index. Records are read and parsed on
demand; small bounded caches retain recent previews and formatted documents.
The string inspector also renders a viewport, and defers wrapping until its
actual display width is known.

Validation still happens before the viewer opens: malformed JSON, invalid UTF-8,
and interior blank lines are reported with a line number. Trailing blank lines
are permitted. This is a read-only viewer, not a live file follower; do not edit
the input file while viewing it.

## Sample file

The repository includes `stress-test.jsonl.gz`, a compressed synthetic dataset
with 30,000 records. Extract it before opening it:

```bash
gzip -dk stress-test.jsonl.gz
uv run jlv stress-test.jsonl
```

The extracted file is 300 MB and is Git-ignored. Records range from approximately
1-100 KB and contain nested structures and strings of varying lengths. Some
strings intentionally end mid-sentence to fit the generated record sizes.
