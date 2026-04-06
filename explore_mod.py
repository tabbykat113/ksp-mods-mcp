"""
Exploration script: pull everything we can from CKAN, GitHub, and SpaceDock
for a single mod and pretty-print it.

Usage:
    uv run explore_mod.py                        # uses default mod (MechJeb2)
    uv run explore_mod.py <ckan-identifier>      # e.g. KerbalEngineer
"""

import sys
import re
import base64
import os

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.pretty import Pretty
from rich.rule import Rule
from rich.text import Text

console = Console()

CKAN_META_REPO     = "https://raw.githubusercontent.com/KSP-CKAN/CKAN-meta/master"
CKAN_API_CONTENTS  = "https://api.github.com/repos/KSP-CKAN/CKAN-meta/contents"

# GitHub token is optional but raises the rate limit from 60 to 5000 req/hr.
# Set GITHUB_TOKEN in your environment to use it.
GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}
if token := os.environ.get("GITHUB_TOKEN"):
    GITHUB_HEADERS["Authorization"] = f"Bearer {token}"


# ---------------------------------------------------------------------------
# CKAN helpers
# ---------------------------------------------------------------------------

def find_latest_ckan_url(client: httpx.Client, identifier: str) -> tuple[str | None, str | None]:
    """
    List the mod's directory via the GitHub Contents API and return
    (canonical_identifier, raw_url) for the lexicographically latest .ckan file.
    """
    # Try exact case first, then scan for case-insensitive match.
    candidates = [identifier]
    url = f"{CKAN_API_CONTENTS}/{identifier}"
    console.log(f"Listing CKAN-meta directory: {url}")
    r = client.get(url, headers=GITHUB_HEADERS)
    if r.status_code == 404:
        # Case-insensitive search: list repo root and find matching dir name.
        console.log("Exact directory not found, scanning repo root for case-insensitive match...")
        root = client.get(CKAN_API_CONTENTS, headers=GITHUB_HEADERS)
        root.raise_for_status()
        lower = identifier.lower()
        match = next((e["name"] for e in root.json() if e["name"].lower() == lower), None)
        if not match:
            return None, None
        identifier = match
        r = client.get(f"{CKAN_API_CONTENTS}/{identifier}", headers=GITHUB_HEADERS)
    r.raise_for_status()

    ckan_files = [e["name"] for e in r.json() if e["name"].endswith(".ckan")]
    if not ckan_files:
        return None, None

    latest = sorted(ckan_files)[-1]  # lexicographic sort works for semver filenames
    raw_url = f"{CKAN_META_REPO}/{identifier}/{latest}"
    return identifier, raw_url


