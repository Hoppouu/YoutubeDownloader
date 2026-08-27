import logging
import os
import sys
import tempfile
import threading
from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from dependency_manager import get_application_directory


class SingleLineExceptionFormatter(logging.Formatter):
    def formatException(self, exc_info):
        return super().formatException(exc_info).replace("\n", " | ")


class _ExceptionNotifier(QObject):
    showRequested = Signal()


def setup_logging():
    application_logs = get_application_directory() / "logs"
    fallback_root = Path(
        os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    )
    candidate_directories = [
        application_logs,
        fallback_root / "YouTubeDownloader" / "logs",
    ]

    handler = None
    log_path = None
    for logs_directory in candidate_directories:
        try:
            logs_directory.mkdir(parents=True, exist_ok=True)
            log_path = logs_directory / f"{date.today().isoformat()}.txt"
            handler = logging.FileHandler(log_path, encoding="utf-8")
            break
        except OSError:
            continue
    if handler is None or log_path is None:
        raise OSError("로그 파일을 저장할 writable 위치를 찾지 못했습니다.")

    handler.setFormatter(
        SingleLineExceptionFormatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    for existing_handler in root_logger.handlers[:]:
        root_logger.removeHandler(existing_handler)
        existing_handler.close()
    root_logger.addHandler(handler)
    logging.captureWarnings(True)
    if log_path.parent != application_logs:
        root_logger.warning(
            "실행 파일 옆에 로그를 저장할 수 없어 fallback 경로를 사용합니다: %s",
            log_path,
        )
    return log_path


def install_exception_hook():
    logger = logging.getLogger("unhandled")

    notifier = _ExceptionNotifier()

    def show_error_message():
        app = QApplication.instance()
        if app is not None:
            QMessageBox.critical(
                app.activeWindow(),
                "예상하지 못한 오류",
                "예상하지 못한 오류가 발생했습니다.\n\n"
                "자세한 내용은 logs 폴더의 로그 파일을 확인해주세요.",
            )

    notifier.showRequested.connect(show_error_message)

    def handle_exception(exc_type, exc_value, traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, traceback)
            return

        logger.critical(
            "처리되지 않은 예외가 발생했습니다.",
            exc_info=(exc_type, exc_value, traceback),
        )
        notifier.showRequested.emit()

    def handle_thread_exception(args):
        handle_exception(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception
    return notifier
