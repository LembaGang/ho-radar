# ho-radar

The observation layer of Headless Oracle's monitoring loop. A GitHub Actions
workflow runs once a day with no model credit, looks at a fixed list of public
sources, writes a dated and deterministic snapshot, and gives each monitor a
mechanical verdict. The results are committed to this repository under
`data/`. Judgments about what a movement means are made elsewhere, by readers
of the verdict file; this tool only observes and compares.

## What it watches

| id | kind | monitor | URL |
|---|---|---|---|
| x402-issue-2291 | issue | x402-issues | https://api.github.com/repos/x402-foundation/x402/issues/2291 |
| x402-issue-2332 | issue | x402-issues | https://api.github.com/repos/x402-foundation/x402/issues/2332 |
| x402-issue-2357 | issue | x402-issues | https://api.github.com/repos/x402-foundation/x402/issues/2357 |
| argentum-core-commits | commits | x402-issues | https://api.github.com/repos/giskard09/argentum-core/commits?per_page=30 |
| argentum-core-releases | releases | x402-issues | https://api.github.com/repos/giskard09/argentum-core/releases?per_page=5 |
| mcp-pr-2133 | pull request | mcp-registry | https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/pulls/2133 |
| mcp-commits | commits | mcp-registry | https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/commits?per_page=30 |
| mcp-pulls-updated | pull request list | mcp-registry | https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/pulls?state=all&sort=updated&direction=desc&per_page=30 |
| agt-releases | releases | kill-trigger | https://api.github.com/repos/microsoft/agent-governance-toolkit/releases?per_page=5 |
| agt-commits | commits | kill-trigger | https://api.github.com/repos/microsoft/agent-governance-toolkit/commits?per_page=30 |
| cf-blog | RSS | kill-trigger | https://blog.cloudflare.com/rss/ |
| cf-changelog | RSS | kill-trigger | https://developers.cloudflare.com/changelog/rss/index.xml |
| ho-record | JSON | record | https://api.headlessoracle.com/record |

The list lives in `radar/sources.py`. For issues with comments, a second
request fetches the newest comment's id and timestamp. For `ho-record`, a
second request fetches the newest record URL and keeps only its HTTP status.

Keyword filters (case-insensitive) decide which new commits (by the first
line of the message), releases, pull requests and feed items count:

- kill-trigger: `receipt|attest|recomput|signed|signature|verifiab|provenance|tamper|audit log|transparency log`
- mcp-registry: `extension|registry|SEP-?2133`
- x402-issues: no filter, any change counts

## What it stores, and what it never stores

This repository is public. What is stored, by kind of source:

- Commits: the sha, the commit date, the html_url and the keywords (from the
  monitor's filter, lower-cased) that matched the first line of the commit
  message. No part of the commit message is stored; it is read in memory and
  discarded.
- Issues, pull requests, releases and feed items: ids, numbers, states,
  counts, timestamps, labels, links, and the title as published by its
  source, with every at-sign handle (an @ followed by letters, digits or
  hyphens) replaced by `@[handle]`.
- The record: the fields listed in `radar/collect.py` (dates, sequence
  numbers, a record digest, counts, the HTTP status of the newest record URL).

Never stored: the login of any author, committer or commenter (the owner name of
each watched repository does appear, in its URLs), any user object, avatar URL, email address,
comment body, issue body, pull request body, commit author or committer block,
commit message text, or RSS author or creator element. The tests walk every
output file and every fixture and fail on those keys, on email-like strings,
on `from <owner>/` or `into <owner>/` text and on any unreplaced at-sign handle.

Limit: a title is written by a third party. The tool replaces at-sign handles
but cannot guarantee that a title written by someone else names no person.

Files written per run:

- `data/<date>/snapshot.json`: the normalised observation of every source,
  with `last_ok_date` and `last_ok_data` carried forward when a fetch fails.
- `data/<date>/summary.json`: the verdict per monitor and what moved.
- `data/<date>/SUMMARY.md`: the same, in plain text.
- `data/latest.json`: the newest date, the path of its summary, and the
  SHA-256 of that summary file.

Output is deterministic: the same fetched bytes with the same `--date` and
`--now` give byte-identical files.

## Verdicts and exit codes

For kill-trigger, x402-issues and mcp-registry:

- `BASELINE`: no earlier snapshot exists, or at least one source of the
  monitor has no earlier observation to compare with (no source is `ERROR`).
- `ERROR`: at least one of the monitor's sources could not be fetched or
  parsed, returned a list that is empty where the last good observation had
  items, had a feed item with neither guid nor link, or was not fetched before
  the 600 second total deadline. A failure is never reported as `QUIET`.
- `MOVED`: at least one source changed, or a new item matched the filter. For
  the pull request list, a match counts only when the pull request is new to
  the list or its state or merge time changed; a bare update time change is
  counted in `other_new_count`.
- `QUIET`: nothing changed against the last good observation.

For record (Headless Oracle's own daily record):

- `OK`: every check passed.
- `INCIDENT`: a check failed. The checks are `newest_is_expected`,
  `not_late`, `no_failed_steps`, `no_ots_failed`, `newest_is_yesterday` (the
  newest record is for the UTC day before the run date) and `newest_url_ok`.
  When an earlier good observation exists, three more apply and are otherwise
  absent: `sequence_not_regressed`, `record_not_rewritten` (only when the
  newest date is unchanged) and `advanced_since_previous` (only when the run
  date is later than the earlier observation's date).
- `ERROR`: the record could not be fetched or parsed.

`radar.collect` exits 0 when no monitor is `ERROR` and record is `OK`, 1 when
any monitor is `ERROR`, and 2 when record is `INCIDENT` and no monitor is
`ERROR`. The workflow commits the data first and then fails the job on a
non-zero code, so GitHub notifies the repository owner.

## Schedule

`.github/workflows/radar.yml` runs daily at 09:37 UTC and on manual dispatch.
A run started before about 09:05 UTC reports the record as INCIDENT by
design, because the newest record is then two days old.
The record of the previous UTC day is generated at about 09:01 UTC, so the run
is placed after that publication, and off the hour because GitHub delays
schedules that fire at the top of an hour. It runs the tests first, then the
collector, then commits `data/`.

## Run locally

    python3 -m unittest discover -s tests -v
    python3 -m radar.collect --data-dir data

Options: `--date YYYY-MM-DD` (default: today, UTC) and `--now
YYYY-MM-DDTHH:MM:SSZ` (default: the current UTC time). Re-running for the same
date overwrites that date's files and still compares with the previous earlier
date. Python 3.10 to 3.13, standard library only. If the environment variable
`GITHUB_TOKEN` is set, it is sent only to api.github.com and is never written
to any file.
