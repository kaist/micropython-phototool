# MicroPython HID (M5StickC Plus2 clicker) — RAM-optimized, no prints
# Base HID ideas from H. Groefsema (GPLv3)

import time
from machine import Pin
from micropython import const
import struct
import bluetooth
from bluetooth import UUID
from M5 import *  # Power, Lcd, Widgets
import json, binascii

# === Keycodes (use PageUp/PageDown by default) ===
KC_PGUP = const(0x4B)   # use 0x50 for ←
KC_PGDN = const(0x4E)   # use 0x4F for →

# -------- Keystore (bonding) ----------
class KeyStore(object):
    def __init__(self):
        self.secrets = {}

    def add_secret(self, t, key, value):
        self.secrets[(t, bytes(key))] = bytes(value)

    def get_secret(self, t, index, key):
        k = (t, bytes(key) if key else None)
        if key is None:
            i = 0
            for (tt, _k), _val in self.secrets.items():
                if tt == t:
                    if i == index:
                        return _val
                    i += 1
            return None
        return self.secrets.get(k, None)

    def remove_secret(self, t, key):
        del self.secrets[(t, bytes(key))]

    def has_secret(self, t, key):
        return (t, bytes(key)) in self.secrets

    def get_json_secrets(self):
        return [
            (sec_type, binascii.b2a_base64(key, newline=False), binascii.b2a_base64(value, newline=False))
            for (sec_type, key), value in self.secrets.items()
        ]

    def add_json_secrets(self, entries):
        for sec_type, key, value in entries:
            self.secrets[sec_type, binascii.a2b_base64(key)] = binascii.a2b_base64(value)

    def load_secrets(self):
        return

    def save_secrets(self):
        return


class JSONKeyStore(KeyStore):
    def load_secrets(self):
        try:
            with open("apps/clicker_keys.json", "r") as file:
                self.add_json_secrets(json.load(file))
        except:
            pass

    def save_secrets(self):
        try:
            with open("apps/clicker_keys.json", "w") as file:
                json.dump(self.get_json_secrets(), file)
        except:
            pass


F_READ = bluetooth.FLAG_READ
F_WRITE = bluetooth.FLAG_WRITE
F_READ_WRITE = bluetooth.FLAG_READ | bluetooth.FLAG_WRITE
F_READ_NOTIFY = bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY
F_READ_WRITE_NORESPONSE = bluetooth.FLAG_READ | bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE
F_READ_WRITE_NOTIFY_NORESPONSE = bluetooth.FLAG_READ | bluetooth.FLAG_WRITE | bluetooth.FLAG_NOTIFY | bluetooth.FLAG_WRITE_NO_RESPONSE

DSC_F_READ = const(0x02)

_ADV_TYPE_FLAGS = const(0x01)
_ADV_TYPE_NAME = const(0x09)
_ADV_TYPE_UUID16_COMPLETE = const(0x03)
_ADV_TYPE_UUID32_COMPLETE = const(0x05)
_ADV_TYPE_UUID128_COMPLETE = const(0x07)
_ADV_TYPE_APPEARANCE = const(0x19)

_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)
_IRQ_GATTS_READ_REQUEST = const(4)
_IRQ_MTU_EXCHANGED = const(21)
_IRQ_CONNECTION_UPDATE = const(27)
_IRQ_ENCRYPTION_UPDATE = const(28)
_IRQ_GET_SECRET = const(29)
_IRQ_SET_SECRET = const(30)
_IRQ_PASSKEY_ACTION = const(31)

_IO_CAPABILITY_NO_INPUT_OUTPUT = const(3)

_PASSKEY_ACTION_INPUT = const(2)
_PASSKEY_ACTION_DISP = const(3)
_PASSKEY_ACTION_NUMCMP = const(4)

