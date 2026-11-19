from pathlib import Path
from datetime import datetime
from hashlib import sha256
import json, math, re, shutil
ROOT=Path(r'E:\港理课程\课程文件\eie568\Proposal_智能社交挂件\2026-11-15_单板调试与进度报告')
EV=ROOT/'evidence'
V2=EV/'hardware_verified_v2'
def read(p): return json.loads(p.read_text(encoding='utf-8-sig'))
main=read(V2/'summary.json'); follow=read(V2/'followup/summary.json'); rom=read(V2/'bootloader_roundtrip/summary.json')
records=[json.loads(line) for line in (V2/'serial_records.jsonl').read_text(encoding='utf-8').splitlines()]
boots=[r['data'] for r in records if r['data'].get('type')=='boot']; assert boots[0]['firmware_version']==2
imu=[r['data'] for r in records if r['stage']=='imu_baseline' and r['data'].get('type')=='imu']
assert len(imu)==110 and all(math.isfinite(x) for r in imu for key in ('accel_g','gyro_dps') for x in r[key])
assert main['led']['commands']==main['led']['matching_readbacks']==6
assert [s['network_count'] for s in main['wifi_scans']]==[7,6,8]
assert main['ble_pc_receiver']['matching_callbacks']==main['ble_pc_receiver']['matching_service_uuid_callbacks']==54
assert main['board_ble_scans'][0]['reports']==135 and main['board_ble_scans'][0]['duration_ms']==5032
for key,count in [('after_board_scan',33),('after_radio_switch',25),('after_software_restart',35)]:
 assert follow[key]['matching_callbacks']==follow[key]['matching_uuid_callbacks']==count and follow[key]['over_air_verified']
assert len(follow['serial_reopen_checks'])==2 and all(s['pass'] for s in follow['serial_reopen_checks'])
assert follow['software_restart']['pass'] and follow['software_restart']['observed_boot_version']==2
assert rom['software_bootloader_entry_verified'] and rom['v2_flash_preserved_and_verified'] and rom['return_to_application']['pass']
assert rom['final_ble']['matching_callbacks']==rom['final_ble']['matching_uuid_callbacks']==34
hist=ROOT/'history/before_v2_hardware_update'; hist.mkdir(exist_ok=True)
for rel in ['Grp_9_EIE568Progress.docx','Grp_9_EIE568Progress.pdf','Grp_9_EIE568Progress_英文正文.txt','调试结果与待办.txt','tools/fill_template_report.py','evidence/verified_summary.json','evidence/final_board_state.json']:
 p=ROOT/rel; dst=hist/rel; dst.parent.mkdir(parents=True,exist_ok=True)
 if not dst.exists(): shutil.copy2(p,dst)
verified=read(EV/'verified_summary.json')
verified['local_board']['current_serial_usable']=True
verified['local_board']['current_firmware']='BumpSingleBoardDiag v2 (independent diagnostic)'
verified['local_board']['diagnostic_version_observed']=2
verified['hardware_tests_previous_v1']='hardware_verified/summary.json'
verified['hardware_tests']=main
verified['hardware_followup']=follow
verified['hardware_bootloader_roundtrip']=rom
(EV/'verified_summary.json').write_text(json.dumps(verified,indent=2),encoding='utf-8')
state={'diagnostic_v1_uploaded_and_flash_verified':True,'complete_original_rp2040_flash_backup_verified':True,'backup_bytes':16777216,'diagnostic_v2_compiled':True,'diagnostic_v2_uploaded':True,'diagnostic_v2_flash_verified':True,'observed_boot_version':2,'current_serial_usable':True,'current_port':'COM12','last_known_problem':'v1 NVIC software reset prevented USB communication; retained as history','recovery_verified_this_run':{'serial_reopen_checks':2,'watchdog_restart_checks':1,'rom_roundtrip_checks':1},'basic_ble_hardware_checks_completed':True,'final_ble_independently_received':True,'two_badge_encounters_verified':False,'external_modules_tested':False,'board_to_server_verified':False,'current_application':'BumpSingleBoardDiag v2; BLE advertising; IMU stream paused','next_work':'Confirm external module models/wiring and current team BLE source; integrate application firmware, two badges and Wi-Fi/MQTT/server','evidence':'hardware_verified_v2/'}
(EV/'final_board_state.json').write_text(json.dumps(state,indent=2),encoding='utf-8')
source=ROOT/'tools/fill_template_report.py'; text=source.read_text(encoding='utf-8-sig')
replacements={
'Single-board hardware. ':('Single-board hardware. ', 'We uploaded and flash-verified revision 2 of an independent diagnostic sketch after backing up the RP2040\u2019s 16 MiB external flash. It recorded 110 baseline IMU samples over approximately 21.8 s, at roughly 200 ms recording intervals. All six-axis values were finite; mean acceleration magnitude was 1.014 g. Six LED GPIO write/read-back checks matched, and three Wi-Fi scans found 7, 6 and 8 networks.'),
'Verification limits and issue. ':('BLE and recovery checks. ', 'A PC independently recorded 54 receive callbacks matching the board\u2019s manufacturer data and A5B0 service UUID in 20 s, confirming real BLE advertising. The board returned 135 discovery reports during a 5.032 s scan. Further PC checks verified advertising after scanning, BLE/Wi-Fi mode switching and one software watchdog restart. Two serial reopen checks and one software bootloader-entry/return check also succeeded. These are short functional checks, not long-term reliability results.'),
'Remaining work. ':('Remaining work. ', 'Verify LED illumination and motion-triggered discovery, confirm and integrate the external module, and align the Bump BLE frame, identifier and event format with the server interface. Wi-Fi association/MQTT upload, real two-badge encounters, duplicate suppression and complete storage/web integration remain untested on hardware.')
}
for key,pair in replacements.items():
 pattern=r'^\('+re.escape(repr(key))+r', .*\),$'
 text,n=re.subn(pattern,lambda m:repr(pair)+',',text,flags=re.M)
 assert n==1,(key,n)
source.write_text(text,encoding='utf-8')
qa=EV/'template_qa'
contract=qa/'artifact.md'; old=contract.read_text(encoding='utf-8-sig')
old=old.replace('Actual hardware upload succeeded; older pre-upload reports are stale. Distinguish injected BLE inputs from actual sensor/radio measurements; BLE and full hardware-to-server remain unverified.','Diagnostic v2 upload and flash verification succeeded. Evidence hardware_verified_v2 verifies basic BLE advertising received by the PC, board scanning, radio switching, serial reopen, one watchdog restart and one ROM roundtrip. Distinguish these independent diagnostic checks from the supplied mock and from the still-unverified Bump two-badge application and device-to-server workflow.')
contract.write_text(old,encoding='utf-8')
summary={'firmware_version_observed':2,'baseline_imu_samples':len(imu),'wifi_scan_network_counts':[7,6,8],'pc_primary_callbacks':54,'board_primary_scan_reports':135,'followup_matching_callbacks':[33,25,35],'serial_reopens_passed':2,'watchdog_restarts_passed':1,'rom_roundtrips_passed':1,'final_pc_callbacks':34,'all_checked':True,'final_application':'independent diagnostic v2','elapsed_time_note':'reboot observation interval includes subsequent IMU collection; not reboot latency'}
(V2/'verified_results.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(json.dumps(summary,indent=2))
