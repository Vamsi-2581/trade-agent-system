# Generated manually for P0-4: Mark non-Binance ExchangeAccount records as inactive

from django.db import migrations


def deactivate_non_binance(apps, schema_editor):
    """Mark all ExchangeAccount records whose exchange is not 'binance' as is_active=False."""
    ExchangeAccount = apps.get_model("exchange", "ExchangeAccount")
    updated = ExchangeAccount.objects.exclude(exchange="binance").update(
        is_active=False
    )
    if updated:
        print(f"  Deactivated {updated} non-Binance ExchangeAccount record(s).")


def reactivate_non_binance(apps, schema_editor):
    """Reverse: restore is_active=True for records that were deactivated."""
    ExchangeAccount = apps.get_model("exchange", "ExchangeAccount")
    updated = ExchangeAccount.objects.exclude(exchange="binance").update(is_active=True)
    if updated:
        print(f"  Reactivated {updated} ExchangeAccount record(s).")


class Migration(migrations.Migration):
    dependencies = [
        ("exchange", "0002_exchangeaccount_testnet"),
    ]

    operations = [
        migrations.RunPython(
            code=deactivate_non_binance,
            reverse_code=reactivate_non_binance,
        ),
    ]
