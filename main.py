import logging
import queue
import re
import sys
import threading

import requests
from PySide6.QtCore import QSettings, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStatusBar,
)

from app_logging import install_exception_hook, setup_logging
from dependency_manager import (
    DenoManager,
    FFmpegManager,
    YtDlpManager,
    classify_dependency_error,
    cleanup_stale_downloads,
)
from task_control import (
    CancellationToken,
    OperationCancelled,
    request_process_termination,
)
from ui_main import Ui_MainWindow
from yt_dlp_runner import YtDlpDownloadError, download_video, get_thumbnail_url


logger = logging.getLogger(__name__)


class DependencySetupThread(QThread):
    statusChanged = Signal(str)
    progressUpdated = Signal(int)
    completed = Signal(object)

    YT_DLP_PHASE = (0, 12)
    FFMPEG_PHASE = (12, 75)
    DENO_PHASE = (75, 98)

    def __init__(self):
        super().__init__()
        self.cancel_token = CancellationToken()

    def cancel(self):
        self.cancel_token.cancel()
        self.requestInterruption()

    def download_progress_callback(
        self,
        label,
        start_progress,
        end_progress,
        post_download_message,
    ):
        last_local_progress = -1

        def update(downloaded_bytes, total_bytes):
            nonlocal last_local_progress
            self.cancel_token.raise_if_cancelled()
            if total_bytes > 0:
                local_progress = min(
                    100,
                    int(downloaded_bytes * 100 / total_bytes),
                )
                # A resumed/restarted HTTP request must never move the phase back.
                if local_progress <= last_local_progress:
                    return
                last_local_progress = local_progress
                # Keep one point in the phase for extraction/validation.
                phase_span = max(1, end_progress - start_progress - 1)
                overall_progress = start_progress + int(
                    local_progress * phase_span / 100
                )
                self.progressUpdated.emit(overall_progress)
                if local_progress >= 100:
                    self.statusChanged.emit(post_download_message)
                else:
                    self.statusChanged.emit(
                        f"{label} 다운로드 중... {local_progress}%"
                    )
            else:
                # A missing Content-Length is shown as Qt's indeterminate bar.
                self.progressUpdated.emit(-1)
                downloaded_mb = downloaded_bytes / (1024 * 1024)
                self.statusChanged.emit(
                    f"{label} 다운로드 중... {downloaded_mb:.1f} MB"
                )

        return update

    def download_retry_callback(self, label):
        def retry(retry_number, retry_count, details):
            self.cancel_token.raise_if_cancelled()
            logger.warning(
                "%s 다운로드 재시도 %d/%d: %s",
                label,
                retry_number,
                retry_count,
                details,
            )
            self.statusChanged.emit(
                f"{label} 다운로드를 다시 시도하고 있습니다... "
                f"({retry_number}/{retry_count})"
            )

        return retry

    @staticmethod
    def error_result(error):
        return {
            "category": classify_dependency_error(error),
            "details": str(error),
        }

    def run(self):
        result = {
            "yt_dlp_ready": False,
            "ffmpeg_ready": False,
            "deno_ready": False,
            "current_version": None,
            "latest_version": None,
            "update_available": False,
            "cancelled": False,
            "errors": {},
        }

        try:
            self.progressUpdated.emit(0)
            cleanup_stale_downloads()
            yt_dlp_manager = YtDlpManager(
                cancel_token=self.cancel_token,
                progress_callback=self.download_progress_callback(
                    "yt-dlp",
                    *self.YT_DLP_PHASE,
                    "yt-dlp를 확인하고 있습니다...",
                ),
                retry_callback=self.download_retry_callback("yt-dlp"),
            )
            ffmpeg_manager = FFmpegManager(
                cancel_token=self.cancel_token,
                progress_callback=self.download_progress_callback(
                    "FFmpeg",
                    *self.FFMPEG_PHASE,
                    "FFmpeg 압축을 해제하고 있습니다...",
                ),
                retry_callback=self.download_retry_callback("FFmpeg"),
            )
            deno_manager = DenoManager(
                cancel_token=self.cancel_token,
                progress_callback=self.download_progress_callback(
                    "Deno",
                    *self.DENO_PHASE,
                    "Deno 압축을 해제하고 있습니다...",
                ),
                retry_callback=self.download_retry_callback("Deno"),
            )

            self.statusChanged.emit("yt-dlp를 확인하고 있습니다...")
            try:
                result["current_version"] = yt_dlp_manager.ensure_installed()
                result["yt_dlp_ready"] = True
                logger.info("yt-dlp 준비 확인: %s", result["current_version"])
            except OperationCancelled:
                raise
            except Exception as exc:
                logger.exception("yt-dlp 준비 실패")
                result["errors"]["yt-dlp"] = self.error_result(exc)
            self.progressUpdated.emit(self.YT_DLP_PHASE[1])

            self.cancel_token.raise_if_cancelled()
            self.statusChanged.emit("FFmpeg를 준비하고 있습니다...")
            try:
                ffmpeg_path, ffprobe_path = ffmpeg_manager.ensure_installed()
                result["ffmpeg_ready"] = True
                logger.info(
                    "FFmpeg 준비 확인: ffmpeg=%s, ffprobe=%s",
                    ffmpeg_path,
                    ffprobe_path,
                )
            except OperationCancelled:
                raise
            except Exception as exc:
                logger.exception("FFmpeg 준비 실패")
                result["errors"]["ffmpeg"] = self.error_result(exc)
            self.progressUpdated.emit(self.FFMPEG_PHASE[1])

            self.cancel_token.raise_if_cancelled()
            self.statusChanged.emit("YouTube JavaScript 런타임을 준비하고 있습니다...")
            try:
                deno_path = deno_manager.ensure_installed()
                result["deno_ready"] = True
                logger.info("Deno 준비 확인: %s", deno_path)
            except OperationCancelled:
                raise
            except Exception as exc:
                logger.exception("Deno 준비 실패")
                result["errors"]["deno"] = self.error_result(exc)
            self.progressUpdated.emit(self.DENO_PHASE[1])

            if result["yt_dlp_ready"]:
                try:
                    self.progressUpdated.emit(99)
                    self.statusChanged.emit("yt-dlp 최신 버전을 확인하고 있습니다...")
                    current, latest, update_available = yt_dlp_manager.check_update()
                    result["current_version"] = current
                    result["latest_version"] = latest
                    result["update_available"] = update_available
                except OperationCancelled:
                    raise
                except Exception:
                    # A version-check failure must never make installed tools unusable.
                    logger.exception("yt-dlp 업데이트 확인 실패; 현재 버전을 사용합니다")
            self.progressUpdated.emit(100)
        except OperationCancelled:
            result["cancelled"] = True
            logger.info("필수 도구 준비 작업 취소")
        except Exception as exc:
            logger.exception("필수 도구 준비 중 예상하지 못한 오류")
            result["errors"]["unexpected"] = self.error_result(exc)

        self.completed.emit(result)


