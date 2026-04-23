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

# ── 阶段 1a：生成项目代码 ─────────────────────────────────────────────
def generate_project(task: str):
    """专注生成代码，不做其他事"""
    add_event("主CC：生成项目代码…")
    prompt = f"""任务：{task}

将完整可运行的项目代码写入目录 {PROJECT_DIR}。
规则：
- 网页类 → 单文件 index.html（CSS/JS 全部内嵌）
- CLI 工具 → main.py 或 main.js
- 代码必须完整，不省略任何逻辑，可直接运行
- 不需要解释，直接写文件

写完后输出一行：PROJECT_GENERATED"""
    call_claude(prompt, tools="Write,Bash,Edit", timeout=600)
    add_event("主CC：代码写入完成")

# ── 阶段 1b：分析项目，生成测试计划 ──────────────────────────────────────
def plan_project(task: str) -> dict:
    """读取已生成的项目，决定 Agent 数量和角色"""
    add_event("主CC：分析项目，制定测试计划…")
    prompt = f"""查看目录 {PROJECT_DIR} 中已生成的项目文件（用 ls/cat 阅读），然后直接输出一个 JSON 对象。

任务背景：{task}

JSON 格式（直接输出，不要 markdown 代码块，不要任何额外文字）：
{{"project_type":"web_app","description":"项目简介","how_to_run":"如何运行（如：在浏览器打开 project/index.html）","tech_stack":"技术栈","agents":[{{"id":1,"role":"角色名","icon":"emoji","focus":"测试重点","approach":"测试手段"}}]}}

Agent 数量原则：简单项目2-3个，中等3-4个，复杂4-5个。必须包含功能测试角色。"""

    output = call_claude(prompt, tools="Read,Bash", timeout=120)

    # 多策略提取 JSON
    plan = _extract_plan_json(output)
    if plan:
        # 写入文件备查
        (WORKSPACE / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2))
        add_event(f"主CC：测试计划确定（{plan.get('project_type','?')}，{len(plan.get('agents',[]))} 个 Agent）")
        return plan

    add_event("主CC：计划解析失败，使用默认 3-agent 方案", "warn")
    return _default_plan(task)

def _extract_plan_json(text: str) -> dict | None:
    """多策略从文本中提取合法 plan JSON"""
    # 策略1：直接解析整个文本
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # 策略2：提取第一个完整 JSON 对象（贪婪匹配）
    try:
        m = re.search(r'\{[\s\S]*"agents"[\s\S]*\}', text)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    # 策略3：逐字符找最长合法 JSON
    for i, ch in enumerate(text):
        if ch == '{':
            for j in range(len(text), i, -1):
                try:
                    obj = json.loads(text[i:j])
                    if isinstance(obj, dict) and "agents" in obj:
                        return obj
                except Exception:
                    continue
    return None

def _default_plan(task: str) -> dict:
    return {
        "project_type": "unknown",
        "description":  task,
        "how_to_run":   f"查看 {PROJECT_DIR}",
        "tech_stack":   "未知",
        "agents": [
            {"id": 1, "role": "功能测试员",    "icon": "🔍",
             "focus": "验证核心功能逻辑是否正确",          "approach": "代码审查+功能测试"},
            {"id": 2, "role": "用户体验测试员", "icon": "👤",
             "focus": "评估易用性和新手上手体验",           "approach": "用户体验"},
            {"id": 3, "role": "破坏性测试员",   "icon": "💥",
             "focus": "寻找边界案例、竞态条件和崩溃点",    "approach": "破坏性测试"},
        ],
    }

# ── 阶段 1：入口（拆分后的组合调用）──────────────────────────────────────
def analyze_and_generate(task: str) -> dict:
    patch_status(status="generating")
    generate_project(task)          # 专注写代码
    patch_status(status="planning")
    return plan_project(task)       # 专注分析计划

# ── 阶段 2：生成测试简报 ───────────────────────────────────────────────
def generate_brief(agent: dict, plan: dict) -> Path:
    """为单个 agent 生成测试简报，写入文件"""
    others = ", ".join(a["role"] for a in plan["agents"] if a["id"] != agent["id"])

    prompt = f"""生成测试简报（Markdown）：

项目：{plan.get('description')} | 类型：{plan.get('project_type')} | 目录：{PROJECT_DIR}
运行：{plan.get('how_to_run')}

角色：{agent['role']}（#{agent['id']}）
重点：{agent['focus']}
手段：{agent['approach']}
其他 Agent（不重复）：{others}

简报包含：① 角色定位（2句） ② 具体测试清单（8+条） ③ 报告必须用此 JSON：
{{"agent":{agent['id']},"role":"{agent['role']}","passed":true/false,"issues":[{{"description":"","severity":"high/medium/low","location":"","fix_hint":""}}],"summary":""}}
"""
    brief_text = call_claude(prompt, tools="", timeout=90)
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
