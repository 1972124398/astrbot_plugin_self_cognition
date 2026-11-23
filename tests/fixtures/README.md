# Companion v1 test fixture

`companion_v1.py` is a test copy of the dependency-free protocol bundled with W1's
`astrbot_plugin_play_together` (0BSD), captured on 2026-11-23. It is used only when
the sibling companion checkouts are absent, so CI verifies optional integration
without requiring those plugins as runtime dependencies.

When sibling checkouts exist, the integration tests load each plugin's actual
protocol module independently instead. No peer source files are modified.
