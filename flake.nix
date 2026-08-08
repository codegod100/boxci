{
  description = "boxci — minimal Nix-flake CI engine (sleek-inspired pipelines)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;

        boxci = python.pkgs.buildPythonApplication {
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
      in {
        packages = {
          default = boxci;
          boxci = boxci;
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
            python312
            yq
            jq
            git
          ];
          shellHook = ''
            export BOXCI_ROOT="$PWD"
            echo "boxci dev shell — run: boxci run pipelines/example.yml"
          '';
        };
      });
}
