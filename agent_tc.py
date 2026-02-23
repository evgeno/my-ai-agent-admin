#!/usr/bin/env python3
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import openai  # для перехвата openai.RateLimitError
import paramiko
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

load_dotenv()

# -------------------------
# Config
# -------------------------
LOG_PATH = os.environ.get("AGENT_LOG", "agent_run.log")
REPORT_DIR = Path(os.environ.get("AGENT_REPORT_DIR", "reports"))
REPORT_DIR.mkdir(parents=True, exist_ok=True)

OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

# Сохраняем все шаги в памяти, потом делаем отчёт
RUN_STEPS: List[Dict[str, Any]] = []


def log(event: str, data: Dict[str, Any]):
    rec = {"ts": time.time(), "event": event, **data}
    line = json.dumps(rec, ensure_ascii=False)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# -------------------------
# Policy
# -------------------------

DENY_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bmkfs\b",
    r"\bdd\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\biptables\s+-F\b",
    r"\bnft\s+flush\b",
    r"curl\s+.*\|\s*sh",
    r"wget\s+.*\|\s*sh",
    r":\(\)\s*\{",  # fork bomb
    r"\bssh\s+",  # запрещаем ssh внутри ssh
]

# Запрещаем объединение команд (одна команда = один шаг)
DENY_TOKENS = ["&&", "||", ";", "`", "$(", "|"]


def policy_check(cmd: str) -> Optional[str]:
    c = cmd.strip()
    if not c:
        return "Empty command"

    for tok in DENY_TOKENS:
        if tok in c:
            return f"Chaining/token '{tok}' is not allowed. One command per step."

    for pat in DENY_PATTERNS:
        if re.search(pat, c):
            return f"Command denied by policy (matched: {pat})"

    return None


# -------------------------
# SSH Exec
# -------------------------


def _ssh_exec(
    host: str,
    user: str,
    password: Optional[str],
    key_path: Optional[str],
    cmd: str,
    timeout: int = 600,  # apt может быть долгим
) -> Dict[str, Any]:
    err = policy_check(cmd)
    if err:
        step = {
            "ts": time.time(),
            "cmd": cmd,
            "exit_code": None,
            "ok": False,
            "stdout": "",
            "stderr": "",
            "duration_s": 0.0,
            "error": err,
        }
        RUN_STEPS.append(step)
        log("ssh_denied", {"cmd": cmd, "error": err})
        return {"ok": False, "error": err, "cmd": cmd}

    log("ssh_start", {"host": host, "user": user, "cmd": cmd, "timeout": timeout})

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: Dict[str, Any] = {
        "hostname": host,
        "username": user,
        "timeout": 10,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if key_path:
        connect_kwargs["key_filename"] = key_path
    else:
        connect_kwargs["password"] = password

    t0 = time.time()
    ssh.connect(**connect_kwargs)

    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)

    channel = stdout.channel
    channel.settimeout(timeout)

    out_chunks = []
    err_chunks = []

    start = time.time()

    while not channel.exit_status_ready():
        if channel.recv_ready():
            out_chunks.append(channel.recv(4096).decode(errors="replace"))
        if channel.recv_stderr_ready():
            err_chunks.append(channel.recv_stderr(4096).decode(errors="replace"))
        if time.time() - start > timeout:
            break
        time.sleep(0.1)

    # дочитываем остатки
    while channel.recv_ready():
        out_chunks.append(channel.recv(4096).decode(errors="replace"))
    while channel.recv_stderr_ready():
        err_chunks.append(channel.recv_stderr(4096).decode(errors="replace"))

    code = channel.recv_exit_status()

    out = "".join(out_chunks)
    errout = "".join(err_chunks)
    ssh.close()

    dt = time.time() - t0

    payload = {
        "cmd": cmd,
        "exit_code": code,
        "ok": code == 0,
        "duration_s": round(dt, 3),
        "stdout_len": len(out),
        "stderr_len": len(errout),
    }
    if code != 0:
        payload["stdout_head"] = out[:800]
        payload["stderr_head"] = errout[:800]
    log("ssh_done", payload)

    step = {
        "ts": time.time(),
        "cmd": cmd,
        "exit_code": code,
        "ok": code == 0,
        "stdout": out[:12000],
        "stderr": errout[:6000],
        "duration_s": dt,
    }
    RUN_STEPS.append(step)

    return {
        "ok": code == 0,
        "exit_code": code,
        "cmd": cmd,
        "stdout": out,
        "stderr": errout,
        "duration_s": dt,
    }


