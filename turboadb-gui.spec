# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import re

from PyInstaller.utils.hooks import collect_submodules, collect_all

# PyInstaller executes a spec with ``SPECPATH`` rather than ``__file__``.
ROOT = Path(SPECPATH)
_version_text = (ROOT / 'turboadb' / '__init__.py').read_text(encoding='utf-8')
_version_match = re.search(r'__version__\s*=\s*"([^"]+)"', _version_text)
if _version_match is None:
    raise RuntimeError('Could not read the TurboADB version for the executable name.')
APP_VERSION = _version_match.group(1)
EXE_NAME = f'TurboADB-{APP_VERSION}-win64'

hiddenimports = ['winrm', 'requests_ntlm', 'spnego']   # WinRM remote-deploy (NTLM)
hiddenimports += ['keyring.backends', 'keyring.backends.Windows']  # OS vault (password)
hiddenimports += collect_submodules('keyring')
hiddenimports += collect_submodules('turboadb')
hiddenimports += ['PyQt5.QtSvg']   # gui/icons.py renders its vector icons lazily
datas = [('turboadb/assets/icon.ico', 'turboadb/assets'),
         ('turboadb/assets/icon.png', 'turboadb/assets')]
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
)
