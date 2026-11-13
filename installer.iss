; 视频提取器 —— 安装脚本（Inno Setup 6）
; 程序装到 Program Files，用户数据（配置 / cookie / 视频）放 %LOCALAPPDATA%

#define MyAppName "视频提取器"
#define MyAppNameEn "VideoGrabber"
#define MyAppVersion "1.3.1"
#define MyAppPublisher "Local"
#define MyAppExeName "VideoGrabber.exe"

[Setup]
AppId={{9C4A7E21-6B3D-4F58-9A2E-1D7C5B8F0A34}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=VideoGrabber-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
CreateDesktopIcon=Create a desktop shortcut
LaunchApp=Launch {#MyAppName}
AdditionalIcons=Additional tasks:

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清程序目录下的残留，用户数据（LOCALAPPDATA）保留
Type: files; Name: "{app}\config.json"
Type: files; Name: "{app}\cookies.txt"
Type: filesandordirs; Name: "{app}\downloads"
Type: filesandordirs; Name: "{app}\extension"
Type: filesandordirs; Name: "{app}\browser-profile"
