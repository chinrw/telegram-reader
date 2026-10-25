{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
# The Telethon session can do anything the account owner can, so the server
# runs as its own user with a private state directory. Point the agent at
# `url`. The agent must not run as this user or be able to read its files.
let
  cfg = config.services.telegram-reader;
  user = "telegram-reader";
  stateDir = "/var/lib/telegram-reader";
  # Telethon appends ".session" to this path.
  session = "${stateDir}/reader";
  downloadsDir = cfg.downloads.directory;
  downloadsParent = dirOf downloadsDir;
  # Unit files need token quoting and literal-percent escaping, independently of shell arguments.
  unitPath = value: builtins.toJSON (lib.replaceStrings [ "%" ] [ "%%" ] value);

  # systemd's IP filter takes addresses only, so it is applied when the proxy
  # is given as an IPv4 literal and skipped for host names.
  proxyHost =
    if cfg.proxy == null then
      null
    else
      let
        found = builtins.match "[a-z0-9]+://([0-9.]+):[0-9]+/?" cfg.proxy;
      in
      if found == null then null else builtins.elemAt found 0;

  environment = {
    TELEGRAM_API_ID_FILE = cfg.apiIdFile;
    TELEGRAM_API_HASH_FILE = cfg.apiHashFile;
    TELEGRAM_READER_SESSION = session;
  }
  // lib.optionalAttrs (cfg.proxy != null) { TELEGRAM_READER_PROXY = cfg.proxy; }
  // lib.optionalAttrs (downloadsDir != null) { TELEGRAM_READER_DOWNLOADS = downloadsDir; };

  # EXIT_NOT_LOGGED_IN in server.py. A session that is missing or was ended
  # from another device needs a new login, so systemd stops retrying.
  notLoggedInStatus = 78;

  login = pkgs.writeShellApplication {
    name = "telegram-reader-login";
    # Shell-like text in configured environment values must stay literal.
    excludeShellChecks = [ "SC2016" ];
    runtimeInputs = [
      pkgs.coreutils
      pkgs.systemd
      pkgs.util-linux
    ];
    text = ''
      if [ "$(id -u)" -ne 0 ]; then
        echo "run this with sudo" >&2
        exit 1
      fi
      # The running service holds the SQLite session open, and a second
      # writer would fail with "database is locked".
      systemctl stop telegram-reader.service
      umask 077
      install -d -m 0700 -o ${user} -g ${user} ${stateDir}
      runuser -u ${user} -- env ${
        lib.concatStringsSep " " (lib.mapAttrsToList (k: v: "${k}=${lib.escapeShellArg v}") environment)
      } ${lib.getExe cfg.package} login
      systemctl start telegram-reader.service
      systemctl --no-pager --lines=0 status telegram-reader.service
    '';
  };

  # Runs as root before each start. The agent can usually rename entries next
  # to the download directory, so this never follows a symlink left where the
  # directory should be. Older acl releases checked with lstat and then wrote
  # through the path, a window in which a swapped-in symlink would get the
  # ACL. 2.4.0 writes with lsetxattr.
  prepareDownloads =
    assert lib.assertMsg (lib.versionAtLeast pkgs.acl.version "2.4.0")
      "telegram-reader needs setfacl -P to refuse symlinks atomically (acl >= 2.4.0)";
    pkgs.writeShellApplication {
      name = "telegram-reader-prepare-downloads";
      # Shell-like text in configured paths and user names must stay literal.
      excludeShellChecks = [ "SC2016" ];
      runtimeInputs = [
        pkgs.acl
        pkgs.coreutils
        pkgs.util-linux
      ];
      text = ''
        umask 002
        ${lib.optionalString (cfg.downloads.createAs != null) ''
          if [ ! -e ${lib.escapeShellArg downloadsDir} ] && [ ! -L ${lib.escapeShellArg downloadsDir} ]; then
            # Plain mkdir by the tree's owner inherits the parent's setgid
            # group and default ACLs, which chmod or chgrp would not keep.
            runuser -u ${lib.escapeShellArg cfg.downloads.createAs} -- mkdir ${lib.escapeShellArg downloadsDir}
          fi
        ''}
        setfacl -P -m u:${user}:rwx,d:u:${user}:rwx ${lib.escapeShellArg downloadsDir}
      '';
    };
in
{
  options.services.telegram-reader = {
    enable = lib.mkEnableOption "the Telegram MCP server for an LLM agent";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "telegram-reader.packages.\${system}.default";
      description = "The telegram-reader package to run.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 9520;
      description = "Loopback port of the MCP endpoint.";
    };

    url = lib.mkOption {
      type = lib.types.str;
      readOnly = true;
      default = "http://127.0.0.1:${toString cfg.port}/mcp";
      description = "Streamable HTTP MCP endpoint, for clients on this host.";
    };

    apiIdFile = lib.mkOption {
      type = lib.types.str;
      example = "/run/secrets/telegram-reader/api_id";
      description = "File holding the api_id from my.telegram.org, readable by the telegram-reader user.";
    };

    apiHashFile = lib.mkOption {
      type = lib.types.str;
      example = "/run/secrets/telegram-reader/api_hash";
      description = "File holding the api_hash from my.telegram.org, readable by the telegram-reader user.";
    };

    proxy = lib.mkOption {
      type = lib.types.nullOr (lib.types.strMatching "[^@]*");
      default = null;
      example = "http://192.0.2.10:8080";
      description = ''
        HTTP CONNECT or SOCKS proxy for MTProto traffic, as protocol://host:port,
        without embedded credentials.
        With an IPv4 literal, the service may reach only loopback and that address.
      '';
    };

    downloads = {
      directory = lib.mkOption {
        type = lib.types.nullOr (lib.types.strMatching "/.*");
        default = null;
        example = "/srv/media/telegram";
        description = ''
          Absolute directory where download_media saves files, one subdirectory
          per chat. The agent must see it at the same path. Null disables downloads.
        '';
      };

      createAs = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "alice";
        description = ''
          User that creates the directory when it is missing, with plain mkdir
          so it inherits the parent's group and default ACLs. Null leaves
          creating it to you.
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.${user} = {
      isSystemUser = true;
      group = user;
      home = stateDir;
    };
    users.groups.${user} = { };

    environment.systemPackages = [ login ];

    systemd.services.telegram-reader = {
      description = "Telegram MCP server: read channels and groups, download media";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      environment = lib.mapAttrs (_: value: lib.replaceStrings [ "%" ] [ "%%" ] value) environment;
      # Until `sudo telegram-reader-login` creates the session there is
      # nothing to serve. The login wrapper starts the unit afterwards.
      unitConfig = {
        ConditionPathExists = "${session}.session";
      }
      // lib.optionalAttrs (downloadsDir != null) {
        # Wants, not Requires: without the mount only downloads should fail.
        WantsMountsFor = [ (unitPath downloadsParent) ];
      };
      serviceConfig = {
        ExecStart = lib.escapeShellArgs [
          (lib.getExe cfg.package)
          "serve"
          "--port"
          (toString cfg.port)
        ];
        User = user;
        Group = user;
        StateDirectory = "telegram-reader";
        StateDirectoryMode = "0700";
        # Downloads stay group-writable for the agent. The session stays
        # private because its directory is 0700.
        UMask = "0002";
        Restart = "on-failure";
        RestartSec = 30;
        RestartPreventExitStatus = notLoggedInStatus;

        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        CapabilityBoundingSet = "";
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectProc = "invisible";
        ProcSubset = "pid";
        ProtectClock = true;
        ProtectHostname = true;
        ProtectControlGroups = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "~@privileged"
        ];
      }
      // lib.optionalAttrs (proxyHost != null) {
        # Loopback serves the agent. The proxy is the only way out to Telegram.
        IPAddressDeny = "any";
        IPAddressAllow = [
          "localhost"
          proxyHost
        ];
      }
      // lib.optionalAttrs (downloadsDir != null) {
        # "-": a broken download directory should fail downloads, not reading.
        ExecStartPre = "-+${lib.getExe prepareDownloads}";
        # The parent, not the directory itself: systemd resolves symlinks when
        # it builds the mount namespace, and the agent may be able to swap the
        # directory for one. File permissions still confine writes to it.
        ReadWritePaths = [ (unitPath "-${downloadsParent}") ];
      };
    };
  };
}
