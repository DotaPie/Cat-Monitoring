### LOGGING ###
from logging_setup import get_logger
logger = get_logger()

### IMPORTS ###
import os
import threading
from datetime import datetime as dt
import json
import time
from pathlib import Path
import signal
import shutil
import psutil
from concurrent.futures import ThreadPoolExecutor
import glob
from cam import CameraManager
from view import Viewer

### CONF ###
with open(os.path.join(os.path.dirname((os.path.abspath(__file__))), "config.json"), "r") as f:
    config = json.load(f)

VIDEO_PATH_IN_RAM = "/dev/shm/CatMonitoring/videos"

LOGGING_LEVEL = config["LOGGING_LEVEL"]
FTP_UPLOAD_VIDEO = config["FTP_UPLOAD_VIDEO"]
VIDEO_PATH = Path(os.path.expandvars(config["VIDEO_PATH"])).expanduser() # deals with $USER and ~/...
SAVE_VIDEO_LOCALLY = config["SAVE_VIDEO_LOCALLY"]
MAX_CONCURRENT_VIDEO_WRITES_AND_UPLOADS = config["MAX_CONCURRENT_VIDEO_WRITES_AND_UPLOADS"]
HTTP_SERVER_ENABLED = config["HTTP_SERVER_ENABLED"]
HTTP_SERVER_PORT = config["HTTP_SERVER_PORT"]
HTTP_FPS_LIMITER = config["HTTP_FPS_LIMITER"]

### GLOBALS ###
stop_event = threading.Event()

### FUNCTIONS ###

def init_storage_in_ram():
    if os.path.isdir(VIDEO_PATH_IN_RAM):
        shutil.rmtree(VIDEO_PATH_IN_RAM)
    os.makedirs(VIDEO_PATH_IN_RAM, exist_ok=True)

def read_cpu_temperature_c_generic() -> float | None:
    # 1) psutil (works on Linux, some BSD/macOS; usually empty on Windows)
    try:
        temps = psutil.sensors_temperatures(fahrenheit=False)
        if temps:
            candidates = []
            for name, entries in temps.items():
                for e in entries:
                    if e.current is None:
                        continue
                    label = (e.label or name or "").lower()
                    score = 0
                    if any(k in label for k in ("cpu", "core", "package", "soc", "arm")):
                        score += 2
                    candidates.append((score, float(e.current)))
            if candidates:
                return max(candidates)[1]
    except Exception:
        pass

    # 2) Linux sysfs fallback
    try:
        vals = []
        for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                with open(path) as f:
                    v = f.read().strip()
                if v:
                    x = float(v)
                    vals.append(x / 1000.0 if x > 1000 else x)  # some expose millidegC
            except Exception:
                continue
        if vals:
            return max(vals)  # pick hottest zone
    except Exception:
        pass

    return None

def monitor_resources_usages(sample_sec: float = 10.0):
    proc = psutil.Process(os.getpid())

    # Prime CPU counters so next calls return a delta over the interval
    proc.cpu_percent(None)
    psutil.cpu_percent(None)

    while not stop_event.is_set():
        # Block for the sample window (system CPU over the same interval)
        system_cpu = psutil.cpu_percent(interval=sample_sec)             # 0–100 * total cores
        # Now get the process CPU over that same window
        proc_cpu_total = proc.cpu_percent(None)                          # may be >100 on multi-core
        proc_cpu_norm  = proc_cpu_total / psutil.cpu_count(logical=True) # normalize to 0–100 of one core

        # Process memory
        mem_info = proc.memory_info()
        proc_rss_mb = mem_info.rss / (1024**2)

        # System memory
        vm = psutil.virtual_memory()
        sys_used_mib = vm.used / (1024**2)

        logger.debug("[SYS] CPU")
        logger.debug(f"  |-- process: {proc_cpu_norm:.2f} %")
        logger.debug(f"  |-- system:  {system_cpu:.2f} %")
        logger.debug(f"  |-- temperature:  {read_cpu_temperature_c_generic()} °C")

        logger.debug("[SYS] RAM")
        logger.debug(f"  |-- process: {proc_rss_mb:.2f} MB")
        logger.debug(f"  |-- system:  {sys_used_mib:.2f} MB")

def main():
    logger.info("")
    logger.info("")
    logger.info(f"[SYS] Init")
    os.makedirs(VIDEO_PATH, exist_ok=True)

    threads = []
    if LOGGING_LEVEL == "DEBUG":
        resource_usage_monitor_t = None

    def shutdown(signum, frame):
        logger.info(f"[SYS] Signal {signum} received ({frame}) - shutting down")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)

    # Initialize camera manager
    camera_manager = CameraManager(
        stop_event=stop_event,
        max_concurrent_workers=MAX_CONCURRENT_VIDEO_WRITES_AND_UPLOADS,
        ftp_upload_video=FTP_UPLOAD_VIDEO,
        save_video_locally=SAVE_VIDEO_LOCALLY,
        video_path=VIDEO_PATH
    )
    
    try:
        camera_manager.init_cameras()
        init_storage_in_ram()
        
        # Start camera threads
        camera_manager.start_camera_threads()

        if LOGGING_LEVEL == "DEBUG":
            resource_usage_monitor_t = threading.Thread(target=monitor_resources_usages)
            resource_usage_monitor_t.start()

        if HTTP_SERVER_ENABLED:
            # Start viewer HTTP server (non-blocking)
            viewer = Viewer(
                current_frame=camera_manager.get_current_frames(),
                cam_count=camera_manager.get_camera_count(),
                camera_configs=camera_manager.get_camera_configs(),
                stop_event=stop_event,
                host="0.0.0.0",
                port=HTTP_SERVER_PORT,
                http_fps_limit=HTTP_FPS_LIMITER
            )
            viewer.start()
            logger.info(f"[SYS] HTTP server started on 0.0.0.0:{HTTP_SERVER_PORT}")

        # main wait loop; exits when signal handler sets the event
        while not stop_event.is_set():
            time.sleep(1)

    except Exception:
        logger.exception(f"[SYS] Unexpected exception detected")

    finally:
        logger.info("[SYS] Starting thread cleanup ...")

        # stop server
        if HTTP_SERVER_ENABLED:
            viewer.stop()

        # stop RAM monitor
        if LOGGING_LEVEL == "DEBUG":
            try:
                logger.info("[SYS] Joining CPU/RAM monitoring ...")
                resource_usage_monitor_t.join()
                
            except Exception as e:
                logger.warning(f"[SYS] CPU/RAM monitor cleanup issue ({repr(e)})")

        # join cam workers
        camera_manager.join_camera_threads()

        # shutdown camera manager (including video upload executor)
        camera_manager.shutdown_executor()

        logger.info("[SYS] Cleanup completed")

    return 0

if __name__ == "__main__":
    # propagate exit code from main()
    raise SystemExit(main())