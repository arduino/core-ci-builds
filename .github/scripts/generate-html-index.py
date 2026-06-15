# Copyright (c) Arduino s.r.l. and/or its affiliated companies
# SPDX-License-Identifier: MPL-2.0

"""Generate pages/index.html listing all CI package indexes."""

import argparse
import collections
import datetime
import html
import json
import os
import re
import semver
import subprocess
import urllib.request

from packaging_legacy.version import parse as legacy_parse
from pathlib import Path

# Assumptions on directory structure:
OUTPUT_DIR = Path("pages") # contains JSON and srcmap files, index.html will be generated here
SNIPPETS_DIR = Path("snippets") # contains owner/repo/branch/ directories with snippets
SCRIPTS_DIR = Path("package_index/scripts") # contains last_modified.sh
GROUP_PATTERNS_FILE = Path(__file__).parent / "group-patterns.json"


def get_index_name(owner: str, repo: str, branch: str) -> str:
    """Construct index filename from owner/repo/branch."""
    short_repo = repo.removeprefix("ArduinoCore-")
    return f"{owner}_{short_repo}_{branch}_ci"


def load_timestamps() -> dict:
    """Run last_modified.sh and parse its output; return filepath → ISO8601 dict."""
    #try:
    ts: dict = {}
    output = subprocess.check_output([SCRIPTS_DIR / "last_modified.sh"], text=True)
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            date_str, filepath = parts
            file_parts = filepath.split("/")
            if len(file_parts) > 5 and file_parts[0] == "snippets":
                owner, repo, branch, version = file_parts[1:5]
                src_stem = '/'.join(file_parts[:5])
                dst_stem = f'{get_index_name(owner, repo, branch)}/{owner}'
                filepath = filepath.replace(src_stem, dst_stem)
                ts[filepath] = date_str
    return ts
    #except Exception:
    #    return {}


def load_srcmap(index_name: str) -> dict:
    """Load pages/<index_name>.srcmap if it exists, return {"platforms":{}, "tools":{}}."""
    srcmap_path = OUTPUT_DIR / (index_name + ".srcmap")
    if not srcmap_path.exists():
        return {}
    try:
        with open(srcmap_path) as f:
            return json.load(f)
    except Exception:
        return {}


def sort_by_version(l: list) -> list:
    if not l:
        return l
    elif isinstance(l[0], str):
        # list of version strings, no timestamp available
        try:
            return sorted(l, key=lambda x: semver.Version.parse(x), reverse=True)
        except:
            return sorted(l, key=lambda x: legacy_parse(x), reverse=True)
    else:
        # list of (version, ts) tuples: tiebreak equal versions by timestamp (newer first)
        try:
            return sorted(l, key=lambda x: (semver.Version.parse(x[0]), x[1]), reverse=True)
        except:
            return sorted(l, key=lambda x: (legacy_parse(x[0]), x[1]), reverse=True)


def parse_srcmap_items(raw_srcmap: dict, timestamps: dict = {}) -> tuple:
    """Parse a srcmap dict into (items, plat_versions, last_ts).

    items is a defaultdict mapping (kind, packager, name) → [(version, ts), ...]
    timestamps maps srcfile path → ISO8601 string; absent entries yield "".
    """
    items = collections.defaultdict(list)
    plat_versions = set()
    last_ts = ""
    for kind in "platforms", "tools":
        for what, srcfile in raw_srcmap.get(kind, {}).items():
            packager, name, version = what.split(":", 2)
            ts = timestamps.get(srcfile, "")
            items[(kind, packager, name)].append((version, ts))
            last_ts = max(last_ts, ts)
            if kind == "platforms":
                plat_versions.add(version)
    return items, plat_versions, last_ts


def load_releases(owner: str, repo: str, branch: str) -> set:
    """Load tagged-versions file for a branch; return a set of version_short strings."""
    tag_file = SNIPPETS_DIR / owner / repo / branch / "tagged-versions"
    if not tag_file.exists():
        return set()
    return set(tag_file.read_text().splitlines())


