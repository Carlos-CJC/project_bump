"""Reproduce a disappearing COM port, double-check loss isolation and uncertain writes."""
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import board_session as bs

def run():
    log=StringIO()
    def rows(): return [json.loads(line) for line in log.getvalue().splitlines()]
    ports={'A':'COM13','B':'COM14'}
    buffers={}
    fail_write=set()
    writes=[]
    class Port:
        def __init__(self,name,*args,**kwargs): self.port=name
        @property
        def in_waiting(self):
            if self.port not in ports.values():raise OSError('ClearCommError failed (WinError 22)')
            return len(buffers.get(self.port,b''))
        def read(self,n):
            incoming=buffers.pop(self.port,b'')
            return incoming[:n]
        def write(self,payload):
            writes.append((self.port,payload))
            if self.port in fail_write: raise OSError('port disconnected during write')
            return len(payload)
        def close(self): pass
    def devices():
        return [SimpleNamespace(serial_number=bs.USB[s], device=n,vid=0x2341,pid=0x005e) for s,n in ports.items()]
    def status(boot):return json.dumps(dict(type='status',boot_id=boot)).encode()+b'\n'
    with patch.object(bs,'comports',devices),patch.object(bs.serial,'Serial',Port):
        a,b=(bs.BoardLink(side,log,set()) for side in ('A','B'))
        a.poll();b.poll()
        buffers.update(COM13=status('a0'),COM14=status('b0'));a.poll();b.poll()
        buffers['COM13']=b'partial';a.poll();del ports['A'];a.poll()
        buffers['COM14']=status('b0');b.poll()
        assert not a.transmit('b','b','lost_request')
        ports['A']='COM17'; a.retry_at=0;a.poll()
        buffers['COM17']=status('a1');a.poll()
        fail_write.add('COM17');assert not a.transmit('b','b','ambiguous_request')
        fail_write.clear();a.retry_at=0;a.poll();a.status_at=0;a.poll()
        buffers['COM17']=status('a2');a.poll();b.poll()
    r=rows()
    assert [x['data']['boot_id'] for x in r if x['kind']=='first_status' and x['board']=='A']==['a0','a1','a2']
    assert [x['data'] for x in r if x['board']=='A' and x['kind']=='reopen']==['COM17','COM17']
    assert sum(x['kind']=='lost' and x['board']=='A' for x in r)==2
    assert all(x['kind']!='lost' for x in r if x['board']=='B')
    assert sum(x['kind']=='rx' for x in r if x['board']=='B')==2
    assert [v for _,v in writes].count(b'b\n')==1
    assert [x['kind'] for x in r if x['kind'] in ('tx_skipped','tx_uncertain')]==['tx_skipped','tx_uncertain']
    assert not any('partial' in str(x['data']) for x in r)
    print('PASS independent loss/reopen, serial-number to new COM port, partial-line discard, first status, uncertain write attempted once, healthy board continuity')
if __name__=='__main__':run()
