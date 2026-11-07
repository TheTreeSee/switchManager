"""Staged apply to one switch, with an isolated log and explicit result states.

Order per switch:
  1. parse + validate the CSV; connect; refresh the live interface map; render; write output/<ip>.cfg
  2. (optional) preview + confirmation
  3. (optional, --rollback-timer) check no reload is pending, then 'reload in <minutes>'
  4. send stage 'base' (VLANs), 'access' (access ports), then 'management' (SVIs, gateway, routing, trunks)
  5. check the session still works (reconnect if needed), save, verify the startup config
  6. 'reload cancel' when a timer was set; optional TFTP backup
"""

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .apply_log import ApplyLog, sha256, utc_now
from .backup import run_backup
from .config import Settings
from .csv_parser import parse_switch_csv
from .discovery import discover_device
from .interface_map import InterfaceMap
from .models import ValidationError
from .netmiko_client import connect, is_connection_error
from .renderer import RenderedConfig, render

SUCCESS = "SUCCESS"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
CONNECTION_LOST_ROLLBACK_PENDING = "CONNECTION_LOST_ROLLBACK_PENDING"
CONNECTION_LOST_STATE_UNKNOWN = "CONNECTION_LOST_STATE_UNKNOWN"
SAVE_RESULT_UNKNOWN = "SAVE_RESULT_UNKNOWN"
RELOAD_CANCEL_UNKNOWN = "RELOAD_CANCEL_UNKNOWN"
UNCERTAIN = {CONNECTION_LOST_ROLLBACK_PENDING, CONNECTION_LOST_STATE_UNKNOWN, SAVE_RESULT_UNKNOWN, RELOAD_CANCEL_UNKNOWN}

# CLI error lines start with '%' ('% Invalid input detected', '%Error ...'); '% Warning' is not fatal
CLI_ERROR = re.compile(r"^\s*%(?!\s*Warning)", re.MULTILINE | re.IGNORECASE)
NO_RELOAD = re.compile(r"No reload is scheduled", re.IGNORECASE)

RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY = 10


@dataclass
class ApplyResult:
    ip: str
    status: str
    message: str
    log_path: Path | None = None
    output_path: Path | None = None
    backup_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status in (SUCCESS, SKIPPED)


