#!/usr/bin/env python3
"""Build the static congressional activity and language dashboard."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import shutil
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
from analysis.plotting import charts, site_charts, theme  # noqa: E402
from analysis.speakers import (  # noqa: E402
    LANGUAGE_METRICS,
    language_member_rates,
    language_timeseries,
    load_daily,
    timeseries,
)

LOG = logging.getLogger("build_site")

DAILY_PATH = ROOT / "data" / "site" / "speaker_daily"
BILLS_PATH = ROOT / "data" / "site" / "bills"
SITE_DIR = ROOT / "site"
LONG_RUN_FIGURES = {
    "overview.png": ROOT / "outputs" / "figures" / "overview.png",
    "overview_house.png": ROOT / "outputs" / "figures" / "overview_house.png",
    "overview_senate.png": ROOT / "outputs" / "figures" / "overview_senate.png",
}

CAVEATS = [
    "Speech counts include only remarks attributable to a specific member by Bioguide ID; "
    "procedural speech, submitted Extensions of Remarks, and material printed into the "
    "Record are excluded.",
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

LANGUAGE_MEMBER_TOP = 8

INTERACTIVE_CHART_JS = r"""
const SVG_NS = 'http://www.w3.org/2000/svg';
const chartColors = {
  D: '#3D6F8C',
  R: '#C85A3D',
  I: '#4A7C59',
  other: '#6B6B6B',
  text: '#1A1A1A',
  muted: '#6B6B6B',
  grid: '#D6D3CC',
};

function svgNode(tag, attributes = {}, text = '') {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
  if (text !== '') node.textContent = text;
  return node;
}

function addSvgText(svg, x, y, text, attributes = {}) {
  svg.appendChild(svgNode('text', {x, y, ...attributes}, text));
}

function formatPeriod(period) {
  if (!period.includes('-')) return period;
  const [year, month] = period.split('-').map(Number);
  return new Intl.DateTimeFormat('en', {
    month: 'short', year: 'numeric', timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, month - 1, 1)));
}

function formatRate(value) {
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 1, maximumFractionDigits: 1,
  });
}

function addTooltip(wrapper) {
  const tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip';
  tooltip.hidden = true;
  wrapper.appendChild(tooltip);
  return tooltip;
}

function bindTooltip(mark, wrapper, tooltip, text) {
  mark.classList.add('data-mark');
  mark.setAttribute('tabindex', '0');
  mark.setAttribute('aria-label', text);
  const showAt = (left, top) => {
    tooltip.textContent = text;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
    tooltip.hidden = false;
  };
  mark.addEventListener('pointermove', event => {
    const bounds = wrapper.getBoundingClientRect();
    showAt(event.clientX - bounds.left + 12, event.clientY - bounds.top + 12);
  });
  mark.addEventListener('pointerleave', () => { tooltip.hidden = true; });
  mark.addEventListener('focus', () => {
    const bounds = wrapper.getBoundingClientRect();
    const markBounds = mark.getBoundingClientRect();
    showAt(markBounds.left - bounds.left + markBounds.width / 2,
      markBounds.top - bounds.top + markBounds.height + 8);
  });
  mark.addEventListener('blur', () => { tooltip.hidden = true; });
}

function addPanelHeading(wrapper, metric, detail) {
  const heading = document.createElement('div');
  heading.className = 'mini-chart-heading';
  const title = document.createElement('h3');
  title.textContent = metric.label;
  const subtitle = document.createElement('p');
  subtitle.className = 'definition';
  subtitle.textContent = `${metric.definition} ${detail}`;
  heading.append(title, subtitle);
  wrapper.appendChild(heading);
}

function addAccessibleTable(wrapper, captionText, headers, rows) {
  const table = document.createElement('table');
  table.className = 'sr-only';
  const caption = document.createElement('caption');
  caption.textContent = captionText;
  const thead = document.createElement('thead');
  const headerRow = document.createElement('tr');
  headers.forEach(header => {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = header;
    headerRow.appendChild(th);
  });
  thead.appendChild(headerRow);
  const tbody = document.createElement('tbody');
  rows.forEach(values => {
    const row = document.createElement('tr');
    values.forEach(value => {
      const cell = document.createElement('td');
      cell.textContent = value;
      row.appendChild(cell);
    });
    tbody.appendChild(row);
  });
  table.append(caption, thead, tbody);
  wrapper.appendChild(table);
}