_GATTS_NO_ERROR = const(0x00)
_GATTS_ERROR_READ_NOT_PERMITTED = const(0x02)
_GATTS_ERROR_INVALID_HANDLE = const(0x01)
_GATTS_ERROR_INSUFFICIENT_AUTHENTICATION = const(0x05)
_GATTS_ERROR_INSUFFICIENT_AUTHORIZATION = const(0x08)
_GATTS_ERROR_INSUFFICIENT_ENCRYPTION = const(0x0f)

class Advertiser:
    def __init__(self, ble, services=(UUID(0x1812),), appearance=const(960), name="Generic HID Device"):
        self._ble = ble
        self._payload = self._build_payload(name=name, services=services, appearance=appearance)
        self.advertising = False

    def _build_payload(self, limited_disc=False, br_edr=False, name=None, services=None, appearance=0):
        payload = bytearray()

        def _append(adv_type, value):
            payload.extend(struct.pack("BB", len(value) + 1, adv_type))
            payload.extend(value)

        _append(_ADV_TYPE_FLAGS, struct.pack("B", (0x01 if limited_disc else 0x02) + (0x18 if br_edr else 0x04)))
        if name:
            _append(_ADV_TYPE_NAME, name if isinstance(name, bytes) else name.encode())
        if services:
            for uuid in services:
                b = bytes(uuid)
                if len(b) == 2:
                    _append(_ADV_TYPE_UUID16_COMPLETE, b)
                elif len(b) == 4:
                    _append(_ADV_TYPE_UUID32_COMPLETE, b)
                elif len(b) == 16:
                    _append(_ADV_TYPE_UUID128_COMPLETE, b)
        if appearance:
            _append(_ADV_TYPE_APPEARANCE, struct.pack("<h", appearance))
        return bytes(payload)

    def start_advertising(self):
        if not self.advertising:
            self._ble.gap_advertise(100000, adv_data=self._payload)
            self.advertising = True

    def stop_advertising(self):
        if self.advertising:
            self._ble.gap_advertise(0, adv_data=self._payload)
            self.advertising = False


