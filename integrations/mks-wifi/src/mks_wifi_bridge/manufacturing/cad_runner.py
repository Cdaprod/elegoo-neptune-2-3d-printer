"""Run inside FreeCADCmd or CadQuery Python; request comes from controller-owned JSON."""

import json
import os
from pathlib import Path


def run(request):
    root = Path(request["root"])
    output = Path(request["output"])
    source = request["source"]
    objects = request["objects"]
    if source["engine"] == "freecad":
        import FreeCAD as App
        import MeshPart
        import Part

        doc = App.openDocument(str(root / source["document"]))
        try:

            def read(key):
                name, prop = key.split(".", 1)
                obj = doc.getObject(name)
                value = getattr(obj, prop)
                return float(value.Value) if hasattr(value, "Value") else value

            protected = {k: read(k) for k in source["protectedParameters"]}
            for key, expected in source["protectedParameters"].items():
                if abs(float(protected[key]) - float(expected)) > 1e-7:
                    raise ValueError("Protected parameter differs from expected: " + key)
            for key, value in source["parameters"].items():
                name, prop = key.split(".", 1)
                obj = doc.getObject(name)
                if obj is None or prop not in obj.PropertiesList:
                    raise ValueError("Unknown CAD parameter: " + key)
                if any(k == prop for k, _ in obj.ExpressionEngine):
                    raise ValueError("Parameter has an upstream expression: " + key)
                setattr(obj, prop, value)
            doc.recompute()
            invalid = [o.Name for o in doc.Objects if "Invalid" in o.State]
            if invalid:
                raise ValueError("Invalid document objects: " + repr(invalid))
            report = {}
            for obj in objects:
                feature = doc.getObject(obj["cadObject"])
                if feature is None or not hasattr(feature, "Shape"):
                    raise ValueError("Missing CAD solid: " + obj["cadObject"])
                shape = feature.Shape
                if shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
                    raise ValueError("Export must be one valid BREP solid: " + feature.Name)
                mesh = MeshPart.meshFromShape(
                    Shape=shape, LinearDeflection=0.05, AngularDeflection=0.15, Relative=False
                )
                mesh.write(str(output / obj["export"]))
                Part.export([feature], str(output / (Path(obj["export"]).stem + ".step")))
                report[obj["id"]] = {
                    "valid": True,
                    "solids": 1,
                    "volume": shape.Volume,
                    "bounds": [
                        shape.BoundBox.XMin,
                        shape.BoundBox.YMin,
                        shape.BoundBox.ZMin,
                        shape.BoundBox.XMax,
                        shape.BoundBox.YMax,
                        shape.BoundBox.ZMax,
                    ],
                }
            if any(read(k) != v for k, v in protected.items()):
                raise ValueError("Protected screw-bearing parameter changed")
            doc.saveAs(str(output / "source.FCStd"))
            report["_engine"] = App.Version()
        finally:
            App.closeDocument(doc.Name)
    else:
        import runpy
        import sys

        import cadquery as cq

        sys.path.insert(0, str(root))
        namespace = runpy.run_path(
            str(root / source["document"]), init_globals={"PARAMETERS": source["parameters"]}
        )
        report = {}
        for obj in objects:
            value = namespace[obj["cadObject"]]
            shape = value.val() if isinstance(value, cq.Workplane) else value
            if not shape.isValid() or len(shape.Solids()) != 1:
                raise ValueError("CadQuery export must be one valid solid")
            cq.exporters.export(shape, str(output / obj["export"]))
            cq.exporters.export(shape, str(output / (Path(obj["export"]).stem + ".step")))
            report[obj["id"]] = {"valid": True, "solids": 1, "volume": shape.Volume()}
        if source["protectedParameters"]:
            raise ValueError("CadQuery protected parameters require a model validation contract")
        report["_engine"] = cq.__version__
    (output / "validation.json").write_text(json.dumps(report, indent=2))


if os.environ.get("MANUFACTURING_BUILD_REQUEST"):
    run(json.loads(Path(os.environ["MANUFACTURING_BUILD_REQUEST"]).read_text()))
