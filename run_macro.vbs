Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
' Correctly use WScript.ScriptFullName to resolve the script path
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "pythonw.exe """ & scriptDir & "\macro_app.py""", 0, False
