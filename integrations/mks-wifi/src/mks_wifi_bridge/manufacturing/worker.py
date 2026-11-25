"""Native Mac build worker and content reconciliation watcher."""

import argparse
import json
import os
import time
from pathlib import Path

from .controller import Controller


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage", default=os.getenv("NEPTUNE_MANUFACTURING_STORAGE", "/data/manufacturing")
    )
    parser.add_argument(
        "--projects",
        default=os.getenv("NEPTUNE_MANUFACTURING_PROJECTS", "/data/manufacturing/projects"),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--watch", action="store_true", help="Apply manifest autoBuild/autoSlice/autoStage policies"
    )
    args = parser.parse_args()
    controller = Controller(Path(args.storage), Path(args.projects))
    seen = {}
    while True:
        if args.watch:
            for item in controller.discover():
                if item["error"]:
                    continue
                root, m = controller.project(item["name"])
                policy = m.spec.deployment
                if not (policy.autoBuild or policy.autoSlice):
                    continue
                signature = controller.inputs(root, m)
                if policy.autoSlice:
                    signature += controller.version(os.getenv("ORCASLICER_PATH", "orca-slicer"))
                if seen.get(item["name"]) == signature:
                    continue
                for plate in m.spec.plates:
                    if policy.autoSlice and policy.autoBuild:
                        action = "build-slice-stage" if policy.autoStage else "build-slice"
                    elif policy.autoSlice:
                        action = "slice-stage" if policy.autoStage else "slice"
                    else:
                        action = "build"
                    controller.enqueue(item["name"], plate.name, action)
                seen[item["name"]] = signature
        worked = controller.work_once()
        if args.once:
            print(json.dumps({"processed": worked}))
            return
        time.sleep(0.2 if worked else 2)


if __name__ == "__main__":
    main()
