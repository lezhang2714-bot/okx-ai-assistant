; OKX AI Assistant — Inno Setup script (strategy 1: runtime setup inside installer UI)
; Build via build_installer_windows.bat (requires Inno Setup 6).

#ifndef MyAppVersion
  #define MyAppVersion "1.3.1"
#endif
#ifndef MyAppName
  #define MyAppName "OKX AI Assistant"
#endif
#ifndef MyAppSlug
  #define MyAppSlug "OKX-AI-Assistant"
#endif
#ifndef StagingDir
  #define StagingDir "..\release_staging\installer_payload"
#endif

[Setup]
AppId={{C8E4F1A2-9B3D-4E56-AF70-2D8C9E1B4A60}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=OKX AI Assistant
DefaultDirName={localappdata}\Programs\{#MyAppSlug}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\output
OutputBaseFilename={#MyAppSlug}-Setup-v{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\web_assets\app.ico
SetupIconFile={#StagingDir}\web_assets\app.ico
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
Source: "{#StagingDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\launch_web_control_panel.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\web_assets\app.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\launch_web_control_panel.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\web_assets\app.ico"; Tasks: desktopicon

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch_web_control_panel.vbs"""; Description: "Launch {#MyAppName}"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\build"
Type: filesandordirs; Name: "{app}\.venv"

[Code]
var
  RuntimeSetupPage: TOutputMsgMemoWizardPage;
  RuntimeSetupStarted: Boolean;
  RuntimeSetupExitCode: Integer;

function RuntimeReady: Boolean;
begin
  Result :=
    FileExists(ExpandConstant('{app}\.venv\Scripts\python.exe')) or
    FileExists(ExpandConstant('{app}\build\python_runtime\python.exe'));
end;

procedure AppendSetupLog(const LogPath: String);
var
  Lines: TArrayOfString;
  I: Integer;
begin
  if LoadStringsFromFile(LogPath, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      RuntimeSetupPage.RichEditViewer.Lines.Add(Lines[I]);
end;

procedure RunRuntimeSetup;
var
  LogPath, AppDir, CmdLine: String;
  ExecOk: Boolean;
begin
  RuntimeSetupExitCode := 1;
  LogPath := ExpandConstant('{tmp}\okx_runtime_setup.log');
  AppDir := ExpandConstant('{app}');

  DeleteFile(LogPath);
  RuntimeSetupPage.RichEditViewer.Lines.Clear;
  RuntimeSetupPage.RichEditViewer.Lines.Add('正在启动运行环境安装…');

  WizardForm.NextButton.Enabled := False;
  WizardForm.BackButton.Enabled := False;
  WizardForm.CancelButton.Enabled := False;
  try
    CmdLine := '/c set OKX_SETUP_SILENT=1&& call "' + AppDir + '\setup_windows_runtime.bat" > "' + LogPath + '" 2>&1';
    ExecOk := Exec(ExpandConstant('{cmd}'), CmdLine, AppDir, SW_HIDE, ewWaitUntilTerminated, RuntimeSetupExitCode);
    AppendSetupLog(LogPath);

    if ExecOk and (RuntimeSetupExitCode = 0) then
    begin
      RuntimeSetupPage.RichEditViewer.Lines.Add('');
      RuntimeSetupPage.RichEditViewer.Lines.Add('运行环境安装完成。');
    end
    else
    begin
      RuntimeSetupPage.RichEditViewer.Lines.Add('');
      RuntimeSetupPage.RichEditViewer.Lines.Add('运行环境安装未成功完成。');
      RuntimeSetupPage.RichEditViewer.Lines.Add('可在安装目录手动运行 setup_windows_runtime.bat。');
    end;
  finally
    WizardForm.NextButton.Enabled := True;
    WizardForm.BackButton.Enabled := True;
    WizardForm.CancelButton.Enabled := True;
  end;
end;

procedure InitializeWizard;
begin
  RuntimeSetupPage := CreateOutputMsgMemoPage(wpInstalling,
    '正在安装运行环境',
    '正在安装 Python 与依赖包，请保持网络连接。首次安装可能需要几分钟。',
    '安装日志',
    '');
  RuntimeSetupStarted := False;
  RuntimeSetupExitCode := 0;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = RuntimeSetupPage.ID) and (not RuntimeSetupStarted) then
  begin
    RuntimeSetupStarted := True;
    RunRuntimeSetup;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = RuntimeSetupPage.ID then
    Result := RuntimeReady;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = RuntimeSetupPage.ID then
  begin
    if RuntimeSetupExitCode <> 0 then
      if MsgBox('运行环境未能自动配置完成。' + #13#10 +
        '可稍后在安装目录运行 setup_windows_runtime.bat。' + #13#10#13#10 +
        '是否仍继续完成安装？', mbConfirmation, MB_YESNO) = IDNO then
        Result := False;
  end;
end;
