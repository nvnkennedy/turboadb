# Embedding TurboADB in your own program

TurboADB can live inside a window of your own program and be driven by it, as if
it were part of that program. This works from any language that has a window
handle and can make an HTTP request: PyQt5, PySide6, PyQt6, Delphi, C# (WinForms or
WPF), C++ and others.

TurboADB runs as a **process of its own** inside the window you give it, so its Qt
never meets your program's. It fills that window, follows its size, and closes when
the window is destroyed or when you ask it to. Your program then talks to it through
a small **control API**: JSON over HTTP on `127.0.0.1`, protected by a token.

- [How it works](#how-it-works)
- [Python: PyQt5, PySide6 and others](#python-pyqt5-pyside6-and-others)
- [Delphi](#delphi)
- [C#](#c)
- [The control API](#the-control-api)
- [Things to know](#things-to-know)

## How it works

1. **Start TurboADB with your window's handle.** Give it a random token in the
   environment variable `TURBOADB_API_TOKEN`, so the token never appears on a command
   line:

   ```text
   TurboADB-6.1.0-win64.exe --embed-into <HWND> --api-port 0 --handshake-file <file>
   ```

   With TurboADB installed in a Python (`pip install "turboadb[gui]"`), start
   `pythonw -m turboadb.gui.app` with the same options, or `turboadb gui`.

   | Option | Meaning |
   |---|---|
   | `--embed-into HWND` | The window to live in: its handle, in decimal or `0x` hex. |
   | `--api-port PORT` | Start the control API on this port. `0` picks a free one. |
   | `--handshake-file FILE` | Where TurboADB writes how to reach the API, once its window is up. |
   | `--hide PARTS` | Leave out parts of TurboADB's window, comma separated: `menubar`, `ribbon`, `sidebar`, `statusbar`, `log`. |

2. **Wait for the handshake file.** TurboADB writes it in one go when it is ready,
   usually within a few seconds:

   ```json
   {"api": "http://127.0.0.1:52817/v1", "port": 52817, "token": "…", "pid": 18320,
    "version": "6.1.0", "api_version": 1, "window": 3540230, "embedded": true}
   ```

   If it cannot start, the file says why instead: `{"error": "there is no window 5 to go into", "pid": 18320}`.

3. **Call the API** with the header `Authorization: Bearer <token>`.

## Python: PyQt5, PySide6 and others

`turboadb.embed_qt.TurboADBWidget` is a widget you put in any layout. It works with
PyQt5, PySide6, PyQt6 and PySide2, using the binding your program has already
imported. It starts TurboADB the first time it is shown.

```python
from turboadb.embed_qt import TurboADBWidget

view = TurboADBWidget(hide=("menubar",))
view.started.connect(lambda: view.client.open("R58M12345", page="files"))
view.event_received.connect(lambda event: print(event["type"], event))
layout.addWidget(view)
```

`view.client` is a `turboadb.embed.Embedded` client. Its signals are `started`,
`failed(str)`, `event_received(dict)` and `finished`.

Without Qt, or with any other toolkit, use `turboadb.embed` directly. It needs only
the standard library:

```python
from turboadb.embed import launch

tadb = launch(parent=hwnd, hide=("menubar",))   # hwnd: your window's handle
print(tadb.devices())
tadb.open("R58M12345", page="logcat")
print(tadb.shell("R58M12345", "getprop ro.product.model"))
for event in tadb.events():                      # until TurboADB closes
    print(event["type"], event)
```

`launch(exe=...)` starts the standalone `TurboADB-x.y.z-win64.exe` instead of the
TurboADB of this Python. That is the way for a PySide6 program whose Python has no
PyQt5.

[`examples/embed_in_qt.py`](examples/embed_in_qt.py) is a complete program with its
own toolbar above an embedded TurboADB. Run it with `python examples/embed_in_qt.py`,
or add `PySide6` to use PySide6.

## Delphi

Put a `TPanel` where TurboADB should be, then start TurboADB inside it and read the
handshake. This example was written for Delphi 10.4 and later
(`System.Net.HttpClient`). It has not been compiled here, so treat it as a starting
point.

```pascal
uses
  Winapi.Windows, System.SysUtils, System.Classes, System.IOUtils, System.JSON,
  System.Net.HttpClient, System.Net.URLClient;

procedure TMainForm.StartTurboADB;
var
  Handshake, CommandLine: string;
  Startup: TStartupInfo;
  Process: TProcessInformation;
  Info: TJSONValue;
  Deadline: Cardinal;
begin
  FToken := TGUID.NewGuid.ToString + TGUID.NewGuid.ToString;
  Handshake := TPath.Combine(TPath.GetTempPath, TGUID.NewGuid.ToString + '.json');
  // the token goes in the environment the new process inherits, not on its command line
  SetEnvironmentVariable('TURBOADB_API_TOKEN', PChar(FToken));
  CommandLine := Format('"%s" --embed-into %d --api-port 0 --handshake-file "%s" --hide menubar',
    [TurboADBExe, PanelTurboADB.Handle, Handshake]);
  FillChar(Startup, SizeOf(Startup), 0);
  Startup.cb := SizeOf(Startup);
  if not CreateProcess(nil, PChar(CommandLine), nil, nil, False, 0, nil, nil, Startup, Process) then
    RaiseLastOSError;
  SetEnvironmentVariable('TURBOADB_API_TOKEN', nil);
  CloseHandle(Process.hThread);
  CloseHandle(Process.hProcess);

  Deadline := GetTickCount + 90000;
  while not TFile.Exists(Handshake) do
  begin
    if GetTickCount > Deadline then
      raise Exception.Create('TurboADB did not start');
    Sleep(100);
  end;
  Info := TJSONObject.ParseJSONValue(TFile.ReadAllText(Handshake, TEncoding.UTF8));
  try
    if Info.FindValue('error') <> nil then
      raise Exception.Create(Info.GetValue<string>('error'));
    FApi := Info.GetValue<string>('api');
  finally
    Info.Free;
  end;
  TFile.Delete(Handshake);
end;

function TMainForm.CallTurboADB(const Method, Path, Body: string): TJSONValue;
var
  Http: THTTPClient;
  Response: IHTTPResponse;
  Source: TStringStream;
begin
  Http := THTTPClient.Create;
  try
    Http.CustomHeaders['Authorization'] := 'Bearer ' + FToken;
    if Method = 'GET' then
      Response := Http.Get(FApi + Path)
    else
    begin
      Source := TStringStream.Create(Body, TEncoding.UTF8);
      try
        Response := Http.Post(FApi + Path, Source, nil,
          [TNetHeader.Create('Content-Type', 'application/json')]);
      finally
        Source.Free;
      end;
    end;
    Result := TJSONObject.ParseJSONValue(Response.ContentAsString(TEncoding.UTF8));
  finally
    Http.Free;
  end;
end;

// CallTurboADB('POST', '/open', '{"serial": "R58M12345", "page": "files"}');
// CallTurboADB('POST', '/quit', '{}');   // in FormClose
```

TurboADB follows the panel's size by itself, so no code is needed when the form is
resized.

## C#

```csharp
using System.Diagnostics;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

var token = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32));
var handshake = Path.Combine(Path.GetTempPath(), $"turboadb-{Guid.NewGuid()}.json");
var start = new ProcessStartInfo(turboAdbExe,
    $"--embed-into {panel.Handle} --api-port 0 --handshake-file \"{handshake}\"")
    { UseShellExecute = false };
start.Environment["TURBOADB_API_TOKEN"] = token;
Process.Start(start);

while (!File.Exists(handshake)) await Task.Delay(100);
var api = JsonDocument.Parse(File.ReadAllText(handshake)).RootElement.GetProperty("api").GetString();

var http = new HttpClient();
http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
Console.WriteLine(await http.GetStringAsync(api + "/devices"));
await http.PostAsync(api + "/open", new StringContent(
    "{\"serial\": \"R58M12345\", \"page\": \"files\"}", Encoding.UTF8, "application/json"));
```

## The control API

Every request needs `Authorization: Bearer <token>`. Bodies and answers are JSON.
An answer is `{"ok": true, "result": …}`, or `{"ok": false, "error": "…"}` with an
HTTP status of 400 (bad request), 401 (no or wrong token), 403 (a web page), 404 (no
such device, tab or request), 405, 409 (not possible now) or 504 (TurboADB's window
was busy, for example with a dialog open).

| Request | Body | Result |
|---|---|---|
| `GET /v1/info` | | Version, process, window handle, pages, parts and the device commands offered. |
| `GET /v1/devices` | | Every device adb reports: `serial`, `state`, `online`, `network`, `model`, `open`. |
| `GET /v1/tabs` | | The open tabs: `tab` (a number), `index`, `title`, `kind`, `serial`, `page`, `current`. |
| `POST /v1/open` | `{"serial"}` or `{"host", "port"}`, optional `"page"` | Opens the device's tab, or brings the open one up, on that page. A page asked for before the device has connected is shown once it has. |
| `POST /v1/show` | `{"serial"}` or `{"tab"}`, optional `"page"` | Brings a device tab up. |
| `POST /v1/close` | `{"serial"}` or `{"tab"}` | Closes a tab. |
| `POST /v1/screen` | `{"serial", "action": "start" or "stop", "display_id"}` | Starts or stops the device's screen in Device Control. |
| `POST /v1/device` | `{"serial", "method", "args": [], "kwargs": {}}` | Runs one device command and returns its result: `{"success", "value", "error", …}`. |
| `GET /v1/ui` | | Which parts show, the Webcam page setting and the theme. |
| `POST /v1/ui` | any of `menubar`, `ribbon`, `sidebar`, `statusbar`, `log`, `webcam_page` (true or false) and `theme` | Shows or hides those parts, turns the Webcam page on or off, or changes the theme. |
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

## Things to know

- **Windows.** Embedding uses Windows window handles. The control API works on
  every system, with `--api-port` and no `--embed-into`.
- **Size and DPI.** TurboADB fills your window's client area and follows it. It also
  takes the DPI awareness of your window, so it is sized right in programs that are
  not per-monitor aware.
- **Keyboard.** A click in TurboADB gives it the keyboard, as in any window.
  `/v1/focus` does the same from code while your window is the active one.
- **Closing.** A hidden window keeps TurboADB running, so it can sit on a tab page that
  is not showing. A destroyed window closes it within a fraction of a second.
  `/v1/quit` closes it at any time, and `TurboADBWidget` closes it when your program
  quits.
- **Several at once.** Each window can hold its own TurboADB, and the normal TurboADB
  window can run beside them. They share the adb server and `~/.turboadb`.
- **Updates.** Inside your program, TurboADB does not restart itself after an update.
  Start it again to use the new version.
- **Security.** The API listens on `127.0.0.1` only and needs the token. Requests from
  web pages are refused. The token never appears on a command line, and TurboADB
  removes it from its environment, so nothing it starts inherits it. Device commands
  come from a fixed list. `push` and `pull` read and write files with the rights of the
  account TurboADB runs as.
