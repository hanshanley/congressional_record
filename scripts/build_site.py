#!/usr/bin/env python3
"""Build the static congressional activity and language dashboard."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
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
from analysis.score.registry import HEADLINE_METRICS  # noqa: E402
from analysis.speakers import (  # noqa: E402
    LANGUAGE_METRICS,
    incomplete_profanity_term_rows,
    language_member_rates,
    language_timeseries,
    load_daily,
    profanity_term_member_counts,
    profanity_term_leaders,
    timeseries,
)

LOG = logging.getLogger("build_site")

DAILY_PATH = ROOT / "data" / "site" / "speaker_daily"
BILLS_PATH = ROOT / "data" / "site" / "bills"
SITE_DIR = ROOT / "site"
LONG_RUN_DATA_PATH = ROOT / "data" / "site" / "long_run_language.json"
LONG_RUN_METRICS_PATH = ROOT / "data" / "processed" / "metrics" / "civility_metrics.parquet"
PUBLIC_URL = "https://www.themarginoferror.com/professional_profanity/"
ALL_MEMBER_SCOPE_LABEL = "All available Congresses (1994–present)"

CAVEATS = [
    "Speech counts include only remarks attributable to a specific member by Bioguide ID; "
    "procedural speech, submitted Extensions of Remarks, and material printed into the "
    "Record are excluded. Named-member coverage begins January 25, 1994; the separate "
    "long-run party charts use the Stanford Hein corpus back to 1873.",
    "Profanity uses a conservative, hand-curated list rather than an exhaustive dictionary. "
    "Passages marked as quotations are excluded from a member's rate and retained as a "
    "separate audit count.",
    "A member's most-used profanity term is the most frequent accepted, unquoted surface form "
    "in the selected period; alphabetical order breaks ties. It does not imply personal preference.",
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
  R: '#A9442E',
  I: '#4A7C59',
  other: '#6B6B6B',
  text: '#1A1A1A',
  muted: '#6B6B6B',
  grid: '#D6D3CC',
};
let selectedLongRunMetric = 'profanity_per_1k';
let selectedLongRunChamber = 'all';
let selectedRecentMetric = 'profanity';
let selectedRecentView = 'trend';
let selectedRecentChamber = 'all';
let selectedTermView = 'leaders';
let selectedTermParty = 'all';
let selectedTermChamber = 'all';
let currentLanguage = null;
let currentLongRun = null;

const STATE_TILES = {
  ME:[10,0], VT:[8,1], NH:[9,1], MA:[10,1],
  WA:[0,2], ID:[1,2], MT:[2,2], ND:[3,2], MN:[4,2], WI:[5,2],
  MI:[7,2], NY:[8,2], RI:[9,2], CT:[10,2],
  OR:[0,3], NV:[1,3], WY:[2,3], SD:[3,3], IA:[4,3], IL:[5,3],
  IN:[6,3], OH:[7,3], PA:[8,3], NJ:[9,3],
  CA:[0,4], UT:[1,4], CO:[2,4], NE:[3,4], MO:[4,4], KY:[5,4],
  WV:[6,4], VA:[7,4], MD:[8,4], DE:[9,4], DC:[10,4],
  AZ:[0,5], NM:[1,5], KS:[2,5], AR:[3,5], TN:[4,5], NC:[5,5], SC:[6,5],
  AK:[0,6], HI:[1,6], OK:[2,6], LA:[3,6], MS:[4,6], AL:[5,6], GA:[6,6],
  TX:[2,7], FL:[7,7],
};

function isCompactChart() {
  return window.innerWidth < 600;
}

function updateHash(values) {
  const params = new URLSearchParams(location.hash.slice(1));
  Object.entries(values).forEach(([key, value]) => params.set(key, value));
  history.replaceState(null, '', `#${params.toString()}`);
}

function chamberLabel(chamber) {
  return chamber === 'all' ? 'House + Senate' :
    chamber[0].toUpperCase() + chamber.slice(1);
}

function longRunSeries(longRun) {
  return selectedLongRunChamber === 'all'
    ? longRun.series
    : longRun.chamber_series.filter(row => row.chamber === selectedLongRunChamber);
}

function recentSeries(language) {
  return selectedRecentChamber === 'all'
    ? language.series
    : language.chamber_series.filter(row => row.chamber === selectedRecentChamber);
}

function recentMembers(language, key) {
  return selectedRecentChamber === 'all'
    ? language.members[key]
    : language.members_by_chamber[selectedRecentChamber][key];
}

function recentTermDetailAvailable(language) {
  return selectedTermChamber === 'all'
    ? language.profanity_term_detail_available
    : language.profanity_term_detail_available_by_chamber[selectedTermChamber];
}

function svgNode(tag, attributes = {}, text = '') {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
  if (text !== '') node.textContent = text;
  return node;
}

function addSvgText(svg, x, y, text, attributes = {}) {
  const node = svgNode('text', {x, y, ...attributes}, text);
  svg.appendChild(node);
  return node;
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

function spacedLabelIndices(length, maxLabels = 7) {
  if (length <= maxLabels) return [...Array(length).keys()];
  const step = Math.ceil((length - 1) / (maxLabels - 1));
  const indices = [];
  for (let index = 0; index < length; index += step) indices.push(index);
  const last = length - 1;
  if (indices[indices.length - 1] !== last) {
    if (last - indices[indices.length - 1] < Math.max(2, Math.floor(step / 2))) {
      indices[indices.length - 1] = last;
    } else {
      indices.push(last);
    }
  }
  return indices;
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
    tooltip.hidden = false;
    const maxLeft = Math.max(8, wrapper.clientWidth - tooltip.offsetWidth - 8);
    const maxTop = Math.max(8, wrapper.clientHeight - tooltip.offsetHeight - 8);
    tooltip.style.left = `${Math.max(8, Math.min(left, maxLeft))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(top, maxTop))}px`;
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
  subtitle.textContent = [metric.definition, detail].filter(Boolean).join(' ');
  heading.append(title, subtitle);
  wrapper.appendChild(heading);
}

function partyName(party) {
  return party === 'D' ? 'Democrats' : 'Republicans';
}

function addPartyLegend(wrapper) {
  const legend = document.createElement('div');
  legend.className = 'chart-legend';
  legend.setAttribute('aria-label', 'Chart legend');
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
}

function addAccessibleTable(wrapper, captionText, headers, rows) {
  const accessible = document.createElement('div');
  accessible.className = 'sr-only';
  const table = document.createElement('table');
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
  accessible.appendChild(table);
  wrapper.appendChild(accessible);
}

function syncSelect(select, items, selected, onSelect) {
  const signature = items.map(item => `${item.key}:${item.label}`).join('|');
  if (select.dataset.signature !== signature) {
    select.replaceChildren();
    items.forEach(item => {
      const option = document.createElement('option');
      option.value = item.key;
      option.textContent = item.label;
      select.appendChild(option);
    });
    select.dataset.signature = signature;
  }
  select.value = selected;
  select.onchange = () => onSelect(select.value);
}

function renderTrendPanel(language, key) {
  const metric = language.metrics[key];
  const wrapper = document.createElement('section');
  wrapper.className = 'mini-chart';
  addPanelHeading(wrapper, metric,
    `Rates are shown per 100,000 words by ${language.granularity}.`);
  addPartyLegend(wrapper);
  const tooltip = addTooltip(wrapper);
  const compact = isCompactChart();
  const width = compact ? 360 : 760;
  const height = compact ? 285 : 280;
  const margin = compact
    ? {left: 42, right: 14, top: 18, bottom: 48}
    : {left: 62, right: 24, top: 18, bottom: 54};
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const svg = svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': `${metric.label} rates over time in ${language.scope_label}, ` +
      chamberLabel(selectedRecentChamber),
  });
  const sourceSeries = recentSeries(language);
  const periods = [...new Set(sourceSeries.map(row => row.period))].sort();
  const values = sourceSeries.map(row => Number(row[metric.rate]) || 0);
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
      'text-anchor': 'end', fill: chartColors.muted, 'font-size': compact ? 10 : 12,
    });
  }
  const labelIndices = new Set(spacedLabelIndices(periods.length, compact ? 4 : 7));
  periods.forEach((period, index) => {
    if (!labelIndices.has(index)) return;
    addSvgText(svg, x(index), height - 18, formatPeriod(period), {
      'text-anchor': 'middle', fill: chartColors.muted, 'font-size': compact ? 9 : 12,
    });
  });
  [['D', 'Democrats'], ['R', 'Republicans']].forEach(([party, label]) => {
    const rowsByPeriod = new Map(
      sourceSeries.filter(row => row.party === party)
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
    sourceSeries.map(row => [
      formatPeriod(row.period),
      row.party === 'D' ? 'Democrats' : 'Republicans',
      formatRate(row[metric.rate]),
      Number(row[metric.hits]).toLocaleString(),
      Number(row.words).toLocaleString(),
    ]),
  );
  return wrapper;
}

function renderLongRunPanel(longRun, key) {
  const metric = longRun.metrics[key];
  const wrapper = document.createElement('section');
  wrapper.className = 'mini-chart long-run-panel';
  addPanelHeading(wrapper, metric, metric.units);
  addPartyLegend(wrapper);
  const tooltip = addTooltip(wrapper);
  const compact = isCompactChart();
  const width = compact ? 360 : 760;
  const height = compact ? 290 : 270;
  const margin = compact
    ? {left: 42, right: 14, top: 14, bottom: 42}
    : {left: 62, right: 24, top: 14, bottom: 44};
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const svg = svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': `${metric.label}, Democrats compared with Republicans, ` +
      `${longRun.first_year} to ${longRun.last_year}, ${chamberLabel(selectedLongRunChamber)}`,
  });
  const sourceSeries = longRunSeries(longRun);
  const years = [...new Set(sourceSeries.map(row => Number(row.year)))].sort((a, b) => a - b);
  const values = sourceSeries.map(row => Number(row[metric.rate]) || 0);
  const maxValue = Math.max(0.01, ...values) * 1.08;
  const x = year => margin.left + (
    (year - years[0]) / Math.max(1, years[years.length - 1] - years[0])
  ) * plotWidth;
  const y = value => margin.top + plotHeight - (value / maxValue) * plotHeight;
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = maxValue * tick / 4;
    const py = y(value);
    svg.appendChild(svgNode('line', {
      x1: margin.left, x2: width - margin.right, y1: py, y2: py,
      stroke: chartColors.grid, 'stroke-width': 1,
    }));
    addSvgText(svg, margin.left - 9, py + 4, value.toFixed(2), {
      'text-anchor': 'end', fill: chartColors.muted, 'font-size': compact ? 9 : 11,
    });
  }
  const yearLabelIndices = new Set(spacedLabelIndices(years.length, compact ? 4 : 7));
  years.forEach((year, index) => {
    if (!yearLabelIndices.has(index)) return;
    addSvgText(svg, x(year), height - 14, String(year), {
      'text-anchor': 'middle', fill: chartColors.muted, 'font-size': compact ? 9 : 11,
    });
  });
  [['D', 'Democrats'], ['R', 'Republicans']].forEach(([party, label]) => {
    const rows = sourceSeries.filter(row => row.party === party)
      .sort((a, b) => Number(a.year) - Number(b.year));
    const points = rows.map(row => [
      x(Number(row.year)), y(Number(row[metric.rate]) || 0), row,
    ]);
    svg.appendChild(svgNode('polyline', {
      points: points.map(point => `${point[0]},${point[1]}`).join(' '),
      fill: 'none', stroke: chartColors[party], 'stroke-width': 2.5,
      'stroke-dasharray': party === 'R' ? '8 4' : '',
      'vector-effect': 'non-scaling-stroke', 'data-party': party,
    }));
    points.forEach(([px, py, row], index) => {
      if (index % 4 === 0 || index === points.length - 1) {
        svg.appendChild(svgNode('circle', {
          cx: px, cy: py, r: 2.2, fill: chartColors[party], 'data-party': party,
        }));
      }
      const mark = svgNode('circle', {
        cx: px, cy: py, r: 7, fill: 'transparent', 'data-party': party,
      });
      bindTooltip(mark, wrapper, tooltip,
        `${row.year} ${label}: ${Number(row[metric.rate]).toFixed(3)} ${metric.units} ` +
        `(${Number(row[metric.hits]).toLocaleString()} hits; ` +
        `${Number(row.words).toLocaleString()} words)`);
      svg.appendChild(mark);
    });
  });
  wrapper.appendChild(svg);
  addAccessibleTable(
    wrapper,
    `${metric.label}, ${longRun.first_year} to ${longRun.last_year}`,
    ['Year', 'Party', metric.units, 'Hits', 'Words'],
    sourceSeries.map(row => [
      row.year, partyName(row.party), Number(row[metric.rate]).toFixed(3),
      Number(row[metric.hits]).toLocaleString(), Number(row.words).toLocaleString(),
    ]),
  );
  return wrapper;
}

function renderLongRun(longRun) {
  currentLongRun = longRun;
  if (!longRun.metrics[selectedLongRunMetric]) {
    selectedLongRunMetric = Object.keys(longRun.metrics)[0];
  }
  syncSelect(
    document.getElementById('long-run-metric'),
    Object.entries(longRun.metrics).map(([key, metric]) => ({key, label: metric.label})),
    selectedLongRunMetric,
    key => {
      selectedLongRunMetric = key;
      updateHash({longMetric: key});
      renderLongRun(longRun);
    },
  );
  syncSelect(
    document.getElementById('long-run-chamber'),
    [
      {key: 'all', label: 'All chambers'},
      {key: 'house', label: 'House'},
      {key: 'senate', label: 'Senate'},
    ],
    selectedLongRunChamber,
    key => {
      selectedLongRunChamber = key;
      updateHash({longChamber: key});
      renderLongRun(longRun);
    },
  );
  const container = document.getElementById('long-run-chart');
  container.replaceChildren(renderLongRunPanel(longRun, selectedLongRunMetric));
}

function renderMemberPanel(language, key) {
  const metric = language.metrics[key];
  const rows = recentMembers(language, key) || [];
  const wrapper = document.createElement('section');
  wrapper.className = 'mini-chart';
  addPanelHeading(wrapper, metric, '');
  addPartyLegend(wrapper);
  const tooltip = addTooltip(wrapper);
  const compact = isCompactChart();
  const width = compact ? 360 : 760;
  const rowHeight = compact ? 34 : 38;
  const height = Math.max(170, rows.length * rowHeight + 72);
  const margin = compact
    ? {left: 138, right: 34, top: 15, bottom: 38}
    : {left: 220, right: 72, top: 15, bottom: 42};
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
      'text-anchor': 'middle', fill: chartColors.muted, 'font-size': compact ? 9 : 12,
    });
  });
  rows.forEach((row, index) => {
    const py = margin.top + index * rowHeight;
    const value = Number(row[metric.rate]) || 0;
    const barWidth = value / maxValue * plotWidth;
    addSvgText(svg, margin.left - 12, py + 22,
      `${row.speaker_name} (${row.party || 'Other'})`, {
      'text-anchor': 'end', fill: chartColors.text, 'font-size': compact ? 10 : 14,
      'data-party': row.party || 'other',
    });
    const bar = svgNode('rect', {
      x: margin.left, y: py + 5, width: Math.max(1, barWidth), height: 23, rx: 2,
      fill: chartColors[row.party] || chartColors.other,
      'data-party': row.party || 'other',
    });
    const termDetail = key === 'profanity' && row.favorite_profanity_term
      ? `; most-used term: “${row.favorite_profanity_term}” ` +
        `(${Number(row.favorite_profanity_term_hits).toLocaleString()})`
      : '';
    bindTooltip(bar, wrapper, tooltip,
      `${row.speaker_name} (${row.party || 'Other'}, ${row.chamber}): ` +
      `${formatRate(value)} per 100,000 words ` +
      `(${Number(row[metric.hits]).toLocaleString()} hits; ` +
      `${Number(row.words).toLocaleString()} words${termDetail})`);
    svg.appendChild(bar);
    addSvgText(svg, Math.min(width - 8, margin.left + barWidth + 8), py + 22,
      formatRate(value), {
        fill: chartColors.text, 'font-size': compact ? 10 : 13, 'font-weight': 'bold',
        'data-party': row.party || 'other',
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
  const rows = recentMembers(language, key) || [];
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = key === 'profanity' ? 8 : 7;
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
      ...(key === 'profanity' ? [item.favorite_profanity_term || '—'] : []),
      formatRate(item[metric.rate]), Number(item[metric.hits]).toLocaleString(),
      Number(item.words).toLocaleString(),
    ];
    values.forEach((value, index) => {
      const cell = document.createElement('td');
      if (index === 0 || index >= (key === 'profanity' ? 5 : 4)) cell.className = 'num';
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

function renderSelectedHighlight(language) {
  const container = document.getElementById('language-highlight');
  container.replaceChildren();
  const metric = language.metrics[selectedRecentMetric];
  const sourceSeries = recentSeries(language);
  const partyRate = party => {
    const rows = sourceSeries.filter(row => row.party === party);
    const words = rows.reduce((sum, row) => sum + Number(row.words), 0);
    const hits = rows.reduce((sum, row) => sum + Number(row[metric.hits]), 0);
    return words ? 100000 * hits / words : 0;
  };
  const topMembers = recentMembers(language, selectedRecentMetric).slice(0, 3);
  const democraticRate = partyRate('D');
  const republicanRate = partyRate('R');
  const difference = democraticRate - republicanRate;
  const higherParty = Math.abs(difference) < 0.05
    ? 'Tie' : difference > 0 ? 'Democrats' : 'Republicans';
  const comparison = higherParty === 'Tie'
    ? 'Party rates are effectively tied'
    : `${higherParty} +${formatRate(Math.abs(difference))}`;
  const eyebrow = document.createElement('p');
  eyebrow.className = 'eyebrow';
  eyebrow.textContent = `Selected measure · ${chamberLabel(selectedRecentChamber)}`;
  const title = document.createElement('h3');
  title.textContent = metric.label;
  const rates = document.createElement('div');
  rates.className = 'party-rates';
  [['democratic', democraticRate, 'Democrats'],
    ['republican', republicanRate, 'Republicans']].forEach(
    ([className, rate, label]) => {
      const item = document.createElement('span');
      item.className = `party-rate ${className}`;
      const value = document.createElement('b');
      value.textContent = formatRate(rate);
      item.append(value, document.createTextNode(` ${label}`));
      rates.appendChild(item);
    }
  );
  const comparisonText = document.createElement('p');
  comparisonText.className = 'comparison';
  comparisonText.textContent = `${comparison} per 100,000 words`;
  const leader = document.createElement('p');
  leader.className = 'leader top-members';
  if (topMembers.length) {
    const label = document.createElement('strong');
    label.textContent = 'Top member rates';
    const list = document.createElement('ol');
    topMembers.forEach(member => {
      const item = document.createElement('li');
      const term = selectedRecentMetric === 'profanity' && member.favorite_profanity_term
        ? ` · “${member.favorite_profanity_term}”`
        : '';
      item.textContent = `${member.speaker_name} (${member.party}) — ` +
        `${formatRate(member[metric.rate])}${term}`;
      list.appendChild(item);
    });
    leader.append(label, list);
  } else {
    leader.textContent = 'No nonzero member rate in this view';
  }
  container.append(eyebrow, title, rates, comparisonText, leader);
}

function renderRecentFocus() {
  if (!currentLanguage) return;
  renderTermExplorer(currentLanguage);
  Object.keys(currentLanguage.metrics).forEach(
    key => renderLanguageTable(currentLanguage, key)
  );
  syncSelect(
    document.getElementById('recent-metric'),
    Object.entries(currentLanguage.metrics).map(([key, metric]) => ({key, label: metric.label})),
    selectedRecentMetric,
    key => {
      selectedRecentMetric = key;
      updateHash({metric: key});
      renderRecentFocus();
    },
  );
  syncSelect(
    document.getElementById('recent-chamber'),
    [
      {key: 'all', label: 'All chambers'},
      {key: 'house', label: 'House'},
      {key: 'senate', label: 'Senate'},
    ],
    selectedRecentChamber,
    key => {
      selectedRecentChamber = key;
      updateHash({chamber: key});
      renderRecentFocus();
    },
  );
  syncSelect(
    document.getElementById('recent-view'),
    [
      {key: 'trend', label: 'Trend'},
      {key: 'members', label: 'Member ranking'},
      {key: 'table', label: 'Exact values'},
    ],
    selectedRecentView,
    key => {
      selectedRecentView = key;
      updateHash({view: key});
      renderRecentFocus();
    },
  );
  const visual = document.getElementById('recent-visual');
  const tables = document.getElementById('language-tables');
  visual.replaceChildren();
  if (selectedRecentView === 'trend') {
    visual.hidden = false;
    tables.hidden = true;
    visual.appendChild(renderTrendPanel(currentLanguage, selectedRecentMetric));
  } else if (selectedRecentView === 'members') {
    visual.hidden = false;
    tables.hidden = true;
    visual.appendChild(renderMemberPanel(currentLanguage, selectedRecentMetric));
  } else {
    visual.hidden = true;
    tables.hidden = false;
    tables.querySelectorAll('.card').forEach(card => {
      card.hidden = card.id !== `${selectedRecentMetric}-table`;
    });
  }
  renderSelectedHighlight(currentLanguage);
}

function renderLanguage(language) {
  currentLanguage = language;
  document.getElementById('language-shown').textContent = language.explanation.shown;
  document.getElementById('language-examined').textContent = language.explanation.examined;
  document.getElementById('language-limitation').textContent = language.explanation.limitation;
  renderRecentFocus();
}

function filteredTermRecords(language) {
  return (language.profanity_term_member_counts || []).filter(row =>
    (selectedTermParty === 'all' || row.party === selectedTermParty) &&
    (selectedTermChamber === 'all' || row.chamber === selectedTermChamber)
  );
}

function summarizeTerms(records) {
  const terms = new Map();
  records.forEach(record => {
    if (!terms.has(record.term)) {
      terms.set(record.term, {term: record.term, total_hits: 0, variants: new Set(), members: new Map()});
    }
    const term = terms.get(record.term);
    term.total_hits += Number(record.hits);
    (record.variants || []).forEach(variant => term.variants.add(variant));
    if (!term.members.has(record.bioguide)) {
      term.members.set(record.bioguide, {
        bioguide: record.bioguide,
        speaker_name: record.speaker_name,
        party: record.party,
        hits: 0,
      });
    }
    term.members.get(record.bioguide).hits += Number(record.hits);
  });
  return [...terms.values()].map(term => {
    const leaderHits = Math.max(...[...term.members.values()].map(member => member.hits));
    return {
      term: term.term,
      total_hits: term.total_hits,
      leader_hits: leaderHits,
      variants: [...term.variants].sort(),
      leaders: [...term.members.values()]
        .filter(member => member.hits === leaderHits)
        .sort((a, b) => a.speaker_name.localeCompare(b.speaker_name)),
    };
  }).sort((a, b) =>
    b.total_hits - a.total_hits || b.leader_hits - a.leader_hits ||
    a.term.localeCompare(b.term)
  );
}

function appendCell(row, value, className = '') {
  const cell = document.createElement('td');
  if (className) cell.className = className;
  if (value instanceof Node) cell.appendChild(value);
  else cell.textContent = value;
  row.appendChild(cell);
}

function renderTermTable(language, summaries) {
  const table = document.getElementById('term-leaders-table');
  const head = table.querySelector('thead tr');
  const body = document.querySelector('#term-leaders-table tbody');
  head.replaceChildren();
  body.replaceChildren();
  const available = recentTermDetailAvailable(language);
  const headers = selectedTermView === 'leaders'
    ? ['Term family', 'Top member', 'Party', 'Leader’s uses', 'Total uses']
    : ['#', 'Term family', 'Uses', 'Share', 'Grouped forms'];
  headers.forEach(label => {
    const cell = document.createElement('th');
    cell.scope = 'col';
    cell.textContent = label;
    head.appendChild(cell);
  });
  if (!available) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 5;
    cell.className = 'muted';
    cell.textContent = 'Term-level detail is not available for this historical scope.';
    row.appendChild(cell);
    body.appendChild(row);
  } else if (!summaries.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 5;
    cell.className = 'muted';
    cell.textContent = 'No accepted term uses were observed in this scope.';
    row.appendChild(cell);
    body.appendChild(row);
  }
  const allHits = summaries.reduce((sum, item) => sum + item.total_hits, 0);
  summaries.forEach((item, index) => {
    const row = document.createElement('tr');
    if (selectedTermView === 'leaders') {
      const term = document.createElement('span');
      term.textContent = item.term;
      if (item.variants.length > 1) term.title = `Grouped forms: ${item.variants.join(', ')}`;
      const leaders = document.createElement('span');
      item.leaders.forEach((leader, leaderIndex) => {
        if (leaderIndex) leaders.appendChild(document.createTextNode(', '));
        const link = document.createElement('a');
        link.href = `https://bioguide.congress.gov/search/bio/${leader.bioguide}`;
        link.textContent = leader.speaker_name;
        leaders.appendChild(link);
      });
      appendCell(row, term);
      appendCell(row, leaders);
      appendCell(row, [...new Set(item.leaders.map(leader => leader.party))].join(', '));
      appendCell(row, item.leader_hits.toLocaleString(), 'num');
      appendCell(row, item.total_hits.toLocaleString(), 'num');
    } else {
      appendCell(row, String(index + 1), 'num');
      appendCell(row, item.term);
      appendCell(row, item.total_hits.toLocaleString(), 'num');
      appendCell(row, allHits ? `${(100 * item.total_hits / allHits).toFixed(1)}%` : '0.0%', 'num');
      appendCell(row, item.variants.join(', '));
    }
    body.appendChild(row);
  });
}

function renderStateMap(language, records) {
  const container = document.getElementById('state-term-map');
  container.replaceChildren();
  const stateTerms = new Map();
  records.forEach(record => {
    if (!STATE_TILES[record.state]) return;
    if (!stateTerms.has(record.state)) stateTerms.set(record.state, new Map());
    const terms = stateTerms.get(record.state);
    terms.set(record.term, (terms.get(record.term) || 0) + Number(record.hits));
  });
  const winners = new Map();
  stateTerms.forEach((terms, state) => {
    const ordered = [...terms.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    const topHits = ordered[0][1];
    winners.set(state, {
      terms: ordered.filter(item => item[1] === topHits).map(item => item[0]),
      hits: topHits,
    });
  });
  const maxHits = Math.max(1, ...[...winners.values()].map(item => item.hits));
  const svg = svgNode('svg', {
    viewBox: '0 0 880 650',
    role: 'img',
    'aria-label': `Most-used profanity term family by state in ${language.scope_label}`,
  });
  const title = svgNode('title', {}, `Most-used profanity term family by state in ${language.scope_label}`);
  svg.appendChild(title);
  Object.entries(STATE_TILES).forEach(([state, [column, row]]) => {
    const winner = winners.get(state);
    const ratio = winner ? Math.sqrt(winner.hits / maxHits) : 0;
    const shades = ['#EAE5DA', '#D7E5F1', '#B8D2E7', '#86B2D4', '#4A86B5', '#23567D'];
    const shade = winner ? shades[Math.min(5, Math.max(1, Math.ceil(ratio * 5)))] : shades[0];
    const group = svgNode('g', {transform: `translate(${column * 78 + 10} ${row * 75 + 18})`});
    const tile = svgNode('rect', {
      width: 70, height: 67, rx: 7, fill: shade, stroke: '#FFFEFA', 'stroke-width': 2,
    });
    const tooltip = winner
      ? `${state}: ${winner.terms.join(' / ')} (${winner.hits.toLocaleString()} uses)`
      : `${state}: no accepted uses`;
    tile.setAttribute('tabindex', '0');
    tile.setAttribute('aria-label', tooltip);
    tile.appendChild(svgNode('title', {}, tooltip));
    group.appendChild(tile);
    const dark = ratio > 0.62;
    group.appendChild(svgNode('text', {
      x: 8, y: 18, fill: dark ? '#FFFEFA' : '#171717', 'font-size': 13, 'font-weight': 800,
    }, state));
    if (winner) {
      const label = winner.terms.join(' / ');
      group.appendChild(svgNode('text', {
        x: 35, y: 42, fill: dark ? '#FFFEFA' : '#171717', 'font-size': 10,
        'text-anchor': 'middle', 'font-weight': 700,
      }, label.length > 11 ? `${label.slice(0, 10)}…` : label));
      group.appendChild(svgNode('text', {
        x: 35, y: 56, fill: dark ? '#E6EEF5' : '#4F4B45', 'font-size': 9,
        'text-anchor': 'middle',
      }, winner.hits.toLocaleString()));
    }
    svg.appendChild(group);
  });
  container.appendChild(svg);
  const note = document.createElement('p');
  note.className = 'definition map-legend';
  note.textContent = 'Each tile names the most-used grouped term; darker tiles indicate more uses. ' +
    'Hover or focus a state for its full term and count.';
  container.appendChild(note);
}

function renderTermExplorer(language) {
  syncSelect(
    document.getElementById('term-view'),
    [
      {key: 'leaders', label: 'Top member for each term'},
      {key: 'frequency', label: 'Most-used terms'},
    ],
    selectedTermView,
    key => {
      selectedTermView = key;
      updateHash({termView: key});
      renderTermExplorer(currentLanguage);
    },
  );
  syncSelect(
    document.getElementById('term-party'),
    [
      {key: 'all', label: 'All parties'},
      {key: 'D', label: 'Democratic'},
      {key: 'R', label: 'Republican'},
      {key: 'I', label: 'Independent'},
      {key: 'other', label: 'Other / unknown'},
    ],
    selectedTermParty,
    key => {
      selectedTermParty = key;
      updateHash({termParty: key});
      renderTermExplorer(currentLanguage);
    },
  );
  syncSelect(
    document.getElementById('term-chamber'),
    [
      {key: 'all', label: 'House + Senate'},
      {key: 'house', label: 'House'},
      {key: 'senate', label: 'Senate'},
    ],
    selectedTermChamber,
    key => {
      selectedTermChamber = key;
      updateHash({termChamber: key});
      renderTermExplorer(currentLanguage);
    },
  );
  const records = filteredTermRecords(language);
  const summaries = summarizeTerms(records);
  const available = recentTermDetailAvailable(language);
  renderTermTable(language, summaries);
  renderStateMap(language, records);
  document.getElementById('term-leaders-scope').textContent =
    `${language.scope_label} · ${selectedTermParty === 'all' ? 'All parties' : selectedTermParty} · ` +
    `${chamberLabel(selectedTermChamber)}`;
  document.getElementById('term-leaders-note').textContent = available
    ? 'Counts include accepted, unquoted uses by attributed members; inflected, plural, ' +
      'spacing, and spelling variants are grouped into term families.'
    : 'Term-level detail has not been backfilled for this historical scope.';
}
"""

