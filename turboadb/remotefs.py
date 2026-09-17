"""Device-side file helpers: shell command builders, lossless ``ls -l``
parsing, folder listing and path probing.

Shared by the Files tab (``gui/file_browser.py``), :class:`ADBHandler`'s file
methods and the CLI. Functions that talk to a device take a *handler* offering
``shell(command, timeout=..., safe=False)``; nothing here imports Qt.
"""

from __future__ import annotations

import mimetypes
import os
import posixpath
import re
import shlex
from typing import List, Optional, Tuple

from .results import strip_ansi


# File names are captured losslessly: ``ls -l`` puts exactly ONE space between
# the date and the name, so leading/trailing spaces belong to the name.  ``?``
# fields are what toybox prints for entries it cannot stat.
_LS_PERMS = r'[bcdlpsw?-][rwxstST?-]{9}'
_LS_DATE = (r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}'
            r'|'
            r'[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4})')
_LS_REGEX = re.compile(
    r'^(' + _LS_PERMS + r')[.+@]?\s+'
    r'(\d+|\?)\s+'                  # link count
    r'(\S+)\s+'
    r'(\S+)\s+'
    r'(\d+,\s*\d+|\d+|\?)\s+'       # size, or "major, minor" for device nodes
    r'(' + _LS_DATE + r'|\?)'
    r' (.+)\Z'
)
# Old toolbox ``ls -l``: no link count, and no size for folders and links.
_LS_TOOLBOX_REGEX = re.compile(
    r'^(' + _LS_PERMS + r')\s+(\S+)\s+(\S+)\s+'
    r'(?:(\d+,\s*\d+|\d+)\s+)?'
    r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})'
    r' (.+)\Z'
)
# Anything else shaped "perms links user group size DATE name".  The date is
# matched as a WHOLE, because a locale listing writes it in three tokens
# ("sept. 14  2026"): counting two tokens instead left the year glued to the
# front of the file name.
_LS_LOCALE_MONTH = r'[^\W\d_]{2,}\.?'          # "Jan", "sept.", "окт."
_LS_CLOCK_OR_YEAR = r'\d{1,2}:\d{2}(?::\d{2})?|\d{4}'
_LS_FALLBACK_DATE = (
    r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s+[+-]\d{4})?'
    r'|' + _LS_LOCALE_MONTH + r'\s+\d{1,2}\s+(?:' + _LS_CLOCK_OR_YEAR + r')'
    r'|\d{1,2}\s+' + _LS_LOCALE_MONTH + r'\s+(?:' + _LS_CLOCK_OR_YEAR + r')'
)
_LS_FALLBACK_REGEX = re.compile(
    r'^(' + _LS_PERMS + r')[.+@]?\s+(?:\d+|\?)\s+(\S+)\s+(\S+)\s+(\d+)\s+'
    r'(' + _LS_FALLBACK_DATE + r') (.+)\Z'
)
# Last resort: two unidentified tokens where the date belongs.  The name cannot
# be trusted (an unrecognised three-token date puts its tail in front of it), so
# rows parsed this way are reported as inexact and the listing is retried.
_LS_LOOSE_REGEX = re.compile(
    r'^(' + _LS_PERMS + r')[.+@]?\s+(?:\d+|\?)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\S+\s+\S+) (.+)\Z'
)
_LS_TOTAL_RE = re.compile(r'^total \d+\Z')
_ANSI_CSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_PERMS_RE = re.compile(r'^' + _LS_PERMS + r'[.+@]?$')


def _human_size(num_bytes: int) -> str:
    """Format bytes into a clean, human-readable string ("500 B", "1.0 KB")."""
    if num_bytes <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024.0:
            return f"{num_bytes:3.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


# ---- device shell command builders (every path shlex-quoted) ----------------


def _dir_arg(path: str) -> str:
    """*path* with a trailing slash, so a symlinked folder lists its target."""
    return path if path.endswith("/") else path + "/"


def _ls_cmd(path: str) -> str:
    return f"ls -la {shlex.quote(_dir_arg(path))}"


