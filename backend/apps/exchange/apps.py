from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class ExchangeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.exchange"

    def ready(self):
        if not getattr(settings, "FERNET_KEY", ""):
            raise ImproperlyConfigured(
                "FERNET_KEY environment variable is required. "
                "Generate one with: python -c "
                '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
