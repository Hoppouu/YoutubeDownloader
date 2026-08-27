import os
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import requests

from task_control import CancellationToken, CREATE_NO_WINDOW


MIN_YT_DLP_VERSION = "2026.08.19"

# Official yt-dlp release endpoints
YT_DLP_LATEST_API_URL = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
YT_DLP_DOWNLOAD_URL = (
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
)

# FFmpeg's official download page links to gyan.dev for Windows builds.
FFMPEG_DOWNLOAD_URL = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)

# Deno is yt-dlp's recommended JavaScript runtime and is distributed as one exe.
DENO_DOWNLOAD_URL = (
    "https://github.com/denoland/deno/releases/latest/download/"
    "deno-x86_64-pc-windows-msvc.zip"
)

HTTP_HEADERS = {"User-Agent": "YouTubeDownloader/2.0"}
HTTP_TIMEOUT = (15, 60)
DOWNLOAD_RETRY_DELAYS = (1, 2, 4)
RETRYABLE_HTTP_STATUS_CODES = {408, 416, 429, 500, 502, 503, 504}
logger = logging.getLogger(__name__)


class DependencyError(RuntimeError):
    def __init__(self, message, category="general"):
        super().__init__(message)
        self.category = category


def classify_dependency_error(error):
    """Return a stable UI category without exposing technical details."""
    current = error
    seen = set()
    chain = []
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__

    for item in chain:
        if isinstance(item, DependencyError) and item.category != "general":
            return item.category
    if any(isinstance(item, PermissionError) for item in chain):
        return "permission"
    if any(isinstance(item, zipfile.BadZipFile) for item in chain):
        return "archive"
    if any(isinstance(item, requests.RequestException) for item in chain):
        return "network"

    winerrors = {getattr(item, "winerror", None) for item in chain}
    if 5 in winerrors:
        return "permission"
    if 32 in winerrors:
        return "security"

    details = " ".join(str(item).lower() for item in chain)
    if any(marker in details for marker in ("bad zip", "zip file", "압축")):
        return "archive"
    if any(
        marker in details
        for marker in (
            "connection",
            "timed out",
            "timeout",
            "name resolution",
            "http error",
            "인터넷",
        )
    ):
        return "network"
    if any(
        marker in details
        for marker in ("올바른 windows 실행 파일", "검증에 실패", "quarantine")
    ):
        return "security"
    if any(isinstance(item, OSError) for item in chain):
        return "filesystem"
    return "general"


