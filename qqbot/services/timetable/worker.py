"""Quiet entry point for the isolated timetable parser process.

Importing a NoneBot plugin package can emit framework initialization logs to
stdout.  The worker protocol reserves stdout for one JSON document, so import
the plugin implementation behind a temporary stream and restore stdout before
writing the result.
"""

from contextlib import redirect_stdout
from io import StringIO


with redirect_stdout(StringIO()):
    from services.timetable.ics import _worker_main


if __name__ == "__main__":
    _worker_main()
