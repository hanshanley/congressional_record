#!/usr/bin/env python3
"""Build the static congressional activity and language dashboard."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")

from matplotlib.ticker import MaxNLocator  # noqa: E402
import pandas as pd  # noqa: E402

from analysis.bills import (  # noqa: E402
    activity_leaderboards,
    load_bills,
    member_activity,
)
from analysis.plotting import charts, theme  # noqa: E402
from analysis.speakers import load_daily, timeseries  # noqa: E402

LOG = logging.getLogger("build_site")

DAILY_PATH = ROOT / "data" / "site" / "speaker_daily"
BILLS_PATH = ROOT / "data" / "site" / "bills"
SITE_DIR = ROOT / "site"

CAVEATS = [
    "Speech counts include only remarks attributable to a specific member by Bioguide ID; "
    "procedural speech and submitted Extensions of Remarks are excluded.",
    "Profanity uses a narrow, hand-curated list. Passages marked as quotations are excluded "
    "from a member's rate and retained as a separate audit count.",
    "Members below the word threshold are omitted from the profanity ranking because rates "
    "computed from small samples are unstable.",
    "Legislative counts cover sponsored House and Senate bills (H.R. and S.) only; "
    "cosponsorships, resolutions, and amendments are outside this version.",
    "A sponsored bill passing or becoming law is descriptive. It does not establish that one "
    "member personally caused the outcome or measure legislative effectiveness by itself.",
    "The Congressional Record is lightly edited rather than verbatim, and source metadata can "
    "be corrected after publication.",
]

METRIC_DEFINITIONS = {
    "speech": "Attributed non-procedural spoken words; turns and active days provide context.",
    "sponsored": "Distinct H.R. and S. bills for which the member is the official sponsor.",
    "passed": "Sponsored bills with an official measure-level House or Senate passage action.",
    "enacted": "Sponsored bills with an assigned public or private law number.",
    "profanity": "Unquoted curated profanity hits per 100,000 attributed words.",
}


class _TrustedHTML(str):
    """HTML assembled exclusively from escaped text and fixed markup."""


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--daily", type=Path, default=DAILY_PATH)
    parser.add_argument("--bills", type=Path, default=BILLS_PATH)
    parser.add_argument("--out", type=Path, default=SITE_DIR)
    parser.add_argument(
        "--congress",
        default="latest",
        help="Initial view: a Congress number, 'latest' (default), or 'all'.",
    )
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--min-words", type=int, default=25_000)
    return parser.parse_args(argv)


def resolve_congress(value, daily: pd.DataFrame) -> Optional[int]:
    """Resolve a Congress selector; ``None`` denotes the all-Congresses view."""
    text = str(value).strip().lower()
    if text == "all":
        return None
    if text == "latest":
        return int(daily["congress"].max())
    try:
        return int(text)
    except ValueError:
        raise SystemExit(
            f"error: --congress must be a number, 'latest' or 'all' (got {value!r})"
        )


def _chart_leaderboard(board: pd.DataFrame, figs: Path, min_words: int) -> Path:
    fig, ax = charts.new_figure(figsize=(11, max(4.0, 0.42 * len(board) + 2)))
    ordered = board.iloc[::-1]
    colors = [theme.PARTY_COLORS.get(p, theme.MUTED) for p in ordered["party"]]
    ax.barh(
        ordered["speaker_name"],
        ordered["profanity_per_100k"],
        color=colors,
        height=0.72,
    )
    charts.style_axes(
        ax,
        "Highest profanity rates in Congress",
        "Profanity per 100,000 spoken words",
        "",
        subtitle=f"Quotations excluded; members below {min_words:,} words omitted",
    )
    ax.grid(axis="x", linestyle="-", linewidth=0.5)
    ax.grid(axis="y", visible=False)
    return charts.finish(
        fig,
        ax,
        figs / "leaderboard.png",
        source="Source: Congressional Record via GovInfo CREC / Stanford Hein.",
        legend=False,
    )


def _chart_trend(series: pd.DataFrame, figs: Path) -> Path:
    fig, ax = charts.new_figure(figsize=(11, 5.5))
    chamber_hue = {"house": theme.GREEN, "senate": theme.GOLD}
    for chamber in ("house", "senate"):
        sub = series[series["chamber"] == chamber].sort_values("period")
        if sub.empty:
            continue
        style = theme.CHAMBER_STYLE[chamber]
        charts.line(
            ax,
            sub["period"].astype(int),
            sub["profanity_per_100k"],
            color=chamber_hue[chamber],
            label=theme.CHAMBER_LABELS[chamber],
            linestyle=style["linestyle"],
            marker=style["marker"],
            linewidth=style["linewidth"],
            markersize=style["markersize"],
        )
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    charts.style_axes(
        ax,
        "Profanity on the floor over time",
        "Year",
        "Hits per 100,000 spoken words",
        subtitle="Attributed, non-procedural remarks; quotations excluded",
    )
    return charts.finish(
        fig,
        ax,
        figs / "trend.png",
        source="Source: Congressional Record via GovInfo CREC / Stanford Hein.",
    )


def _records(frame: pd.DataFrame) -> list[dict]:
    """Return JSON-safe records without NumPy scalar values."""
    return json.loads(frame.to_json(orient="records"))


def _bill_examples(
    scoped_bills: pd.DataFrame,
    bioguide: str,
    metric: str,
    limit: int = 3,
) -> list[dict]:
    rows = scoped_bills[scoped_bills["sponsor_bioguide"] == bioguide]
    if metric == "passed":
        rows = rows[rows["passed_any_chamber"]]
    elif metric == "enacted":
        rows = rows[rows["became_law"]]
    rows = rows.sort_values(
        ["introduced_date", "bill_type", "bill_number"], ascending=[False, True, False]
    ).head(limit)
    return [
        {
            "bill_id": row.bill_id,
            "label": f"{row.bill_type} {row.bill_number}",
            "title": row.title,
            "url": _congress_bill_url(
                int(row.congress), str(row.bill_type), int(row.bill_number)
            ),
        }
        for row in rows.itertuples()
    ]


def _ordinal(value: int) -> str:
    remainder = value % 100
    suffix = "th" if 10 < remainder < 14 else {1: "st", 2: "nd", 3: "rd"}.get(
        value % 10, "th"
    )
    return f"{value}{suffix}"


def _congress_bill_url(congress: int, bill_type: str, number: int) -> str:
    chamber = "house" if bill_type == "HR" else "senate"
    return (
        f"https://www.congress.gov/bill/{_ordinal(congress)}-congress/"
        f"{chamber}-bill/{number}"
    )


def _enrich_board(
    board: pd.DataFrame,
    scoped_bills: pd.DataFrame,
    metric: str,
) -> list[dict]:
    records = _records(board)
    for row in records:
        row["member_url"] = (
            f"https://bioguide.congress.gov/search/bio/{row['bioguide']}"
            if row.get("bioguide")
            else ""
        )
        if metric in {"sponsored", "passed", "enacted"}:
            row["examples"] = _bill_examples(
                scoped_bills, str(row["bioguide"]), metric
            )
    return records


def _coverage_warning(
    congress: Optional[int],
    scoped_daily: pd.DataFrame,
    scoped_bills: pd.DataFrame,
) -> str:
    warnings = []
    if congress == 103:
        warnings.append(
            "Speech coverage for Congress 103 begins on "
            f"{scoped_daily['date'].min()}, so its speaking and profanity rankings are partial."
        )
    if congress is not None and scoped_bills.empty:
        warnings.append(
            f"Legislative data for Congress {congress} has not been seeded; "
            "bill leaderboards are unavailable rather than zero."
        )
    if congress is None:
        speech_congresses = set(int(value) for value in scoped_daily["congress"].unique())
        bill_congresses = set(int(value) for value in scoped_bills["congress"].unique())
        missing = sorted(speech_congresses - bill_congresses)
        if missing:
            warnings.append(
                "The all-Congresses legislative totals exclude unseeded Congresses "
                + ", ".join(str(value) for value in missing)
                + "."
            )
    return " ".join(warnings)


def build_payload(
    daily: pd.DataFrame,
    bills: pd.DataFrame,
    congress: Optional[int],
    *,
    top: int,
    min_words: int,
) -> dict:
    """Build one Congress payload consumed by the static dashboard."""
    activity = member_activity(daily, bills, congress)
    boards = activity_leaderboards(activity, top=top, min_words=min_words)
    scoped_daily = daily if congress is None else daily[daily["congress"] == congress]
    scoped_bills = bills if congress is None else bills[bills["congress"] == congress]
    return {
        "congress": congress,
        "label": "All Congresses" if congress is None else f"Congress {congress}",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "min_words": min_words,
        "top": top,
        "coverage": {
            "speech_first_date": str(scoped_daily["date"].min()),
            "speech_last_date": str(scoped_daily["date"].max()),
            "speaker_days": int(len(scoped_daily)),
            "members_with_speech": int(scoped_daily["bioguide"].nunique()),
            "bills": int(len(scoped_bills)),
            "members_with_bills": int(
                scoped_bills.loc[
                    scoped_bills["sponsor_bioguide"].astype(str).str.strip().ne(""),
                    "sponsor_bioguide",
                ].nunique()
            ),
            "bill_sources": sorted(scoped_bills["source"].dropna().unique().tolist()),
            "warning": _coverage_warning(congress, scoped_daily, scoped_bills),
        },
        "definitions": METRIC_DEFINITIONS,
        "caveats": CAVEATS,
        "leaderboards": {
            metric: _enrich_board(board, scoped_bills, metric)
            for metric, board in boards.items()
        },
    }


def _legacy_profanity_leaderboard(
    records: list[dict],
    daily: pd.DataFrame,
    congress: Optional[int],
) -> list[dict]:
    """Add the date range expected by the compatibility leaderboard output."""
    scoped_daily = daily if congress is None else daily[daily["congress"] == congress]
    date_ranges = scoped_daily.groupby("bioguide")["date"].agg(["min", "max"])
    legacy = []
    for record in records:
        row = record.copy()
        bioguide = row.get("bioguide")
        if bioguide in date_ranges.index:
            row["first_date"] = str(date_ranges.at[bioguide, "min"])
            row["last_date"] = str(date_ranges.at[bioguide, "max"])
        legacy.append(row)
    return legacy


def _fmt_int(value) -> str:
    return f"{int(value or 0):,}"


def _member_cell(row: dict) -> _TrustedHTML:
    name = html.escape(str(row.get("speaker_name") or row.get("bioguide") or "Unknown"))
    url = html.escape(str(row.get("member_url") or ""), quote=True)
    return _TrustedHTML(f'<a href="{url}">{name}</a>' if url else name)


def _examples_cell(row: dict) -> _TrustedHTML:
    links = []
    for example in row.get("examples", []):
        links.append(
            f'<a href="{html.escape(str(example["url"]), quote=True)}" '
            f'title="{html.escape(str(example["title"]), quote=True)}">'
            f'{html.escape(str(example["label"]))}</a>'
        )
    return _TrustedHTML(
        ", ".join(links) or '<span class="muted">None</span>'
    )


def _table(metric: str, rows: list[dict]) -> str:
    configs = {
        "speech": (
            ["#", "Member", "Party", "State", "Chamber", "Words", "Turns", "Active days"],
            lambda r: [
                r["rank"], _member_cell(r), r["party"], r["state"], str(r["chamber"]).title(),
                _fmt_int(r["words"]), _fmt_int(r["turns"]), _fmt_int(r["active_days"]),
            ],
        ),
        "sponsored": (
            ["#", "Member", "Party", "State", "Bills", "Passed", "Enacted", "Examples"],
            lambda r: [
                r["rank"], _member_cell(r), r["party"], r["state"],
                _fmt_int(r["bills_sponsored"]), _fmt_int(r["bills_passed"]),
                _fmt_int(r["bills_enacted"]), _examples_cell(r),
            ],
        ),
        "passed": (
            ["#", "Member", "Party", "State", "Passed", "Sponsored", "Passage share", "Examples"],
            lambda r: [
                r["rank"], _member_cell(r), r["party"], r["state"],
                _fmt_int(r["bills_passed"]), _fmt_int(r["bills_sponsored"]),
                f"{100 * float(r['passage_share']):.1f}%", _examples_cell(r),
            ],
        ),
        "enacted": (
            ["#", "Member", "Party", "State", "Enacted", "Sponsored", "Enactment share", "Examples"],
            lambda r: [
                r["rank"], _member_cell(r), r["party"], r["state"],
                _fmt_int(r["bills_enacted"]), _fmt_int(r["bills_sponsored"]),
                f"{100 * float(r['enactment_share']):.1f}%", _examples_cell(r),
            ],
        ),
        "profanity": (
            ["#", "Member", "Party", "State", "Per 100k", "Hits", "Quoted (excl.)", "Words"],
            lambda r: [
                r["rank"], _member_cell(r), r["party"], r["state"],
                f"{float(r['profanity_per_100k']):.1f}", _fmt_int(r["profanity_hits"]),
                _fmt_int(r["profanity_quoted_hits"]), _fmt_int(r["words"]),
            ],
        ),
    }
    headers, values = configs[metric]
    head = "".join(f"<th>{html.escape(label)}</th>" for label in headers)
    body = []
    for row in rows:
        cells = values(row)
        body.append(
            "<tr>"
            + "".join(
                f"<td class=\"{'num' if index == 0 or index >= len(cells) - 3 else ''}\">"
                f"{cell if isinstance(cell, _TrustedHTML) else html.escape(str(cell))}"
                "</td>"
                for index, cell in enumerate(cells)
            )
            + "</tr>"
        )
    return f"<table data-metric=\"{metric}\"><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _render_html(payload: dict, congresses: list[int]) -> str:
    options = ['<option value="all">All Congresses</option>']
    for congress in sorted(congresses, reverse=True):
        selected = " selected" if congress == payload["congress"] else ""
        options.append(f'<option value="{congress}"{selected}>Congress {congress}</option>')
    warning = payload["coverage"]["warning"]
    caveats = "".join(f"<li>{html.escape(item)}</li>" for item in CAVEATS)
    sections = [
        ("speech", "Who talks the most"),
        ("sponsored", "Who sponsors the most bills"),
        ("passed", "Whose sponsored bills pass a chamber"),
        ("enacted", "Whose sponsored bills become law"),
        ("profanity", "Who uses profanity at the highest rate"),
    ]
    cards = "".join(
        f'<section class="card" id="{metric}"><h2>{html.escape(title)}</h2>'
        f'<p class="definition">{html.escape(METRIC_DEFINITIONS[metric])}</p>'
        f'<div class="table-wrap">{_table(metric, payload["leaderboards"][metric])}</div></section>'
        for metric, title in sections
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Congressional activity and language</title>
<meta name="description" content="Transparent rankings of congressional floor speech,
bill sponsorship, passage, enactment, and profanity.">
<style>
  :root {{ --bg:{theme.BG}; --text:{theme.TEXT}; --muted:{theme.MUTED};
           --grid:{theme.GRID}; --blue:{theme.BLUE}; --paper:#fff; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:Georgia,'Times New Roman',serif;
          margin:0 auto; padding:2.5rem 1.25rem 4rem; max-width:76rem; line-height:1.5; }}
  h1 {{ font-size:2.2rem; margin:0 0 .35rem; }}
  h2 {{ font-size:1.3rem; margin:.1rem 0 .3rem; }}
  a {{ color:var(--blue); }}
  .sub,.definition,.muted {{ color:var(--muted); }}
  .toolbar {{ display:flex; gap:1rem; align-items:center; margin:1.5rem 0; }}
  select {{ font:inherit; padding:.45rem .6rem; background:var(--paper); border:1px solid var(--grid); }}
  .warning {{ background:#FFF3CD; border-left:4px solid #C7922B; padding:.8rem 1rem; margin:1rem 0; }}
  .card {{ background:var(--paper); border:1px solid var(--grid); padding:1rem;
           margin:1.25rem 0 2rem; }}
  .table-wrap {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:.92rem; }}
  th,td {{ padding:.48rem .55rem; border-bottom:1px solid var(--grid); text-align:left; }}
  th {{ border-bottom:2px solid var(--grid); white-space:nowrap; }}
  td.num {{ font-variant-numeric:tabular-nums; }}
  img {{ width:100%; height:auto; }}
  li {{ margin:.4rem 0; }}
  footer {{ margin-top:3rem; color:var(--muted); font-size:.86rem; }}
</style>
</head>
<body>
<h1>Congressional activity and language</h1>
<p class="sub">Who speaks, sponsors bills, sees sponsored bills advance, and uses profanity
on the floor. Rankings are descriptive and use official Congressional Record and Bill Status data.</p>
<div class="toolbar"><label for="congress">View</label><select id="congress">{''.join(options)}</select></div>
<div id="coverage-warning" class="warning" {'hidden' if not warning else ''}>{html.escape(warning)}</div>
<main id="leaderboards">{cards}</main>
<section><h2>Profanity over time</h2><img src="figures/trend.png" alt="Floor profanity rate over time by chamber"></section>
<section><h2>How to read these rankings</h2><ul>{caveats}</ul></section>
<footer id="coverage">Speech coverage {html.escape(payload['coverage']['speech_first_date'])}
to {html.escape(payload['coverage']['speech_last_date'])}; {_fmt_int(payload['coverage']['bills'])}
H.R./S. bill records in this view. Generated {html.escape(payload['generated_utc'])}.</footer>
<script>
const definitions = {json.dumps(METRIC_DEFINITIONS)};
const titles = {json.dumps(dict(sections))};
const select = document.getElementById('congress');
function cell(text, link) {{
  const td = document.createElement('td');
  if (link) {{
    const a = document.createElement('a'); a.href = link; a.textContent = text; td.appendChild(a);
  }} else td.textContent = text;
  return td;
}}
function renderTable(metric, rows) {{
  const existing = document.querySelector(`table[data-metric="${{metric}}"]`);
  const tbody = existing.querySelector('tbody'); tbody.replaceChildren();
  for (const row of rows) {{
    const tr = document.createElement('tr');
    let values;
    if (metric === 'speech') values = [row.rank, row.speaker_name, row.party, row.state,
      String(row.chamber || '').replace(/^./, c => c.toUpperCase()),
      Number(row.words).toLocaleString(), Number(row.turns).toLocaleString(),
      Number(row.active_days).toLocaleString()];
    else if (metric === 'profanity') values = [row.rank, row.speaker_name, row.party, row.state,
      Number(row.profanity_per_100k).toFixed(1), Number(row.profanity_hits).toLocaleString(),
      Number(row.profanity_quoted_hits).toLocaleString(), Number(row.words).toLocaleString()];
    else {{
      const primary = metric === 'sponsored' ? row.bills_sponsored :
        metric === 'passed' ? row.bills_passed : row.bills_enacted;
      const secondary = metric === 'sponsored' ? row.bills_passed : row.bills_sponsored;
      const third = metric === 'sponsored' ? row.bills_enacted :
        `${{(100 * (metric === 'passed' ? row.passage_share : row.enactment_share)).toFixed(1)}}%`;
      values = [row.rank, row.speaker_name, row.party, row.state,
        Number(primary).toLocaleString(), Number(secondary).toLocaleString(),
        typeof third === 'number' ? Number(third).toLocaleString() : third, ''];
    }}
    values.forEach((value, index) => tr.appendChild(cell(value,
      index === 1 ? row.member_url : '')));
    if (['sponsored','passed','enacted'].includes(metric)) {{
      const target = tr.lastChild; target.replaceChildren();
      (row.examples || []).forEach((example, index) => {{
        if (index) target.appendChild(document.createTextNode(', '));
        const a = document.createElement('a'); a.href = example.url;
        a.textContent = example.label; a.title = example.title; target.appendChild(a);
      }});
    }}
    tbody.appendChild(tr);
  }}
}}
async function loadCongress(value) {{
  const response = await fetch(`data/congress_${{value}}.json`);
  if (!response.ok) throw new Error(`Unable to load Congress ${{value}}`);
  const payload = await response.json();
  Object.entries(payload.leaderboards).forEach(([metric, rows]) => renderTable(metric, rows));
  const warning = document.getElementById('coverage-warning');
  warning.textContent = payload.coverage.warning || ''; warning.hidden = !payload.coverage.warning;
  document.getElementById('coverage').textContent =
    `Speech coverage ${{payload.coverage.speech_first_date}} to ${{payload.coverage.speech_last_date}}; ` +
    `${{Number(payload.coverage.bills).toLocaleString()}} H.R./S. bill records in this view. ` +
    `Generated ${{payload.generated_utc}}.`;
  history.replaceState(null, '', `#congress=${{value}}`);
}}
select.addEventListener('change', () => loadCongress(select.value).catch(error => alert(error.message)));
</script>
</body>
</html>
"""


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.top <= 0 or args.min_words < 0:
        LOG.error("--top must be positive and --min-words cannot be negative")
        return 1

    daily = load_daily(args.daily)
    if daily is None or daily.empty:
        LOG.error("no speaker table at %s; run scripts/update_speakers.py first", args.daily)
        return 1
    bills = load_bills(args.bills)
    if bills is None or bills.empty:
        LOG.error("no bill table at %s; run scripts/update_bills.py first", args.bills)
        return 1

    congress = resolve_congress(args.congress, daily)
    available = sorted(set(int(value) for value in daily["congress"].unique()))
    if congress is not None and congress not in available:
        LOG.error("no speaker rows for Congress %s", congress)
        return 1

    out: Path = args.out
    figs = out / "figures"
    data = out / "data"
    figs.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    payloads = {
        value: build_payload(
            daily, bills, value, top=args.top, min_words=args.min_words
        )
        for value in available
    }
    payloads[None] = build_payload(
        daily, bills, None, top=args.top, min_words=args.min_words
    )
    for value, payload in payloads.items():
        suffix = "all" if value is None else str(value)
        (data / f"congress_{suffix}.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    (data / "congresses.json").write_text(
        json.dumps({"congresses": available}, indent=2) + "\n", encoding="utf-8"
    )

    selected = payloads[congress]
    profanity = pd.DataFrame(selected["leaderboards"]["profanity"])
    if profanity.empty:
        LOG.error("no member cleared the %d-word threshold", args.min_words)
        return 1
    series = timeseries(daily)
    _chart_leaderboard(profanity, figs, args.min_words)
    _chart_trend(series, figs)

    # Retain the original machine-readable outputs for existing embeds.
    legacy_profanity = _legacy_profanity_leaderboard(
        selected["leaderboards"]["profanity"], daily, congress
    )
    (data / "leaderboard.json").write_text(
        json.dumps(legacy_profanity, indent=2) + "\n",
        encoding="utf-8",
    )
    (data / "timeseries.json").write_text(
        series.to_json(orient="records", indent=2), encoding="utf-8"
    )
    meta = {
        "generated_utc": selected["generated_utc"],
        "congress": congress,
        "min_words": args.min_words,
        "top": args.top,
        "members": selected["coverage"]["members_with_speech"],
        "eligible": len(selected["leaderboards"]["profanity"]),
        "rows": selected["coverage"]["speaker_days"],
        "first_date": selected["coverage"]["speech_first_date"],
        "last_date": selected["coverage"]["speech_last_date"],
        "bills": selected["coverage"]["bills"],
        "caveats": CAVEATS,
    }
    (data / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    (out / "index.html").write_text(
        _render_html(selected, available), encoding="utf-8"
    )
    LOG.info("site written to %s (%s)", out, selected["label"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
