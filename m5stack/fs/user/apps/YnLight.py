import bluetooth as bt, micropython, math, time, os
from micropython import const
from M5 import *
try: import ujson as json
except: import json

def _dot(a,b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def _sub(a,b): return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def _scale(a,s): return (a[0]*s, a[1]*s, a[2]*s)
def _cross(a,b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def _norm(a): return math.sqrt(_dot(a,a)) or 1.0
def _unit(a): n=_norm(a); return (a[0]/n, a[1]/n, a[2]/n)
def _proj_perp(v,u): return _sub(v, _scale(u, _dot(v,u)))
def _signed_angle_around_axis(g0,g,u):
    p0=_proj_perp(g0,u); p=_proj_perp(g,u)
    n0=_norm(p0); n1=_norm(p)
    if n0<1e-6 or n1<1e-6: return 0.0
    p0=(p0[0]/n0,p0[1]/n0,p0[2]/n0); p=(p[0]/n1,p[1]/n1,p[2]/n1)
    return math.atan2(_dot(u,_cross(p0,p)), _dot(p0,p))

class HoldRunner:
    def __init__(self, is_holding_fn, on_start=None, on_tick=None, on_stop=None, min_hold_ms=0, tick_period_ms=0):
        self.f=is_holding_fn; self.a=on_start or (lambda:None); self.t=on_tick; self.s=on_stop or (lambda:None)
        self.d=int(min_hold_ms); self.p=int(tick_period_ms); self.t0=None; self.started=False; self.lt=0
    def poll(self):
        now=time.ticks_ms()
        if self.f():
            if self.t0 is None: self.t0=now
            if (not self.started) and time.ticks_diff(now,self.t0)>=self.d:
                self.started=True; self.lt=now; self.a()
            if self.started and self.t and (self.p<=0 or time.ticks_diff(now,self.lt)>=self.p):
                self.lt=now; self.t()
        else:
            if self.started: self.s()
            self.t0=None; self.started=False

class TiltOnHold:
    AXIS={'x':(1.0,0.0,0.0),'y':(0.0,1.0,0.0)}
    def __init__(self,is_holding_fn,imu,axis='auto',max_deg=30,invert=False,min_hold_ms=200,tick_period_ms=50,
                 smooth=0.3,on_change=None,start_value=0,start_value_fn=None,on_start=None,mode='relative',deadzone_deg=1.0):
        self.imu=imu; self.mode=mode; self.u=self.AXIS['y']; self.maxr=math.radians(max(1,int(max_deg)))
        self.sgn=-1.0 if invert else 1.0; self.a=max(0.0,min(1.0,float(smooth))); self.cb=on_change
        self.sv=start_value; self.svf=start_value_fn; self.on_start=on_start; self.dz=math.radians(max(0.0,float(deadzone_deg)))
        self.g0=(0.0,0.0,1.0); self.anchor=0; self.value=0; self.axis_mode=axis
        self.hr=HoldRunner(is_holding_fn, self._start, self._tick, self._stop, min_hold_ms, tick_period_ms)
    def poll(self): self.hr.poll()
    def _read_g(self): ax,ay,az=self.imu.getAccel(); return _unit((ax,ay,az))
    def _choose_axis_auto(self,g0):
        best=('y',self.AXIS['y']); bs=1.0
        for _,u in self.AXIS.items():
            s=abs(_dot(g0,u))
            if s<bs: bs=s; best=(None,u)
        self.u=best[1]
    def _calib(self):
        sx=sy=sz=0.0
        for _ in range(6):
            ax,ay,az=self.imu.getAccel(); sx+=ax; sy+=ay; sz+=az; time.sleep_ms(10)
        self.g0=_unit((sx/6,sy/6,sz/6))
        if self.axis_mode=='auto': self._choose_axis_auto(self.g0)
        elif self.axis_mode in ('x','y'): self.u=self.AXIS[self.axis_mode]
    def _set(self,v):
        if v<-100:v=-100
        if v>100:v=100
        vi=int(v)
        if vi!=self.value:
            self.value=vi
            if self.cb: self.cb(self.value)
    def _map(self,ang):
        if abs(ang)<self.dz: target=float(self.value)
        else:
            k=ang/self.maxr; k=-1.0 if k<-1.0 else (1.0 if k>1.0 else k)
            target=(self.anchor+100.0*self.sgn*k) if self.mode=='relative' else (100.0*self.sgn*k)
        self._set((1.0-self.a)*float(self.value)+self.a*target)
    def _start(self):
        self._calib()
        v0=self.svf() if self.svf else self.sv
        v0=-100 if v0<-100 else (100 if v0>100 else v0)
        self.anchor=int(v0); self._set(self.anchor)
        if self.on_start:
            try: self.on_start(self.anchor)
            except: pass
    def _tick(self): self._map(_signed_angle_around_axis(self.g0,self._read_g(),self.u))
    def _stop(self): pass

SRV_UUID_STR='f000aa60-0451-4000-b000-000000000000'
CHR_UUID_STR='f000aa61-0451-4000-b000-000000000000'
_IRQ_SCAN_RESULT=const(5); _IRQ_SCAN_DONE=const(6); _IRQ_PERIPHERAL_CONNECT=const(7); _IRQ_PERIPHERAL_DISCONNECT=const(8)
_IRQ_GATTC_SERVICE_RESULT=const(9); _IRQ_GATTC_SERVICE_DONE=const(10); _IRQ_GATTC_CHARACTERISTIC_RESULT=const(11)
_IRQ_GATTC_CHARACTERISTIC_DONE=const(12); _IRQ_GATTC_WRITE_DONE=const(17)
_ADV_TYPE_SHORT_NAME=const(0x08); _ADV_TYPE_COMPLETE_NAME=const(0x09)
_ADV_UUID16_INCOMP=const(0x02); _ADV_UUID16_COMP=const(0x03); _ADV_UUID32_INCOMP=const(0x04); _ADV_UUID32_COMP=const(0x05)
_ADV_UUID128_INCOMP=const(0x06); _ADV_UUID128_COMP=const(0x07)
_NAMES_PATH='apps/yn360_names.json'

def _clamp(v,lo,hi): v=int(v); return lo if v<lo else (hi if v>hi else v)
def _decode_field(p,t):
    i=0;o=[]
    while i+1<len(p):
        ln=p[i]; 
        if ln==0: break
        if p[i+1]==t: o.append(p[i+2:i+1+ln])
        i+=1+ln
    return o
def _adv_name(p):
    n=_decode_field(p,_ADV_TYPE_COMPLETE_NAME) or _decode_field(p,_ADV_TYPE_SHORT_NAME)
    try: return (n and n[0].decode('utf-8')) or None
    except: return None
def _addr_str(a): return ":".join("{:02X}".format(b) for b in a)
def _services_from_adv(p):
    s=[]
    for c in (_ADV_UUID16_INCOMP,_ADV_UUID16_COMP,_ADV_UUID32_INCOMP,_ADV_UUID32_COMP,_ADV_UUID128_INCOMP,_ADV_UUID128_COMP):
        for u in _decode_field(p,c): s.append(bt.UUID(u))
    return s
def _ensure_dir(path):
    try:
        d=path.rsplit('/',1)[0]
        if d: os.stat(d)
    except OSError:
        try: os.mkdir(d)
        except: pass
def _read_json(p):
    try:
        with open(p,'r') as f: return json.load(f)
    except: return {}
def _write_json(p,obj):
    try: _ensure_dir(p); open(p,'w').write(json.dumps(obj)); return True
    except: return False
def _mac_key(a): return ":".join("{:02X}".format(b) for b in a)

class YN360Controller:
    def __init__(self, ble=None, on_update=None, max_conns=3):
        self._ble=ble or bt.BLE(); self._ble.active(True); self._ble.irq(self._irq)
        try: self._ble.config(gap_name='M5');
        except: pass
        self.SRV_UUID=bt.UUID(SRV_UUID_STR); self.CHR_UUID=bt.UUID(CHR_UUID_STR)
        self._by_addr={}; self._name2addr={}; self._scanning=False; self._auto=False
        self._q=[]; self._sched=False; self._cd={}; self._max=max_conns; self._cb=on_update
        self._names=_read_json(_NAMES_PATH); self._last_sig=None
    def set_callback(self,cb): self._cb=cb
    def scan(self,duration_ms=4000,active=True,auto_connect=True):
        if self._scanning:
            try: self._ble.gap_scan(None)
            except: pass
        self._scanning=True; self._auto=auto_connect
        self._ble.gap_scan(duration_ms,30000,30000,active)
    def connected_devices(self): return self._snap()
    def send_scene_by_name(self,name,scene):
        a=self._name2addr.get(name)
        if a: return self.send_scene_by_addr(a,scene)
        for a,st in self._by_addr.items():
            if st.get('adv')==name and st.get('conn') is not None:
                return self.send_scene_by_addr(a,scene)
        return False
    def send_scene_by_addr(self,addr,scene):
        st=self._by_addr.get(addr)
        if not st: return False
        st.setdefault('txq',[]).append(self._frame(scene,st))
        if st.get('conn') is not None and st.get('ch_val') is not None and not st.get('tx_busy'):
            self._drain(addr)
        elif st.get('conn') is not None and not st.get('sv_range'):
            self._ble.gattc_discover_services(st['conn'])
        return True
    def send_scene_all(self,scene):
        ok=False
        for a,st in self._by_addr.items():
            if st.get('conn') is not None: ok|=self.send_scene_by_addr(a,scene)
        return ok
    def _snap(self):
        o=[]
        for a,st in self._by_addr.items():
            if st.get('conn') is None: continue
            o.append({'addr':a,'addr_str':_addr_str(a),'name':st.get('name'),'adv':st.get('adv'),
                      'rssi':st.get('rssi'),'ready':st.get('ch_val') is not None})
        return o
    def _notify(self):
        if not self._cb: return
        lst=self._snap(); sig=[]
        for d in lst:
            mac=":".join("{:02X}".format(b) for b in d['addr'])
            sig.append(mac+('#1' if d['ready'] else '#0'))
        sig=tuple(sorted(sig))
        if sig==self._last_sig: return
        self._last_sig=sig; self._cb(lst)
    def _rebuild(self):
        self._name2addr={}
        for a,st in self._by_addr.items():
            al=st.get('name')
            if al: self._name2addr[al]=a
    def _alias(self,addr):
        k=_mac_key(addr); al=self._names.get(k)
        if al: return al
        used=set(self._names.values()); n=1
        while True:
            c="{:02d}".format(n)
            if c not in used: al=c; break
            n+=1
        self._names[k]=al; _write_json(_NAMES_PATH,self._names); return al
    def _queue_all(self):
        self._q=[]; now=time.ticks_ms()
        for a,st in self._by_addr.items():
            if st.get('conn') is None and not st.get('connecting'):
                if time.ticks_diff(self._cd.get(a,0),now)<=0: self._q.append(a)
    def _schedule(self):
        if not self._sched:
            self._sched=True; micropython.schedule(self._run,0)
    def _run(self,_):
        self._sched=False; busy=0
        for st in self._by_addr.values():
            if st.get('conn') is not None or st.get('connecting'): busy+=1
        if busy>=self._max: return
        now=time.ticks_ms()
        self._q=[a for a in self._q if time.ticks_diff(self._cd.get(a,0),now)<=0]
        cand=None
        while self._q:
            a=self._q.pop(0); st=self._by_addr.get(a)
            if st and st.get('conn') is None and not st.get('connecting'): cand=a; break
        if cand is None: return
        st=self._by_addr[cand]; st['connecting']=True
        try: self._ble.gap_connect(st['addr_type'],cand)
        except OSError as e:
            st['connecting']=False; err=e.args[0]
            if err==16: self._cd[cand]=time.ticks_add(now,200); self._q.append(cand)
            if self._q: self._schedule()
    def _drain(self,addr):
        st=self._by_addr.get(addr)
        if not st or st.get('ch_val') is None: return
        q=st.get('txq') or []
        if not q: return
        data=q.pop(0)
        try: self._ble.gattc_write(st['conn'],st['ch_val'],data,0)
        except OSError: pass
    def _frame(self,scene,st):
        m=scene.get('mode')
        if m=='light':
            w=scene.get('white',0); y=scene.get('yellow',0)
            s=w+y; last=st.get('last_sum')
            if last==s:
                if w<99: w+=1
                elif w>0: w-=1
                s=w+y
            st['last_sum']=s
            return bytes((0xAE,0xAA,0x01,w,y,0x56))
        if m=='color':
            r,g,b=scene.get('color',(0,0,0))
            return bytes((0xAE,0xA1,int(r/100*255),int(g/100*255),int(b/100*255),0x56))
        if m=='off': return bytes((0xAE,0xAA,0x01,0,0,0x56))
    def _irq(self,event,data):
        if event==_IRQ_SCAN_RESULT:
            at,addr,_,rssi,adv=data; addr=bytes(addr)
            try:
                if self.SRV_UUID in _services_from_adv(adv):
                    st=self._by_addr.get(addr); advn=_adv_name(adv)
                    if not st:
                        al=self._alias(addr)
                        st={'addr_type':at,'name':al,'adv':advn,'rssi':rssi,'conn':None,'connecting':False,
                            'sv_range':None,'ch_val':None,'txq':[],'tx_busy':False,'last_sum':None}
                        self._by_addr[addr]=st; self._name2addr[al]=addr
                    else:
                        st['rssi']=rssi
                        if not st.get('adv') and advn: st['adv']=advn
            except: pass
        elif event==_IRQ_SCAN_DONE:
            self._scanning=False
            if self._auto: self._queue_all(); self._schedule()
        elif event==_IRQ_PERIPHERAL_CONNECT:
            ch,at,addr=data; addr=bytes(addr); st=self._by_addr.get(addr)
            if not st:
                try: self._ble.gap_disconnect(ch)
                except: pass
                return
            st['conn']=ch; st['connecting']=False; st['sv_range']=None; st['ch_val']=None
            self._ble.gattc_discover_services(ch); self._notify()
        elif event==_IRQ_PERIPHERAL_DISCONNECT:
            ch,at,addr=data; addr=bytes(addr); st=self._by_addr.get(addr)
            if st:
                st['conn']=None; st['connecting']=False; st['sv_range']=None; st['ch_val']=None; st['tx_busy']=False
                self._notify()
        elif event==_IRQ_GATTC_SERVICE_RESULT:
            ch,sh,eh,uuid=data
            if isinstance(uuid,bt.UUID) and uuid==self.SRV_UUID:
                for a,st in self._by_addr.items():
                    if st.get('conn')==ch: st['sv_range']=(sh,eh)
        elif event==_IRQ_GATTC_SERVICE_DONE:
            ch,status=data
            for a,st in self._by_addr.items():
                if st.get('conn')==ch:
                    rng=st.get('sv_range')
                    if rng: self._ble.gattc_discover_characteristics(ch,rng[0],rng[1])
        elif event==_IRQ_GATTC_CHARACTERISTIC_RESULT:
            ch,_,vh,_,uuid=data
            if isinstance(uuid,bt.UUID) and uuid==self.CHR_UUID:
                for a,st in self._by_addr.items():
                    if st.get('conn')==ch: st['ch_val']=vh
        elif event==_IRQ_GATTC_CHARACTERISTIC_DONE:
            ch,status=data
            for a,st in self._by_addr.items():
                if st.get('conn')==ch: self._drain(a); self._notify()
        elif event==_IRQ_GATTC_WRITE_DONE:
            ch,vh,status=data
            for a,st in self._by_addr.items():
                if st.get('conn')==ch and st.get('ch_val')==vh:
                    time.sleep_ms(4); self._drain(a)

class App:
    def __init__(self): pass
    def on_imu_value(self,v):
        if not self.devices: return 0
        v=0 if v<0 else (100 if v>100 else v)
        if self.dev_state['mode']=='light':
            if self.cursor==1: self.dev_state['white']=v
            elif self.cursor==2: self.dev_state['yellow']=v
            self.set_state(); self.draw(notall=True)
        elif self.dev_state['mode']=='color':
            self.dev_state['color'][self.cursor-1]=v; self.set_state(); self.draw(notall=True)
    def on_imu_start(self):
        if not self.devices: return 0
        if self.dev_state['mode']=='light':
            return self.dev_state['white'] if self.cursor==1 else self.dev_state['yellow']
        if self.dev_state['mode']=='color':
            return self.dev_state['color'][self.cursor-1]
        return 0
    def start(self,app):
        self.devices=[]
        try:
            with open('apps/yn360_states.json','r') as f:
                self.devs_state=json.load(f)
        except:
            self.devs_state={};
            

        self.current_device=0; self.app=app;  self.dev_state={}; self.cursor=0
        self.tilt=TiltOnHold(BtnA.isHolding, Imu, axis='auto', max_deg=30, invert=True, min_hold_ms=200,
                             tick_period_ms=50, smooth=0.3, mode='relative', on_change=self.on_imu_value,
                             start_value_fn=self.on_imu_start)
        self.bt=YN360Controller(ble=self.app.ble,on_update=self.on_update,max_conns=12)
        self.app.callback_table_long['left']=self.scan
        self.app.callback_table['left']=self.next_dev
        self.app.callback_table_long['ok']=self.next_cursor
        self.app.callback_table['ok']=self.click_handler
        self.app.callback_table['right']=self.minus_handler
        self.app.loop_callback=self.loop
        self.scan()
    def scan(self):
        self.bt.scan(6000,auto_connect=True); self.app.gui.waiter.start(title='Searching')
        time.sleep(6); self.app.gui.waiter.stop(); self.draw()
    def next_cursor(self):
        if not self.devices: return
        self.cursor+=1; m={'light':3,'color':4,'off':1}
        if self.cursor>m[self.dev_state['mode']]-1: self.cursor=0

        self.draw()
        
    def save_devices(self):
        open('apps/yn360_states.json','w').write(json.dumps(self.devs_state))
    def get_dev(self):
        cur=self.devices[self.current_device]['name']
        self.dev_state=self.devs_state.get(cur,{'mode':'light','white':0,'yellow':0,'color':[0,0,0]})
        
        self.bt.send_scene_by_addr(self.devices[self.current_device]['addr'],self.dev_state)
        
    def set_state(self):
        name=self.devices[self.current_device]['name']
        self.devs_state[name]=self.dev_state; self.bt.send_scene_by_addr(self.devices[self.current_device]['addr'],self.dev_state)
    def next_dev(self):
        self.current_device+=1
        if self.current_device>len(self.devices)-1:self.current_device=0
        self.get_dev();self.draw()
        self.save_devices()
    def minus_handler(self):
        if not self.devices: return
        if self.cursor==0:
            m=self.dev_state['mode']
            self.dev_state['mode']='off' if m=='light' else ('color' if m=='off' else 'light')
            self.set_state(); self.draw()
        else:
            if self.dev_state['mode']=='light':
                k='white' if self.cursor==1 else 'yellow'
                self.dev_state[k]=max(0,self.dev_state[k]-10)
            else:
                i=self.cursor-1; self.dev_state['color'][i]=max(0,self.dev_state['color'][i]-10)
            self.set_state(); self.draw(notall=True)
    def click_handler(self):
        if not self.devices:
            self.scan()
            return
        if self.cursor==0:
            m=self.dev_state['mode']
            self.dev_state['mode']='color' if m=='light' else ('off' if m=='color' else 'light')
            self.set_state(); self.draw()
        else:
            if self.dev_state['mode']=='light':
                k='white' if self.cursor==1 else 'yellow'
                self.dev_state[k]=min(100,self.dev_state[k]+10)
            else:
                i=self.cursor-1; self.dev_state['color'][i]=min(100,self.dev_state['color'][i]+10)
            self.set_state(); self.draw()
    def on_update(self,event):
        self.devices=[d for d in event if d['ready']]
        if len(self.devices)<(self.current_device-1): self.current_device=len(self.devices)-1
        if self.devices: self.get_dev()
        self.draw()

    def stop(self):
        self.save_devices()
        self.app.stop_app(); self.app.gui.show_main_menu()
    def txt_center(self,txt,y,font):
        Lcd.setFont(font); Lcd.setTextColor(0xffffff,0x000000)
        w=Lcd.textWidth(txt); x=(125-w)//2+5; Lcd.drawString(txt,x,y)
    def txt(self,txt,x,y,font):
        Lcd.setFont(font); Lcd.setTextColor(0xffffff,0x000000); Lcd.drawString(txt,x,y)
    def draw(self,notall=False):
        if not notall: Lcd.fillRect(0,31,135,240-31,0x000000)
        if not self.devices:
            self.txt_center('NO DEVICES',120,Widgets.FONTS.DejaVu18)
            self.txt_center('Click OK',150,Widgets.FONTS.DejaVu12)
            self.txt_center('to search',166,Widgets.FONTS.DejaVu12); return
        if not notall:
            self.txt_center('Light: '+self.devices[self.current_device]['name'],40,Widgets.FONTS.DejaVu18)
            self.txt('<',5,40,Widgets.FONTS.DejaVu18); self.txt('>',120,40,Widgets.FONTS.DejaVu18)
            modes={'color':'RGB','light':'CCT','off':'OFF'}
            self.txt('Mode: '+modes[self.dev_state['mode']],15,80,Widgets.FONTS.DejaVu18)
            Lcd.drawRect(10,77+self.cursor*25,116,22,0xffffff)
        if self.dev_state['mode']=='light':
            Lcd.fillRect(15,105,105,16,0x121212); Lcd.fillRect(15,105,int(105*self.dev_state['white']/100),16,0xffffff)
            Lcd.fillRect(15,130,105,16,0x807F2F); Lcd.fillRect(15,130,int(105*self.dev_state['yellow']/100),16,0xFFFD00)
        elif self.dev_state['mode']=='color':
            Lcd.fillRect(15,105,105,16,0x690000); Lcd.fillRect(15,105,int(105*self.dev_state['color'][0]/100),16,0xff0000)
            Lcd.fillRect(15,130,105,16,0x0C6900); Lcd.fillRect(15,130,int(105*self.dev_state['color'][1]/100),16,0x00ff00)
            Lcd.fillRect(15,155,105,16,0x000369); Lcd.fillRect(15,155,int(105*self.dev_state['color'][2]/100),16,0x0000ff)
    def loop(self):
        self.tilt.poll()
        self.value=self.tilt.value
