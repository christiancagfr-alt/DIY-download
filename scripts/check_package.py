"""Reject private application settings in distributable directories."""
from pathlib import Path
import sys

PRIVATE_NAMES = {
    "token.json", "credentials.json", "谷歌服务账号.json",
    "diy_downloader_configs.json", "global_settings.json",
    "paste_link_settings.json", "upload_settings.json", "upload_task_queue.json",
    ".diy_drive_ids.json", ".diy_folder_done.json", ".env",
}

def check_package(root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError("Build directory does not exist")
    bad = [p.relative_to(root).as_posix() for p in root.rglob("*")
           if p.is_file() and (p.name.lower() in PRIVATE_NAMES or p.name.startswith(".env."))]
    if bad:
        raise ValueError("Private configuration in package: " + ", ".join(bad))

if __name__ == "__main__":
    check_package(sys.argv[1])
    print("Package contains no private application configuration.")

