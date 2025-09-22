from M5 import *
import bluetooth as bt
from micropython import const
import time, json, os, gc
from hardware import Timer

# ---------- утилиты ----------
def format_mmss(seconds: int) -> str:
    minutes = seconds // 60
    sec = seconds % 60
    return f"{minutes:02d}:{sec:02d}"

def _mac_str(b: bytes) -> str:
    return ":".join("{:02X}".format(x) for x in b)

# ---------- IRQ, оставлены только используемые ----------
_IRQ_SCAN_RESULT = const(5)
_IRQ_SCAN_DONE = const(6)
_IRQ_PERIPHERAL_CONNECT = const(7)
_IRQ_PERIPHERAL_DISCONNECT = const(8)
_IRQ_GATTC_SERVICE_RESULT = const(9)
_IRQ_GATTC_SERVICE_DONE = const(10)
_IRQ_GATTC_CHARACTERISTIC_RESULT = const(11)
_IRQ_GATTC_CHARACTERISTIC_DONE = const(12)
_IRQ_GATTC_NOTIFY = const(18)

# ---------- UUID Insta360 ----------
BE80 = bt.UUID(0xbe80)  # service
BE81 = bt.UUID(0xbe81)  # write (commands)
BE82 = bt.UUID(0xbe82)  # notify/read (events)

# ---------- ADV helpers (минимум) ----------
_ADV_TYPE_NAME = const(0x09)
_ADV_TYPE_UUID16_COMPLETE  = const(0x03)
_ADV_TYPE_UUID32_COMPLETE  = const(0x05)
_ADV_TYPE_UUID128_COMPLETE = const(0x07)

def decode_field(payload, adv_type):
    i = 0
    out = []
    while i + 1 < len(payload):
        ln = payload[i]
        if ln == 0:
            break
        if i + 1 < len(payload) and payload[i + 1] == adv_type:
            out.append(payload[i + 2 : i + ln + 1])
        i += 1 + ln
    return out

def decode_name(payload):
    n = decode_field(payload, _ADV_TYPE_NAME)
    return str(n[0], "utf-8") if n else ""

def decode_services(payload):
    services = []
    for code in (_ADV_TYPE_UUID16_COMPLETE, _ADV_TYPE_UUID32_COMPLETE, _ADV_TYPE_UUID128_COMPLETE):
        for u in decode_field(payload, code):
            services.append(bt.UUID(u))
    return services

# ---------- протоколные константы ----------
SEQ_POS = const(10)
SEQ_MIN = const(1)
SEQ_MAX = const(254)
STATE_STANDBY = b"\x07\x00\x00\x00\x05\x00\x00"
RESP_SIG = b"\x00\x00\x04\x00\x00"

def b(*xs): return bytes(xs)

CMD = {
    "start_rec": b(0x12,0,0,0, 0x04,0,0, 0x04,0, 0x02,0xff,0,0,0x80,0,0, 0x08,0x01),
    "stop_rec" : b(0x12,0,0,0, 0x04,0,0, 0x05,0, 0x02,0xff,0,0,0x80,0,0, 0x10,0x01),
    "apply"    : b(0x10,0,0,0, 0x04,0,0, 0x0f,0, 0x02,0xff,0,0,0x80,0,0),
    "set_photo"     : bytes([0x3f,0,0,0, 0x04,0,0, 0x03,0, 0x02,0xe3,0,0,0x80,0,0, 0x12,0x2d,0x5a,0x18,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0, 0x6a,0x02,0x30,0x01,0x9a,0x01,0x00,0xaa,0x01,0x00,0xba,0x01,0x00,0xd2,0x01,0x00,0xe2,0x02,0x00]),

}

