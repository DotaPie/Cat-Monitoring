# Cat Monitoring
Takes stream from N cameras and detects motion on them. If motion is detected, video (with some pre-buffer and post-buffer) is saved locally or uploaded to FTP server (can do one or another, or both).
<p align="center">
  <img src="https://github.com/DotaPie/cat-monitoring/blob/main/cat.gif" width="640" alt="Usage preview">
</p>

## Quick start
```
git clone https://github.com/DotaPie/cat-monitoring.git && cd ./cat-monitoring
```
>Open ./src/config.json and edit as you want (cams, ftp server, paths, ...)
>- configuration will work out-of-box, except cameras configuration (mainly number of cameras and their device paths)
>- if STATUS_LED_RPI is true, requirements-rpi.txt is used instead of requirements.txt during installation
```
sudo bash ./install.sh
```

## Hardware requirements
- RAM: 1GB (recommended)
- CPU: At least performance of Raspberry PI 4 (recommended) 
    - it takes about 50% CPU usage on Raspberry PI 4, when using 2x USB 2.0 camera (640x480@10FPS)
- GPU: not needed
- Camera: any USB camera/-s

## OS requirements: 
- debian based linux
- installed python3.11 or higher
- installed git
- mounted /dev/shm with at least 256MB (should be already there on most modern linux distros)
- GUI not needed

## FTP server requirements (optional):
- any FTP/FTPS running server with default port 21 and default port range for FTPS
    - tested on linux (FTPS), but it should also work on windows

## Verify installation and check logs
```
sudo systemctl status cat-monitoring
sudo journalctl -u cat-monitoring.service -n 100 -f 
sudo journalctl -u cat-monitoring.service -n 100 -f -g '\[CAM1\]'
```
