#!/usr/bin/env python3
"""
Universal Test Bench Orchestrator
用法: python orchestrator.py "任务描述" [--max-iter N]
"""

import argparse, subprocess, json, sys, time, threading, webbrowser, re, shutil, socket
from functools import partial
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler, SimpleHTTPRequestHandler

# ── 路径 ──────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent.absolute()
WORKSPACE      = BASE_DIR / "workspace"
PROJECT_DIR    = WORKSPACE / "project"
BRIEFS_DIR     = WORKSPACE / "briefs"
REPORTS_DIR    = WORKSPACE / "reports"
STATUS_FILE    = WORKSPACE / "run.json"
DASHBOARD_HTML = BASE_DIR / "dashboard" / "index.html"
DASHBOARD_PORT = 7788
PROJECT_PORT   = 8765
PROJECT_URL    = f"http://localhost:{PROJECT_PORT}"

# CLI 项目类型（走 pytest 路径，不派 AI Agent）
CLI_TYPES = {"cli_tool", "cli", "python_cli", "script", "python_script", "python"}

# 子 Agent 工具权限（Web 路径专用）
SUBAGENT_TOOLS = "Read,Write,Bash,Edit,mcp__playwright__*"

# ── 状态管理 ──────────────────────────────────────────────────────────
_status_lock = threading.Lock()

def _write_raw(data: dict):
    with _status_lock:
        STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def read_status() -> dict:
    try:
        with _status_lock:
            return json.loads(STATUS_FILE.read_text())
    except Exception:
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

