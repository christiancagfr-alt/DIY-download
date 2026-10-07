from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
errors: list[str] = []

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
