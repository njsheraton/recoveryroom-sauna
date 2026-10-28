"""Recovery Room sauna controller.

A fresh, independently owned controller for one Clearlight sauna and an
always-running shop Mac. Built from the engineering handoff (spec v1.0,
29 Oct 2026). Nothing in this package is copied from, or connects to, the
home installation.

Phase status (see docs/plan-and-inputs.md):
  phase 1-2  protocol, simulator, read-only LAN worker, status-only UI   [this build]
  phase 3    authenticated commands, serialised worker, durable Stop     [not built]
  phase 4    business-hours supervisor, arming, owner/staff roles        [policy maths only]
  phase 5-6  install, commissioning, pilot, scheduled preheat            [not started]
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
