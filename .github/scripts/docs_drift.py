#!/usr/bin/env python3
"""Docs drift check.

Asks PipesHub, which indexes both this code repository (with its code graph)
and the documentation repository, which documentation a change makes wrong.
On a pull request it comments with what drifted and opens a documentation
pull request with the fix. With SWEEP_DAYS set it checks the docs against
everything merged in that window instead.

Standard library only, so it runs on a bare runner.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

PSH = os.environ.get("PIPESHUB_URL", "http://localhost:3002").rstrip("/")
PSH_APP = os.environ.get("PIPESHUB_APP_URL", PSH).rstrip("/")
REPO = os.environ["GITHUB_REPOSITORY"]
DOCS_REPO = os.environ["DOCS_REPO"]
PR = os.environ.get("PR_NUMBER") or ""
BASE = os.environ.get("BASE_REF") or "main"
SWEEP_DAYS = os.environ.get("SWEEP_DAYS") or ""
MARKER = "<!-- pipeshub-docs-drift -->"
MAX_DIFF_CHARS = 60_000
# Tests, fixtures, snapshots, lockfiles and non-English strings say nothing a reader sees.
NOT_READER_FACING = re.compile(
    r"(^|/)(tests?|__tests__|fixtures?|integration-tests)/|\.test\.|_test\.py$|(^|/)test_[^/]+\.py$"
    r"|/locales/(?!en-US)|package-lock\.json$|\.lock$|\.snap$|\.arazzo\.ya?ml$"
)


def run(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def github(method: str, path: str, body: dict | None = None, token: str | None = None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token or os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pipeshub-docs-drift",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def ask_pipeshub(query: str) -> tuple[dict, str | None]:
    """Runs one agent turn and returns (final snapshot, conversation id)."""
    req = urllib.request.Request(
        PSH + "/api/v1/conversations/stream",
        data=json.dumps({"query": query, "chatMode": "agent"}).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["PIPESHUB_TOKEN"],
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    final: dict = {}
    conversation = None
    event = None
    with urllib.request.urlopen(req, timeout=1800) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:])
            except ValueError:
                continue
            value = data.get("value") if isinstance(data.get("value"), dict) else {}
            if event == "CUSTOM" and value.get("conversationId"):
                conversation = value["conversationId"]
            elif event == "TOOL_CALL_START":
                print(f"  PipesHub tool: {data.get('toolCallName')}", flush=True)
            elif event == "STATE_SNAPSHOT" and data.get("snapshot", {}).get("final"):
                final = data["snapshot"]
            elif event == "RUN_FINISHED":
                conversation = conversation or data.get("result", {}).get("conversation", {}).get("_id")
    return final, conversation


def pr_change() -> tuple[list[str], str]:
    run("git", "fetch", "--no-tags", "--quiet", "origin", BASE)
    files = [f for f in run("git", "diff", "--name-only", f"origin/{BASE}...HEAD").split("\n") if f]
    shown = [f for f in files if not NOT_READER_FACING.search(f)]
    diff = run("git", "diff", "-U3", f"origin/{BASE}...HEAD", "--", *shown) if shown else ""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n[diff cut here]\n"
    return files, diff


EDIT_RULES = f"""For each drift, give a minimal edit to the documentation page:
- "path": the page's path in {DOCS_REPO}, for example developer/personal-access-tokens.mdx
- "find": text copied exactly from the page as it is now (read the page first), one or more whole lines, unique in the page
- "replace": the corrected text in the page's own voice and format; to add a table row or a paragraph, include the neighbouring line from "find" plus the new text
- "what": a few words naming what is out of date, for a table cell
- "why": one sentence naming what changed in the code
- "code": the code file (and symbol, if useful) that shows the change

Reply with two or three sentences for reviewers, then the edits in a ```json fenced block: {{"drifts": [...]}}.
Return {{"drifts": []}} when nothing a reader sees has drifted. Do not invent drift."""


def pr_prompt(files: list[str], diff: str) -> str:
    title = os.environ.get("PR_TITLE", "")
    body = (os.environ.get("PR_BODY") or "").strip()[:3000]
    return f"""You are the documentation drift check for pull request #{PR} in {REPO}: "{title}".
The code is the indexed repository {REPO} (with its code graph). The product documentation is the indexed repository {DOCS_REPO} (MDX pages).

Find every statement in the documentation that this change makes wrong or incomplete for a reader: UI labels and steps, API endpoints and fields, defaults, limits, environment variables, permissions. Use the diff below, the code graph and a search of the documentation, and read each page you edit. Ignore refactors and tests that change nothing a reader sees.

{EDIT_RULES}

Pull request description:
{body or "(none)"}

Changed files ({len(files)}):
{chr(10).join(files[:120])}

Diff, tests and translations left out:
```diff
{diff}
```"""


def sweep_prompt(days: str) -> str:
    log = run("git", "log", f"--since={days} days ago", "--no-merges", "--pretty=%h %s", "origin/" + BASE)
    feats = [l for l in log.split("\n") if re.match(r"^\w+ (feat|fix)(\(|:)", l)][:150]
    return f"""You are the nightly documentation drift sweep for {REPO}.
The code is the indexed repository {REPO} (with its code graph). The product documentation is the indexed repository {DOCS_REPO} (MDX pages).

These commits were merged in the last {days} days. Check which of them change something a reader of the documentation sees (UI labels and steps, API endpoints, defaults, limits, environment variables, integrations listed in the docs) and find the pages that are now wrong or missing it. Read the code and the pages before you edit.

{EDIT_RULES}

Commits:
{chr(10).join(feats)}"""


def parse_drifts(answer: str) -> list[dict]:
    # The edits can hold fenced code themselves, so read from the json fence to the last fence.
    start = answer.find("```json")
    if start < 0:
        return []
    body = answer[start + len("```json"):]
    end = body.rfind("```")
    for candidate in (body[:end] if end >= 0 else body, body):
        try:
            drifts = json.loads(candidate[candidate.find("{"):candidate.rfind("}") + 1]).get("drifts", [])
            return [d for d in drifts if d.get("path") and d.get("find") and d.get("replace")]
        except ValueError:
            continue
    return []


