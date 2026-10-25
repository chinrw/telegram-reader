# telegram-reader

An MCP server that lets an LLM agent read the Telegram channels and groups your
account can see, and download their videos and files under names taken from the
post. The agent cannot send, edit, join, react or mark anything read, and it never
sees your private chats, bots or Saved Messages.

You need:

- an `api_id` and `api_hash` from <https://my.telegram.org> (API development tools);
- NixOS for the module below, or Python 3.11+ to run it by hand;
- an MCP client that speaks streamable HTTP, on the same host.

## Set it up on NixOS

Add the flake and enable the module. The two credential files must be readable by
the `telegram-reader` user, for example as sops-nix secrets owned by it.

```nix
# flake.nix
inputs.telegram-reader = {
  url = "github:chinrw/telegram-reader";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

```nix
# configuration.nix
{ config, inputs, ... }:
{
  imports = [ inputs.telegram-reader.nixosModules.default ];

  services.telegram-reader = {
    enable = true;
    apiIdFile = "/run/secrets/telegram-reader/api_id";
    apiHashFile = "/run/secrets/telegram-reader/api_hash";
    # Only when Telegram is not reachable directly.
    proxy = "http://192.0.2.10:8080";
    # Leave unset to turn downloads off.
    downloads.directory = "/srv/media/telegram";
  };
}
```

Proxy URLs must not contain usernames or passwords. Do not put credentials in
Nix configuration.

After switching, log the account in once. It asks for your phone number, the login
code Telegram sends you, and your two-step verification password if you set one:

```console
$ sudo telegram-reader-login
```

The service starts when the login succeeds. Point your agent at
`config.services.telegram-reader.url`, which is `http://127.0.0.1:9520/mcp` by
default. The session shows up in Telegram under Settings > Devices with your host
name, and ending it there logs the server out.

A permanent Telegram disconnection stops the server so the NixOS unit can restart
it. The server allows ten seconds for HTTP requests to drain during shutdown,
then cancels requests still running. An unauthorized session needs a new login.
Successful DC migrations keep the server running, though individual in-flight
requests may still fail while Telegram reconnects.

## Run it without NixOS

Run it as a user your agent cannot read files of. The session file gives full
control of the account.

```console
$ pip install .
$ export TELEGRAM_API_ID_FILE=~/.config/telegram-reader/api_id
$ export TELEGRAM_API_HASH_FILE=~/.config/telegram-reader/api_hash
$ export TELEGRAM_READER_SESSION=~/.local/state/telegram-reader/reader
$ export TELEGRAM_READER_DOWNLOADS=~/Downloads/telegram   # optional
$ export TELEGRAM_READER_PROXY=socks5://127.0.0.1:1080    # optional
$ telegram-reader login
$ telegram-reader serve --port 9520
```

## Tools

| Tool | What it does |
|---|---|
| `list_chats` | Lists up to 200 joined channels and groups, filtered by kind or a title/username substring. There is no paging. |
| `read_messages` | Reads recent messages of one chat, newest first, with paging and a date cutoff. |
| `search_messages` | Searches one chat, or every joined channel and group. |
| `get_message` | Fetches one message by `t.me` link. Photos come back as images the model can see. |
| `download_media` | Downloads a post's files in the background. |
| `download_status` | Reports download progress. |

Chats can be given as the id from `list_chats`, a `@username`, or a `t.me` link.
Private-channel links such as `https://t.me/c/123456/789` work too. Public channels
can be read without joining them.

Global search examines at most `3 * limit` hits before filtering out private
chats. If this budget runs out before enough readable results are found, the
result has `truncated: true`: matching posts may be missing. Search a specific
chat to avoid this global scan limit.

## Call the tools from scripts

`telegram-reader call` runs one tool on the running server and prints its JSON
result, so a cron job or shell script can read Telegram without an LLM. Arguments
are `key=value` pairs. A misspelled name is refused with the list of names the tool
takes. Pass `--port` when the server does not listen on 9520.

```console
$ telegram-reader call list_chats kind=channel
$ telegram-reader call read_messages chat=@newsfeed limit=5 since=2026-10-01
$ telegram-reader call search_messages query=release chat=newsfeed
$ telegram-reader call download_media ref=https://t.me/newsfeed/123 name="Show - 01"
$ telegram-reader call download_status
```

The command needs no credentials, since it only talks to the server. A tool error,
a refused argument or an unreachable server prints the reason on stderr and exits 1.
Photos from `get_message` are left out of the output.

## Downloads

Ask the agent to read the post and pass a clean title as `name`. Files are saved as

```
<downloads.directory>/<chat>/<name> [<message id>].<ext>
```

and every file of an album as `<name> - 01 [<id>].<ext>`, `<name> - 02 [<id>].<ext>`,
and so on, named after the album's text. Without a `name`, the first line of the
post is used. `<chat>` is the channel's username plus its id, such as
`newsfeed-c1234567890`, or an id-based name when the chat has no username.

- Files are created without execute bits and owned by the service user. A writer
  in the directory can replace or copy them, and interpreters can read them
  without execute permission.
- Existing files are never overwritten, and a symlink planted in the directory
  cannot redirect a download.
- Two downloads run at a time and twenty can be queued or running. A download
  starts only if the disk has room for it, for the downloads still in progress,
  and 1 GiB more.
- Album capacity is checked as a whole, counting only new jobs. Requesting an
  existing job does not need a free slot.
- The extension comes from the original file. The characters
  `` $ ` \ " ' ; | & < > * ? { } ! : / `` in names become `_`.

The module requires an absolute download directory and grants the service user
access to it with an ACL. Set
`downloads.createAs` to a user who should create it with plain `mkdir`, so it
inherits the parent directory's group and default ACLs.

## What the agent sees, and what it does not

Every result carries a notice that Telegram content is third-party data: the
agent may read it, but should not follow instructions found in it or run files
downloaded from it. The server can only state this. Enforcing it is up to your
agent's own approval rules, for example by denying commands that run an
interpreter on files in the download directory.

The server refuses private chats, bots and Saved Messages on every path,
including global search and messages fetched by id. It also refuses phone numbers
and other strings that would make Telegram look up your contacts.

## Limits

- The endpoint has no authentication. It listens on 127.0.0.1 only and rejects
  foreign `Host` and `Origin` headers, but any local process can call it.
- Download jobs and completion records live in memory. The server keeps at most
  200 jobs, retaining active jobs and discarding older completed or failed ones.
  A completed file is reused only while its record is retained and its metadata
  still matches. Moving, deleting or changing it makes the job report `failed`;
  ask for the post again to download it again. After a restart or record eviction,
  move an existing destination file away first: the server cannot verify its
  origin and will not overwrite it or assume it is complete from its size.
- A job that waits a long time in the queue can fail when Telegram's file reference
  expires. Asking again fixes it.

## License

MIT
