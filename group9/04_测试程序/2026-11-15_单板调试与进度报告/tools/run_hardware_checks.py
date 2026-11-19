import asyncio, json, math, statistics, time
from pathlib import Path
from datetime import datetime, timezone
import serial
from serial.tools import list_ports
from bleak import BleakScanner

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence/hardware_verified_v2'
OUT.mkdir(exist_ok=True)
records=[]
serial_pending=b''
summary={'started_utc':datetime.now(timezone.utc).isoformat(),'firmware':'BumpSingleBoardDiag','independent_diagnostic':True,'expected_firmware_version':2,'external_modules_tested':False,'gesture_tested':False,'wifi_connected':False,'mqtt_tested':False,'two_badges_tested':False}

def save():
 (OUT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
 (OUT/'serial_records.jsonl').write_text('\n'.join(json.dumps(r) for r in records)+'\n',encoding='utf-8')

def port_name():
 ports=[p for p in list_ports.comports() if p.vid==0x2341 and p.pid in (0x005e,0x805e,0x015e,0x025e)]
 if len(ports)!=1: raise RuntimeError('Expected exactly one Nano RP2040 serial device; found '+str(len(ports)))
 p=ports[0]
 if p.serial_number and p.serial_number.upper()!='416523501C74AAC8': raise RuntimeError('Board serial number differs from the verified target')
 return p.device

def connect():
 deadline=time.monotonic()+20
 while True:
  try:
   s=serial.Serial(port_name(),115200,timeout=.15)
   return s
  except (RuntimeError,serial.SerialException):
   if time.monotonic()>deadline: raise
   time.sleep(.3)

def capture(s,stage,seconds,until=None):
 global serial_pending
 found=[]; start=time.monotonic(); completed=False
 while time.monotonic()-start<seconds and not completed:
  serial_pending += s.read(s.in_waiting or 1)
  while b'\n' in serial_pending:
   raw,serial_pending=serial_pending.split(b'\n',1)
   raw=raw.decode('utf-8',errors='replace').strip()
   if not raw: continue
   try: obj=json.loads(raw)
   except json.JSONDecodeError: obj={'type':'unparsed','raw':raw}
   records.append({'stage':stage,'host_utc':datetime.now(timezone.utc).isoformat(),'data':obj}); found.append(obj)
   if until and obj.get('type')==until: completed=True
 save(); return found

async def scan_air(expected):
 hits=[]
 def detected(dev,adv):
  payload=adv.manufacturer_data.get(0xffff)
  if payload is None or ('ffff'+payload.hex()).lower()!=expected.lower(): return
  hits.append({'host_utc':datetime.now(timezone.utc).isoformat(),'rssi_dbm':adv.rssi,'manufacturer_hex':'ffff'+payload.hex(),'service_uuids':adv.service_uuids,'local_name':adv.local_name})
 async with BleakScanner(detection_callback=detected): await asyncio.sleep(20)
 return hits

s=None
try:
 s=connect(); summary['port']=s.port
 data=capture(s,'imu_baseline',22)
 imu=[d for d in data if d.get('type')=='imu']
 if not imu: raise RuntimeError('No diagnostic IMU data observed; will not send further commands')
 s.write(b'q'); capture(s,'quiet',.6)
 norms=[math.sqrt(sum(v*v for v in d['accel_g'])) for d in imu]
 summary['imu']={'samples':len(imu),'all_finite':all(math.isfinite(v) for d in imu for key in ('accel_g','gyro_dps') for v in d[key]),'all_real_sensor':all(d.get('source')=='real_sensor' for d in imu),'first_uptime_ms':imu[0]['uptime_ms'],'last_uptime_ms':imu[-1]['uptime_ms'],'accel_norm_mean_g':statistics.mean(norms),'accel_norm_min_g':min(norms),'accel_norm_max_g':max(norms),'max_abs_gyro_dps':max(abs(v) for d in imu for v in d['gyro_dps']),'controlled_motion_applied':False}
 s.write(b'l'); led=capture(s,'led_gpio',2.5); led=[d for d in led if d.get('type')=='led_gpio']
 summary['led']={'commands':len(led),'matching_readbacks':sum(d['commanded']==d['readback'] for d in led),'optical_confirmation':False}
 summary['wifi_scans']=[]
 for i in range(3):
  s.write(b'w'); data=capture(s,'wifi_scan_'+str(i+1),25,until='wifi_scan')
  scans=[d for d in data if d.get('type')=='wifi_scan']; summary['wifi_scans']+=scans
  firmware=[d for d in data if d.get('type')=='wifi_firmware']
  if firmware: summary['wifi_firmware_version']=firmware[-1]['version']
  save()
 s.write(b'b'); data=capture(s,'ble_start',15,until='ble_advertise')
 adverts=[d for d in data if d.get('type')=='ble_advertise']
 if not adverts or adverts[-1].get('started')!=1: raise RuntimeError('BLE advertising not started; inspect serial log')
 summary['ble_advertise']=adverts[-1]
 expected=adverts[-1]['manufacturer_hex']
 hits=asyncio.run(scan_air(expected)); (OUT/'pc_ble_rx.json').write_text(json.dumps(hits,indent=2),encoding='utf-8')
 summary['ble_pc_receiver']={'duration_s':20,'matching_callbacks':len(hits),'matching_manufacturer_hex':expected,'matching_service_uuid_callbacks':sum('0000a5b0-0000-1000-8000-00805f9b34fb' in h['service_uuids'] for h in hits),'rssi_min_dbm':min((h['rssi_dbm'] for h in hits),default=None),'rssi_max_dbm':max((h['rssi_dbm'] for h in hits),default=None),'over_air_verified':len(hits)>0}
 save()
 s.write(b's'); data=capture(s,'board_ble_rx',10,until='ble_scan')
 summary['board_ble_scans']=[d for d in data if d.get('type')=='ble_scan']
 s.write(b'i'); final=capture(s,'imu_with_ble',5); s.write(b'q'); capture(s,'final_quiet',.5)
 summary['imu_during_ble_samples']=sum(d.get('type')=='imu' for d in final)
 summary['board_left_in']='diagnostic BLE advertising, IMU output paused'
 summary['finished_utc']=datetime.now(timezone.utc).isoformat(); save()
 print(json.dumps(summary))
except Exception as e:
 summary['error']=str(e); summary['finished_utc']=datetime.now(timezone.utc).isoformat(); save(); print(json.dumps(summary)); raise
finally:
 if s is not None and s.is_open: s.close()
