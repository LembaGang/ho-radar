"""ho-radar collector: observe the fixed sources, write a dated snapshot and a
mechanical verdict per monitor.

Python standard library only. All network access goes through one injectable
function, fetch(url) -> (status, body), so tests never touch the network.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from radar import sources as S

USER_AGENT = "ho-radar/1 (+https://github.com/LembaGang/ho-radar)"
TIMEOUT = 30
RETRY_SLEEPS = (2, 5)
MAX_ATTEMPTS = 3
MAX_BYTES = 32 * 1024 * 1024
MAX_RSS_ITEMS = 50
MAX_LINE = 120
MAX_ERROR = 300
DEADLINE_SECONDS = 600
HANDLE_RE = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]*")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NOW_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def build_request(url, token=None):
    """Build the GET request. The token goes only to api.github.com, and as an
    unredirected header so that it is not forwarded if a redirect is followed."""
    host = urllib.parse.urlsplit(url).hostname
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    if host == "api.github.com":
        req.add_header("Accept", "application/vnd.github+json")
        if token:
            req.add_unredirected_header("Authorization", "Bearer " + token)
    return req


def fetch_default(url):
    """The real fetcher: returns (status, body). Raises on timeout or network
    failure; HTTP error statuses are returned, not raised."""
    req = build_request(url, os.environ.get("GITHUB_TOKEN"))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(MAX_BYTES + 1)
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_BYTES + 1)
        except Exception:
            body = b""
        return exc.code, body
    if len(body) > MAX_BYTES:
        raise ValueError("response larger than %d bytes" % MAX_BYTES)
    return status, body


def scrub(title):
    """Titles are stored as published, with every @handle replaced."""
    if title is None:
        return None
    return HANDLE_RE.sub("@[handle]", title)


def monitor_regex(monitor):
    pattern = S.KEYWORDS.get(monitor)
    return re.compile(pattern, re.IGNORECASE) if pattern else None


class DeadlineError(Exception):
    pass


def clip(text, limit=MAX_ERROR):
    text = " ".join(str(text).split())
    return text[:limit]


def get_with_retry(fetch, url, sleep):
    """Up to MAX_ATTEMPTS attempts with sleeps of 2 s then 5 s between them.
    A 404 is final and is not retried. Returns (status, body, error); error is
    None when the final status is 2xx."""
    status = None
    body = b""
    err = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            status, body = fetch(url)
            err = None
        except DeadlineError:
            return None, b"", "deadline"
        except Exception as exc:  # timeout, DNS failure, oversize, ...
            status, body = None, b""
            err = clip("%s: %s" % (type(exc).__name__, exc))
        if status is not None and 200 <= status < 300:
            return status, body, None
        if status == 404:
            break
        if attempt < MAX_ATTEMPTS - 1:
            sleep(RETRY_SLEEPS[attempt])
    if err is None:
        err = "HTTP %s" % status
    return status, body, err


class FetchProblem(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


# ---------------------------------------------------------------------------
# normalisation: only the allowed fields ever leave the payload
# ---------------------------------------------------------------------------

def _json(body):
    return json.loads(body.decode("utf-8"))


def _first_line(text):
    return (text or "").split("\n", 1)[0].rstrip("\r")[:MAX_LINE]


def _label(label):
    if isinstance(label, dict):
        return str(label["name"])
    return str(label)


def norm_gh_issue(src, body, fetch, sleep):
    d = _json(body)
    comments = int(d["comments"])
    last = None
    if comments > 0:
        url = "%s/comments?per_page=1&page=%d" % (src["url"], comments)
        status, cbody, err = get_with_retry(fetch, url, sleep)
        if err is not None:
            raise FetchProblem(status, "last comment fetch failed: " + err)
        arr = _json(cbody)
        if not isinstance(arr, list) or not arr:
            raise ValueError("last comment payload is not a non-empty list")
        c = arr[0]
        last = {"id": c["id"], "created_at": c["created_at"], "html_url": c["html_url"]}
    return {
        "number": d["number"],
        "title": scrub(d["title"]),
        "state": d["state"],
        "comments": comments,
        "updated_at": d["updated_at"],
        "closed_at": d.get("closed_at"),
        "labels": sorted(_label(x) for x in d.get("labels") or []),
        "html_url": d["html_url"],
        "last_comment": last,
    }


def norm_gh_pull(src, body, fetch, sleep):
    d = _json(body)
    return {
        "number": d["number"],
        "title": scrub(d["title"]),
        "state": d["state"],
        "merged": d["merged"],
        "merged_at": d.get("merged_at"),
        "updated_at": d["updated_at"],
        "comments": d["comments"],
        "review_comments": d["review_comments"],
        "html_url": d["html_url"],
    }


def _commit_date(commit):
    committer = commit.get("committer")
    date = committer.get("date") if isinstance(committer, dict) else None
    if date is None:
        # de-identified fixture form: the date sits directly on the commit
        date = commit.get("date")
    return date


def norm_gh_commits(src, body, fetch, sleep):
    arr = _json(body)
    if not isinstance(arr, list):
        raise ValueError("commits payload is not a list")
    regex = monitor_regex(src.get("monitor"))
    items = []
    for c in arr:
        commit = c["commit"]
        # the message is read here and discarded: only the matched keywords
        # of its first line are stored
        line = _first_line(commit["message"])
        terms = set()
        if regex is not None:
            terms = set(m.group(0).lower() for m in regex.finditer(line))
        items.append({
            "sha": c["sha"],
            "date": _commit_date(commit),
            "html_url": c["html_url"],
            "hit_terms": sorted(terms),
        })
    return {"items": items}


def norm_gh_releases(src, body, fetch, sleep):
    arr = _json(body)
    if not isinstance(arr, list):
        raise ValueError("releases payload is not a list")
    items = []
    for r in arr:
        items.append({
            "tag": r["tag_name"],
            "title": scrub(r.get("name")),
            "published_at": r.get("published_at"),
            "prerelease": r.get("prerelease"),
            "draft": r.get("draft"),
            "html_url": r["html_url"],
        })
    return {"items": items}


def norm_gh_pulls_list(src, body, fetch, sleep):
    arr = _json(body)
    if not isinstance(arr, list):
        raise ValueError("pulls payload is not a list")
    items = []
    for p in arr:
        items.append({
            "number": p["number"],
            "title": scrub(p["title"]),
            "state": p["state"],
            "updated_at": p["updated_at"],
            "merged_at": p.get("merged_at"),
            "html_url": p["html_url"],
        })
    return {"items": items}


def _local(tag, root_ns):
    """Local name of an element tag, or None when it is in a foreign namespace."""
    if tag.startswith("{"):
        ns, name = tag[1:].split("}", 1)
        return name if ns == root_ns else None
    return tag


def norm_rss(src, body, fetch, sleep):
    lowered = body[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in body.lower():
        raise ValueError("DTD or entity declarations are not accepted")
    root = ET.fromstring(body)
    root_ns = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if _local(root.tag, root_ns) != "rss":
        raise ValueError("root element is not rss")
    channel = None
    for child in root:
        if _local(child.tag, root_ns) == "channel":
            channel = child
            break
    if channel is None:
        raise ValueError("no channel element")

    def text_of(item, name):
        for child in item:
            if _local(child.tag, root_ns) == name:
                return (child.text or "").strip()
        return None

    items = []
    for child in channel:
        if _local(child.tag, root_ns) != "item":
            continue
        entry = {
            "title": scrub(text_of(child, "title")),
            "link": text_of(child, "link"),
            "guid": text_of(child, "guid"),
            "pub_date": text_of(child, "pubDate"),
        }
        if not entry["guid"] and not entry["link"]:
            raise ValueError("feed item has neither guid nor link")
        items.append(entry)
        if len(items) >= MAX_RSS_ITEMS:
            break
    return {"items": items}


def norm_ho_record(src, body, fetch, sleep):
    d = _json(body)
    newest = d["newest"]
    status, _, uerr = get_with_retry(fetch, newest["url"], sleep)
    if uerr == "deadline":
        raise FetchProblem(None, "deadline")
    return {
        "schema": d["schema"],
        "expected_newest_date": d["expected_newest_date"],
        "newest_date": newest["date"],
        "newest_sequence": newest["sequence"],
        "newest_record_sha256": newest["record_sha256"],
        "late": newest["late"],
        "newest_url": newest["url"],
        "failed_steps_count": len(d["failed_steps"]),
        "ots_failed": d["ots_proofs"]["failed"],
        "ots_pending": d["ots_proofs"]["pending"],
        "records": d["records"],
        "chain_head_sequence": d["chain_head"]["sequence"],
        "newest_url_status": status,
    }


NORMALISERS = {
    "gh_issue": norm_gh_issue,
    "gh_pull": norm_gh_pull,
    "gh_commits": norm_gh_commits,
    "gh_releases": norm_gh_releases,
    "gh_pulls_list": norm_gh_pulls_list,
    "rss": norm_rss,
    "ho_record": norm_ho_record,
}


def observe(src, fetch, sleep):
    status, body, err = get_with_retry(fetch, src["url"], sleep)
    if err is not None:
        return {"status": "ERROR", "http_status": status, "error": clip(err), "data": None}
    try:
        data = NORMALISERS[src["kind"]](src, body, fetch, sleep)
    except FetchProblem as exc:
        return {"status": "ERROR", "http_status": exc.status or status,
                "error": clip(exc), "data": None}
    except Exception as exc:
        return {"status": "ERROR", "http_status": status,
                "error": clip("parse failure: %s: %s" % (type(exc).__name__, exc)),
                "data": None}
    return {"status": "OK", "http_status": status, "error": None, "data": data}


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

ISSUE_FIELDS = ("state", "comments", "updated_at", "closed_at", "labels", "last_comment")
PULL_FIELDS = ("state", "merged", "merged_at", "updated_at", "comments", "review_comments")


def _issue_value(data, field):
    if field == "last_comment":
        return (data.get("last_comment") or {}).get("id")
    return data.get(field)


def _changed(cur, prev, fields, getter):
    return sorted(f for f in fields if getter(cur, f) != getter(prev, f))


def _key_commit(it):
    return it["sha"]


def _key_release(it):
    return it["tag"]


def _key_pull(it):
    return (it["number"], it["updated_at"] or "")


def _key_rss(it):
    return it.get("guid") or it.get("link") or ""


def _text_release(it):
    return "%s %s" % (it["tag"] or "", it["title"] or "")


def _text_title(it):
    return it["title"] or ""


LIST_KINDS = {
    "gh_commits": (_key_commit, None),
    "gh_releases": (_key_release, _text_release),
    "gh_pulls_list": (_key_pull, _text_title),
    "rss": (_key_rss, _text_title),
}


def _pull_is_significant(item, prev_by_number):
    """A pull request counts when it is new to the list or newly changed in
    state or merge time; a bare updated_at change does not."""
    before = prev_by_number.get(item["number"])
    if before is None:
        return True
    return (before.get("state") != item["state"]
            or before.get("merged_at") != item["merged_at"])


def diff_source(entry, prev_obs, regex):
    """Compare one OK entry with the previous observation (a data dict)."""
    kind = entry["kind"]
    cur = entry["data"]
    res = {"id": entry["id"], "status": "OK", "moved": False}
    if prev_obs is None:
        res["baseline"] = True
        return res
    if kind == "gh_issue":
        changes = _changed(cur, prev_obs, ISSUE_FIELDS, _issue_value)
        res["changes"] = changes
        res["moved"] = bool(changes)
    elif kind == "gh_pull":
        changes = _changed(cur, prev_obs, PULL_FIELDS, lambda d, f: d.get(f))
        res["changes"] = changes
        res["moved"] = bool(changes)
    elif kind in LIST_KINDS:
        key, text = LIST_KINDS[kind]
        prev_items = prev_obs.get("items", [])
        seen = set(key(it) for it in prev_items)
        new_items = [it for it in cur["items"] if key(it) not in seen]
        if regex is None:
            hits = list(new_items)
        elif kind == "gh_commits":
            hits = [it for it in new_items if it["hit_terms"]]
        else:
            hits = [it for it in new_items if regex.search(text(it))]
        if kind == "gh_pulls_list" and regex is not None:
            by_number = dict((it["number"], it) for it in prev_items)
            hits = [it for it in hits if _pull_is_significant(it, by_number)]
        hits.sort(key=key)
        res["hits"] = hits
        res["other_new_count"] = len(new_items) - len(hits)
        res["moved"] = bool(hits)
        if kind == "gh_commits" and len(cur["items"]) >= 30 and len(new_items) == len(cur["items"]):
            res["possibly_truncated"] = True
    else:
        # ho_record and anything else: judged by checks, not by diff
        res["moved"] = False
    return res


def record_checks(data, date, prev=None, prev_date=None):
    """Named boolean checks on the record. prev is the last good record
    observation (a data dict) and prev_date the date it was made; checks that
    do not apply are left out."""
    try:
        yesterday = (datetime.date.fromisoformat(date) - datetime.timedelta(days=1)).isoformat()
    except ValueError:
        yesterday = None
    checks = {
        "newest_is_expected": data["newest_date"] == data["expected_newest_date"],
        "not_late": data["late"] is False,
        "no_failed_steps": data["failed_steps_count"] == 0,
        "no_ots_failed": data["ots_failed"] == 0,
        "newest_is_yesterday": yesterday is not None and data["newest_date"] == yesterday,
        "newest_url_ok": data["newest_url_status"] == 200,
    }
    if prev is not None:
        prev_seq = prev.get("chain_head_sequence")
        if prev_seq is not None:
            checks["sequence_not_regressed"] = data["chain_head_sequence"] >= prev_seq
        prev_newest = prev.get("newest_date")
        if prev_newest is not None and data["newest_date"] == prev_newest:
            checks["record_not_rewritten"] = (
                data["newest_record_sha256"] == prev.get("newest_record_sha256"))
        if prev_newest is not None and prev_date is not None and date > prev_date:
            checks["advanced_since_previous"] = data["newest_date"] > prev_newest
    return checks


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def dumps(obj):
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def asc(text):
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def write_bytes(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def load_previous(data_dir, date):
    """Newest data/<YYYY-MM-DD>/snapshot.json with a date strictly before date."""
    best = None
    if os.path.isdir(data_dir):
        for name in os.listdir(data_dir):
            if DATE_RE.match(name) and name < date:
                if os.path.isfile(os.path.join(data_dir, name, "snapshot.json")):
                    if best is None or name > best:
                        best = name
    if best is None:
        return None, None
    with open(os.path.join(data_dir, best, "snapshot.json"), "rb") as fh:
        return best, json.loads(fh.read().decode("utf-8"))


def prev_observation(prev_entry):
    if prev_entry is None:
        return None
    if prev_entry["status"] == "OK":
        return prev_entry["data"]
    return prev_entry.get("last_ok_data")


def prev_observation_date(prev_entry, prev_date):
    if prev_entry is None:
        return None
    if prev_entry["status"] == "OK":
        return prev_date
    return prev_entry.get("last_ok_date")


def render_markdown(summary):
    lines = ["# ho-radar summary " + summary["date"], ""]
    lines.append("generated_at: " + summary["generated_at"])
    lines.append("previous snapshot: " + (summary["previous_date"] or "none (first run)"))
    lines.append("exit code: %d" % summary["exit_code"])
    for name in sorted(summary["monitors"]):
        mon = summary["monitors"][name]
        lines.append("")
        lines.append("## %s: %s" % (name, mon["verdict"]))
        for src in mon["sources"]:
            line = "- %s: %s" % (src["id"], src["status"])
            if src["status"] == "ERROR":
                line += " (%s)" % src.get("error")
            elif src.get("baseline"):
                line += ", no previous observation"
            elif src["moved"]:
                line += ", moved"
                if src.get("changes"):
                    line += " [%s]" % ", ".join(src["changes"])
            else:
                line += ", no movement"
            if src.get("other_new_count"):
                line += ", %d other new item(s)" % src["other_new_count"]
            if src.get("possibly_truncated"):
                line += ", possibly truncated"
            lines.append(line)
            for hit in src.get("hits", []):
                if "hit_terms" in hit:
                    label = "sha %s terms: %s" % (hit["sha"][:12], ", ".join(hit["hit_terms"]) or "none")
                else:
                    label = hit.get("title") or hit.get("tag") or ""
                where = hit.get("html_url") or hit.get("link") or ""
                lines.append("  - hit: %s %s" % (label, where))
        for cname in sorted(mon.get("checks", {})):
            lines.append("- check %s: %s" % (cname, "pass" if mon["checks"][cname] else "FAIL"))
    return asc("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# main run
# ---------------------------------------------------------------------------

def run(data_dir, date, now, fetch, sleep, clock=None):
    clock = clock or time.monotonic
    started = clock()
    real_fetch = fetch

    def fetch(url):
        # no new request is started once the total deadline has passed
        if clock() - started >= DEADLINE_SECONDS:
            raise DeadlineError(url)
        return real_fetch(url)

    prev_date, prev = load_previous(data_dir, date)
    prev_by_id = {}
    if prev is not None:
        prev_by_id = dict((e["id"], e) for e in prev["sources"])

    entries = []
    for src in sorted(S.SOURCES, key=lambda s: s["id"]):
        obs = observe(src, fetch, sleep)
        if obs["status"] == "OK" and src["kind"] in LIST_KINDS and not obs["data"]["items"]:
            before = prev_observation(prev_by_id.get(src["id"]))
            if before and before.get("items"):
                obs = {"status": "ERROR", "http_status": obs["http_status"],
                       "error": "empty where previous had %d" % len(before["items"]),
                       "data": None}
        entry = {
            "id": src["id"],
            "kind": src["kind"],
            "monitor": src["monitor"],
            "url": src["url"],
            "status": obs["status"],
            "http_status": obs["http_status"],
            "error": obs["error"],
            "data": obs["data"],
            "last_ok_date": None,
            "last_ok_data": None,
        }
        if obs["status"] == "OK":
            entry["last_ok_date"] = date
        else:
            p = prev_by_id.get(src["id"])
            if p is not None:
                if p["status"] == "OK":
                    entry["last_ok_date"] = prev_date
                    entry["last_ok_data"] = p["data"]
                else:
                    entry["last_ok_date"] = p.get("last_ok_date")
                    entry["last_ok_data"] = p.get("last_ok_data")
        entries.append(entry)

    monitors = {}
    for mname in S.MONITORS:
        regex = monitor_regex(mname)
        results = []
        checks = None
        for entry in entries:
            if entry["monitor"] != mname:
                continue
            if entry["status"] == "ERROR":
                results.append({"id": entry["id"], "status": "ERROR", "moved": False,
                                "error": entry["error"]})
                continue
            if prev is None:
                results.append({"id": entry["id"], "status": "OK", "moved": False})
                continue
            prev_obs = prev_observation(prev_by_id.get(entry["id"]))
            results.append(diff_source(entry, prev_obs, regex))
        results.sort(key=lambda r: r["id"])
        any_error = any(r["status"] == "ERROR" for r in results)
        if mname == "record":
            if any_error:
                verdict = "ERROR"
            else:
                rec = [e for e in entries if e["monitor"] == "record"][0]
                before = prev_by_id.get(rec["id"])
                checks = record_checks(rec["data"], date, prev_observation(before),
                                       prev_observation_date(before, prev_date))
                verdict = "OK" if all(checks.values()) else "INCIDENT"
        elif any_error:
            verdict = "ERROR"
        elif prev is not None and any(r["moved"] for r in results):
            verdict = "MOVED"
        elif prev is None or any(r.get("baseline") for r in results):
            verdict = "BASELINE"
        else:
            verdict = "QUIET"
        mon = {"verdict": verdict, "sources": results}
        if checks is not None:
            mon["checks"] = checks
        monitors[mname] = mon

    if any(m["verdict"] == "ERROR" for m in monitors.values()):
        code = 1
    elif monitors["record"]["verdict"] == "INCIDENT":
        code = 2
    else:
        code = 0

    summary = {
        "schema": "ho-radar-summary/1",
        "date": date,
        "generated_at": now,
        "previous_date": prev_date,
        "monitors": monitors,
        "exit_code": code,
    }
    snapshot = {
        "schema": "ho-radar-snapshot/1",
        "date": date,
        "generated_at": now,
        "sources": entries,
    }

    day_dir = os.path.join(data_dir, date)
    os.makedirs(day_dir, exist_ok=True)
    summary_bytes = dumps(summary).encode("ascii")
    write_bytes(os.path.join(day_dir, "snapshot.json"), dumps(snapshot).encode("ascii"))
    write_bytes(os.path.join(day_dir, "summary.json"), summary_bytes)
    write_bytes(os.path.join(day_dir, "SUMMARY.md"), render_markdown(summary).encode("ascii"))
    latest = {
        "date": date,
        "summary": "data/%s/summary.json" % date,
        "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
    }
    write_bytes(os.path.join(data_dir, "latest.json"), dumps(latest).encode("ascii"))
    return summary


def main(argv=None, fetch=None, sleep=None, clock=None):
    parser = argparse.ArgumentParser(prog="radar.collect")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--date", default=None)
    parser.add_argument("--now", default=None)
    args = parser.parse_args(argv)

    utc_now = datetime.datetime.now(datetime.timezone.utc)
    date = args.date or utc_now.strftime("%Y-%m-%d")
    now = args.now or utc_now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not DATE_RE.match(date):
        parser.error("--date must be YYYY-MM-DD")
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        parser.error("--date is not a real date")
    if not NOW_RE.match(now):
        parser.error("--now must be YYYY-MM-DDTHH:MM:SSZ")

    summary = run(args.data_dir, date, now,
                  fetch or fetch_default, sleep or time.sleep, clock)
    verdicts = " ".join("%s=%s" % (k, summary["monitors"][k]["verdict"])
                        for k in sorted(summary["monitors"]))
    print("ho-radar %s: %s exit=%d" % (date, verdicts, summary["exit_code"]))
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
