# Development

## Layout

- `src/telegram_reader/server.py` holds the MCP tools, the private-chat refusal, the
  login and serve commands, and turns Telethon errors into tool errors.
- `src/telegram_reader/downloads.py` holds the background download jobs and file
  naming.
- `nix/nixos.nix` is the NixOS module. `flake.nix` exports it with the package.
- `tests/` runs offline. `tests/fakes.py` stands in for the Telethon client, so no
  account or network access is needed.

## Run the tests

```console
$ nix develop
$ PYTHONPATH=src python -m unittest discover -s tests
```

`nix flake check` builds the package, which runs the same tests, and evaluates the
module with every option set.

## Rules the code keeps

- Every path that returns messages checks the chat kind before the request and
  the message's peer after it. Basic-group message ids share one numbering with
  private chats, so a fetch by id can return a private message.
- The download directory is shared with the agent, which can rename and replace
  entries in it. All writes go through directory descriptors opened with
  `O_NOFOLLOW`. Files are created with `O_EXCL` under a `.part` name and published
  with `link()`, which fails instead of replacing an existing file.
- Nothing calls `send_read_acknowledge`, joins a chat, or increments view
  counters. Keep it that way. The tests pin the tool list and their read-only
  annotations.

When you add a guard, add a test that fails without it. Check by deleting the
guard and running the suite.