def collect_ci_entries() -> dict:
    last_timestamps = load_timestamps()
    data: dict = {}
    for branch_dir in sorted(SNIPPETS_DIR.glob("*/*/*/")):
        parts = branch_dir.relative_to(SNIPPETS_DIR).parts
        if len(parts) != 3:
            continue
        owner, repo, branch = parts
        index_name = f"{get_index_name(owner, repo, branch)}.json"
        raw_srcmap = load_srcmap(index_name)
        items, plat_versions, last_ts = parse_srcmap_items(raw_srcmap, last_timestamps)

        data[(owner, repo, branch)] = {
            "file": index_name,
            "plat_vers": sort_by_version(list(plat_versions)),
            "link": f"{index_name}.json",
            "mtime": last_ts,
            "items": items,
            "releases": load_releases(owner, repo, branch),
        }
    return data


def collect_prod_entry() -> dict:
    """Load prod.json.srcmap from OUTPUT_DIR and build an entry dict (no timestamps)."""
    raw_srcmap = load_srcmap("prod.json")
    if not raw_srcmap:
        return {}
    items, _, _ = parse_srcmap_items(raw_srcmap)
    return {
        "file": None,
        "plat_vers": [""], # empty column
        "link": None,
        "mtime": None,
        "items": items,
        "releases": set(),
    }


def fmt_ts(ts: str) -> str:
    """Emit a timestamp span; age is computed client-side by JS."""
    return f'<span class="ts" data-ts="{ts}">{ts}</span>' if ts else ""


def tag_pill(version: str, releases: set) -> str:
    """Return a tag badge if the version matches a tagged-versions entry."""
    if version in releases:
        return ' <span class="badge badge-tag">release</span>'
    return ""


def fmt_version_link(version: str, releases: set, owner: str = "", repo: str = "") -> str:
    """Wrap version in a GitHub tree link. +suffix → SHA, otherwise tag."""
    if not owner or not repo:
        return version
    ref = version_to_ref(version)
    url = f"https://github.com/{owner}/{repo}/tree/{ref}"
    return f'<a href="{url}">{version}</a>{tag_pill(version, releases)}'


def version_to_ref(version: str) -> str:
    """Extract the git ref from a version string. +suffix → SHA, else tag."""
    return version.split("+", 1)[1] if "+" in version else version


def more_pill(count: int) -> str:
    return f' <span class="badge badge-more">+{count - 1}</span>' if count > 1 else ""

def commit_snippet(entry: dict) -> str:
    """Return summary+stats HTML spans for a commit_data entry, or ''."""
    if not entry:
        return ""
    parts = []
    if entry.get("summary"):
        parts.append(f'<span class="commit-summary">{entry["summary"]}</span>')
    add, del_, files = entry.get("add"), entry.get("del"), entry.get("files")
    if add is not None or del_ is not None:
        add_s = f'+{add}' if add is not None else '?'
        del_s = f'-{del_}' if del_ is not None else '?'
        files_part = (f', <span class="stat-files">{files} file{"s" if files != 1 else ""}</span>'
                      if files is not None else '')
        parts.append(f'<span class="commit-stats-inline">'
                     f'<span class="stat-add">{add_s}</span> '
                     f'<span class="stat-del">{del_s}</span> lines{files_part}</span>')
    return "".join(parts)


def version_cell(versions: list, releases: set, owner: str, repo: str,
                 commit_data: dict) -> str:
    """Build full version cell: latest-link + more-pill + commit-snippet."""
    if not versions:
        return "unknown"
    latest = versions[0]
    if isinstance(latest, tuple):
        latest = latest[0]
    entry = (commit_data.get(f"{owner}/{repo}/{version_to_ref(latest)}", {})
             if owner and repo and latest else {})
    return (fmt_version_link(latest, releases, owner, repo)
            + more_pill(len(versions))
            + commit_snippet(entry))


def group_items(items: dict) -> list:
    """Group items by regex patterns from group-patterns.json.

    Patterns match against '{kind}/{packager}:{name}' strings (no overlaps assumed).
    Returns a list of ([(kind, packager, name), ...], {key: [(ver,ts),...]}) tuples.
    """

    try:
        with open(GROUP_PATTERNS_FILE) as f:
            patterns = [re.compile(f"^{p}$") for p in json.load(f)]
    except Exception:
        patterns = []

    # Map each item key → pattern index (or None if unmatched)
    key_to_group = {}
    groups = collections.defaultdict(list)
    for key in items:
        kind, packager, name = key
        fqn = f"{kind}/{packager}:{name}"
        group_id = key  # default: ungrouped
        for i, pat in enumerate(patterns):
            if pat.match(fqn):
                group_id = i
                break
        key_to_group[key] = group_id
        groups[group_id].append(key)

    # Build output preserving sort order (by first member in each group)
    result = []
    seen = set()
    for key in sorted(items.keys()):
        if key in seen:
            continue
        gid = key_to_group[key]
        sorted_members = sorted(groups[gid])
        member_versions = {m: sort_by_version(items[m]) for m in sorted_members}
        for m in sorted_members:
            seen.add(m)
        result.append((sorted_members, member_versions))
    return result


