"""A hand-recorded capability for the Meridian dashboard - a TEST FIXTURE.

This lives under tests/, deliberately, and not inside the `cua` package. Everything in
`cua` is a general computer-use engine that must know nothing about any particular
application; the moment target-specific knowledge - captions, member numbers, account
types - sits inside it, the claim that a recording is portable stops being testable.
Knowledge of a target belongs to whoever is driving the engine: the discovery loop's
model at record time, and fixtures like this one at test time.

It is the shape the LLM discovery loop emits, written by hand so the schema, binding,
storage and replay contract could be validated against a live surface before a model was
involved - and kept, because it gives the suite a real artifact whose locators come from
genuine observations rather than hand-authored guesses.
"""
from __future__ import annotations


from cua.artifact.schema import (ApprovalState, Capability, InputParam, KnownOutcome,
                                 OutputField, ParamType, Provenance, RecoverableCondition,
                                 RiskClass, Step, SurfaceBinding)
from cua.artifact.store import Store
from cua.surfaces import locators
from cua.surfaces.models import (Checkpoint, Click, Fill, Navigate, Read, Role)
from cua.surfaces.web import PlaywrightSurface

def build(base_url: str) -> Capability:
    entry = f"{base_url}/meridian/"
    with PlaywrightSurface() as s:
        s.act(Navigate(url=entry))
        home = s.observe()

        member_field = next(e for e in home.elements if e.role is Role.TEXTBOX
                            and (e.label_text or "").startswith("Member / Name"))
        search_btn = next(e for e in home.elements if e.role is Role.LINK
                          and e.name == "Search")

        s.act(Fill(locator=locators.build(member_field), text="12345"))
        s.act(Click(locator=locators.build(search_btn)))
        results = s.observe()
        select_link = next(e for e in results.elements if e.role is Role.LINK
                           and e.name == "Select")

        s.act(Click(locator=locators.build(select_link,
                                           scope=locators.row_scope("12345"))))
        detail = s.observe()
        name_span = next(e for e in detail.elements if e.role is Role.TEXT
                         and (e.label_text or "") == "Name:")
        balance_cell = next(e for e in detail.elements if e.role is Role.CELL
                            and e.column_header == "Current Balance")
        status_cell = next(e for e in detail.elements if e.role is Role.CELL
                           and e.column_header == "Status")

        # Locators built from live observations, then parameterised.
        member_loc = locators.build(member_field, "Member / Name input")
        search_loc = locators.build(search_btn, "Search button")
        select_loc = locators.build(select_link, "Select link for the requested member",
                                    scope=locators.row_scope("{member_id}",
                                                             "member number"))
        name_loc = locators.build(name_span, "member full name")
        balance_loc = locators.build(balance_cell, "balance of the requested account",
                                     scope=locators.row_scope("{account_type}",
                                                              "account type"))
        status_loc = locators.build(status_cell, "status of the requested account",
                                    scope=locators.row_scope("{account_type}",
                                                             "account type"))
        weakest = min(locators.durability(l) for l in
                      (member_loc, search_loc, select_loc, name_loc, balance_loc, status_loc))

    return Capability(
        id="meridian.read_account_balance",
        name="Read a member's account balance",
        description=("Look up a member in Meridian Core Servicing and return the balance "
                     "and status of one of their accounts."),
        surface=SurfaceBinding(kind="web", entry_url=entry, tenant="demo",
                               variant="meridian-core"),
        inputs=[
            InputParam(name="member_id", type=ParamType.STRING, pattern=r"\d{4,10}",
                       example="12345",
                       description="The member number to look up."),
            InputParam(name="account_type", type=ParamType.ENUM,
                       enum_values=["Savings", "Checking", "Certificate"],
                       example="Savings",
                       description="Which of the member's accounts to report on."),
        ],
        outputs=[
            OutputField(name="member_name", description="Member's full name.",
                        source_step=4),
            OutputField(name="balance", type=ParamType.DECIMAL, source_step=5,
                        description="Current balance of the requested account."),
            OutputField(name="account_status", source_step=6,
                        description="Status of the requested account, e.g. Open."),
        ],
        steps=[
            Step(index=0, intent="Open the servicing application.",
                 action=Navigate(url=entry), risk=RiskClass.SAFE),
            Step(index=1, intent="Enter the member number to search for.",
                 action=Fill(locator=member_loc, text="{member_id}"),
                 risk=RiskClass.SAFE),
            Step(index=2, intent="Run the member search.",
                 action=Click(locator=search_loc), risk=RiskClass.SAFE,
                 checkpoint=Checkpoint(description="A results grid is displayed.",
                                       text_present=["Search Results"])),
            Step(index=3, intent="Open the matching member's record.",
                 action=Click(locator=select_loc), risk=RiskClass.SAFE,
                 checkpoint=Checkpoint(description="The member detail screen is shown.",
                                       text_present=["Member Detail"])),
            Step(index=4, intent="Read the member's name for the caller.",
                 action=Read(locator=name_loc, output="member_name"),
                 risk=RiskClass.SAFE),
            Step(index=5, intent="Read the balance of the requested account.",
                 action=Read(locator=balance_loc, output="balance"),
                 risk=RiskClass.SAFE),
            Step(index=6, intent="Read the status of the requested account.",
                 action=Read(locator=status_loc, output="account_status"),
                 risk=RiskClass.SAFE),
        ],
        success=Checkpoint(
            description="The member's detail screen is displayed with no error banner.",
            text_present=["Member Detail", "Account Relationships"],
            text_absent=["No member records match", "Server Error"]),
        known_outcomes=[
            KnownOutcome(
                name="member_not_found",
                description="No member matches the supplied number. A legitimate answer "
                            "for the caller, not a failure of the automation.",
                signature=Checkpoint(description="The no-match message is shown.",
                                     text_present=["No member records match"])),
        ],
        recoverable=[
            RecoverableCondition(
                name="session_expired",
                description="The servicing session timed out; sign on again and retry.",
                signature=Checkpoint(description="The session expired screen is shown.",
                                     text_present=["Session Expired"]),
                recovery=[Navigate(url=f"{base_url}/meridian/signon.aspx")],
                retry_from_step=0, max_attempts=2),
        ],
        approval=ApprovalState.DRAFT,
        provenance=Provenance(goal="Look up a member and read an account balance.",
                              model=None, discovery_steps=7,
                              weakest_locator=weakest),
    )