def _ls_escaped_cmd(path: str) -> str:
    """``ls -lab``: names with newlines, control characters or ' -> ' come back escaped."""
    return f"ls -lab {shlex.quote(_dir_arg(path))}"


def _mkdir_cmd(path: str) -> str:
    return f"mkdir -p {shlex.quote(path)}"


def _touch_cmd(path: str) -> str:
    """``touch``, falling back to a create-only redirect.  The fallback checks
    the file first: ``: > file`` alone TRUNCATED an existing file on builds
    whose toybox refuses ``touch`` (e.g. a read-only mtime)."""
    q = shlex.quote(path)
    return f"touch {q} 2>/dev/null || [ -e {q} ] || : > {q}"


# Superseded by _copy_into_cmd / _rename_cmd, which add the ``--`` terminator
# and mv's ``-T``/``-n`` guards.  Nothing here calls them any more; they are
# kept only because gui/file_browser.py still imports the pair.  Use the two
# builders below for new code.
def _cp_cmd(src: str, dst: str) -> str:
    return f"cp -r {shlex.quote(src)} {shlex.quote(dst)}"


def _mv_cmd(src: str, dst: str) -> str:
    return f"mv {shlex.quote(src)} {shlex.quote(dst)}"


def _copy_into_cmd(src: str, dst: str, merge: bool) -> str:
    """Device copy of *src* to exactly *dst*.  *merge* copies a folder's contents
    into an existing *dst* folder (``src/.``) instead of nesting ``dst/name``."""
    source = src.rstrip("/") + "/." if merge else src
    return f"cp -r -- {shlex.quote(source)} {shlex.quote(dst)}"


def _rename_check_cmd(src: str, dst: str) -> str:
    """Prints ``free``, ``same`` (same file, e.g. a case-only rename), ``dir`` or ``file``."""
    s, d = shlex.quote(src), shlex.quote(dst)
    return (f"if [ -e {d} ] || [ -L {d} ]; then "
            f"if [ ! -L {s} ] && [ ! -L {d} ] && [ {s} -ef {d} ]; then echo same; "
            f"elif [ -d {d} ] && [ ! -L {d} ]; then echo dir; else echo file; fi; "
            f"else echo free; fi")


def _rename_cmd(src: str, dst: str, overwrite: bool = False, verify: bool = True) -> str:
    """``mv -T`` never moves *src* INTO an existing folder.  Without *overwrite*,
    ``-n`` refuses to replace anything; toybox then still exits 0, so the move
    is verified."""
    s, d = shlex.quote(src), shlex.quote(dst)
    if overwrite:
        return f"mv -f -T -- {s} {d}"
    if not verify:
        return f"mv -T -- {s} {d}"
    return (f"mv -n -T -- {s} {d} && if [ -e {s} ] || [ -L {s} ]; then "
            f"echo 'mv: not renamed: the new name already exists' >&2; exit 1; fi")


def _probe_cmd(paths) -> str:
    """One line per path: ``<kind><link> <resolved>``.  *kind* is d (folder),
    f (anything else), b (broken symlink) or n (missing); *link* is ``L`` or
    ``-``; folders and links also get their ``readlink -f`` target."""
    quoted = " ".join(shlex.quote(p) for p in paths)
    return (f'for f in {quoted}; do '
            'if [ -L "$f" ]; then l=L; else l=-; fi; '
            'if [ -d "$f" ]; then t=d; elif [ -e "$f" ]; then t=f; '
            'elif [ -L "$f" ]; then t=b; else t=n; fi; '
            'r=; if [ "$l" = L ] || [ "$t" = d ]; then r=$(readlink -f -- "$f" 2>/dev/null); fi; '
            "printf '%s%s %s\\n' \"$t\" \"$l\" \"$r\"; done")


def _edit_stat_cmd(path: str) -> str:
    """``<size> <octal mode> <type>`` of what *path* points to, then its real path."""
    q = shlex.quote(path)
    return (f"stat -L -c '%s %a %F' -- {q} || exit 1; "
            f"readlink -f -- {q} 2>/dev/null || printf '%s\\n' {q}")


