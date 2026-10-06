; QQ 聊天记录分析工具 Windows 安装包
; 由 build.ps1 传入 /DMyAppVersion=<版本号>，不要手工改版本。

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "QQ 聊天记录分析工具"
#define MyAppExeName "QQChatLog.exe"

[Setup]
AppId={{9E68F6BB-98B8-4A89-9E8C-FE3F18B5F158}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=woowss
AppPublisherURL=https://github.com/woowss/qq-chatlog-analyze-tool
AppSupportURL=https://github.com/woowss/qq-chatlog-analyze-tool/issues
AppUpdatesURL=https://github.com/woowss/qq-chatlog-analyze-tool/releases
DefaultDirName={code:GetDefaultInstallDir}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist\windows
OutputBaseFilename=QQChatLog-{#MyAppVersion}-windows-x64-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#MyAppName}
Uninstallable=yes
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "..\..\dist\windows\QQChatLog\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function GetDefaultInstallDir(Param: String): String;
var
  LocalAppData: String;
  UserProfile: String;
begin
  // 优先使用环境变量；精简 Windows 镜像有时没有注册表 Shell Folder 项，
  // 这时 Inno 的 localappdata 常量会失败，但用户环境变量仍然可用。
  LocalAppData := GetEnv('LOCALAPPDATA');
  if LocalAppData = '' then
  begin
    UserProfile := GetEnv('USERPROFILE');
    if UserProfile <> '' then
      LocalAppData := AddBackslash(UserProfile) + 'AppData\Local';
  end;
  if LocalAppData = '' then
    LocalAppData := GetEnv('TEMP');
  if LocalAppData = '' then
    LocalAppData := ExpandConstant('{tmp}');
  Result := AddBackslash(LocalAppData) + 'Programs\QQChatLog';
end;

// 用户数据位于 %LOCALAPPDATA%\qqchatlog，不在安装目录中。
// 因此卸载只删除程序文件，不删除聊天记录、配置、缓存或日志。
