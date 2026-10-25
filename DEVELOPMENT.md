# Development

## Layout

- `src/telegram_reader/server.py` holds the MCP tools, the private-chat refusal, the
  login and serve commands, and turns Telethon errors into tool errors.
- `src/telegram_reader/downloads.py` holds the background download jobs and file
  naming.
- `src/telegram_reader/verify.py` compares a local file with Telegram's part hashes.
  Files on another DC need Telethon's exported senders, a private API.
- `src/telegram_reader/call.py` is the `call` subcommand, a plain HTTP client for
  scripts. It never loads the session.
- `nix/nixos.nix` is the NixOS module. `flake.nix` exports it with the package.
- `tests/` runs offline. `tests/fakes.py` stands in for the Telethon client, so no
  account or network access is needed.

## Run the tests

```console
$ nix develop
$ PYTHONPATH=src python -m unittest discover -s tests
```

`nix flake check` builds the package, which runs the same tests, and evaluates the
module with every option set. It builds only the files git tracks, so `git add` a
new module or test before running it, or the check runs without them.

## Rules the code keeps

- Explicit-chat operations check the chat kind before requesting messages.
  Global search retrieves account-wide hits inside the trusted service and
  filters their chat kinds before returning them. Every returned message's peer
  is checked. Basic-group message ids share one numbering with private chats,
  so a fetch by id can return a private message.
- The download directory is shared with the agent, which can rename and replace
  entries in it. All writes go through directory descriptors opened with
  `O_NOFOLLOW`. Files are created with `O_EXCL` under a `.part` name and published
  with `link()`, which fails instead of replacing an existing file. Reads for hash
  checks take the same path and refuse anything but a plain file, so the agent
  cannot point the service at files outside the directory.
- Nothing calls `send_read_acknowledge`, joins a chat, or increments view
  counters. Keep it that way. The tests pin the tool list and their read-only
  annotations.

When you add a guard, add a test that fails without it. Check by deleting the
guard and running the suite.
