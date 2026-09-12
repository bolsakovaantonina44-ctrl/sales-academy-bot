"""Academy package bootstrap."""
from . import domain as _domain
from .report_text import render_report as _render_report, partial_report as _partial_report

_domain.render_report = _render_report
_domain.partial_report = _partial_report

from .safety_guards import install as _install_safety_guards
_install_safety_guards()

# Import installs a one-time Store hook that queues the commercial CTA for users
# who had already completed all free trainings before this release.
from . import commercial_cta as _commercial_cta  # noqa: F401,E402
