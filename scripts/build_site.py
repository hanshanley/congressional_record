#!/usr/bin/env python3
"""Build the static congressional activity and language dashboard."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
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
from analysis.daily_language import restrict_to_complete_metrics  # noqa: E402
from analysis.plotting import charts, site_charts, theme  # noqa: E402
from analysis.score.registry import HEADLINE_METRICS  # noqa: E402
from analysis.speakers import (  # noqa: E402
    LANGUAGE_METRICS,
    incomplete_profanity_term_rows,
    language_member_rates,
    language_metric_available,
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
PUBLIC_URL = "https://www.themarginoferror.com/congressional_profanity/"
SUBSCRIBE_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSeHPhVHdJ0xdCYq3wDYjkOIxgVdErP4qszNOBQYrnyzTz3xyQ/viewform"
)
SOURCE_REPOSITORY_URL = "https://github.com/hanshanley/congressional_record"
ALL_MEMBER_SCOPE_LABEL = "1994–present"

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
SLUR_CAVEAT = (
    "Slurs use an exact list of ethnic, sexual-orientation, gender-identity, and disability "
    "slurs used in the United States, drawn from Wikipedia's slur lists and cross-checked "
    "against a Kaggle profanity collection. Forms that are also ordinary words, names, "
    "places, or clinical terms are left out, as are verb uses such as \"retard the growth.\" Quoted "
    "slurs are excluded, but a member who repeats a slur to condemn or describe it is still "
    "counted, so a count is not evidence of endorsement."
)

METRIC_DEFINITIONS = {
    "speech": "Attributed non-procedural spoken words; turns and active days provide context.",
    "sponsored": "Distinct H.R. and S. bills for which the member is the official sponsor.",
    "passed": "Sponsored bills with an official measure-level House or Senate passage action.",
    "enacted": "Sponsored bills with an assigned public or private law number.",
    "profanity": "Unquoted curated profanity hits per 100,000 attributed words.",
}
ACTIVITY_NUMERIC_COLUMNS = {
    "speech": [0, 5, 6, 7],
    "sponsored": [0, 4, 5, 6],
    "passed": [0, 4, 5, 6],
    "enacted": [0, 4, 5, 6],
    "profanity": [0, 5, 6, 7, 8],
}

LANGUAGE_MEMBER_TOP = 8

INTERACTIVE_CHART_JS = r"""
const SVG_NS = 'http://www.w3.org/2000/svg';
const chartColors = {
  D: '#3D6F8C',
  R: '#A9442E',
  I: '#4A7C59',
  other: '#655F55',
  text: '#171713',
  muted: '#655F55',
  grid: '#D9D1C2',
  paper: '#F2EFE7',
};
let selectedLongRunMetric = 'profanity_per_1k';
let selectedLongRunChamber = 'all';
let selectedRecentMetric = 'profanity';
let selectedRecentView = 'trend';
let selectedRecentChamber = 'all';
let selectedTermView = 'leaders';
let selectedTermParty = 'all';
let selectedTermChamber = 'all';
let showAllTerms = false;
const TERM_INITIAL_ROWS = 12;
const termLeaderDescription = document.getElementById('term-leaders-description')
  .textContent.trim().replace(/\s+/g, ' ');
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

function censorTerm(term) {
  const [first, ...rest] = String(term).split(' ');
  const letterCount = [...first].filter(character => /[A-Za-z]/.test(character)).length;
  let letterIndex = 0;
  const censored = [...first].map(character => {
    if (!/[A-Za-z]/.test(character)) return character;
    const keep = letterIndex < (letterCount >= 4 ? 2 : 1) || letterIndex === letterCount - 1;
    letterIndex += 1;
    return keep ? character : '*';
  }).join('');
  return [censored, ...rest].join(' ');
}

function censoredTermNode(term, detail = '') {
  const value = String(term);
  const node = document.createElement('span');
  node.className = 'censored-term';
  node.tabIndex = 0;
  node.textContent = censorTerm(value);
  node.dataset.term = value;
  node.setAttribute('aria-label', value);
  node.title = detail ? `Uncensored term: ${value}. ${detail}` : `Uncensored term: ${value}`;
  return node;
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
  subtitle.textContent = detail || '';
  heading.append(title, subtitle);
  if (metric.definition) {
    const definition = document.createElement('details');
    definition.className = 'chart-definition';
    const summary = document.createElement('summary');
    summary.textContent = 'What this measure counts';
    const text = document.createElement('p');
    text.className = 'definition';
    text.textContent = metric.definition;
    definition.append(summary, text);
    heading.appendChild(definition);
  }
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
      'text-anchor': 'end', fill: chartColors.muted, 'font-size': compact ? 9 : 9.5,
    });
  }
  const yearLabelIndices = new Set(spacedLabelIndices(years.length, compact ? 4 : 7));
  years.forEach((year, index) => {
    if (!yearLabelIndices.has(index)) return;
    addSvgText(svg, x(year), height - 14, String(year), {
      'text-anchor': 'middle', fill: chartColors.muted, 'font-size': compact ? 9 : 9.5,
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
      'text-anchor': 'end', fill: chartColors.text, 'font-size': compact ? 11 : 15,
      'data-party': row.party || 'other', class: 'svg-label',
    });
    const bar = svgNode('rect', {
      x: margin.left, y: py + 5, width: Math.max(1, barWidth), height: 23, rx: 0,
      fill: chartColors[row.party] || chartColors.other,
      'data-party': row.party || 'other',
    });
    const termDetail = key === 'profanity' && row.favorite_profanity_term
      ? `; most-used term: “${censorTerm(row.favorite_profanity_term)}” ` +
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
        fill: chartColors.text, 'font-size': compact ? 10 : 12,
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
      } else if (key === 'profanity' && index === 4 && value !== '—') {
        cell.appendChild(censoredTermNode(value));
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
      item.textContent = `${member.speaker_name} (${member.party}) — ` +
        `${formatRate(member[metric.rate])}`;
      if (selectedRecentMetric === 'profanity' && member.favorite_profanity_term) {
        item.append(' · ', censoredTermNode(member.favorite_profanity_term));
      }
      list.appendChild(item);
    });
    leader.append(label, list);
  } else {
    leader.textContent = 'No nonzero member rate in this view';
  }
  const summary = document.createElement('div');
  summary.className = 'summary-heading';
  summary.append(eyebrow, title, comparisonText);
  container.append(summary, rates, leader);
}

