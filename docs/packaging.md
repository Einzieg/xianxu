# Windows 安装包

按首页准备环境并执行 `uv sync --locked` 后：

```powershell
$ErrorActionPreference = 'Stop'
.\Build-Installer.ps1
```

PyInstaller 打包 Python 引擎、模型及依赖，Tauri NSIS 生成 x64 安装器，输出在 `.build/installer-target/release/bundle/nsis/`。每次使用独立暂存目录，普通版不混入之前的 DD 文件。构建过程不安装驱动，也不覆盖正在运行的工作区播放器。

安装包运行不需要 Python/Node/Rust。系统需要 Windows 10/11 x64 和 WebView2；Tauri 安装器使用默认 WebView2 引导。per-machine 安装会请求管理员权限。升级、卸载前须先停止演奏并正常退出程序。

## 含 DD 的构建

仅当你已确认具备适用的再分发权时：

```powershell
$ErrorActionPreference = 'Stop'
.\Build-Installer.ps1 -DDPackagePath '.\2026.DD.EV.HVCI.63xxx\2.hid' -ConfirmDDRedistributionRights
```

必需文件：`ddhid.63340.dll`、`drv/ddc.exe`、`drv/ddhid63340.inf`、`drv/ddhid63340.sys`、`drv/ddhid63340.cat`。脚本校验文件及 DLL、安装器、CAT 的 Authenticode 状态，不修改签名文件。

安装阶段自动在正确目录调用厂商 `ddc.exe`，收集输出，检查返回码与服务注册。3010表示需要重启；失败显示警告，不假称驱动已可用。应用卸载不会移除共享驱动，不关闭内存完整性、安全启动、签名检查。

当前官方仓库未附 MIT LICENSE；如果使用其他明确授权的版本，应随发布包保留许可与来源，详见 [第三方说明](../THIRD_PARTY_NOTICES.md)。

## 发布前验证

1. 运行 Python/前端测试及 Rust 检查。
2. 复制独立打包目录到不含源码、`.venv` 的路径，使用独立数据目录执行 `ROCK_MUSIC_SMOKE=1`；不调用真实播放。
3. 在干净 Windows 虚拟机测试安装、升级、卸载、WebView2、取消 UAC、DD 安装失败和重启场景。
4. 未做干净虚拟机测试前，不声称完整安装验收通过。未签名的应用安装器可能触发 SmartScreen；DD 厂商签名不能替代应用签名。