class YtDlpUpdateThread(QThread):
    statusChanged = Signal(str)
    progressUpdated = Signal(int)
    completed = Signal(bool, str, str)

    def __init__(self, expected_version):
        super().__init__()
        self.expected_version = expected_version
        self.cancel_token = CancellationToken()

    def cancel(self):
        self.cancel_token.cancel()
        self.requestInterruption()

    def update_download_progress(self, downloaded_bytes, total_bytes):
        self.cancel_token.raise_if_cancelled()
        if total_bytes > 0:
            local_progress = min(100, int(downloaded_bytes * 100 / total_bytes))
            self.progressUpdated.emit(int(local_progress * 0.95))
            if local_progress >= 100:
                self.statusChanged.emit("yt-dlp를 확인하고 있습니다...")
            else:
                self.statusChanged.emit(
                    f"yt-dlp 업데이트 다운로드 중... {local_progress}%"
                )
        else:
            self.progressUpdated.emit(-1)
            downloaded_mb = downloaded_bytes / (1024 * 1024)
            self.statusChanged.emit(
                f"yt-dlp 업데이트 다운로드 중... {downloaded_mb:.1f} MB"
            )

    def update_retry_status(self, retry_number, retry_count, details):
        self.cancel_token.raise_if_cancelled()
        logger.warning(
            "yt-dlp 업데이트 다운로드 재시도 %d/%d: %s",
            retry_number,
            retry_count,
            details,
        )
        self.statusChanged.emit(
            "yt-dlp 업데이트 다운로드를 다시 시도하고 있습니다... "
            f"({retry_number}/{retry_count})"
        )

    def run(self):
        try:
            installed_version = YtDlpManager(
                cancel_token=self.cancel_token,
                progress_callback=self.update_download_progress,
                retry_callback=self.update_retry_status,
            ).update(self.expected_version)
            self.progressUpdated.emit(100)
            self.completed.emit(True, installed_version, "")
        except OperationCancelled:
            logger.info("yt-dlp 업데이트 취소")
        except Exception as exc:
            logger.exception("yt-dlp 업데이트 실패")
            self.completed.emit(False, "", str(exc))


