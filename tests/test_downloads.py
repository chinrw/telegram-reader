"""Unit tests for telegram_reader.downloads. Run: PYTHONPATH=src python -m unittest discover -s tests"""
import asyncio
import errno
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp.server.fastmcp.exceptions import ToolError

from telegram_reader import downloads as dl

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def media_message(msg_id, payload=b"payload", name="file.zip", ext=".zip", kind="document", **extra):
    msg = SimpleNamespace(
        id=msg_id,
        date=NOW,
        file=SimpleNamespace(name=name, ext=ext, size=len(payload)),
        payload=payload,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        sticker=None,
        gif=None,
        document=None,
        web_preview=None,
        poll=None,
    )
    setattr(msg, kind, object())
    for key, value in extra.items():
        setattr(msg, key, value)
    return msg


class FakeDownloader:
    """Writes msg.payload in two chunks; a gate lets tests hold downloads open."""

    def __init__(self):
        self.gate = None
        self.started = []

    async def download_media(self, msg, file=None, progress_callback=None):
        self.started.append(msg.id)
        if self.gate is not None:
            await self.gate.wait()
        if getattr(msg, "explode", None):
            raise msg.explode
        if getattr(msg, "during", None):
            msg.during()
        payload = msg.payload[: getattr(msg, "deliver", len(msg.payload))]
        total = 0 if getattr(msg, "no_total", False) else len(msg.payload)
        half = len(payload) // 2
        for chunk in (payload[:half], payload[half:]):
            file.write(chunk)
            if progress_callback:
                progress_callback(file.tell(), total)
        return file


class DownloadsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "telegram"
        self.root.mkdir()
        self.client = FakeDownloader()
        self.downloads = dl.Downloads(self.client, str(self.root))

    async def finish(self, job_id):
        return await self.downloads.wait(job_id)

    async def changed_download(self, msg_id, change):
        msg = media_message(msg_id)
        job_id = f"-1001:{msg_id}"
        await self.downloads.start(job_id, "newsfeed", "News", msg)
        done = await self.finish(job_id)
        self.assertEqual(done["status"], "done")
        target = Path(done["path"])
        before = target.stat()
        if change == "rewrite":
            target.write_bytes(b"tainted")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        else:
            original = target.with_suffix(".original")
            target.rename(original)
            if change == "symlink":
                target.symlink_to(original)
            else:
                target.write_bytes(msg.payload)
        return msg

    async def test_downloads_into_the_chat_directory_without_exec_bits(self):
        started = await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7))
        self.assertIn(started["status"], ("queued", "running"))
        done = await self.finish("-1001:7")
        target = self.root / "newsfeed" / "file [7].zip"
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["path"], str(target))
        self.assertEqual(target.read_bytes(), b"payload")
        self.assertEqual(done["downloaded"], len(b"payload"))
        self.assertFalse(target.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        self.assertEqual(sorted(p.name for p in (self.root / "newsfeed").iterdir()), ["file [7].zip"])

    async def test_starting_twice_reuses_the_job_within_the_same_process(self):
        msg = media_message(7)
        first = await self.downloads.start("-1001:7", "newsfeed", "News", msg)
        again = await self.downloads.start("-1001:7", "newsfeed", "News", msg)
        self.assertEqual(first["job_id"], again["job_id"])
        await self.finish("-1001:7")
        result = await self.downloads.start("-1001:7", "newsfeed", "News", msg)
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.client.started, [7])

    async def test_a_restart_does_not_trust_previous_downloads(self):
        msg = media_message(7)
        await self.downloads.start("-1001:7", "newsfeed", "News", msg)
        await self.finish("-1001:7")
        fresh = dl.Downloads(self.client, str(self.root))
        with self.assertRaisesRegex(ToolError, "cannot verify"):
            await fresh.start("-1001:7", "newsfeed", "News", msg)
        self.assertEqual(self.client.started, [7])

    async def test_a_different_file_at_the_target_is_never_overwritten(self):
        (self.root / "newsfeed").mkdir()
        (self.root / "newsfeed" / "file [7].zip").write_bytes(b"someone else's")
        with self.assertRaisesRegex(ToolError, "already exists"):
            await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7))
        self.assertEqual((self.root / "newsfeed" / "file [7].zip").read_bytes(), b"someone else's")

    async def test_preexisting_same_size_file_is_not_assumed_complete(self):
        (self.root / "newsfeed").mkdir()
        target = self.root / "newsfeed" / "file [7].zip"
        target.write_bytes(b"tainted")
        with self.assertRaisesRegex(ToolError, "cannot verify"):
            await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7))
        self.assertEqual(target.read_bytes(), b"tainted")
        self.assertEqual(self.client.started, [])

    async def test_status_rechecks_changed_completed_files(self):
        for query in ("one", "all"):
            for change in ("symlink", "replacement", "rewrite"):
                with self.subTest(query=query, change=change):
                    msg_id = len(self.client.started) + 1
                    await self.changed_download(msg_id, change)
                    job_id = f"-1001:{msg_id}"
                    if query == "one":
                        result = self.downloads.status(job_id)
                    else:
                        result = next(job for job in self.downloads.status()["jobs"] if job["job_id"] == job_id)
                    self.assertEqual(result["status"], "failed")
                    self.assertIn("changed", result["error"])

    async def test_reuse_refuses_changed_completed_files(self):
        for msg_id, change in enumerate(("symlink", "replacement", "rewrite"), 1):
            with self.subTest(change=change):
                msg = await self.changed_download(msg_id, change)
                with self.assertRaisesRegex(ToolError, "cannot verify"):
                    await self.downloads.start(f"-1001:{msg_id}", "newsfeed", "News", msg)

    async def test_status_refuses_symlinked_completed_directories(self):
        for msg_id, directory in enumerate(("chat", "root"), 1):
            with self.subTest(directory=directory):
                msg = media_message(msg_id)
                job_id = f"-1001:{msg_id}"
                await self.downloads.start(job_id, "newsfeed", "News", msg)
                await self.finish(job_id)
                target = self.root / "newsfeed" if directory == "chat" else self.root
                moved = target.with_name(f"moved-{directory}")
                target.rename(moved)
                target.symlink_to(moved, target_is_directory=True)
                self.assertEqual(self.downloads.status(job_id)["status"], "failed")
                target.unlink()
                moved.rename(target)

    async def test_planted_symlinks_are_never_followed(self):
        victim = Path(self.tmp.name) / "victim"
        victim.write_bytes(b"keep")
        (self.root / "newsfeed").mkdir()
        (self.root / "newsfeed" / "file [7].zip").symlink_to(victim)
        with self.assertRaisesRegex(ToolError, "already exists"):
            await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7))
        (self.root / "newsfeed" / ".file [8].zip.part").symlink_to(victim)
        await self.downloads.start("-1001:8", "newsfeed", "News", media_message(8))
        failed = await self.finish("-1001:8")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(victim.read_bytes(), b"keep")
        self.assertTrue((self.root / "newsfeed" / ".file [8].zip.part").is_symlink())

    async def test_a_file_that_appears_mid_download_is_left_alone(self):
        self.client.gate = asyncio.Event()
        await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7))
        for _ in range(3):
            await asyncio.sleep(0)
        (self.root / "newsfeed" / "file [7].zip").write_bytes(b"placed by the agent")
        self.client.gate.set()
        failed = await self.finish("-1001:7")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("appeared", failed["error"])
        self.assertEqual((self.root / "newsfeed" / "file [7].zip").read_bytes(), b"placed by the agent")
        self.assertEqual(sorted(p.name for p in (self.root / "newsfeed").iterdir()), ["file [7].zip"])

    async def test_a_symlinked_chat_directory_is_refused(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (self.root / "newsfeed").symlink_to(elsewhere)
        with self.assertRaisesRegex(ToolError, "not a plain directory"):
            await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7))
        self.assertEqual(list(elsewhere.iterdir()), [])

    async def test_a_symlinked_root_is_refused(self):
        real = Path(self.tmp.name) / "real"
        real.mkdir()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(real)
        downloads = dl.Downloads(self.client, str(link))
        with self.assertRaisesRegex(ToolError, "not a plain directory"):
            await downloads.start("-1001:7", "newsfeed", "News", media_message(7))

    async def test_a_stale_part_file_of_our_own_is_replaced(self):
        (self.root / "newsfeed").mkdir()
        (self.root / "newsfeed" / ".file [7].zip.part").write_bytes(b"half")
        await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7))
        done = await self.finish("-1001:7")
        self.assertEqual(done["status"], "done")
        self.assertEqual((self.root / "newsfeed" / "file [7].zip").read_bytes(), b"payload")

    async def test_a_failed_download_leaves_nothing_behind(self):
        msg = media_message(7, explode=ConnectionError("proxy dropped"))
        await self.downloads.start("-1001:7", "newsfeed", "News", msg)
        failed = await self.finish("-1001:7")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("proxy dropped", failed["error"])
        self.assertEqual(list((self.root / "newsfeed").iterdir()), [])

    async def test_refusals_happen_before_any_file_is_created(self):
        poll = media_message(1, kind="poll")
        with self.assertRaisesRegex(ToolError, "no downloadable file"):
            await self.downloads.start("-1001:1", "newsfeed", "News", poll)
        self.downloads.free_bytes = lambda fd: 10
        with self.assertRaisesRegex(ToolError, "free space"):
            await self.downloads.start("-1001:3", "newsfeed", "News", media_message(3))
        self.assertEqual(self.client.started, [])
        self.assertFalse(any((self.root / "newsfeed").iterdir()))

    async def test_at_most_two_downloads_run_at_once(self):
        self.client.gate = asyncio.Event()
        for msg_id in (1, 2, 3):
            await self.downloads.start(f"-1001:{msg_id}", "newsfeed", "News", media_message(msg_id))
        for _ in range(5):
            await asyncio.sleep(0)
        states = [job["status"] for job in self.downloads.status()["jobs"]]
        self.assertEqual(states, ["running", "running", "queued"])
        self.client.gate.set()
        for msg_id in (1, 2, 3):
            self.assertEqual((await self.finish(f"-1001:{msg_id}"))["status"], "done")

    async def test_active_jobs_are_capped(self):
        self.client.gate = asyncio.Event()
        for msg_id in range(dl.MAX_ACTIVE_JOBS):
            await self.downloads.start(f"-1001:{msg_id}", "newsfeed", "News", media_message(msg_id + 1))
        with self.assertRaisesRegex(ToolError, "wait for some to finish"):
            await self.downloads.start("-1001:99", "newsfeed", "News", media_message(99))
        self.client.gate.set()
        for msg_id in range(dl.MAX_ACTIVE_JOBS):
            await self.finish(f"-1001:{msg_id}")

    async def test_queued_jobs_reserve_their_space(self):
        self.client.gate = asyncio.Event()
        self.downloads.free_bytes = lambda fd: dl.FREE_SPACE_MARGIN + 10
        await self.downloads.start("-1001:1", "newsfeed", "News", media_message(1, payload=b"x" * 8))
        with self.assertRaisesRegex(ToolError, "free space"):
            await self.downloads.start("-1001:2", "newsfeed", "News", media_message(2, payload=b"y" * 8))
        self.client.gate.set()
        await self.finish("-1001:1")

    async def test_a_short_download_is_not_published(self):
        await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7, deliver=3))
        failed = await self.finish("-1001:7")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("incomplete", failed["error"])
        self.assertEqual(list((self.root / "newsfeed").iterdir()), [])

    async def test_a_download_without_a_reported_size_completes(self):
        msg = media_message(7, no_total=True)
        msg.file.size = None
        await self.downloads.start("-1001:7", "newsfeed", "News", msg)
        self.assertEqual((await self.finish("-1001:7"))["status"], "done")

    async def test_a_part_file_swapped_mid_download_is_not_published(self):
        chat = self.root / "newsfeed"

        def swap():
            (chat / ".file [7].zip.part").rename(chat / "moved-away")
            (chat / ".file [7].zip.part").write_bytes(b"payload")

        await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7, during=swap))
        failed = await self.finish("-1001:7")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("replaced", failed["error"])
        self.assertFalse((chat / "file [7].zip").exists())

    async def test_a_file_replaced_after_link_is_not_reported_complete(self):
        target = self.root / "newsfeed" / "file [7].zip"
        original = target.with_name("original.zip")
        link = os.link

        def replace_after_link(*args, **kwargs):
            link(*args, **kwargs)
            target.rename(original)
            target.write_bytes(b"tainted")

        with mock.patch.object(dl.os, "link", side_effect=replace_after_link):
            await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7))
            result = await self.finish("-1001:7")

        self.assertEqual(result["status"], "failed")
        self.assertIn("changed", result["error"])
        self.assertEqual(self.downloads.status("-1001:7")["status"], "failed")
        self.assertEqual(target.read_bytes(), b"tainted")
        self.assertEqual(original.read_bytes(), b"payload")
        self.assertEqual(target.stat().st_size, original.stat().st_size)
        self.assertNotEqual(target.stat().st_ino, original.stat().st_ino)

    async def test_a_done_job_whose_file_was_moved_downloads_again(self):
        msg = media_message(7)
        await self.downloads.start("-1001:7", "newsfeed", "News", msg)
        await self.finish("-1001:7")
        (self.root / "newsfeed" / "file [7].zip").rename(Path(self.tmp.name) / "kept.zip")
        self.assertEqual(self.downloads.status("-1001:7")["status"], "failed")
        again = await self.downloads.start("-1001:7", "newsfeed", "News", msg)
        self.assertIn(again["status"], ("queued", "running"))
        self.assertEqual((await self.finish("-1001:7"))["status"], "done")
        self.assertEqual(self.client.started, [7, 7])

    async def test_a_finished_file_is_reported_even_on_a_full_disk(self):
        msg = media_message(7)
        await self.downloads.start("-1001:7", "newsfeed", "News", msg)
        await self.finish("-1001:7")
        self.downloads.free_bytes = lambda fd: 0
        self.assertEqual((await self.downloads.start("-1001:7", "newsfeed", "News", msg))["status"], "done")

    async def test_odd_chat_directory_names_are_refused(self):
        for bad in ("..", "a/b", "", "x" * 81, "news feed"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ToolError, "chat directory"):
                await self.downloads.start("-1001:7", bad, "News", media_message(7))

    def test_sweep_removes_only_our_own_leftover_parts(self):
        chat = self.root / "newsfeed"
        chat.mkdir()
        (chat / ".7_a.zip.part").write_bytes(b"half")
        (chat / "8_b.zip").write_bytes(b"kept")
        (chat / ".notes").write_bytes(b"kept")
        victim = Path(self.tmp.name) / "victim.part"
        victim.write_bytes(b"keep")
        (chat / ".9_c.zip.part").symlink_to(victim)
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / ".1_x.part").write_bytes(b"keep")
        (self.root / "linked").symlink_to(elsewhere)
        self.assertEqual(self.downloads.sweep_stale_parts(), 1)
        self.assertEqual(sorted(p.name for p in chat.iterdir()), [".9_c.zip.part", ".notes", "8_b.zip"])
        self.assertTrue(victim.exists())
        self.assertTrue((elsewhere / ".1_x.part").exists())

    def test_sweep_continues_when_a_partial_disappears(self):
        scandir, unlink = os.scandir, os.unlink
        for window in ("stat", "unlink"):
            with self.subTest(window=window):
                chat = self.root / window
                chat.mkdir()
                gone, following = chat / ".1_gone.part", chat / ".2_following.part"
                gone.write_bytes(b"half")
                following.write_bytes(b"half")

                def disappearing_stat(*, follow_symlinks):
                    unlink(gone)
                    return os.stat(gone, follow_symlinks=follow_symlinks)

                def sorted_entries(directory):
                    with scandir(directory) as entries:
                        for entry in sorted(entries, key=lambda item: item.name):
                            if window == "stat" and entry.name == gone.name:
                                yield SimpleNamespace(name=entry.name, is_file=entry.is_file, stat=disappearing_stat)
                            else:
                                yield entry

                def disappearing_unlink(name, *, dir_fd):
                    if window == "unlink" and name == gone.name:
                        unlink(name, dir_fd=dir_fd)
                    return unlink(name, dir_fd=dir_fd)

                with mock.patch.object(dl.os, "scandir", side_effect=sorted_entries), \
                     mock.patch.object(dl.os, "unlink", side_effect=disappearing_unlink):
                    self.assertEqual(self.downloads.sweep_stale_parts(), 1)
                self.assertFalse(gone.exists())
                self.assertFalse(following.exists())

    def test_sweep_propagates_permission_and_io_errors(self):
        chat = self.root / "newsfeed"
        chat.mkdir()
        target = chat / ".1_failed.part"
        target.write_bytes(b"half")
        scandir = os.scandir

        def managed_entries(directory):
            with scandir(directory) as entries:
                yield from entries

        for error in (PermissionError(errno.EACCES, "permission denied"), OSError(errno.EIO, "I/O failure")):
            with self.subTest(errno=error.errno):
                with mock.patch.object(dl.os, "scandir", side_effect=managed_entries), \
                     mock.patch.object(dl.os, "unlink", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        self.downloads.sweep_stale_parts()
                self.assertIs(caught.exception, error)
                self.assertEqual(target.read_bytes(), b"half")

    async def test_leftovers_owned_by_someone_else_are_kept(self):
        chat = self.root / "newsfeed"
        chat.mkdir()
        (chat / ".file [7].zip.part").write_bytes(b"theirs")
        self.downloads.owner_uid += 1
        self.assertEqual(self.downloads.sweep_stale_parts(), 0)
        await self.downloads.start("-1001:7", "newsfeed", "News", media_message(7))
        failed = await self.finish("-1001:7")
        self.assertIn("not left by this service", failed["error"])
        self.assertEqual((chat / ".file [7].zip.part").read_bytes(), b"theirs")

    async def test_unknown_jobs_explain_restarts(self):
        with self.assertRaisesRegex(ToolError, "restart"):
            self.downloads.status("-1001:99")


class FileNameTest(unittest.TestCase):
    def name(self, base=None, original="file.zip", ext=".zip", text=None, index=None, msg_id=7):
        msg = media_message(msg_id, name=original, ext=ext)
        msg.message = text
        return dl.file_name(msg, base, index)

    def test_agent_title_gets_the_message_id_and_original_extension(self):
        self.assertEqual(self.name("【字幕组】某番 - 01 [1080p]", original="x.MKV", ext=".mkv"),
                         "【字幕组】某番 - 01 [1080p] [7].mkv")

    def test_an_extension_in_the_title_is_not_doubled(self):
        self.assertEqual(self.name("Show 01.zip"), "Show 01 [7].zip")

    def test_album_files_are_numbered_before_the_id(self):
        self.assertEqual(self.name("Pack", index="02", msg_id=12), "Pack - 02 [12].zip")

    def test_without_a_title_the_first_text_line_then_the_file_name_is_used(self):
        self.assertEqual(self.name(text="\n  新番速递 第3集\n#tag https://x"), "新番速递 第3集 [7].zip")
        self.assertEqual(self.name(original="原始名字.zip"), "原始名字 [7].zip")
        self.assertEqual(self.name(original=None, ext=".jpg"), "telegram [7].jpg")

    def test_shell_and_windows_metacharacters_become_underscores(self):
        self.assertEqual(self.name("a;rm -rf ~;$(id): x?"), "a_rm -rf ~__(id)_ x_ [7].zip")
        self.assertEqual(self.name("../../etc/passwd"), "_.._etc_passwd [7].zip")

    def test_hidden_prefixes_trailing_dots_and_control_characters_are_dropped(self):
        self.assertEqual(self.name("...hidden. "), "hidden [7].zip")
        # U+202E would make "gpj.exe" display as "exe.jpg".
        self.assertEqual(self.name("a\u202egpj.exe\n"), "agpj.exe [7].zip")

    def test_unusable_extensions_are_dropped(self):
        self.assertEqual(self.name("x", original=None, ext=".$(rm)"), "x [7]")

    def test_long_titles_are_cut_by_bytes_and_keep_the_suffix(self):
        result = self.name("长" * 200)
        self.assertLessEqual(len(result.encode()), dl.NAME_BYTES)
        self.assertTrue(result.endswith(" [7].zip"))
        self.assertTrue(result.startswith("长"))


if __name__ == "__main__":
    unittest.main()
