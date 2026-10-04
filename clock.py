"""Time and Date — what the two lines of the card say.

Pure functions: no Windows, no window, no clock of its own, so all of it is
testable without starting a widget.

    >>> face(datetime(2026, 9, 26, 15, 7), hour24=True)
    ('15:07', 'Saturday, 26 September 2026')

The date is the format asked for: weekday, day, month, year -- en-GB order, with
no leading zero on the day ("5 September", never "05 September"), and English
names regardless of the machine's locale, so the card cannot change shape because
of a regional setting.

The 12-hour face follows the same UK convention: no leading zero on the hour and a
lowercase "am"/"pm" ("9:07 am", "12:07 pm").  The 24-hour face pads the hour
("09:07"), which is how it is written here.
"""

from __future__ import annotations

from datetime import datetime

DAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

#: Nothing shorter than this between wake-ups, so a wake-up that lands a hair
#: before the minute boundary cannot turn into a busy loop.
MIN_WAIT = 0.05


def date_text(moment: datetime) -> str:
    """e.g. ``Saturday, 26 September 2026``."""
    return f"{DAYS[moment.weekday()]}, {moment.day} {MONTHS[moment.month - 1]} {moment.year}"


def time_text(moment: datetime, hour24: bool = True, seconds: bool = False) -> str:
    """e.g. ``15:07``, ``3:07 pm``, or with seconds ``15:07:42`` / ``3:07:42 pm``."""
    tail = f":{moment.second:02d}" if seconds else ""
    if hour24:
        return f"{moment.hour:02d}:{moment.minute:02d}{tail}"
    hour = moment.hour % 12 or 12
    meridiem = "am" if moment.hour < 12 else "pm"
    return f"{hour}:{moment.minute:02d}{tail} {meridiem}"


def face(
    moment: datetime, hour24: bool = True, seconds: bool = False, show_date: bool = True
) -> tuple[str, str]:
    """The two lines, as the page draws them.  A hidden date is an empty second line."""
    return time_text(moment, hour24, seconds), date_text(moment) if show_date else ""


def wait_for_next_change(moment: datetime, hour24: bool = True) -> float:
    """Seconds until the face stops being current.

    Nothing smaller than a minute is ever shown, so the host can sleep until the
    next minute boundary instead of waking every second.  In 12-hour mode the face
    changes on exactly the same boundaries, so ``hour24`` is accepted only to keep
    the call sites obvious.
    """
    remaining = 60.0 - moment.second - moment.microsecond / 1_000_000
    return max(MIN_WAIT, remaining)


def tip(moment: datetime, hour24: bool = True) -> str:
    """The tray tooltip: the time first, because that is what changes."""
    return f"{time_text(moment, hour24)}  ·  {date_text(moment)}"
