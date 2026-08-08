"""
scan_for_secrets.py

Quick local scan for accidentally-hardcoded secrets across this project's
Python files. Not a replacement for a real tool (gitleaks, trufflehog) if
this ever goes on GitHub -- just a fast, no-dependency first pass to run
now while everything's still local.

USAGE
-----
    python scan_for_secrets.py
"""

import re
from pathlib import Path

SUSPICIOUS_PATTERNS = [
    (r'(?i)(api[_-]?key|secret|token|password)\s*=\s*["\'][A-Za-z0-9_\-]{16,}["\']', "possible hardcoded secret"),
    (r'AIza[0-9A-Za-z_\-]{35}', "looks like a Google API key"),
    (r'sk-[A-Za-z0-9]{20,}', "looks like an OpenAI-style API key"),
    (r'ghp_[A-Za-z0-9]{36}', "looks like a GitHub personal access token"),
]

# Lines matching these are almost certainly reading from the environment,
# not hardcoding -- skip them to avoid false positives on every legitimate
# os.getenv("...") call in the project.
SAFE_PATTERNS = [
    r'os\.getenv\(',
    r'os\.environ\[',
    r'os\.environ\.get\(',
]


def scan_file(path: Path):
    findings = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    for line_no, line in enumerate(text.splitlines(), start=1):
        if any(re.search(p, line) for p in SAFE_PATTERNS):
            continue
        for pattern, label in SUSPICIOUS_PATTERNS:
            if re.search(pattern, line):
                findings.append((line_no, label, line.strip()))
    return findings


def main():
    root = Path(".")
    py_files = sorted(root.glob("*.py"))
    total_findings = 0

    for path in py_files:
        if path.name == "scan_for_secrets.py":
            continue  # don't flag this file's own pattern definitions
        findings = scan_file(path)
        if findings:
            print(f"\n{path.name}:")
            for line_no, label, line in findings:
                print(f"  line {line_no}: {label}")
                print(f"    {line}")
                total_findings += 1

    if total_findings == 0:
        print("No suspicious hardcoded secrets found in .py files.")
    else:
        print(f"\n{total_findings} finding(s) above -- review each one.")

    if Path(".git").exists():
        gitignore = Path(".gitignore")
        gitignore_text = gitignore.read_text() if gitignore.exists() else ""
        if ".env" not in gitignore_text:
            print("\nWARNING: this is a git repo but .env is NOT in .gitignore. "
                  "Add it before you ever push, or your secrets go to GitHub.")
        else:
            print("\n.env is properly listed in .gitignore.")
    else:
        print("\nNo .git folder found here -- not using git yet, so no push risk right now.")


if __name__ == "__main__":
    main()
