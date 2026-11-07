#!/usr/bin/env python3
"""Refresh public PR charts; --self-test runs offline checks.

The collector never reads credentials or sends authorization.
"""

import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from textwrap import wrap
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
PROGRAMMING_CATALOG = json.loads(
    (ROOT / "data/programming-languages.json").read_text(encoding="utf-8")
)
PROGRAMMING_LANGUAGES = frozenset(PROGRAMMING_CATALOG["languages"])
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
LANGUAGE_COLORS = ("#c96842", "#378da2", "#9a78c7", "#8c9b4f", "#cb8798", "#738b9a")


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
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in (500, 502, 503, 504) or attempt == 2:
                raise
            error.close()
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2**attempt)


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("GitHub timestamps must include a timezone")
    return parsed


def search_prs(username, fetch, start=None, end=None):
    query = f"author:{username} is:pr is:public"
    if start is not None:
        end = end or datetime.now(timezone.utc).date()
        query += f" created:{start}..{end}"
    records = []
    expected = None
    page = 1
    while True:
        result = fetch(
            "/search/issues",
            {
                "q": query,
                "sort": "created",
                "order": "asc",
                "per_page": 100,
                "page": page,
            },
        )
        count = result["total_count"]
        if (
            type(count) is not int
            or count < 0
            or result.get("incomplete_results") is not False
        ):
            raise ValueError("Incomplete or invalid public PR search")
        if count > 1000:
            # Search returns at most 1,000 matches; split dates instead of dropping records.
            start = start or date(1970, 1, 1)
            end = end or datetime.now(timezone.utc).date()
            if start == end:
                # shortcut: over 1,000 PRs in one day needs timestamp-based partitions.
                raise ValueError(
                    "Too many PRs in one day; keeping the published snapshot"
                )
            middle = start + (end - start) // 2
            records = search_prs(username, fetch, start, middle)
            records += search_prs(username, fetch, middle + timedelta(days=1), end)
            if len(records) != count:
                raise ValueError(
                    "Search changed during date partitioning; retry the refresh"
                )
            return records
        if expected is not None and count != expected:
            raise ValueError("Search changed during pagination; retry the refresh")
        expected = count
        records.extend(result["items"])
        if len(records) >= expected:
            if len(records) != expected:
                raise ValueError(
                    "Search total does not match the returned pull requests"
                )
            return records
        if len(result["items"]) != 100:
            raise ValueError("Search returned fewer PRs than its reported total")
        page += 1


def collect(config, fetch=get_json):
    username = config["username"]
    if not re.fullmatch(r"[A-Za-z0-9-]+", username):
        raise ValueError("Invalid GitHub username")
    grouped = defaultdict(list)
    seen = set()
    for item in search_prs(username, fetch):
        match = re.fullmatch(
            r"https://api\.github\.com/repos/([A-Za-z0-9-]+/[A-Za-z0-9_.-]+)",
            item["repository_url"],
        )
        if not match:
            raise ValueError("Invalid repository URL")
        repository = match[1]
        number = item["number"]
        url = f"https://github.com/{repository}/pull/{number}"
        identity = (repository.casefold(), number)
        if (
            type(number) is not int
            or number < 1
            or identity in seen
            or item.get("user", {}).get("login", "").casefold() != username.casefold()
            or item["html_url"] != url
        ):
            raise ValueError("Pull request identity did not match the query")
        seen.add(identity)
        merged_at = item["pull_request"]["merged_at"]
        instant(item["created_at"])
        if item["state"] not in ("open", "closed"):
            raise ValueError("Unknown pull request state")
        if merged_at is not None and (
            item["state"] != "closed"
            or instant(merged_at) < instant(item["created_at"])
        ):
            raise ValueError("Invalid merge state or date")
        if repository.split("/")[0].casefold() == username.casefold():
            continue
        grouped[repository].append(
            {
                "number": number,
                "url": url,
                "state": "merged" if merged_at else item["state"],
                "created_at": item["created_at"],
                "merged_at": merged_at,
            }
        )

    projects = []
    for repository, records in grouped.items():
        metadata = fetch(f"/repos/{repository}")
        owner = metadata.get("owner", {}).get("login", "")
        language = metadata.get("language")
        if (
            metadata.get("private") is not False
            or metadata.get("visibility") != "public"
            or metadata.get("full_name", "").casefold() != repository.casefold()
            or owner.casefold() != repository.split("/")[0].casefold()
            or owner.casefold() == username.casefold()
            or type(metadata.get("fork")) is not bool
            or (
                language is not None and (not isinstance(language, str) or not language)
            )
        ):
            raise ValueError(
                f"Cannot verify {repository} as a public repository owned by someone else"
            )
        if metadata["fork"]:
            continue
        projects.append(
            {
                "repository": repository,
                "owner": owner,
                "fork": metadata["fork"],
                "language": language,
                "pull_requests": sorted(records, key=lambda record: record["number"]),
            }
        )
    projects.sort(
        key=lambda p: (
            -counts([p])["merged"],
            -len(p["pull_requests"]),
            p["repository"].casefold(),
        )
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
        {**record, "repository": project["repository"]}
        for project in projects
        for record in project["pull_requests"]
        if record["state"] == "merged"
    ]
    return sorted(
        merged, key=lambda record: instant(record["merged_at"]), reverse=True
    )[:5]


