; Only an explicitly provisioned, redistribution-authorized DD edition contains
; this directory. The ordinary public installer never downloads a driver.
Function XianxuDDServiceState
  Push $R9
  Push $0
  Push $1
  Push $2
  Push $3
  StrCpy $3 0
  System::Call 'advapi32::OpenSCManagerW(p 0, p 0, i 1) p .r0'
  IntCmp $0 0 xianxu_state_done
  System::Call 'advapi32::OpenServiceW(p r0, w "ddhid63340", i 4) p .r1'
  IntCmp $1 0 xianxu_state_close_manager
  System::Alloc 28
  Pop $2
  IntCmp $2 0 xianxu_state_close_service
  System::Call 'advapi32::QueryServiceStatus(p r1, p r2) i .r3'
  IntCmp $3 0 xianxu_state_free
  ; SERVICE_STATUS.dwCurrentState is the second DWORD.
  System::Call '*$2(i, i .r3)'
xianxu_state_free:
  System::Free $2
xianxu_state_close_service:
  System::Call 'advapi32::CloseServiceHandle(p r1)'
xianxu_state_close_manager:
  System::Call 'advapi32::CloseServiceHandle(p r0)'
xianxu_state_done:
  StrCpy $R9 $3
  Pop $3
  Pop $2
  Pop $1
  Pop $0
  Exch $R9
FunctionEnd

!macro NSIS_HOOK_POSTINSTALL
  IfFileExists "$INSTDIR\drivers\dd\drv\ddc.exe" 0 xianxu_dd_done
  Call XianxuDDServiceState
  Pop $0
  ; Do not remove/recreate a driver which is already loaded.
  StrCmp $0 "4" xianxu_dd_existing
  ReadRegStr $1 HKLM "SYSTEM\CurrentControlSet\Services\ddhid63340" "ImagePath"
  StrCmp $1 "" xianxu_dd_install
  DetailPrint "DD is already registered. Automatic reinstallation skipped."
  MessageBox MB_OK|MB_ICONINFORMATION "DD is already registered but is not running. Automatic reinstallation was skipped to avoid interrupting an existing device operation. Finish setup and restart Windows before checking DD."
  Goto xianxu_dd_done
xianxu_dd_existing:
  DetailPrint "DD ddhid63340 is already running. Reusing the existing driver."
  Goto xianxu_dd_done
xianxu_dd_install:
  SetOutPath "$INSTDIR\drivers\dd\drv"
  DetailPrint "Installing the bundled DD HID driver (administrator permission required)..."
  nsExec::ExecToLog /TIMEOUT=120000 '"$INSTDIR\drivers\dd\drv\ddc.exe"'
  Pop $0
  StrCmp $0 "timeout" xianxu_dd_timeout
  StrCmp $0 "0" xianxu_dd_check
  StrCmp $0 "3010" xianxu_dd_reboot
  MessageBox MB_OK|MB_ICONEXCLAMATION "DD installer returned $0. The application is installed, but DD may be unavailable. Review the installation log or use Win32 input."
  Goto xianxu_dd_done
xianxu_dd_timeout:
  SetRebootFlag true
  MessageBox MB_OK|MB_ICONEXCLAMATION "DD setup timed out. The application files are installed. Do not rerun DD setup immediately: finish setup, save your work, and restart Windows before checking the driver."
  Goto xianxu_dd_done
xianxu_dd_reboot:
  SetRebootFlag true
xianxu_dd_check:
  ReadRegStr $1 HKLM "SYSTEM\CurrentControlSet\Services\ddhid63340" "ImagePath"
  StrCmp $1 "" 0 xianxu_dd_done
  MessageBox MB_OK|MB_ICONEXCLAMATION "DD service registration was not found. Use Win32 input or repair the driver installation."
xianxu_dd_done:
  SetOutPath "$INSTDIR"
!macroend

; Never uninstall the shared system driver automatically with the application.
