import json
import logging
import os
import re
import subprocess
from pathlib import Path

from dependency_manager import get_tools_directory
from task_control import (
    CancellationToken,
    CREATE_NO_WINDOW,
    OperationCancelled,
    finish_process_termination,
    request_process_termination,
)


FORMAT_POLICY = "bestvideo+bestaudio/best"
PROGRESS_PREFIX = "__YTDLP_PROGRESS__:"
POSTPROCESS_PREFIX = "__YTDLP_POSTPROCESS__:"
FILE_PREFIX = "__YTDLP_FILE__:"
DOWNLOAD_PROGRESS_LIMIT = 97
logger = logging.getLogger(__name__)


class YtDlpDownloadError(RuntimeError):
    def __init__(self, category, user_message, details=""):
        super().__init__(user_message)
        self.category = category
        self.user_message = user_message
        self.details = details


class OverallProgress:
    def __init__(self, format_order, expected_sizes):
        self.format_order = list(format_order)
        self.expected_sizes = dict(expected_sizes)
        self.downloaded_by_format = {format_id: 0 for format_id in format_order}
        self.previous_progress = 0

    def update(
        self,
        format_id,
        downloaded_bytes,
        total_bytes,
        total_bytes_estimate,
        raw_percent,
    ):
        if not format_id or format_id == "NA":
            format_id = self.format_order[0] if self.format_order else "download"
        if format_id not in self.format_order:
            self.format_order.append(format_id)
            self.downloaded_by_format[format_id] = 0

        reported_total = total_bytes or total_bytes_estimate
        if not self.expected_sizes.get(format_id) and reported_total:
            self.expected_sizes[format_id] = reported_total

        if downloaded_bytes is not None:
            self.downloaded_by_format[format_id] = max(
                self.downloaded_by_format.get(format_id, 0), downloaded_bytes
            )

        all_sizes_known = bool(self.format_order) and all(
            self.expected_sizes.get(item, 0) > 0 for item in self.format_order
        )
        if all_sizes_known:
            total_expected = sum(self.expected_sizes[item] for item in self.format_order)
            total_downloaded = sum(
                min(
                    self.downloaded_by_format.get(item, 0),
                    self.expected_sizes[item],
                )
                for item in self.format_order
            )
            ratio = total_downloaded / total_expected if total_expected else 0
        else:
            # The selected formats or their sizes are incomplete. Use the current
            # format stage while keeping the final value monotonic.
            stage_count = max(1, len(self.format_order))
            stage_index = self.format_order.index(format_id)
            if reported_total and downloaded_bytes is not None:
                stage_ratio = min(1.0, downloaded_bytes / reported_total)
            else:
                stage_ratio = max(0.0, min(1.0, (raw_percent or 0) / 100))
            ratio = (stage_index + stage_ratio) / stage_count

        calculated = min(DOWNLOAD_PROGRESS_LIMIT, int(ratio * DOWNLOAD_PROGRESS_LIMIT))
        self.previous_progress = max(self.previous_progress, calculated)
        return self.previous_progress


def _common_arguments(tools_directory):
    tools_directory = Path(tools_directory)
    arguments = [
        str(tools_directory / "yt-dlp.exe"),
        "--ignore-config",
        "--no-color",
        "--encoding",
        "utf-8",
        "--ffmpeg-location",
        str(tools_directory),
    ]

    deno_path = tools_directory / "deno.exe"
    if deno_path.is_file():
        arguments.extend(["--js-runtimes", f"deno:{deno_path}"])
    return arguments


def _subprocess_environment():
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return environment


def _notify_process(process_callback, process):
    if process_callback:
        process_callback(process)


def _capture_process(command, cancel_token, process_callback=None, timeout=None):
    cancel_token.raise_if_cancelled()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_environment(),
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise YtDlpDownloadError(
            "unexpected", "yt-dlp를 실행하지 못했습니다.", str(exc)
        ) from exc

    _notify_process(process_callback, process)
    elapsed_steps = 0
    try:
        while True:
            cancel_token.raise_if_cancelled()
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                return process.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                elapsed_steps += 1
                if timeout is not None and elapsed_steps * 0.2 >= timeout:
                    request_process_termination(process)
                    finish_process_termination(process)
                    raise subprocess.TimeoutExpired(command, timeout)
    except OperationCancelled:
        request_process_termination(process)
        finish_process_termination(process)
        raise
    except Exception:
        request_process_termination(process)
        finish_process_termination(process)
        raise
    finally:
        _notify_process(process_callback, None)