function renderTrendPanel(language, key) {
  const metric = language.metrics[key];
  const wrapper = document.createElement('section');
  wrapper.className = 'mini-chart';
  addPanelHeading(wrapper, metric,
    `Rates are shown per 100,000 words by ${language.granularity}.`);
  const legend = document.createElement('div');
  legend.className = 'chart-legend';
  [['D', 'Democrats'], ['R', 'Republicans']].forEach(([party, label]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chart-toggle';
    button.setAttribute('aria-pressed', 'true');
    button.innerHTML = `<i style="background:${chartColors[party]}"></i>${label}`;
    button.addEventListener('click', () => {
      const active = button.getAttribute('aria-pressed') !== 'true';
      button.setAttribute('aria-pressed', String(active));
      wrapper.querySelectorAll(`[data-party="${party}"]`)
        .forEach(element => { element.style.display = active ? '' : 'none'; });
    });
    legend.appendChild(button);
  });
  wrapper.appendChild(legend);
  const tooltip = addTooltip(wrapper);
  const width = 900;
  const height = 280;
  const margin = {left: 72, right: 28, top: 18, bottom: 54};
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const svg = svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': `${metric.label} rates over time in ${language.scope_label}`,
  });
  const periods = [...new Set(language.series.map(row => row.period))].sort();
  const values = language.series.map(row => Number(row[metric.rate]) || 0);
  const maxValue = Math.max(1, ...values) * 1.08;
  const x = index => margin.left + (
    periods.length <= 1 ? plotWidth / 2 : index * plotWidth / (periods.length - 1)
  );
  const y = value => margin.top + plotHeight - (value / maxValue) * plotHeight;

  for (let tick = 0; tick <= 4; tick += 1) {
    const value = maxValue * tick / 4;
    const py = y(value);
    svg.appendChild(svgNode('line', {
      x1: margin.left, x2: width - margin.right, y1: py, y2: py,
      stroke: chartColors.grid, 'stroke-width': 1,
    }));
    addSvgText(svg, margin.left - 10, py + 4, formatRate(value), {
      'text-anchor': 'end', fill: chartColors.muted, 'font-size': 12,
    });
  }
  const labelStep = Math.max(1, Math.ceil(periods.length / 7));
  periods.forEach((period, index) => {
    if (index % labelStep !== 0 && index !== periods.length - 1) return;
    addSvgText(svg, x(index), height - 18, formatPeriod(period), {
      'text-anchor': 'middle', fill: chartColors.muted, 'font-size': 12,
    });
  });
  [['D', 'Democrats'], ['R', 'Republicans']].forEach(([party, label]) => {
    const rowsByPeriod = new Map(
      language.series.filter(row => row.party === party)
        .map(row => [row.period, row])
    );
    const points = periods.flatMap((period, index) => {
      const row = rowsByPeriod.get(period);
      return row ? [[x(index), y(Number(row[metric.rate]) || 0), row]] : [];
    });
    if (!points.length) return;
    svg.appendChild(svgNode('polyline', {
      points: points.map(point => `${point[0]},${point[1]}`).join(' '),
      fill: 'none', stroke: chartColors[party], 'stroke-width': 2.8,
      'stroke-dasharray': party === 'R' ? '9 5' : '',
      'vector-effect': 'non-scaling-stroke', 'data-party': party,
    }));
    points.forEach(([px, py, row]) => {
      const mark = party === 'D'
        ? svgNode('circle', {cx: px, cy: py, r: 5, fill: chartColors[party]})
        : svgNode('polygon', {
            points: `${px},${py - 6} ${px - 6},${py + 5} ${px + 6},${py + 5}`,
            fill: chartColors[party],
          });
      bindTooltip(mark, wrapper, tooltip,
        `${formatPeriod(row.period)} ${label}: ` +
        `${formatRate(row[metric.rate])} per 100,000 words ` +
        `(${Number(row[metric.hits]).toLocaleString()} hits; ` +
        `${Number(row.words).toLocaleString()} words)`);
      mark.setAttribute('data-party', party);
      svg.appendChild(mark);
    });
  });
  wrapper.appendChild(svg);
  addAccessibleTable(
    wrapper,
    `${metric.label} rates over time in ${language.scope_label}`,
    ['Period', 'Party', 'Rate per 100,000 words', 'Hits', 'Words'],
    language.series.map(row => [
      formatPeriod(row.period),
      row.party === 'D' ? 'Democrats' : 'Republicans',
      formatRate(row[metric.rate]),
      Number(row[metric.hits]).toLocaleString(),
      Number(row.words).toLocaleString(),
    ]),
  );
  return wrapper;
}

function renderMemberPanel(language, key) {
  const metric = language.metrics[key];
  const rows = language.members[key] || [];
  const wrapper = document.createElement('section');
  wrapper.className = 'mini-chart';
  addPanelHeading(wrapper, metric,
    'Members below the word threshold are omitted.');
  const tooltip = addTooltip(wrapper);
  const width = 900;
  const rowHeight = 38;
  const height = Math.max(170, rows.length * rowHeight + 72);
  const margin = {left: 250, right: 80, top: 15, bottom: 42};
  const plotWidth = width - margin.left - margin.right;
  const svg = svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': `Highest eligible ${metric.label.toLowerCase()} rates in ${language.scope_label}`,
  });
  if (!rows.length) {
    addSvgText(svg, width / 2, height / 2, 'No eligible members', {
      'text-anchor': 'middle', fill: chartColors.muted, 'font-size': 16,
    });
    wrapper.appendChild(svg);
    addAccessibleTable(
      wrapper,
      `Highest eligible ${metric.label.toLowerCase()} rates in ${language.scope_label}`,
      ['Rank', 'Member', 'Party', 'Chamber', 'Rate per 100,000 words', 'Hits', 'Words'],
      rows.map(row => [
        row.rank,
        row.speaker_name,
        row.party || 'Other',
        row.chamber,
        formatRate(row[metric.rate]),
        Number(row[metric.hits]).toLocaleString(),
        Number(row.words).toLocaleString(),
      ]),
    );
    return wrapper;
  }
  const maxValue = Math.max(1, ...rows.map(row => Number(row[metric.rate]) || 0)) * 1.12;
  [0, 0.5, 1].forEach(fraction => {
    const px = margin.left + plotWidth * fraction;
    svg.appendChild(svgNode('line', {
      x1: px, x2: px, y1: margin.top, y2: height - margin.bottom,
      stroke: chartColors.grid, 'stroke-width': 1,
    }));
    addSvgText(svg, px, height - 15, formatRate(maxValue * fraction), {
      'text-anchor': 'middle', fill: chartColors.muted, 'font-size': 12,
    });
  });
  rows.forEach((row, index) => {
    const py = margin.top + index * rowHeight;
    const value = Number(row[metric.rate]) || 0;
    const barWidth = value / maxValue * plotWidth;
    addSvgText(svg, margin.left - 12, py + 22, row.speaker_name, {
      'text-anchor': 'end', fill: chartColors.text, 'font-size': 14,
    });
    const bar = svgNode('rect', {
      x: margin.left, y: py + 5, width: Math.max(1, barWidth), height: 23, rx: 2,
      fill: chartColors[row.party] || chartColors.other,
    });
    bindTooltip(bar, wrapper, tooltip,
      `${row.speaker_name} (${row.party || 'Other'}, ${row.chamber}): ` +
      `${formatRate(value)} per 100,000 words ` +
      `(${Number(row[metric.hits]).toLocaleString()} hits; ` +
      `${Number(row.words).toLocaleString()} words)`);
    svg.appendChild(bar);
    addSvgText(svg, Math.min(width - 8, margin.left + barWidth + 8), py + 22,
      formatRate(value), {
        fill: chartColors.text, 'font-size': 13, 'font-weight': 'bold',
      });
  });
  wrapper.appendChild(svg);
  return wrapper;
}

