#!/usr/bin/env python3
"""Preserve labwc XML while applying the commissioned touchscreen profile."""
import argparse
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROFILE = Path(__file__).parent / 'profiles/waveshare-7.9.env'
JOURNAL = '[Journal]\nStorage=persistent\nSplitMode=uid\nSystemMaxUse=128M\nMaxRetentionSec=14day\n'


def profile():
    result = {}
    for line in PROFILE.read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key] = value.strip('\"\'')
    return result


def discover_touch(devices, vendor, product):
    """Match sysfs USB IDs, never a USB topology or event number."""
    names = set()
    for device in Path(devices).glob('event*/device'):
        try:
            if (device / 'id/vendor').read_text().strip().lower() != vendor:
                continue
            if (device / 'id/product').read_text().strip().lower() != product:
                continue
            names.add((device / 'name').read_text().strip())
        except OSError:
            continue
    if len(names) != 1:
        raise RuntimeError(f'Expected one touchscreen name for {vendor}:{product}; found {sorted(names)}')
    return names.pop()


def configure_xml(path, name, settings):
    path = Path(path)
    if path.exists():
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        tree = ET.parse(path, parser=parser)
        original = path.read_bytes()
    else:
        tree = ET.ElementTree(ET.Element('labwc_config'))
        original = None
    root = tree.getroot()
    ns = root.tag.split('}')[0][1:] if root.tag.startswith('{') else ''
    if ns:
        ET.register_namespace('', ns)
    tag = lambda key: f'{{{ns}}}{key}' if ns else key
    def child(parent, key):
        node = parent.find(tag(key))
        return node if node is not None else ET.SubElement(parent, tag(key))
    # Remove only obsolete entries for this known display, preserving all other settings.
    for node in list(root.findall(tag('touch'))):
        if settings['TOUCH_DEVICE_NAME'] in node.get('deviceName', ''):
            root.remove(node)
    touch = ET.SubElement(root, tag('touch'), {
        'deviceName': name, 'mapToOutput': settings['TOUCH_MAP_OUTPUT'],
        'mouseEmulation': settings['TOUCH_MOUSE_EMULATION'],
    })
    libinput = child(root, 'libinput')
    for node in list(libinput.findall(tag('device'))):
        if settings['TOUCH_DEVICE_NAME'] in node.get('category', ''):
            libinput.remove(node)
    device = ET.SubElement(libinput, tag('device'), {'category': name})
    child(device, 'calibrationMatrix').text = settings['TOUCH_CALIBRATION_MATRIX']
    # Stable formatting; do not rewrite a file when its desired bytes already match.
    ET.indent(tree, space='  ')
    data = ET.tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'
    if original == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + '.pre-neptune')
    if original is not None and not backup.exists():
        backup.write_bytes(original)
        backup.chmod(0o600)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.neptune-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.chmod(temporary, (path.stat().st_mode & 0o777) if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sys-input', default='/sys/class/input')
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'labwc/rc.xml')
    args = parser.parse_args()
    settings = profile()
    name = discover_touch(args.sys_input, settings['TOUCH_USB_VENDOR_ID'], settings['TOUCH_USB_PRODUCT_ID'])
    changed = configure_xml(args.config, name, settings)
    if changed:
        subprocess.run(['pkill', '-HUP', '-u', str(os.getuid()), '-x', 'labwc'], check=False)
    print(f'Touchscreen configured: {name}; XML {"updated" if changed else "unchanged"}')


if __name__ == '__main__':
    main()