def build_inner_html(index, key = None, url_prefix = "", commit_data: dict = {}):
    """Render <details> blocks for a single index entry."""
    # Main item block
    releases = index['releases']
    if key: # all CI branch entries
        owner, repo, branch = key
        details = 'file-item'
        badge = '<span class="badge badge-branch">CI branch</span>'
        title = f'<a href="{url_prefix + index['file']}">{owner}/{repo}<br>&nbsp;@ {branch}</a>'
    else: # prod entry
        owner, repo = "", ""
        details = 'file-item official-item'
        badge = '<span class="badge badge-official">official</span>'
        title = 'package_index.json'

    plat_vers = index['plat_vers']
    out = f"""
        <details class="{details}">
            <summary class="grid-row summary-row">
                <span class="activity-cell">📜</span>
                <span>{title} {badge}</span>
                <span>{version_cell(plat_vers, releases, owner, repo, commit_data)}</span>
                <span>{fmt_ts(index['mtime'])}</span>
            </summary>"""

    # Sub-groups (merged by regex pattern match)
    for members, member_versions in group_items(index['items']):
        kind = members[0][0][:-1]  # drop plural 's'
        label = ", ".join(f"{p}:<b>{n}</b>" for _, p, n in members)
        badge = f'<span class="badge badge-{kind}">{kind}</span>'

        # Union of all versions across members, sorted
        all_versions_set = {}
        for m, vers in member_versions.items():
            for v, ts in vers:
                if v not in all_versions_set or ts > all_versions_set[v]:
                    all_versions_set[v] = ts
        all_versions = sort_by_version([(v, ts) for v, ts in all_versions_set.items()])

        out += f"""
            <details class="sub-group">
                <summary class="grid-row sub-summary-row">
                    <span></span>
                    <span><span class="tree-branch">↳</span> {label} {badge}</span>
                    <span>{version_cell(all_versions, releases, owner, repo, commit_data)}</span>
                    <span>{fmt_ts(all_versions[0][1])}</span>
                </summary>"""
        # Build per-member version lookup for quick membership check
        member_ver_sets = {m: {v for v, _ in vers} for m, vers in member_versions.items()}
        for v, ts in all_versions:
            present = [m for m in members if v in member_ver_sets[m]]
            names_str = ", ".join(f"{p}:{n}" for _, p, n in present)
            entry = commit_data.get(f"{owner}/{repo}/{version_to_ref(v)}", {}) if owner and repo else {}
            out += f"""
                <div class="grid-row detail-row">
                    <span></span>
                    <span>{names_str}</span>
                    <span>{fmt_version_link(v, releases, owner, repo)}{commit_snippet(entry)}</span>
                    <span>{fmt_ts(ts)}</span>
                </div>"""
        out += "</details>\n"

    out += "</details>\n"
    return out


def commit_summary(message: str, owner: str, repo: str) -> str:
    """Return a ready-to-embed HTML snippet for a commit message.

    Merge commits (first word == "Merge"):
      text = 3rd line (PR title), HTML-escaped, followed by a PR link
             built from the 4th word of line 1 (e.g. "#123").
    Regular commits:
      text = HTML-escaped 1st line.
    """
    lines = message.splitlines()
    first_line = lines[0].strip() if lines else ""
    words = first_line.split()
    if words and words[0] == "Merge" and len(words) > 3:
        pr_word = words[3]
        pr_number = pr_word.lstrip("#") if pr_word.startswith("#") else None
        text = lines[2].strip() if len(lines) > 2 else first_line
        esc = html.escape(text)
        if pr_number:
            pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
            return f'{esc} (<a href="{pr_url}" target="_blank">#{pr_number}</a>)'
        return esc
    return html.escape(first_line)


def build_table_html(data: dict, prod: dict, url_prefix: str, commit_data: dict = {}) -> str:
    """Render all table entries as file-item blocks."""
    html_output = ""
    for key, index in data.items():
        html_output += build_inner_html(index, key, url_prefix, commit_data)
    html_output += build_inner_html(prod, "", url_prefix, commit_data)
    return html_output


