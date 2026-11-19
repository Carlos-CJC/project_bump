"""Real board observation + explicitly injected motion + direct LAN MQTT evidence."""
import argparse, asyncio, json, math, sqlite3, time, uuid
from pathlib import Path
from datetime import datetime, timezone
import serial
from serial.tools import list_ports
from bleak import BleakScanner
from current_wifi_config import current_wifi

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--hotspot',action='store_true')
parser.add_argument('--instrumented',action='store_true',help='Stream discovery status and omit concurrent PC BLE monitoring')
args=parser.parse_args()
OUT=ROOT/('evidence/motion_mqtt/direct_instrumented' if args.instrumented else 'evidence/motion_mqtt/direct_hotspot' if args.hotspot else 'evidence/motion_mqtt/direct')
OUT.mkdir(exist_ok=False)
RUNTIME=ROOT/('private/motion_hotspot_runtime' if args.hotspot else 'private/motion_mqtt_runtime')
records=[]; pending=b''; s=None
summary={'started_utc':datetime.now(timezone.utc).isoformat(),'program':'BumpSingleBoardMotionMqtt','physical_motion_controlled':False,'external_modules_tested':False,'two_badges_tested':False,'mqtt_transport':'private_lan_tcp_without_tls','boot_id':str(uuid.uuid4())}

