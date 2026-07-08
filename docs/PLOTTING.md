# Plotting utilities — Substack-style figures

A small, reusable plotting toolkit (`analysis/plotting/`) that gives every figure in
this project the same **Substack-style** look used across the `personal_projects`
portfolio (e.g. `uk_decline`): cream background, serif type, a muted print-friendly
palette, o-markers with a background-coloured edge, a y-only grid, borderless legend,
bold two-line titles, and an italic source note.

## What's in the folder

```
analysis/plotting/
├── __init__.py     # exposes `theme` and `charts`
├── theme.py        # palette constants + rcParams; apply(), source_note()
└── charts.py       # composable helpers: new_figure, style_axes, line, marker_line, finish
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
charts.line(ax, years, values, color=theme.PARTY_COLORS["D"], label="Democrats")
charts.line(ax, years, other,  color=theme.PARTY_COLORS["R"], label="Republicans")
charts.marker_line(ax, 2017)                        # dotted source-boundary marker
charts.style_axes(ax, "My metric", "Year", "per 1,000 words",
                  subtitle="an optional second title line")
charts.finish(fig, ax, "data/reports/figures/my_metric.png",
              source="Sources: ...")               # legend + note + save @ dpi=200
```

### Helper reference (`analysis.plotting.charts`)

| function | purpose |
|----------|---------|
| `new_figure(figsize=(11,6))` | apply theme + return `(fig, ax)` |
| `line(ax, xs, ys, color, label=None, linewidth=2.2, markersize=4)` | one styled series (line + cream-edged o-markers) |
| `marker_line(ax, x, color=None, style=":")` | vertical reference marker (e.g. a source-boundary year) |
| `style_axes(ax, title, xlabel, ylabel, subtitle=None)` | bold (optionally two-line) title, labels, y-grid, `axisbelow` |
| `finish(fig, ax, out_path, source=None, legend=True, dpi=200)` | borderless legend + italic source note + `tight_layout` + save; returns the path |

### Theme reference (`analysis.plotting.theme`)

- `apply()` — push `RC_PARAMS` into `matplotlib.rcParams` (call once before plotting;
  `charts.new_figure` does this for you).
- `source_note(fig, text, x=0.01, y=0.01, ha="left")` — the standard italic, muted note.
- Palette constants and `PARTY_COLORS` / `PARTY_LABELS` as above.

## How the project uses it

`analysis/viz.py` builds all civility figures with these helpers:
the six-panel `overview.png`, one full-size chart per metric, and a genuine
`directed_asymmetry.png` (Democrats' − Republicans' directed hostility, filled blue
above / terracotta below zero). Regenerate everything with:

```bash
python -m analysis.run viz
```

## Reusing it in another project

`analysis/plotting/` has no project-specific dependencies beyond matplotlib, so it can
be copied wholesale. To match a different portfolio project exactly, keep `theme.py`'s
palette and `RC_PARAMS` in sync with that project's `theme.py` (this one mirrors
`uk_decline/tuition/theme.py`).
