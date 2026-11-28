#!/usr/bin/env python3
"""Send engineering artifacts over verified SSH to the Pi's loopback HTTP receiver; never print."""

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

# Host scripts use the same dependency-free policy shipped with Observatory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mks-wifi/src/mks_wifi_bridge"))
import artifact_policy  # noqa: E402

BRIDGE = r"""
import http.client,json,os,sys
try:
    name,digest,length,token_path,port=sys.argv[1:]
    if os.stat(token_path).st_mode & 0o077:
        raise ValueError("Receiver token permissions are not private")
    token=open(token_path).read().strip()
    from urllib.parse import urlencode
    connection=http.client.HTTPConnection("127.0.0.1",int(port),timeout=30)
    connection.request("POST","/receive?"+urlencode({"filename":name}),body=sys.stdin.buffer,
        headers={"Authorization":"Bearer "+token,"Content-Type":"application/octet-stream",
                 "Content-Length":length,"X-Content-SHA256":digest,
                 "X-LilBitDrop-Transport":"ssh"})
    response=connection.getresponse()
    raw=response.read(65537)
    if response.status != 201 or len(raw)>65536:
        raise ValueError("Receiver rejected transfer (HTTP %s)"%response.status)
    result=json.loads(raw)
    if result.get("sha256") != digest or result.get("size") != int(length):
        raise ValueError("Receiver receipt does not match original bytes")
    print(json.dumps(result))
except Exception as error:
    print(json.dumps({"error":"Secure handoff failed", "type":type(error).__name__}))
    sys.exit(1)
"""


def send(
    path,
    host="neptune",
    token_file="/home/cdaprod/.config/lilbitdrop/receiver-token",
    port=8091,
    runner=subprocess.run,
):
    path = Path(path)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host):
        raise ValueError("Use a configured SSH host alias")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Supply a regular engineering file")
    artifact_policy.transport_kind(path.name)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid loopback receiver port")
    with path.open("rb") as source:
        size = path.stat().st_size
        if not 0 < size <= artifact_policy.MAX_BYTES:
            raise ValueError("Artifact must be 1 byte to 50 MiB")
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        source.seek(0)
        command = " ".join(
            shlex.quote(value)
            for value in [
                "python3",
                "-c",
                BRIDGE,
                path.name,
                digest,
                str(size),
                token_file,
                str(port),
            ]
        )
        result = runner(
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=5",
                host,
                command,
            ],
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=330,
        )
    if result.returncode:
        raise ValueError("SSH handoff failed; verify host trust, receiver and private token file")
    receipt = json.loads(result.stdout)
    if receipt.get("sha256") != digest or receipt.get("printing") is not False:
        raise ValueError("Invalid receive-only receipt")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    parser.add_argument("--ssh-host", default="neptune")
    parser.add_argument(
        "--remote-token-file", default="/home/cdaprod/.config/lilbitdrop/receiver-token"
    )
    parser.add_argument("--receiver-port", type=int, default=8091)
    args = parser.parse_args()
    try:
        print(
            json.dumps(send(args.file, args.ssh_host, args.remote_token_file, args.receiver_port))
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
