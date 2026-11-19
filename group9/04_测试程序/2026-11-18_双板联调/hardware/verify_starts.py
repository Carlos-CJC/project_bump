"""Check independent boots on the current 0.5.2 installation without rewriting flash."""
import json
import re
from datetime import datetime
from pathlib import Path
import time
import original_backup as hw
import install_candidate as candidate
import serial

VERSION='0.5.2-dual'

def check():
    folder=hw.PRIVATE/('restart_regression_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    folder.mkdir()
    out=hw.BASE/'logs'/'startup_regression_0.5.2.json'
    prior={s:[] for s in hw.IDS}
    initial={}
    for path in sorted(hw.PRIVATE.glob('install_*/result.json')):
        result=json.loads(path.read_text(encoding='utf8'))
        side=result['board']; status=result['status']
        prior[side].append(status)
        if status['fw']==VERSION: initial[side]=status
    assert set(initial)==set(hw.IDS), 'Both expected installations must be present'
    starts={s:[d] for s,d in initial.items()}
    def persist():
        out.write_text(json.dumps(dict(utc=hw.now(),version=VERSION,starts=starts),indent=2),encoding='utf8')
    persist()
    for i in range(1,4):
        for side in hw.IDS:
            stage=folder/f'{i}_{side}';stage.mkdir()
            hw.select(side,'app'); hw.enter(side); hw.wait_mode(side,'rom')
            info=hw.pico(['info','-a'],stage/'rom.txt'); assert '16384K' in info
            hw.select(side,'rom'); hw.pico(['reboot','-a'],stage/'restart.txt'); hw.wait_mode(side,'app')
            status=candidate.capture(side,stage)
            if not status.get('nina_version'):
                end=time.monotonic()+4
                with serial.Serial(hw.access(side),115200,timeout=.3) as port:
                    while time.monotonic()<end:
                        port.write(b'status\n'); port.flush()
                        try: d=json.loads(port.readline())
                        except ValueError: continue
                        if d.get('type')=='status' and d.get('nina_version'):
                            assert d['boot_id']==status['boot_id']
                            status=d;break
            starts[side].append(status); persist()
            assert status['fw']==VERSION and status['device_id']==initial[side]['device_id']
            assert re.fullmatch('[0-9a-f]{8}',status['boot_id']) and re.fullmatch('[0-9a-f]{12}',status['self_short_id'])
            for field in ('boot_id','self_short_id'):
                earlier=[row[field] for key,rows in prior.items() for row in rows]
                earlier.extend(row[field] for key,rows in starts.items() for row in (rows[:-1] if key==side else rows))
                assert status[field] not in earlier, f'{side}: repeated {field}'
            print(side,'restart',i,status['boot_id'],status['self_short_id'],status.get('nina_version'),flush=True)
    print(out.name)
if __name__=='__main__':check()
