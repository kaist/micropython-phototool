import gc
import vfs
import os
from flashbdev import sys_bdev, vfs_bdev
import esp32
print("PhotoTool 1.0.0")

try:
    if vfs_bdev:
        vfs.mount(vfs_bdev, "/flash")
except OSError:
    import inisetup
    inisetup.setup()
gc.collect()
gc.threshold(56 * 1024)
import micropython
import sys
micropython.alloc_emergency_exception_buf(256)
sys.path.append("/flash/libs")
os.chdir("/flash")