class _ReuseHTTPServer(HTTPServer):
    allow_reuse_address = True
    def server_bind(self):
        if hasattr(socket, "SO_REUSEPORT"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        super().server_bind()

def start_dashboard():
    srv = _ReuseHTTPServer(("localhost", DASHBOARD_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

# ── 项目 HTTP 服务（Web 路径专用）────────────────────────────────────
class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

def start_project_server():
    handler = partial(_QuietStaticHandler, directory=str(PROJECT_DIR))
    try:
        srv = _ReuseHTTPServer(("localhost", PROJECT_PORT), handler)
    except OSError:
        add_event(f"项目服务器端口 {PROJECT_PORT} 被占用", "warn")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    add_event(f"项目服务器启动：{PROJECT_URL}")
    return srv

# ── Claude 调用封装 ───────────────────────────────────────────────────
def call_claude(prompt: str, tools: str = "Read,Write,Bash,Edit",
                timeout: int = 600) -> str:
    try:
        cmd = ["claude", "-p", prompt]
        if tools:
            cmd += ["--allowedTools", tools]
        # cwd 设为 PROJECT_DIR，防止子 Claude 发现并误执行 orchestrator.py
        cwd = str(PROJECT_DIR) if PROJECT_DIR.exists() else str(BASE_DIR)
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        return r.stdout
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        print("❌  找不到 claude CLI，请确认 Claude Code 已安装")
        sys.exit(1)

def extract_json(text: str) -> dict | None:
    try:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    return None

# ── 阶段 1a：生成项目代码 ─────────────────────────────────────────────
def generate_project(task: str):
    add_event("主CC：生成项目代码…")
    prompt = f"""任务：{task}

将完整可运行的项目代码写入目录 {PROJECT_DIR}。

规则：
1. 网页类必须是单文件 index.html（CSS/JS 全部内嵌，禁止外部引用）
2. CLI 工具写单文件 main.py 或 main.js
3. 代码必须完整，不允许 TODO 或占位符
4. 不得引用 {PROJECT_DIR} 目录之外的资源

写完后输出：PROJECT_GENERATED"""
    call_claude(prompt, tools="Write,Bash,Edit,Read", timeout=600)
    add_event("主CC：代码写入完成")

# ── 阶段 1b：分析项目类型 ──────────────────────────────────────────────
def plan_project(task: str) -> dict:
    add_event("主CC：分析项目类型…")
    # 直接读文件判断类型，不额外调用 Claude
    files = list(PROJECT_DIR.iterdir()) if PROJECT_DIR.exists() else []
    exts  = {f.suffix for f in files}

    if ".html" in exts or ".htm" in exts:
        project_type = "web_app"
    elif ".py" in exts:
        project_type = "cli_tool"
    elif ".js" in exts and ".html" not in exts:
        project_type = "cli_tool"
    else:
        project_type = "unknown"

    # 读取主文件内容作为描述
    main_file = next((f for f in files if f.stem in ("main", "index", "app")), None)
    description = task

    plan = {
        "project_type": project_type,
        "description":  description,
        "how_to_run":   _how_to_run(project_type, files),
        "tech_stack":   _tech_stack(exts),
        "agents":       _default_web_agents() if project_type not in CLI_TYPES else [],
    }
    (WORKSPACE / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    add_event(f"主CC：项目类型 → {project_type}")
    return plan

def _how_to_run(project_type: str, files: list) -> str:
    if project_type == "web_app":
        return f"在浏览器打开 {PROJECT_URL}"
    py = next((f for f in files if f.suffix == ".py"), None)
    return f"python {py.name}" if py else "查看项目目录"

def _tech_stack(exts: set) -> str:
    parts = []
    if ".py"   in exts: parts.append("Python")
    if ".html" in exts: parts.append("HTML/CSS/JS")
    if ".js"   in exts and ".html" not in exts: parts.append("Node.js")
    return ", ".join(parts) or "未知"

def _default_web_agents() -> list:
    return [
        {"id": 1, "role": "功能测试员",  "icon": "🔍",
         "focus": "核心功能逻辑", "approach": "真实交互操作"},
        {"id": 2, "role": "UI 测试员",   "icon": "🎨",
         "focus": "视觉与交互体验", "approach": "Playwright 截图对比"},
        {"id": 3, "role": "破坏性测试员","icon": "💥",
         "focus": "边界与异常", "approach": "极端输入+竞态"},
    ]

def analyze_and_generate(task: str) -> dict:
    patch_status(status="generating")
    generate_project(task)
    patch_status(status="planning")
    return plan_project(task)

# ══════════════════════════════════════════════════════════════════════
# CLI 路径：生成 pytest → 直接跑 → 修复循环
# ══════════════════════════════════════════════════════════════════════

def run_cli_loop(plan: dict, max_iter: int) -> bool:
    """CLI 项目：生成 pytest 测试文件，循环跑 + 修复，无需 AI Agent。"""
    # 注册单个虚拟 agent 用于 Dashboard 显示
    patch_status(
        project_type=plan["project_type"],
        agents=[{
            "id": 1, "role": "pytest 自动测试", "icon": "🧪",
            "focus": "自动化单元/集成/边界测试", "status": "waiting",
            "action": "生成测试文件…", "issues_count": 0,
            "passed": None, "issues": [], "summary": "",
        }],
    )

    # 读项目代码（嵌入提示词，省去子进程重复读文件）
    py_files = list(PROJECT_DIR.glob("*.py"))
    code_snippets = ""
    for f in py_files:
        code_snippets += f"\n### {f.name}\n```python\n{f.read_text()}\n```\n"

    test_path = PROJECT_DIR / "test_main.py"

    add_event("主CC：生成 pytest 测试文件…")
    patch_agent(1, status="running", action="生成 pytest 测试…")

    gen_prompt = f"""为以下 Python 项目写一个完整的 pytest 测试文件，写入 {test_path}。

{code_snippets}

要求：
- 覆盖正常用例、边界用例、异常/错误用例
- 至少 15 个测试函数
- 只写测试代码，不要注释或说明
- 直接写文件到 {test_path}"""
    call_claude(gen_prompt, tools="Write", timeout=180)
    add_event("主CC：测试文件生成完毕，开始运行")

    all_passed = False
    for iteration in range(1, max_iter + 1):
        patch_status(iteration=iteration, status="testing")
        add_event(f"━━━ 第 {iteration}/{max_iter} 轮 pytest ━━━")
        patch_agent(1, status="running", action=f"运行 pytest（第 {iteration} 轮）…")

        result = subprocess.run(
            ["python", "-m", "pytest", str(test_path), "-v", "--tb=short", "--no-header"],
            capture_output=True, text=True, cwd=str(PROJECT_DIR),
        )
        output = result.stdout + result.stderr

        # 保存原始输出
        (REPORTS_DIR / f"pytest_iter{iteration}.txt").write_text(output)

        # 解析结果
        passed_n  = int(m.group(1)) if (m := re.search(r'(\d+) passed', output)) else 0
        failed_n  = int(m.group(1)) if (m := re.search(r'(\d+) failed', output)) else 0
        error_n   = int(m.group(1)) if (m := re.search(r'(\d+) error',  output)) else 0
        total_bad = failed_n + error_n

        if result.returncode == 0:
            patch_agent(1, status="passed",
                        action=f"✅ {passed_n} 个测试全部通过",
                        issues_count=0, passed=True,
                        summary=f"{passed_n} 个测试通过")
            add_event(f"🎉 pytest：{passed_n} 个测试全部通过", "success")
            all_passed = True
            break

        issues = _parse_pytest_failures(output)
        patch_agent(1, status="failed",
                    action=f"❌ {total_bad} 个测试失败",
                    issues_count=total_bad, passed=False,
                    issues=issues,
                    summary=f"{passed_n} 通过，{total_bad} 失败")
        add_event(f"pytest：{passed_n} 通过，{total_bad} 失败", "error")

        if iteration >= max_iter:
            add_event(f"已达最大迭代次数 ({max_iter})", "warn")
            break

        add_event("主CC：分析失败原因，修复代码…", "fix")
        patch_status(status="fixing")

        # 重新读最新代码嵌入修复提示（避免子进程读文件）
        current_code = ""
        for f in PROJECT_DIR.glob("*.py"):
            if f.name != "test_main.py":
                current_code += f"\n### {f.name}\n```python\n{f.read_text()}\n```\n"

        fix_prompt = f"""修复以下 Python 项目中的 bug，使 pytest 全部通过。

当前代码：
{current_code}

pytest 失败输出（最近 3000 字符）：
{output[-3000:]}

要求：
- 只修改项目源文件（不要修改 test_main.py）
- 直接写回 {PROJECT_DIR} 中对应的 .py 文件"""
        call_claude(fix_prompt, tools="Write,Edit,Read", timeout=300)
        add_event(f"主CC：第 {iteration} 轮修复完成", "fix")
        patch_status(status="testing")

    return all_passed

def _parse_pytest_failures(output: str) -> list:
    issues = []
    for block in re.split(r'_{10,}', output):
        m = re.search(r'FAILED (.+?) -', block)
        if not m:
            m = re.search(r'ERROR (.+)', block)
        if m:
            name = m.group(1).strip()
            err  = re.search(r'(AssertionError|ValueError|TypeError|[\w]+Error)[^\n]*', block)
            issues.append({
                "description": name,
                "severity":    "high",
                "location":    name.split("::")[0] if "::" in name else "test_main.py",
                "fix_hint":    err.group(0)[:120] if err else "",
            })
    return issues[:20]

# ══════════════════════════════════════════════════════════════════════
# Web 路径：多 Agent（模板简报，无独立简报调用）
# ══════════════════════════════════════════════════════════════════════

# 各角色简报模板（省去简报生成的 AI 调用）
_WEB_BRIEF_TEMPLATES = {
    "功能测试员": "验证所有核心功能在正常输入下工作正确。测试每个按钮/交互/计算结果。",
    "UI 测试员":  "检查视觉布局、响应式、动画、颜色对比度、文字可读性。截图记录视觉问题。",
    "破坏性测试员": "输入极端/非法值，快速点击，多次操作，尝试触发崩溃或异常状态。",
}

def _make_web_brief(agent: dict, plan: dict) -> str:
    role     = agent["role"]
    focus    = agent.get("focus", _WEB_BRIEF_TEMPLATES.get(role, agent.get("approach", "")))
    template = _WEB_BRIEF_TEMPLATES.get(role, focus)
    report_path = REPORTS_DIR / f"report_{agent['id']}_iter__ITER__.json"

    return f"""# 测试简报：{role}（Agent #{agent['id']}）

## 角色定位
你是 {role}，负责从「{focus}」角度测试以下项目。
{template}

## 项目信息
- URL：{PROJECT_URL}
- 目录：{PROJECT_DIR}
- 运行方式：{plan.get('how_to_run', '')}

## 测试清单（逐项完成）
1. 打开页面，截图确认加载正常
2. 测试所有主要交互（按钮、输入框、菜单等）
3. 验证核心功能的输出结果正确
4. 检查控制台是否有报错
5. 测试边界情况（空输入、极大值、特殊字符）
6. 检查页面在不同操作后的状态一致性
7. 验证错误提示是否友好
8. 额外关注：{focus}

## 可用 Playwright 工具
- browser_navigate → 打开 {PROJECT_URL}
- browser_snapshot → 获取页面 DOM 结构
- browser_click / browser_type / browser_press_key → 真实交互
- browser_take_screenshot → 截图存证
- browser_console_messages → 看控制台报错
- browser_evaluate → 执行 JS

## 实时状态
每完成一项，写入 {WORKSPACE}/agent_{agent['id']}_live.json：
{{"action": "当前操作描述", "issues_count": N}}

## 最终报告（必须完成）
测试完毕后将以下 JSON 写入报告文件（路径见运行时注入）：
{{"agent":{agent['id']},"role":"{role}","passed":true/false,"issues":[{{"description":"","severity":"high/medium/low","location":"","fix_hint":""}}],"summary":""}}
"""

def run_agent(agent: dict, plan: dict, iteration: int) -> dict:
    report_path = REPORTS_DIR / f"report_{agent['id']}_iter{iteration}.json"
    status_path = WORKSPACE   / f"agent_{agent['id']}_live.json"

    for p in [report_path, status_path]:
        if p.exists():
            p.unlink()

    brief = _make_web_brief(agent, plan)
    runtime_note = f"""
---
## 运行时（第 {iteration} 轮）
- 报告路径：`{report_path}`
- 实时状态：`{status_path}`

⚠️ 测试结束前必须将完整报告 JSON 写入报告路径，否则本轮作废。
"""
    patch_agent(agent["id"], status="running", action="启动中…", issues_count=0)
    add_event(f"[Agent {agent['id']} · {agent['role']}] 开始测试")

    done_event = threading.Event()
    def _invoke():
        call_claude(brief + runtime_note, tools=SUBAGENT_TOOLS, timeout=600)
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

    report = _parse_report(report_path, agent)
    passed = report.get("passed", False)
    issues = report.get("issues", [])
    patch_agent(agent["id"],
                status="passed" if passed else "failed",
                action="✅ 全部通过" if passed else f"❌ 发现 {len(issues)} 个问题",
                issues_count=len(issues), passed=passed,
                issues=issues, summary=report.get("summary", ""))
    add_event(
        f"[Agent {agent['id']}] {'通过' if passed else f'发现 {len(issues)} 个问题'}",
        "success" if passed else "error",
    )
    return report

def _parse_report(path: Path, agent: dict) -> dict:
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        for pattern in [
            lambda t: json.loads(t),
            lambda t: json.loads(re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', t).group(1)),
            lambda t: json.loads(re.search(r'\{[\s\S]*?"issues"[\s\S]*\}', t).group()),
            lambda t: extract_json(t),
        ]:
            try:
                data = pattern(raw)
                if data:
                    return data
            except Exception:
                pass

    live_path = WORKSPACE / f"agent_{agent['id']}_live.json"
    if live_path.exists():
        try:
            live = json.loads(live_path.read_text())
            cnt  = live.get("issues_count", 0)
            act  = live.get("action", "")
            return {
                "agent": agent["id"], "role": agent["role"],
                "passed": cnt == 0 and "404" not in act and "失败" not in act,
                "issues": ([{"description": f"最后动作: {act}", "severity": "medium",
                             "location": "", "fix_hint": "Agent 未提交完整报告"}] if cnt > 0 else []),
                "summary": f"报告未写入，最后动作：{act[:60]}",
            }
        except Exception:
            pass

    return {"agent": agent["id"], "role": agent["role"],
            "passed": False, "issues": [], "summary": "报告解析失败"}

def run_all_agents(agents: list, plan: dict, iteration: int) -> list:
    reports, lock = [], threading.Lock()
    def _run(ag):
        r = run_agent(ag, plan, iteration)
        with lock: reports.append(r)
    threads = [threading.Thread(target=_run, args=(a,)) for a in agents]
    for t in threads: t.start()
    for t in threads: t.join()
    return reports

def fix_web_issues(reports: list, plan: dict, iteration: int) -> bool:
    all_issues = [
        {"from_agent": r.get("role", r.get("agent")), **iss}
        for r in reports if not r.get("passed")
        for iss in r.get("issues", [])
    ]
    if not all_issues:
        return True

    add_event(f"主CC：汇总 {len(all_issues)} 个问题，开始修复…", "fix")
    patch_status(status="fixing")

    # 读取当前项目文件内容嵌入提示词
    html_files = list(PROJECT_DIR.glob("*.html"))
    code = ""
    if html_files:
        code = f"\n当前 index.html（前 4000 字符）：\n{html_files[0].read_text()[:4000]}"

    prompt = f"""修复以下 Web 项目中的 Bug。

项目目录：{PROJECT_DIR}{code}

需要修复的问题：
{json.dumps(all_issues, ensure_ascii=False, indent=2)}

要求：直接修改 {PROJECT_DIR} 中的文件，修复全部问题，不要删除正常功能。"""

    call_claude(prompt, tools="Read,Write,Edit,Bash", timeout=300)
    add_event(f"主CC：第 {iteration} 轮修复完成", "fix")
    patch_status(status="testing")
    return False

def run_web_loop(plan: dict, max_iter: int) -> bool:
    """Web 项目：多 Agent 并行测试 + 主 CC 修复循环。"""
    start_project_server()

    patch_status(
        project_type=plan["project_type"],
        agents=[{
            "id": a["id"], "role": a["role"], "icon": a.get("icon", "🤖"),
            "focus": a.get("focus", ""), "status": "waiting",
            "action": "准备中…", "issues_count": 0,
            "passed": None, "issues": [], "summary": "",
        } for a in plan["agents"]],
    )
    add_event("主CC：开始 Web 多 Agent 测试（使用模板简报）")

    all_passed = False
    for iteration in range(1, max_iter + 1):
        patch_status(iteration=iteration, status="testing")
        add_event(f"━━━ 第 {iteration}/{max_iter} 轮测试 ━━━")

        s = read_status()
        for a in s["agents"]:
            a.update({"status": "waiting", "action": "准备中…", "passed": None})
        _write_raw(s)

        reports = run_all_agents(plan["agents"], plan, iteration)

        n_passed = sum(1 for r in reports if r.get("passed"))
        n_total  = len(reports)
        add_event(f"第 {iteration} 轮：{n_passed}/{n_total} 通过",
                  "success" if n_passed == n_total else "warn")

        if n_passed == n_total:
            all_passed = True
            break

        if iteration < max_iter:
            done = fix_web_issues(reports, plan, iteration)
            if done:
                all_passed = True
                break
        else:
            add_event(f"已达最大迭代次数 ({max_iter})", "warn")

    return all_passed

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

    init_workspace(task, max_iter)
    start_dashboard()
    time.sleep(0.3)
    url = f"http://localhost:{DASHBOARD_PORT}"
    print(f"\n  📊 Dashboard → {url}")
    webbrowser.open(url)
    time.sleep(0.5)

    # 生成项目 + 判断类型
    plan = analyze_and_generate(task)

    # 根据项目类型选择测试路径
    if plan["project_type"] in CLI_TYPES:
        add_event("📋 CLI 项目 → 使用 pytest 自动化测试（无 AI Agent）")
        all_passed = run_cli_loop(plan, max_iter)
    else:
        add_event("🌐 Web 项目 → 使用多 Agent 浏览器测试")
        all_passed = run_web_loop(plan, max_iter)

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
