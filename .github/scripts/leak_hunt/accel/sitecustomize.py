"""PYTHONPATH entry point for leak_hunt_accel; chains to any other sitecustomize on sys.path."""

import importlib.util
import os
import sys

try:
    import leak_hunt_accel  # noqa: F401
except Exception:
    pass

_here = os.path.dirname(os.path.realpath(__file__))
for _entry in list(sys.path):
    try:
        if not _entry or os.path.realpath(_entry) == _here:
            continue
        _cand = os.path.join(_entry, "sitecustomize.py")
        if os.path.isfile(_cand):
            _spec = importlib.util.spec_from_file_location(
                "_leak_hunt_chained_sitecustomize", _cand
            )
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            break
    except Exception:
        break
