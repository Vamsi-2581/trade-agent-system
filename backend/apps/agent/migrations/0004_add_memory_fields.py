from django.db import migrations, models
from django.db import connection


def backfill_agent_name_and_user_id(apps, schema_editor):
    """回填现有数据: agent_name = agent_type, user_id = 'migrated_unknown'"""
    AgentMemory = apps.get_model("agent", "AgentMemory")
    AgentMemory.objects.update(
        agent_name=models.F("agent_type"), user_id="migrated_unknown"
    )


def add_embedding_column(apps, schema_editor):
    """Add embedding column — on SQLite use TextField as fallback."""
    if connection.vendor == "sqlite":
        schema_editor.execute("ALTER TABLE agent_memory ADD COLUMN embedding TEXT")
    else:
        schema_editor.execute(
            "ALTER TABLE agent_memory ADD COLUMN embedding vector(768)"
        )


def remove_embedding_column(apps, schema_editor):
    """Remove embedding column — skip on SQLite."""
    if connection.vendor == "sqlite":
        return
    schema_editor.execute("ALTER TABLE agent_memory DROP COLUMN embedding")


class Migration(migrations.Migration):
    dependencies = [
        ("agent", "0003_add_user_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentmemory",
            name="user_id",
            field=models.CharField(max_length=64, default="", db_index=True),
        ),
        migrations.AddField(
            model_name="agentmemory",
            name="agent_name",
            field=models.CharField(max_length=32, default=""),
        ),
        migrations.AddField(
            model_name="agentmemory",
            name="session_id",
            field=models.CharField(max_length=64, blank=True, default=""),
        ),
        migrations.RunPython(add_embedding_column, remove_embedding_column),
        migrations.RunPython(backfill_agent_name_and_user_id),
        migrations.AddIndex(
            model_name="agentmemory",
            index=models.Index(fields=["user_id"], name="agent_memory_user_id_idx"),
        ),
    ]
