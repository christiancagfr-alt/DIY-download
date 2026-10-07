; Inno Setup script for DIY下载器
; 使用 PyInstaller onedir 目录安装，避免 onefile 在 Temp\_MEI 解压 python312.dll 失败。

#ifndef MyAppVersion
  #define MyAppVersion "1.1.4"
#endif

#ifndef MyAppSourceDir
  #define MyAppSourceDir "..\dist\DIYDownloader"
#endif

#define MyAppName "DIY下载器"
#define MyAppPublisher "secure-artifacts"
#define MyAppURL "https://github.com/secure-artifacts/DIY-sheets_batch_downloader"
#define MyAppExeName "DIYDownloader.exe"

[Setup]
AppId={{A7C2E4B1-9F3D-4C18-8B6A-DIYDOWNLOADER01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\DIYDownloader
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=DIYDownloader-v{#MyAppVersion}-windows-setup
SetupIconFile=..\logo.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; 默认装到用户目录，无需管理员；减少 Program Files 权限问题
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Files]
; 整个 onedir 目录递归安装（含 python312.dll 与依赖，不再从 Temp 解压）
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\logo.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\logo.ico"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
