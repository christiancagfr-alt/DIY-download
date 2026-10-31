"""应用版本与更新仓库常量（唯一真源）。"""

APP_NAME = "DIY下载器"
APP_VERSION = "1.6.7"

# 个人 GitHub 仓库（自动更新源与构建发布源）
GITHUB_OWNER = "christiancagfr-alt"
GITHUB_REPO = "DIY-download"
GITHUB_REPO_SLUG = f"{GITHUB_OWNER}/{GITHUB_REPO}"

RELEASES_PAGE = f"https://github.com/{GITHUB_REPO_SLUG}/releases"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO_SLUG}/releases/latest"
