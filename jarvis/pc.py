"""The PC domain at Kodee depth: one organized library for
THIS computer — info, processes, windows, disks, battery,
clipboard, volume, and the two genuinely destructive acts
(kill a process, empty the recycle bin) that ride the gate's
confirmation cards.

Every op follows the house rules: a real Windows driver
(PowerShell, JSON out), an honest refusal off-Windows, and
parsers that are unit-tested on canned output. The runner is
injectable so the logic is provable anywhere.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

Runner = Callable[[str], Dict[str, Any]]


def _default_runner(script: str) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-Command", script],
            capture_output=True, text=True, timeout=30)
        return {"rc": proc.returncode,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or ""}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_ps(script: str, runner: Optional[Runner] = None,
           ) -> Dict[str, Any]:
    """One PowerShell call. Off-Windows: the honest refusal
    every PC op shares."""
    if sys.platform != "win32":
        return {"error": f"driver not present on this OS "
                         f"({sys.platform}): ye kaam sirf "
                         "Windows par hota hai, sir."}
    out = (runner or _default_runner)(script)
    if out.get("error"):
        return {"error": str(out["error"])}
    if out.get("rc") not in (0, None):
        tail = str(out.get("stderr", "")).strip()[-200:]
        return {"error": f"PowerShell rc={out.get('rc')}"
                         + (f": {tail}" if tail else "")}
    return out


def _ps_json(script: str, runner=None):
    """Run and parse the JSON the script prints. Returns
    (data, error_dict)."""
    out = run_ps(script, runner)
    if out.get("error"):
        return None, out
    text = (out.get("stdout") or "").strip()
    try:
        return json.loads(text), None
    except Exception:
        return None, {"error": "PowerShell ka jawab samajh "
                               f"nahi aaya: {text[:120]}"}


def _as_list(data) -> List[Dict]:
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


# -- read-only eyes ------------------------------------------------

def op_pc_info(runner=None):
    script = (
        "$os = Get-CimInstance Win32_OperatingSystem; "
        "$cpu = Get-CimInstance Win32_Processor | "
        "Select-Object -First 1; "
        "$gpu = Get-CimInstance Win32_VideoController | "
        "Select-Object -First 1; "
        "[pscustomobject]@{os=$os.Caption; "
        "cpu=$cpu.Name; gpu=$gpu.Name; "
        "ram_total_gb=[math]::Round($os.TotalVisibleMemorySize"
        "/1MB,1); ram_free_gb=[math]::Round("
        "$os.FreePhysicalMemory/1MB,1); "
        "uptime_hours=[math]::Round(((Get-Date) - "
        "$os.LastBootUpTime).TotalHours,1)} | ConvertTo-Json "
        "-Compress")
    data, err = _ps_json(script, runner)
    if err:
        return err
    return {"ok": True, "info": data}


def op_process_list(name: str = "", runner=None):
    filt = ""
    if name:
        # Escape PowerShell's -like wildcards so the filter is
        # a plain substring search, never a pattern the caller
        # (or a process name) smuggled in.
        safe = str(name).replace("`", "``").replace(
            "*", "`*").replace("?", "`?").replace(
            "[", "`[").replace("]", "`]").replace("'", "''")
        filt = f" | Where-Object {{$_.ProcessName -like '*{safe}*'}}"
    script = (
        "Get-Process" + filt +
        " | Sort-Object CPU -Descending | Select-Object "
        "-First 20 ProcessName, Id, "
        "@{n='cpu_s';e={[math]::Round($_.CPU,1)}}, "
        "@{n='mem_mb';e={[math]::Round($_.WorkingSet64/1MB,1)}} "
        "| ConvertTo-Json -Compress")
    data, err = _ps_json(script, runner)
    if err:
        return err
    return {"ok": True, "processes": _as_list(data)}


def op_window_list(runner=None):
    script = (
        "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} "
        "| Select-Object ProcessName, Id, MainWindowTitle "
        "| ConvertTo-Json -Compress")
    data, err = _ps_json(script, runner)
    if err:
        return err
    rows = [{"app": r.get("ProcessName"), "pid": r.get("Id"),
             "title": r.get("MainWindowTitle")}
            for r in _as_list(data)]
    return {"ok": True, "windows": rows}


def op_disk_usage(runner=None):
    script = (
        "Get-CimInstance Win32_LogicalDisk -Filter "
        "\"DriveType=3\" | Select-Object DeviceID, "
        "@{n='free_gb';e={[math]::Round($_.FreeSpace/1GB,1)}}, "
        "@{n='total_gb';e={[math]::Round($_.Size/1GB,1)}} "
        "| ConvertTo-Json -Compress")
    data, err = _ps_json(script, runner)
    if err:
        return err
    return {"ok": True, "drives": _as_list(data)}


def op_battery_status(runner=None):
    script = (
        "$b = Get-CimInstance Win32_Battery | Select-Object "
        "-First 1; if ($b) { [pscustomobject]@{percent="
        "$b.EstimatedChargeRemaining; "
        "status=$b.BatteryStatus} | ConvertTo-Json -Compress } "
        "else { '{\"percent\": null}' }")
    data, err = _ps_json(script, runner)
    if err:
        return err
    if not data or data.get("percent") is None:
        return {"ok": True, "battery": None,
                "note": "Is machine me battery nahi hai, sir "
                        "(desktop lagta hai)."}
    return {"ok": True, "battery": data}


def op_clipboard_read(runner=None):
    out = run_ps("Get-Clipboard -Raw", runner)
    if out.get("error"):
        return out
    full = out.get("stdout") or ""
    return {"ok": True, "clipboard": full[:2000],
            "truncated": len(full) > 2000}


# -- hands -----------------------------------------------------------

def op_clipboard_write(text: str, runner=None):
    safe = str(text).replace("'", "''")
    out = run_ps(f"Set-Clipboard -Value '{safe}'", runner)
    if out.get("error"):
        return out
    # Verify by reading back (audit 2026-10-22: Set-Clipboard
    # can fail non-terminatingly with exit 0, and the owner
    # then pastes the OLD clipboard somewhere, told it was
    # the new text).
    back = run_ps("Get-Clipboard -Raw", runner)
    if not back.get("error"):
        actual = (back.get("stdout") or "").rstrip("\r\n")
        if actual != str(text):
            return {"ok": False,
                    "error": "clipboard par text nahi chipka — "
                             "wapas padhne par kuch aur mila"}
    return {"ok": True, "clipboard_set": True,
            "chars": len(str(text)), "verified": True}


# The canonical CoreAudio route to the master endpoint:
# MMDeviceEnumerator -> default render device -> Activate
# IAudioEndpointVolume -> scalar get/set. Complete and stock
# (no third-party tools); compiled in-memory by Add-Type.
_AUDIO_CS = r"""
using System;
using System.Runtime.InteropServices;
[Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
class MMDeviceEnumerator { }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
  int EnumAudioEndpoints(int flow, int state, out IntPtr c);
  int GetDefaultAudioEndpoint(int flow, int role,
                              out IMMDevice dev);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
  int Activate(ref Guid iid, int ctx, IntPtr p,
    [MarshalAs(UnmanagedType.IUnknown)] out object o);
}
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int RegisterControlChangeNotify(IntPtr n);
  int UnregisterControlChangeNotify(IntPtr n);
  int GetChannelCount(out uint c);
  int SetMasterVolumeLevel(float l, Guid ctx);
  int SetMasterVolumeLevelScalar(float l, Guid ctx);
  int GetMasterVolumeLevel(out float l);
  int GetMasterVolumeLevelScalar(out float l);
  int SetChannelVolumeLevel(uint ch, float l, Guid ctx);
  int SetChannelVolumeLevelScalar(uint ch, float l, Guid ctx);
  int GetChannelVolumeLevel(uint ch, out float l);
  int GetChannelVolumeLevelScalar(uint ch, out float l);
  int SetMute(bool m, Guid ctx);
  int GetMute(out bool m);
  int GetVolumeStepInfo(out uint s, out uint t);
  int VolumeStepUp(Guid ctx);
  int VolumeStepDown(Guid ctx);
  int QueryHardwareSupport(out uint m);
  int GetVolumeRange(out float mn, out float mx,
                     out float inc);
}
public static class AudioVol {
  static IAudioEndpointVolume Endpoint() {
    var e = (IMMDeviceEnumerator)new MMDeviceEnumerator();
    IMMDevice dev;
    e.GetDefaultAudioEndpoint(0, 1, out dev);
    Guid iid = typeof(IAudioEndpointVolume).GUID;
    object o;
    dev.Activate(ref iid, 23, IntPtr.Zero, out o);
    return (IAudioEndpointVolume)o;
  }
  public static float Get() {
    float l; Endpoint().GetMasterVolumeLevelScalar(out l);
    return l;
  }
  public static void Set(float f) {
    Endpoint().SetMasterVolumeLevelScalar(f, Guid.Empty);
  }
}
"""


def _audio_script(call: str) -> str:
    return ("Add-Type -TypeDefinition @'\n" + _AUDIO_CS +
            "\n'@; " + call)


def _parse_level(text: str):
    try:
        return int(round(float(str(text).strip())))
    except (TypeError, ValueError):
        return None


def op_volume_get(runner=None):
    out = run_ps(_audio_script(
        "[math]::Round([AudioVol]::Get() * 100)"), runner)
    if out.get("error"):
        return out
    text = (out.get("stdout") or "").strip()
    try:
        return {"ok": True, "volume": int(float(text))}
    except ValueError:
        return {"error": f"volume samajh nahi aaya: {text[:80]}"}


def op_volume_set(level: int, runner=None):
    """Two routes, per the options rule. Route 1: the absolute
    endpoint scalar (exact). Route 2 (only when route 1 errors):
    the multimedia key-ladder — pin to the floor with 50
    down-steps, then climb level/2 up-steps — reported honestly
    as approximate."""
    level = max(0, min(100, int(level)))
    out = run_ps(_audio_script(
        f"[AudioVol]::Set({level / 100.0:.2f}); "
        "[math]::Round([AudioVol]::Get() * 100)"), runner)
    if not out.get("error"):
        # The script already reads the level back — USE it
        # (audit 2026-10-22: the read-back was computed and
        # thrown away, so a failed Set still reported the
        # requested level as done).
        actual = _parse_level(out.get("stdout") or "")
        if actual is not None and abs(actual - level) <= 1:
            return {"ok": True, "volume_set_to": level,
                    "route": "endpoint (exact)",
                    "verified": True}
    steps_up = int(round(level / 2.0))
    script = (
        "$s = New-Object -ComObject WScript.Shell; "
        "for ($i=0; $i -lt 50; $i++) "
        "{$s.SendKeys([char]174)}; "
        f"for ($i=0; $i -lt {steps_up}; $i++) "
        "{$s.SendKeys([char]175)}")
    out2 = run_ps(script, runner)
    if out2.get("error"):
        return out2
    back = run_ps(_audio_script(
        "[math]::Round([AudioVol]::Get() * 100)"), runner)
    actual = None if back.get("error") else _parse_level(
        back.get("stdout") or "")
    if actual is not None and abs(actual - level) > 3:
        return {"ok": False, "volume_now": actual,
                "error": f"volume {level} par nahi gaya — "
                         f"abhi {actual} par hai, dono raaste "
                         "koshish karke haar gaye"}
    return {"ok": True, "volume_set_to": level,
            "route": "key-ladder (floor se gin kar)",
            "verified": actual is not None,
            "note": "Pehla raasta fail hua, isliye key-steps "
                    "wala dusra raasta chala — volume takreeban "
                    "usi satah par hai, halka idhar-udhar ho "
                    "sakta hai, sir."}


def op_process_kill(target: str, runner=None):
    """DESTRUCTIVE — rides the confirmation card. Kills by
    EXACT process name or numeric pid, and verifies the
    process is actually gone.

    Audit 2026-10-22: Get-Process -Name treats its argument
    as a WILDCARD pattern — target='*' would have piped every
    process on the machine into Stop-Process. Names now match
    by exact -ieq equality over an enumeration, wildcard
    characters are refused outright, the PID branch carries
    -ErrorAction Stop like the name branch always did, and
    both branches check afterwards that nothing survives."""
    t = str(target).strip()
    if any(ch in t for ch in "*?[]"):
        return {"error": "process_kill failed: target me "
                         "wildcard (* ? [ ]) nahi chalega — "
                         "poora exact process naam ya PID do, "
                         "sir. Wildcard se aadha PC band ho "
                         "sakta hai."}
    if t.isdigit():
        pid = int(t)
        script = (f"Stop-Process -Id {pid} -Force "
                  f"-ErrorAction Stop; "
                  f"Start-Sleep -Milliseconds 200; "
                  f"if (Get-Process -Id {pid} -ErrorAction "
                  f"SilentlyContinue) {{ throw 'process abhi "
                  f"bhi zinda hai' }}")
    else:
        safe = t.replace("'", "''")
        script = (f"$ps = Get-Process | Where-Object "
                  f"{{$_.ProcessName -ieq '{safe}'}}; "
                  f"if (-not $ps) {{ throw 'is naam ka koi "
                  f"process nahi mila' }}; "
                  f"$ps | Stop-Process -Force -ErrorAction "
                  f"Stop; Start-Sleep -Milliseconds 200; "
                  f"if (Get-Process | Where-Object "
                  f"{{$_.ProcessName -ieq '{safe}'}}) "
                  f"{{ throw 'process abhi bhi zinda hai' }}")
    out = run_ps(script, runner)
    if out.get("error"):
        return out
    return {"ok": True, "killed": t, "verified": True}


def op_empty_recycle_bin(runner=None):
    """DESTRUCTIVE — rides the confirmation card."""
    out = run_ps("Clear-RecycleBin -Force -ErrorAction Stop",
                 runner)
    if out.get("error"):
        return out
    return {"ok": True, "recycle_bin_emptied": True}


def op_lock_pc(runner=None):
    out = run_ps("rundll32.exe user32.dll,LockWorkStation",
                 runner)
    if out.get("error"):
        return out
    # rundll32 exits 0 even when the lock never engages, and
    # Windows offers no cheap read-back — say so instead of
    # claiming a verified lock (audit 2026-10-22).
    return {"ok": True, "locked": True, "verified": False,
            "note": "lock command chal gaya, sir — Windows "
                    "iski pushti wapas nahi deta, isliye "
                    "screen dekh kar pakka kar lena."}
