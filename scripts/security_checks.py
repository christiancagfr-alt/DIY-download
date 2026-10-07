#!/usr/bin/env python3
"""Small repository security policy checker used by CI.

This complements Bandit/pip-audit. It intentionally checks a short list of
high-confidence patterns so CI is not dominated by false positives.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "build", "dist", "release",
    "__pycache__", ".mypy_cache", ".pytest_cache",
}
TEXT_SUFFIXES = {
    ".py", ".yml", ".yaml", ".json", ".md", ".txt", ".ini", ".cfg",
    ".toml", ".spec", ".iss",
}

RULES = [
    ("command-shell", re.compile(r"\bshell\s*=\s*True\b"), "shell=True"),
    ("os-system", re.compile(r"\bos\.system\s*\("), "os.system"),
    ("dynamic-eval", re.compile(r"(?<![.\w])eval\s*\("), "eval()"),
    ("dynamic-exec", re.compile(r"(?<![.\w])exec\s*\("), "exec()"),
    ("pickle-load", re.compile(r"\bpickle\.loads?\s*\("), "pickle load"),
    ("tls-verify-off", re.compile(r"\bverify\s*=\s*False\b"), "TLS verify=False"),
    ("tls-cert-none", re.compile(r"\bCERT_NONE\b"), "TLS CERT_NONE"),
    ("tls-hostname-off", re.compile(r"\bcheck_hostname\s*=\s*False\b"), "hostname verification disabled"),
    ("weak-temp", re.compile(r"\btempfile\.mktemp\s*\("), "tempfile.mktemp"),
    ("world-writable", re.compile(r"\bchmod\s*\([^\n]*0o?777\b"), "world-writable chmod"),
]

SECRET_RULES = [
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("github-token", re.compile(r"(?:gh[pousr]_[0-9A-Za-z]{20,}|github_pat_[0-9A-Za-z_]{20,})")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack-token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{20,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

ACTION_FLOATING = re.compile(
    r"^\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([^\s#]+)",
    re.MULTILINE,
)
FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name == ".env.example" or path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings: list[str] = []

    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(root)

        for rule_id, pattern, label in RULES:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{rel}:{line}: {rule_id}: {label}")

        for rule_id, pattern in SECRET_RULES:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{rel}:{line}: {rule_id}: possible committed secret")

        if rel.parts[:2] == (".github", "workflows"):
            for match in ACTION_FLOATING.finditer(text):
                action, ref = match.groups()
                if not FULL_SHA.fullmatch(ref):
                    line = text.count("\n", 0, match.start()) + 1
                    findings.append(
                        f"{rel}:{line}: unpinned-action: {action}@{ref} must use a full commit SHA"
                    )

    if findings:
        print("Security policy check FAILED:")
        for item in findings:
            print(" -", item)
        return 1

    print("Security policy check PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
