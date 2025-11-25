### LOGGING ###
from logging_setup import get_logger
logger = get_logger()

### IMPORTS ###
import os
os.environ["OPENCV_LOG_LEVEL"] = "ERROR"
import cv2
from collections import deque
import threading
from enum import Enum
from datetime import datetime as dt
from datetime import timedelta, date
import json
import time
from pathlib import Path, PurePosixPath
from upload import upload_and_cleanup
import signal
import shutil
import psutil
from concurrent.futures import ThreadPoolExecutor
import glob
import math
from hud import draw_hud
from view import Viewer

### ENUMS ###
class State(Enum):
    NONE = 0
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
SKIP_DETECTION_SECONDS = config["SKIP_DETECTION_SECONDS"]
HTTP_SERVER_ENABLED = config["HTTP_SERVER_ENABLED"]
HTTP_SERVER_PORT = config["HTTP_SERVER_PORT"]
HTTP_FPS_LIMITER = config["HTTP_FPS_LIMITER"]

SHOW_MOTION_PERCENT_ON_FRAME = config["SHOW_MOTION_PERCENT_ON_FRAME"]
SHOW_STATE_ON_FRAME = config["SHOW_STATE_ON_FRAME"]
SHOW_FPS_ON_FRAME = config["SHOW_FPS_ON_FRAME"]
SHOW_CAM_NAME_ON_FRAME = config["SHOW_CAM_NAME_ON_FRAME"]
SHOW_TIMESTAMP_ON_FRAME = config["SHOW_TIMESTAMP_ON_FRAME"]

CAMERA_CONFIGS = [
    {"NAME": cam_name, **cam_config}
    for cam_name, cam_config in config.items() if cam_name.startswith("CAM")
]

CAM_COUNT = len(CAMERA_CONFIGS)

POST_EVENT_FRAMES = []
for cam_index in range(len(CAMERA_CONFIGS)):
    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
        POST_EVENT_FRAMES.append(CAMERA_CONFIGS[cam_index]["POST_MOTION_SECONDS"] * CAMERA_CONFIGS[cam_index]["FPS_LIMITER"])
    else:
        POST_EVENT_FRAMES.append(CAMERA_CONFIGS[cam_index]["POST_MOTION_SECONDS"] * CAMERA_CONFIGS[cam_index]["FPS"])

### GLOBALS ###
cap_array = [None for _ in range(CAM_COUNT)]
state_array = [State.NONE for _ in range(CAM_COUNT)]
stop_event = threading.Event()
video_upload_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_VIDEO_WRITES_AND_UPLOADS)
current_frame = [None for _ in range(CAM_COUNT)]

### FUNCTIONS ###


def get_datetime_string(shiftSeconds=None):
    if shiftSeconds != None:
        return (dt.now() + timedelta(seconds=shiftSeconds)).strftime("%Y-%m-%d_%H-%M-%S_%f")
    
    return dt.now().strftime("%Y-%m-%d_%H-%M-%S_%f")

