# CodexQuotaTray integration

Source: https://github.com/SYD-Official/CodexQuotaTray

Pinned upstream commit: `a89f33f0aa8395d421567f83f7720beb7c08b7d3` (version 2.6.0).

Copyright (c) 2026 SYD-Official. MIT license: `../../licenses/CodexQuotaTray-MIT.txt`.

This is a derivative component of Codex Session Usage, not an official
CodexQuotaTray release. Original native quota, taskbar, rendering, settings,
completion notification and parser tests are retained. The combined frontend
uses the existing supervisor and metadata-only usage bridge, shares a settings
directory, and never installs the upstream quota-only executable as an update.

The unified activity timeline comes from the session accounting engine instead
of a second tail-only counter. Missing request timestamps remain unbucketed.
The derivative does not read auth.json; plan labels use the quota response only.
