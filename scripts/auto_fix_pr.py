#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Bug Auto-Fix & PR Agent
Powered by OpenRouter AI & GitHub CLI (gh)
"""

import os
import sys
import json
import re
import argparse
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def load_env(env_path: Path):
    """Simple .env parser without external dependencies."""
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


def run_cmd(cmd, cwd=None, check=True):
    """Run a shell command and return stdout."""
    res = subprocess.run(
        cmd,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"Command failed (exit {res.returncode}): {cmd}\nStderr: {res.stderr.strip()}")
    return res.stdout.strip()


def list_issues(repo=None):
    """List open issues using gh."""
    repo_arg = f"--repo {repo}" if repo else ""
    cmd = f"gh issue list {repo_arg} --state open --limit 20"
    out = run_cmd(cmd)
    print("=== Open Issues ===")
    print(out or "No open issues found.")


def fetch_issue(issue_number, repo=None):
    """Fetch issue details via gh issue view."""
    repo_arg = f"--repo {repo}" if repo else ""
    cmd = f"gh issue view {issue_number} {repo_arg} --json number,title,body,comments,labels,url"
    raw = run_cmd(cmd)
    return json.loads(raw)


def find_candidate_files(issue_text):
    """Extract file paths mentioned in the issue text and check if they exist anywhere in repo."""
    potential_files = []
    matches = re.findall(r'[\w./\-]+\.[a-zA-Z0-9]+', issue_text)
    ignored_prefixes = (".git", ".backup", "submissions", "lot-", "data/", ".github/")

    for m in matches:
        p = Path(m)
        if p.is_file() and not any(str(p.as_posix()).startswith(ign) for ign in ignored_prefixes):
            norm = str(p.as_posix())
            if norm not in potential_files:
                potential_files.append(norm)
        else:
            fname = p.name
            if len(fname) > 3 and not fname.startswith(".") and not fname.endswith(".json"):
                found = [
                    str(f.as_posix()) for f in Path(".").glob(f"**/{fname}")
                    if not any(part in ignored_prefixes for part in f.parts)
                    and "__pycache__" not in f.parts
                ]
                for f_path in found:
                    if f_path not in potential_files:
                        potential_files.append(f_path)

    return potential_files[:5]


def call_openrouter(messages, api_key, model="openrouter/free"):
    """Call OpenRouter Chat Completion API."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com",
        "X-Title": "GitHub Issue Auto-Fixer"
    }
    payload = {
        "model": model,
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "choices" not in data or not data["choices"]:
                raise RuntimeError(f"No choices returned: {data}")
            choice = data["choices"][0]
            msg = choice.get("message", {})
            content = msg.get("content")
            if not content:
                content = msg.get("reasoning") or choice.get("text") or ""
            return content
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter API Error {e.code}: {err_body}")


DEFAULT_MODELS = [
    "cohere/north-mini-code:free",
    "poolside/laguna-s-2.1:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
]


def parse_markdown_response(text, default_issue_num):
    """Parse analysis, commit, PR, and search-and-replace blocks from markdown."""
    analysis = "Automated bug fix via OpenRouter AI"
    commit_msg = f"fix: resolve issue #{default_issue_num}"
    pr_title = f"fix(#{default_issue_num}): resolve issue"
    pr_body = f"Closes #{default_issue_num}\n\nAutomated fix generated via OpenRouter AI."
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


def plan_and_fix(issue_data, target_files, api_key, model):
    """Ask OpenRouter to diagnose the bug and produce search-and-replace fixes."""
    issue_num = issue_data["number"]
    issue_title = issue_data["title"]
    issue_body = issue_data.get("body", "") or ""

    files_context = []
    for fp in target_files:
        p = Path(fp)
        if p.is_file():
            content = p.read_text(encoding="utf-8", errors="replace")
            if len(content) > 25000:
                content = content[:25000] + "\n\n... [Content truncated] ..."
            files_context.append(f"### File: {fp}\n```python\n{content}\n```")

    files_str = "\n\n".join(files_context)

    system_prompt = (
        "You are an expert senior software engineer specializing in automated bug fixing and code maintenance.\n"
        "Your task is to analyze the issue report, inspect the provided file contents, fix the bug, and provide a search-and-replace diff.\n\n"
        "Output MUST follow this EXACT structure:\n\n"
        "# Analysis\n"
        "<Brief explanation of root cause and fix strategy>\n\n"
        "# Commit\n"
        "fix(scope): concise conventional commit message\n\n"
        "# PR\n"
        "fix(#<number>): concise PR title\n"
        "<PR body explaining changes. Must include 'Closes #<number>'>\n\n"
        "# Changes\n"
        "### File: <relative/file/path>\n"
        "<<<<<<< SEARCH\n"
        "<exact lines in original file to be replaced>\n"
        "=======\n"
        "<new replacement lines>\n"
        ">>>>>>>\n"
    )

    user_prompt = (
        f"Issue #{issue_num}: {issue_title}\n\n"
        f"Issue Description:\n{issue_body}\n\n"
        f"Files to inspect:\n{files_str}\n\n"
        "Please provide your analysis and SEARCH/REPLACE diff now."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    models_to_try = [model] if model else []
    for m in DEFAULT_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)
    last_err = None

    for m in models_to_try:
        print(f"[*] Calling OpenRouter ({m}) to analyze and fix...")
        try:
            response_text = call_openrouter(messages, api_key, m)
            parsed = parse_markdown_response(response_text, issue_num)
            if not parsed["changes"]:
                print(f"[!] Model {m} returned no changes block. Raw snippet:\n{response_text[:300]}")
                continue
            return parsed
        except Exception as e:
            print(f"[!] Model {m} failed: {e}")
            last_err = e

    raise RuntimeError(f"All models failed to generate valid diff. Last error: {last_err}")


def apply_search_replace(file_path: Path, search_block: str, replace_block: str):
    """Apply search-and-replace block to a file with indentation tolerance."""
    if not file_path.is_file():
        matches = [f for f in Path(".").glob(f"**/{file_path.name}") if ".git" not in f.parts and "__pycache__" not in f.parts]
        if matches:
            file_path = matches[0]
        else:
            print(f"[!] Warning: File {file_path} not found.")
            return False

    content = file_path.read_text(encoding="utf-8", errors="replace")

    # Exact match
    if search_block in content:
        new_content = content.replace(search_block, replace_block, 1)
        file_path.write_text(new_content, encoding="utf-8")
        return True

    # Strip-based line matching (tolerant to whitespace/indentation differences)
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

    print(f"[!] Warning: Could not locate search block in {file_path}")
    return False


def fix_single_issue(issue_num, repo, api_key, model, target_files=None, dry_run=False):
    """Execute end-to-end bug fix for a single issue and create PR."""
    print(f"\n{'=' * 50}")
    print(f"[*] Fetching issue #{issue_num} from repo '{repo or 'current'}'...")
    issue_data = fetch_issue(issue_num, repo)
    print(f"[+] Found issue #{issue_data['number']}: {issue_data['title']}")

    if not target_files:
        issue_full_text = f"{issue_data.get('title', '')}\n{issue_data.get('body', '')}"
        for comment in issue_data.get("comments", []):
            issue_full_text += f"\n{comment.get('body', '')}"
        target_files = find_candidate_files(issue_full_text)

    if not target_files:
        print(f"[!] Could not auto-detect affected files for issue #{issue_num}. Skipping.")
        return None

    print(f"[+] Target files to inspect: {', '.join(target_files)}")

    fix_data = plan_and_fix(issue_data, target_files, api_key, model)

    print(f"[*] AI Analysis: {fix_data.get('analysis')}")
    print(f"[*] Suggested Commit: {fix_data.get('commit_message')}")
    print(f"[*] PR Title: {fix_data.get('pr_title')}")

    if dry_run:
        print("[*] Dry run mode enabled. Changes will not be applied to git.")
        for change in fix_data.get("changes", []):
            print(f"\n--- Proposed Diff for {change['path']} ---")
            print("<<<<<<< SEARCH")
            print(change["search"].strip())
            print("=======")
            print(change["replace"].strip())
            print(">>>>>>>")
        return None

    branch_name = f"fix/issue-{issue_num}"
    print(f"[*] Creating and switching to branch '{branch_name}'...")
    run_cmd(f"git checkout -b {branch_name}", check=False)

    modified_paths = []
    for change in fix_data.get("changes", []):
        fpath = Path(change["path"])
        ok = apply_search_replace(fpath, change["search"], change["replace"])
        if ok and str(fpath) not in modified_paths:
            modified_paths.append(str(fpath))
            print(f"[+] Applied changes to: {fpath}")

    if not modified_paths:
        print(f"[!] No files were modified for issue #{issue_num}. Aborting PR.")
        return None

    # Syntax sanity check for python files
    for p in modified_paths:
        if p.endswith(".py"):
            try:
                import py_compile
                py_compile.compile(p, doraise=True)
                print(f"[✓] Syntax check passed: {p}")
            except Exception as e:
                print(f"[!] Syntax error in generated code {p}: {e}")
                return None

    # Unit test verification if tests folder exists
    if Path("tests").is_dir():
        print("[*] Running unit test suite to verify fix...")
        try:
            test_out = run_cmd("python -m unittest discover -s tests -p 'test_*.py'")
            print(f"[✓] Unit tests passed!\n{test_out}")
        except Exception as e:
            print(f"[!] Warning: Unit tests failed after fix: {e}")

    # Git commit
    commit_msg = fix_data.get("commit_message", f"fix: resolve issue #{issue_num}")
    for p in modified_paths:
        run_cmd(f'git add "{p}"')

    run_cmd(f'git commit -m "{commit_msg}"')
    print(f"[+] Committed changes with message: {commit_msg}")

    # Ensure remote URL has auth token if running in CI
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if gh_token and repo:
        run_cmd(f'git remote set-url origin https://x-access-token:{gh_token}@github.com/{repo}.git', check=False)

    # Push to remote
    print(f"[*] Pushing branch '{branch_name}' to origin...")
    run_cmd(f"git push -u origin {branch_name}")

    # Create PR via gh
    pr_title = fix_data.get("pr_title", f"fix(#{issue_num}): resolve issue")
    pr_body = fix_data.get("pr_body", f"Closes #{issue_num}\n\nAutomated fix generated via OpenRouter AI.")

    print(f"[*] Creating Pull Request on GitHub repo '{repo or 'current'}'...")
    pr_repo_arg = f"--repo {repo}" if repo else ""
    pr_cmd = f'gh pr create {pr_repo_arg} --title "{pr_title}" --body "{pr_body}"'
    pr_url = run_cmd(pr_cmd)

    print(f"[🎉 SUCCESS] Pull Request created successfully: {pr_url}")
    # Switch back to main branch for next iterations
    run_cmd("git checkout main", check=False)
    return pr_url


def run_cron_batch(repo, api_key, model, max_issues=3):
    """Scan open issues and automatically fix those without an existing PR."""
    repo_arg = f"--repo {repo}" if repo else ""
    print(f"[*] Scanning open issues in {repo or 'current repository'} for cron batch...")
    cmd = f"gh issue list {repo_arg} --state open --limit 20 --json number,title,labels"
    raw = run_cmd(cmd)
    issues = json.loads(raw)

    if not issues:
        print("[*] No open issues found.")
        return

    # Check existing open/closed PR branches
    pr_cmd = f"gh pr list {repo_arg} --state all --limit 50 --json headRefName"
    existing_prs = json.loads(run_cmd(pr_cmd))
    existing_branches = {p.get("headRefName") for p in existing_prs}

    processed = 0
    for issue in issues:
        num = issue["number"]
        branch_name = f"fix/issue-{num}"
        if branch_name in existing_branches:
            print(f"[*] Issue #{num} already has a corresponding branch/PR '{branch_name}'. Skipping.")
            continue

        print(f"\n[*] Processing open issue #{num}: {issue['title']}")
        try:
            url = fix_single_issue(num, repo, api_key, model)
            if url:
                processed += 1
            if processed >= max_issues:
                print(f"[*] Reached batch limit of {max_issues} issues for this cron run.")
                break
        except Exception as e:
            print(f"[!] Error processing issue #{num}: {e}")

    print(f"\n[*] Cron batch run finished. Total PRs created: {processed}")


def main():
    parser = argparse.ArgumentParser(description="Auto-fix GitHub Issues and create PRs using OpenRouter AI")
    parser.add_argument("--issue", type=int, help="Target Issue number to fix")
    parser.add_argument("--repo", type=str, help="GitHub repo (e.g. owner/repo). Defaults to GITHUB_REPOSITORY or current repo")
    parser.add_argument("--files", nargs="*", help="Specific files related to this bug")
    parser.add_argument("--model", type=str, default=None, help="OpenRouter model (default from env or fallback list)")
    parser.add_argument("--dry-run", action="store_true", help="Only generate fix without committing or pushing PR")
    parser.add_argument("--list", action="store_true", help="List open issues in the repository")
    parser.add_argument("--cron-batch", action="store_true", help="Batch process open issues without existing PRs (ideal for scheduled runs)")
    parser.add_argument("--max-issues", type=int, default=3, help="Max issues to fix in a cron-batch run (default: 3)")
    args = parser.parse_args()

    # Load .env
    root_dir = Path(__file__).resolve().parent.parent
    load_env(root_dir / ".env")

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("[Error] OPENROUTER_API_KEY not found in environment or .env file.")
        sys.exit(1)

    model = args.model or os.environ.get("OPENROUTER_MODEL")
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")

    if args.list:
        list_issues(repo)
        return

    if args.cron_batch:
        run_cron_batch(repo, api_key, model, max_issues=args.max_issues)
        return

    if not args.issue:
        print("[!] No issue number specified. Listing open issues:")
        list_issues(repo)
        print("\nUsage: python scripts/auto_fix_pr.py --issue <ISSUE_NUMBER>")
        print("   or: python scripts/auto_fix_pr.py --cron-batch")
        return

    fix_single_issue(args.issue, repo, api_key, model, target_files=args.files, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