class HumanInterfaceDevice(object):
    DEVICE_STOPPED = const(0)
    DEVICE_IDLE = const(1)
    DEVICE_ADVERTISING = const(2)
    DEVICE_CONNECTED = const(3)

    def __init__(self, device_name="KB"):
        self._ble = bluetooth.BLE()
        self.adv = None
        self.device_state = HumanInterfaceDevice.DEVICE_STOPPED
        self.conn_handle = None
        self.state_change_callback = None
        self.io_capability = _IO_CAPABILITY_NO_INPUT_OUTPUT
        self.bond = True
        self.le_secure = True

        self.encrypted = False
        self.authenticated = False
        self.bonded = False
        self.key_size = 0

        self.passkey = 1234
        self.secrets = JSONKeyStore()

        self.device_name = device_name
        self.device_appearance = 960

        self.model_number = "1"
        self.serial_number = "1"
        self.firmware_revision = "1"
        self.hardware_revision = "1"
        self.software_revision = "2"
        self.manufacture_name = "Homebrew"

        self.pnp_manufacturer_source = 0x01
        self.pnp_manufacturer_uuid = 0xFFFF
        self.pnp_product_id = 0x01
        self.pnp_product_version = 0x0123

        self.battery_level = 100

        # Services
        self.DIS = (
            UUID(0x180A),
            (
                (UUID(0x2A24), F_READ),
                (UUID(0x2A25), F_READ),
                (UUID(0x2A26), F_READ),
                (UUID(0x2A27), F_READ),
                (UUID(0x2A28), F_READ),
                (UUID(0x2A29), F_READ),
                (UUID(0x2A50), F_READ),
            ),
        )
        self.BAS = (
            UUID(0x180F),
            (
                (UUID(0x2A19), F_READ_NOTIFY, ((UUID(0x2904), DSC_F_READ),)),
            ),
        )
        self.DID = (
            UUID(0x1200),
            (
                (UUID(0x0200), F_READ),
                (UUID(0x0201), F_READ),
                (UUID(0x0202), F_READ),
                (UUID(0x0203), F_READ),
                (UUID(0x0204), F_READ),
                (UUID(0x0205), F_READ),
            ),
        )

        self.h_bat = None

    def ble_irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            self.conn_handle, _, _ = data
            self.set_state(HumanInterfaceDevice.DEVICE_CONNECTED)
        elif event == _IRQ_CENTRAL_DISCONNECT:
            self.conn_handle = None
            self.set_state(HumanInterfaceDevice.DEVICE_IDLE)
            self.encrypted = False
            self.authenticated = False
            self.bonded = False
        elif event == _IRQ_GATTS_WRITE:
            return _GATTS_NO_ERROR
        elif event == _IRQ_GATTS_READ_REQUEST:
            conn_handle, _attr_handle = data
            if conn_handle != self.conn_handle:
                return _GATTS_ERROR_READ_NOT_PERMITTED
            if self.bond and not self.bonded:
                return _GATTS_ERROR_INSUFFICIENT_AUTHORIZATION
            if self.io_capability != _IO_CAPABILITY_NO_INPUT_OUTPUT and not self.authenticated:
                return _GATTS_ERROR_INSUFFICIENT_AUTHENTICATION
            if self.le_secure and (not self.encrypted or self.key_size < 16):
                return _GATTS_ERROR_INSUFFICIENT_ENCRYPTION
            return _GATTS_NO_ERROR
        elif event == _IRQ_MTU_EXCHANGED:
            conn_handle, mtu = data
            if conn_handle == self.conn_handle:
                self._ble.config(mtu=mtu)
        elif event == _IRQ_CONNECTION_UPDATE:
            pass
        elif event == _IRQ_ENCRYPTION_UPDATE:
            _conn, self.encrypted, self.authenticated, self.bonded, self.key_size = data
        elif event == _IRQ_PASSKEY_ACTION:
            conn_handle, action, _passkey = data
            if action == _PASSKEY_ACTION_NUMCMP:
                accept = False
                if hasattr(self, "passkey_callback") and self.passkey_callback is not None:
                    accept = self.passkey_callback()
                self._ble.gap_passkey(conn_handle, action, accept)
            elif action == _PASSKEY_ACTION_DISP:
                self._ble.gap_passkey(conn_handle, action, self.passkey)
            elif action == _PASSKEY_ACTION_INPUT:
                pk = None
                if hasattr(self, "passkey_callback") and self.passkey_callback is not None:
                    pk = self.passkey_callback()
                self._ble.gap_passkey(conn_handle, action, pk)
        elif event == _IRQ_SET_SECRET:
            sec_type, key, value = data
            if value is None:
                if self.secrets.has_secret(sec_type, key):
                    self.secrets.remove_secret(sec_type, key)
                    self.secrets.save_secrets()
                    return True
                return False
            else:
                self.secrets.add_secret(sec_type, key, value)
                self.secrets.save_secrets()
                return True
        elif event == _IRQ_GET_SECRET:
            sec_type, index, key = data
            return self.secrets.get_secret(sec_type, index, key)

    def start(self):
        if self.device_state is HumanInterfaceDevice.DEVICE_STOPPED:
            self.secrets.load_secrets()
            self._ble.irq(self.ble_irq)
            self._ble.active(1)
            self._ble.config(gap_name=self.device_name)
            self._ble.config(mtu=23)
            self._ble.config(bond=self.bond)
            self._ble.config(le_secure=self.le_secure)
            self._ble.config(mitm=self.le_secure)
            self._ble.config(io=self.io_capability)
            self.set_state(HumanInterfaceDevice.DEVICE_IDLE)

    def save_service_characteristics(self, handles):
        # DIS
        (h_mod, h_ser, h_fwr, h_hwr, h_swr, h_man, h_pnp) = handles[0]
        def sp(s, n):
            return struct.pack(str(n) + "s", s.encode("UTF-8"))
        self._ble.gatts_write(h_mod, sp(self.model_number, 24))
        self._ble.gatts_write(h_ser, sp(self.serial_number, 16))
        self._ble.gatts_write(h_fwr, sp(self.firmware_revision, 8))
        self._ble.gatts_write(h_hwr, sp(self.hardware_revision, 16))
        self._ble.gatts_write(h_swr, sp(self.software_revision, 8))
        self._ble.gatts_write(h_man, sp(self.manufacture_name, 36))
        self._ble.gatts_write(h_pnp, struct.pack(">BHHH",
                                self.pnp_manufacturer_source,
                                self.pnp_manufacturer_uuid,
                                self.pnp_product_id,
                                self.pnp_product_version))
        # BAS
        (self.h_bat, h_bfmt,) = handles[1]
        self._ble.gatts_write(self.h_bat, struct.pack("<B", self.battery_level))
        self._ble.gatts_write(h_bfmt, b'\x04\x00\xad\x27\x01\x00\x00')
        # DID
        (h_sid, h_vid, h_pid, h_ver, h_rec, h_vs) = handles[2]
        self._ble.gatts_write(h_sid, b'0x0103')
        self._ble.gatts_write(h_vid, struct.pack(">H", self.pnp_manufacturer_uuid))
        self._ble.gatts_write(h_pid, struct.pack(">H", self.pnp_product_id))
        self._ble.gatts_write(h_ver, struct.pack(">H", self.pnp_product_version))
        self._ble.gatts_write(h_rec, b'0x01')
        self._ble.gatts_write(h_vs, struct.pack(">H", self.pnp_manufacturer_source))

    def stop(self):
        if self.device_state is not HumanInterfaceDevice.DEVICE_STOPPED:
            if self.device_state is HumanInterfaceDevice.DEVICE_ADVERTISING and self.adv:
                self.adv.stop_advertising()
            if self.conn_handle is not None:
                self._ble.gap_disconnect(self.conn_handle)
                self.conn_handle = None
            self._ble.active(0)
            self.set_state(HumanInterfaceDevice.DEVICE_STOPPED)

    def is_connected(self):
        return self.device_state is HumanInterfaceDevice.DEVICE_CONNECTED

    def set_state(self, state):
        self.device_state = state
        if self.state_change_callback is not None:
            self.state_change_callback()

    def set_state_change_callback(self, cb):
        self.state_change_callback = cb

    def start_advertising(self):
        if (self.device_state is not HumanInterfaceDevice.DEVICE_STOPPED and
            self.device_state is not HumanInterfaceDevice.DEVICE_ADVERTISING and
            self.adv):
            self.adv.start_advertising()
            self.set_state(HumanInterfaceDevice.DEVICE_ADVERTISING)

    def stop_advertising(self):
        if self.device_state is not HumanInterfaceDevice.DEVICE_STOPPED and self.adv:
            self.adv.stop_advertising()
            if self.device_state is not HumanInterfaceDevice.DEVICE_CONNECTED:
                self.set_state(HumanInterfaceDevice.DEVICE_IDLE)

    def set_battery_level(self, level):
        if level > 100:
            level = 100
        elif level < 0:
            level = 0
        self.battery_level = level

    def notify_battery_level(self):
        if self.is_connected() and self.h_bat is not None:
            val = struct.pack("<B", self.battery_level)
            self._ble.gatts_write(self.h_bat, val)
            self._ble.gatts_notify(self.conn_handle, self.h_bat, val)

    def notify_hid_report(self):
        pass


