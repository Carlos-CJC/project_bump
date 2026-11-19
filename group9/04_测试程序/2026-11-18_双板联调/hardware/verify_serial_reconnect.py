from pathlib import Path
from datetime import datetime
import json,sys,time,subprocess
import original_backup as hw

ops=hw.BASE.parent/'ops/board_session.py'
folder=hw.BASE/'logs'/('collector_reconnect_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
def action(*args):
    cmd=[sys.executable,'-B',str(ops),args[0],str(folder),*args[1:]]
    subprocess.run(cmd,check=True,timeout=5,capture_output=True)
def rows():
    p=folder/'serial.jsonl'
    if not p.exists():return []
    rs=[]
    for l in p.read_text(encoding='utf8').splitlines():
        try:rs.append(json.loads(l))
        except ValueError:continue
    return rs
def await_event(check,timeout=12):
    stop=time.monotonic()+timeout
    while time.monotonic()<stop:
        r=rows()
        if check(r):return r
        time.sleep(.14)
    raise TimeoutError('Expected event did not appear')
def first(rs,side):return [r['data'] for r in rs if r['board']==side and r['kind']=='first_status']
def count(rs,side,kind):return sum(r['board']==side and r['kind']==kind for r in rs)
def main():
    p=subprocess.Popen([sys.executable,'-B',str(ops),'listen',str(folder)])
    rom=False
    try:
        r=await_event(lambda r:first(r,'A') and first(r,'B'))
        old={s:first(r,s)[0] for s in hw.IDS}
        assert all(d['queued']==0 and not d['discovery'] and d['fw']=='0.5.2-dual' for d in old.values())
        hw.select('A','app'); action('send','A','t')
        hw.wait_mode('A','rom');rom=True
        await_event(lambda r:count(r,'A','lost')==1)
        action('send','both','status')
        r=await_event(lambda r:count(r,'A','tx_skipped')>0 and any(x['kind']=='tx' and x['board']=='B' and x['data']=='status' and x['utc']>next(z['utc'] for z in r if z['kind']=='lost') for x in r))
        time.sleep(1)
        hw.select('A','rom')
        hw.pico(['reboot','-a'],folder/'A_reboot.txt');rom=False
        r=await_event(lambda rs:len(first(rs,'A'))==2,20)
        action('send','B','status');time.sleep(.5)
        r=rows();lost=next(x['utc'] for x in r if x['kind']=='lost' and x['board']=='A')
        opened=next(x['utc'] for x in r if x['kind']=='reopen' and x['board']=='A')
        healthy=[]
        for x in r:
            if x['kind']=='rx' and x['board']=='B' and lost<x['utc']<opened:
                try:d=json.loads(x['data'])
                except ValueError:continue
                if d.get('type')=='status': healthy.append(d)
        assert healthy and all(d['boot_id']==old['B']['boot_id'] for d in healthy)
        assert first(r,'A')[-1]['boot_id']!=old['A']['boot_id']
        assert count(r,'B','lost')==0 and count(r,'B','reopen')==0
        assert sum(x['board']=='A' and x['kind']=='tx' and x['data']=='t' for x in r)==1
        result=dict(utc=hw.now(),initial=old,first_A_after_reopen=first(r,'A')[-1],A_lost_utc=lost,A_reopen_utc=opened,healthy_B_samples_during_A_outage=len(healthy),B_boot_during_A_outage=healthy[-1]['boot_id'],A_t_command_writes=1,offline_request='tx_skipped',other_board_disconnects=0)
        (folder/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print(folder.name,'PASS','A from',old['A']['boot_id'],'to',first(r,'A')[-1]['boot_id'],'B unchanged',old['B']['boot_id'],'status samples during A outage:',len(healthy),flush=True)
    finally:
        if rom:
            hw.select('A','rom');hw.pico(['reboot','-a'],folder/'A_final_reboot.txt')
        if p.poll() is None and (folder/'commands').exists():
            action('stop');p.wait(timeout=6)
if __name__=='__main__':main()
