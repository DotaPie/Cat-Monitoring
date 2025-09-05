### LOGGING ###
from logging_setup import get_logger
logger = get_logger()

### IMPORTS ###
import os
os.environ["OPENCV_LOG_LEVEL"] = "ERROR"
import cv2
import numpy as np
from collections import deque
import threading
from enum import Enum
from datetime import datetime as dt
from datetime import timedelta, date
import json
import time
import ftplib
from pathlib import Path, PurePosixPath
import sys
import signal
import shutil
import psutil
from concurrent.futures import ThreadPoolExecutor

from hud import draw_hud

### ENUMS ###
class State(Enum):
    DETECTING = 1
    RECORDING = 2
    POST_RECORDING = 3

state_string = {
    State.DETECTING:"PRE-MOTION", 
    State.RECORDING:"MOTION",
    State.POST_RECORDING:"POST-MOTION"
}

### CONF ###
with open(os.path.join(os.path.dirname((os.path.abspath(__file__))), "config.json"), "r") as f:
    config = json.load(f)

VIDEO_PATH_IN_RAM = "/dev/shm/CatMonitoring/videos"

LOGGING_LEVEL = config["LOGGING_LEVEL"]

FTP_UPLOAD_VIDEO = config["FTP_UPLOAD_VIDEO"]
FTP_HOSTNAME = config["FTP_HOSTNAME"]
FTP_USERNAME = config["FTP_USERNAME"]
FTP_PASSWORD = config["FTP_PASSWORD"]
FTP_PATH = config["FTP_PATH"]
FTP_TIMEOUT = config["FTP_TIMEOUT"]

VIDEO_PATH = Path(os.path.expandvars(config["VIDEO_PATH"])).expanduser() # deals with $USER and ~/...
SAVE_VIDEO_LOCALLY = config["SAVE_VIDEO_LOCALLY"]
MAX_CONCURRENT_VIDEO_WRITES_AND_UPLOADS = config["MAX_CONCURRENT_VIDEO_WRITES_AND_UPLOADS"]
MAX_VIDEO_LENGTH_SECONDS = config["MAX_VIDEO_LENGTH_SECONDS"]

BUFFER_SECONDS = config["BUFFER_SECONDS"]
POST_EVENT_SECONDS = config["POST_EVENT_SECONDS"]
NUMBER_OF_FRAMES_WITH_MOTION = config["NUMBER_OF_FRAMES_WITH_MOTION"] # recommended min. 3
NUMBER_OF_FRAMES_WITH_NO_MOTION = config["NUMBER_OF_FRAMES_WITH_NO_MOTION"] # recommended min. 3
SKIP_FIRST_FRAMES = config["SKIP_FIRST_FRAMES"]

STATUS_LED_RPI = config["STATUS_LED_RPI"]
if STATUS_LED_RPI:
    import RPi.GPIO as GPIO
    STATUS_LED_GPIO_PIN_RPI = config["STATUS_LED_GPIO_PIN_RPI"]

CAMERA_CONFIGS = [
    {"NAME": cam_name, **cam_config}
    for cam_name, cam_config in config.items() if cam_name.startswith("CAM")
]

CAM_COUNT = len(CAMERA_CONFIGS)

POST_EVENT_FRAMES = []
for cam_index in range(len(CAMERA_CONFIGS)):
    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
        POST_EVENT_FRAMES.append(POST_EVENT_SECONDS * CAMERA_CONFIGS[cam_index]["FPS_LIMITER"])
    else:
        POST_EVENT_FRAMES.append(POST_EVENT_SECONDS * CAMERA_CONFIGS[cam_index]["FPS"])

### GLOBALS ###
cap_array = [None for _ in range(CAM_COUNT)]
state_array = [State.DETECTING for _ in range(CAM_COUNT)]
stop_event = threading.Event()
upload_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_VIDEO_WRITES_AND_UPLOADS)

### FUNCTIONS ###
def ftp_join_path(*parts) -> str:
    return "/".join(str(p).strip("/\\") for p in parts)

def ensure_remote_dirs(ftp: ftplib.FTP, path: str) -> None:
    original_cwd = ftp.pwd()
    try:
        for part in PurePosixPath(path).parts:
            if part == "/":
                continue
            try:
                ftp.mkd(part)                     # try to create this level
            except ftplib.error_perm as e:
                if not str(e).startswith("550"):  # 550 = already exists
                    raise                        # re-raise unexpected errors
            ftp.cwd(part)                        # descend into it
    finally:
        ftp.cwd(original_cwd)                    # restore working dir