class CancellableProcessThread(QThread):
    def __init__(self):
        super().__init__()
        self.cancel_token = CancellationToken()
        self._process = None
        self._process_lock = threading.Lock()

    def set_process(self, process):
        with self._process_lock:
            self._process = process

    def cancel(self):
        self.cancel_token.cancel()
        self.requestInterruption()
        with self._process_lock:
            request_process_termination(self._process)
        # Keep the GUI responsive while giving yt-dlp time to exit normally.
        # If it ignores terminate(), the main-thread timer applies the final kill.
        QTimer.singleShot(2000, self._kill_process_if_running)

    def _kill_process_if_running(self):
        with self._process_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            try:
                logger.warning("종료되지 않은 외부 프로세스를 강제 종료합니다")
                process.kill()
            except OSError:
                logger.exception("외부 프로세스 강제 종료 실패")


class ThumbnailDownloader(QThread):
    finished = Signal(bytes)

    def __init__(self, thumbnail_url):
        super().__init__()
        self.thumbnail_url = thumbnail_url
        self.cancel_token = CancellationToken()

    def cancel(self):
        self.cancel_token.cancel()
        self.requestInterruption()

    def run(self):
        try:
            image_data = bytearray()
            with requests.get(
                self.thumbnail_url,
                stream=True,
                timeout=(5, 10),
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    self.cancel_token.raise_if_cancelled()
                    if chunk:
                        image_data.extend(chunk)
            self.finished.emit(bytes(image_data))
        except OperationCancelled:
            logger.info("썸네일 다운로드 취소")
        except requests.RequestException:
            logger.exception("썸네일 다운로드 실패")
            self.finished.emit(b"")
        except Exception:
            logger.exception("썸네일 처리 중 예상하지 못한 오류")
            self.finished.emit(b"")


class ThumbnailUrlThread(CancellableProcessThread):
    finished = Signal(str)

    def __init__(self, video_url):
        super().__init__()
        self.video_url = video_url

    def run(self):
        try:
            thumbnail_url = get_thumbnail_url(
                self.video_url,
                cancel_token=self.cancel_token,
                process_callback=self.set_process,
            )
            self.finished.emit(thumbnail_url or "")
        except OperationCancelled:
            logger.info("썸네일 URL 확인 취소")
        except Exception:
            logger.exception("썸네일 URL 확인 중 예상하지 못한 오류")
            self.finished.emit("")


class FileDownloadThread(CancellableProcessThread):
    finished = Signal(str)
    failed = Signal(str, str, str)
    cancelled = Signal()
    progressUpdated = Signal(int)
    statusChanged = Signal(str)

    def __init__(self, url, output_directory):
        super().__init__()
        self.url = url
        self.output_directory = output_directory

    def run(self):
        try:
            downloaded_file = download_video(
                self.url,
                self.output_directory,
                progress_callback=self.progressUpdated.emit,
                status_callback=self.statusChanged.emit,
                cancel_token=self.cancel_token,
                process_callback=self.set_process,
            )
            self.finished.emit(downloaded_file)
        except OperationCancelled:
            logger.info("사용자 종료로 다운로드 취소: %s", self.url)
            self.cancelled.emit()
        except YtDlpDownloadError as exc:
            logger.error(
                "다운로드 실패: category=%s, message=%s",
                exc.category,
                exc.user_message.replace("\n", " "),
            )
            for line in exc.details.splitlines():
                logger.error("yt-dlp error output: %s", line)
            self.failed.emit(exc.category, exc.user_message, exc.details)
        except Exception as exc:
            logger.exception("다운로드 중 예상하지 못한 오류")
            self.failed.emit(
                "unexpected",
                "예상하지 못한 다운로드 오류가 발생했습니다.\n"
                "자세한 내용은 로그 파일을 확인해주세요.",
                str(exc),
            )


class Main_Window(QMainWindow, Ui_MainWindow):
    PROGRESS_SCALE = 10
    MAX_PROGRESS_ANIMATION_FRAMES = 120

    def __init__(self):
        super().__init__()
        self.setupUi(self)
        self.setStatusBar(QStatusBar(self))

        self.settings = QSettings("MyApp", "VideoDownloader")
        self.url = ""
        self.fileName = ""
        self.is_downloading = False
        self.dependencies_initializing = False
        self.yt_dlp_ready = False
        self.ffmpeg_ready = False
        self.deno_ready = False
        self.update_prompt_shown = False
        self.closing = False
        self._allow_close = False
        self.progress_cycle = 0
        self.tool_setup_cycle = 0
        self.dependency_progress_value = 0
        self.display_progress = 0
        self.target_progress = 0
        self.progress_animation_step = 1
        self.pending_download_url = None
        self.url_queue = queue.Queue()

        self.dependency_thread = None
        self.yt_dlp_update_thread = None
        self.file_download_thread = None
        self.thumbnail_url_thread = None
        self.thumbnail_downloader = None

        self.clipboard_timer = QTimer(self)
        self.clipboard_timer.setInterval(500)
        self.clipboard_timer.timeout.connect(self.check_clipboard)
        self.previous_clipboard = ""

        self.queue_timer = QTimer(self)
        self.queue_timer.setInterval(250)
        self.queue_timer.timeout.connect(self.process_download_queue)
        self.queue_timer.start()

        self.shutdown_timer = QTimer(self)
        self.shutdown_timer.setInterval(100)
        self.shutdown_timer.timeout.connect(self.check_shutdown_complete)

        # One timer owns every determinate ProgressBar animation. Progress
        # events only move target_progress, so timer chains can never overlap.
        self.progress_animation_timer = QTimer(self)
        self.progress_animation_timer.setInterval(15)
        self.progress_animation_timer.timeout.connect(
            self.advance_progress_animation
        )

        self.load_settings()
        self.autoDownload = self.action_4.isChecked()
        self.run_ClipboardThread()
        self.reset_progress_bar()
        self.start_dependency_setup()

    def reset_progress_bar(self):
        self.progress_animation_timer.stop()
        self.display_progress = 0
        self.target_progress = 0
        self.progress_animation_step = 1
        self.progressBar.setRange(0, 100 * self.PROGRESS_SCALE)
        self.progressBar.setValue(0)
        self.progressBar.setFormat("0.0%")

    def set_progress_indeterminate(self):
        self.progress_animation_timer.stop()
        self.progressBar.setRange(0, 0)
        self.progressBar.setFormat("")

    def set_progress_target(self, progress):
        progress = max(0, min(100, int(progress)))
        scaled_progress = progress * self.PROGRESS_SCALE
        self.target_progress = max(self.target_progress, scaled_progress)
        if self.progressBar.minimum() == 0 and self.progressBar.maximum() == 0:
            self.progressBar.setRange(0, 100 * self.PROGRESS_SCALE)
            self.progressBar.setValue(self.display_progress)
            self.progressBar.setFormat(
                f"{self.display_progress / self.PROGRESS_SCALE:.1f}%"
            )
        if self.display_progress < self.target_progress:
            difference = self.target_progress - self.display_progress
            self.progress_animation_step = max(
                1,
                (
                    difference + self.MAX_PROGRESS_ANIMATION_FRAMES - 1
                )
                // self.MAX_PROGRESS_ANIMATION_FRAMES,
            )
            if not self.progress_animation_timer.isActive():
                self.progress_animation_timer.start()

    def advance_progress_animation(self):
        if self.display_progress >= self.target_progress:
            self.progress_animation_timer.stop()
            return
        self.display_progress = min(
            self.target_progress,
            self.display_progress + self.progress_animation_step,
        )
        self.progressBar.setValue(self.display_progress)
        self.progressBar.setFormat(
            f"{self.display_progress / self.PROGRESS_SCALE:.1f}%"
        )
        if self.display_progress >= self.target_progress:
            self.progress_animation_timer.stop()

    def start_dependency_setup(self):
        if self.closing or (
            self.dependency_thread and self.dependency_thread.isRunning()
        ):
            return
        self.dependencies_initializing = True
        self.tool_setup_cycle += 1
        self.dependency_progress_value = 0
        self.pushButton.setEnabled(False)
        self.reset_progress_bar()
        self.statusBar().showMessage("필수 도구를 준비하고 있습니다...")
        self.dependency_thread = DependencySetupThread()
        self.dependency_thread.statusChanged.connect(self.statusBar().showMessage)
        self.dependency_thread.progressUpdated.connect(
            self.update_dependency_progress
        )
        self.dependency_thread.completed.connect(self.on_dependencies_ready)
        self.dependency_thread.start()

    def update_dependency_progress(self, progress):
        if self.closing:
            return
        if progress < 0:
            self.set_progress_indeterminate()
            return
        progress = max(0, min(100, int(progress)))
        self.dependency_progress_value = max(
            self.dependency_progress_value,
            progress,
        )
        self.set_progress_target(self.dependency_progress_value)

    @staticmethod
    def dependency_error_message(tool_name, error_info):
        category = (
            error_info.get("category", "general")
            if isinstance(error_info, dict)
            else "general"
        )
        labels = {
            "yt-dlp": "yt-dlp",
            "ffmpeg": "FFmpeg/FFprobe",
            "deno": "YouTube JavaScript 런타임(Deno)",
            "unexpected": "필수 도구",
        }
        label = labels.get(tool_name, tool_name)

        if category == "network":
            message = (
                f"{label}을(를) 준비하지 못했습니다.\n"
                "인터넷 연결, 방화벽 또는 회사/학교 네트워크 차단을 확인해주세요."
            )
        elif category == "permission":
            message = (
                f"{label}을(를) 저장할 권한이 없습니다.\n"
                "프로그램을 쓰기 가능한 일반 폴더에 옮긴 뒤 다시 실행해주세요."
            )
        elif category in {"security", "integrity", "execution"}:
            message = (
                f"{label} 파일을 사용할 수 없습니다.\n"
                "Windows 보안 또는 백신이 파일을 차단·격리했는지 보호 기록을 확인해주세요."
            )
        elif category == "archive":
            message = (
                f"{label} 압축 파일이 손상되었거나 열리지 않습니다.\n"
                "네트워크 상태를 확인한 뒤 프로그램을 다시 실행해주세요."
            )
        elif category == "filesystem":
            message = (
                f"{label} 파일을 저장하지 못했습니다.\n"
                "디스크 여유 공간과 프로그램 폴더 상태를 확인해주세요."
            )
        else:
            message = (
                f"{label} 준비 중 예상하지 못한 오류가 발생했습니다.\n"
                "자세한 내용은 logs 폴더의 로그 파일을 확인해주세요."
            )

        if tool_name == "ffmpeg":
            message += "\n영상/음성 병합 기능을 사용할 수 없습니다."
        elif tool_name == "deno":
            message += "\n일부 YouTube 영상 형식이 보이지 않을 수 있습니다."
        return message

    def on_dependencies_ready(self, result):
        self.dependencies_initializing = False
        if result.get("cancelled") or self.closing:
            return

        self.yt_dlp_ready = result["yt_dlp_ready"]
        self.ffmpeg_ready = result["ffmpeg_ready"]
        self.deno_ready = result["deno_ready"]

        error_messages = [
            self.dependency_error_message(tool_name, error_info)
            for tool_name, error_info in result["errors"].items()
        ]

        if error_messages:
            QMessageBox.warning(self, "도구 준비 실패", "\n\n".join(error_messages))

        if result["update_available"] and not self.update_prompt_shown:
            self.dependencies_initializing = True
            self.update_prompt_shown = True
            self.show_yt_dlp_update_prompt(
                result["current_version"], result["latest_version"]
            )
            return

        self.finish_dependency_state()

    def finish_dependency_state(self):
        if self.closing:
            return
        self.pushButton.setEnabled(True)
        if self.yt_dlp_ready and self.ffmpeg_ready:
            self.set_progress_target(100)
            self.statusBar().showMessage("다운로드 준비가 완료되었습니다.")
            if self.pending_download_url:
                pending_url = self.pending_download_url
                self.pending_download_url = None
                self.enqueue_download(pending_url)
            else:
                tool_setup_cycle = self.tool_setup_cycle
                QTimer.singleShot(
                    2500,
                    lambda: self.reset_tool_setup_progress(tool_setup_cycle),
                )
        else:
            self.reset_progress_bar()
            self.statusBar().showMessage(
                "일부 필수 도구를 준비하지 못했습니다. 다운로드 시 다시 시도합니다."
            )

    def reset_tool_setup_progress(self, tool_setup_cycle):
        if (
            not self.closing
            and not self.dependencies_initializing
            and not self.is_downloading
            and self.tool_setup_cycle == tool_setup_cycle
        ):
            if self.display_progress < self.target_progress:
                QTimer.singleShot(
                    100,
                    lambda: self.reset_tool_setup_progress(tool_setup_cycle),
                )
            elif self.display_progress == 100 * self.PROGRESS_SCALE:
                self.reset_progress_bar()

    def show_yt_dlp_update_prompt(self, current_version, latest_version):
        self.pushButton.setEnabled(False)
        message = (
            "새로운 yt-dlp 버전이 있습니다.\n\n"
            f"현재 버전: {current_version}\n"
            f"최신 버전: {latest_version}\n\n"
            "업데이트하시겠습니까?"
        )
        answer = QMessageBox.question(
            self,
            "yt-dlp 업데이트",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if answer == QMessageBox.StandardButton.Yes:
            self.dependency_progress_value = 0
            self.reset_progress_bar()
            self.statusBar().showMessage("yt-dlp를 업데이트하고 있습니다...")
            self.yt_dlp_update_thread = YtDlpUpdateThread(latest_version)
            self.yt_dlp_update_thread.statusChanged.connect(
                self.statusBar().showMessage
            )
            self.yt_dlp_update_thread.progressUpdated.connect(
                self.update_dependency_progress
            )
            self.yt_dlp_update_thread.completed.connect(
                self.on_yt_dlp_update_finished
            )
            self.yt_dlp_update_thread.start()
        else:
            self.dependencies_initializing = False
            logger.info("사용자가 이번 실행의 yt-dlp 업데이트를 거절했습니다")
            self.finish_dependency_state()

    def on_yt_dlp_update_finished(self, success, installed_version, details):
        self.dependencies_initializing = False
        if self.closing:
            return
        if success:
            self.yt_dlp_ready = True
            self.set_progress_target(100)
            logger.info("yt-dlp 업데이트 완료: %s", installed_version)
            QMessageBox.information(
                self, "yt-dlp 업데이트", "yt-dlp 업데이트가 완료되었습니다."
            )
        else:
            self.reset_progress_bar()
            logger.error("yt-dlp 업데이트 실패 상세: %s", details)
            QMessageBox.warning(
                self,
                "yt-dlp 업데이트 실패",
                "yt-dlp 업데이트에 실패했습니다.\n\n"
                "현재 버전을 계속 사용합니다.",
            )
        self.finish_dependency_state()

    def can_start_download(self, show_message=False):
        if self.closing:
            return False
        if self.dependencies_initializing:
            if show_message:
                QMessageBox.information(
                    self,
                    "도구 준비 중",
                    "필수 도구를 준비하고 있습니다. 잠시 후 다시 시도해주세요.",
                )
            return False
        return self.yt_dlp_ready and self.ffmpeg_ready

    def run_ClipboardThread(self):
        if self.action_3.isChecked() and not self.closing:
            self.previous_clipboard = QApplication.clipboard().text()
            self.clipboard_timer.start()
        else:
            self.clipboard_timer.stop()

    def check_clipboard(self):
        clipboard_text = QApplication.clipboard().text()
        if clipboard_text != self.previous_clipboard:
            self.previous_clipboard = clipboard_text
            self.update_line_edit(clipboard_text)

    def setAutoDownload(self):
        self.autoDownload = self.action_4.isChecked()

    @staticmethod
    def is_url(url):
        return re.match(r"https?://", url) is not None

    def update_line_edit(self, new_text):
        if self.is_url(new_text):
            self.lineEdit.setText(new_text)
            if self.autoDownload:
                if self.can_start_download(False):
                    self.enqueue_download(new_text)
                elif not self.dependencies_initializing:
                    self.pending_download_url = new_text
                    self.start_dependency_setup()

    def update_progress_bar(self, progress):
        progress = max(0, min(100, int(progress)))
        self.set_progress_target(progress)
        if progress < 98:
            self.statusBar().showMessage(f"다운로드 중... {progress}%")

    def update_download_status(self, message):
        if not self.closing:
            self.statusBar().showMessage(message)

    def downloadEvent(self):
        url = self.lineEdit.text().strip()
        if not self.is_url(url) or self.closing:
            return
        if self.dependencies_initializing:
            QMessageBox.information(
                self,
                "도구 준비 중",
                "필수 도구를 준비하고 있습니다. 잠시 후 다시 시도해주세요.",
            )
            return
        if not (self.yt_dlp_ready and self.ffmpeg_ready):
            self.pending_download_url = url
            QMessageBox.information(
                self,
                "필수 도구 준비",
                "다운로드에 필요한 도구를 다시 준비합니다.\n"
                "준비가 완료되면 다운로드를 시작합니다.",
            )
            self.start_dependency_setup()
            return
        self.enqueue_download(url)

    def enqueue_download(self, url):
        if self.closing:
            return
        self.url_queue.put(url)
        self.lineEdit.setText("")
        self.process_download_queue()

    @Slot()
    def process_download_queue(self):
        if (
            self.closing
            or self.is_downloading
            or not self.can_start_download(False)
            or self.url_queue.empty()
        ):
            return

        self.url = self.url_queue.get_nowait()
        self.is_downloading = True
        self.progress_cycle += 1
        self.reset_progress_bar()
        self.statusBar().showMessage("영상 정보를 확인하고 있습니다...")
        logger.info("다운로드 대기열 시작: %s", self.url)

        self.file_download_thread = FileDownloadThread(self.url, directory)
        self.file_download_thread.progressUpdated.connect(self.update_progress_bar)
        self.file_download_thread.statusChanged.connect(self.update_download_status)
        self.file_download_thread.finished.connect(self.on_download_finished)
        self.file_download_thread.failed.connect(self.on_download_failed)
        self.file_download_thread.cancelled.connect(self.on_download_cancelled)
        self.file_download_thread.start()

    def on_download_finished(self, file_path):
        self.fileName = file_path
        self.set_progress_target(100)
        self.statusBar().showMessage("다운로드가 완료되었습니다.")
        self.lineEdit.setText("")
        if self.closing:
            self.is_downloading = False
            return
        completion_cycle = self.progress_cycle
        QTimer.singleShot(
            2500,
            lambda: self.reset_completed_progress(completion_cycle),
        )
        self.download_thumbnail()

    def reset_completed_progress(self, completion_cycle):
        if not self.closing and self.progress_cycle == completion_cycle:
            if self.display_progress < self.target_progress:
                QTimer.singleShot(
                    100,
                    lambda: self.reset_completed_progress(completion_cycle),
                )
            else:
                self.reset_progress_bar()

    def on_download_failed(self, _category, message, _details):
        self.reset_progress_bar()
        if self.closing:
            self.is_downloading = False
            return
        self.statusBar().showMessage("다운로드에 실패했습니다.")
        QMessageBox.warning(self, "다운로드 실패", message)
        self.is_downloading = False
        self.process_download_queue()

    def on_download_cancelled(self):
        self.is_downloading = False
        if not self.closing:
            self.reset_progress_bar()
            self.statusBar().showMessage("다운로드가 취소되었습니다.")

    def download_thumbnail(self):
        if self.closing:
            self.is_downloading = False
            return
        self.thumbnail_url_thread = ThumbnailUrlThread(self.url)
        self.thumbnail_url_thread.finished.connect(self.on_thumbnail_url_ready)
        self.thumbnail_url_thread.start()

    def on_thumbnail_url_ready(self, thumbnail_url):
        if self.closing:
            return
        if thumbnail_url:
            self.thumbnail_downloader = ThumbnailDownloader(thumbnail_url)
            self.thumbnail_downloader.finished.connect(self.add_thumbnail_to_list)
            self.thumbnail_downloader.start()
        else:
            logger.warning("썸네일 URL을 확인하지 못했습니다: %s", self.url)
            self.finish_current_download()

    def add_thumbnail_to_list(self, image_data):
        if self.closing:
            return
        pixmap = QPixmap()
        if image_data:
            pixmap.loadFromData(image_data)
        if not pixmap.isNull():
            display_name = self.fileName.rsplit(".", 1)[0]
            item = QListWidgetItem(display_name)
            item.setIcon(pixmap)
            self.listWidget.insertItem(0, item)
        else:
            logger.warning("썸네일 이미지를 표시하지 못했습니다: %s", self.url)
        self.finish_current_download()

    def finish_current_download(self):
        self.is_downloading = False
        self.process_download_queue()

    def load_settings(self):
        global directory
        self.action_3.setChecked(
            self.settings.value("menu_action_3_checked", False, type=bool)
        )
        self.action_4.setChecked(
            self.settings.value("menu_action_4_checked", False, type=bool)
        )
        directory = self.settings.value("download_directory", "", type=str)
        if directory:
            self.statusBar().showMessage(f"파일 경로 => {directory}")

    def save_settings(self):
        self.settings.setValue(
            "menu_action_3_checked", self.action_3.isChecked()
        )
        self.settings.setValue(
            "menu_action_4_checked", self.action_4.isChecked()
        )
        self.settings.setValue("download_directory", directory)

    def set_directory(self):
        global directory
        selected_directory = QFileDialog.getExistingDirectory(
            self, "디렉토리 열기", directory
        )
        if selected_directory:
            directory = selected_directory
            self.statusBar().showMessage(f"파일 경로 => {directory}")
            logger.info("다운로드 경로 변경: %s", directory)
            self.save_settings()

    def closeEvent(self, event):
        if self._allow_close:
            event.accept()
            return

        event.ignore()
        if self.closing:
            return

        self.closing = True
        logger.info("프로그램 종료 요청")
        self.save_settings()
        self.pushButton.setEnabled(False)
        self.clipboard_timer.stop()
        self.queue_timer.stop()
        self.progress_animation_timer.stop()
        self.statusBar().showMessage("실행 중인 작업을 안전하게 종료하고 있습니다...")

        while not self.url_queue.empty():
            try:
                self.url_queue.get_nowait()
            except queue.Empty:
                break

        for thread in self.cancellable_threads():
            thread.cancel()
        self.shutdown_timer.start()
        self.check_shutdown_complete()

    def cancellable_threads(self):
        return [
            thread
            for thread in (
                self.dependency_thread,
                self.yt_dlp_update_thread,
                self.file_download_thread,
                self.thumbnail_url_thread,
                self.thumbnail_downloader,
            )
            if thread is not None and thread.isRunning()
        ]

    def check_shutdown_complete(self):
        running_threads = self.cancellable_threads()
        if running_threads:
            self.statusBar().showMessage(
                f"실행 중인 작업 {len(running_threads)}개를 종료하고 있습니다..."
            )
            return

        self.shutdown_timer.stop()
        logger.info("모든 백그라운드 작업 종료 완료")
        self._allow_close = True
        QTimer.singleShot(0, self.close)


directory = "./downloads"


def main():
    log_path = setup_logging()
    logger.info("YouTubeDownloader 시작; log=%s", log_path)
    app = QApplication(sys.argv)
    exception_notifier = install_exception_hook()
    window = Main_Window()
    window.exception_notifier = exception_notifier
    window.show()
    exit_code = app.exec()
    logger.info("YouTubeDownloader 종료; exit_code=%s", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