class Keyboard(HumanInterfaceDevice):
    HID_INPUT_REPORT = bytes((
        0x05, 0x01,
        0x09, 0x06,
        0xA1, 0x01,
        0x85, 0x01,
        0x75, 0x01, 0x95, 0x08,
        0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7,
        0x15, 0x00, 0x25, 0x01,
        0x81, 0x02,
        0x95, 0x01, 0x75, 0x08,
        0x81, 0x01,
        0x95, 0x06, 0x75, 0x08,
        0x15, 0x00, 0x25, 0x65,
        0x05, 0x07, 0x19, 0x00, 0x29, 0x65,
        0x81, 0x00,
        0xC0
    ))

    def __init__(self, name="Bluetooth Keyboard"):
        super(Keyboard, self).__init__(name)
        self.device_appearance = 961

        self.HIDS = (
            UUID(0x1812),
            (
                (UUID(0x2A4A), F_READ),
                (UUID(0x2A4B), F_READ),
                (UUID(0x2A4C), F_READ_WRITE_NORESPONSE),
                (UUID(0x2A4D), F_READ_NOTIFY, ((UUID(0x2908), DSC_F_READ),)),
                (UUID(0x2A4D), F_READ_WRITE_NOTIFY_NORESPONSE, ((UUID(0x2908), DSC_F_READ),)),
                (UUID(0x2A4E), F_READ_WRITE_NORESPONSE),
            ),
        )

        self._state_buf = bytearray(8)
        self._modifiers = 0

        self.h_rep = None
        self.h_repout = None

    def ble_irq(self, event, data):
        if event == _IRQ_GATTS_WRITE:
            conn_handle, attr_handle = data
            if attr_handle == self.h_repout:
                return _GATTS_NO_ERROR
        return super(Keyboard, self).ble_irq(event, data)

    def start(self):
        super(Keyboard, self).start()
        handles = self._ble.gatts_register_services([self.DIS, self.BAS, self.DID, self.HIDS])
        self.save_service_characteristics(handles)
        self.adv = Advertiser(self._ble, (UUID(0x1812), UUID(0x180F)), self.device_appearance, self.device_name)

    def save_service_characteristics(self, handles):
        super(Keyboard, self).save_service_characteristics(handles)
        (h_info, h_hid, h_ctrl, self.h_rep, h_d1, self.h_repout, h_d2, h_proto) = handles[3]
        self._ble.gatts_write(h_info, b"\x01\x01\x00\x00")
        self._ble.gatts_write(h_hid, self.HID_INPUT_REPORT)
        self._ble.gatts_write(h_ctrl, b"\x00")
        self._state_buf[:] = b"\x00\x00\x00\x00\x00\x00\x00\x00"
        self._ble.gatts_write(self.h_rep, self._state_buf)
        self._ble.gatts_write(h_d1, struct.pack("<BB", 1, 1))
        self._ble.gatts_write(self.h_repout, self._state_buf)
        self._ble.gatts_write(h_d2, struct.pack("<BB", 1, 2))
        self._ble.gatts_write(h_proto, b"\x01")

    def notify_hid_report(self):
        if self.is_connected() and self.h_rep is not None:
            self._ble.gatts_write(self.h_rep, self._state_buf)
            self._ble.gatts_notify(self.conn_handle, self.h_rep, self._state_buf)

    def set_modifiers(self, right_gui=0, right_alt=0, right_shift=0, right_control=0, left_gui=0, left_alt=0, left_shift=0, left_control=0):
        self._modifiers = ((right_gui << 7) | (right_alt << 6) | (right_shift << 5) | (right_control << 4) |
                           (left_gui << 3) | (left_alt << 2) | (left_shift << 1) | left_control)
        self._state_buf[0] = self._modifiers

    def set_keys(self, k0=0x00, k1=0x00, k2=0x00, k3=0x00, k4=0x00, k5=0x00):
        sb = self._state_buf
        sb[2] = k0; sb[3] = k1; sb[4] = k2; sb[5] = k3; sb[6] = k4; sb[7] = k5

    def set_kb_callback(self, kb_callback):
        pass


