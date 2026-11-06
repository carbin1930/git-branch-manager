Option Explicit
' Git Branch Manager - launcher (no console window)
Dim fso, sh, base, pyw, i, candidates(2)

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)

' Candidate interpreters, in order of preference.
' NOTE: pythonw.exe on PATH may be a stripped build without tkinter,
' so the full installed Python path is tried first.
candidates(0) = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python314-32\pythonw.exe"
candidates(1) = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Launcher\pyw.exe"
candidates(2) = "pythonw.exe"

pyw = candidates(2)
For i = 0 To 1
    If fso.FileExists(candidates(i)) Then
        pyw = candidates(i)
        Exit For
    End If
Next

sh.CurrentDirectory = base
On Error Resume Next
sh.Run """" & pyw & """ """ & base & "\git_branch_manager.pyw""", 0, False
If Err.Number <> 0 Then
    MsgBox "Cannot start Python." & vbCrLf & vbCrLf & _
           "Please install Python 3 (with tkinter) or edit this file to point to pythonw.exe." & vbCrLf & vbCrLf & _
           "Tried: " & pyw, 16, "Git Branch Manager"
End If
