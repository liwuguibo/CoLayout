"""Install a per-user Linux desktop entry for this checkout."""
import argparse
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def desktop_entry(root):
    def quote(path):
        value = str(path)
        if any(character in value for character in '\n\r\0'):
            raise ValueError('Desktop entry paths cannot contain newlines.')
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'

    return '\n'.join([
        '[Desktop Entry]',
        'Type=Application',
        'Version=1.0',
        'Name=CoLayout',
        'Comment=Human and agent layout workspace',
        'Exec=' + quote(root / 'Start-CoLayout.sh'),
        'Path=' + str(root),
        'Icon=' + str(root / 'colayout_lite/assets/branding/colayout-e2.png'),
        'Terminal=false',
        'Categories=Development;',
        'StartupWMClass=colayout-lite',
        '',
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'applications/colayout-lite.desktop'
    parser.add_argument('--out', type=Path, default=default)
    args = parser.parse_args()
    if os.name == 'nt':
        raise RuntimeError('Use tools/install_workspace_shortcut.ps1 on Windows.')
    args.out.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    target = args.out.expanduser().resolve()
    target.write_text(desktop_entry(ROOT), encoding='utf8')
    target.chmod(0o644)
    print(target)


if __name__ == '__main__':
    main()