def post_process_video(cam_index, pre_buffer_frames, motion_video_path, motion_start_datetime_string):
    """Combine pre-buffer frames with already-written motion video to create final video"""
    try:
        cam_name = CAMERA_CONFIGS[cam_index]["NAME"]
        
        logger.info(f"[{cam_name}] Combining pre-buffer with motion video ...")
        timestamp = dt.now().timestamp()
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")

        ensure_ram_dirs()

        # Create final combined video file
        file_name = f"{cam_name}_{motion_start_datetime_string}.mp4"
        full_file_path = os.path.join(VIDEO_PATH_IN_RAM, file_name)

        if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
            video_fps = CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
        else:
            video_fps = CAMERA_CONFIGS[cam_index]["FPS"]
        
        out = cv2.VideoWriter(full_file_path, fourcc, video_fps, (CAMERA_CONFIGS[cam_index]["FRAME_WIDTH"], CAMERA_CONFIGS[cam_index]["FRAME_HEIGHT"]))

        # Write pre-buffer frames first
        for frame in pre_buffer_frames:
            out.write(frame)

        # Read back and copy frames from the motion video
        if os.path.exists(motion_video_path):
            motion_cap = cv2.VideoCapture(motion_video_path)
            while True:
                ret, frame = motion_cap.read()
                if not ret:
                    break
                out.write(frame)
            motion_cap.release()
            
            # Clean up temporary motion video
            try:
                os.remove(motion_video_path)
                logger.debug(f"[{cam_name}] Removed temporary motion video: {motion_video_path}")
            except Exception as e:
                logger.warning(f"[{cam_name}] Failed to remove temporary motion video: {repr(e)}")
        else:
            logger.warning(f"[{cam_name}] Motion video file not found: {motion_video_path}")

        out.release()
        out = None

        duration_ms = (dt.now().timestamp() - timestamp) * 1000
        logger.info(f"[{cam_name}] Combined video saved as {full_file_path} ({duration_ms:.3f} ms)")

        # Handle FTP upload and local storage after video is complete
        upload_and_cleanup(cam_name, full_file_path, 
                          FTP_UPLOAD_VIDEO, SAVE_VIDEO_LOCALLY, VIDEO_PATH)
        
    except Exception as e:
        logger.error(f"[{cam_name}] Failed to process combined video {full_file_path} ({repr(e)})")
        
        # Clean up files on error
        for path in [full_file_path, motion_video_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass
            
    finally:
        logger.debug(f"[{cam_name}] Cleaning up resources in video combiner ...")
        if 'out' in locals() and out is not None:
            try:
                out.release()
            except:
                pass

def motion_percent_mog2(mog2, frame, downscale, thr_bin=200, blur_ksize=3):
    """
    Returns percentage of moving pixels (0..100) on a downscaled grayscale view.
    """
    h, w = frame.shape[:2]
    ds_w = max(1, int(round(w / downscale)))
    ds_h = max(1, int(round(h / downscale)))

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (ds_w, ds_h), interpolation=cv2.INTER_AREA)
    if blur_ksize:
        small = cv2.GaussianBlur(small, (blur_ksize, blur_ksize), 0)

    fg = mog2.apply(small, learningRate=0.01)
    _, mask = cv2.threshold(fg, thr_bin, 255, cv2.THRESH_BINARY)

    moving = cv2.countNonZero(mask)
    return (moving / float(mask.size)) * 100.0

