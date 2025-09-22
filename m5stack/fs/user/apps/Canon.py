import ubluetooth as bt
from hardware import Timer
from M5 import *
import gc

from micropython import const
_IRQ_SCAN_RESULT = const(5)
_IRQ_SCAN_DONE = const(6)
_IRQ_PERIPHERAL_CONNECT = const(7)
_IRQ_PERIPHERAL_DISCONNECT = const(8)
_IRQ_GATTC_SERVICE_RESULT = const(9)
_IRQ_GATTC_SERVICE_DONE = const(10)
_IRQ_GATTC_CHARACTERISTIC_RESULT = const(11)
_IRQ_GATTC_CHARACTERISTIC_DONE = const(12)
_IRQ_ENCRYPTION_UPDATE = const(28)

# canon_remote_ble.py
import time, json, os

# --- Canon BR-E1 UUIDs ---
SERVICE_UUID   = bt.UUID("00050000-0000-1000-0000-d8492fffa821")
INIT_CHAR_UUID = bt.UUID("00050002-0000-1000-0000-d8492fffa821")  # handshake: 0x03 + name
CTRL_CHAR_UUID = bt.UUID("00050003-0000-1000-0000-d8492fffa821")  # commands: AF/SHUTTER/MOVIE

# --- AD types ---
_AD_TYPE_UUID16_INCOMPLETE  = 0x02
_AD_TYPE_UUID16_COMPLETE    = 0x03
_AD_TYPE_UUID128_INCOMPLETE = 0x06
_AD_TYPE_UUID128_COMPLETE   = 0x07
_AD_TYPE_NAME_SHORT         = 0x08
_AD_TYPE_NAME_COMPLETE      = 0x09






def usleep_ms(duration_ms=5000):
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < duration_ms:
        time.sleep_ms(10)  

def _uuid128_le_from_str(s: str) -> bytes:
    # "0000F505-0000-1000-8000-00805F9B34FB" -> 16 байт LE как в advertising
    h = s.replace("-", "")
    be = bytes.fromhex(h)     # big endian (стандартная запись)
    le = bytes(reversed(be))  # переворот
    return le

_F505_UUID_LE = _uuid128_le_from_str("00050000-0000-1000-0000-d8492fffa821")

_AD_TYPE_UUID128_INCOMPLETE = 0x06
_AD_TYPE_UUID128_COMPLETE   = 0x07

def _adv_has_service(payload: bytes, uuid_le: bytes) -> bool:
    i = 0
    n = len(payload)
    while i + 1 < n:
        ln = payload[i]
        if ln == 0:
            break
        t = payload[i+1]
        v = payload[i+2:i+1+ln]
        i += 1 + ln
        if t in (_AD_TYPE_UUID128_COMPLETE, _AD_TYPE_UUID128_INCOMPLETE):
            # блоки по 16 байт
            for j in range(0, len(v), 16):
                if v[j:j+16] == uuid_le:
                    return True
    return False

def _adv_get_name(payload: bytes):
    i = 0; n = len(payload)
    while i + 1 < n:
        ln = payload[i]
        if ln == 0: break
        t  = payload[i+1]
        v  = payload[i+2:i+1+ln]
        i += 1 + ln
        if t in (_AD_TYPE_NAME_COMPLETE, _AD_TYPE_NAME_SHORT):
            try: return v.decode()
            except: return None
    return None

def _mac_str(b: bytes) -> str:
    return ":".join("{:02X}".format(x) for x in b)

