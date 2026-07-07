; =====================================================================
;  AutoEdit インストーラー (Inno Setup スクリプト)
;  docs/request/resolve8.md に基づくダウンローダ型インストーラー。
;
;  役割:
;    1. GitHub Releases から本体ペイロード(分割zip)をダウンロード   (R2/§4.2)
;    2. 分割partを結合し、SHA256で破損検知                          (§8)
;    3. インストール先へ展開 (onedir 構成をそのまま配置)            (§4)
;    4. 編集動画の出力先フォルダをユーザーに選ばせ setting.json へ反映 (R3/§6)
;    5. デスクトップショートカット作成の可否を選ばせる              (R4/§5.2)
;
;  前提:
;    - Inno Setup 6.1 以上 (CreateDownloadPage / DownloadTemporaryFile を使用)
;    - 配布は CUDA 不使用の CPU 版 (resolve8.md §4.2.1)。GPU版は配布しない。
;    - インストール先は既定でユーザー領域 ({localappdata}) とし管理者昇格不要 (§9-5)
;
;  リリース担当が編集する箇所は下記 #define (バージョン/part一覧/SHA256)。
; =====================================================================

; ---- リリース毎に更新するパラメータ -------------------------------
#define MyAppName        "AutoEdit"
#define MyAppVersion     "1.0.2"
#define MyAppPublisher   "Evoluty Systems"
#define MyAppExeName     "AutoEdit.exe"

; GitHub Releases の配布元 (resolve8.md §4 / R5)
#define RepoOwner        "evolutysystems"
; 配布チャネル: payload を匿名DLさせるため public リポジトリ(autoedit)を使用する。
; (homepage は private でリリースアセットの匿名取得ができないため / 2026-07)
#define RepoName         "autoedit"
#define ReleaseTag       "v" + MyAppVersion
#define ReleaseBaseUrl   "https://github.com/" + RepoOwner + "/" + RepoName + "/releases/download/" + ReleaseTag

; 本体ペイロードのファイル名(カンマ区切り)。
;  - CPU版で 2GiB 未満に収まる場合は単一ファイル: "AutoEdit-v1.0.0.zip"
;  - 2GiB を超える場合は分割: "AutoEdit-v1.0.0.zip.001,AutoEdit-v1.0.0.zip.002"
;    (各 part は GitHub Releases の 2GiB 上限未満であること)
#define PayloadParts     "AutoEdit-v1.0.2.zip"

; 結合後zipの期待 SHA-256 (大文字16進・空文字なら検証スキップ)。
;  リリース時に Get-FileHash で取得して設定する (installer/README.md 参照)。
#define PayloadSHA256    "F15CFCDD5DA362B12C2450EB03BB9AD3E16ED7AEE2B06323384E1B8ACBDFB75B"
; -------------------------------------------------------------------

