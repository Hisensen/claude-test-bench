#!/usr/bin/env python3
"""
Universal Test Bench Orchestrator
用法: python orchestrator.py "任务描述" [--max-iter N]
"""

import argparse, subprocess, json, sys, time, threading, webbrowser, re, shutil
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── 路径 ──────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent.absolute()
WORKSPACE      = BASE_DIR / "workspace"
PROJECT_DIR    = WORKSPACE / "project"
BRIEFS_DIR     = WORKSPACE / "briefs"
REPORTS_DIR    = WORKSPACE / "reports"
STATUS_FILE    = WORKSPACE / "run.json"
DASHBOARD_HTML = BASE_DIR / "dashboard" / "index.html"
DASHBOARD_PORT = 7788

# ── 状态管理 ──────────────────────────────────────────────────────────
_status_lock = threading.Lock()

def _write_raw(data: dict):
    with _status_lock:
        STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def read_status() -> dict:
    try:
        with _status_lock:
            return json.loads(STATUS_FILE.read_text())
    except:
        return {}

def patch_status(**kw):
    s = read_status()
    s.update(kw)
    _write_raw(s)

def add_event(msg: str, kind: str = "info"):
    s = read_status()
    s.setdefault("events", []).append({
        "t": datetime.now().strftime("%H:%M:%S"),
        "k": kind,
        "m": msg,
    })
    s["events"] = s["events"][-60:]
    _write_raw(s)

def patch_agent(agent_id: int, **kw):
    s = read_status()
    for a in s.get("agents", []):
        if a["id"] == agent_id:
            a.update(kw)
            break
    _write_raw(s)

def init_workspace(task: str, max_iter: int):
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    PROJECT_DIR.mkdir(parents=True)
    BRIEFS_DIR.mkdir(parents=True)
    REPORTS_DIR.mkdir(parents=True)
    _write_raw({
        "task":           task,
        "status":         "initializing",
        "iteration":      0,
        "max_iterations": max_iter,
        "project_type":   "",
        "started_at":     datetime.now().isoformat(),
        "agents":         [],
        "events":         [],
    })

# ── Dashboard HTTP 服务 ───────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/":
            body = DASHBOARD_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/status":
            body = STATUS_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

