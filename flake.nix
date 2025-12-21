{
  description = "Isolated env for Vespula with custom BlueZ 5.66";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/release-24.05";
    flake-utils.url = "github:numtide/flake-utils";
    fenix.url = "github:nix-community/fenix";
    fenix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, flake-utils, fenix }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        lib = pkgs.lib;
        fenixPkgs = fenix.packages.${system};
        customBluez = pkgs.bluez.overrideAttrs (old: rec {
          version = "5.66";
          src = pkgs.fetchurl {
            url = "https://www.kernel.org/pub/linux/bluetooth/bluez-${version}.tar.xz";
            sha256 = "39fea64b590c9492984a0c27a89fc203e1cdc74866086efb8f4698677ab2b574";
          };
          doCheck = false;
          outputs = [ "out" "dev" ];
          configureFlags = (old.configureFlags or []) ++ [
            "--enable-mesh"
            "--enable-library"
            "--enable-deprecated"
            "--sysconfdir=/etc"
          ];
          nativeBuildInputs = (old.nativeBuildInputs or []) ++ [ pkgs.pkg-config ];
          buildInputs = (old.buildInputs or []) ++ [
            pkgs.dbus
            pkgs.glib
            pkgs.libical
            pkgs.readline
            pkgs.udev
            pkgs.systemd
            pkgs.alsa-lib
          ];
          postInstall = ''
            mkdir -p $out/etc/bluetooth || true
            rm -f $out/etc/bluetooth/*.conf 2>/dev/null || true
          '';
        });
      in {
        devShells.default = pkgs.mkShell {
          buildInputs = [
            # BlueZ + Python stack
            customBluez
            pkgs.python312
            pkgs.python312Packages.pydbus
            pkgs.python312Packages.pygobject3
            pkgs.python312Packages.tkinter
            pkgs.gobject-introspection

            # System plumbing
            pkgs.pkg-config
            pkgs.dbus
            pkgs.glib
            pkgs.systemd
            pkgs.polkit

            # Tauri deps
            pkgs.nodejs_20
            pkgs.openssl
            pkgs.webkitgtk_4_1
            pkgs.gtk3
            pkgs.libsoup_3
            pkgs.libayatana-appindicator
            pkgs.librsvg
            
            # add core graphics/text stack so we don't chase .so's:
            pkgs.cairo pkgs.pango pkgs.harfbuzz pkgs.at-spi2-core

            # Rust toolchain (>=1.81) via fenix
            fenixPkgs.stable.toolchain

            # Runtime helpers for GTK/WebKit
            pkgs.gsettings-desktop-schemas
            pkgs.glib-networking
            
            pkgs.gdk-pixbuf
          ];
          shellHook = ''
            export PATH="${customBluez}/bin:${customBluez}/sbin:$PATH"
            export LD_LIBRARY_PATH="${customBluez}/lib:$LD_LIBRARY_PATH"
            export PKG_CONFIG_PATH="${customBluez}/lib/pkgconfig:$PKG_CONFIG_PATH"
            echo "Entered Nix env with custom BlueZ 5.66. Run your app here."
            
            # One big library path for GTK/WebKit and friends (no more one-by-one misses)
            export LD_LIBRARY_PATH="${lib.makeLibraryPath [
              pkgs.gtk3 pkgs.webkitgtk_4_1 pkgs.libsoup_3 pkgs.libayatana-appindicator
              pkgs.librsvg pkgs.gdk-pixbuf pkgs.cairo pkgs.pango pkgs.harfbuzz
              pkgs.at-spi2-core pkgs.openssl pkgs.glib
            ]}:$LD_LIBRARY_PATH"

            # GTK/WebKit runtime libs for the Tauri dev run
            export LD_LIBRARY_PATH="${pkgs.gtk3}/lib:${pkgs.webkitgtk_4_1}/lib:${pkgs.libsoup_3}/lib:${pkgs.libayatana-appindicator}/lib:${pkgs.librsvg}/lib:${pkgs.openssl.out}/lib:$LD_LIBRARY_PATH"

            # GSettings schemas so WebKit/GTK can find settings at runtime
            export XDG_DATA_DIRS="${pkgs.gsettings-desktop-schemas}/share:${pkgs.gtk3}/share:''\${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"

            # TLS, proxies, etc. for GLib/GIO (WebKit/GTK use this)
            export GIO_EXTRA_MODULES="${pkgs.glib-networking}/lib/gio/modules"
            
            # gdk-pixbuf runtime (image loaders)
            export GDK_PIXBUF_MODULEDIR="${pkgs.gdk-pixbuf}/lib/gdk-pixbuf-2.0/2.10.0/loaders"
            export GDK_PIXBUF_MODULE_FILE="${pkgs.gdk-pixbuf}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
            
            export LIBGL_ALWAYS_SOFTWARE=1
            export WEBKIT_DISABLE_COMPOSITING_MODE=1

          '';
        };
      }
    );
}