class CanonRemoteBLE:
    """
    pair(): скан → connect → (pair/шифрование если доступно) → discovery → отправить handshake (fire-and-forget)
            → сохранить MAC и value handles → disconnect.
    show(): connect по сохранённому MAC (без нового pairing); если есть сохранённый handle CTRL, сразу шлёт 0x8C;
            иначе делает discovery → (опц.) handshake → 0x8C → disconnect.
    disconnect(): разорвать соединение и выключить BLE.
    """
    def __init__(self,ble=None,app=None, my_name="M5UiFlow", store="canon_peer.json", scan_ms=5000, verbose=False):
        self.app=app
        self.verbose = verbose
        self.sets_data={}
        self.store = store
        self.scan_ms = scan_ms
        self.ble=ble
        self._peer_cache = None


        # включим бондинг/Just Works (если сборка поддерживает)
        try:
            self.ble.config(bond=True, mitm=False, io=3)  # 3 = NO_INPUT_OUTPUT
        except:
            pass
        try:
            self.ble.config(gap_name=my_name)
        except:
            pass
        self.my_name = my_name

        # runtime state
        self.conn = None
        self.connected = False
        self.encrypted = False
        self.peer_addr_type = None
        self.peer_addr = None

        self._svc_range = None
        self._h_init = None
        self._h_ctrl = None
        self._init_props = 0              # свойства F504 (write/no-rsp)
        self._ctrl_props = 0

        self._discovery_done = False
        self._mode = None                 # "pair" | "show"
        self._auto_handshake_on_discover = False
        self._disconnect_after_handshake = False

        self.ble.irq(self._irq)

    # ---------- IRQ ----------
    def _irq(self, event, data):
        if event == _IRQ_SCAN_RESULT:
            addr_type, addr, adv_type, rssi, adv = data
            name = _adv_get_name(adv) or ""
            if _adv_has_service(adv, _F505_UUID_LE):
                if self.verbose:
                    print("Found:", _mac_str(bytes(addr)), "RSSI", rssi, name)
                self.peer_addr_type = addr_type
                self.peer_addr = bytes(addr)
                try: self.ble.gap_scan(None)
                except: pass
                self.ble.gap_connect(addr_type, addr)

        elif event == _IRQ_SCAN_DONE:
            if self.verbose: print("Scan done")

        elif event == _IRQ_PERIPHERAL_CONNECT:
            self.conn, addr_type, addr = data
            self.connected = True
            self.encrypted = False
            self.peer_addr_type = addr_type
            self.peer_addr = bytes(addr)
            if self.verbose: print("Connected to", _mac_str(self.peer_addr))

            # сброс discovery-стейта
            self._svc_range = None
            self._h_init = None
            self._h_ctrl = None
            self._init_props = 0
            self._ctrl_props = 0
            self._discovery_done = False

            if self._mode == "pair":
                # только в режиме pair просим шифрование
                try:
                    self.ble.gap_pair(self.conn)
                    if self.verbose: print("Pairing requested...")
                except AttributeError:
                    if self.verbose: print("gap_pair() not available")
                # discovery стартуем после шифрования (см. _IRQ_ENCRYPTION_UPDATE)
            else:
                # в show() pairing не делаем — короткая пауза + MTU → discovery
                if not self._conn_ok():
                    return
                usleep_ms(200)
                try:
                    self.ble.gattc_exchange_mtu(self.conn)
                    usleep_ms(100)
                except:
                    pass
                if self._conn_ok():
                    self.ble.gattc_discover_services(self.conn)

        elif event == _IRQ_PERIPHERAL_DISCONNECT:
            self.conn, addr_type, addr = data
            if self.verbose: print("Disconnected")
            if self._mode=='show':
                self.app.set_sh(0)
            self.connected = False
            self.conn = None

        elif event == _IRQ_ENCRYPTION_UPDATE:
            conn_handle, encrypted, authenticated, bonded, key_size = data
            if conn_handle == self.conn:
                self.encrypted = bool(encrypted)
                if self.verbose:
                    print(f"Encryption: enc={self.encrypted} (k{key_size})")
                # сразу сохраним peer — чтобы адрес не потерять
                if self.peer_addr:
                    pass
                    self._save_peer()  # MAC/type
                if self._mode == "pair" and self.encrypted:
                    # после шифрования — MTU и discovery
                    self._svc_range = None
                    self._h_init = None
                    self._h_ctrl = None
                    self._init_props = 0
                    self._ctrl_props = 0
                    self._discovery_done = False
                    try: self.ble.gattc_exchange_mtu(self.conn)
                    except: pass
                    self.ble.gattc_discover_services(self.conn)

        elif event == _IRQ_GATTC_SERVICE_RESULT:
            conn, start, end, uuid = data
            if conn == self.conn and uuid == SERVICE_UUID:
                self._svc_range = (start, end)

        elif event == _IRQ_GATTC_SERVICE_DONE:
            if self._svc_range:
                s, e = self._svc_range
                self.ble.gattc_discover_characteristics(self.conn, s, e)
            else:
                if self.verbose: print("F505 service not found")

        elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
            conn, defh, vh, props, uuid = data
            if conn == self.conn:
                if uuid == INIT_CHAR_UUID:
                    self._h_init = vh
                    self._init_props = props
                    if self.verbose:
                        print("INIT F504 val_handle", vh, "props", hex(props))
                elif uuid == CTRL_CHAR_UUID:
                    self._h_ctrl = vh
                    self._ctrl_props = props
                    if self.verbose:
                        print("CTRL F506 val_handle", vh, "props", hex(props))

        elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
            self._discovery_done = True
            # Сохраним handles вместе с MAC — полезно для быстрой съёмки в show()
            if self.peer_addr:
                try:
                    self._save_peer({"h_init": self._h_init or -1, "h_ctrl": self._h_ctrl or -1})
                except:
                    pass
            # Авто-handshake только если включено для текущего режима (pair)
            if self._h_init and self._auto_handshake_on_discover:
                usleep_ms(120)
                self._send_handshake_fire_and_forget()
                if self._disconnect_after_handshake:
                    usleep_ms(150)
                    self.app.pair_done()
                    self.app.set_sh(0)
                    try: self.ble.gap_disconnect(self.conn)
                    except: pass

    # ---------- helpers ----------
    def _conn_ok(self):
        return isinstance(self.conn, int) and self.connected

    def _write_quiet(self, handle, data, prefer_response=True):
        """
        Пишем без ожидания WRITE_DONE. Если характеристика не поддерживает
        Write-with-response — используем Write-without-response.
        """
        if not self._conn_ok():
            if self.verbose: print("write skipped: no conn")
            return
        flag = 1 if prefer_response else 0
        try:
            self.ble.gattc_write(self.conn, handle, data, flag)
        except Exception as e:
            # fallback: без ответа
            try:
                self.ble.gattc_write(self.conn, handle, data, 0)
            except:
                if self.verbose: print("gattc_write failed:", e)

    def _send_handshake_fire_and_forget(self):
        if not self._h_init or not self._conn_ok():
            return
        payload = b"\x03" + self.my_name.encode("ascii")
        # если у F504 есть бит Write (0x08) — пробуем с ответом, но IRQ не ждём
        prefer_rsp = (self._init_props & 0x08) != 0
        if self.verbose:
            mode = "with-rsp" if prefer_rsp else "no-rsp"
            print("Sending handshake", f"({mode})")
        self._write_quiet(self._h_init, payload, prefer_response=prefer_rsp)

    def _photo_fire_and_forget(self):
        if not self._h_ctrl:
            raise RuntimeError("CTRL (F506) not discovered")
        if not self._conn_ok():
            if self.verbose: print("photo skipped: no conn")
            return
        prefer_rsp = (self._ctrl_props & 0x08) != 0
        self.app.set_sh(2)
        self._write_quiet(self._h_ctrl, b"\x8C", prefer_response=prefer_rsp)

    def _wait(self, cond, timeout_ms):
        t0 = time.ticks_ms()
        while not cond():
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                return False
            usleep_ms(20)
        return True

    def _save_peer(self, extra=None):
        try:
            obj = {"addr_type": int(self.peer_addr_type),
                   "addr": list(self.peer_addr)}
            if extra:
                obj.update(extra)
            
            self.sets_data.update(obj)
            with open(self.store, "w") as f:
                json.dump(self.sets_data, f)
                json.dumps(self._peer_cache)

            if self.verbose:
                print("Saved peer:", _mac_str(self.peer_addr), "type", self.peer_addr_type, "extra", extra)
        except Exception as e:
            if self.verbose: print("Save error:", e)

    def _load_peer(self):
        try:
            if self._peer_cache:
                d=json.loads(self._peer_cache)
            else:
                f=open(self.store).read()
                d = json.loads(f)
                self._peer_cache=f
            at = int(d["addr_type"])
            addr = bytes(d["addr"])
            h_init = d.get("h_init")
            h_ctrl = d.get("h_ctrl")
            print('NAME',d.get('name'))
            return at, addr, h_init, h_ctrl
        except:
            return None, None, None, None

    # ---------- API ----------
    def pair(self, timeout_ms=20000):
        """
        Скан → connect → (pair, если доступно) → discovery → отправить handshake (fire-and-forget) →
        сохранить MAC и value handles → disconnect. Не ждём WRITE_DONE.
        """
        self._mode = "pair"
        self._auto_handshake_on_discover = True
        self._disconnect_after_handshake = True

        if self.verbose: print("Scanning for Canon (F505)...")
        self.ble.gap_scan(self.scan_ms, 30000, 30000)

        if not self._wait(lambda: self.connected, timeout_ms):
            self.app.set_sh(3)
            self.app.pair_done()
            return False

        # дождёмся хотя бы discovery (handshake шлём в CHARACTERISTIC_DONE)
        self._wait(lambda: self._discovery_done, 5000)
        # немного подождать, затем разорвать
        self._wait(lambda: not self.connected, 3000)

        # адрес/handles должны быть сохранены в ENCRYPTION_UPDATE/CHAR_DONE; если нет — попробуем сейчас
        if self.peer_addr and (self.store not in os.listdir()):
            self._save_peer()
        return self.peer_addr is not None

    def show(self, timeout_ms=10000, force_handshake=False):
        """
        Подключается по сохранённому MAC, без pairing.
        Если есть сохранённый handle F506 — сразу шлёт 0x8C и отключается.
        Иначе (или при ошибке) — делает discovery и потом шлёт.
        """
        self.app.set_sh(1)
        self._mode = "show"
        self._auto_handshake_on_discover = False
        self._disconnect_after_handshake = False

        at, addr, h_init_saved, h_ctrl_saved = self._load_peer()
        if at is None or addr is None:
            self.app.set_sh(3)
            return

        if self.verbose: print("Connecting to", _mac_str(addr), "type", at)
        self.ble.gap_connect(at, addr)
        if not self._wait(lambda: self.connected, timeout_ms):
            if self.verbose: print("show(): connect timeout")
            return False

        # --- Быстрый путь: сразу писать в сохранённый handle CTRL ---
        fast_ok = False
        if isinstance(h_ctrl_saved, int) and h_ctrl_saved > 0:
            try:
                # fire-and-forget; используем без ответа, чтобы не зависеть от WRITE_DONE
                if not self._conn_ok():
                    raise OSError("no conn")
                self.app.set_sh(2)
                self.ble.gattc_write(self.conn, h_ctrl_saved, b"\x8C", 0)
                fast_ok = True
                if self.verbose: print("Shot via saved handle:", h_ctrl_saved)
            except Exception as e:
                if self.verbose: print("Fast write failed, fallback to discover:", e)

        if fast_ok:
            usleep_ms(120)
            try: self.ble.gap_disconnect(self.conn)
            except: pass
            self._wait(lambda: not self.connected, 2000)
            return True

        # --- Fallback: discovery и обычная съёмка ---
        self._svc_range = None; self._h_init = None; self._h_ctrl = None
        self._init_props = 0; self._ctrl_props = 0
        self._discovery_done = False

        # небольшая пауза + (опц.) MTU
        if not self._conn_ok():
            if self.verbose: print("show(): link lost before discovery")
            return False
        usleep_ms(200)
        try:
            self.ble.gattc_exchange_mtu(self.conn)
            usleep_ms(100)
        except:
            pass
        if not self._conn_ok():
            if self.verbose: print("show(): link lost before discovery (after MTU)")
            return False
        self.ble.gattc_discover_services(self.conn)
        if not self._wait(lambda: self._discovery_done and self._h_ctrl is not None and self._conn_ok(), 5000):
            if self.verbose: print("show(): discover timeout")
            try: self.ble.gap_disconnect(self.conn)
            except: pass
            return False

        # опциональный handshake в каждом сеансе
        if force_handshake and self._h_init:
            self._send_handshake_fire_and_forget()
            usleep_ms(100)

        # фото (fire-and-forget)
        self._photo_fire_and_forget()

        # обновим сохранённые handles, чтобы в следующий раз был быстрый путь
        try:
            self._save_peer({"h_init": self._h_init or -1, "h_ctrl": self._h_ctrl or -1})
        except:
            pass

        usleep_ms(120)
        try: self.ble.gap_disconnect(self.conn)
        except: pass
        self._wait(lambda: not self.connected, 2000)
        return True

    def disconnect(self):
        try:
            if self.connected and self.conn is not None:
                self.ble.gap_disconnect(self.conn)
        except:
            pass
        self._wait(lambda: not self.connected, 1000)
        if self.verbose: print("BLE stopped")








