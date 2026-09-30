# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import re

from PyInstaller.utils.hooks import collect_submodules, collect_all
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)

# PyInstaller executes a spec with ``SPECPATH`` rather than ``__file__``.
ROOT = Path(SPECPATH)
_version_text = (ROOT / 'turboadb' / '__init__.py').read_text(encoding='utf-8')
_version_match = re.search(r'__version__\s*=\s*"([^"]+)"', _version_text)
if _version_match is None:
    raise RuntimeError('Could not read the TurboADB version for the executable name.')
APP_VERSION = _version_match.group(1)
EXE_NAME = f'TurboADB-{APP_VERSION}-win64'

# The exe's version information: Task Manager names the process after its
# FileDescription, and showed the file name without one.
_numbers = tuple((
    [int(re.match(r'\d*', part).group() or 0) for part in APP_VERSION.split('.')[:4]] + [0, 0, 0, 0])[:4])
VERSION_INFO = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_numbers, prodvers=_numbers, mask=0x3F, flags=0x0, OS=0x40004,
                      fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', 'TurboADB'),
            StringStruct('FileDescription', 'TurboADB'),
            StringStruct('FileVersion', APP_VERSION),
            StringStruct('InternalName', 'TurboADB'),
            StringStruct('LegalCopyright', 'Copyright (c) 2026 TurboADB contributors. MIT License.'),
            StringStruct('OriginalFilename', f'{EXE_NAME}.exe'),
            StringStruct('ProductName', 'TurboADB'),
            StringStruct('ProductVersion', APP_VERSION),
        ])]),
        VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])]),
    ],
)

hiddenimports = ['winrm', 'requests_ntlm', 'spnego']   # WinRM remote-deploy (NTLM)
hiddenimports += ['keyring.backends', 'keyring.backends.Windows']  # OS vault (password)
hiddenimports += collect_submodules('keyring')
hiddenimports += collect_submodules('turboadb')
hiddenimports += ['PyQt5.QtSvg']   # gui/icons.py renders its vector icons lazily
datas = [('turboadb/assets/icon.ico', 'turboadb/assets'),
         ('turboadb/assets/icon.png', 'turboadb/assets'),
         ('turboadb/assets/icon-light.ico', 'turboadb/assets'),   # the light taskbar's
         ('turboadb/assets/icon-light.png', 'turboadb/assets')]
binaries = []
# Bundle the ENTIRE pywinrm/NTLM stack (submodules + binaries + data). NTLM is a
# lazy import inside pywinrm, so collect_all is needed or the frozen exe fails at
# run time with 'No module named requests_ntlm/spnego/...'.
for _pkg in ('winrm', 'requests', 'requests_ntlm', 'spnego', 'xmltodict',
             'cryptography', 'ntlm_auth'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d; binaries += _b; hiddenimports += _h
    except Exception:
        pass

a = Analysis(
    ['scripts\\gui_entry.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['turboadb\\assets\\icon.ico'],
    version=VERSION_INFO,
)
