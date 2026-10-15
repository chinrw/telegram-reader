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
        # Evaluates the module with every option set and builds the unit file.
        module =
          (nixpkgs.lib.nixosSystem {
            inherit (pkgs.stdenv.hostPlatform) system;
            modules = [
              self.nixosModules.default
              {
                boot.isContainer = true;
                system.stateVersion = "25.11";
                users.users.alice.isNormalUser = true;
                services.telegram-reader = {
                  enable = true;
                  apiIdFile = "/run/secrets/api_id";
                  apiHashFile = "/run/secrets/api_hash";
                  proxy = "http://192.0.2.10:8080";
                  downloads = {
                    directory = "/srv/media/telegram";
                    createAs = "alice";
                  };
                };
              }
            ];
          }).config.systemd.units."telegram-reader.service".unit;
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
