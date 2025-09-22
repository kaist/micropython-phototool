import network, socket, time
import struct, json
import machine
from M5 import *
def _ip2bytes(ip: str) -> bytes:
    return struct.pack("!BBBB", *[int(x) for x in ip.split(".")])

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

    def __init__(self, ip="192.168.4.1", port=80, html_path="/index.html", fallback_html=None):
        self.ip = ip
        self.port = port
        self.html_path = html_path
        self.fallback_html = fallback_html or (
            "<!doctype html><meta charset='utf-8'>"
            "<title>Captive Portal</title>"
            "<style>body{font-family:system-ui;background:#000;color:#fff;padding:24px}</style>"
            "<h2>Captive Portal</h2>"
            "<p>Файл не найден: {path}</p>"
            "<p>Создайте <code>{path}</code> на устройстве.</p>"
        )
        self.sock = None

    # -------- файловые/JSON helpers --------

    def _default_config(self):
        # значения по умолчанию
        return {
            "name": "MyDevice",
            "brightness": 50,     # 0..100
            "autooff_min": 0,     # 0..1440
            "sound": 1            # 1=вкл, 0=выкл
        }

    def _load_config(self):
        cfg = self._default_config()
        try:
            with open("config.json", "r") as f:
                data = json.loads(f.read() or "{}")
            if isinstance(data, dict):
                cfg.update(data)
        except:
            pass
        # нормализуем типы/диапазоны
        try: cfg["name"] = str(cfg.get("name", "MyDevice"))[:32]
        except: cfg["name"] = "M5"
        try:
            b = int(cfg.get("brightness", 100))
            cfg["brightness"] = 0 if b < 0 else (100 if b > 100 else b)
        except:
            cfg["brightness"] = 50
        try:
            m = int(cfg.get("autooff_min", 5))
            cfg["autooff_min"] = 0 if m < 0 else (1440 if m > 1440 else m)
        except:
            cfg["autooff_min"] = 0
        try:
            s = cfg.get("sound", 1)
            if isinstance(s, str):
                s = 1 if s.lower() in ("1","true","on","yes","y") else 0
            cfg["sound"] = 1 if int(s) != 0 else 0
        except:
            cfg["sound"] = 1
        return cfg

    def _save_config(self, cfg: dict):
        try:
            with open("config.json", "w") as f:
                f.write(json.dumps(cfg))
            machine.reset()
            return True
        except:
            return False
        

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

    def _read_file(self):
        try:
            with open(self.html_path, "rb") as f:
                return f.read()
        except:
            html = self.fallback_html.format(path=self.html_path)
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

    def _send_302(self, conn, location=b"/"):
        try:
            hdr = (
                b"HTTP/1.1 302 Found\r\n"
                b"Location: " + location + b"\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
            conn.sendall(hdr)
        except:
            pass

    def _send_json(self, conn, obj: dict):
        try:
            body = json.dumps(obj).encode()
        except:
            body = b"{}"
        self._send_200(conn, body, mime=b"application/json")

    # --- url utils ---

    def _urldecode(self, bts: bytes) -> str:
        out = bytearray()
        i = 0
        L = len(bts)
        while i < L:
            c = bts[i]
            if c == 37 and i + 2 < L:  # '%'
                try:
                    out.append(int(bts[i+1:i+3].decode(), 16))
                    i += 3
                    continue
                except:
                    pass
            if c == 43:  # '+'
                out.append(32)
                i += 1
                continue
            out.append(c)
            i += 1
        return out.decode()

    def _parse_qs(self, qs: bytes) -> dict:
        params = {}
        if not qs:
            return params
        for part in qs.split(b"&"):
            if not part:
                continue
            if b"=" in part:
                k, v = part.split(b"=", 1)
            else:
                k, v = part, b""
            params[self._urldecode(k)] = self._urldecode(v)
        return params

    # --- request handling ---

    def _handle_conn(self, conn):
        try:
            req = conn.recv(1024)
            if not req:
                return

            line = req.split(b"\r\n", 1)[0]
            parts = line.split()
            method = parts[0] if len(parts) >= 1 else b"GET"
            raw_path = parts[1] if len(parts) >= 2 else b"/"
            path, _, qs = raw_path.partition(b"?")

            # системные проверки — отвечаем 200, чтобы форсировать captive
            if path in self.CAPTIVE_PATHS:
                if method == b"HEAD":
                    self._send_raw(conn, b"200 OK", b"")
                else:
                    self._send_200(conn, self._read_file())
                return

            # API: отдать текущую конфигурацию
            if path == b"/api/config":
                cfg = self._load_config()
                self._send_json(conn, cfg)
                return

            # Сохранение конфигурации
            if path == b"/save":
                params = self._parse_qs(qs)
                cfg = self._load_config()

                # fields
                name = params.get("name", cfg["name"]) or cfg["name"]
                cfg["name"] = str(name)[:32]

                try:
                    br = int(params.get("brightness", cfg["brightness"]))
                except:
                    br = cfg["brightness"]
                cfg["brightness"] = 0 if br < 0 else (100 if br > 100 else br)

                try:
                    ao = int(params.get("autooff_min", cfg["autooff_min"]))
                except:
                    ao = cfg["autooff_min"]
                cfg["autooff_min"] = 0 if ao < 0 else (1440 if ao > 1440 else ao)

                s = params.get("sound", None)  # чекбокс: если нет — выключено
                cfg["sound"] = 1 if (isinstance(s, str) and s.lower() in ("1","on","true","yes","y")) else 0

                ok = self._save_config(cfg)
                if not ok:
                    self._send_200(conn, b"<p>Ошибка сохранения /config.json</p>")
                    return

                self._send_302(conn, b"/")
                return

            # Любой другой путь — отдать основную страницу
            if method == b"HEAD":
                self._send_raw(conn, b"200 OK", b"")
            else:
                self._send_200(conn, self._read_file())

        finally:
            try:
                conn.close()
            except:
                pass

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
                 gw="192.168.4.1", html_path="/index.html"):
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
        self.app.ble.active(False)
        self.portal = CaptivePortal(ssid="Setup "+self.app.config['name'], html_path="apps/settings.html")
        self.portal.start(run_forever=False)
        self.app.loop_callback = self.portal.poll
        Lcd.setFont(Widgets.FONTS.DejaVu12)
        Lcd.setTextColor(0xffffff, 0x000000)
        w = Lcd.textWidth('Connect to WiFi:')           
        x = (125 - w) // 2+5
        y = 80                            
        Lcd.drawString('Connect to WiFi:', x, y)
   
        w = Lcd.textWidth("Setup "+self.app.config['name'])           
        x = (125 - w) // 2+5
        y = 100                          
        Lcd.drawString("Setup "+self.app.config['name'], x, y)

    def stop(self):
        try:
            if self.portal:
                self.portal.stop()
        except:
            pass
        self.app.loop_callback = None
        self.app.stop_app()
        self.app.gui.show_main_menu()
