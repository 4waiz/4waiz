#!/usr/bin/env python3
"""Render a Repobeats-inspired GitHub activity dashboard for a personal profile.

Every number comes from GitHub's GraphQL API (contributionsCollection) for the
last 30 days, compared with the 30 days before that. Nothing is estimated:
when a ratio has no meaningful denominator it is shown as N/A.

Output: assets/stats/stats-dark.svg and assets/stats/stats-light.svg.
Dependencies: Python 3.10+ standard library only.

Usage:
  GH_TOKEN=... python scripts/profile_stats.py [--user 4waiz] [--out assets/stats]
  python scripts/profile_stats.py --from-json data.json   # re-render saved data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from xml.sax.saxutils import escape

API = "https://api.github.com/graphql"
DAYS = 30

# ---------------------------------------------------------------- data ----


def graphql(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = Request(API, data=body, method="POST", headers={
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "4waiz-profile-stats",
    })
    try:
        with urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except HTTPError as exc:  # never echo request headers, only the API's reply
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"GitHub API returned HTTP {exc.code}: {detail}") from None
    except URLError as exc:
        raise SystemExit(f"Could not reach the GitHub API: {exc.reason}") from None
    if payload.get("errors"):
        raise SystemExit("GitHub GraphQL error: " + json.dumps(payload["errors"])[:800])
    return payload["data"]


def iso(day: date, end: bool = False) -> str:
    return f"{day.isoformat()}T{'23:59:59' if end else '00:00:00'}Z"


def fetch(user: str, token: str, today: date) -> dict:
    """Collect everything the dashboard draws, in UTC calendar days."""
    start = today - timedelta(days=DAYS - 1)
    prev_start, prev_end = start - timedelta(days=DAYS), start - timedelta(days=1)

    # One alias per day gives exact daily counts per contribution type.
    per_day = "totalCommitContributions totalIssueContributions totalPullRequestContributions"
    day_aliases = "\n".join(
        f'd{i}: contributionsCollection(from: "{iso(d)}", to: "{iso(d, True)}") {{ {per_day} }}'
        for i, d in enumerate(start + timedelta(days=n) for n in range(DAYS))
    )
    query = f"""
    query($login: String!, $from: DateTime!, $to: DateTime!, $pfrom: DateTime!, $pto: DateTime!) {{
      user(login: $login) {{
        recent: contributionsCollection(from: $from, to: $to) {{
          contributionCalendar {{ totalContributions
            weeks {{ contributionDays {{ date contributionCount }} }} }}
          {per_day}
          commitContributionsByRepository(maxRepositories: 25) {{
            repository {{ nameWithOwner isPrivate }}
            contributions(first: 100) {{ totalCount nodes {{ occurredAt commitCount }} }}
          }}
        }}
        previous: contributionsCollection(from: $pfrom, to: $pto) {{
          contributionCalendar {{ totalContributions }}
          {per_day}
        }}
        {day_aliases}
      }}
    }}"""
    window = {"from": iso(start), "to": iso(today, True)}
    data = graphql(token, query, {"login": user, **window,
                                  "pfrom": iso(prev_start), "pto": iso(prev_end, True)})["user"]
    if data is None:
        raise SystemExit(f"GitHub user not found: {user}")

    recent, previous = data["recent"], data["previous"]
    days = [start + timedelta(days=n) for n in range(DAYS)]
    calendar = {d["date"]: d for w in recent["contributionCalendar"]["weeks"]
                for d in w["contributionDays"]}

    merged = paged_states(token, user, window, "pullRequest", "mergedAt", start)
    closed = paged_states(token, user, window, "issue", "closedAt", start)

    repos = []
    for entry in recent["commitContributionsByRepository"]:
        repo = entry["repository"]
        if repo["isPrivate"]:  # never publish private repository names
            continue
        daily = [0] * DAYS
        for node in entry["contributions"]["nodes"]:
            idx = (date.fromisoformat(node["occurredAt"][:10]) - start).days
            if 0 <= idx < DAYS:
                daily[idx] += node["commitCount"]
        repos.append({"name": repo["nameWithOwner"], "commits": sum(daily), "daily": daily})
    repos.sort(key=lambda r: (-r["commits"], r["name"].lower()))

    def counts(src: dict) -> dict:
        return {"commits": src["totalCommitContributions"],
                "pulls": src["totalPullRequestContributions"],
                "issues": src["totalIssueContributions"],
                "contributions": src.get("contributionCalendar", {}).get("totalContributions")}

    return {
        "user": user,
        "generated": today.isoformat(),
        "start": start.isoformat(),
        "current": counts(recent),
        "previous": counts(previous),
        "calendar": [{"count": calendar.get(d.isoformat(), {}).get("contributionCount", 0)} for d in days],
        "daily": {
            "commits": [data[f"d{i}"]["totalCommitContributions"] for i in range(DAYS)],
            "pulls": [data[f"d{i}"]["totalPullRequestContributions"] for i in range(DAYS)],
            "issues": [data[f"d{i}"]["totalIssueContributions"] for i in range(DAYS)],
            "merged": merged,
            "closed": closed,
        },
        "repos": repos[:4],
    }


def paged_states(token: str, user: str, window: dict, kind: str, field: str, start: date) -> list[int]:
    """Per day: how many of the user's PRs/issues opened in the window were merged/closed."""
    connection = "pullRequestContributions" if kind == "pullRequest" else "issueContributions"
    query = f"""
    query($login: String!, $from: DateTime!, $to: DateTime!, $after: String) {{
      user(login: $login) {{ contributionsCollection(from: $from, to: $to) {{
        {connection}(first: 100, after: $after) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{ {kind} {{ {field} }} }}
        }} }} }}
    }}"""
    daily, after = [0] * DAYS, None
    while True:
        page = graphql(token, query, {"login": user, **window, "after": after})
        conn = page["user"]["contributionsCollection"][connection]
        for node in conn["nodes"]:
            stamp = (node.get(kind) or {}).get(field)
            if stamp:
                idx = (date.fromisoformat(stamp[:10]) - start).days
                if 0 <= idx < DAYS:
                    daily[idx] += 1
        if not conn["pageInfo"]["hasNextPage"]:
            return daily
        after = conn["pageInfo"]["endCursor"]


