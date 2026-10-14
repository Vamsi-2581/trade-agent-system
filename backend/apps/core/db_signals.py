"""Celery signal handlers for automatic Django DB connection cleanup."""

from celery.signals import task_postrun, task_prerun, worker_process_init
from django.db import close_old_connections, connections


@worker_process_init.connect
def init_worker(**kwargs):
    """Clean up inherited connections after fork."""
    for conn in connections.all():
        conn.close()
    close_old_connections()


@task_prerun.connect
def prep_task_db(**kwargs):
    """Ensure fresh DB connection before each Celery task runs."""
    for conn in connections.all():
        conn.close()
    close_old_connections()


@task_postrun.connect
def post_task_cleanup(**kwargs):
    """Close all Django DB connections after each Celery task."""
    for conn in connections.all():
        conn.close()
    close_old_connections()
