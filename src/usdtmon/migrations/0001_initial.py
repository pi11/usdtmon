from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from usdtmon.models import NotificationStatus
from tortoise import fields
from tortoise.indexes import Index
from tortoise.migrations.constraints import CheckConstraint, UniqueConstraint

# NOTE: CreateModel ignores Meta.constraints in tortoise-orm 1.1, so the CHECK and
# partial UNIQUE constraints are applied with explicit AddConstraint operations.

class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name='Cryptocurrency',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('symbol', fields.CharField(unique=True, max_length=16)),
                ('name', fields.CharField(max_length=64)),
                ('decimals', fields.SmallIntField()),
                ('minimum_notification_amount', fields.DecimalField(max_digits=38, decimal_places=18)),
                ('enabled', fields.BooleanField(default=True)),
            ],
            options={'table': 'cryptocurrency', 'app': 'models', 'pk_attr': 'id'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='Network',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('code', fields.CharField(unique=True, max_length=32)),
                ('name', fields.CharField(max_length=64)),
                ('token_standard', fields.CharField(max_length=16)),
                ('explorer_tx_url', fields.CharField(max_length=255)),
                ('enabled', fields.BooleanField(default=True)),
                ('polling_interval', fields.IntField(null=True)),
                ('confirmations_required', fields.IntField(default=0)),
            ],
            options={'table': 'network', 'app': 'models', 'pk_attr': 'id'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='NetworkAsset',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('cryptocurrency', fields.ForeignKeyField('models.Cryptocurrency', source_field='cryptocurrency_id', db_constraint=True, to_field='id', related_name='assets', on_delete=OnDelete.RESTRICT)),
                ('network', fields.ForeignKeyField('models.Network', source_field='network_id', db_constraint=True, to_field='id', related_name='assets', on_delete=OnDelete.RESTRICT)),
                ('contract_address', fields.CharField(max_length=128)),
                ('decimals', fields.SmallIntField(null=True)),
                ('enabled', fields.BooleanField(default=True)),
            ],
            options={'table': 'network_asset', 'app': 'models', 'unique_together': (('cryptocurrency', 'network'), ('network', 'contract_address')), 'pk_attr': 'id', 'table_description': 'The allowlist: one verified token contract per (cryptocurrency, network).'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='TelegramUser',
            fields=[
                ('id', fields.BigIntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('telegram_id', fields.BigIntField(unique=True)),
                ('username', fields.CharField(null=True, max_length=64)),
                ('is_blocked', fields.BooleanField(default=False)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'telegram_user', 'app': 'models', 'pk_attr': 'id'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='Wallet',
            fields=[
                ('id', fields.BigIntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('network', fields.ForeignKeyField('models.Network', source_field='network_id', db_constraint=True, to_field='id', related_name='wallets', on_delete=OnDelete.RESTRICT)),
                ('address', fields.CharField(max_length=128)),
                ('normalized_address', fields.CharField(max_length=128)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'wallet', 'app': 'models', 'unique_together': (('network', 'normalized_address'),), 'pk_attr': 'id'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='MonitoringCursor',
            fields=[
                ('id', fields.BigIntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('network_asset', fields.ForeignKeyField('models.NetworkAsset', source_field='network_asset_id', db_constraint=True, to_field='id', related_name='cursors', on_delete=OnDelete.CASCADE)),
                ('wallet', fields.ForeignKeyField('models.Wallet', source_field='wallet_id', db_constraint=True, to_field='id', related_name='cursors', on_delete=OnDelete.CASCADE)),
                ('last_processed_block', fields.BigIntField()),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={'table': 'monitoring_cursor', 'app': 'models', 'unique_together': (('network_asset', 'wallet'),), 'pk_attr': 'id', 'table_description': 'Last fully processed block for one (asset, wallet) pair.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='Subscription',
            fields=[
                ('id', fields.BigIntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('user', fields.ForeignKeyField('models.TelegramUser', source_field='user_id', db_constraint=True, to_field='id', related_name='subscriptions', on_delete=OnDelete.CASCADE)),
                ('wallet', fields.ForeignKeyField('models.Wallet', source_field='wallet_id', db_constraint=True, to_field='id', related_name='subscriptions', on_delete=OnDelete.RESTRICT)),
                ('network_asset', fields.ForeignKeyField('models.NetworkAsset', source_field='network_asset_id', db_constraint=True, to_field='id', related_name='subscriptions', on_delete=OnDelete.RESTRICT)),
                ('active', fields.BooleanField(default=True)),
                ('start_block', fields.BigIntField()),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('removed_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
            ],
            options={'table': 'subscription', 'app': 'models', 'indexes': [Index(fields=['wallet_id', 'network_asset_id', 'active'])], 'pk_attr': 'id'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='Transfer',
            fields=[
                ('id', fields.BigIntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('network', fields.ForeignKeyField('models.Network', source_field='network_id', db_constraint=True, to_field='id', related_name='transfers', on_delete=OnDelete.RESTRICT)),
                ('network_asset', fields.ForeignKeyField('models.NetworkAsset', source_field='network_asset_id', db_constraint=True, to_field='id', related_name='transfers', on_delete=OnDelete.RESTRICT)),
                ('wallet', fields.ForeignKeyField('models.Wallet', source_field='wallet_id', db_constraint=True, to_field='id', related_name='transfers', on_delete=OnDelete.RESTRICT)),
                ('transaction_hash', fields.CharField(max_length=128)),
                ('event_index', fields.IntField()),
                ('from_address', fields.CharField(max_length=128)),
                ('to_address', fields.CharField(max_length=128)),
                ('amount_raw', fields.DecimalField(max_digits=78, decimal_places=0)),
                ('block_number', fields.BigIntField()),
                ('block_timestamp', fields.DatetimeField(auto_now=False, auto_now_add=False)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'transfer', 'app': 'models', 'unique_together': (('network', 'transaction_hash', 'event_index'),), 'pk_attr': 'id', 'table_description': 'A qualifying incoming token transfer (one log event).'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='Notification',
            fields=[
                ('id', fields.BigIntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('subscription', fields.ForeignKeyField('models.Subscription', source_field='subscription_id', db_constraint=True, to_field='id', related_name='notifications', on_delete=OnDelete.CASCADE)),
                ('transfer', fields.ForeignKeyField('models.Transfer', source_field='transfer_id', db_constraint=True, to_field='id', related_name='notifications', on_delete=OnDelete.CASCADE)),
                ('status', fields.CharEnumField(default=NotificationStatus.PENDING, description='PENDING: pending\nSENDING: sending\nSENT: sent\nFAILED: failed', enum_type=NotificationStatus, max_length=7)),
                ('attempts', fields.IntField(default=0)),
                ('next_retry_at', fields.DatetimeField(auto_now=False, auto_now_add=False)),
                ('locked_until', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
                ('delivery_uncertain', fields.BooleanField(default=False)),
                ('sent_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
                ('telegram_message_id', fields.BigIntField(null=True)),
                ('last_error', fields.TextField(null=True, unique=False)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'notification', 'app': 'models', 'unique_together': (('subscription', 'transfer'),), 'indexes': [Index(fields=['status', 'next_retry_at'])], 'pk_attr': 'id', 'table_description': 'Transactional outbox row: one message per (subscription, transfer).'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='WorkerLease',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('name', fields.CharField(unique=True, max_length=128)),
                ('owner', fields.CharField(max_length=128)),
                ('expires_at', fields.DatetimeField(auto_now=False, auto_now_add=False)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={'table': 'worker_lease', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Time-limited exclusive lease, e.g. "monitor:asset:1", held by one worker.'},
            bases=['Model'],
        ),
        ops.AddConstraint(model_name='Cryptocurrency', constraint=CheckConstraint(check='minimum_notification_amount >= 0', name='ck_crypto_min_amount_non_negative')),
        ops.AddConstraint(model_name='Cryptocurrency', constraint=CheckConstraint(check='decimals >= 0 AND decimals <= 36', name='ck_crypto_decimals')),
        ops.AddConstraint(model_name='Subscription', constraint=UniqueConstraint(fields=('user_id', 'wallet_id', 'network_asset_id'), name='uq_subscription_active', condition='active')),
        ops.AddConstraint(model_name='Transfer', constraint=CheckConstraint(check='amount_raw > 0', name='ck_transfer_amount_positive')),
    ]
