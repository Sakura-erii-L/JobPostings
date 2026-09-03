#define MyAppName "JobPostings"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "JobPostings"
#define MyAppExeName "JobPostings.exe"

[Setup]
AppId={{C4B6DCC4-EE1B-4D45-93F0-9FDFE6D6F2B7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\JobPostings
DefaultGroupName={#MyAppName}
OutputDir=output
OutputBaseFilename=JobPostings-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
Uninstallable=yes

[Files]
Source: "..\build\jobpostings-server\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\build\JobPostings.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\JobPostings"; Filename: "{app}\JobPostings.exe"
Name: "{userdesktop}\JobPostings"; Filename: "{app}\JobPostings.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："
Name: "startup"; Description: "开机启动并驻留托盘"; GroupDescription: "启动选项："

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JobPostings"; ValueData: "{app}\JobPostings.exe"; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\JobPostings.exe"; Description: "启动 JobPostings"; Flags: nowait postinstall skipifsilent
