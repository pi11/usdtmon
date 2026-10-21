"""Seed the networks and the verified USDT contracts (the token allowlist).

Contract addresses are stored in the adapter's normalized form: lowercase hex
for Ethereum, canonical base58check for Tron.
"""

from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("models", "0001_initial")]

    operations = [
        ops.RunSQL(
            [
                """
                INSERT INTO network
                    (code, name, token_standard, explorer_tx_url, enabled,
                     polling_interval, confirmations_required)
                VALUES
                    ('ethereum', 'Ethereum', 'ERC-20', 'https://etherscan.io/tx/{tx}',
                     TRUE, 30, 12),
                    ('tron', 'TRON', 'TRC-20', 'https://tronscan.org/#/transaction/{tx}',
                     TRUE, 30, 20)
                """,
                """
                INSERT INTO cryptocurrency
                    (symbol, name, decimals, minimum_notification_amount, enabled)
                VALUES ('USDT', 'Tether USD', 6, 0.5, TRUE)
                """,
                """
                INSERT INTO network_asset
                    (cryptocurrency_id, network_id, contract_address, decimals, enabled)
                SELECT c.id, n.id, v.contract, 6, TRUE
                FROM (VALUES
                    ('ethereum', '0xdac17f958d2ee523a2206206994597c13d831ec7'),
                    ('tron', 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t')
                ) AS v(network, contract)
                JOIN network n ON n.code = v.network
                JOIN cryptocurrency c ON c.symbol = 'USDT'
                """,
            ],
            reverse_sql=[
                """
                DELETE FROM network_asset
                WHERE cryptocurrency_id = (SELECT id FROM cryptocurrency WHERE symbol = 'USDT')
                """,
                "DELETE FROM cryptocurrency WHERE symbol = 'USDT'",
                "DELETE FROM network WHERE code IN ('ethereum', 'tron')",
            ],
        ),
    ]
