#!/usr/bin/env python3
"""Refresh public contribution charts with Python's standard library.

Run normally to collect public data, or with --self-test for offline checks.
The collector deliberately never reads credentials or sends authorization.
"""

import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
BANGKOK = ZoneInfo("Asia/Bangkok")
STATES = ("merged", "open", "closed")
PALETTES = {
    "light": {
        "bg": "#f5f2eb",
        "ink": "#202b32",
        "muted": "#59666c",
        "line": "#d8d9d3",
        "accent": "#b74725",
        "paper": "#fffcf5",
    },
    "dark": {
        "bg": "#152128",
        "ink": "#f3f0e7",
        "muted": "#b0bdc2",
        "line": "#3d4b51",
        "accent": "#ff986f",
        "paper": "#1b2d36",
    },
}


def get_json(endpoint, params=None):
    url = "https://api.github.com" + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "epichigh-public-profile",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("GitHub timestamps must include a timezone")
    return parsed


def collect(config, fetch=get_json):
    username = config["username"]
    if not re.fullmatch(r"[A-Za-z0-9-]+", username):
        raise ValueError("Invalid GitHub username")
    projects = []
    seen_repositories = set()
    for project in config["projects"]:
        repository = project["repository"]
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository name")
        if repository.split("/")[0].casefold() == username.casefold():
            raise ValueError(
                "Personal repositories are outside this profile's chart scope"
            )
        if repository.casefold() in seen_repositories:
            raise ValueError("Duplicate project")
        seen_repositories.add(repository.casefold())
        metadata = fetch(f"/repos/{repository}")
        if (
            metadata.get("private") is not False
            or metadata.get("visibility") != "public"
            or metadata.get("fork") is not False
            or metadata.get("full_name", "").casefold() != repository.casefold()
        ):
            raise ValueError(
                f"Cannot verify {repository} as a public upstream repository"
            )

        records = []
        seen_numbers = set()
        expected = None
        page = 1
        while True:
            result = fetch(
                "/search/issues",
                {
                    "q": f"repo:{repository} author:{username} is:pr is:public",
                    "sort": "created",
                    "order": "asc",
                    "per_page": 100,
                    "page": page,
                },
            )
            count = result["total_count"]
            # shortcut: search caps at 1,000 matches; partition by year if a project reaches it.
            if type(count) is not int or count < 0:
                raise ValueError("Invalid search total")
            if result.get("incomplete_results") is not False or count > 1000:
                raise ValueError(
                    f"Incomplete search for {repository}; keeping the published snapshot"
                )
            if expected is not None and count != expected:
                raise ValueError("Search changed during pagination; retry the refresh")
            expected = count
            for item in result["items"]:
                number = item["number"]
                url = f"https://github.com/{repository}/pull/{number}"
                if (
                    type(number) is not int
                    or number < 1
                    or number in seen_numbers
                    or item.get("user", {}).get("login", "").casefold()
                    != username.casefold()
                    or item["repository_url"]
                    != f"https://api.github.com/repos/{repository}"
                    or item["html_url"] != url
                ):
                    raise ValueError(
                        "Pull request identity or scope did not match the query"
                    )
                seen_numbers.add(number)
                merged_at = item["pull_request"]["merged_at"]
                instant(item["created_at"])
                if item["state"] not in ("open", "closed"):
                    raise ValueError("Unknown pull request state")
                if merged_at is not None:
                    if item["state"] != "closed" or instant(merged_at) < instant(
                        item["created_at"]
                    ):
                        raise ValueError("Invalid merge state or date")
                records.append(
                    {
                        "number": number,
                        "url": url,
                        "state": "merged" if merged_at else item["state"],
                        "created_at": item["created_at"],
                        "merged_at": merged_at,
                    }
                )
            if len(records) >= expected:
                break
            if len(result["items"]) != 100:
                raise ValueError(
                    "Search returned fewer pull requests than its reported total"
                )
            page += 1
        if len(records) != expected:
            raise ValueError("Search total does not match the collected pull requests")
        projects.append(
            {
                "repository": repository,
                "label": project["label"],
                "pull_requests": sorted(records, key=lambda record: record["number"]),
            }
        )
    return {
        "username": username,
        "as_of": datetime.now(BANGKOK).date().isoformat(),
        "projects": projects,
    }


