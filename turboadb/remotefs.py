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

from .results import human_bytes as _human_size, strip_ansi


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


def _copy_into_cmd(src: str, dst: str, merge: bool) -> str:
    """Device copy of *src* to exactly *dst*.  *merge* copies a folder's contents
    into an existing *dst* folder (``src/.``) instead of nesting ``dst/name``."""
    source = src.rstrip("/") + "/." if merge else src
    return f"cp -r -- {shlex.quote(source)} {shlex.quote(dst)}"


def _copy_decision(source: str, target: str, info, folder: str, taken=None):
    """Decide a device copy of *source* to exactly *target*, which is in *folder*.

    *info* holds :func:`_probe_remote` results for *source*, *target* and, when
    *source* is a real folder, *folder*.  Returns ``(problem, exists, merge)``:
    *problem* is ``""`` or why the copy must not run - ``missing`` (no source),
    ``same`` (the target is the source), ``inside`` (a folder into itself or its
    own subfolder: ``cp -r`` would recurse until it fails), ``taken``
    (another item of the batch has this target; *taken* holds the batch's
    targets so far) or ``different`` (another kind of item is at the target).
    *exists* is True when an item is at the target; a file there is replaced, a
    folder is merged into (*merge*: the copy runs as ``source/.``).

    ``cp -r`` copies a symbolic link as a link and never follows it: a linked
    source can't recurse into itself, so it is not checked for that, and it
    never merges into a folder of its name (``link/.`` would copy what the link
    points to instead) - a name already taken there is refused.

    The Files tab's Paste and :meth:`ADBHandler.copy` (``turboadb cp``) both
    decide with this, so the two never disagree about what may be copied."""
    kind, is_link, resolved = info[source]
    if kind == "n":
        return "missing", False, False
    n_source = posixpath.normpath(source)
    if n_source == posixpath.normpath(target):
        return "same", False, False
    if kind == "d" and not is_link:
        n_folder = posixpath.normpath(folder)
        real_source = posixpath.normpath(resolved or source)
        real_folder = posixpath.normpath((info[folder][2] if folder in info else "") or folder)
        if posixpath.join(real_folder, posixpath.basename(target)) == real_source:
            return "same", False, False
        if any(b == a or b.startswith(a.rstrip("/") + "/")
               for a, b in ((n_source, n_folder), (real_source, real_folder))):
            return "inside", False, False
    if taken is not None:
        if target in taken:
            return "taken", False, False
        taken.add(target)
    target_kind = info[target][0]
    if target_kind == "n":
        return "", False, False
    if is_link or target_kind == "b" or (target_kind == "d") != (kind == "d"):
        return "different", True, False
    return "", True, kind == "d"


def _rename_check_cmd(src: str, dst: str) -> str:
    """Prints ``free``, ``same`` (same file, e.g. a case-only rename), ``dir`` or ``file``."""
    s, d = shlex.quote(src), shlex.quote(dst)
    return (f"if [ -e {d} ] || [ -L {d} ]; then "
            f"if [ ! -L {s} ] && [ ! -L {d} ] && [ {s} -ef {d} ]; then echo same; "
            f"elif [ -d {d} ] && [ ! -L {d} ]; then echo dir; else echo file; fi; "
            f"else echo free; fi")


# How mv's option parser rejects ``-T`` on shells that predate it (toybox before
# Android 11 says "Unknown option 'T'", busybox "invalid option").
_NO_T_OPTION = '*"nknown option"*|*"nvalid option"*|*"llegal option"*'


def _rename_cmd(src: str, dst: str, overwrite: bool = False, verify: bool = True) -> str:
    """``mv -T`` never moves *src* INTO an existing folder.  Without *overwrite*,
    ``-n`` refuses to replace anything: toybox then still exits 0, while GNU's
    mv 9.2 and later fail, each in words of its own.  So a failed mv that
    leaves both names in place counts as that refusal, and the move is
    verified.

    Many head units run a toybox without ``-T``.  There the command falls back
    to a plain ``mv`` that first refuses a folder at *dst*, or a link to one (a
    plain mv would move *src* inside it, over a file of its name there), and
    does a case-only rename of the same file in two steps through a temporary
    name."""
    s, d = shlex.quote(src), shlex.quote(dst)
    flag = "-f " if overwrite else "-n " if verify else ""
    tmp = shlex.quote(dst.rstrip("/") + ".turboadb-rename")
    report = "{ printf '%s\\n' \"$err\" >&2; false; }"
    if flag == "-n ":  # the refusal: the check at the end says it in plain words
        report = (f"{{ {{ [ -e {s} ] || [ -L {s} ]; }} && "
                  f"{{ [ -e {d} ] || [ -L {d} ]; }}; }} || {report}")
    plain = (f"if [ ! -L {s} ] && [ ! -L {d} ] && [ {s} -ef {d} ]; then "
             f"[ ! -e {tmp} ] && mv -- {s} {tmp} && mv -- {tmp} {d}; "
             f"elif [ -d {d} ]; then "
             f"echo 'mv: not renamed: a folder with the new name exists' >&2; false; "
             f"else err=$(mv {flag}-- {s} {d} 2>&1) || {report}; fi")
    cmd = (f"err=$(mv {flag}-T -- {s} {d} 2>&1) || case \"$err\" in "
           f"{_NO_T_OPTION}) {plain} || exit 1;; "
           f"*) {report} || exit 1;; esac")
    if verify and not overwrite:
        cmd += (f"; if [ -e {s} ] || [ -L {s} ]; then "
                f"echo 'mv: not renamed: the new name already exists' >&2; exit 1; fi")
    return cmd


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


