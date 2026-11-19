from pathlib import Path
from datetime import datetime, timezone
import json, sys, time
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parents[1] / '2026-11-15_单板调试与进度报告/tools/pydeps'))
import serial
from serial.tools.list_ports import comports
IDS = {'A':'0617355018835E82', 'B':'416523501C5F96C8'}
def ports():
    return {p.serial_number:p.device for p in comports() if (p.vid,p.pid)==(0x2341,0x005e)}
def now():
    return datetime.now(timezone.utc).isoformat()
def test():
    found=ports()
    serials={}
    path=BASE / 'logs' / ('ir_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.jsonl')
    with path.open('x',encoding='utf8') as log:
        def record(side,kind,data):
            entry=dict(utc=now(),board=side,kind=kind,data=data)
            log.write(json.dumps(entry,ensure_ascii=False)+'\n'); log.flush()
            if kind=='rx' and 'state=IDLE my' in data: return
            print(side,kind,data,flush=True)
        def observe(seconds):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                for side,s in serials.items():
                    if s.in_waiting:
                        for data in s.read(s.in_waiting).decode(errors='replace').splitlines():
                            if data: record(side,'rx',data)
                time.sleep(.035)
        def send(side,cmd):
            serials[side].write(cmd.encode()); serials[side].flush(); record(side,'tx',cmd)
        try:
            for side,uid in IDS.items():
                s=serial.Serial(found[uid],115200,timeout=.12); serials[side]=s
                record(side,'identity',dict(serial=uid,port=s.port))
            observe(.4)
            for side in IDS: send(side,'s')
            observe(1)
            for side in IDS: send(side,'b')
            observe(10)
            for side in IDS: send(side,'s')
            observe(.6); send('A','w'); observe(3)
            for side in IDS: send(side,'s')
            observe(.6); send('B','w'); observe(.5)
        finally:
            for s in serials.values(): s.close()
    print(path)
if __name__=='__main__': test()
