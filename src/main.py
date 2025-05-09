# keep logging init above anything else
from logging_setup import get_logger
logger = get_logger()

import cv2
import numpy as np
from collections import deque
import threading
import os
from enum import Enum
from datetime import datetime as dt
from datetime import timedelta, date
import json
import time
import ftplib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import AnyStr
import sys
import signal

class State(Enum):
    NONE = 0
    DETECTING = 1
    RECORDING = 2
    POST_RECORDING = 3
    ERROR = 4

# CONF
with open(os.path.join(os.path.dirname((os.path.abspath(__file__))), "config.json"), "r") as f:
    config = json.load(f)

FTP_SERVER_TYPE = config["FTP_SERVER_TYPE"]
FTP_HOSTNAME = config["FTP_HOSTNAME"]
FTP_USERNAME = config["FTP_USERNAME"]
FTP_PASSWORD = config["FTP_PASSWORD"]
FTP_PATH = config["FTP_PATH"] # NOTE: this assumes FTP server is running same OS type as CatMonitoring is running on (Linux -> Linux, Windows -> Windows, ...)

VIDEO_PATH = Path(os.path.expandvars(config["VIDEO_PATH"])).expanduser() # deals with $USER and ~/...
DELETE_VIDEO = config["DELETE_VIDEO"]

BUFFER_SECONDS = config["BUFFER_SECONDS"]
POST_EVENT_SECONDS = config["POST_EVENT_SECONDS"]
NUMBER_OF_FRAMES_WITH_MOTION = config["NUMBER_OF_FRAMES_WITH_MOTION"] # recommended min. 3
NUMBER_OF_FRAMES_WITH_NO_MOTION = config["NUMBER_OF_FRAMES_WITH_NO_MOTION"] # recommended min. 3
SKIP_FIRST_FRAMES = config["SKIP_FIRST_FRAMES"]

STATUS_LED = config["STATUS_LED"]
if STATUS_LED == True:
    import RPi.GPIO as GPIO
    STATUS_LED_GPIO_PIN = config["STATUS_LED_GPIO_PIN"]

CAMERA_CONFIGS = [
    {"NAME": cam_name, **cam_config}
    for cam_name, cam_config in config.items() if cam_name.startswith("CAM")
]

CAM_COUNT = len(CAMERA_CONFIGS)

# GLOBALS
cap_array = [None for _ in range(CAM_COUNT)]

frame_buffer_array = []
for cam_index in range(CAM_COUNT):
    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
        buffer_frames = BUFFER_SECONDS * CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
    else:
        buffer_frames = BUFFER_SECONDS * CAMERA_CONFIGS[cam_index]["FPS"]

    frame_buffer_array.append(deque(maxlen = buffer_frames))

frame_buffer_copy_array = [[] for _ in range(CAM_COUNT)] 
frames_array = [[] for _ in range(CAM_COUNT)]

background_subtractor_array = [cv2.createBackgroundSubtractorMOG2(history=100, varThreshold=50) for _ in range(CAM_COUNT)]
post_motion_frame_count_array = [0 for _ in range(CAM_COUNT)]
motion_pixels_array = [0 for _ in range(CAM_COUNT)]
previous_motion_pixels_array = [0 for _ in range(CAM_COUNT)]
motion_frames_array = [0 for _ in range(CAM_COUNT)]
no_motion_frames_array = [0 for _ in range(CAM_COUNT)]
video_start_datetime_string_array = ["" for _ in range(CAM_COUNT)]
frame_counter_array = [0 for _ in range(CAM_COUNT)]
state_array = [State.DETECTING for _ in range(CAM_COUNT)]

stop_event = threading.Event()

def ftp_join_path(*parts: AnyStr) -> str:
    if FTP_SERVER_TYPE == "linux":
        PathClass = PurePosixPath
    elif FTP_SERVER_TYPE == "windows":
        PathClass = PureWindowsPath
    else:
        logger.error("[FTP] FTP_SERVER_TYPE must be linux or windows, defaulting to linux")
        PathClass = PurePosixPath

    path = PathClass(parts[0])
    for part in parts[1:]:
        path /= part
    return str(path)

def get_YYMMDD():
    return date.today().strftime("%Y%m%d")[2:]

def ftp_upload_file(full_file_path = None):
    ftp_server = ftplib.FTP(FTP_HOSTNAME, FTP_USERNAME, FTP_PASSWORD)
    ftp_server.encoding = "utf-8"

    try:
        ftpResponse = ftp_server.mkd(f"{ftp_join_path(FTP_PATH, get_YYMMDD())}") 
        logger.info("[FTP] Creating directory ...")
    except:
        pass

    ftp_full_file_path = ftp_join_path(FTP_PATH, get_YYMMDD(), os.path.basename(full_file_path))

    if(full_file_path != None):
        with open(full_file_path, "rb") as f:
            ftp_server.storbinary(f"STOR {ftp_full_file_path}", f) 
            logger.info(f"[FTP] Uploaded file as {ftp_full_file_path}")

    ftp_server.quit()