def get_application_directory():
    """Return a persistent, writable directory beside the source or built exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_tools_directory():
    return get_application_directory() / "tools"


def _version_key(version):
    match = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", version or "")
    if not match:
        raise DependencyError(f"버전 형식을 확인할 수 없습니다: {version}")
    return tuple(int(part) for part in match.groups())


def _run_hidden(command, timeout=15, cancel_token=None):
    if cancel_token:
        cancel_token.raise_if_cancelled()
    try:
        result = subprocess.run(
            [str(part) for part in command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except PermissionError as exc:
        raise DependencyError(str(exc), "permission") from exc
    except OSError as exc:
        category = "security" if getattr(exc, "winerror", None) == 32 else "filesystem"
        raise DependencyError(str(exc), category) from exc
    except subprocess.SubprocessError as exc:
        raise DependencyError(str(exc), "execution") from exc
    if cancel_token:
        cancel_token.raise_if_cancelled()
    return result


def _download_file(
    url,
    destination,
    cancel_token=None,
    progress_callback=None,
    retry_callback=None,
):
    cancel_token = cancel_token or CancellationToken()
    destination = Path(destination)
    cancel_token.raise_if_cancelled()
    logger.info("도구 파일 다운로드 시작: %s", url)
    last_error = None
    for attempt in range(len(DOWNLOAD_RETRY_DELAYS) + 1):
        try:
            _download_file_once(
                url,
                destination,
                cancel_token,
                progress_callback,
            )
            last_error = None
            break
        except PermissionError as exc:
            raise DependencyError(str(exc), "permission") from exc
        except requests.RequestException as exc:
            last_error = exc
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status_code is None or status_code in RETRYABLE_HTTP_STATUS_CODES
            if not retryable or attempt >= len(DOWNLOAD_RETRY_DELAYS):
                break

            retry_number = attempt + 1
            logger.warning(
                "도구 파일 다운로드 재시도 %d/%d: %s (%s)",
                retry_number,
                len(DOWNLOAD_RETRY_DELAYS),
                url,
                exc,
            )
            if retry_callback:
                retry_callback(
                    retry_number,
                    len(DOWNLOAD_RETRY_DELAYS),
                    str(exc),
                )
            if cancel_token.wait(DOWNLOAD_RETRY_DELAYS[attempt]):
                cancel_token.raise_if_cancelled()
        except OSError as exc:
            category = (
                "security" if getattr(exc, "winerror", None) == 32 else "filesystem"
            )
            raise DependencyError(str(exc), category) from exc

    if last_error is not None:
        raise DependencyError(str(last_error), "network") from last_error

    if not destination.is_file() or destination.stat().st_size == 0:
        raise DependencyError("다운로드한 파일이 비어 있습니다.", "integrity")
    logger.info("도구 파일 다운로드 완료: %s (%d bytes)", url, destination.stat().st_size)


def _download_file_once(url, destination, cancel_token, progress_callback):
    resume_offset = destination.stat().st_size if destination.is_file() else 0
    headers = dict(HTTP_HEADERS)
    if resume_offset:
        headers["Range"] = f"bytes={resume_offset}-"

    with requests.get(
        url,
        headers=headers,
        stream=True,
        timeout=HTTP_TIMEOUT,
        allow_redirects=True,
    ) as response:
        if response.status_code == 416 and resume_offset:
            # The remote object changed, or the local partial file is already too long.
            destination.unlink(missing_ok=True)
            raise requests.HTTPError(
                "서버가 부분 다운로드 범위를 거부했습니다.", response=response
            )
        response.raise_for_status()

        resumed = resume_offset > 0 and response.status_code == 206
        if not resumed:
            resume_offset = 0
        total_bytes = _get_response_total_bytes(response.headers, resume_offset)
        downloaded_bytes = resume_offset
        if progress_callback:
            progress_callback(downloaded_bytes, total_bytes)

        mode = "ab" if resumed else "wb"
        with open(destination, mode) as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                cancel_token.raise_if_cancelled()
                if not chunk:
                    continue
                output.write(chunk)
                downloaded_bytes += len(chunk)
                if progress_callback:
                    progress_callback(downloaded_bytes, total_bytes)

        if total_bytes and downloaded_bytes < total_bytes:
            raise requests.ConnectionError(
                f"다운로드가 중간에 끝났습니다: {downloaded_bytes}/{total_bytes} bytes"
            )

    if progress_callback and not total_bytes:
        progress_callback(downloaded_bytes, downloaded_bytes)


def _get_response_total_bytes(headers, resume_offset):
    content_range = headers.get("Content-Range", "")
    match = re.search(r"/(\d+)\s*$", content_range)
    if match:
        return int(match.group(1))
    try:
        content_length = int(headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        content_length = 0
    return resume_offset + content_length if content_length else 0


def _new_staging_path(tools_directory, prefix):
    descriptor, path = tempfile.mkstemp(
        prefix=f".{prefix}-", suffix=".download.exe", dir=tools_directory
    )
    os.close(descriptor)
    return Path(path)


def _has_windows_executable_header(path):
    try:
        with open(path, "rb") as executable:
            return executable.read(2) == b"MZ"
    except OSError:
        return False


def _extract_zip_member(archive, member_name, destination, cancel_token=None):
    cancel_token = cancel_token or CancellationToken()
    with archive.open(member_name) as source, open(destination, "wb") as output:
        while chunk := source.read(1024 * 1024):
            cancel_token.raise_if_cancelled()
            output.write(chunk)


def _remove_directory_safely(path, attempts=3):
    path = Path(path)
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if attempt == attempts - 1:
                logger.warning("임시 디렉터리 정리 실패: %s (%s)", path, exc)
                return False
            time.sleep(0.2)
    return False


@contextmanager
def _managed_temp_directory(prefix):
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        _remove_directory_safely(path)


def cleanup_stale_downloads(tools_directory=None, minimum_age_seconds=300):
    """Remove leftovers from an older crashed instance without touching active work."""
    now = time.time()
    tools_directory = Path(tools_directory or get_tools_directory())
    for pattern in ("youtube-downloader-ffmpeg-*", "youtube-downloader-deno-*"):
        for path in Path(tempfile.gettempdir()).glob(pattern):
            try:
                if now - path.stat().st_mtime >= minimum_age_seconds:
                    _remove_directory_safely(path)
            except OSError as exc:
                logger.debug("오래된 임시 디렉터리 확인 실패: %s (%s)", path, exc)

    if tools_directory.is_dir():
        for path in tools_directory.glob(".*.download.exe"):
            try:
                if now - path.stat().st_mtime >= minimum_age_seconds:
                    path.unlink(missing_ok=True)
                    logger.info("오래된 staging 파일 정리: %s", path)
            except OSError as exc:
                logger.debug("오래된 staging 파일 정리 실패: %s (%s)", path, exc)


def _replace_files_safely(replacements):
    """Replace multiple files and restore the old files if a replacement fails."""
    backups = {}
    replaced = []
    try:
        for staged_path, target_path in replacements:
            if target_path.exists():
                backup_path = _new_staging_path(target_path.parent, target_path.stem)
                backups[target_path] = backup_path
                shutil.copy2(target_path, backup_path)

            os.replace(staged_path, target_path)
            replaced.append(target_path)
    except OSError as exc:
        for target_path in reversed(replaced):
            backup_path = backups.get(target_path)
            try:
                if backup_path and backup_path.exists():
                    os.replace(backup_path, target_path)
                elif target_path.exists():
                    target_path.unlink()
            except OSError:
                pass
        category = "security" if getattr(exc, "winerror", None) == 32 else "filesystem"
        if isinstance(exc, PermissionError):
            category = "permission"
        raise DependencyError(str(exc), category) from exc
    finally:
        for backup_path in backups.values():
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
        for staged_path, _ in replacements:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass


class YtDlpManager:
    def __init__(
        self,
        tools_directory=None,
        cancel_token=None,
        progress_callback=None,
        retry_callback=None,
    ):
        self.tools_directory = Path(tools_directory or get_tools_directory())
        self.executable_path = self.tools_directory / "yt-dlp.exe"
        self.cancel_token = cancel_token or CancellationToken()
        self.progress_callback = progress_callback
        self.retry_callback = retry_callback

    def get_current_version(self):
        if not self.executable_path.is_file():
            return None

        result = _run_hidden(
            [self.executable_path, "--version"], cancel_token=self.cancel_token
        )
        version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if result.returncode != 0 or not version:
            raise DependencyError(result.stderr.strip() or "yt-dlp 버전 확인에 실패했습니다.")
        _version_key(version)
        return version

    def get_latest_version(self):
        self.cancel_token.raise_if_cancelled()
        try:
            with requests.get(
                YT_DLP_LATEST_API_URL,
                headers=HTTP_HEADERS,
                timeout=(5, 10),
            ) as response:
                response.raise_for_status()
                self.cancel_token.raise_if_cancelled()
                version = response.json().get("tag_name", "").lstrip("v")
            _version_key(version)
            return version
        except (ValueError, KeyError, requests.RequestException, DependencyError) as exc:
            raise DependencyError(str(exc)) from exc

    def check_update(self):
        current_version = self.get_current_version()
        latest_version = self.get_latest_version()
        return current_version, latest_version, (
            _version_key(current_version) < _version_key(latest_version)
        )

    def ensure_installed(self):
        self.cancel_token.raise_if_cancelled()
        self.tools_directory.mkdir(parents=True, exist_ok=True)
        if self.executable_path.is_file():
            try:
                return self.get_current_version()
            except DependencyError:
                # A corrupt or non-executable file is replaced only after validating a new one.
                pass
        return self.update()

    def update(self, expected_version=None):
        self.cancel_token.raise_if_cancelled()
        self.tools_directory.mkdir(parents=True, exist_ok=True)
        staged_path = _new_staging_path(self.tools_directory, "yt-dlp")
        try:
            _download_file(
                YT_DLP_DOWNLOAD_URL,
                staged_path,
                self.cancel_token,
                self.progress_callback,
                self.retry_callback,
            )
            if not _has_windows_executable_header(staged_path):
                raise DependencyError(
                    "다운로드한 yt-dlp 파일이 올바른 Windows 실행 파일이 아닙니다.",
                    "security",
                )

            result = _run_hidden(
                [staged_path, "--version"], cancel_token=self.cancel_token
            )
            downloaded_version = (
                result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
            )
            if result.returncode != 0 or not downloaded_version:
                raise DependencyError(
                    "다운로드한 yt-dlp 실행 파일 검증에 실패했습니다.",
                    "security",
                )
            if _version_key(downloaded_version) < _version_key(MIN_YT_DLP_VERSION):
                raise DependencyError(
                    f"yt-dlp {MIN_YT_DLP_VERSION} 이상이 필요합니다."
                )
            if expected_version and _version_key(downloaded_version) < _version_key(
                expected_version
            ):
                raise DependencyError("요청한 최신 yt-dlp 버전보다 오래된 파일입니다.")

            os.replace(staged_path, self.executable_path)
            logger.info("yt-dlp 준비 완료: %s", downloaded_version)
            return downloaded_version
        except PermissionError as exc:
            raise DependencyError(str(exc), "permission") from exc
        except OSError as exc:
            category = "security" if getattr(exc, "winerror", None) == 32 else "filesystem"
            raise DependencyError(str(exc), category) from exc
        finally:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass


class FFmpegManager:
    def __init__(
        self,
        tools_directory=None,
        cancel_token=None,
        progress_callback=None,
        retry_callback=None,
    ):
        self.tools_directory = Path(tools_directory or get_tools_directory())
        self.ffmpeg_path = self.tools_directory / "ffmpeg.exe"
        self.ffprobe_path = self.tools_directory / "ffprobe.exe"
        self.cancel_token = cancel_token or CancellationToken()
        self.progress_callback = progress_callback
        self.retry_callback = retry_callback

    def is_installed(self):
        if not (self.ffmpeg_path.is_file() and self.ffprobe_path.is_file()):
            return False
        return self._validate_executable(self.ffmpeg_path) and self._validate_executable(
            self.ffprobe_path
        )

    def ensure_installed(self):
        self.cancel_token.raise_if_cancelled()
        self.tools_directory.mkdir(parents=True, exist_ok=True)
        if self.is_installed():
            return self.ffmpeg_path, self.ffprobe_path

        staged_ffmpeg = _new_staging_path(self.tools_directory, "ffmpeg")
        staged_ffprobe = _new_staging_path(self.tools_directory, "ffprobe")

        try:
            with _managed_temp_directory("youtube-downloader-ffmpeg-") as temp_dir:
                archive_path = temp_dir / "ffmpeg.zip"
                _download_file(
                    FFMPEG_DOWNLOAD_URL,
                    archive_path,
                    self.cancel_token,
                    self.progress_callback,
                    self.retry_callback,
                )

                try:
                    with zipfile.ZipFile(archive_path) as archive:
                        ffmpeg_member = self._find_binary(archive, "ffmpeg.exe")
                        ffprobe_member = self._find_binary(archive, "ffprobe.exe")
                        _extract_zip_member(
                            archive, ffmpeg_member, staged_ffmpeg, self.cancel_token
                        )
                        _extract_zip_member(
                            archive, ffprobe_member, staged_ffprobe, self.cancel_token
                        )
                except (OSError, zipfile.BadZipFile, KeyError) as exc:
                    category = classify_dependency_error(exc)
                    if category == "general":
                        category = "archive"
                    raise DependencyError(
                        f"FFmpeg 압축 파일을 처리하지 못했습니다: {exc}",
                        category,
                    ) from exc

            if not self._validate_executable(staged_ffmpeg):
                raise DependencyError(
                    "다운로드한 ffmpeg.exe 검증에 실패했습니다.", "security"
                )
            if not self._validate_executable(staged_ffprobe):
                raise DependencyError(
                    "다운로드한 ffprobe.exe 검증에 실패했습니다.", "security"
                )

            _replace_files_safely(
                [
                    (staged_ffmpeg, self.ffmpeg_path),
                    (staged_ffprobe, self.ffprobe_path),
                ]
            )
            logger.info("FFmpeg/FFprobe 준비 완료: %s", self.tools_directory)
            return self.ffmpeg_path, self.ffprobe_path
        finally:
            for staged_path in (staged_ffmpeg, staged_ffprobe):
                try:
                    staged_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _find_binary(archive, filename):
        matches = [
            name
            for name in archive.namelist()
            if name.replace("\\", "/").lower().endswith(f"/bin/{filename}")
        ]
        if not matches:
            raise KeyError(filename)
        return matches[0]

    @staticmethod
    def _validate_executable(path):
        if not _has_windows_executable_header(path):
            return False
        try:
            result = _run_hidden([path, "-version"])
            return result.returncode == 0
        except DependencyError:
            return False


class DenoManager:
    def __init__(
        self,
        tools_directory=None,
        cancel_token=None,
        progress_callback=None,
        retry_callback=None,
    ):
        self.tools_directory = Path(tools_directory or get_tools_directory())
        self.executable_path = self.tools_directory / "deno.exe"
        self.cancel_token = cancel_token or CancellationToken()
        self.progress_callback = progress_callback
        self.retry_callback = retry_callback

    def ensure_installed(self):
        self.cancel_token.raise_if_cancelled()
        self.tools_directory.mkdir(parents=True, exist_ok=True)
        if self._is_supported():
            return self.executable_path

        staged_path = _new_staging_path(self.tools_directory, "deno")
        try:
            with _managed_temp_directory("youtube-downloader-deno-") as temp_dir:
                archive_path = temp_dir / "deno.zip"
                _download_file(
                    DENO_DOWNLOAD_URL,
                    archive_path,
                    self.cancel_token,
                    self.progress_callback,
                    self.retry_callback,
                )
                try:
                    with zipfile.ZipFile(archive_path) as archive:
                        member = next(
                            name
                            for name in archive.namelist()
                            if Path(name).name.lower() == "deno.exe"
                        )
                        _extract_zip_member(
                            archive, member, staged_path, self.cancel_token
                        )
                except (OSError, StopIteration, zipfile.BadZipFile) as exc:
                    category = classify_dependency_error(exc)
                    if category == "general":
                        category = "archive"
                    raise DependencyError(
                        f"Deno 압축 파일을 처리하지 못했습니다: {exc}",
                        category,
                    ) from exc

            if not self._is_supported(staged_path):
                raise DependencyError(
                    "다운로드한 deno.exe 검증에 실패했습니다.", "security"
                )
            os.replace(staged_path, self.executable_path)
            logger.info("Deno 준비 완료: %s", self.executable_path)
            return self.executable_path
        except PermissionError as exc:
            raise DependencyError(str(exc), "permission") from exc
        except OSError as exc:
            category = "security" if getattr(exc, "winerror", None) == 32 else "filesystem"
            raise DependencyError(str(exc), category) from exc
        finally:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _is_supported(self, path=None):
        executable_path = Path(path or self.executable_path)
        if not _has_windows_executable_header(executable_path):
            return False
        try:
            result = _run_hidden([executable_path, "--version"])
            if result.returncode != 0:
                return False
            first_line = result.stdout.strip().splitlines()[0]
            match = re.search(r"deno\s+(\d+)\.(\d+)\.(\d+)", first_line)
            return bool(match and tuple(map(int, match.groups())) >= (2, 3, 0))
        except (DependencyError, IndexError):
            return False
