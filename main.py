import yt_dlp
import pyperclip
import time
import sys
from PySide6.QtCore import QThread, Signal, QTimer, Slot, QSettings
from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog, QListWidgetItem, QStatusBar
from PySide6.QtGui import QPixmap
from ui_main import Ui_MainWindow
import requests
import re
import os
import queue


def get_thumbnail_url(video_url):
    """yt-dlp를 사용하여 비디오 썸네일 URL 추출"""
    ydl_opts = {
        'quiet': True,  # 출력 없애기
        'extract_flat': True  # 비디오 내용만 추출
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(video_url, download=False)
        thumbnail_url = info_dict.get('thumbnail', None)
        return thumbnail_url
        
        
        
# 전체 다운로드 진행률 계산
total_size = 0
downloaded_size = 0
def progress_hook(d):
    global total_size, downloaded_size
    if d['status'] == 'downloading':
        if total_size == 0 and d.get('total_bytes'):
            total_size = d['total_bytes'] 
        downloaded_size = d['downloaded_bytes']
        

        if total_size > 0:
            progress = int(downloaded_size / total_size * 100)
            window.progressUpdated.emit(progress)


def download_video_with_ytdlp(youtube_url):
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': f'{directory}/%(title)s.%(ext)s',
        'progress_hooks': [progress_hook],
    }
            
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(youtube_url, download=True)
        file_path = ydl.prepare_filename(info_dict).replace('.webm', '.mp4')
        
        # 경로에서 파일 이름만 추출
        file_name = os.path.basename(file_path)
        print(f"Downloaded file: {file_name}")
    
    return file_name

def download_thumbnail(thumbnail_url):
    """썸네일 이미지를 다운로드하여 QPixmap 객체로 변환"""
    try:
        response = requests.get(thumbnail_url)
        response.raise_for_status()
        
        pixmap = QPixmap()
        pixmap.loadFromData(response.content)
        
        return pixmap
    except requests.exceptions.RequestException as e:
        print(f"썸네일 다운로드 실패: {e}")
        return None

def trace_clipboard():
    clipboard_text = pyperclip.paste()
    print("클립보드에 있는 텍스트:", clipboard_text)
    previous_clipboard = ""
    while True:
        clipboard_text = pyperclip.paste()

        if clipboard_text != previous_clipboard:
            print("클립보드 내용 변경됨: ", clipboard_text)
            previous_clipboard = clipboard_text
            window.lineEdit.text = clipboard_text

        time.sleep(1)
    

class ClipboardThread(QThread):
    clipboardChanged = Signal(str) 

    def __init__(self):
        super().__init__()
        self.previous_clipboard = ""
        
        self.clipboard = QApplication.clipboard()
                
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_clipboard)
        self.timer.start(500)
        
    def check_clipboard(self):
        """클립보드의 내용이 변경되었는지 확인"""
        clipboard_text = self.clipboard.text()
        if clipboard_text != self.previous_clipboard:
            self.previous_clipboard = clipboard_text
            self.clipboardChanged.emit(clipboard_text)
            
    
    def run(self):
        """스레드가 시작될 때 실행"""
        self.exec()

    def stop(self):
        """스레드를 안전하게 종료하는 메서드"""
        self.timer.stop()
        self.requestInterruption()
        self.quit()
        self.wait()


class ThumbnailDownloader(QThread):
    finished = Signal(QPixmap)

    def __init__(self, thumbnail_url):
        super().__init__()
        self.thumbnail_url = thumbnail_url

    def run(self):
        try:
            response = requests.get(self.thumbnail_url)
            response.raise_for_status()

            pixmap = QPixmap()
            pixmap.loadFromData(response.content)

            self.finished.emit(pixmap)
        except requests.exceptions.RequestException as e:
            print(f"썸네일 다운로드 실패: {e}")
            self.finished.emit(QPixmap())

class FileDownloadThread(QThread):
    finished = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            downloaded_file = download_video_with_ytdlp(self.url)
            self.finished.emit(downloaded_file)
        except Exception as e:
            print(f"다운로드 중 오류 발생: {e}")
            self.finished.emit('')
                
class QueueThread(QThread):
    finished = Signal(str)
    
    def __init__(self, url_queue):
        super().__init__()
        self.url_queue = url_queue
        
    def run(self):
        while True:
            if not self.url_queue.empty():
                try:
                    self.finished.emit('download')
                except Exception as e:
                    print(f"다운로드 중 오류 발생: {e}")
                    self.finished.emit('')
            time.sleep(1)
            

