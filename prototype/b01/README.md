# B01 共享桌面界面开发预览

主路线：Qt6 / PySide6-Essentials **6.10.2**、shiboken6 **6.10.2**、独立 CPython **3.12.14** 开发环境。共用产品目标 **0.2.11**，候选 `b01-preview.1`，协议 **1.0**。这是项目内小型开发原型，不是 B09 独立运行包或 B12 安装器。旧 SwiftUI 0.2.10/build46 和 0.2.9/build35 不替换。

只接入能力/诊断、真实 worker 启动确认、有限无模型进度验证、操作/请求/租约身份、安全取消/尾部确认、退出和重新连接。其他功能在界面明确“尚未实现”，预览没有生产任务根目录或推理入口，也不连接 Ollama。合成步骤结果只用于验证进程生命周期，不是成功译文或保存恢复验收。

## 固定开发环境

`dependencies.lock.json` 固定两平台 wheel 的 HTTPS 来源、字节及 SHA-256。开发解释器须为明确已确认的本地 CPython3.12.14，不借任意全局/Conda 环境安装库；Windows解释器及工具尚待实机确认。`prepare_dev.py` 只创建仓库 `artifacts` 下**尚不存在**的新 venv，先验证全部 wheel 再安装。实际两份 Mac wheel 均未附 LICENSE/NOTICE/COPYING 文件（6.8.3和6.10.2均如此），不能称为已随包取得完整许可文本。已从Qt官方qtbase v6.10.2读取LGPL3、GPL3及GPL2参考文本并登记hash；模块/插件/第三方许可、对应源码、动态替换与重链接仍须B10对实际产物闭环。不能仅依据wheel metadata宣称整个Qt软件包已具备公共分发条件。

6.8.3候选在macOS原生辅助功能访问后退出崩溃，原环境和失败候选均保留。当前固定6.10.2的新环境；实际桌面交互和OS退出结果见[开发记录](../../docs/跨平台-B01开发记录.md)，不能只用窗口关闭事件宣称正常退出。

Mac本批现有环境在 `artifacts/b01-20261004/venv-qt6102`；从根目录运行：

```sh
artifacts/b01-20261004/venv-qt6102/bin/python -I -B prototype/b01/tests/test_protocol.py
artifacts/b01-20261004/venv-qt6102/bin/python -I -B prototype/b01/launch.py
```

如果重建环境，用已确认解释器运行下面脚本，目录必须为新的：

```sh
/已确认的CPython路径/python3.12 -I -B prototype/b01/prepare_dev.py --environment artifacts/b01-new/venv
```

构建在本平台生成独立候选与原型源码逐文件 hash，Mac从现有全部 marker/Info.plist查最高 build 后占号，失败也不复用；Windows单独记时间身份：

```sh
artifacts/b01-20261004/venv-qt6102/bin/python -I -B prototype/b01/build_preview.py --output dist/B01-新候选目录
```

Mac生成独立 `.app` 与 arm64 原生启动器，固定本批 venv路径，临时签名并严格核验。该 `.app` 仍依赖受控开发环境，不能移到其他机器称为独立产品。Windows生成源码/字节码检查及绑定本地解释器的 `Launch-B01.cmd`；不是独立 EXE/安装器。

## Windows接续

Windows当前离线，**不假定** `C:\Projects\Local-translator` 存在。先在正常登录后确认原生主机、真实项目根、同一仓库及 `codex/cross-platform-b01` 接续分支；保存两端修改，串行交接。Mac新资料须经本批源码提交/本地交接包或已授权Git接续实际到位，SSH连通不会自动复制。不得reset/强推、自动合main、查询远端SHA，或同步模型、任务DB、录音和详细日志。

`Check-Windows-B01.ps1` 在原生Windows只读核对本地源码/分支/仓库及最新任务资料，读取CPU/RAM/物理盘/各卷剩余空间/GPU/驱动/工具；专用显存只使用nvidia-smi的实际查询，缺失为未验证。脚本将私有路径和完整硬件结果存入忽略目录，不修改连接服务/SSH Shell/权限/登录启动项。确认变量后在原生PowerShell运行：

```powershell
& (Join-Path $B01ProjectRoot 'prototype\b01\Check-Windows-B01.ps1') `
  -ProjectRoot $B01ProjectRoot -ExpectedSourceCommit $B01SourceCommit
Set-Location -LiteralPath $B01ProjectRoot
& $B01PythonExe -I -B prototype/b01/prepare_dev.py --environment artifacts/b01-windows/venv
& .\artifacts\b01-windows\venv\Scripts\python.exe -I -B prototype/b01/tests/test_protocol.py
& .\artifacts\b01-windows\venv\Scripts\python.exe -I -B prototype/b01/build_preview.py --output dist/B01-Windows-新候选
```

`$B01ProjectRoot`、`$B01SourceCommit`、`$B01PythonExe`必须来自真实开工核对，不从Mac路径推导。硬件查询不等于CUDA通过。原生Windows构建/命令检查可由SSH执行；GUI须在实际登录桌面启动该候选，验证窗口、导航、开始/取消、停止/重连、退出无本批worker残留。可通过 `launch.py --evidence 本地输出 --screenshot 本地PNG` 留下候选/Qt/QPA/事件/退出信息，再补实际桌面交互证据；offscreen图或Mac图片不算Windows证据。原有Codex服务不属于本批清理对象。

## 协议与夹具

协议见[本地worker协议](../../docs/跨平台-B01本地worker协议.md)。`translator_preview/fixtures.py`只读核对 `fixtures/b01` 的原字节/hash、schema1 envelope/base64/checksum、source、成功前缀和两类recipe binding，不迁移或重写旧任务。Swift夹具由现有生产 `JSONOutput`、`TaskRecoveryStore`生成，全部为自编材料；对应完整Swift回归包含固定字节复验。Python字幕夹具由生产`translation_config`生成，SQLite仍schema3，B01未实现跨平台读写数据库。

本批交付后停止。B02必须另行启动，先补B01 Windows实际源码/依赖/构建/桌面证据，再按上批记录推进共享文字规则。
