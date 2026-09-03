{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    poetry2nix.url = "github:nix-community/poetry2nix";
  };

  outputs = {
    self,
    nixpkgs,
    poetry2nix,
    ...
  }: let
    defaultSystems = [
      "x86_64-linux"
      "x86_64-darwin"
      "aarch64-linux"
      "aarch64-darwin"
    ];
    eachDefaultSystem = f:
      builtins.listToAttrs (map (system: {
          name = system;
          value = f system;
        })
        defaultSystems);
  in {
    packages = eachDefaultSystem (system: let
      pkgs = import nixpkgs {inherit system;};
      inherit (poetry2nix.lib.mkPoetry2Nix {inherit pkgs;}) mkPoetryApplication defaultPoetryOverrides;
    in {
      default = mkPoetryApplication {
        projectDir = ./.;
        python = pkgs.python311;
        doCheck = false;
        overrides = defaultPoetryOverrides.extend (self: super: {
          frozenlist = super.frozenlist.overridePythonAttrs (old: {
            buildInputs = (old.buildInputs or []) ++ [super.expandvars];
          });
        });
      };
    });

    nixosModules.default = {
      config,
      lib,
      pkgs,
      ...
    }: let
      settingsFormat = pkgs.formats.keyValue {};
    in {
      options.academy.backend.events = with lib; {
        enable = mkEnableOption "Bootstrap Academy Events Microservice";
        environmentFiles = mkOption {
          type = types.listOf types.path;
        };
        settings = mkOption {
          inherit (settingsFormat) type;
        };
        sweepDeletedUsers = {
          enable = mkEnableOption "periodic deletion of the data of users that no longer exist";
          interval = mkOption {
            type = types.str;
            default = "daily";
          };
          randomizedDelay = mkOption {
            type = types.str;
            default = "5m";
          };
        };
      };

      config = let
        cfg = config.academy.backend.events;
        serviceConfig = {
          User = "academy-events";
          Group = "academy-events";
          DynamicUser = true;
          EnvironmentFile = cfg.environmentFiles ++ [(settingsFormat.generate "config" cfg.settings)];
        };
      in
        lib.mkIf cfg.enable {
          systemd.services =
            {
              academy-events = {
                wantedBy = ["multi-user.target"];
                inherit serviceConfig;
                preStart = ''
                  cd ${lib.fileset.toSource {
                    root = ./.;
                    fileset = lib.fileset.unions [
                      ./alembic
                      ./alembic.ini
                    ];
                  }}
                  ${self.packages.${pkgs.system}.default}/bin/alembic upgrade head
                '';
                script = ''
                  ${self.packages.${pkgs.system}.default}/bin/api
                '';
              };
            }
            // lib.optionalAttrs cfg.sweepDeletedUsers.enable {
              academy-events-sweep-deleted-users = {
                serviceConfig = serviceConfig // {Type = "oneshot";};
                script = ''
                  ${self.packages.${pkgs.system}.default}/bin/sweep-deleted-users
                '';
              };
            };

          systemd.timers = lib.optionalAttrs cfg.sweepDeletedUsers.enable {
            academy-events-sweep-deleted-users = {
              wantedBy = ["timers.target"];
              timerConfig = {
                OnCalendar = cfg.sweepDeletedUsers.interval;
                RandomizedDelaySec = cfg.sweepDeletedUsers.randomizedDelay;
                Persistent = true;
              };
            };
          };
        };
    };

    devShells = eachDefaultSystem (system: let
      pkgs = import nixpkgs {inherit system;};
    in {
      default = pkgs.mkShell {
        packages = with pkgs; [
          python311
          poetry
          poethepoet
          pyright
          python311.pkgs.greenlet
        ];
        shellHook = ''
          poe setup
          source .venv/bin/activate
        '';
      };
    });
  };
}
