"""Tests for radar.collect. No test touches the network: every fetch goes
through a fake that serves the de-identified fixtures."""

import contextlib
import copy
import datetime
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar import collect, sources  # noqa: E402

FIX = ROOT / "tests" / "fixtures"

BANNED_KEYS = {"login", "user", "author", "committer", "email", "name", "body",
               "avatar_url", "gravatar_id", "creator"}
URL_KEYS = {"html_url", "link", "url", "newest_url"}
# an "@" directly after a word character, dot, plus or hyphen looks like an
# email address; "@scope/package" at the start of a word does not
EMAIL_LIKE = re.compile(r"[\w.+-]@")

D1 = "2026-10-22"
D2 = "2026-10-23"
D3 = "2026-10-24"

URL = dict((s["id"], s["url"]) for s in sources.SOURCES)
FILES = {
    "x402-issue-2291": "x402_issue_2291.json",
    "x402-issue-2332": "x402_issue_2332.json",
    "x402-issue-2357": "x402_issue_2357.json",
    "argentum-core-commits": "argentum_commits.json",
    "argentum-core-releases": "argentum_releases.json",
    "mcp-pr-2133": "mcp_pull_2133.json",
    "mcp-commits": "mcp_commits.json",
    "mcp-pulls-updated": "mcp_pulls_updated.json",
    "agt-releases": "agt_releases.json",
    "agt-commits": "agt_commits.json",
    "cf-blog": "cf_blog_rss.xml",
    "cf-changelog": "cf_changelog_rss.xml",
    "ho-record": "ho_record.json",
}
NEWEST_URL = "https://headlessoracle.com/record/2026-10-21"


def read_fixture(name):
    return (FIX / name).read_bytes()


def comments_url(sid, page):
    return "%s/comments?per_page=1&page=%d" % (URL[sid], page)


def comment_payload(cid, issue_html_url):
    return [{"id": cid, "created_at": "2026-10-22T09:00:00Z",
             "html_url": "%s#issuecomment-%d" % (issue_html_url, cid)}]


