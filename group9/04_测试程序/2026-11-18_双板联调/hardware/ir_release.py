from ir_baseline import BASE, IDS, ports, now
import json, time
from contextlib import ExitStack
from datetime import datetime
import serial

path=BASE / 'logs' / ('ir_release_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.jsonl')
with path.open('x',encoding='utf8') as out, ExitStack() as stack:
    found=ports()
    boards={k:stack.enter_context(serial.Serial(found[v],115200,timeout=.05)) for k,v in IDS.items()}
    pending={k:b'' for k in boards}
    states={}
    def record(side,kind,data):
        out.write(json.dumps(dict(utc=now(),board=side,kind=kind,data=data))+'\n'); out.flush()
        if kind=='tx' or any(x in data for x in ('STATUS','PAIRED','BYE','unpair')):
            print(side,kind,data,flush=True)
    def send(side,char):
        boards[side].write(char.encode()); boards[side].flush(); record(side,'tx',char)
    def observe(duration,expected=None):
        until=time.monotonic()+duration
        while time.monotonic()<until:
            for side,p in boards.items():
                if p.in_waiting: pending[side]+=p.read(p.in_waiting)
                while b'\n' in pending[side]:
                    raw,pending[side]=pending[side].split(b'\n',1)
                    line=raw.decode(errors='replace').strip()
                    if not line: continue
                    record(side,'rx',line)
                    if 'state=' in line:
                        states[side]=line.split('state=',1)[1].split()[0].rstrip(']')
            if expected and all(states.get(side)==expected for side in boards): return True
            time.sleep(.02)
        return False
    for side in boards: send(side,'s')
    observe(.5)
    for side in boards: send(side,'b')
    if observe(18,'PAIRED'):
        for side in boards: send(side,'s')
        observe(.5);send('B','w');observe(3)
        for side in boards: send(side,'s')
        observe(1)
        print('states after B-only stop:',states,flush=True)
    for side in boards: send(side,'w')
    observe(1)
    for side in boards: send(side,'s')
    observe(.5)
print(path)
