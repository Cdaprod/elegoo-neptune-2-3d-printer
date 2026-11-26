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
