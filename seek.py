import re


def parse_seek_position(value: str) -> int:
    """Parse seconds, MM:SS, or HH:MM:SS into a non-negative number of seconds."""
    if not re.fullmatch(r"[0-9]+(?::[0-9]{1,2}){0,2}", value.strip()):
        raise ValueError("use seconds, MM:SS, or HH:MM:SS")

    parts = [int(part) for part in value.split(":")]
    if len(parts) > 1 and any(part >= 60 for part in parts[1:]):
        raise ValueError("minutes and seconds must be less than 60")

    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]
