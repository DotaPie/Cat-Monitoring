# Purr View
- Takes stream from any amount of cameras (or any video stream that is accepted by opencv python library) and detects motion on them. 
- If motion is detected, video (with some pre-buffer and post-buffer) is saved locally or uploaded to FTP server (can do one or another, or both). 
- Streams can be viewed via web browser (this feature supports only up to 4 streams).
<p align="center">
  <img src="https://github.com/DotaPie/purr-view/blob/main/cat.gif" width="640" alt="Usage preview">
</p>

## Hardware requirements
I will be assuming example of 2 cams, each 640 x 480 @ 10 FPS, and running on Raspberry PI 4
- RAM: process consumes up to 500 MB, but there is still chance of some memory leaking so I recommend at least 1 GB (for now)
    - this also depends on the configuration of pre-buffer and post-buffer 
- CPU: process consumes around 15% of the CPU while idling in detection
  - this increases to 30% when previewing the video streams from both cams
- GPU: not needed
- Camera: any USB camera/-s (or any video stream that is accepted by opencv python library)

## OS requirements: 
- debian based linux
- installed python3.8 or higher (python3.11 or higher recommended, because of EOL https://devguide.python.org/versions/)
- installed git
- mounted /dev/shm with (should be already there on most modern linux distros)
    - size depends on number of video streams, resolution and FPS, but allocating 256 MB will be most likely enaugh
- GUI not needed

## FTP server requirements (optional):
- any FTP/FTPS running server with default port 21 and default port range for FTPS
    - tested on linux (FTPS), but it should also work on windows

## Quick start
```
git clone https://github.com/DotaPie/purr-view.git && cd ./purr-view
```
Open ./src/config.json and edit as you want (cams, ftp server, paths, ...)
- Configuration will work out-of-box, except cameras configuration (mainly number of cameras and their device paths)
```
sudo bash ./install.sh
```

### Verify service is up and runing and check logs
> We can also filter by keyword, for example "[SYS]"
```
sudo systemctl status purr-view
sudo journalctl -u purr-view.service -n 100 -f 
sudo journalctl -u purr-view.service -n 100 -f -g '\[SYS\]'
```

### Preview camera streams
> Supports only up to 4 video stream views
```
http://purrview.local
```

### Change configuration only
```
sudo nano /opt/PurrView/config.json
sudo systemctl restart purr-view
```

### Re-deploy service easily after changing files in ./src
> In case we change source files or config again in ./src directory
```
sudo bash ./re-deploy.sh
```
