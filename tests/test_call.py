"""Tests for `telegram-reader call` against a real HTTP server backed by the fake client."""
import io
import json
import os
import socket
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import uvicorn

from telegram_reader import call
from telegram_reader import server as tr
from test_server import PUBLIC_CHANNEL, make_client, marked


class CallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = make_client()
        app = tr.build_server(tr.Reader(cls.client)).streamable_http_app()
        sock = socket.socket()
        sock.bind((tr.HOST, 0))
        cls.port = sock.getsockname()[1]
        cls.url = f"http://{tr.HOST}:{cls.port}/mcp"
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.01)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(10)

    def run_call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = tr.main(["call", "--port", str(self.port), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_tool_result_and_exits_zero(self):
        code, out, _ = self.run_call("read_messages", f"chat={marked(PUBLIC_CHANNEL)}", "limit=2")
        self.assertEqual(code, 0)
        self.assertEqual([m["id"] for m in json.loads(out)["messages"]], [10, 9])

    def test_string_values_take_the_types_the_tool_declares(self):
        args = {"ref": "https://t.me/chatroom/11", "include_photo": "false"}
        result = call.rpc(self.url, "tools/call", {"name": "get_message", "arguments": args})
        self.assertEqual([part["type"] for part in result["content"]], ["text"])

    def test_images_are_left_out_of_the_output(self):
        code, out, _ = self.run_call("get_message", "ref=https://t.me/chatroom/11")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["notice"], tr.NOTICE)

    def test_tool_errors_go_to_stderr_and_exit_one(self):
        code, out, err = self.run_call("read_messages", "chat=+15550000000")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("chat must be an id", err)

    def test_misspelled_arguments_are_refused_before_the_tool_runs(self):
        before = len(self.client.iter_calls)
        code, _, err = self.run_call("read_messages", "chat=newsfeed", "limt=2")
        self.assertEqual(code, 1)
        self.assertIn("no argument 'limt'; it takes chat, limit", err)
        self.assertEqual(len(self.client.iter_calls), before)

    def test_unknown_tools_and_bare_words_are_refused(self):
        code, _, err = self.run_call("send_message", "chat=newsfeed")
        self.assertEqual(code, 1)
        self.assertIn("no tool 'send_message'", err)
        code, _, err = self.run_call("read_messages", "newsfeed")
        self.assertEqual(code, 1)
        self.assertIn("expected key=value", err)

    def test_a_proxy_from_the_environment_is_not_used(self):
        # Port 9 is closed, so a request sent to this proxy would fail.
        proxy = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "no_proxy": "", "NO_PROXY": ""}
        with mock.patch.dict(os.environ, proxy):
            code, out, err = self.run_call("list_chats", "kind=channel")
        self.assertEqual(code, 0, err)
        self.assertEqual([c["title"] for c in json.loads(out)["chats"]], ["News"])

    def test_a_server_that_is_not_running_is_reported(self):
        with socket.socket() as sock:
            sock.bind((tr.HOST, 0))
            free_port = sock.getsockname()[1]
        err = io.StringIO()
        with redirect_stderr(err):
            code = tr.main(["call", "--port", str(free_port), "download_status"])
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err.getvalue())


if __name__ == "__main__":
    unittest.main()
