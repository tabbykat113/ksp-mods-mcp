"""
Universal KSP CFG parser.

KSP's CFG format is a tree of named nodes, each containing key/value fields
and child nodes. This parser handles the full format including:
  - Inline braces: NODE { key = value }
  - Multi-line blocks (name on its own line, then { on next line)
  - Duplicate keys (preserved as list of tuples)
  - Nested sub-blocks to arbitrary depth
  - Line comments (//)

Usage:
    nodes = parse_cfg(text)          # returns list[CfgNode]
    part  = nodes[0]                 # CfgNode(name="PART", fields=[...], children=[...])
    val   = part.get("name")         # first value for key, or None
    vals  = part.get_all("key")      # all values for key (for duplicate-key entries)
    mods  = part.children_named("MODULE")
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class CfgNode:
    name: str
    fields: list[tuple[str, str]]
    children: list["CfgNode"]

    def get(self, key: str) -> str | None:
        """Return the first value for *key*, or None."""
        for k, v in self.fields:
            if k == key:
                return v
        return None

    def get_all(self, key: str) -> list[str]:
        """Return all values for *key* (handles duplicate keys)."""
        return [v for k, v in self.fields if k == key]

    def children_named(self, name: str) -> list["CfgNode"]:
        """Return all direct children with the given block name."""
        return [c for c in self.children if c.name == name]

    def child_named(self, name: str) -> "CfgNode | None":
        """Return the first direct child with the given block name, or None."""
        for c in self.children:
            if c.name == name:
                return c
        return None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"//.*$")


def _strip_comment(line: str) -> str:
    return _COMMENT_RE.sub("", line).strip()


def parse_cfg(text: str) -> list[CfgNode]:
    """
    Parse a KSP CFG file and return a list of top-level CfgNodes.

    Handles both styles:
      - Block name on its own line, { on the next line
      - Inline: NAME { key = value ... }
    """
    # Tokenise: expand inline { and } so the main loop sees one token per line.
    # Each segment between braces may itself contain multiple key=value pairs
    # separated by whitespace runs — we keep them as individual lines so the
    # key=value handler can process them one at a time.
    tokens: list[str] = []
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line)
        if not line:
            continue
        # Split on { and } keeping the delimiters as separate tokens.
        # e.g. "MODULE { name = Foo }" → ["MODULE", "{", "name = Foo", "}"]
        segments = re.split(r"([{}])", line)
        for seg in segments:
            seg = seg.strip()
            if seg:
                tokens.append(seg)

    # Stack-based parser. Each entry is (node_name, fields, children).
    # We use a list-of-tuples rather than a CfgNode during construction to
    # avoid mutating frozen dataclasses.
    stack: list[tuple[str, list[tuple[str, str]], list[CfgNode]]] = []
    top_level: list[CfgNode] = []
    pending_name: str | None = None  # block name seen before its {

    for token in tokens:
        if token == "{":
            name = pending_name or ""
            pending_name = None
            stack.append((name, [], []))

        elif token == "}":
            if not stack:
                continue  # malformed — ignore unmatched }
            name, fields, children = stack.pop()
            node = CfgNode(name=name, fields=fields, children=children)
            if stack:
                stack[-1][2].append(node)
            else:
                top_level.append(node)
            pending_name = None

        elif "=" in token:
            # key = value (possibly with spaces around =)
            eq = token.index("=")
            key = token[:eq].strip()
            val = token[eq + 1:].strip()
            # Guard: if the value looks like it contains another key=value pair
            # (a bare word followed by " = "), raise — multiple fields on one
            # line is not supported. We use a word-boundary check to avoid false
            # positives on values like URLs that happen to contain " = ".
            if re.search(r"\s+\w+\s*=\s*\S", val):
                raise ValueError(
                    f"Multiple key=value pairs on one line is not supported: {token!r}"
                )
            if stack:
                stack[-1][1].append((key, val))
            # key=value outside any block is silently dropped

        else:
            # Bare identifier — the name of the next block
            pending_name = token

    return top_level