class FakeNet(object):
    """Serves fixtures by URL and records every request."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        for sid, fname in FILES.items():
            self.routes[URL[sid]] = (200, read_fixture(fname))
        self.routes[comments_url("x402-issue-2332", 236)] = (
            200, read_fixture("x402_issue_2332_lastcomment.json"))
        for sid, page in (("x402-issue-2291", 3), ("x402-issue-2357", 27)):
            issue = json.loads(read_fixture(FILES[sid]).decode("ascii"))
            self.set_json(comments_url(sid, page), comment_payload(111, issue["html_url"]))
        self.routes[NEWEST_URL] = (200, b"{}")

    def fetch(self, url):
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            return 404, b"not routed"
        if isinstance(route, Exception):
            raise route
        return route

    def json_of(self, sid_or_url):
        url = URL.get(sid_or_url, sid_or_url)
        return json.loads(self.routes[url][1].decode("utf-8"))

    def set_json(self, sid_or_url, obj, status=200):
        url = URL.get(sid_or_url, sid_or_url)
        self.routes[url] = (status, json.dumps(obj).encode("ascii"))

    def set_raw(self, sid_or_url, body, status=200):
        self.routes[URL.get(sid_or_url, sid_or_url)] = (status, body)

    def set_status(self, sid_or_url, status):
        self.routes[URL.get(sid_or_url, sid_or_url)] = (status, b"")

    def set_record_dates(self, newest, expected):
        rec = self.json_of("ho-record")
        rec["newest"]["date"] = newest
        rec["expected_newest_date"] = expected
        self.set_json("ho-record", rec)

    def count(self, url):
        return len([c for c in self.calls if c == url])

    def follow_date(self, date):
        """Make the record the one a run on `date` should see: newest is the
        previous UTC day, one record per day after the captured one."""
        n = (datetime.date.fromisoformat(date) - datetime.date.fromisoformat(D1)).days
        if n == 0:
            return
        rec = json.loads(read_fixture("ho_record.json").decode("ascii"))
        yesterday = (datetime.date.fromisoformat(date) - datetime.timedelta(days=1)).isoformat()
        rec["newest"]["date"] = yesterday
        rec["expected_newest_date"] = yesterday
        rec["newest"]["sequence"] += n
        rec["chain_head"]["sequence"] += n
        rec["records"] += n
        rec["newest"]["record_sha256"] = "%064x" % n
        self.set_json("ho-record", rec)


def fixture_commit(sha, message, date="2026-10-23T00:00:00Z"):
    return {"sha": sha,
            "html_url": "https://github.com/example/repo/commit/" + sha,
            "commit": {"message": message, "date": date}}


def prepend_commit(net, sid, commit):
    items = net.json_of(sid)
    items = [commit] + items[:-1]
    net.set_json(sid, items)


def rss_add_item(body, title, link, guid):
    item = ('<item><title>%s</title><link>%s</link>'
            '<guid isPermaLink="false">%s</guid>'
            '<pubDate>Thu, 22 Oct 2026 12:00:00 GMT</pubDate></item>\n    '
            % (title, link, guid)).encode("ascii")
    i = body.index(b"<item>")
    return body[:i] + item + body[i:]


class RadarCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-radar-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "data")
        self.net = FakeNet()
        self.sleeps = []

    def run_day(self, date, net=None, data=None, now=None, follow=True, clock=None):
        net = net or self.net
        data = data or self.data
        if follow:
            net.follow_date(date)
        argv = ["--data-dir", data, "--date", date,
                "--now", now or (date + "T05:17:00Z")]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = collect.main(argv, fetch=net.fetch, sleep=self.sleeps.append, clock=clock)
        return code, self.summary(date, data)

    def summary(self, date, data=None):
        path = os.path.join(data or self.data, date, "summary.json")
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("ascii"))

    def snapshot(self, date, data=None):
        path = os.path.join(data or self.data, date, "snapshot.json")
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("ascii"))

    def verdicts(self, summary):
        return dict((k, v["verdict"]) for k, v in summary["monitors"].items())

    def source(self, summary, monitor, sid):
        for s in summary["monitors"][monitor]["sources"]:
            if s["id"] == sid:
                return s
        self.fail("source %s not in monitor %s" % (sid, monitor))


class TestVerdicts(RadarCase):
    def test_01_first_run_all_three_monitors_baseline_and_record_ok(self):
        code, s = self.run_day(D1)
        self.assertEqual(self.verdicts(s), {
            "kill-trigger": "BASELINE", "x402-issues": "BASELINE",
            "mcp-registry": "BASELINE", "record": "OK"})
        self.assertEqual(code, 0)
        self.assertEqual(s["exit_code"], 0)
        self.assertIsNone(s["previous_date"])
        self.assertEqual(s["generated_at"], D1 + "T05:17:00Z")
        self.assertTrue(all(s["monitors"]["record"]["checks"].values()))
        for name in ("snapshot.json", "summary.json", "SUMMARY.md"):
            self.assertTrue(os.path.isfile(os.path.join(self.data, D1, name)))
        latest = json.loads(Path(self.data, "latest.json").read_text())
        self.assertEqual(latest["date"], D1)
        self.assertEqual(latest["summary"], "data/%s/summary.json" % D1)

    def test_02_second_run_with_identical_payloads_is_quiet(self):
        self.run_day(D1)
        code, s = self.run_day(D2)
        self.assertEqual(self.verdicts(s), {
            "kill-trigger": "QUIET", "x402-issues": "QUIET",
            "mcp-registry": "QUIET", "record": "OK"})
        self.assertEqual(code, 0)
        self.assertEqual(s["previous_date"], D1)
        for mon in ("kill-trigger", "x402-issues", "mcp-registry"):
            for src in s["monitors"][mon]["sources"]:
                self.assertFalse(src["moved"], src["id"])

    def test_03_x402_issue_comment_change_moves_x402_issues_only(self):
        self.run_day(D1)
        issue = self.net.json_of("x402-issue-2332")
        issue["comments"] = 237
        issue["updated_at"] = "2026-10-23T01:00:00Z"
        self.net.set_json("x402-issue-2332", issue)
        self.net.set_json(comments_url("x402-issue-2332", 237),
                          comment_payload(9999999999, issue["html_url"]))
        code, s = self.run_day(D2)
        self.assertEqual(self.verdicts(s), {
            "kill-trigger": "QUIET", "x402-issues": "MOVED",
            "mcp-registry": "QUIET", "record": "OK"})
        self.assertEqual(code, 0)
        moved = self.source(s, "x402-issues", "x402-issue-2332")
        self.assertTrue(moved["moved"])
        self.assertEqual(moved["changes"], ["comments", "last_comment", "updated_at"])
        for sid in ("x402-issue-2291", "x402-issue-2357", "argentum-core-commits",
                    "argentum-core-releases"):
            self.assertFalse(self.source(s, "x402-issues", sid)["moved"], sid)
        snap = self.snapshot(D2)
        entry = [e for e in snap["sources"] if e["id"] == "x402-issue-2332"][0]
        self.assertEqual(entry["data"]["last_comment"]["id"], 9999999999)
        self.assertEqual(entry["data"]["comments"], 237)

    def test_04_agt_commit_hits_only_when_message_matches_keywords(self):
        # a hit: the message line contains "signed receipts"
        self.run_day(D1)
        sha = "a" * 40
        prepend_commit(self.net, "agt-commits",
                       fixture_commit(sha, "feat: signed receipts for tool calls (#9999)"))
        _, s = self.run_day(D2)
        self.assertEqual(s["monitors"]["kill-trigger"]["verdict"], "MOVED")
        res = self.source(s, "kill-trigger", "agt-commits")
        self.assertTrue(res["moved"])
        self.assertEqual([h["sha"] for h in res["hits"]], [sha])
        self.assertEqual(res["hits"][0]["hit_terms"], ["receipt", "signed"])
        self.assertNotIn("message_line", res["hits"][0])
        self.assertNotIn("signed receipts for tool calls",
                         Path(self.data, D2, "snapshot.json").read_text())
        self.assertEqual(res["other_new_count"], 0)
        self.assertEqual(s["monitors"]["x402-issues"]["verdict"], "QUIET")
        self.assertEqual(s["monitors"]["mcp-registry"]["verdict"], "QUIET")

        # not a hit: "fix typo" is counted but not listed
        net = FakeNet()
        data = os.path.join(self.tmp, "data-typo")
        self.run_day(D1, net=net, data=data)
        prepend_commit(net, "agt-commits", fixture_commit("b" * 40, "fix typo"))
        _, s2 = self.run_day(D2, net=net, data=data)
        res2 = self.source(s2, "kill-trigger", "agt-commits")
        self.assertFalse(res2["moved"])
        self.assertEqual(res2["hits"], [])
        self.assertEqual(res2["other_new_count"], 1)
        self.assertEqual(s2["monitors"]["kill-trigger"]["verdict"], "QUIET")

    def test_05_new_cloudflare_blog_item_with_attestation_moves_kill_trigger(self):
        self.run_day(D1)
        body = self.net.routes[URL["cf-blog"]][1]
        self.net.set_raw("cf-blog", rss_add_item(
            body, "Attestation for every request", "https://blog.cloudflare.com/new-post/",
            "01NEWGUID"))
        _, s = self.run_day(D2)
        self.assertEqual(s["monitors"]["kill-trigger"]["verdict"], "MOVED")
        res = self.source(s, "kill-trigger", "cf-blog")
        self.assertTrue(res["moved"])
        self.assertEqual([h["guid"] for h in res["hits"]], ["01NEWGUID"])
        self.assertEqual(res["hits"][0]["title"], "Attestation for every request")
        # a new item that does not match is counted, not listed
        net = FakeNet()
        data = os.path.join(self.tmp, "data-plain")
        self.run_day(D1, net=net, data=data)
        net.set_raw("cf-blog", rss_add_item(
            net.routes[URL["cf-blog"]][1], "Faster images", "https://blog.cloudflare.com/img/", "01X"))
        _, s2 = self.run_day(D2, net=net, data=data)
        res2 = self.source(s2, "kill-trigger", "cf-blog")
        self.assertFalse(res2["moved"])
        self.assertEqual(res2["other_new_count"], 1)
        self.assertEqual(s2["monitors"]["kill-trigger"]["verdict"], "QUIET")

    def test_06_new_mcp_pull_about_registry_moves_mcp_registry(self):
        self.run_day(D1)
        pulls = self.net.json_of("mcp-pulls-updated")
        pulls = [{"number": 9001, "title": "Update registry schema", "state": "open",
                  "updated_at": "2026-10-23T02:00:00Z", "merged_at": None,
                  "html_url": "https://github.com/example/repo/pull/9001"}] + pulls[:-1]
        self.net.set_json("mcp-pulls-updated", pulls)
        _, s = self.run_day(D2)
        self.assertEqual(s["monitors"]["mcp-registry"]["verdict"], "MOVED")
        pr = self.source(s, "mcp-registry", "mcp-pr-2133")
        self.assertFalse(pr["moved"])
        self.assertEqual(pr["changes"], [])
        lst = self.source(s, "mcp-registry", "mcp-pulls-updated")
        self.assertTrue(lst["moved"])
        self.assertEqual([h["number"] for h in lst["hits"]], [9001])
        self.assertEqual(s["monitors"]["kill-trigger"]["verdict"], "QUIET")
        self.assertEqual(s["monitors"]["x402-issues"]["verdict"], "QUIET")

    def test_07_fetch_error_is_error_then_last_ok_carry_forward_gives_quiet(self):
        code, s1 = self.run_day(D1)
        self.assertEqual(code, 0)
        good = [e for e in self.snapshot(D1)["sources"]
                if e["id"] == "argentum-core-commits"][0]["data"]
        url = URL["argentum-core-commits"]
        self.net.set_status("argentum-core-commits", 500)
        self.sleeps[:] = []
        self.net.calls[:] = []
        code, s2 = self.run_day(D2)
        self.assertEqual(code, 1)
        self.assertEqual(s2["exit_code"], 1)
        self.assertEqual(self.verdicts(s2), {
            "kill-trigger": "QUIET", "x402-issues": "ERROR",
            "mcp-registry": "QUIET", "record": "OK"})
        self.assertEqual(self.net.count(url), 3)
        self.assertEqual(self.sleeps, [2, 5])
        res = self.source(s2, "x402-issues", "argentum-core-commits")
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error"], "HTTP 500")
        entry = [e for e in self.snapshot(D2)["sources"]
                 if e["id"] == "argentum-core-commits"][0]
        self.assertEqual(entry["status"], "ERROR")
        self.assertEqual(entry["http_status"], 500)
        self.assertIsNone(entry["data"])
        self.assertEqual(entry["last_ok_date"], D1)
        self.assertEqual(entry["last_ok_data"], good)

        # the source comes back unchanged against the last good data
        self.net = FakeNet()
        code, s3 = self.run_day(D3)
        self.assertEqual(code, 0)
        self.assertEqual(self.verdicts(s3)["x402-issues"], "QUIET")
        ok = [e for e in self.snapshot(D3)["sources"]
              if e["id"] == "argentum-core-commits"][0]
        self.assertEqual(ok["last_ok_date"], D3)
        self.assertIsNone(ok["last_ok_data"])

    def test_07b_change_during_outage_is_seen_against_last_good_data(self):
        self.run_day(D1)
        self.net.set_status("argentum-core-commits", 500)
        self.run_day(D2)
        self.net = FakeNet()
        sha = "c" * 40
        prepend_commit(self.net, "argentum-core-commits", fixture_commit(sha, "new work"))
        _, s3 = self.run_day(D3)
        self.assertEqual(self.verdicts(s3)["x402-issues"], "MOVED")
        res = self.source(s3, "x402-issues", "argentum-core-commits")
        self.assertEqual([h["sha"] for h in res["hits"]], [sha])

    def test_07c_error_on_the_first_run_is_error_not_baseline(self):
        self.net.set_status("agt-commits", 503)
        code, s = self.run_day(D1)
        self.assertEqual(s["monitors"]["kill-trigger"]["verdict"], "ERROR")
        self.assertEqual(s["monitors"]["x402-issues"]["verdict"], "BASELINE")
        self.assertEqual(code, 1)

    def test_07d_timeouts_and_parse_failures_are_errors(self):
        self.net.routes[URL["cf-blog"]] = TimeoutError("timed out")
        self.net.set_raw("mcp-commits", b"this is not json")
        self.net.set_raw("cf-changelog", b"<rss><channel><item></rss>")
        self.run_day(D1)
        code, s = self.run_day(D2)
        self.assertEqual(code, 1)
        self.assertEqual(self.source(s, "kill-trigger", "cf-blog")["status"], "ERROR")
        self.assertIn("TimeoutError", self.source(s, "kill-trigger", "cf-blog")["error"])
        self.assertEqual(self.net.count(URL["cf-blog"]), 6)
        self.assertEqual(self.source(s, "mcp-registry", "mcp-commits")["status"], "ERROR")
        self.assertEqual(self.source(s, "kill-trigger", "cf-changelog")["status"], "ERROR")
        for src in s["monitors"]["kill-trigger"]["sources"]:
            if src["status"] == "ERROR":
                self.assertLessEqual(len(src["error"]), 300)

    def test_07e_failed_last_comment_fetch_makes_the_issue_source_error(self):
        self.net.set_status(comments_url("x402-issue-2332", 236), 502)
        code, s = self.run_day(D1)
        self.assertEqual(code, 1)
        self.assertEqual(self.source(s, "x402-issues", "x402-issue-2332")["status"], "ERROR")
        self.assertEqual(s["monitors"]["x402-issues"]["verdict"], "ERROR")
        self.assertEqual(self.net.count(comments_url("x402-issue-2332", 236)), 3)

    def test_08_record_incidents_name_the_failing_check_and_exit_2(self):
        def late(rec):
            rec["newest"]["late"] = True

        def failed(rec):
            rec["failed_steps"] = ["ots-stamp"]

        def mismatch(rec):
            rec["expected_newest_date"] = "2026-10-20"

        def stale(rec):
            rec["newest"]["date"] = "2026-10-17"
            rec["expected_newest_date"] = "2026-10-17"

        def ots_failed(rec):
            rec["ots_proofs"]["failed"] = 1

        cases = [
            ("late", late, None, "not_late"),
            ("failed_steps", failed, None, "no_failed_steps"),
            ("newest_not_expected", mismatch, None, "newest_is_expected"),
            ("newest_url_404", None, 404, "newest_url_ok"),
            ("newest_five_days_old", stale, None, "newest_is_yesterday"),
            ("ots_failed", ots_failed, None, "no_ots_failed"),
        ]
        for label, mutate, url_status, failing in cases:
            with self.subTest(case=label):
                net = FakeNet()
                data = os.path.join(self.tmp, "data-" + label)
                if mutate is not None:
                    rec = net.json_of("ho-record")
                    mutate(rec)
                    net.set_json("ho-record", rec)
                if url_status is not None:
                    net.set_status(NEWEST_URL, url_status)
                code, s = self.run_day(D1, net=net, data=data)
                record = s["monitors"]["record"]
                self.assertEqual(record["verdict"], "INCIDENT")
                self.assertEqual(code, 2)
                self.assertEqual(s["exit_code"], 2)
                expected = dict((name, name != failing) for name in record["checks"])
                self.assertEqual(sorted(record["checks"]), sorted([
                    "newest_is_expected", "not_late", "no_failed_steps", "no_ots_failed",
                    "newest_is_yesterday", "newest_url_ok"]))
                self.assertEqual(record["checks"], expected)

    def test_08b_record_fetch_failure_is_error(self):
        self.net.set_status("ho-record", 500)
        code, s = self.run_day(D1)
        self.assertEqual(s["monitors"]["record"]["verdict"], "ERROR")
        self.assertNotIn("checks", s["monitors"]["record"])
        self.assertEqual(code, 1)

    def test_08c_error_outranks_incident_in_the_exit_code(self):
        rec = self.net.json_of("ho-record")
        rec["newest"]["late"] = True
        self.net.set_json("ho-record", rec)
        self.net.set_status("agt-commits", 500)
        code, s = self.run_day(D1)
        self.assertEqual(s["monitors"]["record"]["verdict"], "INCIDENT")
        self.assertEqual(s["monitors"]["kill-trigger"]["verdict"], "ERROR")
        self.assertEqual(code, 1)

    def test_09_same_inputs_give_byte_identical_outputs(self):
        outputs = []
        for label in ("a", "b"):
            net = FakeNet()
            data = os.path.join(self.tmp, "det-" + label)
            self.run_day(D1, net=net, data=data)
            prepend_commit(net, "agt-commits",
                           fixture_commit("d" * 40, "verify signature chain (#1)"))
            self.run_day(D2, net=net, data=data)
            files = {}
            for day in (D1, D2):
                for name in ("snapshot.json", "summary.json", "SUMMARY.md"):
                    files[day + "/" + name] = Path(data, day, name).read_bytes()
            files["latest.json"] = Path(data, "latest.json").read_bytes()
            outputs.append(files)
        self.assertEqual(sorted(outputs[0]), sorted(outputs[1]))
        self.assertEqual(len(outputs[0]), 7)
        for name in outputs[0]:
            self.assertEqual(outputs[0][name], outputs[1][name], name)

    def test_09b_rerunning_a_date_overwrites_and_still_compares_with_earlier_date(self):
        self.run_day(D1)
        self.run_day(D2)
        prepend_commit(self.net, "agt-commits",
                       fixture_commit("e" * 40, "tamper evident log"))
        _, again = self.run_day(D2)
        self.assertEqual(again["previous_date"], D1)
        self.assertEqual(again["monitors"]["kill-trigger"]["verdict"], "MOVED")
        _, first = self.run_day(D1)
        self.assertIsNone(first["previous_date"])
        self.assertEqual(first["monitors"]["kill-trigger"]["verdict"], "BASELINE")
        latest = json.loads(Path(self.data, "latest.json").read_text())
        self.assertEqual(latest["date"], D1)

    def test_09c_latest_json_digest_matches_summary_file(self):
        self.run_day(D1)
        latest = json.loads(Path(self.data, "latest.json").read_text())
        import hashlib
        digest = hashlib.sha256(Path(self.data, D1, "summary.json").read_bytes()).hexdigest()
        self.assertEqual(latest["summary_sha256"], digest)

    def test_09d_possibly_truncated_when_all_thirty_commits_are_new(self):
        self.run_day(D1)
        items = [fixture_commit("%040x" % (i + 1), "work %d" % i) for i in range(30)]
        self.net.set_json("argentum-core-commits", items)
        _, s = self.run_day(D2)
        res = self.source(s, "x402-issues", "argentum-core-commits")
        self.assertTrue(res["possibly_truncated"])
        self.assertEqual(len(res["hits"]), 30)
        self.assertEqual(s["monitors"]["x402-issues"]["verdict"], "MOVED")


class TestNormalisation(unittest.TestCase):
    """Real-shaped payloads (with the identifying fields) must come out clean."""

    def fetch_none(self, url):
        raise AssertionError("no fetch expected: " + url)

    def test_commit_payload_keeps_only_allowed_fields(self):
        long_line = "x" * 200
        payload = [{
            "sha": "1" * 40,
            "html_url": "https://github.com/o/r/commit/" + "1" * 40,
            "author": {"login": "someone", "avatar_url": "https://example.invalid/a.png"},
            "committer": {"login": "web-flow"},
            "commit": {
                "author": {"name": "A Person", "email": "a@example.invalid", "date": "2026-10-01T00:00:00Z"},
                "committer": {"name": "GitHub", "email": "noreply@github.com", "date": "2026-10-02T00:00:00Z"},
                "message": long_line + "\n\nCo-authored-by: Someone <s@example.invalid>",
            },
        }, {
            "sha": "2" * 40,
            "html_url": "https://github.com/o/r/commit/" + "2" * 40,
            "commit": {"committer": {"name": "N", "date": "2026-10-03T00:00:00Z"},
                       "message": "first line\r\nsecond line"},
        }]
        payload.append({
            "sha": "3" * 40, "html_url": "https://github.com/o/r/commit/" + "3" * 40,
            "commit": {"committer": {"date": "2026-10-04T00:00:00Z"},
                       "message": "Merge pull request #5 from someone/branch: Signed Receipts, receipt"},
        })
        data = collect.norm_gh_commits({"monitor": "kill-trigger"}, json.dumps(payload).encode(),
                                       self.fetch_none, None)
        self.assertEqual(data["items"][0]["date"], "2026-10-02T00:00:00Z")
        self.assertEqual(data["items"][0]["hit_terms"], [])
        self.assertEqual(data["items"][1]["hit_terms"], [])
        self.assertEqual(data["items"][1]["date"], "2026-10-03T00:00:00Z")
        self.assertEqual(data["items"][2]["hit_terms"], ["receipt", "signed"])
        text = json.dumps(data)
        for banned in ("someone", "A Person", "example.invalid", "Co-authored", "web-flow",
                       "xxxx", "first line", "Merge", "branch", "message"):
            self.assertNotIn(banned, text)
        for item in data["items"]:
            self.assertEqual(sorted(item), ["date", "hit_terms", "html_url", "sha"])
        # a monitor without a keyword filter stores no terms
        plain = collect.norm_gh_commits({"monitor": "x402-issues"}, json.dumps(payload).encode(),
                                        self.fetch_none, None)
        self.assertTrue(all(i["hit_terms"] == [] for i in plain["items"]))

    def test_titles_have_at_handles_replaced(self):
        issue = {"number": 1, "title": "Thanks @octo-cat and @Foo9 for this", "state": "open",
                 "comments": 0, "updated_at": "x", "closed_at": None, "labels": [],
                 "html_url": "https://github.com/o/r/issues/1"}
        data = collect.norm_gh_issue({"url": "u"}, json.dumps(issue).encode(), self.fetch_none, None)
        self.assertEqual(data["title"], "Thanks @[handle] and @[handle] for this")
        pull = {"number": 2, "title": "Bump @scope/pkg", "state": "open", "merged": False,
                "merged_at": None, "updated_at": "x", "comments": 0, "review_comments": 0,
                "html_url": "h"}
        self.assertEqual(collect.norm_gh_pull({}, json.dumps(pull).encode(), self.fetch_none,
                                              None)["title"], "Bump @[handle]/pkg")
        rel = [{"tag_name": "v1", "name": "by @someone", "html_url": "h"}]
        self.assertEqual(collect.norm_gh_releases({}, json.dumps(rel).encode(), self.fetch_none,
                                                  None)["items"][0]["title"], "by @[handle]")
        lst = [{"number": 3, "title": "cc @a-b", "state": "open", "updated_at": "x", "html_url": "h"}]
        self.assertEqual(collect.norm_gh_pulls_list({}, json.dumps(lst).encode(), self.fetch_none,
                                                    None)["items"][0]["title"], "cc @[handle]")
        xml = b"<rss><channel><item><title>Hi @bob</title><link>l</link></item></channel></rss>"
        self.assertEqual(collect.norm_rss({}, xml, self.fetch_none, None)["items"][0]["title"],
                         "Hi @[handle]")

    def test_issue_payload_labels_become_plain_sorted_strings(self):
        payload = {
            "number": 7, "title": "T", "state": "open", "comments": 0,
            "updated_at": "2026-10-01T00:00:00Z", "closed_at": None,
            "labels": [{"name": "zeta", "color": "fff"}, {"name": "alpha"}],
            "html_url": "https://github.com/o/r/issues/7",
            "user": {"login": "someone"}, "body": "secret body text",
        }
        data = collect.norm_gh_issue({"url": "https://api.github.com/x"},
                                     json.dumps(payload).encode(), self.fetch_none, None)
        self.assertEqual(data["labels"], ["alpha", "zeta"])
        self.assertIsNone(data["last_comment"])
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("someone", json.dumps(data))

    def test_release_name_is_stored_as_title(self):
        payload = [{"tag_name": "v1", "name": "First", "published_at": "2026-10-01T00:00:00Z",
                    "prerelease": False, "draft": False, "html_url": "https://github.com/o/r/releases/v1",
                    "author": {"login": "someone"}, "body": "notes"}]
        data = collect.norm_gh_releases({}, json.dumps(payload).encode(), self.fetch_none, None)
        self.assertEqual(data["items"][0]["title"], "First")
        self.assertEqual(data["items"][0]["tag"], "v1")
        self.assertNotIn("someone", json.dumps(data))
        self.assertNotIn("notes", json.dumps(data))

    def test_rss_ignores_authors_and_handles_namespaces(self):
        xml = (
            '<?xml version="1.0"?>'
            '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
            '<atom:link href="x" rel="self"/>'
            '<item><title>One</title><link>https://e.invalid/1</link>'
            '<dc:creator>Some Person</dc:creator><author>a@e.invalid</author>'
            '<description>words</description>'
            '<pubDate>Wed, 21 Oct 2026 14:10:42 GMT</pubDate></item>'
            '</channel></rss>').encode()
        data = collect.norm_rss({}, xml, self.fetch_none, None)
        self.assertEqual(data["items"], [{"title": "One", "link": "https://e.invalid/1",
                                          "guid": None, "pub_date": "Wed, 21 Oct 2026 14:10:42 GMT"}])
        self.assertNotIn("Some Person", json.dumps(data))

    def test_rss_keeps_first_fifty_items_in_document_order(self):
        items = "".join("<item><title>t%d</title><link>l%d</link><guid>g%d</guid></item>" % (i, i, i)
                        for i in range(60))
        xml = ("<rss><channel>%s</channel></rss>" % items).encode()
        data = collect.norm_rss({}, xml, self.fetch_none, None)
        self.assertEqual(len(data["items"]), 50)
        self.assertEqual(data["items"][0]["guid"], "g0")
        self.assertEqual(data["items"][49]["guid"], "g49")

    def test_rss_with_entity_declarations_is_rejected(self):
        xml = b'<!DOCTYPE rss [<!ENTITY a "b">]><rss><channel></channel></rss>'
        with self.assertRaises(ValueError):
            collect.norm_rss({}, xml, self.fetch_none, None)

    def test_fixture_feeds_parse_with_their_namespace_declarations(self):
        for name in ("cf_blog_rss.xml", "cf_changelog_rss.xml"):
            data = collect.norm_rss({}, read_fixture(name), self.fetch_none, None)
            self.assertEqual(len(data["items"]), 10, name)
            for item in data["items"]:
                self.assertTrue(item["title"] and item["link"] and item["guid"], name)


class TestFetching(RadarCase):
    class FakeResp(object):
        def __init__(self, status, body):
            self.status = status
            self._body = body

        def read(self, n=None):
            return self._body if n is None else self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def run_through_default_fetcher(self, env):
        seen = []
        routes = self.net.routes

        def fake_urlopen(req, timeout=None):
            seen.append((req, timeout))
            status, body = routes.get(req.full_url, (404, b""))
            return TestFetching.FakeResp(status, body)

        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            if "GITHUB_TOKEN" not in env:
                os.environ.pop("GITHUB_TOKEN", None)
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                with contextlib.redirect_stdout(buf):
                    code = collect.main(["--data-dir", self.data, "--date", D1,
                                         "--now", D1 + "T05:17:00Z"],
                                        sleep=self.sleeps.append)
        return code, seen

    def test_11_token_goes_only_to_api_github_com_and_never_into_output(self):
        token = "tok-SECRET-0123456789"
        code, seen = self.run_through_default_fetcher({"GITHUB_TOKEN": token})
        self.assertEqual(code, 0)
        hosts = set()
        for req, timeout in seen:
            host = urllib.parse.urlsplit(req.full_url).hostname
            hosts.add(host)
            auth = req.get_header("Authorization")
            if host == "api.github.com":
                self.assertEqual(auth, "Bearer " + token, req.full_url)
                self.assertEqual(req.get_header("Accept"), "application/vnd.github+json")
            else:
                self.assertIsNone(auth, req.full_url)
            self.assertEqual(req.get_header("User-agent"),
                             "ho-radar/1 (+https://github.com/LembaGang/ho-radar)")
            self.assertEqual(timeout, 30)
            if auth:
                self.assertIn("Authorization", req.unredirected_hdrs)
                self.assertNotIn("Authorization", req.headers)
        self.assertIn("api.github.com", hosts)
        self.assertTrue(hosts - {"api.github.com"}, "no non-github host was fetched")
        self.assertIn("api.headlessoracle.com", hosts)
        self.assertIn("headlessoracle.com", hosts)
        for path in Path(self.data).rglob("*"):
            if path.is_file():
                self.assertNotIn(token.encode(), path.read_bytes(), str(path))

    def test_11b_without_a_token_no_authorization_header_is_sent(self):
        code, seen = self.run_through_default_fetcher({})
        self.assertEqual(code, 0)
        self.assertTrue(seen)
        for req, _ in seen:
            self.assertIsNone(req.get_header("Authorization"))

    def test_11c_http_error_statuses_are_returned_not_raised(self):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b"err"))
        with mock.patch("urllib.request.urlopen", boom):
            self.assertEqual(collect.fetch_default("https://api.github.com/x"), (500, b"err"))


class TestRepository(unittest.TestCase):
    def test_10_privacy_no_banned_keys_or_email_like_strings_in_outputs_or_fixtures(self):
        tmp = tempfile.mkdtemp(prefix="ho-radar-privacy-")
        self.addCleanup(shutil.rmtree, tmp, True)
        data = os.path.join(tmp, "data")
        net = FakeNet()
        sleeps = []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            collect.main(["--data-dir", data, "--date", D1, "--now", D1 + "T05:17:00Z"],
                         fetch=net.fetch, sleep=sleeps.append)
            prepend_commit(net, "agt-commits", fixture_commit("f" * 40, "signed receipts"))
            net.set_raw("cf-blog", rss_add_item(net.routes[URL["cf-blog"]][1],
                                                "Attestation", "https://blog.cloudflare.com/a/", "G1"))
            collect.main(["--data-dir", data, "--date", D2, "--now", D2 + "T05:17:00Z"],
                         fetch=net.fetch, sleep=sleeps.append)
        outputs = [p for p in Path(data).rglob("*") if p.is_file()]
        self.assertEqual(len(outputs), 7)

        def walk(obj, where, in_url_key=False):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    self.assertNotIn(k, BANNED_KEYS, "%s: banned key %r" % (where, k))
                    walk(v, where, k in URL_KEYS)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v, where, in_url_key)
            elif isinstance(obj, str) and not in_url_key:
                self.assertIsNone(EMAIL_LIKE.search(obj), "%s: email-like string %r" % (where, obj))

        for path in outputs:
            raw = path.read_bytes().decode("ascii")
            if path.suffix == ".json":
                walk(json.loads(raw), str(path))
            else:
                self.assertIsNone(EMAIL_LIKE.search(raw), str(path))
                self.assertNotIn("dc:creator", raw)

        fixtures = [p for p in FIX.iterdir() if p.suffix in (".json", ".xml")]
        self.assertGreaterEqual(len(fixtures), 14)
        for path in fixtures:
            text = path.read_text(encoding="ascii")
            self.assertNotIn("dc:creator", text, str(path))
            self.assertNotIn("<author", text, str(path))
            if path.suffix == ".json":
                walk(json.loads(text), str(path))
            else:
                for el in ET.fromstring(text.encode("ascii")).iter():
                    local = el.tag.rsplit("}", 1)[-1]
                    self.assertNotIn(local, BANNED_KEYS, str(path))

    def test_12_workflow_file_has_the_required_shape(self):
        text = (ROOT / ".github" / "workflows" / "radar.yml").read_text(encoding="ascii")
        self.assertIn("'37 9 * * *'", text)
        self.assertNotIn("17 5 * * *", text)
        self.assertIn("09:01Z", text)
        self.assertIn("          ref: main\n          fetch-depth: 1", text)
        self.assertIn("git pull --rebase -q origin main", text)
        self.assertLess(text.index('git commit -m "radar $d"'), text.index("git pull --rebase"))
        self.assertLess(text.index("git pull --rebase"), text.index("git push"))
        self.assertIn("workflow_dispatch", text)
        self.assertIn("contents: write", text)
        self.assertIn("unittest discover", text)
        self.assertIn("group: radar", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("ubuntu-24.04", text)
        self.assertIn("timeout-minutes: 15", text)
        self.assertIn("fetch-depth: 1", text)
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", text)
        self.assertNotIn("pull_request_target", text)
        uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.MULTILINE)
        self.assertEqual(len(uses), 1, uses)
        self.assertTrue(uses[0].startswith("actions/checkout@"), uses)
        ref = uses[0].split("@", 1)[1]
        self.assertEqual(ref, "3d3c42e5aac5ba805825da76410c181273ba90b1")
        self.assertEqual(text.count("uses:"), 1)
        self.assertIn("# v7.0.1, tag commit read from the GitHub API 2026-10-22T13:12Z", text)
        self.assertEqual(len(re.findall(r"secrets\.", text)), 1)

    def test_13_every_file_is_ascii_lf_and_python_has_no_tabs(self):
        count = 0
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for fname in filenames:
                path = Path(dirpath, fname)
                raw = path.read_bytes()
                count += 1
                try:
                    raw.decode("ascii")
                except UnicodeDecodeError as exc:
                    self.fail("%s is not ASCII: %s" % (path, exc))
                self.assertNotIn(b"\r", raw, str(path))
                if path.suffix == ".py":
                    self.assertNotIn(b"\t", raw, str(path))
        self.assertGreater(count, 20)
        self.assertEqual((ROOT / ".gitattributes").read_bytes(), b"* text eol=lf\n")

    def test_layout_files_exist(self):
        for rel in ("README.md", "radar/__init__.py", "radar/sources.py", "radar/collect.py",
                    "tests/__init__.py", "tests/test_collect.py", "tests/fixtures/README.md",
                    ".github/workflows/radar.yml", "data/.gitkeep"):
            self.assertTrue((ROOT / rel).is_file(), rel)
        self.assertEqual((ROOT / "radar" / "__init__.py").read_bytes(), b"")
        self.assertEqual((ROOT / "tests" / "__init__.py").read_bytes(), b"")

    def test_sources_match_the_spec_table(self):
        ids = [s["id"] for s in sources.SOURCES]
        self.assertEqual(len(ids), 13)
        self.assertEqual(len(set(ids)), 13)
        self.assertEqual(sources.MONITORS, ("kill-trigger", "x402-issues", "mcp-registry", "record"))
        by = dict((s["id"], s) for s in sources.SOURCES)
        self.assertEqual(by["ho-record"]["url"], "https://api.headlessoracle.com/record")
        self.assertEqual(by["cf-changelog"]["url"],
                         "https://developers.cloudflare.com/changelog/rss/index.xml")
        self.assertEqual(sorted(set(s["monitor"] for s in sources.SOURCES)),
                         sorted(sources.MONITORS))
        self.assertIsNone(sources.KEYWORDS["x402-issues"])


FROM_OWNER = re.compile(r"\b(?:from|into) [A-Za-z0-9-]+/")
AT_HANDLE = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]*")


class TestR1Fixes(RadarCase):
    """Tests added after the first adversarial read (fixes F1 to F7)."""

    # F1 ------------------------------------------------------------------
    def test_14_no_person_text_in_any_output_value_or_fixture_text(self):
        net = FakeNet()
        data = os.path.join(self.tmp, "data-f1")
        self.run_day(D1, net=net, data=data)
        prepend_commit(net, "agt-commits", fixture_commit(
            "9" * 40, "Merge pull request #9 from someone/signed-receipts @someone"))
        net.set_raw("cf-blog", rss_add_item(net.routes[URL["cf-blog"]][1],
                                            "Attestation by @someone", "https://blog.cloudflare.com/x/", "G9"))
        self.run_day(D2, net=net, data=data)
        files = [p for p in Path(data).rglob("*") if p.is_file()]
        self.assertEqual(len(files), 7)
        for path in files:
            text = path.read_text(encoding="ascii")
            self.assertIsNone(FROM_OWNER.search(text), str(path))
            self.assertIsNone(AT_HANDLE.search(text), str(path))
            self.assertNotIn("someone", text.replace("@[handle]", ""), str(path))
            if path.suffix == ".json":
                stack = [json.loads(text)]
                while stack:
                    obj = stack.pop()
                    if isinstance(obj, dict):
                        stack.extend(obj.values())
                    elif isinstance(obj, list):
                        stack.extend(obj)
                    elif isinstance(obj, str):
                        self.assertIsNone(FROM_OWNER.search(obj), obj)
                        self.assertIsNone(AT_HANDLE.search(obj), obj)
        fixture_files = [p for p in FIX.iterdir() if p.is_file()]
        self.assertGreaterEqual(len(fixture_files), 15)
        for path in fixture_files:
            text = path.read_text(encoding="ascii")
            self.assertIsNone(FROM_OWNER.search(text), "from owner/ text in " + str(path))
            self.assertIsNone(AT_HANDLE.search(text), "unreplaced handle in " + str(path))

    # F3 ------------------------------------------------------------------
    def test_15_source_without_earlier_observation_makes_monitor_baseline(self):
        self.net.set_status("agt-commits", 503)
        code, s1 = self.run_day(D1)
        self.assertEqual(s1["monitors"]["kill-trigger"]["verdict"], "ERROR")
        self.net.routes[URL["agt-commits"]] = (200, read_fixture("agt_commits.json"))
        code, s2 = self.run_day(D2)
        self.assertEqual(code, 0)
        self.assertEqual(self.verdicts(s2), {
            "kill-trigger": "BASELINE", "x402-issues": "QUIET",
            "mcp-registry": "QUIET", "record": "OK"})
        self.assertTrue(self.source(s2, "kill-trigger", "agt-commits")["baseline"])
        code, s3 = self.run_day(D3)
        self.assertEqual(s3["monitors"]["kill-trigger"]["verdict"], "QUIET")

    def test_15b_error_still_outranks_partial_baseline(self):
        self.net.set_status("agt-commits", 503)
        self.run_day(D1)
        self.net.set_status("cf-blog", 500)
        code, s2 = self.run_day(D2)
        self.assertEqual(s2["monitors"]["kill-trigger"]["verdict"], "ERROR")
        self.assertEqual(code, 1)

    def test_15c_movement_in_one_source_outranks_partial_baseline(self):
        self.net.set_status("agt-commits", 503)
        self.run_day(D1)
        self.net.routes[URL["agt-commits"]] = (200, read_fixture("agt_commits.json"))
        body = self.net.routes[URL["cf-blog"]][1]
        self.net.set_raw("cf-blog", rss_add_item(
            body, "Attestation for every request", "https://blog.cloudflare.com/new-post/",
            "01NEWGUID"))
        code, s2 = self.run_day(D2)
        self.assertEqual(code, 0)
        self.assertEqual(s2["monitors"]["kill-trigger"]["verdict"], "MOVED")
        self.assertTrue(self.source(s2, "kill-trigger", "agt-commits")["baseline"])
        self.assertTrue(self.source(s2, "kill-trigger", "cf-blog")["moved"])

    # F4 ------------------------------------------------------------------
    def test_16_empty_list_where_previous_had_items_is_error(self):
        self.run_day(D1)
        self.net.set_json("agt-commits", [])
        code, s2 = self.run_day(D2)
        res = self.source(s2, "kill-trigger", "agt-commits")
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error"], "empty where previous had 30")
        self.assertEqual(s2["monitors"]["kill-trigger"]["verdict"], "ERROR")
        self.assertEqual(code, 1)
        # still empty the next day: still an error against the last good data
        code, s3 = self.run_day(D3)
        self.assertEqual(self.source(s3, "kill-trigger", "agt-commits")["error"],
                         "empty where previous had 30")
        # a normal payload returns: compared with the last good observation
        self.net.routes[URL["agt-commits"]] = (200, read_fixture("agt_commits.json"))
        code, s4 = self.run_day("2026-10-25")
        self.assertEqual(s4["monitors"]["kill-trigger"]["verdict"], "QUIET")
        self.assertEqual(code, 0)

    def test_16b_empty_feed_where_previous_had_items_is_error(self):
        self.run_day(D1)
        self.net.set_raw("cf-blog", b"<rss><channel><title>x</title></channel></rss>")
        code, s2 = self.run_day(D2)
        res = self.source(s2, "kill-trigger", "cf-blog")
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error"], "empty where previous had 10")
        self.assertEqual(code, 1)

    def test_16c_empty_list_on_first_observation_is_not_an_error(self):
        self.net.set_json("agt-releases", [])
        code, s1 = self.run_day(D1)
        self.assertEqual(self.source(s1, "kill-trigger", "agt-releases")["status"], "OK")
        self.assertEqual(code, 0)

    def test_16d_feed_item_without_guid_and_link_is_a_parse_error(self):
        xml = b"<rss><channel><item><title>Signed receipts everywhere</title></item></channel></rss>"
        with self.assertRaises(ValueError):
            collect.norm_rss({}, xml, lambda u: None, None)
        self.run_day(D1)
        self.net.set_raw("cf-blog", xml)
        code, s2 = self.run_day(D2)
        res = self.source(s2, "kill-trigger", "cf-blog")
        self.assertEqual(res["status"], "ERROR")
        self.assertIn("neither guid nor link", res["error"])
        self.assertEqual(code, 1)

    # F5 ------------------------------------------------------------------
    def test_17_record_checks_against_the_previous_observation(self):
        _, s1 = self.run_day(D1)
        self.assertEqual(sorted(s1["monitors"]["record"]["checks"]), sorted([
            "newest_is_expected", "not_late", "no_failed_steps", "no_ots_failed",
            "newest_is_yesterday", "newest_url_ok"]))
        _, s2 = self.run_day(D2)
        checks = s2["monitors"]["record"]["checks"]
        self.assertEqual(sorted(checks), sorted([
            "newest_is_expected", "not_late", "no_failed_steps", "no_ots_failed",
            "newest_is_yesterday", "newest_url_ok", "sequence_not_regressed",
            "advanced_since_previous"]))
        self.assertTrue(all(checks.values()))
        self.assertNotIn("record_not_rewritten", checks)
        self.assertNotIn("expected_is_recent", checks)

    def test_17b_regressed_chain_sequence_is_an_incident(self):
        self.run_day(D1)
        self.net.follow_date(D2)
        rec = self.net.json_of("ho-record")
        rec["chain_head"]["sequence"] = 5
        self.net.set_json("ho-record", rec)
        code, s2 = self.run_day(D2, follow=False)
        checks = s2["monitors"]["record"]["checks"]
        self.assertEqual(s2["monitors"]["record"]["verdict"], "INCIDENT")
        self.assertEqual([k for k, v in checks.items() if not v], ["sequence_not_regressed"])
        self.assertEqual(code, 2)

    def test_17c_rewritten_record_is_an_incident(self):
        self.run_day(D1)
        rec = self.net.json_of("ho-record")
        rec["newest"]["record_sha256"] = "0" * 64
        self.net.set_json("ho-record", rec)
        code, s2 = self.run_day(D2, follow=False)
        checks = s2["monitors"]["record"]["checks"]
        self.assertEqual(s2["monitors"]["record"]["verdict"], "INCIDENT")
        self.assertFalse(checks["record_not_rewritten"])
        self.assertEqual(code, 2)

    def test_17d_frozen_record_did_not_advance(self):
        self.run_day(D1)
        code, s2 = self.run_day(D2, follow=False)
        checks = s2["monitors"]["record"]["checks"]
        self.assertEqual(s2["monitors"]["record"]["verdict"], "INCIDENT")
        self.assertTrue(checks["record_not_rewritten"])
        self.assertFalse(checks["advanced_since_previous"])
        self.assertFalse(checks["newest_is_yesterday"])
        self.assertEqual(code, 2)

    def test_17e_no_previous_good_record_means_no_comparison_checks(self):
        self.net.set_status("ho-record", 500)
        self.run_day(D1)
        _, s2 = self.run_day(D2)
        self.assertEqual(s2["monitors"]["record"]["verdict"], "OK")
        self.assertEqual(len(s2["monitors"]["record"]["checks"]), 6)

    def test_17f_checks_that_do_not_apply_are_absent(self):
        data = {"newest_date": "2026-10-21", "expected_newest_date": "2026-10-21", "late": False,
                "failed_steps_count": 0, "ots_failed": 0, "newest_url_status": 200,
                "chain_head_sequence": 13, "newest_record_sha256": "a"}
        prev = dict(data)
        same_day = collect.record_checks(data, "2026-10-22", prev, "2026-10-22")
        self.assertNotIn("advanced_since_previous", same_day)
        self.assertTrue(same_day["record_not_rewritten"])
        older = dict(prev, newest_date="2026-10-20")
        later = collect.record_checks(data, "2026-10-22", older, "2026-10-21")
        self.assertNotIn("record_not_rewritten", later)
        self.assertTrue(later["advanced_since_previous"])

    # F6 ------------------------------------------------------------------
    def test_18_total_deadline_stops_new_fetches_and_marks_the_rest_error(self):
        class Clock(object):
            t = 0.0

            def __call__(self):
                return self.t

        clock = Clock()
        net = self.net

        class Slow(object):
            def fetch(inner, url):
                clock.t += 100.0
                return net.fetch(url)

        code, s = self.run_day(D1, net=Slow(), follow=False, clock=clock)
        self.assertEqual(len(net.calls), 6)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(code, 1)
        ok_ids = ["agt-commits", "agt-releases", "argentum-core-commits",
                  "argentum-core-releases", "cf-blog", "cf-changelog"]
        for mon in s["monitors"].values():
            for src in mon["sources"]:
                if src["id"] in ok_ids:
                    self.assertEqual(src["status"], "OK", src["id"])
                else:
                    self.assertEqual(src["status"], "ERROR", src["id"])
                    self.assertEqual(src["error"], "deadline", src["id"])
        self.assertEqual(s["monitors"]["record"]["verdict"], "ERROR")
        self.assertEqual(s["monitors"]["kill-trigger"]["verdict"], "BASELINE")

    # F7 ------------------------------------------------------------------
    def test_19_pull_update_time_alone_is_not_a_hit_but_new_state_is(self):
        def pull(state, updated, merged_at=None):
            return {"number": 9002, "title": "Registry thing", "state": state,
                    "updated_at": updated, "merged_at": merged_at,
                    "html_url": "https://github.com/example/repo/pull/9002"}

        def put(item):
            pulls = [p for p in json.loads(read_fixture("mcp_pulls_updated.json").decode("ascii"))]
            self.net.set_json("mcp-pulls-updated", [item] + pulls[:-1])

        put(pull("open", "2026-10-22T01:00:00Z"))
        self.run_day(D1)
        put(pull("open", "2026-10-23T01:00:00Z"))
        _, s2 = self.run_day(D2)
        res = self.source(s2, "mcp-registry", "mcp-pulls-updated")
        self.assertFalse(res["moved"])
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["other_new_count"], 1)
        self.assertEqual(s2["monitors"]["mcp-registry"]["verdict"], "QUIET")
        put(pull("closed", "2026-10-24T01:00:00Z", "2026-10-24T01:00:00Z"))
        _, s3 = self.run_day(D3)
        res = self.source(s3, "mcp-registry", "mcp-pulls-updated")
        self.assertTrue(res["moved"])
        self.assertEqual([h["number"] for h in res["hits"]], [9002])
        self.assertEqual(s3["monitors"]["mcp-registry"]["verdict"], "MOVED")


if __name__ == "__main__":
    unittest.main()
