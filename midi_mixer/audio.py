"""PulseAudio helpers: listing apps, matching sink inputs, muting."""
import re


def display_name(si) -> str:
    props = getattr(si, "proplist", {}) or {}
    for key in ("application.name", "application.process.binary", "media.name"):
        v = props.get(key)
        if v:
            return v
    return f"sink_input_{getattr(si, 'index', None) or 'unknown'}"


def list_running_apps(pulse):
    apps = set()
    for si in pulse.sink_input_list():
        props = getattr(si, "proplist", {}) or {}
        name = display_name(si)
        binary = props.get("application.process.binary")
        pid = props.get("application.process.id") or props.get("application.pid")
        suffix = []
        if binary:
            suffix.append(binary)
        if pid:
            suffix.append(f"pid:{pid}")
        apps.add(f"{name} ({', '.join(suffix)})" if suffix else name)
    return ["MASTER"] + sorted(apps)


def target_base_name(target: str) -> str:
    """Strip the " (binary, pid:123)" suffix added by list_running_apps."""
    m = re.match(r"^(.*?)\s*\([^()]*\)\s*$", target or "")
    return (m.group(1) if m else target or "").strip().lower()


def si_matches_target(si, target: str) -> bool:
    """True if sink input `si` belongs to the app named by `target`.

    Exact (case-insensitive) match on the app name, the process binary or the
    media name, so all streams of an app match (e.g. several Firefox tabs) but
    unrelated apps never do. The pid in a saved label is ignored on purpose: it
    changes every time the app restarts.
    """
    props = getattr(si, "proplist", {}) or {}
    names = {
        (props.get("application.name") or "").lower(),
        (props.get("application.process.binary") or "").lower(),
        (props.get("media.name") or "").lower(),
    }
    names.discard("")
    base = target_base_name(target)
    return bool(base) and (base in names or (target or "").strip().lower() in names)


def set_mute(pulse, target: str, muted: bool):
    """Mute/unmute MASTER or every stream of `target`. Returns (name, volume) or None if nothing matched."""
    if target == "MASTER":
        sink = pulse.get_sink_by_name(pulse.server_info().default_sink_name)
        pulse.mute(sink, muted)
        return "MASTER", pulse.volume_get_all_chans(sink)
    matched = [si for si in pulse.sink_input_list() if si_matches_target(si, target)]
    for si in matched:
        pulse.mute(si, muted)
    if not matched:
        return None
    return display_name(matched[0]), pulse.volume_get_all_chans(matched[0])
