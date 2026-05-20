#!/usr/bin/env python3
"""
NetMon - Shared SSH helper for Omada devices (router + EAPs).

Omada CLIs require legacy SSH algorithms and only accept password auth by default.
This module wraps both key-based (subprocess) and password-based (pexpect) flows
behind a single run_commands() entry point.
"""

import logging
import subprocess

SSH_BASE_OPTS = [
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group14-sha1",
    "-o", "Ciphers=+aes128-cbc,aes256-cbc,3des-cbc",
]


def build_ssh_cmd(ssh_target, password, port=None):
    """Build base SSH command list. With password we use pexpect; without we use key auth.
    Pass a non-default port (Omada site-wide setting) explicitly."""
    cmd = ["ssh"] + SSH_BASE_OPTS
    if port:
        cmd.extend(["-p", str(port)])
    if not password:
        cmd.extend(["-o", "BatchMode=yes"])
    cmd.append(ssh_target)
    return cmd


def _run_commands_pexpect(ssh_cmd, password, commands, timeout_sec=20):
    """Run commands over an interactive SSH session (password auth)."""
    try:
        import pexpect
    except ImportError:
        logging.error("pexpect not installed - needed for password SSH. "
                      "Install: apt install python3-pexpect")
        return None

    child = None
    outputs = []
    try:
        child = pexpect.spawn(
            ssh_cmd[0], ssh_cmd[1:],
            timeout=timeout_sec,
            encoding="utf-8",
            codec_errors="replace",
        )
        idx = child.expect([r"[Pp]assword:", r"[>#\$]", pexpect.TIMEOUT, pexpect.EOF], timeout=10)
        if idx == 0:
            child.send(password + "\r")
            idx = child.expect([r"[>#\$]", pexpect.TIMEOUT, pexpect.EOF], timeout=10)
            if idx != 0:
                return None
        elif idx != 1:
            return None

        if child.after == ">":
            child.send("enable\r")
            idx = child.expect([r"[Pp]assword:", r"#", r">", pexpect.TIMEOUT, pexpect.EOF], timeout=10)
            if idx == 0:
                child.send(password + "\r")
                idx = child.expect([r"#", r">", pexpect.TIMEOUT, pexpect.EOF], timeout=10)
                if idx not in (0, 1):
                    return None
            elif idx not in (1, 2):
                return None

        for command in commands:
            child.send(command + "\r")
            idx = child.expect([r"[>#\$]", pexpect.TIMEOUT, pexpect.EOF], timeout=timeout_sec)
            if idx != 0:
                return None
            outputs.append(child.before or "")

        child.send("exit\r")
        try:
            child.expect(pexpect.EOF, timeout=3)
        except Exception:
            pass
        return outputs
    except Exception as e:
        logging.warning("SSH pexpect session failed: %s", e)
        return None
    finally:
        if child is not None:
            child.close(force=True)


def _run_command_key(ssh_cmd, command, timeout=12):
    """Run a single command via key-based SSH."""
    try:
        out = subprocess.run(
            ssh_cmd + [command],
            capture_output=True, text=True, timeout=timeout
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logging.warning("SSH command failed: %s", e)
        return None


def run_commands(ssh_target, password, commands, timeout_sec=20, port=None):
    """
    Run one or more commands on a remote SSH target.
    Returns a list of stdout strings (one per command), or None on session failure.
    """
    if not ssh_target:
        return None
    ssh_cmd = build_ssh_cmd(ssh_target, password, port=port)
    if password:
        return _run_commands_pexpect(ssh_cmd + ["-tt"], password, commands, timeout_sec=timeout_sec)
    outputs = []
    for cmd in commands:
        out = _run_command_key(ssh_cmd, cmd, timeout=timeout_sec)
        if out is None:
            return None
        outputs.append(out)
    return outputs
