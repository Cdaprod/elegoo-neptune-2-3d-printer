#!/usr/bin/env python3
"""Build an isolated experimental image. Contains no upload or flashing operation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

REVISION = 'af5eb011d08f2a2e19809e38470f678958530b29'
URL = 'https://github.com/makerbase-mks/Mks-Robin-Nano-Marlin2.0-Firmware.git'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='New empty build checkout; must not exist')
    parser.add_argument('--pio', default='pio')
    args = parser.parse_args()
    if args.destination.exists():
        parser.error('Destination exists; choose a new directory to preserve its contents')
    version = subprocess.check_output([args.pio, '--version'], text=True).strip()
    if version != 'PlatformIO Core, version 6.1.19':
        parser.error('Install platformio==6.1.19 for this recorded build')
    subprocess.run(['git', 'clone', '--no-checkout', '--depth', '1', URL, str(args.destination)], check=True)
    root = args.destination.resolve()
    subprocess.run(['git', 'fetch', '--depth', '1', 'origin', REVISION], cwd=root, check=True)
    subprocess.run(['git', 'checkout', '--detach', REVISION], cwd=root, check=True)
    patch = root / '.rsd-applied.patch'
    patch.write_bytes(Path(__file__).with_name('marlin-rsd1.patch').read_bytes())
    subprocess.run(['git', 'apply', '--check', str(patch)], cwd=root, check=True)
    subprocess.run(['git', 'apply', str(patch)], cwd=root, check=True)
    subprocess.run([sys.executable, str(Path(__file__).with_name('test_handler.py')), str(root)], check=True)
    environment = {**os.environ, 'SOURCE_DATE_EPOCH': '1640995200'}
    subprocess.run([args.pio, 'run', '-e', 'mks_robin_nano35'], cwd=root, env=environment, check=True)
    output = root / '.pio/build/mks_robin_nano35'
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'experimental_only': True, 'hardware_accepted': False, 'upstream': URL,
                'revision': REVISION, 'source_date_epoch': environment['SOURCE_DATE_EPOCH'],
                'platformio': version, 'patch_sha256': sha(patch),
                'artifacts': {path.name: sha(path) for path in output.glob('*.bin')}}
    (output / 'rsd1-build.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest, indent=2))
    print('NOT FLASH QUALIFIED. No upload/flash command was executed.')


if __name__ == '__main__':
    main()
