"""Command line entry point.

    cua discover  - run the LLM loop once against a live app, save a capability
    cua replay    - execute a saved capability deterministically, with arguments
    cua catalog   - list saved capabilities as an agent would see them
    cua show      - print a capability's contract for human review

`discover` is the only command that can reach a model; `replay` cannot, by
construction, because nothing on that path imports the discovery package.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .artifact.schema import ApprovalState
from .artifact.store import Store
from .config import ARTIFACTS, EVIDENCE, INTERVENTIONS, load_dotenv, provider
from .safety.policy import for_host
from .surfaces.web import PlaywrightSurface

app = typer.Typer(add_completion=False, help="Computer-use automation: record once, replay many.")


def _store(root: Optional[Path]) -> Store:
    return Store(root or ARTIFACTS)


def _parse_args(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"expected name=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


@app.command()
def discover(
    goal: str = typer.Option(..., help="What to accomplish, in plain language."),
    url: str = typer.Option(..., help="Entry point of the target application."),
    capability_id: str = typer.Option(..., "--id", help="Identifier for the artifact."),
    param: list[str] = typer.Option([], "--param",
                                    help="name=example — a value that becomes an input."),
    describe: list[str] = typer.Option([], "--describe", help="name=description."),
    secret: list[str] = typer.Option([], "--secret",
                                     help="Name of a parameter carrying sensitive data."),
    max_steps: int = typer.Option(18, help="Step budget before giving up."),
    headed: bool = typer.Option(False, help="Show the browser while it runs."),
    artifacts: Optional[Path] = typer.Option(None),
    evidence: Optional[Path] = typer.Option(None),
) -> None:
    """Run the LLM-driven loop once and save what it learned as a capability."""
    from .discovery.agent import DiscoveryAgent, DiscoveryConfig, DiscoveryFailed
    from .evidence.recorder import RunRecorder
    from .safety.redaction import Redactor
    from .safety.surface import PolicySurface

    load_dotenv()
    parameters = _parse_args(param)
    redactor = Redactor([parameters[n] for n in secret if n in parameters])
    recorder = RunRecorder(evidence or EVIDENCE, "discovery", redactor=redactor)
    llm = provider()
    typer.echo(f"model: {llm.model}   evidence: {recorder.dir}")

    surface = PlaywrightSurface(headless=not headed)
    guarded = PolicySurface(surface, for_host(url), redactor)
    try:
        capability = DiscoveryAgent(guarded, llm, recorder).run(DiscoveryConfig(
            goal=goal, entry_url=url, capability_id=capability_id,
            parameters=parameters, parameter_docs=_parse_args(describe),
            sensitive_parameters=set(secret), max_steps=max_steps))
    except DiscoveryFailed as exc:
        recorder.failure(str(exc), surface=guarded, label="discovery-failed")
        recorder.finish("failed")
        typer.echo(f"discovery failed: {exc}", err=True)
        raise typer.Exit(1)
    finally:
        guarded.close()

    problems = capability.validate_contract()
    for problem in problems:
        typer.echo(f"  contract warning: {problem}", err=True)
    path = _store(artifacts).save(capability)
    recorder.finish("success", capability=capability.id, artifact=str(path))
    typer.echo(f"saved {path}  (fingerprint {capability.fingerprint()})")
    typer.echo(f"steps: {len(capability.steps)}  outputs: "
               f"{[o.name for o in capability.outputs]}")


@app.command()
def replay(
    capability_id: str = typer.Argument(..., help="Capability to run."),
    arg: list[str] = typer.Option([], "--arg", help="name=value for a declared input."),
    version: Optional[int] = typer.Option(None, help="Pin a version (default: latest)."),
    headed: bool = typer.Option(False, help="Show the browser while it runs."),
    approve_risky: bool = typer.Option(False, help="Permit irreversible steps."),
    escalate: bool = typer.Option(False,
                                  help="On an unrecoverable step, hand the live session "
                                       "to a human operator and wait."),
    operator_timeout: float = typer.Option(300.0, help="Seconds to wait for an operator."),
    artifacts: Optional[Path] = typer.Option(None),
    evidence: Optional[Path] = typer.Option(None),
) -> None:
    """Execute a saved capability deterministically. No model is consulted."""
    from .replay.engine import ReplayEngine

    load_dotenv()
    capability = _store(artifacts).load(capability_id, version)
    approver = (lambda step, reason: True) if approve_risky else None

    from .safety.redaction import Redactor
    from .safety.surface import PolicySurface

    policy = for_host(capability.surface.entry_url)
    redactor = Redactor()
    surface = PlaywrightSurface(headless=not headed)
    active = PolicySurface(surface, policy, redactor, approver)

    if escalate:
        from .escalation.broker import InterventionBroker
        from .escalation.session import ControlledSurface
        # Control transfer wraps policy, so a human may act freely while every
        # automated action underneath still passes the allowlist.
        active = ControlledSurface(
            active, InterventionBroker(INTERVENTIONS),
            session_hint=("the visible browser window this run is driving" if headed
                          else "headless session - start with --headed to hand it over"),
            operator_timeout_s=operator_timeout)

    try:
        result = ReplayEngine(active, policy, evidence_root=evidence or EVIDENCE,
                              approver=approver).run(capability, _parse_args(arg))
    finally:
        active.close()

    typer.echo(result.summary())
    typer.echo(f"evidence: {result.evidence_dir}")
    raise typer.Exit(0 if result.ok else 1)


operator_app = typer.Typer(help="Mock operator console for human intervention.")
app.add_typer(operator_app, name="operator")


@operator_app.command("list")
def operator_list(queue: Optional[Path] = typer.Option(None)) -> None:
    """Show intervention requests waiting for a person."""
    from .escalation.broker import InterventionBroker

    requests = InterventionBroker(queue or INTERVENTIONS).open_requests()
    if not requests:
        typer.echo("no open interventions")
        return
    for request in requests:
        typer.echo(request.brief())
        typer.echo("")


@operator_app.command("take")
def operator_take(request_id: str, operator: str = typer.Option(..., "--as"),
                  queue: Optional[Path] = typer.Option(None)) -> None:
    """Take control of the live session an automation run has paused."""
    from .escalation.broker import InterventionBroker

    request = InterventionBroker(queue or INTERVENTIONS).claim(request_id, operator)
    typer.echo(f"{operator} now holds the session for {request.id}")
    typer.echo(f"session: {request.session_hint}")
    typer.echo("automation is paused; run 'cua operator release' when you are done")


@operator_app.command("release")
def operator_release(request_id: str, notes: str = typer.Option("", "--notes"),
                     queue: Optional[Path] = typer.Option(None)) -> None:
    """Hand control back so the run can resume on the same session."""
    from .escalation.broker import InterventionBroker

    request = InterventionBroker(queue or INTERVENTIONS).release(request_id, notes)
    typer.echo(f"control returned for {request.id}; the run may resume")


@app.command()
def catalog(artifacts: Optional[Path] = typer.Option(None)) -> None:
    """List saved capabilities as a calling agent would see them."""
    typer.echo(json.dumps(_store(artifacts).catalog(), indent=2))


@app.command()
def show(capability_id: str, version: Optional[int] = typer.Option(None),
         artifacts: Optional[Path] = typer.Option(None)) -> None:
    """Print a capability's contract for human review."""
    typer.echo(_store(artifacts).load(capability_id, version).review())


@app.command()
def approve(capability_id: str, artifacts: Optional[Path] = typer.Option(None)) -> None:
    """Mark a capability approved for unattended replay."""
    store = _store(artifacts)
    capability = store.load(capability_id)
    problems = capability.validate_contract()
    if problems:
        for problem in problems:
            typer.echo(f"  {problem}", err=True)
        typer.echo("refusing to approve a capability with contract problems", err=True)
        raise typer.Exit(1)
    capability.approval = ApprovalState.APPROVED
    typer.echo(f"approved: {store.save(capability)}")


if __name__ == "__main__":
    app()
