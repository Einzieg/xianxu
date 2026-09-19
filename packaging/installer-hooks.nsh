; Only an explicitly provisioned, redistribution-authorized DD edition contains
; this directory. The ordinary public installer never downloads a driver.
!macro NSIS_HOOK_POSTINSTALL
  IfFileExists "$INSTDIR\drivers\dd\drv\ddc.exe" 0 xianxu_dd_done
  SetOutPath "$INSTDIR\drivers\dd\drv"
  DetailPrint "Installing the bundled DD HID driver (administrator permission required)..."
  nsExec::ExecToLog /TIMEOUT=120000 '"$INSTDIR\drivers\dd\drv\ddc.exe"'
  Pop $0
  StrCmp $0 "0" xianxu_dd_check
  StrCmp $0 "3010" xianxu_dd_reboot
  MessageBox MB_OK|MB_ICONEXCLAMATION "DD installer returned $0. The application is installed, but DD may be unavailable. Review the installation log or use Win32 input."
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