def counts(projects):
    return Counter(
        record["state"] for project in projects for record in project["pull_requests"]
    )


def recent_merges(projects):
    merged = [
        {**record, "label": project["label"], "repository": project["repository"]}
        for project in projects
        for record in project["pull_requests"]
        if record["state"] == "merged"
    ]
    return sorted(
        merged, key=lambda record: instant(record["merged_at"]), reverse=True
    )[:5]


def render_svg(snapshot, theme, mobile):
    colors = PALETTES[theme]
    projects = snapshot["projects"]
    totals = counts(projects)
    merges = recent_merges(projects)
    width = 480 if mobile else 1000
    inset = 24 if mobile else 36
    chart_y = 322 if mobile else 312
    row_height = 100 if mobile else 96
    timeline_y = chart_y + len(projects) * row_height + 78 if mobile else chart_y
    height = (
        timeline_y + max(len(merges), 1) * 88 + 46
        if mobile
        else max(
            chart_y + len(projects) * row_height, timeline_y + max(len(merges), 1) * 76
        )
        + 54
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Public open-source contributions</title>',
        f'<desc id="desc">{totals["merged"]} merged and {totals["open"]} open pull requests in selected public upstream libraries. Status by project and the five most recent merges. Counts describe activity, not skill level.</desc>',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}</style>',
        f'<rect width="{width}" height="{height}" rx="16" fill="{colors["bg"]}"/>',
    ]

    def text(x, y, value, size=18, color="ink", weight=400, anchor="start", mono=False):
        parts.append(
            f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" fill="{colors[color]}" class="{"mono" if mono else ""}">{escape(str(value))}</text>'
        )

    def line(x1, y1, x2, y2):
        parts.append(
            f'<path d="M{x1} {y1}H{x2}" stroke="{colors["line"]}"/>'
            if y1 == y2
            else f'<path d="M{x1} {y1}L{x2} {y2}" stroke="{colors["line"]}"/>'
        )

    def segment(x, y, segment_width, state, bar_height=14):
        fill = {"merged": "accent", "open": "paper", "closed": "muted"}[state]
        stroke = "accent" if state == "merged" else "muted"
        parts.append(
            f'<rect x="{x:.2f}" y="{y}" width="{segment_width:.2f}" height="{bar_height}" fill="{colors[fill]}" stroke="{colors[stroke]}"/>'
        )

    text(inset, 42, "Open-source work", 28, weight=700)
    text(inset, 74, "Selected public upstream libraries", 20 if mobile else 18, "muted")
    date = datetime.fromisoformat(snapshot["as_of"]).strftime("%d %b %Y")
    text(
        inset if mobile else width - inset,
        100 if mobile else 42,
        f"AS OF {date.upper()}",
        16 if mobile else 14,
        "muted",
        anchor="start" if mobile else "end",
        mono=True,
    )

    stat_y = 130 if mobile else 106
    cell = (width - 2 * inset) / 3
    values = (
        totals["merged"],
        totals["open"],
        sum(bool(p["pull_requests"]) for p in projects),
    )
    for index, (value, label) in enumerate(
        zip(values, ("MERGED PRS", "OPEN PRS", "PROJECTS"))
    ):
        x = inset + index * cell
        if index:
            line(x - 12, stat_y, x - 12, stat_y + 72)
        text(x, stat_y + 35, value, 40, "accent" if index == 0 else "ink", 700)
        text(x, stat_y + 66, label, 21 if mobile else 16, "muted", mono=True)
    line(inset, stat_y + 92, width - inset, stat_y + 92)

    text(inset, 258 if mobile else 246, "Contribution status", 24, weight=650)
    legend_y = 282 if mobile else 271
    for x, state, label in zip(
        (inset, inset + 110, inset + 200),
        STATES,
        ("Merged", "Open", "Closed, not merged"),
    ):
        segment(x, legend_y - 11, 11, state, 11)
        text(x + 18, legend_y, label, 20 if mobile else 16, "muted")
    bar_width = 432 if mobile else 438
    maximum = max((len(p["pull_requests"]) for p in projects), default=0) or 1
    for index, project in enumerate(projects):
        y = chart_y + index * row_height
        stats = counts([project])
        text(inset, y, project["label"], 21 if mobile else 20, weight=600)
        line(inset, y + 24, inset + bar_width, y + 24)
        offset = inset
        for state in STATES:
            if stats[state]:
                length = bar_width * stats[state] / maximum
                segment(offset, y + 16, length, state, 16)
                offset += length
        summary = f"{stats['merged']} merged · {stats['open']} open"
        if stats["closed"]:
            summary += f" · {stats['closed']} closed"
        text(inset, y + 58, summary, 22 if mobile else 16, "muted")

    timeline_x = inset if mobile else 550
    text(
        timeline_x, timeline_y - 44 if mobile else 246, "Recent merges", 24, weight=650
    )
    if mobile:
        line(inset, timeline_y - 80, width - inset, timeline_y - 80)
    if merges:
        step = 88 if mobile else 76
        line(
            timeline_x + 7,
            timeline_y + 7,
            timeline_x + 7,
            timeline_y + (len(merges) - 1) * step + 7,
        )
        for index, record in enumerate(merges):
            y = timeline_y + index * step
            parts.append(
                f'<circle cx="{timeline_x + 7}" cy="{y + 7}" r="5" fill="{colors["accent"]}"/>'
            )
            when = instant(record["merged_at"]).astimezone(BANGKOK).strftime("%d %b %Y")
            text(timeline_x + 27, y + 4, when, 18 if mobile else 15, "muted", mono=True)
            text(
                timeline_x + 27,
                y + 34,
                f"{record['label']}  #{record['number']}",
                21 if mobile else 19,
                weight=600,
            )
    else:
        text(
            timeline_x,
            timeline_y + 12,
            "No merged PRs in this selection yet.",
            18,
            "muted",
        )
    text(
        inset, height - 24, "PUBLIC DATA ONLY · DATES IN UTC+7", 15, "muted", mono=True
    )
    parts.append("</svg>\n")
    return "\n".join(parts)


