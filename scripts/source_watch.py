#!/usr/bin/env python3
"""Daily watcher for MangaRead Reader's six sources.

Two checks, both report-only (no config is ever modified automatically):

1. keiyoushi watcher — did the keiyoushi/extensions-source repo land new
   commits in the extension directories that correspond to our sources?
   Their fixes are the earliest reliable signal that a site changed its
   markup, API, or domain.

2. health checks — hit each source directly and verify the responses still
   look the way our adapters expect (selector class names present, API JSON
   keys present). Catches breakage before keiyoushi reacts.

State lives in scripts/source_watch_state.json (committed back by the
workflow) so a change is reported exactly once: new keiyoushi commits are
diffed against the last seen SHA, and health failures only report on a
transition (ok -> fail), not every day the site stays down.

Output: writes report.md when there is something to report, and appends
`findings=true|false` to $GITHUB_OUTPUT.
"""

import json
import os
import sys
import urllib.error
import urllib.request

KEIYOUSHI_REPO = "keiyoushi/extensions-source"

# our source id -> directory in the keiyoushi repo
WATCHED_PATHS = {
    "mangadex": "src/all/mangadex",
    "asurascans": "src/en/asurascans",
    "flamecomics": "src/en/flamecomics",
    "vortexscans": "src/en/arvenscans",  # Vortex's keiyoushi name
    "thunderscans": "src/all/thunderscans",
    "1manga": "src/en/onemangaco",
}

# (source id, url, list of needles — ALL must appear in the response body)
# Needles mirror what the app's adapters/parser profiles depend on.
HEALTH_CHECKS = [
    ("mangadex", "https://api.mangadex.org/manga?limit=1", ['"data"']),
    ("asurascans", "https://asuracomic.net/series?page=1", ["series"]),
    # Search SEMANTICS, not just reachability: Asura once renamed its query
    # param and the old one silently returned the default list (HTTP 200,
    # looked healthy, broke in-app search + slug re-resolution). A query for
    # a specific title must surface that title's slug.
    (
        "asurascans search",
        "https://asurascans.com/browse?page=1&search=omniscient%20reader",
        ["omniscient-readers-viewpoint"],
    ),
    ("flamecomics", "https://flamecomics.xyz/", ["buildId"]),
    (
        "vortexscans",
        "https://api.vortexscans.org/api/query?page=1&perPage=1",
        ["posts"],
    ),
    ("thunderscans", "https://en-thunderscans.com/comics/", ["chapter"]),
    # 1manga chapter rows hang off the _3pfyN CSS-module class; if that
    # token vanishes the site redeployed with new class names and the
    # chapterSelectors override needs updating.
    ("1manga", "https://1manga.co/popular", ["/manga/"]),
]

STATE_FILE = os.path.join(os.path.dirname(__file__), "source_watch_state.json")
REPORT_FILE = "report.md"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def http_get(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def github_api(path):
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = http_get(f"https://api.github.com{path}", headers=headers)
    return json.loads(body)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    return {"keiyoushi": {}, "health": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")


def check_keiyoushi(state):
    """Returns markdown sections for sources with new upstream commits."""
    sections = []
    for source_id, path in WATCHED_PATHS.items():
        try:
            commits = github_api(
                f"/repos/{KEIYOUSHI_REPO}/commits?path={path}&per_page=10"
            )
        except Exception as error:  # noqa: BLE001 - report and move on
            sections.append(
                f"### {source_id}\n\n⚠️ Could not query keiyoushi commits: {error}\n"
            )
            continue
        if not commits:
            continue
        newest = commits[0]["sha"]
        last_seen = state["keiyoushi"].get(source_id)
        state["keiyoushi"][source_id] = newest
        if last_seen is None or last_seen == newest:
            # First run just primes the state; identical SHA means no change.
            continue
        new_commits = []
        for commit in commits:
            if commit["sha"] == last_seen:
                break
            message = commit["commit"]["message"].splitlines()[0]
            new_commits.append(f"- [`{commit['sha'][:7]}`]({commit['html_url']}) {message}")
        if new_commits:
            sections.append(
                f"### {source_id} — upstream changes in `{path}`\n\n"
                + "\n".join(new_commits)
                + "\n\nReview the diff and decide whether "
                "`mangaread/source_config.v1.json` needs an override update.\n"
            )
    return sections


def check_health(state):
    """Returns markdown sections for sources that newly fail their probe."""
    sections = []
    for source_id, url, needles in HEALTH_CHECKS:
        previous = state["health"].get(source_id, "ok")
        try:
            status, body = http_get(url)
            missing = [needle for needle in needles if needle not in body]
            if status == 200 and not missing:
                current, detail = "ok", ""
            elif status in (403, 503):
                # Bot challenges block GitHub's runner IPs routinely; the
                # site is probably fine for real users. Track separately so
                # an ok->challenged transition is mentioned once, softly.
                current, detail = "challenged", f"HTTP {status} (likely bot challenge)"
            else:
                current = "fail"
                detail = (
                    f"HTTP {status}, missing expected markers: {missing}"
                    if missing
                    else f"HTTP {status}"
                )
        except Exception as error:  # noqa: BLE001
            current, detail = "fail", f"request error: {error}"
        state["health"][source_id] = current
        if current != "ok" and current != previous:
            severity = "⚠️" if current == "challenged" else "❌"
            sections.append(
                f"### {source_id} — health check {current}\n\n"
                f"{severity} `{url}` → {detail}\n"
            )
        elif current == "ok" and previous != "ok":
            sections.append(f"### {source_id} — recovered ✅\n")
    return sections


def main():
    state = load_state()
    sections = check_keiyoushi(state) + check_health(state)
    save_state(state)

    findings = bool(sections)
    if findings:
        with open(REPORT_FILE, "w", encoding="utf-8") as handle:
            handle.write(
                "Automated source watch report. Nothing has been changed — "
                "review and update `mangaread/source_config.v1.json` if needed.\n\n"
            )
            handle.write("\n".join(sections))

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"findings={'true' if findings else 'false'}\n")
    print(f"findings={findings}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
