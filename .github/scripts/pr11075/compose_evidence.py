# SPDX-License-Identifier: AGPL-3.0-only
import json,pathlib,os
from PIL import Image,ImageDraw,ImageFont
root=pathlib.Path(os.environ.get('PR11075_ROOT',pathlib.Path(__file__).resolve().parent))
out=root/'ui-evidence'
font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',19)
small=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',14)
for surface in ['main','compare']:
 before=Image.open(out/('before-'+surface+'-entry.png'))
 after=Image.open(out/('after-'+surface+'-entry.png'))
 # Fixed crops of the observed toast and dialog; no rescaling or modification of their contents.
 left=before.crop((818,40,1198,143))
 right=after.crop((335,186,865,716))
 canvas=Image.new('RGB',(1000,670),'#f4f6f8');d=ImageDraw.Draw(canvas)
 d.text((20,15),'PR #11075 | '+surface.title()+' | HTTP LAN',font=font,fill='#152b35')
 d.text((20,50),'BEFORE  f9bffe2658',font=font,fill='#152b35')
 d.text((450,50),'AFTER  9482694959',font=font,fill='#152b35')
 canvas.paste(left,(20,92));canvas.paste(right,(450,85))
 d.text((20,225),'Direct microphone unavailable.',font=small,fill='#152b35')
 d.text((20,251),'No recording-file dialog.',font=small,fill='#152b35')
 d.text((20,622),'Real Studio UI. Controlled STT readiness/response; no actual ASR or native mobile capture.',font=small,fill='#152b35')
 canvas.save(out/(surface+'-comparison.png'))
meta=json.loads((out/'meta.json').read_text())
meta['artifacts']=['main-comparison.png','compare-comparison.png','before-main-entry.png','after-main-entry.png','before-compare-entry.png','after-compare-entry.png','after-main-success.png','after-compare-success.png']
(out/'meta.json').write_text(json.dumps(meta,indent=2))