function renderLanguageTable(language, key) {
  const metric = language.metrics[key];
  const table = document.querySelector(`table[data-language-metric="${key}"]`);
  if (!table) return;
  const tbody = table.querySelector('tbody');
  tbody.replaceChildren();
  const rows = language.members[key] || [];
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 7;
    cell.className = 'muted';
    cell.textContent = 'No nonzero rates in this view.';
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  rows.forEach(item => {
    const row = document.createElement('tr');
    const values = [
      item.rank, item.speaker_name, item.party, item.chamber,
      formatRate(item[metric.rate]), Number(item[metric.hits]).toLocaleString(),
      Number(item.words).toLocaleString(),
    ];
    values.forEach((value, index) => {
      const cell = document.createElement('td');
      if (index === 0 || index >= 4) cell.className = 'num';
      if (index === 1 && item.member_url) {
        const link = document.createElement('a');
        link.href = item.member_url;
        link.textContent = value;
        cell.appendChild(link);
      } else {
        cell.textContent = index === 3
          ? String(value).replace(/^./, character => character.toUpperCase())
          : value;
      }
      row.appendChild(cell);
    });
    tbody.appendChild(row);
  });
}

function renderLanguage(language) {
  const trendContainer = document.getElementById('language-trends');
  trendContainer.replaceChildren(
    ...Object.keys(language.metrics).map(key => renderTrendPanel(language, key))
  );
  trendContainer.setAttribute('aria-label', language.trend_alt);
  const memberContainer = document.getElementById('language-members');
  memberContainer.replaceChildren(
    ...Object.keys(language.metrics).map(key => renderMemberPanel(language, key))
  );
  memberContainer.setAttribute('aria-label', language.member_alt);
  document.getElementById('language-shown').textContent = language.explanation.shown;
  document.getElementById('language-examined').textContent = language.explanation.examined;
  document.getElementById('language-finding').textContent = language.explanation.finding;
  document.getElementById('language-limitation').textContent = language.explanation.limitation;
  Object.keys(language.metrics).forEach(key => renderLanguageTable(language, key));
}
"""

ACTIVITY_JS = r"""
const select = document.getElementById('congress');
let loadedCongress = select.value;
let loadSequence = 0;

function activityCell(text, link) {
  const cell = document.createElement('td');
  if (link) {
    const anchor = document.createElement('a');
    anchor.href = link;
    anchor.textContent = text;
    cell.appendChild(anchor);
  } else {
    cell.textContent = text;
  }
  return cell;
}

function renderActivityTable(metric, rows) {
  const table = document.querySelector(`table[data-metric="${metric}"]`);
  if (!table) return;
  const tbody = table.querySelector('tbody');
  tbody.replaceChildren();
  rows.forEach(item => {
    const row = document.createElement('tr');
    let values;
    if (metric === 'speech') {
      values = [
        item.rank, item.speaker_name, item.party, item.state,
        String(item.chamber || '').replace(/^./, character => character.toUpperCase()),
        Number(item.words).toLocaleString(), Number(item.turns).toLocaleString(),
        Number(item.active_days).toLocaleString(),
      ];
    } else if (metric === 'profanity') {
      values = [
        item.rank, item.speaker_name, item.party, item.state,
        Number(item.profanity_per_100k).toFixed(1),
        Number(item.profanity_hits).toLocaleString(),
        Number(item.profanity_quoted_hits).toLocaleString(),
        Number(item.words).toLocaleString(),
      ];
    } else {
      const primary = metric === 'sponsored' ? item.bills_sponsored
        : metric === 'passed' ? item.bills_passed : item.bills_enacted;
      const secondary = metric === 'sponsored' ? item.bills_passed : item.bills_sponsored;
      const third = metric === 'sponsored' ? item.bills_enacted
        : `${(100 * (metric === 'passed' ? item.passage_share : item.enactment_share)).toFixed(1)}%`;
      values = [
        item.rank, item.speaker_name, item.party, item.state,
        Number(primary).toLocaleString(), Number(secondary).toLocaleString(),
        typeof third === 'number' ? Number(third).toLocaleString() : third, '',
      ];
    }
    values.forEach((value, index) => row.appendChild(activityCell(
      value, index === 1 ? item.member_url : '',
    )));
    if (['sponsored', 'passed', 'enacted'].includes(metric)) {
      const target = row.lastChild;
      target.replaceChildren();
      (item.examples || []).forEach((example, index) => {
        if (index) target.appendChild(document.createTextNode(', '));
        const anchor = document.createElement('a');
        anchor.href = example.url;
        anchor.textContent = example.label;
        anchor.title = example.title;
        target.appendChild(anchor);
      });
    }
    tbody.appendChild(row);
  });
}

