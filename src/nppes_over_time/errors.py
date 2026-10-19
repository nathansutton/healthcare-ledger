"""One exception type for problems the user can fix.

The CLI prints a ``UserError`` as a single line (problem, cause, next step) and exits
with its ``exit_code``: 2 for bad input (nothing was created or changed), 1 for a check
that failed. Anything else is a bug and gets a traceback only with ``-v``.
"""

from __future__ import annotations


class UserError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


class PrivacyRefusal(UserError, RuntimeError):
    """The warehouse carries a tax id or SSN; nothing may be published."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__(
            "refusing to export; warehouse fails privacy checks (" + "; ".join(self.problems) + "). "
            "Do not publish this build. Report it to the maintainer (docs/MAINTAINING.md, "
            "'Privacy refusal'); updating the scrub rules is a maintainer task.")