function renderRecentFocus() {
  if (!currentLanguage) return;
  if (!currentLanguage.metrics[selectedRecentMetric]) {
    selectedRecentMetric = Object.keys(currentLanguage.metrics)[0];
  }
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

function termUsageNode(item) {
  const wrapper = document.createElement('div');
  wrapper.className = 'term-usage';
  const unit = Number(item.leader_hits) === 1 ? 'use' : 'uses';
  const subject = item.leaders.length > 1 ? 'by each leading member' : 'by the leading member';
  const aria = `${Number(item.leader_hits).toLocaleString()} ${unit} ${subject} ` +
    `out of ${Number(item.total_hits).toLocaleString()} uses by all members`;
  wrapper.setAttribute('aria-label', aria);
  const count = document.createElement('strong');
  count.textContent = `${Number(item.leader_hits).toLocaleString()}` +
    `${item.leaders.length > 1 ? ' each' : ''}`;
  const total = document.createElement('span');
  total.textContent = `/ ${Number(item.total_hits).toLocaleString()} total`;
  wrapper.append(count, total);
  return wrapper;
}

function partyBadges(parties) {
  const wrapper = document.createElement('span');
  wrapper.className = 'party-badges';
  parties.forEach(party => {
    const badge = document.createElement('span');
    badge.className = `party-badge party-${String(party).toLowerCase()}`;
    badge.textContent = party;
    wrapper.appendChild(badge);
  });
  return wrapper;
}

function renderTermTable(language, summaries) {
  const table = document.getElementById('term-leaders-table');
  table.dataset.view = selectedTermView;
  table.closest('.term-explorer-grid').dataset.view = selectedTermView;
  table.querySelector('caption').textContent = selectedTermView === 'leaders'
    ? 'Leading members by term' : 'Term frequency and share of accepted uses';
  const head = table.querySelector('thead tr');
  const body = document.querySelector('#term-leaders-table tbody');
  head.replaceChildren();
  body.replaceChildren();
  const available = recentTermDetailAvailable(language);
  const headers = selectedTermView === 'leaders'
    ? ['Term', 'Member(s) with most uses', 'Leader / all uses']
    : ['#', 'Term', 'Uses', 'Share'];
  const numericColumns = selectedTermView === 'leaders' ? [2] : [0, 2, 3];
  headers.forEach((label, index) => {
    const cell = document.createElement('th');
    cell.scope = 'col';
    cell.textContent = label;
    if (numericColumns.includes(index)) cell.classList.add('num');
    if (selectedTermView === 'frequency' && index === 0) cell.classList.add('rank');
    head.appendChild(cell);
  });
  if (!available) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = headers.length;
    cell.className = 'muted';
    cell.textContent = 'Term-level detail is not available for this historical scope.';
    row.appendChild(cell);
    body.appendChild(row);
  } else if (!summaries.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = headers.length;
    cell.className = 'muted';
    cell.textContent = 'No accepted term uses were observed in this scope.';
    row.appendChild(cell);
    body.appendChild(row);
  }
  const allHits = summaries.reduce((sum, item) => sum + item.total_hits, 0);
  summaries.forEach((item, index) => {
    const row = document.createElement('tr');
    row.hidden = !showAllTerms && index >= TERM_INITIAL_ROWS;
    if (selectedTermView === 'leaders') {
      const detail = item.variants.length > 1
        ? `Grouped forms: ${item.variants.join(', ')}` : '';
      const term = censoredTermNode(item.term, detail);
      const leaders = document.createElement('span');
      item.leaders.forEach((leader, leaderIndex) => {
        if (leaderIndex) leaders.appendChild(document.createTextNode(', '));
        const link = document.createElement('a');
        link.href = `https://bioguide.congress.gov/search/bio/${leader.bioguide}`;
        link.textContent = leader.speaker_name;
        leaders.append(link, partyBadges([leader.party]));
      });
      appendCell(row, term);
      appendCell(row, leaders);
      appendCell(row, termUsageNode(item), 'num');
    } else {
      appendCell(row, String(index + 1), 'num rank');
      const detail = item.variants.length > 1
        ? `Grouped forms: ${item.variants.join(', ')}` : '';
      appendCell(row, censoredTermNode(item.term, detail));
      appendCell(row, item.total_hits.toLocaleString(), 'num count');
      appendCell(row, allHits ? `${(100 * item.total_hits / allHits).toFixed(1)}%` : '0.0%', 'num share');
    }
    body.appendChild(row);
  });
  const toggle = document.getElementById('term-row-toggle');
  const remaining = Math.max(0, summaries.length - TERM_INITIAL_ROWS);
  toggle.hidden = remaining === 0;
  toggle.textContent = showAllTerms
    ? 'Show fewer terms'
    : `Show ${remaining.toLocaleString()} more term${remaining === 1 ? '' : 's'}`;
  toggle.setAttribute('aria-expanded', String(showAllTerms));
  toggle.onclick = () => {
    showAllTerms = !showAllTerms;
    renderTermTable(language, summaries);
  };
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
    'aria-label': `Most-used profanity term by state in ${language.scope_label}`,
  });
  const title = svgNode('title', {}, `Most-used profanity term by state in ${language.scope_label}`);
  svg.appendChild(title);
  Object.entries(STATE_TILES).forEach(([state, [column, row]]) => {
    const winner = winners.get(state);
    const ratio = winner ? Math.sqrt(winner.hits / maxHits) : 0;
    const shades = ['#E8E2D5', '#D8CFBD', '#C2B59C', '#9C8E74', '#675D4D', '#2D2A24'];
    const shade = winner ? shades[Math.min(5, Math.max(1, Math.ceil(ratio * 5)))] : shades[0];
    const group = svgNode('g', {transform: `translate(${column * 78 + 10} ${row * 75 + 18})`});
    const tile = svgNode('rect', {
      width: 70, height: 67, rx: 0, fill: shade, stroke: chartColors.paper, 'stroke-width': 2,
    });
    const censoredLabel = winner ? winner.terms.map(censorTerm).join(' / ') : '';
    const tooltip = winner
      ? `${state}: ${censoredLabel} (${winner.hits.toLocaleString()} uses)`
      : `${state}: no accepted uses`;
    tile.setAttribute('tabindex', '0');
    tile.setAttribute('aria-label', tooltip);
    tile.appendChild(svgNode('title', {}, tooltip));
    group.appendChild(tile);
    const dark = ratio > 0.62;
    group.appendChild(svgNode('text', {
      x: 8, y: 18, fill: dark ? chartColors.paper : chartColors.text, 'font-size': 12,
      'font-weight': 700,
    }, state));
    if (winner) {
      const label = censoredLabel;
      group.appendChild(svgNode('text', {
        x: 35, y: 42, fill: dark ? chartColors.paper : chartColors.text, 'font-size': 10,
        'text-anchor': 'middle', 'font-weight': 700,
      }, label.length > 11 ? `${label.slice(0, 10)}…` : label));
      group.appendChild(svgNode('text', {
        x: 35, y: 56, fill: dark ? '#E8E2D5' : chartColors.muted, 'font-size': 9,
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
  document.querySelectorAll('#term-view button').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.view === selectedTermView));
    button.onclick = () => {
      selectedTermView = button.dataset.view;
      updateHash({termView: selectedTermView});
      renderTermExplorer(currentLanguage);
    };
  });
  document.getElementById('term-leaders-heading').textContent = selectedTermView === 'leaders'
    ? 'Who uses each term the most?' : 'Most-used terms';
  document.getElementById('term-leaders-description').textContent = selectedTermView === 'leaders'
    ? termLeaderDescription
    : 'Terms ranked by accepted, unquoted uses in the selected scope. ' +
      'Share is each term’s percentage of those uses. Related forms are grouped; ' +
      'hover over or focus a censored term to reveal it.';
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
  const note = document.getElementById('term-leaders-note');
  note.hidden = available;
  note.textContent = available
    ? ''
    : 'Term-level detail has not been backfilled for this historical scope.';
}
"""

ACTIVITY_JS = r"""
const DATA_ROOT = '../data';
const select = document.getElementById('congress');
let loadedCongress = select.value;
let loadSequence = 0;
let selectedActivityMetric = 'speech';

function censorTerm(term) {
  const [first, ...rest] = String(term).split(' ');
  const letterCount = [...first].filter(character => /[A-Za-z]/.test(character)).length;
  let letterIndex = 0;
  const censored = [...first].map(character => {
    if (!/[A-Za-z]/.test(character)) return character;
    const keep = letterIndex < (letterCount >= 4 ? 2 : 1) || letterIndex === letterCount - 1;
    letterIndex += 1;
    return keep ? character : '*';
  }).join('');
  return [censored, ...rest].join(' ');
}

function censoredTermNode(term) {
  const value = String(term);
  const node = document.createElement('span');
  node.className = 'censored-term';
  node.tabIndex = 0;
  node.textContent = censorTerm(value);
  node.dataset.term = value;
  node.setAttribute('aria-label', value);
  node.title = `Uncensored term: ${value}`;
  return node;
}

const activityMetrics = [
  ['speech', 'Speech'],
  ['sponsored', 'Sponsored bills'],
  ['passed', 'Passed a chamber'],
  ['enacted', 'Became law'],
  ['profanity', 'Profanity'],
];

function selectActivityMetric(metric) {
  selectedActivityMetric = metric;
  document.querySelectorAll('#activity-metric button').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.metric === metric));
  });
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
    values.forEach((value, index) => {
      const cell = activityCell(value, index === 1 ? item.member_url : '');
      if (activityNumericColumns[metric].includes(index)) cell.classList.add('num');
      if (metric === 'profanity' && index === 4 && value !== '—') {
        cell.replaceChildren(censoredTermNode(value));
      }
      row.appendChild(cell);
    });
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
document.querySelectorAll('#activity-metric button').forEach(button => {
  button.addEventListener('click', () => selectActivityMetric(button.dataset.metric));
});
selectActivityMetric(selectedActivityMetric);
const requestedCongress = activityState.get('congress');
if (requestedCongress && [...select.options].some(option => option.value === requestedCongress)
    && requestedCongress !== select.value) {
  select.value = requestedCongress;
  loadActivityCongress(requestedCongress).catch(() => {});
}
"""

# Mirrors margin_of_error/src/styles/global.css so the dashboard reads as a page of
# The Margin of Error. The two repositories deploy separately, so the house tokens,
# masthead, and footer are duplicated here rather than linked.
HOUSE_CSS = """
  :root { --paper:#f2efe7; --paper-deep:#e8e2d5; --ink:#171713; --muted:#655f55;
          --rule:#b8b0a2; --hair:#d9d1c2; --signal:#b52a25; --signal-dark:#7e1715;
          --dem:#3D6F8C; --rep:#A9442E; --ind:#4A7C59;
          --serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Palatino,Georgia,serif;
          --sans:"Arial Narrow","Avenir Next Condensed","Franklin Gothic Medium",Arial,sans-serif;
          --mono:"SFMono-Regular",Consolas,"Liberation Mono",monospace;
          --page:min(1180px,calc(100vw - 40px)); }
  *,*::before,*::after { box-sizing:border-box; border-radius:0; }
  html { background:var(--paper); color:var(--ink); font-family:var(--serif); }
  body { margin:0; font-size:18px; line-height:1.58;
         background:linear-gradient(90deg,transparent 0,transparent calc(50% - 1px),
           rgba(23,23,19,.018) 50%,transparent calc(50% + 1px)),var(--paper); }
  a { color:inherit; text-decoration-color:var(--signal); text-decoration-thickness:1px;
      text-underline-offset:.18em; }
  a:hover { color:var(--signal-dark); }
  a:focus-visible,button:focus-visible,select:focus-visible,[tabindex]:focus-visible {
    outline:2px solid var(--signal); outline-offset:3px;
  }
  button,input,select { font:inherit; }
  img { display:block; max-width:100%; height:auto; }
  .skip-link { position:fixed; z-index:100; top:.75rem; left:.75rem; padding:.65rem .9rem;
               background:var(--ink); color:var(--paper); transform:translateY(-180%); }
  .skip-link:focus { transform:translateY(0); }
  .site-header { width:var(--page); margin:0 auto; padding-top:1rem;
                 border-bottom:3px double var(--ink); }
  .edition-line { display:flex; justify-content:space-between; padding:.35rem 0;
                  border-block:1px solid var(--ink); font-family:var(--mono); font-size:.68rem;
                  letter-spacing:.08em; text-transform:uppercase; }
  .masthead { display:grid; grid-template-columns:1fr auto; gap:2rem; align-items:end;
              padding:1.4rem 0 1rem; }
  .wordmark { display:inline-grid; width:fit-content; text-decoration:none; line-height:.82; }
  .wordmark-the { padding-left:.2rem; color:var(--signal); font-family:var(--mono);
                  font-size:.65rem; letter-spacing:.24em; text-transform:uppercase; }
  .wordmark-main { font-size:clamp(2.4rem,5.4vw,5.5rem); font-weight:700;
                   letter-spacing:-.065em; }
  .wordmark-main i { font-size:.48em; font-weight:400; }
  .masthead nav ul { display:flex; gap:1.4rem; margin:0; padding:0 0 .3rem; list-style:none;
                     font-family:var(--sans); font-size:.78rem; letter-spacing:.1em;
                     text-transform:uppercase; }
  .masthead nav a { text-decoration:none; }
  .masthead nav .subscribe-link { padding:.5rem .7rem; background:var(--signal);
                                  color:var(--paper); }
  main { min-height:60vh; }
  .feature { width:var(--page); margin:0 auto; }
  .feature-header { display:grid; grid-template-columns:minmax(0,4fr) minmax(180px,1fr);
                    column-gap:4rem; padding:clamp(2.5rem,5vw,4.5rem) 0 2rem;
                    border-bottom:1px solid var(--ink); }
  .feature-header > *:not(.byline) { grid-column:1; }
  .kicker,.eyebrow { margin:0 0 1rem; color:var(--signal-dark); font-family:var(--mono);
                     font-size:.68rem; font-weight:700; letter-spacing:.13em;
                     text-transform:uppercase; }
  h1 { margin:0; font-size:clamp(3rem,5vw,5rem); font-weight:500; letter-spacing:-.05em;
       line-height:.96; }
  h2 { margin:0; font-size:clamp(2rem,3.6vw,3rem); font-weight:500; letter-spacing:-.04em;
       line-height:1.04; }
  h3 { margin:0 0 .3rem; font-size:1.35rem; font-weight:500; letter-spacing:-.02em;
       line-height:1.15; }
  .dek { max-width:760px; margin:1.2rem 0 0; font-size:clamp(1.3rem,2.2vw,1.8rem);
         font-style:italic; line-height:1.24; }
  .byline { grid-row:1 / span 3; grid-column:2; align-self:end; padding-top:1rem;
            border-top:1px solid var(--ink); font-family:var(--mono); font-size:.68rem;
            letter-spacing:.04em; text-transform:uppercase; }
  .byline p { margin:0 0 .35rem; }
  .feature-nav { display:flex; flex-wrap:wrap; gap:.45rem 1.75rem; align-items:baseline;
                 margin:1.5rem 0 0 clamp(0px,12vw,150px); }
  .feature-nav > span { color:var(--signal); font-family:var(--mono); font-size:.62rem;
                        letter-spacing:.06em; text-transform:uppercase; }
  .feature-nav a { padding-bottom:.1rem; border-bottom:2px solid transparent;
                   font-family:var(--sans); font-size:.78rem; letter-spacing:.08em;
                   text-decoration:none; text-transform:uppercase; }
  .feature-nav a:hover { border-bottom-color:var(--rule); }
  .feature-nav a[aria-current="page"] { border-bottom-color:var(--signal); }
  .feature-section { padding-top:clamp(2.75rem,5vw,4.25rem); }
  .section-head { display:grid; grid-template-columns:minmax(140px,.7fr) minmax(0,3.3fr);
                  gap:clamp(1rem,3vw,3rem); margin:0 0 1.75rem; padding-top:1rem;
                  border-top:3px double var(--ink); }
  .section-head > .kicker { margin:.6rem 0 0; }
  .section-head .eyebrow { margin:0 0 .6rem; color:var(--muted); font-weight:400; }
  .section-dek,.sub { max-width:46rem; margin:.75rem 0 0; font-size:1.08rem; line-height:1.5; }
  .definition,.muted { color:var(--muted); }
  .definition { font-size:.95rem; }
  .control-row label,.explorer-controls label,.toolbar label {
    display:grid; gap:.3rem; color:var(--muted); font-family:var(--mono); font-size:.62rem;
    letter-spacing:.08em; text-transform:uppercase;
  }
  select { appearance:none; width:100%; min-width:0; padding:.35rem 1.5rem .4rem 0; border:0;
           border-bottom:1px solid var(--ink); color:var(--ink); cursor:pointer;
           background:transparent url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23171713'/%3E%3C/svg%3E") no-repeat right .15rem center / .6rem;
           font-family:var(--serif); font-size:1.12rem; letter-spacing:normal;
           text-transform:none; }
  select:disabled { opacity:.5; cursor:progress; }
  .tab-row { display:flex; flex-wrap:wrap; gap:.25rem 1.5rem; margin:0;
             border-bottom:1px solid var(--rule); }
  .tab-button { appearance:none; margin:0 0 -1px; padding:.5rem 0 .4rem; border:0;
                border-bottom:2px solid transparent; background:transparent; color:var(--muted);
                font-family:var(--sans); font-size:.78rem; letter-spacing:.1em;
                text-transform:uppercase; cursor:pointer; }
  .tab-button:hover { color:var(--ink); }
  .tab-button[aria-selected="true"],.tab-button[aria-pressed="true"] {
    color:var(--ink); border-bottom-color:var(--signal);
  }
  .warning { margin:2rem 0 0; padding:.15rem 0 .15rem 1.2rem; border-left:2px solid var(--signal);
             font-style:italic; }
  .error { color:var(--signal-dark); font-family:var(--mono); font-size:.75rem; }
  .card { min-width:0; margin:0 0 2.5rem; }
  .table-wrap { width:100%; max-width:100%; overflow-x:auto; border-top:3px double var(--ink); }
  table { border-collapse:separate; border-spacing:0; width:100%; font-size:.95rem; }
  th,td { padding:.55rem .6rem; border-bottom:1px solid var(--hair); text-align:left;
          vertical-align:baseline; }
  th { position:sticky; top:0; z-index:1; background:var(--paper); color:var(--muted);
       border-bottom:1px solid var(--ink); white-space:nowrap; font-family:var(--mono);
       font-size:.6rem; font-weight:400; letter-spacing:.08em; text-transform:uppercase; }
  tbody tr:hover { background:rgb(232 226 213 / 60%); }
  tbody tr:last-child td { border-bottom:0; }
  th.num,td.num { font-variant-numeric:tabular-nums; text-align:right; }
  .censored-term { position:relative; cursor:help; border-bottom:1px dotted currentColor;
                   white-space:nowrap; }
  .censored-term:hover::after,.censored-term:focus::after {
    content:attr(data-term); position:absolute; left:0; bottom:calc(100% + .35rem); z-index:5;
    background:var(--ink); color:var(--paper); padding:.3rem .45rem; font-family:var(--mono);
    font-size:.72rem; font-style:normal; font-weight:400; letter-spacing:normal;
    text-transform:none; white-space:nowrap;
  }
  .notes { margin:clamp(2.75rem,5vw,4rem) 0 0; border-block:1px solid var(--ink); }
  .notes summary { padding:.85rem 0; cursor:pointer; list-style:none; font-family:var(--mono);
                   font-size:.68rem; letter-spacing:.1em; text-transform:uppercase; }
  .notes summary::-webkit-details-marker { display:none; }
  .notes summary::before { content:"+"; display:inline-block; width:1.25rem;
                           color:var(--signal); }
  .notes[open] summary::before { content:"−"; }
  .notes ul { max-width:52rem; margin:0 0 1.25rem; padding-left:1.25rem; font-size:.98rem; }
  .notes li { margin:.55rem 0; }
  .sr-only { position:absolute !important; width:1px !important; height:1px !important;
             padding:0 !important; margin:-1px !important; overflow:hidden !important;
             clip:rect(0,0,0,0) !important; white-space:nowrap !important; border:0 !important; }
  .coverage-note { margin:2.5rem 0 0; color:var(--muted); font-family:var(--mono);
                   font-size:.66rem; letter-spacing:.04em; line-height:1.8;
                   text-transform:uppercase; }
  .site-footer { display:grid; grid-template-columns:2fr 1fr auto; gap:2rem; width:var(--page);
                 margin:3rem auto 0; padding:2rem 0 3rem; border-top:3px double var(--ink);
                 font-size:.85rem; }
  .site-footer p { margin:0; }
  .site-footer .kicker { margin-bottom:.35rem; }
  .footer-links { display:flex; flex-wrap:wrap; gap:1rem; font-family:var(--sans);
                  font-size:.7rem; text-transform:uppercase; }
  .copyright { color:var(--muted); font-family:var(--mono); font-size:.62rem;
               text-transform:uppercase; }
  @media (max-width:760px) {
    :root { --page:calc(100vw - 28px); }
    body { font-size:16px; }
    .edition-line { justify-content:end; }
    .edition-line span:first-child { display:none; }
    .masthead { grid-template-columns:1fr; gap:1.2rem; }
    .masthead nav ul { display:grid; grid-template-columns:repeat(4,auto); gap:.4rem;
                       justify-content:space-between; width:100%; font-size:.7rem; }
    .masthead nav .subscribe-link { padding:.4rem .5rem; }
    .feature-header { grid-template-columns:1fr; row-gap:1.5rem; }
    .feature-header > *:not(.byline),.byline { grid-row:auto; grid-column:1; }
    h1 { font-size:clamp(2.5rem,11vw,3.75rem); }
    .feature-nav { margin-left:0; }
    .section-head { grid-template-columns:1fr; gap:.4rem; }
    .site-footer { grid-template-columns:1fr; }
  }
