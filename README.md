# YouTubeDownloader
- 복사한 URL 자동 입력 및 다운로드 대기열
- 다중 포맷의 실제 바이트를 합산한 전체 다운로드 진행률 표시
- 다운로드 완료 후 썸네일 목록 표시
- yt-dlp, FFmpeg/FFprobe, Deno를 프로그램 옆 `tools/` 폴더에서 자체 관리
- 시스템 PATH, Python용 `yt_dlp` 패키지 및 사용자의 FFmpeg 설치가 필요 없음

## 실행

소스 코드 실행에 필요한 Python 패키지만 설치합니다.

```cmd
python -m venv venv
venv\Scripts\activate.bat
pip install -r requirements.txt
python main.py
```

첫 실행 시 인터넷에 연결되어 있으면 다음 공식/신뢰 배포처에서 도구를 자동으로
준비합니다.

- `yt-dlp.exe`: [yt-dlp](https://github.com/yt-dlp/yt-dlp/releases)
- `ffmpeg.exe`, `ffprobe.exe`: [FFmpeg](https://ffmpeg.org/download.html)
- `deno.exe`: [Deno](https://github.com/denoland/deno/releases)

도구는 소스 실행 시 프로젝트의 `tools/`, 빌드 실행 시
`YouTubeDownloader.exe` 옆의 `tools/`에 저장됩니다.

## PyInstaller 빌드

```cmd
python -m venv venv
venv\Scripts\python -m pip install -r requirements.txt pyinstaller
venv\Scripts\pyinstaller --clean --noconfirm main.spec
```

빌드 결과인 `dist/YouTubeDownloader.exe`를 실행하면 같은 위치에 `tools/`가
생성됩니다. 외부 도구는 one-file 번들의 임시 경로(`sys._MEIPASS`)에 넣지 않으므로
업데이트 후에도 유지됩니다.

사용자에게 필요한 오류는 GUI 메시지로
표시하고, 상세 yt-dlp 출력과 Python traceback은 실행 파일 옆의 날짜별 로그에 저장합니다.

```text
dist/
├─ YouTubeDownloader.exe
├─ tools/
│  ├─ yt-dlp.exe
│  ├─ ffmpeg.exe
│  ├─ ffprobe.exe
│  └─ deno.exe
└─ logs/
   └─ YYYY-MM-DD.txt
```

`tools/`와 `logs/`는 최초 실행 시 필요한 경우 자동 생성됩니다.
