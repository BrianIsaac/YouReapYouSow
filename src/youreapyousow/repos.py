"""Typed repositories for every current-state record, on one database."""

from pydantic import BaseModel

from youreapyousow.authority.grants import AuthorityGrant
from youreapyousow.authority.lifecycle import LifecycleRules
from youreapyousow.domain import (
    Deployment,
    LandedQuote,
    Objective,
    Observation,
    Order,
    PolicyDecision,
    PurchaseIntent,
    Quote,
)
from youreapyousow.procure.need import Need
from youreapyousow.store import Database, Records


class ObjectiveLifecycle(BaseModel):
    """Lifecycle rules stored against an objective.

    Attributes:
        objective_id: The objective.
        rules: Its rollback and termination settings.
    """

    objective_id: str
    rules: LifecycleRules


class ReapOperator(BaseModel):
    """The operator's Reap cardholder and account, created once and reused.

    Attributes:
        user_id: The KYC-approved cardholder of record.
        account_id: Their account; each objective's card draws on it.
    """

    user_id: str
    account_id: str


class Repositories:
    """One ``Records`` per kind of record."""

    def __init__(self, db: Database) -> None:
        """Bind every repository to the shared database.

        Args:
            db: The database.
        """
        self.db = db
        self.objectives = Records(db, "objective", Objective)
        self.grants = Records(db, "grant", AuthorityGrant)
        self.lifecycles = Records(db, "lifecycle", ObjectiveLifecycle)
        self.quotes = Records(db, "quote", Quote)
        self.needs = Records(db, "need", Need)
        self.landed_quotes = Records(db, "landed_quote", LandedQuote)
        self.orders = Records(db, "order", Order)
        self.intents = Records(db, "intent", PurchaseIntent)
        self.decisions = Records(db, "decision", PolicyDecision)
        self.deployments = Records(db, "deployment", Deployment)
        self.observations = Records(db, "observation", Observation)
        self.reap_operator = Records(db, "reap_operator", ReapOperator)