def get_YYYYMMDD():
    today = date.today()
    return today.strftime("%Y"), today.strftime("%m"), today.strftime("%d")

def ftp_upload_file(cam_name: str, full_file_path: str) -> None:
    if full_file_path is None:
        raise ValueError("full_file_path must be provided")
    
    timestamp = dt.now().timestamp()

    # --- build remote paths -------------------------------------------------
    YYYY, MM, DD = date.today().strftime("%Y %m %d").split()
    remote_dir   = ftp_join_path(FTP_PATH, YYYY, MM, DD)
    remote_file  = ftp_join_path(remote_dir, os.path.basename(full_file_path))

    # --- connect and upload -------------------------------------------------
    with ftplib.FTP(FTP_HOSTNAME, FTP_USERNAME, FTP_PASSWORD, timeout=FTP_TIMEOUT) as ftp:
        ftp.encoding = "utf-8"

        # create YYYY/MM/DD under FTP_PATH if needed
        ensure_remote_dirs(ftp, remote_dir)

        # transfer the file
        with open(full_file_path, "rb") as src:
            ftp.storbinary(f"STOR {remote_file}", src)
            logger.info(f"[{cam_name}] Uploaded {remote_file} ({(dt.now().timestamp() - timestamp):.3f} s)")

def get_datetime_string(shiftSeconds=None):
    if shiftSeconds != None:
        return (dt.now() + timedelta(seconds=shiftSeconds)).strftime("%Y-%m-%d_%H-%M-%S_%f")
    
    return dt.now().strftime("%Y-%m-%d_%H-%M-%S_%f")

def write_and_upload_video(cam_index, frame_buffer_copy, frames_copy, video_start_datetime_string):
    try:
        cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

        logger.info(f"[{cam_name}] Saving video ...")
        timestamp = dt.now().timestamp()
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")

        ensure_storage_in_ram()

        file_name = f"{cam_name}_{video_start_datetime_string}.mp4"
        full_file_path = os.path.join(VIDEO_PATH_IN_RAM, file_name)

        if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
            video_fps = CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
        else:
            video_fps = CAMERA_CONFIGS[cam_index]["FPS"]
        
        out = cv2.VideoWriter(full_file_path, fourcc, video_fps, (CAMERA_CONFIGS[cam_index]["FRAME_WIDTH"], CAMERA_CONFIGS[cam_index]["FRAME_HEIGHT"]))

        for frame in frame_buffer_copy:
            out.write(frame)

        for frame in frames_copy:
            out.write(frame)

        out.release()
        out = None

        logger.info(f"[{cam_name}] Video saved as {full_file_path} ({(dt.now().timestamp() - timestamp):.3f} s)")

        if FTP_UPLOAD_VIDEO:
            try:
                ftp_upload_file(cam_name, full_file_path)
            except Exception as e:
                logger.error(f"[{cam_name}] Failed to upload file {full_file_path} ({repr(e)})")

        if SAVE_VIDEO_LOCALLY:
            try:
                logger.info(f"[{cam_name}] Copying file {full_file_path} into {VIDEO_PATH} ...")
                shutil.copy2(full_file_path, os.path.join(VIDEO_PATH, file_name))
            except Exception as e:
                logger.error(f"[{cam_name}] Failed to save file locally {full_file_path} ({repr(e)})")
        
        logger.debug(f"[{cam_name}] Deleting file {full_file_path} ...")
        os.remove(full_file_path)

    except Exception as e:
        logger.error(f"[{cam_name}] Failed to process file {full_file_path} ({repr(e)})")
        
        if full_file_path and os.path.exists(full_file_path):
            try:
                os.remove(full_file_path)
            except:
                pass
            
    finally:
        logger.debug(f"[{cam_name}] Cleaning up resources in video writer and uploader ...")
        if out != None:
            out.release()

def is_motion(cam_index, motion_pixels, motion_frames):
    # I am doing -1, because now it needs +1 motion_frame to trigger (because of how "motion_frames += 1" is triggered) 
    if motion_pixels >= CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and motion_frames >= NUMBER_OF_FRAMES_WITH_MOTION - 1:
        return True
    
    return False