# ---------------------------------------------------------------- math ----


def change(now: int, before: int) -> tuple[str, str]:
    """Return ("+72 (+3,600%)", direction) with N/A when the percentage is undefined."""
    diff = now - before
    sign = "+" if diff > 0 else ""
    pct = "N/A" if before == 0 else f"{sign}{diff / before * 100:,.1f}%".replace(".0%", "%")
    direction = "up" if diff > 0 else "down" if diff < 0 else "flat"
    return f"{sign}{diff:,} ({pct})", direction


# --------------------------------------------------------------- theme ----

THEMES = {
    "dark": {
        "bg": "#0D1117", "border": "#253350", "text": "#F5F8FF", "muted": "#93A6CC",
        "faint": "#5C6B8A", "track": "#161E2E", "empty": "#161B26",
        "blue": "#60A5FA", "blue2": "#2563EB", "purple": "#A78BFA", "purple2": "#6D4FD8",
        "orange": "#F59E5B", "teal": "#2DD4BF", "up": "#4ADE80", "down": "#F87171",
        "heat": ["#161B26", "#0C2D6B", "#1D4ED8", "#3B82F6", "#93C5FD"],
    },
    "light": {
        "bg": "#FFFFFF", "border": "#D3DEF0", "text": "#0B1B3F", "muted": "#51618A",
        "faint": "#8A97B3", "track": "#F1F5FC", "empty": "#EEF2F8",
        "blue": "#2563EB", "blue2": "#93C5FD", "purple": "#7C3AED", "purple2": "#B9A3F7",
        "orange": "#EA580C", "teal": "#0F9F8F", "up": "#16A34A", "down": "#DC2626",
        "heat": ["#EEF2F8", "#BFDBFE", "#60A5FA", "#2563EB", "#1E40AF"],
    },
}
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"

