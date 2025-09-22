import network, socket, time
import struct, json
import machine
import os
from M5 import *
from apps.canon import CanonRemoteBLE
import gc
from driver.neopixel import NeoPixel



def _ip2bytes(ip: str) -> bytes:
    return struct.pack("!BBBB", *[int(x) for x in ip.split(".")])

# ===== helpers =====

def _ensure_dir(path: str):
    """Создаёт промежуточную папку для файла, если её нет (например, apps/, app/)."""
    try:
        d = path.rsplit("/", 1)[0]
        if d and d not in (".", "/"):
            try:
                os.stat(d)
            except:
                os.mkdir(d)
    except:
        pass

# --------------------------- DNS ---------------------------

class _DNSServer:
    def __init__(self, ip="192.168.4.1", port=53):
        self.ip_bytes = _ip2bytes(ip)
        self.port = port
        self.sock = None

    def start(self):
        if self.sock:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", self.port))
        s.setblocking(False)
        self.sock = s

    def stop(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

    def _build_resp(self, req):
        try:
            txid = req[0:2]
            # найти конец QNAME
            i = 12
            while i < len(req) and req[i] != 0:
                i += 1 + req[i]
            q_end = i + 5  # 0x00 + QTYPE(2) + QCLASS(2)

            # header: response, no error; QDCOUNT=1, ANCOUNT=1
            header = txid + b"\x81\x80" + b"\x00\x01" + b"\x00\x01" + b"\x00\x00" + b"\x00\x00"
            question = req[12:q_end]  # важно: без +1

            # answer: pointer to name @0x0c, TYPE=A, CLASS=IN, TTL=60, RDLENGTH=4
            answer = b"\xc0\x0c" + b"\x00\x01" + b"\x00\x01" + b"\x00\x00\x00\x3c" + b"\x00\x04" + self.ip_bytes
            return header + question + answer
        except:
            return None

    def poll(self):
        if not self.sock:
            return
        try:
            data, addr = self.sock.recvfrom(512)
        except:
            return
        resp = self._build_resp(data)
        if resp:
            try:
                self.sock.sendto(resp, addr)
            except:
                pass

# --------------------------- HTTP ---------------------------

class _HTTPServer:
    CAPTIVE_PATHS = (
        b"/generate_204", b"/gen_204",
        b"/hotspot-detect.html", b"/ncsi.txt", b"/connecttest.txt"
    )

    def __init__(self, ip="192.168.4.1", port=80, html_path="apps/settings.html", fallback_html=None):
        self.ip = ip
        self.port = port
        self.html_path = html_path
        self.fallback_html = fallback_html or (
            "<!doctype html><meta charset='utf-8'>"
            "<title>Captive Portal</title>"
            "<style>body{font-family:system-ui;background:#000;color:#fff;padding:24px}</style>"
            "<h2>Captive Portal</h2>"
            "<p>File not found: {path}</p>"
            "<p>Create <code>{path}</code> on the device.</p>"
        )
        self.sock = None

    # --- file helpers ---

    def _read_file(self):
        try:
            with open(self.html_path, "rb") as f:
                return f.read()
        except:
            html = self.fallback_html.replace("{path}", self.html_path)
            return html.encode()

    # --- send helpers ---

    def _send_raw(self, conn, status=b"200 OK", body=b"", mime=b"text/html; charset=utf-8"):
        try:
            hdr = (
                b"HTTP/1.1 " + status + b"\r\n"
                b"Content-Type: " + mime + b"\r\n"
                b"Cache-Control: no-store\r\n"
                b"Connection: close\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"\r\n"
            )
            conn.sendall(hdr)
            if body and not status.startswith(b"204"):
                conn.sendall(body)
        except:
            pass

    def _send_200(self, conn, body: bytes, mime=b"text/html; charset=utf-8"):
        self._send_raw(conn, b"200 OK", body, mime)

    def _send_json(self, conn, obj: dict):
        try:
            body = json.dumps(obj).encode()
        except:
            body = b"{}"
        self._send_200(conn, body, mime=b"application/json")

    def _send_400(self, conn, msg=b"Bad Request"):
        self._send_raw(conn, b"400 Bad Request", msg, b"text/plain; charset=utf-8")

    def _send_404(self, conn):
        self._send_raw(conn, b"404 Not Found", b"Not Found", b"text/plain; charset=utf-8")

    def _send_411(self, conn):
        self._send_raw(conn, b"411 Length Required", b"Length Required", b"text/plain; charset=utf-8")

    # --- request parsing ---

    def _read_headers(self, conn):
        """Читает заголовки до CRLFCRLF; возвращает (method, path, qs, headers:dict, rest_body:bytes)"""
        try:
            conn.settimeout(5)
        except:
            pass
        buf = b""
        head_limit = 8192
        while b"\r\n\r\n" not in buf and len(buf) < head_limit:
            chunk = conn.recv(1024)
            if not chunk:
                break
            buf += chunk
        head, sep, rest = buf.partition(b"\r\n\r\n")
        if not sep:
            return None, None, None, {}, b""
        lines = head.split(b"\r\n")
        req_line = lines[0] if lines else b"GET / HTTP/1.1"
        parts = req_line.split()
        method = parts[0] if len(parts) >= 1 else b"GET"
        raw_path = parts[1] if len(parts) >= 2 else b"/"
        path, _, qs = raw_path.partition(b"?")
        headers = {}
        for ln in lines[1:]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        return method, path, qs, headers, rest

    # --- handlers ---

    def _handle_post_img(self, conn, headers, rest):
        """/img — сохранить поток PPM в app/led.ppm (потоково, без буферизации всего файла)."""
        cl = headers.get(b"content-length", None)
        if not cl:
            self._send_411(conn); return
        try:
            total = int(cl)
        except:
            self._send_400(conn, b"Invalid Content-Length"); return

        out_path = "apps/led.ppm"
        written = 0
        print('start write led')
        try:
            os.remove(out_path)
        except:pass
        if 1:
            with open(out_path, "wb") as f:
                if rest:
                    f.write(rest)
                    written += len(rest)
                # дочитываем тело по кускам
                print(written,total)
                while written < total:
                    chunk = conn.recv(min(1024, total - written))
                    
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
            if written != total:
                self._send_400(conn, b"Incomplete body")
                return
        #except:
        #    self._send_400(conn, b"Write error")
        #    return

        self._send_json(conn, {"ok": True, "bytes": written})

    def _handle_post_settings(self, conn, headers, rest):
        """/settings — принять JSON и сохранить в apps/led_settings.json"""
        cl = headers.get(b"content-length", b"0")
        try:
            total = int(cl)
        except:
            total = 0
        body = rest or b""
        while len(body) < total:
            chunk = conn.recv(min(512, total - len(body)))
            if not chunk:
                break
            body += chunk
        try:
            data = json.loads(body or b"{}")
        except:
            self._send_400(conn, b"Invalid JSON"); return

        cfg = {
            "pxCount": max(1, int(data.get("pxCount", 64))),
            "canonMode": bool(data.get("canonMode", False)),
            "startPause": max(0, int(data.get("startPause", 0))),
        }
        try:
            out_path = "apps/led_settings.json"
            _ensure_dir(out_path)
            with open(out_path, "w") as f:
                f.write(json.dumps(cfg))
        except:
            self._send_400(conn, b"Settings write error"); return

        self._send_json(conn, {"ok": True})

    def _handle_get_settings(self, conn):
        """Необязательно: GET /settings — вернуть текущие настройки (удобно для отладки)."""
        try:
            with open("apps/led_settings.json", "r") as f:
                cfg = json.loads(f.read() or "{}")
        except:
            cfg = {"pxCount": 64, "canonMode": False, "startPause": 0}
        self._send_json(conn, cfg)

    # --- connection ---

    def _handle_conn(self, conn):
        try:
            method, path, qs, headers, rest = self._read_headers(conn)
            if method is None:
                self._send_400(conn, b"Bad request")
                return

            # captive paths
            if path in self.CAPTIVE_PATHS:
                if method == b"HEAD":
                    self._send_raw(conn, b"200 OK", b"")
                else:
                    self._send_200(conn, self._read_file())
                return

            if path == b"/img" and method == b"POST":
                self._handle_post_img(conn, headers, rest)
                return

            if path == b"/settings":
                if method == b"POST":
                    self._handle_post_settings(conn, headers, rest)
                    return
                elif method in (b"GET", b"HEAD"):
                    # опционально — можно убрать при желании
                    self._handle_get_settings(conn)
                    return

            # default: отдать страницу
            if method == b"HEAD":
                self._send_raw(conn, b"200 OK", b"")
            else:
                self._send_200(conn, self._read_file())

        finally:
            try:
                conn.close()
            except:
                pass

    # --- lifecycle ---

    def start(self):
        if self.sock:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except:
            pass
        s.bind(("0.0.0.0", self.port))
        s.listen(8)
        s.setblocking(False)
        self.sock = s

    def stop(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

    def poll(self):
        if not self.sock:
            return
        try:
            conn, addr = self.sock.accept()
        except:
            return
        self._handle_conn(conn)

# --------------------------- Facade ---------------------------

class CaptivePortal:
    def __init__(self, ssid="Camera-Setup", ip="192.168.4.1", mask="255.255.255.0",
                 gw="192.168.4.1", html_path="apps/settings.html"):
        self.ap = network.WLAN(network.AP_IF)
        self.ap.active(True)
        self.ssid = ssid
        self.ip = ip
        self.mask = mask
        self.gw = gw
        self.html_path = html_path

        self.dns = _DNSServer(ip=ip, port=53)
        self.http = _HTTPServer(ip=ip, port=80, html_path=html_path)

        self._running = False

    def _setup_ap(self):
        self.ap.active(True)
        try:
            self.ap.config(essid=self.ssid, authmode=network.AUTH_OPEN)  # открытая сеть
        except:
            self.ap.config(essid=self.ssid)
        try:
            self.ap.ifconfig((self.ip, self.mask, self.gw, self.gw))
        except:
            pass

        for _ in range(30):
            if self.ap.active():
                break
            time.sleep_ms(100)

        print("AP started:", self.ap.config("essid"), self.ap.ifconfig())

    def start(self, run_forever=True):
        if self._running:
            return
        self._setup_ap()
        self.dns.start()
        self.http.start()
        self._running = True
        print("CaptivePortal ready on http://%s/  (file: %s)" % (self.ip, self.html_path))

        if run_forever:
            try:
                while self._running:
                    self.poll()
                    time.sleep_ms(5)
            except KeyboardInterrupt:
                print("Stopping by KeyboardInterrupt")
                self.stop()

    def poll(self):
        if not self._running:
            return
        self.dns.poll()
        self.http.poll()

    def stop(self):
        self._running = False
        self.dns.stop()
        self.http.stop()
        try:
            if self.ap:
                self.ap.active(False)
        except:
            pass
        print("CaptivePortal stopped")

# --------------------------- App wrapper ---------------------------

class App:
    def __init__(self):
        self.name = 'settings'
        self.icon = 'settings.bmp'
        self.portal = None
        self.app = None

    def start(self, app):
        self.app = app
        self.last_time=0
        self.portal_running=False

        self.set_mode=0
        self.wait_ms=0
        self.lightness=100
        
        gc.collect()
        
        # HTML из apps/settings.html — это та страница, которую мы сделали
        


        self.app.callback_table['ok']=self.shoot
        self.app.callback_table['right']=self.minus
        self.app.callback_table['left']=self.plus      
        self.app.callback_table_long['left']=self.start_portal
        self.app.callback_table_long['ok']=self.change_mode

        self.draw()
   
    def start_portal(self):
        if self.portal_running:
            self.stop()
            return
        self.app.ble.active(False)
        self.portal = CaptivePortal(ssid="FrzLight "+self.app.config['name'], html_path="apps/freezlight.html")
        self.portal_running=True
        self.draw()

        self.portal.start(run_forever=False)
        self.app.loop_callback = self.portal.poll
   
    def change_mode(self):
        self.set_mode+=1
        if self.set_mode>1:self.set_mode=0
        self.draw()
    
    def plus(self):
        if self.set_mode==0:
            self.wait_ms+=1
            if self.wait_ms>100:self.wait_ms=100
        else:
            self.lightness+=10
            if self.lightness>100:self.lightness=100
        self.draw()
        
    def minus(self):
        if self.set_mode==0:
            self.wait_ms-=1
            if self.wait_ms<0:self.wait_ms=0
        else:
            self.lightness-=10
            if self.lightness<0:self.lightness=0
        self.draw()    
        
    

        
    def draw(self):
        Lcd.fillRect(0, 31,135,240-31, 0x000000)
        Lcd.setFont(Widgets.FONTS.DejaVu12)
        Lcd.setTextColor(0xffffff, 0x000000)
        if self.portal_running:
            w = Lcd.textWidth('Connect to WiFi:')
            x = (125 - w) // 2 + 5
            y = 40
            Lcd.drawString('Connect to WiFi:', x, y)

            w = Lcd.textWidth("FrzLight " + self.app.config['name'])
            x = (125 - w) // 2 + 5
            y = 60
            Lcd.drawString("FrzLight " + self.app.config['name'], x, y)
        
        Lcd.drawRect(5, 98 if self.set_mode==0 else 118, 125, 16, 0xFFFFFF)
        
        txt="Delay "+str(self.wait_ms)+" ms."
        w = Lcd.textWidth(txt)
        x = (125 - w) // 2 + 5
        y = 100
        Lcd.drawString(txt,x,y)
 
        txt="Lightness "+str(self.lightness)+"%"
        w = Lcd.textWidth(txt)
        x = (125 - w) // 2 + 5
        y = 120
        Lcd.drawString(txt,x,y)
        

        txt="Last time"
        w = Lcd.textWidth(txt)
        x = (125 - w) // 2 + 5
        y = 160
        Lcd.drawString(txt,x,y)
        Lcd.setFont(Widgets.FONTS.DejaVu24)
        
        txt=str(self.last_time)+' s' if self.last_time else '--'
        w = Lcd.textWidth(txt)
        x = (125 - w) // 2 + 5
        y = 180
        Lcd.drawString(txt,x,y)
        

        
    def set_sh(self,event):
        print(event)
        
    def shoot(self):
        if self.portal_running:
            self.stop()
            return
        try:
            sets=json.loads(open('apps/led_settings.json').read())
        except:
            sets={"startPause": 0, "pxCount": 144, "canonMode": true}
            
            
        np = NeoPixel(machine.Pin(26), sets['pxCount'])
        
        if sets['startPause']:
            for x in range(sets['startPause']):
                self.app.play_tone(220,250)
                
                time.sleep_ms(750)

        if sets['canonMode']:
            self.canon=CanonRemoteBLE(ble=self.app.ble,app=self,my_name=self.app.config['name'],store='apps/canon_new.json',verbose=True)
            time.sleep_ms(100)
            self.canon.show()
            del self.canon
        
        self.app.play_tone(330,500)
        Widgets.setBrightness(0)
        t0=time.time()
        with P16Reader("apps/led.ppm", level=self.lightness, order="GRB") as r:
            print(r.width, r.height)
            while True:
                row=r.load_next()
                if row is None: break
                np.buf=row
                np.write()
                time.sleep_ms(self.wait_ms)
                
        self.last_time=time.time()-t0
        self.app.play_tone(330,50)
        time.sleep_ms(50)
        self.app.play_tone(330,50)
        time.sleep_ms(50)       
        self.app.play_tone(330,50)                   
        Widgets.setBrightness(30)
        self.draw()

    
        
 

    def stop(self):
        try:
            if self.portal:
                self.portal.stop()
        except:
            pass
        self.app.loop_callback = None
        self.app.stop_app()
        self.app.gui.show_main_menu()
        



class P16Reader:
    """
    Формат:
      P16 <w> <h>\\n
      затем h блоков по 2*w байт (RGB565), БЕЗ '\\n' между строками.

    __init__(path, order='GRB', level=100)
      level — множитель яркости в процентах (0..100).

    load_next() -> memoryview длиной 3*w (GRB или RGB), либо None при конце.
    """

    def __init__(self, path: str, order: str = "GRB", level: int = 100):
        if order not in ("GRB", "RGB"):
            raise ValueError("order must be 'GRB' or 'RGB'")
        self._order_grb = (order == "GRB")

        # нормализуем процент
        if level is None:
            level = 100
        self._level = 0 if level < 0 else (100 if level > 100 else int(level))

        self._f = open(path, "rb")

        # --- заголовок ---
        header = self._readline_exact(self._f)
        parts = header.strip().split()
        if len(parts) != 3 or parts[0] != b"P16":
            self._f.close()
            raise ValueError("Bad P16 header")
        self.width  = int(parts[1])
        self.height = int(parts[2])

        # размеры/буферы
        self._data_off      = self._f.tell()
        self._row_in_bytes  = 2 * self.width
        self._row_out_bytes = 3 * self.width
        self._row_in  = bytearray(self._row_in_bytes)
        self._row_out = bytearray(self._row_out_bytes)

        # Базовые таблицы расширения до 8 бит
        base5 = [(v << 3) | (v >> 2) for v in range(32)]   # 5 -> 8
        base6 = [(v << 2) | (v >> 4) for v in range(64)]   # 6 -> 8
        # Масштабированные таблицы с учётом level%
        if self._level == 100:
            self._t5 = bytes(base5)
            self._t6 = bytes(base6)
        elif self._level == 0:
            self._t5 = bytes(32)
            self._t6 = bytes(64)
        else:
            L = self._level
            # целочисленное округление: (x*L + 50)//100
            self._t5 = bytes(((x * L + 50) // 100) & 0xFF for x in base5)
            self._t6 = bytes(((x * L + 50) // 100) & 0xFF for x in base6)

        self.row_index = 0

    # --- utils ---
    @staticmethod
    def _readline_exact(f):
        buf = bytearray()
        while True:
            b = f.read(1)
            if not b:
                raise OSError("EOF before newline")
            buf += b
            if b == b'\n':
                return bytes(buf)

    @staticmethod
    def _readinto_exact(f, mv, n):
        got = 0
        while got < n:
            m = f.readinto(mv[got:n])
            if not m:
                chunk = f.read(n - got)
                if not chunk:
                    raise OSError("EOF in body")
                mv[got:got+len(chunk)] = chunk
                m = len(chunk)
            got += m

    # --- fast converter ---
    @micropython.native
    def _convert_row(self, src_mv, dst_mv, w, t5, t6, order_grb):
        # src: 2*w байт (RGB565), dst: 3*w байт
        s = src_mv; d = dst_mv
        _t5 = t5; _t6 = t6
        j = 0
        si = 0
        for _ in range(w):
            hi = s[si]; lo = s[si+1]
            si += 2
            # извлекаем r5/g6/b5 без тяжёлых сдвигов:
            r5 = hi >> 3
            g6 = ((hi & 7) << 3) | (lo >> 5)
            b5 = lo & 31
            R = _t5[r5]
            G = _t6[g6]
            B = _t5[b5]
            if order_grb:
                d[j]   = G; d[j+1] = R; d[j+2] = B
            else:
                d[j]   = R; d[j+1] = G; d[j+2] = B
            j += 3

    # --- API ---
    def load_next(self):
        """Вернуть следующую строку (memoryview длиной 3*w) или None при конце."""
        if self.row_index >= self.height:
            return None
        mv_in  = memoryview(self._row_in)
        mv_out = memoryview(self._row_out)
        self._readinto_exact(self._f, mv_in, self._row_in_bytes)
        self._convert_row(mv_in, mv_out, self.width, self._t5, self._t6, self._order_grb)
        self.row_index += 1
        return mv_out

    def seek_row(self, y: int):
        if not (0 <= y < self.height):
            raise ValueError("row out of range")
        self._f.seek(self._data_off + y * self._row_in_bytes)
        self.row_index = y

    def tell_row(self) -> int:
        return self.row_index

    def close(self):
        try: self._f.close()
        except: pass

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): self.close()



