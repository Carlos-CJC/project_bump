"""Install one agreed BumpDualBadge UF2 after validating the two original backups."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import struct
import time

import original_backup as hw
import serial

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def validated_image(file, expected, required_marker=b'BumpDualBadge'):
    raw=file.read_bytes()
    if digest(raw)!=expected.lower() or not raw or len(raw)%512:
        raise ValueError('UF2 hash or length mismatch')
    blocks=len(raw)//512
    contents={}
    addresses=set()
    for offset in range(0,len(raw),512):
        block=raw[offset:offset+512]
        m1,m2,flags,addr,size,number,total,family=struct.unpack_from('<8I',block)
        if (m1,m2,struct.unpack_from('<I',block,508)[0])!=(0x0a324655,0x9e5d5157,0x0ab16f30):
            raise ValueError('Unexpected UF2 framing')
        if flags!=0x2000 or family!=0xe48bff56 or size!=256 or total!=blocks:
            raise ValueError('Unexpected RP2040 UF2 format')
        if not 0x10000000<=addr<=0x10ffff00 or addr%256 or addr in addresses or number>=total or number in contents:
            raise ValueError('Invalid or duplicate UF2 flash range')
        contents[number]=block[32:32+size]
        addresses.add(addr)
    if 0x10000000 not in addresses or required_marker not in b''.join(contents[n] for n in range(blocks)):
        raise ValueError('Expected firmware identity missing')
    return raw

def check_originals():
    for tag in hw.IDS:
        manifest=json.loads((hw.PRIVATE/f'original_{tag}.json').read_text(encoding='utf8'))
        image=(hw.PRIVATE/manifest['image']).read_bytes()
        if len(image)!=16777216 or digest(image)!=manifest['sha256'] or manifest['usb_serial']!=hw.IDS[tag]:
            raise RuntimeError(f'Original backup identity/integrity check failed on {tag}')
        if not manifest['device_verify'] or not manifest['boot_verified']:
            raise RuntimeError(f'Original backup verification incomplete on {tag}')

def capture(tag,folder):
    transcript=[]
    status=None
    with serial.Serial(hw.access(tag),115200,timeout=.08) as board:
        until=time.monotonic()+18
        next_query=time.monotonic()+1
        while time.monotonic()<until:
            if time.monotonic()>next_query:
                board.write(b'status\n'); board.flush(); next_query=time.monotonic()+1
            line=board.readline().decode(errors='replace').strip()
            if not line: continue
            transcript.append(dict(utc=hw.now(),text=line))
            try: record=json.loads(line)
            except ValueError: continue
            if record.get('type')=='status' and record.get('firmware')=='BumpDualBadge':
                if (record.get('self_code16','').lower()==hw.CODES[tag][2:].lower()
                        and re.fullmatch('bump-[0-9a-f]{12}',record.get('device_id',''))
                        and record.get('imu_ready') and record.get('imu_samples',0)>0):
                    status=record
                    break
    (folder/'serial.json').write_text(json.dumps(transcript,indent=2),encoding='utf8')
    if status is None: raise RuntimeError(f'Missing integrated status/device/IMU identity on {tag}')
    return status

def install(tag,source,sha):
    raw=validated_image(source,sha)
    check_originals()
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    folder=hw.PRIVATE/f'install_{tag}_{stamp}'
    folder.mkdir()
    staged=folder/'candidate.uf2'
    staged.write_bytes(raw)
    hw.select(tag,'app'); hw.enter(tag); device=hw.wait_mode(tag,'rom')
    info=hw.pico(['info','-a'],folder/'rom.txt')
    if '16384K' not in info: raise RuntimeError('Unexpected chip flash size')
    rel=str(staged.relative_to(hw.PRIVATE))
    hw.pico(['load','-v',rel],folder/'load.txt')
    hw.select(tag,'rom')
    verify=hw.pico(['verify',rel],folder/'verify.txt')
    if 'OK' not in verify: raise RuntimeError('Explicit read-back comparison failed')
    hw.select(tag,'rom'); hw.pico(['reboot','-a'],folder/'restart.txt')
    hw.wait_mode(tag,'app')
    status=capture(tag,folder)
    report=dict(utc=hw.now(),board=tag,source=str(source),sha256=sha,usb_serial=hw.IDS[tag],rom=device,readback=True,status=status)
    (folder/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(dict(board=tag,sha256=sha,status=status),ensure_ascii=False),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('uf2',type=Path)
    parser.add_argument('sha256')
    parser.add_argument('board',choices=['A','B'])
    args=parser.parse_args()
    install(args.board,args.uf2.resolve(),args.sha256.lower())


