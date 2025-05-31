# cat-monitoring

WIP

Takes stream from N cams, detects motion on them, and if motion is detected takes couple of seconds before the motion, and together with frames during the motion and after motion, it sends it to FTP server.

Linux only (currently tested on Raspberry PI 4 (4GB) only, but without LED it should work on any debian based linux with Python3.11+ pre-installed)

Regarding performance, it takes about 50% CPU usage on Raspberry PI 4 when using 2x USB 2.0 CAM (640x480@10FPS)

No live preview of cams (so far).

Quick start:
1) git clone https://github.com/DotaPie/cat-monitoring.git && cd ./cat-monitoring
2) Open ./src/config.json and edit as you want (cams, ftp server, ...)
3) sudo bash ./install.sh

Note: for Raspberry PI choose requirements-rpi.txt during running install script (you will be prompted)

Verify with:
1) sudo systemctl status cat-monitoring
2) sudo journalctl -u cat-monitoring.service -n 100 -f # check logs
2) sudo journalctl -u cat-monitoring.service -n 100 -f -g '\\[CAM1\\]' # check logs only for CAM1

KNOWN BUGS:
- sometimes, video start with POST-MOTION tag, then hops into PRE-MOTION, then continues normally into MOTION and POST-MOTION, dunno why (note: CAM1_2025-05-09_21-09-24_495535.mp4)

TODO asap:
- figure out how not to trigger motion when cloud hides are unhides sun (work in progress - testing)
- finish readme
- create videos in /dev/shm/CatMonitoring/videos by default, change DELETE_VIDEO into SAVE_VIDEO_LOCALLY (use VIDEO_PATH here)
    - change paths in installation script based on paths in configuration file (videos and logs)
- generate uninstall.sh 

TODO features:
- cams preview via web browser
- pushup notification (android, iphone, windows) when motion is detected (configurable and optional .. use https://ntfy.sh, up to 250 messages/day free)