class _Abort(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def cli_errors(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if CLI_ERROR.match(line)]


def write_output(settings: Settings, ip: str, rendered: RenderedConfig) -> Path:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    path = settings.output_dir / f"{ip}.cfg"
    path.write_text(rendered.text, encoding="utf-8")
    return path


def schedule_reload(conn, minutes: int) -> str:
    """'reload in N' without saving the running config (the reload falls back to startup-config)."""
    output = conn.send_command_timing(f"reload in {minutes}", strip_prompt=False, strip_command=False)
    full = output
    for _ in range(4):
        last = output.strip().splitlines()[-1] if output.strip() else ""
        if re.search(r"save\?|\[yes/no\]", last, re.IGNORECASE):
            output = conn.send_command_timing("no", strip_prompt=False, strip_command=False)
        elif "[confirm]" in last:
            output = conn.send_command_timing("\n", strip_prompt=False, strip_command=False)
        else:
            break
        full += output
    return full


def preview_text(ip: str, hostname: str, spec, rendered: RenderedConfig, csv_path: Path, output_path: Path | None,
                 rollback_minutes: int | None, model: str = "") -> str:
    lines = [
        f"Target      : {ip} {hostname or ''} {('(' + model + ')') if model else ''}".rstrip(),
        f"Input       : {csv_path}",
        f"Output      : {output_path or '(not written yet)'}",
        f"Mode        : {'Layer 3 - ip routing' if spec.layer3 else 'Layer 2'}"
        + (f", default gateway {spec.default_gateway}" if spec.default_gateway else ""),
        f"VLANs       : {len(spec.vlans)}   access port groups: {len(spec.access_groups)}   trunk groups: {len(spec.trunk_groups)}",
        f"Validation  : OK ({sum(len(s.commands) for s in rendered.stages)} commands)",
        f"Mgmt path   : {'YES - SVIs/gateway/routing/trunks are sent last' if rendered.has_management_changes else 'no changes'}",
    ]
    if rendered.has_management_changes:
        if rollback_minutes:
            lines.append(f"Rollback    : 'reload in {rollback_minutes}' before changes, cancelled after a verified save.")
            lines.append("              WARNING: if the reload happens, unsaved running-config changes (also older ones) are lost.")
        else:
            lines.append("Rollback    : none (use --rollback-timer <minutes> for a reload safety net)")
    for warning in rendered.warnings:
        lines.append(f"Warning     : {warning}")
    return "\n".join(lines)


def ask_confirmation(question: str) -> bool:
    try:
        return input(f"{question} Type 'yes' to continue: ").strip().lower() == "yes"
    except EOFError:
        return False


def apply_to_switch(ip: str, csv_path: Path, settings: Settings, *, hostname: str = "", set_hostname: bool = False,
                    rollback_minutes: int | None = None, confirm: bool = True, expected_body: str | None = None,
                    backup: bool = False, connector: Callable = connect, sleep: Callable = time.sleep,
                    echo: Callable[[str], None] = print) -> ApplyResult:
    log = ApplyLog(settings.logs_dir, ip)
    result = ApplyResult(ip, FAILED, "", log.path)
    conn = None
    timer_set = False
    reload_at = ""
    sent_any = False
    save_sent = False
    try:
        log.event(f"target {ip}, inventory hostname '{hostname}'")
        log.event(f"input {csv_path} sha256 {sha256(csv_path)}")
        log.event(f"start {utc_now()}")

        # 1. parse, connect, live interface map, render
        try:
            spec = parse_switch_csv(csv_path, settings.mgmt_gateway)
        except ValidationError as exc:
            raise _Abort(FAILED, "validation failed: " + "; ".join(exc.problems))

        try:
            conn = connector(ip, settings)
        except Exception as exc:
            raise _Abort(FAILED, f"connection failed: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
        log.event(f"connected, prompt {conn.find_prompt()}")

        live = discover_device(conn)
        log.block("live interface map (show ip interface brief)", "\n".join(live["interfaces"]))
        try:
            rendered = render(spec, InterfaceMap(live["interfaces"]), ip, hostname=hostname or live["hostname"],
                              model=live["model"], trunk_encapsulation=settings.trunk_encapsulation, set_hostname=set_hostname)
        except ValidationError as exc:
            raise _Abort(FAILED, "validation against the live interfaces failed: " + "; ".join(exc.problems))
        output_path = write_output(settings, ip, rendered)
        result.output_path = output_path
        log.event(f"output {output_path} sha256 {sha256(output_path)}")
        log.event("validation OK")

        if expected_body is not None and rendered.body() != expected_body:
            raise _Abort(FAILED, "the live interface map differs from the saved discovery the batch was confirmed with - run --detect and try again")

        # 2. confirmation
        if confirm:
            echo(preview_text(ip, hostname or live["hostname"], spec, rendered, csv_path, output_path, rollback_minutes, live["model"]))
            if not ask_confirmation(f"Apply this configuration to {ip}?"):
                log.event("operator declined - nothing sent")
                raise _Abort(SKIPPED, "not confirmed - nothing sent")

        # 3. rollback timer
        if rollback_minutes and rendered.has_management_changes:
            pending = conn.send_command("show reload")
            log.exchange("preflight", "show reload", pending)
            if not NO_RELOAD.search(pending):
                raise _Abort(FAILED, "a reload is already scheduled on the switch - not replacing it (check 'show reload')")
            output = schedule_reload(conn, rollback_minutes)
            log.exchange("rollback timer", f"reload in {rollback_minutes}", output)
            check = conn.send_command("show reload")
            log.exchange("rollback timer check", "show reload", check)
            if NO_RELOAD.search(check):
                raise _Abort(FAILED, "could not schedule the rollback reload - nothing changed")
            timer_set = True
            reload_at = check.strip().splitlines()[0] if check.strip() else f"in {rollback_minutes} minutes"
            log.event(f"rollback reload scheduled: {reload_at}")

        # 4. stages
        for stage in rendered.stages:
            if not stage.commands:
                continue
            sent_any = True
            log.event(f"sending stage '{stage.name}' ({len(stage.commands)} commands)")
            output = conn.send_config_set(stage.commands, read_timeout=120)
            log.exchange(f"stage {stage.name}", stage.commands, output)
            errors = cli_errors(output)
            if errors:
                pending = f" - the rollback reload ({reload_at}) is still scheduled and will undo this" if timer_set else " - running config is partly changed and NOT saved"
                raise _Abort(FAILED, f"switch rejected commands in stage '{stage.name}': {errors[0]}{pending}")

        # 5. session still usable? (management changes can drop it)
        conn = ensure_session(conn, ip, settings, connector, log, sleep)

        save_sent = True
        log.event("saving configuration")
        output = conn.save_config()
        log.exchange("save", "write memory", output)
        if not re.search(r"\[OK\]|\bOK\b", output):
            raise _Abort(FAILED, "save was not confirmed by the switch" + (f" - rollback reload ({reload_at}) still scheduled" if timer_set else ""))
        verify_saved(conn, rendered, log)

        # 6. cancel the rollback reload
        if timer_set:
            try:
                output = conn.send_command_timing("reload cancel", strip_prompt=False, strip_command=False)
                check = conn.send_command("show reload")
                log.exchange("reload cancel", ["reload cancel", "show reload"], output + "\n" + check)
            except Exception as exc:
                log.exception(exc)
                raise _Abort(RELOAD_CANCEL_UNKNOWN, f"saved, but 'reload cancel' could not be confirmed - the switch may still reload ({reload_at}); reconnect and run 'reload cancel'")
            if not NO_RELOAD.search(check):
                raise _Abort(RELOAD_CANCEL_UNKNOWN, f"saved, but a reload is still scheduled ({reload_at}) - reconnect and run 'reload cancel'")
            timer_set = False

        message = "applied and saved"
        if backup:
            try:
                result.backup_path = run_backup(ip, settings, conn=conn, hostname=hostname or live["hostname"], log=log)
                message += f", backup {result.backup_path.name}"
                log.event(f"backup written to {result.backup_path}")
            except Exception as exc:
                log.exception(exc)
                message += f" (backup FAILED: {str(exc).splitlines()[0]})"
        result.status, result.message = SUCCESS, message

    except _Abort as abort:
        result.status, result.message = abort.status, abort.message
    except Exception as exc:
        log.exception(exc)
        if is_connection_error(exc) and sent_any:
            if save_sent:
                result.status = SAVE_RESULT_UNKNOWN
                result.message = "connection lost after 'write memory' was sent - the startup config may or may not contain the change; verify on the switch"
                if timer_set:
                    result.message += f". A reload is scheduled ({reload_at}) but is NOT a guaranteed rollback here"
            elif timer_set:
                result.status = CONNECTION_LOST_ROLLBACK_PENDING
                result.message = f"connection lost before saving - the switch should reload to its saved config ({reload_at}) unless someone intervenes"
            else:
                result.status = CONNECTION_LOST_STATE_UNKNOWN
                result.message = "connection lost while configuring, no rollback timer - check the switch out-of-band"
            result.message += f"; last confirmed step: {log.last_confirmed}"
        else:
            result.status = FAILED
            result.message = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    finally:
        log.event(f"result {result.status}: {result.message}")
        log.event(f"end {utc_now()}")
        log.close()
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass
    return result


def ensure_session(conn, ip: str, settings: Settings, connector: Callable, log: ApplyLog, sleep: Callable):
    """Return a working session: the current one, or a new one after the management change."""
    try:
        conn.find_prompt()
        return conn
    except Exception as exc:
        if not is_connection_error(exc):
            raise
        log.event(f"session lost after configuration ({type(exc).__name__}), reconnecting")
    last_exc: BaseException | None = None
    for attempt in range(1, RECONNECT_ATTEMPTS + 1):
        sleep(RECONNECT_DELAY)
        try:
            new = connector(ip, settings)
            log.event(f"reconnected on attempt {attempt}")
            return new
        except Exception as exc:
            last_exc = exc
            log.event(f"reconnect attempt {attempt} failed: {type(exc).__name__}")
    from .netmiko_client import ConnectionLost
    raise ConnectionLost(f"could not reconnect to {ip}") from last_exc


def verify_saved(conn, rendered: RenderedConfig, log: ApplyLog) -> None:
    """The startup config must contain the key management-path lines that were sent."""
    startup = conn.send_command("show startup-config", read_timeout=60)
    log.block("verify: show startup-config", startup)
    wanted = [c for s in rendered.stages for c in s.commands
              if not c.startswith(" ") and (c.startswith(("ip routing", "ip default-gateway", "interface Vlan", "vlan ", "hostname ")))]
    present = {line.rstrip() for line in startup.splitlines()}
    # VLANs live in vlan.dat on many switches, not in startup-config: only check them when VTP shows them there
    missing = [c for c in wanted if c not in present and not c.startswith("vlan ")]
    if missing:
        raise _Abort(FAILED, f"saved, but the startup config does not contain: {', '.join(missing[:3])}")
    log.event("verify: startup config contains the management-path lines")
