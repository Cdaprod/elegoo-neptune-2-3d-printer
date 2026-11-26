"""Configuration regression tests; no host configuration or printer I/O."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('appliance', ROOT/'deploy/raspberry-pi/configure_appliance.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class ApplianceTests(unittest.TestCase):
    def test_namespaced_xml_preserved_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'rc.xml'
            original = '<labwc_config xmlns="http://openbox.org/3.4/rc"><keyboard><keybind key="A-x"/></keyboard><!--keep--><touch deviceName="Other"/><touch deviceName="WaveShare WaveShare (USB 1-1.4)" mouseEmulation="yes"/><libinput><device category="Other"><naturalScroll>yes</naturalScroll></device></libinput></labwc_config>'
            p.write_text(original)
            self.assertTrue(a.configure_xml(p, 'WaveShare WaveShare (USB 1-2)', a.profile()))
            first = p.read_bytes()
            self.assertFalse(a.configure_xml(p, 'WaveShare WaveShare (USB 1-2)', a.profile()))
            self.assertEqual(first, p.read_bytes())
            self.assertEqual(p.with_name('rc.xml.pre-neptune').read_text(), original)
            root = ET.parse(p).getroot()
            ns = {'x':'http://openbox.org/3.4/rc'}
            self.assertIsNotNone(root.find('x:keyboard/x:keybind', ns))
            self.assertEqual(len(root.findall('x:touch', ns)), 2)
            touch = root.findall('x:touch', ns)[1]
            self.assertEqual(touch.get('mouseEmulation'), 'no')
            self.assertEqual(root.findall('x:libinput/x:device', ns)[1].find('x:calibrationMatrix', ns).text, '0 1 0 -1 0 1')
            self.assertIn('<!--keep-->', first.decode())

    def test_sysfs_discovers_actual_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'event7/device'
            (p/'id').mkdir(parents=True)
            (p/'id/vendor').write_text('0712\n')
            (p/'id/product').write_text('000a\n')
            (p/'name').write_text('WaveShare WaveShare (USB 1-3)\n')
            self.assertEqual(a.discover_touch(d,'0712','000a'), 'WaveShare WaveShare (USB 1-3)')
            with self.assertRaises(RuntimeError):
                a.discover_touch(d,'0000','0000')

    def test_fullscreen_sequence_preserved(self):
        s = (ROOT/'deploy/raspberry-pi/kiosk/authenticated.py').read_text()
        for value in ['no_viewport=True','ignore_default_args=["--enable-automation"]','Browser.getWindowForTarget','Browser.setWindowBounds','"windowState": "fullscreen"','/hmi']:
            self.assertIn(value,s)


if __name__ == '__main__':
    unittest.main()