# ======= Device (M5StickC Plus2) with BAS notifications + STATUS callbacks =======
class Device:
    def __init__(self, name, on_status=None):
        self.on_status = on_status
        self._last_batt_sent = -1

        self.keyboard = Keyboard(name)
        self.keyboard.set_state_change_callback(self.keyboard_state_callback)
        self.keyboard.start()

        # === PERIODIC BATTERY POLL ===
        self._batt_period_ms = 30000                # раз в 30 сек; подгони как нужно
        self._next_batt_ms = time.ticks_ms()        # первый опрос сразу

        # Стартуем рекламу и показываем "waiting"
        try:
            self.keyboard.start_advertising()
        except:
            pass
        if self.on_status:
            self.on_status('waiting')

    def keyboard_state_callback(self):
        if self.keyboard.is_connected():
            # сразу отправим текущий уровень
            self._push_battery(True)
            # и через 2 сек запланируем регулярный опрос
            self._next_batt_ms = time.ticks_add(time.ticks_ms(), 2000)
            if self.on_status:
                self.on_status('connected')
        else:
            # ушли в idle/disconnected → снова рекламируемся
            try:
                self.keyboard.start_advertising()
            except:
                pass
            if self.on_status:
                self.on_status('waiting')

    def _read_battery_percent(self):
        try:
            lvl = Power.getBatteryLevel()
            if lvl is None:
                return None
            lvl = int(lvl)
            if lvl < 0:   lvl = 0
            if lvl > 100: lvl = 100
            return lvl
        except:
            return None

    def _push_battery(self, force=False):
        lvl = self._read_battery_percent()
        if lvl is None:
            return
        if force or lvl != self._last_batt_sent:
            self.keyboard.set_battery_level(lvl)
            if self.keyboard.is_connected():
                self.keyboard.notify_battery_level()
            self._last_batt_sent = lvl

    def tick(self):
        # вызывать из главного цикла (app.loop_callback)
        if not self.keyboard.is_connected():
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self._next_batt_ms) >= 0:
            self._push_battery(False)
            self._next_batt_ms = time.ticks_add(now, self._batt_period_ms)

    def _tap(self, keycode, hold_ms=35):
        if self.keyboard.is_connected():
            self.keyboard.set_keys(keycode)
            self.keyboard.set_modifiers()
            self.keyboard.notify_hid_report()
            time.sleep_ms(hold_ms)
            self.keyboard.set_keys()
            self.keyboard.set_modifiers()
            self.keyboard.notify_hid_report()
            time.sleep_ms(hold_ms)


