import json
import os
import time
from typing import Any, Dict, List, Literal, Optional, TypedDict

import paramiko
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

load_dotenv()

# -----------------------------
# 1) Policy (охрана труда)
# -----------------------------

READONLY_PREFIXES = (
    "uname",
    "uptime",
    "date",
    "whoami",
    "id",
    "cat /etc/os-release",
    "df",
    "free",
    "ps",
    "top -b -n1",
    "systemctl status",
    "journalctl",
    "ss ",
    "ip a",
    "ip r",
    "ping ",
    "dig ",
    "nslookup ",
    "tail ",
    "head ",
    "grep ",
    "ls ",
    "stat ",
    "du ",
)

# "Мягкие" изменения: обратимые или ограниченно рискованные
CHANGE_PREFIXES = (
    "apt-get update",
    "apt-get upgrade",
    "apt-get install",
    "apt-get remove",
    "dnf update",
    "dnf install",
    "dnf remove",
    "yum update",
    "yum install",
    "yum remove",
    "systemctl restart",
    "systemctl reload",
)

DENY_TOKENS = (
    "ssh ",
    "rm -rf",
    "mkfs",
    "dd ",
    ">:",
    "iptables -F",
    "nft flush",
    "shutdown",
    "reboot",
    ":(){",
    "curl | sh",
    "wget | sh",
)


def classify_command(cmd: str) -> Literal["readonly", "change", "deny"]:
    c = cmd.strip()

    for tok in DENY_TOKENS:
        if tok in c:
            return "deny"

    for p in READONLY_PREFIXES:
        if c.startswith(p):
            return "readonly"

    for p in CHANGE_PREFIXES:
        if c.startswith(p):
            return "change"

    return "deny"


# -----------------------------
# 2) SSH executor
# -----------------------------


def run_ssh(
    host: str,
    user: str,
    key_path: Optional[str],
    password: Optional[str],
    cmd: str,
    timeout: int = 60,
) -> Dict[str, Any]:
    kind = classify_command(cmd)
    if kind == "deny":
        return {
            "ok": False,
            "error": f"Command denied by policy: {cmd}",
            "cmd": cmd,
            "kind": kind,
        }

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: Dict[str, Any] = {
        "hostname": host,
        "username": user,
        "timeout": 10,
    }

    if key_path:
        connect_kwargs["key_filename"] = key_path
    elif password:
        connect_kwargs["password"] = password
    else:
        return {
            "ok": False,
            "error": "No auth method provided",
            "cmd": cmd,
            "kind": kind,
        }

    ssh.connect(**connect_kwargs)

    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    ssh.close()

    return {
        "ok": code == 0,
        "cmd": cmd,
        "kind": kind,
        "exit_code": code,
        "stdout": out,
        "stderr": err,
    }


# -----------------------------
# 3) State
# -----------------------------


class AgentState(TypedDict, total=False):
    goal: str
    host: str
    user: str
    password: Optional[str]
    key_path: Optional[str]
    steps: List[Dict[str, Any]]
    transcript: List[Dict[str, str]]
    done: bool
    max_steps: int

    # внутренние поля (чтобы не ругался типизатор)
    _next_command: str
    _success_criteria: str
    _rationale: str
    _report_md: str


# -----------------------------
# 4) LLM (OpenRouter)
# -----------------------------


def make_llm() -> ChatOpenAI:
    api_key = os.environ["OPENROUTER_API_KEY"]
    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0.1,
    )


SYSTEM = """Ты автономный помощник системного администратора.
У тебя есть доступ только к одному инструменту: выполнение ОДНОЙ ssh-команды за раз.
Твоя задача: достигнуть цели пользователя, собирая факты и применяя минимально рискованные действия.
Ты НЕ запускаешь ssh внутри команд.
Инструмент уже выполняет команду на удалённом сервере под пользователем, заданным в параметрах.
Команда должна быть ТОЛЬКО тем, что нужно выполнить НА СЕРВЕРЕ (например: "systemctl status nginx").
Никогда не указывай "root@" или другой хост/пользователя в next_command.
Ты НЕ находишься в локальном терминале.
Ты НЕ выполняешь ssh.
Инструмент уже подключён к серверу под нужным пользователем.

Ты должен возвращать ТОЛЬКО команду,
которую нужно выполнить НА СЕРВЕРЕ.

НЕ используй:
- ssh
- root@
- имя хоста
- кавычки вокруг всей команды
- && между несколькими действиями

Одна команда за шаг.

Правила:
- Сначала диагностика (read-only), затем осторожные изменения.
- Запрещено придумывать опасные команды (rm -rf, mkfs, dd, iptables flush, reboot/shutdown и т.п.).
- Если не уверен — собирай больше фактов.
- Всегда проверяй эффект после изменения.
- Отвечай СТРОГО JSON-объектом вида:
{
  "rationale": "кратко почему так",
  "next_command": "одна команда",
  "success_criteria": "как поймём, что шаг помог",
  "stop": false
}
Если нужно завершить работу (цель достигнута или упёрлись) — stop=true и next_command="".
"""