# ---------- BLE контроллер ----------
class Insta360BLE_MP:
    def __init__(self, app=None, ble=None, store="insta360_peer.json", verbose=True, use_standby_trigger=True, auto_remember=True):
        self.ble = ble
        self.app = app
        self.ble.active(True)
        self.ble.config(gap_name='M5')
        self.ble.config(mtu=247)
        self.current_name = ""

        self.verbose = verbose
        self._scan_done = True
        self.use_standby_trigger = use_standby_trigger
        self.auto_remember = auto_remember

        self.store = store
        self._peer_cache = None   # (addr_type, addr_bytes, name)

        # runtime
        self.conn = None
        self.connected = False
        self.peer_addr_type = None
        self.peer_addr = None
        self.peer_name = None

        self._svc_range = None
        self._h_cmd = None   # BE81 value handle
        self._h_evt = None   # BE82 value handle
        self._disc_done = False

        self._found = []     # [{mac, addr_type, addr, name, rssi}]
        self._seq = 0
        self.ble.irq(self._irq)

    # ---------- persist ----------
    def _normalize_peer_obj(self, d):
        at  = int(d.get("addr_type")) if d.get("addr_type") is not None else None
        adr = bytes(d.get("addr", [])) if isinstance(d.get("addr"), list) else None
        nm  = d.get("name")
        return (at, adr, nm)

    def _load_peer(self, refresh=False):
        if (self._peer_cache is not None) and (not refresh):
            return self._peer_cache
        try:
            with open(self.store) as f:
                d = json.load(f) or {}
            self._peer_cache = self._normalize_peer_obj(d)
        except:
            self._peer_cache = (None, None, None)
        return self._peer_cache

    def _save_peer(self, at=None, addr=None, name=None):
        cur_at, cur_addr, cur_name = self._load_peer(False)
        if at   is not None: cur_at = int(at)
        if addr is not None: cur_addr = bytes(addr)
        if name is not None: cur_name = name
        obj = {}
        if cur_at   is not None: obj["addr_type"] = cur_at
        if cur_addr is not None: obj["addr"]      = list(cur_addr)
        if cur_name:             obj["name"]      = cur_name
        self.current_name = cur_name
        try:
            tmp = self.store + ".tmp"
            with open(tmp, "w") as f:
                json.dump(obj, f)
            try: os.remove(self.store)
            except: pass
            os.rename(tmp, self.store)
            self._peer_cache = self._normalize_peer_obj(obj)
            if self.verbose and cur_addr:
                print("Saved last:", _mac_str(cur_addr), cur_name or "")
        except Exception as e:
            if self.verbose:
                print("Save error:", e)

    def remember_current(self):
        if self.peer_addr:
            self._save_peer(self.peer_addr_type, self.peer_addr, self.peer_name)

    def forget_last(self):
        try: os.remove(self.store)
        except: pass
        self._peer_cache = (None, None, None)
        if self.verbose:
            print("Forgot last camera")

    # ---------- IRQ ----------
    def _irq(self, event, data):
        if event == _IRQ_SCAN_RESULT:
            a_type, a, _, rssi, adv = data
            services = decode_services(adv)
            name = decode_name(adv) or ""
            if BE80 in services:
                print('find')
                mac = _mac_str(bytes(a))
                if not any(d["mac"] == mac for d in self._found):
                    self._found.append({
                        "mac": mac, "addr_type": a_type, "addr": bytes(a),
                        "name": name, "rssi": rssi
                    })

        elif event == _IRQ_SCAN_DONE:
            self._scan_done = True
            if self.verbose:
                print("scan done")

        elif event == _IRQ_PERIPHERAL_CONNECT:
            self.conn, a_type, a = data
            self.connected = True
            self.peer_addr_type = a_type
            self.peer_addr = bytes(a)
            # имя из последнего скана (если было)
            mac = _mac_str(self.peer_addr)
            nm = None
            for d in self._found:
                if d["mac"] == mac:
                    nm = d.get("name") or None
                    break
            self.peer_name = nm

            # сброс discovery
            self._svc_range = None
            self._h_cmd = None
            self._h_evt = None
            self._disc_done = False
            self._seq = 0

            if self.verbose:
                print("connected to", mac, self.peer_name or "")
            # небольшая пауза + (опц.) MTU, затем discovery
            time.sleep_ms(150)
            try:
                self.ble.gattc_exchange_mtu(self.conn)
            except:
                pass
            self.ble.gattc_discover_services(self.conn)

        elif event == _IRQ_PERIPHERAL_DISCONNECT:
            self.conn, *_ = data
            if self.verbose:
                print("disconnected")
            self.connected = False
            self.conn = None
            self.app.disconnected()

        elif event == _IRQ_GATTC_SERVICE_RESULT:
            conn, start, end, uuid = data
            if self.verbose:
                print('service:', uuid)
            if conn == self.conn and uuid == BE80:
                self._svc_range = (start, end)

        elif event == _IRQ_GATTC_SERVICE_DONE:
            if self._svc_range:
                s, e = self._svc_range
                self.ble.gattc_discover_characteristics(self.conn, s, e)
            else:
                if self.verbose:
                    print("BE80 service not found")

        elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
            conn, _defh, vh, props, uuid = data
            if conn == self.conn:
                if uuid == BE81:
                    self._h_cmd = vh
                elif uuid == BE82:
                    self._h_evt = vh

        elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
            self._disc_done = True
            if self.verbose:
                print("handles: BE81(write)=", self._h_cmd, " BE82(notify)=", self._h_evt)
            # включить notifications (CCCD = value_handle + 1)
            if self._h_evt is not None:
                try:
                    self.ble.gattc_write(self.conn, self._h_evt + 1, b"\x01\x00", 1)
                except:
                    pass
            if self.auto_remember and self.peer_addr:
                self._save_peer(self.peer_addr_type, self.peer_addr, self.peer_name)

        elif event == _IRQ_GATTC_NOTIFY:
            conn, value_handle, payload = data
            if conn == self.conn and value_handle == self._h_evt:
                self._on_notify(payload)

    # ---------- notify parsing ----------
    def _on_notify(self, data: bytes):
        # распознаём по сигнатуре и коду 0x10 (флаг в байте 17)
        if len(data) >= 18 and data[2:7] == RESP_SIG:
            code = data[7]
            flag = data[17]
            if code == 0x10 and flag > 0:
                self.app.rec_start()
                if self.verbose:
                    print("[notify] REC START")
            elif code == 0x10 and flag == 0:
                self.app.rec_stop()
                if self.verbose:
                    print("[notify] REC STOP")
            elif code == 0xF4 and self.verbose:
                print("[notify] BUSY")
            elif data[0] == 0x22 and self.verbose:
                print("[notify] CMD ERROR")
            elif self.verbose:
                print("[notify] code=0x%02X len=%d" % (code, len(data)))
        else:
            # сырые можно включить при отладке
            # if self.verbose: print("[notify raw]", data)
            pass

    # ---------- helpers ----------
    def _wait(self, cond, timeout_ms):
        t0 = time.ticks_ms()
        while not cond():
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                return False
            time.sleep_ms(10)
        return True

    def _ensure_ready(self):
        if not (self.connected and isinstance(self.conn, int)):
            raise RuntimeError("Not connected")
        if self._h_cmd is None:
            raise RuntimeError("BE81 handle not discovered")

    def _with_seq(self, payload: bytes) -> bytearray:
        arr = bytearray(payload)
        self._seq = (self._seq + 1) if self._seq < SEQ_MAX else SEQ_MIN
        if len(arr) > SEQ_POS:
            arr[SEQ_POS] = self._seq
        return arr

    def _chunks20(self, data: bytes):
        for i in range(0, len(data), 20):
            yield data[i:i+20]

    def _send(self, payload: bytes, trigger_standby=None):
        """
        Отправить с seq и разбиением на 20 байт.
        Все промежуточные пачки — без ответа (0), последний — с ответом (1).
        """
        self._ensure_ready()
        arr = self._with_seq(payload)
        tr = self.use_standby_trigger if (trigger_standby is None) else trigger_standby

        parts = list(self._chunks20(arr))
        if tr and len(parts) > 1:
            try:
                gc.collect()
                self.ble.gattc_write(self.conn, self._h_cmd, STATE_STANDBY, 0)
                time.sleep_ms(2)
            except:
                if self.verbose:
                    print("[send] standby failed")

        if len(parts) == 1:
            # короткая команда — сразу с ответом
            self.ble.gattc_write(self.conn, self._h_cmd, parts[0], 1)
            return

        # длинная команда: очередью, последний — с rsp
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            mode = 1 if is_last else 0
            try:
                self.ble.gattc_write(self.conn, self._h_cmd, part, mode)
            except Exception as e:
                if self.verbose:
                    print("[send] write failed:", e)
            if not is_last:
                time.sleep_ms(20)

    # ---------- scan/connect ----------
    def scan(self, scan_ms=5000, interval_us=30000, window_us=30000):
        """Сканировать Insta360. Возвращает список словарей: mac,name,rssi,addr_type,addr."""
        self._found = []
        self._scan_done = False
        if self.verbose:
            print("scanning...")
        self.ble.gap_scan(scan_ms, interval_us, window_us)
        self._wait(lambda: self._scan_done, scan_ms + 200)
        self._found.sort(key=lambda d: d["rssi"], reverse=True)
        if self.verbose and self._found:
            for i, d in enumerate(self._found):
                print("[{}] {}  RSSI {:>4}  {}".format(i, d["mac"], d["rssi"], d["name"] or ""))
        return list(self._found)

    def connect_last(self, timeout_ms=6000):
        at, adr, nm = self._load_peer()
        self.current_name = nm
        if at is None or adr is None:
            if self.verbose:
                print("No saved camera")
            return False
        if self.verbose:
            print("connecting to last:", _mac_str(adr), nm or "")
        self.ble.gap_connect(at, adr)
        if not self._wait(lambda: self.connected, timeout_ms):
            if self.verbose:
                print("connect timeout")
            return False
        return self._wait(lambda: self._disc_done and (self._h_cmd is not None), 5000)

    def connect_by_mac(self, mac_str, scan_ms=5000, timeout_ms=6000):
        lst = self.scan(scan_ms)
        target = None
        for d in lst:
            if d["mac"].upper() == mac_str.upper():
                target = d; break
        if not target:
            if self.verbose:
                print("MAC not found:", mac_str)
            return False
        if self.verbose:
            print("connecting to:", target["mac"], target["name"] or "")
        self.ble.gap_connect(target["addr_type"], target["addr"])
        if not self._wait(lambda: self.connected, timeout_ms):
            return False
        return self._wait(lambda: self._disc_done and (self._h_cmd is not None), 5000)

    def connect_by_name(self, name, scan_ms=5000, timeout_ms=6000, allow_substring=True):
        lst = self.scan(scan_ms)
        name_lc = (name or "").lower()
        target = None
        for d in lst:
            if (d.get("name") or "").lower() == name_lc:
                target = d; break
        if not target and allow_substring and name_lc:
            for d in lst:
                if name_lc in (d.get("name") or "").lower():
                    target = d; break
        if not target:
            if self.verbose:
                print("no camera matching name:", repr(name))
                for d in lst:
                    print(" -", (d.get("name") or "<no name>"), d["mac"], "RSSI", d["rssi"])
            return False
        if self.verbose:
            print("connecting to:", target["name"] or "<no name>", target["mac"])
        self.ble.gap_connect(target["addr_type"], target["addr"])
        if not self._wait(lambda: self.connected, timeout_ms):
            return False
        return self._wait(lambda: self._disc_done and (self._h_cmd is not None), 5000)

    def connect_select(self, index=0, scan_ms=5000, timeout_ms=6000):
        lst = self.scan(scan_ms)
        if not lst or index < 0 or index >= len(lst):
            if self.verbose:
                print("bad index or no cameras")
            return False
        d = lst[index]
        if self.verbose:
            print("connecting to:", d["mac"], d["name"] or "")
        self.ble.gap_connect(d["addr_type"], d["addr"])
        if not self._wait(lambda: self.connected, timeout_ms):
            return False
        return self._wait(lambda: self._disc_done and (self._h_cmd is not None), 5000)

    def disconnect(self):
        try:
            if self.conn is not None:
                self.ble.gap_disconnect(self.conn)
        except:
            pass

    # ---------- API команд ----------
    def start_rec(self):  self._send(CMD["start_rec"])
    def stop_rec(self):   self._send(CMD["stop_rec"])
    def apply(self):      self._send(CMD["apply"])
    def set_photo(self):       self._send(CMD["set_photo"])