def start_dashboard():
    srv = HTTPServer(("localhost", DASHBOARD_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

# ── Claude 调用封装 ───────────────────────────────────────────────────
def call_claude(prompt: str, tools: str = "Read,Write,Bash,Edit",
                timeout: int = 600) -> str:
    try:
        r = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", tools],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(BASE_DIR),
        )
        return r.stdout
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        print("❌  找不到 claude CLI，请确认 Claude Code 已安装")
        sys.exit(1)

def extract_json(text: str) -> dict | None:
    """从文本中提取第一个 JSON 对象"""
    try:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    return None

# ── 阶段 1：任务分析 + 项目生成 ────────────────────────────────────────
def analyze_and_generate(task: str) -> dict:
    add_event("主CC：分析任务并生成项目…")
    patch_status(status="generating")

    prompt = f"""你是一名全栈工程师兼测试架构师。

## 用户任务
{task}

## 你需要完成的两件事

### 1. 生成项目
根据任务生成完整、可运行的项目代码，将所有文件写入目录：
{PROJECT_DIR}

要求：
- 代码完整，逻辑自洽，可以直接运行或在浏览器中打开
- 如果是网页，写单文件 index.html（内嵌 CSS/JS）
- 如果是 CLI 工具，写 main.py 或 main.js 等

### 2. 制定测试计划
将以下 JSON 写入文件 {WORKSPACE}/plan.json：

```json
{{
  "project_type": "web_app | cli | api | library",
  "description": "一句话描述项目",
  "how_to_run": "如何访问或运行项目（例：在浏览器打开 project/index.html）",
  "tech_stack": "技术栈（例：HTML/CSS/JS）",
  "agents": [
    {{
      "id": 1,
      "role": "角色名",
      "icon": "一个 emoji",
      "focus": "该 agent 测试的具体方向（详细）",
      "approach": "测试手段（代码审查 / 功能测试 / 用户体验 / 安全测试 / 破坏性测试 / 性能测试）"
    }}
  ]
}}
```

## agent 数量原则
- 简单项目（CLI/小工具）：2–3 个
- 中等项目（单页网页/小 API）：3–4 个
- 复杂项目（多功能网页/全栈）：4–6 个

必须包含"功能测试"角色，以及至少一个非功能测试角色（UX/安全/破坏性/性能）。
不同 agent 的 focus 不能重叠。

现在开始工作，先生成项目，再写 plan.json。
"""

    call_claude(prompt, tools="Read,Write,Bash,Edit", timeout=600)

    plan_file = WORKSPACE / "plan.json"
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text())
            add_event(f"主CC：项目生成完成（{plan.get('project_type', '?')}，{len(plan.get('agents', []))} 个测试角色）")
            return plan
        except Exception:
            pass

    # 解析失败时的保底计划
    add_event("主CC：plan.json 解析失败，使用默认 3-agent 计划", "warn")
    return {
        "project_type": "unknown",
        "description":  task,
        "how_to_run":   f"查看 {PROJECT_DIR}",
        "tech_stack":   "未知",
        "agents": [
            {"id": 1, "role": "功能测试员",   "icon": "🔍",
             "focus": "验证核心功能逻辑是否正确",          "approach": "代码审查+功能测试"},
            {"id": 2, "role": "用户体验测试员", "icon": "👤",
             "focus": "评估易用性和新手上手体验",           "approach": "用户体验"},
            {"id": 3, "role": "破坏性测试员",   "icon": "💥",
             "focus": "寻找边界案例、竞态条件和崩溃点",    "approach": "破坏性测试"},
        ],
    }

# ── 阶段 2：生成测试简报 ───────────────────────────────────────────────
def generate_brief(agent: dict, plan: dict) -> Path:
    """为单个 agent 生成测试简报，写入文件"""
    other_agents = [a for a in plan["agents"] if a["id"] != agent["id"]]

    prompt = f"""为以下测试角色生成一份详细的测试简报（Markdown 格式）。

## 项目信息
- 类型：{plan.get('project_type')}
- 简介：{plan.get('description')}
- 运行方式：{plan.get('how_to_run')}
- 技术栈：{plan.get('tech_stack', '未知')}
- 项目目录：{PROJECT_DIR}

## 该 Agent 信息
- ID：{agent['id']}
- 角色：{agent['role']}
- 测试重点：{agent['focus']}
- 测试手段：{agent['approach']}

## 其他 Agent（不要重复他们的工作）
{json.dumps(other_agents, ensure_ascii=False, indent=2)}

## 简报内容要求
1. **角色定位**：用 2-3 句话描述这个 agent 的视角和目标
2. **测试清单**：逐条列出可执行的测试项（至少 8 条，要具体）
3. **报告格式**：最终报告必须是以下 JSON 格式

```json
{{
  "agent": {agent['id']},
  "role": "{agent['role']}",
  "passed": true,
  "issues": [
    {{
      "description": "问题描述",
      "severity": "high | medium | low",
      "location": "涉及的代码位置或功能点",
      "fix_hint": "修复建议"
    }}
  ],
  "summary": "一句话总结"
}}
```

只输出简报内容，不要前言或额外解释。
"""

    brief_text = call_claude(prompt, tools="", timeout=120)
    brief_path = BRIEFS_DIR / f"agent_{agent['id']}.md"
    brief_path.write_text(brief_text, encoding="utf-8")
    return brief_path

