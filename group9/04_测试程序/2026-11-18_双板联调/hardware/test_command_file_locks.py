"""Exercise real Windows file sharing modes while both serial links are simulated."""
from collections import Counter
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import board_session as bs

def hold(path, share, create=False):
    import msvcrt
    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    handle = create_file(str(path), 0xC0000000, share, None, 1 if create else 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDWR), "r+b", buffering=0)

def wait(condition, why):
    until = time.monotonic() + 5
    while not condition():
        if time.monotonic() > until:
            raise AssertionError(why)
        time.sleep(.015)

def exercise():
    observed = bs.RUN / "hardware/logs/dual-live-07/commands/1795025032370982900-001cc7f.json"
    sample = observed.read_bytes() if observed.exists() else b'{"action":"send","side":"both","command":"status"}'
    assert json.loads(sample)["command"] == "status"
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as handles:
        run = Path(temporary) / "run"
        inbox = run / "commands"
        journal = run / "serial.jsonl"
        def rows():
            return [json.loads(l) for l in journal.read_text(encoding="utf8").splitlines() if l]
        polls = Counter()
        writes = []
        held = {}
        errors = []
        class Board:
            def __init__(self, side, log, secrets): self.side = side
            def poll(self): polls[self.side] += 1
            def close(self): pass
            def transmit(self, text, label, rid):
                writes.append((rid,self.side,text))
                if text in ("b","w"):
                    held[rid]=handles.enter_context(hold(inbox/(rid+".working"),1))
                return text != "w"
        def listener():
            try: bs.listen(run)
            except Exception as exc: errors.append(exc)
        def put(stem,request):
            part=inbox/(stem+".part")
            part.write_text(json.dumps(request),encoding="utf8")
            part.rename(inbox/(stem+".json"))
        with patch.object(bs,"BoardLink",Board):
            worker=threading.Thread(target=listener,daemon=True); worker.start()
            wait(lambda:journal.exists() and polls["B"]>0,"collector failed to start")
            locked_read=handles.enter_context(hold(inbox/"001.json",0,True))
            locked_read.write(sample)
            locked_rename=handles.enter_context(hold(inbox/"002.json",1,True))
            locked_rename.write(b'{"action":"send","side":"A","command":"b"}')
            wait(lambda:any(r["kind"]=="command_claim_deferred" and r["data"]=="001.json" for r in rows()),"read lock not observed")
            before=polls.copy();time.sleep(.3)
            assert not writes and all(polls[k]>before[k]+2 for k in ("A","B"))
            locked_read.close()
            wait(lambda:any(r["kind"]=="command_claim_deferred" and r["data"]=="002.json" for r in rows()),"rename lock not observed")
            assert writes==[("001","A","status"),("001","B","status")]
            locked_rename.close()
            wait(lambda:any(r["kind"]=="command_marker_deferred" and r["data"]=="002.working" for r in rows()),"post-send marker lock not observed")
            put("003",dict(action="send",side="B",command="w"))
            wait(lambda:any(r["kind"]=="command_marker_deferred" and r["data"]=="003.working" for r in rows()),"failed marker lock not observed")
            held["002"].close()
            wait(lambda:(inbox/"002.done").exists(),"post-send success marker not recovered")
            # A prolonged lock persists through stop. The marker intent remains in the flushed journal.
            put("004",dict(action="stop",side="both"))
            worker.join(5)
            assert not worker.is_alive() and not errors,errors
            assert writes==[("001","A","status"),("001","B","status"),("002","A","b"),("003","B","w")]
            assert (inbox/"003.working").exists() and (inbox/"004.done").exists()
            assert any(r["kind"]=="command_result" and r["data"]=={"request":"003","marker":".failed"} for r in rows())
            assert any(r["kind"]=="command_markers_pending_at_stop" and r["data"]==["003.working"] for r in rows())
            assert rows()[-1]["kind"]=="stop"
    print("PASS real Windows locks at read/claim/finalize, FIFO, both simulated links kept polling, no replay, persistent marker evidence")
if __name__=="__main__":exercise()
