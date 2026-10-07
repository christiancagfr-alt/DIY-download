from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
errors: list[str] = []

SECRET_PATTERNS = [
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("OpenAI-style secret", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
]
FORBIDDEN_TRACKED_NAMES = {
    "token.json",
    "credentials.json",
    "谷歌服务账号.json",
    ".env",
}

def fail(message: str) -> None:
    errors.append(message)

# Repository migration must be complete.
for path in ROOT.rglob("*"):
    if not path.is_file() or ".git" in path.parts:
        continue
    relative = path.relative_to(ROOT).as_posix()
    # Avoid policy literals in this checker tripping its own content scan.
    if relative == "scripts/security_checks.py":
        continue
    if path.suffix.lower() not in {".py", ".md", ".yml", ".yaml", ".txt", ".iss"}:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue
    if "secure-artifacts/DIY-sheets_batch_downloader" in text:
        fail(f"legacy organization repository reference: {path.relative_to(ROOT)}")
    if "shell=True" in text or "verify=False" in text or "CERT_NONE" in text:
        fail(f"unsafe execution/TLS pattern: {path.relative_to(ROOT)}")
    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        fail(f"private key material committed: {path.relative_to(ROOT)}")
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            fail(f"possible {label} committed: {relative}")

for path in ROOT.rglob("*"):
    if not path.is_file() or ".git" in path.parts:
        continue
    if path.name in FORBIDDEN_TRACKED_NAMES or (path.name.startswith(".env.") and path.name != ".env.example"):
        fail(f"sensitive local configuration is tracked: {path.relative_to(ROOT).as_posix()}")

gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
for required_ignore in ("token.json", "credentials.json", "谷歌服务账号.json", ".env"):
    if required_ignore not in gitignore:
        fail(f".gitignore is missing sensitive pattern: {required_ignore}")

req = (ROOT / "requirements_google.txt").read_text(encoding="utf-8")
required = {
    "httplib2==0.32.0",
    "yt-dlp==2026.8.19",
}
for item in required:
    if item not in req:
        fail(f"required reviewed dependency pin missing: {item}")
if "Pillow" in req:
    fail("unused Pillow dependency must not be present")

version = (ROOT / "version.py").read_text(encoding="utf-8")
if 'GITHUB_OWNER = "christiancagfr-alt"' not in version or 'GITHUB_REPO = "DIY-download"' not in version:
    fail("update source is not the personal repository")

video = (ROOT / "video_batch_downloader.py").read_text(encoding="utf-8")
if '"ignoreconfig": True' not in video:
    fail("yt-dlp external config is not disabled")
if 'platform_name not in ("YouTube", "Facebook")' not in video:
    fail("video URL allowlist is missing")

workflow_dir = ROOT / ".github" / "workflows"
action_re = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
sha_ref = re.compile(r"^[^@]+@[0-9a-f]{40}$")
for wf in workflow_dir.glob("*.y*ml"):
    text = wf.read_text(encoding="utf-8")
    for ref in action_re.findall(text):
        if ref.startswith("./") or ref.startswith("docker://"):
            continue
        if not sha_ref.fullmatch(ref):
            fail(f"GitHub Action not pinned to commit SHA: {wf.name}: {ref}")

if errors:
    print("Security policy checks FAILED:")
    for err in errors:
        print(f" - {err}")
    raise SystemExit(1)

print("Security policy checks passed.")
