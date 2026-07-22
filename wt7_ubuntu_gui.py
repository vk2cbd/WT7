
#!/usr/bin/env python3
"""WT7 PyQt5 antenna controller GUI."""
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
from wt7_config import B210Calibration, B210_CAL_LEVELS_DBM, PowerConfig, ScanConfig, SourceConfig, YFactorConfig, calibrated_dbm_from_dbfs, load_b210_calibration, load_configs, load_power_config, load_scan_config, load_site_config, load_sources, load_yfactor_config, save_b210_calibration, save_configs, save_power_config, save_scan_config, save_site_config, save_sources, save_yfactor_config
from wt7_logging import EventLogger
from wt7_solar import sun_equatorial, sun_position
from wt7_state import AppStateStore, SystemRunState
APP_VERSION = "v0.7"

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
def lock_width(widget, sample: str, pad: int = 10):
    width = widget.fontMetrics().horizontalAdvance(sample) + pad
    widget.setMinimumWidth(width); widget.setMaximumWidth(width)
    return widget
class Panel(QFrame):
    def __init__(self): super().__init__(); self.setObjectName('panel'); self.setFrameShape(QFrame.StyledPanel)
class AntennaCard(Panel):
    jog_pressed=pyqtSignal(str,str); jog_released=pyqtSignal(str); stop_clicked=pyqtSignal(str)
    def __init__(self,name):
        super().__init__(); self.name=name; self.setMinimumHeight(126); self.setMaximumHeight(138)
        g=QGridLayout(self); g.setContentsMargins(10,8,10,8); g.setHorizontalSpacing(10); g.setVerticalSpacing(5)
        title=bold(name.upper()); title.setMinimumWidth(72); self.state=lbl('DISCONNECTED','stateStopped'); lock_width(self.state,'DISCONNECTED',18)
        self.az=bold('--.--',18); self.el=bold('--.--',18); lock_width(self.az,'359.99',12); lock_width(self.el,'090.00',12)
        self.az_err=lbl('--.--'); self.el_err=lbl('--.--'); lock_width(self.az_err,'+999.99',10); lock_width(self.el_err,'+999.99',10)
        self.limits=lbl('SAFE','safe'); lock_width(self.limits,'FAULT',18); self.mode=lbl('--'); lock_width(self.mode,'Disconnected',10); self.target=lbl('--.-- / --.--'); lock_width(self.target,'359.99 / 090.00',12)
        g.addWidget(title,0,0,1,2); g.addWidget(self.state,0,8,1,2,Qt.AlignRight)
        g.addWidget(lbl('AZ','muted'),1,0); g.addWidget(self.az,1,1); g.addWidget(lbl('AZ err','muted'),1,3); g.addWidget(self.az_err,1,4); g.addWidget(lbl('Limits','muted'),1,5); g.addWidget(self.limits,1,6)
        g.addWidget(lbl('EL','muted'),2,0); g.addWidget(self.el,2,1); g.addWidget(lbl('EL err','muted'),2,3); g.addWidget(self.el_err,2,4); g.addWidget(lbl('Mode','muted'),2,5); g.addWidget(self.mode,2,6)
        g.addWidget(lbl('Target','muted'),3,3); g.addWidget(self.target,3,4,1,3)
        c=QWidget(); c.setObjectName('manualPad'); cg=QGridLayout(c); cg.setContentsMargins(0,0,0,0); cg.setHorizontalSpacing(6); cg.setVerticalSpacing(6)
        for text,direction,row,col in [('EL+',Direction.EL_UP.value,0,1),('AZ-',Direction.AZ_CCW.value,1,0),('AZ+',Direction.AZ_CW.value,1,2),('EL-',Direction.EL_DOWN.value,2,1)]:
            b=btn(text); b.setFixedSize(64,30); b.pressed.connect(lambda d=direction: self.jog_pressed.emit(self.name,d)); b.released.connect(lambda: self.jog_released.emit(self.name)); cg.addWidget(b,row,col)
        stop=btn('STOP'); stop.setFixedSize(64,30); stop.clicked.connect(lambda: self.stop_clicked.emit(self.name)); cg.addWidget(stop,1,1)
        g.addWidget(c,1,7,3,3,Qt.AlignLeft|Qt.AlignVCenter)
        g.setColumnMinimumWidth(1,self.az.maximumWidth()); g.setColumnMinimumWidth(2,18); g.setColumnMinimumWidth(4,self.az_err.maximumWidth()); g.setColumnMinimumWidth(6,self.limits.maximumWidth())
        g.setColumnMinimumWidth(7,6); g.setColumnStretch(10,1)
    def set_position(self,pos: Optional[Position]):
        self.az.setText('--.--' if pos is None else f'{pos.azimuth:06.2f}'); self.el.setText('--.--' if pos is None else f'{pos.elevation:05.2f}')
    def set_target(self,target: Optional[TargetPosition], pos: Optional[Position]):
        if not target: self.target.setText('--.-- / --.--'); self.az_err.setText('--.--'); self.el_err.setText('--.--'); return
        self.target.setText(f'{target.azimuth:06.2f} / {target.elevation:05.2f}')
        if pos: self.az_err.setText(f'{shortest_angle_delta(pos.azimuth,target.azimuth):+0.2f}'); self.el_err.setText(f'{target.elevation-pos.elevation:+0.2f}')
    def set_state(self,text):
        self.state.setText(text.upper()); low=text.lower(); name='stateFault' if 'fault' in low else ('stateBusy' if any(x in low for x in ['slew','park','scan','yfactor','manual','connecting']) else ('stateGood' if 'tracking' in low else 'stateStopped'))
        self.state.setObjectName(name); self.state.style().unpolish(self.state); self.state.style().polish(self.state); self.mode.setText('Auto' if text.upper() in ['TRACKING','SLEWING','PARKING'] else text.title())
    def set_limits_ok(self,ok):
        self.limits.setText('SAFE' if ok else 'FAULT'); self.limits.setObjectName('safe' if ok else 'faultTag'); self.limits.style().unpolish(self.limits); self.limits.style().polish(self.limits)
