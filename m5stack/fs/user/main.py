import os, sys, io
from M5 import *
import time
import machine
import micropython
import esp32
buzzer = machine.PWM(machine.Pin(2))
buzzer.duty(0)
import ubluetooth as bt
import gc
from hardware import Timer
import json

nvs = esp32.NVS("appsets")


class Module:
    """Простейший контейнер для имитации модуля"""
    pass


def load_module(path, name):
    """Загрузить .py как модуль (совместимо с MicroPython)."""
    # 1) Чистый namespace-словарь для exec
    ns = {
        "__name__": name,
        "__file__": path,
        "__package__": None,  # если нужны relative import'ы — заполняй по месту
    }

    sys.modules[name] = ns
    with open(path, "r") as f:
        src = f.read()
    code = compile(src, path, "exec")
    exec(code, ns, ns)
    class _Mod: pass
    mod = _Mod()
    for k, v in ns.items():
        setattr(mod, k, v)

    sys.modules[name] = mod
    gc.collect()
    return mod



def load_apps(folder):
    apps = []
    for fname in os.listdir(folder):
        if fname.endswith(".py"):
            d={}
            d['name'] = fname[:-3]
            d['path']=folder + "/" + fname
            d['icon']=fname[:-3]+'.bmp'
            apps.append(d)
    return apps


#135x240
class Colors:
    bg=0x000000
    main=0xffffff
color=Colors()

    
def play_tone(freq, duration):
    buzzer.freq(freq)
    buzzer.duty(512)
    time.sleep_ms(duration)
    buzzer.duty(0)


class Waiter:
    def __init__(self,app):
        self.app=app
        self.frames=['|', '/', '-', '\\']
        self.cur_frame=0

    def start(self,title):
        self.old_cl=self.app.callback_table
        self.old_cl_long=self.app.callback_table_long
        self.app.callback_table={'left':None,'right':None,'ok':None}
        self.app.callback_table_long={'left':None,'right':None,'ok':None}       
        Lcd.fillRect(0, 31,135,240-31, color.bg)
        x = (Lcd.width() - 32) // 2
        y = (Lcd.height() - 32) // 2
        Lcd.drawImage("apps/wait.bmp", x, y)
        Lcd.setFont(Widgets.FONTS.DejaVu18)
        Lcd.setTextColor(color.main, color.bg)
        w = Lcd.textWidth(title)       
        x = (125 - w) // 2+5                  
        y = 160                              
        Lcd.drawString(title, x, y)
        
    def stop(self):
        self.app.callback_table=self.old_cl
        self.app.callback_table_long=self.old_cl_long
        gc.collect()
        Lcd.fillRect(0, 31,135,240-31, color.bg)


        
        
        
        

class Menu:
    def __init__(self, items,app, x=5, y=40, line_height=26, max_visible=7,callback=None,cancel_callback=None):
        self.callback=callback
        self.cancel_callback=None
        Lcd.fillRect(0, 31,135,240-31, color.bg)
        self.app=app
        
        self.old_cl=self.app.callback_table
        self.old_cl_long=self.app.callback_table_long
        self.items = items
        self.cursor_index = 0
        self.scroll_offset = 0
        self.max_visible = max_visible
        self.x = x
        self.y = y
        self.line_height = line_height
        self.width = Lcd.width()-10
        self.draw()
    

    def draw(self):
        #
        Lcd.setFont(Widgets.FONTS.DejaVu24)
        for i in range(self.max_visible):
            idx = self.scroll_offset + i
            if idx >= len(self.items):
                break
            text = self.items[idx]
            y_pos = self.y + i * self.line_height

            if idx == self.cursor_index:
                # подсветка всей строки
                Lcd.fillRect(self.x, y_pos, self.width, self.line_height, color.main)  # желтый фон
                Lcd.setTextColor(color.bg, color.main)  # черный текст
                Lcd.setCursor(self.x + 5, y_pos + 2)
                Lcd.drawString(text, self.x + 5, y_pos + 2)
            else:
                # обычная строка
                Lcd.fillRect(self.x, y_pos, self.width, self.line_height, color.bg)  # фон
                Lcd.setTextColor(color.main, color.bg)  # белый текст
                Lcd.setCursor(self.x + 5, y_pos + 2)
                Lcd.drawString(text, self.x + 5, y_pos + 2)

    def up(self):
        if self.cursor_index > 0:
            self.cursor_index -= 1
            if self.cursor_index < self.scroll_offset:
                self.scroll_offset -= 1
        self.draw()


    def down(self):
        if self.cursor_index < len(self.items) - 1:
            self.cursor_index += 1
            if self.cursor_index >= self.scroll_offset + self.max_visible:
                self.scroll_offset += 1
        self.draw()
        

    def select(self): 
        self.app.callback_table=self.old_cl
        self.app.callback_table_long=self.old_cl_long
        self.callback(self.cursor_index)
        gc.collect()


