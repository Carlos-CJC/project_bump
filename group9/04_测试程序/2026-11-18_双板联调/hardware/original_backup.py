from ir_baseline import BASE, IDS, ports, now
from pathlib import Path
import sys, hashlib, json, subprocess, time
import serial

PICO = Path(r'E:\港理课程\网站维护\任务工作记录\2026-10-23_eie568_lab1\toolchain\data\packages\arduino\tools\rp2040tools\1.0.6\picotool.exe')
PRIVATE = BASE / 'private'
CHIPS = {'A':'50351706825E8318','B':'50236541C8965F1C'}
CODES = {'A':'0xA30F','B':'0xC371'}
UID_LOC = {'A':'PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)#USB(1)#USB(1)','B':'PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)#USB(1)#USB(2)'}
def usb():
    import ctypes as c
    from serial.tools import list_ports_windows as win
    h=win.SetupDiGetClassDevs(None,'USB',None,win.DIGCF_PRESENT | 4)
    device=win.SP_DEVINFO_DATA()
    device.cbSize=c.sizeof(device)
    entries=[]
    try:
        i=0
        while win.SetupDiEnumDeviceInfo(h,i,c.byref(device)):
            i+=1
            ident=c.create_unicode_buffer(1024)
            if not win.SetupDiGetDeviceInstanceId(h,c.byref(device),ident,len(ident),None): continue
            parts=ident.value.upper().split(chr(92))
            if len(parts)!=3 or parts[1] not in ('VID_2341&PID_005E','VID_2E8A&PID_0003'): continue
            loc=c.create_unicode_buffer(2048)
            if not win.SetupDiGetDeviceRegistryProperty(h,c.byref(device),win.SPDRP_LOCATION_PATHS,None,c.byref(loc),c.sizeof(loc),None):
                raise c.WinError()
            entries.append(dict(id=ident.value,loc=loc.value))
        return entries
    finally: win.SetupDiDestroyDeviceInfoList(h)
def select(side,mode):
    entries=usb(); other='B' if side=='A' else 'A'
    assert any(x['id'].endswith(IDS[other]) and x['loc']==UID_LOC[other] for x in entries),entries
    rom=[x for x in entries if 'VID_2E8A&PID_0003' in x['id']]
    app=[x for x in entries if x['id'].endswith(IDS[side]) and x['loc']==UID_LOC[side]]
    assert (mode=='rom' and not app and len(rom)==1 and rom[0]['loc']==UID_LOC[side]) or (mode=='app' and len(app)==1 and not rom),entries
    return entries
def wait_mode(side,mode):
    deadline=time.monotonic()+20
    while True:
        try: return select(side,mode)
        except AssertionError:
            if time.monotonic()>=deadline: raise
            time.sleep(.25)

def pico(args,log,timeout=150):
    r=subprocess.run([str(PICO),*args],cwd=PRIVATE,capture_output=True,timeout=timeout)
    data=(r.stdout+r.stderr).decode(errors='replace')
    log.write_text(data,encoding='utf8')
    if r.returncode: raise RuntimeError(f'{args[0]}: {data[-450:]}')
    return data
def access(side):
    return ports()[IDS[side]]
def enter(side):
    with serial.Serial(access(side),1200,timeout=.2) as port:
        port.dtr=True;time.sleep(.12);port.dtr=False
    time.sleep(1)
def live(side):
    with serial.Serial(access(side),115200,timeout=.12) as port:
        end=time.monotonic()+5; data=b''
        while time.monotonic()<end:
            port.write(b's');port.flush();time.sleep(.25);data+=port.read(port.in_waiting or 1)
            if CHIPS[side].encode() in data and CODES[side].encode() in data and b'STATUS state=IDLE' in data: break
    return data.decode(errors='replace')
def back(side):
    manifest=PRIVATE/f'original_{side}.json'
    image=PRIVATE/f'original_{side}.bin'
    if image.exists() or manifest.exists(): raise RuntimeError('Refusing to replace a previous backup')
    source=select(side,'app')
    enter(side); rom=wait_mode(side,'rom')
    info=pico(['info','-a'],PRIVATE/f'{side}_rom.log')
    assert '16384K' in info, info
    print(side,'ROM checked, reading 16 MiB',flush=True)
    pico(['save','-a',image.name],PRIVATE/f'{side}_save.log')
    assert image.stat().st_size==16*1024*1024
    compare=pico(['verify',image.name,'-o','0x10000000'],PRIVATE/f'{side}_verify.log')
    assert 'OK' in compare
    digest=hashlib.sha256(image.read_bytes()).hexdigest()
    select(side,'rom')
    pico(['reboot','-a'],PRIVATE/f'{side}_reboot.log')
    time.sleep(1)
    after=wait_mode(side,'app'); banner=live(side)
    (BASE/'logs'/f'original_{side}_reboot.txt').write_text(banner,encoding='utf8')
    assert CHIPS[side] in banner and CODES[side] in banner and 'STATUS state=IDLE' in banner,banner
    manifest.write_text(json.dumps(dict(at_utc=now(),board=side,usb_serial=IDS[side],chip_uid=CHIPS[side],code=CODES[side],before=source,bootrom=rom,after=after,image=image.name,bytes=image.stat().st_size,sha256=digest,device_verify=True,boot_verified=True,scope='RP2040 external flash; separate NINA unchanged'),indent=2),encoding='utf8')
    print(side,'full read-back verified, original IR firmware restarted',digest,flush=True)
if __name__=='__main__':
    for side in sys.argv[1:]: back(side)