# ---------- UI-обёртка ----------
class App:
    def __init__(self):
        self.name = 'Insta360'
        self.icon = 'insta360.bmp'

    def start(self, app):
        self.app = app
        self.mode = self.app.get_set("insta_mode", "int", 0)
        self.rec_time = 0
        self.command_state = 0
        self.video_state = 0
        self.connected = False
        self.bt = Insta360BLE_MP(ble=app.ble, app=self, store='apps/insta_new.json', verbose=True)

        self.app.callback_table_long['left'] = self.select_camera
        self.app.callback_table_long['ok'] = self.change_mode
        self.app.callback_table['ok'] = self.shot
        self.connect()
        self.draw()

    def change_mode(self):
        self.mode += 1
        if self.mode > 1:
            self.mode = 0
        self.draw()
        self.app.save_set("insta_mode", self.mode, "int")

    def stop(self):
        self.bt.disconnect()
        self.app.stop_app()
        self.app.gui.show_main_menu()

    def select_camera(self):
        self.app.gui.waiter.start(title='Finding...')
        lst = self.bt.scan(5000)
        self.scan_result = lst
        out = [x.get('name') or '<no name>' for x in lst]
        self.app.gui.waiter.stop()
        self.app.gui.show_list(data=out, current=0, callback=self.select_camera_result, cancel_callback=self.draw)

    def select_camera_result(self, item):
        cur = self.scan_result[item]
        self.app.gui.waiter.start(title='Connect...')
        self.bt.connect_by_mac(cur['mac'])
        self.app.gui.waiter.stop()
        self.connected = True
        self.draw()

    def disconnected(self):
        self.connected = False
        self.draw()

    def connect(self):
        self.app.gui.waiter.start(title='Connect...')
        result = self.bt.connect_last()
        self.app.gui.waiter.stop()
        self.connected = result
        self.draw()
        return result

    def shot(self):
        if not self.connected:
            if not self.connect():
                return
        if self.mode == 0:
            if self.command_state == 2:
                return
            self.command_state = 1
            self.draw()
            self.bt.set_photo()
            self.bt.apply()
        else:  # mode == 1
            self.command_state = 1
            self.draw()
            if self.video_state == 0:
                self.video_state = 1
                self.bt.start_rec()
            else:
                self.video_state = 0
                self.bt.stop_rec()

    def rec_start(self):
        self.rec_time = time.time()
        self.timer = Timer(3)
        self.timer.init(mode=Timer.PERIODIC, period=1000, callback=self.draw)
        self.command_state = 2
        self.draw()

    def rec_stop(self):
        try:
            self.timer.deinit()
        except:
            pass
        self.command_state = 0
        self.draw()

    def draw(self, event=None):
        if not event:
            Lcd.fillRect(0, 31, 135, 240-31, 0x000000)
        gc.collect()
        if self.connected:
            text = self.bt.current_name or "<no name>"
            color = 0xffffff
        else:
            text = 'not connected'
            color = 0x990000

        Lcd.setFont(Widgets.FONTS.DejaVu12)
        Lcd.setTextColor(color, 0x000000)
        w = Lcd.textWidth(text)
        x = (125 - w) // 2 + 5
        y = 40
        Lcd.drawString(text, x, y)

        x = 15
        y = 200
        if self.mode == 0:
            Lcd.drawImage("apps/insta_photo.bmp", x, y)
            txt = "PHOTO"
        else:
            Lcd.drawImage("apps/insta_video.bmp", x, y)
            txt = "VIDEO"

        Lcd.setFont(Widgets.FONTS.DejaVu18)
        Lcd.setTextColor(0xffffff, 0x000000)
        x = 53
        y = 208
        Lcd.drawString(txt, x, y)

        color = [0x090909, 0x996600, 0x990000]
        Lcd.fillCircle(int(Lcd.width()/2), 150, 30, color[self.command_state])

        if self.command_state == 2:
            Lcd.setFont(Widgets.FONTS.DejaVu40)
            Lcd.setTextColor(0xffffff, 0x000000)
            txt = format_mmss(int(time.time() - self.rec_time))
            w = Lcd.textWidth(txt)
            x = (125 - w) // 2 + 5
            y = 70
            Lcd.drawString(txt, x, y)
        gc.collect()