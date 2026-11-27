"""No USB/serial opens: deterministic identity and conflicting-owner preflight tests."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from check_usb_owner import identity, ownership


class UsbOwnership(unittest.TestCase):
    def test_ch340_identity_requires_the_actual_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device = root / "ttyUSB0"
            device.touch()
            usb = root / "usb"
            node = usb / "interface" / "ttyUSB0"
            node.mkdir(parents=True)
            (usb / "idVendor").write_text("1a86")
            (usb / "idProduct").write_text("7523")
            driver = root / "ch341-uart"
            driver.mkdir()
            (node / "driver").symlink_to(driver)
            sysroot = root / "sys"
            (sysroot / "ttyUSB0").mkdir(parents=True)
            (sysroot / "ttyUSB0" / "device").symlink_to(node)
            self.assertEqual(identity(device, sysroot)["vid_pid"], "1a86:7523")
            (usb / "idProduct").write_text("0001")
            with self.assertRaises(RuntimeError):
                identity(device, sysroot)

    def test_picocom_or_paused_containers_block_without_killing_anything(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[0] == "fuser":
                return SimpleNamespace(returncode=0, stdout="1234")
            return SimpleNamespace(returncode=1, stdout="")

        with self.assertRaisesRegex(RuntimeError, "Close picocom"):
            ownership("/dev/null", run)
        self.assertFalse(any("kill" in command for command in calls))

        def paused(command, **kwargs):
            return SimpleNamespace(
                returncode=0, stdout="" if command[0] == "fuser" else "true"
            )

        with self.assertRaisesRegex(RuntimeError, "Paused containers"):
            ownership("/dev/null", paused)


if __name__ == "__main__":
    unittest.main()
