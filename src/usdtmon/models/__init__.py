"""Database models.

Integrity rules live in PostgreSQL constraints, not only in application code:
unique blockchain event identity, one active subscription per (user, wallet,
asset), one notification per (subscription, transfer).
"""

from __future__ import annotations

from enum import StrEnum

from tortoise import fields
from tortoise.migrations.constraints import CheckConstraint, UniqueConstraint
from tortoise.models import Model

# uint256 fits into NUMERIC(78, 0).
AMOUNT_DIGITS = 78


class TelegramUser(Model):
    id = fields.BigIntField(primary_key=True)
    telegram_id = fields.BigIntField(unique=True)
    username = fields.CharField(max_length=64, null=True)
    # Set when Telegram reports that the user blocked the bot; cleared on /start.
    is_blocked = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)

    subscriptions: fields.ReverseRelation[Subscription]

    class Meta:
        table = "telegram_user"


class Cryptocurrency(Model):
    id = fields.IntField(primary_key=True)
    symbol = fields.CharField(max_length=16, unique=True)
    name = fields.CharField(max_length=64)
    decimals = fields.SmallIntField()
    minimum_notification_amount = fields.DecimalField(max_digits=38, decimal_places=18)
    enabled = fields.BooleanField(default=True)

    class Meta:
        table = "cryptocurrency"
        constraints = (
            CheckConstraint(
                check="minimum_notification_amount >= 0", name="ck_crypto_min_amount_non_negative"
            ),
            CheckConstraint(check="decimals >= 0 AND decimals <= 36", name="ck_crypto_decimals"),
        )


class Network(Model):
    id = fields.IntField(primary_key=True)
    code = fields.CharField(max_length=32, unique=True)
    name = fields.CharField(max_length=64)
    # Shown to users, e.g. "ERC-20", "TRC-20".
    token_standard = fields.CharField(max_length=16)
    # Explorer URL template with a "{tx}" placeholder.
    explorer_tx_url = fields.CharField(max_length=255)
    enabled = fields.BooleanField(default=True)
    # Seconds; NULL means use MONITOR_INTERVAL.
    polling_interval = fields.IntField(null=True)
    # Used only when the network runs in "confirmations" finality mode.
    confirmations_required = fields.IntField(default=0)

    class Meta:
        table = "network"


class NetworkAsset(Model):
    """The allowlist: one verified token contract per (cryptocurrency, network)."""

    id = fields.IntField(primary_key=True)
    cryptocurrency: fields.ForeignKeyRelation[Cryptocurrency] = fields.ForeignKeyField(
        "models.Cryptocurrency", related_name="assets", on_delete=fields.RESTRICT
    )
    network: fields.ForeignKeyRelation[Network] = fields.ForeignKeyField(
        "models.Network", related_name="assets", on_delete=fields.RESTRICT
    )
    # Normalized with the network adapter (lowercase hex on EVM, base58 on Tron).
    contract_address = fields.CharField(max_length=128)
    # NULL means "same as the cryptocurrency".
    decimals = fields.SmallIntField(null=True)
    enabled = fields.BooleanField(default=True)

    cryptocurrency_id: int
    network_id: int

    class Meta:
        table = "network_asset"
        unique_together = (("cryptocurrency", "network"), ("network", "contract_address"))

    def effective_decimals(self) -> int:
        """Requires ``cryptocurrency`` to be fetched when ``decimals`` is NULL."""
        if self.decimals is not None:
            return self.decimals
        return self.cryptocurrency.decimals


class Wallet(Model):
    id = fields.BigIntField(primary_key=True)
    network: fields.ForeignKeyRelation[Network] = fields.ForeignKeyField(
        "models.Network", related_name="wallets", on_delete=fields.RESTRICT
    )
    # As entered by the first subscriber (after validation).
    address = fields.CharField(max_length=128)
    normalized_address = fields.CharField(max_length=128)
    created_at = fields.DatetimeField(auto_now_add=True)

    network_id: int

    class Meta:
        table = "wallet"
        unique_together = (("network", "normalized_address"),)


class Subscription(Model):
    id = fields.BigIntField(primary_key=True)
    user: fields.ForeignKeyRelation[TelegramUser] = fields.ForeignKeyField(
        "models.TelegramUser", related_name="subscriptions", on_delete=fields.CASCADE
    )
    wallet: fields.ForeignKeyRelation[Wallet] = fields.ForeignKeyField(
        "models.Wallet", related_name="subscriptions", on_delete=fields.RESTRICT
    )
    network_asset: fields.ForeignKeyRelation[NetworkAsset] = fields.ForeignKeyField(
        "models.NetworkAsset", related_name="subscriptions", on_delete=fields.RESTRICT
    )
    active = fields.BooleanField(default=True)
    # Finalized block at subscription time: only transfers in later blocks notify.
    start_block = fields.BigIntField()
    created_at = fields.DatetimeField(auto_now_add=True)
    removed_at = fields.DatetimeField(null=True)

    user_id: int
    wallet_id: int
    network_asset_id: int

    class Meta:
        table = "subscription"
        constraints = (
            UniqueConstraint(
                fields=("user_id", "wallet_id", "network_asset_id"),
                name="uq_subscription_active",
                condition="active",
            ),
        )
        indexes = (("wallet_id", "network_asset_id", "active"),)


