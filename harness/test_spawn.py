# SPDX-License-Identifier: AGPL-3.0-only
import os, sys, queue, threading, random, multiprocessing as mp
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(os.environ['REVIEW_BACKEND']).resolve()))
from core.inference.orchestrator import InferenceOrchestrator as O

def host():
    o = O.__new__(O)
    o._mailbox_lock = threading.Lock()
    o._mailboxes = {}
    o._direct_mailboxes = {}
    o._request_cancel_events = {}
    o._active_cancel_lock = threading.Lock()
    o._active_cancel_events = []
    o._executing_cancel_events = []
    o._dispatcher_thread = None
    o._dispatcher_stop = threading.Event()
    o._resp_queue = queue.Queue()
    o._proc = object()
    o._ensure_subprocess_alive = lambda: True
    return o

def produce(q):
    for i in range(100):
        q.put(dict(request_id='old', type='token', text=str(i)))
    q.put(dict(request_id='mine', type='token', text='NEW'))
    q.put(dict(request_id='mine', type='gen_done'))

def test_real_spawn_process_queue():
    o = host()
    ctx = mp.get_context('spawn')
    q = ctx.Queue()
    o._resp_queue = q
    p = ctx.Process(target=produce, args=(q,))
    p.start()
    read, drain, release = o._direct_reader('mine')
    try:
        assert list(o._consume_token_stream(read, drain, crash_context='generation')) == ['NEW']
    finally:
        release()
        p.join(5)
        if p.is_alive():
            p.terminate()
            p.join(5)
        q.close()
        q.join_thread()
    assert p.exitcode == 0
