# Licences of what TurboADB ships

TurboADB's own source code is under the [MIT License](https://github.com/NVNKENNEDY/turboadb/blob/main/LICENSE).

The Windows app (`TurboADB-*-win64.exe`), and the wheel and source package that
carry it, also contain the software listed below. Because the app includes PyQt5,
which is licensed under the GNU General Public License version 3, **the app as a
whole is distributed under the GPL v3** (`GPL-3.0.txt`, beside this file). The MIT
License continues to apply to TurboADB's own code taken on its own.

## Getting the source

- **TurboADB:** <https://github.com/NVNKENNEDY/turboadb>. Every release is tagged
  there, and `scripts/build_exe.py` builds the app from it.
- **PyQt5:** source packages at <https://pypi.org/project/PyQt5/#files>.
- **Qt 5.15:** <https://download.qt.io/archive/qt/5.15/>. To use a different build
  of the Qt libraries (LGPL v3, `LGPL-3.0.txt`), run TurboADB from its source with
  your own PyQt5 and Qt, or rebuild the app with `scripts/build_exe.py`.

## What the app contains

| Component | Licence |
| --- | --- |
| [PyQt5](https://www.riverbankcomputing.com/software/pyqt/) | GPL v3 |
| [Qt 5.15](https://www.qt.io/) | LGPL v3 |
| [PyQt5-sip](https://pypi.org/project/PyQt5-sip/) | BSD-2-Clause |
| [Python](https://www.python.org/) | PSF-2.0 |
| [PyInstaller](https://pyinstaller.org/) bootloader | GPL v2 or later, with an exception for the programs it packages |
| [pywinrm](https://pypi.org/project/pywinrm/) | MIT |
| [requests](https://pypi.org/project/requests/) | Apache-2.0 |
| [requests_ntlm](https://pypi.org/project/requests-ntlm/) | ISC |
| [pyspnego](https://pypi.org/project/pyspnego/) | MIT |
| [sspilib](https://pypi.org/project/sspilib/) | MIT |
| [xmltodict](https://pypi.org/project/xmltodict/) | MIT |
| [keyring](https://pypi.org/project/keyring/) | MIT |
| [pywin32-ctypes](https://pypi.org/project/pywin32-ctypes/) | BSD-3-Clause |
| [cryptography](https://pypi.org/project/cryptography/) | Apache-2.0 or BSD-3-Clause |
| [cffi](https://pypi.org/project/cffi/) | MIT-0 |
| [pycparser](https://pypi.org/project/pycparser/) | BSD-3-Clause |
| [certifi](https://pypi.org/project/certifi/) | MPL-2.0 |
| [charset-normalizer](https://pypi.org/project/charset-normalizer/) | MIT |
| [idna](https://pypi.org/project/idna/) | BSD-3-Clause |
| [urllib3](https://pypi.org/project/urllib3/) | MIT |
| [importlib_metadata](https://pypi.org/project/importlib-metadata/) | Apache-2.0 |
| [zipp](https://pypi.org/project/zipp/), [jaraco.classes, jaraco.context, jaraco.functools](https://github.com/jaraco), [more-itertools](https://pypi.org/project/more-itertools/), [backports.tarfile](https://pypi.org/project/backports.tarfile/) | MIT |

A build can also pick up packages from the Python it runs in, all under permissive
licences: Pillow (HPND), bcrypt and packaging (Apache-2.0), Jinja2, MarkupSafe,
colorama and Pygments (BSD), pytest, pluggy, iniconfig, py and setuptools (MIT), and
typing_extensions (PSF-2.0).

## Not shipped

TurboADB does not include adb or scrcpy. They are installed on the first run, through
winget or from their publishers, under their own terms:
[Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)
(Google) and [scrcpy](https://github.com/Genymobile/scrcpy) (Apache-2.0).
