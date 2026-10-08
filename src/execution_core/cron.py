"""Five-field UTC cron, with GitHub's day-of-month / day-of-week rule.

A tick is one UTC minute. When both the day-of-month and the day-of-week
fields are restricted, a date matches if either field matches. A literal
``*`` is the only unrestricted field. Names ``JAN`` through ``DEC`` and
``SUN`` through ``SAT`` are accepted in either case. ``7`` is Sunday.

https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
"""

from datetime import timedelta, UTC

from .protocol import invalid

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_DAYS = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
# A Feb 29 search can cross a non-leap century. Eight years is enough.
_LOOKBACK = timedelta(days=366 * 8)


class Cron:
    """One parsed schedule. Fields are inclusive sets. ``*_any`` is a star."""

    def __init__(self, minutes, hours, dom, months, dow, dom_any, dow_any):
        self.minutes = minutes
        self.hours = hours
        self.dom = dom
        self.months = months
        self.dow = dow
        self.dom_any = dom_any
        self.dow_any = dow_any


def parse_cron(expression):
    """Return a `Cron`. An unusable expression is `INVALID_PARAMS`."""

    if not isinstance(expression, str):
        invalid("on.schedule cron is not accepted")
    fields = expression.split()
    if len(fields) != 5:
        invalid("on.schedule cron is not accepted")
    minutes, minute_any = _field(fields[0], 0, 59, {})
    hours, hour_any = _field(fields[1], 0, 23, {})
    dom, dom_any = _field(fields[2], 1, 31, {})
    months, month_any = _field(fields[3], 1, 12, _MONTHS)
    dow, dow_any = _field(fields[4], 0, 7, _DAYS)
    if 7 in dow:
        dow = frozenset(value for value in dow if value != 7) | {0}
    del minute_any, hour_any, month_any
    return Cron(minutes, hours, dom, months, dow, dom_any, dow_any)


def cron_matches(cron, moment):
    """Return whether `moment` is a tick of `cron`. Seconds are ignored."""

    moment = _utc_minute(moment)
    if moment.minute not in cron.minutes or moment.hour not in cron.hours:
        return False
    if moment.month not in cron.months:
        return False
    dom_ok = moment.day in cron.dom
    # datetime weekday is Monday=0. Cron weekday is Sunday=0.
    dow_ok = (moment.weekday() + 1) % 7 in cron.dow
    if cron.dom_any and cron.dow_any:
        return True
    if cron.dom_any:
        return dow_ok
    if cron.dow_any:
        return dom_ok
    return dom_ok or dow_ok


def schedule_decision(expressions, last_fired, now):
    """Return ``(fire_at, cursor)``.

    ``fire_at`` is the single latest due tick after ``last_fired`` and at or
    before ``now``, or None. The first observation records that latest tick
    as ``cursor`` and fires nothing. A later call with the same cursor does
    not fire it again. Several missed ticks produce one catch-up, at the
    newest of them.
    """

    crons = [parse_cron(expression) for expression in expressions]
    now_minute = _utc_minute(now)
    latest = _latest(crons, now_minute)
    if latest is None:
        return None, _minute(last_fired) if last_fired is not None else None
    if last_fired is None:
        return None, latest
    previous = _minute(last_fired)
    if latest <= previous:
        return None, previous
    return latest, latest


def _utc_minute(moment):
    if moment.tzinfo is None:
        invalid("on.schedule time must be UTC")
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


def _minute(moment):
    return _utc_minute(moment)


def _latest(crons, moment):
    cursor = moment
    limit = moment - _LOOKBACK
    while cursor >= limit:
        if any(cron_matches(cron, cursor) for cron in crons):
            return cursor
        cursor -= timedelta(minutes=1)
    return None


def _field(text, low, high, names):
    if text == "":
        invalid("on.schedule cron is not accepted")
    any_star = text == "*"
    values = set()
    for part in text.split(","):
        values.update(_piece(part, low, high, names))
    return frozenset(values), any_star


def _piece(part, low, high, names):
    if part == "":
        invalid("on.schedule cron is not accepted")
    step = 1
    base = part
    ranged = False
    if "/" in part:
        base, step_text = part.split("/", 1)
        if not step_text.isdigit() or (step_text.startswith("0") and step_text != "0"):
            invalid("on.schedule cron is not accepted")
        step = int(step_text)
        if step < 1:
            invalid("on.schedule cron is not accepted")
        ranged = True
    start, end = _span(base, low, high, names, ranged)
    if start > end:
        invalid("on.schedule cron is not accepted")
    return range(start, end + 1, step)


def _span(base, low, high, names, ranged):
    if base == "*":
        return low, high
    if "-" in base:
        left, right = base.split("-", 1)
        return _number(left, low, high, names), _number(right, low, high, names)
    number = _number(base, low, high, names)
    if ranged:
        return number, high
    return number, number


def _number(text, low, high, names):
    if text in names:
        return names[text]
    folded = text.upper()
    if folded in names:
        return names[folded]
    if not text.isdigit():
        invalid("on.schedule cron is not accepted")
    value = int(text)
    if value < low or value > high:
        invalid("on.schedule cron is not accepted")
    return value
