# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util,sys,json,tempfile
from pathlib import Path
root=Path(__file__).resolve().parent
results={}
for rev in ('base','head'):
    sys.path.insert(0,str(root/rev/'studio'))
    spec=importlib.util.spec_from_file_location('node_'+rev,root/rev/'studio/install_node_prebuilt.py')
    m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
    host=m.HostInfo(system='Linux',machine='x64',node_os='linux',node_arch='x64',archive_ext='.tar.gz',is_windows=False)
    with tempfile.TemporaryDirectory(dir=root,prefix='node-ab-') as directory:
        d=Path(directory);node=m.node_binary_path(d,host);npm=m.npm_cli_path(d,host)
        node.parent.mkdir(parents=True,exist_ok=True);npm.parent.mkdir(parents=True,exist_ok=True)
        node.write_bytes(b'fixture-node');node.chmod(0o755);npm.write_bytes(b'fixture-npm')
        m.write_metadata(d,version='24.17.0',asset='fixture',sha256='fixture')
        calls=[]
        m.installed_node_version=lambda *a:(calls.append('node -v'),'24.17.0')[1]
        m.installed_npm_major=lambda *a:(calls.append('npm --version'),11)[1]
        assert m.existing_install_matches(d,host,version='24.17.0')
        first=list(calls);calls.clear()
        assert m.existing_install_matches(d,host,version='24.17.0')
        results[rev]={'first_check':first,'second_check':list(calls)}
        m.installed_npm_major=lambda *a:None
        assert not m.existing_install_matches(d,host,version='24.17.0')
        results[rev]['broken_npm_rejected']=True
    sys.path.pop(0)
assert results['base']['second_check']==['node -v','npm --version']
assert results['head']['second_check']==['npm --version']
print(json.dumps(results,indent=2))