class Transfer(Model):
    """A qualifying incoming token transfer (one log event)."""

    id = fields.BigIntField(primary_key=True)
    network: fields.ForeignKeyRelation[Network] = fields.ForeignKeyField(
        "models.Network", related_name="transfers", on_delete=fields.RESTRICT
    )
    network_asset: fields.ForeignKeyRelation[NetworkAsset] = fields.ForeignKeyField(
        "models.NetworkAsset", related_name="transfers", on_delete=fields.RESTRICT
    )
    wallet: fields.ForeignKeyRelation[Wallet] = fields.ForeignKeyField(
        "models.Wallet", related_name="transfers", on_delete=fields.RESTRICT
    )
    transaction_hash = fields.CharField(max_length=128)
    event_index = fields.IntField()
    from_address = fields.CharField(max_length=128)
    to_address = fields.CharField(max_length=128)
    amount_raw = fields.DecimalField(max_digits=AMOUNT_DIGITS, decimal_places=0)
    block_number = fields.BigIntField()
    block_timestamp = fields.DatetimeField()
    created_at = fields.DatetimeField(auto_now_add=True)

    network_id: int
    network_asset_id: int
    wallet_id: int

    class Meta:
        table = "transfer"
        # Blockchain event identity.
        unique_together = (("network", "transaction_hash", "event_index"),)
        constraints = (CheckConstraint(check="amount_raw > 0", name="ck_transfer_amount_positive"),)


class MonitoringCursor(Model):
    """Last fully processed block for one (asset, wallet) pair."""

    id = fields.BigIntField(primary_key=True)
    network_asset: fields.ForeignKeyRelation[NetworkAsset] = fields.ForeignKeyField(
        "models.NetworkAsset", related_name="cursors", on_delete=fields.CASCADE
    )
    wallet: fields.ForeignKeyRelation[Wallet] = fields.ForeignKeyField(
        "models.Wallet", related_name="cursors", on_delete=fields.CASCADE
    )
    last_processed_block = fields.BigIntField()
    updated_at = fields.DatetimeField(auto_now=True)

    network_asset_id: int
    wallet_id: int

    class Meta:
        table = "monitoring_cursor"
        unique_together = (("network_asset", "wallet"),)


class NotificationStatus(StrEnum):
    PENDING = "pending"
    # Claimed by a notifier; delivery outcome unknown until the row is updated.
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class Notification(Model):
    """Transactional outbox row: one message per (subscription, transfer)."""

    id = fields.BigIntField(primary_key=True)
    subscription: fields.ForeignKeyRelation[Subscription] = fields.ForeignKeyField(
        "models.Subscription", related_name="notifications", on_delete=fields.CASCADE
    )
    transfer: fields.ForeignKeyRelation[Transfer] = fields.ForeignKeyField(
        "models.Transfer", related_name="notifications", on_delete=fields.CASCADE
    )
    status = fields.CharEnumField(NotificationStatus, default=NotificationStatus.PENDING)
    attempts = fields.IntField(default=0)
    next_retry_at = fields.DatetimeField()
    # Claim lease of the notifier currently sending this row.
    locked_until = fields.DatetimeField(null=True)
    # True when a previous attempt may have reached Telegram (crash or timeout).
    delivery_uncertain = fields.BooleanField(default=False)
    sent_at = fields.DatetimeField(null=True)
    telegram_message_id = fields.BigIntField(null=True)
    last_error = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    subscription_id: int
    transfer_id: int

    class Meta:
        table = "notification"
        unique_together = (("subscription", "transfer"),)
        indexes = (("status", "next_retry_at"),)


class WorkerLease(Model):
    """Time-limited exclusive lease, e.g. "monitor:asset:1", held by one worker."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=128, unique=True)
    owner = fields.CharField(max_length=128)
    expires_at = fields.DatetimeField()
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "worker_lease"


__all__ = [
    "Cryptocurrency",
    "MonitoringCursor",
    "Network",
    "NetworkAsset",
    "Notification",
    "NotificationStatus",
    "Subscription",
    "TelegramUser",
    "Transfer",
    "Wallet",
    "WorkerLease",
]