# ── 阶段 3：运行单个 Agent ─────────────────────────────────────────────
def run_agent(agent: dict, plan: dict, iteration: int) -> dict:
    """在独立线程中调用 claude -p 运行测试 agent，同时实时轮询状态"""
    report_path     = REPORTS_DIR / f"report_{agent['id']}_iter{iteration}.json"
    status_path     = WORKSPACE   / f"agent_{agent['id']}_live.json"
    brief_path      = BRIEFS_DIR  / f"agent_{agent['id']}.md"

    if not brief_path.exists():
        patch_agent(agent["id"], status="failed", action="简报文件缺失")
        return {"agent": agent["id"], "passed": False, "issues": [], "summary": "brief missing"}

    # 清除上一轮残留
    for p in [report_path, status_path]:
        if p.exists():
            p.unlink()

    brief = brief_path.read_text(encoding="utf-8")

    runtime_note = f"""

---
## 运行时信息（第 {iteration} 轮）

- 项目目录：`{PROJECT_DIR}`
- 运行方式：{plan.get('how_to_run', '查看项目目录')}
- 报告路径：`{report_path}`
- 实时状态路径：`{status_path}`

## 实时状态更新规范
每完成一个测试项，立即将以下 JSON 写入实时状态文件（覆盖写入）：
```json
{{"action": "当前正在做的事情（一句话）", "issues_count": 当前已发现问题数}}
```

## 完成规范
全部测试结束后，将完整报告 JSON 写入报告路径，然后停止。
"""

    patch_agent(agent["id"], status="running", action="启动中…", issues_count=0)
    add_event(f"[Agent {agent['id']} · {agent['role']}] 开始测试")

    # 在内部线程调用 claude，主线程轮询状态文件
    done_event = threading.Event()

    def _invoke():
        call_claude(brief + runtime_note, tools="Read,Write,Bash,Edit", timeout=400)
        done_event.set()

    threading.Thread(target=_invoke, daemon=True).start()

    while not done_event.wait(timeout=1.5):
        if status_path.exists():
            try:
                s = json.loads(status_path.read_text())
                patch_agent(agent["id"],
                            action=s.get("action", "…"),
                            issues_count=s.get("issues_count", 0))
            except Exception:
                pass

    # 读取最终报告
    report = _parse_report(report_path, agent)
    passed = report.get("passed", False)
    issues = report.get("issues", [])

    patch_agent(agent["id"],
                status="passed" if passed else "failed",
                action="✅ 全部通过" if passed else f"❌ 发现 {len(issues)} 个问题",
                issues_count=len(issues),
                passed=passed,
                issues=issues,
                summary=report.get("summary", ""))

    add_event(
        f"[Agent {agent['id']}] {'通过' if passed else f'发现 {len(issues)} 个问题'}",
        "success" if passed else "error",
    )
    return report

def _parse_report(path: Path, agent: dict) -> dict:
    fallback = {"agent": agent["id"], "role": agent["role"],
                "passed": False, "issues": [], "summary": "报告解析失败"}
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text())
    except Exception:
        # 尝试从文件内容中提取 JSON
        raw = path.read_text()
        data = extract_json(raw)
        return data if data else fallback

# ── 阶段 4：并行运行所有 Agent ─────────────────────────────────────────
def run_all_agents(agents: list, plan: dict, iteration: int) -> list:
    reports, lock = [], threading.Lock()

    def _run(ag):
        r = run_agent(ag, plan, iteration)
        with lock:
            reports.append(r)

    threads = [threading.Thread(target=_run, args=(a,)) for a in agents]
    for t in threads: t.start()
    for t in threads: t.join()
    return reports