W, PAD, GAP = 840, 1, 12
COL = (W - 2 * PAD - 2 * GAP) / 3


# ----------------------------------------------------------------- svg ----


def levels(values: list[int]) -> list[int]:
    """0 for no activity, else 1-4 by quartile of this window's active days.

    GitHub's own calendar levels are relative to the whole year, which flattens
    a 30-day strip; ranking within the window keeps busy days distinguishable.
    """
    active = sorted(v for v in values if v > 0)
    if not active:
        return [0] * len(values)
    cuts = [active[min(len(active) - 1, len(active) * q // 4)] for q in (1, 2, 3)]
    return [0 if v == 0 else 1 + sum(v > c for c in cuts) for v in values]


def text(x, y, s, size=13, fill="text", weight=400, anchor="start", mono=False) -> str:
    return (f'<text x="{x:g}" y="{y:g}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" fill="{{{fill}}}"{" class=\"m\"" if mono else ""}>{escape(str(s))}</text>')


def card(x, y, w, h) -> str:
    return (f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="6" '
            f'fill="{{bg}}" stroke="{{border}}" stroke-width="1"/>')


# 16x16 line icons, drawn at the given origin.
ICONS = {
    "calendar": lambda c: "".join(
        f'<rect x="{1 + 5 * (i % 3)}" y="{2 + 4.5 * (i // 3)}" width="4" height="3" rx="0.6" fill="{c}"/>'
        for i in range(9)),
    "issue": lambda c: (f'<circle cx="8" cy="8" r="6.25" fill="none" stroke="{c}" stroke-width="1.5"/>'
                        f'<circle cx="8" cy="8" r="1.6" fill="{c}"/>'),
    "pull": lambda c: (f'<g fill="none" stroke="{c}" stroke-width="1.5" stroke-linecap="round">'
                       f'<circle cx="4" cy="3.5" r="1.8"/><circle cx="4" cy="12.5" r="1.8"/>'
                       f'<circle cx="12" cy="12.5" r="1.8"/><path d="M4 5.3v5.4M12 10.7V6.5a2 2 0 0 0-2-2H7.5"/>'
                       f'<path d="M9 3l-1.6 1.5L9 6"/></g>'),
    "commit": lambda c: (f'<g fill="none" stroke="{c}" stroke-width="1.5" stroke-linecap="round">'
                         f'<circle cx="8" cy="8" r="3"/><path d="M1 8h4M11 8h4"/></g>'),
    "repo": lambda c: (f'<g fill="none" stroke="{c}" stroke-width="1.5" stroke-linejoin="round">'
                       f'<path d="M3 13.5V3a1.5 1.5 0 0 1 1.5-1.5H13v10H4.5A1.5 1.5 0 0 0 3 13"/>'
                       f'<path d="M4.5 14.5H13M6 1.5v5l1.5-1 1.5 1v-5"/></g>'),
}


def icon(name: str, x, y, color: str) -> str:
    return f'<g transform="translate({x:g} {y:g})">{ICONS[name]("{" + color + "}")}</g>'


def render(d: dict, theme: str) -> str:
    cur, prev, daily = d["current"], d["previous"], d["daily"]
    out: list[str] = []
    y = PAD

    # Banner: total contributions + one square per day, shaded like GitHub's calendar.
    h = 52
    out.append(card(PAD, y, W - 2 * PAD, h))
    out.append(icon("calendar", 18, y + 18, "blue"))
    total = cur["contributions"]
    out.append(text(44, y + 31, f"{total:,} Contributions in the Last 30 Days", 16, "blue", 600))
    sq, step = 10, 13
    x0 = W - PAD - 18 - (DAYS - 1) * step - sq
    counts = [cell["count"] for cell in d["calendar"]]
    for i, (count, lvl) in enumerate(zip(counts, levels(counts))):
        day = date.fromisoformat(d["start"]) + timedelta(days=i)
        out.append(f'<rect x="{x0 + i * step}" y="{y + 21}" width="{sq}" height="{sq}" rx="2" '
                   f'fill="{{heat{lvl}}}"><title>{day:%b %d}: {count} contributions</title></rect>')
    y += h + GAP

    # Metric cards.
    metrics = [
        ("issues", "blue", f'{cur["issues"]:,} Issue{"" if cur["issues"] == 1 else "s"} Opened'),
        ("pulls", "purple", f'{cur["pulls"]:,} Pull Request{"" if cur["pulls"] == 1 else "s"} Opened'),
        ("commits", "orange", f'{cur["commits"]:,} Commit{"" if cur["commits"] == 1 else "s"}'),
    ]
    h = 72
    for n, (key, color, title) in enumerate(metrics):
        x = PAD + n * (COL + GAP)
        out.append(card(x, y, COL, h))
        out.append(text(x + 16, y + 28, title, 15, color, 600))
        delta, direction = change(cur[key], prev[key])
        out.append(text(x + 16, y + 55, delta, 12, "muted", mono=True))
        arrow = {"up": "↑", "down": "↓", "flat": "→"}[direction]
        tone = {"up": "up", "down": "down", "flat": "faint"}[direction]
        out.append(text(x + COL - 16, y + 55, "past month", 12, tone, mono=True, anchor="end"))
        out.append(text(x + COL - 98, y + 55, arrow, 13, tone, 600, anchor="middle"))
    y += h + GAP

    # Daily bar charts.
    charts = [
        ("issue", "Issues", [("Opened", "blue", daily["issues"]), ("Closed", "blue2", daily["closed"])], "blue"),
        ("pull", "Pull Requests", [("Opened", "purple", daily["pulls"]), ("Merged", "purple2", daily["merged"])], "purple"),
        ("commit", "Commits", [("Commits", "orange", daily["commits"])], "orange"),
    ]
    h = 150
    for n, (ico, label, series, color) in enumerate(charts):
        x = PAD + n * (COL + GAP)
        out.append(card(x, y, COL, h))
        out.append(icon(ico, x + 16, y + 20, color))
        out.append(text(x + 40, y + 33, label, 15, color, 600))
        for k, (name, c, _) in enumerate(series):
            ly = y + 24 + k * 18
            out.append(f'<circle cx="{x + COL - 76:g}" cy="{ly}" r="5" fill="{{{c}}}"/>')
            out.append(text(x + COL - 66, ly + 4.5, name, 12, "muted"))
        out.append(bars(x + 16, y + h - 16, COL - 32, 76, series))
    y += h + GAP

    # Most active public repositories, each with its own 30-day commit heatmap.
    h = 74
    out.append(card(PAD, y, W - 2 * PAD, h))
    out.append(icon("repo", 18, y + 21, "teal"))
    out.append(text(44, y + 34, "Most Active", 15, "teal", 600))
    out.append(text(44, y + 53, "Repositories", 15, "teal", 600))
    repos = d["repos"]
    if not repos:
        out.append(text(W / 2 + 60, y + 42, "No public commits in the last 30 days", 12, "faint", anchor="middle", mono=True))
    col_w, cx0 = 150, 176
    for n, repo in enumerate(repos):
        cx = cx0 + n * (col_w + 10)
        name = repo["name"].split("/", 1)[1] if repo["name"].startswith(d["user"] + "/") else repo["name"]
        name = name if len(name) <= 15 else name[:14] + "…"
        out.append(text(cx, y + 25, name, 12, "text", mono=True))
        out.append(text(cx + 141, y + 25, f'{repo["commits"]:,}', 12, "muted", anchor="end", mono=True))
        for i, lvl in enumerate(levels(repo["daily"])):
            out.append(f'<rect x="{cx + (i % 15) * 9.5:g}" y="{y + 36 + (i // 15) * 9.5:g}" width="7.5" '
                       f'height="7.5" rx="1.5" fill="{{heat{lvl}}}"/>')
    y += h

    out.append(text(W - PAD - 2, y + 17, f'{d["user"]} · last 30 days vs previous 30 · '
                    f'updated {d["generated"]} UTC', 10.5, "faint", anchor="end", mono=True))
    height = y + 24

    t = THEMES[theme]
    palette = {**{k: v for k, v in t.items() if k != "heat"}, **{f"heat{i}": c for i, c in enumerate(t["heat"])}}
    body = "\n".join(out)
    for key in sorted(palette, key=len, reverse=True):
        body = body.replace("{" + key + "}", palette[key])

    summary = (f'{d["user"]} GitHub activity for the 30 days to {d["generated"]}: '
               f'{total:,} contributions, {cur["commits"]:,} commits, {cur["pulls"]:,} pull requests opened, '
               f'{cur["issues"]:,} issues opened. Previous 30 days: {prev["contributions"]:,} contributions, '
               f'{prev["commits"]:,} commits, {prev["pulls"]:,} pull requests, {prev["issues"]:,} issues. '
               'Most active public repositories: '
               + (", ".join(f'{r["name"]} ({r["commits"]} commits)' for r in repos) or "none") + ".")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height:g}" '
            f'viewBox="0 0 {W} {height:g}" role="img" aria-labelledby="t d">\n'
            f'<title id="t">GitHub stats for {escape(d["user"])}</title>\n'
            f'<desc id="d">{escape(summary)}</desc>\n'
            f'<style>text{{font-family:{SANS}}}.m{{font-family:{MONO}}}</style>\n'
            f'<rect width="100%" height="100%" fill="{t["bg"]}"/>\n{body}\n</svg>\n')


def bars(x, base, width, height, series) -> str:
    """Grouped daily bars, one slot per day; series share a common y-scale."""
    peak = max((max(vals) for _, _, vals in series), default=0)
    slot = width / DAYS
    bw = min(5.0, (slot - 2) / len(series))
    parts = [f'<line x1="{x:g}" y1="{base:g}" x2="{x + width:g}" y2="{base:g}" stroke="{{track}}" stroke-width="1"/>']
    if peak == 0:
        parts.append(text(x + width / 2, base - height / 2 + 4, "No activity in the last 30 days", 11, "faint",
                          anchor="middle", mono=True))
        return "".join(parts)
    for i in range(DAYS):
        group = len(series) * bw
        left = x + i * slot + (slot - group) / 2
        for k, (name, color, vals) in enumerate(series):
            v = vals[i]
            if v == 0:
                continue
            bh = max(2.0, height * v / peak)
            parts.append(f'<rect x="{left + k * bw:.2f}" y="{base - bh:.2f}" width="{bw:.2f}" height="{bh:.2f}" '
                         f'rx="1" fill="{{{color}}}"><title>{escape(name)}: {v}</title></rect>')
    parts.append(text(x, base - height - 6, f"max {peak:,}/day", 10, "faint", mono=True))
    return "".join(parts)


# ---------------------------------------------------------------- main ----


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", default=os.getenv("GH_USERNAME", "4waiz"))
    ap.add_argument("--out", default="assets/stats")
    ap.add_argument("--from-json", help="render previously saved data instead of calling the API")
    ap.add_argument("--save-json", help="also write the fetched data to this path")
    args = ap.parse_args()

    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            raise SystemExit("Set GH_TOKEN (the workflow passes github.token).")
        data = fetch(args.user, token, datetime.now(timezone.utc).date())
        if args.save_json:
            Path(args.save_json).write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Daily aliases must add up to the 30-day collection; a mismatch means bad data.
    for key in ("commits", "pulls", "issues"):
        if sum(data["daily"][key]) != data["current"][key]:
            print(f"warning: daily {key} sum {sum(data['daily'][key])} != total {data['current'][key]}",
                  file=sys.stderr)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        svg = render(data, theme)
        ElementTree.fromstring(svg)  # fail loudly on malformed XML
        (out / f"stats-{theme}.svg").write_text(svg, encoding="utf-8", newline="\n")
    c, p = data["current"], data["previous"]
    print(f"{data['user']} {data['start']}..{data['generated']}: contributions {c['contributions']} "
          f"(prev {p['contributions']}), commits {c['commits']} (prev {p['commits']}), "
          f"PRs {c['pulls']} (prev {p['pulls']}), issues {c['issues']} (prev {p['issues']})")


if __name__ == "__main__":
    main()
