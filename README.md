# cat-monitoring

WIP

Linux only (currently tested on raspberry pi 4 only, but without LED it should work on any linux)

Quick start:
1) git clone https://github.com/DotaPie/cat-monitoring.git && cd ./cat-monitoring
2) Open ./src/config.json and edit as you want (cams, ftp server, ...)
3) sudo bash ./install.sh

Note: for raspberry choose requirements-rpi.txt during running install script (will be prompted)

TODO:
- Timestamp to videos
- Verify videos continuity, especially pre-buffer -> main buffer