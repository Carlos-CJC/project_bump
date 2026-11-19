import asyncio, json, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
import serial
from serial.tools import list_ports
from bleak import BleakScanner
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence/hardware_verified_v2/bootloader_roundtrip'
OUT.mkdir(exist_ok=True)
PICO=Path(r'E:\港理课程\网站维护\任务工作记录\2026-10-23_eie568_lab1\toolchain\data\packages\arduino\tools\rp2040tools\1.0.6\picotool.exe')
ELF=ROOT/'firmware/diag-bin-v2/BumpSingleBoardDiag.ino.elf'
summary={'started_utc':datetime.now(timezone.utc).isoformat(),'flash_rewritten_during_this_check':False}
records=[]; pending=b''; s=None

def save():
 (OUT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
 (OUT/'serial_records.jsonl').write_text('\n'.join(json.dumps(r) for r in records)+'\n',encoding='utf-8')

def connect():
 deadline=time.monotonic()+20
 while time.monotonic()<deadline:
  ports=[p for p in list_ports.comports() if p.vid==0x2341 and p.pid in (0x005e,0x805e,0x015e,0x025e) and (p.serial_number or '').upper()=='416523501C74AAC8']
  if len(ports)==1:
   try: return serial.Serial(ports[0].device,115200,timeout=.15)
   except serial.SerialException: pass
  time.sleep(.3)
 raise RuntimeError('Verified serial device did not reconnect')

def capture(stage,seconds,kind=None):
 global pending
 found=[]; until=time.monotonic()+seconds
 while time.monotonic()<until:
  pending+=s.read(s.in_waiting or 1)
  while b'\n' in pending:
   raw,pending=pending.split(b'\n',1)
   try: d=json.loads(raw.decode('utf-8'))
   except (ValueError,UnicodeError): continue
   records.append({'stage':stage,'host_utc':datetime.now(timezone.utc).isoformat(),'data':d}); found.append(d)
   if kind and d.get('type')==kind: save(); return found
 save(); return found

def pico(args,name):
 p=subprocess.run([str(PICO),*args],capture_output=True,text=True,timeout=20)
 (OUT/(name+'.log')).write_text(p.stdout+p.stderr,encoding='utf-8')
 return p

async def receive(expected):
 hits=[]
 def callback(dev,adv):
  payload=adv.manufacturer_data.get(0xffff)
  if payload is not None and 'ffff'+payload.hex()==expected.lower():
   hits.append({'manufacturer_hex':'ffff'+payload.hex(),'service_uuids':adv.service_uuids,'rssi_dbm':adv.rssi,'host_utc':datetime.now(timezone.utc).isoformat()})
 async with BleakScanner(detection_callback=callback): await asyncio.sleep(10)
 return hits

try:
 s=connect(); s.write(b't'); s.flush()
 try: capture('bootloader_request',2)
 except serial.SerialException as e: summary['expected_disconnect']=str(e)
 s.close(); s=None; pending=b''
 deadline=time.monotonic()+10
 while True:
  p=pico(['info','-a'],'rom_info')
  if p.returncode==0 and '16384K' in p.stdout and 'ROM version:  3' in p.stdout: break
  if time.monotonic()>deadline: raise RuntimeError('Expected RP2040 ROM device not found')
  time.sleep(.5)
 summary['software_bootloader_entry_verified']=True; save(); print('Software BOOTSEL entry verified',flush=True)
 p=pico(['verify',str(ELF)],'flash_verify')
 if p.returncode: raise RuntimeError('v2 flash does not match its ELF')
 summary['v2_flash_preserved_and_verified']=True; save()
 p=pico(['reboot','-a'],'return_to_application')
 if p.returncode: raise RuntimeError('ROM reboot-to-application failed')
 time.sleep(.5); s=connect(); data=capture('return_boot',8,'boot'); data+=capture('return_imu',2)
 boots=[d for d in data if d.get('type')=='boot']; imu=[d for d in data if d.get('type')=='imu']
 summary['return_to_application']={'port':s.port,'observed_boot_version':boots[-1].get('firmware_version') if boots else None,'imu_samples':len(imu),'first_uptime_ms':imu[0]['uptime_ms'] if imu else None,'pass':bool(boots and boots[-1].get('firmware_version')==2 and imu)}
 if not summary['return_to_application']['pass']: raise RuntimeError('v2 application boot not verified')
 s.write(b'q'); capture('quiet',.4); s.write(b'b'); data=capture('ble_start',15,'ble_advertise'); adverts=[d for d in data if d.get('type')=='ble_advertise']
 if not adverts or adverts[-1].get('started')!=1: raise RuntimeError('BLE did not start after return')
 hits=asyncio.run(receive(adverts[-1]['manufacturer_hex']))
 (OUT/'pc_ble_rx.json').write_text(json.dumps(hits,indent=2),encoding='utf-8')
 summary['final_ble']={'duration_s':10,'matching_callbacks':len(hits),'matching_uuid_callbacks':sum('0000a5b0-0000-1000-8000-00805f9b34fb' in h['service_uuids'] for h in hits),'over_air_verified':any('0000a5b0-0000-1000-8000-00805f9b34fb' in h['service_uuids'] for h in hits)}
 summary['board_left_in']='v2 diagnostic; serial responsive, BLE advertising, IMU output paused'
 summary['finished_utc']=datetime.now(timezone.utc).isoformat(); save(); print(json.dumps(summary),flush=True)
except Exception as e:
 summary['error']=str(e); summary['finished_utc']=datetime.now(timezone.utc).isoformat(); save(); print(json.dumps(summary),flush=True); raise
finally:
 if s is not None and s.is_open: s.close()
