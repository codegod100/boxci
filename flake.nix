{
  description = "boxci — minimal Nix-flake CI engine (sleek-inspired pipelines)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, rust-overlay }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ (import rust-overlay) ];
        };
        python = pkgs.python312;

        # radicle-job MSRV is 1.86; nixpkgs 24.11 ships older rustc.
        rustToolchain = pkgs.rust-bin.stable."1.86.0".default;
        rustPlatform = pkgs.makeRustPlatform {
          cargo = rustToolchain;
          rustc = rustToolchain;
        };

        # rad-job CLI — publishes Job COBs for Desktop/Explorer CI status.
        rad-job = rustPlatform.buildRustPackage rec {
          pname = "radicle-job";
          version = "0.6.0";

          src = pkgs.fetchCrate {
            inherit pname version;
            hash = "sha256-iaYJNckEvLVnodLVtxE2DhUcGdngVE4aXmAjoD/lZ5Q=";
          };

          cargoLock = {
            lockFile = "${src}/Cargo.lock";
            allowBuiltinFetchGit = true;
          };

          nativeBuildInputs = with pkgs; [
            pkg-config
            git
          ];
          buildInputs = with pkgs; [
            openssl
          ];

          doCheck = false;
          cargoBuildFlags = [ "--bin" "rad-job" ];
        };

        boxciApp = python.pkgs.buildPythonApplication {
          pname = "boxci";
          version = "0.1.0";
          pyproject = true;
          src = ./runner;

          build-system = [ python.pkgs.setuptools python.pkgs.wheel ];

          dependencies = [
            python.pkgs.pyyaml
            python.pkgs.flask
          ];

          doCheck = false;
        };

        # Put rad-job on PATH next to boxci for Job COB publishing.
        boxci = pkgs.symlinkJoin {
          name = "boxci";
          paths = [ boxciApp rad-job ];
          nativeBuildInputs = [ pkgs.makeWrapper ];
          postBuild = ''
            for bin in boxci boxci-server; do
              if [ -f "$out/bin/$bin" ]; then
                wrapProgram "$out/bin/$bin" \
                  --prefix PATH : ${pkgs.lib.makeBinPath [ rad-job pkgs.git pkgs.openssh ]}
              fi
            done
          '';
        };
        # Docker image bundling boxci + rad-job + runtime tools + Nix.
        dockerImage = pkgs.dockerTools.buildImage {
          name = "boxci";
          tag = "latest";
          created = "now";
          copyToRoot = [
            boxci
            pkgs.nix
            # runtime tools that boxci scripts / rad-job need at run time
            pkgs.git
            pkgs.openssh
            pkgs.bash
            pkgs.coreutils
            pkgs.cacert
            # minimal nix config so `nix` works without a running daemon
            (pkgs.writeTextDir "etc/nix/nix.conf" ''
              experimental-features = nix-command flakes
              build-users-group =
              ssl-cert-file = /etc/ssl/certs/ca-bundle.crt
            '')
          ];
          config = {
            Entrypoint = [ "boxci" ];
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "GIT_SSL_CAINFO=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "NIX_CONF_DIR=/etc/nix"
              "NIX_REMOTE=local"
            ];
          };
        };
      in {
        packages = {
          default = boxci;
          # Python engine only (no rad-job) — faster iteration.
          boxci-app = boxciApp;
          boxci = boxci;
          rad-job = rad-job;
          # OCI/Docker image: `nix build .#dockerImage` → ./result is a tarball
          dockerImage = dockerImage;
          docker = dockerImage;
        };

        apps = {
          default = {
            type = "app";
            program = "${boxci}/bin/boxci";
          };
          server = {
            type = "app";
            program = "${boxci}/bin/boxci-server";
          };
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            boxci
            rad-job
            python312
            yq
            jq
            git
            openssh
          ];
          shellHook = ''
            export BOXCI_ROOT="$PWD"
            echo "boxci dev shell — run: boxci run pipelines/example.yml"
            echo "rad-job for Job COBs: $(command -v rad-job || echo missing)"
          '';
        };
      });
}