def save():
 (OUT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
 (OUT/'serial_records.jsonl').write_text('\n'.join(json.dumps(r) for r in records)+'\n',encoding='utf-8')
 (OUT/'serial_pending.txt').write_text(pending.decode('utf-8',errors='replace'),encoding='utf-8')

def stage(name):
 summary['stage']=name; save(); print(name,flush=True)

def connect():
 deadline=time.monotonic()+20
 while time.monotonic()<deadline:
  ports=[p for p in list_ports.comports() if p.vid==0x2341 and p.pid in (0x005e,0x805e,0x015e,0x025e) and (p.serial_number or '').upper()=='416523501C74AAC8']
  if len(ports)==1:
   try: return serial.Serial(ports[0].device,115200,timeout=.15,write_timeout=2)
   except serial.SerialException: pass
  time.sleep(.3)
 raise RuntimeError('Verified board serial port unavailable')

def capture(label,seconds,until=None):
 global pending
 found=[]; end=time.monotonic()+seconds
 while time.monotonic()<end:
  pending+=s.read(s.in_waiting or 1)
  while b'\n' in pending:
   raw,pending=pending.split(b'\n',1)
   if not raw.strip(): continue
   try: data=json.loads(raw.decode('utf-8'))
   except (ValueError,UnicodeError): data={'type':'unparsed','raw':raw.decode('utf-8',errors='replace')}
   records.append({'stage':label,'host_utc':datetime.now(timezone.utc).isoformat(),'data':data}); found.append(data)
   if until and data.get('type')==until: save(); return found
 save(); return found

def cmd(command,label,seconds=2,until=None):
 s.write((command+'\n').encode('utf-8')); return capture(label,seconds,until)

def status(label):
 data=cmd('status',label,2,'status'); found=[d for d in data if d.get('type')=='status']
 if not found: raise RuntimeError(label+': no status received')
 if found[-1].get('firmware')!='BumpSingleBoardMotionMqtt': raise RuntimeError('Unexpected firmware')
 return found[-1]

def setting(key,value):
 if '\n' in value or '\r' in value or '\0' in value: raise RuntimeError('Configuration value contains unsupported control characters')
 result=cmd('SET '+key+' '+value,'configuration_'+key,2,'configuration_set')
 if not any(d.get('type')=='configuration_set' and d.get('key')==key and d.get('ok') for d in result):
  raise RuntimeError('Configuration not accepted: '+key)

async def receive_ble(expected):
 hits=[]
 def callback(device,adv):
  payload=adv.manufacturer_data.get(0xffff)
  if payload is not None and 'ffff'+payload.hex()==expected.lower():
   hits.append({'host_utc':datetime.now(timezone.utc).isoformat(),'manufacturer_hex':'ffff'+payload.hex(),'service_uuids':adv.service_uuids,'local_name':adv.local_name,'rssi_dbm':adv.rssi})
 async with BleakScanner(detection_callback=callback): await asyncio.sleep(10)
 return hits

def db_rows():
 with sqlite3.connect(RUNTIME/'telemetry.sqlite3') as db:
  rows=db.execute('SELECT canonical_json FROM telemetry WHERE boot_id=? ORDER BY seq',(summary['boot_id'],)).fetchall()
 return [json.loads(r[0]) for r in rows]

try:
 s=connect(); summary['port']=s.port; capture('boot',1)
 summary['initial_status']=status('initial_status')
 setting('boot_id',summary['boot_id']); setting('transport','wifi_mqtt')
 credentials=json.loads((RUNTIME/'config.json').read_text(encoding='utf-8'))['mqtt']['accounts']['badge-a']
 listener=json.loads((RUNTIME/'motion_server.json').read_text(encoding='utf-8'))
 setting('broker_host',listener['bind_ip']); setting('broker_port',str(listener['port'])); setting('mqtt_user','badge-a'); setting('mqtt_password',credentials['password'])
 del credentials
 try:
  if args.hotspot:
   connection=json.loads((ROOT/'private/motion_hotspot/session.json').read_text(encoding='utf-8-sig'))
   if not connection.get('ready'): raise RuntimeError('Temporary hotspot is not ready')
   connection['authentication']='WPA2PSK'
   connection['wifi_password']=connection.pop('password')
  else: connection=current_wifi(True)
  if connection['authentication'] not in ('WPA2PSK','WPAPSK'): raise RuntimeError('Current network authentication is unsupported by this board test')
  setting('ssid',connection['ssid']); setting('wifi_password',connection['wifi_password'])
  summary['wifi_configuration']={'available':True,'authentication':connection['authentication'],'source':'temporary_2_4ghz_hotspot' if args.hotspot else 'current_connected_windows_profile','saved_to_board_flash':False,'values_logged':False}
  del connection
 except (OSError,RuntimeError) as e:
  summary['wifi_configuration']={'available':False,'reason':str(e),'values_logged':False}
 if args.hotspot:
  scan=cmd('wifi_scan','wifi_visibility',30,'wifi_scan')
  summary['wifi_visibility']=[d for d in scan if d.get('type')=='wifi_scan']
 observation_seconds=20 if args.hotspot else 60
 stage('Observing live IMU for '+str(observation_seconds)+' seconds')
 before=status('baseline_start'); cmd('i','telemetry_on',1,'telemetry'); baseline=capture('real_imu_observation',observation_seconds); cmd('q','telemetry_off',1,'telemetry'); after=status('baseline_end')
 dc=after['samples']-before['samples']; dt=after['last_sample_uptime_ms']-before['last_sample_uptime_ms']
 values=[d['accel_g'] for d in baseline if d.get('type')=='status' and isinstance(d.get('accel_g'),list)]
 summary['real_imu_observation']={'samples':dc,'sample_time_span_ms':dt,'observed_samples_per_s':dc/(dt/1000) if dt else None,'status_records':len(values),'observed_values_finite':all(math.isfinite(v) for a in values for v in a),'max_gap_ms_at_end':after['max_gap_ms'],'hardware_triggers':after['trigger_count']-before['trigger_count'],'accepted_hardware_triggers':after['accepted_hardware_triggers']-before['accepted_hardware_triggers'],'physical_motion_controlled':False}
 stage('Attempting a real sensor snapshot upload over Wi-Fi')
 data=cmd('sample','snapshot',2,'event_queued'); summary['snapshot_events']=[d['payload'] for d in data if d.get('type')=='event_queued']
 first_sync=cmd('sync','first_direct_sync',65,'sync_complete')
 summary['first_sync']={key:[d for d in first_sync if d.get('type')==key] for key in ['wifi_connect','mqtt_connect','mqtt_subscribe','mqtt_publish','application_ack','sync_complete','error']}
 summary['stored_after_first_sync']=db_rows(); save()
 stage('Checking injected motion through the real discovery path')
 if args.instrumented: cmd('i','discovery_telemetry_on',1,'telemetry')
 injected=cmd('inject_shake','injected_motion',8,'injection_complete'); summary['injected_events']=[d['payload'] for d in injected if d.get('type')=='event_queued']
 window=status('discovery_start'); summary['discovery_status']=window
 adverts=[d for d in injected if d.get('type')=='ble_advertise' and d.get('started')==1]
 if adverts and not args.instrumented:
  hits=asyncio.run(receive_ble(adverts[-1]['manufacturer_hex']))
  (OUT/'motion_ble_pc_rx.json').write_text(json.dumps(hits,indent=2),encoding='utf-8')
  summary['motion_ble_reception']={'duration_s':10,'matching_callbacks':len(hits),'matching_uuid_callbacks':sum('0000a5b0-0000-1000-8000-00805f9b34fb' in d['service_uuids'] for d in hits),'trigger_source':'injected_test'}
 elif args.instrumented: summary['motion_ble_reception']={'not_performed':True,'reason':'Concurrent PC BLE monitoring omitted for isolation'}
 else: summary['motion_ble_reception']={'error':'No started advertisement found'}
 repeated=cmd('inject_shake','repeated_injection',8,'injection_complete'); repeated_status=status('after_repeat')
 summary['repeated_motion']={'ignored_messages':[d for d in repeated if d.get('type')=='motion_ignored'],'window_not_extended':window['window_deadline_ms']==repeated_status['window_deadline_ms'],'first_deadline_ms':window['window_deadline_ms'],'after_repeat_deadline_ms':repeated_status['window_deadline_ms']}
 stage('Waiting for the discovery window and automatic MQTT synchronization')
 remaining=repeated_status.get('window_remaining_ms',0)/1000
 automatic=capture('automatic_sync',remaining+65,until='sync_complete')
 summary['automatic_sync']={key:[d for d in automatic if d.get('type')==key] for key in ['discovery_complete','sync_started','wifi_connect','mqtt_connect','mqtt_publish','application_ack','sync_complete','error']}
 if args.instrumented: cmd('q','discovery_telemetry_off',1,'telemetry')
 summary['final_status']=status('final_status')
 dumped=cmd('dump','final_queue_dump',2,'queue_dump_complete'); summary['undelivered_queue']=[d['payload'] for d in dumped if d.get('type')=='queued_event']
 summary['stored_rows']=db_rows()
 expected=summary['snapshot_events']+summary['injected_events']
 summary['expected_events_stored_exactly']=bool(expected) and all(e in summary['stored_rows'] for e in expected)
 summary['finished_utc']=datetime.now(timezone.utc).isoformat(); save()
 print(json.dumps(summary),flush=True)
except Exception as e:
 summary['error']=str(e); summary['finished_utc']=datetime.now(timezone.utc).isoformat(); save(); print(json.dumps(summary),flush=True); raise
finally:
 if s is not None and s.is_open: s.close()