def planner_node(state: AgentState) -> AgentState:
    llm = make_llm()

    last_steps = state["steps"][-5:]
    context = {
        "goal": state["goal"],
        "host": state["host"],
        "recent_steps": last_steps,
        "policy_note": "Команды вне allowlist будут отклонены.",
        "remaining_budget": state["max_steps"] - len(state["steps"]),
    }

    msgs = [
        SystemMessage(content=SYSTEM),
        HumanMessage(content="Контекст:\n" + json.dumps(context, ensure_ascii=False)),
    ]

    resp = llm.invoke(msgs).content
    try:
        plan = json.loads(resp)
    except Exception:
        plan = {
            "rationale": "Модель вернула не-JSON; делаю безопасный дефолт: сбор базовой диагностики.",
            "next_command": "uname -a",
            "success_criteria": "получим информацию о ядре/ОС",
            "stop": False,
        }

    state["transcript"].append({"role": "planner", "content": resp})
    state["transcript"].append(
        {"role": "planner_parsed", "content": json.dumps(plan, ensure_ascii=False)}
    )

    if plan.get("stop") is True or len(state["steps"]) >= state["max_steps"]:
        state["done"] = True
        return state

    state["_next_command"] = plan.get("next_command", "") or ""
    state["_success_criteria"] = plan.get("success_criteria", "") or ""
    state["_rationale"] = plan.get("rationale", "") or ""
    state["transcript"].append(
        {"role": "next_command", "content": state["_next_command"]}
    )
    return state


def executor_node(state: AgentState) -> AgentState:
    cmd = state.get("_next_command", "")
    if not cmd:
        state["done"] = True
        return state

    result = run_ssh(
        host=state["host"],
        user=state["user"],
        key_path=state.get("key_path"),
        password=state.get("password"),
        cmd=cmd,
    )
    result["rationale"] = state.get("_rationale", "")
    result["success_criteria"] = state.get("_success_criteria", "")
    result["ts"] = time.time()
    state["steps"].append(result)
    return state


def critic_node(state: AgentState) -> AgentState:
    if not state["steps"]:
        return state

    last = state["steps"][-1]
    if last.get("kind") == "deny":
        state["done"] = True
        return state

    tail = state["steps"][-3:]
    if len(tail) == 3 and all(not s.get("ok", False) for s in tail):
        state["done"] = True

    return state


def route_next(state: AgentState) -> str:
    return END if state["done"] else "planner"


def reporter_node(state: AgentState) -> AgentState:
    lines: List[str] = []
    lines.append(f"# Отчёт: {state['goal']}")
    lines.append("")
    lines.append(f"- Host: `{state['host']}`")
    lines.append(f"- User: `{state['user']}`")
    lines.append(f"- Steps: {len(state['steps'])}/{state['max_steps']}")
    lines.append("")

    for i, s in enumerate(state["steps"], 1):
        lines.append(f"## Step {i}: `{s.get('cmd', '')}`")
        lines.append(
            f"- Kind: `{s.get('kind')}`  Exit: `{s.get('exit_code', 'n/a')}`  OK: `{s.get('ok')}`"
        )
        if s.get("rationale"):
            lines.append(f"- Why: {s['rationale']}")
        if s.get("success_criteria"):
            lines.append(f"- Success criteria: {s['success_criteria']}")

        if s.get("error"):
            lines.append("")
            lines.append("**Policy/Error:**")
            lines.append("```")
            lines.append(str(s["error"]))
            lines.append("```")
        else:
            lines.append("")
            lines.append("### STDOUT")
            lines.append("```")
            lines.append((s.get("stdout") or "").strip()[:8000])
            lines.append("```")
            lines.append("### STDERR")
            lines.append("```")
            lines.append((s.get("stderr") or "").strip()[:8000])
            lines.append("```")
        lines.append("")

    state["_report_md"] = "\n".join(lines)
    return state


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("planner", planner_node)
    g.add_node("executor", executor_node)
    g.add_node("critic", critic_node)
    g.add_node("reporter", reporter_node)

    g.set_entry_point("planner")
    g.add_edge("planner", "executor")
    g.add_edge("executor", "critic")
    g.add_conditional_edges(
        "critic", route_next, {END: "reporter", "planner": "planner"}
    )
    g.add_edge("reporter", END)

    return g.compile()


def run(
    goal: str,
    host: str,
    user: str,
    key_path: Optional[str],
    password: Optional[str],
    max_steps: int = 25,
) -> str:
    app = build_graph()
    init: AgentState = {
        "goal": goal,
        "host": host,
        "user": user,
        "key_path": key_path,
        "password": password,
        "steps": [],
        "transcript": [],
        "done": False,
        "max_steps": max_steps,
    }
    out = app.invoke(init)
    return out.get("_report_md", "")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--goal", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--password", default=None)
    p.add_argument("--key", default=None)
    p.add_argument("--max-steps", type=int, default=25)
    args = p.parse_args()

    md = run(
        goal=args.goal,
        host=args.host,
        user=args.user,
        key_path=args.key,
        password=args.password,
        max_steps=args.max_steps,
    )
    print(md)
