# Fixtures

Each file is a de-identified, trimmed copy of a payload captured on
2026-10-22 (see the capture time per file). The original captures are not in
this repository; their SHA-256 digests are recorded here so the copies can be
traced. The copies were produced by keeping only the fields the collector
reads, dropping every banned key (login, user, author, committer, email, name,
body, avatar_url, gravatar_id, creator) and keeping the first 10 items of each
RSS feed with only title, link, guid and pubDate. Non-ASCII characters are
stored as escapes. Departures from a plain copy:

- Commit payloads keep sha, html_url, the first line of the commit message and
  the committer date, which sits at commit.date because the committer key is
  banned. The collector reads commit.committer.date first and falls back to
  commit.date. In the message line any ' from <owner>/<branch>' suffix of a
  merge line is removed and every at-sign handle is replaced by @[handle]; the
  collector reads the message in memory and stores only matched keywords.
- In every title in every fixture each at-sign handle is replaced by @[handle].
- Label objects (none were present in the captured issues) would be stored as
  plain strings, because the name key is banned.
- Release titles (the name field) are absent because name is a banned key, so
  releases normalise to a null title in these fixtures.
- The captured payloads for the comments of issues 2291 and 2357 were not
  captured; the tests build those two responses in code.

| fixture | source URL | captured (UTC) | sha256 of the original capture |
|---|---|---|---|
| agt_commits.json | https://api.github.com/repos/microsoft/agent-governance-toolkit/commits?per_page=30 | 2026-10-22T13:01:27Z | 9c01fc30d0a5ba4705cdab31e7364861c3718281aa48054f67d2c099b6b74453 |
| agt_releases.json | https://api.github.com/repos/microsoft/agent-governance-toolkit/releases?per_page=5 | 2026-10-22T13:01:26Z | 92d6ea8564c8d7d9624b568c8c56500476d20a6d0c73b506591da193cf9a764e |
| argentum_commits.json | https://api.github.com/repos/giskard09/argentum-core/commits?per_page=30 | 2026-10-22T13:01:21Z | 0b0ce4c24327e6966fe80352a919c10232abaf0be73ba39e15992e6a6a91d51a |
| argentum_releases.json | https://api.github.com/repos/giskard09/argentum-core/releases?per_page=5 | 2026-10-22T13:01:22Z | 5a38c15e941c56e21c65bd64978aee2251d3aaf2fb9c5144636c12574d361d1d |
| cf_blog_rss.xml | https://blog.cloudflare.com/rss/ | 2026-10-22T13:01:29Z | 0d6ddff46daca51b9d9244cf3106a6c6e0911b3f6d3c7e0f5db4298feea6f3c4 |
| cf_changelog_rss.xml | https://developers.cloudflare.com/changelog/rss/index.xml | 2026-10-22T13:01:34Z | a9c5910845da9c6076fe1922b856ed3afcc4fd277f7d6a512648f677607ce262 |
| ho_record.json | https://api.headlessoracle.com/record | 2026-10-22T13:01:36Z | 7dbccdfec567209fb18215ab1276231938486b1cecb6b9a034582d83e21be2ee |
| mcp_commits.json | https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/commits?per_page=30 | 2026-10-22T13:01:24Z | 6ac66403cae8d73502ca3625fe3dddfa0aab3f0ccf943cb8dfbff83f814e36ab |
| mcp_pull_2133.json | https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/pulls/2133 | 2026-10-22T13:01:23Z | 2cccfb718b7880e38fa5c4be8ccd7f255b38b435539d868baa987f054d3862a9 |
| mcp_pulls_updated.json | https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/pulls?state=all&sort=updated&direction=desc&per_page=30 | 2026-10-22T13:01:25Z | b369e8ad349e23ceae94fd5f6b16977ac614d6edda0b4facb4459715e8f492db |
| x402_issue_2291.json | https://api.github.com/repos/x402-foundation/x402/issues/2291 | 2026-10-22T13:01:16Z | 16c1a008318adde6c1535d6d92ca736b2826ecd19d556fcb5fc524c6f08b866d |
| x402_issue_2332.json | https://api.github.com/repos/x402-foundation/x402/issues/2332 | 2026-10-22T13:01:17Z | fb5ff9dcb68a79ef03af8d536d0b3d0ea792c4e9770603d54332729cd0483eab |
| x402_issue_2332_lastcomment.json | https://api.github.com/repos/x402-foundation/x402/issues/2332/comments?per_page=1&page=236 | 2026-10-22T13:01:19Z | bd1d6a60a22c544664f472b9d86def3f0fc790f7e381170ba41788d630c84279 |
| x402_issue_2357.json | https://api.github.com/repos/x402-foundation/x402/issues/2357 | 2026-10-22T13:01:18Z | 89fa3642e522e12f88c1ab549297503f224022dc115ccfdd3b2402fa2da0bcbb |
