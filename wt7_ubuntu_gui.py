
#!/usr/bin/env python3
"""WT7 PyQt5 antenna controller GUI alpha."""
from __future__ import annotations
import argparse, csv, math, queue, threading, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from PyQt5.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QPainter, QPen, QColor
from PyQt5.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
from wt7_antenna import Axis, Direction, Position, SafeAntenna, shortest_angle_delta
from wt7_astro import TargetPosition, local_sidereal_time, moon_equatorial, moon_position, source_position
from wt7_b210_power import B210PowerMeter, B210PowerMeterConfig, B210PowerReading
from wt7_config import PowerConfig, ScanConfig, SourceConfig, YFactorConfig, calibrated_dbm_from_dbfs, load_configs, load_power_config, load_scan_config, load_site_config, load_sources, load_yfactor_config, save_configs, save_power_config, save_scan_config, save_site_config, save_sources, save_yfactor_config
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
        super().__init__(); self.name=name; self.setMinimumHeight(126); self.setMaximumHeight(138)
        g=QGridLayout(self); g.setContentsMargins(10,8,10,8); g.setHorizontalSpacing(10); g.setVerticalSpacing(5)
        title=bold(name.upper()); title.setMinimumWidth(72); self.state=lbl('DISCONNECTED','stateStopped')
        self.az=bold('--',18); self.el=bold('--',18); self.az.setMinimumWidth(78); self.el.setMinimumWidth(78)
        self.az_err=lbl('--'); self.el_err=lbl('--'); self.az_err.setMinimumWidth(58); self.el_err.setMinimumWidth(58)
        self.limits=lbl('SAFE','safe'); self.limits.setMinimumWidth(72); self.mode=lbl('--'); self.mode.setMinimumWidth(70); self.target=lbl('--'); self.target.setMinimumWidth(130)
        g.addWidget(title,0,0,1,2); g.addWidget(self.state,0,8,1,2,Qt.AlignRight)
        g.addWidget(lbl('AZ','muted'),1,0); g.addWidget(self.az,1,1); g.addWidget(lbl('AZ err','muted'),1,2); g.addWidget(self.az_err,1,3); g.addWidget(lbl('Limits','muted'),1,4); g.addWidget(self.limits,1,5)
        g.addWidget(lbl('EL','muted'),2,0); g.addWidget(self.el,2,1); g.addWidget(lbl('EL err','muted'),2,2); g.addWidget(self.el_err,2,3); g.addWidget(lbl('Mode','muted'),2,4); g.addWidget(self.mode,2,5)
        g.addWidget(lbl('Target','muted'),3,2); g.addWidget(self.target,3,3,1,3)
        c=QWidget(); c.setObjectName('manualPad'); cg=QGridLayout(c); cg.setContentsMargins(0,0,0,0); cg.setHorizontalSpacing(6); cg.setVerticalSpacing(6)
        for text,direction,row,col in [('EL+',Direction.EL_UP.value,0,1),('AZ-',Direction.AZ_CCW.value,1,0),('AZ+',Direction.AZ_CW.value,1,2),('EL-',Direction.EL_DOWN.value,2,1)]:
            b=btn(text); b.setFixedSize(64,30); b.pressed.connect(lambda d=direction: self.jog_pressed.emit(self.name,d)); b.released.connect(lambda: self.jog_released.emit(self.name)); cg.addWidget(b,row,col)
        stop=btn('STOP'); stop.setFixedSize(64,30); stop.clicked.connect(lambda: self.stop_clicked.emit(self.name)); cg.addWidget(stop,1,1)
        g.addWidget(c,1,7,3,3,Qt.AlignRight|Qt.AlignVCenter)
        g.setColumnMinimumWidth(6,14); g.setColumnStretch(6,1)
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
        super().__init__(); self.power=power; self.setMinimumHeight(142); self.setMaximumHeight(160)
        g=QGridLayout(self); g.setContentsMargins(10,8,10,8); g.setHorizontalSpacing(10); g.setVerticalSpacing(5)
        self.status=lbl('SDR RELEASED'); self.status.setMinimumWidth(170)
        self.a_val=bold('--.- dBFS',14); self.b_val=bold('--.- dBFS',14); self.a_stats=lbl('Avg -- Min -- Max --'); self.b_stats=lbl('Avg -- Min -- Max --')
        self.gain_a=edit(power.gain_db,58); self.gain_b=edit(power.gain_b_db,58); self.freq=edit(f'{power.center_frequency_hz/1_000_000:0.1f}',72); self.rate=edit(f'{power.sample_rate_hz/1000:0.0f}',64); self.bw=edit(f'{power.measurement_bandwidth_hz/1000:0.0f}',64); self.clock=edit(power.clock_source or 'internal',78); self.avg=edit(power.smoothing_samples,54); self.gui_hz=edit(f'{power.update_rate_hz:0.0f}',54)
        g.addWidget(bold('B210'),0,0); g.addWidget(self.status,1,0,2,1,Qt.AlignTop)
        g.addWidget(bold('CH A'),0,1); g.addWidget(self.a_val,0,2); g.addWidget(lbl('Gain','muted'),1,1); g.addWidget(self.gain_a,1,2); g.addWidget(self.a_stats,2,1,1,2)
        g.addWidget(bold('CH B'),0,4); g.addWidget(self.b_val,0,5); g.addWidget(lbl('Gain','muted'),1,4); g.addWidget(self.gain_b,1,5); g.addWidget(self.b_stats,2,4,1,2)
        params=[('Freq MHz',self.freq),('Rate ksps',self.rate),('BW kHz',self.bw),('Clock',self.clock),('Avg',self.avg),('GUI Hz',self.gui_hz)]
        for i,(k,w) in enumerate(params): g.addWidget(lbl(k),3,i*2); g.addWidget(w,3,i*2+1)
        actions=[('SDR Power On',self.start_clicked,'primary'),('Release SDR',self.stop_clicked,''),('Cal',self.cal_clicked,''),('Start Log',self.log_start_clicked,''),('Stop Log',self.log_stop_clicked,'')]
        for i,(text,sig,name) in enumerate(actions):
            b=btn(text,name); b.clicked.connect(sig.emit); g.addWidget(b,4,i+1)
        g.addWidget(lbl('WT7 owns B210 while SDR power is on','muted'),4,6,1,6)
        g.setColumnStretch(3,1); g.setColumnStretch(6,1); g.setColumnStretch(12,1); self.a_hist=[]; self.b_hist=[]
    def meter_config(self):
        return B210PowerMeterConfig(center_frequency_hz=int(float(self.freq.text())*1_000_000), sample_rate_hz=int(float(self.rate.text())*1000), measurement_bandwidth_hz=int(float(self.bw.text())*1000), update_rate_hz=float(self.gui_hz.text()), gain_a_db=float(self.gain_a.text()), gain_b_db=float(self.gain_b.text()), clock_source=self.clock.text().strip() or 'internal', device_args=self.power.b210_device_args)
    def save_config(self):
        self.power.center_frequency_hz=int(float(self.freq.text())*1_000_000); self.power.sample_rate_hz=int(float(self.rate.text())*1000); self.power.measurement_bandwidth_hz=int(float(self.bw.text())*1000); self.power.update_rate_hz=float(self.gui_hz.text()); self.power.gain_db=self.gain_a.text(); self.power.gain_b_db=self.gain_b.text(); self.power.smoothing_samples=max(1,int(float(self.avg.text()))); self.power.clock_source=self.clock.text().strip() or 'internal'; return self.power
    def set_reading(self,r:B210PowerReading):
        keep=max(1,int(float(self.avg.text() or '1'))); self.a_hist=(self.a_hist+[r.power_a_dbfs])[-keep:]; self.b_hist=(self.b_hist+[r.power_b_dbfs])[-keep:]; aa=sum(self.a_hist)/len(self.a_hist); bb=sum(self.b_hist)/len(self.b_hist)
        self.latest_power_dbfs=aa; self.latest_power_b_dbfs=bb; self.active_calibrations=getattr(self,'active_calibrations',{})
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

