用户正在调用 Test Bench —— 一个多智能体自动化测试平台。

## 你的职责

运行以下命令启动 Test Bench，然后告知用户 Dashboard 已经在浏览器中打开，让他们可以实时观察测试进展：

```bash
cd /Users/macbookpro/Desktop/claude_project/test_bench
python orchestrator.py "$ARGUMENTS"
```

## 运行后的操作

1. Dashboard 会自动在浏览器打开（http://localhost:7788）
2. 告知用户：Dashboard 正在实时显示各 Agent 的测试进度
3. 等待命令执行完毕（可能需要几分钟）
4. 完成后汇报测试结果摘要

## 注意事项

- 如果用户没有提供任务描述（$ARGUMENTS 为空），询问用户想要构建和测试什么
- 如果命令报错找不到 claude，提示用户检查 Claude Code 是否已正确安装
- orchestrator 会阻塞运行直到测试完成，这是正常的
