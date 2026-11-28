"""Generate synthetic extrusion fixtures for browser validation; never printer input."""
import math
from pathlib import Path

root = Path('/tmp/neptune-hmi-browser')
root.mkdir(exist_ok=True)
for index, name in enumerate(['Display-bracket', 'Crossbar', 'Saddle', 'Camera-arm', 'Enclosure']):
    lines = ['; Synthetic browser fixture — not for printing', 'G21', 'G90', 'M83']
    for layer in range(25):
        z = (layer+1)*0.25
        for wall in range(4):
            radius = 13+index*2-wall*.5
            for i in range(49):
                angle = i*math.tau/48
                x, y = 30+radius*math.cos(angle), 30+radius*math.sin(angle)
                lines.append(f'G1 X{x:.3f} Y{y:.3f} Z{z:.3f} E{0 if i==0 else .16} F1200')
    (root/(name+'.gcode')).write_text('\n'.join(lines))

# An isolated, explicitly simulated 3MF package for the canonical HTTP-to-HMI test.
import hashlib
import json
import struct
from zipfile import ZipFile

slicer = Path('/tmp/neptune-slicer-browser')
slicer.mkdir(exist_ok=True)
vertices = [(0,0,0),(12,0,0),(0,12,0),(0,0,12)]
faces = [(0,2,1),(0,1,3),(0,3,2),(1,2,3)]
mesh_xml = '<mesh><vertices>'+''.join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x,y,z in vertices)+'</vertices><triangles>'+''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a,b,c in faces)+'</triangles></mesh>'
model = '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" unit="millimeter"><resources>'+''.join(f'<object id="{i}" name="Simulated 3MF body {i}">{mesh_xml}</object>' for i in (1,2))+'</resources><build><item objectid="1"/><item objectid="2" transform="1 0 0 0 1 0 0 0 1 20 0 0"/></build></model>'
project = slicer/'Simulated-Orca.3mf'
with ZipFile(project,'w') as package:package.writestr('3D/3dmodel.model',model)
def fixture_stl(path, offsets):
    with path.open('wb') as output:
        output.write(b'Simulated browser fixture - never printer input'.ljust(80,b'\0'))
        output.write(struct.pack('<I',len(offsets)*len(faces)))
        for offset in offsets:
            for face in faces:
                values=[value for index in face for value in (vertices[index][0]+offset,*vertices[index][1:])]
                output.write(struct.pack('<12fH',0,0,0,*values,0))
fixture_stl(slicer/'body.stl',[0]);fixture_stl(slicer/'plate.stl',[0,20])
manifest = {'schema_version':1,'processor':'3mf-bodies-v1','source_sha256':hashlib.sha256(project.read_bytes()).hexdigest(),'source_name':project.name,'units':'mm','settings':{'layer_height':'0.2'},'build':[],
            'bodies':[{'name':f'Simulated 3MF body {i}','object_id':str(i),'resource':'3D/3dmodel.model','mesh_fingerprint':hashlib.sha256((slicer/'body.stl').read_bytes()).hexdigest(),'display':{'method':'full_mesh'},'geometry_file':'body.stl','instances':[]} for i in (1,2)],
            'plates':[{'id':'1','name':'Simulated plate','geometry_file':'plate.stl','instances':[{'object_id':'1'},{'object_id':'2'}]}]}
(slicer/'manifest.json').write_text(json.dumps(manifest))
