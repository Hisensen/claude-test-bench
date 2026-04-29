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
# Web 路径 v2：脚本化测试（特征清点 + 矩阵 + DOM 探测 + 自动 spec + 自愈）
# ══════════════════════════════════════════════════════════════════════

def extract_features(html_path: Path) -> dict:
    """阶段 D：让 AI 清点项目里的所有可测特征，输出 features.json"""
    add_event("阶段 D：清点项目特征（按钮/状态/按键）…")
    html = html_path.read_text(encoding="utf-8")[:12000]
    prompt = f"""阅读以下 HTML 项目，机械式清点所有可测特征，直接输出 JSON（不要 markdown 代码块、不要解释）。

```html
{html}
```

JSON schema:
{{
  "buttons":     [{{"id_or_text": "选择器/可见文字", "purpose": "用途简述"}}],
  "inputs":      [{{"id_or_label": "选择器/标签", "type": "text/number/etc"}}],
  "key_handlers":[{{"key": "ArrowUp/Enter/etc", "action": "做什么"}}],
  "states":      [{{"name": "menu/play/end", "trigger": "如何进入"}}],
  "canvas":      true/false,
  "external_state_var": "如 window.gameState 或 null（仅 canvas 项目用）",
  "console_check": true
}}

注意：宁可漏写，不可瞎编。只列代码里**实际存在**的元素和事件。"""
    output = call_claude(prompt, tools="", timeout=120)
    feats = extract_json(output) or {"buttons": [], "inputs": [], "key_handlers": [],
                                      "states": [], "canvas": False, "external_state_var": None,
                                      "console_check": True}
    (WORKSPACE / "features.json").write_text(json.dumps(feats, ensure_ascii=False, indent=2))
    n_btn = len(feats.get("buttons", []))
    n_key = len(feats.get("key_handlers", []))
    n_state = len(feats.get("states", []))
    add_event(f"阶段 D：清点完成 → {n_btn} 按钮, {n_key} 按键, {n_state} 状态")
    return feats

def expand_matrix(feats: dict) -> list:
    """阶段 E.1：把特征机械展开成测试用例矩阵（纯代码，零 AI）"""
    cases = []
    cases.append({"id": "load",   "kind": "smoke",  "desc": "页面能正常加载，无 JS 报错"})
    cases.append({"id": "title",  "kind": "smoke",  "desc": "<title> 标签存在且非空"})
    cases.append({"id": "no_console_error", "kind": "smoke", "desc": "控制台无 error 级别消息"})

    for i, b in enumerate(feats.get("buttons", [])):
        sel = b.get("id_or_text", "")
        cases.append({"id": f"btn_visible_{i}", "kind": "ui",
                      "desc": f"按钮「{sel}」可见可点击", "selector": sel})
        cases.append({"id": f"btn_click_{i}", "kind": "interaction",
                      "desc": f"点击按钮「{sel}」不引发异常", "selector": sel})

    for i, inp in enumerate(feats.get("inputs", [])):
        sel = inp.get("id_or_label", "")
        cases.append({"id": f"input_accept_{i}", "kind": "interaction",
                      "desc": f"输入框「{sel}」接受输入", "selector": sel,
                      "input_type": inp.get("type", "text")})

    for i, k in enumerate(feats.get("key_handlers", [])):
        cases.append({"id": f"key_{i}", "kind": "interaction",
                      "desc": f"按键「{k.get('key')}」触发反应不报错", "key": k.get("key")})

    for i, s in enumerate(feats.get("states", [])):
        cases.append({"id": f"state_{i}", "kind": "flow",
                      "desc": f"能进入状态「{s.get('name')}」", "trigger": s.get("trigger", "")})

    for w, h in [(375, 667), (768, 1024), (1280, 720)]:
        cases.append({"id": f"viewport_{w}x{h}", "kind": "responsive",
                      "desc": f"视口 {w}x{h} 下页面可见", "viewport": [w, h]})
    return cases