def fetch_ckan_metadata(client: httpx.Client, identifier: str) -> dict | None:
    identifier, url = find_latest_ckan_url(client, identifier)
    if not url:
        console.print(f"[red]Mod directory '{identifier}' not found in CKAN-meta.[/red]")
        return None
    console.log(f"Fetching .ckan file: {url}")
    r = client.get(url)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def parse_github_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL."""
    m = re.search(r"github\.com[/:]([^/]+)/([^/\s#?]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


def fetch_github_repo_meta(client: httpx.Client, owner: str, repo: str) -> dict | None:
    url = f"https://api.github.com/repos/{owner}/{repo}"
    console.log(f"Fetching GitHub repo meta: {url}")
    r = client.get(url, headers=GITHUB_HEADERS)
    if r.status_code == 404:
        console.print("[yellow]GitHub repo not found (404).[/yellow]")
        return None
    r.raise_for_status()
    return r.json()


def fetch_github_readme(client: httpx.Client, owner: str, repo: str) -> str | None:
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    console.log(f"Fetching GitHub README: {url}")
    r = client.get(url, headers=GITHUB_HEADERS)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    content = data.get("content", "")
    if data.get("encoding") == "base64":
        return base64.b64decode(content).decode("utf-8", errors="replace")
    return content


def fetch_github_topics(client: httpx.Client, owner: str, repo: str) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/topics"
    headers = {**GITHUB_HEADERS, "Accept": "application/vnd.github.mercy-preview+json"}
    r = client.get(url, headers=headers)
    if r.status_code != 200:
        return []
    return r.json().get("names", [])


def fetch_github_releases(client: httpx.Client, owner: str, repo: str, limit: int = 3) -> list[dict]:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases"
    r = client.get(url, headers=GITHUB_HEADERS, params={"per_page": limit})
    if r.status_code != 200:
        return []
    return r.json()


# ---------------------------------------------------------------------------
# SpaceDock helpers
# ---------------------------------------------------------------------------

def extract_spacedock_id(url: str) -> int | None:
    m = re.search(r"spacedock\.info/mod/(\d+)", url)
    return int(m.group(1)) if m else None


def fetch_spacedock_meta(client: httpx.Client, mod_id: int) -> dict | None:
    url = f"https://spacedock.info/api/mod/{mod_id}"
    console.log(f"Fetching SpaceDock meta: {url}")
    r = client.get(url)
    if r.status_code == 404:
        console.print("[yellow]SpaceDock mod not found (404).[/yellow]")
        return None
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def section(title: str):
    console.print()
    console.print(Rule(f"[bold cyan]{title}[/bold cyan]"))


def show_fields(data: dict, fields: list):
    for field in fields:
        label = field if isinstance(field, str) else field[0]
        key   = field if isinstance(field, str) else field[1]
        val   = data.get(key)
        if val is None:
            continue
        if isinstance(val, (dict, list)):
            console.print(f"  [bold]{label}:[/bold]")
            console.print(Pretty(val, indent_guides=True))
        else:
            console.print(f"  [bold]{label}:[/bold] {val}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def explore(identifier: str):
    console.print(Panel(f"[bold]Exploring mod:[/bold] [green]{identifier}[/green]", expand=False))

    with httpx.Client(timeout=30, follow_redirects=True) as client:

        # --- CKAN ---
        section("CKAN Metadata")
        ckan = fetch_ckan_metadata(client, identifier)
        if not ckan:
            return

        show_fields(ckan, [
            "identifier", "name", "abstract", "description",
            "author", "license",
            "tags", "version",
            "ksp_version_min", "ksp_version_max", "ksp_version",
            "depends", "recommends", "suggests", "conflicts",
            "resources", "download", "download_size", "download_hash",
        ])

        resources = ckan.get("resources", {})

        # --- GitHub ---
        gh_url  = resources.get("repository") or resources.get("homepage")
        gh_pair = parse_github_url(gh_url) if gh_url else None

        if gh_pair:
            owner, repo = gh_pair

            section(f"GitHub Repo Meta  ({owner}/{repo})")
            meta = fetch_github_repo_meta(client, owner, repo)
            if meta:
                show_fields(meta, [
                    "full_name", "description", "homepage",
                    "stargazers_count", "forks_count", "watchers_count",
                    "open_issues_count", "language",
                    "created_at", "updated_at", "pushed_at",
                    "license", "topics", "default_branch",
                ])

            section("GitHub Topics")
            console.print(f"  {fetch_github_topics(client, owner, repo)}")

            section("GitHub Releases (latest 3)")
            for rel in fetch_github_releases(client, owner, repo):
                console.print(f"  [bold]{rel.get('tag_name')}[/bold]  {rel.get('published_at')}")
                body = (rel.get("body") or "")[:400]
                if body:
                    console.print(Text(body, style="dim"))

            section("GitHub README (first 2000 chars)")
            readme = fetch_github_readme(client, owner, repo)
            if readme:
                console.print(Text(readme[:2000] + ("..." if len(readme) > 2000 else ""), style="dim"))
            else:
                console.print("[yellow]No README found.[/yellow]")
        else:
            console.print("\n[yellow]No GitHub URL found in CKAN resources.[/yellow]")

        # --- SpaceDock ---
        sd_url = resources.get("spacedock")
        sd_id  = extract_spacedock_id(sd_url) if sd_url else None

        if sd_id:
            section(f"SpaceDock Meta  (id={sd_id})")
            sd = fetch_spacedock_meta(client, sd_id)
            if sd:
                show_fields(sd, [
                    "name", "author", "license",
                    "short_description",
                    "downloads", "followers", "rating",
                    "created", "updated", "background", "versions",
                ])
                desc = (sd.get("description") or "")
                if desc:
                    section("SpaceDock Description (first 2000 chars)")
                    console.print(Text(desc[:2000] + ("..." if len(desc) > 2000 else ""), style="dim"))
        else:
            console.print("\n[yellow]No SpaceDock URL found in CKAN resources.[/yellow]")

        section("Raw CKAN (full)")
        console.print(Pretty(ckan, indent_guides=True))


if __name__ == "__main__":
    identifier = sys.argv[1] if len(sys.argv) > 1 else "MechJeb2"
    explore(identifier)
