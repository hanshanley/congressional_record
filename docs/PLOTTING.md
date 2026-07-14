# Plotting utilities — Substack-style figures

A small, reusable plotting toolkit (`analysis/plotting/`) that gives every figure in
this project the same **Substack-style** look used across the `personal_projects`
portfolio (e.g. `uk_decline`): cream background, serif type, a muted print-friendly
palette, **tickless** axes with a thin y-only grid, bold titles with a muted sub-title,
direct **end-of-line series labels** (in place of a legend), and an italic source note.

## What's in the folder

```
analysis/plotting/
├── __init__.py     # exposes `theme` and `charts`
├── theme.py        # palette + rcParams; apply(), source_note(), end_label(), white_stroke()
└── charts.py       # composable helpers: new_figure, style_axes, line, end_label, marker_line, finish
```

## The palette (identical to `uk_decline/tuition/theme.py`)

| name     | hex       | use |
|----------|-----------|-----|
| `BG`     | `#F7F5F0` | figure/axes background (cream) |
| `TEXT`   | `#1A1A1A` | titles, labels, primary lines |
| `MUTED`  | `#6B6B6B` | ticks, grid text, source note, reference lines |
| `GRID`   | `#D6D3CC` | gridlines / spines |
| `ACCENT` | `#C85A3D` | terracotta — **Republicans** |
| `BLUE`   | `#3D6F8C` | muted blue — **Democrats** |
| `GREEN`  | `#4A7C59` | Independents |
| `GOLD`   | `#C2993E` | spare series colour |

Party helpers: `theme.PARTY_COLORS` (`{"D": BLUE, "R": ACCENT, "I": GREEN, "other": MUTED}`)
and `theme.PARTY_LABELS`.

## Quick start

```python
from analysis.plotting import theme, charts

fig, ax = charts.new_figure(figsize=(10, 5.5))     # applies the theme for you
# clean, markerless lines with direct end-of-line labels (the house style):
charts.line(ax, years, dem, color=theme.PARTY_COLORS["D"], marker=None, linewidth=2.6)
charts.line(ax, years, rep, color=theme.PARTY_COLORS["R"], marker=None, linewidth=2.6)
charts.end_label(ax, years[-1], dem[-1], "Democrats", theme.PARTY_COLORS["D"])
charts.end_label(ax, years[-1], rep[-1], "Republicans", theme.PARTY_COLORS["R"])
ax.margins(x=0.13)                                  # room for the end labels
charts.marker_line(ax, boundary_year)               # boundary from source_metadata.json
charts.style_axes(ax, "My metric", "Year", "per 1,000 words",
                  subtitle="a muted second-tier sub-title")
charts.finish(fig, ax, "outputs/figures/my_metric.png",
              source="Sources: ...", legend=False)  # note + save @ dpi=200
```

### Helper reference (`analysis.plotting.charts`)

| function | purpose |
|----------|---------|
| `new_figure(figsize=(11,6))` | apply theme + return `(fig, ax)` |
| `line(ax, xs, ys, color, label=None, linewidth=2.2, markersize=4, linestyle="-", marker="o")` | one styled series; pass `marker=None` for a clean, markerless line |
| `end_label(ax, x, y, text, color, *, fontsize=10.5, pad="  ")` | direct end-of-line label with a white halo (replaces a legend) |
| `marker_line(ax, x, color=None, style=":")` | vertical reference marker (e.g. a source-boundary year) |
| `style_axes(ax, title, xlabel, ylabel, subtitle=None)` | bold title + muted second-tier sub-title, labels, y-grid, `axisbelow` |
| `finish(fig, ax, out_path, source=None, legend=True, dpi=200)` | optional legend + italic source note + `tight_layout` + save; returns the path |

### Theme reference (`analysis.plotting.theme`)

- `apply()` — push `RC_PARAMS` into `matplotlib.rcParams` (call once before plotting;
  `charts.new_figure` does this for you). Axes are tickless, spines thin (`0.8`), top/right
  hidden, titles bold, and `text.parse_math` is off (so `$` renders literally).
- `source_note(fig, text, x=0.01, y=0.01, ha="left")` — the standard italic, muted note.
- `end_label(...)` / `white_stroke()` — end-of-line labels and the white text halo they use.
- Palette constants and `PARTY_COLORS` / `PARTY_LABELS` as above.

## How the project uses it

`analysis/viz.py` builds all civility figures with these helpers. Headline panels come from
`analysis/score/registry.py`; provenance and the source-boundary marker come from
`data/processed/coverage/source_metadata.json`. The suite includes the six-panel overview,
one full-size chart per metric, chamber splits, and D−R differences in personal-disrespect
language near out-party references. Publication PNGs are intentionally tracked in
`outputs/figures/`; intermediate metric tables remain under ignored `data/` paths. Regenerate
everything with:

```bash
python -m analysis.run viz
```

## Reusing it in another project

`analysis/plotting/` has no project-specific dependencies beyond matplotlib, so it can
be copied wholesale. To match a different portfolio project exactly, keep `theme.py`'s
palette and `RC_PARAMS` in sync with that project's `theme.py` (this one mirrors
`uk_decline/tuition/theme.py`).