def cam_worker(cam_index):
    cam_name = CAMERA_CONFIGS[cam_index]["NAME"]

    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
        buffer_frames = CAMERA_CONFIGS[cam_index]["PRE_MOTION_SECONDS"] * CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
    else:
        buffer_frames = CAMERA_CONFIGS[cam_index]["PRE_MOTION_SECONDS"] * CAMERA_CONFIGS[cam_index]["FPS"]

    frame_buffer = deque(maxlen = buffer_frames)
    pre_buffer_frames = []  # Store pre-buffer frames when motion starts
    video_writer = None  # Active VideoWriter during recording
    temp_video_path = None  # Path to temporary video file
    background_subtractor = cv2.createBackgroundSubtractorMOG2(
        history=80, 
        varThreshold=32, 
        detectShadows=False
    )
    post_motion_frame_count = 0
    motion_percent = 0
    previous_motion_percent = 0
    motion_frames = 0
    no_motion_frames = 0
    motion_start_datetime_string = ""
    frame_counter = 0
    first_movement_detection_timestamp = None
    state_array[cam_index] = State.DETECTING
    
    # FPS counter variables
    fps_counter = 0
    fps_frame_count = 0
    fps_last_second = int(dt.now().timestamp())

    skip_detection_timestamp = dt.now().timestamp()
    skip_detection_flag = True

    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
        frame_duration_expected = 1.0 / float(CAMERA_CONFIGS[cam_index]["FPS_LIMITER"])
        frame_timestamp = dt.now().timestamp()

    while not stop_event.is_set():
        if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
            frame_timestamp = dt.now().timestamp()

        # Measure frame capture time
        capture_start = dt.now().timestamp()
        ret, frame = cap_array[cam_index].read()
        capture_duration = (dt.now().timestamp() - capture_start) * 1000
        
        if not ret:
            logger.error(f"[{cam_name}] Empty frame")
            return
        
        frame_counter += 1
        logger.debug(f"[{cam_name}] [Frame #{frame_counter}] Frame capture ({capture_duration:.3f} ms)")

        # Calculate FPS once per second by counting frames
        current_second = int(dt.now().timestamp())
        fps_frame_count += 1
        if current_second != fps_last_second:
            fps_counter = fps_frame_count - 1  # Don't count the frame that triggered the second change
            fps_frame_count = 1  # Start new second with current frame
            fps_last_second = current_second

        # Optimize frame processing - only do motion detection on specified frames
        motion_detection_frame = frame_counter % (CAMERA_CONFIGS[cam_index]["MOTION_DETECTION_FRAME_STEP"]) == 0
        
        if motion_detection_frame:
            # Measure motion detection time
            motion_start = dt.now().timestamp()
            # thr_bin and blur_ksize are just chatgpt numbers, they work, I dont modify them
            motion_percent = motion_percent_mog2(background_subtractor, frame, downscale=CAMERA_CONFIGS[cam_index]["MOTION_DETECTION_DOWNSCALE"])
            motion_duration = (dt.now().timestamp() - motion_start) * 1000
            logger.debug(f"[{cam_name}] [Frame #{frame_counter}] Motion detection ({motion_duration:.3f} ms) -> {motion_percent:.2f}% moving")
        else:
            logger.debug(f"[{cam_name}] [Frame #{frame_counter}] Skipping motion detection")

        # draw HUD
        hud_start = dt.now().timestamp()
        current_frame[cam_index] = draw_hud(
            frame, 
            f"{state_string[state_array[cam_index]]}" if SHOW_STATE_ON_FRAME else "", 
            dt.now().strftime("%H:%M:%S.%f")[:-3] if SHOW_TIMESTAMP_ON_FRAME else "", 
            cam_name if SHOW_CAM_NAME_ON_FRAME else "", 
            f"{fps_counter}" if SHOW_FPS_ON_FRAME else "",
            f"{motion_percent:.2f}%" if SHOW_MOTION_PERCENT_ON_FRAME else "",
            ""
        )
        hud_duration = (dt.now().timestamp() - hud_start) * 1000
        
        buffer_start = dt.now().timestamp()
        frame_buffer.append(current_frame[cam_index]) # no need for .copy()
        buffer_duration = (dt.now().timestamp() - buffer_start) * 1000
        
        logger.debug(f"[{cam_name}] [Frame #{frame_counter}] HUD draw ({hud_duration:.3f} ms), Buffer append ({buffer_duration:.3f} ms)")

        if skip_detection_flag:
            if dt.now().timestamp() - skip_detection_timestamp > SKIP_DETECTION_SECONDS:
                skip_detection_flag = False
                logger.info(f"[{cam_name}] Motion detection enabled (SKIP_DETECTION_SECONDS elapsed)")

        # stabilize frame detector first
        if not skip_detection_flag: 
            # Measure motion logic processing time
            logic_start = dt.now().timestamp()
            
            # increase or reset motion_frames/no_motion_frames if needed
            if motion_percent >= CAMERA_CONFIGS[cam_index]["MOTION_DETECTION_THRESHOLD_PERCENT"] and previous_motion_percent >= CAMERA_CONFIGS[cam_index]["MOTION_DETECTION_THRESHOLD_PERCENT"]:
                motion_frames += 1
                no_motion_frames = 0 

            elif motion_percent < CAMERA_CONFIGS[cam_index]["MOTION_DETECTION_THRESHOLD_PERCENT"] and previous_motion_percent < CAMERA_CONFIGS[cam_index]["MOTION_DETECTION_THRESHOLD_PERCENT"]:
                no_motion_frames += 1 
                motion_frames = 0
                
            logic_duration = (dt.now().timestamp() - logic_start) * 1000
            logger.debug(f"[{cam_name}] [Frame #{frame_counter}] Motion logic processing ({logic_duration:.3f} ms)")

            logger.debug(f"[{cam_name}] [Frame #{frame_counter}] Motion frames -> {motion_frames}") 
            logger.debug(f"[{cam_name}] [Frame #{frame_counter}] No motion frames -> {no_motion_frames}") 

            # save current motion_percent value for next frame
            previous_motion_percent = motion_percent
            
            # Movement detected, switching into RECORDING state
            if state_array[cam_index] == State.DETECTING and motion_frames >= CAMERA_CONFIGS[cam_index]["NUMBER_OF_FRAMES_WITH_MOTION"] - 1:
                logger.info(f"[{cam_name}] Motion detected")
                no_motion_frames = 0 # prep. for no motion detection
                state_array[cam_index] = State.RECORDING
                motion_start_datetime_string = get_datetime_string()
                
                # Quick copy of pre-buffer frames (couple ms operation)
                pre_buffer_frames = list(frame_buffer)  # convert deque into list (and copy), <1ms event
                
                # Start VideoWriter immediately for streaming recording
                try:
                    writer_start_timestamp = dt.now().timestamp()
                    ensure_ram_dirs()
                    file_name = f"{cam_name}_{motion_start_datetime_string}_temp.mp4"
                    temp_video_path = os.path.join(VIDEO_PATH_IN_RAM, file_name)
                    
                    if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
                        video_fps = CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
                    else:
                        video_fps = CAMERA_CONFIGS[cam_index]["FPS"]
                    
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    video_writer = cv2.VideoWriter(
                        temp_video_path, 
                        fourcc, 
                        video_fps, 
                        (CAMERA_CONFIGS[cam_index]["FRAME_WIDTH"], CAMERA_CONFIGS[cam_index]["FRAME_HEIGHT"])
                    )
                    writer_duration_ms = (dt.now().timestamp() - writer_start_timestamp) * 1000
                    logger.info(f"[{cam_name}] Started streaming video writer: {temp_video_path} ({writer_duration_ms:.3f} ms)")
                except Exception as e:
                    logger.error(f"[{cam_name}] Failed to start video writer: {repr(e)}")
                    video_writer = None
                    temp_video_path = None
                
                first_movement_detection_timestamp = dt.now().timestamp()

            elif state_array[cam_index] == State.RECORDING:
                # Movement not detected, switching into POST_RECORDING state
                if no_motion_frames >= CAMERA_CONFIGS[cam_index]["NUMBER_OF_FRAMES_WITH_NO_MOTION"] - 1:
                    logger.info(f"[{cam_name}] Motion stopped")
                    state_array[cam_index] = State.POST_RECORDING
                    post_motion_frame_count = 0 # prep for POST_MOTION
                # Split video if movement is taking too long (to prevent excessive RAM consumption)
                elif dt.now().timestamp() - first_movement_detection_timestamp > MAX_VIDEO_LENGTH_SECONDS:
                    logger.warning(f"[{cam_name}] Max video length reached ({MAX_VIDEO_LENGTH_SECONDS} s). If the motion persists, it will simply create new video with motion.")
                    state_array[cam_index] = State.POST_RECORDING
                    post_motion_frame_count = 0 # prep for POST_MOTION
                
            # Write frames directly to video during RECORDING and POST_RECORDING
            if state_array[cam_index] == State.RECORDING or state_array[cam_index] == State.POST_RECORDING:
                if video_writer is not None:
                    try:
                        frame_write_start = dt.now().timestamp()
                        video_writer.write(current_frame[cam_index])
                        frame_write_duration_ms = (dt.now().timestamp() - frame_write_start) * 1000
                        logger.debug(f"[{cam_name}] [Frame #{frame_counter}] Frame write {frame_write_duration_ms:.3f} ms")
                    except Exception as e:
                        logger.error(f"[{cam_name}] [Frame #{frame_counter}] Failed to write frame to video: {repr(e)}")

                if state_array[cam_index] == State.POST_RECORDING:
                    post_motion_frame_count += 1
            
                    if post_motion_frame_count == POST_EVENT_FRAMES[cam_index]:
                        logger.info(f"[{cam_name}] Post motion frame count reached")

                        # Close the video writer and process the video
                        if video_writer is not None:
                            try:
                                writer_close_start = dt.now().timestamp()
                                video_writer.release()
                                video_writer = None
                                close_duration_ms = (dt.now().timestamp() - writer_close_start) * 1000
                                logger.info(f"[{cam_name}] Video writer closed ({close_duration_ms:.3f} ms)")
                            except Exception as e:
                                logger.error(f"[{cam_name}] Failed to close video writer: {repr(e)}")

                        # Submit for post-processing (merge with pre-buffer)
                        if temp_video_path:
                            video_upload_executor.submit(post_process_video, cam_index, pre_buffer_frames.copy(), temp_video_path, motion_start_datetime_string)
                        
                        # Reset state
                        previous_motion_percent = 0
                        motion_frames = 0
                        no_motion_frames = 0
                        pre_buffer_frames.clear()
                        first_movement_detection_timestamp = None
                        temp_video_path = None

                        state_array[cam_index] = State.DETECTING
                        
        # Measure FPS limiting and overall loop performance
        if CAMERA_CONFIGS[cam_index]["FPS_LIMITER"] != 0:
            frame_duration = dt.now().timestamp() - frame_timestamp
            
            if frame_duration < frame_duration_expected:
                sleep_time = frame_duration_expected - frame_duration
                logger.debug(f"[{cam_name}] [Frame #{frame_counter}] Applying FPS limiter (sleeping {sleep_time*1000:.3f} ms)")
                time.sleep(sleep_time)
            elif frame_duration > frame_duration_expected and not skip_detection_flag:
                logger.warning(f"[{cam_name}] [Frame #{frame_counter}] Frame is taking too long to process")    
        
    # Cleanup: Close video writer if still open
    if video_writer is not None:
        try:
            video_writer.release()
            logger.info(f"[{cam_name}] Video writer closed on exit")
        except Exception as e:
            logger.error(f"[{cam_name}] Failed to close video writer on exit: {repr(e)}")

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
    
    # Try camera optimizations with detailed reporting
    logger.debug(f"[{cam_name}] Adjusting buffer size ...")
    
    # Important to increase buffer size, with buffer only 1, it wont go much above 10 FPS
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1) 
    for buf_size in [1, 2, 3]:
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, buf_size)
            actual_buf = int(cap.get(cv2.CAP_PROP_BUFFERSIZE))
            logger.debug(f"[{cam_name}] Buffer size change (target: {buf_size} -> actual: {actual_buf})")

            if actual_buf != buf_size:
                break
        except:
            logger.debug(f"[{cam_name}] Buffer size change (target: {buf_size} -> actual: FAILED)")
        
    cap_array[cam_index] = cap    

    ret, frame = cap_array[cam_index].read() # fetch first frame to get things going
    
    # verify cam params
    cam_width = CAMERA_CONFIGS[cam_index]["FRAME_WIDTH"]
    cam_height = CAMERA_CONFIGS[cam_index]["FRAME_HEIGHT"]
    cam_fps = CAMERA_CONFIGS[cam_index]["FPS"]
    cam_fps_limiter = CAMERA_CONFIGS[cam_index]["FPS_LIMITER"]
    cam_motion_detection_threshold_percent = CAMERA_CONFIGS[cam_index]["MOTION_DETECTION_THRESHOLD_PERCENT"]

    # Get actual camera properties for detailed analysis
    actual_width = int(cap_array[cam_index].get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(cap_array[cam_index].get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = int(cap_array[cam_index].get(cv2.CAP_PROP_FPS))
    actual_buffer_size = int(cap_array[cam_index].get(cv2.CAP_PROP_BUFFERSIZE))
    actual_fourcc = cap_array[cam_index].get(cv2.CAP_PROP_FOURCC)
    
    # Convert fourcc back to readable format
    fourcc_str = "".join([chr((int(actual_fourcc) >> 8 * i) & 0xFF) for i in range(4)])
    
    mismatched_params_string = ""
    if cam_width != actual_width:
        mismatched_params_string += f"WIDTH(target:{cam_width} -> actual:{actual_width}), "
    if cam_height != actual_height:
        mismatched_params_string += f"HEIGHT(target:{cam_height} -> actual:{actual_height}), "
    if cam_fps != actual_fps:
        mismatched_params_string += f"FPS(target:{cam_fps} -> actual:{actual_fps}), "

    if mismatched_params_string != "":
        logger.warning(f"[{cam_name}] Parameter mismatches: {mismatched_params_string[:-2]}")
    
    logger.info(f"[{cam_name}] Settings")
    logger.info(f"[{cam_name}]   |-- Resolution: {actual_width}x{actual_height}")
    logger.info(f"[{cam_name}]   |-- Hardware FPS: {actual_fps}")
    logger.info(f"[{cam_name}]   |-- Software FPS limit: {cam_fps_limiter}")
    logger.info(f"[{cam_name}]   |-- Motion detection threshold %: {cam_motion_detection_threshold_percent}")
    logger.info(f"[{cam_name}]   |-- Format: {fourcc_str}")
    logger.info(f"[{cam_name}]   |-- Buffer: {actual_buffer_size}")

def init_cams():
    logger.info(f"[SYS] Found {CAM_COUNT} camera/-s in config")

    threads = []
    for cam_index in range(CAM_COUNT):
        t = threading.Thread(target=init_cam, args=(cam_index, ))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()   

def init_storage_in_ram():
    if os.path.isdir(VIDEO_PATH_IN_RAM):
        shutil.rmtree(VIDEO_PATH_IN_RAM)
    os.makedirs(VIDEO_PATH_IN_RAM, exist_ok=True)

def ensure_ram_dirs():
    if not os.path.isdir(VIDEO_PATH_IN_RAM):
        os.makedirs(VIDEO_PATH_IN_RAM, exist_ok=True)
        logger.warning("Video directory in RAM not found, creating new ...")
    else:
        logger.debug("Video directory in RAM found")

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

        if HTTP_SERVER_ENABLED:
            # Start viewer HTTP server (non-blocking)
            viewer = Viewer(
                current_frame=current_frame,
                cam_count=CAM_COUNT,
                camera_configs=CAMERA_CONFIGS,
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
            video_upload_executor.shutdown(wait=True)
        except Exception as e:
            logger.warning(f"[SYS] Executor shutdown issue ({repr(e)})")

        logger.info("[SYS] Cleanup completed")

    return 0

if __name__ == "__main__":
    # propagate exit code from main()
    raise SystemExit(main())