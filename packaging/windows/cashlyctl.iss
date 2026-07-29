#define EnvVersion GetEnv("CASHLYCTL_WINDOWS_VERSION")
#if EnvVersion == ""
#define MyAppVersion "0.1.0"
#else
#define MyAppVersion EnvVersion
#endif

[Setup]
AppId={{78BA0C15-7D5B-4F54-A713-1A1908AD9BB1}
AppName=CashlyCTL
AppVersion={#MyAppVersion}
AppPublisher=Cashly Tech Services Inc.
AppPublisherURL=https://gocashly.io
DefaultDirName={localappdata}\Programs\CashlyCTL
DefaultGroupName=CashlyCTL
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist\installer
OutputBaseFilename=CashlyCTLSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
UninstallDisplayIcon={app}\cashlyctl.exe

[Tasks]
Name: startup; Description: "Start CashlyCTL hotkeys when I sign in"; Flags: unchecked

[Files]
Source: "..\..\dist\cashlyctl\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CashlyCTL Console"; Filename: "{app}\cashlyctl.exe"; Parameters: "console"; WorkingDir: "{app}"
Name: "{group}\CashlyCTL Hotkeys"; Filename: "{app}\cashlyctl.exe"; Parameters: "hotkeys start"; WorkingDir: "{app}"
Name: "{group}\CashlyCTL Pair CRM"; Filename: "{app}\cashlyctl.exe"; Parameters: "crm pair --open-browser"; WorkingDir: "{app}"
Name: "{userstartup}\CashlyCTL Hotkeys"; Filename: "{app}\cashlyctl.exe"; Parameters: "hotkeys start --minimize-console"; WorkingDir: "{app}"; Tasks: startup

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}')); Flags: preservestringtype

[Run]
Filename: "{app}\cashlyctl.exe"; Parameters: "hotkeys start --minimize-console"; Description: "Start CashlyCTL hotkeys now"; Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
Type: files; Name: "{userstartup}\CashlyCTL Hotkeys.lnk"

[Code]
function NeedsAddPath(PathToAdd: string): Boolean;
var
  CurrentPath: string;
  SearchPath: string;
  SearchNeedle: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', CurrentPath) then
    CurrentPath := '';

  SearchPath := ';' + Uppercase(CurrentPath) + ';';
  SearchNeedle := ';' + Uppercase(PathToAdd) + ';';
  Result := Pos(SearchNeedle, SearchPath) = 0;
end;