def get_datetime_string(shiftSeconds=None):
    if shiftSeconds != None:
        return (dt.now() + timedelta(seconds=shiftSeconds)).strftime("%Y-%m-%d_%H-%M-%S_%f")
    
    return dt.now().strftime("%Y-%m-%d_%H-%M-%S_%f")

def write_and_upload_video(cam_index, recorded_frames, video_start_datetime_string):
    cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

    logger.info(f"[{cam_name}] Saving video ...")
    timestamp = dt.now().timestamp()
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    file_name = f"{cam_name}_{video_start_datetime_string}.mp4"
    full_file_path = os.path.join(VIDEO_PATH, file_name)

    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
        video_fps = CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
    else:
        video_fps = CAMERA_CONFIGS[cam_index]["FPS"]
    
    out = cv2.VideoWriter(full_file_path, fourcc, video_fps, (CAMERA_CONFIGS[cam_index]["FRAME_WIDTH"], CAMERA_CONFIGS[cam_index]["FRAME_HEIGHT"]))

    for frame in frame_buffer_copy_array[cam_index]:
        out.write(frame)

    for frame in recorded_frames:
        out.write(frame)

    out.release()
    logger.info(f"[{cam_name}] Video saved as {full_file_path} ({(dt.now().timestamp() - timestamp) * 1000:.3f}ms)")

    try:
        ftp_upload_file(full_file_path)
    except Exception:
        logger.exception(f"[FTP] Failed to upload file {full_file_path}")

    if DELETE_VIDEO:
        os.remove(full_file_path)

def copy_frame_buffer(cam_index):
    cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

    logger.info(f"[{cam_name}] Copying frame buffer ...") 
    timestamp = dt.now().timestamp()
    frame_buffer_copy_array[cam_index] = list(frame_buffer_array[cam_index]) # convert deque into list (and copy)
    logger.info(f"[{cam_name}] Frame buffer copied ({(dt.now().timestamp() - timestamp) * 1000:.3f}ms)") 

def is_motion(cam_index):
    # I am doing -1, because now it needs +1 motion_frame to trigger (because of how "motion_frames += 1" is triggered) 
    if motion_pixels_array[cam_index] >= CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and motion_frames_array[cam_index] >= NUMBER_OF_FRAMES_WITH_MOTION - 1:
        no_motion_frames_array[cam_index] = 0 # prepare for no motion detection
        return True
    
    return False

def is_no_motion(cam_index):
    # I am doing -1, because now it needs +1 no_motion_frame to trigger (because of how "no_motion_frames += 1" is triggered) 
    if motion_pixels_array[cam_index] < CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and no_motion_frames_array[cam_index] >= NUMBER_OF_FRAMES_WITH_NO_MOTION - 1:
        motion_frames_array[cam_index] = 0 # prepare for motion detection
        return True
    
    return False

def cam_worker(cam_index):
    cam_name = CAMERA_CONFIGS[cam_index]["NAME"]
    frame_timestamp = dt.now().timestamp()

    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
        frame_duration_expected = 1.0 / float(CAMERA_CONFIGS[cam_index]["FPS_LIMITER"])

    while not stop_event.is_set():
        if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
            frame_timestamp = dt.now().timestamp()
        ret, frame = cap_array[cam_index].read()
        
        if not ret:
            logger.warning(f"[{cam_name}] Empty frame")
            return
        
        frame_counter_array[cam_index] += 1

        if state_array[cam_index] == State.DETECTING:
            frame_buffer_array[cam_index].append(frame.copy())

        # Detect motion
        fg_mask = background_subtractor_array[cam_index].apply(frame)
        _, thresh = cv2.threshold(fg_mask, 25, 255, cv2.THRESH_BINARY)
        motion_pixels_array[cam_index] = int(np.sum(thresh) / 255)
        logger.debug(f"[{cam_name}] Frame #{frame_counter_array[cam_index]} -> {motion_pixels_array[cam_index]} px")

        # stabilize frame detector
        if frame_counter_array[cam_index] > SKIP_FIRST_FRAMES:
            if motion_pixels_array[cam_index] >= CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and previous_motion_pixels_array[cam_index] >= CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"]:
                motion_frames_array[cam_index] += 1  

            elif motion_pixels_array[cam_index] < CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"] and previous_motion_pixels_array[cam_index] < CAMERA_CONFIGS[cam_index]["DETECTION_THRESHOLD"]:
                no_motion_frames_array[cam_index] += 1   

            previous_motion_pixels_array[cam_index] = motion_pixels_array[cam_index]

            if is_motion(cam_index) and state_array[cam_index] == State.DETECTING:
                logger.info(f"[{cam_name}] Motion detected")
                state_array[cam_index] = State.RECORDING
                
                video_start_datetime_string_array[cam_index] = get_datetime_string()
                threading.Thread(target=copy_frame_buffer, args=(cam_index,)).start()

            elif is_no_motion(cam_index) and state_array[cam_index] == State.RECORDING:
                logger.info(f"[{cam_name}] Motion stopped")
                state_array[cam_index] = State.POST_RECORDING
                post_motion_frame_count_array[cam_index] = 0
                
            if state_array[cam_index] == State.RECORDING:
                frames_array[cam_index].append(frame.copy())

            elif state_array[cam_index] == State.POST_RECORDING:
                post_motion_frame_count_array[cam_index] += 1
                frames_array[cam_index].append(frame.copy())

                # motion detected during POST_RECORDING
                if is_motion(cam_index):
                    logger.info(f"[{cam_name}] Motion detected (during POST_RECORDING state)")   
                    state_array[cam_index] = State.RECORDING 
                    continue

                # trigger finalizing video and prepare for next motion detection
                if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
                    post_event_frames = POST_EVENT_SECONDS * CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
                else:
                    post_event_frames = POST_EVENT_SECONDS * CAMERA_CONFIGS[cam_index]["FPS"]

                if post_motion_frame_count_array[cam_index] == post_event_frames:
                    logger.info(f"[{cam_name}] Post motion frame count reached")
                    frames_copy = frames_array[cam_index].copy()
                    threading.Thread(target=write_and_upload_video, args=(cam_index, frames_copy, video_start_datetime_string_array[cam_index])).start()

                    previous_motion_pixels_array[cam_index] = 0
                    motion_frames_array[cam_index] = 0
                    no_motion_frames_array[cam_index] = 0
                    frames_array[cam_index].clear()

                    state_array[cam_index] = State.DETECTING
        
        if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
            frame_duration = dt.now().timestamp() - frame_timestamp
            
            if frame_duration < frame_duration_expected:
                logger.debug(f"[{cam_name}] Delaying next frame ...")   
                time.sleep(frame_duration_expected - frame_duration)
            elif frame_duration > frame_duration_expected and frame_counter_array[cam_index] > SKIP_FIRST_FRAMES:
                logger.warning(f"[{cam_name}] Frame is taking too long to process")    
            else:
                pass