class B210Panel(Panel):
    start_clicked=pyqtSignal(); stop_clicked=pyqtSignal(); log_start_clicked=pyqtSignal(); log_stop_clicked=pyqtSignal(); cal_clicked=pyqtSignal()
    def __init__(self,power:PowerConfig):
        super().__init__(); self.power=power; self.setMinimumHeight(154); self.setMaximumHeight(174)
        main=QVBoxLayout(self); main.setContentsMargins(10,8,10,8); main.setSpacing(0)
        top=QHBoxLayout(); top.setSpacing(10)
        params_row=QHBoxLayout(); params_row.setSpacing(7)
        actions_row=QHBoxLayout(); actions_row.setSpacing(7)
        self.status=lbl('SDR RELEASED'); lock_width(self.status,'SDR POWER ON UNCAL',18); self.status.setWordWrap(True); self.status.setObjectName('safe')
        self.a_val=bold('--.-',14); self.b_val=bold('--.-',14); lock_width(self.a_val,'-999.9',12); lock_width(self.b_val,'-999.9',12); self.a_unit=bold('dBFS'); self.b_unit=bold('dBFS')
        self.gain_a=edit(power.gain_db,40); self.gain_b=edit(power.gain_b_db,40); self.freq=edit(f'{power.center_frequency_hz/1_000_000:0.1f}',72); self.rate=edit(f'{power.sample_rate_hz/1000:0.0f}',64); self.bw=edit(f'{power.measurement_bandwidth_hz/1000:0.0f}',64); self.clock=edit(power.clock_source or 'internal',78); self.avg=edit(power.smoothing_samples,40); self.gui_hz=edit(f'{power.update_rate_hz:0.0f}',40)
        def add_channel(row,name,value,unit,gain):
            row.addWidget(bold(name)); row.addWidget(value); row.addWidget(unit); row.addWidget(lbl('Gain','muted')); row.addWidget(gain); row.addWidget(lbl('dB'))
        top.addWidget(bold('B210')); add_channel(top,'CH A',self.a_val,self.a_unit,self.gain_a); add_channel(top,'CH B',self.b_val,self.b_unit,self.gain_b); top.addStretch(1); top.addWidget(self.status,0,Qt.AlignRight)
        for text,widget in [('Freq MHz',self.freq),('Rate ksps',self.rate),('BW kHz',self.bw),('Clock',self.clock),('Avg',self.avg),('GUI Hz',self.gui_hz)]:
            params_row.addWidget(lbl(text)); params_row.addWidget(widget)
        params_row.addStretch(1)
        actions=[('SDR Power On',self.start_clicked,'primary'),('Release SDR',self.stop_clicked,''),('Cal',self.cal_clicked,''),('Start Log',self.log_start_clicked,''),('Stop Log',self.log_stop_clicked,'')]
        for text,sig,name in actions:
            b=btn(text,name); b.clicked.connect(sig.emit); actions_row.addWidget(b)
        actions_row.addStretch(1)
        gap=QWidget(); gap.setFixedHeight(28); bottom_gap=QWidget(); bottom_gap.setFixedHeight(12)
        main.addLayout(top); main.addWidget(gap); main.addLayout(params_row); main.addLayout(actions_row); main.addWidget(bottom_gap)
        self.a_hist=[]; self.b_hist=[]; self.latest_raw_a_dbfs=None; self.latest_raw_b_dbfs=None; self.latest_power_dbfs=None; self.latest_power_b_dbfs=None; self.active_calibrations={}; self.power_sequence=0
    def meter_config(self):
        return B210PowerMeterConfig(center_frequency_hz=int(float(self.freq.text())*1_000_000), sample_rate_hz=int(float(self.rate.text())*1000), measurement_bandwidth_hz=int(float(self.bw.text())*1000), update_rate_hz=float(self.gui_hz.text()), gain_a_db=float(self.gain_a.text()), gain_b_db=float(self.gain_b.text()), clock_source=self.clock.text().strip() or 'internal', device_args=self.power.b210_device_args)
    def save_config(self):
        self.power.center_frequency_hz=int(float(self.freq.text())*1_000_000); self.power.sample_rate_hz=int(float(self.rate.text())*1000); self.power.measurement_bandwidth_hz=int(float(self.bw.text())*1000); self.power.update_rate_hz=float(self.gui_hz.text()); self.power.gain_db=self.gain_a.text(); self.power.gain_b_db=self.gain_b.text(); self.power.smoothing_samples=max(1,int(float(self.avg.text()))); self.power.clock_source=self.clock.text().strip() or 'internal'; return self.power
    def load_active_calibrations(self, config_path: Path, power: PowerConfig) -> dict[str, B210Calibration]:
        calibrations={}
        for channel in ('A','B'):
            cal=load_b210_calibration(config_path,power.center_frequency_hz,power.sample_rate_hz,power.measurement_bandwidth_hz,power.gain_db,power.gain_b_db,channel)
            if len(cal.points_dbfs_by_dbm) >= 2:
                calibrations[channel]=cal
        self.active_calibrations=calibrations
        return calibrations
    def set_reading(self,r:B210PowerReading):
        self.latest_raw_a_dbfs=float(r.power_a_dbfs); self.latest_raw_b_dbfs=float(r.power_b_dbfs); self.power_sequence+=1
        keep=max(1,int(float(self.avg.text() or '1'))); self.a_hist=(self.a_hist+[r.power_a_dbfs])[-keep:]; self.b_hist=(self.b_hist+[r.power_b_dbfs])[-keep:]; aa=sum(self.a_hist)/len(self.a_hist); bb=sum(self.b_hist)/len(self.b_hist)
        self.latest_power_dbfs=aa; self.latest_power_b_dbfs=bb; self.active_calibrations=getattr(self,'active_calibrations',{})
        ma=self.measurement_from_dbfs('East','A',aa,len(self.a_hist)); mb=self.measurement_from_dbfs('West','B',bb,len(self.b_hist))
        self.a_val.setText(f"{float(ma['power_value']):0.1f}"); self.b_val.setText(f"{float(mb['power_value']):0.1f}")
        self.a_unit.setText(str(ma['power_unit'])); self.b_unit.setText(str(mb['power_unit']))
        self.status.setText('SDR POWER ON CAL' if ma['power_calibrated'] or mb['power_calibrated'] else 'SDR POWER ON UNCAL')
    def clear_reading(self,status='SDR RELEASED'):
        self.a_hist=[]; self.b_hist=[]; self.latest_power_dbfs=None; self.latest_power_b_dbfs=None; self.latest_raw_a_dbfs=None; self.latest_raw_b_dbfs=None; self.power_sequence=0
        self.a_val.setText('--.-'); self.b_val.setText('--.-'); self.a_unit.setText('dBFS'); self.b_unit.setText('dBFS'); self.status.setText(status)
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
        return self.measurement_from_dbfs(antenna_name, channel, dbfs, 1)
    def current_raw_power_measurement(self, antenna_name: str, after_sequence: int = -1) -> Optional[dict[str, object]]:
        if self.power_sequence <= after_sequence: return None
        channel = self.power_channel_for_antenna(antenna_name)
        dbfs = getattr(self, 'latest_raw_b_dbfs', None) if channel == 'B' else getattr(self, 'latest_raw_a_dbfs', None)
        if dbfs is None: return None
        return self.measurement_from_dbfs(antenna_name, channel, dbfs, 1)
    def measurement_from_dbfs(self, antenna_name: str, channel: str, dbfs: float, sample_count: int) -> dict[str, object]:
        cal = getattr(self, 'active_calibrations', {}).get(channel)
        value = float(dbfs); unit = 'dBFS'; calibrated = False; extrapolated = False
        if cal:
            value, extrapolated = calibrated_dbm_from_dbfs(cal, float(dbfs))
            unit = 'dBm'; calibrated = True
        return {'power_value': value, 'power_dbfs': float(dbfs), 'power_unit': unit, 'power_channel': channel, 'power_calibrated': calibrated, 'power_extrapolated': extrapolated, 'sample_count': sample_count}
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