def languages(projects):
    totals = Counter()
    for project in projects:
        total = counts([project])["merged"]
        if total:
            totals[project["language"] or "Not reported"] += total
    return sorted(totals.items(), key=lambda entry: (-entry[1], entry[0]))


def programming_projects(projects):
    return [
        project for project in projects if project["language"] in PROGRAMMING_LANGUAGES
    ]


def owners(projects):
    groups = defaultdict(list)
    for project in projects:
        if counts([project])["merged"]:
            groups[project["owner"]].append(project)
    return sorted(
        groups.items(),
        key=lambda group: (-counts(group[1])["merged"], group[0].casefold()),
    )


class SVG:
    def __init__(self, width, height, theme, title, description):
        self.width, self.height, self.colors = width, height, PALETTES[theme]
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>',
            '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}</style>',
            f'<rect width="{width}" height="{height}" rx="16" fill="{self.colors["bg"]}"/>',
        ]

    def text(
        self, x, y, value, size=18, color="ink", weight=400, anchor="start", mono=False
    ):
        self.parts.append(
            f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" fill="{self.colors.get(color, color)}" class="{"mono" if mono else ""}">{escape(str(value))}</text>'
        )

    def lines(self, x, y, value, width, size=18, **options):
        lines = wrap(value, width=width, break_long_words=True, break_on_hyphens=False)
        for index, value in enumerate(lines):
            self.text(x, y + index * size * 1.25, value, size, **options)
        return len(lines) * size * 1.25

    def line(self, x1, y1, x2, y2, color="line", dashed=False):
        self.parts.append(
            f'<path d="M{x1:.2f} {y1:.2f}L{x2:.2f} {y2:.2f}" stroke="{self.colors.get(color, color)}" stroke-width="1.5" fill="none" stroke-dasharray="{"4 6" if dashed else "none"}"/>'
        )

    def circle(self, x, y, radius, fill, stroke=None, dashed=False):
        self.parts.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" fill="{self.colors.get(fill, fill)}" stroke="{self.colors.get(stroke, stroke) if stroke else "none"}" stroke-width="1.5" stroke-dasharray="{"4 6" if dashed else "none"}"/>'
        )

    def rect(self, x, y, width, height, fill, stroke=None):
        self.parts.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height}" fill="{self.colors.get(fill, fill)}" stroke="{self.colors.get(stroke, stroke) if stroke else "none"}"/>'
        )

    def finish(self):
        return "\n".join(self.parts + ["</svg>\n"])


