"""The fixed list of sources ho-radar observes, as plain data."""

GH = "https://api.github.com/repos/"

MONITORS = ("kill-trigger", "x402-issues", "mcp-registry", "record")

# Keyword regexes (matched case-insensitively with re.search). None means that
# any change in the monitor's sources counts.
KEYWORDS = {
    "kill-trigger": (
        "receipt|attest|recomput|signed|signature|verifiab|provenance|"
        "tamper|audit log|transparency log"
    ),
    "mcp-registry": "extension|registry|SEP-?2133",
    "x402-issues": None,
    "record": None,
}

SOURCES = [
    {
        "id": "x402-issue-2291",
        "kind": "gh_issue",
        "monitor": "x402-issues",
        "url": GH + "x402-foundation/x402/issues/2291",
    },
    {
        "id": "x402-issue-2332",
        "kind": "gh_issue",
        "monitor": "x402-issues",
        "url": GH + "x402-foundation/x402/issues/2332",
    },
    {
        "id": "x402-issue-2357",
        "kind": "gh_issue",
        "monitor": "x402-issues",
        "url": GH + "x402-foundation/x402/issues/2357",
    },
    {
        "id": "argentum-core-commits",
        "kind": "gh_commits",
        "monitor": "x402-issues",
        "url": GH + "giskard09/argentum-core/commits?per_page=30",
    },
    {
        "id": "argentum-core-releases",
        "kind": "gh_releases",
        "monitor": "x402-issues",
        "url": GH + "giskard09/argentum-core/releases?per_page=5",
    },
    {
        "id": "mcp-pr-2133",
        "kind": "gh_pull",
        "monitor": "mcp-registry",
        "url": GH + "modelcontextprotocol/modelcontextprotocol/pulls/2133",
    },
    {
        "id": "mcp-commits",
        "kind": "gh_commits",
        "monitor": "mcp-registry",
        "url": GH + "modelcontextprotocol/modelcontextprotocol/commits?per_page=30",
    },
    {
        "id": "mcp-pulls-updated",
        "kind": "gh_pulls_list",
        "monitor": "mcp-registry",
        "url": (
            GH + "modelcontextprotocol/modelcontextprotocol/pulls"
            "?state=all&sort=updated&direction=desc&per_page=30"
        ),
    },
    {
        "id": "agt-releases",
        "kind": "gh_releases",
        "monitor": "kill-trigger",
        "url": GH + "microsoft/agent-governance-toolkit/releases?per_page=5",
    },
    {
        "id": "agt-commits",
        "kind": "gh_commits",
        "monitor": "kill-trigger",
        "url": GH + "microsoft/agent-governance-toolkit/commits?per_page=30",
    },
    {
        "id": "cf-blog",
        "kind": "rss",
        "monitor": "kill-trigger",
        "url": "https://blog.cloudflare.com/rss/",
    },
    {
        "id": "cf-changelog",
        "kind": "rss",
        "monitor": "kill-trigger",
        "url": "https://developers.cloudflare.com/changelog/rss/index.xml",
    },
    {
        "id": "ho-record",
        "kind": "ho_record",
        "monitor": "record",
        "url": "https://api.headlessoracle.com/record",
    },
]
