用户正在调用 Test Bench —— 一个自动化代码生成 + 测试平台。

## 工作原理

- **CLI 项目**（Python 等）：生成代码 → 自动写 pytest → 跑 → 失败让 AI 修 → 循环
- **Web 项目**（HTML 单页应用）：生成代码 → AI 清点特征 → 矩阵展开测试用例 → 程序化探测真实 DOM → AI 一次性生成 pytest-playwright 脚本 → 跑 → 失败按类型修（脚本错改 spec / 真 bug 改项目，带快照回滚保护）

整个流程零人工介入。AI 只负责"设计"（一次性写代码 + 一次性写测试 + 按需修复），执行交给脚本/pytest。

## 你的职责

运行以下命令启动 Test Bench：

```bash
cd /Users/macbookpro/Desktop/claude_project/test_bench
python orchestrator.py "$ARGUMENTS"
```

## 运行后的操作

1. Dashboard 会自动在浏览器打开（http://localhost:7788）
2. 告知用户：Dashboard 正在实时显示各阶段进度（清点 → 探测 → 生成脚本 → 跑测试 → 修复）
3. 等待命令执行完毕（CLI 项目约 2-3 分钟，Web 项目约 3-5 分钟）
4. 完成后汇报测试结果摘要：通过/失败用例数、关键问题、最终是否全绿

## 注意事项

- 如果用户没有提供任务描述（$ARGUMENTS 为空），询问用户想要构建和测试什么
- 如果命令报错找不到 claude，提示用户检查 Claude Code 是否已正确安装
- 如果报错缺 playwright，提示运行：`pip install pytest-playwright && playwright install chromium`
- orchestrator 会阻塞运行直到测试完成，这是正常的；推荐用后台 Bash + run_in_background=true，避免阻塞对话