def heading(svg, title, subtitle, snapshot, mobile):
    inset = 24 if mobile else 36
    svg.text(inset, 42, title, 27, weight=700)
    svg.text(inset, 73, subtitle, 19 if mobile else 18, "muted")
    when = datetime.fromisoformat(snapshot["as_of"]).strftime("%d %b %Y").upper()
    svg.text(
        inset if mobile else svg.width - inset,
        101 if mobile else 42,
        f"AS OF {when}",
        15 if mobile else 13,
        "muted",
        anchor="start" if mobile else "end",
        mono=True,
    )


def render_activity(snapshot, theme, mobile):
    projects = snapshot["projects"]
    totals, merges = counts(projects), recent_merges(projects)
    width, inset = (480, 24) if mobile else (1000, 36)
    timeline_y = 402 if mobile else 130
    height = timeline_y + max(len(merges), 1) * 106 + 34 if mobile else 608
    svg = SVG(
        width,
        height,
        theme,
        "Public contribution activity",
        "Public pull requests authored in non-fork repositories owned by others. Merged, open and closed PRs are counted separately.",
    )
    heading(
        svg,
        "Public contributions",
        "Public PRs · Fork repositories excluded",
        snapshot,
        mobile,
    )
    stat_y = 140 if mobile else 130
    cell = 144 if mobile else 150
    for index, (value, label) in enumerate(
        zip(
            (totals["merged"], totals["open"], len(projects)),
            ("MERGED PRS", "OPEN PRS", "REPOS"),
        )
    ):
        x = inset + index * cell
        svg.text(x, stat_y + 34, value, 40, "accent" if index == 0 else "ink", 700)
        svg.text(x, stat_y + 65, label, 20 if mobile else 16, "muted", mono=True)
    svg.line(inset, stat_y + 92, inset + 432, stat_y + 92)
    total = sum(totals.values())
    svg.text(inset, stat_y + 136, f"{total} public pull requests", 23, weight=650)
    x = inset
    for state, color in zip(STATES, ("accent", "paper", "muted")):
        length = 432 * totals[state] / (total or 1)
        if length:
            svg.rect(
                x,
                stat_y + 158,
                length,
                18,
                color,
                "muted" if state != "merged" else "accent",
            )
            x += length
    svg.text(
        inset,
        stat_y + 208,
        f"{totals['closed']} closed without merging",
        21 if mobile else 18,
        "muted",
    )
    if not mobile:
        svg.lines(
            inset,
            420,
            "All discovered public repositories owned by others are included, except forks.",
            42,
            18,
            color="muted",
        )
        svg.lines(
            inset,
            506,
            "Counts describe public activity. They do not measure impact or skill.",
            42,
            18,
            color="muted",
        )
    tx = inset if mobile else 550
    svg.text(tx, timeline_y, "Recent merges", 24, weight=650)
    for index, record in enumerate(merges):
        y = timeline_y + 48 + index * (106 if mobile else 84)
        svg.circle(tx + 6, y - 6, 4, "accent")
        when = instant(record["merged_at"]).astimezone(BANGKOK).strftime("%d %b %Y")
        svg.text(
            tx + 22,
            y - 3,
            f"{when} · #{record['number']}",
            17 if mobile else 15,
            "muted",
            mono=True,
        )
        svg.lines(
            tx + 22,
            y + 26,
            record["repository"].replace("/", "/ "),
            33 if mobile else 36,
            20 if mobile else 18,
            weight=600,
        )
    if not merges:
        svg.text(tx, timeline_y + 50, "No merged PRs yet.", 20, "muted")
    return svg.finish()


def language_palette(projects):
    return {
        language: LANGUAGE_COLORS[index % len(LANGUAGE_COLORS)]
        for index, (language, _) in enumerate(languages(projects))
    }