# ── 阶段 5：汇总修复 ───────────────────────────────────────────────────
def fix_issues(reports: list, plan: dict, iteration: int) -> bool:
    all_issues = []
    for r in reports:
        if not r.get("passed"):
            for iss in r.get("issues", []):
                all_issues.append({"from_agent": r.get("role", r.get("agent")), **iss})

    if not all_issues:
        return True

    add_event(f"主CC：汇总 {len(all_issues)} 个问题，开始修复…", "fix")
    patch_status(status="fixing")

    prompt = f"""你是一名资深工程师，负责修复项目中的 Bug。

项目目录：{PROJECT_DIR}
运行方式：{plan.get('how_to_run')}

## 测试报告（需要修复的问题）
{json.dumps(all_issues, ensure_ascii=False, indent=2)}

## 要求
1. 直接修改 {PROJECT_DIR} 中的文件，修复以上全部问题
2. 不要删除正常运行的功能
3. 修复完成后，输出：{{"fixed": true, "changes": ["修改1", "修改2"]}}
"""

    call_claude(prompt, tools="Read,Write,Edit,Bash", timeout=300)
    add_event(f"主CC：第 {iteration} 轮修复完成", "fix")
    patch_status(status="testing")
    return False

# ── 主流程 ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Universal Test Bench")
    ap.add_argument("task",       help="任务描述")
    ap.add_argument("--max-iter", type=int, default=5, dest="max_iter")
    args = ap.parse_args()
    task, max_iter = args.task, args.max_iter

    print(f"\n{'═'*55}")
    print(f"  🧪  Universal Test Bench")
    print(f"  任务: {task}")
    print(f"{'═'*55}")

    # 初始化
    init_workspace(task, max_iter)
    start_dashboard()
    time.sleep(0.3)
    url = f"http://localhost:{DASHBOARD_PORT}"
    print(f"\n  📊 Dashboard → {url}")
    webbrowser.open(url)
    time.sleep(0.5)

    # 阶段1：分析 + 生成
    plan = analyze_and_generate(task)

    # 注册 agents 到状态
    patch_status(
        status="briefing",
        project_type=plan.get("project_type", ""),
        agents=[{
            "id":           a["id"],
            "role":         a["role"],
            "icon":         a.get("icon", "🤖"),
            "focus":        a.get("focus", ""),
            "status":       "waiting",
            "action":       "等待简报生成…",
            "issues_count": 0,
            "passed":       None,
            "issues":       [],
            "summary":      "",
        } for a in plan["agents"]],
    )

    # 阶段2：并行生成简报
    add_event("主CC：为各 Agent 生成测试简报…")
    brief_threads = [threading.Thread(target=generate_brief, args=(a, plan)) for a in plan["agents"]]
    for t in brief_threads: t.start()
    for t in brief_threads: t.join()
    add_event("主CC：简报生成完毕，开始测试")

    # 阶段3–5：迭代测试循环
    all_passed = False
    for iteration in range(1, max_iter + 1):
        patch_status(iteration=iteration, status="testing")
        add_event(f"━━━  第 {iteration}/{max_iter} 轮测试  ━━━")

        # 重置 agent 状态
        s = read_status()
        for a in s["agents"]:
            a.update({"status": "waiting", "action": "准备中…", "passed": None})
        _write_raw(s)

        reports = run_all_agents(plan["agents"], plan, iteration)

        n_passed = sum(1 for r in reports if r.get("passed"))
        n_total  = len(reports)
        add_event(f"第 {iteration} 轮结果：{n_passed}/{n_total} 通过",
                  "success" if n_passed == n_total else "warn")

        if n_passed == n_total:
            all_passed = True
            break

        if iteration < max_iter:
            done = fix_issues(reports, plan, iteration)
            if done:
                all_passed = True
                break
        else:
            add_event(f"已达最大迭代次数 ({max_iter})", "warn")

    # 完成
    final_status = "completed" if all_passed else "timeout"
    patch_status(status=final_status)
    if all_passed:
        add_event("🎉  所有测试通过！", "success")
        print("\n  🎉  所有测试通过！")
    else:
        add_event("⚠️  部分测试未通过，请查看 Dashboard", "warn")
        print("\n  ⚠️  部分测试未通过")

    print(f"  📊  Dashboard 保持运行 → {url}")
    print("  按 Ctrl+C 退出\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("  👋  Bye!\n")

if __name__ == "__main__":
    main()
