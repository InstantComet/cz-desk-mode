#!/bin/bash
# Build cz-desk-mode_<version>_all.deb into ./build. Needs dpkg-deb (any Debian/Ubuntu).
set -euo pipefail
cd "$(dirname "$0")"
VERSION=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' app-builder.json | head -1)
PKG=cz-desk-mode
python3 -m py_compile $PKG.py app/app.py
root=$(mktemp -d); trap 'rm -rf "$root"' EXIT
chmod 0755 "$root"
install -D -m 0755 $PKG.py                "$root/usr/lib/$PKG/$PKG.py"
install -D -m 0644 $PKG.service           "$root/usr/lib/systemd/system/$PKG.service"
install -D -m 0644 70-$PKG.rules          "$root/usr/lib/udev/rules.d/70-$PKG.rules"
install -D -m 0644 $PKG.default           "$root/etc/default/$PKG"
install -D -m 0644 app/app.py             "$root/usr/share/APPLaunch/apps/$PKG/app.py"
install -D -m 0644 app/$PKG.desktop       "$root/usr/share/APPLaunch/applications/$PKG.desktop"
install -D -m 0644 app/$PKG.png           "$root/usr/share/APPLaunch/share/images/$PKG.png"
install -D -m 0644 LICENSE                "$root/usr/share/doc/$PKG/copyright"
install -d -m 0755 "$root/DEBIAN"
sed "s/@VERSION@/$VERSION/" debian/control.in > "$root/DEBIAN/control"
echo "/etc/default/$PKG" > "$root/DEBIAN/conffiles"
for s in postinst prerm postrm; do install -m 0755 debian/$s "$root/DEBIAN/$s"; done
echo "Installed-Size: $(du -sk --exclude=DEBIAN "$root" | cut -f1)" >> "$root/DEBIAN/control"
mkdir -p build
dpkg-deb --root-owner-group -Zxz --build "$root" "build/${PKG}_${VERSION}_all.deb"
