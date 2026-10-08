"""Reinterpret original times using an explicit policy, never hidden UTC fallback."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiops_v4.data.discovery import TIME_FIELDS
from aiops_v4.data.reader import is_missing


def time_zone(name: str) -> ZoneInfo:
    if not name or not isinstance(name, str):
        raise ValueError('naive_timezone must be an explicit IANA timezone')
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f'unknown or unavailable timezone: {name}') from error


def observation_time(record, naive_timezone: str) -> tuple[datetime, tuple[str, ...]]:
    zone = time_zone(naive_timezone)
    for field in TIME_FIELDS[record.source]:
        value = record.raw.get(field)
        if is_missing(value):
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().strip('"').replace('Z', '+00:00'))
        except ValueError:
            continue
        flags = ()
        if parsed.tzinfo is None:
            local = parsed.replace(tzinfo=zone, fold=0)
            restored = local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
            if restored != parsed:
                raise ValueError(f'nonexistent local time: {value} in {naive_timezone}')
            if local.utcoffset() != parsed.replace(tzinfo=zone, fold=1).utcoffset():
                raise ValueError(f'ambiguous local time: {value} in {naive_timezone}')
            parsed, flags = local, (f'naive_timezone:{naive_timezone}',)
        return parsed.astimezone(timezone.utc), flags
    raise ValueError('no parseable original timestamp')


def window_start(value: datetime, width: int) -> datetime:
    if type(width) is not int or width <= 0:
        raise ValueError('window_seconds must be positive')
    if value.tzinfo is None:
        raise ValueError('window timestamp must include timezone')
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch + timedelta(seconds=(value - epoch).total_seconds() // width * width)


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
