' PackSync.vbs — silent launcher for Pack Sync (no console window)
' Double-click this file to start Pack Sync without any console flash.
' Requires Python 3.11+ to be installed (https://www.python.org/downloads/)

Option Explicit

Dim oShell, oFSO, dir, script, py, candidates, i

Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

dir    = oFSO.GetParentFolderName(WScript.ScriptFullName)
script = dir & "\pack_sync.py"

If Not oFSO.FileExists(script) Then
    MsgBox "pack_sync.py not found next to PackSync.vbs." & vbCrLf & _
           "Make sure both files are in the same folder.", vbCritical, "Pack Sync"
    WScript.Quit 1
End If

' ── Try pythonw candidates (no console window) ──────────────────────────────

' Build a list: well-known per-user and system locations first, then plain PATH
candidates = Array( _
    oShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe", _
    oShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe", _
    oShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe", _
    oShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python310\pythonw.exe", _
    "C:\Python313\pythonw.exe", _
    "C:\Python312\pythonw.exe", _
    "C:\Python311\pythonw.exe", _
    "pythonw.exe" _
)

For i = 0 To UBound(candidates)
    py = candidates(i)
    ' For plain "pythonw.exe" (PATH lookup) always try; for full paths check existence
    If InStr(py, "\") = 0 Or oFSO.FileExists(py) Then
        On Error Resume Next
        ' Window style 0 = hidden; bWaitOnReturn = False
        oShell.Run """" & py & """ """ & script & """", 0, False
        If Err.Number = 0 Then WScript.Quit 0
        On Error GoTo 0
    End If
Next

' ── Fall back: try "pyw" launcher (Windows Python Launcher w/o console) ───────
On Error Resume Next
oShell.Run "pyw -3 """ & script & """", 0, False
If Err.Number = 0 Then WScript.Quit 0
On Error GoTo 0

' ── Nothing worked ────────────────────────────────────────────────────────────
MsgBox "Python 3.11 or newer is required to run Pack Sync." & vbCrLf & vbCrLf & _
       "Download from: https://www.python.org/downloads/" & vbCrLf & _
       "Enable ""Add Python to PATH"" during installation.", _
       vbExclamation, "Pack Sync — Python Not Found"
WScript.Quit 1
