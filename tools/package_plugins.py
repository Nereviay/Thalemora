"""打包两个宿主插件；只收录固定源码与许可，不读取任何用户配置。"""
import argparse
import json
from pathlib import Path
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='产物目录')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=True)
    packages = [
        ('sillytavern', 'zip', ['manifest.json', 'index.js', 'README.md']),
        ('operit/adapter', 'toolpkg', ['manifest.json', 'main.js', 'settings.js', 'settings.ui.js', 'README.md']),
    ]
    for relative, suffix, files in packages:
        source = root / 'integrations' / relative
        manifest = json.loads((source / 'manifest.json').read_text())
        host = relative.split('/')[0]
        target = args.output / f'thalemora-{host}-{manifest["version"]}.{suffix}'
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.write(source / name, name)
            archive.write(root / 'LICENSE', 'LICENSE')
        print(target)


if __name__ == '__main__':
    main()