class App:
    def __init__(self):
        self.name='Canon'
        self.icon='canon.bmp'
        self.timer_mode=0
        self.int_mode=False
        self.sh_state=0
        self.time_to_shoot=0
        self.int_is_start=False
        
    def start(self,app):
        self.app=app
        self.timer_mode=self.app.get_set("canon_timer","int",0) 
        self.int_mode=not not self.app.get_set("canon_int","int",0)
        self.sh_state=0

        self.bt=CanonRemoteBLE(ble=app.ble,app=self,my_name=self.app.config['name'],store='apps/canon_new.json',verbose=True)
        self.app.callback_table['ok']=self.shoot
        self.app.callback_table['right']=self.minus_timer
        self.app.callback_table['left']=self.plus_timer       
        self.app.callback_table_long['left']=self.start_pair
        self.app.callback_table_long['ok']=self.change_mode
        self.draw()
        
    def start_pair(self):
        self.app.gui.waiter.start(title='Pairing...')
        self.bt.pair()
    def set_sh(self,state):
        self.sh_state=state
        self.draw()
        
    def shoot(self):
        
        if self.timer_mode==0:
            
            self.bt.show()
        else:
            if self.int_mode and (not self.int_is_start):
                self.int_is_start=True
            elif self.int_mode and self.int_is_start:
                print('DEINIT TIMER')
                self.timer.deinit()
                self.time_to_shoot=0
                self.draw()
                self.int_is_start=False
                return
                
                
            self.timer=Timer(3)
            self.time_to_shoot=self.timer_mode
            self.draw()
            self.timer.init(mode=Timer.PERIODIC, period=1000, callback=self.timer_callback)
            
    def timer_callback(self,event=None):
        self.time_to_shoot-=1
        self.draw()
        if self.time_to_shoot==0:
            if not(self.int_mode):
                self.timer.deinit()
                self.time_to_shoot=0
            else:
                self.time_to_shoot=self.timer_mode
                
            self.bt.show()
            
        
            
        
    def draw(self):
        gc.collect()
        Lcd.startWrite()
        Lcd.fillRect(0, 31,135,240-31, 0x000000)
        x = (Lcd.width() - 16) // 2-30
        y = 60
        Lcd.drawImage("apps/timer.bmp", x, y)
        Lcd.setFont(Widgets.FONTS.DejaVu18)
        Lcd.setTextColor(0xffffff, 0x000000)
        text='now' if self.timer_mode==0 else f'{self.timer_mode}s'
        Lcd.drawString(text, x+38, y+10)
        if self.int_mode:
            Lcd.setFont(Widgets.FONTS.DejaVu12)
            Lcd.setTextColor(0xffffff, 0x000000)
            w = Lcd.textWidth('intervalometer')           
            x = (125 - w) // 2+5
            y = 40                            
            Lcd.drawString('intervalometer', x, y)

        if self.sh_state==3:
            Lcd.setFont(Widgets.FONTS.DejaVu12)
            Lcd.setTextColor(0x990000, 0x000000)
            w = Lcd.textWidth('not paired')           
            x = (125 - w) // 2+5
            y = 120                           
            Lcd.drawString('not paired', x, y)
        Lcd.fillCircle(int(Lcd.width()/2), 180, 40, [0x333333,0x996600,0x339900,0x990000][self.sh_state])

        if self.time_to_shoot:
            Lcd.setFont(Widgets.FONTS.DejaVu40)
            Lcd.setTextColor(0xffffff, [0x333333,0x996600,0x339900,0x990000][self.sh_state])
            w = Lcd.textWidth(str(self.time_to_shoot))       
            x = (125 - w) // 2+6
            y = 161                         
            Lcd.drawString(str(self.time_to_shoot), x, y)
        Lcd.endWrite()
            
        gc.collect()
        
    def minus_timer(self):
        self.timer_mode-=1
        if self.timer_mode<0:self.timer_mode=0
        self.draw()
        self.app.save_set("canon_timer",self.timer_mode,"int")
        
    def plus_timer(self):
        self.timer_mode+=1
        if self.timer_mode>60:self.timer_mode=60
        self.draw()
        self.app.save_set("canon_timer",self.timer_mode,"int")
        
    def change_mode(self):
        self.int_mode=not self.int_mode
        self.draw()
        self.app.save_set("canon_int",1 if self.int_mode else 0,"int")
         
    def pair_done(self):
        self.app.gui.waiter.stop()
        self.draw()
        
        
        
    def stop(self):
        self.bt.disconnect()
        self.app.stop_app()
        self.app.gui.show_main_menu()