def _stamp_cmd(path: str) -> str:
    """``<size> <modified, in seconds since the epoch>`` of what *path* points to."""
    return f"stat -L -c '%s %Y' -- {shlex.quote(path)}"


# Printed once a copy was written over its target (see _write_over_cmd).
WRITTEN_MARK = "TURBOADB_WRITTEN"


def _write_over_cmd(src: str, dst: str) -> str:
    """Write the device file *src* over *dst* in place: *dst* keeps its owner,
    mode and SELinux label (``cat`` into it, where adb push would replace it),
    and :data:`WRITTEN_MARK` is printed when it worked (old devices give no
    exit status)."""
    return f"cat {shlex.quote(src)} > {shlex.quote(dst)} && echo {WRITTEN_MARK}"


def _rm_file_cmd(path: str) -> str:
    return f"rm -f -- {shlex.quote(path)}"


def _regular_file_cmd(path: str) -> str:
    """Prints ``f`` when *path* is, or links to, a regular file (the shell's own
    test: it works where there is no ``stat``)."""
    q = shlex.quote(path)
    return f"if [ -f {q} ]; then echo f; else echo x; fi"


def _chmod_cmd(mode: str, path: str) -> str:
    return f"chmod {shlex.quote(mode)} -- {shlex.quote(path)}"


def _rm_cmd(paths) -> str:
    return "rm -rf " + " ".join(shlex.quote(p) for p in paths)


def _sizes_cmd(path: str) -> str:
    """The size of every file *path* holds, one number per line: the file's
    own, or each file under a folder (a link to a folder is followed, as adb
    follows it).  Prints nothing for a missing path.  The PC adds them up:
    mksh's arithmetic is 32-bit, and a few GB would overflow it."""
    q = shlex.quote(path)
    inside = shlex.quote(path.rstrip("/") + "/")
    return (f"if [ -d {q} ]; then find {inside} -type f -exec stat -c %s {{}} + 2>/dev/null; "
            f"elif [ -e {q} ]; then stat -L -c %s -- {q} 2>/dev/null; fi")


def _push_target_size_cmd(remote: str, name: str) -> str:
    """The size of the file ``adb push NAME REMOTE`` is writing: ``REMOTE/NAME``
    when REMOTE is a folder, else REMOTE itself."""
    return (f"d={shlex.quote(remote)}; [ -d \"$d\" ] && d=\"$d/\"{shlex.quote(name)}; "
            f"stat -L -c %s -- \"$d\" 2>/dev/null")


def _sum_sizes(stdout) -> Optional[int]:
    """The total of the sizes a size command printed, or None when it printed none."""
    numbers = [int(ln) for ln in _output_lines(stdout) if ln.strip().isdigit()]
    return sum(numbers) if numbers else None


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
        # A name that starts or ends with a line break ("out\r", made by a
        # script with Windows line endings) is not what a path to it says:
        # _normalize_remote_path trims those, so "out\r" would act on "out".
        if not exact or name in seen or name != name.strip("\r\n"):
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


def _parse_stamp(stdout):
    """``(size, modified)`` from :func:`_stamp_cmd` output, or None (no ``stat``)."""
    lines = _output_lines(stdout)
    m = re.fullmatch(r"(\d+) (\d+)", lines[0].strip()) if lines else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def _parse_edit_stat(stdout, path: str):
    """``(size, mode, type, real_path)`` from :func:`_edit_stat_cmd` output, or None."""
    lines = _output_lines(stdout)
    m = _EDIT_STAT_RE.match(lines[0]) if lines else None
    if not m:
        return None
    real = lines[1] if len(lines) == 2 and lines[1].startswith("/") else path
    return int(m.group(1)), m.group(2), m.group(3), real
