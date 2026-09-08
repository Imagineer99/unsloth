# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
import copy
import unittest
import json
import pathlib
import tempfile
from unittest.mock import patch
from analyze import analyze, clearance, overlap
from prepare import MANIFEST

def sample(zoom=1, x=84, y=9, width=30, height=30):
    return {'capture_ok':True,'native':{
        'page_zoom':zoom,'view_width':900,'view_height':600,'window_visible':True,'backing_scale':2,
        'buttons':[{'name':name,'x':14+i*20,'y':20,'width':12,'height':12,'hidden':False}
                   for i,name in enumerate(['close','minimize','zoom'])]},
        'facts':{'viewport':{'width':900/zoom,'dpr':2},
                 'sidebar_button':{'x':x,'y':y,'width':width,'height':height}}}

class GeometryTests(unittest.TestCase):
    def test_touching_edges_have_no_area(self):
        self.assertEqual(overlap({'x':0,'y':0,'width':10,'height':10},{'x':10,'y':0,'width':10,'height':10}),0)
    def test_control_case_is_clear(self):
        self.assertTrue(clearance(sample())['clear'])
    def test_exact_125_percent_mobile_shape_fails(self):
        result=clearance(sample(1.25,8,11,34,34))
        self.assertFalse(result['clear'])
        self.assertEqual(result['overlap_square_points']['close'],144)
    def test_retina_factor_does_not_distort_point_geometry(self):
        record=sample(); record['native']['backing_scale']=3;record['facts']['viewport']['dpr']=3
        self.assertEqual(clearance(record)['native_sidebar_button']['x'],84)
    def test_200_percent_desktop_inset_is_clear(self):
        self.assertTrue(clearance(sample(2,45,9,30,30))['clear'])
    def test_missing_or_hidden_native_controls_are_incomplete(self):
        for change in ['missing','hidden','zero']:
            record=sample()
            if change=='missing':record['native']['buttons'].pop()
            elif change=='hidden':record['native']['buttons'][0]['hidden']=True
            else:record['native']['buttons'][0]['width']=0
            with self.assertRaises(ValueError):clearance(record)
    def test_missing_dom_button_is_incomplete(self):
        record=sample();record['facts']['sidebar_button']=None
        with self.assertRaises(ValueError):clearance(record)
    def test_failed_capture_is_not_regression(self):
        record=sample();record['capture_ok']=False
        with self.assertRaises(ValueError):clearance(record)
    def test_mismatched_coordinate_system_is_not_regression(self):
        record=sample(1.25);record['facts']['viewport']['width']=900
        with self.assertRaises(ValueError):clearance(record)

class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent)
        self.root=pathlib.Path(self.temporary.name)
        for side in ['base','head']:
            directory=self.root/side;directory.mkdir()
            (directory/'provenance.json').write_text(json.dumps({'sha':MANIFEST[side],'profile_uuid':side}))
            (directory/'result.json').write_text(json.dumps({'status':'complete','sha':MANIFEST[side]}))
            labels=['appearance100','toolbar100']
            if side=='head':labels+=['appearance125','appearance50','appearance200','toolbar125','toolbar50','toolbar200','reload125','reset100']
            for label in labels:
                pct=int(''.join(c for c in label if c.isdigit()))
                record=sample(pct/100,x=84/(pct/100))
                record['facts'].update(side=side,sha=MANIFEST[side],scale_control=side=='head')
                (directory/f'{label}.json').write_text(json.dumps(record))
        self.composer=patch('analyze.composite');self.composer.start()
    def tearDown(self):self.composer.stop();self.temporary.cleanup()
    def alter(self,side,label,change):
        path=self.root/side/f'{label}.json';value=json.loads(path.read_text());change(value);path.write_text(json.dumps(value))
    def test_clean_native_measurements_pass(self):
        self.assertEqual(analyze(self.root)['status'],'pass')
    def test_head_overlap_has_measured_regression_verdict(self):
        self.alter('head','toolbar125',lambda r:r['facts'].update(sidebar_button={'x':8,'y':11,'width':34,'height':34}))
        self.assertEqual(analyze(self.root)['findings'],['head/toolbar125 overlaps native controls'])
    def test_baseline_failure_does_not_become_pr_regression(self):
        self.alter('base','toolbar100',lambda r:r['facts'].update(sidebar_button={'x':8,'y':11,'width':34,'height':34}))
        with self.assertRaisesRegex(ValueError,'Baseline fails'):analyze(self.root)
    def test_mislabeled_zoom_is_incomplete(self):
        self.alter('head','appearance125',lambda r:r['native'].update(page_zoom=1))
        with self.assertRaisesRegex(ValueError,'label'):analyze(self.root)
    def test_wrong_source_capture_is_incomplete(self):
        self.alter('head','appearance125',lambda r:r['facts'].update(sha=MANIFEST['base']))
        with self.assertRaisesRegex(ValueError,'identity'):analyze(self.root)
    def test_shared_profile_is_incomplete(self):
        path=self.root/'head/provenance.json';value=json.loads(path.read_text());value['profile_uuid']='base';path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'shared native profile'):analyze(self.root)

if __name__=='__main__':unittest.main()