async function loadActivityCongress(value) {
  const sequence = ++loadSequence;
  const error = document.getElementById('dashboard-error');
  error.hidden = true;
  select.disabled = true;
  try {
    const response = await fetch(`data/congress_${value}.json`);
    if (!response.ok) throw new Error(`Unable to load Congress ${value}`);
    const payload = await response.json();
    if (sequence !== loadSequence) return;
    Object.entries(payload.leaderboards)
      .forEach(([metric, rows]) => renderActivityTable(metric, rows));
    const warning = document.getElementById('coverage-warning');
    warning.textContent = payload.coverage.warning || '';
    warning.hidden = !payload.coverage.warning;
    document.getElementById('coverage').textContent =
      `Speech coverage ${payload.coverage.speech_first_date} to ` +
      `${payload.coverage.speech_last_date}; ` +
      `${Number(payload.coverage.bills).toLocaleString()} H.R./S. bill records. ` +
      `Site data snapshot: ${payload.generated_utc}.`;
    loadedCongress = value;
    history.replaceState(null, '', `#congress=${value}`);
  } catch (caught) {
    if (sequence !== loadSequence) return;
    select.value = loadedCongress;
    error.textContent = caught instanceof Error
      ? caught.message : 'Unable to update the activity tables.';
    error.hidden = false;
    throw caught;
  } finally {
    if (sequence === loadSequence) select.disabled = false;
  }
}

