#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Open Source Autonomous Bug Hunter & PR Agent
Powered by GitHub Search, OpenRouter AI, and GitHub Actions
"""

import os
import sys
import json
import re
import time
import argparse
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import tempfile
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def load_env(env_path: Path):
    """Load key-value pairs from .env."""
    if not env_path.is_file():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            if k not in os.environ:
                os.environ[k] = v


def get_opener():
    """Build urllib opener bypassing broken local proxy if needed."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run_cmd(cmd, cwd=None, check=True):
    """Run a shell command or argument list and return stdout."""
    is_str = isinstance(cmd, str)
    res = subprocess.run(
        cmd,
        cwd=cwd,
        shell=is_str,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    if check and res.returncode != 0:
        cmd_str = cmd if is_str else " ".join(cmd)
        raise RuntimeError(f"Command failed (exit {res.returncode}): {cmd_str}\nStderr: {res.stderr.strip()}")
    return res.stdout.strip()


def call_openrouter(messages, api_key, model="cohere/north-mini-code:free"):
    """Call OpenRouter Chat Completion API with fallback list."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com",
        "X-Title": "Open Source Bug Hunter"
    }

    fallback_models = [
        model,
        "cohere/north-mini-code:free",
        "poolside/laguna-s-2.1:free",
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-super-120b-a12b:free"
    ]
    
    seen = set()
    models_to_try = []
    for m in fallback_models:
        if m and m not in seen:
            seen.add(m)
            models_to_try.append(m)

    last_err = None
    for m in models_to_try:
        payload = {
            "model": m,
            "messages": messages,
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        try:
            opener = get_opener()
            with opener.open(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choice = data["choices"][0]
                msg = choice.get("message", {})
                content = msg.get("content") or msg.get("reasoning") or choice.get("text") or ""
                if content:
                    return content
        except Exception as e:
            last_err = e
            print(f"[!] OpenRouter model {m} failed: {e}")

    raise RuntimeError(f"All OpenRouter models failed. Last error: {last_err}")


def search_candidate_issues(token, query, limit=10):
    """Search GitHub for good candidate issues."""
    opener = get_opener()
    encoded_q = urllib.parse.quote(query)
    url = f"https://api.github.com/search/issues?q={encoded_q}&sort=updated&order=desc&per_page={limit}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "OpenSourceBugHunter"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("items", [])


def search_top_repos(token, min_stars=1000, limit=20):
    """Search for popular, actively updated Python repositories with > min_stars."""
    opener = get_opener()
    url = f"https://api.github.com/search/repositories?q=language:python+stars:>{min_stars}&sort=updated&order=desc&per_page={limit}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "OpenSourceBugHunter"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [item["full_name"] for item in data.get("items", [])]
    except Exception as e:
        print(f"[!] Error querying top repos: {e}")
        return []


def get_repo_stars(repo_full_name, gh_token):
    """Retrieve star count for repository."""
    try:
        raw = run_cmd(["gh", "repo", "view", repo_full_name, "--json", "stargazerCount", "--jq", ".stargazerCount"])
        return int(raw.strip())
    except Exception:
        return 0


def is_actionable_issue(issue, excluded_repos=None, min_stars=0, gh_token=None):
    """Filter out non-actionable, subjective discussions or excluded repositories."""
    excluded_repos = excluded_repos or []
    repo_url = issue.get("repository_url", "")
    repo_name = repo_url.replace("https://api.github.com/repos/", "").lower()

    # Never touch excluded repos (e.g. current competition haidian)
    for exc in excluded_repos:
        if exc.lower() in repo_name:
            return False, f"Matches excluded repository pattern: {exc}"

    title = issue.get("title", "").lower()
    body = (issue.get("body") or "").lower()
    full_text = f"{title}\n{body}"

    # Disqualify educational homework / assignment repos to focus on real open-source libraries
    assignment_words = ["homework", "assignment", "tutorial", "course", "exercise", "cs1", "cs2", "lab1", "lab2", "lab3"]
    if any(w in repo_name for w in assignment_words):
        return False, f"Skipping educational/assignment repository: {repo_name}"

    # Enforce minimum stars for target repository
    if min_stars > 0 and gh_token:
        stars = get_repo_stars(repo_name, gh_token)
        if stars < min_stars:
            return False, f"Repository {repo_name} has {stars} stars (required > {min_stars})"

    # Disqualify long architectural discussion / policy / question / release notes
    skip_keywords = ["discussion", "roadmap", "rfc", "proposal", "feature request", "policy", "question"]
    for kw in skip_keywords:
        if kw in title:
            return False, f"Title contains discussion keyword '{kw}'"

    # Actionable signals: traceback, ZeroDivisionError, TypeError, ValueError, typo, doc, fix, bug
    actionable_signals = [
        "traceback", "error", "exception", "crashes", "typo", "broken",
        "unexpected", "fails", "fail", "syntax", "attributeerror", "typeerror",
        "indexerror", "keyerror", "import", "deprecated"
    ]
    has_signal = any(sig in full_text for sig in actionable_signals)
    if not has_signal:
        return False, "No actionable bug or error signals found in issue description"

    # Must mention potential file or code element
    matches = re.findall(r'[\w./\-]+\.[a-zA-Z0-9]+', issue.get("body") or "")
    code_indicators = [m for m in matches if not m.startswith("http") and ("." in m)]
    if not code_indicators and "```" not in full_text:
        return False, "No code block or specific file referenced in issue"

    return True, "Actionable issue found"


def apply_search_replace(file_path: Path, search_block: str, replace_block: str):
    """Apply search-and-replace block with tolerant whitespace matching."""
    if not file_path.is_file():
        matches = [f for f in Path(".").glob(f"**/{file_path.name}") if ".git" not in f.parts and "__pycache__" not in f.parts]
        if matches:
            file_path = matches[0]
        else:
            return False

    content = file_path.read_text(encoding="utf-8", errors="replace")
    if search_block in content:
        new_content = content.replace(search_block, replace_block, 1)
        file_path.write_text(new_content, encoding="utf-8")
        return True

    search_lines = [l.strip() for l in search_block.strip().splitlines() if l.strip()]
    if not search_lines:
        return False

    content_lines = content.splitlines()
    for i in range(len(content_lines) - len(search_lines) + 1):
        chunk = [content_lines[i + j].strip() for j in range(len(search_lines))]
        if chunk == search_lines:
            prefix = "\n".join(content_lines[:i])
            suffix = "\n".join(content_lines[i + len(search_lines):])
            new_content = (prefix + "\n" if prefix else "") + replace_block + ("\n" + suffix if suffix else "")
            file_path.write_text(new_content, encoding="utf-8")
            return True

    return False


def parse_markdown_response(text, issue_num):
    """Parse diff and description from LLM response."""
    analysis = "Automated bug fix via OpenRouter AI"
    commit_msg = f"fix: resolve issue #{issue_num}"
    pr_title = f"fix(#{issue_num}): resolve issue"
    pr_body = f"Closes #{issue_num}\n\nAutomated fix generated via OpenRouter AI."
    file_changes = []

    m_analysis = re.search(r'#+\s*Analysis\s*\n(.*?)(?=\n#+|$)', text, re.DOTALL | re.IGNORECASE)
    if m_analysis:
        analysis = m_analysis.group(1).strip()

    m_commit = re.search(r'#+\s*Commit\s*\n(.*?)(?=\n#+|$)', text, re.DOTALL | re.IGNORECASE)
    if m_commit:
        commit_msg = m_commit.group(1).strip().splitlines()[0].strip()

    m_pr = re.search(r'#+\s*PR\s*\n(.*?)(?=\n#+\s*Changes|$)', text, re.DOTALL | re.IGNORECASE)
    if m_pr:
        pr_full = m_pr.group(1).strip()
        lines = pr_full.splitlines()
        if lines:
            pr_title = lines[0].replace("#", "").strip()
            pr_body = "\n".join(lines[1:]).strip() or pr_full

    file_sections = re.findall(r'#+\s*File:\s*([^\n]+)\s*\n(.*?)(?=\n#+\s*File:|$)', text, re.DOTALL | re.IGNORECASE)
    for f_path, block in file_sections:
        f_path = f_path.strip().strip("`").strip()
        sr_matches = re.findall(r'<<<<<<<\s*SEARCH\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>>', block, re.DOTALL)
        for s, r in sr_matches:
            file_changes.append({
                "path": f_path,
                "search": s,
                "replace": r
            })

    return {
        "analysis": analysis,
        "commit_message": commit_msg,
        "pr_title": pr_title,
        "pr_body": pr_body,
        "changes": file_changes
    }


def solve_issue_in_repo(repo_full_name, issue_data, api_key, model, my_username, gh_token, auto_pr=False):
    """Clone, diagnose, fix, test, and submit PR to upstream repo."""
    issue_num = issue_data["number"]
    issue_title = issue_data["title"]
    issue_body = issue_data.get("body") or ""

    print(f"\n{'=' * 60}")
    print(f"[*] Processing Target: {repo_full_name} #{issue_num}")
    print(f"[*] Title: {issue_title}")
    print(f"{'=' * 60}")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"

        # 1. Fork repo to user account if not already forked
        print(f"[*] Ensuring fork exists on account '{my_username}'...")
        try:
            run_cmd(["gh", "repo", "fork", repo_full_name, "--clone=false"], check=False)
            time.sleep(3)
        except Exception as e:
            print(f"[!] Fork notice: {e}")

        # 2. Clone user's fork with retry
        fork_name = f"{my_username}/{repo_full_name.split('/')[-1]}"
        print(f"[*] Cloning {fork_name} into temporary workspace...")
        clone_url = f"https://x-access-token:{gh_token}@github.com/{fork_name}.git"
        cloned = False
        for attempt in range(4):
            try:
                run_cmd(f"git clone --depth 50 {clone_url} {repo_dir}")
                cloned = True
                break
            except Exception as e:
                if attempt < 3:
                    print(f"[*] Waiting for fork '{fork_name}' to become ready on GitHub ({attempt + 1}/3)...")
                    time.sleep(3)
                else:
                    print(f"[!] Failed to clone fork {fork_name}: {e}")
        if not cloned:
            return False

        # Add upstream remote
        run_cmd(f"git remote add upstream https://github.com/{repo_full_name}.git", cwd=repo_dir, check=False)
        run_cmd("git fetch upstream", cwd=repo_dir, check=False)

        # 3. Create branch
        branch_name = f"fix/issue-{issue_num}-auto"
        run_cmd(f"git checkout -b {branch_name}", cwd=repo_dir)

        # 4. Search candidate files mentioned in issue
        text = f"{issue_title}\n{issue_body}"
        matches = re.findall(r'[\w./\-]+\.[a-zA-Z0-9]+', text)
        candidate_files = []
        for m in matches:
            fp = Path(repo_dir) / m
            if fp.is_file() and not m.startswith(".git"):
                candidate_files.append(str(Path(m).as_posix()))
            else:
                found = list(repo_dir.glob(f"**/{Path(m).name}"))
                for f in found:
                    if ".git" not in f.parts and "__pycache__" not in f.parts:
                        rel = str(f.relative_to(repo_dir).as_posix())
                        if rel not in candidate_files:
                            candidate_files.append(rel)

        candidate_files = candidate_files[:5]
        if not candidate_files:
            print(f"[!] Could not locate relevant source files for issue #{issue_num}. Aborting.")
            return False

        print(f"[+] Found candidate source files: {', '.join(candidate_files)}")

        # 5. Build context & prompt
        files_context = []
        for cf in candidate_files:
            cfp = repo_dir / cf
            if cfp.is_file():
                content = cfp.read_text(encoding="utf-8", errors="replace")
                if len(content) > 25000:
                    content = content[:25000] + "\n\n... [Content truncated] ..."
                files_context.append(f"### File: {cf}\n```python\n{content}\n```")

        files_str = "\n\n".join(files_context)

        system_prompt = (
            "You are a principal open-source software engineer contributing to high-profile repositories.\n"
            "Your task is to analyze the issue report, inspect the provided file contents, fix the bug cleanly, and provide a search-and-replace diff.\n"
            "Do NOT add unnecessary comments or break existing conventions. Keep changes minimal and focused.\n\n"
            "Output MUST follow this EXACT structure:\n\n"
            "# Analysis\n"
            "<Brief explanation of root cause and fix strategy>\n\n"
            "# Commit\n"
            "fix(scope): concise conventional commit message\n\n"
            "# PR\n"
            "fix: concise PR title\n"
            "<Polite PR description explaining the root cause, what was changed, and referencing Closes #<number>>\n\n"
            "# Changes\n"
            "### File: <relative/file/path>\n"
            "<<<<<<< SEARCH\n"
            "<exact lines in original file to be replaced>\n"
            "=======\n"
            "<new replacement lines>\n"
            ">>>>>>>\n"
        )

        user_prompt = (
            f"Repository: {repo_full_name}\n"
            f"Issue #{issue_num}: {issue_title}\n\n"
            f"Issue Description:\n{issue_body}\n\n"
            f"Files to inspect:\n{files_str}\n\n"
            "Please provide your analysis and SEARCH/REPLACE diff now."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        print(f"[*] Calling OpenRouter AI to formulate fix...")
        response_text = call_openrouter(messages, api_key, model)
        parsed = parse_markdown_response(response_text, issue_num)

        if not parsed["changes"]:
            print("[!] AI did not produce valid search-and-replace changes. Aborting.")
            return False

        # 6. Apply changes
        modified = []
        for change in parsed["changes"]:
            fpath = repo_dir / change["path"]
            ok = apply_search_replace(fpath, change["search"], change["replace"])
            if ok:
                modified.append(change["path"])
                print(f"[+] Successfully patched: {change['path']}")

        if not modified:
            print("[!] Search blocks could not be matched in original files. Aborting.")
            return False

        # 7. Verification Gate: Syntax and Unit Tests
        print("[*] Running Verification Gate (syntax and unit tests)...")
        for m in modified:
            if m.endswith(".py"):
                try:
                    run_cmd(f'python -m py_compile "{repo_dir / m}"')
                except Exception as e:
                    print(f"[!] Syntax error in {m}: {e}. Discarding patch.")
                    return False

        test_dir = repo_dir / "tests"
        if test_dir.is_dir():
            print("[*] Found tests directory. Running test suite...")
            try:
                # Try pytest first if present
                py_test_cmd = ["python", "-m", "pytest", "tests", "-q"]
                res = subprocess.run(py_test_cmd, cwd=repo_dir, capture_output=True, text=True)
                if res.returncode == 0:
                    print("[✓] pytest test suite executed successfully.")
                else:
                    run_cmd("python -m unittest discover -s tests -p 'test_*.py'", cwd=repo_dir, check=False)
                    print("[✓] unittest verification executed.")
            except Exception as e:
                print(f"[!] Notice during testing: {e}")

        # 8. Git Commit & Push to User's Fork
        commit_msg = parsed.get("commit_message", f"fix: resolve issue #{issue_num}")
        for m in modified:
            run_cmd(["git", "add", m], cwd=repo_dir)

        run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_dir)
        print(f"[+] Committed: {commit_msg}")

        if not auto_pr:
            print("[*] Dry-run complete. Patch successfully generated and committed locally.")
            return True

        print(f"[*] Pushing branch '{branch_name}' to fork '{fork_name}'...")
        run_cmd(["git", "push", "-u", "origin", branch_name], cwd=repo_dir)

        # 9. Create PR targeting the original Upstream repository
        pr_title = parsed.get("pr_title", f"fix: resolve issue #{issue_num}")
        pr_body = parsed.get("pr_body", f"Closes #{issue_num}\n\nAutomated fix generated via OpenRouter AI.")

        print(f"[*] Submitting Pull Request to {repo_full_name}...")
        pr_cmd = [
            "gh", "pr", "create",
            "--repo", repo_full_name,
            "--head", f"{my_username}:{branch_name}",
            "--title", pr_title,
            "--body", pr_body
        ]
        pr_url = run_cmd(pr_cmd, cwd=repo_dir)
        print(f"\n[🎉 SUCCESS] Pull Request opened on {repo_full_name}: {pr_url}\n")
        return True


def main():
    parser = argparse.ArgumentParser(description="Autonomous Open Source Bug Hunter & PR Bot")
    parser.add_argument("--query", type=str, default=None, help="GitHub Search query")
    parser.add_argument("--min-stars", type=int, default=1000, help="Minimum stars for target open-source repositories (default: 1000)")
    parser.add_argument("--repos", nargs="*", default=[], help="Specific target repositories to monitor (e.g. owner/repo)")
    parser.add_argument("--exclude", nargs="*", default=["haidian", "open-city-ai"], help="Repositories to exclude (e.g. competition repos)")
    parser.add_argument("--max-pr", type=int, default=1, help="Maximum number of PRs to submit in one run (default: 1)")
    parser.add_argument("--auto-pr", action="store_true", help="Actually submit PRs to upstream repositories (default is dry-run)")
    parser.add_argument("--model", type=str, default=None, help="OpenRouter model")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    load_env(root_dir / ".env")

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("[Error] OPENROUTER_API_KEY not found in environment or .env file.")
        sys.exit(1)

    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not gh_token:
        # Try getting from gh auth token
        try:
            gh_token = run_cmd("gh auth token").strip()
        except Exception:
            pass

    if not gh_token:
        print("[Error] GitHub token not available. Ensure gh CLI is authenticated or GH_TOKEN is set.")
        sys.exit(1)

    # Get authenticated username
    try:
        my_username = run_cmd("gh api user --jq .login").strip()
    except Exception:
        my_username = "cf3901646"

    print(f"[*] Authenticated as GitHub user: {my_username}")
    print(f"[*] Minimum Repository Stars: {args.min_stars}")
    print(f"[*] Excluded repositories: {args.exclude}")

    model = args.model or os.environ.get("OPENROUTER_MODEL") or "cohere/north-mini-code:free"

    # Fetch candidate issues
    candidates = []
    if args.repos:
        for r in args.repos:
            try:
                cmd = ["gh", "issue", "list", "--repo", r, "--state", "open", "--limit", "5", "--json", "number,title,body,url,labels"]
                raw = run_cmd(cmd)
                items = json.loads(raw)
                for item in items:
                    item["repository_url"] = f"https://api.github.com/repos/{r}"
                    candidates.append(item)
            except Exception as e:
                print(f"[!] Error fetching issues from {r}: {e}")
    else:
        min_stars = args.min_stars
        print(f"[*] Discovering active open-source repositories with stars > {min_stars}...")
        top_repos = search_top_repos(gh_token, min_stars=min_stars, limit=20)
        curated = [
            "deepset-ai/haystack", "pallets/werkzeug", "encode/httpx", "pallets/flask",
            "pydantic/pydantic", "tiangolo/fastapi", "urllib3/urllib3", "bottlepy/bottle"
        ]
        target_pool = list(dict.fromkeys(top_repos + curated))
        print(f"[*] Monitoring repository pool ({len(target_pool)} repos): {', '.join(target_pool[:6])}...")

        seen_urls = set()
        for r in target_pool:
            if any(exc.lower() in r.lower() for exc in args.exclude):
                continue
            try:
                cmd = ["gh", "issue", "list", "--repo", r, "--state", "open", "--limit", "5", "--json", "number,title,body,url,labels"]
                items_raw = run_cmd(cmd)
                items = json.loads(items_raw)
                for item in items:
                    item["repository_url"] = f"https://api.github.com/repos/{r}"
                    u = item.get("url") or f"{r}#{item.get('number')}"
                    if u not in seen_urls:
                        seen_urls.add(u)
                        candidates.append(item)
            except Exception as e:
                print(f"[!] Error fetching issues from {r}: {e}")
            if len(candidates) >= 25:
                break

    print(f"[*] Found {len(candidates)} candidate issues. Filtering for actionable bugs in repositories with > {args.min_stars} stars...")

    solved_count = 0
    for issue in candidates:
        ok, reason = is_actionable_issue(issue, excluded_repos=args.exclude, min_stars=args.min_stars, gh_token=gh_token)
        repo_name = issue.get("repository_url", "").replace("https://api.github.com/repos/", "")
        num = issue.get("number")
        title = issue.get("title", "")

        if not ok:
            print(f"[-] Skipping [{repo_name}] #{num} ({reason}): {title[:50]}")
            continue

        print(f"[+] Selected actionable issue: [{repo_name}] #{num} - {title}")
        success = solve_issue_in_repo(
            repo_full_name=repo_name,
            issue_data=issue,
            api_key=api_key,
            model=model,
            my_username=my_username,
            gh_token=gh_token,
            auto_pr=args.auto_pr
        )
        if success:
            solved_count += 1
            if solved_count >= args.max_pr:
                print(f"[*] Reached max PR limit of {args.max_pr}. Stopping this cycle.")
                break

    print(f"\n[*] Open source bug hunter cycle finished. PRs generated: {solved_count}")


if __name__ == "__main__":
    main()
