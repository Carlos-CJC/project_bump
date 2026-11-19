import asyncio, json, math, time
from pathlib import Path
from datetime import datetime, timezone
import serial
from serial.tools import list_ports
from bleak import BleakScanner

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence/hardware_verified_v2/followup'
OUT.mkdir(exist_ok=True)
records=[]
pending=b''
summary={'started_utc':datetime.now(timezone.utc).isoformat(),'independent_diagnostic':True,'external_modules_tested':False,'two_badges_tested':False,'wifi_connected':False,'mqtt_tested':False}

def save():
 (OUT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
 (OUT/'serial_records.jsonl').write_text('\n'.join(json.dumps(r) for r in records)+'\n',encoding='utf-8')

def progress(stage):
 summary['last_stage']=stage; save(); print(stage,flush=True)

def connect():
 deadline=time.monotonic()+20
 while True:
  try:
   ports=[p for p in list_ports.comports() if p.vid==0x2341 and p.pid in (0x005e,0x805e,0x015e,0x025e) and (p.serial_number or '').upper()=='416523501C74AAC8']
   if len(ports)!=1: raise RuntimeError('Verified target serial port is unavailable')
   return serial.Serial(ports[0].device,115200,timeout=.15)
  except (RuntimeError,serial.SerialException):
   if time.monotonic()>deadline: raise
   time.sleep(.3)

def capture(s,stage,seconds,until=None):
 global pending
 found=[]; end=time.monotonic()+seconds
 while time.monotonic()<end:
  pending += s.read(s.in_waiting or 1)
  while b'\n' in pending:
   raw,pending=pending.split(b'\n',1)
   raw=raw.decode('utf-8',errors='replace').strip()
   if not raw: continue
   try: data=json.loads(raw)
   except json.JSONDecodeError: data={'type':'unparsed','raw':raw}
   records.append({'stage':stage,'host_utc':datetime.now(timezone.utc).isoformat(),'data':data}); found.append(data)
   if until and data.get('type')==until: save(); return found
 save(); return found

def command(s,key,stage,seconds,kind):
 s.write(key.encode('ascii')); data=capture(s,stage,seconds,kind)
 hits=[d for d in data if d.get('type')==kind]
 if not hits: raise RuntimeError(stage+': expected '+kind+' output missing')
 return hits[-1]

async def scan(expected):
 hits=[]
 def detected(dev,adv):
  payload=adv.manufacturer_data.get(0xffff)
  if payload is None or 'ffff'+payload.hex()!=expected.lower(): return
  hits.append({'host_utc':datetime.now(timezone.utc).isoformat(),'rssi_dbm':adv.rssi,'manufacturer_hex':'ffff'+payload.hex(),'service_uuids':adv.service_uuids,'local_name':adv.local_name})
 async with BleakScanner(detection_callback=detected): await asyncio.sleep(10)
 return hits

def check_air(stage,expected):
 try:
  hits=asyncio.run(scan(expected))
  (OUT/(stage+'_pc_rx.json')).write_text(json.dumps(hits,indent=2),encoding='utf-8')
  matched=sum('0000a5b0-0000-1000-8000-00805f9b34fb' in d['service_uuids'] for d in hits)
  result={'duration_s':10,'matching_callbacks':len(hits),'matching_uuid_callbacks':matched,'over_air_verified':matched>0,'rssi_min_dbm':min((d['rssi_dbm'] for d in hits),default=None),'rssi_max_dbm':max((d['rssi_dbm'] for d in hits),default=None)}
 except Exception as e: result={'error':str(e),'over_air_verified':False}
 summary[stage]=result; save(); return result

s=None
try:
 s=connect(); summary['port']=s.port
 progress('Checking scan-to-advertising recovery')
 s.write(b'i'); first=capture(s,'initial_imu',2); s.write(b'q'); capture(s,'initial_quiet',.5)
 baseline=[d for d in first if d.get('type')=='imu']
 if not baseline: raise RuntimeError('No live IMU response before follow-up tests')
 adv=command(s,'b','initial_ble',15,'ble_advertise'); summary['initial_advertisement']=adv
 if adv.get('started')!=1: raise RuntimeError('Initial BLE advertising failed')
 expected=adv['manufacturer_hex']
 result=command(s,'s','scan_resume',10,'ble_scan'); summary['board_ble_scan']=result
 check_air('after_board_scan',expected)
 progress('Checking BLE to Wi-Fi to BLE switching')
 summary['switch_wifi_scan']=command(s,'w','switch_wifi',25,'wifi_scan')
 summary['switch_ble_advertisement']=command(s,'b','switch_ble',15,'ble_advertise')
 check_air('after_radio_switch',expected)
 s.write(b'i'); data=capture(s,'imu_after_switch',3); s.write(b'q'); capture(s,'quiet_after_switch',.5)
 after=[d for d in data if d.get('type')=='imu']
 summary['imu_after_switch']={'samples':len(after),'first_uptime_ms':after[0]['uptime_ms'] if after else None,'last_uptime_ms':after[-1]['uptime_ms'] if after else None,'no_mcu_restart_across_switch':bool(after and after[0]['uptime_ms']>baseline[-1]['uptime_ms'])}
 progress('Checking serial close-and-reopen')
 summary['serial_reopen_checks']=[]
 for i in range(2):
  s.close(); time.sleep(.3); s=connect(); pending=b''; s.write(b'i'); data=capture(s,'serial_reopen_'+str(i+1),2); s.write(b'q'); capture(s,'quiet_reopen_'+str(i+1),.4)
  imu=[d for d in data if d.get('type')=='imu']
  summary['serial_reopen_checks'].append({'pass':bool(imu),'samples':len(imu),'port':s.port}); save()
 progress('Checking software restart and USB reconnection')
 summary['before_restart_checks_saved']=True; save()
 reset_start=time.monotonic(); s.write(b'r'); s.flush(); time.sleep(.3)
 try: capture(s,'restart_command',1)
 except serial.SerialException as e: summary['expected_restart_disconnect']=str(e)
 s.close(); s=None; pending=b''; time.sleep(1)
 s=connect(); data=capture(s,'after_restart_boot',8,until='boot'); data += capture(s,'after_restart_imu',2)
 boots=[d for d in data if d.get('type')=='boot']; imu=[d for d in data if d.get('type')=='imu']
 summary['software_restart']={'serial_reconnected':True,'observed_boot_version':boots[-1].get('firmware_version') if boots else None,'imu_samples':len(imu),'first_uptime_ms':imu[0].get('uptime_ms') if imu else None,'observation_elapsed_s':round(time.monotonic()-reset_start,3),'pass':bool(boots and boots[-1].get('firmware_version')==2 and imu)}
 s.write(b'q'); capture(s,'post_reset_quiet',.5)
 summary['post_reset_advertisement']=command(s,'b','post_reset_ble',15,'ble_advertise')
 final_air=check_air('after_software_restart',expected)
 summary['board_left_in']='diagnostic BLE advertising with IMU output paused' if final_air.get('over_air_verified') else 'serial responsive; radio state requires inspection'
 summary['finished_utc']=datetime.now(timezone.utc).isoformat(); save(); print(json.dumps(summary),flush=True)
except Exception as e:
 summary['error']=str(e); summary['finished_utc']=datetime.now(timezone.utc).isoformat(); save(); print(json.dumps(summary),flush=True); raise
finally:
 if s is not None and s.is_open: s.close()