ACTIVITY_JS = r"""
const DATA_ROOT = '../data';
const select = document.getElementById('congress');
let loadedCongress = select.value;
let loadSequence = 0;
let selectedActivityMetric = 'speech';

const activityMetrics = [
  ['speech', 'Speech'],
  ['sponsored', 'Sponsored bills'],
  ['passed', 'Passed a chamber'],
  ['enacted', 'Became law'],
  ['profanity', 'Profanity'],
];

function selectActivityMetric(metric) {
  selectedActivityMetric = metric;
  document.getElementById('activity-metric').value = metric;
  document.querySelectorAll('#leaderboards .card').forEach(card => {
    card.hidden = card.id !== metric;
  });
  const params = new URLSearchParams(location.hash.slice(1));
  params.set('table', metric);
  history.replaceState(null, '', `#${params.toString()}`);
}

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
        item.favorite_profanity_term || '—',
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
    const response = await fetch(`${DATA_ROOT}/congress_${value}.json`);
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
    const params = new URLSearchParams(location.hash.slice(1));
    params.set('congress', value);
    history.replaceState(null, '', `#${params.toString()}`);
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
const activityState = new URLSearchParams(location.hash.slice(1));
if (activityMetrics.some(([metric]) => metric === activityState.get('table'))) {
  selectedActivityMetric = activityState.get('table');
}
document.getElementById('activity-metric').addEventListener(
  'change', event => selectActivityMetric(event.target.value)
);
selectActivityMetric(selectedActivityMetric);
const requestedCongress = activityState.get('congress');
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
    scope_label: Optional[str] = None,
) -> dict:
    scope_label = scope_label or (
        ALL_MEMBER_SCOPE_LABEL if congress is None else f"Congress {congress}"
    )
    granularity = "year" if congress is None else "month"
    series = language_timeseries(daily, congress)
    chamber_series = language_timeseries(daily, congress, by_chamber=True)
    rankings = language_member_rates(
        daily,
        congress,
        min_words=min_words,
        top=LANGUAGE_MEMBER_TOP,
    )
    chamber_rankings = {
        chamber: language_member_rates(
            daily,
            congress,
            min_words=min_words,
            top=LANGUAGE_MEMBER_TOP,
            chamber=chamber,
        )
        for chamber in ("house", "senate")
    }
    term_frame = daily if congress is None else daily[daily["congress"] == congress]
    term_frame = term_frame[term_frame["chamber"].isin(["house", "senate"])]
    term_detail_available = incomplete_profanity_term_rows(term_frame).empty
    term_detail_available_by_chamber = {
        chamber: incomplete_profanity_term_rows(
            term_frame[term_frame["chamber"] == chamber]
        ).empty
        for chamber in ("house", "senate")
    }
    term_leaders = (
        profanity_term_leaders(daily, congress)
        if term_detail_available else []
    )
    chamber_term_leaders = {
        chamber: (
            profanity_term_leaders(daily, congress, chamber=chamber)
            if term_detail_available_by_chamber[chamber] else []
        )
        for chamber in ("house", "senate")
    }

    def enrich_term_leaders(rows: list[dict]) -> list[dict]:
        for row in rows:
            for leader in row["leaders"]:
                leader["member_url"] = (
                    f"https://bioguide.congress.gov/search/bio/{leader['bioguide']}"
                )
        return rows
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

    def enrich_rankings(frames: dict[str, pd.DataFrame]) -> dict[str, list[dict]]:
        enriched = {}
        for key, frame in frames.items():
            records = _records(frame)
            for row in records:
                row["member_url"] = (
                    f"https://bioguide.congress.gov/search/bio/{row['bioguide']}"
                    if row.get("bioguide")
                    else ""
                )
            enriched[key] = records
        return enriched

    member_records = enrich_rankings(rankings)

    findings = []
    highlights = []
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
                f"{higher} have the higher aggregate {metric['label'].lower()} rate "
                f"({higher_rate:.1f} versus {lower_rate:.1f} per 100,000 words)."
            )
        frame = rankings[key]
        leader = None if frame.empty else frame.iloc[0]
        highlights.append({
            "key": key,
            "label": metric["label"],
            "democratic_rate": democratic_rate,
            "republican_rate": republican_rate,
            "difference": democratic_rate - republican_rate,
            "higher_party": (
                "Tie" if abs(democratic_rate - republican_rate) < 0.05 else higher
            ),
            "leader_name": "" if leader is None else str(leader["speaker_name"]),
            "leader_rate": 0.0 if leader is None else float(leader[metric["rate"]]),
            "top_members": [
                {
                    "rank": int(row["rank"]),
                    "name": str(row["speaker_name"]),
                    "party": str(row["party"]),
                    "rate": float(row[metric["rate"]]),
                }
                for _, row in frame.head(3).iterrows()
            ],
        })
        if frame.empty:
            findings.append(party_finding)
            continue
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
        "chamber_series": _records(chamber_series),
        "members": member_records,
        "members_by_chamber": {
            chamber: enrich_rankings(frames)
            for chamber, frames in chamber_rankings.items()
        },
        "profanity_term_leaders": enrich_term_leaders(term_leaders),
        "profanity_term_member_counts": profanity_term_member_counts(daily, congress),
        "profanity_term_detail_available": term_detail_available,
        "profanity_term_leaders_by_chamber": {
            chamber: enrich_term_leaders(rows)
            for chamber, rows in chamber_term_leaders.items()
        },
        "profanity_term_detail_available_by_chamber": term_detail_available_by_chamber,
        "parties": party_summary,
        "highlights": highlights,
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
                "The trend panels compare party-wide rates over time. Member panels "
                "show the highest nonzero rates among speakers with substantial floor remarks."
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


def build_long_run_payload(metrics: pd.DataFrame) -> dict:
    """Build compact annual Democratic/Republican data for the homepage overview."""
    frame = metrics[
        metrics["chamber"].isin(["house", "senate"])
        & metrics["party"].isin(["D", "R"])
    ].copy()
    hit_columns = [metric.raw_count for metric in HEADLINE_METRICS]
    grouped = frame.groupby(["year", "party"], as_index=False)[["words", *hit_columns]].sum()
    chamber_grouped = frame.groupby(
        ["year", "party", "chamber"], as_index=False
    )[["words", *hit_columns]].sum()
    for target in (grouped, chamber_grouped):
        for metric in HEADLINE_METRICS:
            target[metric.rate] = (
                metric.scale
                * target[metric.raw_count]
                / target["words"].where(target["words"] > 0)
            ).fillna(0.0)
    return {
        "metrics": {
            metric.rate: {
                "rate": metric.rate,
                "hits": metric.raw_count,
                "label": metric.title,
                "units": metric.units,
                "polarity": metric.polarity,
            }
            for metric in HEADLINE_METRICS
        },
        "series": _records(grouped[
            ["year", "party", "words", *hit_columns, *[
                metric.rate for metric in HEADLINE_METRICS
            ]]
        ]),
        "chamber_series": _records(chamber_grouped[
            ["year", "party", "chamber", "words", *hit_columns, *[
                metric.rate for metric in HEADLINE_METRICS
            ]]
        ]),
        "first_year": int(grouped["year"].min()),
        "last_year": int(grouped["year"].max()),
        "source_note": (
            "Stanford Hein Congressional Record through 2017 and GovInfo CREC from "
            "2017 onward; House and Senate floor language, Democrats and Republicans."
        ),
    }


def load_long_run_payload() -> dict:
    """Refresh long-run site data locally when possible, otherwise load committed data."""
    if LONG_RUN_METRICS_PATH.exists():
        payload = build_long_run_payload(pd.read_parquet(LONG_RUN_METRICS_PATH))
        LONG_RUN_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        LONG_RUN_DATA_PATH.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload
    if not LONG_RUN_DATA_PATH.exists():
        raise SystemExit(
            "error: no long-run site data; run scripts/build_site.py where "
            "data/processed/metrics/civility_metrics.parquet is available"
        )
    return json.loads(LONG_RUN_DATA_PATH.read_text(encoding="utf-8"))


def build_payload(
    daily: pd.DataFrame,
    bills: pd.DataFrame,
    congress: Optional[int],
    *,
    top: int,
    min_words: int,
    generated_utc: str,
    scope_label: Optional[str] = None,
) -> dict:
    """Build one Congress payload consumed by the static dashboard."""
    activity = member_activity(daily, bills, congress)
    boards = activity_leaderboards(activity, top=top, min_words=min_words)
    scoped_daily = daily if congress is None else daily[daily["congress"] == congress]
    scoped_bills = bills if congress is None else bills[bills["congress"] == congress]
    return {
        "congress": congress,
        "label": scope_label or (
            ALL_MEMBER_SCOPE_LABEL if congress is None else f"Congress {congress}"
        ),
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
            scope_label=scope_label,
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
            ["#", "Member", "Party", "State", "Most-used term", "Per 100k", "Hits",
             "Quoted (excl.)", "Words"],
            lambda r: [
                r["rank"], _member_cell(r), r["party"], r["state"],
                r.get("favorite_profanity_term") or "—",
                f"{float(r['profanity_per_100k']):.1f}", _fmt_int(r["profanity_hits"]),
                _fmt_int(r["profanity_quoted_hits"]), _fmt_int(r["words"]),
            ],
        ),
    }
    headers, values = configs[metric]
    head = "".join(f'<th scope="col">{html.escape(label)}</th>' for label in headers)
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
    return (
        f'<table data-metric="{metric}"><caption class="sr-only">'
        f'{html.escape(METRIC_DEFINITIONS[metric])}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _language_table(metric: str, rows: list[dict]) -> str:
    metadata = LANGUAGE_METRICS[metric]
    headers = ["#", "Member", "Party", "Chamber"]
    if metric == "profanity":
        headers.append("Most-used term")
    headers.extend(["Per 100k", "Hits", "Words"])
    head = "".join(f'<th scope="col">{html.escape(label)}</th>' for label in headers)
    body = []
    for row in rows:
        cells = [
            row["rank"],
            _member_cell(row),
            row["party"],
            str(row["chamber"]).title(),
            *(
                [row.get("favorite_profanity_term") or "—"]
                if metric == "profanity" else []
            ),
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
        colspan = 8 if metric == "profanity" else 7
        body.append(
            f'<tr><td colspan="{colspan}" class="muted">No nonzero rates in this view.</td></tr>'
        )
    return (
        f'<table data-language-metric="{metric}"><caption class="sr-only">'
        f'{html.escape(metadata["label"])} member rates</caption>'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _term_leaders_table(rows: list[dict], *, available: bool) -> str:
    body = []
    if not available:
        body.append(
            '<tr><td colspan="5" class="muted">'
            "Term-level detail is not available for this historical scope.</td></tr>"
        )
    elif not rows:
        body.append(
            '<tr><td colspan="5" class="muted">'
            "No accepted term uses were observed in this scope.</td></tr>"
        )
    for row in rows:
        leaders = ", ".join(str(_member_cell(leader)) for leader in row["leaders"])
        parties = ", ".join(sorted({leader["party"] for leader in row["leaders"]}))
        variants = row.get("variants", [])
        variant_title = (
            f' title="{html.escape("Grouped forms: " + ", ".join(variants), quote=True)}"'
            if len(variants) > 1 else ""
        )
        body.append(
            "<tr>"
            f"<td{variant_title}>{html.escape(row['term'])}</td>"
            f"<td>{leaders}</td>"
            f"<td>{html.escape(parties)}</td>"
            f'<td class="num">{_fmt_int(row["leader_hits"])}</td>'
            f'<td class="num">{_fmt_int(row["total_hits"])}</td>'
            "</tr>"
        )
    return (
        '<table id="term-leaders-table"><caption class="sr-only">'
        "Top congressional users of each observed profanity term</caption>"
        '<thead><tr><th scope="col">Term</th><th scope="col">Top member</th>'
        '<th scope="col">Party</th><th scope="col">Leader’s uses</th>'
        f'<th scope="col">Total uses</th></tr></thead><tbody>{"".join(body)}</tbody></table>'
    )


def _render_html(payload: dict, congresses: list[int], long_run: dict) -> str:
    recent_selected = payload["label"].startswith("Last 5 Congresses")
    all_selected = " selected" if payload["congress"] is None and not recent_selected else ""
    options = [
        f'<option value="all"{all_selected}>{ALL_MEMBER_SCOPE_LABEL}</option>'
    ]
    if len(congresses) >= 5:
        selected = " selected" if recent_selected else ""
        options.append(
            f'<option value="recent5"{selected}>Last 5 Congresses</option>'
        )
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
        (
            f'<section class="card" id="{metric}-table">'
            f'<h2>Highest {html.escape(metadata["label"].lower())} rates</h2>'
            f'<p class="definition">{html.escape(metadata["definition"])} '
            + (
                "“Most-used term” is the most frequent unquoted match, not a claim of preference. "
                if metric == "profanity" else ""
            )
            + "Only members with a nonzero rate and enough words are included.</p>"
            + f'<div class="table-wrap">{_language_table(metric, language["members"][metric])}</div>'
            + "</section>"
        )
        for metric, metadata in LANGUAGE_METRICS.items()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Language of Congress</title>
<meta name="description" content="Long-run Democratic and Republican trends in congressional
courtesy, cooperation, personal disrespect, misconduct allegations, and profanity.">
<link rel="canonical" href="{PUBLIC_URL}">
<style>
  :root {{ --bg:#F3F0E8; --text:#171717; --muted:#68655F;
           --grid:#D8D3C9; --blue:{theme.BLUE}; --red:{theme.ACCENT};
           --paper:#FFFEFA; --soft:#EAE5DA; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text);
          font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          margin:0 auto; padding:1.4rem 1.25rem 4rem; max-width:74rem; line-height:1.55; }}
  h1,h2,h3 {{ font-family:'Iowan Old Style','Palatino Linotype',Georgia,serif; }}
  h1 {{ font-size:clamp(2.4rem,5vw,4.5rem); line-height:.98; letter-spacing:-.045em;
        max-width:58rem; margin:2.4rem 0 1rem; }}
  h2 {{ font-size:clamp(1.8rem,3vw,2.7rem); line-height:1.05; letter-spacing:-.025em;
        margin:.1rem 0 .5rem; }}
  h3 {{ font-size:1.12rem; margin:0 0 .3rem; }}
  a {{ color:var(--blue); }}
  nav {{ display:flex; gap:.35rem; align-items:center; border-bottom:1px solid var(--grid);
         padding-bottom:.9rem; }}
  nav a {{ color:var(--muted); text-decoration:none; padding:.4rem .7rem; border-radius:999px;
           font-size:.88rem; font-weight:650; }}
  nav a:hover {{ background:var(--soft); color:var(--text); }}
  nav a[aria-current="page"] {{ color:var(--paper); background:var(--text); }}
  .skip-link {{ position:absolute; left:-9999px; top:.5rem; z-index:10;
                background:var(--text); color:var(--paper); padding:.55rem .75rem; }}
  .skip-link:focus {{ left:.5rem; }}
  .sub,.definition,.muted {{ color:var(--muted); }}
  .hero-deck {{ font-size:1.12rem; max-width:50rem; margin-bottom:3rem; }}
  .section-header {{ margin:4rem 0 1.2rem; }}
  .section-header h2 {{ margin:0; }}
  .section-header p {{ margin:.25rem 0 0; max-width:52rem; }}
  select {{ font:inherit; padding:.6rem 2.2rem .6rem .8rem; background:var(--paper);
            border:1px solid var(--grid); border-radius:.45rem; }}
  .explorer-controls {{ display:grid; grid-template-columns:repeat(2,minmax(12rem,18rem));
                        gap:.75rem; margin:1rem 0 1.2rem; }}
  .recent-controls {{ grid-template-columns:2fr 1fr 1fr 1fr; }}
  .term-controls {{ grid-template-columns:2fr 1fr 1fr; }}
  .explorer-controls label {{ display:grid; gap:.3rem; color:var(--muted);
                              font-size:.72rem; font-weight:800; letter-spacing:.08em;
                              text-transform:uppercase; }}
  .explorer-controls select {{ width:100%; color:var(--text); text-transform:none;
                               letter-spacing:normal; font-weight:650; }}
  .warning {{ background:#FFF3CD; border-left:4px solid #C7922B; padding:.8rem 1rem; margin:1rem 0; }}
  .card {{ background:var(--paper); border:1px solid var(--grid); padding:1rem;
           margin:1.25rem 0 2rem; min-width:0; }}
  .language {{ margin:1.5rem 0 2.5rem; }}
  .overview {{ margin:1.25rem 0 2.5rem; }}
  .overview-intro {{ max-width:48rem; margin-bottom:1.5rem; }}
  .overview-intro p {{ color:var(--muted); margin:.35rem 0; }}
  .tab-row {{ display:flex; gap:.4rem; flex-wrap:wrap; margin:.85rem 0; }}
  .tab-button {{ appearance:none; border:1px solid var(--grid); background:transparent;
                 color:var(--muted); border-radius:999px; padding:.52rem .8rem;
                 font:inherit; font-size:.84rem; font-weight:700; cursor:pointer; }}
  .tab-button:hover {{ color:var(--text); border-color:var(--muted); }}
  .tab-button[aria-selected="true"],.tab-button[aria-pressed="true"] {{
    color:var(--paper); background:var(--text); border-color:var(--text);
  }}
  .focus-panel {{ background:var(--paper); border:1px solid var(--grid); border-radius:.65rem;
                  padding:1rem 1.2rem; box-shadow:0 12px 35px rgb(40 34 24 / 6%); }}
  .recent-shell {{ display:grid; grid-template-columns:minmax(0,1fr) 18rem; gap:1rem;
                   align-items:start; }}
  .context-panel {{ background:var(--text); color:var(--paper); border-radius:.65rem;
                    padding:1.1rem; position:sticky; top:1rem; }}
  .context-panel .eyebrow {{ color:#B9B5AD; }}
  .context-panel h3 {{ color:var(--paper); font-size:1.45rem; }}
  .party-rates {{ display:grid; grid-template-columns:1fr 1fr; gap:.5rem; }}
  .party-rate {{ border-radius:.35rem; padding:.55rem .6rem; font-size:.78rem; }}
  .party-rate b {{ display:block; font-size:1.45rem; line-height:1; margin-bottom:.2rem; }}
  .party-rate.democratic {{ color:{theme.BLUE}; background:{theme.tint(theme.BLUE, 0.88)}; }}
  .party-rate.republican {{ color:{theme.ACCENT}; background:{theme.tint(theme.ACCENT, 0.88)}; }}
  .context-panel .comparison {{ font-weight:750; margin:.8rem 0 .35rem; }}
  .context-panel .leader {{ color:#CBC7BE; margin:.25rem 0 0; font-size:.86rem; }}
  .top-members ol {{ margin:.4rem 0 0; padding-left:1.3rem; }}
  .top-members li {{ margin:.25rem 0; }}
  .eyebrow {{ text-transform:uppercase; letter-spacing:.12em; font-size:.7rem; font-weight:800;
              color:var(--muted); margin:0 0 .35rem; }}
  .methodology {{ background:var(--paper); border:1px solid var(--grid); margin:1rem 0;
                  padding:.75rem 1rem; border-radius:.45rem; }}
  .methodology summary {{ cursor:pointer; font-weight:bold; }}
  .methodology-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr));
                       gap:1rem; margin-top:.8rem; }}
  .methodology-grid p {{ margin:.2rem 0; color:var(--muted); }}
  .chart-card {{ background:var(--paper); border:1px solid var(--grid); margin:1rem 0;
                 padding:.75rem; border-radius:.65rem; }}
  .chart-card figcaption {{ color:var(--muted); font-size:.9rem; padding:.3rem .35rem 0; }}
  .interactive-chart {{ display:grid; gap:1rem; }}
  .mini-chart {{ position:relative; border-top:1px solid var(--grid); padding:.75rem .25rem 0;
                 min-width:0; }}
  .mini-chart:first-child {{ border-top:0; }}
  .mini-chart-heading h3 {{ font-size:1.45rem; }}
  .mini-chart-heading p {{ margin:.15rem 0 .35rem; }}
  .mini-chart svg {{ display:block; width:100%; height:auto; overflow:visible; }}
  .mini-chart svg text {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
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
                    padding:.45rem .55rem; font: .82rem/1.35 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                    box-shadow:0 2px 8px rgb(0 0 0 / 20%); }}
  .data-mark {{ cursor:pointer; transition:opacity .12s ease, filter .12s ease; }}
  .data-mark:hover,.data-mark:focus {{ opacity:.72; filter:brightness(.88); outline:none; }}
  .sr-only {{ position:absolute !important; width:1px !important; height:1px !important;
              padding:0 !important; margin:-1px !important; overflow:hidden !important;
              clip:rect(0,0,0,0) !important; white-space:nowrap !important; border:0 !important; }}
  .error {{ color:#8A1C1C; font-weight:bold; }}
  .table-wrap {{ width:100%; max-width:100%; overflow-x:auto; }}
  .term-explorer-grid {{ display:grid; grid-template-columns:minmax(0,1.1fr) minmax(24rem,.9fr);
                         gap:1rem; align-items:start; }}
  .term-explorer-grid .card {{ margin:0; height:100%; }}
  .state-map-card figcaption {{ margin:0 0 .75rem; }}
  .state-map-card figcaption strong {{ display:block; font-family:'Iowan Old Style',
                                       'Palatino Linotype',Georgia,serif; font-size:1.35rem; }}
  #state-term-map svg {{ display:block; width:100%; height:auto; }}
  #state-term-map svg text {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
  .map-legend {{ margin:.5rem 0 0; }}
  #language-tables .card {{ border:0; box-shadow:none; padding:0; margin:0; }}
  table {{ border-collapse:collapse; width:100%; font-size:.92rem; }}
  th,td {{ padding:.48rem .55rem; border-bottom:1px solid var(--grid); text-align:left; }}
  th {{ border-bottom:2px solid var(--grid); white-space:nowrap; }}
  td.num {{ font-variant-numeric:tabular-nums; }}
  img {{ width:100%; height:auto; }}
  li {{ margin:.4rem 0; }}
  footer {{ margin-top:3rem; color:var(--muted); font-size:.86rem; }}
  @media (max-width:44rem) {{
    body {{ padding:1.5rem .75rem 3rem; }}
    .explorer-controls,.recent-controls,.term-controls {{ grid-template-columns:1fr 1fr; }}
    .overview-intro,.methodology-grid,.recent-shell,.term-explorer-grid {{
      grid-template-columns:1fr;
    }}
    .context-panel {{ position:static; }}
    .chart-card {{ padding:.3rem; }}
    #language-tables table {{ font-size:.78rem; table-layout:fixed; }}
    #language-tables th,#language-tables td {{ padding:.32rem .25rem; overflow-wrap:anywhere; }}
    #language-tables th:nth-child(4),#language-tables td:nth-child(4),
    #language-tables th:nth-child(7),#language-tables td:nth-child(7) {{ display:none; }}
  }}
</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to content</a>
<nav aria-label="Primary"><a href="./" aria-current="page">The Language of Congress</a>
<a href="activity/">Member activity and bills</a></nav>
<main id="main-content">
<h1>The Language of Congress</h1>
<p class="sub hero-deck">How Democratic and Republican language in the Congressional Record has changed,
from courtesy and bipartisan cooperation to personal disrespect, misconduct allegations,
and profanity.</p>
<section class="overview" aria-labelledby="overview-heading">
<div class="overview-intro">
<div><p class="eyebrow">1873-present</p><h2 id="overview-heading">The long-run picture</h2>
<p>Choose a measure to compare Democratic and Republican floor language across the full
digital and historical record. Rates are word-normalized; positive and negative measures
remain separate.</p></div>
</div>
<div class="explorer-controls">
<label>Measure<select id="long-run-metric"></select></label>
<label>Chamber<select id="long-run-chamber"></select></label>
</div>
<div id="long-run-chart" class="focus-panel"
 aria-label="Interactive long-run Democratic and Republican language chart"></div>
<p class="definition">{html.escape(long_run['source_note'])}</p>
</section>
<div id="coverage-warning" class="warning" {'hidden' if not warning else ''}>{html.escape(warning)}</div>
<p id="dashboard-error" class="error" role="alert" hidden></p>
<section class="language" aria-labelledby="language-heading">
<div class="section-header">
<div><h2 id="language-heading">Recent language on the floor</h2>
<p class="sub">Three transparent lexical measures are shown separately: profanity,
personal hostility or disrespect, and misconduct allegations. They describe language in
attributed floor remarks and compare Democrats with Republicans; they do not establish intent
or whether an allegation is true. Named-member results begin in 1994; this view opens with
the last five Congresses.</p></div>
</div>
<div class="explorer-controls recent-controls">
<label>Measure<select id="recent-metric"></select></label>
<label>Chamber<select id="recent-chamber"></select></label>
<label>View<select id="recent-view"></select></label>
<label>Congress<select id="congress">{''.join(options)}</select></label>
</div>
<div class="recent-shell">
<div>
<div id="recent-visual" class="focus-panel"></div>
<div id="language-tables" class="focus-panel" hidden>{language_cards}</div>
</div>
<aside id="language-highlight" class="context-panel"
 aria-label="Selected Congress language summary"></aside>
</div>
<details class="methodology">
<summary>Methodology and limitations</summary>
<div class="methodology-grid">
<div><h3>What is shown</h3><p id="language-shown">{html.escape(explanation['shown'])}</p></div>
<div><h3>What is examined</h3><p id="language-examined">{html.escape(explanation['examined'])}</p></div>
<div><h3>Limits</h3><p id="language-limitation">{html.escape(explanation['limitation'])}</p></div>
</div>
</details>
<noscript><img src="figures/language_trends.png"
 alt="{html.escape(language['trend_alt'], quote=True)}"></noscript>
</section>
<section class="language" aria-labelledby="term-leaders-heading">
<div class="section-header">
<div><p class="eyebrow" id="term-leaders-scope">{html.escape(language['scope_label'])} · House + Senate</p>
<h2 id="term-leaders-heading">Who uses each term the most?</h2>
<p class="sub">For every observed term family in the conservative codebook, this table shows
the member with the most accepted, unquoted uses. Members tied for the highest count are shown
together. Inflected, plural, spacing, and spelling variants—such as “damn” and “damned”—are
grouped before ranking, as are phrasal forms that use the same base expletive, such as “fuck”
and “fuck you”; raw forms remain in the downloadable data. “Total uses” is the family’s total
across all attributed members in that scope. The
codebook favors precision over completeness and is not an exhaustive list of every possible
curse word.</p>
<p class="definition" id="term-leaders-note">{
    "Counts include accepted, unquoted uses by attributed members."
    if language["profanity_term_detail_available"]
    else "Term-level detail has not been backfilled for this historical scope."
}</p></div>
</div>
<div class="explorer-controls term-controls">
<label>Table<select id="term-view"></select></label>
<label>Party<select id="term-party"></select></label>
<label>Chamber<select id="term-chamber"></select></label>
</div>
<div class="term-explorer-grid">
<div class="card table-wrap">{_term_leaders_table(
    language['profanity_term_leaders'],
    available=language['profanity_term_detail_available'],
)}</div>
<figure class="card state-map-card">
<figcaption><strong>Most-used term by state</strong>
The leading grouped term among attributed members from each state in the selected scope.</figcaption>
<div id="state-term-map"></div>
</figure>
</div>
</section>
<details class="methodology"><summary>Data notes and exclusions</summary><ul>{caveats}</ul></details>
</main>
<footer id="coverage">Speech coverage {html.escape(payload['coverage']['speech_first_date'])}
to {html.escape(payload['coverage']['speech_last_date'])}. Newest Congressional Record date:
{html.escape(payload['coverage']['speech_last_date'])}. Site data snapshot:
{html.escape(payload['generated_utc'])}. <a href="activity/">Open member activity and bill tables.</a></footer>
<script>
const longRunLanguage = {_script_json(long_run)};
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
    updateHash({{congress: value}});
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
const requestedState = new URLSearchParams(location.hash.slice(1));
if (longRunLanguage.metrics[requestedState.get('longMetric')]) {{
  selectedLongRunMetric = requestedState.get('longMetric');
}}
if (['all', 'house', 'senate'].includes(requestedState.get('longChamber'))) {{
  selectedLongRunChamber = requestedState.get('longChamber');
}}
if (initialLanguage.metrics[requestedState.get('metric')]) {{
  selectedRecentMetric = requestedState.get('metric');
}}
if (['all', 'house', 'senate'].includes(requestedState.get('chamber'))) {{
  selectedRecentChamber = requestedState.get('chamber');
}}
if (['leaders', 'frequency'].includes(requestedState.get('termView'))) {{
  selectedTermView = requestedState.get('termView');
}}
if (['all', 'D', 'R', 'I', 'other'].includes(requestedState.get('termParty'))) {{
  selectedTermParty = requestedState.get('termParty');
}}
if (['all', 'house', 'senate'].includes(requestedState.get('termChamber'))) {{
  selectedTermChamber = requestedState.get('termChamber');
}}
if (['trend', 'members', 'table'].includes(requestedState.get('view'))) {{
  selectedRecentView = requestedState.get('view');
}}
renderLongRun(longRunLanguage);
renderLanguage(initialLanguage);
let resizeTimer;
window.addEventListener('resize', () => {{
  window.clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => {{
    if (currentLongRun) renderLongRun(currentLongRun);
    if (currentLanguage) renderRecentFocus();
  }}, 120);
}});
const requestedCongress = requestedState.get('congress');
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
    options = [
        f'<option value="all"{all_selected}>{ALL_MEMBER_SCOPE_LABEL}</option>'
    ]
    if len(congresses) >= 5:
        options.append('<option value="recent5">Last 5 Congresses</option>')
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
    metric_options = "".join(
        f'<option value="{metric}">{html.escape(label)}</option>'
        for metric, label in (
            ("speech", "Speech"),
            ("sponsored", "Sponsored bills"),
            ("passed", "Passed a chamber"),
            ("enacted", "Became law"),
            ("profanity", "Profanity"),
        )
    )
    cards = "".join(
        f'<section class="card" id="{metric}"><h2>{html.escape(title)}</h2>'
        f'<p class="definition">{html.escape(METRIC_DEFINITIONS[metric])}</p>'
        f'<div class="table-wrap">{_table(metric, payload["leaderboards"][metric])}</div></section>'
        for metric, title in sections
    )
    caveats = "".join(
        f"<li>{html.escape(item)}</li>"
        for index, item in enumerate(CAVEATS)
        if index != 3
    )
    warning = payload["coverage"]["warning"]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Congressional member activity and bills</title>
<meta name="description" content="Exact-value congressional speech, bill sponsorship,
passage, enactment, and profanity tables by Congress.">
<link rel="canonical" href="{PUBLIC_URL}activity/">
<style>
  :root {{ --bg:#F3F0E8; --text:#171717; --muted:#68655F;
           --grid:#D8D3C9; --blue:{theme.BLUE}; --paper:#FFFEFA; --soft:#EAE5DA; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text);
          font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          margin:0 auto; padding:1.4rem 1.25rem 4rem; max-width:74rem; line-height:1.55; }}
  h1,h2 {{ font-family:'Iowan Old Style','Palatino Linotype',Georgia,serif; }}
  h1 {{ font-size:clamp(2.4rem,5vw,4.2rem); line-height:1; letter-spacing:-.04em;
        margin:2.4rem 0 1rem; }}
  h2 {{ font-size:1.7rem; margin:.1rem 0 .3rem; }}
  a {{ color:var(--blue); }}
  nav {{ display:flex; gap:.35rem; align-items:center; border-bottom:1px solid var(--grid);
         padding-bottom:.9rem; }}
  nav a {{ color:var(--muted); text-decoration:none; padding:.4rem .7rem; border-radius:999px;
           font-size:.88rem; font-weight:650; }}
  nav a:hover {{ background:var(--soft); color:var(--text); }}
  nav a[aria-current="page"] {{ color:var(--paper); background:var(--text); }}
  .skip-link {{ position:absolute; left:-9999px; top:.5rem; z-index:10;
                background:var(--text); color:var(--paper); padding:.55rem .75rem; }}
  .skip-link:focus {{ left:.5rem; }}
  .sub,.definition,.muted {{ color:var(--muted); }}
  .hero-deck {{ font-size:1.08rem; max-width:48rem; }}
  .toolbar {{ display:flex; justify-content:space-between; gap:1rem; align-items:center;
              margin:2rem 0 1rem; }}
  .toolbar label {{ display:grid; gap:.3rem; color:var(--muted); font-size:.72rem;
                    font-weight:800; letter-spacing:.08em; text-transform:uppercase; }}
  .toolbar select {{ color:var(--text); text-transform:none; letter-spacing:normal;
                     font-weight:650; min-width:12rem; }}
  select {{ font:inherit; padding:.6rem 2.2rem .6rem .8rem; background:var(--paper);
            border:1px solid var(--grid); border-radius:.45rem; }}
  .tab-row {{ display:flex; gap:.4rem; flex-wrap:wrap; margin:.9rem 0 1.2rem; }}
  .tab-button {{ appearance:none; border:1px solid var(--grid); background:transparent;
                 color:var(--muted); border-radius:999px; padding:.52rem .8rem;
                 font:inherit; font-size:.84rem; font-weight:700; cursor:pointer; }}
  .tab-button[aria-pressed="true"] {{ color:var(--paper); background:var(--text);
                                     border-color:var(--text); }}
  .warning {{ background:#FFF3CD; border-left:4px solid #C7922B; padding:.8rem 1rem; margin:1rem 0; }}
  .error {{ color:#8A1C1C; font-weight:bold; }}
  .card {{ background:var(--paper); border:1px solid var(--grid); border-radius:.65rem;
           padding:1.2rem; margin:0 0 2rem; box-shadow:0 12px 35px rgb(40 34 24 / 6%); }}
  .notes {{ background:var(--paper); border:1px solid var(--grid); border-radius:.45rem;
            padding:.75rem 1rem; margin:1rem 0; }}
  .notes summary {{ cursor:pointer; font-weight:750; }}
  .sr-only {{ position:absolute !important; width:1px !important; height:1px !important;
              padding:0 !important; margin:-1px !important; overflow:hidden !important;
              clip:rect(0,0,0,0) !important; white-space:nowrap !important; border:0 !important; }}
  .table-wrap {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:.92rem; }}
  th,td {{ padding:.48rem .55rem; border-bottom:1px solid var(--grid); text-align:left; }}
  th {{ border-bottom:2px solid var(--grid); white-space:nowrap; }}
  td.num {{ font-variant-numeric:tabular-nums; }}
  li {{ margin:.4rem 0; }}
  footer {{ margin-top:3rem; color:var(--muted); font-size:.86rem; }}
  @media (max-width:44rem) {{
    body {{ padding:1.5rem .75rem 3rem; }}
    .toolbar {{ display:block; }}
    .toolbar label {{ display:grid; margin-top:.75rem; }}
    table {{ table-layout:fixed; font-size:.78rem; }}
    th,td {{ padding:.36rem .28rem; overflow-wrap:normal; }}
    table[data-metric="speech"] th:nth-child(3),table[data-metric="speech"] td:nth-child(3),
    table[data-metric="speech"] th:nth-child(4),table[data-metric="speech"] td:nth-child(4),
    table[data-metric="speech"] th:nth-child(5),table[data-metric="speech"] td:nth-child(5),
    table[data-metric="speech"] th:nth-child(7),table[data-metric="speech"] td:nth-child(7),
    table[data-metric="speech"] th:nth-child(8),table[data-metric="speech"] td:nth-child(8),
    table[data-metric="sponsored"] th:nth-child(4),table[data-metric="sponsored"] td:nth-child(4),
    table[data-metric="sponsored"] th:nth-child(6),table[data-metric="sponsored"] td:nth-child(6),
    table[data-metric="sponsored"] th:nth-child(7),table[data-metric="sponsored"] td:nth-child(7),
    table[data-metric="sponsored"] th:nth-child(8),table[data-metric="sponsored"] td:nth-child(8),
    table[data-metric="passed"] th:nth-child(4),table[data-metric="passed"] td:nth-child(4),
    table[data-metric="passed"] th:nth-child(6),table[data-metric="passed"] td:nth-child(6),
    table[data-metric="passed"] th:nth-child(7),table[data-metric="passed"] td:nth-child(7),
    table[data-metric="passed"] th:nth-child(8),table[data-metric="passed"] td:nth-child(8),
    table[data-metric="enacted"] th:nth-child(4),table[data-metric="enacted"] td:nth-child(4),
    table[data-metric="enacted"] th:nth-child(6),table[data-metric="enacted"] td:nth-child(6),
    table[data-metric="enacted"] th:nth-child(7),table[data-metric="enacted"] td:nth-child(7),
    table[data-metric="enacted"] th:nth-child(8),table[data-metric="enacted"] td:nth-child(8),
    table[data-metric="profanity"] th:nth-child(4),table[data-metric="profanity"] td:nth-child(4),
    table[data-metric="profanity"] th:nth-child(8),table[data-metric="profanity"] td:nth-child(8),
    table[data-metric="profanity"] th:nth-child(9),table[data-metric="profanity"] td:nth-child(9) {{
      display:none;
    }}
  }}
</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to content</a>
<nav aria-label="Primary"><a href="../">The Language of Congress</a>
<a href="./" aria-current="page">Member activity and bills</a></nav>
<main id="main-content">
<h1>Congressional member activity and bills</h1>
<p class="sub hero-deck">Exact-value tables for attributed speech, sponsored bills, passage,
enactment, and nonzero profanity rates. Named-member speech coverage begins January 25, 1994;
“all available Congresses” does not include the 1873–1993 aggregate-only period. The
language-analysis homepage remains the primary view.</p>
<div class="toolbar">
<label for="activity-metric">Table<select id="activity-metric">{metric_options}</select></label>
<label for="congress">Congress<select id="congress">{''.join(options)}</select></label>
</div>
<div id="coverage-warning" class="warning" {'hidden' if not warning else ''}>{html.escape(warning)}</div>
<p id="dashboard-error" class="error" role="alert" hidden></p>
<div id="leaderboards">{cards}</div>
<details class="notes"><summary>Data notes and exclusions</summary><ul>{caveats}</ul></details>
</main>
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
    latest_congress = int(daily["congress"].max())
    incomplete_terms = incomplete_profanity_term_rows(
        daily[daily["congress"] == latest_congress]
    )
    if not incomplete_terms.empty:
        LOG.error(
            "%d current-Congress speaker-day rows have incomplete profanity term counts; "
            "finish the backfill before publishing",
            len(incomplete_terms),
        )
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
    long_run = load_long_run_payload()
    (data / "long_run_language.json").write_text(
        json.dumps(long_run, indent=2) + "\n",
        encoding="utf-8",
    )

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
    if len(available) >= 5:
        recent_congresses = available[-5:]
        recent_daily = daily[daily["congress"].isin(recent_congresses)]
        recent_bills = bills[bills["congress"].isin(recent_congresses)]
        payloads["recent5"] = build_payload(
            recent_daily,
            recent_bills,
            None,
            top=args.top,
            min_words=args.min_words,
            generated_utc=generated_utc,
            scope_label=(
                f"Last 5 Congresses ({recent_congresses[0]}–{recent_congresses[-1]})"
            ),
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
    homepage_selected = payloads.get("recent5", selected)
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
        _render_html(homepage_selected, available, long_run), encoding="utf-8"
    )
    activity_dir = out / "activity"
    activity_dir.mkdir(parents=True, exist_ok=True)
    (activity_dir / "index.html").write_text(
        _render_activity_html(selected, available), encoding="utf-8"
    )
    (out / "activity.html").write_text(
        '<!doctype html><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=activity/">'
        '<meta name="description" content="Redirect to congressional member activity and bills.">'
        f'<link rel="canonical" href="{PUBLIC_URL}activity/">'
        '<title>Redirecting…</title><h1>Congressional member activity and bills</h1>'
        '<a href="activity/">Open member activity and bills</a>',
        encoding="utf-8",
    )
    LOG.info("site written to %s (%s)", out, selected["label"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
