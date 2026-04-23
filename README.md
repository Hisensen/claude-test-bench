# 🧪 Claude Test Bench

A multi-agent automated testing platform built on Claude Code.  
Give it any task → it builds the project → spawns multiple AI agents to test from different angles → iterates until all tests pass.

![Dashboard Preview](https://img.shields.io/badge/Dashboard-Web%20UI-7c3aed?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-22c55e?style=flat-square)

## How It Works

```
/test-bench "build a calculator web app"
        ↓
Main CC  →  generates project code
        ↓
Main CC  →  analyzes task, decides agent roles (2–6 agents)
        ↓
Agents run in parallel (each with a different testing perspective):
  🔍 Functionality tester
  🎨 UI / visual checker
  👤 Newbie user
  💥 Destructive tester
  ... (dynamically determined)
        ↓
Web Dashboard shows live status per agent
        ↓
Main CC aggregates issues → fixes code → reruns
        ↓
Loop until all agents pass (or max iterations reached)
```

## Quick Start

**Prerequisites:** [Claude Code](https://claude.ai/code) installed and signed in.

```bash
git clone https://github.com/Hisensen/claude-test-bench.git
cd claude-test-bench

# Open in Claude Code
claude .

# Then use the slash command:
/test-bench build a todo list web app
/test-bench write a Python CLI calculator
/test-bench make a weather API with Flask
```

The dashboard opens automatically at `http://localhost:7788`.

## Dashboard

Real-time web UI that shows:
- **Agent cards** — each agent's current action, status, and issues found
- **Event log** — timestamped feed of what's happening
- **Progress bar** — iteration progress with animations
- **Issue details** — severity, location, and fix hints per issue

## File Structure

```
test_bench/
├── .claude/commands/
│   └── test-bench.md       # Slash command definition
├── orchestrator.py         # Main engine (universal, task-agnostic)
├── dashboard/
│   └── index.html          # Self-contained web dashboard
├── examples/
│   └── 10games/            # Demo: 10-game webpage (full run history)
└── workspace/              # Auto-created per run (gitignored)
    ├── project/            # Generated project files
    ├── briefs/             # Per-agent test briefs
    ├── reports/            # Agent JSON reports
    └── run.json            # Live status (read by dashboard)
```

## Demo: 10-Game Webpage

The `examples/10games/` directory contains a complete run from the original demo task:
- 10 fully playable browser games (Snake, Minesweeper, 2048, etc.)
- 3 iterations, 4 agents, 12 bug fixes
- Full iteration log

Open `examples/10games/project/index.html` in any browser to play.

## Agent Roles (Dynamic)

The main CC decides which agents to spawn based on the task. Typical combinations:

| Project Type | Agents |
|---|---|
| Simple CLI | Functionality + Edge Cases + Help Docs |
| Web App | Functionality + UI + Newbie UX + Destructive |
| REST API | Functionality + Auth/Security + Data Validation |
| Complex App | Functionality + UI + Performance + Security + Newbie + Destructive |

## How Agents Update the Dashboard

Each agent writes a live status file (`workspace/agent_N_live.json`) as it works:

```json
{ "action": "Checking Snake collision logic...", "issues_count": 1 }
```

The orchestrator polls this file every 1.5s and updates `workspace/run.json`.  
The dashboard polls `/status` every 700ms and re-renders the UI.

## License

MIT
