투박하지만 yt_dlp을 이용한 유튜브 다운로드 프로그램. [다운로드 하기](https://github.com/Hoppouu/YoutubeDownloader/releases/tag/v1.0)
### 기타 내용
1. URL복사하면 자동으로 프로그램의 다운로드할 URL에 넣어줌.
2. 현재 다운로드 중인데 다른 URL을 다운로드 하려고 할 때 Queue에 넣어서 순차적으로 진행됨.
3. 다운로드 상황을 나타내는 프로그레스 바가 있음.
4. 다운로드하면 리스트에 영상 썸네일 표시
### 실행 방법
[ffmpeg](https://github.com/BtbN/FFmpeg-Builds/releases)가 설치되어 있어야 됨.<br>
환경 변수 위치 -> ffmpeg\bin
```cmd
python -m venv venv
venv\Scripts\activate.bat
pip install -r requirements.txt
python main.py
```
<br>