def inspect_selected_formats(
    video_url,
    cancel_token=None,
    process_callback=None,
    status_callback=None,
):
    cancel_token = cancel_token or CancellationToken()
    if status_callback:
        status_callback("영상 정보를 확인하고 있습니다...")

    command = _common_arguments(get_tools_directory()) + [
        "--dump-single-json",
        "--simulate",
        "--format",
        FORMAT_POLICY,
        video_url,
    ]
    try:
        return_code, stdout, stderr = _capture_process(
            command, cancel_token, process_callback, timeout=45
        )
    except subprocess.TimeoutExpired as exc:
        raise YtDlpDownloadError(
            "network",
            "영상 정보 확인 시간이 초과되었습니다.\n"
            "인터넷 연결을 확인한 뒤 다시 시도해주세요.",
            str(exc),
        ) from exc
    if return_code != 0:
        details = "\n".join(part for part in (stdout, stderr) if part)
        raise _classify_download_error(details)

    try:
        info = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise YtDlpDownloadError(
            "yt-dlp",
            "영상 형식 정보를 확인하지 못했습니다.",
            stdout or stderr,
        ) from exc

    selected_formats = info.get("requested_formats") or [info]
    format_order = []
    expected_sizes = {}
    for selected_format in selected_formats:
        format_id = str(selected_format.get("format_id") or "download")
        format_order.append(format_id)
        expected_size = selected_format.get("filesize") or selected_format.get(
            "filesize_approx"
        )
        if expected_size:
            expected_sizes[format_id] = int(expected_size)

    logger.info("yt-dlp 선택 포맷: %s", "+".join(format_order))
    for format_id in format_order:
        logger.info(
            "yt-dlp 포맷 예상 크기: format=%s, bytes=%s",
            format_id,
            expected_sizes.get(format_id, "unknown"),
        )
    return format_order, expected_sizes


def download_video(
    youtube_url,
    output_directory,
    progress_callback=None,
    status_callback=None,
    cancel_token=None,
    process_callback=None,
):
    cancel_token = cancel_token or CancellationToken()
    format_order, expected_sizes = inspect_selected_formats(
        youtube_url,
        cancel_token,
        process_callback,
        status_callback,
    )
    progress_state = OverallProgress(format_order, expected_sizes)

    tools_directory = get_tools_directory()
    output_template = str(Path(output_directory) / "%(title)s.%(ext)s")
    command = _common_arguments(tools_directory) + [
        "--newline",
        "--progress",
        "--progress-template",
        (
            "download:"
            f"{PROGRESS_PREFIX}%(info.format_id)s|"
            "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
            "%(progress.total_bytes_estimate)s|%(progress._percent_str)s"
        ),
        "--progress-template",
        f"postprocess:{POSTPROCESS_PREFIX}%(progress.status)s",
        "--print",
        f"after_move:{FILE_PREFIX}%(filepath)s",
        "--no-simulate",
        "--format",
        FORMAT_POLICY,
        "--merge-output-format",
        "mp4",
        "--output",
        output_template,
        youtube_url,
    ]

    cancel_token.raise_if_cancelled()
    logger.info("다운로드 시작: %s", youtube_url)
    if status_callback:
        status_callback("다운로드를 시작하고 있습니다...")

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_subprocess_environment(),
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise YtDlpDownloadError(
            "unexpected", "yt-dlp를 실행하지 못했습니다.", str(exc)
        ) from exc

    _notify_process(process_callback, process)
    downloaded_path = None
    output_lines = []
    merge_notified = False
    last_emitted_progress = -1
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            cancel_token.raise_if_cancelled()
            line = raw_line.rstrip("\r\n")
            output_lines.append(line)
            logger.debug("yt-dlp: %s", line)

            if line.startswith(PROGRESS_PREFIX):
                progress = _parse_progress_line(line, progress_state)
                if progress_callback and progress > last_emitted_progress:
                    last_emitted_progress = progress
                    progress_callback(progress)
            elif line.startswith(POSTPROCESS_PREFIX):
                if not merge_notified:
                    merge_notified = True
                    if progress_callback:
                        progress_callback(98)
                    if status_callback:
                        status_callback("영상과 음성을 병합하고 있습니다...")
            elif line.startswith(FILE_PREFIX):
                downloaded_path = line[len(FILE_PREFIX) :].strip()

        return_code = process.wait()
        cancel_token.raise_if_cancelled()
    except OperationCancelled:
        request_process_termination(process)
        finish_process_termination(process)
        logger.info("다운로드 취소: %s", youtube_url)
        raise
    except Exception:
        request_process_termination(process)
        finish_process_termination(process)
        raise
    finally:
        _notify_process(process_callback, None)
        if process.stdout:
            process.stdout.close()

    details = "\n".join(output_lines)
    if return_code != 0:
        raise _classify_download_error(details)
    if not downloaded_path:
        raise YtDlpDownloadError(
            "yt-dlp",
            "다운로드는 끝났지만 저장된 파일 경로를 확인하지 못했습니다.",
            details,
        )

    if progress_callback:
        progress_callback(100)
    logger.info("다운로드 완료: %s", downloaded_path)
    return os.path.basename(downloaded_path)