def render_network(snapshot, theme, mobile):
    projects = snapshot["projects"]
    groups = owners(projects)
    palette = language_palette(projects)
    total = counts(projects)["merged"]
    repo_count = sum(len(group) for _, group in groups)
    legend_rows = math.ceil(len(palette) / (2 if mobile else 4))
    top = 150 + legend_rows * 30
    if mobile:
        height = top + 110 + sum(76 + len(group) * 62 for _, group in groups) + 30
    else:
        # Separate orbit panels keep every owner legible as the network grows.
        panels = max(1, math.ceil(len(groups) / 12))
        height = top + panels * 850 + 28
    svg = SVG(
        480 if mobile else 1000,
        height,
        theme,
        "Where the work lands",
        f"{total} merged PRs across {repo_count} public repositories owned by others. Owners connect to repositories. Bubble area represents merged PR count within each node type; color is repository primary language.",
    )
    inset = 24 if mobile else 36
    svg.text(inset, 42, "Where the work lands", 27, weight=700)
    svg.text(inset, 75, f"{total} merged PRs · {repo_count} repositories", 20, "muted")
    svg.text(
        inset,
        104,
        "Size = merged PRs · Colour = repo language",
        17 if mobile else 18,
        "muted",
    )
    columns = 2 if mobile else 4
    cell = 216 if mobile else 232
    for index, (language, color) in enumerate(palette.items()):
        x, y = inset + index % columns * cell, 140 + index // columns * 30
        svg.circle(x + 5, y - 6, 5, color)
        svg.text(x + 20, y, language, 18 if mobile else 17)
    maximum = max((counts(group)["merged"] for _, group in groups), default=1)

    def owner_node(x, y, group, scale):
        count = counts(group)["merged"]
        radius = scale * math.sqrt(count / maximum)
        color = palette[languages(group)[0][0]]
        svg.circle(x, y, radius + 6, "none", color, dashed=True)
        svg.circle(x, y, radius, color)
        return radius, color, count

    if mobile:
        svg.circle(52, top + 26, 27, "paper", "accent")
        svg.text(52, top + 33, "EH", 21, weight=700, anchor="middle")
        svg.text(96, top + 33, snapshot["username"], 23, weight=700)
        y = top + 102
        previous_y = top + 54
        for owner, group in groups:
            svg.line(52, previous_y, 52, y, dashed=True)
            radius, _, count = owner_node(52, y, group, 24)
            svg.text(96, y - 6, owner + "/", 21, weight=700)
            svg.text(96, y + 20, f"{count} merged", 17, "muted")
            for index, project in enumerate(group):
                py = y + 60 + index * 62
                count = counts([project])["merged"]
                color = palette[project["language"] or "Not reported"]
                svg.line(52, y + radius, 108, py, color, dashed=True)
                svg.circle(108, py, 16 * math.sqrt(count / maximum), color)
                svg.lines(
                    140,
                    py - 2,
                    project["repository"].split("/", 1)[1],
                    25,
                    19,
                    weight=550,
                )
                svg.text(446, py + 5, count, 18, "muted", anchor="end", mono=True)
            previous_y = y + radius + 7
            y += 76 + len(group) * 62
    else:
        for panel_index in range(max(1, math.ceil(len(groups) / 12))):
            panel = groups[panel_index * 12 : (panel_index + 1) * 12]
            cx, cy = 500, top + 425 + panel_index * 850
            svg.circle(cx, cy, 238, "none", "line", dashed=True)
            svg.circle(cx, cy, 350, "none", "line", dashed=True)
            for index, (owner, group) in enumerate(panel):
                angle = -math.pi / 2 + 2 * math.pi * index / max(len(panel), 1)
                ox, oy = cx + 238 * math.cos(angle), cy + 238 * math.sin(angle)
                svg.line(cx, cy, ox, oy, "line", dashed=True)
                radius, color, count = owner_node(ox, oy, group, 42)
                svg.text(
                    ox,
                    oy + radius + 27 if radius > 24 else oy - radius - 17,
                    owner + "/",
                    15,
                    weight=700,
                    anchor="middle",
                )
                svg.text(
                    ox,
                    oy + radius + 49 if radius > 24 else oy + radius + 25,
                    count,
                    15,
                    "muted",
                    anchor="middle",
                    mono=True,
                )
                for repo_index, project in enumerate(group):
                    spread = min(0.38, 0.65 * 2 * math.pi / max(len(panel), 1))
                    child_angle = angle + (
                        repo_index - (len(group) - 1) / 2
                    ) * spread / max(len(group) - 1, 1)
                    rx, ry = (
                        cx + 350 * math.cos(child_angle),
                        cy + 350 * math.sin(child_angle),
                    )
                    count = counts([project])["merged"]
                    color = palette[project["language"] or "Not reported"]
                    radius = 24 * math.sqrt(count / maximum)
                    svg.line(ox, oy, rx, ry, color, dashed=True)
                    svg.circle(rx, ry, radius, color)
                    svg.text(
                        rx,
                        ry - radius - 12,
                        count,
                        15,
                        "muted",
                        anchor="middle",
                        mono=True,
                    )
                    svg.lines(
                        rx,
                        ry + radius + 23,
                        project["repository"].split("/", 1)[1],
                        21,
                        15,
                        anchor="middle",
                    )
            svg.circle(cx, cy, 45, "paper", "accent")
            svg.text(cx, cy + 9, "EH", 27, weight=700, anchor="middle")
            svg.text(cx, cy + 76, snapshot["username"], 20, weight=600, anchor="middle")
        svg.text(
            inset,
            height - 21,
            "OWNERS → REPOSITORIES · MERGED PRS ONLY",
            14,
            "muted",
            mono=True,
        )
    if not groups:
        svg.text(inset, top + 100, "No merged PRs yet.", 20, "muted")
    return svg.finish()