def cam_loop(cam_index):
    cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

    while 1:
        try:
            cam_worker(cam_index) 
        except Exception:
            logger.exception(f"[{cam_name}] Camera worker crashed")

        if not stop_event.is_set():
            logger.error(f"[{cam_name}] Camera worker stopped")    
        logger.info(f"[{cam_name}] Closing cv2 cap ...")

        try:
            cap_array[cam_index].release() 
            logger.info(f"[{cam_name}] Cv2 cap closed")
        except Exception:
            logger.warning(f"[{cam_name}] Cv2 cap failed to close")

        if stop_event.is_set():
            return
 
        logger.info(f"[{cam_name}] Re-opening cv2 cap in 15 seconds ...")
        time.sleep(15)
        init_cam(cam_index)


def init_cam(cam_index):
    global cap_array

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
    logger.info(f"[CAM] Found {CAM_COUNT} camera/-s in config")

    threads = []
    for cam_index in range(CAM_COUNT):
        t = threading.Thread(target=init_cam, args=(cam_index, ))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

def init_LED():
    GPIO.setmode(GPIO.BCM) 
    GPIO.setup(STATUS_LED_GPIO_PIN, GPIO.OUT)

def handle_LED():
    while not stop_event.is_set():
        motion = False

        for cam_index in range(CAM_COUNT):
            if state_array[cam_index] == State.RECORDING:
                motion = True
                break

        if motion:
            GPIO.output(STATUS_LED_GPIO_PIN, GPIO.HIGH) 
            time.sleep(0.5)
        else:
            GPIO.output(STATUS_LED_GPIO_PIN, GPIO.HIGH) 
            time.sleep(0.125)
            GPIO.output(STATUS_LED_GPIO_PIN, GPIO.LOW) 
            time.sleep(0.375)       

def main():
    os.makedirs(VIDEO_PATH, exist_ok=True)

    threads = []
    if STATUS_LED:
        led_t = None

    def shutdown(signum, frame):
        logger.info(f"[MAIN] Signal {signum} received - shutting down")
        stop_event.set()                 

        if STATUS_LED:
            led_t.join()
            logger.info("[LED] Shutdown of LED completed")       

        for cam_index in range(CAM_COUNT):
            cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

            threads[cam_index].join()
            logger.info(f"[{cam_name}] Shutdown of camera worker completed")

        GPIO.output(STATUS_LED_GPIO_PIN, GPIO.LOW)
        GPIO.cleanup()                   
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, shutdown)

    init_cams()

    for cam_index in range(CAM_COUNT):   
        cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

        logger.info(f"[{cam_name}] Starting motion detection ...")
        t = threading.Thread(target=cam_loop, args=(cam_index, ))
        t.start()
        threads.append(t)

    if STATUS_LED:
        logger.info("[LED] Init LED ...")
        init_LED()
        logger.info("[LED] Starting LED handler ...")
        led_t = threading.Thread(target=handle_LED)
        led_t.start()

    try:
        while 1:
            time.sleep(1)
    except Exception:
        logger.exception("[MAIN] Unexpected exception detected")
        shutdown(signal.SIGTERM, None)

if __name__ == "__main__":
    main()