def probe_dom(project_url: str, feats: dict) -> dict:
    """阶段 E.2：用 Python Playwright 程序化探测真实 DOM（零 AI）"""
    from playwright.sync_api import sync_playwright
    add_event("阶段 E：DOM 探测（程序化 Playwright）…")
    snapshots = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(project_url, wait_until="load", timeout=15000)
            page.wait_for_timeout(800)
            snapshots["initial"] = {
                "title": page.title(),
                "html_excerpt": page.content()[:3000],
                "console_errors": [],
                "buttons_in_dom": [b.inner_text()[:40] for b in page.query_selector_all("button")[:20]],
                "inputs_in_dom":  [i.get_attribute("id") or i.get_attribute("name") or ""
                                   for i in page.query_selector_all("input,textarea,select")[:20]],
            }
            browser.close()
    except Exception as e:
        snapshots["initial"] = {"error": str(e)[:200]}
    (WORKSPACE / "dom_probes.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
    add_event(f"阶段 E：探测完成 → {len(snapshots.get('initial', {}).get('buttons_in_dom', []))} 个按钮")
    return snapshots

def generate_test_script(matrix: list, dom: dict, feats: dict, html_path: Path) -> Path:
    """阶段 E.3：让 AI 把矩阵 + 真实 DOM 翻译成 pytest-playwright 脚本（一次 AI 调用）"""
    add_event(f"阶段 E：生成测试脚本（{len(matrix)} 个用例）…")
    test_path = PROJECT_DIR / "test_e2e.py"
    html_excerpt = html_path.read_text(encoding="utf-8")[:5000]
    prompt = f"""为以下 Web 项目写一个完整的 pytest-playwright 测试文件，写入路径：{test_path}

## 项目 HTML（前 5000 字符）
```html
{html_excerpt}
```

## 真实 DOM 探测（实际页面长这样）
```json
{json.dumps(dom, ensure_ascii=False, indent=2)[:2000]}
```

## 项目特征
```json
{json.dumps(feats, ensure_ascii=False, indent=2)[:2000]}
```

## 测试用例矩阵（每条转成一个 pytest 测试函数）
```json
{json.dumps(matrix, ensure_ascii=False, indent=2)[:4000]}
```

## 硬性要求
1. 文件第一行必须是：`from playwright.sync_api import Page, expect`
2. 项目 URL：`{PROJECT_URL}`
3. 用 pytest-playwright 的 `page` fixture
4. 每个测试函数命名为 `test_{{matrix_id}}`
5. 失败时给出**简短**断言信息，不要长堆栈
6. 不抓不可恢复异常；让 pytest 捕获就行
7. canvas 项目用 `page.evaluate()` 读取 `window.{feats.get("external_state_var") or "<state-var>"}` 检查内部状态
8. 控制台错误检查用 `page.on("console", lambda m: ...)` 收集
9. 只输出 Python 代码，不要 markdown 代码块、不要解释
10. 直接 Write 到 {test_path}

写完后输出一行：TEST_GENERATED"""
    call_claude(prompt, tools="Write", timeout=240)
    if not test_path.exists():
        add_event("阶段 E：测试脚本未生成 ❌", "error")
        return None
    add_event(f"阶段 E：测试脚本生成完成 ({test_path.stat().st_size} 字节)")
    return test_path

def run_test_script(test_path: Path) -> dict:
    """阶段 E.4：跑 pytest，解析结果（零 AI）"""
    add_event("阶段 E：运行 pytest-playwright…")
    result = subprocess.run(
        ["python", "-m", "pytest", str(test_path), "-v", "--tb=short", "--no-header",
         "-x" if False else "--maxfail=999",  # 不要 fail-fast，全部跑完
         "--browser=chromium"],
        capture_output=True, text=True, cwd=str(PROJECT_DIR), timeout=600,
    )
    output = result.stdout + result.stderr
    passed_n = int(m.group(1)) if (m := re.search(r'(\d+) passed', output)) else 0
    failed_n = int(m.group(1)) if (m := re.search(r'(\d+) failed', output)) else 0
    error_n  = int(m.group(1)) if (m := re.search(r'(\d+) error',  output)) else 0
    failures = []
    for blk in re.split(r'_{10,}|={10,}', output):
        m = re.search(r'(FAILED|ERROR) (\S+)', blk)
        if m:
            err = re.search(r'(AssertionError|TimeoutError|\w+Error)[^\n]*', blk)
            failures.append({
                "test_id": m.group(2).split("::")[-1],
                "kind": m.group(1),
                "error": (err.group(0) if err else blk[:200])[:300],
            })
    return {
        "passed": passed_n, "failed": failed_n, "error": error_n,
        "all_passed": result.returncode == 0,
        "failures": failures, "raw_output": output[-5000:],
    }

def categorize_failure(f: dict) -> str:
    """失败分类：F1 选择器/时序错 | F2 真 bug | F3 环境错 | F4 未知"""
    err = f.get("error", "").lower()
    if any(k in err for k in ["element not found", "no element", "timeout waiting",
                              "selector", "is not visible", "is not attached"]):
        return "F1_selector"
    if "connection refused" in err or "module not found" in err or "import" in err:
        return "F3_env"
    if "assertionerror" in err or "expect" in err:
        return "F2_bug"
    return "F4_unknown"

def fix_with_rollback(failures: list, test_path: Path, html_path: Path,
                     iteration: int, pre_fix_passed: int) -> bool:
    """阶段 F：分类驱动的自愈循环 + git regression 防护"""
    if not failures:
        return True
    classes = {}
    for f in failures:
        classes.setdefault(categorize_failure(f), []).append(f)

    # F3 环境错优先：通常无解，跳过
    if "F3_env" in classes:
        add_event(f"阶段 F：{len(classes['F3_env'])} 个环境错误，跳过修复", "warn")

    # F1 改 spec, F2 改项目代码
    f1, f2 = classes.get("F1_selector", []) + classes.get("F4_unknown", []), classes.get("F2_bug", [])

    # ── 修脚本（F1）──
    if f1:
        add_event(f"阶段 F：修脚本（{len(f1)} 个选择器/时序错）…", "fix")
        prompt = f"""修复 pytest-playwright 测试脚本中的失败用例。**只改测试文件**，不要改项目代码。

测试文件：{test_path}

```python
{test_path.read_text(encoding='utf-8')[:6000]}
```

失败用例（选择器或时序问题）：
{json.dumps(f1, ensure_ascii=False, indent=2)[:3000]}

把 {test_path} 里失败的 test 函数定向修好（调整选择器、加 wait_for、改 timeout）。其它通过的 test 不要动。
只输出"DONE"。"""
        call_claude(prompt, tools="Read,Write,Edit", timeout=240)

    # ── 修代码（F2，带 git rollback）──
    if f2:
        add_event(f"阶段 F：修项目代码（{len(f2)} 个 assertion 失败）…", "fix")
        # 备份当前项目状态
        snapshot_dir = WORKSPACE / f".snapshot_iter{iteration}"
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        shutil.copytree(PROJECT_DIR, snapshot_dir)
        prompt = f"""修复 Web 项目中的 bug，使 pytest 通过。**只改项目代码**，不要改 test_e2e.py。

项目 HTML（{html_path.name}）：
```html
{html_path.read_text(encoding='utf-8')[:6000]}
```

失败的断言：
{json.dumps(f2, ensure_ascii=False, indent=2)[:3000]}

直接修改 {html_path}，使断言通过。不要删除正常功能。"""
        call_claude(prompt, tools="Read,Write,Edit", timeout=300)

        # 跑全量回归
        add_event("阶段 F：跑全量回归检查…")
        new_results = run_test_script(test_path)
        if new_results["passed"] < pre_fix_passed:
            # 越修越差，回滚
            add_event(f"⚠️ 修复后通过数下降 ({pre_fix_passed} → {new_results['passed']})，回滚", "warn")
            shutil.rmtree(PROJECT_DIR)
            shutil.copytree(snapshot_dir, PROJECT_DIR)
        shutil.rmtree(snapshot_dir, ignore_errors=True)
    return False

def run_web_loop(plan: dict, max_iter: int) -> bool:
    """Web v2 主循环：清点 → 矩阵 → 探测 → 生成 spec → 跑 → 修复"""
    start_project_server()
    html_files = list(PROJECT_DIR.glob("*.html"))
    if not html_files:
        add_event("❌ 找不到 HTML 文件", "error")
        return False
    html_path = html_files[0]

    # Dashboard 用一个虚拟 Agent 卡片显示状态
    patch_status(
        project_type=plan["project_type"],
        agents=[{
            "id": 1, "role": "Web 自动测试", "icon": "🌐",
            "focus": "脚本化端到端测试", "status": "waiting",
            "action": "准备中…", "issues_count": 0,
            "passed": None, "issues": [], "summary": "",
        }],
    )
    add_event("阶段 D-E 启动：脚本化 Web 测试")
    patch_agent(1, status="running", action="清点项目特征…")

    feats = extract_features(html_path)
    matrix = expand_matrix(feats)
    add_event(f"阶段 E.1：矩阵展开 → {len(matrix)} 个测试用例")
    dom = probe_dom(PROJECT_URL, feats)
    test_path = generate_test_script(matrix, dom, feats, html_path)
    if not test_path:
        patch_agent(1, status="failed", action="❌ 测试脚本生成失败", passed=False)
        return False

    all_passed = False
    for iteration in range(1, max_iter + 1):
        patch_status(iteration=iteration, status="testing")
        patch_agent(1, status="running", action=f"运行 pytest（第 {iteration} 轮）…")
        add_event(f"━━━ 第 {iteration}/{max_iter} 轮 pytest ━━━")
        results = run_test_script(test_path)
        (REPORTS_DIR / f"web_iter{iteration}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2))
        total_bad = results["failed"] + results["error"]

        if results["all_passed"]:
            patch_agent(1, status="passed", passed=True,
                        action=f"✅ {results['passed']} 个测试全部通过",
                        summary=f"{results['passed']} 通过")
            add_event(f"🎉 Web pytest：{results['passed']} 个测试全部通过", "success")
            all_passed = True
            break

        issues = [{"description": f["test_id"], "severity": "high",
                   "location": "test_e2e.py", "fix_hint": f["error"][:120]}
                  for f in results["failures"][:20]]
        patch_agent(1, status="failed", passed=False,
                    action=f"❌ {total_bad} 个失败",
                    issues=issues, issues_count=total_bad,
                    summary=f"{results['passed']} 通过，{total_bad} 失败")
        add_event(f"Web pytest：{results['passed']} 通过，{total_bad} 失败", "error")

        if iteration >= max_iter:
            add_event(f"已达最大迭代次数 ({max_iter})", "warn")
            break
        patch_status(status="fixing")
        fix_with_rollback(results["failures"], test_path, html_path,
                          iteration, results["passed"])
        patch_status(status="testing")
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