class MainMenu:
    def __init__(self,apps,app):
        self.app=app
        Lcd.fillRect(0, 31,135,240-31, color.bg)
        self.apps=apps
        self.current=app.current_app
        self.draw()

    def draw(self):
        Lcd.fillRect(0, 31,135,240-31, color.bg)
        x = (Lcd.width() - 64) // 2
        y = (Lcd.height() - 64) // 2


         
        Lcd.drawImage(f"apps/{self.apps[self.current]['icon']}", x, y)

        Lcd.setFont(Widgets.FONTS.DejaVu24)
        Lcd.setTextColor(color.main, color.bg)
        text = self.apps[self.current]['name']
        w = Lcd.textWidth(text)            # ширина текста в пикселях
        x = (125 - w) // 2+5                  # центр по горизонтали
        y = 200                              # твоя высота

        Lcd.drawString(text, x, y)

    def up(self):
        self.current-=1
        if self.current<0:self.current=len(self.apps)-1
        self.draw()
        nvs.set_i32("cur_menu",self.current)
        nvs.commit()

    def down(self):
        self.current+=1
        if self.current>(len(self.apps)-1):self.current=0
        self.draw()
        nvs.set_i32("cur_menu",self.current)
        nvs.commit()

    def select(self):
        
        Lcd.fillRect(0, 31,135,240-31, color.bg)
        self.app.current_app=self.current
        gc.collect()
        self.app.gui.title_text=self.app.apps[self.current]['name']
        self.app.gui.update_title()
        mod=load_module(self.apps[self.current]['path'], 'RunCurrent')
        
        self.app.callback_table={'left':None,'right':None,'ok':None}
        self.app.callback_table_long={'right':None,'left':None,'ok':None}
        self.app.run = getattr(mod, "App")()
        self.app.run.start(self.app)
        self.app.callback_table_long['right']=self.app.run.stop
        gc.collect()




class Gui:
    def __init__(self):
        self.app=None
        Lcd.fillRect(0, 31,135,240-31, color.bg)
        self.title_text=None
        self.power_led_on=False
        self.waiter=None
        self.update_title()
 
    
    def update_title(self):
        gc.collect()
        Lcd.fillRect(5, 5, 125, 25, color.bg)
        Lcd.drawLine(5, 30, 130, 30, 0xcecece)
        Lcd.setCursor(10, 11)
        Lcd.setFont(Widgets.FONTS.DejaVu12)
        #tm=time.gmtime()
        if not self.title_text:
            try:Lcd.print(self.app.config['name'][:10], color.main)
            except:pass
        else:
            Lcd.print(self.title_text, color.main)      
        color_e=color.main if Power.getBatteryLevel()>30 else 0xff0000
        if Power.getBatteryLevel()>80:
            color_e=0x00ff00
        Lcd.fillRect(100, 8, 25, 18, color.main)
        Lcd.fillRect(97, 12, 5, 9, color.main)
        bat=int(Power.getBatteryLevel())
        Lcd.fillRect(102, 10, 21, 14, color.bg)
        lev=17-int(Power.getBatteryLevel()/100.0*17.0)
        Lcd.fillRect(104+lev, 12, 17-lev, 10, color_e)       
        if Power.getBatteryLevel()<20:
            self.power_led_on=not self.power_led_on
            if self.power_led_on:
                Power.setLed(255)
            else:
                Power.setLed(0)      
        else:
            Power.setLed(0)
        gc.collect()
            
    def show_list(self,data=[],current=0,callback=None,cancel_callback=None):
        gc.collect()
        menu=Menu(data,app=self.app,callback=callback,cancel_callback=None)
        self.app.callback_table={'left':menu.up,'right':menu.down,'ok':menu.select}
        self.app.callback_table_long={'left':None,'right':None,'ok':menu.down}

    def show_main_menu(self):
        gc.collect()
        menu=MainMenu(self.app.apps,self.app)
        self.app.callback_table={'left':menu.up,'right':menu.down,'ok':menu.select}
        self.app.callback_table_long={'left':None,'right':None,'ok':menu.down}
        
            
    