class B210CalibrationDialog(SimpleDialog):
    LEVELS_DBM=B210_CAL_LEVELS_DBM
    def __init__(self,app):
        super().__init__(app,'B210 Calibration')
        g=QGridLayout(); self.main.addLayout(g)
        self.freq=edit(app.power.freq.text(),74); self.rate=edit(app.power.rate.text(),62); self.bw=edit(app.power.bw.text(),62); self.gain_a=edit(app.power.gain_a.text(),48); self.gain_b=edit(app.power.gain_b.text(),48)
        self.channel=QComboBox(); self.channel.addItems(['A','B'])
        fields=[('Freq MHz',self.freq),('Rate ksps',self.rate),('BW kHz',self.bw),('Gain A',self.gain_a),('Gain B',self.gain_b),('Cal channel',self.channel)]
        for i,(label,widget) in enumerate(fields):
            g.addWidget(lbl(label),i//3,(i%3)*2); g.addWidget(widget,i//3,(i%3)*2+1)
        load_btn=btn('Load'); load_btn.clicked.connect(self.load_calibration); g.addWidget(load_btn,0,6,2,1)
        self.table=QTableWidget(len(self.LEVELS_DBM),4); self.table.setHorizontalHeaderLabels(['Source dBm','CH A dBFS','CH B dBFS','Capture'])
        for row,level in enumerate(self.LEVELS_DBM):
            self.table.setItem(row,0,QTableWidgetItem(str(level)))
            self.table.setItem(row,1,QTableWidgetItem('--'))
            self.table.setItem(row,2,QTableWidgetItem('--'))
            capture=btn('Capture Selected'); capture.clicked.connect(lambda _checked=False,l=level:self.capture_level(l)); self.table.setCellWidget(row,3,capture)
        self.table.setMinimumSize(430,300); self.main.addWidget(self.table)
        self.status=lbl('Select channel, set signal generator level, then capture each row.','faultTag'); self.main.addWidget(self.status)
        row=QHBoxLayout(); save=btn('Save Selected'); close=btn('Close'); save.clicked.connect(self.save_selected); close.clicked.connect(self.accept); row.addWidget(save); row.addStretch(1); row.addWidget(close); self.main.addLayout(row)
        self.load_calibration()
    def set_status(self,text): self.status.setText(text)
    def frequency_hz(self): return int(round(float(self.freq.text())*1_000_000))
    def sample_rate_hz(self): return int(round(float(self.rate.text())*1000))
    def bandwidth_hz(self): return int(round(float(self.bw.text())*1000))
    def selected_channel(self): return 'B' if self.channel.currentText().strip().upper() == 'B' else 'A'
    def _col(self,channel): return 2 if channel == 'B' else 1
    def load_calibration(self):
        try:
            for channel in ('A','B'):
                cal=load_b210_calibration(self.app.config_path,self.frequency_hz(),self.sample_rate_hz(),self.bandwidth_hz(),self.gain_a.text(),self.gain_b.text(),channel)
                col=self._col(channel)
                for row,level in enumerate(self.LEVELS_DBM):
                    value=cal.points_dbfs_by_dbm.get(level)
                    self.table.setItem(row,col,QTableWidgetItem(f'{value:0.2f}' if value is not None else '--'))
            self.set_status('Loaded B210 calibration records.')
        except Exception as exc:
            self.set_status(f'Calibration load fault: {exc}')
    def capture_level(self,level_dbm):
        channel=self.selected_channel()
        power=getattr(self.app.power,'latest_power_b_dbfs',None) if channel == 'B' else getattr(self.app.power,'latest_power_dbfs',None)
        if power is None:
            self.set_status(f'Start B210 power and wait for CH {channel} readings before capture.'); return
        row=list(self.LEVELS_DBM).index(level_dbm); self.table.setItem(row,self._col(channel),QTableWidgetItem(f'{power:0.2f}'))
        self.set_status(f'Captured {level_dbm:d} dBm for CH {channel}: {power:0.2f} dBFS.')
    def points_from_table(self,channel):
        points={}; col=self._col(channel)
        for row,level in enumerate(self.LEVELS_DBM):
            item=self.table.item(row,col); text=(item.text() if item else '').strip()
            if text and text != '--': points[level]=float(text)
        return points
    def save_selected(self):
        try:
            channel=self.selected_channel(); points=self.points_from_table(channel)
            if len(points) < 2:
                self.set_status(f'Capture at least two calibration points for CH {channel} before saving.'); return
            calibration=B210Calibration(self.frequency_hz(),self.sample_rate_hz(),self.bandwidth_hz(),self.gain_a.text().strip(),self.gain_b.text().strip(),channel,points)
            save_b210_calibration(self.app.config_path,calibration)
            self.app.power_config=self.app.power.save_config()
            self.app.power.load_active_calibrations(self.app.config_path,self.app.power_config)
            self.app.event_log.info('B210_CAL_SAVE',frequency_hz=calibration.frequency_hz,sample_rate_hz=calibration.sample_rate_hz,bandwidth_hz=calibration.bandwidth_hz,gain_a=calibration.gain_a_db,gain_b=calibration.gain_b_db,channel=channel,points=len(points))
            self.set_status(f'Saved B210 calibration: CH {channel} {len(points)} points.')
        except Exception as exc:
            self.set_status(f'Calibration save fault: {exc}')

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
        self.event_log=EventLogger(Path('logs'),self.site.log_retention_days,self.site.log_level); self.state_store=AppStateStore(); self.sessions={}; self.positions={}; self.cards={}; self.current_target=None; self.card_targets={}; self.tracking_kind=''; self.tracking_stop=threading.Event(); self.jog_stops={}; self.b210_stop=threading.Event(); self.b210_thread=None; self.events=queue.Queue(); self.log_handle=None; self.log_writer=None; self.scan_stop=threading.Event(); self.scan_thread=None; self.scan_antenna_name=''; self.scan_axis=None; self.scan_offset_degrees=0.0; self.scan_offset_lock=threading.Lock(); self.scan_result_dialogs=[]; self.yfactor_stop=threading.Event(); self.yfactor_thread=None; self.yfactor_hot_label=''; self.peak_stop=threading.Event(); self.peak_thread=None; self.tracking_nominal_az={}; self.modeless_dialogs=[]; self.active_scan_antenna=''; self.active_scan_dialog=None; self.scan_resume_kind=''
        self.setWindowTitle(f'WT7 ANTENNA CONTROLLER {APP_VERSION}'); self.resize(1120,780); self.setMinimumSize(1080,720); self.build_ui(); self.style_ui(); self.set_status('Load config, connect antennas, then use guarded jogs.'); self.event_log.info('APP_START',version=APP_VERSION,config=str(config_path))
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
        src=Panel(); sr=QGridLayout(src); sr.setContentsMargins(8,7,8,7); sr.setHorizontalSpacing(12); sr.setVerticalSpacing(6)
        sr.addWidget(bold('SOURCE'),0,0); self.source_name=lbl('Target --'); lock_width(self.source_name,'Omega Neb M17',12); sr.addWidget(self.source_name,0,1)
        sr.addWidget(lbl('AZ','muted'),0,2); self.source_az=bold('--.--',14); lock_width(self.source_az,'359.99',12); sr.addWidget(self.source_az,0,3)
        sr.addWidget(lbl('EL','muted'),0,4); self.source_el=bold('--.--',14); lock_width(self.source_el,'-90.00',12); sr.addWidget(self.source_el,0,5)
        sr.addWidget(lbl('HA','muted'),0,6); self.source_ha=bold('--:--',14); lock_width(self.source_ha,'-12:00',12); sr.addWidget(self.source_ha,0,7)
        self.sun=bold('SUN AZ --.-- EL --.--'); self.moon=bold('MOON AZ --.-- EL --.--'); lock_width(self.sun,'SUN AZ 359.99 EL -90.00',12); lock_width(self.moon,'MOON AZ 359.99 EL -90.00',12); sr.addWidget(self.sun,1,0,1,2); sr.addWidget(self.moon,1,2,1,5)
        ref=Panel(); ref.setMaximumWidth(260); rg=QGridLayout(ref); rg.setContentsMargins(8,7,8,7); rg.setVerticalSpacing(6); self.local=lbl('Local --'); self.utc=lbl('UTC --'); self.lmst=lbl('LMST --'); lock_width(self.local,'Local 2026-07-18 20:42:18 AEST',10); lock_width(self.utc,'UTC 2026-07-18 10:42:18',10); lock_width(self.lmst,'LMST 16:35:51',10); rg.addWidget(self.local,0,0); rg.addWidget(self.utc,1,0); rg.addWidget(self.lmst,2,0)
        header.addWidget(src,0); header.addWidget(ref,0); header.addStretch(1); main.addLayout(header)
        self.status=lbl('')
        for name in self.configs:
            card=AntennaCard(name); card.jog_pressed.connect(self.start_jog); card.jog_released.connect(self.stop_jog); card.stop_clicked.connect(self.stop_antenna); self.cards[name]=card; main.addWidget(card)
        self.power=B210Panel(self.power_config); self.power.app=self; self.power.load_active_calibrations(self.config_path,self.power_config); self.power.start_clicked.connect(self.start_b210); self.power.stop_clicked.connect(self.stop_b210); self.power.cal_clicked.connect(self.open_b210_calibration); self.power.log_start_clicked.connect(self.start_b210_log); self.power.log_stop_clicked.connect(self.stop_b210_log); main.addWidget(self.power)
        ev=Panel(); ev.setMinimumHeight(82); eg=QGridLayout(ev); eg.setContentsMargins(9,8,9,8); eg.addWidget(bold('RECENT EVENTS'),0,0); ob=btn('Open Log'); ob.clicked.connect(self.open_log_hint); eg.addWidget(ob,0,3,Qt.AlignRight); self.ev1=lbl('--'); self.ev2=lbl('--','muted'); eg.addWidget(self.ev1,1,0,1,4); eg.addWidget(self.ev2,2,0,1,4); main.addWidget(ev); main.addStretch(1)
    def style_ui(self):
        self.setStyleSheet("""
            QWidget{background:#f6f6f5;color:#1f252b;font-family:Arial,Helvetica,sans-serif;font-size:10pt}
            QLabel{background:transparent;border:none;color:#1f252b}
            QLabel#muted{color:#8a8f95}
            QLabel#bold{font-weight:700}
            QFrame#panel{background:#f1f1f0;border:1px solid #d7d7d5}
            QPushButton{background:#fff;color:#111820;border:1px solid #cfd3d6;border-radius:8px;padding:4px 10px;min-height:20px;min-width:62px}
            QPushButton:hover{background:#eef4fb;border-color:#9bbbe0}
            QPushButton:pressed{background:#cfe0f4;border:2px solid #4d87c7;padding:3px 9px}
            QPushButton#primary{background:#121820;color:white;border-color:#121820}
            QPushButton#primary:hover{background:#243244;border-color:#243244}
            QPushButton#primary:pressed{background:#36506f;border:2px solid #79aee8;padding:3px 9px}
            QPushButton#danger{color:#e74b2c;border-color:#e74b2c}
            QPushButton#danger:hover{background:#fff0ed}
            QPushButton#danger:pressed{background:#ffd7ce;border:2px solid #e74b2c;padding:3px 9px}
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
    def open_log_hint(self): self.info('Event logs are in the logs directory beside the app.')
    def open_limits(self): LimitsDialog(self).exec_()
    def open_observer(self): ObserverDialog(self).exec_()
    def open_tracking_dialog(self): TrackingDialog(self).exec_()
    def open_sources_dialog(self): SourcesDialog(self).exec_()
    def open_calibration_dialog(self): CalibrationDialog(self).exec_()
    def open_scan_dialog(self):
        d=ScanSettingsDialog(self); d.setWindowModality(Qt.NonModal); d.setAttribute(Qt.WA_DeleteOnClose,True); self.modeless_dialogs.append(d); d.destroyed.connect(lambda _obj,d=d: self.modeless_dialogs.remove(d) if d in self.modeless_dialogs else None); d.show()
    def open_yfactor_dialog(self): YFactorSettingsDialog(self).exec_()
    def open_encoders_dialog(self): EncodersDialog(self).exec_()
    def open_power_dialog(self): PowerSettingsDialog(self).exec_()
    def open_peak_calibration(self): PeakCalibrationDialog(self).exec_()
    def open_b210_calibration(self): B210CalibrationDialog(self).exec_()
    def run_thread(self,fn,name='WT7Worker'): threading.Thread(target=fn,name=name,daemon=True).start()
    def connect_all(self):
        pending=[(n,c) for n,c in self.configs.items() if n not in self.sessions]
        if not pending: self.set_status('Already connected.'); return
        self.set_status('Connecting antennas...')
        for n,_ in pending: self.cards[n].set_state('CONNECTING')
        def one(n,c):
            try:
                s=SafeAntenna(c,self.motion_event); p=s.read_position(); s.update_oled_connected(); self.emit(lambda data:self.finish_connect(*data),(n,s,p,''))
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
        self.tracking_stop.set(); self.tracking_nominal_az.clear()
        for n in self.sessions:
            if n in self.cards: self.cards[n].set_state('STOPPED')
        self.set_status('Tracking stopped.')
    def stop_all(self):
        self.tracking_stop.set(); self.tracking_nominal_az.clear(); [ev.set() for ev in self.jog_stops.values()]
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
        self.tracking_stop.set(); self.tracking_nominal_az.clear()
        stop=threading.Event()
        try:
            target=self.current_tracking_target(kind)
            for n,s in self.sessions.items():
                effective_target=self.apply_scan_offset(target,n)
                s.config.limits.assert_position_allowed(effective_target.azimuth,effective_target.elevation)
        except Exception as e:
            self.tracking_kind=''; self.current_target=None; self.tracking_stop=threading.Event(); self.set_status(f'Tracking fault: {e}'); return
        self.tracking_stop=stop; self.tracking_kind=kind; self.apply_target(target)
        for n in self.sessions: self.cards[n].set_state('TRACKING')
        self.run_thread(lambda:self.tracking_loop(kind,stop),'Tracking'); self.set_status(f'Tracking {kind.title()}.')
    def tracking_loop(self,kind,stop):
        active_threads={}
        try:
            while not stop.is_set():
                target=self.current_tracking_target(kind); self.emit(lambda t:self.apply_target(t),target)
                for name,session in list(self.sessions.items()):
                    if stop.is_set(): break
                    thread=active_threads.get(name)
                    if thread and thread.is_alive(): continue
                    thread=threading.Thread(target=lambda n=name,s=session:self.tracking_worker(n,s,kind,stop),name=f'Track{name}',daemon=True)
                    active_threads[name]=thread; thread.start()
                until=time.monotonic()+max(0.1,self.site.track_interval_seconds)
                while not stop.is_set() and time.monotonic()<until: time.sleep(0.1)
        except Exception as e:
            if stop is self.tracking_stop:
                self.tracking_kind=''; self.tracking_stop.set()
            self.emit(lambda m:self.set_status(f'Tracking fault: {m}'),str(e))
        finally:
            for thread in list(active_threads.values()):
                if thread.is_alive(): thread.join(timeout=1.0)
    def tracking_worker(self,name,session,kind,stop):
        try:
            target=self.current_tracking_target(kind)
            effective_target=self.apply_scan_offset(target,name)
            display_state=self.movement_display_state(name,session,effective_target,'TRACKING')
            force_comp=self.az_lh_compensation_for_tracking(name,session,effective_target,'TRACKING')
            self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(name,display_state))
            session.config.limits.assert_position_allowed(effective_target.azimuth,effective_target.elevation)
            def live_target(_pos):
                latest=self.current_tracking_target(kind)
                self.emit(lambda t:self.apply_target(t),latest)
                effective=self.apply_scan_offset(latest,name)
                return effective.azimuth,effective.elevation
            session.guarded_slew_to(effective_target.azimuth,effective_target.elevation,session.config.az_track_speed,session.config.el_track_speed,stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p,n=name:self.emit(lambda data:self.update_position(*data),(n,p)),target_callback=live_target,apply_az_low_to_high_compensation=True,force_az_low_to_high_compensation=force_comp)
            if not stop.is_set():
                try:
                    latest=self.apply_scan_offset(self.current_tracking_target(kind),name)
                    state=self.movement_display_state(name,session,latest,'TRACKING')
                except Exception:
                    state='TRACKING'
                self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(name,state))
        except Exception as e:
            if stop is self.tracking_stop:
                self.tracking_kind=''; stop.set()
            self.emit(lambda data:self.mark_fault(*data),(name,str(e)))
    def park_all(self):
        if not self.sessions: self.set_status('Connect antennas before parking.'); return
        self.tracking_stop.set(); self.tracking_kind=''; self.tracking_nominal_az.clear(); stop=threading.Event(); sessions=list(self.sessions.items()); self.set_status('Parking antennas.')
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
    def slew_all_to_target(self,target,activity,stop,scan_antenna=None):
        threads=[]
        for n,s in list(self.sessions.items()):
            def worker(n=n,s=s):
                try:
                    effective_target=self.apply_scan_offset(target,n)
                    state_activity='SCAN' if activity == 'SCAN' and n == scan_antenna else ('TRACKING' if activity == 'SCAN' else activity)
                    display_state=state_activity if state_activity == 'SCAN' else self.movement_display_state(n,s,effective_target,state_activity)
                    force_comp=self.az_lh_compensation_for_tracking(n,s,effective_target,activity)
                    self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(n,display_state)); s.config.limits.assert_position_allowed(effective_target.azimuth,effective_target.elevation)
                    s.guarded_slew_to(effective_target.azimuth,effective_target.elevation,s.config.az_track_speed,s.config.el_track_speed,stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p,n=n:self.emit(lambda data:self.update_position(*data),(n,p)),apply_az_low_to_high_compensation=(activity!='SCAN'),force_az_low_to_high_compensation=force_comp)
                    if not stop.is_set(): self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(n,state_activity))
                except Exception as e: self.emit(lambda data:self.mark_fault(*data),(n,str(e)))
            t=threading.Thread(target=worker,daemon=True); threads.append(t); t.start()
        for t in threads: t.join()
    def az_lh_compensation_for_tracking(self,name,session,target,activity):
        if activity == 'SCAN': return False
        if activity != 'TRACKING' or not self.tracking_kind: return None
        previous=self.tracking_nominal_az.get(name)
        self.tracking_nominal_az[name]=target.azimuth
        if previous is None: return None
        try: delta=session.config.limits.azimuth_delta_to_target(previous,target.azimuth)
        except Exception: delta=shortest_angle_delta(previous,target.azimuth)
        if abs(delta) < 1e-6: return None
        return delta > 0.0

    def movement_display_state(self,name,session,target,activity):
        if activity != 'TRACKING': return 'SLEWING'
        pos=self.positions.get(name)
        if not pos: return 'SLEWING'
        try: az_error=abs(session.config.limits.azimuth_delta_to_target(pos.azimuth,target.azimuth))
        except Exception: az_error=abs(shortest_angle_delta(pos.azimuth,target.azimuth))
        el_error=abs(target.elevation-pos.elevation); gross_az=max(self.site.az_slow_threshold_degrees,self.az_tol()*3.0); gross_el=max(self.site.el_slow_threshold_degrees,self.el_tol()*3.0)
        return 'SLEWING' if az_error > gross_az or el_error > gross_el else 'TRACKING'
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
        self.current_target=target; self.source_name.setText(target.name); self.source_az.setText(f'{target.azimuth:06.2f}'); self.source_el.setText(f'{target.elevation:06.2f}'); self.source_ha.setText(self.hour_angle(target.name))
        for n,c in self.cards.items(): c.set_target(self.card_targets.get(n,target),self.positions.get(n))
    def set_antenna_target(self,name,target):
        if target is None: self.card_targets.pop(name,None)
        else: self.card_targets[name]=target
        if name in self.cards: self.cards[name].set_target(self.card_targets.get(name,self.current_target),self.positions.get(name))
    def hour_angle(self,name):
        now=datetime.now(timezone.utc); lst=local_sidereal_time(self.site.longitude,now)/15.0
        if name=='Sun': ra=sun_equatorial(now).ra_hours
        elif name=='Moon': ra=moon_equatorial(now)[0].ra_hours
        elif name in self.sources: ra=self.sources[name].ra_hours
        else: return '--'
        return ha_text(lst-ra)
    def update_reference(self):
        now=datetime.now().astimezone(); utc=now.astimezone(timezone.utc); self.local.setText(f'Local {now:%Y-%m-%d %H:%M:%S %Z}'); self.utc.setText(f'UTC {utc:%Y-%m-%d %H:%M:%S}'); self.lmst.setText(f'LMST {hms(local_sidereal_time(self.site.longitude,utc)/15.0*3600)}')
        sun=self.target_for_kind('sun'); moon=self.target_for_kind('moon'); self.sun.setText(f'SUN AZ {sun.azimuth:06.2f} EL {sun.elevation:06.2f}'); self.moon.setText(f'MOON AZ {moon.azimuth:06.2f} EL {moon.elevation:06.2f}')
        if self.tracking_kind:
            try: self.apply_target(self.reference_target_for_label(self.tracking_kind,sun,moon))
            except Exception as e: self.set_status(f'Target update fault: {e}')
        elif self.yfactor_hot_label:
            try: self.apply_target(self.reference_target_for_label(self.yfactor_hot_label,sun,moon))
            except Exception as e: self.set_status(f'Y Factor target update fault: {e}')
    def reference_target_for_label(self,label,sun,moon):
        key=(label or '').strip().lower()
        if key == 'sun': return sun
        if key == 'moon': return moon
        if key == 'source': return self.current_tracking_target('source')
        if label in self.sources:
            src=self.sources[label]; return source_position(src.name,src.ra_hours,src.dec_degrees,self.site.latitude,self.site.longitude)
        return self.yfactor_hot_target(label)
    def poll_positions(self):
        if not self.sessions: return
        def worker():
            for n,s in list(self.sessions.items()):
                try: self.emit(lambda data:self.update_position(*data),(n,s.read_position()))
                except Exception as e: self.emit(lambda data:self.mark_fault(*data),(n,str(e)))
        self.run_thread(worker,'Poll')
    def update_position(self,name,pos):
        self.positions[name]=pos; c=self.cards[name]; c.set_position(pos); c.set_target(self.card_targets.get(name,self.current_target),pos); cfg=self.configs[name]; c.set_limits_ok(cfg.limits.is_az_allowed(pos.azimuth) and cfg.limits.is_el_allowed(pos.elevation))
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
            save_scan_config(self.config_path,cfg); self.scan_stop.clear(); self.scan_resume_kind=self.tracking_kind; self.tracking_stop.set(); self.active_scan_antenna=cfg.antenna_name; self.active_scan_dialog=dialog; dialog.set_status(f'{axis.value} scan starting on {cfg.antenna_name}.'); self.set_status(f'{axis.value} scan starting on {cfg.antenna_name}.')
            self.scan_thread=threading.Thread(target=lambda:self.scan_worker(axis,cfg,dialog),daemon=True); self.scan_thread.start()
        except Exception as exc: dialog.set_status(str(exc))
    def stop_scan(self):
        self.scan_stop.set(); self.set_scan_offset(None); self.set_status('Scan stop requested.')
        dialog=self.active_scan_dialog
        if dialog: self.emit(lambda d:d.set_status('Scan stop requested.'),dialog)
        session=self.sessions.get(self.active_scan_antenna)
        if session: self.run_thread(lambda s=session:s.stop_all(),'StopScanAntenna')
    def scan_preload_offset(self,axis,cfg,first_offset):
        if axis != Axis.AZIMUTH: return None
        comp=max(0.0,float(self.configs[cfg.antenna_name].az_low_to_high_compensation or 0.0))
        if comp <= 0.0: return None
        return first_offset + comp if cfg.az_scan_high_to_low else first_offset - comp
    def slew_scan_offset(self,cfg,axis,offset,status_text=None,dialog=None):
        nominal=self.current_tracking_target(self.tracking_kind)
        self.set_scan_offset(cfg.antenna_name,axis,offset)
        self.emit(lambda t:self.apply_target(t),nominal)
        if status_text and dialog: self.emit(lambda s:dialog.set_status(s),status_text)
        self.slew_all_to_target(nominal,'SCAN',self.scan_stop,scan_antenna=cfg.antenna_name)
        return nominal

    def resume_tracking_after_scan(self,kind):
        if not kind or not self.sessions: return
        try:
            target=self.current_tracking_target(kind); self.apply_target(target)
            stop=threading.Event(); self.tracking_stop=stop; self.tracking_kind=kind; self.tracking_nominal_az.clear()
            for name,session in self.sessions.items():
                try:
                    state=self.movement_display_state(name,session,self.apply_scan_offset(target,name),'TRACKING')
                except Exception:
                    state='TRACKING'
                self.cards[name].set_state(state)
            self.run_thread(lambda:self.tracking_loop(kind,stop),'Tracking')
        except Exception as exc:
            self.tracking_kind=''; self.set_status(f'Tracking resume fault: {exc}')

    def scan_worker(self,axis,cfg,dialog):
        rows=[]; offsets=self.scan_offsets(axis,cfg); total_points=max(1,len(offsets)*cfg.scan_count); point_no=0; scan_dir=Path(self.config_path).parent/'scan'; scan_dir.mkdir(exist_ok=True); csv_path=scan_dir/f"wt7_scan_{cfg.antenna_name.lower()}_{axis.value}_{datetime.now():%Y%m%d-%H%M%S}.csv"
        try:
            self.set_scan_offset(cfg.antenna_name,axis,offsets[0] if offsets else 0.0)
            for scan_no in range(1,cfg.scan_count+1):
                if offsets and not self.scan_stop.is_set():
                    preload=self.scan_preload_offset(axis,cfg,offsets[0])
                    if preload is not None:
                        self.slew_scan_offset(cfg,axis,preload,f'{cfg.antenna_name} {axis.value} scan {scan_no}/{cfg.scan_count} preload offset {preload:+0.2f}',dialog)
                for offset in offsets:
                    if self.scan_stop.is_set(): break
                    point_no+=1
                    nominal=self.slew_scan_offset(cfg,axis,offset,f'{cfg.antenna_name} {axis.value} scan {scan_no}/{cfg.scan_count} point {point_no}/{total_points} offset {offset:+0.2f}',dialog); target=self.offset_target(nominal,axis,offset)
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
            resume_kind=self.scan_resume_kind; self.scan_resume_kind=''
            self.set_scan_offset(None)
            if self.active_scan_antenna == cfg.antenna_name:
                self.active_scan_antenna=''; self.active_scan_dialog=None
            if resume_kind:
                self.emit(lambda k:self.resume_tracking_after_scan(k),resume_kind)
            self.scan_stop.clear()
    def collect_power_point(self,axis,offset,dwell,nominal,target,antenna,scan_no):
        vals=[]; last_sequence=getattr(self.power,'power_sequence',0); end=time.monotonic()+dwell
        while time.monotonic()<end and not self.scan_stop.is_set():
            m=self.power.current_raw_power_measurement(antenna,last_sequence)
            if m:
                vals.append(m); last_sequence=getattr(self.power,'power_sequence',last_sequence)
            time.sleep(0.02)
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
    def yfactor_phase_target(self,phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec):
        hot=self.yfactor_hot_target(label)
        return hot if phase=='hot' else self.yfactor_cold_target(cold_mode,hot,cold_az,cold_el,cold_ra,cold_dec)
    def yfactor_phase_target_tuple(self,phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec):
        target=self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec)
        return target.azimuth,target.elevation
    def yfactor_position_error(self,session,pos,target):
        return session.config.limits.azimuth_delta_to_target(pos.azimuth,target.azimuth), target.elevation-pos.elevation
    def yfactor_slew_and_settle(self,session,antenna,phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,dialog):
        last={}
        for attempt in range(1,4):
            target=self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec)
            session.guarded_slew_to(target.azimuth,target.elevation,session.config.az_track_speed,session.config.el_track_speed,self.yfactor_stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p:self.emit(lambda data:self.update_position(*data),(antenna,p)),target_callback=(lambda _p, phase=phase: self.yfactor_phase_target_tuple(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec)))
            pos=session.read_position(); self.emit(lambda data:self.update_position(*data),(antenna,pos))
            target=self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec); az_err,el_err=self.yfactor_position_error(session,pos,target)
            last={'target_az':target.azimuth,'target_el':target.elevation,'antenna_az':pos.azimuth,'antenna_el':pos.elevation,'az_error':az_err,'el_error':el_err,'settle_attempts':attempt}
            if abs(az_err) <= self.az_tol() and abs(el_err) <= self.el_tol(): return last
            if self.yfactor_stop.is_set(): return last
            self.emit(lambda s:dialog.set_status(s),f'Measurement {phase}: settling antenna, AZ err {az_err:+0.2f} EL err {el_err:+0.2f}.')
        raise RuntimeError(f'Y Factor {phase} did not settle within tolerance; AZ err {last.get("az_error",0.0):+0.2f} EL err {last.get("el_error",0.0):+0.2f}.')
    def start_yfactor(self,dialog,antenna,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,count,dwell,alternate):
        if self.yfactor_thread and self.yfactor_thread.is_alive(): dialog.set_status('Y Factor already running.'); return
        if antenna not in self.sessions: dialog.set_status('Connect and select an antenna before Y Factor measurement.'); return
        if self.power.current_power_measurement(antenna) is None: dialog.set_status(f"Start B210 power and wait for CH {self.power.power_channel_for_antenna(antenna)} readings before Y Factor measurement."); return
        self.tracking_stop.set(); self.tracking_kind=''; self.tracking_nominal_az.clear(); self.yfactor_hot_label=label; self.apply_target(self.yfactor_hot_target(label))
        self.yfactor_stop.clear(); dialog.set_status(f'Y Factor starting on {antenna}.'); self.set_status(f'Y Factor starting on {antenna}.')
        self.cards[antenna].set_state('YFACTOR')
        self.yfactor_thread=threading.Thread(target=lambda:self.yfactor_worker(dialog,antenna,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,count,dwell,alternate),daemon=True); self.yfactor_thread.start()
    def stop_yfactor(self): self.yfactor_stop.set(); self.set_status('Y Factor stop requested.')
    def yfactor_worker(self,dialog,antenna,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,count,dwell,alternate):
        rows=[]; out=Path(self.config_path).parent/'yfactor'; out.mkdir(exist_ok=True); path=out/f"wt7_yfactor_{antenna.lower()}_{datetime.now():%Y%m%d-%H%M%S}.csv"; session=self.sessions[antenna]
        try:
            for name,s in list(self.sessions.items()):
                if name!=antenna: s.stop_all(); self.emit(lambda n:self.cards[n].set_state('STOPPED'),name)
            self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(antenna,'YFACTOR'))
            for i in range(1,count+1):
                phases=('hot','cold') if (not alternate or i%2==1) else ('cold','hot'); results={}
                for phase in phases:
                    if self.yfactor_stop.is_set(): break
                    hot=self.yfactor_hot_target(label); target=self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec)
                    card_target=target if phase!='hot' else None
                    self.emit(lambda s:dialog.set_status(s),f'Measurement {i}/{count}: {phase}.'); self.emit(lambda t:self.apply_target(t),hot); self.emit(lambda data:self.set_antenna_target(*data),(antenna,card_target)); self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(antenna,'YFACTOR'))
                    start_meta=self.yfactor_slew_and_settle(session,antenna,phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,dialog)
                    results[phase]=self.collect_yfactor_power(antenna,dwell,session,lambda phase=phase:self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec),start_meta)
                if self.yfactor_stop.is_set(): break
                ydb=results['hot']['power_value']-results['cold']['power_value']; rows.append({'local_time':datetime.now().astimezone().isoformat(timespec='seconds'),'antenna':antenna,'measurement':i,'hot_power':results['hot']['power_value'],'cold_power':results['cold']['power_value'],'power_unit':results['hot']['power_unit'],'y_factor_db':ydb,'hot_start_az_error':results['hot'].get('start_az_error'),'hot_start_el_error':results['hot'].get('start_el_error'),'hot_end_az_error':results['hot'].get('end_az_error'),'hot_end_el_error':results['hot'].get('end_el_error'),'cold_start_az_error':results['cold'].get('start_az_error'),'cold_start_el_error':results['cold'].get('start_el_error'),'cold_end_az_error':results['cold'].get('end_az_error'),'cold_end_el_error':results['cold'].get('end_el_error'),'hot_settle_attempts':results['hot'].get('settle_attempts'),'cold_settle_attempts':results['cold'].get('settle_attempts')})
            if rows:
                with path.open('w',newline='',encoding='utf-8') as h: w=csv.DictWriter(h,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
                avg=sum(r['y_factor_db'] for r in rows)/len(rows); msg=f'Y Factor {avg:0.1f} dB, n={len(rows)}'
            else: msg='Y Factor stopped.'
            self.emit(lambda m:dialog.set_status(m),msg); self.emit(lambda m:self.set_status(m),msg)
        except Exception as exc:
            self.emit(lambda m:dialog.set_status(m),str(exc)); self.emit(lambda m:self.set_status(f'Y Factor fault: {m}'),str(exc))
        finally:
            self.yfactor_stop.clear(); self.yfactor_hot_label=''
            self.emit(lambda name:self.set_antenna_target(name,None),antenna)
            self.emit(lambda name:self.cards[name].set_state('STOPPED'),antenna)
    def collect_yfactor_power(self,antenna,dwell,session=None,target_func=None,start_meta=None):
        vals=[]; end=time.monotonic()+dwell
        while time.monotonic()<end and not self.yfactor_stop.is_set():
            m=self.power.current_power_measurement(antenna)
            if m: vals.append(m)
            time.sleep(0.1)
        if not vals: raise RuntimeError('No B210 power measurements were available.')
        result={'power_value':sum(float(v['power_value']) for v in vals)/len(vals),'power_dbfs':sum(float(v['power_dbfs']) for v in vals)/len(vals),'power_unit':vals[-1]['power_unit'],'sample_count':len(vals)}
        if start_meta: result.update({'start_target_az':start_meta.get('target_az'),'start_target_el':start_meta.get('target_el'),'start_antenna_az':start_meta.get('antenna_az'),'start_antenna_el':start_meta.get('antenna_el'),'start_az_error':start_meta.get('az_error'),'start_el_error':start_meta.get('el_error'),'settle_attempts':start_meta.get('settle_attempts')})
        if session and target_func:
            pos=session.read_position(); self.emit(lambda data:self.update_position(*data),(antenna,pos)); target=target_func(); az_err,el_err=self.yfactor_position_error(session,pos,target)
            result.update({'end_target_az':target.azimuth,'end_target_el':target.elevation,'end_antenna_az':pos.azimuth,'end_antenna_el':pos.elevation,'end_az_error':az_err,'end_el_error':el_err})
        return result
    def start_peak_axis_tracking(self,dialog,axis,label,antenna):
        if self.peak_thread and self.peak_thread.is_alive(): dialog.set_status('Peak tracking already running.'); return
        if antenna not in self.sessions: dialog.set_status('Connect and select an antenna.'); return
        self.tracking_stop.set(); self.tracking_nominal_az.clear(); self.peak_stop.clear(); self.peak_thread=threading.Thread(target=lambda:self.peak_axis_worker(dialog,axis,label,antenna),daemon=True); self.peak_thread.start(); dialog.set_status(f'Tracking {axis.value} only.')
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
        try: cfg=self.power.meter_config(); self.power_config=self.power.save_config(); save_power_config(self.config_path,self.power_config); self.power.load_active_calibrations(self.config_path,self.power_config)
        except Exception as e: self.set_status(f'B210 config fault: {e}'); return
        self.b210_stop.clear(); self.power.clear_reading('SDR POWER ON UNCAL')
        def worker():
            try:
                with B210PowerMeter(cfg) as meter:
                    while not self.b210_stop.is_set(): self.emit(lambda r:self.handle_b210(r),meter.read_power())
            except Exception as e:
                if not self.b210_stop.is_set(): self.emit(lambda m:self.set_status(f'B210 fault: {m}'),str(e))
            finally: self.emit(lambda _x:self.power.clear_reading('SDR RELEASED'),None)
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