# -------------------------
# Stop condition to avoid loops
# -------------------------


def should_stop_due_to_apt_failures() -> Optional[str]:
    """
    Если apt несколько раз подряд возвращает 100 — стопаемся,
    чтобы агент не крутился бесконечно.
    """
    recent = RUN_STEPS[-10:]
    apt_100 = 0
    for s in recent:
        c = s.get("cmd") or ""
        ec = s.get("exit_code")
        if (
            ("apt-get update" in c)
            or ("apt-get install" in c)
            or ("apt-get upgrade" in c)
        ):
            if ec == 100:
                apt_100 += 1
    if apt_100 >= 2:
        return (
            "APT repeatedly failed with exit code 100. "
            "Likely missing sudo rights, broken repos, no network/DNS, or dpkg lock. "
            "Stopping to avoid looping."
        )
    return None


# -------------------------
# Agent Factory
# -------------------------


def make_agent(host: str, user: str, password: Optional[str], key_path: Optional[str]):
    @tool("run_remote")
    def run_remote(command: str) -> str:
        """
        Run ONE safe command on the remote server.
        Tool already connects to host/user — do NOT use ssh inside.
        """
        cmd = command.strip()

        # Авто-правка для Debian apt:
        # - делаем noninteractive
        # - добавляем sudo (иначе apt почти всегда падает)
        if cmd.startswith("apt-get "):
            if not cmd.startswith("sudo "):
                cmd = "sudo " + cmd

            # -y обязательно для install/upgrade/remove
            if " -y" not in cmd and (
                cmd.startswith("sudo apt-get install")
                or cmd.startswith("sudo apt-get upgrade")
                or cmd.startswith("sudo apt-get remove")
            ):
                cmd = cmd + " -y"

            if "DEBIAN_FRONTEND=noninteractive" not in cmd:
                cmd = "DEBIAN_FRONTEND=noninteractive " + cmd

        res = _ssh_exec(
            host=host, user=user, password=password, key_path=key_path, cmd=cmd
        )

        # Анти-луп по apt
        stop_reason = should_stop_due_to_apt_failures()
        if stop_reason:
            return "FATAL: " + stop_reason

        if not res.get("ok"):
            return (
                f"ERROR\n"
                f"cmd: {res.get('cmd')}\n"
                f"exit: {res.get('exit_code')}\n"
                f"stdout:\n{(res.get('stdout') or '')[:1200]}\n"
                f"stderr:\n{(res.get('stderr') or '')[:800]}\n"
            )

        out = (res.get("stdout") or "")[:4000]
        err = (res.get("stderr") or "")[:1200]
        return (
            f"OK exit={res.get('exit_code')}\n"
            f"cmd: {res.get('cmd')}\n"
            f"duration_s: {res.get('duration_s')}\n"
            f"stdout:\n{out}\n"
            f"stderr:\n{err}\n"
        )

    api_key = os.environ["OPENROUTER_API_KEY"]

    llm = ChatOpenAI(
        model=OPENROUTER_MODEL,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0.1,
    )

    system = SystemMessage(
        content=f"""
Ты — автономный помощник системного администратора для Debian 12.
Единственный способ выполнить команду — вызвать инструмент run_remote(command).
Ты НЕ используешь ssh, root@, имя хоста/пользователя внутри command.

Правила:
- Одна команда за шаг (никаких && ; | $() backticks).
- Сначала диагностика, потом изменения.
- Для установки/обновления используй apt-get (инструмент сам добавит sudo, -y и DEBIAN_FRONTEND=noninteractive).
- После изменений всегда проверяй результат (dpkg -l, systemctl is-active/status, nginx -v).
- Если получаешь "FATAL: APT repeatedly failed..." — остановись и дай чёткие рекомендации что проверить.

Важно про лимиты:
- Если модельный лимит (429) — завершайся кратко и не пытайся делать ещё шаги.
""".strip()
    )

    agent = create_react_agent(model=llm, tools=[run_remote])
    return agent, system