class App:
    def __init__(self):
        self.run=None
        try:self.config=json.loads(open('config.json','r').read())
        except:
            self.config={"brightness": 100, "autooff_min": 5, "name": "M5", "sound": 1}
        Widgets.setBrightness(int(self.config['brightness']/100.0*255))
        self.loop_callback=None
        self.ble = bt.BLE()
        self.ble.active(True)
        self.gui=Gui()
        self.auto_off=time.time()
        try:
            self.current_app=int(nvs.get_i32("cur_menu"))
        except:
            self.current_app=0
        self.apps =load_apps("apps")
        self.callback_table={'left':None,'right':None,'ok':None}
        self.callback_table_long={'left':None,'right':None,'ok':None}
        self.upd_time=0
        time.timezone("GMT+3")
        self.buttons_state={'ok':0,'left':0,'right':0}
        BtnA.setCallback(type=BtnA.CB_TYPE.WAS_PRESSED, cb=lambda s:self.click_h('ok',1,s))
        BtnB.setCallback(type=BtnB.CB_TYPE.WAS_PRESSED, cb=lambda s:self.click_h('left',1,s))
        BtnPWR.setCallback(type=BtnPWR.CB_TYPE.WAS_PRESSED, cb=lambda s:self.click_h('right',1,s))
        
        BtnA.setCallback(type=BtnA.CB_TYPE.WAS_RELEASED, cb=lambda s:self.click_h('ok',0,s))
        BtnB.setCallback(type=BtnB.CB_TYPE.WAS_RELEASED, cb=lambda s:self.click_h('left',0,s))
        BtnPWR.setCallback(type=BtnPWR.CB_TYPE.WAS_RELEASED, cb=lambda s:self.click_h('right',0,s))
        self.gui.app=self
        self.gui.waiter=Waiter(app=self)
        
    def save_set(self,name,data,data_type):
        if data_type=='int':
            nvs.set_i32(name,data)
        else:
            nvs.set_blob(name,data)
        nvs.commit()
    def get_set(self,name,data_type,default):
        if data_type=='int':
            try:
                return int(nvs.get_i32(name))
            except:
                return default
        else:
            try:
                return str(nvs.get_blob(name))
            except:
                return default            
            
        
        
        
    def start(self):
        self.gui.show_main_menu()
 
    def click_h(self,btn,flag,state):
        self.auto_off=time.time()
        Widgets.setBrightness(int(self.config['brightness']/100.0*255))
        if flag==1:
            self.buttons_state[btn]=time.ticks_ms()
        else:
            df=time.ticks_ms()-self.buttons_state[btn]
            self.click(btn,is_long=df>300)
            
            
    def click(self,btn,is_long):
        if not is_long:
            if self.config['sound']:
                play_tone(300,10)
            self.callback_table[btn]()
        else:
            if self.config['sound']:
                play_tone(400,10)
                time.sleep_ms(20)
                play_tone(200,10)
            self.callback_table_long[btn]()
            
    def stop_app(self):
        self.gui.waiter.start(title='wait...')
        machine.reset()
        
    def second_updater(self):
        if self.config['autooff_min'] and (time.time()-self.auto_off)>60*self.config['autooff_min']:
            Power.powerOff()
        if (time.time()-self.auto_off)>10:
            Widgets.setBrightness(16)
        self.gui.update_title()
        
    def loop(self):
        update()
        if (time.time()-self.upd_time)>=1:
            self.second_updater()
            self.upd_time=time.time()
        if self.loop_callback:
            self.loop_callback()
    
    
    
    
    
    





begin()
Widgets.setRotation(0)
Widgets.fillScreen(color.bg)
app=App()






  

if __name__ == '__main__':
  app.start()
  try:
    while True:
      app.loop()
  except (Exception, KeyboardInterrupt) as e:
    try:
      from utility import print_error_msg
      print_error_msg(e)
    except ImportError:
      print("please update to latest firmware")
