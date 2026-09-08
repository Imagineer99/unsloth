# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Evaluate measured native frames and make labelled, unscaled screenshot pairs."""
import json
import math
import pathlib
import re
from prepare import MANIFEST

def overlap(a, b):
    return max(0, min(a['x']+a['width'], b['x']+b['width'])-max(a['x'], b['x'])) * max(0, min(a['y']+a['height'], b['y']+b['height'])-max(a['y'], b['y']))

def clearance(record):
    native, facts = record['native'], record['facts']
    if not record['capture_ok'] or not native['window_visible']:
        raise ValueError('Native window/capture unavailable')
    z = native['page_zoom']
    if not math.isfinite(z) or not 0.49 <= z <= 2.01:
        raise ValueError('Invalid native zoom')
    if abs(facts['viewport']['width']*z-native['view_width']) > 2:
        raise ValueError('DOM/native coordinate systems do not agree')
    if len(native['buttons']) != 3 or any(b['hidden'] or b['width'] <= 0 or b['height'] <= 0 for b in native['buttons']):
        raise ValueError('Expected three visible native window buttons')
    css = facts['sidebar_button']
    if css is None or css['width'] <= 0 or css['height'] <= 0:
        raise ValueError('Sidebar button missing')
    button = {k: css[k]*z for k in ['x','y','width','height']}
    intersections = {b['name']: overlap(button, b) for b in native['buttons']}
    return {'native_sidebar_button': button, 'overlap_square_points': intersections,
            'clear': all(area < 0.01 for area in intersections.values())}

def composite(root, name, left_label, right_label):
    from PIL import Image, ImageDraw, ImageStat, ImageFont
    left_path, right_path = root/'base'/f'{left_label}.png', root/'head'/f'{right_label}.png'
    left, right = Image.open(left_path).convert('RGB'), Image.open(right_path).convert('RGB')
    for picture in [left, right]:
        if picture.width < 850 or picture.height < 500 or max(ImageStat.Stat(picture).stddev) < 2:
            raise ValueError('Screenshot dimensions/content do not establish visible window capture')
    if name.startswith('toolbar'):
        # Keep native buttons and navigation legible; preserve the original pixels.
        backing=json.loads((root/'base'/f'{left_label}.json').read_text())['native']['backing_scale']
        height=round(140*backing)
        left=left.crop((0,0,left.width,min(height,left.height)))
        right=right.crop((0,0,right.width,min(height,right.height)))
    canvas = Image.new('RGB', (left.width+right.width, max(left.height,right.height)+82), '#f0f0f0')
    canvas.paste(left,(0,82)); canvas.paste(right,(left.width,82))
    draw = ImageDraw.Draw(canvas)
    font=ImageFont.load_default(size=22)
    draw.text((15,8),f'BEFORE {MANIFEST["base"][:12]} | {left_label}',fill='black',font=font)
    draw.text((left.width+15,8),f'AFTER {MANIFEST["head"][:12]} | {right_label}',fill='black',font=font)
    draw.text((15,42),'Native macOS window; controlled production-component scene; images not resized',fill='black',font=font)
    canvas.save(root/f'{name}.png')

def analyze(root):
    root = pathlib.Path(root)
    findings, measurements = [], {}
    labels = {'base':['appearance100','toolbar100'],
              'head':['appearance100','appearance125','appearance50','appearance200','toolbar100','toolbar125','toolbar50','toolbar200','reload125','reset100']}
    provenance = {}
    dimensions = None
    for side in ['base','head']:
        provenance[side] = json.loads((root/side/'provenance.json').read_text())
        if provenance[side]['sha'] != MANIFEST[side]: raise ValueError('Provenance SHA mismatch')
        result = json.loads((root/side/'result.json').read_text())
        if result.get('status') != 'complete' or result.get('sha') != MANIFEST[side]:
            raise ValueError(f'{side} native scene incomplete: {result}')
        for label in labels[side]:
            record = json.loads((root/side/f'{label}.json').read_text())
            if record['facts']['sha'] != MANIFEST[side] or record['facts']['side'] != side:
                raise ValueError('Capture identity mismatch')
            if not record['capture_ok']: raise ValueError('Missing native screenshot')
            expected_zoom = int(re.search(r'(\d+)$',label)[1])/100
            native = record['native']
            if abs(native['page_zoom']-expected_zoom)>0.001:
                raise ValueError('Snapshot zoom disagrees with its label')
            if abs(native['view_width']-900)>1 or abs(native['view_height']-600)>1:
                raise ValueError('Native scene does not have the prescribed 900x600-point viewport')
            current_dimensions = (native['view_width'],native['view_height'],native['backing_scale'])
            if dimensions is None: dimensions=current_dimensions
            if current_dimensions != dimensions: raise ValueError('Native viewport or display scale changed between sides')
            if label.startswith('toolbar'):
                measured = clearance(record); measurements[f'{side}/{label}'] = measured
                if not measured['clear']: findings.append(f'{side}/{label} overlaps native controls')
            if label.startswith('appearance'):
                if record['facts']['scale_control'] != (side=='head'):
                    raise ValueError('Scale control visibility mismatch')
    if provenance['base']['profile_uuid']==provenance['head']['profile_uuid']:
        raise ValueError('Sides shared native profile')
    if not measurements['base/toolbar100']['clear']:
        raise ValueError('Baseline fails; this does not establish a PR regression')
    for side, label in [('base','preferences100'),('head','preferences100'),('head','preferences125')]:
        record=json.loads((root/side/f'{label}.json').read_text())
        if not record['capture_ok'] or abs(record['facts']['preferences_native_top']-58)>2:
            raise ValueError('Preferences screenshots are not aligned')
        if record['facts']['sha'] != MANIFEST[side]: raise ValueError('Preferences SHA mismatch')
    composite(root,'appearance-before-after','appearance100','appearance100')
    composite(root,'preferences-before-after','preferences100','preferences100')
    composite(root,'preferences-scaling-before-after','preferences100','preferences125')
    composite(root,'toolbar-before-after','toolbar100','toolbar125')
    result = {'pr':9666,'base':MANIFEST['base'],'head':MANIFEST['head'],
              'expect':MANIFEST['expect'],'scope':MANIFEST['evidence_scope'],
              'status':'regression' if findings else 'pass', 'findings':findings,
              'measurements':measurements,'provenance':provenance,
              'visual_review':'pending human inspection; not approved for public comment'}
    (root/'meta.json').write_text(json.dumps(result,indent=2))
    return result