def is_no_motion(cam_index, motion_pixels, no_motion_frames):
    # I am doing -1, because now it needs +1 no_motion_frame to trigger (because of how "no_motion_frames += 1" is triggered) 
    if motion_pixels < CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and no_motion_frames >= NUMBER_OF_FRAMES_WITH_NO_MOTION - 1:
        return True
    
    return False

def cam_worker(cam_index):
    cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
        buffer_frames = BUFFER_SECONDS * CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
    else:
        buffer_frames = BUFFER_SECONDS * CAMERA_CONFIGS[cam_index]["FPS"]

    frame_buffer = deque(maxlen = buffer_frames)
    frame_buffer_copy = []
    frames = []
    background_subtractor = cv2.createBackgroundSubtractorMOG2(history=100, varThreshold=50, detectShadows=True)
    post_motion_frame_count = 0
    motion_pixels = 0
    previous_motion_pixels = 0
    motion_frames = 0
    no_motion_frames = 0
    video_start_datetime_string = ""
    frame_counter = 0
    first_movement_detection_timestamp = None

    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
        frame_duration_expected = 1.0 / float(CAMERA_CONFIGS[cam_index]["FPS_LIMITER"])
        frame_timestamp = dt.now().timestamp()

    while not stop_event.is_set():
        if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
            frame_timestamp = dt.now().timestamp()
        ret, frame = cap_array[cam_index].read()
        
        if not ret:
            logger.error(f"[{cam_name}] Empty frame")
            return
        
        frame_counter += 1

        fg_mask = background_subtractor.apply(frame, learningRate=0.01)
        fg_mask[fg_mask == 127] = 0
        _, thresh = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)

        motion_pixels = int(np.sum(thresh) / 255)
        logger.debug(f"[{cam_name}] Frame #{frame_counter} -> {motion_pixels} px")

        frame = draw_hud(frame, state_string[state_array[cam_index]], dt.now().strftime("%H:%M:%S.%f")[:-3], cam_name, "")
        frame_buffer.append(frame.copy())

        # stabilize frame detector first
        if frame_counter > SKIP_FIRST_FRAMES: 

            # increase or reset motion_frames/no_motion_frames if needed
            if motion_pixels >= CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and previous_motion_pixels >= CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"]:
                motion_frames += 1
            elif motion_pixels >= CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and previous_motion_pixels < CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"]:
                motion_frames = 0

            elif motion_pixels < CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and previous_motion_pixels < CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"]:
                no_motion_frames += 1 
            elif motion_pixels < CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and previous_motion_pixels >= CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"]:
                no_motion_frames = 0  

            # save current motion_pixels value for next frame
            previous_motion_pixels = motion_pixels
            
            # Movement detected, switching into RECORDING state
            if state_array[cam_index] == State.DETECTING and is_motion(cam_index, motion_pixels, motion_frames):
                logger.info(f"[{cam_name}] Motion detected")
                no_motion_frames = 0 # prep. for no motion detection
                state_array[cam_index] = State.RECORDING
                video_start_datetime_string = get_datetime_string()
                frame_buffer_copy = list(frame_buffer) # convert deque into list (and copy), <1ms event
                first_movement_detection_timestamp = dt.now().timestamp()

            elif state_array[cam_index] == State.RECORDING:
                # Movement not detected, switching into POST_RECORDING state
                if is_no_motion(cam_index, motion_pixels, no_motion_frames):
                    logger.info(f"[{cam_name}] Motion stopped")
                    state_array[cam_index] = State.POST_RECORDING
                    post_motion_frame_count = 0 # prep for POST_MOTION
                # Split video if movement is taking too long (to prevent excessive RAM consumption)
                elif dt.now().timestamp() - first_movement_detection_timestamp > MAX_VIDEO_LENGTH_SECONDS:
                    logger.warning(f"[{cam_name}] Max video length reached ({MAX_VIDEO_LENGTH_SECONDS}s). If the motion persists, it will simply create new video with motion.")
                    state_array[cam_index] = State.POST_RECORDING
                    post_motion_frame_count = 0 # prep for POST_MOTION
                
            if state_array[cam_index] == State.RECORDING or state_array[cam_index] == State.POST_RECORDING:
                frames.append(frame.copy())

                if state_array[cam_index] == State.POST_RECORDING:
                    post_motion_frame_count += 1
            
                    if post_motion_frame_count == POST_EVENT_FRAMES[cam_index]:
                        logger.info(f"[{cam_name}] Post motion frame count reached")

                        upload_executor.submit(write_and_upload_video, cam_index, frame_buffer_copy, frames.copy(), video_start_datetime_string)
                        
                        previous_motion_pixels = 0
                        motion_frames = 0
                        no_motion_frames = 0
                        frames.clear()
                        first_movement_detection_timestamp = None

                        state_array[cam_index] = State.DETECTING
                        
        if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
            frame_duration = dt.now().timestamp() - frame_timestamp
            
            if frame_duration < frame_duration_expected:
                logger.debug(f"[{cam_name}] Delaying next frame ...")   
                time.sleep(frame_duration_expected - frame_duration)
            elif frame_duration > frame_duration_expected and frame_counter > SKIP_FIRST_FRAMES:
                logger.warning(f"[{cam_name}] Frame is taking too long to process")    
            else:
                pass