def render_record(snapshot):
    projects = snapshot["projects"]
    totals = counts(projects)
    lines = [
        "# Public contribution record",
        "",
        f"As of **{snapshot['as_of']} (Bangkok, UTC+7)**. "
        f"**{totals['merged']} merged · {totals['open']} open** pull requests.",
        "",
        f"Only pull requests authored by {snapshot['username']} in the selected public upstream libraries are counted. "
        "This is a curated record, not a complete work history or a measure of skill. "
        "Personal repositories and private activity are excluded.",
        "",
        "| Project | Merged | Open | Closed without merging |",
        "| --- | ---: | ---: | ---: |",
    ]
    for project in projects:
        stats = counts([project])
        query = urllib.parse.urlencode({"q": f"is:pr author:{snapshot['username']}"})
        url = f"https://github.com/{project['repository']}/pulls?{query}"
        lines.append(
            f"| [{project['repository']}]({url}) | {stats['merged']} | {stats['open']} | {stats['closed']} |"
        )
    lines.extend(
        [
            "",
            "## Pull requests",
            "",
            "| Project | Pull request | Status | Merged (UTC+7) |",
            "| --- | --- | --- | --- |",
        ]
    )
    for project in projects:
        for record in reversed(project["pull_requests"]):
            date = (
                instant(record["merged_at"]).astimezone(BANGKOK).date().isoformat()
                if record["merged_at"]
                else "—"
            )
            lines.append(
                f"| {project['label']} | [#{record['number']}]({record['url']}) | {record['state'].capitalize()} | {date} |"
            )
    lines.extend(
        [
            "",
            "## Scope and freshness",
            "",
            "The daily refresh uses GitHub's public REST API without authentication. "
            "Only repository names, PR numbers, public links, states, and creation/merge dates are stored; "
            "titles, descriptions, comments, and private contribution totals are not collected into the snapshot.",
            "",
            "Merged PRs use their merge date, not their creation date. Open PRs have not been merged. "
            "Closed PRs without a merge are counted separately. The timeline shows up to five recent merges.",
            "",
            "If GitHub returns an error or incomplete search results, the published snapshot is retained. "
            "Check the date above for freshness. [Source data](data/public-activity.json) · "
            "[Included projects](data/public-projects.json)",
            "",
        ]
    )
    return "\n".join(lines)