select.addEventListener('change', () => loadActivityCongress(select.value).catch(() => {}));
const requestedCongress = new URLSearchParams(location.hash.slice(1)).get('congress');
if (requestedCongress && [...select.options].some(option => option.value === requestedCongress)
    && requestedCongress !== select.value) {
  select.value = requestedCongress;
  loadActivityCongress(requestedCongress).catch(() => {});
}
"""


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
    parser.add_argument(
        "--generated-utc",
        help="Fixed ISO-8601 build timestamp. SOURCE_DATE_EPOCH is also supported.",
    )
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


def resolve_generated_utc(
    value: Optional[str],
    daily: pd.DataFrame,
    bills: pd.DataFrame,
) -> str:
    """Return a deterministic timestamp for the newest input in the snapshot."""
    if value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SystemExit(f"error: invalid --generated-utc value {value!r}") from exc
        if parsed.tzinfo is None:
            raise SystemExit("error: --generated-utc must include a timezone")
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch is not None:
        try:
            return dt.datetime.fromtimestamp(
                int(epoch), tz=dt.timezone.utc
            ).isoformat(timespec="seconds")
        except (ValueError, OverflowError) as exc:
            raise SystemExit("error: SOURCE_DATE_EPOCH must be an integer timestamp") from exc
    candidates = []
    speech_dates = pd.to_datetime(
        daily.get("date"), format="mixed", utc=True, errors="coerce"
    )
    if speech_dates is not None and speech_dates.notna().any():
        candidates.append(speech_dates.max().to_pydatetime())
    bill_updates = pd.to_datetime(
        bills.get("source_updated_at"), format="mixed", utc=True, errors="coerce"
    )
    if bill_updates is not None and bill_updates.notna().any():
        candidates.append(bill_updates.max().to_pydatetime())
    if not candidates:
        raise SystemExit("error: cannot derive a snapshot timestamp from empty inputs")
    return max(candidates).astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _chart_leaderboard(board: pd.DataFrame, figs: Path, min_words: int) -> Path:
    fig, ax = charts.new_figure(figsize=(11, max(4.0, 0.42 * len(board) + 2)))
    if board.empty:
        charts.style_axes(
            ax,
            "Highest profanity rates in Congress",
            "Profanity per 100,000 spoken words",
            "",
            subtitle=f"No nonzero rate among members with at least {min_words:,} words",
        )
        ax.text(
            0.5, 0.5, "No eligible nonzero rates", transform=ax.transAxes,
            ha="center", va="center", color=theme.MUTED,
        )
        return charts.finish(
            fig,
            ax,
            figs / "leaderboard.png",
            source="Source: Congressional Record via GovInfo CREC / Stanford Hein.",
            legend=False,
        )
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
        for field in (
            "hostility_hits", "misconduct_hits",
            "hostility_per_100k", "misconduct_per_100k",
        ):
            row.pop(field, None)
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


def _language_payload(
    daily: pd.DataFrame,
    congress: Optional[int],
    *,
    min_words: int,
) -> dict:
    scope_label = "All Congresses" if congress is None else f"Congress {congress}"
    granularity = "year" if congress is None else "month"
    series = language_timeseries(daily, congress)
    rankings = language_member_rates(
        daily,
        congress,
        min_words=min_words,
        top=LANGUAGE_MEMBER_TOP,
    )
    party_summary = {}
    for party in ("D", "R"):
        rows = series[series["party"] == party]
        words = int(rows["words"].sum())
        party_summary[party] = {
            "words": words,
            **{
                metric["hits"]: int(rows[metric["hits"]].sum())
                for metric in LANGUAGE_METRICS.values()
            },
        }
        for metric in LANGUAGE_METRICS.values():
            hits = party_summary[party][metric["hits"]]
            party_summary[party][metric["rate"]] = (
                100_000 * hits / words if words else 0.0
            )

    member_records = {}
    for key, frame in rankings.items():
        records = _records(frame)
        for row in records:
            row["member_url"] = (
                f"https://bioguide.congress.gov/search/bio/{row['bioguide']}"
                if row.get("bioguide")
                else ""
            )
        member_records[key] = records

    findings = []
    for key, metric in LANGUAGE_METRICS.items():
        democratic_rate = party_summary["D"][metric["rate"]]
        republican_rate = party_summary["R"][metric["rate"]]
        if abs(democratic_rate - republican_rate) < 0.05:
            party_finding = (
                f"Democratic and Republican aggregate {metric['label'].lower()} rates are "
                f"approximately equal at {democratic_rate:.1f} per 100,000 words."
            )
        else:
            higher = "Democrats" if democratic_rate > republican_rate else "Republicans"
            higher_rate = max(democratic_rate, republican_rate)
            lower_rate = min(democratic_rate, republican_rate)
            party_finding = (
                f"{higher} has the higher aggregate {metric['label'].lower()} rate "
                f"({higher_rate:.1f} versus {lower_rate:.1f} per 100,000 words)."
            )
        frame = rankings[key]
        if frame.empty:
            findings.append(party_finding)
            continue
        leader = frame.iloc[0]
        findings.append(
            f"{party_finding} {leader['speaker_name']} has the highest eligible "
            f"{metric['label'].lower()} "
            f"rate at {float(leader[metric['rate']]):.1f} per 100,000 words."
        )
    period_label = "yearly" if granularity == "year" else "monthly"
    return {
        "scope_label": scope_label,
        "granularity": granularity,
        "metrics": LANGUAGE_METRICS,
        "series": _records(series),
        "members": member_records,
        "parties": party_summary,
        "trend_alt": (
            f"{period_label.title()} Democratic and Republican rates for profanity, personal "
            f"hostility or disrespect, and misconduct allegations in {scope_label}."
        ),
        "member_alt": (
            f"Highest eligible member rates for profanity, personal hostility or "
            f"disrespect, and misconduct allegations in {scope_label}."
        ),
        "explanation": {
            "shown": (
                f"The charts show {period_label} Democratic and Republican rates for three "
                "separate lexical measures, plus the highest-rate members in "
                f"{scope_label}. Rates are hits per 100,000 attributed spoken words."
            ),
            "examined": (
                "The trend panels examine how the chamber-wide rates change over time. "
                f"The member panels compare speakers with at least {min_words:,} words."
            ),
            "finding": " ".join(findings) if findings else (
                "No member cleared the word threshold in this scope."
            ),
            "limitation": (
                "These are descriptive word-pattern counts, not judgments about intent. "
                "Misconduct language does not prove misconduct, and quoted profanity is "
                "excluded from a speaker's rate."
            ),
        },
    }


def build_payload(
    daily: pd.DataFrame,
    bills: pd.DataFrame,
    congress: Optional[int],
    *,
    top: int,
    min_words: int,
    generated_utc: str,
) -> dict:
    """Build one Congress payload consumed by the static dashboard."""
    activity = member_activity(daily, bills, congress)
    boards = activity_leaderboards(activity, top=top, min_words=min_words)
    scoped_daily = daily if congress is None else daily[daily["congress"] == congress]
    scoped_bills = bills if congress is None else bills[bills["congress"] == congress]
    return {
        "congress": congress,
        "label": "All Congresses" if congress is None else f"Congress {congress}",
        "generated_utc": generated_utc,
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
        "language": _language_payload(
            daily,
            congress,
            min_words=min_words,
        ),
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


def _script_json(value) -> str:
    return (
        json.dumps(value, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("'", "\\u0027")
    )


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


def _language_table(metric: str, rows: list[dict]) -> str:
    metadata = LANGUAGE_METRICS[metric]
    headers = ["#", "Member", "Party", "Chamber", "Per 100k", "Hits", "Words"]
    head = "".join(f"<th>{html.escape(label)}</th>" for label in headers)
    body = []
    for row in rows:
        cells = [
            row["rank"],
            _member_cell(row),
            row["party"],
            str(row["chamber"]).title(),
            f"{float(row[metadata['rate']]):.1f}",
            _fmt_int(row[metadata["hits"]]),
            _fmt_int(row["words"]),
        ]
        body.append(
            "<tr>"
            + "".join(
                f"<td class=\"{'num' if index == 0 or index >= 4 else ''}\">"
                f"{cell if isinstance(cell, _TrustedHTML) else html.escape(str(cell))}"
                "</td>"
                for index, cell in enumerate(cells)
            )
            + "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="7" class="muted">No nonzero rates in this view.</td></tr>')
    return (
        f'<table data-language-metric="{metric}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _render_html(payload: dict, congresses: list[int]) -> str:
    all_selected = " selected" if payload["congress"] is None else ""
    options = [f'<option value="all"{all_selected}>All Congresses</option>']
    for congress in sorted(congresses, reverse=True):
        selected = " selected" if congress == payload["congress"] else ""
        options.append(f'<option value="{congress}"{selected}>Congress {congress}</option>')
    warning = payload["coverage"]["warning"]
    language = payload["language"]
    explanation = language["explanation"]
    caveats = "".join(
        f"<li>{html.escape(item)}</li>"
        for item in (CAVEATS[0], CAVEATS[1], CAVEATS[2], CAVEATS[-1])
    )
    language_cards = "".join(
        f'<section class="card" id="{metric}-table">'
        f'<h2>Highest {html.escape(metadata["label"].lower())} rates</h2>'
        f'<p class="definition">{html.escape(metadata["definition"])} '
        "Only members with a nonzero rate and enough words are included.</p>"
        f'<div class="table-wrap">{_language_table(metric, language["members"][metric])}</div>'
        "</section>"
        for metric, metadata in LANGUAGE_METRICS.items()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Congressional comity and conflict language</title>
<meta name="description" content="Long-run Democratic and Republican trends in congressional
courtesy, cooperation, personal disrespect, misconduct allegations, and profanity.">
<style>
  :root {{ --bg:{theme.BG}; --text:{theme.TEXT}; --muted:{theme.MUTED};
           --grid:{theme.GRID}; --blue:{theme.BLUE}; --paper:#fff; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:Georgia,'Times New Roman',serif;
          margin:0 auto; padding:2.5rem 1.25rem 4rem; max-width:76rem; line-height:1.5; }}
  h1 {{ font-size:2.2rem; margin:0 0 .35rem; }}
  h2 {{ font-size:1.3rem; margin:.1rem 0 .3rem; }}
  h3 {{ font-size:1rem; margin:0 0 .25rem; }}
  a {{ color:var(--blue); }}
  nav {{ display:flex; gap:1rem; border-bottom:1px solid var(--grid); padding-bottom:.75rem;
         margin-bottom:1.4rem; }}
  nav a[aria-current="page"] {{ color:var(--text); font-weight:bold; text-decoration:none; }}
  .sub,.definition,.muted {{ color:var(--muted); }}
  .toolbar {{ display:flex; gap:1rem; align-items:center; margin:1.5rem 0; }}
  select {{ font:inherit; padding:.45rem .6rem; background:var(--paper); border:1px solid var(--grid); }}
  .warning {{ background:#FFF3CD; border-left:4px solid #C7922B; padding:.8rem 1rem; margin:1rem 0; }}
  .card {{ background:var(--paper); border:1px solid var(--grid); padding:1rem;
           margin:1.25rem 0 2rem; }}
  .language {{ margin:1.5rem 0 2.5rem; }}
  .overview {{ background:var(--paper); border:1px solid var(--grid); padding:1rem;
               margin:1.25rem 0 2.5rem; }}
  .overview h2 {{ font-size:1.5rem; }}
  .overview-links {{ display:flex; gap:1rem; flex-wrap:wrap; margin:.7rem 0; }}
  .explanation-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr));
                       gap:.8rem; margin:1rem 0; }}
  .explanation-grid article {{ background:var(--paper); border:1px solid var(--grid);
                               padding:.85rem 1rem; }}
  .explanation-grid p {{ margin:0; }}
  .chart-card {{ background:var(--paper); border:1px solid var(--grid); margin:1rem 0;
                 padding:.75rem; }}
  .chart-card figcaption {{ color:var(--muted); font-size:.9rem; padding:.3rem .35rem 0; }}
  .interactive-chart {{ display:grid; gap:1rem; }}
  .mini-chart {{ position:relative; border-top:1px solid var(--grid); padding:.75rem .25rem 0;
                 overflow-x:auto; }}
  .mini-chart:first-child {{ border-top:0; }}
  .mini-chart-heading h3 {{ font-size:1.08rem; }}
  .mini-chart-heading p {{ margin:.15rem 0 .35rem; }}
  .mini-chart svg {{ display:block; width:100%; min-width:42rem; height:auto; overflow:visible; }}
  .mini-chart svg text {{ font-family:Georgia,'Times New Roman',serif; }}
  .chart-legend {{ display:flex; gap:1rem; color:var(--muted); font-size:.86rem;
                   margin:.3rem 0 0; }}
  .chart-toggle {{ display:inline-flex; align-items:center; gap:.35rem; border:1px solid var(--grid);
                   background:var(--paper); color:var(--text); font:inherit; padding:.25rem .5rem;
                   cursor:pointer; border-radius:999px; }}
  .chart-toggle[aria-pressed="false"] {{ opacity:.45; text-decoration:line-through; }}
  .chart-toggle:focus-visible {{ outline:2px solid var(--blue); outline-offset:2px; }}
  .chart-legend i {{ width:1rem; height:.25rem; display:inline-block; }}
  .chart-tooltip {{ position:absolute; z-index:2; max-width:18rem; pointer-events:none;
                    background:var(--text); color:var(--paper); border-radius:.2rem;
                    padding:.45rem .55rem; font: .82rem/1.35 Georgia,'Times New Roman',serif;
                    box-shadow:0 2px 8px rgb(0 0 0 / 20%); }}
  .data-mark {{ cursor:pointer; transition:opacity .12s ease, filter .12s ease; }}
  .data-mark:hover,.data-mark:focus {{ opacity:.72; filter:brightness(.88); outline:none; }}
  .sr-only {{ position:absolute !important; width:1px !important; height:1px !important;
              padding:0 !important; margin:-1px !important; overflow:hidden !important;
              clip:rect(0,0,0,0) !important; white-space:nowrap !important; border:0 !important; }}
  .error {{ color:#8A1C1C; font-weight:bold; }}
  .table-wrap {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:.92rem; }}
  th,td {{ padding:.48rem .55rem; border-bottom:1px solid var(--grid); text-align:left; }}
  th {{ border-bottom:2px solid var(--grid); white-space:nowrap; }}
  td.num {{ font-variant-numeric:tabular-nums; }}
  img {{ width:100%; height:auto; }}
  li {{ margin:.4rem 0; }}
  footer {{ margin-top:3rem; color:var(--muted); font-size:.86rem; }}
  @media (max-width:44rem) {{
    body {{ padding:1.5rem .75rem 3rem; }}
    h1 {{ font-size:1.8rem; }}
    .explanation-grid {{ grid-template-columns:1fr; }}
    .chart-card {{ padding:.3rem; }}
  }}
</style>
</head>
<body>
<nav aria-label="Primary"><a href="index.html" aria-current="page">Language analysis</a>
<a href="activity.html">Member activity and bills</a></nav>
<h1>Congressional comity and conflict language</h1>
<p class="sub">How Democratic and Republican language in the Congressional Record has changed,
from courtesy and bipartisan cooperation to personal disrespect, misconduct allegations,
and profanity.</p>
<section class="overview" aria-labelledby="overview-heading">
<h2 id="overview-heading">The long-run picture, 1873-present</h2>
<p><strong>What is shown:</strong> Six separate lexical measures, reported as
word-normalized rates for Democrats and Republicans. Positive and negative language remain
separate rather than being collapsed into a single score.</p>
<p><strong>What is being examined:</strong> Whether congressional courtesy, praise,
cooperation, personal attacks, misconduct allegations, and profanity move differently over
time and by party.</p>
<p><strong>What the figure suggests:</strong> Formal courtesy has declined sharply in recent
decades, while cooperation language rose from a low historical base. Conflict measures remain
rare but show pronounced recent spikes, especially misconduct allegations and profanity.
The source transition and changing coverage mean individual jumps should not be read as causal.</p>
<img src="figures/overview.png"
 alt="Six long-run charts comparing Democratic and Republican congressional language from 1873 to the present: courtesy, gratitude, cooperation, personal disrespect, misconduct allegations, and profanity.">
<div class="overview-links"><a href="figures/overview_house.png">House detail</a>
<a href="figures/overview_senate.png">Senate detail</a></div>
</section>
<div class="toolbar"><label for="congress">Recent detail</label><select id="congress">{''.join(options)}</select></div>
<div id="coverage-warning" class="warning" {'hidden' if not warning else ''}>{html.escape(warning)}</div>
<p id="dashboard-error" class="error" role="alert" hidden></p>
<section class="language" aria-labelledby="language-heading">
<h2 id="language-heading">Recent language on the floor</h2>
<p class="sub">Three transparent lexical measures are shown separately: profanity,
personal hostility or disrespect, and misconduct allegations. They describe language in
attributed floor remarks and compare Democrats with Republicans; they do not establish intent
or whether an allegation is true.</p>
<div class="explanation-grid">
<article><h3>What is shown</h3><p id="language-shown">{html.escape(explanation['shown'])}</p></article>
<article><h3>What is being examined</h3><p id="language-examined">{html.escape(explanation['examined'])}</p></article>
<article><h3>What the data says</h3><p id="language-finding">{html.escape(explanation['finding'])}</p></article>
<article><h3>What cannot be concluded</h3><p id="language-limitation">{html.escape(explanation['limitation'])}</p></article>
</div>
<figure class="chart-card">
<div id="language-trends" class="interactive-chart" role="group"
 aria-label="{html.escape(language['trend_alt'], quote=True)}">
<noscript><img src="figures/language_trends.png"
 alt="{html.escape(language['trend_alt'], quote=True)}"></noscript>
</div>
<figcaption>Party rates use summed hits divided by summed spoken words, rather than
averaging daily rates. Hover or focus a point for its hits and word count.</figcaption>
</figure>
<figure class="chart-card">
<div id="language-members" class="interactive-chart" role="group"
 aria-label="{html.escape(language['member_alt'], quote=True)}">
<noscript><img src="figures/language_members.png"
 alt="{html.escape(language['member_alt'], quote=True)}"></noscript>
</div>
<figcaption>Member comparisons exclude speakers below the displayed word threshold; exact
profanity values remain available in the table below. Hover or focus a bar for its raw counts.</figcaption>
</figure>
</section>
<main id="language-tables">{language_cards}</main>
<section><h2>How to read this language analysis</h2><ul>{caveats}</ul></section>
<footer id="coverage">Speech coverage {html.escape(payload['coverage']['speech_first_date'])}
to {html.escape(payload['coverage']['speech_last_date'])}. Newest Congressional Record date:
{html.escape(payload['coverage']['speech_last_date'])}. Site data snapshot:
{html.escape(payload['generated_utc'])}. <a href="activity.html">Open member activity and bill tables.</a></footer>
<script>
const initialLanguage = {_script_json(language)};
{INTERACTIVE_CHART_JS}
const select = document.getElementById('congress');
let loadedCongress = select.value;
let loadSequence = 0;
async function loadCongress(value) {{
  const sequence = ++loadSequence;
  const error = document.getElementById('dashboard-error');
  error.hidden = true; select.disabled = true;
  try {{
    const response = await fetch(`data/congress_${{value}}.json`);
    if (!response.ok) throw new Error(`Unable to load Congress ${{value}}`);
    const payload = await response.json();
    if (sequence !== loadSequence) return;
    renderLanguage(payload.language);
    const warning = document.getElementById('coverage-warning');
    warning.textContent = payload.coverage.warning || ''; warning.hidden = !payload.coverage.warning;
    document.getElementById('coverage').textContent =
      `Speech coverage ${{payload.coverage.speech_first_date}} to ${{payload.coverage.speech_last_date}}. ` +
      `Newest Congressional Record date: ${{payload.coverage.speech_last_date}}. ` +
      `Site data snapshot: ${{payload.generated_utc}}.`;
    loadedCongress = value;
    history.replaceState(null, '', `#congress=${{value}}`);
  }} catch (caught) {{
    if (sequence !== loadSequence) return;
    select.value = loadedCongress;
    error.textContent = caught instanceof Error ? caught.message : 'Unable to update the dashboard.';
    error.hidden = false;
    throw caught;
  }} finally {{
    if (sequence === loadSequence) select.disabled = false;
  }}
}}
select.addEventListener('change', () => loadCongress(select.value).catch(() => {{}}));
renderLanguage(initialLanguage);
const requestedCongress = new URLSearchParams(location.hash.slice(1)).get('congress');
if (requestedCongress && [...select.options].some(option => option.value === requestedCongress)
    && requestedCongress !== select.value) {{
  select.value = requestedCongress;
  loadCongress(requestedCongress).catch(() => {{}});
}}
</script>
</body>
</html>
"""


