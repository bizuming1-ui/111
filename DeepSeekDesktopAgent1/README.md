# DeepSeekDesktopAgent（D:\DeepSeekDesktopAgent）

在 PowerShell 7 里用自然语言控制 Windows 桌面。
`PowerShell(ai)` → `agent.py`（Python：模型循环 + 授权 + 验证）→ `agent-desktop.exe`（Rust：Windows UI Automation）→ 真实应用。

## 目录
```
D:\DeepSeekDesktopAgent
├─ agent.py              入口：模型调用、任务循环、执行回执、--doctor、--verify-audit
├─ da_core.py            agent-desktop 调用器、统一错误信封、哈希链审计日志
├─ da_policy.py          任务授权（意图/否定词/高风险/密码框/应用白名单）
├─ da_tools.py           11 个工具；每个副作用动作都带“动作后独立读回”
├─ requirements.txt
├─ ps\DeskAgent.ps1      定义 ai / ai-key / ai-stop / ai-doctor / ai-test / ai-audit / ai-verify / ai-voice
├─ ps\Setup.ps1          一键部署
├─ ps\Verify-Independent.ps1   C#(Win32)+UIA 独立复核
├─ tests\                离线回归测试 + agent-desktop 替身
├─ logs\audit.jsonl      防篡改审计日志（自动生成；不含你输入的明文）
├─ logs\screens\         截图（带时间戳，不覆盖）
└─ secrets\deepseek.key  DPAPI 加密的 API Key（自动生成）
```

## 使用
```powershell
ai-key                                   # 只需一次
ai-doctor                                # 体检
ai -DryRun "打开记事本并输入 Hello"       # 干跑：不动桌面
ai "打开记事本并输入 Hello"               # 真执行，末尾有“执行回执”
ai "列出当前所有窗口"
ai                                       # 交互模式
ai-stop   /   ai-resume                  # 急停 / 解除（可在另一个 pwsh 窗口执行）
ai-verify -E2E                           # 第三方复核
```

## 安全模型（模型无法给自己授权）
1. **意图授权**：副作用工具必须能在你“原话”里找到对应意图；“不要点击…”会被识别为禁止（按分句，不跨句）。
2. **高风险控件**：删除/发送/付款/提交/安装/重启…（含所在对话框标题，如“确认删除”里的“确定”）一律不自动点击。
3. **密码框**：`secure` 状态或名称含 密码/password/PIN/CVV 的字段，拒绝写入，值也不返回给模型。
4. **应用白名单**：只能启动 notepad/calculator/paint/explorer/edge/chrome/vscode，不能传路径、不能开 shell。
5. **写前核对**：`set_value` 重新快照→核对选中标签→核对编辑器唯一→读取并比对原值→才写入；用 `--stdin` 传文本（不进进程参数）。
6. **熔断/步数/急停**：同一调用连续失败 3 次熔断；默认最多 40 步；STOP 文件随时停。

## 结果验证的三个层级（程序生成，不靠模型自述）
| 动作 | 验证证据 | effect_verified |
|---|---|---|
| launch | 独立 `list-windows` 枚举出新窗口 | true / null |
| set_value | 重新快照后 UIA 读回 value == 目标 | true / false |
| click | 提供 expect_text 时，点击后快照里出现该文字 | true / null |
| press | 前后控件树差异（ui_changed） | 永远 null（只算“已投递”） |
| screenshot | PNG 文件头+宽高与 CLI 报告一致 | artifact_verified |

`null` = 没有证据，不是失败也不是成功；回执会提示“请人工核对”。

## 已知边界（请知悉）
- 管理员权限的应用：从普通 pwsh 能读不能操作（UIPI）。请以同等权限启动 pwsh。
- UAC 弹窗/锁屏/登录界面属于安全桌面，无法自动化。
- agent-desktop 无头模式不支持 `type`；写文本只用 `set-value`（要求控件暴露 SetValue）。
- Windows 11 记事本会恢复上次会话内容；此时“预期原值”不符，Agent 会安全拒绝写入而不是覆盖你的文字。
- Chromium/Electron 应用首次读取需 10–25 秒才稳定，可让 Agent 用更长的 timeout_ms 重试。
