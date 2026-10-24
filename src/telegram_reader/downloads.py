"""Background downloads of Telegram media into a directory the agent shares.

The agent can usually create, rename and delete entries in that directory. Every write therefore goes through directory descriptors
opened with O_NOFOLLOW, files are only ever created with O_EXCL, and a finished
download is published with link(), which fails instead of replacing anything
that appeared under the final name. A symlink planted by anyone with write
access can make a download fail, but cannot redirect or overwrite a file.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field

from mcp.server.fastmcp.exceptions import ToolError

# Leave room for the rest of the media tree when downloads fill the disk.
FREE_SPACE_MARGIN = 1024**3
# Each download streams through the same proxy as the read tools.
MAX_PARALLEL = 2
# Queued plus running, room for two full albums. A looping agent gets an
# error instead of piling up jobs. Queued jobs hold no file descriptors.
MAX_ACTIVE_JOBS = 20
# Finished and failed jobs kept for download_status; older ones are forgotten.
MAX_REMEMBERED_JOBS = 200
# Below the 255-byte file name limit, leaving room for the ".<name>.part" form.
NAME_BYTES = 180
# No execute bits: files from Telegram are data. The service user owns them,
# so the agent cannot chmod them either.
FILE_MODE = 0o664
DOWNLOADABLE = ("photo", "video", "voice", "audio", "sticker", "gif", "document")
EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,10}")
# The model pastes these names into shell commands, and Windows cannot show
# some of them over a network share. Brackets survive because release
# names use them and the " [<id>]" suffix needs them.
UNSAFE_NAME_CHARS = re.compile(r"""[$`\\"';|&<>*?{}!:/]""")
# Used when a post has neither text nor an original file name.
FALLBACK_BASE = "telegram"
CHAT_DIR_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
PART_RE = re.compile(r"\..+\.part")
ACTIVE = ("queued", "running")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

log = logging.getLogger("telegram_reader.downloads")


def _clean(text: str) -> str:
    # Cf covers the bidi overrides that make "gpj.exe" display as "exe.jpg".
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = UNSAFE_NAME_CHARS.sub("_", " ".join(text.split()))
    # Leading dots hide a file; trailing dots and spaces vanish on Windows.
    return text.strip(". ")


def file_ext(msg) -> str:
    """The original file's extension, else the one Telethon derives from the MIME type."""
    original = getattr(msg.file, "name", None) or ""
    for ext in (os.path.splitext(original)[1], getattr(msg.file, "ext", None) or ""):
        if EXT_RE.fullmatch(ext):
            return ext.lower()
    return ""


def default_base(msg) -> str:
    """The first line of the post text, else the original file name without its extension."""
    for line in (getattr(msg, "message", None) or "").splitlines():
        if _clean(line):
            return line
    original = getattr(msg.file, "name", None) or ""
    return os.path.splitext(original)[0] or FALLBACK_BASE


def file_name(msg, base: str | None = None, index: str | None = None) -> str:
    """Build "<base>[ - <index>] [<message id>]<ext>".

    base is the title the agent derived from the post. The message id keeps two
    posts with the same title apart and leads back to the post.
    """
    ext = file_ext(msg)
    stem = _clean(base or "") or _clean(default_base(msg)) or FALLBACK_BASE
    if ext and stem.lower().endswith(ext):
        stem = stem[: -len(ext)].rstrip(". ") or FALLBACK_BASE
    tail = (f" - {index}" if index else "") + f" [{msg.id}]{ext}"
    budget = NAME_BYTES - len(tail.encode())
    stem = stem.encode()[:budget].decode(errors="ignore").rstrip(". ") or FALLBACK_BASE
    return stem + tail


def media_kind(msg) -> str | None:
    return next((kind for kind in DOWNLOADABLE if getattr(msg, kind, None)), None)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, (ToolError, RuntimeError, ConnectionError)):
        return str(exc) or type(exc).__name__
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    # Telethon errors carry the request in str(); the class name is enough.
    return type(exc).__name__


@dataclass
class Job:
    job_id: str
    chat: str
    chat_dir: str
    name: str
    message_id: int
    path: str
    size: int | None
    downloaded: int = 0
    status: str = "queued"
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def progress(self, current: int, total: int) -> None:
        self.downloaded = current
        if total and self.size is None:
            self.size = total

    def outstanding(self) -> int:
        return max(0, (self.size or 0) - self.downloaded)

    def view(self) -> dict:
        out = {
            "job_id": self.job_id,
            "chat": self.chat,
            "message_id": self.message_id,
            "status": self.status,
            "path": self.path,
            "size": self.size,
            "downloaded": self.downloaded,
        }
        if self.error:
            out["error"] = self.error
        return out


class Downloads:
    def __init__(self, client, root: str, max_parallel: int = MAX_PARALLEL):
        self._client = client
        self._root = root
        self._slots = asyncio.Semaphore(max_parallel)
        self._jobs: dict[str, Job] = {}
        # Files owned by this uid are ones the service wrote; anything else is the agent's.
        self.owner_uid = os.geteuid()

    @staticmethod
    def free_bytes(dir_fd: int) -> int:
        st = os.fstatvfs(dir_fd)
        return st.f_bavail * st.f_frsize

    def _open_root(self) -> int:
        try:
            return os.open(self._root, DIR_FLAGS)
        except FileNotFoundError as exc:
            raise ToolError(f"download directory {self._root} is missing") from exc
        except OSError as exc:
            raise self._open_error(self._root, exc) from exc

    @staticmethod
    def _open_error(path: str, exc: OSError) -> ToolError:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return ToolError(f"{path} is not a plain directory; refusing to write there")
        return ToolError(f"cannot open {path}: {exc.strerror}")

    def _open_chat_dir(self, chat_dir: str) -> int:
        root_fd = self._open_root()
        try:
            # mkdir inherits the media tree's setgid group and default ACLs.
            with contextlib.suppress(FileExistsError):
                os.mkdir(chat_dir, 0o775, dir_fd=root_fd)
            try:
                return os.open(chat_dir, DIR_FLAGS, dir_fd=root_fd)
            except OSError as exc:
                raise self._open_error(f"{self._root}/{chat_dir}", exc) from exc
        finally:
            os.close(root_fd)

    def _remember(self, job: Job) -> None:
        self._jobs[job.job_id] = job
        finished = [j for j in self._jobs.values() if j.status not in ACTIVE]
        for old in finished[: max(0, len(self._jobs) - MAX_REMEMBERED_JOBS)]:
            del self._jobs[old.job_id]

    def free_slots(self) -> int:
        return MAX_ACTIVE_JOBS - sum(1 for j in self._jobs.values() if j.status in ACTIVE)

    def _known_job(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job is None or job.status == "failed":
            return None
        if job.status == "done":
            # The agent is expected to move finished files away; then the job is stale.
            try:
                os.lstat(job.path)
            except FileNotFoundError:
                del self._jobs[job_id]
                return None
        return job

    async def start(
        self, job_id: str, chat_dir: str, chat_title: str, msg, base: str | None = None, index: str | None = None
    ) -> dict:
        job = self._known_job(job_id)
        if job is not None:
            return job.view()
        if not CHAT_DIR_RE.fullmatch(chat_dir):
            raise ToolError(f"refusing chat directory name {chat_dir!r}")
        if media_kind(msg) is None:
            raise ToolError("message has no downloadable file (web previews, polls, locations and contacts are not files)")
        size = getattr(msg.file, "size", None)
        active = [j for j in self._jobs.values() if j.status in ACTIVE]
        if len(active) >= MAX_ACTIVE_JOBS:
            raise ToolError(f"{len(active)} downloads are already queued or running; wait for some to finish")
        name = file_name(msg, base, index)
        path = os.path.join(self._root, chat_dir, name)
        dir_fd = self._open_chat_dir(chat_dir)
        try:
            with contextlib.suppress(FileNotFoundError):
                existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISREG(existing.st_mode) and size is not None and existing.st_size == size:
                    job = Job(job_id, chat_title, chat_dir, name, msg.id, path, size, downloaded=size, status="done")
                    self._remember(job)
                    return job.view()
                raise ToolError(f"{path} already exists and is not this file; move it away first")
            # Queued jobs have written nothing yet, so their bytes are reserved here.
            reserved = sum(j.outstanding() for j in active)
            if self.free_bytes(dir_fd) < (size or 0) + reserved + FREE_SPACE_MARGIN:
                raise ToolError("not enough free space under the download directory")
        finally:
            os.close(dir_fd)
        job = Job(job_id, chat_title, chat_dir, name, msg.id, path, size)
        self._remember(job)
        job.task = asyncio.create_task(self._run(job, msg))
        return job.view()

    def _create_part(self, dir_fd: int, part: str) -> int:
        try:
            return os.open(part, CREATE_FLAGS, FILE_MODE, dir_fd=dir_fd)
        except FileExistsError:
            st = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
            # Only a leftover from a download this service abandoned is ours to replace.
            if not (stat.S_ISREG(st.st_mode) and st.st_uid == self.owner_uid):
                raise RuntimeError(f"{part} exists and was not left by this service") from None
            os.unlink(part, dir_fd=dir_fd)
            return os.open(part, CREATE_FLAGS, FILE_MODE, dir_fd=dir_fd)

    async def _fetch_into(self, job: Job, msg, dir_fd: int, part: str) -> None:
        fd = self._create_part(dir_fd, part)
        try:
            with os.fdopen(fd, "wb") as out:
                await self._client.download_media(msg, file=out, progress_callback=job.progress)
                out.flush()
                # A multi-GiB fsync on virtiofs would stall every other request on the loop.
                await asyncio.to_thread(os.fsync, out.fileno())
                written = os.fstat(out.fileno())
                if job.size is not None and written.st_size != job.size:
                    raise RuntimeError(f"download incomplete: got {written.st_size} of {job.size} bytes")
                # The agent can rename the part file away and put its own in its place.
                named = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (written.st_dev, written.st_ino):
                    raise RuntimeError("the partial file was replaced during the download")
                job.downloaded = job.size = written.st_size
            os.link(part, job.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(part, dir_fd=dir_fd)

    async def _run(self, job: Job, msg) -> None:
        try:
            async with self._slots:
                job.status = "running"
                # Opened only now, so queued jobs hold no descriptors.
                dir_fd = self._open_chat_dir(job.chat_dir)
                try:
                    await self._fetch_into(job, msg, dir_fd, f".{job.name}.part")
                finally:
                    os.close(dir_fd)
                job.status = "done"
        except asyncio.CancelledError:
            job.status, job.error = "failed", "cancelled when the service stopped"
            raise
        except FileExistsError:
            job.status, job.error = "failed", f"{job.path} appeared while downloading; left it untouched"
        except Exception as exc:
            job.status, job.error = "failed", _describe(exc)
            log.warning("download %s failed: %s", job.job_id, job.error)

    def sweep_stale_parts(self) -> int:
        """Remove partial files this service left behind when it was killed mid-download."""
        try:
            root_fd = os.open(self._root, DIR_FLAGS)
        except OSError:
            return 0
        removed = 0
        try:
            for chat in os.scandir(root_fd):
                if not chat.is_dir(follow_symlinks=False):
                    continue
                try:
                    chat_fd = os.open(chat.name, DIR_FLAGS, dir_fd=root_fd)
                except OSError:
                    continue
                try:
                    for entry in os.scandir(chat_fd):
                        if not PART_RE.fullmatch(entry.name) or not entry.is_file(follow_symlinks=False):
                            continue
                        if entry.stat(follow_symlinks=False).st_uid == self.owner_uid:
                            os.unlink(entry.name, dir_fd=chat_fd)
                            removed += 1
                finally:
                    os.close(chat_fd)
        finally:
            os.close(root_fd)
        return removed

    def status(self, job_id: str = "") -> dict:
        if not job_id:
            return {"jobs": [job.view() for job in self._jobs.values()]}
        job = self._jobs.get(job_id)
        if job is None:
            raise ToolError("unknown job; jobs do not survive a service restart, so start the download again")
        return job.view()

    async def wait(self, job_id: str) -> dict:
        job = self._jobs[job_id]
        if job.task is not None:
            await asyncio.shield(job.task)
        return job.view()
