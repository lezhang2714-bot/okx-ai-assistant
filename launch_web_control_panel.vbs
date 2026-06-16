' Launch OKX AI Assistant with tray + taskbar window (no console).
Option Explicit

Dim shell, fso, baseDir, launcherPath, pythonPath, cmdLine, msg

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcherPath = baseDir & "\tray_launcher.py"

If Not fso.FileExists(launcherPath) Then
  msg = "Cannot find tray launcher:" & vbCrLf & launcherPath
  MsgBox msg, vbCritical, "OKX AI Assistant"
  WScript.Quit 1
End If

If Not HasRuntimePython(baseDir, fso) Then
  msg = "Python runtime is not installed yet." & vbCrLf & vbCrLf
  msg = msg & "Run setup_windows_runtime.bat in the install folder,"
  msg = msg & " or rerun the installer to finish setup."
  MsgBox msg, vbExclamation, "OKX AI Assistant"
  WScript.Quit 1
End If

pythonPath = FindPythonLauncher(baseDir, fso)
If pythonPath = "" Then
  MsgBox "Cannot find pythonw.exe or python.exe in the install folder.", vbCritical, "OKX AI Assistant"
  WScript.Quit 1
End If

cmdLine = """" & pythonPath & """ """ & launcherPath & """"
shell.Run cmdLine, 1, False

Function HasRuntimePython(ByVal root, ByVal files)
  HasRuntimePython = _
    files.FileExists(root & "\.venv\Scripts\python.exe") Or _
    files.FileExists(root & "\.python\python.exe") Or _
    files.FileExists(root & "\build\python_runtime\python.exe")
End Function

Function FindPythonLauncher(ByVal root, ByVal files)
  If files.FileExists(root & "\.venv\Scripts\pythonw.exe") Then
    FindPythonLauncher = root & "\.venv\Scripts\pythonw.exe"
    Exit Function
  End If
  If files.FileExists(root & "\.venv\Scripts\python.exe") Then
    FindPythonLauncher = root & "\.venv\Scripts\python.exe"
    Exit Function
  End If
  If files.FileExists(root & "\build\python_runtime\pythonw.exe") Then
    FindPythonLauncher = root & "\build\python_runtime\pythonw.exe"
    Exit Function
  End If
  If files.FileExists(root & "\build\python_runtime\python.exe") Then
    FindPythonLauncher = root & "\build\python_runtime\python.exe"
    Exit Function
  End If
  If files.FileExists(root & "\.python\pythonw.exe") Then
    FindPythonLauncher = root & "\.python\pythonw.exe"
    Exit Function
  End If
  If files.FileExists(root & "\.python\python.exe") Then
    FindPythonLauncher = root & "\.python\python.exe"
    Exit Function
  End If
  FindPythonLauncher = ""
End Function
