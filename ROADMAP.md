# Roadmap

## Vision

An agent-queryable mod index for Kerbal Space Program, designed to be useful at every level of data richness — from a bare CKAN harvest through to a fully LM-enriched semantic database.

The core principle: **each pass adds value independently**. A fresh install with only Pass 1 complete is already useful. Passes 2 and 3 enrich the data progressively, and the MCP server degrades gracefully based on what's available.

All multi-mod crawl operations (enrichment, synthesis) are designed to be **resumable** — an interrupted run picks up where it left off rather than starting over, using per-row completion timestamps as checkpoints.

---

## Pass 1 — CKAN Harvest ✅

Download the CKAN-meta repository as a single archive and index all mod metadata into a local SQLite database. Cheap, fast (~seconds), and sufficient for name/tag/version/popularity search.

- Restartable via ETag — re-running is a no-op if nothing changed upstream
- Stores full version history per mod for accurate KSP compatibility filtering

**MCP capabilities at this tier:** name search, tag filtering, KSP version filtering, popularity sorting, full CKAN metadata lookup.

---

## Pass 2 — External Enrichment

For each mod, lazily fetch additional data from external sources: GitHub repo metadata and README, SpaceDock descriptions and download statistics. Queries are paced to respect rate limits — the natural latency between mods keeps us well within API limits without explicit throttling.

- Per-mod completion flag — interrupted runs resume from the last unprocessed mod
- Graceful fallback — mods with no GitHub or SpaceDock link are skipped cleanly
- Adds: long-form descriptions, README prose, accurate download counts, repo activity signals (stars, last push date)

**MCP capabilities added:** richer detail views, sorting/filtering by repo freshness.

---

## Pass 3 — LM Synthesis

Feed each mod's harvested data into a local language model (via LM Studio's OpenAI-compatible API) to produce a structured summary: a natural-language description written for discoverability, plus extracted tags and categories beyond what CKAN provides.

Embed the summaries using a local embedding model and store the vectors in the database (via `sqlite-vec`).

The key insight: **vector search works best on AI-generated summaries of human text**, not raw documentation. This pass transforms noisy, install-heavy README content into clean, semantically consistent descriptions that embed well.

- Per-mod completion flag — resumable, can be run in the background over hours
- LM and embedding model are configurable; defaults to locally-hosted models via LM Studio
- Entirely optional — the index is fully functional without this pass

**MCP capabilities added:** semantic/vector search, AI-generated summaries, richer tag taxonomy.

---

## Update strategy

Keeping the index fresh is a multi-layer problem:

**Pass 1** is cheap enough to re-run on a schedule (e.g. daily). The ETag check makes it a no-op when nothing has changed, so there's no cost to running it frequently.

**Passes 2 and 3** are expensive and should only re-run for mods that have actually changed. The strategy:
- Pass 1 detects mod changes by comparing the new archive's `.ckan` content against stored `ckan_json` — a hash mismatch means the mod was updated
- Changed mods get their Pass 2/3 completion flags cleared, queuing them for re-enrichment
- Unchanged mods are skipped entirely, keeping incremental runs fast regardless of index size

**Deep data** (e.g. GitHub Issues for known bugs, open PR counts) follows the same pattern but with its own staleness window — issues change more frequently than READMEs, so they'd need a shorter refresh interval and their own completion timestamp. This data is entirely optional and only meaningful when a GitHub repository is known for the mod.

---

## Other planned work

- **Scheduled re-harvest** — detect when CKAN-meta has changed and re-run Pass 1 automatically
- **KSP 2 support** — the architecture supports it; needs source investigation
- **CLI polish** — progress for Pass 2/3, `--status` flag to summarise index completeness
