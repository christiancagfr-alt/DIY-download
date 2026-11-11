#!/bin/bash
# Usage: bash scripts/package_macos.sh dist/DIYDownloader.app release/DIYDownloader.dmg
set -euo pipefail
app_path="${1:?app path required}"
dmg_path="${2:?dmg output path required}"
test -d "$app_path"
test -s "$app_path/Contents/Info.plist"
mkdir -p "$(dirname "$dmg_path")"
staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/diy-dmg.XXXXXX")"
trap 'rm -rf "$staging_dir"' EXIT
ditto "$app_path" "$staging_dir/DIYDownloader.app"
ln -s /Applications "$staging_dir/Applications"
hdiutil create -volname "DIYDownloader" -srcfolder "$staging_dir" -ov -format UDZO "$dmg_path"
hdiutil verify "$dmg_path"