def _mode_cmd(path: str) -> str:
    return f"stat -c %a -- {shlex.quote(path)}"


def _chmod_cmd(mode: str, path: str) -> str:
    return f"chmod {shlex.quote(mode)} -- {shlex.quote(path)}"


def _rm_cmd(paths) -> str:
    return "rm -rf " + " ".join(shlex.quote(p) for p in paths)


def _link_dirs_cmd(paths) -> str:
    """One line per path, in order: ``d`` if it resolves to a directory, else ``f``."""
    quoted = " ".join(shlex.quote(p) for p in paths)
    return f'for f in {quoted}; do if [ -d "$f" ]; then echo d; else echo f; fi; done'


def _normalize_remote_path(path: str) -> str:
    """Absolute, normalised device path (never starts with '-' or '//').

    Only line breaks are trimmed: a leading or trailing SPACE is a legal part of
    an Android file name, and stripping it made ``rm "/sdcard/dup "`` delete the
    neighbouring ``/sdcard/dup`` instead.
    """
    path = (path or "").strip("\r\n")
    if not path:
        return "/"
    return posixpath.normpath("/" + path.lstrip("/"))


def _as_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _result_error(res) -> str:
    return _as_text(getattr(res, "stderr", "")).strip() or getattr(res, "text", "") or "failed"


def _output_lines(text) -> List[str]:
    """Shell stdout split on ``\\n`` only, tolerating ``\\r\\n`` line ends.

    Colour escapes are removed first: on a colourising device shell they made
    every probe reply look unexpected, so ``rm``/``mv``/``cp`` failed outright.
    """
    lines = strip_ansi(_as_text(text)).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _chunks(paths, size: int = 50, max_chars: int = 24000):
    """Split *paths* into shell-command-sized groups."""
    chunk, used = [], 0
    for p in paths:
        if chunk and (len(chunk) >= size or used + len(p) > max_chars):
            yield chunk
            chunk, used = [], 0
        chunk.append(p)
        used += len(p) + 3
    if chunk:
        yield chunk


# ---- ls parsing --------------------------------------------------------------


class _Listing(list):
    """Rows of a device listing plus the names that could not be parsed exactly."""

    def __init__(self, rows=(), uncertain=()):
        super().__init__(rows)
        self.uncertain = set(uncertain)


_LS_B_ESCAPES = {"a": 7, "b": 8, "e": 27, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11}


def _unescape_ls_b(text: str) -> Tuple[str, bool]:
    """Decode a toybox ``ls -b`` name (``\\ ``, ``\\\\``, ``\\n``, ``\\ooo`` …).

    Returns ``(name, exact)``; *exact* is False when the bytes are not UTF-8.
    """
    out = bytearray()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            octal = text[i + 1:i + 4]
            if nxt in _LS_B_ESCAPES:
                out.append(_LS_B_ESCAPES[nxt])
                i += 2
            elif len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
            else:
                out.extend(nxt.encode("utf-8", "surrogatepass"))
                i += 2
            continue
        out.extend(ch.encode("utf-8", "surrogatepass"))
        i += 1
    try:
        return out.decode("utf-8"), True
    except UnicodeDecodeError:
        return out.decode("utf-8", "replace"), False


def _split_link(rest: str, size: int) -> Tuple[str, str, bool]:
    """Split ``name -> target`` exactly, using that a symlink's size is the byte
    length of its target.  Returns ``(name, target, exact)``."""
    sep = " -> "
    if size > 0:
        raw = rest.encode("utf-8", "surrogatepass")
        bsep = sep.encode("ascii")
        cut = len(raw) - size - len(bsep)
        if cut > 0 and raw[cut:cut + len(bsep)] == bsep:
            try:
                return raw[:cut].decode("utf-8"), raw[cut + len(bsep):].decode("utf-8"), True
            except UnicodeDecodeError:
                pass
    name, found, target = rest.partition(sep)
    return name, target, bool(found) and rest.count(sep) == 1