# ======= Simple App wrapper with on-screen status =======
class App:
    def __init__(self):
        self.name = 'settings'
        self.icon = 'settings.bmp'
        self.portal = None
        self.app = None
        self._status_y = 90   # Y for status line
        self._bg = 0x000000
        self._fg = 0xffffff

    def _draw_centered(self, txt, y):
        try:
            w = Lcd.textWidth(txt)
            x = (125 - w) // 2 + 5
            Lcd.drawString(txt, x, y)
        except:
            pass

    def _clear_line(self, y, h=18):
        try:
            Lcd.fillRect(0, y-2, 160, h, self._bg)
        except:
            pass

    def set_status(self, state):
        Lcd.fillCircle(int(Lcd.width()/2), 180, 30, 0x00ff00 if state=='connected' else 0xff0000)

    def up(self):
        self.dev._tap(KC_PGUP)

    def down(self):
        self.dev._tap(KC_PGDN)

    def start(self, app):
        self.app = app
        try:
            del self.app.ble
        except:
            pass

        bt_name = self.app.config['name']
        self.dev = Device(name=bt_name, on_status=self.set_status)

        self.app.callback_table['ok'] = self.down
        self.app.callback_table_long['ok'] = self.up

        Lcd.setFont(Widgets.FONTS.DejaVu12)
        Lcd.setTextColor(self._fg, self._bg)
        Lcd.clear(self._bg)

        self.set_status('waiting')

        txt = 'BT NAME:'
        self._draw_centered(txt, 80)
        self._draw_centered(bt_name, 100)

        # === включаем периодический опрос батареи через главный цикл ===
        self.app.loop_callback = self.loop

    def loop(self):
        try:
            self.dev.tick()
        except:
            pass

    def stop(self):
        self.app.loop_callback = None
        self.app.stop_app()