def summary_of(answer: str) -> str:
    start = answer.find("```json")
    return (answer[:start] if start >= 0 else answer).strip()


def locate(text: str, find: str) -> tuple[int, int] | None:
    """Exact match first, then one that ignores runs of whitespace."""
    i = text.find(find)
    if i >= 0:
        return i, i + len(find)
    parts = [re.escape(p) for p in find.split()]
    m = re.search(r"\s+".join(parts), text) if parts else None
    return (m.start(), m.end()) if m else None


def patch_lines(text: str, find: str, replace: str) -> tuple[str, int] | None:
    """Falls back to the changed lines alone, for a page whose markup reads
    differently from the text PipesHub quoted (an MDX component around a
    heading, say). Every changed line must be found once in the page."""
    old, new = find.split("\n"), replace.split("\n")
    lines = text.split("\n")
    stripped = [l.strip() for l in lines]
    edits: list[tuple[int, int, list[str]]] = []  # (start, end) in page lines -> new lines
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            spans = [stripped.index(o.strip()) if stripped.count(o.strip()) == 1 else -1 for o in old[i1:i2] if o.strip()]
            if not spans or -1 in spans or spans != list(range(spans[0], spans[0] + len(spans))):
                return None
            edits.append((spans[0], spans[-1] + 1, new[j1:j2]))
        else:  # insert after the line before it
            anchor = next((old[k].strip() for k in range(i1 - 1, -1, -1) if old[k].strip()), None)
            if anchor is None or stripped.count(anchor) != 1:
                return None
            at = stripped.index(anchor) + 1
            edits.append((at, at, new[j1:j2]))
    if not edits:
        return None
    first = min(e[0] for e in edits)
    for start, end, repl in sorted(edits, reverse=True):
        lines[start:end] = repl
    return "\n".join(lines), first + 1


def open_docs_pr(drifts: list[dict], title: str, body: str, branch: str) -> tuple[str | None, list[dict]]:
    token = os.environ["DOCS_REPO_TOKEN"]
    work = tempfile.mkdtemp(prefix="docs-")
    url = f"https://x-access-token:{token}@github.com/{DOCS_REPO}.git"
    run("git", "clone", "--quiet", "--depth", "1", url, work)
    run("git", "checkout", "--quiet", "-b", branch, cwd=work)
    applied = []
    for d in drifts:
        path = os.path.join(work, d["path"].lstrip("/"))
        if not os.path.isfile(path):
            print(f"  skip {d['path']}: no such page")
            continue
        text = open(path, encoding="utf-8").read()
        # Exact text first, then only the changed lines (keeps the page's own
        # blank lines and markup), then a match that ignores whitespace.
        exact = text.find(d["find"])
        patched = None if exact >= 0 else patch_lines(text, d["find"], d["replace"])
        span = (exact, exact + len(d["find"])) if exact >= 0 else (None if patched else locate(text, d["find"]))
        if span is not None:
            d["line"] = text.count("\n", 0, span[0]) + 1
            text = text[: span[0]] + d["replace"] + text[span[1]:]
        elif patched is not None:
            text, d["line"] = patched
        else:
            print(f"  skip {d['path']}: text to replace not found")
            continue
        open(path, "w", encoding="utf-8").write(text)
        applied.append(d)
    if not applied:
        return None, []
    run("git", "-c", "user.name=PipesHub docs drift", "-c", "user.email=docs-drift@users.noreply.github.com",
        "commit", "--quiet", "-am", title, cwd=work)
    run("git", "push", "--quiet", "--force", "origin", branch, cwd=work)
    owner = DOCS_REPO.split("/")[0]
    existing = github("GET", f"/repos/{DOCS_REPO}/pulls?state=open&head={owner}:{branch}", token=token)
    if existing:
        github("PATCH", f"/repos/{DOCS_REPO}/pulls/{existing[0]['number']}", {"body": body}, token=token)
        return existing[0]["html_url"], applied
    pr = github("POST", f"/repos/{DOCS_REPO}/pulls",
                {"title": title, "head": branch, "base": "main", "body": body}, token=token)
    return pr["html_url"], applied


