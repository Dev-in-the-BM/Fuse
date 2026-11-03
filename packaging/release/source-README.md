# Fuse source

Fuse is a GTK 4/libadwaita MediaTek device client.

## Run from this source tree

The system must provide Python 3.10+, PyGObject, GTK 4, Cairo, and libadwaita
1.4 or newer. Install those runtime dependencies with your distribution's
package manager, then run:

```sh
chmod +x run.sh
./run.sh
```

To install this source tree for your user and create a GNOME app-grid shortcut:

```sh
./install.sh
```

The MTK Client backend is installed separately from Fuse's Preferences, or an
existing checkout can be selected there. The installer does not use `sudo` or
install system packages.
