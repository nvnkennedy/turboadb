"""The remote webcam only runs an ffmpeg no less privileged user could plant.

ffmpeg runs on the remote with the WinRM admin's rights.  It used to be taken
from, and provisioned into, C:\\Windows\\Temp\\turboadb-ffmpeg (and C:\\ffmpeg\\bin
was searched): folders any local user of that PC can create first and fill
with an ffmpeg.exe of their own.  No test here opens a WinRM session or a
share: the remote's answers are scripted.
"""
import pytest

from turboadb.gui import ffmpeg_tools
from turboadb.gui import remote_webcam as rw

_OURS = r"C:\Program Files\TurboADB\ffmpeg"


def test_ffmpeg_is_only_found_or_put_where_only_admins_write():
    for script in (rw._LOCATE, rw._PREPARE, rw._REMOTE_DOWNLOAD):
        lowered = script.lower()
        for open_to_all in (r"windows\temp", r"c:\ffmpeg", "$env:temp", "programdata"):
            assert open_to_all not in lowered
    assert "$env:ProgramFiles" in rw._LOCATE and "$env:ProgramFiles" in rw._REMOTE_DOWNLOAD


@pytest.fixture
def remote(monkeypatch, tmp_path):
    """A remote that has no ffmpeg yet; each WinRM script gets its scripted answer."""
    local = tmp_path / "ffmpeg.exe"
    local.write_bytes(b"MZ-the-local-ffmpeg")
    world = {"scripts": [], "hash": "OK", "download": "FFMPEG:" + _OURS + r"\ffmpeg.exe",
             "pushed": [], "push_error": None}

    def run_ps(host, login, password, script, winrm_port=5985):
        world["scripts"].append(script)
        if "Get-Command ffmpeg" in script:
            return 0, "NOFFMPEG:\n", ""
        if "'DIR:'" in script:
            return 0, f"DIR:{_OURS}\n", ""
        if "'HASH:OK'" in script:
            return 0, f"HASH:{world['hash']}\n", ""
        if "DownloadFile" in script:
            return 0, world["download"] + "\n", ""
        raise AssertionError("unexpected script")

    def smb_push(host, login, password, local_ffmpeg, remote_dir, log=None):
        if world["push_error"]:
            raise RuntimeError(world["push_error"])
        world["pushed"].append((host, local_ffmpeg, remote_dir))
        return remote_dir + r"\ffmpeg.exe"

    monkeypatch.setattr(rw, "_ensure_winrm", lambda say=None: True)
    monkeypatch.setattr(rw, "_run_ps", run_ps)
    monkeypatch.setattr(rw, "_smb_push", smb_push)
    monkeypatch.setattr(ffmpeg_tools, "cached_ffmpeg", lambda: str(local))
    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg", lambda log=None, **kw: str(local))
    monkeypatch.delenv("TURBOADB_FFMPEG_SHA256", raising=False)
    world["local"] = str(local)
    world["digest"] = ffmpeg_tools._sha256_file(str(local))
    return world


def test_the_local_copy_is_pushed_into_the_admin_only_folder_and_checked(remote):
    assert rw.ensure_remote_ffmpeg("lab-pc", "D\\u", "pw") == _OURS + r"\ffmpeg.exe"
    assert remote["pushed"] == [("lab-pc", remote["local"], _OURS)]
    verify = next(s for s in remote["scripts"] if "'HASH:OK'" in s)
    assert remote["digest"] in verify and (_OURS + r"\ffmpeg.exe") in verify


def test_a_copy_that_arrived_different_is_not_used(remote):
    remote["hash"] = "0badc0de"
    logged = []
    assert rw.ensure_remote_ffmpeg("lab-pc", "D\\u", "pw", log=logged.append) == remote["download"][7:]
    assert any("not the local ffmpeg" in line for line in logged)


def test_the_remote_download_follows_a_pinned_hash(remote, monkeypatch):
    remote["push_error"] = "the admin share is off"
    monkeypatch.setenv("TURBOADB_FFMPEG_SHA256", "AB" * 32)
    monkeypatch.setenv("TURBOADB_FFMPEG_URL", "https://example/it's-a-build.zip")
    rw.ensure_remote_ffmpeg("lab-pc", "D\\u", "pw")
    download = next(s for s in remote["scripts"] if "DownloadFile" in s)
    assert "$pin = '" + "ab" * 32 + "'" in download
    assert "https://example/it''s-a-build.zip" in download  # quoted for PowerShell


def test_the_advice_names_the_admin_only_folder(remote):
    remote["push_error"] = "the admin share is off"
    remote["download"] = "NOFFMPEG:no internet there"
    with pytest.raises(RuntimeError) as err:
        rw.ensure_remote_ffmpeg("lab-pc", "D\\u", "pw")
    text = str(err.value)
    assert _OURS in text and "administrator" in text and "Temp" not in text


def test_the_admin_share_path_follows_the_remote_folder():
    assert rw._admin_share_path("pc1", _OURS) == r"\\pc1\C$\Program Files\TurboADB\ffmpeg"
    assert rw._admin_share_path("pc1", r"d:\Apps\TurboADB\ffmpeg") == r"\\pc1\D$\Apps\TurboADB\ffmpeg"
    with pytest.raises(RuntimeError):
        rw._admin_share_path("pc1", r"\\server\share\ffmpeg")
