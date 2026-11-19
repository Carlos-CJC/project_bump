"""Install a SHA-pinned diagnostic UF2 on A while confirming B stays at its own USB port."""
import argparse
from datetime import datetime
from pathlib import Path
import json
import original_backup as hw
import install_candidate as candidate

def install(path,sha,marker,variant):
    data=candidate.validated_image(path,sha,marker.encode('ascii'))
    candidate.check_originals()
    stage=hw.PRIVATE/('diagnostic_A_'+variant+'_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    stage.mkdir()
    image=stage/'candidate.uf2'
    image.write_bytes(data)
    baseline=hw.select('A','app')
    hw.enter('A')
    rom=hw.wait_mode('A','rom')
    assert '16384K' in hw.pico(['info','-a'],stage/'rom.txt')
    target=str(image.relative_to(hw.PRIVATE))
    hw.pico(['load','-v',target],stage/'load.txt')
    hw.select('A','rom')
    assert 'OK' in hw.pico(['verify',target],stage/'verify.txt')
    hw.select('A','rom')
    hw.pico(['reboot','-a'],stage/'reboot.txt')
    hw.wait_mode('A','app')
    proof=dict(utc=hw.now(),board='A',variant=variant,bytes=len(data),sha256=sha.lower(),source=str(path),required_marker=marker,device_verified=True,before=baseline,verified_bootrom=rom)
    (stage/'install.json').write_text(json.dumps(proof,indent=2),encoding='utf8')
    print('A diagnostic loaded and flash verified',variant,len(data),sha,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('artifact',type=Path)
    p.add_argument('sha')
    p.add_argument('marker')
    p.add_argument('variant',choices=['stock','batch'])
    a=p.parse_args(); install(a.artifact.resolve(),a.sha,a.marker,a.variant)