def _parse_ls_entry(line: str, escaped: bool = False):
    """``(row, exact)`` for one ``ls -l`` line (see :func:`_parse_ls_line`), or None.

    *exact* is False when the name can't be recovered with certainty (e.g. a
    link whose name contains ' -> ').  *escaped* parses ``ls -b`` output.
    """
    loose = False
    m = _LS_REGEX.match(line)
    if m:
        perms, _links, user, group, size_field, mtime, rest = m.groups()
    else:
        m = _LS_TOOLBOX_REGEX.match(line) or _LS_FALLBACK_REGEX.match(line)
        if m is None:
            m = _LS_LOOSE_REGEX.match(line)
            if m is None:
                return None
            loose = True  # the date wasn't recognised: the name may be truncated
        perms, user, group, size_field, mtime, rest = m.groups()
        size_field = size_field or ""
    perms = perms[:10]
    kind = perms[0]
    unknown = "?" in perms or size_field == "?"  # toybox couldn't stat the entry
    exact = not loose
    target = ""
    if kind == "l":
        if escaped:  # spaces in the name are escaped, so the first ' -> ' splits
            name, found, target = rest.partition(" -> ")
            exact = exact and bool(found)
        else:
            name, target, split_ok = _split_link(
                rest, int(size_field) if size_field.isdigit() else -1
            )
            exact = exact and split_ok
    else:
        name = rest
    if escaped:
        name, decoded = _unescape_ls_b(name)
        exact = exact and decoded
    elif "�" in name:
        exact = False  # undecodable bytes: the real name is unknown
    if not name or "/" in name:
        return None  # no directory entry looks like this

    owner = f"{user}:{group}"
    is_dir = kind == "d"
    if kind in ("b", "c") and not unknown:
        raw_size = 0
        sz_str = re.sub(r",\s*", ", ", size_field)  # "major, minor"
        ftype = "Block Device" if kind == "b" else "Character Device"
    else:
        raw_size = int(size_field) if size_field.isdigit() else 0
        if kind == "l":
            # ls -l describes the link itself; the target type is resolved by
            # _list_remote_dir.  A trailing slash is the only hint available here.
            is_dir = target.endswith("/")
            ftype = "Folder Link" if is_dir else "File Link"
        elif kind == "p":
            ftype = "Pipe"
        elif kind == "s":
            ftype = "Socket"
        elif is_dir:
            ftype = "Folder"
        else:
            ext = os.path.splitext(name)[1].lower()
            ftype = mimetypes.types_map.get(ext, ext.upper() or "File")
        sz_str = "<DIR>" if is_dir else _human_size(raw_size)
    if unknown:
        # "-?????????  ? ?  ?  ? ? name": listed, but its details are unreadable.
        if not is_dir:
            ftype = "Unknown"
            sz_str = "Unknown"
        if mtime == "?":
            mtime = "Unknown"
    return (name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir), exact


def _parse_ls_line(line: str):
    """Parse one ANSI-free ``ls -la`` line into
    ``(name, raw_size, size_text, type, mtime, perms, owner, is_dir)``.

    Only the line break is removed: leading/trailing spaces belong to the name.
    """
    parsed = _parse_ls_entry(line.rstrip("\r\n"))
    return parsed[0] if parsed else None


def _parse_ls_listing(text, escaped: bool = False):
    """``(rows, uncertain_names, clean)`` for a whole ``ls -la`` (or ``-lab``) output.

    *clean* is False when a line didn't parse or a name isn't exact — a name
    containing a newline splits into two lines, for example — and the listing
    should be retried with ``ls -lab``.  Rows next to such lines, inexact names
    and duplicated names are reported in *uncertain_names*.
    """
    text = _as_text(text)
    clean = True
    if "\x1b" in text:
        text = _ANSI_CSI_RE.sub("", text)  # colours from a pty
        clean = escaped  # a raw ESC inside a name can't be told apart from colour
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = [ln for ln in lines if ln]
    if body and all(ln.endswith("\r") for ln in body):
        lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]
    rows, uncertain, seen = [], set(), set()
    prev = None
    for ln in lines:
        if prev is None and (not ln or _LS_TOTAL_RE.match(ln)):
            continue
        parsed = _parse_ls_entry(ln, escaped) if ln else None
        if parsed is None:
            clean = False
            if prev is not None:
                uncertain.add(prev)  # probably the first part of a multi-line name
            continue
        row, exact = parsed
        name = prev = row[0]
        if name in (".", ".."):
            continue
        if not exact or name in seen:
            clean = False
            uncertain.add(name)
        seen.add(name)
        rows.append(row)
    return rows, uncertain, clean


