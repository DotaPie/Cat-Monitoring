# Cat Monitoring

Takes stream from N cameras. If motion is detected, video is saved locally or uploaded to FTP server (can do one or another, or both).

## Hardware requirements
    - RAM: 1GB (recommended)
    - CPU: At least performance of Raspberry PI 4 (recommended) 
        - it takes about 50% CPU usage on Raspberry PI 4, when using 2x USB 2.0 camera (640x480@10FPS)
    - GPU: not needed
    - Camera: any USB camera/-s

## OS requirements: 
    - debian based linux
    - pre-installed python3.11 or higher
    - mounted /dev/shm with at least 512MB (should be already there on most modern linux distros)
    - GUI not needed

## FTP server requirements (optional, if saving videos only locally):
    - any FTP/FTPS running server with default port 21 and default port range for FTPS
        - tested on linux (FTPS), but it should also work on windows

## Quick start
```
git clone https://github.com/DotaPie/cat-monitoring.git && cd ./cat-monitoring
```
Open ./src/config.json and edit as you want (cams, ftp server, paths, ...)
    - if you want LED usage for Raspberry PI, choose requirements-rpi.txt during running install script (you will be prompted)
    - for any other option use requirements.txt
    - LED blinks if script is running, stays on when any movement detected on any camera
```
sudo bash ./install.sh
```

## Verify installation and check logs
```
sudo systemctl status cat-monitoring
sudo journalctl -u cat-monitoring.service -n 100 -f 
sudo journalctl -u cat-monitoring.service -n 100 -f -g '\\[CAM1\\]'
```

## TODO (features):
- figure out how not to trigger motion when cloud hides are unhides sun (wip - testing)
- cameras preview via web browser
- pushup notification (android, iphone, windows) when motion is detected (configurable and optional .. use https://ntfy.sh, up to 250 messages/day free)

## BUGS (known):
- sometimes, video start with POST-MOTION tag, then hops into PRE-MOTION, then continues normally into MOTION and POST-MOTION, dunno why (note for me: CAM1_2025-05-09_21-09-24_495535.mp4)