"""Generate a native macOS icon using the repository's existing logo."""
from pathlib import Path
import subprocess
import tempfile


def build_icon():
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='diy-icon-') as tmp:
        iconset = Path(tmp) / 'DIYDownloader.iconset'
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = '@2x' if scale == 2 else ''
                subprocess.run(['sips', '-z', str(size * scale), str(size * scale),
                                str(root / 'logo.png'), '--out',
                                str(iconset / f'icon_{size}x{size}{suffix}.png')],
                               check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(root / 'logo.icns')], check=True)


if __name__ == '__main__':
    build_icon()
