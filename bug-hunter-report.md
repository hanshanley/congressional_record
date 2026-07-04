# Bug Hunter Report — `congressional_record` pipeline

**Scope:** `fetch_crec.py`, `crec/api.py`, `crec/download.py`, `crec/enumerate.py`, `crec/metadata.py`, `tests/test_parsing.py`, `README.md`
**Summary:** 0 HIGH · 13 MEDIUM · 12 LOW · (tests pass)
**Dimensions run:** Correctness · Logic · Comments/Docs · Readability · Security · Performance

Overlapping findings across reviewers were de-duplicated (kept highest severity, merged notes).

---

## MEDIUM

### Correctness
- **`crec/api.py:79,116` — Non-retryable 4xx are retried 5×.** The retry predicate matches
  `requests.HTTPError` from `raise_for_status()`, so 400/401/403 (e.g. an **invalid API key**) are
  retried with backoff instead of failing fast — an invalid key never surfaces a clear error.
  → Map non-retryable 4xx to `GovInfoError` before `raise_for_status`, or exclude `HTTPError` from the retry predicate.

### Logic consistency
- **`fetch_crec.py:146,187` — `--overwrite` appends duplicate manifest rows.** Overwrite bypasses the
  `gid in processed` guard but the write is an unconditional append, so re-downloading a granule adds a
  second JSONL row with the same `granuleId` (accumulates every overwrite run).
  → On overwrite, replace the existing row (or skip append when `gid` already present). *(also flagged by Correctness)*
- **`fetch_crec.py:183 vs 213-217` — Counters don't map to manifest writes.** The backfill path (`row is None`)
  writes a row but is tallied as `skip_count`, which also counts granules that write nothing; no counter maps
  to rows actually written.
  → Add a distinct `backfill_count` so counted totals match rows written.

### Comments / docs / schema drift
- **`fetch_crec.py:170-183` — Backfill manifest row omits fields & duplicates the schema.** The recovery
  row hand-builds a reduced record (missing `congress`, `session`, `chamber`, `citation`, `member_names`,
  `bioguide_ids`, `char_count`) that diverges from `download_granule`'s row (download.py:81-96) and from the
  README's stated schema — inconsistent manifest + `KeyError` risk downstream.
  → Re-parse the on-disk `.mods.xml` to populate the same fields via a shared `build_manifest_row` helper.
  *(flagged by Readability, Docs, Logic, Correctness)*

### Readability
- **`fetch_crec.py:136-193` — `main()` is a ~90-line, triple-nested loop** mixing enumeration, download,
  rate-limit accounting, and manifest writing.
  → Extract per-granule handling into a helper returning an outcome; leave `main()` as orchestration.

### Security
- **`crec/download.py:34-42,59` — Path traversal from unvalidated ids.** `package_id`/`granule_id` (remote
  API strings) are interpolated into filesystem paths with no validation; a `../` or leading `/` (malicious/
  MITM'd/buggy response) escapes `out_dir` and can overwrite files.
  → Validate ids against `^[A-Za-z0-9._-]+$`, or resolve the final path and assert it stays under `out_dir`.
- **`crec/api.py:99,111,113,115` — API key leaks into logs/exceptions.** `nextPage` URLs carry `api_key`
  (per the code's own comment) and flow into `LOG.warning`, exception messages, and `before_sleep_log`.
  → Redact the `api_key` query param before logging or embedding a URL in error text.
- **`crec/api.py:94-95,146-150,171-181` — SSRF + credential leak via `nextPage`.** `paginate` follows the
  server-supplied `nextPage` URL verbatim and `_request` attaches `api_key` to whatever host it points to.
  → Assert `nextPage`/redirect host matches `base_url` before requesting; only attach the key to the trusted host.

### Performance
- **`crec/metadata.py:78-98` — `parse_mods` walks the tree ~13×.** Each `_find_text`/citation call runs its
  own `elem.iter()`; multiplied over ~10⁵–10⁶ granules this is a lot of redundant CPU.
  → Single `root.iter()` pass, bucket nodes by local name, look up fields from the map.
- **`crec/metadata.py:33-38` — `_granule_part` nested iteration is ~O(n²).** For every `relatedItem` it runs a
  nested `iter()` over that subtree.
  → Collect `relatedItem` + `granuleClass` in one traversal.
- **`fetch_crec.py:89-103,126` — Full `processed` set held in memory.** On multi-decade/million-granule
  backfills this grows to hundreds of MB for the whole run.
  → Persistent/on-disk membership (sqlite) or partition resume state by month.
- **`fetch_crec.py:141-148` — Resume re-enumerates already-complete packages.** No package-level completion is
  recorded, so a resumed backfill repeats large numbers of granule-listing round-trips.
  → Record completed `packageId`s and skip `iter_granules` for them.

### Docs
- **`fetch_crec.py:173-182` vs `README.md:21-24` — README overstates manifest fields.** README says every row
  includes `congress`/`session`/`chamber`/`citation`/`member_names`/`bioguide_ids`/char count, but backfilled
  rows omit them.
  → Fix the backfill row (preferred) or document the reduced schema.

---

## LOW

- **`crec/api.py:177` — `paginate` stops on an empty intermediate page** (`if not next_page or not items`),
  even when `nextPage` is non-null → a transient empty page silently truncates enumeration (**potential data
  loss**). → Terminate on `nextPage is None` only.
- **`crec/api.py:122` — `b'"error"' in resp.content[:4096]` substring scan** can false-positive on a valid page
  whose field value/key equals `error`. → Parse JSON and check for a top-level `error` object.
- **`fetch_crec.py:155-160` — `fail_count` not incremented before the give-up `raise`** → the aborting granule
  is never counted. → Increment before re-raising.
- **`fetch_crec.py:149-168` — `consecutive_rate_limits` only resets on success** → non-rate-limit failures don't
  reset it, contradicting the "in a row" intent. → Reset on any non-retryable per-granule failure too.
- **`fetch_crec.py:191` — `--limit 0` is falsy** → disables the stop instead of stopping immediately.
  → Test `args.limit is not None`.
- **`fetch_crec.py:188` — `flush()` after every row** → a flush syscall per granule. → Flush every N rows/interval.
- **`crec/metadata.py:71` — `ET.fromstring` on remote XML is entity-expansion (billion-laughs) DoS-prone**
  (XXE not resolved by expat, so DoS-only). → Use `defusedxml.ElementTree`.
- **`fetch_crec.py:77-80` / `crec/enumerate.py:62-65` — duplicated "next month" rollover arithmetic.**
  → Extract a shared `next_month(date)` helper.
- **`crec/metadata.py:47-49` — verbose `{k: v for k, v in node.attrib.items()}`.** → `dict(node.attrib)`.
- **`README.md:30` — coverage lists 3 classes** but the default keeps 5 (`DAILYDIGEST`, `FRONTMATTER` too).
  → List all five or drop the enumeration.
- **`fetch_crec.py:200-201` — give-up message is DEMO_KEY-specific** but also fires when a real key hits the
  hourly cap. → Gate wording on `using_demo` or generalize.
- **`crec/download.py:27` — comment "collapse >2 blank lines"** but the regex triggers at 2+.
  → Reword to "collapse runs of 2+ blank lines to a single blank line."

---

## Files reviewed
`fetch_crec.py`, `crec/api.py`, `crec/download.py`, `crec/enumerate.py`, `crec/metadata.py`,
`tests/test_parsing.py`, `README.md`
