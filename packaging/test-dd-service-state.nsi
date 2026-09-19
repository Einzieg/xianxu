; Read-only probe: never invokes NSIS_HOOK_POSTINSTALL or the driver installer.
Unicode true
RequestExecutionLevel user
SilentInstall silent
OutFile "..\.build\test-dd-service-state.exe"
!include "installer-hooks.nsh"
Function .onInit
  Call XianxuDDServiceState
  Pop $0
  SetErrorLevel $0
  Quit
FunctionEnd
Section
SectionEnd
