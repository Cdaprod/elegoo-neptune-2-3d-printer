#!/usr/bin/env python3
"""Read-only remote appliance evidence. Never opens serial devices or sends G-code."""
import argparse
import json
import re
import subprocess
from pathlib import Path

PROBE = r'''
import glob,json,os,platform,shutil,subprocess
from pathlib import Path

def command(args):
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=12)
        return {"ok":result.returncode==0,"output":result.stdout[:65536]}
    except (OSError,subprocess.TimeoutExpired):
        return {"ok":False,"output":""}

report={"schema_version":1,"inspection":"read_only_no_printer_commands",
        "hostname":platform.node(),"architecture":platform.machine()}
report["containers"]=[]
for name in ("neptune-observatory","neptune-octoprint"):
    result=command(["docker","inspect",name])
    if result["ok"]:
        data=json.loads(result["output"])[0]
        report["containers"].append({"name":name,"running":data["State"]["Running"],
            "paused":data["State"]["Paused"],"mounts":[{k:m.get(k) for k in
            ("Type","Source","Destination","RW")} for m in data["Mounts"]]})
    else:report["containers"].append({"name":name,"inspection":"unavailable"})
report["storage"]=command(["lsblk","--json","-o","NAME,TRAN,SIZE,FSTYPE,MOUNTPOINTS"])
report["network"]=command(["ip","-brief","link"])
report["services"]={}
for name in ("bluetooth.service","lilbitdrop-receiver.service"):
    report["services"][name]=command(["systemctl","is-active",name])
report["services"]["kiosk"]=command(["systemctl","--user","is-active","neptune-kiosk.service"])
report["serial_devices"]=[{"stable_path":p,"device":os.path.realpath(p)}
                          for p in glob.glob("/dev/serial/by-id/*")]
report["touchscreen"]=[]
for path in Path("/sys/class/input").glob("event*/device"):
    try:
        if (path/"id/vendor").read_text().strip()=="0712" and (path/"id/product").read_text().strip()=="000a":
            report["touchscreen"].append({"name":(path/"name").read_text().strip(),"usb_id":"0712:000a"})
    except OSError:pass
report["handoff"]={p:{"exists":Path(p).exists(),"readable":os.access(p,os.R_OK)} for p in
    ("/srv/neptune/incoming/partial","/srv/neptune/incoming/completed",
     "/home/cdaprod/lilbitdrop-lab/peripheral.py")}
report["root_free_bytes"]=shutil.disk_usage("/").free
# Cached application observations only. Do not call refresh, transfer or control endpoints.
api_script=r"""
import json,os,urllib.request,urllib.error
results={}
for endpoint in ('/api/factory-usb','/api/printer/sdcard','/api/fabrication/ingestion/status'):
    try:
        request=urllib.request.Request('http://127.0.0.1:8000'+endpoint,
          headers={'Authorization':'Bearer '+os.getenv('NEPTUNE_OBSERVATORY_TOKEN','')})
        with urllib.request.urlopen(request,timeout=5) as response:data=json.load(response)
        results[endpoint]={'http_status':200}
        for key in ('status','available','content_retrieval','printer_listing_source'):
            if key in data:results[endpoint][key]=data[key]
        if 'entries' in data:results[endpoint]['entry_count']=len(data['entries'])
        if 'adapters' in data:results[endpoint]['adapters']=[{k:a.get(k) for k in
            ('provider','state','pending','ingested')} for a in data['adapters']]
    except urllib.error.HTTPError as error:results[endpoint]={'http_status':error.code}
    except Exception:results[endpoint]={'inspection':'unavailable'}
print(json.dumps(results))
"""
result=command(["docker","exec","neptune-observatory","python","-c",api_script])
try:report["application"]=json.loads(result["output"]) if result["ok"] else {"inspection":"unavailable"}
except ValueError:report["application"]={"inspection":"unavailable"}
report["capabilities"]={"native_sd_bytes":"unverified_optional",
 "native_sd_enumeration":"operator_verified_previous_transcript_not_reissued",
 "physical_print":"not_tested","wireless_delivery":"not_tested",
 "serial_ownership":"device_inventory_only_no_port_opened"}
print(json.dumps(report))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="neptune")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.host):
        parser.error("Use a configured SSH alias")
    try:
        result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o",
                                 "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
                                 args.host, "python3 -"], input=PROBE, text=True,
                                capture_output=True, timeout=120)
        if result.returncode:
            parser.exit(1, "Commissioning SSH probe failed; verify host trust and read access.\n")
        report = json.loads(result.stdout)
        rendered = json.dumps(report, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered)
        else:
            print(rendered, end="")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        parser.exit(1, "Could not collect a valid read-only commissioning report.\n")


if __name__ == "__main__":
    main()
