# Vendored: wsdd

- Project: wsdd - Web Service Discovery host daemon, by Steffen Christgau (https://github.com/christgau/wsdd)
- Version: 0.8 (copied from the Fedora package wsdd-0.8-6.fc44, /usr/bin/wsdd)
- License: MIT (see LICENSE-wsdd)
- sha256 of wsdd.py: b161354fe528e7d9e7e0802e4675875cabb2ee59217cc5a9c599497d7f7dd937

Why vendored: distros package it under different names, units and config files
(wsdd / wsdd-server / wsdd2 / EPEL-only / absent), so lan-share ships one copy and runs
it under its own lan-share-wsdd.service. Do not edit wsdd.py; replace it wholesale when updating.
Known upstream issue: `-vv` crashes on Python 3.14 (we only pass `-v`).