"""

MAIN_CSS = """
  .language-page .feature-header { grid-template-columns:1fr; gap:.85rem;
                                  padding:clamp(2rem,4vw,3rem) 0 1.75rem; border:0; }
  .language-page .feature-header > .feature-intro { grid-column:1; }
  .language-page .feature-header > .feature-summary { grid-column:1; }
  .language-page h1 { max-width:28ch; font-size:clamp(2.6rem,4.5vw,3.8rem); line-height:1.06;
                       text-wrap:balance; }
  .language-page .dek { margin:0; max-width:42rem; font-size:1.15rem;
                        font-style:normal; line-height:1.45; }
  .language-page .byline { display:flex; flex-wrap:wrap; gap:.3rem 1.25rem; margin-top:.75rem;
                          padding:0; border:0; font-size:.62rem; color:var(--muted); }
  .language-page .byline p { margin:0; }
  .page-navigation { display:flex; flex-wrap:wrap; justify-content:space-between;
                     gap:.75rem 2rem; padding:.85rem 0; border-bottom:1px solid var(--rule); }
  .language-page .feature-nav { margin:0; padding:0; border:0; }
  .language-page .feature-nav > span { display:none; }
  .section-nav { display:flex; flex-wrap:wrap; gap:.75rem 1.5rem; margin:0; padding:0; }
  .section-nav a { text-decoration:none; color:var(--muted); font-family:var(--sans);
                   font-size:.78rem; }
  .section-nav a:hover { text-decoration:underline; }
  .language-page .feature-section { padding-top:clamp(2rem,4vw,3rem);
                                    scroll-margin-top:1.5rem; }
  .language-page .feature-section ~ .feature-section { margin-top:3rem;
                                                     border-top:1px solid var(--hair); }
  .language-page .section-head { display:block; margin-bottom:1.5rem; padding:0; border:0; }
  .language-page .section-head > div { display:grid; grid-template-columns:1fr;
                                     gap:.5rem; }
  .language-page h2 { font-size:clamp(1.7rem,2.5vw,2.15rem); line-height:1.12; text-wrap:balance; }
  .language-page .section-dek { margin:0; max-width:44rem; color:var(--muted);
                              font-size:.95rem; line-height:1.5; }
  .language-page .section-head .eyebrow { grid-column:1 / -1; margin:0;
                                       font-size:.6rem; }
  .language-page .term-section-header .definition { grid-column:1 / -1; margin:.5rem 0 0; }
  .explorer-controls { display:grid; grid-template-columns:repeat(2,minmax(12rem,16rem));
                       gap:1.25rem 2rem; margin:0 0 1.5rem; padding:1rem 0;
                       border:0; }
  .long-run-controls { grid-template-columns:repeat(2,minmax(0,14rem)); }
  .language-page select { border-bottom-color:var(--rule); font-size:1rem; }
  .control-note { align-self:center; max-width:30rem; margin:0; color:var(--muted);
                  font-size:.88rem; line-height:1.45; }
  .recent-controls { grid-template-columns:2fr 1fr 1fr 1.2fr; }
  .term-toolbar { display:flex; align-items:flex-end; justify-content:space-between;
                  flex-wrap:wrap; gap:1.25rem 2.5rem; margin:0 0 1.25rem;
                  padding:1rem 0; border:0; }
  .term-controls { grid-template-columns:repeat(2,minmax(0,12rem)); margin:0;
                   padding:0; border:0; }
  .term-toolbar .tab-row { border:0; }
  .focus-panel { min-width:0; }
  .source-note { max-width:52rem; margin:1rem 0 0; font-size:.88rem; font-style:italic; }
  .recent-shell { display:grid; gap:1.5rem; }
  .context-panel { display:grid; grid-template-columns:1fr .9fr 1.3fr; gap:2rem;
                   padding:1rem 0 1.5rem; align-items:start; }
  .context-panel .eyebrow { margin:0 0 .5rem; font-size:.6rem; }
  .context-panel h3 { margin:0; font-size:1.4rem; letter-spacing:-.03em; }
  .party-rates { display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
  .party-rate { font-family:var(--mono); font-size:.6rem; letter-spacing:.06em;
                text-transform:uppercase; }
  .party-rate b { display:block; margin-bottom:.3rem; font-family:var(--serif);
                  font-size:2.5rem; font-weight:500; letter-spacing:-.045em; line-height:1; }
  .party-rate.democratic { color:var(--dem); }
  .party-rate.republican { color:var(--rep); }
  .context-panel .comparison { margin:.5rem 0 0; font-size:.92rem; font-style:italic;
                               line-height:1.35; }
  .top-members { margin:0; font-size:.95rem; line-height:1.45; }
  .top-members strong { display:block; color:var(--muted); font-family:var(--mono);
                        font-size:.6rem; font-weight:400; letter-spacing:.08em;
                        text-transform:uppercase; }
  .top-members ol { margin:.5rem 0 0; padding-left:1.2rem; }
  .top-members li { margin:.35rem 0; }
  .methodology-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr));
                      gap:1.5rem 2.5rem; padding:0 0 1.5rem; }
  .methodology-grid h3 { font-size:1.15rem; }
  .methodology-grid p { margin:.2rem 0; color:var(--muted); font-size:.95rem; }
  .mini-chart { position:relative; min-width:0; display:grid;
                grid-template-columns:minmax(0,1fr) auto; gap:.5rem 1rem; }
  .mini-chart-heading h3 { font-size:1.15rem; letter-spacing:-.02em; }
  .mini-chart-heading p { max-width:46rem; margin:.15rem 0 .5rem; font-size:.85rem; }
  .chart-definition { max-width:46rem; margin:.25rem 0 .75rem; color:var(--muted); }
  .chart-definition summary { cursor:pointer; font-size:.82rem; }
  .mini-chart > svg { grid-column:1 / -1; display:block; width:100%; height:auto;
                     overflow:visible; }
  .mini-chart svg text { font-family:var(--mono); }
  .mini-chart svg text.svg-label { font-family:var(--serif); }
  .chart-legend { display:flex; flex-wrap:wrap; align-items:center; gap:.5rem 1.5rem;
                  margin:0; align-self:start; }
  .chart-toggle { display:inline-flex; align-items:center; gap:.5rem; padding:.2rem 0; border:0;
                  background:transparent; color:var(--ink); font-family:var(--sans);
                  font-size:.74rem; letter-spacing:.1em; text-transform:uppercase;
                  cursor:pointer; }
  .chart-toggle[aria-pressed="false"] { opacity:.4; text-decoration:line-through; }
  .chart-legend i { display:inline-block; width:1.5rem; height:3px; }
  .chart-tooltip { position:absolute; z-index:2; max-width:18rem; pointer-events:none;
                   background:var(--ink); color:var(--paper); padding:.5rem .65rem;
                   font:.7rem/1.5 var(--mono); letter-spacing:.02em; }
  .data-mark { cursor:pointer; transition:opacity .12s ease; }
  .data-mark:hover,.data-mark:focus { opacity:.6; outline:none; }
  #language-tables .card { margin:0; }
  #language-tables h3 { font-size:1.6rem; letter-spacing:-.03em; }
  #language-tables .definition { max-width:46rem; margin:.2rem 0 1.25rem; }
  #language-tables table { font-size:.9rem; }
  .term-explorer-grid { display:grid; grid-template-columns:1fr; gap:1.25rem; }
  .term-explorer-grid[data-view="frequency"] { grid-template-columns:1fr; }
  .term-explorer-grid .card { margin:0; }
  .state-map-card { max-width:48rem; padding:1rem 0; }
  .language-page .table-wrap { border-top:1px solid var(--rule); }
  .state-map-card figcaption { margin:0 0 1rem; color:var(--muted); font-size:.92rem;
                               line-height:1.45; }
  .state-map-card figcaption strong { display:block; margin-bottom:.2rem; color:var(--ink);
                                      font-size:1.35rem; font-weight:500; letter-spacing:-.02em; }
  #state-term-map svg { display:block; width:100%; height:auto; }
  #state-term-map svg text { font-family:var(--mono); }
  .map-legend { margin:.75rem 0 0; font-size:.85rem; }
  #term-leaders-table { table-layout:fixed; }
  #term-leaders-table[data-view="leaders"] th:first-child,
  #term-leaders-table[data-view="leaders"] td:first-child { width:18%; }
  #term-leaders-table[data-view="leaders"] th:last-child,
  #term-leaders-table[data-view="leaders"] td:last-child { width:12rem; }
  #term-leaders-table[data-view="frequency"] .rank {
    width:2.5rem; color:var(--muted); font-family:var(--mono); font-size:.72rem;
  }
  #term-leaders-table[data-view="frequency"] th:nth-child(3),
  #term-leaders-table[data-view="frequency"] td:nth-child(3) { width:5.25rem; }
  #term-leaders-table[data-view="frequency"] th:last-child,
  #term-leaders-table[data-view="frequency"] td:last-child { width:5rem; }
  #term-leaders-table[data-view="frequency"] .share { color:var(--muted); }
  #term-leaders-table[data-view="frequency"] .censored-term {
    white-space:normal; overflow-wrap:anywhere;
  }
  #term-leaders-table td:nth-child(2) a { text-decoration-color:var(--rule); }
  #term-leaders-table td:nth-child(2) a:hover { text-decoration-color:var(--signal); }
  .party-badges { display:inline-flex; gap:.2rem; margin-left:.35rem; }
  .party-badge { font-family:var(--mono); font-size:.62rem; font-weight:700;
                 letter-spacing:.04em; }
  .party-d { color:var(--dem); }
  .party-r { color:var(--rep); }
  .party-i { color:var(--ind); }
  .party-other { color:var(--muted); }
  .term-usage { display:flex; align-items:baseline; justify-content:flex-end; gap:.3rem;
                text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .term-usage span { color:var(--muted); font-size:.82rem; }
  .term-row-toggle { display:block; margin:1rem 0 .2rem; padding:.25rem 0; border:0;
                     background:transparent; color:var(--signal-dark); cursor:pointer;
                     font-family:var(--sans); font-size:.74rem; letter-spacing:.1em;
                     text-transform:uppercase; text-decoration:underline;
                     text-decoration-color:var(--signal); text-underline-offset:.25em; }
  .term-row-toggle:hover { color:var(--ink); }
  .term-row-toggle[hidden] { display:none; }
  .language-page .notes { margin:1rem 0 0; border:0; }
  .language-page .notes summary { color:var(--muted); font-size:.62rem; }
  .language-page .notes[open] > summary { color:var(--ink); }
  .language-page .coverage-note { max-width:60rem; margin:1.5rem 0 0; line-height:1.7; }
  .reading-note { max-width:52rem; margin:.75rem 0 0; color:var(--muted); font-size:.88rem; }
  noscript img { width:100%; margin-top:1.5rem; }
  @media (prefers-reduced-motion:no-preference) {
    html { scroll-behavior:smooth; }
  }
  @media (min-width:761px) and (max-width:1000px) {
    .term-explorer-grid,.term-explorer-grid[data-view="frequency"] { grid-template-columns:1fr; }
  }
  @media (max-width:760px) {
    .language-page .feature-header { grid-template-columns:1fr; gap:1.25rem; padding:2rem 0; }
    .language-page .feature-header > .feature-intro,
    .language-page .feature-header > .feature-summary { grid-column:1; }
    .language-page h1 { max-width:16ch; font-size:clamp(2.5rem,9vw,3.5rem); }
    .language-page .section-head > div { grid-template-columns:1fr; gap:.75rem; }
    .page-navigation { gap:.85rem; }
    .section-nav { gap:.75rem 1.25rem; }
    .explorer-controls,.recent-controls,.term-controls { grid-template-columns:1fr 1fr; }
    .long-run-controls .control-note { grid-column:1 / -1; }
    .methodology-grid,.recent-shell,.term-explorer-grid,
    .term-explorer-grid[data-view="frequency"] { grid-template-columns:1fr; }
    .context-panel { grid-template-columns:1fr 1fr; gap:1.25rem; }
    .context-panel .top-members { grid-column:1 / -1; }
    .mini-chart { grid-template-columns:1fr; gap:0; }
    .chart-legend { margin:.25rem 0 .75rem; }
    .term-toolbar { display:grid; gap:1rem; }
    #term-leaders-table[data-view="leaders"] th:first-child,
    #term-leaders-table[data-view="leaders"] td:first-child { width:22%; }
    #term-leaders-table[data-view="leaders"] th:last-child,
    #term-leaders-table[data-view="leaders"] td:last-child { width:36%; }
    #term-leaders-table[data-view="frequency"] .rank { width:2rem; }
    #term-leaders-table[data-view="frequency"] th:nth-child(3),
    #term-leaders-table[data-view="frequency"] td:nth-child(3),
    #term-leaders-table[data-view="frequency"] th:last-child,
    #term-leaders-table[data-view="frequency"] td:last-child { width:4rem; }
    #term-leaders-table th,#term-leaders-table td { padding:.46rem .35rem; }
    .term-usage { flex-wrap:wrap; gap:.12rem .24rem; }
    #language-tables table { font-size:.78rem; table-layout:fixed; }
    #language-tables th,#language-tables td { padding:.32rem .25rem; overflow-wrap:anywhere; }
    #language-tables th:nth-child(4),#language-tables td:nth-child(4),
    #language-tables th:nth-child(7),#language-tables td:nth-child(7) { display:none; }
  }
  @media (max-width:380px) {
    .explorer-controls { gap:1rem; }
    .context-panel { grid-template-columns:1fr; }
    .context-panel .top-members { grid-column:1; }
    .party-rate b { font-size:2rem; }
  }
"""

ACTIVITY_CSS = """
  .toolbar { display:flex; flex-wrap:wrap; gap:1.5rem 3rem; align-items:flex-end;
             justify-content:space-between; margin:0 0 2.25rem; }
  .toolbar label { width:14rem; max-width:100%; }
  #leaderboards .card h2 { font-size:clamp(1.8rem,3vw,2.6rem); }
  #leaderboards .definition { max-width:46rem; margin:.5rem 0 1.4rem; }
  table th:first-child,table td:first-child { width:2.75rem; color:var(--muted);
                                              font-family:var(--mono); font-size:.72rem; }
  @media (max-width:760px) {
    .toolbar { display:grid; gap:1.25rem; }
    .toolbar label { max-width:16rem; }
    table { table-layout:fixed; font-size:.8rem; }
    table th:first-child,table td:first-child { width:2rem; }
    table[data-metric="speech"] th:nth-child(6),
    table[data-metric="speech"] td:nth-child(6) { width:7rem; }
    th,td { padding:.38rem .28rem; overflow-wrap:normal; }
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
    table[data-metric="profanity"] th:nth-child(9),table[data-metric="profanity"] td:nth-child(9) {
      display:none;
    }
  }
"""


def _site_header() -> str:
    """The Margin of Error masthead, matching src/components/Header.astro."""
    nav = "".join(
        f'<li><a href="{href}"{attrs}>{label}</a></li>'
        for href, label, attrs in (
            ("/", "Latest", ""),
            ("/archive/", "Archive", ""),
            ("/about/", "About", ""),
            (SUBSCRIBE_URL, "Subscribe", ' class="subscribe-link"'),
        )
    )
    return (
        '<header class="site-header">\n'
        '<div class="edition-line" aria-hidden="true">'
        "<span>Independent notes on systems and society</span><span>Est. 2021</span></div>\n"
        '<div class="masthead">\n'
        '<a class="wordmark" href="/" aria-label="The Margin of Error, home">'
        '<span class="wordmark-the">The</span>'
        '<span class="wordmark-main">Margin <i>of</i> Error</span></a>\n'
        f'<nav aria-label="Primary navigation"><ul>{nav}</ul></nav>\n'
        "</div>\n</header>"
    )


def _site_footer(generated_utc: str) -> str:
    """The Margin of Error footer, matching src/components/Footer.astro."""
    year = html.escape(str(generated_utc)[:4])
    return (
        '<footer class="site-footer">\n'
        '<div><p class="kicker">Endnote</p><p>The Margin of Error is written by '
        '<a href="https://www.hanshanley.com/">Hans W. A. Hanley</a>.</p></div>\n'
        '<div class="footer-links"><a href="/rss.xml">RSS</a>'
        '<a href="/are-you-an-ai/">AI index</a>'
        f'<a href="{SOURCE_REPOSITORY_URL}">Data &amp; code</a>'
        f'<a href="{SUBSCRIBE_URL}">Subscribe</a></div>\n'
        f'<p class="copyright">© {year} Hans W. A. Hanley</p>\n'
        "</footer>"
    )


def _head_links(canonical: str) -> str:
    return (
        '<meta name="theme-color" content="#f2efe7">\n'
        '<link rel="icon" href="/favicon-32.png" type="image/png" sizes="32x32">\n'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png" sizes="180x180">\n'
        f'<link rel="canonical" href="{canonical}">'
    )


def _long_date(value: str) -> str:
    """Format an ISO date such as ``2026-09-22`` as ``September 22, 2026``."""
    try:
        parsed = dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{parsed:%B} {parsed.day}, {parsed.year}"


def _feature_nav(current: str) -> str:
    """Links between the two dashboard pages, relative to the current page."""
    pages = (
        ("language", "The Language of Congress"),
        ("activity", "Member Activity &amp; Bills"),
    )
    hrefs = {
        "language": {"language": "./", "activity": "activity/"},
        "activity": {"language": "../", "activity": "./"},
    }[current]
    links = []
    for key, label in pages:
        current_attr = ' aria-current="page"' if key == current else ""
        links.append(f'<a href="{hrefs[key]}"{current_attr}>{label}</a>')
    return (
        '<nav class="feature-nav" aria-label="Dashboard pages"><span>Pages</span>'
        + "".join(links) + "</nav>"
    )


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
            ha="center", va="center", color=theme.active_color("MUTED"),
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
    charts.fit_time_axis(ax)
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
    scope_frame = daily if congress is None else daily[daily["congress"] == congress]
    scope_frame = scope_frame[scope_frame["chamber"].isin(["house", "senate"])]
    # A measure is published only once every row in the scope has been scored for it,
    # so a partially backfilled measure is hidden rather than shown with false zeros.
    metrics = {
        key: metric
        for key, metric in LANGUAGE_METRICS.items()
        if language_metric_available(scope_frame, key)
    }
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
                for metric in metrics.values()
            },
        }
        for metric in metrics.values():
            hits = party_summary[party][metric["hits"]]
            party_summary[party][metric["rate"]] = (
                100_000 * hits / words if words else 0.0
            )

    def enrich_rankings(frames: dict[str, pd.DataFrame]) -> dict[str, list[dict]]:
        enriched = {}
        for key, frame in frames.items():
            if key not in metrics:
                continue
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
    for key, metric in metrics.items():
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
    measures = " and ".join(metric["label"].lower() for metric in metrics.values())
    return {
        "scope_label": scope_label,
        "granularity": granularity,
        "metrics": metrics,
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
            f"{period_label.title()} Democratic and Republican rates for {measures} "
            f"in {scope_label}."
        ),
        "member_alt": f"Highest eligible member rates for {measures} in {scope_label}.",
        "explanation": {
            "shown": (
                f"The charts show {period_label} Democratic and Republican rates for "
                f"{measures}, plus the highest-rate members in {scope_label}. Rates are "
                "hits per 100,000 attributed spoken words."
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
                "Quoted profanity and quoted slurs are excluded from a speaker's rate, but "
                "a member who repeats a slur to condemn or describe it is still counted."
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
        payload = restrict_to_complete_metrics(
            build_long_run_payload(pd.read_parquet(LONG_RUN_METRICS_PATH))
        )
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
    return restrict_to_complete_metrics(
        json.loads(LONG_RUN_DATA_PATH.read_text(encoding="utf-8"))
    )


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


def _censored_term_cell(term: str, detail: str = "") -> _TrustedHTML:
    """Render a censored term whose full value is available on hover and focus."""
    value = str(term)
    first, separator, remainder = value.partition(" ")
    letter_count = sum(character.isalpha() for character in first)
    letter_index = 0
    censored_chars = []
    for character in first:
        if character.isalpha():
            keep = (
                letter_index < (2 if letter_count >= 4 else 1)
                or letter_index == letter_count - 1
            )
            letter_index += 1
            if not keep:
                censored_chars.append("*")
                continue
        censored_chars.append(character)
    censored = "".join(censored_chars) + (separator + remainder if separator else "")
    tooltip = f"Uncensored term: {value}"
    if detail:
        tooltip += f". {detail}"
    return _TrustedHTML(
        '<span class="censored-term" tabindex="0" '
        f'data-term="{html.escape(value, quote=True)}" '
        f'aria-label="{html.escape(value, quote=True)}" '
        f'title="{html.escape(tooltip, quote=True)}">'
        f"{html.escape(censored)}</span>"
    )


def _term_usage_cell(row: dict) -> _TrustedHTML:
    leader_hits = int(row["leader_hits"])
    total_hits = int(row["total_hits"])
    tied = len(row["leaders"]) > 1
    each = " each" if tied else ""
    unit = "use" if leader_hits == 1 else "uses"
    subject = "by each leading member" if tied else "by the leading member"
    aria = f"{leader_hits:,} {unit} {subject} out of {total_hits:,} uses by all members"
    return _TrustedHTML(
        f'<div class="term-usage" aria-label="{html.escape(aria, quote=True)}">'
        f"<strong>{leader_hits:,}{each}</strong>"
        f"<span>/ {total_hits:,} total</span>"
        "</div>"
    )


def _party_badges(parties: set[str]) -> _TrustedHTML:
    badges = []
    for party in sorted(parties):
        css_party = re.sub(r"[^a-z0-9_-]", "", party.lower()) or "other"
        badges.append(
            f'<span class="party-badge party-{css_party}">'
            f"{html.escape(party)}</span>"
        )
    return _TrustedHTML(
        f'<span class="party-badges">{"".join(badges)}</span>'
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
                (
                    _censored_term_cell(r["favorite_profanity_term"])
                    if r.get("favorite_profanity_term") else "—"
                ),
                f"{float(r['profanity_per_100k']):.1f}", _fmt_int(r["profanity_hits"]),
                _fmt_int(r["profanity_quoted_hits"]), _fmt_int(r["words"]),
            ],
        ),
    }
    headers, values = configs[metric]
    numeric = ACTIVITY_NUMERIC_COLUMNS[metric]
    head = "".join(
        f'<th scope="col" class="{"num" if index in numeric else ""}">{html.escape(label)}</th>'
        for index, label in enumerate(headers)
    )
    body = []
    for row in rows:
        cells = values(row)
        body.append(
            "<tr>"
            + "".join(
                f"<td class=\"{'num' if index in numeric else ''}\">"
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
                [
                    _censored_term_cell(row["favorite_profanity_term"])
                    if row.get("favorite_profanity_term") else "—"
                ]
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
            '<tr><td colspan="3" class="muted">'
            "Term-level detail is not available for this historical scope.</td></tr>"
        )
    elif not rows:
        body.append(
            '<tr><td colspan="3" class="muted">'
            "No accepted term uses were observed in this scope.</td></tr>"
        )
    for row in rows:
        leaders = _TrustedHTML(
            ", ".join(
                str(_member_cell(leader)) + str(_party_badges({leader["party"]}))
                for leader in row["leaders"]
            )
        )
        variants = row.get("variants", [])
        detail = (
            "Grouped forms: " + ", ".join(variants)
            if len(variants) > 1 else ""
        )
        body.append(
            "<tr>"
            f"<td>{_censored_term_cell(row['term'], detail)}</td>"
            f"<td>{leaders}</td>"
            f'<td class="num">{_term_usage_cell(row)}</td>'
            "</tr>"
        )
    return (
        '<table id="term-leaders-table" data-view="leaders"><caption class="sr-only">'
        "Top congressional users of each observed profanity term</caption>"
        '<thead><tr><th scope="col">Term</th>'
        '<th scope="col">Member(s) with most uses</th>'
        '<th scope="col" class="num">Leader / all uses</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table>'
        '<button id="term-row-toggle" class="term-row-toggle" type="button" '
        'aria-controls="term-leaders-table" aria-expanded="false" hidden></button>'
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
        for item in (CAVEATS[0], CAVEATS[1], SLUR_CAVEAT, CAVEATS[2], CAVEATS[-1])
    )
    language_cards = "".join(
        (
            f'<section class="card" id="{metric}-table">'
            f'<h3>Highest {html.escape(metadata["label"].lower())} rates</h3>'
            f'<p class="definition">{html.escape(metadata["definition"])} '
            + (
                "“Most-used term” is the most frequent unquoted match, not a claim of preference. "
                if metric == "profanity" else ""
            )
            + "Only members with a nonzero rate and enough words are included.</p>"
            + f'<div class="table-wrap">{_language_table(metric, language["members"].get(metric, []))}</div>'
            + "</section>"
        )
        for metric, metadata in LANGUAGE_METRICS.items()
    )
    coverage = payload["coverage"]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Language of Congress — The Margin of Error</title>
<meta name="description" content="Long-run Democratic and Republican trends in congressional
courtesy, profanity, and slurs.">
{_head_links(PUBLIC_URL)}
<style>{HOUSE_CSS}{MAIN_CSS}</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to content</a>
{_site_header()}
<main id="main-content">
<article class="feature language-page">
<header class="feature-header">
<div class="feature-intro">
<p class="kicker">Congress / Data</p>
<h1>The Language of Congress</h1>
</div>
<div class="feature-summary">
<p class="dek">How Congress speaks, from historical trends to individual members.</p>
<div class="byline">
<p>By Hans W. A. Hanley</p>
<p>Congressional Record, 1873–present</p>
<p>Updated through {html.escape(_long_date(coverage['speech_last_date']))}</p>
</div>
</div>
</header>
<div class="page-navigation">
{_feature_nav("language")}
<nav class="section-nav" aria-label="Explore the language of Congress">
<a href="#history">Historical trends</a>
<a href="#members">Members</a>
<a href="#terms">Vocabulary</a>
</nav>
</div>
<section id="history" class="feature-section overview" aria-labelledby="overview-heading">
<header class="section-head">
<div><h2 id="overview-heading">The long-run picture</h2>
<p class="section-dek">Democratic and Republican floor language since 1873.</p></div>
</header>
<div class="explorer-controls long-run-controls">
<label>Measure<select id="long-run-metric"></select></label>
<label>Chamber<select id="long-run-chamber"></select></label>
</div>
<div id="long-run-chart" class="focus-panel"
 aria-label="Interactive long-run Democratic and Republican language chart"></div>
<details class="notes chart-notes">
<summary>Sources and how to read the chart</summary>
<p class="control-note">Rates per 1,000 words. Each measure counts a different kind of
language, not an overall civility score. Select a party in the legend to hide or show
its series; hover over or focus a point for the underlying counts.</p>
<p class="definition source-note">{html.escape(long_run['source_note'])}</p>
</details>
</section>
<div id="coverage-warning" class="warning" {'hidden' if not warning else ''}>{html.escape(warning)}</div>
<p id="dashboard-error" class="error" role="alert" hidden></p>
<section id="members" class="feature-section language" aria-labelledby="language-heading">
<header class="section-head">
<div><h2 id="language-heading">Recent language on the floor</h2>
<p class="section-dek">Member-level results cover 1994–present.</p></div>
</header>
<div class="explorer-controls recent-controls">
<label>Congress<select id="congress">{''.join(options)}</select></label>
<label>Measure<select id="recent-metric"></select></label>
<label>Chamber<select id="recent-chamber"></select></label>
<label>View<select id="recent-view"></select></label>
</div>
<div class="recent-shell">
<div>
<div id="recent-visual" class="focus-panel"></div>
<div id="language-tables" class="focus-panel" hidden>{language_cards}</div>
</div>
</div>
<details class="notes congress-summary">
<summary>Party comparison and leading members</summary>
<aside id="language-highlight" class="context-panel"
 aria-label="Selected Congress language summary"></aside>
</details>
<details class="notes methodology">
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
<section id="terms" class="feature-section language" aria-labelledby="term-leaders-heading">
<header class="section-head term-section-header">
<div><p class="eyebrow" id="term-leaders-scope">{html.escape(language['scope_label'])} · House + Senate</p>
<h2 id="term-leaders-heading">Who uses each term the most?</h2>
<p class="section-dek" id="term-leaders-description">Profanity by term and member.
Related forms are grouped; only accepted, unquoted uses count.</p>
<p class="definition" id="term-leaders-note" {
    "hidden" if language["profanity_term_detail_available"] else ""
}>{
    "" if language["profanity_term_detail_available"]
    else "Term-level detail has not been backfilled for this historical scope."
}</p></div>
</header>
<div class="term-toolbar">
<div id="term-view" class="tab-row" role="group" aria-label="Term view">
<button type="button" class="tab-button" data-view="leaders"
 aria-pressed="true" aria-controls="term-leaders-table">Member leaders</button>
<button type="button" class="tab-button" data-view="frequency"
 aria-pressed="false" aria-controls="term-leaders-table">Term frequency</button>
</div>
<div class="explorer-controls term-controls">
<label>Party<select id="term-party"></select></label>
<label>Chamber<select id="term-chamber"></select></label>
</div>
</div>
<div class="term-explorer-grid">
<div class="card table-wrap">{_term_leaders_table(
    language['profanity_term_leaders'],
    available=language['profanity_term_detail_available'],
)}</div>
<details class="notes state-map-details">
<summary>Explore the state map</summary>
<figure class="card state-map-card">
<figcaption><strong>Most-used term by state</strong>
The leading grouped term among attributed members from each state in the selected scope.</figcaption>
<div id="state-term-map"></div>
</figure>
</details>
</div>
<details class="notes term-notes">
<summary>How to read the term table</summary>
<p class="reading-note">“Total” includes all attributed members; “each” applies to every member
in a tie. Terms are censored—hover over or focus one to reveal it. Raw matches remain in
the downloadable data. The Congress selector above applies to this section too.</p>
</details>
</section>
<details class="notes data-notes"><summary>Data notes, coverage, and exclusions</summary><ul>{caveats}</ul>
<p id="coverage" class="coverage-note">Speech coverage {html.escape(coverage['speech_first_date'])}
to {html.escape(coverage['speech_last_date'])}. Newest Congressional Record date:
{html.escape(coverage['speech_last_date'])}. Site data snapshot:
{html.escape(payload['generated_utc'])}. <a href="activity/">Open member activity and bill tables.</a></p>
</details>
</article>
</main>
{_site_footer(payload['generated_utc'])}
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
const sectionLinks = document.querySelectorAll('.section-nav a');
sectionLinks.forEach(link => {{
  link.addEventListener('click', event => {{
    const section = document.getElementById(link.getAttribute('href').slice(1));
    if (!section) return;
    event.preventDefault();
    updateHash({{section: section.id}});
    section.scrollIntoView();
  }});
}});
const requestedSection = requestedState.get('section');
if (requestedSection) {{
  const section = document.getElementById(requestedSection);
  if (section && [...sectionLinks].some(link => link.getAttribute('href') === `#${{section.id}}`)) {{
    section.scrollIntoView();
  }}
}}
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
    metric_buttons = "".join(
        f'<button type="button" class="tab-button" data-metric="{metric}" '
        f'aria-pressed="{"true" if metric == "speech" else "false"}" '
        f'aria-controls="{metric}">{html.escape(label)}</button>'
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
    coverage = payload["coverage"]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Congressional member activity and bills — The Margin of Error</title>
<meta name="description" content="Congressional speech, bill sponsorship,
passage, enactment, and profanity tables by Congress.">
{_head_links(PUBLIC_URL + "activity/")}
<style>{HOUSE_CSS}{ACTIVITY_CSS}</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to content</a>
{_site_header()}
<main id="main-content">
<article class="feature">
<header class="feature-header">
<p class="kicker">Congress / Data</p>
<h1>Member activity &amp; bills</h1>
<p class="dek">Explore member speech, bill sponsorship, passage, enactment,
and profanity by Congress.</p>
<div class="byline">
<p>By Hans W. A. Hanley</p>
<p>Congressional Record, 1994–present</p>
<p>Updated through {html.escape(_long_date(coverage['speech_last_date']))}</p>
</div>
</header>
{_feature_nav("activity")}
<section class="feature-section" aria-label="Activity tables">
<div class="toolbar">
<div id="activity-metric" class="tab-row" role="group" aria-label="Activity measure">
{metric_buttons}</div>
<label for="congress">Congress<select id="congress">{''.join(options)}</select></label>
</div>
<div id="coverage-warning" class="warning" {'hidden' if not warning else ''}>{html.escape(warning)}</div>
<p id="dashboard-error" class="error" role="alert" hidden></p>
<div id="leaderboards">{cards}</div>
</section>
<details class="notes"><summary>Data notes and exclusions</summary><ul>{caveats}</ul></details>
<p id="coverage" class="coverage-note">Speech coverage {html.escape(coverage['speech_first_date'])}
to {html.escape(coverage['speech_last_date'])}; {_fmt_int(coverage['bills'])}
H.R./S. bill records. Site data snapshot: {html.escape(payload['generated_utc'])}.</p>
</article>
</main>
{_site_footer(payload['generated_utc'])}
<script>
const activityNumericColumns = {_script_json(ACTIVITY_NUMERIC_COLUMNS)};
{ACTIVITY_JS}</script>
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
    profanity = pd.DataFrame(selected["leaderboards"]["profanity"])
    series = timeseries(daily)
    with theme.house_style():
        site_charts.language_trends(
            pd.DataFrame(selected_language["series"]),
            figs / "language_trends.png",
            scope_label=selected_language["scope_label"],
            granularity=selected_language["granularity"],
            metrics=selected_language["metrics"],
        )
        site_charts.language_members(
            {
                key: pd.DataFrame(rows)
                for key, rows in selected_language["members"].items()
            },
            figs / "language_members.png",
            scope_label=selected_language["scope_label"],
            min_words=args.min_words,
            metrics=selected_language["metrics"],
        )
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
