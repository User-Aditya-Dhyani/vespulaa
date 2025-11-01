{
  description = "Isolated env for Vespula with custom BlueZ 5.66";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/release-24.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        customBluez = pkgs.bluez.overrideAttrs (old: rec {
          version = "5.66";
          src = pkgs.fetchurl {
            url = "https://www.kernel.org/pub/linux/bluetooth/bluez-${version}.tar.xz";
            sha256 = "39fea64b590c9492984a0c27a89fc203e1cdc74866086efb8f4698677ab2b574";
          };
          doCheck = false;  # Skip tests to avoid conf-related failures
          outputs = [ "out" "dev" ];  # Exclude "test" output to prevent wrapping errors
          configureFlags = old.configureFlags or [] ++ [
            "--enable-mesh"
            "--enable-library"
            "--enable-deprecated"
            "--sysconfdir=/etc"
          ];
          nativeBuildInputs = old.nativeBuildInputs or [] ++ [ pkgs.pkg-config ];
          buildInputs = old.buildInputs or [] ++ [
            pkgs.dbus
            pkgs.glib
            pkgs.libical
            pkgs.readline
            pkgs.udev
            pkgs.systemd
            pkgs.alsa-lib
          ];
          # Suppress rm stderr + ignore errors
          postInstall = ''
            mkdir -p $out/etc/bluetooth || true
            rm -f $out/etc/bluetooth/*.conf 2>/dev/null || true
          '';
        });
      in {
        devShells.default = pkgs.mkShell {
          buildInputs = [
            customBluez
            pkgs.python312
            pkgs.python312Packages.pydbus
            pkgs.python312Packages.tkinter
            pkgs.gobject-introspection
            pkgs.pkg-config
            pkgs.dbus
            pkgs.glib
            pkgs.systemd
            pkgs.polkit
          ];
          shellHook = ''
            export PATH="${customBluez}/bin:${customBluez}/sbin:$PATH"
            export LD_LIBRARY_PATH="${customBluez}/lib:$LD_LIBRARY_PATH"
            export PKG_CONFIG_PATH="${customBluez}/lib/pkgconfig:$PKG_CONFIG_PATH"
            echo "Entered Nix env with custom BlueZ 5.66. Run your app here."
          '';
        };
      }
    );
}