def render_languages(snapshot, theme, mobile):
    projects = programming_projects(snapshot["projects"])
    values = languages(projects)
    palette = language_palette(snapshot["projects"])
    total = sum(value for _, value in values)
    all_merged = counts(snapshot["projects"])["merged"]
    rows_y = 450 if mobile else 150
    height = max(rows_y + len(values) * 70 + 115, 590 if mobile else 520)
    svg = SVG(
        480 if mobile else 1000,
        height,
        theme,
        "Programming languages across merged PRs",
        f"{total} eligible merged PRs out of {all_merged}. Only repository primary languages classified as programming by GitHub Linguist are included. Percentages use eligible PRs; markup, data, prose, and unreported languages are excluded. This does not identify changed-file languages or measure proficiency.",
    )
    inset = 24 if mobile else 36
    svg.text(inset, 42, "Programming languages", 27, weight=700)
    svg.text(inset, 75, "By repo's primary language · PR-weighted", 18, "muted")
    svg.text(
        inset,
        104,
        f"{total} of {all_merged} merged PRs · {sum(counts([p])['merged'] > 0 for p in projects)} repositories",
        18,
        "muted",
    )
    cx, cy, radius = (240, 274, 112) if mobile else (230, 278, 123)
    circumference = 2 * math.pi * radius
    offset = 0
    svg.circle(cx, cy, radius, "none", "line")
    for language, count in values:
        length = circumference * count / total
        svg.parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{palette[language]}" stroke-width="32" stroke-dasharray="{length:.5f} {circumference - length:.5f}" stroke-dashoffset="{-offset:.5f}" transform="rotate(-90 {cx} {cy})"/>'
        )
        offset += length
    svg.text(
        cx,
        cy + 5,
        f"{values[0][1] / total:.0%}" if total else "0",
        42,
        weight=700,
        anchor="middle",
    )
    svg.text(
        cx,
        cy + 39,
        values[0][0] if total else "Eligible PRs",
        21,
        "muted",
        anchor="middle",
    )
    rx, bar_width = (inset, 432) if mobile else (500, 464)
    for index, (language, count) in enumerate(values):
        y = rows_y + index * 70
        svg.circle(rx + 5, y - 7, 5, palette[language])
        svg.text(rx + 20, y, language, 21, weight=650)
        svg.text(
            rx + bar_width,
            y,
            f"{count} · {count / total:.1%}",
            18,
            "muted",
            anchor="end",
            mono=True,
        )
        svg.rect(rx, y + 15, bar_width, 8, "line")
        svg.rect(rx, y + 15, bar_width * count / total, 8, palette[language])
    svg.lines(
        inset,
        height - (100 if mobile else 70),
        "Only programming languages classified by GitHub Linguist. Based on repository language, not changed files or proficiency.",
        44 if mobile else 100,
        17 if mobile else 16,
        color="muted",
    )
    return svg.finish()


