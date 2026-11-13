# Embedding TurboADB in your own program

The whole TurboADB app (device tabs, terminals, logcat, files, the device screen,
dialogs) can live inside a window of your own program and be driven from it. It
works with any program that has a window: PyQt5, PySide6, PyQt6, Qt C++, Python Tk,
wxPython, C# WinForms and WPF, Win32 and C++, Delphi and Electron.

TurboADB runs as a **process of its own** inside the area of your window you give
it. Its Qt never meets your program's toolkit, and if one of the two programs
crashed, the other would keep running. It fills that area, follows its size and
its DPI setting, keeps its dialogs in front of your window, and closes when the
area is destroyed. Your program drives it through a small **control API**: JSON over
HTTP on `127.0.0.1`, protected by a token.

- [How it works](#how-it-works)
- [Pick your framework](#pick-your-framework)
- [Python: PyQt5, PySide6, PyQt6](#python-pyqt5-pyside6-pyqt6)
- [Python: Tk and wxPython](#python-tk-and-wxpython)
- [C#: WinForms and WPF](#c-winforms-and-wpf)
- [Qt C++](#qt-c)
- [Win32, C++ and Delphi](#win32-c-and-delphi)
- [Electron](#electron)
- [The control API](#the-control-api)
- [Rules for a smooth embed](#rules-for-a-smooth-embed)
- [What has been tested](#what-has-been-tested)

## How it works

1. **Give TurboADB an area of your window that has its own window handle** (HWND).
   Every toolkit has a container for this: a panel, a frame, a native widget.
2. **Start TurboADB with that handle**, with a random token in the environment
   variable `TURBOADB_API_TOKEN` (so the token never appears on a command line):

   ```text
   TurboADB-6.3.0-win64.exe --embed-into <HWND> --api-port 0 --handshake-file <file>
   ```

   With TurboADB installed in a Python (`pip install "turboadb[gui]"`), start
   `pythonw -m turboadb.gui.app` with the same options instead.

   | Option | Meaning |
   |---|---|
   | `--embed-into HWND` | The area to live in: its handle, in decimal or `0x` hex. |
   | `--api-port PORT` | Start the control API on this port. `0` picks a free one. |
   | `--handshake-file FILE` | Where TurboADB writes how to reach the API, once its window is up. |
   | `--hide PARTS` | Leave out parts of TurboADB's window, comma separated: `menubar`, `ribbon`, `sidebar`, `statusbar`, `log`. |

3. **Wait for the handshake file.** TurboADB writes it in one go when it is ready,
   usually within a few seconds:

   ```json
   {"api": "http://127.0.0.1:52817/v1", "port": 52817, "token": "…", "pid": 18320,
    "version": "6.3.0", "api_version": 1, "window": 3540230, "embedded": true}
   ```

   If it cannot start, the file says why instead: `{"error": "there is no window 5 to go into", "pid": 18320}`.

4. **Call the API** with the header `Authorization: Bearer <token>`.

TurboADB follows the size of its area by itself, so your program needs no resize
code. It closes when the area's window is destroyed, or on `POST /v1/quit`.

Python programs do steps 2 to 4 with one call, `turboadb.embed.launch(parent=hwnd)`,
or with the ready-made Qt widget.

## Pick your framework

| Your program | The area TurboADB goes into | Its handle |
|---|---|---|
| PyQt5, PySide6, PyQt6 | `TurboADBWidget` | (the widget does it) |
| Python Tk | `tk.Frame` | `frame.winfo_id()` |
| wxPython | `wx.Panel` | `panel.GetHandle()` |
| C# WinForms | `Panel` | `panel.Handle` |
| C# WPF | a WinForms `Panel` in a `WindowsFormsHost` | `panel.Handle` |
| Qt C++ | a `QWidget` with `Qt::WA_NativeWindow` | `widget->winId()` |
| Win32, C++, Delphi | a child window (`CreateWindowEx`, a `TPanel`) | the HWND, `Panel.Handle` |
| Electron | a frameless child `BrowserWindow` over the area | `getNativeWindowHandle()` |

## Python: PyQt5, PySide6, PyQt6

`turboadb.embed_qt.TurboADBWidget` is a widget you put in any layout. It uses the Qt
binding your program has already imported (PyQt5, PySide6, PyQt6 or PySide2), and
starts TurboADB the first time it is shown.

```python
from turboadb.embed_qt import TurboADBWidget

view = TurboADBWidget(hide=("menubar",))
view.started.connect(lambda: view.client.open("R58M12345", page="files"))
view.event_received.connect(lambda event: print(event["type"], event))
layout.addWidget(view)
```

`view.client` is a `turboadb.embed.Embedded` client: `devices()`, `tabs()`,
`open(serial, page=...)`, `show(...)`, `screen(serial)`, `shell(serial, command)`,
`call(serial, method, *args)`, `set_ui(...)`, `dialog(name)` and `quit()`. The widget's
signals are `started`, `failed(str)`, `event_received(dict)` and `finished`.

TurboADB itself runs on PyQt5. In a PySide6 or PyQt6 program whose Python has no
PyQt5, point the widget at the standalone app:
`TurboADBWidget(exe=r"C:\Tools\TurboADB-6.3.0-win64.exe")`.

[`examples/embed_in_qt.py`](examples/embed_in_qt.py) is a complete program with its
own toolbar above an embedded TurboADB: `python examples/embed_in_qt.py`, or add
`PySide6` to run it with PySide6.

## Python: Tk and wxPython

`turboadb.embed` needs only the standard library, so it works with any toolkit:

```python
import tkinter as tk
from turboadb.embed import launch

root = tk.Tk()
root.geometry("1300x850")
area = tk.Frame(root)
area.pack(fill="both", expand=True)
root.update_idletasks()                    # the frame needs its window before it has a handle
tadb = launch(parent=area.winfo_id(), hide=("menubar",))
tk.Button(root, text="Open phone", command=lambda: tadb.open("R58M12345", page="files")).pack()
root.protocol("WM_DELETE_WINDOW", lambda: (tadb.quit(), root.destroy()))
root.mainloop()
```

```python
import wx
from turboadb.embed import launch

app = wx.App(False)
frame = wx.Frame(None, title="My program", size=(1300, 850))
area = wx.Panel(frame)
frame.Show()
tadb = launch(parent=area.GetHandle(), hide=("menubar",))
app.MainLoop()
```

`launch()` waits until TurboADB is up (a few seconds). To keep your window
responsive meanwhile, call it on a thread. `launch(exe=...)` starts the standalone
`TurboADB-x.y.z-win64.exe` instead of the TurboADB of this Python.

## C#: WinForms and WPF

**WinForms.** A `Panel` is the area. Start TurboADB once the form is shown, and read
the handshake without blocking the form:

```csharp
using System.Diagnostics;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text.Json;

var area = new Panel { Dock = DockStyle.Fill };
Controls.Add(area);
var http = new HttpClient();
string api = "";

Shown += async (_, _) =>
{
    var token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
    var handshake = Path.Combine(Path.GetTempPath(), $"tadb-{Guid.NewGuid()}.json");
    var start = new ProcessStartInfo(@"C:\Tools\TurboADB-6.3.0-win64.exe",
        $"--embed-into {area.Handle} --api-port 0 --handshake-file \"{handshake}\" --hide menubar")
        { UseShellExecute = false };
    start.Environment["TURBOADB_API_TOKEN"] = token;
    Process.Start(start);
    while (!File.Exists(handshake)) await Task.Delay(100);
    api = JsonDocument.Parse(File.ReadAllText(handshake)).RootElement.GetProperty("api").GetString()!;
    File.Delete(handshake);
    http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
};

// from a button: always await, so the form keeps running while TurboADB answers
// await http.PostAsJsonAsync(api + "/open", new { serial = "R58M12345", page = "files" });
```

**WPF.** WPF controls have no window handle of their own, so host a WinForms panel
in a `WindowsFormsHost` (set `<UseWindowsForms>true</UseWindowsForms>` in the
`.csproj`), and do the same as in WinForms with that panel's handle:

```xml
<WindowsFormsHost><wf:Panel x:Name="TurboArea"/></WindowsFormsHost>
```

Start TurboADB in `ContentRendered`, when the panel has its handle. WPF elements
cannot be drawn on top of the TurboADB area (popups and dialogs can).

## Qt C++

```cpp
auto *area = new QWidget(this);
area->setAttribute(Qt::WA_NativeWindow);
area->setAttribute(Qt::WA_DontCreateNativeAncestors);
layout->addWidget(area);

auto env = QProcessEnvironment::systemEnvironment();
env.insert("TURBOADB_API_TOKEN", token);
auto *process = new QProcess(this);
process->setProcessEnvironment(env);
process->start(exePath, {"--embed-into", QString::number(area->winId()),
                         "--api-port", "0", "--handshake-file", handshakePath});
// poll for the handshake with a QTimer, read it with QFile + QJsonDocument, and call
// the API with QNetworkAccessManager (asynchronous) and "Authorization: Bearer <token>"
```

## Win32, C++ and Delphi

Create a child window as the area, and start TurboADB with its handle:

```cpp
HWND area = CreateWindowExW(0, L"STATIC", nullptr, WS_CHILD | WS_VISIBLE,
                            0, 40, width, height - 40, hMainWnd, nullptr, hInst, nullptr);
SetEnvironmentVariableW(L"TURBOADB_API_TOKEN", token.c_str());
std::wstring cmd = L"\"C:\\Tools\\TurboADB-6.3.0-win64.exe\" --embed-into "
    + std::to_wstring((UINT_PTR)area) + L" --api-port 0 --handshake-file \"" + handshake + L"\"";
CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, FALSE, 0, nullptr, nullptr, &si, &pi);
SetEnvironmentVariableW(L"TURBOADB_API_TOKEN", nullptr);
// main window: WS_CLIPCHILDREN, and on WM_SIZE MoveWindow(area, ...): TurboADB follows
// API: WinHTTP or libcurl, from a worker thread
```

In **Delphi**, the area is a `TPanel`:

```pascal
uses Winapi.Windows, System.SysUtils, System.IOUtils, System.JSON;

procedure TMainForm.StartTurboADB;
var
  CommandLine: string;
  Startup: TStartupInfo;
  Process: TProcessInformation;
begin
  FToken := TGUID.NewGuid.ToString + TGUID.NewGuid.ToString;
  FHandshake := TPath.Combine(TPath.GetTempPath, TGUID.NewGuid.ToString + '.json');
  SetEnvironmentVariable('TURBOADB_API_TOKEN', PChar(FToken));   // inherited, never on the command line
  CommandLine := Format('"%s" --embed-into %d --api-port 0 --handshake-file "%s" --hide menubar',
    [TurboADBExe, PanelTurboADB.Handle, FHandshake]);
  FillChar(Startup, SizeOf(Startup), 0);
  Startup.cb := SizeOf(Startup);
  if not CreateProcess(nil, PChar(CommandLine), nil, nil, False, 0, nil, nil, Startup, Process) then
    RaiseLastOSError;
  SetEnvironmentVariable('TURBOADB_API_TOKEN', nil);
  CloseHandle(Process.hThread);
  CloseHandle(Process.hProcess);
  HandshakeTimer.Enabled := True;   // a TTimer: no waiting loop on the form's thread
end;

procedure TMainForm.HandshakeTimerTimer(Sender: TObject);
var
  Info: TJSONValue;
begin
  if not TFile.Exists(FHandshake) then Exit;
  HandshakeTimer.Enabled := False;
  Info := TJSONObject.ParseJSONValue(TFile.ReadAllText(FHandshake, TEncoding.UTF8));
  try
    FApi := Info.GetValue<string>('api');
  finally
    Info.Free;
  end;
  TFile.Delete(FHandshake);
end;

// Requests: THTTPClient with CustomHeaders['Authorization'] := 'Bearer ' + FToken, run in
// TTask.Run (or with THTTPClient's asynchronous methods), never on the form's thread.
```

## Electron

A web page is one window, so HTML cannot hold TurboADB. Give it a frameless child
window placed over the area, and move it with the main window. Keep the token and
the API calls in the main process, and reach them from the page over IPC.

```js
const { app, BrowserWindow } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');

const area = new BrowserWindow({ parent: mainWindow, frame: false, show: false, skipTaskbar: true });
const place = () => {
  const b = mainWindow.getContentBounds();
  area.setBounds({ x: b.x, y: b.y + 40, width: b.width, height: b.height - 40 });
};
place(); area.show();
mainWindow.on('move', place);
mainWindow.on('resize', place);

const hwnd = area.getNativeWindowHandle().readBigUInt64LE().toString();
const token = crypto.randomBytes(32).toString('hex');
spawn(exePath, ['--embed-into', hwnd, '--api-port', '0', '--handshake-file', handshakePath],
      { env: { ...process.env, TURBOADB_API_TOKEN: token }, stdio: 'ignore' });
// read the handshake, then: fetch(api + '/devices', { headers: { Authorization: `Bearer ${token}` } })
```

## The control API

Every request needs `Authorization: Bearer <token>`. Bodies and answers are JSON. An
answer is `{"ok": true, "result": …}`, or `{"ok": false, "error": "…"}` with an HTTP
status of 400 (bad request), 401 (no or wrong token), 403 (a web page), 404 (no such
device, tab or request), 405, 409 (not possible now) or 504 (TurboADB's window was
busy, for example with a dialog open).

| Request | Body | Result |
|---|---|---|
| `GET /v1/info` | | Version, process, window handle, pages, parts, dialogs and the device commands offered. |
| `GET /v1/devices` | | Every device adb reports: `serial`, `state`, `online`, `network`, `model`, `open`. |
| `GET /v1/tabs` | | The open tabs: `tab` (a number), `index`, `title`, `kind`, `serial`, `page`, `connected`, `current`. |
| `POST /v1/open` | `{"serial"}` or `{"host", "port"}`, optional `"page"` | Opens the device's tab, or brings the open one up, on that page. A page asked for before the device has connected is shown once it has. |
| `POST /v1/show` | `{"serial"}` or `{"tab"}`, optional `"page"` | Brings a device tab up. |
| `POST /v1/close` | `{"serial"}` or `{"tab"}` | Closes a tab. |
| `POST /v1/screen` | `{"serial", "action": "start" or "stop", "display_id"}` | Starts or stops the device's screen in Device Control. |
| `POST /v1/device` | `{"serial", "method", "args": [], "kwargs": {}}` | Runs one device command and returns its result: `{"success", "value", "error", …}`. |
| `GET /v1/ui` | | Which parts show, the Webcam page setting and the theme. |
| `POST /v1/ui` | any of `menubar`, `ribbon`, `sidebar`, `statusbar`, `log`, `webcam_page` (true or false) and `theme` | Shows or hides those parts, turns the Webcam page on or off, or changes the theme. |
| `POST /v1/dialog` | `{"name": "settings", "new_target", "connect" or "about"}` | Opens one of TurboADB's dialogs (for a program that hides TurboADB's menu bar). |
| `GET /v1/events?after=N&wait=S` | | What happened since event `N`, waiting up to `S` seconds (60 at most) for something. |
| `POST /v1/focus` | | Gives TurboADB the keyboard (when your window is the active one). |
| `POST /v1/quit` | | Closes TurboADB as File > Exit does. |

**Pages:** `terminal`, `logcat`, `files`, `controls` (also `screen`), `apps`, `phone`,
`webcam` (when the Webcam page is on) and `displays` (cars and multi-display devices).

**Device commands** are TurboADB's own engine methods. Among them are `shell`,
`getprop`, `install`, `uninstall`, `push`, `pull`, `screenshot`, `keyevent`,
`input_text`, `tap`, `swipe`, `reboot`, `list_packages`, `start_app`, `stop_app`,
`list_displays` and `battery`. `GET /v1/info` lists them all. A command that streams,
such as logcat or an interactive shell, belongs on its page in the window and is not
offered. Example:

```json
POST /v1/device
{"serial": "R58M12345", "method": "shell", "args": ["getprop ro.build.version.release"]}

{"ok": true, "result": {"success": true, "action": "shell",
  "value": {"command": "…", "exit_code": 0, "stdout": "14\n", "stderr": "", …}, "error": null}}
```

**Events** each have an `id`, a `time` and a `type`:

| Type | Fields |
|---|---|
| `ready` | `version` |
| `device` | `serial`, `state` (`device`, `offline`, `unauthorized`, … or `gone`) |
| `tab_opened`, `tab_selected` | the tab, as in `/v1/tabs` |
| `tab_closed` | `tab` |
| `page` | `tab`, `serial`, `page` |
| `screen` | `tab`, `serial`, `active` |
| `log` | `text`: a line of TurboADB's log |
| `closing` | TurboADB is closing |

## Rules for a smooth embed

- **Don't make your window's thread wait for TurboADB.** On Windows, a window inside
  another program's window shares its keyboard and focus with it, so either
  program can wait for the other for a moment. Call the API with `await` (C#,
  JavaScript), from a worker thread or timer (Delphi, C++, Tk), or through
  `turboadb.embed` and `TurboADBWidget`, which handle this for you. Requests that
  only read (`devices`, `tabs`, `ui`, `info`) never wait for more than half a second:
  when TurboADB's window is busy, they answer with what it showed at its last look.
- **Keyboard.** A click in TurboADB gives it the keyboard, as in any window.
  `/v1/focus` does the same from code while your window is the active one.
- **Dialogs and menus** of TurboADB open in front of your window, and belong to it.
- **Closing.** A hidden area keeps TurboADB running, so it can sit on a tab page that
  is not showing. Destroying the area closes TurboADB within about a second.
  `/v1/quit` closes it at any time, and `TurboADBWidget` closes it when your program
  quits.
- **Size and DPI.** TurboADB fills the area and follows it, and takes the DPI
  awareness of your window, so it is sized right whether or not your program is
  per-monitor aware.
- **Several at once.** Each area can hold its own TurboADB, and the normal TurboADB
  window can run beside them. They share the adb server and `~/.turboadb`.
- **Updates.** Inside your program, TurboADB does not restart itself after an update.
  Start it again to use the new version.
- **Security.** The API listens on `127.0.0.1` only and needs the token. Requests from
  web pages are refused. The token never appears on a command line, and TurboADB
  removes it from its environment, so nothing it starts inherits it. Device commands
  come from a fixed list. `push` and `pull` read and write files with the rights of
  the account TurboADB runs as.
- **Windows.** Embedding uses Windows window handles. The control API works on every
  system, with `--api-port` and no `--embed-into`.
- **Programs that are not DPI aware.** On a display scaled above 100%, Windows
  enlarges such a program as a picture, and TurboADB inside it with it. Windows does
  not let a sharp, DPI-aware window live inside one that is not, so TurboADB takes
  your program's mode. Make your program DPI aware for sharp text in both.

## What has been tested

TurboADB 6.1.0, the standalone `TurboADB-6.1.0-win64.exe`, on Windows 11 with the
display scaled to 125%, inside a host program of each kind below. Each was checked
for these six things:

- TurboADB starts inside the area it was given.
- It fills that area, and follows it when the host's window is resized.
- Its API answers, and reports that it is embedded in that area.
- The host can change it through the API (its sidebar hidden and shown again).
- It closes about a second after the host's window is closed.
- The host itself ends cleanly.

| Host program | Version | Result |
|---|---|---|
| PyQt5 | 5.15.11 | All passed. The device screen (scrcpy) showed inside TurboADB and stayed inside when the host was resized, and a TurboADB dialog opened in front of the host. |
| PySide6 | 6.12.0 | All passed (no PyQt5 in that Python: the widget started the standalone app) |
| PyQt6 | 6.12.0 | All passed |
| Python Tk | Tk 8.6 | All passed |
| wxPython | 4.3.2 | All passed |
| C# WinForms | the .NET Framework 4.8 classes, run from PowerShell 5.1 | All passed |
| C# WPF | `WindowsFormsHost`, .NET Framework 4.8, run from PowerShell 5.1 | All passed |
| Win32 | the Windows API called directly, as C++ and Delphi call it | All passed |
| Electron | 45.2.3 | All passed |

Qt C++, Delphi and compiled C# were not built here. They give TurboADB a window
handle and call the same API as the hosts above, so they work the same way.