def cam_loop(cam_index):
    cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

    while 1:
        try:
            cam_worker(cam_index) 
        except Exception as e:
            logger.error(f"[{cam_name}] Camera worker excepted ({repr(e)})")

        if not stop_event.is_set():
            logger.error(f"[{cam_name}] Camera worker stopped")  

        logger.info(f"[{cam_name}] Closing cv2 cap ...")

        try:
            cap_array[cam_index].release() 
            cap_array[cam_index] = None
        except Exception as e:
            logger.warning(f"[{cam_name}] Cv2 cap failed to close ({repr(e)})")

        if stop_event.is_set():
            return
 
        logger.info(f"[{cam_name}] Re-opening cv2 cap in 2 seconds ...")
        time.sleep(2)
        init_cam(cam_index)


def init_cam(cam_index):
    cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

    logger.info(f"[{cam_name}] Opening cap ...")
    cap = cv2.VideoCapture(CAMERA_CONFIGS[cam_index]["DEVICE_PATH"], cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_CONFIGS[cam_index]["FRAME_WIDTH"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_CONFIGS[cam_index]["FRAME_HEIGHT"])
    cap.set(cv2.CAP_PROP_FPS, CAMERA_CONFIGS[cam_index]["FPS"])
    cap_array[cam_index] = cap    

    ret, frame = cap_array[cam_index].read() # fetch first frame to get things going

    # verify cam params
    cam_width = CAMERA_CONFIGS[cam_index]["FRAME_WIDTH"]
    cam_height = CAMERA_CONFIGS[cam_index]["FRAME_HEIGHT"]
    cam_fps = CAMERA_CONFIGS[cam_index]["FPS"]
    cam_fps_limiter = CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
    cam_detection_threshold = CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"]
    
    if cam_width != cap_array[cam_index].get(cv2.CAP_PROP_FRAME_WIDTH) or cam_height != cap_array[cam_index].get(cv2.CAP_PROP_FRAME_HEIGHT) or cam_fps != cap_array[cam_index].get(cv2.CAP_PROP_FPS):
        logger.error(f"[{cam_name}] Mismatch in camera configuration")
    else:
        logger.info(f"[{cam_name}] {cam_width} x {cam_height} @ {cam_fps}({cam_fps_limiter}) | {cam_detection_threshold}")

def init_cams():
    logger.info(f"[SYS] Found {CAM_COUNT} camera/-s in config")

    threads = []
    for cam_index in range(CAM_COUNT):
        t = threading.Thread(target=init_cam, args=(cam_index, ))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

def init_LED():
    GPIO.setmode(GPIO.BCM) 
    GPIO.setup(STATUS_LED_GPIO_PIN_RPI, GPIO.OUT)

def handle_LED():
    while not stop_event.is_set():
        motion = False

        for cam_index in range(CAM_COUNT):
            if state_array[cam_index] == State.RECORDING:
                motion = True
                break

        if motion:
            GPIO.output(STATUS_LED_GPIO_PIN_RPI, GPIO.HIGH) 
            time.sleep(0.5)
        else:
            GPIO.output(STATUS_LED_GPIO_PIN_RPI, GPIO.HIGH) 
            time.sleep(0.125)
            GPIO.output(STATUS_LED_GPIO_PIN_RPI, GPIO.LOW) 
            time.sleep(0.375)       

def init_storage_in_ram():
    if os.path.isdir(VIDEO_PATH_IN_RAM):
        shutil.rmtree(VIDEO_PATH_IN_RAM)
    os.makedirs(VIDEO_PATH_IN_RAM, exist_ok=True)

def ensure_storage_in_ram():
    if not os.path.isdir(VIDEO_PATH_IN_RAM):
        os.makedirs(VIDEO_PATH_IN_RAM, exist_ok=True)
        logger.warning("Video directory in RAM not found, creating new ...")
    else:
        logger.debug("Video directory in RAM found")

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
        proc_rss_mb = mem_info.rss / (1024 * 1024)

        # System memory
        vm = psutil.virtual_memory()
        sys_used_mib = vm.used / (1024**2)

        logger.debug("[SYS] CPU")
        logger.debug(f"  |-- process: {proc_cpu_norm:.2f} %")
        logger.debug(f"  |-- system:  {system_cpu:.2f} %")

        logger.debug("[SYS] RAM")
        logger.debug(f"  |-- process: {proc_rss_mb:.2f} MB")
        logger.debug(f"  |-- system:  {sys_used_mib:.2f} MB")

def main():
    logger.info("")
    logger.info("")
    logger.info(f"[SYS] Init")
    os.makedirs(VIDEO_PATH, exist_ok=True)

    threads = []
    if STATUS_LED_RPI:
        led_t = None
    if LOGGING_LEVEL == "DEBUG":
        resource_usage_monitor_t = None

    def shutdown(signum, frame):
        logger.info(f"[SYS] Signal {signum} received ({frame}) - shutting down")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, shutdown)

    try:
        init_cams()
        init_storage_in_ram()

        for cam_index in range(CAM_COUNT):
            cam_name = CAMERA_CONFIGS[cam_index]["NAME"]
            logger.info(f"[{cam_name}] Starting motion detection ...")
            t = threading.Thread(target=cam_loop, args=(cam_index,))
            t.start()
            threads.append(t)

        if LOGGING_LEVEL == "DEBUG":
            resource_usage_monitor_t = threading.Thread(target=monitor_resources_usages)
            resource_usage_monitor_t.start()

        if STATUS_LED_RPI:
            logger.info("[SYS] Init LED ...")
            init_LED()
            logger.info("[SYS] Starting LED handler ...")
            led_t = threading.Thread(target=handle_LED)
            led_t.start()

        # main wait loop; exits when signal handler sets the event
        while not stop_event.is_set():
            time.sleep(1)

    except Exception:
        logger.exception(f"[SYS] Unexpected exception detected")

    finally:
        logger.info("[SYS] Starting thread cleanup ...")
        # stop LED thread
        if STATUS_LED_RPI:
            try:
                logger.info("[SYS] Joining and cleaning up LED handler ...")
                led_t.join()
                GPIO.output(STATUS_LED_GPIO_PIN_RPI, GPIO.LOW)
                GPIO.cleanup()
                
            except Exception as e:
                logger.warning(f"[SYS] LED cleanup issue ({repr(e)})")

        # stop RAM monitor
        if LOGGING_LEVEL == "DEBUG":
            try:
                logger.info("[SYS] Joining CPU/RAM monitoring ...")
                resource_usage_monitor_t.join()
                
            except Exception as e:
                logger.warning(f"[SYS] CPU/RAM monitor cleanup issue ({repr(e)})")

        # join cam workers
        for cam_index, t in enumerate(threads):
            cam_name = CAMERA_CONFIGS[cam_index]["NAME"]
            try:
                logger.info(f"[{cam_name}] Joining camera workerer thread ...")
                t.join()
            except Exception as e:
                logger.warning(f"[{cam_name}] Worker join issue ({repr(e)})")

        # drain writer/upload executor
        try:
            logger.info("[SYS] Finishing tasks in thread executor ...")
            upload_executor.shutdown(wait=True)
        except Exception as e:
            logger.warning(f"[SYS] Executor shutdown issue ({repr(e)})")

        logger.info("[SYS] Cleanup completed")

    return 0

if __name__ == "__main__":
    # propagate exit code from main()
    raise SystemExit(main())