def render_record(snapshot):
    projects = snapshot["projects"]
    totals = counts(projects)
    programming = languages(programming_projects(projects))
    eligible = sum(value for _, value in programming)
    lines = [
        "# Public contribution record",
        "",
        f"As of **{snapshot['as_of']} (Bangkok, UTC+7)**. **{totals['merged']} merged · {totals['open']} open · {totals['closed']} closed without merging** across **{len(projects)} repositories**.",
        "",
        f"All public pull requests authored by {snapshot['username']} that GitHub search can discover in non-fork repositories outside the {snapshot['username']} account are included. There is no project allowlist. Archived projects, documentation, translations, and older work are included. Fork repositories, personal repositories, and private activity are excluded. A PR sent from a fork to an original upstream repository still counts; the destination repository determines eligibility.",
        "",
        "## Repositories",
        "",
        "| Repository | Primary language | Merged | Open | Closed, not merged |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for project in projects:
        stats = counts([project])
        query = urllib.parse.urlencode({"q": f"is:pr author:{snapshot['username']}"})
        url = f"https://github.com/{project['repository']}/pulls?{query}"
        language = (project["language"] or "Not reported").replace("|", "\\|")
        lines.append(
            f"| [{project['repository']}]({url}) | {language} | {stats['merged']} | {stats['open']} | {stats['closed']} |"
        )
    lines += [
        "",
        "## Programming languages across merged PRs",
        "",
        f"The donut uses **{eligible} of {totals['merged']} merged PRs**. The remaining {totals['merged'] - eligible} have a non-programming or unreported repository language and are excluded from its percentages.",
        "",
        "| Repository primary language | Merged PRs | Share |",
        "| --- | ---: | ---: |",
    ]
    for language, count in programming:
        lines.append(f"| {language} | {count} | {count / eligible:.1%} |")
    lines += [
        "",
        "## Pull requests",
        "",
        "| Repository | Pull request | Status | Merged (UTC+7) |",
        "| --- | --- | --- | --- |",
    ]
    for project in projects:
        for record in reversed(project["pull_requests"]):
            when = (
                instant(record["merged_at"]).astimezone(BANGKOK).date().isoformat()
                if record["merged_at"]
                else "—"
            )
            lines.append(
                f"| {project['repository']} | [#{record['number']}]({record['url']}) | {record['state'].capitalize()} | {when} |"
            )
    lines += [
        "",
        "## Method and freshness",
        "",
        "The daily refresh searches GitHub's public REST API without authentication, follows every result page, and splits large searches by creation date. Each destination repository is checked for public visibility, ownership, and non-fork status before inclusion. Deleted repositories and PRs no longer visible in public search cannot be recovered by this record.",
        "",
        "The orbit chart groups merged PRs by repository owner, including both people and organizations. Each repository belongs to one owner. Bubble area is proportional to merged PR count within each node type; owner and repository bubbles use different scales. Colors follow repository primary language, with an owner's dominant language used for its bubble. All owners and repositories with merged PRs are shown; additional orbit panels are added as needed.",
        "",
        f"The donut assigns one count per merged PR to the destination repository's current primary language only when [GitHub Linguist]({PROGRAMMING_CATALOG['source']}) classifies it as programming. The [pinned language catalog](data/programming-languages.json) defines this classification. Markup (including HTML and CSS), data, prose, unreported languages, and languages absent from that catalog are excluded from the donut and its denominator. The overall totals and orbit chart still include these contributions. This does not measure changed-file languages, lines of code, effort, impact, or proficiency.",
        "",
        "Only authored pull requests are counted. Direct commits, issues, reviews, and discussions are not part of these PR charts. Open PRs have not been merged; closed PRs without a merge are separate. The timeline uses merge dates and shows the five most recent merges.",
        "",
        "Only public repository identifiers, owner names, language/fork metadata, PR numbers, links, states, and creation/merge dates are stored. Titles, descriptions, comments, and private contribution totals are not stored.",
        "",
        "If GitHub returns an error, incomplete results, or a rate limit, the published snapshot is retained. Check the date above for freshness. [Source data](data/public-activity.json) · [Profile configuration](data/public-profile.json) · [GitHub search documentation](https://docs.github.com/en/rest/search/search)",
        "",
    ]
    return "\n".join(lines)


def self_test():
    from io import StringIO
    from unittest.mock import patch

    def http_error(code):
        return urllib.error.HTTPError(
            "https://api.github.com/search/issues", code, "test", {}, None
        )

    with (
        patch(
            "urllib.request.urlopen",
            side_effect=[http_error(504), StringIO('{"ok": true}')],
        ) as request,
        patch("time.sleep") as sleep,
    ):
        assert get_json("/search/issues") == {"ok": True}
        assert request.call_count == 2 and sleep.call_count == 1
        assert not request.call_args.args[0].has_header("Authorization")
    for code, attempts in ((504, 3), (403, 1), (429, 1)):
        with (
            patch("urllib.request.urlopen", side_effect=http_error(code)) as request,
            patch("time.sleep"),
        ):
            try:
                get_json("/search/issues")
            except urllib.error.HTTPError:
                pass
            else:
                raise AssertionError("HTTP failure was hidden")
            assert request.call_count == attempts

    def item(
        number,
        repository="upstream/library",
        state="closed",
        merged="2026-01-01T00:00:00Z",
    ):
        return {
            "number": number,
            "user": {"login": "author"},
            "state": state,
            "repository_url": f"https://api.github.com/repos/{repository}",
            "html_url": f"https://github.com/{repository}/pull/{number}",
            "created_at": "2025-12-31T00:00:00Z",
            "pull_request": {"merged_at": merged},
            "body": "Do not copy this description",
        }

    records = [item(n) for n in range(1, 101)] + [
        item(101, state="open", merged=None),
        item(102, state="closed", merged=None),
        item(1, "another/fork"),
        item(1, "AuThOr/personal"),
        item(1, "upstream/unknown"),
    ]
    pages = []
    metadata = {}
    for repository in ("upstream/library", "another/fork", "upstream/unknown"):
        metadata[repository] = {
            "full_name": repository,
            "owner": {"login": repository.split("/")[0]},
            "private": False,
            "visibility": "public",
            "fork": repository == "another/fork",
            "language": "Go" if repository == "upstream/library" else None,
        }

    def api(endpoint, params=None):
        if endpoint.startswith("/repos/"):
            return metadata[endpoint.removeprefix("/repos/")]
        assert "is:pr is:public" in params["q"]
        pages.append(params["page"])
        start = (params["page"] - 1) * 100
        return {
            "total_count": len(records),
            "incomplete_results": False,
            "items": records[start : start + 100],
        }

    snapshot = collect({"username": "author"}, api)
    assert pages == [1, 2]
    assert counts(snapshot["projects"]) == {"merged": 101, "open": 1, "closed": 1}
    assert len(snapshot["projects"]) == 2
    assert not any(p["fork"] for p in snapshot["projects"])
    assert languages(snapshot["projects"]) == [("Go", 100), ("Not reported", 1)]
    assert languages(programming_projects(snapshot["projects"])) == [("Go", 100)]
    assert {"Go", "JavaScript", "TypeScript", "Python", "Rust"} <= PROGRAMMING_LANGUAGES
    non_programming = {
        **snapshot,
        "projects": [
            {**snapshot["projects"][0], "language": language}
            for language in ("HTML", "CSS", "JSON", "Markdown", None)
        ],
    }
    assert programming_projects(non_programming["projects"]) == []
    assert "100.0%" in render_record(snapshot)
    assert "100 of 101 merged PRs" in render_languages(snapshot, "light", False)
    assert [counts(group)["merged"] for _, group in owners(snapshot["projects"])] == [
        101,
    ]
    assert "Do not copy" not in json.dumps(snapshot) and "personal" not in json.dumps(
        snapshot
    )
    assert recent_merges(snapshot["projects"])[0]["merged_at"].startswith("2026")
    assert "2026-01-01" in render_record(snapshot)

    def rejects(fetch):
        try:
            collect({"username": "author"}, fetch)
        except (ValueError, KeyError):
            return
        raise AssertionError("Unsafe or incomplete data was accepted")

    for change in (
        {"private": True},
        {"visibility": "private"},
        {"private": None},
        {"owner": {"login": "author"}},
    ):
        rejects(
            lambda endpoint, params=None: (
                {**metadata["upstream/library"], **change}
                if endpoint.startswith("/repos/")
                else api(endpoint, params)
            )
        )
    for bad in (
        {"total_count": 2, "incomplete_results": True, "items": []},
        {"total_count": 2, "incomplete_results": False, "items": [item(1)]},
        {"total_count": 2, "incomplete_results": False, "items": [item(1), item(1)]},
        {
            "total_count": 1,
            "incomplete_results": False,
            "items": [{**item(1), "user": {"login": "other"}}],
        },
        {
            "total_count": 1,
            "incomplete_results": False,
            "items": [
                {
                    **item(1),
                    "repository_url": "https://evil.example/repos/upstream/library",
                }
            ],
        },
    ):
        rejects(
            lambda endpoint, params=None: (
                api(endpoint, params) if endpoint.startswith("/repos/") else bad
            )
        )

    partitioned = []

    def large_search(endpoint, params):
        query = params["q"]
        if "2025-01-01..2025-01-02" in query:
            return {"total_count": 1001, "incomplete_results": False, "items": []}
        count = 501 if "2025-01-01..2025-01-01" in query else 500
        partitioned.append(query)
        start = (params["page"] - 1) * 100
        return {
            "total_count": count,
            "incomplete_results": False,
            "items": list(range(start, min(start + 100, count))),
        }

    assert (
        len(search_prs("author", large_search, date(2025, 1, 1), date(2025, 1, 2)))
        == 1001
    )
    assert len(set(partitioned)) == 2
    records.clear()
    empty = collect({"username": "author"}, api)
    for data in (snapshot, non_programming, empty):
        render_record(data)
        for theme in PALETTES:
            for mobile in (True, False):
                for renderer in (render_activity, render_network, render_languages):
                    ET.fromstring(renderer(data, theme, mobile))
    print(
        "Self-check passed: discovery, public scope, own-repo and fork exclusions, programming-language denominator, pagination, date partitioning, and empty charts."
    )


def main():
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        return
    if sys.argv[1:]:
        raise ValueError("Usage: update-public-activity.py [--self-test]")
    config = json.loads((ROOT / "data/public-profile.json").read_text(encoding="utf-8"))
    snapshot = collect(config)
    outputs = {
        "data/public-activity.json": json.dumps(snapshot, indent=2) + "\n",
        "PUBLIC_ACTIVITY.md": render_record(snapshot),
    }
    for kind, renderer in (
        ("", render_activity),
        ("network-", render_network),
        ("languages-", render_languages),
    ):
        for theme in PALETTES:
            for mobile in (False, True):
                name = f"assets/profile/activity-{kind}{'mobile-' if mobile else ''}{theme}.svg"
                outputs[name] = renderer(snapshot, theme, mobile)
    for name, content in outputs.items():
        destination = ROOT / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    print(
        f"Updated {len(snapshot['projects'])} public repositories: {dict(counts(snapshot['projects']))}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit(f"Public activity refresh failed: {type(error).__name__}: {error}")