def collect_commit_refs(data: dict) -> set:
    """Return all (owner, repo, ref) triples referenced by any row in the index."""
    refs = set()
    for (owner, repo, branch), index in data.items():
        for versions in index['items'].values():
            for v, _ in versions:
                refs.add((owner, repo, version_to_ref(v)))
    return refs


_GRAPHQL_FRAGMENT = "fragment C on Commit { message additions deletions changedFilesIfAvailable }"
_GRAPHQL_URL = "https://api.github.com/graphql"
_GRAPHQL_PAGE = 50  # aliases per repository block


def query_commit_data(refs: set, token: str) -> dict:
    """Fetch commit details for all refs via batched GraphQL queries.

    Returns dict mapping "owner/repo/ref" → {msg, add, del, files}.
    Falls back gracefully on errors (returns empty dict entry).
    """
    if not token:
        print("Warning: GITHUB_TOKEN not set; skipping commit data pre-fetch.", flush=True)
        return {}

    # Group refs by (owner, repo)
    by_repo: dict = collections.defaultdict(list)
    for owner, repo, ref in refs:
        by_repo[(owner, repo)].append(ref)

    result: dict = {}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    for (owner, repo), repo_refs in by_repo.items():
        # Paginate in chunks of _GRAPHQL_PAGE
        for chunk_start in range(0, len(repo_refs), _GRAPHQL_PAGE):
            chunk = repo_refs[chunk_start:chunk_start + _GRAPHQL_PAGE]
            aliases = "\n    ".join(
                f's{i}: object(expression: "{ref}") {{ ...C }}'
                for i, ref in enumerate(chunk)
            )
            query = f"""
query {{
  repo: repository(owner: "{owner}", name: "{repo}") {{
    {aliases}
  }}
}}
{_GRAPHQL_FRAGMENT}
"""
            payload = json.dumps({"query": query}).encode()
            req = urllib.request.Request(_GRAPHQL_URL, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read())
            except Exception as e:
                print(f"Warning: GraphQL request failed for {owner}/{repo}: {e}", flush=True)
                continue

            if "errors" in body:
                print(f"Warning: GraphQL errors for {owner}/{repo}: {body['errors']}", flush=True)

            repo_data = (body.get("data") or {}).get("repo") or {}
            for i, ref in enumerate(chunk):
                node = repo_data.get(f"s{i}") or {}
                key = f"{owner}/{repo}/{ref}"
                msg = node.get("message", "")
                result[key] = {
                    "add":     node.get("additions"),
                    "del":     node.get("deletions"),
                    "files":   node.get("changedFilesIfAvailable"),
                    "summary": commit_summary(msg, owner, repo) if msg else "",
                }

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate index.html listing all CI package indexes."
    )
    parser.add_argument("--output-dir", help="Path to directory with generated files")
    parser.add_argument("--owner-repo", help="GitHub owner/repo for base URL (e.g. arduino/core-ci-builds)")
    parser.add_argument("--github-token", help="GitHub token for GraphQL pre-fetch (falls back to GITHUB_TOKEN env var)", default="")
    args = parser.parse_args()
    if args.output_dir:
        OUTPUT_DIR = Path(args.output_dir)
    if args.owner_repo:
        if "/" not in args.owner_repo:
            parser.error(f"Invalid owner/repo format: {args.owner_repo}")
        owner, repo = args.owner_repo.split("/", 1)
        url_prefix = f"https://{owner}.github.io/{repo}/"
    else:
        url_prefix = ""

    if not OUTPUT_DIR.exists():
        print(f"Error: Output directory '{OUTPUT_DIR}' does not exist.")
        exit(1)

    data = collect_ci_entries()
    prod = collect_prod_entry()
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    token = args.github_token or os.environ.get("GITHUB_TOKEN", "")
    refs = collect_commit_refs(data)
    commit_data = query_commit_data(refs, token)

    dynamic_content = build_table_html(data, prod, url_prefix, commit_data)

    template_path = Path(__file__).with_suffix(".template.html")
    with open(template_path, "r", encoding="utf-8") as template_file:
        template_content = template_file.read()

    final_html = (template_content
                  .replace("{{ FILE_LIST_CONTENT }}", dynamic_content)
                  .replace("{{ GENERATED_AT }}", generated_at))
    with open(OUTPUT_DIR / "index.html", "w", encoding="utf-8") as output_file:
        output_file.write(final_html)
