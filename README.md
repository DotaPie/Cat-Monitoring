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
2) sudo journalctl -u cat-monitoring.service -n 100 -f -g '' # for CAM1 log only for example, insert "\\[CAM1\\]" into ''

KNOWN BUGS:
- sometimes, video start with POST-MOTION tag, then hops into PRE-MOTION, then continues normally into MOTION and POST-MOTION, dunno why (note: CAM1_2025-05-09_21-09-24_495535.mp4)

TODO ASAP:
- Finish readme

TODO later:
- cams preview via web browser