def _render_activity_html(payload: dict, congresses: list[int]) -> str:
    all_selected = " selected" if payload["congress"] is None else ""
    options = [f'<option value="all"{all_selected}>All Congresses</option>']
    for congress in sorted(congresses, reverse=True):
        selected = " selected" if congress == payload["congress"] else ""
        options.append(f'<option value="{congress}"{selected}>Congress {congress}</option>')
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
    caveats = "".join(f"<li>{html.escape(item)}</li>" for item in CAVEATS)
    warning = payload["coverage"]["warning"]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Congressional member activity and bills</title>
<meta name="description" content="Exact-value congressional speech, bill sponsorship,
passage, enactment, and profanity tables by Congress.">
<style>
  :root {{ --bg:{theme.BG}; --text:{theme.TEXT}; --muted:{theme.MUTED};
           --grid:{theme.GRID}; --blue:{theme.BLUE}; --paper:#fff; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:Georgia,'Times New Roman',serif;
          margin:0 auto; padding:2.5rem 1.25rem 4rem; max-width:76rem; line-height:1.5; }}
  h1 {{ font-size:2.2rem; margin:0 0 .35rem; }}
  h2 {{ font-size:1.3rem; margin:.1rem 0 .3rem; }}
  a {{ color:var(--blue); }}
  nav {{ display:flex; gap:1rem; border-bottom:1px solid var(--grid); padding-bottom:.75rem;
         margin-bottom:1.4rem; }}
  nav a[aria-current="page"] {{ color:var(--text); font-weight:bold; text-decoration:none; }}
  .sub,.definition,.muted {{ color:var(--muted); }}
  .toolbar {{ display:flex; gap:1rem; align-items:center; margin:1.5rem 0; }}
  select {{ font:inherit; padding:.45rem .6rem; background:var(--paper); border:1px solid var(--grid); }}
  .warning {{ background:#FFF3CD; border-left:4px solid #C7922B; padding:.8rem 1rem; margin:1rem 0; }}
  .error {{ color:#8A1C1C; font-weight:bold; }}
  .card {{ background:var(--paper); border:1px solid var(--grid); padding:1rem;
           margin:1.25rem 0 2rem; }}
  .table-wrap {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:.92rem; }}
  th,td {{ padding:.48rem .55rem; border-bottom:1px solid var(--grid); text-align:left; }}
  th {{ border-bottom:2px solid var(--grid); white-space:nowrap; }}
  td.num {{ font-variant-numeric:tabular-nums; }}
  li {{ margin:.4rem 0; }}
  footer {{ margin-top:3rem; color:var(--muted); font-size:.86rem; }}
  @media (max-width:44rem) {{
    body {{ padding:1.5rem .75rem 3rem; }}
    h1 {{ font-size:1.8rem; }}
  }}
</style>
</head>
<body>
<nav aria-label="Primary"><a href="index.html">Language analysis</a>
<a href="activity.html" aria-current="page">Member activity and bills</a></nav>
<h1>Congressional member activity and bills</h1>
<p class="sub">Exact-value tables for attributed speech, sponsored bills, passage,
enactment, and nonzero profanity rates. The language-analysis homepage remains the primary view.</p>
<div class="toolbar"><label for="congress">View</label><select id="congress">{''.join(options)}</select></div>
<div id="coverage-warning" class="warning" {'hidden' if not warning else ''}>{html.escape(warning)}</div>
<p id="dashboard-error" class="error" role="alert" hidden></p>
<main id="leaderboards">{cards}</main>
<section><h2>How to read these tables</h2><ul>{caveats}</ul></section>
<footer id="coverage">Speech coverage {html.escape(payload['coverage']['speech_first_date'])}
to {html.escape(payload['coverage']['speech_last_date'])}; {_fmt_int(payload['coverage']['bills'])}
H.R./S. bill records. Site data snapshot: {html.escape(payload['generated_utc'])}.</footer>
<script>{ACTIVITY_JS}</script>
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
    generated_utc = resolve_generated_utc(args.generated_utc, daily, bills)
    available = sorted(set(int(value) for value in daily["congress"].unique()))
    if congress is not None and congress not in available:
        LOG.error("no speaker rows for Congress %s", congress)
        return 1

    out: Path = args.out
    figs = out / "figures"
    data = out / "data"
    figs.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    for name, source in LONG_RUN_FIGURES.items():
        if not source.exists():
            LOG.error("missing long-run figure %s; run scripts/update.py first", source)
            return 1
        shutil.copyfile(source, figs / name)

    payloads = {
        value: build_payload(
            daily,
            bills,
            value,
            top=args.top,
            min_words=args.min_words,
            generated_utc=generated_utc,
        )
        for value in available
    }
    payloads[None] = build_payload(
        daily,
        bills,
        None,
        top=args.top,
        min_words=args.min_words,
        generated_utc=generated_utc,
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
    selected_language = selected["language"]
    site_charts.language_trends(
        pd.DataFrame(selected_language["series"]),
        figs / "language_trends.png",
        scope_label=selected_language["scope_label"],
        granularity=selected_language["granularity"],
    )
    site_charts.language_members(
        {
            key: pd.DataFrame(rows)
            for key, rows in selected_language["members"].items()
        },
        figs / "language_members.png",
        scope_label=selected_language["scope_label"],
        min_words=args.min_words,
    )
    profanity = pd.DataFrame(selected["leaderboards"]["profanity"])
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
    (out / "activity.html").write_text(
        _render_activity_html(selected, available), encoding="utf-8"
    )
    LOG.info("site written to %s (%s)", out, selected["label"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
