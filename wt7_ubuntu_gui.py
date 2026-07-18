
#!/usr/bin/env python3
"""WT7 PyQt5 antenna controller GUI alpha."""
from __future__ import annotations
import argparse, csv, queue, threading, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from PyQt5.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget
from wt7_antenna import Direction, Position, SafeAntenna, shortest_angle_delta
from wt7_astro import TargetPosition, local_sidereal_time, moon_equatorial, moon_position, source_position
from wt7_b210_power import B210PowerMeter, B210PowerMeterConfig, B210PowerReading
from wt7_config import PowerConfig, calibrated_dbm_from_dbfs, load_configs, load_power_config, load_site_config, load_sources, save_power_config
from wt7_logging import EventLogger
from wt7_solar import sun_equatorial, sun_position
from wt7_state import AppStateStore, SystemRunState
APP_VERSION = "v0.1-alpha"

def hms(seconds: float) -> str:
    seconds %= 86400.0; h=int(seconds//3600); m=int((seconds%3600)//60); s=int(seconds%60); return f"{h:02d}:{m:02d}:{s:02d}"
def ha_text(hours: float) -> str:
    while hours <= -12: hours += 24
    while hours > 12: hours -= 24
    sign='+' if hours >= 0 else '-'; mins=int(round(abs(hours)*60)); return f"{sign}{mins//60:02d}:{mins%60:02d}"
def lbl(text='', name=''):
    w=QLabel(text); w.setObjectName(name); return w
def bold(text='', size=10):
    w=lbl(text,'bold'); f=QFont(); f.setPointSize(size); f.setBold(True); w.setFont(f); return w
def btn(text, name=''):
    w=QPushButton(text); w.setMinimumSize(QSize(46,28)); w.setObjectName(name); return w
def edit(text, width=70):
    w=QLineEdit(str(text)); w.setFixedWidth(width); return w
class Panel(QFrame):
    def __init__(self): super().__init__(); self.setObjectName('panel'); self.setFrameShape(QFrame.StyledPanel)
class AntennaCard(Panel):
    jog_pressed=pyqtSignal(str,str); jog_released=pyqtSignal(str); stop_clicked=pyqtSignal(str)
    def __init__(self,name):
        super().__init__(); self.name=name; self.setMinimumHeight(144); outer=QVBoxLayout(self); outer.setContentsMargins(9,8,9,8)
        head=QHBoxLayout(); head.addWidget(bold(name.upper())); head.addStretch(1); self.state=lbl('DISCONNECTED','stateStopped'); head.addWidget(self.state); outer.addLayout(head)
        g=QGridLayout(); g.setHorizontalSpacing(12); g.setVerticalSpacing(4)
        self.az=bold('--',20); self.el=bold('--',20); self.az_err=lbl('--'); self.el_err=lbl('--'); self.limits=lbl('SAFE','safe'); self.mode=lbl('--'); self.target=lbl('--')
        for r,axis,val,errlab,err in [(0,'AZ',self.az,'AZ err',self.az_err),(1,'EL',self.el,'EL err',self.el_err)]:
            g.addWidget(lbl(axis,'muted'),r,0); g.addWidget(val,r,1); g.addWidget(lbl(errlab,'muted'),r,2); g.addWidget(err,r,3)
        g.addWidget(lbl('Limits','muted'),0,4); g.addWidget(self.limits,0,5); g.addWidget(lbl('Mode','muted'),1,4); g.addWidget(self.mode,1,5); g.addWidget(lbl('Target','muted'),2,2); g.addWidget(self.target,2,3,1,3)
        c=QWidget(); cg=QGridLayout(c); cg.setHorizontalSpacing(6); cg.setVerticalSpacing(6)
        for text,direction,row,col in [('EL+',Direction.EL_UP.value,0,1),('AZ-',Direction.AZ_CCW.value,1,0),('AZ+',Direction.AZ_CW.value,1,2),('EL-',Direction.EL_DOWN.value,2,1)]:
            b=btn(text); b.pressed.connect(lambda d=direction: self.jog_pressed.emit(self.name,d)); b.released.connect(lambda: self.jog_released.emit(self.name)); cg.addWidget(b,row,col)
        stop=btn('STOP'); stop.clicked.connect(lambda: self.stop_clicked.emit(self.name)); cg.addWidget(stop,1,1)
        g.addWidget(c,0,6,3,1,Qt.AlignRight|Qt.AlignVCenter); g.setColumnMinimumWidth(1,88); g.setColumnMinimumWidth(3,64); g.setColumnMinimumWidth(5,76); g.setColumnStretch(6,1); outer.addLayout(g)
    def set_position(self,pos: Optional[Position]):
        self.az.setText('--' if pos is None else f'{pos.azimuth:0.2f}'); self.el.setText('--' if pos is None else f'{pos.elevation:0.2f}')
    def set_target(self,target: Optional[TargetPosition], pos: Optional[Position]):
        if not target: self.target.setText('--'); self.az_err.setText('--'); self.el_err.setText('--'); return
        self.target.setText(f'{target.azimuth:0.2f} / {target.elevation:0.2f}')
        if pos: self.az_err.setText(f'{shortest_angle_delta(pos.azimuth,target.azimuth):+0.2f}'); self.el_err.setText(f'{target.elevation-pos.elevation:+0.2f}')
    def set_state(self,text):
        self.state.setText(text.upper()); low=text.lower(); name='stateFault' if 'fault' in low else ('stateBusy' if any(x in low for x in ['slew','park','scan','yfactor','manual','connecting']) else ('stateGood' if 'tracking' in low else 'stateStopped'))
        self.state.setObjectName(name); self.state.style().unpolish(self.state); self.state.style().polish(self.state); self.mode.setText('Auto' if text.upper() in ['TRACKING','SLEWING','PARKING'] else text.title())
    def set_limits_ok(self,ok):
        self.limits.setText('SAFE' if ok else 'FAULT'); self.limits.setObjectName('safe' if ok else 'faultTag'); self.limits.style().unpolish(self.limits); self.limits.style().polish(self.limits)
class B210Panel(Panel):
    start_clicked=pyqtSignal(); stop_clicked=pyqtSignal(); log_start_clicked=pyqtSignal(); log_stop_clicked=pyqtSignal(); cal_clicked=pyqtSignal()
    def __init__(self,power:PowerConfig):
        super().__init__(); self.power=power; g=QGridLayout(self); g.setContentsMargins(9,8,9,8); g.setHorizontalSpacing(10); g.setVerticalSpacing(5)
        self.status=lbl('SDR RELEASED'); self.a_val=bold('--.- dBFS',14); self.b_val=bold('--.- dBFS',14); self.a_stats=lbl('Avg -- Min -- Max --'); self.b_stats=lbl('Avg -- Min -- Max --')
        self.gain_a=edit(power.gain_db); self.gain_b=edit(power.gain_b_db); self.freq=edit(f'{power.center_frequency_hz/1_000_000:0.1f}'); self.rate=edit(f'{power.sample_rate_hz/1000:0.0f}'); self.bw=edit(f'{power.measurement_bandwidth_hz/1000:0.0f}'); self.clock=edit(power.clock_source or 'internal'); self.avg=edit(power.smoothing_samples); self.gui_hz=edit(f'{power.update_rate_hz:0.0f}')
        g.addWidget(bold('B210'),0,0); g.addWidget(self.status,1,0); g.addWidget(bold('CH A'),0,1); g.addWidget(self.a_val,0,2); g.addWidget(bold('CH B'),0,4); g.addWidget(self.b_val,0,5)
        g.addWidget(lbl('Gain','muted'),1,1); g.addWidget(self.gain_a,1,2); g.addWidget(self.a_stats,2,1,1,2); g.addWidget(lbl('Gain','muted'),1,4); g.addWidget(self.gain_b,1,5); g.addWidget(self.b_stats,2,4,1,2)
        for i,(k,w) in enumerate([('Freq MHz',self.freq),('Rate ksps',self.rate),('BW kHz',self.bw),('Clock',self.clock)]): g.addWidget(lbl(k),4,i*2+1); g.addWidget(w,4,i*2+2)
        g.addWidget(lbl('Avg'),5,1); g.addWidget(self.avg,5,2); g.addWidget(lbl('GUI Hz'),5,3); g.addWidget(self.gui_hz,5,4)
        for col,(text,sig,name) in enumerate([('SDR Power On',self.start_clicked,'primary'),('Release SDR',self.stop_clicked,''),('Cal',self.cal_clicked,''),('Start Log',self.log_start_clicked,''),('Stop Log',self.log_stop_clicked,'')], start=5):
            b=btn(text,name); b.clicked.connect(sig.emit); g.addWidget(b,5 if col<8 else 6,col if col<8 else col-3)
        g.addWidget(lbl('WT7 owns B210 while SDR power is on','muted'),6,1,1,4); self.a_hist=[]; self.b_hist=[]
    def meter_config(self):
        return B210PowerMeterConfig(center_frequency_hz=int(float(self.freq.text())*1_000_000), sample_rate_hz=int(float(self.rate.text())*1000), measurement_bandwidth_hz=int(float(self.bw.text())*1000), update_rate_hz=float(self.gui_hz.text()), gain_a_db=float(self.gain_a.text()), gain_b_db=float(self.gain_b.text()), clock_source=self.clock.text().strip() or 'internal', device_args=self.power.b210_device_args)
    def save_config(self):
        self.power.center_frequency_hz=int(float(self.freq.text())*1_000_000); self.power.sample_rate_hz=int(float(self.rate.text())*1000); self.power.measurement_bandwidth_hz=int(float(self.bw.text())*1000); self.power.update_rate_hz=float(self.gui_hz.text()); self.power.gain_db=self.gain_a.text(); self.power.gain_b_db=self.gain_b.text(); self.power.smoothing_samples=max(1,int(float(self.avg.text()))); self.power.clock_source=self.clock.text().strip() or 'internal'; return self.power
    def set_reading(self,r:B210PowerReading):
        keep=max(1,int(float(self.avg.text() or '1'))); self.a_hist=(self.a_hist+[r.power_a_dbfs])[-keep:]; self.b_hist=(self.b_hist+[r.power_b_dbfs])[-keep:]; aa=sum(self.a_hist)/len(self.a_hist); bb=sum(self.b_hist)/len(self.b_hist)
        self.a_val.setText(f'{aa:0.1f} dBFS'); self.b_val.setText(f'{bb:0.1f} dBFS'); self.a_stats.setText(f'Avg {aa:0.1f} Min {min(self.a_hist):0.1f} Max {max(self.a_hist):0.1f}'); self.b_stats.setText(f'Avg {bb:0.1f} Min {min(self.b_hist):0.1f} Max {max(self.b_hist):0.1f}')
    def power_channel_for_antenna(self, antenna_name: str) -> str:
        attrs = object.__getattribute__(self, '__dict__')
        app = attrs.get('app')
        cfg = getattr(app, 'power_config', None) or attrs.get('power') or PowerConfig()
        name = (antenna_name or '').strip().lower()
        if name == 'west': return (cfg.west_channel or 'B').strip().upper() or 'B'
        if name == 'east': return (cfg.east_channel or 'A').strip().upper() or 'A'
        return 'A'
    def current_power_measurement(self, antenna_name: str) -> Optional[dict[str, object]]:
        channel = self.power_channel_for_antenna(antenna_name)
        dbfs = getattr(self, 'latest_power_b_dbfs', None) if channel == 'B' else getattr(self, 'latest_power_dbfs', None)
        if dbfs is None: return None
        cal = getattr(self, 'active_calibrations', {}).get(channel)
        value = float(dbfs); unit = 'dBFS'; calibrated = False; extrapolated = False
        if cal:
            value, extrapolated = calibrated_dbm_from_dbfs(cal, float(dbfs))
            unit = 'dBm'; calibrated = True
        return {'power_value': value, 'power_dbfs': float(dbfs), 'power_unit': unit, 'power_channel': channel, 'power_calibrated': calibrated, 'power_extrapolated': extrapolated, 'sample_count': 1}
    def log_header(self) -> list[str]:
        return ['utc_time','ch_a_dbfs','ch_a_value','ch_a_unit','ch_a_calibrated','ch_b_dbfs','ch_b_value','ch_b_unit','ch_b_calibrated']
PowerMeterPanel = B210Panel
class WT7App(QWidget):
    def __init__(self,config_path):
        super().__init__(); self.config_path=Path(config_path); self.configs=load_configs(self.config_path); self.site=load_site_config(self.config_path); self.power_config=load_power_config(self.config_path); self.sources=load_sources(self.config_path); self.selected_source_name=self.site.selected_source if self.site.selected_source in self.sources else next(iter(self.sources), '')
        self.event_log=EventLogger(Path('logs'),self.site.log_retention_days,self.site.log_level); self.state_store=AppStateStore(); self.sessions={}; self.positions={}; self.cards={}; self.current_target=None; self.tracking_kind=''; self.tracking_stop=threading.Event(); self.jog_stops={}; self.b210_stop=threading.Event(); self.b210_thread=None; self.events=queue.Queue(); self.log_handle=None; self.log_writer=None
        self.setWindowTitle(f'WT7 ANTENNA CONTROLLER {APP_VERSION}'); self.resize(790,920); self.build_ui(); self.style_ui(); self.set_status('Load config, connect antennas, then use guarded jogs.'); self.event_log.info('APP_START',version=APP_VERSION,config=str(config_path))
        self.t_ref=QTimer(self); self.t_ref.timeout.connect(self.update_reference); self.t_ref.start(1000); self.t_evt=QTimer(self); self.t_evt.timeout.connect(self.process_events); self.t_evt.start(100); self.t_pos=QTimer(self); self.t_pos.timeout.connect(self.poll_positions); self.t_pos.start(1000)
    def build_ui(self):
        main=QVBoxLayout(self); main.setContentsMargins(26,12,26,12); main.setSpacing(8)
        row=QHBoxLayout(); row.addWidget(lbl('WT7 ANTENNA CONTROLLER','appTitle')); row.addWidget(lbl('READY','readyTag')); row.addWidget(lbl('PyQt5 alpha','muted')); row.addStretch(1); main.addLayout(row)
        quick=QHBoxLayout();
        for text,name,cb in [('Connect','primary',self.connect_all),('Disconnect','',self.disconnect_all),('Track','',lambda:self.start_tracking('source')),('Park','',self.park_all),('STOP ALL','danger',self.stop_all)]: b=btn(text,name); b.clicked.connect(cb); quick.addWidget(b)
        quick.addStretch(1); main.addLayout(quick)
        menu=Panel(); mg=QGridLayout(menu); mg.setContentsMargins(9,8,9,8); mg.setHorizontalSpacing(7); mg.setVerticalSpacing(7)
        for i,text in enumerate(['Limits','Observer','Sources','Encoders','Tracking','Calibration','Peak Cal','Scan Cal','Y Factor','Power','Event Log']): b=btn(text); b.clicked.connect(self.not_ported if text!='Event Log' else self.open_log_hint); mg.addWidget(b,i//9,i%9)
        main.addWidget(menu)
        src=Panel(); sr=QHBoxLayout(src); sr.setContentsMargins(8,8,8,8); sr.addWidget(bold('SOURCE')); self.source_name=lbl('Target --'); sr.addWidget(self.source_name); sr.addStretch(1); sr.addWidget(lbl('AZ','muted')); self.source_az=bold('--',14); sr.addWidget(self.source_az); sr.addStretch(1); sr.addWidget(lbl('EL','muted')); self.source_el=bold('--',14); sr.addWidget(self.source_el); sr.addStretch(1); sr.addWidget(lbl('HA','muted')); self.source_ha=bold('--',14); sr.addWidget(self.source_ha); main.addWidget(src)
        ref=Panel(); rg=QGridLayout(ref); rg.setContentsMargins(8,8,8,8); self.lmst=lbl('LMST --'); self.utc=lbl('UTC --'); self.local=lbl('Local --'); self.sun=bold('SUN AZ -- EL --'); self.moon=bold('MOON AZ -- EL --'); rg.addWidget(self.lmst,0,0); rg.addWidget(self.utc,0,1); rg.addWidget(self.local,0,2); rg.addWidget(self.sun,1,0); rg.addWidget(self.moon,1,1); main.addWidget(ref)
        self.status=lbl(''); main.addWidget(self.status)
        for name in self.configs:
            card=AntennaCard(name); card.jog_pressed.connect(self.start_jog); card.jog_released.connect(self.stop_jog); card.stop_clicked.connect(self.stop_antenna); self.cards[name]=card; main.addWidget(card)
        self.power=B210Panel(self.power_config); self.power.start_clicked.connect(self.start_b210); self.power.stop_clicked.connect(self.stop_b210); self.power.cal_clicked.connect(lambda:self.info('B210 calibration dialog will be ported after main PyQt5 shell validation.')); self.power.log_start_clicked.connect(self.start_b210_log); self.power.log_stop_clicked.connect(self.stop_b210_log); main.addWidget(self.power)
        ev=Panel(); eg=QGridLayout(ev); eg.setContentsMargins(9,8,9,8); eg.addWidget(bold('RECENT EVENTS'),0,0); ob=btn('Open Log'); ob.clicked.connect(self.open_log_hint); eg.addWidget(ob,0,3,Qt.AlignRight); self.ev1=lbl('--'); self.ev2=lbl('--','muted'); eg.addWidget(self.ev1,1,0,1,4); eg.addWidget(self.ev2,2,0,1,4); main.addWidget(ev); main.addStretch(1)
    def style_ui(self):
        self.setStyleSheet("""QWidget{background:#f6f6f5;color:#1f252b;font-family:Arial,Helvetica,sans-serif;font-size:10pt} QLabel#appTitle{font-weight:700} QLabel#muted{color:#8a8f95} QLabel#bold{font-weight:700} QFrame#panel{background:#f1f1f0;border:1px solid #dddddc} QPushButton{background:#fff;border:1px solid #d6d8da;border-radius:9px;padding:4px 9px;min-height:18px} QPushButton#primary{background:#121820;color:white;border-color:#121820} QPushButton#danger{color:#e74b2c;border-color:#e74b2c} QLineEdit{background:white;border:1px solid #d8dadd;border-radius:8px;padding:4px 8px} QLabel#readyTag,QLabel#stateGood{background:#ead7c9;border:1px solid #dcc2ae;padding:5px 8px} QLabel#stateBusy{background:#d9e7f8;border:1px solid #bed3ed;padding:5px 8px} QLabel#stateStopped{background:#eee;border:1px solid #d2d2d2;padding:5px 8px} QLabel#stateFault,QLabel#faultTag{background:#ffd9d9;color:#b00000;border:1px solid #e3a2a2;padding:5px 8px} QLabel#safe{background:#ead7c9;border:1px solid #dcc2ae;padding:5px 8px}""")
    def emit(self,fn,arg=None): self.events.put((fn,arg))
    def process_events(self):
        while True:
            try: fn,arg=self.events.get_nowait()
            except queue.Empty: break
            fn(arg)
    def set_status(self,msg):
        self.status.setText(msg); self.ev2.setText(self.ev1.text() if hasattr(self,'ev1') else '');
        if hasattr(self,'ev1'): self.ev1.setText(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")
        self.state_store.set_status(msg,SystemRunState.IDLE)
    def info(self,msg): QMessageBox.information(self,'WT7',msg)
    def not_ported(self): self.info('This WT7 PyQt5 alpha currently ports the main control surface. Secondary dialogs are still available in wt7_tk_legacy_gui.py during transition.')
    def open_log_hint(self): self.info('Event logs are in the logs directory beside the app.')
    def run_thread(self,fn,name='WT7Worker'): threading.Thread(target=fn,name=name,daemon=True).start()
    def connect_all(self):
        pending=[(n,c) for n,c in self.configs.items() if n not in self.sessions]
        if not pending: self.set_status('Already connected.'); return
        self.set_status('Connecting antennas...')
        for n,_ in pending: self.cards[n].set_state('CONNECTING')
        def one(n,c):
            try:
                s=SafeAntenna(c,self.motion_event); p=s.read_position(); s.update_oled_position(p.azimuth,p.elevation,'STOPPED'); self.emit(lambda data:self.finish_connect(*data),(n,s,p,''))
            except Exception as e: self.emit(lambda data:self.finish_connect(*data),(n,None,None,str(e)))
        for n,c in pending: self.run_thread(lambda n=n,c=c: one(n,c),f'Connect{n}')
    def finish_connect(self,name,session,pos,error):
        if session:
            self.sessions[name]=session; self.positions[name]=pos; self.cards[name].set_position(pos); self.cards[name].set_state('STOPPED'); self.set_status(f'{name} connected. Connected {len(self.sessions)}/{len(self.configs)} antennas.')
        else: self.cards[name].set_state('FAULT'); self.set_status(f'{name} connect fault: {error}')
    def disconnect_all(self):
        self.stop_all(); sessions=list(self.sessions.items()); self.sessions.clear()
        def worker():
            for n,s in sessions:
                try: s.close()
                except Exception: pass
                self.emit(lambda name:self.finish_disconnect(name),n)
        self.run_thread(worker,'Disconnect')
    def finish_disconnect(self,name):
        self.positions.pop(name,None); self.cards[name].set_position(None); self.cards[name].set_target(None,None); self.cards[name].set_state('DISCONNECTED'); self.set_status(f'{name} disconnected.')
    def stop_all(self):
        self.tracking_stop.set(); [ev.set() for ev in self.jog_stops.values()]
        for n,s in list(self.sessions.items()): self.run_thread(lambda s=s:s.stop_all(),f'Stop{n}'); self.cards[n].set_state('STOPPED')
        self.set_status('Stopped.')
    def stop_antenna(self,name):
        self.stop_jog(name); s=self.sessions.get(name)
        if s: self.run_thread(lambda:s.stop_all(),f'Stop{name}')
        self.cards[name].set_state('STOPPED')
    def start_jog(self,name,direction_text):
        s=self.sessions.get(name)
        if not s: self.set_status(f'{name} is not connected.'); return
        stop=threading.Event(); self.jog_stops[name]=stop; direction=Direction(direction_text); self.cards[name].set_state('MANUAL')
        def update(p): self.emit(lambda data:self.update_position(*data),(name,p))
        def worker():
            try: s.guarded_jog(direction,s.config.gui_speed,None,stop,update)
            except Exception as e: self.emit(lambda data:self.mark_fault(*data),(name,str(e)))
            finally: self.emit(lambda n:self.cards[n].set_state('STOPPED'),name)
        self.run_thread(worker,f'Jog{name}')
    def stop_jog(self,name):
        ev=self.jog_stops.get(name)
        if ev: ev.set()
    def start_tracking(self,kind):
        if not self.sessions: self.set_status('Connect antennas before tracking.'); return
        self.tracking_stop.set(); self.tracking_stop=threading.Event(); self.tracking_kind=kind
        for n in self.sessions: self.cards[n].set_state('TRACKING')
        self.run_thread(lambda:self.tracking_loop(kind),'Tracking'); self.set_status(f'Tracking {kind.title()}.')
    def tracking_loop(self,kind):
        try:
            while not self.tracking_stop.is_set():
                target=self.current_tracking_target(kind); self.emit(lambda t:self.apply_target(t),target); self.slew_all_to_target(target,'TRACKING',self.tracking_stop)
                until=time.monotonic()+max(0.1,self.site.track_interval_seconds)
                while not self.tracking_stop.is_set() and time.monotonic()<until: time.sleep(0.1)
        except Exception as e: self.emit(lambda m:self.set_status(f'Tracking fault: {m}'),str(e))
    def park_all(self):
        if not self.sessions: self.set_status('Connect antennas before parking.'); return
        self.tracking_stop.set(); stop=threading.Event(); self.set_status('Parking antennas.')
        def worker():
            for n,s in list(self.sessions.items()):
                try:
                    self.emit(lambda name:self.cards[name].set_state('PARKING'),n); s.config.limits.assert_position_allowed(s.config.park_az,s.config.park_el)
                    s.guarded_slew_to(s.config.park_az,s.config.park_el,s.config.az_track_speed,s.config.el_track_speed,stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p,n=n:self.emit(lambda data:self.update_position(*data),(n,p)))
                    self.emit(lambda name:self.cards[name].set_state('PARKED'),n)
                except Exception as e: self.emit(lambda data:self.mark_fault(*data),(n,str(e)))
            self.emit(lambda m:self.set_status(m),'Park complete.')
        self.run_thread(worker,'Park')
    def slew_all_to_target(self,target,activity,stop):
        threads=[]
        for n,s in list(self.sessions.items()):
            def worker(n=n,s=s):
                try:
                    self.emit(lambda name:self.cards[name].set_state('SLEWING'),n); s.config.limits.assert_position_allowed(target.azimuth,target.elevation)
                    s.guarded_slew_to(target.azimuth,target.elevation,s.config.az_track_speed,s.config.el_track_speed,stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p,n=n:self.emit(lambda data:self.update_position(*data),(n,p)))
                    if not stop.is_set(): self.emit(lambda name:self.cards[name].set_state(activity),n)
                except Exception as e: self.emit(lambda data:self.mark_fault(*data),(n,str(e)))
            t=threading.Thread(target=worker,daemon=True); threads.append(t); t.start()
        for t in threads: t.join()
    def az_tol(self): return abs(self.site.az_track_tolerance_degrees)
    def el_tol(self): return abs(self.site.el_track_tolerance_degrees)
    def current_tracking_target(self,kind):
        if kind=='sun': return self.target_for_kind('sun')
        if kind=='moon': return self.target_for_kind('moon')
        if not self.selected_source_name or self.selected_source_name not in self.sources: raise RuntimeError('No selected source in config.')
        src=self.sources[self.selected_source_name]; return source_position(src.name,src.ra_hours,src.dec_degrees,self.site.latitude,self.site.longitude)
    def target_for_kind(self,kind):
        if kind=='sun':
            s=sun_position(self.site.latitude,self.site.longitude); return TargetPosition('Sun',s.azimuth,s.elevation)
        m=moon_position(self.site.latitude,self.site.longitude); return TargetPosition('Moon',m.azimuth,m.elevation)
    def apply_target(self,target):
        self.current_target=target; self.source_name.setText(target.name); self.source_az.setText(f'{target.azimuth:0.2f}'); self.source_el.setText(f'{target.elevation:0.2f}'); self.source_ha.setText(self.hour_angle(target.name))
        for n,c in self.cards.items(): c.set_target(target,self.positions.get(n))
    def hour_angle(self,name):
        now=datetime.now(timezone.utc); lst=local_sidereal_time(self.site.longitude,now)/15.0
        if name=='Sun': ra=sun_equatorial(now).ra_hours
        elif name=='Moon': ra=moon_equatorial(now)[0].ra_hours
        elif name in self.sources: ra=self.sources[name].ra_hours
        else: return '--'
        return ha_text(lst-ra)
    def update_reference(self):
        now=datetime.now().astimezone(); utc=now.astimezone(timezone.utc); self.local.setText(f'Local {now:%Y-%m-%d %H:%M:%S %Z}'); self.utc.setText(f'UTC {utc:%Y-%m-%d %H:%M:%S}'); self.lmst.setText(f'LMST {hms(local_sidereal_time(self.site.longitude,utc)/15.0*3600)}')
        sun=self.target_for_kind('sun'); moon=self.target_for_kind('moon'); self.sun.setText(f'SUN AZ {sun.azimuth:0.2f} EL {sun.elevation:0.2f}'); self.moon.setText(f'MOON AZ {moon.azimuth:0.2f} EL {moon.elevation:0.2f}')
        if self.tracking_kind:
            try: self.apply_target(self.current_tracking_target(self.tracking_kind))
            except Exception as e: self.set_status(f'Target update fault: {e}')
    def poll_positions(self):
        if not self.sessions: return
        def worker():
            for n,s in list(self.sessions.items()):
                try: self.emit(lambda data:self.update_position(*data),(n,s.read_position()))
                except Exception as e: self.emit(lambda data:self.mark_fault(*data),(n,str(e)))
        self.run_thread(worker,'Poll')
    def update_position(self,name,pos):
        self.positions[name]=pos; c=self.cards[name]; c.set_position(pos); c.set_target(self.current_target,pos); cfg=self.configs[name]; c.set_limits_ok(cfg.limits.is_az_allowed(pos.azimuth) and cfg.limits.is_el_allowed(pos.elevation))
    def mark_fault(self,name,error):
        self.cards[name].set_state('FAULT'); self.set_status(f'{name}: {error}'); self.event_log.error('ANTENNA_FAULT',antenna=name,error=error)
    def motion_event(self,event,payload): self.event_log.debug(event,payload=payload)
    def start_b210(self):
        if self.b210_thread and self.b210_thread.is_alive(): self.set_status('B210 already running.'); return
        try: cfg=self.power.meter_config(); self.power_config=self.power.save_config(); save_power_config(self.config_path,self.power_config)
        except Exception as e: self.set_status(f'B210 config fault: {e}'); return
        self.b210_stop.clear(); self.power.status.setText('SDR POWER ON UNCAL')
        def worker():
            try:
                with B210PowerMeter(cfg) as meter:
                    while not self.b210_stop.is_set(): self.emit(lambda r:self.handle_b210(r),meter.read_power())
            except Exception as e:
                if not self.b210_stop.is_set(): self.emit(lambda m:self.set_status(f'B210 fault: {m}'),str(e))
            finally: self.emit(lambda _x:self.power.status.setText('SDR RELEASED'),None)
        self.b210_thread=threading.Thread(target=worker,daemon=True); self.b210_thread.start(); self.set_status('B210 power started.')
    def stop_b210(self): self.b210_stop.set(); self.stop_b210_log(); self.set_status('B210 release requested.')
    def handle_b210(self,r):
        self.power.set_reading(r)
        if self.log_writer: self.log_writer.writerow([r.timestamp.isoformat(),f'{r.power_a_dbfs:0.3f}',f'{r.power_b_dbfs:0.3f}',r.sample_count]); self.log_handle.flush()
    def start_b210_log(self):
        if self.log_handle: self.set_status('B210 log already running.'); return
        path=Path(f'wt7_power_{datetime.now():%Y%m%d-%H%M%S}.csv'); self.log_handle=path.open('w',newline='',encoding='utf-8'); self.log_writer=csv.writer(self.log_handle); self.log_writer.writerow(['utc_time','ch_a_dbfs','ch_b_dbfs','samples']); self.set_status(f'B210 log started: {path.name}')
    def stop_b210_log(self):
        if self.log_handle: self.log_handle.close(); self.set_status('B210 log stopped.')
        self.log_handle=None; self.log_writer=None
    def closeEvent(self,event):
        self.event_log.info('APP_STOP',version=APP_VERSION); self.stop_b210(); self.stop_all()
        for s in list(self.sessions.values()):
            try: s.close()
            except Exception: pass
        event.accept()
def parse_args():
    p=argparse.ArgumentParser(description='Launch WT7 PyQt5 antenna GUI.'); p.add_argument('--config',default='wt7_ubuntu.ini',help='Config file. Default: wt7_ubuntu.ini'); return p.parse_args()
def main():
    args=parse_args(); app=QApplication([]); w=WT7App(args.config); w.show(); return app.exec_()
if __name__=='__main__': raise SystemExit(main())
