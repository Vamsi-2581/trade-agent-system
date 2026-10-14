from django.db import migrations


def create_system_scheduler_user(apps, schema_editor):
    """Create a system_scheduler user for associating scheduled/automated tasks."""
    User = apps.get_model("authentication", "User")
    if not User.objects.filter(username="system_scheduler").exists():
        from django.contrib.auth.hashers import make_password

        user = User(
            username="system_scheduler",
            email="scheduler@tradeclaw.local",
            is_active=True,
            is_frozen=False,
            password=make_password(None),
        )
        user.save()


def remove_system_scheduler_user(apps, schema_editor):
    User = apps.get_model("authentication", "User")
    User.objects.filter(username="system_scheduler").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("authentication", "0004_add_feishu_open_id"),
    ]

    operations = [
        migrations.RunPython(
            create_system_scheduler_user, remove_system_scheduler_user
        ),
    ]
