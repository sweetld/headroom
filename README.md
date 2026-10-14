# Headroom

A small, always-on-top gauge of memory and swap health for the Linux desktop.

![Headroom in its "swapping to disk" state (demo data)](screenshot.png)

It answers one question: is this machine about to freeze? On a machine with
zram (compressed swap held in RAM) plus a swap partition on a slow disk, the
freezes come when swap spills out of zram onto the disk. System Monitor and
GKrellM add every swap device into one total, which hides exactly that.
Headroom keeps **fast swap** (zram) and **disk swap** apart and says in words
which one is in use.

It shows:

- RAM in use, fast swap (with its compression ratio) and disk swap
- pressure stall graphs: time spent waiting on disk and on memory
- swap traffic to disk and to zram, and CPU
- how much of the desktop shell (gnome-shell) has been swapped out

## Requirements

Python 3 with PyGObject (GTK 3), Pango and pycairo. These come with most GNOME
desktops (`python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`). It reads `/proc`
and `/sys` only: no root, no daemon. It was written for Pop!_OS but should work
on any GTK desktop.

## Usage

```bash
./headroom.py                     # run the widget
./headroom.py --demo              # run it on made-up data, to see every state
./headroom.py --screenshot F.png  # render the widget to a PNG and exit
./install.sh                      # add it to the applications menu
./uninstall.sh                    # remove the menu entry and login item
```

Right-click the widget for its size, "Start at login" and other options.
Settings are kept in `~/.config/headroom/`.

## Why GTK and not Electron

A memory monitor must not add to memory pressure. GTK 3 and Cairo keep it to a
few tens of MB, where an Electron app would cost several hundred.

## Licence

MIT. See [LICENSE](LICENSE).