class ScanPlotWidget(QWidget):
    def __init__(self, axis, rows, parent=None):
        super().__init__(parent); self.axis=axis; self.rows=rows; self.summary_text='Fit --'; self.setMinimumSize(560,340)
    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        w,h=self.width(),self.height(); left,right,top,bottom=58,w-22,22,h-48
        painter.fillRect(0,0,w,h,QColor('white'))
        points=[(float(r['offset_degrees']),float(r['power_value'])) for r in self.rows if r.get('power_value') is not None]
        if not points:
            self.summary_text='Fit unavailable: no scan data'; painter.drawText(20,40,'No scan data'); return
        points=sorted(points); xs=[p[0] for p in points]; ys=[p[1] for p in points]
        fit=self.fit_gaussian_with_slope(points); fit_points=[]
        if fit:
            for i in range(121):
                x=min(xs)+(max(xs)-min(xs))*i/120.0; fit_points.append((x,self.evaluate_fit(fit,x)))
        all_y=ys+[y for _x,y in fit_points]; minx,maxx=min(xs),max(xs); miny,maxy=min(all_y),max(all_y)
        if minx==maxx: minx-=1; maxx+=1
        if miny==maxy: miny-=0.5; maxy+=0.5
        pad=(maxy-miny)*0.08; miny-=pad; maxy+=pad
        def px(x): return left+(x-minx)/(maxx-minx)*(right-left)
        def py(y): return bottom-(y-miny)/(maxy-miny)*(bottom-top)
        grid=QPen(QColor('#d8d8d8'),1); painter.setPen(grid)
        for i in range(6):
            fx=i/5; x=left+fx*(right-left); y=bottom-fx*(bottom-top)
            painter.drawLine(int(x),top,int(x),bottom); painter.drawLine(left,int(y),right,int(y))
            painter.setPen(QColor('#333333')); painter.drawText(int(x)-18,bottom+18,f'{minx+fx*(maxx-minx):0.1f}'); painter.drawText(4,int(y)+4,f'{miny+fx*(maxy-miny):0.1f}'); painter.setPen(grid)
        painter.setPen(QPen(QColor('#222222'),1)); painter.drawLine(left,bottom,right,bottom); painter.drawLine(left,top,left,bottom)
        if minx <= 0 <= maxx:
            pen=QPen(QColor('#555555'),1); pen.setStyle(Qt.DashLine); painter.setPen(pen); x=int(px(0)); painter.drawLine(x,top,x,bottom); painter.drawText(x+4,top+14,'boresight')
        if fit_points:
            painter.setPen(QPen(QColor('#d62728'),2)); last=None
            for xval,yval in fit_points:
                pnt=(int(px(xval)),int(py(yval)))
                if last: painter.drawLine(last[0],last[1],pnt[0],pnt[1])
                last=pnt
        trace=QPen(QColor('#0057b8'),2); painter.setPen(trace); last=None
        for xval,yval in points:
            pnt=(int(px(xval)),int(py(yval)))
            if last: painter.drawLine(last[0],last[1],pnt[0],pnt[1])
            last=pnt
        painter.setBrush(QColor('#0057b8')); painter.setPen(QPen(QColor('#0057b8'),1))
        for xval,yval in points: painter.drawEllipse(int(px(xval))-3,int(py(yval))-3,6,6)
        painter.setPen(QColor('#111111')); painter.drawText((left+right)//2-55,h-12,f'{self.axis.value} offset degrees')
        unit=str(self.rows[-1].get('power_unit','dBFS')); painter.save(); painter.translate(16,(top+bottom)//2+35); painter.rotate(-90); painter.drawText(0,0,unit); painter.restore()
        if fit:
            fwhm=2.35482*fit['sigma']; self.summary_text=f"Boresight error {fit['center']:+0.3f} deg; FWHM {fwhm:0.3f} deg; peak {fit['peak']:0.2f} {unit}; RMS {fit['rms']:0.3f} dB"
        else:
            self.summary_text='Gaussian fit unavailable'
    def fit_gaussian_with_slope(self,points):
        if len(points)<5: return None
        points=sorted(points); xs=[p[0] for p in points]; ys=[p[1] for p in points]; min_x,max_x=min(xs),max(xs); span=max_x-min_x
        if span<=0.0: return None
        peak_x=xs[ys.index(max(ys))]; sigma_min=max(span/30.0,0.02); sigma_max=max(span,sigma_min*2.0); center_start=max(min_x,peak_x-span*0.25); center_stop=min(max_x,peak_x+span*0.25); best=None
        for center in self.fit_range(center_start,center_stop,41):
            for sigma in self.fit_range(sigma_min,sigma_max,50):
                fit=self.solve_linear_fit(points,center,sigma)
                if fit and fit['amplitude']>0.0 and (best is None or fit['sse']<best['sse']): best=fit
        if not best: return None
        for center_width,sigma_factor in ((span*0.08,0.35),(span*0.03,0.18)):
            center_start=max(min_x,best['center']-center_width); center_stop=min(max_x,best['center']+center_width); sigma_start=max(sigma_min,best['sigma']*(1.0-sigma_factor)); sigma_stop=min(sigma_max,best['sigma']*(1.0+sigma_factor))
            for center in self.fit_range(center_start,center_stop,41):
                for sigma in self.fit_range(sigma_start,sigma_stop,41):
                    fit=self.solve_linear_fit(points,center,sigma)
                    if fit and fit['amplitude']>0.0 and fit['sse']<best['sse']: best=fit
        best['rms']=math.sqrt(best['sse']/len(points)); best['peak']=self.evaluate_fit(best,best['center']); return best
    def fit_range(self,start,stop,count):
        if count<=1 or start==stop: return [start]
        return [start+(stop-start)*i/(count-1) for i in range(count)]
    def solve_linear_fit(self,points,center,sigma):
        normal=[[0.0 for _ in range(3)] for _ in range(3)]; rhs=[0.0,0.0,0.0]
        for x,y in points:
            g=math.exp(-0.5*((x-center)/sigma)**2); vals=(1.0,x,g)
            for i in range(3):
                rhs[i]+=vals[i]*y
                for j in range(3): normal[i][j]+=vals[i]*vals[j]
        solution=self.solve_3x3(normal,rhs)
        if solution is None: return None
        baseline,slope,amplitude=solution; sse=0.0
        for x,y in points:
            predicted=baseline+slope*x+amplitude*math.exp(-0.5*((x-center)/sigma)**2); sse+=(y-predicted)**2
        return {'baseline':baseline,'slope':slope,'amplitude':amplitude,'center':center,'sigma':sigma,'sse':sse}
    def solve_3x3(self,matrix,rhs):
        a=[matrix[row][:]+[rhs[row]] for row in range(3)]
        for col in range(3):
            pivot=max(range(col,3),key=lambda row: abs(a[row][col]))
            if abs(a[pivot][col])<1e-12: return None
            a[col],a[pivot]=a[pivot],a[col]; pv=a[col][col]
            for item in range(col,4): a[col][item]/=pv
            for row in range(3):
                if row==col: continue
                factor=a[row][col]
                for item in range(col,4): a[row][item]-=factor*a[col][item]
        return [a[row][3] for row in range(3)]
    def evaluate_fit(self,fit,x):
        return fit['baseline']+fit['slope']*x+fit['amplitude']*math.exp(-0.5*((x-fit['center'])/fit['sigma'])**2)
class SimpleDialog(QDialog):
    def __init__(self, app, title):
        super().__init__(app); self.app=app; self.setWindowTitle(title); self.main=QVBoxLayout(self); self.main.setContentsMargins(12,12,12,12); self.main.setSpacing(8)
    def buttons(self):
        box=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel); box.accepted.connect(self.accept); box.rejected.connect(self.reject); self.main.addWidget(box); return box
    def edit(self, value, width=90):
        w=edit(value,width); return w
    def to_float(self, widget, name):
        try: return float(widget.text())
        except ValueError: raise ValueError(f'{name} must be numeric')
    def to_int(self, widget, name):
        try: return int(float(widget.text()))
        except ValueError: raise ValueError(f'{name} must be numeric')
    def fail(self, exc): QMessageBox.warning(self,self.windowTitle(),str(exc))

class LimitsDialog(SimpleDialog):
    keys=[('az_min','AZ min'),('az_max','AZ max'),('el_min','EL min'),('el_max','EL max'),('az_margin','AZ margin'),('el_margin','EL margin'),('max_jog_seconds','Max jog sec'),('poll_interval','Poll sec'),('park_az','Park AZ'),('park_el','Park EL')]
    def __init__(self,app):
        super().__init__(app,'Limits'); self.fields={}; tabs=QTabWidget(); self.main.addWidget(tabs)
        for name,cfg in app.configs.items():
            page=QWidget(); g=QGridLayout(page); g.setContentsMargins(8,8,8,8); f={}; vals={**cfg.limits.__dict__,'park_az':cfg.park_az,'park_el':cfg.park_el}
            for r,(key,label) in enumerate(self.keys): g.addWidget(lbl(label),r,0); f[key]=self.edit(vals[key]); g.addWidget(f[key],r,1)
            self.fields[name]=f; tabs.addTab(page,name)
        self.buttons()
    def accept(self):
        try:
            for name,f in self.fields.items():
                cfg=self.app.configs[name]; cfg.limits.az_min=self.to_float(f['az_min'],'AZ min'); cfg.limits.az_max=self.to_float(f['az_max'],'AZ max'); cfg.limits.el_min=self.to_float(f['el_min'],'EL min'); cfg.limits.el_max=self.to_float(f['el_max'],'EL max'); cfg.limits.az_margin=self.to_float(f['az_margin'],'AZ margin'); cfg.limits.el_margin=self.to_float(f['el_margin'],'EL margin'); cfg.limits.max_jog_seconds=self.to_float(f['max_jog_seconds'],'Max jog'); cfg.limits.poll_interval=self.to_float(f['poll_interval'],'Poll sec'); cfg.park_az=self.to_float(f['park_az'],'Park AZ'); cfg.park_el=self.to_float(f['park_el'],'Park EL'); cfg.limits.assert_position_allowed(cfg.park_az,cfg.park_el)
            save_configs(self.app.config_path,self.app.configs); self.app.set_status('Limits saved.'); super().accept()
        except Exception as exc: self.fail(exc)

class ObserverDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Observer'); g=QGridLayout(); self.main.addLayout(g); s=app.site
        self.lat=self.edit(s.latitude); self.lon=self.edit(s.longitude); self.timeout_enabled=QCheckBox('Enable disconnect timeout'); self.timeout_enabled.setChecked(bool(s.timeout_enabled)); self.timeout_min=self.edit(s.timeout_minutes); self.retention=self.edit(s.log_retention_days)
        for r,(label,w) in enumerate([('Latitude',self.lat),('Longitude',self.lon),('Timeout min',self.timeout_min),('Log retention days',self.retention)]): g.addWidget(lbl(label),r,0); g.addWidget(w,r,1)
        g.addWidget(self.timeout_enabled,4,0,1,2); self.buttons()
    def accept(self):
        try:
            self.app.site.latitude=self.to_float(self.lat,'Latitude'); self.app.site.longitude=self.to_float(self.lon,'Longitude'); self.app.site.timeout_enabled=self.timeout_enabled.isChecked(); self.app.site.timeout_minutes=self.to_float(self.timeout_min,'Timeout min'); self.app.site.log_retention_days=max(1,self.to_int(self.retention,'Log retention'))
            save_site_config(self.app.config_path,self.app.site); self.app.set_status('Observer saved.'); self.app.update_reference(); super().accept()
        except Exception as exc: self.fail(exc)

class TrackingDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Tracking'); tabs=QTabWidget(); self.main.addWidget(tabs)
        site=QWidget(); sg=QGridLayout(site); self.site_fields={}
        specs=[('track_interval_seconds','Interval sec'),('az_track_tolerance_degrees','AZ start tol'),('el_track_tolerance_degrees','EL start tol'),('az_stop_tolerance_degrees','AZ stop tol'),('el_stop_tolerance_degrees','EL stop tol'),('az_slow_speed','AZ slow speed'),('el_slow_speed','EL slow speed'),('az_slow_threshold_degrees','AZ slow deg'),('el_slow_threshold_degrees','EL slow deg')]
        for r,(key,label) in enumerate(specs): self.site_fields[key]=self.edit(getattr(app.site,key)); sg.addWidget(lbl(label),r,0); sg.addWidget(self.site_fields[key],r,1)
        tabs.addTab(site,'Global'); self.ant_fields={}
        for name,cfg in app.configs.items():
            page=QWidget(); g=QGridLayout(page); f={}; specs=[('gui_speed','Manual speed'),('az_track_speed','AZ speed'),('el_track_speed','EL speed'),('az_low_to_high_compensation','AZ L-H comp')]
            for r,(key,label) in enumerate(specs): f[key]=self.edit(getattr(cfg,key)); g.addWidget(lbl(label),r,0); g.addWidget(f[key],r,1)
            self.ant_fields[name]=f; tabs.addTab(page,name)
        self.buttons()
    def accept(self):
        try:
            for key,w in self.site_fields.items(): setattr(self.app.site,key,self.to_int(w,key) if key.endswith('speed') else self.to_float(w,key))
            for name,f in self.ant_fields.items():
                cfg=self.app.configs[name]; cfg.gui_speed=self.to_int(f['gui_speed'],'Manual speed'); cfg.az_track_speed=self.to_int(f['az_track_speed'],'AZ speed'); cfg.el_track_speed=self.to_int(f['el_track_speed'],'EL speed'); cfg.az_low_to_high_compensation=self.to_float(f['az_low_to_high_compensation'],'AZ L-H comp')
            save_site_config(self.app.config_path,self.app.site); save_configs(self.app.config_path,self.app.configs); self.app.set_status('Tracking settings saved.'); super().accept()
        except Exception as exc: self.fail(exc)

class SourcesDialog(SimpleDialog):
    NAME_COL=0; RA_COL=1; DEC_COL=2; AZ_COL=3; EL_COL=4; FLUX_COL=5
    def __init__(self,app):
        super().__init__(app,'Sources'); self.table=QTableWidget(0,6); self.table.setHorizontalHeaderLabels(['Name','RA h','Dec deg','Current AZ','Current EL','Flux 4800 MHz']); self.main.addWidget(self.table)
        for src in app.sources.values(): self.add_row(src)
        row=QHBoxLayout(); self.selected=QComboBox(); self.refresh_selected_items(); self.selected.setCurrentText(app.selected_source_name); add=btn('Add Row'); delete=btn('Delete Row'); add.clicked.connect(self.add_new_source); delete.clicked.connect(self.delete_row); row.addWidget(lbl('Selected')); row.addWidget(self.selected); row.addWidget(add); row.addWidget(delete); row.addStretch(1); self.main.addLayout(row); self.resize(760,420); self.buttons()
        self.table.itemChanged.connect(self.on_item_changed); self.table.itemSelectionChanged.connect(self.on_table_selection_changed); self.timer=QTimer(self); self.timer.timeout.connect(self.update_current_positions); self.timer.start(1000); self.update_current_positions()
    def readonly_item(self,value='--'):
        item=QTableWidgetItem(str(value)); item.setFlags(item.flags() & ~Qt.ItemIsEditable); return item
    def add_row(self,src):
        r=self.table.rowCount(); self.table.insertRow(r)
        for c,val in enumerate([src.name,src.ra_hours,src.dec_degrees]): self.table.setItem(r,c,QTableWidgetItem(str(val)))
        self.table.setItem(r,self.AZ_COL,self.readonly_item('--')); self.table.setItem(r,self.EL_COL,self.readonly_item('--')); self.table.setItem(r,self.FLUX_COL,QTableWidgetItem(str(src.flux_4800_mhz)))
    def add_new_source(self):
        self.add_row(SourceConfig('New Source',0,0,0)); self.refresh_selected_items()
    def on_table_selection_changed(self):
        r=self.table.currentRow()
        if r < 0: return
        item=self.table.item(r,self.NAME_COL); name=(item.text() if item else '').strip()
        if name: self.selected.setCurrentText(name)
    def select_source(self):
        self.refresh_selected_items(); selected=self.selected.currentText().strip()
        if not selected:
            self.fail(ValueError('Select a source first.')); return
        self.app.selected_source_name=selected; self.app.site.selected_source=selected; save_sources(self.app.config_path,self.read_sources_from_table(),selected); self.app.sources=self.read_sources_from_table(); self.app.update_reference(); self.app.set_status(f'Source selected: {selected}.')
    def read_sources_from_table(self):
        sources={}
        for r in range(self.table.rowCount()):
            name=(self.table.item(r,self.NAME_COL).text() if self.table.item(r,self.NAME_COL) else '').strip()
            if not name: continue
            sources[name]=SourceConfig(name,float(self.table.item(r,self.RA_COL).text()),float(self.table.item(r,self.DEC_COL).text()),float(self.table.item(r,self.FLUX_COL).text()))
        return sources
    def delete_row(self):
        r=self.table.currentRow()
        if r >= 0: self.table.removeRow(r); self.refresh_selected_items(); self.update_current_positions()
    def refresh_selected_items(self):
        current=self.selected.currentText().strip() if hasattr(self,'selected') else ''
        names=[]
        for r in range(self.table.rowCount()):
            item=self.table.item(r,self.NAME_COL); name=(item.text() if item else '').strip()
            if name: names.append(name)
        if not hasattr(self,'selected'): return
        self.selected.blockSignals(True); self.selected.clear(); self.selected.addItems(names); self.selected.setCurrentText(current if current in names else (names[0] if names else '')); self.selected.blockSignals(False)
    def on_item_changed(self,item):
        if item and item.column() == self.NAME_COL: self.refresh_selected_items()
    def update_current_positions(self):
        for r in range(self.table.rowCount()):
            try:
                name=(self.table.item(r,self.NAME_COL).text() if self.table.item(r,self.NAME_COL) else '').strip() or 'Source'
                ra=float(self.table.item(r,self.RA_COL).text()); dec=float(self.table.item(r,self.DEC_COL).text())
                pos=source_position(name,ra,dec,self.app.site.latitude,self.app.site.longitude)
                self.table.setItem(r,self.AZ_COL,self.readonly_item(f'{pos.azimuth:0.2f}')); self.table.setItem(r,self.EL_COL,self.readonly_item(f'{pos.elevation:0.2f}'))
            except Exception:
                self.table.setItem(r,self.AZ_COL,self.readonly_item('--')); self.table.setItem(r,self.EL_COL,self.readonly_item('--'))
    def accept(self):
        try:
            sources=self.read_sources_from_table()
            if not sources: raise ValueError('At least one source is required')
            selected=self.selected.currentText().strip(); selected=selected if selected in sources else next(iter(sources))
            self.app.sources=sources; self.app.selected_source_name=selected; self.app.site.selected_source=selected; save_sources(self.app.config_path,sources,selected); self.app.set_status('Sources saved.'); super().accept()
        except Exception as exc: self.fail(exc)
class CalibrationDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Calibration'); self.tabs=QTabWidget(); self.main.addWidget(self.tabs); self.fields={}
        for name,cfg in app.configs.items():
            page=QWidget(); g=QGridLayout(page); p=app.positions.get(name); raw_az='--' if not p else f'{p.raw_azimuth:0.2f}'; raw_el='--' if not p else f'{p.raw_elevation:0.2f}'
            az=self.edit(cfg.calibration.az_offset); el=self.edit(cfg.calibration.el_offset); self.fields[name]=(az,el)
            rows=[('Raw AZ',lbl(raw_az)),('Raw EL',lbl(raw_el)),('AZ offset',az),('EL offset',el)]
            for r,(label,w) in enumerate(rows): g.addWidget(lbl(label),r,0); g.addWidget(w,r,1)
            self.tabs.addTab(page,name)
        self.status=lbl(''); self.main.addWidget(self.status)
        row=QHBoxLayout(); manual=btn('Calibrate Manual'); target=btn('Calibrate From Target'); apply=btn('Apply Offsets'); close=btn('Close')
        manual.clicked.connect(lambda:self.set_status('Edit offsets directly, then press Apply Offsets.'))
        target.clicked.connect(self.calibrate_from_target); apply.clicked.connect(self.apply_offsets); close.clicked.connect(self.reject)
        for b in [manual,target,apply]: row.addWidget(b)
        row.addStretch(1); row.addWidget(close); self.main.addLayout(row)
    def current_name(self): return self.tabs.tabText(self.tabs.currentIndex())
    def set_status(self,text): self.status.setText(text)
    def calibrate_from_target(self):
        name=self.current_name(); pos=self.app.positions.get(name); target=self.app.current_target
        if not pos or not target: self.set_status('Current antenna position and target are required.'); return
        az_off=(target.azimuth-pos.raw_azimuth+540.0)%360.0-180.0; el_off=target.elevation-pos.raw_elevation
        az,el=self.fields[name]; az.setText(f'{az_off:0.6f}'); el.setText(f'{el_off:0.6f}'); self.apply_offsets()
    def apply_offsets(self):
        try:
            for name,(az,el) in self.fields.items(): self.app.configs[name].calibration.az_offset=self.to_float(az,'AZ offset'); self.app.configs[name].calibration.el_offset=self.to_float(el,'EL offset')
            save_configs(self.app.config_path,self.app.configs); self.app.set_status('Calibration offsets saved.'); self.set_status('Calibration offsets saved.')
        except Exception as exc: self.fail(exc)
class PowerSettingsDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Power'); g=QGridLayout(); self.main.addLayout(g); p=app.power_config; self.fields={}
        specs=[('center_frequency_hz','Freq MHz',p.center_frequency_hz/1_000_000),('sample_rate_hz','Rate ksps',p.sample_rate_hz/1000),('measurement_bandwidth_hz','BW kHz',p.measurement_bandwidth_hz/1000),('gain_db','Gain A',p.gain_db),('gain_b_db','Gain B',p.gain_b_db),('update_rate_hz','GUI Hz',p.update_rate_hz),('smoothing_samples','Avg',p.smoothing_samples),('clock_source','Clock',p.clock_source)]
        for r,(key,label,value) in enumerate(specs): self.fields[key]=self.edit(value); g.addWidget(lbl(label),r,0); g.addWidget(self.fields[key],r,1)
        self.buttons()
    def accept(self):
        try:
            p=self.app.power_config; p.center_frequency_hz=int(self.to_float(self.fields['center_frequency_hz'],'Freq MHz')*1_000_000); p.sample_rate_hz=int(self.to_float(self.fields['sample_rate_hz'],'Rate ksps')*1000); p.measurement_bandwidth_hz=int(self.to_float(self.fields['measurement_bandwidth_hz'],'BW kHz')*1000); p.gain_db=self.fields['gain_db'].text(); p.gain_b_db=self.fields['gain_b_db'].text(); p.update_rate_hz=self.to_float(self.fields['update_rate_hz'],'GUI Hz'); p.smoothing_samples=self.to_int(self.fields['smoothing_samples'],'Avg'); p.clock_source=self.fields['clock_source'].text().strip() or 'internal'
            save_power_config(self.app.config_path,p); self.app.power_config=p; self.app.power.freq.setText(f'{p.center_frequency_hz/1_000_000:0.1f}'); self.app.power.rate.setText(f'{p.sample_rate_hz/1000:0.0f}'); self.app.power.bw.setText(f'{p.measurement_bandwidth_hz/1000:0.0f}'); self.app.power.gain_a.setText(str(p.gain_db)); self.app.power.gain_b.setText(str(p.gain_b_db)); self.app.power.avg.setText(str(p.smoothing_samples)); self.app.power.gui_hz.setText(f'{p.update_rate_hz:0.0f}'); self.app.power.clock.setText(p.clock_source); self.app.set_status('Power settings saved; restart SDR power to apply.'); super().accept()
        except Exception as exc: self.fail(exc)

class ScanSettingsDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Scan Cal'); g=QGridLayout(); self.main.addLayout(g); s=load_scan_config(app.config_path); self.fields={}; self.antenna=QComboBox(); self.antenna.addItems(list(app.configs.keys())); self.antenna.setCurrentText(s.antenna_name); self.high_to_low=QCheckBox('AZ scan high to low'); self.high_to_low.setChecked(s.az_scan_high_to_low)
        specs=[('span_degrees','Span +/- deg',s.span_degrees),('increment_degrees','Increment deg',s.increment_degrees),('dwell_seconds','Dwell sec',s.dwell_seconds),('scan_count','Scans',s.scan_count)]
        g.addWidget(lbl('Antenna'),0,0); g.addWidget(self.antenna,0,1)
        for r,(key,label,value) in enumerate(specs,1): self.fields[key]=self.edit(value); g.addWidget(lbl(label),r,0); g.addWidget(self.fields[key],r,1)
        g.addWidget(self.high_to_low,5,0,1,2); self.status=lbl('Track a source and start B210 power before scanning.','faultTag'); self.main.addWidget(self.status)
        row=QHBoxLayout(); az=btn('AZ Scan'); el=btn('EL Scan'); stop=btn('Stop Scan'); close=btn('Close')
        az.clicked.connect(lambda:self.start_scan(Axis.AZIMUTH)); el.clicked.connect(lambda:self.start_scan(Axis.ELEVATION)); stop.clicked.connect(self.stop_scan); close.clicked.connect(self.reject)
        for b in [az,el,stop]: row.addWidget(b)
        row.addStretch(1); row.addWidget(close); self.main.addLayout(row)
    def scan_config(self):
        return ScanConfig(self.to_float(self.fields['span_degrees'],'Span'),self.to_float(self.fields['increment_degrees'],'Increment'),self.to_float(self.fields['dwell_seconds'],'Dwell'),self.to_int(self.fields['scan_count'],'Scans'),self.antenna.currentText(),self.high_to_low.isChecked())
    def start_scan(self,axis):
        try:
            cfg=self.scan_config(); save_scan_config(self.app.config_path,cfg)
            starter=getattr(self.app,'start_calibration_scan',None)
            if callable(starter): starter(axis,cfg,self)
            else: self.set_status('Internal error: scan worker is unavailable.')
        except Exception as exc: self.set_status(str(exc))
    def stop_scan(self):
        stopper=getattr(self.app,'stop_scan',None)
        if callable(stopper): stopper()
        else: self.set_status('No PyQt scan worker is running.')
    def set_status(self,text): self.status.setText(text)
class YFactorSettingsDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Y Factor'); g=QGridLayout(); self.main.addLayout(g); y=load_yfactor_config(app.config_path)
        self.hot=QComboBox(); self.hot.addItems(['Sun','Moon','Source']); self.hot.setCurrentText(y.hot_target)
        self.antenna=QComboBox(); self.antenna.addItems(list(app.configs.keys())); self.antenna.setCurrentText(y.antenna_name)
        self.cold=QComboBox(); self.cold.addItems(['Sun AZ / EL 80','Moon AZ / EL 80','AZ/EL','RA/Dec']); self.cold.setCurrentText(y.cold_mode)
        self.workflow=QComboBox(); self.workflow.addItems(['Alternate H/C','Repeat H/C']); self.workflow.setCurrentText('Alternate H/C' if y.alternate_order else 'Repeat H/C')
        self.fields={}; specs=[('cold_az','Cold AZ',y.cold_az),('cold_el','Cold EL',y.cold_el),('cold_ra','Cold RA h',y.cold_ra),('cold_dec','Cold Dec',y.cold_dec),('count','Measurements',y.count),('dwell_seconds','Dwell sec',y.dwell_seconds)]
        for r,(label,w) in enumerate([('Hot target',self.hot),('Antenna',self.antenna),('Cold sky',self.cold)]): g.addWidget(lbl(label),r,0); g.addWidget(w,r,1)
        for r,(key,label,value) in enumerate(specs,3): self.fields[key]=self.edit(value); g.addWidget(lbl(label),r,0); g.addWidget(self.fields[key],r,1)
        g.addWidget(lbl('Workflow'),9,0); g.addWidget(self.workflow,9,1); self.status=lbl('Start B210 power before measuring.','faultTag'); self.main.addWidget(self.status)
        row=QHBoxLayout(); start=btn('Start'); stop=btn('Stop'); close=btn('Close'); start.clicked.connect(self.start_measurement); stop.clicked.connect(self.stop_measurement); close.clicked.connect(self.reject)
        row.addWidget(start); row.addWidget(stop); row.addStretch(1); row.addWidget(close); self.main.addLayout(row)
        self.hot.currentTextChanged.connect(self.on_hot_target_changed); self.on_hot_target_changed()
    def on_hot_target_changed(self):
        if self.cold.currentText() not in ['Sun AZ / EL 80','Moon AZ / EL 80']: return
        if self.hot.currentText() == 'Sun': self.cold.setCurrentText('Sun AZ / EL 80')
        elif self.hot.currentText() == 'Moon': self.cold.setCurrentText('Moon AZ / EL 80')
    def yfactor_config(self):
        return YFactorConfig(self.antenna.currentText(),self.hot.currentText(),self.cold.currentText(),self.to_float(self.fields['cold_az'],'Cold AZ'),self.to_float(self.fields['cold_el'],'Cold EL'),self.to_float(self.fields['cold_ra'],'Cold RA'),self.to_float(self.fields['cold_dec'],'Cold Dec'),self.to_int(self.fields['count'],'Measurements'),self.to_float(self.fields['dwell_seconds'],'Dwell'),self.workflow.currentText()=='Alternate H/C')
    def start_measurement(self):
        try:
            cfg=self.yfactor_config(); save_yfactor_config(self.app.config_path,cfg)
            starter=getattr(self.app,'start_yfactor',None)
            if callable(starter): starter(self,cfg.antenna_name,cfg.hot_target,cfg.cold_mode,cfg.cold_az,cfg.cold_el,cfg.cold_ra,cfg.cold_dec,cfg.count,cfg.dwell_seconds,cfg.alternate_order)
            else: self.set_status('Internal error: Y Factor worker is unavailable.')
        except Exception as exc: self.set_status(str(exc))
    def stop_measurement(self):
        stopper=getattr(self.app,'stop_yfactor',None)
        if callable(stopper): stopper()
        else: self.set_status('No PyQt Y Factor worker is running.')
    def set_status(self,text): self.status.setText(text)
class PeakCalibrationDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Peak Calibration'); g=QGridLayout(); self.main.addLayout(g)
        self.source=QComboBox(); self.source.addItems(['Sun','Moon','Source']); current=(app.current_target.name if app.current_target else 'Source'); self.source.setCurrentText(current if current in ['Sun','Moon'] else 'Source')
        self.antenna=QComboBox(); self.antenna.addItems(list(app.configs.keys()))
        g.addWidget(lbl('Source'),0,0); g.addWidget(self.source,0,1); g.addWidget(lbl('Antenna'),1,0); g.addWidget(self.antenna,1,1)
        self.target_label=lbl('Target --'); self.antenna_label=lbl('Antenna --'); self.raw_label=lbl('Raw --'); self.offset_label=lbl('Offsets --')
        for r,w in enumerate([self.target_label,self.antenna_label,self.raw_label,self.offset_label],2): g.addWidget(w,r,0,1,2)
        self.status=lbl('Select source and antenna, then choose tracking, jog, or lock calibration.'); self.main.addWidget(self.status)
        axis=Panel(); ag=QGridLayout(axis); ag.addWidget(lbl('Axis Tracking'),0,0,1,3); az=btn('Track AZ Only'); el=btn('Track EL Only'); stop=btn('Stop Tracking'); az.clicked.connect(lambda:self.app.start_peak_axis_tracking(self,Axis.AZIMUTH,self.source.currentText(),self.antenna.currentText())); el.clicked.connect(lambda:self.app.start_peak_axis_tracking(self,Axis.ELEVATION,self.source.currentText(),self.antenna.currentText())); stop.clicked.connect(lambda:(self.app.stop_peak_tracking(), self.set_status('Peak tracking stopped.')))
        ag.addWidget(az,1,0); ag.addWidget(el,1,1); ag.addWidget(stop,1,2); self.main.addWidget(axis)
        jog=Panel(); jg=QGridLayout(jog); jg.addWidget(lbl('Manual Peak Jog'),0,0,1,3)
        dirs={'EL+':Direction.EL_UP.value,'AZ-':Direction.AZ_CCW.value,'AZ+':Direction.AZ_CW.value,'EL-':Direction.EL_DOWN.value}
        for text,row,col in [('EL+',1,1),('AZ-',2,0),('STOP',2,1),('AZ+',2,2),('EL-',3,1)]:
            b=btn(text)
            if text == 'STOP': b.clicked.connect(lambda:self.app.stop_antenna(self.antenna.currentText()))
            else:
                b.pressed.connect(lambda t=text:self.app.start_jog(self.antenna.currentText(),dirs[t])); b.released.connect(lambda:self.app.stop_jog(self.antenna.currentText()))
            jg.addWidget(b,row,col)
        self.main.addWidget(jog)
        locks=Panel(); lg=QGridLayout(locks); lg.addWidget(lbl('Calibration Lock'),0,0,1,2); laz=btn('LOCK AZ CAL'); lel=btn('LOCK EL CAL'); laz.clicked.connect(lambda:self.app.lock_peak_axis(self,Axis.AZIMUTH,self.source.currentText(),self.antenna.currentText())); lel.clicked.connect(lambda:self.app.lock_peak_axis(self,Axis.ELEVATION,self.source.currentText(),self.antenna.currentText())); lg.addWidget(laz,1,0); lg.addWidget(lel,1,1); self.main.addWidget(locks)
        row=QHBoxLayout(); row.addStretch(1); close=btn('Close'); close.clicked.connect(self.reject); row.addWidget(close); self.main.addLayout(row); self.source.currentTextChanged.connect(lambda _text:self.refresh_labels()); self.antenna.currentTextChanged.connect(lambda _text:self.refresh_labels()); self.timer=QTimer(self); self.timer.timeout.connect(self.refresh_labels); self.timer.start(1000); self.refresh_labels()
    def refresh_labels(self):
        try: target=self.app.yfactor_hot_target(self.source.currentText())
        except Exception: target=None
        name=self.antenna.currentText(); pos=self.app.positions.get(name); cfg=self.app.configs.get(name)
        self.target_label.setText('Target --' if not target else f'{target.name} AZ {target.azimuth:0.2f} EL {target.elevation:0.2f}')
        self.antenna_label.setText('Antenna --' if not pos else f'Antenna AZ {pos.azimuth:0.2f} EL {pos.elevation:0.2f}')
        self.raw_label.setText('Raw --' if not pos else f'Raw AZ {pos.raw_azimuth:0.2f} EL {pos.raw_elevation:0.2f}')
        self.offset_label.setText('Offsets --' if not cfg else f'Offsets AZ {cfg.calibration.az_offset:+0.2f} EL {cfg.calibration.el_offset:+0.2f}')
    def set_status(self,text): self.status.setText(text)
class EncodersDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Encoders'); self.table=QTableWidget(0,8); self.table.setHorizontalHeaderLabels(['Antenna','Axis','Type','Model','Version','Serial','Resolution','Position']); self.main.addWidget(self.table); scan=btn('Scan'); scan.clicked.connect(self.scan); self.main.addWidget(scan); close=QDialogButtonBox(QDialogButtonBox.Close); close.rejected.connect(self.reject); self.main.addWidget(close); self.resize(720,320)
    def scan(self):
        self.table.setRowCount(0)
        if not self.app.sessions: QMessageBox.warning(self,'Encoders','Connect antennas before encoder scan.'); return
        for name,session in self.app.sessions.items():
            try:
                data=session.scan_encoders()
                for axis,info in data.items():
                    r=self.table.rowCount(); self.table.insertRow(r); vals=[name,axis.value,info.encoder_type,info.model,info.version,info.serial,info.resolution,info.position]
                    for c,v in enumerate(vals): self.table.setItem(r,c,QTableWidgetItem(str(v)))
            except Exception as exc: QMessageBox.warning(self,'Encoders',f'{name}: {exc}')
PowerMeterPanel = B210Panel

class WT7App(QWidget):
    def __init__(self,config_path):
        super().__init__(); self.config_path=Path(config_path); self.configs=load_configs(self.config_path); self.site=load_site_config(self.config_path); self.power_config=load_power_config(self.config_path); self.sources=load_sources(self.config_path); self.selected_source_name=self.site.selected_source if self.site.selected_source in self.sources else next(iter(self.sources), '')
        self.event_log=EventLogger(Path('logs'),self.site.log_retention_days,self.site.log_level); self.state_store=AppStateStore(); self.sessions={}; self.positions={}; self.cards={}; self.current_target=None; self.tracking_kind=''; self.tracking_stop=threading.Event(); self.jog_stops={}; self.b210_stop=threading.Event(); self.b210_thread=None; self.events=queue.Queue(); self.log_handle=None; self.log_writer=None; self.scan_stop=threading.Event(); self.scan_thread=None; self.scan_antenna_name=''; self.scan_axis=None; self.scan_offset_degrees=0.0; self.scan_offset_lock=threading.Lock(); self.scan_result_dialogs=[]; self.yfactor_stop=threading.Event(); self.yfactor_thread=None; self.peak_stop=threading.Event(); self.peak_thread=None
        self.setWindowTitle(f'WT7 ANTENNA CONTROLLER {APP_VERSION}'); self.resize(1240,760); self.setMinimumSize(1120,680); self.build_ui(); self.style_ui(); self.set_status('Load config, connect antennas, then use guarded jogs.'); self.event_log.info('APP_START',version=APP_VERSION,config=str(config_path))
        self.t_ref=QTimer(self); self.t_ref.timeout.connect(self.update_reference); self.t_ref.start(1000); self.t_evt=QTimer(self); self.t_evt.timeout.connect(self.process_events); self.t_evt.start(100); self.t_pos=QTimer(self); self.t_pos.timeout.connect(self.poll_positions); self.t_pos.start(1000)
    def build_ui(self):
        main=QVBoxLayout(self); main.setContentsMargins(18,10,18,10); main.setSpacing(8)
        top1=QHBoxLayout(); top1.setSpacing(7)
        row1=[('Connect','primary',self.connect_all),('Disconnect','',self.disconnect_all),('Limits','',self.open_limits),('Observer','',self.open_observer),('Tracking','',self.open_tracking_dialog),('Sources','',self.open_sources_dialog),('Calibration','',self.open_calibration_dialog),('Peak Cal','',self.open_peak_calibration),('Scan Cal','',self.open_scan_dialog),('Y Factor','',self.open_yfactor_dialog),('Encoders','',self.open_encoders_dialog),('STOP ALL','danger',self.stop_all)]
        for text,name,cb in row1:
            b=btn(text,name); b.clicked.connect(cb); top1.addWidget(b)
        top1.addStretch(1); main.addLayout(top1)
        top2=QHBoxLayout(); top2.setSpacing(7)
        row2=[('Track Sun','',lambda:self.start_tracking('sun')),('Track Moon','',lambda:self.start_tracking('moon')),('Track Source','',lambda:self.start_tracking('source')),('Stop Track','',self.stop_tracking),('Park','',self.park_all)]
        for text,name,cb in row2:
            b=btn(text,name); b.clicked.connect(cb); top2.addWidget(b)
        top2.addStretch(1); main.addLayout(top2)
        header=QHBoxLayout(); header.setSpacing(8)
        src=Panel(); sr=QHBoxLayout(src); sr.setContentsMargins(8,8,8,8); sr.addWidget(bold('SOURCE')); self.source_name=lbl('Target --'); sr.addWidget(self.source_name); sr.addStretch(1); sr.addWidget(lbl('AZ','muted')); self.source_az=bold('--',14); sr.addWidget(self.source_az); sr.addStretch(1); sr.addWidget(lbl('EL','muted')); self.source_el=bold('--',14); sr.addWidget(self.source_el); sr.addStretch(1); sr.addWidget(lbl('HA','muted')); self.source_ha=bold('--',14); sr.addWidget(self.source_ha)
        ref=Panel(); rg=QGridLayout(ref); rg.setContentsMargins(8,7,8,7); rg.setHorizontalSpacing(18); self.lmst=lbl('LMST --'); self.utc=lbl('UTC --'); self.local=lbl('Local --'); self.sun=bold('SUN AZ -- EL --'); self.moon=bold('MOON AZ -- EL --'); rg.addWidget(self.lmst,0,0); rg.addWidget(self.utc,0,1); rg.addWidget(self.local,0,2); rg.addWidget(self.sun,1,0); rg.addWidget(self.moon,1,1)
        header.addWidget(src,2); header.addWidget(ref,3); main.addLayout(header)
        self.status=lbl(''); main.addWidget(self.status)
        for name in self.configs:
            card=AntennaCard(name); card.jog_pressed.connect(self.start_jog); card.jog_released.connect(self.stop_jog); card.stop_clicked.connect(self.stop_antenna); self.cards[name]=card; main.addWidget(card)
        self.power=B210Panel(self.power_config); self.power.app=self; self.power.start_clicked.connect(self.start_b210); self.power.stop_clicked.connect(self.stop_b210); self.power.cal_clicked.connect(lambda:self.info('B210 calibration capture remains in wt7_tk_legacy_gui.py during the PyQt transition.')); self.power.log_start_clicked.connect(self.start_b210_log); self.power.log_stop_clicked.connect(self.stop_b210_log); main.addWidget(self.power)
        ev=Panel(); ev.setMinimumHeight(82); eg=QGridLayout(ev); eg.setContentsMargins(9,8,9,8); eg.addWidget(bold('RECENT EVENTS'),0,0); ob=btn('Open Log'); ob.clicked.connect(self.open_log_hint); eg.addWidget(ob,0,3,Qt.AlignRight); self.ev1=lbl('--'); self.ev2=lbl('--','muted'); eg.addWidget(self.ev1,1,0,1,4); eg.addWidget(self.ev2,2,0,1,4); main.addWidget(ev); main.addStretch(1)
    def style_ui(self):
        self.setStyleSheet("""
            QWidget{background:#f6f6f5;color:#1f252b;font-family:Arial,Helvetica,sans-serif;font-size:10pt}
            QLabel{background:transparent;border:none;color:#1f252b}
            QLabel#muted{color:#8a8f95}
            QLabel#bold{font-weight:700}
            QFrame#panel{background:#f1f1f0;border:1px solid #d7d7d5}
            QPushButton{background:#fff;color:#111820;border:1px solid #cfd3d6;border-radius:8px;padding:4px 10px;min-height:20px;min-width:62px}
            QPushButton#primary{background:#121820;color:white;border-color:#121820}
            QPushButton#danger{color:#e74b2c;border-color:#e74b2c}
            QLineEdit{background:white;color:#111820;border:1px solid #d8dadd;border-radius:8px;padding:3px 7px}
            QLabel#stateGood{background:#ead7c9;border:1px solid #dcc2ae;padding:5px 8px}
            QLabel#stateBusy{background:#d9e7f8;border:1px solid #bed3ed;padding:5px 8px}
            QLabel#stateStopped{background:#eeeeed;border:1px solid #d2d2d2;padding:5px 8px}
            QLabel#stateFault,QLabel#faultTag{background:#ffd9d9;color:#b00000;border:1px solid #e3a2a2;padding:5px 8px}
            QLabel#safe{background:#ead7c9;border:1px solid #dcc2ae;padding:5px 8px}
        """)
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
    def open_limits(self): LimitsDialog(self).exec_()
    def open_observer(self): ObserverDialog(self).exec_()
    def open_tracking_dialog(self): TrackingDialog(self).exec_()
    def open_sources_dialog(self): SourcesDialog(self).exec_()
    def open_calibration_dialog(self): CalibrationDialog(self).exec_()
    def open_scan_dialog(self): ScanSettingsDialog(self).exec_()
    def open_yfactor_dialog(self): YFactorSettingsDialog(self).exec_()
    def open_encoders_dialog(self): EncodersDialog(self).exec_()
    def open_power_dialog(self): PowerSettingsDialog(self).exec_()
    def open_peak_calibration(self): PeakCalibrationDialog(self).exec_()
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
    def stop_tracking(self):
        self.tracking_stop.set()
        for n in self.sessions:
            if n in self.cards: self.cards[n].set_state('STOPPED')
        self.set_status('Tracking stopped.')
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
        self.tracking_stop.set(); self.tracking_kind=''; stop=threading.Event(); sessions=list(self.sessions.items()); self.set_status('Parking antennas.')
        for n,_s in sessions: self.cards[n].set_state('PARKING')
        def park_one(n,s):
            try:
                s.config.limits.assert_position_allowed(s.config.park_az,s.config.park_el)
                s.guarded_slew_to(s.config.park_az,s.config.park_el,s.config.az_track_speed,s.config.el_track_speed,stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p,n=n:self.emit(lambda data:self.update_position(*data),(n,p)))
                if not stop.is_set(): self.emit(lambda name:self.cards[name].set_state('PARKED'),n)
            except Exception as e: self.emit(lambda data:self.mark_fault(*data),(n,str(e)))
        def worker():
            threads=[]
            for n,s in sessions:
                t=threading.Thread(target=lambda n=n,s=s: park_one(n,s),name=f'Park{n}',daemon=True); threads.append(t); t.start()
            for t in threads: t.join()
            self.emit(lambda m:self.set_status(m),'Park complete.')
        self.run_thread(worker,'Park')
    def slew_all_to_target(self,target,activity,stop):
        threads=[]
        for n,s in list(self.sessions.items()):
            def worker(n=n,s=s):
                try:
                    effective_target=self.apply_scan_offset(target,n)
                    self.emit(lambda name:self.cards[name].set_state('SLEWING'),n); s.config.limits.assert_position_allowed(effective_target.azimuth,effective_target.elevation)
                    s.guarded_slew_to(effective_target.azimuth,effective_target.elevation,s.config.az_track_speed,s.config.el_track_speed,stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p,n=n:self.emit(lambda data:self.update_position(*data),(n,p)))
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
    def validate_scan_config(self,cfg):
        if cfg.antenna_name not in self.configs: raise RuntimeError('Select East or West antenna for the scan.')
        if cfg.antenna_name not in self.sessions: raise RuntimeError(f'{cfg.antenna_name} must be connected before scanning.')
        if not (0.1 <= cfg.span_degrees <= 30.0): raise RuntimeError('Scan span must be 0.1..30.0 degrees.')
        if not (0.01 <= cfg.increment_degrees <= cfg.span_degrees): raise RuntimeError('Scan increment must be 0.01 degrees up to the scan span.')
        if not (0.1 <= cfg.dwell_seconds <= 60.0): raise RuntimeError('Dwell must be 0.1..60.0 seconds.')
        if not (1 <= cfg.scan_count <= 20): raise RuntimeError('Scan count must be 1..20.')
    def scan_offsets(self,axis,cfg):
        vals=[]; x=cfg.span_degrees
        while x >= -cfg.span_degrees - cfg.increment_degrees*0.5:
            vals.append(round(max(x,-cfg.span_degrees),6)); x-=cfg.increment_degrees
        if axis == Axis.AZIMUTH and not cfg.az_scan_high_to_low: vals.reverse()
        return vals
    def offset_target(self,target,axis,offset):
        az=(target.azimuth+offset)%360.0 if axis == Axis.AZIMUTH else target.azimuth
        el=max(0.0,min(90.0,target.elevation+offset)) if axis == Axis.ELEVATION else target.elevation
        return TargetPosition(target.name,az,el)
    def set_scan_offset(self,antenna_name=None,axis=None,offset=0.0):
        with self.scan_offset_lock:
            self.scan_antenna_name=antenna_name or ''; self.scan_axis=axis; self.scan_offset_degrees=float(offset) if antenna_name and axis else 0.0
    def apply_scan_offset(self,target,antenna_name):
        with self.scan_offset_lock:
            scan_name=self.scan_antenna_name; axis=self.scan_axis; offset=self.scan_offset_degrees
        if antenna_name != scan_name or axis is None or offset == 0.0: return target
        return self.offset_target(target,axis,offset)
    def average_scan_rows(self,rows,offsets):
        averaged=[]
        for offset in offsets:
            matching=[row for row in rows if row.get('power_value') is not None and float(row.get('offset_degrees',0.0)) == float(offset)]
            if not matching: continue
            row=dict(matching[-1]); row['power_value']=sum(float(r['power_value']) for r in matching)/len(matching); row['power_dbfs']=sum(float(r['power_dbfs']) for r in matching)/len(matching); row['sample_count']=sum(int(r.get('sample_count',0)) for r in matching); row['scan_number']='avg'; averaged.append(row)
        return averaged
    def start_calibration_scan(self,axis,cfg,dialog):
        if self.scan_thread and self.scan_thread.is_alive(): dialog.set_status('Scan already running.'); return
        if not self.tracking_kind: dialog.set_status('Start tracking Sun, Moon, or Source before scanning.'); return
        try:
            self.validate_scan_config(cfg); meas=self.power.current_power_measurement(cfg.antenna_name)
            if meas is None: dialog.set_status(f"Start B210 power and wait for CH {self.power.power_channel_for_antenna(cfg.antenna_name)} readings before scanning."); return
            save_scan_config(self.config_path,cfg); self.scan_stop.clear(); dialog.set_status(f'{axis.value} scan starting on {cfg.antenna_name}.'); self.set_status(f'{axis.value} scan starting on {cfg.antenna_name}.')
            self.scan_thread=threading.Thread(target=lambda:self.scan_worker(axis,cfg,dialog),daemon=True); self.scan_thread.start()
        except Exception as exc: dialog.set_status(str(exc))
    def stop_scan(self):
        self.scan_stop.set(); self.set_status('Scan stop requested.')
    def scan_worker(self,axis,cfg,dialog):
        rows=[]; offsets=self.scan_offsets(axis,cfg); total_points=max(1,len(offsets)*cfg.scan_count); point_no=0; scan_dir=Path(self.config_path).parent/'scan'; scan_dir.mkdir(exist_ok=True); csv_path=scan_dir/f"wt7_scan_{cfg.antenna_name.lower()}_{axis.value}_{datetime.now():%Y%m%d-%H%M%S}.csv"
        try:
            self.set_scan_offset(cfg.antenna_name,axis,offsets[0] if offsets else 0.0)
            for scan_no in range(1,cfg.scan_count+1):
                for offset in offsets:
                    if self.scan_stop.is_set(): break
                    point_no+=1
                    nominal=self.current_tracking_target(self.tracking_kind); target=self.offset_target(nominal,axis,offset); self.set_scan_offset(cfg.antenna_name,axis,offset); self.emit(lambda t:self.apply_target(t),nominal); self.emit(lambda s:dialog.set_status(s),f'{cfg.antenna_name} {axis.value} scan {scan_no}/{cfg.scan_count} point {point_no}/{total_points} offset {offset:+0.2f}')
                    self.slew_all_to_target(nominal,'SCAN',self.scan_stop)
                    if self.scan_stop.is_set(): break
                    rows.append(self.collect_power_point(axis,offset,cfg.dwell_seconds,nominal,target,cfg.antenna_name,scan_no))
                if self.scan_stop.is_set(): break
            averaged=self.average_scan_rows(rows,offsets)
            if rows and not self.scan_stop.is_set(): self.write_scan_csv(csv_path,rows,averaged); self.emit(lambda data:self.show_scan_result(*data),(axis,cfg.antenna_name,csv_path,averaged or rows))
            msg='Scan stopped.' if self.scan_stop.is_set() else f'Scan complete: {csv_path.name}'
            self.emit(lambda m:dialog.set_status(m),msg); self.emit(lambda m:self.set_status(m),msg)
        except Exception as exc:
            self.emit(lambda m:dialog.set_status(m),str(exc)); self.emit(lambda m:self.set_status(f'Scan fault: {m}'),str(exc))
        finally:
            self.set_scan_offset(None); self.scan_stop.clear()
            if self.tracking_kind:
                try: self.slew_all_to_target(self.current_tracking_target(self.tracking_kind),'TRACKING',self.tracking_stop)
                except Exception: pass
    def collect_power_point(self,axis,offset,dwell,nominal,target,antenna,scan_no):
        vals=[]; end=time.monotonic()+dwell
        while time.monotonic()<end and not self.scan_stop.is_set():
            m=self.power.current_power_measurement(antenna)
            if m: vals.append(m)
            time.sleep(0.1)
        if not vals: raise RuntimeError('No B210 power measurements were available.')
        pos=self.positions.get(antenna); avg_val=sum(float(v['power_value']) for v in vals)/len(vals); avg_dbfs=sum(float(v['power_dbfs']) for v in vals)/len(vals)
        return {'local_time':datetime.now().astimezone().isoformat(timespec='seconds'),'antenna':antenna,'axis':axis.value,'scan_number':scan_no,'offset_degrees':offset,'nominal_az':nominal.azimuth,'nominal_el':nominal.elevation,'target_az':target.azimuth,'target_el':target.elevation,'power_value':avg_val,'power_dbfs':avg_dbfs,'power_unit':vals[-1]['power_unit'],'power_channel':vals[-1]['power_channel'],'sample_count':len(vals),'antenna_az':None if not pos else pos.azimuth,'antenna_el':None if not pos else pos.elevation,'raw_az':None if not pos else pos.raw_azimuth,'raw_el':None if not pos else pos.raw_elevation}
    def write_scan_csv(self,path,rows,averaged=None):
        with path.open('w',newline='',encoding='utf-8') as h:
            fieldnames=list(rows[0]); w=csv.DictWriter(h,fieldnames=fieldnames); w.writeheader(); w.writerows(rows)
            if averaged:
                w.writerow({key:'' for key in fieldnames}); w.writerows(averaged)
    def show_scan_result(self,axis,antenna,path,rows):
        d=QDialog(); d.setWindowTitle(f'{antenna} {axis.value} Scan'); d.setWindowModality(Qt.NonModal); d.setAttribute(Qt.WA_DeleteOnClose,True); v=QVBoxLayout(d); v.addWidget(lbl(f'{antenna} {axis.value} scan saved to {path.name}'))
        plot=ScanPlotWidget(axis,rows,d); v.addWidget(plot)
        points=[(float(r['offset_degrees']),float(r['power_value'])) for r in rows if r.get('power_value') is not None]
        fit=plot.fit_gaussian_with_slope(points) if points else None
        if fit:
            unit=str(rows[-1].get('power_unit','dBFS')); fwhm=2.35482*fit['sigma']; v.addWidget(lbl(f"Boresight error {fit['center']:+0.3f} deg; FWHM {fwhm:0.3f} deg; peak {fit['peak']:0.2f} {unit}; RMS {fit['rms']:0.3f} dB"))
        else:
            v.addWidget(lbl('Gaussian fit unavailable'))
        close=btn('Close'); close.clicked.connect(d.close); v.addWidget(close,alignment=Qt.AlignRight); d.resize(640,440); self.scan_result_dialogs.append(d); d.destroyed.connect(lambda _obj,d=d: self.scan_result_dialogs.remove(d) if d in self.scan_result_dialogs else None); d.show()
    def yfactor_hot_target(self,label):
        if label == 'Sun': return self.target_for_kind('sun')
        if label == 'Moon': return self.target_for_kind('moon')
        return self.current_tracking_target('source')
    def yfactor_cold_target(self,mode,hot,az,el,ra,dec):
        if mode == 'Sun AZ / EL 80': s=self.target_for_kind('sun'); return TargetPosition('Cold Sky',s.azimuth,80.0)
        if mode == 'Moon AZ / EL 80': m=self.target_for_kind('moon'); return TargetPosition('Cold Sky',m.azimuth,80.0)
        if mode in ('AZ/EL','AZ / EL'): return TargetPosition('Cold Sky',az,el)
        return source_position('Cold Sky',ra,dec,self.site.latitude,self.site.longitude)
    def start_yfactor(self,dialog,antenna,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,count,dwell,alternate):
        if self.yfactor_thread and self.yfactor_thread.is_alive(): dialog.set_status('Y Factor already running.'); return
        if antenna not in self.sessions: dialog.set_status('Connect and select an antenna before Y Factor measurement.'); return
        if self.power.current_power_measurement(antenna) is None: dialog.set_status(f"Start B210 power and wait for CH {self.power.power_channel_for_antenna(antenna)} readings before Y Factor measurement."); return
        self.yfactor_stop.clear(); dialog.set_status(f'Y Factor starting on {antenna}.'); self.set_status(f'Y Factor starting on {antenna}.')
        self.yfactor_thread=threading.Thread(target=lambda:self.yfactor_worker(dialog,antenna,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,count,dwell,alternate),daemon=True); self.yfactor_thread.start()
    def stop_yfactor(self): self.yfactor_stop.set(); self.set_status('Y Factor stop requested.')
    def yfactor_worker(self,dialog,antenna,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,count,dwell,alternate):
        rows=[]; out=Path(self.config_path).parent/'yfactor'; out.mkdir(exist_ok=True); path=out/f"wt7_yfactor_{antenna.lower()}_{datetime.now():%Y%m%d-%H%M%S}.csv"; session=self.sessions[antenna]
        try:
            for name,s in list(self.sessions.items()):
                if name!=antenna: s.stop_all(); self.emit(lambda n:self.cards[n].set_state('STOPPED'),name)
            for i in range(1,count+1):
                phases=('hot','cold') if (not alternate or i%2==1) else ('cold','hot'); results={}
                for phase in phases:
                    if self.yfactor_stop.is_set(): break
                    hot=self.yfactor_hot_target(label); target=hot if phase=='hot' else self.yfactor_cold_target(cold_mode,hot,cold_az,cold_el,cold_ra,cold_dec)
                    self.emit(lambda s:dialog.set_status(s),f'Measurement {i}/{count}: {phase}.'); self.emit(lambda t:self.apply_target(t),hot)
                    session.guarded_slew_to(target.azimuth,target.elevation,session.config.az_track_speed,session.config.el_track_speed,self.yfactor_stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p:self.emit(lambda data:self.update_position(*data),(antenna,p)),target_callback=(lambda _p, phase=phase: (self.yfactor_hot_target(label).azimuth,self.yfactor_hot_target(label).elevation) if phase=='hot' else (self.yfactor_cold_target(cold_mode,self.yfactor_hot_target(label),cold_az,cold_el,cold_ra,cold_dec).azimuth,self.yfactor_cold_target(cold_mode,self.yfactor_hot_target(label),cold_az,cold_el,cold_ra,cold_dec).elevation)))
                    results[phase]=self.collect_yfactor_power(antenna,dwell)
                if self.yfactor_stop.is_set(): break
                ydb=results['hot']['power_value']-results['cold']['power_value']; rows.append({'local_time':datetime.now().astimezone().isoformat(timespec='seconds'),'antenna':antenna,'measurement':i,'hot_power':results['hot']['power_value'],'cold_power':results['cold']['power_value'],'power_unit':results['hot']['power_unit'],'y_factor_db':ydb})
            if rows:
                with path.open('w',newline='',encoding='utf-8') as h: w=csv.DictWriter(h,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
                avg=sum(r['y_factor_db'] for r in rows)/len(rows); msg=f'Y Factor {avg:0.1f} dB, n={len(rows)}'
            else: msg='Y Factor stopped.'
            self.emit(lambda m:dialog.set_status(m),msg); self.emit(lambda m:self.set_status(m),msg)
        except Exception as exc:
            self.emit(lambda m:dialog.set_status(m),str(exc)); self.emit(lambda m:self.set_status(f'Y Factor fault: {m}'),str(exc))
        finally: self.yfactor_stop.clear()
    def collect_yfactor_power(self,antenna,dwell):
        vals=[]; end=time.monotonic()+dwell
        while time.monotonic()<end and not self.yfactor_stop.is_set():
            m=self.power.current_power_measurement(antenna)
            if m: vals.append(m)
            time.sleep(0.1)
        if not vals: raise RuntimeError('No B210 power measurements were available.')
        return {'power_value':sum(float(v['power_value']) for v in vals)/len(vals),'power_dbfs':sum(float(v['power_dbfs']) for v in vals)/len(vals),'power_unit':vals[-1]['power_unit'],'sample_count':len(vals)}
    def start_peak_axis_tracking(self,dialog,axis,label,antenna):
        if self.peak_thread and self.peak_thread.is_alive(): dialog.set_status('Peak tracking already running.'); return
        if antenna not in self.sessions: dialog.set_status('Connect and select an antenna.'); return
        self.tracking_stop.set(); self.peak_stop.clear(); self.peak_thread=threading.Thread(target=lambda:self.peak_axis_worker(dialog,axis,label,antenna),daemon=True); self.peak_thread.start(); dialog.set_status(f'Tracking {axis.value} only.')
    def stop_peak_tracking(self): self.peak_stop.set(); self.set_status('Peak tracking stop requested.')
    def peak_axis_worker(self,dialog,axis,label,antenna):
        s=self.sessions[antenna]
        try:
            while not self.peak_stop.is_set():
                target=self.yfactor_hot_target(label); pos=s.read_position(); self.emit(lambda data:self.update_position(*data),(antenna,pos))
                az=target.azimuth if axis==Axis.AZIMUTH else pos.azimuth; el=target.elevation if axis==Axis.ELEVATION else pos.elevation
                s.guarded_slew_to(az,el,s.config.az_track_speed,s.config.el_track_speed,self.peak_stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p:self.emit(lambda data:self.update_position(*data),(antenna,p)))
                time.sleep(max(0.1,self.site.track_interval_seconds))
        except Exception as exc: self.emit(lambda m:dialog.set_status(m),str(exc))
    def lock_peak_axis(self,dialog,axis,label,antenna):
        try:
            if antenna not in self.sessions: raise RuntimeError('Antenna is not connected.')
            target=self.yfactor_hot_target(label); actual=target.azimuth if axis==Axis.AZIMUTH else target.elevation; pos=self.sessions[antenna].calibrate_axis(axis,actual); save_configs(self.config_path,self.configs); self.emit(lambda data:self.update_position(*data),(antenna,pos)); dialog.refresh_labels(); dialog.set_status(f'{axis.value} calibration locked.')
        except Exception as exc: dialog.set_status(str(exc))
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
