{
  description = "MCP server that lets an LLM agent read your Telegram channels and groups and download their files";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      pyproject = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;
    in
    {
      nixosModules = {
        telegram-reader = import ./nix/nixos.nix { inherit self; };
        default = self.nixosModules.telegram-reader;
      };

      packages = forAllSystems (pkgs: rec {
        telegram-reader = pkgs.python3Packages.buildPythonApplication {
          pname = pyproject.name;
          inherit (pyproject) version;
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [
            mcp
            python-socks
            telethon
            uvicorn
          ];
          nativeCheckInputs = [ pkgs.python3Packages.unittestCheckHook ];
          unittestFlagsArray = [
            "-s"
            "tests"
          ];
          meta = {
            description = pyproject.description;
            homepage = "https://github.com/chinrw/telegram-reader";
            license = pkgs.lib.licenses.mit;
            mainProgram = "telegram-reader";
            platforms = pkgs.lib.platforms.linux;
          };
        };
        default = telegram-reader;
      });

      checks = forAllSystems (pkgs: {
        # Building the package runs the unit tests via unittestCheckHook.
        package = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        # Build both wrappers and check the root helper without changing users or ACLs.
        module =
          let
            lib = nixpkgs.lib;
            directory = "/srv/media library ' \" \\ %n $data/Telegram $(touch path-injection) files";
            createAs = "alice $(touch user-injection)";
            configuration =
              overrides:
              (lib.nixosSystem {
                inherit (pkgs.stdenv.hostPlatform) system;
                modules = [
                  self.nixosModules.default
                  {
                    boot.isContainer = true;
                    system.stateVersion = "25.11";
                    services.telegram-reader = {
                      enable = true;
                      package = pkgs.hello;
                      apiIdFile = "/run/secrets/api_id";
                      apiHashFile = "/run/secrets/api_hash";
                      proxy = "http://192.0.2.10:8080";
                      downloads = { inherit directory createAs; };
                    }
                    // overrides;
                  }
                ];
              }).config;
            config = configuration { };
            helper = lib.removePrefix "-+" config.systemd.services.telegram-reader.serviceConfig.ExecStartPre;
            login = builtins.head (
              builtins.filter (
                package: (package.name or "") == "telegram-reader-login"
              ) config.environment.systemPackages
            );
            relativePath = builtins.tryEval (
              (configuration { downloads.directory = "relative/path"; })
              .services.telegram-reader.downloads.directory
            );
            proxyCredentials = builtins.tryEval (
              (configuration { proxy = "http://user:password@192.0.2.10:8080"; }).services.telegram-reader.proxy
            );
          in
          pkgs.runCommand "telegram-reader-module-check"
            {
              unit = config.systemd.units."telegram-reader.service".unit;
              nativeBuildInputs = [
                pkgs.python3
                login
              ];
            }
            ''
              runuser() {
                test "$#" -eq 5
                test "$1" = -u
                test "$2" = ${lib.escapeShellArg createAs}
                test "$3" = --
                test "$4" = mkdir
                test "$5" = ${lib.escapeShellArg directory}
                touch "$TMPDIR/runuser-called"
              }
              setfacl() {
                test "$#" -eq 4
                test "$1" = -P
                test "$2" = -m
                test "$3" = u:telegram-reader:rwx,d:u:telegram-reader:rwx
                test "$4" = ${lib.escapeShellArg directory}
                touch "$TMPDIR/setfacl-called"
              }
              export -f runuser setfacl
              ${helper}
              test -e "$TMPDIR/runuser-called"
              test -e "$TMPDIR/setfacl-called"
              test ! -e path-injection
              test ! -e user-injection
              test ${lib.boolToString relativePath.success} = false
              test ${lib.boolToString proxyCredentials.success} = false
              mkdir "$TMPDIR/units"
              cat "$unit/telegram-reader.service" > "$TMPDIR/units/telegram-reader.service"
              printf '\n[Unit]\nDefaultDependencies=no\n' >> "$TMPDIR/units/telegram-reader.service"
              # Inspect systemd's parsed values without executing the unit or using a running manager.
              XDG_RUNTIME_DIR="$TMPDIR" SYSTEMD_LOG_LEVEL=debug \
                SYSTEMD_UNIT_PATH="$TMPDIR/units" SYSTEMD_GENERATOR_PATH=/dev/null \
                SYSTEMD_ENVIRONMENT_GENERATOR_PATH=/dev/null \
                ${pkgs.systemd}/bin/systemd-analyze --user --man=no --generators=no \
                  verify "$TMPDIR/units/telegram-reader.service" > "$TMPDIR/unit-dump" 2>&1 \
                  || { cat "$TMPDIR/unit-dump"; exit 1; }
              python - "$TMPDIR/unit-dump" <<'PY'
              import pathlib
              import re
              import sys

              dump = pathlib.Path(sys.argv[1]).read_text()
              body = re.search(r"^\t.*Unit telegram-reader\.service:\n((?:\t\t.*\n)*)", dump, re.M).group(1)
              fields = [line.strip().split(": ", 1) for line in body.splitlines() if ": " in line.strip()]
              parent = ${builtins.toJSON (dirOf directory)}
              writable = [value for key, value in fields if key == "ReadWritePaths"]
              assert writable == ["-" + parent], writable
              mounts = [value.rsplit(" (origin-", 1)[0] for key, value in fields if key == "WantsMountsFor"]
              assert parent in mounts, mounts
              environment = dict(value.split("=", 1) for key, value in fields if key == "Environment")
              actual = environment["TELEGRAM_READER_DOWNLOADS"]
              assert actual == ${builtins.toJSON directory}, actual
              PY
              mkdir "$out"
              cp "$unit/telegram-reader.service" "$out/"
            '';
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [
              ps.mcp
              ps.python-socks
              ps.telethon
              ps.uvicorn
            ]))
          ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
