; Inno Setup script for SymLiSync — single installer that drops the tray GUI
; (SymLiSync.exe) + the console CLI (symlisync.exe) and puts the CLI on PATH.
; Per-user install (no admin); the user may change the install directory.

#define AppName "SymLiSync"
#define AppVersion "1.1.0"
#define AppPublisher "GaiusSheh"
#define AppExe "SymLiSync-Tray.exe"
#define CliExe "symlisync.exe"

[Setup]
AppId={{8F3A1C2E-7B5D-4E9A-9C61-2F0A6D4B8E11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ChangesEnvironment=yes
OutputDir={#SourcePath}\..\dist
OutputBaseFilename=SymLiSync-Setup-{#AppVersion}
SetupIconFile={#SourcePath}\..\src\ui\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Restart Manager can't close our hidden tray window, so we taskkill it
; explicitly in code (PrepareToInstall / uninstall) instead.
CloseApplications=no

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Files]
Source: "{#SourcePath}\..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\..\dist\{#CliExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"

[Tasks]
Name: "addtopath"; Description: "把命令行工具 symlisync 加入 PATH（供脚本/AI 代理调用）"; Flags: checkedonce
Name: "autostart"; Description: "开机时自动启动 SymLiSync（后台托盘运行）"

[Registry]
; Append {app} to the user PATH (only when not already present and the task is selected)
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath('{app}')
; Autostart: write the same HKCU Run value the app's settings use (name "SymLiSync"),
; so the in-app「开机自启」checkbox stays in sync. Removed on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "SymLiSync"; ValueData: """{app}\{#AppExe}"""; Tasks: autostart; \
    Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove runtime-registered integration (Explorer right-click menu + autostart)
; before the files are deleted, so nothing is left orphaned.
Filename: "{app}\{#CliExe}"; Parameters: "cleanup"; RunOnceId: "SymLiSyncCleanup"; Flags: runhidden

[Code]
procedure KillRunning;
var
  rc: Integer;
begin
  // Force-close the running tray + CLI so files aren't locked during
  // install/uninstall. The app has no unsaved state, so this is safe.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /t /im SymLiSync-Tray.exe',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im symlisync.exe',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillRunning;
  Result := '';
end;

function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
  Target: string;
begin
  Target := ExpandConstant(Param);
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Lowercase(Target) + ';', ';' + Lowercase(OrigPath) + ';') = 0;
end;

procedure RemovePath(Target: string);
var
  OrigPath: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
    exit;
  // Try both "<path>;" and ";<path>" forms
  P := Pos(';' + Target, OrigPath);
  if P = 0 then
    P := Pos(Target + ';', OrigPath);
  if P = 0 then
    P := Pos(Target, OrigPath);
  if P > 0 then
  begin
    Delete(OrigPath, P, Length(Target) + 1);
    RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep = usUninstall then
  begin
    KillRunning;   // close the tray first so its exe isn't locked
    RemovePath(ExpandConstant('{app}'));
    // Let the user decide whether to keep their data (symlinks config, settings).
    // Default button = "是" (keep); choosing "否" deletes the data folder.
    DataDir := ExpandConstant('{app}\data');
    if DirExists(DataDir) then
    begin
      if MsgBox('是否保留你的 SymLiSync 数据（symlinks 配置、设置等）？'#13#10#13#10
                + '选择「是」保留（重装后可恢复）；选择「否」将一并删除。',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON1) = IDNO then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