[Setup]
; AppId はアンインストール識別子。バージョン跨ぎで固定 (絶対に再利用・流用しないこと)
AppId={{8F3A1C2E-6B7D-4E91-A0C5-2D9F4B6E1A33}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 既定インストール先: ユーザー領域 (昇格不要・setting.json 書込が容易 / §9-5)
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; 64bit 環境ではインストーラ自身を 64bit で動作させる ({sys}\tar.exe を正しく解決するため)
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
; 生成されるインストーラ
OutputDir=Output
OutputBaseFilename=AutoEditSetup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; アンインストーラ
Uninstallable=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
; アイコン(gui/app.ico)は未配置のため SetupIconFile は省略 (§9-8)

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
; デスクトップショートカット作成の可否 (R4/§5.2)。既定でチェックON。
Name: "desktopicon"; Description: "デスクトップにショートカットを作成する"; GroupDescription: "追加タスク:"

[Icons]
; スタートメニュー(常時) と デスクトップ(タスク選択時)。
; WorkingDir={app} でログ等のカレント相対出力をインストール先基準に固定 (§5.2)
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; インストール完了後の起動オプション
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; [Code] で展開したファイルは Inno のアンインストールログに含まれないため、
; インストール先フォルダ一式を明示削除する。ユーザー指定の出力先(別フォルダ)は残す (§9-11)。
Type: filesandordirs; Name: "{app}"

[Code]
var
  OutputDirPage: TInputDirWizardPage;   { 編集動画の出力先を選ぶカスタムページ (R3) }
  DownloadPage: TDownloadWizardPage;     { ペイロードのダウンロード進捗ページ (R2) }

{ カンマ区切り文字列を配列へ分割する (空要素は除外) }
function SplitString(const S, Delim: String): TArrayOfString;
var
  tmp, part: String;
  p, n: Integer;
begin
  tmp := S;
  n := 0;
  SetArrayLength(Result, 0);
  repeat
    p := Pos(Delim, tmp);
    if p > 0 then
    begin
      part := Copy(tmp, 1, p - 1);
      tmp := Copy(tmp, p + Length(Delim), Length(tmp));
    end
    else
    begin
      part := tmp;
      tmp := '';
    end;
    part := Trim(part);
    if part <> '' then
    begin
      n := n + 1;
      SetArrayLength(Result, n);
      Result[n - 1] := part;
    end;
  until p = 0;
end;

{ 分割part群を1ファイルへバイナリ結合する (単一partでも可) }
function MergeFiles(const Parts: TArrayOfString; const OutFile: String): Boolean;
var
  outStream, inStream: TFileStream;
  i: Integer;
begin
  Result := False;
  try
    outStream := TFileStream.Create(OutFile, fmCreate);
    try
      for i := 0 to GetArrayLength(Parts) - 1 do
      begin
        inStream := TFileStream.Create(Parts[i], fmOpenRead);
        try
          { Count=0 でソース全体、第3引数 BufferSize は Inno Setup 6.7+ で必須 (推奨 $100000) }
          outStream.CopyFrom(inStream, 0, $100000);
        finally
          inStream.Free;
        end;
      end;
    finally
      outStream.Free;
    end;
    Result := True;
  except
    Log('ペイロード結合エラー: ' + GetExceptionMessage);
  end;
end;

{ zip を展開先へ解凍する。標準同梱の tar.exe を優先し、失敗時は PowerShell へフォールバック }
function ExtractZip(const ZipFile, DestDir: String): Boolean;
var
  rc: Integer;
  sysTar, psExe: String;
begin
  Result := False;

  { 第一候補: Windows 同梱の tar.exe (libarchive)。大容量zipを streaming 展開でき高速 }
  sysTar := ExpandConstant('{sys}\tar.exe');
  if FileExists(sysTar) then
  begin
    if Exec(sysTar, '-xf "' + ZipFile + '" -C "' + DestDir + '"', '',
            SW_HIDE, ewWaitUntilTerminated, rc) and (rc = 0) then
    begin
      Result := True;
      Exit;
    end
    else
      Log('tar.exe 展開失敗 (rc=' + IntToStr(rc) + ')。PowerShell へフォールバックします。');
  end;

  { フォールバック: PowerShell Expand-Archive (Windows 標準・追加依存なし) }
  psExe := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  if Exec(psExe,
          '-NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath ''' +
          ZipFile + ''' -DestinationPath ''' + DestDir + ''' -Force"',
          '', SW_HIDE, ewWaitUntilTerminated, rc) and (rc = 0) then
    Result := True
  else
    Log('PowerShell 展開失敗 (rc=' + IntToStr(rc) + ')。');
end;

{ setting.json の "output_directory" の値をユーザー選択パスへ書き換える (§6.2)。
  JSON パーサを持ち込まず、当該行の値(ダブルクォート間)のみを置換する堅牢な方式。
  プレースホルダ @@OUTPUT_DIR@@ でも実パスでも、現在値に関わらず置換できる。 }
function PatchOutputDir(const SettingsFile, OutDir: String): Boolean;
var
  lines: TArrayOfString;
  i, kp, r1, r2: Integer;
  line, rest, afterQ1, jsonPath, prefix, suffix: String;
  done: Boolean;
begin
  Result := False;
  if not FileExists(SettingsFile) then
  begin
    Log('setting.json が見つかりません: ' + SettingsFile);
    Exit;
  end;
  if not LoadStringsFromFile(SettingsFile, lines) then
  begin
    Log('setting.json の読み込みに失敗しました: ' + SettingsFile);
    Exit;
  end;

  { Windows パスの '\' を JSON で安全な '/' へ変換 (既存設定も '/' 区切り) }
  jsonPath := OutDir;
  StringChangeEx(jsonPath, '\', '/', True);

  done := False;
  for i := 0 to GetArrayLength(lines) - 1 do
  begin
    line := lines[i];
    kp := Pos('"output_directory"', line);
    if kp > 0 then
    begin
      { キー名以降の部分を取り出し、値の開始/終了クォートを特定する }
      rest := Copy(line, kp + Length('"output_directory"'), Length(line));
      r1 := Pos('"', rest);                          { 値の開始クォート }
      if r1 > 0 then
      begin
        afterQ1 := Copy(rest, r1 + 1, Length(rest));
        r2 := Pos('"', afterQ1);                      { 値の終了クォート }
        if r2 > 0 then
        begin
          { 開始クォートまでを前置、終了クォート以降(カンマ等)を後置として再構築 }
          prefix := Copy(line, 1, kp + Length('"output_directory"') - 1) + Copy(rest, 1, r1);
          suffix := Copy(afterQ1, r2, Length(afterQ1));
          lines[i] := prefix + jsonPath + suffix;
          done := True;
          Break;
        end;
      end;
    end;
  end;

  if done then
    Result := SaveStringsToFile(SettingsFile, lines, False)
  else
    Log('setting.json 内に output_directory が見つかりませんでした。');
end;

{ ダウンロード進捗コールバック (UI 用) }
function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  if ProgressMax > 0 then
    Log(Format('ダウンロード中 %s : %d / %d', [FileName, Progress, ProgressMax]));
  Result := True;  { True = 続行 }
end;

{ ウィザード初期化: 出力先ページとダウンロードページを準備する }
procedure InitializeWizard;
begin
  { 編集動画の出力先フォルダ選択 (R3/§5.1)。インストール先選択ページの直後に配置 }
  OutputDirPage := CreateInputDirPage(wpSelectDir,
    '編集した動画の出力先',
    '自動編集した動画の保存先フォルダを選んでください。',
    '自動編集した動画はこのフォルダに出力されます。' + #13#10 +
    '後からアプリの「設定」でも変更できます。' + #13#10#13#10 +
    '「次へ」をクリックして続行します。',
    False, '');
  OutputDirPage.Add('');
  { 既定は書込可能なユーザー領域 (§9-7) }
  OutputDirPage.Values[0] := ExpandConstant('{userdocs}\AutoEdit\output');

  { ペイロードのダウンロード進捗ページ }
  DownloadPage := CreateDownloadPage(
    SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), @OnDownloadProgress);
end;

{ [準備完了]→[次へ] でペイロードをダウンロードする (R2/§4.2)。
  ダウンロード失敗・ユーザー中止時は False を返しページに留まる (§10)。 }
function NextButtonClick(CurPageID: Integer): Boolean;
var
  parts: TArrayOfString;
  i: Integer;
begin
  Result := True;
  if CurPageID = wpReady then
  begin
    parts := SplitString('{#PayloadParts}', ',');
    DownloadPage.Clear;
    for i := 0 to GetArrayLength(parts) - 1 do
      { 第3引数(各partのSHA256)は空。検証は結合後zip全体で行う (§8) }
      DownloadPage.Add('{#ReleaseBaseUrl}/' + parts[i], parts[i], '');

    DownloadPage.Show;
    try
      try
        DownloadPage.Download;   { 失敗・中止時は例外送出 }
        Result := True;
      except
        if DownloadPage.AbortedByUser then
          Log('ダウンロードがユーザーにより中止されました。')
        else
          SuppressibleMsgBox(AddPeriod(GetExceptionMessage), mbCriticalError, MB_OK, IDOK);
        Result := False;
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

{ 指定インストール先ディレクトリ配下の setting.json のパスを返す }
function SettingsPathFor(const BaseDir: String): String;
begin
  Result := BaseDir + '\_internal\src\settings\setting.json';
end;

{ 更新インストール(既存 setting.json あり)では出力先選択ページをスキップする。
  既存の output_directory (ユーザー値) を維持するため (request_autoupdate.md §8.1)。 }
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if (OutputDirPage <> nil) and (PageID = OutputDirPage.ID) then
    Result := FileExists(SettingsPathFor(WizardDirValue));
end;

{ インストール直前: 結合→検証→展開→(新規)出力先パッチ/(更新)設定復元 を実施する (§4/§6)。
  非空文字列を返すとインストールを中止しメッセージ表示する。 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  parts, partPaths: TArrayOfString;
  i: Integer;
  mergedZip, appDir, settingsPath, backupPath, expectedHash, actualHash, outDir: String;
  isUpdate: Boolean;
begin
  Result := '';

  parts := SplitString('{#PayloadParts}', ',');
  SetArrayLength(partPaths, GetArrayLength(parts));
  for i := 0 to GetArrayLength(parts) - 1 do
    partPaths[i] := ExpandConstant('{tmp}\') + parts[i];

  mergedZip := ExpandConstant('{tmp}\AutoEdit-payload.zip');

  { 1) 分割partを結合 (単一partでも結合関数で1ファイル化) }
  if not MergeFiles(partPaths, mergedZip) then
  begin
    Result := 'ダウンロードしたファイルの結合に失敗しました。再実行してください。';
    Exit;
  end;

  { 2) SHA256 検証 (#define PayloadSHA256 指定時のみ。破損・改ざん検知 / §8) }
  expectedHash := Uppercase(Trim('{#PayloadSHA256}'));
  if expectedHash <> '' then
  begin
    actualHash := Uppercase(GetSHA256OfFile(mergedZip));
    if actualHash <> expectedHash then
    begin
      Log('SHA256 不一致: expected=' + expectedHash + ' actual=' + actualHash);
      Result := 'ダウンロードしたファイルが破損しています(ハッシュ不一致)。再実行してください。';
      Exit;
    end;
  end
  else
    Log('PayloadSHA256 が未設定のため整合性検証をスキップしました。');

  { 3) 更新判定 + 既存 setting.json の退避 (展開前)。
       更新時はユーザー設定を保持するため、展開で上書きされる前に退避する
       (request_autoupdate.md §8.1)。 }
  appDir := ExpandConstant('{app}');
  settingsPath := SettingsPathFor(appDir);
  backupPath := ExpandConstant('{tmp}\setting.user.json');
  isUpdate := FileExists(settingsPath);
  if isUpdate then
    if not FileCopy(settingsPath, backupPath, False) then
      Log('既存 setting.json の退避に失敗しました。設定が既定に戻る可能性があります。');

  { 4) インストール先へ展開 (onedir 構成をそのまま配置 / §4)。
       payload の既定 setting.json でユーザー設定が上書きされる。 }
  ForceDirectories(appDir);
  if not ExtractZip(mergedZip, appDir) then
  begin
    Result := 'インストールファイルの展開に失敗しました。空き容量・権限を確認してください。';
    Exit;
  end;

  { 5) 設定の確定 }
  if isUpdate then
  begin
    { 更新: 退避した既存 setting.json をそのまま復元し、既存値を一切上書きしない。
      新バージョンで増えた新規キーはアプリ起動時に _merge_with_defaults が補完する
      (request_autoupdate.md §8.2)。出力先パッチ・作成は行わない (既存値を維持)。 }
    if not FileCopy(backupPath, settingsPath, False) then
      Log('既存 setting.json の復元に失敗しました。設定が既定に戻る可能性があります。');
  end
  else
  begin
    { 新規インストール: output_directory をユーザー選択値へパッチし、出力先を作成する。
      失敗してもインストールは継続する (既定値のまま / §10)。 }
    outDir := OutputDirPage.Values[0];
    if not PatchOutputDir(settingsPath, outDir) then
      Log('setting.json のパッチに失敗しました。アプリの設定画面で出力先を変更してください。');
    if not ForceDirectories(outDir) then
      Log('出力先フォルダの作成に失敗しました: ' + outDir);
  end;
end;