def _list_remote_dir(handler, path: str):
    """Worker: list a device folder and resolve which symlinks are folders.

    Returns ``(rows, error)``; *error* is '' when everything succeeded.  *rows*
    is a :class:`_Listing`; its ``uncertain`` names must not be acted on.
    """
    res = handler.shell(_ls_cmd(path), timeout=30, safe=False)
    rows, uncertain, clean = _parse_ls_listing(res.stdout)
    error = "" if res.ok else _result_error(res)
    if not clean:
        # Newlines, control characters or ' -> ' in names: ls -b escapes them.
        try:
            res_b = handler.shell(_ls_escaped_cmd(path), timeout=30, safe=False)
            rows_b, uncertain_b, clean_b = _parse_ls_listing(res_b.stdout, escaped=True)
        except Exception:
            rows_b, uncertain_b, clean_b = [], set(), False
        if rows_b and (clean_b or len(uncertain_b) < len(uncertain)):
            rows, uncertain = rows_b, uncertain_b
    rows = _Listing(rows, uncertain)
    links = [i for i, row in enumerate(rows) if row[5].startswith("l") and not row[7]]
    link_paths = [posixpath.join(path, rows[i][0]) for i in links]
    # _chunks caps the command by character count too: a folder of long names
    # otherwise built a shell line the device refused.
    taken = 0
    for group in _chunks(link_paths):
        chunk = links[taken:taken + len(group)]
        taken += len(group)
        try:
            check = handler.shell(_link_dirs_cmd(group), timeout=30, safe=False)
        except Exception as exc:  # links stay "File Link"; the reason is reported
            error = error or f"could not resolve symlink types: {type(exc).__name__}: {exc}"
            break
        flags = [ln.strip() for ln in strip_ansi(_as_text(check.stdout)).splitlines()
                 if ln.strip() in ("d", "f")]
        if len(flags) != len(chunk):
            continue
        for i, flag in zip(chunk, flags):
            if flag == "d":
                name, raw_size, _sz, _ft, mtime, perms, owner, _d = rows[i]
                rows[i] = (name, raw_size, "<DIR>", "Folder Link", mtime, perms, owner, True)
    return rows, error


def _probe_remote(handler, paths):
    """Worker: ``{path: (kind, is_link, resolved)}`` for device *paths* (see :func:`_probe_cmd`)."""
    info = {}
    for chunk in _chunks(list(dict.fromkeys(paths))):
        res = handler.shell(_probe_cmd(chunk), timeout=60, safe=False)
        lines = _output_lines(res.stdout)
        if len(lines) != len(chunk) or any(
                len(ln) < 3 or ln[0] not in "dfbn" or ln[1] not in "L-" or ln[2] != " "
                for ln in lines):
            detail = _result_error(res) if not res.ok else "unexpected reply"
            raise RuntimeError(f"could not check the device paths: {detail}")
        for p, ln in zip(chunk, lines):
            info[p] = (ln[0], ln[1] == "L", ln[3:])
    return info


_EDIT_STAT_RE = re.compile(r"^(\d+) ([0-7]{3,4}) (.+)\Z")


def _parse_edit_stat(stdout, path: str):
    """``(size, mode, type, real_path)`` from :func:`_edit_stat_cmd` output, or None."""
    lines = _output_lines(stdout)
    m = _EDIT_STAT_RE.match(lines[0]) if lines else None
    if not m:
        return None
    real = lines[1] if len(lines) == 2 and lines[1].startswith("/") else path
    return int(m.group(1)), m.group(2), m.group(3), real