def cell(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")


def table(drifts: list[dict], docs_sha_ref: str = "main") -> str:
    rows = ["| Page | Out of date | Changed in code |", "|---|---|---|"]
    for d in drifts:
        anchor = f"#L{d['line']}" if d.get("line") else ""
        link = f"https://github.com/{DOCS_REPO}/blob/{docs_sha_ref}/{d['path']}{anchor}"
        rows.append(f"| [`{d['path']}`]({link}) | {cell(d.get('what') or d.get('why', ''))} | `{cell(d.get('code', ''))}` |")
    return "\n".join(rows)


def sources(final: dict) -> str:
    seen, out = set(), []
    for c in final.get("citations") or []:
        name = c.get("recordName") or c.get("title")
        url = c.get("webUrl")
        if name and name not in seen:
            seen.add(name)
            out.append(f"[{name}]({url})" if url else name)
    return " · ".join(out[:8])


def comment_on_pr(body: str) -> None:
    comments = github("GET", f"/repos/{REPO}/issues/{PR}/comments?per_page=100")
    mine = [c for c in comments if MARKER in (c.get("body") or "")]
    if mine:
        github("PATCH", f"/repos/{REPO}/issues/comments/{mine[-1]['id']}", {"body": body})
    else:
        github("POST", f"/repos/{REPO}/issues/{PR}/comments", {"body": body})


def main() -> int:
    if SWEEP_DAYS:
        print(f"Sweep: docs against the last {SWEEP_DAYS} days of {BASE}")
        query = sweep_prompt(SWEEP_DAYS)
    else:
        files, diff = pr_change()
        print(f"PR #{PR}: {len(files)} files changed; asking PipesHub")
        query = pr_prompt(files, diff)

    final, conversation = ask_pipeshub(query)
    answer = final.get("answer") or ""
    # A clean "no drift" still carries the json block; anything else is a failed check,
    # never a pass (a model outage must not read as "docs are fine").
    if not answer or final.get("errorCode") or "```json" not in answer:
        print("PipesHub could not complete the check:", (answer or "no answer")[:300], file=sys.stderr)
        return 2
    drifts = parse_drifts(answer)
    chat_link = f"{PSH_APP}/chat/?conversationId={conversation}" if conversation else PSH_APP
    print(summary_of(answer))
    print(f"{len(drifts)} drift(s) proposed")

    if not drifts:
        if PR:
            comment_on_pr(f"{MARKER}\n### ✅ Docs drift: none found\n\nPipesHub checked this change against "
                          f"`{DOCS_REPO}` and found nothing a reader sees that is now out of date. "
                          f"[Open the check in PipesHub]({chat_link})")
        return 0

    if SWEEP_DAYS:
        branch = "docs-drift/sweep"
        title = f"docs: catch up with the last {SWEEP_DAYS} days of {REPO.split('/')[1]}"
        origin = f"the changes merged into `{REPO}` in the last {SWEEP_DAYS} days"
    else:
        branch = f"docs-drift/{REPO.split('/')[1]}-pr-{PR}"
        title = f"docs: update for {REPO}#{PR}"
        origin = f"{REPO}#{PR}"
    docs_body = (f"PipesHub found documentation that {origin} makes out of date, and proposes these edits.\n\n"
                 f"{table(drifts)}\n\n{summary_of(answer)}\n\n"
                 f"Each edit cites the code that changed. Review the wording, then merge.\n\n"
                 f"_Opened by the Docs drift check · [PipesHub answer]({chat_link})_")
    docs_pr, applied = open_docs_pr(drifts, title, docs_body, branch)
    if not applied:
        print("No proposed edit matched the current pages", file=sys.stderr)
        return 2
    print(f"Docs PR: {docs_pr}")

    if PR:
        n = len({d["path"] for d in applied})
        src = sources(final)
        pages = f"{n} documentation page{'s' if n != 1 else ''}"
        comment_on_pr(
            f"{MARKER}\n### 📄 Docs drift: {len(applied)} statement{'s' if len(applied) != 1 else ''} in {pages} now out of date\n\n"
            f"PipesHub read this change against the code graph of `{REPO}` and the docs in `{DOCS_REPO}`.\n\n"
            f"{table(applied, branch)}\n\n"
            f"**Fix ready:** {docs_pr}\n\n"
            f"<details><summary>PipesHub's summary</summary>\n\n{summary_of(answer)}\n\n</details>\n\n"
            + (f"Sources: {src}\n\n" if src else "")
            + f"[Open the check in PipesHub]({chat_link})"
        )
    # Fail the check until the docs catch up, so the drift is seen before merge.
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} from {e.url.split('?')[0]}", file=sys.stderr)
        sys.exit(2)