class Main_Window(QMainWindow, Ui_MainWindow):
    progressUpdated = Signal(int)
    
    def __init__(self):
        super(Main_Window, self).__init__()
        self.setupUi(self)
        
        self.setStatusBar(QStatusBar(self))
        
        self.settings = QSettings("MyApp", "VideoDownloader")
        self.load_settings()
        
        self.clipboard_thread = None
        self.run_ClipboardThread()
        self.url = ""
        self.fileName = ""
        self.is_downloading = False
        self.autoDownload = self.action_4.isChecked()
        self.url_queue = queue.Queue()
        
        self.download_thread = QueueThread(self.url_queue)
        self.download_thread.finished.connect(self.download)
        self.download_thread.start()
        self.progressUpdated.connect(self.update_progress_bar)
        self.progressBar.setValue(0)
        
    def run_ClipboardThread(self):
        try:
            if(self.action_3.isChecked()):
                if self.clipboard_thread is None or (not self.clipboard_thread.isRunning()):
                    self.clipboard_thread = ClipboardThread()
                    self.clipboard_thread.clipboardChanged.connect(self.update_line_edit)
                    self.clipboard_thread.start()
            else:
                if self.clipboard_thread:
                    self.clipboard_thread.stop()
                    self.clipboard_thread = None
        except Exception as e:
            print()

    
    def setAutoDownload(self):
        try: 
            self.autoDownload = not self.autoDownload
        except Exception as e:
            print()
    
    def is_url(self, url):
        youtube_regex = r'https?://'
        return re.match(youtube_regex, url) is not None
    
    
    def update_line_edit(self, new_text):
        if self.is_url(new_text):
            self.lineEdit.setText(new_text)
            if self.autoDownload:
                self.url_queue.put(new_text)
            
    def set_directory(self):
        global directory
        directory = QFileDialog.getExistingDirectory(self, "디렉토리 열기", "")
        
        if directory:
            self.lineEdit.setText(directory)
            print(f"선택한 디렉토리 경로: {directory}")

        
    def update_progress_bar(self, progress):
            current_progress = self.progressBar.value()
            
            def smooth_update():
                nonlocal current_progress
                
                if current_progress < progress:
                    current_progress += 1
                elif current_progress > progress:
                    current_progress = progress 
                
                self.progressBar.setValue(current_progress)

                if current_progress != progress:
                    QTimer.singleShot(10, smooth_update)

            smooth_update()
            
    def downloadEvent(self):
        if(self.is_url(self.lineEdit.text())):
            self.url_queue.put(self.lineEdit.text())
            self.lineEdit.setText("")
            self.download('download')
        
    @Slot(str)
    def download(self, message):
        if 'download' in message and not self.is_downloading:
            self.url = self.url_queue.get()
            try:
                self.is_downloading = True
                self.download_thread = FileDownloadThread(self.url)
                self.download_thread.finished.connect(self.on_download_finished)
                self.download_thread.start()
            except Exception as e:
                print("wrong URL.")
        
    def on_download_finished(self, file_path):
        self.fileName = file_path
        if file_path:
            print(f"다운로드 완료: {file_path}")
            self.update_progress_bar(0)
            self.lineEdit.setText("")
            self.download_thumbnail()
        else:
            print("다운로드 실패")
            
    def download_thumbnail(self):
        thumbnail_url = get_thumbnail_url(self.url)
        if thumbnail_url:
            self.downloader = ThumbnailDownloader(thumbnail_url)
            self.downloader.finished.connect(self.add_thumbnail_to_list)
            self.downloader.start()
    
    def add_thumbnail_to_list(self, pixmap):
        """QPixmap을 QListWidget에 추가"""
        if not pixmap.isNull():
            self.fileName = self.fileName[:-4]
            item = QListWidgetItem(self.fileName)
            item.setIcon(pixmap)
            self.listWidget.insertItem(0,item)
            self.is_downloading = False
        else:
            print("썸네일 다운로드 실패")
        
    def load_settings(self):
        """저장된 설정을 불러와 메뉴바 체크 상태 적용"""
        global directory
        
        is_checked = self.settings.value("menu_action_3_checked", False, type=bool)
        self.action_3.setChecked(is_checked)
        
        is_checked = self.settings.value("menu_action_4_checked", False, type=bool)
        self.action_4.setChecked(is_checked)
        
        directory = self.settings.value("download_directory", "", type=str)
        if directory:
            self.statusBar().showMessage(f"파일 경로 => {directory}")

    def save_settings(self):
        """현재 메뉴바 체크 상태와 디렉토리 경로를 설정에 저장"""
        self.settings.setValue("menu_action_3_checked", self.action_3.isChecked())
        self.settings.setValue("menu_action_4_checked", self.action_4.isChecked())
        self.settings.setValue("download_directory", directory)

    def set_directory(self):
        """디렉토리 선택 후 경로를 저장"""
        global directory
        directory = QFileDialog.getExistingDirectory(self, "디렉토리 열기", self.lineEdit.text())

        if directory:
            self.statusBar().showMessage(f"파일 경로 => {directory}")
            print(f"선택한 디렉토리 경로: {directory}")

            self.save_settings()
        
    def closeEvent(self, event):
        """애플리케이션 종료 시 설정을 저장"""
        self.save_settings()
        event.accept()

directory = "./downloads"
progress = 0
app = QApplication(sys.argv)

window = Main_Window()
window.show()
app.exec()