def get_thumbnail_url(
    video_url,
    timeout=30,
    cancel_token=None,
    process_callback=None,
):
    cancel_token = cancel_token or CancellationToken()
    command = _common_arguments(get_tools_directory()) + [
        "--skip-download",
        "--no-warnings",
        "--print",
        "thumbnail",
        video_url,
    ]
    try:
        return_code, stdout, stderr = _capture_process(
            command, cancel_token, process_callback, timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("썸네일 URL 확인 실패: %s", exc)
        return None

    if return_code != 0:
        logger.warning("썸네일 URL 확인 실패: %s", stderr.strip())
        return None

    for line in reversed(stdout.splitlines()):
        if line.startswith(("https://", "http://")):
            return line.strip()
    return None


def _parse_number(value):
    if not value or value in {"NA", "None", "null"}:
        return None
    try:
        return int(float(value.strip()))
    except (TypeError, ValueError):
        return None


def _parse_progress_line(line, progress_state):
    payload = line[len(PROGRESS_PREFIX) :]
    parts = payload.split("|", 4)
    parts.extend([""] * (5 - len(parts)))
    format_id, downloaded, total, estimate, percent_text = parts
    percent_match = re.search(r"([0-9]+(?:\.[0-9]+)?)%", percent_text)
    raw_percent = float(percent_match.group(1)) if percent_match else None
    return progress_state.update(
        format_id.strip(),
        _parse_number(downloaded),
        _parse_number(total),
        _parse_number(estimate),
        raw_percent,
    )


def _classify_download_error(details):
    normalized = details.lower()

    if "http error 403" in normalized or "403: forbidden" in normalized:
        return YtDlpDownloadError(
            "http_403",
            "YouTube가 영상 요청을 거부했습니다.\n"
            "yt-dlp 업데이트를 확인한 뒤 다시 시도해주세요.",
            details,
        )

    network_markers = (
        "unable to download webpage",
        "unable to download api page",
        "network is unreachable",
        "name resolution",
        "connection refused",
        "connection reset",
        "timed out",
        "transporterror",
        "winerror 10060",
        "winerror 10061",
    )
    if any(marker in normalized for marker in network_markers):
        return YtDlpDownloadError(
            "network",
            "네트워크 오류로 영상을 다운로드하지 못했습니다.\n"
            "인터넷 연결을 확인해주세요.",
            details,
        )

    ffmpeg_markers = (
        "ffmpeg not found",
        "ffprobe not found",
        "postprocessing:",
        "conversion failed",
        "error opening output files",
    )
    if any(marker in normalized for marker in ffmpeg_markers):
        return YtDlpDownloadError(
            "ffmpeg",
            "FFmpeg 처리 중 오류가 발생했습니다.\n"
            "도구 준비 상태와 저장 공간을 확인해주세요.",
            details,
        )

    if "error:" in normalized:
        return YtDlpDownloadError(
            "yt-dlp",
            "yt-dlp가 영상을 다운로드하지 못했습니다.\n"
            "URL 또는 영상의 공개 상태를 확인해주세요.",
            details,
        )

    return YtDlpDownloadError(
        "unexpected",
        "예상하지 못한 다운로드 오류가 발생했습니다.",
        details,
    )