def self_test():
    config = {
        "username": "author",
        "projects": [{"repository": "upstream/library", "label": "Library"}],
    }
    metadata = {
        "full_name": "upstream/library",
        "private": False,
        "visibility": "public",
        "fork": False,
    }

    def item(number, state="closed", merged="2026-01-01T00:00:00Z"):
        return {
            "number": number,
            "user": {"login": "author"},
            "state": state,
            "repository_url": "https://api.github.com/repos/upstream/library",
            "html_url": f"https://github.com/upstream/library/pull/{number}",
            "created_at": "2025-12-31T00:00:00Z",
            "pull_request": {"merged_at": merged},
            "body": "Do not copy this description",
        }

    records = [item(n) for n in range(1, 101)] + [
        item(101, "open", None),
        item(102, "closed", None),
    ]
    pages = []

    def api(endpoint, params=None):
        if endpoint.startswith("/repos/"):
            return metadata
        pages.append(params["page"])
        start = (params["page"] - 1) * 100
        return {
            "total_count": len(records),
            "incomplete_results": False,
            "items": records[start : start + 100],
        }

    snapshot = collect(config, api)
    assert pages == [1, 2]
    assert counts(snapshot["projects"]) == {"merged": 100, "open": 1, "closed": 1}
    assert "Do not copy" not in json.dumps(snapshot)
    assert recent_merges(snapshot["projects"])[0]["merged_at"].startswith("2026")
    assert "2026-01-01" in render_record(snapshot)

    def rejects(fetch, settings=config):
        try:
            collect(settings, fetch)
        except (ValueError, KeyError):
            return
        raise AssertionError("Unsafe or incomplete data was accepted")

    for invalid in (
        {**metadata, "private": True},
        {**metadata, "visibility": "private"},
        {**metadata, "fork": True},
        {**metadata, "private": None},
    ):
        rejects(lambda endpoint, params=None: invalid)
    for bad_search in (
        {"total_count": 102, "incomplete_results": True, "items": []},
        {"total_count": 1001, "incomplete_results": False, "items": []},
        {"total_count": 2, "incomplete_results": False, "items": [item(1)]},
        {"total_count": 2, "incomplete_results": False, "items": [item(1), item(1)]},
        {
            "total_count": 1,
            "incomplete_results": False,
            "items": [{**item(1), "user": {"login": "someone-else"}}],
        },
    ):
        rejects(
            lambda endpoint, params=None: (
                metadata if endpoint.startswith("/repos/") else bad_search
            )
        )
    rejects(api, {"username": "UPSTREAM", "projects": config["projects"]})
    records.clear()
    empty = collect(config, api)
    assert counts(empty["projects"]) == {}
    for data in (snapshot, empty):
        for theme in PALETTES:
            for mobile in (True, False):
                ET.fromstring(render_svg(data, theme, mobile))
    print(
        "Self-check passed: public scope, pagination, states, dates, and empty charts."
    )


def main():
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        return
    if sys.argv[1:]:
        raise ValueError("Usage: update-public-activity.py [--self-test]")
    config = json.loads(
        (ROOT / "data/public-projects.json").read_text(encoding="utf-8")
    )
    snapshot = collect(config)
    outputs = {
        "data/public-activity.json": json.dumps(snapshot, indent=2) + "\n",
        "PUBLIC_ACTIVITY.md": render_record(snapshot),
    }
    for theme in PALETTES:
        for mobile in (False, True):
            name = f"assets/profile/activity-{'mobile-' if mobile else ''}{theme}.svg"
            outputs[name] = render_svg(snapshot, theme, mobile)
    for name, content in outputs.items():
        destination = ROOT / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    print(f"Updated public activity: {dict(counts(snapshot['projects']))}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit(f"Public activity refresh failed: {type(error).__name__}: {error}")