# -------------------------
# Reporting
# -------------------------


def write_report(goal: str, host: str, user: str, final_text: str) -> Path:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"report_{host}_{ts}.md"

    lines: List[str] = []
    lines.append("# Agent report")
    lines.append("")
    lines.append(f"- Goal: {goal}")
    lines.append(f"- Host: `{host}`")
    lines.append(f"- User: `{user}`")
    lines.append(f"- Model: `{OPENROUTER_MODEL}`")
    lines.append(f"- Steps: {len(RUN_STEPS)}")
    lines.append("")

    for i, s in enumerate(RUN_STEPS, 1):
        lines.append(f"## Step {i}")
        lines.append(f"**Command:** `{s.get('cmd', '')}`")
        lines.append(
            f"- Exit: `{s.get('exit_code')}`  OK: `{s.get('ok')}`  Duration: `{round(float(s.get('duration_s', 0.0)), 3)}s`"
        )
        if s.get("error"):
            lines.append("")
            lines.append("**Policy/Error:**")
            lines.append("```")
            lines.append(str(s["error"]))
            lines.append("```")
        if not s.get("ok", False):
            lines.append("")
            lines.append("### STDOUT (head)")
            lines.append("```")
            lines.append((s.get("stdout") or "").strip())
            lines.append("```")
            lines.append("### STDERR (head)")
            lines.append("```")
            lines.append((s.get("stderr") or "").strip())
            lines.append("```")
        lines.append("")

    lines.append("## Final agent message")
    lines.append("```")
    lines.append((final_text or "").strip())
    lines.append("```")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# -------------------------
# Run
# -------------------------


def run(
    goal: str,
    host: str,
    user: str,
    password: Optional[str],
    key: Optional[str],
    max_steps: int = 35,
) -> str:
    RUN_STEPS.clear()

    agent, system = make_agent(host=host, user=user, password=password, key_path=key)

    log(
        "agent_start",
        {"goal": goal, "host": host, "user": user, "max_steps": max_steps},
    )

    final = ""
    try:
        result = agent.invoke(
            {"messages": [system, ("user", goal)]},
            config={"recursion_limit": max_steps},
        )
        messages = result.get("messages", [])
        final = (
            str(messages[-1].content or "")
            if messages
            else "(No final message returned by agent.)"
        )

    except openai.RateLimitError as e:
        # ВАЖНО: не падаем. Сохраняем отчёт о том, что уже успели сделать.
        final = (
            "LLM rate limit hit (OpenRouter free tier). "
            "Commands already executed on the server are captured in the report.\n\n"
            f"Error: {e}"
        )
        log("llm_rate_limited", {"error": str(e)})

    except Exception as e:
        final = f"LLM call failed: {type(e).__name__}: {e}"
        log("llm_error", {"error": str(e), "type": type(e).__name__})

    report_path = write_report(goal=goal, host=host, user=user, final_text=final)
    log("report_written", {"path": str(report_path)})

    log("agent_done", {"final_len": len(final), "steps": len(RUN_STEPS)})
    return final + f"\n\n[Report saved to {report_path}]"


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--goal", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--password", default=None)
    p.add_argument("--key", default=None)
    p.add_argument("--max-steps", type=int, default=35)
    args = p.parse_args()

    print(
        run(
            goal=args.goal,
            host=args.host,
            user=args.user,
            password=args.password,
            key=args.key,
            max_steps=args.max_steps,
        )
    )
