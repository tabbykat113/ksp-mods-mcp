# TODO

## Enrichment cache invalidation on CKAN data change

`github_cache` and `spacedock_cache` are not automatically cleared when a mod's
CKAN data changes (e.g. new release). To fix this, Pass 1 would need to diff
incoming `ckan_json` against the stored value and clear enrichment rows for
changed mods. This is the same mechanism sketched in ROADMAP.md's update
strategy. Until then, TTL expiry is the only invalidation path.

---

## Next session — discuss enrichment strategy

Idea 4 (reduce friction) is done. Before building anything else, we need to pick
an approach for enriching mod data beyond what CKAN-meta provides. The candidates
have different trade-offs and aren't all mutually exclusive.

### Option A: Lazy cached lookup (replaces Pass 2)

When `get_mod` is called, fetch SpaceDock/GitHub data on the fly and cache it in
the DB with a TTL. Most mod data is never read, so this avoids scraping ~4000 mods
upfront. Could optionally add a background pre-warm for power users.

Questions: TTL strategy? What to fetch (README, stars, last push, SpaceDock desc)?
How to handle mods with no external links?

### Option B: Batch scraping (original Pass 2)

Crawl all mods from SpaceDock/GitHub in a separate process. Resumable, paced.
The value proposition: more indexable data improves search quality (READMEs,
descriptions, tags we don't get from CKAN-meta alone).

However, raw scraped data without an LM to synthesize it is likely flawed —
sources are incomplete, inconsistent, or missing entirely for many mods. Batch
scraping may only make sense as a feeder for Pass 3 (LM synthesis), not as a
standalone enrichment step. Otherwise, lazy access is preferred.

Questions: Is full coverage actually needed? What's the incremental update story?
Does this only make sense paired with Pass 3?

### Option C: A2A "CKAN keeper" agent

A persistent local agent that queries APIs on demand, builds knowledge over time,
and is reachable via A2A. A prototype communication bridge exists in `E:\Projects\MCP\a2a-mcp-tabby-version`.
Most powerful but most complex.

Questions: Scope? Is this solving a real problem yet or building ahead of need?

### Also discuss

- **Pass 3 (LM synthesis + embeddings)** — depends on having richer data. Which
  enrichment approach feeds it best?
- **What external data is actually high-value?** GitHub README, stars, last commit,
  SpaceDock description, forum thread? Prioritize before building.
- **What can an agent already do without us scraping?** We provide GitHub repo links
  in mod metadata. An agent with a GitHub MCP could browse a mod's bug tracker,
  check recent commits, read the README — all live. Does that reduce what we need
  to pre-index, or complement it?
