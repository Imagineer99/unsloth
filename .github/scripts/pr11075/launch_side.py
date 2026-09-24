# SPDX-License-Identifier: AGPL-3.0-only
import json,os,pathlib,socket,subprocess,sys
root=pathlib.Path(os.environ.get('PR11075_ROOT',pathlib.Path(__file__).resolve().parent)).resolve()
side=sys.argv[1]; tree=root/('head' if side=='after' else 'base')
home=root/'ui-evidence'/side/'home';home.mkdir(parents=True,exist_ok=True)
sock=socket.socket();sock.bind(('0.0.0.0',0));port=sock.getsockname()[1];sock.close()
env=dict(os.environ,UNSLOTH_STUDIO_HOME=str(home),HF_HOME=str(home/'hf'),XDG_CACHE_HOME=str(home/'cache'),UNSLOTH_STUDIO_DISABLE_TORCH_WARM='1',UNSLOTH_DISABLE_UPDATE_CHECK='1',UNSLOTH_DISABLE_MLX_AUTOREPAIR='1',UNSLOTH_STUDIO_DISABLE_DEVICE_PROBE='1',UNSLOTH_ALLOW_CPU='1')
log=open(home.parent/'server.log','w')
p=subprocess.Popen([sys.executable,str(tree/'studio/backend/run.py'),'--host','0.0.0.0','--port',str(port),'--frontend',str(tree/'studio/frontend/dist'),'--no-cloudflare','--silent'],cwd=tree,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
meta=dict(side=side,sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=tree,text=True).strip(),pid=p.pid,port=port,home=str(home),tree=str(tree))
(home.parent/'launch.json').write_text(json.dumps(meta,indent=2))
print(json.dumps(meta))
